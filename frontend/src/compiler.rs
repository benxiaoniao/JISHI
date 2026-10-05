//! 基石字节码编译器（R5）—— `jishi/compiler.py`（1151 行）的**逐行移植**。
//!
//! 编译分三步（与 Python 侧完全一致）：
//!
//! 1. **收集**：遍历 AST，为每个作用域记 `bound`（被绑定的名字）与 `used`
//!    （被读取的名字），同时建好作用域树。
//! 2. **定种**：后序遍历作用域树（最深的子作用域先算），判定哪些名字是局部变量、
//!    哪些要提升为闭包单元、哪些是全局。
//! 3. **发射**：逐节点生成指令，每条指令带源码行列。
//!
//! ⚠️ 第 2 步的**后序**顺序是关键：必须先处理内层，才能在外层发出任何指令之前
//! 确定「这个变量要变成单元」，否则会出现前半段 `STORE_FAST`、后半段
//! `LOAD_DEREF` 的分裂。
//!
//! ## 两个「按对象身份」的地方怎么搬过来的
//!
//! Python 用 `func_scopes[id(node)]` 把 **AST 节点对象**映射到它的作用域。
//! 这边 AST 是 `parser::J` 树，节点是 `Box<Node>` —— 只要树不被移动/克隆，
//! `&Node` 的**地址**就是稳定唯一的，于是用它当键（`nid()`）。
//! 收集发生在 `jishilib::expand` **之后**，那时树已经定型。

use std::collections::{BTreeSet, HashMap};

use crate::jishilib;
use crate::opcodes as op;
use crate::parser::{J, Node};

pub const MODULE_SCOPE: &str = "module";
pub const FUNC_SCOPE: &str = "function";

/// 与 `runtime.UNBOUND_SHADOW_HINT` 逐字相同（改一处必须改另一处）。
const UNBOUND_SHADOW_HINT: &str = "外层也有一个叫「{name}」的变量。在这个函数里给它赋过值，它就成了这个函数的局部变量，赋值之前不能读取";

// ---------------------------------------------------------------------------
// 常量池
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub enum Const {
    None,
    Bool(bool),
    Int(i64),
    /// 超出 i64 的整数：十进制原文
    Big(String),
    Float(f64),
    Str(String),
}

/// Python 的缓存键是 `(type(v).__name__, v)`：
/// - `1` / `1.0` / `真` 三者**互不相同**（类型名不同）；
/// - `0.0` 与 `-0.0` **相同**（`==` 且 `hash` 相同）—— 所以浮点按归一化后的位比。
#[derive(Clone)]
struct ConstKey(Const);

fn norm_bits(f: f64) -> u64 {
    if f == 0.0 {
        0.0f64.to_bits()
    } else {
        f.to_bits()
    }
}

impl std::hash::Hash for ConstKey {
    fn hash<H: std::hash::Hasher>(&self, st: &mut H) {
        match &self.0 {
            Const::None => 0u8.hash(st),
            Const::Bool(b) => {
                1u8.hash(st);
                b.hash(st);
            }
            Const::Int(i) => {
                2u8.hash(st);
                i.hash(st);
            }
            Const::Big(s) => {
                2u8.hash(st);
                s.hash(st);
            }
            Const::Float(f) => {
                3u8.hash(st);
                norm_bits(*f).hash(st);
            }
            Const::Str(s) => {
                4u8.hash(st);
                s.hash(st);
            }
        }
    }
}

impl Eq for ConstKey {}

impl PartialEq for ConstKey {
    fn eq(&self, other: &Self) -> bool {
        use Const::*;
        match (&self.0, &other.0) {
            (None, None) => true,
            (Bool(a), Bool(b)) => a == b,
            (Int(a), Int(b)) => a == b,
            (Big(a), Big(b)) => a == b,
            (Float(a), Float(b)) => norm_bits(*a) == norm_bits(*b),
            (Str(a), Str(b)) => a == b,
            _ => false,
        }
    }
}

// ---------------------------------------------------------------------------
// 指令 / 代码对象 / 编译结果
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Default)]
pub struct Instr {
    pub op: i64,
    pub a: i64,
    pub b: i64,
    pub c: i64,
    pub line: i64,
    pub col: i64,
    /// 若这条指令是**某条语句的第一条指令**，这里是那条语句的行号，否则 0。
    /// 调试器靠它认出「语句边界」（`as_tuple()` 仍是 6 元组，不进序列化）。
    pub stmt_line: i64,
}

#[derive(Clone, Debug, Default)]
pub struct Code {
    pub name: String,
    pub params: Vec<String>,
    pub param_idx: Vec<i64>,
    pub param_local: Vec<i64>,
    pub param_cell: Vec<i64>,
    pub param_kind: Vec<i64>,
    pub instrs: Vec<Instr>,
    pub nlocals: i64,
    pub local_names: Vec<String>,
    pub cellvars: Vec<String>,
    pub freevars: Vec<String>,
    /// 局部槽号 → 提示文本（**不进序列化**，只给字节码执行器报错用）。
    pub local_hints: Vec<(i64, String)>,
    pub firstlineno: i64,
}

#[derive(Clone, Debug, Default)]
pub struct Module {
    pub consts: Vec<Const>,
    pub names: Vec<String>,
    pub kw_names: Vec<Vec<String>>,
    pub codes: Vec<Code>,
    pub main: i64,
    pub filename: String,
}

// ---------------------------------------------------------------------------
// J 树的小工具
// ---------------------------------------------------------------------------

static J_NULL: J = J::Null;

/// 按字段名取 J（缺字段当 `J::Null`）。
fn nf<'a>(n: &'a Node, name: &str) -> &'a J {
    n.fields
        .iter()
        .find(|(k, _)| *k == name)
        .map(|(_, v)| v)
        .unwrap_or(&J_NULL)
}

/// 节点身份（`Box<Node>` 的地址）—— 与 Python 的 `id(node)` 同角色。
fn nid(n: &Node) -> usize {
    n as *const Node as usize
}

fn node_of(v: &J) -> Option<&Node> {
    match v {
        J::Node(n) => Some(n),
        _ => None,
    }
}

/// 取 `J::Node` 的 `kind`（非节点给空串，调用方按 `==` 比）。
fn kd(v: &J) -> &'static str {
    match v {
        J::Node(n) => n.kind,
        _ => "",
    }
}

fn is_null(v: &J) -> bool {
    matches!(v, J::Null)
}

fn as_jstr(v: &J) -> &str {
    match v {
        J::Str(s) => s.as_str(),
        _ => "",
    }
}

fn as_int(v: &J) -> i64 {
    match v {
        J::Int(i) => *i,
        J::Bool(true) => 1,
        _ => 0,
    }
}

fn as_bool(v: &J) -> bool {
    matches!(v, J::Bool(true))
}

fn as_list(v: &J) -> &[J] {
    match v {
        J::List(l) => l.as_slice(),
        _ => &[],
    }
}

fn str_list(v: &J) -> Vec<String> {
    as_list(v).iter().map(|x| as_jstr(x).to_string()).collect()
}

fn int_list(v: &J) -> Vec<i64> {
    as_list(v).iter().map(as_int).collect()
}

/// 节点的 `line`，`or 1` 兜底（与 `getattr(node, "line", 1) or 1` 一致）。
fn jline(n: &Node) -> i64 {
    match nf(n, "line") {
        J::Int(i) if *i != 0 => *i,
        _ => 1,
    }
}

fn jpos(n: &Node) -> (i64, i64) {
    let col = match nf(n, "col") {
        J::Int(i) if *i != 0 => *i,
        _ => 1,
    };
    (jline(n), col)
}

/// `J` 版本的行号（非节点给 1）。
fn jline_v(v: &J) -> i64 {
    match node_of(v) {
        Some(n) => jline(n),
        None => 1,
    }
}

// ---------------------------------------------------------------------------
// 作用域
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
enum ScopeKind {
    Module,
    Function,
}

struct Scope {
    kind: ScopeKind,
    name: String,
    bound: BTreeSet<String>,
    used: BTreeSet<String>,
    /// 本作用域绑定、被内层引用
    cellvars: BTreeSet<String>,
    /// 本作用域引用、由外层绑定
    freevars: BTreeSet<String>,
    children: Vec<usize>,
    parent: Option<usize>,
}

impl Scope {
    fn new(kind: ScopeKind, name: &str, parent: Option<usize>) -> Scope {
        Scope {
            kind,
            name: name.to_string(),
            bound: BTreeSet::new(),
            used: BTreeSet::new(),
            cellvars: BTreeSet::new(),
            freevars: BTreeSet::new(),
            children: Vec::new(),
            parent,
        }
    }

    fn cell_index(&self, name: &str) -> i64 {
        let cv: Vec<&String> = self.cellvars.iter().collect();
        if let Some(i) = cv.iter().position(|x| x.as_str() == name) {
            return i as i64;
        }
        let fv: Vec<&String> = self.freevars.iter().collect();
        (cv.len() + fv.iter().position(|x| x.as_str() == name).unwrap_or(0)) as i64
    }

    fn is_func(&self) -> bool {
        self.kind == ScopeKind::Function
    }
}

struct Collector {
    scopes: Vec<Scope>,
    func_scopes: HashMap<usize, usize>,
}

impl Collector {
    fn add(&mut self, kind: ScopeKind, name: &str, parent: Option<usize>) -> usize {
        self.scopes.push(Scope::new(kind, name, parent));
        self.scopes.len() - 1
    }
}

// ---------------------------------------------------------------------------
// 第 1 步：收集
// ---------------------------------------------------------------------------

fn collect_stmts(stmts: &[J], scope: usize, c: &mut Collector) {
    for st in stmts {
        collect_stmt(st, scope, c);
    }
}

fn collect_stmt(st: &J, scope: usize, c: &mut Collector) {
    let n = match node_of(st) {
        Some(n) => n,
        None => return,
    };
    match n.kind {
        "Assign" => {
            let target = nf(n, "target");
            if kd(target) == "Name" {
                let id = as_jstr(nf(node_of(target).unwrap(), "id")).to_string();
                c.scopes[scope].bound.insert(id.clone());
                // 复合赋值要先读旧值
                if as_jstr(nf(n, "op")) != "=" {
                    c.scopes[scope].used.insert(id);
                }
            } else if kd(target) == "TargetList" {
                let tn = node_of(target).unwrap();
                for name_node in as_list(nf(tn, "names")) {
                    if let Some(m) = node_of(name_node) {
                        let id = as_jstr(nf(m, "id")).to_string();
                        c.scopes[scope].bound.insert(id);
                    }
                }
            }
            collect_expr(nf(n, "value"), scope, c);
        }
        "ExprStmt" => collect_expr(nf(n, "expr"), scope, c),
        "If" => {
            for br in as_list(nf(n, "branches")) {
                let items = as_list(br);
                if items.len() >= 2 {
                    collect_expr(&items[0], scope, c);
                    collect_stmts(as_list(&items[1]), scope, c);
                }
            }
            let orelse = nf(n, "orelse");
            if !is_null(orelse) {
                collect_stmts(as_list(orelse), scope, c);
            }
        }
        "For" => {
            collect_expr(nf(n, "iter"), scope, c);
            let target = nf(n, "target");
            if kd(target) == "Name" {
                let id = as_jstr(nf(node_of(target).unwrap(), "id")).to_string();
                c.scopes[scope].bound.insert(id);
            }
            collect_stmts(as_list(nf(n, "body")), scope, c);
        }
        "Loop" => {
            collect_expr(nf(n, "times"), scope, c);
            collect_stmts(as_list(nf(n, "body")), scope, c);
        }
        "While" => {
            collect_expr(nf(n, "test"), scope, c);
            collect_stmts(as_list(nf(n, "body")), scope, c);
        }
        "FuncDef" => {
            let name = as_jstr(nf(n, "name")).to_string();
            c.scopes[scope].bound.insert(name.clone());
            for d in as_list(nf(n, "defaults")) {
                if !is_null(d) {
                    collect_expr(d, scope, c);
                }
            }
            let sub = c.add(ScopeKind::Function, &name, Some(scope));
            for p in str_list(nf(n, "params")) {
                c.scopes[sub].bound.insert(p);
            }
            c.scopes[scope].children.push(sub);
            c.func_scopes.insert(nid(n), sub);
            collect_stmts(as_list(nf(n, "body")), sub, c);
        }
        "ClassDef" => {
            let name = as_jstr(nf(n, "name")).to_string();
            c.scopes[scope].bound.insert(name);
            let base = nf(n, "base");
            if !is_null(base) {
                c.scopes[scope].used.insert(as_jstr(base).to_string());
            }
            for node in as_list(nf(n, "body")) {
                let m = match node_of(node) {
                    Some(m) => m,
                    None => continue,
                };
                match m.kind {
                    "FuncDef" => {
                        let mname = as_jstr(nf(m, "name")).to_string();
                        let sub = c.add(ScopeKind::Function, &mname, Some(scope));
                        for p in str_list(nf(m, "params")) {
                            c.scopes[sub].bound.insert(p);
                        }
                        c.scopes[scope].children.push(sub);
                        c.func_scopes.insert(nid(m), sub);
                        collect_stmts(as_list(nf(m, "body")), sub, c);
                    }
                    // 类变量（M23.3）：值在「定义类的外层作用域」里求值
                    "Assign" => collect_expr(nf(m, "value"), scope, c),
                    _ => {}
                }
            }
        }
        "Return" => {
            let v = nf(n, "value");
            if !is_null(v) {
                collect_expr(v, scope, c);
            }
        }
        "Import" => {
            let alias = nf(n, "alias");
            let name = if is_null(alias) || as_jstr(alias).is_empty() {
                as_jstr(nf(n, "name")).to_string()
            } else {
                as_jstr(alias).to_string()
            };
            c.scopes[scope].bound.insert(name);
        }
        "Try" => {
            // ⚠️ **这一支原先漏了**（R5 逐行移植时发现并如实复刻，R5.1 补齐）。
            // 后果比「少收集几个名字」严重得多：
            //   ① 体里**赋值**的名字进不了 bound → 编成 STORE_GLOBAL；
            //   ② 体里**读**的名字进不了 used → 闭包捕获不到 → 运行期 E0301；
            //   ③ 体里**定义函数/类**时 `func_scopes` 没登记 → 发射期 KeyError，
            //      **编译期直接崩**。
            // 只有「外层有同名变量」时才看得出来（否则全局与局部恰好等价）。
            // 与 Python 侧的 `_collect_stmt` **逐字对应**，改一处必须改另一处。
            collect_stmts(as_list(nf(n, "body")), scope, c);
            for h in as_list(nf(n, "handlers")) {
                let hm = match node_of(h) {
                    Some(m) => m,
                    None => continue,
                };
                let ty = nf(hm, "type");
                if !is_null(ty) {
                    collect_expr(ty, scope, c);
                }
                let hname = nf(hm, "name");
                if !is_null(hname) && !as_jstr(hname).is_empty() {
                    // 捕获名与「赋值」同级：它是所在作用域的局部
                    let s = as_jstr(hname).to_string();
                    c.scopes[scope].bound.insert(s);
                }
                collect_stmts(as_list(nf(hm, "body")), scope, c);
            }
            let fin = nf(n, "finalbody");
            if !is_null(fin) {
                collect_stmts(as_list(fin), scope, c);
            }
        }
        "Raise" => {
            let v = nf(n, "value");
            if !is_null(v) {
                collect_expr(v, scope, c);
            }
        }
        // Break / Continue / Pass 没有子语句与子表达式 —— 显式列出来，
        // 免得下次往 AST 里加语句时又「悄悄落进 `_ => {}`」。
        "Break" | "Continue" | "Pass" => {}
        _ => {}
    }
}

fn collect_expr(e: &J, scope: usize, c: &mut Collector) {
    let n = match node_of(e) {
        Some(n) => n,
        None => return,
    };
    match n.kind {
        "Name" => {
            let id = as_jstr(nf(n, "id")).to_string();
            c.scopes[scope].used.insert(id);
        }
        "BinOp" => {
            collect_expr(nf(n, "left"), scope, c);
            collect_expr(nf(n, "right"), scope, c);
        }
        "UnaryOp" => collect_expr(nf(n, "operand"), scope, c),
        "BoolOp" => {
            for v in as_list(nf(n, "values")) {
                collect_expr(v, scope, c);
            }
        }
        "Lambda" => {
            let name = as_jstr(nf(n, "name")).to_string();
            let sub = c.add(ScopeKind::Function, &name, Some(scope));
            for p in str_list(nf(n, "params")) {
                c.scopes[sub].bound.insert(p);
            }
            c.scopes[scope].children.push(sub);
            c.func_scopes.insert(nid(n), sub);
            for d in as_list(nf(n, "defaults")) {
                if !is_null(d) {
                    collect_expr(d, scope, c);
                }
            }
            collect_stmts(as_list(nf(n, "body")), sub, c);
        }
        "Compare" => {
            collect_expr(nf(n, "left"), scope, c);
            for cmp in as_list(nf(n, "comparators")) {
                collect_expr(cmp, scope, c);
            }
        }
        "Call" => {
            collect_expr(nf(n, "func"), scope, c);
            for a in as_list(nf(n, "args")) {
                collect_expr(a, scope, c);
            }
            for kw in as_list(nf(n, "keywords")) {
                let pair = as_list(kw);
                if pair.len() >= 2 {
                    collect_expr(&pair[1], scope, c);
                }
            }
        }
        "Attr" => collect_expr(nf(n, "obj"), scope, c),
        "Subscript" => {
            collect_expr(nf(n, "obj"), scope, c);
            collect_expr(nf(n, "index"), scope, c);
        }
        "List" => {
            for el in as_list(nf(n, "elements")) {
                collect_expr(el, scope, c);
            }
        }
        "Dict" => {
            for k in as_list(nf(n, "keys")) {
                collect_expr(k, scope, c);
            }
            for v in as_list(nf(n, "values")) {
                collect_expr(v, scope, c);
            }
        }
        "Comprehension" => {
            collect_expr(nf(n, "iter"), scope, c);
            let target = nf(n, "target");
            if kd(target) == "Name" {
                let id = as_jstr(nf(node_of(target).unwrap(), "id")).to_string();
                c.scopes[scope].bound.insert(id);
            }
            for f in ["elt", "key", "value", "condition"] {
                let v = nf(n, f);
                if !is_null(v) {
                    collect_expr(v, scope, c);
                }
            }
        }
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// 第 2 步：定种（后序）
// ---------------------------------------------------------------------------

fn resolve(scopes: &mut Vec<Scope>, id: usize) {
    let children = scopes[id].children.clone();
    for child in children {
        resolve(scopes, child);
    }
    if !scopes[id].is_func() {
        return;
    }
    let used: Vec<String> = scopes[id].used.iter().cloned().collect();
    for name in used {
        if scopes[id].bound.contains(&name) {
            continue;
        }
        let mut chain = vec![id];
        let mut found: Option<usize> = None;
        let mut e = scopes[id].parent;
        while let Some(cur) = e {
            if scopes[cur].bound.contains(&name) || scopes[cur].cellvars.contains(&name) {
                found = Some(cur);
                break;
            }
            chain.push(cur);
            e = scopes[cur].parent;
        }
        let found = match found {
            None => continue,                       // 全局（内建、导入的名字）
            Some(f) if !scopes[f].is_func() => continue,
            Some(f) => f,
        };
        scopes[found].cellvars.insert(name.clone());
        for s in chain {
            scopes[s].freevars.insert(name.clone());
        }
    }
}

// ---------------------------------------------------------------------------
// 第 3 步：发射
// ---------------------------------------------------------------------------

struct Emitter<'a> {
    m: Module,
    scopes: &'a Vec<Scope>,
    func_scopes: &'a HashMap<usize, usize>,
    code: Code,
    scope: usize,
    fuse_method_call: bool,
    const_cache: HashMap<ConstKey, usize>,
    name_cache: HashMap<String, usize>,
    tmp_seq: i64,
}

impl<'a> Emitter<'a> {
    fn new(m: Module, scopes: &'a Vec<Scope>,
           func_scopes: &'a HashMap<usize, usize>, root: usize,
           fuse_method_call: bool) -> Emitter<'a> {
        Emitter {
            m,
            scopes,
            func_scopes,
            code: Code::default(),
            scope: root,
            fuse_method_call,
            const_cache: HashMap::new(),
            name_cache: HashMap::new(),
            tmp_seq: 0,
        }
    }

    // -- 表管理 -------------------------------------------------------------

    fn const_idx(&mut self, v: Const) -> i64 {
        let key = ConstKey(v.clone());
        if let Some(hit) = self.const_cache.get(&key) {
            return *hit as i64;
        }
        self.m.consts.push(v);
        let idx = self.m.consts.len() - 1;
        self.const_cache.insert(key, idx);
        idx as i64
    }

    fn name_idx(&mut self, name: &str) -> i64 {
        if let Some(hit) = self.name_cache.get(name) {
            return *hit as i64;
        }
        self.m.names.push(name.to_string());
        let idx = self.m.names.len() - 1;
        self.name_cache.insert(name.to_string(), idx);
        idx as i64
    }

    fn kw_idx(&mut self, names: Vec<String>) -> i64 {
        self.m.kw_names.push(names);
        (self.m.kw_names.len() - 1) as i64
    }

    // -- 指令发射 -----------------------------------------------------------

    fn push_instr(&mut self, opcode: i64, a: i64, b: i64, c: i64,
                  line: i64, col: i64) -> i64 {
        self.code.instrs.push(Instr { op: opcode, a, b, c, line, col, stmt_line: 0 });
        (self.code.instrs.len() - 1) as i64
    }

    fn emit(&mut self, opcode: i64, node: Option<&Node>, a: i64, b: i64, c: i64) -> i64 {
        let (line, col) = match node {
            Some(n) => jpos(n),
            None => (1, 1),
        };
        self.push_instr(opcode, a, b, c, line, col)
    }

    fn here(&self) -> i64 {
        self.code.instrs.len() as i64
    }

    fn patch(&mut self, idx: i64, target: i64, field: char) {
        let i = idx as usize;
        match field {
            'a' => self.code.instrs[i].a = target,
            'b' => self.code.instrs[i].b = target,
            _ => self.code.instrs[i].c = target,
        }
    }

    /// 在当前代码对象里申请一个隐藏局部槽（如循环计数器）。
    fn new_tmp(&mut self) -> i64 {
        let slot = self.code.nlocals;
        self.code.nlocals += 1;
        self.tmp_seq += 1;
        self.code.local_names.push(format!("$tmp{}", self.tmp_seq));
        slot
    }

    // -- 名字访问 -----------------------------------------------------------

    fn load_name(&mut self, name: &str, node: Option<&Node>) -> i64 {
        let (is_cell, is_local) = {
            let sc = &self.scopes[self.scope];
            (
                sc.cellvars.contains(name) || sc.freevars.contains(name),
                sc.is_func() && sc.bound.contains(name),
            )
        };
        if is_cell {
            let idx = self.scopes[self.scope].cell_index(name);
            return self.emit(op::LOAD_DEREF, node, idx, 0, 0);
        }
        if is_local {
            let slot = self.local_slot(name);
            return self.emit(op::LOAD_FAST, node, slot, 0, 0);
        }
        let idx = self.name_idx(name);
        self.emit(op::LOAD_GLOBAL, node, idx, 0, 0)
    }

    fn store_name(&mut self, name: &str, node: Option<&Node>) -> i64 {
        let (is_cell, is_local) = {
            let sc = &self.scopes[self.scope];
            (
                sc.cellvars.contains(name) || sc.freevars.contains(name),
                sc.is_func() && sc.bound.contains(name),
            )
        };
        if is_cell {
            let idx = self.scopes[self.scope].cell_index(name);
            return self.emit(op::STORE_DEREF, node, idx, 0, 0);
        }
        if is_local {
            let slot = self.local_slot(name);
            return self.emit(op::STORE_FAST, node, slot, 0, 0);
        }
        let idx = self.name_idx(name);
        self.emit(op::STORE_GLOBAL, node, idx, 0, 0)
    }

    fn local_slot(&self, name: &str) -> i64 {
        self.code
            .local_names
            .iter()
            .position(|n| n == name)
            .map(|i| i as i64)
            .unwrap_or_else(|| panic!("（内部）局部变量「{}」没有分配槽位", name))
    }

    // -- 语句 ---------------------------------------------------------------

    fn compile_body(&mut self, stmts: &[J]) {
        for st in stmts {
            self.compile_stmt(st);
        }
    }

    /// 编译一条语句，顺手在它的**第一条指令**上打「语句起始」标（B5）。
    fn compile_stmt(&mut self, st: &J) {
        let start = self.code.instrs.len();
        let line = jline_v(st);
        self.compile_stmt_inner(st);
        self.mark_start(start, line);
    }

    fn mark_start(&mut self, start: usize, line: i64) {
        if self.code.instrs.len() > start {
            self.code.instrs[start].stmt_line = line;
        }
    }

    /// 发一条「空操作」（常量入栈再弹出）并就地打上语句起始标。
    fn emit_nop(&mut self, line: i64, col: i64) {
        let start = self.code.instrs.len();
        let ci = self.const_idx(Const::None);
        self.push_instr(op::LOAD_CONST, ci, 0, 0, line, col);
        self.push_instr(op::POP_TOP, 0, 0, 0, line, col);
        self.mark_start(start, line);
    }

    fn compile_stmt_inner(&mut self, st: &J) {
        let n = match node_of(st) {
            Some(n) => n,
            None => return,
        };
        match n.kind {
            "Assign" => self.c_assign(n),
            "ExprStmt" => {
                let e = nf(n, "expr");
                self.compile_expr(e);
                // 模块顶层的表达式语句要保留值给 REPL 回显
                if self.is_module_scope() {
                    self.emit(op::STORE_LAST, Some(n), 0, 0, 0);
                } else {
                    self.emit(op::POP_TOP, Some(n), 0, 0, 0);
                }
            }
            "If" => self.c_if(n),
            "For" => self.c_for(n),
            "Loop" => self.c_loop(n),
            "While" => self.c_while(n),
            "Break" => {
                self.emit(op::BREAK_LOOP, Some(n), 0, 0, 0);
            }
            "Continue" => {
                self.emit(op::CONTINUE_LOOP, Some(n), 0, 0, 0);
            }
            "FuncDef" => self.c_funcdef(n),
            "ClassDef" => self.c_classdef(n),
            "Return" => {
                let v = nf(n, "value");
                if !is_null(v) {
                    self.compile_expr(v);
                } else {
                    let ci = self.const_idx(Const::None);
                    self.emit(op::LOAD_CONST, Some(n), ci, 0, 0);
                }
                self.emit(op::RETURN, Some(n), 0, 0, 0);
            }
            "Import" => self.c_import(n),
            "Try" => self.c_try(n),
            "Raise" => {
                let v = nf(n, "value");
                if !is_null(v) {
                    self.compile_expr(v);
                } else {
                    let ci = self.const_idx(Const::None);
                    self.emit(op::LOAD_CONST, Some(n), ci, 0, 0);
                }
                self.emit(op::THROW, Some(n), 0, 0, 0);
            }
            // 空块占位（`:` / `通过`）：树遍历里它是一次真实的 `exec_stmt`
            "Pass" => {
                let (l, c) = jpos(n);
                self.emit_nop(l, c);
            }
            _ => panic!("还不支持的语句：{}", n.kind),
        }
    }

    fn is_module_scope(&self) -> bool {
        self.scopes[self.scope].kind == ScopeKind::Module
    }

    fn c_assign(&mut self, n: &Node) {
        let target = nf(n, "target");
        let value = nf(n, "value");
        let opstr = as_jstr(nf(n, "op")).to_string();
        let binop = |full: &str| -> i64 {
            op::symbol_to_binop(&full[..full.len() - 1]).expect("复合赋值的运算符")
        };
        match kd(target) {
            "TargetList" => {
                let tn = node_of(target).unwrap();
                let star = nf(tn, "star_index");
                let star_i = if is_null(star) { -1 } else { as_int(star) };
                let names = as_list(nf(tn, "names"));
                let count = names.len() as i64;
                self.compile_expr(value);
                self.emit(op::UNPACK, Some(n), count, star_i, 0);
                for name_node in names {
                    let id = as_jstr(nf(node_of(name_node).unwrap(), "id")).to_string();
                    self.store_name(&id, Some(n));
                }
            }
            "Name" => {
                let id = as_jstr(nf(node_of(target).unwrap(), "id")).to_string();
                if opstr == "=" {
                    self.compile_expr(value);
                    self.store_name(&id, Some(n));
                } else {
                    self.load_name(&id, Some(n));
                    self.compile_expr(value);
                    let b = binop(&opstr);
                    self.emit(op::BIN_OP, Some(n), b, 0, 0);
                    self.store_name(&id, Some(n));
                }
            }
            "Subscript" => {
                let tn = node_of(target).unwrap();
                let obj = nf(tn, "obj");
                let index = nf(tn, "index");
                self.compile_expr(obj);
                self.compile_expr(index);
                if opstr == "=" {
                    self.compile_expr(value);
                    self.emit(op::SET_ITEM, Some(n), 0, 0, 0);
                } else {
                    self.emit(op::DUP_TWO, Some(n), 0, 0, 0);
                    self.emit(op::GET_ITEM, Some(n), 0, 0, 0);
                    self.compile_expr(value);
                    let b = binop(&opstr);
                    self.emit(op::BIN_OP, Some(n), b, 0, 0);
                    self.emit(op::SET_ITEM, Some(n), 0, 0, 0);
                }
            }
            "Attr" => {
                let tn = node_of(target).unwrap();
                let attr = as_jstr(nf(tn, "attr")).to_string();
                let name_i = self.name_idx(&attr);
                let obj = nf(tn, "obj");
                self.compile_expr(obj);
                if opstr == "=" {
                    self.compile_expr(value);
                    self.emit(op::SET_ATTR, Some(n), name_i, 0, 0);
                } else {
                    self.emit(op::DUP_TOP, Some(n), 0, 0, 0);
                    self.emit(op::GET_ATTR, Some(n), name_i, 0, 0);
                    self.compile_expr(value);
                    let b = binop(&opstr);
                    self.emit(op::BIN_OP, Some(n), b, 0, 0);
                    self.emit(op::SET_ATTR, Some(n), name_i, 0, 0);
                }
            }
            _ => panic!("不支持的赋值目标"),
        }
    }

    fn c_if(&mut self, n: &Node) {
        let mut end_jumps: Vec<i64> = Vec::new();
        for br in as_list(nf(n, "branches")) {
            let items = as_list(br);
            if items.len() < 2 {
                continue;
            }
            self.compile_expr(&items[0]);
            let jf = self.emit(op::POP_JUMP_IF_FALSE, node_of(&items[0]), 0, 0, 0);
            self.compile_body(as_list(&items[1]));
            end_jumps.push(self.emit(op::JUMP, Some(n), 0, 0, 0));
            let here = self.here();
            self.patch(jf, here, 'a');
        }
        let orelse = nf(n, "orelse");
        if !is_null(orelse) {
            self.compile_body(as_list(orelse));
        }
        let end = self.here();
        for j in end_jumps {
            self.patch(j, end, 'a');
        }
    }

    fn c_for(&mut self, n: &Node) {
        let target = nf(n, "target");
        let id = as_jstr(nf(node_of(target).unwrap(), "id")).to_string();
        self.compile_expr(nf(n, "iter"));
        self.emit(op::GET_ITER, Some(n), 0, 0, 0);
        let setup = self.emit(op::SETUP_LOOP, Some(n), 0, 0, 0);
        let top = self.here();
        let patch_top = self.emit(op::FOR_ITER, Some(n), 0, 0, 0);
        self.store_name(&id, Some(n));
        self.compile_body(as_list(nf(n, "body")));
        self.emit(op::JUMP, Some(n), top, 0, 0);
        // 迭代结束：FOR_ITER 已弹出迭代器
        let exit_iter = self.here();
        self.emit(op::POP_BLOCK, Some(n), 0, 0, 0);
        let end_jump = self.emit(op::JUMP, Some(n), 0, 0, 0);
        // 中断：栈被回退到 SETUP_LOOP 处（迭代器还在），手动弹出
        let cleanup = self.here();
        self.emit(op::POP_TOP, Some(n), 0, 0, 0);
        self.emit(op::POP_BLOCK, Some(n), 0, 0, 0);
        let end = self.here();
        self.patch(patch_top, exit_iter, 'a');
        self.patch(end_jump, end, 'a');
        self.patch(setup, top, 'a');        // 继续 → 回到 FOR_ITER
        self.patch(setup, cleanup, 'b');    // 中断 → 清理后出循环
    }

    fn c_loop(&mut self, n: &Node) {
        let slot = self.new_tmp();
        self.compile_expr(nf(n, "times"));
        self.emit(op::LOOP_SETUP, Some(n), slot, 0, 0);
        let setup = self.emit(op::SETUP_LOOP, Some(n), 0, 0, 0);
        let top = self.here();
        self.emit(op::LOAD_FAST, Some(n), slot, 0, 0);
        let c0 = self.const_idx(Const::Int(0));
        self.emit(op::LOAD_CONST, Some(n), c0, 0, 0);
        self.emit(op::COMPARE, Some(n), op::CMPOP_GT, 0, 0);
        let to_exit = self.emit(op::POP_JUMP_IF_FALSE, Some(n), 0, 0, 0);
        self.compile_body(as_list(nf(n, "body")));
        let cont = self.here();
        self.emit(op::LOAD_FAST, Some(n), slot, 0, 0);
        let c1 = self.const_idx(Const::Int(1));
        self.emit(op::LOAD_CONST, Some(n), c1, 0, 0);
        self.emit(op::BIN_OP, Some(n), op::BINOP_SUB, 0, 0);
        self.emit(op::STORE_FAST, Some(n), slot, 0, 0);
        self.emit(op::JUMP, Some(n), top, 0, 0);
        let exit_at = self.here();
        self.emit(op::POP_BLOCK, Some(n), 0, 0, 0);
        self.patch(to_exit, exit_at, 'a');
        self.patch(setup, cont, 'a');
        self.patch(setup, exit_at, 'b');
    }

    fn c_while(&mut self, n: &Node) {
        let setup = self.emit(op::SETUP_LOOP, Some(n), 0, 0, 0);
        let top = self.here();
        self.compile_expr(nf(n, "test"));
        let to_exit = self.emit(op::POP_JUMP_IF_FALSE, Some(n), 0, 0, 0);
        self.compile_body(as_list(nf(n, "body")));
        self.emit(op::JUMP, Some(n), top, 0, 0);
        let exit_at = self.here();
        self.emit(op::POP_BLOCK, Some(n), 0, 0, 0);
        self.patch(to_exit, exit_at, 'a');
        self.patch(setup, top, 'a');
        self.patch(setup, exit_at, 'b');
    }

    /// 把 FuncDef / Lambda 编译成**一个留在栈上的函数对象**。
    fn emit_function_object(&mut self, n: &Node) {
        let sub = *self.func_scopes.get(&nid(n)).expect("函数作用域");
        let code = self.make_code(n, sub);
        let code_idx = self.m.codes.len();
        self.m.codes.push(code);

        // 闭包：按子作用域 freevars 顺序，从当前帧取单元
        let freevars = self.m.codes[code_idx].freevars.clone();
        let cur = self.scope;
        for name in &freevars {
            let idx = self.scopes[cur].cell_index(name);
            self.emit(op::LOAD_CELL, Some(n), idx, 0, 0);
        }
        // 默认参数：在定义处作用域求值，压栈（M7）
        let mut ndefaults = 0i64;
        for d in as_list(nf(n, "defaults")) {
            if !is_null(d) {
                self.compile_expr(d);
                ndefaults += 1;
            }
        }
        self.emit(op::MAKE_FUNCTION, Some(n), code_idx as i64,
                  freevars.len() as i64, ndefaults);

        // 进入函数体继续发射
        let sub_code = std::mem::take(&mut self.m.codes[code_idx]);
        let saved_code = std::mem::replace(&mut self.code, sub_code);
        let saved_scope = self.scope;
        self.scope = sub;
        self.c_check_annotations(n);
        self.compile_body(as_list(nf(n, "body")));
        // 隐式「返回 空」：与树遍历一致（exec_block 跑完返回 None）
        let none_i = self.const_idx(Const::None);
        self.emit(op::LOAD_CONST, Some(n), none_i, 0, 0);
        self.emit(op::RETURN, Some(n), 0, 0, 0);
        let done = std::mem::replace(&mut self.code, saved_code);
        self.m.codes[code_idx] = done;
        self.scope = saved_scope;
    }

    fn c_funcdef(&mut self, n: &Node) {
        self.emit_function_object(n);
        let name = as_jstr(nf(n, "name")).to_string();
        self.store_name(&name, Some(n));
    }

    fn c_classdef(&mut self, n: &Node) {
        // 类定义本身也是一条语句，先占一条指令（见 Python 侧的长注释）
        let (nl, nc) = jpos(n);
        self.emit_nop(nl, nc);
        let base = nf(n, "base");
        if !is_null(base) {
            let b = as_jstr(base).to_string();
            self.load_name(&b, Some(n));
        }

        let mut n_methods = 0i64;
        // 类变量：(属性名, 槽位, 行, 列) —— 值先算好，最后挂上
        let mut value_slots: Vec<(String, i64, i64, i64)> = Vec::new();
        for node in as_list(nf(n, "body")) {
            let m = match node_of(node) {
                Some(m) => m,
                None => continue,
            };
            match m.kind {
                "Pass" => {
                    let (l, c) = jpos(m);
                    self.emit_nop(l, c);
                }
                "Assign" => {
                    let start = self.code.instrs.len();
                    let slot = self.new_tmp();
                    self.compile_expr(nf(m, "value"));
                    self.emit(op::STORE_FAST, Some(m), slot, 0, 0);
                    let attr = as_jstr(nf(node_of(nf(m, "target")).unwrap(), "id"))
                        .to_string();
                    let (l, c) = jpos(m);
                    value_slots.push((attr, slot, l, c));
                    self.mark_start(start, jline(m));
                }
                "FuncDef" => {
                    let start = self.code.instrs.len();
                    let sub = *self.func_scopes.get(&nid(m)).expect("方法作用域");
                    let code = self.make_code(m, sub);
                    let code_idx = self.m.codes.len();
                    self.m.codes.push(code);
                    let freevars = self.m.codes[code_idx].freevars.clone();
                    let cur = self.scope;
                    for name in &freevars {
                        let idx = self.scopes[cur].cell_index(name);
                        self.emit(op::LOAD_CELL, Some(m), idx, 0, 0);
                    }
                    let mut ndefaults = 0i64;
                    for d in as_list(nf(m, "defaults")) {
                        if !is_null(d) {
                            self.compile_expr(d);
                            ndefaults += 1;
                        }
                    }
                    self.emit(op::MAKE_FUNCTION, Some(m), code_idx as i64,
                              freevars.len() as i64, ndefaults);
                    n_methods += 1;
                    // 进入方法体继续发射
                    let sub_code = std::mem::take(&mut self.m.codes[code_idx]);
                    let saved_code = std::mem::replace(&mut self.code, sub_code);
                    let saved_scope = self.scope;
                    self.scope = sub;
                    self.c_check_annotations(m);
                    self.compile_body(as_list(nf(m, "body")));
                    let none_i = self.const_idx(Const::None);
                    self.emit(op::LOAD_CONST, Some(m), none_i, 0, 0);
                    self.emit(op::RETURN, Some(m), 0, 0, 0);
                    let done = std::mem::replace(&mut self.code, saved_code);
                    self.m.codes[code_idx] = done;
                    self.scope = saved_scope;
                    self.mark_start(start, jline(m));
                }
                _ => {}
            }
        }

        // 栈 [基类?, 方法..., 类名] → 类
        let cname = as_jstr(nf(n, "name")).to_string();
        let ci = self.const_idx(Const::Str(cname));
        self.emit(op::LOAD_CONST, Some(n), ci, 0, 0);
        let has_base = if is_null(nf(n, "base")) { 0 } else { 1 };
        self.emit(op::BUILD_CLASS, Some(n), n_methods, has_base, 0);

        // 类变量：类造好后逐个挂上
        for (attr, slot, l, c) in value_slots {
            self.push_instr(op::DUP_TOP, 0, 0, 0, l, c);
            self.push_instr(op::LOAD_FAST, slot, 0, 0, l, c);
            let ni = self.name_idx(&attr);
            self.push_instr(op::SET_ATTR, ni, 0, 0, l, c);
        }
        let name = as_jstr(nf(n, "name")).to_string();
        self.store_name(&name, Some(n));
    }

    /// 在函数体最前面发射「按类型标注校验实参」（M24.3）。
    fn c_check_annotations(&mut self, n: &Node) {
        let anns = as_list(nf(n, "annotations"));
        if anns.is_empty() {
            return;
        }
        let params = str_list(nf(n, "params"));
        let checked: Vec<(usize, String)> = anns
            .iter()
            .enumerate()
            .filter(|(_, a)| !is_null(a) && !as_jstr(a).is_empty())
            .map(|(i, a)| (i, as_jstr(a).to_string()))
            .collect();
        if checked.is_empty() {
            return;
        }

        self.load_name("检查实参", Some(n));
        // 扁平描述：[参数名, 类型名, …]
        for (i, name) in &checked {
            let p = params[*i].clone();
            let ci = self.const_idx(Const::Str(p));
            self.emit(op::LOAD_CONST, Some(n), ci, 0, 0);
            let ci2 = self.const_idx(Const::Str(name.clone()));
            self.emit(op::LOAD_CONST, Some(n), ci2, 0, 0);
        }
        self.emit(op::BUILD_LIST, Some(n), (checked.len() * 2) as i64, 0, 0);
        // 实参值列表（顺序与描述一致）
        for (i, _) in &checked {
            let p = params[*i].clone();
            self.load_name(&p, Some(n));
        }
        self.emit(op::BUILD_LIST, Some(n), checked.len() as i64, 0, 0);
        let fname = as_jstr(nf(n, "name")).to_string();
        let ci3 = self.const_idx(Const::Str(fname));
        self.emit(op::LOAD_CONST, Some(n), ci3, 0, 0);
        self.emit(op::CALL, Some(n), 3, -1, 0);
        self.emit(op::POP_TOP, Some(n), 0, 0, 0);
    }

    fn make_code(&mut self, n: &Node, scope: usize) -> Code {
        let cellnames: Vec<String> = self.scopes[scope].cellvars.iter().cloned().collect();
        let cellset: BTreeSet<String> = cellnames.iter().cloned().collect();
        let params = str_list(nf(n, "params"));

        let mut locals_ordered: Vec<String> =
            params.iter().filter(|p| !cellset.contains(*p)).cloned().collect();
        let params_set: BTreeSet<String> = params.iter().cloned().collect();
        let rest: Vec<String> = self.scopes[scope]
            .bound
            .iter()
            .filter(|b| !cellset.contains(*b) && !params_set.contains(*b))
            .cloned()
            .collect();
        locals_ordered.extend(rest);

        // 参数槽位映射：被捕获的参数进闭包单元，其余按出现顺序占局部槽
        let mut param_local: Vec<i64> = Vec::new();
        let mut param_cell: Vec<i64> = Vec::new();
        let mut slot = 0i64;
        for p in &params {
            if cellset.contains(p) {
                param_local.push(-1);
                param_cell.push(cellnames.iter().position(|x| x == p).unwrap() as i64);
            } else {
                param_local.push(slot);
                param_cell.push(-1);
                slot += 1;
            }
        }

        // 「本函数赋过值、外层也有同名变量」的槽位提示
        let slot_of: HashMap<String, i64> = locals_ordered
            .iter()
            .enumerate()
            .map(|(i, x)| (x.clone(), i as i64))
            .collect();
        let mut outer_bound: BTreeSet<String> = BTreeSet::new();
        let mut e = self.scopes[scope].parent;
        while let Some(cur) = e {
            outer_bound.extend(self.scopes[cur].bound.iter().cloned());
            outer_bound.extend(self.scopes[cur].cellvars.iter().cloned());
            e = self.scopes[cur].parent;
        }
        let mut shadowed: Vec<String> = locals_ordered
            .iter()
            .filter(|x| outer_bound.contains(*x))
            .cloned()
            .collect();
        for x in cellset.iter() {
            if outer_bound.contains(x) && !shadowed.contains(x) {
                shadowed.push(x.clone());
            }
        }
        shadowed.sort();
        let mut local_hints: Vec<(i64, String)> = Vec::new();
        for name in shadowed {
            if let Some(s) = slot_of.get(&name) {
                local_hints.push((*s, UNBOUND_SHADOW_HINT.replace("{name}", &name)));
            }
        }

        let param_kind = {
            let k = int_list(nf(n, "param_kind"));
            if k.is_empty() {
                vec![op::PARAM_NORMAL; params.len()]
            } else {
                k
            }
        };
        let firstlineno = match nf(n, "line") {
            J::Int(i) => *i,
            _ => 1,
        };
        let freevars: Vec<String> = self.scopes[scope].freevars.iter().cloned().collect();

        // ⚠️ `param_idx` 必须**在这里**登记进名字表 —— Python 的 `_make_code`
        // 就是在这个时机调 `self.name_idx(p)` 的，而它会改变 names 的**顺序**。
        // 挪到后面补算，产物就不再逐字节相同。
        let param_idx: Vec<i64> = params.iter().map(|p| self.name_idx(p)).collect();

        Code {
            name: as_jstr(nf(n, "name")).to_string(),
            params,
            param_idx,
            param_local,
            param_cell,
            param_kind,
            instrs: Vec::new(),
            nlocals: locals_ordered.len() as i64,
            local_names: locals_ordered,
            cellvars: cellnames,
            freevars,
            local_hints,
            firstlineno,
        }
    }

    fn c_import(&mut self, n: &Node) {
        let alias = as_jstr(nf(n, "alias")).to_string();
        let mut alias_i = -1i64;
        if !alias.is_empty() {
            alias_i = self.name_idx(&alias);
        }
        // b 编码导入来源：0=标准库 1=python 2=本地包
        let src = if as_bool(nf(n, "from_python")) {
            1
        } else if as_bool(nf(n, "from_local")) {
            2
        } else {
            0
        };
        let name = as_jstr(nf(n, "name")).to_string();
        let ni = self.name_idx(&name);
        self.emit(op::IMPORT, Some(n), ni, src, alias_i);
        let bind = if alias.is_empty() { name } else { alias };
        self.store_name(&bind, Some(n));
    }

    fn c_try(&mut self, n: &Node) {
        let setup = self.emit(op::SETUP_TRY, Some(n), 0, 0, 0);
        self.compile_body(as_list(nf(n, "body")));
        self.emit(op::POP_TRY, Some(n), 0, 0, 0);
        let to_fin = self.emit(op::JUMP, Some(n), 0, 0, 0);   // 正常路径 → fin_start

        let fin = nf(n, "finalbody");
        let has_fin = !is_null(fin);

        let catch_at = self.here();
        let to_reraise = self.emit(op::CHECK_SIGNAL, Some(n), 0, 0, 0);
        let mut handler_done: Vec<i64> = Vec::new();
        // ⚠️ **内层 SETUP_TRY 覆盖捕获体**（R6 收口，D73 §五，与 Python `_c_try` 逐字同源）：
        // 异常分发到 catch 时外层 SETUP_TRY 已被弹掉，捕获体里 `返回`/`中断`/`继续` 会
        // 跳过 finally；给捕获体再包一层只有 finally 的 handler（catch_ip=catch_body_err）。
        let mut inner_setups: Vec<i64> = Vec::new();
        for h in as_list(nf(n, "handlers")) {
            let hn = match node_of(h) {
                Some(x) => x,
                None => continue,
            };
            let ty = nf(hn, "type");
            let hname = as_jstr(nf(hn, "name")).to_string();
            let mut to_next: Option<i64> = None;
            if !is_null(ty) {
                // ⚠️ 这里**不能**先 DUP_TOP（2026-09-26 修过的坑，见 Python 侧注释）
                self.compile_expr(ty);                              // [err, cond]
                self.emit(op::MATCH_EXC, Some(hn), 0, 0, 0);         // [err, bool]
                to_next = Some(self.emit(op::POP_JUMP_IF_FALSE, Some(hn), 0, 0, 0));
                if !hname.is_empty() {
                    self.store_name(&hname, Some(hn));
                } else {
                    self.emit(op::POP_TOP, Some(hn), 0, 0, 0);
                }
            } else {
                // 裸捕获：全接（信号已被 CHECK_SIGNAL 排除在 reraise）
                if !hname.is_empty() {
                    self.store_name(&hname, Some(hn));
                } else {
                    self.emit(op::POP_TOP, Some(hn), 0, 0, 0);
                }
            }
            // ⚠️ 内层 SETUP_TRY（只有带「最终」块才需要）。a/b 稍后回填。
            if has_fin {
                inner_setups.push(self.emit(op::SETUP_TRY, Some(hn), 0, 0, 0));
            }
            self.compile_body(as_list(nf(hn, "body")));
            if has_fin {
                self.emit(op::POP_TRY, Some(hn), 0, 0, 0);
            }
            handler_done.push(self.emit(op::JUMP, Some(hn), 0, 0, 0));
            if let Some(tn) = to_next {
                let here = self.here();
                self.patch(tn, here, 'a');     // 不匹配 → 下一个捕获
            }
        }

        // 未匹配路径：执行 finally 后继续抛（栈顶的 err 由 THROW 弹出）。
        // 也是「信号（尝试体/捕获体）」的重抛入口 —— 三者都走「finally + THROW」。
        let reraise_at = self.here();
        if has_fin {
            self.compile_body(as_list(fin));
        }
        self.emit(op::THROW, Some(n), 0, 0, 0);

        // 捕获体自己抛出的新异常：直接重抛（**不跑 finally**）。只有有捕获体才有这条路。
        let mut catch_body_err: Option<i64> = None;
        let mut to_reraise2: Option<i64> = None;
        if !inner_setups.is_empty() {
            catch_body_err = Some(self.here());
            to_reraise2 = Some(self.emit(op::CHECK_SIGNAL, Some(n), 0, 0, 0));
            self.emit(op::THROW, Some(n), 0, 0, 0);   // 错误 → 直接重抛
        }

        // 正常路径的 finally
        let fin_start = self.here();
        if has_fin {
            self.compile_body(as_list(fin));
            self.emit(op::END_FINALLY, Some(n), 0, 0, 0);
        }

        self.patch(setup, catch_at, 'a');
        self.patch(setup, if has_fin { fin_start } else { -1 }, 'b');
        self.patch(to_fin, fin_start, 'a');
        self.patch(to_reraise, reraise_at, 'a');
        if let Some(j) = to_reraise2 {
            self.patch(j, reraise_at, 'a');
        }
        for j in inner_setups {
            self.patch(j, catch_body_err.unwrap(), 'a');
            self.patch(j, fin_start, 'b');
        }
        for j in handler_done {
            self.patch(j, fin_start, 'a');
        }
    }

    // -- 表达式 -------------------------------------------------------------

    fn compile_expr(&mut self, e: &J) {
        let n = match node_of(e) {
            Some(n) => n,
            None => return,
        };
        match n.kind {
            "Num" | "Str" => {
                let ci = self.const_idx(const_from_j(nf(n, "value")));
                self.emit(op::LOAD_CONST, Some(n), ci, 0, 0);
            }
            "Name" => {
                let id = as_jstr(nf(n, "id")).to_string();
                if id == "空" {
                    // 与树遍历一致：空 是常量，不走名字查找
                    let ci = self.const_idx(Const::None);
                    self.emit(op::LOAD_CONST, Some(n), ci, 0, 0);
                } else {
                    self.load_name(&id, Some(n));
                }
            }
            "List" => {
                let els = as_list(nf(n, "elements"));
                for el in els {
                    self.compile_expr(el);
                }
                self.emit(op::BUILD_LIST, Some(n), els.len() as i64, 0, 0);
            }
            "Dict" => {
                let keys = as_list(nf(n, "keys"));
                let values = as_list(nf(n, "values"));
                for (k, v) in keys.iter().zip(values.iter()) {
                    self.compile_expr(k);
                    self.compile_expr(v);
                }
                self.emit(op::BUILD_DICT, Some(n), keys.len() as i64, 0, 0);
            }
            "Comprehension" => self.c_comprehension(n),
            "BinOp" => {
                let opstr = as_jstr(nf(n, "op")).to_string();
                self.compile_expr(nf(n, "left"));
                self.compile_expr(nf(n, "right"));
                let b = op::symbol_to_binop(&opstr).expect("二元运算符");
                self.emit(op::BIN_OP, Some(n), b, 0, 0);
            }
            "UnaryOp" => {
                let opstr = as_jstr(nf(n, "op")).to_string();
                self.compile_expr(nf(n, "operand"));
                let u = op::symbol_to_unop(&opstr).expect("一元运算符");
                self.emit(op::UNARY_OP, Some(n), u, 0, 0);
            }
            "BoolOp" => self.c_boolop(n),
            // 匿名函数：函数对象直接留在栈上当表达式结果
            "Lambda" => self.emit_function_object(n),
            "Compare" => self.c_compare(n),
            "Call" => self.c_call(n),
            "Attr" => {
                let attr = as_jstr(nf(n, "attr")).to_string();
                self.compile_expr(nf(n, "obj"));
                let ni = self.name_idx(&attr);
                self.emit(op::GET_ATTR, Some(n), ni, 0, 0);
            }
            // 切片本身也是值（M24.2）：读 a[1:3] 与写 a[1:3] = … 共用这条路径
            "Slice" => self.c_slice(n, n),
            "Subscript" => {
                self.compile_expr(nf(n, "obj"));
                self.compile_expr(nf(n, "index"));
                self.emit(op::GET_ITEM, Some(n), 0, 0, 0);
            }
            _ => panic!("还不支持的表达式：{}", n.kind),
        }
    }

    fn c_slice(&mut self, s: &Node, host: &Node) {
        let mut flags = 0i64;
        let start = nf(s, "start");
        if !is_null(start) {
            self.compile_expr(start);
            flags |= 1;
        }
        let stop = nf(s, "stop");
        if !is_null(stop) {
            self.compile_expr(stop);
            flags |= 2;
        }
        let step = nf(s, "step");
        if !is_null(step) {
            self.compile_expr(step);
            flags |= 4;
        }
        self.emit(op::BUILD_SLICE, Some(host), flags, 0, 0);
    }

    fn c_boolop(&mut self, n: &Node) {
        let opstr = as_jstr(nf(n, "op")).to_string();
        // 与树遍历一致：结果一定是真/假，不是短路值本身
        let jump_op = if opstr == "与" {
            op::POP_JUMP_IF_FALSE
        } else {
            op::POP_JUMP_IF_TRUE
        };
        let values = as_list(nf(n, "values"));
        let nvals = values.len();
        let mut to_end: Vec<i64> = Vec::new();
        for v in &values[..nvals - 1] {
            self.compile_expr(v);
            to_end.push(self.emit(jump_op, node_of(v), 0, 0, 0));
        }
        self.compile_expr(&values[nvals - 1]);
        let last_jump = self.emit(jump_op, Some(n), 0, 0, 0);
        // 全部通过
        let ci = self.const_idx(Const::Bool(opstr == "与"));
        self.emit(op::LOAD_CONST, Some(n), ci, 0, 0);
        let end_jump = self.emit(op::JUMP, Some(n), 0, 0, 0);
        let short_at = self.here();
        let ci2 = self.const_idx(Const::Bool(opstr != "与"));
        self.emit(op::LOAD_CONST, Some(n), ci2, 0, 0);
        let end = self.here();
        for j in to_end {
            self.patch(j, short_at, 'a');
        }
        self.patch(last_jump, short_at, 'a');
        self.patch(end_jump, end, 'a');
    }

    fn c_compare(&mut self, n: &Node) {
        let ops = str_list(nf(n, "ops"));
        let comparators = as_list(nf(n, "comparators"));
        let nops = ops.len();
        self.compile_expr(nf(n, "left"));
        let mut short_jumps: Vec<i64> = Vec::new();
        for (i, (o, comp)) in ops.iter().zip(comparators.iter()).enumerate() {
            let last = i == nops - 1;
            self.compile_expr(comp);
            if !last {
                self.emit(op::DUP_TOP, Some(n), 0, 0, 0);
                self.emit(op::ROT_THREE, Some(n), 0, 0, 0);   // [L,R,R] → [R,L,R]
            }
            let cop = op::symbol_to_cmpop(o).expect("比较运算符");
            self.emit(op::COMPARE, Some(n), cop, 0, 0);
            if !last {
                short_jumps.push(self.emit(op::JUMP_IF_FALSE_OR_POP, Some(n), 0, 0, 0));
            }
        }
        // 走到这里说明每一对都成立，栈顶就是最终布尔值
        if !short_jumps.is_empty() {
            let end_jump = self.emit(op::JUMP, Some(n), 0, 0, 0);
            // 短路出口：栈上是 [中间值, 假] → 交换后丢掉中间值，只留「假」
            let short_at = self.here();
            self.emit(op::ROT_TWO, Some(n), 0, 0, 0);
            self.emit(op::POP_TOP, Some(n), 0, 0, 0);
            let end = self.here();
            for j in short_jumps {
                self.patch(j, short_at, 'a');
            }
            self.patch(end_jump, end, 'a');
        }
    }

    fn c_call(&mut self, n: &Node) {
        let func = nf(n, "func");
        let args = as_list(nf(n, "args"));
        let keywords = as_list(nf(n, "keywords"));
        // M11 优化：X.追加(单参数) → LIST_APPEND 指令
        if keywords.is_empty() && args.len() == 1 && kd(func) == "Attr" {
            let f = node_of(func).unwrap();
            if as_jstr(nf(f, "attr")) == "追加" {
                self.compile_expr(nf(f, "obj"));
                self.compile_expr(&args[0]);
                self.emit(op::LIST_APPEND, Some(n), 0, 0, 0);
                return;
            }
        }
        // M48 阶段三② 方法调用融合（**只有 C VM 该打开**）
        if self.fuse_method_call && keywords.is_empty() && kd(func) == "Attr" {
            let f = node_of(func).unwrap();
            self.compile_expr(nf(f, "obj"));
            for a in args {
                self.compile_expr(a);
            }
            let attr = as_jstr(nf(f, "attr")).to_string();
            let ni = self.name_idx(&attr);
            let (_, fcol) = jpos(f);
            self.emit(op::METHOD_CALL, Some(n), args.len() as i64, ni, fcol);
            return;
        }
        self.compile_expr(func);
        for a in args {
            self.compile_expr(a);
        }
        let mut kw_i = -1i64;
        if !keywords.is_empty() {
            let names: Vec<String> = keywords
                .iter()
                .map(|k| {
                    let pair = as_list(k);
                    if pair.is_empty() {
                        String::new()
                    } else {
                        as_jstr(&pair[0]).to_string()
                    }
                })
                .collect();
            kw_i = self.kw_idx(names);
            for k in keywords {
                let pair = as_list(k);
                if pair.len() >= 2 {
                    self.compile_expr(&pair[1]);
                }
            }
        }
        self.emit(op::CALL, Some(n), args.len() as i64, kw_i, 0);
    }

    fn c_comprehension(&mut self, n: &Node) {
        let tmp = self.new_tmp();
        let kind = as_jstr(nf(n, "kind")).to_string();
        if kind == "list" {
            self.emit(op::BUILD_LIST, Some(n), 0, 0, 0);
        } else {
            self.emit(op::BUILD_DICT, Some(n), 0, 0, 0);
        }
        self.emit(op::STORE_FAST, Some(n), tmp, 0, 0);

        // 迭代
        self.compile_expr(nf(n, "iter"));
        self.emit(op::GET_ITER, Some(n), 0, 0, 0);
        let top = self.here();
        let patch_exit = self.emit(op::FOR_ITER, Some(n), 0, 0, 0);
        let target = nf(n, "target");
        let tname = as_jstr(nf(node_of(target).unwrap(), "id")).to_string();
        self.store_name(&tname, node_of(target));

        // 条件（可选）
        let mut skip: Option<i64> = None;
        let cond = nf(n, "condition");
        if !is_null(cond) {
            self.compile_expr(cond);
            skip = Some(self.emit(op::POP_JUMP_IF_FALSE, node_of(cond), 0, 0, 0));
        }

        // 追加元素
        if kind == "list" {
            self.emit(op::LOAD_FAST, Some(n), tmp, 0, 0);
            let ni = self.name_idx("追加");
            self.emit(op::GET_ATTR, Some(n), ni, 0, 0);
            self.compile_expr(nf(n, "elt"));
            self.emit(op::CALL, Some(n), 1, -1, 0);
            self.emit(op::POP_TOP, Some(n), 0, 0, 0);     // 追加返回 空，丢弃
        } else {
            self.emit(op::LOAD_FAST, Some(n), tmp, 0, 0);
            self.compile_expr(nf(n, "key"));
            self.compile_expr(nf(n, "value"));
            self.emit(op::SET_ITEM, Some(n), 0, 0, 0);
        }

        let cont = self.here();
        if let Some(sk) = skip {
            self.patch(sk, cont, 'a');
        }
        self.emit(op::JUMP, Some(n), top, 0, 0);

        let exit_at = self.here();
        self.patch(patch_exit, exit_at, 'a');
        self.emit(op::LOAD_FAST, Some(n), tmp, 0, 0);     // 结果入栈
    }
}

// ---------------------------------------------------------------------------
// 常量：AST 字面量 → 常量池项
// ---------------------------------------------------------------------------

fn const_from_j(v: &J) -> Const {
    match v {
        J::Null => Const::None,
        J::Bool(b) => Const::Bool(*b),
        J::Int(i) => Const::Int(*i),
        J::Big(s) => Const::Big(s.clone()),
        J::Float(f) => Const::Float(*f),
        J::Str(s) => Const::Str(s.clone()),
        // 到不了（Num/Str 的 value 一定是标量）。真到了就当场炸 ——
        // 常量池出现非标量是编译器 bug，静默塞个空值进去最难查。
        J::List(_) | J::Node(_) => panic!("常量池里出现了非标量值"),
    }
}

// ---------------------------------------------------------------------------
// 对外入口
// ---------------------------------------------------------------------------

/// AST（`parser::J` 的 Program 节点）→ `Module`。
///
/// `lib_dir` 是「用基石写的标准库」目录（`jishi/stdlib-jishi/`）—— 由 Python
/// 侧传进来（Rust 侧不该去猜包在哪）。传 `None` 表示**关掉基石库层**。
pub fn compile_program(program: &Node, filename: &str,
                       lib_dir: Option<&str>, fuse_method_call: bool) -> Module {
    // M52：先把「用基石写的标准库」展开进本程序
    let expanded = jishilib::expand(program, lib_dir, filename);

    let m = Module {
        filename: filename.to_string(),
        codes: vec![Code {
            name: "<模块>".to_string(),
            firstlineno: 1,
            ..Default::default()
        }],
        ..Default::default()
    };

    let mut c = Collector { scopes: Vec::new(), func_scopes: HashMap::new() };
    let root = c.add(ScopeKind::Module, "<模块>", None);
    collect_stmts(as_list(nf(&expanded, "body")), root, &mut c);
    resolve(&mut c.scopes, root);

    // ⚠️ **绝不能把 body 克隆出来再编**：`func_scopes` 的键是节点地址，
    // 克隆一份就是另一批地址，`emit_function_object` 会当场找不到作用域
    // （第一版就是这么炸的）。`expanded` 必须活到编译结束。
    let mut em = Emitter::new(m, &c.scopes, &c.func_scopes, root, fuse_method_call);
    let main_code = std::mem::take(&mut em.m.codes[0]);
    em.code = main_code;
    em.compile_body(as_list(nf(&expanded, "body")));
    em.emit(op::HALT, None, 0, 0, 0);
    let done = std::mem::take(&mut em.code);
    em.m.codes[0] = done;
    em.m.main = 0;
    em.m
}
