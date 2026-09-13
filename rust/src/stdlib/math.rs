//! 标准库 `数学`（Python 侧 `jishi/stdlib/数学.py`，18 个函数）。
//!
//! 薄封装 `f64` 的数学运算，但有几处**类型**要对齐 Python：
//!
//! - `平方` / `幂` 的**整数结果保持整数**（Python 的 `2 ** 10` 是 `int`）；
//! - `绝对值` 保类型（`绝对值(-3)` 仍是整数）；
//! - `四舍五入` 与 Python 的 `round` 一样用**银行家舍入**
//!   （`四舍五入(2.5)` 给 `2` 而不是 `3`），且位数 ≤ 0 时返回整数。

use std::collections::HashMap;

use super::{as_intish, as_num, need, need_range, table, with_values, R};
use crate::{display, err, JishiError, Val, VM};

/// `round` 的银行家舍入（半值向偶数），与 Python 的 `round` 一致。
///
/// 不走「先加 0.5 再取整」那种写法：那在二进制浮点下会系统性偏大
/// （`round(2.5)` 会成 3，而 Python 给 2）。Rust 的 `{:.N}` 格式化用的
/// 是**精确十进制舍入 + 平局取偶**，正好与 CPython 的 `round` 同规则。
fn round_half_even(x: f64, n: i32) -> f64 {
    if !x.is_finite() {
        return x;
    }
    if n >= 0 {
        format!("{:.*}", n as usize, x)
            .parse::<f64>()
            .unwrap_or(x)
    } else {
        let scale = 10f64.powi(-n);
        let scaled: f64 = format!("{:.0}", x / scale).parse().unwrap_or(x / scale);
        scaled * scale
    }
}

/// 整数的银行家舍入（`round(int, -n)`）。位数 ≥ 0 时整数不变。
fn round_int_half_even(v: i128, n: i32) -> i128 {
    if n >= 0 {
        return v;
    }
    if -n > 38 {
        return 0; // 10^38 已经超出 i128，任何整数都会被舍成 0
    }
    let scale = 10i128.pow((-n) as u32);
    let q = v / scale;
    let r = (v % scale).abs();
    let half = scale / 2;
    let sign = if v < 0 { -1 } else { 1 };
    let stepped = if r > half {
        q + sign
    } else if r < half {
        q
    } else if q % 2 == 0 {
        q
    } else {
        q + sign
    };
    // 别忘了乘回标度：`round(1250, -2)` 是 1200，不是 12
    stepped * scale
}

fn gcd(a: i128, b: i128) -> i128 {
    let (mut x, mut y) = (a.abs(), b.abs());
    while y != 0 {
        let t = x % y;
        x = y;
        y = t;
    }
    x
}

pub(crate) fn module() -> HashMap<String, Val> {
    let m = table! {
        "开方" => |args: &[Val], _vm: &mut VM| -> R {
            need("开方", args, 1)?;
            let x = as_num(&args[0]).map_err(|_| {
                err("类型错误", format!("「{}」不能开平方（需要数字）", display(&args[0])))
            })?;
            if x < 0.0 {
                return Err(err(
                    "类型错误",
                    format!("「{}」不能开平方（负数没有实数平方根）", display(&args[0])),
                ));
            }
            Ok(Val::Float(x.sqrt()))
        },
        "平方" => |args: &[Val], _vm: &mut VM| -> R {
            need("平方", args, 1)?;
            match &args[0] {
                Val::Int(i) => Ok(Val::Int(crate::big_ok(i.checked_mul(*i))?)),
                Val::Bool(b) => {
                    let i = if *b { 1i128 } else { 0 };
                    Ok(Val::Int(i * i))
                }
                Val::Dec(d) => Ok(Val::Dec(std::rc::Rc::new(d.mul(d)))),
                v => Ok(Val::Float(as_num(v)? * as_num(v)?)),
            }
        },
        "幂" => |args: &[Val], _vm: &mut VM| -> R {
            need("幂", args, 2)?;
            // 交给通用的 `**`：整数底数 + 非负整数指数会走整数快速幂、
            // 精确小数走十进制，两边行为与 `2 ** 10` 这种写法完全一致
            crate::binop(6, args[0].clone(), args[1].clone())
        },
        "绝对值" => |args: &[Val], _vm: &mut VM| -> R {
            need("绝对值", args, 1)?;
            match &args[0] {
                Val::Int(i) => Ok(Val::Int(crate::big_ok(i.checked_abs())?)),
                Val::Bool(b) => Ok(Val::Bool(*b)),
                Val::Float(f) => Ok(Val::Float(f.abs())),
                Val::Dec(d) => Ok(Val::Dec(std::rc::Rc::new(d.abs()))),
                v => Err(err("类型错误", format!("「{}」不能求绝对值", display(v)))),
            }
        },
        "向上取整" => |args: &[Val], _vm: &mut VM| -> R {
            need("向上取整", args, 1)?;
            let x = as_num(&args[0])?;
            Ok(Val::Int(to_big(x.ceil())?))
        },
        "向下取整" => |args: &[Val], _vm: &mut VM| -> R {
            need("向下取整", args, 1)?;
            let x = as_num(&args[0])?;
            Ok(Val::Int(to_big(x.floor())?))
        },
        "四舍五入" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("四舍五入", args, 1, 2)?;
            let n = if args.len() > 1 { as_intish(&args[1])? as i32 } else { 0 };
            match &args[0] {
                // Python 的 `round(整数, 位数)` 返回整数
                Val::Int(i) => Ok(Val::Int(round_int_half_even(*i, n))),
                Val::Bool(b) => {
                    let i = if *b { 1i128 } else { 0 };
                    Ok(Val::Int(round_int_half_even(i, n)))
                }
                v => {
                    let x = as_num(v).map_err(|_| {
                        err("类型错误", format!("「{}」不能四舍五入", display(v)))
                    })?;
                    if n <= 0 {
                        // 位数 ≤ 0 时 Python 再套一层 `round(...)`，得到整数
                        Ok(Val::Int(round_half_even(x, n) as i128))
                    } else {
                        Ok(Val::Float(round_half_even(x, n)))
                    }
                }
            }
        },
        "正弦" => |args: &[Val], _vm: &mut VM| -> R {
            need("正弦", args, 1)?;
            Ok(Val::Float(as_num(&args[0])?.sin()))
        },
        "余弦" => |args: &[Val], _vm: &mut VM| -> R {
            need("余弦", args, 1)?;
            Ok(Val::Float(as_num(&args[0])?.cos()))
        },
        "正切" => |args: &[Val], _vm: &mut VM| -> R {
            need("正切", args, 1)?;
            Ok(Val::Float(as_num(&args[0])?.tan()))
        },
        "角度转弧度" => |args: &[Val], _vm: &mut VM| -> R {
            need("角度转弧度", args, 1)?;
            Ok(Val::Float(as_num(&args[0])?.to_radians()))
        },
        "对数" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("对数", args, 1, 2)?;
            let x = as_num(&args[0])?;
            let base = if args.len() > 1 { as_num(&args[1])? } else { std::f64::consts::E };
            if x <= 0.0 || base <= 0.0 || base == 1.0 {
                // 文案对齐 Python：底数不传时就是自然常数本身的值
                return Err(err(
                    "类型错误",
                    format!("「{}」不能求以 {} 为底的对数", display(&args[0]), base),
                ));
            }
            Ok(Val::Float(x.ln() / base.ln()))
        },
        "常用对数" => |args: &[Val], _vm: &mut VM| -> R {
            need("常用对数", args, 1)?;
            let x = as_num(&args[0])?;
            if x <= 0.0 {
                return Err(err(
                    "类型错误",
                    format!("「{}」不能求常用对数", display(&args[0])),
                ));
            }
            Ok(Val::Float(x.log10()))
        },
        "最大公约数" => |args: &[Val], _vm: &mut VM| -> R {
            need("最大公约数", args, 2)?;
            Ok(Val::Int(gcd(as_intish(&args[0])?, as_intish(&args[1])?)))
        },
        "最小公倍数" => |args: &[Val], _vm: &mut VM| -> R {
            need("最小公倍数", args, 2)?;
            let a = as_intish(&args[0])?;
            let b = as_intish(&args[1])?;
            if a == 0 || b == 0 {
                return Ok(Val::Int(0));
            }
            // 先除后乘，免得中间结果白白溢出（Python 的 lcm 也是这么算的）
            Ok(Val::Int(crate::big_ok((a / gcd(a, b)).checked_mul(b.abs()))?))
        },
        "阶乘" => |args: &[Val], _vm: &mut VM| -> R {
            need("阶乘", args, 1)?;
            let n = as_intish(&args[0])?;
            if n < 0 {
                return Err(err(
                    "类型错误",
                    format!("「{}」不能求阶乘（需要非负整数）", display(&args[0])),
                ));
            }
            let mut r: i128 = 1;
            let mut i: i128 = 2;
            while i <= n {
                r = crate::big_ok(r.checked_mul(i))?;
                i += 1;
            }
            Ok(Val::Int(r))
        },
    };
    with_values!(m,
        "圆周率" => Val::Float(std::f64::consts::PI),
        "自然常数" => Val::Float(std::f64::consts::E),
    )
}

/// `f64` → `i128`（取整后的值一定在这个范围；否则报错而不是静默截断）。
fn to_big(f: f64) -> Result<i128, JishiError> {
    if !f.is_finite() || f > i128::MAX as f64 || f < i128::MIN as f64 {
        return Err(err("值错误", "数值太大，超出了整数能表示的范围"));
    }
    Ok(f as i128)
}
