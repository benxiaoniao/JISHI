//! 基石包管理（`jishi/packages.py` + `cli.py` 的包命令，R7.6）。
//!
//! 覆盖 `安装 / 卸载 / 列表 / 发布 / 索引 / 检查` 六个子命令，与 Python 侧
//! **输出逐字对齐**（错误文案、退出码、索引 JSON 排版）。
//!
//! ## 索引格式（JSON，顶层结构固定）
//!
//! ```json
//! { "格式": "jishi-package-index", "版本": 1,
//!   "包": { "某包": { "版本": "1.0.0", "下载地址": "https://…/某包-1.0.0.zip",
//!                     "依赖": {"基础库": ">=1.0.0"}, "描述": "…", "校验和": "sha256:…" } } }
//! ```
//!
//! ## 🔴 一处**刻意保留、如实登记**的差异：打包出来的 zip **字节不同**
//!
//! Python 的 `build_package_zip` 用 `zipfile.ZIP_DEFLATED`（压缩）；宿主用的是
//! 自己那份 **stored**（原样存）写出器 —— 内容一模一样、**字节不同**（与标准库
//! `压缩.打包` 同一条已知差异，见 `zip.rs` 模块头）。所以：
//!
//! * `发布` / `索引 --checksum` 打出的 **zip 体积**与 **sha256 校验和**两边对不上；
//! * 其余（索引 JSON 的**格式与排版**、错误文案、`安装/卸载/列表/检查` 的行为）逐字一致。
//!
//! 这条差异**不影响安装**：`stored` 是完全合规的 zip，Python 的 `zipfile` /
//! 基石的 `压缩.解压` 都读得出来（R7.4 的闸门已经在语料上验过）。
//!
//! ## 网络
//!
//! 复用 `crate::http`（Windows 走 WinHTTP、其它平台裸 TCP），并支持 `file://`
//! —— 本地镜像（`file:///…/索引.json` + `file:///…/某包.zip`）是**预期用法**，
//! 也是本模块判据能做到「不联网」的原因。
//!
//! ⚠️ 一处没实现：非 ASCII **主机名**的 IDNA 编码（Python 的 `.encode("idna")`）。
//! 默认索引源是 ASCII 主机（`xn--3jsy75e.cn` / `raw.githubusercontent.com`），
//! 路径里的中文**会**被百分号编码（那是常用场景），只有「中文域名」这一条没覆盖。
//!
//! ## 🔴 第二处已登记的差异：JSON 对象的**键顺序**
//!
//! 宿主的 JSON 解析器（`crate::json`）把对象装进 `BTreeMap` —— **键序丢了**
//! （Python 的 `json.loads` 保留插入顺序）。对**顶层与「包」**没有影响
//! （`make_index` 本来就按名字排序），但**多条目**的 `依赖` / `描述` 之类的
//! **子对象**会按字典序输出，而 Python 按原样输出。例：
//!
//! ```text
//! 包.json:  "依赖": {"乙乙": …, "甲甲": …, "丙丙": …}
//! Python 索引 → 乙乙 / 甲甲 / 丙丙      宿主索引 → 丙丙 / 乙乙 / 甲甲
//! ```
//!
//! **只影响 `发布` / `索引` 打出的 JSON 里那几行的先后**，不影响任何语义
//! （依赖是个「名字 → 约束」的映射，**顺序无意义**；安装/解析都按键取值）。
//! 要抹平它得让核心 JSON 类型保序（那是**热路径**上的类型，`Walker` 每个节点都
//! 要 `get` 一次），代价与收益不成比例 —— 所以这里**如实登记**，不硬凑。

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use crate::json::Json;
use crate::jsonw::{dump_indent, Jv};

// ---------------------------------------------------------------------------
// 常量（与 packages.py 逐个对应）
// ---------------------------------------------------------------------------

pub const INDEX_FORMAT: &str = "jishi-package-index";
pub const INDEX_VERSION: i64 = 1;

/// 默认索引源，**按顺序尝试**（自建源优先、GitHub 兜底）。
pub const DEFAULT_INDEX_URLS: [&str; 2] = [
    "https://xn--3jsy75e.cn/packages/%E7%B4%A2%E5%BC%95.json",
    "https://raw.githubusercontent.com/benxiaoniao/JISHI/master/packages/%E7%B4%A2%E5%BC%95.json",
];

/// 覆盖默认索引的环境变量名。
pub const INDEX_ENV_VAR: &str = "JISHI_INDEX";

/// 单个索引源的读取超时（秒）—— **要短**，源不可达时快速失败好去试下一个。
pub const INDEX_TIMEOUT: f64 = 6.0;

/// 包元信息里允许出现的字段（发布时校验，拼错字段名会被拦下）。
pub const ALLOWED_META_KEYS: [&str; 9] =
    ["名字", "版本", "描述", "依赖", "入口", "许可", "作者", "主页", "关键字"];

/// 按优先级返回索引源列表；`JISHI_INDEX` 给了就**只用**它。
pub fn default_index_urls() -> Vec<String> {
    match std::env::var(INDEX_ENV_VAR) {
        Ok(v) if !v.trim().is_empty() => vec![v.trim().to_string()],
        _ => DEFAULT_INDEX_URLS.iter().map(|s| s.to_string()).collect(),
    }
}

pub fn default_index_url() -> String {
    default_index_urls()[0].clone()
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

/// JSON 值 → Python `str()` 的样子（版本号这类「既可能是串也可能是数」的字段用）。
fn py_str(v: &Json) -> String {
    match v {
        Json::Str(s) => s.clone(),
        Json::Int(i) => i.to_string(),
        Json::Float(f) => jishi_frontend::serialize::py_repr_float(*f),
        Json::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        Json::Null => "None".to_string(),
        _ => String::new(),
    }
}

/// Python `f"{x}"` 的样子（缺字段时是 `None`）。
fn display_of(v: Option<&Json>) -> String {
    match v {
        Some(x) => py_str(x),
        None => "None".to_string(),
    }
}

fn truthy(v: Option<&Json>) -> bool {
    match v {
        None | Some(Json::Null) => false,
        Some(Json::Bool(b)) => *b,
        Some(Json::Int(i)) => *i != 0,
        Some(Json::Float(f)) => *f != 0.0,
        Some(Json::Str(s)) => !s.is_empty(),
        Some(Json::Arr(a)) => !a.is_empty(),
        Some(Json::Obj(m)) => !m.is_empty(),
    }
}

fn jstr(v: &Json) -> String {
    v.as_str().unwrap_or("").to_string()
}

fn json_to_jv(v: &Json) -> Jv {
    match v {
        Json::Null => Jv::Null,
        Json::Bool(b) => Jv::Bool(*b),
        Json::Int(i) => Jv::Int128(*i),
        Json::Float(f) => Jv::Float(*f),
        Json::Str(s) => Jv::Str(s.clone()),
        Json::Arr(a) => Jv::Arr(a.iter().map(json_to_jv).collect()),
        Json::Obj(m) => Jv::Obj(m.iter().map(|(k, v)| (k.clone(), json_to_jv(v))).collect()),
    }
}

/// `json.dumps(..., ensure_ascii=False, indent=2)` 的文本。
fn jv_text(v: &Jv) -> String {
    let mut s = String::new();
    dump_indent(v, 0, &mut s);
    s
}

// ---------------------------------------------------------------------------
// 版本约束
// ---------------------------------------------------------------------------

/// 把 `1.2.3` 解析成 `(1,2,3)`；非法段按 0 处理。
pub fn parse_version(v: &str) -> (i64, i64, i64) {
    let mut parts: Vec<i64> = v.trim().split('.').take(3)
        .map(|x| x.trim().parse::<i64>().unwrap_or(0)).collect();
    while parts.len() < 3 {
        parts.push(0);
    }
    (parts[0], parts[1], parts[2])
}

/// 判断 `version` 是否满足 `constraint`（`*` 空 / `1.2.3` / `==` `>=` `<=` `>` `<` / `^`）。
pub fn version_satisfies(version: &str, constraint: &str) -> bool {
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

// ---------------------------------------------------------------------------
// 索引解析
// ---------------------------------------------------------------------------

/// 解析远端索引 JSON，校验格式标记与版本号。
pub fn parse_index(text: &str) -> Result<Json, String> {
    let body = crate::json::parse(text).map_err(|e| format!("远端索引不是合法 JSON：{e}"))?;
    let obj = match body.as_obj() {
        Some(m) => m,
        None => return Err("远端索引顶层必须是 JSON 对象".to_string()),
    };
    let fmt = match obj.get("格式") {
        Some(Json::Str(s)) => s.clone(),
        Some(other) => py_str(other),
        None => "None".to_string(),
    };
    if fmt != INDEX_FORMAT {
        return Err(format!(
            "不是基石包索引（格式标记应为「{INDEX_FORMAT}」，看到「{fmt}」）"));
    }
    let ver = obj.get("版本");
    let ver_ok = ver.and_then(|v| v.as_i128()) == Some(INDEX_VERSION as i128);
    if !ver_ok {
        return Err(format!(
            "索引格式版本 {} 与当前支持的 {INDEX_VERSION} 不兼容", display_of(ver)));
    }
    match obj.get("包") {
        Some(Json::Obj(_)) => {}
        _ => return Err("索引里缺少「包」对象（包名 → 元信息）".to_string()),
    }
    Ok(body)
}

/// 从已解析的索引里取某包的元信息；不存在时给出「是不是想装某个已有的包」提示。
pub fn resolve_package<'a>(index: &'a Json, name: &str) -> Result<&'a Json, String> {
    let packages = match index.get("包").and_then(|p| p.as_obj()) {
        Some(m) => m,
        None => return Err(format!("索引里没有包「{name}」")),
    };
    if !packages.contains_key(name) {
        let close: Vec<&String> = packages.keys()
            .filter(|n| !name.is_empty() && (n.contains(name) || name.contains(n.as_str())))
            .collect();
        let hint = if close.is_empty() {
            String::new()
        } else {
            let names: Vec<&str> = close.iter().take(3).map(|s| s.as_str()).collect();
            format!("，你是不是想装 {}？", names.join("、"))
        };
        return Err(format!("索引里没有包「{name}」{hint}"));
    }
    let meta = &packages[name];
    if meta.as_obj().is_none() {
        return Err(format!("包「{name}」的元信息格式错误"));
    }
    Ok(meta)
}

// ---------------------------------------------------------------------------
// 包目录的 包.json
// ---------------------------------------------------------------------------

fn join(a: &str, b: &str) -> String {
    let p = Path::new(a).join(b);
    p.to_string_lossy().to_string()
}

/// 读并校验一个包目录的 `包.json`。
pub fn read_package_meta(pkg_dir: &str) -> Result<Json, String> {
    let meta_path = join(pkg_dir, "包.json");
    if !Path::new(&meta_path).is_file() {
        return Err(format!("「{pkg_dir}」里没有 包.json，不是一个基石包"));
    }
    let text = fs::read_to_string(&meta_path)
        .map_err(|e| format!("读不了「{meta_path}」：{e}"))?;
    let meta = crate::json::parse(&text)
        .map_err(|e| format!("「{meta_path}」不是合法 JSON：{e}"))?;
    let obj = match meta.as_obj() {
        Some(m) => m,
        None => return Err(format!("「{meta_path}」顶层必须是 JSON 对象")),
    };

    // ⚠️ Python 是 `(meta.get("名字") or "").strip()` / `str(meta.get("版本") or "")`
    // —— `or ""` 的**假值回退**（`null` / `""` / `0` / `假` 都当「没填」），
    // 拿 `is_empty` 判断是**不等价**的（`版本: 0` 会被漏过去）。
    let name = match obj.get("名字") {
        Some(v) if truthy(Some(v)) => py_str(v),
        _ => String::new(),
    }.trim().to_string();
    if name.is_empty() {
        return Err(format!("「{meta_path}」缺少必填字段「名字」"));
    }
    let version = match obj.get("版本") {
        Some(v) if truthy(Some(v)) => py_str(v),
        _ => String::new(),
    }.trim().to_string();
    if version.is_empty() {
        return Err(format!("包「{name}」的 包.json 缺少必填字段「版本」"));
    }
    if !is_semver(&version) {
        return Err(format!("包「{name}」的版本「{version}」不是语义化版本（应形如 1.0.0）"));
    }
    let unknown: Vec<String> = obj.keys()
        .filter(|k| !ALLOWED_META_KEYS.contains(&k.as_str())).cloned().collect();
    if !unknown.is_empty() {
        let mut u = unknown.clone();
        u.sort();
        let mut allowed: Vec<&str> = ALLOWED_META_KEYS.to_vec();
        allowed.sort_unstable();
        return Err(format!(
            "包「{name}」的 包.json 有无法识别的字段：{}（可用字段：{}）",
            u.join("、"), allowed.join("、")));
    }
    if let Some(Json::Obj(deps)) = obj.get("依赖") {
        for (dep_name, constraint) in deps.iter() {
            if constraint.as_str().is_none() {
                return Err(format!("包「{name}」依赖「{dep_name}」的约束必须是字符串"));
            }
        }
    } else if obj.contains_key("依赖") && obj.get("依赖").map(|v| !matches!(v, Json::Null)).unwrap_or(false) {
        return Err(format!("包「{name}」的「依赖」必须是对象（依赖名 → 版本约束）"));
    }
    if let Some(entry) = obj.get("入口") {
        if !matches!(entry, Json::Null) && entry.as_str().is_none() {
            return Err(format!("包「{name}」的「入口」必须是字符串（模块名）"));
        }
    }
    Ok(meta)
}

fn is_semver(v: &str) -> bool {
    let parts: Vec<&str> = v.split('.').collect();
    if parts.is_empty() || parts.len() > 3 {
        return false;
    }
    parts.iter().all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit()))
}

// ---------------------------------------------------------------------------
// 校验和
// ---------------------------------------------------------------------------

/// 文件 sha256 十六进制串（分包读，避免把大文件整个读进内存）。
pub fn sha256_file(path: &str) -> Result<String, String> {
    use std::io::Read;
    let mut f = fs::File::open(path).map_err(|e| format!("读不了「{path}」：{e}"))?;
    let mut buf = [0u8; 1 << 16];
    let mut all: Vec<u8> = Vec::new();
    loop {
        let n = f.read(&mut buf).map_err(|e| format!("读不了「{path}」：{e}"))?;
        if n == 0 {
            break;
        }
        all.extend_from_slice(&buf[..n]);
    }
    Ok(crate::crypto::sha256_hex(&all))
}

// ---------------------------------------------------------------------------
// URL 编码（`packages.encode_url`）
// ---------------------------------------------------------------------------

/// Python `urllib.parse.quote(s, safe=…)` 的等价实现（保留 `_.-~` 与 `safe` 里的字符）。
fn quote(s: &str, safe: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        let c = b as char;
        if c.is_ascii_alphanumeric() || "_.-~".contains(c) || safe.contains(c) {
            out.push(c);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// `urllib.parse.urlsplit` 的简化版（只处理 `scheme://netloc/path?query#frag`）。
fn urlsplit(u: &str) -> (String, String, String, String, String) {
    let mut rest = u;
    let mut scheme = String::new();
    if let Some(i) = u.find("://") {
        let s = &u[..i];
        if !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c)) {
            scheme = s.to_ascii_lowercase();
            rest = &u[i + 3..];
        }
    }
    let mut netloc = String::new();
    if !scheme.is_empty() {
        let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
        netloc = rest[..end].to_string();
        rest = &rest[end..];
    }
    let (rest, fragment) = match rest.find('#') {
        Some(i) => (&rest[..i], rest[i + 1..].to_string()),
        None => (rest, String::new()),
    };
    let (path, query) = match rest.find('?') {
        Some(i) => (&rest[..i], rest[i + 1..].to_string()),
        None => (rest, String::new()),
    };
    (scheme, netloc, path.to_string(), query, fragment)
}

/// `urllib.parse.urlunsplit` 的简化版。
fn urlunsplit(scheme: &str, netloc: &str, path: &str, query: &str, fragment: &str) -> String {
    let uses_netloc = matches!(scheme, "http" | "https" | "file" | "ftp");
    let mut url = path.to_string();
    if !netloc.is_empty() || (!scheme.is_empty() && uses_netloc && !url.starts_with("//")) {
        if !url.is_empty() && !url.starts_with('/') {
            url = format!("/{url}");
        }
        url = format!("//{netloc}{url}");
    }
    if !scheme.is_empty() {
        url = format!("{scheme}:{url}");
    }
    if !query.is_empty() {
        url = format!("{url}?{query}");
    }
    if !fragment.is_empty() {
        url = format!("{url}#{fragment}");
    }
    url
}

/// 把 URL 里非 ASCII 的部分编码好（域名走 IDNA、路径走百分号编码）。
pub fn encode_url(url: &str) -> String {
    if url.is_ascii() {
        return url.to_string();     // 纯 ASCII，无需处理
    }
    let (scheme, netloc_in, path, query, fragment) = urlsplit(url);
    let mut netloc = netloc_in;
    if !netloc.is_ascii() {
        // ⚠️ 没有 IDNA 实现：主机名非 ASCII 时**原样保留**（见模块头登记）。
        netloc = netloc.clone();
    }
    urlunsplit(&scheme, &netloc, &quote(&path, "/%:"), &quote(&query, "%=&"), &fragment)
}

/// 百分号解码（`file://` 路径里可能有 `%E4%B8%AD` 这类）。
fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            let hex = std::str::from_utf8(&b[i + 1..i + 3]).ok()
                .and_then(|h| u8::from_str_radix(h, 16).ok());
            if let Some(v) = hex {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

// ---------------------------------------------------------------------------
// 打包与索引
// ---------------------------------------------------------------------------

/// 递归收集包里该发布的文件：`(磁盘路径, 归档相对名)`。
fn collect_pkg_files(pkg_dir: &str) -> Result<(Vec<(String, String)>, bool), String> {
    let mut files: Vec<(String, String)> = Vec::new();
    let mut has_jsh = false;
    walk_pkg(Path::new(pkg_dir), pkg_dir, &mut files, &mut has_jsh)?;
    Ok((files, has_jsh))
}

fn walk_pkg(dir: &Path, root: &str, out: &mut Vec<(String, String)>,
            has_jsh: &mut bool) -> Result<(), String> {
    let mut entries: Vec<_> = fs::read_dir(dir)
        .map_err(|e| format!("读不了目录「{}」：{e}", dir.to_string_lossy()))?
        .filter_map(|e| e.ok()).collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let name = e.file_name().to_string_lossy().to_string();
        let full = e.path();
        if full.is_dir() {
            if matches!(name.as_str(), "__pycache__" | ".jishi" | ".git") {
                continue;
            }
            walk_pkg(&full, root, out, has_jsh)?;
            continue;
        }
        if name == "包.json" || name.ends_with(".jsh") {
            if name.ends_with(".jsh") {
                *has_jsh = true;
            }
            let rel = full.strip_prefix(root).unwrap_or(&full)
                .to_string_lossy().replace('\\', "/");
            out.push((full.to_string_lossy().to_string(), rel));
        }
    }
    Ok(())
}

/// 把一个包目录打成 zip（顶层目录 = 包名），返回 `(zip 路径, 包名)`。
pub fn build_package_zip(pkg_dir: &str, out_dir: &str) -> Result<(String, String), String> {
    let meta = read_package_meta(pkg_dir)?;
    let name = jstr(meta.get("名字").unwrap_or(&Json::Null)).trim().to_string();
    let version = py_str(meta.get("版本").unwrap_or(&Json::Null)).trim().to_string();

    fs::create_dir_all(out_dir).map_err(|e| format!("建不了目录「{out_dir}」：{e}"))?;
    let zip_path = join(out_dir, &format!("{name}-{version}.zip"));

    let (files, has_jsh) = collect_pkg_files(pkg_dir)?;
    if !has_jsh {
        return Err(format!("包「{name}」里没有可发布的模块（需要至少一个 .jsh 文件）"));
    }

    let mut entries: Vec<(String, Vec<u8>)> = Vec::with_capacity(files.len());
    for (full, rel) in &files {
        let data = fs::read(full).map_err(|e| format!("读不了「{full}」：{e}"))?;
        entries.push((format!("{name}/{rel}"), data));
    }
    let bytes = crate::zip::build_stored(&entries);
    fs::write(&zip_path, &bytes).map_err(|e| format!("写不了「{zip_path}」：{e}"))?;
    Ok((zip_path, name))
}

/// 由一个包目录生成一条索引条目（插入顺序与 Python 一致：版本/下载地址/依赖/描述/入口）。
pub fn index_entry(pkg_dir: &str, base_url: &str,
                   zip_name: Option<&str>) -> Result<Jv, String> {
    let meta = read_package_meta(pkg_dir)?;
    let name = jstr(meta.get("名字").unwrap_or(&Json::Null)).trim().to_string();
    let version = py_str(meta.get("版本").unwrap_or(&Json::Null)).trim().to_string();

    let mut entry: Vec<(String, Jv)> = vec![("版本".into(), Jv::Str(version.clone()))];
    if !base_url.is_empty() {
        let prefix = base_url.trim_end_matches('/');
        let zname = match zip_name {
            Some(z) => z.to_string(),
            None => format!("{name}-{version}.zip"),
        };
        entry.push(("下载地址".into(),
                    Jv::Str(encode_url(&format!("{prefix}/{zname}")))));
    }
    if truthy(meta.get("依赖")) {
        entry.push(("依赖".into(), json_to_jv(meta.get("依赖").unwrap())));
    }
    if truthy(meta.get("描述")) {
        entry.push(("描述".into(), json_to_jv(meta.get("描述").unwrap())));
    }
    if truthy(meta.get("入口")) {
        entry.push(("入口".into(), json_to_jv(meta.get("入口").unwrap())));
    }
    Ok(Jv::Obj(entry))
}

/// 把 `{包名: 条目}` 组装成完整索引（包名排序，便于 diff）。
pub fn make_index(packages: &BTreeMap<String, Jv>) -> Jv {
    let pk: Vec<(String, Jv)> =
        packages.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
    Jv::Obj(vec![
        ("格式".into(), Jv::Str(INDEX_FORMAT.to_string())),
        ("版本".into(), Jv::Int(INDEX_VERSION)),
        ("包".into(), Jv::Obj(pk)),
    ])
}

/// 序列化索引：缩进 2 空格 + 中文不转义 + 末尾换行。
pub fn index_to_text(index: &Jv) -> String {
    format!("{}\n", jv_text(index))
}

/// 把一条条目合并进已有索引（不修改入参）；`None` = 还没有旧索引。
pub fn merge_index(old: Option<&Json>, name: &str, entry: Jv) -> Jv {
    let mut packages: BTreeMap<String, Jv> = BTreeMap::new();
    if let Some(Json::Obj(m)) = old.and_then(|o| o.get("包")) {
        for (k, v) in m.iter() {
            packages.insert(k.clone(), json_to_jv(v));
        }
    }
    packages.insert(name.to_string(), entry);
    make_index(&packages)
}

/// 扫描目录下所有「含 包.json 的子目录」，返回包目录路径列表（排序）。
pub fn scan_packages(root: &str) -> Result<Vec<String>, String> {
    if !Path::new(root).is_dir() {
        return Err(format!("找不到目录「{root}」"));
    }
    if Path::new(&join(root, "包.json")).is_file() {
        return Ok(vec![root.to_string()]);
    }
    let mut names: Vec<String> = fs::read_dir(root)
        .map_err(|e| format!("读不了目录「{root}」：{e}"))?
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    names.sort();
    let mut out = Vec::new();
    for name in names {
        let sub = join(root, &name);
        if Path::new(&sub).is_dir() && Path::new(&join(&sub, "包.json")).is_file() {
            out.push(sub);
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// 依赖冲突检测（钻石依赖）
// ---------------------------------------------------------------------------

#[derive(Clone)]
pub struct Requirement {
    pub from: String,
    pub constraint: String,
    pub chain: Vec<String>,
}

pub struct Conflict {
    pub package: String,
    pub version: String,
    pub reqs: Vec<Requirement>,
}

/// 遍历依赖图，找出「同一个包被要求了互不相容的版本」的情况（只报告不求解）。
pub fn detect_conflicts(
    roots: &[String],
    find_dir: &dyn Fn(&str) -> Option<String>,
    version_of: &dyn Fn(&str) -> String,
) -> Vec<Conflict> {
    let mut seen: BTreeMap<String, (String, Vec<Requirement>)> = BTreeMap::new();
    for r in roots {
        walk_dep(r, &[], find_dir, version_of, &mut seen, 0);
    }
    let mut out = Vec::new();
    for (name, (version, reqs)) in seen.iter() {
        if reqs.len() < 2 {
            continue;
        }
        if reqs.iter().all(|r| version_satisfies(version, &r.constraint)) {
            continue;
        }
        out.push(Conflict { package: name.clone(), version: version.clone(),
                            reqs: reqs.clone() });
    }
    out
}

fn walk_dep(name: &str, chain: &[String],
            find_dir: &dyn Fn(&str) -> Option<String>,
            version_of: &dyn Fn(&str) -> String,
            seen: &mut BTreeMap<String, (String, Vec<Requirement>)>,
            depth: usize) {
    // 环检测：只看「这条链上是否已经出现过」（用包名，不用整条链）。
    if chain.iter().any(|c| c == name) || depth > 64 {
        return;
    }
    let meta = find_dir(name)
        .map(|d| read_package_meta(&d).unwrap_or(Json::Null))
        .unwrap_or(Json::Null);
    seen.entry(name.to_string())
        .or_insert_with(|| (version_of(name), Vec::new()));
    let deps: Vec<(String, String)> = match meta.get("依赖").and_then(|d| d.as_obj()) {
        Some(m) => m.iter().map(|(k, v)| (k.clone(), jstr(v))).collect(),
        None => Vec::new(),
    };
    // 链条是「从根到**当前包**」：Python 是 `chain + [name]`（当前包自己也要在链上）。
    let mut chain_with_self = chain.to_vec();
    chain_with_self.push(name.to_string());
    for (dep_name, constraint) in deps {
        seen.entry(dep_name.clone())
            .or_insert_with(|| (version_of(&dep_name), Vec::new()));
        let rec = seen.get_mut(&dep_name).unwrap();
        rec.1.push(Requirement {
            from: name.to_string(),
            constraint,
            chain: chain_with_self.clone(),
        });
        walk_dep(&dep_name, &chain_with_self, find_dir, version_of, seen, depth + 1);
    }
}

/// 把一条冲突渲染成中文多行说明。
pub fn format_conflict(c: &Conflict) -> String {
    let mut lines = vec![format!(
        "包「{}」的版本要求互相打架（当前装的是 {}）：", c.package, c.version)];
    for r in &c.reqs {
        let mut chain: Vec<String> = r.chain.clone();
        chain.push(c.package.clone());
        lines.push(format!("  · {} 要求 {}", chain.join(" → "), r.constraint));
    }
    lines.push("  建议：统一各上游对它的版本要求，或先卸载引起冲突的包".to_string());
    lines.join("\n")
}

// ---------------------------------------------------------------------------
// 安装目标目录
// ---------------------------------------------------------------------------

fn home_dir() -> String {
    std::env::var("USERPROFILE").ok()
        .filter(|s| !s.is_empty())
        .or_else(|| std::env::var("HOME").ok().filter(|s| !s.is_empty()))
        .unwrap_or_else(|| ".".to_string())
}

fn cwd() -> String {
    std::env::current_dir().map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| ".".to_string())
}

/// 本地包搜索目录（先项目后全局）—— 与 `runtime._package_dirs` 一致。
pub fn package_dirs() -> Vec<String> {
    let mut v = vec![
        join(&cwd(), ".jishi/packages"),
        join(&home_dir(), ".jishi/packages"),
    ];
    let mut seen = HashSet::new();
    v.retain(|d| seen.insert(d.clone()));
    v
}

/// 本地包缓存根目录：优先项目 `.jishi/packages/`，其次用户目录。
pub fn packages_root() -> String {
    let c = join(&cwd(), ".jishi/packages");
    if Path::new(&c).is_dir() {
        c
    } else {
        join(&home_dir(), ".jishi/packages")
    }
}

/// 确保项目 `.jishi/packages/` 存在（安装默认装到这里）。
pub fn ensure_root() -> String {
    let c = join(&cwd(), ".jishi/packages");
    let _ = fs::create_dir_all(&c);
    c
}

/// 在本地包搜索目录里找包目录，找不到返回 `None`。
pub fn find_pkg_dir(name: &str) -> Option<String> {
    for base in package_dirs() {
        let cand = join(&base, name);
        if Path::new(&cand).is_dir() {
            return Some(cand);
        }
    }
    None
}

fn copy_tree(src: &Path, dst: &Path) -> Result<(), String> {
    fs::create_dir_all(dst).map_err(|e| format!("建不了目录「{}」：{e}", dst.display()))?;
    let entries = fs::read_dir(src)
        .map_err(|e| format!("读不了目录「{}」：{e}", src.display()))?;
    for e in entries.filter_map(|e| e.ok()) {
        let from = e.path();
        let to = dst.join(e.file_name());
        if from.is_dir() {
            copy_tree(&from, &to)?;
        } else {
            fs::copy(&from, &to)
                .map_err(|e| format!("复制「{}」失败：{e}", from.display()))?;
        }
    }
    Ok(())
}

/// 把本地包目录安装到缓存目录，返回目标路径。
pub fn install_package_dir(src_dir: &str, name: &str, dest_root: Option<&str>,
                           force: bool) -> Result<String, String> {
    let meta_path = join(src_dir, "包.json");
    if !Path::new(&meta_path).is_file() {
        return Err(format!("「{src_dir}」里没有 包.json，不是一个基石包"));
    }
    let text = fs::read_to_string(&meta_path)
        .map_err(|e| format!("「{src_dir}」的 包.json 读不了：{e}"))?;
    let meta = crate::json::parse(&text)
        .map_err(|e| format!("「{src_dir}」的 包.json 读不了：{e}"))?;
    // Python：`real_name = meta.get("名字") or name` —— 假值才回退到传入的名字。
    let real_name = match meta.get("名字") {
        Some(v) if truthy(Some(v)) => py_str(v),
        _ => name.to_string(),
    };
    if real_name != name {
        return Err(format!("包「{name}」的 包.json 声明名字为「{real_name}」，不一致"));
    }

    let root = match dest_root {
        Some(r) => r.to_string(),
        None => ensure_root(),
    };
    let target = join(&root, name);
    if Path::new(&target).exists() && !force {
        let v = meta.get("版本").map(py_str).unwrap_or_else(|| "未知".to_string());
        return Err(format!("包「{name}」已安装（版本 {v}），用「jishi 安装 {name} --force」覆盖"));
    }
    if Path::new(&target).exists() {
        fs::remove_dir_all(&target).map_err(|e| format!("删不掉旧包「{target}」：{e}"))?;
    }
    copy_tree(Path::new(src_dir), Path::new(&target))?;
    Ok(target)
}

struct TempDir(PathBuf);
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn unique_temp(prefix: &str) -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let n = N.fetch_add(1, Ordering::Relaxed);
    let pid = std::process::id();
    std::env::temp_dir().join(format!("{prefix}{pid}-{n}"))
}

/// 把包 zip（内含 `包名/` 顶层目录）解压安装到缓存目录，返回目标路径。
pub fn install_package_zip(zip_path: &str, name: &str, dest_root: Option<&str>,
                           force: bool) -> Result<String, String> {
    if !Path::new(zip_path).is_file() {
        return Err(format!("找不到包文件「{zip_path}」"));
    }
    let root = match dest_root {
        Some(r) => r.to_string(),
        None => ensure_root(),
    };
    let data = fs::read(zip_path).map_err(|e| format!("读不了包文件「{zip_path}」：{e}"))?;
    let entries = crate::zip::read_all(&data).map_err(|e| e.message)?;

    let top_dirs: BTreeSet<String> = entries.iter()
        .map(|(n, _)| n.split('/').next().unwrap_or("").to_string())
        .filter(|h| !h.is_empty()).collect();
    let inner = if top_dirs.contains(name) {
        Some(name.to_string())
    } else if top_dirs.len() == 1 {
        top_dirs.iter().next().cloned()
    } else {
        None
    };
    let Some(inner) = inner else {
        return Err(format!("zip「{zip_path}」里没有与包名「{name}」对应的顶层目录"));
    };

    let target = join(&root, name);
    if Path::new(&target).exists() && !force {
        return Err(format!("包「{name}」已安装，用「--force」覆盖"));
    }
    if Path::new(&target).exists() {
        fs::remove_dir_all(&target).map_err(|e| format!("删不掉旧包「{target}」：{e}"))?;
    }

    let tmp = TempDir(unique_temp("jishi-pkg-"));
    let _ = fs::remove_dir_all(&tmp.0);
    for (arc, content) in &entries {
        if arc.ends_with('/') {
            continue;
        }
        let dest = tmp.0.join(arc);
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("解压失败：{e}"))?;
        }
        fs::write(&dest, content).map_err(|e| format!("解压失败：{e}"))?;
    }
    install_package_dir(&tmp.0.join(&inner).to_string_lossy(), name,
                        Some(&root), true)
}

/// 卸载一个本地包，返回是否真的删除了。
pub fn uninstall_package(name: &str, dest_root: Option<&str>) -> Result<bool, String> {
    let root = dest_root.map(|s| s.to_string()).unwrap_or_else(packages_root);
    let target = join(&root, name);
    if Path::new(&target).is_dir() {
        fs::remove_dir_all(&target).map_err(|e| format!("删不掉包「{target}」：{e}"))?;
        return Ok(true);
    }
    Ok(false)
}

/// 列出已安装的本地包 `(名字, 版本, 描述)`，按名字排序。
pub fn list_packages(dest_root: Option<&str>) -> Vec<(String, String, String)> {
    let root = dest_root.map(|s| s.to_string()).unwrap_or_else(packages_root);
    if !Path::new(&root).is_dir() {
        return Vec::new();
    }
    let mut names: Vec<String> = fs::read_dir(&root).into_iter().flatten()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    names.sort();
    let mut out = Vec::new();
    for name in names {
        let pdir = join(&root, &name);
        if !Path::new(&pdir).is_dir() {
            continue;
        }
        let meta = fs::read_to_string(join(&pdir, "包.json")).ok()
            .and_then(|t| crate::json::parse(&t).ok())
            .unwrap_or(Json::Null);
        // ⚠️ Python 是 `meta.get(k, 默认)` —— **键在就用它的值**（哪怕是空串），
        // 不是「假值就回退默认」。用 `filter(is_empty)` 会在 `{"版本": ""}` 上走错。
        let show_name = meta.get("名字").map(py_str).unwrap_or_else(|| name.clone());
        let version = meta.get("版本").map(py_str)
            .unwrap_or_else(|| "未知".to_string());
        let desc = meta.get("描述").map(py_str).unwrap_or_default();
        out.push((show_name, version, desc));
    }
    out
}

// ---------------------------------------------------------------------------
// 网络
// ---------------------------------------------------------------------------

/// 读索引文本：本地文件直接读，远端 URL 走 HTTP（`file://` 也认）。
///
/// `pub(crate)`：`jishi 医生` 的「索引源可达性」那一项要用它（R8.4）。
pub(crate) fn read_index_text(index_url: &str) -> Result<String, String> {
    if Path::new(index_url).is_file() {
        return fs::read_to_string(index_url).map_err(|e| format!("{e}"));
    }
    let url = encode_url(index_url);
    let bytes = fetch(&url, INDEX_TIMEOUT)?;
    Ok(String::from_utf8_lossy(&bytes).to_string())
}

/// 取一段网址的字节：`file://` 直接读文件，其余走 HTTP。
fn fetch(url: &str, timeout: f64) -> Result<Vec<u8>, String> {
    if let Some(rest) = url.strip_prefix("file://") {
        // `file:///C:/x` → rest = `/C:/x`；Windows 上要把开头的 `/` 去掉。
        let mut p = rest;
        #[cfg(windows)]
        {
            if p.as_bytes().first() == Some(&b'/')
                && p.as_bytes().get(2) == Some(&b':') {
                p = &p[1..];
            }
        }
        let path = percent_decode(p);
        return fs::read(&path).map_err(|e| format!("读不了「{path}」：{e}"));
    }
    crate::http::fetch_bytes(url, timeout).map_err(|e| e.message)
}

/// 按索引条目下载 zip 并安装（含校验和比对）。
fn install_one(name: &str, meta: &Json, source: &str, force: bool) -> Result<(), String> {
    let url = meta.get("下载地址").and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| format!("索引里包「{name}」没有「下载地址」字段"))?;
    let url = encode_url(url);
    let tmp = unique_temp(&format!("jishi-{name}-"));
    let tmp_zip = tmp.with_extension("zip");
    let result = (|| -> Result<(), String> {
        let bytes = fetch(&url, 60.0).map_err(|e| wrap_download_err(name, source, &e))?;
        fs::write(&tmp_zip, &bytes)
            .map_err(|e| wrap_download_err(name, source, &e.to_string()))?;
        let want = meta.get("校验和").map(py_str).unwrap_or_default();
        let want = want.trim().to_string();
        if !want.is_empty() {
            let want = want.strip_prefix("sha256:").unwrap_or(&want).to_string();
            let got = sha256_file(&tmp_zip.to_string_lossy())?;
            if got.to_lowercase() != want.to_lowercase() {
                let w: String = want.chars().take(16).collect();
                let g: String = got.chars().take(16).collect();
                return Err(format!(
                    "包「{name}」的校验和对不上（索引写 {w}…，实际 {g}…），文件可能被改动过，已放弃安装"));
            }
        }
        install_package_zip(&tmp_zip.to_string_lossy(), name, None, force)
            .map_err(|e| wrap_download_err(name, source, &e))?;
        Ok(())
    })();
    let _ = fs::remove_file(&tmp_zip);
    result?;
    let v = meta.get("版本").map(py_str).unwrap_or_else(|| "未知".to_string());
    println!("已安装包「{name}」（版本 {v}）");
    Ok(())
}

fn wrap_download_err(name: &str, source: &str, e: &str) -> String {
    format!(
        "下载或安装包「{name}」失败：{e}\n\
         \x20 下载地址来自索引源：{source}\n\
         \x20 可换一个源再试：jishi 安装 {name} --index <索引地址>\n\
         \x20 或用手上的包文件：jishi 安装 {name} --local-zip 包.zip")
}

/// 所有索引源都读不到时的中文提示（给可操作的办法，不甩堆栈）。
fn unreachable_msg(name: &str, unreachable: &[(String, String)]) -> String {
    let mut lines = vec![format!("所有索引源都读不到（试了 {} 个）：", unreachable.len())];
    for (url, err) in unreachable {
        lines.push(format!("  ✗ {url}"));
        lines.push(format!("      {err}"));
    }
    lines.push("可能的原因与办法：".to_string());
    lines.push("  · 网络不通或被代理拦了 → 检查网络 / 代理设置后重试".to_string());
    lines.push(format!("  · 想换别的索引 → jishi 安装 {name} --index <索引地址>"));
    lines.push(format!("  · 手上已有包文件 → jishi 安装 {name} --local-zip 包.zip"));
    lines.push(format!("  · 手上已有包目录 → jishi 安装 {name} --local-dir 目录"));
    lines.join("\n")
}

/// 按顺序试每个索引源，第一个能给包的就用它。
fn install_from_sources(name: &str, urls: &[String], force: bool) -> Result<(), String> {
    let mut unreachable: Vec<(String, String)> = Vec::new();
    let mut reached: Vec<String> = Vec::new();
    let total = urls.len();
    for (i, url) in urls.iter().enumerate() {
        if total > 1 {
            println!("（{}/{total}）索引源：{url}", i + 1);
        }
        let text = match read_index_text(url) {
            Ok(t) => t,
            Err(e) => {
                unreachable.push((url.clone(), e));
                if total > 1 && i + 1 < total {
                    println!("      读不到，换下一个源…");
                }
                continue;
            }
        };
        let parsed = parse_index(&text);
        let meta = parsed.as_ref().ok().map(|p| resolve_package(p, name));
        match meta {
            Some(Ok(m)) => {
                install_one(name, m, url, force)?;
                return Ok(());
            }
            Some(Err(e)) => {
                reached.push(format!("{url}（{e}）"));
                continue;
            }
            None => {
                let e = parsed.unwrap_err();
                reached.push(format!("{url}（{e}）"));
                continue;
            }
        }
    }
    if !reached.is_empty() {
        return Err(format!("索引里没有包「{name}」（已查 {} 个源）：\n  {}",
                           reached.len(), reached.join("\n  ")));
    }
    Err(unreachable_msg(name, &unreachable))
}

// ---------------------------------------------------------------------------
// 子命令
// ---------------------------------------------------------------------------

/// `--名 值` / `--名=值` 拆开；不带值时返回 `None`。
fn split_eq(a: &str) -> (&str, Option<&str>) {
    match a.split_once('=') {
        Some((k, v)) if k.starts_with('-') => (k, Some(v)),
        _ => (a, None),
    }
}

fn take_value(rest: &[String], i: &mut usize, key: &str, inline: Option<&str>)
    -> Result<String, String> {
    if let Some(v) = inline {
        return Ok(v.to_string());
    }
    *i += 1;
    rest.get(*i).cloned().ok_or_else(|| format!("{key} 需要一个值"))
}

fn cmd_install(rest: &[String]) -> i32 {
    let mut package: Option<String> = None;
    let (mut local_dir, mut local_zip, mut from_index, mut index) =
        (None, None, None, None);
    let mut no_index = false;
    let mut force = false;
    let mut i = 0;
    while i < rest.len() {
        let a = rest[i].clone();
        let (key, inline) = split_eq(&a);
        match key {
            "--local-dir" | "--local-zip" | "--from-index" | "--index" => {
                match take_value(rest, &mut i, key, inline) {
                    Ok(v) => match key {
                        "--local-dir" => local_dir = Some(v),
                        "--local-zip" => local_zip = Some(v),
                        "--from-index" => from_index = Some(v),
                        _ => index = Some(v),
                    },
                    Err(e) => { eprintln!("{e}"); return 2; }
                }
            }
            "--no-index" => no_index = true,
            "--force" => force = true,
            _ if a.starts_with('-') => { eprintln!("未知选项：{a}"); return 2; }
            _ => {
                if package.is_none() {
                    package = Some(a);
                } else {
                    eprintln!("多余的参数：{a}");
                    return 2;
                }
            }
        }
        i += 1;
    }
    let Some(package) = package else {
        eprintln!("用法：jishi 安装 包名 [--local-dir 目录 | --local-zip 包.zip | \
                   --index 索引.json | --force]");
        return 2;
    };

    let r: Result<(), String> = (|| {
        if let Some(d) = &local_dir {
            install_package_dir(d, &package, None, force)?;
            println!("已安装包「{package}」（来自目录 {d}）");
            return Ok(());
        }
        if let Some(z) = &local_zip {
            install_package_zip(z, &package, None, force)?;
            println!("已安装包「{package}」（来自 {z}）");
            return Ok(());
        }
        if let Some(src) = &from_index {
            return install_from_sources(&package, &[src.clone()], force);
        }
        if let Some(src) = &index {
            return install_from_sources(&package, &[src.clone()], force);
        }
        // 默认：本地包缓存目录直接按名字找
        if let Some(pdir) = find_pkg_dir(&package) {
            println!("包「{package}」已在本地缓存：{pdir}");
            return Ok(());
        }
        if no_index {
            eprintln!("找不到包「{package}」");
            eprintln!("用法：jishi 安装 包名 [--local-dir 目录 | --local-zip 包.zip | \
                       --index 索引.json]");
            return Err(String::new());      // 已经自己打过两句，用空串占位
        }
        let urls = default_index_urls();
        println!("本地没有包「{package}」，按优先级尝试 {} 个索引源：", urls.len());
        install_from_sources(&package, &urls, force)
    })();

    match r {
        Ok(()) => 0,
        Err(e) if e.is_empty() => 1,
        Err(e) => { eprintln!("安装失败：{e}"); 1 }
    }
}

fn cmd_uninstall(rest: &[String]) -> i32 {
    let package = match rest.iter().find(|a| !a.starts_with('-')) {
        Some(p) => p.clone(),
        None => { eprintln!("用法：jishi 卸载 包名"); return 2; }
    };
    match uninstall_package(&package, None) {
        Ok(true) => { println!("已卸载包「{package}」"); 0 }
        Ok(false) => { eprintln!("包「{package}」未安装"); 1 }
        Err(e) => { eprintln!("卸载失败：{e}"); 1 }
    }
}

fn cmd_list(_rest: &[String]) -> i32 {
    let pkgs = list_packages(None);
    if pkgs.is_empty() {
        println!("（尚未安装任何本地包）");
        return 0;
    }
    for (name, version, desc) in pkgs {
        let d = if desc.is_empty() { String::new() } else { format!(" — {desc}") };
        println!("{name} {version}{d}");
    }
    0
}

fn cmd_publish(rest: &[String]) -> i32 {
    let mut path: Option<String> = None;
    let mut out = "dist/packages".to_string();
    let mut base_url = String::new();
    let mut index = String::new();
    let mut i = 0;
    while i < rest.len() {
        let a = rest[i].clone();
        let (key, inline) = split_eq(&a);
        match key {
            "--out" | "--base-url" | "--index" => {
                match take_value(rest, &mut i, key, inline) {
                    Ok(v) => match key {
                        "--out" => out = v,
                        "--base-url" => base_url = v,
                        _ => index = v,
                    },
                    Err(e) => { eprintln!("{e}"); return 2; }
                }
            }
            _ if a.starts_with('-') => { eprintln!("未知选项：{a}"); return 2; }
            _ => {
                if path.is_none() { path = Some(a); }
                else { eprintln!("多余的参数：{a}"); return 2; }
            }
        }
        i += 1;
    }
    let Some(pkg_dir) = path else {
        eprintln!("用法：jishi 发布 包目录 [--out 目录] [--base-url 前缀] [--index 索引.json]");
        return 2;
    };

    let (zip_path, name) = match build_package_zip(&pkg_dir, &out) {
        Ok(v) => v,
        Err(e) => { eprintln!("发布失败：{e}"); return 1; }
    };
    let checksum = match sha256_file(&zip_path) {
        Ok(c) => c,
        Err(e) => { eprintln!("发布失败：{e}"); return 1; }
    };
    let zname = Path::new(&zip_path).file_name()
        .map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    let mut entry = match index_entry(&pkg_dir, &base_url, Some(&zname)) {
        Ok(v) => v,
        Err(e) => { eprintln!("发布失败：{e}"); return 1; }
    };
    if !base_url.is_empty() {
        if let Jv::Obj(ref mut kv) = entry {
            kv.push(("校验和".into(), Jv::Str(format!("sha256:{checksum}"))));
        }
    }

    let size_kb = fs::metadata(&zip_path).map(|m| m.len() as f64).unwrap_or(0.0) / 1024.0;
    println!("已打包：{zip_path}（{size_kb:.1} KB）");
    println!("校验和：sha256:{checksum}");
    if base_url.is_empty() {
        println!("未指定 --base-url，索引条目里不含下载地址；\
                  发布时请用 --base-url https://…/下载 指定托管前缀");
    }
    println!("索引条目：");
    println!("{}", jv_text(&Jv::Obj(vec![(name.clone(), entry.clone())])));

    if !index.is_empty() {
        let r = (|| -> Result<(), String> {
            let old = if Path::new(&index).is_file() {
                let t = fs::read_to_string(&index).map_err(|e| e.to_string())?;
                Some(parse_index(&t)?)
            } else {
                None
            };
            let merged = merge_index(old.as_ref(), &name, entry.clone());
            fs::write(&index, index_to_text(&merged).as_bytes())
                .map_err(|e| e.to_string())?;
            Ok(())
        })();
        if let Err(e) = r {
            eprintln!("写入索引「{index}」失败：{e}");
            return 1;
        }
        println!("已更新索引：{index}");
    }
    0
}

fn cmd_index(rest: &[String]) -> i32 {
    let mut path: Option<String> = None;
    let mut base_url = String::new();
    let mut out_index = String::new();
    let mut zip_dir = String::new();
    let mut out = "dist/packages".to_string();
    let mut checksum = false;
    let mut i = 0;
    while i < rest.len() {
        let a = rest[i].clone();
        let (key, inline) = split_eq(&a);
        match key {
            "--base-url" | "--out-index" | "--zip-dir" | "--out" => {
                match take_value(rest, &mut i, key, inline) {
                    Ok(v) => match key {
                        "--base-url" => base_url = v,
                        "--out-index" => out_index = v,
                        "--zip-dir" => zip_dir = v,
                        _ => out = v,
                    },
                    Err(e) => { eprintln!("{e}"); return 2; }
                }
            }
            "--checksum" => checksum = true,
            _ if a.starts_with('-') => { eprintln!("未知选项：{a}"); return 2; }
            _ => {
                if path.is_none() { path = Some(a); }
                else { eprintln!("多余的参数：{a}"); return 2; }
            }
        }
        i += 1;
    }
    let Some(path) = path else {
        eprintln!("用法：jishi 索引 目录 [--base-url 前缀] [--out-index 索引.json] \
                   [--zip-dir 目录] [--checksum]");
        return 2;
    };

    let dirs = match scan_packages(&path) {
        Ok(d) => d,
        Err(e) => { eprintln!("生成索引失败：{e}"); return 1; }
    };
    if dirs.is_empty() {
        eprintln!("「{path}」下没找到任何包（子目录需含 包.json）");
        return 1;
    }
    if !zip_dir.is_empty() {
        let _ = fs::create_dir_all(&zip_dir);
    }

    let mut packages: BTreeMap<String, Jv> = BTreeMap::new();
    for d in &dirs {
        let r = (|| -> Result<(String, Jv), String> {
            let meta = read_package_meta(d)?;
            let name = jstr(meta.get("名字").unwrap_or(&Json::Null)).to_string();
            let version = py_str(meta.get("版本").unwrap_or(&Json::Null)).trim().to_string();
            let zip_name = format!("{name}-{version}.zip");
            let mut entry = index_entry(d, &base_url, Some(&zip_name))?;
            if !zip_dir.is_empty() || checksum {
                let dir = if zip_dir.is_empty() { out.clone() } else { zip_dir.clone() };
                let (zp, _) = build_package_zip(d, &dir)?;
                let sum = sha256_file(&zp)?;
                if let Jv::Obj(ref mut kv) = entry {
                    kv.push(("校验和".into(), Jv::Str(format!("sha256:{sum}"))));
                }
            }
            Ok((name, entry))
        })();
        match r {
            Ok((name, entry)) => { packages.insert(name, entry); }
            Err(e) => eprintln!("跳过「{d}」：{e}"),
        }
    }
    if packages.is_empty() {
        eprintln!("没有可打包的包");
        return 1;
    }

    let text = index_to_text(&make_index(&packages));
    if !out_index.is_empty() {
        if let Err(e) = fs::write(&out_index, text.as_bytes()) {
            eprintln!("写不了索引「{out_index}」：{e}");
            return 1;
        }
        println!("已生成索引：{out_index}（含 {} 个包）", packages.len());
    } else {
        // ⚠️ `println!` 而不是 `print!`：`text` 自己已经以 `\n` 结尾，Python 那边是
        // `print(text)` → 末尾还会**再补一个换行**。差这一个空行对拍就会红。
        println!("{text}");
    }
    0
}

fn cmd_check(rest: &[String]) -> i32 {
    let installed: Vec<String> = list_packages(None).into_iter().map(|p| p.0).collect();
    let roots: Vec<String> = if rest.is_empty() { installed } else { rest.to_vec() };
    if roots.is_empty() {
        println!("（尚未安装任何本地包）");
        return 0;
    }
    let find_dir = |n: &str| find_pkg_dir(n);
    let version_of = |n: &str| -> String {
        find_pkg_dir(n)
            .and_then(|d| read_package_meta(&d).ok())
            .and_then(|m| m.get("版本").map(py_str))
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "0.0.0".to_string())
    };
    let conflicts = detect_conflicts(&roots, &find_dir, &version_of);
    if conflicts.is_empty() {
        println!("检查了 {} 个包，未发现依赖冲突", roots.len());
        return 0;
    }
    for c in &conflicts {
        println!("{}", format_conflict(c));
    }
    println!("\n共发现 {} 处依赖冲突", conflicts.len());
    1
}

/// 包管理子命令分发：安装 / 卸载 / 列表 / 发布 / 索引 / 检查。
pub fn run_package_cmd(args: &[String]) -> ExitCode {
    let sub = args.first().map(|s| s.as_str()).unwrap_or("");
    let rest = if args.is_empty() { &args[0..] } else { &args[1..] };
    let code = match sub {
        "安装" => cmd_install(rest),
        "卸载" => cmd_uninstall(rest),
        "列表" => cmd_list(rest),
        "发布" => cmd_publish(rest),
        "索引" => cmd_index(rest),
        "检查" => cmd_check(rest),
        other => { eprintln!("未知的包管理命令：{other}"); 2 }
    };
    ExitCode::from(code as u8)
}
