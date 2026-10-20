//! 标准库 `HTTP路由`（M122）。与 Python 侧 `oracle/jishi/stdlib/HTTP路由.py` 同行为。
//!
//! `HTTP服务端` 给了四个零件，但没有路由 —— 路径一多就变成一串 `如果 路径 == …`。
//! 这个模块只管「哪条路走哪个函数」，**纯逻辑**，五个引擎都能跑。
//!
//! ## 匹配规则（三宿主必须一模一样）
//!
//! | 情形 | 行为 |
//! |---|---|
//! | 路径 | 按 `/` 切段；**末尾斜杠严格区分**（`/a` 与 `/a/` 是两条路） |
//! | 参数段 | `:` 开头的段匹配任意一段；**空段不算**（`/a/` 不匹配 `/a/:id`） |
//! | 方法 | 大写后比较；`"*"` = 任何方法 |
//! | 多条都匹配 | 参数段**最少**的优先；再相同取**先注册**的 |
//! | 没匹配上 | 返回 `空`，不抛错（404 是正常流程） |
//!
//! ⚠️ 路由表内部就是**列表 + 字典**（`{"方法","路径","处理"}`），故意不缓存
//! 「切好的段」和「参数个数」：`打印(路由表)` 时不该露出实现细节，而且少两个
//! 字段就少两处能跟 Python 侧分叉的地方。代价是每次匹配重切一次，可忽略。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{as_text, need_range, table, R};
use crate::{dict_get, dict_new, err, list_new, Val};

/// 把路径切成段：`/` → `[]`；`/a/b` → `["a","b"]`；`/a/` → `["a",""]`。
///
/// ⚠️ `trim_start_matches` 与 Python 的 `lstrip("/")` 同义（都去掉**所有**开头的 `/`）。
fn 切段(路径: &str) -> Vec<String> {
    if 路径 == "/" {
        return Vec::new();
    }
    路径
        .trim_start_matches('/')
        .split('/')
        .map(|s| s.to_string())
        .collect()
}

fn 可调用(v: &Val) -> bool {
    matches!(
        v,
        Val::Builtin(..) | Val::Func(_) | Val::BoundMethod(_) | Val::WalkFn(..)
    )
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "HTTP路由";
        // 造一张空表（就是空列表，`长度(表)` 是注册了几条）
        "路由表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("路由表", args, 0, 0)?;
            Ok(list_new(Vec::new()))
        },

        "加路由" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("加路由", args, 4, 4)?;
            let 表 = match &args[0] {
                Val::List(rc) => rc.clone(),
                other => {
                    return Err(err("类型错误", format!(
                        "「表」要是路由表（用 `路由表()` 造），现在是 {}",
                        crate::display(other))))
                }
            };
            let 方法 = as_text(&args[1]).trim().to_uppercase();
            if 方法.is_empty() {
                return Err(err("值错误",
                    "「方法」不能是空文本（要「什么方法都收」请写 \"*\"）".to_string()));
            }
            let 路径 = as_text(&args[2]);
            if !路径.starts_with('/') {
                return Err(err("值错误",
                    format!("路径要以 / 开头（现在是「{路径}」）")));
            }
            if !可调用(&args[3]) {
                return Err(err("类型错误", format!(
                    "「处理」要是函数，现在是 {}", crate::display(&args[3]))));
            }
            // 与 Python 侧同样两条校验（空段 / 光秃秃的冒号）
            for 段 in 切段(&路径) {
                if 段.is_empty() {
                    return Err(err("值错误",
                        format!("路径「{路径}」里有空段（是不是多写了一个 /）")));
                }
                if 段 == ":" {
                    return Err(err("值错误",
                        format!("路径「{路径}」里有光秃秃的 `:`，参数要有名字（比如 `:id`）")));
                }
            }
            表.borrow_mut().push(dict_new(vec![
                (Val::Str("方法".into()), Val::Str(方法)),
                (Val::Str("路径".into()), Val::Str(路径)),
                (Val::Str("处理".into()), args[3].clone()),
            ]));
            // 返回表本身（方便连着写）
            Ok(Val::List(表))
        },

        "匹配" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("匹配", args, 3, 3)?;
            let 表 = match &args[0] {
                Val::List(rc) => rc.clone(),
                other => {
                    return Err(err("类型错误", format!(
                        "「表」要是路由表（用 `路由表()` 造），现在是 {}",
                        crate::display(other))))
                }
            };
            let 方法 = as_text(&args[1]).trim().to_uppercase();
            let 段们 = 切段(&as_text(&args[2]));

            let mut 最佳: Option<(Val, Val, usize)> = None;   // (处理, 参数, 参数数)
            let 路由们: Vec<Val> = 表.borrow().clone();
            for 路由 in &路由们 {
                let d = match 路由 {
                    Val::Dict(rc) => rc.clone(),
                    _ => continue,
                };
                let 路由方法 = match dict_get(&d.borrow(), &Val::Str("方法".into())) {
                    Some(Val::Str(s)) => s,
                    _ => continue,
                };
                if 路由方法 != "*" && 路由方法 != 方法 {
                    continue;
                }
                let 路由路径 = match dict_get(&d.borrow(), &Val::Str("路径".into())) {
                    Some(Val::Str(s)) => s,
                    _ => continue,
                };
                let 模式们 = 切段(&路由路径);
                if 模式们.len() != 段们.len() {
                    continue;
                }

                let mut 对: Vec<(Val, Val)> = Vec::new();
                let mut 能匹配 = true;
                for (模式, 实际) in 模式们.iter().zip(段们.iter()) {
                    if let Some(名) = 模式.strip_prefix(':') {
                        // 空段不算参数值：`/a/` 不能匹配 `/a/:id`
                        if 实际.is_empty() {
                            能匹配 = false;
                            break;
                        }
                        对.push((Val::Str(名.to_string()), Val::Str(实际.clone())));
                    } else if 模式 != 实际 {
                        能匹配 = false;
                        break;
                    }
                }
                if !能匹配 {
                    continue;
                }
                let 参数数 = 模式们.iter().filter(|s| s.starts_with(':')).count();
                if 最佳.as_ref().map_or(true, |(_, _, n)| 参数数 < *n) {
                    let 处理 = dict_get(&d.borrow(), &Val::Str("处理".into()))
                        .unwrap_or(Val::None_);
                    最佳 = Some((处理, dict_new(对), 参数数));
                }
            }

            Ok(match 最佳 {
                None => Val::None_,
                Some((处理, 参数, _)) => dict_new(vec![
                    (Val::Str("处理".into()), 处理),
                    (Val::Str("参数".into()), 参数),
                ]),
            })
        },

        "路径们" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("路径们", args, 1, 1)?;
            let 表 = match &args[0] {
                Val::List(rc) => rc.clone(),
                other => {
                    return Err(err("类型错误", format!(
                        "「表」要是路由表（用 `路由表()` 造），现在是 {}",
                        crate::display(other))))
                }
            };
            let mut 出 = Vec::new();
            for 路由 in 表.borrow().iter() {
                if let Val::Dict(rc) = 路由 {
                    let d = rc.borrow();
                    let m = dict_get(&d, &Val::Str("方法".into()));
                    let p = dict_get(&d, &Val::Str("路径".into()));
                    if let (Some(Val::Str(m)), Some(Val::Str(p))) = (m, p) {
                        出.push(Val::Str(format!("{m} {p}")));
                    }
                }
            }
            Ok(list_new(出))
        },
    }
}
