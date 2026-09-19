//! 标准库 `日志`（Python 侧 `jishi/stdlib/日志.py`，9 个函数）。
//!
//! 带级别的输出与「按级别过滤」。调试信息走 `调试(...)`、正式输出走
//! `信息(...)`，上线时把级别调高（`设级别("警告")`）就全安静了。
//!
//! 级别与前缀是**进程级全局**（Python 侧也是模块级全局），所以这里用
//! `OnceLock<Mutex<…>>` 存。

use std::cell::RefCell;
use std::collections::HashMap;

use super::{need_range, table, R};
use crate::{display, err, list_new, Val, VM};

/// 级别名 → 序号（越大越严重）。`静默` 比 `错误` 还大，用来彻底闭嘴。
fn 级别序号(名: &str) -> Option<i32> {
    match 名 {
        "调试" => Some(10),
        "信息" => Some(20),
        "警告" => Some(30),
        "错误" => Some(40),
        "静默" => Some(99),
        _ => None,
    }
}

/// 级别顺序（从松到严）。
const 级别顺序: [&str; 5] = ["调试", "信息", "警告", "错误", "静默"];

// 用 `thread_local`：`Val` 含 `Rc` 不满足 `Send`，放不进 `Mutex`；
// Rust 宿主是单线程的，这样就够（与 args.rs 同一理由）。
thread_local! {
    static 状态: RefCell<(String, String)> =
        RefCell::new(("信息".to_string(), String::new()));
}

fn 取状态<R>(f: impl FnOnce(&mut (String, String)) -> R) -> R {
    状态.with(|s| f(&mut s.borrow_mut()))
}

fn 输出(vm: &mut VM, 级别: &str, 内容: &[Val]) {
    let (当前, 前缀) = 取状态(|s| (s.0.clone(), s.1.clone()));
    let 阈值 = 级别序号(&当前).unwrap_or(20);
    if 级别序号(级别).unwrap_or(0) < 阈值 {
        return;
    }
    let mut 段: Vec<String> = Vec::new();
    if !前缀.is_empty() {
        段.push(前缀);
    }
    段.push(format!("[{级别}]"));
    for x in 内容 {
        段.push(display(x));
    }
    let 行 = 段.join(" ");
    // **写进 vm.output**（与 `打印` 同一条缓冲），不要直接 println!/eprintln!：
    // 宿主的 stdout 有缓冲、stderr 没有，两者混着写会让「日志行」跑到
    // 「打印行」前面去——五路对拍立刻暴露（M41 踩到）。
    vm.output.push_str(&行);
    vm.output.push('\n');
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! {
        "级别们" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("级别们", args, 0, 0)?;
            Ok(list_new(级别顺序.iter().map(|s| Val::Str(s.to_string())).collect()))
        },
        "设级别" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("设级别", args, 1, 1)?;
            let 名 = match &args[0] { Val::Str(s) => s.clone(), o => display(o) };
            if 级别序号(&名).is_none() {
                return Err(err(
                    "值错误",
                    format!("不认识级别「{名}」，可用的有：{}", 级别顺序.join("、")),
                ));
            }
            取状态(|s| s.0 = 名);
            Ok(Val::None_)
        },
        "取级别" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("取级别", args, 0, 0)?;
            Ok(Val::Str(取状态(|s| s.0.clone())))
        },
        "设前缀" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("设前缀", args, 1, 1)?;
            let 名 = match &args[0] { Val::Str(s) => s.clone(), o => display(o) };
            取状态(|s| s.1 = 名);
            Ok(Val::None_)
        },
        "调试" => |args: &[Val], vm: &mut VM| -> R {
            输出(vm, "调试", args);
            Ok(Val::None_)
        },
        "信息" => |args: &[Val], vm: &mut VM| -> R {
            输出(vm, "信息", args);
            Ok(Val::None_)
        },
        "警告" => |args: &[Val], vm: &mut VM| -> R {
            输出(vm, "警告", args);
            Ok(Val::None_)
        },
        "错误" => |args: &[Val], vm: &mut VM| -> R {
            输出(vm, "错误", args);
            Ok(Val::None_)
        },
    }
}
