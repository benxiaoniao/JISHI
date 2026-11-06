/*!
 * 基石的 wasm 胶水（R8.1）—— **手写，不上 wasm-bindgen**。
 *
 * 为什么手写：本项目从 R0 起的核心资产是「**零第三方依赖**」，上 wasm-bindgen
 * 会连带拖进 `wasm-pack` 那套工具链与体积。而 wasm 的线性内存 + 几个
 * `extern "C"` 导出本来就够用 —— 要的只是下面这几十行。
 *
 * ## 用法
 *
 * ```js
 * const jishi = await JishiWasm.load("jishi_wasm.wasm");
 * const r = jishi.run('令 甲 = 1\n打印(甲 + 1)');
 * // → { ok: true, stdout: "2\n", value: null, durationMs: 0.21 }
 * ```
 *
 * ## 三件不做会出错的事
 *
 * 1. **每次调用都要重新取 `memory.buffer`**：`Uint8Array` 是建在**那一刻**的
 *    `ArrayBuffer` 上的；wasm 内存一扩容，旧的 buffer 会被**detach**（长度变 0），
 *    再往上写就是静默写空。所以下面每个 `new Uint8Array(ex.memory.buffer, …)`
 *    都是现取的。
 * 2. **`jishi_alloc` 配 `jishi_free`**：不配对就是每跑一次漏一块。
 * 3. **wasm 的 trap 要接住**：`panic = "abort"` 下 panic 变成 `RuntimeError`，
 *    比如无限递归撞栈上限 —— 那不是「程序报错」，得翻译成一句人能读的话。
 */
(function (root) {
  "use strict";

  /** ABI 版本：与 `wasm/src/lib.rs` 的 `ABI_VERSION` 必须一致。 */
  var ABI_VERSION = 3;   // M167 加 jishi_call / jishi_host_respond；M174 加 jishi_native

  function Jishi(instance, opts) {
    opts = opts || {};
    this._ex = instance.exports;
    this.memory = instance.exports.memory;
    this._enc = new TextEncoder();
    this._dec = new TextDecoder("utf-8");
    // M167：`浏览器` 模块的宿主侧接线。**可注入**是为了判据能在 Node 里
    // 用假 DOM 跑（真 DOM 的取值逻辑在浏览器里才验得了）。
    this._doc = opts.doc || (typeof document !== "undefined" ? document : null);
    this._store = opts.store || root.localStorage || null;
    this._fetch = opts.fetch || (root.fetch ? root.fetch.bind(root) : null);
    //: 上一次 `run` 的源码 —— `call`（事件回调）要**重跑一遍**它。见 `_hostCall`。
    this._src = "";
    //: 已绑定的监听（幂等）：`元素 → (事件|函数名) → 处理器`。
    //: 为什么幂等：事件回调会**重跑整个程序**，顶层那句 `绑定` 会再执行一次 ——
    //: 不幂等的话每点一次就多挂一个监听，点三次跑三遍。
    this._bound = new WeakMap();
    //: M175：宿主事件（菜单 / 拖放 / 托盘 / 再次启动）→ 基石函数名。
    this._当 = {};
    //: 有没有已经告诉宿主「可以派发事件了」（每次 run 会重新登记，别重复报）。
    this._当已报 = false;
  }

  /** 把一段字符串写进 wasm 内存，返回 `{ptr, len}`。调用方负责 `free`。 */
  Jishi.prototype._put = function (text) {
    var bytes = this._enc.encode(text);
    var ptr = this._ex.jishi_alloc(bytes.length);
    // ⚠️ 现取 buffer（见文件头第 1 条）
    new Uint8Array(this.memory.buffer, ptr, bytes.length).set(bytes);
    return { ptr: ptr, len: bytes.length };
  };

  /** 把「结果缓冲」读出来并释放临时内存。 */
  Jishi.prototype._take = function (moved, also) {
    var n = this._ex.jishi_out_len();
    var out = { ptr: 0, len: 0 };
    if (n > 0) {
      out = { ptr: this._ex.jishi_alloc(n), len: n };
      this._ex.jishi_out_copy(out.ptr);
    }
    var text = n > 0
      ? this._dec.decode(new Uint8Array(this.memory.buffer, out.ptr, n))
      : "";
    if (out.ptr) this._ex.jishi_free(out.ptr, out.len);
    this._ex.jishi_free(moved.ptr, moved.len);
    if (also) {
      for (var i = 0; i < also.length; i++) {
        this._ex.jishi_free(also[i].ptr, also[i].len);
      }
    }
    return text;
  };

  /** 跑一段源码。返回**协议 JSON 解析后的对象**（另加 `durationMs`）。 */
  Jishi.prototype.run = function (source) {
    this._src = String(source == null ? "" : source);
    return this._call("jishi_run", source);
  };

  /**
   * **事件回调**（M167）：重跑一遍源码，再调 `fnName`。
   *
   * > 一次事件 = 一次完整执行。
   *
   * ⚠️ **不是「只调那个函数」**：`jishi_run` 一返回，那次执行的 VM 就没了。
   * 所以是「重建全局状态 → 再执行处理函数」。状态要靠 `存本地` / DOM 自己
   * 持久化 —— 这正是「刷新还在」自然成立的原因。
   *
   * @param {string} fnName 函数名（具名函数；匿名的不行）
   * @param {string} [arg]  传给它的实参（`请求` 的响应文本等）
   */
  Jishi.prototype.call = function (fnName, arg) {
    if (!this._src) {
      return { ok: false, stdout: "", value: null,
               error: { code: null, title: "还没跑过程序",
                        message: "先 `run(源码)` 再 `call(函数名)`", line: null, col: null, hint: null } };
    }
    var t0 = (root.performance || Date).now();
    var m1 = this._put(this._src);
    var m2 = this._put(String(fnName || ""));
    var m3 = this._put(arg == null ? "" : String(arg));
    var text;
    try {
      this._ex.jishi_call(m1.ptr, m1.len, m2.ptr, m2.len, m3.ptr, m3.len);
      text = this._take(m1, [m2, m3]);
    } catch (e) {
      try {
        this._ex.jishi_free(m1.ptr, m1.len);
        this._ex.jishi_free(m2.ptr, m2.len);
        this._ex.jishi_free(m3.ptr, m3.len);
      } catch (_) {}
      return { ok: false, stdout: "", value: null,
               error: { code: null, title: "事件处理出错",
                        message: String(e && e.message || e), line: null, col: null, hint: null },
               durationMs: (root.performance || Date).now() - t0 };
    }
    var r;
    try { r = JSON.parse(text); }
    catch (_) { return { ok: false, stdout: "", value: null,
                         error: { code: null, title: "内部错误",
                                  message: "wasm 返回的结果不是合法 JSON", line: null, col: null, hint: null } }; }
    r.durationMs = (root.performance || Date).now() - t0;
    return r;
  };

  /** 求一个表达式的值（结果同 `run`）。 */
  Jishi.prototype.eval = function (expr) {
    return this._call("jishi_eval", expr);
  };

  Jishi.prototype._call = function (fn, source) {
    var t0 = (root.performance || Date).now();
    var moved = this._put(source == null ? "" : String(source));
    var text;
    try {
      this._ex[fn](moved.ptr, moved.len);
      text = this._take(moved);
    } catch (e) {
      // 释放我们那份，别因为异常漏掉
      try { this._ex.jishi_free(moved.ptr, moved.len); } catch (_) {}
      // wasm 的 trap（`RuntimeError: unreachable`）：多半是撞了 wasm 的栈上限
      // （无限递归）。**翻译成人话**，别把英文的 unreachable 甩给用户。
      var trap = e && e.constructor && e.constructor.name === "RuntimeError";
      return {
        ok: false,
        stdout: "",
        value: null,
        durationMs: (root.performance || Date).now() - t0,
        // ⚠️ 形状**与引擎给的失败一样**（`{ok:false, error:{code,title,…}}`）——
        // 这是刻意对齐的：页面只该认一个协议。别改成 `errors` 数组。
        error: {
          code: null,
          title: trap ? "程序跑飞了" : "引擎出错",
          message: trap
            ? "执行时撞上了运行环境的限制（常见于无限递归）"
            : String(e && e.message || e),
          line: null, col: null, hint: null,
        },
      };
    }
    var r;
    try {
      r = JSON.parse(text);
    } catch (_) {
      return {
        ok: false, stdout: "", value: null,
        error: {
          code: null,
          title: "内部错误",
          message: "wasm 返回的结果不是合法 JSON（这是引擎的 bug，不是你的代码）",
          line: null, col: null, hint: null,
        },
        durationMs: (root.performance || Date).now() - t0,
      };
    }
    // ⚠️ wasm 里**没有时钟**（`std::time::Instant::now()` 会 panic），所以
    // 协议里的 `duration_ms` 恒为 0 —— 真实耗时在这儿用 `performance.now()` 量，
    // 覆盖掉那个 0（页面上显示的才是真的）。
    r.durationMs = (root.performance || Date).now() - t0;
    return r;
  };

  // -------------------------------------------------------------------------
  // M167：`浏览器` 模块的宿主侧
  //
  // 基石用一条通道把**所有** DOM 操作问过来：`jishi_host_call(请求JSON)`，
  // 这边执行完调 `jishi_host_respond(响应JSON)` 写回去。加新操作只要在这里加
  // 一个 case，**不用动 ABI**。
  // -------------------------------------------------------------------------

  Jishi.prototype._hostCall = function (ptr, len) {
    var req;
    try {
      req = JSON.parse(this._dec.decode(new Uint8Array(this.memory.buffer, ptr, len)));
    } catch (_) {
      return this._respond({ ok: false, message: "内部：宿主请求不是合法 JSON" });
    }
    var 应答;
    try {
      应答 = { ok: true, value: this._doOp(req.op, req.args || []) };
    } catch (e) {
      应答 = { ok: false, message: String((e && e.message) || e) };
    }
    return this._respond(应答);
  };

  /**
   * M174（ABI 3）：宿主命令。`操作\0参数`，**单向** —— 发出去就不管了。
   *
   * 为什么不返回结果：基石的 wasm 执行是同步的，而 IPC 天生异步。要「等宿主
   * 回话」得把整个执行模型改成异步续体 —— 而真正要的两个命令（改标题、关窗）
   * 恰好都是单向的。MVP 不为了对称去改执行模型。
   *
   * 两代外壳都照顾到了：
   * - **原生窗口**（wry）：`root.ipc.postMessage(...)` → Rust 的 ipc_handler
   * - **应用模式**（浏览器 `--app`）：改 `document.title` / `window.close()`
   *   —— `--app` 窗口的标题栏显示的就是页面 title，所以**同一句代码都生效**
   */
  Jishi.prototype._native = function (opPtr, opLen, argPtr, argLen) {
    var 操作 = this._dec.decode(new Uint8Array(this.memory.buffer, opPtr, opLen));
    var 参数 = this._dec.decode(new Uint8Array(this.memory.buffer, argPtr, argLen));
    var ipc = root.ipc;
    var 有ipc = !!(ipc && typeof ipc.postMessage === "function");
    // ⚠️ **用 JSON，不要用 `操作\0参数`**：NUL 会在 WebView2 那一层被当终止符，
    //    宿主只收到 `"标题"`，参数整段消失（M174 实测踩过）。
    switch (操作) {
      case "标题":
        if (this._doc) this._doc.title = 参数;
        if (有ipc) ipc.postMessage(JSON.stringify({ op: "标题", arg: 参数 }));
        return 1;
      case "关闭":
        if (有ipc) { ipc.postMessage(JSON.stringify({ op: "关闭" })); return 1; }
        if (typeof root.close === "function") { root.close(); return 1; }
        return 0;
      // M175：页面里登记「宿主事件 → 基石函数」（`窗口.当`）。
      //
      // 🔴 这一条**不发给宿主**：宿主只需要知道「点了什么动作」，由页面自己
      //    决定哪个函数接（动作名 → 函数名的对应表留在页面里）。这样
      //    「换个页面 / 改个函数名」都不用动宿主，也不会多一轮 IPC。
      case "当": {
        var 单 = null;
        try { 单 = JSON.parse(参数); } catch (_) { return 0; }
        if (!单 || !单.事件 || !单.函数) return 0;
        this._当[单.事件] = 单.函数;
        // 第一次登记时告诉宿主「我可以接事件了」—— 宿主会把之前排队的事件
        // 补发过来（比如「第二个实例带着参数来了」，那可能发生在页面加载之前）。
        if (!this._当已报) {
          this._当已报 = true;
          if (有ipc) ipc.postMessage(JSON.stringify({ op: "宿主事件就绪" }));
        }
        var 自 = this;
        // 宿主用 `evaluate_script` 调这个（见 native_window.rs 的 `派发`）。
        // 走 `call` 而不是自己去碰导出函数：那是「一次事件 = 一次完整执行」
        // 的唯一入口（重跑源码 → 再调函数），与 DOM 事件同一条路。
        root.__jishiWin = function (事件名, 值) {
          var 名 = 自._当[事件名];
          if (!名) return;
          var 结果 = 自.call(名, 值 == null ? "" : String(值));
          // 🔴 事件通道最容易**吞错**（M167 的教训）：处理函数报错时人只会看到
          //    「点了没反应」。所以这里必须把错误显式吐出来 —— 控制台一份、
          //    宿主一份（宿主会打到调试通道上）。
          if (结果 && 结果.ok === false) {
            var 错 = 结果.error || {};
            var 文 = (错.title || "出错") + "：" + (错.message || "");
            try { console.error("[基石] 处理「" + 事件名 + "」出错：" + 文); } catch (_) {}
            // 现场读 `root.ipc`（注册那会儿可能还没有；而且页面可能被复用）
            if (root.ipc && root.ipc.postMessage) {
              root.ipc.postMessage(JSON.stringify({ op: "引擎失败", arg: 文 }));
            }
          }
        };
        return 1;
      }
      default:
        return 0;
    }
  };

  Jishi.prototype._respond = function (obj) {
    var bytes = this._enc.encode(JSON.stringify(obj));
    var p = this._ex.jishi_alloc(bytes.length);
    // ⚠️ 现取 buffer（见文件头第 1 条）
    new Uint8Array(this.memory.buffer, p, bytes.length).set(bytes);
    this._ex.jishi_host_respond(p, bytes.length);
    this._ex.jishi_free(p, bytes.length);
    return 0;
  };

  Jishi.prototype._sel = function (s) {
    if (!this._doc || !this._doc.querySelector) {
      throw new Error("这个环境没有页面（没有 DOM）");
    }
    var el = this._doc.querySelector(String(s));
    if (!el) throw new Error("页面上没有匹配「" + s + "」的元素");
    return el;
  };

  Jishi.prototype._needStore = function () {
    if (!this._store) throw new Error("这个环境没有 localStorage");
    return this._store;
  };

  Jishi.prototype._doOp = function (op, a) {
    var el;
    switch (op) {
      case "设文本": el = this._sel(a[0]); el.textContent = String(a[1]); return null;
      // ⚠️ 设的是 innerHTML —— 内容必须是**自己用 html 模块拼好的**，
      // 绝不能是用户输入（那是 XSS 的口子）。
      case "设HTML": el = this._sel(a[0]); el.innerHTML = String(a[1]); return null;
      case "取值": el = this._sel(a[0]); return el.value == null ? "" : String(el.value);
      case "设值": el = this._sel(a[0]); el.value = String(a[1]); return null;
      case "加类": this._sel(a[0]).classList.add(String(a[1])); return null;
      case "删类": this._sel(a[0]).classList.remove(String(a[1])); return null;
      case "切类": this._sel(a[0]).classList.toggle(String(a[1])); return null;
      case "点击": this._sel(a[0]).click(); return null;
      case "显示": el = this._sel(a[0]); el.style.display = (a[1] === false) ? "none" : ""; return null;
      case "绑定": this._bind(this._sel(a[0]), String(a[1]), String(a[2])); return null;
      case "存本地": this._needStore().setItem(String(a[0]), String(a[1])); return null;
      case "读本地": {
        var got = this._needStore().getItem(String(a[0]));
        // ⚠️ 「没存过」给 `空`，不是空文本 —— 两者在基石里要分得开
        return got === null || got === undefined ? null : got;
      }
      case "删本地": this._needStore().removeItem(String(a[0])); return null;
      case "请求": this._request(String(a[0]), String(a[1])); return null;
      default: throw new Error("浏览器模块里没有「" + op + "」这个操作");
    }
  };

  /// 绑事件（**幂等**：同一个 元素+事件+函数名 只挂一次）。
  ///
  /// 为什么必须幂等：事件回调会**重跑整个程序**，顶层那句 `绑定` 也会再执行一遍。
  /// 不幂等的话点一次挂两个监听、下次点就跑两遍，越点越慢。
  Jishi.prototype._bind = function (el, ev, fnName) {
    var self = this;
    var perEl = this._bound.get(el);
    if (!perEl) { perEl = {}; this._bound.set(el, perEl); }
    var key = ev + "|" + fnName;
    if (perEl[key]) return;
    // 回调的实参：**只有「点了带 `数据_序` 的元素」时才传**，否则不传。
    //
    // 为什么这么绕：`加一()` 是零参函数，传任何实参都会报「参数太多」；
    // 而「点叉删第 N 条」需要知道 N。第一个版本把 `e.type` 当实参传
    // （`加一("点击")`），于是每次都失败、错误还被事件通道吞掉 —— 表现是
    // 「点了没反应」，查了半天。现在的规矩：**有 `数据_序` 才带参**。
    var h = function (e) {
      var t = e && e.target;
      var 序 = (t && t.dataset && t.dataset["序"]) || "";
      self.call(fnName, 序);
    };
    perEl[key] = h;
    if (el.addEventListener) el.addEventListener(ev, h);
    else if (el.on) el.on(ev, h);        // 假 DOM（判据）用 on
  };

  /// 发请求，**回来的时候再进一次 VM** 跑处理函数。
  ///
  /// 异步不硬塞进同步：`请求` 立刻返回，响应到了由这边调 `call`。
  Jishi.prototype._request = function (url, fnName) {
    var self = this;
    if (!this._fetch) throw new Error("这个环境没有网络（没有 fetch）");
    this._fetch(url)
      .then(function (r) { return r.text(); })
      .then(function (t) { self.call(fnName, t); })
      .catch(function (e) { self.call(fnName, "错误：" + String((e && e.message) || e)); });
  };

  /** 设步数上限（**循环圈数**；`0` = 不限）。默认 2000 万圈（约 1~2 秒）。 */
  Jishi.prototype.setBudget = function (steps) {
    this._ex.jishi_set_budget(steps >>> 0);
  };

  /** 当前的步数上限。 */
  Jishi.prototype.getBudget = function () {
    return this._ex.jishi_get_budget();
  };

  var JishiWasm = {
    ABI_VERSION: ABI_VERSION,

    /**
     * 加载一份 `jishi_wasm.wasm`。
     *
     * @param {string|URL|ArrayBuffer|Uint8Array} src 网址，或直接给字节
     * @returns {Promise<Jishi>}
     */
    load: function (src, opts) {
      // M167：给 `浏览器` 模块接上宿主侧。用 holder 是因为 import 必须在
      // instantiate **之前**给出，而它要用到 instantiate **之后**才有的实例 ——
      // 所以它得在**外层**，不能写在某个 `.then` 里（那是另一个作用域）。
      var holder = { inst: null };
      var get;
      if (src instanceof ArrayBuffer || ArrayBuffer.isView(src)) {
        get = Promise.resolve(src);
      } else {
        get = fetch(src).then(function (r) {
          if (!r.ok) throw new Error("拿不到 wasm：" + r.status + " " + src);
          return r.arrayBuffer();
        });
      }
      return get.then(function (bytes) {
        // ⚠️ 直接把 `bytes` 交给 `instantiate`：规范接受 ArrayBuffer **或**
        // 任意 TypedArray 视图。**别写 `bytes.buffer`** —— Node 的 `Buffer` 是
        // 一块**共享内存池**上的视图，`.buffer` 会把整个池子（含别人的数据）
        // 交给 wasm，字节偏移全错。
        // 用 `instantiateStreaming` 会快一点，但它要求 `Content-Type: application/wasm`；
        // 静态托管有时给 `text/plain`，那就白等一次 —— 所以直接 instantiate。
        return WebAssembly.instantiate(bytes, {
          jishi: {
            jishi_host_call: function (ptr, len) {
              return holder.inst ? holder.inst._hostCall(ptr, len) : 0;
            },
            // M174（ABI 3）：页面里的基石代码 → 宿主（窗口）的单向命令。
            // wasm 那边是 `#[link(wasm_import_module = "jishi")]` 的 extern "C"，
            // **必须挂在这个模块名下**（写成 env.xxx 就找不到）。
            jishi_native: function (opPtr, opLen, argPtr, argLen) {
              return holder.inst ? holder.inst._native(opPtr, opLen, argPtr, argLen) : 0;
            },
          },
        });
      }).then(function (res) {
        var inst = new Jishi(res.instance, opts);
        holder.inst = inst;
        var v = inst._ex.jishi_version();
        if (v !== ABI_VERSION) {
          throw new Error(
            "wasm 与 shim 对不上：shim 要 ABI " + ABI_VERSION + "，拿到 " + v +
            "。跑 `python tools/build_wasm.py` 重新构建。");
        }
        return inst;
      });
    },
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = JishiWasm;        // Node（判据用它）
  }
  root.JishiWasm = JishiWasm;
})(typeof globalThis !== "undefined" ? globalThis : this);
