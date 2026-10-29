//! 标准库 `html`（Python 侧 `jishi/stdlib/html.py`，8 个函数）。
//!
//! 转义与生成片段。**默认全部转义**；要放已生成好的片段就套 `原样`。
//!
//! ⚠️ **属性用「字典」传，不用关键字参数**——`标签("p", "内容", {"类名": "提示"})`。
//! 理由见 Python 侧 `_属性串`：三个执行器 + Rust/Node 两种宿主的「内建函数」
//! 类型都**没有放关键字参数的位置**，用关键字会在别的宿主上静默丢失。
//!
//! ⚠️ **`原样` 标记用普通值（`Val::HtmlRaw`）而不是「字符串子类」**——
//! M47 把文本改成 C 侧原生字符串后，子类身份在 C VM 下会丢（见 Python 侧说明）。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{need_range, table, to_vec, R};
use crate::{err, Val};

/// `原样` 的标记类型。Rust 的 `Val` 是枚举，加一个变体就是最自然的做法
/// （不像 JS 那样要绕开「String 子类」）。
fn escape_html(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            _ => out.push(c),
        }
    }
    out
}

/// 内容位：`原样` 的片段原样输出，其余一律转义。
fn content(v: &Val) -> String {
    match v {
        Val::HtmlRaw(s) => s.clone(),
        other => escape_html(&crate::display(other)),
    }
}

/// 属性名字的中文化名（`class` / `for` 是别的语言的关键字，另给中文名）。
fn attr_key(k: &str) -> String {
    let mapped = match k {
        "类名" => "class",
        "对应" => "for",
        other => other,
    };
    mapped.replace('_', "-")
}

/// 属性字典 → 属性串。值为 `空` 时只输出属性名（HTML 布尔属性）。
fn attrs_str(v: Option<&Val>) -> Result<String, crate::JishiError> {
    let d = match v {
        None | Some(Val::None_) => return Ok(String::new()),
        Some(x) => x,
    };
    let pairs = match d {
        Val::Dict(m) => m.borrow().clone(),
        _ => {
            return Err(err(
                "类型错误",
                "属性要传字典，例如 标签(\"p\", \"内容\", {\"类名\": \"提示\"})",
            ))
        }
    };
    let mut out = String::new();
    for (k, val) in pairs {
        let key = attr_key(&crate::display(&k));
        match val {
            Val::None_ => out.push_str(&format!(" {key}")),
            other => out.push_str(&format!(" {key}=\"{}\"", escape_html(&crate::display(&other)))),
        }
    }
    Ok(out)
}

fn check_tag(name: &str, v: &Val) -> Result<String, crate::JishiError> {
    match v {
        Val::Str(s) if !s.is_empty() => Ok(s.clone()),
        _ => Err(err(
            "类型错误",
            format!("「{name}」的第一个参数要是标签名（非空文本）"),
        )),
    }
}

// ---------------------------------------------------------------------------
// M107：解析方向（从网页里取东西）
//
// ⚠️ **为什么自己写扫描器**：`html` 在五个执行器上要给出同一个结果，而 Rust /
// Node 复刻不了 CPython 的 `HTMLParser` 容错规则。所以这里照 Python 侧
// `stdlib/html.py` 那套**小而确定**的规则逐字对齐（Python 那份是判据基准）：
// 标签、注释、`<!doctype>`、`<script>/<style>` 丢弃、固定实体表 + 数字实体。
// 空白只认 ASCII 那六个（各语言的「unicode 空白」口径在小处对不齐）。
// ---------------------------------------------------------------------------

const 空白: &str = " \t\n\r\u{b}\u{c}";
const 名字字符: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.";

fn 是空白(c: char) -> bool {
    空白.contains(c)
}

fn 是名字字符(c: char) -> bool {
    名字字符.contains(c)
}

fn 解实体(t: &str) -> String {
    let chars: Vec<char> = t.chars().collect();
    let mut out = String::new();
    let mut i = 0usize;
    while i < chars.len() {
        if chars[i] != '&' {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        // 找下一个分号；`&#123;` 最长 10 字符，超过就不是实体
        let mut 分号 = None;
        for k in (i + 1)..chars.len().min(i + 12) {
            if chars[k] == ';' {
                分号 = Some(k);
                break;
            }
        }
        match 分号 {
            Some(k) if k - i <= 11 => {
                let 体: String = chars[i + 1..k].iter().collect();
                let 码: Option<u32> = if 体.starts_with('#') {
                    let 数 = &体[1..];
                    if 数.starts_with('x') || 数.starts_with('X') {
                        u32::from_str_radix(&数[1..], 16).ok()
                    } else {
                        数.parse::<u32>().ok()
                    }
                } else {
                    None
                };
                let 认了 = if let Some(码) = 码 {
                    !(0xD800..=0xDFFF).contains(&码) && 码 <= 0x10FFFF
                        && char::from_u32(码).is_some()
                } else {
                    matches!(体.as_str(), "amp" | "lt" | "gt" | "quot" | "apos" | "nbsp")
                };
                if 认了 {
                    match 码 {
                        Some(码) => out.push(char::from_u32(码).unwrap()),
                        None => out.push_str(match 体.as_str() {
                            "amp" => "&",
                            "lt" => "<",
                            "gt" => ">",
                            "quot" => "\"",
                            "apos" => "'",
                            _ => " ",          // nbsp
                        }),
                    }
                    i = k + 1;
                    continue;
                }
                out.push('&');
                i += 1;
            }
            _ => {
                out.push('&');
                i += 1;
            }
        }
    }
    out
}

fn 压空白(t: &str) -> String {
    let mut out = String::new();
    let mut 上个是空白 = false;
    for c in t.chars() {
        if 是空白(c) {
            if !上个是空白 {
                out.push(' ');
            }
            上个是空白 = true;
        } else {
            out.push(c);
            上个是空白 = false;
        }
    }
    out.trim_matches(' ').to_string()
}

/// 扫成 `(类型, 名, 体)` 三元组：类型 0=文本 1=开始 2=结束。
fn 扫(s: &str) -> Vec<(u8, String, String)> {
    let chars: Vec<char> = s.chars().collect();
    let n = chars.len();
    let mut 出: Vec<(u8, String, String)> = Vec::new();
    let mut i = 0usize;

    fn find(chars: &[char], from: usize, c: char) -> Option<usize> {
        (from..chars.len()).find(|&k| chars[k] == c)
    }
    fn starts(chars: &[char], from: usize, pat: &[char]) -> bool {
        from + pat.len() <= chars.len() && (0..pat.len()).all(|k| chars[from + k] == pat[k])
    }
    // 大小写不敏感的前缀匹配（找 `</script` 时源里可能是大写）
    fn starts_ci(chars: &[char], from: usize, pat: &[char]) -> bool {
        from + pat.len() <= chars.len()
            && (0..pat.len())
                .all(|k| chars[from + k].to_ascii_lowercase() == pat[k])
    }
    let 注释 = ['<', '!', '-', '-'];
    while i < n {
        let Some(ltpos) = find(&chars, i, '<') else {
            出.push((0, String::new(), chars[i..].iter().collect()));
            break;
        };
        if ltpos > i {
            出.push((0, String::new(), chars[i..ltpos].iter().collect()));
        }
        // 注释 `<!-- … -->`
        if starts(&chars, ltpos, &注释) {
            let mut 末2 = None;
            for k in ltpos + 4..n.saturating_sub(2) {
                if chars[k] == '-' && chars[k + 1] == '-' && chars[k + 2] == '>' {
                    末2 = Some(k + 2);
                    break;
                }
            }
            i = match 末2 {
                Some(m) => m + 1,
                None => n,
            };
            continue;
        }
        // `<!doctype>` / `<?…?>`
        if ltpos + 1 < n && (chars[ltpos + 1] == '!' || chars[ltpos + 1] == '?') {
            i = match find(&chars, ltpos + 1, '>') {
                Some(m) => m + 1,
                None => n,
            };
            continue;
        }
        let Some(末) = find(&chars, ltpos + 1, '>') else {
            出.push((0, String::new(), chars[ltpos..].iter().collect()));
            break;
        };
        let 体: String = chars[ltpos + 1..末].iter().collect();
        i = 末 + 1;
        let 结束 = 体.starts_with('/');
        let 体 = if 结束 { &体[1..] } else { &体[..] };
        let vc: Vec<char> = 体.chars().collect();
        let mut j = 0usize;
        while j < vc.len() && 是空白(vc[j]) {
            j += 1;
        }
        let 起 = j;
        while j < vc.len() && 是名字字符(vc[j]) {
            j += 1;
        }
        let 名 = vc[起..j].iter().collect::<String>().to_lowercase();
        if 名.is_empty() {
            continue;
        }
        if 名 == "script" || 名 == "style" {
            if !结束 {
                let 针: Vec<char> = format!("</{名}").chars().collect();
                let mut 找到 = None;
                for k in i..n {
                    if starts_ci(&chars, k, &针) {
                        找到 = Some(k);
                        break;
                    }
                }
                i = match 找到 {
                    Some(k) => match find(&chars, k, '>') {
                        Some(m) => m + 1,
                        None => n,
                    },
                    None => n,
                };
            }
            continue;
        }
        let 类型 = if 结束 { 2u8 } else { 1u8 };
        出.push((类型, 名, 体.to_string()));
    }
    出
}

fn 文本节点(节点们: &[(u8, String, String)]) -> String {
    let mut 块 = String::new();
    for (类型, _, 体) in 节点们 {
        if *类型 == 0 {
            块.push_str(体);
        }
    }
    压空白(&解实体(&块))
}

fn 取属性(标签体: &str, 名: &str) -> Option<String> {
    // 全用 Vec<char>，避免「字节下标 / 字符下标」混用（标签体里可能有中文）
    let 低: Vec<char> = 标签体.to_lowercase().chars().collect();
    let 针: Vec<char> = 名.to_lowercase().chars().collect();
    let body: Vec<char> = 标签体.chars().collect();
    let n = 低.len();
    let mut i = 0usize;
    while i < n {
        // 在 低 里找 针
        let mut k = None;
        if 针.len() <= n.saturating_sub(i) {
            for c in i..=(n - 针.len()) {
                if (0..针.len()).all(|t| 低[c + t] == 针[t]) {
                    k = Some(c);
                    break;
                }
            }
        }
        let Some(k) = k else { return None };
        // 前面必须是「名字边界」
        let 前好 = k == 0 || !是名字字符(低[k - 1]);
        let mut 后 = k + 针.len();
        while 后 < n && 是空白(低[后]) {
            后 += 1;
        }
        if 前好 && 后 < n && 低[后] == '=' {
            后 += 1;
            while 后 < n && 是空白(低[后]) {
                后 += 1;
            }
            if 后 < n && (低[后] == '"' || 低[后] == '\'') {
                let 引 = 低[后];
                if let Some(末) = (后 + 1..body.len()).find(|&m| body[m] == 引) {
                    return Some(解实体(&body[后 + 1..末].iter().collect::<String>()));
                }
                return Some(解实体(&body[后 + 1..].iter().collect::<String>()));
            }
            let mut 末 = 后;
            while 末 < n && !是空白(低[末]) && 低[末] != '>' {
                末 += 1;
            }
            return Some(解实体(&body[后..末].iter().collect::<String>()));
        }
        i = k + 针.len();
    }
    None
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "html";
        "转义" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("转义", args, 1, 1)?;
            Ok(Val::Str(escape_html(&crate::display(&args[0]))))
        },
        "原样" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("原样", args, 1, 1)?;
            match &args[0] {
                Val::HtmlRaw(_) => Ok(args[0].clone()),
                other => Ok(Val::HtmlRaw(crate::display(other))),
            }
        },
        "标签" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("标签", args, 1, 3)?;
            let tag = check_tag("标签", &args[0])?;
            let c = args.get(1).cloned().unwrap_or(Val::Str(String::new()));
            let a = attrs_str(args.get(2))?;
            Ok(Val::Str(format!("<{tag}{a}>{}</{tag}>", content(&c))))
        },
        "空元素" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("空元素", args, 1, 2)?;
            let tag = check_tag("空元素", &args[0])?;
            let a = attrs_str(args.get(1))?;
            Ok(Val::Str(format!("<{tag}{a} />")))
        },
        "链接" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("链接", args, 2, 3)?;
            let text = args[0].clone();
            let href = args[1].clone();
            // 自己拼属性串：href 在前，调用方给的属性在后（与 Python 侧同序）
            let mut a = format!(" href=\"{}\"", escape_html(&crate::display(&href)));
            if let Some(extra) = args.get(2) {
                let s = attrs_str(Some(extra))?;
                a.push_str(&s);
            }
            Ok(Val::Str(format!("<a{a}>{}</a>", content(&text))))
        },
        "属性文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("属性文本", args, 1, 1)?;
            Ok(Val::Str(escape_html(&crate::display(&args[0]))))
        },
        "有序列表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("有序列表", args, 1, 2)?;
            Ok(Val::Str(list_html("ol", &args[0], args.get(1))?))
        },
        "无序列表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("无序列表", args, 1, 2)?;
            Ok(Val::Str(list_html("ul", &args[0], args.get(1))?))
        },
        // M107：解析方向
        "取文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("取文本", args, 1, 1)?;
            let s = crate::display(&args[0]);
            Ok(Val::Str(文本节点(&扫(&s))))
        },
        "取链接" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("取链接", args, 1, 1)?;
            let s = crate::display(&args[0]);
            let 节点们 = 扫(&s);
            let mut 出: Vec<Val> = Vec::new();
            let mut i = 0usize;
            while i < 节点们.len() {
                let (类型, 名, 体) = &节点们[i];
                if *类型 == 1 && 名 == "a" {
                    if let Some(地址) = 取属性(体, "href") {
                        let mut 块: Vec<(u8, String, String)> = Vec::new();
                        let mut j = i + 1;
                        while j < 节点们.len() {
                            let (t2, n2, _) = &节点们[j];
                            if *t2 == 2 && n2 == "a" {
                                break;
                            }
                            块.push(节点们[j].clone());
                            j += 1;
                        }
                        出.push(crate::list_new(vec![
                            Val::Str(文本节点(&块)),
                            Val::Str(地址),
                        ]));
                        i = j + 1;
                        continue;
                    }
                }
                i += 1;
            }
            Ok(crate::list_new(出))
        },
        "取表格" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("取表格", args, 1, 1)?;
            let s = crate::display(&args[0]);
            let 节点们 = 扫(&s);
            let mut i = 0usize;
            while i < 节点们.len()
                && !(节点们[i].0 == 1 && 节点们[i].1 == "table")
            {
                i += 1;
            }
            let mut 出: Vec<Val> = Vec::new();
            let mut 行: Vec<Val> = Vec::new();
            let mut 在一格 = false;
            let mut 格块: Vec<(u8, String, String)> = Vec::new();
            while i < 节点们.len() {
                let (类型, 名, 体) = &节点们[i];
                if *类型 == 2 && 名 == "table" {
                    break;
                } else if *类型 == 1 && 名 == "tr" {
                    行 = Vec::new();
                } else if *类型 == 2 && 名 == "tr" {
                    if !行.is_empty() {
                        出.push(crate::list_new(行.clone()));
                    }
                } else if *类型 == 1 && (名 == "td" || 名 == "th") {
                    在一格 = true;
                    格块 = Vec::new();
                } else if *类型 == 2 && (名 == "td" || 名 == "th") {
                    if 在一格 {
                        行.push(Val::Str(文本节点(&格块)));
                    }
                    在一格 = false;
                } else if 在一格 && *类型 == 0 {
                    格块.push((0, String::new(), 体.clone()));
                }
                i += 1;
            }
            Ok(crate::list_new(出))
        },
    }
}

fn list_html(tag: &str, items: &Val, attrs: Option<&Val>) -> Result<String, crate::JishiError> {
    let a = attrs_str(attrs)?;
    let xs = to_vec(items).map_err(|_| {
        err(
            "类型错误",
            format!("「{}列表」的内容要传列表", if tag == "ol" { "有序" } else { "无序" }),
        )
    })?;
    let mut out = format!("<{tag}{a}>");
    for x in &xs {
        out.push_str(&format!("<li>{}</li>", content(x)));
    }
    out.push_str(&format!("</{tag}>"));
    Ok(out)
}
