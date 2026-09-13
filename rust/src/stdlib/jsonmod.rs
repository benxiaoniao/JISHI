//! 标准库 `json`（Python 侧 `jishi/stdlib/json.py`，4 个函数）。
//!
//! **不复用 `crate::json`**：那个解析器是给字节码用的，为了查字段快用了
//! `BTreeMap`（会**把键排序**），而且不支持科学计数法。`json` 模块要的是
//! Python `json` 模块的语义：
//!
//! - 键**保持插入顺序**，重复键保留**首次出现的位置** + **最后一次的值**
//!   （Python 的 `{"a":1,"b":2,"a":3}` 是 `{"a":3,"b":2}`）；
//! - 数字：无小数点/指数的当整数，否则当小数（`1e3` 是 `1000.0`）；
//! - `转文本` 默认 `ensure_ascii=False`（中文**不**转义），
//!   无缩进时空格分隔符是 `", "` / `": "`（Python 的默认值就是这样）。
//!
//! 序列化的浮点用 Python 的 `repr` 规则：整数形态保留 `.0`（`2.0`），
//! 指数在 `[-4, 16)` 之外才用科学计数法（`1e+20`）。

use std::collections::HashMap;

use super::{as_text, need, need_range, table, R};
use crate::{err, JishiError, Val, VM};

// ---------------------------------------------------------------------------
// 序列化
// ---------------------------------------------------------------------------

/// JSON 字符串字面量（含引号）。与 Python `json.dumps(ensure_ascii=False)`
/// 一致：只转义 `"` `\` 与小于 0x20 的控制字符，`/` 不动。
fn quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
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

/// Python 的 `repr(float)`（`json.dumps` 就是用它写数字的）。
pub(crate) fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "NaN".to_string();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity" } else { "-Infinity" }.to_string();
    }
    let a = x.abs();
    if a == 0.0 {
        return if x.is_sign_negative() { "-0.0" } else { "0.0" }.to_string();
    }
    let exp10 = a.log10().floor() as i32;
    if (-4..16).contains(&exp10) {
        if x == x.trunc() {
            // 整数形态的小数要留 `.0`（Python 的 `2.0`）
            format!("{:.1}", x)
        } else {
            format!("{}", x) // Rust 的最短往返表示，与 Python 同源
        }
    } else {
        // 科学计数法：Python 是 `1e+20` / `1e-05`（指数带符号、至少两位）
        let s = format!("{:e}", x); // 形如 `1e20` / `1.5e-7`
        let (mant, e) = s.split_once('e').unwrap_or((s.as_str(), "0"));
        let ev: i32 = e.parse().unwrap_or(0);
        format!("{}e{}{:02}", mant, if ev < 0 { "-" } else { "+" }, ev.abs())
    }
}

/// 键转成 JSON 的键（Python 允许 str/int/float/bool/None）。
fn key_text(k: &Val) -> String {
    match k {
        Val::Str(s) => s.clone(),
        Val::Int(i) => i.to_string(),
        Val::Float(f) => float_repr(*f),
        Val::Bool(b) => if *b { "true" } else { "false" }.to_string(),
        Val::None_ => "null".to_string(),
        other => as_text(other),
    }
}

fn encode(v: &Val, indent: Option<usize>, depth: usize, out: &mut String) {
    match v {
        Val::None_ => out.push_str("null"),
        Val::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Val::Int(i) => out.push_str(&i.to_string()),
        Val::Float(f) => out.push_str(&float_repr(*f)),
        Val::Str(s) => out.push_str(&quote(s)),
        // 精确小数的 `str()` 是 `1.5`，Python 的 `default=str` 会把它当**字符串**
        Val::Dec(d) => out.push_str(&quote(&d.to_string())),
        Val::Date(d) => out.push_str(&quote(&d.to_display())),
        Val::List(items) => {
            let items = items.borrow();
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            let inner = depth + 1;
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                match indent {
                    // 有缩进：每个元素前都换行缩进（含第一个）
                    Some(n) => {
                        out.push('\n');
                        out.push_str(&" ".repeat(n * inner));
                    }
                    // 无缩进：Python 的分隔符是 `", "`（第一个元素前没有空格）
                    None => {
                        if i > 0 {
                            out.push(' ');
                        }
                    }
                }
                encode(item, indent, inner, out);
            }
            if let Some(n) = indent {
                out.push('\n');
                out.push_str(&" ".repeat(n * depth));
            }
            out.push(']');
        }
        Val::Dict(pairs) => {
            let pairs = pairs.borrow();
            if pairs.is_empty() {
                out.push_str("{}");
                return;
            }
            let inner = depth + 1;
            out.push('{');
            for (i, (k, val)) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                match indent {
                    Some(n) => {
                        out.push('\n');
                        out.push_str(&" ".repeat(n * inner));
                    }
                    None => {
                        if i > 0 {
                            out.push(' ');
                        }
                    }
                }
                out.push_str(&quote(&key_text(k)));
                out.push_str(": ");
                encode(val, indent, inner, out);
            }
            if let Some(n) = indent {
                out.push('\n');
                out.push_str(&" ".repeat(n * depth));
            }
            out.push('}');
        }
        // 集合 / 表格 / 函数… Python 走 `default=str`，于是都变成字符串
        other => out.push_str(&quote(&as_text(other))),
    }
}

// ---------------------------------------------------------------------------
// 解析
// ---------------------------------------------------------------------------

struct P<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> P<'a> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn error<T>(&self, msg: &str) -> Result<T, JishiError> {
        Err(err("值错误", format!("不是有效的 JSON 文本：{msg}（第 {} 个字符附近）", self.i + 1)))
    }

    fn value(&mut self) -> Result<Val, JishiError> {
        self.ws();
        if self.i >= self.b.len() {
            return self.error("内容不完整");
        }
        match self.b[self.i] {
            b'{' => self.object(),
            b'[' => self.array(),
            b'"' => Ok(Val::Str(self.string()?)),
            b't' => self.literal("true", Val::Bool(true)),
            b'f' => self.literal("false", Val::Bool(false)),
            b'n' => self.literal("null", Val::None_),
            b'N' => self.literal("NaN", Val::Float(f64::NAN)),
            b'I' => self.literal("Infinity", Val::Float(f64::INFINITY)),
            b'-' if self.b[self.i..].starts_with(b"-Infinity") => {
                self.i += 9;
                Ok(Val::Float(f64::NEG_INFINITY))
            }
            _ => self.number(),
        }
    }

    fn literal(&mut self, word: &str, v: Val) -> Result<Val, JishiError> {
        if self.b[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            Ok(v)
        } else {
            self.error("认不出这个字面量")
        }
    }

    fn number(&mut self) -> Result<Val, JishiError> {
        let start = self.i;
        if self.i < self.b.len() && self.b[self.i] == b'-' {
            self.i += 1;
        }
        let mut is_float = false;
        while self.i < self.b.len() {
            match self.b[self.i] {
                b'0'..=b'9' => self.i += 1,
                b'.' => {
                    is_float = true;
                    self.i += 1;
                }
                b'e' | b'E' => {
                    is_float = true;
                    self.i += 1;
                    if self.i < self.b.len() && matches!(self.b[self.i], b'+' | b'-') {
                        self.i += 1;
                    }
                }
                _ => break,
            }
        }
        if start == self.i {
            return self.error("这里应该是一个值");
        }
        let text = std::str::from_utf8(&self.b[start..self.i]).unwrap_or("");
        if is_float {
            text.parse::<f64>()
                .map(Val::Float)
                .map_err(|_| err("值错误", format!("不是有效的 JSON 文本：数字「{text}」读不出来")))
        } else {
            match text.parse::<i128>() {
                Ok(i) => Ok(Val::Int(i)),
                // 超出 i128 就退化成小数（Python 是任意精度整数，这里如实报错）
                Err(_) => Err(err(
                    "值错误",
                    format!("整数「{text}」超出 Rust 宿主能表示的范围（约 1.7e38）"),
                )),
            }
        }
    }

    fn string(&mut self) -> Result<String, JishiError> {
        self.i += 1; // 跳过开头引号
        let mut out = String::new();
        while self.i < self.b.len() {
            let c = self.b[self.i];
            match c {
                b'"' => {
                    self.i += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.i += 1;
                    if self.i >= self.b.len() {
                        break;
                    }
                    let e = self.b[self.i];
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            // 代理对：把两个 \uXXXX 合成一个码点
                            if (0xD800..0xDC00).contains(&hi)
                                && self.b[self.i..].starts_with(b"\\u")
                            {
                                self.i += 2;
                                let lo = self.hex4()?;
                                let cp = 0x10000
                                    + ((hi - 0xD800) << 10)
                                    + (lo.wrapping_sub(0xDC00));
                                out.push(char::from_u32(cp).unwrap_or('\u{FFFD}'));
                            } else {
                                out.push(char::from_u32(hi).unwrap_or('\u{FFFD}'));
                            }
                        }
                        _ => return self.error("认不出这个转义"),
                    }
                }
                _ => {
                    // 原样按 UTF-8 走：找出这个字符的字节长度
                    let len = utf8_len(c);
                    if self.i + len > self.b.len() {
                        break;
                    }
                    match std::str::from_utf8(&self.b[self.i..self.i + len]) {
                        Ok(s) => out.push_str(s),
                        Err(_) => out.push('\u{FFFD}'),
                    }
                    self.i += len;
                }
            }
        }
        self.error("字符串没有收尾的引号")
    }

    fn hex4(&mut self) -> Result<u32, JishiError> {
        if self.i + 4 > self.b.len() {
            return self.error("转义不完整");
        }
        let s = std::str::from_utf8(&self.b[self.i..self.i + 4]).unwrap_or("");
        let v = u32::from_str_radix(s, 16)
            .map_err(|_| err("值错误", "不是有效的 JSON 文本：\\u 后面要是四位十六进制"))?;
        self.i += 4;
        Ok(v)
    }

    fn array(&mut self) -> Result<Val, JishiError> {
        self.i += 1;
        let mut items = Vec::new();
        self.ws();
        if self.i < self.b.len() && self.b[self.i] == b']' {
            self.i += 1;
            return Ok(crate::list_new(items));
        }
        loop {
            items.push(self.value()?);
            self.ws();
            if self.i >= self.b.len() {
                return self.error("数组没有收尾的 ]");
            }
            match self.b[self.i] {
                b',' => self.i += 1,
                b']' => {
                    self.i += 1;
                    return Ok(crate::list_new(items));
                }
                _ => return self.error("数组里应该是 , 或 ]"),
            }
        }
    }

    fn object(&mut self) -> Result<Val, JishiError> {
        self.i += 1;
        let mut pairs: Vec<(Val, Val)> = Vec::new();
        self.ws();
        if self.i < self.b.len() && self.b[self.i] == b'}' {
            self.i += 1;
            return Ok(crate::dict_new(pairs));
        }
        loop {
            self.ws();
            if self.i >= self.b.len() || self.b[self.i] != b'"' {
                return self.error("对象的键要用双引号包起来");
            }
            let k = self.string()?;
            self.ws();
            if self.i >= self.b.len() || self.b[self.i] != b':' {
                return self.error("键后面要跟一个冒号");
            }
            self.i += 1;
            let v = self.value()?;
            // 重复键：保留首次出现的位置、用最后一次的值（Python 的 dict 行为）
            if let Some(slot) = pairs.iter_mut().find(|(pk, _)| {
                matches!(pk, Val::Str(s) if *s == k)
            }) {
                slot.1 = v;
            } else {
                pairs.push((Val::Str(k), v));
            }
            self.ws();
            if self.i >= self.b.len() {
                return self.error("对象没有收尾的 }");
            }
            match self.b[self.i] {
                b',' => self.i += 1,
                b'}' => {
                    self.i += 1;
                    return Ok(crate::dict_new(pairs));
                }
                _ => return self.error("对象里应该是 , 或 }"),
            }
        }
    }
}

fn utf8_len(b: u8) -> usize {
    if b < 0x80 {
        1
    } else if b >> 5 == 0b110 {
        2
    } else if b >> 4 == 0b1110 {
        3
    } else if b >> 3 == 0b11110 {
        4
    } else {
        1
    }
}

pub(crate) fn parse(text: &str) -> Result<Val, JishiError> {
    let mut p = P { b: text.as_bytes(), i: 0 };
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        return Err(err("值错误", "不是有效的 JSON 文本：末尾有多余内容"));
    }
    Ok(v)
}

/// 供 `网络.获取JSON` 复用。
pub(crate) fn parse_pub(text: &str) -> Result<Val, JishiError> {
    parse(text)
}

// ---------------------------------------------------------------------------
// 模块
// ---------------------------------------------------------------------------

pub(crate) fn to_text(v: &Val, indent: Option<usize>) -> String {
    let mut out = String::new();
    encode(v, indent, 0, &mut out);
    out
}

fn indent_of(args: &[Val], idx: usize) -> Option<usize> {
    match args.get(idx) {
        None | Some(Val::None_) => None,
        Some(x) => super::as_intish(x).ok().filter(|n| *n >= 0).map(|n| n as usize),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! {
        "转文本" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("转文本", args, 1, 2)?;
            let ind = indent_of(args, 1);
            Ok(Val::Str(to_text(&args[0], ind)))
        },
        "解析" => |args: &[Val], _vm: &mut VM| -> R {
            need("解析", args, 1)?;
            parse(&as_text(&args[0]))
        },
        "读文件" => |args: &[Val], _vm: &mut VM| -> R {
            need("读文件", args, 1)?;
            let p = as_text(&args[0]);
            let text = std::fs::read_to_string(&p)
                .map_err(|e| err("值错误", format!("读文件「{p}」失败：{e}")))?;
            parse(&text)
        },
        "写文件" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("写文件", args, 2, 3)?;
            let p = as_text(&args[0]);
            let ind = indent_of(args, 2);
            let text = to_text(&args[1], ind);
            std::fs::write(&p, text.as_bytes())
                .map_err(|e| err("值错误", format!("写文件「{p}」失败：{e}")))?;
            Ok(Val::None_)
        },
    }
}
