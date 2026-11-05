//! M174：**原生窗口**（wry + 系统 WebView）—— `窗口.打开({原生: 真})` 走这里。
//!
//! ## 它和应用模式（M173）的关系：只换外壳
//!
//! ```text
//!   页面（html 构件） + wasm 引擎（`浏览器` 模块的事件模型）
//!          ↑ 一行不改
//!   外壳 v1：浏览器 `--app=`（M173）  →  外壳 v2：wry 原生窗口（M174，本文件）
//! ```
//!
//! 两代**共用同一个「只监听 127.0.0.1 的小服务」**（在 `stdlib/window.rs` 里），
//! 页面拿到的 URL 一模一样。差别只有外面那层：v2 的窗口标题、关闭行为都由
//! 我们说了算，而且能把页面里的命令（ABI 3）落到真窗口上。
//!
//! ## 四条硬约束（都实测过）
//!
//! 1. **一个进程只能有一个 `EventLoop`**（Windows 上建第二个会直接 panic）⇒
//!    M174 只支持**一个**原生窗口，第二个明确报错（多窗口是 M175 的事）。
//! 2. **`EventLoop` 必须在同一线程创建并 `run`**：本项目的解释器是单线程的，
//!    正好满足；这也意味着**不能**把窗口丢给子线程。
//! 3. ⚠️ **`ControlFlow::WaitUntil` 在 tao/Windows 上不按期唤醒**（实测：设了
//!    1.8 秒，程序一直不醒，靠 `timeout` 才杀掉；换 `Wait` / `Poll` 都正常）。
//!    所以这里**只用 `Wait`** —— 事件驱动、不烧 CPU、也不需要定时器。
//! 4. **窗口要用 `Rc<Window>` 放**：`tao::window::Window` 是个值类型，
//!    move 一下地址就变 ⇒ 裸指针会悬空。IPC 处理器要求 `Fn + 'static`，
//!    捕获不了 `&Window`；`Rc` 既稳定又能 clone 进闭包。（第一版用了裸指针，
//!    在「打开后、等之前就设标题」这条路上会踩空。）
//!
//! ## 为什么用 `run_return` 而不是 `run`
//!
//! `run` 的返回类型是 `!`（内部 `process::exit`）。那样 `窗口.等(窗)` 之后的
//! 清理代码就永远不跑了 —— 而用户的脚本通常还有事要做。`run_return` 会正常返回。

use std::cell::RefCell;
use std::rc::Rc;

use tao::{
    dpi::LogicalSize,
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoop, EventLoopBuilder, EventLoopProxy},
    platform::run_return::EventLoopExtRunReturn,
    window::{Window, WindowBuilder},
};
use wry::WebViewBuilder;

/// 事件循环的自定义事件。
///
/// 只有「关窗口」一件：IPC 是在 WebView 的回调里收到的，那里**拿不到**
/// `control_flow`（它由事件循环的闭包持有）。所以只能发个事件，让循环自己退。
pub enum 事件 {
    关窗,
}

struct 全局 {
    循环: EventLoop<事件>,
    代理: EventLoopProxy<事件>,
    窗口: Rc<Window>,
    /// `Option` 是为了能在收尾时**显式先 drop**（webview 握着 WebView2 的控制器，
    /// 比窗口活得久会在退出时崩）。
    网页: Option<wry::WebView>,
    活着: bool,
    /// 事件循环只能跑一次（tao 的约束）。跑过之后再 `等` 就是错的。
    跑过: bool,
}

thread_local! {
    static 全: RefCell<Option<全局>> = RefCell::new(None);
    /// 给「改标题」用的窗口句柄（`Rc` 克隆，地址稳定）。
    static 窗: RefCell<Option<Rc<Window>>> = const { RefCell::new(None) };
}

/// 这个构建里有没有原生窗口能力（`--no-default-features` 时整个模块不存在）。
pub const 可用: bool = true;

/// 这个进程里有没有开着的原生窗口。
pub fn 在运行() -> bool {
    全.with(|s| s.borrow().as_ref().is_some_and(|g| g.活着))
}

/// 建一个原生窗口并把它指向 `网址`（那个地址由调用方起的小服务提供）。
///
/// ⚠️ 返回时窗口还没开始收事件 —— 要等 `等()` 跑事件循环。这跟应用模式
/// （浏览器进程一起来就响应）不同，但**用户代码的写法一样**：
/// `打开` → 干活 → `等`，中间那段通常只有毫秒级。
pub fn 打开(标题: &str, 宽: f64, 高: f64, 网址: &str) -> Result<(), String> {
    // 已经开过一个：直接说清楚，别让用户对着一个不响应的窗口猜。
    全.with(|s| -> Result<(), String> {
        if let Some(g) = s.borrow().as_ref() {
            if g.跑过 {
                return Err("这个程序的原生窗口已经关掉了 —— 一次运行只能开一个原生窗口".into());
            }
            return Err(
                "一个程序只能有一个原生窗口（多窗口还没做）── 要开第二个请用「原生: 假」".into(),
            );
        }
        Ok(())
    })?;

    // ⚠️ 必须带自定义事件类型：IPC 里要发 `事件::关窗`。
    //    tao 0.34 的 `build()` 不返回 `Result`（建不出来它会自己 panic）——
    //    而它 panic 的唯一原因是「本线程已经有事件循环了」，`打开` 开头那道
    //    「一个程序只能有一个」已经挡住了。
    let 循环: EventLoop<事件> = EventLoopBuilder::with_user_event().build();
    let 代理 = 循环.create_proxy();

    let 窗口 = Rc::new(
        WindowBuilder::new()
            .with_title(标题)
            .with_inner_size(LogicalSize::new(宽, 高))
            .build(&循环)
            .map_err(|e| format!("建窗口失败：{e}"))?,
    );

    // IPC 处理器：**在主线程被调用**（WebView2 的 WebMessageReceived 就在 UI 线程），
    // 所以里面可以放心走 thread_local。
    let 代理2 = 代理.clone();
    let 网页 = WebViewBuilder::new()
        .with_url(网址)
        .with_ipc_handler(move |请求: wry::http::Request<String>| {
            if let Some(ev) = 处理命令(请求.body()) {
                let _ = 代理2.send_event(ev);
            }
        })
        .build(&*窗口)
        .map_err(|e| {
            format!(
                "建 WebView 失败：{e}\n\
                 （Windows 要 WebView2 运行时：Win11 自带、Win10 装过 Edge 就有；\
                 Linux 要 libwebkit2gtk-4.1）"
            )
        })?;

    // 窗口指针**打开时就登记**：`打开` 与 `等` 之间也可能要改标题。
    窗.with(|c| {
        let _ = c.borrow_mut().insert(Rc::clone(&窗口));
    });
    // 调试：把 URL 打出来 —— 页面空白、脚本不执行时，第一件事就是拿它 curl 一下。
    调试(&format!("窗口指向 {网址}"));

    全.with(|s| {
        let _ = s.borrow_mut().insert(全局 {
            循环,
            代理,
            窗口,
            网页: Some(网页),
            活着: true,
            跑过: false,
        });
    });
    Ok(())
}

/// 跑事件循环，**阻塞到窗口关掉**。返回退出码（这一版恒 0）。
pub fn 等() -> i32 {
    let Some(mut 全局) = 全.with(|s| s.borrow_mut().take()) else {
        return 0;
    };
    if 全局.跑过 {
        全.with(|s| {
            let _ = s.borrow_mut().insert(全局);
        });
        return 0;
    }
    全局.跑过 = true;

    全局.循环.run_return(|事件, _, 控制| {
        *控制 = ControlFlow::Wait;
        match 事件 {
            Event::UserEvent(事件::关窗) => *控制 = ControlFlow::Exit,
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => *控制 = ControlFlow::Exit,
            _ => {}
        }
    });

    // 收尾：webview 必须先 drop（它握着 WebView2 的控制器），再 drop 窗口。
    全局.网页 = None;
    窗.with(|c| {
        let _ = c.borrow_mut().take();
    });
    全局.活着 = false;
    全.with(|s| {
        let _ = s.borrow_mut().insert(全局);
    });
    0
}

/// 从外面（脚本）请求关窗：发事件让事件循环退。
///
/// 事件循环**还没跑**时也能请求 —— tao 的 `EventLoopProxy` 会把事件排住，
/// `等` 一进循环就收到并立刻退。所以「打开 → 关闭 → 等」也是通的。
pub fn 请求关闭() -> bool {
    全.with(|s| {
        let b = s.borrow();
        match b.as_ref() {
            Some(g) if g.活着 => g.代理.send_event(事件::关窗).is_ok(),
            _ => false,
        }
    })
}

/// 改窗口标题（页面的 IPC 与脚本都走这里）。没有窗口时返回 `false`。
///
/// 日志放在这里而不是调用方：页面发来的 IPC 和脚本里直接调**都要记**，
/// 而且记的是 `Window::title()` —— 问系统要回来的那个值。
pub fn 设标题(新: &str) -> bool {
    窗.with(|c| match c.borrow().as_ref() {
        Some(w) => {
            w.set_title(新);
            调试(&format!("设标题({新}) → 系统里现在是「{}」", w.title()));
            true
        }
        None => false,
    })
}

/// 解析页面发来的命令：`操作\0参数`（用 NUL 分隔，中文不用转义、也不用 JSON）。
///
/// 返回 `Some(事件)` 表示「要让事件循环退」。
/// 解析页面发来的命令（JSON：`{"op":"标题","arg":"已改名"}`）。
///
/// ## 为什么是 JSON 而不是 `操作\0参数`
///
/// 第一版用 NUL 当分隔符（「中文不用转义、解析就一行」），**实测被 WebView2
/// 截断**：页面 `postMessage("标题\0已改名")`，宿主收到的只有 `"标题"`（6 字节），
/// NUL 和它后面的一切都没了 —— IPC 走的那层把消息当 C 字符串处理。
///
/// 教训：**跨进程/跨 FFI 的消息里不要用 NUL 当分隔符**，它太容易被当终止符。
/// JSON 虽然要解析，但自带转义、可扩展（以后加字段不用改协议形状），
/// 而且本仓库已经有 `crate::json`（零依赖自研）。
fn 处理命令(正文: &str) -> Option<事件> {
    let j = match crate::json::parse(正文) {
        Ok(v) => v,
        Err(_) => {
            调试(&format!("IPC 有个不是 JSON 的消息，忽略：{正文:?}"));
            return None;
        }
    };
    let 操作 = j.get("op").and_then(|v| v.as_str()).unwrap_or("");
    let 参数 = j.get("arg").and_then(|v| v.as_str()).unwrap_or("");
    match 操作 {
        "标题" => {
            设标题(参数);
            None
        }
        "关闭" => Some(事件::关窗),
        // 页面自报状态（不算「命令」，但走同一条通道）。
        // 判据靠它断言「窗口里那一半（页面 + wasm 引擎）真的跑起来了」——
        // 只验「窗口建出来了」是半个证据。
        // 「页面已加载」与「引擎就绪」分开，是为了区分「脚本没跑」和「引擎没起来」。
        "页面已加载" => {
            调试("页面到了（脚本开始执行）");
            None
        }
        "引擎就绪" => {
            调试("页面里的 wasm 引擎起来了");
            None
        }
        "引擎失败" => {
            调试("页面里的 wasm 引擎失败了（看页面上的提示）");
            None
        }
        _ => {
            调试(&format!("不认识的命令：{操作}"));
            None
        }
    }
}

/// 调试输出开关：`JISHI_WINDOW_DEBUG=1` 时把窗口事件打到 stderr。
///
/// 为什么用环境变量而不是命令行开关：`窗口` 是标准库模块，拿不到 CLI 参数；
/// 而判据要能在**不改用户脚本**的前提下开这个通道。
fn 调试(行: &str) {
    if std::env::var("JISHI_WINDOW_DEBUG").map(|v| v == "1").unwrap_or(false) {
        eprintln!("[窗口] {行}");
    }
}
