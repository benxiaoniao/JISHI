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

fn 起服务(目录: &str) -> Result<u16, crate::JishiError> {
    let 键 = 目录.replace('\\', "/").to_lowercase();
    if let Some(s) = 服务表().lock().unwrap().get(&键) {
        return Ok(s.端口);
    }
    let 监听 = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|e| err("运行期错误", format!("起不了本地服务：{e}")))?;
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
        let 端口 = 起服务(&目录)?;
        let 名 = 全
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default();
        (format!("http://127.0.0.1:{端口}/{}", 编码(&名)), 目录)
    } else {
        return Err(err("值错误", "「窗口.打开」要指定「页面」或「服务」"));
    };

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
    // 把子进程**取出来**再等：不能拿着锁阻塞（别的窗口还要用表）
    let 子 = {
        let mut 表 = 表().lock().unwrap();
        match 取记录(句柄, &mut 表) {
            None => return 0,          // 已经关掉了：等一个关掉的窗口 = 立刻返回
            Some(记录) => 记录.子.take(),
        }
    };
    let 码 = match 子 {
        None => 0,
        Some(mut c) => c.wait().map(|s| s.code().unwrap_or(0) as i128).unwrap_or(0),
    };
    收尾(句柄);
    码
}

// ---------------------------------------------------------------------------

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "窗口";
        "打开" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("打开", args, 1, 1)?;
            Ok(Val::Int(打开(&args[0])?))
        },
        "关闭" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("关闭", args, 1, 1)?;
            let 句柄 = match &args[0] {
                Val::Int(i) => *i,
                other => return Err(err("类型错误", format!("「关闭」要窗口句柄（整数），给的是{}", crate::type_label(other)))),
            };
            Ok(Val::Bool(关闭(句柄)))
        },
        "在运行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("在运行", args, 1, 1)?;
            let 句柄 = match &args[0] {
                Val::Int(i) => *i,
                other => return Err(err("类型错误", format!("「在运行」要窗口句柄（整数），给的是{}", crate::type_label(other)))),
            };
            let mut 表 = 表().lock().unwrap();
            let 活着 = match 取记录(句柄, &mut 表) {
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
                关闭(号);
                收尾(号);
            }
            Ok(Val::Int(数 as i128))
        },
    }
}
