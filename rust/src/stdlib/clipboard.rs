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
        Err(crate::err_with_hint(
            "运行期错误",
            "「剪贴板.读文本」这一版只有 Windows 能做",
            "mac 要 NSPasteboard、Linux 要 GTK/X11/Wayland 三套 —— 都要额外绑定，没做".into(),
        ))
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
        let _ = 文;
        Err(crate::err_with_hint(
            "运行期错误",
            "「剪贴板.写文本」这一版只有 Windows 能做",
            "mac 要 NSPasteboard、Linux 要 GTK/X11/Wayland 三套 —— 都要额外绑定，没做".into(),
        ))
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
