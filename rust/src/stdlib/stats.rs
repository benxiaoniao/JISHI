//! 标准库 `统计`（Python 侧 `jishi/stdlib/统计.py`，10 个函数）。
//!
//! 一组数据的集中趋势与离散程度。**报错措辞与 Python 侧逐字一致**——
//! 对拍会逐字节比 stdout 与报错文本。

use std::collections::HashMap;

use super::{as_num, need_range, table, to_vec, R};
use crate::{err, Val, VM};

/// 把输入统一成数字列表（与 Python 侧 `_nums` 同规则）。
/// 数值结果：**整数就返回 、否则 **。
///
/// 与 Python 侧对齐： 在 Python 里是  6、显示成 
/// （不是 ）。所以这里不能一律 ——那会让输出变成 。
fn f64v(x: f64) -> Val {
    if x.fract() == 0.0 && x.is_finite() && x.abs() < 9.0e18 {
        Val::Int(x as i128)
    } else {
        Val::Float(x)
    }
}

fn nums(name: &str, v: &Val) -> Result<Vec<f64>, crate::JishiError> {
    if let Val::Str(_) = v {
        return Err(err("类型错误", format!("「{name}」要传一组数字，不能传文本")));
    }
    let items = to_vec(v).map_err(|_| {
        err("类型错误", format!("「{name}」要传一组数字（列表）"))
    })?;
    let mut out = Vec::with_capacity(items.len());
    for (i, x) in items.iter().enumerate() {
        match x {
            Val::Int(n) => out.push(*n as f64),
            Val::Float(f) => out.push(*f),
            other => {
                return Err(err(
                    "类型错误",
                    format!("「{name}」里第 {} 个不是数字：{}", i + 1, crate::display(other)),
                ))
            }
        }
    }
    Ok(out)
}

fn need_nonempty(name: &str, xs: Vec<f64>) -> Result<Vec<f64>, crate::JishiError> {
    if xs.is_empty() {
        return Err(err(
            "值错误",
            format!("「{name}」要至少有一个数字，现在是空的"),
        ));
    }
    Ok(xs)
}

/// 整数判定：**只有 `Val::Int` 才算**（`Val::Float(2.0)` 不算——
/// 与 Python 侧 `isinstance(v, int)` 一致）。
fn as_int_arg(name: &str, v: &Val) -> Result<i128, crate::JishiError> {
    match v {
        Val::Int(n) => Ok(*n),
        _ => Err(err("类型错误", format!("「{name}」要传整数"))),
    }
}

/// 数值参数（`分位数` 的 p、`加权平均` 的权重…）——收 Int / Float / Dec。
fn as_float_arg(name: &str, v: &Val) -> Result<f64, crate::JishiError> {
    as_num(v).map_err(|_| err("类型错误", format!("「{name}」要传数字")))
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! {
        "求和" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("求和", args, 1, 1)?;
            let xs = nums("求和", &args[0])?;
            let s: f64 = xs.iter().sum();
            Ok(f64v(s))
        },
        "平均" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("平均", args, 1, 1)?;
            let xs = need_nonempty("平均", nums("平均", &args[0])?)?;
            Ok(Val::Float(xs.iter().sum::<f64>() / xs.len() as f64))
        },
        "中位数" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("中位数", args, 1, 1)?;
            let mut xs = need_nonempty("中位数", nums("中位数", &args[0])?)?;
            xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let n = xs.len();
            let mid = n / 2;
            if n % 2 == 1 {
                Ok(f64v(xs[mid]))
            } else {
                Ok(Val::Float((xs[mid - 1] + xs[mid]) / 2.0))
            }
        },
        "众数" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("众数", args, 1, 1)?;
            let xs = need_nonempty("众数", nums("众数", &args[0])?)?;
            // 并列最多取**最先出现**的：按首次出现顺序扫，只在严格更多时替换
            let mut order: Vec<f64> = Vec::new();
            let mut counts: HashMap<String, (f64, usize)> = HashMap::new();
            for x in &xs {
                let k = format!("{:.17e}", x);
                match counts.get_mut(&k) {
                    Some(e) => e.1 += 1,
                    None => {
                        counts.insert(k, (*x, 1));
                        order.push(*x);
                    }
                }
            }
            let mut best = order[0];
            let mut best_n = counts[&format!("{:.17e}", best)].1;
            for x in &order {
                let n = counts[&format!("{:.17e}", x)].1;
                if n > best_n {
                    best = *x;
                    best_n = n;
                }
            }
            Ok(f64v(best))
        },
        "方差" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("方差", args, 1, 2)?;
            let xs = need_nonempty("方差", nums("方差", &args[0])?)?;
            let sample = matches!(args.get(1), Some(Val::Bool(true)));
            Ok(Val::Float(variance(&xs, sample)?))
        },
        "标准差" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("标准差", args, 1, 2)?;
            let xs = need_nonempty("标准差", nums("标准差", &args[0])?)?;
            let sample = matches!(args.get(1), Some(Val::Bool(true)));
            Ok(Val::Float(variance(&xs, sample)?.sqrt()))
        },
        "极差" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("极差", args, 1, 1)?;
            let xs = need_nonempty("极差", nums("极差", &args[0])?)?;
            let mx = xs.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let mn = xs.iter().cloned().fold(f64::INFINITY, f64::min);
            Ok(f64v(mx - mn))
        },
        "分位数" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("分位数", args, 2, 2)?;
            let p = as_float_arg("分位数", &args[1]).map_err(|_| {
                err("类型错误", "「分位数」的第二个参数要传 0 到 1 之间的小数")
            })?;
            if !(0.0..=1.0).contains(&p) {
                return Err(err(
                    "值错误",
                    format!("分位数要在 0 到 1 之间，现在是 {}", crate::display(&args[1])),
                ));
            }
            let mut xs = need_nonempty("分位数", nums("分位数", &args[0])?)?;
            xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            if xs.len() == 1 {
                return Ok(f64v(xs[0]));
            }
            let pos = p * (xs.len() - 1) as f64;
            let lo = pos.floor() as usize;
            let hi = pos.ceil() as usize;
            if lo == hi {
                return Ok(f64v(xs[lo]));
            }
            Ok(Val::Float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo as f64)))
        },
        "去极值平均" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("去极值平均", args, 1, 2)?;
            let k = if args.len() == 2 { as_int_arg("去掉个数", &args[1])? } else { 1 };
            if k < 0 {
                return Err(err("值错误", "「去掉个数」不能是负数"));
            }
            let mut xs = need_nonempty("去极值平均", nums("去极值平均", &args[0])?)?;
            xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let k = k as usize;
            if k * 2 >= xs.len() {
                return Err(err(
                    "值错误",
                    format!("去掉 {k} 个最高和最低之后就没数据了（一共 {} 个）", xs.len()),
                ));
            }
            let kept = &xs[k..xs.len() - k];
            Ok(Val::Float(kept.iter().sum::<f64>() / kept.len() as f64))
        },
        "加权平均" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("加权平均", args, 2, 2)?;
            let xs = need_nonempty("加权平均", nums("加权平均", &args[0])?)?;
            let ws = nums("加权平均的权重", &args[1])?;
            if xs.len() != ws.len() {
                return Err(err(
                    "值错误",
                    format!(
                        "数据和权重的个数不一样：{} 个数据、{} 个权重",
                        xs.len(),
                        ws.len()
                    ),
                ));
            }
            let total: f64 = ws.iter().sum();
            if total == 0.0 {
                return Err(err("值错误", "权重之和是 0，没法算加权平均"));
            }
            let s: f64 = xs.iter().zip(ws.iter()).map(|(x, w)| x * w).sum();
            Ok(Val::Float(s / total))
        },
    }
}

fn variance(xs: &[f64], sample: bool) -> Result<f64, crate::JishiError> {
    let n = xs.len();
    let m = xs.iter().sum::<f64>() / n as f64;
    let ss: f64 = xs.iter().map(|x| (x - m) * (x - m)).sum();
    if sample {
        if n < 2 {
            return Err(err("值错误", "样本方差至少要 2 个数（要除以 n-1）"));
        }
        return Ok(ss / (n - 1) as f64);
    }
    Ok(ss / n as f64)
}
