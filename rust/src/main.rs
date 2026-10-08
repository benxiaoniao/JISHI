//! 基石 Rust 引擎 CLI（M13.3）。

use std::env;
use std::fs;
use std::io::Read;
use std::path::Path;
use std::process::ExitCode;

/// `jishi 格式化`（M21.1）—— 输出/退出码**逐字照** Python 侧 `cli._cmd_format`
/// （含 `--check` / `--write` / `--stdin` / `--indent` 与目录两条路）。
fn run_format(rest: &[String]) -> ExitCode {
    use jishi_ffi::formatter;

    let mut file: Option<String> = None;
    let mut write = false;
    let mut check = false;
    let mut stdin = false;
    let mut indent: usize = 4;
    let mut i = 0;
    while i < rest.len() {
        let a = rest[i].as_str();
        match a {
            "-w" | "--write" => write = true,
            "--check" => check = true,
            "--stdin" => stdin = true,
            "--indent" => {
                i += 1;
                match rest.get(i).and_then(|s| s.parse::<usize>().ok()) {
                    Some(n) => indent = n,
                    None => { eprintln!("--indent 需要一个整数"); return ExitCode::from(2); }
                }
            }
            _ => {
                if let Some(v) = a.strip_prefix("--indent=") {
                    match v.parse::<usize>() {
                        Ok(n) => indent = n,
                        Err(_) => { eprintln!("--indent 需要一个整数"); return ExitCode::from(2); }
                    }
                } else if file.is_none() {
                    file = Some(a.to_string());
                } else {
                    eprintln!("多余的参数：{a}");
                    return ExitCode::from(2);
                }
            }
        }
        i += 1;
    }

    let src: String;
    let name: String;
    if stdin {
        let mut data: Vec<u8> = Vec::new();
        let _ = std::io::stdin().read_to_end(&mut data);
        // 与 Python `_read_stdin` 同口径：字节 → UTF-8（非法字节替换成 U+FFFD，
        // 让词法器给出「无法识别的字符」的中文报错，而不是在解码处崩掉）。
        src = String::from_utf8_lossy(&data).to_string();
        name = "<stdin>".to_string();
    } else {
        let f = match &file {
            Some(f) => f.clone(),
            None => {
                eprintln!("请给出要格式化的文件，或用 --stdin 从标准输入读取");
                return ExitCode::from(2);
            }
        };
        let p = Path::new(&f);
        if p.is_dir() {
            if !(check || write) {
                eprintln!("「{f}」是目录，请说明要做什么：\n\
                           \x20 jishi 格式化 {f} --check   # 只检查（未格式化则退出码 1）\n\
                           \x20 jishi 格式化 {f} --write   # 就地改写目录里所有 .jsh");
                return ExitCode::from(2);
            }
            return format_tree(p, &f, check, write, indent);
        }
        match read_source_text(&f) {
            Ok(t) => src = t,
            Err(code) => return code,
        }
        name = f;
    }

    if write && stdin {
        eprintln!("--write 不能和 --stdin 一起用");
        return ExitCode::from(2);
    }

    let out = match formatter::format_source(&src, &name, indent) {
        Ok(o) => o,
        Err(e) => {
            eprint!("{}", formatter::render_lex_error(&e, &name, &src));
            return ExitCode::from(1);
        }
    };

    if check {
        if src == out {
            return ExitCode::SUCCESS;
        }
        eprintln!("{name} 没有按统一风格书写（跑 jishi 格式化 {} --write 可自动修）",
                  file.clone().unwrap_or_else(|| "-".to_string()));
        return ExitCode::from(1);
    }

    if write {
        let f = file.clone().unwrap();
        if let Err(e) = fs::write(&f, out.as_bytes()) {
            eprintln!("写不了文件「{f}」：{e}");
            return ExitCode::from(2);
        }
        println!("已格式化 {f}");
        return ExitCode::SUCCESS;
    }

    print!("{out}");
    ExitCode::SUCCESS
}

/// 读一个源码文件（Python 文本模式：**做换行归一**）。
///
/// ⚠️ 换行归一是**必须的**：Python 的 `open(..., "r")` 会把 `\r\n` 翻成 `\n`，
/// 于是 `--check` 里 `src == out` 的那次比较两边都是 `\n`；Rust 若照原样读，
/// 每个 CRLF 文件都会被误报成「没格式化」。
fn read_source_text(path: &str) -> Result<String, ExitCode> {
    match fs::read_to_string(path) {
        Ok(t) => Ok(t.replace("\r\n", "\n").replace('\r', "\n")),
        Err(e) => {
            use std::io::ErrorKind::*;
            match e.kind() {
                NotFound => {
                    eprintln!("找不到文件「{path}」");
                    Err(ExitCode::from(1))
                }
                PermissionDenied => {
                    eprintln!("没有权限读取「{path}」");
                    Err(ExitCode::from(2))
                }
                InvalidData => {
                    eprintln!("文件「{path}」不是 UTF-8 编码，无法读取");
                    Err(ExitCode::from(1))
                }
                _ => {
                    eprintln!("读不了文件「{path}」：{e}");
                    Err(ExitCode::from(2))
                }
            }
        }
    }
}

/// `jishi 格式化 <目录>`：递归处理目录下的 .jsh（M38 B4）。
fn format_tree(root: &Path, root_arg: &str, check: bool, write: bool,
               indent: usize) -> ExitCode {
    use jishi_ffi::formatter;

    let files = formatter::collect_jsh_sorted(root);
    if files.is_empty() {
        eprintln!("「{root_arg}」里没有 .jsh 文件");
        return ExitCode::from(2);
    }

    let mut unformatted: Vec<std::path::PathBuf> = Vec::new();
    let mut broken: Vec<String> = Vec::new();
    for p in &files {
        let shown = jishi_ffi::checker::display_path(p);
        let raw = match fs::read_to_string(p) {
            Ok(t) => t,
            Err(e) => {
                broken.push(format!("{shown}（读不了：{e}）"));
                continue;
            }
        };
        let src = raw.replace("\r\n", "\n").replace('\r', "\n");
        let out = match formatter::format_source(&src, &shown, indent) {
            Ok(o) => o,
            Err(e) => {
                let title = jishi_ffi::errcodes::title(e.code).unwrap_or("词法错误");
                broken.push(format!(
                    "{shown}（{} {title}——先修好这处词法错误，再格式化）", e.code));
                continue;
            }
        };
        if src != out {
            unformatted.push(p.clone());
            if write {
                if let Err(e) = fs::write(p, out.as_bytes()) {
                    eprintln!("写不了文件「{shown}」：{e}");
                    continue;
                }
                println!("已格式化 {shown}");
            }
        }
    }

    if check {
        if !unformatted.is_empty() {
            eprintln!("{}/{} 个文件没有按统一风格书写（跑 jishi 格式化 {root_arg} --write 可自动修）：",
                      unformatted.len(), files.len());
            for p in &unformatted {
                eprintln!("  {}", jishi_ffi::checker::display_path(p));
            }
        } else {
            println!("全部 {} 个文件都符合统一风格", files.len());
        }
    } else {
        let n = unformatted.len();
        println!("共 {} 个文件，改写了 {n} 个{}",
                 files.len(),
                 if files.len() > n { "（其余本来就符合）" } else { "" });
    }

    for b in &broken {
        eprintln!("跳过：{b}");
    }
    if check {
        if !(unformatted.is_empty() && broken.is_empty()) {
            return ExitCode::from(1);
        }
        return ExitCode::SUCCESS;
    }
    // --write：改写了是**成功**，只有「没处理掉的文件」才算失败
    if broken.is_empty() { ExitCode::SUCCESS } else { ExitCode::from(1) }
}

/// `jishi 检查`（源码静态检查，R7.1-c）—— 输出**逐字照** Python 侧
/// `cli._cmd_static_check`（含 `--json` 的 `indent=2` 排版与退出码约定）。
fn run_check(rest: &[String]) -> ExitCode {
    use jishi_ffi::checker;

    let mut json = false;
    let mut paths: Vec<String> = Vec::new();
    for a in rest {
        if a == "--json" {
            json = true;
        } else {
            paths.push(a.clone());
        }
    }
    if paths.is_empty() {
        paths.push(".".to_string());     // 与 Python 侧默认一致
    }
    let sym = checker::Symbols::from_host();
    let rows = checker::check_paths(&paths, &sym);
    let code = if rows.is_empty() { ExitCode::SUCCESS } else { ExitCode::from(1) };

    if json {
        println!("{}", json_check(&rows));
        return code;
    }
    if rows.is_empty() {
        println!("检查了 {} 个路径，没发现问题。", paths.len());
        return code;
    }
    for (p, it) in &rows {
        println!("{}", it.format(&jishi_ffi::checker::display_path(p)));
    }
    let errors = rows.iter()
        .filter(|(_, i)| i.level == checker::LEVEL_ERROR).count();
    let mut files: std::collections::HashSet<String> = std::collections::HashSet::new();
    for (p, _) in &rows {
        files.insert(jishi_ffi::checker::display_path(p));
    }
    println!("\n共 {} 个问题（错误 {errors} 个 / 警告 {} 个），涉及 {} 个文件。",
             rows.len(), rows.len() - errors, files.len());
    println!("（只是提醒，不改你的代码；确认无误可以照常运行）");
    code
}

/// `--json` 的载荷 —— **排版逐字照 Python 的 `json.dumps(..., ensure_ascii=False,
/// indent=2)`**（`{` 后换行、两空格缩进、`[]` / `{}` 空容器写成一行）。
fn json_check(rows: &[(std::path::PathBuf, jishi_ffi::checker::Issue)]) -> String {
    use jishi_ffi::checker::jstr;

    let mut s = String::from("{\n  \"ok\": ");
    s.push_str(if rows.is_empty() { "true" } else { "false" });
    s.push_str(",\n  \"issues\": ");
    if rows.is_empty() {
        s.push_str("[]");
    } else {
        s.push_str("[\n");
        for (i, (p, it)) in rows.iter().enumerate() {
            if i > 0 {
                s.push_str(",\n");
            }
            s.push_str("    {\n");
            s.push_str(&format!("      \"file\": {},\n",
                                jstr(&jishi_ffi::checker::display_path(p))));
            s.push_str(&format!("      \"code\": {},\n", jstr(&it.code)));
            s.push_str(&format!("      \"level\": {},\n", jstr(it.level)));
            s.push_str(&format!("      \"line\": {},\n", it.line));
            s.push_str(&format!("      \"col\": {},\n", it.col));
            s.push_str(&format!("      \"message\": {},\n", jstr(&it.message)));
            s.push_str(&format!("      \"hint\": {},\n", jstr(&it.hint)));
            s.push_str(&format!("      \"name\": {}\n", jstr(&it.name)));
            s.push_str("    }");
        }
        s.push_str("\n  ]");
    }
    s.push_str("\n}");
    s
}

/// 临时诊断：Rust 的 spawn 在本机到底哪种 stdio 配置会失败（R7.4 排查用）。
fn spawn_probe() -> ExitCode {
    use std::process::{Command, Stdio};
    let me = std::env::current_exe().unwrap();
    let targets: Vec<(&str, std::ffi::OsString, Vec<std::ffi::OsString>)> = vec![
        ("cmd", "cmd".into(), vec!["/c".into(), "echo".into(), "hi".into()]),
        ("self", me.clone().into_os_string(), vec!["--lang-spec".into()]),
    ];
    let names = ["全 piped", "stderr=null", "stdin=null ", "stdout=null", "全 null    "];
    for (tname, prog, args) in &targets {
        for mode in 0..5usize {
            let mut c = Command::new(prog);
            c.args(args);
            match mode {
                0 => { c.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()); }
                1 => { c.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()); }
                2 => { c.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()); }
                3 => { c.stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::piped()); }
                _ => { c.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()); }
            }
            match c.spawn() {
                Ok(mut ch) => {
                    let _ = ch.wait();
                    println!("{tname} / {} -> OK", names[mode]);
                }
                Err(e) => println!("{tname} / {} -> {e}", names[mode]),
            }
        }
    }
    ExitCode::SUCCESS
}

/// 用法（`-h/--help`）。⚠️ 裸命令行**不再走这里** —— 它进 REPL（R8.4b）。
fn usage() {
    eprintln!("用法：jishi-rs <文件.jsh> [选项]  # 跑源码 / --ast / --json-errors / --sandbox");
    eprintln!("      jishi-rs                  # 没给文件就进交互式 REPL（等同 -i）");
    eprintln!("      jishi-rs --stdin [选项] < 文件.jsh");
    eprintln!("      jishi-rs --load-bytecode <字节码.json>");
    eprintln!("      jishi-rs --dump-stdlib / --dump-methods / --dump-builtins");
    eprintln!("      jishi-rs 错误 [码]        # 查错误码（R7.1-b）");
    eprintln!("      jishi-rs 源码静态检查 [路径...] [--json]   # 源码检查（R7.1-c）");
    eprintln!("      jishi-rs 格式化 [文件] [--stdin] [--check] [--write] [--indent N]");
    eprintln!("      jishi-rs 安装/卸载/列表/发布/索引/检查   # 包管理（R7.6）");
    eprintln!("      jishi-rs 医生 [--no-net]   # 环境自检（R8.4）");
    eprintln!("      jishi-rs 新项目 <名字>     # 生成脚手架（R8.4）");
    eprintln!("      jishi-rs 教程              # 教程索引（R8.4）");
    eprintln!("      jishi-rs lsp               # 语言服务器（stdio，R7.3）");
    eprintln!("      jishi-rs dap               # 调试适配器（stdio，R7.5）");
    eprintln!("      jishi-rs 调试 <文件.jsh>   # 交互式调试（R7.5 收尾）");
    eprintln!("      jishi-rs mcp               # MCP 服务器（stdio，R7.4）");
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    // R8.4b：**裸命令行进 REPL**（与 Python 的 `jishi` 同行为）；只有显式 `-h/--help`
    // 才打用法。以前这里一律打用法退 2 —— 那是宿主还没有 REPL 时的权宜。
    if args.len() < 2 {
        return jishi_ffi::run::main_cli(&[]);
    }
    if args[1] == "-h" || args[1] == "--help" || args[1] == "帮助" {
        usage();
        return ExitCode::SUCCESS;
    }
    if args[1] == "--usage" {
        usage();
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
    // 2026-10-05：报 `Val` 的实际字节数（结构钉）。加变体时若把大载荷内联进来，
    // 它就会悄悄膨胀 —— 「没报错、只是变慢」那类退化，对拍抓不住。
    if args[1] == "--dump-val-size" || args[1] == "--dump_val_size" {
        println!("{}", jishi_ffi::dump_val_size());
        return ExitCode::SUCCESS;
    }
    // R6 主体：**树遍历参考执行器** —— 吃**AST JSON**（前端契约的 ast 阶段产物），
    // 不经过字节码。与字节码 VM 互为参考实现（差异就是编译器的问题）。
    // 进度见 `walk.rs`；未实现的节点会明确报错。
    if args[1] == "--walk" {
        let path = match args.get(2) {
            Some(p) => p,
            None => { eprintln!("用法：jishi-rs --walk AST.json [源文件.jsh]"); return ExitCode::from(2); }
        };
        let text = match fs::read_to_string(path) {
            Ok(t) => t,
            Err(e) => { eprintln!("读取文件失败：{e}"); return ExitCode::from(1); }
        };
        // 可选的源文件路径（R6 主体 ⑥）：报错渲染要打「源码行 + `^`」就得从外面给
        // 这个（AST JSON 里没有源码）。缺省时退回「没有源码行」，位置行照样打。
        let source = args.get(3).map(|s| s.as_str());
        return match jishi_ffi::run_walk(&text, source) {
            Ok(out) => { print!("{out}"); ExitCode::SUCCESS }
            // ⚠️ 先把**出错前已经打印的内容**吐出来，再报错（与字节码 VM 一致）。
            Err((out, e)) => { print!("{out}"); eprint!("{e}"); ExitCode::from(1) }
        };
    }
    // R7.1-b：`jishi 错误 [码]` 搬进 Rust（工具链迁移的第一件）。
    // 输出**逐字照** Python 侧 `cli._cmd_errcode`（含「没有这个码」走 stderr + 退码 2），
    // 对拍在 `tests/test_m85_errcodes_rust.py`。
    if args[1] == "错误" || args[1] == "--errcode" {
        let want = args.get(2).map(|s| s.as_str()).unwrap_or("");
        let (out, err, code) = jishi_ffi::errcodes::render(want);
        print!("{out}");
        eprint!("{err}");
        return ExitCode::from(code as u8);
    }
    // 自报家底（两张表 → JSON 文本），给漂移检测用：Python 侧按同一结构比。
    if args[1] == "--dump-errcodes" || args[1] == "--dump_errcodes" {
        println!("{}", jishi_ffi::errcodes::dump_json());
        return ExitCode::SUCCESS;
    }
    // R7.1-c：`jishi 源码静态检查` 搬进 Rust（四条 lint，逐字对齐 Python 侧）。
    //
    // 🔴 2026-10-07（R7.6）改了命令名：以前这里叫 `检查`，但 Python 侧的 `检查`
    // 是**包依赖冲突检测**（`_run_package_cmd`），源码检查叫 `源码静态检查`
    // （别名 `静态检查`）。R7.6 把包管理搬进来时，`检查` 必须让回给包管理 ——
    // 否则 `jishi-rs 检查` 与 `jishi 检查` 是两条不同的命令，等 Rust 顶上
    // `jishi` 这个名字时脚本会**静默走错命令**（这是本项目最想避免的一类问题）。
    if args[1] == "源码静态检查" || args[1] == "静态检查" || args[1] == "--check" {
        return run_check(&args[2..]);
    }
    // R7.1-d：`jishi 格式化` 搬进 Rust（保留注释与字面量原样，只调排版）。
    if args[1] == "格式化" || args[1] == "--format" {
        return run_format(&args[2..]);
    }
    // R7.2：语言元数据（`jishi --lang-spec`，机器可读）。输出与 Python 侧**逐字节一致**。
    if args[1] == "--lang-spec" || args[1] == "--lang_spec"
        || args[1] == "--dump-lang-spec" || args[1] == "--dump_lang_spec"
    {
        println!("{}", jishi_ffi::langspec::lang_spec_json());
        return ExitCode::SUCCESS;
    }
    // R7.3：语言服务器（stdio + JSON-RPC）—— 编辑器那条路。
    if args[1] == "lsp" || args[1] == "--lsp" {
        return ExitCode::from(jishi_ffi::lsp::run_lsp() as u8);
    }
    // R7.5：调试适配器（stdio + DAP）—— 编辑器那条路。
    // `--执行器 树遍历`（默认就是它）；别的值由 `run_dap` 明确拒绝（不静默降级）。
    if args[1] == "dap" || args[1] == "--dap" {
        let mut engine = "树遍历".to_string();
        let mut i = 2;
        while i < args.len() {
            if (args[i] == "--执行器" || args[i] == "--engine") && i + 1 < args.len() {
                engine = args[i + 1].clone();
                i += 2;
            } else {
                i += 1;
            }
        }
        return ExitCode::from(jishi_ffi::dap::run_dap(&engine) as u8);
    }
    // R7.5 收尾：`jishi 调试`（交互式命令行）搬进 Rust —— 会话核心在 R7.5 已就绪，
    // 这一轮补的是**命令表**（断点/变量/查看/栈/源码/帮助…）与 `LineDriver`。
    // `--执行器 vm/cvm` 明确拒绝（与 dap 同一登记）。
    if args[1] == "调试" || args[1] == "--debug" {
        return ExitCode::from(jishi_ffi::debugger::run_debug(&args[2..]) as u8);
    }
    if args[1] == "--mcp-spawn-probe" {
        return spawn_probe();
    }
    // R7.4：MCP 服务器（stdio + 换行分隔 JSON-RPC）—— agent 那条路。
    if args[1] == "mcp" || args[1] == "--mcp" {
        return ExitCode::from(jishi_ffi::mcp::run_mcp() as u8);
    }
    // R7.2：AI 语言卡（由同一份元数据渲染的 Markdown）。
    if args[1] == "--ai-card" || args[1] == "--ai_card" {
        println!("{}", jishi_ffi::langspec::render_ai_card());
        return ExitCode::SUCCESS;
    }
    // 静态检查用到的四个符号集合（关键字/内建/标准库模块/异常类型）自报家底。
    if args[1] == "--dump-check-symbols" || args[1] == "--dump_check_symbols" {
        let sym = jishi_ffi::checker::Symbols::from_host();
        println!("{}", jishi_ffi::checker::dump_symbols_json(&sym));
        return ExitCode::SUCCESS;
    }
    // R7.1-d：格式化器的符号集合（运算符 / 关键字 / 引号）自报家底，给漂移检测用。
    if args[1] == "--dump-format-tables" || args[1] == "--dump_format_tables" {
        println!("{}", jishi_ffi::formatter::dump_tables_json());
        return ExitCode::SUCCESS;
    }
    // R7.6：包管理子命令（安装/卸载/列表/发布/索引/检查）走独立入口 ——
    // 避免与「运行文件」的位置参数冲突（与 Python 的 `cli.main` 同口径）。
    if matches!(args[1].as_str(),
                "安装" | "卸载" | "列表" | "发布" | "索引" | "检查") {
        return jishi_ffi::packages::run_package_cmd(&args[1..]);
    }
    // R8.4：`医生` / `新项目` / `教程` —— 发行包换成单二进制之后**不能缺**的三件
    // （`jishi 医生` 正是安装说明里让用户跑的第一个命令）。
    // ⚠️ `医生` 是**有意重写**的：Python 版报「Python 版本 / C VM / MCP Server」，
    //    那三样在单二进制里根本不存在（见 `tools.rs` 模块头）。
    if args[1] == "医生" || args[1] == "--doctor" {
        let no_net = args[2..].iter().any(|a| a == "--no-net" || a == "--no_net");
        return jishi_ffi::tools::run_doctor(no_net);
    }
    if args[1] == "新项目" || args[1] == "--new" {
        match args.get(2) {
            Some(n) if !n.starts_with('-') => {
                return jishi_ffi::tools::run_new_project(n);
            }
            _ => {
                // Python 侧走 argparse，打的是英文 usage —— 这里给一句中文即可
                // （argparse 的换行跟终端宽度有关，照抄没意义；只对齐退码 2）。
                eprintln!("用法：jishi 新项目 <名字>");
                return ExitCode::from(2);
            }
        }
    }
    if args[1] == "教程" || args[1] == "--tutorial" {
        return jishi_ffi::tools::run_tutorial();
    }
    // 其余一律交给 CLI 主入口（R7.4）。
    //
    // ⚠️ **位置参数的语义从 R7.4 起是「源码」**（与 Python 的 `jishi <文件>` 对齐），
    // 字节码要走 `--load-bytecode`。以前这里把 `args[1]` 直接当字节码 JSON 读，
    // 于是 `jishi-rs 你好.jsh` 会得到一句莫名的「字节码加载失败」——
    // 而单二进制顶上 `jishi` 这个名字之后（R8），那条命令必须能跑。
    jishi_ffi::run::main_cli(&args[1..])
}
