//! 标准库 `站点`（Python 侧 `jishi/stdlib/站点.py`，7 个函数）。
//!
//! 把「一堆页面」写成**一个能直接传服务器的静态站**。设计意图与四条规矩
//! （可复现 / 资源指纹 / 不引框架 / 五引擎逐字一致）见 Python 侧模块头，
//! 这里只说 Rust 这份的两处实现选择：
//!
//! 1. **指纹用 FNV-1a 32 位**，不是 SHA-256。理由不是「简单」，而是**三份实现
//!    必须逐位一致** —— SHA-256 要拖一份实现进来（或者依赖 crate），而 FNV
//!    三行写完，Python / Rust / JS 各写一份也不会跑偏。
//! 2. **默认样式表这份字符串是从 Python 侧照抄的**（`默认样式`）。M166 的教训：
//!    三宿主各写一份 CSS，手抄漏一个引号，页面**照样能看**，只有逐字比才抓得住。
//!    ⇒ 判据里有「整站输出五引擎逐字一致」，抄错了立刻红。
//!
//! ⚠️ **落盘必须是 `\n`**：`std::fs::write` 写的是字节，正好不会像 Python 的
//! 文本模式那样在 Windows 上换成 `\r\n`（Python 侧显式传了 `newline="\n"`）。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{need_range, table, R};
use crate::{err, Val};

/// 默认样式表。**逐字与 Python 侧 `站点.默认样式` 一致**。
pub(crate) const 默认样式: &str = r#"/* 基石站点的默认样式（由「站点」模块写出） */
:root {
  --bg: #f7f8fa; --fg: #1f2430; --muted: #5c6370; --line: #e4e7ec;
  --card: #ffffff; --accent: #1a5fb4; --accent-soft: #eaf1fb;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.75 "Segoe UI", "Microsoft YaHei", -apple-system, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.top {
  background: var(--card); border-bottom: 1px solid var(--line);
  padding: 14px 28px; display: flex; align-items: center; gap: 28px;
  position: sticky; top: 0; z-index: 2;
}
.brand { font-size: 20px; font-weight: 700; letter-spacing: .04em; }
.brand span { color: var(--muted); font-size: 13px; font-weight: 400; margin-left: 6px; }
nav a { margin-right: 18px; color: var(--muted); font-size: 15px; }
nav a.now { color: var(--accent); font-weight: 600; }
main { max-width: 960px; margin: 0 auto; padding: 32px 28px 64px; }
h1 { font-size: 30px; margin: 8px 0 6px; }
h2 { font-size: 20px; margin: 34px 0 12px; padding-bottom: 6px;
     border-bottom: 1px solid var(--line); }
.lead { color: var(--muted); font-size: 17px; margin: 0 0 26px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 14px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
        padding: 16px 18px; }
.card .k { color: var(--muted); font-size: 13px; }
.card .v { font-size: 26px; font-weight: 700; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; background: var(--card);
        border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line);
         vertical-align: top; font-size: 15px; }
th { background: var(--accent-soft); font-weight: 600; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
code { background: #eef1f5; padding: 1px 5px; border-radius: 4px;
       font-family: "Cascadia Code", Consolas, monospace; font-size: 14px; }
pre { background: #1f2430; color: #e6e9ef; padding: 14px 16px; border-radius: 8px;
      overflow-x: auto; }
pre code { background: none; color: inherit; padding: 0; }
footer { border-top: 1px solid var(--line); color: var(--muted); font-size: 13px;
         padding: 18px 28px 40px; max-width: 960px; margin: 0 auto; }
"#;

fn need_str(name: &str, v: &Val) -> Result<String, crate::JishiError> {
    match v {
        Val::Str(s) => Ok(s.to_string()),
        other => Err(err(
            "类型错误",
            format!(
                "「{name}」要传文本，但给的是{}（要拼数字请写 文本(值)）",
                crate::type_label(other)
            ),
        )),
    }
}

fn escape(s: &str) -> String {
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

/// 字典取键（没有就 `None`）。非字典直接报错 —— 与 Python 侧 `_取` 同口径。
fn 取(d: &Val, key: &str) -> Result<Option<Val>, crate::JishiError> {
    match d {
        Val::Dict(m) => Ok(m
            .borrow()
            .iter()
            .find(|(k, _)| matches!(k, Val::Str(s) if s.as_ref() == key))
            .map(|(_, v)| v.clone())),
        other => Err(err(
            "类型错误",
            format!("要传字典，但给的是{}", crate::type_label(other)),
        )),
    }
}

/// 从页/布局里取一个文本键，没有就给默认。
fn 取文(d: &Val, key: &str, 默认: &str) -> Result<String, crate::JishiError> {
    match 取(d, key)? {
        None | Some(Val::None_) => Ok(默认.to_string()),
        Some(v) => need_str(key, &v),
    }
}

/// 布局的全部键，顺序与 Python 侧 `_布局默认` **一致**（`打印(布局)` 要对得上）。
fn 布局(d: Option<&Val>) -> Result<Val, crate::JishiError> {
    let 源 = match d {
        None | Some(Val::None_) => None,
        Some(v @ Val::Dict(_)) => Some(v),
        Some(other) => {
            return Err(err(
                "类型错误",
                format!(
                    "「布局」的选项要传字典，但给的是{}",
                    crate::type_label(other)
                ),
            ))
        }
    };
    let 取或 = |键: &str, 默认: &str| -> Result<String, crate::JishiError> {
        match 源 {
            Some(s) => 取文(s, 键, 默认),
            None => Ok(默认.to_string()),
        }
    };
    // `指纹` 是**布尔**（其余键都是文本），单独取
    let 指针 = match 源.map(|s| 取(s, "指纹")).transpose()? {
        None | Some(None) | Some(Some(Val::None_)) => true,
        Some(Some(v)) => matches!(crate::truthy(v), true),
    };
    // 导航项：允许 `[["地址", "文字"]]`；每一项必须是长度 ≥2 的列表
    let 项们 = match 源.map(|s| 取(s, "导航项")).transpose()? {
        None | Some(None) | Some(Some(Val::None_)) => Vec::new(),
        Some(Some(v)) => {
            let raws = match &v {
                Val::List(l) => l.borrow().clone(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!("「导航项」要传列表，但给的是{}", crate::type_label(other)),
                    ))
                }
            };
            let mut out = Vec::with_capacity(raws.len());
            for it in raws {
                let pair = match &it {
                    Val::List(l) => l.borrow().clone(),
                    other => {
                        return Err(err(
                            "类型错误",
                            format!(
                                "「导航项」要是 [\"地址\", \"文字\"] 这样的对，但有一项是{}",
                                crate::type_label(other)
                            ),
                        ))
                    }
                };
                if pair.len() < 2 {
                    return Err(err(
                        "类型错误",
                        "「导航项」要是 [\"地址\", \"文字\"] 这样的对，但有一项不够两个",
                    ));
                }
                let a = need_str("导航项的地址", &pair[0])?;
                let b = need_str("导航项的文字", &pair[1])?;
                out.push(crate::list_new(vec![
                    Val::Str(a.into()),
                    Val::Str(b.into()),
                ]));
            }
            out
        }
    };
    let 语言 = 取或("语言", "zh-CN")?;
    let 样式文 = 取或("样式文件", "style.css")?;
    let 样式 = 取或("样式", 默认样式)?;
    Ok(crate::dict_new(vec![
        (Val::Str("标题分隔".into()), Val::Str(取或("标题分隔", " · ")?.into())),
        (Val::Str("标题后缀".into()), Val::Str(取或("标题后缀", "")?.into())),
        (Val::Str("品牌".into()), Val::Str(取或("品牌", "")?.into())),
        (Val::Str("品牌副".into()), Val::Str(取或("品牌副", "")?.into())),
        (Val::Str("导航项".into()), crate::list_new(项们)),
        (Val::Str("脚注".into()), Val::Str(取或("脚注", "")?.into())),
        (Val::Str("语言".into()), Val::Str(语言.into())),
        (Val::Str("样式文件".into()), Val::Str(样式文.into())),
        (Val::Str("样式".into()), Val::Str(样式.into())),
        (Val::Str("网址前缀".into()), Val::Str(取或("网址前缀", "")?.into())),
        (Val::Str("指纹".into()), Val::Bool(指针)),
    ]))
}

/// 导航项 + 当前地址 → `<a>` 串（当前那条带 `class="now"`）。
fn 导航(项们: &[Val], 当前: &str) -> Result<String, crate::JishiError> {
    let mut out = String::new();
    for it in 项们 {
        let pair = match it {
            Val::List(l) => l.borrow().clone(),
            other => {
                return Err(err(
                    "类型错误",
                    format!(
                        "「导航」的每一项要是 [\"地址\", \"文字\"]，但有一项是{}",
                        crate::type_label(other)
                    ),
                ))
            }
        };
        if pair.len() < 2 {
            return Err(err(
                "类型错误",
                "「导航」的每一项要是 [\"地址\", \"文字\"]，但有一项不够两个",
            ));
        }
        let 址 = need_str("导航项的地址", &pair[0])?;
        let 字 = need_str("导航项的文字", &pair[1])?;
        let 类 = if 址 == 当前 { " class=\"now\"" } else { "" };
        out.push_str(&format!(
            "<a{类} href=\"{}\">{}</a>",
            escape(&址),
            escape(&字)
        ));
    }
    Ok(out)
}

fn 页面(页: &Val, 表: &Val) -> Result<String, crate::JishiError> {
    if !matches!(页, Val::Dict(_)) {
        return Err(err(
            "类型错误",
            format!("「页面」要传字典，但给的是{}", crate::type_label(页)),
        ));
    }
    let 标题 = 取文(页, "标题", "")?;
    let 正文 = 取文(页, "正文", "")?;
    let 路径 = 取文(页, "路径", "")?;
    let 脚注 = match 取(页, "脚注")? {
        None | Some(Val::None_) => 取文(表, "脚注", "")?,
        Some(v) => need_str("脚注", &v)?,
    };
    let 后缀 = 取文(表, "标题后缀", "")?;
    let 分隔 = 取文(表, "标题分隔", " · ")?;
    let 全标题 = if 后缀.is_empty() {
        标题
    } else {
        format!("{标题}{分隔}{后缀}")
    };
    let 品牌副 = 取文(表, "品牌副", "")?;
    let 牌子 = if 品牌副.is_empty() {
        String::new()
    } else {
        format!("<span>{}</span>", escape(&品牌副))
    };
    let 导航项 = match 取(表, "导航项")? {
        Some(Val::List(l)) => l.borrow().clone(),
        _ => Vec::new(),
    };
    let 导航片 = 导航(&导航项, &路径)?;
    let 语言 = 取文(表, "语言", "zh-CN")?;
    let 样式文件 = 取文(表, "样式文件", "style.css")?;
    let 品牌 = 取文(表, "品牌", "")?;

    Ok(format!(
        "<!DOCTYPE html>\n<html lang=\"{}\">\n<head>\n<meta charset=\"utf-8\">\n\
         <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n\
         <title>{}</title>\n<link rel=\"stylesheet\" href=\"{}\">\n</head>\n<body>\n\
         <header class=\"top\">\n  <div class=\"brand\">{}{}</div>\n  <nav>{}</nav>\n\
         </header>\n<main>\n{}\n</main>\n<footer>{}</footer>\n</body>\n</html>\n",
        escape(&语言),
        escape(&全标题),
        escape(&样式文件),
        escape(&品牌),
        牌子,
        导航片,
        正文,
        escape(&脚注)
    ))
}

/// FNV-1a 32 位，按 UTF-8 字节算 —— **与 Python / Node 那份逐位一致**。
fn 指纹(文: &str) -> String {
    let mut h: u32 = 2166136261;
    for b in 文.as_bytes() {
        h ^= *b as u32;
        h = h.wrapping_mul(16777619);
    }
    format!("{h:08x}")
}

/// 页列表 → 每页的路径（页可以是字典，也可以直接是路径文本）。
fn 取路径们(页们: &Val) -> Result<Vec<String>, crate::JishiError> {
    let items = match 页们 {
        Val::List(l) => l.borrow().clone(),
        other => {
            return Err(err(
                "类型错误",
                format!("要传页列表，但给的是{}", crate::type_label(other)),
            ))
        }
    };
    let mut out = Vec::with_capacity(items.len());
    for it in items {
        out.push(match &it {
            Val::Dict(_) => 取文(&it, "路径", "")?,
            other => need_str("页的路径", other)?,
        });
    }
    Ok(out)
}

fn 拼网址(前缀: &str, 路径: &str) -> String {
    if 前缀.is_empty() {
        return 路径.to_string();
    }
    format!(
        "{}/{}",
        前缀.trim_end_matches('/'),
        路径.trim_start_matches('/')
    )
}

fn 站点地图(页们: &Val, 前缀: &str) -> Result<String, crate::JishiError> {
    let mut 址 = 取路径们(页们)?;
    // 🔴 排序 + 去重：sitemap 是**给机器读**的，顺序不能取决于调用方给的顺序
    //（否则「同样一组页面」写不出同样的字节）。
    // 口径是 **UTF-8 字节序** —— Rust 的 `str` 排序本来就是它，
    // Python / Node 那边也显式按字节排（三家的默认规则其实不一样）。
    址.sort();
    址.dedup();
    let mut out = String::from(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
         <urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n",
    );
    for 一 in &址 {
        out.push_str(&format!(
            "  <url><loc>{}</loc></url>\n",
            escape(&拼网址(前缀, 一))
        ));
    }
    out.push_str("</urlset>\n");
    Ok(out)
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "站点";
        "布局" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("布局", args, 0, 1)?;
            布局(args.first())
        },
        "导航" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("导航", args, 1, 2)?;
            let 项们 = match &args[0] {
                Val::List(l) => l.borrow().clone(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!("「导航」要传列表，但给的是{}", crate::type_label(other)),
                    ))
                }
            };
            let 当前 = match args.get(1) {
                None | Some(Val::None_) => String::new(),
                Some(v) => need_str("当前地址", v)?,
            };
            Ok(Val::Str(导航(&项们, &当前)?.into()))
        },
        "页面" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("页面", args, 2, 2)?;
            Ok(Val::Str(页面(&args[0], &args[1])?.into()))
        },
        "资源指纹" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("资源指纹", args, 1, 1)?;
            Ok(Val::Str(指纹(&need_str("内容", &args[0])?).into()))
        },
        "渲染" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("渲染", args, 2, 2)?;
            let items = match &args[0] {
                Val::List(l) => l.borrow().clone(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!("「渲染」要传页列表，但给的是{}", crate::type_label(other)),
                    ))
                }
            };
            let mut out = Vec::with_capacity(items.len());
            for 页 in items {
                let 路径 = 取文(&页, "路径", "")?;
                let html = 页面(&页, &args[1])?;
                out.push(crate::list_new(vec![
                    Val::Str(路径.into()),
                    Val::Str(html.into()),
                ]));
            }
            Ok(crate::list_new(out))
        },
        "sitemap" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("sitemap", args, 1, 2)?;
            let 前缀 = match args.get(1) {
                None | Some(Val::None_) => String::new(),
                Some(v) => need_str("前缀", v)?,
            };
            Ok(Val::Str(站点地图(&args[0], &前缀)?.into()))
        },
        "写站" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("写站", args, 3, 3)?;
            let 输出目录 = need_str("输出目录", &args[0])?;
            let items = match &args[1] {
                Val::List(l) => l.borrow().clone(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!("「写站」要传页列表，但给的是{}", crate::type_label(other)),
                    ))
                }
            };
            let 表 = &args[2];
            let 样式 = 取文(表, "样式", "")?;
            let 样式文件 = 取文(表, "样式文件", "style.css")?;
            let 指针 = match 取(表, "指纹")? {
                None | Some(Val::None_) => true,
                Some(v) => crate::truthy(v),
            };
            let mut 真名 = String::new();
            let mut 印 = String::new();
            if !样式.is_empty() {
                印 = 指纹(&样式);
                if 指针 {
                    let (名, 扩) = match 样式文件.rsplit_once('.') {
                        Some((a, b)) => (a.to_string(), format!(".{b}")),
                        None => (样式文件.clone(), String::new()),
                    };
                    真名 = format!("{名}.{印}{扩}");
                } else {
                    // 留在原文件名上（有人按固定名字引用它），见 `布局` 里 `指纹` 的说明
                    真名 = 样式文件.clone();
                }
            }
            if let Err(e) = std::fs::create_dir_all(&输出目录) {
                return Err(err("运行期错误", format!("建目录失败：{e}")));
            }
            let mut 页数: i128 = 0;
            for 页 in &items {
                let 路径 = 取文(页, "路径", "")?;
                if 路径.is_empty() {
                    return Err(err(
                        "类型错误",
                        "「写站」的每一页都要有「路径」（如 index.html）",
                    ));
                }
                let mut 内容 = 页面(页, 表)?;
                if !真名.is_empty() && 真名 != 样式文件 {
                    内容 = 内容.replace(
                        &format!("href=\"{样式文件}\""),
                        &format!("href=\"{真名}\""),
                    );
                }
                let 全 = std::path::Path::new(&输出目录).join(&路径);
                if let Some(父) = 全.parent() {
                    if let Err(e) = std::fs::create_dir_all(父) {
                        return Err(err("运行期错误", format!("建目录失败：{e}")));
                    }
                }
                if let Err(e) = std::fs::write(&全, 内容.as_bytes()) {
                    return Err(err("运行期错误", format!("写文件失败：{e}")));
                }
                页数 += 1;
            }
            if !真名.is_empty() {
                let 全 = std::path::Path::new(&输出目录).join(&真名);
                if let Err(e) = std::fs::write(&全, 样式.as_bytes()) {
                    return Err(err("运行期错误", format!("写文件失败：{e}")));
                }
            }
            let 前缀 = 取文(表, "网址前缀", "")?;
            let 图 = 站点地图(&args[1], &前缀)?;
            let 全 = std::path::Path::new(&输出目录).join("sitemap.xml");
            if let Err(e) = std::fs::write(&全, 图.as_bytes()) {
                return Err(err("运行期错误", format!("写文件失败：{e}")));
            }
            Ok(crate::dict_new(vec![
                (Val::Str("页面数".into()), Val::Int(页数)),
                (Val::Str("样式文件".into()), Val::Str(真名.into())),
                (Val::Str("指纹".into()), Val::Str(印.into())),
                (Val::Str("sitemap".into()), Val::Str("sitemap.xml".into())),
            ]))
        },
    }
}
