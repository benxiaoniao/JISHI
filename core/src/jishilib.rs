//! 基石库层（R5）—— `jishi/jishilib.py` 的 Rust 版。
//!
//! **用基石自己写的标准库**（`jishi/stdlib-jishi/*.jsh`）在**编译期展开**进主程序：
//! 把模块源码包成**一个函数**（`__基石库_统计__()`），顶层名字打包成字典返回，
//! 主程序里的 `导入 统计` 就地改写成 `统计 = $造模块("统计", __基石库_统计__())`。
//!
//! 于是「库」在字节码层面**就是一次普通函数调用** —— 三个执行器与两个跨语言宿主
//! 一个字都不用改。详细论证见 `jishi/jishilib.py` 的模块文档（那边写得比这里全）。
//!
//! ## 移植时最容易错的两处
//!
//! 1. **行号要整体减 1**：外壳源码比模块源码多一行函数头，不减的话模块里第 2 行的
//!    错误会被报成第 3 行 —— 中文报错的行列是这项目的立身之本之一。
//! 2. **改写只下探到「语句列表」**：Python 的 `_rewrite`/`_collect` 对
//!    `list[tuple]` 那种嵌套只处理**元组里的节点**，`branches[i][1]`（块体）**不进去**。
//!    所以 `如果 真：` 里面写 `导入 统计` **不会**被展开（运行时回落宿主层）。
//!    这边必须照做 —— 多下探一层就是另一种产物。

use std::path::{Path, PathBuf};

use crate::parser::{parse, J, Node};

/// 外壳函数名前缀。用双下划线 + 中文前缀，撞车概率极低；只在编译期出现。
const SHELL_PREFIX: &str = "__基石库_";

pub fn shell_name(name: &str) -> String {
    format!("{}{}__", SHELL_PREFIX, name)
}

// ---------------------------------------------------------------------------
// J 树的小工具（与 compiler.rs 里那套重复，但它们各自要独立成立）
// ---------------------------------------------------------------------------

fn field<'a>(n: &'a Node, name: &str) -> Option<&'a J> {
    n.fields.iter().find(|(k, _)| *k == name).map(|(_, v)| v)
}

fn as_jstr(v: Option<&J>) -> &str {
    match v {
        Some(J::Str(s)) => s.as_str(),
        _ => "",
    }
}

fn as_list(v: Option<&J>) -> &[J] {
    match v {
        Some(J::List(l)) => l.as_slice(),
        _ => &[],
    }
}

fn as_bool(v: Option<&J>) -> bool {
    matches!(v, Some(J::Bool(true)))
}

// ---------------------------------------------------------------------------
// 模块定位与切分
// ---------------------------------------------------------------------------

fn module_path(lib_dir: &str, name: &str) -> Option<PathBuf> {
    let p = Path::new(lib_dir).join(format!("{}.jsh", name));
    if p.is_file() {
        Some(p)
    } else {
        None
    }
}

/// 模块顶层**对外导出**的名字：`[(导出名, 源码里的名字), …]`。
///
/// - `__X` → 导出为 **`X`**（显式改名。有的导出名是关键字，写不成函数名）
/// - `_X` → 私有，不导出
/// - 其余 → 原名导出
fn export_names(program: &Node) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for node in as_list(field(program, "body")) {
        let n = match node {
            J::Node(n) => n,
            _ => continue,
        };
        let name = match n.kind {
            "FuncDef" | "ClassDef" => as_jstr(field(n, "name")).to_string(),
            "Assign" => match field(n, "target") {
                Some(J::Node(t)) if t.kind == "Name" => {
                    as_jstr(field(t, "id")).to_string()
                }
                _ => continue,
            },
            _ => continue,
        };
        let chars: Vec<char> = name.chars().collect();
        let pair = if chars.len() > 2 && chars[0] == '_' && chars[1] == '_' {
            (chars[2..].iter().collect::<String>(), name.clone())
        } else if chars.first() == Some(&'_') {
            continue;
        } else {
            (name.clone(), name.clone())
        };
        if !out.contains(&pair) {
            out.push(pair);
        }
    }
    out
}

/// 把模块源码包成外壳函数源码。
fn shell_source(lib_dir: &str, name: &str) -> String {
    let path = module_path(lib_dir, name).expect("模块存在");
    let source = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("读不到基石库模块 {:?}：{}", path, e));
    let norm = source.replace("\r\n", "\n").replace('\r', "\n");

    let exports = match parse(&norm, path.to_str().unwrap_or("lib.jsh")) {
        Ok(p) => export_names(&p),
        Err(e) => panic!("基石库模块 {} 自身解析失败：{}", name, e.message),
    };
    let body: Vec<String> = norm
        .split('\n')
        .map(|line| if line.trim().is_empty() { String::new() } else { format!("    {}", line) })
        .collect();
    let pairs: Vec<String> = exports
        .iter()
        .map(|(out, src)| format!("\"{}\": {}", out, src))
        .collect();
    format!("函数 {}()：\n{}\n    返回 {{{}}}\n",
            shell_name(name), body.join("\n"), pairs.join(", "))
}

/// 递归把所有节点的行号加上 `delta`（最小 1）。
///
/// ⚠️ **下探范围要与 Python 的 `_shift_lines` 逐字相同**：它只看「列表里的节点」
/// 和「元组里的节点」，**嵌套列表不进去**。所以 `如果` 的 `branches`（列表套元组）
/// 里，`test` 会挪、**块体那半不会**。多下探一层就会让模块里的行号整体差一行
/// （实测抓到过：`统计.jsh` 的 `计数` 里第 35 行被挪成了 34）。
fn shift_lines(node: &mut Node, delta: i64) {
    if let Some(J::Int(line)) = field(node, "line").cloned() {
        for (k, v) in node.fields.iter_mut() {
            if *k == "line" {
                *v = J::Int((line + delta).max(1));
            }
        }
    }
    for (k, v) in node.fields.iter_mut() {
        match *k {
            // 列表套元组：只挪元组里那个「是节点的」元素
            "branches" => {
                if let J::List(items) = v {
                    for br in items.iter_mut() {
                        if let J::List(pair) = br {
                            if let Some(J::Node(n)) = pair.get_mut(0) {
                                shift_lines(n, delta);
                            }
                        }
                    }
                }
            }
            "keywords" => {
                if let J::List(items) = v {
                    for kw in items.iter_mut() {
                        if let J::List(pair) = kw {
                            if let Some(J::Node(n)) = pair.get_mut(1) {
                                shift_lines(n, delta);
                            }
                        }
                    }
                }
            }
            _ => shift_j(v, delta),
        }
    }
}

fn shift_j(v: &mut J, delta: i64) {
    match v {
        J::Node(n) => shift_lines(n, delta),
        // Python 的列表分支只认「节点」；嵌套列表既不进也不挪
        J::List(items) => {
            for it in items.iter_mut() {
                if let J::Node(n) = it {
                    shift_lines(n, delta);
                }
            }
        }
        _ => {}
    }
}

/// 解析出外壳函数的 `FuncDef` 节点（行号已整体减 1）。
fn shell_def(lib_dir: &str, name: &str) -> Node {
    let src = shell_source(lib_dir, name);
    let want = shell_name(name);
    let program = match parse(&src, &format!("{}.jsh", name)) {
        Ok(p) => p,
        Err(e) => panic!("外壳函数 {} 解析失败：{}", want, e.message),
    };
    for node in as_list(field(&program, "body")) {
        if let J::Node(n) = node {
            if n.kind == "FuncDef" && as_jstr(field(n, "name")) == want {
                let mut def = n.as_ref().clone();
                shift_lines(&mut def, -1);
                return def;
            }
        }
    }
    panic!("外壳函数没生成出来：{}", name);
}

// ---------------------------------------------------------------------------
// AST 改写：`导入 统计` → `统计 = $造模块("统计", __基石库_统计__())`
// ---------------------------------------------------------------------------

struct St<'a> {
    lib_dir: &'a str,
    filename: &'a str,
    /// 已生成的外壳函数定义（**按首次出现的顺序**，Python 那边是 dict 的插入序）
    shells: Vec<(String, Node)>,
}

impl<'a> St<'a> {
    fn has(&self, name: &str) -> bool {
        module_path(self.lib_dir, name).is_some()
    }
}

/// 深度优先重写：先重写子节点，再用 `visit` 处理本节点。
fn rewrite_node(node: &Node, st: &mut St) -> Node {
    let mut out = Node::new(node.kind, 0, 0);
    out.fields.clear();
    for (k, v) in &node.fields {
        let nv = match *k {
            // Python 的 `list[tuple]`：**元组逐元素**过一遍（块体那半原样留着）
            "branches" => J::List(
                as_list(Some(v))
                    .iter()
                    .map(|br| {
                        let items = as_list(Some(br));
                        if items.len() == 2 {
                            J::List(vec![rewrite_j(&items[0], st), items[1].clone()])
                        } else {
                            br.clone()
                        }
                    })
                    .collect(),
            ),
            "keywords" => J::List(
                as_list(Some(v))
                    .iter()
                    .map(|kw| {
                        let items = as_list(Some(kw));
                        if items.len() == 2 {
                            J::List(vec![items[0].clone(), rewrite_j(&items[1], st)])
                        } else {
                            kw.clone()
                        }
                    })
                    .collect(),
            ),
            _ => match v {
                J::Node(_) => rewrite_j(v, st),
                J::List(items) => J::List(items.iter().map(|x| map_item(x, st)).collect()),
                other => other.clone(),
            },
        };
        out.fields.push((k, nv));
    }
    out.super_ok = node.super_ok;
    // 行号/列号在 fields 里，上面已原样带过来
    visit(out, st)
}

fn rewrite_j(v: &J, st: &mut St) -> J {
    match v {
        J::Node(n) => J::Node(Box::new(rewrite_node(n, st))),
        other => other.clone(),
    }
}

/// Python `_map_item`：节点 → 重写；别的一律原样（元组由上面按字段名单独处理）。
fn map_item(x: &J, st: &mut St) -> J {
    match x {
        J::Node(n) => J::Node(Box::new(rewrite_node(n, st))),
        other => other.clone(),
    }
}

fn visit(n: Node, st: &mut St) -> Node {
    if n.kind != "Import" {
        return n;
    }
    if as_bool(field(&n, "from_python")) || as_bool(field(&n, "from_local")) {
        return n;
    }
    let name = as_jstr(field(&n, "name")).to_string();
    if !st.has(&name) {
        return n;
    }
    let shell = shell_name(&name);
    if !st.shells.iter().any(|(k, _)| *k == shell) {
        let def = shell_def(st.lib_dir, &name);
        st.shells.push((shell.clone(), def));
    }
    let alias = as_jstr(field(&n, "alias")).to_string();
    let binding = if alias.is_empty() { name.clone() } else { alias };
    let _ = st.filename;

    let line = match field(&n, "line") {
        Some(J::Int(i)) => *i,
        _ => 1,
    };
    let col = match field(&n, "col") {
        Some(J::Int(i)) => *i,
        _ => 1,
    };

    // $造模块("统计", __基石库_统计__())
    let shell_call = Node::new("Call", line, col)
        .f("func", Node::new("Name", line, col).f("id", J::Str(shell)).boxed())
        .f("args", J::List(Vec::new()))
        .f("keywords", J::List(Vec::new()))
        .boxed();
    // ⚠️ 模块名要包成 **`Str` 节点**，不能直接塞一个裸 `J::Str` ——
    // 编译器是按 `kind` 分派的，裸 `J::Str` 不是节点，`compile_expr` 会
    // 直接跳过它（第一版就这么漏掉了那个实参，产物少一条 LOAD_CONST，
    // 靠 `compare --stage bytecode` 当场抓出来）。
    let name_node = Node::new("Str", line, col).f("value", J::Str(name)).boxed();
    let outer = Node::new("Call", line, col)
        .f("func", Node::new("Name", line, col).f("id", J::Str("$造模块".into())).boxed())
        .f("args", J::List(vec![name_node, shell_call]))
        .f("keywords", J::List(Vec::new()))
        .boxed();
    Node::new("Assign", line, col)
        .f("target", Node::new("Name", line, col).f("id", J::Str(binding)).boxed())
        .f("op", J::Str("=".into()))
        .f("value", outer)
}

/// 先探一遍有没有用到基石库模块（没用到就省掉深拷贝）。
fn collect(program: &Node, lib_dir: &str) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    for node in as_list(field(program, "body")) {
        walk(node, lib_dir, &mut found);
    }
    found
}

fn walk(v: &J, lib_dir: &str, found: &mut Vec<String>) {
    match v {
        J::Node(n) => {
            if n.kind == "Import" {
                if !as_bool(field(n, "from_python")) && !as_bool(field(n, "from_local")) {
                    let name = as_jstr(field(n, "name")).to_string();
                    if module_path(lib_dir, &name).is_some() && !found.contains(&name) {
                        found.push(name);
                    }
                }
                return;
            }
            for (_, fv) in n.fields.iter() {
                walk(fv, lib_dir, found);
            }
        }
        J::List(items) => {
            for it in items.iter() {
                walk(it, lib_dir, found);
            }
        }
        _ => {}
    }
}

/// 把主程序里所有 `导入 <基石库模块>` 就地改写成「调外壳函数」。
///
/// `lib_dir` 为 `None` 表示**关掉这一层**（`JISHI_NO_JISHILIB=1` 时 Python 侧传 None）。
pub fn expand(program: &Node, lib_dir: Option<&str>, filename: &str) -> Node {
    let lib_dir = match lib_dir {
        Some(d) => d,
        None => return program.clone(),
    };
    if collect(program, lib_dir).is_empty() {
        return program.clone();
    }

    let mut st = St { lib_dir, filename, shells: Vec::new() };
    let mut body: Vec<J> = as_list(field(program, "body"))
        .iter()
        .map(|x| map_item(x, &mut st))
        .collect();

    // 外壳函数定义插在最前面，保证任何一处改写后的调用都能找到它
    let mut defs: Vec<J> = st.shells.iter().map(|(_, d)| d.clone().boxed()).collect();
    defs.append(&mut body);

    let mut out = program.clone();
    for (k, v) in out.fields.iter_mut() {
        if *k == "body" {
            *v = J::List(defs.clone());
        }
    }
    out
}
