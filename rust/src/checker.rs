//! 源码静态检查（R7.1-c，2026-10-05）—— `jishi/checker.py` + `jishi/scope.py` 的移植。
//!
//! 不运行程序，只看源码能看出什么问题。四条规则（与 Python 侧逐字对齐）：
//!
//! | 问题码 | 名字 | 说明 |
//! |---|---|---|
//! | `name.undefined` | 未定义名 | 与编辑器实时诊断**同一条规则**（`scope.py`，不另写一份） |
//! | `name.shadow` | 遮蔽内建 | 模块级的绑定撞上**内建函数 / 异常类型** |
//! | `name.unused` | 未使用变量 | 函数里绑定过、全文件从未读取 |
//! | `compare.self` | 可疑相等 | `甲 == 甲`、`甲 是 甲` 这类恒真（或恒假）比较 |
//!
//! ## 一切取「保守口径」（照抄 Python 侧的结论，别自作聪明）
//!
//! 静态检查一旦误报，用户就会学会忽略它——那还不如不检查。所以每条都留余量、
//! 宁可漏报：未定义名「任一作用域绑定过就不报」、属性基址不参与判断（包依赖是
//! 运行时注入的，静态看不到）、`$` / `__` 开头的内部名跳过；遮蔽内建只认
//! **内建函数与异常类型**、只报**模块级**变量、跳过导入名/形参/循环变量/推导式
//! 变量/捕获名；未使用变量只查函数体内、且只要文件里被读过任何一次就不报。
//!
//! 📌 这些「刻意不开的门」在 Python 侧都是**误报实测**换来的（第一版把标准库
//! 模块名也算进遮蔽、也在语料上报了 33 条，逐条判定全是误报）。所以移植时
//! **一条都不许放宽**，否则用户面前立刻多出一堆噪音。
//!
//! ## 为什么用「指针」当节点标记
//!
//! Python 侧用 `id(node)` 记住「哪些 Name 处于绑定位置」。Rust 里没有可变对象的
//! 稳定 id，就用**节点地址**（`&Node as *const _ as usize`）—— AST 是独占持有、
//! 分析期间不移动，所以地址等价于 Python 的 `id()`。
//!
//! ## 判据
//!
//! `tests/test_m86_checker_rust.py`：与 `jishi 源码静态检查` 在**同一批文件**上
//! **逐字段对拍**（文本输出与 `--json` 两种形态都比），外加自报家底的
//! 符号表漂移检测（内建 / 关键字 / 标准库模块 / 异常类型四个集合）。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use jishi_frontend::parser::{self, J, Node};

use crate::errcodes;
use crate::scope::*;

/// 问题级别（与 LSP 诊断的两级一致）。
pub const LEVEL_ERROR: &str = "error";
pub const LEVEL_WARNING: &str = "warning";

/// 与编辑器一致的「值关键字」——任何文件都可能用，不算未定义。
const LITERAL_KEYWORDS: [&str; 3] = ["真", "假", "空"];

/// 恒真/恒假的比较运算符：同一名字在两边时一定是写错了。
const SELF_COMPARE_OPS: [&str; 4] = ["==", "!=", "是", "不是"];

/// 「遮蔽内建」只对照这两类名字。**标准库模块名故意不在此列** ——
/// `导入 随机` 就是在导入那个模块（正常用法），形参叫「路径」「日志」也太自然。
const RESERVED_KINDS: [&str; 2] = ["内建函数", "异常类型"];

/// 「遮蔽内建」不看这些绑定来源（导入名 / 形参 / 循环变量 / 推导式变量 / 捕获名）。
const SHADOW_SKIP_KINDS: [&str; 5] =
    ["导入名", "形参", "循环变量", "推导式变量", "捕获名"];

/// 目录扫描时跳过的目录名（与工作区符号索引同一口径）。
const SKIP_DIRS: [&str; 6] =
    [".jishi", ".git", ".venv", "build-env", "node_modules", "__pycache__"];

/// 语言符号表 —— 「未定义名 / 遮蔽内建」判据的输入。
///
/// ⚠️ **这四个集合必须与 Python 侧 `langdata.LangData` 的四个字段等宽**，
/// 否则同一份文件两边会给出不同的诊断。四个来源都**不是新造的**：
/// 关键字用前端分词表、内建/标准库/异常用宿主自己那几张（已被
/// `test_m51_consistency` 钉住的）表 —— 由 `--dump-check-symbols` 自报家底，
/// `test_m86` 与 Python 侧逐个比。
pub struct Symbols {
    pub keywords: HashSet<String>,
    pub builtins: HashSet<String>,
    pub stdlib: HashSet<String>,
    pub exceptions: HashSet<String>,
}

impl Symbols {
    /// 从宿主现有的表凑齐四个集合（不新抄一份）。
    pub fn from_host() -> Symbols {
        Symbols {
            keywords: jishi_frontend::tokenizer::KEYWORDS
                .iter().map(|s| s.to_string()).collect(),
            builtins: crate::builtin_names().into_iter().collect(),
            stdlib: crate::stdlib_names().into_iter().collect(),
            exceptions: crate::exception_names().into_iter()
                .map(|s| s.to_string()).collect(),
        }
    }
}

/// 一条检查结果（字段与 Python 侧 `Issue` 对齐，便于脚本统一消费）。
pub struct Issue {
    pub code: String,
    pub level: &'static str,
    pub line: i64,
    pub col: i64,
    pub message: String,
    pub hint: String,
    pub name: String,
}

impl Issue {
    /// 人类可读的一行（外加缩进的提示行）—— 逐字照 Python `Issue.format`。
    pub fn format(&self, path: &str) -> String {
        let loc = if path.is_empty() {
            format!("{}:{}", self.line, self.col)
        } else {
            format!("{path}:{}:{}", self.line, self.col)
        };
        let mut out = format!("{loc}: [{}] {}：{}", self.level, self.code,
                              self.message);
        if !self.hint.is_empty() {
            out.push_str(&format!("\n    提示：{}", self.hint));
        }
        out
    }
}

/// 编译器/解析器发射的内部名，用户写不出来，也不该报给用户。
fn is_internal(name: &str) -> bool {
    name.starts_with("__") || name.starts_with('$')
}

/// 所有位于某个函数/类**体内部**的节点（顶层语句本身不算）。
fn inside_def_ids(prog: &Node) -> HashSet<usize> {
    fn descend(n: &Node, inside: bool, ids: &mut HashSet<usize>) {
        let mut kids: Vec<&Node> = Vec::new();
        child_nodes(n, &mut kids);
        for c in kids {
            if inside {
                ids.insert(pid(c));
            }
            let nested = inside || matches!(c.kind, "FuncDef" | "ClassDef" | "Lambda");
            descend(c, nested, ids);
        }
    }
    let mut ids: HashSet<usize> = HashSet::new();
    descend(prog, false, &mut ids);
    ids
}

/// 遍历一段语句块里的节点，但**不进入嵌套的函数/类**（`_分块节点`）。
fn block_nodes<'a>(body: Vec<&'a Node>) -> Vec<&'a Node> {
    let mut out: Vec<&'a Node> = Vec::new();
    let mut stack: Vec<&'a Node> = body;
    while let Some(node) = stack.pop() {
        if matches!(node.kind, "FuncDef" | "ClassDef" | "Lambda") {
            continue;
        }
        out.push(node);
        let mut kids: Vec<&'a Node> = Vec::new();
        child_nodes(node, &mut kids);
        stack.extend(kids);
    }
    out
}

/// 所有「绑定」：`(名字, 位置节点, 种类)`。
fn all_bindings<'a>(prog: &'a Node) -> Vec<(String, &'a Node, &'static str)> {
    let mut out: Vec<(String, &Node, &'static str)> = Vec::new();
    for node in walk_nodes(prog) {
        match node.kind {
            "Assign" | "For" | "Comprehension" => {
                let kind = match node.kind {
                    "Assign" => "变量",
                    "For" => "循环变量",
                    _ => "推导式变量",
                };
                let mut flat: Vec<&Node> = Vec::new();
                bound_names(node, &mut flat);
                for b in flat {
                    if let Some(id) = name_id(b) {
                        out.push((id, b, kind));
                    }
                }
            }
            "FuncDef" => {
                if let Some(s) = node.fields.iter()
                    .find(|(k, _)| *k == "name").and_then(|(_, v)| str_at(v)) {
                    out.push((s, node, "函数名"));
                }
                for p in str_list_field(node, "params") {
                    out.push((p, node, "形参"));
                }
            }
            "ClassDef" => {
                if let Some(s) = node.fields.iter()
                    .find(|(k, _)| *k == "name").and_then(|(_, v)| str_at(v)) {
                    out.push((s, node, "类名"));
                }
            }
            "Import" => {
                let nm = node.fields.iter().find(|(k, _)| *k == "name")
                    .and_then(|(_, v)| str_at(v)).unwrap_or_default();
                let al = node.fields.iter().find(|(k, _)| *k == "alias")
                    .and_then(|(_, v)| str_at(v)).filter(|s| !s.is_empty());
                out.push((al.unwrap_or(nm), node, "导入名"));
            }
            "ExceptHandler" => {
                if let Some(s) = node.fields.iter()
                    .find(|(k, _)| *k == "name").and_then(|(_, v)| str_at(v))
                    .filter(|s| !s.is_empty()) {
                    out.push((s, node, "捕获名"));
                }
            }
            "Lambda" => {
                for p in str_list_field(node, "params") {
                    out.push((p, node, "形参"));
                }
            }
            _ => {}
        }
    }
    out
}

// ---------------------------------------------------------------------------
// 四类检查
// ---------------------------------------------------------------------------

fn undefined_names(prog: &Node, sym: &Symbols) -> Vec<Issue> {
    let mut known: HashSet<String> = HashSet::new();
    known.extend(sym.builtins.iter().cloned());
    known.extend(sym.keywords.iter().cloned());
    known.extend(sym.exceptions.iter().cloned());
    for k in LITERAL_KEYWORDS {
        known.insert(k.to_string());
    }
    known.extend(sym.stdlib.iter().cloned());
    known.extend(defined_names(prog));

    let mut marks = binding_marks(prog);
    marks.extend(attr_base_marks(prog));

    let mut out: Vec<Issue> = Vec::new();
    let mut seen: HashSet<(String, i64)> = HashSet::new();
    for node in load_names(prog, &marks) {
        let id = match name_id(node) {
            Some(s) => s,
            None => continue,
        };
        if known.contains(&id) || is_internal(&id) {
            continue;
        }
        let (line, col) = node_line_col(node);
        let key = (id.clone(), line);
        if seen.contains(&key) {
            continue;
        }
        seen.insert(key);
        out.push(Issue {
            code: "name.undefined".to_string(),
            level: LEVEL_WARNING,
            line,
            col,
            message: format!("名字「{id}」在这份文件里没有定义过"),
            hint: "要么是拼错了，要么它来自别的文件（本命令只做单文件分析）；\
                   若来自包依赖，那是运行时注入的，静态看不到。".to_string(),
            name: id,
        });
    }
    out
}

fn shadow_builtin(prog: &Node, sym: &Symbols) -> Vec<Issue> {
    let mut reserved: HashMap<String, &'static str> = HashMap::new();
    for name in &sym.builtins {
        reserved.insert(name.clone(), "内建函数");
    }
    for name in &sym.exceptions {
        reserved.entry(name.clone()).or_insert("异常类型");
    }

    let inside = inside_def_ids(prog);
    let mut out: Vec<Issue> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for (name, node, kind) in all_bindings(prog) {
        let Some(rk) = reserved.get(&name) else { continue };
        if seen.contains(&name) || is_internal(&name) {
            continue;
        }
        if !RESERVED_KINDS.contains(rk) || SHADOW_SKIP_KINDS.contains(&kind) {
            continue;
        }
        if kind == "变量" && inside.contains(&pid(node)) {
            continue;   // 函数里的局部变量：只影响那个函数，不报
        }
        seen.insert(name.clone());
        let (line, col) = node_line_col(node);
        out.push(Issue {
            code: "name.shadow".to_string(),
            level: LEVEL_WARNING,
            line,
            col,
            message: format!("{kind}「{name}」遮蔽了{rk}「{name}」"),
            hint: format!("这个作用域里原来的「{name}」就被挡住了——换个名字（如\
                           「我的{name}」）能避免后面的人看错。"),
            name,
        });
    }
    out
}

fn unused_vars(prog: &Node) -> Vec<Issue> {
    // 「读取」的口径**只排除绑定位置**（不排除属性基址）：`表.追加(1)` 算使用。
    let marks = binding_marks(prog);
    let used: HashSet<String> = load_names(prog, &marks).iter()
        .filter_map(|n| name_id(n)).collect();

    let mut out: Vec<Issue> = Vec::new();
    let funcs: Vec<&Node> = walk_nodes(prog).into_iter()
        .filter(|n| n.kind == "FuncDef").collect();
    for f in funcs {
        let fname = f.fields.iter().find(|(k, _)| *k == "name")
            .and_then(|(_, v)| str_at(v)).unwrap_or_default();
        let body: Vec<&Node> = match f.fields.iter()
            .find(|(k, _)| *k == "body").map(|(_, v)| v) {
            Some(J::List(items)) => items.iter().filter_map(as_node).collect(),
            _ => Vec::new(),
        };
        let mut seen: HashSet<String> = HashSet::new();
        for node in block_nodes(body) {
            // ⚠️ 只认 `Assign` / `For`（**不含**推导式）—— 与 Python 侧一致。
            if !matches!(node.kind, "Assign" | "For") {
                continue;
            }
            let mut flat: Vec<&Node> = Vec::new();
            bound_names(node, &mut flat);
            for b in flat {
                let Some(id) = name_id(b) else { continue };
                if seen.contains(&id) || used.contains(&id) || is_internal(&id) {
                    continue;
                }
                seen.insert(id.clone());
                let hint = if node.kind == "For" {
                    "只是想把循环跑固定次数的话，用「循环 N 次」可以不引入变量。"
                } else {
                    "删掉这个绑定，或确认它是不是本该赋给别的名字。"
                };
                let (line, col) = node_line_col(b);
                out.push(Issue {
                    code: "name.unused".to_string(),
                    level: LEVEL_WARNING,
                    line,
                    col,
                    message: format!("变量「{id}」在函数「{fname}」里绑定后从未被读取"),
                    hint: hint.to_string(),
                    name: id,
                });
            }
        }
    }
    out
}

fn self_compare(prog: &Node) -> Vec<Issue> {
    let mut out: Vec<Issue> = Vec::new();
    for node in walk_nodes(prog) {
        if node.kind != "Compare" {
            continue;
        }
        // `left` 与 `comparators` 里的元素都是**同一个 AST 里的节点**，
        // 所以 `prev` 一路借 `&Node` 就够（链式比较要按顺序串起来）。
        let mut prev: Option<&Node> = node.fields.iter()
            .find(|(k, _)| *k == "left").and_then(|(_, v)| as_node(v));
        let ops: Vec<String> = match node.fields.iter()
            .find(|(k, _)| *k == "ops").map(|(_, v)| v) {
            Some(J::List(items)) => items.iter().filter_map(str_at).collect(),
            _ => Vec::new(),
        };
        let comps: Vec<&Node> = match node.fields.iter()
            .find(|(k, _)| *k == "comparators").map(|(_, v)| v) {
            Some(J::List(items)) => items.iter().filter_map(as_node).collect(),
            _ => Vec::new(),
        };
        for (op, right) in ops.iter().zip(comps.iter()) {
            let prev_name = prev.filter(|p| p.kind == "Name").and_then(name_id);
            let right_name = if right.kind == "Name" { name_id(right) } else { None };
            if let (Some(a), Some(b)) = (prev_name.as_ref(), right_name.as_ref()) {
                if a == b && SELF_COMPARE_OPS.contains(&op.as_str()) {
                    let (line, col) = node_line_col(prev.unwrap());
                    out.push(Issue {
                        code: "compare.self".to_string(),
                        level: LEVEL_WARNING,
                        line,
                        col,
                        message: format!("「{a} {op} {a}」两边是同一个名字，\
                                          这个比较的结果是恒定的"),
                        hint: "是不是该拿另外两个值来比？".to_string(),
                        name: a.clone(),
                    });
                }
            }
            prev = Some(right);
        }
    }
    out
}

// ---------------------------------------------------------------------------
// 对外入口
// ---------------------------------------------------------------------------

/// 对一棵已解析的 AST 跑全部检查，按位置排序返回。
pub fn check_program(prog: &Node, sym: &Symbols) -> Vec<Issue> {
    let mut issues: Vec<Issue> = Vec::new();
    issues.extend(undefined_names(prog, sym));
    issues.extend(shadow_builtin(prog, sym));
    issues.extend(unused_vars(prog));
    issues.extend(self_compare(prog));
    issues.sort_by(|a, b| {
        (a.line, a.col, a.code.as_str()).cmp(&(b.line, b.col, b.code.as_str()))
    });
    issues
}

/// 检查一段源码文本。**语法错也当一条问题返回**（不往外抛）。
pub fn check_source(text: &str, filename: &str, sym: &Symbols) -> Vec<Issue> {
    match parser::parse(text, filename) {
        Ok(prog) => check_program(&prog, sym),
        Err(e) => {
            let title = errcodes::title(e.code).unwrap_or("");
            let message = if e.message.is_empty() {
                title.to_string()
            } else {
                format!("{title}：{}", e.message)
            };
            vec![Issue {
                code: e.code.to_string(),
                level: LEVEL_ERROR,
                line: if e.line == 0 { 1 } else { e.line as i64 },
                col: if e.col == 0 { 1 } else { e.col as i64 },
                message,
                hint: e.hint.unwrap_or_default(),
                name: String::new(),
            }]
        }
    }
}

/// 检查一个 `.jsh` 文件。读不出来时**也返回一条问题**（不抛）。
pub fn check_file(path: &Path, sym: &Symbols) -> Vec<Issue> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) => {
            return vec![Issue {
                code: "check.read".to_string(),
                level: LEVEL_ERROR,
                line: 1,
                col: 1,
                message: format!("读不了文件：{e}"),
                hint: String::new(),
                name: String::new(),
            }];
        }
    };
    check_source(&text, &display_path(path), sym)
}

fn is_skipped_dir(name: &str) -> bool {
    SKIP_DIRS.contains(&name) || (name.starts_with('.') && name != "." && name != "..")
}

/// 按路径收集待检查的 `.jsh` 文件（是文件就返回它，是目录就递归找）。
pub fn iter_jsh_files(root: &Path) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    if root.is_file() {
        out.push(root.to_path_buf());
        return out;
    }
    if !root.is_dir() {
        return out;
    }
    fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
        let Ok(rd) = std::fs::read_dir(dir) else { return };
        let mut entries: Vec<PathBuf> = rd.filter_map(|e| e.ok())
            .map(|e| e.path()).collect();
        entries.sort();
        for p in entries {
            if p.is_dir() {
                if let Some(n) = p.file_name().and_then(|s| s.to_str()) {
                    if !is_skipped_dir(n) {
                        walk(&p, out);
                    }
                }
            } else if p.extension().and_then(|s| s.to_str()) == Some("jsh") {
                let hidden = p.file_name().and_then(|s| s.to_str())
                    .map(|n| n.starts_with('.')).unwrap_or(false);
                if !hidden {
                    out.push(p);
                }
            }
        }
    }
    walk(root, &mut out);
    out
}

/// 批量检查（文件或目录），返回 `[(文件, 问题)]`，按文件、位置排序。
pub fn check_paths(paths: &[String], sym: &Symbols) -> Vec<(PathBuf, Issue)> {
    let mut out: Vec<(PathBuf, Issue)> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for raw in paths {
        for path in iter_jsh_files(Path::new(raw)) {
            let key = std::fs::canonicalize(&path)
                .map(|p| p.display().to_string())
                .unwrap_or_else(|_| display_path(&path));
            if seen.contains(&key) {
                continue;
            }
            seen.insert(key);
            for issue in check_file(&path, sym) {
                out.push((path.clone(), issue));
            }
        }
    }
    out.sort_by(|a, b| {
        (display_path(&a.0), a.1.line, a.1.col, a.1.code.clone())
            .cmp(&(display_path(&b.0), b.1.line, b.1.col, b.1.code.clone()))
    });
    out
}

/// 打印路径时的形态 —— **与 Python 的 `str(Path(...))` 对齐**。
///
/// ⚠️ Windows 上 Python 的 `Path` 会把 `/` 归一成 `\`（`Path("a/b")` 印出来是
/// `a\b`），宿主不这么做的话，同一次检查两边打出来的文件名就不一样 ——
/// 对拍会红，而且用户看到的路径风格也来回变。
pub fn display_path(p: &Path) -> String {
    let s = p.display().to_string();
    if cfg!(windows) { s.replace('/', "\\") } else { s }
}

/// 自报家底（给漂移检测用）：四个符号集合 → JSON 文本（键排好序）。
pub fn dump_symbols_json(sym: &Symbols) -> String {
    fn arr(s: &HashSet<String>) -> String {
        let mut v: Vec<&String> = s.iter().collect();
        v.sort();
        let items: Vec<String> = v.iter().map(|x| jstr(x)).collect();
        format!("[{}]", items.join(","))
    }
    format!("{{\"keywords\":{},\"builtins\":{},\"stdlib\":{},\"exceptions\":{}}}",
            arr(&sym.keywords), arr(&sym.builtins), arr(&sym.stdlib),
            arr(&sym.exceptions))
}

/// JSON 字符串转义（够用即可：控制字符走 \u）。
pub fn jstr(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}
