//! 标准库 `正则`（Python 侧 `jishi/stdlib/正则.py`，6 个函数）。
//!
//! **引擎自己写**（没有 `regex` / `fancy-regex` crate）。做法是
//! 「语法树 + 回溯匹配」——不用编译成字节码，因为标准库这边用到的模式
//! 都很短，回溯法够用，代码量也小得多。
//!
//! ## 支持的范围
//!
//! | 类别 | 支持 |
//! |---|---|
//! | 字面量 / `.` | ✅（`.` 不匹配换行，与 Python 默认一致） |
//! | 字符类 | `[abc]` `[^abc]` `[a-z]`，类内可用 `\d` `\w` `\s` |
//! | 量词 | `*` `+` `?` `{m}` `{m,}` `{m,n}`，以及非贪婪 `*?` `+?` `??` `{m,n}?` |
//! | 分组 | `(...)` `(?:...)` `(?P<名>...)`（名字只作文档） |
//! | 前后查看 | `(?=...)` `(?!...)` |
//! | 锚点 | `^` `$` `\A` `\Z` `\b` `\B` |
//! | 简写类 | `\d` `\D` `\w` `\W` `\s` `\S`（都按 Unicode 判，与 Python 3 一致） |
//! | 反向引用 | `\1`…`\9` |
//! | 转义 | `\n` `\t` `\r` `\f` `\v` `\0` `\xHH` `\uXXXX` `\\` 等 |
//!
//! **不支持**（会明确报错或按字面处理，不会静默给错）：内联标志
//! `(?i)`、`(?m)`、`(?s)`、占有量词 `*+`、条件组、`\p{...}`。
//!
//! `查找全部` 的返回形状照 Python 的 `findall`：没有分组给「整个匹配」，
//! 一个分组给「那个分组」，两个以上给元组（基石这边是列表）。
//! 分组没参与匹配时 Python 给 `None`，这里给空文本——这一点不同，见下。

use std::collections::HashMap;

use crate::stdlib::{as_text, need, table, R};
use crate::{err, JishiError, Val, VM};

// ---------------------------------------------------------------------------
// 语法树
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
enum CItem {
    Ch(char),
    Range(char, char),
    /// `\d` / `\D`（`true` 表示取反）
    Digit(bool),
    /// `\w` / `\W`
    Word(bool),
    /// `\s` / `\S`
    Space(bool),
}

#[derive(Clone, Debug)]
enum Node {
    Char(char),
    Any,
    Class { neg: bool, items: Vec<CItem> },
    Start,
    End,
    TextStart,
    TextEnd,
    WordBoundary(bool),
    Group { idx: usize, capturing: bool, inner: Box<Node> },
    Look { neg: bool, inner: Box<Node> },
    BackRef(usize),
    Concat(Vec<Node>),
    Alt(Vec<Node>),
    Repeat { inner: Box<Node>, min: u32, max: u32, greedy: bool },
}

struct Prog {
    root: Node,
    ngroups: usize,
}

type Caps = Vec<Option<(usize, usize)>>;

// ---------------------------------------------------------------------------
// 字符判定（与 Python 3 的 str 模式一致：按 Unicode）
// ---------------------------------------------------------------------------

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn is_digit(c: char) -> bool {
    // Python 的 `\d` 是 Unicode 的 Nd；用「ASCII + 各数字区块」近似
    c.is_ascii_digit() || (c.is_numeric() && !c.is_alphabetic() && {
        // 排除 `一`、`Ⅷ` 这类「数值字符但非十进制数字」
        let u = c as u32;
        (0x0660..=0x0669).contains(&u)
            || (0x06F0..=0x06F9).contains(&u)
            || (0x0966..=0x096F).contains(&u)
            || (0x09E6..=0x09EF).contains(&u)
            || (0x0A66..=0x0A6F).contains(&u)
            || (0x0AE6..=0x0AEF).contains(&u)
            || (0x0B66..=0x0B6F).contains(&u)
            || (0x0BE6..=0x0BEF).contains(&u)
            || (0x0C66..=0x0C6F).contains(&u)
            || (0x0CE6..=0x0CEF).contains(&u)
            || (0x0D66..=0x0D6F).contains(&u)
            || (0x0E50..=0x0E59).contains(&u)
            || (0x0ED0..=0x0ED9).contains(&u)
            || (0x0F20..=0x0F29).contains(&u)
            || (0xFF10..=0xFF19).contains(&u)
    })
}

fn is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c as u32, 0x1C..=0x1F)
}

// ---------------------------------------------------------------------------
// 解析
// ---------------------------------------------------------------------------

struct Parser {
    p: Vec<char>,
    i: usize,
    ngroups: usize,
}

impl Parser {
    fn peek(&self) -> Option<char> {
        self.p.get(self.i).copied()
    }

    fn bad<T>(&self, msg: &str) -> Result<T, JishiError> {
        Err(err("值错误", format!("正则表达式写错了：{msg}")))
    }

    fn alt(&mut self) -> Result<Node, JishiError> {
        let mut parts = vec![self.concat()?];
        while self.peek() == Some('|') {
            self.i += 1;
            parts.push(self.concat()?);
        }
        Ok(if parts.len() == 1 {
            parts.pop().unwrap()
        } else {
            Node::Alt(parts)
        })
    }

    fn concat(&mut self) -> Result<Node, JishiError> {
        let mut items = Vec::new();
        while let Some(c) = self.peek() {
            if c == '|' || c == ')' {
                break;
            }
            items.push(self.repeat()?);
        }
        Ok(if items.len() == 1 {
            items.pop().unwrap()
        } else {
            Node::Concat(items)
        })
    }

    fn repeat(&mut self) -> Result<Node, JishiError> {
        let atom = self.atom()?;
        let (min, max) = match self.peek() {
            Some('*') => {
                self.i += 1;
                (0, u32::MAX)
            }
            Some('+') => {
                self.i += 1;
                (1, u32::MAX)
            }
            Some('?') => {
                self.i += 1;
                (0, 1)
            }
            Some('{') => {
                // `{m}` / `{m,}` / `{m,n}`；不是量词就当字面量（Python 同）
                match self.try_braces() {
                    Some(v) => v,
                    None => return Ok(atom),
                }
            }
            _ => return Ok(atom),
        };
        // 非贪婪
        let greedy = if self.peek() == Some('?') {
            self.i += 1;
            false
        } else {
            true
        };
        if self.peek() == Some('*') || self.peek() == Some('+') || self.peek() == Some('?') {
            return self.bad("量词不能连着写两次（比如 `a**`）");
        }
        Ok(Node::Repeat { inner: Box::new(atom), min, max, greedy })
    }

    fn try_braces(&mut self) -> Option<(u32, u32)> {
        let save = self.i;
        self.i += 1; // 跳过 '{'
        let mut lo = String::new();
        while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
            lo.push(self.p[self.i]);
            self.i += 1;
        }
        if lo.is_empty() {
            self.i = save;
            return None;
        }
        let lo_v: u32 = lo.parse().ok()?;
        match self.peek() {
            Some('}') => {
                self.i += 1;
                Some((lo_v, lo_v))
            }
            Some(',') => {
                self.i += 1;
                let mut hi = String::new();
                while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                    hi.push(self.p[self.i]);
                    self.i += 1;
                }
                if self.peek() != Some('}') {
                    self.i = save;
                    return None;
                }
                self.i += 1;
                if hi.is_empty() {
                    Some((lo_v, u32::MAX))
                } else {
                    let hi_v: u32 = hi.parse().ok()?;
                    Some((lo_v, hi_v))
                }
            }
            _ => {
                self.i = save;
                None
            }
        }
    }

    fn atom(&mut self) -> Result<Node, JishiError> {
        let c = match self.peek() {
            Some(c) => c,
            None => return self.bad("表达式不该在这里结束"),
        };
        match c {
            '(' => {
                self.i += 1;
                let mut capturing = true;
                let mut look: Option<bool> = None;
                if self.peek() == Some('?') {
                    self.i += 1;
                    match self.peek() {
                        Some(':') => {
                            self.i += 1;
                            capturing = false;
                        }
                        Some('=') => {
                            self.i += 1;
                            capturing = false;
                            look = Some(false);
                        }
                        Some('!') => {
                            self.i += 1;
                            capturing = false;
                            look = Some(true);
                        }
                        Some('P') => {
                            // `(?P<名字>...)`：名字只作文档
                            self.i += 1;
                            if self.peek() != Some('<') {
                                return self.bad("`(?P` 后面要跟 `<名字>`");
                            }
                            while self.peek().is_some() && self.peek() != Some('>') {
                                self.i += 1;
                            }
                            if self.peek() != Some('>') {
                                return self.bad("组名没有收尾的 `>`");
                            }
                            self.i += 1;
                        }
                        other => {
                            return self.bad(&format!(
                                "不支持的分组写法 `(?{}`（目前只支持 `(?:`、`(?=`、`(?!`、`(?P<名>`)",
                                other.unwrap_or(' ')
                            ))
                        }
                    }
                }
                let inner = self.alt()?;
                if self.peek() != Some(')') {
                    return self.bad("括号没有配对");
                }
                self.i += 1;
                if let Some(neg) = look {
                    return Ok(Node::Look { neg, inner: Box::new(inner) });
                }
                if capturing {
                    self.ngroups += 1;
                    let idx = self.ngroups;
                    return Ok(Node::Group { idx, capturing: true, inner: Box::new(inner) });
                }
                Ok(inner)
            }
            '[' => self.class(),
            '.' => {
                self.i += 1;
                Ok(Node::Any)
            }
            '^' => {
                self.i += 1;
                Ok(Node::Start)
            }
            '$' => {
                self.i += 1;
                Ok(Node::End)
            }
            '\\' => self.escape_atom(),
            _ => {
                self.i += 1;
                Ok(Node::Char(c))
            }
        }
    }

    fn class(&mut self) -> Result<Node, JishiError> {
        self.i += 1; // '['
        let neg = if self.peek() == Some('^') {
            self.i += 1;
            true
        } else {
            false
        };
        let mut items: Vec<CItem> = Vec::new();
        let mut first = true;
        loop {
            let c = match self.peek() {
                Some(c) => c,
                None => return self.bad("字符类没有收尾的 `]`"),
            };
            // `]` 出现在开头时是字面量
            if c == ']' && !first {
                self.i += 1;
                break;
            }
            first = false;
            let lo = self.class_item()?;
            // 区间 `a-z`（只有单个字符才能做区间端点）
            if self.peek() == Some('-')
                && matches!(self.p.get(self.i + 1), Some(&c2) if c2 != ']')
            {
                if let CItem::Ch(a) = lo {
                    self.i += 1; // '-'
                    match self.class_item()? {
                        CItem::Ch(b) => {
                            items.push(CItem::Range(a, b));
                            continue;
                        }
                        other => {
                            items.push(CItem::Ch(a));
                            items.push(CItem::Ch('-'));
                            items.push(other);
                            continue;
                        }
                    }
                }
            }
            items.push(lo);
        }
        if items.is_empty() {
            return self.bad("字符类里什么都没有");
        }
        Ok(Node::Class { neg, items })
    }

    fn class_item(&mut self) -> Result<CItem, JishiError> {
        let c = match self.peek() {
            Some(c) => c,
            None => return self.bad("字符类没有收尾的 `]`"),
        };
        if c != '\\' {
            self.i += 1;
            return Ok(CItem::Ch(c));
        }
        self.i += 1;
        let e = match self.peek() {
            Some(e) => e,
            None => return self.bad("反斜杠后面没有东西"),
        };
        self.i += 1;
        Ok(match e {
            'd' => CItem::Digit(false),
            'D' => CItem::Digit(true),
            'w' => CItem::Word(false),
            'W' => CItem::Word(true),
            's' => CItem::Space(false),
            'S' => CItem::Space(true),
            other => CItem::Ch(self.decode_escape_char(other)?),
        })
    }

    /// 把 `\按下的字符` 解成真正要匹配的字符（`\n` `\xHH` 这类）。
    fn decode_escape_char(&mut self, e: char) -> Result<char, JishiError> {
        Ok(match e {
            'n' => '\n',
            't' => '\t',
            'r' => '\r',
            'f' => '\u{c}',
            'v' => '\u{b}',
            '0' => '\0',
            'a' => '\u{7}',
            'x' => {
                let h = self.take_hex(2)?;
                char::from_u32(h).unwrap_or('\u{FFFD}')
            }
            'u' => {
                let h = self.take_hex(4)?;
                char::from_u32(h).unwrap_or('\u{FFFD}')
            }
            other => other,
        })
    }

    fn take_hex(&mut self, n: usize) -> Result<u32, JishiError> {
        let mut s = String::new();
        for _ in 0..n {
            match self.peek() {
                Some(c) if c.is_ascii_hexdigit() => {
                    s.push(c);
                    self.i += 1;
                }
                _ => return self.bad("十六进制转义位数不够"),
            }
        }
        Ok(u32::from_str_radix(&s, 16).unwrap_or(0))
    }

    fn escape_atom(&mut self) -> Result<Node, JishiError> {
        self.i += 1; // '\\'
        let e = match self.peek() {
            Some(e) => e,
            None => return self.bad("反斜杠后面没有东西"),
        };
        self.i += 1;
        Ok(match e {
            'd' => Node::Class { neg: false, items: vec![CItem::Digit(false)] },
            'D' => Node::Class { neg: false, items: vec![CItem::Digit(true)] },
            'w' => Node::Class { neg: false, items: vec![CItem::Word(false)] },
            'W' => Node::Class { neg: false, items: vec![CItem::Word(true)] },
            's' => Node::Class { neg: false, items: vec![CItem::Space(false)] },
            'S' => Node::Class { neg: false, items: vec![CItem::Space(true)] },
            'b' => Node::WordBoundary(true),
            'B' => Node::WordBoundary(false),
            'A' => Node::TextStart,
            'Z' => Node::TextEnd,
            '1'..='9' => {
                // 反向引用：尽量多读几位数字，超出分组数就退回来
                let mut n = e.to_digit(10).unwrap() as usize;
                while let Some(c) = self.peek() {
                    if let Some(d) = c.to_digit(10) {
                        let cand = n * 10 + d as usize;
                        if cand <= self.ngroups {
                            n = cand;
                            self.i += 1;
                            continue;
                        }
                    }
                    break;
                }
                if n > self.ngroups {
                    return self.bad(&format!("引用了不存在的分组 `\\{n}`"));
                }
                Node::BackRef(n)
            }
            other => Node::Char(self.decode_escape_char(other)?),
        })
    }
}

fn compile(pattern: &str) -> Result<Prog, JishiError> {
    let mut p = Parser { p: pattern.chars().collect(), i: 0, ngroups: 0 };
    let root = p.alt()?;
    if p.i < p.p.len() {
        return Err(err(
            "值错误",
            format!("正则表达式写错了：第 {} 个字符处多出来一个「{}」", p.i + 1, p.p[p.i]),
        ));
    }
    Ok(Prog { root, ngroups: p.ngroups })
}

// ---------------------------------------------------------------------------
// 匹配（回溯 + 续延）
// ---------------------------------------------------------------------------

#[allow(clippy::too_many_arguments)]
fn match_one(
    node: &Node,
    inp: &[char],
    pos: usize,
    caps: &mut Caps,
    k: &mut dyn FnMut(usize, &mut Caps) -> bool,
) -> bool {
    match node {
        Node::Char(c) => {
            if inp.get(pos) == Some(c) {
                k(pos + 1, caps)
            } else {
                false
            }
        }
        Node::Any => {
            if pos < inp.len() && inp[pos] != '\n' {
                k(pos + 1, caps)
            } else {
                false
            }
        }
        Node::Class { neg, items } => {
            let c = match inp.get(pos) {
                Some(c) => *c,
                None => return false,
            };
            let mut hit = false;
            for it in items {
                hit = match it {
                    CItem::Ch(x) => c == *x,
                    CItem::Range(a, b) => *a <= c && c <= *b,
                    CItem::Digit(n) => is_digit(c) != *n,
                    CItem::Word(n) => is_word(c) != *n,
                    CItem::Space(n) => is_space(c) != *n,
                };
                if hit {
                    break;
                }
            }
            if hit != *neg {
                k(pos + 1, caps)
            } else {
                false
            }
        }
        Node::Start => {
            if pos == 0 {
                k(pos, caps)
            } else {
                false
            }
        }
        Node::End => {
            // Python 的 `$` 也匹配「末尾那个换行符之前」
            if pos == inp.len() || (pos + 1 == inp.len() && inp[pos] == '\n') {
                k(pos, caps)
            } else {
                false
            }
        }
        Node::TextStart => {
            if pos == 0 {
                k(pos, caps)
            } else {
                false
            }
        }
        Node::TextEnd => {
            if pos == inp.len() {
                k(pos, caps)
            } else {
                false
            }
        }
        Node::WordBoundary(want) => {
            let before = pos > 0 && is_word(inp[pos - 1]);
            let after = pos < inp.len() && is_word(inp[pos]);
            let at_boundary = before != after;
            if at_boundary == *want {
                k(pos, caps)
            } else {
                false
            }
        }
        Node::Group { idx, capturing, inner } => {
            if !*capturing {
                return match_one(inner, inp, pos, caps, k);
            }
            let start = pos;
            let idx = *idx;
            let saved = caps[idx];
            let mut cont = |end: usize, c: &mut Caps| -> bool {
                let prev = c[idx];
                c[idx] = Some((start, end));
                if k(end, c) {
                    return true;
                }
                c[idx] = prev;
                false
            };
            let ok = match_one(inner, inp, pos, caps, &mut cont);
            if !ok {
                caps[idx] = saved;
            }
            ok
        }
        Node::Look { neg, inner } => {
            let mut snapshot = caps.clone();
            let matched = match_one(inner, inp, pos, &mut snapshot, &mut |_, _| true);
            if matched != *neg {
                k(pos, caps)
            } else {
                false
            }
        }
        Node::BackRef(n) => match caps.get(*n).copied().flatten() {
            Some((a, b)) => {
                let text: Vec<char> = inp[a..b].to_vec();
                if pos + text.len() <= inp.len() && inp[pos..pos + text.len()] == text[..] {
                    k(pos + text.len(), caps)
                } else {
                    false
                }
            }
            None => false,
        },
        Node::Concat(items) => match_seq(items, inp, pos, caps, k),
        Node::Alt(parts) => {
            for part in parts {
                if match_one(part, inp, pos, caps, k) {
                    return true;
                }
            }
            false
        }
        Node::Repeat { inner, min, max, greedy } => {
            repeat_match(inner, inp, pos, 0, *min, *max, *greedy, caps, k)
        }
    }
}

fn match_seq(
    seq: &[Node],
    inp: &[char],
    pos: usize,
    caps: &mut Caps,
    k: &mut dyn FnMut(usize, &mut Caps) -> bool,
) -> bool {
    match seq.split_first() {
        None => k(pos, caps),
        Some((first, rest)) => {
            let mut cont = |p: usize, c: &mut Caps| -> bool { match_seq(rest, inp, p, c, k) };
            match_one(first, inp, pos, caps, &mut cont)
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn repeat_match(
    inner: &Node,
    inp: &[char],
    pos: usize,
    count: u32,
    min: u32,
    max: u32,
    greedy: bool,
    caps: &mut Caps,
    k: &mut dyn FnMut(usize, &mut Caps) -> bool,
) -> bool {
    let can_more = count < max;
    let try_more = |caps: &mut Caps, k: &mut dyn FnMut(usize, &mut Caps) -> bool| -> bool {
        if !can_more {
            return false;
        }
        match_one(inner, inp, pos, caps, &mut |p, c| {
            // 空匹配保护：否则 `(a*)*` 会原地打转
            if p == pos {
                return false;
            }
            repeat_match(inner, inp, p, count + 1, min, max, greedy, c, k)
        })
    };
    if greedy {
        if try_more(caps, k) {
            return true;
        }
        if count >= min {
            return k(pos, caps);
        }
        false
    } else {
        if count >= min && k(pos, caps) {
            return true;
        }
        try_more(caps, k)
    }
}

/// 从 `pos` 起尝试匹配，成功就把分组结果写回 `caps` 并返回结尾位置。
fn match_at(prog: &Prog, inp: &[char], pos: usize) -> Option<(usize, Caps)> {
    let mut caps: Caps = vec![None; prog.ngroups + 1];
    let mut end: Option<usize> = None;
    let ok = {
        let root = &prog.root;
        let mut cont = |p: usize, _c: &mut Caps| -> bool {
            end = Some(p);
            true
        };
        match_one(root, inp, pos, &mut caps, &mut cont)
    };
    let _ = prog;
    if ok {
        Some((end.unwrap_or(pos), caps))
    } else {
        None
    }
}

/// 整串里的第一处匹配（Python 的 `search`）。
fn search(prog: &Prog, text: &str) -> Option<(usize, usize, Caps)> {
    let inp: Vec<char> = text.chars().collect();
    for start in 0..=inp.len() {
        if let Some((end, caps)) = match_at(prog, &inp, start) {
            let mut caps = caps;
            caps[0] = Some((start, end));
            return Some((start, end, caps));
        }
    }
    None
}

/// 是否从开头就匹配（Python 的 `match`）。
fn matches_from_start(prog: &Prog, text: &str) -> bool {
    let inp: Vec<char> = text.chars().collect();
    match_at(prog, &inp, 0).is_some()
}

/// 找出全部匹配：`(起点, 终点, 分组)`。
fn find_all(prog: &Prog, text: &str) -> Vec<(usize, usize, Caps)> {
    let inp: Vec<char> = text.chars().collect();
    let mut out = Vec::new();
    let mut pos = 0usize;
    while pos <= inp.len() {
        match match_at(prog, &inp, pos) {
            Some((end, mut caps)) => {
                caps[0] = Some((pos, end));
                out.push((pos, end, caps));
                // 空匹配要往前挪一格，否则会原地死循环（Python 也是这么做的）
                pos = if end == pos { pos + 1 } else { end };
            }
            None => pos += 1,
        }
    }
    out
}

fn slice(inp: &[char], r: Option<(usize, usize)>) -> String {
    match r {
        Some((a, b)) if b <= inp.len() && a <= b => inp[a..b].iter().collect(),
        _ => String::new(),
    }
}

fn chars_of(s: &str) -> Vec<char> {
    s.chars().collect()
}

// ---------------------------------------------------------------------------
// 替换模板（`\1` / `\g<1>` / `\\` / `\n` 这类）
// ---------------------------------------------------------------------------

fn expand(tpl: &str, inp: &[char], caps: &Caps) -> Result<String, JishiError> {
    let t: Vec<char> = tpl.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < t.len() {
        let c = t[i];
        if c != '\\' {
            out.push(c);
            i += 1;
            continue;
        }
        i += 1;
        let e = match t.get(i) {
            Some(e) => *e,
            None => return Err(err("值错误", "替换模板不该以反斜杠结尾")),
        };
        i += 1;
        match e {
            '0'..='9' => {
                let n = e.to_digit(10).unwrap() as usize;
                if n >= caps.len() {
                    return Err(err("值错误", format!("替换模板引用了不存在的分组 `\\{n}`")));
                }
                out.push_str(&slice(inp, caps[n]));
            }
            'g' => {
                if t.get(i) != Some(&'<') {
                    return Err(err("值错误", "替换模板里 `\\g` 后面要跟 `<组号>`"));
                }
                i += 1;
                let mut num = String::new();
                while let Some(c) = t.get(i) {
                    if *c == '>' {
                        break;
                    }
                    num.push(*c);
                    i += 1;
                }
                if t.get(i) != Some(&'>') {
                    return Err(err("值错误", "替换模板里的 `\\g<` 没有收尾的 `>`"));
                }
                i += 1;
                match num.trim().parse::<usize>() {
                    Ok(n) if n < caps.len() => out.push_str(&slice(inp, caps[n])),
                    // 名字引用（`\g<名>`）这里不支持——分组名只作文档
                    _ => {
                        return Err(err(
                            "值错误",
                            format!("替换模板里的 `\\g<{num}>` 不是有效的分组号（本宿主不支持按名字引用分组）"),
                        ))
                    }
                }
            }
            'n' => out.push('\n'),
            't' => out.push('\t'),
            'r' => out.push('\r'),
            '\\' => out.push('\\'),
            other => {
                return Err(err(
                    "值错误",
                    format!("替换模板里的 `\\{other}` 不是有效的转义（想输反斜杠就写 `\\\\`）"),
                ))
            }
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// 模块
// ---------------------------------------------------------------------------

fn build(pattern: &Val) -> Result<Prog, JishiError> {
    compile(&as_text(pattern))
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! {
        "匹配" => |args: &[Val], _vm: &mut VM| -> R {
            need("匹配", args, 2)?;
            let prog = build(&args[0])?;
            Ok(Val::Bool(matches_from_start(&prog, &as_text(&args[1]))))
        },
        "搜索" => |args: &[Val], _vm: &mut VM| -> R {
            need("搜索", args, 2)?;
            let prog = build(&args[0])?;
            let text = as_text(&args[1]);
            Ok(Val::Str(match search(&prog, &text) {
                Some((a, b, _)) => text.chars().collect::<Vec<char>>()[a..b].iter().collect(),
                None => String::new(),
            }))
        },
        "查找全部" => |args: &[Val], _vm: &mut VM| -> R {
            need("查找全部", args, 2)?;
            let prog = build(&args[0])?;
            let text = as_text(&args[1]);
            let inp = chars_of(&text);
            let found = find_all(&prog, &text);
            let n = prog.ngroups;
            let items: Vec<Val> = found
                .iter()
                .map(|(_, _, caps)| {
                    if n == 0 {
                        Val::Str(slice(&inp, caps[0]))
                    } else if n == 1 {
                        Val::Str(slice(&inp, caps[1]))
                    } else {
                        // 两个以上分组：Python 的 findall 给元组，这边给列表
                        crate::list_new(
                            (1..=n).map(|g| Val::Str(slice(&inp, caps[g]))).collect(),
                        )
                    }
                })
                .collect();
            Ok(crate::list_new(items))
        },
        "替换" => |args: &[Val], _vm: &mut VM| -> R {
            need("替换", args, 3)?;
            let prog = build(&args[0])?;
            let tpl = as_text(&args[1]);
            let text = as_text(&args[2]);
            let inp = chars_of(&text);
            let found = find_all(&prog, &text);
            let mut out = String::new();
            let mut last = 0usize;
            for (a, b, caps) in &found {
                out.extend(inp[last..*a].iter());
                out.push_str(&expand(&tpl, &inp, caps)?);
                last = *b;
            }
            if last < inp.len() {
                out.extend(inp[last..].iter());
            }
            Ok(Val::Str(out))
        },
        "拆分" => |args: &[Val], _vm: &mut VM| -> R {
            need("拆分", args, 2)?;
            let prog = build(&args[0])?;
            let text = as_text(&args[1]);
            let inp = chars_of(&text);
            let found = find_all(&prog, &text);
            let mut out: Vec<Val> = Vec::new();
            let mut last = 0usize;
            for (a, b, caps) in &found {
                out.push(Val::Str(slice(&inp, Some((last, *a)))));
                // Python 的 `re.split` 会把捕获组的内容也放进结果里
                for g in 1..=prog.ngroups {
                    let piece = slice(&inp, caps[g]);
                    if !piece.is_empty() || caps[g].is_some() {
                        out.push(Val::Str(piece));
                    }
                }
                last = *b;
            }
            out.push(Val::Str(slice(&inp, Some((last, inp.len())))));
            Ok(crate::list_new(out))
        },
        "分组" => |args: &[Val], _vm: &mut VM| -> R {
            need("分组", args, 2)?;
            let prog = build(&args[0])?;
            let text = as_text(&args[1]);
            let inp = chars_of(&text);
            match search(&prog, &text) {
                Some((_, _, caps)) => Ok(crate::list_new(
                    (1..=prog.ngroups).map(|g| Val::Str(slice(&inp, caps[g]))).collect(),
                )),
                None => Ok(crate::list_new(Vec::new())),
            }
        },
    }
}
