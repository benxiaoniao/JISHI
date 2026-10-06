//! AST 作用域分析（`jishi/scope.py` 的移植）—— **checker 与 LSP 共用一份**。
//!
//! 为什么单独一个模块（R7.3）：编辑器实时诊断（`lsp.rs`）与命令行
//! `jishi 源码静态检查`（`checker.rs`）判的是**同一件事**——「这个名字定义过没有」
//! 「哪些位置是绑定位置」。Python 侧为此刻意抽了 `scope.py`，Rust 这边照做；
//! 各自抄一份的下场是「编辑器不报、命令行报」这类漂移。
//!
//! ⚠️ 两个遍历的**顺序**都是契约，别「顺手统一」：
//! * [`walk_nodes`] 是**栈序**（`stack.extend(children)` → 最后一个孩子先出）；
//! * [`preorder`] 是**先序**（按字段顺序）。
//! 顺序会影响「同名只报第一次」时**报哪一处**。

use std::collections::HashSet;

use jishi_frontend::parser::{J, Node};

pub(crate) fn as_node(v: &J) -> Option<&Node> {
    match v {
        J::Node(n) => Some(n.as_ref()),
        _ => None,
    }
}

pub(crate) fn field<'a>(v: &'a J, key: &str) -> Option<&'a J> {
    as_node(v).and_then(|n| {
        n.fields.iter().find(|(k, _)| *k == key).map(|(_, x)| x)
    })
}

pub(crate) fn str_at(v: &J) -> Option<String> {
    match v {
        J::Str(s) => Some(s.clone()),
        _ => None,
    }
}

pub(crate) fn int_at(v: &J) -> Option<i64> {
    match v {
        J::Int(i) => Some(*i),
        _ => None,
    }
}

/// 从任意节点取 `(line, col)`（`loc_from`：缺省 1，0 也算 1）。
pub(crate) fn node_line_col(n: &Node) -> (i64, i64) {
    let get = |key: &str| -> Option<i64> {
        n.fields.iter().find(|(k, _)| *k == key).and_then(|(_, v)| int_at(v))
    };
    let line = get("line").unwrap_or(1);
    let col = get("col").unwrap_or(1);
    (if line == 0 { 1 } else { line }, if col == 0 { 1 } else { col })
}

/// 节点地址当「身份」（等价 Python 的 `id(node)`，见文件头说明）。
pub(crate) fn pid(n: &Node) -> usize {
    n as *const Node as usize
}

/// 把任意字段值里的 AST 节点摊平出来（`scope.flatten`）。
///
/// ⚠️ **`TargetList` 要摊成里面的名字**（M41 修的那条）：`令 甲, 乙 = …` 与
/// `遍历 序, 项 在 …` 的 target 都是 `TargetList`，它自己**不是** `Name` ——
/// 不摊的话解包出来的名字会被判成「从未定义」。
pub(crate) fn flatten<'a>(v: &'a J, out: &mut Vec<&'a Node>) {
    match v {
        J::Node(n) if n.kind == "TargetList" => {
            if let Some(J::List(names)) = field(v, "names") {
                for x in names {
                    flatten(x, out);
                }
            }
        }
        J::Node(_) => {
            if let Some(n) = as_node(v) {
                out.push(n);
            }
        }
        J::List(items) => {
            for x in items {
                flatten(x, out);
            }
        }
        _ => {}
    }
}

/// 一个节点的直接子节点（`scope.child_nodes`：走所有字段，摊平）。
pub(crate) fn child_nodes<'a>(n: &'a Node, out: &mut Vec<&'a Node>) {
    for (_, v) in &n.fields {
        flatten(v, out);
    }
}

/// 深度优先遍历全是节点的一棵 AST（`scope.walk_nodes`）。
///
/// ⚠️ **顺序要一模一样**：Python 是「栈 + `stack.extend(children)`」，于是
/// **最后一个孩子先出栈**。顺序会影响「同名只报第一次」时**报哪一处**，
/// 所以这里也照抄（`Vec` 当栈、`extend` 后从尾部取）。
pub(crate) fn walk_nodes<'a>(root: &'a Node) -> Vec<&'a Node> {
    let mut out: Vec<&'a Node> = Vec::new();
    let mut stack: Vec<&'a Node> = vec![root];
    while let Some(node) = stack.pop() {
        out.push(node);
        let mut kids: Vec<&'a Node> = Vec::new();
        child_nodes(node, &mut kids);
        stack.extend(kids);
    }
    out
}

/// 递归**先序**遍历（`scope.load_names` / `defined_names` 那一套用的顺序：
/// 孩子按字段顺序，与上面那个「栈序」**不同**，两套都要照抄）。
pub(crate) fn preorder<'a>(node: &'a Node, f: &mut dyn FnMut(&'a Node)) {
    f(node);
    let mut kids: Vec<&'a Node> = Vec::new();
    child_nodes(node, &mut kids);
    for k in kids {
        preorder(k, f);
    }
}

/// 一个「绑定语句」绑定的名字节点（赋值 / 遍历 / 推导式的目标）。
pub(crate) fn bound_names<'a>(node: &'a Node, out: &mut Vec<&'a Node>) {
    let mut flat: Vec<&'a Node> = Vec::new();
    if let Some(t) = node.fields.iter().find(|(k, _)| *k == "target") {
        flatten(&t.1, &mut flat);
    }
    for n in flat {
        if n.kind == "Name" {
            out.push(n);
        }
    }
}

pub(crate) fn name_id(n: &Node) -> Option<String> {
    n.fields.iter().find(|(k, _)| *k == "id").and_then(|(_, v)| str_at(v))
}

pub(crate) fn str_list_field(n: &Node, key: &str) -> Vec<String> {
    match n.fields.iter().find(|(k, _)| *k == key).map(|(_, v)| v) {
        Some(J::List(items)) => items.iter().filter_map(str_at).collect(),
        _ => Vec::new(),
    }
}

// ---------------------------------------------------------------------------
// 定义与使用
// ---------------------------------------------------------------------------

pub(crate) fn defined_names(prog: &Node) -> HashSet<String> {
    let mut names: HashSet<String> = HashSet::new();
    preorder(prog, &mut |n| match n.kind {
        "FuncDef" => {
            if let Some(s) = n.fields.iter()
                .find(|(k, _)| *k == "name").and_then(|(_, v)| str_at(v)) {
                names.insert(s);
            }
            for p in str_list_field(n, "params") {
                names.insert(p);
            }
        }
        "ClassDef" => {
            if let Some(s) = n.fields.iter()
                .find(|(k, _)| *k == "name").and_then(|(_, v)| str_at(v)) {
                names.insert(s);
            }
        }
        "ExceptHandler" => {
            if let Some(s) = n.fields.iter()
                .find(|(k, _)| *k == "name").and_then(|(_, v)| str_at(v))
                .filter(|s| !s.is_empty()) {
                names.insert(s);
            }
        }
        "Import" => {
            let nm = n.fields.iter().find(|(k, _)| *k == "name")
                .and_then(|(_, v)| str_at(v)).unwrap_or_default();
            let al = n.fields.iter().find(|(k, _)| *k == "alias")
                .and_then(|(_, v)| str_at(v)).filter(|s| !s.is_empty());
            names.insert(al.unwrap_or(nm));
        }
        "Assign" | "For" | "Comprehension" => {
            let mut flat: Vec<&Node> = Vec::new();
            bound_names(n, &mut flat);
            for b in flat {
                if let Some(id) = name_id(b) {
                    names.insert(id);
                }
            }
        }
        _ => {}
    });
    names
}

pub(crate) fn binding_marks(prog: &Node) -> HashSet<usize> {
    let mut marks: HashSet<usize> = HashSet::new();
    preorder(prog, &mut |n| {
        if matches!(n.kind, "Assign" | "For" | "Comprehension") {
            let mut flat: Vec<&Node> = Vec::new();
            bound_names(n, &mut flat);
            for b in flat {
                marks.insert(pid(b));
            }
        }
    });
    marks
}

pub(crate) fn load_names<'a>(prog: &'a Node, marks: &HashSet<usize>) -> Vec<&'a Node> {
    let mut out: Vec<&'a Node> = Vec::new();
    preorder(prog, &mut |n| {
        if n.kind == "Name" && !marks.contains(&pid(n)) {
            out.push(n);
        }
    });
    out
}

pub(crate) fn attr_base_marks(prog: &Node) -> HashSet<usize> {
    let mut marks: HashSet<usize> = HashSet::new();
    preorder(prog, &mut |n| {
        if n.kind == "Attr" {
            if let Some(obj) = field_obj_as_name(n) {
                marks.insert(pid(obj));
            }
        }
    });
    marks
}

pub(crate) fn field_obj_as_name(n: &Node) -> Option<&Node> {
    let obj = n.fields.iter().find(|(k, _)| *k == "obj").map(|(_, v)| v)?;
    match as_node(obj) {
        Some(x) if x.kind == "Name" => Some(x),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// 给 LSP 用的几个包装（返回 `Vec` 而不是填 `&mut Vec`，写起来顺一点）
// ---------------------------------------------------------------------------

/// 一个字段值里的直接子节点（Python 的 `_child_nodes`）。
pub(crate) fn child_nodes_of_value<'a>(v: &'a J) -> Vec<&'a Node> {
    let mut out: Vec<&Node> = Vec::new();
    flatten(v, &mut out);
    out
}

/// 一个节点的直接子节点。
pub(crate) fn child_nodes_vec<'a>(n: &'a Node) -> Vec<&'a Node> {
    let mut out: Vec<&Node> = Vec::new();
    child_nodes(n, &mut out);
    out
}

/// 先序遍历（LSP 侧要用，见模块头「两个顺序都是契约」）。
pub(crate) fn preorder_pub<'a>(node: &'a Node, f: &mut dyn FnMut(&'a Node)) {
    preorder(node, f)
}

/// 从节点字段取字符串（不克隆节点）。
pub(crate) fn s_field_node(n: &Node, key: &str) -> Option<String> {
    n.fields.iter().find(|(k, _)| *k == key).and_then(|(_, v)| str_at(v))
}
