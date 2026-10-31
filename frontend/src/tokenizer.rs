// 基石词法分析器（R2：从 `jishi/tokenizer.py` 逐行为移植）。
//
// ⚠️ **这个文件的目标是「与 Python 侧逐 token 一致」，不是「写一个更好的分词器」。**
// 一切看起来可以更简洁的地方（比如那个「一个字符 = 一个码点」的列号口径、
// `-#` 注释的边界、三引号跨行的 NEWLINE 补发），都**照抄**原实现 ——
// 差异一旦出现，`tools/conformance.py compare --stage tokens` 会精确指出第几个
// token 的哪个字段不一样，那才是唯一验收标准（见 `docs/前端契约.md`）。
//
// 与 Python 版的对应关系（行号指 `jishi/tokenizer.py`）：
//   KEYWORDS / GLUE_CHECK           ↔  KEYWORDS / _GLUE_CHECK_SKIP
//   OPERATORS                       ↔  _OPERATORS_LONGEST_FIRST
//   tokenize                        ↔  Tokenizer._run
//   measure_indent                  ↔  Tokenizer._measure_indent
//   tokenize_line                   ↔  Tokenizer._tokenize_line
//   scan_string / scan_fstring      ↔  _scan_string / _scan_fstring
//   continue_triple                 ↔  _continue_triple
//   scan_number / scan_word         ↔  _scan_number / _scan_word
//   escape                          ↔  Tokenizer._escape

/// 关键字表（顺序即 Python 里 `KEYWORDS` 的插入顺序 —— **不要重排**：
/// 粘连检查依「长度降序 + 稳定排序」，同长度时顺序就是插入顺序）。
///
/// ⚠️ 实际判定走 `is_keyword` 里的 `matches!`（`match` 会编译成
/// 「按长度跳转 + 定长比较」，而线性 `contains` 在中文代码里是热点 ——
/// 几乎每个标识符都要扫一遍）。**这张表是它的出处**，由 `内置关键词表()`
/// 暴露给 Python 侧的漂移检测测试逐项比对。
pub const KEYWORDS: &[&str] = &[
    "令", "如果", "否则", "否则如果", "遍历", "循环", "当", "中断", "继续", "函数",
    "返回", "导入", "在", "从", "为", "真", "假", "空", "与", "或", "非", "是",
    "不是", "不在", "次", "类", "继承", "新建", "尝试", "捕获", "最终", "抛出",
    "用", "匹配", "情形", "枚举",
];

/// 做粘连检查的关键字，**按 Python 的 `sorted(KEYWORDS, key=len, reverse=True)`
/// 稳定排序后的顺序**（长度降序；同长度按插入顺序），并已剔掉
/// `_GLUE_CHECK_SKIP = {不是, 不在, 是, 用, 匹配, 枚举}` 与长度为 1 的。
///
/// ⚠️ 实际判定走 `glue_hit` 里的 `match`（线性扫 17 遍前缀在中文代码里
/// 是热点：几乎每个标识符都要走一遍）。**这张表是它的出处**，由
/// `内置粘连前缀表()` 暴露给 Python 侧的漂移检测测试比对。
pub const GLUE_CHECK: &[&str] = &[
    "否则如果", "如果", "否则", "遍历", "循环", "中断", "继续", "函数", "返回",
    "导入", "继承", "新建", "尝试", "捕获", "最终", "抛出", "情形",
];

/// 运算符，**按长度降序**（同长度之间的相对顺序不影响结果：
/// 同长度里不可能有两个都前缀匹配的串）。
pub const OPERATORS: &[&str] = &[
    "//=", "**=", "**", "//", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=",
    "->", "+", "-", "*", "/", "%", "<", ">", "=", "(", ")", "[", "]", "{", "}", ",",
    ".", ":", "^",
];

// ---------------------------------------------------------------------------
// 值
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub enum Part {
    Text(String),
    Expr(String),
}

#[derive(Debug, Clone)]
pub enum Value {
    None,
    Int(i64),
    /// 超出 i64 的整数字面量：保留**原文十进制串**（Python 侧会把它变成
    /// 任意精度整数；写数字进 JSON 会在宿主里丢精度 —— 同一课 M32 已上过）。
    Big(String),
    Float(f64),
    Str(String),
    /// FSTRING：分段列表 `[("text"|"expr", 内容), …]`
    Parts(Vec<Part>),
}

/// 只问一件事：这个 token 是不是括号。
///
/// ⚠️ **必须先判 `kind == "OP"`**：Python 侧写的是 `t.type == "OP" and
/// t.value in "([{"` —— 少了前半句，一个值恰好是 `(` 的**字符串字面量**也会被
/// 算进括号深度，于是后面整份文件的缩进判定全错。
#[inline]
fn bracket_delta(t: &Tok) -> i64 {
    if t.kind != "OP" {
        return 0;
    }
    match &t.value {
        Value::Str(s) => match s.as_str() {
            "(" | "[" | "{" => 1,
            ")" | "]" | "}" => -1,
            _ => 0,
        },
        _ => 0,
    }
}

#[derive(Debug, Clone)]
pub struct Tok {
    pub kind: &'static str,
    pub value: Value,
    pub line: usize,
    pub col: usize,
    pub end_col: usize,
    /// 这个 token 所在**行的下标**（0 起）；`None` → 空串
    /// （收尾那批 DEDENT / EOF）。存下标而不存文本，是为了让 120k 个 token
    /// 共享同一行字符串、不重复分配。
    pub src: Option<usize>,
}

impl Tok {
    fn new(kind: &'static str, value: Value, line: usize, col: usize,
           end_col: usize, src: Option<usize>) -> Self {
        Tok { kind, value, line, col, end_col, src }
    }
}

// ---------------------------------------------------------------------------
// 错误
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct LexErr {
    pub code: &'static str,
    pub message: String,
    /// 源码行的下标（0 起）；`None` → 空串。
    ///
    /// ⚠️ 目前**不由扩展回传**：Python 壳拿到 `行` 之后一律走
    /// `JishiError.with_source(lines)` 现取（七个词法错误的 `行` 恒在范围内），
    /// 少传一份就少一处可能漂的地方。留着它是为了「错误自带出处」这件事
    /// 在 Rust 侧不丢信息（调试与将来的 Rust 原生前端要用）。
    #[allow(dead_code)]
    pub src: Option<usize>,
    pub line: usize,
    pub col: usize,
    pub hint: Option<String>,
    pub underline: Option<(usize, usize)>,
}

impl LexErr {
    fn new(code: &'static str, message: impl Into<String>, line: usize,
           col: usize, src: Option<usize>) -> Self {
        LexErr { code, message: message.into(), src, line, col, hint: None,
                 underline: None, }
    }
}

// ---------------------------------------------------------------------------
// 字符类别（对应 Python 里的几个正则）
// ---------------------------------------------------------------------------

#[inline]
fn is_cjk(c: char) -> bool {
    matches!(c as u32, 0x3400..=0x4DBF | 0x4E00..=0x9FFF | 0xF900..=0xFAFF)
}

#[inline]
fn is_ascii_start(c: char) -> bool {
    c.is_ascii_alphabetic() || c == '_'
}

#[inline]
fn is_digit(c: char) -> bool {
    c.is_ascii_digit()
}

/// `_scan_word` 吞下的连续词字符：CJK / ASCII 字母下划线 / 数字。
#[inline]
fn is_word_char(c: char) -> bool {
    is_cjk(c) || is_ascii_start(c) || is_digit(c)
}

/// `str.isspace()` 的口径（CPython 的 Unicode 空白集）——
/// 空行判定用的是 `content.strip()`，漏一个字符就是「空行不空」的差异。
#[inline]
fn is_py_space(c: char) -> bool {
    match c {
        '\u{09}'..='\u{0D}' | '\u{1C}'..='\u{1F}' | ' ' | '\u{85}' | '\u{A0}'
        | '\u{1680}' | '\u{2028}' | '\u{2029}' | '\u{202F}' | '\u{205F}'
        | '\u{3000}' => true,
        '\u{2000}'..='\u{200A}' => true,
        _ => false,
    }
}

fn py_strip(s: &[char]) -> &[char] {
    let mut a = 0;
    let mut b = s.len();
    while a < b && is_py_space(s[a]) {
        a += 1;
    }
    while b > a && is_py_space(s[b - 1]) {
        b -= 1;
    }
    &s[a..b]
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

/// `hay[i..].starts_with(needle)`
fn starts_with_at(hay: &[char], i: usize, needle: &[char]) -> bool {
    i + needle.len() <= hay.len() && hay[i..i + needle.len()] == *needle
}

/// 在 `hay` 的 `[from, ..)` 里找 `needle` 的**字符下标**（对应 `str.find`）。
fn find_seq(hay: &[char], from: usize, needle: &[char]) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() || from > hay.len() - needle.len()
    {
        return None;
    }
    (from..=hay.len() - needle.len()).find(|&i| hay[i..i + needle.len()] == *needle)
}

fn chars_of(word: &[char]) -> String {
    word.iter().collect()
}

/// 这个词是不是关键字（与 `KEYWORDS` 逐项一致）。
///
/// 写成 `matches!` 而不是 `KEYWORDS.contains(...)`：后者是**线性扫 36 个串**，
/// 而中文代码里每个标识符都要过一次 —— 实测这是分词核心里的热点之一
/// （`match` 会按长度分组跳转，比较次数恒定）。
#[inline]
fn is_keyword(word: &str) -> bool {
    matches!(word,
        "令" | "如果" | "否则" | "否则如果" | "遍历" | "循环" | "当" | "中断"
        | "继续" | "函数" | "返回" | "导入" | "在" | "从" | "为" | "真" | "假"
        | "空" | "与" | "或" | "非" | "是" | "不是" | "不在" | "次" | "类"
        | "继承" | "新建" | "尝试" | "捕获" | "最终" | "抛出" | "用" | "匹配"
        | "情形" | "枚举")
}

/// `word` 的前两个字（不足两字给 `None`）。用于粘连检查的**常数时间前缀判定**。
fn first_two(word: &str) -> Option<&str> {
    let mut idx = 0usize;
    let mut seen = 0usize;
    for (i, _) in word.char_indices() {
        if seen == 2 {
            idx = i;
            break;
        }
        seen += 1;
    }
    if idx > 0 {
        Some(&word[..idx])
    } else if seen == 2 {
        Some(word)
    } else {
        None
    }
}

/// 粘连检查：`word` 是否以某个**多字关键字**为前缀。命中就返回那个关键字。
///
/// 与 Python 的 `sorted(KEYWORDS, key=len, reverse=True)` 顺序等价：
/// 先看 4 字的「否则如果」，再看 2 字的那一批（`GLUE_CHECK` 里长度为 1 的
/// 一个都没有 —— 单字关键字与海量常用词冲突，做前缀检查只会误报）。
///
/// 调用前提：`word` 是纯 CJK **且不是关键字** —— 于是「以 kw 为前缀」
/// 与「比 kw 长」等价，不必再数字数。
fn glue_hit(word: &str) -> Option<&'static str> {
    if word.starts_with("否则如果") {
        return Some("否则如果");
    }
    match first_two(word) {
        Some("如果") => Some("如果"),
        Some("否则") => Some("否则"),
        Some("遍历") => Some("遍历"),
        Some("循环") => Some("循环"),
        Some("中断") => Some("中断"),
        Some("继续") => Some("继续"),
        Some("函数") => Some("函数"),
        Some("返回") => Some("返回"),
        Some("导入") => Some("导入"),
        Some("继承") => Some("继承"),
        Some("新建") => Some("新建"),
        Some("尝试") => Some("尝试"),
        Some("捕获") => Some("捕获"),
        Some("最终") => Some("最终"),
        Some("抛出") => Some("抛出"),
        Some("情形") => Some("情形"),
        _ => None,
    }
}

fn to_chars(s: &str) -> Vec<char> {
    s.chars().collect()
}

/// 归一化换行（`\r\n` / `\r` → `\n`）并剥掉开头的 BOM（只剥一个）。
fn normalize(text: &str) -> String {
    let mut it = text.chars().peekable();
    if it.peek() == Some(&'\u{feff}') {
        it.next();
    }
    let mut out = String::with_capacity(text.len());
    let mut prev_cr = false;
    for c in it {
        match c {
            '\r' => {
                out.push('\n');
                prev_cr = true;
            }
            '\n' => {
                if prev_cr {
                    prev_cr = false;        // `\r\n` 已经压过 `\n` 了
                } else {
                    out.push('\n');
                }
            }
            _ => {
                out.push(c);
                prev_cr = false;
            }
        }
    }
    out
}

// ---------------------------------------------------------------------------
// 跨行三引号的状态
// ---------------------------------------------------------------------------

struct State {
    pending_triple: Option<Vec<char>>,
    pending_quote: char,
    pending_line: usize,
    pending_col: usize,
}

pub struct Output {
    pub tokens: Vec<Tok>,
    /// 归一化后的行（与 Python 的 `Tokenizer.lines` 逐项相同），
    /// 供 source_line 与报错定位用。
    pub lines: Vec<String>,
}

/// 分词入口。
pub fn tokenize(text: &str, _filename: &str) -> Result<Output, LexErr> {
    let src = normalize(text);
    let mut lines: Vec<String> = src.split('\n').map(|s| s.to_string()).collect();
    if let Some(last) = lines.last() {
        if last.is_empty() {
            lines.pop();
        }
    }

    let n_lines = lines.len();
    let mut tokens: Vec<Tok> = Vec::new();
    let mut indent_stack: Vec<usize> = vec![0];
    let mut bracket_depth: i64 = 0;
    let mut in_block_comment = false;
    let mut st = State { pending_triple: None, pending_quote: '"', pending_line: 1,
                         pending_col: 1 };

    let mut line_idx = 0usize;
    while line_idx < n_lines {
        let line_no = line_idx + 1;

        // ---- 跨行三引号延续 ----
        if st.pending_triple.is_some() {
            let raw = to_chars(&lines[line_idx]);
            let (tok, rest, done) = continue_triple(&raw, line_idx, line_no, &mut st)?;
            if done {
                tokens.push(tok.expect("闭合时必有 token"));
                for t in &rest {
                    bracket_depth = (bracket_depth + bracket_delta(t)).max(0);
                }
                tokens.extend(rest);
                if bracket_depth == 0 {
                    // 收尾引号后面即使什么都没有也要收这一行（M39 的坑）
                    tokens.push(Tok::new("NEWLINE", Value::Str("\n".into()),
                                         line_no, 1, 1, Some(line_idx)));
                }
            }
            line_idx += 1;
            continue;
        }

        // ---- 多行注释：整行跳过（不参与缩进处理）----
        if in_block_comment {
            let raw = to_chars(&lines[line_idx]);
            if find_seq(&raw, 0, &['-', '#']).is_some() {
                in_block_comment = false;
            }
            line_idx += 1;
            continue;
        }

        let raw = to_chars(&lines[line_idx]);
        let mut content_start = 0usize;
        let mut indent_w = 0usize;
        let mut bad_chars: Vec<LexErr> = Vec::new();
        if bracket_depth == 0 {
            let (w, bad, text_len) = measure_indent(&raw, line_idx, line_no);
            indent_w = w;
            bad_chars = bad;
            content_start = text_len;
        }
        let content = &raw[content_start..];

        // ---- 空行 / 纯注释行：不发 NEWLINE，不处理缩进 ----
        let stripped = py_strip(content);
        if stripped.is_empty() || stripped[0] == '#' {
            if stripped.len() >= 2 && stripped[1] == '-' {
                in_block_comment = true;
                if let Some(pos) = find_seq(content, 0, &['#', '-']) {
                    if find_seq(content, pos, &['-', '#']).is_some() {
                        in_block_comment = false;
                    }
                }
            }
            line_idx += 1;
            continue;
        }

        // ---- 缩进错误与层级调整（在「空行/注释行」之后，与 Python 同序）----
        if bracket_depth == 0 {
            if !bad_chars.is_empty() {
                return Err(bad_chars.remove(0));
            }
            let top = *indent_stack.last().expect("缩进栈恒非空");
            if indent_w > top {
                indent_stack.push(indent_w);
                tokens.push(Tok::new("INDENT", Value::Int(indent_w as i64),
                                     line_no, 1, 1, Some(line_idx)));
            } else if indent_w < top {
                loop {
                    let t = match indent_stack.last() {
                        Some(&t) => t,
                        None => break,
                    };
                    if indent_w >= t {
                        break;
                    }
                    indent_stack.pop();
                    tokens.push(Tok::new("DEDENT", Value::Int(indent_w as i64),
                                         line_no, 1, 1, Some(line_idx)));
                }
                if indent_stack.is_empty() || indent_w != *indent_stack.last().unwrap()
                {
                    return Err(LexErr::new(
                        "E0101", "这个缩进层级对不上前面的代码", line_no, 1,
                        Some(line_idx)));
                }
            }
        }

        // ---- 分词本行 ----
        let toks = tokenize_line_impl(&raw, content_start, line_idx, line_no, &mut st)?;
        for t in &toks {
            bracket_depth = (bracket_depth + bracket_delta(t)).max(0);
        }
        tokens.extend(toks);
        if bracket_depth == 0 && st.pending_triple.is_none() {
            tokens.push(Tok::new("NEWLINE", Value::Str("\n".into()), line_no,
                                 content_start + 1, 1, Some(line_idx)));
        }

        line_idx += 1;
    }

    // ---- 文件结束时还停在三引号里：字符串没写完 ----
    if st.pending_triple.is_some() {
        return Err(LexErr {
            code: "E0104",
            message: "字符串没有正常结束，是不是漏了结束引号？".into(),
            src: if st.pending_line >= 1 && st.pending_line <= n_lines {
                Some(st.pending_line - 1)
            } else {
                None
            },
            line: st.pending_line,
            col: st.pending_col,
            hint: None,
            underline: None,
        });
    }

    // ---- EOF：补齐 DEDENT ----
    for _ in 0..indent_stack.len().saturating_sub(1) {
        tokens.push(Tok::new("DEDENT", Value::Int(0), n_lines, 1, 1, None));
    }
    tokens.push(Tok::new("EOF", Value::None, n_lines, 1, 1, None));

    Ok(Output { tokens, lines })
}

// ---------------------------------------------------------------------------
// 给 Python 侧「漂移检测」用的表（M51 的老规矩：宿主自己报家底）
// ---------------------------------------------------------------------------

/// 关键字表（与 `is_keyword` 必须同源 —— 测试会逐项比对这两者，
/// 以及和 Python 的 `tokenizer.KEYWORDS`）。
pub fn keyword_table() -> &'static [&'static str] {
    KEYWORDS
}

pub fn glue_table() -> &'static [&'static str] {
    GLUE_CHECK
}

pub fn operator_table() -> &'static [&'static str] {
    OPERATORS
}

// ---------------------------------------------------------------------------
// 缩进测量
// ---------------------------------------------------------------------------

/// 返回 `(宽度, 错误列表, 缩进文本长度)`。宽度按 tab=4 折算。
fn measure_indent(raw: &[char], src: usize, line_no: usize)
    -> (usize, Vec<LexErr>, usize)
{
    let mut w = 0usize;
    let mut bad: Vec<LexErr> = Vec::new();
    let mut i = 0usize;
    let n = raw.len();
    while i < n {
        let ch = raw[i];
        if ch == ' ' {
            w += 1;
            i += 1;
        } else if ch == '\t' {
            w += 4 - (w % 4);
            i += 1;
        } else if ch == '\u{3000}' {
            bad.push(LexErr::new(
                "E0103", "行首缩进里不能用全角空格（U+3000），请改用半角空格",
                line_no, i + 1, Some(src)));
            i += 1;                      // 继续测量，收集全部错误
        } else {
            break;
        }
    }
    let indent_len = i;
    let indent_text = &raw[..indent_len];
    if indent_text.contains(&'\t') && indent_text.contains(&' ') {
        let pos = indent_text.iter().position(|&c| c == ' ').unwrap();
        bad.push(LexErr::new("E0102", "同一行缩进里混用了 Tab 和空格，请统一",
                             line_no, pos + 1, Some(src)));
    }
    (w, bad, indent_len)
}

// ---------------------------------------------------------------------------
// 行内分词
// ---------------------------------------------------------------------------

/// 给格式化器用的**公开**单行分词（R7.1-d）。
///
/// `line` 是**原始行文本**（未归一化 —— 与 Python 侧
/// `Tokenizer._tokenize_line(code, 0, 1)` 吃的东西一致）。返回的 `col` / `end_col`
/// 是**相对该行的 1 基列号**，于是格式化器可以按 `[col-1, end_col)`（OP 是
/// `[col-1, end_col-1)`）切片取回**原文**（保留全角、引号风格等）。
///
/// ⚠️ 每次调用用一个**全新的词法状态**（`pending_triple` 等不复用）——
/// Python 侧格式化器就是这么用的（每次 `_respacify` 后 `_pending_triple = None`）。
pub fn tokenize_line(line: &str, line_no: usize) -> Result<Vec<Tok>, LexErr> {
    let raw = to_chars(line);
    let mut st = State { pending_triple: None, pending_quote: '"', pending_line: 1,
                         pending_col: 1 };
    tokenize_line_impl(&raw, 0, 0, line_no, &mut st)
}

fn tokenize_line_impl(raw: &[char], start: usize, src: usize, line_no: usize,
                      st: &mut State) -> Result<Vec<Tok>, LexErr> {
    let mut tokens: Vec<Tok> = Vec::new();
    let mut i = start;
    let n = raw.len();

    while i < n {
        let ch = raw[i];

        // 空白（含全角空格，作分隔符）
        if ch == ' ' || ch == '\u{3000}' || ch == '\t' {
            i += 1;
            continue;
        }

        // 注释
        if ch == '#' {
            if i + 1 < n && raw[i + 1] == '-' {
                if let Some(pos) = find_seq(raw, i + 2, &['-', '#']) {
                    i = pos + 2;
                    continue;
                }
                return Ok(tokens);      // 未闭合的多行注释：本行其余都是注释
            }
            break;                      // 单行注释到行尾
        }

        // 字符串 / 插值字符串
        if matches!(ch, '\'' | '"' | '“' | '”' | '‘' | '’' | '「' | '」' | '『'
                    | '』' | '`') {
            if ch == '`' {
                let (tok, ni) = scan_fstring(raw, i, src, line_no)?;
                tokens.push(tok);
                i = ni;
                continue;
            }
            let (tok, ni) = scan_string(raw, i, src, line_no, st)?;
            i = ni;
            match tok {
                Some(t) => tokens.push(t),
                None => return Ok(tokens),   // 三引号挂起，本行到此结束
            }
            continue;
        }

        // 数字
        if is_digit(ch) || (ch == '.' && i + 1 < n && is_digit(raw[i + 1])) {
            let (tok, ni) = scan_number(raw, i, src, line_no);
            tokens.push(tok);
            i = ni;
            continue;
        }

        // 词（标识符 / 关键字）
        if is_cjk(ch) || is_ascii_start(ch) {
            let (tok, ni) = scan_word(raw, i, src, line_no)?;
            tokens.push(tok);
            i = ni;
            continue;
        }

        // 全角标点归一化（一对一映射，不参与多字符运算符匹配）
        if let Some(norm) = normalize_fullwidth(ch) {
            tokens.push(Tok::new("OP", Value::Str(norm.to_string()), line_no,
                                 i + 1, i + 2, Some(src)));
            i += 1;
            continue;
        }

        // 半角运算符：多字符优先（OPERATORS 已按长度降序）
        let mut matched: Option<&str> = None;
        for op in OPERATORS {
            let oc: Vec<char> = op.chars().collect();
            if starts_with_at(raw, i, &oc) {
                matched = Some(op);
                break;
            }
        }
        if let Some(op) = matched {
            let w = op.chars().count();
            tokens.push(Tok::new("OP", Value::Str(op.to_string()), line_no,
                                 i + 1, i + w + 1, Some(src)));
            i += w;
            continue;
        }

        return Err(LexErr {
            code: "E0105",
            message: format!("无法识别的字符「{}」（U+{:04X}）", ch, ch as u32),
            src: Some(src),
            line: line_no,
            col: i + 1,
            hint: Some("如果这是全角字符，请改用半角；或在中文输入法下重敲一遍"
                       .to_string()),
            underline: None,
        });
    }

    Ok(tokens)
}

// ---------------------------------------------------------------------------
// 全角归一化（与 `errors._FULLWIDTH_MAP` 逐项一致）
// ---------------------------------------------------------------------------

/// 全角 → 半角一对一映射（与 `errors._FULLWIDTH_MAP` 的 **44 项**逐项一致）。
/// ⚠️ 后 14 项（`《》【】「」『』‘’“”．、`）容易漏 —— 其中 `《》【】．、`
/// 在词法里是**真会走到**的（`“”‘’「」『』` 会先被当成字符串引号，走不到这里）。
fn normalize_fullwidth(ch: char) -> Option<&'static str> {
    Some(match ch {
        '\u{3000}' => " ",
        '：' => ":",
        '；' => ";",
        '，' => ",",
        '（' => "(",
        '）' => ")",
        '［' => "[",
        '］' => "]",
        '｛' => "{",
        '｝' => "}",
        '＂' => "\"",
        '＇' => "'",
        '！' => "!",
        '？' => "?",
        '＝' => "=",
        '＋' => "+",
        '－' => "-",
        '＊' => "*",
        '／' => "/",
        '％' => "%",
        '＜' => "<",
        '＞' => ">",
        '＆' => "&",
        '｜' => "|",
        '＾' => "^",
        '～' => "~",
        '＠' => "@",
        '＃' => "#",
        '＿' => "_",
        '￥' => "$",
        '《' => "<",
        '》' => ">",
        '【' => "[",
        '】' => "]",
        '「' => "\"",
        '」' => "\"",
        '『' => "'",
        '』' => "'",
        '‘' => "'",
        '’' => "'",
        '“' => "\"",
        '”' => "\"",
        '．' => ".",
        '、' => ",",
        _ => return None,
    })
}

// ---------------------------------------------------------------------------
// 字符串扫描
// ---------------------------------------------------------------------------

/// 中文引号配对：起始引号 → 结束引号。
fn paired_close(q: char) -> char {
    match q {
        '“' => '”',
        '‘' => '’',
        '「' => '」',
        '『' => '』',
        other => other,
    }
}

fn escape(raw: &[char], i: usize, src: usize, line_no: usize)
    -> Result<Vec<char>, LexErr>
{
    if i + 1 >= raw.len() {
        return Err(LexErr::new("E0104", "字符串末尾的转义符没有内容", line_no,
                               i + 1, Some(src)));
    }
    let c = raw[i + 1];
    Ok(match c {
        'n' => vec!['\n'],
        't' => vec!['\t'],
        'r' => vec!['\r'],
        '\\' => vec!['\\'],
        '\'' => vec!['\''],
        '"' => vec!['"'],
        '0' => vec!['\0'],
        // 未知转义：原样保留反斜杠与字符（这样 Windows 路径可以直写）
        other => vec!['\\', other],
    })
}

fn scan_string(raw: &[char], i0: usize, src: usize, line_no: usize,
               st: &mut State) -> Result<(Option<Tok>, usize), LexErr> {
    let quote = raw[i0];
    let start_col = i0 + 1;
    let close = paired_close(quote);
    let mut i = i0;
    let mut triple = false;
    if i + 2 < raw.len() && raw[i..i + 3] == [quote, quote, quote] {
        triple = true;
        i += 3;
    } else {
        i += 1;
    }

    let n = raw.len();
    let mut buf: Vec<char> = Vec::new();
    while i < n {
        let ch = raw[i];
        if ch == '\\' {
            buf.extend(escape(raw, i, src, line_no)?);
            i += 2;
            continue;
        }
        if triple {
            if raw[i..].len() >= 3 && raw[i..i + 3] == [quote, quote, quote] {
                i += 3;
                return Ok((Some(Tok::new("STRING", Value::Str(chars_of(&buf)),
                                         line_no, start_col, i, Some(src))), i));
            }
            buf.push(ch);
            i += 1;
        } else {
            if ch == close {
                i += 1;
                return Ok((Some(Tok::new("STRING", Value::Str(chars_of(&buf)),
                                         line_no, start_col, i, Some(src))), i));
            }
            if ch == '\n' {
                break;
            }
            buf.push(ch);
            i += 1;
        }
    }

    // 三引号字符串未在本行闭合：挂起，等后续行
    if triple {
        buf.push('\n');
        st.pending_triple = Some(buf);
        st.pending_quote = quote;
        st.pending_line = line_no;
        st.pending_col = start_col;
        return Ok((None, i));
    }

    Err(LexErr::new("E0104", "字符串没有正常结束，是不是漏了结束引号？",
                    line_no, i + 1, Some(src)))
}

fn scan_fstring(raw: &[char], i0: usize, src: usize, line_no: usize)
    -> Result<(Tok, usize), LexErr>
{
    let start_col = i0 + 1;
    let mut i = i0 + 1;
    let n = raw.len();
    let mut parts: Vec<Part> = Vec::new();
    let mut text: Vec<char> = Vec::new();

    while i < n {
        let ch = raw[i];
        if ch == '`' {
            flush_text(&mut text, &mut parts);
            i += 1;
            return Ok((Tok::new("FSTRING", Value::Parts(parts), line_no,
                                start_col, i, Some(src)), i));
        }
        if ch == '\\' {
            text.extend(escape(raw, i, src, line_no)?);
            i += 2;
            continue;
        }
        if ch == '{' {
            if i + 1 < n && raw[i + 1] == '{' {
                text.push('{');
                i += 2;
                continue;
            }
            flush_text(&mut text, &mut parts);
            i += 1;
            let mut depth = 1i32;
            let expr_start = i;
            while i < n {
                let c = raw[i];
                if c == '{' {
                    depth += 1;
                } else if c == '}' {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                i += 1;
            }
            if i >= n || depth != 0 {
                return Err(LexErr::new(
                    "E0104", "插值表达式没有闭合，是不是漏了「}」？", line_no,
                    start_col, Some(src)));
            }
            parts.push(Part::Expr(chars_of(&raw[expr_start..i])));
            i += 1;
            continue;
        }
        if ch == '}' {
            if i + 1 < n && raw[i + 1] == '}' {
                text.push('}');
                i += 2;
                continue;
            }
            text.push(ch);
            i += 1;
            continue;
        }
        text.push(ch);
        i += 1;
    }

    flush_text(&mut text, &mut parts);
    Err(LexErr::new("E0104", "插值字符串没有正常结束，是不是漏了反引号？",
                    line_no, i + 1, Some(src)))
}

fn flush_text(text: &mut Vec<char>, parts: &mut Vec<Part>) {
    if !text.is_empty() {
        parts.push(Part::Text(chars_of(text)));
        text.clear();
    }
}

/// 继续扫描跨行三引号。返回 `(token, 剩余行 token, 是否闭合)`。
fn continue_triple(raw: &[char], src: usize, line_no: usize, st: &mut State)
    -> Result<(Option<Tok>, Vec<Tok>, bool), LexErr>
{
    let mut buf = st.pending_triple.take().expect("调用前必有挂起内容");
    let quote = st.pending_quote;
    let mut i = 0usize;
    let n = raw.len();
    while i < n {
        let ch = raw[i];
        if ch == '\\' {
            buf.extend(escape(raw, i, src, line_no)?);
            i += 2;
            continue;
        }
        if starts_with_at(raw, i, &[quote, quote, quote]) {
            i += 3;
            let start_line = st.pending_line;
            let start_col = st.pending_col;
            let tok = Tok::new("STRING", Value::Str(chars_of(&buf)), start_line,
                               start_col, i, Some(src));
            let rest = tokenize_line_impl(raw, i, src, line_no, st)?;
            return Ok((Some(tok), rest, true));
        }
        buf.push(ch);
        i += 1;
    }
    // 仍未闭合：把本行内容（含换行）并入
    buf.push('\n');
    st.pending_triple = Some(buf);
    Ok((None, Vec::new(), false))
}

// ---------------------------------------------------------------------------
// 数字 / 词
// ---------------------------------------------------------------------------

fn scan_number(raw: &[char], i0: usize, src: usize, line_no: usize) -> (Tok, usize) {
    let n = raw.len();
    let mut i = i0;
    while i < n && is_digit(raw[i]) {
        i += 1;
    }
    if i < n && raw[i] == '.' {
        i += 1;
        while i < n && is_digit(raw[i]) {
            i += 1;
        }
    }
    if i < n && (raw[i] == 'e' || raw[i] == 'E') {
        let mut j = i + 1;
        if j < n && (raw[j] == '+' || raw[j] == '-') {
            j += 1;
        }
        if j < n && is_digit(raw[j]) {
            i = j;
            while i < n && is_digit(raw[i]) {
                i += 1;
            }
        }
    }
    let text: String = raw[i0..i].iter().collect();
    let value = if text.contains('.') || text.contains('e') || text.contains('E') {
        Value::Float(text.parse::<f64>().unwrap_or(f64::NAN))
    } else {
        match text.parse::<i64>() {
            Ok(v) => Value::Int(v),
            Err(_) => Value::Big(text.clone()),
        }
    };
    (Tok::new("NUMBER", value, line_no, i0 + 1, i, Some(src)), i)
}

fn scan_word(raw: &[char], i0: usize, src: usize, line_no: usize)
    -> Result<(Tok, usize), LexErr>
{
    let n = raw.len();
    let mut i = i0;
    while i < n && is_word_char(raw[i]) {
        i += 1;
    }
    let word = &raw[i0..i];

    let pure_cjk = word.iter().all(|&c| is_cjk(c));

    if pure_cjk {
        let word_s = chars_of(word);
        if is_keyword(&word_s) {
            return Ok((Tok::new("KEYWORD", Value::Str(word_s), line_no, i0 + 1, i,
                                Some(src)), i));
        }
        // 以「多字关键字」为前缀 → 粘连错误
        if let Some(kw) = glue_hit(&word_s) {
            let klen = kw.chars().count();
            let mut e = LexErr::new(
                "E0106",
                format!("「{}」被当成了一个名字，但它的开头是关键字「{}」",
                        word_s, kw),
                line_no, i0 + 1, Some(src));
            e.hint = Some(format!(
                "你是不是想写「{} {}」？关键字和后面的内容之间要留空格",
                kw, &word_s[kw.len()..]));
            e.underline = Some((i0 + 1, i0 + klen));
            return Err(e);
        }
        return Ok((Tok::new("NAME", Value::Str(word_s), line_no, i0 + 1, i,
                            Some(src)), i));
    }

    // 混合词 / ASCII 词：一律标识符
    Ok((Tok::new("NAME", Value::Str(chars_of(word)), line_no, i0 + 1, i,
                 Some(src)), i))
}
