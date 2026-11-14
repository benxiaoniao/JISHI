//! `jishi 打包`（M172）—— Rust 这一份。
//!
//! 与 Python 侧 `oracle/jishi/packcmd.py` **同口径**：同样四类产物、同样的文件名、
//! 同样的 `install.sh` / `Info.plist` 文本（逐字），同样的「中间包放临时目录、
//! 成品才落 `--出`」。
//!
//! ⚠️ **两次打包的字节只在「同一个引擎内」相同**（与 `压缩.打包确定` 同一条口径）：
//! Python/Node 侧 zip 用 deflate，Rust 侧 `build_stored` 只做 stored；gzip 也是
//! `gzip_stored`（合法 gzip，但不压缩 —— 见 `zip.rs::gzip_stored` 的说明，
//! `压缩.压缩文件` 一直是这个行为）。所以判据比的是「产物清单 + 解出来的内容」，
//! 不是跨引擎的字节。

use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use crate::{Val, JishiError};

/// 认的产物类型，顺序 = 打印顺序。
const 全部类型: [&str; 4] = ["便携", "安装包", "应用", "压缩包"];

fn 平台(type_: &str) -> &'static str {
    match type_ {
        "安装包" => "windows-x64",
        "应用" => "macos-arm64",
        "压缩包" => "linux-x64",
        _ => "",
    }
}

/// FNV-1a 32 位（与 `站点.资源指纹`、Python `packcmd._fnv1a32` 同一个算法）。
///
/// `CFBundleIdentifier` 只认 ASCII 反向域名，而名字多半是中文 ⇒ 拿指纹当后缀。
/// **不能用随机数**：那会让两次打包的 `Info.plist` 不一样。
fn fnv1a32(文本: &str) -> String {
    let mut h: u32 = 0x811C_9DC5;
    for b in 文本.as_bytes() {
        h ^= *b as u32;
        h = h.wrapping_mul(0x0100_0193);
    }
    format!("{h:08x}")
}

fn 文件名(p: &str) -> String {
    Path::new(&p.replace('\\', "/"))
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default()
}

/// Linux 的 `install.sh`。**逐字与 Python `packcmd.安装脚本` 相同。**
fn 安装脚本(名字: &str, 入口: &str) -> String {
    let 入口名 = 文件名(入口);
    let 链 = if 入口.is_empty() {
        "echo \"这个包没标「入口」，没有建命令\"".to_string()
    } else {
        format!(
            r#"if ln -sf "$DEST/{入口}" "$PREFIX/bin/{入口名}" 2>/dev/null; then
  :
else
  # 有些文件系统不让建软链（FAT / 受限账号 / Windows 上的 sh）——
  # 那就退化成一个两行的启动脚本，效果一样。装不上比装歪了好。
  printf '#!/bin/sh\nexec "%s" "$@"\n' "$DEST/{入口}" > "$PREFIX/bin/{入口名}"
  chmod +x "$PREFIX/bin/{入口名}"
fi"#
        )
    };
    format!(
        r#"#!/bin/sh
# 由 `jishi 打包` 生成 —— 把一个程序装到 $PREFIX/opt/{名字}，入口软链到 $PREFIX/bin。
#
#   ./install.sh                    装到 ~/.local
#   ./install.sh --前缀 /usr/local   装到别处（要写权限）
#   ./install.sh --卸载              卸掉（只删自己装的那份）
set -eu
PREFIX="${{JISHI_PREFIX:-$HOME/.local}}"
UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --前缀|--prefix) PREFIX="$2"; shift 2 ;;
    --卸载|--uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "看不懂这个参数：$1" >&2; exit 2 ;;
  esac
done
APP="{名字}"
HERE=$(cd "$(dirname "$0")" && pwd)
DEST="$PREFIX/opt/$APP"
if [ "$UNINSTALL" = 1 ]; then
  rm -f "$PREFIX/bin/{入口名}"
  rm -rf "$DEST"
  echo "已卸载：$DEST"
  exit 0
fi
mkdir -p "$DEST" "$PREFIX/bin"
for f in "$HERE"/*; do
  base=$(basename "$f")
  [ "$base" = "install.sh" ] && continue
  cp -R "$f" "$DEST/"
done
{链}
echo "已安装：$DEST"
"#
    )
}

/// macOS 的 `Contents/Info.plist`。**逐字与 Python `packcmd.应用清单` 相同。**
fn 应用清单(名字: &str, 版本: &str, 入口: &str) -> String {
    let 可执行 = 文件名(入口);
    let 标识 = format!("cn.jishi.{}", fnv1a32(名字));
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>zh_CN</string>
    <key>CFBundleExecutable</key>
    <string>{可执行}</string>
    <key>CFBundleIdentifier</key>
    <string>{标识}</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>{名字}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>{版本}</string>
    <key>CFBundleVersion</key>
    <string>{版本}</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
"#
    )
}

// ---------------------------------------------------------------------------
// tar（ustar）+ gzip
// ---------------------------------------------------------------------------

fn 八进制(值: u64, 宽: usize) -> Vec<u8> {
    // `宽` 个字节：前面补 0，最后一个字节写 NUL（tar 的老写法）
    let s = format!("{:0width$o}", 值, width = 宽 - 1);
    let mut v = s.into_bytes();
    v.truncate(宽 - 1);
    while v.len() < 宽 - 1 {
        v.insert(0, b'0');
    }
    v.push(0);
    v
}

fn 写头(名: &str, 大小: usize, 是目录: bool) -> Vec<u8> {
    let mut h = vec![0u8; 512];
    // 名字最长 100 字节（**按字节算**，中文一个字三字节）；超了拆成 prefix + name
    let 字节 = 名.as_bytes();
    let (前缀, 主体) = if 字节.len() > 100 {
        let mut 切 = 0;
        for i in (0..字节.len()).rev() {
            if 字节[i] == b'/' && 字节.len() - i - 1 <= 100 && i <= 155 {
                切 = i;
                break;
            }
        }
        if 切 == 0 {
            (&字节[..0], 字节)
        } else {
            (&字节[..切], &字节[切 + 1..])
        }
    } else {
        (&字节[..0], 字节)
    };
    h[..主体.len()].copy_from_slice(主体);
    h[345..345 + 前缀.len()].copy_from_slice(前缀);
    h[100..108].copy_from_slice(&八进制(if 是目录 { 0o755 } else if 名.ends_with(".sh") { 0o755 } else { 0o644 }, 8));
    h[108..116].copy_from_slice(&八进制(0, 8)); // uid
    h[116..124].copy_from_slice(&八进制(0, 8)); // gid
    h[124..136].copy_from_slice(&八进制(大小 as u64, 12));
    h[136..148].copy_from_slice(&八进制(0, 12)); // mtime 定死
    h[148..156].copy_from_slice(b"        "); // 算校验和时这 8 字节当空格
    h[156] = if 是目录 { b'5' } else { b'0' };
    h[257..263].copy_from_slice(b"ustar\0");
    h[263..265].copy_from_slice(b"00");
    // uname / gname 留空（与 Python `TarInfo` 的清零一致）
    let 和: u32 = h.iter().map(|b| *b as u32).sum();
    let 校验 = format!("{:06o}\0 ", 和);
    h[148..156].copy_from_slice(校验.as_bytes());
    h
}

fn 收集(目录: &Path, 顶层: &str) -> Result<Vec<(String, PathBuf)>, String> {
    fn 走(d: &Path, 相对: &str, 出: &mut Vec<(String, PathBuf)>) -> Result<(), String> {
        let rd = fs::read_dir(d).map_err(|e| format!("读目录「{}」失败：{e}", d.display()))?;
        let mut 子们: Vec<PathBuf> = rd.filter_map(|e| e.ok()).map(|e| e.path()).collect();
        子们.sort();
        for p in 子们 {
            let 名 = p.file_name().unwrap_or_default().to_string_lossy().to_string();
            let r = if 相对.is_empty() { 名 } else { format!("{相对}/{名}") };
            if p.is_dir() {
                走(&p, &r, 出)?;
            } else if p.is_file() {
                出.push((r, p));
            }
        }
        Ok(())
    }
    let mut 出 = Vec::new();
    走(目录, "", &mut 出)?;
    出.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let _ = 顶层;
    Ok(出)
}

/// 把 `目录` 打成 `顶层名/...` 的 tar.gz。**三样定死**才不会漂：
/// 条目按名字排序 / mtime 全 0 / uid·gid 与 uname·gname 全空。
fn 写tar_gz(目录: &Path, 顶层: &str, 输出: &Path) -> Result<(), String> {
    let mut tar: Vec<u8> = Vec::new();
    // 顶层目录条目
    tar.extend_from_slice(&写头(&format!("{顶层}/"), 0, true));
    for (相对, 全) in 收集(目录, 顶层)? {
        let 数据 = fs::read(&全).map_err(|e| format!("读「{}」失败：{e}", 全.display()))?;
        tar.extend_from_slice(&写头(&format!("{顶层}/{相对}"), 数据.len(), false));
        tar.extend_from_slice(&数据);
        let 余 = 数据.len() % 512;
        if 余 != 0 {
            tar.extend_from_slice(&vec![0u8; 512 - 余]);
        }
    }
    tar.extend_from_slice(&vec![0u8; 1024]); // 两个全零块 = 结束
    let gz = crate::zip::gzip_stored(&tar);
    fs::write(输出, gz).map_err(|e| format!("写「{}」失败：{e}", 输出.display()))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// 收集 / 解包
// ---------------------------------------------------------------------------

fn 要进包(源: &Path, 出: &Path) -> Vec<String> {
    fn 走(d: &Path, 源: &Path, 出: &Path, 结果: &mut Vec<String>) {
        let rd = match fs::read_dir(d) {
            Ok(v) => v,
            Err(_) => return,
        };
        let mut 子们: Vec<PathBuf> = rd.filter_map(|e| e.ok()).map(|e| e.path()).collect();
        子们.sort();
        for p in 子们 {
            if p.is_dir() {
                // `--出` 若在源目录里，别把自己打进去（否则越打越大）
                if 同路径(&p, 出) {
                    continue;
                }
                走(&p, 源, 出, 结果);
            } else if p.is_file() {
                if p.file_name().map(|n| n == "打包.json").unwrap_or(false)
                    && 同路径(d, 源)
                {
                    continue; // 配置是打包的输入，不是程序的一部分
                }
                结果.push(p.display().to_string());
            }
        }
    }
    fn 同路径(a: &Path, b: &Path) -> bool {
        match (fs::canonicalize(a), fs::canonicalize(b)) {
            (Ok(x), Ok(y)) => x == y,
            _ => a == b,
        }
    }
    let mut 结果 = Vec::new();
    走(源, 源, 出, &mut 结果);
    结果.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    结果
}

fn 解到(数据: &[u8], 目录: &Path) -> Result<(), String> {
    for (名, bytes) in crate::zip_read_all(数据)? {
        if 名.ends_with('/') {
            continue;
        }
        crate::zip_entry_name_ok(&名)?;
        let 全 = 目录.join(名.replace('/', std::path::MAIN_SEPARATOR_STR));
        if let Some(父) = 全.parent() {
            fs::create_dir_all(父).map_err(|e| format!("建目录失败：{e}"))?;
        }
        fs::write(&全, bytes).map_err(|e| format!("写「{}」失败：{e}", 全.display()))?;
    }
    Ok(())
}

fn 拷树(从: &Path, 到: &Path) -> Result<(), String> {
    fs::create_dir_all(到).map_err(|e| format!("建目录「{}」失败：{e}", 到.display()))?;
    for (相对, 全) in 收集(从, "")? {
        let 目标 = 到.join(相对.replace('/', std::path::MAIN_SEPARATOR_STR));
        if let Some(父) = 目标.parent() {
            fs::create_dir_all(父).map_err(|e| format!("建目录失败：{e}"))?;
        }
        fs::copy(&全, &目标).map_err(|e| format!("复制「{}」失败：{e}", 全.display()))?;
    }
    Ok(())
}

/// 打一个包（调语言层那份实现，`基础` = 源目录，来源 = 显式文件列表）。
#[allow(clippy::too_many_arguments)]
fn 打一个包(文件们: &[String], 源: &str, 临时: &Path, 名字: &str, 版本: &str,
            入口: &str, 平台值: &str, 带运行时: bool,
            引擎: &str) -> Result<String, JishiError> {
    let 列表 = crate::list_new(
        文件们.iter().map(|p| Val::Str(p.clone().into())).collect(),
    );
    let mut 对 = vec![
        (Val::Str("名字".into()), Val::Str(名字.to_string().into())),
        (Val::Str("版本".into()), Val::Str(版本.to_string().into())),
        (Val::Str("平台".into()), Val::Str(平台值.to_string().into())),
        (Val::Str("入口".into()), Val::Str(入口.to_string().into())),
        (Val::Str("基础".into()), Val::Str(源.to_string().into())),
    ];
    // M177：自带运行时（包内布局 app/ + bin/ 由 `打包.生成` 负责）
    if 带运行时 {
        对.push((Val::Str("运行时".into()), Val::Bool(true)));
        if !引擎.is_empty() {
            对.push((Val::Str("引擎".into()), Val::Str(引擎.to_string().into())));
        }
    }
    let 选项 = crate::dict_new(对);
    crate::stdlib::pack::建包(&列表, &临时.display().to_string(), Some(&选项))
}

// ---------------------------------------------------------------------------
// M198：Inno Setup 向导安装包
// ---------------------------------------------------------------------------
//
// 存根版（`jishi-pack-stub` + 尾部载荷索引）能装、能卸、在「应用和功能」里能看到，
// 但它**没有向导**：不能选目录、不能选要不要桌面快捷方式、没有完成页。
// 那一套交给 Inno Setup —— 而它是**第三方编译器**，所以这里只做四件事：
//
//   1. 找 ISCC（`JISHI_ISCC` → 系统级 → 用户级 → PATH）；
//   2. 解出要装的文件，把 `tools/installer_user.iss` 的占位符换成真值写成 `.iss`；
//   3. 调 ISCC 编出 setup.exe；
//   4. 把那个 setup.exe 当**载荷**塞进存根壳（壳负责翻译老参数，见 packstub）。
//
// 🔴 找不到 ISCC **不是错误**：回落到存根版，但**当场说明**为什么、怎么装 Inno。

/// 载荷里那一条的名字。**与 `packstub` 共用这一份常量**（不是各写一个字符串）。
///
/// 壳靠它认出「这个包装的是另一个安装器」：改一边不改另一边 = 装成一堆散文件。
pub const 子安装器条目: &str = "__installer__.exe";

/// 用户程序安装器的模板。**编进二进制**：装好的 `jishi.exe` 旁边没有仓库的 `tools/`。
const 用户iss模板: &str = include_str!("../../tools/installer_user.iss");

/// 简体中文的消息文件（同上，也编进去再写到临时目录）。
const 内置消息文件: &str = include_str!("../../tools/inno/ChineseSimplified.isl");

/// 名字 / 版本里不能出现的字符（与 Python `packcmd._禁用字符` 同一条）。
///
/// 分两类：`"` `{` `}` `'` 会把 Inno 的语法（字符串、常量、`[Code]` 里的字面量）
/// 搅坏；`< > : | ? * / \` 是 Windows 文件名非法字符。
const 禁用字符: &[char] = &['"', '{', '}', '\'', '<', '>', ':', '|', '?', '*', '/', '\\'];

fn fnv1a64(数据: &[u8], 偏移: u64) -> u64 {
    let mut h = 偏移;
    for b in 数据 {
        h ^= *b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01B3);
    }
    h
}

/// 由**应用名**派生一个稳定的 GUID（Inno 的 `AppId`）。
///
/// 为什么要派生：装两个不同的基石程序时卸载项要各是各的（写死一个 AppId 会
/// 「后装的顶掉前一个」）；为什么不能用随机数：升级时 AppId 变了 = Inno 认为是
/// 另一个软件，控制面板里冒出两条、旧文件留在盘上。
///
/// ⚠️ 与 Python `packcmd.应用id` **必须逐字一致**（同一个名字 → 同一个 GUID）。
fn 应用id(名字: &str) -> String {
    let a = fnv1a64(名字.as_bytes(), 0xCBF2_9CE4_8422_2325);
    let b = fnv1a64(名字.as_bytes(), 0x9E37_79B9_7F4A_7C15);
    let g1 = (a >> 32) as u32;
    let g2 = (a >> 16) as u16;
    let g3 = ((a & 0xFFFF) as u16 & 0x0FFF) | 0x4000;
    let g4 = ((b >> 48) as u16 & 0x3FFF) | 0x8000;
    let g5 = b & 0xFFFF_FFFF_FFFF;
    format!("{g1:08X}-{g2:04X}-{g3:04X}-{g4:04X}-{g5:012X}")
}

/// 名字 / 版本能用吗？不能用返回一句人话。
fn 查名字(名字: &str, 版本: &str) -> Option<String> {
    if 名字.is_empty() {
        return Some("「名字」不能是空的".to_string());
    }
    for (值, 叫) in [(名字, "名字"), (版本, "版本")] {
        for ch in 值.chars() {
            if 禁用字符.contains(&ch) || (ch as u32) < 0x20 {
                return Some(format!(
                    "「{叫}」里不能有字符 {ch:?}（会把安装程序的脚本搅坏，\
                     或造出 Windows 不认的文件名）：{值:?}"
                ));
            }
        }
    }
    None
}

/// 找 Inno Setup 的编译器。`Ok(None)` = 这台机器没有（调用方降级）。
///
/// `JISHI_ISCC` 指了却没有那个文件 ⇒ `Err`：用户明确说了要用哪个编译器，
/// 静默忽略他的意图比报错更糟。
fn 找iscc() -> Result<Option<PathBuf>, String> {
    if let Ok(v) = std::env::var("JISHI_ISCC") {
        let v = v.trim().to_string();
        if !v.is_empty() {
            let p = PathBuf::from(&v);
            return if p.is_file() {
                Ok(Some(p))
            } else {
                Err(format!("JISHI_ISCC 指到「{v}」，但那里没有 ISCC.exe"))
            };
        }
    }
    if !cfg!(windows) {
        return Ok(None);
    }
    let mut 候选: Vec<PathBuf> = Vec::new();
    for 变量 in ["ProgramFiles(x86)", "ProgramFiles"] {
        if let Ok(根) = std::env::var(变量) {
            for 版 in ["Inno Setup 7", "Inno Setup 6"] {
                候选.push(Path::new(&根).join(版).join("ISCC.exe"));
            }
        }
    }
    if let Ok(本地) = std::env::var("LOCALAPPDATA") {
        for 版 in ["Inno Setup 7", "Inno Setup 6"] {
            候选.push(
                Path::new(&本地).join("Programs").join(版).join("ISCC.exe"),
            );
        }
    }
    for p in &候选 {
        if p.is_file() {
            return Ok(Some(p.clone()));
        }
    }
    if let Some(路径) = std::env::var_os("PATH") {
        for 目录 in std::env::split_paths(&路径) {
            for 名 in ["ISCC.exe", "iscc.exe", "ISCC", "iscc"] {
                let p = 目录.join(名);
                if p.is_file() {
                    return Ok(Some(p));
                }
            }
        }
    }
    Ok(None)
}

/// **一遍过**替换 `@键@`（与 Python `_替换` 同一条规则）。
///
/// 不能逐个 `replace`：值里如果正好含 `@名字@`，第二轮会把刚填进去的内容再搅一次。
/// 只有「同一行、短」的才算占位符 —— 注释里出现一个孤零零的 at 符号不该炸掉模板。
fn 替换(模板: &str, 值: &[(&str, String)]) -> Result<String, String> {
    let 字: Vec<char> = 模板.chars().collect();
    let mut 出 = String::with_capacity(模板.len() + 256);
    let mut i = 0usize;
    while i < 字.len() {
        if 字[i] == '@' {
            let mut j = i + 1;
            while j < 字.len() && 字[j] != '@' && 字[j] != '\n' {
                j += 1;
            }
            if j < 字.len() && 字[j] == '@' && j > i + 1 && j - i <= 13 {
                let 键: String = 字[i + 1..j].iter().collect();
                match 值.iter().find(|(k, _)| *k == 键) {
                    Some((_, v)) => {
                        出.push_str(v);
                        i = j + 1;
                        continue;
                    }
                    None => {
                        return Err(format!(
                            "模板里的占位符「@{键}@」代码里没有给值 —— \
                             `tools/installer_user.iss` 与 packcmd 不同步了"
                        ))
                    }
                }
            }
        }
        出.push(字[i]);
        i += 1;
    }
    Ok(出)
}

/// 包里带的 `.ico`（优先 `share/基石.ico`）。空 = 包里有图标就用，没有就不设。
fn 找图标(源目录: &Path) -> Option<PathBuf> {
    let 优先 = 源目录.join("share").join("基石.ico");
    if 优先.is_file() {
        return Some(优先);
    }
    for 子 in ["", "share", "app"] {
        let 目录 = if 子.is_empty() { 源目录.to_path_buf() } else { 源目录.join(子) };
        let Ok(读) = fs::read_dir(&目录) else { continue };
        let mut 名们: Vec<PathBuf> = 读
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| {
                p.is_file()
                    && p.extension()
                        .map(|x| x.to_string_lossy().to_lowercase() == "ico")
                        .unwrap_or(false)
            })
            .collect();
        名们.sort();
        if let Some(p) = 名们.into_iter().next() {
            return Some(p);
        }
    }
    None
}

/// 包里这几个名字的第一个会被当**许可文件**（有就出许可页，没有就跳过那一页）。
///
/// 只认这几个固定名字：`README.md` 之类不是许可，拿来当许可页反而要用户
/// 在「我同意」上点一下才能装 —— 那是骚扰。与 Python `_许可名们` 同一条。
const 许可名们: [&str; 5] = ["LICENSE", "LICENSE.txt", "LICENSE.md", "许可.txt", "许可协议.txt"];

fn 找许可(源目录: &Path) -> Option<PathBuf> {
    许可名们
        .iter()
        .map(|名| 源目录.join(名))
        .find(|p| p.is_file())
}

/// 把模板换成一份能交给 ISCC 的 `.iss` 文本（不含 BOM，写盘时再加）。
fn 用户iss(
    名字: &str,
    版本: &str,
    入口: &str,
    源目录: &Path,
    出目录: &str,
    消息文件: &str,
) -> Result<String, String> {
    let 入口盘 = 入口.replace('/', "\\");
    let 有引擎 = 源目录.join("bin").join("jishi.exe").is_file()
        || 源目录.join("bin").join("jishi").is_file();

    let (快捷目标, 快捷参数行, 运行段, 卸载图标) = if 有引擎 && !入口盘.is_empty() {
        let 参数 = format!("Parameters: \"{{app}}\\app\\{入口盘}\"; ");
        (
            "\"{app}\\bin\\jishi.exe\"".to_string(),
            参数.clone(),
            format!(
                "[Run]\nFilename: \"{{app}}\\bin\\jishi.exe\"; {参数}\
                 Description: \"立即运行 {名字}\"; \
                 Flags: postinstall nowait skipifsilent; Check: ShouldRun\n"
            ),
            "{app}\\bin\\jishi.exe".to_string(),
        )
    } else if !入口盘.is_empty() {
        (format!("\"{{app}}\\{入口盘}\""), String::new(), String::new(),
         "{uninstallexe}".to_string())
    } else {
        // 没标入口：快捷方式指向安装目录（双击打开文件夹），比指个坏路径强
        ("\"{app}\"".to_string(), String::new(), String::new(),
         "{uninstallexe}".to_string())
    };

    let 图标 = 找图标(源目录);
    let 图标文 = 图标
        .as_ref()
        .map(|p| p.display().to_string())
        .unwrap_or_default();
    let 值: Vec<(&str, String)> = vec![
        ("APPID", format!("{{{{{}}}", 应用id(名字))),
        ("名字", 名字.to_string()),
        ("版本", 版本.to_string()),
        ("默认标题", 名字.to_string()),
        ("出目录", 出目录.to_string()),
        ("基名", format!("{名字}-{版本}-windows-x64-setup")),
        ("源目录", 源目录.display().to_string()),
        ("快捷目标", 快捷目标),
        ("快捷参数行", 快捷参数行),
        ("图标参数", if 图标.is_some() { format!("IconFilename: \"{图标文}\"; ") } else { String::new() }),
        ("卸载图标", 卸载图标),
        ("图标行", if 图标.is_some() { format!("SetupIconFile={图标文}") } else { String::new() }),
        ("消息文件", 消息文件.to_string()),
        (
            "许可行",
            找许可(源目录)
                .map(|p| format!("LicenseFile={}", p.display()))
                .unwrap_or_default(),
        ),
        ("运行段", 运行段),
    ];
    替换(用户iss模板, &值)
}

/// UTF-8 **带 BOM**、CRLF —— Inno 6 靠 BOM 认 UTF-8（没 BOM 时中文按 ANSI 解，
/// 而且**编译不报错**，装完才发现「应用和功能」里是一串问号）。
///
/// 🔴 先把已有的 CRLF 归一成 LF 再统一转换：模板文件被编辑器存成 CRLF 时，
/// 直接 `\n → \r\n` 会得到 `\r\r\n` —— 编译不报错，但**每一行都多一个空行**，
/// 而且 `.iss` 与 Python 那边逐字不一致（判据当场红，却看不出为什么）。
fn 带bom(文本: &str) -> Vec<u8> {
    let mut 出 = vec![0xEF, 0xBB, 0xBF];
    let 规整 = 文本.replace("\r\n", "\n").replace('\r', "\n");
    出.extend_from_slice(规整.replace('\n', "\r\n").as_bytes());
    出
}

/// 解出要装的文件 + 写出一份 `.iss`，返回 iss 的路径。
///
/// 不管有没有 ISCC 都做这一步：`--保留iss` 在任何机器上都有东西可留（排查
/// 「装出来不对」时那是最有用的线索），判据也能在没有 Inno 的机器上比对
/// 「两套命令行生成的脚本是否逐字相同」。
fn 备iss(
    名字: &str,
    版本: &str,
    入口: &str,
    出: &str,
    临时: &Path,
    zip: &[u8],
) -> Result<PathBuf, String> {
    let 发行 = 临时.join("发行");
    fs::create_dir_all(&发行).map_err(|e| format!("建目录失败：{e}"))?;
    解到(zip, &发行)?;
    let 语言 = 临时.join("ChineseSimplified.isl");
    fs::write(&语言, 内置消息文件.as_bytes())
        .map_err(|e| format!("写语言文件失败：{e}"))?;
    let 文本 = 用户iss(名字, 版本, 入口, &发行, 出, &语言.display().to_string())?;
    let iss = 临时.join("安装.iss");
    fs::write(&iss, 带bom(&文本)).map_err(|e| format!("写 iss 失败：{e}"))?;
    Ok(iss)
}

/// 调 ISCC 编出来，再把那个 setup.exe 打成「只装一个文件」的 zip（壳的载荷）。
#[allow(clippy::too_many_arguments)]
fn 编译iss(
    iss: &Path,
    名字: &str,
    版本: &str,
    出: &str,
    临时: &Path,
    iscc: &Path,
) -> Result<String, String> {
    let 结果 = std::process::Command::new(iscc)
        .arg("/Q")
        .arg(iss)
        .current_dir(临时)
        .output()
        .map_err(|e| format!("起不来 ISCC（{}）：{e}", iscc.display()))?;
    let 文 = format!(
        "{}{}",
        String::from_utf8_lossy(&结果.stdout),
        String::from_utf8_lossy(&结果.stderr)
    );
    let 编出 = Path::new(出).join(format!("{名字}-{版本}-windows-x64-setup.exe"));
    if !结果.status.success() || !编出.is_file() {
        // 编译失败**不**悄悄退回存根：那是我们自己的脚本坏了，换一种产物只会藏问题
        let 尾: String = 文.chars().rev().take(2000).collect::<Vec<_>>()
            .into_iter().rev().collect();
        return Err(format!(
            "Inno Setup 编译失败（退出码 {:?}）\n{尾}\n\
             　　 不想用向导版就加 `--不用inno`（出存根版）。",
            结果.status.code()
        ));
    }
    let 子 = 临时.join("子包");
    fs::create_dir_all(&子).map_err(|e| format!("建目录失败：{e}"))?;
    let 载荷 = 子.join(子安装器条目);
    fs::rename(&编出, &载荷).or_else(|_| {
        fs::copy(&编出, &载荷)
            .map(|_| ())
            .and_then(|_| fs::remove_file(&编出))
    })
    .map_err(|e| format!("搬安装器失败：{e}"))?;
    打一个包(
        &[载荷.display().to_string()],
        &子.display().to_string(),
        临时,
        名字,
        版本,
        "",
        平台("安装包"),
        false,
        "",
    )
    .map_err(|e| e.message)
}

fn 找存根() -> String {
    let 名 = if cfg!(windows) { "jishi-pack-stub.exe" } else { "jishi-pack-stub" };
    let mut 候选: Vec<PathBuf> = Vec::new();
    if let Ok(自己) = std::env::current_exe() {
        if let Some(父) = 自己.parent() {
            候选.push(父.join(名));
        }
    }
    // 开发时的位置（仓库里）
    if let Ok(自己) = std::env::current_exe() {
        for 上 in 自己.ancestors().skip(1).take(5) {
            候选.push(上.join("rust").join("target").join("release").join(名));
        }
    }
    for p in 候选 {
        if p.is_file() {
            return p.display().to_string();
        }
    }
    String::new()
}

fn 用法() -> String {
    "用法：jishi 打包 [目录] [--出 目录] [--名字 X] [--版本 X] [--入口 X]\n\
     \x20               [--存根 路径] [--目标 便携,安装包,应用,压缩包]\n\
     \x20               [--安装器 自研|inno] [--保留iss] [--安静]\n"
        .to_string()
}

// ---------------------------------------------------------------------------

pub fn run_pack(argv: &[String]) -> ExitCode {
    let mut 目录 = ".".to_string();
    let (mut 出, mut 名字, mut 版本, mut 入口, mut 存根, mut 目标们) =
        (String::new(), String::new(), String::new(), String::new(), String::new(), String::new());
    let mut 安静 = false;
    // M177：自带运行时
    let mut 带运行时 = false;
    let mut 引擎 = String::new();
    // M198/M200：安装器用哪一套。**默认自研**（存根 + 自带向导）——
    // Inno 从「默认」退成「可选」：它要多装一个第三方编译器，
    // 而我们自己的向导 M200 起能选目录、能加 PATH、能升级。
    let mut 安装器 = "自研".to_string();
    let mut 保留iss = false;
    let mut i = 0;
    while i < argv.len() {
        let a = argv[i].as_str();
        let 取值 = |i: &mut usize| -> String {
            *i += 1;
            argv.get(*i).cloned().unwrap_or_default()
        };
        match a {
            "--出" | "--out" => 出 = 取值(&mut i),
            "--名字" | "--name" => 名字 = 取值(&mut i),
            "--版本" | "--version" => 版本 = 取值(&mut i),
            "--入口" | "--entry" => 入口 = 取值(&mut i),
            "--存根" | "--stub" => 存根 = 取值(&mut i),
            "--目标" | "--targets" => 目标们 = 取值(&mut i),
            "--安静" | "-q" | "--quiet" => 安静 = true,
            // 老开关留着（M198 的脚本里可能有），等价于新的显式选择
            "--不用inno" | "--no-inno" => 安装器 = "自研".to_string(),
            "--安装器" | "--installer" => 安装器 = 取值(&mut i),
            "--保留iss" | "--keep-iss" => 保留iss = true,
            // M177：把引擎打进包里，装到没装过基石的机器上双击就能用
            "--自带运行时" | "--with-runtime" => 带运行时 = true,
            "--引擎" | "--engine" => 引擎 = 取值(&mut i),
            "-h" | "--help" => {
                print!("{}", 用法());
                return ExitCode::SUCCESS;
            }
            _ if a.starts_with('-') => {
                eprintln!("错误：不认识这个参数「{a}」\n{}", 用法());
                return ExitCode::from(2);
            }
            _ => 目录 = a.to_string(),
        }
        i += 1;
    }

    let 源 = match fs::canonicalize(&目录) {
        Ok(p) => {
            let s = p.display().to_string();
            match s.strip_prefix(r"\\?\") {
                Some(rest) => rest.to_string(),
                None => s,
            }
        }
        Err(_) => {
            eprintln!("错误：「{目录}」不是目录");
            return ExitCode::from(2);
        }
    };
    if !Path::new(&源).is_dir() {
        eprintln!("错误：「{目录}」不是目录");
        return ExitCode::from(2);
    }
    let 源 = 源.trim_end_matches(['/', '\\']).to_string();

    // 配置：`打包.json`（命令行优先）
    if let Ok(文本) = fs::read_to_string(Path::new(&源).join("打包.json")) {
        if let Ok(j) = crate::json::parse(&文本) {
            let 取 = |k: &str| j.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
            if 名字.is_empty() {
                名字 = 取("名字");
            }
            if 版本.is_empty() {
                版本 = 取("版本");
            }
            if 入口.is_empty() {
                入口 = 取("入口");
            }
            // M177：`打包.json` 里也能开自带运行时（命令行优先 —— 命令行只能"打开"，
            // 没有"关掉"的开关，因为写配置的人本来就知道自己要什么）
            if !带运行时 {
                带运行时 = j.get("运行时").and_then(|v| v.as_bool()).unwrap_or(false);
            }
            if 引擎.is_empty() {
                引擎 = 取("引擎");
            }
        }
    }
    if 名字.is_empty() {
        名字 = 文件名(&源);
    }
    if 版本.is_empty() {
        版本 = "0.0.0".to_string();
    }
    if 出.is_empty() {
        出 = format!("{源}/_出");
    }
    if 存根.is_empty() {
        存根 = 找存根();
    } else if !Path::new(&存根).is_file() {
        eprintln!("错误：找不到自解压存根「{存根}」");
        return ExitCode::from(2);
    }

    let mut 要哪些: Vec<String> = if 目标们.is_empty() {
        全部类型.iter().map(|s| s.to_string()).collect()
    } else {
        目标们.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect()
    };
    for t in &要哪些 {
        if !全部类型.contains(&t.as_str()) {
            eprintln!("错误：不认识的产物类型「{t}」（认：{}）", 全部类型.join("、"));
            return ExitCode::from(2);
        }
    }
    if 要哪些.iter().any(|t| t == "安装包") && 存根.is_empty() {
        eprintln!("错误：出 Windows 安装包要 `jishi-pack-stub`，没找到。\
                   用 --存根 指给它，或者 `--目标` 里去掉「安装包」。");
        return ExitCode::from(2);
    }
    // M198：向导版的载荷要套在存根壳里（壳翻译老参数），所以存根仍然是必需的；
    // ISCC 决定的是「载荷是 Inno 向导还是散文件」
    let iscc: Option<PathBuf> = match 安装器.as_str() {
        // 自研：**不碰 Inno**（机器上装没装 ISCC 都不影响产物）
        "自研" => None,
        "inno" => match 找iscc() {
            Ok(v) => v,
            Err(e) => {
                // 🔴 显式要了 Inno 却没有 ISCC —— **报错，不偷偷换产物**。
                //    静默降级的代价是「作者以为拿到的是向导版，装出来却不一样」，
                //    那种事查起来要一整天（M198 就为它写过一大段说明）。
                eprintln!("错误：你指定了 `--安装器 inno`，但{e}");
                eprintln!("　　 装一个：winget install -e --id JRSoftware.InnoSetup");
                eprintln!("　　 或者去掉这个开关，用默认的**自研安装器**（自带向导，不需要 Inno）。");
                return ExitCode::from(2);
            }
        },
        other => {
            eprintln!("错误：不认识的安装器「{other}」（只有「自研」和「inno」两种）");
            return ExitCode::from(2);
        }
    };
    let mut 警告: Vec<String> = Vec::new();
    if 要哪些.iter().any(|t| t == "应用") && 入口.is_empty() {
        要哪些.retain(|t| t != "应用");
        警告.push("没有「入口」，出不了 .app（macOS 靠 Info.plist 里的入口启动）".to_string());
    }

    let 文件们 = 要进包(Path::new(&源), Path::new(&出));
    if 文件们.is_empty() {
        eprintln!("错误：「{源}」里没有文件");
        return ExitCode::from(2);
    }
    // 🔴 入口必须在**要打包的目录里**（M188 加）：
    //    入口写错（或者忘了把那个 .jsh 放进目录）时，包照样能出，
    //    装完双击的后果是「找不到文件」—— 用户看到的是「装了个坏东西」。
    if !入口.is_empty() && !Path::new(&源).join(&入口).is_file() {
        eprintln!(
            "错误：入口「{入口}」不在「{源}」里 —— 装上之后双击会「找不到文件」。\n　　 \
             要么把 {入口} 放进这个目录，要么把 --入口 写成目录里真有的那个文件。");
        return ExitCode::from(2);
    }
    if let Err(e) = fs::create_dir_all(&出) {
        eprintln!("错误：建输出目录「{出}」失败：{e}");
        return ExitCode::FAILURE;
    }
    // M198：后面要给 ISCC 传 `OutputDir`，**必须绝对路径** —— 相对路径它会
    // 相对 `.iss` 所在目录（临时目录）去算，编完在输出目录里找不到产物，
    // 而 ISCC 的退出码还是 0（「成功但没东西」）。
    let 出 = match fs::canonicalize(&出) {
        Ok(p) => {
            let s = p.display().to_string();
            s.strip_prefix(r"\\?\").unwrap_or(&s).to_string()
        }
        Err(_) => 出.clone(),
    };

    let 临时 = std::env::temp_dir().join(format!("jishi-pack-{}", std::process::id()));
    let _ = fs::remove_dir_all(&临时);
    if let Err(e) = fs::create_dir_all(&临时) {
        eprintln!("错误：建临时目录失败：{e}");
        return ExitCode::FAILURE;
    }

    let mut 成品: Vec<(String, String)> = Vec::new();
    let 结果 = (|| -> Result<(), String> {
        if 要哪些.iter().any(|t| t == "便携") {
            let 包 = 打一个包(&文件们, &源, &临时, &名字, &版本, &入口, 平台("便携"), 带运行时, &引擎)
                .map_err(|e| e.message)?;
            let 目标 = format!("{出}/{}", 文件名(&包));
            fs::copy(&包, &目标).map_err(|e| format!("复制便携包失败：{e}"))?;
            成品.push(("便携".to_string(), 目标));
        }
        if 要哪些.iter().any(|t| t == "安装包") {
            let 包 = 打一个包(&文件们, &源, &临时, &名字, &版本, &入口, 平台("安装包"), 带运行时, &引擎)
                .map_err(|e| e.message)?;
            let 目标 = format!("{出}/{名字}-{版本}-windows-x64-setup.exe");
            let 原包字节 = fs::read(&包).map_err(|e| format!("读包失败：{e}"))?;
            // M198：`.iss` **不管有没有 ISCC 都生成**（`--保留iss` 与判据都要它）。
            // 有 ISCC 时再编成子安装器、拿那个 zip 当载荷。
            let iss = 备iss(&名字, &版本, &入口, &出, &临时, &原包字节)?;
            let 包字节 = match &iscc {
                Some(路径) => {
                    let 子 = 编译iss(&iss, &名字, &版本, &出, &临时, 路径)?;
                    fs::read(&子).map_err(|e| format!("读子包失败：{e}"))?
                }
                None => 原包字节,
            };
            let 存根字节 = fs::read(&存根).map_err(|e| format!("读存根失败：{e}"))?;
            // 尾部索引：`[存根][包][偏移 u64][长度 u64]["JISHIPK1"]`（与 `打包.安装包` 同格式）
            let mut 全 = Vec::with_capacity(存根字节.len() + 包字节.len() + 24);
            全.extend_from_slice(&存根字节);
            全.extend_from_slice(&包字节);
            全.extend_from_slice(&(存根字节.len() as u64).to_le_bytes());
            全.extend_from_slice(&(包字节.len() as u64).to_le_bytes());
            全.extend_from_slice(b"JISHIPK1");
            fs::write(&目标, &全).map_err(|e| format!("写安装包失败：{e}"))?;
            // M200：**自研是默认**，这里不再是「降级」，不用警告 ——
            // 只有显式要 `--安装器 inno` 却没 ISCC 才报错（那一段在上面）。
            成品.push(("安装包".to_string(), 目标));
            if 保留iss {
                let iss出 = format!("{出}/{名字}-{版本}-windows-x64-setup.iss");
                fs::copy(&iss, &iss出).map_err(|e| format!("复制 iss 失败：{e}"))?;
                成品.push(("安装脚本".to_string(), iss出));
            }
        }
        if 要哪些.iter().any(|t| t == "应用") {
            let 包 = 打一个包(&文件们, &源, &临时, &名字, &版本, &入口, 平台("应用"), 带运行时, &引擎)
                .map_err(|e| e.message)?;
            let 字节 = fs::read(&包).map_err(|e| format!("读包失败：{e}"))?;
            let 束 = 临时.join(format!("{名字}.app"));
            let 内容 = 束.join("Contents");
            解到(&字节, &内容.join("MacOS"))?;
            fs::write(内容.join("Info.plist"), 应用清单(&名字, &版本, &入口))
                .map_err(|e| format!("写 Info.plist 失败：{e}"))?;
            fs::write(内容.join("PkgInfo"), "APPL????")
                .map_err(|e| format!("写 PkgInfo 失败：{e}"))?;
            // `.app` 本身就是产物（macOS 上它是个目录，双击就开）⇒ 落在 `--出`
            let 成品束 = Path::new(&出).join(format!("{名字}.app"));
            if 成品束.is_dir() {
                let _ = fs::remove_dir_all(&成品束);
            }
            拷树(&束, &成品束)?;
            let tar = format!("{出}/{名字}-{版本}-macos-arm64.tar.gz");
            写tar_gz(&成品束, &format!("{名字}.app"), Path::new(&tar))?;
            成品.push(("应用".to_string(), 成品束.display().to_string()));
            成品.push(("应用".to_string(), tar));
        }
        if 要哪些.iter().any(|t| t == "压缩包") {
            let 包 = 打一个包(&文件们, &源, &临时, &名字, &版本, &入口, 平台("压缩包"), 带运行时, &引擎)
                .map_err(|e| e.message)?;
            let 字节 = fs::read(&包).map_err(|e| format!("读包失败：{e}"))?;
            let 顶层 = format!("{名字}-{版本}-linux-x64");
            let 暂存 = 临时.join(&顶层);
            解到(&字节, &暂存)?;
            fs::write(暂存.join("install.sh"), 安装脚本(&名字, &入口))
                .map_err(|e| format!("写 install.sh 失败：{e}"))?;
            let tar = format!("{出}/{顶层}.tar.gz");
            写tar_gz(&暂存, &顶层, Path::new(&tar))?;
            成品.push(("压缩包".to_string(), tar));
        }
        Ok(())
    })();

    let _ = fs::remove_dir_all(&临时);
    if let Err(e) = 结果 {
        eprintln!("错误：{e}");
        return ExitCode::FAILURE;
    }

    if !安静 {
        for w in &警告 {
            eprintln!("⚠️  {w}");
        }
        println!("打包「{名字} {版本}」→ {出}");
        for (类, 路径) in &成品 {
            println!("  {类}：{路径}");
        }
    } else {
        for (_, 路径) in &成品 {
            println!("{路径}");
        }
    }
    ExitCode::SUCCESS
}
