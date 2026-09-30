//! 标准库 `标识`（Python 侧 `jishi/stdlib/标识.py`，3 个函数）。
//!
//! 唯一标识（UUID 第 4 版式）。**零第三方依赖**：UUID v4 就是「128 位随机数 +
//! 第 7 字节高 4 位置 0100、第 9 字节高 2 位置 10」，自己拼就行。
//!
//! 随机源**复用 `rand` 模块的 `next_u64`**（全项目只有那一个实现）——
//! 不要在这里另写一个 xorshift，否则两处的种子策略会漂开。
//!
//! ⚠️ 随机结果没法与 Python 逐字节对拍，测试只能验「形状」（长度、分段数、
//! 字符集、两次不同）。

use std::collections::HashMap;

use super::{need_range, rand, table, R};
use crate::{err, list_new, Val, VM};

/// 生成 16 字节的 UUID v4。
fn uuid4_bytes() -> [u8; 16] {
    let mut out = [0u8; 16];
    for i in 0..2 {
        out[i * 8..(i + 1) * 8].copy_from_slice(&rand::next_u64().to_be_bytes());
    }
    out[6] = (out[6] & 0x0f) | 0x40; // 版本 4
    out[8] = (out[8] & 0x3f) | 0x80; // 变体 RFC 4122
    out
}

const B64URL: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/// 16 字节 → base64url（去掉填充），与 Python 侧 `_b64url` 同算法。
fn b64url(data: &[u8]) -> String {
    let mut out = Vec::new();
    let mut i = 0;
    while i < data.len() {
        let chunk = &data[i..std::cmp::min(i + 3, data.len())];
        let mut bits = 0u32;
        for (j, b) in chunk.iter().enumerate() {
            bits |= (*b as u32) << (16 - 8 * j);
        }
        out.push(B64URL[((bits >> 18) & 63) as usize]);
        out.push(B64URL[((bits >> 12) & 63) as usize]);
        if chunk.len() > 1 {
            out.push(B64URL[((bits >> 6) & 63) as usize]);
        }
        if chunk.len() > 2 {
            out.push(B64URL[(bits & 63) as usize]);
        }
        i += 3;
    }
    String::from_utf8(out).unwrap()
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "标识";
        "唯一标识" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("唯一标识", args, 0, 1)?;
            let no_dash = if args.is_empty() {
                false
            } else {
                match &args[0] {
                    Val::Bool(b) => *b,
                    v => {
                        return Err(err(
                            "类型错误",
                            format!("「去掉横线」要传「真」或「假」，得到了 {}", crate::display(v)),
                        ))
                    }
                }
            };
            let b = uuid4_bytes();
            let hex: String = b.iter().map(|x| format!("{:02x}", x)).collect();
            if no_dash {
                return Ok(Val::Str(hex));
            }
            Ok(Val::Str(format!(
                "{}-{}-{}-{}-{}",
                &hex[0..8],
                &hex[8..12],
                &hex[12..16],
                &hex[16..20],
                &hex[20..32]
            )))
        },
        "唯一标识短" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("唯一标识短", args, 0, 0)?;
            Ok(Val::Str(b64url(&uuid4_bytes())))
        },
        "唯一标识字节" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("唯一标识字节", args, 0, 0)?;
            Ok(list_new(
                uuid4_bytes().iter().map(|b| Val::Int(*b as i128)).collect(),
            ))
        },
    }
}
