//! 基石 Rust 引擎 CLI（M13.3）。

use std::env;
use std::fs;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("用法：jishi-rs 字节码.json");
        eprintln!("      jishi-rs --dump-stdlib");
        eprintln!("      jishi-rs --dump-methods");
        eprintln!("      jishi-rs --dump-builtins");
        return ExitCode::from(2);
    }
    // M51：把宿主**实际带的标准库模块与函数**打成 JSON。
    // 给 `tests/test_m51_consistency.py` 当事实源 —— 「Python 侧有、宿主没有」
    // 这种漂移以前是静默的（M50 第一批的宿主缺失是手工测出来的）。
    if args[1] == "--dump-stdlib" || args[1] == "--dump_stdlib" {
        println!("{}", jishi_ffi::dump_stdlib());
        return ExitCode::SUCCESS;
    }
    // M54：同理，报**对象方法表**（列表/字典/文本/集合/文件/小数）。
    // 以前方法表在五个引擎里各写一份却没人比对，Rust 缺 `文本.查找`、
    // Node 缺 `文件.位置/定位/刷新` 都是偶然撞见的。
    if args[1] == "--dump-methods" || args[1] == "--dump_methods" {
        println!("{}", jishi_ffi::dump_methods());
        return ExitCode::SUCCESS;
    }
    // M55：同理，报**内建函数**清单。加个内建要动三个引擎，而以前没有任何
    // 检测盯着这件事（M51/M54 只覆盖标准库函数与对象方法）。
    if args[1] == "--dump-builtins" || args[1] == "--dump_builtins" {
        println!("{}", jishi_ffi::dump_builtins());
        return ExitCode::SUCCESS;
    }
    let path = &args[1];
    let text = match fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) => { eprintln!("读取文件失败：{e}"); return ExitCode::from(1); }
    };

    // 脚本参数（`jishi-rs 字节码.json 参数1 参数2`）→ `系统.参数()`。
    // 与 Node 宿主的 `process.argv.slice(3)`、Python 侧 `系统._设参数` 同语义：
    // 不含字节码文件名本身。
    jishi_ffi::set_args(args[2..].to_vec());

    let mut vm = match jishi_ffi::load_module(&text) {
        Ok(vm) => vm,
        Err(e) => { eprintln!("字节码加载失败：{e}"); return ExitCode::from(1); }
    };

    match vm.run() {
        Ok(()) => {
            print!("{}", jishi_ffi::get_output(&vm));
            ExitCode::SUCCESS
        }
        Err(e) => {
            // M56：有错误码就与 Python 侧同形（`错误 E2002：类型错误（消息）`），
            // 没有（类型是宽泛的「异常」）退回「错误（异常）：消息」。
            // 标题用 `error_title`（与 Python 的 title 同一张表：「断言没通过」…）。
            match &e.code {
                Some(code) => {
                    let title = jishi_ffi::error_title(&e.type_name);
                    if e.message.is_empty() || e.message == title {
                        eprintln!("错误 {code}：{title}");
                    } else {
                        eprintln!("错误 {code}：{title}（{}）", e.message);
                    }
                }
                None => eprintln!("错误（{}）：{}", e.type_name, e.message),
            }
            // 位置另起一行，格式与 Node 宿主一致
            if let (Some(line), Some(col)) = (e.line, e.col) {
                eprintln!("  ← 第 {line} 行第 {col} 列");
            }
            // M56：提示语（与 Python 侧「提示：…」同格式）
            if let Some(hint) = &e.hint {
                eprintln!("  提示：{hint}");
            }
            ExitCode::from(1)
        }
    }
}
