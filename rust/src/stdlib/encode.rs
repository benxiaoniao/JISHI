//! 标准库 `编码`（Python 侧 `jishi/stdlib/编码.py`，8 个函数）。
//!
//! base64 / 十六进制 / URL 编解码。**零第三方依赖**——base64 与 URL 编码
//! 都自己写（十几行），不引 `base64` crate。

use std::collections::HashMap;

use super::{need_range, table, to_vec, R};
use crate::{err, list_new, Val, VM};

/// 文本或字节列表 → `Vec<u8>`（与 Python 侧 `_to_bytes` 同规则）。
fn to_bytes(name: &str, v: &Val) -> Result<Vec<u8>, crate::JishiError> {
    match v {
        Val::Str(s) => Ok(s.as_bytes().to_vec()),
        _ => {
            let items = to_vec(v).map_err(|_| {
                err("值错误", format!("「{name}」要传文本、字节列表或 bytes"))
            })?;
            let mut out = Vec::with_capacity(items.len());
            for (i, x) in items.iter().enumerate() {
                match x {
                    Val::Int(n) => {
                        if !(0..=255).contains(n) {
                            return Err(err(
                                "值错误",
                                format!("「{name}」里第 {} 个超出范围（要 0-255）：{}", i + 1, n),
                            ));
                        }
                        out.push(*n as u8);
                    }
                    other => {
                        return Err(err(
                            "值错误",
                            format!("「{name}」里第 {} 个不是 0-255 的整数：{}", i + 1, crate::display(other)),
                        ))
                    }
                }
            }
            Ok(out)
        }
    }
}

/// 解码类函数的输入：必须是 ASCII 文本（base64 / 十六进制都是）。
fn want_ascii(name: &str, v: &Val) -> Result<String, crate::JishiError> {
    let raw = match v {
        Val::Str(s) => s.clone(),
        _ => {
            let bs = to_bytes(name, v)?;
            String::from_utf8(bs).map_err(|_| {
                err("值错误", format!("「{name}」要传文本或字节列表"))
            })?
        }
    };
    if !raw.is_ascii() {
        return Err(err(
            "值错误",
            format!("「{name}」的内容应当是 base64 / 十六进制字符，但里面出现了非 ASCII 字符"),
        ));
    }
    Ok(raw)
}

const B64: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn b64_encode(data: &[u8]) -> String {
    let mut out = Vec::new();
    for chunk in data.chunks(3) {
        let mut bits = 0u32;
        for (j, b) in chunk.iter().enumerate() {
            bits |= (*b as u32) << (16 - 8 * j);
        }
        out.push(B64[((bits >> 18) & 63) as usize]);
        out.push(B64[((bits >> 12) & 63) as usize]);
        if chunk.len() > 1 {
            out.push(B64[((bits >> 6) & 63) as usize]);
        } else {
            out.push(b'=');
        }
        if chunk.len() > 2 {
            out.push(B64[(bits & 63) as usize]);
        } else {
            out.push(b'=');
        }
    }
    String::from_utf8(out).unwrap()
}

fn b64_decode(s: &str) -> Result<Vec<u8>, crate::JishiError> {
    let bytes = s.as_bytes();
    if bytes.len() % 4 != 0 {
        return Err(err("值错误", format!("这不是合法的 base64 内容：{s}")));
    }
    let val = |c: u8| -> Option<u32> { B64.iter().position(|x| *x == c).map(|i| i as u32) };
    let mut out = Vec::new();
    for chunk in bytes.chunks(4) {
        let mut bits = 0u32;
        let mut pad = 0;
        for (j, c) in chunk.iter().enumerate() {
            if *c == b'=' {
                pad += 1;
                continue;
            }
            match val(*c) {
                Some(v) => bits |= v << (18 - 6 * j),
                None => return Err(err("值错误", format!("这不是合法的 base64 内容：{s}"))),
            }
        }
        out.push((bits >> 16) as u8);
        if pad < 2 {
            out.push((bits >> 8) as u8);
        }
        if pad < 1 {
            out.push(bits as u8);
        }
    }
    Ok(out)
}

/// URL 编码：与 Python 的 `urllib.parse.quote` 同规则——
/// **保留 `A-Za-z0-9_.-~` 与调用方指定的 safe 字符**，其余按 UTF-8 逐字节 `%XX`。
fn url_quote(s: &str, keep_slash: bool) -> String {
    let mut out = String::new();
    for b in s.as_bytes() {
        let c = *b as char;
        let unreserved = c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-' | '~');
        if unreserved || (keep_slash && c == '/') {
            out.push(c);
        } else {
            out.push_str(&format!("%{:02X}", b));
        }
    }
    out
}

fn url_unquote(s: &str) -> Result<String, crate::JishiError> {
    let bytes = s.as_bytes();
    let mut out: Vec<u8> = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            if i + 2 >= bytes.len() {
                return Err(err("值错误", format!("这不是合法的 URL 编码内容：{s}")));
            }
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
            match u8::from_str_radix(hex, 16) {
                Ok(b) => out.push(b),
                Err(_) => {
                    return Err(err("值错误", format!("这不是合法的 URL 编码内容：{s}")))
                }
            }
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out)
        .map_err(|_| err("值错误", format!("这不是合法的 URL 编码内容：{s}")))
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! {
        "base64编码" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("base64编码", args, 1, 1)?;
            Ok(Val::Str(b64_encode(&to_bytes("base64编码", &args[0])?)))
        },
        "base64解码" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("base64解码", args, 1, 1)?;
            let s = want_ascii("base64解码", &args[0])?;
            Ok(list_new(b64_decode(&s)?.into_iter().map(|b| Val::Int(b as i128)).collect()))
        },
        "十六进制编码" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("十六进制编码", args, 1, 2)?;
            let upper = matches!(args.get(1), Some(Val::Bool(true)));
            let h: String = to_bytes("十六进制编码", &args[0])?
                .iter()
                .map(|b| format!("{:02x}", b))
                .collect();
            Ok(Val::Str(if upper { h.to_uppercase() } else { h }))
        },
        "十六进制解码" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("十六进制解码", args, 1, 1)?;
            let raw = want_ascii("十六进制解码", &args[0])?;
            // 宽容输入：空格 / Tab / `0x` / 逗号都去掉；奇数长度前面补 0
            let mut s: String = raw
                .chars()
                .filter(|c| !c.is_whitespace() && *c != ',')
                .collect();
            s = s.replace("0x", "").replace("0X", "");
            if s.is_empty() {
                return Ok(list_new(vec![]));
            }
            if s.len() % 2 == 1 {
                s.insert(0, '0');
            }
            let mut out = Vec::new();
            let b = s.as_bytes();
            let mut i = 0;
            while i < b.len() {
                let hex = std::str::from_utf8(&b[i..i + 2]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(x) => out.push(Val::Int(x as i128)),
                    Err(_) => {
                        return Err(err(
                            "值错误",
                            format!("这不是合法的十六进制内容：{raw}"),
                        ))
                    }
                }
                i += 2;
            }
            Ok(list_new(out))
        },
        "url编码" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("url编码", args, 1, 2)?;
            let keep = !matches!(args.get(1), Some(Val::Bool(false)));
            Ok(Val::Str(url_quote(&crate::display(&args[0]), keep)))
        },
        "url解码" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("url解码", args, 1, 1)?;
            match &args[0] {
                Val::Str(s) => Ok(Val::Str(url_unquote(s)?)),
                v => Err(err(
                    "值错误",
                    format!("「url解码」要传文本，得到了 {}", crate::display(v)),
                )),
            }
        },
        "文本字节" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("文本字节", args, 1, 1)?;
            match &args[0] {
                Val::Str(s) => Ok(list_new(
                    s.as_bytes().iter().map(|b| Val::Int(*b as i128)).collect(),
                )),
                v => Err(err(
                    "值错误",
                    format!("「文本字节」要传文本，得到了 {}", crate::display(v)),
                )),
            }
        },
        "字节文本" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("字节文本", args, 1, 1)?;
            let bs = to_bytes("字节文本", &args[0])?;
            String::from_utf8(bs).map(Val::Str).map_err(|_| {
                err(
                    "值错误",
                    "这些字节不是合法的 UTF-8 文本（可能被截断或本来就是二进制）",
                )
            })
        },
    }
}
