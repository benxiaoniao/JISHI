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
//! 已实现：`Program` / `Assign`（`=`）/ `ExprStmt` / `Name` / `Num` / `Str` /
//! `BinOp` / `Call`。
//!
//! **内建调用走的是与字节码 VM 共用的那一张表**（`lib.rs::new_builtins`，
//! 见 `BuiltinHost`）—— 所以 `长度` / `范围` / `最大` / `类型` … 全都直接可用，
//! 而且**只有一份实现**（本文件里没有、也不该有任何内建的定义）。
//!
//! **其余节点会明确报「还没实现「X」」** —— 不静默给错值。
//! 后续按语料逐个补齐（见 `docs/设计决策.md` D68/D69）。
//!
//! 已知还没接上的字面量：`真` / `假` 在前端是 **`Num` 且 `value` 是布尔**
//! （`eval_num` 只认数字，现在会报「Num 的 value 不是数字」）；`空` 是
//! **`Name`**。这三块归「字面量」那一步。
//!
//! ⚠️ 还没做「报错渲染与其它执行器逐字对齐」：现在只渲染**头行 + 提示语**，
//! 还差**源码行 / `^` / 调用链**那一块 —— 那要**源文件名与源码**，而本执行器
//! 的输入是 AST JSON（里面没有它们）。所以现阶段对拍的是**成功路径的 stdout**
//! 与「出错前已经打印的内容」。
//!
//! ## 与字节码 VM 的关系
//!
//! **语义层是共用的**（`binop` / `display` / 内建表…都在 `lib.rs` 里），
//! 这里只新写「AST 遍历 + 作用域」那一层。这是**有意的**：
//! 要抓的是「编译器和求值」的差异，不是「两份 `+` 的实现哪里不一样」。

use crate::json::Json;
use crate::{binop, display, err, format_value_with, make_iterator, new_builtins,
            BuiltinHost, Val, JishiError};
use std::collections::HashMap;

/// 运算符 → `binop` 的编码。
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

/// 取值：本轮只做「模块级作用域」。
///
/// ⚠️ 函数/闭包要的作用域链与单元表是后续步骤（`FuncDef` 还没实现）。
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
        for st in body {
            self.exec_stmt(st)?;
        }
        Ok(())
    }

    fn exec_stmt(&mut self, n: &Json) -> Result<(), JishiError> {
        match kind(n) {
            "Assign" => self.exec_assign(n),
            "ExprStmt" => {
                let e = field(n, "expr")?;
                self.eval(e)?;
                Ok(())
            }
            other => Err(self.unimpl(other, n)),
        }
    }

    /// `目标 = 值`。⚠️ 复合赋值（`+=`）走的是别的 `op`，本轮明确报「还没实现」。
    fn exec_assign(&mut self, n: &Json) -> Result<(), JishiError> {
        let op = n.get("op").and_then(|o| o.as_str()).unwrap_or("=");
        if op != "=" {
            return Err(self.unimpl("复合赋值", n));
        }
        let target = field(n, "target")?;
        let value = self.eval(field(n, "value")?)?;
        match kind(target) {
            "Name" => {
                let id = field(target, "id")?.as_str()
                    .ok_or_else(|| self.bad("Assign.target 没有 id", n))?;
                self.globals.insert(id.to_string(), value);
                Ok(())
            }
            other => Err(self.unimpl(&format!("赋值目标「{other}」"), target)),
        }
    }

    fn eval(&mut self, n: &Json) -> Result<Val, JishiError> {
        match kind(n) {
            "Num" => self.eval_num(n),
            "Str" => Ok(Val::Str(
                field(n, "value")?.as_str().unwrap_or("").to_string())),
            "Name" => self.eval_name(n),
            "BinOp" => self.eval_binop(n),
            "Call" => self.eval_call(n),
            other => Err(self.unimpl(other, n)),
        }
    }

    /// 数字字面量：JSON 里可能是整数（`Int`）也可能是浮点（`Float`）。
    ///
    /// ⚠️ 与前端一致：`3.0` 这种写法在 Python 侧是 float、`3` 是 int。
    fn eval_num(&mut self, n: &Json) -> Result<Val, JishiError> {
        let v = field(n, "value")?;
        if let Some(i) = v.as_i128() {
            return Ok(Val::Int(i));
        }
        v.as_f64().map(Val::Float).ok_or_else(|| self.bad("Num 的 value 不是数字", n))
    }

    /// 读名字。**找不到就报 E0301**，文案/候选提示都要与其它执行器一致。
    fn eval_name(&mut self, n: &Json) -> Result<Val, JishiError> {
        let id = field(n, "id")?.as_str().unwrap_or("");
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
