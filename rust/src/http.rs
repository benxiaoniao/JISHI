//! 标准库 `网络`（Python 侧 `jishi/stdlib/网络.py`）。
//!
//! **Node 那边用子进程绕，Rust 这边绕不过**——而且基石的网络是**同步**的
//! （没有 `await`），所以需要一个能同步拿结果的 HTTP 客户端。零依赖的
//! 前提下，两条路：
//!
//! - **Windows：绑 `winhttp.dll`**（`extern "system"`，是系统库、不是 crate）。
//!   好处是 **https 也能用**——TLS 是 WinHTTP 内部的事，我们一行加密都不用写。
//! - **其它平台：裸 `TcpStream` 走 HTTP/1.1**。`http://` 完全可用；
//!   `https://` 自 M206 起**也能用** —— TLS 交给**系统自带的 OpenSSL**
//!   （`dlopen("libssl.so.3")` / macOS 的 `libssl.3.dylib` / 系统 LibreSSL），
//!   我们只负责「把 fd 交给它、设 SNI、校验证书与主机名」。
//!   ⚠️ 系统里没有 libssl 时**明确报错并说清怎么装**，绝不静默降级成明文。
//!
//! 默认超时 10 秒、`User-Agent: jishi/0.1`。
//!
//! ## M107 补了什么（2026-10-11）
//!
//! 原来只能 GET / 表单 POST，且**拿不到状态码与响应头**。这一轮补：
//! `请求头` 参数、`提交JSON`、`获取详情`（状态码 / 响应头 / 正文，非 2xx 不抛）、
//! `下载`（写文件、给字节数）。报错统一成 `HTTP {状态}`，各引擎措辞一致。
//!
//! ## M206 补了什么（2026-11-17）：联网安全护栏
//!
//! | 护栏 | 为什么必须有 |
//! |---|---|
//! | **响应大小上限**（默认 32 MB，`选项` 可改） | 以前 `WinHttpReadData` 往 `Vec` 里堆到底 —— 一个坏链接/恶意大响应就能把程序 OOM 掉 |
//! | **`下载` 流式落盘** | 同上；1 GB 的文件不该先占 1 GB 内存。现在边读边写 `.part`，成功才改名 |
//! | **重定向上限 5**（自己跟，不交给 WinHTTP） | 不自控的话「跨协议降级」拦不住，重定向环也数不清 |
//! | **跨协议降级禁止**（https→http 报错） | 明文降级是最经典的中间人入口。要明文请**显式**写 `http://` |
//! | **gzip / deflate 自动解压**（复用自家 inflate） | 不压是浪费带宽，而 `inflate` 我们本来就有；解压也**受同一上限约束**（防解压炸弹） |
//! | **代理**（`HTTP_PROXY`/`HTTPS_PROXY` + `选项["代理"]`） | 企业内网、抓包排查都要 |
//! | **头注入拦截** | 请求头的值里带 CR/LF 能把请求劈成两半（塞额外的头/响应）—— 直接拒 |
//! | **报错分类** | 「域名解析不了 / 连不上 / 被重置 / 超时 / 证书没过」是五件不同的事，不能都叫「出错了」 |
//!
//! 📌 **`选项` 字典**（`获取`/`获取JSON`/`获取详情` 是第 4 个参数，`提交`/`提交JSON`/`下载` 是第 5 个）：
//! `{"大小上限": 字节数, "重定向上限": 5, "允许降级": 假, "代理": "http://127.0.0.1:7890", "解压": 真}`。
//! 一个都不给时行为与 M107 一致（只是多了上限、解压、降级拦截三道保险）。

use crate::BuiltinHost;
use std::collections::HashMap;
use std::io::Write;

use crate::stdlib::{as_num, as_text, need_range, table, R};
use crate::{err, JishiError, Val};

const DEFAULT_TIMEOUT: f64 = 10.0;
const AGENT: &str = "jishi/0.1";

/// 内存里收响应时的默认上限：32 MB。
///
/// 为什么是这个数：网页一般 < 2 MB，JSON 接口更小；32 MB 已经很宽松，
/// 又不至于让「一个坏链接」把进程撑爆。要更大的自己传 `选项["大小上限"]`，
/// 传 `0` = 不限（`下载` 默认就是 0 —— 它流式落盘，不占内存）。
pub(crate) const DEFAULT_LIMIT: usize = 32 * 1024 * 1024;

/// 默认跟着走几次重定向（`urllib` 是 10，这里更保守：
/// 正常站点一两次就到位，多了只会在环里耗时间）。
pub(crate) const DEFAULT_REDIRECTS: u32 = 5;

/// 一次响应的完整形态（状态码 + 响应头 + 正文字节）。
pub(crate) struct Resp {
    status: u32,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
    /// 落盘时写出去的字节数（内存模式 = `body.len()`）。
    字节数: usize,
}

/// 拆出来的网址各部分。
struct Url {
    secure: bool,
    host: String,
    port: u16,
    path: String,
}

impl Url {
    /// 拼回文本（重定向解析、报错用）。
    fn 文本(&self) -> String {
        let 默认端口 = if self.secure { 443 } else { 80 };
        let 端口 = if self.port == 默认端口 {
            String::new()
        } else {
            format!(":{}", self.port)
        };
        format!(
            "{}://{}{}{}",
            if self.secure { "https" } else { "http" },
            self.host,
            端口,
            self.path
        )
    }

    /// `Host:` 头（端口不是默认值时要带上）。
    fn 主机头(&self) -> String {
        let 默认端口 = if self.secure { 443 } else { 80 };
        if self.port == 默认端口 {
            self.host.clone()
        } else {
            format!("{}:{}", self.host, self.port)
        }
    }
}

/// 一次请求的全部可调项。
///
/// 为什么做成结构体而不是一长串参数：`client::一次` 要在两个平台各实现一份，
/// 参数一多必漂移；结构体还能让默认口径只定义一处。
#[derive(Clone)]
pub(crate) struct 选项 {
    pub method: String,
    pub body: Option<Vec<u8>>,
    pub timeout: f64,
    pub headers: Vec<(String, String)>,
    /// 响应体上限（字节）。`0` = 不限。
    pub 上限: usize,
    /// 用户**显式给了** `大小上限`（`下载` 据此决定要不要「默认不限」）。
    pub 上限已给: bool,
    pub 重定向上限: u32,
    /// 落盘路径：给了就**边读边写**（先写 `.part`，成功才改名）。
    pub 落盘: Option<String>,
    /// `假` = https 不允许跳到 http（默认）。
    pub 允许降级: bool,
    pub 代理: Option<String>,
    /// 用户显式要求「不走代理」（`选项["代理"] = "无"`）—— 环境变量也不看。
    pub 绕过环境代理: bool,
    pub 解压: bool,
}

impl 选项 {
    fn 新(method: &str) -> Self {
        选项 {
            method: method.to_string(),
            body: None,
            timeout: DEFAULT_TIMEOUT,
            headers: Vec::new(),
            上限: DEFAULT_LIMIT,
            上限已给: false,
            重定向上限: DEFAULT_REDIRECTS,
            落盘: None,
            允许降级: false,
            代理: 环境代理(),
            绕过环境代理: false,
            解压: true,
        }
    }
}

/// `NO_PROXY` 里的条目（`,` 分隔；`*` 表示全不走代理）。
fn 不走代理表() -> Vec<String> {
    for 名 in ["NO_PROXY", "no_proxy"] {
        if let Ok(v) = std::env::var(名) {
            return v
                .split(',')
                .map(|x| x.trim().trim_start_matches('.').to_ascii_lowercase())
                .filter(|x| !x.is_empty())
                .collect();
        }
    }
    Vec::new()
}

/// 这台主机要不要走代理（照 `NO_PROXY` 判）。
fn 走代理(host: &str) -> bool {
    let 表 = 不走代理表();
    if 表.iter().any(|x| x == "*") {
        return false;
    }
    let h = host.to_ascii_lowercase();
    表.iter().any(|x| h == *x || h.ends_with(&format!(".{x}")))
        .eq(&false)
}

/// 从环境变量取代理（`HTTPS_PROXY` 优先，其次 `HTTP_PROXY`；大小写都认）。
fn 环境代理() -> Option<String> {
    for 名 in [
        "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
    ] {
        if let Ok(v) = std::env::var(名) {
            let v = v.trim();
            if !v.is_empty() && v.to_ascii_lowercase() != "none" {
                return Some(v.to_string());
            }
        }
    }
    None
}

/// 把网址里**非 ASCII 的字节**做百分号编码（台账 R101）。
///
/// 以前网址带中文必须自己 `编码.url编码` 再拼；现在**直接写中文**就行。
/// 保留集与 Python / Node 完全一致（含 `%` 本身 —— 否则已编码的网址会被二次编码）：
/// `:/?#[]@!$&'()*+,;=%` 之外的字节一律 `%XX`。
///
/// ⚠️ **中文域名**不在这里处理（那要先做 punycode，是另一件事）。
fn 规范网址(raw: &str) -> String {
    const 保留: &str = "-_.!~*'();/?:@&=+$,#[]%";
    let mut 出 = String::with_capacity(raw.len() + 16);
    for b in raw.bytes() {
        let c = b as char;
        if b.is_ascii_alphanumeric() || 保留.contains(c) {
            出.push(c);
        } else {
            出.push_str(&format!("%{b:02X}"));
        }
    }
    出
}

fn parse_url(raw: &str) -> Result<Url, JishiError> {
    let bad = || err("运行期错误", format!("「{raw}」不是能识别的网址（要写 http:// 或 https://）"));
    // R101：先规整（中文自动编码）。**报错里仍用用户原本写的那个**，好认。
    let 规整 = 规范网址(raw);
    let raw = 规整.as_str();
    let (secure, rest) = if let Some(r) = raw.strip_prefix("https://") {
        (true, r)
    } else if let Some(r) = raw.strip_prefix("http://") {
        (false, r)
    } else {
        return Err(bad());
    };
    let (authority, path) = match rest.find('/') {
        Some(k) => (&rest[..k], &rest[k..]),
        None => (rest, "/"),
    };
    if authority.is_empty() {
        return Err(bad());
    }
    // IPv6 的 `[::1]:8080` 也认
    let (host, port) = if let Some(rest2) = authority.strip_prefix('[') {
        match rest2.split_once(']') {
            Some((h, tail)) => {
                let p = tail.strip_prefix(':').and_then(|s| s.parse::<u16>().ok());
                (h.to_string(), p)
            }
            None => return Err(bad()),
        }
    } else {
        match authority.rsplit_once(':') {
            Some((h, p)) => match p.parse::<u16>() {
                Ok(v) => (h.to_string(), Some(v)),
                // 冒号后面不是端口（很少见），整段当主机名
                Err(_) => (authority.to_string(), None),
            },
            None => (authority.to_string(), None),
        }
    };
    Ok(Url {
        secure,
        host,
        port: port.unwrap_or(if secure { 443 } else { 80 }),
        path: path.to_string(),
    })
}

/// 按 `Location` 解析出下一跳（支持绝对 / `//host/path` / `/path` / 相对路径）。
fn 解析相对(base: &Url, loc: &str) -> Result<Url, JishiError> {
    let loc = loc.trim();
    if loc.starts_with("http://") || loc.starts_with("https://") {
        return parse_url(loc);
    }
    if let Some(rest) = loc.strip_prefix("//") {
        return parse_url(&format!(
            "{}://{rest}",
            if base.secure { "https" } else { "http" }
        ));
    }
    if loc.starts_with('/') {
        return Ok(Url {
            secure: base.secure,
            host: base.host.clone(),
            port: base.port,
            path: loc.to_string(),
        });
    }
    // 相对路径：以当前路径的目录为基准
    let 目录 = match base.path.rfind('/') {
        Some(k) => &base.path[..=k],
        None => "/",
    };
    Ok(Url {
        secure: base.secure,
        host: base.host.clone(),
        port: base.port,
        path: format!("{目录}{loc}"),
    })
}

fn 头值<'a>(headers: &'a [(String, String)], 名: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case(名))
        .map(|(_, v)| v.as_str())
}

/// 解压（gzip / deflate）。上限沿用调用方那个 —— **解压炸弹和「响应太大」是同一件事**。
fn 解压(数据: Vec<u8>, 编码: &str, 上限: usize) -> Result<Vec<u8>, String> {
    let 限 = if 上限 == 0 { usize::MAX } else { 上限 };
    let 编码 = 编码.to_ascii_lowercase();
    if 编码.contains("gzip") {
        // ⚠️ gzip **不是** zlib 的两字节头：它是 `1f 8b 08` + 可选扩展 + deflate
        //    + 8 字节尾（CRC32 + ISIZE）。以前只按 zlib 处理 ⇒ 0x1f & 0x0F = 15
        //    ⇒ 报「不是 zlib/gzip 流（压缩方法 15）」（2026-11-17 踩）。
        let 起 = gzip体起点(&数据).ok_or_else(|| "gzip 头不完整或不合法".to_string())?;
        return crate::inflate::inflate_raw_至多(&数据[起..], 限);
    }
    if 编码.contains("deflate") {
        // 规范说 zlib 包装，但现实中不少服务器直接发裸 deflate —— 两种都试
        return match crate::inflate::inflate_raw_至多(&数据[2..], 限) {
            Ok(v) => Ok(v),
            Err(_) => crate::inflate::inflate_raw_至多(&数据, 限),
        };
    }
    Ok(数据)
}

// ---------------------------------------------------------------------------
// 收响应：内存 or 边读边写
// ---------------------------------------------------------------------------

/// 响应体的去处。**决定在拿到响应头之后**（要看 `Content-Encoding` 才知道能不能流式）。
pub(crate) struct 收集器 {
    内存: Option<Vec<u8>>,
    文件: Option<std::fs::File>,
    临时: Option<std::path::PathBuf>,
    最终: Option<std::path::PathBuf>,
    上限: usize,
    已写: usize,
}

impl 收集器 {
    fn 新建(opt: &选项, headers: &[(String, String)]) -> Result<Self, String> {
        let 编码 = 头值(headers, "content-encoding").unwrap_or("").to_ascii_lowercase();
        let 压缩 = 编码.contains("gzip") || 编码.contains("deflate");
        let 限 = if opt.上限 == 0 { usize::MAX } else { opt.上限 };
        if let Some(路径) = &opt.落盘 {
            // 压缩数据没法边解边写（要整块 inflate）⇒ 只有没压缩时才真流式
            if 压缩 && opt.解压 {
                return Ok(收集器 {
                    内存: Some(Vec::new()),
                    文件: None,
                    临时: None,
                    最终: Some(std::path::PathBuf::from(路径)),
                    上限: 限,
                    已写: 0,
                });
            }
            let 临时 = std::path::PathBuf::from(format!("{路径}.part"));
            let f = std::fs::File::create(&临时)
                .map_err(|e| format!("写文件「{}」失败：{e}", 临时.display()))?;
            return Ok(收集器 {
                内存: None,
                文件: Some(f),
                临时: Some(临时),
                最终: Some(std::path::PathBuf::from(路径)),
                上限: 限,
                已写: 0,
            });
        }
        Ok(收集器 {
            内存: Some(Vec::new()),
            文件: None,
            临时: None,
            最终: None,
            上限: 限,
            已写: 0,
        })
    }

    pub(crate) fn 写(&mut self, 块: &[u8]) -> Result<(), String> {
        if self.已写 + 块.len() > self.上限 {
            return Err(format!(
                "响应太大（已经 {} 字节，上限 {} 字节）",
                self.已写 + 块.len(),
                self.上限
            ));
        }
        if let Some(v) = &mut self.内存 {
            v.extend_from_slice(块);
        } else if let Some(f) = &mut self.文件 {
            f.write_all(块).map_err(|e| format!("写文件失败：{e}"))?;
        }
        self.已写 += 块.len();
        Ok(())
    }

    /// 收尾：文件模式 flush + 改名（`.part` → 真名）。
    fn 完成(mut self) -> Result<(Vec<u8>, usize), String> {
        if let Some(f) = self.文件.take() {
            f.sync_all().map_err(|e| format!("写文件失败：{e}"))?;
            drop(f);
        }
        if let (Some(临), Some(终)) = (&self.临时, &self.最终) {
            std::fs::rename(临, 终).map_err(|e| format!("改名「{}」失败：{e}", 终.display()))?;
        }
        let 体 = self.内存.take().unwrap_or_default();
        Ok((体, self.已写))
    }

    /// 放弃（重定向 / 出错时）：删掉 `.part`，不留半截文件。
    fn 放弃(mut self) {
        drop(self.文件.take());
        if let Some(临) = &self.临时 {
            let _ = std::fs::remove_file(临);
        }
    }
}

// ---------------------------------------------------------------------------
// 公共流程：重定向 + 解压 + 落盘
// ---------------------------------------------------------------------------

/// 发一个请求（含重定向跟随、解压、落盘）。**4xx/5xx 不抛错** —— 那是调用方的判断。
pub(crate) fn 执行(raw: &str, opt: &选项) -> Result<Resp, JishiError> {
    let mut url = parse_url(raw)?;
    let mut method = opt.method.clone();
    let mut body = opt.body.clone();
    let mut 已跳 = 0u32;
    loop {
        let mut 本轮 = opt.clone();
        本轮.method = method.clone();
        本轮.body = body.clone();
        let mut r = client::一次(&url, &本轮)?;

        // ---- 重定向（自己跟：要拦跨协议降级、要数清次数）----
        let 状态 = r.status;
        if matches!(状态, 301 | 302 | 303 | 307 | 308) {
            if let Some(位置) = 头值(&r.headers, "location").map(|s| s.to_string()) {
                if 已跳 >= opt.重定向上限 {
                    return Err(err(
                        "运行期错误",
                        format!("{raw} 跳转超过 {} 次（可能是重定向环）", opt.重定向上限),
                    ));
                }
                let 下一站 = 解析相对(&url, &位置).map_err(|e| {
                    err("运行期错误", format!("{raw} 的跳转地址「{位置}」看不懂：{}", e.message))
                })?;
                if url.secure && !下一站.secure && !opt.允许降级 {
                    return Err(err(
                        "运行期错误",
                        format!(
                            "「{raw}」想从 https 跳到 http（{}）—— 明文降级不安全，已拦下",
                            下一站.文本()
                        ),
                    ));
                }
                // 303 与「301/302 + 非 GET」按浏览器惯例改成 GET（307/308 保持原样）
                if 状态 == 303 || ((状态 == 301 || 状态 == 302) && method != "GET" && method != "HEAD")
                {
                    method = "GET".to_string();
                    body = None;
                }
                已跳 += 1;
                url = 下一站;
                continue;
            }
        }

        // ---- 解压（内存模式；压缩 + 落盘时也是内存收完再写）----
        if opt.解压 && !r.body.is_empty() {
            if let Some(编码) = 头值(&r.headers, "content-encoding").map(|s| s.to_string()) {
                let 低 = 编码.to_ascii_lowercase();
                if 低.contains("gzip") || 低.contains("deflate") {
                    let 原长 = r.body.len();
                    r.body = 解压(std::mem::take(&mut r.body), &编码, opt.上限).map_err(|m| {
                        err(
                            "运行期错误",
                            format!("「{raw}」的响应解压失败：{m}（压缩数据 {原长} 字节）"),
                        )
                    })?;
                    r.字节数 = r.body.len();
                    if let Some(路径) = &opt.落盘 {
                        std::fs::write(路径, &r.body).map_err(|e| {
                            err("运行期错误", format!("写文件「{路径}」失败：{e}"))
                        })?;
                        r.body.clear();
                    }
                }
            }
        }
        return Ok(r);
    }
}

/// 把 `选项` 字典读出来（没给 = 全默认）。
fn 读选项(v: Option<&Val>) -> Result<选项, JishiError> {
    let mut o = 选项::新("GET");
    let d = match v {
        None | Some(Val::None_) => return Ok(o),
        Some(Val::Dict(d)) => d.borrow().to_items(),
        Some(_) => {
            return Err(err(
                "类型错误",
                "「选项」要传一个字典，例如 {\"大小上限\": 1048576, \"重定向上限\": 3}",
            ))
        }
    };
    for (k, val) in d {
        let 名 = crate::display(&k);
        match 名.as_str() {
            "大小上限" | "上限" => {
                let n = as_num(&val)?;
                if n < 0.0 {
                    return Err(err("值错误", "「大小上限」不能是负数（0 = 不限）"));
                }
                o.上限 = n as usize;
                o.上限已给 = true;
            }
            "重定向上限" => {
                let n = as_num(&val)?;
                if n < 0.0 {
                    return Err(err("值错误", "「重定向上限」不能是负数"));
                }
                o.重定向上限 = n as u32;
            }
            "允许降级" => o.允许降级 = crate::truthy(val),
            "代理" => {
                let s = as_text(&val);
                let 低 = s.trim().to_ascii_lowercase();
                // `无` / `none` / `直连` / `off` = **连环境变量都不看**（强制直连）。
                // 为什么需要：环境里有 `HTTP_PROXY` 时，判据/内网脚本常常要绕开它。
                if matches!(低.as_str(), "无" | "none" | "直连" | "off" | "不") {
                    o.代理 = None;
                    o.绕过环境代理 = true;
                } else {
                    o.代理 = if s.trim().is_empty() { None } else { Some(s) };
                }
            }
            "解压" => o.解压 = crate::truthy(val),
            其他 => {
                return Err(err(
                    "值错误",
                    format!("「选项」里没有「{其他}」这一项（认识：大小上限 / 重定向上限 / 允许降级 / 代理 / 解压）"),
                ))
            }
        }
    }
    Ok(o)
}

// ---------------------------------------------------------------------------
// Windows：WinHTTP
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod client {
    use super::{describe_code, 收集器, 选项, Resp, Url, AGENT};
    use crate::{err, JishiError};

    type Handle = *mut core::ffi::c_void;

    const ACCESS_TYPE_DEFAULT_PROXY: u32 = 0;
    const ACCESS_TYPE_NAMED_PROXY: u32 = 3;
    const FLAG_SECURE: u32 = 0x0080_0000;
    const FLAG_REFRESH: u32 = 0x0000_0100;
    /// 自己跟重定向：WinHTTP 的「关掉重定向」是**特性开关**
    /// （`WinHttpSetOption(OPTION_DISABLE_FEATURE, …)`），**不是** OpenRequest 的 flag。
    /// ⚠️ 2026-11-17 踩过：把它当 flag 传进 OpenRequest ⇒ 构造请求直接返回
    /// `ERROR_INVALID_PARAMETER(87)`（现象是「所有请求都失败」，看错误码才知道）。
    const OPTION_DISABLE_FEATURE: u32 = 9;
    const DISABLE_REDIRECTS: u32 = 0x2000_0000;
    const QUERY_STATUS_CODE: u32 = 19;
    const QUERY_FLAG_NUMBER: u32 = 0x2000_0000;
    const QUERY_RAW_HEADERS_CRLF: u32 = 22;
    const ERROR_INSUFFICIENT_BUFFER: u32 = 122;

    #[link(name = "winhttp")]
    extern "system" {
        fn WinHttpOpen(agent: *const u16, access_type: u32, proxy: *const u16,
                       bypass: *const u16, flags: u32) -> Handle;
        fn WinHttpConnect(session: Handle, host: *const u16, port: u16, reserved: u32) -> Handle;
        fn WinHttpOpenRequest(connect: Handle, verb: *const u16, object: *const u16,
                              version: *const u16, referrer: *const u16,
                              accept_types: *const *const u16, flags: u32) -> Handle;
        fn WinHttpSendRequest(request: Handle, headers: *const u16, headers_len: u32,
                              optional: *const core::ffi::c_void, optional_len: u32,
                              total_len: u32, context: usize) -> i32;
        fn WinHttpReceiveResponse(request: Handle, reserved: *mut core::ffi::c_void) -> i32;
        fn WinHttpQueryHeaders(request: Handle, info_level: u32, name: *const u16,
                               buffer: *mut core::ffi::c_void, buffer_len: *mut u32,
                               index: *mut u32) -> i32;
        fn WinHttpQueryDataAvailable(request: Handle, available: *mut u32) -> i32;
        fn WinHttpReadData(request: Handle, buffer: *mut core::ffi::c_void,
                           to_read: u32, read: *mut u32) -> i32;
        fn WinHttpCloseHandle(handle: Handle) -> i32;
        fn WinHttpSetTimeouts(handle: Handle, resolve: i32, connect: i32, send: i32,
                              receive: i32) -> i32;
        fn WinHttpSetOption(handle: Handle, option: u32, buffer: *mut core::ffi::c_void,
                            len: u32) -> i32;
        fn GetLastError() -> u32;
    }

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    struct Guard(Handle);
    impl Drop for Guard {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe { WinHttpCloseHandle(self.0) };
            }
        }
    }

    fn last_error() -> String {
        describe_code(unsafe { GetLastError() })
    }

    /// 把 `http://127.0.0.1:7890` 这种代理地址拆成 WinHTTP 要的 `host:port`。
    fn 代理串(s: &str) -> String {
        let s = s.trim();
        let 去协议 = s
            .strip_prefix("http://")
            .or_else(|| s.strip_prefix("https://"))
            .unwrap_or(s);
        去协议.split('/').next().unwrap_or(去协议).to_string()
    }

    /// 发**一次**请求（不跟重定向，`执行` 自己跟）。
    pub fn 一次(url: &Url, opt: &选项) -> Result<Resp, JishiError> {
        let ms = (opt.timeout * 1000.0) as i32;
        let agent = wide(AGENT);
        let 用代理 = opt.代理.as_ref().filter(|_| super::走代理(&url.host));
        let (模式, 代理宽) = match 用代理 {
            Some(p) => (ACCESS_TYPE_NAMED_PROXY, Some(wide(&代理串(p)))),
            None => (ACCESS_TYPE_DEFAULT_PROXY, None),
        };
        // ⚠️ **不传 bypass 串**：`NO_PROXY` 我们已经在上面按主机判过了
        //    （`走代理`）。给 WinHTTP 再传一份反而会踩 `ERROR_INVALID_PARAMETER(87)`
        //    —— 2026-11-17 实测：带 `::1` 这类 IPv6 条目时它直接不认。
        let session = unsafe {
            WinHttpOpen(
                agent.as_ptr(),
                模式,
                代理宽.as_ref().map(|v| v.as_ptr()).unwrap_or(std::ptr::null()),
                std::ptr::null(),
                0,
            )
        };
        if session.is_null() {
            return Err(err("运行期错误", format!("初始化 HTTP 会话失败：{}", last_error())));
        }
        let session = Guard(session);
        unsafe { WinHttpSetTimeouts(session.0, ms, ms, ms, ms) };

        let host = wide(&url.host);
        let connect = unsafe { WinHttpConnect(session.0, host.as_ptr(), url.port, 0) };
        if connect.is_null() {
            return Err(err("运行期错误", format!("连接「{}」失败：{}", url.host, last_error())));
        }
        let connect = Guard(connect);

        let verb = wide(&opt.method);
        let object = wide(&url.path);
        let flags = FLAG_REFRESH | if url.secure { FLAG_SECURE } else { 0 };
        let request = unsafe {
            WinHttpOpenRequest(connect.0, verb.as_ptr(), object.as_ptr(),
                               std::ptr::null(), std::ptr::null(),
                               std::ptr::null(), flags)
        };
        if request.is_null() {
            return Err(err("运行期错误", format!("构造请求失败：{}", last_error())));
        }
        let request = Guard(request);
        // 重定向自己跟（要拦跨协议降级、要数清次数）
        let mut 关跳 = DISABLE_REDIRECTS;
        unsafe {
            WinHttpSetOption(request.0, OPTION_DISABLE_FEATURE,
                             &mut 关跳 as *mut u32 as *mut core::ffi::c_void, 4);
        }

        let 头串 = build_headers(&opt.headers);
        let 头宽 = 头串.as_ref().map(|s| wide(s));
        let (hptr, hlen) = match &头宽 {
            Some(h) => (h.as_ptr(), u32::MAX),
            None => (std::ptr::null(), 0),
        };
        let (bptr, blen) = match &opt.body {
            None => (std::ptr::null(), 0u32),
            Some(b) => (b.as_ptr() as *const core::ffi::c_void, b.len() as u32),
        };
        let ok = unsafe { WinHttpSendRequest(request.0, hptr, hlen, bptr, blen, blen, 0) };
        if ok == 0 {
            return Err(err("运行期错误", format!("发请求失败：{}", last_error())));
        }
        let ok = unsafe { WinHttpReceiveResponse(request.0, std::ptr::null_mut()) };
        if ok == 0 {
            return Err(err("运行期错误", format!("收响应失败：{}", last_error())));
        }

        let mut status: u32 = 0;
        let mut size = std::mem::size_of::<u32>() as u32;
        let ok = unsafe {
            WinHttpQueryHeaders(request.0, QUERY_STATUS_CODE | QUERY_FLAG_NUMBER,
                                std::ptr::null(), &mut status as *mut u32 as *mut core::ffi::c_void,
                                &mut size, std::ptr::null_mut())
        };
        if ok == 0 {
            return Err(err("运行期错误", format!("读状态码失败：{}", last_error())));
        }

        let headers = query_headers(request.0)?;
        let 名 = url.文本();
        let mut 收 = 收集器::新建(opt, &headers)
            .map_err(|m| err("运行期错误", format!("「{名}」{m}")))?;

        loop {
            let mut avail: u32 = 0;
            let ok = unsafe { WinHttpQueryDataAvailable(request.0, &mut avail) };
            if ok == 0 {
                return Err(err("运行期错误", format!("读响应失败：{}", last_error())));
            }
            if avail == 0 {
                break;
            }
            let mut buf = vec![0u8; avail.min(256 * 1024) as usize];
            let mut got: u32 = 0;
            let ok = unsafe {
                WinHttpReadData(request.0, buf.as_mut_ptr() as *mut core::ffi::c_void,
                                buf.len() as u32, &mut got)
            };
            if ok == 0 {
                return Err(err("运行期错误", format!("读响应失败：{}", last_error())));
            }
            if got == 0 {
                break;
            }
            buf.truncate(got as usize);
            收.写(&buf).map_err(|m| err("运行期错误", format!("「{名}」{m}")))?;
        }

        let 是跳转 = matches!(status, 301 | 302 | 303 | 307 | 308);
        if 是跳转 || !(200..300).contains(&status) {
            // 非 2xx 不落盘（不然 404 的正文会盖掉用户要的文件）
            let (体, 字节) = if opt.落盘.is_some() {
                收.放弃();
                (Vec::new(), 0)
            } else {
                收.完成().map_err(|m| err("运行期错误", m))?
            };
            return Ok(Resp { status, headers, body: 体, 字节数: 字节 });
        }
        let (body, 字节数) = 收.完成().map_err(|m| err("运行期错误", m))?;
        Ok(Resp { status, headers, body, 字节数 })
    }

    fn build_headers(headers: &[(String, String)]) -> Option<String> {
        if headers.is_empty() {
            return None;
        }
        let mut s = String::new();
        for (k, v) in headers {
            s.push_str(k);
            s.push_str(": ");
            s.push_str(v);
            s.push_str("\r\n");
        }
        Some(s)
    }

    fn query_headers(request: Handle) -> Result<Vec<(String, String)>, JishiError> {
        // 先问要多大的缓冲
        let mut len: u32 = 0;
        let ok = unsafe {
            WinHttpQueryHeaders(request, QUERY_RAW_HEADERS_CRLF, std::ptr::null(),
                                std::ptr::null_mut(), &mut len, std::ptr::null_mut())
        };
        if ok == 0 {
            let code = unsafe { GetLastError() };
            if code != ERROR_INSUFFICIENT_BUFFER {
                return Err(err("运行期错误", format!("读响应头失败：{}", describe_code(code))));
            }
        }
        // len 是「wchar 个数」（含结尾空），再补一块余量
        let cap = (len as usize + 1).max(1);
        let mut buf: Vec<u16> = vec![0; cap];
        let mut len2 = len + 1;
        let ok = unsafe {
            WinHttpQueryHeaders(request, QUERY_RAW_HEADERS_CRLF, std::ptr::null(),
                                buf.as_mut_ptr() as *mut core::ffi::c_void, &mut len2,
                                std::ptr::null_mut())
        };
        if ok == 0 {
            return Err(err("运行期错误", format!("读响应头失败：{}", last_error())));
        }
        let text = String::from_utf16_lossy(&buf[..len2 as usize]);
        Ok(super::parse_header_text(&text))
    }
}

/// WinHTTP 的常见错误码 → 中文（不认识的给原始码，别瞎猜）。
#[allow(dead_code)]
fn describe_code(code: u32) -> String {
    match code {
        12002 => "请求超时".to_string(),
        12007 => "域名解析不了".to_string(),
        12029 => "连不上服务器".to_string(),
        12030 | 12031 => "连接被服务器断开了".to_string(),
        12175 => "证书校验没过（或服务器不支持要求的协议版本）".to_string(),
        12152 | 12156 => "服务器返回的内容不完整".to_string(),
        other => format!("WinHTTP 错误码 {other}"),
    }
}

/// 把「CRLF 分隔的原始响应头」解析成 `(名字, 值)` 列表（第一行状态行丢弃）。
fn parse_header_text(text: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for line in text.split("\r\n") {
        if let Some((k, v)) = line.split_once(':') {
            out.push((k.trim().to_string(), v.trim().to_string()));
        }
        // 没有冒号的行（状态行 / 空行）跳过
    }
    out
}

// ---------------------------------------------------------------------------
// 其它平台：裸 TCP（+ dlopen 系统 OpenSSL 做 TLS）
// ---------------------------------------------------------------------------

/// gzip 文件头的长度（含魔数 + 可选字段）。不是 gzip 就给 `None`。
fn gzip体起点(d: &[u8]) -> Option<usize> {
    if d.len() < 10 || d[0] != 0x1f || d[1] != 0x8b || d[2] != 8 {
        return None;
    }
    let 旗标 = d[3];
    let mut i = 10usize;
    if 旗标 & 0x04 != 0 {
        // FEXTRA：2 字节长度 + 内容
        if i + 2 > d.len() {
            return None;
        }
        let 长 = u16::from_le_bytes([d[i], d[i + 1]]) as usize;
        i += 2 + 长;
    }
    if 旗标 & 0x08 != 0 {
        // FNAME：以 NUL 结尾
        while i < d.len() && d[i] != 0 {
            i += 1;
        }
        i += 1;
    }
    if 旗标 & 0x10 != 0 {
        // FCOMMENT：以 NUL 结尾
        while i < d.len() && d[i] != 0 {
            i += 1;
        }
        i += 1;
    }
    if 旗标 & 0x02 != 0 {
        i += 2; // FHCRC
    }
    if i >= d.len() {
        None
    } else {
        Some(i)
    }
}

#[cfg(all(not(windows), not(unix)))]
mod client {
    //! wasm 这类「既不是 Windows 也不是 unix」的目标：**没有真的网络**。
    //!
    //! 以前这里会编 unix 那份（`cfg(not(windows))` 对 wasm32 也成立），
    //! 而它要 `std::os::unix::io::AsRawFd` —— wasm 上根本没有这个模块，**编不过**。
    //! M206 加 TLS 时才发现这条缝（`tools/build_wasm.py` 当场红）。
    use super::{选项, Resp, Url};
    use crate::{err, JishiError};

    pub fn 一次(_url: &Url, _opt: &选项) -> Result<Resp, JishiError> {
        Err(err(
            "运行期错误",
            "这个平台（wasm）没有网络能力 —— `网络` 只能在命令行宿主里用",
        ))
    }
}

#[cfg(all(not(windows), unix))]
mod client {
    use super::{收集器, 选项, Resp, Url, AGENT};
    use crate::{err, JishiError};
    use std::io::{Read, Write};
    use std::net::{TcpStream, ToSocketAddrs};
    use std::time::Duration;

    /// 连接：明文或 TLS（两者都实现 `Read`/`Write`，上层只当它是字节流）。
    enum 流 {
        明文(TcpStream),
        Tls(super::tls::连接),
    }

    impl Read for 流 {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            match self {
                流::明文(s) => s.read(buf),
                流::Tls(t) => t.read(buf),
            }
        }
    }

    impl Write for 流 {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            match self {
                流::明文(s) => s.write(buf),
                流::Tls(t) => t.write(buf),
            }
        }
        fn flush(&mut self) -> std::io::Result<()> {
            match self {
                流::明文(s) => s.flush(),
                流::Tls(t) => t.flush(),
            }
        }
    }

    /// 代理地址拆成 `(主机, 端口)`；`http://` 前缀可有可无。
    fn 代理地址(s: &str) -> Result<(String, u16), String> {
        let s = s.trim();
        let s = s
            .strip_prefix("http://")
            .or_else(|| s.strip_prefix("https://"))
            .unwrap_or(s);
        let s = s.split('/').next().unwrap_or(s);
        match s.rsplit_once(':') {
            Some((h, p)) => p
                .parse::<u16>()
                .map(|port| (h.to_string(), port))
                .map_err(|_| format!("代理地址「{s}」里的端口看不懂")),
            None => Ok((s.to_string(), 8080)),
        }
    }

    pub fn 一次(url: &Url, opt: &选项) -> Result<Resp, JishiError> {
        let dur = Duration::from_secs_f64(opt.timeout.max(0.1));
        // ---- 连：直连 or 经代理 ----
        let 代理 = opt.代理.as_ref().filter(|_| super::走代理(&url.host));
        let (mut s, 走隧道) = match 代理 {
            Some(p) => {
                let (ph, pp) = 代理地址(p).map_err(|m| err("运行期错误", m))?;
                (连(&ph, pp, dur).map_err(|m| err("运行期错误", m))?, url.secure)
            }
            None => (连(&url.host, url.port, dur).map_err(|m| err("运行期错误", m))?, false),
        };

        if 走隧道 {
            // https 经代理：先 CONNECT 打个洞，再在洞里做 TLS
            let 目标 = format!("{}:{}", url.host, url.port);
            写全部(&mut s, format!("CONNECT {目标} HTTP/1.1\r\nHost: {目标}\r\n\r\n").as_bytes())?;
            let (状态, _头, 剩余) = 读头(&mut s)?;
            if !(200..300).contains(&状态) {
                return Err(err(
                    "运行期错误",
                    format!(
                        "代理「{}」不肯放行 {目标}（CONNECT 返回 {状态}）",
                        代理.cloned().unwrap_or_default()
                    ),
                ));
            }
            if !剩余.is_empty() {
                return Err(err("运行期错误", "代理在 CONNECT 之后多发了数据（不支持）"));
            }
        }

        // ---- TLS ----
        let mut 流 = if url.secure {
            流::Tls(super::tls::连接::建(&s, &url.host, dur).map_err(|m| err("运行期错误", m))?)
        } else {
            流::明文(s)
        };

        // ---- 请求 ----
        let mut req = format!(
            "{} {} HTTP/1.1\r\nHost: {}\r\nUser-Agent: {AGENT}\r\nAccept: */*\r\nConnection: close\r\n",
            opt.method, url.path, url.主机头()
        );
        let mut 有编码头 = false;
        let mut 有长度头 = false;
        for (k, v) in &opt.headers {
            let 低 = k.to_ascii_lowercase();
            if 低 == "accept-encoding" {
                有编码头 = true;
            }
            if 低 == "content-length" {
                有长度头 = true;
            }
            req.push_str(k);
            req.push_str(": ");
            req.push_str(v);
            req.push_str("\r\n");
        }
        if !有编码头 {
            req.push_str("Accept-Encoding: gzip, deflate\r\n");
        }
        if let Some(b) = &opt.body {
            if !有长度头 {
                req.push_str(&format!("Content-Length: {}\r\n", b.len()));
            }
        }
        req.push_str("\r\n");
        写全部(&mut 流, req.as_bytes())?;
        if let Some(b) = &opt.body {
            写全部(&mut 流, b)?;
        }

        // ---- 响应头 ----
        let (status, headers, 余) = 读头(&mut 流)?;
        let 名 = url.文本();
        let mut 收 = 收集器::新建(opt, &headers)
            .map_err(|m| err("运行期错误", format!("「{名}」{m}")))?;

        // ---- 响应体：chunked / 定长 / 读到关闭 ----
        let 分块 = 头值(&headers, "transfer-encoding")
            .map(|v| v.to_ascii_lowercase().contains("chunked"))
            .unwrap_or(false);
        if 分块 {
            // 🔴 `余` 是「读头时多读进来的字节」，它属于**分块流**，得交给分块器
            //    —— 不能当正文写进收集器（那会把 chunk 头混进正文；CI 上
            //    example.com 的 gzip 正文就是这样变成「gzip 头不合法」的）。
            let mut 解析 = 分块器::新建(流, 余);
            loop {
                match 解析
                    .下一块()
                    .map_err(|m| err("运行期错误", format!("「{名}」{m}")))?
                {
                    Some(块) => 收
                        .写(&块)
                        .map_err(|m| err("运行期错误", format!("「{名}」{m}")))?,
                    None => break,
                }
            }
        } else if let Some(n) = 头值(&headers, "content-length")
            .and_then(|s| s.trim().parse::<usize>().ok())
        {
            if !余.is_empty() {
                收.写(&余).map_err(|m| err("运行期错误", format!("「{名}」{m}")))?;
            }
            let mut 剩 = n.saturating_sub(收.已写);
            let mut buf = [0u8; 64 * 1024];
            while 剩 > 0 {
                let 要 = buf.len().min(剩);
                let k = 流
                    .read(&mut buf[..要])
                    .map_err(|e| err("运行期错误", format!("收响应失败：{e}")))?;
                if k == 0 {
                    break;
                }
                收.写(&buf[..k])
                    .map_err(|m| err("运行期错误", format!("「{名}」{m}")))?;
                剩 -= k;
            }
        } else {
            if !余.is_empty() {
                收.写(&余).map_err(|m| err("运行期错误", format!("「{名}」{m}")))?;
            }
            let mut buf = [0u8; 64 * 1024];
            loop {
                let k = 流
                    .read(&mut buf)
                    .map_err(|e| err("运行期错误", format!("收响应失败：{e}")))?;
                if k == 0 {
                    break;
                }
                收.写(&buf[..k])
                    .map_err(|m| err("运行期错误", format!("「{名}」{m}")))?;
            }
        }

        let 是跳转 = matches!(status, 301 | 302 | 303 | 307 | 308);
        if 是跳转 || !(200..300).contains(&status) {
            let (体, 字节) = if opt.落盘.is_some() {
                收.放弃();
                (Vec::new(), 0)
            } else {
                收.完成().map_err(|m| err("运行期错误", m))?
            };
            return Ok(Resp { status, headers, body: 体, 字节数: 字节 });
        }
        let (body, 字节数) = 收.完成().map_err(|m| err("运行期错误", m))?;
        Ok(Resp { status, headers, body, 字节数 })
    }

    fn 头值<'a>(headers: &'a [(String, String)], 名: &str) -> Option<&'a str> {
        headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(名))
            .map(|(_, v)| v.as_str())
    }

    fn 连(host: &str, port: u16, dur: Duration) -> Result<TcpStream, String> {
        let addr = format!("{host}:{port}");
        let 加 = addr
            .to_socket_addrs()
            .map_err(|e| format!("域名解析不了「{host}」：{e}"))?
            .next()
            .ok_or_else(|| format!("域名解析不了「{host}」"))?;
        let s = TcpStream::connect_timeout(&加, dur).map_err(|e| match e.kind() {
            std::io::ErrorKind::ConnectionRefused => {
                format!("连不上服务器「{host}:{port}」（对方拒绝）")
            }
            std::io::ErrorKind::TimedOut => format!("连接「{host}:{port}」超时"),
            _ => format!("连不上服务器「{host}:{port}」：{e}"),
        })?;
        let 长一档 = dur + Duration::from_secs(2);
        let _ = s.set_read_timeout(Some(长一档));
        let _ = s.set_write_timeout(Some(长一档));
        let _ = s.set_nodelay(true);
        Ok(s)
    }

    fn 写全部<W: Write>(w: &mut W, 数据: &[u8]) -> Result<(), JishiError> {
        w.write_all(数据)
            .map_err(|e| err("运行期错误", format!("发请求失败：{e}")))
    }

    /// 读到 `\r\n\r\n`，返回 `(状态码, 响应头, 多读进来的字节)`。
    fn 读头<R: Read>(r: &mut R) -> Result<(u32, Vec<(String, String)>, Vec<u8>), JishiError> {
        let mut 全部 = Vec::new();
        let mut buf = [0u8; 4096];
        loop {
            if let Some(k) = 全部.windows(4).position(|w| w == b"\r\n\r\n") {
                let head = String::from_utf8_lossy(&全部[..k]).to_string();
                let status: u32 = head
                    .split_whitespace()
                    .nth(1)
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
                return Ok((status, super::parse_header_text(&head), 全部[k + 4..].to_vec()));
            }
            let n = r
                .read(&mut buf)
                .map_err(|e| err("运行期错误", format!("收响应头失败：{e}")))?;
            if n == 0 {
                return Err(err("运行期错误", "服务器的响应格式不对（头没读完连接就断了）"));
            }
            全部.extend_from_slice(&buf[..n]);
            if 全部.len() > 256 * 1024 {
                return Err(err("运行期错误", "服务器的响应头超过 256 KB（不合规）"));
            }
        }
    }

    /// 分块传输（chunked）的增量解码器：一次吐一块，**不整读**（`下载` 才不会占内存）。
    struct 分块器<R: Read> {
        源: R,
        缓: Vec<u8>,
        完: bool,
    }

    impl<R: Read> 分块器<R> {
        /// `预读` 是「读响应头时多读进来的字节」—— 它已经是分块流的一部分。
        fn 新建(源: R, 预读: Vec<u8>) -> Self {
            分块器 { 源, 缓: 预读, 完: false }
        }

        fn 填(&mut self) -> std::io::Result<bool> {
            let mut buf = [0u8; 64 * 1024];
            let n = self.源.read(&mut buf)?;
            if n > 0 {
                self.缓.extend_from_slice(&buf[..n]);
            }
            Ok(n > 0)
        }

        fn 下一块(&mut self) -> Result<Option<Vec<u8>>, String> {
            if self.完 {
                return Ok(None);
            }
            loop {
                if let Some(k) = self.缓.windows(2).position(|w| w == b"\r\n") {
                    let 行 = String::from_utf8_lossy(&self.缓[..k]).to_string();
                    let 长度文 = 行.split(';').next().unwrap_or("").trim().to_string();
                    let 长 = usize::from_str_radix(&长度文, 16)
                        .map_err(|_| format!("分块长度「{长度文}」不像十六进制"))?;
                    self.缓.drain(..k + 2);
                    if 长 == 0 {
                        self.完 = true;
                        return Ok(None);
                    }
                    while self.缓.len() < 长 + 2 {
                        if !self.填().map_err(|e| format!("收响应失败：{e}"))? {
                            return Err("分块数据没读完连接就断了".to_string());
                        }
                    }
                    let 块 = self.缓[..长].to_vec();
                    self.缓.drain(..长 + 2);
                    return Ok(Some(块));
                }
                if !self.填().map_err(|e| format!("收响应失败：{e}"))? {
                    return Err("分块长度行没读完连接就断了".to_string());
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// TLS（非 Windows）：dlopen 系统自带的 OpenSSL —— 不引 crate、不写加密
// ---------------------------------------------------------------------------

#[cfg(all(not(windows), unix))]
mod tls {
    //! 用**系统自带的 libssl** 做 TLS 客户端。
    //!
    //! 为什么是 dlopen 而不是链接：Linux 上发行版给的版本五花八门
    //! （`libssl.so.3` / `.so.1.1`），macOS 上还有 Homebrew 与系统 LibreSSL 两套。
    //! 链接（`#[link(name="ssl")]`）在「版本对不上」时是**加载期就崩**；
    //! dlopen 则是「找不到就报一句人话，并告诉用户装什么」。
    //!
    //! 我们只做三件事：**设 SNI、设主机名校验、看校验结果**。
    //! 真正的加密、握手、证书链校验全在 libssl 里 —— 那是它的本职。

    use std::ffi::{c_char, c_int, c_void, CStr, CString};
    use std::io::{Read, Write};
    use std::net::TcpStream;
    use std::os::unix::io::AsRawFd;
    use std::sync::OnceLock;
    use std::time::Duration;

    const RTLD_NOW: c_int = 2;
    const SSL_VERIFY_PEER: c_int = 1;
    const SSL_CTRL_SET_TLSEXT_HOSTNAME: c_int = 55;
    const TLSEXT_NAMETYPE_HOST_NAME: i64 = 0;
    const X509_V_OK: i64 = 0;
    const SSL_ERROR_ZERO_RETURN: c_int = 6;
    const SSL_ERROR_WANT_READ: c_int = 2;
    const SSL_ERROR_WANT_WRITE: c_int = 3;
    const SSL_ERROR_SYSCALL: c_int = 5;

    extern "C" {
        fn dlopen(file: *const c_char, mode: c_int) -> *mut c_void;
        fn dlsym(handle: *mut c_void, name: *const c_char) -> *mut c_void;
        fn dlerror() -> *mut c_char;
    }

    unsafe fn 取(库: *mut c_void, 名: &str) -> Option<*mut c_void> {
        let c = CString::new(名).ok()?;
        let p = dlsym(库, c.as_ptr());
        if p.is_null() {
            None
        } else {
            Some(p)
        }
    }

    struct 函数表 {
        tls_client_method: unsafe extern "C" fn() -> *const c_void,
        ssl_ctx_new: unsafe extern "C" fn(*const c_void) -> *mut c_void,
        ssl_ctx_free: unsafe extern "C" fn(*mut c_void),
        ssl_ctx_set_verify: unsafe extern "C" fn(*mut c_void, c_int, *const c_void),
        ssl_ctx_set_default_verify_paths: unsafe extern "C" fn(*mut c_void) -> c_int,
        /// 可选：`SSL_CTX_load_verify_locations`（`JISHI_TLS_CA` 指一份 PEM）
        ssl_ctx_load_verify_locations:
            Option<unsafe extern "C" fn(*mut c_void, *const c_char, *const c_char) -> c_int>,
        ssl_new: unsafe extern "C" fn(*mut c_void) -> *mut c_void,
        ssl_free: unsafe extern "C" fn(*mut c_void),
        ssl_set_fd: unsafe extern "C" fn(*mut c_void, c_int) -> c_int,
        ssl_ctrl: unsafe extern "C" fn(*mut c_void, c_int, i64, *mut c_void) -> i64,
        ssl_connect: unsafe extern "C" fn(*mut c_void) -> c_int,
        ssl_read: unsafe extern "C" fn(*mut c_void, *mut c_void, c_int) -> c_int,
        ssl_write: unsafe extern "C" fn(*mut c_void, *const c_void, c_int) -> c_int,
        ssl_get_error: unsafe extern "C" fn(*mut c_void, c_int) -> c_int,
        ssl_get_verify_result: unsafe extern "C" fn(*const c_void) -> i64,
        ssl_shutdown: unsafe extern "C" fn(*mut c_void) -> c_int,
        /// 可选：老一点的 LibreSSL 没有 `SSL_set1_host`
        ssl_set1_host: Option<unsafe extern "C" fn(*mut c_void, *const c_char) -> c_int>,
        /// 可选：`SSL_get0_param` + `X509_VERIFY_PARAM_set1_host`（上面那个的替代路）
        ssl_get0_param: Option<unsafe extern "C" fn(*mut c_void) -> *mut c_void>,
        param_set1_host: Option<unsafe extern "C" fn(*mut c_void, *const c_char) -> c_int>,
        /// 可选：`ERR_error_string_n`（错误码 → 人话）
        err_error_string_n: Option<unsafe extern "C" fn(u64, *mut c_char, usize)>,
    }

    // 函数指针表在进程生命周期内不变（只读），跨线程共享安全
    unsafe impl Send for 函数表 {}
    unsafe impl Sync for 函数表 {}

    static 函数表缓存: OnceLock<Result<函数表, String>> = OnceLock::new();

    /// 依次尝试的动态库名（Linux 与 macOS 都列上，代码只写一份）。
    const LIBSSL: &[&str] = &[
        "libssl.so.3",
        "libssl.so.1.1",
        "libssl.so",
        "libssl.3.dylib",
        "/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib",
        "/usr/local/opt/openssl@3/lib/libssl.3.dylib",
        "libssl.dylib",
        "/usr/lib/libssl.dylib",
    ];
    const LIBCRYPTO: &[&str] = &[
        "libcrypto.so.3",
        "libcrypto.so.1.1",
        "libcrypto.so",
        "libcrypto.3.dylib",
        "/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib",
        "/usr/local/opt/openssl@3/lib/libcrypto.3.dylib",
        "libcrypto.dylib",
        "/usr/lib/libcrypto.dylib",
    ];

    fn 打开(候选: &[&str]) -> Result<*mut c_void, String> {
        let mut 最后 = String::from("没找到");
        for 名 in 候选 {
            let Ok(c) = CString::new(*名) else { continue };
            let h = unsafe { dlopen(c.as_ptr(), RTLD_NOW) };
            if !h.is_null() {
                return Ok(h);
            }
            unsafe {
                let e = dlerror();
                if !e.is_null() {
                    最后 = CStr::from_ptr(e).to_string_lossy().to_string();
                }
            }
        }
        Err(最后)
    }

    fn 加载() -> Result<&'static 函数表, String> {
        match 函数表缓存.get_or_init(真的加载) {
            Ok(t) => Ok(t),
            Err(m) => Err(format!(
                "这个平台上没有可用的 TLS 库（libssl）—— `https://` 用不了：{m}\n\
                 装一个即可：Debian/Ubuntu `apt install libssl3`；Fedora `dnf install openssl-libs`；\
                 macOS 自带（或 `brew install openssl@3`）。也可以改用 `http://`"
            )),
        }
    }

    fn 真的加载() -> Result<函数表, String> {
        let lib = 打开(LIBSSL).map_err(|e| format!("找不到 libssl（{e}）"))?;
        let 密 = 打开(LIBCRYPTO).ok();
        unsafe {
            macro_rules! 必需 {
                ($名:literal) => {{
                    let p = 取(lib, $名)
                        .ok_or_else(|| format!("libssl 里没有 {}（版本太老？）", $名))?;
                    std::mem::transmute(p)
                }};
            }
            Ok(函数表 {
                tls_client_method: 必需!("TLS_client_method"),
                ssl_ctx_new: 必需!("SSL_CTX_new"),
                ssl_ctx_free: 必需!("SSL_CTX_free"),
                ssl_ctx_set_verify: 必需!("SSL_CTX_set_verify"),
                ssl_ctx_set_default_verify_paths: 必需!("SSL_CTX_set_default_verify_paths"),
                ssl_ctx_load_verify_locations: 取(lib, "SSL_CTX_load_verify_locations")
                    .map(|p| std::mem::transmute(p)),
                ssl_new: 必需!("SSL_new"),
                ssl_free: 必需!("SSL_free"),
                ssl_set_fd: 必需!("SSL_set_fd"),
                ssl_ctrl: 必需!("SSL_ctrl"),
                ssl_connect: 必需!("SSL_connect"),
                ssl_read: 必需!("SSL_read"),
                ssl_write: 必需!("SSL_write"),
                ssl_get_error: 必需!("SSL_get_error"),
                ssl_get_verify_result: 必需!("SSL_get_verify_result"),
                ssl_shutdown: 必需!("SSL_shutdown"),
                ssl_set1_host: 取(lib, "SSL_set1_host").map(|p| std::mem::transmute(p)),
                ssl_get0_param: 取(lib, "SSL_get0_param").map(|p| std::mem::transmute(p)),
                param_set1_host: 密
                    .and_then(|h| 取(h, "X509_VERIFY_PARAM_set1_host"))
                    .map(|p| std::mem::transmute(p)),
                err_error_string_n: 密
                    .and_then(|h| 取(h, "ERR_error_string_n"))
                    .map(|p| std::mem::transmute(p)),
            })
        }
    }

    fn 错误串(表: &函数表) -> String {
        match 表.err_error_string_n {
            None => String::new(),
            Some(f) => {
                let mut buf = [0 as c_char; 256];
                unsafe { f(0, buf.as_mut_ptr(), buf.len()) };
                let s = unsafe { CStr::from_ptr(buf.as_ptr()) }.to_string_lossy().to_string();
                if s.is_empty() {
                    String::new()
                } else {
                    format!("：{s}")
                }
            }
        }
    }

    pub(super) struct 连接 {
        表: &'static 函数表,
        ssl: *mut c_void,
    }

    impl 连接 {
        pub(super) fn 建(流: &TcpStream, 主机: &str, 超时: Duration) -> Result<Self, String> {
            let 表 = 加载()?;
            let 方法 = unsafe { (表.tls_client_method)() };
            if 方法.is_null() {
                return Err("libssl 拿不到 TLS_client_method".to_string());
            }
            let ctx = unsafe { (表.ssl_ctx_new)(方法) };
            if ctx.is_null() {
                return Err("libssl 建不了 TLS 上下文".to_string());
            }
            // 校验对端证书（默认就是 PEER，但显式设一遍 —— 这行是安全底线，不靠默认值）
            unsafe {
                (表.ssl_ctx_set_verify)(ctx, SSL_VERIFY_PEER, std::ptr::null());
                (表.ssl_ctx_set_default_verify_paths)(ctx);
            }
            // 自定义 CA（企业自签 / 判据用）：`JISHI_TLS_CA=<PEM 路径>`
            if let Ok(路径) = std::env::var("JISHI_TLS_CA") {
                let 路径 = 路径.trim().to_string();
                if !路径.is_empty() {
                    let Ok(c) = CString::new(路径.clone()) else {
                        unsafe { (表.ssl_ctx_free)(ctx) };
                        return Err("JISHI_TLS_CA 的路径里有 NUL".to_string());
                    };
                    let ok = match 表.ssl_ctx_load_verify_locations {
                        Some(f) => (unsafe { f(ctx, c.as_ptr(), std::ptr::null()) }) == 1,
                        None => false,
                    };
                    if !ok {
                        unsafe { (表.ssl_ctx_free)(ctx) };
                        return Err(format!(
                            "读不了 JISHI_TLS_CA 指的那份 CA「{路径}」（这个 libssl 也不支持自定 CA？）"
                        ));
                    }
                }
            }
            let ssl = unsafe { (表.ssl_new)(ctx) };
            // SSL_new 已经给 ctx +1 引用 ⇒ 这里可以放掉自己那份
            unsafe { (表.ssl_ctx_free)(ctx) };
            if ssl.is_null() {
                return Err("libssl 建不了 TLS 连接对象".to_string());
            }

            let Ok(c) = CString::new(主机) else {
                unsafe { (表.ssl_free)(ssl) };
                return Err(format!("主机名「{主机}」里有 NUL，用不了 TLS"));
            };
            unsafe {
                if (表.ssl_set_fd)(ssl, 流.as_raw_fd()) != 1 {
                    (表.ssl_free)(ssl);
                    return Err(format!("把连接交给 TLS 失败（{主机}）"));
                }
                // SNI：虚拟主机必需，不然服务器给的是一张默认证书
                (表.ssl_ctrl)(
                    ssl,
                    SSL_CTRL_SET_TLSEXT_HOSTNAME,
                    TLSEXT_NAMETYPE_HOST_NAME,
                    c.as_ptr() as *mut c_void,
                );
            }
            // 主机名校验：优先 SSL_set1_host，老 LibreSSL 走 X509_VERIFY_PARAM_set1_host
            let 设了 = unsafe {
                match (表.ssl_set1_host, 表.ssl_get0_param, 表.param_set1_host) {
                    (Some(f), _, _) => f(ssl, c.as_ptr()) == 1,
                    (None, Some(取参), Some(设)) => 设(取参(ssl), c.as_ptr()) == 1,
                    _ => false,
                }
            };
            if !设了 {
                unsafe { (表.ssl_free)(ssl) };
                return Err("这个 libssl 不支持主机名校验（太老了）—— 拒绝在不校验证书的情况下连 https"
                    .to_string());
            }

            // 超时：SSL_connect 是阻塞的，靠 socket 的读写超时兜
            let 兜 = 超时 + Duration::from_secs(2);
            let _ = 流.set_read_timeout(Some(兜));
            let _ = 流.set_write_timeout(Some(兜));

            let 连 = 连接 { 表, ssl };
            let rc = unsafe { (连.表.ssl_connect)(连.ssl) };
            if rc != 1 {
                let 码 = unsafe { (连.表.ssl_get_error)(连.ssl, rc) };
                let 校验 = unsafe { (连.表.ssl_get_verify_result)(连.ssl) };
                let 详情 = 错误串(连.表);
                if 校验 != X509_V_OK {
                    return Err(format!("证书校验没过「{主机}」（X509 码 {校验}）{详情}"));
                }
                return Err(match 码 {
                    SSL_ERROR_SYSCALL => format!("TLS 握手中途断开（{主机}）{详情}"),
                    _ => format!("TLS 握手失败（{主机}，SSL 错误 {码}）{详情}"),
                });
            }
            Ok(连)
        }
    }

    impl Drop for 连接 {
        fn drop(&mut self) {
            unsafe {
                (self.表.ssl_shutdown)(self.ssl);
                (self.表.ssl_free)(self.ssl);
            }
        }
    }

    impl Read for 连接 {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            loop {
                let n = unsafe {
                    (self.表.ssl_read)(self.ssl, buf.as_mut_ptr() as *mut c_void, buf.len() as c_int)
                };
                if n > 0 {
                    return Ok(n as usize);
                }
                let 码 = unsafe { (self.表.ssl_get_error)(self.ssl, n) };
                match 码 {
                    SSL_ERROR_ZERO_RETURN => return Ok(0),
                    SSL_ERROR_WANT_READ | SSL_ERROR_WANT_WRITE => continue,
                    _ => {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::ConnectionAborted,
                            format!("TLS 读失败（SSL 错误 {码}）"),
                        ))
                    }
                }
            }
        }
    }

    impl Write for 连接 {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            let n = unsafe {
                (self.表.ssl_write)(self.ssl, buf.as_ptr() as *const c_void, buf.len() as c_int)
            };
            if n > 0 {
                return Ok(n as usize);
            }
            let 码 = unsafe { (self.表.ssl_get_error)(self.ssl, n) };
            Err(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                format!("TLS 写失败（SSL 错误 {码}）"),
            ))
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
}

// ---------------------------------------------------------------------------
// 模块
// ---------------------------------------------------------------------------

/// `网络.获取(网址)` 的实现体（可带超时 / 请求头）——`并发.并行取` 也走这里，
/// **口径必须一致**（正文取法、报错文案都同一句，M114）。
pub(crate) fn 取正文参数(
    raw: &str,
    t: f64,
    h: &[(String, String)],
) -> Result<String, JishiError> {
    let mut o = 选项::新("GET");
    o.timeout = t;
    o.headers = h.to_vec();
    let r = 执行(raw, &o)?;
    let r = 要成功(r, "访问", raw)?;
    Ok(String::from_utf8_lossy(&r.body).to_string())
}

/// `网络.获取(网址)` 的默认口径（10 秒超时、无额外请求头）。
pub(crate) fn 取正文(raw: &str) -> Result<String, JishiError> {
    取正文参数(raw, DEFAULT_TIMEOUT, &[])
}

fn timeout_of(args: &[Val], idx: usize) -> Result<f64, JishiError> {
    match args.get(idx) {
        Some(v) => Ok(as_num(v)?.max(0.1)),
        None => Ok(DEFAULT_TIMEOUT),
    }
}

/// 取一段网址的**原始字节** —— 包管理下载 zip 与自更新用（R7.6）。
///
/// ⚠️ 只认 `http://` / `https://`（与标准库 `网络.获取` 同一套）。`file://`
/// 由调用方自己处理（包管理的本地镜像场景，见 `packages.rs`）。
///
/// 上限给 512 MB：安装包本来就几十 MB，32 MB 会误伤；又不是「不限」。
pub(crate) fn fetch_bytes(url: &str, timeout: f64) -> Result<Vec<u8>, JishiError> {
    let mut o = 选项::新("GET");
    o.timeout = timeout;
    o.上限 = 512 * 1024 * 1024;
    let r = 执行(url, &o)?;
    Ok(r.body)
}

/// 请求头字典 → `(名字, 值)` 列表（ASCII 校验，`Content-Length`/`Host` 不让用户写）。
fn 请求头(v: Option<&Val>) -> Result<Vec<(String, String)>, JishiError> {
    let d = match v {
        None | Some(Val::None_) => return Ok(Vec::new()),
        Some(x) => x,
    };
    let pairs = match d {
        Val::Dict(m) => m.borrow().to_items(),
        _ => return Err(err("类型错误", "「请求头」要传一个字典，例如 {\"X-Token\": \"abc\"}")),
    };
    let mut out = Vec::new();
    for (k, val) in pairs {
        let 名 = crate::display(&k);
        if !名.chars().all(|c| (c as u32) <= 0xFF) {
            return Err(err("类型错误", format!("请求头**名字**「{名}」必须是 ASCII（英文数字）")));
        }
        let 值 = crate::display(&val);
        if !值.chars().all(|c| (c as u32) <= 0xFF) {
            return Err(err("类型错误", format!("请求头「{名}」的值里有中文，发不出去")));
        }
        // 🔴 头注入（M206）：值里带 CR/LF 就能塞进额外的头，甚至把请求劈成两半
        if 值.contains('\r') || 值.contains('\n') {
            return Err(err(
                "值错误",
                format!("请求头「{名}」的值里有换行 —— 那能把请求劈成两半（头注入）"),
            ));
        }
        if 名.contains('\r') || 名.contains('\n') || 名.contains(':') {
            return Err(err("值错误", format!("请求头名字「{名}」里有冒号或换行，不合法")));
        }
        let 低 = 名.to_ascii_lowercase();
        if 低 == "content-length" || 低 == "host" {
            continue;
        }
        out.push((名, 值));
    }
    Ok(out)
}

fn 要成功(r: Resp, 动作: &str, 网址: &str) -> Result<Resp, JishiError> {
    if (200..300).contains(&r.status) {
        return Ok(r);
    }
    Err(err("运行期错误", format!("{动作}「{网址}」失败：HTTP {}", r.status)))
}

/// 响应头列表 → 字典（同名头合并成 `a, b`）。
fn 头表(headers: &[(String, String)]) -> Val {
    let mut 出: Vec<(Val, Val)> = Vec::new();
    for (k, v) in headers {
        match 出.iter_mut().find(|(kk, _)| crate::display(kk) == *k) {
            Some((_, val)) => {
                // 🔴 M165b：`Val::Str` 里面是 `Rc<str>`（不可变），要拼接只能重造一个
                if let Val::Str(s) = val {
                    let 合 = format!("{s}, {v}");
                    *val = Val::Str(合.into());
                }
            }
            None => 出.push((Val::Str(k.clone().into()), Val::Str(v.clone().into()))),
        }
    }
    crate::dict_new(出)
}

/// 组装一次请求（方法 / 正文 / 超时 / 头 / 选项）→ 发 → 检查 2xx。
fn 发(
    raw: &str,
    方法: &str,
    体: Option<Vec<u8>>,
    超时: f64,
    头: Vec<(String, String)>,
    选项参数: Option<&Val>,
    动作: &str,
) -> Result<Resp, JishiError> {
    let mut o = 读选项(选项参数)?;
    o.method = 方法.to_string();
    o.body = 体;
    o.timeout = 超时;
    o.headers = 头;
    let r = 执行(raw, &o)?;
    要成功(r, 动作, raw)
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "网络";
        // `选项` 是最后一个参数（`获取`/`获取JSON`/`获取详情`/`下载` 是第 4 个，
        // `提交`/`提交JSON` 是第 5 个）：一个字典装全部护栏参数
        "获取" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("获取", args, 1, 4)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 1)?;
            let h = 请求头(args.get(2))?;
            let r = 发(&raw, "GET", None, t, h, args.get(3), "访问")?;
            Ok(Val::Str(String::from_utf8_lossy(&r.body).to_string().into()))
        },
        "提交" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("提交", args, 1, 5)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 2)?;
            let body = if args.len() > 1 && !matches!(args[1], Val::None_) {
                as_text(&args[1]).into_bytes()
            } else {
                Vec::new()
            };
            let mut h = 请求头(args.get(3))?;
            if !h.iter().any(|(k, _)| k.eq_ignore_ascii_case("content-type")) {
                h.push(("Content-Type".to_string(),
                        "application/x-www-form-urlencoded".to_string()));
            }
            let r = 发(&raw, "POST", Some(body), t, h, args.get(4), "提交到")?;
            Ok(Val::Str(String::from_utf8_lossy(&r.body).to_string().into()))
        },
        "提交JSON" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("提交JSON", args, 2, 5)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 2)?;
            let body = crate::stdlib::jsonmod::to_text(&args[1], None)?.into_bytes();
            let mut h = 请求头(args.get(3))?;
            if !h.iter().any(|(k, _)| k.eq_ignore_ascii_case("content-type")) {
                h.push(("Content-Type".to_string(),
                        "application/json; charset=utf-8".to_string()));
            }
            let r = 发(&raw, "POST", Some(body), t, h, args.get(4), "提交到")?;
            Ok(Val::Str(String::from_utf8_lossy(&r.body).to_string().into()))
        },
        "获取JSON" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("获取JSON", args, 1, 4)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 1)?;
            let h = 请求头(args.get(2))?;
            let r = 发(&raw, "GET", None, t, h, args.get(3), "访问")?;
            let text = String::from_utf8_lossy(&r.body).to_string();
            crate::stdlib::jsonmod::parse_pub(&text)
        },
        "获取详情" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("获取详情", args, 1, 4)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 1)?;
            let h = 请求头(args.get(2))?;
            let mut o = 读选项(args.get(3))?;
            o.method = "GET".to_string();
            o.timeout = t;
            o.headers = h;
            let r = 执行(&raw, &o)?;
            Ok(crate::dict_new(vec![
                (Val::Str("状态".to_string().into()), Val::Int(r.status as i128)),
                (Val::Str("响应头".to_string().into()), 头表(&r.headers)),
                (Val::Str("正文".to_string().into()),
                 Val::Str(String::from_utf8_lossy(&r.body).to_string().into())),
            ]))
        },
        "下载" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("下载", args, 2, 5)?;
            let raw = as_text(&args[0]);
            let 路径 = as_text(&args[1]);
            let t = timeout_of(args, 2)?;
            let h = 请求头(args.get(3))?;
            let mut o = 读选项(args.get(4))?;
            // `下载` 默认**不限大小**（流式落盘，本来就该拿来下大文件）；
            // 用户显式给了 `大小上限` 才照办
            if !o.上限已给 {
                o.上限 = 0;
            }
            o.method = "GET".to_string();
            o.timeout = t;
            o.headers = h;
            o.落盘 = Some(路径.clone());
            // 落盘时**明确要 identity**：gzip 的数据没法边解边写（内存会升高）
            if !o.headers.iter().any(|(k, _)| k.eq_ignore_ascii_case("accept-encoding")) {
                o.headers.push(("Accept-Encoding".to_string(), "identity".to_string()));
            }
            let r = 执行(&raw, &o)?;
            let r = 要成功(r, "下载", &raw)?;
            Ok(Val::Int(r.字节数 as i128))
        },
    }
}
