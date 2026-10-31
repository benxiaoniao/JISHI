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
            let headers: Vec<Val> = rows[0].iter().map(|s| Val::Str(s.clone())).collect();
            let data: Vec<Vec<Val>> = rows[1..]
                .iter()
                .map(|r| r.iter().map(|s| Val::Str(s.clone())).collect())
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
                        Val::Str(h.clone()),
                        Val::Str(r.get(i).cloned().unwrap_or_default()),
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
            Ok(Val::Str(转对齐文本(&t)))
        },
        "转TSV" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("转TSV", args, 1, 1)?;
            let t = 取表格(&args[0], "转TSV")?;
            Ok(Val::Str(转TSV文本(&t)?))
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
            let headers: Vec<Val> = cols.iter().map(|k| Val::Str(as_text(k))).collect();
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
    }
}
