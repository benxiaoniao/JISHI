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
//! | `jishi_call(源码, 函数名, 实参)` | 事件回调（M167）；常驻模式下**不重放** |
//! | `jishi_frame(ts)` | **常驻 VM 跑一帧**（M186，D110）；`0` 好 / `2` 出错 / `3` 没会话 |
//! | `jishi_version()` | 自检用：非 0 就说明这份 wasm 是我们的 |
//!
//! ⚠️ **一个实例只跑一件事**：结果放在 `thread_local` 的缓冲里，下一次调用覆盖。
//! playground 是「点一下跑一次」，够用；要并发就往 `Worker` 里塞实例。

use std::cell::RefCell;

use jishi_ffi::sandbox::{self, SandboxOpts};

const ABI_VERSION: i32 = 4;   // M167 jishi_call / jishi_host_respond；M174 jishi_native；M186 jishi_frame

thread_local! {
    /// 上一次调用的结果（**协议 JSON 文本**，与原生 `--json-result` 同形）。
    static OUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    /// **常驻 VM**（M186，D110）：`图形.运行` 之后 `jishi_run` 不再丢它，
    /// 每一帧从这儿取出来调一次 `每帧`，再放回去。
    ///
    /// ⚠️ 取出来的那一刻槽是空的 —— 所以 `jishi_frame` 里**任何 `?` 早退都
    /// 必须放回去**（或者明确不要了）。下面用「先取 → 算 → 放回」的直线写法，
    /// 中间不 `?`，就是为了不给自己留这个坑。
    static 常驻: RefCell<Option<jishi_ffi::VM>> = const { RefCell::new(None) };
    /// `(上一帧时间戳, 帧号)`。帧号从 0 开始，`-1` = 还没跑过第一帧。
    static 帧态: RefCell<(f64, i64)> = const { RefCell::new((0.0, -1)) };
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
    // 每跑一次 = 一次「重启」：丢掉上一轮的常驻 VM 与登记（D110 §二）。
    常驻.with(|c| {
        c.borrow_mut().take();
    });
    jishi_ffi::frame::清();
    jishi_ffi::frame::清画布();

    let (json, vm) = sandbox::run_sandboxed_keep(&src, &o, "<playground>");
    set_out(json);
    match vm {
        Some(mut vm) if jishi_ffi::frame::有() => {
            // 常驻模式（D110 §八）：默认阈值 1000 圈要 17 秒才热，游戏等不起。
            jishi_ffi::设jit阈值(&mut vm, 60);
            帧态.with(|s| *s.borrow_mut() = (0.0, -1));
            常驻.with(|c| *c.borrow_mut() = Some(vm));
            // 告诉 shim「可以开始出帧了」。放这儿（跑完之后）而不是 `图形.运行`
            // 里 —— 那是唯一能确定「该不该停靠」的时刻（D110 §四末）。
            jishi_ffi::通知页面宿主("开帧");
        }
        _ => {
            // 没常驻：可能是正常结束，也可能是跑挂了 ⇒ 登记一律清掉，
            // 免得下一次 run 误判「已经在跑了」。
            jishi_ffi::frame::清();
        }
    }
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

    // —— 常驻模式（M186）：**不重放**，直接在这个 VM 上处理（D110 §六）——
    //
    // 为什么不能走下面那条重放路：点一下「开始游戏」会重跑顶层 ⇒ 分数归零、
    // 球回出发点。那不是性能问题，是功能坏了。
    if jishi_ffi::frame::有() {
        if let Some(mut vm) = 常驻.with(|c| c.borrow_mut().take()) {
            let _事件 = jishi_ffi::frame::事件守卫::新();
            let o = opts();
            jishi_ffi::set_output_limit(&mut vm, Some(o.max_stdout));
            jishi_ffi::重置步数(&mut vm);
            let r = jishi_ffi::frame::调处理函数(&mut vm, &name, &arg);
            let 出 = jishi_ffi::取走输出(&mut vm);
            set_out(match r {
                Ok(()) => sandbox::成功json(&出),
                Err(e) => sandbox::错误json(&e),
            });
            常驻.with(|c| *c.borrow_mut() = Some(vm));
            return 0;
        }
    }

    // 到这里还没常驻：如果上一轮停靠过一个 VM（`图形.停` 之后），现在就丢掉，
    // 让它回到「一次事件 = 一次完整执行」那条路（D110 §二）。
    常驻.with(|c| {
        c.borrow_mut().take();
    });

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

/// **跑一帧**（M186）—— `图形.运行` 之后由 shim 的 `requestAnimationFrame` 调。
///
/// 返回码：
///
/// | 码 | 意思 | shim 该做什么 |
/// |---|---|---|
/// | `0` | 这一帧跑完了 | 结果缓冲里可能有输出（`打印`），读出来打到控制台 |
/// | `2` | 出错，**帧循环已停** | 把输出缓冲当**错误 JSON** 处理，停 rAF |
/// | `3` | 没有常驻会话 | 停 rAF（`图形.停` / 刷新 / 程序没进常驻） |
///
/// 🔴 **`dt` 必须 clamp 到 100ms**（D110 §五）：后台标签页 rAF 会停，切回来
/// 那一帧的时间差可能是几十秒；而碰撞是离散采样 —— 一帧跨 30 秒，球直接穿墙，
/// 而且**看不出是 dt 的错**（画面只是「球不见了」）。第一帧给 0。
#[no_mangle]
pub extern "C" fn jishi_frame(ts: f64) -> i32 {
    if !jishi_ffi::frame::有() {
        // 停了：把可能还停靠着的 VM 丢掉（D110 §二「`停` 释放」）。
        常驻.with(|c| {
            c.borrow_mut().take();
        });
        return 3;
    }
    let Some(mut vm) = 常驻.with(|c| c.borrow_mut().take()) else {
        return 3;
    };
    let (号, dt) = 帧态.with(|s| {
        let mut s = s.borrow_mut();
        let (上, 号) = *s;
        let mut dt = if 号 < 0 { 0.0 } else { ts - 上 };
        if !dt.is_finite() || dt < 0.0 {
            dt = 0.0;
        }
        if dt > 100.0 {
            dt = 100.0;
        }
        let 新号 = 号 + 1;
        *s = (ts, 新号);
        (新号, dt)
    });

    let o = opts();
    jishi_ffi::set_output_limit(&mut vm, Some(o.max_stdout));
    // 🔴 每帧清零（D110 §七）：不清的话一局游戏跑到某个总圈数就被当成死循环。
    jishi_ffi::重置步数(&mut vm);

    let r = jishi_ffi::frame::跑一帧(&mut vm, 号, dt);
    let 出 = jishi_ffi::取走输出(&mut vm);
    match r {
        Ok(()) => {
            常驻.with(|c| *c.borrow_mut() = Some(vm));
            // 每帧的 `打印` 照样交出去（shim 打到控制台）—— 但**攒在缓冲里不清**，
            // 60 行/秒会把内存吃光。
            set_out(出);
            0
        }
        Err(e) => {
            // 出错即停：不清掉登记的话，下一帧还会再跑一次、再报一次
            // （D110 §七「只报一次」）。
            jishi_ffi::frame::清();
            set_out(sandbox::错误json(&e));
            2
        }
    }
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

/// 宿主塞一个随机种子进来（M188）。
///
/// 🔴 **为什么非要有这个导出**：`wasm32-unknown-unknown` **没有时钟**，
/// `SystemTime::now()` 在那边是**直接 panic**（不是返回 0）——
/// 于是 `随机` 模块在浏览器 / 窗口里**一调就把引擎炸了**。
/// M188 写贪吃蛇时，第一颗食物还没放下就 `RuntimeError: unreachable`。
///
/// JS 那边有现成的 `Math.random()`，加载时喂一个过来最省事
/// （原生宿主不需要，那边有时钟）。
#[no_mangle]
pub extern "C" fn jishi_seed(v: f64) {
    jishi_ffi::stdlib::rand::设种子(v as u64);
}

/// 宿主告诉「时间」模块现在几点（Unix 毫秒）—— M188。
///
/// 🔴 同 `jishi_seed` 的理由：`wasm32-unknown-unknown` **没有时钟**，
/// `时间.时间戳()` / `时间.今天()` 在网页和窗口里会**直接 panic**
/// （报「程序跑飞了」，看不出跟时间有关）。
/// shim 在加载时塞一次、之后**每帧再塞一次**（`jishi_frame` 里），所以时间会走。
#[no_mangle]
pub extern "C" fn jishi_now(ms: f64) {
    jishi_ffi::stdlib::datetime::设现在(ms as u64);
}

/// 宿主告诉「时间」模块本地时区差几分钟（东八区 = 480）—— M188。
///
/// 与 `jishi_now` 配套：wasm 上没有本地时区 API，不喂这个的话
/// `时间.今天()` 只能按 UTC 算（东八区凌晨 0–8 点会差一天）。
#[no_mangle]
pub extern "C" fn jishi_tz(分钟: f64) {
    jishi_ffi::stdlib::datetime::设时区偏移(分钟 as i64);
}
