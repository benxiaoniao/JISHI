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
  var ABI_VERSION = 1;

  function Jishi(instance) {
    this._ex = instance.exports;
    this.memory = instance.exports.memory;
    this._enc = new TextEncoder();
    this._dec = new TextDecoder("utf-8");
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
  Jishi.prototype._take = function (moved) {
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
    return text;
  };

  /** 跑一段源码。返回**协议 JSON 解析后的对象**（另加 `durationMs`）。 */
  Jishi.prototype.run = function (source) {
    return this._call("jishi_run", source);
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
    load: function (src) {
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
        return WebAssembly.instantiate(bytes, {});
      }).then(function (res) {
        var inst = new Jishi(res.instance);
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
