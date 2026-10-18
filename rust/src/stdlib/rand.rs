//! 标准库 `随机`（Python 侧 `jishi/stdlib/随机.py`，4 个函数）。
//!
//! **结果是随机的，所以没法与 Python 逐字节对拍**——测试只能验「落在范围内」、
//! 「洗牌后元素集合不变」这类性质。
//!
//! Rust 标准库没有随机数发生器，这里用 `xorshift64*`（自己写，十几行），
//! 种子取自系统时间纳秒 + 一个进程内递增计数。质量对标准库这种用途足够，
//! 而且不引依赖。**不适合做密码学用途**——那该用 `加密` 模块的摘要算法。

use crate::BuiltinHost;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use super::{as_intish, as_num, as_text, need, need_range, table, to_vec, R};
use crate::{display, err, Val};

static STATE: OnceLock<Mutex<u64>> = OnceLock::new();

/// 取一个 64 位随机数。
///
/// **给别的模块用的**（M50 的  要生成 UUID）：Rust 标准库没有随机数
/// 发生器，全项目只有这里一个实现——别再各写一个（那样两处的质量与种子
/// 策略会漂开）。
pub(crate) fn next_u64() -> u64 {
    let slot = STATE.get_or_init(|| {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0x9E37_79B9_7F4A_7C15);
        // 0 是 xorshift 的不动点，必须避开
        Mutex::new(nanos | 1)
    });
    let mut s = slot.lock().unwrap_or_else(|e| e.into_inner());
    let mut x = *s;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    *s = x;
    x.wrapping_mul(0x2545_F491_4F6C_DD1D)
}

/// `[0, bound)` 内的均匀整数（bound == 0 时返回 0）。
fn below(bound: u64) -> u64 {
    if bound == 0 {
        return 0;
    }
    // 拒绝采样消除取模偏置：区间越大越接近均匀，最多多抽几次
    let limit = u64::MAX - (u64::MAX % bound) - 1;
    loop {
        let x = next_u64();
        if x <= limit {
            return x % bound;
        }
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "随机";
        "随机整数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("随机整数", args, 2)?;
            let lo = as_intish(&args[0])?;
            let hi = as_intish(&args[1])?;
            if lo > hi {
                return Err(err(
                    "值错误",
                    format!("「随机整数」的下界 {} 比上界 {} 还大", display(&args[0]), display(&args[1])),
                ));
            }
            let span = (hi - lo) as u128 + 1;
            if span > u64::MAX as u128 {
                // 区间大到没法均匀取——退化成「取一个中间值」，别静默给偏置结果
                return Ok(Val::Int(lo));
            }
            Ok(Val::Int(lo + below(span as u64) as i128))
        },
        "随机小数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("随机小数", args, 0)?;
            // 取 53 位有效位，落在 [0, 1)
            let x = (next_u64() >> 11) as f64 / (1u64 << 53) as f64;
            Ok(Val::Float(x))
        },
        "随机选择" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("随机选择", args, 1)?;
            let items = to_vec(&args[0])?;
            if items.is_empty() {
                return Err(err("值错误", "不能从空序列里随机选择"));
            }
            Ok(items[below(items.len() as u64) as usize].clone())
        },
        // M117：`随机文本` / `按权重`。字符集**故意用中文短名**（核心一），
        // 不认识就把可用的列出来（核心二：报错可操作）。
        "随机文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("随机文本", args, 1, 2)?;
            let 池 = if args.len() > 1 {
                let 名 = as_text(&args[1]);
                match 名.as_str() {
                    "字母数字" => "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                    "字母" => "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    "小写字母" => "abcdefghijklmnopqrstuvwxyz",
                    "大写字母" => "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    "数字" => "0123456789",
                    _ => {
                        return Err(err(
                            "值错误",
                            format!(
                                "不认识的字符集「{名}」（可用：字母数字 / 字母 / 小写字母 / 大写字母 / 数字）"
                            ),
                        ))
                    }
                }
            } else {
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            };
            let n = as_intish(&args[0])?;
            if n < 0 {
                return Err(err("值错误", format!("长度不能是负数（现在是 {n}）")));
            }
            if n > 100_000 {
                return Err(err("值错误", format!("一次最多生成 10 万个字符（现在要 {n} 个）")));
            }
            let 字符们: Vec<char> = 池.chars().collect();
            let mut 出 = String::with_capacity(n as usize * 2);
            for _ in 0..n {
                出.push(字符们[below(字符们.len() as u64) as usize]);
            }
            Ok(Val::Str(出))
        },
        "按权重" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("按权重", args, 2)?;
            let 项 = to_vec(&args[0])?;
            let 权们 = to_vec(&args[1])?;
            if 项.is_empty() {
                return Err(err("值错误", "不能从空序列里按权重挑元素"));
            }
            if 项.len() != 权们.len() {
                return Err(err(
                    "值错误",
                    format!("选项有 {} 个，权重有 {} 个 —— 两者要一样多", 项.len(), 权们.len()),
                ));
            }
            let mut 权值 = Vec::with_capacity(权们.len());
            let mut 总 = 0f64;
            for w in &权们 {
                let x = as_num(w)?;
                if x < 0.0 {
                    return Err(err("值错误", "权重不能是负数"));
                }
                总 += x;
                权值.push(x);
            }
            if 总 <= 0.0 {
                return Err(err("值错误", "权重加起来要大于 0（全 0 等于没有可选项）"));
            }
            let 落 = (next_u64() >> 11) as f64 / (1u64 << 53) as f64 * 总;
            let mut 累计 = 0f64;
            for (i, w) in 权值.iter().enumerate() {
                累计 += *w;
                if 落 < 累计 {
                    return Ok(项[i].clone());
                }
            }
            Ok(项[项.len() - 1].clone())      // 浮点误差兜底
        },
        "洗牌" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("洗牌", args, 1)?;
            let mut items = to_vec(&args[0])?;
            // Fisher-Yates：原地洗**副本**，原列表不动（与 Python 侧一致）
            let n = items.len();
            for i in (1..n).rev() {
                let j = below(i as u64 + 1) as usize;
                items.swap(i, j);
            }
            Ok(crate::list_new(items))
        },
    }
}
