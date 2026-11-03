//! 基石的 **wasm 入口**（R8.1）—— 给网页 playground 用。
//!
//! **裸 C ABI + 手写 JS shim，不上 `wasm-bindgen`**（2026-10-07 拍板）：
//! 保住「**零第三方依赖**」这条从 R0 起的核心资产。代价是自己管内存 ——
//! 所以下面有 `jishi_alloc` / `jishi_free` 两个导出，JS 侧那 30 行 shim 负责
//! 「把源码写进 wasm 内存 → 调 → 把结果读出来」（见 `site/jishi-wasm.js`）。
//!
//! ## 与原生宿主的**差异**（如实登记，只有这一处）
//!
//! wasm 上**没有线程**，`sandbox.rs` 那套「另起线程 + `recv_timeout`」的超时
//! 用不了（`std::thread` 在 `wasm32-unknown-unknown` 上会 panic）。所以这里走
//! [`jishi_ffi::sandbox::run_sandboxed_direct`]：**同步跑 + 步数预算**兜死循环。
//!
//! ⇒ 于是**结果 JSON 的形状与原生完全一样**（同一个 `SandboxOpts`、同一份
//! `success_json` / `failure_json`），差别只在「怎么停下来」：
//! 原生是「到点真 kill」，wasm 是「转够圈数就报错」。
//!
//! ## ABI（就这么几个导出）
//!
//! | 导出 | 作用 |
//! |---|---|
//! | `jishi_alloc(len)` / `jishi_free(ptr, len)` | 让 JS 往 wasm 内存里塞字符串 |
//! | `jishi_run(ptr, len)` | 跑一段源码 → 结果进「结果缓冲」 |
//! | `jishi_eval(ptr, len)` | 求一个表达式 → 同上 |
//! | `jishi_out_len()` / `jishi_out_copy(dst)` | 把结果缓冲取出来 |
//! | `jishi_set_budget(steps)` | 设步数上限（**0 = 不限**） |
//! | `jishi_version()` | 自检用：非 0 就说明这份 wasm 是我们的 |
//!
//! ⚠️ **一个实例只跑一件事**：结果放在 `thread_local` 的缓冲里，下一次调用覆盖。
//! playground 是「点一下跑一次」，够用；要并发就往 `Worker` 里塞实例。

use std::cell::RefCell;

use jishi_ffi::sandbox::{self, SandboxOpts};

const ABI_VERSION: i32 = 2;   // M167：加 jishi_call / jishi_host_respond

thread_local! {
    /// 上一次调用的结果（**协议 JSON 文本**，与原生 `--json-result` 同形）。
    static OUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

/// 把一段 Rust 字符串放进结果缓冲。
fn set_out(text: String) {
    OUT.with(|o| *o.borrow_mut() = text.into_bytes());
}

/// 当前沙箱选项：默认值（与 Python 侧逐条对齐）＋ 由 JS 侧设的步数预算。
fn opts() -> SandboxOpts {
    SandboxOpts { max_steps: budget(), ..Default::default() }
}

/// **默认步数上限**（循环圈数）—— 20M 圈在 wasm 上大约 1~2 秒。
///
/// ⚠️ **默认是有限值，不是「不限」**：站点要是忘了设，一个死循环就会把标签页
/// 冻住。要放开得**显式**调 `jishi_set_budget(0)`。
const DEFAULT_BUDGET: u32 = 20_000_000;

thread_local! {
    static BUDGET: RefCell<u32> = const { RefCell::new(DEFAULT_BUDGET) };
}

fn budget() -> Option<u64> {
    BUDGET.with(|b| match *b.borrow() {
        0 => None,                      // 0 = 显式「不限」
        n => Some(n as u64),
    })
}

// ---------------------------------------------------------------------------
// 内存：让 JS 能把字符串放进 wasm 的线性内存
// ---------------------------------------------------------------------------

/// 申请一块 `len` 字节的内存，返回指针。JS 侧写完后要 `jishi_free`。
#[no_mangle]
pub extern "C" fn jishi_alloc(len: usize) -> *mut u8 {
    let mut v = Vec::<u8>::with_capacity(len);
    let p = v.as_mut_ptr();
    std::mem::forget(v);        // 交给 JS 管，等它调 free
    p
}

/// 释放 [`jishi_alloc`] 拿到的内存。**必须配着用**，否则每跑一次漏一块。
#[no_mangle]
pub extern "C" fn jishi_free(ptr: *mut u8, len: usize) {
    if ptr.is_null() {
        return;
    }
    unsafe { drop(Vec::from_raw_parts(ptr, len, len)) };
}

// ---------------------------------------------------------------------------
// 跑代码
// ---------------------------------------------------------------------------

/// 设步数上限（`0` = **不限**）。**一步 = 循环走完一圈**（回跳一次，见
/// `jishi_ffi::set_step_limit`）。默认 [`DEFAULT_BUDGET`]。
///
/// 为什么要给 JS 侧一个开关：playground 想让「跑飞了」尽快停，但也不想把
/// 正常的长循环掐掉 —— 上限值属于**站点策略**，不属于引擎。
///
/// ⚠️ 参数是 **`u32`**（不是 `u64`）：wasm 的 `u64` 在 JS 侧要写 `5000n` 这种
/// BigInt，而 40 亿圈早就远超任何合理的预算了 —— 拿类型换 shim 的清爽。
#[no_mangle]
pub extern "C" fn jishi_set_budget(steps: u32) {
    BUDGET.with(|b| *b.borrow_mut() = steps);
}

/// 当前的步数上限（`0` = 不限）—— 给 shim / 自检用。
#[no_mangle]
pub extern "C" fn jishi_get_budget() -> u32 {
    BUDGET.with(|b| *b.borrow())
}

/// 跑一段源码；结果（协议 JSON）放进结果缓冲。返回 `0`。
///
/// ⚠️ **不 panic 出来**：`opt-level="z"` + `panic="abort"` 下 panic 会变成
/// wasm trap，JS 侧接到的是一句英文的 `RuntimeError`（比如栈溢出那种无限递归）。
/// 那一层由 shim 兜成「程序跑飞了」的中文提示。
#[no_mangle]
pub extern "C" fn jishi_run(ptr: *const u8, len: usize) -> i32 {
    let src = read_src(ptr, len);
    let o = opts();
    set_out(sandbox::run_sandboxed_direct(&src, &o, "<playground>"));
    0
}

/// 求一个表达式的值；结果（协议 JSON）放进结果缓冲。返回 `0`。
#[no_mangle]
pub extern "C" fn jishi_eval(ptr: *const u8, len: usize) -> i32 {
    let src = read_src(ptr, len);
    let o = opts();
    match sandbox::eval_sandboxed_direct(&src, &o, "<playground>") {
        Ok(text) => set_out(text),
        // 值不是 JSON 能表达的（Python 侧这里冒 `TypeError`）—— 与 MCP 的
        // `eval_expr` 同口径给一句话，不假装它是成功。
        // ⚠️ 形状**与引擎给的失败一致**（`{ok:false, error:{code,title,…}}`）。
        Err(type_name) => set_out(format!(
            "{{\"ok\": false, \"error\": {{\"code\": null, \
             \"title\": \"这个值没法表示成 JSON\", \
             \"message\": \"类型「{type_name}」不能序列化成 JSON\", \
             \"line\": null, \"col\": null, \"hint\": null}}}}")),
    }
    0
}

fn read_src(ptr: *const u8, len: usize) -> String {
    if ptr.is_null() || len == 0 {
        return String::new();
    }
    let bytes = unsafe { std::slice::from_raw_parts(ptr, len) };
    String::from_utf8_lossy(bytes).to_string()
}

// ---------------------------------------------------------------------------
// 取结果
// ---------------------------------------------------------------------------

/// 结果缓冲的字节数。
#[no_mangle]
pub extern "C" fn jishi_out_len() -> usize {
    OUT.with(|o| o.borrow().len())
}

/// 把结果缓冲拷到 `dst`（JS 侧先用 [`jishi_alloc`] 备好 `jishi_out_len()` 字节）。
#[no_mangle]
pub extern "C" fn jishi_out_copy(dst: *mut u8) {
    if dst.is_null() {
        return;
    }
    OUT.with(|o| {
        let b = o.borrow();
        unsafe { std::ptr::copy_nonoverlapping(b.as_ptr(), dst, b.len()) };
    });
}

// ---------------------------------------------------------------------------
// M167：事件回调 + 浏览器宿主通道
// ---------------------------------------------------------------------------

/// 事件回调入口：**重新跑一遍源码，再调那个函数**。
///
/// > **一次事件 = 一次完整执行。**（设计见 `jishi_ffi::stdlib::browser` 的模块头）
///
/// 为什么是「重跑整个程序 + 再调函数」而不是「只调函数」：`jishi_run` 一返回，
/// 那次执行的 VM 就没了 —— 「只调函数」意味着要它能常驻，而 wasm 不能阻塞主线程
/// 等事件（会冻页面）。所以走**无状态重放**：重建全局状态（从 localStorage / DOM
/// 读回来），再执行处理函数。代价是每次事件都会重跑顶层代码，这是模式的形状。
///
/// ⚠️ **函数名要校验**：它会拼进源码（`{src}\n{name}()`），不校验就等于给了
/// 一个「从 JS 往基石源码里注入」的口子。只放行标识符字符（`is_alphanumeric`
/// 对中文为真，所以中文函数名照样能用）。
#[no_mangle]
pub extern "C" fn jishi_call(src_ptr: *const u8, src_len: usize,
                             fn_ptr: *const u8, fn_len: usize,
                             arg_ptr: *const u8, arg_len: usize) -> i32 {
    let src = read_src(src_ptr, src_len);
    let name = read_src(fn_ptr, fn_len);
    if name.is_empty()
        || !name.chars().all(|c| c.is_alphanumeric() || c == '_')
    {
        set_out(fail_json("事件处理函数的名字不合法", &name));
        return 1;
    }
    // 第五、六个参数是**给回调的实参**（`请求` 的响应文本、`绑定` 的事件信息）。
    // 空 = 不带参数。它会变成一个**字符串字面量**塞进调用里，所以必须转义。
    let arg = read_src(arg_ptr, arg_len);
    let 实参 = if arg.is_empty() {
        String::new()
    } else {
        format!("\"{}\"", arg.replace('\\', "\\\\").replace('"', "\\\"").replace('\n', "\\n").replace('\r', "\\r"))
    };
    let full = format!("{src}\n\n{name}({实参})\n");
    let o = opts();
    set_out(sandbox::run_sandboxed_direct(&full, &o, "<playground>"));
    0
}

/// JS 侧实现「浏览器宿主调用」时，把响应 JSON 写回来（M167）。
///
/// 通道形状：基石侧 `jishi_host_call(请求)` → shim 执行 → 调这个写回响应。
/// 一条通道而不是十几个 import，加新操作**不用动 ABI**。
#[no_mangle]
pub extern "C" fn jishi_host_respond(ptr: *const u8, len: usize) {
    if ptr.is_null() {
        return;
    }
    let bytes = unsafe { std::slice::from_raw_parts(ptr, len) };
    jishi_ffi::browser_respond(bytes);
}

/// 构造一条「失败」结果（形状与引擎给的失败一致）。
fn fail_json(title: &str, detail: &str) -> String {
    let esc = |s: &str| s.replace('\\', "\\\\").replace('"', "\\\"");
    format!(
        "{{\"ok\": false, \"error\": {{\"code\": null, \"title\": \"{}\", \
         \"message\": \"{}\", \"line\": null, \"col\": null, \"hint\": null}}}}",
        esc(title), esc(detail))
}

/// ABI 版本 —— shim 拿它自检「这份 wasm 是不是配套的」。
#[no_mangle]
pub extern "C" fn jishi_version() -> i32 {
    ABI_VERSION
}
