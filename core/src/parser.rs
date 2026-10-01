// 基石语法分析器（R3：从 `jishi/parser.py` 逐行为移植）。
//
// ⚠️ **目标同样是「与 Python 侧逐节点一致」，不是「写个更好的解析器」。**
// 一切「看着可以更干净」的地方都照抄原实现：脱糖的**时机与位置**、
// 临时变量的**编号与命名**（`__用_1__` / `__匹配_1__` / `__遍历项_行_列__`）、
// 每个节点的 `line`/`col` 到底取哪个 token —— 差一处就是 AST 不一致。
//
// ## 为什么 AST 能「通用地」表示
//
// `tools/conformance.py` 用 `sort_keys=True` 序列化，**键的顺序不参与比较**，
// 所以 Rust 侧只要保证「同一个节点、同一组字段名、同一组值」即可，
// 不必为 34 个节点类各写一遍结构体。于是这里用 `J`（类 JSON 值）+ `Node`
// （`kind` + 字段表）表示整棵树，绑定层再按字段名调对应的 dataclass。
//
// ## 与 Python 版的对应关系（行号指 `jishi/parser.py`）
//   Parser._parse_expr / _parse_unary / _parse_postfix / _parse_atom
//   _parse_statement / _parse_statements / _parse_block
//   _parse_if / _for / _loop / _while / _funcdef / _params / _classdef
//   _parse_return / _import / _raise / _try / _with / _match / _enum
//   _desugar_match / _desugar_enum / _desugar_ternary / _desugar_super
//   _check_no_stray_super / _check_class_body / _check_en_word

use crate::tokenizer::{self, Part, Tok, Value};

// ---------------------------------------------------------------------------
// 通用值 / 节点
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub enum J {
    Null,
    Bool(bool),
    Int(i64),
    /// 超出 i64 的整数：十进制原文（JSON 里也是字符串，见契约 §4.3）
    Big(String),
    Float(f64),
    Str(String),
    List(Vec<J>),
    Node(Box<Node>),
}

#[derive(Clone, Debug)]
pub struct Node {
    pub kind: &'static str,
    pub fields: Vec<(&'static str, J)>,
    /// 只给 `超()` 脱糖打标记用（Python 侧是 `setattr(fn, "_super_ok", True)`），
    /// **不参与序列化** —— `jsonable` 只走 `dataclasses.fields`。
    pub super_ok: bool,
}

impl Node {
    pub fn new(kind: &'static str, line: i64, col: i64) -> Node {
        Node {
            kind,
            fields: vec![("line", J::Int(line)), ("col", J::Int(col))],
            super_ok: false,
        }
    }

    pub fn f(mut self, name: &'static str, v: J) -> Node {
        self.fields.push((name, v));
        self
    }

    pub fn boxed(self) -> J {
        J::Node(Box::new(self))
    }

    pub fn get(&self, name: &str) -> Option<&J> {
        self.fields.iter().find(|(k, _)| *k == name).map(|(_, v)| v)
    }

    pub fn get_mut(&mut self, name: &str) -> Option<&mut J> {
        self.fields.iter_mut().find(|(k, _)| *k == name).map(|(_, v)| v)
    }
}

/// 建一个节点并直接包成 `J`（少写一层 `boxed()`）。
fn nj(kind: &'static str, line: i64, col: i64, fields: Vec<(&'static str, J)>) -> J {
    let mut n = Node::new(kind, line, col);
    n.fields.extend(fields);
    n.boxed()
}

fn s(v: &str) -> J {
    J::Str(v.to_string())
}

// ---------------------------------------------------------------------------
// 语法错误
// ---------------------------------------------------------------------------

pub struct ParseErr {
    pub code: &'static str,
    pub message: String,
    pub line: usize,
    pub col: usize,
    pub hint: Option<String>,
    pub underline: Option<(usize, usize)>,
    /// 机器可读的替换建议 `{"old":…, "new":…}`（对应 `JishiError.fix`）
    pub fix: Option<(String, String)>,
}

impl ParseErr {
    fn new(code: &'static str, message: impl Into<String>, line: usize, col: usize)
        -> Self {
        ParseErr { code, message: message.into(), line, col, hint: None,
                   underline: None, fix: None }
    }
}

const E_UNEXPECTED: &str = "E0201";
const E_BLOCK: &str = "E0202";
const E_COLON: &str = "E0203";
const E_NAME: &str = "E0204";
const E_UNCLOSED: &str = "E0205";

// ---------------------------------------------------------------------------
// 常量与提示表
// ---------------------------------------------------------------------------

/// 文本插值脱糖用的**内部保留名**（M40）。`$` 在词法阶段就写不出来，所以
/// 用户代码永远遮蔽不了它（用内建 `文本` 会被一句 `导入 文本` 撞掉）。
const INTERP_TO_TEXT: &str = "$文本";

/// `超()` 在 AST 里的名字（M23.4，非关键字，靠解析期脱糖识别）
const SUPER_NAME: &str = "超";

const MATCH_HINT: &str = "写法是：
匹配 分数：
    情形 90：
        打印(\"优秀\")
    情形 80, 85：
        打印(\"良好\")
    情形 其他：
        打印(\"继续努力\")

「情形」后面可以写多个值（逗号分隔，命中任一个即可）、
可以加守卫（`情形 90 如果 有加分：`），
「情形 其他」是兜底，要放在最后。";

const CLASS_HINT: &str = "类体里只放两样东西：

类 甲：
    计数 = 0                # 1. 类变量（名字 = 值）
    函数 打招呼(自身)：      # 2. 方法（函数）
        打印(\"你好\")

空类写一个单独的冒号占位：
类 空类：
    :";

const ENUM_HINT: &str = "写法是：
枚举 颜色：
    红 = 1
    绿 = 2

成员也可以不写值，让它自动接着编号：
枚举 方向：
    上
    下
    左
    右

用的时候写 `颜色.红`；取名字和值用 `颜色.红.名字` / `颜色.红.值`；
遍历所有成员用 `遍历 c 在 颜色.全部：`。";

/// 英文语句关键字 → 中文对应（M7.5 容错解析）
fn en_stmt_hint(w: &str) -> Option<&'static str> {
    Some(match w {
        "def" => "函数", "if" => "如果", "else" => "否则", "elif" => "否则如果",
        "for" => "遍历", "while" => "当", "return" => "返回", "import" => "导入",
        "from" => "从", "class" => "类", "try" => "尝试", "except" => "捕获",
        "finally" => "最终", "raise" => "抛出", "break" => "中断",
        "continue" => "继续", "with" => "用",
        _ => return None,
    })
}

/// 英文值 / 运算符关键字 → 中文对应
fn en_value_hint(w: &str) -> Option<&'static str> {
    Some(match w {
        "True" => "真", "False" => "假", "None" => "空",
        "and" => "与", "or" => "或", "not" => "非",
        "in" => "在", "not in" => "不在", "is" => "是", "is not" => "不是",
        _ => return None,
    })
}

/// 英文内建函数 → 中文对应
fn en_builtin_hint(w: &str) -> Option<&'static str> {
    Some(match w {
        "print" => "打印", "input" => "输入", "len" => "长度", "range" => "范围",
        "int" => "整数", "float" => "小数", "str" => "文本", "type" => "类型",
        "min" => "最小", "max" => "最大", "sum" => "总和", "reversed" => "反转",
        _ => return None,
    })
}

/// 英文语法基石没有对应 → 直接给说明
fn en_unsupported(w: &str) -> Option<&'static str> {
    Some(match w {
        "pass" => "空代码块直接写一个冒号「:」占位，不需要 pass",
        "lambda" => "基石没有匿名函数，请用「函数 名字(参数)：」定义",
        "with" => "基石暂不支持 with，可用「尝试/最终」管理资源",
        "del" => "基石没有 del，变量不需要手动删除",
        "global" => "基石没有 global，函数内可直接读写外层变量",
        "yield" => "基石暂不支持生成器 yield",
        "async" => "基石暂不支持异步 async",
        "await" => "基石暂不支持异步 await",
        _ => return None,
    })
}

#[inline]
fn bin_prec(op: &str) -> Option<i64> {
    Some(match op {
        "或" => 1,
        "与" => 2,
        "==" | "!=" | "<" | ">" | "<=" | ">=" | "在" | "不在" | "是" | "不是" => 3,
        "+" | "-" => 4,
        "*" | "/" | "//" | "%" => 5,
        "**" => 6,
        _ => return None,
    })
}

/// 关键字形式的比较运算符（M23.2）
#[inline]
fn is_keyword_cmp(op: &str) -> bool {
    matches!(op, "在" | "不在" | "是" | "不是")
}

#[inline]
fn is_cmp_op(op: &str) -> bool {
    matches!(op, "==" | "!=" | "<" | ">" | "<=" | ">=") || is_keyword_cmp(op)
}

/// 「非」的优先级（M23 修正）：与比较同级
const NOT_PREC: i64 = 3;

#[inline]
fn is_assign_op(op: &str) -> bool {
    matches!(op, "=" | "+=" | "-=" | "*=" | "/=" | "//=" | "%=" | "**=")
}

/// 形参种类（与 `opcodes.ParamKind` / C 侧 JS_PARAM_* 同源）
const PK_NORMAL: i64 = 0;
const PK_VARARGS: i64 = 1;
const PK_VARKW: i64 = 2;

// ---------------------------------------------------------------------------
// token 的一些便利
// ---------------------------------------------------------------------------

fn tok_is_str(t: &Tok, v: &str) -> bool {
    matches!(&t.value, Value::Str(x) if x == v)
}

/// Python 侧 `str(token.value)` 的近似（只用于报错文案）
/// 有些 token 的 `value` **不是给人看的**：插值字符串（FSTRING）存的是一张
/// 分段表。直接把它塞进中文报错会把 Python 的 `repr` 泄出去
/// （实测漏出来过：`「[('text', '导\t!=…')]」`），用户看不懂；而这个 `repr`
/// 的转义规则（`\t` / `\u3000` / 引号选择…）要在 Rust 侧逐字复刻是个填不满的坑。
/// 所以凡是要把 token 值写进文案的地方一律说人话 —— 与 Python 的
/// `_TOK_LABELS` / `_tok_text` **同一份口径**（两边改要一起改）。
pub const FSTRING_LABEL: &str = "插值字符串";
pub const EOF_LABEL: &str = "文件末尾";

/// 右括号 → 左括号（E0205 用）。不是右括号就是 `None`。
fn opener_of(op: &str) -> Option<&'static str> {
    match op {
        ")" => Some("("),
        "]" => Some("["),
        "}" => Some("{"),
        _ => None,
    }
}

fn tok_text(t: &Tok) -> String {
    // ⚠️ `EOF` 的 value 是 `None`，不给名字就会把 Python 的 `None` 印进中文报错
    // （「这里需要一个值，但看到了「None」」）—— 与 Python 侧的 `_TOK_LABELS`
    // 是同一张表的两份，改一处必须改另一处。
    if t.kind == "EOF" {
        return EOF_LABEL.to_string();
    }
    match &t.value {
        Value::None => "None".to_string(),
        Value::Int(i) => i.to_string(),
        Value::Big(b) => b.clone(),
        Value::Float(f) => py_float_repr(*f),
        Value::Str(x) => x.clone(),
        Value::Parts(_) => FSTRING_LABEL.to_string(),
    }
}

/// Python `repr(float)` 的近似（整数值要带 `.0`）。
fn py_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "nan".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf".into() } else { "-inf".into() };
    }
    let t = format!("{}", f);
    if t.contains('.') || t.contains('e') || t.contains('E') {
        t
    } else {
        format!("{}.0", t)
    }
}

/// token 的值 → `J`（供 `A.Num(value=…)` / `A.Str(value=…)` 用）
fn tok_val_j(t: &Tok) -> J {
    match &t.value {
        Value::None => J::Null,
        Value::Int(i) => J::Int(*i),
        Value::Big(b) => J::Big(b.clone()),
        Value::Float(f) => J::Float(*f),
        Value::Str(x) => J::Str(x.clone()),
        Value::Parts(_) => J::Null,
    }
}

/// 十进制字符串 +1（给「枚举成员自动编号」用 —— Python 那边是任意精度整数）
fn big_inc(s: &str) -> String {
    let mut d: Vec<u8> = s.bytes().map(|b| b - b'0').collect();
    let mut i = d.len();
    loop {
        if i == 0 {
            d.insert(0, 1);
            break;
        }
        i -= 1;
        if d[i] == 9 {
            d[i] = 0;
        } else {
            d[i] += 1;
            break;
        }
    }
    d.iter().map(|x| (x + b'0') as char).collect()
}

// ---------------------------------------------------------------------------
// 解析器
// ---------------------------------------------------------------------------

/// `_parse_statement` 可能返回多条语句（解析期脱糖的那几个）
enum Stmt {
    One(Node),
    /// 解析期脱糖的语句**一次产出多条**（用/匹配/枚举），展平时用
    Many(Vec<J>),
}

struct Params {
    names: Vec<String>,
    defaults: Vec<J>,
    anns: Vec<Option<String>>,
    kinds: Vec<i64>,
}

pub struct Parser {
    toks: Vec<Tok>,
    filename: String,
    pos: usize,
    with_seq: u32,
    match_seq: u32,
}

type R<T> = Result<T, ParseErr>;

impl Parser {
    pub fn new(toks: Vec<Tok>, filename: &str) -> Parser {
        Parser { toks, filename: filename.to_string(), pos: 0,
                 with_seq: 0, match_seq: 0 }
    }

    // -- 基础工具 ----------------------------------------------------------

    fn peek(&self, k: usize) -> &Tok {
        let idx = (self.pos + k).min(self.toks.len() - 1);
        &self.toks[idx]
    }

    fn cur(&self) -> &Tok {
        self.peek(0)
    }

    fn advance(&mut self) -> Tok {
        let t = self.toks[self.pos].clone();
        if t.kind != "EOF" {
            self.pos += 1;
        }
        t
    }

    fn at(&self, type_: &str) -> bool {
        self.cur().kind == type_
    }

    fn at_op(&self, op: &str) -> bool {
        let t = self.cur();
        t.kind == "OP" && tok_is_str(t, op)
    }

    fn at_kw(&self, kw: &str) -> bool {
        let t = self.cur();
        t.kind == "KEYWORD" && tok_is_str(t, kw)
    }

    /// 容错解析（M7.5）：英文关键字/内建 → 给出中文建议。
    fn check_en_word(&self, t: &Tok, mode: &str) -> R<()> {
        let w = match &t.value {
            Value::Str(x) => x.as_str(),
            _ => return Ok(()),
        };
        if mode != "newline" {
            if let Some(cn) = en_stmt_hint(w) {
                return Err(self.err_tok(
                    E_UNEXPECTED,
                    format!("「{}」是英文关键字，基石用中文——你是不是想写「{}」？",
                            w, cn),
                    Some(format!("把英文关键字换成对应的中文关键字即可。\n例如：{} → {}",
                                 w, cn)),
                    t, Some((w.to_string(), cn.to_string()))));
            }
        }
        if let Some(cn) = en_value_hint(w) {
            return Err(self.err_tok(
                E_UNEXPECTED,
                format!("「{}」是英文写法，基石用中文——你是不是想写「{}」？", w, cn),
                Some(format!("把「{}」换成「{}」即可", w, cn)),
                t, Some((w.to_string(), cn.to_string()))));
        }
        if mode != "newline" {
            if let Some(expl) = en_unsupported(w) {
                return Err(self.err_tok(
                    E_UNEXPECTED,
                    format!("「{}」是 Python 的写法，基石里没有这个关键字", w),
                    Some(expl.to_string()), t, None));
            }
        }
        if mode == "atom" {
            if let Some(cn) = en_builtin_hint(w) {
                if self.at_op("(") {
                    return Err(self.err_tok(
                        E_UNEXPECTED,
                        format!("「{}」是 Python 的内建函数，基石里叫「{}」", w, cn),
                        Some(format!("把 {}(…) 改成 {}(…)", w, cn)),
                        t, Some((w.to_string(), cn.to_string()))));
                }
            }
        }
        Ok(())
    }

    fn err_tok(&self, code: &'static str, msg: impl Into<String>,
               hint: Option<String>, t: &Tok, fix: Option<(String, String)>)
        -> ParseErr {
        let mut e = ParseErr::new(code, msg, t.line, t.col);
        e.hint = hint;
        e.underline = Some((t.col, t.end_col));
        e.fix = fix;
        e
    }

    /// 当前位置报错（对应 `Parser._err`，`tok=None` 时取当前 token）
    fn err_here(&self, code: &'static str, msg: impl Into<String>,
                hint: Option<String>) -> ParseErr {
        let t = self.cur();
        self.err_tok(code, msg, hint, t, None)
    }

    fn expect_op(&mut self, op: &str) -> R<()> {
        if self.at_op(op) {
            self.advance();
            return Ok(());
        }
        let t = self.cur().clone();
        // 容错解析（M7.5 扩展）：表达式里冒出英文运算符要先说清（M23 补）
        if t.kind == "NAME" {
            let tv = match &t.value { Value::Str(x) => x.clone(), _ => String::new() };
            if en_value_hint(&tv).is_some() {
                let nxt = self.peek(1).clone();
                let nxt_is_name = nxt.kind == "NAME";
                let pair = if nxt_is_name {
                    Some(format!("{} {}", tv, tok_text(&nxt)))
                } else {
                    None
                };
                if let Some(p) = &pair {
                    if let Some(cn) = en_value_hint(p) {
                        return Err(self.err_tok(
                            E_UNEXPECTED,
                            format!("「{}」是英文写法，基石用中文——你是不是想写「{}」？",
                                    p, cn),
                            Some(format!("把「{}」换成「{}」即可", p, cn)),
                            &t, Some((p.clone(), cn.to_string()))));
                    }
                }
                let cn = en_value_hint(&tv).unwrap();
                return Err(self.err_tok(
                    E_UNEXPECTED,
                    format!("「{}」是英文运算符，基石用中文——你是不是想写「{}」？",
                            tv, cn),
                    Some(format!("把「{}」换成「{}」即可", tv, cn)),
                    &t, Some((tv.clone(), cn.to_string()))));
            }
        }
        // 期望右括号、而**已经到文件末尾** —— 这就是「括号没有闭合」（E0205）。
        // ⚠️ 只有「走到末尾都没找到」才算：如果中途撞上了别的东西
        // （`[1, 2` 后面换行写了下一句），那时报 E0201 并说出「看到了什么」
        // 对用户更有用 —— 别为了把码归拢而把那条信息丢掉。
        if let Some(opener) = opener_of(op) {
            if t.kind == "EOF" {
                match self.find_unclosed_opener(op) {
                    Some(o) => {
                        return Err(self.err_tok(
                            E_UNCLOSED,
                            format!("这里需要「{}」，但已经到文件末尾了", op),
                            Some(format!("给这里这个「{}」补上对应的「{}」。",
                                         opener, op)),
                            &o, None));
                    }
                    None => {
                        return Err(self.err_tok(
                            E_UNCLOSED,
                            format!("这里需要「{}」，但已经到文件末尾了", op),
                            Some("往回找最近的那个没闭合的开括号，补上它对应的右括号。"
                                 .to_string()),
                            &t, None));
                    }
                }
            }
        }
        Err(self.err_tok(E_UNEXPECTED,
                         format!("这里需要「{}」，但看到了「{}」", op, tok_text(&t)),
                         None, &t, None))
    }

    /// 从当前位置**往回**找最近的那个没闭合的开括号（给 E0205 定位用）。
    ///
    /// 为什么非要往回找：EOF 那个 token 的位置是「最后一行第 1 列」、
    /// `src` 还是空的 —— 指着它对用户毫无帮助，连源码行都显示不出来。
    /// 而**没闭合的那个开括号**才是要动手改的地方。
    /// 这是错误路径上的 O(n) 扫描，正常解析走不到。
    fn find_unclosed_opener(&self, op: &str) -> Option<Tok> {
        let opener = opener_of(op)?;
        let mut depth = 0i32;
        let mut i = self.pos;
        while i > 0 {
            i -= 1;
            let t = &self.toks[i];
            if t.kind != "OP" {
                continue;
            }
            if tok_is_str(t, op) {
                depth += 1;
            } else if tok_is_str(t, opener) {
                if depth == 0 {
                    return Some(t.clone());
                }
                depth -= 1;
            }
        }
        None
    }

    fn expect_kw(&mut self, kw: &str) -> R<()> {
        if self.at_kw(kw) {
            self.advance();
            return Ok(());
        }
        let t = self.cur().clone();
        Err(self.err_tok(E_UNEXPECTED,
                         format!("这里需要关键字「{}」，但看到了「{}」", kw,
                                 tok_text(&t)),
                         None, &t, None))
    }

    fn expect_name(&mut self, what: &str) -> R<Tok> {
        if self.cur().kind == "NAME" {
            return Ok(self.advance());
        }
        Err(self.err_here(E_NAME, format!("这里需要一个{}", what), None))
    }

    /// `.` 后面的属性名 —— **允许关键字**（M34：`正则.匹配` 不能被新关键字撞掉）
    fn expect_attr_name(&mut self) -> R<Tok> {
        let t = self.cur();
        if t.kind == "NAME" || t.kind == "KEYWORD" {
            return Ok(self.advance());
        }
        Err(self.err_here(E_NAME, "这里需要一个属性名", None))
    }

    fn expect_newline(&mut self) -> R<()> {
        if self.at("NEWLINE") {
            self.advance();
            return Ok(());
        }
        // 容错解析：行尾残留英文运算符 → 中文建议
        let nxt = self.cur().clone();
        if nxt.kind == "NAME" {
            if let Value::Str(v) = &nxt.value {
                if en_value_hint(v).is_some() {
                    self.check_en_word(&nxt, "newline")?;
                }
            }
        }
        Err(self.err_here(
            E_UNEXPECTED,
            "这一行没写完，但下一行已经开始了——是不是少了个符号？", None))
    }

    fn expect_colon(&mut self, kw: &str) -> R<()> {
        if self.at_op(":") {
            self.advance();
            return Ok(());
        }
        Err(self.err_here(
            E_COLON,
            format!("「{}」后面要跟冒号「：」，然后换行缩进写代码块", kw), None))
    }

    /// 按 AST 节点位置报错（块解析完之后拿不到 token 时用）
    fn err_at(&self, code: &'static str, msg: impl Into<String>, node: &Node,
              hint: Option<String>) -> ParseErr {
        let line = match node.get("line") { Some(J::Int(v)) => *v as usize, _ => 1 };
        let col = match node.get("col") { Some(J::Int(v)) => *v as usize, _ => 1 };
        let mut e = ParseErr::new(code, msg, line, col);
        e.hint = hint;
        e.underline = Some((col, col + 1));
        e
    }

    // -- 程序入口 ----------------------------------------------------------

    pub fn parse(&mut self) -> R<Node> {
        let body = self.parse_statements(&["EOF"])?;
        Ok(Node::new("Program", 1, 1).f("body", J::List(body)))
    }

    fn parse_statements(&mut self, until: &[&str]) -> R<Vec<J>> {
        let mut stmts: Vec<J> = Vec::new();
        loop {
            let t = self.cur().clone();
            let ty = t.kind;
            if until.contains(&ty) || ty == "EOF" {
                break;
            }
            if ty == "NEWLINE" {
                self.advance();
                continue;
            }
            if ty == "DEDENT" {
                if until.contains(&"DEDENT") {
                    break;
                }
                return Err(self.err_tok(E_UNEXPECTED, "这里多了一个退格缩进",
                                        None, &t, None));
            }
            match self.parse_statement()? {
                Stmt::Many(list) => {
                    stmts.extend(list);
                    continue;
                }
                Stmt::One(stmt) => {
                    let kind = stmt.kind;
                    stmts.push(stmt.boxed());
                    // 块语句自带结构，以 DEDENT 结束，不需要（也没有）行尾 NEWLINE
                    if matches!(kind, "If" | "For" | "Loop" | "While" | "FuncDef"
                                | "ClassDef" | "Try" | "Pass") {
                        continue;
                    }
                    if !self.at("NEWLINE") && self.cur().kind != "EOF" {
                        self.expect_newline()?;
                    } else if self.at("NEWLINE") {
                        self.advance();
                    }
                }
            }
        }
        Ok(stmts)
    }

    // -- 语句 --------------------------------------------------------------

    fn parse_statement(&mut self) -> R<Stmt> {
        let t = self.cur().clone();
        if t.kind == "KEYWORD" {
            let kw = match &t.value { Value::Str(x) => x.clone(), _ => String::new() };
            let kw = kw.as_str();
            // 真/假/空/非 作为值开头的表达式语句
            if matches!(kw, "真" | "假" | "空" | "非") {
                return Ok(Stmt::One(self.parse_expr_stmt()?));
            }
            match kw {
                "令" => return Ok(Stmt::One(self.parse_assign(true)?)),
                "如果" => return Ok(Stmt::One(self.parse_if()?)),
                "遍历" => return Ok(Stmt::One(self.parse_for()?)),
                "循环" => return Ok(Stmt::One(self.parse_loop()?)),
                "当" => return Ok(Stmt::One(self.parse_while()?)),
                "中断" => {
                    self.advance();
                    return Ok(Stmt::One(Node::new("Break", t.line as i64,
                                                  t.col as i64)));
                }
                "继续" => {
                    self.advance();
                    return Ok(Stmt::One(Node::new("Continue", t.line as i64,
                                                  t.col as i64)));
                }
                "函数" => return Ok(Stmt::One(self.parse_funcdef()?)),
                "类" => return Ok(Stmt::One(self.parse_classdef()?)),
                "返回" => return Ok(Stmt::One(self.parse_return()?)),
                "导入" => return Ok(Stmt::One(self.parse_import()?)),
                "尝试" => return Ok(Stmt::One(self.parse_try()?)),
                "用" => return Ok(Stmt::Many(self.parse_with()?)),
                "匹配" => return Ok(Stmt::Many(self.parse_match()?)),
                "枚举" => return Ok(Stmt::Many(self.parse_enum()?)),
                "情形" => {
                    return Err(self.err_tok(
                        E_UNEXPECTED, "「情形」只能写在「匹配」的代码块里",
                        Some(MATCH_HINT.to_string()), &t, None));
                }
                "抛出" => return Ok(Stmt::One(self.parse_raise()?)),
                "捕获" | "最终" => {
                    return Err(self.err_tok(
                        E_UNEXPECTED,
                        format!("「{}」必须跟在「尝试」的代码块后面", kw),
                        Some("写法是：\n尝试：\n    …\n捕获 值错误 为 e：\n    …"
                             .to_string()),
                        &t, None));
                }
                _ => {
                    return Err(self.err_tok(
                        E_UNEXPECTED,
                        format!("不能以关键字「{}」开头写这句话", kw),
                        None, &t, None));
                }
            }
        }

        if t.kind == "NAME" {
            // 容错解析（M7.5）：英文关键字/内建 → 中文建议
            self.check_en_word(&t, "stmt")?;
            if self.looks_like_assign() {
                return Ok(Stmt::One(self.parse_assign(false)?));
            }
            return Ok(Stmt::One(self.parse_expr_stmt()?));
        }

        // 直接以值开头的表达式语句
        let is_value_start = matches!(t.kind, "NUMBER" | "STRING")
            || (t.kind == "OP" && matches!(&t.value, Value::Str(x)
                                           if x == "[" || x == "(" || x == "-"))
            || (t.kind == "KEYWORD"
                && matches!(&t.value, Value::Str(x)
                            if x == "真" || x == "假" || x == "空" || x == "非"));
        if is_value_start {
            return Ok(Stmt::One(self.parse_expr_stmt()?));
        }

        if t.kind == "OP" && tok_is_str(&t, ":") {
            // 空块占位：单独的冒号行表示什么都不做
            self.advance();
            return Ok(Stmt::One(Node::new("Pass", t.line as i64, t.col as i64)));
        }

        Err(self.err_tok(E_UNEXPECTED,
                         format!("不知道怎么写这句话：{}", describe(&t)),
                         None, &t, None))
    }

    /// 前瞻：判断从当前位置开始是否是「名字 + 下标/属性 + 赋值符」的赋值语句
    fn looks_like_assign(&self) -> bool {
        if self.cur().kind != "NAME" {
            return false;
        }
        let mut i = self.pos + 1;
        let n = self.toks.len();
        while i < n {
            let t = &self.toks[i];
            if t.kind == "OP" && tok_is_str(t, "[") {
                let mut depth = 1;
                i += 1;
                while i < n && depth > 0 {
                    let c = &self.toks[i];
                    if c.kind == "OP" && tok_is_str(c, "[") {
                        depth += 1;
                    } else if c.kind == "OP" && tok_is_str(c, "]") {
                        depth -= 1;
                    }
                    i += 1;
                }
                continue;
            }
            if t.kind == "OP" && tok_is_str(t, ".") && i + 1 < n
                && self.toks[i + 1].kind == "NAME"
            {
                i += 2;
                continue;
            }
            break;
        }
        i < n && self.toks[i].kind == "OP"
            && matches!(&self.toks[i].value, Value::Str(x) if is_assign_op(x))
    }

    fn parse_assign(&mut self, decl: bool) -> R<Node> {
        let (start_line, start_col);
        let target: Node;
        if decl {
            let start = self.advance();      // 「令」
            start_line = start.line as i64;
            start_col = start.col as i64;
            if self.at_op("*") {
                let star = self.cur().clone();
                return Err(self.err_tok(
                    E_UNEXPECTED,
                    "「令」后面不能直接写「*」——星号解包至少要有一个固定名字",
                    Some("写成 令 甲, *余 = 列表（星号放中间或末尾），\
                          或只收集全部：令 余 = 列表".to_string()),
                    &star, None));
            }
            let target_tok = self.expect_name("变量名")?;
            let mut target_node = Node::new("Name", target_tok.line as i64,
                                            target_tok.col as i64);
            let tv = match &target_tok.value {
                Value::Str(x) => x.clone(), _ => String::new(),
            };
            target_node = target_node.f("id", s(&tv));

            if self.at_op(",") {
                let mut names: Vec<J> = vec![target_node.boxed()];
                let mut star_index: Option<i64> = None;
                while self.at_op(",") {
                    self.advance();
                    if self.at_op("*") {
                        let star_tok = self.advance();
                        if star_index.is_some() {
                            return Err(self.err_tok(
                                E_UNEXPECTED, "解包赋值里只能有一个「*」",
                                None, &star_tok, None));
                        }
                        star_index = Some((names.len() - 1) as i64 + 1);
                    } else if self.at_op("**") {
                        let tt = self.cur().clone();
                        return Err(self.err_tok(
                            E_UNEXPECTED,
                            "解包赋值里只能用「*」收集剩下的，「**」是收集关键字用的",
                            None, &tt, None));
                    }
                    let tk = self.expect_name("变量名")?;
                    let v = match &tk.value {
                        Value::Str(x) => x.clone(), _ => String::new(),
                    };
                    names.push(Node::new("Name", tk.line as i64, tk.col as i64)
                               .f("id", s(&v)).boxed());
                }
                target = Node::new("TargetList", start_line, start_col)
                    .f("names", J::List(names))
                    .f("star_index", match star_index {
                        Some(i) => J::Int(i), None => J::Null });
            } else {
                target = target_node;
            }
        } else {
            let start = self.cur().clone();
            start_line = start.line as i64;
            start_col = start.col as i64;
            target = node_from_j(self.parse_postfix()?)?;
        }
        let op_tok = self.advance();
        let op = match &op_tok.value {
            Value::Str(x) if op_tok.kind == "OP" && is_assign_op(x) => x.clone(),
            _ => {
                return Err(self.err_tok(
                    E_UNEXPECTED,
                    format!("这里需要一个赋值符号（=、+= 等），但看到了「{}」",
                            tok_text(&op_tok)),
                    None, &op_tok, None));
            }
        };
        if target.kind == "TargetList" && op != "=" {
            return Err(self.err_tok(E_UNEXPECTED,
                                    "多赋值（解包）只能用「=」，不能用复合赋值",
                                    None, &op_tok, None));
        }
        let value = if target.kind == "TargetList" {
            self.parse_value_tuple()?
        } else {
            self.parse_expr(0, true)?
        };
        Ok(Node::new("Assign", start_line, start_col)
           .f("target", target.boxed())
           .f("op", s(&op))
           .f("value", value))
    }

    fn parse_expr_stmt(&mut self) -> R<Node> {
        if let Some(tup) = self.try_parse_tuple_assign()? {
            return Ok(tup);
        }
        let start = self.cur().clone();
        let expr = self.parse_expr(0, true)?;
        Ok(Node::new("ExprStmt", start.line as i64, start.col as i64)
           .f("expr", expr))
    }

    /// 赋值右值：表达式 (, 表达式)*。多个时包装成列表（多值即列表）。
    fn parse_value_tuple(&mut self) -> R<J> {
        let first = self.parse_expr(0, true)?;
        if !self.at_op(",") {
            return Ok(first);
        }
        let start = self.cur().clone();
        let mut elements = vec![first];
        while self.at_op(",") {
            self.advance();
            elements.push(self.parse_expr(0, true)?);
        }
        Ok(nj("List", start.line as i64, start.col as i64,
              vec![("elements", J::List(elements))]))
    }

    /// 把 FSTRING 分段组装成字符串拼接（M7 文本插值）
    fn parse_fstring(&mut self, tok: &Tok) -> R<J> {
        let parts: Vec<(String, String)> = match &tok.value {
            Value::Parts(ps) => ps.iter().map(|p| match p {
                Part::Text(x) => ("text".to_string(), x.clone()),
                Part::Expr(x) => ("expr".to_string(), x.clone()),
            }).collect(),
            _ => Vec::new(),
        };
        let line = tok.line as i64;
        let col = tok.col as i64;
        let mut nodes: Vec<J> = Vec::new();
        for (kind, val) in parts {
            if kind == "text" {
                nodes.push(nj("Str", line, col, vec![("value", s(&val))]));
            } else {
                let sub_tokens = tokenizer::tokenize(&val, &self.filename)
                    .map_err(|e| lex_to_parse(e))?;
                let mut sub = Parser::new(sub_tokens.tokens, &self.filename);
                let expr = sub.parse_expr(0, true)?;
                nodes.push(nj("Call", line, col, vec![
                    ("func", nj("Name", line, col,
                                vec![("id", s(INTERP_TO_TEXT))])),
                    ("args", J::List(vec![expr])),
                    ("keywords", J::List(vec![])),
                ]));
            }
        }
        if nodes.is_empty() {
            return Ok(nj("Str", line, col, vec![("value", s(""))]));
        }
        let mut result = nodes.remove(0);
        for n in nodes {
            result = nj("BinOp", line, col, vec![
                ("left", result), ("op", s("+")), ("right", n)]);
        }
        Ok(result)
    }

    /// 推导式尾部：遍历 变量 在 可迭代 [如果 条件]（M7）
    fn parse_comp_tail(&mut self, kind: &str, elt: J, key: J, value: J,
                       line: i64, col: i64) -> R<J> {
        self.expect_kw("遍历")?;
        let target_tok = self.expect_name("循环变量名")?;
        self.expect_kw("在")?;
        // 可迭代对象**不认条件表达式**（M36）
        let it = self.parse_expr(0, false)?;
        let tv = match &target_tok.value {
            Value::Str(x) => x.clone(), _ => String::new(),
        };
        let target = nj("Name", target_tok.line as i64,
                        target_tok.col as i64, vec![("id", s(&tv))]);
        let condition = if self.at_kw("如果") {
            self.advance();
            self.parse_expr(0, true)?
        } else {
            J::Null
        };
        Ok(nj("Comprehension", line, col, vec![
            ("kind", s(kind)), ("elt", elt), ("key", key), ("value", value),
            ("target", target), ("iter", it), ("condition", condition)]))
    }

    /// 探测「名字 , 名字 … = 表达式」；不匹配则返回 None 不消耗 token
    fn try_parse_tuple_assign(&mut self) -> R<Option<Node>> {
        let mut i = 0usize;
        let mut specs: Vec<(bool, Tok)> = Vec::new();
        loop {
            let mut t = self.peek(i).clone();
            let mut is_star = false;
            if t.kind == "OP" && tok_is_str(&t, "*") {
                is_star = true;
                t = self.peek(i + 1).clone();
                i += 1;
            }
            if t.kind != "NAME" {
                return Ok(None);
            }
            let nxt = self.peek(i + 1).clone();
            if nxt.kind == "OP" && tok_is_str(&nxt, ",") {
                specs.push((is_star, t));
                i += 2;
                continue;
            }
            if nxt.kind == "OP" && tok_is_str(&nxt, "=") && !specs.is_empty() {
                specs.push((is_star, t));
                break;
            }
            return Ok(None);
        }

        let first = specs[0].1.clone();
        let mut nodes: Vec<J> = Vec::new();
        let mut star_index: Option<i64> = None;
        for (k, (is_star, tok)) in specs.iter().enumerate() {
            if *is_star {
                self.advance();       // 消费「*」
                if star_index.is_some() {
                    return Err(self.err_tok(E_UNEXPECTED,
                                            "解包赋值里只能有一个「*」",
                                            None, tok, None));
                }
                star_index = Some(k as i64);
            }
            let name_tok = self.advance();
            let v = match &name_tok.value {
                Value::Str(x) => x.clone(), _ => String::new(),
            };
            nodes.push(nj("Name", name_tok.line as i64, name_tok.col as i64,
                          vec![("id", s(&v))]));
            if k < specs.len() - 1 {
                self.advance();       // 消费逗号
            }
        }
        self.advance();               // 消费「=」
        let value = self.parse_value_tuple()?;
        Ok(Some(Node::new("Assign", first.line as i64, first.col as i64)
                .f("target", nj("TargetList", first.line as i64,
                                first.col as i64, vec![
                                    ("names", J::List(nodes)),
                                    ("star_index", match star_index {
                                        Some(i) => J::Int(i), None => J::Null }),
                                ]))
                .f("op", s("="))
                .f("value", value)))
    }

    fn parse_if(&mut self) -> R<Node> {
        let start = self.advance();           // 如果
        let mut branches: Vec<J> = Vec::new();
        let test = self.parse_expr(0, true)?;
        self.expect_colon("如果")?;
        let body = self.parse_block()?;
        branches.push(J::List(vec![test, J::List(body)]));
        let mut orelse: Option<Vec<J>> = None;
        while self.at_kw("否则") || self.at_kw("否则如果") {
            let tok = self.advance();
            let is_elif = tok_is_str(&tok, "否则如果") || self.at_kw("如果");
            if is_elif {
                if tok_is_str(&tok, "否则") {
                    self.advance();           // 消费「如果」
                }
                let t2 = self.parse_expr(0, true)?;
                self.expect_colon("否则如果")?;
                let b2 = self.parse_block()?;
                branches.push(J::List(vec![t2, J::List(b2)]));
            } else {
                self.expect_colon("否则")?;
                orelse = Some(self.parse_block()?);
                break;
            }
        }
        Ok(Node::new("If", start.line as i64, start.col as i64)
           .f("branches", J::List(branches))
           .f("orelse", match orelse {
               Some(v) => J::List(v), None => J::Null }))
    }

    fn parse_for(&mut self) -> R<Node> {
        let start = self.advance();
        let first_tok = self.expect_name("循环变量名")?;
        let fv = match &first_tok.value {
            Value::Str(x) => x.clone(), _ => String::new(),
        };
        let mut names: Vec<J> = vec![nj("Name", first_tok.line as i64,
                                        first_tok.col as i64,
                                        vec![("id", s(&fv))])];
        let mut star_index: Option<i64> = None;
        while self.at_op(",") {
            self.advance();
            if self.at_op("*") {
                let star_tok = self.advance();
                if star_index.is_some() {
                    return Err(self.err_tok(E_UNEXPECTED,
                                            "解包遍历里只能有一个「*」",
                                            None, &star_tok, None));
                }
                star_index = Some(names.len() as i64);
            }
            let tk = self.expect_name("循环变量名")?;
            let v = match &tk.value { Value::Str(x) => x.clone(), _ => String::new() };
            names.push(nj("Name", tk.line as i64, tk.col as i64,
                          vec![("id", s(&v))]));
        }
        self.expect_kw("在")?;
        let it = self.parse_expr(0, true)?;
        self.expect_colon("遍历")?;
        let body = self.parse_block()?;
        let line = start.line as i64;
        let col = start.col as i64;
        if names.len() == 1 {
            return Ok(Node::new("For", line, col)
                      .f("target", names.remove(0))
                      .f("iter", it)
                      .f("body", J::List(body)));
        }
        // 多变量：脱糖（临时名带 `__`，用户写不出来）
        let tmp_name = format!("__遍历项_{}_{}__", start.line, start.col);
        let tmp = nj("Name", line, col, vec![("id", s(&tmp_name))]);
        let unpack = nj("Assign", line, col, vec![
            ("target", nj("TargetList", line, col, vec![
                ("names", J::List(names)),
                ("star_index", match star_index {
                    Some(i) => J::Int(i), None => J::Null })])),
            ("op", s("=")),
            ("value", tmp.clone()),
        ]);
        let mut new_body = vec![unpack];
        new_body.extend(body);
        Ok(Node::new("For", line, col)
           .f("target", tmp)
           .f("iter", it)
           .f("body", J::List(new_body)))
    }

    fn parse_loop(&mut self) -> R<Node> {
        let start = self.advance();
        let times = self.parse_expr(0, true)?;
        if self.at_kw("次") {
            self.advance();
        }
        self.expect_colon("循环")?;
        let body = self.parse_block()?;
        Ok(Node::new("Loop", start.line as i64, start.col as i64)
           .f("times", times)
           .f("body", J::List(body)))
    }

    fn parse_while(&mut self) -> R<Node> {
        let start = self.advance();
        let test = self.parse_expr(0, true)?;
        self.expect_colon("当")?;
        let body = self.parse_block()?;
        Ok(Node::new("While", start.line as i64, start.col as i64)
           .f("test", test)
           .f("body", J::List(body)))
    }

    fn parse_params(&mut self) -> R<Params> {
        self.expect_op("(")?;
        let mut params: Vec<String> = Vec::new();
        let mut defaults: Vec<J> = Vec::new();
        let mut anns: Vec<Option<String>> = Vec::new();
        let mut kinds: Vec<i64> = Vec::new();
        // "normal" → 还能接普通参数；"starred" → 只接 **选项；"done" → 收尾
        let mut state = "normal";
        let mut star_tok: Option<Tok> = None;

        if !self.at_op(")") {
            loop {
                let mut kind = PK_NORMAL;
                if self.at_op("**") || self.at_op("*") {
                    let is_dstar = matches!(&self.cur().value, Value::Str(x)
                                            if x == "**");
                    let tok = self.advance();
                    if is_dstar {
                        if state == "done" {
                            return Err(self.err_tok(E_UNEXPECTED,
                                                    "「**选项」只能有一个",
                                                    None, &tok, None));
                        }
                        state = "done";
                        kind = PK_VARKW;
                    } else {
                        if state != "normal" {
                            return Err(self.err_tok(
                                E_UNEXPECTED,
                                "「*参数」只能有一个，而且要写在「**选项」前面",
                                None, &tok, None));
                        }
                        state = "starred";
                        star_tok = Some(tok);
                        kind = PK_VARARGS;
                    }
                } else {
                    if state == "starred" {
                        let st = star_tok.clone().unwrap();
                        return Err(self.err_tok(
                            E_UNEXPECTED,
                            "「*参数」后面不能再有普通参数——多余的位置实参都归它收了",
                            Some("要传更多参数请用关键字，或把普通参数写在「*参数」前面"
                                 .to_string()), &st, None));
                    }
                    if state == "done" {
                        let tt = self.cur().clone();
                        return Err(self.err_tok(
                            E_UNEXPECTED, "「**选项」必须是最后一个参数",
                            None, &tt, None));
                    }
                }

                let p = self.expect_name("参数名")?;
                let pv = match &p.value { Value::Str(x) => x.clone(), _ => String::new() };
                params.push(pv.clone());
                kinds.push(kind);

                let ann = self.parse_annotation_opt()?;
                if kind != PK_NORMAL && ann.is_some() {
                    let sigil = if kind == PK_VARKW { "**" } else { "*" };
                    return Err(self.err_tok(
                        E_UNEXPECTED,
                        format!("「{}{}」是收集参数，不能写类型标注", sigil, pv),
                        Some("它收进来的元素类型不固定；要检查请在函数体里用 断言"
                             .to_string()), &p, None));
                }
                anns.push(ann);

                if self.at_op("=") {
                    if kind != PK_NORMAL {
                        let sigil = if kind == PK_VARKW { "**" } else { "*" };
                        return Err(self.err_tok(
                            E_UNEXPECTED,
                            format!("「{}{}」是收集参数，不能有默认值", sigil, pv),
                            Some("它没收到东西时自然是空列表/空字典".to_string()),
                            &p, None));
                    }
                    self.advance();
                    defaults.push(self.parse_expr(0, true)?);
                } else {
                    defaults.push(J::Null);
                }

                if self.at_op(",") {
                    self.advance();
                    if self.at_op(")") {      // 尾随逗号
                        break;
                    }
                    continue;
                }
                break;
            }
        }

        // 有默认值的参数必须排在最后（与 Python 一致）
        let mut seen_default = false;
        for i in 0..defaults.len() {
            if !matches!(defaults[i], J::Null) {
                seen_default = true;
            } else if seen_default && kinds[i] == PK_NORMAL {
                return Err(self.err_here(
                    E_UNEXPECTED,
                    format!("参数「{}」没有默认值，但它排在有默认值的参数后面——\
                             有默认值的参数要放在最后", params[i]),
                    None));
            }
        }
        self.expect_op(")")?;
        Ok(Params { names: params, defaults, anns, kinds })
    }

    fn parse_funcdef(&mut self) -> R<Node> {
        let start = self.advance();
        let name_tok = self.expect_name("函数名")?;
        let p = self.parse_params()?;
        // 返回类型标注（M24.3）：-> 类型名
        let mut returns: Option<String> = None;
        if self.at_op("->") {
            self.advance();
            returns = Some(self.parse_annotation_name("返回类型")?);
        }
        self.expect_colon("函数")?;
        let body = self.parse_block()?;
        let nv = match &name_tok.value {
            Value::Str(x) => x.clone(), _ => String::new(),
        };
        Ok(Node::new("FuncDef", start.line as i64, start.col as i64)
           .f("name", s(&nv))
           .f("params", J::List(p.names.iter().map(|x| s(x)).collect()))
           .f("body", J::List(body))
           .f("defaults", J::List(p.defaults))
           .f("annotations", J::List(p.anns.iter().map(|a| match a {
               Some(x) => s(x), None => J::Null }).collect()))
           .f("param_kind", J::List(p.kinds.iter().map(|k| J::Int(*k)).collect()))
           .f("returns", match returns { Some(x) => s(&x), None => J::Null }))
    }

    fn parse_annotation_name(&mut self, what: &str) -> R<String> {
        let tok = self.expect_name(what)?;
        let mut text = match &tok.value {
            Value::Str(x) => x.clone(), _ => String::new(),
        };
        while self.at_op(".") {
            self.advance();
            let n = self.expect_name(what)?;
            text += ".";
            text += &match &n.value { Value::Str(x) => x.clone(), _ => String::new() };
        }
        Ok(text)
    }

    fn parse_annotation_opt(&mut self) -> R<Option<String>> {
        if !self.at_op(":") {
            return Ok(None);
        }
        self.advance();
        Ok(Some(self.parse_annotation_name("类型名")?))
    }

    /// 类体只允许「方法 / 类变量 / 占位」——**解析期**统一校验（M35）
    fn check_class_body(&self, body: &[J]) -> R<()> {
        for item in body {
            let node = match item {
                J::Node(n) => n,
                _ => continue,
            };
            match node.kind {
                "FuncDef" | "Pass" => continue,
                "Assign" => {
                    let target_ok = matches!(node.get("target"),
                        Some(J::Node(t)) if t.kind == "Name");
                    let op_eq = matches!(node.get("op"), Some(J::Str(o)) if o == "=");
                    if target_ok && op_eq {
                        continue;               // 类变量
                    }
                    let (msg, hint) = if target_ok {
                        let id = match node.get("target") {
                            Some(J::Node(t)) => match t.get("id") {
                                Some(J::Str(x)) => x.clone(),
                                _ => String::new(),
                            },
                            _ => String::new(),
                        };
                        let op = match node.get("op") {
                            Some(J::Str(o)) => o.clone(), _ => String::new(),
                        };
                        (format!("类体里不能用「{} {}」——类变量只能在类体里用\
                                  一次性的「名字 = 值」声明", id, op),
                         "类变量要在类体里用一次性的「=」声明：\n类 甲：\n    \
                          计数 = 0          # 类变量\n    函数 加(自身)：    # 方法\n    \
                          甲.计数 += 1   # 改类变量写在方法里".to_string())
                    } else {
                        ("类体里不能对下标或属性赋值，只能用「名字 = 值」定义类变量"
                         .to_string(),
                         "类变量要在类体里用一次性的「=」声明：\n类 甲：\n    \
                          计数 = 0          # 类变量\n    函数 加(自身)：    # 方法\n    \
                          甲.计数 += 1   # 改类变量写在方法里".to_string())
                    };
                    return Err(self.err_at(E_UNEXPECTED, msg, node, Some(hint)));
                }
                "ExprStmt" => {
                    let is_tongguo = matches!(node.get("expr"),
                        Some(J::Node(e)) if e.kind == "Name"
                            && matches!(e.get("id"), Some(J::Str(x)) if x == "通过"));
                    if is_tongguo {
                        // 从 Python 借来的写法，特意点名
                        return Err(self.err_at(
                            E_UNEXPECTED,
                            "类体里不能写「通过」——它不是关键字，只是个普通名字",
                            node,
                            Some("空类用「单独一行冒号」占位：\n类 空类：\n    :\n\n\
                                  （基石没有 Python 的 `pass`；`通过` 在别的地方\
                                  也只是个名字，写了会报「找不到这个名字」。）"
                                 .to_string())));
                    }
                    return Err(self.err_at(
                        E_UNEXPECTED,
                        "类体里不能写「表达式语句」（比如调用函数）",
                        node,
                        Some("类体里只放声明，不放会执行的东西：\n类 甲：\n    \
                              计数 = 0\n    函数 打招呼(自身)：\n        \
                              打印(\"你好\")   # 想执行就写进方法里\n\n\
                              想在类上写说明文字，请用注释（# 开头）。".to_string())));
                }
                other => {
                    return Err(self.err_at(
                        E_UNEXPECTED,
                        format!("类体里只能写方法（函数）、类变量（名字 = 值）\
                                 或空类占位（:），不能写「{}」", other),
                        node, Some(CLASS_HINT.to_string())));
                }
            }
        }
        Ok(())
    }

    fn parse_classdef(&mut self) -> R<Node> {
        let start = self.advance();
        let name_tok = self.expect_name("类名")?;
        let mut base: Option<String> = None;
        if self.at_kw("继承") {
            self.advance();
            base = Some(match &self.expect_name("基类名")?.value {
                Value::Str(x) => x.clone(), _ => String::new() });
        }
        self.expect_colon("类")?;
        let mut body = self.parse_block()?;
        self.check_class_body(&body)?;

        // M23.4：方法体里的 `超()` → `超(自身, "定义类名")`
        let cls_name = match &name_tok.value {
            Value::Str(x) => x.clone(), _ => String::new(),
        };
        for item in body.iter_mut() {
            if let J::Node(n) = item {
                if n.kind != "FuncDef" {
                    continue;
                }
                let self_param: Option<String> = {
                    let names = match n.get("params") {
                        Some(J::List(v)) => v.clone(), _ => Vec::new() };
                    names.first().and_then(|x| match x {
                        J::Str(x) => Some(x.clone()), _ => None })
                };
                desugar_super(n, &cls_name, self_param.as_deref(), &self.filename)?;
            }
        }

        Ok(Node::new("ClassDef", start.line as i64, start.col as i64)
           .f("name", s(&cls_name))
           .f("base", match base { Some(x) => s(&x), None => J::Null })
           .f("body", J::List(body)))
    }

    fn parse_return(&mut self) -> R<Node> {
        let start = self.advance();
        let mut value = J::Null;
        if !self.at("NEWLINE") && self.cur().kind != "EOF" {
            value = self.parse_expr(0, true)?;
            // 多值返回：返回 a, b → 返回列表
            if self.at_op(",") {
                let mut elements = vec![value];
                while self.at_op(",") {
                    self.advance();
                    elements.push(self.parse_expr(0, true)?);
                }
                value = nj("List", start.line as i64, start.col as i64,
                           vec![("elements", J::List(elements))]);
            }
        }
        Ok(Node::new("Return", start.line as i64, start.col as i64)
           .f("value", value))
    }

    fn parse_import(&mut self) -> R<Node> {
        let start = self.advance();
        let name_tok = self.expect_name("模块名")?;
        let mut from_python = false;
        let mut from_local = false;
        let mut alias: Option<String> = None;
        if self.at_kw("从") {
            self.advance();
            let src = self.expect_name("来源")?;
            let sv = match &src.value { Value::Str(x) => x.clone(), _ => String::new() };
            if sv == "python" {
                from_python = true;
            } else if sv == "本地包" {
                from_local = true;
            } else {
                return Err(self.err_tok(
                    E_UNEXPECTED,
                    format!("只能「从 python」或「从 本地包」导入，看到了「{}」", sv),
                    None, &src, None));
            }
            if self.at_kw("为") {
                self.advance();
                alias = Some(match &self.expect_name("别名")?.value {
                    Value::Str(x) => x.clone(), _ => String::new() });
            }
        } else if self.at_kw("为") {
            // M18.4：普通标准库导入也支持别名
            self.advance();
            alias = Some(match &self.expect_name("别名")?.value {
                Value::Str(x) => x.clone(), _ => String::new() });
        }
        let nv = match &name_tok.value {
            Value::Str(x) => x.clone(), _ => String::new() };
        Ok(Node::new("Import", start.line as i64, start.col as i64)
           .f("name", s(&nv))
           .f("from_python", J::Bool(from_python))
           .f("from_local", J::Bool(from_local))
           .f("alias", match alias { Some(x) => s(&x), None => J::Null }))
    }

    fn parse_raise(&mut self) -> R<Node> {
        let start = self.advance();
        if self.at("NEWLINE") || self.at("DEDENT") || self.cur().kind == "EOF" {
            return Err(self.err_tok(
                E_UNEXPECTED,
                "「抛出」后面要写一个值（比如 抛出 值错误(\"说明\")）",
                None, &start, None));
        }
        let value = self.parse_expr(0, true)?;
        Ok(Node::new("Raise", start.line as i64, start.col as i64)
           .f("value", value))
    }

    /// `用 <表达式> 为 <名字>：` —— **解析期脱糖**，不新增 AST 节点
    fn parse_with(&mut self) -> R<Vec<J>> {
        let start = self.advance();          // 用
        let context = self.parse_expr(0, true)?;
        if !self.at_kw("为") {
            let tt = self.cur().clone();
            return Err(self.err_tok(
                E_UNEXPECTED, "「用」后面要用「为」接一个名字",
                Some("写法是：\n用 打开(\"数据.txt\") 为 f：\n    内容 = f.读()"
                     .to_string()), &tt, None));
        }
        self.advance();                      // 为
        let name_tok = self.expect_name("名字")?;
        self.expect_colon("用")?;
        let body = self.parse_block()?;

        self.with_seq += 1;
        let tmp = format!("__用_{}__", self.with_seq);
        let line = start.line as i64;
        let col = start.col as i64;
        let hold = nj("Assign", line, col, vec![
            ("target", nj("Name", line, col, vec![("id", s(&tmp))])),
            ("op", s("=")),
            ("value", context),
        ]);
        let nv = match &name_tok.value {
            Value::Str(x) => x.clone(), _ => String::new() };
        let bind = nj("Assign", line, col, vec![
            ("target", nj("Name", name_tok.line as i64, name_tok.col as i64,
                          vec![("id", s(&nv))])),
            ("op", s("=")),
            ("value", nj("Call", line, col, vec![
                ("func", nj("Name", line, col, vec![("id", s("进入上下文"))])),
                ("args", J::List(vec![nj("Name", line, col,
                                         vec![("id", s(&tmp))])])),
                ("keywords", J::List(vec![])),
            ])),
        ]);
        let cleanup = nj("Try", line, col, vec![
            ("body", J::List(body)),
            ("handlers", J::List(vec![])),
            ("finalbody", J::List(vec![nj("ExprStmt", line, col, vec![
                ("expr", nj("Call", line, col, vec![
                    ("func", nj("Name", line, col, vec![("id", s("退出上下文"))])),
                    ("args", J::List(vec![nj("Name", line, col,
                                             vec![("id", s(&tmp))])])),
                    ("keywords", J::List(vec![])),
                ])),
            ])])),
        ]);
        Ok(vec![hold, bind, cleanup])
    }

    /// `匹配 <表达式>：` —— **解析期脱糖**成 if/elif 链
    fn parse_match(&mut self) -> R<Vec<J>> {
        let start = self.advance();          // 匹配
        let subject = self.parse_expr(0, true)?;
        self.expect_colon("匹配")?;
        let cases = self.parse_case_block()?;
        if cases.is_empty() {
            return Err(self.err_tok(E_BLOCK,
                                    "「匹配」里至少要写一个「情形」",
                                    Some(MATCH_HINT.to_string()), &start, None));
        }
        // 裸兜底（`情形 其他：` 不带守卫）把剩下的情况全接住，所以必须最后
        let n = cases.len();
        for (i, c) in cases.iter().enumerate() {
            if c.wildcard && c.guard.is_none() && i != n - 1 {
                return Err(self.err_here(
                    E_UNEXPECTED,
                    "「情形 其他」要放在最后一条（它把剩下的情况全接住）",
                    Some(MATCH_HINT.to_string())));
            }
        }
        self.match_seq += 1;
        let tmp = format!("__匹配_{}__", self.match_seq);
        Ok(desugar_match(&subject, cases, &start, &tmp))
    }

    /// 解析「匹配」的代码块：里面只能是一串「情形」子块
    fn parse_case_block(&mut self) -> R<Vec<Case>> {
        if !self.at("NEWLINE") {
            return Err(self.err_here(E_BLOCK,
                                     "「匹配」后面必须换行，然后缩进写「情形」",
                                     Some(MATCH_HINT.to_string())));
        }
        self.advance();                      // NEWLINE
        if !self.at("INDENT") {
            return Err(self.err_here(E_BLOCK,
                                     "「匹配」的代码块里要写「情形」",
                                     Some(MATCH_HINT.to_string())));
        }
        self.advance();                      // INDENT
        let mut cases: Vec<Case> = Vec::new();
        while !self.at("DEDENT") && !self.at("EOF") {
            if self.at("NEWLINE") {
                self.advance();
                continue;
            }
            if !self.at_kw("情形") {
                let d = describe(&self.cur().clone());
                return Err(self.err_here(
                    E_UNEXPECTED,
                    format!("「匹配」的代码块里只能写「情形」，但看到了{}", d),
                    Some(MATCH_HINT.to_string())));
            }
            cases.push(self.parse_case()?);
        }
        if self.at("DEDENT") {
            self.advance();
        }
        Ok(cases)
    }

    /// 一条「情形」：模式（可多个，逗号分隔）+ 可选守卫「如果 条件」
    fn parse_case(&mut self) -> R<Case> {
        let start = self.advance();          // 情形
        let t = self.cur().clone();
        let mut wildcard = false;
        if self.at_kw("如果") {
            wildcard = true;
        } else if t.kind == "NAME" {
            let tv = match &t.value { Value::Str(x) => x.clone(), _ => String::new() };
            if tv == "其他" || tv == "_" {
                let nxt = self.peek(1).clone();
                let colon_next = nxt.kind == "OP" && tok_is_str(&nxt, ":");
                let if_next = nxt.kind == "KEYWORD" && tok_is_str(&nxt, "如果");
                if colon_next || if_next {
                    self.advance();
                    wildcard = true;
                }
            }
        }
        let mut patterns: Vec<J> = Vec::new();
        if !wildcard {
            patterns.push(self.parse_expr(0, false)?);
            while self.at_op(",") {
                self.advance();
                patterns.push(self.parse_expr(0, false)?);
            }
        }
        let guard = if self.at_kw("如果") {
            self.advance();
            Some(self.parse_expr(0, true)?)
        } else {
            None
        };
        self.expect_colon("情形")?;
        let body = self.parse_block()?;
        Ok(Case { wildcard, patterns, guard, body,
                  line: start.line as i64, col: start.col as i64 })
    }

    /// `枚举 <名字>：` —— **解析期脱糖**成「类 + 成员实例」
    fn parse_enum(&mut self) -> R<Vec<J>> {
        let start = self.advance();          // 枚举
        let name_tok = self.expect_name("枚举名")?;
        self.expect_colon("枚举")?;
        let members = self.parse_enum_members()?;
        if members.is_empty() {
            return Err(self.err_tok(E_BLOCK, "「枚举」里至少要写一个成员",
                                    Some(ENUM_HINT.to_string()), &start, None));
        }
        let cls_name = match &name_tok.value {
            Value::Str(x) => x.clone(), _ => String::new() };
        Ok(self.desugar_enum(&cls_name, members, &start, &name_tok))
    }

    fn parse_enum_members(&mut self) -> R<Vec<(Tok, J)>> {
        if !self.at("NEWLINE") {
            return Err(self.err_here(E_BLOCK,
                                     "「枚举」后面必须换行，然后缩进写成员",
                                     Some(ENUM_HINT.to_string())));
        }
        self.advance();                      // NEWLINE
        if !self.at("INDENT") {
            return Err(self.err_here(E_BLOCK,
                                     "「枚举」需要一个代码块：换行缩进，写「成员 = 值」",
                                     Some(ENUM_HINT.to_string())));
        }
        self.advance();                      // INDENT
        let mut members: Vec<(Tok, J)> = Vec::new();
        // 自动编号：None 表示没法自动编号（上一个的值不是整数）
        let mut next_auto: Option<J> = Some(J::Int(1));
        while !self.at("DEDENT") && !self.at("EOF") {
            if self.at("NEWLINE") {
                self.advance();
                continue;
            }
            let mtok = self.expect_name("成员名")?;
            if self.at_op("=") {
                self.advance();
                let value = self.parse_expr(0, true)?;
                // 值是整数字面量才允许后面的成员自动接着编号
                next_auto = int_like(&value).map(|v| match v {
                    J::Int(i) => J::Int(i + 1),
                    J::Big(b) => J::Big(big_inc(&b)),
                    _ => J::Int(1),
                });
                members.push((mtok, value));
            } else {
                match next_auto.clone() {
                    None => {
                        return Err(self.err_tok(
                            E_UNEXPECTED,
                            format!("成员「{}」要写值", tok_text(&mtok)),
                            Some("上一个成员的值不是整数，没法自动编号，所以这里要\
                                  写成「名字 = 值」".to_string()), &mtok, None));
                    }
                    Some(v) => {
                        let node = nj("Num", mtok.line as i64, mtok.col as i64,
                                      vec![("value", v.clone())]);
                        next_auto = match v {
                            J::Int(i) => Some(J::Int(i + 1)),
                            J::Big(b) => Some(J::Big(big_inc(&b))),
                            _ => None,
                        };
                        members.push((mtok, node));
                    }
                }
            }
            if self.at("NEWLINE") {
                self.advance();
            }
        }
        if self.at("DEDENT") {
            self.advance();
        }
        Ok(members)
    }

    fn desugar_enum(&mut self, cls_name: &str, members: Vec<(Tok, J)>,
                    start: &Tok, name_tok: &Tok) -> Vec<J> {
        let line = start.line as i64;
        let col = start.col as i64;
        let mline = name_tok.line as i64;
        let mcol = name_tok.col as i64;
        let self_name = nj("Name", mline, mcol, vec![("id", s("自身"))]);
        let field = |name: &str| -> J {
            nj("Attr", mline, mcol, vec![
                ("obj", self_name.clone()), ("attr", s(name))])
        };
        let init_fn = nj("FuncDef", mline, mcol, vec![
            ("name", s("初始化")),
            ("params", J::List(vec![s("自身"), s("名字"), s("值")])),
            ("body", J::List(vec![
                nj("Assign", mline, mcol, vec![
                    ("target", field("名字")), ("op", s("=")),
                    ("value", nj("Name", mline, mcol, vec![("id", s("名字"))]))]),
                nj("Assign", mline, mcol, vec![
                    ("target", field("值")), ("op", s("=")),
                    ("value", nj("Name", mline, mcol, vec![("id", s("值"))]))]),
            ])),
            ("defaults", J::List(vec![])),
            ("annotations", J::List(vec![])),
            ("param_kind", J::List(vec![])),
            ("returns", J::Null),
        ]);
        let repr_fn = nj("FuncDef", mline, mcol, vec![
            ("name", s("文本")),
            ("params", J::List(vec![s("自身")])),
            ("body", J::List(vec![nj("Return", mline, mcol, vec![
                ("value", nj("BinOp", mline, mcol, vec![
                    ("left", nj("Str", mline, mcol,
                                vec![("value", s(&format!("{}.", cls_name)))])),
                    ("op", s("+")),
                    ("right", field("名字"))]))])])),
            ("defaults", J::List(vec![])),
            ("annotations", J::List(vec![])),
            ("param_kind", J::List(vec![])),
            ("returns", J::Null),
        ]);
        let mut out: Vec<J> = vec![nj("ClassDef", line, col, vec![
            ("name", s(cls_name)), ("base", J::Null),
            ("body", J::List(vec![init_fn, repr_fn]))])];

        let mut refs: Vec<J> = Vec::new();
        for (mtok, value) in members {
            let kline = mtok.line as i64;
            let kcol = mtok.col as i64;
            let mv = match &mtok.value {
                Value::Str(x) => x.clone(), _ => String::new() };
            out.push(nj("Assign", kline, kcol, vec![
                ("target", nj("Attr", kline, kcol, vec![
                    ("obj", nj("Name", line, col, vec![("id", s(cls_name))])),
                    ("attr", s(&mv))])),
                ("op", s("=")),
                ("value", nj("Call", kline, kcol, vec![
                    ("func", nj("Name", line, col, vec![("id", s(cls_name))])),
                    ("args", J::List(vec![
                        nj("Str", kline, kcol, vec![("value", s(&mv))]), value])),
                    ("keywords", J::List(vec![])),
                ])),
            ]));
            refs.push(nj("Attr", kline, kcol, vec![
                ("obj", nj("Name", line, col, vec![("id", s(cls_name))])),
                ("attr", s(&mv))]));
        }
        out.push(nj("Assign", line, col, vec![
            ("target", nj("Attr", line, col, vec![
                ("obj", nj("Name", line, col, vec![("id", s(cls_name))])),
                ("attr", s("全部"))])),
            ("op", s("=")),
            ("value", nj("List", line, col, vec![("elements", J::List(refs))])),
        ]));
        out
    }

    // -- 块 ----------------------------------------------------------------

    fn parse_block(&mut self) -> R<Vec<J>> {
        if !self.at("NEWLINE") {
            return Err(self.err_here(
                E_BLOCK, "冒号后面必须换行，然后缩进写代码块",
                Some("例如：\n如果 真：\n    打印(1)".to_string())));
        }
        self.advance();                      // NEWLINE
        if self.at("INDENT") {
            self.advance();
            let stmts = self.parse_statements(&["DEDENT"])?;
            if self.at("DEDENT") {
                self.advance();
            }
            return Ok(stmts);
        }
        // 无缩进块
        if self.at_op(":") {
            self.advance();
            let t = self.cur().clone();
            return Ok(vec![Node::new("Pass", t.line as i64, t.col as i64).boxed()]);
        }
        Err(self.err_here(
            E_BLOCK, "这个语句需要一个代码块：换行后缩进，或写一个冒号表示空块",
            Some("例如：\n如果 真：\n    打印(1)".to_string())))
    }

    fn parse_try(&mut self) -> R<Node> {
        let start = self.advance();          // 尝试
        self.expect_colon("尝试")?;
        let body = self.parse_block()?;

        let mut handlers: Vec<J> = Vec::new();
        let mut finalbody: Option<Vec<J>> = None;
        while self.at_kw("捕获") {
            let h_tok = self.advance();      // 捕获
            let mut htype = J::Null;
            let mut hname: Option<String> = None;
            if self.cur().kind == "NAME" {
                htype = self.parse_expr(0, true)?;
                if self.at_kw("为") {
                    self.advance();
                    hname = Some(match &self.expect_name("异常变量的名字")?.value {
                        Value::Str(x) => x.clone(), _ => String::new() });
                }
            } else if self.at_kw("为") {
                self.advance();
                hname = Some(match &self.expect_name("异常变量的名字")?.value {
                    Value::Str(x) => x.clone(), _ => String::new() });
            }
            self.expect_colon("捕获")?;
            let hbody = self.parse_block()?;
            handlers.push(nj("ExceptHandler", h_tok.line as i64,
                             h_tok.col as i64, vec![
                                 ("type", htype), ("name", match hname {
                                     Some(x) => s(&x), None => J::Null }),
                                 ("body", J::List(hbody))]));
        }

        if self.at_kw("最终") {
            let _f_tok = self.advance();
            self.expect_colon("最终")?;
            finalbody = Some(self.parse_block()?);
        }

        if handlers.is_empty() && finalbody.is_none() {
            return Err(self.err_tok(
                E_UNEXPECTED, "「尝试」后面至少要有一个「捕获」或一个「最终」",
                Some("写法是：\n尝试：\n    …\n捕获 异常 为 e：\n    …".to_string()),
                &start, None));
        }
        Ok(Node::new("Try", start.line as i64, start.col as i64)
           .f("body", J::List(body))
           .f("handlers", J::List(handlers))
           .f("finalbody", match finalbody {
               Some(v) => J::List(v), None => J::Null }))
    }

    /// 把 token 识别成二元运算符名；不是运算符返回 None
    fn binop_of(&self, t: &Tok) -> Option<&'static str> {
        if t.kind == "OP" {
            if let Value::Str(v) = &t.value {
                if bin_prec(v).is_some() && !matches!(v.as_str(), "与" | "或") {
                    return Some(match v.as_str() {
                        "==" => "==", "!=" => "!=", "<" => "<", ">" => ">",
                        "<=" => "<=", ">=" => ">=", "+" => "+", "-" => "-",
                        "*" => "*", "/" => "/", "//" => "//", "%" => "%",
                        "**" => "**", _ => return None,
                    });
                }
            }
        }
        if t.kind == "KEYWORD" {
            if let Value::Str(v) = &t.value {
                return Some(match v.as_str() {
                    "在" => "在", "不在" => "不在", "是" => "是", "不是" => "不是",
                    _ => return None,
                });
            }
        }
        None
    }

    // -- 表达式：优先级爬升 --------------------------------------------------

    fn parse_expr(&mut self, min_prec: i64, allow_ternary: bool) -> R<J> {
        // 「非」是**前缀**运算符，但优先级比比较运算松、比「与/或」紧
        let t0 = self.cur().clone();
        let mut left: J = if t0.kind == "KEYWORD" && tok_is_str(&t0, "非") {
            self.advance();
            let operand = self.parse_expr(NOT_PREC, true)?;
            nj("UnaryOp", t0.line as i64, t0.col as i64, vec![
                ("op", s("非")), ("operand", operand)])
        } else {
            self.parse_unary()?
        };
        loop {
            let t = self.cur().clone();
            if t.kind == "KEYWORD" {
                if let Value::Str(v) = &t.value {
                    if v == "与" || v == "或" {
                        let op = v.clone();
                        let prec = bin_prec(&op).unwrap();
                        if prec < min_prec {
                            break;
                        }
                        self.advance();
                        // 合并同优先级链：a 与 b 与 c
                        let right = self.parse_expr(prec + 1, true)?;
                        let merging = matches!(&left, J::Node(n)
                            if n.kind == "BoolOp"
                               && matches!(n.get("op"), Some(J::Str(o)) if *o == op));
                        if merging {
                            if let J::Node(n) = &mut left {
                                if let Some(J::List(vals)) = n.get_mut("values") {
                                    vals.push(right);
                                }
                            }
                        } else {
                            left = nj("BoolOp", t.line as i64, t.col as i64, vec![
                                ("op", s(&op)),
                                ("values", J::List(vec![left, right]))]);
                        }
                        continue;
                    }
                }
            }
            let op: Option<&'static str> = self.binop_of(&t);
            let Some(op) = op else { break };
            let prec = bin_prec(op).unwrap();
            if prec < min_prec {
                break;
            }
            self.advance();
            if op == "**" {
                // 右结合
                let right = self.parse_expr(prec, true)?;
                left = nj("BinOp", t.line as i64, t.col as i64, vec![
                    ("left", left), ("op", s(op)), ("right", right)]);
            } else {
                let right = self.parse_expr(prec + 1, true)?;
                if is_cmp_op(op) {
                    // 链式比较：1 < x < 5
                    let mut ops = vec![s(op)];
                    let mut comparators = vec![right];
                    loop {
                        let nxt = self.cur().clone();
                        let Some(nxt_op) = self.binop_of(&nxt) else { break };
                        if !is_cmp_op(nxt_op) {
                            break;
                        }
                        let prec2 = bin_prec(nxt_op).unwrap();
                        if prec2 < min_prec {
                            break;
                        }
                        self.advance();
                        comparators.push(self.parse_expr(prec2 + 1, true)?);
                        ops.push(s(nxt_op));
                    }
                    left = nj("Compare", t.line as i64, t.col as i64, vec![
                        ("left", left), ("ops", J::List(ops)),
                        ("comparators", J::List(comparators))]);
                } else {
                    left = nj("BinOp", t.line as i64, t.col as i64, vec![
                        ("left", left), ("op", s(op)), ("right", right)]);
                }
            }
        }
        // M36 条件表达式 `甲 如果 条件 否则 乙`
        if allow_ternary && min_prec == 0 && self.at_kw("如果") {
            return self.desugar_ternary(left);
        }
        Ok(left)
    }

    /// `甲 如果 条件 否则 乙` —— 脱糖成「匿名函数立即调用」
    fn desugar_ternary(&mut self, then_expr: J) -> R<J> {
        let kw = self.advance();             // 消费「如果」
        let cond = self.parse_expr(0, true)?;
        if !self.at_kw("否则") {
            let tt = self.cur().clone();
            return Err(self.err_tok(
                E_UNEXPECTED, "条件表达式少了「否则」",
                Some("写法是「甲 如果 条件 否则 乙」——例如：\
                      令 等级 = \"成年\" 如果 年龄 >= 18 否则 \"未成年\"".to_string()),
                &tt, None));
        }
        self.advance();                      // 消费「否则」
        let other = self.parse_expr(0, true)?;   // 右结合：这里还会再嵌套
        let line = kw.line as i64;
        let col = kw.col as i64;
        let tl = match &then_expr {
            J::Node(n) => match n.get("line") { Some(J::Int(v)) => *v, _ => line },
            _ => line,
        };
        let tc = match &then_expr {
            J::Node(n) => match n.get("col") { Some(J::Int(v)) => *v, _ => col },
            _ => col,
        };
        let ol = match &other {
            J::Node(n) => match n.get("line") { Some(J::Int(v)) => *v, _ => line },
            _ => line,
        };
        let oc = match &other {
            J::Node(n) => match n.get("col") { Some(J::Int(v)) => *v, _ => col },
            _ => col,
        };
        let body = vec![nj("If", line, col, vec![
            ("branches", J::List(vec![
                J::List(vec![cond, J::List(vec![
                    nj("Return", tl, tc, vec![("value", then_expr)])])])])),
            ("orelse", J::List(vec![
                nj("Return", ol, oc, vec![("value", other)])])),
        ])];
        let lam = nj("Lambda", line, col, vec![
            ("params", J::List(vec![])),
            ("body", J::List(body)),
            ("defaults", J::List(vec![])),
            ("annotations", J::List(vec![])),
            ("param_kind", J::List(vec![])),
            ("name", s("匿名函数")),
        ]);
        Ok(nj("Call", line, col, vec![
            ("func", lam), ("args", J::List(vec![])),
            ("keywords", J::List(vec![]))]))
    }

    fn parse_unary(&mut self) -> R<J> {
        let t = self.cur().clone();
        if t.kind == "OP" && tok_is_str(&t, "-") {
            self.advance();
            let operand = self.parse_unary()?;
            return Ok(nj("UnaryOp", t.line as i64, t.col as i64,
                         vec![("op", s("-")), ("operand", operand)]));
        }
        if t.kind == "KEYWORD" && tok_is_str(&t, "非") {
            // 兜底（正常路径由 parse_expr 的前缀分支处理）
            self.advance();
            let operand = self.parse_expr(NOT_PREC, true)?;
            return Ok(nj("UnaryOp", t.line as i64, t.col as i64,
                         vec![("op", s("非")), ("operand", operand)]));
        }
        self.parse_postfix()
    }

    fn parse_postfix(&mut self) -> R<J> {
        let mut node = self.parse_atom()?;
        loop {
            let t = self.cur().clone();
            if t.kind == "OP" && tok_is_str(&t, "(") {
                self.advance();
                let mut args: Vec<J> = Vec::new();
                let mut keywords: Vec<J> = Vec::new();
                if !self.at_op(")") {
                    loop {
                        let is_kw = self.cur().kind == "NAME"
                            && self.peek(1).kind == "OP"
                            && tok_is_str(self.peek(1), "=");
                        if is_kw {
                            let kw_tok = self.advance();
                            self.advance();          // 消费 =
                            let v = self.parse_expr(0, true)?;
                            let kv = match &kw_tok.value {
                                Value::Str(x) => x.clone(), _ => String::new() };
                            keywords.push(J::List(vec![s(&kv), v]));
                        } else {
                            args.push(self.parse_expr(0, true)?);
                        }
                        if self.at_op(",") {
                            self.advance();
                            if self.at_op(")") {     // 尾随逗号
                                break;
                            }
                            continue;
                        }
                        break;
                    }
                }
                self.expect_op(")")?;
                node = nj("Call", t.line as i64, t.col as i64, vec![
                    ("func", node), ("args", J::List(args)),
                    ("keywords", J::List(keywords))]);
            } else if t.kind == "OP" && tok_is_str(&t, ".") {
                self.advance();
                let attr_tok = self.expect_attr_name()?;
                let av = match &attr_tok.value {
                    Value::Str(x) => x.clone(), _ => String::new() };
                node = nj("Attr", t.line as i64, t.col as i64,
                          vec![("obj", node), ("attr", s(&av))]);
            } else if t.kind == "OP" && tok_is_str(&t, "[") {
                self.advance();
                if self.looks_like_slice() {
                    node = self.parse_slice(node, &t)?;
                } else {
                    let index = self.parse_expr(0, true)?;
                    self.expect_op("]")?;
                    node = nj("Subscript", t.line as i64, t.col as i64,
                              vec![("obj", node), ("index", index)]);
                }
            } else {
                break;
            }
        }
        Ok(node)
    }

    /// 向前看：当前下标位置是否是一个切片（含冒号）
    fn looks_like_slice(&self) -> bool {
        let mut depth = 0i64;
        let mut i = 0usize;
        loop {
            let t = self.peek(i);
            if t.kind == "EOF" {
                return false;
            }
            if t.kind == "OP" {
                if let Value::Str(v) = &t.value {
                    match v.as_str() {
                        "[" | "(" | "{" => depth += 1,
                        "]" | ")" | "}" => {
                            if v == "]" && depth == 0 {
                                return false;
                            }
                            depth -= 1;
                        }
                        ":" if depth == 0 => return true,
                        _ => {}
                    }
                }
            }
            i += 1;
        }
    }

    fn parse_slice(&mut self, node: J, open_tok: &Tok) -> R<J> {
        let mut start = J::Null;
        let mut stop = J::Null;
        let mut step = J::Null;

        if !self.at_op(":") {
            start = self.parse_expr(0, true)?;
        }
        if self.at_op(":") {
            self.advance();
            if !self.at_op(":") && !self.at_op("]") {
                stop = self.parse_expr(0, true)?;
            }
            if self.at_op(":") {
                self.advance();
                if !self.at_op("]") {
                    step = self.parse_expr(0, true)?;
                }
            }
        }
        self.expect_op("]")?;
        let line = open_tok.line as i64;
        let col = open_tok.col as i64;
        Ok(nj("Subscript", line, col, vec![
            ("obj", node),
            ("index", nj("Slice", line, col, vec![
                ("start", start), ("stop", stop), ("step", step)])),
        ]))
    }

    /// 集合字面量 `{a, b, c}`（M24.1）—— 脱糖成 `集合([元素…])`
    fn parse_set_literal(&mut self, first: J, open_tok: &Tok) -> R<J> {
        let line = open_tok.line as i64;
        let col = open_tok.col as i64;
        let wrap = |inner: J| -> J {
            nj("Call", line, col, vec![
                ("func", nj("Name", line, col, vec![("id", s("集合"))])),
                ("args", J::List(vec![inner])),
                ("keywords", J::List(vec![]))])
        };
        if self.at_kw("遍历") {
            let comp = self.parse_comp_tail("list", first, J::Null, J::Null,
                                            line, col)?;
            self.expect_op("}")?;
            return Ok(wrap(comp));
        }
        let mut elements = vec![first];
        while self.at_op(",") {
            self.advance();
            if self.at_op("}") {             // 尾随逗号
                break;
            }
            elements.push(self.parse_expr(0, true)?);
        }
        self.expect_op("}")?;
        Ok(wrap(nj("List", line, col, vec![("elements", J::List(elements))])))
    }

    /// 匿名函数（M24.4）：`函数(x)：x * 2`
    fn parse_lambda(&mut self) -> R<J> {
        let start = self.advance();          // 消费「函数」
        let p = self.parse_params()?;
        self.expect_op(":")?;
        if self.at("NEWLINE") || self.at("EOF") || self.at("DEDENT") {
            return Err(self.err_tok(
                E_UNEXPECTED, "匿名函数要写成一行：「函数(参数)：表达式」",
                Some("需要多行代码块请用「函数 名字(参数)：」定义具名函数"
                     .to_string()), &start, None));
        }
        let body_expr = self.parse_expr(0, true)?;
        let bl = match &body_expr {
            J::Node(n) => match n.get("line") { Some(J::Int(v)) => *v,
                                                _ => start.line as i64 },
            _ => start.line as i64,
        };
        let bc = match &body_expr {
            J::Node(n) => match n.get("col") { Some(J::Int(v)) => *v,
                                               _ => start.col as i64 },
            _ => start.col as i64,
        };
        Ok(nj("Lambda", start.line as i64, start.col as i64, vec![
            ("params", J::List(p.names.iter().map(|x| s(x)).collect())),
            ("body", J::List(vec![nj("Return", bl, bc,
                                     vec![("value", body_expr)])])),
            ("defaults", J::List(p.defaults)),
            ("annotations", J::List(p.anns.iter().map(|a| match a {
                Some(x) => s(x), None => J::Null }).collect())),
            ("param_kind", J::List(p.kinds.iter().map(|k| J::Int(*k)).collect())),
            ("name", s("匿名函数")),
        ]))
    }

    fn parse_atom(&mut self) -> R<J> {
        let t = self.cur().clone();
        if t.kind == "NUMBER" {
            self.advance();
            return Ok(nj("Num", t.line as i64, t.col as i64,
                         vec![("value", tok_val_j(&t))]));
        }
        if t.kind == "STRING" {
            self.advance();
            return Ok(nj("Str", t.line as i64, t.col as i64,
                         vec![("value", tok_val_j(&t))]));
        }
        if t.kind == "FSTRING" {
            self.advance();
            return self.parse_fstring(&t);
        }
        if t.kind == "KEYWORD" {
            let v = match &t.value { Value::Str(x) => x.clone(), _ => String::new() };
            match v.as_str() {
                "真" => {
                    self.advance();
                    return Ok(nj("Num", t.line as i64, t.col as i64,
                                 vec![("value", J::Bool(true))]));
                }
                "假" => {
                    self.advance();
                    return Ok(nj("Num", t.line as i64, t.col as i64,
                                 vec![("value", J::Bool(false))]));
                }
                "空" => {
                    self.advance();
                    return Ok(nj("Name", t.line as i64, t.col as i64,
                                 vec![("id", s("空"))]));
                }
                "新建" => {
                    // 新建 类(...) 是语法糖：等价于 类(...)
                    self.advance();
                    let name_tok = self.expect_name("类名")?;
                    let nv = match &name_tok.value {
                        Value::Str(x) => x.clone(), _ => String::new() };
                    return Ok(nj("Name", t.line as i64, t.col as i64,
                                 vec![("id", s(&nv))]));
                }
                "函数" => return self.parse_lambda(),
                other => {
                    return Err(self.err_tok(
                        E_UNEXPECTED,
                        format!("表达式里不能直接用关键字「{}」", other),
                        None, &t, None));
                }
            }
        }
        if t.kind == "NAME" {
            self.advance();
            self.check_en_word(&t, "atom")?;
            let nv = match &t.value { Value::Str(x) => x.clone(), _ => String::new() };
            return Ok(nj("Name", t.line as i64, t.col as i64, vec![("id", s(&nv))]));
        }
        if t.kind == "OP" && tok_is_str(&t, "[") {
            self.advance();
            if self.at_op("]") {
                self.advance();
                return Ok(nj("List", t.line as i64, t.col as i64,
                             vec![("elements", J::List(vec![]))]));
            }
            let first = self.parse_expr(0, true)?;
            if self.at_kw("遍历") {
                let comp = self.parse_comp_tail("list", first, J::Null, J::Null,
                                                t.line as i64, t.col as i64)?;
                self.expect_op("]")?;
                return Ok(comp);
            }
            let mut elements = vec![first];
            while self.at_op(",") {
                self.advance();
                if self.at_op("]") {         // 尾随逗号
                    break;
                }
                elements.push(self.parse_expr(0, true)?);
            }
            self.expect_op("]")?;
            return Ok(nj("List", t.line as i64, t.col as i64,
                         vec![("elements", J::List(elements))]));
        }
        if t.kind == "OP" && tok_is_str(&t, "{") {
            self.advance();
            if self.at_op("}") {
                self.advance();
                return Ok(nj("Dict", t.line as i64, t.col as i64,
                             vec![("keys", J::List(vec![])),
                                  ("values", J::List(vec![]))]));
            }
            let k = self.parse_expr(0, true)?;
            if !self.at_op(":") {
                // 集合字面量（M24.1）
                return self.parse_set_literal(k, &t);
            }
            self.expect_op(":")?;
            let v = self.parse_expr(0, true)?;
            if self.at_kw("遍历") {
                let comp = self.parse_comp_tail("dict", J::Null, k, v,
                                                t.line as i64, t.col as i64)?;
                self.expect_op("}")?;
                return Ok(comp);
            }
            let mut keys = vec![k];
            let mut values = vec![v];
            while self.at_op(",") {
                self.advance();
                if self.at_op("}") {         // 尾随逗号
                    break;
                }
                let kk = self.parse_expr(0, true)?;
                self.expect_op(":")?;
                let vv = self.parse_expr(0, true)?;
                keys.push(kk);
                values.push(vv);
            }
            self.expect_op("}")?;
            return Ok(nj("Dict", t.line as i64, t.col as i64,
                         vec![("keys", J::List(keys)),
                              ("values", J::List(values))]));
        }
        if t.kind == "OP" && tok_is_str(&t, "(") {
            self.advance();
            let inner = self.parse_expr(0, true)?;
            self.expect_op(")")?;
            return Ok(inner);
        }
        Err(self.err_tok(E_UNEXPECTED,
                         format!("这里需要一个值，但看到了{}", describe(&t)),
                         None, &t, None))
    }
}

/// `匹配` 的脱糖：`__匹配_N__ = <待匹配值>` + if/elif 链
fn desugar_match(subject: &J, cases: Vec<Case>, start: &Tok, tmp: &str) -> Vec<J> {
    let line = start.line as i64;
    let col = start.col as i64;
    let bind = nj("Assign", line, col, vec![
        ("target", nj("Name", line, col, vec![("id", s(tmp))])),
        ("op", s("=")),
        ("value", subject.clone()),
    ]);

    let mut branches: Vec<J> = Vec::new();
    let mut orelse: Option<Vec<J>> = None;
    for c in cases {
        let cline = c.line;
        let ccol = c.col;
        if c.wildcard {
            match c.guard {
                // 裸兜底：把剩下的情况全接住（「必须最后」已在 parse_match 里查过）
                None => orelse = Some(c.body),
                Some(g) => branches.push(J::List(vec![g, J::List(c.body)])),
            }
            continue;
        }
        let mut tests: Vec<J> = Vec::new();
        for p in c.patterns {
            tests.push(nj("Compare", cline, ccol, vec![
                ("left", nj("Name", cline, ccol, vec![("id", s(tmp))])),
                ("ops", J::List(vec![s("==")])),
                ("comparators", J::List(vec![p])),
            ]));
        }
        let mut test = if tests.len() == 1 {
            tests.remove(0)
        } else {
            nj("BoolOp", cline, ccol,
               vec![("op", s("或")), ("values", J::List(tests))])
        };
        if let Some(g) = c.guard {
            test = nj("BoolOp", cline, ccol, vec![
                ("op", s("与")), ("values", J::List(vec![test, g]))]);
        }
        branches.push(J::List(vec![test, J::List(c.body)]));
    }

    let mut out: Vec<J> = vec![bind];
    if branches.is_empty() {
        // 只有一条无守卫的「情形 其他」——块体无条件执行，摊平即可
        out.extend(orelse.unwrap_or_default());
        return out;
    }
    out.push(nj("If", line, col, vec![
        ("branches", J::List(branches)),
        ("orelse", match orelse { Some(v) => J::List(v), None => J::Null }),
    ]));
    out
}

struct Case {
    wildcard: bool,
    patterns: Vec<J>,
    guard: Option<J>,
    body: Vec<J>,
    line: i64,
    col: i64,
}

/// `J::Node` → `Node`（解析器内部产物恒为节点，取不到就是实现有 bug）
fn node_from_j(v: J) -> R<Node> {
    match v {
        J::Node(n) => Ok(*n),
        _ => Err(ParseErr::new(E_UNEXPECTED, "内部错误：期望一个 AST 节点", 1, 1)),
    }
}

/// `A.Num` 且值是整数（不含 bool）时返回它，否则 None
fn int_like(v: &J) -> Option<J> {
    match v {
        J::Node(n) if n.kind == "Num" => match n.get("value") {
            Some(J::Int(i)) => Some(J::Int(*i)),
            Some(J::Big(b)) => Some(J::Big(b.clone())),
            _ => None,
        },
        _ => None,
    }
}

/// 对应 `Parser._describe`
fn describe(t: &Tok) -> String {
    // ⚠️ FSTRING **不走特判**：`tok_text` 已经把它说成「插值字符串」，
    // 走 `_` 那条兜底分支就与 Python 的 `_describe` 逐字相同
    // （Python 那边也是 `f"「{_tok_text(t)}」"` 兜底）。一开始给它单开了一条
    // 分支、忘了补那对「」，两边就差两个字符 —— 正是 R3 要抓的东西。
    match t.kind {
        "KEYWORD" => format!("关键字「{}」", tok_text(t)),
        "NAME" => format!("名字「{}」", tok_text(t)),
        "OP" => format!("符号「{}」", tok_text(t)),
        _ => format!("「{}」", tok_text(t)),
    }
}

fn lex_to_parse(e: tokenizer::LexErr) -> ParseErr {
    ParseErr {
        code: e.code, message: e.message, line: e.line, col: e.col,
        hint: e.hint, underline: e.underline, fix: None,
    }
}

// ---------------------------------------------------------------------------
// `超()` 脱糖与收尾检查（M23.4）
// ---------------------------------------------------------------------------

/// 把类方法体里的 `超()` 就地改写为 `超(自身, "定义类名")`。
fn desugar_super(root: &mut Node, defining: &str, self_param: Option<&str>,
                 filename: &str) -> R<()> {
    walk_mut(root, &mut |node| {
        if node.kind == "ClassDef" {
            return Ok(false);            // 嵌套类由它自己处理，跳过
        }
        if node.kind == "Call" {
            let is_super = matches!(node.get("func"),
                Some(J::Node(f)) if f.kind == "Name"
                    && matches!(f.get("id"), Some(J::Str(x)) if x == SUPER_NAME));
            let no_args = matches!(node.get("args"),
                Some(J::List(v)) if v.is_empty());
            let no_kw = matches!(node.get("keywords"),
                Some(J::List(v)) if v.is_empty());
            if is_super && no_args && no_kw {
                let (fl, fc) = match node.get("func") {
                    Some(J::Node(f)) => (
                        match f.get("line") { Some(J::Int(v)) => *v, _ => 1 },
                        match f.get("col") { Some(J::Int(v)) => *v, _ => 1 }),
                    _ => (1, 1),
                };
                let Some(sp) = self_param else {
                    return Err(ParseErr {
                        code: E_UNEXPECTED,
                        message: format!(
                            "「{}()」要写在方法里（方法的第一个参数是「自身」），\
                             现在这个方法没有参数，取不到实例", SUPER_NAME),
                        line: fl as usize, col: fc as usize,
                        hint: None, underline: Some((fc as usize, fc as usize)),
                        fix: None,
                    });
                };
                if let Some(J::List(args)) = node.get_mut("args") {
                    args.push(nj("Name", fl, fc, vec![("id", s(sp))]));
                    args.push(nj("Str", fl, fc, vec![("value", s(defining))]));
                }
                if let Some(J::Node(f)) = node.get_mut("func") {
                    f.super_ok = true;
                }
                let _ = filename;
            }
        }
        Ok(true)
    })
}

/// 深度优先遍历并允许就地改写；返回 `false` 表示不再往下走。
fn walk_mut<F>(node: &mut Node, f: &mut F) -> R<()>
where
    F: FnMut(&mut Node) -> R<bool>,
{
    if !f(node)? {
        return Ok(());
    }
    for (_, v) in node.fields.iter_mut() {
        walk_j(v, f)?;
    }
    Ok(())
}

fn walk_j<F>(v: &mut J, f: &mut F) -> R<()>
where
    F: FnMut(&mut Node) -> R<bool>,
{
    match v {
        J::Node(n) => walk_mut(n, f),
        J::List(items) => {
            for it in items.iter_mut() {
                walk_j(it, f)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

/// 检查有没有漏在类方法外的 `超`
fn check_no_stray_super(program: &Node) -> R<()> {
    fn on_node(n: &Node) -> R<()> {
        if n.kind == "Name" && !n.super_ok {
            if let Some(J::Str(x)) = n.get("id") {
                if x == SUPER_NAME {
                    let line = match n.get("line") {
                        Some(J::Int(v)) => *v as usize, _ => 1 };
                    let col = match n.get("col") {
                        Some(J::Int(v)) => *v as usize, _ => 1 };
                    return Err(ParseErr {
                        code: E_UNEXPECTED,
                        message: format!(
                            "「{}()」只能在类的方法里用（用它调基类的同名方法）",
                            SUPER_NAME),
                        line, col,
                        hint: Some(format!(
                            "写法：在方法里写 {}().方法名(参数)——例如 {}().初始化(名字)",
                            SUPER_NAME, SUPER_NAME)),
                        underline: Some((col, col)),
                        fix: None,
                    });
                }
            }
        }
        Ok(())
    }
    fn go_j(v: &J) -> R<()> {
        match v {
            J::Node(n) => {
                on_node(n)?;
                for (_, f) in n.fields.iter() {
                    go_j(f)?;
                }
                Ok(())
            }
            J::List(items) => {
                for it in items.iter() {
                    go_j(it)?;
                }
                Ok(())
            }
            _ => Ok(()),
        }
    }
    on_node(program)?;
    for (_, f) in program.fields.iter() {
        go_j(f)?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// 对外入口
// ---------------------------------------------------------------------------

/// `lines` 是调用方给的源码行（与 `parser.parse(tokens, source_lines, …)`
/// 的第二参一致）：**用它反拼出源码**再分词，词法与 Python 侧完全同源。
/// 这么设计是因为 Python 侧的 `parse()` 只拿得到 tokens 与 lines，拿不到原文。
/// 语法分析：吃**源码文本**，自己分词（R 线 · R3）。
///
/// ⚠️ **为什么参数是文本而不是 tokens**（这一点是想清楚才定的）：
/// Python 侧 `parse(tokens, source_lines, filename)` 里那份 `tokens` 是**冗余**的
/// —— AST 只由源文本决定。而 `source_lines` 是个**有损**的载体：不同调用方的
/// 切分口径不一样（`split_lines` 会去掉末尾空行），反拼出来的文本会比原文短，
/// 于是 **EOF 行号偏移、报错行号少一行**（R3 实测：9 个样例里错 4 个）。
/// 与其在两侧维持两条「切分口径」的隐性约定，不如让扩展**直接吃原文**：
/// 分词与语法用的就是同一份文本，不可能错位。
pub fn parse(text: &str, filename: &str) -> R<Node> {
    let out = tokenizer::tokenize(text, filename).map_err(lex_to_parse)?;
    let mut p = Parser::new(out.tokens, filename);
    let program = p.parse()?;
    check_no_stray_super(&program)?;
    Ok(program)
}

/// 把一段文本解析成**单个表达式**（对应 `parser.parse_expression`，M37）
pub fn parse_expression(text: &str, filename: &str) -> R<Node> {
    let out = tokenizer::tokenize(text, filename).map_err(lex_to_parse)?;
    let mut p = Parser::new(out.tokens, filename);
    let node = p.parse_expr(0, true)?;
    if p.at("NEWLINE") {
        p.advance();
    }
    if p.cur().kind != "EOF" {
        return Err(p.err_here(E_UNEXPECTED, "「查看」后面只能写一个表达式",
                              Some("例如：查看 单价 * 数量 —— 想看多句话请改脚本后\
                                    重新跑一次".to_string())));
    }
    match node {
        J::Node(n) => Ok(*n),
        _ => Err(ParseErr::new(E_UNEXPECTED, "不是一个表达式", 1, 1)),
    }
}
