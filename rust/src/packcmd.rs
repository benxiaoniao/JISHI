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
     \x20               [--存根 路径] [--目标 便携,安装包,应用,压缩包] [--安静]\n"
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
    if let Err(e) = fs::create_dir_all(&出) {
        eprintln!("错误：建输出目录「{出}」失败：{e}");
        return ExitCode::FAILURE;
    }

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
            let 包字节 = fs::read(&包).map_err(|e| format!("读包失败：{e}"))?;
            let 存根字节 = fs::read(&存根).map_err(|e| format!("读存根失败：{e}"))?;
            // 尾部索引：`[存根][包][偏移 u64][长度 u64]["JISHIPK1"]`（与 `打包.安装包` 同格式）
            let mut 全 = Vec::with_capacity(存根字节.len() + 包字节.len() + 24);
            全.extend_from_slice(&存根字节);
            全.extend_from_slice(&包字节);
            全.extend_from_slice(&(存根字节.len() as u64).to_le_bytes());
            全.extend_from_slice(&(包字节.len() as u64).to_le_bytes());
            全.extend_from_slice(b"JISHIPK1");
            fs::write(&目标, &全).map_err(|e| format!("写安装包失败：{e}"))?;
            成品.push(("安装包".to_string(), 目标));
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
