//! 标准库 `文本`（Python 侧 `jishi/stdlib/文本.py`，18 个函数）。
//!
//! 这里要小心两件事：
//!
//! 1. **值 → 文本统一走 `as_text`（`display`）**。Python 侧这一模块原来是
//!    `str(x)`，于是 `文本.补零(真, 3)` 给出 `True`——与 `文本(真)` 的 `真`
//!    不一致（M30 只把 `拼接` / `格式化` 改成 `jishi_repr`，其余是漏的）。
//!    M33 把 Python 侧也统一过来，两侧才逐字节一致。
//! 2. **`居中` 的偏置不是「左一半右一半」**。CPython 的公式是
//!    `left = marg // 2 + (marg & width & 1)`，`'ab'.center(5)` 给 `'  ab '`。
//!    自己写「左边 floor(总/2)」会对不上。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{as_intish, need, need_range, table, R};
use crate::{display, err, JishiError, Val};

/// `str.center` 的偏置公式（CPython 的 `unicode_center_impl`）。
fn center_left(marg: usize, width: usize) -> usize {
    marg / 2 + (marg & width & 1)
}

/// 「宽字符」的码点区间 —— 在等宽终端里占 **2 列**。
///
/// ⚠️ **必须与 `oracle/jishi/stdlib/文本.py` 的 `_宽区间` 逐条一致**
/// （判据 `tests/test_m106_display_width.py` 会当场比对三处实现）。
const WIDE_RANGES: &[(u32, u32)] = &[
    (0x1100, 0x115F),   // 韩文字母
    (0x2E80, 0x303E),   // CJK 部首 + 康熙部首 + CJK 符号与标点
    (0x3041, 0x33FF),   // 平假名 / 片假名 / 注音符号 / CJK 兼容
    (0x3400, 0x4DBF),   // CJK 扩展 A
    (0x4E00, 0x9FFF),   // CJK 基本区
    (0xA000, 0xA4CF),   // 彝文
    (0xAC00, 0xD7A3),   // 韩文音节
    (0xF900, 0xFAFF),   // CJK 兼容表意
    (0xFE30, 0xFE6F),   // CJK 兼容形式
    (0xFF00, 0xFF60),   // 全角 ASCII 变体
    (0xFFE0, 0xFFE6),   // 全角符号（￥ ￦ 等）
    (0x1F300, 0x1F64F), // emoji（多数终端按 2 列）
    (0x1F900, 0x1F9FF), // emoji 补充区
    (0x20000, 0x2FFFD), // CJK 扩展 B 及以后
    (0x30000, 0x3FFFD),
];

/// 「零宽」区间：组合符 / 零宽控制符 —— 不占列。
const ZERO_RANGES: &[(u32, u32)] = &[
    (0x0300, 0x036F), // 组合变音符
    (0x0483, 0x0489), // 组合西里尔符
    (0x200B, 0x200F), // 零宽空格 / 方向标记
    (0xFE00, 0xFE0F), // 变体选择符
];

/// 一个码点占几列。
fn char_width(c: char) -> usize {
    let cp = c as u32;
    if ZERO_RANGES.iter().any(|&(lo, hi)| lo <= cp && cp <= hi) {
        return 0;
    }
    if WIDE_RANGES.iter().any(|&(lo, hi)| lo <= cp && cp <= hi) {
        return 2;
    }
    1
}

/// 这段文本在等宽终端里占几列 —— 一个汉字算 2，组合符算 0。
// M120：`表格.转文本` 要在 tables.rs 里算列宽，所以开放到 crate 内。
pub(crate) fn display_width(s: &str) -> usize {
    s.chars().map(char_width).sum()
}

/// 取「填充字符」：Python 要求恰好一个字符，否则抛错。
fn one_char(v: &Val) -> Result<char, JishiError> {
    let s = super::as_text(v);
    let mut it = s.chars();
    match (it.next(), it.next()) {
        (Some(c), None) => Ok(c),
        _ => Err(err("类型错误", "填充字符必须恰好是一个字符")),
    }
}

fn repeat_char(c: char, n: usize) -> String {
    std::iter::repeat(c).take(n).collect()
}

/// Python 的 `str.isdigit`：只认「十进制数字字符」。
///
/// `char::is_numeric` 不够用——它按 Unicode 的 `Numeric_Type` 判，会把
/// `一` / `Ⅷ` 也算进来，而 Python 的 `'一'.isdigit()` 是 `False`。
/// Rust 标准库没有暴露 `Numeric_Type`，所以这里按「已知的数字区块」列。
fn is_digit_char(c: char) -> bool {
    if c.is_ascii_digit() {
        return true;
    }
    let u = c as u32;
    // 上标/下标数字
    matches!(u, 0x00B2 | 0x00B3 | 0x00B9) || (0x2070..=0x2079).contains(&u) || (0x2080..=0x2089).contains(&u)
        // 带圈数字
        || (0x2460..=0x2473).contains(&u) || u == 0x24EA || (0x2776..=0x2793).contains(&u)
        // 各语系的十进制数字区块（阿拉伯-印度、天城文、泰文、全角…）
        || (0x0660..=0x0669).contains(&u)
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
        || (0x1040..=0x1049).contains(&u)
        || (0x17E0..=0x17E9).contains(&u)
        || (0x1810..=0x1819).contains(&u)
        || (0xFF10..=0xFF19).contains(&u)
        || (0x1D7CE..=0x1D7FF).contains(&u)
}

/// Python 的 `str.isspace`：比 Rust 的 `is_whitespace` 多认 `\x1c`–`\x1f`。
fn is_space_char(c: char) -> bool {
    if c.is_whitespace() {
        return true;
    }
    matches!(c as u32, 0x1C..=0x1F)
}

/// Python 的 `str.isupper`：至少有一个「有大小写的字符」，且全部是大写。
fn is_upper(s: &str) -> bool {
    let mut has_cased = false;
    for c in s.chars() {
        if c.is_uppercase() {
            has_cased = true;
        } else if c.is_lowercase() {
            return false;
        }
    }
    has_cased
}

/// Python 的 `str.islower`。
fn is_lower(s: &str) -> bool {
    let mut has_cased = false;
    for c in s.chars() {
        if c.is_lowercase() {
            has_cased = true;
        } else if c.is_uppercase() {
            return false;
        }
    }
    has_cased
}

/// Python 的 `str.splitlines`：认的换行比 `\n` 多（`\v` `\f` `\x85` `\u2028`…），
/// 并且**结尾的换行不会多出一个空行**。
pub(crate) fn split_lines(s: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        let boundary = match c {
            '\r' => {
                if chars.peek() == Some(&'\n') {
                    chars.next();
                }
                true
            }
            '\n' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}'
            | '\u{2029}' => true,
            _ => false,
        };
        if boundary {
            out.push(std::mem::take(&mut cur));
        } else {
            cur.push(c);
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

// ---------------------------------------------------------------------------
// 格式化（`{}` 占位符 + 常见格式说明符）
// ---------------------------------------------------------------------------

/// 把一个值按格式说明符渲染（`{}` 里 `:` 之后那一段）。
///
/// 支持：`{:.1f}` `{:>8}` `{:<8}` `{:^8}` `{:08.2f}` `{:d}` `{:,}` `{:%}`
/// `{:e}` `{:.3}`（文本截断）。认不出的说明符就退回普通显示——
/// 宁可不格式化，也不要给错。
fn fmt_one(v: &Val, spec: &str) -> String {
    if spec.is_empty() {
        return super::as_text(v);
    }
    // 拆说明符：[[fill]align][sign][#][0][width][,][.prec][type]
    let chars: Vec<char> = spec.chars().collect();
    let mut i = 0;
    let mut fill: Option<char> = None;
    let mut align: Option<char> = None;
    if chars.len() >= 2 && matches!(chars[1], '<' | '>' | '=' | '^') {
        fill = Some(chars[0]);
        align = Some(chars[1]);
        i = 2;
    } else if !chars.is_empty() && matches!(chars[0], '<' | '>' | '=' | '^') {
        align = Some(chars[0]);
        i = 1;
    }
    let sign = if i < chars.len() && matches!(chars[i], '+' | '-' | ' ') {
        let s = chars[i];
        i += 1;
        Some(s)
    } else {
        None
    };
    if i < chars.len() && chars[i] == '#' {
        i += 1;
    }
    let zero = if i < chars.len() && chars[i] == '0' {
        i += 1;
        true
    } else {
        false
    };
    let mut width = 0usize;
    while i < chars.len() && chars[i].is_ascii_digit() {
        width = width * 10 + chars[i].to_digit(10).unwrap() as usize;
        i += 1;
    }
    let comma = if i < chars.len() && chars[i] == ',' {
        i += 1;
        true
    } else {
        false
    };
    let mut prec: Option<usize> = None;
    if i < chars.len() && chars[i] == '.' {
        i += 1;
        let mut p = 0usize;
        while i < chars.len() && chars[i].is_ascii_digit() {
            p = p * 10 + chars[i].to_digit(10).unwrap() as usize;
            i += 1;
        }
        prec = Some(p);
    }
    let ty = if i < chars.len() { Some(chars[i]) } else { None };

    let num = match v {
        Val::Int(n) => *n as f64,
        Val::Float(f) => *f,
        Val::Bool(b) => {
            if *b {
                1.0
            } else {
                0.0
            }
        }
        _ => f64::NAN,
    };

    let mut s = match ty {
        Some('f') | Some('F') => {
            let p = prec.unwrap_or(6);
            format!("{:.*}", p, num)
        }
        Some('d') => format!("{}", num.trunc() as i128),
        Some('e') | Some('E') => {
            let p = prec.unwrap_or(6);
            let t = format!("{:.*e}", p, num);
            if ty == Some('E') {
                t.to_uppercase()
            } else {
                t
            }
        }
        Some('%') => {
            let p = prec.unwrap_or(6);
            format!("{:.*}%", p, num * 100.0)
        }
        _ => {
            let mut base = super::as_text(v);
            if let Some(p) = prec {
                base = base.chars().take(p).collect();
            }
            base
        }
    };

    if comma {
        // 只给整数部分加千分位（Python 的 `,` 也是这个效果）
        let (sign_part, body) = if let Some(rest) = s.strip_prefix('-') {
            ("-", rest.to_string())
        } else {
            ("", s.clone())
        };
        let (int_part, frac) = match body.find('.') {
            Some(k) => (body[..k].to_string(), body[k..].to_string()),
            None => (body.clone(), String::new()),
        };
        if int_part.chars().all(|c| c.is_ascii_digit()) && !int_part.is_empty() {
            let mut grouped = String::new();
            let d: Vec<char> = int_part.chars().collect();
            for (k, c) in d.iter().enumerate() {
                if k > 0 && (d.len() - k) % 3 == 0 {
                    grouped.push(',');
                }
                grouped.push(*c);
            }
            s = format!("{sign_part}{grouped}{frac}");
        }
    }

    if sign == Some('+') && !s.starts_with('-') && !s.starts_with('+') {
        s.insert(0, '+');
    }

    let slen = s.chars().count();
    if width > slen {
        let f = fill.unwrap_or(if zero { '0' } else { ' ' });
        let al = align.unwrap_or(if zero { '=' } else { '>' });
        let total = width - slen;
        s = match al {
            '<' => format!("{s}{}", repeat_char(f, total)),
            '^' => {
                let left = total / 2;
                format!(
                    "{}{}{}",
                    repeat_char(f, left),
                    s,
                    repeat_char(f, total - left)
                )
            }
            '=' => {
                // 补在符号与数字之间：`-` 或 `+` 留在最前面
                let (prefix, rest) = if let Some(r) = s.strip_prefix('-') {
                    ("-", r.to_string())
                } else if let Some(r) = s.strip_prefix('+') {
                    ("+", r.to_string())
                } else {
                    ("", s.clone())
                };
                format!("{prefix}{}{rest}", repeat_char(f, total))
            }
            _ => format!("{}{s}", repeat_char(f, total)),
        };
    }
    s
}

/// Python 的 `str.format` 子集：`{}` 顺序取值、`{0}` 按下标取值，
/// `{{` / `}}` 是转义。
fn format_template(tpl: &str, args: &[Val]) -> String {
    let mut out = String::new();
    let chars: Vec<char> = tpl.chars().collect();
    let mut i = 0;
    let mut auto = 0usize;
    while i < chars.len() {
        let c = chars[i];
        if c == '{' {
            if i + 1 < chars.len() && chars[i + 1] == '{' {
                out.push('{');
                i += 2;
                continue;
            }
            // 找到配对的 `}`
            let mut j = i + 1;
            let mut depth = 1;
            while j < chars.len() && depth > 0 {
                if chars[j] == '{' {
                    depth += 1;
                } else if chars[j] == '}' {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                j += 1;
            }
            if j >= chars.len() {
                out.push(c); // 没配对的 `{` 原样输出
                i += 1;
                continue;
            }
            let inner: String = chars[i + 1..j].iter().collect();
            let (idx_part, spec) = match inner.find(':') {
                Some(k) => (inner[..k].to_string(), inner[k + 1..].to_string()),
                None => (inner.clone(), String::new()),
            };
            let arg = if idx_part.is_empty() {
                let a = args.get(auto).cloned();
                auto += 1;
                a
            } else {
                idx_part
                    .trim()
                    .parse::<usize>()
                    .ok()
                    .and_then(|k| args.get(k).cloned())
            };
            match arg {
                Some(v) => out.push_str(&fmt_one(&v, &spec)),
                None => out.push_str(&inner), // 没参数就原样留着，别悄悄吞掉
            }
            i = j + 1;
        } else if c == '}' && i + 1 < chars.len() && chars[i + 1] == '}' {
            out.push('}');
            i += 2;
        } else {
            out.push(c);
            i += 1;
        }
    }
    out
}

/// 定点：把数字写成固定小数位的文本，**四舍五入（远离零）**。
///
/// 🔴 为什么不用 `format!("{:.N}", x)`：正好一半时三个宿主结论不同 ——
/// `4.25` 保留 1 位，Python 的 `format` 给 `4.2`（半偶）、JS 的 `toFixed` 给
/// `4.3`。而「三执行器语义一致」是硬约束，所以这里**在十进制字符串上进位**：
/// 先取 `display`（这个显示口径本来就已对拍），再按位四舍五入。
fn 定点(v: &Val, 位: usize) -> String {
    let s = display(v);
    if s.contains('e') || s.contains('E') {
        return s;                       // 科学计数法少见，原样给（见文档的已知边界）
    }
    let 负 = s.starts_with('-');
    let 纯 = if 负 { &s[1..] } else { s.as_str() };
    let (整数部, 小数部) = match 纯.find('.') {
        Some(i) => (&纯[..i], &纯[i + 1..]),
        None => (纯, ""),
    };
    let 整数部 = if 整数部.is_empty() { "0" } else { 整数部 };
    let mut 补足 = String::from(小数部);
    while 补足.chars().count() < 位 {
        补足.push('0');
    }
    let 保留: String = 补足.chars().take(位).collect();
    let 丢弃: Vec<char> = 补足.chars().skip(位).collect();
    let mut 结果 = if 位 > 0 {
        format!("{整数部}.{保留}")
    } else {
        整数部.to_string()
    };
    if 丢弃.first().map(|c| *c >= '5').unwrap_or(false) {
        结果 = 进一(&结果);
    }
    if 负 {
        format!("-{结果}")
    } else {
        结果
    }
}

/// 十进制字符串加一（带小数点）：`9.9` → `10.0`。
fn 进一(s: &str) -> String {
    let mut 位们: Vec<char> = s.chars().collect();
    let mut 走 = 位们.len() as isize - 1;
    while 走 >= 0 {
        let i = 走 as usize;
        match 位们[i] {
            '.' => 走 -= 1,
            '9' => {
                位们[i] = '0';
                走 -= 1;
            }
            c => {
                位们[i] = char::from_u32(c as u32 + 1).unwrap_or(c);
                return 位们.into_iter().collect();
            }
        }
    }
    format!("1{s}")
}

/// 按**显示宽度**补空格（汉字算 2 列，与 `左对齐`/`右对齐` 同口径）。
fn 补(s: &str, 宽: usize, 靠右: bool) -> String {
    let 差 = 宽.saturating_sub(display_width(s));
    if 差 == 0 {
        return s.to_string();
    }
    let 空格 = " ".repeat(差);
    if 靠右 {
        format!("{空格}{s}")
    } else {
        format!("{s}{空格}")
    }
}

/// `文本.对齐表`（M142）：一列值 → 一行；列表的列表 → 一张对齐的表。
fn 对齐表(args: &[Val]) -> R {
    need_range("对齐表", args, 1, 2)?;
    if let Val::Str(_) | Val::Bytes(_) = &args[0] {
        return Err(err(
            "类型错误",
            "「对齐表」要传一列值或一张表（列表），不能传文本",
        ));
    }
    let 原始 = super::to_vec(&args[0])
        .map_err(|_| err("类型错误", "「对齐表」要传一列值或一张表（列表）"))?;
    if 原始.is_empty() {
        return Err(err("值错误", "「对齐表」要至少有一个值，现在是空的"));
    }
    let 小数位 = match args.get(1) {
        None => None,
        Some(Val::Int(n)) if *n >= 0 && *n <= 10 => Some(*n as usize),
        Some(Val::Int(n)) => {
            return Err(err(
                "值错误",
                format!("「对齐表」的小数位要在 0 到 10 之间（给的是 {n}）"),
            ))
        }
        Some(v) => {
            return Err(err(
                "类型错误",
                format!("「对齐表」的小数位要是不给或整数，给的是 {}", display(v)),
            ))
        }
    };

    let 多行 = matches!(原始[0], Val::List(_));
    let 表: Vec<Vec<Val>> = if 多行 {
        let mut t: Vec<Vec<Val>> = Vec::with_capacity(原始.len());
        for 行 in &原始 {
            t.push(super::to_vec(行)?);
        }
        let 宽 = t[0].len();
        for (i, 行) in t.iter().enumerate() {
            if 行.len() != 宽 {
                return Err(err(
                    "值错误",
                    format!(
                        "「对齐表」每行的列数要相同：第 1 行有 {宽} 列，第 {} 行有 {} 列",
                        i + 1,
                        行.len()
                    ),
                ));
            }
        }
        t
    } else {
        vec![原始]
    };

    let 格子: Vec<Vec<String>> = 表
        .iter()
        .map(|行| {
            行.iter()
                .map(|v| match (小数位, v) {
                    (Some(n), Val::Int(_)) | (Some(n), Val::Float(_)) => 定点(v, n),
                    _ => display(v),
                })
                .collect()
        })
        .collect();

    let 列数 = 格子[0].len();
    let 列宽: Vec<usize> = (0..列数)
        .map(|j| 格子.iter().map(|r| display_width(&r[j])).max().unwrap_or(0))
        .collect();
    let 数字列: Vec<bool> = (0..列数)
        .map(|j| {
            表.iter().all(|r| {
                matches!(r[j], Val::Int(_) | Val::Float(_))
            })
        })
        .collect();

    let mut 行们 = Vec::with_capacity(格子.len());
    for 行 in &格子 {
        let 片: Vec<String> = 行
            .iter()
            .enumerate()
            .map(|(j, 格)| 补(格, 列宽[j], 数字列[j]))
            .collect();
        行们.push(片.join("  ").trim_end().to_string());
    }
    Ok(Val::Str(行们.join("\n")))
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "文本";
        "拼接" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("拼接", args, 1, 2)?;
            let items = super::to_vec(&args[0])?;
            let sep = if args.len() > 1 { super::as_text(&args[1]) } else { String::new() };
            let parts: Vec<String> = items.iter().map(super::as_text).collect();
            Ok(Val::Str(parts.join(&sep)))
        },
        "居中" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("居中", args, 2, 3)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?.max(0) as usize;
            let fill = if args.len() > 2 { one_char(&args[2])? } else { ' ' };
            let n = display_width(&s);
            if n >= w {
                return Ok(Val::Str(s));
            }
            let marg = w - n;
            let left = center_left(marg, w);
            Ok(Val::Str(format!(
                "{}{}{}",
                repeat_char(fill, left),
                s,
                repeat_char(fill, marg - left)
            )))
        },
        "对齐表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            对齐表(args)
        },
        "显示宽度" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("显示宽度", args, 1)?;
            Ok(Val::Int(display_width(&super::as_text(&args[0])) as i128))
        },
        "左对齐" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("左对齐", args, 2, 3)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?.max(0) as usize;
            let fill = if args.len() > 2 { one_char(&args[2])? } else { ' ' };
            let n = display_width(&s);
            Ok(Val::Str(if n >= w {
                s
            } else {
                format!("{s}{}", repeat_char(fill, w - n))
            }))
        },
        "右对齐" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("右对齐", args, 2, 3)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?.max(0) as usize;
            let fill = if args.len() > 2 { one_char(&args[2])? } else { ' ' };
            let n = display_width(&s);
            Ok(Val::Str(if n >= w {
                s
            } else {
                format!("{}{s}", repeat_char(fill, w - n))
            }))
        },
        "补零" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("补零", args, 2)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?.max(0) as usize;
            let n = s.chars().count();
            if n >= w {
                return Ok(Val::Str(s));
            }
            // Python 的 `zfill` 把 0 补在**符号之后**
            let pad = repeat_char('0', w - n);
            if let Some(rest) = s.strip_prefix('-') {
                Ok(Val::Str(format!("-{pad}{rest}")))
            } else if let Some(rest) = s.strip_prefix('+') {
                Ok(Val::Str(format!("+{pad}{rest}")))
            } else {
                Ok(Val::Str(format!("{pad}{s}")))
            }
        },
        "重复" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("重复", args, 2)?;
            let s = super::as_text(&args[0]);
            let n = as_intish(&args[1])?;
            Ok(Val::Str(if n <= 0 { String::new() } else { s.repeat(n as usize) }))
        },
        "计数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("计数", args, 2)?;
            let s = super::as_text(&args[0]);
            let sub = super::as_text(&args[1]);
            if sub.is_empty() {
                return Ok(Val::Int(s.chars().count() as i128 + 1));
            }
            // 不重叠计数：`'aaa'.count('aa')` 是 1
            let mut count = 0i128;
            let mut from = 0usize;
            while let Some(k) = s[from..].find(&sub) {
                count += 1;
                from += k + sub.len();
                if from > s.len() {
                    break;
                }
            }
            Ok(Val::Int(count))
        },
        "是数字" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("是数字", args, 1)?;
            let s = super::as_text(&args[0]);
            Ok(Val::Bool(!s.is_empty() && s.chars().all(is_digit_char)))
        },
        "是字母" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("是字母", args, 1)?;
            let s = super::as_text(&args[0]);
            Ok(Val::Bool(!s.is_empty() && s.chars().all(|c| c.is_alphabetic())))
        },
        "是空白" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("是空白", args, 1)?;
            let s = super::as_text(&args[0]);
            Ok(Val::Bool(!s.is_empty() && s.chars().all(is_space_char)))
        },
        "是大写" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("是大写", args, 1)?;
            Ok(Val::Bool(is_upper(&super::as_text(&args[0]))))
        },
        "是小写" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("是小写", args, 1)?;
            Ok(Val::Bool(is_lower(&super::as_text(&args[0]))))
        },
        "首字母大写" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("首字母大写", args, 1)?;
            let s = super::as_text(&args[0]);
            let mut out = String::new();
            for (k, c) in s.chars().enumerate() {
                if k == 0 {
                    out.extend(c.to_uppercase());
                } else {
                    out.extend(c.to_lowercase());
                }
            }
            Ok(Val::Str(out))
        },
        "去前缀" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("去前缀", args, 2)?;
            let s = super::as_text(&args[0]);
            let p = super::as_text(&args[1]);
            Ok(Val::Str(match s.strip_prefix(p.as_str()) {
                Some(rest) => rest.to_string(),
                None => s,
            }))
        },
        "去后缀" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("去后缀", args, 2)?;
            let s = super::as_text(&args[0]);
            let p = super::as_text(&args[1]);
            Ok(Val::Str(match s.strip_suffix(p.as_str()) {
                Some(rest) => rest.to_string(),
                None => s,
            }))
        },
        "格式化" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("格式化", args, 1, usize::MAX)?;
            let tpl = super::as_text(&args[0]);
            // 布尔与空先转成中文文本，免得 `格式化("{}", 真)` 给出 `True`；
            // 数字保持原样，好让 `{:.1f}` 这类说明符仍然生效（对齐 Python 侧）
            let rest: Vec<Val> = args[1..]
                .iter()
                .map(|v| match v {
                    Val::Bool(_) | Val::None_ => Val::Str(super::as_text(v)),
                    other => other.clone(),
                })
                .collect();
            Ok(Val::Str(format_template(&tpl, &rest)))
        },
        "切成三段" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("切成三段", args, 2)?;
            let s = super::as_text(&args[0]);
            let sep = super::as_text(&args[1]);
            let parts: Vec<Val> = match s.find(&sep) {
                Some(k) if !sep.is_empty() => vec![
                    Val::Str(s[..k].to_string()),
                    Val::Str(sep.clone()),
                    Val::Str(s[k + sep.len()..].to_string()),
                ],
                _ => vec![Val::Str(s), Val::Str(String::new()), Val::Str(String::new())],
            };
            Ok(crate::list_new(parts))
        },
        "按行拆分" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("按行拆分", args, 1)?;
            let s = super::as_text(&args[0]);
            let items: Vec<Val> = split_lines(&s).into_iter().map(Val::Str).collect();
            Ok(crate::list_new(items))
        },
        // M51 补：这两个是 M50 第一批加的（「既有模块补缺」），当时只做了
        // Python 侧，宿主**静默缺失** —— 正是这个缺口促成 M51 的漂移检测。
        "填充" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("填充", args, 2, 3)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?.max(0) as usize;
            let f = if args.len() > 2 { super::as_text(&args[2]) } else { " ".to_string() };
            let fchars: Vec<char> = f.chars().collect();
            if fchars.len() != 1 {
                return Err(err("类型错误", "「填充」的填充字符要传一个字符"));
            }
            let n = display_width(&s);
            if n >= w {
                return Ok(Val::Str(s));
            }
            let total = w - n;
            // ⚠️ 居中**不是**「左右各一半」：CPython `str.center` 是
            //    `left = marg // 2 + (marg & width & 1)`（**左多**）。
            let left = center_left(total, w);
            let mut out = String::new();
            for _ in 0..left { out.push(fchars[0]); }
            out.push_str(&s);
            for _ in 0..(total - left) { out.push(fchars[0]); }
            Ok(Val::Str(out))
        },
        "截断" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("截断", args, 2, 3)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?;
            let e = if args.len() > 2 { super::as_text(&args[2]) } else { "…".to_string() };
            if w < 0 {
                return Err(err("类型错误",
                    format!("「截断」的宽度不能是负数，得到了 {}", display(&args[1]))));
            }
            let w = w as usize;
            let chars: Vec<char> = s.chars().collect();
            if chars.len() <= w {
                return Ok(Val::Str(s));
            }
            let ech: Vec<char> = e.chars().collect();
            if w <= ech.len() {
                return Ok(Val::Str(ech[..w].iter().collect()));
            }
            let mut out: String = chars[..w - ech.len()].iter().collect();
            out.push_str(&e);
            Ok(Val::Str(out))
        },
    }
}
