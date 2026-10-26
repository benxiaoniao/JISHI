//! 标准库 `向量`（Python 侧 `jishi/stdlib/向量.py`，11 个函数）。
//!
//! 一列数字上的逐元素运算与点积。**报错措辞与 Python 侧逐字一致**——
//! 对拍会逐字节比 stdout 与报错文本。
//!
//! ## 为什么这里要在 Rust 里再写一遍
//!
//! M139 时它是「用基石写的」（`stdlib-jishi/向量.jsh`，一份代码五引擎共用），
//! 但**一万元素相加在 Python VM 上要 488 毫秒** —— 一万次「取下标 + 加法 +
//! 追加」全走解释器。M140 按用户拍板把这类**批处理下沉成宿主原语**：
//! `.jsh` 删掉，Python 用 `stdlib/向量.py`、Rust 用本文件、Node 用 `vm.js`。
//! 循环回到宿主原生代码里，实测 488 → 3 毫秒（Python VM）。
//!
//! ⚠️ 因此「三份实现」是**有意的取舍**，不是遗留：见 `CHANGELOG.md` 的 M140。
//!
//! ⚠️ 因此这里有一条硬要求：**每个元素是整数还是小数，必须与 Python 一致**。
//! 不能像 `统计` 那样一律转 `f64` 再在输出时猜（`[1,2]+[10.0,20.0]` 在 Python
//! 里是 `[11.0, 22.0]`，猜成 `[11, 22]` 就错了）⇒ 逐元素保留 `Val` 的类型。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{as_num, need_range, table, to_vec, R};
use crate::{err, Val};

/// 取数值（调用前已经由 `nums` 校验过是整数/小数，所以这里不会失败）。
fn f64of(v: &Val) -> f64 {
    as_num(v).unwrap_or(0.0)
}

/// 取一列数字（**保留整数/小数**），报错措辞与 Python 侧 `_nums` 逐字一致。
fn nums(name: &str, v: &Val) -> Result<Vec<Val>, crate::JishiError> {
    match v {
        Val::Str(_) | Val::Bytes(_) => {
            return Err(err(
                "类型错误",
                format!("「{name}」要传一列数字，不能传文本"),
            ))
        }
        _ => {}
    }
    let items = to_vec(v)
        .map_err(|_| err("类型错误", format!("「{name}」要传一列数字（列表）")))?;
    let mut out = Vec::with_capacity(items.len());
    for (i, x) in items.iter().enumerate() {
        match x {
            Val::Int(_) | Val::Float(_) => out.push(x.clone()),
            // `布尔` 不算数字（与 Python 侧显式排除 bool 一致）
            _ => {
                return Err(err(
                    "类型错误",
                    format!("「{name}」里第 {} 个不是数字：{}", i + 1, crate::display(x)),
                ))
            }
        }
    }
    Ok(out)
}

fn 同长(a: &[Val], b: &[Val], name: &str) -> Result<(), crate::JishiError> {
    if a.len() != b.len() {
        return Err(err(
            "值错误",
            format!(
                "「{name}」要求两列数字**长度相同**，实际是 {} 和 {}（长度不等时逐元素算会错位，所以这里直接报错）",
                a.len(),
                b.len()
            ),
        ));
    }
    Ok(())
}

fn 要非空(mut xs: Vec<Val>, name: &str) -> Result<Vec<Val>, crate::JishiError> {
    if xs.is_empty() {
        return Err(err("值错误", format!("「{name}」要至少有一个数，现在是空的")));
    }
    xs.shrink_to_fit();
    Ok(xs)
}

/// 两个数做一次算术：**两个都是整数就还整数**，否则小数（与 Python 的规则一致）。
fn 算(a: &Val, b: &Val, 运算: char) -> Val {
    if let (Val::Int(x), Val::Int(y)) = (a, b) {
        let r = match 运算 {
            '+' => x.checked_add(*y),
            '-' => x.checked_sub(*y),
            '*' => x.checked_mul(*y),
            _ => None,
        };
        if let Some(v) = r {
            return Val::Int(v);
        }
        // 整数溢出就退回小数（Python 那边是大整数，行为差别只在极端值上）
    }
    let (x, y) = (f64of(a), f64of(b));
    Val::Float(match 运算 {
        '+' => x + y,
        '-' => x - y,
        '*' => x * y,
        _ => x,
    })
}

fn 取整数参数(name: &str, v: &Val, 哪: &str) -> Result<i128, crate::JishiError> {
    match v {
        Val::Int(n) => Ok(*n),
        _ => Err(err(
            "类型错误",
            format!("「{name}」的{哪}要是整数，给的是 {}", crate::display(v)),
        )),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "向量";
        "构造" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("构造", args, 1, 2)?;
            let n = 取整数参数("构造", &args[0], "个数")?;
            if n < 0 {
                return Err(err("值错误", format!("「构造」的个数不能是负数（给的是 {n}）")));
            }
            let 初值 = match args.get(1) {
                None => Val::Int(0),
                Some(Val::Int(_)) | Some(Val::Float(_)) => args[1].clone(),
                Some(other) => {
                    return Err(err(
                        "类型错误",
                        format!("「构造」的初值要是数字，给的是 {}", crate::display(other)),
                    ))
                }
            };
            let mut out = Vec::with_capacity(n as usize);
            for _ in 0..n {
                out.push(初值.clone());
            }
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(out))))
        },
        "加" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("加", args, 2, 2)?;
            let a = nums("加", &args[0])?;
            let b = nums("加", &args[1])?;
            同长(&a, &b, "加")?;
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(
                a.iter().zip(&b).map(|(x, y)| 算(x, y, '+')).collect(),
            ))))
        },
        "减" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("减", args, 2, 2)?;
            let a = nums("减", &args[0])?;
            let b = nums("减", &args[1])?;
            同长(&a, &b, "减")?;
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(
                a.iter().zip(&b).map(|(x, y)| 算(x, y, '-')).collect(),
            ))))
        },
        "标量乘" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("标量乘", args, 2, 2)?;
            let a = nums("标量乘", &args[0])?;
            match &args[1] {
                Val::Int(_) | Val::Float(_) => {}
                other => {
                    return Err(err(
                        "类型错误",
                        format!("「标量乘」的倍数要是数字，给的是 {}", crate::display(other)),
                    ))
                }
            }
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(
                a.iter().map(|x| 算(x, &args[1], '*')).collect(),
            ))))
        },
        "点积" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("点积", args, 2, 2)?;
            let a = nums("点积", &args[0])?;
            let b = nums("点积", &args[1])?;
            同长(&a, &b, "点积")?;
            let mut 合 = Val::Int(0);
            for (x, y) in a.iter().zip(&b) {
                合 = 算(&合, &算(x, y, '*'), '+');
            }
            Ok(合)
        },
        "和" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("和", args, 1, 1)?;
            let a = nums("和", &args[0])?;
            let mut 合 = Val::Int(0);
            for x in &a {
                合 = 算(&合, x, '+');
            }
            Ok(合)
        },
        "均值" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("均值", args, 1, 1)?;
            let a = 要非空(nums("均值", &args[0])?, "均值")?;
            let mut 合 = 0.0f64;
            for x in &a {
                合 += f64of(x);
            }
            // Python 里 `/` 是真除法 ⇒ 结果**一定是小数**
            Ok(Val::Float(合 / a.len() as f64))
        },
        "最大" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("最大", args, 1, 1)?;
            let a = 要非空(nums("最大", &args[0])?, "最大")?;
            let mut 结果 = a[0].clone();
            for x in &a {
                if f64of(x) > f64of(&结果) {
                    结果 = x.clone();
                }
            }
            Ok(结果)
        },
        "最小" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("最小", args, 1, 1)?;
            let a = 要非空(nums("最小", &args[0])?, "最小")?;
            let mut 结果 = a[0].clone();
            for x in &a {
                if f64of(x) < f64of(&结果) {
                    结果 = x.clone();
                }
            }
            Ok(结果)
        },
        "维数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("维数", args, 1, 1)?;
            let a = nums("维数", &args[0])?;
            Ok(Val::Int(a.len() as i128))
        },
        "切片" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("切片", args, 2, 3)?;
            let a = nums("切片", &args[0])?;
            let 总数 = a.len() as i128;
            let 起 = 取整数参数("切片", &args[1], "起")?;
            let 尾 = match args.get(2) {
                None => 总数,
                Some(v) => 取整数参数("切片", v, "止")?,
            };
            if 起 < 0 || 尾 < 0 {
                return Err(err(
                    "值错误",
                    format!("「切片」的起和止都不能是负数（给的是 {起} 和 {尾}）"),
                ));
            }
            if 起 > 尾 {
                return Err(err(
                    "值错误",
                    format!("「切片」的起 {起} 不能大于止 {尾}"),
                ));
            }
            if 尾 > 总数 {
                return Err(err(
                    "值错误",
                    format!("「切片」的止 {尾} 超出维数 {总数}"),
                ));
            }
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(
                a[起 as usize..尾 as usize].to_vec(),
            ))))
        },
    }
}
