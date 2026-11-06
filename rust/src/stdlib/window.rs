//! 标准库 `窗口`（Python 侧 `jishi/stdlib/窗口.py`，5 个函数）。
//!
//! 把程序**开成一个窗口**：起一个只监听 `127.0.0.1` 的小服务，用 Chromium 内核
//! 浏览器的应用模式（`--app=`）打开 —— 没有地址栏、任务栏里是独立图标。
//! 设计意图与三条边界见 Python 侧模块头（应用模式改不了标题 / 没有原生菜单 /
//! 不能拖文件；那三条正是 0.4.0 换 wry 要补的）。
//!
//! ## 这份实现的两处注意
//!
//! 1. **URL 要百分号编码**：中文文件名直接塞进请求行是**非法的**（HTTP 只认
//!    ASCII）。浏览器会自己编码，但别的东西（判据里的 urllib / 我们自己的服务端）
//!    会当场报错 —— 所以这里生成 URL 时就编好。
//! 2. **服务线程按目录复用**：同一个目录开两个窗口只起一个服务。窗口全关掉才停。
//!    停止靠一个 `AtomicBool` + 自己连一下自己（把 `accept` 从阻塞里捅出来）。

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use crate::BuiltinHost;

use super::{need_range, table, R};
use crate::{err, err_with_hint, Val};

/// 引擎素材的挂载点（页面里写 `/__jishi/jishi-wasm.js` 就对了）。
const 引擎前缀: &str = "/__jishi/";

struct 记录 {
    子: Option<Child>,
    目录: String,
    网址: String,
    /// M174：这个窗口是**原生窗口**（wry）还是浏览器应用模式。
    ///
    /// 两者的 `子` / `目录` 语义不同：原生的 `子` 是 `None`（窗口在本进程里），
    /// 而且它的生命周期跟着**事件循环**走，不跟着子进程走。
    原生: bool,
}

struct 服务 {
    端口: u16,
    停: Arc<AtomicBool>,
}

fn 表() -> &'static Mutex<Vec<Option<记录>>> {
    static 表: OnceLock<Mutex<Vec<Option<记录>>>> = OnceLock::new();
    表.get_or_init(|| Mutex::new(Vec::new()))
}

fn 服务表() -> &'static Mutex<HashMap<String, 服务>> {
    static 表: OnceLock<Mutex<HashMap<String, 服务>>> = OnceLock::new();
    表.get_or_init(|| Mutex::new(HashMap::new()))
}

// ---------------------------------------------------------------------------
// 引擎素材 / 浏览器
// ---------------------------------------------------------------------------

fn 找引擎() -> Option<PathBuf> {
    let mut 候选: Vec<PathBuf> = Vec::new();
    if let Ok(自己) = std::env::current_exe() {
        for 上 in 自己.ancestors().skip(1).take(5) {
            候选.push(上.join("site"));
            候选.push(上.join("wasm"));
        }
        if let Some(父) = 自己.parent() {
            候选.push(父.to_path_buf());
        }
    }
    for d in 候选 {
        if d.join("jishi-wasm.js").is_file() && d.join("jishi_wasm.wasm").is_file() {
            return Some(d);
        }
    }
    None
}

fn 找浏览器() -> Result<Vec<String>, crate::JishiError> {
    let mut 候选: Vec<String> = Vec::new();
    if cfg!(windows) {
        let 基 = [
            std::env::var("PROGRAMFILES").unwrap_or_else(|_| r"C:\Program Files".into()),
            std::env::var("PROGRAMFILES(X86)")
                .unwrap_or_else(|_| r"C:\Program Files (x86)".into()),
            std::env::var("LOCALAPPDATA").unwrap_or_default(),
        ];
        for b in 基.iter().filter(|s| !s.is_empty()) {
            候选.push(format!(r"{b}\Microsoft\Edge\Application\msedge.exe"));
            候选.push(format!(r"{b}\Google\Chrome\Application\chrome.exe"));
        }
    } else if cfg!(target_os = "macos") {
        for p in [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ] {
            候选.push(p.to_string());
        }
    } else {
        for 名 in ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"] {
            if let Some(p) = which(名) {
                候选.push(p);
            }
        }
    }
    for p in 候选 {
        if Path::new(&p).is_file() {
            return Ok(vec![p]);
        }
    }
    Err(err_with_hint(
        "值错误",
        "找不到能开应用窗口的浏览器（要 Edge / Chrome / Chromium 这类）",
        "装一个 Chromium 内核的浏览器，或者用「浏览器」选项指定它的可执行文件路径".into(),
    ))
}

fn which(名: &str) -> Option<String> {
    let 路径 = std::env::var("PATH").ok()?;
    for d in 路径.split(if cfg!(windows) { ';' } else { ':' }) {
        if d.is_empty() {
            continue;
        }
        let p = Path::new(d).join(名);
        if p.is_file() {
            return Some(p.display().to_string());
        }
    }
    None
}

fn 浏览器命令(选项: Option<&Val>) -> Result<Vec<String>, crate::JishiError> {
    let Some(v) = 选项.and_then(|d| 取(d, "浏览器").ok().flatten()) else {
        return 找浏览器();
    };
    match v {
        Val::None_ => 找浏览器(),
        Val::Str(s) => {
            let p = s.to_string();
            if p.trim().is_empty() {
                return 找浏览器();
            }
            if !Path::new(&p).is_file() {
                return Err(err("值错误", format!("「浏览器」指向的文件不存在：{p}")));
            }
            Ok(vec![p])
        }
        Val::List(l) => {
            let 项们 = l.borrow().clone();
            if 项们.is_empty() {
                return Err(err("值错误", "「浏览器」给了个空列表"));
            }
            let 命令: Vec<String> = 项们.iter().map(super::as_text).collect();
            if 命令[0].trim().is_empty() {
                return Err(err("值错误", "「浏览器」的第一项不能是空的（那应该是可执行文件或命令）"));
            }
            Ok(命令)
        }
        other => Err(err(
            "类型错误",
            format!("「浏览器」要传文本或列表，给的是{}", crate::type_label(&other)),
        )),
    }
}

// ---------------------------------------------------------------------------
// 取值
// ---------------------------------------------------------------------------

fn 取(d: &Val, key: &str) -> Result<Option<Val>, crate::JishiError> {
    match d {
        Val::Dict(m) => Ok(m
            .borrow()
            .iter()
            .find(|(k, _)| matches!(k, Val::Str(s) if s.as_ref() == key))
            .map(|(_, v)| v.clone())),
        other => Err(err(
            "类型错误",
            format!("要传字典，但给的是{}", crate::type_label(other)),
        )),
    }
}

fn 取文(d: &Val, key: &str, 默认: &str) -> Result<String, crate::JishiError> {
    match 取(d, key)? {
        None | Some(Val::None_) => Ok(默认.to_string()),
        Some(Val::Str(s)) => Ok(s.to_string()),
        Some(other) => Err(err(
            "值错误",
            format!("「{key}」要传文本，给的是{}", crate::type_label(&other)),
        )),
    }
}

fn 取数(d: &Val, key: &str, 默认: i128) -> Result<i128, crate::JishiError> {
    match 取(d, key)? {
        None | Some(Val::None_) => Ok(默认),
        Some(Val::Int(i)) => Ok(i),
        Some(other) => Err(err(
            "值错误",
            format!("「{key}」要传整数（像素），给的是{}", crate::type_label(&other)),
        )),
    }
}

/// 取一个真假选项。**只认真假**（不搞「非空即真」—— 那样 `"假"` 会被当成真）。
fn 取布(d: &Val, key: &str, 默认: bool) -> Result<bool, crate::JishiError> {
    match 取(d, key)? {
        None | Some(Val::None_) => Ok(默认),
        Some(Val::Bool(b)) => Ok(b),
        Some(other) => Err(err(
            "值错误",
            format!("「{key}」要传真或假，给的是{}", crate::type_label(&other)),
        )),
    }
}

// ---------------------------------------------------------------------------
// 小服务
// ---------------------------------------------------------------------------

/// URL 里的文件名编码：只留 `A-Za-z0-9-_.~`，其余按 UTF-8 逐字节 `%XX`。
fn 编码(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.as_bytes() {
        let c = *b as char;
        if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '~') {
            out.push(c);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn 解码(s: &str) -> String {
    let 字节 = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(字节.len());
    let mut i = 0;
    while i < 字节.len() {
        if 字节[i] == b'%' && i + 2 < 字节.len() {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(字节[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

fn 类型(路径: &Path) -> &'static str {
    match 路径
        .extension()
        .map(|e| e.to_string_lossy().to_ascii_lowercase())
        .as_deref()
    {
        Some("html") | Some("htm") => "text/html; charset=utf-8",
        Some("js") | Some("mjs") => "text/javascript; charset=utf-8",
        Some("wasm") => "application/wasm",
        Some("css") => "text/css; charset=utf-8",
        Some("json") => "application/json; charset=utf-8",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("ico") => "image/x-icon",
        Some("woff2") => "font/woff2",
        Some("txt") => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn 回应(流: &mut TcpStream, 码: u16, 类型: &str, 内容: &[u8]) {
    let 头 = format!(
        "HTTP/1.1 {码} {}\r\nContent-Type: {类型}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
        原因(码),
        内容.len()
    );
    let _ = 流.write_all(头.as_bytes());
    let _ = 流.write_all(内容);
    let _ = 流.flush();
}

fn 原因(码: u16) -> &'static str {
    match 码 {
        200 => "OK",
        204 => "No Content",
        404 => "Not Found",
        _ => "OK",
    }
}

fn 处理一次(流: &mut TcpStream, 目录: &Path, 引擎: Option<&Path>) {
    let mut 缓冲 = [0u8; 4096];
    let n = match 流.read(&mut 缓冲) {
        Ok(n) if n > 0 => n,
        _ => return,
    };
    let 请求 = String::from_utf8_lossy(&缓冲[..n]).to_string();
    let 行 = 请求.lines().next().unwrap_or("");
    let mut 段 = 行.split_whitespace();
    let _方法 = 段.next().unwrap_or("");
    let 路径 = 段.next().unwrap_or("/");
    let 纯 = 路径.split(['?', '#']).next().unwrap_or("/");

    let 相对 = 解码(纯);
    let 文件 = if let Some(rest) = 相对.strip_prefix(引擎前缀) {
        引擎.map(|d| d.join(rest.replace('/', std::path::MAIN_SEPARATOR_STR)))
    } else {
        let 名 = 相对.trim_start_matches('/');
        if 名.is_empty() {
            Some(目录.join("index.html"))
        } else {
            Some(目录.join(名.replace('/', std::path::MAIN_SEPARATOR_STR)))
        }
    };
    // 目录穿越：只认「落在允许的两个根里面」的路径
    let Some(文件) = 文件.filter(|p| {
        let 根 = if 相对.starts_with(引擎前缀) { 引擎 } else { Some(目录) };
        根.map(|r| 规范化(p).starts_with(规范化(r))).unwrap_or(false)
    }) else {
        回应(流, 404, "text/plain; charset=utf-8", "找不到".as_bytes());
        return;
    };
    if 文件.ends_with("favicon.ico") && !文件.is_file() {
        回应(流, 204, "text/plain", b"");
        return;
    }
    match std::fs::read(&文件) {
        Ok(数据) => 回应(流, 200, 类型(&文件), &数据),
        Err(_) => 回应(流, 404, "text/plain; charset=utf-8", "找不到".as_bytes()),
    }
}

fn 规范化(p: &Path) -> PathBuf {
    // 不要求存在：先按字面削掉 `.` 与 `..`，避免用 canonicalize 对不存在路径失败
    let mut 出 = PathBuf::new();
    for c in p.components() {
        match c {
            std::path::Component::ParentDir => {
                出.pop();
            }
            std::path::Component::CurDir => {}
            other => 出.push(other),
        }
    }
    出
}

/// 这个页面目录「应该」用哪个端口（39000–40999）。
///
/// 为什么固定：`浏览器.存本地` 落在浏览器的 localStorage 里，而 localStorage 按
/// **origin**（协议 + 主机 + 端口）隔离。端口每次随机 = 每次都是个新 origin =
/// 上次存的东西读不到。固定端口让「同一个程序」永远同一个 origin。
fn 想要端口(键: &str) -> u16 {
    let 印 = crate::sha256_hex(键.as_bytes());
    let 数 = u32::from_str_radix(&印[..4], 16).unwrap_or(0) % 2000;
    (39000 + 数) as u16
}

fn 起服务(目录: &str) -> Result<u16, crate::JishiError> {
    let 键 = 目录.replace('\\', "/").to_lowercase();
    if let Some(s) = 服务表().lock().unwrap().get(&键) {
        return Ok(s.端口);
    }
    // 🔴 **端口要可预测，不能每次随机**（M176 演练抓到的真缺陷）：
    //    页面的 origin 是 `http://127.0.0.1:<端口>`，而浏览器按 **origin** 隔离
    //    localStorage —— 端口一变，`浏览器.存本地` 写进去的东西下次就读不到了。
    //    症状是「计算器每次启动都回到 0」，而用户以为程序没记住他上次算到哪。
    //    做法：按**页面目录**散列出一个固定端口（39000–40999），占了再退回系统分配
    //    （那种情况下这一次的本地存储会是空的 —— 罕见，且不会破坏已有数据）。
    let 监听 = match TcpListener::bind(("127.0.0.1", 想要端口(&键))) {
        Ok(l) => l,
        Err(_) => TcpListener::bind(("127.0.0.1", 0))
            .map_err(|e| err("运行期错误", format!("起不了本地服务：{e}")))?,
    };
    let 端口 = 监听
        .local_addr()
        .map_err(|e| err("运行期错误", format!("拿不到端口：{e}")))?
        .port();
    let 停 = Arc::new(AtomicBool::new(false));
    let 停2 = 停.clone();
    let 目录 = 目录.to_string();
    std::thread::spawn(move || {
        let 根 = PathBuf::from(&目录);
        let 引擎 = 找引擎();
        for 连接 in 监听.incoming() {
            if 停2.load(Ordering::Relaxed) {
                break;
            }
            match 连接 {
                Ok(mut 流) => {
                    let _ = 流.set_read_timeout(Some(std::time::Duration::from_secs(5)));
                    处理一次(&mut 流, &根, 引擎.as_deref());
                }
                Err(_) => break,
            }
        }
    });
    服务表().lock().unwrap().insert(
        键,
        服务 {
            端口,
            停,
        },
    );
    Ok(端口)
}

fn 停服务(目录: &str) {
    let 键 = 目录.replace('\\', "/").to_lowercase();
    let 有 = 服务表().lock().unwrap().remove(&键);
    if let Some(s) = 有 {
        s.停.store(true, Ordering::Relaxed);
        // 捅一下 accept，让它醒过来看标志位
        let _ = TcpStream::connect(("127.0.0.1", s.端口));
    }
}

fn 配置目录(键: &str) -> PathBuf {
    // 浏览器给每个 --user-data-dir 一份独立存储 ⇒ `浏览器.存本地` 按程序分开存
    let 印 = crate::sha256_hex(键.as_bytes());
    let 基 = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_else(|_| ".".into());
    let d = Path::new(&基).join(".jishi").join("windows").join(&印[..16]);
    let _ = std::fs::create_dir_all(&d);
    d
}

// ---------------------------------------------------------------------------
// 五个函数
// ---------------------------------------------------------------------------

fn 打开(选项: &Val) -> Result<i128, crate::JishiError> {
    // 页面里（wasm）不能再开窗口：它自己就在一个窗口里，而且沙箱里没有
    // 进程、没有端口。要给它一句能照着改的话，不是「起服务失败」那种。
    #[cfg(target_arch = "wasm32")]
    {
        return Err(err_with_hint(
            "运行期错误",
            "页面里不能再开窗口（你已经在窗口里了）",
            "开窗要在脚本里 窗口.打开；页面里可以用 窗口.设标题 / 窗口.关闭()".into(),
        ));
    }
    #[cfg(not(target_arch = "wasm32"))]
    if !matches!(选项, Val::Dict(_)) {
        return Err(err_with_hint(
            "类型错误",
            "「窗口.打开」要一个字典",
            "例如 {\"标题\": \"计算器\", \"页面\": \"计算器.html\"}".into(),
        ));
    }
    let 页面 = 取文(选项, "页面", "")?;
    let 服务 = 取文(选项, "服务", "")?;
    let 宽 = 取数(选项, "宽", 900)?;
    let 高 = 取数(选项, "高", 640)?;

    if !页面.is_empty() && !服务.is_empty() {
        return Err(err(
            "值错误",
            "「页面」和「服务」只能给一个：页面我们自己起服务，服务是你已经有的地址",
        ));
    }
    let (网址, 目录) = if !服务.is_empty() {
        (服务.clone(), String::new())
    } else if !页面.is_empty() {
        let 全 = 绝对路径(Path::new(&页面));
        if !全.is_file() {
            return Err(err("值错误", format!("找不到页面：{页面}")));
        }
        let 目录 = 全
            .parent()
            .map(|p| p.display().to_string())
            .unwrap_or_default();
        // 🔴 起服务**之前**就把素材查清楚：找不到就别开窗。
        //
        // 素材缺失时窗口照样能打开，但页面里的 `import "/__jishi/jishi-wasm.js"`
        // 会 404 ⇒ **白窗口、一行报错都没有**。那种「看起来开了、其实什么都没跑」
        // 是最难查的故障。（M173 起就是坏的：发行包没带素材。）
        if 找引擎().is_none() {
            return Err(err_with_hint(
                "运行期错误",
                "找不到 wasm 引擎素材（jishi-wasm.js + jishi_wasm.wasm）——\
                 窗口里的基石代码跑不起来",
                "它们应当和 jishi 可执行文件在**同一目录**（发行包会自动带上）；\
                 在源码仓库里先跑 `python tools/build_wasm.py` 生成到 site/"
                    .into(),
            ));
        }
        let 端口 = 起服务(&目录)?;
        let 名 = 全
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default();
        (format!("http://127.0.0.1:{端口}/{}", 编码(&名)), 目录)
    } else {
        return Err(err("值错误", "「窗口.打开」要指定「页面」或「服务」"));
    };

    // ── M174：原生窗口 ──────────────────────────────────────────────────
    //
    // 默认就走原生：用户装的是完整发行版，`窗口.打开` 该给他一个**真窗口**
    // （标题、任务栏、关闭都由我们说了算）。`原生: 假` 退回 M173 的应用模式
    // —— 那条路在没装 WebView2 的机器上仍然可用，也是这一站之前的全部世界。
    if 取布(选项, "原生", true)? {
        #[cfg(feature = "native-window")]
        {
            let 标题 = {
                let t = 取文(选项, "标题", "")?;
                if t.is_empty() {
                    "基石程序".to_string()
                } else {
                    t
                }
            };
            crate::native_window::打开(&标题, 宽 as f64, 高 as f64, &网址)
                .map_err(|e| err("运行期错误", e))?;
            let mut 表 = 表().lock().unwrap();
            let 号 = 表.len() as i128 + 1;
            表.push(Some(记录 {
                子: None,
                目录,
                网址,
                原生: true,
            }));
            return Ok(号);
        }
        #[cfg(not(feature = "native-window"))]
        {
            return Err(err_with_hint(
                "运行期错误",
                "这个构建没有原生窗口能力（编译时没开 `native-window` 特性）",
                "用「原生: 假」走浏览器应用模式，或换完整发行版".into(),
            ));
        }
    }

    let mut 命令 = 浏览器命令(Some(选项))?;
    let 配置 = 配置目录(if 页面.is_empty() { &网址 } else { &页面 });
    命令.push(format!("--app={网址}"));
    命令.push(format!("--window-size={宽},{高}"));
    命令.push(format!("--user-data-dir={}", 配置.display()));
    命令.push("--no-first-run".into());
    命令.push("--no-default-browser-check".into());
    命令.push("--disable-features=TranslateUI".into());

    let mut cmd = Command::new(&命令[0]);
    cmd.args(&命令[1..])
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let 子 = cmd
        .spawn()
        .map_err(|e| err("运行期错误", format!("起不来窗口（{}）：{e}", 命令[0])))?;

    let mut 表 = 表().lock().unwrap();
    let 号 = 表.len() as i128 + 1;
    表.push(Some(记录 {
        子: Some(子),
        目录,
        网址,
        原生: false,
    }));
    Ok(号)
}

fn 绝对路径(p: &Path) -> PathBuf {
    if let Ok(v) = std::fs::canonicalize(p) {
        let s = v.display().to_string();
        if let Some(rest) = s.strip_prefix(r"\\?\") {
            return PathBuf::from(rest);
        }
        return v;
    }
    if p.is_absolute() {
        return p.to_path_buf();
    }
    std::env::current_dir()
        .map(|c| c.join(p))
        .unwrap_or_else(|_| p.to_path_buf())
}

fn 取记录(句柄: i128, 表: &mut Vec<Option<记录>>) -> Option<&mut 记录> {
    if 句柄 < 1 {
        return None;
    }
    表.get_mut(句柄 as usize - 1).and_then(|x| x.as_mut())
}

fn 关闭(句柄: i128) -> bool {
    let mut 表 = 表().lock().unwrap();
    let Some(记录) = 取记录(句柄, &mut 表) else {
        return false;
    };
    if 记录.原生 {
        // 原生窗口杀不了进程（它就在本进程里）—— 发个事件让事件循环退。
        // 真实收尾在 `等` 里做，这里**不许** `take` 记录：`等` 还要靠它
        // 判断「这是原生窗口，该跑事件循环」。
        drop(表);
        #[cfg(feature = "native-window")]
        {
            return crate::native_window::请求关闭();
        }
        #[cfg(not(feature = "native-window"))]
        {
            return false;
        }
    }
    if let Some(mut 子) = 记录.子.take() {
        let _ = 子.kill();
        let _ = 子.wait();
    }
    true
}

fn 收尾(句柄: i128) {
    let mut 表 = 表().lock().unwrap();
    let Some(槽) = (if 句柄 >= 1 {
        表.get_mut(句柄 as usize - 1)
    } else {
        None
    }) else {
        return;
    };
    let Some(记录) = 槽.take() else { return };
    let 还有 = 表
        .iter()
        .flatten()
        .any(|x| x.目录 == 记录.目录 && !x.目录.is_empty());
    drop(表);
    if !记录.目录.is_empty() && !还有 {
        停服务(&记录.目录);
    }
}

fn 等(句柄: i128) -> i128 {
    enum 等谁 {
        原生,
        子(Child),
        无,
    }
    // 把子进程 / 记录**取出来**再等：不能拿着锁阻塞（别的窗口还要用表）
    let 谁 = {
        let mut 表 = 表().lock().unwrap();
        match 取记录(句柄, &mut 表) {
            // 已经关掉了：等一个关掉的窗口 = 立刻返回
            None => 等谁::无,
            Some(记录) if 记录.原生 => 等谁::原生,
            Some(记录) => match 记录.子.take() {
                Some(c) => 等谁::子(c),
                None => 等谁::无,
            },
        }
    };
    let 码: i128 = match 谁 {
        等谁::原生 => {
            #[cfg(feature = "native-window")]
            {
                crate::native_window::等() as i128
            }
            #[cfg(not(feature = "native-window"))]
            {
                0
            }
        }
        等谁::子(mut c) => c.wait().map(|s| s.code().unwrap_or(0) as i128).unwrap_or(0),
        等谁::无 => 0,
    };
    收尾(句柄);
    码
}

// ---------------------------------------------------------------------------

/// 把基石的值转成 JSON 文本（菜单定义 / 托盘选项 / 对话框选项要用）。
///
/// 只支持标量 + 列表 + 字典 —— 这几样配置本来就长这样。别的类型（日期、表格、
/// 原生对象）明确报错，而不是悄悄变成 `null`（那会让菜单项莫名其妙消失）。
fn 转json(v: &Val, out: &mut String) -> Result<(), crate::JishiError> {
    match v {
        Val::None_ => out.push_str("null"),
        Val::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Val::Int(i) => out.push_str(&i.to_string()),
        Val::Float(f) => {
            if f.is_finite() {
                out.push_str(&f.to_string());
            } else {
                out.push_str("null");
            }
        }
        // ⚠️ `jsonw::esc` **自带前后引号** —— 再加一层就成了 `""甲的""`（M175 踩过）
        Val::Str(s) => out.push_str(&crate::jsonw::esc(s)),
        Val::List(列) => {
            out.push('[');
            for (i, 项) in 列.borrow().iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                转json(项, out)?;
            }
            out.push(']');
        }
        Val::Dict(d) => {
            out.push('{');
            for (i, (k, v)) in d.borrow().iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                match k {
                    Val::Str(s) => out.push_str(&crate::jsonw::esc(s)),
                    other => {
                        return Err(err(
                            "类型错误",
                            format!("字典的键要传文本，给的是{}", crate::type_label(other)),
                        ))
                    }
                }
                out.push(':');
                转json(v, out)?;
            }
            out.push('}');
        }
        other => {
            return Err(err(
                "类型错误",
                format!("{}没法转成菜单/选项（要文本、数字、真假、列表、字典）", crate::type_label(other)),
            ))
        }
    }
    Ok(())
}

fn 选项json(选项: &Val, 名: &str) -> Result<String, crate::JishiError> {
    if !matches!(选项, Val::Dict(_)) {
        return Err(err_with_hint(
            "类型错误",
            format!("「窗口.{名}」要一个字典"),
            "例如 {\"标题\": \"选一个文件\", \"类型\": [[\"图片\", \"png,jpg\"]]}".into(),
        ));
    }
    let mut s = String::new();
    转json(选项, &mut s)?;
    Ok(s)
}

fn 菜单json(定义: &Val) -> Result<String, crate::JishiError> {
    if !matches!(定义, Val::List(_)) {
        return Err(err_with_hint(
            "类型错误",
            "菜单定义要一个列表",
            "例如 [{\"标签\": \"文件\", \"子项\": [{\"标签\": \"退出\", \"动作\": \"退出\"}]}]".into(),
        ));
    }
    let mut s = String::new();
    转json(定义, &mut s)?;
    Ok(s)
}

/// 「只能在脚本里做」的统一报错：页面（wasm）里没有窗口控制权。
///
/// `allow(dead_code)`：只有 wasm 构建才调它（原生构建走另一条分支），
/// 而反过来 `没有原生窗口` 只在关掉 `native-window` 的构建里用得上。
#[allow(dead_code)]
fn 只在脚本里(名: &str, 提示: &str) -> crate::JishiError {
    err_with_hint(
        "运行期错误",
        format!("页面里不能用「窗口.{名}」—— 它要操作宿主窗口，而页面跑在沙箱里"),
        format!("{提示}；这类调用写在脚本里（窗口.等 之前）"),
    )
}

/// 这个构建没有原生窗口能力（`--no-default-features`）时的统一报错。
#[allow(dead_code)]
fn 没有原生窗口(名: &str) -> crate::JishiError {
    err_with_hint(
        "运行期错误",
        format!("这个构建没有原生窗口能力，「窗口.{名}」用不了（编译时没开 native-window 特性）"),
        "用完整发行版；或者退回应用模式（窗口.打开({原生: 假})）—— 那条路没有菜单/托盘".into(),
    )
}

/// 把文本写成 JSON 字符串字面量（`esc` 自带引号，别再补一层 —— M175 踩过）。
#[allow(dead_code)]
fn json串(s: &str) -> String {
    crate::jsonw::esc(s)
}

// -- M175 的八个函数 --------------------------------------------------------

fn 设菜单(定义: &Val) -> Result<bool, crate::JishiError> {
    let 文 = 菜单json(定义)?;
    #[cfg(target_arch = "wasm32")]
    {
        let _ = 文;
        return Err(只在脚本里("设菜单", "页面里要接菜单点击，用 窗口.当(\"菜单\", 函数)"));
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        #[cfg(feature = "native-window")]
        {
            crate::native_window::设菜单(&文).map_err(|e| err("运行期错误", e))?;
            Ok(true)
        }
        #[cfg(not(feature = "native-window"))]
        Err(没有原生窗口("设菜单"))
    }
}

fn 设右键菜单(定义: &Val) -> Result<bool, crate::JishiError> {
    let 文 = 菜单json(定义)?;
    #[cfg(target_arch = "wasm32")]
    {
        let _ = 文;
        return Err(只在脚本里("设右键菜单", "页面自己的右键菜单，用网页的 contextmenu 事件就行"));
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        #[cfg(feature = "native-window")]
        {
            crate::native_window::设右键菜单(&文).map_err(|e| err("运行期错误", e))?;
            Ok(true)
        }
        #[cfg(not(feature = "native-window"))]
        Err(没有原生窗口("设右键菜单"))
    }
}

fn 设托盘(选项: &Val) -> Result<bool, crate::JishiError> {
    let 文 = 选项json(选项, "设托盘")?;
    #[cfg(target_arch = "wasm32")]
    {
        let _ = 文;
        return Err(只在脚本里("设托盘", "页面里要接托盘点击，用 窗口.当(\"托盘\", 函数)"));
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        #[cfg(feature = "native-window")]
        {
            crate::native_window::设托盘(&文).map_err(|e| err("运行期错误", e))?;
            Ok(true)
        }
        #[cfg(not(feature = "native-window"))]
        Err(没有原生窗口("设托盘"))
    }
}

fn 关托盘() -> Result<bool, crate::JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        return Err(只在脚本里("关托盘", "托盘归宿主管"));
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        #[cfg(feature = "native-window")]
        {
            Ok(crate::native_window::关托盘())
        }
        #[cfg(not(feature = "native-window"))]
        Err(没有原生窗口("关托盘"))
    }
}

fn 单实例(名: &str) -> Result<bool, crate::JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        let _ = 名;
        return Err(只在脚本里("单实例", "单实例是「同一台机器别开两份」，归脚本管"));
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        #[cfg(feature = "native-window")]
        {
            crate::native_window::单实例(名).map_err(|e| err("运行期错误", e))
        }
        #[cfg(not(feature = "native-window"))]
        Err(没有原生窗口("单实例"))
    }
}

fn 选文件(选项: &Val) -> Result<Val, crate::JishiError> {
    let 多选 = if matches!(选项, Val::Dict(_)) {
        取布(选项, "多选", false)?
    } else {
        false
    };
    let 文 = if matches!(选项, Val::None_) {
        "{}".to_string()
    } else {
        选项json(选项, "选文件")?
    };
    #[cfg(target_arch = "wasm32")]
    {
        let _ = 文;
        return Err(只在脚本里(
            "选文件",
            "页面里选文件用网页的 <input type=\"file\">（浏览器自己会给文件选择框）",
        ));
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        #[cfg(feature = "native-window")]
        {
            let 路们 = crate::native_window::选文件(&文).map_err(|e| err("运行期错误", e))?;
            Ok(if 多选 {
                Val::List(std::rc::Rc::new(std::cell::RefCell::new(
                    路们.into_iter().map(|p| Val::Str(p.into())).collect(),
                )))
            } else {
                路们.into_iter().next().map(|p| Val::Str(p.into())).unwrap_or(Val::None_)
            })
        }
        #[cfg(not(feature = "native-window"))]
        Err(没有原生窗口("选文件"))
    }
}

fn 存文件(选项: &Val) -> Result<Val, crate::JishiError> {
    let 文 = if matches!(选项, Val::None_) {
        "{}".to_string()
    } else {
        选项json(选项, "存文件")?
    };
    #[cfg(target_arch = "wasm32")]
    {
        let _ = 文;
        return Err(只在脚本里("存文件", "页面里存文件用网页的下载（<a download>）"));
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        #[cfg(feature = "native-window")]
        {
            let 路 = crate::native_window::存文件(&文).map_err(|e| err("运行期错误", e))?;
            Ok(路.map(|p| Val::Str(p.into())).unwrap_or(Val::None_))
        }
        #[cfg(not(feature = "native-window"))]
        Err(没有原生窗口("存文件"))
    }
}

/// 取 `窗口.当` 的第一个参数：必须是下面四个之一。
///
/// 先校验它、再去取函数名：用户把事件名写错时，应该看到「没有这个事件」
/// 而不是「函数不是具名的」（后者会把人引到错的方向）。
fn 事件名(v: &Val) -> Result<String, crate::JishiError> {
    let 名 = match v {
        Val::Str(s) => s.to_string(),
        other => {
            return Err(err(
                "类型错误",
                format!("「当」的第一个参数要事件名（文本），给的是{}", crate::type_label(other)),
            ))
        }
    };
    match 名.as_str() {
        "菜单" | "拖放" | "托盘" | "再次启动" => Ok(名),
        other => Err(err_with_hint(
            "值错误",
            format!("没有「{other}」这个宿主事件"),
            "能接的是：菜单 / 拖放 / 托盘 / 再次启动".into(),
        )),
    }
}

/// `窗口.当(事件名, 函数)`：页面里登记「宿主事件 → 基石函数」。
///
/// 事件有四个：`菜单`（参数 = 动作名）、`拖放`（参数 = 路径，多个用换行分隔）、
/// `托盘`（参数 = "点击"）、`再次启动`（参数 = 第二个实例的命令行参数）。
fn 当(事件: &str, 函数: &str) -> Result<bool, crate::JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        let 文 = format!("{{\"事件\":{},\"函数\":{}}}", json串(事件), json串(函数));
        Ok(crate::abi3::发("当", &文) > 0)
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        // 原生构建里这两个参数都用不上（这条分支只会报错），但报错文案要指名道姓
        let _ = (事件, 函数);
        Err(err_with_hint(
            "运行期错误",
            "「窗口.当」只在**页面里**有效 —— 脚本这会儿正阻塞在 窗口.等 上，接不了事件",
            "把 窗口.当 写进页面的基石代码里".into(),
        ))
    }
}

/// `窗口.当` 的第二个参数：要**具名函数**。
///
/// 与 `浏览器.绑定` 同一个理由（M167）：事件来的时候是**另一次执行回合**，
/// 手里只有名字，没有上一次那个 VM 里的函数对象。
fn 当函数名(v: &Val) -> Result<String, crate::JishiError> {
    match v {
        Val::Str(s) if !s.is_empty() => Ok(s.to_string()),
        Val::Func(f) => Ok(f.name.clone()),
        Val::WalkFn(f) => Ok(f.name.clone()),
        _ => Err(err(
            "类型错误",
            "「窗口.当」的第二个参数要传**具名函数**（比如 `处理菜单`）—— 事件来的时候\
             是另一次执行回合，手里只有名字；匿名函数找不到。",
        )),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "窗口";
        "打开" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("打开", args, 1, 1)?;
            Ok(Val::Int(打开(&args[0])?))
        },
        "关闭" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            // M174：**省略句柄 = 关掉「自己所在的那个窗口」**。
            // 这个形状是给**页面里的代码**用的：网页里的基石代码不知道句柄
            // （它跑在 wasm 里，窗口是宿主的事），但它确实需要「点一下就关窗」。
            if args.is_empty() {
                return Ok(Val::Bool(无参关闭()?));
            }
            // 页面里没有「句柄」这回事 —— 传个数字进来要明确报错，
            // 别让它去找一张空的表然后返回假（那看起来像「窗口不存在」，
            // 其实是「你这个参数在页面里根本没有意义」）。
            #[cfg(target_arch = "wasm32")]
            {
                return Err(err_with_hint(
                    "运行期错误",
                    "页面里没有窗口句柄（你就在窗口里）",
                    "页面里写 `窗口.关闭()`，不要传句柄".into(),
                ));
            }
            need_range("关闭", args, 1, 1)?;
            let 句柄 = match &args[0] {
                Val::Int(i) => *i,
                other => return Err(err("类型错误", format!("「关闭」要窗口句柄（整数），给的是{}", crate::type_label(other)))),
            };
            Ok(Val::Bool(关闭(句柄)))
        },
        "设标题" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("设标题", args, 1, 1)?;
            let 新 = match &args[0] {
                Val::Str(s) => s.to_string(),
                other => return Err(err("类型错误", format!("「设标题」要文本，给的是{}", crate::type_label(other)))),
            };
            Ok(Val::Bool(设标题(&新)?))
        },
        "在运行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("在运行", args, 1, 1)?;
            let 句柄 = match &args[0] {
                Val::Int(i) => *i,
                other => return Err(err("类型错误", format!("「在运行」要窗口句柄（整数），给的是{}", crate::type_label(other)))),
            };
            let mut 表 = 表().lock().unwrap();
            let 活着 = match 取记录(句柄, &mut 表) {
                Some(记录) if 记录.原生 => {
                    drop(表);
                    #[cfg(feature = "native-window")]
                    {
                        crate::native_window::在运行()
                    }
                    #[cfg(not(feature = "native-window"))]
                    {
                        false
                    }
                }
                Some(记录) => match 记录.子.as_mut() {
                    // try_wait 会顺手把已退出的子进程状态收掉
                    Some(c) => !matches!(c.try_wait(), Ok(Some(_))),
                    None => false,
                },
                None => false,
            };
            Ok(Val::Bool(活着))
        },
        "等" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("等", args, 1, 1)?;
            let 句柄 = match &args[0] {
                Val::Int(i) => *i,
                other => return Err(err("类型错误", format!("「等」要窗口句柄（整数），给的是{}", crate::type_label(other)))),
            };
            Ok(Val::Int(等(句柄)))
        },
        // -- M175：桌面集成（菜单 / 托盘 / 单实例 / 文件对话框 / 页面事件） --
        //
        // 这八个分两类：
        //   · **脚本侧**（设菜单 / 设右键菜单 / 设托盘 / 关托盘 / 单实例 /
        //     选文件 / 存文件）—— 它们操作的是宿主窗口，页面（wasm）里明确报错；
        //   · **页面侧**（当）—— 登记「宿主事件 → 基石函数」，脚本里明确报错。
        "设菜单" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("设菜单", args, 1, 1)?;
            Ok(Val::Bool(设菜单(&args[0])?))
        },
        "设右键菜单" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("设右键菜单", args, 1, 1)?;
            Ok(Val::Bool(设右键菜单(&args[0])?))
        },
        "设托盘" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("设托盘", args, 1, 1)?;
            Ok(Val::Bool(设托盘(&args[0])?))
        },
        "关托盘" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("关托盘", args, 0, 0)?;
            Ok(Val::Bool(关托盘()?))
        },
        "单实例" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("单实例", args, 1, 1)?;
            match &args[0] {
                Val::Str(s) if !s.trim().is_empty() => Ok(Val::Bool(单实例(s)?)),
                Val::Str(_) => Err(err("值错误", "「单实例」的名字不能是空文本")),
                other => Err(err(
                    "类型错误",
                    format!("「单实例」要名字（文本），给的是{}", crate::type_label(other)),
                )),
            }
        },
        "选文件" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("选文件", args, 0, 1)?;
            let 空 = Val::None_;
            Ok(选文件(args.first().unwrap_or(&空))?)
        },
        "存文件" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("存文件", args, 0, 1)?;
            let 空 = Val::None_;
            Ok(存文件(args.first().unwrap_or(&空))?)
        },
        "当" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("当", args, 2, 2)?;
            let 事件 = 事件名(&args[0])?;
            let 函数 = 当函数名(&args[1])?;
            Ok(Val::Bool(当(&事件, &函数)?))
        },
        "全部关闭" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("全部关闭", args, 0, 0)?;
            let 号们: Vec<i128> = {
                let 表 = 表().lock().unwrap();
                表.iter()
                    .enumerate()
                    .filter(|(_, x)| x.is_some())
                    .map(|(i, _)| i as i128 + 1)
                    .collect()
            };
            let 数 = 号们.len();
            for 号 in 号们 {
                // 原生窗口的记录**不能**在这里 take 掉：`等` 还要靠它决定
                // 「该跑事件循环」，take 早了 = 循环永远不跑 = WebView2 泄漏。
                关闭(号);
                if !是原生(号) {
                    收尾(号);
                }
            }
            Ok(Val::Int(数 as i128))
        },
    }
}

/// 这个句柄是不是原生窗口（已关掉 / 不存在都算「不是」）。
fn 是原生(句柄: i128) -> bool {
    let mut 表 = 表().lock().unwrap();
    取记录(句柄, &mut 表).map(|r| r.原生).unwrap_or(false)
}

/// 省略句柄的 `窗口.关闭()`：关掉「**自己所在的那个窗口**」。
///
/// - **wasm（页面里）**：走 ABI 3 的 `jishi_native("关闭")`，宿主收到就关窗。
///   浏览器应用模式下 shim 退化成 `window.close()`。
/// - **原生脚本里**：请求关掉本进程那个原生窗口。事件循环还没跑也能请求
///   （事件会被排住，`等` 一进循环就收到）。
fn 无参关闭() -> Result<bool, crate::JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        return Ok(crate::abi3::发("关闭", "") > 0);
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        #[cfg(feature = "native-window")]
        {
            return Ok(crate::native_window::请求关闭());
        }
        #[cfg(not(feature = "native-window"))]
        {
            return Err(err_with_hint(
                "运行期错误",
                "这个构建没有原生窗口能力：`窗口.关闭()` 省略句柄只在原生窗口或页面里有效",
                "写全参数：窗口.关闭(句柄)".into(),
            ));
        }
    }
}

/// `窗口.设标题(新标题)`：改窗口标题。
///
/// - **wasm（页面里）**：走 ABI 3。原生窗口改真标题；浏览器应用模式下 shim
///   改 `document.title`（`--app` 窗口的标题栏显示的就是它）—— 同一句代码
///   在两代外壳下都生效，这正是 ABI 3 想要的效果。
/// - **原生脚本里**：直接改本进程那个窗口。
fn 设标题(新: &str) -> Result<bool, crate::JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        return Ok(crate::abi3::发("标题", 新) > 0);
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        #[cfg(feature = "native-window")]
        {
            if crate::native_window::设标题(新) {
                return Ok(true);
            }
            return Err(err_with_hint(
                "运行期错误",
                "现在没有原生窗口可以改标题",
                "先 窗口.打开({原生: 真}) 再改；不想用原生窗口的话，页面里改 document.title 就行"
                    .into(),
            ));
        }
        #[cfg(not(feature = "native-window"))]
        {
            return Err(err_with_hint(
                "运行期错误",
                "这个构建没有原生窗口能力",
                "用完整发行版，或者在页面里改 document.title".into(),
            ));
        }
    }
}
