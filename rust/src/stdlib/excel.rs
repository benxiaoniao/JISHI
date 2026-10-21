//! 标准库 `Excel` 读（M123）。与 Python 侧 `oracle/jishi/stdlib/Excel.py` 同行为。
//!
//! `.xlsx` 是 **zip 包 + 一堆 XML**：工作表名在 `xl/workbook.xml`，
//! 每个表在 `xl/worksheets/sheetN.xml`，文本可能在 `xl/sharedStrings.xml`。
//!
//! ## 为什么不引 XML 库
//!
//! 只为读 xlsx 引一个 XML crate 不划算（依赖、编译时间、wasm 体积都涨）。
//! 而且 xlsx 里的 XML 是**机器生成**的：标签规整、属性永远带引号、没有 DTD、
//! 没有 CDATA（文本里的 `<` 会被转义成 `&lt;`）。⇒ 一个**按标签名扫片段**的
//! 小扫描器足够，且行为完全可预测（`scan.rs` 下面那几段）。
//!
//! ⚠️ 唯一的坑是**实体**：文本里会出现 `&amp;` `&lt;` `&gt;` `&quot;` `&apos;`，
//!    必须转回来，否则「A&B 公司」会读成字面的 `A&amp;B 公司`。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::{as_text, need_range, table, R};
use crate::{err, Val};

// ---------------------------------------------------------------------------
// 最小 XML 扫描
// ---------------------------------------------------------------------------

/// XML 实体还原。**只认这五个**（xlsx 里只会出现这五个；数字实体极罕见）。
fn 还原实体(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    let mut 出 = String::with_capacity(s.len());
    let 字节: Vec<char> = s.chars().collect();
    let mut i = 0;
    while i < 字节.len() {
        if 字节[i] == '&' {
            // 找分号；超过 10 个字符还没分号就当普通 `&`（别把整段吃掉）
            if let Some(相) = 字节[i..].iter().position(|c| *c == ';') {
                if 相 <= 10 {
                    let 名: String = 字节[i + 1..i + 相].iter().collect();
                    let 换 = match 名.as_str() {
                        "amp" => Some('&'),
                        "lt" => Some('<'),
                        "gt" => Some('>'),
                        "quot" => Some('"'),
                        "apos" => Some('\''),
                        _ => None,
                    };
                    if let Some(c) = 换 {
                        出.push(c);
                        i += 相 + 1;
                        continue;
                    }
                }
            }
        }
        出.push(字节[i]);
        i += 1;
    }
    出
}

/// 取 `名` 元素的**内容**片段（不含标签本身）。
///
/// 只找**第一个**：xlsx 里同名元素不会嵌套（`<row>` 里的 `<c>` 里没有 `<row>`）。
fn 首个内容<'a>(xml: &'a str, 名: &str) -> Option<&'a str> {
    let 开 = format!("<{名}");
    let 闭 = format!("</{名}>");
    let 起 = xml.find(&开)?;
    // 跳过开始标签本身（可能是 `<c r="A1">` 或 `<c/>`）
    let 头尾 = xml[起..].find('>')? + 起;
    if xml[起..=头尾].ends_with("/>") {
        return Some("");
    }
    let 尾 = xml[头尾..].find(&闭)? + 头尾;
    Some(&xml[头尾 + 1..尾])
}

/// 取所有 `名` 元素的**开始标签原文**（用于读属性）。
fn 开始标签们<'a>(xml: &'a str, 名: &str) -> Vec<&'a str> {
    let 开 = format!("<{名}");
    let mut 出 = Vec::new();
    let mut 起 = 0;
    while let Some(相) = xml[起..].find(&开) {
        let 头 = 起 + 相;
        // 边界：`<c` 不能匹配 `<cols`
        let 后 = xml[头 + 开.len()..].chars().next();
        if let Some(c) = 后 {
            if c.is_alphanumeric() || c == '_' || c == ':' {
                起 = 头 + 开.len();
                continue;
            }
        }
        if let Some(头尾) = xml[头..].find('>') {
            出.push(&xml[头..头 + 头尾 + 1]);
            起 = 头 + 头尾 + 1;
        } else {
            break;
        }
    }
    出
}

/// 取某个标签里属性的值：`<c r="A1" t="s">` 里取 `r` → `A1`。
fn 属性(标签: &str, 键: &str) -> Option<String> {
    let 样 = format!("{键}=\"");
    let 起 = 标签.find(&样)? + 样.len();
    let 尾 = 标签[起..].find('"')? + 起;
    Some(还原实体(&标签[起..尾]))
}

/// 取 `名` 元素的所有**完整片段**（`<row>…</row>` 这种需要成对取）。
fn 元素们<'a>(xml: &'a str, 名: &str) -> Vec<&'a str> {
    let 开 = format!("<{名}");
    let 闭 = format!("</{名}>");
    let mut 出 = Vec::new();
    let mut 起 = 0;
    while let Some(相) = xml[起..].find(&开) {
        let 头 = 起 + 相;
        let 后 = xml[头 + 开.len()..].chars().next();
        if let Some(c) = 后 {
            if c.is_alphanumeric() || c == '_' || c == ':' {
                起 = 头 + 开.len();
                continue;
            }
        }
        let Some(头尾) = xml[头..].find('>') else { break };
        let 内起 = 头 + 头尾 + 1;
        // 自闭合
        if xml[头..内起].ends_with("/>") {
            出.push(&xml[头..内起]);
            起 = 内起;
            continue;
        }
        match xml[内起..].find(&闭) {
            Some(相2) => {
                let 尾 = 内起 + 相2 + 闭.len();
                出.push(&xml[头..尾]);
                起 = 尾;
            }
            None => break,
        }
    }
    出
}

// ---------------------------------------------------------------------------
// xlsx 语义
// ---------------------------------------------------------------------------

fn 列号(引用: &str) -> usize {
    let mut 列: usize = 0;
    for ch in 引用.chars() {
        if ch.is_ascii_uppercase() {
            列 = 列 * 26 + (ch as usize - 'A' as usize + 1);
        } else if ch.is_ascii_lowercase() {
            列 = 列 * 26 + (ch as usize - 'a' as usize + 1);
        } else {
            break;
        }
    }
    列.saturating_sub(1)
}

/// 一个 `<c>` → 基石值。**没值就是 `空`**（不是空字符串）。
fn 单元格(片段: &str, 共享: &[String]) -> Val {
    let 开始 = 片段.split('>').next().unwrap_or("");
    let 类型 = 属性(开始, "t").unwrap_or_else(|| "n".to_string());

    if 类型 == "inlineStr" {
        // <is><t>…</t></is>（可能有多个 t，富文本分片）
        let 内 = 首个内容(片段, "is").unwrap_or("");
        let mut 拼 = String::new();
        for t in 元素们(内, "t") {
            if let Some(体) = 首个内容(t, "t") {
                拼.push_str(&还原实体(体));
            }
        }
        return Val::Str(拼);
    }

    let Some(v) = 首个内容(片段, "v") else {
        return Val::None_;
    };
    let 值 = 还原实体(v);
    match 类型.as_str() {
        "s" => match 值.trim().parse::<usize>() {
            Ok(序) => 共享
                .get(序)
                .map(|s| Val::Str(s.clone()))
                .unwrap_or_else(|| Val::Str(String::new())),
            Err(_) => Val::Str(值),
        },
        "b" => {
            let t = 值.trim();
            Val::Bool(!(t == "0" || t.is_empty() || t.eq_ignore_ascii_case("false")))
        }
        "str" | "e" => Val::Str(值),
        _ => {
            // 数字：整数给整数（`42` 不该变 `42.0`），否则小数
            if 值.contains('.') || 值.contains('e') || 值.contains('E') {
                match 值.trim().parse::<f64>() {
                    Ok(f) => Val::Float(f),
                    Err(_) => Val::Str(值),
                }
            } else {
                match 值.trim().parse::<i128>() {
                    Ok(i) => Val::Int(i),
                    Err(_) => match 值.trim().parse::<f64>() {
                        Ok(f) => Val::Float(f),
                        Err(_) => Val::Str(值),
                    },
                }
            }
        }
    }
}

/// 共享字符串表（`<si>` 里可能有多个 `<t>`，拼起来别丢内容）。
fn 共享串(xml: &str) -> Vec<String> {
    let mut 出 = Vec::new();
    for si in 元素们(xml, "si") {
        let mut 拼 = String::new();
        for t in 元素们(si, "t") {
            if let Some(体) = 首个内容(t, "t") {
                拼.push_str(&还原实体(体));
            }
        }
        出.push(拼);
    }
    出
}

/// 工作簿里的工作表：`[(名字, 包内路径)]`，**顺序就是标签页顺序**。
fn 工作表们(条们: &[(String, Vec<u8>)]) -> Result<Vec<(String, String)>, String> {
    let 取 = |名: &str| {
        条们
            .iter()
            .find(|(n, _)| n == 名)
            .map(|(_, d)| String::from_utf8_lossy(d).into_owned())
    };
    let 原 = 取("xl/workbook.xml").ok_or("这不是一个 .xlsx 工作簿（里面没有 xl/workbook.xml）")?;

    // r:id → 目标路径
    let mut 关系: HashMap<String, String> = HashMap::new();
    if let Some(rels) = 取("xl/_rels/workbook.xml.rels") {
        for 片段 in 元素们(&rels, "Relationship") {
            let 开始 = 片段.split('>').next().unwrap_or("");
            let (Some(id), Some(目标)) = (属性(开始, "Id"), 属性(开始, "Target")) else {
                continue;
            };
            let 路径 = if let Some(stripped) = 目标.strip_prefix('/') {
                stripped.to_string()
            } else {
                format!("xl/{目标}")
            };
            关系.insert(id, 路径.replace("/./", "/"));
        }
    }

    let mut 出 = Vec::new();
    for 片段 in 元素们(&原, "sheet") {
        let 开始 = 片段.split('>').next().unwrap_or("");
        let Some(名字) = 属性(开始, "name") else { continue };
        // `r:id` 的前缀不一定叫 r ⇒ 找以 `:id` 结尾的属性
        let mut 路径 = String::new();
        for 键 in ["r:id", "id"] {
            if let Some(v) = 属性(开始, 键) {
                if let Some(p) = 关系.get(&v) {
                    路径 = p.clone();
                    break;
                }
            }
        }
        if 路径.is_empty() {
            // 没有 rels 的（极简生成器）⇒ 按位置猜
            路径 = format!("xl/worksheets/sheet{}.xml", 出.len() + 1);
        }
        出.push((名字, 路径));
    }
    Ok(出)
}

// ---------------------------------------------------------------------------
// 模块
// ---------------------------------------------------------------------------

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "Excel";
        "读工作簿" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("读工作簿", args, 1, 1)?;
            let p = as_text(&args[0]);
            let 字节 = std::fs::read(&p).map_err(|e| {
                if e.kind() == std::io::ErrorKind::NotFound {
                    err("运行错误", format!("找不到文件「{p}」"))
                } else {
                    err("运行错误", format!("打不开文件「{p}」"))
                }
            })?;
            let 条们 = crate::zip::read_all(&字节)
                .map_err(|_| err("值错误", format!(
                    "「{p}」不是一个 .xlsx（Excel 的 .xlsx 是 zip 包；老的 .xls 不支持）")))?;
            let 表们 = 工作表们(&条们).map_err(|m| err("值错误", m))?;
            Ok(crate::dict_new(vec![
                (Val::Str("路径".into()), Val::Str(p)),
                (Val::Str("工作表们".into()),
                 crate::list_new(表们.into_iter().map(|(n, _)| Val::Str(n)).collect())),
            ]))
        },

        "读工作表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("读工作表", args, 2, 2)?;
            let 路径 = match &args[0] {
                Val::Dict(d) => {
                    match crate::dict_get(&d.borrow(), &Val::Str("路径".into())) {
                        Some(Val::Str(s)) => s,
                        _ => return Err(err("类型错误", "「簿」要是 `读工作簿(…)` 的返回值".to_string())),
                    }
                }
                _ => return Err(err("类型错误", "「簿」要是 `读工作簿(…)` 的返回值".to_string())),
            };
            let 名 = as_text(&args[1]);
            let 字节 = std::fs::read(&路径)
                .map_err(|_| err("运行错误", format!("找不到文件「{路径}」")))?;
            let 条们 = crate::zip::read_all(&字节)
                .map_err(|_| err("值错误", format!("「{路径}」不是一个 .xlsx")))?;
            let 表们 = 工作表们(&条们).map_err(|m| err("值错误", m))?;

            let Some((_, 表路径)) = 表们.iter().find(|(n, _)| *n == 名) else {
                let 可用: Vec<String> = 表们.iter().map(|(n, _)| format!("「{n}」")).collect();
                let 列 = if 可用.is_empty() { "（一个都没有）".to_string() } else { 可用.join("、") };
                return Err(err("值错误", format!("没有叫「{名}」的工作表。这个工作簿里有：{列}")));
            };
            let Some((_, 数据)) = 条们.iter().find(|(n, _)| n == 表路径) else {
                return Err(err("值错误", format!("工作簿里找不到「{名}」的内容（{表路径}）")));
            };
            let xml = String::from_utf8_lossy(数据).into_owned();
            let 共享 = 条们
                .iter()
                .find(|(n, _)| n == "xl/sharedStrings.xml")
                .map(|(_, d)| 共享串(&String::from_utf8_lossy(d)))
                .unwrap_or_default();

            let 行们 = 元素们(&xml, "row");
            if 行们.is_empty() {
                return Ok(Val::Table(std::rc::Rc::new(super::tables::Table {
                    headers: Vec::new(),
                    rows: Vec::new(),
                })));
            }

            // 列数 = 所有行里最宽的那行（表头短了也不丢数据）
            let mut 列数 = 0usize;
            for 行 in &行们 {
                for c in 元素们(行, "c") {
                    if let Some(r) = 属性(c.split('>').next().unwrap_or(""), "r") {
                        列数 = 列数.max(列号(&r) + 1);
                    }
                }
            }
            if 列数 == 0 {
                列数 = 1;
            }

            let mut 数据行们: Vec<Vec<Val>> = Vec::new();
            for 行 in &行们 {
                let mut 行值: Vec<Val> = vec![Val::None_; 列数];
                let mut 顺位 = 0usize;
                for c in 元素们(行, "c") {
                    let 开始 = c.split('>').next().unwrap_or("");
                    let 列 = match 属性(开始, "r") {
                        Some(r) => 列号(&r),
                        None => {
                            // 没有 `r` 的极简生成器 ⇒ 顺着放
                            let mut i = 顺位;
                            while i < 列数 && !matches!(行值[i], Val::None_) {
                                i += 1;
                            }
                            i
                        }
                    };
                    顺位 = 列 + 1;
                    if 列 < 列数 {
                        行值[列] = 单元格(c, &共享);
                    }
                }
                数据行们.push(行值);
            }

            // ⚠️ `Table.headers` 是 `Vec<Val>`（不是字符串）：表头也要保持**值的类型**，
            //    否则 `表格.按列取` 之类的按名字找列时，数字表头就对不上了。
            let 表头: Vec<Val> = 数据行们[0]
                .iter()
                .map(|v| match v {
                    Val::None_ => Val::Str(String::new()),
                    other => other.clone(),
                })
                .collect();
            let 体: Vec<Vec<Val>> = 数据行们.into_iter().skip(1).collect();
            Ok(Val::Table(std::rc::Rc::new(super::tables::Table {
                headers: 表头,
                rows: 体,
            })))
        },
    }
}
