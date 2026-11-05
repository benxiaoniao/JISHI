//! ABI 3（M174）：**页面里的基石代码 → 宿主（窗口）**。
//!
//! ## 为什么需要它
//!
//! M173 的窗口是「浏览器应用模式」：窗口是浏览器进程的，我们改不了它的标题。
//! M174 换成原生窗口后，标题归我们管 —— 但**页面里跑的基石代码够不着宿主**：
//! wasm 沙箱里没有窗口、没有文件、没有进程。
//!
//! 所以在 wasm 里加一条**导入函数** `jishi_native`：
//!
//! ```text
//!   页面里的基石代码   窗口.设标题("新的")
//!        ↓ 编译成
//!   wasm 导入调用      jishi_native(操作指针, 长度, 参数指针, 长度)
//!        ↓ shim
//!   宿主               window.ipc.postMessage("标题\0新的")
//!        ↓ wry
//!   原生窗口           Window::set_title("新的")
//! ```
//!
//! ## 两条设计约束
//!
//! 1. **单向**：发出去就完了，不等返回值。
//!    基石的 wasm 执行是同步的，而 IPC 天生异步 —— 要「等宿主回话」就得把整个
//!    执行模型改成异步续体。而真正需要的命令（改标题、关窗）恰好都是单向的。
//!    MVP 不为了对称去改执行模型。
//! 2. **`操作\0参数` 而不是 JSON**：中文不用转义、解析就一行 `split_once`，
//!    而且**判据能逐字节断言**（JSON 要处理键序、空格、转义三样）。
//!
//! ## 非 wasm 平台
//!
//! 原生宿主跑的是字节码，不经过这条路 —— 那边 `窗口.设标题` 直接调
//! `native_window::设标题`。所以这里的 `发` 在非 wasm 上是死代码，返回 -1。

#[cfg(target_arch = "wasm32")]
mod 真 {
    // ⚠️ `wasm_import_module = "jishi"` 必须写：不写就成了 `env.jishi_native`，
    //    而 shim 里挂在 `jishi` 这个模块下（M167 踩过一次）。
    #[link(wasm_import_module = "jishi")]
    extern "C" {
        fn jishi_native(操作: *const u8, 操作长: usize, 参数: *const u8, 参数长: usize) -> i32;
    }

    /// 发一条命令给宿主。返回 1 = 宿主收下了。
    pub fn 发(操作: &str, 参数: &str) -> i32 {
        // SAFETY: 两个指针都来自仍然活着的 `&str`，长度也是它们自己的长度；
        // 调用期间不会移动（shim 同步读完）。空串时指针有效但长度为 0，合法。
        unsafe { jishi_native(操作.as_ptr(), 操作.len(), 参数.as_ptr(), 参数.len()) }
    }
}

#[cfg(not(target_arch = "wasm32"))]
mod 真 {
    pub fn 发(_操作: &str, _参数: &str) -> i32 {
        -1
    }
}

pub use 真::发;
