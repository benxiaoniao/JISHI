//! `jishi-pack-stub` —— **自解压安装器**（M171）。
//!
//! ## 它为什么必须是一个**原生程序**，而不是基石写的
//!
//! 用户下载 `我的程序-1.0.0-setup.exe` 时**机器上还没有基石**。安装器得能
//! 独立跑起来，所以它只能是静态编出来的原生程序 —— 这跟 `jishi.exe` 本身
//! 是同一类东西（**引擎级**，不是语言级）。
//!
//! 模块那一层（`打包.清单/生成/校验/安装包`）负责「事实与规则」：
//! 清单长什么样、包怎么拼、拼完怎么验。**这个程序只负责动手**。
//!
//! ## 包长什么样
//!
//! ```text
//! [ 本程序 ][ zip 包 ][ zip 偏移 u64 ][ zip 长度 u64 ][ JISHIPK1 ]
//! ```
//!
//! 尾部那 24 字节是**索引**，不是「在文件里搜 `PK\x03\x04`」—— 用户程序里
//! 随便一段数据都可能是 zip 魔数，倒着搜会搜错。
//!
//! ## 用法
//!
//! ```text
//! setup.exe                    双击 → 图形向导（选目录 / 桌面 / PATH）
//! setup.exe --目标 <目录>       装到指定目录（CI 用这个，不碰用户目录）
//! setup.exe /D=<目录>          同上（Inno 的老写法，顺手兼容）
//! setup.exe /S                 静默：不打印进度（只打印错误）
//! setup.exe --加PATH           顺手把安装目录加进用户 PATH（默认**不加**）
//! setup.exe --桌面             顺手在桌面放一个快捷方式（向导里也能勾）
//! setup.exe --查看             只打印清单与「这次会做什么」，不动盘
//! setup.exe --校验             只逐份核对 zip 里的内容，不动盘
//! setup.exe --解析快捷方式 路径  把一个 .lnk 读回来（查「点了没反应」用）
//! setup.exe --卸载             卸载（从自己的目录读清单）
//! ```
//!
//! ## 🔴 它是 **GUI 子系统**程序（M200 修）
//!
//! 双击时**不许弹黑窗**：以前它是控制台程序，双击先闪一个黑框 —— 用户第一眼
//! 看到的就是「这玩意儿好粗糙」。现在编成 `windows_subsystem = "windows"`，
//! 从命令行用时**接上父进程的控制台**（`AttachConsole`），所以
//! `--查看` / `/S` 那些输出照旧；没有控制台可接就把标准输出接到 `NUL`，
//! 让 `println!` 变成无操作而不是「写失败」。
//!
//! ## 这一版**没做**的（如实记）
//!
//! - **开始菜单快捷方式**：`.lnk` 是二进制格式，写一个「长得对」的容易，
//!   写一个「点了真能打开」的要 `IShellLink` COM。留给 M172（那时才有
//!   `jishi 打包` 的一整套产物要一起验）。
//! - **图形化向导**：这一版只有控制台进度。向导要等 `窗口` 能力（M173）落定。
//!
//! 上面两条是 M171 的历史记录，M172/M200 都补上了。

// 🔴 必须在**任何 item 之前**（inner attribute）。
//    只在「有向导」的构建里这么编：`--no-default-features` 那一档没有窗口，
//    双击就该是控制台安装 —— 那时改 GUI 子系统反而会把唯一的输出也弄没。
#![cfg_attr(all(windows, feature = "native-window"), windows_subsystem = "windows")]

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

use jishi_ffi::json::Json;

/// M200：自研安装向导（图形界面）。只在开了 `native-window` 特性时编进来 ——
/// 关掉特性（小体积构建）就退化成控制台安装，功能不缺，只是没有窗口。
#[cfg(feature = "native-window")]
#[path = "packstub/向导.rs"]  // 🔴 中文模块名必须显式给路径（编译器不按中文名找文件）
mod 向导;

/// 尾部索引的魔数。与 Python `打包.安装包魔数` / Node 同一串。
const 魔数: &[u8; 8] = b"JISHIPK1";
/// 尾部索引长度：偏移 u64 + 长度 u64 + 魔数 8。
const 尾部长: usize = 24;
/// 包里那份清单的名字。
const 清单名: &str = "清单.json";
/// M177：自带运行时的包根上那份「怎么启动」的说明。
///
/// **它不是清单的一部分**：`清单.json` 只说「包里有哪些文件」，
/// 「双击时该执行什么」是另一件事 —— 分成两份，清单的格式就不用动
/// （那是三个宿主都在守的契约）。
const 运行名: &str = "运行时.json";

/// M198：载荷里装着一个**别的安装器**（Inno Setup 编出来的向导）时的条目名。
///
/// ## 为什么还要这层壳
///
/// 以为「让 Inno 出安装包」就完事，实测三件事都做不到（2026-11-13 本机验过）：
///
/// 1. **静默没法从脚本里打开** —— `WizardSilent` 是只读的；`/S` 这种老开关
///    Inno 根本不认，`ShouldSkipPage` 跳完所有页也不装（`rc=0`、一个文件都不落）。
/// 2. **`--目标` 只能当「默认值」** —— 老脚本拿它当「装到这儿、别问我」。
/// 3. **自己起不来自己** —— 想「转成 `/VERYSILENT` 再拉一遍」，`Exec` 直接
///    返回错误 5；换 `cmd /c` 绕过也不成：Inno 用互斥锁挡住第二个实例，
///    父进程又在等子进程，两边死等。
///
/// 所以老参数只能在这层壳里翻译。顺带把 `--查看` / `--校验` / `--解析快捷方式`
/// 这三个查错动作也留住了 —— 纯 Inno 包里没有「载荷清单」这个概念可查。
const 子安装器名: &str = "__installer__.exe";
/// 卸载时用的程序名（安装时写进安装目录）。
const 卸载名: &str = "uninstall.exe";

// ---------------------------------------------------------------------------
// 尾部索引
// ---------------------------------------------------------------------------

/// 从自身末尾读出 zip 的 `(偏移, 长度)`。不是安装包就 `None`。
fn 找载荷(自身: &[u8]) -> Option<(usize, usize)> {
    if 自身.len() < 尾部长 {
        return None;
    }
    let t = &自身[自身.len() - 尾部长..];
    if &t[16..24] != 魔数 {
        return None;
    }
    let off = u64::from_le_bytes(t[0..8].try_into().ok()?) as usize;
    let len = u64::from_le_bytes(t[8..16].try_into().ok()?) as usize;
    if off < 尾部长 || off.saturating_add(len) > 自身.len() {
        return None;
    }
    Some((off, len))
}

// ---------------------------------------------------------------------------
// 清单
// ---------------------------------------------------------------------------

struct 包 {
    名字: String,
    版本: String,
    平台: String,
    入口: String,
    文件们: Vec<(String, i64, String)>, // (路径, 大小, sha256)
    条目们: Vec<(String, Vec<u8>)>,
    /// M177：引擎在包内的路径（例如 `bin/jishi.exe`）。空 = 这个包不自带运行时
    /// （老包，或者用户只打了自己的文件）。
    引擎: String,
}

fn 取文(j: &Json, k: &str) -> String {
    j.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

fn 读包(载荷: &[u8]) -> Result<包, String> {
    let 条目们 = jishi_ffi::zip_read_all(载荷).map_err(|e| format!("包里读不出来：{e}"))?;
    let 原文 = 条目们
        .iter()
        .find(|(n, _)| n == 清单名)
        .map(|(_, d)| d.clone())
        .ok_or_else(|| {
            format!("包里没有「{清单名}」—— 这不是「打包.生成」造的包")
        })?;
    let 文本 = String::from_utf8(原文).map_err(|e| format!("「{清单名}」不是 UTF-8：{e}"))?;
    let j = jishi_ffi::json::parse(&文本).map_err(|e| format!("「{清单名}」解析失败：{e}"))?;
    let 文件们 = j
        .get("文件们")
        .and_then(|v| v.as_array())
        .ok_or_else(|| format!("「{清单名}」格式不对：缺「文件们」"))?
        .iter()
        .filter_map(|f| {
            Some((
                f.get("路径")?.as_str()?.to_string(),
                f.get("大小")?.as_i128()? as i64,
                f.get("sha256")?.as_str()?.to_string(),
            ))
        })
        .collect();
    // M177：有 `运行时.json` 就说明这个包自带引擎 ⇒ 快捷方式要指向引擎、
    // 并把入口当**参数**传进去（不是指向 .jsh —— 那玩意儿没有文件关联）。
    // 读不动就当没有（老包照样装，只是双击开不了）。
    let 引擎 = 条目们
        .iter()
        .find(|(n, _)| n == 运行名)
        .and_then(|(_, d)| String::from_utf8(d.clone()).ok())
        .and_then(|t| jishi_ffi::json::parse(&t).ok())
        .map(|r| 取文(&r, "引擎"))
        .unwrap_or_default();
    Ok(包 {
        名字: 取文(&j, "名字"),
        版本: 取文(&j, "版本"),
        平台: 取文(&j, "平台"),
        入口: 取文(&j, "入口"),
        文件们,
        条目们,
        引擎,
    })
}

impl 包 {
    /// 按清单逐份核对 zip 里的内容。**先把话说清楚，再动盘。**
    fn 核对(&self) -> Result<(), String> {
        let mut 问题们: Vec<String> = Vec::new();
        for (路径, 大小, 摘要) in &self.文件们 {
            match self.条目们.iter().find(|(n, _)| n == 路径) {
                None => 问题们.push(format!("缺文件：{路径}")),
                Some((_, 数据)) => {
                    if 数据.len() as i64 != *大小 {
                        问题们.push(format!(
                            "大小不对：{路径}（清单 {大小}，实际 {}）",
                            数据.len()
                        ));
                    } else if jishi_ffi::sha256_hex(数据) != *摘要 {
                        问题们.push(format!("内容不对：{路径}（sha256 与清单不符）"));
                    }
                }
            }
        }
        if 问题们.is_empty() {
            Ok(())
        } else {
            Err(format!(
                "这个包是坏的，什么都没装：\n  {}",
                问题们.join("\n  ")
            ))
        }
    }
}

// ---------------------------------------------------------------------------
// 注册表 / PATH（只有 Windows 有）
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod reg {
    use std::ffi::c_void;

    pub const HKEY_CURRENT_USER: isize = 0x8000_0001u32 as isize;
    const KEY_ALL_ACCESS: u32 = 0xF003F;
    pub const REG_SZ: u32 = 1;
    pub const REG_EXPAND_SZ: u32 = 2;
    pub const REG_DWORD: u32 = 4;

    #[link(name = "advapi32")]
    extern "system" {
        fn RegCreateKeyExW(
            hkey: isize,
            sub: *const u16,
            reserved: u32,
            class: *const u16,
            options: u32,
            sam: u32,
            sa: *const c_void,
            out: *mut isize,
            disp: *mut u32,
        ) -> i32;
        fn RegOpenKeyExW(hkey: isize, sub: *const u16, options: u32, sam: u32, out: *mut isize)
            -> i32;
        fn RegSetValueExW(
            hkey: isize,
            name: *const u16,
            reserved: u32,
            ty: u32,
            data: *const u8,
            len: u32,
        ) -> i32;
        fn RegQueryValueExW(
            hkey: isize,
            name: *const u16,
            reserved: *mut u32,
            ty: *mut u32,
            data: *mut u8,
            len: *mut u32,
        ) -> i32;
        fn RegCloseKey(hkey: isize) -> i32;
        fn RegDeleteTreeW(hkey: isize, sub: *const u16) -> i32;
        fn MoveFileExW(from: *const u16, to: *const u16, flags: u32) -> i32;
    }

    fn 宽(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    pub fn 写值(子键: &str, 名: &str, 值: &str, 类型: u32) -> Result<(), String> {
        let sub = 宽(子键);
        let mut h: isize = 0;
        let rc = unsafe {
            RegCreateKeyExW(
                HKEY_CURRENT_USER,
                sub.as_ptr(),
                0,
                std::ptr::null(),
                0,
                KEY_ALL_ACCESS,
                std::ptr::null(),
                &mut h,
                std::ptr::null_mut(),
            )
        };
        if rc != 0 {
            return Err(format!("打开注册表键「{子键}」失败（错误码 {rc}）"));
        }
        // 字符串值一律 UTF-16LE + 结尾 NUL（Windows 的老规矩）
        let mut 字节: Vec<u8> = 值.encode_utf16().flat_map(|u| u.to_le_bytes()).collect();
        字节.extend_from_slice(&[0, 0]);
        let nm = 宽(名);
        let rc = unsafe {
            RegSetValueExW(h, nm.as_ptr(), 0, 类型, 字节.as_ptr(), 字节.len() as u32)
        };
        unsafe { RegCloseKey(h) };
        if rc != 0 {
            return Err(format!("写注册表值「{名}」失败（错误码 {rc}）"));
        }
        Ok(())
    }

    /// 写一个 32 位整数（`REG_DWORD`）。
    ///
    /// 🔴 必须与 `写值` 分开：「应用和功能」里的 `NoModify` / `NoRepair` /
    ///    `EstimatedSize` 都是**数字**值。用字符串写进去系统读不到 ——
    ///    表现就是「明明写了，那个『修改』按钮还在」。
    pub fn 写数(子键: &str, 名: &str, 值: u32) -> Result<(), String> {
        let sub = 宽(子键);
        let mut h: isize = 0;
        let rc = unsafe {
            RegCreateKeyExW(
                HKEY_CURRENT_USER,
                sub.as_ptr(),
                0,
                std::ptr::null(),
                0,
                KEY_ALL_ACCESS,
                std::ptr::null(),
                &mut h,
                std::ptr::null_mut(),
            )
        };
        if rc != 0 {
            return Err(format!("打开注册表键「{子键}」失败（错误码 {rc}）"));
        }
        let 字节 = 值.to_le_bytes();
        let nm = 宽(名);
        let rc = unsafe {
            RegSetValueExW(
                h,
                nm.as_ptr(),
                0,
                REG_DWORD,
                字节.as_ptr(),
                字节.len() as u32,
            )
        };
        unsafe { RegCloseKey(h) };
        if rc != 0 {
            return Err(format!("写注册表值「{名}」失败（错误码 {rc}）"));
        }
        Ok(())
    }

    pub fn 读值(子键: &str, 名: &str) -> Option<String> {
        let sub = 宽(子键);
        let nm = 宽(名);
        let mut h: isize = 0;
        let rc = unsafe {
            RegOpenKeyExW(HKEY_CURRENT_USER, sub.as_ptr(), 0, KEY_ALL_ACCESS, &mut h)
        };
        if rc != 0 {
            return None;
        }
        let mut 类型: u32 = 0;
        let mut 长度: u32 = 0;
        let rc = unsafe {
            RegQueryValueExW(
                h,
                nm.as_ptr(),
                std::ptr::null_mut(),
                &mut 类型,
                std::ptr::null_mut(),
                &mut 长度,
            )
        };
        if rc != 0 || 长度 == 0 {
            unsafe { RegCloseKey(h) };
            return None;
        }
        let mut 缓冲 = vec![0u8; 长度 as usize];
        let rc = unsafe {
            RegQueryValueExW(
                h,
                nm.as_ptr(),
                std::ptr::null_mut(),
                &mut 类型,
                缓冲.as_mut_ptr(),
                &mut 长度,
            )
        };
        unsafe { RegCloseKey(h) };
        if rc != 0 {
            return None;
        }
        let mut u16s: Vec<u16> = 缓冲
            .chunks_exact(2)
            .map(|c| u16::from_le_bytes([c[0], c[1]]))
            .collect();
        while u16s.last() == Some(&0) {
            u16s.pop();
        }
        Some(String::from_utf16_lossy(&u16s))
    }

    /// 把文件标记成「**下次开机时删除**」。
    ///
    /// 卸载程序自己正被 Windows 锁着（运行中的 exe 删不掉），可目录得腾空
    /// 才能删掉 —— 于是先把它挪到临时目录，再挂一个「重启后删」的标记。
    /// 这是 Windows 上卸载器的老办法，不是绕路。
    pub fn 安排重启后删除(路径: &str) -> bool {
        let w = 宽(路径);
        unsafe { MoveFileExW(w.as_ptr(), std::ptr::null(), 4) != 0 } // 4 = DELAY_UNTIL_REBOOT
    }

    pub fn 删键(子键: &str) -> Result<(), String> {
        let sub = 宽(子键);
        let rc = unsafe { RegDeleteTreeW(HKEY_CURRENT_USER, sub.as_ptr()) };
        // 2 = ERROR_FILE_NOT_FOUND：本来就没有，算成功（卸载要幂等）
        if rc != 0 && rc != 2 {
            return Err(format!("删注册表键「{子键}」失败（错误码 {rc}）"));
        }
        Ok(())
    }
}

/// 把 `目录` 追加到用户 PATH（已存在就不重复加）。返回「是不是新加的」。
///
/// ⚠️ **`#[cfg(windows)]` 不能少**（M174 修）：下面是 `reg` 模块（也是 Windows 专用），
/// 而文件末尾还有一对 `#[cfg(not(windows))]` 的同名兜底函数。漏了这个 cfg，
/// 在 Linux/macOS 上**两个版本都参与编译** ⇒ `error[E0428] 已定义多次` ⇒
/// 整个 crate 编不过（M172 起 Linux/macOS 的 CI 一直是红的，本地只验了 Windows）。
#[cfg(windows)]
fn 加path(目录: &str) -> Result<bool, String> {
    let 键 = "Environment";
    let 旧 = reg::读值(键, "Path").unwrap_or_default();
    let 项们: Vec<&str> = 旧.split(';').filter(|s| !s.is_empty()).collect();
    if 项们.iter().any(|s| s.eq_ignore_ascii_case(目录)) {
        return Ok(false);
    }
    let 新 = if 旧.is_empty() {
        目录.to_string()
    } else {
        format!("{};{}", 旧.trim_end_matches(';'), 目录)
    };
    reg::写值(键, "Path", &新, reg::REG_EXPAND_SZ)?;
    Ok(true)
}

/// 从用户 PATH 里去掉 `目录`。返回「是不是真的去掉了」。
#[cfg(windows)]
fn 减path(目录: &str) -> Result<bool, String> {
    let 键 = "Environment";
    let 旧 = reg::读值(键, "Path").unwrap_or_default();
    let 项们: Vec<&str> = 旧.split(';').filter(|s| !s.is_empty()).collect();
    let 留下: Vec<&str> = 项们
        .iter()
        .copied()
        .filter(|s| !s.eq_ignore_ascii_case(目录))
        .collect();
    if 留下.len() == 项们.len() {
        return Ok(false);
    }
    reg::写值(键, "Path", &留下.join(";"), reg::REG_EXPAND_SZ)?;
    Ok(true)
}

#[cfg(not(windows))]
mod reg {
    pub const REG_SZ: u32 = 1;
    pub const REG_EXPAND_SZ: u32 = 2;
    pub fn 写值(_: &str, _: &str, _: &str, _: u32) -> Result<(), String> {
        Err("注册表只有 Windows 有".to_string())
    }
    pub fn 写数(_: &str, _: &str, _: u32) -> Result<(), String> {
        Err("注册表只有 Windows 有".to_string())
    }
    pub fn 读值(_: &str, _: &str) -> Option<String> {
        None
    }
    pub fn 删键(_: &str) -> Result<(), String> {
        Ok(())
    }
    pub fn 安排重启后删除(_: &str) -> bool {
        false
    }
}

#[cfg(not(windows))]
fn 加path(_: &str) -> Result<bool, String> {
    Ok(false)
}
#[cfg(not(windows))]
fn 减path(_: &str) -> Result<bool, String> {
    Ok(false)
}

// ---------------------------------------------------------------------------
// 开始菜单快捷方式（.lnk）
// ---------------------------------------------------------------------------

/// 写一个 `.lnk` —— **不是手拼二进制，是调系统的 `IShellLink`**。
///
/// ## 为什么不自己拼字节
///
/// `.lnk` 是「看起来简单、实际上要 oracle」的典型：网上流传的最小实现只写
/// 相对路径，能生成文件、图标也对，但**能不能双击打开要看 Explorer 的解析路径**
/// —— 而那条路径没有测试可依，坏了也只会表现为「用户点了没反应」。
/// 让系统自己写，就永远不会写出它认不出的东西。
///
/// ## 为什么零依赖也要写这段
///
/// COM 的 ABI 就是「对象指针 + 虚表」，`extern "system"` 调虚表函数不需要任何
/// crate。代价是这一小片 `unsafe`：**虚表下标写错会静默调错函数**，所以每个下标
/// 都注了名（`IShellLinkW` 的顺序见 Windows SDK 的 `shobjidl.h`）。
#[cfg(windows)]
mod lnk {
    use std::ffi::c_void;

    #[repr(C)]
    struct Guid {
        d1: u32,
        d2: u16,
        d3: u16,
        d4: [u8; 8],
    }

    const CLSID_SHELLLINK: Guid = Guid {
        d1: 0x0002_1401,
        d2: 0,
        d3: 0,
        d4: [0xC0, 0, 0, 0, 0, 0, 0, 0x46],
    };
    const IID_ISHELLLINKW: Guid = Guid {
        d1: 0x0002_14F9,
        d2: 0,
        d3: 0,
        d4: [0xC0, 0, 0, 0, 0, 0, 0, 0x46],
    };
    const IID_IPERSISTFILE: Guid = Guid {
        d1: 0x0000_010b,
        d2: 0,
        d3: 0,
        d4: [0xC0, 0, 0, 0, 0, 0, 0, 0x46],
    };
    const CLSCTX_INPROC_SERVER: u32 = 1;
    const COINIT_APARTMENTTHREADED: u32 = 2;

    /// `IShellLinkW` 的虚表下标（IUnknown 占 0–2）。
    const 取路径: usize = 3;
    /// `GetArguments`（读回参数，查「快捷方式带没带程序文件」用）。
    const 取参数: usize = 10;
    const 设说明: usize = 7;
    const 设工作目录: usize = 9;
    /// M177：**命令行参数**（`"app\\计算器.jsh"`）。
    ///
    /// 自带运行时的包里，快捷方式指向 `bin\\jishi.exe`，程序文件是**参数** ——
    /// 不能指望 `.jsh` 有文件关联（那要在注册表里抢全局，装两个基石程序就打架）。
    const 设参数: usize = 11;
    const 设图标: usize = 17;
    const 设路径: usize = 20;
    /// `IPersistFile`：IUnknown 0–2，GetClassID 3，IsDirty 4，Load 5，**Save 6**。
    const 存盘: usize = 6;

    #[link(name = "ole32")]
    extern "system" {
        fn CoInitializeEx(reserved: *mut c_void, mode: u32) -> i32;
        fn CoCreateInstance(
            clsid: *const Guid,
            outer: *mut c_void,
            ctx: u32,
            iid: *const Guid,
            out: *mut *mut c_void,
        ) -> i32;
        fn CoUninitialize();
    }

    fn 宽(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// 取虚表里第 `n` 个函数指针。
    unsafe fn 虚函数(对象: *mut c_void, n: usize) -> *const usize {
        let 表 = *(对象 as *mut *const *const usize);
        *表.add(n)
    }

    pub fn 建(
        lnk路径: &str,
        目标: &str,
        工作目录: &str,
        说明: &str,
        参数: &str,
    ) -> Result<(), String> {
        unsafe {
            // 已经初始化过（RPC_E_CHANGED_MODE = 0x8001_0106）不算错
            let hr = CoInitializeEx(std::ptr::null_mut(), COINIT_APARTMENTTHREADED);
            if hr < 0 && hr as u32 != 0x8001_0106 {
                return Err(format!("COM 初始化失败（0x{:08x}）", hr as u32));
            }
            let mut 链接: *mut c_void = std::ptr::null_mut();
            let hr = CoCreateInstance(
                &CLSID_SHELLLINK,
                std::ptr::null_mut(),
                CLSCTX_INPROC_SERVER,
                &IID_ISHELLLINKW,
                &mut 链接,
            );
            if hr < 0 || 链接.is_null() {
                return Err(format!("建 ShellLink 失败（0x{:08x}）", hr as u32));
            }

            let 目标宽 = 宽(目标);
            let 目录宽 = 宽(工作目录);
            let 说明宽 = 宽(说明);
            let mut 错 = String::new();

            type 一参 = unsafe extern "system" fn(*mut c_void, *const u16) -> i32;
            type 图标参 = unsafe extern "system" fn(*mut c_void, *const u16, i32) -> i32;

            let f: 一参 = std::mem::transmute(虚函数(链接, 设路径));
            let hr = f(链接, 目标宽.as_ptr());
            if hr < 0 {
                错 = format!("设目标失败（0x{:08x}）", hr as u32);
            }
            if 错.is_empty() {
                let f: 一参 = std::mem::transmute(虚函数(链接, 设工作目录));
                let _ = f(链接, 目录宽.as_ptr());
                let f: 一参 = std::mem::transmute(虚函数(链接, 设说明));
                let _ = f(链接, 说明宽.as_ptr());
                if !参数.is_empty() {
                    let 参数宽 = 宽(参数);
                    let f: 一参 = std::mem::transmute(虚函数(链接, 设参数));
                    let hr = f(链接, 参数宽.as_ptr());
                    if hr < 0 {
                        错 = format!("设参数失败（0x{:08x}）", hr as u32);
                    }
                }
                let f: 图标参 = std::mem::transmute(虚函数(链接, 设图标));
                let _ = f(链接, 目标宽.as_ptr(), 0);

                let mut 文件: *mut c_void = std::ptr::null_mut();
                let qi: unsafe extern "system" fn(
                    *mut c_void,
                    *const Guid,
                    *mut *mut c_void,
                ) -> i32 = std::mem::transmute(虚函数(链接, 0));
                let hr = qi(链接, &IID_IPERSISTFILE, &mut 文件);
                if hr < 0 || 文件.is_null() {
                    错 = format!("拿 IPersistFile 失败（0x{:08x}）", hr as u32);
                } else {
                    let 路径宽 = 宽(lnk路径);
                    let 存: unsafe extern "system" fn(*mut c_void, *const u16, i32) -> i32 =
                        std::mem::transmute(虚函数(文件, 存盘));
                    let hr = 存(文件, 路径宽.as_ptr(), 1);
                    if hr < 0 {
                        错 = format!("写 .lnk 失败（0x{:08x}）", hr as u32);
                    }
                    let 释放: unsafe extern "system" fn(*mut c_void) -> u32 =
                        std::mem::transmute(虚函数(文件, 2));
                    释放(文件);
                }
            }
            let 释放: unsafe extern "system" fn(*mut c_void) -> u32 =
                std::mem::transmute(虚函数(链接, 2));
            释放(链接);
            CoUninitialize();
            if 错.is_empty() {
                Ok(())
            } else {
                Err(错)
            }
        }
    }

    /// 读回一个 `.lnk` 的 `(目标, 参数)` —— 给 `--解析快捷方式` 用。
    ///
    /// M177 起**参数也要读**：自带运行时的包里快捷方式指向引擎、程序文件是参数，
    /// 只报目标看不出「双击之后到底跑哪个程序」。
    pub fn 读(lnk路径: &str) -> Result<(String, String), String> {
        unsafe {
            let hr = CoInitializeEx(std::ptr::null_mut(), COINIT_APARTMENTTHREADED);
            if hr < 0 && hr as u32 != 0x8001_0106 {
                return Err(format!("COM 初始化失败（0x{:08x}）", hr as u32));
            }
            let mut 链接: *mut c_void = std::ptr::null_mut();
            let hr = CoCreateInstance(
                &CLSID_SHELLLINK,
                std::ptr::null_mut(),
                CLSCTX_INPROC_SERVER,
                &IID_ISHELLLINKW,
                &mut 链接,
            );
            if hr < 0 || 链接.is_null() {
                return Err(format!("建 ShellLink 失败（0x{:08x}）", hr as u32));
            }
            let mut 错 = String::new();
            let mut 目标 = String::new();
            let mut 参数 = String::new();
            let mut 文件: *mut c_void = std::ptr::null_mut();
            let qi: unsafe extern "system" fn(
                *mut c_void,
                *const Guid,
                *mut *mut c_void,
            ) -> i32 = std::mem::transmute(虚函数(链接, 0));
            let hr = qi(链接, &IID_IPERSISTFILE, &mut 文件);
            if hr < 0 || 文件.is_null() {
                错 = format!("拿 IPersistFile 失败（0x{:08x}）", hr as u32);
            } else {
                let 路径宽 = 宽(lnk路径);
                let 载: unsafe extern "system" fn(*mut c_void, *const u16, u32) -> i32 =
                    std::mem::transmute(虚函数(文件, 5));
                let hr = 载(文件, 路径宽.as_ptr(), 0);
                if hr < 0 {
                    错 = format!("读 .lnk 失败（0x{:08x}）", hr as u32);
                } else {
                    let mut 缓冲 = vec![0u16; 1024];
                    let 取: unsafe extern "system" fn(
                        *mut c_void,
                        *mut u16,
                        i32,
                        *mut c_void,
                        u32,
                    ) -> i32 = std::mem::transmute(虚函数(链接, 取路径));
                    let hr = 取(链接, 缓冲.as_mut_ptr(), 1024, std::ptr::null_mut(), 0);
                    if hr < 0 {
                        错 = format!("取目标失败（0x{:08x}）", hr as u32);
                    } else {
                        let 长 = 缓冲.iter().position(|c| *c == 0).unwrap_or(缓冲.len());
                        目标 = String::from_utf16_lossy(&缓冲[..长]);
                        // 参数（GetArguments = 10）：跟 GetPath 同一个签名
                        let mut 参缓冲 = vec![0u16; 2048];
                        let 取参: unsafe extern "system" fn(
                            *mut c_void,
                            *mut u16,
                            i32,
                            *mut c_void,
                            u32,
                        ) -> i32 = std::mem::transmute(虚函数(链接, 取参数));
                        if 取参(链接, 参缓冲.as_mut_ptr(), 2048, std::ptr::null_mut(), 0) >= 0 {
                            let 参长 =
                                参缓冲.iter().position(|c| *c == 0).unwrap_or(参缓冲.len());
                            参数 = String::from_utf16_lossy(&参缓冲[..参长]);
                        }
                    }
                }
                let 释放: unsafe extern "system" fn(*mut c_void) -> u32 =
                    std::mem::transmute(虚函数(文件, 2));
                释放(文件);
            }
            let 释放: unsafe extern "system" fn(*mut c_void) -> u32 =
                std::mem::transmute(虚函数(链接, 2));
            释放(链接);
            CoUninitialize();
            if 错.is_empty() {
                Ok((目标, 参数))
            } else {
                Err(错)
            }
        }
    }
}

/// 非 Windows 的**同签名桩**：Linux / macOS 上这两件事本来就不存在，
/// 但调用点必须编得过。
///
/// 🔴 **签名要和 Windows 那边一字不差**（M177 踩过）：给 `建` 加了「参数」
/// 那一维之后忘了改这里 ⇒ Linux / macOS 的 CI 三平台一起红，
/// 而 Windows 本地全绿。这就是 AGENTS 里「`#[cfg(windows)]` 必须配对」的意思。
#[cfg(not(windows))]
mod lnk {
    pub fn 建(_: &str, _: &str, _: &str, _: &str, _: &str) -> Result<(), String> {
        Err("开始菜单快捷方式只有 Windows 有".to_string())
    }
    pub fn 读(_: &str) -> Result<(String, String), String> {
        Err("开始菜单快捷方式只有 Windows 有".to_string())
    }
}

/// 桌面上的那个 `.lnk`（M200 补）。
fn 桌面快捷方式路径(名字: &str) -> PathBuf {
    桌面目录().join(format!("{名字}.lnk"))
}

/// 桌面目录的权威答案。
///
/// 🔴 不能写死 `%USERPROFILE%\Desktop`：
/// * OneDrive 会把桌面重定向到 `%USERPROFILE%\OneDrive\Desktop`；
/// * 中文 Windows 上物理目录名可能就叫「桌面」，而系统认知的是那个 GUID；
/// * 企业漫游配置也可能是网络路径。
///
/// 系统自己记在 `User Shell Folders` 里，值是 `%USERPROFILE%\Desktop` 这种
/// **带环境变量**的串 —— 要自己展开（`ExpandEnvironmentStrings` 也行，
/// 但只为这一处再引一个 API 不值当）。
fn 桌面目录() -> PathBuf {
    let 键 = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders";
    if let Some(v) = reg::读值(键, "Desktop") {
        let v = 展开环境变量(&v);
        if !v.trim().is_empty() {
            return PathBuf::from(v);
        }
    }
    PathBuf::from(env::var("USERPROFILE").unwrap_or_else(|_| ".".to_string())).join("Desktop")
}

/// 展开 `%VAR%`（注册表里那些路径都是这种写法）。
fn 展开环境变量(s: &str) -> String {
    let 字: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < 字.len() {
        if 字[i] == '%' {
            if let Some(偏) = 字[i + 1..].iter().position(|c| *c == '%') {
                let 名: String = 字[i + 1..i + 1 + 偏].iter().collect();
                if !名.is_empty() {
                    if let Ok(v) = env::var(&名) {
                        out.push_str(&v);
                        i = i + 偏 + 2;
                        continue;
                    }
                }
            }
        }
        out.push(字[i]);
        i += 1;
    }
    out
}

/// 这次安装**不该**往系统里写东西的理由（`None` = 该写）。
///
/// 只有一个触发条件：**`JISHI_SETUP_NO_SYSTEM=1`**（判据 / 自动化专用）。
///
/// ## 为什么不是「装到临时目录就不写」（第一版就是那么写的，被判据打回来了）
///
/// 那听起来很合理：装到 `%TEMP%` 的程序活不过一次清理，不该出现在
/// 「设置 → 应用」里。但它把 M171/M177 的判据全弄红了 —— 那几条**就是**
/// 在 `tmp_path` 里装一遍，然后验「开始菜单里有快捷方式」「卸载项写上了」。
/// 判据要验的正是系统集成，而规则说「临时目录不集成」—— 两边都对，冲突。
///
/// ⇒ 正确的分工：**安装器照做它该做的**（装哪儿就集成到哪儿），
///    **判据自己负责别留垃圾**（这一条给它们一个显式开关）。
///
/// ## 那用户撞见的「点不动的升级演练」怎么根治
///
/// 那是 M200 第一轮的判据**装完从不卸载**留下的（注册表项指着
/// `%TEMP%\pytest-…\装\uninstall.exe`，测试结束临时目录被删）。
/// 两条一起修：① 文件语义的判据统一带这个开关；② 要验系统集成的判据
/// 装到 `build/` 下并在 `finally` 里把目录 / 注册表 / 快捷方式全清。
fn 跳过系统集成() -> Option<String> {
    if env::var("JISHI_SETUP_NO_SYSTEM").ok().as_deref() == Some("1") {
        return Some("设了 JISHI_SETUP_NO_SYSTEM=1".to_string());
    }
    None
}

/// 开始菜单里的那个 `.lnk` 的完整路径（`%APPDATA%\Microsoft\Windows\Start Menu\Programs\`）。
fn 快捷方式路径(名字: &str) -> PathBuf {
    let 基 = env::var("APPDATA")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| {
            let home = env::var("USERPROFILE").unwrap_or_else(|_| ".".to_string());
            format!("{home}\\AppData\\Roaming")
        });
    Path::new(&基)
        .join("Microsoft")
        .join("Windows")
        .join("Start Menu")
        .join("Programs")
        .join(format!("{名字}.lnk"))
}

// ---------------------------------------------------------------------------
// 动作
// ---------------------------------------------------------------------------

/// 规范成绝对路径，并**去掉 `\\?\` 前缀**。
///
/// `fs::canonicalize` 在 Windows 上给的是 `\\?\D://...`（verbatim 路径）：
/// 能用来开文件，但拿去做字符串比较会失配（`D://...`.starts_with 它 = 假），
/// 塞进注册表也难看。这里统一削掉。
fn 绝对路径(p: &Path) -> PathBuf {
    if let Ok(v) = fs::canonicalize(p) {
        let s = v.display().to_string();
        if let Some(rest) = s.strip_prefix(r"\\?\") {
            return PathBuf::from(rest);
        }
        return v;
    }
    if p.is_absolute() {
        return p.to_path_buf();
    }
    env::current_dir()
        .map(|c| c.join(p))
        .unwrap_or_else(|_| p.to_path_buf())
}

fn 默认安装目录(名字: &str) -> PathBuf {
    // %LOCALAPPDATA% 是「当前用户、不需要管理员」的安装位置（Chrome / VS Code
    // 的用户级安装都在这）。**装到 Program Files 要提权，那不是这一版要的。**
    let 基 = env::var("LOCALAPPDATA")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| {
            let home = env::var("USERPROFILE").unwrap_or_else(|_| ".".to_string());
            format!("{home}\\AppData\\Local")
        });
    Path::new(&基).join("Programs").join(名字)
}

/// 进度回调：`(0..=1, 一句话)` —— 装到哪一步了。
///
/// 用 `&dyn` 而不是泛型：向导（经 IPC 注入页面）与控制台（打印）两条路都要传，
/// 泛型会让每处调用各单态化一份。
type 进度报<'a> = dyn Fn(f32, &str) + 'a;

/// 解包。`段` = 把「本题内部的 0..1」映射到**全局进度**的哪一段。
///
/// 为什么进度按**文件数**算而不是字节数：字节总量要先全过一遍才准，而文件数
/// 当场就有。用户看到「解包 12/30」比一个凭空估出来的百分比诚实 —— 估不准的
/// 进度条会卡在 90% 不动，那比没有更糟。
fn 解到(
    包: &包,
    目标: &Path,
    进度: Option<&进度报>,
    段: (f32, f32),
) -> Result<(), String> {
    let 总 = 包
        .条目们
        .iter()
        .filter(|(n, _)| !n.ends_with('/') && n != 清单名)
        .count()
        .max(1);
    // 最多报 50 次：每报一次就是一次「跨线程 → 主线程 → WebView 执行 JS」，
    // 三千个文件逐个报会把 IPC 刷爆，界面反而更卡。
    let 每 = (总 / 50).max(1);
    let mut 第 = 0usize;

    for (名, 数据) in &包.条目们 {
        if 名.ends_with('/') || 名 == 清单名 {
            continue;
        }
        jishi_ffi::zip_entry_name_ok(名)?;
        let 相对 = 名.replace('/', std::path::MAIN_SEPARATOR_STR);
        let 全 = 目标.join(相对);
        if let Some(父) = 全.parent() {
            fs::create_dir_all(父).map_err(|e| format!("建目录「{}」失败：{e}", 父.display()))?;
        }
        fs::write(&全, 数据).map_err(|e| format!("写「{}」失败：{e}", 全.display()))?;
        第 += 1;
        if let Some(f) = 进度 {
            if 第 % 每 == 0 || 第 == 总 {
                let x = 段.0 + 段.1 * (第 as f32 / 总 as f32);
                f(x, &format!("解包 {第}/{总}"));
            }
        }
    }
    // 清单也留一份在安装目录：卸载/升级/事后核对都靠它（它不在 payload 里，
    // 所以不会被当成用户文件删掉）。
    if let Some((_, 原文)) = 包.条目们.iter().find(|(n, _)| n == 清单名) {
        fs::write(目标.join(清单名), 原文).map_err(|e| format!("写清单失败：{e}"))?;
    }
    Ok(())
}

fn 安装(
    自身: &[u8],
    包: &包,
    目标: &Path,
    静默: bool,
    要path: bool,
    要桌面: bool,
    进度: Option<&进度报>,
) -> Result<(), String> {
    包.核对()?;
    fs::create_dir_all(目标).map_err(|e| format!("建安装目录「{}」失败：{e}", 目标.display()))?;
    // 注册表里要存**绝对路径**：`--目标` 可能是相对路径，而「应用」里点卸载时
    // 工作目录完全不确定（Explorer 给的 cwd 是 system32）。
    let 目标 = &绝对路径(目标);

    // M200：**该不该做系统集成**先定下来（见 `跳过系统集成的理由`）。
    let 跳过 = 跳过系统集成();

    解到(包, 目标, 进度, (0.08, 0.84))?;
    if let Some(f) = 进度 {
        f(0.93, "写卸载程序");
    }

    // 卸载程序 = **本程序去掉载荷**（只留存根那截，几百 KB）。
    // 这样它不需要包里那些文件，也能自己知道自己是谁。
    let off = 找载荷(自身).map(|(o, _)| o).ok_or("内部错误：找不到载荷偏移")?;
    let 卸载路径 = 目标.join(卸载名);
    fs::write(&卸载路径, &自身[..off])
        .map_err(|e| format!("写卸载程序「{}」失败：{e}", 卸载路径.display()))?;

    let 目录文 = 目标.display().to_string();
    let 键 = format!(
        "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{}",
        包.名字
    );
    let 卸载命令 = format!("\"{}\" --卸载", 卸载路径.display());

    // ---- 快捷方式指到哪 ----
    // M177：自带运行时的包（有 `bin/` 引擎）—— 快捷方式指**引擎**，
    // 用户的程序文件当**命令行参数**。为什么不直接指向 `.jsh`：那要注册全局
    // 文件关联，装两个基石程序就互相覆盖；而快捷方式带参数是每个程序独立的。
    let 入口盘 = 目标
        .join(包.入口.replace('/', std::path::MAIN_SEPARATOR_STR))
        .display()
        .to_string();
    let (快捷目标, 快捷参数) = if !包.引擎.is_empty() {
        (
            目标
                .join(包.引擎.replace('/', std::path::MAIN_SEPARATOR_STR))
                .display()
                .to_string(),
            if 包.入口.is_empty() {
                String::new()
            } else {
                // 带引号：安装路径里可能有空格
                format!("\"{入口盘}\"")
            },
        )
    } else if 包.入口.is_empty() {
        (卸载路径.display().to_string(), String::new())
    } else {
        (入口盘.clone(), String::new())
    };

    // ---- 注册表卸载项 ----
    let mut 注册问题: Option<String> = None;
    if 跳过.is_none() {
        // 🔴 图标只能给 `.exe`：`.jsh` 没有图标资源，拿它当 `DisplayIcon`
        //    只会在「应用和功能」里显示一个**白板** —— 用户会以为装坏了。
        //    卸载器本身也是个 exe，退回它至少有个正经图标。
        let 图标 = if 快捷目标.to_ascii_lowercase().ends_with(".exe") {
            快捷目标.clone()
        } else {
            卸载路径.display().to_string()
        };
        for (名, 值) in [
            ("DisplayName", 包.名字.clone()),
            ("DisplayVersion", 包.版本.clone()),
            ("InstallLocation", 目录文.clone()),
            ("DisplayIcon", 图标),
            ("UninstallString", 卸载命令.clone()),
            ("QuietUninstallString", format!("{卸载命令} /S")),
        ] {
            if let Err(e) = reg::写值(&键, 名, &值, reg::REG_SZ) {
                注册问题 = Some(e);
                break;
            }
        }
        // 这两个是**数字**：系统据此决定要不要显示「修改 / 修复」按钮。
        // 我们没有那两种模式（点了会把 `/MODIFY` 甩给卸载器，什么也不会发生），
        // 所以显式关掉。写不进去不算失败（文件已经装好了）。
        if 注册问题.is_none() {
            let _ = reg::写数(&键, "NoModify", 1);
            let _ = reg::写数(&键, "NoRepair", 1);
            // 「应用和功能」里那一列「大小」读的是它（单位 KB）
            let kb = (包.文件们.iter().map(|(_, 大小, _)| *大小).sum::<i64>() / 1024).max(1);
            let _ = reg::写数(&键, "EstimatedSize", kb as u32);
        }
    }

    let mut path问题: Option<String> = None;
    let mut 加了path = false;
    if 要path && 跳过.is_none() {
        // M177：PATH 要加**能敲出程序名**的那个目录 —— 带运行时时可执行在 bin/。
        let path目录 = if 包.引擎.is_empty() {
            目录文.clone()
        } else {
            目标.join("bin").display().to_string()
        };
        match 加path(&path目录) {
            Ok(v) => 加了path = v,
            Err(e) => path问题 = Some(e),
        }
    }

    // ---- 开始菜单 / 桌面 ----
    let 快捷方式 = 快捷方式路径(&包.名字);
    let mut 快捷问题: Option<String> = None;
    let mut 桌面问题: Option<String> = None;
    if 跳过.is_none() && !包.名字.is_empty() {
        if let Some(父) = 快捷方式.parent() {
            let _ = fs::create_dir_all(父);
        }
        if let Err(e) = lnk::建(
            &快捷方式.display().to_string(),
            &快捷目标,
            &目录文,
            &format!("{} 命令", 包.名字),
            &快捷参数,
        ) {
            快捷问题 = Some(e);
        }
        if 要桌面 {
            let 桌面 = 桌面快捷方式路径(&包.名字);
            if let Some(父) = 桌面.parent() {
                let _ = fs::create_dir_all(父);
            }
            if let Err(e) = lnk::建(
                &桌面.display().to_string(),
                &快捷目标,
                &目录文,
                &format!("{} 命令", 包.名字),
                &快捷参数,
            ) {
                桌面问题 = Some(e);
            }
        }
    }

    if let Some(f) = 进度 {
        f(0.99, "收尾");
    }
    if !静默 {
        if let Some(因) = &跳过 {
            // 为什么不集成**要说出来**：否则用户装完在「应用和功能」里找不到，
            // 会以为安装器坏了（其实是它故意不往系统里写东西）。
            println!("  没做系统集成：{因}");
        }
        println!("安装到：{目录文}");
        println!("  共 {} 个文件", 包.文件们.len());
        match &注册问题 {
            None if 跳过.is_some() => {}
            None => println!("  已写入卸载项（设置 → 应用 里能卸）"),
            // 注册表写不进去**不算失败**：文件已经装好了，程序能用。
            // 但必须说出来 —— 否则用户以为「装了」，却在「应用」里找不到。
            Some(e) => println!("  ⚠️ 卸载项没写上：{e}（文件已装好，程序能用）"),
        }
        match &快捷问题 {
            None if 跳过.is_none() && !包.名字.is_empty() => {
                println!("  开始菜单里有了：{}", 快捷方式.display())
            }
            Some(e) => println!("  ⚠️ 开始菜单快捷方式没建成：{e}"),
            _ => {}
        }
        if 要桌面 {
            match &桌面问题 {
                None if 跳过.is_none() => println!("  桌面上也有了"),
                Some(e) => println!("  ⚠️ 桌面快捷方式没建成：{e}"),
                _ => {}
            }
        }
        if 加了path {
            println!("  已加入 PATH（新开的终端里能直接敲程序名）");
        } else if let Some(e) = &path问题 {
            println!("  ⚠️ PATH 没改成：{e}");
        }
        if !包.引擎.is_empty() && !包.入口.is_empty() {
            // 自带运行时：告诉用户「双击开始菜单里那个就行」，别让他对着路径发呆
            println!(
                "完成：开始菜单里点「{}」就能打开（引擎 {} + 程序 {}）",
                包.名字,
                目标
                    .join(包.引擎.replace('/', std::path::MAIN_SEPARATOR_STR))
                    .display(),
                入口盘
            );
        } else if !包.入口.is_empty() {
            println!("完成：{}", 入口盘);
        } else {
            println!("完成（这个包没标「入口」，去安装目录里找）");
        }
    }
    Ok(())
}

/// M200：装之前先判「全新 / 升级 / 降级 / 相同」，再**带备份**执行。
///
/// 四条规矩，都是「不做就会静默出事」的：
///
/// 1. **版本相同不装**（除非 `--重装`）。双击两次同一个安装包不该产生两次升级，
///    更不该把用户数据搞出新副本。
/// 2. **往低版本装要显式要**（`--允许降级`）。默认拒绝并说清「装着的是哪个版本」——
///    降级会丢掉新版本才有的功能，这不能是默认行为。
/// 3. **先备份再动手**，装砸了整个还原（`installer::备份`）。
/// 4. **升级只删「旧清单有、新清单没有」的文件** —— 用户自己放进安装目录的东西
///    不在旧清单里，一个都不碰。这是「升级保留用户数据」的全部实现。
fn 装并管升级(
    自身: &[u8],
    包: &包,
    目标: &Path,
    静默: bool,
    要path: bool,
    要桌面: bool,
    允许降级: bool,
    重装: bool,
    进度: Option<&进度报>,
) -> Result<(), String> {
    use jishi_ffi::installer as 内核;

    // ① 单实例：两个安装器同时写一个目录，轻则「文件被占用」，
    //    重则**一边回滚一边写入**，装完是个混合体。
    let _锁 = 内核::独占::抢(&包.名字);
    if _锁.is_none() {
        eprintln!(
            "⚠️  已经有一个「{}」的安装正在进行，等它装完再来（同时装会互相踩）。",
            包.名字
        );
        return Err("另一个安装器正在运行".to_string());
    }

    // ② 判定
    if let Some(f) = 进度 {
        f(0.02, "检查已有安装");
    }
    let 目录 = 绝对路径(目标);
    let 已装 = 内核::读已装(&目录);
    let 动 = 内核::判断(已装.as_ref(), &包.版本, 允许降级);
    match &动 {
        内核::动作::相同 if !重装 => {
            if !静默 {
                println!("已经装的就是 {} —— 不再重复安装（要重装加 `--重装`）", 包.版本);
            }
            内核::日志(&format!("跳过（版本相同）：{}", 目录.display()));
            if let Some(f) = 进度 {
                f(1.0, "已经是最新版本");
            }
            return Ok(());
        }
        内核::动作::降级 { 从 } if !允许降级 => {
            return Err(format!(
                "装着更新的版本「{从}」，这个包是「{}」—— 默认不往下降级。\n\
                 确实要降级：加 `--允许降级`（新版本才有的功能会丢）。",
                包.版本
            ));
        }
        _ => {}
    }
    if !静默 {
        println!("{} → {}", 动.说明(), 目录.display());
    }

    // ③ 备份 → 装 → 成功丢弃 / 失败还原
    if let Some(f) = 进度 {
        f(0.05, "备份原安装（装砸了要能还原）");
    }
    let 备份 = 内核::备份::做(&目录, &包.名字)?;
    内核::日志(&format!("开始（{}）→ {}", 动.说明(), 目录.display()));
    match 安装(自身, 包, 目标, 静默, 要path, 要桌面, 进度) {
        Ok(()) => {
            备份.丢弃();
            // ④ 升级：删掉「旧清单里有、新清单里没有」的文件
            if let Some(旧) = &已装 {
                let 新文件们: Vec<String> =
                    包.文件们.iter().map(|(p, _, _)| p.clone()).collect();
                let mut 删了 = 0usize;
                for 相对 in 内核::该删的(旧, &新文件们) {
                    if let Some(p) = 内核::相对路径(&目录, &相对) {
                        if fs::remove_file(&p).is_ok() {
                            删了 += 1;
                        }
                    }
                }
                if 删了 > 0 && !静默 {
                    println!("  清掉 {删了} 个旧版本才有的文件（你自己的文件不在里面）");
                }
            }
            内核::日志("完成");
            if let Some(f) = 进度 {
                f(1.0, "完成");
            }
            if !静默 {
                println!("{} 完成", 动.说明());
            }
            Ok(())
        }
        Err(e) => {
            // ⚠️ 先取备份位置再还原：`还原()` 成功时会把副本删掉
            let 位置 = 备份.备份位置();
            let 还原好 = 备份.还原();
            内核::日志(&format!(
                "失败：{e}；回滚{}",
                if 还原好 { "成功" } else { "失败" }
            ));
            if 还原好 {
                Err(format!("{e}\n（安装失败，已经把原来的安装**原样还原**）"))
            } else {
                Err(format!(
                    "{e}\n🔴 安装失败，而且**没能自动还原** —— 原来的文件在：\n  {}\n\
                     把那个目录里的东西复制回来即可。",
                    位置.map(|p| p.display().to_string()).unwrap_or_default()
                ))
            }
        }
    }
}

fn 卸载(目标: &Path, 静默: bool) -> Result<(), String> {
    let 清单路径 = 目标.join(清单名);
    let 文本 = fs::read_to_string(&清单路径)
        .map_err(|e| format!("读不到「{}」：{e}（这个目录不是打包装出来的？）", 清单路径.display()))?;
    let j = jishi_ffi::json::parse(&文本).map_err(|e| format!("清单解析失败：{e}"))?;
    let 名字 = 取文(&j, "名字");
    let 路径们: Vec<String> = j
        .get("文件们")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|f| f.get("路径").and_then(|v| v.as_str()).map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();

    let mut 删了 = 0usize;
    for 路径 in &路径们 {
        let 全 = 目标.join(路径.replace('/', std::path::MAIN_SEPARATOR_STR));
        if fs::remove_file(&全).is_ok() {
            删了 += 1;
        }
    }
    let _ = fs::remove_file(&清单路径);

    // 卸载程序自己删不掉自己（正被锁着）。先把它挪到临时目录 —— 挪是允许的，
    // 删不行 —— 目录立刻就空了；再给它挂一个「重启后删」的标记。
    let 自己 = env::current_exe().unwrap_or_else(|_| 目标.join(卸载名));
    let mut 挪走了 = false;
    if 自己.starts_with(目标) {
        let 名 = format!(".jishi-uninst-{}.exe", std::process::id());
        // ⚠️ **`fs::rename` 不能跨卷**：安装目录在 D: 而 TEMP 在 C: 时必失败。
        // 所以先试系统临时目录（常见情形同卷、最干净），再退到**安装目录的父目录**
        // （一定同卷）。
        let mut 候选 = vec![env::temp_dir().join(&名)];
        if let Some(父) = 目标.parent() {
            候选.push(父.join(&名));
        }
        for 临时 in 候选 {
            if fs::rename(&自己, &临时).is_ok() {
                挪走了 = true;
                let _ = reg::安排重启后删除(&临时.display().to_string());
                break;
            }
        }
    }

    // 自底向上删空目录（删不掉的——比如用户在里面放了东西——就留着，不硬来）
    let mut 目录们: Vec<PathBuf> = Vec::new();
    for 路径 in &路径们 {
        let 全 = 目标.join(路径.replace('/', std::path::MAIN_SEPARATOR_STR));
        let mut 父 = 全.parent().map(|p| p.to_path_buf());
        while let Some(p) = 父 {
            if p == 目标 || !p.starts_with(目标) {
                break;
            }
            目录们.push(p.clone());
            父 = p.parent().map(|x| x.to_path_buf());
        }
    }
    目录们.sort();
    目录们.dedup();
    for d in 目录们.iter().rev() {
        let _ = fs::remove_dir(d); // 非空就失败，正好（不去扫用户文件）
    }
    let _ = fs::remove_dir(目标);
    if !挪走了 {
        // 挪不走（跨卷之类）：至少让它别在重启后还赖着
        let _ = reg::安排重启后删除(&自己.display().to_string());
    }

    if !名字.is_empty() {
        let 键 = format!(
            "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{名字}"
        );
        // 🔴 **容错**：这个键可能被别的清理工具删了、也可能当初就没写成功
        //    （注册表不可写时我们只警告不失败）。以前这里是 `?`，于是
        //    「文件删得干干净净，退出码却是 1」—— 用户看到报错会以为没卸掉。
        let _ = reg::删键(&键);
        // 快捷方式也要收走 —— 否则卸载完还留着点了没反应的图标。
        let _ = fs::remove_file(快捷方式路径(&名字));
        let _ = fs::remove_file(桌面快捷方式路径(&名字));
    }
    let _ = 减path(&目标.display().to_string());

    if !静默 {
        println!("已卸载：{}（删了 {删了} 个文件）", 目标.display());
        if !挪走了 {
            println!("  ⚠️ 卸载程序自己被占用，已挂到「重启后删除」");
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// 控制台（M200：双击不弹黑窗）
// ---------------------------------------------------------------------------

/// 这个进程现在有没有可用的控制台。
#[cfg(windows)]
fn 有控制台() -> bool {
    #[link(name = "kernel32")]
    extern "system" {
        fn GetConsoleWindow() -> isize;
    }
    unsafe { GetConsoleWindow() != 0 }
}

/// 把标准输出接到一个**有人看得见**的地方，返回「有没有去处」。
///
/// 三种情形，优先级从高到低：
///
/// 1. **已经有控制台**（从 cmd 直接跑、或本来就不是 GUI 子系统）→ 就用它；
/// 2. **父进程给了管道 / 文件**（pytest、CI、`> log.txt`）→ **一个字都不动**；
/// 3. **两者都没有**（双击，父进程是 Explorer）→ 先 `AttachConsole` 借父控制台，
///    借不到就指到 `NUL`，让 `println!` 变成「写进去了没人看」而不是每次写都失败。
///
/// 返回值 = 「输出有人看得见吗」。**调用方用它决定报错走 `eprintln` 还是弹框**：
/// 返回 `true` 时输出有去处（终端 / 管道 / 文件），返回 `false` 时是纯 GUI 启动，
/// 报错只能靠 `MessageBox`。
#[cfg(windows)]
fn 接上控制台() -> bool {
    const STD_OUTPUT_HANDLE: u32 = (-11i32) as u32;
    const STD_ERROR_HANDLE: u32 = (-12i32) as u32;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    const FILE_SHARE_READ: u32 = 1;
    const FILE_SHARE_WRITE: u32 = 2;
    const OPEN_EXISTING: u32 = 3;
    const ATTACH_PARENT_PROCESS: u32 = (-1i32) as u32;

    #[link(name = "kernel32")]
    extern "system" {
        fn AttachConsole(pid: u32) -> i32;
        fn SetStdHandle(which: u32, h: isize) -> i32;
        fn GetStdHandle(which: u32) -> isize;
        fn CreateFileW(
            name: *const u16,
            access: u32,
            share: u32,
            sa: *mut std::ffi::c_void,
            disposition: u32,
            flags: u32,
            tmpl: isize,
        ) -> isize;
    }
    fn 宽(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    if 有控制台() {
        return true;
    }
    // 🔴 **父进程已经给了去处就别动**（M200 第一版踩的坑）：
    //    判据 / CI / `> log.txt` 都会把 stdout 变成**管道或文件**再起进程 ——
    //    那时 `GetStdHandle` 是有效的，只是「不是控制台」。第一版无条件
    //    `SetStdHandle(NUL)` 把它们全盖掉了，于是 `--查看` 一个字都没有，
    //    而退出码还是 0（最坏的一种：看着成功、事实上什么都没输出）。
    //    这里返回 `true`：管道那头有人读 ⇒ 报错照常 `eprintln`，**不要弹框**
    //    （判据 / 脚本里弹一个模态框 = 一直挂到超时）。
    if unsafe { GetStdHandle(STD_OUTPUT_HANDLE) } > 0 {
        return true;
    }
    unsafe {
        if AttachConsole(ATTACH_PARENT_PROCESS) != 0 {
            // ⚠️ `AttachConsole` **不会**改本进程的标准句柄，得自己指过去
            let con = 宽("CONOUT$");
            let h = CreateFileW(
                con.as_ptr(),
                GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                std::ptr::null_mut(),
                OPEN_EXISTING,
                0,
                0,
            );
            if h != -1 {
                SetStdHandle(STD_OUTPUT_HANDLE, h);
                SetStdHandle(STD_ERROR_HANDLE, h);
                return true;
            }
        }
        let nul = 宽("NUL");
        let h = CreateFileW(
            nul.as_ptr(),
            GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            std::ptr::null_mut(),
            OPEN_EXISTING,
            0,
            0,
        );
        if h != -1 {
            SetStdHandle(STD_OUTPUT_HANDLE, h);
            SetStdHandle(STD_ERROR_HANDLE, h);
        }
        false
    }
}

#[cfg(not(windows))]
fn 接上控制台() -> bool {
    true
}

/// 装完给用户的那句话（M200 向导模式）。
///
/// 🔴 为什么用系统消息框：向导是**双击启动的 GUI 程序**，没有控制台 ——
///    `println` 出去的东西用户永远看不到，那等于「装完了什么都不说」。
///    换成控制台输出只在 `--参数` 那条路上有意义（那里用户本来就在终端里）。
fn 报消息(名字: &str, 版本: &str, 错误: Option<&str>) {
    let 文 = match 错误 {
        None => format!(
            "「{名字} {版本}」装好了。\n\n去开始菜单里找它就能打开；\n不想要了就在「设置 → 应用」里卸载。"
        ),
        Some(e) => format!("「{名字} {版本}」没装上。\n\n{e}"),
    };
    报框("基石安装程序", &文, 错误.is_some());
}

/// 一个系统消息框（Windows），其它平台打一行。
///
/// 🔴 为什么安装器非得有它：向导是**双击启动的 GUI 程序**，没有控制台 ——
///    `println` 出去的东西用户永远看不到，那等于「装完了什么都不说」。
fn 报框(标题文: &str, 正文文: &str, 是错误: bool) {
    #[cfg(windows)]
    {
        #[link(name = "user32")]
        extern "system" {
            fn MessageBoxW(h: isize, text: *const u16, caption: *const u16, ty: u32) -> i32;
        }
        fn 宽文(s: &str) -> Vec<u16> {
            s.encode_utf16().chain(std::iter::once(0)).collect()
        }
        let 正文 = 宽文(正文文);
        let 标题 = 宽文(标题文);
        // 0 = MB_OK；0x40 = 信息图标；0x10 = 错误图标
        let 图标 = if 是错误 { 0x10 } else { 0x40 };
        unsafe {
            MessageBoxW(0, 正文.as_ptr(), 标题.as_ptr(), 图标);
        }
    }
    #[cfg(not(windows))]
    {
        let _ = 标题文;
        println!("{正文文}");
    }
}

fn 用法() -> String {
    "用法：\n\
     \x20 setup.exe                 双击 → 图形向导（选目录 / 桌面 / PATH）\n\
     \x20 setup.exe --目标 <目录>    装到指定目录（不给参数就是开向导）\n\
     \x20 setup.exe --加PATH        把安装目录加进用户 PATH（默认不加）\n\
     \x20 setup.exe --桌面          在桌面放一个快捷方式（向导里也能勾）\n\
     \x20 setup.exe /S              静默\n\
     \x20 setup.exe --查看          只打印清单与「这次会做什么」\n\
     \x20 setup.exe --校验          只核对内容\n\
     \x20 setup.exe --卸载          卸载\n"
        .to_string()
}

fn 跑子安装器(包: &包, 目标: Option<&str>, 静默: bool, 透传: &[String]) -> Result<i32, String> {
    let (_, 数据) = 包
        .条目们
        .iter()
        .find(|(n, _)| n == 子安装器名)
        .ok_or("内部错误：找不到子安装器载荷")?;
    let 临时 = env::temp_dir().join(format!("jishi-setup-{}", std::process::id()));
    let _ = fs::remove_dir_all(&临时);
    fs::create_dir_all(&临时).map_err(|e| format!("建临时目录失败：{e}"))?;
    let 程序 = 临时.join("setup.exe");
    fs::write(&程序, 数据).map_err(|e| format!("写安装器失败：{e}"))?;

    let mut 命令 = Command::new(&程序);
    // 老语义：`--目标` 单独给的时候**就是静默**（存根版是控制台程序，从不问人）
    if 静默 || 目标.is_some() {
        命令.args(["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]);
    }
    for a in 透传 {
        命令.arg(a);
    }
    if let Some(d) = 目标 {
        命令.arg(format!("/DIR={d}"));
    }
    // 等它装完再清临时目录：向导版要那份 setup.exe 一直活着
    let 状态 = 命令.status().map_err(|e| format!("起不来安装器：{e}"))?;
    let _ = fs::remove_dir_all(&临时);
    Ok(状态.code().unwrap_or(1))
}

fn main() -> ExitCode {
    // M200：双击**不弹黑窗**。有控制台可接就接上（命令行用法要看输出），
    // 接不上就把 stdout 指到 NUL —— 那时 `println!` 是「写进去了没人看」，
    // 而不是每次写都失败。
    let 看得见 = 接上控制台();
    let args: Vec<String> = env::args().skip(1).collect();
    let mut 静默 = false;
    //: M200：向导里勾了才建，命令行也能直接要（`--桌面`）
    let mut 要桌面 = false;
    let mut 要path = false;
    let mut 目标参数: Option<String> = None;
    let mut 快捷参数: Option<String> = None;
    let mut 动作 = "安装";
    //: M200：往低版本装要显式要（默认拦下来问一句）；版本相同也重装也要显式要
    let mut 允许降级 = false;
    let mut 重装 = false;
    //: M198：认不出但看着像开关的参数（要等读到载荷才知道该不该透传）
    let mut 未知: Vec<String> = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        match a {
            "/S" | "-s" | "--静默" => 静默 = true,
            "--加PATH" => 要path = true,
            "--桌面" | "--desktop" => 要桌面 = true,
            "--允许降级" | "--allow-downgrade" => 允许降级 = true,
            "--重装" | "--reinstall" => 重装 = true,
            "--查看" | "--info" => 动作 = "查看",
            "--校验" | "--verify" => 动作 = "校验",
            "--卸载" | "--uninstall" => 动作 = "卸载",
            "--解析快捷方式" | "--lnk" => {
                i += 1;
                动作 = "解析快捷方式";
                快捷参数 = args.get(i).cloned();
            }
            "--目标" | "--target" => {
                i += 1;
                目标参数 = args.get(i).cloned();
                if 目标参数.is_none() {
                    eprintln!("错误：「{a}」后面要跟一个目录");
                    return ExitCode::from(2);
                }
            }
            "-h" | "--help" | "/?" => {
                print!("{}", 用法());
                return ExitCode::SUCCESS;
            }
            _ if a.starts_with("/D=") => 目标参数 = Some(a[3..].to_string()),
            _ if a.starts_with("/DIR=") => 目标参数 = Some(a[5..].to_string()),
            _ if a.starts_with("--目标=") => 目标参数 = Some(a[6..].to_string()),
            // M198：Inno 自己的开关（`/VERYSILENT` `/SILENT` `/SP-` `/LOG=…` …）。
            // 先收着：普通包收到它们仍然是「不认识这个参数」，**只有**子安装器包才透传。
            _ if a.starts_with('/') || a.starts_with("--") => 未知.push(a.to_string()),
            _ => {
                eprintln!("错误：不认识这个参数「{a}」\n{}", 用法());
                return ExitCode::from(2);
            }
        }
        i += 1;
    }

    let 自身 = match env::current_exe().and_then(fs::read) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("错误：读自己失败：{e}");
            return ExitCode::FAILURE;
        }
    };

    // 解析快捷方式也走早退：它跟载荷无关（用户可能拿它查一个坏掉的 .lnk）
    if 动作 == "解析快捷方式" {
        let Some(路径) = 快捷参数 else {
            eprintln!("用法：jishi-pack-stub --解析快捷方式 <路径.lnk>");
            return ExitCode::from(2);
        };
        return match lnk::读(&路径) {
            Ok((目标, 参数)) => {
                println!("目标：{目标}");
                if !参数.is_empty() {
                    println!("参数：{参数}");
                }
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("错误：{e}");
                ExitCode::FAILURE
            }
        };
    }

    // 卸载走另一条路：它不该依赖载荷（卸载程序是截短的存根，没有 zip）
    if 动作 == "卸载" {
        // M198：子安装器包没有我们写的 uninstall.exe（卸载器是 Inno 写的
        // unins000.exe、卸载项在它自己的 AppId 下），所以这里只能指路。
        if let Some((off, len)) = 找载荷(&自身) {
            if let Ok(包) = 读包(&自身[off..off + len]) {
                if 包.条目们.iter().any(|(n, _)| n == 子安装器名) {
                    eprintln!("这个安装包装的是 Inno 向导版 —— 卸载请走：");
                    eprintln!("  设置 → 应用 → 已安装的应用 里找「{}」", 包.名字);
                    eprintln!("  或者运行安装目录里的 unins000.exe");
                    return ExitCode::FAILURE;
                }
            }
        }
        let 目标 = 目标参数
            .map(PathBuf::from)
            .or_else(|| {
                env::current_exe()
                    .ok()
                    .and_then(|p| p.parent().map(|x| x.to_path_buf()))
            })
            .unwrap_or_else(|| PathBuf::from("."));
        return match 卸载(&目标, 静默) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                // 从「设置 → 应用」点卸载时这是个 GUI 进程，没有控制台 ——
                // `eprintln` 出去的东西用户看不见，等于「点了没反应」。
                if 看得见 {
                    eprintln!("错误：{e}");
                } else {
                    报框("卸载失败", &e, true);
                }
                ExitCode::FAILURE
            }
        };
    }

    let Some((off, len)) = 找载荷(&自身) else {
        eprintln!("错误：这个程序里没有安装内容 —— 它不是完整的 setup.exe");
        return ExitCode::FAILURE;
    };
    let 包 = match 读包(&自身[off..off + len]) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("错误：{e}");
            return ExitCode::FAILURE;
        }
    };

    match 动作 {
        "查看" => {
            println!("名字：{}", 包.名字);
            println!("版本：{}", 包.版本);
            println!("平台：{}", 包.平台);
            println!("入口：{}", 包.入口);
            if 包.条目们.iter().any(|(n, _)| n == 子安装器名) {
                println!("载荷：Inno Setup 向导安装器（M198；参数由本程序翻译）");
                println!("文件：1 个（内嵌安装器）");
            } else {
                println!("文件：{} 个", 包.文件们.len());
            }
            // M200：顺带报「这台机器上装的是哪个版本、这次会做成什么动作」。
            // 为什么放在 `--查看` 里：它是**只读**动作，脚本与判据都能安全调 ——
            // 「升级会不会覆盖 / 会不会拒绝」这种问题不该靠真装一遍才知道。
            let 查目录 = 目标参数
                .clone()
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    默认安装目录(if 包.名字.is_empty() { "程序" } else { &包.名字 })
                });
            let 已装 = jishi_ffi::installer::读已装(&绝对路径(&查目录));
            match &已装 {
                Some(旧) => println!("已装：{}（{}）", 旧.版本, 绝对路径(&查目录).display()),
                None => println!("已装：无"),
            }
            println!(
                "本次会：{}",
                jishi_ffi::installer::判断(已装.as_ref(), &包.版本, false).说明()
            );
            ExitCode::SUCCESS
        }
        "校验" => match 包.核对() {
            Ok(()) => {
                if !静默 {
                    println!("校验通过：{} 个文件与清单一致", 包.文件们.len());
                }
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("错误：{e}");
                ExitCode::FAILURE
            }
        },
        _ => {
            // 🔴 M200：`自身` 与 `包` 都要能被**后台线程**用（向导把安装放到
            //    线程里跑，否则窗口会「未响应」）。线程要求 'static ⇒ 只能
            //    `Arc` 持有，不能借用 main 的栈。
            let 自身 = std::sync::Arc::new(自身);
            let 包 = std::sync::Arc::new(包);

            // M198：载荷是「另一个安装器」（Inno 向导）⇒ 交给它装，参数由这里翻译
            if 包.条目们.iter().any(|(n, _)| n == 子安装器名) {
                return match 跑子安装器(&包, 目标参数.as_deref(), 静默, &未知) {
                    Ok(0) => ExitCode::SUCCESS,
                    Ok(码) => ExitCode::from(码.clamp(1, 255) as u8),
                    Err(e) => {
                        eprintln!("错误：{e}");
                        ExitCode::FAILURE
                    }
                };
            }
            // M200：**双击**（一个参数都没有）= 开向导。
            // 带参数（`/S`、`--目标`…）一律走控制台那条 —— 脚本不该弹出窗口，
            // 那会让「无人值守安装」卡在一个没人点的界面上（最坏的情况：
            // CI 里挂到超时，而日志只有一个空白的窗口句柄）。
            #[cfg(feature = "native-window")]
            {
                if args.is_empty() {
                    let 默认 = 默认安装目录(if 包.名字.is_empty() { "程序" } else { &包.名字 });
                    let 已装 = jishi_ffi::installer::读已装(&绝对路径(&默认));
                    let 字节数: i64 = 包.文件们.iter().map(|(_, 大小, _)| *大小).sum();
                    // 任务要跨线程 ⇒ 捕获的东西必须自己拥有（clone 的是 Arc，不是数据）
                    let 任务: Box<向导::任务> = {
                        let 自身 = 自身.clone();
                        let 包 = 包.clone();
                        Box::new(move |目录: &Path, 加PATH: bool, 要桌面: bool, 进度: &进度报| {
                            装并管升级(
                                &自身[..], &包, 目录, false, 加PATH, 要桌面, false, false,
                                Some(进度),
                            )
                        })
                    };
                    let 结局 = 向导::问(
                        &包.名字,
                        &包.版本,
                        字节数,
                        &默认,
                        已装.as_ref().map(|x| x.版本.as_str()),
                        任务,
                    );
                    return match 结局 {
                        向导::结局::装好 | 向导::结局::取消 => ExitCode::SUCCESS,
                        向导::结局::失败(e) => {
                            报框("没装上", &e, true);
                            ExitCode::FAILURE
                        }
                        向导::结局::开不了窗(e) => {
                            // 这里的用户**看得见窗口**（他在双击），所以必须用
                            // 弹框说清楚 —— 静默退出等于「双击了没反应」。
                            let 自己 = env::current_exe()
                                .map(|p| p.display().to_string())
                                .unwrap_or_default();
                            报框(
                                "开不了安装向导",
                                &format!(
                                    "这台机器上没有可用的 WebView2，向导打不开。\n\n{e}\n\n\
                                     可以用静默安装（不需要窗口）：\n  \"{自己}\" /S"
                                ),
                                true,
                            );
                            ExitCode::FAILURE
                        }
                    };
                }
            }   // #[cfg(feature = "native-window")]

            // 普通包收到 Inno 的开关 = 用户拿错了安装包类型，别装作懂
            if let Some(a) = 未知.first() {
                eprintln!("错误：不认识这个参数「{a}」\n{}", 用法());
                return ExitCode::from(2);
            }
            let 目标 = 目标参数
                .map(PathBuf::from)
                .unwrap_or_else(|| 默认安装目录(if 包.名字.is_empty() { "程序" } else { &包.名字 }));
            // 控制台进度：`\r` 覆盖同一行，装完再补一个换行。
            // 静默（`/S`）时一个字都不打 —— 脚本解析输出时会嫌它吵。
            let 打进度 = |x: f32, s: &str| {
                use std::io::Write;
                print!("\r  {:3.0}% {s}", x * 100.0);
                let _ = std::io::stdout().flush();
            };
            let 进度: Option<&进度报> = if 静默 { None } else { Some(&打进度) };
            match 装并管升级(&自身[..], &包, &目标, 静默, 要path, 要桌面, 允许降级, 重装, 进度)
            {
                Ok(()) => {
                    if !静默 {
                        println!();
                    }
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    if 看得见 {
                        eprintln!("\n错误：{e}");
                    } else {
                        报框("安装失败", &e, true);
                    }
                    ExitCode::FAILURE
                }
            }
        }
    }
}
