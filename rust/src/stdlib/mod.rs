//! 基石标准库（Rust 宿主，M33）。
//!
//! Python 侧共 14 个模块、约 130 个函数，本宿主逐个对齐语义。
//!
//! 零第三方依赖（`rust/Cargo.toml` 的 `[dependencies]` 是空的）意味着
//! 有些东西得自己写：
//!
//! | 模块 | 别人的实现 | 这里怎么做 |
//! |---|---|---|
//! | `正则` | Python `re` / JS `RegExp` | **自写回溯引擎**（`regex.rs`） |
//! | `加密` | `hashlib` | **自写 md5/sha1/sha256/sha512**（`crypto.rs`） |
//! | `压缩` | `zipfile`（zlib） | **手写 zip 容器 + 自写 inflate**（`zip.rs`） |
//! | `网络` | `urllib` / `fetch` | Windows 走 WinHTTP，其余平台走裸 TCP（`http.rs`） |
//! | `表格` | `csv` 模块 | 自写 CSV 引号转义（`tables.rs`） |
//! | 其余 | — | 薄封装 `std`（fs / path / env / time / process） |
//!
//! ## 为什么每个模块一个文件
//!
//! lib.rs 本身已经 2700 多行。14 个模块全塞进去会撑到四千行以上，
//! 而且 `正则` / `加密` / `压缩` / `网络` 各自还有几百行实现。
//! 按模块分文件之后，改哪个模块看哪个文件。
//!
//! ## 与 Python 侧对齐的两条约定
//!
//! 1. **值 → 文本**统一走 `display()`（等价于 Python 侧
//!    `jishi_repr(x, top=True)`：字符串原样、其余走中文显示），
//!    不要用宿主自己的格式化——那会把 `真` 显示成 `true`。
//! 2. **错误消息中文**，风格与既有代码一致。

use std::collections::HashMap;

use crate::{display, err, JishiError, Val, VM};

pub(crate) mod datetime;
mod files;
pub(crate) mod jsonmod;
mod math;
mod path;
mod rand;
mod system;
pub(crate) mod tables;
mod text;

/// 标准库函数的统一返回类型（省得每处都写一长串）。
pub(crate) type R = Result<Val, JishiError>;

/// 标准库模块的分发表。
///
/// `Val::Builtin` 只能存**无捕获的函数指针**，所以这里只能是「名字 → 表」的
/// 形式，模块表在每次 `导入` 时现场构造（与 Python 侧 `stdlib_module` 同构）。
pub(crate) fn module(name: &str) -> Result<Val, JishiError> {
    let table = match name {
        "数学" => math::module(),
        "文本" => text::module(),
        "随机" => rand::module(),
        "路径" => path::module(),
        "文件" => files::module(),
        "日期" => datetime::date_module(),
        "时间" => datetime::time_module(),
        "系统" => system::module(),
        "加密" => crate::crypto::module(),
        "压缩" => crate::zip::module(),
        "表格" => tables::module(),
        "json" => jsonmod::module(),
        "正则" => crate::regex::module(),
        "网络" => crate::http::module(),
        _ => {
            return Err(err("异常", format!("没有找到标准库模块「{name}」")));
        }
    };
    Ok(Val::Module(name.to_string(), table))
}

// ---------------------------------------------------------------------------
// 构造模块表的语法糖
// ---------------------------------------------------------------------------

/// 从 `"名字" => 函数` 列表造一个模块表。
///
/// 直接用 `HashMap::insert` 写会变成一百多行样板，宏能省掉。
macro_rules! table {
    ($( $name:literal => $f:expr ),* $(,)?) => {{
        let mut m: HashMap<String, Val> = HashMap::new();
        $( m.insert($name.to_string(), Val::Builtin($name, $f)); )*
        m
    }};
}

/// 往表里塞**非函数**的常量（`数学.圆周率` 这种）。
macro_rules! with_values {
    ($m:expr, $( $name:literal => $v:expr ),* $(,)?) => {{
        let mut m = $m;
        $( m.insert($name.to_string(), $v); )*
        m
    }};
}

pub(crate) use table;
pub(crate) use with_values;

// ---------------------------------------------------------------------------
// 参数检查
// ---------------------------------------------------------------------------

/// 恰好 N 个参数。
pub(crate) fn need(name: &str, args: &[Val], n: usize) -> Result<(), JishiError> {
    if args.len() != n {
        return Err(err(
            "类型错误",
            format!("函数「{name}」需要 {n} 个参数，但传了 {} 个", args.len()),
        ));
    }
    Ok(())
}

/// 参数个数落在 `[lo, hi]` 区间。
pub(crate) fn need_range(name: &str, args: &[Val], lo: usize, hi: usize) -> Result<(), JishiError> {
    if args.len() < lo || args.len() > hi {
        return Err(err(
            "类型错误",
            format!(
                "函数「{name}」需要 {lo} 到 {hi} 个参数，但传了 {} 个",
                args.len()
            ),
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// 取值的辅助函数
// ---------------------------------------------------------------------------

/// 值 → 文本。等价于 Python 侧 `jishi_repr(x, top=True)`：
/// 字符串原样，其余走中文显示（`真` / `空` / `[1, '甲']`）。
pub(crate) fn as_text(v: &Val) -> String {
    display(v)
}

/// 值 → f64。布尔按 0/1（Python 里 `真` 就是整数 1）。
pub(crate) fn as_num(v: &Val) -> Result<f64, JishiError> {
    match v {
        Val::Int(i) => Ok(*i as f64),
        Val::Float(f) => Ok(*f),
        Val::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        _ => Err(err(
            "类型错误",
            format!("需要一个数字，但得到了「{}」", display(v)),
        )),
    }
}

/// 值 → i128。对齐 Python 的 `int(x)`：小数向零截断、文本按十进制解析。
pub(crate) fn as_intish(v: &Val) -> Result<i128, JishiError> {
    match v {
        Val::Int(i) => Ok(*i),
        Val::Float(f) => Ok(*f as i128),
        Val::Bool(b) => Ok(if *b { 1 } else { 0 }),
        Val::Str(s) => s
            .trim()
            .parse::<i128>()
            .map_err(|_| err("值错误", format!("「{s}」不能转成整数"))),
        _ => Err(err(
            "类型错误",
            format!("需要一个整数，但得到了「{}」", display(v)),
        )),
    }
}

/// 取列表（共享容器的克隆——调用方拿到的是同一份数据的句柄）。
pub(crate) fn as_list(v: &Val) -> Result<crate::JList, JishiError> {
    match v {
        Val::List(l) => Ok(l.clone()),
        _ => Err(err(
            "类型错误",
            format!("需要一个列表，但得到了「{}」", display(v)),
        )),
    }
}

/// 把「可迭代的基石值」摊平成 `Vec<Val>`。
///
/// 与 Python 侧 `list(序列)` 对齐：列表原样、文本按字符、字典按键、集合按元素。
/// 顺序都按**插入顺序**（本宿主的字典与集合都是有序 `Vec`）。
pub(crate) fn to_vec(v: &Val) -> Result<Vec<Val>, JishiError> {
    match v {
        Val::List(l) => Ok(l.borrow().clone()),
        Val::Str(s) => Ok(s.chars().map(|c| Val::Str(c.to_string())).collect()),
        Val::Dict(d) => Ok(d.borrow().iter().map(|(k, _)| k.clone()).collect()),
        Val::Set(s) => Ok(s.borrow().clone()),
        _ => Err(err(
            "类型错误",
            format!("「{}」不能当序列用，需要列表、文本、集合或字典", display(v)),
        )),
    }
}

// ---------------------------------------------------------------------------
// 脚本参数（系统.参数）
// ---------------------------------------------------------------------------

use std::sync::{Mutex, OnceLock};

/// 脚本收到的命令行参数。用全局是因为 `Val::Builtin` 放不了闭包。
static ARGS: OnceLock<Mutex<Vec<String>>> = OnceLock::new();

fn args_slot() -> &'static Mutex<Vec<String>> {
    ARGS.get_or_init(|| Mutex::new(Vec::new()))
}

/// 由 `main.rs` 在加载字节码前调用。
pub(crate) fn set_args(args: Vec<String>) {
    if let Ok(mut slot) = args_slot().lock() {
        *slot = args;
    }
}

pub(crate) fn script_args() -> Vec<String> {
    args_slot().lock().map(|s| s.clone()).unwrap_or_default()
}
