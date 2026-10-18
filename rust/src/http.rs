//! 标准库 `网络`（Python 侧 `jishi/stdlib/网络.py`）。
//!
//! **Node 那边用子进程绕，Rust 这边绕不过**——而且基石的网络是**同步**的
//! （没有 `await`），所以需要一个能同步拿结果的 HTTP 客户端。零依赖的
//! 前提下，两条路：
//!
//! - **Windows：绑 `winhttp.dll`**（`extern "system"`，是系统库、不是 crate）。
//!   好处是 **https 也能用**——TLS 是 WinHTTP 内部的事，我们一行加密都不用写。
//! - **其它平台：裸 `TcpStream` 走 HTTP/1.1**。`http://` 完全可用；
//!   `https://` 需要 TLS，而手写 TLS 不现实，所以**明确报错**并说明原因，
//!   绝不静默降级成明文请求。
//!
//! 默认超时 10 秒、`User-Agent: jishi/0.1`、自动跟随重定向（WinHTTP 默认行为，
//! 与 Python 的 `urllib` 一致）。
//!
//! ## M107 补了什么（2026-10-11）
//!
//! 原来只能 GET / 表单 POST，且**拿不到状态码与响应头**。这一轮补：
//! `请求头` 参数、`提交JSON`、`获取详情`（状态码 / 响应头 / 正文，非 2xx 不抛）、
//! `下载`（写文件、给字节数）。报错统一成 `HTTP {状态}`，各引擎措辞一致。

use crate::BuiltinHost;
use std::collections::HashMap;

use crate::stdlib::{as_num, as_text, need_range, table, R};
use crate::{err, JishiError, Val};

const DEFAULT_TIMEOUT: f64 = 10.0;
const AGENT: &str = "jishi/0.1";

/// 一次响应的完整形态（状态码 + 响应头 + 正文字节）。
pub(crate) struct Resp {
    status: u32,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

/// 拆出来的网址各部分。
struct Url {
    secure: bool,
    host: String,
    port: u16,
    path: String,
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

/// WinHTTP 的常见错误码 → 中文（不认识的给原始码，别瞎猜）。
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

// ---------------------------------------------------------------------------
// Windows：WinHTTP
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod client {
    use super::{describe_code, parse_header_text, Resp, Url, AGENT};
    use crate::{err, JishiError};

    type Handle = *mut core::ffi::c_void;

    const ACCESS_TYPE_DEFAULT_PROXY: u32 = 0;
    const FLAG_SECURE: u32 = 0x0080_0000;
    const FLAG_REFRESH: u32 = 0x0000_0100;
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

    /// 发一个请求，返回 `(状态码, 响应头, 响应体)` —— **不因 4xx/5xx 抛错**。
    pub fn request(url: &Url, method: &str, body: Option<&[u8]>, timeout: f64,
                   headers: &[(String, String)]) -> Result<Resp, JishiError> {
        let ms = (timeout * 1000.0) as i32;
        let agent = wide(AGENT);
        let session = unsafe {
            WinHttpOpen(agent.as_ptr(), ACCESS_TYPE_DEFAULT_PROXY,
                        std::ptr::null(), std::ptr::null(), 0)
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

        let verb = wide(method);
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

        // 自定义请求头（CRLF 分隔；长度 -1 让 WinHTTP 自己算）
        let 头串 = build_headers(headers);
        let 头宽 = 头串.as_ref().map(|s| wide(s));
        let (hptr, hlen) = match &头宽 {
            Some(h) => (h.as_ptr(), u32::MAX),
            None => (std::ptr::null(), 0),
        };
        let (bptr, blen) = match body {
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

        // 响应头原文（CRLF 分隔的多行文本）
        let headers = query_headers(request.0)?;

        let mut out: Vec<u8> = Vec::new();
        loop {
            let mut avail: u32 = 0;
            let ok = unsafe { WinHttpQueryDataAvailable(request.0, &mut avail) };
            if ok == 0 {
                return Err(err("运行期错误", format!("读响应失败：{}", last_error())));
            }
            if avail == 0 {
                break;
            }
            let mut buf = vec![0u8; avail as usize];
            let mut got: u32 = 0;
            let ok = unsafe {
                WinHttpReadData(request.0, buf.as_mut_ptr() as *mut core::ffi::c_void,
                                avail, &mut got)
            };
            if ok == 0 {
                return Err(err("运行期错误", format!("读响应失败：{}", last_error())));
            }
            if got == 0 {
                break;
            }
            buf.truncate(got as usize);
            out.extend_from_slice(&buf);
        }
        Ok(Resp { status, headers, body: out })
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
        Ok(parse_header_text(&text))
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
// 其它平台：裸 TCP（http 可用，https 明确报错）
// ---------------------------------------------------------------------------

#[cfg(not(windows))]
mod client {
    use super::{parse_header_text, Resp, Url, AGENT};
    use crate::{err, JishiError};
    use std::io::{Read, Write};
    use std::net::{TcpStream, ToSocketAddrs};
    use std::time::Duration;

    pub fn request(url: &Url, method: &str, body: Option<&[u8]>, timeout: f64,
                   headers: &[(String, String)]) -> Result<Resp, JishiError> {
        if url.secure {
            return Err(err(
                "运行期错误",
                "这个平台上本宿主没有 TLS，`https://` 用不了（请用 `http://`，或换 Python / Node 侧跑）",
            ));
        }
        let addr = format!("{}:{}", url.host, url.port);
        let sock = addr
            .to_socket_addrs()
            .map_err(|e| err("运行期错误", format!("域名解析不了：{e}")))?
            .next()
            .ok_or_else(|| err("运行期错误", "域名解析不了"))?;
        let dur = Duration::from_secs_f64(timeout.max(0.1));
        let mut s = TcpStream::connect_timeout(&sock, dur)
            .map_err(|e| err("运行期错误", format!("连不上服务器：{e}")))?;
        let _ = s.set_read_timeout(Some(dur));
        let _ = s.set_write_timeout(Some(dur));

        let mut req = format!(
            "{method} {} HTTP/1.1\r\nHost: {}\r\nUser-Agent: {AGENT}\r\nAccept: */*\r\nConnection: close\r\n",
            url.path, url.host
        );
        for (k, v) in headers {
            req.push_str(k);
            req.push_str(": ");
            req.push_str(v);
            req.push_str("\r\n");
        }
        if let Some(b) = body {
            req.push_str(&format!("Content-Length: {}\r\n", b.len()));
        }
        req.push_str("\r\n");
        s.write_all(req.as_bytes())
            .map_err(|e| err("运行期错误", format!("发请求失败：{e}")))?;
        if let Some(b) = body {
            s.write_all(b).map_err(|e| err("运行期错误", format!("发请求失败：{e}")))?;
        }
        let mut raw: Vec<u8> = Vec::new();
        s.read_to_end(&mut raw)
            .map_err(|e| err("运行期错误", format!("收响应失败：{e}")))?;

        let split = raw
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .ok_or_else(|| err("运行期错误", "服务器的响应格式不对"))?;
        let head = String::from_utf8_lossy(&raw[..split]).to_string();
        let status: u32 = head
            .split_whitespace()
            .nth(1)
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let mut body_bytes = raw[split + 4..].to_vec();
        if head.to_ascii_lowercase().contains("transfer-encoding: chunked") {
            body_bytes = dechunk(&body_bytes);
        }
        let headers = parse_header_text(&head);
        Ok(Resp { status, headers, body: body_bytes })
    }

    fn dechunk(data: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        let mut i = 0;
        while i < data.len() {
            let mut j = i;
            while j < data.len() && data[j] != b'\r' {
                j += 1;
            }
            let size_text = String::from_utf8_lossy(&data[i..j]).to_string();
            let size = usize::from_str_radix(size_text.split(';').next().unwrap_or("").trim(), 16)
                .unwrap_or(0);
            if size == 0 {
                break;
            }
            let start = j + 2;
            let end = (start + size).min(data.len());
            out.extend_from_slice(&data[start..end]);
            i = end + 2;
        }
        out
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
    let url = parse_url(raw)?;
    let r = client::request(&url, "GET", None, t, h)
        .map_err(|e| err("运行期错误", format!("访问「{raw}」失败：{}", e.message)))?;
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

/// 取一段网址的**原始字节** —— 包管理下载 zip 用（R7.6）。
///
/// ⚠️ 只认 `http://` / `https://`（与标准库 `网络.获取` 同一套）。`file://`
/// 由调用方自己处理（包管理的本地镜像场景，见 `packages.rs`）。
pub(crate) fn fetch_bytes(url: &str, timeout: f64) -> Result<Vec<u8>, JishiError> {
    let u = parse_url(url)?;
    let r = client::request(&u, "GET", None, timeout, &[])?;
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
                if let Val::Str(s) = val {
                    s.push_str(", ");
                    s.push_str(v);
                }
            }
            None => 出.push((Val::Str(k.clone()), Val::Str(v.clone()))),
        }
    }
    crate::dict_new(出)
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "网络";
        "获取" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("获取", args, 1, 3)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 1)?;
            let h = 请求头(args.get(2))?;
            Ok(Val::Str(取正文参数(&raw, t, &h)?))
        },
        "提交" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("提交", args, 1, 4)?;
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
            let url = parse_url(&raw)?;
            let r = client::request(&url, "POST", Some(&body), t, &h)
                .map_err(|e| err("运行期错误", format!("提交到「{raw}」失败：{}", e.message)))?;
            let r = 要成功(r, "提交到", &raw)?;
            Ok(Val::Str(String::from_utf8_lossy(&r.body).to_string()))
        },
        "提交JSON" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("提交JSON", args, 2, 4)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 2)?;
            let body = crate::stdlib::jsonmod::to_text(&args[1], None)?.into_bytes();
            let mut h = 请求头(args.get(3))?;
            if !h.iter().any(|(k, _)| k.eq_ignore_ascii_case("content-type")) {
                h.push(("Content-Type".to_string(),
                        "application/json; charset=utf-8".to_string()));
            }
            let url = parse_url(&raw)?;
            let r = client::request(&url, "POST", Some(&body), t, &h)
                .map_err(|e| err("运行期错误", format!("提交到「{raw}」失败：{}", e.message)))?;
            let r = 要成功(r, "提交到", &raw)?;
            Ok(Val::Str(String::from_utf8_lossy(&r.body).to_string()))
        },
        "获取JSON" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("获取JSON", args, 1, 3)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 1)?;
            let h = 请求头(args.get(2))?;
            let url = parse_url(&raw)?;
            let r = client::request(&url, "GET", None, t, &h)
                .map_err(|e| err("运行期错误", format!("访问「{raw}」失败：{}", e.message)))?;
            let r = 要成功(r, "访问", &raw)?;
            let text = String::from_utf8_lossy(&r.body).to_string();
            crate::stdlib::jsonmod::parse_pub(&text)
        },
        "获取详情" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("获取详情", args, 1, 3)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 1)?;
            let h = 请求头(args.get(2))?;
            let url = parse_url(&raw)?;
            let r = client::request(&url, "GET", None, t, &h)
                .map_err(|e| err("运行期错误", format!("访问「{raw}」失败：{}", e.message)))?;
            Ok(crate::dict_new(vec![
                (Val::Str("状态".to_string()), Val::Int(r.status as i128)),
                (Val::Str("响应头".to_string()), 头表(&r.headers)),
                (Val::Str("正文".to_string()),
                 Val::Str(String::from_utf8_lossy(&r.body).to_string())),
            ]))
        },
        "下载" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("下载", args, 2, 4)?;
            let raw = as_text(&args[0]);
            let 路径 = as_text(&args[1]);
            let t = timeout_of(args, 2)?;
            let h = 请求头(args.get(3))?;
            let url = parse_url(&raw)?;
            let r = client::request(&url, "GET", None, t, &h)
                .map_err(|e| err("运行期错误", format!("下载「{raw}」失败：{}", e.message)))?;
            let r = 要成功(r, "下载", &raw)?;
            std::fs::write(&路径, &r.body)
                .map_err(|e| err("运行期错误", format!("写文件「{路径}」失败：{e}")))?;
            Ok(Val::Int(r.body.len() as i128))
        },
    }
}
