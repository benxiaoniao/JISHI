//! 标准库 `路径`（Python 侧 `jishi/stdlib/路径.py`，17 个函数）。
//!
//! 对齐的是 Python 的 `pathlib`（**不是** 直接套 `std::path`），有三处
//! 「照着 Rust 写就会错」的地方：
//!
//! 1. **`后缀` 不能直接用 `Path::extension`**。pathlib 的 `suffix` 规则是
//!    「最后一个点既不在开头、也不在末尾」，于是 `a.` 与 `.bashrc` 都算
//!    **无后缀**；而 `Path::extension()` 对 `a.` 会给 `Some("")`。
//! 2. **`父目录` 要有 `.` 的兜底**：`Path("a").parent()` 在 Rust 里是空串，
//!    而 pathlib 给 `Path(".")`（字符串是 `.`）。
//! 3. **Windows 上分隔符统一成 `\`**：pathlib 的 `PureWindowsPath.__str__`
//!    总吐反斜杠，而 Rust 的 `Path` 会保留输入里的 `/`。
//!
//! `列出` 返回的是条目名、`文件名`/`后缀`/`无后缀名` 只看最后一段，
//! 都不受分隔符规范化影响。

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use super::{as_text, need, need_range, table, R};
use crate::stdlib::datetime::format_system_time;
use crate::{err, Val, VM};

/// 把路径里的 `/` 换成平台分隔符（Windows 上 pathlib 总吐 `\`）。
pub(crate) fn norm_sep(p: &Path) -> String {
    let s = p.to_string_lossy().to_string();
    if cfg!(windows) {
        s.replace('/', "\\")
    } else {
        s
    }
}

/// pathlib 的 `name`：最后一段（两种分隔符都认）。
pub(crate) fn file_name_of(p: &str) -> String {
    let unified = p.replace('\\', "/");
    let trimmed = unified.trim_end_matches('/');
    match trimmed.rsplit_once('/') {
        Some((_, last)) => last.to_string(),
        None => trimmed.to_string(),
    }
}

/// pathlib 的 `suffix`：最后一个点必须**不在开头、也不在末尾**。
///
/// `Path::extension()` 做不到这一点（`a.` 会给 `Some("")`），所以自己算。
pub(crate) fn suffix_of(name: &str) -> String {
    if let Some(i) = name.rfind('.') {
        let before = name[..i].chars().count();
        let total = name.chars().count();
        if before > 0 && before + 1 < total {
            return name[i..].to_string();
        }
    }
    String::new()
}

/// pathlib 的 `stem`：去掉后缀的文件名（无后缀时就是原名）。
pub(crate) fn stem_of(name: &str) -> String {
    let suf = suffix_of(name);
    if suf.is_empty() {
        name.to_string()
    } else {
        name[..name.len() - suf.len()].to_string()
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! {
        "连接" => |args: &[Val], _vm: &mut VM| -> R {
            if args.is_empty() {
                return Ok(Val::Str(".".to_string()));
            }
            let mut buf = PathBuf::from(as_text(&args[0]));
            for seg in &args[1..] {
                buf.push(as_text(seg));
            }
            Ok(Val::Str(norm_sep(&buf)))
        },
        "存在" => |args: &[Val], _vm: &mut VM| -> R {
            need("存在", args, 1)?;
            Ok(Val::Bool(Path::new(&as_text(&args[0])).exists()))
        },
        "是文件" => |args: &[Val], _vm: &mut VM| -> R {
            need("是文件", args, 1)?;
            Ok(Val::Bool(Path::new(&as_text(&args[0])).is_file()))
        },
        "是目录" => |args: &[Val], _vm: &mut VM| -> R {
            need("是目录", args, 1)?;
            Ok(Val::Bool(Path::new(&as_text(&args[0])).is_dir()))
        },
        "绝对路径" => |args: &[Val], _vm: &mut VM| -> R {
            need("绝对路径", args, 1)?;
            let p = as_text(&args[0]);
            // `std::path::absolute` 只做词法规范化（不解析符号链接、不加
            // Windows 的 `\\?\` 前缀），是与 Python `resolve()` 最接近的
            let abs = std::path::absolute(&p)
                .map_err(|e| err("值错误", format!("「{p}」转不成绝对路径：{e}")))?;
            Ok(Val::Str(norm_sep(&abs)))
        },
        "父目录" => |args: &[Val], _vm: &mut VM| -> R {
            need("父目录", args, 1)?;
            let p = as_text(&args[0]);
            let parent = Path::new(&p)
                .parent()
                .map(|q| q.to_string_lossy().to_string());
            // pathlib 的 `Path("a").parent` 是 `Path(".")`
            let out = match parent {
                Some(q) if !q.is_empty() => q.replace('/', "\\"),
                _ => ".".to_string(),
            };
            Ok(Val::Str(if cfg!(windows) { out } else { out.replace('\\', "/") }))
        },
        "文件名" => |args: &[Val], _vm: &mut VM| -> R {
            need("文件名", args, 1)?;
            Ok(Val::Str(file_name_of(&as_text(&args[0]))))
        },
        "后缀" => |args: &[Val], _vm: &mut VM| -> R {
            need("后缀", args, 1)?;
            Ok(Val::Str(suffix_of(&file_name_of(&as_text(&args[0])))))
        },
        "无后缀名" => |args: &[Val], _vm: &mut VM| -> R {
            need("无后缀名", args, 1)?;
            Ok(Val::Str(stem_of(&file_name_of(&as_text(&args[0])))))
        },
        "当前目录" => |args: &[Val], _vm: &mut VM| -> R {
            need("当前目录", args, 0)?;
            let d = std::env::current_dir()
                .map_err(|e| err("文件错误", format!("取当前目录失败：{e}")))?;
            Ok(Val::Str(norm_sep(&d)))
        },
        "主目录" => |args: &[Val], _vm: &mut VM| -> R {
            need("主目录", args, 0)?;
            Ok(Val::Str(home_dir()))
        },
        "创建目录" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("创建目录", args, 1, 2)?;
            let p = as_text(&args[0]);
            let recursive = matches!(args.get(1), Some(Val::Bool(true)));
            let r = if recursive {
                fs::create_dir_all(&p)
            } else {
                // 只建最后一级：父目录不存在要报错（Python 的 `mkdir()` 同）
                fs::create_dir(&p)
            };
            // 返回值要规范化分隔符：Python 的 `str(p)` 给 `build\\_m33\\sub`
            let shown = norm_sep(Path::new(&p));
            match r {
                Ok(()) => Ok(Val::Str(shown)),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Ok(Val::Str(shown)),
                Err(e) => Err(err("文件错误", format!("创建目录「{p}」失败：{e}"))),
            }
        },
        "删除" => |args: &[Val], _vm: &mut VM| -> R {
            need("删除", args, 1)?;
            let p = as_text(&args[0]);
            let path = Path::new(&p);
            if path.is_file() {
                fs::remove_file(path)
                    .map_err(|e| err("文件错误", format!("删除「{p}」失败：{e}")))?;
                return Ok(Val::Bool(true));
            }
            if path.is_dir() {
                // 只删空目录（与 Python 的 `rmdir` 一致）；非空就如实报错，
                // 不递归删——那太容易误伤
                fs::remove_dir(path).map_err(|e| {
                    err("文件错误", format!("删除目录「{p}」失败（目录可能不是空的）：{e}"))
                })?;
                return Ok(Val::Bool(true));
            }
            Ok(Val::Bool(false))
        },
        "列出" => |args: &[Val], _vm: &mut VM| -> R {
            need("列出", args, 1)?;
            let p = as_text(&args[0]);
            let path = Path::new(&p);
            if !path.is_dir() {
                return Err(err("值错误", format!("「{p}」不是目录")));
            }
            let mut names: Vec<String> = Vec::new();
            for entry in fs::read_dir(path)
                .map_err(|e| err("文件错误", format!("读目录「{p}」失败：{e}")))?
            {
                let entry =
                    entry.map_err(|e| err("文件错误", format!("读目录「{p}」失败：{e}")))?;
                names.push(entry.file_name().to_string_lossy().to_string());
            }
            names.sort();
            Ok(crate::list_new(names.into_iter().map(Val::Str).collect()))
        },
        "重命名" => |args: &[Val], _vm: &mut VM| -> R {
            need("重命名", args, 2)?;
            let old = as_text(&args[0]);
            let new = as_text(&args[1]);
            if !Path::new(&old).exists() {
                return Err(err("值错误", format!("「{old}」不存在")));
            }
            fs::rename(&old, &new)
                .map_err(|e| err("文件错误", format!("把「{old}」重命名成「{new}」失败：{e}")))?;
            Ok(Val::Str(new))
        },
        "大小" => |args: &[Val], _vm: &mut VM| -> R {
            need("大小", args, 1)?;
            let p = as_text(&args[0]);
            let meta = fs::metadata(&p)
                .map_err(|e| err("文件错误", format!("读「{p}」的信息失败：{e}")))?;
            if !meta.is_file() {
                return Err(err("值错误", format!("「{p}」不是文件")));
            }
            Ok(Val::Int(meta.len() as i128))
        },
        "修改时间" => |args: &[Val], _vm: &mut VM| -> R {
            need("修改时间", args, 1)?;
            let p = as_text(&args[0]);
            let meta = fs::metadata(&p)
                .map_err(|e| err("文件错误", format!("读「{p}」的信息失败：{e}")))?;
            let mtime = meta
                .modified()
                .map_err(|e| err("文件错误", format!("读「{p}」的修改时间失败：{e}")))?;
            Ok(Val::Str(format_system_time(mtime)))
        },
    }
}

/// 用户主目录（Windows 用 `USERPROFILE`，其余用 `HOME`）。
fn home_dir() -> String {
    for key in ["USERPROFILE", "HOME"] {
        if let Ok(v) = std::env::var(key) {
            if !v.is_empty() {
                return v;
            }
        }
    }
    String::new()
}
