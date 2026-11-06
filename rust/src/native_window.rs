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
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};

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
/// 「关窗口」与「要转给页面的宿主事件」两类：IPC / 菜单 / 托盘 / 拖放的回调
/// **都不在事件循环的闭包里**（它们由各自的库在别的线程或别的回调里触发），
/// 拿不到 `control_flow`，也摸不到 thread_local。所以只能发个事件，
/// 让循环自己回到主线程上来处理。
pub enum 事件 {
    关窗,
    /// 宿主事件要转给页面：`(事件名, 参数)`。
    页面(String, String),
}

struct 全局 {
    循环: EventLoop<事件>,
    代理: EventLoopProxy<事件>,
    窗口: Rc<Window>,
    活着: bool,
    /// 事件循环只能跑一次（tao 的约束）。跑过之后再 `等` 就是错的。
    跑过: bool,
}

/// M175：菜单栏 / 右键菜单 / 托盘必须**一直活着** —— 它们 drop 了，原生资源就没了。
///
/// 它们都不是 `Send`（原生菜单句柄、托盘图标都得在主线程用），所以只能放
/// thread_local；跨线程的那一半（菜单 id → 动作名）见 `动作表`。
#[derive(Default)]
struct 桌面件 {
    菜单栏: Option<muda::Menu>,
    右键: Option<muda::Menu>,
    托盘: Option<tray_icon::TrayIcon>,
}

thread_local! {
    static 全: RefCell<Option<全局>> = RefCell::new(None);
    /// 🔴 **页面必须单独放**，不能塞在 `全局` 里。
    ///
    /// 原因（M175 踩的）：`等` 要把 `全局` **取出来**才能跑事件循环（`run_return`
    /// 要 `&mut EventLoop`，而回调里又要访问页面 —— 同一个 RefCell 会 `borrow` 冲突）。
    /// 取出来之后循环期间 `全` 是 `None`，于是「派发宿主事件给页面」就找不到页面，
    /// 表现成**菜单点了没反应**（日志还说是「页面已经没了」）。
    ///
    /// `Option` 是为了能在收尾时**显式先 drop**（webview 握着 WebView2 的控制器，
    /// 比窗口活得久会在退出时崩）。
    static 网: RefCell<Option<wry::WebView>> = RefCell::new(None);
    /// 给「改标题」用的窗口句柄（`Rc` 克隆，地址稳定）。
    static 窗: RefCell<Option<Rc<Window>>> = const { RefCell::new(None) };
    static 桌面: RefCell<桌面件> = RefCell::new(桌面件::default());
}

/// 菜单项 id → 用户给的「动作」名。
///
/// 🔴 **必须是跨线程的 static**：`muda` 的菜单事件在它自己的线程里回调，
/// 那里摸不到 thread_local（tray-icon 同理）。id 由我们生成（`jishi0`、`jishi1`…），
/// 不用「动作名当 id」——那样同一个动作在两个菜单里出现就会撞。
fn 动作表() -> &'static Mutex<HashMap<String, String>> {
    static 表: OnceLock<Mutex<HashMap<String, String>>> = OnceLock::new();
    表.get_or_init(|| Mutex::new(HashMap::new()))
}

/// 页面**还没有**能力接宿主事件之前，事件在这儿排队。
///
/// 为什么需要它：`窗口.单实例` 的正常用法是「开窗之前先问是不是第一个」——
/// 那时页面还没加载，更没有 `窗口.当` 注册的处理函数。第二个实例叫过来的参数
/// 不能就这么丢掉，得等页面登记好再送过去（见 `宿主事件就绪`）。
/// 事件循环的代理，**存成 static 而不是闭包里捕获**。
///
/// 🔴 踩过的坑（M175）：`窗口.单实例` 的正常用法是在 `窗口.打开` **之前**调用 ——
/// 那一刻还没有事件循环，闭包里捕获到的 `代理` 就是 `None`，而且**永远**是 None。
/// 于是「第二个实例来了」这条事件只能进排队，可那时页面早就就绪了，没人来冲刷它
/// ⇒ 表现成「第二个实例确实退出了，但第一个窗口毫无反应」。
fn 代理槽() -> &'static Mutex<Option<EventLoopProxy<事件>>> {
    static 槽: OnceLock<Mutex<Option<EventLoopProxy<事件>>>> = OnceLock::new();
    槽.get_or_init(|| Mutex::new(None))
}

fn 待发() -> &'static Mutex<Vec<(String, String)>> {
    static 队: OnceLock<Mutex<Vec<(String, String)>>> = OnceLock::new();
    队.get_or_init(|| Mutex::new(Vec::new()))
}

/// 页面里 `窗口.当` 至少登记过一个处理函数了吗？
///
/// 由页面主动上报（shim 在第一次登记后发一条 `宿主事件就绪`）—— 不能靠
/// 「页面加载完成」：`<script>` 完了之后还要 fetch wasm、跑一遍源码才登记得上。
static 页面就绪: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn 登记动作(动作: &str) -> String {
    static 序: AtomicUsize = AtomicUsize::new(0);
    let id = format!("jishi{}", 序.fetch_add(1, Ordering::Relaxed));
    动作表()
        .lock()
        .unwrap()
        .insert(id.clone(), 动作.to_string());
    id
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
    // 新页面还没登记处理函数：把「就绪」标志清掉（宿主事件会先排队，见 `待发`）。
    页面就绪.store(false, Ordering::Relaxed);
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
    *代理槽().lock().unwrap() = Some(代理.clone());

    let 窗口 = Rc::new(
        WindowBuilder::new()
            .with_title(标题)
            .with_inner_size(LogicalSize::new(宽, 高))
            .build(&循环)
            .map_err(|e| format!("建窗口失败：{e}"))?,
    );

    // IPC 处理器：**在主线程被调用**（WebView2 的 WebMessageReceived 就在 UI 线程），
    // 所以里面可以放心走 thread_local。
    // 🔴 M176 演练抓到的真问题：WebView2 的**用户数据目录默认落在 exe 旁边**
    //    （`<exe>.WebView2/`）。装到 `C://Program Files\…` 时那里不可写，
    //    窗口直接起不来 —— 用户看到的是「双击没反应」。wry 的注释原话就是
    //    「useful in Windows when a bundled application can't have the webview
    //    data inside Program Files」。所以显式指到用户目录下。
    let mut 网页上下文 = wry::WebContext::new(网页数据目录());

    let 代理2 = 代理.clone();
    // ⚠️ `with_web_context` 是**关联函数**（构造函数），不是链式方法 ——
    //    `WebViewBuilder::new().with_web_context(..)` 编不过（CI 会告诉你
    //    「this is an associated function, not a method」）。
    let 构建 = WebViewBuilder::with_web_context(&mut 网页上下文)
        .with_url(网址)
        .with_ipc_handler(move |请求: wry::http::Request<String>| {
            if let Some(ev) = 处理命令(请求.body()) {
                let _ = 代理2.send_event(ev);
            }
        })
        // M175：拖文件进窗口。wry 在三个平台都替我们接住了（Windows 的 WM_DROPFILES /
        // macOS 的拖放协议 / Linux 的 GTK），这里只把路径转给页面。
        //
        // 为什么只处理 `Drop`：`Enter`/`Over` 是拖动过程中的高频事件（鼠标每动一下
        // 就来一次），转给页面会把事件通道刷爆，而页面也做不了什么有用的事。
        .with_drag_drop_handler({
            let 代理 = 代理.clone();
            move |e: wry::DragDropEvent| {
                if let wry::DragDropEvent::Drop { paths, .. } = e {
                    let 列: Vec<String> =
                        paths.iter().map(|p| p.display().to_string()).collect();
                    调试(&format!("拖进来 {} 个文件", 列.len()));
                    // 多个文件用**换行**分隔：路径里不可能有换行（三个平台都不允许），
                    // 而 wasm 那条通道只能传一个字符串（`jishi_call` 的实参是文本）。
                    let _ = 代理.send_event(事件::页面("拖放".into(), 列.join("\n")));
                }
                true
            }
        });

    // 菜单与托盘的点击事件在**它们自己的线程**里回调 —— 只能靠 proxy 发回循环。
    // 这两句是全局的（一个进程只能装一次），重复调用会被覆盖成同一个闭包，无害。
    装菜单事件(代理.clone());
    装托盘事件(代理.clone());

    // ⚠️ **平台分叉在这儿**：Windows / macOS 用统一的 `build(&window)`；
    //    Linux 必须用 `build_gtk(容器)` —— wry 在 Linux 上把 webview 塞进一个
    //    GTK 容器里，`build()` 那个签名在 Linux 上根本不存在（**编译期**就挂，
    //    不是运行时）。照 `wry/examples/simple.rs` 的写法。
    //
    //    代价如实记：Linux 这条分支是照官方示例写的，CI 只能验「编得过」，
    //    **验不了「窗口真能开」**（runner 没有桌面）—— 见 CHANGELOG。
    #[cfg(any(target_os = "windows", target_os = "macos"))]
    let 网页 = 构建.build(&*窗口).map_err(|e| 建网页失败(e.to_string()))?;

    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    let 网页 = {
        use tao::platform::unix::WindowExtUnix;
        use wry::WebViewBuilderExtUnix;
        let 容器 = 窗口
            .default_vbox()
            .ok_or_else(|| "Linux 上窗口没有默认容器（GTK 没初始化？）".to_string())?
            .clone();
        构建.build_gtk(&容器).map_err(|e| 建网页失败(e.to_string()))?
    };

    // 窗口指针**打开时就登记**：`打开` 与 `等` 之间也可能要改标题。
    窗.with(|c| {
        let _ = c.borrow_mut().insert(Rc::clone(&窗口));
    });
    网.with(|c| {
        let _ = c.borrow_mut().insert(网页);
    });
    // 调试：把 URL 打出来 —— 页面空白、脚本不执行时，第一件事就是拿它 curl 一下。
    调试(&format!("窗口指向 {网址}"));

    全.with(|s| {
        let _ = s.borrow_mut().insert(全局 {
            循环,
            代理,
            窗口,
            活着: true,
            跑过: false,
        });
    });
    Ok(())
}

/// WebView 的用户数据目录（缓存 / Cookie / localStorage）。
///
/// 放 `~/.jishi/webview`：与「应用模式」的 `~/.jishi/windows/…` 同一个家。
/// 拿不到家目录时返回 `None`（退回 wry 的默认行为 —— 至少不会因为这里失败而不开窗）。
fn 网页数据目录() -> Option<std::path::PathBuf> {
    let 基 = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .ok()?;
    if 基.trim().is_empty() {
        return None;
    }
    let d = std::path::Path::new(&基).join(".jishi").join("webview");
    std::fs::create_dir_all(&d).ok()?;
    调试(&format!("WebView 数据目录：{}", d.display()));
    Some(d)
}

/// 建 WebView 失败时给一句**能照着改**的话（三个平台的缺东西各不相同）。
fn 建网页失败(原因: String) -> String {
    format!(
        "建 WebView 失败：{原因}\n\
         （Windows 要 WebView2 运行时：Win11 自带、Win10 装过 Edge 就有；\
         Linux 要 libwebkit2gtk-4.1；macOS 自带 WKWebView）"
    )
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
            // 宿主事件（菜单/托盘/拖放/再次启动）→ 交给页面。
            // 走到这里说明**已经在主线程**了，thread_local 摸得到。
            Event::UserEvent(事件::页面(名, 参)) => 派发(&名, &参),
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => *控制 = ControlFlow::Exit,
            _ => {}
        }
    });

    // 收尾：webview 必须先 drop（它握着 WebView2 的控制器），再 drop 窗口。
    网.with(|c| {
        let _ = c.borrow_mut().take();
    });
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
        // M175：页面右击（页面里装了监听，见 `设右键菜单`）→ 弹原生菜单。
        // 坐标是页面里的 client 坐标，对 Windows 来说就是窗口内的坐标。
        "右键菜单" => {
            if let Some((x, y)) = 参数.split_once(',') {
                if let (Ok(x), Ok(y)) = (x.trim().parse::<i32>(), y.trim().parse::<i32>()) {
                    弹右键菜单(x, y);
                }
            }
            None
        }
        // 页面说「我登记好处理函数了」—— 把排队的宿主事件一次送过去。
        "宿主事件就绪" => {
            页面就绪.store(true, Ordering::Relaxed);
            调试("页面登记好处理函数了");
            let 积压: Vec<(String, String)> = 待发().lock().unwrap().drain(..).collect();
            if !积压.is_empty() {
                调试(&format!("页面就绪，补发 {} 条压住的事件", 积压.len()));
            }
            for (名, 参) in 积压 {
                派发(&名, &参);
            }
            None
        }
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
            // 把页面报上来的原因也打出来 —— 只说「失败了」等于没说，而窗口里
            // 看不到控制台，这条日志往往是人唯一的线索。
            调试(&format!("页面里的 wasm 引擎失败了：{参数}"));
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

// ---------------------------------------------------------------------------
// M175：桌面集成 —— 菜单 / 托盘 / 右键菜单 / 拖放 / 单实例 / 文件对话框
// ---------------------------------------------------------------------------
//
// 这一节的所有入口都是**主线程**调用的（脚本在 `窗口.等` 之前调用它们），
// 所以 thread_local 随便用；真正的点击事件在**别的线程**回来，那半边只能靠
// `EventLoopProxy` 发事件、由循环回到主线程后再派发（见 `派发`）。

fn 当前窗口() -> Result<Rc<Window>, String> {
    窗.with(|c| {
        c.borrow()
            .clone()
            .ok_or_else(|| "现在没有原生窗口（先 窗口.打开）".to_string())
    })
}

/// 在页面里跑一小段 JS。
fn 执行(js: &str) -> bool {
    网.with(|c| match c.borrow().as_ref() {
        Some(w) => match w.evaluate_script(js) {
            Ok(()) => true,
            Err(e) => {
                调试(&format!("执行 JS 失败：{e}"));
                false
            }
        },
        None => false,
    })
}

/// 把宿主事件交给页面：`window.__jishiWin(事件名, 参数)`。
///
/// 为什么是「叫一个页面里的函数」而不是「直接调用户注册的回调」：
/// 页面里的基石代码跑在 wasm 里，宿主够不着它的函数表；而 shim 里
/// `窗口.当` 登记过「事件名 → 函数名」，由它去调（见 `wasm/jishi-wasm.js`）。
fn 派发(事件名: &str, 参数: &str) {
    if !页面就绪.load(Ordering::Relaxed) {
        调试(&format!("页面还没登记处理函数，先把「{事件名}」排队"));
        待发().lock().unwrap().push((事件名.to_string(), 参数.to_string()));
        return;
    }
    let js = format!(
        "window.__jishiWin && window.__jishiWin({}, {})",
        js文本(事件名),
        js文本(参数)
    );
    // 三种情况要分得开（页面里看不到控制台，这条日志往往是人唯一的线索）：
    //   ① 窗口/页面没了 ② JS 报错 ③ 页面没注册处理函数（`__jishiWin` 不存在，
    //   表达式短路成 `undefined`，**不报错**）。
    let 情况 = 网.with(|c| match c.borrow().as_ref() {
        None => "窗口或页面已经没了".to_string(),
        Some(w) => match w.evaluate_script(&js) {
            Ok(()) => String::new(),
            Err(e) => format!("JS 报错：{e}"),
        },
    });
    if 情况.is_empty() {
        调试(&format!(
            "派发 {事件名} 了（页面若没反应，多半是没写 窗口.当 或处理函数报错）"
        ));
    } else {
        调试(&format!("页面没接住事件 {事件名}：{情况}"));
    }
}

/// 把一个字符串写成 JS 单引号字面量。
///
/// ⚠️ 不能只做 `format!("'{s}'")`：参数里可能有引号（文件名带 `'`）、换行
/// （拖放多文件就是换行分隔的）、反斜杠（Windows 路径全是反斜杠）。
fn js文本(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('\'');
    for c in s.chars() {
        match c {
            '\'' => o.push_str("\\'"),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            // 防止参数里出现 `</script>` 之类的组合（这里不是 HTML，但省心）
            '<' => o.push_str("\\u003C"),
            '\u{2028}' | '\u{2029}' => o.push_str("\\n"),
            _ => o.push(c),
        }
    }
    o.push('\'');
    o
}

// -- 菜单 -------------------------------------------------------------------

/// 解析菜单定义（JSON）并建出原生菜单。
///
/// 定义是一串字典：
/// ```text
/// [{"标签": "文件", "子项": [
///     {"标签": "打开", "动作": "打开", "快捷键": "Ctrl+O"},
///     {"类型": "分隔"},
///     {"标签": "退出", "动作": "退出"}]}]
/// ```
fn 建菜单(定义: &str) -> Result<muda::Menu, String> {
    let 值 = crate::json::parse(定义).map_err(|e| format!("菜单定义不是合法 JSON：{e}"))?;
    let 项们 = 值
        .as_array()
        .ok_or("菜单定义要是一个列表，例如 [{\"标签\": \"文件\", \"子项\": [...]}]")?;
    建菜单项(项们)
}

/// 从已经解析好的列表建菜单（托盘菜单要用 —— 它嵌在托盘选项里）。
fn 建菜单项(项们: &[crate::json::Json]) -> Result<muda::Menu, String> {
    let 菜单 = muda::Menu::new();
    for 项 in 项们 {
        if let Some(件) = 建项(项)? {
            菜单.append(&*件)
                .map_err(|e| format!("菜单项加不进去：{e}"))?;
        }
    }
    Ok(菜单)
}

fn 建项(值: &crate::json::Json) -> Result<Option<Box<dyn muda::IsMenuItem>>, String> {
    let 取文 = |键: &str| -> String {
        值.get(键)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    };
    if 取文("类型") == "分隔" {
        return Ok(Some(Box::new(muda::PredefinedMenuItem::separator())));
    }
    let 标签 = 取文("标签");
    if 标签.is_empty() {
        return Err("菜单项缺「标签」".into());
    }
    // 有「子项」= 子菜单
    if let Some(子项) = 值.get("子项").and_then(|v| v.as_array()) {
        let 子 = muda::Submenu::new(&标签, true);
        for 项 in 子项 {
            if let Some(件) = 建项(项)? {
                子.append(&*件)
                    .map_err(|e| format!("子菜单「{标签}」的项加不进去：{e}"))?;
            }
        }
        return Ok(Some(Box::new(子)));
    }
    let 动作 = 取文("动作");
    if 动作.is_empty() {
        return Err(format!(
            "菜单项「{标签}」缺「动作」—— 点了要通知页面哪个动作？（也可能你想写子菜单，那就给「子项」）"
        ));
    }
    // 快捷键解析不了时**不报错**，只是没有快捷键：不同平台的键名差别很大，
    // 为一个装饰性字段让整个菜单挂掉不值得。
    let 快捷键文 = 取文("快捷键");
    let 快捷键 = if 快捷键文.is_empty() {
        None
    } else {
        match 快捷键文.parse::<muda::accelerator::Accelerator>() {
            Ok(a) => Some(a),
            Err(e) => {
                调试(&format!("快捷键「{快捷键文}」解析不了（{e}），这一项就没有快捷键"));
                None
            }
        }
    };
    let id = 登记动作(&动作);
    if 取文("类型") == "勾选" {
        let 选中 = 值.get("选中").and_then(|v| v.as_bool()).unwrap_or(false);
        return Ok(Some(Box::new(muda::CheckMenuItem::with_id(
            id, 标签, true, 选中, 快捷键,
        ))));
    }
    Ok(Some(Box::new(muda::MenuItem::with_id(
        id, 标签, true, 快捷键,
    ))))
}

/// 装菜单点击的全局回调。菜单事件在 muda 自己的线程里回来，只能靠 proxy 发回循环。
fn 装菜单事件(代理: EventLoopProxy<事件>) {
    muda::MenuEvent::set_event_handler(Some(move |e: muda::MenuEvent| {
        let id = e.id().0.clone();
        let 动作 = 动作表().lock().unwrap().get(&id).cloned();
        match 动作 {
            Some(a) => {
                调试(&format!("菜单点了：{a}"));
                let _ = 代理.send_event(事件::页面("菜单".into(), a));
            }
            // 托盘图标的菜单项也走这条路 —— 认不出来说明是别的来源，忽略
            None => 调试(&format!("菜单事件 id={id} 没登记过动作")),
        }
    }));
}

/// 挂菜单栏。三个平台三条路，都是 muda 的入口。
pub fn 设菜单(定义: &str) -> Result<(), String> {
    let 菜单 = 建菜单(定义)?;
    挂菜单栏(&菜单)?;
    桌面.with(|d| d.borrow_mut().菜单栏 = Some(菜单));
    调试("菜单栏挂上了");
    Ok(())
}

#[cfg(target_os = "windows")]
fn 挂菜单栏(菜单: &muda::Menu) -> Result<(), String> {
    use tao::platform::windows::WindowExtWindows;
    let w = 当前窗口()?;
    // SAFETY: 窗口还活着（`Rc` 在我们手里），hwnd 有效。
    unsafe { 菜单.init_for_hwnd(w.hwnd()) }.map_err(|e| format!("挂菜单栏失败：{e}"))
}

#[cfg(target_os = "macos")]
fn 挂菜单栏(菜单: &muda::Menu) -> Result<(), String> {
    // macOS 的菜单栏归**应用**（不在窗口上），所以是 init_for_nsapp。
    菜单.init_for_nsapp();
    Ok(())
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn 挂菜单栏(菜单: &muda::Menu) -> Result<(), String> {
    use tao::platform::unix::WindowExtUnix;
    let w = 当前窗口()?;
    菜单.init_for_gtk_window(w.gtk_window(), w.default_vbox())
        .map_err(|e| format!("挂菜单栏失败：{e}"))
}

/// 设右键菜单：把菜单交给宿主，并让页面把右击事件让给我们。
pub fn 设右键菜单(定义: &str) -> Result<(), String> {
    let 菜单 = 建菜单(定义)?;
    桌面.with(|d| d.borrow_mut().右键 = Some(菜单));
    // 页面里装一个监听，右击时把坐标发回来 —— 这样「右键」这件事统一由
    // 宿主弹原生菜单，而不是页面里画一个 HTML 菜单（那在桌面程序里很出戏）。
    执行(
        r#"(function(){
  if (window.__jishiCtx) return;
  window.__jishiCtx = 1;
  document.addEventListener("contextmenu", function (e) {
    e.preventDefault();
    if (window.ipc && window.ipc.postMessage) {
      window.ipc.postMessage(JSON.stringify({op: "右键菜单", arg: (e.clientX|0) + "," + (e.clientY|0)}));
    }
  });
})();"#,
    );
    Ok(())
}

/// 在窗口的 `(x, y)` 处弹出右键菜单（坐标是页面里的 client 坐标）。
fn 弹右键菜单(x: i32, y: i32) {
    桌面.with(|d| {
        let b = d.borrow();
        let Some(菜单) = b.右键.as_ref() else {
            调试("收到右键，但没设过右键菜单");
            return;
        };
        let Ok(w) = 当前窗口() else { return };
        调试(&format!("弹右键菜单 @({x},{y})"));
        #[cfg(target_os = "windows")]
        {
            use muda::ContextMenu;
            use tao::platform::windows::WindowExtWindows;
            // SAFETY: 窗口与菜单都还活着（都在 thread_local 里）。
            unsafe {
                菜单.show_context_menu_for_hwnd(
                    w.hwnd(),
                    Some(muda::dpi::Position::Physical(
                        muda::dpi::PhysicalPosition::new(x, y),
                    )),
                );
            }
        }
        #[cfg(target_os = "macos")]
        {
            use muda::ContextMenu;
            use tao::platform::macos::WindowExtMacOS;
            unsafe {
                菜单.show_context_menu_for_nsview(
                    w.ns_view(),
                    Some(muda::dpi::Position::Physical(
                        muda::dpi::PhysicalPosition::new(x, y),
                    )),
                );
            }
        }
        #[cfg(not(any(target_os = "windows", target_os = "macos")))]
        {
            use muda::ContextMenu;
            use tao::platform::unix::WindowExtUnix;
            // ⚠️ tao 给的是 `&gtk::ApplicationWindow`，而 muda 要 `&gtk::Window`
            //    —— gtk-rs 里前者不是后者的 Deref 目标，必须显式 `upcast_ref`
            //    （CI 的 Linux job 就是这么报的 `expected &Window, found
            //    &ApplicationWindow`）。这也是我们要在 Linux 目标段里显式依赖
            //    `gtk 0.18` 的唯一原因。
            use gtk::prelude::Cast;
            // ⚠️ 局部变量**不能叫 `窗`**：那会撞上本文件的 thread_local `static 窗`
            //    —— Rust 在 `let` 的模式位置把它当成**常量模式**，于是报
            //    「expected &Window, found LocalKey<RefCell<...>>」（CI 抓到）。
            let gtk窗 = w.gtk_window().upcast_ref::<gtk::Window>();
            菜单.show_context_menu_for_gtk_window(
                gtk窗,
                Some(muda::dpi::Position::Physical(
                    muda::dpi::PhysicalPosition::new(x, y),
                )),
            );
        }
    });
}

// -- 托盘 -------------------------------------------------------------------

/// 内置托盘图标：32×32 的「窗口框」（一块圆角方块，中间挖空）。
///
/// 为什么要有内置的：不给图标就不给托盘，用户第一次试 `窗口.设托盘` 会因为
/// 「没准备 png」而失败 —— 那不该是首次体验。真要自己的图标再传 `图标`。
fn 内置图标() -> tray_icon::Icon {
    const 边: u32 = 32;
    let mut 像素 = vec![0u8; (边 * 边 * 4) as usize];
    for y in 0..边 {
        for x in 0..边 {
            let 边距 = 3;
            if x < 边距 || y < 边距 || x >= 边 - 边距 || y >= 边 - 边距 {
                continue;
            }
            let 心 = (边 / 2) as i32;
            let (dx, dy) = ((x as i32 - 心).abs(), (y as i32 - 心).abs());
            if dx <= 5 && dy <= 5 {
                continue; // 中间挖空 = 像个窗框
            }
            let i = ((y * 边 + x) * 4) as usize;
            像素[i] = 0x4C;
            像素[i + 1] = 0x6E;
            像素[i + 2] = 0xF5;
            像素[i + 3] = 0xFF;
        }
    }
    tray_icon::Icon::from_rgba(像素, 边, 边).expect("内置托盘图标是 32×32 RGBA，不会失败")
}

/// 读一张 PNG 当托盘图标。
///
/// 只收 PNG：ICO/BMP/JPEG 各有各的坑，而 PNG 是唯一「一个解码器就能覆盖、
/// 且带透明通道」的格式。`png` crate 只有几千行、纯 Rust。
fn 读图标(路径: &str) -> Result<tray_icon::Icon, String> {
    let 字节 = std::fs::read(路径).map_err(|e| format!("读不到托盘图标「{路径}」：{e}"))?;
    let mut 解码 = png::Decoder::new(&字节[..]);
    // 把调色板/透明色/16 位统一成 8 位的 RGB(A) —— 后面就只用管四种颜色类型。
    解码.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut 读 = 解码
        .read_info()
        .map_err(|e| format!("「{路径}」不是合法 PNG：{e}"))?;
    let mut 缓 = vec![0u8; 读.output_buffer_size()];
    let 信 = 读
        .next_frame(&mut 缓)
        .map_err(|e| format!("「{路径}」解不开：{e}"))?;
    let 原 = &缓[..信.buffer_size()];
    let mut rgba: Vec<u8> = Vec::with_capacity((信.width * 信.height * 4) as usize);
    match 信.color_type {
        png::ColorType::Rgba => rgba.extend_from_slice(原),
        png::ColorType::Rgb => {
            for c in 原.chunks_exact(3) {
                rgba.extend_from_slice(&[c[0], c[1], c[2], 255]);
            }
        }
        png::ColorType::GrayscaleAlpha => {
            for c in 原.chunks_exact(2) {
                rgba.extend_from_slice(&[c[0], c[0], c[0], c[1]]);
            }
        }
        png::ColorType::Grayscale => {
            for c in 原 {
                rgba.extend_from_slice(&[*c, *c, *c, 255]);
            }
        }
        other => return Err(format!("「{路径}」的颜色类型 {other:?} 支持不了，换成普通 PNG")),
    }
    tray_icon::Icon::from_rgba(rgba, 信.width, 信.height)
        .map_err(|e| format!("「{路径}」转不成托盘图标：{e}"))
}

/// 装托盘图标的点击回调。和菜单一样，事件在它自己的线程回来。
fn 装托盘事件(代理: EventLoopProxy<事件>) {
    tray_icon::TrayIconEvent::set_event_handler(Some(move |e: tray_icon::TrayIconEvent| {
        // 只报「左键抬起」：按下/移动/双击对用户没意义，而且会把通道刷爆。
        if let tray_icon::TrayIconEvent::Click {
            button: tray_icon::MouseButton::Left,
            button_state: tray_icon::MouseButtonState::Up,
            ..
        } = e
        {
            调试("托盘被点了");
            let _ = 代理.send_event(事件::页面("托盘".into(), "点击".into()));
        }
    }));
}

/// 设托盘。`选项` 是 JSON：`{"提示": "...", "图标": "x.png", "菜单": [...]}`。
pub fn 设托盘(选项: &str) -> Result<(), String> {
    let 值 = crate::json::parse(选项).map_err(|e| format!("托盘选项不是合法 JSON：{e}"))?;
    let 提示 = 值
        .get("提示")
        .and_then(|v| v.as_str())
        .unwrap_or("基石程序")
        .to_string();
    let 图 = match 值.get("图标").and_then(|v| v.as_str()) {
        Some(p) if !p.is_empty() => 读图标(p)?,
        _ => 内置图标(),
    };
    let mut 建 = tray_icon::TrayIconBuilder::new()
        .with_tooltip(&提示)
        .with_icon(图)
        // 左键点图标 = 报个「托盘」事件给页面（不是弹菜单）。右键才是菜单 ——
        // 这是 Windows / Linux 用户的肌肉记忆。
        .with_menu_on_left_click(false);
    if let Some(菜单值) = 值.get("菜单") {
        // 直接复用菜单定义那条路：托盘菜单和菜单栏是同一套格式。
        let 项们 = 菜单值
            .as_array()
            .ok_or("托盘选项里的「菜单」要是一个列表（和 窗口.设菜单 一样）")?;
        let 菜单 = 建菜单项(项们)?;
        建 = 建.with_menu(Box::new(菜单));
    }
    let 托盘 = 建.build().map_err(|e| {
        format!("托盘建不起来：{e}\n（Linux 要 libayatana-appindicator3；判据里可以跳过）")
    })?;
    桌面.with(|d| d.borrow_mut().托盘 = Some(托盘));
    调试("托盘挂上了");
    Ok(())
}

/// 收掉托盘图标。
pub fn 关托盘() -> bool {
    桌面.with(|d| {
        let 有 = d.borrow().托盘.is_some();
        d.borrow_mut().托盘 = None;
        有
    })
}

// -- 单实例 -----------------------------------------------------------------

/// `窗口.单实例("名字")`：真 = 我是第一个；假 = 已经有在跑的（参数已交给它）。
pub fn 单实例(名: &str) -> Result<bool, String> {

    // 命令行参数（跳过第 0 个 = 程序自己）。第二个实例把这些交给第一个，
    // 第一个会以「再次启动」事件的形式转给页面。
    let mut 参数: Vec<String> = std::env::args().skip(1).collect();
    // 第一个参数是「正在跑的那个脚本文件」—— 交给另一个实例没有意义
    // （两边跑的是同一个程序）。剩下的才是用户真正传进来的东西。
    if 参数
        .first()
        .map(|a| a.to_lowercase().ends_with(".jsh"))
        .unwrap_or(false)
    {
        参数.remove(0);
    }
    crate::single_instance::抢(名, &参数, move |正文| {
        调试(&format!("另一个实例来了，参数：{正文}"));
        // **现场读**代理：`单实例` 通常在 `打开` 之前调用，那时槽里还是空的。
        let 代理 = 代理槽().lock().unwrap().clone();
        match 代理 {
            Some(a) => {
                let _ = a.send_event(事件::页面("再次启动".into(), 正文));
            }
            // 还没有窗口：先排队，等页面登记好再送（见 `待发`）
            None => 待发().lock().unwrap().push(("再次启动".into(), 正文)),
        }
    })
}

// -- 文件对话框 -------------------------------------------------------------

fn 建对话框(值: &crate::json::Json) -> rfd::FileDialog {
    let mut 框 = rfd::FileDialog::new();
    if let Some(t) = 值.get("标题").and_then(|v| v.as_str()) {
        if !t.is_empty() {
            框 = 框.set_title(t);
        }
    }
    if let Some(d) = 值.get("目录").and_then(|v| v.as_str()) {
        if !d.is_empty() {
            框 = 框.set_directory(d);
        }
    }
    if let Some(n) = 值.get("默认名").and_then(|v| v.as_str()) {
        if !n.is_empty() {
            框 = 框.set_file_name(n);
        }
    }
    // 「类型」：`[["图片", "png,jpg"], ["所有文件", "*"]]`
    if let Some(组们) = 值.get("类型").and_then(|v| v.as_array()) {
        for 组 in 组们 {
            let Some(对) = 组.as_array() else { continue };
            let 名 = 对.first().and_then(|v| v.as_str()).unwrap_or("文件");
            let 扩 = 对.get(1).and_then(|v| v.as_str()).unwrap_or("*");
            let 列: Vec<&str> = 扩.split(',').map(|s| s.trim()).filter(|s| !s.is_empty()).collect();
            if !列.is_empty() {
                框 = 框.add_filter(名, &列);
            }
        }
    }
    框
}

/// `窗口.选文件(...)`：返回选中的路径（多选时是多个），取消返回空。
pub fn 选文件(选项: &str) -> Result<Vec<String>, String> {
    let 值 = crate::json::parse(选项).map_err(|e| format!("选项不是合法 JSON：{e}"))?;
    let 框 = 建对话框(&值);
    let 多选 = 值.get("多选").and_then(|v| v.as_bool()).unwrap_or(false);
    let 结果: Vec<String> = if 多选 {
        match 框.pick_files() {
            Some(列) => 列.into_iter().map(|p| p.display().to_string()).collect(),
            None => Vec::new(),
        }
    } else {
        match 框.pick_file() {
            Some(p) => vec![p.display().to_string()],
            None => Vec::new(),
        }
    };
    调试(&format!("选文件 → {} 个", 结果.len()));
    Ok(结果)
}

/// `窗口.存文件(...)`：返回用户定的路径，取消返回空。
pub fn 存文件(选项: &str) -> Result<Option<String>, String> {
    let 值 = crate::json::parse(选项).map_err(|e| format!("选项不是合法 JSON：{e}"))?;
    let 框 = 建对话框(&值);
    let 结果 = 框.save_file().map(|p| p.display().to_string());
    调试(&format!("存文件 → {结果:?}"));
    Ok(结果)
}
