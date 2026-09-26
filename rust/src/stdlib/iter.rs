//! 标准库 `迭代`（Python 侧 `jishi/stdlib/迭代.py`，12 个函数）。
//!
//! 遍历序列时的常用整理动作。`带下标` / `配对` 是**内建**（在 lib.rs 里，
//! 与 Python 的 enumerate / zip 同地位），本模块装它们之外的：分组、滑窗、
//! 去重、展开、取前/后、求和、计数、累积、组合、排列、笛卡尔积。

use std::collections::HashMap;

use super::{as_intish, need_range, table, to_vec, R};
use crate::{display, err, list_new, truthy, Val, VM};

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "迭代";
        "分组" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("分组", args, 2, 2)?;
            let items = to_vec(&args[0])?;
            let n = as_intish(&args[1])?;
            if n <= 0 {
                return Err(err(
                    "值错误",
                    format!("「分组」的每组个数要大于 0，得到了 {}", display(&args[1])),
                ));
            }
            Ok(list_new(
                items.chunks(n as usize).map(|c| list_new(c.to_vec())).collect(),
            ))
        },
        "滑窗" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("滑窗", args, 2, 3)?;
            let items = to_vec(&args[0])?;
            let n = as_intish(&args[1])?;
            let s = if args.len() == 3 { as_intish(&args[2])? } else { 1 };
            if n <= 0 {
                return Err(err(
                    "值错误",
                    format!("「滑窗」的窗口大小要大于 0，得到了 {}", display(&args[1])),
                ));
            }
            if s <= 0 {
                return Err(err(
                    "值错误",
                    format!("「滑窗」的步长要大于 0，得到了 {}", display(&args[2])),
                ));
            }
            let (n, s) = (n as usize, s as usize);
            let mut out: Vec<Val> = Vec::new();
            let mut i = 0usize;
            while i + n <= items.len() {
                out.push(list_new(items[i..i + n].to_vec()));
                i += s;
            }
            Ok(list_new(out))
        },
        "去重" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("去重", args, 1, 1)?;
            let items = to_vec(&args[0])?;
            let mut out: Vec<Val> = Vec::new();
            let mut 见过: Vec<String> = Vec::new();
            for x in items {
                let k = display(&x);
                if 见过.contains(&k) {
                    continue;
                }
                见过.push(k);
                out.push(x);
            }
            Ok(list_new(out))
        },
        "展开" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("展开", args, 1, 1)?;
            let items = to_vec(&args[0])?;
            let mut out: Vec<Val> = Vec::new();
            for seg in items {
                match to_vec(&seg) {
                    Ok(s) => out.extend(s),
                    Err(_) => out.push(seg),
                }
            }
            Ok(list_new(out))
        },
        "取前" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("取前", args, 2, 2)?;
            let items = to_vec(&args[0])?;
            let n = as_intish(&args[1])?.max(0) as usize;
            Ok(list_new(items.into_iter().take(n).collect()))
        },
        "取后" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("取后", args, 2, 2)?;
            let items = to_vec(&args[0])?;
            let n = as_intish(&args[1])?.max(0) as usize;
            let skip = items.len().saturating_sub(n);
            Ok(list_new(items.into_iter().skip(skip).collect()))
        },
        "求和" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("求和", args, 1, 1)?;
            let items = to_vec(&args[0])?;
            let mut 整 = 0i128;
            let mut 有小数 = false;
            let mut 小 = 0f64;
            for x in &items {
                match x {
                    Val::Int(i) => { 整 += i; }
                    Val::Bool(b) => { 整 += if *b { 1 } else { 0 }; }
                    Val::Float(f) => { 小 += f; 有小数 = true; }
                    other => {
                        return Err(err(
                            "类型错误",
                            format!("「求和」遇到了不能相加的「{}」", crate::type_label(other)),
                        ));
                    }
                }
            }
            if 有小数 {
                Ok(Val::Float(小 + 整 as f64))
            } else {
                Ok(Val::Int(整))
            }
        },
        "计数" => |args: &[Val], vm: &mut VM| -> R {
            need_range("计数", args, 2, 2)?;
            let items = to_vec(&args[0])?;
            let f = args[1].clone();
            let mut n = 0i128;
            for x in items {
                if truthy(vm.call_value(f.clone(), vec![x])?) {
                    n += 1;
                }
            }
            Ok(Val::Int(n))
        },
        // M51 补：这四个是 M50 第一批加的（「既有模块补缺」），当时只做了
        // Python 侧，宿主**静默缺失**。
        "累积" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("累积", args, 1, 2)?;
            let items = to_vec(&args[0])?;
            let mut acc = if args.len() > 1 { args[1].clone() } else { Val::Int(0) };
            let mut out: Vec<Val> = Vec::with_capacity(items.len());
            for x in items {
                // 用基石自己的 `+`（文本拼接、列表相加都算数），不是数值加法
                acc = crate::binop(0, acc, x)?;
                out.push(acc.clone());
            }
            Ok(list_new(out))
        },
        "组合" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("组合", args, 2, 2)?;
            let items = to_vec(&args[0])?;
            let n = as_intish(&args[1])?;
            if n < 0 {
                return Err(err("类型错误", format!(
                    "「组合」要取的个数不能是负数，得到了 {}", display(&args[1]))));
            }
            let k = n as usize;
            if k > items.len() {
                return Ok(list_new(Vec::new()));
            }
            let mut out: Vec<Val> = Vec::new();
            comb(&items, k, 0, &mut Vec::new(), &mut out);
            Ok(list_new(out))
        },
        "排列" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("排列", args, 1, 2)?;
            let items = to_vec(&args[0])?;
            let n = if args.len() > 1 { as_intish(&args[1])? } else { -1 };
            if n < -1 {
                return Err(err("类型错误", format!(
                    "「排列」要取的个数不能是负数，得到了 {}", display(&args[1]))));
            }
            let k = if n == -1 { items.len() } else { n as usize };
            if k > items.len() {
                return Ok(list_new(Vec::new()));
            }
            let mut out: Vec<Val> = Vec::new();
            let mut used = vec![false; items.len()];
            perm(&items, k, &mut used, &mut Vec::new(), &mut out);
            Ok(list_new(out))
        },
        "笛卡尔积" => |args: &[Val], _vm: &mut VM| -> R {
            if args.is_empty() {
                return Ok(list_new(Vec::new()));
            }
            let mut lists: Vec<Vec<Val>> = Vec::with_capacity(args.len());
            for (i, a) in args.iter().enumerate() {
                let got = to_vec(a).map_err(|_| err("类型错误", format!(
                    "「{}」不能当「笛卡尔积」第 {} 个序列用", display(a), i + 1)))?;
                if got.is_empty() {
                    return Ok(list_new(Vec::new()));
                }
                lists.push(got);
            }
            let mut rows: Vec<Vec<Val>> = vec![Vec::new()];
            for l in &lists {
                let mut next: Vec<Vec<Val>> = Vec::new();
                for prefix in &rows {
                    for x in l {
                        let mut row = prefix.clone();
                        row.push(x.clone());
                        next.push(row);
                    }
                }
                rows = next;
            }
            Ok(list_new(rows.into_iter().map(list_new).collect()))
        },
    }
}

/// `组合` 的递归体：从 `start` 起挑，攒够 `k` 个收一个（顺序同
/// `itertools.combinations` —— 下标递增）。
fn comb(items: &[Val], k: usize, start: usize, cur: &mut Vec<Val>, out: &mut Vec<Val>) {
    if cur.len() == k {
        out.push(list_new(cur.clone()));
        return;
    }
    for i in start..items.len() {
        cur.push(items[i].clone());
        comb(items, k, i + 1, cur, out);
        cur.pop();
    }
}

/// `排列` 的递归体：按**位置**去重（`[1, 1]` 的两个 1 是两个不同的位），
/// 与 `itertools.permutations` 同口径。
fn perm(items: &[Val], k: usize, used: &mut Vec<bool>, cur: &mut Vec<Val>, out: &mut Vec<Val>) {
    if cur.len() == k {
        out.push(list_new(cur.clone()));
        return;
    }
    for i in 0..items.len() {
        if used[i] {
            continue;
        }
        used[i] = true;
        cur.push(items[i].clone());
        perm(items, k, used, cur, out);
        cur.pop();
        used[i] = false;
    }
}
