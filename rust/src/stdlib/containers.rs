//! 标准库 `容器`（Python 侧 `jishi/stdlib/容器.py`，7 个函数）。
//!
//! 字典/列表的批量统计与整理：计数、最多、按值排序、分组、取前/后、分块。
//!
//! **模块名为什么是「容器」不是「集合」**：`集合` 是内建函数名
//! （`集合([1,2,3])` 去重造集合），同名模块会把内建遮蔽掉。
//!
//! Rust 侧的字典是 `Vec<(Val, Val)>`（保插入序，见 `JDict`），所以
//! 「保序」这件事天然成立，不需要额外记位置。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{as_intish, need_range, table, to_vec, R};
use crate::{dict_new, display, err, list_new, Val};

/// 计数：返回 [[元素, 次数], …]，按首次出现的先后排。
fn count_pairs(items: &[Val]) -> Vec<(Val, i128)> {
    let mut 位置: HashMap<String, usize> = HashMap::new();
    let mut out: Vec<(Val, i128)> = Vec::new();
    for x in items {
        let key = display(x);
        match 位置.get(&key) {
            Some(&i) => out[i].1 += 1,
            None => {
                位置.insert(key, out.len());
                out.push((x.clone(), 1));
            }
        }
    }
    out
}

/// 递归复制列表 / 字典（M117）。数字/文本/字节不可变，原样返回即可。
fn 深拷贝值(v: &Val) -> Val {
    match v {
        Val::List(l) => {
            let 内: Vec<Val> = l.borrow().iter().map(深拷贝值).collect();
            list_new(内)
        }
        Val::Dict(d) => {
            let 对们: Vec<(Val, Val)> = d
                .borrow()
                .items
                .iter()
                .map(|(k, x)| (k.clone(), 深拷贝值(x)))
                .collect();
            dict_new(对们)
        }
        _ => v.clone(),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "容器";
        // M117：`索引表`（值 → 首次下标）/ `深拷贝`。与基石库层 `容器.jsh` 同行为。
        // 键化沿用 `count_pairs` 那套（`display` 后进 HashMap），与 `计数` 口径一致。
        "索引表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("索引表", args, 1, 1)?;
            let items = to_vec(&args[0])?;
            let mut 见过: HashMap<String, ()> = HashMap::new();
            let mut 出: Vec<(Val, Val)> = Vec::new();
            for (i, v) in items.iter().enumerate() {
                if 见过.insert(display(v), ()).is_none() {
                    出.push((v.clone(), Val::Int(i as i128)));
                }
            }
            Ok(dict_new(出))
        },
        "深拷贝" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("深拷贝", args, 1, 1)?;
            Ok(深拷贝值(&args[0]))
        },
        "计数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("计数", args, 1, 1)?;
            let items = to_vec(&args[0])?;
            Ok(dict_new(
                count_pairs(&items)
                    .into_iter()
                    .map(|(k, n)| (k, Val::Int(n)))
                    .collect(),
            ))
        },
        "最多" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("最多", args, 1, 2)?;
            let items = to_vec(&args[0])?;
            let n = if args.len() == 2 { as_intish(&args[1])? } else { 1 };
            if n <= 0 {
                return Err(err(
                    "值错误",
                    format!("「最多」的个数要大于 0，得到了 {}", display(&args[1])),
                ));
            }
            let mut pairs = count_pairs(&items);
            // 次数降序；平局时 `sort_by` 是稳定排序，天然保持首次出现的先后
            pairs.sort_by(|a, b| b.1.cmp(&a.1));
            if n == 1 {
                return Ok(pairs.first().map(|(k, _)| k.clone()).unwrap_or(Val::None_));
            }
            Ok(list_new(
                pairs
                    .into_iter()
                    .take(n as usize)
                    .map(|(k, c)| list_new(vec![k, Val::Int(c)]))
                    .collect(),
            ))
        },
        "按值排序" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("按值排序", args, 1, 2)?;
            let desc = if args.len() == 2 { crate::truthy(args[1].clone()) } else { true };
            let pairs = match &args[0] {
                Val::Dict(d) => d.borrow().clone(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!(
                            "「按值排序」要一个字典，得到了「{}」",
                            crate::type_label(other)
                        ),
                    ));
                }
            };
            let mut 项 = pairs.into_items();
            // 稳定排序：平局保持原（首次出现）顺序
            项.sort_by(|a, b| {
                let c = crate::cmp_vals(&a.1, &b.1);
                if desc { c.reverse() } else { c }
            });
            Ok(list_new(
                项.into_iter()
                    .map(|(k, v)| list_new(vec![k, v]))
                    .collect(),
            ))
        },
        "分组" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("分组", args, 2, 2)?;
            let items = to_vec(&args[0])?;
            let f = args[1].clone();
            let mut keys: Vec<Val> = Vec::new();
            let mut groups: Vec<Vec<Val>> = Vec::new();
            for x in items {
                let k = vm.call_value(f.clone(), vec![x.clone()])?;
                let kk = display(&k);
                match keys.iter().position(|e| display(e) == kk) {
                    Some(i) => groups[i].push(x),
                    None => {
                        keys.push(k);
                        groups.push(vec![x]);
                    }
                }
            }
            Ok(dict_new(
                keys.into_iter()
                    .zip(groups)
                    .map(|(k, g)| (k, list_new(g)))
                    .collect(),
            ))
        },
        "取前" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("取前", args, 2, 2)?;
            let items = to_vec(&args[0])?;
            let n = as_intish(&args[1])?.max(0) as usize;
            Ok(list_new(items.into_iter().take(n).collect()))
        },
        "取后" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("取后", args, 2, 2)?;
            let items = to_vec(&args[0])?;
            let n = as_intish(&args[1])?.max(0) as usize;
            let skip = items.len().saturating_sub(n);
            Ok(list_new(items.into_iter().skip(skip).collect()))
        },
        "分块" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("分块", args, 2, 2)?;
            let items = to_vec(&args[0])?;
            let n = as_intish(&args[1])?;
            if n <= 0 {
                return Err(err(
                    "值错误",
                    format!("「分块」的每块个数要大于 0，得到了 {}", display(&args[1])),
                ));
            }
            Ok(list_new(
                items
                    .chunks(n as usize)
                    .map(|c| list_new(c.to_vec()))
                    .collect(),
            ))
        },
    }
}
