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
  var ABI_VERSION = 4;   // M167 jishi_call；M174 jishi_native；M186 jishi_frame（常驻 VM）

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
    //: M186：帧循环的调度器。**可注入**是为了判据能在 Node 里完全确定地驱动
    //: （真跑用 `requestAnimationFrame`；判据自己按固定时间戳调 `frame()`）。
    //: ⚠️ **没有就明确报错**，不许偷偷退回 `setTimeout` —— 那会在 Node 里
    //: 起一个 60 次/秒的循环，和判据手动喂的帧**打架**（M186 实测：60 帧数出 61）。
    this._raf = opts.raf || (root.requestAnimationFrame
      ? root.requestAnimationFrame.bind(root) : null);
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

    // ---- M186：图形 ----
    //: `<canvas>` 与它的 2d 上下文（`图形.准备` 之后才有）。
    this._画布 = null;
    this._ctx = null;
    //: 画布的逻辑尺寸（与 `图形.宽度/高度` 一致）。
    this._宽 = 0;
    this._高 = 0;
    //: 相机偏移（`设相机`）。所有绘制都 `translate(-x, -y)`。
    this._相机 = { x: 0, y: 0 };
    //: 图像句柄表：`句柄 → { img, 就绪 }`。`读图` 是异步的，没加载完就跳过这次绘制。
    this._图 = {};
    this._图序 = 1;
    //: 输入状态。`按过` 是**边沿**（读一次就清），`按着` 是电平。
    this._键 = {};
    this._按过 = {};
    this._输入已挂 = false;
    this._鼠标 = { x: 0, y: 0, 左键: false, 右键: false };
    this._滚 = { dx: 0, dy: 0 };
    //: 帧循环是否在跑（rAF 泵）。
    this._帧中 = false;
    //: 最近一次帧错误的文本（判据要能读到；控制台也打一份）。
    this._帧错 = null;

    // ---- M187：声音 ----
    //: `undefined` = 还没建过；`null` = 这个环境没有 AudioContext。
    this._声 = undefined;
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

      // ---- M186：图形（同一条通道，多几个 case —— 通道形状没变）----
      case "准备": return this._g准备(a);
      case "清屏": return this._g清屏(a);
      case "矩形": return this._g形状("rect", a);
      case "圆": return this._g形状("circle", a);
      case "椭圆": return this._g形状("ellipse", a);
      case "线": return this._g形状("line", a);
      case "多边形": return this._g多边形(a);
      case "文本": return this._g文本(a);
      case "测文本": return this._g测文本(a);
      case "读图": return this._g读图(a);
      case "画图": return this._g画图(a, false);
      case "画子图": return this._g画图(a, true);
      case "取像素": return this._g取像素(a);
      case "设相机": this._相机 = { x: Number(a[0]) || 0, y: Number(a[1]) || 0 }; return null;
      case "读相机": return { x: this._相机.x, y: this._相机.y };
      case "按着": return !!this._键[this._键名(a[0])];
      case "按过": {
        var 键 = this._键名(a[0]);
        var 有 = !!this._按过[键];
        delete this._按过[键];       // 边沿：读一次就清
        return 有;
      }
      case "鼠标": return { x: this._鼠标.x, y: this._鼠标.y,
                            左键: this._鼠标.左键, 右键: this._鼠标.右键 };
      case "滚轮": {
        var 滚 = { dx: this._滚.dx, dy: this._滚.dy };
        this._滚 = { dx: 0, dy: 0 };   // 读完清零：它是「这一帧滚了多少」
        return 滚;
      }

      // ---- M186：帧循环的开与停（D110）----
      case "开帧": this._开帧(); return null;
      case "停帧": this._停帧(); return null;

      // ---- M187：声音（Web Audio）----
      case "声音载入": return this._音载入(a);
      case "声音播放": return this._音播放(a);
      case "声音停止": return this._音停止(a);
      case "声音在放": return this._音在放(a);
      case "声音音量": return this._音音量(a);
      case "声音主音量": return this._音主音量(a);
      case "声音读主音量": return this._音().主.gain.value;

      default: throw new Error("宿主里没有「" + op + "」这个操作");
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

  // -------------------------------------------------------------------------
  // M186：图形（画布 / 绘制 / 图像 / 相机 / 输入 / 帧循环）
  //
  // 全部走**同一条** `jishi_host_call` 通道（`_doOp` 里那几个 case），
  // 所以通道 ABI 一个字没改 —— 只是多了几个操作名（见 D110 §十）。
  //
  // ⚠️ 坐标系**一律逻辑像素**：位图按 devicePixelRatio 放大，但 `setTransform`
  //    一次性抵掉，所以下面所有绘制都用逻辑坐标，游戏代码不用管 DPR。
  // -------------------------------------------------------------------------

  /** 中文键名 → `KeyboardEvent.key`。单字符（字母/数字）不敏感大小写。 */
  var 键表 = {
    "左": "ArrowLeft", "右": "ArrowRight", "上": "ArrowUp", "下": "ArrowDown",
    "空格": " ", "回车": "Enter", "退出": "Escape", "上档": "Shift",
    "控制": "Control", "替换": "Alt", "制表": "Tab", "退格": "Backspace",
    "删除": "Delete", "翻页上": "PageUp", "翻页下": "PageDown",
  };

  Jishi.prototype._键名 = function (k) {
    k = String(k == null ? "" : k);
    if (键表[k]) return 键表[k];
    // `键表` 把「空格」映射成 `" "`，而浏览器 `e.key` **本来就是 `" "`** ——
    // 两边在 `" "` 上汇合（M188 核对过：`按着("空格")` 真的能读到）。
    return k.length === 1 ? k.toLowerCase() : k;
  };

  /** 拿 2d 上下文；**没准备过就自动准备一块**（少一步就少一个「忘了调用」的坑）。 */
  Jishi.prototype._needCtx = function () {
    if (!this._ctx) this._g准备(["[null,null,null]"]);
    return this._ctx;
  };

  /** 绘制统一入口：带相机偏移 + `save/restore`（样式不泄漏到下一条图元）。 */
  Jishi.prototype._画 = function (f) {
    var ctx = this._needCtx();
    ctx.save();
    ctx.translate(-this._相机.x, -this._相机.y);
    try { f(ctx); } finally { ctx.restore(); }
  };

  /**
   * 样式落地。`类` 决定「什么都没给时」的默认色：
   * 填充类给白、线给白、文本给白。
   */
  Jishi.prototype._样式 = function (ctx, s, 类) {
    s = s || {};
    var 填 = s["填充"], 描 = s["描边"];
    if (类 === "填充" && 填 == null) 填 = "#fff";
    if (类 === "线" && 描 == null) 描 = "#fff";
    if (类 === "文本" && 填 == null) 填 = "#fff";
    if (填 != null) ctx.fillStyle = 填;
    if (描 != null) ctx.strokeStyle = 描;
    if (s["线宽"] != null) ctx.lineWidth = Number(s["线宽"]) || 1;
    if (s["字号"] != null) ctx.font = (Number(s["字号"]) || 16) + "px sans-serif";
    if (s["对齐"] != null) {
      ctx.textAlign = ({ "左": "left", "中": "center", "右": "right" })[s["对齐"]] || "left";
    }
    if (s["透明度"] != null) ctx.globalAlpha = Number(s["透明度"]);
    return { 填: 填, 描: 描 };
  };

  Jishi.prototype._g准备 = function (a) {
    var 参 = JSON.parse(a[0]);
    var dpr = Number(root.devicePixelRatio) || 1;
    var doc = this._doc;
    if (!doc || !doc.querySelector) throw new Error("这个环境没有页面（没有 DOM）");
    var cv = doc.querySelector("#画布");
    if (!cv && doc.createElement && doc.body && doc.body.appendChild) {
      cv = doc.createElement("canvas");
      cv.id = "画布";
      doc.body.appendChild(cv);
    }
    if (!cv) throw new Error('页面上没有 <canvas id="画布">，也没法自己插一块');
    var 宽 = 参[0] == null ? (cv.clientWidth || root.innerWidth || 800) : Number(参[0]);
    var 高 = 参[1] == null ? (cv.clientHeight || root.innerHeight || 600) : Number(参[1]);
    // 位图放大、CSS 尺寸不变 ⇒ 看着清楚，坐标仍是逻辑像素
    cv.width = Math.max(1, Math.round(宽 * dpr));
    cv.height = Math.max(1, Math.round(高 * dpr));
    if (cv.style) { cv.style.width = 宽 + "px"; cv.style.height = 高 + "px"; }
    var ctx = cv.getContext && cv.getContext("2d");
    if (!ctx) throw new Error("这块 <canvas> 拿不到 2d 上下文");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._画布 = cv; this._ctx = ctx; this._dpr = dpr;
    this._宽 = 宽; this._高 = 高;
    if (参[2]) { ctx.fillStyle = 参[2]; ctx.fillRect(0, 0, 宽, 高); }
    this._挂输入();
    return { 宽: 宽, 高: 高 };
  };

  Jishi.prototype._g清屏 = function (a) {
    var ctx = this._needCtx();
    // 清屏**不吃相机**：它清的是整块画布，不是世界坐标里的某一块
    ctx.save();
    ctx.setTransform(this._dpr, 0, 0, this._dpr, 0, 0);
    ctx.globalAlpha = 1;
    ctx.fillStyle = String(a[0] || "#000");
    ctx.fillRect(0, 0, this._宽, this._高);
    ctx.restore();
    return null;
  };

  Jishi.prototype._g形状 = function (种, a) {
    var 自 = this;
    this._画(function (ctx) {
      if (种 === "rect") {
        var s = 自._样式(ctx, JSON.parse(a[4] || "{}"), "填充");
        var 圆角 = Number((JSON.parse(a[4] || "{}")["圆角"]) || 0);
        if (圆角 > 0 && ctx.roundRect) {
          ctx.beginPath(); ctx.roundRect(a[0], a[1], a[2], a[3], 圆角);
          if (s.填 != null) ctx.fill();
          if (s.描 != null) ctx.stroke();
        } else {
          if (s.填 != null) ctx.fillRect(a[0], a[1], a[2], a[3]);
          if (s.描 != null) ctx.strokeRect(a[0], a[1], a[2], a[3]);
        }
      } else if (种 === "circle" || 种 === "ellipse") {
        var 选 = JSON.parse(a[种 === "circle" ? 3 : 4] || "{}");
        var s2 = 自._样式(ctx, 选, "填充");
        ctx.beginPath();
        if (种 === "circle") ctx.arc(a[0], a[1], Math.abs(a[2]), 0, Math.PI * 2);
        else ctx.ellipse(a[0], a[1], Math.abs(a[2]), Math.abs(a[3]), 0, 0, Math.PI * 2);
        if (s2.填 != null) ctx.fill();
        if (s2.描 != null) ctx.stroke();
      } else {
        var s3 = 自._样式(ctx, JSON.parse(a[4] || "{}"), "线");
        ctx.beginPath();
        ctx.moveTo(a[0], a[1]);
        ctx.lineTo(a[2], a[3]);
        ctx.stroke();
      }
    });
    return null;
  };

  Jishi.prototype._g多边形 = function (a) {
    var 自 = this;
    var 点 = JSON.parse(a[0]);
    this._画(function (ctx) {
      var s = 自._样式(ctx, JSON.parse(a[1] || "{}"), "填充");
      if (!点.length) return;
      ctx.beginPath();
      ctx.moveTo(点[0][0], 点[0][1]);
      for (var i = 1; i < 点.length; i++) ctx.lineTo(点[i][0], 点[i][1]);
      ctx.closePath();
      if (s.填 != null) ctx.fill();
      if (s.描 != null) ctx.stroke();
    });
    return null;
  };

  Jishi.prototype._g文本 = function (a) {
    var 自 = this;
    this._画(function (ctx) {
      var s = 自._样式(ctx, JSON.parse(a[3] || "{}"), "文本");
      ctx.textBaseline = "alphabetic";
      ctx.fillText(String(a[0]), a[1], a[2]);
      if (s.描 != null) ctx.strokeText(String(a[0]), a[1], a[2]);
    });
    return null;
  };

  Jishi.prototype._g测文本 = function (a) {
    var ctx = this._needCtx();
    var 选 = JSON.parse(a[1] || "{}");
    ctx.save();
    if (选["字号"] != null) ctx.font = (Number(选["字号"]) || 16) + "px sans-serif";
    var m = ctx.measureText(String(a[0]));
    var 高 = (m.actualBoundingBoxAscent || 0) + (m.actualBoundingBoxDescent || 0);
    ctx.restore();
    if (!高) 高 = Number(选["字号"] || 16) * 1.2;
    return { 宽: m.width || 0, 高: 高 };
  };

  /**
   * `读图`：**异步**。返回句柄立刻可用于绘制；没加载完的那几帧**跳过绘制**
   * （不阻塞帧 —— 阻塞一帧就是卡一帧，宁可不画）。
   */
  Jishi.prototype._g读图 = function (a) {
    var 句 = this._图序++;
    var 项 = { img: null, 就绪: false, 错: false };
    this._图[句] = 项;
    var Img = root.Image;
    if (!Img) return 句;              // 这个环境没有 Image（判据的假 DOM）⇒ 永远不画
    var img = new Img();
    img.onload = function () { 项.就绪 = true; };
    img.onerror = function () { 项.错 = true; };
    img.src = String(a[0]);
    项.img = img;
    return 句;
  };

  Jishi.prototype._g画图 = function (a, 子) {
    var 项 = this._图[a[0]];
    if (!项 || !项.就绪) return null;   // 没加载完 / 没这张图：跳过这次绘制
    var img = 项.img;
    var 选 = JSON.parse((子 ? a[9] : a[3]) || "{}");
    var 转 = (Number(选["旋转"]) || 0) * Math.PI / 180;
    var 缩 = 选["缩放"] == null ? 1 : Number(选["缩放"]);
    var 翻 = 选["翻转"];
    var 翻x = 翻 === "横" || 翻 === "水平" ? -1 : 1;
    var 翻y = 翻 === "纵" || 翻 === "垂直" ? -1 : 1;
    var 自 = this;
    this._画(function (ctx) {
      if (选["透明度"] != null) ctx.globalAlpha = Number(选["透明度"]);
      var dx, dy, dw, dh, sx = 0, sy = 0, sw = 0, sh = 0;
      if (子) {
        sx = a[1]; sy = a[2]; sw = a[3]; sh = a[4];
        dx = a[5]; dy = a[6]; dw = a[7]; dh = a[8];
      } else {
        sw = img.width || img.naturalWidth || 0;
        sh = img.height || img.naturalHeight || 0;
        dw = sw * 缩; dh = sh * 缩;
        dx = a[1]; dy = a[2];
      }
      // 旋转绕**目标框中心**（不这样转，图会绕着左上角飞出去）
      ctx.translate(dx + dw / 2, dy + dh / 2);
      if (转) ctx.rotate(转);
      if (翻x < 0 || 翻y < 0) ctx.scale(翻x, 翻y);
      ctx.drawImage(img, sx, sy, sw, sh, -dw / 2, -dh / 2, dw, dh);
    });
    return null;
  };

  Jishi.prototype._g取像素 = function (a) {
    var ctx = this._needCtx();
    var dpr = this._dpr;
    var d = ctx.getImageData(Math.round(a[0] * dpr), Math.round(a[1] * dpr), 1, 1).data;
    return [d[0], d[1], d[2], d[3]];
  };

  // -------------------------------------------------------------------------
  // M187：声音（Web Audio）
  //
  // 🔴 **浏览器自动播放策略**（用的人一定会撞）：页面加载后、用户还没点过/按过
  // 之前 `AudioContext` 是 `suspended` 的 —— 这时 `start()` 不响、也**不报错**。
  // 所以这里的规矩是：
  //
  // - 没解锁 ⇒ `播放` **返回假**（不是报错）—— 调用方据此知道「等用户动一下」；
  // - 第一次 `pointerdown` / `keydown` / `mousedown` 里 `resume()`（那一次手势
  //   就是解锁的钥匙），监听**挂上就不摘**（标签页切回来可能又 suspended）。
  //
  // 句柄是数字（与 `图形.读图` 同形），表格在这里，wasm 那边只存数字。
  // -------------------------------------------------------------------------
  Jishi.prototype._音 = function () {
    if (this._声 === null) throw new Error("这个环境没有声音（没有 AudioContext）");
    if (this._声 !== undefined) return this._声;
    var 造 = root.AudioContext || root.webkitAudioContext;
    if (!造) {
      // 没有 Web Audio：明确报错，**不假装能放**
      this._声 = null;
      throw new Error("这个环境没有声音（没有 AudioContext）");
    }
    var ctx = new 造();
    var 主 = ctx.createGain();
    主.gain.value = 1;
    主.connect(ctx.destination);
    this._声 = { 上下文: ctx, 主: 主, 表: {}, 序: 1 };
    var 解 = function () {
      if (ctx.state === "suspended" && ctx.resume) ctx.resume();
    };
    if (root.addEventListener) {
      root.addEventListener("pointerdown", 解);
      root.addEventListener("mousedown", 解);
      root.addEventListener("keydown", 解);
    }
    return this._声;
  };

  /// 取一项（句柄不存在 ⇒ 报错，别静默当没放）。
  Jishi.prototype._音项 = function (号) {
    var s = this._音();
    var 项 = s.表[String(号)];
    if (!项) {
      throw new Error("没有「" + 号 + "」这个声音句柄（先 `声音.载入(路径)` 拿一个）");
    }
    return 项;
  };

  Jishi.prototype._音载入 = function (a) {
    var s = this._音();
    var 路径 = String(a[0]);
    var 号 = s.序++;
    var 项 = { 缓冲: null, 源: null, 增益: null, 在放: false, 音量: 1, 错: null };
    s.表[String(号)] = 项;
    var 自 = this;
    var 读 = this._fetch || (root.fetch ? root.fetch.bind(root) : null);
    if (!读) {
      项.错 = "这个环境没有网络（没有 fetch），读不了声音文件";
      return 号;
    }
    // 解码是**异步**的：没解完之前 `播放` 返回假（不是报错）
    读(路径)
      .then(function (r) {
        if (r && r.ok === false) throw new Error("HTTP " + r.status);
        return r.arrayBuffer();
      })
      .then(function (ab) {
        return new Promise(function (行, 不行) {
          // 老浏览器只认回调形式；新的两个都收
          s.上下文.decodeAudioData(ab, 行, 不行);
        });
      })
      .then(function (buf) { 项.缓冲 = buf; })
      .catch(function (e) { 项.错 = String((e && e.message) || e); });
    return 号;
  };

  Jishi.prototype._音播放 = function (a) {
    var 项 = this._音项(a[0]);
    var s = this._音();
    // ① 还没解锁 ② 还没解码完 ③ 文件读失败了 —— 三种都**返回假**
    if (s.上下文.state !== "running") return false;
    if (!项.缓冲) return false;
    var 选 = {};
    try { 选 = JSON.parse(a[1] || "{}") || {}; } catch (_) { 选 = {}; }
    // 同一句柄再放一次 ⇒ 先停掉旧的（不然会有两条声音叠着）
    if (项.源) { try { 项.源.stop(); } catch (_) {} 项.源 = null; }
    var 源 = s.上下文.createBufferSource();
    源.buffer = 项.缓冲;
    源.loop = !!选["循环"];
    if (选["速度"] != null) 源.playbackRate.value = Number(选["速度"]) || 1;
    var 增 = s.上下文.createGain();
    var 量 = 选["音量"] != null ? Number(选["音量"]) : 项.音量;
    增.gain.value = 量 == null || isNaN(量) ? 1 : 量;
    源.connect(增);
    增.connect(s.主);
    项.源 = 源; 项.增 = 增; 项.在放 = true;
    源.onended = function () {
      if (项.源 === 源) { 项.在放 = false; 项.源 = null; }
    };
    源.start(0);
    return true;
  };

  Jishi.prototype._音停止 = function (a) {
    var 项 = this._音项(a[0]);
    if (项.源) {
      try { 项.源.stop(); } catch (_) {}
      项.源 = null;
    }
    项.在放 = false;
    return null;
  };

  Jishi.prototype._音在放 = function (a) {
    return !!this._音项(a[0]).在放;
  };

  Jishi.prototype._音音量 = function (a) {
    var 项 = this._音项(a[0]);
    var 量 = Number(a[1]);
    if (isNaN(量)) throw new Error("「声音.设音量」的音量要数字");
    项.音量 = 量;
    if (项.增) 项.增.gain.value = 量;    // 正在放的也跟着变
    return 量;
  };

  Jishi.prototype._音主音量 = function (a) {
    var s = this._音();
    var 量 = Number(a[0]);
    if (isNaN(量)) throw new Error("「声音.主音量」的音量要数字");
    s.主.gain.value = 量;
    return 量;
  };

  /**
   * 挂输入监听（**只挂一次**）。
   *
   * ⚠️ 挂在 `window` 上而不是画布上：画布要能拿到焦点才收键盘，而刚插进去的
   * `<canvas>` 没有 `tabindex`，`keydown` 根本不来（M186 的坑，写在这儿）。
   * 代价是「页面里别再依赖全局按键」—— 记在文档里。
   */
  Jishi.prototype._挂输入 = function () {
    if (this._输入已挂) return;
    this._输入已挂 = true;
    var w = root;
    if (!w || !w.addEventListener) return;   // 假环境：判据直接喂状态
    var 自 = this;
    w.addEventListener("keydown", function (e) {
      var k = 自._键名(e.key);
      自._键[k] = true;
      if (!e.repeat) 自._按过[k] = true;
      // 方向键 / 空格会滚页面 —— 游戏里那是灾难
      if (e.preventDefault && 自._要拦(e.key)) e.preventDefault();
    });
    w.addEventListener("keyup", function (e) { 自._键[自._键名(e.key)] = false; });
    w.addEventListener("mousemove", function (e) {
      自._鼠标.x = e.clientX; 自._鼠标.y = e.clientY;
    });
    w.addEventListener("mousedown", function (e) {
      if (e.button === 0) 自._鼠标.左键 = true;
      else if (e.button === 2) 自._鼠标.右键 = true;
    });
    w.addEventListener("mouseup", function (e) {
      if (e.button === 0) 自._鼠标.左键 = false;
      else if (e.button === 2) 自._鼠标.右键 = false;
    });
    w.addEventListener("contextmenu", function (e) {
      if (e.preventDefault) e.preventDefault();
    });
    w.addEventListener("wheel", function (e) {
      自._滚.dx += Number(e.deltaX) || 0;
      自._滚.dy += Number(e.deltaY) || 0;
    }, { passive: true });
  };

  /** 哪些键要吃掉默认行为（只吃方向键 / 空格 —— 别把 F5 也吃了）。 */
  Jishi.prototype._要拦 = function (k) {
    return k === "ArrowLeft" || k === "ArrowRight" || k === "ArrowUp"
        || k === "ArrowDown" || k === " " || k === "Tab";
  };

  /** 把结果缓冲读出来（**不释放别的东西**，与 `_take` 分开写）。 */
  Jishi.prototype._取输出 = function () {
    var n = this._ex.jishi_out_len();
    if (!n) return "";
    var p = this._ex.jishi_alloc(n);
    this._ex.jishi_out_copy(p);
    var 文 = this._dec.decode(new Uint8Array(this.memory.buffer, p, n));
    this._ex.jishi_free(p, n);
    return 文;
  };

  /**
   * 开帧（`图形.运行` 之后由 wasm 入口发 `{"op":"开帧"}`）。
   *
   * 泵在这儿而不是在 Rust：wasm 没有线程、没有定时器，而 `requestAnimationFrame`
   * 恰好同时满足「与显示器同步」和「后台自动停」（D110 §四）。
   */
  Jishi.prototype._开帧 = function () {
    if (typeof this._ex.jishi_frame !== "function") {
      throw new Error("这份 wasm 太旧（没有 jishi_frame，ABI 4 才有）——"
        + "重跑 `python tools/build_wasm.py`");
    }
    if (this._帧中) return;
    this._帧中 = true;
    this._帧错 = null;
    if (!this._raf) {
      throw new Error("这个环境没有 requestAnimationFrame —— 帧循环只能在浏览器 / WebView 里跑"
        + "（判据要驱动它请注入 `raf`）");
    }
    var 自 = this;
    var raf = this._raf;
    var 走 = function (ts) {
      if (!自._帧中) return;
      var 码;
      try {
        码 = 自._ex.jishi_frame(Number(ts) || 0);
      } catch (e) {
        自._帧中 = false;
        自._帧错 = String((e && e.message) || e);
        try { console.error("[基石] 帧循环抛了异常：" + 自._帧错); } catch (_) {}
        return;
      }
      if (码 === 3) { 自._帧中 = false; return; }
      if (码 === 2) {
        自._帧中 = false;
        自._帧错 = 自._取输出();
        try { console.error("[基石] 帧循环停了：" + 自._帧错); } catch (_) {}
        return;
      }
      // 0：这一帧的 `打印` 打到控制台（引擎那边已经把它从缓冲里取走了）
      var 文 = 自._取输出();
      if (文) { try { console.log(文); } catch (_) {} }
      raf(走);
    };
    raf(走);
  };

  /** 停帧（`图形.停`）—— 只是不再排下一帧，VM 由引擎那边释放。 */
  Jishi.prototype._停帧 = function () {
    this._帧中 = false;
  };

  /** 帧循环在不在跑（判据 / 页面自检用）。 */
  Jishi.prototype.framesActive = function () {
    return !!this._帧中;
  };

  /**
   * **手动跑一帧**（判据专用）。
   *
   * 为什么要有：真跑时 `dt` 来自 rAF 的时间戳，判据没法控制；而「第 60 帧
   * 状态还在不在」这种断言必须**确定性**。判据自己按固定时间戳喂
   * （0 / 16 / 32 …），于是 `dt` 与帧号都完全可预期。
   */
  Jishi.prototype.frame = function (ts) {
    this._喂时间 && this._喂时间(this);
    var 码 = this._ex.jishi_frame(Number(ts) || 0);
    return { code: 码, out: this._取输出() };
  };

  /**
   * 把「现在」喂给 wasm 里的 `时间` 模块（M188）。
   *
   * 🔴 为什么要有：`wasm32-unknown-unknown` **没有时钟**，`SystemTime::now()`
   * 在那边是**直接 panic**。所以时间只能由宿主告诉它。
   * 加载时喂一次（一次性程序够用），`frame()` 每帧再喂一次（游戏里的时间会走）。
   */
  Jishi.prototype._喂时间 = function (inst) {
    var 目 = inst || this;
    if (!目._ex) return;
    if (typeof 目._ex.jishi_now === "function") 目._ex.jishi_now(Date.now());
    // 时区只要喂一次：`getTimezoneOffset()` 是「本地比 UTC 慢几分钟」，
    // 取反才是「快几分钟」（东八区 = 480）。DST 变了的极端情况这里不管。
    if (!目._喂过时区 && typeof 目._ex.jishi_tz === "function") {
      目._ex.jishi_tz(-new Date().getTimezoneOffset());
      目._喂过时区 = true;
    }
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
        // M188：给「随机」模块一个种子。
        // 🔴 wasm 里**没有时钟** —— `SystemTime::now()` 在 wasm32-unknown-unknown 上
        //    是**直接 panic**（不是返回 0），`随机.随机整数` 一调就把引擎炸了。
        //    `Math.random()` 是这里唯一现成的熵，加载时喂一个进去。
        if (typeof inst._ex.jishi_seed === "function") {
          inst._ex.jishi_seed(
            Math.floor(Math.random() * 4294967296) * 4294967296
            + Math.floor(Math.random() * 4294967296));
        }
        // M188：告诉「时间」模块现在几点。
        // 🔴 wasm 里**没有时钟**（`SystemTime::now()` 会 panic）——
        //    `时间.时间戳()` / `时间.今天()` 曾经一调就把引擎炸掉。
        //    这里塞一次，`frame()` 里每帧再塞一次，时间才会走。
        inst._喂时间(inst);
        return inst;
      });
    },
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = JishiWasm;        // Node（判据用它）
  }
  root.JishiWasm = JishiWasm;
})(typeof globalThis !== "undefined" ? globalThis : this);
