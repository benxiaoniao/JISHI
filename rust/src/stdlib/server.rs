//! 标准库 `HTTP服务端`（M115）—— 起一个**只监听本机**的小服务。
//!
//! 分两半，边界如实说：
//!
//! | 一半 | 函数 | 哪些平台能跑 |
//! |---|---|---|
//! | **纯协议核心**（只是文本处理） | `解析请求` / `构造响应` | 这里全在 —— 原生 / wasm 都能跑 |
//! | **真服务端**（要 socket） | `启动` / `空闲端口` | 原生 ✅；**wasm ✗**（浏览器没有 socket） |
//!
//! ⚠️ Node 宿主也起不了（`node:http` 是异步 + 事件循环，基石 VM 是同步的），
//! 那条边界在 `oracle/node/vm.js` 里报。取舍见 `docs/设计决策.md` **D81**。
//!
//! ## 三条约定（跨引擎一致的前提，与 Python 侧逐条对应）
//!
//! 1. **只监听 `127.0.0.1`**（安全默认，局域网连不上）；
//! 2. **一问一答就关**（`Connection: close`）—— 一个请求 = 一次 `处理函数` 调用；
//! 3. **正文按 UTF-8**，不合法字节替换成 `U+FFFD`。
//!
//! ## 两个「只认 ASCII」的地方（跨语言必须写死，别用宿主的 Unicode 表）
//!
//! - 请求行按**空格 / 制表符**切（`split_whitespace` 各语言的 Unicode 表不一样）；
//! - 头的**名字与值**去两端空白只去**空格 / 制表符**。
//!
//! 这是 M107 `html` 那条教训的同族：`isspace` / `\s` 在 U+001C–1F、U+0085 上
//! 对不齐，所以空白集合必须自己写死。

use std::collections::HashMap;
use std::io::{Read, Write};

use super::{as_text, need_range, table, R};
use crate::{dict_items, dict_new, err, list_new, BuiltinHost, JishiError, Val};

/// 状态码 → 原因短语。**认不得的状态码留空**（不硬编一个假的英文短语）。
fn 原因(码: i128) -> &'static str {
    match 码 {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        204 => "No Content",
        301 => "Moved Permanently",
        302 => "Found",
        303 => "See Other",
        304 => "Not Modified",
        307 => "Temporary Redirect",
        308 => "Permanent Redirect",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        408 => "Request Timeout",
        409 => "Conflict",
        413 => "Content Too Large",
        415 => "Unsupported Media Type",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "",
    }
}

const 默认内容类型: &str = "text/plain; charset=utf-8";

/// 响应头里**不许用户自己写**的名字（框架自己填）。
fn 自填(名: &str) -> bool {
    matches!(
        名.to_ascii_lowercase().as_str(),
        "content-length" | "content-type" | "connection"
    )
}

/// 去两端空白 —— **只去空格与制表符**（不用各语言不一的 `trim`）。
fn 去空白(s: &str) -> &str {
    s.trim_matches(|c| c == ' ' || c == '\t')
}

/// 按**空格 / 制表符**切成若干段（丢掉空段）。请求行用它。
fn 切段(s: &str) -> Vec<&str> {
    去空白(s)
        .split(|c| c == ' ' || c == '\t')
        .filter(|p| !p.is_empty())
        .collect()
}

/// `%XX` 解码（UTF-8，不合法字节替换）。⚠️ **`+` 不当作空格**。
fn 百分号解码(s: &str) -> String {
    let b = s.as_bytes();
    let mut 出: Vec<u8> = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            let 高 = (b[i + 1] as char).to_digit(16);
            let 低 = (b[i + 2] as char).to_digit(16);
            if let (Some(h), Some(l)) = (高, 低) {
                出.push((h * 16 + l) as u8);
                i += 3;
                continue;
            }
        }
        出.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&出).to_string()
}

/// **只收整数**（浮点、文本都不收）—— 三宿主同一句文案。
///
/// ⚠️ 不能用 `crate::as_int`：它报的是「需要整数」（给别的内建用的），
/// 而这里要说清**是谁**要整数。
fn 要整数(v: &Val, 谁: &str) -> Result<i128, JishiError> {
    match v {
        Val::Int(i) => Ok(*i),
        _ => Err(err("类型错误", format!("{谁}要是整数"))),
    }
}

fn 要文本(v: &Val, 谁: &str) -> Result<String, JishiError> {
    match v {
        Val::Str(_) => Ok(as_text(v)),
        other => Err(err(
            "类型错误",
            format!("{谁}要是文本，得到了「{}」", crate::type_label(other)),
        )),
    }
}

/// 请求文本 → 请求字典。**服务端也走这一条**（只有一份解析实现）。
pub(crate) fn 解析请求字典(原始: &str) -> Result<Val, JishiError> {
    let (头部, 正文) = match 原始.find("\r\n\r\n") {
        Some(i) => (&原始[..i], &原始[i + 4..]),
        None => (原始, ""),
    };
    let mut 行们 = 头部.split("\r\n");
    let 首行 = 行们.next().unwrap_or("");
    if 首行.trim_matches(|c| c == ' ' || c == '\t').is_empty() {
        return Err(err("值错误", "这不是一个 HTTP 请求：第一行是空的"));
    }
    let 片段 = 切段(首行);
    if 片段.len() < 2 {
        return Err(err(
            "值错误",
            format!("这不是一个 HTTP 请求：第一行该是「方法 路径 版本」，现在是「{首行}」"),
        ));
    }
    let 方法 = 片段[0].to_string();
    let 目标 = 片段[1];
    let 版本 = if 片段.len() > 2 { 片段[2].to_string() } else { "HTTP/1.1".to_string() };
    let (路径, 查询串) = match 目标.find('?') {
        Some(i) => (&目标[..i], &目标[i + 1..]),
        None => (目标, ""),
    };
    let mut 查询对: Vec<(Val, Val)> = Vec::new();
    for 片段 in 查询串.split('&') {
        if 片段.is_empty() {
            continue;
        }
        let (k, v) = match 片段.find('=') {
            Some(i) => (&片段[..i], &片段[i + 1..]),
            None => (片段, ""),
        };
        查询对.push((Val::Str(百分号解码(k)), Val::Str(百分号解码(v))));
    }
    let mut 头对: Vec<(Val, Val)> = Vec::new();
    for 行 in 行们 {
        let 行 = 去空白(行);
        if 行.is_empty() {
            continue;
        }
        let (名, 值) = match 行.find(':') {
            Some(i) => (去空白(&行[..i]), 去空白(&行[i + 1..])),
            None => {
                return Err(err("值错误", format!("请求头这一行没有冒号：「{行}」")));
            }
        };
        头对.push((
            Val::Str(名.to_ascii_lowercase()),
            Val::Str(值.to_string()),
        ));
    }
    Ok(dict_new(vec![
        (Val::Str("方法".into()), Val::Str(方法)),
        (Val::Str("路径".into()), Val::Str(百分号解码(路径))),
        (Val::Str("查询".into()), dict_new(查询对)),
        (Val::Str("版本".into()), Val::Str(版本)),
        (Val::Str("请求头".into()), dict_new(头对)),
        (Val::Str("正文".into()), Val::Str(正文.to_string())),
    ]))
}

/// 「状态 + 正文 + 头」→ 完整响应文本。`Content-Length` 按**字节**算。
fn 构造响应文本(
    码: i128,
    正文: &str,
    内容类型: &str,
    头: Option<&Val>,
) -> Result<String, JishiError> {
    let mut 行们: Vec<String> = Vec::new();
    let 短语 = 原因(码);
    行们.push(if 短语.is_empty() {
        format!("HTTP/1.1 {码}")
    } else {
        format!("HTTP/1.1 {码} {短语}")
    });
    行们.push(format!("Content-Type: {内容类型}"));
    if let Some(v) = 头 {
        match v {
            Val::None_ => {}
            Val::Dict(d) => {
                for (k, val) in dict_items(&d.borrow()) {
                    let 键 = as_text(&k);
                    if 自填(&键) {
                        return Err(err(
                            "值错误",
                            format!("「{键}」由框架自己填，不要在「头」里再写一遍"),
                        ));
                    }
                    let 值 = as_text(&val);
                    if !键.is_ascii() || !值.is_ascii() {
                        return Err(err(
                            "类型错误",
                            format!("请求头「{键}」的名字与值只能是 ASCII（英文数字）"),
                        ));
                    }
                    行们.push(format!("{键}: {值}"));
                }
            }
            other => {
                return Err(err(
                    "类型错误",
                    format!("「头」要是一个字典，得到了「{}」", crate::type_label(other)),
                ))
            }
        }
    }
    行们.push(format!("Content-Length: {}", 正文.len()));
    行们.push("Connection: close".to_string());
    Ok(format!("{}\r\n\r\n{正文}", 行们.join("\r\n")))
}

/// 处理函数的返回值 → 完整响应文本。**文本**直接用；**字典**交给 `构造响应文本`。
fn 把应答变文本(应答: &Val) -> Result<String, JishiError> {
    match 应答 {
        Val::Str(_) => Ok(as_text(应答)),
        Val::None_ => 构造响应文本(200, "", 默认内容类型, None),
        Val::Dict(d) => {
            let d = d.borrow();
            let 码 = match crate::dict_get(&d, &Val::Str("状态".into())) {
                Some(v) => 要整数(&v, "状态码")?,
                None => 200,
            };
            // ⚠️ 与 `构造响应` 同一条校验 —— 处理函数返回的字典**也**要过
            //    （不然「字典写法」能绕过范围检查，两边行为就不一样了）
            if !(100..=599).contains(&码) {
                return Err(err("值错误", format!("状态码要在 100–599 之间，现在是 {码}")));
            }
            let 正文 = match crate::dict_get(&d, &Val::Str("正文".into())) {
                Some(v) => 要文本(&v, "「构造响应」的正文")?,
                None => String::new(),
            };
            let 类型 = match crate::dict_get(&d, &Val::Str("内容类型".into())) {
                Some(v) => 要文本(&v, "「构造响应」的内容类型")?,
                None => 默认内容类型.to_string(),
            };
            let 头 = crate::dict_get(&d, &Val::Str("头".into()));
            构造响应文本(码, &正文, &类型, 头.as_ref())
        }
        other => Err(err(
            "类型错误",
            format!(
                "处理函数要返回文本或字典，得到了「{}」",
                crate::type_label(other)
            ),
        )),
    }
}

/// 从流里读出**原始请求文本**（头 + 正文）。
#[cfg(not(target_arch = "wasm32"))]
fn 读原始请求(流: &mut std::net::TcpStream) -> Result<String, JishiError> {
    let mut 缓冲: Vec<u8> = Vec::new();
    let mut 块 = [0u8; 4096];
    let 头末 = loop {
        if let Some(i) = 缓冲.windows(4).position(|w| w == b"\r\n\r\n") {
            break i + 4;
        }
        let n = 流
            .read(&mut 块)
            .map_err(|e| err("运行期错误", format!("读请求失败：{e}")))?;
        if n == 0 {
            break 缓冲.len();
        }
        缓冲.extend_from_slice(&块[..n]);
        if 缓冲.len() > 1_048_576 {
            return Err(err("运行期错误", "请求头超过 1 MB —— 这不是本地小服务的用法"));
        }
    };
    let 头文本 = String::from_utf8_lossy(&缓冲[..头末]).to_lowercase();
    let mut 要 = 0usize;
    for 行 in 头文本.split("\r\n") {
        if let Some(rest) = 行.strip_prefix("content-length:") {
            要 = 去空白(rest).parse::<usize>().unwrap_or(0);
        }
    }
    while 缓冲.len() < 头末 + 要 {
        let n = 流
            .read(&mut 块)
            .map_err(|e| err("运行期错误", format!("读正文失败：{e}")))?;
        if n == 0 {
            break;
        }
        缓冲.extend_from_slice(&块[..n]);
    }
    Ok(String::from_utf8_lossy(&缓冲).to_string())
}

#[cfg(not(target_arch = "wasm32"))]
fn 起服务(
    端口: i128,
    处理函数: Val,
    上限: i128,
    vm: &mut dyn BuiltinHost,
) -> Result<Val, JishiError> {
    use std::net::TcpListener;
    if !(0..=65535).contains(&端口) {
        return Err(err("值错误", format!("端口要在 0–65535 之间，现在是 {端口}")));
    }
    // 只监听本机（约定 ①）
    let 监听 = TcpListener::bind(("127.0.0.1", 端口 as u16))
        .map_err(|e| err("运行期错误", format!("起不了服务：{e}")))?;
    let 实际 = 监听
        .local_addr()
        .map_err(|e| err("运行期错误", format!("读不到端口：{e}")))?
        .port() as i128;
    let mut 收到: i128 = 0;
    loop {
        let (mut 流, _) = match 监听.accept() {
            Ok(x) => x,
            // 单个连接出问题不该把服务带走
            Err(_) => continue,
        };
        // ⚠️ 返回 `None` = **空连接**（连上就断：端口探测 / 健康检查）——
        //    **不算一个请求**。为什么在意：`最多请求数` 数的是「处理过的请求」，
        //    不是「接受过的连接」；混起来的话探测连接会把配额吃掉，
        //    真实请求反而被拒（M115 自己踩到）。
        let 结果 = (|| -> Result<Option<String>, JishiError> {
            let 原始 = 读原始请求(&mut 流)?;
            if 原始.is_empty() {
                return Ok(None);
            }
            let 请求 = 解析请求字典(&原始)?;
            Ok(Some(match vm.call_value(处理函数.clone(), vec![请求]) {
                Ok(应答) => 把应答变文本(&应答)?,
                // 处理函数出错 ⇒ 回 500（正文就是错误信息），**服务继续**：
                // 一条坏请求不该让整个本地服务挂掉，而且浏览器里直接看得见错在哪
                // ⚠️ 正文只用 `message` —— 三个宿主那句**本来就是逐字对齐**的，
                //    用整段渲染（带位置与 `^` 指示）反而会各说各的
                Err(e) => 构造响应文本(500, &e.message, 默认内容类型, None)?,
            }))
        })();
        let 文本 = match 结果 {
            Ok(None) => continue,          // 空连接：不算请求、不判停
            Ok(Some(t)) => t,
            // 请求本身不合法 ⇒ 400（**算**一个请求：人家确实发了东西过来）
            Err(e) => 构造响应文本(400, &e.message, 默认内容类型, None).unwrap_or_default(),
        };
        let _ = 流.write_all(文本.as_bytes());
        let _ = 流.flush();
        收到 += 1;
        if 上限 > 0 && 收到 >= 上限 {
            break;
        }
    }
    Ok(dict_new(vec![
        (Val::Str("端口".into()), Val::Int(实际)),
        (Val::Str("请求数".into()), Val::Int(收到)),
    ]))
}

#[cfg(target_arch = "wasm32")]
fn 起服务(
    _端口: i128,
    _处理函数: Val,
    _上限: i128,
    _vm: &mut dyn BuiltinHost,
) -> Result<Val, JishiError> {
    Err(crate::err_with_hint(
        "运行期错误",
        "这个平台起不了 HTTP 服务端（浏览器里没有 socket）",
        "换桌面版/命令行版就能用 `HTTP服务端.启动` —— 单二进制里带着它；".to_string()
            + "纯的 `解析请求` / `构造响应` 在网页里照样能用",
    ))
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "HTTP服务端";
        "解析请求" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("解析请求", args, 1, 1)?;
            let 原始 = 要文本(&args[0], "「解析请求」的参数")?;
            Ok(解析请求字典(&原始)?)
        },
        "构造响应" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("构造响应", args, 0, 4)?;
            let 码 = match args.first() {
                None | Some(Val::None_) => 200,
                Some(v) => 要整数(v, "状态码")?,
            };
            if !(100..=599).contains(&码) {
                return Err(err("值错误", format!("状态码要在 100–599 之间，现在是 {码}")));
            }
            let 正文 = match args.get(1) {
                None | Some(Val::None_) => String::new(),
                Some(v) => 要文本(v, "「构造响应」的正文")?,
            };
            let 类型 = match args.get(2) {
                None | Some(Val::None_) => 默认内容类型.to_string(),
                Some(v) => 要文本(v, "「构造响应」的内容类型")?,
            };
            let 头 = args.get(3).filter(|v| !matches!(v, Val::None_));
            Ok(Val::Str(构造响应文本(码, &正文, &类型, 头)?))
        },
        "空闲端口" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("空闲端口", args, 0, 0)?;
            #[cfg(not(target_arch = "wasm32"))]
            {
                use std::net::TcpListener;
                let l = TcpListener::bind(("127.0.0.1", 0u16))
                    .map_err(|e| err("运行期错误", format!("问不到空闲端口：{e}")))?;
                let p = l.local_addr().map(|a| a.port()).unwrap_or(0);
                drop(l);
                return Ok(Val::Int(p as i128));
            }
            #[cfg(target_arch = "wasm32")]
            {
                let _ = list_new(vec![]);
                Err(crate::err_with_hint(
                    "运行期错误",
                    "这个平台问不到端口（浏览器里没有 socket）",
                    "换桌面版/命令行版就能用".to_string(),
                ))
            }
        },
        "启动" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("启动", args, 2, 3)?;
            let 端口 = 要整数(&args[0], "端口")?;
            let 处理函数 = args[1].clone();
            if !matches!(
                处理函数,
                Val::Func(_) | Val::WalkFn(_) | Val::Builtin(_, _) | Val::BoundMethod(_)
            ) {
                return Err(err(
                    "类型错误",
                    "「启动」的第二个参数要一个**处理函数**：它收一个「请求」字典，\
                     返回响应（文本，或 {状态, 正文, 内容类型, 头} 字典）",
                ));
            }
            let 上限 = match args.get(2) {
                None | Some(Val::None_) => 0i128,
                Some(v) => 要整数(v, "最多请求数")?,
            };
            if 上限 < 0 {
                return Err(err("值错误", format!("最多请求数不能是负数，现在是 {上限}")));
            }
            起服务(端口, 处理函数, 上限, vm)
        },
    }
}
