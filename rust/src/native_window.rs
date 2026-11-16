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
//! 1. **一个进程只能有一个 `EventLoop`**（Windows 上建第二个会直接 panic）——
//!    但**一个 `EventLoop` 可以建多个窗口**（tao 自带的 `multiwindow` 例子就是
//!    这么干的）。所以 M174 先只做单窗口，**M178 起是「单循环 + 多窗口」**：
//!    窗口用 `u64` 编号，事件里带着号走（见下）。
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
//!
//! ## M178：多窗口是怎么做的（三件事）
//!
//! ### 一、窗口按 `u64` 编号，每份状态各自一份
//!
//! M174 是 `static 全: Option<全局>`（进程里就一份）。现在拆成三张按号索引的表：
//!
//! | 表 | 装什么 | 为什么分开 |
//! |---|---|---|
//! | `窗们` | `Rc<Window>` + 活着 | 关窗口 = 从表里删掉（官方 `multiwindow` 例子就这么做） |
//! | `网们` | `wry::WebView` | **必须能单独 drop**：它握着 WebView2 的控制器，比窗口活得久会崩 |
//! | `待发们` / `就绪们` | 没就绪的宿主事件 / 已就绪的窗口号 | 「页面登记好没」是**每个页面**各自的事 |
//!
//! 事件循环只有一份（tao 的硬约束），所以它单独放 `循环槽`，`等` 的时候**借出来**
//! 跑 —— 借出来而不是拿着借用跑，是因为建窗口 / 建 WebView 中间可能同步回调
//! 进来，那时 `RefCell` 再被借一次就是 panic（M175 在 `全` 上踩过同款）。
//!
//! ### 二、页面 → 宿主不需要「自报窗口号」
//!
//! 路线图原本写的是「宿主往页面注入 `window.__jishiWinId`，shim 回传时带上，
//! ABI 3 → 4」。**实现时发现不用**：`with_ipc_handler` 本来就是**每个窗口一个**
//! 闭包，宿主当场就知道这条消息来自哪个 WebView —— 让页面自报一遍，等于把
//! 已知信息绕一圈，还多出「页面没报 / 报错了」两种失败模式。
//!
//! 所以 **ABI 仍是 3**，shim 与 wasm 一行没改。需要窗口号的地方（菜单 / 托盘 /
//! 单实例）都是**宿主侧**的来源，在那里带号就行。
//!
//! ### 三、`等` 等的是**所有**窗口（这是唯一一个不完美的地方）
//!
//! 一个进程只有一个事件循环，跑起来之后所有窗口一起活着。所以「等某个窗口关掉
//! 就返回」**做不到** —— 返回了循环就停，其它窗口会假死（画面还在、点了没反应），
//! 那比报错更糟。定成：
//!
//! ```text
//! 窗口.等(句柄)   →  跑到**最后一个**窗口关闭才返回（不管传的是哪个句柄）
//! 窗口.等()       →  同上
//! ```
//!
//! ⚠️ 用户得知道这一点，否则会写出「关了 A 之后后面的代码为什么不跑」的 bug
//! —— 文档（语言规格 / README / AI 卡）都写明了。


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
    /// 关掉某个窗口。`0` = 「主窗口」（托盘 / 单实例那种**进程级**来源用它，
    /// 派发时再解析成当前的主窗口 —— 它们触发时未必知道该找哪个窗口）。
    关窗(u64),
    /// 宿主事件要转给页面：`(窗口号, 事件名, 参数)`。`0` = 主窗口。
    页面(u64, String, String),
    /// 后台线程要往页面里注入一段 JS（M200：安装进度）。
    ///
    /// 🔴 **不能让后台线程直接调 `执行脚本`**：WebView2 的 `ExecuteScript`
    /// 只能在主线程调（wry 内部会断言）。安装是后台线程跑的，进度要回页面，
    /// 所以走这里：发个事件，循环回到主线程再执行。
    脚本(u64, String),
}

/// 窗口里装什么：一个网址，还是一段**直接喂进去的 HTML**。
///
/// 🔴 为什么要有第二种（M200 实测）：安装向导的页面是内嵌在 exe 里的一段 HTML。
///    用 `file://` 临时文件时，「从页面发 IPC」会让 wry 在
///    `Request::builder().uri(WebView2 给的 Source).unwrap()` 上**直接 panic**
///    （`http::Error(InvalidUri(InvalidFormat))`）—— 也就是说**按钮一点就崩**，
///    而窗口看起来完全正常。走 `NavigateToString`（`with_html`）时页面地址是
///    `about:blank`，那个 Source 解析得动。
///
///    附带的好处：不写临时文件 —— 「杀软拦临时目录」「临时目录只读」
///    这两个失败模式也一起没了。
pub enum 页面来源 {
    网址(String),
    网页(String),
}

/// 一个窗口的状态。**关窗口 = 从 `窗们` 里删掉** —— tao 官方的 `multiwindow`
/// 例子就是这么做的（`windows.remove(&id)`），那一步的 `Drop` 才真正销毁 HWND。
struct 窗件 {
    窗口: Rc<Window>,
}

/// 事件循环 + 「跑过没」。**一个进程一份**（tao 的硬约束）。
///
/// `循环` 是 `Option`：`等` 要把它**借出来**才能 `run_return`（要 `&mut`），
/// 借出去期间这里是 `None`。跑过之后不再放回去 —— 事件循环只能跑一次。
struct 循环件 {
    循环: Option<EventLoop<事件>>,
    跑过: bool,
}

/// M175：菜单栏 / 右键菜单 / 托盘必须**一直活着** —— 它们 drop 了，原生资源就没了。
///
/// 它们都不是 `Send`（原生菜单句柄、托盘图标都得在主线程用），所以只能放
/// thread_local；跨线程的那一半（菜单 id → 动作名）见 `动作表`。
///
/// M178：菜单栏 / 右键菜单改成**每窗口一份**；托盘仍然是**一个进程一个** ——
/// 系统托盘本来就没有「每个窗口一个图标」这个概念。
#[derive(Default)]
struct 桌面件 {
    菜单栏: HashMap<u64, muda::Menu>,
    右键: HashMap<u64, muda::Menu>,
    托盘: Option<tray_icon::TrayIcon>,
}

thread_local! {
    static 循环槽: RefCell<Option<循环件>> = RefCell::new(None);
    /// 开着的窗口。关掉就从这里删 —— `Rc` 的最后一个引用没了，窗口才真的销毁。
    static 窗们: RefCell<HashMap<u64, 窗件>> = RefCell::new(HashMap::new());
    /// WebView **必须能单独 drop**（它握着 WebView2 的控制器，比窗口活得久会在
    /// 退出时崩），而且 `等` 跑循环期间回调要能摸到它 —— 所以它既不塞进 `窗件`，
    /// 也不塞进事件循环。
    static 网们: RefCell<HashMap<u64, wry::WebView>> = RefCell::new(HashMap::new());
    /// 哪些窗口的页面已经 `窗口.当` 登记过处理函数了（**每个页面各自的事**）。
    static 就绪们: RefCell<std::collections::HashSet<u64>> =
        RefCell::new(std::collections::HashSet::new());
    /// 页面还没就绪时，事件按窗口排队。键 `0` = 「还不知道是哪个窗口」的
    /// （`单实例` 在开窗之前就到了），派发时按主窗口解析。
    static 待发们: RefCell<HashMap<u64, Vec<(String, String)>>> = RefCell::new(HashMap::new());
    static 桌面: RefCell<桌面件> = RefCell::new(桌面件::default());
}

/// 菜单项 id → `(窗口号, 动作名)`。
///
/// 🔴 **必须是跨线程的 static**：`muda` 的菜单事件在它自己的线程里回调，
/// 那里摸不到 thread_local（tray-icon 同理）。id 由我们生成（`jishi0`、`jishi1`…），
/// 不用「动作名当 id」——那样同一个动作在两个菜单里出现就会撞。
///
/// M178 加窗口号的原因：**菜单是每窗口一套的**，点了必须只送到那个窗口的页面。
/// 少了这一维就是「点 A 的菜单，B 的标题变了」—— 那种 bug 看起来像随机。
fn 动作表() -> &'static Mutex<HashMap<String, (u64, String)>> {
    static 表: OnceLock<Mutex<HashMap<String, (u64, String)>>> = OnceLock::new();
    表.get_or_init(|| Mutex::new(HashMap::new()))
}

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

fn 取代理() -> Result<EventLoopProxy<事件>, String> {
    代理槽()
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "还没有事件循环（先 窗口.打开）".to_string())
}

fn 登记动作(窗号: u64, 动作: &str) -> String {
    static 序: AtomicUsize = AtomicUsize::new(0);
    let id = format!("jishi{}", 序.fetch_add(1, Ordering::Relaxed));
    动作表()
        .lock()
        .unwrap()
        .insert(id.clone(), (窗号, 动作.to_string()));
    id
}

/// 新窗口的号。**从 1 开始、只增不减**。
///
/// 为什么不复用关掉的号：复用了「旧句柄」和「新窗口」就撞在一起 ——
/// 用户拿一个已经关掉的句柄去操作，会静默打到另一个窗口上。那种 bug
/// 比「句柄失效」难查得多。
fn 下一个号() -> u64 {
    static 序: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    序.fetch_add(1, Ordering::Relaxed)
}

/// 主窗口 = 还活着的、号最小的那个（= 最先开的）。
///
/// 托盘点击 / 单实例 / 文件对话框这些**进程级**的东西都归它 —— 系统里这些
/// 本来就没有「每窗口一份」的概念。
fn 主窗号() -> u64 {
    窗们.with(|c| c.borrow().keys().copied().min().unwrap_or(0))
}

/// `0` 解析成主窗口；其它号原样返回。
fn 解析号(窗号: u64) -> u64 {
    if 窗号 == 0 {
        主窗号()
    } else {
        窗号
    }
}

/// 这个构建里有没有原生窗口能力（`--no-default-features` 时整个模块不存在）。
pub const 可用: bool = true;

/// 还有开着的原生窗口吗（不看具体哪一个）。
pub fn 有窗口() -> bool {
    窗们.with(|c| !c.borrow().is_empty())
}

/// 这个号的原生窗口还开着吗。
pub fn 在运行(窗号: u64) -> bool {
    窗们.with(|c| c.borrow().contains_key(&窗号))
}

/// 借事件循环出来建窗口。
///
/// 为什么「借出来」而不是「拿着借用建」：建窗口 / 建 WebView 会走系统的窗口与
/// WebView2 / WKWebView 构造，中间**可能同步回调进来**。那时如果 `循环槽` 的
/// `RefCell` 正被借用，就是一个 `already borrowed` panic —— M175 在 `全` 上踩过
/// 同款（表现是「菜单点了没反应」）。借出来办事、办完放回，最坏情况只是
/// 「同时有两个开窗请求，第二个拿到一句报错」。
fn 借循环() -> Result<EventLoop<事件>, String> {
    循环槽.with(|c| {
        let mut 槽 = c.borrow_mut();
        if 槽.is_none() {
            // ⚠️ 必须带自定义事件类型：IPC 里要发 `事件::关窗`。
            //    tao 0.34 的 `build()` 不返回 `Result`（建不出来它会自己 panic）——
            //    它 panic 的唯一原因是「本线程已经有事件循环了」，这里保证只建一次。
            let 循环: EventLoop<事件> = EventLoopBuilder::with_user_event().build();
            let 代理 = 循环.create_proxy();
            *代理槽().lock().unwrap() = Some(代理.clone());
            // 菜单与托盘的点击事件在**它们自己的线程**里回调 —— 只能靠 proxy 发回循环。
            // 一个事件循环只装一次（handler 是全局的，装两次只会留最后一个）。
            装菜单事件(代理.clone());
            装托盘事件(代理.clone());
            *槽 = Some(循环件 { 循环: Some(循环), 跑过: false });
        }
        let 件 = 槽.as_mut().expect("上面刚保证过不是 None");
        if 件.跑过 {
            return Err(
                "窗口已经全关过了（事件循环一个进程只跑一次）—— 这一次运行里不能再开窗口"
                    .to_string(),
            );
        }
        件.循环
            .take()
            .ok_or_else(|| "事件循环正忙（上一次开窗还没收尾）".to_string())
    })
}

fn 还循环(循环: EventLoop<事件>) {
    循环槽.with(|c| {
        if let Some(件) = c.borrow_mut().as_mut() {
            件.循环 = Some(循环);
        }
    });
}

/// 建一个原生窗口并把它指向 `网址`（那个地址由调用方起的小服务提供）。
///
/// 返回**窗口号**（≥1，只增不减）：脚本侧拿它当「句柄」对外的身份，
/// 事件与菜单都按它路由。
///
/// ⚠️ 返回时窗口还没开始收事件 —— 要等 `等()` 跑事件循环。这跟应用模式
/// （浏览器进程一起来就响应）不同，但**用户代码的写法一样**：
/// `打开` → 干活 → `等`，中间那段通常只有毫秒级。
pub fn 打开(标题: &str, 宽: f64, 高: f64, 网址: &str) -> Result<u64, String> {
    打开页(标题, 宽, 高, 页面来源::网址(网址.to_string()))
}

/// 同上，但页面内容**直接给**（`with_html`，不落盘、不用网址）。
///
/// 📌 页面的 `document.URL` 会是 `about:blank` —— 页面里要读「自己是谁」的地方
/// 不能依赖 URL（安装向导不依赖）。
pub fn 打开HTML(标题: &str, 宽: f64, 高: f64, 网页: &str) -> Result<u64, String> {
    打开页(标题, 宽, 高, 页面来源::网页(网页.to_string()))
}

fn 打开页(标题: &str, 宽: f64, 高: f64, 页面: 页面来源) -> Result<u64, String> {
    let 窗号 = 下一个号();
    let 循环 = 借循环()?;
    let r = 建窗口(窗号, &循环, 标题, 宽, 高, &页面);
    // ⚠️ 建失败时循环还在，必须放回去 —— 否则后面再 打开 会拿到「正在忙」。
    还循环(循环);
    r.map(|_| 窗号)
}

fn 建窗口(
    窗号: u64,
    循环: &EventLoop<事件>,
    标题: &str,
    宽: f64,
    高: f64,
    页面: &页面来源,
) -> Result<(), String> {
    let 代理 = 取代理()?;

    let 窗口 = Rc::new(
        WindowBuilder::new()
            .with_title(标题)
            .with_inner_size(LogicalSize::new(宽, 高))
            .build(循环)
            .map_err(|e| format!("建窗口失败：{e}"))?,
    );

    // IPC 处理器：**在主线程被调用**（WebView2 的 WebMessageReceived 就在 UI 线程），
    // 所以里面可以放心走 thread_local。
    //
    // 🔴 M178：**这个闭包是每个窗口一个**，所以「这条消息来自哪个窗口」当场就知道
    //    —— 不需要让页面自报窗口号。路线图原来打算注入 `__jishiWinId` 再让 shim
    //    回传（ABI 3 → 4），实现时发现那是把已知信息绕一圈，ABI 因此**没动**。
    //
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
        // M178：页面里能查到「我是几号窗口」。路由**不**靠它（IPC 闭包已经知道），
        // 主要是给页面自己写日志 / 判重时用。
        .with_initialization_script(&format!("window.__jishiWinId = {窗号};"))
        .with_ipc_handler(move |请求: wry::http::Request<String>| {
            if let Some(ev) = 处理命令(窗号, 请求.body()) {
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
                    调试(&format!("窗口 {窗号} 拖进来 {} 个文件", 列.len()));
                    // 多个文件用**换行**分隔：路径里不可能有换行（三个平台都不允许），
                    // 而 wasm 那条通道只能传一个字符串（`jishi_call` 的实参是文本）。
                    let _ = 代理.send_event(事件::页面(窗号, "拖放".into(), 列.join("\n")));
                }
                true
            }
        });

    // 页面来源（见 `页面来源` 的说明：`with_html` 是给「内嵌页面」那条路用的）
    let 构建 = match 页面 {
        页面来源::网址(u) => 构建.with_url(u),
        页面来源::网页(h) => 构建.with_html(h),
    };

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

    // 窗口**打开时就登记**：`打开` 与 `等` 之间也可能要改标题。
    窗们.with(|c| {
        c.borrow_mut().insert(窗号, 窗件 { 窗口 });
    });
    网们.with(|c| {
        c.borrow_mut().insert(窗号, 网页);
    });
    // 调试：把 URL 打出来 —— 页面空白、脚本不执行时，第一件事就是拿它 curl 一下。
    // ⚠️ 「窗口指向」这四个字是**判据在用**的（M176 的「端口两次一样」那条要靠它
    //    把 URL 抠出来）。多窗口之后加了窗口号，但把这四个字留在原地。
    let 描述 = match 页面 {
        页面来源::网址(u) => u.as_str(),
        页面来源::网页(_) => "about:blank（内嵌页面）",
    };
    调试(&format!("窗口指向 {描述}（窗口 {窗号}）"));
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

/// 跑事件循环，**阻塞到所有窗口都关掉**。返回退出码（这一版恒 0）。
///
/// 🔴 **`等` 等的是「所有窗口」，不是你传的那个句柄** —— 一个进程只有一个
/// 事件循环，它一停，其它窗口就假死（画面还在、点了没反应），那比报错更糟。
/// 所以「窗A 关了但窗B 还开着」时这里**不返回**。文档三处都写明了这一点，
/// 因为用户最容易在这里写出「关了 A 之后后面的代码为什么不跑」。
pub fn 等() -> i32 {
    // 借出事件循环（要 `&mut` 才能 `run_return`）；借不到 = 没开过窗口 / 已经跑过。
    let Some(mut 循环) = 循环槽.with(|c| c.borrow_mut().as_mut().and_then(|件| 件.循环.take()))
    else {
        return 0;
    };
    // 从这一刻起就算「跑过」：事件循环只能跑一次（tao 的硬约束）。
    循环槽.with(|c| {
        if let Some(件) = c.borrow_mut().as_mut() {
            件.跑过 = true;
        }
    });

    循环.run_return(|事件, _, 控制| {
        *控制 = ControlFlow::Wait;
        match 事件 {
            Event::UserEvent(事件::关窗(窗号)) => {
                if 关掉(解析号(窗号)) && !有窗口() {
                    *控制 = ControlFlow::Exit;
                }
            }
            // 宿主事件（菜单/托盘/拖放/再次启动）→ 交给**对应窗口**的页面。
            // 走到这里说明**已经在主线程**了，thread_local 摸得到。
            Event::UserEvent(事件::页面(窗号, 名, 参)) => 派发(窗号, &名, &参),
            Event::UserEvent(事件::脚本(窗号, js)) => {
                let 号 = 解析号(窗号);
                if !执行脚本(号, &js) {
                    调试(&format!("后台脚本没送进页面（窗口 {号} 已经不在了）"));
                }
            }
            // 用户点了标题栏的 ×（或 Alt+F4）：只关这一个，别的继续开着。
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                window_id,
                ..
            } => {
                if let Some(窗号) = 号由tao(window_id) {
                    关掉(窗号);
                }
                if !有窗口() {
                    *控制 = ControlFlow::Exit;
                }
            }
            _ => {}
        }
    });

    // 收尾：webview 必须先 drop（它握着 WebView2 的控制器），再 drop 窗口。
    // 这里的顺序**不能反**，也不能靠 HashMap 的遍历顺序 —— 所以分两步清。
    //
    // ⚠️ M181 收口时怀疑过「在这儿 drop 会把进程带走」，改成漏掉 → **没用**
    // （该挂还是挂），真正的触发条件是「点完菜单立刻拆窗口」。所以这里保持原样。
    调试("收尾：扔页面");
    网们.with(|c| c.borrow_mut().clear());
    调试("收尾：扔窗口");
    窗们.with(|c| c.borrow_mut().clear());
    调试("收尾：扔队列");
    就绪们.with(|c| c.borrow_mut().clear());
    待发们.with(|c| c.borrow_mut().clear());
    调试("收尾：扔菜单与托盘");
    桌面.with(|d| {
        let mut b = d.borrow_mut();
        b.菜单栏.clear();
        b.右键.clear();
        b.托盘 = None;
    });
    调试("收尾完成");
    // 事件循环本身**不还回去**（跑过的 EventLoop 不能再用，tao 会 panic）。
    // `循环槽` 里留着 `跑过 = true`，之后再 `打开` 会拿到一句人话。
    0
}

/// 关掉一个窗口：先扔页面，再扔窗口（`Rc` 最后一个引用没了，HWND 才真的销毁）。
///
/// 返回「本来有没有这个窗口」。调用方靠它 + `有窗口()` 决定要不要退出循环。
fn 关掉(窗号: u64) -> bool {
    if 窗号 == 0 {
        return false;
    }
    // 页面里的排队事件跟着窗口一起扔（不然号和事件会留下来「等」新窗口 —— 但号
    // 是只增不减的，永远等不到，只是白占内存）。
    就绪们.with(|c| {
        c.borrow_mut().remove(&窗号);
    });
    待发们.with(|c| {
        c.borrow_mut().remove(&窗号);
    });
    let 有页 = 网们.with(|c| c.borrow_mut().remove(&窗号).is_some());
    let 有窗 = 窗们.with(|c| c.borrow_mut().remove(&窗号).is_some());
    // 🔴 **菜单必须最后扔**（M178 用崩溃换来的顺序）。
    //
    // `muda::Menu::drop` 会对每个挂过的 hwnd 做 `SetMenu(hwnd, NULL)` +
    // `DrawMenuBar(hwnd)`。我们这时正**在那扇窗的窗口过程里**（CloseRequested /
    // 关窗事件都在消息处理中途），窗口还活着 —— `DrawMenuBar` 会在一个正在被
    // 销毁的窗口上重画菜单，实测会把进程打成 `0xC0000409`（fail-fast，没有
    // panic 消息，日志停在「关掉窗口 N」）。
    // 窗口先销毁、再 drop 菜单就没事：那时 `SetMenu` 拿到的是失效 hwnd，
    // 系统直接返回 FALSE，什么也不做（`let _ =` 吞掉）。
    桌面.with(|d| {
        let mut b = d.borrow_mut();
        b.菜单栏.remove(&窗号);
        b.右键.remove(&窗号);
    });
    调试(&format!("关掉窗口 {窗号}"));
    有页 || 有窗
}

/// tao 的 `WindowId` → 我们的窗口号（找不到 = 不是我们的窗口）。
fn 号由tao(要找: tao::window::WindowId) -> Option<u64> {
    窗们.with(|c| {
        c.borrow()
            .iter()
            .find(|(_, 件)| 件.窗口.id() == 要找)
            .map(|(号, _)| *号)
    })
}

/// 从外面（脚本 / 页面）请求关窗。
///
/// 事件循环**还没跑**时也能请求 —— tao 的 `EventLoopProxy` 会把事件排住，
/// `等` 一进循环就收到并立刻关掉那个窗口。所以「打开 → 关闭 → 等」也是通的。
/// `窗号` 传 0 表示「主窗口」（脚本里 `窗口.关闭()` 省略句柄就走这条）。
pub fn 请求关闭(窗号: u64) -> bool {
    // 🔴 **不要在这里用 `在运行(窗号)` 判活**（M200 实测，代价是 115 秒）：
    //    它读的是 thread_local `窗们`，从**后台线程**调时看到的是「一个窗口都没有」
    //    ⇒ 恒为 false ⇒ 「装完自动关窗」永远不生效，窗口一直挂着，
    //    而安装早就成功了（日志里「完成」到「窗口关」差了近两分钟）。
    //    `关窗` 事件对「窗口已经不在了」是幂等的，多发一次无害。
    match 取代理() {
        Ok(代理) => 代理.send_event(事件::关窗(窗号)).is_ok(),
        Err(_) => false,
    }
}

/// 在那个窗口的页面里执行一段 JS。
///
/// 🔴 **只能在主线程调**（WebView2 的 `ExecuteScript` 就这么要求；wry 内部会检查）。
/// 主线程 = 事件循环回调里、IPC 处理器里、`等()` 之前。后台线程要回话请先
/// `EventLoopProxy::send_event` 回主线程（M200 的安装进度就是这么回来的）。
///
/// 返回 `false`：窗口不在（还没开 / 已经关了）。**不是错误** —— 调用方
/// （比如「装完关窗」那一刻）本来就可能赶上窗口已经没了。
pub fn 执行脚本(窗号: u64, js: &str) -> bool {
    网们.with(|m| {
        let m = m.borrow();
        let Some(w) = m.get(&窗号) else {
            return false;
        };
        w.evaluate_script(js).is_ok()
    })
}

/// 从**后台线程**往页面注入一段 JS（M200：安装进度）。
///
/// 与 [`执行脚本`] 的区别只有一条：这个可以从任何线程调。做法是把 `(窗口号, js)`
/// 发成事件，回主线程再执行 —— 见 `事件::脚本` 的说明。
///
/// 返回 `false`：事件循环已经停了（窗口全关了 / `等()` 已经返回）。
/// 那时再更新页面也没有意义，调用方**不该**把它当错误（装完关窗就是这个时点）。
pub fn 后台执行脚本(窗号: u64, js: &str) -> bool {
    match 取代理() {
        Ok(代理) => 代理
            .send_event(事件::脚本(窗号, js.to_string()))
            .is_ok(),
        Err(_) => false,
    }
}

/// 改某个窗口的标题（页面的 IPC 与脚本都走这里）。找不到那个窗口时返回 `false`。
///
/// 日志放在这里而不是调用方：页面发来的 IPC 和脚本里直接调**都要记**，
/// 而且记的是 `Window::title()` —— 问系统要回来的那个值（M178 多窗口后
/// 这条日志带窗口号，不然三个窗口的日志分不清谁是谁）。
pub fn 设标题(窗号: u64, 新: &str) -> bool {
    let 号 = 解析号(窗号);
    窗们.with(|c| match c.borrow().get(&号) {
        Some(件) => {
            件.窗口.set_title(新);
            调试(&format!(
                "窗口 {号} 设标题({新}) → 系统里现在是「{}」",
                件.窗口.title()
            ));
            true
        }
        None => false,
    })
}

// ---------------------------------------------------------------------------
// M185：窗口几何（三个平台都走 tao —— **零新依赖**）
// ---------------------------------------------------------------------------
//
// 📌 一律用**逻辑像素**：用户写 `设大小(800, 600)` 指的是「看起来 800×600」，
//    不是「800 个物理点」。tao 的 `LogicalSize` / `LogicalPosition` 会按这个
//    窗口的 `scale_factor` 自己换算，`读几何` 再除回去 —— 两边同一个口径，
//    才不会出现「设 800 读回 816」那种迷糊事。

/// 对某个窗口做一件事 —— 拿不到窗口就 `None`，调用方转成一句人话。
fn 对窗口<T>(窗号: u64, f: impl FnOnce(&Window) -> T) -> Option<T> {
    let 号 = 解析号(窗号);
    窗们.with(|c| c.borrow().get(&号).map(|件| f(&件.窗口)))
}

pub fn 设大小(窗号: u64, 宽: f64, 高: f64) -> bool {
    对窗口(窗号, |窗| 窗.set_inner_size(LogicalSize::new(宽, 高))).is_some()
}

pub fn 设位置(窗号: u64, x: f64, y: f64) -> bool {
    对窗口(窗号, |窗| {
        窗.set_outer_position(tao::dpi::LogicalPosition::new(x, y))
    })
    .is_some()
}

/// 居中到**当前显示器**的正中间。
///
/// 显示器给的是物理像素（`size` / `position`），窗口尺寸也取物理，最后除回
/// `scale_factor` 再 `set_outer_position` —— 混着用会让 Retina 上偏一半。
pub fn 居中(窗号: u64) -> bool {
    对窗口(窗号, |窗| {
        if let Some(监) = 窗.current_monitor() {
            let 屏 = 监.size();
            let 位 = 监.position();
            let 窗尺 = 窗.outer_size();
            let 比 = 窗.scale_factor();
            let x = 位.x as f64 + (屏.width as f64 - 窗尺.width as f64) / 2.0;
            let y = 位.y as f64 + (屏.height as f64 - 窗尺.height as f64) / 2.0;
            窗.set_outer_position(tao::dpi::LogicalPosition::new(x / 比, y / 比));
        }
    })
    .is_some()
}

pub fn 最大化(窗号: u64, 开: bool) -> bool {
    对窗口(窗号, |窗| 窗.set_maximized(开)).is_some()
}

pub fn 最小化(窗号: u64, 开: bool) -> bool {
    对窗口(窗号, |窗| 窗.set_minimized(开)).is_some()
}

pub fn 设全屏(窗号: u64, 开: bool) -> bool {
    对窗口(窗号, |窗| {
        窗.set_fullscreen(if 开 {
            Some(tao::window::Fullscreen::Borderless(None))
        } else {
            None
        })
    })
    .is_some()
}

pub fn 设置顶(窗号: u64, 开: bool) -> bool {
    对窗口(窗号, |窗| 窗.set_always_on_top(开)).is_some()
}

/// `读几何` 读回来的东西（**逻辑像素**，与 `设大小` / `设位置` 同口径）。
pub struct 几何 {
    pub x: f64,
    pub y: f64,
    pub 宽: f64,
    pub 高: f64,
    pub 最大化: bool,
    pub 全屏: bool,
}

pub fn 读几何(窗号: u64) -> Option<几何> {
    对窗口(窗号, |窗| {
        let 比 = 窗.scale_factor();
        // ⚠️ `outer_position` 在某些平台会返回 `Err`（比如 Wayland 不给绝对位置）
        // —— 那时候 x/y 给 0，而不是整个函数失败：尺寸和状态还是有用的。
        let 位 = 窗.outer_position().ok();
        // 报**客户区**尺寸（`inner_size`）：`设大小` 设的也是它，
        // 两边同一个口径。想要「带边框的外框」是另一个函数，这一版不给。
        let 内 = 窗.inner_size();
        几何 {
            x: 位.map(|p| p.x as f64 / 比).unwrap_or(0.0),
            y: 位.map(|p| p.y as f64 / 比).unwrap_or(0.0),
            宽: 内.width as f64 / 比,
            高: 内.height as f64 / 比,
            最大化: 窗.is_maximized(),
            全屏: 窗.fullscreen().is_some(),
        }
    })
}

/// 设窗口图标（`.png`）。解码用 `png` crate —— 它本来就是依赖（托盘图标在用）。
///
/// ⚠️ **只收 8 位 RGBA** 的 PNG：`Icon::from_rgba` 要的就是这个格式，
/// 别的位深/色彩类型要转换，这一版不做（宁可报一句清楚的话）。
pub fn 设图标(窗号: u64, 路径: &str) -> Result<bool, String> {
    let 数据 = std::fs::read(路径).map_err(|e| format!("读不到图标文件「{路径}」：{e}"))?;
    let mut 读 = png::Decoder::new(std::io::Cursor::new(&数据))
        .read_info()
        .map_err(|e| format!("「{路径}」不是能读的 PNG：{e}"))?;
    let mut 缓 = vec![0u8; 读.output_buffer_size()];
    let 信息 = 读
        .next_frame(&mut 缓)
        .map_err(|e| format!("「{路径}」解不开：{e}"))?;
    if 信息.color_type != png::ColorType::Rgba || 信息.bit_depth != png::BitDepth::Eight {
        return Err(format!(
            "「{路径}」的像素格式是 {:?}/{:?} —— 窗口图标只收 **8 位 RGBA** 的 PNG",
            信息.color_type, 信息.bit_depth
        ));
    }
    let 图 = tao::window::Icon::from_rgba(缓, 信息.width, 信息.height)
        .map_err(|e| format!("这个 PNG 当不了窗口图标：{e}"))?;
    Ok(对窗口(窗号, |窗| 窗.set_window_icon(Some(图))).is_some())
}

/// 解析页面发来的命令（JSON：`{"op":"标题","arg":"已改名"}`）。
///
/// `窗号` 是**发这条消息的那个窗口** —— IPC 处理器是每窗口一个，所以调用方
/// 当场就知道（见 `建窗口` 里的注释）。
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
/// 宿主插进来的 IPC 处理器（M200：**安装向导**）。
///
/// ## 为什么允许外部插入
///
/// 安装器要的界面协议（`{"动作":"安装","目录":…}`）和语言侧的
/// （`{"op":"标题","arg":…}`）完全不是一回事，但「窗口 + 事件循环 + WebView +
/// 目录选择框」这套东西绝对不该写第二遍 —— 那会是本项目的第四个第三方依赖
/// （wry/tao）第二次被接进来，两边迟早各自踩一遍平台坑。
///
/// 所以只开**一个洞**：设了处理器就先问它，返回 `true` = 已处理，语言侧那条路
/// 就不走了。setup 是**独立进程**（不加载 VM），语言侧永远是 `None` ——
/// 运行期代价是一次 `Mutex` 读。
static 外部处理器: std::sync::Mutex<Option<Box<dyn Fn(&str) -> bool + Send + 'static>>> =
    std::sync::Mutex::new(None);

/// 设置 / 清除外部 IPC 处理器（见 [`外部处理器`] 的说明）。
///
/// ⚠️ 处理器**在主线程**被调用（WebView 的 IPC 回调就在 UI 线程），所以里面
/// 可以放心调 `请求关闭` / `设标题` 这些。
pub fn 设外部处理器(f: Option<Box<dyn Fn(&str) -> bool + Send + 'static>>) {
    if let Ok(mut g) = 外部处理器.lock() {
        *g = f;
    }
}

fn 处理命令(窗号: u64, 正文: &str) -> Option<事件> {
    // M200：安装器插进来的处理器优先（语言侧没设，等于多读一次锁）
    if let Ok(g) = 外部处理器.lock() {
        if let Some(f) = g.as_ref() {
            if f(正文) {
                return None;
            }
        }
    }
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
            设标题(窗号, 参数);
            None
        }
        // 🔴 M178：页面里的 `窗口.关闭()` 关的是**它自己那个窗口**，不是整个程序。
        //    靠的就是这里的 `窗号`（来自哪个 WebView 的 IPC）。
        "关闭" => Some(事件::关窗(窗号)),
        // M175：页面右击（页面里装了监听，见 `设右键菜单`）→ 弹原生菜单。
        // 坐标是页面里的 client 坐标，对 Windows 来说就是窗口内的坐标。
        "右键菜单" => {
            if let Some((x, y)) = 参数.split_once(',') {
                if let (Ok(x), Ok(y)) = (x.trim().parse::<i32>(), y.trim().parse::<i32>()) {
                    弹右键菜单(窗号, x, y);
                }
            }
            None
        }
        // 页面说「我登记好处理函数了」—— 把这个窗口排队的宿主事件一次送过去。
        "宿主事件就绪" => {
            就绪们.with(|c| {
                c.borrow_mut().insert(窗号);
            });
            调试(&format!("窗口 {窗号} 的页面登记好处理函数了"));
            // 两条队列：这个窗口自己的事件 + 「当时还不知道是哪个窗口」的
            // （单实例在开窗之前就到了那种）。后者只补给主窗口，别的事件
            // 已经在 派发 里解析过了。
            let mut 积压: Vec<(String, String)> = 待发们.with(|c| {
                c.borrow_mut().remove(&窗号).unwrap_or_default()
            });
            if 窗号 == 主窗号() {
                let 主的: Vec<(String, String)> = 待发们.with(|c| {
                    c.borrow_mut().remove(&0).unwrap_or_default()
                });
                积压.extend(主的);
            }
            if !积压.is_empty() {
                调试(&format!("窗口 {窗号} 就绪，补发 {} 条压住的事件", 积压.len()));
            }
            for (名, 参) in 积压 {
                派发(窗号, &名, &参);
            }
            None
        }
        // 页面自报状态（不算「命令」，但走同一条通道）。
        // 判据靠它断言「窗口里那一半（页面 + wasm 引擎）真的跑起来了」——
        // 只验「窗口建出来了」是半个证据。
        // 「页面已加载」与「引擎就绪」分开，是为了区分「脚本没跑」和「引擎没起来」。
        "页面已加载" => {
            调试(&format!("窗口 {窗号} 的页面到了（脚本开始执行）"));
            None
        }
        "引擎就绪" => {
            调试(&format!("窗口 {窗号} 的页面里 wasm 引擎起来了"));
            None
        }
        "引擎失败" => {
            // 把页面报上来的原因也打出来 —— 只说「失败了」等于没说，而窗口里
            // 看不到控制台，这条日志往往是人唯一的线索。
            调试(&format!("窗口 {窗号} 的 wasm 引擎失败了：{参数}"));
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

/// 取某个窗口的句柄。`窗号` 传 0 = 主窗口。
fn 某窗口(窗号: u64) -> Result<Rc<Window>, String> {
    let 号 = 解析号(窗号);
    if 号 == 0 {
        return Err("现在没有原生窗口（先 窗口.打开）".to_string());
    }
    窗们.with(|c| {
        c.borrow()
            .get(&号)
            .map(|件| Rc::clone(&件.窗口))
            .ok_or_else(|| format!("窗口 {号} 已经关掉了"))
    })
}

/// 在页面里跑一小段 JS。
fn 执行(窗号: u64, js: &str) -> bool {
    let 号 = 解析号(窗号);
    网们.with(|c| match c.borrow().get(&号) {
        Some(w) => match w.evaluate_script(js) {
            Ok(()) => true,
            Err(e) => {
                调试(&format!("窗口 {号} 执行 JS 失败：{e}"));
                false
            }
        },
        None => false,
    })
}

/// 把宿主事件交给**某个窗口**的页面：`window.__jishiWin(事件名, 参数)`。
///
/// 为什么是「叫一个页面里的函数」而不是「直接调用户注册的回调」：
/// 页面里的基石代码跑在 wasm 里，宿主够不着它的函数表；而 shim 里
/// `窗口.当` 登记过「事件名 → 函数名」，由它去调（见 `wasm/jishi-wasm.js`）。
///
/// `窗号` 传 0 = 主窗口（托盘 / 单实例那种进程级来源）。**解析只在这里做一次**：
/// 关掉主窗口之后，后面来的事件会落到新主窗口身上，而不是消失。
fn 派发(窗号: u64, 事件名: &str, 参数: &str) {
    let 号 = 解析号(窗号);
    if 号 == 0 {
        调试(&format!("还没有窗口，先把「{事件名}」排队"));
        待发们.with(|c| {
            c.borrow_mut()
                .entry(0)
                .or_default()
                .push((事件名.to_string(), 参数.to_string()))
        });
        return;
    }
    let 已就绪 = 就绪们.with(|c| c.borrow().contains(&号));
    if !已就绪 {
        调试(&format!("窗口 {号} 的页面还没登记处理函数，先把「{事件名}」排队"));
        待发们.with(|c| {
            c.borrow_mut()
                .entry(号)
                .or_default()
                .push((事件名.to_string(), 参数.to_string()))
        });
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
    let 情况 = 网们.with(|c| match c.borrow().get(&号) {
        None => "窗口或页面已经没了".to_string(),
        Some(w) => match w.evaluate_script(&js) {
            Ok(()) => String::new(),
            Err(e) => format!("JS 报错：{e}"),
        },
    });
    if 情况.is_empty() {
        调试(&format!(
            "给窗口 {号} 派发 {事件名}（页面若没反应，多半是没写 窗口.当 或处理函数报错）"
        ));
    } else {
        调试(&format!("窗口 {号} 没接住事件 {事件名}：{情况}"));
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
fn 建菜单(窗号: u64, 定义: &str) -> Result<muda::Menu, String> {
    let 值 = crate::json::parse(定义).map_err(|e| format!("菜单定义不是合法 JSON：{e}"))?;
    let 项们 = 值
        .as_array()
        .ok_or("菜单定义要是一个列表，例如 [{\"标签\": \"文件\", \"子项\": [...]}]")?;
    建菜单项(窗号, 项们)
}

/// 从已经解析好的列表建菜单（托盘菜单要用 —— 它嵌在托盘选项里）。
///
/// `窗号` 会**登记进每个菜单动作**（见 `登记动作`）：点了才知道该送给哪个窗口。
/// 托盘那种进程级的传 0，派发时解析成主窗口。
fn 建菜单项(窗号: u64, 项们: &[crate::json::Json]) -> Result<muda::Menu, String> {
    let 菜单 = muda::Menu::new();
    for 项 in 项们 {
        if let Some(件) = 建项(窗号, 项)? {
            菜单.append(&*件)
                .map_err(|e| format!("菜单项加不进去：{e}"))?;
        }
    }
    Ok(菜单)
}

fn 建项(窗号: u64, 值: &crate::json::Json) -> Result<Option<Box<dyn muda::IsMenuItem>>, String> {
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
            if let Some(件) = 建项(窗号, 项)? {
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
    let id = 登记动作(窗号, &动作);
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
            // 🔴 M178：动作里带着**窗口号** —— 菜单是每窗口一套的，点了只能
            //    送到那个窗口的页面。少了这一维就是「点 A 的菜单，B 变标题」。
            Some((窗号, a)) => {
                调试(&format!("窗口 {窗号} 的菜单点了：{a}"));
                let _ = 代理.send_event(事件::页面(窗号, "菜单".into(), a));
            }
            // 托盘图标的菜单项也走这条路 —— 认不出来说明是别的来源，忽略
            None => 调试(&format!("菜单事件 id={id} 没登记过动作")),
        }
    }));
}

/// 给某个窗口挂菜单栏。三个平台三条路，都是 muda 的入口。
///
/// `窗号` 传 0 = 主窗口。每个窗口一套菜单（`桌面件.菜单栏` 按号存），
/// 点了也只送到那个窗口的页面（见 `动作表`）。
///
/// ⚠️ **macOS 上菜单栏归应用，不归窗口**：那里调的是 `init_for_nsapp`，
/// 再给第二个窗口设菜单就是**替换掉整个应用的菜单栏**。多窗口 + 每窗口不同菜单
/// 在 macOS 上做不到（系统就是这样），如实记在文档里。
pub fn 设菜单(窗号: u64, 定义: &str) -> Result<(), String> {
    // 🔴 **顺序有讲究：先解析定义，再报「没有窗口」**。
    // 菜单写错（少个「动作」、把列表写成字典）跟有没有窗口**无关**，而且更容易
    // 写错；先报「现在没有原生窗口」会把人引到错误的方向（M175 的判据专门钉住
    // 这条：没有窗口时也必须先看到定义本身的错）。
    let 号 = 解析号(窗号);
    let 菜单 = 建菜单(号, 定义)?;
    if 号 == 0 {
        return Err("现在没有原生窗口（先 窗口.打开）".to_string());
    }
    挂菜单栏(号, &菜单)?;
    桌面.with(|d| d.borrow_mut().菜单栏.insert(号, 菜单));
    调试(&format!("窗口 {号} 的菜单栏挂上了"));
    Ok(())
}

#[cfg(target_os = "windows")]
fn 挂菜单栏(窗号: u64, 菜单: &muda::Menu) -> Result<(), String> {
    use tao::platform::windows::WindowExtWindows;
    let w = 某窗口(窗号)?;
    // SAFETY: 窗口还活着（`Rc` 在我们手里），hwnd 有效。
    unsafe { 菜单.init_for_hwnd(w.hwnd()) }.map_err(|e| format!("挂菜单栏失败：{e}"))
}

#[cfg(target_os = "macos")]
fn 挂菜单栏(_窗号: u64, 菜单: &muda::Menu) -> Result<(), String> {
    // macOS 的菜单栏归**应用**（不在窗口上），所以是 init_for_nsapp。
    // ⇒ 多窗口下**最后一次设的生效**（系统的限制，不是我们的选择）。
    菜单.init_for_nsapp();
    Ok(())
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn 挂菜单栏(窗号: u64, 菜单: &muda::Menu) -> Result<(), String> {
    use tao::platform::unix::WindowExtUnix;
    let w = 某窗口(窗号)?;
    菜单.init_for_gtk_window(w.gtk_window(), w.default_vbox())
        .map_err(|e| format!("挂菜单栏失败：{e}"))
}

/// 设右键菜单（每窗口一份）：把菜单交给宿主，并让页面把右击事件让给我们。
pub fn 设右键菜单(窗号: u64, 定义: &str) -> Result<(), String> {
    // 同 `设菜单`：定义错先报，再说窗口的事。
    let 号 = 解析号(窗号);
    let 菜单 = 建菜单(号, 定义)?;
    if 号 == 0 {
        return Err("现在没有原生窗口（先 窗口.打开）".to_string());
    }
    桌面.with(|d| d.borrow_mut().右键.insert(号, 菜单));
    // 页面里装一个监听，右击时把坐标发回来 —— 这样「右键」这件事统一由
    // 宿主弹原生菜单，而不是页面里画一个 HTML 菜单（那在桌面程序里很出戏）。
    执行(
        号,
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

/// 在**那个窗口**的 `(x, y)` 处弹出它自己的右键菜单（坐标是页面里的 client 坐标）。
fn 弹右键菜单(窗号: u64, x: i32, y: i32) {
    let 号 = 解析号(窗号);
    桌面.with(|d| {
        let b = d.borrow();
        let Some(菜单) = b.右键.get(&号) else {
            调试(&format!("窗口 {号} 收到右键，但它没设过右键菜单"));
            return;
        };
        let Ok(w) = 某窗口(号) else { return };
        调试(&format!("窗口 {号} 弹右键菜单 @({x},{y})"));
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
            // ⚠️ 局部变量**不能叫 `窗`**：那会撞上本文件的 thread_local `static 窗们`
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
            // 托盘是**进程级**的（系统里没有「每窗口一个托盘图标」），
            // 所以事件送给主窗口 —— `0` 在 `派发` 里解析。
            let _ = 代理.send_event(事件::页面(0, "托盘".into(), "点击".into()));
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
        // 托盘菜单的动作也登记在「主窗口」名下（`0` 在派发时解析）。
        let 菜单 = 建菜单项(0, 项们)?;
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

// ---------------------------------------------------------------------------
// M185：托盘能不能「改」（0.3.1 只能建的时候给一次）
// ---------------------------------------------------------------------------

/// `托盘.设菜单(定义)` —— 换掉托盘的右键菜单。定义格式与 `窗口.设菜单` **完全一样**。
pub fn 托盘设菜单(定义: &str) -> Result<(), String> {
    let 值 = crate::json::parse(定义).map_err(|e| format!("托盘菜单不是合法 JSON：{e}"))?;
    let 项们 = 值
        .as_array()
        .ok_or("「托盘.设菜单」要一个列表（和 窗口.设菜单 的格式一样）")?;
    // 托盘菜单的动作也登记在「主窗口」名下（`0` 在派发时解析）—— 与 `设托盘` 一致。
    let 菜单 = 建菜单项(0, 项们)?;
    桌面.with(|d| {
        let b = d.borrow();
        let Some(托) = b.托盘.as_ref() else {
            return Err("还没有托盘 —— 先 窗口.设托盘({...})".to_string());
        };
        托.set_menu(Some(Box::new(菜单)));
        Ok(())
    })
}

/// `托盘.设提示(文)` —— 改鼠标悬停时那句话。
pub fn 托盘设提示(文: &str) -> Result<(), String> {
    桌面.with(|d| {
        let b = d.borrow();
        let Some(托) = b.托盘.as_ref() else {
            return Err("还没有托盘 —— 先 窗口.设托盘({...})".to_string());
        };
        托.set_tooltip(Some(文))
            .map_err(|e| format!("改托盘提示失败：{e}"))
    })
}

// ---------------------------------------------------------------------------
// M185：任务栏进度（Windows 的 `ITaskbarList3`）—— 手写虚表，零新依赖
// ---------------------------------------------------------------------------

/// 任务栏进度条。
///
/// ## 为什么又是手写 COM
///
/// 和 M177 那个 `IShellLinkW` 一个理由：**COM 的 ABI 就是「对象指针 + 虚表」**，
/// `extern "system"` 调虚表函数不需要任何 crate。代价是虚表下标写错会**静默调错
/// 函数**（不是崩），所以每个下标都注了名，顺序照 Windows SDK 的 `shobjidl.h`。
///
/// ## 两个必须做对的地方
///
/// 1. **`HrInit` 不调，后面全返回 `E_UNEXPECTED`**（虚表 3 = `ITaskbarList::HrInit`）；
/// 2. 比例 **0 = 不显示进度条**，不是「显示 0%」—— 任务栏上不该留一条空槽。
#[cfg(windows)]
mod 任务栏 {
    use std::cell::RefCell;
    use std::ffi::c_void;

    #[repr(C)]
    struct Guid {
        d1: u32,
        d2: u16,
        d3: u16,
        d4: [u8; 8],
    }

    /// `CLSID_TaskbarList`
    const CLSID: Guid = Guid {
        d1: 0x56FD_F344,
        d2: 0xFD6D,
        d3: 0x11D0,
        d4: [0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90],
    };
    /// `IID_ITaskbarList3`
    const IID: Guid = Guid {
        d1: 0xEA1A_FB91,
        d2: 0x9E28,
        d3: 0x4B86,
        d4: [0x90, 0xE9, 0x9E, 0x9F, 0x8A, 0x5E, 0xEA, 0x84],
    };

    // 虚表下标。IUnknown 占 0–2；ITaskbarList 是 3–7（HrInit/AddTab/DeleteTab/
    // ActivateTab/SetActiveAlt）；ITaskbarList2 是 8（MarkFullscreenWindow）；
    // **9 才是 ITaskbarList3 的第一个方法** —— 这里最容易数错。
    const 虚_初始化: usize = 3;
    const 虚_设值: usize = 9;
    const 虚_设态: usize = 10;

    /// `TBPF_NOPROGRESS` —— 收掉进度条
    const 态_无: i32 = 0;
    /// `TBPF_NORMAL` —— 绿条
    const 态_正常: i32 = 2;

    thread_local! {
        /// COM 对象只建一次。存 `usize` 而不是裸指针：接口指针不是 `Send`，
        /// 但真正的线程约束由 `thread_local` 保证（它只在这个线程可见）。
        static 对象: RefCell<Option<usize>> = RefCell::new(None);
    }

    #[link(name = "ole32")]
    extern "system" {
        fn CoInitializeEx(保留: *mut c_void, 模式: u32) -> i32;
        fn CoCreateInstance(
            clsid: *const Guid,
            外: *mut c_void,
            上下文: u32,
            iid: *const Guid,
            出: *mut *mut c_void,
        ) -> i32;
    }

    /// 取虚表里第 `n` 个函数指针。
    ///
    /// ⚠️ 形参不能叫 `对象` —— 会撞上上面那个 `static 对象`（同名遮蔽，
    /// 编译报「non-primitive cast」，查起来还挺费劲）。
    unsafe fn 虚函数(接口: *mut c_void, n: usize) -> *const usize {
        let 表 = *(接口 as *mut *const *const usize);
        *表.add(n)
    }

    fn 取() -> Option<usize> {
        对象.with(|c| {
            let mut b = c.borrow_mut();
            if b.is_none() {
                unsafe {
                    // 失败也无妨：wry / WebView2 多半已经在这个线程上初始化过 COM
                    // （那时返回 `RPC_E_CHANGED_MODE`，对象照样能用）。
                    CoInitializeEx(std::ptr::null_mut(), 2);
                    let mut p: *mut c_void = std::ptr::null_mut();
                    if CoCreateInstance(&CLSID, std::ptr::null_mut(), 1, &IID, &mut p) != 0
                        || p.is_null()
                    {
                        return None;
                    }
                    let 初始化: extern "system" fn(*mut c_void) -> i32 =
                        std::mem::transmute(虚函数(p, 虚_初始化));
                    if 初始化(p) < 0 {
                        return None;
                    }
                    *b = Some(p as usize);
                }
            }
            *b
        })
    }

    /// `比例` 是 0.0–1.0；≤ 0 表示**把进度条收掉**。
    pub fn 设(hwnd: isize, 比例: f64) -> bool {
        let Some(地址) = 取() else { return false };
        let p = 地址 as *mut c_void;
        let 比例 = 比例.clamp(0.0, 1.0);
        unsafe {
            let 置态: extern "system" fn(*mut c_void, isize, i32) -> i32 =
                std::mem::transmute(虚函数(p, 虚_设态));
            if 比例 <= 0.0 {
                return 置态(p, hwnd, 态_无) >= 0;
            }
            let 置值: extern "system" fn(*mut c_void, isize, u64, u64) -> i32 =
                std::mem::transmute(虚函数(p, 虚_设值));
            // 先设状态再设值：Windows 的文档就是这么要求的顺序（反了会看不到条）。
            置态(p, hwnd, 态_正常) >= 0 && 置值(p, hwnd, (比例 * 10000.0) as u64, 10000) >= 0
        }
    }
}

/// `窗口.进度(比例, 句柄?)` —— 把进度画在**任务栏图标**上。
#[cfg(windows)]
pub fn 进度(窗号: u64, 比例: f64) -> bool {
    use tao::platform::windows::WindowExtWindows;
    对窗口(窗号, |窗| 任务栏::设(窗.hwnd(), 比例)).unwrap_or(false)
}

// ---------------------------------------------------------------------------
// M185：气泡提示（Windows 的 `Shell_NotifyIconW`）—— 同样是手写的
// ---------------------------------------------------------------------------

/// 系统通知（托盘气泡）。
///
/// ## 实现方式与它的代价（如实写）
///
/// `tray-icon` **没有**气泡 API，所以这里直接调 `Shell_NotifyIconW`：借某个窗口的
/// HWND 挂一个**临时的**托盘图标、带 `NIF_INFO` 让 Shell 弹气泡，然后**立刻撤掉**
/// 图标 —— 气泡已经排进队列，但图标不必常驻托盘区。
///
/// ⚠️ **两个没自动验的点**（`tests/test_m185_desktop.py` 只能验「不报错」）：
/// 1. 气泡**真的弹出来**要看屏幕 —— 判据没法断言；
/// 2. 「立刻撤图标」在个别 Windows 版本上可能把气泡一起收掉。真机上如果发现
///    气泡不出现，把 `NIM_DELETE` 挪到下一次提示之前再删即可（结构体不变）。
///
/// ⚠️ `NOTIFYICONDATAW` 的字段顺序/类型必须和 SDK 一字不差：`cbSize` 对不上时
/// Shell 会拒绝（返回 0），我们报「弹不出来」而不是崩 —— 所以错了也看得见。
#[cfg(windows)]
mod 气泡 {
    #[repr(C)]
    struct 通知 {
        cb_size: u32,
        h_wnd: isize,
        u_id: u32,
        u_flags: u32,
        u_callback: u32,
        h_icon: isize,
        sz_tip: [u16; 128],
        dw_state: u32,
        dw_state_mask: u32,
        sz_info: [u16; 256],
        u_timeout: u32,
        sz_info_title: [u16; 64],
        dw_info_flags: u32,
        guid: [u8; 16],
        h_balloon: isize,
    }

    const NIM_ADD: u32 = 0x0000_0000;
    const NIM_DELETE: u32 = 0x0000_0002;
    const NIF_INFO: u32 = 0x0000_0010;
    /// `NIIF_INFO`：信息图标（还有 WARNING / ERROR / USER 可选）
    const NIIF_INFO: u32 = 0x0000_0001;

    #[link(name = "shell32")]
    extern "system" {
        fn Shell_NotifyIconW(消息: u32, 数据: *mut 通知) -> i32;
    }

    fn 填(目: &mut [u16], 文: &str) {
        let 宽: Vec<u16> = 文.encode_utf16().take(目.len() - 1).collect();
        目[..宽.len()].copy_from_slice(&宽);
        目[宽.len()] = 0;
    }

    pub fn 提示(hwnd: isize, 标题: &str, 内容: &str) -> bool {
        unsafe {
            let mut n: 通知 = std::mem::zeroed();
            n.cb_size = std::mem::size_of::<通知>() as u32;
            n.h_wnd = hwnd;
            // 固定 uID：哪怕 NIM_DELETE 失败，托盘里也最多只留一个我们的图标。
            n.u_id = 0x4A53_4801;
            n.u_flags = NIF_INFO;
            n.dw_info_flags = NIIF_INFO;
            n.u_timeout = 8000;
            填(&mut n.sz_info_title, 标题);
            填(&mut n.sz_info, 内容);
            if Shell_NotifyIconW(NIM_ADD, &mut n) == 0 {
                return false;
            }
            Shell_NotifyIconW(NIM_DELETE, &mut n);
        }
        true
    }
}

/// `窗口.提示(标题, 内容, 句柄?)` —— Windows 上走托盘气泡。
#[cfg(windows)]
pub fn 提示(窗号: u64, 标题: &str, 内容: &str) -> bool {
    use tao::platform::windows::WindowExtWindows;
    对窗口(窗号, |窗| 气泡::提示(窗.hwnd(), 标题, 内容)).unwrap_or(false)
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
                let _ = a.send_event(事件::页面(0, "再次启动".into(), 正文));
            }
            // 还没有窗口：先排队，等页面登记好再送（见 `待发`）
            // 还没有窗口：先排到「0 号队列」（= 还不知道是哪个窗口），
            // 主窗口页面就绪时会被冲刷出去（见 `处理命令` 的 `宿主事件就绪`）。
            None => 待发们.with(|c| {
                c.borrow_mut()
                    .entry(0)
                    .or_default()
                    .push(("再次启动".into(), 正文))
            }),
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
