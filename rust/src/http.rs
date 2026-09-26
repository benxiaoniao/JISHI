//! 标准库 `网络`（Python 侧 `jishi/stdlib/网络.py`，3 个函数）。
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

use std::collections::HashMap;

use crate::stdlib::{as_num, as_text, need, need_range, table, R};
use crate::{err, JishiError, Val, VM};

const DEFAULT_TIMEOUT: f64 = 10.0;
const AGENT: &str = "jishi/0.1";

/// 拆出来的网址各部分。
struct Url {
    secure: bool,
    host: String,
    port: u16,
    path: String,
}

fn parse_url(raw: &str) -> Result<Url, JishiError> {
    let bad = || err("运行期错误", format!("「{raw}」不是能识别的网址（要写 http:// 或 https://）"));
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
    use super::{describe_code, Url, AGENT};
    use crate::{err, JishiError};

    type Handle = *mut core::ffi::c_void;

    const ACCESS_TYPE_DEFAULT_PROXY: u32 = 0;
    const FLAG_SECURE: u32 = 0x0080_0000;
    const FLAG_REFRESH: u32 = 0x0000_0100;
    const QUERY_STATUS_CODE: u32 = 19;
    const QUERY_FLAG_NUMBER: u32 = 0x2000_0000;

    #[link(name = "winhttp")]
    extern "system" {
        fn WinHttpOpen(
            agent: *const u16,
            access_type: u32,
            proxy: *const u16,
            bypass: *const u16,
            flags: u32,
        ) -> Handle;
        fn WinHttpConnect(session: Handle, host: *const u16, port: u16, reserved: u32) -> Handle;
        fn WinHttpOpenRequest(
            connect: Handle,
            verb: *const u16,
            object: *const u16,
            version: *const u16,
            referrer: *const u16,
            accept_types: *const *const u16,
            flags: u32,
        ) -> Handle;
        fn WinHttpSendRequest(
            request: Handle,
            headers: *const u16,
            headers_len: u32,
            optional: *const core::ffi::c_void,
            optional_len: u32,
            total_len: u32,
            context: usize,
        ) -> i32;
        fn WinHttpReceiveResponse(request: Handle, reserved: *mut core::ffi::c_void) -> i32;
        fn WinHttpQueryHeaders(
            request: Handle,
            info_level: u32,
            name: *const u16,
            buffer: *mut core::ffi::c_void,
            buffer_len: *mut u32,
            index: *mut u32,
        ) -> i32;
        fn WinHttpQueryDataAvailable(request: Handle, available: *mut u32) -> i32;
        fn WinHttpReadData(
            request: Handle,
            buffer: *mut core::ffi::c_void,
            to_read: u32,
            read: *mut u32,
        ) -> i32;
        fn WinHttpCloseHandle(handle: Handle) -> i32;
        fn WinHttpSetTimeouts(
            handle: Handle,
            resolve: i32,
            connect: i32,
            send: i32,
            receive: i32,
        ) -> i32;
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
        let code = unsafe { GetLastError() };
        describe_code(code)
    }

    pub fn request(
        url: &Url,
        method: &str,
        body: Option<&[u8]>,
        timeout: f64,
    ) -> Result<String, JishiError> {
        let ms = (timeout * 1000.0) as i32;
        let agent = wide(AGENT);
        let session = unsafe {
            WinHttpOpen(agent.as_ptr(), ACCESS_TYPE_DEFAULT_PROXY, std::ptr::null(), std::ptr::null(), 0)
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
            WinHttpOpenRequest(
                connect.0,
                verb.as_ptr(),
                object.as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null(),
                flags,
            )
        };
        if request.is_null() {
            return Err(err("运行期错误", format!("构造请求失败：{}", last_error())));
        }
        let request = Guard(request);

        let (headers, (ptr, len)) = match body {
            None => (None, (std::ptr::null(), 0u32)),
            Some(b) => (
                Some(wide("Content-Type: application/x-www-form-urlencoded\r\n")),
                (b.as_ptr() as *const core::ffi::c_void, b.len() as u32),
            ),
        };
        let (hptr, hlen) = match &headers {
            Some(h) => (h.as_ptr(), u32::MAX), // -1：让 WinHTTP 自己算长度
            None => (std::ptr::null(), 0),
        };
        let ok = unsafe {
            WinHttpSendRequest(request.0, hptr, hlen, ptr, len, len, 0)
        };
        if ok == 0 {
            return Err(err("运行期错误", format!("发请求失败：{}", last_error())));
        }
        let ok = unsafe { WinHttpReceiveResponse(request.0, std::ptr::null_mut()) };
        if ok == 0 {
            return Err(err("运行期错误", format!("收响应失败：{}", last_error())));
        }

        // 状态码：400 以上 Python 的 urlopen 会抛 HTTPError，我们照做
        let mut status: u32 = 0;
        let mut size = std::mem::size_of::<u32>() as u32;
        let ok = unsafe {
            WinHttpQueryHeaders(
                request.0,
                QUERY_STATUS_CODE | QUERY_FLAG_NUMBER,
                std::ptr::null(),
                &mut status as *mut u32 as *mut core::ffi::c_void,
                &mut size,
                std::ptr::null_mut(),
            )
        };
        if ok != 0 && status >= 400 {
            return Err(err(
                "运行期错误",
                format!("服务器返回了 {status}（HTTP 错误）"),
            ));
        }

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
                WinHttpReadData(
                    request.0,
                    buf.as_mut_ptr() as *mut core::ffi::c_void,
                    avail,
                    &mut got,
                )
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
        Ok(String::from_utf8_lossy(&out).to_string())
    }
}

// ---------------------------------------------------------------------------
// 其它平台：裸 TCP（http 可用，https 明确报错）
// ---------------------------------------------------------------------------

#[cfg(not(windows))]
mod client {
    use super::{Url, AGENT};
    use crate::{err, JishiError};
    use std::io::{Read, Write};
    use std::net::{TcpStream, ToSocketAddrs};
    use std::time::Duration;

    pub fn request(
        url: &Url,
        method: &str,
        body: Option<&[u8]>,
        timeout: f64,
    ) -> Result<String, JishiError> {
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
        if let Some(b) = body {
            req.push_str("Content-Type: application/x-www-form-urlencoded\r\n");
            req.push_str(&format!("Content-Length: {}\r\n", b.len()));
        }
        req.push_str("\r\n");
        s.write_all(req.as_bytes())
            .map_err(|e| err("运行期错误", format!("发请求失败：{e}")))?;
        if let Some(b) = body {
            s.write_all(b)
                .map_err(|e| err("运行期错误", format!("发请求失败：{e}")))?;
        }
        let mut raw: Vec<u8> = Vec::new();
        s.read_to_end(&mut raw)
            .map_err(|e| err("运行期错误", format!("收响应失败：{e}")))?;

        // 切开头与正文
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
        if status >= 400 {
            return Err(err("运行期错误", format!("服务器返回了 {status}（HTTP 错误）")));
        }
        let mut body_bytes = raw[split + 4..].to_vec();
        // chunked 编码要拆块（没有 Content-Length 时 Python 会自动处理）
        if head.to_ascii_lowercase().contains("transfer-encoding: chunked") {
            body_bytes = dechunk(&body_bytes);
        }
        Ok(String::from_utf8_lossy(&body_bytes).to_string())
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

fn timeout_of(args: &[Val], idx: usize) -> Result<f64, JishiError> {
    match args.get(idx) {
        Some(v) => Ok(as_num(v)?.max(0.1)),
        None => Ok(DEFAULT_TIMEOUT),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "网络";
        "获取" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("获取", args, 1, 2)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 1)?;
            let url = parse_url(&raw)?;
            Ok(Val::Str(client::request(&url, "GET", None, t).map_err(|e| {
                err("运行期错误", format!("访问「{raw}」失败：{}", e.message))
            })?))
        },
        "提交" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("提交", args, 1, 3)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 2)?;
            let body = if args.len() > 1 && !matches!(args[1], Val::None_) {
                as_text(&args[1]).into_bytes()
            } else {
                Vec::new()
            };
            let url = parse_url(&raw)?;
            Ok(Val::Str(client::request(&url, "POST", Some(&body), t).map_err(|e| {
                err("运行期错误", format!("提交到「{raw}」失败：{}", e.message))
            })?))
        },
        "获取JSON" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("获取JSON", args, 1, 2)?;
            let raw = as_text(&args[0]);
            let t = timeout_of(args, 1)?;
            let url = parse_url(&raw)?;
            let text = client::request(&url, "GET", None, t).map_err(|e| {
                err("运行期错误", format!("访问「{raw}」失败：{}", e.message))
            })?;
            crate::stdlib::jsonmod::parse_pub(&text)
        },
    }
}
