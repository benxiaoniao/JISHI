//! 标准库 `数据库`（Python 侧 `jishi/stdlib/数据库.py`，15 个函数）。
//!
//! SQLite 的薄封装：建表 / 插入 / 查询 / 汇总 / 修改 / 删除都有中文函数，
//! **不用写 SQL**；`执行` / `执行查询` 是逃生口。
//!
//! ## 为什么这里破例引第三方依赖（`rusqlite`）
//!
//! 本宿主原来「零第三方依赖」（`Cargo.toml` 的 `[dependencies]` 只有同仓库的
//! `jishi-frontend` 这条路径依赖），`正则` / `加密` / `压缩` / `网络` 都是自己写的。
//! **`数据库` 不自己写**：SQLite 是几万行 C 实现，重写既不现实、也拿不到生态
//! （文件格式、外部工具链）。用户 2026-10-15 拍板：**捆 `rusqlite` + bundled**。
//! ⇒ 代价如实记：发行包大一点；**构建需要 C 编译器**（sqlite3.c 要编）。
//! 详见 `docs/设计决策.md` D78。
//!
//! ## 句柄 = 路径文本
//!
//! `打开("账目.db")` 返回的就是那串路径（`Val::Str`），后面每个函数第一个参数
//! 收它。为什么不用下标 / 不透明对象：**五个执行器都要支持同一个句柄**，
//! 而 `Val` 里加一个新变体要动四处（含 C VM 的 ABI）；路径文本两边都天然有。
//!
//! ## 结果顺序必须确定
//!
//! 五引擎逐字节一致是硬要求：`查询` 默认 `ORDER BY rowid`、给了排序就
//! `<字段>, rowid`；`汇总` 按 `rowid` 取回来、在**本地**分组累加
//! （不用 SQL 的 `SUM` —— 浮点加法不满足结合律，累加顺序由查询计划定）。
//! ⚠️ 三个引擎的 SQLite 版本本来就不同（实测 3.53.1 / 3.51.2 / 3.53.2）。

use crate::BuiltinHost;
use std::cell::RefCell;
use std::collections::HashMap;

use super::{as_list, need_range, table, R};
use crate::{dict_new, display, err, err_with_hint, list_new, Val};

// ---------------------------------------------------------------------------
// 连接表（每个导入一份？—— Rust 侧是 thread_local，与 `参数` / `日志` 同一理由：
// `Val` 含 `Rc`，放不进 `Mutex`；宿主是单线程的）
// ---------------------------------------------------------------------------

thread_local! {
    static 库们: RefCell<HashMap<String, rusqlite::Connection>> =
        RefCell::new(HashMap::new());
}

/// 真值判断（`truthy` 收的是值，这里统一按引用调）。
fn 真(x: &Val) -> bool {
    crate::truthy(x.clone())
}

/// 值相等（走 `cmp_vals`，与 VM 的 `==` 同一条规矩）。
fn 相等(a: &Val, b: &Val) -> bool {
    crate::cmp_vals(a, b) == std::cmp::Ordering::Equal
}

fn 取连接(库: &Val) -> Result<String, crate::JishiError> {
    let 键 = match 库 {
        Val::Str(s) => s.clone(),
        o => {
            return Err(err(
                "类型错误",
                format!("库句柄应该是 数据库.打开(...) 返回的那个文本，给过来的却是「{}」", display(o)),
            ))
        }
    };
    库们.with(|m| {
        if m.borrow().contains_key(&键) {
            Ok(键.clone())
        } else {
            Err(err_with_hint(
                "值错误",
                format!("这个库还没打开：「{键}」"),
                "先用 令 库 = 数据库.打开(\"路径\") 打开，再把「库」传给后面的函数".into(),
            ))
        }
    })
}

/// 借出连接跑一段逻辑（借用期只在闭包内，不跨语句）。
fn 用连接<T>(
    conn: &str,
    f: impl FnOnce(&rusqlite::Connection) -> Result<T, crate::JishiError>,
) -> Result<T, crate::JishiError> {
    库们.with(|m| {
        let map = m.borrow();
        let c = map.get(conn).expect("取连接已确认存在");
        f(c)
    })
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

/// 表名 / 字段名 → SQL 标识符（内部双引号翻倍，防注入）。
fn 名(x: &Val) -> Result<String, crate::JishiError> {
    let s = match x {
        Val::Str(s) => s.clone(),
        o => display(o),
    };
    if s.is_empty() {
        return Err(err("值错误", "表名 / 字段名不能是空的"));
    }
    if s.contains('\0') {
        return Err(err(
            "值错误",
            format!("表名 / 字段名里不能有 NUL 字符：「{s}」"),
        ));
    }
    Ok(format!("\"{}\"", s.replace('"', "\"\"")))
}

fn 名们(x: &Val, 谁: &str) -> Result<Vec<String>, crate::JishiError> {
    // 一个字段名（文本）就当一个元素的列表
    let items = match x {
        Val::Str(_) => vec![名(x)?],
        _ => {
            let v = as_list(x).map_err(|_| {
                err(
                    "值错误",
                    format!("{谁}要给一个非空的字段名列表"),
                )
            })?;
            let raw = v.borrow().clone();
            let mut out = Vec::with_capacity(raw.len());
            for it in &raw {
                out.push(名(it)?);
            }
            out
        }
    };
    if items.is_empty() {
        return Err(err(
            "值错误",
            format!("{谁}要给一个非空的字段名列表，例如 [\"日期\", \"金额\"]"),
        ));
    }
    Ok(items)
}

/// 裸字段名（不带引号）—— 给 `PRAGMA table_info` 比对用。
fn 裸名(x: &Val) -> String {
    match x {
        Val::Str(s) => s.clone(),
        o => display(o),
    }
}

/// 「一个字段名」或「一串字段名」→ 裸名字列表（与 Python 侧 `_字段名单` 同口径）。
fn 裸名们(x: &Val, 谁: &str) -> Result<Vec<String>, crate::JishiError> {
    match x {
        Val::Str(_) => Ok(vec![裸名(x)]),
        _ => {
            let v = as_list(x).map_err(|_| {
                err("值错误", format!("{谁}要给一个非空的字段名列表，例如 [\"金额\"]"))
            })?;
            let out: Vec<String> = v.borrow().iter().map(裸名).collect();
            if out.is_empty() {
                return Err(err(
                    "值错误",
                    format!("{谁}要给一个非空的字段名列表"),
                ));
            }
            Ok(out)
        }
    }
}

/// rusqlite 的报错 → 中文基石错误。
///
/// ⚠️ 尽量取出**SQLite 自己那句话**（`SqliteFailure(_, Some(msg))`），不要
/// 把 rusqlite 的 `Debug` 形状漏出去 —— 那样与 Python / Node 的说法差得远。
/// 📌 但三个引擎的 SQLite 版本本就不同（实测 3.53.1 / 3.51.2 / 3.53.2），
/// **SQLite 自己那句话不做跨引擎逐字比对**（我们自己的报错才比）。
fn 库错(e: rusqlite::Error) -> crate::JishiError {
    let 说 = match &e {
        rusqlite::Error::SqliteFailure(_, Some(m)) => m.clone(),
        _ => e.to_string(),
    };
    err("运行期错误", format!("数据库操作失败：{说}"))
}

/// SQLite 的值 → 基石的值。二进制读不出来（基石没有字节类型）——**明确报错**。
fn 读值(v: rusqlite::types::ValueRef<'_>, 列: &str) -> Result<Val, crate::JishiError> {
    use rusqlite::types::ValueRef as VR;
    Ok(match v {
        VR::Null => Val::None_,
        VR::Integer(i) => Val::Int(i as i128),
        VR::Real(f) => Val::Float(f),
        VR::Text(t) => match std::str::from_utf8(t) {
            Ok(s) => Val::Str(s.to_string()),
            Err(_) => {
                return Err(err(
                    "运行期错误",
                    format!("「{列}」这一列不是合法的 UTF-8 文本，读不出来"),
                ))
            }
        },
        VR::Blob(b) => Val::Bytes(std::rc::Rc::new(b.to_vec())), // M113：字节 ↔ BLOB
    })
}

/// 基石的值 → 能进 SQLite 的值。**进不去就明确报错**，不静默。
fn 存值(v: &Val, 字段: &str) -> Result<rusqlite::types::Value, crate::JishiError> {
    use rusqlite::types::Value as RV;
    Ok(match v {
        Val::None_ => RV::Null,
        Val::Bool(b) => RV::Integer(if *b { 1 } else { 0 }),
        Val::Int(i) => {
            if *i < i64::MIN as i128 || *i > i64::MAX as i128 {
                return Err(err_with_hint(
                    "值错误",
                    format!("「{字段}」这个整数超出了 SQLite 的范围（约 ±9.2e18）"),
                    "特别大的数存成文本（文本(值)），或者拆成两部分存".into(),
                ));
            }
            RV::Integer(*i as i64)
        }
        Val::Float(f) => RV::Real(*f),
        Val::Str(s) => RV::Text(s.clone()),
        Val::Date(_) => RV::Text(display(v)), // 与 Python 的 str(datetime) 同口径
        // M113：字节 ↔ BLOB 原样往返
        Val::Bytes(b) => RV::Blob(b.as_ref().clone()),
        Val::Dec(_) => match display(v).parse::<f64>() {
            Ok(f) => RV::Real(f), // SQLite 只有浮点，见模块文档
            Err(_) => {
                return Err(err(
                    "值错误",
                    format!("「{字段}」这个精确小数转不成浮点"),
                ))
            }
        },
        Val::Dict(_) => {
            return Err(err_with_hint(
                "类型错误",
                format!("「{字段}」这个值是「字典」，存不进数据库"),
                "用 json.文本化(值) 先转成文本再存；或者拆成几张表".into(),
            ))
        }
        Val::List(_) | Val::Set(_) => {
            return Err(err_with_hint(
                "类型错误",
                format!("「{字段}」这个值是「{}」，存不进数据库", crate::type_label(v)),
                "用 json.文本化(值) 先转成文本再存；多个值就拆成多条记录".into(),
            ))
        }
        other => {
            return Err(err_with_hint(
                "类型错误",
                format!("「{字段}」这个值是「{}」，存不进数据库", crate::type_label(other)),
                "数据库里只放 整数 / 浮点 / 文本 / 真假 / 空 / 日期".into(),
            ))
        }
    })
}

/// 一次查询的结果：列名 + 行（已转成基石值）。
struct 结果 {
    列: Vec<String>,
    行们: Vec<Vec<Val>>,
}

fn 跑(conn: &rusqlite::Connection, sql: &str,
      参数: &[rusqlite::types::Value]) -> Result<结果, crate::JishiError> {
    let mut stmt = conn.prepare(sql).map_err(库错)?;
    let 列: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
    let mut rows = stmt
        .query(rusqlite::params_from_iter(参数.iter()))
        .map_err(库错)?;
    let mut 行们: Vec<Vec<Val>> = Vec::new();
    while let Some(r) = rows.next().map_err(库错)? {
        let mut 一行 = Vec::with_capacity(列.len());
        for i in 0..列.len() {
            let 原 = r.get_ref(i).map_err(库错)?;
            一行.push(读值(原, &列[i])?);
        }
        行们.push(一行);
    }
    Ok(结果 { 列, 行们 })
}

fn 行们成记录(结果: &结果) -> Result<Val, crate::JishiError> {
    // ⚠️ 列名重复时「记录」只能留一份 —— 不许静默丢列，明确报错并给修法
    let mut 见过: Vec<&String> = Vec::new();
    for c in &结果.列 {
        if 见过.contains(&c) {
            return Err(err_with_hint(
                "类型错误",
                format!("这句 SQL 里有重名的列（{c}），一条记录里放不下"),
                "在 SQL 里给它们起别名，例如 SELECT 金额 AS 分组, 金额 AS 值".into(),
            ));
        }
        见过.push(c);
    }
    let mut out: Vec<Val> = Vec::with_capacity(结果.行们.len());
    for 一行 in &结果.行们 {
        let pairs: Vec<(Val, Val)> = 结果
            .列
            .iter()
            .zip(一行.iter())
            .map(|(c, v)| (Val::Str(c.clone()), v.clone()))
            .collect();
        out.push(dict_new(pairs));
    }
    Ok(list_new(out))
}

/// 表的列名（按建表顺序）。表不在就报错，并把现有的表列出来。
fn 列名们(conn: &rusqlite::Connection, 表: &Val) -> Result<Vec<String>, crate::JishiError> {
    let 表名 = 名(表)?;
    let r = 跑(conn, &format!("PRAGMA table_info({表名})"), &[])?;
    if r.行们.is_empty() {
        let 现有 = 跑(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' \
             AND name NOT LIKE 'sqlite_%' ORDER BY name",
            &[],
        )?;
        let 名字: Vec<String> = 现有
            .行们
            .iter()
            .map(|行| display(&行[0]))
            .collect();
        let 提示 = if 名字.is_empty() {
            "这个库还是空的（先 数据库.建表）".to_string()
        } else {
            format!("现在有这些表：{}", 名字.join("、"))
        };
        return Err(err_with_hint(
            "值错误",
            format!("没有表「{}」", 裸名(表)),
            提示,
        ));
    }
    Ok(r.行们.iter().map(|row| display(&row[1])).collect())
}

/// 「条件」字典 → `(WHERE 片段, 参数)`。
fn 查条件(条件: Option<&Val>) -> Result<(String, Vec<rusqlite::types::Value>), crate::JishiError> {
    use rusqlite::types::Value as RV;
    let d = match 条件 {
        None | Some(Val::None_) => return Ok((String::new(), Vec::new())),
        Some(Val::Dict(d)) => d.clone(),
        Some(o) => {
            return Err(err_with_hint(
                "类型错误",
                format!("「条件」要给一个字典（字段 → 值），给过来的却是「{}」", display(o)),
                "例如 {\"类别\": \"餐饮\"}；比大小写 {\"金额\": {\">\": 100}}".into(),
            ))
        }
    };
    let items = d.borrow().clone();
    if items.is_empty() {
        return Ok((String::new(), Vec::new()));
    }
    // 键排一下，生成的 SQL 才稳定
    let mut 键们: Vec<Val> = items.iter().map(|(k, _)| k.clone()).collect();
    键们.sort_by_key(|k| display(k));
    let mut 片段: Vec<String> = Vec::new();
    let mut 参数: Vec<RV> = Vec::new();
    for k in &键们 {
        let v = items
            .iter()
            .find(|(kk, _)| 相等(kk, k))
            .map(|(_, v)| v.clone())
            .unwrap_or(Val::None_);
        let 字段 = 名(k)?;
        match &v {
            Val::Dict(ops) => {
                let ops = ops.borrow().clone();
                if ops.is_empty() {
                    return Err(err(
                        "值错误",
                        format!("「{}」的条件是空字典，不知道要比什么", display(k)),
                    ));
                }
                let mut 符号们: Vec<Val> = ops.iter().map(|(s, _)| s.clone()).collect();
                符号们.sort_by_key(|s| display(s));
                for 符 in &符号们 {
                    let x = ops
                        .iter()
                        .find(|(ss, _)| 相等(ss, 符))
                        .map(|(_, x)| x.clone())
                        .unwrap_or(Val::None_);
                    let 符文 = display(符);
                    match 符文.as_str() {
                        ">" | "<" | ">=" | "<=" | "!=" => {
                            片段.push(format!("{字段} {符文} ?"));
                            参数.push(存值(&x, &display(k))?);
                        }
                        "包含" => {
                            片段.push(format!("instr({字段}, ?) > 0"));
                            参数.push(RV::Text(display(&x)));
                        }
                        "非空" => {
                            let 要非空 = 真(&x);
                            片段.push(format!(
                                "{字段} IS {}NULL",
                                if 要非空 { "NOT " } else { "" }
                            ));
                        }
                        _ => {
                            return Err(err_with_hint(
                                "值错误",
                                format!("不认识的比较符号「{符文}」"),
                                "能用：> < >= <= != 包含 非空".into(),
                            ))
                        }
                    }
                }
            }
            _ => {
                片段.push(format!("{字段} = ?"));
                参数.push(存值(&v, &display(k))?);
            }
        }
    }
    Ok((format!(" WHERE {}", 片段.join(" AND ")), 参数))
}

/// 排序片段。**永远拿 rowid 兜底** —— 不然等值的行之间顺序不定。
fn 定序(排序: Option<&Val>, 倒序: bool) -> Result<String, crate::JishiError> {
    let 方向 = if 倒序 { " DESC" } else { "" };
    match 排序 {
        None | Some(Val::None_) => Ok(format!(" ORDER BY rowid{方向}")),
        Some(Val::Str(s)) if s.is_empty() => Ok(format!(" ORDER BY rowid{方向}")),
        Some(v) => {
            let 列 = 名们(v, "排序字段")?;
            Ok(format!(" ORDER BY {}, rowid{方向}", 列.join(", ")))
        }
    }
}

fn 取字典(v: &Val, 谁: &str) -> Result<crate::JDict, crate::JishiError> {
    match v {
        Val::Dict(d) => Ok(d.clone()),
        o => Err(err_with_hint(
            "类型错误",
            format!("「{谁}」要给一个字典，给过来的却是「{}」", display(o)),
            "例如 {\"日期\": \"2026-10-15\", \"金额\": 2550}".into(),
        )),
    }
}

// ---------------------------------------------------------------------------
// 模块表
// ---------------------------------------------------------------------------

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "数据库";
        "打开" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("打开", args, 1, 1)?;
            let 路径 = match &args[0] { Val::Str(s) => s.clone(), o => display(o) };
            let 已有 = 库们.with(|m| m.borrow().contains_key(&路径));
            if 已有 {
                return Ok(Val::Str(路径));
            }
            if 路径 != ":memory:" {
                let p = std::path::Path::new(&路径);
                if let Some(父) = p.parent() {
                    if !父.as_os_str().is_empty() && !父.is_dir() {
                        return Err(err(
                            "文件错误",
                            format!("目录不存在，建不了库：「{}」", 父.display()),
                        ));
                    }
                }
            }
            let c = rusqlite::Connection::open(&路径).map_err(|e| {
                err("文件错误", format!("打不开库「{路径}」：{}", 库错(e).message))
            })?;
            // ⚠️ 三个引擎都显式设一次（node:sqlite 默认是开的，python/rusqlite 默认关）
            c.execute_batch("PRAGMA foreign_keys = ON").map_err(库错)?;
            库们.with(|m| { m.borrow_mut().insert(路径.clone(), c); });
            Ok(Val::Str(路径))
        },
        "关闭" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("关闭", args, 1, 1)?;
            let 键 = match &args[0] { Val::Str(s) => s.clone(), o => display(o) };
            库们.with(|m| { m.borrow_mut().remove(&键); });
            Ok(Val::None_)
        },
        "表们" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("表们", args, 1, 1)?;
            let 键 = 取连接(&args[0])?;
            let r = 用连接(&键, |c| {
                跑(c, "SELECT name FROM sqlite_master WHERE type='table' \
                       AND name NOT LIKE 'sqlite_%' ORDER BY name", &[])
            })?;
            Ok(list_new(r.行们.iter().map(|行| 行[0].clone()).collect()))
        },
        "有表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("有表", args, 2, 2)?;
            let 键 = 取连接(&args[0])?;
            let 名文 = 裸名(&args[1]);
            let r = 用连接(&键, |c| {
                跑(c, "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                   &[rusqlite::types::Value::Text(名文)])
            })?;
            Ok(Val::Bool(!r.行们.is_empty()))
        },
        "建表" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("建表", args, 3, 3)?;
            let 键 = 取连接(&args[0])?;
            let 表 = 名(&args[1])?;
            let 表文 = 裸名(&args[1]);
            // 字段名列表：先按原值拿出来，判掉 id，再各自包成标识符
            let 裸列 = 裸名们(&args[2], &format!("表「{表文}」的字段们"))?;
            if 裸列.iter().any(|x| x == "id") {
                return Err(err_with_hint(
                    "值错误",
                    "字段名里不用写 id",
                    "每张表自动带一个 id 主键，把 id 从字段列表里去掉就行".into(),
                ));
            }
            let 列: Vec<String> = 裸列
                .iter()
                .map(|x| format!("\"{}\"", x.replace('"', "\"\"")))
                .collect();
            用连接(&键, |c| {
                c.execute_batch(&format!(
                    "CREATE TABLE IF NOT EXISTS {表} \
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, {})",
                    列.join(", ")
                )).map_err(库错)
            })?;
            Ok(Val::None_)
        },
        "建索引" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("建索引", args, 3, 4)?;
            let 键 = 取连接(&args[0])?;
            let 表 = 名(&args[1])?;
            let 唯一 = args.len() == 4 && 真(&args[3]);
            用连接(&键, |c| {
                let 现有 = 列名们(c, &args[1])?;
                let 裸 = 裸名们(&args[2], "索引字段")?;
                let 缺: Vec<&String> = 裸.iter().filter(|x| !现有.contains(x)).collect();
                if !缺.is_empty() {
                    return Err(err_with_hint(
                        "值错误",
                        format!("表「{}」里没有这些字段：{}",
                                裸名(&args[1]),
                                缺.iter().map(|s| s.as_str()).collect::<Vec<_>>().join("、")),
                        format!("它有：{}", 现有.join("、")),
                    ));
                }
                let 索引 = 名们(&args[2], "索引字段")?;
                let 表文 = 裸名(&args[1]);
                // 索引名要可复现（重复调用幂等）
                let 索引名 = format!("\"{}__{}\"", 表文, 裸.join("__"));
                c.execute_batch(&format!(
                    "CREATE {}INDEX IF NOT EXISTS {} ON {} ({})",
                    if 唯一 { "UNIQUE " } else { "" },
                    索引名, 表, 索引.join(", ")
                )).map_err(库错)
            })?;
            Ok(Val::None_)
        },
        "插入" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("插入", args, 3, 3)?;
            let 键 = 取连接(&args[0])?;
            let d = 取字典(&args[2], "记录")?;
            let items = d.borrow().clone();
            for (k, _) in &items {
                if 裸名(k) == "id" {
                    return Err(err_with_hint(
                        "值错误", "id 是自动给的，不用写",
                        "把 id 从记录里去掉".into()));
                }
            }
            用连接(&键, |c| {
                let 现有 = 列名们(c, &args[1])?;
                let 多: Vec<String> = items.iter().map(|(k, _)| 裸名(k))
                    .filter(|k| !现有.contains(k)).collect();
                if !多.is_empty() {
                    return Err(err_with_hint(
                        "值错误",
                        format!("表「{}」里没有这些字段：{}",
                                裸名(&args[1]), 多.join("、")),
                        format!("它有：{}", 现有.join("、")),
                    ));
                }
                let mut 名们: Vec<Val> = items.iter().map(|(k, _)| k.clone()).collect();
                名们.sort_by_key(|k| display(k));
                let mut 列SQL: Vec<String> = Vec::new();
                let mut 参数: Vec<rusqlite::types::Value> = Vec::new();
                for k in &名们 {
                    let v = items.iter()
                        .find(|(kk, _)| 相等(kk, k))
                        .map(|(_, v)| v.clone())
                        .unwrap_or(Val::None_);
                    列SQL.push(名(k)?);
                    参数.push(存值(&v, &display(k))?);
                }
                let 占位 = vec!["?"; 列SQL.len()].join(", ");
                let 表 = 名(&args[1])?;
                c.execute(&format!(
                    "INSERT INTO {表} ({}) VALUES ({占位})",
                    列SQL.join(", ")
                ), rusqlite::params_from_iter(参数.iter())).map_err(库错)?;
                Ok(Val::Int(c.last_insert_rowid() as i128))
            })
        },
        "批量插入" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("批量插入", args, 3, 3)?;
            let 键 = 取连接(&args[0])?;
            let 记录们 = as_list(&args[2]).map_err(|_| err(
                "类型错误", "「记录们」要给一个列表"))?;
            let 列表 = 记录们.borrow().clone();
            if 列表.is_empty() { return Ok(Val::Int(0)); }
            用连接(&键, |c| {
                let 现有 = 列名们(c, &args[1])?;
                let mut 名文: Vec<String> = Vec::new();
                for it in &列表 {
                    let d = 取字典(it, "记录们里的每条")?;
                    for (k, _) in d.borrow().iter() {
                        let s = 裸名(k);
                        if s == "id" {
                            return Err(err_with_hint(
                                "值错误", "id 是自动给的，不用写",
                                "把 id 从记录里去掉".into()));
                        }
                        if !名文.contains(&s) { 名文.push(s); }
                    }
                }
                名文.sort();
                let 多: Vec<&String> = 名文.iter().filter(|k| !现有.contains(k)).collect();
                if !多.is_empty() {
                    return Err(err_with_hint(
                        "值错误",
                        format!("表「{}」里没有这些字段：{}",
                                裸名(&args[1]), 多.iter().map(|s| s.as_str()).collect::<Vec<_>>().join("、")),
                        format!("它有：{}", 现有.join("、")),
                    ));
                }
                let 表 = 名(&args[1])?;
                let 列SQL: Vec<String> = 名文.iter().map(|s| format!("\"{}\"", s.replace('"', "\"\""))).collect();
                let 占位 = vec!["?"; 名文.len()].join(", ");
                let sql = format!("INSERT INTO {表} ({}) VALUES ({占位})", 列SQL.join(", "));
                c.execute_batch("BEGIN").map_err(库错)?;
                let r = (|| -> Result<(), crate::JishiError> {
                    for it in &列表 {
                        let d = 取字典(it, "记录们里的每条")?;
                        let items = d.borrow().clone();
                        let mut 参数: Vec<rusqlite::types::Value> = Vec::new();
                        for k in &名文 {
                            let v = items.iter()
                                .find(|(kk, _)| 裸名(kk) == *k)
                                .map(|(_, v)| v.clone())
                                .unwrap_or(Val::None_);
                            参数.push(存值(&v, k)?);
                        }
                        c.execute(&sql, rusqlite::params_from_iter(参数.iter())).map_err(库错)?;
                    }
                    Ok(())
                })();
                match r {
                    Ok(()) => { c.execute_batch("COMMIT").map_err(库错)?; }
                    Err(e) => { let _ = c.execute_batch("ROLLBACK"); return Err(e); }
                }
                Ok(Val::Int(列表.len() as i128))
            })
        },
        "查询" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("查询", args, 2, 6)?;
            let 键 = 取连接(&args[0])?;
            let 倒序 = args.len() >= 5 && 真(&args[4]);
            let 条数 = if args.len() >= 6 { crate::as_int(&args[5])? } else { 0 };
            if 条数 < 0 {
                return Err(err("值错误",
                    format!("条数要给非负整数，给的是「{}」", display(&args[5]))));
            }
            let 排序 = if args.len() >= 4 { Some(args[3].clone()) } else { None };
            let 条件 = if args.len() >= 3 { Some(args[2].clone()) } else { None };
            let 表 = 名(&args[1])?;
            用连接(&键, |c| {
                let 现有 = 列名们(c, &args[1])?;
                if let Some(s) = &排序 {
                    let 要 = 裸名们(s, "排序字段")?;
                    let 缺: Vec<&String> = 要.iter().filter(|x| !现有.contains(x)).collect();
                    if !缺.is_empty() {
                        return Err(err_with_hint(
                            "值错误",
                            format!("表「{}」里没有这些字段：{}",
                                    裸名(&args[1]),
                                    缺.iter().map(|s| s.as_str()).collect::<Vec<_>>().join("、")),
                            format!("它有：{}", 现有.join("、")),
                        ));
                    }
                }
                let (片段, 参数) = 查条件(条件.as_ref())?;
                let 限 = if 条数 > 0 { format!(" LIMIT {条数}") } else { String::new() };
                let sql = format!(
                    "SELECT * FROM {表}{片段}{}{限}",
                    定序(排序.as_ref(), 倒序)?
                );
                let r = 跑(c, &sql, &参数)?;
                行们成记录(&r)
            })
        },
        "条数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("条数", args, 2, 3)?;
            let 键 = 取连接(&args[0])?;
            let 条件 = if args.len() >= 3 { Some(args[2].clone()) } else { None };
            let 表 = 名(&args[1])?;
            用连接(&键, |c| {
                列名们(c, &args[1])?;
                let (片段, 参数) = 查条件(条件.as_ref())?;
                let r = 跑(c, &format!("SELECT COUNT(*) FROM {表}{片段}"), &参数)?;
                Ok(Val::Int(crate::as_int(&r.行们[0][0])? as i128))
            })
        },
        "汇总" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("汇总", args, 4, 5)?;
            let 键 = 取连接(&args[0])?;
            let 条件 = if args.len() >= 5 { Some(args[4].clone()) } else { None };
            let 表 = 名(&args[1])?;
            let 分组裸 = 裸名(&args[2]);
            let 求和裸 = 裸名(&args[3]);
            用连接(&键, |c| {
                let 现有 = 列名们(c, &args[1])?;
                for x in [&分组裸, &求和裸] {
                    if !现有.contains(x) {
                        return Err(err_with_hint(
                            "值错误",
                            format!("表「{}」里没有这些字段：{x}", 裸名(&args[1])),
                            format!("它有：{}", 现有.join("、")),
                        ));
                    }
                }
                let 分组 = 名(&args[2])?;
                let 求和 = 名(&args[3])?;
                let (片段, 参数) = 查条件(条件.as_ref())?;
                // ⚠️ 按 rowid 取回来（确定顺序），分组与累加都在本地做
                let r = 跑(c, &format!(
                    "SELECT {分组}, {求和} FROM {表}{片段} ORDER BY rowid"), &参数)?;
                let mut 组们: Vec<Val> = Vec::new();
                let mut 值们: Vec<Vec<f64>> = Vec::new();
                for 行 in &r.行们 {
                    let 组 = 行[0].clone();
                    let v = &行[1];
                    if matches!(v, Val::None_) {
                        return Err(err_with_hint(
                            "类型错误",
                            format!("「{求和裸}」这一列里有空值，没法汇总"),
                            "先把空值补上，或者用条件把它们排除掉".into(),
                        ));
                    }
                    let f = match v {
                        Val::Int(i) => *i as f64,
                        Val::Float(f) => *f,
                        Val::Bool(b) => if *b { 1.0 } else { 0.0 },
                        Val::Dec(_) => display(v).parse::<f64>().unwrap_or(f64::NAN),
                        _ => {
                            return Err(err(
                                "类型错误",
                                format!("「{求和裸}」这一列里有不是数字的值「{}」，没法汇总",
                                        display(v)),
                            ))
                        }
                    };
                    // 组按**首次出现**的顺序（键可能是文本也可能是数字，混着排序会炸）
                    let mut 找到 = None;
                    for (i, g) in 组们.iter().enumerate() {
                        if 相等(g, &组) { 找到 = Some(i); break; }
                    }
                    match 找到 {
                        Some(i) => 值们[i].push(f),
                        None => { 组们.push(组); 值们.push(vec![f]); }
                    }
                }
                let mut pairs: Vec<(Val, Val)> = Vec::new();
                for (g, 数值) in 组们.iter().zip(值们.iter()) {
                    let n = 数值.len();
                    let 总和: f64 = 数值.iter().sum();
                    let 最大 = 数值.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
                    let 最小 = 数值.iter().cloned().fold(f64::INFINITY, f64::min);
                    pairs.push((g.clone(), dict_new(vec![
                        (Val::Str("个数".into()), Val::Int(n as i128)),
                        (Val::Str("总和".into()), Val::Float(总和)),
                        (Val::Str("平均".into()), Val::Float(总和 / n as f64)),
                        (Val::Str("最大".into()), Val::Float(最大)),
                        (Val::Str("最小".into()), Val::Float(最小)),
                    ])));
                }
                Ok(dict_new(pairs))
            })
        },
        "修改" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("修改", args, 4, 4)?;
            let 键 = 取连接(&args[0])?;
            let 值 = 取字典(&args[3], "新值")?;
            let items = 值.borrow().clone();
            if items.is_empty() {
                return Err(err_with_hint(
                    "类型错误", "「新值」要给一个非空字典",
                    "例如 {\"金额\": 3000}".into()));
            }
            let 条件 = 取字典(&args[2], "条件")?;
            if 条件.borrow().is_empty() {
                return Err(err_with_hint(
                    "值错误", "「修改」必须给条件",
                    "要改整张表就用 数据库.执行(库, \"UPDATE ...\")；\
                     要改一部分就用 {\"类别\": \"餐饮\"} 这种条件".into()));
            }
            用连接(&键, |c| {
                let 现有 = 列名们(c, &args[1])?;
                for (k, _) in &items {
                    if 裸名(k) == "id" {
                        return Err(err("值错误", "id 是主键，不能改"));
                    }
                    if !现有.contains(&裸名(k)) {
                        return Err(err_with_hint(
                            "值错误",
                            format!("表「{}」里没有这些字段：{}", 裸名(&args[1]), 裸名(k)),
                            format!("它有：{}", 现有.join("、")),
                        ));
                    }
                }
                let (片段, 条件参数) = 查条件(Some(&Val::Dict(条件.clone())))?;
                let mut 名们: Vec<Val> = items.iter().map(|(k, _)| k.clone()).collect();
                名们.sort_by_key(|k| display(k));
                let mut 赋值: Vec<String> = Vec::new();
                let mut 参数: Vec<rusqlite::types::Value> = Vec::new();
                for k in &名们 {
                    let v = items.iter()
                        .find(|(kk, _)| 相等(kk, k))
                        .map(|(_, v)| v.clone())
                        .unwrap_or(Val::None_);
                    赋值.push(format!("{} = ?", 名(k)?));
                    参数.push(存值(&v, &display(k))?);
                }
                参数.extend(条件参数);
                let 表 = 名(&args[1])?;
                let n = c.execute(
                    &format!("UPDATE {表} SET {}{片段}", 赋值.join(", ")),
                    rusqlite::params_from_iter(参数.iter())).map_err(库错)?;
                Ok(Val::Int(n as i128))
            })
        },
        "删除" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("删除", args, 3, 3)?;
            let 键 = 取连接(&args[0])?;
            let 条件 = 取字典(&args[2], "条件")?;
            if 条件.borrow().is_empty() {
                return Err(err_with_hint(
                    "值错误", "「删除」必须给条件",
                    "要清空整张表就用 数据库.执行(库, \"DELETE FROM 表名\")".into()));
            }
            let 表 = 名(&args[1])?;
            用连接(&键, |c| {
                列名们(c, &args[1])?;
                let (片段, 参数) = 查条件(Some(&Val::Dict(条件.clone())))?;
                let n = c.execute(
                    &format!("DELETE FROM {表}{片段}"),
                    rusqlite::params_from_iter(参数.iter())).map_err(库错)?;
                Ok(Val::Int(n as i128))
            })
        },
        "执行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("执行", args, 2, 2)?;
            let 键 = 取连接(&args[0])?;
            let sql = match &args[1] { Val::Str(s) => s.clone(), o => display(o) };
            if 像查询(&sql) {
                return Err(err_with_hint(
                    "值错误",
                    "「执行」只跑写操作的 SQL；你给的是 SELECT",
                    "要取回记录用 数据库.执行查询(库, SQL)".into()));
            }
            用连接(&键, |c| {
                let n = c.execute(&sql, []).map_err(库错)?;
                Ok(Val::Int(n as i128))
            })
        },
        "执行查询" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("执行查询", args, 2, 2)?;
            let 键 = 取连接(&args[0])?;
            let sql = match &args[1] { Val::Str(s) => s.clone(), o => display(o) };
            if !像查询(&sql) {
                return Err(err_with_hint(
                    "值错误",
                    "「执行查询」只跑 SELECT；你给的不是 SELECT",
                    "写操作用 数据库.执行(库, SQL)".into()));
            }
            用连接(&键, |c| {
                let r = 跑(c, &sql, &[])?;
                行们成记录(&r)
            })
        },
    }
}

/// 是不是一句 SELECT（跳过开头的行注释）。
fn 像查询(sql: &str) -> bool {
    let mut s = sql.trim_start();
    while s.starts_with("--") {
        s = match s.find('\n') {
            Some(i) => s[i + 1..].trim_start(),
            None => "",
        };
    }
    let 头 = s.chars().take(7).collect::<String>().to_ascii_lowercase();
    头 == "select " || 头 == "select\n" || 头 == "select\r"
}
