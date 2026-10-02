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
            // ⚠️ **先把程序已经打印的东西吐出来**（R6.2）：以前只在 `Ok` 分支打
            // `get_output`，于是**报错时前面所有 `打印` 全丢** —— 崩了看不到跑到哪。
            // 三个 Python 执行器是边跑边出，只有宿主不吐；而宿主报错渲染本来就更简，
            // 再把 stdout 丢掉，用户就一点线索都没有了。
            // 顺序：stdout → 报错块（与 Python 侧一致：报错在末尾）。
            print!("{}", jishi_ffi::get_output(&vm));
            // R6.3：渲染成与 Python 的 `JishiError.render()` **同形**的多行文本
            // （标题 + 位置 + 源码行 + `^` + 调用链 + 提示）。以前是「一行标题
            // + 一行位置」，用户看不到自己写的那一行 —— 单二进制是唯一形态之后
            // （R7/R8）那就等于没报错。见 `docs/embed.md`。
            eprint!("{}", jishi_ffi::render_error(&vm, &e));
            ExitCode::from(1)
        }
    }
}
