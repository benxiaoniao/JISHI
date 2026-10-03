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
//! ## ⚠️ 现在的进度（第一个切片，如实记）
//!
//! 只实现了：`Program` / `Assign`（`=`）/ `ExprStmt` / `Name` / `Num` / `Str` /
//! `BinOp` / `Call`（含 `打印`）。
//! **其余 26 种节点会明确报「还没实现「X」」** —— 不静默给错值。
//! 后续按语料逐个补齐（见 `docs/设计决策.md` D68）。
//!
//! ⚠️ 还没做「报错渲染与其它执行器逐字对齐」（那要复用 `render_error`，
//! 而它需要源文件名 + 读源码）；所以现阶段只对拍**成功路径的 stdout**。
//!
//! ## 与字节码 VM 的关系
//!
//! **语义层是共用的**（`binop` / `display` / 内建表…都在 `lib.rs` 里），
//! 这里只新写「AST 遍历 + 作用域」那一层。这是**有意的**：
//! 要抓的是「编译器和求值」的差异，不是「两份 `+` 的实现哪里不一样」。

use crate::json::Json;
use crate::{binop, display, err, Val, JishiError};
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
    pub(crate) output: String,
}

impl Walker {
    pub(crate) fn new() -> Walker {
        Walker { globals: HashMap::new(), output: String::new() }
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
        match self.globals.get(id) {
            Some(v) => Ok(v.clone()),
            None => {
                // 候选名池 = 已定义的名字 + 内建名（与字节码 VM / C VM 同一套口径，
                // 见 `lib.rs` 的 `builtin_names`）。这里先只用已定义的名字 ——
                // 内建名要等「内建调用」那一步接上（否则会给出不存在的候选）。
                let pool: Vec<String> = self.globals.keys().cloned().collect();
                let msg = format!("找不到名字「{id}」");
                Err(match crate::name_hint(id, &pool) {
                    Some(h) => crate::err_with_hint(crate::T_NAME, msg, h),
                    None => err(crate::T_NAME, msg),
                })
            }
        }
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
        let fname = match kind(func) {
            "Name" => field(func, "id")?.as_str().unwrap_or("").to_string(),
            other => return Err(self.unimpl(&format!("调用目标「{other}」"), func)),
        };
        let mut args = Vec::new();
        if let Some(list) = n.get("args").and_then(|a| a.as_array()) {
            for a in list {
                args.push(self.eval(a)?);
            }
        }
        match fname.as_str() {
            // `打印`：与字节码 VM 的 `_b_print` 同语义（空格分隔 + 换行）。
            // ⚠️ 它走 `format_value`（顶层文本不带引号）—— 对基本类型，
            // `display` 与它一致；自定义对象的「文本()」要等实例支持接上。
            "打印" => {
                let parts: Vec<String> = args.iter().map(display).collect();
                self.output.push_str(&parts.join(" "));
                self.output.push('\n');
                Ok(Val::None_)
            }
            other => Err(self.unimpl(&format!("内建函数「{other}」"), n)),
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

fn kind(n: &Json) -> &str {
    n.get("类型").and_then(|k| k.as_str()).unwrap_or("?")
}

fn field<'a>(n: &'a Json, key: &str) -> Result<&'a Json, JishiError> {
    n.get(key).ok_or_else(|| err("运行期错误",
        format!("AST 节点「{}」缺字段「{key}」", kind(n))))
}
