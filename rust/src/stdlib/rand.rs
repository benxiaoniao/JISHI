//! 标准库 `随机`（Python 侧 `jishi/stdlib/随机.py`，4 个函数）。
//!
//! **结果是随机的，所以没法与 Python 逐字节对拍**——测试只能验「落在范围内」、
//! 「洗牌后元素集合不变」这类性质。
//!
//! Rust 标准库没有随机数发生器，这里用 `xorshift64*`（自己写，十几行），
//! 种子取自系统时间纳秒 + 一个进程内递增计数。质量对标准库这种用途足够，
//! 而且不引依赖。**不适合做密码学用途**——那该用 `加密` 模块的摘要算法。

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use super::{as_intish, need, table, to_vec, R};
use crate::{display, err, Val, VM};

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
        "随机整数" => |args: &[Val], _vm: &mut VM| -> R {
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
        "随机小数" => |args: &[Val], _vm: &mut VM| -> R {
            need("随机小数", args, 0)?;
            // 取 53 位有效位，落在 [0, 1)
            let x = (next_u64() >> 11) as f64 / (1u64 << 53) as f64;
            Ok(Val::Float(x))
        },
        "随机选择" => |args: &[Val], _vm: &mut VM| -> R {
            need("随机选择", args, 1)?;
            let items = to_vec(&args[0])?;
            if items.is_empty() {
                return Err(err("值错误", "不能从空序列里随机选择"));
            }
            Ok(items[below(items.len() as u64) as usize].clone())
        },
        "洗牌" => |args: &[Val], _vm: &mut VM| -> R {
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
