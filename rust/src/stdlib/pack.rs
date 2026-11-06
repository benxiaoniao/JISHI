//! 标准库 `打包`（Python 侧 `jishi/stdlib/打包.py`，3 个函数）。
//!
//! 把「一个程序」打成**一个带清单的 zip**，并能验回来。设计意图与三条硬规矩
//! （可复现 / 清单是唯一事实 / 三宿主逐字一致）见 Python 侧模块头，这里只说
//! Rust 这份的两处实现选择：
//!
//! 1. **`清单.json` 的文本是手写序列化**，不是 `serde_json`：格式是**契约**
//!    （三宿主算出的包要通用），键序、缩进、转义都得逐字一样，交给库反而
//!    不敢保证。与 Python `_清单文本` 一一对应。
//! 2. **zip 用 `zip::build_stored`**（stored，不压缩）。跨引擎本来就**不**
//!    逐字节相同（Python / Node 用 deflate），口径是「同一个引擎、同样输入
//!    ⇒ 同样的字节」+「两引擎解出来的条目清单一致」。

use crate::BuiltinHost;
use std::collections::HashMap;
use std::fs;

use super::{need_range, table, R};
use crate::{err, err_with_hint, Val};

/// 包里那份清单的文件名。**必须放在包根上** —— 安装器解完第一件事就是找它。
const 清单名: &str = "清单.json";

// ---------------------------------------------------------------------------
// 选项
// ---------------------------------------------------------------------------

struct 选项 {
    名字: String,
    版本: String,
    平台: String,
    入口: String,
    基础: String,
}

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

fn 取文(选: &Option<Val>, 键: &str) -> Result<String, crate::JishiError> {
    let Some(d) = 选 else { return Ok(String::new()) };
    match 取(d, 键)? {
        None | Some(Val::None_) => Ok(String::new()),
        Some(Val::Str(s)) => Ok(s.to_string()),
        Some(other) => Err(err(
            "值错误",
            format!("选项「{键}」要传文本，给的是{}", crate::type_label(&other)),
        )),
    }
}

fn 读选项(v: Option<&Val>) -> Result<选项, crate::JishiError> {
    let 源 = match v {
        None | Some(Val::None_) => None,
        Some(x @ Val::Dict(_)) => Some(x.clone()),
        Some(other) => {
            return Err(err(
                "值错误",
                format!(
                    "「选项」要传字典，例如 {{\"名字\": \"记账\", \"版本\": \"1.0.0\"}}，给的是{}",
                    crate::type_label(other)
                ),
            ))
        }
    };
    Ok(选项 {
        名字: 取文(&源, "名字")?,
        版本: 取文(&源, "版本")?,
        平台: 取文(&源, "平台")?,
        入口: 取文(&源, "入口")?,
        基础: 取文(&源, "基础")?,
    })
}

// ---------------------------------------------------------------------------
// 收集
// ---------------------------------------------------------------------------

/// `full` 相对 `base` 的条目名（用 `/` 分隔）。**纯字符串前缀**，
/// 不做 `canonicalize` —— 三个宿主对「相对/绝对混着给」的处理各家不同，
/// 而逐字一致比聪明要紧。算不出就报错，不猜。
fn 条目名(full: &str, base: &str) -> Result<String, crate::JishiError> {
    let f = full.replace('\\', "/");
    let b = base.replace('\\', "/");
    let b = b.trim_end_matches('/');
    if f == b {
        return Ok(String::new());
    }
    match f.strip_prefix(&format!("{b}/")) {
        Some(rel) => Ok(rel.to_string()),
        None => Err(err(
            "值错误",
            format!("「{full}」不在基础目录「{base}」里面 —— 算不出包里的条目名"),
        )),
    }
}

fn 递归收录(dir: &str, base: &str, out: &mut Vec<(String, String)>) -> Result<(), crate::JishiError> {
    let rd = fs::read_dir(dir)
        .map_err(|e| err("运行期错误", format!("读目录「{dir}」失败：{e}")))?;
    let mut 子们: Vec<std::path::PathBuf> = rd.filter_map(|e| e.ok()).map(|e| e.path()).collect();
    子们.sort();
    for p in 子们 {
        let s = p.to_string_lossy().to_string();
        if p.is_dir() {
            递归收录(&s, base, out)?;
        } else if p.is_file() {
            out.push((条目名(&s, base)?, s));
        }
    }
    Ok(())
}

fn 摊开(来源: &Val, 基础: &str) -> Result<Vec<(String, String)>, crate::JishiError> {
    let mut 出: Vec<(String, String)> = Vec::new();
    match 来源 {
        Val::List(_) => {
            if 基础.is_empty() {
                return Err(err_with_hint(
                    "值错误",
                    "传文件列表时必须给「基础」目录 —— 否则算不出包里的条目名",
                    "例如 打包.清单([\"发布/记账.exe\"], {\"名字\": \"记账\", \"基础\": \"发布\"})"
                        .into(),
                ));
            }
            for it in super::to_vec(来源)? {
                let full = super::as_text(&it);
                出.push((条目名(&full, 基础)?, full));
            }
        }
        other => {
            let root = super::as_text(other);
            if !std::path::Path::new(&root).is_dir() {
                return Err(err(
                    "值错误",
                    format!("「{root}」既不是目录也不是列表，无法确定要打包什么"),
                ));
            }
            let base = if 基础.is_empty() { root.clone() } else { 基础.to_string() };
            递归收录(&root, &base, &mut 出)?;
        }
    }
    for (arc, _) in &出 {
        if arc.is_empty() || arc.ends_with('/') {
            return Err(err("值错误", format!("「{arc}」不是有效的包内路径")));
        }
    }
    Ok(出)
}

// ---------------------------------------------------------------------------
// 清单
// ---------------------------------------------------------------------------

struct 一项 {
    路径: String,
    大小: i128,
    sha256: String,
}

/// 按 **UTF-8 字节序**排 —— Python 默认按码点、JS 按 UTF-16 单元，
/// 遇到 emoji 文件名三个宿主会各排各的（M168 的教训）。
fn 建清单(
    项们: &[(String, String)],
    选: &选项,
) -> Result<(Vec<一项>, i128), crate::JishiError> {
    let mut 出 = Vec::with_capacity(项们.len());
    let mut 总: i128 = 0;
    for (arc, full) in 项们 {
        let data = fs::read(full)
            .map_err(|e| err("运行期错误", format!("读「{full}」失败：{e}")))?;
        let 大小 = data.len() as i128;
        总 += 大小;
        出.push(一项 {
            路径: arc.clone(),
            大小,
            sha256: crate::crypto::sha256(&data),
        });
    }
    // 先按字节比，再按字符串比 —— 两个都稳（字节比是契约，字符串比兜底）
    出.sort_by(|a, b| a.路径.as_bytes().cmp(b.路径.as_bytes()));
    let _ = 选;
    Ok((出, 总))
}

fn 清单成值(项们: &[一项], 选: &选项, 总: i128) -> Val {
    let 文件们: Vec<Val> = 项们
        .iter()
        .map(|t| {
            crate::dict_new(vec![
                (Val::Str("路径".into()), Val::Str(t.路径.clone().into())),
                (Val::Str("大小".into()), Val::Int(t.大小)),
                (Val::Str("sha256".into()), Val::Str(t.sha256.clone().into())),
            ])
        })
        .collect();
    crate::dict_new(vec![
        (Val::Str("名字".into()), Val::Str(选.名字.clone().into())),
        (Val::Str("版本".into()), Val::Str(选.版本.clone().into())),
        (Val::Str("平台".into()), Val::Str(选.平台.clone().into())),
        (Val::Str("入口".into()), Val::Str(选.入口.clone().into())),
        (Val::Str("文件数".into()), Val::Int(项们.len() as i128)),
        (Val::Str("字节数".into()), Val::Int(总)),
        (Val::Str("文件们".into()), crate::list_new(文件们)),
    ])
}

// ---------------------------------------------------------------------------
// 清单.json 的文本（**格式是契约**，与 Python `_清单文本` 逐字对应）
// ---------------------------------------------------------------------------

fn 转义(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        let o = c as u32;
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            _ if o < 0x20 => out.push_str(&format!("\\u{o:04x}")),
            c => out.push(c),
        }
    }
    out
}

fn 清单文本(项们: &[一项], 选: &选项, 总: i128) -> String {
    let mut L: Vec<String> = Vec::new();
    L.push("{".to_string());
    L.push(format!("  \"名字\": \"{}\",", 转义(&选.名字)));
    L.push(format!("  \"版本\": \"{}\",", 转义(&选.版本)));
    L.push(format!("  \"平台\": \"{}\",", 转义(&选.平台)));
    L.push(format!("  \"入口\": \"{}\",", 转义(&选.入口)));
    L.push(format!("  \"文件数\": {},", 项们.len()));
    L.push(format!("  \"字节数\": {总},"));
    if 项们.is_empty() {
        L.push("  \"文件们\": []".to_string());
    } else {
        L.push("  \"文件们\": [".to_string());
        let 末 = 项们.len() - 1;
        for (i, t) in 项们.iter().enumerate() {
            L.push(format!(
                "    {{\"路径\": \"{}\", \"大小\": {}, \"sha256\": \"{}\"}}{}",
                转义(&t.路径),
                t.大小,
                转义(&t.sha256),
                if i < 末 { "," } else { "" }
            ));
        }
        L.push("  ]".to_string());
    }
    L.push("}".to_string());
    let mut s = L.join("\n");
    s.push('\n');
    s
}

// ---------------------------------------------------------------------------
// 生成
// ---------------------------------------------------------------------------

fn 包文件名(项们: &[一项], 选: &选项) -> Result<String, crate::JishiError> {
    if 选.名字.is_empty() {
        return Err(err(
            "值错误",
            "「名字」不能空 —— 它是产物的文件名，也是安装后在开始菜单里显示的名字",
        ));
    }
    for (段, 键) in [(&选.名字, "名字"), (&选.版本, "版本"), (&选.平台, "平台")] {
        for bad in ['/', '\\', ':', '*', '?', '"', '<', '>', '|'] {
            if 段.contains(bad) {
                return Err(err(
                    "值错误",
                    format!("选项「{键}」里不能有「{bad}」—— 它要当文件名用"),
                ));
            }
        }
    }
    let mut 部 = vec![选.名字.clone()];
    if !选.版本.is_empty() {
        部.push(选.版本.clone());
    }
    if !选.平台.is_empty() {
        部.push(选.平台.clone());
    }
    let _ = 项们;
    Ok(format!("{}.zip", 部.join("-")))
}

// ---------------------------------------------------------------------------
// 校验
// ---------------------------------------------------------------------------

/// 从解析出来的 `清单.json` 重建那个字典（**顺序固定**，与 Python 的
/// `json.loads` 同序 —— 打印出来要一样）。
fn 清单成值从json(j: &crate::json::Json) -> Val {
    let 取文 = |k: &str| -> String {
        j.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
    };
    let 取数 = |k: &str| -> i128 { j.get(k).and_then(|v| v.as_i128()).unwrap_or(0) };
    let 文件们: Vec<Val> = j
        .get("文件们")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .map(|f| {
                    crate::dict_new(vec![
                        (
                            Val::Str("路径".into()),
                            Val::Str(f.get("路径").and_then(|v| v.as_str()).unwrap_or("").into()),
                        ),
                        (
                            Val::Str("大小".into()),
                            Val::Int(f.get("大小").and_then(|v| v.as_i128()).unwrap_or(0)),
                        ),
                        (
                            Val::Str("sha256".into()),
                            Val::Str(
                                f.get("sha256").and_then(|v| v.as_str()).unwrap_or("").into(),
                            ),
                        ),
                    ])
                })
                .collect()
        })
        .unwrap_or_default();
    crate::dict_new(vec![
        (Val::Str("名字".into()), Val::Str(取文("名字").into())),
        (Val::Str("版本".into()), Val::Str(取文("版本").into())),
        (Val::Str("平台".into()), Val::Str(取文("平台").into())),
        (Val::Str("入口".into()), Val::Str(取文("入口").into())),
        (Val::Str("文件数".into()), Val::Int(取数("文件数"))),
        (Val::Str("字节数".into()), Val::Int(取数("字节数"))),
        (Val::Str("文件们".into()), crate::list_new(文件们)),
    ])
}

fn 校验结果(通过: bool, 问题们: Vec<String>, 清单: Val) -> Val {
    crate::dict_new(vec![
        (Val::Str("通过".into()), Val::Bool(通过)),
        (
            Val::Str("问题们".into()),
            crate::list_new(问题们.into_iter().map(|s| Val::Str(s.into())).collect()),
        ),
        (Val::Str("清单".into()), 清单),
    ])
}

// ---------------------------------------------------------------------------

/// 打一个包（`打包.生成` 的实现体）。
///
/// 单独抽出来的理由：`jishi 打包`（M172）要给四个产物各打一个包（清单里的
/// 「平台」得跟产物名对得上），而 CLI 不在 VM 里 —— 直接把实现体暴露成
/// `pub(crate)`，命令行调的与语言里调的**是同一段代码**，不会各写一份清单格式。
pub(crate) fn 建包(
    来源: &Val,
    输出目录: &str,
    选项: Option<&Val>,
) -> Result<String, crate::JishiError> {
    let 选 = 读选项(选项)?;
    let mut 项们 = 摊开(来源, &选.基础)?;
    if 项们.is_empty() {
        return Err(err(
            "值错误",
            format!("「{}」里没有文件，打出来的包会是空的", super::as_text(来源)),
        ));
    }
    if 项们.iter().any(|(arc, _)| arc == 清单名) {
        return Err(err_with_hint(
            "值错误",
            format!("「{清单名}」是打包自己用的名字，不能当程序文件"),
            "改个名（例如 说明.json）；包根上那份是清单，会被覆盖".into(),
        ));
    }
    let (列, 总) = 建清单(&项们, &选)?;
    let 名 = 包文件名(&列, &选)?;
    fs::create_dir_all(输出目录)
        .map_err(|e| err("运行期错误", format!("建输出目录「{输出目录}」失败：{e}")))?;
    let 包 = format!("{}/{}", 输出目录.trim_end_matches(['/', '\\']), 名);

    // 条目顺序 = 清单顺序（清单.json 在第一个）—— 顺序定死是包字节可复现的
    // 一半（另一半是时间/权限，`Out::add` 里已写死）。
    let 序号: HashMap<String, usize> = 列
        .iter()
        .enumerate()
        .map(|(i, t)| (t.路径.clone(), i))
        .collect();
    项们.sort_by_key(|(arc, _)| 序号.get(arc).copied().unwrap_or(usize::MAX));

    let mut 条目: Vec<(String, Vec<u8>)> = Vec::with_capacity(项们.len() + 1);
    条目.push((清单名.to_string(), 清单文本(&列, &选, 总).into_bytes()));
    for (arc, full) in &项们 {
        let data =
            fs::read(full).map_err(|e| err("运行期错误", format!("读「{full}」失败：{e}")))?;
        条目.push((arc.clone(), data));
    }
    let bytes = crate::zip::build_stored(&条目);
    fs::write(&包, &bytes)
        .map_err(|e| err("运行期错误", format!("写包「{包}」失败：{e}")))?;
    Ok(包)
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "打包";
        "清单" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("清单", args, 1, 2)?;
            let 选 = 读选项(args.get(1))?;
            let 项们 = 摊开(&args[0], &选.基础)?;
            let (列, 总) = 建清单(&项们, &选)?;
            Ok(清单成值(&列, &选, 总))
        },
        "生成" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("生成", args, 2, 3)?;
            let 包 = 建包(&args[0], &super::as_text(&args[1]), args.get(2))?;
            Ok(Val::Str(包.into()))
        },
        "安装包" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("安装包", args, 2, 3)?;
            let 包 = super::as_text(&args[0]);
            let 存根 = super::as_text(&args[1]);
            let 包字节 = fs::read(&包)
                .map_err(|e| err("运行期错误", format!("读包「{包}」失败：{e}")))?;
            let 存根字节 = fs::read(&存根)
                .map_err(|e| err("运行期错误", format!("读存根「{存根}」失败：{e}")))?;
            // 名字/版本/平台要从包里的清单拿 —— 不另要一份，避免「说的和包里的不一样」
            let 条目们 = crate::zip::read_all(&包字节).map_err(|e| {
                err_with_hint(
                    "值错误",
                    format!("「{包}」不是「打包.生成」造的包，接不成安装包：{}", e.message),
                    "先 打包.生成(来源, 目录, 选项) 出一个包，再接安装包".into(),
                )
            })?;
            let 原文 = 条目们
                .iter()
                .find(|(n, _)| n == 清单名)
                .map(|(_, d)| d.clone())
                .ok_or_else(|| {
                    err_with_hint(
                        "值错误",
                        format!("「{包}」里没有「{清单名}」，接不成安装包"),
                        "先 打包.生成(来源, 目录, 选项) 出一个包，再接安装包".into(),
                    )
                })?;
            let 文本 = String::from_utf8(原文).map_err(|e| {
                err("值错误", format!("「{清单名}」不是 UTF-8：{e}"))
            })?;
            let j = crate::json::parse(&文本)
                .map_err(|e| err("值错误", format!("「{清单名}」解析失败：{e}")))?;
            let 取 = |k: &str| j.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();

            let 输出 = if args.len() > 2 {
                super::as_text(&args[2])
            } else {
                let mut 部 = vec![if 取("名字").is_empty() {
                    "程序".to_string()
                } else {
                    取("名字")
                }];
                for k in ["版本", "平台"] {
                    let v = 取(k);
                    if !v.is_empty() {
                        部.push(v);
                    }
                }
                let 目录 = std::path::Path::new(&包)
                    .parent()
                    .map(|p| p.display().to_string())
                    .unwrap_or_default();
                if 目录.is_empty() {
                    format!("{}-setup.exe", 部.join("-"))
                } else {
                    format!("{}-setup.exe", format!("{}/{}", 目录, 部.join("-")))
                }
            };
            if let Some(父) = std::path::Path::new(&输出).parent() {
                if !父.as_os_str().is_empty() {
                    fs::create_dir_all(父).map_err(|e| {
                        err("运行期错误", format!("建目录「{}」失败：{e}", 父.display()))
                    })?;
                }
            }
            // 尾部索引：偏移 = 存根长度（包紧跟在存根后面），长度 = 包长度。
            let mut 全 = Vec::with_capacity(存根字节.len() + 包字节.len() + 24);
            全.extend_from_slice(&存根字节);
            全.extend_from_slice(&包字节);
            全.extend_from_slice(&(存根字节.len() as u64).to_le_bytes());
            全.extend_from_slice(&(包字节.len() as u64).to_le_bytes());
            全.extend_from_slice(b"JISHIPK1");
            fs::write(&输出, &全)
                .map_err(|e| err("运行期错误", format!("写安装包「{输出}」失败：{e}")))?;
            Ok(Val::Str(输出.into()))
        },
        "校验" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("校验", args, 1, 1)?;
            let 路径 = super::as_text(&args[0]);
            let data = match fs::read(&路径) {
                Ok(d) => d,
                Err(e) => {
                    return Ok(校验结果(
                        false,
                        vec![format!("打不开包「{路径}」：{e}")],
                        crate::dict_new(vec![]),
                    ))
                }
            };
            let 条目们 = match crate::zip::read_all(&data) {
                Ok(v) => v,
                Err(e) => {
                    return Ok(校验结果(
                        false,
                        vec![format!("打不开包「{路径}」：{}", e.message)],
                        crate::dict_new(vec![]),
                    ))
                }
            };
            let Some((_, 原文)) = 条目们.iter().find(|(n, _)| n == 清单名) else {
                return Ok(校验结果(
                    false,
                    vec![format!(
                        "包里没有「{清单名}」—— 这不是「打包.生成」造的包（或者是老包）"
                    )],
                    crate::dict_new(vec![]),
                ));
            };
            let 文本 = match std::str::from_utf8(原文) {
                Ok(s) => s,
                Err(e) => {
                    return Ok(校验结果(
                        false,
                        vec![format!("「{清单名}」读不出来：{e}")],
                        crate::dict_new(vec![]),
                    ))
                }
            };
            let 解析 = match crate::json::parse(文本) {
                Ok(j) => j,
                Err(e) => {
                    return Ok(校验结果(
                        false,
                        vec![format!("「{清单名}」读不出来：{e}")],
                        crate::dict_new(vec![]),
                    ))
                }
            };
            let Some(文件们) = 解析.get("文件们").and_then(|v| v.as_array()) else {
                return Ok(校验结果(
                    false,
                    vec![format!("「{清单名}」格式不对：缺「文件们」那一项")],
                    crate::dict_new(vec![]),
                ));
            };
            // (路径, 大小, sha256)
            let mut 应有: Vec<(String, Option<i128>, Option<String>)> = Vec::new();
            for f in 文件们 {
                match f.get("路径").and_then(|v| v.as_str()) {
                    Some(p) => 应有.push((
                        p.to_string(),
                        f.get("大小").and_then(|v| v.as_i128()),
                        f.get("sha256").and_then(|v| v.as_str()).map(|s| s.to_string()),
                    )),
                    None => {
                        return Ok(校验结果(
                            false,
                            vec![format!("「{清单名}」里有一条不认识（缺「路径」）")],
                            crate::dict_new(vec![]),
                        ))
                    }
                }
            }
            应有.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));

            let 实测: Vec<(&String, &Vec<u8>)> = 条目们
                .iter()
                .filter(|(n, _)| n != 清单名 && !n.ends_with('/'))
                .map(|(n, d)| (n, d))
                .collect();

            let mut 问题们: Vec<String> = Vec::new();
            for (名, 应该大小, 应该摘要) in &应有 {
                let Some((_, 数据)) = 实测.iter().find(|(n, _)| *n == 名) else {
                    问题们.push(format!("缺文件：{名}"));
                    continue;
                };
                if let Some(sz) = 应该大小 {
                    if 数据.len() as i128 != *sz {
                        问题们.push(format!(
                            "大小不对：{名}（清单 {sz}，实际 {}）",
                            数据.len()
                        ));
                        continue;
                    }
                }
                if let Some(d) = 应该摘要 {
                    if crate::crypto::sha256(数据) != *d {
                        问题们.push(format!("内容不对：{名}（sha256 与清单不符）"));
                    }
                }
            }
            let mut 多出: Vec<&String> = 实测
                .iter()
                .map(|(n, _)| *n)
                .filter(|n| !应有.iter().any(|(p, _, _)| p == *n))
                .collect();
            多出.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
            for 名 in 多出 {
                问题们.push(format!("清单外多出：{名}"));
            }
            Ok(校验结果(
                问题们.is_empty(),
                问题们,
                清单成值从json(&解析),
            ))
        },
    }
}
