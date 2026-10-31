//! 基石语言服务器（`jishi lsp`）—— JSON-RPC over stdio 的最小 LSP 实现（R7.3）。
//!
//! 逐字对齐 Python 侧 `jishi/lsp.py`（18 个方法、报文逐字节），由
//! `tests/test_m91_lsp_rust.py` 对拍 + 编辑器自带的 `verify-lsp.js` 双跑钉住。
//!
//! ## 协议要点（照抄 Python，别自作主张）
//!
//! * **分帧**：`Content-Length: N\r\n\r\n` + UTF-8 正文；报文是
//!   `json.dumps(o, ensure_ascii=False)` 的**紧凑保序**形式（[`crate::jsonw`]）。
//! * **行列是 0 基**，而基石内部报错是 **1 基**，换算集中在
//!   [`range_from_error`] 与 [`pos`] 两处。
//! * **位置单位是 UTF-16 码元**（LSP 默认）：中文算 1、emoji 算 2。
//!
//! ## 数据源单一
//!
//! 关键字 / 内建 / 标准库 / 方法 都取自 [`crate::langdata`]（R7.2 搬进来的
//! 那份显式表），**不在这里另抄一份**；作用域分析走 [`crate::scope`]
//! （与命令行静态检查**同一份实现**）。这两条正是「编辑器报、命令行不报」
//! 这类漂移的解药。

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use jishi_frontend::parser::{self, J, Node};
use jishi_frontend::tokenizer;

use crate::jsonw::{dump_compact, Jv};
use crate::langdata as D;
use crate::scope;

// --- JSON-RPC 错误码 ---
const METHOD_NOT_FOUND: i64 = -32601;
const INTERNAL_ERROR: i64 = -32603;

/// 文本同步方式（LSP: TextDocumentSyncKind）—— 2 = 增量。
const SYNC_INCREMENTAL: i64 = 2;

// --- LSP 诊断严重级别 ---
const SEVERITY_ERROR: i64 = 1;
const SEVERITY_WARNING: i64 = 2;

// --- LSP 补全类型 ---
const KIND_METHOD: i64 = 2;
const KIND_FUNCTION: i64 = 3;
const KIND_CLASS: i64 = 7;
const KIND_MODULE: i64 = 9;
const KIND_VARIABLE: i64 = 6;
const KIND_KEYWORD: i64 = 14;

// --- LSP 符号类型 ---
const SYM_MODULE: i64 = 2;
const SYM_CLASS: i64 = 5;
const SYM_FUNCTION: i64 = 12;
const SYM_VARIABLE: i64 = 13;

/// 所有文件都可能用到的「值关键字」，不算未定义。
const LITERAL_KEYWORDS: [&str; 3] = ["真", "假", "空"];

/// 语义高亮的 token 类型表 —— **顺序就是契约**（客户端按下标回传）。
const SEMANTIC_TYPES: [&str; 6] = [
    "keyword", "function", "variable", "parameter", "namespace", "property",
];
const SEMANTIC_MODIFIERS: [&str; 3] = ["declaration", "defaultLibrary", "readonly"];

const CODE_ACTION_QUICKFIX: &str = "quickfix";
const CODE_ACTION_SOURCE: &str = "source.format";

fn sem_type(name: &str) -> i64 {
    SEMANTIC_TYPES.iter().position(|x| *x == name).expect("已知语义类型") as i64
}

fn sem_mod(name: &str) -> i64 {
    1 << SEMANTIC_MODIFIERS.iter().position(|x| *x == name).expect("已知修饰符")
}

// ---------------------------------------------------------------------------
// JSON 小工具
// ---------------------------------------------------------------------------

fn s(x: impl Into<String>) -> Jv {
    Jv::Str(x.into())
}

fn obj(pairs: Vec<(&str, Jv)>) -> Jv {
    Jv::Obj(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn arr(items: Vec<Jv>) -> Jv {
    Jv::Arr(items)
}

/// 把宿主 JSON 值转成「输出用」的保序值（回显 `id` 用；`params` 里的值同理）。
fn json_to_jv(v: &crate::json::Json) -> Jv {
    use crate::json::Json;
    match v {
        Json::Null => Jv::Null,
        Json::Bool(b) => Jv::Bool(*b),
        Json::Int(i) => Jv::Int((*i).try_into().unwrap_or(0)),
        Json::Float(f) => Jv::Str(format!("{f}")),
        Json::Str(x) => Jv::Str(x.clone()),
        Json::Arr(a) => Jv::Arr(a.iter().map(json_to_jv).collect()),
        Json::Obj(m) => Jv::Obj(m.iter().map(|(k, x)| (k.clone(), json_to_jv(x))).collect()),
    }
}

/// 造一个「只有 `textDocument.uri`」的 params（`on_code_action` 里复用
/// `on_formatting` 用）。
fn text_doc_params(uri: &str) -> crate::json::Json {
    use crate::json::Json;
    let mut td = std::collections::BTreeMap::new();
    td.insert("uri".to_string(), Json::Str(uri.to_string()));
    let mut top = std::collections::BTreeMap::new();
    top.insert("textDocument".to_string(), Json::Obj(td));
    Json::Obj(top)
}

fn pget<'a>(params: &'a crate::json::Json, key: &str) -> Option<&'a crate::json::Json> {
    params.get(key)
}

fn pstr(v: Option<&crate::json::Json>) -> String {
    v.and_then(|x| x.as_str()).unwrap_or("").to_string()
}

fn pint(v: Option<&crate::json::Json>) -> i64 {
    v.and_then(|x| x.as_i64()).unwrap_or(0)
}

/// `params.textDocument.uri`
fn uri_of(params: &crate::json::Json) -> String {
    pstr(pget(params, "textDocument").and_then(|d| pget(d, "uri")))
}

// ---------------------------------------------------------------------------
// 位置换算（LSP 0 基 + UTF-16 码元 ↔ 内部 1 基 + 字符）
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
struct Pos {
    line: i64,
    ch: i64,
}

#[derive(Clone, Copy, PartialEq)]
struct Rng {
    start: Pos,
    end: Pos,
}

impl Rng {
    fn jv(&self) -> Jv {
        obj(vec![
            ("start", obj(vec![("line", Jv::Int(self.start.line)),
                               ("character", Jv::Int(self.start.ch))])),
            ("end", obj(vec![("line", Jv::Int(self.end.line)),
                             ("character", Jv::Int(self.end.ch))])),
        ])
    }
}

fn pos(line0: i64, char0: i64) -> Pos {
    Pos { line: line0.max(0), ch: char0.max(0) }
}

/// JishiError 的 1 基行列 → LSP 的 0 基区间（末端不含）。
fn range_from_error(line: i64, col: i64, underline: Option<(i64, i64)>) -> Rng {
    let line = line.max(1);
    let (mut start, mut end) = match underline {
        Some((a, b)) => (a, b),
        None => (col, col),
    };
    if start < 1 {
        start = 1;
    }
    if end < start {
        end = start;
    }
    Rng { start: pos(line - 1, start - 1), end: pos(line - 1, end) }
}

/// 文本在 LSP 位置单位（UTF-16 码元）下的长度。
fn utf16_len(t: &str) -> i64 {
    t.chars().map(|c| if (c as u32) > 0xFFFF { 2 } else { 1 }).sum()
}

/// UTF-16 码元数 → 字符下标（越界时夹到字符串两端）。
fn utf16_to_index(t: &str, units: i64) -> i64 {
    if units <= 0 {
        return 0;
    }
    if units >= utf16_len(t) {
        return t.chars().count() as i64;
    }
    let mut total: i64 = 0;
    for (i, ch) in t.chars().enumerate() {
        total += if (ch as u32) > 0xFFFF { 2 } else { 1 };
        if total >= units {
            return i as i64 + 1;
        }
    }
    t.chars().count() as i64
}

/// 字符偏移 → (行, 列)，行 0 基、列按 UTF-16 码元。
fn offset_to_pos(text: &str, offset: i64) -> Pos {
    let cs: Vec<char> = text.chars().collect();
    let n = cs.len() as i64;
    let offset = offset.max(0).min(n);
    let mut line: i64 = 0;
    let mut line_start: i64 = 0;
    for i in 0..offset {
        if cs[i as usize] == '\n' {
            line += 1;
            line_start = i + 1;
        }
    }
    let seg: String = cs[line_start as usize..offset as usize].iter().collect();
    pos(line, utf16_len(&seg))
}

/// (行, 列) 0 基（列按 UTF-16 码元）→ 字符偏移。
fn pos_to_offset(text: &str, line0: i64, char0: i64) -> i64 {
    let lines: Vec<&str> = text.split('\n').collect();
    let line0 = line0.max(0).min(lines.len() as i64 - 1);
    let mut offset: i64 = 0;
    for l in &lines[..line0 as usize] {
        offset += l.chars().count() as i64 + 1;
    }
    offset + utf16_to_index(lines[line0 as usize], char0)
}

/// 整篇文档的区间（格式化用）。
fn whole_doc_range(text: &str) -> Rng {
    let lines: Vec<&str> = text.split('\n').collect();
    Rng {
        start: pos(0, 0),
        end: pos(lines.len() as i64 - 1, utf16_len(lines[lines.len() - 1])),
    }
}

/// 字符列（0 基）→ UTF-16 码元列。
fn utf16_col(line_text: &str, char_col0: i64) -> i64 {
    let cs: Vec<char> = line_text.chars().collect();
    let k = char_col0.max(0).min(cs.len() as i64) as usize;
    let seg: String = cs[..k].iter().collect();
    utf16_len(&seg)
}

// ---------------------------------------------------------------------------
// URI 互转
// ---------------------------------------------------------------------------

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).ok()
                .and_then(|h| u8::from_str_radix(h, 16).ok());
            if let Some(b) = hex {
                out.push(b);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

fn quote_path(p: &str) -> String {
    const SAFE: &str = "/:()~!$&'*,;=@+[]";
    let mut out = String::new();
    for b in p.as_bytes() {
        let c = *b as char;
        if c.is_ascii_alphanumeric() || "_.-~".contains(c) || SAFE.contains(c) {
            out.push(c);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// `file:///x/y.jsh` → `Path`。非 file: 协议或空值一律 `None`。
fn uri_to_path(uri: &str) -> Option<PathBuf> {
    if !uri.starts_with("file://") {
        return None;
    }
    let rest = &uri["file://".len()..];
    // netloc = 第一个 '/' 之前
    let (netloc, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, ""),
    };
    let mut p = percent_decode(path);
    if !netloc.is_empty() && netloc != "localhost" {
        p = format!("//{netloc}{p}");
    }
    let b: Vec<char> = p.chars().collect();
    if b.len() > 2 && b[0] == '/' && b[2] == ':' {
        p = b[1..].iter().collect();     // Windows 盘符前多一个斜杠
    }
    Some(PathBuf::from(p))
}

/// `\\?\D:\x` → `D:\x`（Rust 的 canonicalize 在 Windows 上加了这个前缀，
/// Python 的 `Path.resolve()` 不会 —— 不剥掉的话跨文件跳转的 uri 就与
/// 客户端给的对不上）。
fn strip_unc(p: PathBuf) -> PathBuf {
    let t = p.to_string_lossy().to_string();
    if let Some(rest) = t.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = t.strip_prefix(r"\\?\") {
        return PathBuf::from(rest);
    }
    p
}

fn path_to_uri(path: &Path) -> String {
    let abs = std::fs::canonicalize(path).map(strip_unc)
        .unwrap_or_else(|_| path.to_path_buf());
    let mut p = abs.to_string_lossy().replace('\\', "/");
    if !p.starts_with('/') {
        p = format!("/{p}");             // Windows: C:/x → /C:/x
    }
    format!("file://{}", quote_path(&p))
}

// ---------------------------------------------------------------------------
// 词与区间小工具
// ---------------------------------------------------------------------------

fn is_word_char(ch: char) -> bool {
    ch.is_alphanumeric() || ch == '_'
        || ('\u{3400}'..='\u{9fff}').contains(&ch)
        || ('\u{f900}'..='\u{faff}').contains(&ch)
}

/// 取光标处的「词」及其偏移区间（字符下标）。
fn word_at(text: &str, line0: i64, char0: i64) -> (String, i64, i64) {
    let cs: Vec<char> = text.chars().collect();
    let n = cs.len() as i64;
    let mut start = pos_to_offset(text, line0, char0);
    if start < n && !is_word_char(cs[start as usize])
        && start > 0 && is_word_char(cs[(start - 1) as usize])
    {
        start -= 1;
    }
    let mut left = start;
    while left > 0 && is_word_char(cs[(left - 1) as usize]) {
        left -= 1;
    }
    let mut right = start;
    while right < n && is_word_char(cs[right as usize]) {
        right += 1;
    }
    let seg: String = cs[left as usize..right as usize].iter().collect();
    (seg, left, right)
}

fn ranges_touch(a: Option<Rng>, b: Option<Rng>) -> bool {
    let (a, b) = match (a, b) {
        (Some(a), Some(b)) => (a, b),
        _ => return true,                // 没给 range：按「全都要」处理
    };
    if b.start == b.end {
        let (a0, a1) = (a.start, a.end);
        return a0.line <= b.start.line && b.start.line <= a1.line
            && (b.start.line != a0.line || a0.ch <= b.start.ch)
            && (b.start.line != a1.line || b.start.ch <= a1.ch);
    }
    !(b.end.line < a.start.line || b.start.line > a.end.line
      || (b.end.line == a.start.line && b.end.ch < a.start.ch)
      || (b.start.line == a.end.line && b.start.ch > a.end.ch))
}

/// 在定义所在行里找**名字本身**的列（0 基字符列）。
fn def_name_col(line_text: &str, name: &str, hint_col0: i64) -> i64 {
    let cs: Vec<char> = line_text.chars().collect();
    let nc: Vec<char> = name.chars().collect();
    let from = hint_col0.max(0) as usize;
    let find = |start: usize| -> Option<usize> {
        if nc.is_empty() || cs.len() < nc.len() {
            return None;
        }
        (start..=cs.len() - nc.len()).find(|&i| cs[i..i + nc.len()] == nc[..])
    };
    match find(from).or_else(|| find(0)) {
        Some(i) => i as i64,
        None => hint_col0.max(0),
    }
}

/// 是不是合法的基石标识符。
fn is_valid_name(name: &str) -> bool {
    let cs: Vec<char> = name.chars().collect();
    if cs.is_empty() {
        return false;
    }
    if cs[0].is_numeric() {
        return false;
    }
    cs.iter().all(|c| is_word_char(*c))
}

// ---------------------------------------------------------------------------
// 语言元数据（单一数据源：`langdata`）
// ---------------------------------------------------------------------------

fn lang_builtin(name: &str) -> Option<&'static str> {
    D::BUILTINS.iter().find(|(n, _)| *n == name).map(|(_, d)| *d)
}

fn lang_stdlib(module: &str)
    -> Option<&'static [(&'static str, &'static str, &'static str)]>
{
    D::STDLIB.iter().find(|(m, _)| *m == module).map(|(_, f)| *f)
}

fn lang_all_methods() -> Vec<&'static str> {
    let mut out: Vec<&'static str> = Vec::new();
    for (_, names) in D::METHODS {
        for n in *names {
            if !out.contains(n) {
                out.push(n);
            }
        }
    }
    out
}

// ---------------------------------------------------------------------------
// AST 工具（`collect_symbols` 等）
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct Sym {
    name: String,
    kind: i64,
    container: Option<String>,
    detail: String,
    line: i64,
    col: i64,
    params: Vec<String>,
    /// 是不是 `FuncDef` / `ClassDef`（重命名时名字不在 AST 位置上，要另找）
    named_def: bool,
}

fn sf(n: &Node, key: &str) -> Option<String> {
    scope::s_field_node(n, key)
}

fn nodes_field<'a>(n: &'a Node, key: &str) -> Vec<&'a Node> {
    n.fields.iter().find(|(k, _)| *k == key)
        .map(|(_, v)| scope::child_nodes_of_value(v))
        .unwrap_or_default()
}

/// 收集文档内定义：函数 / 类 / 变量 / 导入。
fn collect_symbols(prog: &Node) -> Vec<Sym> {
    let mut out: Vec<Sym> = Vec::new();

    fn walk(node: &Node, container: Option<&str>, out: &mut Vec<Sym>) {
        let (line, col) = scope::node_line_col(node);
        if node.kind == "FuncDef" {
            let name = sf(node, "name").unwrap_or_default();
            let params = scope::str_list_field(node, "params");
            out.push(Sym {
                name: name.clone(), kind: SYM_FUNCTION,
                container: container.map(|s| s.to_string()),
                detail: format!("({})", params.join(", ")),
                line, col, params, named_def: true,
            });
            for child in nodes_field(node, "body") {
                walk(child, Some(&name), out);
            }
            return;
        }
        if node.kind == "ClassDef" {
            let name = sf(node, "name").unwrap_or_default();
            out.push(Sym {
                name: name.clone(), kind: SYM_CLASS,
                container: container.map(|s| s.to_string()),
                detail: "类".to_string(),
                line, col, params: Vec::new(), named_def: true,
            });
            for child in nodes_field(node, "body") {
                walk(child, Some(&name), out);
            }
            return;
        }
        match node.kind {
            "Assign" => {
                if let Some(t) = node.fields.iter().find(|(k, _)| *k == "target") {
                    let flat = scope::child_nodes_of_value(&t.1);
                    for nn in flat {
                        if nn.kind == "Name" {
                            let (l, c) = scope::node_line_col(nn);
                            out.push(Sym {
                                name: scope::name_id(nn).unwrap_or_default(),
                                kind: SYM_VARIABLE,
                                container: container.map(|s| s.to_string()),
                                detail: "变量".to_string(),
                                line: l, col: c, params: Vec::new(), named_def: false,
                            });
                        }
                    }
                }
            }
            "For" => {
                if let Some(t) = node.fields.iter().find(|(k, _)| *k == "target") {
                    let mut flat: Vec<&Node> = Vec::new();
                    scope::flatten(&t.1, &mut flat);
                    for nn in flat {
                        if nn.kind == "Name" {
                            let (l, c) = scope::node_line_col(nn);
                            out.push(Sym {
                                name: scope::name_id(nn).unwrap_or_default(),
                                kind: SYM_VARIABLE,
                                container: container.map(|s| s.to_string()),
                                detail: "循环变量".to_string(),
                                line: l, col: c, params: Vec::new(), named_def: false,
                            });
                        }
                    }
                }
            }
            "Import" => {
                let nm = sf(node, "name").unwrap_or_default();
                let al = sf(node, "alias").filter(|s| !s.is_empty());
                let from_py = node.fields.iter().find(|(k, _)| *k == "from_python")
                    .map(|(_, v)| matches!(v, J::Int(1)))
                    .unwrap_or(false);
                out.push(Sym {
                    name: al.unwrap_or(nm), kind: SYM_MODULE,
                    container: container.map(|s| s.to_string()),
                    detail: if from_py { "来自 Python" } else { "模块" }.to_string(),
                    line, col, params: Vec::new(), named_def: false,
                });
            }
            _ => {}
        }
        for child in scope::child_nodes_vec(node) {
            walk(child, container, out);
        }
    }

    walk(prog, None, &mut out);
    // 同一名字在同一作用域重复出现（如反复赋值）只保留第一次
    let mut seen: HashSet<(String, i64, Option<String>)> = HashSet::new();
    let mut deduped: Vec<Sym> = Vec::new();
    for sy in out {
        let key = (sy.name.clone(), sy.kind, sy.container.clone());
        if seen.insert(key) {
            deduped.push(sy);
        }
    }
    deduped
}

/// 文档里所有**名字等于 `name` 的 Name 节点**（含解包目标里的名字）。
fn name_refs(prog: &Node, name: &str) -> Vec<(i64, i64)> {
    let mut out: Vec<(i64, i64)> = Vec::new();
    scope::preorder_pub(prog, &mut |node| {
        if node.kind == "Name" && scope::name_id(node).as_deref() == Some(name) {
            out.push(scope::node_line_col(node));
        }
    });
    out
}

/// 所有「绑定位置」的 `(行, 列)`（1 基）——语义高亮标 `declaration` 用。
fn binding_positions(prog: Option<&Node>) -> HashSet<(i64, i64)> {
    let mut out: HashSet<(i64, i64)> = HashSet::new();
    let Some(prog) = prog else { return out };
    let marks = scope::binding_marks(prog);
    scope::preorder_pub(prog, &mut |node| {
        if node.kind == "Name" && marks.contains(&scope::pid(node)) {
            out.insert(scope::node_line_col(node));
        }
    });
    out
}

/// 形参在源码里的位置（`函数 甲(甲, 乙)：` 里那两个名字）。
fn param_positions(tokens: &[tokenizer::Tok]) -> HashSet<(i64, i64)> {
    let mut out: HashSet<(i64, i64)> = HashSet::new();
    let (mut 待函数名, mut 等左括号, mut 深度) = (false, false, 0i64);
    for t in tokens {
        let v = match &t.value {
            tokenizer::Value::Str(x) => x.clone(),
            tokenizer::Value::Int(i) => i.to_string(),
            _ => String::new(),
        };
        if 待函数名 {
            if t.kind == "KEYWORD" {
                continue;
            }
            if t.kind == "NAME" {
                待函数名 = false;
                等左括号 = true;
                continue;
            }
            待函数名 = false;
            等左括号 = false;
            continue;
        }
        if t.kind == "OP" {
            if 等左括号 {
                if v == "(" {
                    深度 = 1;
                    等左括号 = false;
                    continue;
                }
                等左括号 = false;
                continue;
            }
            if 深度 > 0 {
                if v == "(" {
                    深度 += 1;
                } else if v == ")" {
                    深度 -= 1;
                }
            }
            continue;
        }
        if 深度 > 0 {
            if t.kind == "NAME" {
                out.insert((t.line as i64, t.col as i64));
            }
            continue;
        }
        if t.kind == "KEYWORD" && v == "函数" {
            待函数名 = true;
        }
    }
    out
}

// ---------------------------------------------------------------------------
// 签名提示的小工具
// ---------------------------------------------------------------------------

fn sig_from_params(name: &str, params: &[String], detail: &str) -> Jv {
    let 标签 = format!("{name}({})", params.join(", "));
    let mut 出: Vec<Jv> = Vec::new();
    let mut at = name.chars().count() as i64 + 1;
    for p in params {
        let n = p.chars().count() as i64;
        出.push(obj(vec![
            ("label", arr(vec![Jv::Int(at), Jv::Int(at + n)])),
            ("documentation", s("")),
        ]));
        at += n + 2;
    }
    obj(vec![
        ("label", s(标签)),
        ("parameters", arr(出)),
        ("documentation", obj(vec![("kind", s("markdown")),
                                   ("value", s(detail))])),
    ])
}

/// 从「`名字(甲, 乙)：说明`」这类文本里抠出签名。
fn sig_from_text(first_line: &str, full: &str) -> Option<Jv> {
    let text = first_line.trim();
    let open = text.find('(')?;
    let close = text.find(')')?;
    if close < open {
        return None;
    }
    let 名字 = text[..open].trim();
    if 名字.is_empty() {
        return None;
    }
    let 参数段 = &text[open + 1..close];
    let params: Vec<String> = 参数段.split(',')
        .map(|p| p.trim().to_string()).filter(|p| !p.is_empty()).collect();
    let 尾 = &text[close + 1..];
    let 说明 = if 尾.trim_matches(|c| c == ' ' || c == '：' || c == ':').is_empty() {
        full.trim().to_string()
    } else {
        尾.trim_matches(|c| c == ' ' || c == '：' || c == ':').to_string()
    };
    Some(sig_from_params(名字, &params, &说明))
}

/// 光标处的调用上下文 → `(被调用的名字, 第几个实参)`。
fn call_context(text: &str, offset: i64) -> Option<(String, i64)> {
    let cs: Vec<char> = text.chars().collect();
    let n = cs.len() as i64;
    let mut depth: i64 = 0;
    let mut i: i64 = offset.min(n) - 1;
    let mut quote: Option<char> = None;
    let mut found = false;
    while i >= 0 {
        let ch = cs[i as usize];
        if let Some(q) = quote {
            if ch == q && (i == 0 || cs[(i - 1) as usize] != '\\') {
                quote = None;
            }
            i -= 1;
            continue;
        }
        if ch == '"' || ch == '\'' {
            quote = Some(ch);
            i -= 1;
            continue;
        }
        if ch == ')' || ch == ']' || ch == '}' {
            depth += 1;
        } else if ch == '(' {
            if depth == 0 {
                found = true;
                break;
            }
            depth -= 1;
        } else if ch == '[' || ch == '{' {
            if depth > 0 {
                depth -= 1;
            }
        }
        i -= 1;
    }
    if !found {
        return None;
    }
    let open_at = i;
    // 往前读被调用者：标识符（可带 `.` 分段）
    let mut j = open_at - 1;
    while j >= 0 && (cs[j as usize] == ' ' || cs[j as usize] == '\t') {
        j -= 1;
    }
    let end = j + 1;
    while j >= 0 && (is_word_char(cs[j as usize]) || cs[j as usize] == '.') {
        j -= 1;
    }
    let callee: String = cs[(j + 1) as usize..end as usize].iter().collect();
    if callee.is_empty() || callee.starts_with('.') || callee.ends_with('.') {
        return None;
    }
    // 数顶层逗号
    let mut depth: i64 = 0;
    let mut 逗号: i64 = 0;
    let mut quote: Option<char> = None;
    let mut k = open_at + 1;
    while k < offset {
        let ch = cs[k as usize];
        if let Some(q) = quote {
            if ch == q && cs[(k - 1) as usize] != '\\' {
                quote = None;
            }
        } else if ch == '"' || ch == '\'' {
            quote = Some(ch);
        } else if ch == '(' || ch == '[' || ch == '{' {
            depth += 1;
        } else if ch == ')' || ch == ']' || ch == '}' {
            depth -= 1;
        } else if ch == ',' && depth == 0 {
            逗号 += 1;
        }
        k += 1;
    }
    Some((callee, 逗号))
}

// ---------------------------------------------------------------------------
// 服务器
// ---------------------------------------------------------------------------

struct Doc {
    text: String,
    version: Option<crate::json::Json>,
    /// 最近一次**成功**分词的结果（词法错时保持上一次的 —— 与 Python 的
    /// `IncrementalDocument` 同口径：失败时 tokens 不更新、text 更新）。
    tokens: Vec<tokenizer::Tok>,
}

#[derive(Clone)]
struct Fix {
    range: Option<Rng>,
    new_text: String,
}

#[derive(Clone)]
struct CacheEntry {
    mtime: Option<std::time::SystemTime>,
    symbols: Vec<Sym>,
}

pub struct LspServer {
    docs: HashMap<String, Doc>,
    symbols: HashMap<String, Vec<Sym>>,
    diagnostics: HashMap<String, Vec<Jv>>,
    fixes: HashMap<String, Vec<Fix>>,
    programs: HashMap<String, Option<Node>>,
    root: Option<PathBuf>,
    file_cache: HashMap<String, CacheEntry>,
    stopped: bool,
    out: String,
}

impl LspServer {
    pub fn new() -> Self {
        LspServer {
            docs: HashMap::new(),
            symbols: HashMap::new(),
            diagnostics: HashMap::new(),
            fixes: HashMap::new(),
            programs: HashMap::new(),
            root: None,
            file_cache: HashMap::new(),
            stopped: false,
            out: String::new(),
        }
    }

    pub fn stopped(&self) -> bool {
        self.stopped
    }

    // -- 传输 ---------------------------------------------------------------

    fn write(&mut self, obj: Jv) {
        let data = dump_compact(&obj);
        self.out.push_str(&format!("Content-Length: {}\r\n\r\n", data.as_bytes().len()));
        self.out.push_str(&data);
    }

    fn reply(&mut self, id: &crate::json::Json, result: Jv) {
        self.write(obj(vec![
            ("jsonrpc", s("2.0")),
            ("id", json_to_jv(id)),
            ("result", result),
        ]));
    }

    fn reply_err(&mut self, id: &crate::json::Json, code: i64, message: String) {
        self.write(obj(vec![
            ("jsonrpc", s("2.0")),
            ("id", json_to_jv(id)),
            ("error", obj(vec![("code", Jv::Int(code)), ("message", s(message))])),
        ]));
    }

    fn notify(&mut self, method: &str, params: Jv) {
        self.write(obj(vec![
            ("jsonrpc", s("2.0")),
            ("method", s(method)),
            ("params", params),
        ]));
    }

    // -- 主循环 -------------------------------------------------------------

    /// 处理一条消息，返回本次发出的报文文本（测试可直接调用，不必走 stdio）。
    pub fn dispatch(&mut self, msg: &crate::json::Json) -> String {
        self.out.clear();
        let method = match msg.get("method").and_then(|m| m.as_str()) {
            Some(m) => m.to_string(),
            None => return String::new(),      // 响应类消息：忽略
        };
        let id = msg.get("id");
        let params = msg.get("params");
        let empty = crate::json::Json::Obj(Default::default());
        let params = params.unwrap_or(&empty);

        // 未知方法：有 id 才回错（通知就静默）
        let known = HANDLERS.contains(&method.as_str());
        if !known {
            if let Some(id) = id {
                let m = format!("未实现的方法：{method}");
                self.reply_err(id, METHOD_NOT_FOUND, m);
            }
            return std::mem::take(&mut self.out);
        }

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.handle(&method, params)
        }));
        match result {
            Ok(v) => {
                if let Some(id) = id {
                    self.reply(id, v);
                }
            }
            Err(_) => {
                if let Some(id) = id {
                    self.reply_err(id, INTERNAL_ERROR, "内部错误：已捕获".to_string());
                }
            }
        }
        std::mem::take(&mut self.out)
    }

    fn handle(&mut self, method: &str, params: &crate::json::Json) -> Jv {
        match method {
            "initialize" => self.on_initialize(params),
            "initialized" => Jv::Null,
            "shutdown" => Jv::Null,
            "exit" => {
                self.stopped = true;
                Jv::Null
            }
            "textDocument/didOpen" => {
                self.on_did_open(params);
                Jv::Null
            }
            "textDocument/didChange" => {
                self.on_did_change(params);
                Jv::Null
            }
            "textDocument/didClose" => {
                self.on_did_close(params);
                Jv::Null
            }
            "textDocument/didSave" => {
                self.on_did_save(params);
                Jv::Null
            }
            "textDocument/completion" => self.on_completion(params),
            "textDocument/hover" => self.on_hover(params),
            "textDocument/definition" => self.on_definition(params),
            "textDocument/documentSymbol" => self.on_document_symbol(params),
            "textDocument/formatting" => self.on_formatting(params),
            "textDocument/prepareRename" => self.on_prepare_rename(params),
            "textDocument/rename" => self.on_rename(params),
            "textDocument/codeAction" => self.on_code_action(params),
            "textDocument/signatureHelp" => self.on_signature_help(params),
            "workspace/symbol" => self.on_workspace_symbol(params),
            "textDocument/semanticTokens/full" => self.on_semantic_tokens_full(params),
            _ => Jv::Null,
        }
    }

    // -- 生命周期 -----------------------------------------------------------

    fn on_initialize(&mut self, params: &crate::json::Json) -> Jv {
        self.root = self.workspace_root(params);
        let caps = obj(vec![
            ("textDocumentSync", obj(vec![
                ("openClose", Jv::Bool(true)),
                ("change", Jv::Int(SYNC_INCREMENTAL)),
                ("save", Jv::Bool(true)),
            ])),
            ("completionProvider", obj(vec![
                ("triggerCharacters", arr(vec![s(".")])),
            ])),
            ("hoverProvider", Jv::Bool(true)),
            ("definitionProvider", Jv::Bool(true)),
            ("documentSymbolProvider", Jv::Bool(true)),
            ("documentFormattingProvider", Jv::Bool(true)),
            ("renameProvider", obj(vec![("prepareProvider", Jv::Bool(true))])),
            ("codeActionProvider", obj(vec![
                ("codeActionKinds", arr(vec![s(CODE_ACTION_QUICKFIX),
                                             s(CODE_ACTION_SOURCE)])),
            ])),
            ("signatureHelpProvider", obj(vec![
                ("triggerCharacters", arr(vec![s("("), s("，"), s(",")])),
            ])),
            ("workspaceSymbolProvider", Jv::Bool(true)),
            ("semanticTokensProvider", obj(vec![
                ("legend", obj(vec![
                    ("tokenTypes", arr(SEMANTIC_TYPES.iter().map(|x| s(*x)).collect())),
                    ("tokenModifiers", arr(SEMANTIC_MODIFIERS.iter().map(|x| s(*x)).collect())),
                ])),
                ("full", Jv::Bool(true)),
            ])),
        ]);
        obj(vec![
            ("capabilities", caps),
            ("serverInfo", obj(vec![("name", s("jishi-lsp")),
                                    ("version", s(D::LANG_VERSION))])),
        ])
    }

    fn workspace_root(&self, params: &crate::json::Json) -> Option<PathBuf> {
        if let Some(folders) = pget(params, "workspaceFolders").and_then(|f| f.as_array()) {
            for f in folders {
                let p = uri_to_path(&pstr(pget(f, "uri")));
                if p.is_some() {
                    return p;
                }
            }
        }
        if let Some(p) = uri_to_path(&pstr(pget(params, "rootUri"))) {
            return Some(p);
        }
        if let Some(rp) = pget(params, "rootPath").and_then(|x| x.as_str()) {
            if !rp.is_empty() {
                return Some(PathBuf::from(rp));
            }
        }
        std::env::current_dir().ok()
    }

    // -- 文档同步 -----------------------------------------------------------

    fn on_did_open(&mut self, params: &crate::json::Json) {
        let doc = match pget(params, "textDocument") {
            Some(d) => d,
            None => return,
        };
        let uri = pstr(pget(doc, "uri"));
        let text = pstr(pget(doc, "text"));
        // ⚠️ **词法错时整个 didOpen 会失败、文档根本不注册**（与 Python 侧
        // `IncrementalDocument(text, uri)` 在构造里就分词、失败即抛出同口径）。
        // 这不是「顺手修一修」的地方 —— 照抄才叫逐字一致。
        let toks = match tokenizer::tokenize(&text, &uri) {
            Ok(o) => o.tokens,
            Err(_) => return,
        };
        self.docs.insert(uri.clone(), Doc {
            text,
            version: pget(doc, "version").cloned(),
            tokens: toks,
        });
        self.analyze(&uri);
    }

    fn on_did_change(&mut self, params: &crate::json::Json) {
        let uri = uri_of(params);
        let changes = pget(params, "contentChanges").and_then(|c| c.as_array())
            .cloned().unwrap_or_default();
        let Some(doc) = self.docs.get_mut(&uri) else { return };
        if changes.is_empty() {
            return;
        }
        let version = pget(params, "textDocument").and_then(|d| pget(d, "version"));
        let cur = doc.version.as_ref().and_then(|v| v.as_i64());
        if let (Some(v), Some(c)) = (version.and_then(|x| x.as_i64()), cur) {
            if v <= c {
                return;                       // 旧版本/重复版本：不覆盖新文本
            }
        }
        let mut text = doc.text.clone();
        for ch in &changes {
            text = apply_change(&text, ch);
        }
        doc.text = text;
        doc.version = version.cloned();
        self.analyze(&uri);
    }

    fn on_did_close(&mut self, params: &crate::json::Json) {
        let uri = uri_of(params);
        self.docs.remove(&uri);
        self.symbols.remove(&uri);
        self.diagnostics.remove(&uri);
        self.programs.remove(&uri);
        self.fixes.remove(&uri);
        self.notify("textDocument/publishDiagnostics",
                    obj(vec![("uri", s(uri)), ("diagnostics", arr(vec![]))]));
    }

    fn on_did_save(&mut self, params: &crate::json::Json) {
        self.analyze(&uri_of(params));
    }

    // -- 分析与诊断 ---------------------------------------------------------

    fn analyze(&mut self, uri: &str) {
        let doc = match self.docs.get(uri) {
            Some(d) => d,
            None => return,
        };
        let text = doc.text.clone();
        let version = doc.version.clone();

        // 分词（失败则保持上一次的 tokens —— 与 `IncrementalDocument` 同口径）
        let toks_ok = tokenizer::tokenize(&text, uri).ok();
        let program = match &toks_ok {
            Some(_) => match parser::parse(&text, uri) {
                Ok(p) => Some(p),
                Err(_) => None,
            },
            None => None,
        };

        let mut diags: Vec<Jv> = Vec::new();
        match &program {
            Some(p) => {
                self.symbols.insert(uri.to_string(), collect_symbols(p));
                self.programs.insert(uri.to_string(), Some(p.clone()));
                self.fixes.insert(uri.to_string(), Vec::new());
                diags.extend(self.undefined_warnings(p));
            }
            None => {
                let (code, title, message, hint, line, col, underline, fix) =
                    match &toks_ok {
                        None => {
                            let e = tokenizer::tokenize(&text, uri).err().unwrap();
                            (e.code.to_string(),
                             crate::errcodes::title(e.code).unwrap_or("").to_string(),
                             e.message.clone(), e.hint.clone(),
                             e.line as i64, e.col as i64,
                             e.underline.map(|(a, b)| (a as i64, b as i64)), None)
                        }
                        Some(_) => {
                            let e = parser::parse(&text, uri).err().unwrap();
                            (e.code.to_string(),
                             crate::errcodes::title(e.code).unwrap_or("").to_string(),
                             e.message.clone(), e.hint.clone(),
                             e.line as i64, e.col as i64,
                             e.underline.map(|(a, b)| (a as i64, b as i64)),
                             e.fix.clone())
                        }
                    };
                let rng = range_from_error(line, col, underline);
                diags.push(obj(vec![
                    ("range", rng.jv()),
                    ("severity", Jv::Int(SEVERITY_ERROR)),
                    ("code", s(&code)),
                    ("source", s("jishi")),
                    ("message", s(error_text(&title, &message, hint.as_deref(), &code))),
                ]));
                self.symbols.insert(uri.to_string(), Vec::new());
                self.programs.insert(uri.to_string(), None);
                self.fixes.insert(uri.to_string(), quick_fixes_from(fix, rng));
            }
        }
        // 只有分词成功时才刷新 doc.tokens
        if let Some(o) = toks_ok {
            if let Some(d) = self.docs.get_mut(uri) {
                d.tokens = o.tokens;
            }
        }
        let version_jv = version.as_ref().map(json_to_jv).unwrap_or(Jv::Null);
        self.diagnostics.insert(uri.to_string(), diags.clone());
        self.notify("textDocument/publishDiagnostics",
                    obj(vec![("uri", s(uri)),
                             ("diagnostics", arr(diags)),
                             ("version", version_jv)]));
    }

    fn undefined_warnings(&self, program: &Node) -> Vec<Jv> {
        let mut known: HashSet<String> = HashSet::new();
        for (k, _) in D::KEYWORDS {
            known.insert(k.to_string());
        }
        for (n, _) in D::BUILTINS {
            known.insert(n.to_string());
        }
        for e in D::EXCEPTIONS {
            known.insert(e.to_string());
        }
        for kw in LITERAL_KEYWORDS {
            known.insert(kw.to_string());
        }
        for (m, _) in D::STDLIB {
            known.insert(m.to_string());
        }
        for n in scope::defined_names(program) {
            known.insert(n);
        }
        let mut marks = scope::binding_marks(program);
        marks.extend(scope::attr_base_marks(program));

        let mut out: Vec<Jv> = Vec::new();
        let mut seen: HashSet<(String, i64)> = HashSet::new();
        for node in scope::load_names(program, &marks) {
            let id = scope::name_id(node).unwrap_or_default();
            if known.contains(&id) {
                continue;
            }
            if id.starts_with('$') {
                continue;
            }
            let (line, col) = scope::node_line_col(node);
            if !seen.insert((id.clone(), line)) {
                continue;
            }
            out.push(obj(vec![
                ("range", Rng {
                    start: pos(line - 1, col - 1),
                    end: pos(line - 1, col - 1 + id.chars().count() as i64),
                }.jv()),
                ("severity", Jv::Int(SEVERITY_WARNING)),
                ("source", s("jishi")),
                ("message", s(format!("名字「{id}」在这份文件里没有定义过"))),
                ("relatedInformation", Jv::Null),
            ]));
        }
        out
    }

    // -- 补全 ---------------------------------------------------------------

    fn on_completion(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        let pos_p = pget(params, "position");
        let Some(doc) = self.docs.get(&uri) else {
            return obj(vec![("isIncomplete", Jv::Bool(false)), ("items", arr(vec![]))]);
        };
        let text = doc.text.clone();
        let off = pos_to_offset(&text, pint(pos_p.and_then(|p| pget(p, "line"))),
                                pint(pos_p.and_then(|p| pget(p, "character"))));
        let (dot_base, word) = completion_context(&text, off);

        let mut items: Vec<Jv> = Vec::new();
        if !dot_base.is_empty() {
            items.extend(self.attr_items(&dot_base));
        } else {
            for (name, desc) in D::KEYWORDS {
                items.push(obj(vec![
                    ("label", s(*name)), ("kind", Jv::Int(KIND_KEYWORD)),
                    ("detail", s(*desc)),
                    ("documentation", obj(vec![
                        ("kind", s("markdown")),
                        ("value", s(format!("**关键字**：{desc}"))),
                    ])),
                ]));
            }
            for (name, doc_text) in D::BUILTINS {
                items.push(obj(vec![
                    ("label", s(*name)), ("kind", Jv::Int(KIND_FUNCTION)),
                    ("detail", s(first_line(doc_text))),
                    ("documentation", obj(vec![
                        ("kind", s("markdown")),
                        ("value", s(if doc_text.is_empty() { *name } else { doc_text })),
                    ])),
                ]));
            }
            for (m, _) in D::STDLIB {
                items.push(obj(vec![
                    ("label", s(*m)), ("kind", Jv::Int(KIND_MODULE)),
                    ("detail", s("标准库模块")),
                    ("documentation", obj(vec![
                        ("kind", s("markdown")),
                        ("value", s(format!("标准库模块 `{m}`，用 `导入 {m}` 后使用"))),
                    ])),
                ]));
            }
            let syms = self.symbols.get(&uri).cloned().unwrap_or_default();
            for sym in &syms {
                items.push(obj(vec![
                    ("label", s(&sym.name)),
                    ("kind", Jv::Int(item_kind(sym.kind))),
                    ("detail", s(&sym.detail)),
                    ("documentation", obj(vec![
                        ("kind", s("markdown")),
                        ("value", s(format!("`{}{}`（本文件第 {} 行）",
                                            sym.name, sym.detail, sym.line))),
                    ])),
                ]));
            }
        }
        if !word.is_empty() {
            items.retain(|it| {
                match it { Jv::Obj(kv) => kv.iter().find(|(k, _)| k == "label")
                    .map(|(_, v)| matches!(v, Jv::Str(x) if x.starts_with(&word)))
                    .unwrap_or(false), _ => false }
            });
        }
        obj(vec![("isIncomplete", Jv::Bool(false)), ("items", arr(dedupe(items)))])
    }

    fn attr_items(&self, base: &str) -> Vec<Jv> {
        if let Some(fns) = lang_stdlib(base) {
            return fns.iter().map(|(name, sig, doc)| obj(vec![
                ("label", s(*name)), ("kind", Jv::Int(KIND_FUNCTION)),
                ("detail", s(*sig)),
                ("documentation", obj(vec![
                    ("kind", s("markdown")),
                    ("value", s(format!("`{}{}`\n\n{}",
                                        if sig.is_empty() { *name } else { *sig }, "", doc))),
                ])),
            ])).collect();
        }
        lang_all_methods().iter().map(|name| obj(vec![
            ("label", s(*name)), ("kind", Jv::Int(KIND_METHOD)),
            ("detail", s("方法")),
            ("documentation", obj(vec![
                ("kind", s("markdown")),
                ("value", s(format!("方法 `{name}`"))),
            ])),
        ])).collect()
    }

    // -- 悬停 ---------------------------------------------------------------

    fn on_hover(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        let pos_p = pget(params, "position");
        let Some(doc) = self.docs.get(&uri) else { return Jv::Null };
        let text = doc.text.clone();
        let (word, w_start, w_end) = word_at(
            &text, pint(pos_p.and_then(|p| pget(p, "line"))),
            pint(pos_p.and_then(|p| pget(p, "character"))));
        if word.is_empty() {
            return Jv::Null;
        }
        let Some(body) = self.describe(&uri, &word) else { return Jv::Null };
        obj(vec![
            ("contents", obj(vec![("kind", s("markdown")), ("value", s(body))])),
            ("range", Rng { start: offset_to_pos(&text, w_start),
                            end: offset_to_pos(&text, w_end) }.jv()),
        ])
    }

    fn describe(&self, uri: &str, word: &str) -> Option<String> {
        if let Some(d) = D::KEYWORDS.iter().find(|(k, _)| *k == word) {
            return Some(format!("**{word}**（关键字）—— {}", d.1));
        }
        if let Some(doc) = lang_builtin(word) {
            return Some(format!("**{word}**（内建函数）\n\n{doc}"));
        }
        if let Some(funcs) = lang_stdlib(word) {
            let mut lines = vec![format!("**{word}**（标准库模块）"), String::new()];
            for (name, sig, doc) in funcs.iter().take(12) {
                lines.push(format!("- `{}` — {doc}",
                                   if sig.is_empty() { *name } else { *sig }));
            }
            if funcs.len() > 12 {
                lines.push(format!("- …… 共 {} 个函数", funcs.len()));
            }
            return Some(lines.join("\n"));
        }
        if D::EXCEPTIONS.contains(&word) {
            return Some(format!("**{word}**（异常类型）"));
        }
        if let Some(syms) = self.symbols.get(uri) {
            for sym in syms {
                if sym.name == word {
                    let kind = match sym.kind {
                        SYM_FUNCTION => "函数", SYM_CLASS => "类",
                        SYM_MODULE => "模块", SYM_VARIABLE => "变量", _ => "符号",
                    };
                    return Some(format!("**{word}**（{kind}，本文件第 {} 行）\n\n```\n{}{}\n```",
                                        sym.line, word, sym.detail));
                }
            }
        }
        None
    }

    // -- 跳转定义 -----------------------------------------------------------

    fn on_definition(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        let pos_p = pget(params, "position");
        let Some(doc) = self.docs.get(&uri) else { return Jv::Null };
        let text = doc.text.clone();
        let (word, _, _) = word_at(
            &text, pint(pos_p.and_then(|p| pget(p, "line"))),
            pint(pos_p.and_then(|p| pget(p, "character"))));
        if word.is_empty() {
            return Jv::Null;
        }
        if let Some(syms) = self.symbols.get(&uri) {
            for sym in syms {
                if sym.name == word {
                    return obj(vec![
                        ("uri", s(uri)),
                        ("range", Rng {
                            start: pos(sym.line - 1, sym.col - 1),
                            end: pos(sym.line - 1,
                                     sym.col - 1 + word.chars().count() as i64),
                        }.jv()),
                    ]);
                }
            }
        }
        self.cross_file_definition(&uri, &word)
    }

    // -- 文档符号 -----------------------------------------------------------

    fn on_document_symbol(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        let syms = self.symbols.get(&uri).cloned().unwrap_or_default();
        arr(syms.iter().map(|sym| {
            let r = Rng {
                start: pos(sym.line - 1, sym.col - 1),
                end: pos(sym.line - 1, sym.col - 1 + sym.name.chars().count() as i64),
            };
            obj(vec![
                ("name", s(&sym.name)),
                ("kind", Jv::Int(sym.kind)),
                ("detail", s(&sym.detail)),
                ("range", r.jv()),
                ("selectionRange", r.jv()),
            ])
        }).collect())
    }

    // -- 格式化 -------------------------------------------------------------

    fn on_formatting(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        let Some(doc) = self.docs.get(&uri) else { return arr(vec![]) };
        let text = doc.text.clone();
        let out = match crate::formatter::format_source(&text, &uri, 4) {
            Ok(o) => o,
            Err(_) => return arr(vec![]),
        };
        if out == text {
            return arr(vec![]);
        }
        arr(vec![obj(vec![("range", whole_doc_range(&text).jv()),
                          ("newText", s(out))])])
    }

    // -- 重命名 -------------------------------------------------------------

    fn on_prepare_rename(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        let pos_p = pget(params, "position");
        let Some(doc) = self.docs.get(&uri) else { return Jv::Null };
        let text = doc.text.clone();
        let (word, w_start, w_end) = word_at(
            &text, pint(pos_p.and_then(|p| pget(p, "line"))),
            pint(pos_p.and_then(|p| pget(p, "character"))));
        if word.is_empty() || !self.renameable(&uri, &word) {
            return Jv::Null;
        }
        obj(vec![
            ("range", Rng { start: offset_to_pos(&text, w_start),
                            end: offset_to_pos(&text, w_end) }.jv()),
            ("placeholder", s(word)),
        ])
    }

    fn renameable(&self, uri: &str, word: &str) -> bool {
        if D::KEYWORDS.iter().any(|(k, _)| *k == word) || LITERAL_KEYWORDS.contains(&word) {
            return false;
        }
        if lang_builtin(word).is_some() || lang_stdlib(word).is_some()
            || D::EXCEPTIONS.contains(&word)
        {
            return false;
        }
        self.symbols.get(uri).map(|syms| syms.iter().any(|s| {
            s.name == word && s.container.is_none()
        })).unwrap_or(false)
    }

    fn on_rename(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        let new_name = pstr(pget(params, "newName")).trim().to_string();
        let pos_p = pget(params, "position");
        let Some(doc) = self.docs.get(&uri) else { return Jv::Null };
        if new_name.is_empty() {
            return Jv::Null;
        }
        let text = doc.text.clone();
        let (word, _, _) = word_at(
            &text, pint(pos_p.and_then(|p| pget(p, "line"))),
            pint(pos_p.and_then(|p| pget(p, "character"))));
        if word.is_empty() || !self.renameable(&uri, &word) {
            return Jv::Null;
        }
        if !is_valid_name(&new_name) {
            return Jv::Null;
        }
        let program = match self.programs.get(&uri) {
            Some(Some(p)) => p.clone(),
            _ => return Jv::Null,
        };
        let lines: Vec<String> = text.split('\n').map(|x| x.to_string()).collect();
        let mut edits: Vec<Jv> = Vec::new();
        let mut seen: HashSet<(i64, i64)> = HashSet::new();
        let mut 加 = |line1: i64, char_col0: i64, edits: &mut Vec<Jv>| {
            if !seen.insert((line1, char_col0)) {
                return;
            }
            let line_text = if line1 >= 1 && line1 <= lines.len() as i64 {
                lines[(line1 - 1) as usize].clone()
            } else {
                String::new()
            };
            let start = utf16_col(&line_text, char_col0);
            edits.push(obj(vec![
                ("range", Rng {
                    start: pos(line1 - 1, start),
                    end: pos(line1 - 1, start + utf16_len(&word)),
                }.jv()),
                ("newText", s(&new_name)),
            ]));
        };

        // ① 定义处
        let syms = self.symbols.get(&uri).cloned().unwrap_or_default();
        for sym in &syms {
            if sym.name != word || sym.container.is_some() {
                continue;
            }
            if sym.named_def {
                let line_text = if sym.line >= 1 && sym.line <= lines.len() as i64 {
                    lines[(sym.line - 1) as usize].clone()
                } else {
                    String::new()
                };
                加(sym.line, def_name_col(&line_text, &word, sym.col - 1), &mut edits);
            }
        }
        // ② 所有引用（含解包目标里的名字）
        for (l, c) in name_refs(&program, &word) {
            加(l, c - 1, &mut edits);
        }

        if edits.is_empty() {
            return Jv::Null;
        }
        obj(vec![("changes", obj(vec![(uri.as_str(), arr(edits))]))])
    }

    // -- 快速修复 -----------------------------------------------------------

    fn on_code_action(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        if !self.docs.contains_key(&uri) {
            return arr(vec![]);
        }
        let rng = pget(params, "range").map(|r| Rng {
            start: Pos { line: pint(pget(r, "start").and_then(|x| pget(x, "line"))),
                         ch: pint(pget(r, "start").and_then(|x| pget(x, "character"))) },
            end: Pos { line: pint(pget(r, "end").and_then(|x| pget(x, "line"))),
                       ch: pint(pget(r, "end").and_then(|x| pget(x, "character"))) },
        });
        let only: Vec<String> = pget(params, "context").and_then(|c| pget(c, "only"))
            .and_then(|o| o.as_array())
            .map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect())
            .unwrap_or_default();

        let mut out: Vec<Jv> = Vec::new();
        if only.is_empty() || only.iter().any(|x| x == CODE_ACTION_QUICKFIX) {
            let fixes = self.fixes.get(&uri).cloned().unwrap_or_default();
            for fx in &fixes {
                if !ranges_touch(fx.range, rng) {
                    continue;
                }
                let r = fx.range.map(|r| r.jv()).unwrap_or(Jv::Null);
                let ed = obj(vec![("range", r), ("newText", s(&fx.new_text))]);
                let edit = obj(vec![("changes",
                                     obj(vec![(uri.as_str(), arr(vec![ed]))]))]);
                out.push(obj(vec![
                    ("title", s(format!("改成「{}」", fx.new_text))),
                    ("kind", s(CODE_ACTION_QUICKFIX)),
                    ("isPreferred", Jv::Bool(true)),
                    ("edit", edit),
                ]));
            }
        }
        if only.is_empty() || only.iter().any(|x| x == CODE_ACTION_SOURCE) {
            let fmt = self.on_formatting(&text_doc_params(&uri));
            let has = matches!(&fmt, Jv::Arr(a) if !a.is_empty());
            if has {
                out.push(obj(vec![
                    ("title", s("格式化本文档")),
                    ("kind", s(CODE_ACTION_SOURCE)),
                    ("edit", obj(vec![("changes",
                                       obj(vec![(uri.as_str(), fmt)]))])),
                ]));
            }
        }
        arr(out)
    }

    // -- 签名提示 -----------------------------------------------------------

    fn on_signature_help(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        let pos_p = pget(params, "position");
        let Some(doc) = self.docs.get(&uri) else { return Jv::Null };
        let text = doc.text.clone();
        let off = pos_to_offset(&text, pint(pos_p.and_then(|p| pget(p, "line"))),
                                pint(pos_p.and_then(|p| pget(p, "character"))));
        let Some((callee, active)) = call_context(&text, off) else { return Jv::Null };
        let Some(sig) = self.signature_for(&uri, &callee) else { return Jv::Null };
        obj(vec![
            ("signatures", arr(vec![sig])),
            ("activeSignature", Jv::Int(0)),
            ("activeParameter", Jv::Int(active)),
        ])
    }

    fn signature_for(&self, uri: &str, callee: &str) -> Option<Jv> {
        if let Some(i) = callee.rfind('.') {
            let (base, member) = (&callee[..i], &callee[i + 1..]);
            if let Some(fns) = lang_stdlib(base) {
                for (name, sig, doc) in fns {
                    if *name == member {
                        let first = if sig.is_empty() { *name } else { *sig };
                        return sig_from_text(first, doc);
                    }
                }
            }
            return None;
        }
        if let Some(syms) = self.symbols.get(uri) {
            for sym in syms {
                if sym.name == callee && sym.kind == SYM_FUNCTION {
                    return Some(sig_from_params(callee, &sym.params, &sym.detail));
                }
            }
        }
        if let Some(params) = self.params_from_tokens(uri, callee) {
            return Some(sig_from_params(callee, &params, ""));
        }
        if let Some(doc) = lang_builtin(callee) {
            let first = doc.split('\n').next().unwrap_or("");
            return sig_from_text(first, doc);
        }
        None
    }

    fn params_from_tokens(&self, uri: &str, callee: &str) -> Option<Vec<String>> {
        let tokens = self.docs.get(uri)?.tokens.clone();
        for (i, t) in tokens.iter().enumerate() {
            let tv = match &t.value { tokenizer::Value::Str(x) => x.clone(), _ => String::new() };
            if t.kind != "KEYWORD" || tv != "函数" {
                continue;
            }
            if i + 1 >= tokens.len() || tokens[i + 1].kind != "NAME" {
                continue;
            }
            let nv = match &tokens[i + 1].value {
                tokenizer::Value::Str(x) => x.clone(), _ => String::new() };
            if nv != callee {
                continue;
            }
            let mut j = i + 2;
            if j >= tokens.len() {
                continue;
            }
            let ov = match &tokens[j].value {
                tokenizer::Value::Str(x) => x.clone(), _ => String::new() };
            if tokens[j].kind != "OP" || ov != "(" {
                continue;
            }
            j += 1;
            let mut params: Vec<String> = Vec::new();
            let mut 星 = String::new();
            while j < tokens.len() {
                let tk = &tokens[j];
                let v = match &tk.value {
                    tokenizer::Value::Str(x) => x.clone(), _ => String::new() };
                if tk.kind == "OP" && v == ")" {
                    return Some(params);
                }
                if tk.kind == "OP" && (v == "*" || v == "**") {
                    星 = v;
                    j += 1;
                    continue;
                }
                if tk.kind == "OP" && v == "," {
                    星 = String::new();
                    j += 1;
                    continue;
                }
                if tk.kind == "NAME" {
                    params.push(format!("{星}{v}"));
                    星 = String::new();
                }
                j += 1;
            }
            return Some(params);
        }
        None
    }

    // -- 工作区符号与跨文件跳转 ---------------------------------------------

    fn on_workspace_symbol(&mut self, params: &crate::json::Json) -> Jv {
        let query = pstr(pget(params, "query")).trim().to_lowercase();
        let mut out: Vec<Jv> = Vec::new();
        for (uri, syms) in self.symbols.clone() {
            for sym in &syms {
                out.push(workspace_symbol_item(sym, &uri));
            }
        }
        for (path, info) in self.workspace_index() {
            let u = path_to_uri(Path::new(&path));
            if self.docs.contains_key(&u) {
                continue;                    // 已打开的用内存里那份
            }
            for sym in &info.symbols {
                out.push(workspace_symbol_item(sym, &u));
            }
        }
        if !query.is_empty() {
            out.retain(|it| matches!(it, Jv::Obj(kv)
                if kv.iter().find(|(k, _)| k == "name")
                    .map(|(_, v)| matches!(v, Jv::Str(x) if x.to_lowercase().contains(&query)))
                    .unwrap_or(false)));
        }
        out.truncate(200);
        arr(out)
    }

    fn workspace_index(&mut self) -> Vec<(String, CacheEntry)> {
        let Some(root) = self.root.clone() else { return Vec::new() };
        if !root.is_dir() {
            return Vec::new();
        }
        let mut out: Vec<(String, CacheEntry)> = Vec::new();
        let mut count = 0;
        for p in crate::formatter::collect_jsh_sorted(&root) {
            if count >= 400 {
                break;
            }
            let parts: HashSet<String> = p.components()
                .map(|c| c.as_os_str().to_string_lossy().to_string()).collect();
            if parts.contains(".jishi") || parts.contains("node_modules") {
                continue;
            }
            let rel = p.strip_prefix(&root).unwrap_or(&p);
            let rel_parts: Vec<String> = rel.components()
                .map(|c| c.as_os_str().to_string_lossy().to_string()).collect();
            if rel_parts.len() > 1 && rel_parts[..rel_parts.len() - 1].iter()
                .any(|seg| seg.starts_with('.') && seg != "." && seg != "..")
            {
                continue;
            }
            let Ok(st) = std::fs::metadata(&p) else { continue };
            if st.len() > 512 * 1024 {
                continue;
            }
            count += 1;
            let key = p.to_string_lossy().to_string();
            let mtime = st.modified().ok();
            if let Some(cached) = self.file_cache.get(&key) {
                if cached.mtime == mtime {
                    out.push((key, cached.clone()));
                    continue;
                }
            }
            let syms = match std::fs::read_to_string(&p) {
                Ok(text) => match parser::parse(&text, &key) {
                    Ok(prog) => collect_symbols(&prog),
                    Err(_) => Vec::new(),
                },
                Err(_) => Vec::new(),
            };
            let info = CacheEntry { mtime, symbols: syms };
            self.file_cache.insert(key.clone(), info.clone());
            out.push((key, info));
        }
        out
    }

    fn cross_file_definition(&mut self, uri: &str, word: &str) -> Jv {
        let mut hits: Vec<(String, Sym)> = Vec::new();
        for (path, info) in self.workspace_index() {
            if path_to_uri(Path::new(&path)) == uri {
                continue;
            }
            for sym in &info.symbols {
                if sym.name == word && sym.container.is_none() {
                    hits.push((path.clone(), sym.clone()));
                    break;
                }
            }
        }
        if hits.len() != 1 {
            return Jv::Null;
        }
        let (path, sym) = &hits[0];
        obj(vec![
            ("uri", s(path_to_uri(Path::new(path)))),
            ("range", Rng {
                start: pos(sym.line - 1, sym.col - 1),
                end: pos(sym.line - 1, sym.col - 1 + sym.name.chars().count() as i64),
            }.jv()),
        ])
    }

    // -- 语义高亮 -----------------------------------------------------------

    fn on_semantic_tokens_full(&mut self, params: &crate::json::Json) -> Jv {
        let uri = uri_of(params);
        let Some(doc) = self.docs.get(&uri) else { return obj(vec![("data", arr(vec![]))]) };
        let data = self.semantic_data(&uri, &doc.text.clone(), &doc.tokens.clone());
        obj(vec![("data", arr(data.into_iter().map(Jv::Int).collect()))])
    }

    fn semantic_data(&self, uri: &str, text: &str, tokens: &[tokenizer::Tok]) -> Vec<i64> {
        let lines: Vec<&str> = text.split('\n').collect();
        let program = self.programs.get(uri).and_then(|p| p.as_ref());

        let mut params: HashSet<String> = HashSet::new();
        let mut funcs: HashSet<String> = HashSet::new();
        let mut classes: HashSet<String> = HashSet::new();
        let mut modules: HashSet<String> = HashSet::new();
        let mut variables: HashSet<String> = HashSet::new();
        if let Some(syms) = self.symbols.get(uri) {
            for sym in syms {
                for p in &sym.params {
                    if !p.is_empty() {
                        params.insert(p.clone());
                    }
                }
                match sym.kind {
                    SYM_FUNCTION => { funcs.insert(sym.name.clone()); }
                    SYM_CLASS => { classes.insert(sym.name.clone()); }
                    SYM_MODULE => { modules.insert(sym.name.clone()); }
                    _ => { variables.insert(sym.name.clone()); }
                }
            }
        }
        let mut binds = binding_positions(program);
        binds.extend(param_positions(tokens));

        let mut raw: Vec<(i64, i64, i64, i64, i64)> = Vec::new();
        let mut prev_op: Option<String> = None;
        let mut prev_kw: Option<String> = None;
        for t in tokens {
            let pv = match &t.value { tokenizer::Value::Str(x) => x.clone(), _ => String::new() };
            if t.kind == "OP" {
                prev_op = Some(pv);
                prev_kw = None;
                continue;
            }
            if t.kind != "NAME" && t.kind != "KEYWORD" {
                prev_op = None;
                prev_kw = None;
                continue;
            }
            let name = pv;
            let (ty, 修饰): (&str, i64) = if t.kind == "KEYWORD" {
                let m = if LITERAL_KEYWORDS.contains(&name.as_str()) {
                    sem_mod("readonly")
                } else {
                    0
                };
                prev_kw = Some(name);
                ("keyword", m)
            } else {
                let mut m: i64 = 0;
                // **用户绑定的名字优先于标准库/内建**（M42）：`令 参数 = 1`
                // 之后 `参数` 指的是那个变量，不是标准库模块。顺序反了就会
                // 把用户变量上成模块色。
                let ty = if prev_op.as_deref() == Some(".") {
                    "property"
                } else if LITERAL_KEYWORDS.contains(&name.as_str()) {
                    m |= sem_mod("readonly");
                    "keyword"
                } else if D::KEYWORDS.iter().any(|(k, _)| *k == name) {
                    "keyword"
                } else if params.contains(&name) {
                    "parameter"
                } else if funcs.contains(&name) || classes.contains(&name) {
                    "function"
                } else if modules.contains(&name) {
                    if lang_stdlib(&name).is_some() {
                        m |= sem_mod("defaultLibrary");
                    }
                    "namespace"
                } else if variables.contains(&name) {
                    "variable"
                } else if lang_stdlib(&name).is_some() {
                    m |= sem_mod("defaultLibrary");
                    "namespace"
                } else if lang_builtin(&name).is_some() {
                    m |= sem_mod("defaultLibrary");
                    "function"
                } else {
                    // 未定义的名字也按变量上色：语义高亮管的是「这是什么」。
                    "variable"
                };
                if prev_kw.as_deref() == Some("函数") || prev_kw.as_deref() == Some("类") {
                    m |= sem_mod("declaration");
                }
                if binds.contains(&(t.line as i64, t.col as i64)) {
                    m |= sem_mod("declaration");
                }
                prev_kw = None;
                (ty, m)
            };
            prev_op = None;
            let line_text = if t.line >= 1 && t.line <= lines.len() {
                lines[t.line - 1]
            } else {
                ""
            };
            let start = utf16_col(line_text, t.col as i64 - 1);
            let end = utf16_col(line_text, t.end_col as i64);
            let length = end - start;
            if length <= 0 {
                continue;
            }
            raw.push((t.line as i64 - 1, start, length, sem_type(ty), 修饰));
        }

        raw.sort_by_key(|x| (x.0, x.1));
        let mut data: Vec<i64> = Vec::new();
        let (mut prev_line, mut prev_char) = (0i64, 0i64);
        for (line, char, length, ttype, mods) in raw {
            let dchar = if line == prev_line { char - prev_char } else { char };
            data.extend([line - prev_line, dchar, length, ttype, mods]);
            prev_line = line;
            prev_char = char;
        }
        data
    }
}

impl Default for LspServer {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// 自由函数（不依赖状态的那部分）
// ---------------------------------------------------------------------------

const HANDLERS: &[&str] = &[
    "initialize", "initialized", "shutdown", "exit",
    "textDocument/didOpen", "textDocument/didChange", "textDocument/didClose",
    "textDocument/didSave", "textDocument/completion", "textDocument/hover",
    "textDocument/definition", "textDocument/documentSymbol",
    "textDocument/formatting", "textDocument/prepareRename",
    "textDocument/rename", "textDocument/codeAction",
    "textDocument/signatureHelp", "workspace/symbol",
    "textDocument/semanticTokens/full",
];

fn error_text(title: &str, message: &str, hint: Option<&str>, code: &str) -> String {
    let mut parts: Vec<String> = vec![title.to_string()];
    if !message.is_empty() {
        parts.push(message.to_string());
    }
    let text = parts.join("：");
    let mut out = format!("[{code}] {text}");
    if let Some(h) = hint {
        out.push_str(&format!("\n提示：{h}"));
    }
    out
}

fn quick_fixes_from(fix: Option<(String, String)>, rng: Rng) -> Vec<Fix> {
    let Some((old, new)) = fix else { return Vec::new() };
    if old.is_empty() || old == new {
        return Vec::new();
    }
    vec![Fix { range: Some(rng), new_text: new }]
}

fn apply_change(text: &str, change: &crate::json::Json) -> String {
    let Some(new_text) = pget(change, "text").and_then(|t| t.as_str()) else {
        return text.to_string();
    };
    let Some(rng) = pget(change, "range") else {
        return new_text.to_string();
    };
    let a = pos_to_offset(text,
                          pint(pget(rng, "start").and_then(|x| pget(x, "line"))),
                          pint(pget(rng, "start").and_then(|x| pget(x, "character"))));
    let b = pos_to_offset(text,
                          pint(pget(rng, "end").and_then(|x| pget(x, "line"))),
                          pint(pget(rng, "end").and_then(|x| pget(x, "character"))));
    let (a, b) = if b < a { (b, a) } else { (a, b) };
    let cs: Vec<char> = text.chars().collect();
    let mut out: String = cs[..a as usize].iter().collect();
    out.push_str(new_text);
    out.extend(cs[b as usize..].iter());
    out
}

fn completion_context(text: &str, off: i64) -> (String, String) {
    let cs: Vec<char> = text.chars().collect();
    let n = cs.len() as i64;
    let mut p = off;
    while p > 0 && is_word_char(cs[(p - 1) as usize]) {
        p -= 1;
    }
    let mut prefix: String = cs[p as usize..off.max(0).min(n) as usize].iter().collect();

    let mut dot_at: i64 = -1;
    if p > 0 && cs[(p - 1) as usize] == '.' {
        dot_at = p - 1;
    } else if off > 0 && cs[(off - 1) as usize] == '.' {
        dot_at = off - 1;
        prefix = String::new();
    } else if off < n && cs[off as usize] == '.' {
        dot_at = off;
        prefix = String::new();
    }
    if dot_at < 0 {
        return (String::new(), prefix);
    }
    let mut q = dot_at;
    while q > 0 && is_word_char(cs[(q - 1) as usize]) {
        q -= 1;
    }
    (cs[q as usize..dot_at as usize].iter().collect(), prefix)
}

fn dedupe(items: Vec<Jv>) -> Vec<Jv> {
    let mut seen: HashSet<String> = HashSet::new();
    let mut out: Vec<Jv> = Vec::new();
    for it in items {
        let label = match &it {
            Jv::Obj(kv) => kv.iter().find(|(k, _)| k == "label")
                .map(|(_, v)| match v { Jv::Str(x) => x.clone(), _ => String::new() })
                .unwrap_or_default(),
            _ => String::new(),
        };
        if seen.insert(label) {
            out.push(it);
        }
    }
    out
}

fn first_line(text: &str) -> String {
    text.trim().split('\n').next().unwrap_or("").to_string()
}

fn item_kind(sym_kind: i64) -> i64 {
    if sym_kind == SYM_FUNCTION {
        return KIND_FUNCTION;
    }
    if sym_kind == SYM_CLASS {
        return KIND_CLASS;
    }
    if sym_kind == SYM_MODULE {
        return KIND_MODULE;
    }
    KIND_VARIABLE
}

fn workspace_symbol_item(sym: &Sym, uri: &str) -> Jv {
    obj(vec![
        ("name", s(&sym.name)),
        ("kind", Jv::Int(sym.kind)),
        ("containerName", s(sym.container.clone().unwrap_or_default())),
        ("location", obj(vec![
            ("uri", s(uri)),
            ("range", Rng {
                start: pos(sym.line - 1, sym.col - 1),
                end: pos(sym.line - 1, sym.col - 1 + sym.name.chars().count() as i64),
            }.jv()),
        ])),
    ])
}

// ---------------------------------------------------------------------------
// stdio 入口
// ---------------------------------------------------------------------------

/// 从字节流里读一条 LSP 报文（`Content-Length` 分帧）。
fn read_message<R: BufRead>(r: &mut R) -> Option<crate::json::Json> {
    let mut length: Option<usize> = None;
    loop {
        let mut raw = String::new();
        match r.read_line(&mut raw) {
            Ok(0) => return None,
            Ok(_) => {}
            Err(_) => return None,
        }
        let line = raw.trim();
        if line.is_empty() {
            break;
        }
        let low = line.to_ascii_lowercase();
        if let Some(rest) = low.strip_prefix("content-length:") {
            length = rest.trim().parse::<usize>().ok();
            if length.is_none() {
                return None;
            }
        }
    }
    let length = length?;
    let mut buf = vec![0u8; length];
    if r.read_exact(&mut buf).is_err() {
        return None;
    }
    let text = String::from_utf8(buf).ok()?;
    crate::json::parse(&text).ok()
}

/// `jishi-rs lsp` 入口。
pub fn run_lsp() -> i32 {
    let stdin = std::io::stdin();
    let mut r = BufReader::new(stdin.lock());
    let mut server = LspServer::new();
    let stdout = std::io::stdout();
    let mut w = stdout.lock();
    while !server.stopped() {
        let Some(msg) = read_message(&mut r) else { break };
        let text = server.dispatch(&msg);
        if !text.is_empty() {
            let _ = w.write_all(text.as_bytes());
            let _ = w.flush();
        }
    }
    0
}
