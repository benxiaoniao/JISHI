//! 沙箱（`jishi/sandbox.py` 的移植；R7.4）。
//!
//! 面向「**被大模型调用**」的场景 —— LLM 生成的代码可能死循环、刷屏、写文件、
//! 连网络，沙箱把这些都挡在安全边界内：
//!
//! * **导入守卫**：标准库默认只放行无副作用的 随机/数学/文本/时间/json/日期/正则/加密；
//!   文件/表格 需 `allow_write`；网络/系统 需 `allow_network`；Python 桥接默认关。
//! * **stdout 上限**（默认 10000 字符）：超了报错（`Lib.rs` 的 VM 侧实现）。
//! * **超时**（默认 5 秒）：超时返回错误，**不再要结果**。
//! * **统一协议 JSON**（`protocol.py`）：成功 / 失败都是结构化 JSON，
//!   供 agent 程序化读取 —— `--json-result` 与 MCP 共用。
//!
//! ⚠️ **超时怎么「真 kill」**：这里用「另起一条线程跑 VM + `recv_timeout`」，
//! 超时后**丢掉那条线程**（拿到不结果了）。对 CLI 来说无所谓 —— 主线程一返回
//! 进程就退出，线程跟着被系统收掉；真正「能 kill 的是子进程」那条路在 MCP
//! （`mcp.rs` 的 `run_script` 起子进程 + 到点 `kill()`），与 Python 侧同构。

use std::sync::mpsc;
use std::time::{Duration, Instant};

use crate::jsonw::{dump_compact, Jv};
use crate::{err_coded, err_coded_hint, JishiError, Val};

/// 沙箱自己报的错（`RunError`，Python 侧码是 `E2000`）。
fn run_error(msg: String, hint: Option<String>) -> JishiError {
    match hint {
        Some(h) => err_coded_hint("运行期错误", "E2000", msg, h),
        None => err_coded("运行期错误", "E2000", msg),
    }
}

/// 沙箱选项 —— 默认值与 Python `SandboxOpts` **逐条对齐**。
#[derive(Clone)]
pub struct SandboxOpts {
    /// 执行超时（秒）。
    pub timeout: f64,
    /// stdout 上限（字符）。
    pub max_stdout: usize,
    /// 允许文件写（导入 文件 / 表格 / 路径 / 压缩）。
    pub allow_write: bool,
    /// 允许网络（导入 网络 / 系统）。Python 桥接的网络模块也看这个开关。
    pub allow_network: bool,
    /// 允许 Python 桥接导入（宿主不支持，见 `导入` 那条登记）。
    pub allow_python_import: bool,
    /// **执行步数上限**（R8.1；`None` = 不限）。
    ///
    /// ⚠️ 这是给 **wasm** 那条路用的：wasm 上没有线程，`timeout` 那套（另起线程 +
    /// `recv_timeout`）用不了。默认 `None` ⇒ 与 Python 侧**逐条对齐**、不改行为。
    ///
    /// ⚠️ **只有开了 `budget` 特征才真的生效**（`jishi-wasm` 会开）：关着时
    /// `crate::set_step_limit` 是个空实现，这个字段会被**忽略**——原生宿主不需要它
    /// （那边有线程超时），而打开它会让指令循环的回跳处多一次判断（实测 +6%）。
    pub max_steps: Option<u64>,
}

impl Default for SandboxOpts {
    fn default() -> Self {
        SandboxOpts {
            timeout: 5.0,
            max_stdout: 10_000,
            allow_write: false,
            allow_network: false,
            allow_python_import: false,
            max_steps: None,
        }
    }
}

/// 标准库白名单（**无副作用**的那些恒允许）—— 与 Python 的默认
/// `allow_modules` 同一张表。改一处要改两处。
const ALLOW_MODULES: &[&str] = &[
    "随机", "数学", "文本", "时间", "json", "日期", "正则", "加密",
];

/// Python 桥接里被拦的「网络 / 危险」模块（沙箱默认拦截）。
const BLOCKED_PYTHON_MODULES: &[&str] = &[
    "socket", "urllib", "urllib2", "urllib3", "http", "requests", "httplib",
    "httplib2", "ftplib", "smtplib", "telnetlib", "subprocess", "os", "sys",
    "shutil", "ctypes", "pathlib", "email", "imaplib", "poplib",
];

/// 造导入守卫（`sandbox._make_guard` 的等价物）。
///
/// 文案**逐字**照 Python 那几条 —— `--json-errors` / MCP 会把 `message` 与
/// `hint` 原样交给调用方，差一个字就是「两边说法不一样」。
pub fn make_guard(o: &SandboxOpts) -> crate::ImportGuardFn {
    let o = o.clone();
    Box::new(move |name, from_python, from_local, _line, _col, _filename| {
        let deny = |msg: String, hint: Option<String>| -> Result<(), JishiError> {
            Err(run_error(msg, hint))
        };
        if from_local {
            if !o.allow_write {
                return deny(
                    "沙箱禁止「从 本地包」导入（涉及文件访问）".to_string(),
                    Some("如需本地包，请显式开启 allow_write".to_string()));
            }
            return Ok(());
        }
        if from_python {
            if !o.allow_python_import {
                return deny(
                    "沙箱禁止「从 python」导入模块".to_string(),
                    Some("如需 Python 桥接，请显式开启 allow_python_import".to_string()));
            }
            let root = name.split('.').next().unwrap_or("");
            if BLOCKED_PYTHON_MODULES.contains(&root) && !o.allow_network {
                return deny(
                    format!("沙箱禁止导入模块「{name}」（网络/系统访问被拦截）"),
                    None);
            }
            return Ok(());
        }
        match name {
            "文件" | "表格" => {
                if !o.allow_write {
                    return deny(format!("沙箱禁止导入「{name}」（涉及文件读写）"),
                                Some("如需文件读写，请显式开启 allow_write".to_string()));
                }
                return Ok(());
            }
            "路径" | "压缩" => {
                if !o.allow_write {
                    return deny(format!("沙箱禁止导入「{name}」（涉及文件系统访问）"),
                                Some("如需文件系统访问，请显式开启 allow_write".to_string()));
                }
                return Ok(());
            }
            "网络" => {
                if !o.allow_network {
                    return deny("沙箱禁止导入「网络」（涉及网络访问）".to_string(),
                                Some("如需网络访问，请显式开启 allow_network".to_string()));
                }
                return Ok(());
            }
            "系统" => {
                if !o.allow_network {
                    return deny("沙箱禁止导入「系统」（涉及执行外部命令/系统访问）".to_string(),
                                Some("如需系统访问，请显式开启 allow_network".to_string()));
                }
                return Ok(());
            }
            _ => {}
        }
        if !ALLOW_MODULES.contains(&name) {
            return deny(format!("沙箱不允许导入模块「{name}」"), None);
        }
        Ok(())
    })
}

// ---------------------------------------------------------------------------
// 统一协议 JSON（`protocol.py` + `ai.error_to_json`）
// ---------------------------------------------------------------------------

/// 报错的位置「名字」—— 用错误码查表（那张表就是从 `errors.py` 搬来的，
/// `test_m85` 钉着不许漂）；查不到才退回类型名。
fn err_title(e: &JishiError) -> String {
    match &e.code {
        Some(c) => crate::errcodes::title(c)
            .map(|s| s.to_string())
            .unwrap_or_else(|| crate::error_title(&e.type_name).to_string()),
        None => crate::error_title(&e.type_name).to_string(),
    }
}

/// `ai.error_to_json` 的等价物。
///
/// 键顺序是 `code / title / message / line / col / hint`，后面**按需**追加
/// `trace` 与 `fix` —— Python 侧就是这么拼的（dict 保序，输出逐字节要一致）。
pub fn error_json(e: &JishiError) -> Jv {
    let mut kv: Vec<(String, Jv)> = vec![
        ("code".to_string(),
         e.code.as_ref().map(|c| Jv::Str(c.clone())).unwrap_or(Jv::Null)),
        ("title".to_string(), Jv::Str(err_title(e))),
        ("message".to_string(), Jv::Str(e.message.clone())),
        ("line".to_string(), e.line.map(Jv::Int).unwrap_or(Jv::Null)),
        ("col".to_string(), e.col.map(Jv::Int).unwrap_or(Jv::Null)),
        ("hint".to_string(),
         e.hint.as_ref().map(|h| Jv::Str(h.clone())).unwrap_or(Jv::Null)),
    ];
    if !e.trace.is_empty() {
        let arr: Vec<Jv> = e.trace.iter().map(|f| {
            Jv::Obj(vec![
                ("func".to_string(), Jv::Str(f.func.clone())),
                ("line".to_string(), f.line.map(Jv::Int).unwrap_or(Jv::Null)),
                ("col".to_string(), f.col.map(Jv::Int).unwrap_or(Jv::Null)),
            ])
        }).collect();
        kv.push(("trace".to_string(), Jv::Arr(arr)));
    }
    if let Some((old, new)) = &e.fix {
        kv.push(("fix".to_string(), Jv::Obj(vec![
            ("old".to_string(), Jv::Str(old.clone())),
            ("new".to_string(), Jv::Str(new.clone())),
            ("line".to_string(), e.line.map(Jv::Int).unwrap_or(Jv::Null)),
            ("col".to_string(), e.col.map(Jv::Int).unwrap_or(Jv::Null)),
        ])));
    }
    Jv::Obj(kv)
}

/// 沙箱计时 —— **wasm 上必须绕开**（R8.1 实测踩到）。
///
/// `wasm32-unknown-unknown` **没有时钟**：`std::time::Instant::now()` 会
/// **panic**（`time not implemented on this platform`）—— 而且 `panic = "abort"`
/// 下它表现成一句英文的 `RuntimeError: unreachable`，看着完全不像「时钟」的问题。
/// 所以 wasm 上计时恒为 0，**真实耗时由 JS shim 自己量**（那边有 `performance.now()`）。
#[cfg(not(target_arch = "wasm32"))]
pub(crate) type Clock = Instant;
#[cfg(target_arch = "wasm32")]
#[derive(Clone, Copy)]
pub(crate) struct Clock;

#[cfg(not(target_arch = "wasm32"))]
fn clock_start() -> Clock {
    Instant::now()
}
#[cfg(target_arch = "wasm32")]
fn clock_start() -> Clock {
    Clock
}

/// 已耗时（毫秒，`round(x, 2)`）。wasm 上恒 `0`（见 `Clock`）。
#[cfg(not(target_arch = "wasm32"))]
fn ms(start: Clock) -> f64 {
    let x = start.elapsed().as_secs_f64() * 1000.0;
    (x * 100.0).round() / 100.0
}
#[cfg(target_arch = "wasm32")]
fn ms(_start: Clock) -> f64 {
    0.0
}

/// `protocol.success`。
pub fn success_json(stdout: &str, value: Jv, duration: f64) -> String {
    dump_compact(&Jv::Obj(vec![
        ("ok".to_string(), Jv::Bool(true)),
        ("value".to_string(), value),
        ("stdout".to_string(), Jv::Str(stdout.to_string())),
        ("duration_ms".to_string(), Jv::Float(duration)),
    ]))
}

/// `protocol.failure`。
pub fn failure_json(e: &JishiError, duration: f64) -> String {
    dump_compact(&Jv::Obj(vec![
        ("ok".to_string(), Jv::Bool(false)),
        ("error".to_string(), error_json(e)),
        ("duration_ms".to_string(), Jv::Float(duration)),
    ]))
}

/// 超时错误（`run_sandboxed` / `eval_sandboxed` 共用一条文案）。
pub fn timeout_error(timeout: f64) -> JishiError {
    run_error(
        format!("执行超时（{} 秒）",
                jishi_frontend::serialize::py_repr_float(timeout)),
        Some("可能是有死循环，检查循环条件或递归结束条件".to_string()))
}

// ---------------------------------------------------------------------------
// 跑（自己编译 + VM）
// ---------------------------------------------------------------------------

/// 源码 → 字节码 JSON。解析错按**同一个出口**变成 `JishiError`
/// （`fix` / `underline` / `hint` 都带上 —— 少了它们 `--json-errors` 就比 Python 短）。
pub fn compile_source(text: &str, filename: &str) -> Result<String, JishiError> {
    let prog = jishi_frontend::parser::parse(text, filename)
        .map_err(|e| parse_err_to_jishi(&e))?;
    // ⚠️ `lib_dir = None`：**不展开基石库层**（`jishi/stdlib-jishi/*.jsh`）。
    // 宿主的标准库是原生实现、25 个模块一个不缺（`--dump-stdlib`），
    // 所以不需要把 `.jsh` 源码编进来 —— 单二进制也就**不依赖源码目录**。
    // 行为与「展开了基石库层」的 Python 侧一致（M52 立的规矩，对拍盯着）。
    let m = jishi_frontend::compiler::compile_program(&prog, filename, None, false);
    Ok(jishi_frontend::serialize::dump_json(&m))
}

/// `frontend::parser::ParseErr` → 宿主错误（字段一个不落）。
pub fn parse_err_to_jishi(e: &jishi_frontend::parser::ParseErr) -> JishiError {
    let title = crate::errcodes::title(e.code).unwrap_or("").to_string();
    JishiError {
        type_name: title,
        message: e.message.clone(),
        code: Some(e.code.to_string()),
        line: Some(if e.line == 0 { 1 } else { e.line as i64 }),
        col: Some(if e.col == 0 { 1 } else { e.col as i64 }),
        underline: e.underline.map(|(a, b)| (a as i64, b as i64)),
        hint: e.hint.clone(),
        fix: e.fix.clone(),
        trace: Vec::new(),
    }
}

/// 老老实实跑一遍：编译 → VM → 收 stdout。**不带超时**（那一层在下面）。
fn run_straight(source: &str, filename: &str, max_stdout: usize,
                max_steps: Option<u64>)
    -> Result<String, JishiError> {
    let bytecode = compile_source(source, filename)?;
    let mut vm = crate::load_module(&bytecode)
        .map_err(|e| run_error(format!("字节码加载失败：{e}"), None))?;
    crate::set_output_limit(&mut vm, Some(max_stdout));
    crate::set_step_limit(&mut vm, max_steps);
    match vm.run() {
        Ok(()) => Ok(crate::get_output(&vm).to_string()),
        // ⚠️ 出错时**不交 stdout** —— 与 Python `run_sandboxed` 的 `failure()`
        // 一致（它只给错误，不给已经打印的内容）。
        Err(e) => Err(e),
    }
}

/// 求一个表达式的值（`eval_sandboxed` 的内层）：跑完取「最后一个表达式语句的值」。
fn eval_straight(expr: &str, filename: &str, max_stdout: usize,
                 max_steps: Option<u64>)
    -> Result<Jv, EvalFail> {
    let bytecode = compile_source(expr, filename).map_err(EvalFail::Err)?;
    let mut vm = crate::load_module(&bytecode)
        .map_err(|e| EvalFail::Err(run_error(format!("字节码加载失败：{e}"), None)))?;
    crate::set_output_limit(&mut vm, Some(max_stdout));
    crate::set_step_limit(&mut vm, max_steps);
    vm.run().map_err(EvalFail::Err)?;
    match val_to_jv(crate::last_value(&vm)) {
        Ok(v) => Ok(v),
        Err(name) => Err(EvalFail::NotSerializable(name)),
    }
}

/// `eval_expr` 的两种失败：**基石自己的错**，与「值没法变成 JSON」。
pub enum EvalFail {
    /// 程序自己报的错（照常给 `failure` JSON）。
    Err(JishiError),
    /// 值不是 JSON 能表达的东西 —— Python 侧在这里冒 `TypeError`
    /// （`json.dumps` 挂了），MCP 的兜底把它翻译成一句 `{"ok": false, ...}`。
    NotSerializable(String),
}

/// 值 → JSON（`json.dumps(value)` 能表达的那些）。
///
/// ⚠️ Python 侧**只认** None / bool / int / float / str / list / dict 这几种，
/// 别的（集合、精确小数、日期、表格、文件、函数、类实例…）会抛
/// `TypeError: Object of type X is not JSON serializable`。这里同样拒 ——
/// 返回 `Err(类型名)`，由调用方翻成那句错误。
fn val_to_jv(v: &Val) -> Result<Jv, String> {
    Ok(match v {
        Val::None_ => Jv::Null,
        Val::Bool(b) => Jv::Bool(*b),
        Val::Int(i) => {
            if let Ok(small) = i64::try_from(*i) {
                Jv::Int(small)
            } else {
                Jv::Int128(*i)
            }
        }
        Val::Float(f) => Jv::Float(*f),
        Val::Str(s) => Jv::Str(s.clone()),
        Val::List(l) => {
            let mut out: Vec<Jv> = Vec::new();
            // 借用的活必须短：`val_to_jv` 是递归的，`borrow()` 只要还在手上
            // 就不能再借同一格（嵌套列表会 panic）。所以先克隆元素再递归。
            let items: Vec<Val> = l.borrow().clone();
            for x in &items {
                out.push(val_to_jv(x)?);
            }
            Jv::Arr(out)
        }
        Val::Dict(d) => {
            let mut out: Vec<(String, Jv)> = Vec::new();
            let items: Vec<(Val, Val)> = d.borrow().items.clone();
            for (k, x) in &items {
                out.push((dict_key_text(k)?, val_to_jv(x)?));
            }
            Jv::Obj(out)
        }
        // Python 侧这些都会让 `json.dumps` 抛 `TypeError`（不是 JSON 能表达的）。
        other => return Err(non_json_name(other).to_string()),
    })
}

/// 「这个值没法变成 JSON」时用来报的类型名 —— 尽量照 Python 侧 `json.dumps`
/// 那句话里的类名（`Object of type set is not JSON serializable`）。
///
/// ⚠️ **已知边界**：类实例 / 自定义对象那些，Python 报的是它自己的类名
/// （`_Instance` 之类），宿主这边只能给个近似的 —— `test_m92` 里两个方向都
/// 钉着，改了两边一起改。常规值（数值 / 文本 / 列表 / 字典 / 真假 / 空）
/// **完全一致**，那些才是 agent 真会传的。
fn non_json_name(v: &Val) -> &'static str {
    match v {
        Val::Set(_) => "set",
        Val::Dec(_) => "Decimal",
        Val::Date(_) => "datetime",
        Val::Table(_) => "_Table",
        Val::File(_) => "_FileHandle",
        Val::Slice(_) => "slice",
        Val::Func(_) | Val::WalkFn(_) | Val::Builtin(_, _) | Val::BoundMethod(_) =>
            "function",
        Val::Class(_) => "type",
        Val::Instance(_) => "_Instance",
        Val::Module(_, _) => "Module",
        Val::ExcType(_) => "type",
        Val::Exc(_) => "Exception",
        Val::HtmlRaw(_) => "HtmlRaw",
        Val::LoopSignal(_) => "int",
        Val::Cell(_) => "cell",
        Val::Super(_) => "super",
        Val::ListIter(_) | Val::GuardedIter(_) => "iterator",
        // 上面那些能把 JSON 说清楚的值走不到这里。
        _ => "object",
    }
}

/// 字典键 → JSON 对象的键文本。
///
/// Python 的 `json.dumps` 允许 str / int / float / bool / None 当键
/// （别的抛 `TypeError: keys must be str, int, float, bool or None`），
/// int/float 会把键**转成字符串**（`{1: 'a'}` → `{"1": "a"}`）。
fn dict_key_text(k: &Val) -> Result<String, String> {
    match k {
        Val::Str(s) => Ok(s.clone()),
        Val::Int(i) => Ok(i.to_string()),
        Val::Float(f) => Ok(jishi_frontend::serialize::py_repr_float(*f)),
        Val::Bool(b) => Ok(if *b { "true" } else { "false" }.to_string()),
        Val::None_ => Ok("null".to_string()),
        other => Err(non_json_name(other).to_string()),
    }
}

// ---------------------------------------------------------------------------
// 对外：带超时的两条路
// ---------------------------------------------------------------------------

/// 在沙箱里跑一段源码，返回 **protocol JSON 文本**（`run_sandboxed` 的等价物）。
///
/// ⚠️ 这条路**要线程**（超时靠另起线程 + `recv_timeout`）—— 只给原生宿主用。
/// **wasm 用 [`run_sandboxed_direct`]**。
pub fn run_sandboxed(source: &str, opts: &SandboxOpts, filename: &str) -> String {
    let start = clock_start();
    let (tx, rx) = mpsc::channel::<Result<String, JishiError>>();
    let src = source.to_string();
    let fname = filename.to_string();
    let o = opts.clone();
    std::thread::spawn(move || {
        crate::set_import_guard(Some(make_guard(&o)));
        let r = run_straight(&src, &fname, o.max_stdout, o.max_steps);
        crate::set_import_guard(None);          // 跑完摘掉，别影响同一进程里的别人
        let _ = tx.send(r);
    });
    match rx.recv_timeout(secs(opts.timeout)) {
        Ok(Ok(out)) => success_json(&out, Jv::Null, ms(start)),
        Ok(Err(e)) => failure_json(&e, ms(start)),
        // 超时：丢下那条线程不管（见模块头那段说明）。
        Err(_) => failure_json(&timeout_error(opts.timeout), ms(start)),
    }
}

/// 在沙箱里求值一个表达式 —— 返回 `Ok(protocol JSON)`，
/// 或者 `Err(类型名)`（值不是 JSON 能表达的，Python 侧在这里冒 `TypeError`）。
pub fn eval_sandboxed(expr: &str, opts: &SandboxOpts, filename: &str)
    -> Result<String, String> {
    let start = clock_start();
    let (tx, rx) = mpsc::channel::<Result<Jv, EvalFail>>();
    let src = expr.to_string();
    let fname = filename.to_string();
    let o = opts.clone();
    std::thread::spawn(move || {
        crate::set_import_guard(Some(make_guard(&o)));
        let r = eval_straight(&src, &fname, o.max_stdout, o.max_steps);
        crate::set_import_guard(None);
        let _ = tx.send(r);
    });
    match rx.recv_timeout(secs(opts.timeout)) {
        Ok(Ok(v)) => Ok(success_json("", v, ms(start))),
        Ok(Err(EvalFail::Err(e))) => Ok(failure_json(&e, ms(start))),
        Ok(Err(EvalFail::NotSerializable(name))) => Err(name),
        Err(_) => Ok(failure_json(&timeout_error(opts.timeout), ms(start))),
    }
}

// ---------------------------------------------------------------------------
// 不上线程的那条路（**wasm** 用：R8.1）
// ---------------------------------------------------------------------------

/// 在沙箱里跑一段源码 —— **不开线程**，用**步数预算**兜死循环。
///
/// 为什么要另开一条：`wasm32-unknown-unknown` 上**没有线程**，`std::thread::spawn`
/// 会直接 panic ⇒ 上面那条路的超时机制在浏览器里用不了。这里改成
/// **同步调用 + `opts.max_steps` 步数上限**（见 `crate::set_step_limit`）：
/// 死循环跑满预算就报「执行步数超过上限」，页面不会冻住。
///
/// 📌 **错误与成功都返回同一份协议 JSON**（与 `run_sandboxed` 同构），
/// 所以 JS 侧只有一条解析路径。
pub fn run_sandboxed_direct(source: &str, opts: &SandboxOpts, filename: &str) -> String {
    let start = clock_start();
    crate::set_import_guard(Some(make_guard(opts)));
    let r = run_straight(source, filename, opts.max_stdout, opts.max_steps);
    crate::set_import_guard(None);
    match r {
        Ok(out) => success_json(&out, Jv::Null, ms(start)),
        Err(e) => failure_json(&e, ms(start)),
    }
}

/// 求值一个表达式 —— 不开线程（wasm 用）。语义与 [`eval_sandboxed`] 一致。
pub fn eval_sandboxed_direct(expr: &str, opts: &SandboxOpts, filename: &str)
    -> Result<String, String> {
    let start = clock_start();
    crate::set_import_guard(Some(make_guard(opts)));
    let r = eval_straight(expr, filename, opts.max_stdout, opts.max_steps);
    crate::set_import_guard(None);
    match r {
        Ok(v) => Ok(success_json("", v, ms(start))),
        Err(EvalFail::Err(e)) => Ok(failure_json(&e, ms(start))),
        Err(EvalFail::NotSerializable(name)) => Err(name),
    }
}

/// 秒 → `Duration`（负数 / NaN 一律当 0，别让 `from_secs_f64` 直接 panic）。
fn secs(t: f64) -> Duration {
    if !t.is_finite() || t <= 0.0 {
        Duration::from_secs(0)
    } else {
        Duration::from_secs_f64(t)
    }
}
