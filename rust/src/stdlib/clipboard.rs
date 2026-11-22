//! 基石标准库：剪贴板（M185）。
//!
//! ## 只有 Windows 真做（其余的**明确报「不支持」**）
//!
//! | 平台 | 状态 |
//! |---|---|
//! | Windows | ✅ `OpenClipboard` 一族（裸 Win32，零依赖） |
//! | macOS | ❌ 要 `NSPasteboard`（objc 绑定），这一版没做 |
//! | Linux | ❌ 要 GTK / X11 / Wayland 三套，这一版没做 |
//! | 页面里（wasm） | ❌ 那是浏览器的剪贴板权限（`navigator.clipboard`），另一码事 |
//!
//! 报错**必须说清是哪一种**：「这个平台不支持」和「页面里该用别的东西」
//! 是两件不同的事，混成一句的话用户会去翻文档找不存在的开关。
//!
//! ## 两条 Win32 的坑（都在代码注释里复述了一遍）
//!
//! 1. `OpenClipboard` 成功之后**必须** `CloseClipboard`，哪怕中间出错 ——
//!    忘了它，这个进程之后再也打不开剪贴板（其它程序也会被拖住）；
//! 2. `SetClipboardData` 成功之后那块 `GlobalAlloc` 的内存**归系统所有**，
//!    再 `GlobalFree` 就是双重释放。

use std::collections::HashMap;

use crate::BuiltinHost;
use crate::Val;

use super::{need, table, R};

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "剪贴板";
        "读文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("读文本", args, 0)?;
            读文本()
        },
        "写文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("写文本", args, 1)?;
            let 文 = match &args[0] {
                Val::Str(s) => s.to_string(),
                other => {
                    return Err(crate::err(
                        "类型错误",
                        format!("「剪贴板.写文本」要文本，给的是{}", crate::type_label(other)),
                    ))
                }
            };
            写文本(&文)
        },
    }
}

fn 读文本() -> Result<Val, crate::JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        return Err(crate::err_with_hint(
            "运行期错误",
            "页面里的剪贴板是浏览器的权限，不走这个模块",
            "页面里用浏览器的 navigator.clipboard（要用户手势，且 https 或 localhost 才行）"
                .into(),
        ));
    }
    #[cfg(all(not(target_arch = "wasm32"), windows))]
    {
        return 系统剪贴板::读()
            .map(|s| Val::Str(s.into()))
            .map_err(|e| crate::err_with_hint("运行期错误", e, "剪贴板可能正被别的程序占着".into()));
    }
    #[cfg(all(not(target_arch = "wasm32"), not(windows)))]
    {
        // M223：mac / Linux 走系统命令。**读不到不是错**：
        // 「剪贴板里没有文本」和「读失败」在用户看来是同一件事 —— 给空文本。
        let (文, _为什么) = 命令剪贴板::读();
        Ok(Val::Str(文.into()))
    }
}

fn 写文本(文: &str) -> Result<Val, crate::JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        let _ = 文;
        return Err(crate::err_with_hint(
            "运行期错误",
            "页面里的剪贴板是浏览器的权限，不走这个模块",
            "页面里用浏览器的 navigator.clipboard".into(),
        ));
    }
    #[cfg(all(not(target_arch = "wasm32"), windows))]
    {
        return 系统剪贴板::写(文)
            .map(|_| Val::Bool(true))
            .map_err(|e| crate::err_with_hint("运行期错误", e, "剪贴板可能正被别的程序占着".into()));
    }
    #[cfg(all(not(target_arch = "wasm32"), not(windows)))]
    {
        // M223：没有工具时**返回假**（判据钉住「不报错」）。
        let (成, 为什么) = 命令剪贴板::写(文);
        if let Some(因) = 为什么 {
            // 不静默：写不进去要说一句为什么（stderr，不影响返回值）
            eprintln!("剪贴板没写上：{因}");
        }
        Ok(Val::Bool(成))
    }
}

/// 裸 Win32 的剪贴板（零依赖：`user32` + `kernel32` 就是系统的 DLL）。
#[cfg(windows)]
mod 系统剪贴板 {
    use std::ffi::c_void;

    const CF_UNICODETEXT: u32 = 13;
    const GMEM_MOVEABLE: u32 = 0x0002;
    /// 防御性上限：坏数据（没有结尾的 0）别把内存读穿。
    const 最长: usize = 8 * 1024 * 1024;

    #[link(name = "user32")]
    extern "system" {
        fn OpenClipboard(拥有者: isize) -> i32;
        fn CloseClipboard() -> i32;
        fn EmptyClipboard() -> i32;
        fn GetClipboardData(格式: u32) -> isize;
        fn SetClipboardData(格式: u32, 内存: isize) -> isize;
        fn IsClipboardFormatAvailable(格式: u32) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GlobalAlloc(标志: u32, 字节: usize) -> isize;
        fn GlobalLock(内存: isize) -> *mut c_void;
        fn GlobalUnlock(内存: isize) -> i32;
        fn GlobalFree(内存: isize) -> isize;
    }

    pub fn 写(文: &str) -> Result<(), String> {
        let 宽: Vec<u16> = 文.encode_utf16().chain(std::iter::once(0)).collect();
        unsafe {
            if OpenClipboard(0) == 0 {
                return Err("打不开剪贴板".to_string());
            }
            // 🔴 `CloseClipboard` 放在闭包**外面**：中途 `?` 返回也要执行到它，
            //    否则这个进程（以及别的程序）之后都用不了剪贴板。
            let 果 = (|| -> Result<(), String> {
                if EmptyClipboard() == 0 {
                    return Err("清空剪贴板失败".to_string());
                }
                let 块 = GlobalAlloc(GMEM_MOVEABLE, 宽.len() * 2);
                if 块 == 0 {
                    return Err("给剪贴板申请内存失败".to_string());
                }
                let 地 = GlobalLock(块) as *mut u16;
                if 地.is_null() {
                    GlobalFree(块);
                    return Err("锁不住那块内存".to_string());
                }
                std::ptr::copy_nonoverlapping(宽.as_ptr(), 地, 宽.len());
                GlobalUnlock(块);
                // 🔴 成功后这块内存**归系统所有** —— 再 `GlobalFree` 是双重释放。
                if SetClipboardData(CF_UNICODETEXT, 块) == 0 {
                    GlobalFree(块);
                    return Err("剪贴板不收这段文本".to_string());
                }
                Ok(())
            })();
            CloseClipboard();
            果
        }
    }

    pub fn 读() -> Result<String, String> {
        unsafe {
            if OpenClipboard(0) == 0 {
                return Err("打不开剪贴板".to_string());
            }
            let 果 = (|| -> Result<String, String> {
                // 剪贴板里有东西但不是文本 ⇒ **给空文本**，不报错（与 Python 侧一致：
                // 「读出来是空的」比「读失败」更贴切）。
                if IsClipboardFormatAvailable(CF_UNICODETEXT) == 0 {
                    return Ok(String::new());
                }
                let 块 = GetClipboardData(CF_UNICODETEXT);
                if 块 == 0 {
                    return Ok(String::new());
                }
                let 地 = GlobalLock(块) as *const u16;
                if 地.is_null() {
                    return Ok(String::new());
                }
                let mut 长 = 0usize;
                while 长 < 最长 && *地.add(长) != 0 {
                    长 += 1;
                }
                let 文 = String::from_utf16_lossy(std::slice::from_raw_parts(地, 长));
                GlobalUnlock(块);
                Ok(文)
            })();
            CloseClipboard();
            果
        }
    }
}

/// mac / Linux 的剪贴板：走系统命令（零新依赖）。
///
/// ## 为什么不用原生绑定
///
/// - mac 要 `NSPasteboard`（objc 消息）—— 但 `pbcopy`/`pbpaste` 是**系统自带**的，
///   一直在、跨版本稳定；
/// - Linux 要 GTK / X11 / Wayland **三套** —— 而 `wl-copy` / `xclip` / `xsel`
///   是各桌面环境的标配工具，按「现代 → 老」挨个试比绑三套库便宜得多。
///
/// ## 口径
///
/// 写：有一个能用就算成功；一个都没有 ⇒ 返回 `(假, Some(原因))`，**不报错**
/// （「这台机器没装剪贴板工具」是环境事实，不是程序写错了）。
/// 读：读不到一律给空文本（与 Windows「剪贴板里没有文本给空」同口径）。
#[cfg(all(not(target_arch = "wasm32"), not(windows)))]
mod 命令剪贴板 {
    use std::io::Write;
    use std::process::{Command, Stdio};

    fn 候选写() -> Vec<(&'static str, &'static [&'static str])> {
        #[cfg(target_os = "macos")]
        {
            vec![("pbcopy", &[][..])]
        }
        #[cfg(not(target_os = "macos"))]
        {
            vec![
                ("wl-copy", &[][..]),
                ("xclip", &["-selection", "clipboard"][..]),
                ("xsel", &["--clipboard", "--input"][..]),
            ]
        }
    }

    fn 候选读() -> Vec<(&'static str, &'static [&'static str])> {
        #[cfg(target_os = "macos")]
        {
            vec![("pbpaste", &[][..])]
        }
        #[cfg(not(target_os = "macos"))]
        {
            vec![
                ("wl-paste", &["--no-newline"][..]),
                ("xclip", &["-o", "-selection", "clipboard"][..]),
                ("xsel", &["--clipboard", "--output"][..]),
            ]
        }
    }

    pub fn 写(文: &str) -> (bool, Option<String>) {
        let mut 缺: Vec<&str> = Vec::new();
        for (程序, 参) in 候选写() {
            match Command::new(程序)
                .args(参)
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
            {
                Ok(mut 子) => {
                    let 写成 = 子
                        .stdin
                        .as_mut()
                        .map(|s| s.write_all(文.as_bytes()).is_ok())
                        .unwrap_or(false);
                    // 关掉 stdin（drop）才会让 pbcopy/xclip 收尾退出
                    let _ = 子.stdin.take();
                    let 态 = 子.wait();
                    if 写成 && 态.map(|s| s.success()).unwrap_or(false) {
                        return (true, None);
                    }
                    return (false, Some(format!("{程序} 没写进去")));
                }
                Err(_) => 缺.push(程序),
            }
        }
        (false, Some(format!("找不到剪贴板工具（试过 {}）", 缺.join(" / "))))
    }

    pub fn 读() -> (String, Option<String>) {
        let mut 缺: Vec<&str> = Vec::new();
        for (程序, 参) in 候选读() {
            match Command::new(程序).args(参).output() {
                Ok(出) => {
                    if 出.status.success() {
                        return (String::from_utf8_lossy(&出.stdout).to_string(), None);
                    }
                    return (String::new(), Some(format!("{程序} 没读出来")));
                }
                Err(_) => 缺.push(程序),
            }
        }
        (String::new(), Some(format!("找不到剪贴板工具（试过 {}）", 缺.join(" / "))))
    }
}
