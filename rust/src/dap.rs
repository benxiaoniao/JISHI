//! Debug Adapter Protocol（DAP）适配器（`jishi/dap.py` 的移植）—— R7.5。
//!
//! 编辑器（VS Code 等）通过 DAP 跟「调试适配器」说话。传输与 LSP 完全一样
//! （`Content-Length: N\r\n\r\n{JSON}`），差别在消息种类：DAP 有 request /
//! response / event 三种，而且**事件随时可以发**（不必等谁先问）。
//!
//! 用法：`jishi-rs dap` —— 从标准输入收 DAP 消息，应答写到标准输出。
//!
//! ## 线程模型（Rust 版与 Python 版**不一样，但报文一样**）
//!
//! Python：主线程跑协议循环，**脚本线程**跑用户程序，暂停时两个线程同时活着
//! （读状态靠 GIL + 「暂停期间只读」的口头约定）。
//!
//! Rust 里 `Walker` 是 `Rc<RefCell<…>>`、**不是 `Send`**，跨线程共享状态行不通。
//! 所以这里把线程的职责对调：
//!
//! ```text
//! 读线程     只做分帧：把报文丢进 channel（碰不到任何执行状态）
//! 主线程     协议循环 + 跑用户程序 —— 全在一个线程上
//!               · 程序跑着时：每个语句边界**非阻塞**地捞一遍队列（收 pause / disconnect）
//!               · 暂停时：[`crate::debugger::PauseDriver`] 阻塞式泵消息，顺手把
//!                 stackTrace / variables / evaluate 答掉，直到收到恢复命令
//! ```
//!
//! 于是「暂停期间读状态」天然安全（只有一个线程），而**对客户端而言**报文序列
//! 与 Python 版逐条对应 —— 判据就是同一串报文喂两边比字节。
//!
//! ⚠️ 一处**如实说明**的行为差异（时序，不是内容）：程序**自由跑**的时候收到的
//! 请求（`setBreakpoints` 之类），Python 是当场就答，这里是**下一个语句边界**
//! 才答。对 `pause` / `disconnect` 毫无影响（本来就是「下一个语句边界生效」）；
//! 编辑器的正常流程也不会在自由跑的时候发别的请求。
//!
//! ## 如实说明的边界（第一版，与命令行版口径一致）
//!
//! - **只有树遍历执行器**：`--执行器 vm/cvm` 明确报错。Python 侧三个都有，
//!   那是另一半实现（Rust 的字节码 VM 还没接调试钩子）。
//! - **变量不展开**：列表/字典显示成一行文本（`[1, 2, 3]`），与「变量」命令
//!   同一口径 —— 不做「编辑器里能展开、命令行里不能」的分裂。
//! - **没有条件断点 / 异常断点 / 函数断点**：命令行版也没有，这里不假装有。
//! - **只有一条线程**：树遍历是单线程跑的，`threads` 恒为一条。

use std::cell::RefCell;
use std::io::{BufRead, Read, Write};
use std::rc::Rc;
use std::sync::mpsc::{channel, Receiver, Sender, TryRecvError};

use crate::debugger::{self, Debugger, PauseDriver, SessionState, DEAD_LINE_HINT};
use crate::json::Json;
use crate::jsonw::{dump_compact, Jv};
use crate::walk::Walker;
use crate::{BuiltinHost, JishiError};

/// 本适配器只有一条线程（树遍历是单线程跑的）
const THREAD_ID: i64 = 1;
const THREAD_NAME: &str = "主线程";

/// 变量引用的编码：第 i 帧的作用域引用 = `_SCOPE_BASE + i`。
/// 0 按 DAP 规范表示「没有子项」，所以自定义引用从一个够大的数起。
const SCOPE_BASE: i64 = 1000;

// ---------------------------------------------------------------------------
// JSON 小工具（与 `lsp.rs` 同一套写法）
// ---------------------------------------------------------------------------

fn obj(kv: Vec<(&str, Jv)>) -> Jv {
    Jv::Obj(kv.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn s(x: &str) -> Jv {
    Jv::Str(x.to_string())
}

// ---------------------------------------------------------------------------
// 分帧
// ---------------------------------------------------------------------------

/// 读线程 → 主线程的一条消息。
enum Msg {
    Json(Json),
    /// 对端关了（EOF）—— 主线程据此收工
    Eof,
}

/// 从标准输入读一帧；对端关闭（EOF）或帧坏了都返回 `None`。
fn read_frame(inp: &mut impl BufRead) -> Option<Json> {
    let mut len: usize = 0;
    let mut got_header = false;
    loop {
        let mut line = String::new();
        match inp.read_line(&mut line) {
            Ok(0) | Err(_) => return None,
            Ok(_) => {}
        }
        let line = line.trim();
        if line.is_empty() {
            break;                       // 空行 = 头结束
        }
        got_header = true;
        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                len = value.trim().parse().unwrap_or(0);
            }
        }
    }
    if !got_header || len == 0 {
        return None;
    }
    let mut body = vec![0u8; len];
    if inp.read_exact(&mut body).is_err() {
        return None;
    }
    let text = String::from_utf8_lossy(&body).to_string();
    crate::json::parse(&text).ok()
}

/// 起一个只做分帧的读线程。
fn spawn_reader(tx: Sender<Msg>) {
    std::thread::spawn(move || {
        let stdin = std::io::stdin();
        let mut inp = stdin.lock();
        loop {
            match read_frame(&mut inp) {
                None => {
                    let _ = tx.send(Msg::Eof);
                    break;
                }
                Some(v) => {
                    if tx.send(Msg::Json(v)).is_err() {
                        break;
                    }
                }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// 协议状态与要跑的东西
// ---------------------------------------------------------------------------

/// 协议侧的可变状态（**只有主线程碰**）。
struct Proto {
    out: Box<dyn Write>,
    seq: i64,
    /// 脚本线程是否停在断点处（读请求据此判断「状态能不能读」）
    paused: bool,
    filename: String,
    lines: Vec<String>,
    /// 有些客户端会在 launch 之前发 setBreakpoints，先存这儿
    pending_breakpoints: Vec<i64>,
    exited: bool,
    /// 收到 disconnect / EOF：协议循环该收工了
    stopping: bool,
}

/// 要跑的东西：树遍历执行器 + AST + 调试器。
///
/// 📌 放在 `Option` 里由协议循环 `take` 走 —— 跑完就没了（一次会话只跑一个程序）。
struct RunPlan {
    walker: Walker,
    ast: Json,
    dbg: Debugger,
}

/// 读线程与协议循环共用的一小块。
///
/// ⚠️ 这里**不持 `RunPlan` 的所有权**（那会成环：plan → Debugger → driver →
/// 本结构）。协议循环会把 plan `take()` 走，环当场断掉。
///
/// `rx` 也放在这里：程序跑起来之后，等消息的活儿从协议循环交给了
/// 暂停驱动器（`next_command`），但两者在同一线程上（不会并发），
/// 所以一个 `RefCell<Receiver>` 就够。
struct Shared {
    proto: RefCell<Proto>,
    plan: RefCell<Option<RunPlan>>,
    rx: RefCell<Receiver<Msg>>,
    /// launch 之后、configurationDone 之前的请求要够得着会话（典型是
    /// **第二个 `setBreakpoints`**：那时的语义是「替换当前文件的断点」，
    /// 不是「还没 launch，先记下来」）。会话**不在 plan 里**，就是为了这里。
    ///
    /// ⚠️ 这会形成一个 `Rc` 环（会话的 `write_line` / `on_pause` 闭包持有
    /// `Rc<Shared>`）：进程退出前不释放。一个调试适配器进程只跑一次会话，
    /// 不值得为它上弱引用 —— **如实记在这里**。
    session: RefCell<Option<Rc<RefCell<SessionState>>>>,
}

// -- 协议输出 ---------------------------------------------------------------

fn send(shared: &Rc<Shared>, v: &Jv) {
    let body = dump_compact(v).into_bytes();
    let mut p = shared.proto.borrow_mut();
    let out = &mut *p.out;
    if out.write_all(b"Content-Length: ").is_err() {
        return;
    }
    let _ = write!(out, "{}\r\n\r\n", body.len());
    let _ = out.write_all(&body);
    let _ = out.flush();
}

fn next_seq(shared: &Rc<Shared>) -> i64 {
    let mut p = shared.proto.borrow_mut();
    p.seq += 1;
    p.seq
}

fn respond(shared: &Rc<Shared>, req: &Json, body: Jv) {
    let seq = next_seq(shared);
    send(shared, &obj(vec![
        ("seq", Jv::Int(seq)),
        ("type", s("response")),
        ("request_seq", req.get("seq").cloned().map(json_to_jv).unwrap_or(Jv::Null)),
        ("success", Jv::Bool(true)),
        ("command", req.get("command").and_then(|c| c.as_str())
            .map(s).unwrap_or(Jv::Null)),
        ("body", body),
    ]));
}

fn respond_error(shared: &Rc<Shared>, req: &Json, message: &str) {
    let seq = next_seq(shared);
    send(shared, &obj(vec![
        ("seq", Jv::Int(seq)),
        ("type", s("response")),
        ("request_seq", req.get("seq").cloned().map(json_to_jv).unwrap_or(Jv::Null)),
        ("success", Jv::Bool(false)),
        ("command", req.get("command").and_then(|c| c.as_str())
            .map(s).unwrap_or(Jv::Null)),
        ("message", s(message)),
    ]));
}

fn event(shared: &Rc<Shared>, name: &str, body: Jv) {
    let seq = next_seq(shared);
    send(shared, &obj(vec![
        ("seq", Jv::Int(seq)),
        ("type", s("event")),
        ("event", s(name)),
        ("body", body),
    ]));
}

/// 把一段文本送到编辑器的「调试控制台」。
fn output(shared: &Rc<Shared>, text: &str, category: &str) {
    event(shared, "output", obj(vec![
        ("category", s(category)),
        ("output", s(text)),
    ]));
}

fn json_to_jv(v: Json) -> Jv {
    match v {
        Json::Null => Jv::Null,
        Json::Bool(b) => Jv::Bool(b),
        Json::Int(i) => Jv::Int128(i),
        Json::Float(f) => Jv::Float(f),
        Json::Str(t) => Jv::Str(t),
        Json::Arr(items) => Jv::Arr(items.into_iter().map(json_to_jv).collect()),
        Json::Obj(m) => Jv::Obj(m.into_iter().map(|(k, v)| (k, json_to_jv(v))).collect()),
    }
}

// ---------------------------------------------------------------------------
// 请求处理
// ---------------------------------------------------------------------------

/// 请求本身不对（参数缺了/不对）。回一条 error 就行，别把适配器掀翻。
struct BadRequest(String);

/// 会话里的中文暂停原因 → DAP 的 reason（取值是固定的那几个英文词）。
fn reason_for_dap(reason: &str) -> &'static str {
    if reason.contains("入口") {
        "entry"
    } else if reason.contains("断点") {
        "breakpoint"
    } else if reason == "暂停" {
        "pause"
    } else {
        "step"                       // 单步 / 下一步 / 跳出
    }
}

/// 把基石错误渲染成一条给编辑器看的说明（带修法建议）。
///
/// `with_line=false` 给「就地求值」用：那时的行号是**表达式自己**的第 1 行
/// （表达式从 1 行数起），说给用户听只会误导 —— 他已经知道自己求的是什么。
fn explain(err: &JishiError, with_line: bool) -> String {
    // ⚠️ 标题要走**错误码表**（`errors.py` 那张，`errcodes.rs` 搬过来的），
    // **不是** `type_name`：两者在 Python 侧是分开的
    // （`RunZeroDivisionError` 的类名是「除零错误」、`title` 是「不能除以零」）。
    // 用 `type_name` 会得到「第 3 行：除零错误」而不是 Python 的「不能除以零」
    // —— R7.5 的 DAP 对拍照出来的（同族的坑在 R7.1-c 的 `测试` 模块也踩过一次）。
    let mut text = err.code.as_deref()
        .and_then(crate::errcodes::title)
        .map(str::to_string)
        .unwrap_or_else(|| err.type_name.clone());
    if !err.message.is_empty() {
        text.push_str(&format!("：{}", err.message));
    }
    if with_line && err.line.is_some() {
        text = format!("第 {} 行：{text}", err.line.unwrap());
    }
    if let Some(h) = &err.hint {
        text.push_str(&format!("（{h}）"));
    }
    text
}

fn arg_str(args: &Json, keys: &[&str]) -> Option<String> {
    for k in keys {
        if let Some(Json::Str(v)) = args.get(k) {
            if !v.is_empty() {
                return Some(v.clone());
            }
        }
    }
    None
}

/// 读源码（出错抛 `BadRequest`，不是退出进程）。
///
/// 与 `cli._read_source` 同一口径：UTF-8、换行归一。
fn read_program(path: &str) -> Result<(String, Vec<String>), BadRequest> {
    let p = std::path::Path::new(path);
    if p.is_dir() {
        return Err(BadRequest(format!("「{path}」是个目录 —— 调试要指向一个 .jsh 文件")));
    }
    if !p.is_file() {
        return Err(BadRequest(format!("找不到文件「{path}」")));
    }
    let raw = std::fs::read(p)
        .map_err(|e| BadRequest(format!("读不了文件「{path}」：{e}")))?;
    let text = String::from_utf8(raw)
        .map_err(|_| BadRequest(format!("文件「{path}」不是 UTF-8 编码，读不了")))?;
    let source = text.replace("\r\n", "\n").replace('\r', "\n");
    let lines = source.split('\n').map(str::to_string).collect();
    Ok((source, lines))
}

/// 处理一个请求。返回 `Some(命令)` = 这是一条「继续类」命令，脚本线程该醒了。
///
/// `sess` / `walker` 只在**程序跑起来之后**才有（launch 之前发读请求是客户端的
/// 自由，我们照 DAP 规范回空）。
fn handle_request(
    shared: &Rc<Shared>,
    req: &Json,
    sess: Option<&mut SessionState>,
    walker: Option<&mut Walker>,
    pending: &mut Option<String>,
) -> Option<String> {
    let command = req.get("command").and_then(|c| c.as_str()).unwrap_or("");
    match command {
        "initialize" => {
            // 能力声明必须**嵌在 `body.capabilities` 里**（DAP 规范）。
            // 平铺在 `body` 顶层的话，编辑器读 `body.capabilities` 得到
            // undefined，于是所有开关都退回默认值 —— 表现是「单步按钮灰着」
            // 「悬停不求值」这类莫名其妙的现象，而适配器这边一切正常。
            let caps = obj(vec![
                ("supportsConfigurationDoneRequest", Jv::Bool(true)),
                ("supportsEvaluateForHovers", Jv::Bool(true)),
                ("supportsPauseRequest", Jv::Bool(true)),
                ("supportsTerminateRequest", Jv::Bool(true)),
                ("supportTerminateDebuggee", Jv::Bool(true)),
                // 下面这些**明说不支持**，别让编辑器以为点了有用
                ("supportsFunctionBreakpoints", Jv::Bool(false)),
                ("supportsConditionalBreakpoints", Jv::Bool(false)),
                ("supportsHitConditionalBreakpoints", Jv::Bool(false)),
                ("supportsLogPoints", Jv::Bool(false)),
                ("supportsSetVariable", Jv::Bool(false)),
                ("supportsStepBack", Jv::Bool(false)),
                ("supportsRestartRequest", Jv::Bool(false)),
                ("exceptionBreakpointFilters", Jv::Arr(Vec::new())),
            ]);
            respond(shared, req, obj(vec![("capabilities", caps)]));
            // 规范：应答之后紧接着发 `initialized`，客户端才知道可以配置断点了
            event(shared, "initialized", Jv::Obj(Vec::new()));
        }
        "attach" => {
            respond_error(shared, req,
                "基石调试器只支持 launch（直接跑一个 .jsh 脚本），不支持 attach\
——程序要由本适配器自己启动，才能挂上断点");
        }
        "launch" => {
            let args = req.get("arguments").cloned().unwrap_or(Json::Null);
            let Some(program) = arg_str(&args, &["program", "脚本", "文件"]) else {
                respond_error(shared, req,
                    "启动配置里少了 program（要调试的 .jsh 文件路径）。\
launch.json 里写成：{\"type\": \"jishi\", \"request\": \"launch\", \
\"name\": \"调试基石脚本\", \"program\": \"${file}\"}");
                return None;
            };
            let engine = arg_str(&args, &["执行器", "engine"]).unwrap_or_else(|| "树遍历".into());
            if engine != "树遍历" {
                respond_error(shared, req, &format!(
                    "Rust 版的调试适配器只支持「树遍历」执行器（给的是「{engine}」）\
—— 字节码执行器的调试钩子还没搬过来；要用它请用 Python 版 `jishi dap --执行器 {engine}`"));
                return None;
            }
            let (source, lines) = match read_program(&program) {
                Ok(v) => v,
                Err(e) => {
                    respond_error(shared, req, &e.0);
                    return None;
                }
            };
            let stop_on_entry = args.get("stopOnEntry").and_then(|v| v.as_bool())
                .or_else(|| args.get("停在入口").and_then(|v| v.as_bool()))
                .unwrap_or(false);
            let ast = match jishi_frontend::parser::parse(&source, &program) {
                Ok(n) => crate::ast_node_to_json(&n),
                Err(e) => {
                    let err = crate::sandbox::parse_err_to_jishi(&e);
                    respond_error(shared, req, &explain(&err, true));
                    return None;
                }
            };
            // ---- 装配会话：作用域要取执行器那一份（模块顶层就是它）----
            let mut walker = Walker::new();
            // 程序自己的 `打印`（走 `write_output`）在 DAP 里是 `output/stdout`
            // 事件 —— **每写一次报一次**，粒度与 Python 的 `_StreamOut.write`
            // 一致（空串不报，那边 `if text:` 也是这个口径）。
            {
                let w0 = Rc::clone(shared);
                walker.set_output_sink(Some(Box::new(move |text: &str, is_err: bool| {
                    if !text.is_empty() {
                        output(&w0, text, if is_err { "stderr" } else { "stdout" });
                    }
                })));
            }
            let globals = walker.globals();
            let stmt_lines = debugger::statement_lines(&ast);
            let mut proto = shared.proto.borrow_mut();
            proto.filename = program.clone();
            proto.lines = lines.clone();
            let bp = std::mem::take(&mut proto.pending_breakpoints);
            drop(proto);

            let wl = Rc::clone(shared);
            let mut sess = SessionState::new(
                program.clone(), lines, stmt_lines, globals, bp, stop_on_entry,
                false,      // 编辑器不是终端：别打「输入「帮助」看命令」这类寒暄
                Box::new(move |text: &str| output(&wl, &format!("{text}\n"), "console")),
            );
            let wl = Rc::clone(shared);
            sess.on_pause = Some(Box::new(move |reason: &str| {
                wl.proto.borrow_mut().paused = true;
                event(&wl, "stopped", obj(vec![
                    ("reason", s(reason_for_dap(reason))),
                    ("threadId", Jv::Int(THREAD_ID)),
                    ("allThreadsStopped", Jv::Bool(true)),
                    ("description", s(reason)),     // 中文原话，编辑器显示成暂停原因
                ]));
            }));
            let wr = Rc::clone(shared);
            let session = Rc::new(RefCell::new(sess));
            let drv = DapDriver { shared: wr, pending_quit: false };
            let dbg = Debugger::new(Rc::clone(&session), Box::new(drv));
            shared.session.replace(Some(Rc::clone(&session)));
            *shared.plan.borrow_mut() = Some(RunPlan { walker, ast, dbg });
            respond(shared, req, Jv::Obj(Vec::new()));
        }
        "setBreakpoints" => {
            let args = req.get("arguments").cloned().unwrap_or(Json::Null);
            let mut lines: Vec<i64> = Vec::new();
            if let Some(Json::Arr(items)) = args.get("breakpoints") {
                for b in items {
                    let n = b.get("line").and_then(|v| v.as_i64()).unwrap_or(0);
                    if n > 0 {
                        lines.push(n);
                    }
                }
            }
            match sess {
                None => {
                    // 还没 launch：先记下来，装配会话时一起加
                    shared.proto.borrow_mut().pending_breakpoints = lines.clone();
                    let items: Vec<Jv> = lines.iter()
                        .map(|n| obj(vec![("verified", Jv::Bool(true)), ("line", Jv::Int(*n))]))
                        .collect();
                    respond(shared, req, obj(vec![("breakpoints", Jv::Arr(items))]));
                }
                Some(sess) => {
                    // DAP 的 setBreakpoints 是**替换**语义（这次给的集合就是全部），
                    // 而会话层的 add_breakpoints 是追加 —— 所以先清空。
                    sess.clear_breakpoints();
                    let (usable, _dead) = sess.add_breakpoints(&lines);
                    let items: Vec<Jv> = lines.iter().map(|n| {
                        if usable.contains(n) {
                            obj(vec![("verified", Jv::Bool(true)), ("line", Jv::Int(*n))])
                        } else {
                            // 「停不住」不是错误，但必须说出来：否则用户看着断点
                            // 在那儿、程序却从不停，只会怀疑调试器坏了
                            obj(vec![
                                ("verified", Jv::Bool(false)),
                                ("line", Jv::Int(*n)),
                                ("message", s(&format!(
                                    "第 {n} 行没有可停的语句（{DEAD_LINE_HINT}）"))),
                            ])
                        }
                    }).collect();
                    respond(shared, req, obj(vec![("breakpoints", Jv::Arr(items))]));
                }
            }
        }
        "setFunctionBreakpoints" | "setExceptionBreakpoints" => {
            // 函数断点 / 异常断点：如实回空表（能力声明里已说不支持）
            respond(shared, req, obj(vec![("breakpoints", Jv::Arr(Vec::new()))]));
        }
        "configurationDone" => {
            // 先把「没 launch」拦下来 —— **一个请求只能有一个响应**。
            if shared.plan.borrow().is_none() {
                respond_error(shared, req, "还没 launch：先说清楚要跑哪个脚本");
                return None;
            }
            respond(shared, req, Jv::Obj(Vec::new()));
            // 跑程序：**就在这个线程上**（暂停时驱动会泵消息，见模块头）
            run_program(shared);
        }
        "threads" => {
            respond(shared, req, obj(vec![("threads", Jv::Arr(vec![
                obj(vec![("id", Jv::Int(THREAD_ID)), ("name", s(THREAD_NAME))]),
            ]))]));
        }
        "stackTrace" => {
            let paused = shared.proto.borrow().paused;
            let frames: Vec<Jv> = match (sess, paused) {
                (Some(sess), true) => sess.live_chain().iter().enumerate().map(|(i, (line, label))| {
                    obj(vec![
                        // frameId 只在一次暂停内有意义，从 1 起
                        ("id", Jv::Int(i as i64 + 1)),
                        ("name", s(label)),
                        ("line", Jv::Int(*line)),   // DAP 的 line 是 1 基
                        ("column", Jv::Int(1)),
                        ("source", source_ref(shared)),
                    ])
                }).collect(),
                _ => Vec::new(),
            };
            let n = frames.len();
            respond(shared, req, obj(vec![
                ("stackFrames", Jv::Arr(frames)),
                ("totalFrames", Jv::Int(n as i64)),
            ]));
        }
        "scopes" => {
            let index = match frame_index(req) {
                Ok(i) => i,
                Err(msg) => {
                    respond_error(shared, req, &msg);
                    return None;
                }
            };
            respond(shared, req, obj(vec![("scopes", Jv::Arr(vec![
                obj(vec![
                    ("name", s("变量")),
                    ("variablesReference", Jv::Int(SCOPE_BASE + index as i64)),
                    ("expensive", Jv::Bool(false)),
                ]),
            ]))]));
        }
        "variables" => {
            let args = req.get("arguments").cloned().unwrap_or(Json::Null);
            let r = args.get("variablesReference").and_then(|v| v.as_i64()).unwrap_or(0);
            let items = match (sess, walker, r >= SCOPE_BASE) {
                (Some(sess), Some(walker), true) =>
                    frame_variables(shared, sess, walker, (r - SCOPE_BASE) as usize),
                _ => Vec::new(),
            };
            respond(shared, req, obj(vec![("variables", Jv::Arr(items))]));
        }
        "evaluate" => {
            let args = req.get("arguments").cloned().unwrap_or(Json::Null);
            let expr = arg_str(&args, &["expression"]).unwrap_or_default().trim().to_string();
            if expr.is_empty() {
                respond_error(shared, req, "求值的表达式是空的");
                return None;
            }
            let paused = shared.proto.borrow().paused;
            let (Some(sess), Some(walker)) = (sess, walker) else {
                respond_error(shared, req, "程序没停在断点上，现在没法求值");
                return None;
            };
            if !paused {
                respond_error(shared, req, "程序没停在断点上，现在没法求值");
                return None;
            }
            let node = match debugger::parse_expr(&expr) {
                Ok(n) => n,
                Err(e) => {
                    // 「查看」里的表达式写错了：如实说，别把会话掀翻
                    respond_error(shared, req, &explain(&e, false));
                    return None;
                }
            };
            let Some(scope) = sess.scope_at(0) else {
                respond_error(shared, req, "现在没有可求值的帧（程序还没执行到任何语句）");
                return None;
            };
            match walker.eval_in_scope(&node, &scope) {
                Ok(v) => {
                    let repr = walker.format_value(&v, true)
                        .unwrap_or_else(|_| crate::display(&v));
                    let ty = crate::type_label(&v);
                    respond(shared, req, obj(vec![
                        ("result", s(&repr)),
                        ("type", s(&ty)),
                        ("variablesReference", Jv::Int(0)),
                    ]));
                }
                Err(e) => respond_error(shared, req, &explain(&e, false)),
            }
        }
        "source" => {
            let args = req.get("arguments").cloned().unwrap_or(Json::Null);
            let want = args.get("source").cloned().unwrap_or(Json::Null);
            let path = want.get("path").and_then(|p| p.as_str()).map(String::from);
            let filename = shared.proto.borrow().filename.clone();
            let bad = match &path {
                Some(p) => !p.is_empty() && !same_path(p, &filename),
                None => false,
            };
            if bad || filename.is_empty() {
                respond_error(shared, req, &format!("没有这个源文件：{}",
                    path.unwrap_or_default()));
                return None;
            }
            let content = shared.proto.borrow().lines.join("\n");
            respond(shared, req, obj(vec![("content", s(&content))]));
        }
        "continue" => return resume(shared, req, sess, "继续"),
        "next" => return resume(shared, req, sess, "下一步"),
        "stepIn" => return resume(shared, req, sess, "单步"),
        "stepOut" => return resume(shared, req, sess, "跳出"),
        "pause" => {
            if let Some(sess) = sess {
                if !shared.proto.borrow().exited {
                    // 下一条语句边界就会停下
                    sess.pause_requested = true;
                }
            }
            respond(shared, req, Jv::Obj(Vec::new()));
        }
        "disconnect" | "terminate" => {
            respond(shared, req, Jv::Obj(Vec::new()));
            return shutdown(shared, sess);
        }
        _ => {
            // 不认识的请求：空应答。回 error 会让有些客户端直接报「调试器坏了」，
            // 而 DAP 允许客户端试探性地发一些可选请求。
            respond(shared, req, Jv::Obj(Vec::new()));
        }
    }
    let _ = pending;
    None
}

fn same_path(a: &str, b: &str) -> bool {
    let norm = |p: &str| std::fs::canonicalize(p).ok()
        .map(|c| c.to_string_lossy().to_string())
        .unwrap_or_else(|| std::path::Path::new(p).to_string_lossy().to_string());
    norm(a) == norm(b)
}

fn source_ref(shared: &Rc<Shared>) -> Jv {
    let path = {
        let p = shared.proto.borrow();
        if p.filename.is_empty() { "<未知>".to_string() } else { p.filename.clone() }
    };
    let base = std::path::Path::new(&path)
        .file_name().map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| path.clone());
    obj(vec![
        ("name", s(if base.is_empty() { &path } else { &base })),
        ("path", s(&abs_path(&path))),
    ])
}

fn abs_path(p: &str) -> String {
    let pb = std::path::Path::new(p);
    if pb.is_absolute() {
        return p.to_string();
    }
    std::env::current_dir()
        .map(|c| c.join(pb).to_string_lossy().to_string())
        .unwrap_or_else(|_| p.to_string())
}

fn frame_index(req: &Json) -> Result<usize, String> {
    let args = req.get("arguments").cloned().unwrap_or(Json::Null);
    let frame_id = args.get("frameId").and_then(|v| v.as_i64()).unwrap_or(0);
    let index = frame_id - 1;
    if index < 0 || index >= 256 {
        return Err(format!("frameId 不对：{frame_id}"));
    }
    Ok(index as usize)
}

/// 某一帧的变量表（**不展开** —— 与命令行版同一口径）。
fn frame_variables(shared: &Rc<Shared>, sess: &mut SessionState,
                   walker: &mut Walker, index: usize) -> Vec<Jv> {
    let Some(scope) = sess.scope_at(index) else { return Vec::new() };
    let raw = sess.vars_of(&scope);
    let mut out = Vec::new();
    for (name, value) in raw {
        if sess.is_noise(&name) {       // 内建与脱糖临时变量默认不显示
            continue;
        }
        let repr = walker.format_value(&value, true)
            .unwrap_or_else(|_| crate::display(&value));
        let ty = crate::type_label(&value);
        out.push(obj(vec![
            ("name", s(&name)),
            ("value", s(&repr)),
            ("type", s(&ty)),
            ("variablesReference", Jv::Int(0)),     // 不展开
        ]));
    }
    let _ = shared;
    out
}

/// 把一个「继续类」命令交给脚本线程。
///
/// 注意只在**确实停着**的时候塞命令：用户连点两下「继续」时，第二下什么都不该做
/// —— 否则那命令会在下一次暂停时被立刻吃掉，看起来像「断点失灵」。
fn resume(shared: &Rc<Shared>, req: &Json, sess: Option<&mut SessionState>,
          command: &str) -> Option<String> {
    let paused = shared.proto.borrow().paused;
    let can_resume = paused && sess.is_some() && !shared.proto.borrow().exited;
    if can_resume {
        shared.proto.borrow_mut().paused = false;
    }
    respond(shared, req, obj(vec![("allThreadsContinued", Jv::Bool(true))]));
    if !can_resume {
        return None;
    }
    event(shared, "continued", obj(vec![
        ("threadId", Jv::Int(THREAD_ID)),
        ("allThreadsContinued", Jv::Bool(true)),
    ]));
    Some(command.to_string())
}

/// disconnect / terminate：让脚本走「退出」这条路（`最终` 块照跑），然后收工。
fn shutdown(shared: &Rc<Shared>, sess: Option<&mut SessionState>) -> Option<String> {
    let paused = shared.proto.borrow().paused;
    if paused {
        Some("退出".to_string())
    } else {
        // 程序正在跑：请求它在下一个语句边界停下（那时就会读到「退出」）
        if let Some(sess) = sess {
            if !shared.proto.borrow().exited {
                sess.pause_requested = true;
            }
        }
        Some("退出".to_string())
    }
}

// ---------------------------------------------------------------------------
// 跑程序
// ---------------------------------------------------------------------------

/// 跑程序 —— 由 `configurationDone` 调用，**在当前线程**。
fn run_program(shared: &Rc<Shared>) {
    let Some(mut plan) = shared.plan.borrow_mut().take() else { return };
    let err = debugger::run(&mut plan.walker, &plan.ast, plan.dbg);
    shared.proto.borrow_mut().paused = false;
    let code = if let Some(e) = err {
        output(shared, &explain(&e, true), "stderr");
        1
    } else {
        0
    };
    shared.proto.borrow_mut().exited = true;
    event(shared, "exited", obj(vec![("exitCode", Jv::Int(code))]));
    event(shared, "terminated", Jv::Obj(Vec::new()));
}

// ---------------------------------------------------------------------------
// 暂停驱动器：把「等命令」与「答读请求」合成一件事
// ---------------------------------------------------------------------------

/// DAP 版的 [`PauseDriver`]。
///
/// ⚠️ 它是 Rust 版与 Python 版**结构上差别最大**的一处：Python 用「脚本线程暂停 +
/// 主线程读 session」把这两件事并行做掉；Rust 只能**在暂停点串行地泵消息**
/// —— 对客户端而言报文序列一样（这正是判据）。
pub struct DapDriver {
    shared: Rc<Shared>,
    /// EOF / disconnect 之后，下一次暂停要直接读到的命令
    pending_quit: bool,
}

impl PauseDriver for DapDriver {
    fn poll(&mut self, sess: &mut SessionState, walker: &mut Walker) {
        loop {
            let msg = self.shared.rx.borrow_mut().try_recv();
            let v = match msg {
                Ok(Msg::Json(v)) => v,
                // 客户端把管道关了（或读线程没了）：请求在下一个语句边界停下并退出
                Ok(Msg::Eof) | Err(TryRecvError::Disconnected) => {
                    self.shared.proto.borrow_mut().stopping = true;
                    sess.pause_requested = true;
                    self.pending_quit = true;
                    return;
                }
                Err(TryRecvError::Empty) => return,
            };
            // ⚠️ 跑着的时候来的「恢复命令」（客户端不该这么发）在这里被丢掉 ——
            // 与 Python 的 `_resume` 只看 `self.paused` 一致。`pause` / `disconnect`
            // 则会照常生效（`pause` 把 `pause_requested` 立起来，就是上面那一句）。
            let mut pending = None;
            handle_request(&self.shared, &v, Some(sess), Some(walker), &mut pending);
        }
    }

    fn next_command(&mut self, sess: &mut SessionState, walker: &mut Walker) -> String {
        if self.pending_quit {
            return "退出".to_string();
        }
        loop {
            let msg = self.shared.rx.borrow_mut().recv();
            match msg {
                Ok(Msg::Json(v)) => {
                    let mut pending = None;
                    if let Some(c) = handle_request(&self.shared, &v, Some(sess),
                                                    Some(walker), &mut pending) {
                        return c;
                    }
                }
                Ok(Msg::Eof) | Err(_) => {
                    self.shared.proto.borrow_mut().stopping = true;
                    return "退出".to_string();
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

/// `jishi-rs dap [--执行器 树遍历]`。
pub fn run_dap(engine: &str) -> i32 {
    if engine != "树遍历" {
        eprintln!("Rust 版的调试适配器只支持「树遍历」执行器（给的是「{engine}」）");
        eprintln!("—— 字节码执行器的调试钩子还没搬过来；要用它请用 Python 版 \
`jishi dap --执行器 {engine}`");
        return 2;
    }
    let (tx, rx) = channel();
    spawn_reader(tx);
    let shared = Rc::new(Shared {
        rx: RefCell::new(rx),
        session: RefCell::new(None),
        proto: RefCell::new(Proto {
            out: Box::new(std::io::stdout()),
            seq: 0,
            paused: false,
            filename: String::new(),
            lines: Vec::new(),
            pending_breakpoints: Vec::new(),
            exited: false,
            stopping: false,
        }),
        plan: RefCell::new(None),
    });
    // 协议循环：等一条消息 → 处理。**跑程序也在这个线程上**（见模块头）：
    // `configurationDone` 会就地跑完整个程序（暂停时由驱动器泵消息）。
    loop {
        if shared.proto.borrow().stopping {
            break;
        }
        let msg = shared.rx.borrow_mut().recv();
        match msg {
            Ok(Msg::Json(v)) => {
                let mut pending = None;
                // ⚠️ `configurationDone` 会**就地跑程序**，而跑的过程里语句钩子要借
                // 会话（`RefCell` 会报「already borrowed」）—— 所以这一条**不能**
                // 带着会话借用进去。
                let is_cfg = v.get("command").and_then(|c| c.as_str())
                    == Some("configurationDone");
                let sess = if is_cfg { None } else { shared.session.borrow().clone() };
                match sess {
                    // 有会话就把状态一起给过去（跑起来之后这条路由暂停驱动器接管）
                    Some(c) => {
                        let mut g = c.borrow_mut();
                        handle_request(&shared, &v, Some(&mut g), None, &mut pending);
                    }
                    None => {
                        handle_request(&shared, &v, None, None, &mut pending);
                    }
                }
            }
            Ok(Msg::Eof) | Err(_) => break,
        }
    }
    0
}
