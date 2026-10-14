//! 语言本体的**升级**与**卸载**（M110）。
//!
//! > 先分清两个「安装 / 卸载」：
//! > * **语言本体** = 解释器自己（装在 `~/.jishi`，或 Windows 的 Inno 目录）—— 模块管这个。
//! > * **包** = `.jishi/packages/` 里的东西 —— 那是 `packages.rs` 的 `安装` / `卸载 <包>`。
//!
//! ## 一条命令升级：**不另写一套安装逻辑**
//!
//! 升级 = 「取版本清单 → 挑本平台的资产 → 下载 → **校验 SHA256** → 用这个平台
//! **本来就有**的安装方式重装一遍」：
//!
//! | 平台 / 形态 | 怎么升 |
//! |---|---|
//! | Linux / macOS（便携） | 解 tar.gz → 跑包里的 `install.sh`（它本来就是幂等的） |
//! | Windows（便携，zip） | 解 zip → 换掉 `bin/` 与 `share/`（带备份 / 回滚） |
//! | Windows（Inno 版） | 跑 `setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART` |
//!
//! 好处：安装逻辑只有一处（`install.sh` / `installer.iss`），升级只是「取包 + 校验 + 调它」。
//!
//! ## 版本清单
//!
//! 站点与 GitHub Release 各有一份 `jishi-release.json`（同一份字节，由
//! `tools/build_version_manifest.py` 生成）。
//!
//! ⚠️ 清单里**只写文件名**（不写绝对地址）—— 下载时按「镜像前缀 + 文件名」拼，
//! 于是同一份清单能同时服务自建站与 GitHub，多镜像回退也不必改清单。
//!
//! ## 环境变量（自动化与测试用）
//!
//! * `JISHI_HOME` —— 安装目录（默认 `~/.jishi`；Inno 版是 `%LOCALAPPDATA%\Programs\jishi`）
//! * `JISHI_RELEASE_INDEX` —— 版本清单地址（给了就**只用**它）
//! * `JISHI_NO_PATH` —— 传给 `install.sh` / `install.ps1`，让它们别动 PATH

use std::fs;
use std::io::{BufRead, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use crate::json::Json;

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

pub const RELEASE_FORMAT: &str = "jishi-release-index";
pub const RELEASE_VERSION: i64 = 1;

/// 版本清单的默认来源，**按顺序尝试**（自建源优先、GitHub 兜底）。
///
/// GitHub 那条走 `releases/latest/download/` —— 它**永远指向最新那一版**，
/// 所以清单地址本身不必随版本改。
///
/// 🔴 **文件名必须是纯 ASCII**：GitHub 会把**非 ASCII 的资产名改写成 `default.json`**
/// （2026-10-14 发布 v0.2.3 时踩到 —— `softprops/action-gh-release` 的源码里就写着
/// 「GitHub can rewrite uploaded asset names」，它把原名塞回 `label`）。
/// 以前叫 `版本.json` ⇒ 「GitHub 兜底」那条永远 404。现改 `jishi-release.json`。
pub const DEFAULT_RELEASE_INDEX_URLS: [&str; 2] = [
    "https://xn--3jsy75e.cn/download/jishi-release.json",
    "https://github.com/benxiaoniao/JISHI/releases/latest/download/jishi-release.json",
];

/// 覆盖默认清单来源的环境变量名。
pub const RELEASE_INDEX_ENV_VAR: &str = "JISHI_RELEASE_INDEX";

/// 自建站的发行包镜像前缀（清单里的 `文件` 拼在它后面）。
pub const MIRROR_PREFIX: &str = "https://xn--3jsy75e.cn/download/";

/// GitHub Release 的资产地址前缀（拼 `v<版本>/<文件>`）。
pub const GITHUB_RELEASE_BASE: &str =
    "https://github.com/benxiaoniao/JISHI/releases/download";

const TIMEOUT: f64 = 60.0;

fn to_text(v: &Json) -> String {
    match v {
        Json::Str(s) => s.clone(),
        Json::Int(i) => i.to_string(),
        Json::Float(f) => {
            if f.fract() == 0.0 {
                format!("{f:.1}")
            } else {
                f.to_string()
            }
        }
        Json::Bool(b) => if *b { "真".into() } else { "假".into() },
        Json::Null => "空".into(),
        other => format!("{other:?}"),
    }
}

// ---------------------------------------------------------------------------
// 形态识别
// ---------------------------------------------------------------------------

/// 语言本体是怎么装的 —— 决定「怎么升 / 怎么卸」。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum 形态 {
    /// Windows 的 Inno Setup 安装（目录里有 `unins000.exe`）。
    安装程序版 { 目录: PathBuf, 卸载器: PathBuf },
    /// 便携版 / POSIX：装在一个普通目录里（`bin/` + `share/` + 安装脚本）。
    便携 { 目录: PathBuf },
}

impl 形态 {
    pub fn 目录(&self) -> &Path {
        match self {
            形态::安装程序版 { 目录, .. } => 目录,
            形态::便携 { 目录 } => 目录,
        }
    }

    pub fn 名字(&self) -> &'static str {
        match self {
            形态::安装程序版 { .. } => "Windows 安装程序版（Inno Setup）",
            形态::便携 { .. } => "便携版 / POSIX 版（解压安装）",
        }
    }
}

fn home_dir() -> PathBuf {
    for key in ["USERPROFILE", "HOME"] {
        if let Ok(v) = std::env::var(key) {
            if !v.trim().is_empty() {
                return PathBuf::from(v);
            }
        }
    }
    PathBuf::from(".")
}

/// 猜「语言本体装在哪」。
///
/// 顺序（前一条命中就返回）：
/// 1. `JISHI_HOME`（明确指定；自动化与「多版本并存」时用）
/// 2. 正在跑的这个可执行文件在 `<home>/bin/` 下 ⇒ home —— **这条最准**：
///    三种形态（Inno / 便携 / POSIX）都符合这个布局
/// 3. Windows Inno 的默认位置 `%LOCALAPPDATA%\Programs\jishi`（**有卸载器才算**）
/// 4. `~/.jishi`
///
/// ⚠️ 开发机上 `rust/target/release/jishi-rs.exe` 的父目录叫 `release` 不叫 `bin`，
/// 所以第 2 条不会把源码树误当安装目录 —— 这正是「按布局认」比「按名字认」稳的地方。
fn 猜安装目录() -> PathBuf {
    if let Ok(v) = std::env::var("JISHI_HOME") {
        if !v.trim().is_empty() {
            return PathBuf::from(v.trim());
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(bin) = exe.parent() {
            if bin.file_name().map(|n| n == "bin").unwrap_or(false) {
                if let Some(home) = bin.parent() {
                    return home.to_path_buf();
                }
            }
        }
    }
    if cfg!(windows) {
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            let p = PathBuf::from(local).join("Programs").join("jishi");
            if p.join("unins000.exe").is_file() {
                return p;
            }
        }
    }
    home_dir().join(".jishi")
}

/// 识别安装形态。
pub fn 安装形态() -> 形态 {
    let 目录 = 猜安装目录();
    let 卸载器 = 目录.join("unins000.exe");
    if 卸载器.is_file() {
        形态::安装程序版 { 目录, 卸载器 }
    } else {
        形态::便携 { 目录 }
    }
}

/// 本机现在是哪一版：先看安装目录里的 `VERSION`（`install.sh` / 升级时会写），
/// 没有就退回落**正在跑的这个二进制自己的版本**（Inno 版就是这种情况）。
pub fn 本机版本(形态: &形态) -> String {
    if let Ok(s) = fs::read_to_string(形态.目录().join("VERSION")) {
        let t = s.trim();
        if !t.is_empty() {
            return t.to_string();
        }
    }
    crate::langdata::LANG_VERSION.to_string()
}

/// 本平台的清单键。
///
/// ⚠️ 只认这三种 —— 发行流水线就产这三种（`.github/workflows/release.yml` 的 matrix）。
/// 别的平台（Intel macOS、arm64 Linux）暂时没有发行包，如实报「没有你的平台」。
pub fn 平台键() -> &'static str {
    if cfg!(windows) {
        "windows-x64"
    } else if cfg!(target_os = "macos") {
        "macos-arm64"
    } else {
        "linux-x64"
    }
}

// ---------------------------------------------------------------------------
// 版本清单
// ---------------------------------------------------------------------------

/// 一个可下载的资产（清单里的一条）。
#[derive(Debug, Clone)]
pub struct 资产 {
    pub 文件: String,
    pub 校验和: String,
    pub 大小: i64,
}

fn 取资产(v: &Json) -> Option<资产> {
    let 文件 = v.get("文件")?.as_str()?.trim().to_string();
    if 文件.is_empty() {
        return None;
    }
    Some(资产 {
        文件,
        校验和: v.get("校验和").map(to_text).unwrap_or_default().trim().to_string(),
        大小: match v.get("大小") {
            Some(Json::Int(i)) => *i as i64,
            _ => -1,
        },
    })
}

/// 某平台的可下载物：`包` 必有，`安装程序`（Windows）可选。
#[derive(Debug, Clone)]
pub struct 平台资产 {
    pub 包: 资产,
    pub 安装程序: Option<资产>,
}

fn 平台节点<'a>(清单: &'a Json, 版本: &str, 平台: &str) -> Option<&'a Json> {
    清单.get("版本")?.get(版本)?.get("平台")?.get(平台)
}

/// 从清单里挑出「某版本 × 某平台」的资产。
pub fn 挑资产(清单: &Json, 版本: &str, 平台: &str) -> Result<平台资产, String> {
    let node = 平台节点(清单, 版本, 平台)
        .ok_or_else(|| format!("版本清单里没有「{版本} / {平台}」的发行包"))?;
    let 包 = 取资产(node.get("包").unwrap_or(&Json::Null))
        .ok_or_else(|| format!("版本清单里「{版本} / {平台}」的「包」字段不完整"))?;
    Ok(平台资产 {
        包,
        安装程序: node.get("安装程序").and_then(取资产),
    })
}

/// 校验清单的格式标记与版本号（与包索引同一个套路：**格式不对就不认**）。
pub fn parse_manifest(text: &str) -> Result<Json, String> {
    let body = crate::json::parse(text).map_err(|e| format!("版本清单不是合法 JSON：{e}"))?;
    let 格式 = body.get("格式").map(to_text).unwrap_or_default();
    if 格式 != RELEASE_FORMAT {
        return Err(format!(
            "不是基石的版本清单（格式标记应为「{RELEASE_FORMAT}」，看到「{格式}」）"));
    }
    match body.get("格式版本") {
        None => return Err("版本清单里缺少「格式版本」".to_string()),
        Some(Json::Int(v)) if *v == RELEASE_VERSION as i128 => {}
        Some(v) => {
            return Err(format!(
                "版本清单的格式版本 {} 与当前支持的 {RELEASE_VERSION} 不兼容",
                to_text(v)));
        }
    }
    match body.get("最新") {
        None => Err("版本清单里缺少「最新」".to_string()),
        _ => Ok(body),
    }
}

fn 清单来源() -> Vec<String> {
    if let Ok(v) = std::env::var(RELEASE_INDEX_ENV_VAR) {
        let t = v.trim();
        if !t.is_empty() {
            return vec![t.to_string()];
        }
    }
    DEFAULT_RELEASE_INDEX_URLS.iter().map(|s| s.to_string()).collect()
}

// ---------------------------------------------------------------------------
// 下载
// ---------------------------------------------------------------------------

/// 临时目录 + **退出时自动清理**的守卫（出错也不留垃圾）。
struct 临时区(PathBuf);

impl 临时区 {
    fn 新(前缀: &str) -> Self {
        let 唯一 = format!(
            "jishi-{}-{}-{前缀}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0));
        let p = std::env::temp_dir().join(唯一);
        let _ = fs::create_dir_all(&p);
        临时区(p)
    }
    fn 路径(&self) -> &Path {
        &self.0
    }
}

impl Drop for 临时区 {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// 该资产的所有可试地址，**按顺序**：
///
/// 1. **清单自己的目录** —— 离线镜像 / 自建镜像 / `jishi 升级 --源 <地址>` 都靠它
///    （`file:///tmp/镜像/jishi-release.json` ⇒ 资产从 `/tmp/镜像/` 取）
/// 2. 自建站 `https://基石.cn/download/`
/// 3. GitHub Release 兜底
///
/// 清单里只写文件名，地址在这里拼 —— 同一份清单能同时服务三个来源。
fn 资产地址们(清单来源: &str, 版本: &str, 文件: &str) -> Vec<String> {
    let 名 = crate::packages::encode_url(文件);
    let mut 出 = Vec::new();
    if let Some((基, _)) = 清单来源.rsplit_once('/') {
        if !基.is_empty() {
            出.push(format!("{基}/{名}"));
        }
    }
    出.push(format!("{MIRROR_PREFIX}{名}"));
    出.push(format!(
        "{GITHUB_RELEASE_BASE}/v{}/{}",
        crate::packages::encode_url(版本),
        crate::packages::encode_url(文件)));
    出
}

/// 按镜像顺序下载一个资产到 `目标`，**过 SHA256 校验**才算数。
///
/// 对不上就**不装**（删掉临时文件并报错）—— 「换个地方下」不等于
/// 「下到另一个东西」，这是那道闸门（与 `tools/fetch_release_mirror.py` 同一纪律）。
fn 下载并校验(版本: &str, 资产: &资产, 目标: &Path, 地址们: &[String]) -> Result<(), String> {
    let _ = 版本;
    let mut 失败: Vec<String> = Vec::new();
    for url in 地址们 {
        let bytes = match crate::packages::fetch(url, TIMEOUT) {
            Ok(b) => b,
            Err(e) => {
                失败.push(format!("{url}（{e}）"));
                continue;
            }
        };
        fs::write(目标, &bytes).map_err(|e| format!("写临时文件失败：{e}"))?;
        if !资产.校验和.is_empty() {
            let want = 资产.校验和
                .strip_prefix("sha256:")
                .unwrap_or(&资产.校验和)
                .to_lowercase();
            let got = crate::packages::sha256_file(&目标.to_string_lossy())?;
            if got != want {
                let _ = fs::remove_file(目标);
                let w: String = want.chars().take(16).collect();
                let g: String = got.chars().take(16).collect();
                return Err(format!(
                    "「{}」的 SHA256 对不上（清单写 {w}…，实际 {g}…）—— \
                     文件可能被改动过，已放弃安装",
                    资产.文件));
            }
        }
        if 资产.大小 >= 0 && bytes.len() as i64 != 资产.大小 && 资产.校验和.is_empty() {
            // 没给哈希时才拿大小当唯一凭据（有哈希就以哈希为准）
            let _ = fs::remove_file(目标);
            return Err(format!(
                "「{}」的大小对不上（清单写 {}，实际 {}）",
                资产.文件,
                资产.大小,
                bytes.len()));
        }
        println!("    ✓ 已下载并校验：{}", 资产.文件);
        return Ok(());
    }
    Err(format!(
        "所有镜像都下载不到「{}」：\n  {}",
        资产.文件,
        失败.join("\n  ")))
}

// ---------------------------------------------------------------------------
// 解包（zip 走 zip.rs，tar.gz 走 gunzip + 手写 tar）
// ---------------------------------------------------------------------------

/// 极简 tar 解析：只认 **普通文件 / 目录 / GNU 长名（`././@LongLink`）**。
///
/// ⚠️ 发行 tar.gz 是 Python 的 `tarfile` 打的（`tools/build_release.py`），
/// 用的是标准 ustar 头 —— 这里够用。遇到不认识的头就**跳过**，
/// 遇到头本身不合法就**明确报错**（宁可当场停，也别半解半不解地装出个坏安装）。
fn 解tar(data: &[u8]) -> Result<Vec<(String, Vec<u8>)>, String> {
    let mut 出: Vec<(String, Vec<u8>)> = Vec::new();
    let mut i = 0usize;
    let mut 挂起长名: Option<String> = None;
    while i + 512 <= data.len() {
        let 头 = &data[i..i + 512];
        if 头.iter().all(|b| *b == 0) {
            break; // 全零块 = 档案结束
        }
        let str_at = |off: usize, len: usize| -> String {
            let s = &头[off..off + len];
            let end = s.iter().position(|b| *b == 0).unwrap_or(len);
            String::from_utf8_lossy(&s[..end]).to_string()
        };
        let 名 = str_at(0, 100);
        let size_s = str_at(124, 12);
        let typeflag = 头[156];
        let size: usize = {
            let t = size_s.trim().trim_end_matches('\0').trim();
            if t.is_empty() {
                0
            } else {
                usize::from_str_radix(t, 8)
                    .map_err(|_| format!("tar 条目大小不是八进制：「{t}」"))?
            }
        };
        i += 512;
        let 内容 = &data[i..(i + size).min(data.len())];
        i += (size + 511) / 512 * 512;

        if typeflag == b'L' {
            let end = 内容.iter().position(|b| *b == 0).unwrap_or(内容.len());
            挂起长名 = Some(
                String::from_utf8_lossy(&内容[..end]).trim_end_matches('\0').to_string());
            continue;
        }
        let 名 = 挂起长名.take().unwrap_or(名);
        match typeflag {
            b'0' | b'\0' => 出.push((名, 内容.to_vec())),
            b'5' => 出.push((format!("{}/", 名.trim_end_matches('/')), Vec::new())),
            _ => { /* pax 扩展头 / 符号链接等：发行包里没有，跳过 */ }
        }
    }
    Ok(出)
}

/// 解 zip / tar.gz 到 `目标目录`，返回**发行包根目录**（含 `bin/` 的那一层）。
fn 解包(包文件: &Path, 目标目录: &Path) -> Result<PathBuf, String> {
    fs::create_dir_all(目标目录).map_err(|e| format!("建临时目录失败：{e}"))?;
    let name = 包文件
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let 条目们: Vec<(String, Vec<u8>)> = if name.ends_with(".zip") {
        let bytes = fs::read(包文件).map_err(|e| format!("读「{name}」失败：{e}"))?;
        crate::zip::read_all(&bytes)
            .map_err(|e| format!("解开「{name}」失败：{}", e.message))?
    } else if name.ends_with(".tar.gz") || name.ends_with(".tgz") {
        let bytes = fs::read(包文件).map_err(|e| format!("读「{name}」失败：{e}"))?;
        let 解开 = crate::zip::gunzip(&bytes)?;
        解tar(&解开)?
    } else {
        return Err(format!("不认识的发行包格式：「{name}」（只认 .zip / .tar.gz）"));
    };
    for (条目名, 数据) in &条目们 {
        if 条目名.ends_with('/') {
            continue;
        }
        let 出 = 目标目录.join(条目名);
        if let Some(p) = 出.parent() {
            let _ = fs::create_dir_all(p);
        }
        fs::write(&出, 数据).map_err(|e| format!("写「{条目名}」失败：{e}"))?;
    }
    // 目录条目也要建出来（空目录在发行包里没有，但避免万一）
    for (条目名, _) in &条目们 {
        if 条目名.ends_with('/') {
            let _ = fs::create_dir_all(目标目录.join(条目名));
        }
    }
    Ok(发行根(目标目录, &条目们))
}

/// 发行包解出来通常套一层 `jishi-<版本>-<平台>/`；返回真正含 `bin/` 的那一层。
fn 发行根(目标: &Path, 条目们: &[(String, Vec<u8>)]) -> PathBuf {
    let mut 顶层: Option<String> = None;
    let mut 有裸_bin = false;
    for (n, _) in 条目们 {
        let 首段 = n.split('/').next().unwrap_or("");
        if 首段 == "bin" {
            有裸_bin = true;
        }
        match &顶层 {
            None => 顶层 = Some(首段.to_string()),
            Some(t) if t == 首段 => {}
            _ => 顶层 = None, // 多个顶层条目 ⇒ 没有统一的那一层
        }
    }
    if 有裸_bin {
        return 目标.to_path_buf();
    }
    match 顶层 {
        Some(t) if !t.is_empty() && 目标.join(&t).is_dir() => 目标.join(t),
        _ => 目标.to_path_buf(),
    }
}

// ---------------------------------------------------------------------------
// 安装（升级的最后一步）
// ---------------------------------------------------------------------------

fn 复制树(源: &Path, 目标: &Path) -> Result<(), String> {
    fs::create_dir_all(目标).map_err(|e| format!("建目录「{}」失败：{e}", 目标.display()))?;
    for e in fs::read_dir(源).map_err(|e| format!("读目录「{}」失败：{e}", 源.display()))? {
        let e = e.map_err(|e| format!("读目录失败：{e}"))?;
        let 来源 = e.path();
        let 到 = 目标.join(e.file_name());
        let 元 = e.metadata().map_err(|e| format!("读元信息失败：{e}"))?;
        if 元.is_dir() {
            复制树(&来源, &到)?;
        } else {
            fs::copy(&来源, &到)
                .map_err(|err| format!("复制「{}」失败：{err}", 来源.display()))?;
        }
    }
    Ok(())
}

/// Windows 上「正在跑的那个 exe」删不掉 ⇒ 落一个**等本进程退出后替换**的小脚本。
fn 落延迟替换脚本(形态: &形态, 发行根: &Path, 版本: &str) -> Result<PathBuf, String> {
    let home = 形态.目录();
    let 脚本 = home.join("jishi-升级.cmd");
    let 文本 = format!(
        "@echo off\r\n\
         rem 基石升级：替换被占用中的 jishi.exe（等它退出）\r\n\
         setlocal\r\n\
         :wait\r\n\
         tasklist /fi \"PID eq {pid}\" | find \"{pid}\" >nul && (timeout /t 1 /nobreak >nul & goto wait)\r\n\
         xcopy /e /i /y \"{src}\\bin\" \"{home}\\bin\" >nul\r\n\
         if exist \"{src}\\share\" xcopy /e /i /y \"{src}\\share\" \"{home}\\share\" >nul\r\n\
         del /q \"{home}\\VERSION\" >nul 2>nul\r\n\
         > \"{home}\\VERSION\" echo {版本}\r\n\
         echo [OK] 基石 jishi 已升级到 {版本}，请重开终端\r\n\
         del /q \"%~f0\"\r\n",
        pid = std::process::id(),
        src = 发行根.display(),
        home = home.display(),
        版本 = 版本,
    );
    fs::write(&脚本, 文本).map_err(|e| format!("写延迟替换脚本失败：{e}"))?;
    Ok(脚本)
}

/// 把 `发行根/{bin,share}` 装进安装目录（**先备份、坏了换回来**）。
///
/// 返回 `Some(脚本)` 表示「正在运行的 exe 被占用，已安排退出后自动替换」。
fn 就地替换(形态: &形态, 发行根: &Path, 版本: &str) -> Result<Option<PathBuf>, String> {
    let home = 形态.目录();
    fs::create_dir_all(home).map_err(|e| format!("建安装目录失败：{e}"))?;
    let 备份 = home.join(format!("bin.old-{}", std::process::id()));
    let _ = fs::remove_dir_all(&备份);

    // 1) 备份现在的 bin（出事了要能换回来）
    if home.join("bin").is_dir() {
        fs::rename(home.join("bin"), &备份)
            .map_err(|e| format!("备份旧 bin 失败（可能被占用）：{e}"))?;
    }

    // 2) 复制新的 bin / share
    let 结果 = (|| -> Result<(), String> {
        let src_bin = 发行根.join("bin");
        if !src_bin.is_dir() {
            return Err(format!("发行包里没有 bin/ 目录（{}）", 发行根.display()));
        }
        复制树(&src_bin, &home.join("bin"))?;
        let src_share = 发行根.join("share");
        if src_share.is_dir() {
            复制树(&src_share, &home.join("share"))?;
        }
        // 安装 / 卸载脚本也一起换新（不同版本的脚本可能不同）
        for 名 in ["install.sh", "uninstall.sh", "install.ps1", "uninstall.ps1"] {
            let s = 发行根.join(名);
            if s.is_file() {
                let _ = fs::copy(&s, home.join(名));
            }
        }
        fs::write(home.join("VERSION"), format!("{版本}\n"))
            .map_err(|e| format!("写 VERSION 失败：{e}"))?;
        Ok(())
    })();

    match 结果 {
        Ok(()) => {
            let _ = fs::remove_dir_all(&备份);
            Ok(None)
        }
        Err(e) => {
            // 3) 回滚：把旧的换回来
            let _ = fs::remove_dir_all(home.join("bin"));
            if 备份.is_dir() {
                let _ = fs::rename(&备份, home.join("bin"));
            }
            if cfg!(windows) {
                // 多半是「正在跑的那个 exe 被占用了」—— 落延迟替换脚本，等它退出后自己换
                let 脚本 = 落延迟替换脚本(形态, 发行根, 版本)?;
                return Ok(Some(脚本));
            }
            Err(format!("替换失败（已回滚到旧版本）：{e}"))
        }
    }
}

// ---------------------------------------------------------------------------
// 升级命令
// ---------------------------------------------------------------------------

fn 取值(rest: &[String], i: &mut usize, key: &str) -> Result<String, String> {
    if let Some((_, v)) = rest[*i].split_once('=') {
        if !v.is_empty() {
            return Ok(v.to_string());
        }
    }
    *i += 1;
    rest.get(*i).cloned().ok_or_else(|| format!("{key} 后面要跟一个值"))
}

/// `jishi 升级 [--演练] [--源 <地址>] [--本地包 <文件>] [--版本 <版本>]`
pub fn cmd_update(rest: &[String]) -> i32 {
    let mut 演练 = false;
    let mut 本地包: Option<String> = None;
    let mut 版本参数: Option<String> = None;
    let mut 源们: Vec<String> = Vec::new();
    let mut i = 0;
    while i < rest.len() {
        let a = rest[i].clone();
        let key = a.split('=').next().unwrap_or("").to_string();
        match key.as_str() {
            "--演练" | "--dry-run" => 演练 = true,
            "--本地包" | "--local" => match 取值(rest, &mut i, "--本地包") {
                Ok(v) => 本地包 = Some(v),
                Err(e) => { eprintln!("{e}"); return 2; }
            },
            "--版本" => match 取值(rest, &mut i, "--版本") {
                Ok(v) => 版本参数 = Some(v),
                Err(e) => { eprintln!("{e}"); return 2; }
            },
            "--源" | "--index" => match 取值(rest, &mut i, "--源") {
                Ok(v) => 源们.push(v),
                Err(e) => { eprintln!("{e}"); return 2; }
            },
            _ if a.starts_with('-') => { eprintln!("未知选项：{a}"); return 2; }
            _ => { eprintln!("多余的参数：{a}"); return 2; }
        }
        i += 1;
    }
    let 来源们 = if 源们.is_empty() { 清单来源() } else { 源们 };

    let 态 = 安装形态();
    let 本版 = 本机版本(&态);
    println!("== 基石 jishi 升级 ==");
    println!("  本机版本：{本版}");
    println!("  安装方式：{}", 态.名字());
    println!("  安装目录：{}", 态.目录().display());

    // 1) 取清单（多源回退）
    let (清单, 清单来处) = match 取清单(&来源们) {
        Ok((m, from)) => {
            println!("  版本清单：{from}");
            (m, from)
        }
        Err(e) => { eprintln!("{e}"); return 1; }
    };
    let 最新 = to_text(清单.get("最新").unwrap_or(&Json::Null));
    let 最新 = 版本参数.clone().unwrap_or(最新);
    println!("  最新版本：{最新}");

    // 2) 已是最新？
    if crate::packages::parse_version(&本版) >= crate::packages::parse_version(&最新) {
        println!("已是最新（{本版}），不用升级。");
        return 0;
    }

    // 3) 挑本平台的资产
    let 平 = 平台键();
    let 资产集 = match 挑资产(&清单, &最新, 平) {
        Ok(a) => a,
        Err(e) => { eprintln!("{e}"); return 1; }
    };
    println!("  平台资产：{}（{平}）", 资产集.包.文件);

    // 4) 拿包
    let 临时 = 临时区::新("update");
    let 包路径: PathBuf;
    if let Some(p) = &本地包 {
        let p = PathBuf::from(p);
        if !p.is_file() {
            eprintln!("本地包「{}」不存在", p.display());
            return 1;
        }
        if !资产集.包.校验和.is_empty() {
            let want = 资产集.包.校验和
                .strip_prefix("sha256:")
                .unwrap_or(&资产集.包.校验和)
                .to_lowercase();
            match crate::packages::sha256_file(&p.to_string_lossy()) {
                Ok(got) if got == want => println!("    ✓ 本地包 SHA256 与清单一致"),
                Ok(got) => {
                    let w: String = want.chars().take(16).collect();
                    let g: String = got.chars().take(16).collect();
                    eprintln!("本地包的 SHA256 对不上（清单写 {w}…，实际 {g}…）—— 已放弃安装");
                    return 1;
                }
                Err(e) => { eprintln!("{e}"); return 1; }
            }
        }
        println!("  使用本地包：{}", p.display());
        包路径 = p;
    } else {
        let 目标 = 临时.路径().join(&资产集.包.文件);
        let 地址们 = 资产地址们(&清单来处, &最新, &资产集.包.文件);
        if 演练 {
            println!("  [演练] 会从这些地址取包（按顺序）：");
            for u in &地址们 {
                println!("        {u}");
            }
        } else if let Err(e) = 下载并校验(&最新, &资产集.包, &目标, &地址们) {
            eprintln!("{e}");
            return 1;
        }
        包路径 = 目标;
    }

    // 5) 装
    if 演练 {
        println!("  [演练] 会执行的安装方式：{}", 安装方式说明(&态, &资产集));
        println!("  [演练] 结束 —— 什么都没动。");
        return 0;
    }
    match 安装(&态, &最新, &资产集, &包路径, 临时.路径(), &清单来处) {
        Ok(说明) => {
            println!("{说明}");
            println!("✅ 已升级到 {最新}（重开一个终端让版本 / PATH 生效）");
            0
        }
        Err(e) => {
            eprintln!("升级失败：{e}");
            1
        }
    }
}

fn 安装方式说明(态: &形态, 资产集: &平台资产) -> String {
    match 态 {
        形态::安装程序版 { .. } => match &资产集.安装程序 {
            Some(a) => format!("运行 {} /VERYSILENT /SUPPRESSMSGBOXES /NORESTART", a.文件),
            None => "（这一版没有 Windows 安装程序资产，走不了静默升级）".to_string(),
        },
        形态::便携 { .. } => {
            if cfg!(windows) {
                "解压 zip → 替换 bin/ 与 share/（先备份，坏了换回来）".to_string()
            } else {
                "解压 tar.gz → 跑包里的 install.sh（JISHI_HOME 指向当前安装目录）".to_string()
            }
        }
    }
}

/// 真正动手装。返回一句「做了什么」给人看。
fn 安装(
    态: &形态,
    版本: &str,
    资产集: &平台资产,
    包路径: &Path,
    临时: &Path,
    清单来处: &str,
) -> Result<String, String> {
    match 态 {
        形态::安装程序版 { 目录, 卸载器 } => {
            let a = 资产集.安装程序.as_ref().ok_or_else(|| {
                format!(
                    "本机是 Windows 安装程序版（{}），但版本清单里没有「安装程序」资产 —— \
                     走不了静默升级。请手动下载安装程序重装。",
                    目录.display())
            })?;
            let 目标 = 临时.join(&a.文件);
            let 地址们 = 资产地址们(清单来处, 版本, &a.文件);
            println!("  下载安装程序：{}", a.文件);
            下载并校验(版本, a, &目标, &地址们)?;
            println!("  运行静默升级（Inno 会自己先卸旧再装新）…");
            let 状态 = Command::new(&目标)
                .args(["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
                .stdin(Stdio::null())
                .status()
                .map_err(|e| format!("启动安装程序失败：{e}"))?;
            if !状态.success() {
                return Err(format!(
                    "安装程序退出码 {} —— 多半是 jishi 正在运行、占着要替换的文件。\
                     关掉所有 jishi 窗口再跑一次 `jishi 升级`；\
                     或手动运行「{}」。（卸载器：{}）",
                    状态.code().unwrap_or(-1),
                    目标.display(),
                    卸载器.display()));
            }
            Ok("  ✓ 安装程序已完成静默升级".to_string())
        }
        形态::便携 { 目录 } => {
            let 发行根 = 解包(包路径, &临时.join("解开"))?;
            if cfg!(windows) {
                match 就地替换(态, &发行根, 版本)? {
                    Some(脚本) => Ok(format!(
                        "  ⚠️ 正在运行的 jishi.exe 被占用 —— 已安排**退出后自动替换**。\n     \
                         关掉所有 jishi 窗口后替换会自动完成；\n     \
                         若没生效，手动运行：{}",
                        脚本.display())),
                    None => Ok(format!("  ✓ 已替换 {}", 目录.display())),
                }
            } else {
                let 安装脚本 = 发行根.join("install.sh");
                if !安装脚本.is_file() {
                    return Err(format!("发行包里没有 install.sh（{}）", 发行根.display()));
                }
                println!("  运行包里的 install.sh …");
                let 状态 = Command::new("bash")
                    .arg(&安装脚本)
                    .env("JISHI_HOME", 目录)
                    .env("JISHI_NO_PATH", "1") // 升级不该再动 PATH（装的时候动过了）
                    .stdin(Stdio::null())
                    .stdout(Stdio::inherit())
                    .stderr(Stdio::inherit())
                    .status()
                    .map_err(|e| format!("启动 install.sh 失败：{e}"))?;
                if !状态.success() {
                    return Err(format!("install.sh 退出码 {}", 状态.code().unwrap_or(-1)));
                }
                Ok(format!("  ✓ 已重装到 {}", 目录.display()))
            }
        }
    }
}

fn 取清单(来源们: &[String]) -> Result<(Json, String), String> {
    let mut 失败: Vec<String> = Vec::new();
    for url in 来源们 {
        match crate::packages::read_index_text(url) {
            Ok(t) => match parse_manifest(&t) {
                Ok(m) => return Ok((m, url.clone())),
                Err(e) => 失败.push(format!("{url}（{e}）")),
            },
            Err(e) => 失败.push(format!("{url}（{e}）")),
        }
    }
    Err(format!(
        "读不到基石的版本清单（试了 {} 个源）：\n  {}\n\
         　· 网络不通或被代理拦了 → 检查网络后重试\n\
         　· 想换别的清单地址 → jishi 升级 --源 <地址>（或设 JISHI_RELEASE_INDEX）\n\
         　· 想离线升级 → 先下好发行包，再 jishi 升级 --本地包 包.zip",
        来源们.len(),
        失败.join("\n  ")))
}

// ---------------------------------------------------------------------------
// 卸载语言本体
// ---------------------------------------------------------------------------

/// 问一句「真的要卸载吗」——非交互（stdin 不是终端）时**必须** `--是`。
fn 确认(问: &str, 已确认: bool) -> bool {
    if 已确认 {
        return true;
    }
    if !std::io::stdin().is_terminal() {
        eprintln!("这会把基石语言本体从这台机器上卸掉。");
        eprintln!("非交互式运行时必须显式加 `--是` 才动手（防止脚本里误触）。");
        return false;
    }
    print!("{问} [是/否] ");
    let _ = std::io::stdout().flush();
    let mut 行 = String::new();
    if std::io::stdin().lock().read_line(&mut 行).is_err() {
        return false;
    }
    let t = 行.trim().to_lowercase();
    matches!(t.as_str(), "是" | "y" | "yes")
}

/// `jishi 卸载 [--自身] [--是] [--演练]` —— 卸**语言本体**。
///
/// ⚠️ `jishi 卸载 <包名>` 走的是另一条路（`packages.rs` 的包管理）。
/// 不带包名 = 卸语言本体，这条太容易误触 ⇒ **必须二次确认**（非交互要 `--是`）。
pub fn cmd_uninstall_self(rest: &[String]) -> i32 {
    let mut 已确认 = false;
    let mut 演练 = false;
    for a in rest {
        match a.as_str() {
            "--自身" | "--self" => {}
            "--是" | "--yes" | "-y" => 已确认 = true,
            "--演练" | "--dry-run" => 演练 = true,
            other => { eprintln!("未知选项：{other}"); return 2; }
        }
    }
    let 态 = 安装形态();
    let 目录 = 态.目录().to_path_buf();
    println!("即将卸载**基石语言本体**（安装目录 {}）", 目录.display());
    println!("  安装方式：{}", 态.名字());
    println!("  ⚠️ 卸载后 `jishi` 就不能用了；`.jishi/packages/` 里的包也会一起没。");
    if 演练 {
        println!("  [演练] 不会真的卸载。");
        return 0;
    }
    if !确认("真的要卸载吗？", 已确认) {
        println!("已取消。");
        return 1;
    }

    match 态 {
        形态::安装程序版 { 卸载器, .. } => {
            println!("  运行卸载程序（静默）…");
            match Command::new(&卸载器)
                .args(["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
                .stdin(Stdio::null())
                .status()
            {
                Ok(s) if s.success() => {
                    println!("✅ 已卸载。重开终端即可。");
                    0
                }
                Ok(s) => {
                    eprintln!("卸载程序退出码 {}。可以手动运行：{}",
                              s.code().unwrap_or(-1), 卸载器.display());
                    1
                }
                Err(e) => {
                    eprintln!("启动卸载程序失败：{e}\n可以手动运行：{}", 卸载器.display());
                    1
                }
            }
        }
        形态::便携 { .. } => {
            // 优先跑安装目录里带的卸载脚本（它顺手把 PATH 行也清了）
            let sh = 目录.join("uninstall.sh");
            let ps1 = 目录.join("uninstall.ps1");
            if cfg!(windows) && ps1.is_file() {
                println!("  运行安装目录里的 uninstall.ps1 …");
                match Command::new("powershell")
                    .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
                    .arg(&ps1)
                    .env("JISHI_HOME", &目录)
                    .env("JISHI_NO_PATH", "1")
                    .stdin(Stdio::null())
                    .status()
                {
                    Ok(s) if s.success() => {
                        println!("✅ 已卸载。重开终端即可。");
                        return 0;
                    }
                    Ok(s) => eprintln!("uninstall.ps1 退出码 {}", s.code().unwrap_or(-1)),
                    Err(e) => eprintln!("跑 uninstall.ps1 失败：{e}"),
                }
            }
            if !cfg!(windows) && sh.is_file() {
                println!("  运行安装目录里的 uninstall.sh …");
                match Command::new("bash")
                    .arg(&sh)
                    .env("JISHI_HOME", &目录)
                    .env("JISHI_NO_PATH", "1")
                    .stdin(Stdio::null())
                    .status()
                {
                    Ok(s) if s.success() => {
                        println!("✅ 已卸载。重开终端即可。");
                        return 0;
                    }
                    Ok(s) => eprintln!("uninstall.sh 退出码 {}", s.code().unwrap_or(-1)),
                    Err(e) => eprintln!("跑 uninstall.sh 失败：{e}"),
                }
            }
            // 没有脚本（或脚本没跑成）—— 直接删目录，并提示 PATH 要自己清
            match fs::remove_dir_all(&目录) {
                Ok(()) => {
                    println!("  已删除 {}", 目录.display());
                    println!("✅ 已卸载。");
                    println!("  ⚠️ 若当初安装时往 PATH 里加过 {}，记得手动删掉那一行。",
                             目录.join("bin").display());
                    0
                }
                Err(e) => {
                    eprintln!("删除「{}」失败：{e}", 目录.display());
                    1
                }
            }
        }
    }
}

