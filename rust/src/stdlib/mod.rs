//! 基石标准库（Rust 宿主，M33）。
//!
//! Python 侧共 14 个模块、约 130 个函数，本宿主逐个对齐语义。
//!
//! 大部分模块「没有现成实现可用」的，就自己写（这是本项目的传统）：
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
//! ## 依赖：**只有一个第三方**（M112 起）
//!
//! 以前这里是「零第三方依赖」——`正则` / `加密` / `压缩` / `网络` / `表格` 全自己写。
//! **M112 的 `数据库` 是唯一破例**：SQLite 是几万行 C 实现，重写不现实、也拿不到
//! 生态（标准文件格式、外部工具链）⇒ 捆 `rusqlite`（`bundled`，把 sqlite3.c 一起编）。
//! 用户 2026-10-15 拍板，取舍见 `docs/设计决策.md` D78。代价如实记：
//! **构建需要 C 编译器**；浏览器试玩页（wasm）链不了它 ⇒ 那边关掉 `sqlite` 特性，
//! `导入 数据库` 会明确报「这个平台没有内置 SQLite」。
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

use crate::{display, err, JishiError, Val};

pub(crate) mod datetime;
mod files;
pub(crate) mod jsonmod;
mod math;
mod path;
mod rand;
mod system;
pub(crate) mod tables;
mod text;
// M41 新增（与 Python 侧 jishi/stdlib/ 下同名文件对齐）
mod args;
mod containers;
// M50 新增（与 Python 侧 jishi/stdlib/ 下同名文件对齐）
mod stats;
mod ident;
// M139 新增（与 Python 侧 jishi/stdlib/向量.py 对齐）
mod vector;
// M141 新增（与 Python 侧 jishi/stdlib/矩阵.py 对齐）
mod matrix;
mod encode;
mod htmlmod;
mod diffmod;
mod config;
mod iter;
mod log;
mod testmod;
// M114：`并发` —— 把**一批互相独立的 IO** 同时做（结果顺序与输入一致）。
// ⚠️ 它**不是语言级线程**：线程里只跑纯数据，`Val` 一个都不跨线程（见 D80）。
//    wasm 上自动退回顺序执行（`concurrent.rs` 里 `#[cfg]` 分了两个定义）。
mod concurrent;
// M115：`HTTP服务端` —— 起一个只监听本机的小服务。
// ⚠️ 纯协议核心（`解析请求`/`构造响应`）在 wasm 上照样能用；
//    `启动`/`空闲端口` 那两把要 socket 的在 wasm 上**明确报错**（见 D81）。
mod server;
mod router;
mod excel;
// M112 新增（数据持久化）：唯一一个**引了第三方依赖**的模块，见 `db.rs` 头注与 D78。
// ⚠️ 它挂在 `sqlite` 特性后面 —— wasm（浏览器试玩页）关掉那个特性（链不了 sqlite3.c）。
#[cfg(feature = "sqlite")]
mod db;

/// 标准库函数的统一返回类型（省得每处都写一长串）。
pub(crate) type R = Result<Val, JishiError>;

/// 标准库模块的**唯一清单**（模块名 → 构造函数）。
///
/// 加新模块只改这里（外加一个实现文件）：`module()` 与 `--dump-stdlib`
/// 都从它取，所以**不会出现「写了实现、忘了登记」的静默漂移**——
/// 那种漂移在 M50 第一批上真发生过（宿主缺模块，是手工测出来的）。
pub(crate) fn all_modules() -> &'static [(&'static str, fn() -> HashMap<String, Val>)] {
    &[
        ("数学", math::module),
        ("文本", text::module),
        ("随机", rand::module),
        ("路径", path::module),
        ("文件", files::module),
        ("日期", datetime::date_module),
        ("时间", datetime::time_module),
        ("系统", system::module),
        ("加密", crate::crypto::module),
        ("压缩", crate::zip::module),
        ("表格", tables::module),
        ("json", jsonmod::module),
        ("正则", crate::regex::module),
        ("网络", crate::http::module),
        // M41：容器 / 迭代 / 参数 / 日志
        // 注意是「容器」不是「集合」——`集合` 是内建函数名，同名模块会遮蔽它
        ("容器", containers::module),
        ("迭代", iter::module),
        ("参数", args::module),
        ("日志", log::module),
        // M50 新增的六个模块
        ("统计", stats::module),
        ("向量", vector::module),
        ("矩阵", matrix::module),
        ("标识", ident::module),
        ("编码", encode::module),
        ("html", htmlmod::module),
        ("对比", diffmod::module),
        ("配置", config::module),
        // 2026-10-05：补上 `测试`（以前只有 Python 有，宿主挂 HOST_EXEMPT）
        ("测试", testmod::module),
        // M112：数据持久化（SQLite，走 `rusqlite`）。⚠️ wasm 上**没有**这一项
        // （见下面的 `module()` 里的专门报错）。
        #[cfg(feature = "sqlite")]
        ("数据库", db::module),
        // M114：批量 IO 并发。⚠️ 与 `数据库` 不同，它**每个平台都在表里** ——
        // 只是 wasm 上顺序执行（同一份结果，见 `concurrent.rs`）。
        ("并发", concurrent::module),
        // M115：本地 HTTP 小服务。表里**每个平台都在**（wasm 上只是
        // `启动`/`空闲端口` 明确报「这个平台起不了」）。
        ("HTTP服务端", server::module),
        // M122：路由（纯逻辑，五引擎都能跑）
        ("HTTP路由", router::module),
        // M123：Excel 读（zip + 最小 XML 扫描，五引擎都能跑）
        ("Excel", excel::module),
    ]
}

/// 标准库模块的分发表。
///
/// `Val::Builtin` 只能存**无捕获的函数指针**，所以这里只能是「名字 → 表」的
/// 形式，模块表在每次 `导入` 时现场构造（与 Python 侧 `stdlib_module` 同构）。
pub(crate) fn module(name: &str) -> Result<Val, JishiError> {
    for (n, build) in all_modules() {
        if *n == name {
            return Ok(Val::Module(Box::new((*n).to_string()), Box::new(build())));
        }
    }
    // 🔴 `数据库` 在**关掉 sqlite 特性的平台**（浏览器试玩页 / wasm）上不在表里。
    //    这时候**不能**回落到下面那句笼统的「没有找到标准库模块」—— 那句会让人
    //    以为**写错了名字**（M49a 的教训：提示要可操作，别把人带偏）。
    #[cfg(not(feature = "sqlite"))]
    if name == "数据库" {
        // ⚠️ 全限定路径：这段只在**关掉 sqlite 特性**的平台上编译（wasm），
        //    写进 `use` 会让原生构建报「导入了没用的东西」。
        return Err(crate::err_with_hint(
            "找不到这个函数",
            "这个平台没有内置 SQLite（浏览器试玩页链不了它）",
            "换桌面版/命令行版就能用 `导入 数据库` —— 单二进制里带着 SQLite".to_string(),
        ));
    }
    // ⚠️ 类型名要写 Python 侧那个真名（`SemanticFuncError` 的 title，
    // E0302）—— 见 `lib.rs` 顶部「运行期错误的类型名」那段说明（R6.2）。
    Err(err("找不到这个函数", format!("没有找到标准库模块「{name}」")))
}

/// 这个宿主**实际带的标准库模块与函数**，打成 JSON（M51）。
///
/// 用途只有一个：给 `tests/test_m51_consistency.py` 当事实源 ——
/// 「Python 侧有、宿主没有」这种漂移以前是**静默**的（写 `数学.正弦(30)`
/// 在 Python 上能跑、在宿主上报「模块里没有」）。由宿主自己报家底，
/// 测试才抓得住；比让测试去正则解析源码靠谱（解析一改就崩）。
///
/// 只报 `Val::Builtin`（函数），与 Python 侧 `jishi --lang-spec` 的
/// `stdlib[].functions` 对齐。`数学.圆周率` 这类**常量**不在对比范围内
/// （三边都有，但 M51 未纳入检测 —— 已知边界，见 `AGENTS.md`）。
pub fn dump() -> String {
    let mut parts: Vec<String> = Vec::new();
    for (name, build) in all_modules() {
        let table = build();
        let mut keys: Vec<&str> = table
            .iter()
            .filter(|(_, v)| matches!(v, Val::Builtin(..)))
            .map(|(k, _)| k.as_str())
            .collect();
        keys.sort_unstable();
        let items: Vec<String> = keys.iter().map(|k| quote(k)).collect();
        parts.push(format!("{}:[{}]", quote(name), items.join(",")));
    }
    format!("{{{}}}", parts.join(","))
}

/// 极简 JSON 字符串字面量（零依赖，模块名与函数名都是标识符）。
fn quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

// ---------------------------------------------------------------------------
// 构造模块表的语法糖
// ---------------------------------------------------------------------------

/// 从 `"名字" => 函数` 列表造一个模块表。
///
/// 直接用 `HashMap::insert` 写会变成一百多行样板，宏能省掉。
///
/// **第一个参数是模块名**，会拼进函数名（`concat!` 是编译期拼接，零运行时
/// 开销）。理由：宿主要支持命名参数（`加密.摘要("abc", 算法="md5")`），就必须
/// 按「模块.函数」查签名表 —— 全库有 19 组跨模块同名函数（`解析` 在
/// json / 参数 / 日期 都有），只按函数名查会歧义。名字只影响 `display()`。
macro_rules! table {
    ($modname:literal; $( $name:literal => $f:expr ),* $(,)?) => {{
        let mut m: HashMap<String, Val> = HashMap::new();
        $( m.insert($name.to_string(),
                    Val::Builtin(concat!($modname, ".", $name), $f)); )*
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
        // ⚠️ `lo == hi` 时说「N 个」而不是「N 到 N 个」（R6.4）——「1 到 1 个」
        // 虽然不算错，但读起来绕；而且 Python 侧的标准库是真 Python 函数、
        // 由 Python 自己抛 TypeError（`takes 1 positional argument but 2 were
        // given`），翻译过来只可能是「需要 1 个」。**两边要说得一样。**
        let want = if lo == hi {
            format!("{lo} 个")
        } else {
            format!("{lo} 到 {hi} 个")
        };
        return Err(err(
            "类型错误",
            format!("函数「{name}」需要 {want}参数，但传了 {} 个", args.len()),
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
        Val::Set(s) => Ok(s.borrow().to_items()),
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

// ---------------------------------------------------------------------------
// 十进制字符串上的舍入 / 移位 / 千分位（M153）
// ---------------------------------------------------------------------------
//
// 为什么要**在字符串上**做，而不是直接用 f64 运算：
//
// 1. 用户说「四舍五入」想的是「按我写的那个十进制数，五就进一位」——
//    `1.005` 的二进制其实是 1.00499999…，任何 f64 舍入都会给 1.00；
// 2. 更麻烦的是**跨宿主**：Python / Rust / Node 三边只要有一处用了不同的
//    十进制实现，`1.005`、`2.675` 这种边界就会各说各话。
//
// ⇒ 统一规则：取「最短往返」写法（Rust `{:?}`、Python `repr`、JS `String`，
//   三者数值等价、经归一后逐字相同），再按位取舍，**半入（远离零）**。
//   这三份实现（Python `runtime.py` / 这里 / Node `vm.js`）必须逐字对齐。

/// 数值 → 归一后的十进制文本（去掉尾零：`2.0` → `2`）。
pub(crate) fn 最短十进制(x: f64) -> String {
    let s = format!("{:?}", x);          // Debug 给**最短往返**（Display 会给长串）
    去尾零(&十进制_展开(&s))
}

/// `2.0` → `2`、`3.500` → `3.5`（只动小数部分）。
fn 去尾零(s: &str) -> String {
    if !s.contains('.') {
        return s.to_string();
    }
    let (整, 小) = s.split_once('.').unwrap();
    let 小 = 小.trim_end_matches('0');
    if 小.is_empty() { 整.to_string() } else { format!("{整}.{小}") }
}

/// 把科学计数法展开成纯小数写法：`1e-7` → `0.0000001`。
pub(crate) fn 十进制_展开(s: &str) -> String {
    let t = s.trim();
    if !t.contains('e') && !t.contains('E') {
        return t.to_string();
    }
    let 负 = t.starts_with('-');
    let 体 = t.trim_start_matches(['-', '+']);
    let lower = 体.to_ascii_lowercase();
    let (底, 指文) = lower.split_once('e').unwrap_or((&lower, "0"));
    let 指: i32 = 指文.parse().unwrap_or(0);
    let (整, 小) = match 底.split_once('.') {
        Some((a, b)) => (a.to_string(), b.to_string()),
        None => (底.to_string(), String::new()),
    };
    let 数字 = format!("{整}{小}");
    let 点位置 = 整.chars().count() as i32 + 指;
    let chars: Vec<char> = 数字.chars().collect();
    let mut 结果 = String::new();
    if 点位置 <= 0 {
        结果.push_str("0.");
        for _ in 0..(-点位置) { 结果.push('0'); }
        结果.push_str(&数字);
    } else if 点位置 as usize >= chars.len() {
        结果.push_str(&数字);
        for _ in 0..(点位置 as usize - chars.len()) { 结果.push('0'); }
    } else {
        let k = 点位置 as usize;
        结果.push_str(&chars[..k].iter().collect::<String>());
        结果.push('.');
        结果.push_str(&chars[k..].iter().collect::<String>());
    }
    if 负 { format!("-{结果}") } else { 结果 }
}

/// `"-1.005"` → `(true, "1005", 3)`（值 = ±1005 × 10⁻³）。
fn 十进制_解析(s: &str) -> (bool, String, i32) {
    let 负 = s.starts_with('-');
    let 体 = s.trim_start_matches(['-', '+']);
    let (整, 小) = match 体.split_once('.') {
        Some((a, b)) => (a, b),
        None => (体, ""),
    };
    let 原 = format!("{整}{小}");
    let 数字 = 原.trim_start_matches('0');
    let 数字 = if 数字.is_empty() { "0" } else { 数字 };
    (负, 数字.to_string(), 小.chars().count() as i32)
}

/// 「有效数字 + 小数位数」拼回文本（`"1005", 3` → `1.005`）。
fn 十进制_拼(数字: &str, 小数位: i32) -> String {
    if 小数位 <= 0 {
        let mut r = 数字.to_string();
        for _ in 0..(-小数位) { r.push('0'); }
        return if r.is_empty() { "0".to_string() } else { r };
    }
    let n = 小数位 as usize;
    let chars: Vec<char> = 数字.chars().collect();
    if chars.len() <= n {
        let mut r = String::from("0.");
        for _ in 0..(n - chars.len()) { r.push('0'); }
        r.push_str(数字);
        r
    } else {
        let k = chars.len() - n;
        format!("{}.{}", chars[..k].iter().collect::<String>(),
                chars[k..].iter().collect::<String>())
    }
}

/// 有效数字串 +1（`999` → `1000`）。
fn 十进制_进一(数字: &str) -> String {
    let mut 位们: Vec<char> = 数字.chars().collect();
    let mut i = 位们.len();
    while i > 0 {
        i -= 1;
        if 位们[i] == '9' { 位们[i] = '0'; continue; }
        位们[i] = char::from_u32(位们[i] as u32 + 1).unwrap_or('0');
        return 位们.iter().collect();
    }
    format!("1{}", 位们.iter().collect::<String>())
}

/// 按**半入（远离零）**舍到 `小数位` 位；负数位＝往整数高位舍。
pub(crate) fn 十进制_舍入(s: &str, 小数位: i32) -> String {
    let (负, mut 数字, mut 位) = 十进制_解析(&十进制_展开(s));
    if 位 < 小数位 {
        for _ in 0..(小数位 - 位) { 数字.push('0'); }
        位 = 小数位;
    } else if 位 > 小数位 {
        let 丢 = (位 - 小数位) as usize;
        let chars: Vec<char> = 数字.chars().collect();
        let 看 = if 丢 >= chars.len() {
            let mut 补 = vec!['0'; 丢 - chars.len()];
            补.extend_from_slice(&chars);
            补[0]
        } else {
            chars[chars.len() - 丢]
        };
        数字 = if 丢 >= chars.len() {
            "0".to_string()
        } else {
            chars[..chars.len() - 丢].iter().collect()
        };
        if 看 >= '5' {
            数字 = 十进制_进一(&数字);
            位 = 小数位;
        }
        let _ = 位;
    }
    let 文本 = 十进制_拼(&数字, 小数位);
    if 文本.chars().all(|c| c == '0' || c == '.') {
        return 文本;
    }
    if 负 { format!("-{文本}") } else { 文本 }
}

/// 十进制字符串 × 10^k（`百分比` 用；不经过浮点）。
pub(crate) fn 十进制_乘(s: &str, k: i32) -> String {
    let (负, 数字, 位) = 十进制_解析(s);
    let 体 = 十进制_拼(&数字, 位 - k);
    if 负 { format!("-{体}") } else { 体 }
}

/// 整数部分每三位插一个逗号：`1234567.891` → `1,234,567.891`。
pub(crate) fn 十进制_千分位(s: &str) -> String {
    let t = 十进制_展开(s);
    let 负 = t.starts_with('-');
    let 体 = t.trim_start_matches(['-', '+']).to_string();
    let (整, 小) = match 体.split_once('.') {
        Some((a, b)) => (a.to_string(), Some(b.to_string())),
        None => (体.clone(), None),
    };
    let chars: Vec<char> = 整.chars().collect();
    let mut 组: Vec<String> = Vec::new();
    let mut i = chars.len();
    while i > 3 {
        i -= 3;
        组.insert(0, chars[i..i + 3].iter().collect());
    }
    let 头: String = chars[..i].iter().collect();
    组.insert(0, if 头.is_empty() { "0".to_string() } else { 头 });
    let mut 结果 = 组.join(",");
    if let Some(小) = 小 { 结果.push('.'); 结果.push_str(&小); }
    if 负 { format!("-{结果}") } else { 结果 }
}
