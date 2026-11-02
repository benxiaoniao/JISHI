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
        Val::Str(s) if !s.is_empty() => Ok(s.to_string()),
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

// ---------------------------------------------------------------------------
// M166：页面构件（表单 / 表格 / 布局 / 卡片）
//
// 📌 **输出格式是契约**：与 Python 侧逐字节相同（`test_m166_html_page.py` 盯着）。
// 改这里任何一个字符，都要同时改 `oracle/jishi/stdlib/html.py`、
// `oracle/node/vm.js`，并重录语料期望。
// ---------------------------------------------------------------------------

/// 内容位，但**列表会被拼起来**（`表单([字段们], "/新建")` 这种写法）。
///
/// 列表里允许嵌套列表（拼表单时常见），递归展开；每个元素按 `content` 的规矩走
/// （`原样` 片段原样输出，其余转义）。
fn content_list(v: &Val) -> String {
    match v {
        Val::List(l) => l.borrow().iter().map(content).collect::<Vec<_>>().join(""),
        other => content(other),
    }
}

/// 要一个非空文本参数。
fn need_text(name: &str, v: &Val) -> Result<String, crate::JishiError> {
    match v {
        Val::Str(s) if !s.is_empty() => Ok(s.to_string()),
        _ => Err(err("类型错误", format!("「{name}」要传非空文本"))),
    }
}

/// 给控件套一层 `<label>`：有说明就带 `<span>`，没有就裸着。
///
/// 包住之后点标签文字也能聚焦控件 —— 不用写 `for`/`id`，省掉一整类
/// 「id 拼错了点不动」的问题。
fn label_wrap(说明: Option<&Val>, inner: String) -> String {
    let t = match 说明 {
        None | Some(Val::None_) => return inner,
        Some(v) => crate::display(v),
    };
    if t.is_empty() {
        return inner;
    }
    format!("<label><span>{}</span>{inner}</label>", escape_html(&t))
}

/// 造一个自闭合空元素（`<input … />`）。
///
/// 不走 `空元素` 那个内建 —— 这里手里已经是 `(名, 值)` 对，
/// 转成 `Val::Dict` 再转回来只是白绕一圈。
///
/// 值是 `None` 表示 **HTML 布尔属性**（只输出属性名，不输出 `=""`）——
/// 与 `attrs_str` 对 `空` 的处理同口径（`checked` / `disabled` 都是这种）。
fn void_el(name: &str, pairs: &[(&str, Option<String>)]) -> String {
    let mut out = format!("<{name}");
    for (k, v) in pairs {
        match v {
            None => out.push_str(&format!(" {k}")),
            Some(x) => out.push_str(&format!(" {k}=\"{}\"", escape_html(x))),
        }
    }
    out.push_str(" />");
    out
}

/// 页面骨架用的内置样式。
///
/// **不引 CDN、不引框架** —— 一个 `<style>` 就够，离线也能看。做成常量
/// （不是每个页面一份）才能让五个执行器逐字对得上。
const 样式: &str = concat!(
    ":root{--底:#fff;--字:#1f2328;--弱:#6b7280;--线:#e5e7eb;--主:#2563eb;",
    "--成功:#16a34a;--警告:#d97706;--错误:#dc2626}",
    "*{box-sizing:border-box}",
    "body{margin:0;font:16px/1.6 system-ui,-apple-system,\"Segoe UI\",",
    "\"Noto Sans SC\",sans-serif;color:var(--字);background:var(--底)}",
    "header{padding:1rem 1.5rem;border-bottom:1px solid var(--线)}",
    "header h1{margin:0;font-size:1.25rem}",
    "nav{margin-top:.5rem}",
    "nav a{margin-right:1rem;color:var(--主);text-decoration:none}",
    "main{max-width:56rem;margin:0 auto;padding:1.5rem}",
    "footer{padding:1rem 1.5rem;color:var(--弱);border-top:1px solid var(--线);",
    "font-size:.875rem}",
    ".卡片{border:1px solid var(--线);border-radius:.5rem;padding:1rem;margin:1rem 0}",
    ".卡片 h2{margin:0 0 .5rem;font-size:1.1rem}",
    ".提示{padding:.75rem 1rem;border-radius:.375rem;margin:.75rem 0;",
    "border-left:4px solid;background:#f9fafb}",
    ".提示-成功{border-color:var(--成功);background:#f0fdf4}",
    ".提示-警告{border-color:var(--警告);background:#fffbeb}",
    ".提示-错误{border-color:var(--错误);background:#fef2f2}",
    ".基石表格{width:100%;border-collapse:collapse}",
    ".基石表格 th,.基石表格 td{padding:.5rem .75rem;",
    "border-bottom:1px solid var(--线);text-align:left}",
    ".基石表格 thead th{background:#f9fafb}",
    ".斑马纹 tbody tr:nth-child(even){background:#fafafa}",
    ".字段组{border:1px solid var(--线);border-radius:.375rem;margin:.5rem 0;",
    "padding:.5rem .75rem}",
    "label{margin:.5rem 0}",
    "label span{display:block;font-size:.875rem;color:var(--弱)}",
    "input,textarea,select{width:100%;padding:.5rem;border:1px solid var(--线);",
    "border-radius:.375rem;font:inherit}",
    "input[type=\"checkbox\"]{width:auto;margin-right:.5rem}",
    "button{padding:.5rem 1rem;border:0;border-radius:.375rem;background:var(--主);",
    "color:#fff;font:inherit;cursor:pointer}",
);

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "html";
        "转义" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("转义", args, 1, 1)?;
            Ok(Val::Str(escape_html(&crate::display(&args[0])).into()))
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
            let c = args.get(1).cloned().unwrap_or(Val::Str(String::new().into()));
            let a = attrs_str(args.get(2))?;
            Ok(Val::Str(format!("<{tag}{a}>{}</{tag}>", content(&c)).into()))
        },
        "空元素" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("空元素", args, 1, 2)?;
            let tag = check_tag("空元素", &args[0])?;
            let a = attrs_str(args.get(1))?;
            Ok(Val::Str(format!("<{tag}{a} />").into()))
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
            Ok(Val::Str(format!("<a{a}>{}</a>", content(&text)).into()))
        },
        "属性文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("属性文本", args, 1, 1)?;
            Ok(Val::Str(escape_html(&crate::display(&args[0])).into()))
        },
        "有序列表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("有序列表", args, 1, 2)?;
            Ok(Val::Str(list_html("ol", &args[0], args.get(1))?.into()))
        },
        "无序列表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("无序列表", args, 1, 2)?;
            Ok(Val::Str(list_html("ul", &args[0], args.get(1))?.into()))
        },
        // M166：页面构件（表单 / 表格 / 布局 / 卡片）
        //
        // 📌 输出格式是**契约**（与 Python 侧逐字节相同）。参数一律「位置 + 默认值」，
        // 不用关键字 —— 两个宿主的内建签名里没有放关键字的位置（见 `标签` 的注释）。
        "输入框" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("输入框", args, 1, 4)?;
            let 名字 = need_text("输入框", &args[0])?;
            let 类型 = match args.get(2) {
                Some(Val::Str(s)) if !s.is_empty() => s.to_string(),
                _ => "text".to_string(),
            };
            let mut pairs: Vec<(&str, Option<String>)> = vec![
                ("type", Some(类型)),
                ("name", Some(名字)),
            ];
            if let Some(v) = args.get(3) {
                let t = crate::display(v);
                if !t.is_empty() {
                    pairs.push(("value", Some(t)));
                }
            }
            Ok(Val::HtmlRaw(label_wrap(args.get(1), void_el("input", &pairs)).into()))
        },
        "多行文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("多行文本", args, 1, 4)?;
            let 名字 = need_text("多行文本", &args[0])?;
            let 值 = match args.get(2) {
                None | Some(Val::None_) => String::new(),
                Some(v) => crate::display(v),
            };
            let 行数 = match args.get(3) {
                None | Some(Val::None_) => "4".to_string(),
                Some(v) => crate::display(v),
            };
            let inner = format!(
                "<textarea name=\"{}\" rows=\"{}\">{}</textarea>",
                escape_html(&名字), escape_html(&行数), escape_html(&值));
            Ok(Val::HtmlRaw(label_wrap(args.get(1), inner).into()))
        },
        "下拉" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("下拉", args, 2, 4)?;
            let 名字 = need_text("下拉", &args[0])?;
            let 选项 = crate::stdlib::to_vec(&args[1])?;
            let 选中 = match args.get(3) {
                None | Some(Val::None_) => String::new(),
                Some(v) => crate::display(v),
            };
            let mut 片 = String::new();
            for 项 in &选项 {
                let 值 = crate::display(项);
                let 标记 = if !选中.is_empty() && 值 == 选中 { " selected" } else { "" };
                片.push_str(&format!(
                    "<option value=\"{}\"{标记}>{}</option>",
                    escape_html(&值), escape_html(&值)));
            }
            let inner = format!("<select name=\"{}\">{片}</select>", escape_html(&名字));
            // ⚠️ 说明是**第 3 个**参数（第 2 个是选项）—— 写成 `get(1)` 会把整个
            // 选项列表当标签文字印出来（踩过）。
            Ok(Val::HtmlRaw(label_wrap(args.get(2), inner).into()))
        },
        "多选" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("多选", args, 2, 4)?;
            let 名字 = need_text("多选", &args[0])?;
            let 选项 = crate::stdlib::to_vec(&args[1])?;
            let 已选: Vec<String> = match args.get(3) {
                Some(Val::List(l)) => l.borrow().iter().map(crate::display).collect(),
                _ => Vec::new(),
            };
            let mut 片 = String::new();
            for 项 in &选项 {
                let 值 = crate::display(项);
                let 勾了 = 已选.iter().any(|x| *x == 值);
                let mut pairs: Vec<(&str, Option<String>)> = vec![
                    ("type", Some("checkbox".to_string())),
                    ("name", Some(名字.clone())),
                    ("value", Some(值.clone())),
                ];
                // ⚠️ 只在勾了的时候才 push `checked`。别用「先拼上再 replace 剪掉」——
                // `checked=""` 剪完会留下一个 `=""`（踩过）。
                if 勾了 {
                    pairs.push(("checked", None));
                }
                let 控件 = void_el("input", &pairs);
                片.push_str(&format!("<label>{控件}{}</label>", escape_html(&值)));
            }
            let 题 = match args.get(2) {
                None | Some(Val::None_) => String::new(),
                Some(v) => {
                    let t = crate::display(v);
                    if t.is_empty() { String::new() } else { format!("<legend>{}</legend>", escape_html(&t)) }
                }
            };
            Ok(Val::HtmlRaw(format!("<fieldset class=\"字段组\">{题}{片}</fieldset>").into()))
        },
        "按钮" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("按钮", args, 1, 2)?;
            let 文字 = need_text("按钮", &args[0])?;
            let 类型 = match args.get(1) {
                Some(Val::Str(s)) if !s.is_empty() => s.to_string(),
                _ => "submit".to_string(),
            };
            Ok(Val::HtmlRaw(format!(
                "<button type=\"{}\">{}</button>",
                escape_html(&类型), escape_html(&文字)).into()))
        },
        "表单" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("表单", args, 2, 3)?;
            let 到 = need_text("表单", &args[1])?;
            let 方法 = match args.get(2) {
                Some(Val::Str(s)) if !s.is_empty() => s.to_string(),
                _ => "post".to_string(),
            };
            Ok(Val::HtmlRaw(format!(
                "<form action=\"{}\" method=\"{}\">{}</form>",
                escape_html(&到), escape_html(&方法), content_list(&args[0])).into()))
        },
        "由表格" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("由表格", args, 1, 2)?;
            let t = match &args[0] {
                Val::Table(t) => t.clone(),
                other => {
                    return Err(err("类型错误", format!(
                        "「由表格」要传表格值（用 表格.读表格 / 表格.从记录 造出来），不要传{}",
                        crate::type_label(other))))
                }
            };
            let 斑马 = args.get(1).map(|v| crate::truthy(v.clone())).unwrap_or(false);
            let 类 = if 斑马 { "基石表格 斑马纹" } else { "基石表格" };
            let 头: String = t.headers.iter()
                .map(|h| format!("<th>{}</th>", content(h))).collect();
            let 体: String = t.rows.iter().map(|行| {
                format!("<tr>{}</tr>", 行.iter()
                    .map(|c| format!("<td>{}</td>", content(c))).collect::<String>())
            }).collect();
            Ok(Val::HtmlRaw(format!(
                "<table class=\"{类}\"><thead><tr>{头}</tr></thead>\
                 <tbody>{体}</tbody></table>").into()))
        },
        "页面" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("页面", args, 2, 3)?;
            let 题 = escape_html(&need_text("页面", &args[0])?);
            // 导航：列表 → 自动拼 `<a>`；文本 → 当片段（给「我自己拼好了」的场景）
            let 导航片 = match args.get(2) {
                None | Some(Val::None_) => String::new(),
                Some(Val::List(l)) => {
                    let mut s = String::new();
                    for 项 in l.borrow().iter() {
                        let 对 = crate::stdlib::to_vec(项)?;
                        if 对.len() >= 2 {
                            s.push_str(&format!(
                                "<a href=\"{}\">{}</a>",
                                escape_html(&crate::display(&对[1])),
                                escape_html(&crate::display(&对[0]))));
                        }
                    }
                    format!("<nav>{s}</nav>")
                }
                Some(v) => {
                    let t = content(v);
                    if t.is_empty() { String::new() } else { format!("<nav>{t}</nav>") }
                }
            };
            let mut out = String::new();
            out.push_str("<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n");
            out.push_str("<meta charset=\"utf-8\" />\n");
            out.push_str("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n");
            out.push_str(&format!("<title>{题}</title>\n"));
            out.push_str(&format!("<style>{样式}</style>\n"));
            out.push_str("</head>\n<body>\n");
            out.push_str(&format!("<header><h1>{题}</h1>{导航片}</header>\n"));
            out.push_str(&format!("<main>{}</main>\n", content_list(&args[1])));
            out.push_str("<footer>由基石生成</footer>\n");
            out.push_str("</body>\n</html>");
            Ok(Val::HtmlRaw(out.into()))
        },
        "卡片" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("卡片", args, 2, 2)?;
            let 题 = escape_html(&need_text("卡片", &args[0])?);
            Ok(Val::HtmlRaw(format!(
                "<section class=\"卡片\"><h2>{题}</h2>{}</section>",
                content_list(&args[1])).into()))
        },
        "提示" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("提示", args, 2, 2)?;
            let 级别 = crate::display(&args[0]);
            if !matches!(级别.as_str(), "成功" | "警告" | "错误") {
                return Err(err("类型错误", format!(
                    "「提示」的级别只认成功、警告、错误，收到「{级别}」")));
            }
            Ok(Val::HtmlRaw(format!(
                "<div class=\"提示 提示-{级别}\">{}</div>",
                content_list(&args[1])).into()))
        },
        // M107：解析方向
        "取文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("取文本", args, 1, 1)?;
            let s = crate::display(&args[0]);
            Ok(Val::Str(文本节点(&扫(&s)).into()))
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
                            Val::Str(文本节点(&块).into()),
                            Val::Str(地址.into()),
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
                        行.push(Val::Str(文本节点(&格块).into()));
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
