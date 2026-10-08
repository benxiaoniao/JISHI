// 基石前端核心 · Python 扩展入口（R2）。
//
// 这里**只做搬运**：调用 `tokenizer::tokenize`，把结果变成 Python 对象。
// 语义一律在 `tokenizer.rs` 里，本文件不做任何语言判断。
//
// 为什么产出的是**真正的 `jishi.tokenizer.Token` 实例**（而不是元组或字典）：
//   - 上层（parser / formatter / LSP）拿到的东西必须与 Python 版**同形**，
//     否则「换实现」就变成了「换接口」；
//   - 一致性夹具靠 `类型 == "Token"` 认节点（`conformance.jsonable` 取
//     `type(obj).__name__`），用别的容器会直接改变冻结产物。
//
// ⚠️ 出错时抛的是本模块自己的 `LexError`（带 `码/消息/行/列/提示/下划线`
// 六个属性）—— **由 `jishi/tokenizer.py` 还原成对应的 `JishiError` 子类**。
// 这么绕一圈是为了「错误码 → 异常类」的映射只有一处（Python 侧从
// `jishi.errors` 里按 `code` 现查），Rust 这边不抄一份类表、也就不会漂。

// R7.1-a（2026-10-05）：**前端本体已抽成独立 crate** `jishi-frontend`（纯 Rust、
// 零依赖），与独立宿主 `rust/` 共用同一份实现。本文件只剩「搬运用」的 pyo3 壳。
// 引用方式不变（还是 `tokenizer::tokenize` 这样写），只是名字来自下面这行 `use`。
use jishi_frontend::{compiler, opcodes, parser, serialize, tokenizer};

use std::sync::OnceLock;

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyList, PyModule, PyString, PyTuple};

create_exception!(_core, LexError, PyException);
create_exception!(_core, ParseError, PyException);

/// token 类型的名字**只造一次**。
///
/// 为什么值得：一次 2 万行分词要产出约 12 万个 token，每个 token 的 `type`
/// 都是一次 `PyString::new`。这些值只有 10 种 —— 缓存下来省掉的是
/// **12 万次分配**（实测端到端能省下几个百分点，而这条路径上每一毫秒都算数）。
/// `OnceLock` 在这里是安全的：所有访问都在持有 GIL 时发生。
///
/// ⚠️ 是 **10** 种不是 9 种：Python 的 `TOKEN_TYPES` 那个集合里**漏了
/// `FSTRING`**（M7 加插值字符串时没补），实际会产出。少一种的后果是
/// `unreachable!` 直接 panic —— 这是**故意的**：宁可当场炸，也别悄悄给错。
static KINDS: OnceLock<[Py<PyString>; 10]> = OnceLock::new();
static NEWLINE_VALUE: OnceLock<Py<PyString>> = OnceLock::new();

fn kind_name_index(kind: &str) -> usize {
    match kind {
        "INDENT" => 0,
        "DEDENT" => 1,
        "NEWLINE" => 2,
        "NUMBER" => 3,
        "STRING" => 4,
        "NAME" => 5,
        "KEYWORD" => 6,
        "OP" => 7,
        "FSTRING" => 8,
        "EOF" => 9,
        other => unreachable!("未知 token 类型：{other}"),
    }
}

fn kind_obj(py: Python<'_>, kind: &str) -> Py<PyString> {
    let cache = KINDS.get_or_init(|| {
        ["INDENT", "DEDENT", "NEWLINE", "NUMBER", "STRING", "NAME", "KEYWORD",
         "OP", "FSTRING", "EOF"]
            .map(|k| PyString::new(py, k).unbind())
    });
    cache[kind_name_index(kind)].clone_ref(py)
}

fn newline_obj(py: Python<'_>) -> Py<PyString> {
    NEWLINE_VALUE.get_or_init(|| PyString::new(py, "\n").unbind()).clone_ref(py)
}

fn value_to_py(py: Python<'_>, v: &tokenizer::Value) -> PyResult<Py<PyAny>> {
    use tokenizer::Value;
    Ok(match v {
        Value::None => py.None(),
        Value::Int(i) => PyInt::new(py, *i).into_any().unbind(),
        Value::Float(f) => pyo3::types::PyFloat::new(py, *f).into_any().unbind(),
        Value::Str(s) => PyString::new(py, s).into_any().unbind(),
        // 超出 i64 的整数字面量：交给 Python 的 `int()` 变任意精度整数，
        // 与 Python 版分词的产物**逐位相同**（自己截断就是 M32 那类静默丢精度）。
        Value::Big(s) => py
            .import("builtins")?
            .getattr("int")?
            .call1((s.as_str(),))?
            .unbind(),
        Value::Parts(parts) => {
            let list = PyList::empty(py);
            for p in parts {
                let (k, s) = match p {
                    tokenizer::Part::Text(s) => ("text", s),
                    tokenizer::Part::Expr(s) => ("expr", s),
                };
                list.append(PyTuple::new(py, [k, s.as_str()])?)?;
            }
            list.into_any().unbind()
        }
    })
}

fn to_py_err(py: Python<'_>, e: tokenizer::LexErr) -> PyErr {
    let err = LexError::new_err(e.message.clone());
    let v = err.value(py);
    // ⚠️ 不送「源行」：它在 Python 侧一律由 `.with_source(lines)` 按
    // `行` 现取（七个词法错误的 `line` 恒在范围内），少传一份就少一份漂移。
    let _ = v.setattr("码", e.code);
    let _ = v.setattr("消息", e.message);
    let _ = v.setattr("行", e.line);
    let _ = v.setattr("列", e.col);
    let _ = v.setattr("提示", e.hint);
    let _ = v.setattr("下划线", e.underline);
    err
}

/// 分词：返回 `list[jishi.tokenizer.Token]`，与 Python 实现逐 token 一致。
#[pyfunction]
#[pyo3(signature = (source, filename="<输入>"))]
fn tokenize(py: Python<'_>, source: &str, filename: &str)
    -> PyResult<Py<PyAny>>
{
    let out = match tokenizer::tokenize(source, filename) {
        Ok(o) => o,
        Err(e) => return Err(to_py_err(py, e)),
    };
    let cls = py.import("oracle.jishi.tokenizer")?.getattr("Token")?;
    let list = PyList::empty(py);

    // 一行文本常被同一行的十几个 token 共用 —— 缓存一格就够
    // （token 基本按行序产出），省下的是 12 万次 `PyString::new`。
    let mut last_idx: Option<usize> = None;
    let mut last_obj: Option<Py<PyString>> = None;

    for t in &out.tokens {
        let value: Py<PyAny> = if t.kind == "NEWLINE" {
            newline_obj(py).into_any()
        } else {
            value_to_py(py, &t.value)?
        };
        let src_obj: Py<PyString> = match t.src {
            None => PyString::new(py, "").unbind(),
            Some(idx) => {
                if last_idx != Some(idx) {
                    last_obj = Some(PyString::new(py, &out.lines[idx]).unbind());
                    last_idx = Some(idx);
                }
                last_obj.as_ref().expect("刚填过").clone_ref(py)
            }
        };
        let obj = cls.call1((kind_obj(py, t.kind), value, t.line, t.col,
                             t.end_col, src_obj))?;
        list.append(obj)?;
    }
    Ok(list.into_any().unbind())
}

// ---------------------------------------------------------------------------
// R3：语法 → AST
// ---------------------------------------------------------------------------

/// 把通用 AST（`parser::J`）变成 Python 对象。
///
/// 节点按 `kind` 去 `jishi.ast_nodes` 里找同名 dataclass，**全部字段走 kwargs**
/// （`kw_only=True` 的 dataclass 正好只接受关键字实参）—— 于是 34 个节点类
/// 共用这一段转换，Rust 侧不必为每个类各写一遍结构体。
fn j_to_py(py: Python<'_>, v: &parser::J) -> PyResult<Py<PyAny>> {
    use parser::J;
    Ok(match v {
        J::Null => py.None(),
        J::Bool(b) => pyo3::types::PyBool::new(py, *b).to_owned().into_any()
            .unbind(),
        J::Int(i) => PyInt::new(py, *i).into_any().unbind(),
        // 超出 i64 的整数交给 Python 的 `int()` 变任意精度（同 R2 的 NUMBER）
        J::Big(x) => py.import("builtins")?.getattr("int")?.call1((x.as_str(),))?
            .unbind(),
        J::Float(f) => pyo3::types::PyFloat::new(py, *f).into_any().unbind(),
        J::Str(x) => PyString::new(py, x).into_any().unbind(),
        J::List(items) => {
            let list = PyList::empty(py);
            for it in items {
                list.append(j_to_py(py, it)?)?;
            }
            list.into_any().unbind()
        }
        J::Node(n) => {
            let cls = py.import("oracle.jishi.ast_nodes")?.getattr(n.kind)?;
            let kwargs = PyDict::new(py);
            for (k, fv) in &n.fields {
                kwargs.set_item(*k, j_to_py(py, fv)?)?;
            }
            cls.call((), Some(&kwargs))?.unbind()
        }
    })
}

fn to_parse_err(py: Python<'_>, e: parser::ParseErr) -> PyErr {
    let err = ParseError::new_err(e.message.clone());
    let v = err.value(py);
    // 与词法错误同一套：只回传事实，异常类由 Python 侧按 `码` 现查
    let _ = v.setattr("码", e.code);
    let _ = v.setattr("消息", e.message);
    let _ = v.setattr("行", e.line);
    let _ = v.setattr("列", e.col);
    let _ = v.setattr("提示", e.hint);
    let _ = v.setattr("下划线", e.underline);
    let _ = v.setattr("修正", e.fix);
    err
}

/// 语法分析：吃**源码文本**，产出真正的 `jishi.ast_nodes` 节点树。
///
/// ⚠️ 参数是文本、不是「源码行」：行是有损载体（各家切分口径不同，见
/// `parser::parse` 的注释）。Python 侧的壳负责保证交过来的就是原文。
#[pyfunction]
#[pyo3(signature = (source, filename="<输入>"))]
fn parse(py: Python<'_>, source: &str, filename: &str)
    -> PyResult<Py<PyAny>>
{
    match parser::parse(source, filename) {
        Ok(program) => {
            let n = parser::J::Node(Box::new(program));
            j_to_py(py, &n)
        }
        Err(e) => Err(to_parse_err(py, e)),
    }
}

/// 把一段文本解析成**单个表达式**（`parser.parse_expression`，调试器「查看」用）
#[pyfunction]
#[pyo3(signature = (text, filename="<调试>"))]
fn parse_expression(py: Python<'_>, text: &str, filename: &str)
    -> PyResult<Py<PyAny>>
{
    match parser::parse_expression(text, filename) {
        Ok(node) => {
            let n = parser::J::Node(Box::new(node));
            j_to_py(py, &n)
        }
        Err(e) => Err(to_parse_err(py, e)),
    }
}

/// 只跑语法核心、**不构造任何 Python 对象**，返回 AST 节点个数（用来把
/// 「语法本身多快」与「转成 Python 对象多贵」分开量，同 `token_count`）。
#[pyfunction]
#[pyo3(signature = (source, filename="<输入>"))]
fn parse_node_count(source: &str, filename: &str) -> PyResult<usize> {
    match parser::parse(source, filename) {
        Ok(p) => Ok(count_nodes(&parser::J::Node(Box::new(p)))),
        Err(e) => Err(ParseError::new_err(e.message)),
    }
}

fn count_nodes(v: &parser::J) -> usize {
    match v {
        parser::J::Node(n) => {
            1 + n.fields.iter().map(|(_, f)| count_nodes(f)).sum::<usize>()
        }
        parser::J::List(items) => items.iter().map(count_nodes).sum(),
        _ => 0,
    }
}

/// 关键字 / 粘连前缀 / 运算符三张表（供漂移检测逐项比对 Python 侧）。
#[pyfunction]
fn keyword_table(py: Python<'_>) -> Py<PyList> {
    PyList::new(py, tokenizer::keyword_table()).expect("字面量列表").unbind()
}

#[pyfunction]
fn glue_table(py: Python<'_>) -> Py<PyList> {
    PyList::new(py, tokenizer::glue_table()).expect("字面量列表").unbind()
}

#[pyfunction]
fn operator_table(py: Python<'_>) -> Py<PyList> {
    PyList::new(py, tokenizer::operator_table()).expect("字面量列表").unbind()
}

/// 只跑分词核心、**不构造任何 Python 对象**，返回 token 个数。
///
/// 存在的理由：把「分词本身多快」与「转成 Python 对象多贵」分开量。
/// 端到端倍数只有与核心倍数摆在一起，才知道瓶颈是在 Rust 还是在 FFI。
#[pyfunction]
#[pyo3(signature = (source, filename="<输入>"))]
fn token_count(source: &str, filename: &str) -> PyResult<usize> {
    match tokenizer::tokenize(source, filename) {
        Ok(o) => Ok(o.tokens.len()),
        Err(e) => Err(LexError::new_err(e.message)),
    }
}

#[pyfunction]
fn core_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

// ---------------------------------------------------------------------------
// R5：编译器 → 字节码
// ---------------------------------------------------------------------------

/// 常量池项 → Python 值。
fn const_to_py(py: Python<'_>, c: &compiler::Const) -> PyResult<Py<PyAny>> {
    use compiler::Const;
    Ok(match c {
        Const::None => py.None(),
        Const::Bool(b) => pyo3::types::PyBool::new(py, *b).to_owned().into_any().unbind(),
        Const::Int(i) => PyInt::new(py, *i).into_any().unbind(),
        // 超出 i64 的整数交给 Python 的 `int()` 变任意精度（同 R2/R3）
        Const::Big(x) => py.import("builtins")?.getattr("int")?.call1((x.as_str(),))?
            .unbind(),
        Const::Float(f) => pyo3::types::PyFloat::new(py, *f).into_any().unbind(),
        Const::Str(x) => PyString::new(py, x).into_any().unbind(),
    })
}

/// `compile::Module` → 真正的 `jishi.opcodes.CompiledModule`（含 `local_hints`）。
///
/// ⚠️ **为什么还要造 Python 对象**（明明 JSON 才是契约产物）：`local_hints`
/// 不在字节码 JSON 里（格式没这个字段），而字节码执行器**报错时要用它**
/// ——「赋值前读局部」那句「外层也有一个叫「x」的变量…」就是从这儿来的。
/// 只给 JSON 的话，rust 前端跑 VM/CVM 时那句提示会**静默消失**，
/// 两个前端就有了语义差异（R 线的红线）。
fn to_py_module(py: Python<'_>, m: &compiler::Module) -> PyResult<Py<PyAny>> {
    let ops = py.import("oracle.jishi.opcodes")?;
    let mut consts: Vec<Py<PyAny>> = Vec::with_capacity(m.consts.len());
    for c in &m.consts {
        consts.push(const_to_py(py, c)?);
    }

    let mut codes: Vec<Py<PyAny>> = Vec::with_capacity(m.codes.len());
    for c in &m.codes {
        let mut instrs: Vec<Py<PyAny>> = Vec::with_capacity(c.instrs.len());
        for i in &c.instrs {
            let o = ops.getattr("Instr")?
                .call1((i.op, i.a, i.b, i.c, i.line, i.col))?;
            if i.stmt_line != 0 {
                o.setattr("stmt_line", i.stmt_line)?;
            }
            instrs.push(o.unbind());
        }
        let hints = PyDict::new(py);
        for (k, v) in &c.local_hints {
            hints.set_item(*k, v)?;
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item("name", &c.name)?;
        kwargs.set_item("params", c.params.clone())?;
        kwargs.set_item("param_idx", c.param_idx.clone())?;
        kwargs.set_item("param_local", c.param_local.clone())?;
        kwargs.set_item("param_cell", c.param_cell.clone())?;
        kwargs.set_item("param_kind", c.param_kind.clone())?;
        kwargs.set_item("instrs", instrs)?;
        kwargs.set_item("nlocals", c.nlocals)?;
        kwargs.set_item("local_names", c.local_names.clone())?;
        kwargs.set_item("cellvars", c.cellvars.clone())?;
        kwargs.set_item("freevars", c.freevars.clone())?;
        kwargs.set_item("local_hints", hints)?;
        kwargs.set_item("firstlineno", c.firstlineno)?;
        codes.push(ops.getattr("Code")?.call((), Some(&kwargs))?.unbind());
    }

    let kwargs = PyDict::new(py);
    kwargs.set_item("consts", consts)?;
    kwargs.set_item("names", m.names.clone())?;
    kwargs.set_item("kw_names", m.kw_names.clone())?;
    kwargs.set_item("codes", codes)?;
    kwargs.set_item("main", m.main)?;
    kwargs.set_item("filename", &m.filename)?;
    Ok(ops.getattr("CompiledModule")?.call((), Some(&kwargs))?.unbind())
}

/// 源码 → **字节码 JSON 文本**（R5 的契约产物；全程原生，不经 Python 对象）。
#[pyfunction]
#[pyo3(signature = (source, filename="<输入>", lib_dir=None,
                    fuse_method_call=false))]
fn compile(py: Python<'_>, source: &str, filename: &str,
           lib_dir: Option<&str>, fuse_method_call: bool) -> PyResult<String> {
    let prog = parser::parse(source, filename).map_err(|e| to_parse_err(py, e))?;
    let m = compiler::compile_program(&prog, filename, lib_dir, fuse_method_call);
    Ok(serialize::dump_json(&m))
}

/// 源码 → 真正的 `CompiledModule`（给 Python VM / C VM / 调试器用）。
#[pyfunction]
#[pyo3(signature = (source, filename="<输入>", lib_dir=None,
                    fuse_method_call=false))]
fn compile_module(py: Python<'_>, source: &str, filename: &str,
                  lib_dir: Option<&str>, fuse_method_call: bool)
    -> PyResult<Py<PyAny>> {
    let prog = parser::parse(source, filename).map_err(|e| to_parse_err(py, e))?;
    let m = compiler::compile_program(&prog, filename, lib_dir, fuse_method_call);
    to_py_module(py, &m)
}

/// 指令表（号 → 名字）—— 给漂移检测用（与 Python 的 `OP_NAMES` 逐项比）。
#[pyfunction]
fn opcode_table() -> Vec<(String, i64)> {
    opcodes::OP_NAMES
        .iter()
        .enumerate()
        .map(|(i, n)| (n.to_string(), i as i64))
        .collect()
}

/// `repr(float)` 的 Rust 复刻 —— 暴露出来是为了让测试拿它**对拍**。
///
/// 为什么要专门对拍：字节码 JSON 里的浮点是 `repr` 文本，Rust 的 `{}` 不写
/// 指数（`1e30` 会印成 30 个 0）、`{:e}` 的指数又没补零。这种东西**不能靠眼看**，
/// 只能拿几千个值逐个比（`tests/test_m62_compiler_rust.py`）。
#[pyfunction]
fn float_repr(x: f64) -> String {
    serialize::py_repr_float(x)
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tokenize, m)?)?;
    m.add_function(wrap_pyfunction!(token_count, m)?)?;
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    m.add_function(wrap_pyfunction!(keyword_table, m)?)?;
    m.add_function(wrap_pyfunction!(glue_table, m)?)?;
    m.add_function(wrap_pyfunction!(operator_table, m)?)?;
    // R3
    m.add_function(wrap_pyfunction!(parse, m)?)?;
    m.add_function(wrap_pyfunction!(parse_expression, m)?)?;
    m.add_function(wrap_pyfunction!(parse_node_count, m)?)?;
    // R5
    m.add_function(wrap_pyfunction!(compile, m)?)?;
    m.add_function(wrap_pyfunction!(compile_module, m)?)?;
    m.add_function(wrap_pyfunction!(opcode_table, m)?)?;
    m.add_function(wrap_pyfunction!(float_repr, m)?)?;
    m.add("LexError", m.py().get_type::<LexError>())?;
    m.add("ParseError", m.py().get_type::<ParseError>())?;
    Ok(())
}
