//! 标准库 `参数`（Python 侧 `jishi/stdlib/参数.py`）。
//!
//! 命令行解析：子命令 + 选项 + 位置参数。声明式——把「有哪些选项」写成一串
//! `选项(解析器, …)` / `标志(解析器, …)`，剩下的（`--名字 值`、`--名字=值`、
//! `--标志`、缺参报错、用法提示）都交给模块。
//!
//! **为什么是句柄式**：`Val::Builtin` 只能存**无捕获的函数指针**，给每个
//! 解析器挂一套自己的方法在 Rust 侧做不到。为了五路（三执行器 + Node + Rust）
//! 写法完全一致，统一「`新建` 拿句柄 → 句柄当第一个参数传回去」。
//! 状态放全局表，句柄是下标 + 1（0 表示无效）。

use std::cell::RefCell;
use std::collections::HashMap;

use super::{need_range, table, to_vec, R};
use crate::{dict_new, display, err, list_new, Val, VM};

/// 一个选项的声明。
struct Spec {
    name: String,
    default: Val,
    desc: String,
    flag: bool,
}

/// 一个解析器的状态。
struct Parser {
    name: String,
    desc: String,
    specs: Vec<Spec>,
}

// 解析器表（下标 + 1 = 句柄）。用 `thread_local` 而不是 `Mutex<Vec<…>>`：
// `Val` 里含 `Rc`，不满足 `Send`，放不进跨线程的静态量。Rust 宿主是
// 单线程的，`thread_local` 足够，而且省掉一次加锁。
thread_local! {
    static PARSERS: RefCell<Vec<Parser>> = RefCell::new(Vec::new());
}

fn 表<R>(f: impl FnOnce(&mut Vec<Parser>) -> R) -> R {
    PARSERS.with(|p| f(&mut p.borrow_mut()))
}

fn 取下标(v: &Val) -> Result<usize, crate::JishiError> {
    match v {
        Val::Int(i) if *i >= 1 => Ok((*i - 1) as usize),
        _ => Err(err("类型错误", "这个值不是参数解析器（要用 参数.新建(...) 造一个）")),
    }
}

fn 用法文本(p: &Parser) -> String {
    let mut 行 = vec![format!("用法：{} [命令] [选项]", p.name)];
    if !p.desc.is_empty() {
        行.push(format!("  {}", p.desc));
    }
    if !p.specs.is_empty() {
        行.push("选项：".to_string());
        for s in &p.specs {
            if s.flag {
                行.push(format!("  --{:<10} {}", s.name, s.desc));
            } else {
                行.push(format!(
                    "  --{} <值>     {}（默认 {}）",
                    s.name,
                    s.desc,
                    display(&s.default)
                ));
            }
        }
    }
    行.join("\n")
}

fn 未知选项(p: &Parser, 名: &str) -> String {
    let 已知: Vec<String> = p.specs.iter().map(|s| format!("--{}", s.name)).collect();
    let 全部 = if 已知.is_empty() { "（没有）".to_string() } else { 已知.join("、") };
    format!("不认识选项「--{名}」。已声明的有：{全部}\n（敲错了？跑 `--帮助` 看用法）")
}

fn 解析列表(p: &Parser, list: Vec<Val>) -> Result<Val, crate::JishiError> {
    let mut 结果: Vec<(Val, Val)> = p
        .specs
        .iter()
        .map(|s| (Val::Str(s.name.clone()), s.default.clone()))
        .collect();
    let 找 = |名: &str| p.specs.iter().position(|s| s.name == 名);

    let mut 位置: Vec<Val> = Vec::new();
    let mut 命令 = String::new();
    let mut i = 0usize;
    while i < list.len() {
        let 文本 = match &list[i] {
            Val::Str(s) => s.clone(),
            other => display(other),
        };
        if 文本 == "--" {
            位置.extend(list[i + 1..].iter().cloned());
            break;
        }
        if let Some(体) = 文本.strip_prefix("--") {
            if let Some((名, 值)) = 体.split_once('=') {
                match 找(名) {
                    Some(k) => {
                        if p.specs[k].flag {
                            return Err(err("运行期错误", format!("「--{名}」是个开关，不该给值")));
                        }
                        结果[k].1 = Val::Str(值.to_string());
                    }
                    None => return Err(err("运行期错误", 未知选项(p, 名))),
                }
                i += 1;
                continue;
            }
            match 找(体) {
                Some(k) => {
                    if p.specs[k].flag {
                        结果[k].1 = Val::Bool(true);
                        i += 1;
                        continue;
                    }
                    if i + 1 >= list.len() {
                        return Err(err("运行期错误", format!("「--{体}」后面要跟一个值")));
                    }
                    结果[k].1 = list[i + 1].clone();
                    i += 2;
                    continue;
                }
                None => return Err(err("运行期错误", 未知选项(p, 体))),
            }
        }
        if 命令.is_empty() && 位置.is_empty() {
            命令 = 文本;
        } else {
            位置.push(list[i].clone());
        }
        i += 1;
    }
    结果.push((Val::Str("命令".to_string()), Val::Str(命令)));
    结果.push((Val::Str("位置".to_string()), list_new(位置)));
    Ok(dict_new(结果))
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! {
        "新建" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("新建", args, 1, 2)?;
            let 名 = match &args[0] { Val::Str(s) => s.clone(), o => display(o) };
            let 说明 = if args.len() == 2 {
                match &args[1] { Val::Str(s) => s.clone(), o => display(o) }
            } else {
                String::new()
            };
            let n = 表(|t| {
                t.push(Parser { name: 名, desc: 说明, specs: Vec::new() });
                t.len()
            });
            Ok(Val::Int(n as i128))
        },
        "选项" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("选项", args, 2, 4)?;
            let idx = 取下标(&args[0])?;
            let 名 = match &args[1] { Val::Str(s) => s.clone(), o => display(o) };
            let 默认 = args[2].clone();
            let 说明 = if args.len() == 4 {
                match &args[3] { Val::Str(s) => s.clone(), o => display(o) }
            } else {
                String::new()
            };
            表(|t| -> Result<(), crate::JishiError> {
                let p = t.get_mut(idx).ok_or_else(|| err("运行期错误", "参数解析器已失效"))?;
                p.specs.push(Spec { name: 名, default: 默认, desc: 说明, flag: false });
                Ok(())
            })?;
            Ok(args[0].clone())
        },
        "标志" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("标志", args, 2, 3)?;
            let idx = 取下标(&args[0])?;
            let 名 = match &args[1] { Val::Str(s) => s.clone(), o => display(o) };
            let 说明 = if args.len() == 3 {
                match &args[2] { Val::Str(s) => s.clone(), o => display(o) }
            } else {
                String::new()
            };
            表(|t| -> Result<(), crate::JishiError> {
                let p = t.get_mut(idx).ok_or_else(|| err("运行期错误", "参数解析器已失效"))?;
                p.specs.push(Spec { name: 名, default: Val::Bool(false), desc: 说明, flag: true });
                Ok(())
            })?;
            Ok(args[0].clone())
        },
        "用法" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("用法", args, 1, 1)?;
            let idx = 取下标(&args[0])?;
            表(|t| {
                let p = t.get(idx).ok_or_else(|| err("运行期错误", "参数解析器已失效"))?;
                Ok(Val::Str(用法文本(p)))
            })
        },
        "解析" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("解析", args, 2, 2)?;
            // 一次式：第一个参数不是句柄（是列表）时，第二个是「选项 → 默认值」字典
            if !matches!(&args[0], Val::Int(i) if *i >= 1) {
                let decl = match &args[1] {
                    Val::Dict(d) => d.borrow().clone(),
                    other => {
                        return Err(err(
                            "类型错误",
                            format!("「参数.解析」的第二个参数要是字典，得到了「{}」",
                                    crate::type_label(other)),
                        ));
                    }
                };
                let mut specs: Vec<Spec> = Vec::new();
                for (k, v) in decl {
                    let 名 = match &k { Val::Str(s) => s.clone(), o => display(o) };
                    let 是标志 = matches!(v, Val::Bool(_));
                    specs.push(Spec { name: 名, default: v, desc: String::new(), flag: 是标志 });
                }
                let list = to_vec(&args[0])?;
                let p = Parser { name: "脚本".to_string(), desc: String::new(), specs };
                return 解析列表(&p, list);
            }
            let idx = 取下标(&args[0])?;
            let list = to_vec(&args[1])?;
            表(|t| {
                let p = t.get(idx).ok_or_else(|| err("运行期错误", "参数解析器已失效"))?;
                解析列表(p, list)
            })
        },
    }
}
