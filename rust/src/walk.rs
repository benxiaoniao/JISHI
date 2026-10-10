//! **树遍历参考执行器**（R6 主体）—— 吃前端产的 **AST JSON**，直接遍历求值，
//! **不经过字节码**。
//!
//! ## 它是干什么用的
//!
//! 与字节码 VM **互为参考实现**：两个独立实现跑同一段源码，结果必须一致。
//! 这是本项目最看重的那条验证资产（`docs/架构迁移路径.md` §五风险 1：
//! 「失去独立实现互拍」）。字节码 VM 抓不出**编译器**的错（字节码生成错了、
//! VM 照着错的跑），而树遍历直接看 AST —— 两者的差异就是编译器的问题。
//!
//! ## 为什么吃 JSON 而不是「宿主自己 parse」
//!
//! AST 已经是**契约产物**（`docs/前端契约.md` §4.2 的 `ast` 阶段，两个前端
//! 都产它，`conformance.py emit --stage ast` 拿得到）。吃 JSON 有两层好处：
//! ① 宿主不必把 `core` 的 pyo3 依赖解耦进来（省一层麻烦）；
//! ② 与 R5「产物优先给文本」同一条思路 —— 宿主只认文本产物。
//!
//! ## ✅ 现在的进度（语料里出现过的节点**全部实现了**）
//!
//! `tests/cases/` 里的语料，树遍历执行器**全都跑得通**（`tools/check_walk.py` 的
//! 「还没实现」那一列是 **0**）。语句 / 表达式 / 赋值目标 / 函数与闭包 / 类与继承 /
//! 异常与导入 / 切片，一应俱全。具体随时可查：`python tools/check_walk.py`。
//! 语料里还没出现过的节点（`Comprehension` 推导式之类）会明确报「还没实现「X」」。
//!
//! ## ✅ 报错渲染也与字节码 VM 逐字一致（⑥）
//!
//! 源码行 / `^` / 调用链 / 提示 —— 都走**共用的** `render_error_with`（源码行
//! 从 `--walk` 的「源文件路径」参数读，AST JSON 里没有源码）。调用链由本执行器
//! 自己的 `call_stack` 拍出（只含「直接函数调用」的帧，见 `call_stack` 的注释）。
//!
//! ## 与字节码 VM 的关系
//!
//! **语义层是共用的**（`binop` / `compare` / `get_item` / `unpack_values` /
//! `make_iterator` / `display` / 内建表…都在 `lib.rs` 里），这里只新写
//! 「AST 遍历 + 作用域」那一层。这是**有意的**：要抓的是「编译器和求值」的差异，
//! 不是「两份 `+` 的实现哪里不一样」。
//!
//! ⚠️ 共用也意味着**共用的那个函数有 bug 时两边一起错** —— 但那样就只剩
//! 「树遍历 vs 字节码」比不出来，而 Python 侧（另一套实现）还能比出来。
//! 这是这套三层互拍的冗余度所在。

use crate::json::Json;
use crate::{
    as_int, bind_params, binop, call_builtin_method, compare, dict_new, display, err,
    err_with_hint, exception_matches, format_value_with, get_attr, get_item, iter_next, list_new,
    make_exception, make_iterator, name_hint, new_builtins, next_obj_id, reject_kwargs,
    set_attr, set_item, stdlib_module, truthy, type_label,
    unary_neg, unbound_hint, unbound_msg, unbound_shadow_hint, unpack_values,
    BoundMethod, BuiltinHost, Class, ExcValue, Instance, SliceVal, TraceFrame, Val,
    JishiError, T_NAME, T_NOT_CALLABLE,
};
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::sync::atomic::{AtomicUsize, Ordering};

/// 算术运算符 → `binop` 的编码。
///
/// ⚠️ 这张表**必须与 `core/src/opcodes.rs` 的 `BINOP_*` 一致** —— 那是字节码
/// 契约的一部分（`jishi/opcodes.py` 也同一套）。有测试盯着（`test_m75`）。
fn binop_code(op: &str) -> Option<i64> {
    Some(match op {
        "+" => 0,
        "-" => 1,
        "*" => 2,
        "/" => 3,
        "//" => 4,
        "%" => 5,
        "**" => 6,
        _ => return None,
    })
}

/// 比较运算符 → `compare` 的编码。
///
/// ⚠️ 同样必须与 `core/src/opcodes.rs` 的 `symbol_to_cmpop` 一致（有测试盯着）。
fn cmpop_code(op: &str) -> Option<i64> {
    Some(match op {
        "==" => 0,
        "!=" => 1,
        "<" => 2,
        ">" => 3,
        "<=" => 4,
        ">=" => 5,
        "在" => 6,
        "不在" => 7,
        "是" => 8,
        "不是" => 9,
        _ => return None,
    })
}

/// 控制流信号：**只有「跳出去」才返回非 `Normal`**，普通语句一律 `Normal`。
///
/// ⚠️ `返回` 归「函数与闭包」那一步（模块级没有 `返回`）。
enum Flow {
    Normal,
    Break,
    Continue,
    /// `返回 [值]` —— 只有函数体里会出现；`exec_body` **原样往上传**，
    /// 由 `call_walk_fn` 接住（不在函数里的 `返回` 前端会先拦下）。
    Return(Val),
}

/// 树遍历自己的函数值（`Val::WalkFn` 装的就是它）。
///
/// ⚠️ **为什么不能复用 `Val::Func`**：那里面装的是字节码的 `code_idx`，
/// 而树遍历手里是 AST、根本没有字节码 —— 「不经过字节码」正是它存在的理由。
pub(crate) struct WalkFn {
    /// 创建序号 —— 判**同一性**（`是` / 放进集合）用，思路与 `Func.id` 同源。
    pub(crate) id: usize,
    pub(crate) name: String,
    /// **共享的函数体**：方法绑定（`甲.方法`）时只换 `self_val`，
    /// 这一份用 `Rc` 共享 —— 否则每取一次属性就整份复制 AST 与方法体信息。
    def: Rc<FnDef>,
    /// **定义处的作用域** —— 闭包就是靠它：读的时候沿这条链往上找。
    closure: Rc<RefCell<Scope>>,
    /// 非 `None` = 这是个**绑定方法**（`甲.方法` / `超().方法`）：
    /// 调用时它会被放到第一个形参（`自身`）上，与字节码侧 `BoundMethod` 同语义。
    /// 显示 / 类型 / 同一性都要跟它走（见 `lib.rs` 的那三个分支）。
    pub(crate) self_val: Option<Val>,
}

/// 函数的「不随绑定而变」的那部分。
struct FnDef {
    params: Vec<String>,
    /// 形参种类（M25）：0=普通 1=`*参数` 2=`**选项`；与 `params` 等长。
    param_kind: Vec<i64>,
    /// 与 `params` 等长；**没有默认值**的位置放 `Val::None_`
    /// （与 `bind_params` 的约定一致，见那边的 `!matches!(dv, Val::None_)`）。
    defaults: Vec<Val>,
    body: Vec<Json>,
    /// 本函数体里「赋过值的名字」（**赋值即局部**）。读一个还没赋值的 →
    /// 报「赋值之前被用到了」，**不许穿透到外层**（Python 语义）。
    /// 用 `Rc` 是为了每次调用只加一次引用计数，不整份克隆这个集合。
    locals: Rc<HashSet<String>>,
}

impl WalkFn {
    /// 绑上实例 → 一个**绑定方法**（共享函数体与闭包，只多一个 `self_val`）。
    ///
    /// 与字节码侧的 `BoundMethod::User` 同一个意思；那边用「实例 + 字节码函数」，
    /// 这边用「实例 + AST 函数」。
    pub(crate) fn bind_self(&self, inst: Val) -> WalkFn {
        WalkFn {
            id: self.id,
            name: self.name.clone(),
            def: Rc::clone(&self.def),
            closure: Rc::clone(&self.closure),
            self_val: Some(inst),
        }
    }
}

static NEXT_FN_ID: AtomicUsize = AtomicUsize::new(1);

/// 词法作用域链（模块 → 函数 → …）。
///
/// ⚠️ 与字节码 VM 的「按名字预分配槽位 + 单元表」是**两套做法**：树遍历手里
/// 只有 AST，没有编译器算好的槽位。这里的规矩是 —— **读沿链往上找、写只写
/// 当前作用域**。这正是 Python 侧 `Environment`（`jishi/interpreter.py`）那套
/// 语义，也是「赋值即局部」的实现方式；两边跑同一段源码必须一样。
pub(crate) struct Scope {
    vars: HashMap<String, Val>,
    parent: Option<Rc<RefCell<Scope>>>,
}

impl Scope {
    /// 开一个子作用域（父 = 定义处的作用域 → 闭包）。
    fn child(parent: Rc<RefCell<Scope>>) -> Rc<RefCell<Scope>> {
        Rc::new(RefCell::new(Scope { vars: HashMap::new(), parent: Some(parent) }))
    }
    /// 沿链取值（**先看自己**）。
    fn lookup(&self, name: &str) -> Option<Val> {
        if let Some(v) = self.vars.get(name) {
            return Some(v.clone());
        }
        let mut cur = self.parent.clone();
        while let Some(s) = cur {
            let (found, up) = {
                let b = s.borrow();
                (b.vars.get(name).cloned(), b.parent.clone())
            };
            if found.is_some() {
                return found;
            }
            cur = up;
        }
        None
    }
    /// 链上**有没有**这个名字（不看值）—— 只用来挑「赋值前读局部」的提示语。
    fn has_name(&self, name: &str) -> bool {        if self.vars.contains_key(name) {
            return true;
        }
        let mut cur = self.parent.clone();
        while let Some(s) = cur {
            let (found, up) = {
                let b = s.borrow();
                (b.vars.contains_key(name), b.parent.clone())
            };
            if found {
                return true;
            }
            cur = up;
        }
        false
    }

    /// 这一层**自己**绑的名字（不含沿链的父层）—— 调试器「看变量」要的正是它。
    ///
    /// ⚠️ 只给**调试器**用（R7.5）。语言语义那边一律走 `lookup`（沿链找），
    /// 因为「赋值即局部 + 闭包读外层」就是要沿链。
    pub(crate) fn own_vars(&self) -> Vec<(String, Val)> {
        self.vars.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
    }
}
/// `循环 N` 的 N —— 与 Python 的 `int(n)` 对齐（树遍历执行器就是那么做的）：
/// 整数原样、布尔当 0/1、浮点**向零截断**（`int(3.7) == 3`）、文本能解析就用。
///
/// ⚠️ 文案与其它执行器逐字一致（`类型错误（「a」不是有效的循环次数）`）。
fn loop_count(v: &Val) -> Result<i128, JishiError> {
    let bad = || err("类型错误", format!("「{}」不是有效的循环次数", display(v)));
    Ok(match v {
        Val::Int(i) => *i,
        Val::Bool(b) => i128::from(*b),
        Val::Float(f) if f.is_finite() => *f as i128,
        Val::Str(s) => s.trim().parse::<i128>().map_err(|_| bad())?,
        _ => return Err(bad()),
    })
}

pub(crate) struct Walker {
    /// 模块作用域（链的根）。
    globals: Rc<RefCell<Scope>>,
    /// **当前**作用域：模块级就是 `globals`，进了函数就是那个函数的作用域。
    env: Rc<RefCell<Scope>>,
    /// 当前函数体里「赋过值的名字」；模块级是 `None`（模块级没有「赋值即局部」）。
    locals: Option<Rc<HashSet<String>>>,
    /// 内建表 —— **与字节码 VM 共用同一张**（`new_builtins`）。这是「内建语义
    /// 不漂」的保证：内建只有一份实现，两个执行器都调它。
    builtins: HashMap<String, Val>,
    /// **调用栈**（R6 主体 ⑥）：从外到内，只含「直接函数调用」的帧 —— 出错时拍成
    /// `JishiError.trace`（与字节码 VM 的 `snapshot_trace` 同口径）。
    /// ⚠️ 方法调用 / 类实例化 / 内建回调**不进栈**（Python 侧 `_eval_call` 只有
    /// `UserFunction` 直接调用才 append；`UserFunction.__call__` 那条路明确
    /// 「不动 _call_stack」）—— 所以这里只有 `eval_call` 的 `Val::WalkFn`（且
    /// 未绑定）会 push，别的地方调 `call_walk_fn` 一律传 `None`。
    call_stack: Vec<TraceFrame>,
    /// **模块顶层最后一个「表达式语句」的值**（R8.4b：REPL 回显用）。
    ///
    /// 语义与 Python 侧 `Interpreter._last_value` + 哨兵 `_NO_VALUE` 对齐：
    /// `None` = 「这一段里没有顶层表达式语句」，`Some(Val::None_)` = 「顶层表达式
    /// 的值**就是 `空`**」。两者**必须分得开** —— Python 当年用 `None` 一个值同时
    /// 表示这两件事，导致输入 `空` 不回显（M27 修的就是它）。
    last_value: Option<Val>,
    pub(crate) output: String,
    /// 错误流缓冲（与字节码 VM 同构；目前只有 `测试.汇总` 往里写）。
    pub(crate) output_err: String,
    /// 输出出口（R7.5）。`None` = 只攒在 `output` 缓冲里（CLI / `--walk` 就是
    /// 这样）；挂了就是**每写一次就报一次**（DAP 把每次 `write` 变成一条
    /// `output` 事件，粒度必须与 Python 的 `_StreamOut.write` 一致）。
    /// 参数 `(文本, 是不是 stderr)`。
    output_sink: Option<Box<dyn FnMut(&str, bool)>>,
    /// 调试钩子（R7.5）。`None` = 不挂调试器 —— 此时每个语句只多一次判空，
    /// 语义与性能都不变（这是「调试器不改语义」那条原则的实现方式）。
    ///
    /// ⚠️ **为什么是 `Option<Box<dyn>>` 而不是泛型**：泛型会给 `Walker` 多一个
    /// 类型参数，所有 `Walker::new()` 的调用点都要跟着写 `Walker::<NoDebug>::new()`
    /// —— 而调试只影响一个入口，不值得污染整个类型。
    debugger: Option<Box<dyn DebugHooks>>,
    /// 「调试器看到的当前作用域」——**只在类体里与 `env` 不同**。
    ///
    /// Python 侧 `_do_classdef` 是**在外层 env 里**逐句执行的（方法闭包也拿外层
    /// env，类变量另收一张表）；而 Rust 这边类体跑在**子作用域**里（方法闭包拿
    /// 子作用域，于是方法能直接读类变量）。两者的**语言语义**在语料上等价
    /// （`check_walk` / `check_engines` 全绿），但**调试器看到的变量表会不同**。
    /// 为了让 DAP 的报文与 Python 逐字节一致，这里显式记住「Python 会给哪个
    /// 作用域」，钩子优先用它。非类体时它是 `None`（= 就用 `env`）。
    debug_env: Option<Rc<RefCell<Scope>>>,
}

/// 调试器要执行器提供的三件事（R7.5）。
///
/// 📌 **为什么是「三件」而不是更多**：断点、单步、看变量、看调用栈、出错现场，
/// 全部可以由「每个语句执行前调一次」+「进出用户函数各调一次」推出来 ——
/// 这正是 Python 侧两条钩子（`before_stmt` / `push_frame`+`pop_frame`）的形状。
/// 钩子**只读**：它们不写作用域、不跳语句、不改控制流；唯一的例外是
/// `Err(调试退出)` 那一条信号（见 `lib.rs::DEBUG_QUIT`）。
pub(crate) trait DebugHooks {
    /// 每个语句执行前调用。`scope` = 调试器该看到的作用域。
    fn before_stmt(&mut self, walker: &mut Walker, stmt: &Json,
                   scope: &Rc<RefCell<Scope>>) -> Result<(), JishiError>;
    /// 进入一个**用户函数**（`直接函数调用`）时调用。
    fn push_frame(&mut self, name: &str, scope: &Rc<RefCell<Scope>>,
                  call_line: i64, call_col: i64);
    /// 离开那个函数时调用。
    fn pop_frame(&mut self);
}

impl Walker {
    pub(crate) fn new() -> Walker {
        let globals = Rc::new(RefCell::new(Scope { vars: HashMap::new(), parent: None }));
        Walker {
            env: Rc::clone(&globals),
            globals,
            locals: None,
            builtins: new_builtins(),
            call_stack: Vec::new(),
            last_value: None,
            output: String::new(),
            output_err: String::new(),
            output_sink: None,
            debugger: None,
            debug_env: None,
        }
    }

    /// 挂上「每写一次就报一次」的输出出口（R7.5 · DAP 用）。
    pub(crate) fn set_output_sink(&mut self, sink: Option<Box<dyn FnMut(&str, bool)>>) {
        self.output_sink = sink;
    }

    /// 挂上调试钩子（R7.5）；传 `None` 卸下。**只影响钩子调用，不改任何语义**。
    pub(crate) fn set_debugger(&mut self, dbg: Option<Box<dyn DebugHooks>>) {
        self.debugger = dbg;
    }

    /// 模块作用域（调试器看「模块顶层」那一帧要用它）。
    pub(crate) fn globals(&self) -> Rc<RefCell<Scope>> {
        Rc::clone(&self.globals)
    }

    /// 内建表的一份快照 `(名字, 值)` —— **只给调试器的「变量 全部」用**。
    ///
    /// 为什么要它：Python 把整套内建函数与异常类型装进**模块环境**，所以
    /// `变量 全部` 在模块顶层会一次列出三十多个；树遍历执行器的内建在
    /// `Walker::builtins` **另一张表**里。为了命令行会话与 Python **逐字一致**，
    /// 模块顶层那一帧要能把这批名字也列出来 —— 这是**纯显示**，不往任何作用域里
    /// 塞东西（调试器不改语义）。
    pub(crate) fn builtin_entries(&self) -> Vec<(String, Val)> {
        self.builtins.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
    }

    /// 查一个名字的**当前值**（调试器「查看 名字」直接用 `eval`，这个给
    /// 「这个名字到底绑没绑」这类判断用）。
    pub(crate) fn scope_of(&self) -> Rc<RefCell<Scope>> {
        Rc::clone(self.debug_env.as_ref().unwrap_or(&self.env))
    }

    /// 在**指定作用域**里求一个表达式的值（调试器「查看 表达式」用）。
    ///
    /// 做法是临时把「当前作用域」换成它再求值、求完换回来 —— 与 Python 侧
    /// `interp.eval_expr(node, env)` 的语义一致（那边是显式传 env）。
    /// 表达式求值只读，不写作用域（`lambda` 会捕获当前 `env`，所以必须换）。
    pub(crate) fn eval_in_scope(&mut self, node: &Json,
                                scope: &Rc<RefCell<Scope>>) -> Result<Val, JishiError> {
        let saved_env = Rc::clone(&self.env);
        let saved_locals = self.locals.take();
        self.env = Rc::clone(scope);
        let out = self.eval(node);
        self.env = saved_env;
        self.locals = saved_locals;
        out
    }

    /// 把钩子取出来单独跑一会儿 —— 钩子要在 `&mut Walker` 下工作，而它自己
    /// 就存在 `Walker` 里，所以必须先「取出来」再调（Rust 的借用规则）。
    /// 无论钩子返回什么（含 `Err`）都要放回去，否则调试器会**静默失效**。
    fn with_debugger<T>(
        &mut self,
        f: impl FnOnce(&mut dyn DebugHooks, &mut Walker) -> Result<T, JishiError>,
    ) -> Result<Option<T>, JishiError> {
        let mut dbg = match self.debugger.take() {
            Some(d) => d,
            None => return Ok(None),
        };
        let r = f(dbg.as_mut(), self);
        self.debugger = Some(dbg);
        r.map(Some)
    }

    /// `exec_stmt` 开头的那个钩子调用（**每个语句执行前**，含类体里的每一句）。
    fn hook_before_stmt(&mut self, stmt: &Json) -> Result<(), JishiError> {
        if self.debugger.is_none() {
            return Ok(());      // 热路径：没挂调试器时只花一次判空
        }
        let scope = self.scope_of();
        self.with_debugger(|d, w| d.before_stmt(w, stmt, &scope))?;
        Ok(())
    }

    /// 跑一棵 AST（`Program` 节点）。
    /// `run` 的 **REPL 版**：先清空 `last_value`，跑完把它取出来（R8.4b）。
    ///
    /// Python 侧每次执行前 `interp._last_value = _NO_VALUE`、跑完再看它有没有被赋值。
    /// 这里把「清 + 取」收进一个方法，免得调用方忘了清 —— 忘了就会把**上一次的值**
    /// 当成这一次的回显。
    pub(crate) fn run_capture(&mut self, ast: &Json) -> Result<Option<Val>, JishiError> {
        self.last_value = None;
        self.run(ast)?;
        Ok(self.last_value.take())
    }

    pub(crate) fn run(&mut self, ast: &Json) -> Result<(), JishiError> {
        let body = ast.get("body").and_then(|b| b.as_array())
            .ok_or_else(|| err("运行期错误", "AST 里没有 body（不是 Program 节点？）"))?;
        // 顶层不该出现 `跳出` / `继续`（前端在语法期就拦住了），返回值直接丢。
        self.exec_body(body)?;
        Ok(())
    }

    /// 把当前调用栈拍成调用链（只拍一次；已经有链就不动）。
    ///
    /// ⚠️ 口径照字节码 VM 的 `snapshot_trace`：树遍历的 `call_stack` **只含函数帧**
    /// （模块级不 push），所以不必像 VM 那样 `skip(1)` 跳过模块主帧。栈空时不拍
    /// （与 Python 侧 `_do_try` 的 `if err.trace is None and self._call_stack` 同义：
    /// 模块级没有调用链，也不该渲染「调用链」那一块）。
    fn snapshot_trace(&self, e: &mut JishiError) {
        if e.is_debug_quit() {
            return;                     // 「退出」不是错误，不拍调用链
        }
        if !e.trace.is_empty() || self.call_stack.is_empty() {
            return;
        }
        e.trace = self.call_stack.clone();
    }

    /// 错误冒泡时补「当前节点」的位置（**只补还没填过的**）。
    ///
    /// ⚠️ 对齐字节码 VM 的 `fill_error_loc`：那边用「出错指令」的 line/col，这边用
    /// 「出错节点」的 —— 同一条源码、同一处错误，两者天然一致（验证过：`1 / 0`
    /// 的 `BinOp` 节点 col 与 `BINARY_OP` 指令 col 都是 6）。共用函数（`binop` /
    /// `get_item` / `compare` …）返回的错误**不带位置**，就靠这里在递归冒泡时
    /// 由「最内层出错节点」补上（内层先补，外层不再覆盖）。
    fn fill_loc(&self, e: &mut JishiError, n: &Json) {
        if e.line.is_some() || e.is_debug_quit() {
            return;                     // 「退出」没有位置可言，别给它安一个
        }
        if let (Some(l), Some(c)) = self.at(n) {
            e.line = Some(l);
            e.col = Some(c);
        }
    }

    // -- 语句 ------------------------------------------------------------

    fn exec_stmt(&mut self, n: &Json) -> Result<Flow, JishiError> {
        // 调试钩子（R7.5）：**挂在这一处就够**——断点、单步（进/过/出）、看变量、
        // 看调用栈全都由此推出。类体里的每一句也走这里（`exec_classdef` 是逐句
        // 调 `exec_stmt` 的），所以「函数 叫(自身)：」那一行也停得住。
        self.hook_before_stmt(n)?;
        let r = match kind(n) {
            // ⚠️ 这几个 arm 用 `.map(|_| Flow::Normal)` 而不是 `{ f(n)?; Ok(..) }`：
            // `?` 会**提前 return、绕过下面的 `fill_loc`**，于是 `exec_import` 等的
            // 错误（如「不支持从 python 导入」）就没了位置、报错渲染缺源码行框。
            "Assign" => self.exec_assign(n).map(|_| Flow::Normal),
            "ExprStmt" => {
                let v = self.eval(field(n, "expr")?)?;
                // R8.4b：REPL 要回显「模块顶层最后一个表达式语句的值」。
                // ⚠️ 只在**模块级**记（`locals.is_none()`）—— 函数体里的表达式语句
                //    不该冒到 REPL 上面去（与 Python 侧 `_last_value` 同口径）。
                if self.locals.is_none() {
                    self.last_value = Some(v);
                }
                Ok(Flow::Normal)
            }
            "If" => self.exec_if(n),
            "While" => self.exec_while(n),
            "For" => self.exec_for(n),
            "Loop" => self.exec_loop(n),
            "FuncDef" => self.exec_funcdef(n).map(|_| Flow::Normal),
            "ClassDef" => self.exec_classdef(n).map(|_| Flow::Normal),
            "Return" => {
                // `value` 是 JSON `null` 时表示裸 `返回`（前端就是这么编的）
                let v = match n.get("value") {
                    Some(node) if !matches!(node, Json::Null) => self.eval(node)?,
                    _ => Val::None_,
                };
                Ok(Flow::Return(v))
            }
            "Try" => self.exec_try(n),
            "Raise" => self.exec_raise(n),
            "Import" => self.exec_import(n).map(|_| Flow::Normal),
            "Break" => Ok(Flow::Break),
            "Continue" => Ok(Flow::Continue),
            "Pass" => Ok(Flow::Normal),
            other => Err(self.unimpl(other, n)),
        };
        match r {
            Err(mut e) => {
                self.fill_loc(&mut e, n);
                Err(e)
            }
            ok => ok,
        }
    }

    /// 顺序执行一串语句；`跳出` / `继续` **原样往上传**（由最近的循环接住）。
    fn exec_body(&mut self, stmts: &[Json]) -> Result<Flow, JishiError> {
        for st in stmts {
            match self.exec_stmt(st)? {
                Flow::Normal => {}
                f => return Ok(f),
            }
        }
        Ok(Flow::Normal)
    }

    /// `如果 / 否则如果 / 否则`。
    ///
    /// ⚠️ AST 形状（`jishi/ast_nodes.py` 的 `If`）：`branches` 是
    /// `[[条件, 语句块], …]`（`否则如果` 会**多一条 branch**），最后的 `否则`
    /// 单独放在 `orelse`。
    ///
    /// ⚠️ **块不新开作用域** —— 与前端一致：这些块和外面是同一个作用域
    /// （所以 `如果` 里 `令` 的名字出去还在）。
    fn exec_if(&mut self, n: &Json) -> Result<Flow, JishiError> {
        let branches = n.get("branches").and_then(|v| v.as_array())
            .ok_or_else(|| self.bad("If 没有 branches（AST 不是前端产的？）", n))?;
        for br in branches {
            let pair = br.as_array()
                .ok_or_else(|| self.bad("If.branches 的一项不是 [条件, 块]", n))?;
            if pair.len() != 2 {
                return Err(self.bad("If.branches 的一项不是 [条件, 块]", n));
            }
            if truthy(self.eval(&pair[0])?) {
                let body = pair[1].as_array()
                    .ok_or_else(|| self.bad("If 的块不是语句列表", n))?;
                return self.exec_body(body);
            }
        }
        if let Some(orelse) = n.get("orelse").and_then(|v| v.as_array()) {
            return self.exec_body(orelse);
        }
        Ok(Flow::Normal)
    }

    /// `当 条件：`。
    ///
    /// ⚠️ 字段名是 **`test`** 不是 `cond`（`ast_nodes.While`）。
    fn exec_while(&mut self, n: &Json) -> Result<Flow, JishiError> {
        let test = field(n, "test")?;
        let body = stmts_of(n, "body")?;
        while truthy(self.eval(test)?) {
            match self.exec_body(body)? {
                Flow::Break => break,
                Flow::Normal | Flow::Continue => {}
                // ⚠️ `返回` 要**穿过循环**往外传 —— 只有函数体能接住它。
                // （写成 `_ => {}` 就会把 `返回` 吞掉，函数白跑一趟还回 `空`。）
                f @ Flow::Return(_) => return Ok(f),
            }
        }
        Ok(Flow::Normal)
    }

    /// `遍历 目标 在 可迭代对象：`。
    fn exec_for(&mut self, n: &Json) -> Result<Flow, JishiError> {
        let iterable = self.eval(field(n, "iter")?)?;
        let target = field(n, "target")?;
        let body = stmts_of(n, "body")?;
        // 走宿主接口的 `make_iter`：容器 / 文本 / 文件走共用的 `make_iterator`
        // （R6.1 修的那个 O(n²) 就在它里面），**自定义对象会先调它的「迭代()」**
        // —— 与字节码 VM 的 `GET_ITER` 同一条路，不能自己另写一套。
        let mut it = self.make_iter(iterable)?;
        while let Some(v) = iter_next(&mut it)? {
            self.assign_to(target, v)?;
            match self.exec_body(body)? {
                Flow::Break => break,
                Flow::Normal | Flow::Continue => {}
                // ⚠️ `返回` 要**穿过循环**往外传 —— 只有函数体能接住它。
                // （写成 `_ => {}` 就会把 `返回` 吞掉，函数白跑一趟还回 `空`。）
                f @ Flow::Return(_) => return Ok(f),
            }
        }
        Ok(Flow::Normal)
    }

    /// `循环 N：` —— **跑 N 次**（不是「真就一直循环」）。
    /// 次数在**进入循环之前**算好（与树遍历的 `_do_loop` 一致）。
    fn exec_loop(&mut self, n: &Json) -> Result<Flow, JishiError> {
        let times = self.eval(field(n, "times")?)?;
        let count = loop_count(&times)?;
        let body = stmts_of(n, "body")?;
        let mut i: i128 = 0;
        while i < count {
            i += 1;
            match self.exec_body(body)? {
                Flow::Break => break,
                Flow::Normal | Flow::Continue => {}
                // ⚠️ `返回` 要**穿过循环**往外传 —— 只有函数体能接住它。
                // （写成 `_ => {}` 就会把 `返回` 吞掉，函数白跑一趟还回 `空`。）
                f @ Flow::Return(_) => return Ok(f),
            }
        }
        Ok(Flow::Normal)
    }

    /// `函数 名(参数)：…` —— 造一个 `WalkFn` 并绑到名字上。
    fn exec_funcdef(&mut self, n: &Json) -> Result<(), JishiError> {
        let name = field(n, "name")?.as_str().unwrap_or("").to_string();
        let f = self.make_walk_fn(n, &name)?;
        self.assign_name(name, Val::WalkFn(Rc::new(f)));
        Ok(())
    }

    /// `类 名 [继承 基类]：…`
    ///
    /// ⚠️ **类体在一个子作用域里执行**（Python 语义）：`令 派 = 3.14` 成为类变量，
    /// 而方法（`函数 面积(自身)：…`）的**闭包就是这个类作用域** ——
    /// 所以方法体里能直接读到类变量。
    ///
    /// 📌 **不另造一套「类」**：产出的就是宿主那个 `Class`（方法表里放
    /// `Val::WalkFn`）→ `是实例` / `超()` / `get_attr` / `set_attr` 直接可用。
    fn exec_classdef(&mut self, n: &Json) -> Result<(), JishiError> {
        let name = field(n, "name")?.as_str().unwrap_or("").to_string();
        // ---- 先找基类（按名字，运行期求值 —— Python 也是这样）----
        let base_name = n.get("base").and_then(|v| v.as_str()).map(String::from);
        let base = match &base_name {
            Some(bn) => match self.env.borrow().lookup(bn) {
                Some(Val::Class(c)) => Some(c),
                Some(other) => return Err(err("类型错误", format!(
                    "「{bn}」不是类，不能继承（它是「{}」）", type_label(&other)))),
                None => return Err(err_with_hint(T_NAME,
                    format!("找不到名字「{bn}」"),
                    format!("「继承」后面要写一个**已经定义好的类**名"))),
            },
            None => None,
        };
        // ---- 类体：新开作用域跑一遍 ----
        let scope = Scope::child(Rc::clone(&self.env));
        let saved_env = Rc::clone(&self.env);
        let saved_locals = self.locals.take();
        self.env = Rc::clone(&scope);
        self.locals = None;                     // 类体不是函数体
        // ⚠️ 调试器要看到的是**外层作用域**（Python 侧 `_do_classdef` 就在外层
        // env 里逐句跑）—— 见 `debug_env` 的注释。只为「报文与 Python 一致」，
        // 与语言语义无关。
        let saved_debug_env = self.debug_env.take();
        self.debug_env = Some(Rc::clone(&saved_env));
        let mut failed = None;
        for st in stmts_of(n, "body")? {
            if let Err(e) = self.exec_stmt(st) {
                failed = Some(e);
                break;
            }
        }
        self.debug_env = saved_debug_env;
        let body_vars = {
            let mut b = scope.borrow_mut();
            std::mem::take(&mut b.vars)
        };
        self.env = saved_env;
        self.locals = saved_locals;
        if let Some(e) = failed {
            return Err(e);
        }
        // ---- 方法 / 类变量分开 ----
        let mut methods: HashMap<String, Val> = HashMap::new();
        let mut class_vars: HashMap<String, Val> = HashMap::new();
        for (k, v) in body_vars {
            if matches!(v, Val::WalkFn(_)) {
                methods.insert(k, v);
            } else {
                class_vars.insert(k, other_of(v));
            }
        }
        // ---- 继承：方法表合并（子类覆盖基类）、类变量合并 ----
        let (base_obj, base_ids) = match &base {
            Some(b) => {
                let mut merged = b.methods.clone();
                merged.extend(methods);
                methods = merged;
                let mut vars = b.class_vars.borrow().clone();
                vars.extend(class_vars);
                class_vars = vars;
                let mut ids = b.base_ids.clone();
                ids.push(b.id);
                (Some(Rc::new((**b).clone())), ids)
            }
            None => (None, Vec::new()),
        };
        let cls = Class {
            name: name.clone(),
            methods,
            class_vars: Rc::new(RefCell::new(class_vars)),
            base_ids,
            base: base_obj,
            id: next_obj_id(),
        };
        self.assign_name(name, Val::Class(Box::new(cls)));
        Ok(())
    }

    /// 造实例：先建空对象，再调它的 `初始化`（首参是实例自己）——
    /// 与字节码 VM 的 `make_instance` **同一套语义与同一句报错**。
    fn make_instance(&mut self, c: Class, args: Vec<Val>) -> Result<Val, JishiError> {
        let inst = Rc::new(RefCell::new(Instance {
            class: c.clone(),
            fields: HashMap::new(),
        }));
        if c.methods.contains_key("初始化") {
            // 类上取到的是**未绑定**的函数，所以实例要自己塞到第一个实参
            let m = get_attr(Val::Class(Box::new(c.clone())), "初始化", 0, 0)?;
            let mut all = Vec::with_capacity(args.len() + 1);
            all.push(Val::Instance(inst.clone()));
            all.extend(args);
            self.call_value(m, all)?;
        } else if !args.is_empty() {
            return Err(err("类型错误",
                format!("类「{}」没有构造方法", c.name)));
        }
        Ok(Val::Instance(inst))
    }

    /// 绑一个名字到**当前**作用域（`令` / `函数` / `遍历` 的目标都走它）。
    fn assign_name(&mut self, name: String, value: Val) {
        self.env.borrow_mut().vars.insert(name, value);
    }

    /// 从 `FuncDef` / `Lambda` 节点造函数值。
    ///
    /// ⚠️ **默认值在定义处求值**（不是每次调用时求）—— 与 Python 一致：
    /// `函数 f(x=现在())` 里的 `现在()` 只在定义时算一次。
    fn make_walk_fn(&mut self, n: &Json, name: &str) -> Result<WalkFn, JishiError> {
        let params = str_list(n, "params");
        let mut param_kind = int_list(n, "param_kind");
        if param_kind.is_empty() {
            param_kind = vec![0; params.len()];      // 前端不给就全是「普通」
        }
        let mut defaults = Vec::with_capacity(params.len());
        if let Some(ds) = n.get("defaults").and_then(|v| v.as_array()) {
            for d in ds {
                defaults.push(if matches!(d, Json::Null) { Val::None_ } else { self.eval(d)? });
            }
        }
        defaults.resize(params.len(), Val::None_);
        let body = stmts_of(n, "body")?.to_vec();
        let locals = Rc::new(assigned_names(&body));
        Ok(WalkFn {
            id: NEXT_FN_ID.fetch_add(1, Ordering::Relaxed),
            name: name.to_string(),
            def: Rc::new(FnDef { params, param_kind, defaults, body, locals }),
            // 闭包 = **定义处**的作用域（不是调用处的）
            closure: Rc::clone(&self.env),
            self_val: None,             // 具名函数/匿名函数都还没绑实例
        })
    }

    /// 调用一个树遍历函数值。
    ///
    /// `call_site` = 调用点的 **(行, 列)**（`Some` 只对「直接函数调用」；`None` =
    /// 方法调用 / 类实例化 / 内建回调，这些**不进调用栈**，见 `call_stack` 的注释）。
    fn call_walk_fn(&mut self, f: Rc<WalkFn>, args: Vec<Val>,
                    knames: &[String], kwvals: &[Val],
                    call_site: Option<(i64, i64)>) -> Result<Val, JishiError> {
        // 形参绑定走**共用**的那一份（`bind_params`）—— 报错文案与字节码 VM 同源，
        // 不然「函数「f」需要 2 个参数，但传了 3 个」两边各写一遍迟早会漂。
        // 绑定方法：先把实例塞到实参最前面 —— 它落进第一个形参（`自身`），
        // 与字节码侧 `call_frame(self_arg=…)` 完全同一个做法。
        let mut all: Vec<Val> = Vec::with_capacity(args.len() + 1);
        if let Some(s) = &f.self_val {
            all.push(s.clone());
        }
        all.extend(args);
        let def = Rc::clone(&f.def);
        let bound = bind_params(&f.name, &def.params, &def.param_kind,
                                Some(&def.defaults), &all, knames, kwvals)?;
        // 新作用域：父 = **定义处**的作用域（闭包就靠这一句）
        let scope = Scope::child(Rc::clone(&f.closure));
        {
            let mut b = scope.borrow_mut();
            for (i, v) in bound.into_iter().enumerate() {
                if let Some(v) = v {
                    b.vars.insert(def.params[i].clone(), v);
                }
            }
        }
        // 进函数前**压一帧**（只对直接函数调用）—— 出错拍 trace 时它得还在栈上。
        let traced = call_site.is_some();
        if let Some((line, col)) = call_site {
            self.call_stack.push(TraceFrame { func: f.name.clone(), line: Some(line),
                                               col: Some(col) });
        }
        // 换入调用现场（**出错也要换回来**，否则递归里的错会污染外层）
        let saved_env = Rc::clone(&self.env);
        let saved_locals = self.locals.take();
        self.env = Rc::clone(&scope);
        self.locals = Some(Rc::clone(&def.locals));
        // 调试钩子（R7.5）：**只有直接函数调用才 push**（与 `traced` 同一个条件，
        // 也与 Python 侧把 `push_frame` 放在 `isinstance(fn, UserFunction)` 那一支
        // 一致）—— `实例.方法()` 走 `UserFunction.__call__`，那边**不压调试帧**，
        // 所以 `下一步` 的深度判定在方法调用前后不变，两边行为才逐字相同。
        if traced {
            let (line, col) = call_site.unwrap();
            let _ = self.with_debugger(|d, _w| {
                d.push_frame(&f.name, &scope, line, col);
                Ok(())
            });
        }
        let mut out = Val::None_;
        let mut failed = None;
        for st in &def.body {
            match self.exec_stmt(st) {
                Ok(Flow::Return(v)) => {
                    out = v;
                    break;
                }
                Ok(_) => {}                        // Normal / Break / Continue
                Err(mut e) => {
                    // ⚠️ 错误要**穿出这个函数边界**了 → 此刻拍 trace（帧还在栈上）；
                    // 若它是被本函数内的「尝试」接住，`exec_stmt` 根本不会走到这。
                    self.snapshot_trace(&mut e);
                    failed = Some(e);
                    break;
                }
            }
        }
        self.env = saved_env;
        self.locals = saved_locals;
        if traced {
            self.call_stack.pop();
            let _ = self.with_debugger(|d, _w| {
                d.pop_frame();
                Ok(())
            });
        }
        match failed {
            Some(e) => Err(e),
            None => Ok(out),
        }
    }

    /// `目标 = 值`（也管 `+=` / `-=` / `*=` / `/=` / `//=` / `%=` / `**=`）。
    fn exec_assign(&mut self, n: &Json) -> Result<(), JishiError> {
        let op = n.get("op").and_then(|o| o.as_str()).unwrap_or("=");
        let target = field(n, "target")?;
        let value = self.eval(field(n, "value")?)?;
        if op == "=" {
            return self.assign_to(target, value);
        }
        // 复合赋值 `甲 op= 乙` ≡ `甲 = 甲 op 乙`；运算符是去掉末尾那个 `=`
        // （所以 `**=` → `**`、`//=` → `//`）。
        let arith = op.strip_suffix('=').unwrap_or(op);
        let code = binop_code(arith)
            .ok_or_else(|| self.unimpl(&format!("复合赋值「{op}」"), n))?;
        let cur = self.eval(target)?;
        let out = binop(code, cur, value)?;
        self.assign_to(target, out)
    }

    /// 把值赋给一个目标：`名字` / `a, b`（解包）/ `a, *余`。
    fn assign_to(&mut self, target: &Json, value: Val) -> Result<(), JishiError> {
        match kind(target) {
            "Name" => {
                let id = field(target, "id")?.as_str()
                    .ok_or_else(|| self.bad("Name 没有 id", target))?;
                // ⚠️ 写**当前**作用域（模块级 → 模块作用域；函数里 → 那个函数的
                // 局部）—— 「赋值即局部」。链上的外层同名变量**不动**。
                self.env.borrow_mut().vars.insert(id.to_string(), value);
                Ok(())
            }
            "TargetList" => {
                let names = target.get("names").and_then(|v| v.as_array())
                    .ok_or_else(|| self.bad("TargetList 没有 names", target))?;
                let star = target.get("star_index").and_then(|v| v.as_i64()).unwrap_or(-1);
                // 走**共用**的解包：星号、长度不符的中文报错都在里面。
                let vals = unpack_values(value, names.len(), star)?;
                for (nm, v) in names.iter().zip(vals.into_iter()) {
                    self.assign_to(nm, v)?;
                }
                Ok(())
            }
            "Attr" => {
                let obj = self.eval(field(target, "obj")?)?;
                let attr = field(target, "attr")?.as_str().unwrap_or("").to_string();
                set_attr(obj, &attr, value)
            }
            "Subscript" => {
                let obj = self.eval(field(target, "obj")?)?;
                let idx_node = field(target, "index")?;
                let idx = if kind(idx_node) == "Slice" {
                    self.eval_slice(idx_node)?
                } else {
                    self.eval(idx_node)?
                };
                // 切片赋值**本来就被支持**（`甲[0:1] = [9]`）—— 走共用的 `set_item`
                // 与字节码 VM 的 `STORE_SUBSCR` 同一条路。
                set_item(obj, idx, value)
            }
            other => Err(self.unimpl(&format!("赋值目标「{other}」"), target)),
        }
    }

    // -- 表达式 ----------------------------------------------------------

    fn eval(&mut self, n: &Json) -> Result<Val, JishiError> {
        let r = match kind(n) {
            "Num" => self.eval_num(n),
            "Str" => Ok(Val::Str(
                field(n, "value")?.as_str().unwrap_or("").to_string())),
            "Name" => self.eval_name(n),
            "BinOp" => self.eval_binop(n),
            "Compare" => self.eval_compare(n),
            "BoolOp" => self.eval_boolop(n),
            "UnaryOp" => self.eval_unaryop(n),
            "List" => self.eval_list(n),
            "Dict" => self.eval_dict(n),
            "Subscript" => self.eval_subscript(n),
            "Attr" => {
                let obj = self.eval(field(n, "obj")?)?;
                let attr = field(n, "attr")?.as_str().unwrap_or("");
                let (line, col) = self.at(n);
                // 走**共用**的 `get_attr`（实例字段 → 类变量 → 方法绑定；
                // 模块 / 内置类型的属性方法也在里面）—— 不另写一套查找顺序。
                get_attr(obj, attr, line.unwrap_or(0), col.unwrap_or(0))
            }
            "Lambda" => {
                let f = self.make_walk_fn(n, "匿名函数")?;
                Ok(Val::WalkFn(Rc::new(f)))
            }
            "Call" => self.eval_call(n),
            other => Err(self.unimpl(other, n)),
        };
        match r {
            Err(mut e) => {
                self.fill_loc(&mut e, n);
                Err(e)
            }
            ok => ok,
        }
    }

    /// 数字字面量。
    ///
    /// ⚠️ 这里要认**三种** `value`：`3`（整数）、`3.0`（浮点）、
    /// **`true`/`false`** —— 后者是 `真` / `假`！前端把它们也编成 **`Num`**
    /// （不是单独的 `Bool` 节点，见 `ast_nodes.Num` 与前端关键字表）。
    fn eval_num(&mut self, n: &Json) -> Result<Val, JishiError> {
        let v = field(n, "value")?;
        if let Some(b) = v.as_bool() {
            return Ok(Val::Bool(b));
        }
        if let Some(i) = v.as_i128() {
            return Ok(Val::Int(i));
        }
        v.as_f64().map(Val::Float)
            .ok_or_else(|| self.bad("Num 的 value 不是数字", n))
    }

    /// 读名字。顺序：**当前作用域链 → 「赋值即局部」判定 → 内建 → 报 E0301**。
    fn eval_name(&mut self, n: &Json) -> Result<Val, JishiError> {
        let id = field(n, "id")?.as_str().unwrap_or("");
        // ⚠️ `空` 在 AST 里是 **`Name`**（不是字面量节点）：前端在**编译期**
        // 把它换成 `none` 常量（`jishi/parser.py` 的 `_parse_atom` 那一支），
        // 所以这里也必须认得它，否则 `打印(空)` 会被当成「找不到名字」。
        if id == "空" {
            return Ok(Val::None_);
        }
        // ⚠️ **「赋值即局部」必须先判**（在沿链找**之前**）：一个名字只要在本函数里
        // 赋过值，它在这个函数里就**只是**局部变量 —— 读它**不许**穿透到外层去找
        // 同名变量（Python 语义；`interpreter.py` 的 `_lookup` / `runtime.unbound_local`）。
        // 放错顺序的症状很隐蔽：`令 甲 = 1` + 函数里先读 `甲` 再赋 `甲 = 2`
        // → 会**静默拿到外层的 1**（不报错、答案是错的）。
        if self.locals.as_ref().is_some_and(|s| s.contains(id)) {
            if let Some(v) = self.env.borrow().vars.get(id) {
                return Ok(v.clone());
            }
            // 还没轮到那次赋值 → 报「赋值之前被用到了」。
            // 树遍历没有字节码的 `local_hints`，所以**自己判断**外层有没有同名
            // 变量，再挑那句更具体的提示（与编译器生成的那句逐字相同）。
            let hint = if self.globals.borrow().has_name(id) {
                unbound_shadow_hint(id)
            } else {
                unbound_hint(id)
            };
            return Err(err_with_hint(T_NAME, unbound_msg(id), hint));
        }
        if let Some(v) = self.env.borrow().lookup(id) {
            return Ok(v.clone());
        }
        // 内建（与字节码 VM 的「先铺内建、用户赋值覆盖」等价：上面已先查过链）
        if let Some(v) = self.builtins.get(id) {
            return Ok(v.clone());
        }
        // 候选名池 = 已定义的名字（模块级）+ **内建名**（与字节码 VM / C VM 同一套口径）。
        // ⚠️ 内建名一定要在池子里：`长渡` 的提示要指向「长度」，而「长度」可能
        // 只作为**被调用的名字**出现过、根本没进模块作用域（R6 主体那次的教训）。
        let mut pool: Vec<String> = self.globals.borrow().vars.keys().cloned().collect();
        pool.extend(self.builtins.keys().cloned());
        let msg = format!("找不到名字「{id}」");
        Err(match name_hint(id, &pool) {
            Some(h) => err_with_hint(T_NAME, msg, h),
            None => err(T_NAME, msg),
        })
    }

    fn eval_binop(&mut self, n: &Json) -> Result<Val, JishiError> {
        let op = field(n, "op")?.as_str().unwrap_or("");
        let code = binop_code(op)
            .ok_or_else(|| self.unimpl(&format!("运算符「{op}」"), n))?;
        let l = self.eval(field(n, "left")?)?;
        let r = self.eval(field(n, "right")?)?;
        // ⚠️ 走**共用的** `binop`（不在本文件里另写一套算术）：
        // 要抓的是「编译器 vs 求值」的差异，不是两份 `+` 谁对。
        binop(code, l, r)
    }

    /// 比较（**支持链式**：`1 < x < 5`）。
    ///
    /// Python 语义：链式比较是「相邻两项都成立」，而且**短路** —— 有一节为假就停，
    /// 后面那一节的**求值根本不发生**（所以 `1 > 2 > 会报错的东西` 不会报错）。
    fn eval_compare(&mut self, n: &Json) -> Result<Val, JishiError> {
        let ops = n.get("ops").and_then(|v| v.as_array())
            .ok_or_else(|| self.bad("Compare 没有 ops", n))?;
        let comps = n.get("comparators").and_then(|v| v.as_array())
            .ok_or_else(|| self.bad("Compare 没有 comparators", n))?;
        let mut left = self.eval(field(n, "left")?)?;
        for (op, c) in ops.iter().zip(comps.iter()) {
            let sym = op.as_str().unwrap_or("");
            let code = cmpop_code(sym)
                .ok_or_else(|| self.unimpl(&format!("比较运算符「{sym}」"), n))?;
            let right = self.eval(c)?;
            // 走**共用**的 `compare`（成员测试 `在`/`是` 也在它里面）。
            let ok = compare(code, left, right.clone())?;
            if !truthy(ok) {
                return Ok(Val::Bool(false));
            }
            left = right;
        }
        Ok(Val::Bool(true))
    }

    /// `与` / `或`。
    ///
    /// ⚠️ **结果一定是真/假**（不是短路值本身）：`1 或 2` 给「真」而不是 `1`。
    /// 这与其他语言不一样，是有意的 —— 见 `interpreter._eval_boolop -> bool`
    /// 与 `core/compiler.rs` 的 `c_boolop`（那句注释就写着「与树遍历一致：
    /// 结果一定是真/假，不是短路值本身」）。短路照旧发生。
    fn eval_boolop(&mut self, n: &Json) -> Result<Val, JishiError> {
        let op = field(n, "op")?.as_str().unwrap_or("");
        if op != "与" && op != "或" {
            return Err(self.unimpl(&format!("连词「{op}」"), n));
        }
        let values = n.get("values").and_then(|v| v.as_array()).map(|v| v.as_slice()).unwrap_or(&[]);
        let mut last = op == "与";
        for v in values {
            last = truthy(self.eval(v)?);
            if op == "与" && !last {
                return Ok(Val::Bool(false));
            }
            if op == "或" && last {
                return Ok(Val::Bool(true));
            }
        }
        Ok(Val::Bool(last))
    }

    /// `-甲` / `非甲`。
    fn eval_unaryop(&mut self, n: &Json) -> Result<Val, JishiError> {
        let op = field(n, "op")?.as_str().unwrap_or("");
        let v = self.eval(field(n, "operand")?)?;
        match op {
            // 与字节码 VM 的 UNARY_OP 同源（`symbol_to_unop`：`-`→0、`非`→1）。
            "-" => unary_neg(v),
            "非" => Ok(Val::Bool(!truthy(v))),
            other => Err(self.unimpl(&format!("一元运算符「{other}」"), n)),
        }
    }

    /// 切片字面量 `[起点:终点:步长]`（三个分量都可能缺省）。
    ///
    /// ⚠️ 与字节码的 `BUILD_SLICE` **同一口径**：缺省是 `None`（不是 0）——
    /// 负步长下「缺省」与「显式给 0」含义完全不同。
    /// 步长为 0 的那句报错也照抄（`值错误`：切片的步长不能为 0）。
    fn eval_slice(&mut self, n: &Json) -> Result<Val, JishiError> {
        let mut parts: [Option<i128>; 3] = [None, None, None];
        for (i, key) in ["start", "stop", "step"].iter().enumerate() {
            if let Some(node) = n.get(key) {
                if !matches!(node, Json::Null) {
                    let v = self.eval(node)?;
                    parts[i] = Some(as_int(&v)?);
                }
            }
        }
        let [start, stop, step] = parts;
        if step == Some(0) {
            return Err(err("值错误", "切片的步长不能为 0"));
        }
        Ok(Val::Slice(Rc::new(SliceVal { start, stop, step })))
    }

    /// 列表字面量 `[甲, 乙]`。
    fn eval_list(&mut self, n: &Json) -> Result<Val, JishiError> {
        let els = n.get("elements").and_then(|v| v.as_array()).map(|v| v.as_slice()).unwrap_or(&[]);
        let mut out = Vec::with_capacity(els.len());
        for e in els {
            out.push(self.eval(e)?);
        }
        Ok(list_new(out))
    }

    /// 字典字面量 `{键: 值}`。
    fn eval_dict(&mut self, n: &Json) -> Result<Val, JishiError> {
        let keys = n.get("keys").and_then(|v| v.as_array()).map(|v| v.as_slice()).unwrap_or(&[]);
        let vals = n.get("values").and_then(|v| v.as_array()).map(|v| v.as_slice()).unwrap_or(&[]);
        let mut pairs = Vec::with_capacity(keys.len());
        for (k, v) in keys.iter().zip(vals.iter()) {
            pairs.push((self.eval(k)?, self.eval(v)?));
        }
        Ok(dict_new(pairs))
    }

    /// 取下标 `甲[0]`。**切片要等「容器」那一步**（`Slice` 是另一个节点）。
    fn eval_subscript(&mut self, n: &Json) -> Result<Val, JishiError> {
        let obj = self.eval(field(n, "obj")?)?;
        let idx_node = field(n, "index")?;
        let idx = if kind(idx_node) == "Slice" {
            self.eval_slice(idx_node)?
        } else {
            self.eval(idx_node)?
        };
        // 走**共用**的 `get_item`（负索引折算、切片折算、键错误、文本按码点…都在里面）。
        get_item(obj, idx)
    }

    fn eval_call(&mut self, n: &Json) -> Result<Val, JishiError> {
        let func = field(n, "func")?;
        // 调用目标**先把表达式求出来**（`f(1)(2)`、`令 g = f; g()` 都靠这一句）。
        // `数.开方` 那种属性调用会在 `eval` 里报「还没实现「Attr」」——
        // 报错点仍然落在 `Attr` 上，与以前一样好定位。
        let callee = self.eval(func)?;
        let mut args = Vec::new();
        if let Some(list) = n.get("args").and_then(|a| a.as_array()) {
            for a in list {
                args.push(self.eval(a)?);
            }
        }
        // 关键字实参：`keywords` 是 `[[名字, 值表达式], …]`（`ast_nodes.Call`）。
        let (mut knames, mut kwvals) = (Vec::new(), Vec::new());
        if let Some(kws) = n.get("keywords").and_then(|k| k.as_array()) {
            for kw in kws {
                let pair = kw.as_array()
                    .ok_or_else(|| self.bad("keywords 的一项不是 [名字, 值]", n))?;
                if pair.len() != 2 {
                    return Err(self.bad("keywords 的一项不是 [名字, 值]", n));
                }
                knames.push(pair[0].as_str().unwrap_or("").to_string());
                kwvals.push(self.eval(&pair[1])?);
            }
        }
        match callee {
            // 用户函数：形参绑定与报错都走共用的 `bind_params`。
            // ⚠️ **调用点行号**（Call 节点的 line）只对**未绑定**的函数进调用栈：
            // `对.算()` 那种（`self_val` 有值）在 Python 侧走 `UserFunction.__call__`，
            // **不进 `_call_stack`**（trace 里没有方法帧）—— 这里传 `None` 对齐。
            Val::WalkFn(f) => {
                // ⚠️ **调用点 (行, 列)** 只对**未绑定**的函数进调用栈：
                // `对.算()` 那种（`self_val` 有值）在 Python 侧走
                // `UserFunction.__call__`，**不进 `_call_stack`**（trace 里没有
                // 方法帧）—— 这里传 `None` 对齐。列也一起带上：Python 的
                // `Frame(name, line, col)` 三个字段都在，`--json-errors` 的
                // `trace` 要原样给出去（R7.5 顺手补齐，以前这里恒为 `null`）。
                let call_site = if f.self_val.is_none() {
                    match (n.get("line").and_then(|v| v.as_i64()),
                           n.get("col").and_then(|v| v.as_i64())) {
                        (Some(l), Some(c)) => Some((l, c)),
                        (Some(l), None) => Some((l, 1)),
                        _ => None,
                    }
                } else {
                    None
                };
                self.call_walk_fn(f, args, &knames, &kwvals, call_site)
            }
            // 调用一个**类** = 造实例（`新建 甲(…)`）—— 与字节码 VM 的
            // `make_instance` 同语义：先建空对象，再调它的 `初始化`（首参是自身）。
            Val::Class(c) => self.make_instance(*c, args),
            // 内建：走**共用**的那一张表（`new_builtins`）—— 「内建调用接上」这一步的
            // 全部意义就在这里：`长度` / `范围` / `最大` / `打印` … 都只有一份实现，
            // 字节码 VM 与树遍历执行器**不能**各写一套。
            Val::Builtin(_, f) => {
                // 内建（除宿主内部 `map_kwargs` 那几个）不接受关键字实参 ——
                // 明确报，不静默丢。
                if !knames.is_empty() {
                    return Err(self.unimpl("给内建传关键字实参", n));
                }
                f(&args, self)
            }
            // 内建类型的方法（列表 / 文本 / 字典 / 集合 / 文件 / 精确小数）：
            // **走共用的那张方法表**（`call_builtin_method` 是自由函数）——
            // 与字节码 VM 逐字同一套语义，不另写一份。
            Val::BoundMethod(bm) => match *bm {
                BoundMethod::Builtin { name, receiver, line, col } => {
                    match call_builtin_method(self, name, *receiver, &args) {
                        Ok(v) => Ok(v),
                        Err(mut e) => {
                            // ⚠️ 方法内部报错：位置用「取属性」那处（`a.追加` 的 `a`），
                            // 与字节码 VM 的 `call_bound` 同口径（`bm_pos`）。不用 Call
                            // 节点的位置（那是 `追加()` 的 `追加`，差好几列）。
                            if e.line.is_none() && line > 0 {
                                e.line = Some(line);
                                e.col = Some(col);
                            }
                            Err(e)
                        }
                    }
                }
                // 树遍历造不出 `BoundMethod::User`（它用 `Val::WalkFn`）
                BoundMethod::User { name, .. } => Err(err("运行期错误", format!(
                    "树遍历参考执行器拿到了字节码的方法值「{name}」（内部错误）"))),
            },
            // 调用一个**异常类型** = 抛出一个带消息的异常。
            // `抛出 值错误("x")` 走的就是这一句：`值错误` 求值成 `Val::ExcType`，
            // 加括号调用 = 造异常。与字节码 VM 的同一分支逐字同源。
            Val::ExcType(t) => {
                reject_kwargs("异常类型", &knames)?;
                let msg = args.first().map(display).unwrap_or_default();
                // ⚠️ **构造**异常对象，不是立刻抛（R7.4 修；与字节码 VM 同一分支
                // 逐字同源 —— 详见 `lib.rs` 那段注释）。
                Ok(Val::Exc(Rc::new(ExcValue {
                    type_name: t.clone(),
                    message: msg,
                    code: crate::error_code(&t).map(|s| s.to_string()),
                })))
            }
            // 类型名与提示都照 Python 的 `runtime.call_value`（E2006）——
            // 以前这里报的是「还没实现」，可「不能调用一个整数」根本不是
            // 「没实现」，是**确定的语义**（拿它当没实现会掩盖真问题）。
            other => Err(err_with_hint(
                T_NOT_CALLABLE,
                format!("「{}」不能调用", display(&other)),
                "只有函数或带括号的对象才能调用".to_string())),
        }
    }

    /// `尝试 / 捕获 / 最终`。
    ///
    /// ⚠️ 三处口径都是照 Python 侧 `Interpreter._do_try` 定的（两个 VM 也一致）：
    ///
    /// 1. **控制信号（`返回` / `中断` / `继续`）不被捕获** —— 它们不是错误。
    ///    但「最终」**一定要跑**，跑完再把信号原样往外传。
    /// 2. **捕获体在「当前作用域」跑**（不新开子作用域）—— 这条是 R5.1 定的：
    ///    早先树遍历给「捕获 为 名字」单独建了个子作用域，于是**捕获体里的赋值
    ///    出了捕获体就没了**（全语言只有那一处那样）。所以 `捕获 为 e` 就是
    ///    「在当前作用域里赋一次值」，捕获体也在当前作用域跑。
    /// 3. **「最终」里跳出去会覆盖外层信号**（`最终: 返回 2` 压过 `尝试: 返回 1`）
    ///    —— 这就是 `run_finally` 返回 `Option<Flow>` 的原因。
    ///
    /// 另外：`捕获` 的条件求值出错、或没有处理器接住，**都以那个错为准**
    /// （「最终」照跑；若「最终」自己也出错，则由它覆盖）。
    fn exec_try(&mut self, n: &Json) -> Result<Flow, JishiError> {
        let body = stmts_of(n, "body")?;
        let handlers = n.get("handlers").and_then(|v| v.as_array())
            .map(|v| v.as_slice()).unwrap_or(&[]);
        let finalbody = n.get("finalbody").and_then(|v| v.as_array())
            .map(|v| v.as_slice());

        match self.exec_body(body) {
            Ok(flow) => {
                // 「最终」里跳出去 → **覆盖**原本的信号
                match self.run_finally(finalbody)? {
                    Some(f) => Ok(f),
                    None => Ok(flow),
                }
            }
            Err(mut e) => {
                // 🔴 **「调试退出」不让「捕获」接住，但「最终」照跑**（R7.5）——
                // 与 Python 的 `_do_try` 里那句
                // `except (RunBreak, RunContinue, _ReturnSignal, DebugQuit)`
                // 逐条对应。用户按「退出」是想要立刻结束；一句 `捕获：` 就能吞掉
                // 它的话，用户只会觉得调试器失灵。
                if e.is_debug_quit() {
                    return match self.run_finally(finalbody)? {
                        Some(f) => Ok(f),
                        None => Err(e),
                    };
                }
                // 被捕获的异常是一个**带内容的对象**（与 `dispatch_exception`
                // 压栈的同一个形状）—— `e.类型` / `e.消息` 因此可读。
                let exc = ExcValue::from_error(&e);
                for h in handlers {
                    let ty = h.get("type").filter(|t| !matches!(t, Json::Null));
                    let matched = match ty {
                        // 裸 `捕获：` —— 全接
                        None => true,
                        Some(t) => {
                            // ⚠️ 条件在**当前作用域**求值（与 Python 的
                            // `_handler_catches` 一致）；它自己出错就直接往外抛
                            // （此时「最终」不跑 —— Python 也是这样）。
                            let cond = self.eval(t)?;
                            // 条件不是异常类型也直接往外抛（M56-5 收口）：位置
                            // 落在**`捕获` 那一行**（Python 用 `h.line`/`h.col`）。
                            match exception_matches(&exc, &cond) {
                                Ok(m) => m,
                                Err(mut e) => {
                                    self.fill_loc(&mut e, h);
                                    return Err(e);
                                }
                            }
                        }
                    };
                    if !matched {
                        continue;
                    }
                    // ⚠️ **补拍 trace**（对齐 `_do_try` 的 R5.1）：错误在**本函数内
                    // 被「尝试」接住、不会穿出函数边界，所以 `call_walk_fn` 拍不到
                    // —— 这里补上（此刻调用栈还完整）。
                    self.snapshot_trace(&mut e);
                    // 捕获名 = **当前作用域**的一次赋值（口径 2）
                    if let Some(name) = h.get("name").and_then(|v| v.as_str()) {
                        if !name.is_empty() {
                            self.env.borrow_mut()
                                .vars.insert(name.to_string(), exc.clone());
                        }
                    }
                    let hbody = stmts_of(h, "body")?;
                    match self.exec_body(hbody) {
                        // ⚠️ **捕获体自己出错**时「最终」不跑 —— 这是既有口径
                        // （`interpreter._do_try` 那个 `except (RunBreak,
                        // RunContinue, _ReturnSignal, DebugQuit)` 只接控制信号；
                        // 四个字节码宿主也一样）。照抄它，不照 Python 语言本身。
                        Err(he) => return Err(he),
                        // 捕获体正常结束或**跳出去**（`返回`/`中断`/`继续`）：
                        // 「最终」要跑；它自己跳出去时**以它为准**（覆盖捕获体的信号）。
                        Ok(flow) => return match self.run_finally(finalbody)? {
                            Some(f) => Ok(f),
                            None => Ok(flow),
                        },
                    }
                }
                // 没有处理器接住：跑「最终」后继续往上抛（原错误原样带走）
                match self.run_finally(finalbody)? {
                    Some(f) => Ok(f),
                    None => Err(e),
                }
            }
        }
    }

    /// 跑「最终」块。
    ///
    /// 返回 `Some(flow)` = 这个块**自己跳出去了**（`返回` / `中断` / `继续`）——
    /// 那会**覆盖**外层正在传播的信号（Python 的 finally 就是这么定的）。
    fn run_finally(&mut self, fin: Option<&[Json]>) -> Result<Option<Flow>, JishiError> {
        let Some(stmts) = fin else { return Ok(None) };
        match self.exec_body(stmts)? {
            Flow::Normal => Ok(None),
            f => Ok(Some(f)),
        }
    }

    /// `抛出 值`。
    ///
    /// 包装规则走**共用**的 `make_exception`，与字节码 VM 逐字同源：
    /// 异常类型 → 无消息的异常；捕获到的异常对象 → **保住消息**重抛；其余值 →
    /// 包成「异常」。
    fn exec_raise(&mut self, n: &Json) -> Result<Flow, JishiError> {
        let v = match n.get("value") {
            Some(node) if !matches!(node, Json::Null) => self.eval(node)?,
            // 前端在**解析期**就拦下裸 `抛出`（`tests/error_cases/26_抛出无值.jsh`
            // 考的就是它），所以这里到不了 —— 留着是为了「不静默」。
            _ => return Err(err("类型错误",
                "「抛出」后面要写一个值（比如 抛出 值错误(\"说明\")）")),
        };
        Err(make_exception(v))
    }

    /// `导入 模块 [从 python/本地包] [为 别名]`。
    ///
    /// ⚠️ **跨语言宿主有意不支持 `从 python / 本地包`** —— 单二进制里没有
    /// Python。报的那句话是**给用户看的正确信息**，登记在
    /// `tools/check_engines.py` 的 `HOST_KNOWN_GAPS` 里；这里与字节码 VM
    /// **逐字相同**（不一致的话互拍会立刻红）。
    fn exec_import(&mut self, n: &Json) -> Result<(), JishiError> {
        let name = field(n, "name")?.as_str().unwrap_or("").to_string();
        let from_python = n.get("from_python").and_then(|v| v.as_bool()).unwrap_or(false);
        let from_local = n.get("from_local").and_then(|v| v.as_bool()).unwrap_or(false);
        if from_python || from_local {
            return Err(err("运行期错误",
                format!("Rust 引擎不支持「从 python/本地包」导入「{name}」")));
        }
        let modv = stdlib_module(&name)?;
        // 有别名就绑别名，否则绑模块名（与编译器 `c_import` 的 `store_name` 同一口径）。
        let bind = n.get("alias").and_then(|v| v.as_str())
            .filter(|s| !s.is_empty()).map(|s| s.to_string()).unwrap_or(name);
        self.env.borrow_mut().vars.insert(bind, modv);
        Ok(())
    }

    /// 自定义对象的「文本()」（`format_value_with` 的钩子）。
    /// 返回 `Ok(None)` = 没有「文本」方法，按 `display` 显示。
    /// 与字节码 VM 的 `text_hook` **同一套语义与同一句报错**。
    fn instance_text(&mut self, v: &Val) -> Result<Option<String>, JishiError> {
        let inst = match v {
            Val::Instance(inst) => inst,
            _ => return Ok(None),
        };
        if !inst.borrow().class.methods.contains_key("文本") {
            return Ok(None);
        }
        let m = get_attr(v.clone(), "文本", 0, 0)?;
        match self.call_value(m, Vec::new())? {
            Val::Str(s) => Ok(Some(s)),
            other => {
                let cls = inst.borrow().class.name.clone();
                Err(err("类型错误", format!(
                    "类「{cls}」的「文本」方法返回了「{}」，要返回文本",
                    type_label(&other))))
            }
        }
    }

    // -- 报错小工具（位置从节点上取，与其它执行器同源） ----------------------

    fn at(&self, n: &Json) -> (Option<i64>, Option<i64>) {
        (n.get("line").and_then(|v| v.as_i64()),
         n.get("col").and_then(|v| v.as_i64()))
    }

    /// 明确报「还没实现」—— **绝不静默给错值**（本项目最恨「不报错的错答案」）。
    fn unimpl(&self, what: &str, n: &Json) -> JishiError {
        let (line, col) = self.at(n);
        let mut e = err("运行期错误",
            format!("树遍历参考执行器还没实现「{what}」"));
        e.line = line;
        e.col = col;
        e
    }

    fn bad(&self, msg: &str, n: &Json) -> JishiError {
        let (line, col) = self.at(n);
        let mut e = err("运行期错误", msg.to_string());
        e.line = line;
        e.col = col;
        e
    }
}

/// 树遍历执行器在这套接口上的实现。
///
/// ⚠️ **诚实报错、不静默给错值**：需要「用户函数 / 类」的能力
/// （回调用户函数、自定义对象的「文本()」）现在还没实现那两种节点，
/// 所以直接报「还没实现」；**其余类型走的是与 VM 共用的那份实现**
/// （`format_value_with` / `make_iterator`），不会漂。
impl BuiltinHost for Walker {
    fn format_value(&mut self, v: &Val, top: bool) -> Result<String, JishiError> {
        // 容器递归与 VM 共用；只有「自定义对象怎么算文本」由宿主给钩子。
        format_value_with(v, top, &mut |obj| self.instance_text(obj))
    }
    fn lookup_method(&mut self, obj: &Val, name: &str) -> Option<Val> {
        // 与字节码 VM 的 `lookup_method` 同语义：实例上有这个方法就绑上返回；
        // 其余类型只认「退出 / 关闭」（文件那类），别的名字一律 None。
        match obj {
            Val::Instance(i) => {
                if i.borrow().class.methods.contains_key(name) {
                    get_attr(obj.clone(), name, 0, 0).ok()
                } else {
                    None
                }
            }
            _ => match name {
                "退出" | "关闭" => match get_attr(obj.clone(), name, 0, 0) {
                    Ok(v @ Val::BoundMethod(_)) | Ok(v @ Val::WalkFn(_)) => Some(v),
                    _ => None,
                },
                _ => None,
            },
        }
    }
    fn make_iter(&mut self, obj: Val) -> Result<Val, JishiError> {
        // 自定义对象定义了「迭代()」就调它（与字节码 VM 的 `make_iter` 同语义）。
        if let Val::Instance(_) = &obj {
            let has = matches!(&obj, Val::Instance(i)
                               if i.borrow().class.methods.contains_key("迭代"));
            if has {
                let got = get_attr(obj.clone(), "迭代", 0, 0)?;
                let out = self.call_value(got, Vec::new())?;
                // 返回自身会无限循环 —— Python 侧也拦（与 VM 同一句文案）
                if let (Val::Instance(a), Val::Instance(b)) = (&out, &obj) {
                    if Rc::ptr_eq(a, b) {
                        return Err(err("运行期错误",
                            "「迭代()」返回了对象自身，这样遍历会无限循环"));
                    }
                }
                return make_iterator(out);
            }
        }
        make_iterator(obj)
    }
    fn call_value(&mut self, callee: Val, args: Vec<Val>) -> Result<Val, JishiError> {
        // 内建现在能**回调用户函数**了（`映射` / `过滤` / `排序按` / `归约` …）——
        // 这正是 ③ 落地后长出来的能力。
        match callee {
            Val::WalkFn(f) => self.call_walk_fn(f, args, &[], &[], None),
            Val::Class(c) => self.make_instance(*c, args),
            Val::BoundMethod(bm) => match *bm {
                BoundMethod::Builtin { name, receiver, line, col } => {
                    match call_builtin_method(self, name, *receiver, &args) {
                        Ok(v) => Ok(v),
                        Err(mut e) => {
                            if e.line.is_none() && line > 0 {
                                e.line = Some(line);
                                e.col = Some(col);
                            }
                            Err(e)
                        }
                    }
                }
                BoundMethod::User { name, .. } => Err(err("运行期错误", format!(
                    "树遍历参考执行器拿到了字节码的方法值「{name}」（内部错误）"))),
            },
            Val::Builtin(_, f) => f(&args, self),
            other => Err(err("类型错误",
                format!("「{}」不能当函数调用", display(&other)))),
        }
    }
    fn write_output(&mut self, s: &str) {
        self.output.push_str(s);
        // 调试器（R7.5）：DAP 把每一次 `write` 变成一条 `output` 事件 —— 粒度
        // 必须与 Python 侧一致（那边 `_StreamOut.write` 就是这么干的）。
        if let Some(sink) = self.output_sink.as_mut() {
            sink(s, false);
        }
    }
    fn write_stderr(&mut self, s: &str) {
        self.output_err.push_str(s);
        if let Some(sink) = self.output_sink.as_mut() {
            sink(s, true);
        }
    }
}

/// 恒等函数——只是让「方法 / 类变量分开」那段读起来更清楚。
fn other_of(v: Val) -> Val {
    v
}

fn kind(n: &Json) -> &str {
    n.get("类型").and_then(|k| k.as_str()).unwrap_or("?")
}

fn field<'a>(n: &'a Json, key: &str) -> Result<&'a Json, JishiError> {
    n.get(key).ok_or_else(|| err("运行期错误",
        format!("AST 节点「{}」缺字段「{key}」", kind(n))))
}

/// 取一个字符串列表字段（`params` / `names` …）。
fn str_list(n: &Json, key: &str) -> Vec<String> {
    n.get(key).and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default()
}

/// 取一个整数列表字段（`param_kind` / `ops` …）。
fn int_list(n: &Json, key: &str) -> Vec<i64> {
    n.get(key).and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_i64()).collect())
        .unwrap_or_default()
}

/// 扫一遍函数体，收集**本函数赋过值的名字**（「赋值即局部」）。
///
/// ⚠️ **不递归进嵌套的 `FuncDef` / `Lambda` 体** —— 那是它们自己的作用域。
/// ⚠️ 这一步与编译器的名字收集同源：**漏一个分支就会静默错**
/// （「赋值前读局部」报不出来，于是穿透到外层拿到别人的值）。
/// R5 移植时就因为编译器漏了 `Try` 分支踩过一次（`docs/前端契约.md` §12.4）——
/// 所以这里把 `Try` / `捕获` 也一起收上（哪怕第 ⑤ 步才实现它们）。
fn assigned_names(stmts: &[Json]) -> HashSet<String> {
    let mut out = HashSet::new();
    for st in stmts {
        collect_assigned(st, &mut out);
    }
    out
}

fn collect_assigned(n: &Json, out: &mut HashSet<String>) {
    match kind(n) {
        "Assign" => {
            if let Some(t) = n.get("target") {
                collect_target(t, out);
            }
        }
        "For" => {
            if let Some(t) = n.get("target") {
                collect_target(t, out);
            }
        }
        "FuncDef" => {
            if let Some(id) = n.get("name").and_then(|v| v.as_str()) {
                out.insert(id.to_string());
            }
            return;                     // ⚠️ 不进去：那是另一个作用域
        }
        "Lambda" => return,             // ⚠️ 同上
        _ => {}
    }
    // 递归进各种「语句块」字段
    for key in ["body", "orelse", "finalbody"] {
        if let Some(list) = n.get(key).and_then(|v| v.as_array()) {
            for st in list {
                collect_assigned(st, out);
            }
        }
    }
    if let Some(branches) = n.get("branches").and_then(|v| v.as_array()) {
        for br in branches {
            if let Some(list) = br.as_array().and_then(|p| p.get(1)).and_then(|v| v.as_array()) {
                for st in list {
                    collect_assigned(st, out);
                }
            }
        }
    }
    if let Some(handlers) = n.get("handlers").and_then(|v| v.as_array()) {
        for h in handlers {
            if let Some(name) = h.get("name").and_then(|v| v.as_str()) {
                out.insert(name.to_string());
            }
            if let Some(list) = h.get("body").and_then(|v| v.as_array()) {
                for st in list {
                    collect_assigned(st, out);
                }
            }
        }
    }
}

/// 赋值目标里的名字（`名字` / `a, b` 解包）。下标、属性作目标**不是**名字绑定。
fn collect_target(t: &Json, out: &mut HashSet<String>) {
    match kind(t) {
        "Name" => {
            if let Some(id) = t.get("id").and_then(|v| v.as_str()) {
                out.insert(id.to_string());
            }
        }
        "TargetList" => {
            if let Some(names) = t.get("names").and_then(|v| v.as_array()) {
                for nm in names {
                    collect_target(nm, out);
                }
            }
        }
        _ => {}
    }
}

/// 取一个「语句列表」字段（`body` / `orelse` …）。
fn stmts_of<'a>(n: &'a Json, key: &str) -> Result<&'a [Json], JishiError> {
    n.get(key).and_then(|v| v.as_array()).map(|v| v.as_slice())
        .ok_or_else(|| err("运行期错误",
            format!("AST 节点「{}」的「{key}」不是语句列表", kind(n))))
}
