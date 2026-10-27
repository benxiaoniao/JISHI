//! 标准库 `矩阵`（Python 侧 `jishi/stdlib/矩阵.py`，8 个函数）。
//!
//! 构造 / 单位 / 行数 / 列数 / 转置 / 乘 / 行列式 / 解方程。
//! **报错措辞与 Python 侧逐字一致**——对拍会逐字节比 stdout 与报错文本。
//!
//! ## 数值上的两条决定（与 Python 侧同一套，改一处要三处一起改）
//!
//! 1. **整数矩阵的行列式给精确整数**（Bareiss 无分数消元）；
//! 2. **整数方程组给精确解**（克莱姆法则 + 上面那个行列式）。
//!
//! ⚠️ 两个例外都要与 Python 保持一致，否则五引擎对拍立刻红：
//! - **算不出来（i128 溢出）** ⇒ 退回浮点（Python 那边是「绝对值超过 2^53 才退」，
//!   而 i128 溢出时绝对值必然远大于 2^53，两边结论一致）；
//! - **结果绝对值超过 2^53** ⇒ 也给浮点。这条是为了跟 Node 对齐：
//!   JS 的安全整数是 2^53，再大就丢精度，与其三个宿主各说各话，不如统一定界。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{as_num, need_range, table, to_vec, R};
use crate::{err, Val};

/// 超过这个绝对值就退回浮点（JS 的安全整数上限 2^53）。三个宿主同一个界。
const 安全整数上限: i128 = 9_007_199_254_740_992;

fn 是数(v: &Val) -> bool {
    matches!(v, Val::Int(_) | Val::Float(_))
}

/// 取矩阵（`Vec<行>`），报错措辞与 Python 侧逐字一致。
fn 取矩阵(name: &str, v: &Val) -> Result<Vec<Vec<Val>>, crate::JishiError> {
    if let Val::Str(_) | Val::Bytes(_) = v {
        return Err(err(
            "类型错误",
            format!("「{name}」要传一个矩阵（列表的列表），不能传文本"),
        ));
    }
    let 行们 = to_vec(v)
        .map_err(|_| err("类型错误", format!("「{name}」要传一个矩阵（列表的列表）")))?;
    if 行们.is_empty() {
        return Err(err("值错误", format!("「{name}」要至少有一行，现在是空的")));
    }
    let mut 出: Vec<Vec<Val>> = Vec::with_capacity(行们.len());
    let mut 宽: Option<usize> = None;
    for (i, 行) in 行们.iter().enumerate() {
        if let Val::Str(_) | Val::Bytes(_) = 行 {
            return Err(err(
                "类型错误",
                format!("「{name}」第 {} 行不是列表（是文本）", i + 1),
            ));
        }
        let 列们 = to_vec(行)
            .map_err(|_| err("类型错误", format!("「{name}」第 {} 行不是列表", i + 1)))?;
        match 宽 {
            None => 宽 = Some(列们.len()),
            Some(w) if w != 列们.len() => {
                return Err(err(
                    "值错误",
                    format!(
                        "「{name}」每行的长度要相同：第 1 行有 {} 个，第 {} 行有 {} 个",
                        w,
                        i + 1,
                        列们.len()
                    ),
                ))
            }
            _ => {}
        }
        for (j, x) in 列们.iter().enumerate() {
            if !是数(x) {
                return Err(err(
                    "类型错误",
                    format!("「{name}」第 {} 行第 {} 个不是数字：{}", i + 1, j + 1, crate::display(x)),
                ));
            }
        }
        出.push(列们);
    }
    Ok(出)
}

fn 要方形(name: &str, m: &[Vec<Val>]) -> Result<(), crate::JishiError> {
    if m.len() != m[0].len() {
        return Err(err(
            "值错误",
            format!("「{name}」只对方阵有定义，这个矩阵是 {}×{}", m.len(), m[0].len()),
        ));
    }
    Ok(())
}

fn 取整数(name: &str, v: &Val, 中文: &str) -> Result<i128, crate::JishiError> {
    match v {
        Val::Int(n) => Ok(*n),
        _ => Err(err(
            "类型错误",
            format!("「{name}」的{中文}要是整数，给的是 {}", crate::display(v)),
        )),
    }
}

/// 两个数做一次算术：两个都是整数就还整数（与 Python 规则一致）。
fn 算(a: &Val, b: &Val, op: char) -> Val {
    if let (Val::Int(x), Val::Int(y)) = (a, b) {
        let r = match op {
            '+' => x.checked_add(*y),
            '-' => x.checked_sub(*y),
            '*' => x.checked_mul(*y),
            _ => None,
        };
        if let Some(v) = r {
            return Val::Int(v);
        }
    }
    let (x, y) = (as_num(a).unwrap_or(0.0), as_num(b).unwrap_or(0.0));
    Val::Float(match op {
        '+' => x + y,
        '-' => x - y,
        '*' => x * y,
        _ => x,
    })
}

fn 全整数(m: &[Vec<Val>]) -> bool {
    m.iter().all(|行| 行.iter().all(|v| matches!(v, Val::Int(_))))
}

/// Bareiss 无分数消元（i128 精确）。溢出返回 `None`（调用方退浮点）。
fn 整数行列式(a: &[Vec<i128>]) -> Option<i128> {
    let n = a.len();
    let mut m: Vec<Vec<i128>> = a.to_vec();
    if n == 1 {
        return Some(m[0][0]);
    }
    let mut 上一: i128 = 1;
    let mut 符号: i128 = 1;
    for k in 0..n - 1 {
        if m[k][k] == 0 {
            let mut 换 = None;
            for r in k + 1..n {
                if m[r][k] != 0 {
                    换 = Some(r);
                    break;
                }
            }
            match 换 {
                None => return Some(0),
                Some(r) => {
                    m.swap(k, r);
                    符号 = -符号;
                }
            }
        }
        let 主 = m[k][k];
        for i in k + 1..n {
            for j in k + 1..n {
                let 左 = m[i][j].checked_mul(主)?;
                let 右 = m[i][k].checked_mul(m[k][j])?;
                m[i][j] = (左.checked_sub(右)?).checked_div(上一)?;
            }
        }
        上一 = 主;
    }
    Some(符号.checked_mul(m[n - 1][n - 1])?)
}

fn 浮点行列式(a: &[Vec<f64>]) -> f64 {
    let n = a.len();
    let mut m: Vec<Vec<f64>> = a.to_vec();
    let mut 结果 = 1.0f64;
    for k in 0..n {
        let mut 主行 = k;
        for r in k + 1..n {
            if m[r][k].abs() > m[主行][k].abs() {
                主行 = r;
            }
        }
        if m[主行][k].abs() < 1e-12 {
            return 0.0;
        }
        if 主行 != k {
            m.swap(k, 主行);
            结果 = -结果;
        }
        结果 *= m[k][k];
        for r in k + 1..n {
            let 倍 = m[r][k] / m[k][k];
            for c in k..n {
                m[r][c] -= 倍 * m[k][c];
            }
        }
    }
    结果
}

/// 精确整数结果 → `Val`；超过安全整数上限就给浮点（与 Python / Node 同一口径）。
fn 定形(v: i128) -> Val {
    if v.abs() > 安全整数上限 {
        Val::Float(v as f64)
    } else {
        Val::Int(v)
    }
}

fn 取整矩阵(m: &[Vec<Val>]) -> Option<Vec<Vec<i128>>> {
    let mut 出 = Vec::with_capacity(m.len());
    for 行 in m {
        let mut r = Vec::with_capacity(行.len());
        for v in 行 {
            match v {
                Val::Int(n) => r.push(*n),
                _ => return None,
            }
        }
        出.push(r);
    }
    Some(出)
}

fn 取浮点矩阵(m: &[Vec<Val>]) -> Vec<Vec<f64>> {
    m.iter()
        .map(|行| 行.iter().map(|v| as_num(v).unwrap_or(0.0)).collect())
        .collect()
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "矩阵";
        "构造" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("构造", args, 2, 3)?;
            let r = 取整数("构造", &args[0], "行数")?;
            let c = 取整数("构造", &args[1], "列数")?;
            if r <= 0 || c <= 0 {
                return Err(err(
                    "值错误",
                    format!("「构造」的行数和列数都要大于 0（给的是 {r} 和 {c}）"),
                ));
            }
            let 初值 = match args.get(2) {
                None => Val::Int(0),
                Some(v) if 是数(v) => v.clone(),
                Some(v) => {
                    return Err(err(
                        "类型错误",
                        format!("「构造」的初值要是数字，给的是 {}", crate::display(v)),
                    ))
                }
            };
            let mut 出 = Vec::with_capacity(r as usize);
            for _ in 0..r {
                let mut 行 = Vec::with_capacity(c as usize);
                for _ in 0..c {
                    行.push(初值.clone());
                }
                出.push(Val::List(std::rc::Rc::new(std::cell::RefCell::new(行))));
            }
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(出))))
        },
        "单位" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("单位", args, 1, 1)?;
            let n = 取整数("单位", &args[0], "阶")?;
            if n <= 0 {
                return Err(err("值错误", format!("「单位」的阶要大于 0（给的是 {n}）")));
            }
            let n = n as usize;
            let mut 出 = Vec::with_capacity(n);
            for i in 0..n {
                let mut 行 = Vec::with_capacity(n);
                for j in 0..n {
                    行.push(Val::Int(if i == j { 1 } else { 0 }));
                }
                出.push(Val::List(std::rc::Rc::new(std::cell::RefCell::new(行))));
            }
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(出))))
        },
        "行数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("行数", args, 1, 1)?;
            let m = 取矩阵("行数", &args[0])?;
            Ok(Val::Int(m.len() as i128))
        },
        "列数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("列数", args, 1, 1)?;
            let m = 取矩阵("列数", &args[0])?;
            Ok(Val::Int(m[0].len() as i128))
        },
        "转置" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("转置", args, 1, 1)?;
            let a = 取矩阵("转置", &args[0])?;
            let (行数, 列数) = (a.len(), a[0].len());
            let mut 出 = Vec::with_capacity(列数);
            for j in 0..列数 {
                let mut 行 = Vec::with_capacity(行数);
                for i in 0..行数 {
                    行.push(a[i][j].clone());
                }
                出.push(Val::List(std::rc::Rc::new(std::cell::RefCell::new(行))));
            }
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(出))))
        },
        "乘" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("乘", args, 2, 2)?;
            let a = 取矩阵("乘", &args[0])?;
            let b = 取矩阵("乘", &args[1])?;
            if a[0].len() != b.len() {
                return Err(err(
                    "值错误",
                    format!(
                        "「乘」要求左矩阵的列数等于右矩阵的行数，实际是 {}×{} 乘 {}×{}",
                        a.len(),
                        a[0].len(),
                        b.len(),
                        b[0].len()
                    ),
                ));
            }
            let mut 出 = Vec::with_capacity(a.len());
            for i in 0..a.len() {
                let mut 行 = Vec::with_capacity(b[0].len());
                for j in 0..b[0].len() {
                    let mut 合 = Val::Int(0);
                    for k in 0..b.len() {
                        let 积 = 算(&a[i][k], &b[k][j], '*');
                        合 = 算(&合, &积, '+');
                    }
                    行.push(合);
                }
                出.push(Val::List(std::rc::Rc::new(std::cell::RefCell::new(行))));
            }
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(出))))
        },
        "行列式" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("行列式", args, 1, 1)?;
            let a = 取矩阵("行列式", &args[0])?;
            要方形("行列式", &a)?;
            if 全整数(&a) {
                if let Some(整) = 取整矩阵(&a) {
                    if let Some(d) = 整数行列式(&整) {
                        return Ok(定形(d));
                    }
                }
            }
            Ok(Val::Float(浮点行列式(&取浮点矩阵(&a))))
        },
        "解方程" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("解方程", args, 2, 2)?;
            let a = 取矩阵("解方程", &args[0])?;
            要方形("解方程", &a)?;
            let n = a.len();
            let 右值 = 取右端(&args[1], n)?;

            if 全整数(&a) && 右值.iter().all(|v| matches!(v, Val::Int(_))) {
                if let Some(解) = 精确解(&a, &右值) {
                    return Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(解))));
                }
            }

            let mut m = 取浮点矩阵(&a);
            let mut 右: Vec<f64> = 右值.iter().map(|v| as_num(v).unwrap_or(0.0)).collect();
            for k in 0..n {
                let mut 主行 = k;
                for r in k + 1..n {
                    if m[r][k].abs() > m[主行][k].abs() {
                        主行 = r;
                    }
                }
                if m[主行][k].abs() < 1e-12 {
                    return Err(err(
                        "值错误",
                        "「解方程」这个方程组没有唯一解（系数矩阵消元后出现全 0 行）",
                    ));
                }
                if 主行 != k {
                    m.swap(k, 主行);
                    右.swap(k, 主行);
                }
                for r in 0..n {
                    if r == k {
                        continue;
                    }
                    let 倍 = m[r][k] / m[k][k];
                    if 倍 == 0.0 {
                        continue;
                    }
                    for c in k..n {
                        m[r][c] -= 倍 * m[k][c];
                    }
                    右[r] -= 倍 * 右[k];
                }
            }
            let mut 解 = Vec::with_capacity(n);
            for i in 0..n {
                解.push(Val::Float(右[i] / m[i][i]));
            }
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(解))))
        },
    }
}

/// 解方程右边那一列（长度必须等于 n），措辞与 Python 侧一致。
fn 取右端(v: &Val, n: usize) -> Result<Vec<Val>, crate::JishiError> {
    if let Val::Str(_) | Val::Bytes(_) = v {
        return Err(err(
            "类型错误",
            "「解方程」右边的常数项要传一列数，不能传文本",
        ));
    }
    let 列 = to_vec(v).map_err(|_| {
        err("类型错误", "「解方程」右边的常数项要传一列数（列表）")
    })?;
    if 列.len() != n {
        return Err(err(
            "值错误",
            format!(
                "「解方程」右边要有 {n} 个常数（与系数矩阵的行数一致），实际有 {} 个",
                列.len()
            ),
        ));
    }
    for (i, x) in 列.iter().enumerate() {
        if !是数(x) {
            return Err(err(
                "类型错误",
                format!("「解方程」右边第 {} 个不是数字：{}", i + 1, crate::display(x)),
            ));
        }
    }
    Ok(列)
}

/// 克莱姆法则：`x_i = det(A_i) / det(A)`。任何一步算不出来（溢出/超界）返回 `None`。
fn 精确解(a: &[Vec<Val>], b: &[Val]) -> Option<Vec<Val>> {
    let n = a.len();
    let 整甲 = 取整矩阵(a)?;
    let 底 = 整数行列式(&整甲)?;
    if 底 == 0 {
        // 奇异：交给调用方走浮点路径，那边会给出统一的中文报错
        return None;
    }
    if 底.abs() > 安全整数上限 {
        return None;
    }
    let mut 解 = Vec::with_capacity(n);
    for i in 0..n {
        let mut 列: Vec<Vec<i128>> = Vec::with_capacity(n);
        for r in 0..n {
            let mut 行 = Vec::with_capacity(n);
            for c in 0..n {
                行.push(if c == i {
                    match b[r] {
                        Val::Int(v) => v,
                        _ => return None,
                    }
                } else {
                    整甲[r][c]
                });
            }
            列.push(行);
        }
        let 分子 = 整数行列式(&列)?;
        if 分子.abs() > 安全整数上限 {
            return None;
        }
        if 分子 % 底 == 0 {
            解.push(Val::Int(分子 / 底));
        } else {
            解.push(Val::Float(分子 as f64 / 底 as f64));
        }
    }
    Some(解)
}
