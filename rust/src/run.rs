//! `jishi` 命令行主入口（`jishi/cli.py` 的移植；R7.4）。
//!
//! 覆盖的范围（与 Python 侧 `cli.main` 逐条对应）：
//!
//! | 开关 | 说明 |
//! |---|---|
//! | `<文件>` / `--stdin` | 跑一段**源码**（位置参数从 R7.4 起是源码，不再是字节码） |
//! | `--load-bytecode` | 输入的是**字节码 JSON**（配合 `<文件>` / `--stdin`） |
//! | `--dump-bytecode` | 把源码编译成字节码 JSON 打印出来（与 `--load-bytecode` 互为往返） |
//! | `--ast` | 只打印语法树，不运行 |
//! | `--json-errors` | 报错以 JSON 输出（走 stderr） |
//! | `--sandbox` / `--json-result` | 沙箱执行 + 统一协议 JSON（见 `sandbox.rs`） |
//! | `--timeout N` | 沙箱超时秒数（默认 5） |
//! | `--ai-card` / `--lang-spec` / `--format json` | 语言卡 / 机器可读规格（R7.2） |
//! | `-v` / `--version` | 版本号 |
//!
//! ✅ **R8.4b 起全搬完了**：`-i/--repl`（交互式，见 `crate::repl`）与
//! 医生/新项目/教程 三个子命令（见 `crate::tools`）都在。**包管理（R7.6）不在这里** ——
//! 它是 `crate::packages`（`jishi-rs 安装/卸载/列表/发布/索引/检查`），由 `main.rs`
//! 在进本模块**之前**就分发掉了（与 Python 的 `cli.main` 同一形状）。
//!
//! ⚠️ `--dump-bytecode` 有一处**已知差异**：Python 侧编译前会展开「用基石写的
//! 标准库」（`stdlib-jishi/*.jsh` 编成外壳函数），宿主**不展开** —— 它用的是
//! 原生标准库实现（25 个模块一个不缺）。所以**只有**用到 `参数/容器/统计/迭代`
//! 这四个模块的程序，dump 出来的字节码与 Python 不同；行为一致（M52 的契约，
//! 五执行器对拍盯着）。要把这条也抹平，得把那几个 `.jsh` 嵌进二进制 ——
//! 与「分发形态」是同一件事，留到 R7.6。

use std::path::Path;
use std::process::ExitCode;

use jishi_frontend::parser::{J, Node};

use crate::jsonw::{dump_compact, Jv};
use crate::sandbox::{self, SandboxOpts};
use crate::JishiError;

/// 命令行选项（与 Python 侧 `argparse` 的默认值逐条对齐）。
#[derive(Default)]
struct Opts {
    file: Option<String>,
    repl: bool,
    ast: bool,
    json_errors: bool,
    ai_card: bool,
    lang_spec: bool,
    sandbox: bool,
    json_result: bool,
    stdin: bool,
    load_bytecode: bool,
    dump_bytecode: bool,
    timeout: f64,
    version: bool,
    rest: Vec<String>,
}

pub fn main_cli(args: &[String]) -> ExitCode {
    let mut o = Opts { timeout: 5.0, ..Default::default() };
    let mut i = 0;
    let mut seen_file = false;
    while i < args.len() {
        let a = args[i].clone();
        match a.as_str() {
            "-i" | "--repl" => o.repl = true,
            "--ast" => o.ast = true,
            "--json-errors" => o.json_errors = true,
            "--ai-card" => o.ai_card = true,
            "--lang-spec" => o.lang_spec = true,
            "--sandbox" => o.sandbox = true,
            "--json-result" => o.json_result = true,
            "--stdin" => o.stdin = true,
            "--load-bytecode" => o.load_bytecode = true,
            "--dump-bytecode" => o.dump_bytecode = true,
            "--no-cache" => {}          // 宿主没有 AST 缓存：接受就等于生效
            "-v" | "--version" => o.version = true,
            "--format" | "--timeout" => {
                // 这两个跟一个值。`--format` 只有 json 一种，值本身不校验。
                let val = args.get(i + 1).cloned().unwrap_or_default();
                if a == "--timeout" {
                    match val.parse::<f64>() {
                        Ok(t) => o.timeout = t,
                        Err(_) => {
                            eprintln!("--timeout 需要一个数字");
                            return ExitCode::from(2);
                        }
                    }
                }
                i += 1;
            }
            _ => {
                if let Some(v) = a.strip_prefix("--timeout=") {
                    match v.parse::<f64>() {
                        Ok(t) => o.timeout = t,
                        Err(_) => {
                            eprintln!("--timeout 需要一个数字");
                            return ExitCode::from(2);
                        }
                    }
                } else if a.starts_with("--format=") {
                    // 忽略：只有 json 一种格式
                } else if a.starts_with('-') && a != "-" {
                    // 与 Python 侧 `parse_known_args` 同口径：认不出的参数落到 extras
                    o.rest.push(a);
                } else if !seen_file {
                    seen_file = true;
                    o.file = Some(a);
                } else {
                    o.rest.push(a);
                }
            }
        }
        i += 1;
    }

    if o.version {
        println!("基石 jishi {}", crate::langdata::LANG_VERSION);
        return ExitCode::SUCCESS;
    }
    // ⚠️ 与 Python 侧同序：语言卡 / 规格**先于**字节码与运行判断。
    if o.ai_card {
        println!("{}", crate::langspec::render_ai_card());
        return ExitCode::SUCCESS;
    }
    if o.lang_spec {
        println!("{}", crate::langspec::lang_spec_json());
        return ExitCode::SUCCESS;
    }
    if o.file.is_none() && !o.stdin {
        // R8.4b：与 Python 同口径 —— **没给文件就进 REPL**（`jishi` 与 `jishi -i`
        // 都是这个行为），不再打用法然后退 2。
        return crate::repl::run_repl();
    }
    run_file(&o)
}

fn run_file(o: &Opts) -> ExitCode {
    let (text, filename) = if o.stdin {
        (read_stdin(), "<stdin>".to_string())
    } else {
        match read_source(o.file.as_deref().unwrap_or("")) {
            Ok(t) => t,
            Err(code) => return code,
        }
    };

    // `--load-bytecode`：输入的是字节码 JSON（R7.4 起位置参数的默认语义是**源码**）
    if o.load_bytecode {
        return run_bytecode(&text, o);
    }
    if o.dump_bytecode {
        return match sandbox::compile_source(&text, &filename) {
            Ok(json) => {
                println!("{json}");
                ExitCode::SUCCESS
            }
            Err(e) => report(&e, &filename, &text.split('\n').map(str::to_string)
                                 .collect::<Vec<_>>(), o.json_errors),
        };
    }

    let lines: Vec<String> = text.replace("\r\n", "\n").replace('\r', "\n")
        .split('\n').map(|s| s.to_string()).collect();

    // 沙箱 / 统一协议结果：走 run_sandboxed，输出 protocol JSON。
    if o.sandbox || o.json_result {
        let opts = SandboxOpts { timeout: o.timeout, ..Default::default() };
        let result = sandbox::run_sandboxed(&text, &opts, &filename);
        let ok = result.starts_with("{\"ok\": true");
        println!("{result}");
        return if ok { ExitCode::SUCCESS } else { ExitCode::from(1) };
    }

    // 先解析（解析错也当一条错误报 —— 与 Python 侧「先解析再跑」同序）
    let prog = match jishi_frontend::parser::parse(&text, &filename) {
        Ok(p) => p,
        Err(e) => return report(&sandbox::parse_err_to_jishi(&e), &filename, &lines,
                                o.json_errors),
    };

    if o.ast {
        println!("{}", dump_ast(&J::Node(Box::new(prog)), 0));
        return ExitCode::SUCCESS;
    }

    let module = jishi_frontend::compiler::compile_program(&prog, &filename, None, false);
    let bytecode = jishi_frontend::serialize::dump_json(&module);
    crate::set_args(o.rest.clone());
    let mut vm = match crate::load_module(&bytecode) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("字节码加载失败：{e}");
            return ExitCode::from(1);
        }
    };
    match vm.run() {
        Ok(()) => {
            print!("{}", crate::get_output(&vm));
            eprint!("{}", crate::get_output_err(&vm));
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprint!("{}", crate::get_output_err(&vm));
            print!("{}", crate::get_output(&vm));   // 出错前打印过的内容照样吐
            report(&e, &filename, &lines, o.json_errors)
        }
    }
}

/// `--load-bytecode`：字节码 JSON → VM。
fn run_bytecode(text: &str, o: &Opts) -> ExitCode {
    crate::set_args(o.rest.clone());
    let mut vm = match crate::load_module(text) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("字节码加载失败：{e}");
            return ExitCode::from(1);
        }
    };
    match vm.run() {
        Ok(()) => {
            print!("{}", crate::get_output(&vm));
            eprint!("{}", crate::get_output_err(&vm));
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprint!("{}", crate::get_output_err(&vm));
            print!("{}", crate::get_output(&vm));
            // 字节码里没有源码文本，源码框按 `filename` 现读（`render_error` 负责）。
            if o.json_errors {
                let payload = Jv::Obj(vec![
                    ("ok".to_string(), Jv::Bool(false)),
                    ("errors".to_string(),
                     Jv::Arr(vec![sandbox::error_json(&e)])),
                ]);
                eprintln!("{}", dump_compact(&payload));
            } else {
                eprint!("{}", crate::render_error(&vm, &e));
            }
            ExitCode::from(1)
        }
    }
}

/// 报错：`--json-errors` 给机器读的 JSON（**走 stderr**），否则给渲染块。
fn report(e: &JishiError, filename: &str, lines: &[String], json_errors: bool) -> ExitCode {
    if json_errors {
        let payload = Jv::Obj(vec![
            ("ok".to_string(), Jv::Bool(false)),
            ("errors".to_string(), Jv::Arr(vec![sandbox::error_json(e)])),
        ]);
        eprintln!("{}", dump_compact(&payload));
    } else {
        eprint!("{}", render_err(e, filename, lines));
    }
    ExitCode::from(1)
}

/// 渲染报错块 —— 源码行从**内存里那份**取（`--stdin` 时磁盘上没有这个文件）。
pub fn render_err(e: &JishiError, filename: &str, lines: &[String]) -> String {
    crate::render_error_with(e, filename, &|line| {
        if line >= 1 {
            lines.get((line - 1) as usize).cloned()
        } else {
            None
        }
    })
}

// ---------------------------------------------------------------------------
// 读文件 / 读 stdin（与 Python `_read_source` / `_read_stdin` 同口径）
// ---------------------------------------------------------------------------

fn read_stdin() -> String {
    use std::io::Read;
    let mut data: Vec<u8> = Vec::new();
    let _ = std::io::stdin().read_to_end(&mut data);
    // 字节 → UTF-8（非法字节替换成 U+FFFD，让词法器给出中文错，而不是解码处崩）
    String::from_utf8_lossy(&data).to_string()
}

/// 读源码文件。**文本模式**（`\r\n` / `\r` 都归一成 `\n`）——
/// 与 Python 的 `open(path, "r")` 一致，否则报错里的源码行会带上 `\r`。
fn read_source(path: &str) -> Result<(String, String), ExitCode> {
    use std::io::ErrorKind::*;
    if Path::new(path).is_dir() {
        eprintln!("「{path}」是个目录。要整个目录一起处理请用：\
「jishi 格式化 {path} --check」或「… --write」；只想看一个文件就把文件名写全");
        return Err(ExitCode::from(2));
    }
    match std::fs::read_to_string(path) {
        Ok(t) => Ok((t.replace("\r\n", "\n").replace('\r', "\n"), path.to_string())),
        Err(e) => {
            let code = match e.kind() {
                NotFound => {
                    eprintln!("找不到文件「{path}」");
                    1
                }
                PermissionDenied => {
                    eprintln!("没有权限读取「{path}」");
                    2
                }
                InvalidData => {
                    eprintln!("文件「{path}」不是 UTF-8 编码，无法读取");
                    1
                }
                _ => {
                    eprintln!("读不了文件「{path}」：{e}");
                    2
                }
            };
            Err(ExitCode::from(code as u8))
        }
    }
}

// ---------------------------------------------------------------------------
// `--ast`：`cli.dump_ast` 的移植
// ---------------------------------------------------------------------------

/// 把 AST 转成缩进文本（调试用）—— 输出**逐字**照 Python `cli.dump_ast`：
///
/// * 容器：元素**同级**并列（不再加一层缩进），空容器写成 `list()`；
/// * 节点：`名字(` + 每个字段一行 + `)`，`line` / `col` 不印；
/// * 字段值是节点时**内联**（`target: Name(…)`），多行只把第一行接到 `字段:` 后面
///   —— 这是 Python 那段代码的**怪但真实**的效果（`dump_ast(v, indent+1).strip()`）。
pub fn dump_ast(v: &J, indent: usize) -> String {
    match v {
        J::Node(n) => dump_node(n, indent),
        J::List(items) => items.iter()
            .map(|x| dump_ast(x, indent))
            .collect::<Vec<_>>().join("\n"),
        leaf => format!("{}{}", "  ".repeat(indent), py_repr_leaf(leaf)),
    }
}

fn dump_node(n: &Node, indent: usize) -> String {
    let pad = "  ".repeat(indent);
    let mut fields: Vec<String> = Vec::new();
    for (k, v) in &n.fields {
        if *k == "line" || *k == "col" {
            continue;
        }
        match v {
            J::List(items) => {
                if items.is_empty() {
                    fields.push(format!("{k}: list()"));
                } else {
                    let body = items.iter()
                        .map(|x| dump_ast(x, indent + 1))
                        .collect::<Vec<_>>().join("\n");
                    fields.push(format!("{k}:\n{body}"));
                }
            }
            J::Node(_) => fields.push(format!("{k}: {}", dump_ast(v, indent + 1).trim())),
            leaf => fields.push(format!("{k}: {}", py_repr_leaf(leaf))),
        }
    }
    // ⚠️ **`_super_ok` 要单独补**：Python 侧它是 `setattr(node, "_super_ok", True)`
    // 动态挂上去的，所以 `vars(node)` 里它排在**所有声明字段之后**；
    // 而 Rust 的 AST 把它做成了一个**结构体字段**（`Node.super_ok`），不在
    // `fields` 里 —— 于是 `--ast` 以前少印这一行（R7.4 的 CLI 闸门照出来的）。
    if n.super_ok {
        fields.push("_super_ok: True".to_string());
    }
    let mut body = format!("{pad}{}", n.kind);
    body.push('(');
    if !fields.is_empty() {
        let inner = fields.iter().map(|f| format!("  {f}"))
            .collect::<Vec<_>>().join("\n");
        body.push('\n');
        body.push_str(&inner);
        body.push('\n');
        body.push_str(&pad);
    }
    body.push(')');
    body
}

/// 叶子值的 `repr` —— 与 Python 的 `repr()` 同形（`'甲'` / `1` / `1.0` / `True` / `None`）。
fn py_repr_leaf(v: &J) -> String {
    match v {
        J::Null => "None".to_string(),
        J::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        J::Int(i) => i.to_string(),
        // 超 i64 的整数在 AST JSON 里是**字符串**，但 Python 侧那个值还是 int
        // → `repr` 是裸数字（不带引号）。
        J::Big(s) => s.clone(),
        J::Float(f) => jishi_frontend::serialize::py_repr_float(*f),
        J::Str(s) => py_repr_str(s),
        J::List(_) | J::Node(_) => String::new(),   // 上面已分流，走不到
    }
}

/// 字符串的 `repr`：优先单引号；串里含 `'` 且不含 `"` 才改用双引号。
/// 控制字符按 Python 的写法转义（`\n` / `\t` / `\r` / `\xNN`）。
fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || (c as u32) == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}
