//! 标准库 `配置`（Python 侧 `jishi/stdlib/配置.py`，8 个函数）。
//!
//! 读写 JSON 与「键值」配置文本，点号路径取/设，深度合并，展开。
//!
//! ⚠️ **不含 TOML**：Python 侧也没做（`tomllib` 要 3.11+），为不引入版本依赖
//! 三个宿主都只做 JSON 与键值文本。
//!
//! ⚠️ **「没传这个参数」的哨兵用普通文本，不能用特殊值**——Python 侧踩过：
//! 那边原用 `...`（`Ellipsis`），而 `...` 跨边界被转成 `空`，于是
//! 「缺键就返回默认值」**每次都命中**、函数恒返回 `空`。Rust 侧同理：
//! `Val::None_` 是用户能显式传的「空」，不能拿它当哨兵。

use std::collections::HashMap;
use std::path::Path as StdPath;

use super::{need_range, table, R};
use crate::{dict_new, err, list_new, Val, VM};

/// 「没传」的哨兵：普通文本常量（正常配置里不会有人用这个值）。
const NOT_GIVEN: &str = "\u{0}未给\u{0}";

fn is_given(v: Option<&Val>) -> bool {
    match v {
        None => false,
        Some(Val::Str(s)) => s != NOT_GIVEN,
        Some(_) => true,
    }
}

fn as_path(name: &str, v: &Val) -> Result<String, crate::JishiError> {
    match v {
        Val::Str(s) => Ok(s.clone()),
        other => Ok(crate::display(other)),
    }
}

fn as_dict(name: &str, v: &Val) -> Result<Vec<(Val, Val)>, crate::JishiError> {
    match v {
        Val::Dict(d) => Ok(d.borrow().clone()),
        _ => Err(err("类型错误", format!("「{name}」要传字典"))),
    }
}

/// 读文件（不存在返回 None）。
fn read_file(p: &str) -> Result<Option<String>, crate::JishiError> {
    match std::fs::read_to_string(p) {
        Ok(s) => Ok(Some(s)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(err("文件错误", format!("读配置文件「{p}」失败：{e}"))),
    }
}

fn write_file(p: &str, text: &str) -> Result<(), crate::JishiError> {
    if let Some(dir) = StdPath::new(p).parent() {
        if !dir.as_os_str().is_empty() {
            std::fs::create_dir_all(dir)
                .map_err(|e| err("文件错误", format!("写配置文件「{p}」失败：{e}")))?;
        }
    }
    std::fs::write(p, text)
        .map_err(|e| err("文件错误", format!("写配置文件「{p}」失败：{e}")))
}

/// 键值文本里的行内注释：整段被引号包着就原样（含 `#`），否则去掉 `#` 之后的内容。
fn strip_comment(s: &str) -> String {
    let b: Vec<char> = s.chars().collect();
    if b.len() >= 2 && (b[0] == '"' || b[0] == '\'') && b[b.len() - 1] == b[0] {
        return b[1..b.len() - 1].iter().collect();
    }
    let mut quote: Option<char> = None;
    for (i, c) in b.iter().enumerate() {
        match quote {
            Some(q) => {
                if *c == q {
                    quote = None;
                }
            }
            None => {
                if *c == '"' || *c == '\'' {
                    quote = Some(*c);
                } else if *c == '#' {
                    return b[..i].iter().collect::<String>().trim().to_string();
                }
            }
        }
    }
    s.trim().to_string()
}

/// 「这个值是什么形状」——报错时给出可操作的信息。
fn shape_of(v: &Val) -> String {
    match v {
        Val::Dict(d) => {
            let keys = d.borrow();
            if keys.is_empty() {
                return "空字典".to_string();
            }
            let shown: Vec<String> = keys
                .iter()
                .take(8)
                .map(|(k, _)| crate::display(k))
                .collect();
            let more = if keys.len() > 8 {
                format!("…等 {} 个", keys.len())
            } else {
                String::new()
            };
            format!("这些键：{}{}", shown.join("、"), more)
        }
        Val::List(l) => format!("一个 {} 项的列表", l.borrow().len()),
        other => format!("{} 类型", crate::type_label(other)),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! {
        "读JSON" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("读JSON", args, 1, 2)?;
            let p = as_path("读JSON", &args[0])?;
            match read_file(&p)? {
                None => {
                    if is_given(args.get(1)) {
                        return Ok(args[1].clone());
                    }
                    Err(err("文件错误", format!("配置文件「{p}」不存在")))
                }
                // 复用 `json` 模块的解析（它有完整的报错细节），
                // 不自己再写一个 JSON 解析器。
                Some(text) => super::jsonmod::parse(&text).map_err(|e| {
                    err("文件错误", format!("配置文件「{p}」不是合法的 JSON：{}", e.message))
                }),
            }
        },
        "写JSON" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("写JSON", args, 2, 3)?;
            let p = as_path("写JSON", &args[0])?;
            let indent = match args.get(2) {
                None => 2usize,
                Some(Val::Int(n)) if *n >= 0 => *n as usize,
                Some(_) => return Err(err("类型错误", "「缩进」要传 0 或正整数")),
            };
            let text = super::jsonmod::to_text(&args[1], Some(indent));
            write_file(&p, &format!("{text}\n"))?;
            Ok(Val::Str(p))
        },
        "读键值" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("读键值", args, 1, 3)?;
            let p = as_path("读键值", &args[0])?;
            let sep = match args.get(2) {
                None => "=".to_string(),
                Some(Val::Str(s)) if !s.is_empty() => s.clone(),
                Some(_) => return Err(err("类型错误", "「分隔符」要传非空文本")),
            };
            let text = match read_file(&p)? {
                None => {
                    if is_given(args.get(1)) {
                        return Ok(args[1].clone());
                    }
                    return Err(err("文件错误", format!("配置文件「{p}」不存在")));
                }
                Some(t) => t,
            };
            let mut out: Vec<(Val, Val)> = Vec::new();
            for (idx, raw) in text.lines().enumerate() {
                let line = raw.trim();
                if line.is_empty() || line.starts_with('#') {
                    continue;
                }
                match line.find(&sep) {
                    None => {
                        return Err(err(
                            "文件错误",
                            format!("配置文件「{p}」第 {} 行没有「{sep}」：{line}", idx + 1),
                        ))
                    }
                    Some(at) => {
                        let k = line[..at].trim();
                        let v = strip_comment(line[at + sep.len()..].trim());
                        if k.is_empty() {
                            return Err(err(
                                "文件错误",
                                format!("配置文件「{p}」第 {} 行的键是空的", idx + 1),
                            ));
                        }
                        out.push((Val::Str(k.to_string()), Val::Str(v)));
                    }
                }
            }
            Ok(dict_new(out))
        },
        "写键值" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("写键值", args, 2, 3)?;
            let p = as_path("写键值", &args[0])?;
            let pairs = as_dict("写键值", &args[1])
                .map_err(|_| err("类型错误", "「写键值」的第二个参数要传字典"))?;
            let sep = match args.get(2) {
                None => " = ".to_string(),
                Some(Val::Str(s)) => s.clone(),
                Some(_) => return Err(err("类型错误", "「分隔符」要传文本")),
            };
            let mut lines: Vec<String> = Vec::new();
            for (k, v) in pairs {
                let mut s = crate::display(&v);
                if s.contains('#') || s != s.trim() || s.starts_with('"') || s.starts_with('\'') {
                    s = format!("\"{}\"", s.replace('"', "\\\""));
                }
                lines.push(format!("{}{sep}{s}", crate::display(&k)));
            }
            let mut text = lines.join("\n");
            if !lines.is_empty() {
                text.push('\n');
            }
            write_file(&p, &text)?;
            Ok(Val::Str(p))
        },
        "取" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("取", args, 2, 3)?;
            let path = match &args[1] {
                Val::Str(s) if !s.is_empty() => s.clone(),
                _ => return Err(err("类型错误", "「取」的路径要传非空文本")),
            };
            let mut cur = args[0].clone();
            let mut walked: Vec<String> = Vec::new();
            for seg in path.split('.') {
                walked.push(seg.to_string());
                let next = match &cur {
                    Val::Dict(d) => d
                        .borrow()
                        .iter()
                        .find(|(k, _)| crate::display(k) == seg)
                        .map(|(_, v)| v.clone()),
                    Val::List(l) => seg
                        .parse::<usize>()
                        .ok()
                        .and_then(|i| l.borrow().get(i).cloned()),
                    _ => None,
                };
                match next {
                    Some(v) => cur = v,
                    None => {
                        if is_given(args.get(2)) {
                            return Ok(args[2].clone());
                        }
                        let parent = if walked.len() > 1 {
                            walked[..walked.len() - 1].join(".")
                        } else {
                            "<根>".to_string()
                        };
                        return Err(err(
                            "值错误",
                            format!(
                                "配置里没有「{}」这一段（「{parent}」下面是{}）",
                                walked.join("."),
                                shape_of(&cur)
                            ),
                        ));
                    }
                }
            }
            Ok(cur)
        },
        "设" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("设", args, 3, 3)?;
            let pairs = as_dict("设", &args[0])
                .map_err(|_| err("类型错误", "「设」的第一个参数要传字典"))?;
            let path = match &args[1] {
                Val::Str(s) if !s.is_empty() => s.clone(),
                _ => return Err(err("类型错误", "「设」的路径要传非空文本")),
            };
            let segs: Vec<&str> = path.split('.').collect();
            // 逐层建（从里往外），保持「不改原字典」的语义
            let mut value = args[2].clone();
            for seg in segs[1..].iter().rev() {
                let mut m: Vec<(Val, Val)> = vec![(Val::Str(seg.to_string()), value)];
                value = dict_new(std::mem::take(&mut m));
            }
            let mut out = pairs;
            let top = segs[0];
            let mut replaced = false;
            for (k, v) in out.iter_mut() {
                if crate::display(k) == top {
                    // 顶层键已存在：如果是字典就**逐层并入**（`设` 的语义是「建出来」，
                    // 与 Python 侧一致——那边也是整段替换/新建）
                    *v = value.clone();
                    replaced = true;
                    break;
                }
            }
            if !replaced {
                out.push((Val::Str(top.to_string()), value));
            }
            Ok(dict_new(out))
        },
        "合并" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("合并", args, 2, 2)?;
            let base = as_dict("合并", &args[0])
                .map_err(|_| err("类型错误", "「合并」的两个参数都要传字典"))?;
            let over = as_dict("合并", &args[1])
                .map_err(|_| err("类型错误", "「合并」的两个参数都要传字典"))?;
            let mut out = base;
            for (k, v) in over {
                let pos = out.iter().position(|(bk, _)| crate::display(bk) == crate::display(&k));
                match pos {
                    Some(i) => {
                        // 两边都是字典 → 逐层合并
                        if matches!(v, Val::Dict(_)) && matches!(out[i].1, Val::Dict(_)) {
                            let sub = module();
                            let f = &sub["合并"];
                            if let Val::Builtin(_, bf) = f {
                                out[i].1 = bf(&[out[i].1.clone(), v], _vm)?;
                            }
                        } else {
                            out[i].1 = v;
                        }
                    }
                    None => out.push((k, v)),
                }
            }
            Ok(dict_new(out))
        },
        "展开" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("展开", args, 1, 2)?;
            let pairs = as_dict("展开", &args[0])
                .map_err(|_| err("类型错误", "「展开」的第一个参数要传字典"))?;
            let prefix = match args.get(1) {
                None => String::new(),
                Some(Val::Str(s)) => s.clone(),
                Some(_) => return Err(err("类型错误", "「展开」的前缀要传文本")),
            };
            let mut out: Vec<(Val, Val)> = Vec::new();
            for (k, v) in pairs {
                let key = format!("{prefix}{}", crate::display(&k));
                match &v {
                    Val::Dict(d) => {
                        if d.borrow().is_empty() {
                            out.push((Val::Str(key), v.clone()));
                        } else {
                            let sub = module();
                            if let Val::Builtin(_, bf) = &sub["展开"] {
                                if let Val::Dict(inner) =
                                    bf(&[v.clone(), Val::Str(format!("{key}."))], _vm)?
                                {
                                    out.extend(inner.borrow().clone());
                                }
                            }
                        }
                    }
                    _ => out.push((Val::Str(key), v.clone())),
                }
            }
            Ok(dict_new(out))
        },
    }
}
