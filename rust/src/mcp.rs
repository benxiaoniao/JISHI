//! MCP 服务器（`jishi/mcp_server.py` 的移植；R7.4）—— `jishi-rs mcp`。
//!
//! stdio 传输（**换行分隔**的 JSON-RPC 2.0），暴露七个工具：
//!
//! | 工具 | 作用 |
//! |---|---|
//! | `run_script(script, …)` | 沙箱执行基石脚本（**子进程隔离 + 到点真 kill**） |
//! | `eval_expr(expr)` | 沙箱求值一个表达式 |
//! | `describe_language()` | 返回 AI 语言卡 |
//! | `check_source(source)` | 静态检查（与 `jishi 源码静态检查` 同一份实现） |
//! | `lookup_error(code)` | 查错误码 / 静态检查码的含义 |
//! | `lookup_stdlib(module[, function])` | 查标准库签名与说明 |
//! | `format_source(source[, indent])` | 按官方风格格式化 |
//!
//! 📌 **事实来源只有一份**：错误码 / 检查码 / 标准库签名都从 `langdata.rs`
//! （那张表由 `tools/gen_langdata_rs.py` 一次性搬运、`test_m90` 钉着）取，
//! 与 `jishi --lang-spec` 同源 —— 规格变了工具自动跟着变，不会各说各话。
//!
//! ⚠️ **输出要逐字节一致**：`json.dumps(..., ensure_ascii=False)` 的键顺序
//! 就是 Python 侧 dict 的插入顺序，所以下面每个 `Jv::Obj` 的 push 次序
//! 都是**照抄** Python 的（错一处就是「两边的报文不一样」）。

use std::io::{Read, Write};

use crate::json::Json;
use crate::jsonw::{dump_compact, Jv};
use crate::langdata as D;

/// 与 Python 侧 `PROTOCOL_VERSION` 同一个字符串。
const PROTOCOL_VERSION: &str = "2024-11-05";

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

fn s(x: &str) -> Jv {
    Jv::Str(x.to_string())
}

fn obj(kv: Vec<(&str, Jv)>) -> Jv {
    Jv::Obj(kv.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn arr(items: Vec<Jv>) -> Jv {
    Jv::Arr(items)
}

/// `Json`（解析出来的请求）→ `Jv`（要吐出去的响应）。顺序无所谓 —— 响应
/// 的键顺序是**我们**定的，不跟着请求走。
fn jv_of(j: &Json) -> Jv {
    match j {
        Json::Null => Jv::Null,
        Json::Bool(b) => Jv::Bool(*b),
        Json::Int(i) => match i64::try_from(*i) {
            Ok(small) => Jv::Int(small),
            Err(_) => Jv::Int128(*i),
        },
        Json::Float(f) => Jv::Float(*f),
        Json::Str(x) => Jv::Str(x.clone()),
        Json::Arr(items) => Jv::Arr(items.iter().map(jv_of).collect()),
        Json::Obj(m) => Jv::Obj(m.iter().map(|(k, v)| (k.clone(), jv_of(v))).collect()),
    }
}

/// 取字符串参数（Python 侧是 `str(args.get(...))`：数字也转文本）。
fn arg_text(args: &Json, key: &str) -> String {
    match args.get(key) {
        Some(Json::Str(x)) => x.clone(),
        Some(Json::Int(i)) => i.to_string(),
        Some(Json::Float(f)) => jishi_frontend::serialize::py_repr_float(*f),
        Some(Json::Bool(b)) => if *b { "True" } else { "False" }.to_string(),
        _ => String::new(),
    }
}

/// 取数字参数（Python 侧是 `float(... or default)`；取不到就给默认值）。
fn arg_f64(args: &Json, key: &str, default: f64) -> f64 {
    match args.get(key) {
        Some(Json::Int(i)) => *i as f64,
        Some(Json::Float(f)) => *f,
        Some(Json::Str(x)) => x.trim().parse::<f64>().unwrap_or(default),
        _ => default,
    }
}

fn arg_true(args: &Json, key: &str) -> bool {
    matches!(args.get(key), Some(Json::Bool(true)))
}

/// 工具调用出错时的兜底 JSON（Python `_handle` 里那个 `except Exception`）。
fn oops(message: String) -> String {
    dump_compact(&obj(vec![
        ("ok", Jv::Bool(false)),
        ("error", obj(vec![("message", s(&message))])),
    ]))
}

// ---------------------------------------------------------------------------
// tools/list 的 inputSchema
// ---------------------------------------------------------------------------

/// 造一个工具的 `inputSchema`。
///
/// ⚠️ `required` 为 `None` 时**整个键都不出现**（Python 侧 `describe_language`
/// 的 schema 就只有 `type` 和 `properties` 两个键）。
fn schema(props: &[(&str, &str, &str)], required: Option<&[&str]>) -> Jv {
    let mut kv = vec![
        ("type".to_string(), s("object")),
        ("properties".to_string(), Jv::Obj(
            props.iter().map(|(k, ty, doc)| {
                (k.to_string(), obj(vec![("type", s(ty)), ("description", s(doc))]))
            }).collect())),
    ];
    if let Some(req) = required {
        kv.push(("required".to_string(),
                  Jv::Arr(req.iter().map(|r| s(r)).collect())));
    }
    Jv::Obj(kv)
}

fn tool(name: &str, desc: &str, schema_jv: Jv) -> Jv {
    obj(vec![
        ("name", s(name)),
        ("description", s(desc)),
        ("inputSchema", schema_jv),
    ])
}

/// 七个工具（`mcp_server.TOOLS` 的等价物）—— 描述与 schema **逐字照抄**。
fn tools() -> Vec<Jv> {
    vec![
        tool("run_script",
             "在沙箱里执行一段基石（中文编程语言）脚本，返回执行结果。脚本用中文关键字（函数/如果/遍历/打印…）。",
             schema(&[
                 ("script", "string", "要执行的基石源码"),
                 ("timeout", "number", "超时秒数，默认 5"),
                 ("allow_write", "boolean", "是否允许文件读写，默认否"),
                 ("allow_python", "boolean", "是否允许 Python 桥接导入，默认否"),
             ], Some(&["script"]))),
        tool("eval_expr", "在沙箱里求值一个基石表达式，返回其值。",
             schema(&[("expr", "string", "要求值的表达式")], Some(&["expr"]))),
        tool("describe_language",
             "返回基石语言卡（语法速查 + 内建 API），供 agent 学习如何写基石代码。",
             schema(&[], None)),
        tool("check_source",
             "对一段基石源码做静态检查（不运行），返回未定义名 / 遮蔽内建 / 未使用变量 / 可疑相等等问题。写代码后自查、或排查「为什么编辑器画了黄线」时用。",
             schema(&[("source", "string", "要检查的基石源码")], Some(&["source"]))),
        tool("lookup_error",
             "查基石错误码的含义。拿到报错里的 E 码（如 E2002）后，用它换到中文标题、触发类与修复建议。",
             schema(&[("code", "string",
                       "错误码，如 E2002；也接受静态检查码如 name.undefined")],
                    Some(&["code"]))),
        tool("lookup_stdlib",
             "查基石标准库的用法。传模块名列出该模块全部函数与签名；再传函数名则返回该函数的说明。遇到「这个功能有没有内建」时先查这里，别自己手写。",
             schema(&[
                 ("module", "string",
                  "标准库模块名，如 数学 / 容器 / 迭代 / 正则"),
                 ("function", "string", "可选，模块内的函数名；不传则列出整个模块"),
             ], Some(&["module"]))),
        tool("format_source",
             "按基石官方风格格式化一段源码（统一缩进与空格），返回格式化后的代码。",
             schema(&[
                 ("source", "string", "要格式化的基石源码"),
                 ("indent", "number", "缩进空格数，默认 4"),
             ], Some(&["source"]))),
    ]
}

// ---------------------------------------------------------------------------
// 各工具的实现
// ---------------------------------------------------------------------------

/// `_cli_command()`：调自己（宿主就是 CLI 本体；Python 侧源码运行是
/// `[python, -m, jishi.cli]`，打包后也是找同级 `jishi`）。
fn cli_command() -> Vec<std::ffi::OsString> {
    vec![std::env::current_exe()
        .unwrap_or_else(|_| std::path::PathBuf::from("jishi-rs"))
        .into_os_string()]
}

/// `_run_subprocess`：子进程隔离执行 —— **到点真 kill**（这是 R7.4 的验收原文）。
fn run_script(args: &Json) -> String {
    let script = arg_text(args, "script");
    let timeout = arg_f64(args, "timeout", 5.0);
    let allow_write = arg_true(args, "allow_write");
    let allow_python = arg_true(args, "allow_python");

    // ⚠️ **stdin 不用管道** —— 本机（Windows）上给 stdin 方向 `CreatePipe`
    // 稳定失败：`ERROR_PIPE_BUSY`（os error 231）。`jishi-rs --mcp-spawn-probe`
    // 实测：**只有 stdin 是管道时失败**，把 stdout/stderr 换成 null/inherit 都不
    // 影响；目标程序换成 `cmd` 也一样 —— 所以不是「自己 spawn 自己」的问题，
    // 就是 stdin 管道这一件事（兄弟症状见 `test_m31` 那条已登记的
    // `spawnSync node.exe EBUSY`）。改用**临时文件当 stdin**：子进程照旧按字节
    // 读标准输入（`--stdin` 的语义一点没变），只是不走管道。
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let name = format!("jishi-mcp-{}-{stamp}.jsh", std::process::id());
    // ⚠️ 优先系统临时目录；**写不进去就退到当前目录** —— 环境被清空
    // （没有 TEMP/TMP）时 `temp_dir()` 会落到 `C://Windows//` 那种不可写的地方，
    // 直接报「拒绝访问」把工具调用弄挂。这是工具链要扛住的输入之一
    // （agent 的运行环境不一定有完整环境变量）。
    let tmp = match std::fs::write(std::env::temp_dir().join(&name), script.as_bytes()) {
        Ok(()) => std::env::temp_dir().join(&name),
        Err(_) => {
            let here = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
            let alt = here.join(&name);
            if let Err(e) = std::fs::write(&alt, script.as_bytes()) {
                return internal_error(&format!("写不了临时文件：{e}"), 0.0);
            }
            alt
        }
    };
    let stdin_file = match std::fs::File::open(&tmp) {
        Ok(f) => f,
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            return internal_error(&format!("读不了临时文件：{e}"), 0.0);
        }
    };

    let mut cmd = std::process::Command::new(cli_command()[0].clone());
    cmd.args(["--stdin", "--sandbox", "--json-result", "--timeout"])
        .arg(jishi_frontend::serialize::py_repr_float(timeout))
        .stdin(std::process::Stdio::from(stdin_file))
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    // 沙箱选项走环境变量（与 Python 侧同一条路；CLI 层面还没展开这两个开关）
    if allow_write {
        cmd.env("JISHI_SANDBOX_ALLOW_WRITE", "1");
    }
    if allow_python {
        cmd.env("JISHI_SANDBOX_ALLOW_PYTHON", "1");
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            return internal_error(&format!("子进程起不来：{e}"), 0.0);
        }
    };

    // 读 stdout/stderr 各起一条线程：**必须**同时读，否则子进程写满管道
    // 就会卡住，而我们还在等它退出 —— 那就成了假超时。
    let out_pipe = child.stdout.take().unwrap();
    let err_pipe = child.stderr.take().unwrap();
    let h_out = std::thread::spawn(move || read_all(out_pipe));
    let h_err = std::thread::spawn(move || read_all(err_pipe));

    // 等它退出；到点就 kill（Python 的 `subprocess.run(timeout=…)` 同语义）
    let deadline = std::time::Instant::now()
        + std::time::Duration::from_secs_f64(timeout.max(0.0) + 2.0);
    let mut timed_out = false;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => {
                if std::time::Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    timed_out = true;
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            Err(_) => break,
        }
    }
    let code = child.try_wait().ok().flatten()
        .map(|st| st.code().unwrap_or(1)).unwrap_or(1);
    let _ = std::fs::remove_file(&tmp);
    let out = h_out.join().unwrap_or_default();
    let err = h_err.join().unwrap_or_default();

    if timed_out {
        return dump_compact(&obj(vec![
            ("ok", Jv::Bool(false)),
            ("error", obj(vec![
                ("code", s("E2000")),
                ("title", s("运行期错误")),
                ("message", s(&format!("执行超时（{} 秒）",
                                       jishi_frontend::serialize::py_repr_float(timeout)))),
                ("line", Jv::Null),
                ("col", Jv::Null),
                ("hint", Jv::Null),
            ])),
            ("duration_ms", Jv::Float(timeout * 1000.0)),
        ]));
    }
    let text = String::from_utf8_lossy(&out).trim().to_string();
    if !text.is_empty() {
        return text;
    }
    // `{proc.stderr[:200]}` —— Python 那边是按**字符**切前 200 个。
    let err_text = String::from_utf8_lossy(&err).to_string();
    let head: String = err_text.chars().take(200).collect();
    internal_error(&format!("子进程异常退出（code={code}）: {head}"), 0.0)
}

fn read_all(mut r: impl Read) -> Vec<u8> {
    let mut buf = Vec::new();
    let _ = r.read_to_end(&mut buf);
    buf
}

/// `子进程异常退出` 那条兜底（Python 侧 `_run_subprocess` 的最后一个分支）。
fn internal_error(message: &str, duration: f64) -> String {
    dump_compact(&obj(vec![
        ("ok", Jv::Bool(false)),
        ("error", obj(vec![
            ("code", s("E0000")),
            ("title", s("内部错误")),
            ("message", s(message)),
            ("line", Jv::Null),
            ("col", Jv::Null),
            ("hint", Jv::Null),
        ])),
        ("duration_ms", Jv::Float(duration)),
    ]))
}

/// `eval_expr`。
fn eval_expr(args: &Json) -> String {
    let expr = arg_text(args, "expr");
    match crate::sandbox::eval_sandboxed(&expr, &crate::sandbox::SandboxOpts::default(),
                                        "<输入>") {
        Ok(j) => j,
        // 值不是 JSON 能表达的 —— Python 侧 `json.dumps` 在这里冒 `TypeError`，
        // 被 `_handle` 的兜底接住，翻成一句 `{"ok": false, …}`。
        Err(name) => oops(format!(
            "TypeError: Object of type {name} is not JSON serializable")),
    }
}

/// `describe_language`。
fn describe_language() -> String {
    dump_compact(&obj(vec![
        ("ok", Jv::Bool(true)),
        ("card", s(&crate::langspec::render_ai_card())),
    ]))
}

/// `check_source` —— 与命令行 `jishi 源码静态检查` **同一份实现**。
fn check_source(args: &Json) -> String {
    let src = arg_text(args, "source");
    let sym = crate::checker::Symbols::from_host();
    let issues = crate::checker::check_source(&src, "<检查>", &sym);
    let items: Vec<Jv> = issues.iter().map(|it| obj(vec![
        ("code", s(&it.code)),
        ("level", s(it.level)),
        ("line", Jv::Int(it.line)),
        ("col", Jv::Int(it.col)),
        ("message", s(&it.message)),
        ("hint", s(&it.hint)),
        ("name", s(&it.name)),
    ])).collect();
    dump_compact(&obj(vec![
        ("ok", Jv::Bool(true)),
        ("问题数", Jv::Int(items.len() as i64)),
        ("问题", arr(items)),
    ]))
}

/// `lookup_error` —— 两张表都从 `langdata`（与 `--lang-spec` 同源）取。
fn lookup_error(args: &Json) -> String {
    let code = arg_text(args, "code").trim().to_string();
    if code.is_empty() {
        return oops("缺少 code 参数".to_string());
    }
    let lower = code.to_lowercase();
    for (c, level, name, doc) in D::CHECK_CODES {
        if c.to_lowercase() == lower {
            return dump_compact(&obj(vec![
                ("ok", Jv::Bool(true)),
                ("kind", s("静态检查码")),
                ("code", s(c)),
                ("level", s(level)),
                ("name", s(name)),
                ("doc", s(doc)),
            ]));
        }
    }
    let upper = code.to_uppercase();
    for (c, class, title, internal) in D::ERROR_CODES {
        if c.to_uppercase() == upper {
            return dump_compact(&obj(vec![
                ("ok", Jv::Bool(true)),
                ("kind", s("错误码")),
                ("code", s(c)),
                ("class", s(class)),
                ("title", s(title)),
                ("internal", Jv::Bool(*internal)),
            ]));
        }
    }
    dump_compact(&obj(vec![
        ("ok", Jv::Bool(false)),
        ("error", obj(vec![("message", s(&format!("没有这个错误码：{code}")))])),
        ("可用错误码", arr(D::ERROR_CODES.iter().map(|(c, _, _, _)| s(c)).collect())),
        ("可用检查码", arr(D::CHECK_CODES.iter().map(|(c, _, _, _)| s(c)).collect())),
    ]))
}

/// `lookup_stdlib`。
fn lookup_stdlib(args: &Json) -> String {
    let name = arg_text(args, "module").trim().to_string();
    if name.is_empty() {
        return oops("缺少 module 参数".to_string());
    }
    let found = D::STDLIB.iter().find(|(m, _)| *m == name);
    let Some((_, funcs)) = found else {
        let mut mods: Vec<String> = D::STDLIB.iter().map(|(m, _)| (*m).to_string()).collect();
        mods.sort();
        return dump_compact(&obj(vec![
            ("ok", Jv::Bool(false)),
            ("error", obj(vec![("message", s(&format!("没有这个标准库模块：{name}")))])),
            ("可用模块", arr(mods.iter().map(|m| s(m)).collect())),
            ("提示", s("内建函数（不用 导入 就能用）见 describe_language 返回的语言卡")),
        ]));
    };
    let func = arg_text(args, "function").trim().to_string();
    let fn_jv = |f: &(&str, &str, &str)| obj(vec![
        ("name", s(f.0)), ("sig", s(f.1)), ("doc", s(f.2)),
    ]);
    if func.is_empty() {
        return dump_compact(&obj(vec![
            ("ok", Jv::Bool(true)),
            ("module", s(&name)),
            ("用法", s(&format!("导入 {name}"))),
            ("函数", arr(funcs.iter().map(fn_jv).collect())),
            ("提示", s("再传 function 可查单个函数的详细签名")),
        ]));
    }
    for f in funcs.iter() {
        if f.0 == func {
            return dump_compact(&obj(vec![
                ("ok", Jv::Bool(true)),
                ("module", s(&name)),
                ("用法", s(&format!("{name}.{func}"))),
                ("name", s(f.0)), ("sig", s(f.1)), ("doc", s(f.2)),
            ]));
        }
    }
    dump_compact(&obj(vec![
        ("ok", Jv::Bool(false)),
        ("error", obj(vec![("message", s(&format!("模块「{name}」里没有函数「{func}」")))])),
        ("可用函数", arr(funcs.iter().map(|f| s(f.0)).collect())),
    ]))
}

/// `format_source`。
fn format_source(args: &Json) -> String {
    let src = arg_text(args, "source");
    let indent = arg_f64(args, "indent", 4.0) as usize;
    // ⚠️ 先解析一遍（与 Python 侧同一个理由）：格式化器只按缩进重排文本，
    // 遇到语法错**不报错**，而是照原样返回半成品 —— 对 agent 来说那最坏，
    // 它以为自己拿到了干净代码。
    if let Err(e) = jishi_frontend::parser::parse(&src, "<格式化>") {
        let je = crate::sandbox::parse_err_to_jishi(&e);
        let title = crate::errcodes::title(&e.code).unwrap_or("").to_string();
        let msg = if je.message.is_empty() {
            format!("源码有语法错，请先修好再格式化：{title}")
        } else {
            format!("源码有语法错，请先修好再格式化：{title}（{}）", je.message)
        };
        return dump_compact(&obj(vec![
            ("ok", Jv::Bool(false)),
            ("error", obj(vec![
                ("message", s(&msg)),
                ("code", je.code.clone().map(|c| s(&c)).unwrap_or(Jv::Null)),
                ("line", je.line.map(Jv::Int).unwrap_or(Jv::Null)),
                ("col", je.col.map(Jv::Int).unwrap_or(Jv::Null)),
                ("hint", je.hint.clone().map(|h| s(&h)).unwrap_or(Jv::Null)),
            ])),
        ]));
    }
    match crate::formatter::format_source(&src, "<格式化>", indent) {
        Ok(out) => {
            let normalized = src.replace("\r\n", "\n").replace('\r', "\n");
            dump_compact(&obj(vec![
                ("ok", Jv::Bool(true)),
                ("格式化后", s(&out)),
                ("是否已符合风格", Jv::Bool(out == normalized)),
            ]))
        }
        Err(_) => oops("格式化失败".to_string()),
    }
}

// ---------------------------------------------------------------------------
// JSON-RPC
// ---------------------------------------------------------------------------

fn respond(msg: &Json, result: Jv) -> String {
    let id = match msg.get("id") {
        Some(v) => jv_of(v),
        None => Jv::Null,
    };
    dump_compact(&obj(vec![
        ("jsonrpc", s("2.0")),
        ("id", id),
        ("result", result),
    ]))
}

fn respond_error(msg: &Json, code: i64, message: &str) -> String {
    let id = match msg.get("id") {
        Some(v) => jv_of(v),
        None => Jv::Null,
    };
    dump_compact(&obj(vec![
        ("jsonrpc", s("2.0")),
        ("id", id),
        ("error", obj(vec![
            ("code", Jv::Int(code)),
            ("message", s(message)),
        ])),
    ]))
}

/// 处理一条消息，返回**要写出去的那一行**（空 = 不回）。
pub fn handle(msg: &Json) -> Option<String> {
    let method = msg.get("method").and_then(|m| m.as_str()).unwrap_or("");
    match method {
        "initialize" => Some(respond(msg, obj(vec![
            ("protocolVersion", s(PROTOCOL_VERSION)),
            ("capabilities", obj(vec![("tools", obj(vec![]))])),
            ("serverInfo", obj(vec![
                ("name", s("jishi-mcp")),
                ("version", s(D::LANG_VERSION)),
            ])),
        ]))),
        "notifications/initialized" => None,     // 通知，不回
        "tools/list" => Some(respond(msg, obj(vec![("tools", arr(tools()))]))),
        "tools/call" => {
            let params = msg.get("params");
            let name = params.and_then(|p| p.get("name"))
                .and_then(|n| n.as_str()).unwrap_or("").to_string();
            let empty = crate::json::parse("{}").unwrap_or(Json::Null);
            let args = params.and_then(|p| p.get("arguments")).unwrap_or(&empty);
            let text = dispatch(&name, args);
            Some(respond(msg, obj(vec![("content", arr(vec![obj(vec![
                ("type", s("text")),
                ("text", s(&text)),
            ])]))])))
        }
        "ping" => Some(respond(msg, obj(vec![]))),
        other => Some(respond_error(msg, -32601,
                                    &format!("方法未实现：{other}"))),
    }
}

fn dispatch(name: &str, args: &Json) -> String {
    match name {
        "run_script" => run_script(args),
        "eval_expr" => eval_expr(args),
        "describe_language" => describe_language(),
        "check_source" => check_source(args),
        "lookup_error" => lookup_error(args),
        "lookup_stdlib" => lookup_stdlib(args),
        "format_source" => format_source(args),
        other => oops(format!("未知工具 {other}")),
    }
}

/// 入口：逐行读 stdin（**按字节读 + UTF-8 解码**，理由同 `cli._read_stdin`）。
pub fn run_mcp() -> i32 {
    let mut data: Vec<u8> = Vec::new();
    let _ = std::io::stdin().read_to_end(&mut data);
    let text = String::from_utf8_lossy(&data).to_string();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    for raw in text.split('\n') {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        let msg = match crate::json::parse(line) {
            Ok(m) => m,
            Err(_) => continue,                  // 解析不了就跳过（与 Python 一致）
        };
        if let Some(resp) = handle(&msg) {
            let _ = out.write_all(resp.as_bytes());
            let _ = out.write_all(b"\n");
            let _ = out.flush();
        }
    }
    0
}
