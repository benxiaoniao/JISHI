//! 标准库 `编码`（Python 侧 `jishi/stdlib/编码.py`，8 个函数）。
//!
//! base64 / 十六进制 / URL 编解码。**零第三方依赖**——base64 与 URL 编码
//! 都自己写（十几行），不引 `base64` crate。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{need_range, table, to_vec, R};
// M113：解码方向改返回 `Val::Bytes` 之后，这里不再需要 `list_new`
use crate::{display, err, Val};

/// 文本 / 字节 / 0-255 的整数列表 → `Vec<u8>`（与 Python 侧 `_to_bytes` 同规则）。
fn to_bytes(name: &str, v: &Val) -> Result<Vec<u8>, crate::JishiError> {
    match v {
        Val::Str(s) => Ok(s.as_bytes().to_vec()),
        // M113 起有真正的字节类型了，原样拿
        Val::Bytes(b) => Ok(b.as_ref().clone()),
        _ => {
            let items = to_vec(v).map_err(|_| {
                err("值错误", format!("「{name}」要传文本、字节或 0-255 的整数列表"))
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
        Val::Str(s) => s.to_string(),
        // 字节 / 0-255 的整数列表：先摊成字节再当 ASCII 文本看
        _ => {
            let bs = to_bytes(name, v)?;
            String::from_utf8(bs).map_err(|_| {
                err("值错误", format!("「{name}」要传文本或字节"))
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

/// base64 解码。`pub(crate)`：`图形.截图` 要把宿主给的 base64 像素解回字节
/// （不经过脚本层 —— 那会把几百 KB 数据来回穿两次）。
pub(crate) fn b64_decode(s: &str) -> Result<Vec<u8>, crate::JishiError> {
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
    table! { "编码";
        "base64编码" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("base64编码", args, 1, 1)?;
            Ok(Val::Str(b64_encode(&to_bytes("base64编码", &args[0])?).into()))
        },
        "base64解码" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("base64解码", args, 1, 1)?;
            let s = want_ascii("base64解码", &args[0])?;
            // M113：返回**字节**（以前给 0-255 的整数列表 —— 那是没有字节类型时的凑合）
            Ok(Val::Bytes(std::rc::Rc::new(b64_decode(&s)?)))
        },
        "十六进制编码" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("十六进制编码", args, 1, 2)?;
            let upper = matches!(args.get(1), Some(Val::Bool(true)));
            let h: String = to_bytes("十六进制编码", &args[0])?
                .iter()
                .map(|b| format!("{:02x}", b))
                .collect();
            Ok(Val::Str(if upper { h.to_uppercase() } else { h }.into()))
        },
        "十六进制解码" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("十六进制解码", args, 1, 1)?;
            let raw = want_ascii("十六进制解码", &args[0])?;
            // 宽容输入：空格 / Tab / `0x` / 逗号都去掉；奇数长度前面补 0
            let mut s: String = raw
                .chars()
                .filter(|c| !c.is_whitespace() && *c != ',')
                .collect();
            s = s.replace("0x", "").replace("0X", "");
            if s.is_empty() {
                return Ok(Val::Bytes(std::rc::Rc::new(Vec::new())));
            }
            if s.len() % 2 == 1 {
                s.insert(0, '0');
            }
            let mut out: Vec<u8> = Vec::new();
            let b = s.as_bytes();
            let mut i = 0;
            while i < b.len() {
                let hex = std::str::from_utf8(&b[i..i + 2]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(x) => out.push(x),
                    Err(_) => {
                        return Err(err(
                            "值错误",
                            format!("这不是合法的十六进制内容：{raw}"),
                        ))
                    }
                }
                i += 2;
            }
            // M113：返回**字节**
            Ok(Val::Bytes(std::rc::Rc::new(out)))
        },
        "url编码" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("url编码", args, 1, 2)?;
            let keep = !matches!(args.get(1), Some(Val::Bool(false)));
            Ok(Val::Str(url_quote(&crate::display(&args[0]), keep).into()))
        },
        "url解码" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("url解码", args, 1, 1)?;
            match &args[0] {
                Val::Str(s) => Ok(Val::Str(url_unquote(s)?.into())),
                v => Err(err(
                    "值错误",
                    "「url解码」要传文本",
                )),
            }
        },
        "文本字节" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("文本字节", args, 1, 1)?;
            match &args[0] {
                // M113：返回**字节**（以前给 0-255 的整数列表）
                Val::Str(s) => Ok(Val::Bytes(std::rc::Rc::new(s.as_bytes().to_vec()))),
                v => Err(err(
                    "值错误",
                    format!("「文本字节」要传文本，得到了 {}", crate::display(v)),
                )),
            }
        },
        // M204：GBK 改成**自带的静态表**（`gbk_table.rs` + 两个 .bin，共 ~145 KB）——
        //   以前为这两个函数背 `encoding_rs`（本项目第二个第三方依赖，M119 引入）。
        // ⚠️ 语义与 encoding_rs **逐条对过**：解码 24069 / 编码 24085，0 差异
        //    （导出后拿「基石编码库」的独立参考实现又对了一遍）。
        "GBK文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("GBK文本", args, 1, 1)?;
            let bs = to_bytes("GBK文本", &args[0])?;
            match gbk_解码(&bs) {
                Some(t) => Ok(Val::Str(t.into())),
                None => Err(err(
                    "值错误",
                    "这些字节不是合法的 GBK 文本（可能有截断，或其实是别的编码）",
                )),
            }
        },
        "文本GBK" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("文本GBK", args, 1, 1)?;
            let s = match &args[0] {
                Val::Str(t) => t.clone(),
                v => return Err(err("值错误", format!("「文本GBK」要传文本（现在是「{}」）", display(v)))),
            };
            match gbk_编码(&s) {
                Some(b) => Ok(Val::Bytes(std::rc::Rc::new(b))),
                None => Err(err(
                    "值错误",
                    "这段文本里有 GBK 表示不了的字符（比如表情符号）",
                )),
            }
        },
        "字节文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("字节文本", args, 1, 1)?;
            let bs = to_bytes("字节文本", &args[0])?;
            String::from_utf8(bs).map(|s| Val::Str(s.into())).map_err(|_| {
                err(
                    "值错误",
                    "这些字节不是合法的 UTF-8 文本（可能被截断或本来就是二进制）",
                )
            })
        },
    }
}

// ---------------------------------------------------------------------------
// GBK（M204：自带码表，替代 encoding_rs）
// ---------------------------------------------------------------------------

/// GBK 解码（WHATWG gbk）。**任何非法序列都返回 `None`** —— 不静默给 U+FFFD。
///
/// 规则（与 encoding_rs 的 gbk 解码器逐条对齐）：
/// * `0x00..=0x7F` → 自身；
/// * `0x80` → `U+20AC`（欧元，WHATWG 的 gbk 特例）；
/// * `0x81..=0xFE` + `0x40..=0xFE`（不含 `0x7F`）→ 查表，表里没有就是非法；
/// * **四字节序列（`0x30..=0x39` 打头）在 gbk 里是非法**（那是 gb18030 的能力）；
/// * 结尾只剩一个首字节 → 非法。
fn gbk_解码(bs: &[u8]) -> Option<String> {
    let mut 出 = String::with_capacity(bs.len());
    let mut i = 0;
    while i < bs.len() {
        let 首 = bs[i];
        i += 1;
        if 首 < 0x80 {
            出.push(首 as char);
            continue;
        }
        if 首 == 0x80 {
            出.push('\u{20AC}');
            continue;
        }
        if !(0x81..=0xFE).contains(&首) || i >= bs.len() {
            return None;
        }
        let 次 = bs[i];
        i += 1;
        if !(0x40..=0xFE).contains(&次) || 次 == 0x7F {
            return None;
        }
        let 槽 = (首 as usize - 0x81) * 191 + (次 as usize - 0x40);
        let 码位 = crate::gbk_table::槽位(槽);
        if 码位 == 0 {
            return None;
        }
        出.push(char::from_u32(码位 as u32)?);
    }
    Some(出)
}

/// GBK 编码（WHATWG gbk）。**编不出来返回 `None`** —— 不静默换成 `?`。
fn gbk_编码(s: &str) -> Option<Vec<u8>> {
    let mut 出 = Vec::with_capacity(s.len() * 2);
    for ch in s.chars() {
        let cp = ch as u32;
        if cp < 0x80 {
            出.push(cp as u8);
            continue;
        }
        if cp > 0xFFFF {
            return None;
        }
        let 码 = crate::gbk_table::查编码(cp as u16);
        if 码 == 0 {
            return None;
        }
        if 码 < 0x100 {
            出.push(码 as u8);
        } else {
            出.push((码 >> 8) as u8);
            出.push((码 & 0xFF) as u8);
        }
    }
    Some(出)
}

/// 给同 crate 的其它模块用（`图形.截图`）：把 `JishiError` 原样抛出去。
pub(crate) fn b64_decode_pub(s: &str) -> Result<Vec<u8>, crate::JishiError> {
    b64_decode(s)
}
