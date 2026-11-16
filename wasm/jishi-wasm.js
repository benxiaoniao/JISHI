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

    // ---- M196：离屏 / 裁剪 / 路径 / 渐变 / 混合 ----
    //: 离屏画布表：`句柄 → {cv, ctx, 宽, 高}`。
    this._离屏们 = {};
    this._离屏序 = 1;
    //: 当前绘制目标（`null` = 主画布）。**绘制函数一律看它，不看 `_ctx`**。
    this._目标 = null;
    //: 裁剪栈的层数（`设裁剪` +1，`清裁剪` -1）。
    this._裁剪层 = 0;
    //: 路径表：`句柄 → [[操作, ...参数], …]`。路径是**数据**，不是 ctx 状态。
    this._路径们 = {};
    this._路径序 = 1;
    //: 渐变表：`句柄 → CanvasGradient`。样式里写 `"grad:3"` 就能用。
    this._渐变们 = {};
    this._渐变序 = 1;
    //: 全局混合 / 透明度（`设混合` / `设透明度`，一直生效到下次改）。
    this._混合 = null;
    this._透明 = null;

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

  /**
   * M197：自动存 —— 记下「要存什么」，按间隔落盘。
   *
   * 🔴 **不能在每帧里直接写 localStorage**：它是**同步 IO**，每帧写一次会把主线程
   * 拖住（60 帧的游戏掉到 40 帧，而且看不出是谁的错）。所以这里只更新一份
   * 「最新快照」，真正写由 `setInterval` 按间隔做。
   *
   * 📌 定时器只起一个（重复调用只更新快照），`毫秒` 变了**不重起** ——
   * 每帧传不同间隔不该产生一堆定时器。
   */
  Jishi.prototype._自动存 = function (a) {
    var 包 = String(a[0]);
    var 毫 = Math.max(16, Number(a[1]) || 30000);
    var 键 = String(a[2]);
    var 索引键 = String(a[3]);
    var 槽名 = 键.slice(键.lastIndexOf("/") + 1);
    this._自动快照 = { 包: 包, 键: 键, 索引键: 索引键, 槽名: 槽名 };
    if (this._自动定时) return null;
    var 自 = this;
    this._自动定时 = setInterval(function () {
      var p = 自._自动快照;
      if (!p) return;
      try {
        var st = 自._needStore();
        st.setItem(p.键, p.包);
        var 名们 = [];
        try { 名们 = JSON.parse(st.getItem(p.索引键) || "[]"); } catch (_) { 名们 = []; }
        if (!Array.isArray(名们)) 名们 = [];
        if (名们.indexOf(p.槽名) < 0) {
          名们.push(p.槽名);
          st.setItem(p.索引键, JSON.stringify(名们));
        }
      } catch (_) { /* localStorage 满了 / 被禁 —— 不打断游戏 */ }
    }, 毫);
    if (this._自动定时 && this._自动定时.unref) this._自动定时.unref();
    return null;
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
      // M197：自动存（只更新「要存什么」，落盘由定时器做）
      case "自动存": return this._自动存(a);
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

      // ---- M196：离屏 / 裁剪 / 排版 / 路径 / 渐变 / 混合 ----
      case "离屏": return this._g离屏(a);
      case "用画布": return this._g用画布(a);
      case "贴到": return this._g贴到(a);
      case "离屏像素": return this._g离屏像素(a);
      case "设裁剪": return this._g设裁剪(a);
      case "清裁剪": return this._g清裁剪(a);
      case "文字折行": return this._g文字折行(a);
      case "写段落": return this._g写段落(a);
      case "路径": return this._g路径(a);
      case "移到": return this._g移到(a);
      case "线到": return this._g线到(a);
      case "弧到": return this._g弧到(a);
      case "闭合路径": return this._g闭合路径(a);
      case "填充路径": return this._g填充路径(a);
      case "描边路径": return this._g描边路径(a);
      case "线性渐变": return this._g线性渐变(a);
      case "径向渐变": return this._g径向渐变(a);
      case "设混合": return this._g设混合(a);
      case "设透明度": return this._g设透明度(a);

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
      // M197
      case "声音循环": return this._音循环(a);
      case "声音淡入": return this._音淡入(a);
      case "声音淡出": return this._音淡出(a);
      case "声音静音": return this._音静音(a);
      case "转场帧": return this._g转场帧(a);

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

  /** 拿 2d 上下文；**没准备过就自动准备一块**（少一步就少一个「忘了调用」的坑）。
   *
   * M196：先看 `_目标`（`图形.用画布` 切过去的那块离屏），没有才回主画布 ——
   * **所有绘制函数都从这里拿 ctx**，所以「画到离屏上」不需要改任何一个图元的实现。
   */
  Jishi.prototype._needCtx = function () {
    if (this._目标) return this._目标.ctx;
    if (!this._ctx) this._g准备(["[null,null,null]"]);
    return this._ctx;
  };

  /** 当前目标的逻辑尺寸（`清屏` 要按它填满）。 */
  Jishi.prototype._目标尺寸 = function () {
    if (this._目标) return [this._目标.宽, this._目标.高];
    return [this._宽, this._高];
  };

  /** 当前目标的 DPR。离屏画布按**逻辑像素**建（`width=宽`），所以恒为 1。 */
  Jishi.prototype._目标dpr = function () {
    return this._目标 ? 1 : this._dpr;
  };

  /** 绘制统一入口：带相机偏移 + `save/restore`（样式不泄漏到下一条图元）。
   *
   * M196：`设混合` / `设透明度` 是**持久状态**，在这里每条图元重新应用一遍 ——
   * `restore` 会把它们弹回去，不重设的话「设了只管一条」。
   */
  Jishi.prototype._画 = function (f) {
    var ctx = this._needCtx();
    ctx.save();
    ctx.translate(-this._相机.x, -this._相机.y);
    if (this._混合) ctx.globalCompositeOperation = this._混合;
    if (this._透明 != null) ctx.globalAlpha = this._透明;
    try { f(ctx); } finally { ctx.restore(); }
  };

  /**
   * 样式落地。`类` 决定「什么都没给时」的默认色：
   * 填充类给白、线给白、文本给白。
   */
  Jishi.prototype._样式 = function (ctx, s, 类) {
    s = s || {};
    var 自 = this;
    // M196：`"grad:3"` 换成真的 CanvasGradient。换不到就原样给 —— 那样 canvas 会
    // 忽略它（画不出东西），但**不会静默变成黑色**：黑色是最像「有效果」的假象。
    var 兑 = function (v) {
      if (typeof v === "string" && v.slice(0, 5) === "grad:") {
        var g = 自._渐变们[Number(v.slice(5))];
        if (g) return g;
      }
      return v;
    };
    var 填 = 兑(s["填充"]), 描 = 兑(s["描边"]);
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
    // M196：重新准备画布 = 回到主画布（否则绘制还留在上一条离屏上，
    // 「刷新了但还是画不出来」这种最难查）
    this._目标 = null;
    if (参[2]) { ctx.fillStyle = 参[2]; ctx.fillRect(0, 0, 宽, 高); }
    this._挂输入();
    return { 宽: 宽, 高: 高 };
  };

  Jishi.prototype._g清屏 = function (a) {
    var ctx = this._needCtx();
    var 尺 = this._目标尺寸();
    // 清屏**不吃相机**：它清的是整块画布，不是世界坐标里的某一块
    ctx.save();
    ctx.setTransform(this._目标dpr(), 0, 0, this._目标dpr(), 0, 0);
    ctx.globalAlpha = 1;
    ctx.fillStyle = String(a[0] || "#000");
    ctx.fillRect(0, 0, 尺[0], 尺[1]);
    ctx.restore();
    return null;
  };

  // ---- M196：离屏画布 ------------------------------------------------------

  /**
   * 建一块离屏画布，返回句柄。
   *
   * 📌 离屏画布**按逻辑像素建**（`width = 宽`，不乘 DPR）：它是拿来当中间结果
   * 用的，贴到主画布时由主画布负责缩放。乘了 DPR 反而会「贴上去大一圈」。
   */
  Jishi.prototype._g离屏 = function (a) {
    var 宽 = Math.max(1, Math.round(Number(a[0]) || 1));
    var 高 = Math.max(1, Math.round(Number(a[1]) || 1));
    var doc = this._doc;
    var cv = doc && doc.createElement ? doc.createElement("canvas") : null;
    if (!cv) throw new Error("这个环境建不了离屏画布（没有 document.createElement）");
    cv.width = 宽; cv.height = 高;
    var ctx = cv.getContext && cv.getContext("2d");
    if (!ctx) throw new Error("离屏画布拿不到 2d 上下文");
    var id = this._离屏序++;
    this._离屏们[id] = { cv: cv, ctx: ctx, 宽: 宽, 高: 高 };
    return id;
  };

  /** 切换绘制目标（`空` = 主画布）。 */
  Jishi.prototype._g用画布 = function (a) {
    if (a[0] == null || a[0] === undefined) {
      this._目标 = null;
      return null;
    }
    var id = Number(a[0]);
    var 块 = this._离屏们[id];
    if (!块) {
      throw new Error("没有句柄为 " + a[0] + " 的离屏画布 —— 先用 图形.离屏(宽, 高) 建一块");
    }
    this._目标 = 块;
    return null;
  };

  /** 把离屏画布贴到**当前目标**上。 */
  Jishi.prototype._g贴到 = function (a) {
    var 块 = this._离屏们[Number(a[0])];
    if (!块) {
      throw new Error("没有句柄为 " + a[0] + " 的离屏画布 —— 先用 图形.离屏(宽, 高) 建一块");
    }
    var 选 = JSON.parse(a[3] || "{}");
    var 透 = 选["透明度"];
    this._画(function (ctx) {
      if (透 != null) ctx.globalAlpha = Number(透);
      ctx.drawImage(块.cv, Number(a[1]) || 0, Number(a[2]) || 0);
    });
    return null;
  };

  /** 读离屏画布上一个像素（`[红,绿,蓝,透明]`）。 */
  Jishi.prototype._g离屏像素 = function (a) {
    var 块 = this._离屏们[Number(a[0])];
    if (!块) {
      throw new Error("没有句柄为 " + a[0] + " 的离屏画布 —— 先用 图形.离屏(宽, 高) 建一块");
    }
    var x = Math.floor(Number(a[1]) || 0);
    var y = Math.floor(Number(a[2]) || 0);
    if (x < 0 || y < 0 || x >= 块.宽 || y >= 块.高) {
      throw new Error("离屏像素 (" + x + ", " + y + ") 越界 —— 这块画布是 "
        + 块.宽 + " × " + 块.高);
    }
    var d = 块.ctx.getImageData(x, y, 1, 1).data;
    return [d[0], d[1], d[2], d.length > 3 ? d[3] : 255];
  };

  // ---- M196：裁剪 ----------------------------------------------------------

  /**
   * 把后面的绘制裁到一个矩形里。
   *
   * ⚠️ 存的是**世界坐标**（和所有图元一样吃相机）。`save()` 在这里配一次，
   * `清裁剪` 负责 `restore()` —— 所以 `设裁剪` / `清裁剪` **必须配对**，
   * 多清一次会报错（那基本一定是逻辑写错了）。
   */
  Jishi.prototype._g设裁剪 = function (a) {
    var 矩 = JSON.parse(a[0]);
    var ctx = this._needCtx();
    ctx.save();
    ctx.translate(-this._相机.x, -this._相机.y);
    ctx.beginPath();
    ctx.rect(矩[0], 矩[1], 矩[2], 矩[3]);
    ctx.clip();
    this._裁剪层 += 1;
    return null;
  };

  Jishi.prototype._g清裁剪 = function () {
    if (this._裁剪层 <= 0) {
      throw new Error("没有可以清掉的裁剪 —— 设裁剪 / 清裁剪 要配对");
    }
    this._needCtx().restore();
    this._裁剪层 -= 1;
    return null;
  };

  // ---- M196：文字排版 ------------------------------------------------------

  /**
   * 按最大宽度折行，返回行列表。
   *
   * 规则（**按真实字宽量，不按字数**）：
   * 1. 先按 `\n` 分段，每段各自折；
   * 2. 逐字符累加，超宽就断 —— 但如果这一行里有空格、且空格在行的**后半段**，
   *    就回退到那个空格断（英文单词不被劈开）；
   * 3. 段落里的空行保留成空串（用户显式写的空行不该被吃掉）。
   */
  Jishi.prototype._g文字折行 = function (a) {
    var 文 = String(a[0] == null ? "" : a[0]);
    var 限 = Number(a[1]) || 1;
    var 样 = JSON.parse(a[2] || "{}");
    var ctx = this._needCtx();
    ctx.save();
    ctx.font = (Number(样["字号"]) || 16) + "px sans-serif";

    var 量 = function (s) { return ctx.measureText(s).width; };
    var 行们 = [];
    var 段们 = 文.split("\n");
    for (var i = 0; i < 段们.length; i++) {
      var 段 = 段们[i];
      if (!段) { 行们.push(""); continue; }
      var 当前 = "";
      for (var k = 0; k < 段.length; k++) {
        var ch = 段[k];
        if (当前 && 量(当前 + ch) > 限) {
          var 空 = 当前.lastIndexOf(" ");
          if (空 > 0 && 空 >= 当前.length * 0.4) {
            行们.push(当前.slice(0, 空));
            当前 = 当前.slice(空 + 1) + ch;
          } else {
            行们.push(当前);
            当前 = ch;
          }
        } else {
          当前 += ch;
        }
      }
      if (当前) 行们.push(当前);
    }
    ctx.restore();
    return 行们;
  };

  /** 折行并逐行画。`y` 是**第一行的基线**。 */
  Jishi.prototype._g写段落 = function (a) {
    var 样 = JSON.parse(a[4] || "{}");
    var 行们 = this._g文字折行([a[0], a[3], a[4]]);
    var 号 = Number(样["字号"]) || 16;
    var 距 = Number(样["行距"]) || 号 * 1.5;
    var x = Number(a[1]) || 0;
    var y = Number(a[2]) || 0;
    var 自 = this;
    this._画(function (ctx) {
      自._样式(ctx, 样, "文本");
      for (var i = 0; i < 行们.length; i++) {
        if (行们[i]) ctx.fillText(行们[i], x, y + i * 距);
      }
    });
    return null;
  };

  // ---- M196：路径 ----------------------------------------------------------

  /**
   * 路径是**数据**（一串操作），不是 ctx 状态。
   *
   * 为什么不直接 beginPath/moveTo：ctx 的路径是**一次性**的 —— 填充完就没了，
   * 想「先描边再填充」或「平移着重画几遍」都得重放。存成数据后这些都是自然的，
   * 而且**判据能直接比对指令序列**（真 canvas 才需要看像素）。
   */
  Jishi.prototype._g路径 = function () {
    var id = this._路径序++;
    this._路径们[id] = [];
    return id;
  };

  Jishi.prototype._路径加 = function (id, 步) {
    var p = this._路径们[Number(id)];
    if (!p) {
      throw new Error("没有句柄为 " + id + " 的路径 —— 先用 图形.路径() 建一条");
    }
    p.push(步);
    return null;
  };

  Jishi.prototype._g移到 = function (a) {
    return this._路径加(a[0], ["moveTo", Number(a[1]) || 0, Number(a[2]) || 0]);
  };

  Jishi.prototype._g线到 = function (a) {
    return this._路径加(a[0], ["lineTo", Number(a[1]) || 0, Number(a[2]) || 0]);
  };

  Jishi.prototype._g弧到 = function (a) {
    return this._路径加(a[0], ["arcTo",
      Number(a[1]) || 0, Number(a[2]) || 0,
      Number(a[3]) || 0, Number(a[4]) || 0,
      Number(a[5]) || 0]);
  };

  Jishi.prototype._g闭合路径 = function (a) {
    return this._路径加(a[0], ["closePath"]);
  };

  /** 把存的步骤重放到 ctx 上（填充/描边共用）。 */
  Jishi.prototype._路径重放 = function (ctx, p) {
    ctx.beginPath();
    for (var i = 0; i < p.length; i++) {
      var 步 = p[i];
      if (步[0] === "moveTo") ctx.moveTo(步[1], 步[2]);
      else if (步[0] === "lineTo") ctx.lineTo(步[1], 步[2]);
      else if (步[0] === "arcTo") ctx.arcTo(步[1], 步[2], 步[3], 步[4], 步[5]);
      else if (步[0] === "closePath") ctx.closePath();
    }
  };

  Jishi.prototype._g填充路径 = function (a) {
    var p = this._路径们[Number(a[0])];
    if (!p) throw new Error("没有句柄为 " + a[0] + " 的路径 —— 先用 图形.路径() 建一条");
    var 样 = JSON.parse(a[1] || "{}");
    var 自 = this;
    this._画(function (ctx) {
      var s = 自._样式(ctx, 样, "填充");
      自._路径重放(ctx, p);
      if (s.填 != null) ctx.fill();
    });
    return null;
  };

  Jishi.prototype._g描边路径 = function (a) {
    var p = this._路径们[Number(a[0])];
    if (!p) throw new Error("没有句柄为 " + a[0] + " 的路径 —— 先用 图形.路径() 建一条");
    var 样 = JSON.parse(a[1] || "{}");
    var 自 = this;
    this._画(function (ctx) {
      var s = 自._样式(ctx, 样, "线");
      自._路径重放(ctx, p);
      if (s.描 != null) ctx.stroke();
    });
    return null;
  };

  // ---- M196：渐变 ----------------------------------------------------------

  /** 色标 JSON（`[[位置, 颜色], …]`）→ 往渐变对象上加。 */
  Jishi.prototype._上色标 = function (g, 色们) {
    for (var i = 0; i < 色们.length; i++) {
      var 项 = 色们[i];
      g.addColorStop(Math.max(0, Math.min(1, Number(项[0]))), String(项[1]));
    }
    return g;
  };

  /**
   * 线性渐变。返回值是 `"grad:3"` 这种标记 —— 能直接写进样式的
   * `"填充"` / `"描边"`（`_样式` 会兑成真的渐变对象）。
   *
   * 📌 坐标是**世界坐标**（跟着相机）。因为渐变对象是在 `_画` 的 translate
   * 之后建的，用户写什么就是什么，不用自己加相机偏移。
   */
  Jishi.prototype._g线性渐变 = function (a) {
    var ctx = this._needCtx();
    var g = ctx.createLinearGradient(
      Number(a[0]) || 0, Number(a[1]) || 0, Number(a[2]) || 0, Number(a[3]) || 0);
    this._上色标(g, JSON.parse(a[4] || "[]"));
    var id = this._渐变序++;
    this._渐变们[id] = g;
    return "grad:" + id;
  };

  Jishi.prototype._g径向渐变 = function (a) {
    var ctx = this._needCtx();
    var g = ctx.createRadialGradient(
      Number(a[0]) || 0, Number(a[1]) || 0, Number(a[2]) || 0,
      Number(a[0]) || 0, Number(a[1]) || 0, Number(a[3]) || 1);
    this._上色标(g, JSON.parse(a[4] || "[]"));
    var id = this._渐变序++;
    this._渐变们[id] = g;
    return "grad:" + id;
  };

  // ---- M196：混合 ----------------------------------------------------------

  Jishi.prototype._g设混合 = function (a) {
    this._混合 = a[0] == null ? null : String(a[0]);
    return null;
  };

  Jishi.prototype._g设透明度 = function (a) {
    var v = Number(a[0]);
    this._透明 = isFinite(v) ? Math.max(0, Math.min(1, v)) : null;
    return null;
  };

  /**
   * M197：转场遮罩。`(效果, 进度 0–1)`。
   *
   * 📌 它**不吃相机**（`setTransform` 到 dpr 原点）：遮罩盖的是整块画布，
   * 不是世界坐标里的某一块 —— 跟着相机走的话，镜头一移遮罩就露出边来。
   */
  Jishi.prototype._g转场帧 = function (a) {
    var 效 = String(a[0] || "");
    var 进 = Math.max(0, Math.min(1, Number(a[1]) || 0));
    var ctx = this._needCtx();
    var 尺 = this._目标尺寸();
    var d = this._目标dpr();
    ctx.save();
    ctx.setTransform(d, 0, 0, d, 0, 0);
    ctx.fillStyle = "#000";
    if (效 === "淡出") {
      ctx.globalAlpha = 进;
      ctx.fillRect(0, 0, 尺[0], 尺[1]);
    } else if (效 === "淡入") {
      ctx.globalAlpha = 1 - 进;
      ctx.fillRect(0, 0, 尺[0], 尺[1]);
    } else if (效 === "擦除") {
      ctx.fillRect(0, 0, 尺[0] * 进, 尺[1]);
    }
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

  /**
   * M197：把**正在放的**那条改成循环 / 不循环。
   *
   * 📌 与 `播放(句柄, {"循环": 真})` 分工：那个管「这一遍」；这个管「已经在放的」。
   * 没在放时只记下来，下次播放沿用。
   */
  Jishi.prototype._音循环 = function (a) {
    var 项 = this._音项(a[0]);
    var 开 = !!a[1];
    项.循环 = 开;
    if (项.源) 项.源.loop = 开;
    return null;
  };

  /**
   * M197：淡入 —— `毫秒` 内从 0 升到该声音的音量。
   *
   * 🔴 必须先 `cancelScheduledValues`：WebAudio 的自动化是**排队**的，
   * 不取消的话「淡出到一半又淡入」会把两段斜坡叠起来，音量会跳。
   */
  Jishi.prototype._音淡入 = function (a) {
    var 项 = this._音项(a[0]);
    if (!项.增) return false;
    var s = this._音();
    var 现 = s.上下文.currentTime;
    var 毫 = Math.max(1, Number(a[1]) || 1000) / 1000;
    var 目标 = 项.音量 == null ? 1 : 项.音量;
    项.增.gain.cancelScheduledValues(现);
    项.增.gain.setValueAtTime(0, 现);
    项.增.gain.linearRampToValueAtTime(目标, 现 + 毫);
    return true;
  };

  /** M197：淡出 —— `毫秒` 内降到 0，到 0 后自动停（不然它会「静音着继续放」）。 */
  Jishi.prototype._音淡出 = function (a) {
    var 项 = this._音项(a[0]);
    if (!项.增) return false;
    var s = this._音();
    var 现 = s.上下文.currentTime;
    var 毫 = Math.max(1, Number(a[1]) || 1000) / 1000;
    var 源 = 项.源;
    项.增.gain.cancelScheduledValues(现);
    项.增.gain.setValueAtTime(项.增.gain.value, 现);
    项.增.gain.linearRampToValueAtTime(0, 现 + 毫);
    if (源) {
      源.stop(现 + 毫 + 0.01);
    }
    return true;
  };

  /**
   * M197：全部静音 / 取消。
   *
   * 📌 记一份「静音前的音量」—— 取消时恢复它，而不是写死 1
   * （用户可能把总音量调到 0.3，静音再取消不该变成 1）。
   * 📌 静音期间 `在放` 照样给真：声音在走，只是听不见。
   */
  Jishi.prototype._音静音 = function (a) {
    var s = this._音();
    var 开 = !!a[0];
    if (开) {
      if (this._静音前 == null) this._静音前 = s.主.gain.value;
      s.主.gain.value = 0;
    } else if (this._静音前 != null) {
      s.主.gain.value = this._静音前;
      this._静音前 = null;
    }
    return null;
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
