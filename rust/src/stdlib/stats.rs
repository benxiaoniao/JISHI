//! 标准库 `统计`（Python 侧 `jishi/stdlib/统计.py`，10 个函数）。
//!
//! 一组数据的集中趋势与离散程度。**报错措辞与 Python 侧逐字一致**——
//! 对拍会逐字节比 stdout 与报错文本。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{as_num, as_text, need_range, table, to_vec, R};
use crate::{err, Val};

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

fn 两列(name: &str, a: &Val, b: &Val) -> Result<(Vec<f64>, Vec<f64>), crate::JishiError> {
    let xs = nums(name, a)?;
    let ys = nums(name, b)?;
    if xs.len() != ys.len() {
        return Err(err(
            "值错误",
            format!(
                "「{name}」两组数据的个数不一样：{} 和 {}",
                xs.len(),
                ys.len()
            ),
        ));
    }
    if xs.len() < 2 {
        return Err(err("值错误", format!("「{name}」至少要 2 组数据")));
    }
    Ok((xs, ys))
}

fn 均值(xs: &[f64]) -> f64 {
    xs.iter().sum::<f64>() / xs.len() as f64
}

/// 升序秩（并列取平均秩），返回与输入**同序**的秩。
fn 秩表(xs: &[f64]) -> Vec<f64> {
    let mut 序: Vec<usize> = (0..xs.len()).collect();
    序.sort_by(|a, b| xs[*a].partial_cmp(&xs[*b]).unwrap_or(std::cmp::Ordering::Equal));
    let mut 出 = vec![0.0f64; xs.len()];
    let mut i = 0usize;
    while i < 序.len() {
        let mut j = i;
        while j + 1 < 序.len() && xs[序[j + 1]] == xs[序[i]] {
            j += 1;
        }
        let 平均 = (i + 1 + j + 1) as f64 / 2.0;
        for k in i..=j {
            出[序[k]] = 平均;
        }
        i = j + 1;
    }
    出
}

fn 浮点列(xs: Vec<f64>) -> Val {
    crate::list_new(xs.into_iter().map(Val::Float).collect())
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "统计";
        "求和" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("求和", args, 1, 1)?;
            let xs = nums("求和", &args[0])?;
            let s: f64 = xs.iter().sum();
            Ok(f64v(s))
        },
        "平均" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("平均", args, 1, 1)?;
            let xs = need_nonempty("平均", nums("平均", &args[0])?)?;
            Ok(Val::Float(xs.iter().sum::<f64>() / xs.len() as f64))
        },
        "中位数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        "众数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        "方差" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("方差", args, 1, 2)?;
            let xs = need_nonempty("方差", nums("方差", &args[0])?)?;
            let sample = matches!(args.get(1), Some(Val::Bool(true)));
            Ok(Val::Float(variance(&xs, sample)?))
        },
        "标准差" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("标准差", args, 1, 2)?;
            let xs = need_nonempty("标准差", nums("标准差", &args[0])?)?;
            let sample = matches!(args.get(1), Some(Val::Bool(true)));
            Ok(Val::Float(variance(&xs, sample)?.sqrt()))
        },
        "极差" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("极差", args, 1, 1)?;
            let xs = need_nonempty("极差", nums("极差", &args[0])?)?;
            let mx = xs.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let mn = xs.iter().cloned().fold(f64::INFINITY, f64::min);
            Ok(f64v(mx - mn))
        },
        "分位数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        "去极值平均" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        "加权平均" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        // ── M192 ──
        "协方差" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("协方差", args, 2, 3)?;
            let (xs, ys) = 两列("协方差", &args[0], &args[1])?;
            let n = xs.len();
            let mx = 均值(&xs);
            let my = 均值(&ys);
            let mut s = 0.0f64;
            for i in 0..n {
                s += (xs[i] - mx) * (ys[i] - my);
            }
            let 样本 = matches!(args.get(2), Some(Val::Bool(true)));
            let 果 = if 样本 { s / (n - 1) as f64 } else { s / n as f64 };
            Ok(Val::Float(果))
        },
        "秩" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("秩", args, 1, 1)?;
            let xs = nums("秩", &args[0])?;
            if xs.is_empty() {
                return Err(err("值错误", "「秩」要至少有一个数字，现在是空的"));
            }
            Ok(浮点列(秩表(&xs)))
        },
        "相关系数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("相关系数", args, 2, 3)?;
            let 名 = if args.len() > 2 { as_text(&args[2]) } else { "皮尔逊".to_string() };
            if 名 != "皮尔逊" && 名 != "斯皮尔曼" {
                return Err(err(
                    "类型错误",
                    format!("「相关系数」不认识的方法「{名}」—— 只能是：皮尔逊 / 斯皮尔曼"),
                ));
            }
            let (mut xs, mut ys) = 两列("相关系数", &args[0], &args[1])?;
            if 名 == "斯皮尔曼" {
                xs = 秩表(&xs);
                ys = 秩表(&ys);
            }
            let mx = 均值(&xs);
            let my = 均值(&ys);
            let (mut sxy, mut sxx, mut syy) = (0.0f64, 0.0f64, 0.0f64);
            for i in 0..xs.len() {
                let dx = xs[i] - mx;
                let dy = ys[i] - my;
                sxy += dx * dy;
                sxx += dx * dx;
                syy += dy * dy;
            }
            if sxx == 0.0 || syy == 0.0 {
                return Err(err(
                    "值错误",
                    "「相关系数」算不出来：有一组数据的值全都一样（标准差是 0）",
                ));
            }
            Ok(Val::Float(sxy / (sxx * syy).sqrt()))
        },
        "线性回归" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("线性回归", args, 2, 2)?;
            let (xs, ys) = 两列("线性回归", &args[0], &args[1])?;
            let n = xs.len();
            let mx = 均值(&xs);
            let my = 均值(&ys);
            let (mut sxy, mut sxx) = (0.0f64, 0.0f64);
            for i in 0..n {
                let dx = xs[i] - mx;
                sxy += dx * (ys[i] - my);
                sxx += dx * dx;
            }
            if sxx == 0.0 {
                return Err(err(
                    "值错误",
                    "「线性回归」算不出来：甲那组数据全都一样，拟合不出直线",
                ));
            }
            let 斜率 = sxy / sxx;
            let 截距 = my - 斜率 * mx;
            let mut 残差: Vec<f64> = Vec::with_capacity(n);
            let mut ss总 = 0.0f64;
            for i in 0..n {
                let r = ys[i] - (斜率 * xs[i] + 截距);
                残差.push(r);
                let d = ys[i] - my;
                ss总 += d * d;
            }
            let ss残: f64 = 残差.iter().map(|r| r * r).sum();
            let r方 = if ss总 == 0.0 { 1.0 } else { 1.0 - ss残 / ss总 };
            let pairs = vec![
                (Val::Str("斜率".into()), Val::Float(斜率)),
                (Val::Str("截距".into()), Val::Float(截距)),
                (Val::Str("R方".into()), Val::Float(r方)),
                (Val::Str("残差".into()), 浮点列(残差)),
            ];
            Ok(crate::dict_new(pairs))
        },
        "标准化" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("标准化", args, 1, 1)?;
            let xs = need_nonempty("标准化", nums("标准化", &args[0])?)?;
            let n = xs.len();
            let m = 均值(&xs);
            let mut ss = 0.0f64;
            for x in &xs {
                let d = x - m;
                ss += d * d;
            }
            let sd = (ss / n as f64).sqrt();
            if sd == 0.0 {
                return Err(err(
                    "值错误",
                    "「标准化」算不出来：这组数据的值全都一样（标准差是 0）",
                ));
            }
            Ok(浮点列(xs.iter().map(|x| (x - m) / sd).collect()))
        },
        "移动平均" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("移动平均", args, 1, 2)?;
            let 窗口 = match args.get(1) {
                None | Some(Val::None_) => 3i128,
                Some(Val::Int(v)) => *v,
                Some(other) => {
                    return Err(err(
                        "类型错误",
                        format!(
                            "「移动平均」的窗口要传整数，给的是「{}」",
                            crate::display(other)
                        ),
                    ))
                }
            };
            if 窗口 < 1 {
                return Err(err("值错误", "「移动平均」的窗口至少是 1"));
            }
            let 窗口 = 窗口 as usize;
            let xs = need_nonempty("移动平均", nums("移动平均", &args[0])?)?;
            if 窗口 > xs.len() {
                return Err(err(
                    "值错误",
                    format!(
                        "「移动平均」的窗口是 {窗口}，但一共只有 {} 个数",
                        xs.len()
                    ),
                ));
            }
            let mut 出: Vec<f64> = Vec::new();
            let mut 和 = 0.0f64;
            for i in 0..xs.len() {
                和 += xs[i];
                if i >= 窗口 {
                    和 -= xs[i - 窗口];
                }
                if i + 1 >= 窗口 {
                    出.push(和 / 窗口 as f64);
                }
            }
            Ok(浮点列(出))
        },
        "直方图" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("直方图", args, 1, 2)?;
            let 箱数 = match args.get(1) {
                None | Some(Val::None_) => 10i128,
                Some(Val::Int(v)) => *v,
                Some(other) => {
                    return Err(err(
                        "类型错误",
                        format!("「直方图」的箱数要传整数，给的是「{}」", crate::display(other)),
                    ))
                }
            };
            if 箱数 < 1 {
                return Err(err("值错误", "「直方图」的箱数至少是 1"));
            }
            let 箱数 = 箱数 as usize;
            let xs = need_nonempty("直方图", nums("直方图", &args[0])?)?;
            let mut lo = xs[0];
            let mut hi = xs[0];
            for x in &xs {
                if *x < lo {
                    lo = *x;
                }
                if *x > hi {
                    hi = *x;
                }
            }
            if lo == hi {
                let pairs = vec![
                    (Val::Str("边界".into()), crate::list_new(vec![Val::Float(lo), f64v(hi)])),
                    (Val::Str("计数".into()), crate::list_new(vec![Val::Int(xs.len() as i128)])),
                ];
                return Ok(crate::dict_new(pairs));
            }
            let 宽 = (hi - lo) / 箱数 as f64;
            let mut 边界: Vec<Val> = (0..箱数).map(|i| Val::Float(lo + 宽 * i as f64)).collect();
            边界.push(f64v(hi));
            let mut 计数 = vec![0i128; 箱数];
            for x in &xs {
                let mut k = ((x - lo) / 宽) as i128;
                if k >= 箱数 as i128 {
                    k = 箱数 as i128 - 1;
                }
                if k < 0 {
                    k = 0;
                }
                计数[k as usize] += 1;
            }
            let pairs = vec![
                (Val::Str("边界".into()), crate::list_new(边界)),
                (
                    Val::Str("计数".into()),
                    crate::list_new(计数.into_iter().map(Val::Int).collect()),
                ),
            ];
            Ok(crate::dict_new(pairs))
        },
        "百分位排名" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("百分位排名", args, 2, 2)?;
            let xs = nums("百分位排名", &args[0])?;
            if xs.is_empty() {
                return Err(err(
                    "值错误",
                    "「百分位排名」要至少有一个数字，现在是空的",
                ));
            }
            let t = match &args[1] {
                Val::Int(v) => *v as f64,
                Val::Float(f) => *f,
                other => {
                    return Err(err(
                        "类型错误",
                        format!(
                            "「百分位排名」的第二个参数要传一个数字，给的是「{}」",
                            crate::display(other)
                        ),
                    ))
                }
            };
            let 不高于 = xs.iter().filter(|x| **x <= t).count();
            Ok(Val::Float(不高于 as f64 / xs.len() as f64))
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
