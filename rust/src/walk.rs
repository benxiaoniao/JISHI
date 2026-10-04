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
//! ## ⚠️ 现在的进度（如实记）
//!
//! | 已经能跑 | 还没实现（明确报错） |
//! |---|---|
//! | 语句：`Assign`（含 `+=` 等）、`ExprStmt`、`If`、`While`、`For`、`Loop`、`Break`、`Continue`、`Pass` | `FuncDef` / `Return` / `Lambda`（③） |
//! | 表达式：`Num`（含 `真`/`假`）、`Str`、`Name`（含 `空`）、`BinOp`、`Compare`、`BoolOp`、`UnaryOp`、`List`、`Dict`、`Subscript`（读）、`Call` | `Attr` / `Slice` / `Comprehension`（④） |
//! | 赋值目标：`Name`、`TargetList`（解包，含 `*余`） | `Subscript` / `Attr` 作目标（④） |
//! | —  | `ClassDef` / `Try` / `Raise` / `Import`（④⑤） |
//!
//! 具体差什么随时可查：`python tools/check_walk.py`（进度仪表）。
//! **其余节点会明确报「还没实现「X」」** —— 不静默给错值。
//!
//! ⚠️ 还没做「报错渲染与其它执行器逐字对齐」：现在只渲染**头行 + 提示语**，
//! 还差**源码行 / `^` / 调用链**那一块 —— 那要**源文件名与源码**，而本执行器
//! 的输入是 AST JSON（里面没有它们）。所以现阶段对拍的是**成功路径的 stdout**
//! 与「出错前已经打印的内容」。
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
    binop, compare, dict_new, display, err, format_value_with, get_item, iter_next,
    list_new, make_iterator, new_builtins, truthy, unary_neg, unpack_values,
    BuiltinHost, Val, JishiError,
};
use std::collections::HashMap;

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
    pub(crate) globals: HashMap<String, Val>,
    /// 内建表 —— **与字节码 VM 共用同一张**（`new_builtins`）。这是「内建语义
    /// 不漂」的保证：内建只有一份实现，两个执行器都调它。
    builtins: HashMap<String, Val>,
    pub(crate) output: String,
}

impl Walker {
    pub(crate) fn new() -> Walker {
        Walker {
            globals: HashMap::new(),
            builtins: new_builtins(),
            output: String::new(),
        }
    }

    /// 跑一棵 AST（`Program` 节点）。
    pub(crate) fn run(&mut self, ast: &Json) -> Result<(), JishiError> {
        let body = ast.get("body").and_then(|b| b.as_array())
            .ok_or_else(|| err("运行期错误", "AST 里没有 body（不是 Program 节点？）"))?;
        // 顶层不该出现 `跳出` / `继续`（前端在语法期就拦住了），返回值直接丢。
        self.exec_body(body)?;
        Ok(())
    }

    // -- 语句 ------------------------------------------------------------

    fn exec_stmt(&mut self, n: &Json) -> Result<Flow, JishiError> {
        match kind(n) {
            "Assign" => {
                self.exec_assign(n)?;
                Ok(Flow::Normal)
            }
            "ExprStmt" => {
                self.eval(field(n, "expr")?)?;
                Ok(Flow::Normal)
            }
            "If" => self.exec_if(n),
            "While" => self.exec_while(n),
            "For" => self.exec_for(n),
            "Loop" => self.exec_loop(n),
            "Break" => Ok(Flow::Break),
            "Continue" => Ok(Flow::Continue),
            "Pass" => Ok(Flow::Normal),
            other => Err(self.unimpl(other, n)),
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
            }
        }
        Ok(Flow::Normal)
    }

    /// `遍历 目标 在 可迭代对象：`。
    fn exec_for(&mut self, n: &Json) -> Result<Flow, JishiError> {
        let iterable = self.eval(field(n, "iter")?)?;
        let target = field(n, "target")?;
        let body = stmts_of(n, "body")?;
        // 走**共用**的 `make_iterator`（快照 + 反转 + 弹尾）——R6.1 修的那个
        // O(n²) 就在它里面；这里不能自己另写一套。
        let mut it = make_iterator(iterable)?;
        while let Some(v) = iter_next(&mut it)? {
            self.assign_to(target, v)?;
            match self.exec_body(body)? {
                Flow::Break => break,
                Flow::Normal | Flow::Continue => {}
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
            }
        }
        Ok(Flow::Normal)
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
                self.globals.insert(id.to_string(), value);
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
            other => Err(self.unimpl(&format!("赋值目标「{other}」"), target)),
        }
    }

    // -- 表达式 ----------------------------------------------------------

    fn eval(&mut self, n: &Json) -> Result<Val, JishiError> {
        match kind(n) {
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
            "Call" => self.eval_call(n),
            other => Err(self.unimpl(other, n)),
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

    /// 读名字。**找不到就报 E0301**，文案/候选提示都要与其它执行器一致。
    fn eval_name(&mut self, n: &Json) -> Result<Val, JishiError> {
        let id = field(n, "id")?.as_str().unwrap_or("");
        // ⚠️ `空` 在 AST 里是 **`Name`**（不是字面量节点）：前端在**编译期**
        // 把它换成 `none` 常量（`jishi/parser.py` 的 `_parse_atom` 那一支），
        // 所以这里也必须认得它，否则 `打印(空)` 会被当成「找不到名字」。
        if id == "空" {
            return Ok(Val::None_);
        }
        // 用户变量优先（与字节码 VM 的「先铺内建、用户赋值覆盖」等价）。
        if let Some(v) = self.globals.get(id) {
            return Ok(v.clone());
        }
        if let Some(v) = self.builtins.get(id) {
            return Ok(v.clone());
        }
        // 候选名池 = 已定义的名字 + **内建名**（与字节码 VM / C VM 同一套口径）。
        // ⚠️ 内建名一定要在池子里：`长渡` 的提示要指向「长度」，而「长度」可能
        // 只作为**被调用的名字**出现过、根本没进 `globals`（R6 主体那次的教训）。
        let mut pool: Vec<String> = self.globals.keys().cloned().collect();
        pool.extend(self.builtins.keys().cloned());
        let msg = format!("找不到名字「{id}」");
        Err(match crate::name_hint(id, &pool) {
            Some(h) => crate::err_with_hint(crate::T_NAME, msg, h),
            None => err(crate::T_NAME, msg),
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
        if kind(idx_node) == "Slice" {
            return Err(self.unimpl("切片", idx_node));
        }
        let idx = self.eval(idx_node)?;
        // 走**共用**的 `get_item`（负索引折算、键错误、文本按码点…都在里面）。
        get_item(obj, idx)
    }

    fn eval_call(&mut self, n: &Json) -> Result<Val, JishiError> {
        // 关键字参数（`keywords`）本轮不支持 —— 明确报，不静默丢。
        if let Some(kws) = n.get("keywords").and_then(|k| k.as_array()) {
            if !kws.is_empty() {
                return Err(self.unimpl("关键字实参", n));
            }
        }
        let func = field(n, "func")?;
        // 调用目标本轮只认名字（`数.开方` 那种属性调用要等「类 / 模块」那一步）。
        if kind(func) != "Name" {
            return Err(self.unimpl(&format!("调用目标「{}」", kind(func)), func));
        }
        let callee = self.eval(func)?;
        let mut args = Vec::new();
        if let Some(list) = n.get("args").and_then(|a| a.as_array()) {
            for a in list {
                args.push(self.eval(a)?);
            }
        }
        // 内建：走**共用**的那一张表（`new_builtins`）—— 「内建调用接上」这一步的
        // 全部意义就在这里：`长度` / `范围` / `最大` / `打印` … 都只有一份实现，
        // 字节码 VM 与树遍历执行器**不能**各写一套。
        match callee {
            Val::Builtin(_, f) => f(&args, self),
            other => Err(self.unimpl(&format!("调用非内建的值「{}」", display(&other)), n)),
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
        // 容器递归与 VM 共用；自定义对象的「文本()」要等「类」那一步（ClassDef 未实现）。
        format_value_with(v, top, &mut |_| {
            Err(err("运行期错误",
                "树遍历参考执行器还没实现「自定义对象的文本()」"))
        })
    }
    fn lookup_method(&mut self, _obj: &Val, _name: &str) -> Option<Val> {
        // 只有自定义对象 / 文件这类才有「进入/退出/关闭」—— 要等「类」那一步。
        None
    }
    fn make_iter(&mut self, obj: Val) -> Result<Val, JishiError> {
        // ⚠️ 这里**不能**像 VM 那样对 Instance 先拦一道调「迭代()」——
        // 树遍历还没有实例；同一个调用的其余部分（容器 / 文本 / 文件）走共用实现。
        make_iterator(obj)
    }
    fn call_value(&mut self, _callee: Val, _args: Vec<Val>) -> Result<Val, JishiError> {
        Err(err("运行期错误", "树遍历参考执行器还没实现「回调用户函数」"))
    }
    fn write_output(&mut self, s: &str) {
        self.output.push_str(s);
    }
}

fn kind(n: &Json) -> &str {
    n.get("类型").and_then(|k| k.as_str()).unwrap_or("?")
}

fn field<'a>(n: &'a Json, key: &str) -> Result<&'a Json, JishiError> {
    n.get(key).ok_or_else(|| err("运行期错误",
        format!("AST 节点「{}」缺字段「{key}」", kind(n))))
}

/// 取一个「语句列表」字段（`body` / `orelse` …）。
fn stmts_of<'a>(n: &'a Json, key: &str) -> Result<&'a [Json], JishiError> {
    n.get(key).and_then(|v| v.as_array()).map(|v| v.as_slice())
        .ok_or_else(|| err("运行期错误",
            format!("AST 节点「{}」的「{key}」不是语句列表", kind(n))))
}
