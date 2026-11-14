//! 本地包的**编译期展开**（M145）—— `jishi/localpkg.py` 的 Rust 版。
//!
//! 把 `导入 图片格式转换 从 本地包` 展开成「调一个外壳函数」，与基石库层
//! （`jishilib.rs`）完全同一套路：
//!
//! ```text
//! 函数 __本地包_图片格式转换__():
//!     <图片格式转换.jsh 的源码，整体缩进一级>
//!     返回 {"空图": 空图, …}
//!
//! 图片格式转换 = $造模块("图片格式转换", __本地包_图片格式转换__())
//! ```
//!
//! ## 为什么不做「运行时加载」（2026-10-27 实测，别重复踩）
//!
//! 头一版就是运行时加载：读 `.jishi/packages/图片格式转换/`、把包里的 `.jsh` 编成
//! **另一个模块**跑起来、收顶层名字。结果一调用包里的函数就崩：
//!
//! ```text
//! thread 'main' panicked: index out of bounds: the len is 1 but the index is 19
//! ```
//!
//! 因为字节码里 `Func` 只记「代码在**本模块**代码表里的下标」。跨模块调用时
//! 拿主程序的代码表去取包的代码，直接越界。这与 `jishilib.py` 文档里写的
//! 「跨 VM 调用会拿到错误的常量表 —— 本层第一版就是这么撞上的」是同一个坑。
//!
//! 展开成函数之后，包里的函数就是**普通函数**，同一份字节码、同一个模块，
//! 跨模块问题根本不存在。副作用是好事：**Node 与 C VM 自动就支持了**
//! （它们只跑 Python 侧产出的字节码，展开发生在编译期）。
//!
//! ## 与 Python 侧逐条对齐
//!
//! `oracle/jishi/localpkg.py` 是参照实现。**扫描/改写/行号三条规则必须一致**：
//!
//! 1. **扫描（`collect`）不下探 `branches` / `keywords` 里的块体** ——
//!    Python 的 `_collect` 对 `list[tuple]` 只认「元组里是节点的那些」，
//!    块体那一半不看。多下探一层就会出现「Python 没展开、宿主展开了」。
//! 2. **改写（`rewrite`）也不下探块体**，理由同上（`jishilib.rs` 已踩过）。
//! 3. **行号整体减 `1 + 注入的依赖行数`**：外壳源码比包源码多了函数头与依赖注入
//!    那几行，不减的话包里的报错会整体偏移 —— 中文报错的行列是这项目的立身之本。

use std::path::{Path, PathBuf};

use crate::jishilib::{export_names, shift_lines};
use crate::parser::{parse, J, Node};

/// 外壳函数名前缀（与基石库层区分开，看报错时一眼能认出是哪一层）。
const SHELL_PREFIX: &str = "__本地包_";

pub fn shell_name(name: &str) -> String {
    format!("{}{}__", SHELL_PREFIX, name)
}

/// 展开期的失败（**不是** panic）：宿主负责翻成自己的错误对象。
///
/// `line` / `col` 一律是**用户那条 `导入` 语句**的位置 —— 壳函数的源码不在
/// 用户的文件里，把箭头指到包里去只会让人更糊涂。
#[derive(Debug, Clone)]
pub struct PkgErr {
    pub message: String,
    pub hint: Option<String>,
    pub line: i64,
    pub col: i64,
}

fn bad(message: String, line: i64, col: i64) -> PkgErr {
    PkgErr { message, hint: None, line, col }
}

fn bad_hint(message: String, hint: String, line: i64, col: i64) -> PkgErr {
    PkgErr { message, hint: Some(hint), line, col }
}

// ---------------------------------------------------------------------------
// J 树小工具（与 compiler.rs / jishilib.rs 那两套重复，但各自要独立成立）
// ---------------------------------------------------------------------------

fn field<'a>(n: &'a Node, name: &str) -> Option<&'a J> {
    n.fields.iter().find(|(k, _)| *k == name).map(|(_, v)| v)
}

// ---------------------------------------------------------------------------
// 段表（M199 / E5）：包内函数 → 「它在哪个文件、从第几行起」
// ---------------------------------------------------------------------------

/// 找「最后一个起始行 ≤ 行」的那一段（段表按起始行升序，量小，线性扫足够）。
fn 查段(段表: &[(i64, String)], 行: i64) -> Option<(i64, String)> {
    let mut 结果 = None;
    for (起, 文件) in 段表 {
        if *起 > 行 {
            break;
        }
        结果 = Some((*起, 文件.clone()));
    }
    结果
}

/// 给包里的**每个函数节点**打上「来源文件 + 起始行」。
///
/// 🔴 为什么只标 `FuncDef`：只有函数会变成独立的 `Code`，而报错要显示的文件名
///    与行号是挂在 `Code` 上的。标在 `fields` 里而不是给 `Node` 加结构体字段 ——
///    `fields` 本来就是开放键值表，编译期读一下就走，别为一个诊断信息动通用结构。
fn 标记来源(n: &mut Node, 段表: &[(i64, String)]) {
    if std::env::var("JISHI_DBG_E5").is_ok() {
        eprintln!("[E5-标记] kind={} name={} line={:?}", n.kind,
                  as_jstr(field(n, "name")), field(n, "line"));
    }
    if n.kind == "FuncDef" {
        let 行 = match field(n, "line") {
            Some(J::Int(i)) => *i,
            _ => 0,
        };
        if let Some((起, 文件)) = 查段(段表, 行) {
            n.fields.push(("__src_file", J::Str(文件)));
            n.fields.push(("__src_start", J::Int(起)));
        }
    }
    for (_k, v) in n.fields.iter_mut() {
        标记来源值(v, 段表);
    }
}

fn 标记来源值(v: &mut J, 段表: &[(i64, String)]) {
    match v {
        J::Node(b) => 标记来源(b, 段表),
        J::List(items) => {
            for it in items.iter_mut() {
                标记来源值(it, 段表);
            }
        }
        _ => {}
    }
}

fn as_jstr(v: Option<&J>) -> &str {
    match v {
        Some(J::Str(s)) => s.as_str(),
        _ => "",
    }
}

fn as_list(v: Option<&J>) -> &[J] {
    match v {
        Some(J::List(l)) => l.as_slice(),
        _ => &[],
    }
}

fn as_bool(v: Option<&J>) -> bool {
    matches!(v, Some(J::Bool(true)))
}

fn as_int(v: Option<&J>) -> i64 {
    match v {
        Some(J::Int(i)) => *i,
        _ => 1,
    }
}

// ---------------------------------------------------------------------------
// 找包（与 `runtime._package_dirs` / `packages.package_dirs` 同一顺序）
// ---------------------------------------------------------------------------

fn home_dir() -> String {
    std::env::var("USERPROFILE").ok()
        .or_else(|| std::env::var("HOME").ok())
        .unwrap_or_else(|| ".".to_string())
}

fn cwd() -> String {
    std::env::current_dir().map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| ".".to_string())
}

/// 追加包根的**环境变量名**（M147）—— 与 Python 侧 `localpkg.PKG_ROOTS_ENV` 同名同义。
///
/// 为什么要有：包自带的测试里写着 `导入 自己 从 本地包`，而**跑包的测试**不该
/// 要求「先把包装一遍」。`jishi 测试 <包目录>` 会把这个包的父目录塞进来。
///
/// 🔴 为什么用环境变量：同一份「额外包根」要被 Python 编译期、Python 运行时、
/// Rust 宿主、前端/pyo3 **四处**看到。环境变量一次设好，四边都读得到；
/// 内存全局变量则要各自塞一份，pyo3 那条（Python 设不到 pyd 里）一定会漏。
pub const PKG_ROOTS_ENV: &str = "JISHI_PKG_ROOTS";

/// 环境变量里的额外包根（`;` / `:` 分隔，按平台）。
pub fn extra_package_roots() -> Vec<String> {
    let raw = match std::env::var(PKG_ROOTS_ENV) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let sep = if cfg!(windows) { ';' } else { ':' };
    raw.split(sep)
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect()
}

/// 包搜索目录：**脚本目录 → 额外根 → 项目 `.jishi/packages` → 用户目录**。
pub fn package_roots() -> Vec<String> {
    package_roots_at(None)
}

fn 推(v: &mut Vec<String>, d: String) {
    if !d.trim().is_empty() && !v.contains(&d) {
        v.push(d);
    }
}

/// 同上，但**先看脚本自己所在的目录**（M199 / B2）。
///
/// 🔴 为什么要加（`D:/code/基石数据库/语言问题清单.md` 的 B2）：原来只按 **cwd**
///    找 `.jishi/packages`，于是「把库放在脚本旁边」这件事根本不成立 ——
///    在 A 目录 `jishi 工具/备份.jsh` 跑得起来，`cd` 到仓库根再跑同一句就报
///    「没有找到本地包」。用户看到的是**同一份文件忽好忽坏**，而不是路径问题。
///
/// 顺序把脚本目录放**最前**：它是最具体的那一层，跟 `.git` 找仓库、`node_modules`
/// 逐级往上的直觉一致；cwd 与用户目录留作兜底（`jishi 测试 <包>` 那类仍靠它们）。
///
/// ⚠️ 与 Python 侧 `localpkg.package_dirs(脚本目录)` **同序**（`test_m199` 盯着）。
pub fn package_roots_at(base_dir: Option<&str>) -> Vec<String> {
    let mut v: Vec<String> = Vec::new();
    if let Some(b) = base_dir.filter(|b| !b.trim().is_empty()) {
        推(&mut v, Path::new(b).join(".jishi").join("packages")
            .to_string_lossy().to_string());
    }
    for d in extra_package_roots() {
        推(&mut v, d);
    }
    推(&mut v, Path::new(&cwd()).join(".jishi").join("packages")
        .to_string_lossy().to_string());
    推(&mut v, Path::new(&home_dir()).join(".jishi").join("packages")
        .to_string_lossy().to_string());
    v
}

fn package_dirs() -> Vec<String> {
    package_roots()
}

fn find_pkg_dir(name: &str) -> Option<String> {
    find_pkg_dir_at(name, None)
}

fn find_pkg_dir_at(name: &str, base: Option<&str>) -> Option<String> {
    for b in package_roots_at(base) {
        let cand = Path::new(&b).join(name);
        if cand.is_dir() {
            return Some(cand.to_string_lossy().to_string());
        }
    }
    None
}

// ---------------------------------------------------------------------------
// 包.json（这里只需要一层小解析器：名字/版本/入口/依赖）
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
enum MJ {
    Null,
    Bool(bool),
    Num,
    Str(String),
    Arr(Vec<MJ>),
    Obj(Vec<(String, MJ)>),
}

impl MJ {
    fn get(&self, key: &str) -> Option<&MJ> {
        match self {
            MJ::Obj(m) => m.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    fn as_str(&self) -> Option<&str> {
        match self {
            MJ::Str(s) => Some(s.as_str()),
            _ => None,
        }
    }
    fn is_empty_obj(&self) -> bool {
        matches!(self, MJ::Obj(m) if m.is_empty())
    }
}

struct JP<'a> {
    s: &'a [u8],
    i: usize,
}

impl<'a> JP<'a> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\r' | b'\n') {
            self.i += 1;
        }
    }
    fn peek(&mut self) -> Option<u8> {
        self.ws();
        self.s.get(self.i).copied()
    }
    fn eat(&mut self, c: u8) -> bool {
        if self.peek() == Some(c) {
            self.i += 1;
            true
        } else {
            false
        }
    }
    fn value(&mut self) -> Result<MJ, String> {
        match self.peek() {
            Some(b'{') => {
                self.i += 1;
                let mut out: Vec<(String, MJ)> = Vec::new();
                if self.eat(b'}') {
                    return Ok(MJ::Obj(out));
                }
                loop {
                    let k = self.string()?;
                    if !self.eat(b':') {
                        return Err("对象里缺冒号".to_string());
                    }
                    let v = self.value()?;
                    out.push((k, v));
                    if self.eat(b',') {
                        continue;
                    }
                    if self.eat(b'}') {
                        return Ok(MJ::Obj(out));
                    }
                    return Err("对象没有正常结束".to_string());
                }
            }
            Some(b'[') => {
                self.i += 1;
                let mut out: Vec<MJ> = Vec::new();
                if self.eat(b']') {
                    return Ok(MJ::Arr(out));
                }
                loop {
                    out.push(self.value()?);
                    if self.eat(b',') {
                        continue;
                    }
                    if self.eat(b']') {
                        return Ok(MJ::Arr(out));
                    }
                    return Err("数组没有正常结束".to_string());
                }
            }
            Some(b'"') => Ok(MJ::Str(self.string()?)),
            Some(b't') => {
                if self.s[self.i..].starts_with(b"true") {
                    self.i += 4;
                    Ok(MJ::Bool(true))
                } else {
                    Err("不是合法 JSON".to_string())
                }
            }
            Some(b'f') => {
                if self.s[self.i..].starts_with(b"false") {
                    self.i += 5;
                    Ok(MJ::Bool(false))
                } else {
                    Err("不是合法 JSON".to_string())
                }
            }
            Some(b'n') => {
                if self.s[self.i..].starts_with(b"null") {
                    self.i += 4;
                    Ok(MJ::Null)
                } else {
                    Err("不是合法 JSON".to_string())
                }
            }
            Some(_) => {
                let start = self.i;
                if self.s[self.i] == b'-' {
                    self.i += 1;
                }
                while self.i < self.s.len()
                    && matches!(self.s[self.i], b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-')
                {
                    self.i += 1;
                }
                if self.i == start {
                    return Err("不是合法 JSON".to_string());
                }
                Ok(MJ::Num)
            }
            None => Err("JSON 没写完".to_string()),
        }
    }
    fn string(&mut self) -> Result<String, String> {
        if !self.eat(b'"') {
            return Err("这里该是一段文本".to_string());
        }
        let mut out = String::new();
        loop {
            let c = *self.s.get(self.i).ok_or_else(|| "文本没写完".to_string())?;
            self.i += 1;
            match c {
                b'"' => return Ok(out),
                b'\\' => {
                    let e = *self.s.get(self.i).ok_or_else(|| "转义没写完".to_string())?;
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let hex = self.s.get(self.i..self.i + 4)
                                .ok_or_else(|| "\\u 转义没写完".to_string())?;
                            let hex = std::str::from_utf8(hex)
                                .map_err(|_| "\\u 转义不是十六进制".to_string())?;
                            let n = u32::from_str_radix(hex, 16)
                                .map_err(|_| "\\u 转义不是十六进制".to_string())?;
                            self.i += 4;
                            // 代理对不处理（包.json 里不会有）；落不下来就给替换符
                            out.push(char::from_u32(n).unwrap_or('\u{fffd}'));
                        }
                        _ => return Err("不认识的转义".to_string()),
                    }
                }
                _ => {
                    // 逐字节拼回 UTF-8：中文是多字节，得按原始字节收
                    let start = self.i - 1;
                    while self.i < self.s.len() && (self.s[self.i] & 0xC0) == 0x80 {
                        self.i += 1;
                    }
                    out.push_str(std::str::from_utf8(&self.s[start..self.i])
                        .map_err(|_| "不是合法 UTF-8".to_string())?);
                }
            }
        }
    }
}

fn parse_json(text: &str) -> Result<MJ, String> {
    let mut p = JP { s: text.as_bytes(), i: 0 };
    let v = p.value()?;
    Ok(v)
}

/// 读 `包.json`。**容忍不存在**（当空表），但「存在却读不懂」要报出来。
fn read_meta(pkg_dir: &str, name: &str, line: i64, col: i64) -> Result<MJ, PkgErr> {
    let path = Path::new(pkg_dir).join("包.json");
    if !path.is_file() {
        return Ok(MJ::Obj(Vec::new()));
    }
    let text = std::fs::read_to_string(&path).map_err(|e| {
        bad(format!("本地包「{name}」的 包.json 读不了：{e}"), line, col)
    })?;
    let meta = parse_json(&text).map_err(|e| {
        bad(format!("本地包「{name}」的 包.json 读不了：{e}"), line, col)
    })?;
    if meta.is_empty_obj() && !text.trim_start().starts_with('{') {
        return Err(bad(format!("本地包「{name}」的 包.json 读不了：顶层要是 JSON 对象"), line, col));
    }
    if let Some(声明) = meta.get("名字").and_then(|v| v.as_str()) {
        let 声明 = 声明.trim();
        if !声明.is_empty() && 声明 != name {
            return Err(bad(format!(
                "包目录「{name}」里的 包.json 声明名字为「{声明}」，不一致"), line, col));
        }
    }
    Ok(meta)
}

/// 检查 `包.json` 的「基石版本」（M201）：这个包要哪一版语言才跑得动。
///
/// ## 为什么要有这个字段
///
/// 下游项目（`基石数据库` 是第一个）会用到某个版本才有的标准库函数与语义修正。
/// 没有声明时，用户拿旧语言装新包，报的是**「没有找到名字 X」** —— 那句话把人
/// 指向「包的代码写错了」，而真正的原因是「你的语言太旧」。查这个要半天。
///
/// 语义与「依赖」的版本约束**同一套**（`version_satisfies`）：
/// `">=0.3.4"` / `">0.3.3"` / `"0.3.4"`（裸版本 = 至少这个版本）。
///
/// ⚠️ 三个刻意的取舍：
///
/// * **缺字段 = 不校验**（老包一个都不改就能继续用）；
/// * **字段写错类型**要报错，不能静默当没写 —— 那会让用户以为声明生效了；
/// * **当前版本取 `env!("CARGO_PKG_VERSION")`**：`frontend` 的版本本来就是
///   语言版本（`tests/test_m111_release.py` 钉着它与 `core` / `pyproject`
///   一致）。不为此在 `expand` 上加参数 —— 那要三个调用方各改一遍，
///   而它们本来就都等于语言版本。
fn 检查基石版本(meta: &MJ, name: &str, line: i64, col: i64) -> Result<(), PkgErr> {
    // 🔴 字段名是 **`基石版本`**，不是新造的「需要基石」：`packages.rs` 的
    //    `check_base_version`（M145）早就用它了，一个能力不能有两个名字。
    //    那处管的是「安装包」这条路；这里补的是「本地包展开」这条路。
    let Some(值) = meta.get("基石版本") else {
        return Ok(());
    };
    let 需要 = match 值 {
        MJ::Str(s) => s.trim().to_string(),
        _ => {
            return Err(bad_hint(
                format!("本地包「{name}」的 包.json 里「基石版本」要写成文本"),
                "例如 \"基石版本\": \">=0.3.4\"".to_string(),
                line,
                col,
            ))
        }
    };
    if 需要.is_empty() {
        return Ok(());
    }
    let 当前 = env!("CARGO_PKG_VERSION");
    if version_satisfies(当前, &需要) {
        return Ok(());
    }
    Err(bad_hint(
        format!("包「{name}」要求的基石版本满足 {需要}，当前是 {当前}"),
        format!(
            "升级语言本体：`jishi 升级`（或去官网下满足 {需要} 的版本）。\n\
             ⚠️ 别去改 包.json 把要求调低 —— 那只会变成运行时一句「没有这个函数」，更难查。"
        ),
        line,
        col,
    ))
}

/// 版本约束（与 `rust/src/packages.rs::version_satisfies` **同一套语义**）。
///
/// ⚠️ 这里**不依赖宿主 crate**：`frontend` 是独立 crate（`core`/`rust`/`wasm`
/// 都依赖它），把包管理的实现拖进来会形成反向依赖。两边语义必须一致，
/// 判据在 `tests/test_m145_localpkg.py`（同一批约束在宿主与前端各判一遍）。
fn version_satisfies(version: &str, constraint: &str) -> bool {
    let c = constraint.trim();
    if c.is_empty() || c == "*" {
        return true;
    }
    let ver = parse_version(version);
    if let Some(rest) = c.strip_prefix('^') {
        let base = parse_version(rest);
        return base <= ver && ver < (base.0 + 1, 0, 0);
    }
    for op in ["==", ">=", "<=", ">", "<"] {
        if let Some(rest) = c.strip_prefix(op) {
            let want = parse_version(rest);
            return match op {
                "==" => ver == want,
                ">=" => ver >= want,
                "<=" => ver <= want,
                ">" => ver > want,
                _ => ver < want,
            };
        }
    }
    ver == parse_version(c)
}

fn parse_version(v: &str) -> (i64, i64, i64) {
    let mut parts: Vec<i64> = v.trim().split('.').take(3)
        .map(|x| x.trim().parse::<i64>().unwrap_or(0)).collect();
    while parts.len() < 3 {
        parts.push(0);
    }
    (parts[0], parts[1], parts[2])
}

/// 要编进来的 `.jsh`：有「入口」只编入口，否则按文件名排序全编。
fn module_files(pkg_dir: &str, name: &str, meta: &MJ, line: i64, col: i64)
    -> Result<Vec<String>, PkgErr>
{
    let mut files: Vec<String> = Vec::new();
    let rd = std::fs::read_dir(pkg_dir).map_err(|e| {
        bad(format!("本地包「{name}」的目录读不了：{e}"), line, col)
    })?;
    for e in rd.filter_map(|e| e.ok()) {
        let f = e.file_name().to_string_lossy().to_string();
        if f.ends_with(".jsh") {
            files.push(f);
        }
    }
    files.sort();
    let entry = meta.get("入口").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    if !entry.is_empty() {
        let f = if entry.ends_with(".jsh") { entry.clone() } else { format!("{entry}.jsh") };
        if !files.iter().any(|x| x == &f) {
            return Err(bad(format!(
                "本地包「{name}」的 包.json 声明入口「{entry}」，但包里没有这个模块文件"),
                line, col));
        }
        return Ok(vec![f]);
    }
    if files.is_empty() {
        return Err(bad(format!("本地包「{name}」里没有 .jsh 模块文件"), line, col));
    }
    Ok(files)
}

// ---------------------------------------------------------------------------
// 包 → 外壳函数
// ---------------------------------------------------------------------------

/// 生成 `[(外壳名, FuncDef)]`：**依赖的在前**，可直接插到程序顶部。
fn shell_defs(name: &str, line: i64, col: i64, stack: &mut Vec<String>,
              base: Option<&str>)
    -> Result<Vec<(String, Node)>, PkgErr>
{
    if stack.iter().any(|x| x == name) {
        let mut chain: Vec<String> = stack.clone();
        chain.push(name.to_string());
        return Err(bad(format!("检测到循环依赖：{}", chain.join(" → ")), line, col));
    }
    let pkg_dir = find_pkg_dir_at(name, base).ok_or_else(|| {
        // 提示里**把找过的地方列出来**（M199 / B2）：原来只说「放到
        // .jishi/packages/」，而实际会找四处；用户放在脚本旁边却被判「没找到」时，
        // 看不到「脚本目录也在找」这条最关键的线索。
        let mut 找过: Vec<String> = package_roots_at(base)
            .into_iter().map(|d| format!("  · {d}")).collect();
        找过.sort();
        bad_hint(format!("没有找到本地包「{name}」"),
                 format!("找过这些目录（放 <目录>/{name}/，含 包.json 与 .jsh 模块）：\n{}",
                         找过.join("\n")),
                 line, col)
    })?;
    stack.push(name.to_string());

    let meta = read_meta(&pkg_dir, name, line, col)?;
    // M201：这个包要哪一版语言（缺字段 = 不校验，老包不受影响）
    检查基石版本(&meta, name, line, col)?;

    // ---- 依赖：先各自建壳，再在本包体里注入一行赋值 ----
    let mut out: Vec<(String, Node)> = Vec::new();
    let mut 依赖名: Vec<String> = Vec::new();
    let deps: Vec<(String, String)> = match meta.get("依赖") {
        Some(MJ::Obj(m)) => m.iter()
            .map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_string()))
            .collect(),
        _ => Vec::new(),
    };
    for (dep_name, constraint) in deps.iter() {
        let mut sub = shell_defs(dep_name, line, col, stack, base)?;
        out.append(&mut sub);

        let dep_version = find_pkg_dir_at(dep_name, base)
            .and_then(|d| read_meta(&d, dep_name, line, col).ok())
            .and_then(|m| m.get("版本").and_then(|v| v.as_str()).map(|s| s.trim().to_string()))
            .unwrap_or_default();
        let dep_version = if dep_version.is_empty() { "0.0.0".to_string() } else { dep_version };
        if !version_satisfies(&dep_version, constraint) {
            return Err(bad_hint(
                format!("包「{name}」依赖「{dep_name}」的版本要满足 '{constraint}'，\
                         但装的是 {dep_version}"),
                format!("装个符合要求的版本重新试试；如果多个包对「{dep_name}」的要求互相打架\
                         （钻石依赖），跑 `jishi 检查` 能看到完整链条"),
                line, col));
        }
        依赖名.push(dep_name.clone());
    }
    stack.pop();

    // ---- 源码拼起来（多份 .jsh 共享一个作用域，见 Python 侧文档第 2 条取舍）----
    let mut 源码 = String::new();
    let mut 首文件: Option<PathBuf> = None;
    //: M199 / E5：**段表** —— 每个模块在拼接源码里的起始行 → 它的真实路径。
    //: 报错要显示「乙.jsh:4」就靠它（原来是「入口.jsh:7」，第 7 行还是拼出来的）。
    let mut 段表: Vec<(i64, String)> = Vec::new();
    for f in module_files(&pkg_dir, name, &meta, line, col)?.iter() {
        let path = Path::new(&pkg_dir).join(f);
        let text = std::fs::read_to_string(&path).map_err(|e| {
            bad(format!("本地包「{name}」的模块「{f}」读不了：{e}"), line, col)
        })?;
        let norm = text.replace("\r\n", "\n").replace('\r', "\n");
        // 路径归一成**绝对**：Python 侧也这么做，否则同一个包在两边显示成
        // 「build\…\乙.jsh」与「D://…//乙.jsh」，对拍时是两串不同的字。
        段表.push((源码.matches('\n').count() as i64 + 1,
                   std::path::absolute(&path).unwrap_or(path.clone())
                       .to_string_lossy().to_string()));
        源码.push_str(&norm);
        源码.push('\n');
        if 首文件.is_none() {
            首文件 = Some(path);
        }
    }
    let 首文件 = 首文件.unwrap_or_else(|| PathBuf::from(format!("{name}.jsh")));
    let 首路径 = 首文件.to_string_lossy().to_string();

    // 导出名：解析拼好的源码，取顶层「函数/类/赋值」的名字
    let program = parse(&源码, &首路径).map_err(|e| {
        bad(format!("本地包「{name}」的模块有语法错误：{}（第 {} 行）", e.message, e.line),
            line, col)
    })?;
    let exports = export_names(&program);
    let pairs: Vec<String> = exports.iter()
        .map(|(out, src)| format!("\"{}\": {}", out, src))
        .collect();

    let body: Vec<String> = 源码.split('\n')
        .map(|l| if l.trim().is_empty() { String::new() } else { format!("    {l}") })
        .collect();
    let lines: Vec<String> = body;
    let shell_src = format!("函数 {}()：\n{}\n    返回 {{{}}}\n",
                            shell_name(name), lines.join("\n"), pairs.join(", "));

    let shell_prog = parse(&shell_src, &首路径).map_err(|e| {
        bad(format!("本地包「{name}」展开失败：{}（第 {} 行）", e.message, e.line), line, col)
    })?;
    let want = shell_name(name);
    for node in as_list(field(&shell_prog, "body")) {
        if let J::Node(n) = node {
            if n.kind == "FuncDef" && as_jstr(field(n, "name")) == want {
                let mut def = n.as_ref().clone();
                // 行号减掉「函数头 1 行」
                shift_lines(&mut def, -1);
                // M199 / E5：给这个包里的**每个函数**打「来自哪个文件、从第几行起」。
                // 必须在 `shift_lines` 之后 —— 标记是按「拼接源码行」查的。
                标记来源(&mut def, &段表);
                // 依赖注入**用节点插**，不写进源码：`$造模块` 里那个 `$`
                // 在词法器里根本写不出来（Python 词法器恰好放行 —— 拼源码
                // 就只有一边能跑，2026-10-27 实测撞到过）。
                prepend_body(&mut def, &依赖名, line, col);
                out.push((want, def));
                return Ok(out);
            }
        }
    }
    Err(bad(format!("本地包「{name}」展开失败：外壳函数没生成出来"), line, col))  // pragma: no cover
}

// ---------------------------------------------------------------------------
// AST 改写：`导入 图片格式转换 从 本地包` → `图片格式转换 = $造模块("图片格式转换", __本地包_图片格式转换__())`
// ---------------------------------------------------------------------------

/// `依赖名 = $造模块("依赖名", __本地包_依赖名__())` —— 一个赋值节点。
fn dep_assign(dep_name: &str, line: i64, col: i64) -> Node {
    let shell_call = Node::new("Call", line, col)
        .f("func", Node::new("Name", line, col).f("id", J::Str(shell_name(dep_name))).boxed())
        .f("args", J::List(Vec::new()))
        .f("keywords", J::List(Vec::new()))
        .boxed();
    let name_node = Node::new("Str", line, col).f("value", J::Str(dep_name.to_string())).boxed();
    let make_mod = Node::new("Call", line, col)
        .f("func", Node::new("Name", line, col).f("id", J::Str("$造模块".into())).boxed())
        .f("args", J::List(vec![name_node, shell_call]))
        .f("keywords", J::List(Vec::new()))
        .boxed();
    Node::new("Assign", line, col)
        .f("target", Node::new("Name", line, col).f("id", J::Str(dep_name.to_string())).boxed())
        .f("op", J::Str("=".into()))
        .f("value", make_mod)
}

/// 把依赖赋值插到函数体**最前面**（语义：包被执行的第一件事就是拿到依赖）。
fn prepend_body(def: &mut Node, deps: &[String], line: i64, col: i64) {
    if deps.is_empty() {
        return;
    }
    let mut nodes: Vec<J> = deps.iter()
        .map(|d| J::Node(Box::new(dep_assign(d, line, col))))
        .collect();
    for (k, v) in def.fields.iter_mut() {
        if *k == "body" {
            if let J::List(items) = v {
                nodes.extend(items.iter().cloned());
                *v = J::List(std::mem::take(&mut nodes));
            }
        }
    }
}

fn is_local_import(n: &Node) -> bool {
    n.kind == "Import" && as_bool(field(n, "from_local"))
}

/// 先探一遍有哪些本地包导入：`[(包名, 行, 列)]`（按首次出现顺序）。
///
/// 🔴 **这里与 `jishilib.rs` 有意不同：下探到所有块体。**
/// 基石库层那套只认「列表里的节点」，`如果` 的 `branches`（列表套列表）里的
/// 块体**不进去** —— 于是 `如果 真：导入 统计` 不展开，运行时回落宿主层
/// （那边有原生实现，所以看不出来）。但本地包**没有宿主层兜底**：
/// Python 会回落到运行时加载（能跑）、宿主只能报错（跑不了）—— 同一段代码
/// 两个结果，正是最该避免的分叉。本地包这一层是新写的、没有历史字节码要保，
/// 所以**下探到底**（函数体 / 分支体 / 循环体 / 捕获体）。
fn collect(program: &Node) -> Vec<(String, i64, i64)> {
    let mut out: Vec<(String, i64, i64)> = Vec::new();

    fn walk(v: &J, out: &mut Vec<(String, i64, i64)>) {
        match v {
            J::Node(n) => {
                if is_local_import(n) {
                    let name = as_jstr(field(n, "name")).to_string();
                    if !out.iter().any(|(k, _, _)| k == &name) {
                        out.push((name, as_int(field(n, "line")), as_int(field(n, "col"))));
                    }
                    return;
                }
                for (_k, fv) in n.fields.iter() {
                    walk(fv, out);
                }
            }
            J::List(items) => {
                for it in items.iter() {
                    walk(it, out);
                }
            }
            _ => {}
        }
    }

    for it in as_list(field(program, "body")).iter() {
        walk(it, &mut out);
    }
    out
}

fn visit(n: Node) -> Node {
    if !is_local_import(&n) {
        return n;
    }
    let name = as_jstr(field(&n, "name")).to_string();
    let line = as_int(field(&n, "line"));
    let col = as_int(field(&n, "col"));
    let alias = as_jstr(field(&n, "alias")).to_string();
    let binding = if alias.is_empty() { name.clone() } else { alias };

    let shell_call = Node::new("Call", line, col)
        .f("func", Node::new("Name", line, col).f("id", J::Str(shell_name(&name))).boxed())
        .f("args", J::List(Vec::new()))
        .f("keywords", J::List(Vec::new()))
        .boxed();
    // ⚠️ 包名要包成 **`Str` 节点**，不能塞裸 `J::Str` —— 编译器按 `kind` 分派，
    //    裸 `J::Str` 不是节点，`compile_expr` 会**直接跳过**（`jishilib.rs` 第一版
    //    就在这儿漏掉过一个实参，产物少一条 `LOAD_CONST`，然后 `CALL` 的实参个数
    //    比栈上的值多 ⇒ 栈下溢、越界 panic。2026-10-27 这里又踩了一次）。
    let name_node = Node::new("Str", line, col).f("value", J::Str(name.clone())).boxed();
    let make_mod = Node::new("Call", line, col)
        .f("func", Node::new("Name", line, col).f("id", J::Str("$造模块".into())).boxed())
        .f("args", J::List(vec![name_node, shell_call]))
        .f("keywords", J::List(Vec::new()))
        .boxed();
    Node::new("Assign", line, col)
        .f("target", Node::new("Name", line, col).f("id", J::Str(binding)).boxed())
        .f("op", J::Str("=".into()))
        .f("value", make_mod)
}

/// 程序里所有 `导入 … 从 本地包`：`[(包名, 行, 列)]`。
///
/// 给宿主用的（沙箱要在**展开之前**拦这类导入 —— 展开本身要读包源码，
/// 而沙箱默认不许文件访问）。返回的是 `collect` 的结果，只是换个公开入口。
pub fn local_imports(program: &Node) -> Vec<(String, i64, i64)> {
    collect(program)
}

/// 深度优先重写：**每个字段**都下探（含 `branches` 的块体、元组里的节点）。
fn rewrite_node(node: &Node) -> Node {
    let mut out = Node::new(node.kind, 0, 0);
    out.fields.clear();
    for (k, v) in &node.fields {
        out.fields.push((k.clone(), rewrite_val(v)));
    }
    out.super_ok = node.super_ok;
    visit(out)
}

fn rewrite_val(v: &J) -> J {
    match v {
        J::Node(n) => J::Node(Box::new(rewrite_node(n))),
        J::List(items) => J::List(items.iter().map(rewrite_val).collect()),
        other => other.clone(),
    }
}

// ---------------------------------------------------------------------------
// 对外入口
// ---------------------------------------------------------------------------

/// 从「脚本文件名」推出它所在的目录，用作本地包的**第一个**搜索根（M199 / B2）。
///
/// 推不出来就返回 `None` —— 那时行为与以前完全一样：
/// - `<输入>` / `<stdin>` 这类虚拟名（REPL、`--eval`）；
/// - **纯文件名**（`a.jsh`）：它的目录就是 cwd，搜索链里本来就有，不必重复。
///
/// ⚠️ 与 Python `localpkg._脚本目录` **同样三条规则**（判据盯着两边同一结果）。
pub fn 脚本目录(filename: &str) -> Option<String> {
    let f = filename.trim();
    if f.is_empty() || f.starts_with('<') {
        return None;
    }
    match Path::new(f).parent() {
        Some(d) if !d.as_os_str().is_empty() => Some(d.to_string_lossy().to_string()),
        _ => None,
    }
}

/// 把程序里所有 `导入 … 从 本地包` 展开掉，返回**新的** AST。
///
/// 没有本地包导入时原样返回（`clone` 一次，可忽略）。
pub fn expand(program: &Node) -> Result<Node, PkgErr> {
    expand_at(program, None)
}

/// 同上，但本地包**先从 `base_dir/.jishi/packages` 找**（M199 / B2）。
///
/// `base_dir` 传脚本所在目录；`None` = 老行为（只看 cwd 与用户目录）。
pub fn expand_at(program: &Node, base_dir: Option<&str>) -> Result<Node, PkgErr> {
    let targets = collect(program);
    if targets.is_empty() {
        return Ok(program.clone());
    }

    // 先建壳（依赖在前；同一个包只建一次）
    let mut shells: Vec<(String, Node)> = Vec::new();
    for (name, line, col) in targets.iter() {
        let mut stack: Vec<String> = Vec::new();
        for (k, d) in shell_defs(name, *line, *col, &mut stack, base_dir)?.into_iter() {
            if !shells.iter().any(|(s, _)| s == &k) {
                shells.push((k, d));
            }
        }
    }

    let mut out = rewrite_node(program);
    let mut body: Vec<J> = shells.into_iter().map(|(_k, d)| J::Node(Box::new(d))).collect();
    for it in as_list(field(&out, "body")).iter() {
        body.push(it.clone());
    }
    for (k, v) in out.fields.iter_mut() {
        if *k == "body" {
            *v = J::List(std::mem::take(&mut body));
        }
    }
    Ok(out)
}
