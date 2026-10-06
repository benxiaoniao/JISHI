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

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("用法：jishi-rs 字节码.json");
        eprintln!("      jishi-rs --dump-stdlib");
        eprintln!("      jishi-rs --dump-methods");
        eprintln!("      jishi-rs --dump-builtins");
        eprintln!("      jishi-rs 错误 [码]        # 查错误码（R7.1-b）");
        eprintln!("      jishi-rs 检查 [路径...] [--json]   # 源码静态检查（R7.1-c）");
        eprintln!("      jishi-rs 格式化 [文件] [--stdin] [--check] [--write] [--indent N]");
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
    if args[1] == "检查" || args[1] == "--check" {
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
            // 防御：正常跑完时错误流是空的（`测试.汇总` 有失败就会抛）。
            eprint!("{}", jishi_ffi::get_output_err(&vm));
            ExitCode::SUCCESS
        }
        Err(e) => {
            // 错误流缓冲（`测试.汇总` 的失败明细）先吐 —— 与 Python 侧的 stderr
            // 内容顺序一致（先 ✗ 明细、再错误块）。
            eprint!("{}", jishi_ffi::get_output_err(&vm));
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
