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

use std::collections::HashMap;

use super::{as_intish, need, need_range, table, R};
use crate::{display, err, JishiError, Val, VM};

/// `str.center` 的偏置公式（CPython 的 `unicode_center_impl`）。
fn center_left(marg: usize, width: usize) -> usize {
    marg / 2 + (marg & width & 1)
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

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "文本";
        "拼接" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("拼接", args, 1, 2)?;
            let items = super::to_vec(&args[0])?;
            let sep = if args.len() > 1 { super::as_text(&args[1]) } else { String::new() };
            let parts: Vec<String> = items.iter().map(super::as_text).collect();
            Ok(Val::Str(parts.join(&sep)))
        },
        "居中" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("居中", args, 2, 3)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?.max(0) as usize;
            let fill = if args.len() > 2 { one_char(&args[2])? } else { ' ' };
            let n = s.chars().count();
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
        "左对齐" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("左对齐", args, 2, 3)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?.max(0) as usize;
            let fill = if args.len() > 2 { one_char(&args[2])? } else { ' ' };
            let n = s.chars().count();
            Ok(Val::Str(if n >= w {
                s
            } else {
                format!("{s}{}", repeat_char(fill, w - n))
            }))
        },
        "右对齐" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("右对齐", args, 2, 3)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?.max(0) as usize;
            let fill = if args.len() > 2 { one_char(&args[2])? } else { ' ' };
            let n = s.chars().count();
            Ok(Val::Str(if n >= w {
                s
            } else {
                format!("{}{s}", repeat_char(fill, w - n))
            }))
        },
        "补零" => |args: &[Val], _vm: &mut VM| -> R {
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
        "重复" => |args: &[Val], _vm: &mut VM| -> R {
            need("重复", args, 2)?;
            let s = super::as_text(&args[0]);
            let n = as_intish(&args[1])?;
            Ok(Val::Str(if n <= 0 { String::new() } else { s.repeat(n as usize) }))
        },
        "计数" => |args: &[Val], _vm: &mut VM| -> R {
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
        "是数字" => |args: &[Val], _vm: &mut VM| -> R {
            need("是数字", args, 1)?;
            let s = super::as_text(&args[0]);
            Ok(Val::Bool(!s.is_empty() && s.chars().all(is_digit_char)))
        },
        "是字母" => |args: &[Val], _vm: &mut VM| -> R {
            need("是字母", args, 1)?;
            let s = super::as_text(&args[0]);
            Ok(Val::Bool(!s.is_empty() && s.chars().all(|c| c.is_alphabetic())))
        },
        "是空白" => |args: &[Val], _vm: &mut VM| -> R {
            need("是空白", args, 1)?;
            let s = super::as_text(&args[0]);
            Ok(Val::Bool(!s.is_empty() && s.chars().all(is_space_char)))
        },
        "是大写" => |args: &[Val], _vm: &mut VM| -> R {
            need("是大写", args, 1)?;
            Ok(Val::Bool(is_upper(&super::as_text(&args[0]))))
        },
        "是小写" => |args: &[Val], _vm: &mut VM| -> R {
            need("是小写", args, 1)?;
            Ok(Val::Bool(is_lower(&super::as_text(&args[0]))))
        },
        "首字母大写" => |args: &[Val], _vm: &mut VM| -> R {
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
        "去前缀" => |args: &[Val], _vm: &mut VM| -> R {
            need("去前缀", args, 2)?;
            let s = super::as_text(&args[0]);
            let p = super::as_text(&args[1]);
            Ok(Val::Str(match s.strip_prefix(p.as_str()) {
                Some(rest) => rest.to_string(),
                None => s,
            }))
        },
        "去后缀" => |args: &[Val], _vm: &mut VM| -> R {
            need("去后缀", args, 2)?;
            let s = super::as_text(&args[0]);
            let p = super::as_text(&args[1]);
            Ok(Val::Str(match s.strip_suffix(p.as_str()) {
                Some(rest) => rest.to_string(),
                None => s,
            }))
        },
        "格式化" => |args: &[Val], _vm: &mut VM| -> R {
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
        "切成三段" => |args: &[Val], _vm: &mut VM| -> R {
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
        "按行拆分" => |args: &[Val], _vm: &mut VM| -> R {
            need("按行拆分", args, 1)?;
            let s = super::as_text(&args[0]);
            let items: Vec<Val> = split_lines(&s).into_iter().map(Val::Str).collect();
            Ok(crate::list_new(items))
        },
        // M51 补：这两个是 M50 第一批加的（「既有模块补缺」），当时只做了
        // Python 侧，宿主**静默缺失** —— 正是这个缺口促成 M51 的漂移检测。
        "填充" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("填充", args, 2, 3)?;
            let s = super::as_text(&args[0]);
            let w = as_intish(&args[1])?.max(0) as usize;
            let f = if args.len() > 2 { super::as_text(&args[2]) } else { " ".to_string() };
            let fchars: Vec<char> = f.chars().collect();
            if fchars.len() != 1 {
                return Err(err("类型错误", "「填充」的填充字符要传一个字符"));
            }
            let n = s.chars().count();
            if n >= w {
                return Ok(Val::Str(s));
            }
            let total = w - n;
            let left = total / 2;      // 与 Python str.center 同侧（左少右多）
            let mut out = String::new();
            for _ in 0..left { out.push(fchars[0]); }
            out.push_str(&s);
            for _ in 0..(total - left) { out.push(fchars[0]); }
            Ok(Val::Str(out))
        },
        "截断" => |args: &[Val], _vm: &mut VM| -> R {
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
