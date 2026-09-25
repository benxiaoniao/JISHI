//! 标准库 `表格`（Python 侧 `jishi/stdlib/表格.py`，12 个函数）。
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

use std::collections::HashMap;
use std::rc::Rc;

use super::{as_text, need, need_range, table, to_vec, R};
use crate::{err, JishiError, Val, VM};

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

pub(crate) fn module() -> HashMap<String, Val> {
    table! {
        "读表格" => |args: &[Val], _vm: &mut VM| -> R {
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
        "写表格" => |args: &[Val], _vm: &mut VM| -> R {
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
        "写字典表格" => |args: &[Val], _vm: &mut VM| -> R {
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
        "读字典表格" => |args: &[Val], _vm: &mut VM| -> R {
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
        "追加" => |args: &[Val], _vm: &mut VM| -> R {
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
        "表头" => |args: &[Val], _vm: &mut VM| -> R {
            need("表头", args, 1)?;
            let t = as_table(&args[0])?;
            Ok(crate::list_new(t.headers.clone()))
        },
        "数据行" => |args: &[Val], _vm: &mut VM| -> R {
            need("数据行", args, 1)?;
            let t = as_table(&args[0])?;
            let out: Vec<Val> = t
                .rows
                .iter()
                .map(|r| crate::list_new(r.clone()))
                .collect();
            Ok(crate::list_new(out))
        },
        "挑选" => |args: &[Val], _vm: &mut VM| -> R {
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
        "筛选" => |args: &[Val], _vm: &mut VM| -> R {
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
        "排序按" => |args: &[Val], _vm: &mut VM| -> R {
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
        "汇总" => |args: &[Val], _vm: &mut VM| -> R {
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
        "转置" => |args: &[Val], _vm: &mut VM| -> R {
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
    }
}
