//! 标准库 `表格`（Python 侧 `jishi/stdlib/表格.py`，14 个函数）。
//!
//! CSV 的读写**自己写**（没有 `csv` crate）。要对齐的是 Python `csv` 模块的
//! **默认方言**，有两个容易漏的点：
//!
//! 1. **写出去的行分隔符是 `\r\n`**（RFC 4180 规定的，不是平台相关）；
//!    而且文件开头有 **UTF-8 BOM**（Python 侧用 `utf-8-sig`，Excel 靠它认中文）。
//! 2. **空行读出来是空列表而不是 `[""]`**：Python 的
//!    `list(csv.reader(["\\n"]))` 给 `[[]]`。
//!
//! `筛选` 是**按文本比**的：`筛选(t, "分数", 92)` 匹配不到 `"92"`
//! （Python 侧 `"92" == 92` 就是假）。这不是缺陷而是如实对齐——
//! 想按数字比就先 `整数(...)` 转换再比。`排序按` 则是**数值感知**的。

use crate::BuiltinHost;
use std::collections::HashMap;
use std::rc::Rc;

use super::{as_text, need, need_range, table, to_vec, R};
use crate::{display, err, JishiError, Val};
use super::text::display_width;

/// 表格对象：表头 + 数据行。
///
/// `Val` 里专门给它留了一支（`Val::Table`）：显示成 `<表格 N 行 x M 列>`、
/// `类型()` 给 `表格`。用字典糊弄的话这两处就都不对了。
#[derive(Clone)]
pub(crate) struct Table {
    pub(crate) headers: Vec<Val>,
    pub(crate) rows: Vec<Vec<Val>>,
}

impl Table {
    pub(crate) fn display(&self) -> String {
        format!("<表格 {} 行 x {} 列>", self.rows.len(), self.headers.len())
    }
}

fn table_new(headers: Vec<Val>, rows: Vec<Vec<Val>>) -> Val {
    Val::Table(Rc::new(Table { headers, rows }))
}

fn as_table(v: &Val) -> Result<Rc<Table>, JishiError> {
    match v {
        Val::Table(t) => Ok(t.clone()),
        _ => Err(err(
            "类型错误",
            format!("需要一个表格，但得到了「{}」", crate::display(v)),
        )),
    }
}

// ---------------------------------------------------------------------------
// CSV
// ---------------------------------------------------------------------------

/// Python `csv.reader` 的默认方言。
pub(crate) fn parse_csv(text: &str) -> Vec<Vec<String>> {
    let chars: Vec<char> = text.chars().collect();
    let mut rows: Vec<Vec<String>> = Vec::new();
    let mut row: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut in_quotes = false;
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if in_quotes {
            if c == '"' {
                if i + 1 < chars.len() && chars[i + 1] == '"' {
                    field.push('"');
                    i += 2;
                    continue;
                }
                in_quotes = false;
                i += 1;
                continue;
            }
            field.push(c);
            i += 1;
            continue;
        }
        match c {
            '"' if field.is_empty() => {
                in_quotes = true;
                i += 1;
            }
            ',' => {
                row.push(std::mem::take(&mut field));
                i += 1;
            }
            '\r' | '\n' => {
                if c == '\r' && i + 1 < chars.len() && chars[i + 1] == '\n' {
                    i += 1;
                }
                // 整行什么都没有 → 空行，Python 给的是 `[]`（不是 `[""]`）
                if field.is_empty() && row.is_empty() {
                    rows.push(Vec::new());
                } else {
                    row.push(std::mem::take(&mut field));
                    rows.push(std::mem::take(&mut row));
                }
                i += 1;
            }
            _ => {
                field.push(c);
                i += 1;
            }
        }
    }
    if !field.is_empty() || !row.is_empty() {
        row.push(field);
        rows.push(row);
    }
    rows
}

/// Python `csv.writer` 的默认方言（QUOTE_MINIMAL + `\r\n`）。
fn quote_if_needed(field: &str) -> String {
    let needs = field.contains(',')
        || field.contains('"')
        || field.contains('\n')
        || field.contains('\r');
    if needs {
        format!("\"{}\"", field.replace('"', "\"\""))
    } else {
        field.to_string()
    }
}

fn csv_row(fields: &[String]) -> String {
    let mut line = fields
        .iter()
        .map(|f| quote_if_needed(f))
        .collect::<Vec<_>>()
        .join(",");
    line.push_str("\r\n");
    line
}

const BOM: &[u8] = &[0xEF, 0xBB, 0xBF];

fn strip_bom(s: &str) -> &str {
    s.strip_prefix('\u{feff}').unwrap_or(s)
}

// ---------------------------------------------------------------------------
// 表操作
// ---------------------------------------------------------------------------

fn column_index(t: &Table, name: &Val) -> Result<usize, JishiError> {
    let target = as_text(name);
    t.headers
        .iter()
        .position(|h| as_text(h) == target)
        .ok_or_else(|| {
            let names: Vec<String> = t.headers.iter().map(as_text).collect();
            err(
                "类型错误",
                format!(
                    "表格里没有「{target}」这一列（现有的列：{}）",
                    names.join("、")
                ),
            )
        })
}

/// 排序键：能转成小数就按数字排，否则按文本排（数字整体排在文本前面）。
fn sort_key(v: &Val) -> (u8, f64, String) {
    let s = as_text(v);
    match s.trim().parse::<f64>() {
        Ok(f) => (0, f, String::new()),
        Err(_) => (1, 0.0, s),
    }
}

/// 取出表格参数（M120 的两个渲染函数共用）。
fn 取表格(v: &Val, 谁: &str) -> Result<Rc<Table>, JishiError> {
    match v {
        Val::Table(t) => Ok(Rc::clone(t)),
        other => Err(err(
            "类型错误",
            format!(
                "「{谁}」要传一个表格（现在是「{}」）—— 用 表格.读表格 或 表格.从记录 得到",
                display(other)
            ),
        )),
    }
}

/// 按**显示宽度**补空格（汉字算 2 列，与 `文本.显示宽度` 同口径）。
fn 补齐(s: &str, 宽: usize, 靠右: bool) -> String {
    let 差 = 宽.saturating_sub(display_width(s));
    if 差 == 0 {
        s.to_string()
    } else if 靠右 {
        format!("{}{}", " ".repeat(差), s)
    } else {
        format!("{}{}", s, " ".repeat(差))
    }
}

/// 表格 → 对齐文本：表头 + 分隔线 + 数据行。数字列右对齐。
fn 转对齐文本(t: &Table) -> String {
    let 列数 = t.headers.len();
    if 列数 == 0 {
        return String::new();
    }
    let 头: Vec<String> = t.headers.iter().map(display).collect();
    let 行们: Vec<Vec<String>> = t
        .rows
        .iter()
        .map(|r| r.iter().map(display).collect())
        .collect();
    let mut 宽: Vec<usize> = 头.iter().map(|x| display_width(x)).collect();
    let mut 靠右 = vec![false; 列数];
    for i in 0..列数 {
        let mut 有值 = false;
        let mut 全数字 = true;
        for r in &t.rows {
            let Some(v) = r.get(i) else { continue };
            match v {
                Val::None_ => continue,
                Val::Str(s) if s.is_empty() => continue,
                v => {
                    有值 = true;
                    if !matches!(v, Val::Int(_) | Val::Float(_)) {
                        全数字 = false;
                    }
                }
            }
        }
        靠右[i] = 有值 && 全数字;
    }
    for r in &行们 {
        for i in 0..列数.min(r.len()) {
            宽[i] = 宽[i].max(display_width(&r[i]));
        }
    }
    let 拼 = |格们: Vec<String>| 格们.join("  ").trim_end().to_string();
    let mut 出: Vec<String> = Vec::with_capacity(t.rows.len() + 2);
    出.push(拼((0..列数).map(|i| 补齐(&头[i], 宽[i], 靠右[i])).collect()));
    出.push(拼((0..列数).map(|i| "-".repeat(宽[i])).collect()));
    for r in &行们 {
        出.push(拼(
            (0..列数)
                .map(|i| {
                    补齐(
                        r.get(i).map(|x| x.as_str()).unwrap_or(""),
                        宽[i],
                        靠右[i],
                    )
                })
                .collect(),
        ));
    }
    出.join("\n")
}

/// 表格 → TSV 文本。单元格里含制表符/换行时**报错**，不静默改内容。
fn 转TSV文本(t: &Table) -> Result<String, JishiError> {
    let mut 出: Vec<String> = Vec::with_capacity(t.rows.len() + 1);
    for (号, r) in std::iter::once(&t.headers).chain(t.rows.iter()).enumerate() {
        let mut 元: Vec<String> = Vec::with_capacity(r.len());
        for (列, c) in r.iter().enumerate() {
            let s = display(c);
            if s.contains('\t') || s.contains('\n') || s.contains('\r') {
                return Err(err(
                    "类型错误",
                    format!(
                        "第 {} 行第 {} 格里含制表符或换行，TSV 装不下 —— 要写这种内容请用 表格.写表格 存成 CSV",
                        号 + 1,
                        列 + 1
                    ),
                ));
            }
            元.push(s);
        }
        出.push(元.join("\t"));
    }
    Ok(出.join("\n"))
}

// ---------------------------------------------------------------------------
// M192：数据分析（透视 / 多列分组 / 宽长表 / 缺失值）
// ---------------------------------------------------------------------------
//
// 与 Python 侧 `jishi/stdlib/表格.py` 逐句对齐。三条共用约定：
// 1. **列参数可以给一个列名，也可以给列名列表**；
// 2. **「缺失」= 空文本 或 `空`**（`0` / `假` 不算 —— 最容易搞错的地方）；
// 3. **都返回新表格，不改原表**。

/// `透视` / `按列分组` 认的聚合方式（与 `汇总` 的五个口径对齐）。
const 聚合方式们: [&str; 5] = ["求和", "平均", "计数", "最大", "最小"];

/// 这一格算不算「没有值」。
fn 缺失(v: &Val) -> bool {
    matches!(v, Val::None_) || matches!(v, Val::Str(s) if s.is_empty())
}

/// 列名 → 下标（`column_index` 只收 `&Val`，这里收 `&str`）。
fn 列号(t: &Table, 名: &str) -> Result<usize, JishiError> {
    t.headers
        .iter()
        .position(|h| as_text(h) == 名)
        .ok_or_else(|| {
            let 有: Vec<String> = t.headers.iter().map(as_text).collect();
            err(
                "类型错误",
                format!("找不到列「{名}」，现有列：{}", 有.join(" / ")),
            )
        })
}

/// 「一个列名 或 列名列表」→ `Vec<String>`，逐个核对存在。
fn 列们(t: &Table, v: &Val, 谁: &str) -> Result<Vec<String>, JishiError> {
    let 名们: Vec<String> = match v {
        Val::Str(_) => vec![as_text(v)],
        _ => to_vec(v)?.iter().map(as_text).collect(),
    };
    if 名们.is_empty() {
        return Err(err("类型错误", format!("「{谁}」至少要给一个列名")));
    }
    for 名 in &名们 {
        列号(t, 名)?;
    }
    Ok(名们)
}

fn 要聚合(v: &Val, 谁: &str) -> Result<String, JishiError> {
    let s = as_text(v);
    if !聚合方式们.contains(&s.as_str()) {
        return Err(err(
            "类型错误",
            format!(
                "「{谁}」不认识的聚合方式「{s}」—— 只能是：{}",
                聚合方式们.join(" / ")
            ),
        ));
    }
    Ok(s)
}

/// 把一批格子转成数字（缺失跳过，不参与计算 —— 不是 0）。
fn 数值们(值们: &[Val], 列名: &str, 谁: &str) -> Result<Vec<f64>, JishiError> {
    let mut 出 = Vec::with_capacity(值们.len());
    for v in 值们 {
        if 缺失(v) {
            continue;
        }
        let f = match v {
            Val::Int(i) => *i as f64,
            Val::Float(f) => *f,
            Val::Bool(b) => {
                if *b {
                    1.0
                } else {
                    0.0
                }
            }
            // 📌 从 CSV 读进来的格子全是**文本**，所以这条分支是主路
            Val::Str(s) => s.trim().parse::<f64>().map_err(|_| {
                err(
                    "类型错误",
                    format!(
                        "「{谁}」：列「{列名}」里有不是数字的值「{}」，没法聚合",
                        display(v)
                    ),
                )
            })?,
            other => {
                return Err(err(
                    "类型错误",
                    format!(
                        "「{谁}」：列「{列名}」里有不是数字的值「{}」，没法聚合",
                        display(other)
                    ),
                ))
            }
        };
        出.push(f);
    }
    Ok(出)
}

/// 按名字聚合。**空批次**：`计数` 给 0，其余给 `None`（调用方写成空格）。
fn 聚合(
    值们: &[Val],
    方式: &str,
    列名: &str,
    谁: &str,
) -> Result<Option<Val>, JishiError> {
    if 方式 == "计数" {
        return Ok(Some(Val::Int(
            值们.iter().filter(|v| !缺失(v)).count() as i128
        )));
    }
    let 数 = 数值们(值们, 列名, 谁)?;
    if 数.is_empty() {
        return Ok(None);
    }
    let 果 = match 方式 {
        "求和" => 数.iter().sum::<f64>(),
        "平均" => 数.iter().sum::<f64>() / 数.len() as f64,
        "最大" => 数.iter().copied().fold(f64::NEG_INFINITY, f64::max),
        _ => 数.iter().copied().fold(f64::INFINITY, f64::min),
    };
    Ok(Some(Val::Float(果)))
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "表格";
        "读表格" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("读表格", args, 1)?;
            let p = as_text(&args[0]);
            let bytes = std::fs::read(&p)
                .map_err(|_| err("文件错误", format!("找不到文件「{p}」")))?;
            let text = String::from_utf8_lossy(&bytes).to_string();
            let rows = parse_csv(strip_bom(&text));
            if rows.is_empty() {
                return Err(err("文件错误", format!("文件「{p}」是空的，没有表头")));
            }
            let headers: Vec<Val> = rows[0].iter().map(|s| Val::Str(s.clone().into())).collect();
            let data: Vec<Vec<Val>> = rows[1..]
                .iter()
                .map(|r| r.iter().map(|s| Val::Str(s.clone().into())).collect())
                .collect();
            Ok(table_new(headers, data))
        },
        "写表格" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("写表格", args, 2)?;
            let p = as_text(&args[0]);
            let rows = to_vec(&args[1])?;
            let mut out: Vec<u8> = BOM.to_vec();
            for r in &rows {
                let fields: Vec<String> = to_vec(r)?.iter().map(as_text).collect();
                out.extend_from_slice(csv_row(&fields).as_bytes());
            }
            std::fs::write(&p, &out)
                .map_err(|_| err("文件错误", format!("没法写入文件「{p}」")))?;
            Ok(Val::None_)
        },
        "写字典表格" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("写字典表格", args, 2)?;
            let p = as_text(&args[0]);
            let items = to_vec(&args[1])?;
            if items.is_empty() {
                return Err(err("类型错误", "「写字典表格」需要一个非空的字典列表"));
            }
            let first = match &items[0] {
                Val::Dict(d) => d.borrow().clone(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!("列表里的元素应该是字典，但出现了「{}」", crate::display(other)),
                    ))
                }
            };
            let headers: Vec<Val> = first.iter().map(|(k, _)| k.clone()).collect();
            let mut out: Vec<u8> = BOM.to_vec();
            out.extend_from_slice(csv_row(&headers.iter().map(as_text).collect::<Vec<_>>()).as_bytes());
            for item in &items {
                let d = match item {
                    Val::Dict(d) => d.borrow().clone(),
                    _ => return Err(err("类型错误", "列表里的元素应该是字典")),
                };
                let mut fields: Vec<String> = Vec::new();
                for h in &headers {
                    let found = d.iter().find(|(k, _)| k == h).map(|(_, v)| v.clone());
                    fields.push(found.map(|v| as_text(&v)).unwrap_or_default());
                }
                out.extend_from_slice(csv_row(&fields).as_bytes());
            }
            std::fs::write(&p, &out)
                .map_err(|_| err("文件错误", format!("没法写入文件「{p}」")))?;
            Ok(Val::None_)
        },
        "读字典表格" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("读字典表格", args, 1)?;
            let p = as_text(&args[0]);
            let bytes = std::fs::read(&p)
                .map_err(|_| err("文件错误", format!("找不到文件「{p}」")))?;
            let text = String::from_utf8_lossy(&bytes).to_string();
            let rows = parse_csv(strip_bom(&text));
            if rows.is_empty() {
                return Ok(crate::list_new(Vec::new()));
            }
            let headers: Vec<String> = rows[0].clone();
            let mut out: Vec<Val> = Vec::new();
            for r in &rows[1..] {
                let mut pairs: Vec<(Val, Val)> = Vec::new();
                for (i, h) in headers.iter().enumerate() {
                    pairs.push((
                        Val::Str(h.clone().into()),
                        Val::Str(r.get(i).cloned().unwrap_or_default().into()),
                    ));
                }
                out.push(crate::dict_new(pairs));
            }
            Ok(crate::list_new(out))
        },
        "追加" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("追加", args, 2)?;
            let p = as_text(&args[0]);
            let fields: Vec<String> = to_vec(&args[1])?.iter().map(as_text).collect();
            let mut out: Vec<u8> = Vec::new();
            // Python 的 `utf-8-sig` 在**空文件**上会写 BOM，非空则不写
            let existing = std::fs::metadata(&p).map(|m| m.len()).unwrap_or(0);
            if existing == 0 {
                out.extend_from_slice(BOM);
            }
            out.extend_from_slice(csv_row(&fields).as_bytes());
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&p)
                .map_err(|_| err("文件错误", format!("没法写入文件「{p}」")))?;
            f.write_all(&out)
                .map_err(|_| err("文件错误", format!("没法写入文件「{p}」")))?;
            Ok(Val::None_)
        },
        "表头" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("表头", args, 1)?;
            let t = as_table(&args[0])?;
            Ok(crate::list_new(t.headers.clone()))
        },
        "数据行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("数据行", args, 1)?;
            let t = as_table(&args[0])?;
            let out: Vec<Val> = t
                .rows
                .iter()
                .map(|r| crate::list_new(r.clone()))
                .collect();
            Ok(crate::list_new(out))
        },
        "挑选" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("挑选", args, 2)?;
            let t = as_table(&args[0])?;
            if matches!(args[1], Val::List(_)) {
                let cols = to_vec(&args[1])?;
                let mut idxs = Vec::new();
                for c in &cols {
                    idxs.push(column_index(&t, c)?);
                }
                let out: Vec<Val> = t
                    .rows
                    .iter()
                    .map(|r| {
                        crate::list_new(
                            idxs.iter()
                                .map(|i| r.get(*i).cloned().unwrap_or(Val::None_))
                                .collect(),
                        )
                    })
                    .collect();
                return Ok(crate::list_new(out));
            }
            let i = column_index(&t, &args[1])?;
            let out: Vec<Val> = t
                .rows
                .iter()
                .map(|r| r.get(i).cloned().unwrap_or(Val::None_))
                .collect();
            Ok(crate::list_new(out))
        },
        "筛选" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("筛选", args, 3)?;
            let t = as_table(&args[0])?;
            let i = column_index(&t, &args[1])?;
            // 按**文本**比：与 Python 的 `row[i] == 值` 一致
            let target = args[2].clone();
            let rows: Vec<Vec<Val>> = t
                .rows
                .iter()
                .filter(|r| r.get(i).cloned().unwrap_or(Val::None_) == target)
                .cloned()
                .collect();
            Ok(table_new(t.headers.clone(), rows))
        },
        "排序按" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("排序按", args, 2, 3)?;
            let t = as_table(&args[0])?;
            let i = column_index(&t, &args[1])?;
            let reverse = matches!(args.get(2), Some(Val::Bool(true)));
            let mut rows = t.rows.clone();
            // 稳定排序（Python 的 `sorted` 也是稳定的）。
            //
            // 倒序要在**比较器里**反向，不能「排完再整体反过来」：
            // Python 的 `sorted(..., reverse=True)` 对并列项是**保持原顺序**的，
            // 排完再 reverse 会把并列项的次序也倒过来（M33 抓到的偏差）。
            rows.sort_by(|a, b| {
                let ka = sort_key(a.get(i).unwrap_or(&Val::None_));
                let kb = sort_key(b.get(i).unwrap_or(&Val::None_));
                let ord = ka.partial_cmp(&kb).unwrap_or(std::cmp::Ordering::Equal);
                if reverse {
                    ord.reverse()
                } else {
                    ord
                }
            });
            Ok(table_new(t.headers.clone(), rows))
        },
        "汇总" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("汇总", args, 2)?;
            let t = as_table(&args[0])?;
            let i = column_index(&t, &args[1])?;
            let mut values: Vec<f64> = Vec::new();
            for r in &t.rows {
                let cell = r.get(i).cloned().unwrap_or(Val::None_);
                let s = as_text(&cell);
                match s.trim().parse::<f64>() {
                    Ok(f) => values.push(f),
                    Err(_) => {
                        return Err(err(
                            "类型错误",
                            format!("列「{}」里有不是数字的值「{s}」，没法汇总", as_text(&args[1])),
                        ))
                    }
                }
            }
            if values.is_empty() {
                return Err(err(
                    "类型错误",
                    format!("表格没有数据行，没法汇总「{}」", as_text(&args[1])),
                ));
            }
            let count = values.len();
            let total: f64 = values.iter().sum();
            let avg = total / count as f64;
            let max = values.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let min = values.iter().cloned().fold(f64::INFINITY, f64::min);
            let pairs = vec![
                (Val::Str("个数".into()), Val::Int(count as i128)),
                (Val::Str("总和".into()), Val::Float(total)),
                (Val::Str("平均".into()), Val::Float(avg)),
                (Val::Str("最大".into()), Val::Float(max)),
                (Val::Str("最小".into()), Val::Float(min)),
            ];
            Ok(crate::dict_new(pairs))
        },
        // M106 补：`分组汇总` / `连接`（与 Python 侧 `表格.py` 同行为）
        "分组汇总" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("分组汇总", args, 3)?;
            let t = as_table(&args[0])?;
            let ig = column_index(&t, &args[1])?;
            let iv = column_index(&t, &args[2])?;
            let mut buckets: Vec<(Val, Vec<f64>)> = Vec::new();
            // ⚠️ 用「单元格的显示文本」当归并键：CSV 读出来的列**全是文本**，
            // 所以与 Python 侧「拿原值当键」在实际数据上一致。
            let mut at: HashMap<String, usize> = HashMap::new();
            for r in &t.rows {
                let k = r.get(ig).cloned().unwrap_or(Val::None_);
                let cell = r.get(iv).cloned().unwrap_or(Val::None_);
                let s = as_text(&cell);
                let f = s.trim().parse::<f64>().map_err(|_| {
                    err(
                        "类型错误",
                        format!(
                            "列「{}」里有不是数字的值「{s}」，没法汇总",
                            as_text(&args[2])
                        ),
                    )
                })?;
                let key = as_text(&k);
                let idx = match at.get(&key) {
                    Some(i) => *i,
                    None => {
                        at.insert(key, buckets.len());
                        buckets.push((k, Vec::new()));
                        buckets.len() - 1
                    }
                };
                buckets[idx].1.push(f);
            }
            let mut out: Vec<(Val, Val)> = Vec::new();
            for (k, values) in buckets {
                let count = values.len();
                let total: f64 = values.iter().sum();
                out.push((
                    k,
                    crate::dict_new(vec![
                        (Val::Str("个数".into()), Val::Int(count as i128)),
                        (Val::Str("总和".into()), Val::Float(total)),
                        (Val::Str("平均".into()), Val::Float(total / count as f64)),
                        (
                            Val::Str("最大".into()),
                            Val::Float(values.iter().cloned().fold(f64::NEG_INFINITY, f64::max)),
                        ),
                        (
                            Val::Str("最小".into()),
                            Val::Float(values.iter().cloned().fold(f64::INFINITY, f64::min)),
                        ),
                    ]),
                ));
            }
            Ok(crate::dict_new(out))
        },
        "连接" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("连接", args, 3, 4)?;
            let a = as_table(&args[0])?;
            let b = as_table(&args[1])?;
            let ia = column_index(&a, &args[2])?;
            let kb = if args.len() > 3 { args[3].clone() } else { args[2].clone() };
            let ib = column_index(&b, &kb)?;
            // 乙表按键建索引（同一个键只留**第一条**）
            let mut idx: HashMap<String, usize> = HashMap::new();
            for (j, r) in b.rows.iter().enumerate() {
                let k = as_text(&r.get(ib).cloned().unwrap_or(Val::None_));
                idx.entry(k).or_insert(j);
            }
            let mut headers = a.headers.clone();
            for (j, h) in b.headers.iter().enumerate() {
                if j != ib {
                    headers.push(h.clone());
                }
            }
            let mut rows: Vec<Vec<Val>> = Vec::new();
            for r in &a.rows {
                let k = as_text(&r.get(ia).cloned().unwrap_or(Val::None_));
                if let Some(&j) = idx.get(&k) {
                    let mut row = r.clone();
                    for (m, v) in b.rows[j].iter().enumerate() {
                        if m != ib {
                            row.push(v.clone());
                        }
                    }
                    rows.push(row);
                }
            }
            Ok(table_new(headers, rows))
        },
        // 🔴 M120：`转文本` 给人看（对齐表），`转TSV` 给程序读。
        //    **两者末尾都不带换行**（与 `文本.拼接` 同一习惯，`打印` 会补一个）。
        "转文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("转文本", args, 1, 1)?;
            let t = 取表格(&args[0], "转文本")?;
            Ok(Val::Str(转对齐文本(&t).into()))
        },
        "转TSV" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("转TSV", args, 1, 1)?;
            let t = 取表格(&args[0], "转TSV")?;
            Ok(Val::Str(转TSV文本(&t)?.into()))
        },
        "转置" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("转置", args, 1)?;
            let rows = to_vec(&args[0])?;
            if rows.is_empty() {
                return Ok(crate::list_new(Vec::new()));
            }
            let mut cols: Vec<Vec<Val>> = Vec::new();
            for r in &rows {
                let items = to_vec(r)?;
                for (i, cell) in items.into_iter().enumerate() {
                    while cols.len() <= i {
                        cols.push(Vec::new());
                    }
                    cols[i].push(cell);
                }
            }
            Ok(crate::list_new(
                cols.into_iter().map(crate::list_new).collect(),
            ))
        },
        // M107：字典列表 ↔ 表格（JSON 接口回来的数据直接能做表格运算）
        "从记录" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("从记录", args, 1)?;
            let recs = to_vec(&args[0])?;
            let mut cols: Vec<Val> = Vec::new();
            let mut dicts: Vec<Vec<(Val, Val)>> = Vec::with_capacity(recs.len());
            for rec in &recs {
                let d = match rec {
                    Val::Dict(d) => d.borrow().to_items(),
                    other => {
                        return Err(err(
                            "类型错误",
                            format!("「从记录」的每一项都要是字典，遇到了「{}」",
                                    crate::type_label(other)),
                        ))
                    }
                };
                for (k, _) in &d {
                    if !cols.iter().any(|c| c == k) {
                        cols.push(k.clone());
                    }
                }
                dicts.push(d);
            }
            let headers: Vec<Val> = cols.iter().map(|k| Val::Str(as_text(k).into())).collect();
            let mut rows: Vec<Vec<Val>> = Vec::with_capacity(dicts.len());
            for d in &dicts {
                let mut row: Vec<Val> = Vec::with_capacity(cols.len());
                for c in &cols {
                    row.push(d.iter().find(|(k, _)| k == c)
                        .map(|(_, v)| v.clone()).unwrap_or(Val::None_));
                }
                rows.push(row);
            }
            Ok(table_new(headers, rows))
        },
        "转记录" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("转记录", args, 1)?;
            let t = as_table(&args[0])?;
            let mut out: Vec<Val> = Vec::with_capacity(t.rows.len());
            for row in &t.rows {
                let pairs: Vec<(Val, Val)> = t.headers.iter().cloned()
                    .zip(row.iter().cloned()).collect();
                out.push(crate::dict_new(pairs));
            }
            Ok(crate::list_new(out))
        },
        // ── M192 ──
        "透视" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("透视", args, 4, 5)?;
            let t = as_table(&args[0])?;
            let i行 = column_index(&t, &args[1])?;
            let i列 = column_index(&t, &args[2])?;
            let i值 = column_index(&t, &args[3])?;
            let 值列名 = as_text(&args[3]);
            let 方式 = if args.len() > 4 {
                要聚合(&args[4], "透视")?
            } else {
                "求和".to_string()
            };

            let mut 行序: Vec<Val> = Vec::new();
            let mut 列序: Vec<Val> = Vec::new();
            // 键是 `(行, 列)` 两个 `Val` —— `Val` 没实现 `Hash`（里面有 f64），
            // 所以用**线性查找**。表格行数不大，与 Python 侧的 list 查找同阶。
            let mut 桶: Vec<((Val, Val), Vec<Val>)> = Vec::new();
            for row in t.rows.iter() {
                let r = row.get(i行).cloned().unwrap_or(Val::None_);
                let c = row.get(i列).cloned().unwrap_or(Val::None_);
                if !行序.iter().any(|x| x == &r) {
                    行序.push(r.clone());
                }
                if !列序.iter().any(|x| x == &c) {
                    列序.push(c.clone());
                }
                let 值 = row.get(i值).cloned().unwrap_or(Val::None_);
                let 键 = (r, c);
                if let Some((_, v)) = 桶.iter_mut().find(|(k, _)| k == &键) {
                    v.push(值);
                } else {
                    桶.push((键, vec![值]));
                }
            }

            let mut headers: Vec<Val> = Vec::with_capacity(列序.len() + 1);
            headers.push(Val::Str(as_text(&args[1]).into()));
            for c in &列序 {
                headers.push(Val::Str(display(c).into()));
            }
            let mut rows: Vec<Vec<Val>> = Vec::with_capacity(行序.len());
            for r in &行序 {
                let mut 行出: Vec<Val> = Vec::with_capacity(列序.len() + 1);
                行出.push(r.clone());
                for c in &列序 {
                    let 空: Vec<Val> = Vec::new();
                    let 值们 = 桶
                        .iter()
                        .find(|(k, _)| &k.0 == r && &k.1 == c)
                        .map(|(_, v)| v)
                        .unwrap_or(&空);
                    let v = 聚合(值们, &方式, &值列名, "透视")?;
                    行出.push(v.unwrap_or_else(|| Val::Str("".into())));
                }
                rows.push(行出);
            }
            Ok(table_new(headers, rows))
        },
        "按列分组" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("按列分组", args, 3, 4)?;
            let t = as_table(&args[0])?;
            let 名们 = 列们(&t, &args[1], "按列分组")?;
            let i值 = column_index(&t, &args[2])?;
            let 值列名 = as_text(&args[2]);
            let 方式 = if args.len() > 3 {
                要聚合(&args[3], "按列分组")?
            } else {
                "求和".to_string()
            };
            let mut 索引: Vec<usize> = Vec::with_capacity(名们.len());
            for 名 in &名们 {
                索引.push(列号(&t, 名)?);
            }

            let mut 键序: Vec<Vec<Val>> = Vec::new();
            let mut 桶: Vec<(Vec<Val>, Vec<Val>)> = Vec::new();
            for row in t.rows.iter() {
                let 键: Vec<Val> = 索引
                    .iter()
                    .map(|i| row.get(*i).cloned().unwrap_or(Val::None_))
                    .collect();
                let 值 = row.get(i值).cloned().unwrap_or(Val::None_);
                if let Some((_, v)) = 桶.iter_mut().find(|(k, _)| k == &键) {
                    v.push(值);
                } else {
                    键序.push(键.clone());
                    桶.push((键, vec![值]));
                }
            }

            let mut headers: Vec<Val> = 名们.iter().map(|n| Val::Str(n.as_str().into())).collect();
            headers.push(Val::Str(值列名.as_str().into()));
            let mut rows: Vec<Vec<Val>> = Vec::with_capacity(键序.len());
            for 键 in &键序 {
                let 空: Vec<Val> = Vec::new();
                let 值们 = 桶
                    .iter()
                    .find(|(k, _)| k == 键)
                    .map(|(_, v)| v)
                    .unwrap_or(&空);
                let v = 聚合(值们, &方式, &值列名, "按列分组")?;
                let mut 行出 = 键.clone();
                行出.push(v.unwrap_or_else(|| Val::Str("".into())));
                rows.push(行出);
            }
            Ok(table_new(headers, rows))
        },
        "融化" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("融化", args, 2)?;
            let t = as_table(&args[0])?;
            let 保留 = 列们(&t, &args[1], "融化")?;
            let mut 保留索引: Vec<usize> = Vec::with_capacity(保留.len());
            for 名 in &保留 {
                保留索引.push(列号(&t, 名)?);
            }
            let 其余: Vec<(usize, Val)> = t
                .headers
                .iter()
                .enumerate()
                .filter(|(_, h)| !保留.iter().any(|n| n == &as_text(h)))
                .map(|(i, h)| (i, h.clone()))
                .collect();

            let mut rows: Vec<Vec<Val>> = Vec::new();
            for row in t.rows.iter() {
                let 头: Vec<Val> = 保留索引
                    .iter()
                    .map(|i| row.get(*i).cloned().unwrap_or(Val::None_))
                    .collect();
                for (i, 名) in &其余 {
                    let mut 行 = 头.clone();
                    行.push(名.clone());
                    行.push(row.get(*i).cloned().unwrap_or(Val::None_));
                    rows.push(行);
                }
            }
            let mut headers = 保留.iter().map(|n| Val::Str(n.as_str().into())).collect::<Vec<_>>();
            headers.push(Val::Str("变量".into()));
            headers.push(Val::Str("值".into()));
            Ok(table_new(headers, rows))
        },
        "展开列" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("展开列", args, 3)?;
            let t = as_table(&args[0])?;
            let 键名 = as_text(&args[1]);
            let 值名 = as_text(&args[2]);
            let i键 = 列号(&t, &键名)?;
            let i值 = 列号(&t, &值名)?;
            let 保留: Vec<(usize, Val)> = t
                .headers
                .iter()
                .enumerate()
                .filter(|(i, _)| *i != i键 && *i != i值)
                .map(|(i, h)| (i, h.clone()))
                .collect();

            let mut 键序: Vec<Val> = Vec::new();
            let mut 行序: Vec<Vec<Val>> = Vec::new();
            // 每行：头（保留列）+ 键 → 值。同一个 (头, 键) 出现多次：后一个赢。
            let mut 格: Vec<(Vec<Val>, Vec<(Val, Val)>)> = Vec::new();
            for row in t.rows.iter() {
                let 键 = row.get(i键).cloned().unwrap_or(Val::None_);
                let 头: Vec<Val> = 保留
                    .iter()
                    .map(|(i, _)| row.get(*i).cloned().unwrap_or(Val::None_))
                    .collect();
                let 值 = row.get(i值).cloned().unwrap_or(Val::None_);
                if !键序.iter().any(|x| x == &键) {
                    键序.push(键.clone());
                }
                if !行序.iter().any(|x| x == &头) {
                    行序.push(头.clone());
                }
                if let Some((_, m)) = 格.iter_mut().find(|(h, _)| h == &头) {
                    if let Some(slot) = m.iter_mut().find(|(k, _)| k == &键) {
                        slot.1 = 值;
                    } else {
                        m.push((键, 值));
                    }
                } else {
                    格.push((头, vec![(键, 值)]));
                }
            }

            let mut headers: Vec<Val> = 保留.iter().map(|(_, h)| h.clone()).collect();
            for k in &键序 {
                headers.push(Val::Str(display(k).into()));
            }
            let mut rows: Vec<Vec<Val>> = Vec::with_capacity(行序.len());
            for 头 in &行序 {
                let mut 行 = 头.clone();
                for k in &键序 {
                    let v = 格
                        .iter()
                        .find(|(h, _)| h == 头)
                        .and_then(|(_, m)| m.iter().find(|(kk, _)| kk == k))
                        .map(|(_, v)| v.clone())
                        .unwrap_or_else(|| Val::Str("".into()));
                    行.push(v);
                }
                rows.push(行);
            }
            Ok(table_new(headers, rows))
        },
        "填充缺失" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("填充缺失", args, 2, 3)?;
            let t = as_table(&args[0])?;
            let 列名 = as_text(&args[1]);
            let i = 列号(&t, &列名)?;
            let 方法 = if args.len() > 2 {
                args[2].clone()
            } else {
                Val::Str("前向".into())
            };
            let 法名 = as_text(&方法);

            let mut 平均 = None;
            if 法名 == "平均" {
                let 列值: Vec<Val> = t
                    .rows
                    .iter()
                    .map(|r| r.get(i).cloned().unwrap_or(Val::None_))
                    .collect();
                let 数 = 数值们(&列值, &列名, "填充缺失")?;
                if !数.is_empty() {
                    平均 = Some(数.iter().sum::<f64>() / 数.len() as f64);
                }
            }

            let 前向 = 法名 == "前向";
            let 用平均 = 法名 == "平均";
            let mut 上一个: Option<Val> = None;
            let mut rows: Vec<Vec<Val>> = Vec::with_capacity(t.rows.len());
            for row in t.rows.iter() {
                let mut 新 = row.clone();
                while 新.len() <= i {
                    新.push(Val::None_);
                }
                if 缺失(&新[i]) {
                    if 前向 {
                        if let Some(v) = &上一个 {
                            新[i] = v.clone();
                        }
                    } else if 用平均 {
                        if let Some(f) = 平均 {
                            新[i] = Val::Float(f);
                        }
                    } else {
                        新[i] = 方法.clone();
                    }
                } else {
                    上一个 = Some(新[i].clone());
                }
                rows.push(新);
            }
            Ok(table_new(t.headers.clone(), rows))
        },
        "丢弃缺失" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("丢弃缺失", args, 1, 2)?;
            let t = as_table(&args[0])?;
            let 索引: Vec<usize> = if args.len() > 1 && !matches!(args[1], Val::None_) {
                let 名们 = 列们(&t, &args[1], "丢弃缺失")?;
                let mut v = Vec::with_capacity(名们.len());
                for 名 in &名们 {
                    v.push(列号(&t, 名)?);
                }
                v
            } else {
                (0..t.headers.len()).collect()
            };
            let rows: Vec<Vec<Val>> = t
                .rows
                .iter()
                .filter(|r| {
                    !索引
                        .iter()
                        .any(|i| r.get(*i).map(缺失).unwrap_or(true))
                })
                .cloned()
                .collect();
            Ok(table_new(t.headers.clone(), rows))
        },
        "取列" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("取列", args, 2)?;
            let t = as_table(&args[0])?;
            let 名们 = 列们(&t, &args[1], "取列")?;
            let mut 索引: Vec<usize> = Vec::with_capacity(名们.len());
            for 名 in &名们 {
                索引.push(列号(&t, 名)?);
            }
            let rows: Vec<Vec<Val>> = t
                .rows
                .iter()
                .map(|row| {
                    索引
                        .iter()
                        .map(|i| row.get(*i).cloned().unwrap_or_else(|| Val::Str("".into())))
                        .collect()
                })
                .collect();
            let headers: Vec<Val> = 名们.iter().map(|n| Val::Str(n.as_str().into())).collect();
            Ok(table_new(headers, rows))
        },
        "切片行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("切片行", args, 1, 3)?;
            let t = as_table(&args[0])?;
            let 总 = t.rows.len() as i128;
            let 归一 = |v: &Val, 默认: i128| -> Result<usize, JishiError> {
                let mut i = match v {
                    Val::None_ => 默认,
                    Val::Int(x) => *x,
                    other => as_text(other).parse::<i128>().map_err(|_| {
                        err("类型错误", format!("「切片行」的行号要是整数，给的是「{}」", display(other)))
                    })?,
                };
                if i < 0 {
                    i += 总;
                }
                Ok(i.max(0).min(总) as usize)
            };
            let 起i = 归一(&args[1], 0)?;
            let 止i = if args.len() > 2 {
                归一(&args[2], 总)?
            } else {
                总 as usize
            };
            let 止i = 止i.max(起i);
            let rows: Vec<Vec<Val>> = t.rows.get(起i..止i).unwrap_or(&[]).to_vec();
            Ok(table_new(t.headers.clone(), rows))
        },
        "去重" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("去重", args, 1, 2)?;
            let t = as_table(&args[0])?;
            let 索引: Vec<usize> = if args.len() > 1 && !matches!(args[1], Val::None_) {
                let 名们 = 列们(&t, &args[1], "去重")?;
                let mut v = Vec::with_capacity(名们.len());
                for 名 in &名们 {
                    v.push(列号(&t, 名)?);
                }
                v
            } else {
                (0..t.headers.len()).collect()
            };
            let mut 见过: Vec<Vec<String>> = Vec::new();
            let mut rows: Vec<Vec<Val>> = Vec::new();
            for row in t.rows.iter() {
                // 用**显示的文本**做键：整数 1 与文本 "1" 不能算同一行
                // （与 `筛选` 的口径一致 —— 它也是按文本比的）。
                let 键: Vec<String> = 索引
                    .iter()
                    .map(|i| display(row.get(*i).unwrap_or(&Val::Str("".into()))))
                    .collect();
                if 见过.contains(&键) {
                    continue;
                }
                见过.push(键);
                rows.push(row.clone());
            }
            Ok(table_new(t.headers.clone(), rows))
        },
        "转换列" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("转换列", args, 2, 3)?;
            let t = as_table(&args[0])?;
            let 列名 = as_text(&args[1]);
            let i = 列号(&t, &列名)?;
            let 名 = if args.len() > 2 {
                as_text(&args[2])
            } else {
                "整数".to_string()
            };
            if 名 != "整数" && 名 != "小数" && 名 != "文本" {
                return Err(err(
                    "类型错误",
                    format!("「转换列」不认识的类型「{名}」—— 只能是：整数 / 小数 / 文本"),
                ));
            }
            let mut rows: Vec<Vec<Val>> = Vec::with_capacity(t.rows.len());
            for (行号, row) in t.rows.iter().enumerate() {
                let mut 新 = row.clone();
                while 新.len() <= i {
                    新.push(Val::Str("".into()));
                }
                let v = 新[i].clone();
                if !缺失(&v) {
                    let 转不了 = |v: &Val| -> JishiError {
                        err(
                            "类型错误",
                            format!(
                                "「转换列」：第 {} 行的「{列名}」是「{}」，转不成{名}",
                                行号 + 1,
                                display(v)
                            ),
                        )
                    };
                    let 数值 = |v: &Val| -> Option<f64> {
                        match v {
                            Val::Int(x) => Some(*x as f64),
                            Val::Float(f) => Some(*f),
                            Val::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
                            Val::Str(s) => s.trim().parse::<f64>().ok(),
                            _ => None,
                        }
                    };
                    新[i] = match 名.as_str() {
                        "文本" => Val::Str(display(&v).into()),
                        "整数" => {
                            let f = 数值(&v).ok_or_else(|| 转不了(&v))?;
                            Val::Int(f as i128)
                        }
                        _ => {
                            let f = 数值(&v).ok_or_else(|| 转不了(&v))?;
                            Val::Float(f)
                        }
                    };
                }
                rows.push(新);
            }
            Ok(table_new(t.headers.clone(), rows))
        },
        // ── M192 ──
        "合并" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("合并", args, 3, 5)?;
            let a = as_table(&args[0])?;
            let b = as_table(&args[1])?;
            let 键甲 = as_text(&args[2]);
            let i甲 = 列号(&a, &键甲)?;
            let 键乙 = match args.get(3) {
                Some(Val::Str(s)) if !s.is_empty() => s.to_string(),
                _ => 键甲.clone(),
            };
            let i乙 = 列号(&b, &键乙)?;
            let 名 = match args.get(4) {
                Some(v) if !matches!(v, Val::None_) => as_text(v),
                _ => "左".to_string(),
            };
            if 名 != "左" && 名 != "内" && 名 != "外" {
                return Err(err(
                    "类型错误",
                    format!("「合并」不认识的连接方式「{名}」—— 只能是：左 / 内 / 外"),
                ));
            }
            // 乙表按键索引（同一个键只留第一条，与 `连接` 一致）
            let mut 索引: Vec<(String, usize)> = Vec::new();
            for (j, row) in b.rows.iter().enumerate() {
                let k = display(row.get(i乙).unwrap_or(&Val::None_));
                if !索引.iter().any(|(kk, _)| kk == &k) {
                    索引.push((k, j));
                }
            }
            let 乙列: Vec<usize> = (0..b.headers.len()).filter(|j| *j != i乙).collect();
            let mut headers = a.headers.clone();
            for j in &乙列 {
                headers.push(b.headers[*j].clone());
            }
            let mut rows: Vec<Vec<Val>> = Vec::new();
            let mut 用过: Vec<String> = Vec::new();
            for row in a.rows.iter() {
                let k = display(row.get(i甲).unwrap_or(&Val::None_));
                let 命中 = 索引.iter().find(|(kk, _)| kk == &k).map(|(_, j)| *j);
                match 命中 {
                    None => {
                        if 名 == "左" || 名 == "外" {
                            let mut 行 = row.clone();
                            for _ in &乙列 {
                                行.push(Val::Str("".into()));
                            }
                            rows.push(行);
                        }
                    }
                    Some(j) => {
                        用过.push(k);
                        let mut 行 = row.clone();
                        for c in &乙列 {
                            行.push(b.rows[j].get(*c).cloned().unwrap_or(Val::None_));
                        }
                        rows.push(行);
                    }
                }
            }
            if 名 == "外" {
                for (k, j) in &索引 {
                    if 用过.iter().any(|x| x == k) {
                        continue;
                    }
                    let mut 行: Vec<Val> = a
                        .headers
                        .iter()
                        .map(|_| Val::Str("".into()))
                        .collect();
                    // 只在乙出现的键要**填回甲的键列**（否则那行看着像没键）
                    if i甲 < 行.len() {
                        行[i甲] = b.rows[*j].get(i乙).cloned().unwrap_or(Val::None_);
                    }
                    for c in &乙列 {
                        行.push(b.rows[*j].get(*c).cloned().unwrap_or(Val::None_));
                    }
                    rows.push(行);
                }
            }
            Ok(table_new(headers, rows))
        },
        "按时间重采样" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("按时间重采样", args, 2, 5)?;
            let t = as_table(&args[0])?;
            let 时名 = as_text(&args[1]);
            let i时 = 列号(&t, &时名)?;
            let 名 = match args.get(2) {
                Some(v) if !matches!(v, Val::None_) => as_text(v),
                _ => "月".to_string(),
            };
            if 名 == "周" {
                return Err(err(
                    "类型错误",
                    "「按时间重采样」这一版还不支持「周」（要 ISO 周算法，不是字符串前缀）—— 先用 月 / 年，或者从 日期 模块自己算",
                ));
            }
            if 名 != "月" && 名 != "年" {
                return Err(err(
                    "类型错误",
                    format!("「按时间重采样」不认识的周期「{名}」—— 只能是：月 / 年"),
                ));
            }
            let 位数 = if 名 == "月" { 7 } else { 4 };

            let mut 键序: Vec<String> = Vec::new();
            let mut 桶: Vec<(String, Vec<usize>)> = Vec::new();
            for (j, row) in t.rows.iter().enumerate() {
                let txt = display(row.get(i时).unwrap_or(&Val::None_));
                let k: String = txt.chars().take(位数).collect();
                if let Some((_, v)) = 桶.iter_mut().find(|(kk, _)| kk == &k) {
                    v.push(j);
                } else {
                    键序.push(k.clone());
                    桶.push((k, vec![j]));
                }
            }

            // `值列` 没给 ⇒ 只数行数
            let 值列 = match args.get(3) {
                Some(v) if !matches!(v, Val::None_) => Some(as_text(v)),
                _ => None,
            };
            let Some(值名) = 值列 else {
                let headers = vec![
                    Val::Str(format!("{时名}（{名}）").into()),
                    Val::Str("行数".into()),
                ];
                let mut rows: Vec<Vec<Val>> = Vec::with_capacity(键序.len());
                for (k, js) in &桶 {
                    rows.push(vec![
                        Val::Str(k.as_str().into()),
                        Val::Int(js.len() as i128),
                    ]);
                }
                return Ok(table_new(headers, rows));
            };
            let i值 = 列号(&t, &值名)?;
            let 方式 = match args.get(4) {
                Some(v) if !matches!(v, Val::None_) => 要聚合(v, "按时间重采样")?,
                _ => "求和".to_string(),
            };
            let mut rows: Vec<Vec<Val>> = Vec::with_capacity(键序.len());
            for (k, js) in &桶 {
                let 值们: Vec<Val> = js
                    .iter()
                    .map(|j| t.rows[*j].get(i值).cloned().unwrap_or(Val::None_))
                    .collect();
                let v = 聚合(&值们, &方式, &值名, "按时间重采样")?;
                rows.push(vec![
                    Val::Str(k.as_str().into()),
                    v.unwrap_or_else(|| Val::Str("".into())),
                ]);
            }
            let headers = vec![
                Val::Str(format!("{时名}（{名}）").into()),
                Val::Str(值名.into()),
            ];
            Ok(table_new(headers, rows))
        },
    }
}
