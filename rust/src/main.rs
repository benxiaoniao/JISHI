//! 基石 Rust 引擎 CLI（M13.3）。

use std::env;
use std::fs;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("用法：jishi-rs 字节码.json");
        eprintln!("      jishi-rs --dump-stdlib");
        return ExitCode::from(2);
    }
    // M51：把宿主**实际带的标准库模块与函数**打成 JSON。
    // 给 `tests/test_m51_consistency.py` 当事实源 —— 「Python 侧有、宿主没有」
    // 这种漂移以前是静默的（M50 第一批的宿主缺失是手工测出来的）。
    if args[1] == "--dump-stdlib" || args[1] == "--dump_stdlib" {
        println!("{}", jishi_ffi::dump_stdlib());
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
            eprintln!("错误（{}）：{}", e.type_name, e.message);
            ExitCode::from(1)
        }
    }
}
