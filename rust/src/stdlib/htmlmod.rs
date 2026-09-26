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

use std::collections::HashMap;

use super::{need_range, table, to_vec, R};
use crate::{err, Val, VM};

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

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "html";
        "转义" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("转义", args, 1, 1)?;
            Ok(Val::Str(escape_html(&crate::display(&args[0]))))
        },
        "原样" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("原样", args, 1, 1)?;
            match &args[0] {
                Val::HtmlRaw(_) => Ok(args[0].clone()),
                other => Ok(Val::HtmlRaw(crate::display(other))),
            }
        },
        "标签" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("标签", args, 1, 3)?;
            let tag = check_tag("标签", &args[0])?;
            let c = args.get(1).cloned().unwrap_or(Val::Str(String::new()));
            let a = attrs_str(args.get(2))?;
            Ok(Val::Str(format!("<{tag}{a}>{}</{tag}>", content(&c))))
        },
        "空元素" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("空元素", args, 1, 2)?;
            let tag = check_tag("空元素", &args[0])?;
            let a = attrs_str(args.get(1))?;
            Ok(Val::Str(format!("<{tag}{a} />")))
        },
        "链接" => |args: &[Val], _vm: &mut VM| -> R {
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
        "属性文本" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("属性文本", args, 1, 1)?;
            Ok(Val::Str(escape_html(&crate::display(&args[0]))))
        },
        "有序列表" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("有序列表", args, 1, 2)?;
            Ok(Val::Str(list_html("ol", &args[0], args.get(1))?))
        },
        "无序列表" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("无序列表", args, 1, 2)?;
            Ok(Val::Str(list_html("ul", &args[0], args.get(1))?))
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
