//! 基石代码格式化器 —— Python 侧 `jishi/formatter.py`（M21.1）的 Rust 移植（R7.1-d）。
//!
//! 逐字对齐 `jishi 格式化`（文本输出 + 退出码），由 `tests/test_m89_format_rust.py`
//! 对拍（全语料逐字节）+ **幂等**判据钉住。
//!
//! 设计取舍（与 Python 侧一一对应，见 `formatter.py` 顶注）：
//! - **保留注释**：词法器为语法分析丢了 `#` 注释，格式化器自己重建「代码/注释」边界；
//! - **保留字面量原样**：token 文本按**列切片**取回原文（引号风格 / 全角 / 转义原样）；
//! - **不拆行、不并行**：只做「缩进归一」与「词间间距归一」，行结构不变 → 语义必然一致；
//! - **幂等**：格式化结果再格式化一次，输出不变。
//!
//! ⚠️ **按列切片**是这整件事的钥匙：`tokenizer::tokenize_line` 返回的 `col`/`end_col`
//! 落**原文坐标**（不因全角归一化而错位 —— 归一化是一对一的）。OP 的区间是
//! `[col-1, end_col-1)`，其余 token 是 `[col-1, end_col)`（历史约定不一致，见下）。

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use jishi_frontend::tokenizer::{self, LexErr, Tok, Value};

use crate::{render_error_with, JishiError};

/// 紧跟其后不留空格的符号：`) ] } , . :`
const NO_SPACE_BEFORE: &[&str] = &[")", "]", "}", ",", ".", ":"];
/// 其后不留空格的符号：`( [ { .`
const NO_SPACE_AFTER: &[&str] = &["(", "[", "{", "."];
/// 二元运算符（两侧都留空格）
const BINARY_OPS: &[&str] = &[
    "+", "-", "*", "/", "//", "%", "**", "^",
    "==", "!=", "<", ">", "<=", ">=",
    "=", "+=", "-=", "*=", "/=", "//=", "%=", "**=",
];
/// 可能是「正负号」的符号（按上下文判定是一元还是二元）
const SIGN_OPS: &[&str] = &["-", "+"];
/// 赋值运算符：后面跟的 `*` 是解包星号（M25）
const ASSIGN_OPS: &[&str] = &["=", "+=", "-=", "*=", "/=", "//=", "%=", "**="];
/// 这些关键字后面跟的是表达式，所以紧随的 `-`/`+` 是一元号
const VALUE_KEYWORDS: &[&str] = &["真", "假", "空"];

/// 字符串引号配对（与词法器保持一致）
fn paired(ch: char) -> char {
    match ch {
        '“' => '”',
        '‘' => '’',
        '「' => '」',
        '『' => '』',
        _ => ch,
    }
}

/// 支持三引号形式的引号字符（与 Python `_TRIPLE_QUOTES` 同集合）。
fn is_triple_quote(ch: char) -> bool {
    matches!(ch, '"' | '\'' | '“' | '‘' | '「' | '『')
}

/// `cs[i..i+3]` 是否三个都是 `ch`。
fn starts_with3(cs: &[char], i: usize, ch: char) -> bool {
    i + 3 <= cs.len() && cs[i] == ch && cs[i + 1] == ch && cs[i + 2] == ch
}

/// 从 `from` 起找「三个连续 `ch`」的起点（对应 Python 的 `line.find(ch*3, from)`）。
fn find_seq3(cs: &[char], from: usize, ch: char) -> Option<usize> {
    let n = cs.len();
    if n < 3 {
        return None;
    }
    let mut i = from;
    while i + 3 <= n {
        if cs[i] == ch && cs[i + 1] == ch && cs[i + 2] == ch {
            return Some(i);
        }
        i += 1;
    }
    None
}

/// 某 token 的兜底文本（切片为空时才用；NUMBER 之类基本走不到）。
fn token_text(t: &Tok) -> String {
    match &t.value {
        Value::Str(s) => s.clone(),
        Value::Int(i) => i.to_string(),
        Value::Big(s) => s.clone(),
        Value::Float(f) => format!("{f}"),
        Value::Parts(_) | Value::None => String::new(),
    }
}

// ---------------------------------------------------------------------------
// 缩进层级
// ---------------------------------------------------------------------------

/// 用缩进 token 算出每一「代码行」的缩进层级（0 为顶层）。
fn line_levels(source: &str, filename: &str) -> Result<HashMap<usize, i64>, LexErr> {
    let out = tokenizer::tokenize(source, filename)?;
    let mut levels: HashMap<usize, i64> = HashMap::new();
    let mut level: i64 = 0;
    for tok in &out.tokens {
        match tok.kind {
            "INDENT" => level += 1,
            "DEDENT" => level = (level - 1).max(0),
            "NEWLINE" | "EOF" => {}
            _ => {
                levels.entry(tok.line).or_insert(level);
            }
        }
    }
    Ok(levels)
}

/// 缩进文本的宽度（tab 按 4 折算，全角空格等按 2 —— 与词法器一致）。
fn indent_width(lead: &str) -> i64 {
    let mut w: i64 = 0;
    for ch in lead.chars() {
        if ch == '\t' {
            w += 4 - (w % 4);
        } else if ch == ' ' {
            w += 1;
        } else {
            w += 2;
        }
    }
    w
}

/// 由代码行建立「原始缩进宽度 → 层级」映射，供注释行归位。
fn width_level_map(lines: &[String], levels: &HashMap<usize, i64>) -> HashMap<i64, i64> {
    let mut m: HashMap<i64, i64> = HashMap::new();
    for (i, raw) in lines.iter().enumerate() {
        let lv = match levels.get(&(i + 1)) {
            Some(v) => *v,
            None => continue,
        };
        let lead_len = raw.chars().take_while(|c| c.is_whitespace()).count();
        let lead: String = raw.chars().take(lead_len).collect();
        m.entry(indent_width(&lead)).or_insert(lv);
    }
    m
}

/// 缩进宽度折算层级：精确命中优先，否则取不超过它的最大已知宽度。
fn level_of_width(w: i64, m: &HashMap<i64, i64>) -> i64 {
    if let Some(v) = m.get(&w) {
        return *v;
    }
    let mut best: Option<i64> = None;
    for &known in m.keys() {
        if known <= w && best.map_or(true, |b| known > b) {
            best = Some(known);
        }
    }
    match best {
        Some(b) => m[&b],
        None => 0,
    }
}

// ---------------------------------------------------------------------------
// 行内扫描（注释边界 / 括号净增量 / 跨行三引号状态）
// ---------------------------------------------------------------------------

/// 扫描一行的字符串状态，返回 `(行末是否仍在三引号内, 引号字符)`。
///
/// 只关心「跨行三引号字符串」——这类行必须原样保留，不能重排空格。
/// 行内 `#` 之后视为注释，不再扫描。
fn scan_string_state(line: &str, mut in_triple: bool, mut quote: char) -> (bool, char) {
    let cs: Vec<char> = line.chars().collect();
    let n = cs.len();
    let mut i = 0usize;
    while i < n {
        if in_triple {
            if starts_with3(&cs, i, quote) {
                in_triple = false;
                quote = '\0';
                i += 3;
                continue;
            }
            i += 1;
            continue;
        }
        let ch = cs[i];
        if ch == '#' {
            break; // 注释：本行后面不再是代码
        }
        if ch == '\\' {
            i += 2;
            continue;
        }
        if is_triple_quote(ch) {
            if starts_with3(&cs, i, ch) {
                in_triple = true;
                quote = ch;
                i += 3;
                continue;
            }
            // 普通字符串：跳到配对引号
            let close = paired(ch);
            i += 1;
            while i < n {
                if cs[i] == '\\' {
                    i += 2;
                    continue;
                }
                if cs[i] == close {
                    i += 1;
                    break;
                }
                i += 1;
            }
            continue;
        }
        i += 1;
    }
    (in_triple, quote)
}

/// 扫一行，返回 `(注释起始下标或 None, 括号净增量)`。
///
/// **必须走状态机扫，不能用 `line.find("#")`**：字符串里的 `#` 不是注释
/// （M38 B4 实测：`令 甲 = "含 # 号"` 会被从字符串中间劈开，格式化直接报
/// 「字符串没有正常结束」）。同一个扫描器顺手算括号净增量，供续行判定用。
#[allow(clippy::type_complexity)]
fn scan_code(line: &str) -> (Option<usize>, i64) {
    let cs: Vec<char> = line.chars().collect();
    let n = cs.len();
    let mut i = 0usize;
    let mut delta: i64 = 0;
    while i < n {
        let ch = cs[i];
        if ch == '#' {
            return (Some(i), delta);
        }
        if ch == '\\' {
            i += 2;
            continue;
        }
        if ch == '`' {
            // 反引号文本插值（M7）
            i += 1;
            while i < n && cs[i] != '`' {
                i += if cs[i] == '\\' { 2 } else { 1 };
            }
            i += 1;
            continue;
        }
        if is_triple_quote(ch) {
            if starts_with3(&cs, i, ch) {
                match find_seq3(&cs, i + 3, ch) {
                    Some(p) => {
                        i = p + 3;
                        continue;
                    }
                    None => break, // 跨行三引号：本行到末尾都是字符串
                }
            }
            let close = paired(ch);
            i += 1;
            while i < n {
                if cs[i] == '\\' {
                    i += 2;
                    continue;
                }
                if cs[i] == close {
                    break;
                }
                i += 1;
            }
            i += 1;
            continue;
        }
        if ch == '(' || ch == '[' || ch == '{' {
            delta += 1;
        } else if ch == ')' || ch == ']' || ch == '}' {
            delta -= 1;
        }
        i += 1;
    }
    (None, delta)
}

/// 把一行拆成「代码」与「注释」两段（没有注释时第二段为空串）。
fn split_comment(body: &str) -> (String, String) {
    match scan_code(body).0 {
        None => (body.to_string(), String::new()),
        Some(idx) => {
            let cs: Vec<char> = body.chars().collect();
            let code: String = cs[..idx].iter().collect();
            let comment: String = cs[idx..].iter().collect();
            (code, comment.trim_end().to_string())
        }
    }
}

// ---------------------------------------------------------------------------
// 间距归一
// ---------------------------------------------------------------------------

/// 把一行代码按统一间距规则重新拼接（token 文本仍取自原文切片）。
fn respacify(code: &str) -> Result<String, LexErr> {
    let toks = tokenizer::tokenize_line(code, 1)?;
    if toks.is_empty() {
        return Ok(code.trim().to_string());
    }
    let cs: Vec<char> = code.chars().collect();

    let mut out = String::new();
    let mut first = true;
    let mut prev_norm: Option<String> = None;
    let mut prev_type: Option<&'static str> = None;
    let mut prev_unary = false;
    let mut prev_unpack_star = false;
    let mut prev_value = false;
    let mut prev_wide_colon = false;
    let mut brackets: Vec<char> = Vec::new();

    for t in &toks {
        let norm: Option<String> = match t.kind {
            "OP" | "KEYWORD" => match &t.value {
                Value::Str(s) => Some(s.clone()),
                _ => None,
            },
            _ => None, // 名字 / 数字 / 字符串：不参与符号间距规则
        };
        let norm_str = norm.as_deref();

        // ⚠️ 词法器里 OP 的 end_col 比其它 token 多 1（历史约定不一致）：
        // OP 的区间是 [col-1, end_col-1)，其余 token 是 [col-1, end_col)。
        let (mut lo, hi) = if t.kind == "OP" {
            (t.col.saturating_sub(1), t.end_col.saturating_sub(1))
        } else {
            (t.col.saturating_sub(1), t.end_col)
        };
        let hi = hi.min(cs.len());
        lo = lo.min(hi);
        let mut text: String = if hi > lo {
            cs[lo..hi].iter().collect()
        } else {
            String::new()
        };
        if text.is_empty() {
            text = token_text(t);
        }

        let need_space = if first {
            false
        } else if norm_str.map_or(false, |x| NO_SPACE_BEFORE.contains(&x)) {
            false
        } else if matches!(norm_str, Some("(") | Some("[")) && prev_value {
            false // 调用/下标：f(x)、甲[0] 紧贴
        } else if norm_str == Some("(") && prev_norm.as_deref() == Some("函数") {
            // 匿名函数（M24.4）：`函数(x)：x * 2` 的括号紧贴关键字
            false
        } else if prev_norm.as_deref().map_or(false, |x| NO_SPACE_AFTER.contains(&x)) {
            false
        } else if prev_unary {
            false
        } else if prev_unpack_star {
            // 星号解包（M25）：*参数 / **选项 / 令 甲, *余 —— 星号紧贴名字
            false
        } else if prev_wide_colon {
            false // 全角冒号自带间距，后面不再补空格
        } else if prev_norm.as_deref() == Some(":")
            && !brackets.is_empty() && *brackets.last().unwrap() == '['
        {
            false // 切片 a[1:3] 内部不留空格
        } else if prev_norm.as_deref() == Some(":") && brackets.is_empty() {
            false // 块首冒号只出现在行尾，保险
        } else {
            true
        };

        if need_space {
            out.push(' ');
        }
        out.push_str(&text);

        // 记录括号栈（决定 `:` 是切片还是字典）
        if let Some(nx) = norm_str {
            match nx {
                "(" | "[" | "{" => brackets.push(nx.chars().next().unwrap_or('\0')),
                ")" | "]" | "}" => {
                    brackets.pop();
                }
                _ => {}
            }
        }

        // 判定当前 token 是否一元正负号
        let mut is_unary = false;
        if norm_str.map_or(false, |x| SIGN_OPS.contains(&x)) {
            if first {
                is_unary = true;
            } else if prev_norm.as_deref().map_or(false, |x| BINARY_OPS.contains(&x))
                || matches!(prev_norm.as_deref(),
                            Some("(") | Some("[") | Some("{") | Some(",") | Some(":"))
            {
                is_unary = true;
            } else if prev_type == Some("KEYWORD")
                && !prev_norm.as_deref().map_or(false, |x| VALUE_KEYWORDS.contains(&x))
            {
                is_unary = true;
            }
        }

        // 判定当前 token 是否是「解包星号」（M25）
        let mut is_unpack_star = false;
        if matches!(norm_str, Some("*") | Some("**")) {
            if first
                || matches!(prev_norm.as_deref(),
                            Some("(") | Some("[") | Some("{") | Some(",") | Some("=") | Some("->"))
            {
                is_unpack_star = true;
            } else if prev_norm.as_deref().map_or(false, |x| ASSIGN_OPS.contains(&x)) {
                is_unpack_star = true;
            }
        }

        prev_wide_colon = norm_str == Some(":") && text == "：";
        prev_type = Some(t.kind);
        prev_unary = is_unary;
        prev_unpack_star = is_unpack_star;
        prev_value = matches!(t.kind, "NAME" | "NUMBER" | "STRING" | "FSTRING")
            || matches!(norm_str, Some(")") | Some("]") | Some("}"))
            || (t.kind == "KEYWORD"
                && norm_str.map_or(false, |x| VALUE_KEYWORDS.contains(&x)));
        first = false;
        prev_norm = norm;
    }

    Ok(out)
}

// ---------------------------------------------------------------------------
// 对外入口
// ---------------------------------------------------------------------------

/// 格式化基石源码，返回新源码。出错时返回**词法错误**（Python 侧就是
/// `format_source` 抛 `JishiError` 的那条路 —— 词法层过不去就没法排版）。
pub fn format_source(source: &str, filename: &str,
                     indent: usize) -> Result<String, LexErr> {
    let src = source.replace("\r\n", "\n").replace('\r', "\n");
    // 开头的 BOM 原样保留（Windows 编辑器常带）：剥掉它参与解析，最后再放回去。
    let (bom, src) = match src.strip_prefix('\u{feff}') {
        Some(rest) => ("\u{feff}", rest.to_string()),
        None => ("", src),
    };
    let mut lines: Vec<String> = src.split('\n').map(|s| s.to_string()).collect();
    while lines.last().map_or(false, |l| l.trim().is_empty()) {
        lines.pop();
    }
    if lines.is_empty() {
        return Ok(bom.to_string());
    }

    let levels = line_levels(&src, filename)?;
    let width_to_level = width_level_map(&lines, &levels);
    let unit = " ".repeat(indent.max(1));

    let mut out: Vec<String> = Vec::new();
    let mut in_block_comment = false;
    let mut in_triple = false;
    let mut triple_quote: char = '\0';
    let mut bracket_depth: i64 = 0;
    let mut prev_level: i64 = 0;

    for (idx, raw) in lines.iter().enumerate() {
        let line_no = idx + 1;
        let stripped = raw.trim();
        let lead_len = raw.chars().take_while(|c| c.is_whitespace()).count();
        let lead: String = raw.chars().take(lead_len).collect();
        // 去掉行首缩进后的内容。**必须在这里算**：括号续行分支与普通代码行分支
        // 都要用它（M38 B4 实测：只在一处算，另一处会拿到上一轮的旧值，输出直接语法错）。
        let body: String = raw.chars().skip(lead_len).collect();

        // ---- 块注释内部：原样保留 ----
        if in_block_comment {
            out.push(raw.trim_end().to_string());
            if raw.contains("-#") {
                in_block_comment = false;
            }
            continue;
        }

        // ---- 三引号字符串内部：原样保留 ----
        if in_triple {
            out.push(raw.trim_end().to_string());
            let (nt, nq) = scan_string_state(raw, in_triple, triple_quote);
            in_triple = nt;
            triple_quote = nq;
            continue;
        }

        // ---- 空行 ----
        if stripped.is_empty() {
            out.push(String::new());
            continue;
        }

        // ---- 块注释起始行：原样保留 ----
        if stripped.starts_with("#-") {
            out.push(raw.trim_end().to_string());
            let tail: String = stripped.chars().skip(2).collect();
            if !tail.contains("-#") {
                in_block_comment = true;
            }
            continue;
        }

        // ---- 本行含三引号（开或闭）：整行原样保留 ----
        let (new_triple, new_quote) = scan_string_state(raw, false, '\0');
        if new_triple || stripped.starts_with("\"\"\"") || stripped.starts_with("'''") {
            out.push(raw.trim_end().to_string());
            in_triple = new_triple;
            triple_quote = new_quote;
            continue;
        }

        // ---- 只有注释的行：按它自己的原始缩进宽度定位层级 ----
        if stripped.starts_with('#') {
            let level = level_of_width(indent_width(&lead), &width_to_level);
            out.push(format!("{}{}", unit.repeat(level.max(0) as usize), stripped));
            continue;
        }

        // ---- 括号内的续行：保留原有相对缩进，但注释也要留住 ----
        if bracket_depth > 0 {
            let (code_part, comment) = split_comment(&body);
            let pieces = if code_part.trim().is_empty() {
                String::new()
            } else {
                respacify(&code_part)?.trim().to_string()
            };
            let line_out = if !pieces.is_empty() {
                let mut s = format!("{lead}{pieces}");
                if !comment.is_empty() {
                    s.push_str("  ");
                    s.push_str(&comment);
                }
                s
            } else {
                format!("{lead}{comment}")
            };
            out.push(line_out.trim_end().to_string());
            bracket_depth = (bracket_depth + scan_code(&body).1).max(0);
            continue;
        }

        // ---- 普通代码行：缩进归一 + 间距归一 ----
        let level = *levels.get(&line_no).unwrap_or(&prev_level);
        prev_level = level;

        let (code, comment) = split_comment(&body);
        let pieces = respacify(&code)?;
        let mut line_out = format!("{}{}", unit.repeat(level.max(0) as usize), pieces);
        if !comment.is_empty() {
            if !pieces.is_empty() {
                line_out.push_str("  ");
                line_out.push_str(&comment);
            } else {
                line_out = format!("{}{}", unit.repeat(level.max(0) as usize), comment);
            }
        }
        out.push(line_out.trim_end().to_string());
        bracket_depth = (bracket_depth + scan_code(&code).1).max(0);
    }

    // 去掉末尾空行，统一以一个换行结束
    while out.last().map_or(false, |l| l.is_empty()) {
        out.pop();
    }
    Ok(format!("{}{}\n", bom, out.join("\n")))
}

/// 把一个词法错误渲染成与 Python `JishiError.render()` **同形**的文本。
///
/// ⚠️ 复用宿主那一份 `render_error_with`（源码框 + `^` + 提示）—— 报错渲染
/// **只有一份实现**，否则「源码行 / `^` / 提示」这些用户可见的细节会漂。
/// `title` 从**错误码表**查（`errcodes::title`）—— 与 Python 的
/// `JishiError.title`（`errors.py` 里按码定义）同一张表。
pub fn render_lex_error(e: &LexErr, filename: &str, source: &str) -> String {
    let title = crate::errcodes::title(e.code).unwrap_or("词法错误");
    let lines: Vec<String> = source.replace("\r\n", "\n")
        .split('\n').map(|s| s.to_string()).collect();
    let err = JishiError {
        type_name: title.to_string(),
        message: e.message.clone(),
        code: Some(e.code.to_string()),
        line: Some(if e.line == 0 { 1 } else { e.line as i64 }),
        col: Some(if e.col == 0 { 1 } else { e.col as i64 }),
        // 多字符下划线（如「关键字粘连」的 `~^`）—— 与 Python 侧同形。
        underline: e.underline.map(|(a, b)| (a as i64, b as i64)),
        hint: e.hint.clone(),
        fix: None,          // 词法错没有替换建议（`fix` 只有解析器会填）
        trace: Vec::new(),
    };
    render_error_with(&err, filename, &|line| {
        if line >= 1 {
            lines.get((line - 1) as usize).cloned()
        } else {
            None
        }
    })
}

/// 自报家底（给漂移检测用）：格式化器的符号集合 → JSON 文本（各表**排好序**）。
///
/// 📌 与 `--dump-errcodes` / `--dump-check-symbols` 同一条流水线（R7.1-b 起）：
/// **Rust 显式表 + 自报家底 + Python 侧逐字段比**。加一个运算符/关键字只改一侧
/// 时，`tests/test_m89_format_rust.py` 会报红 —— 否则要等语料恰好用到它才暴露。
pub fn dump_tables_json() -> String {
    fn arr(items: &[&str]) -> String {
        let mut v: Vec<&str> = items.to_vec();
        v.sort();
        format!("[{}]", v.iter().map(|s| crate::checker::jstr(s))
                .collect::<Vec<_>>().join(","))
    }
    let mut paired: Vec<(char, char)> = vec![
        ('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』'),
    ];
    paired.sort();
    let paired_s = format!("[{}]", paired.iter()
        .map(|(a, b)| format!("[{},{}]", crate::checker::jstr(&a.to_string()),
                              crate::checker::jstr(&b.to_string())))
        .collect::<Vec<_>>().join(","));
    let triple: Vec<&str> = vec!["\"", "'", "“", "‘", "「", "『"];
    format!(
        "{{\n  \"no_space_before\": {},\n  \"no_space_after\": {},\n  \
         \"binary_ops\": {},\n  \"sign_ops\": {},\n  \"assign_ops\": {},\n  \
         \"value_keywords\": {},\n  \"triple_quotes\": {},\n  \"paired\": {}\n}}",
        arr(NO_SPACE_BEFORE), arr(NO_SPACE_AFTER), arr(BINARY_OPS),
        arr(SIGN_OPS), arr(ASSIGN_OPS), arr(VALUE_KEYWORDS), arr(&triple), paired_s,
    )
}

// ---------------------------------------------------------------------------
// 目录处理（对应 Python 的 `cli._format_tree`）
// ---------------------------------------------------------------------------

/// 递归收集目录下的 `.jsh` 文件并**排序**（对应 Python
/// `sorted(root.rglob("*.jsh"))`）。
///
/// ⚠️ 排序口径要对齐 Python 的 `Path` 比较（Windows 上是**不区分大小写**的
/// `os.path.normcase`）：这里按 `display_path` 的小写排，与我们打印的路径同一形态。
pub fn collect_jsh_sorted(root: &Path) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
        let Ok(rd) = std::fs::read_dir(dir) else { return };
        for entry in rd.flatten() {
            let p = entry.path();
            if p.is_dir() {
                walk(&p, out);
            } else if p.extension().and_then(|s| s.to_str()) == Some("jsh")
                && p.is_file()
            {
                out.push(p);
            }
        }
    }
    walk(root, &mut out);
    out.sort_by_key(|p| crate::checker::display_path(p).to_lowercase());
    out
}
