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

use crate::BuiltinHost;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

use super::{as_text, need, need_range, table, R};
use crate::stdlib::datetime::format_system_time;
use crate::{err, list_new, Val};

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

/// 递归收集文件（`路径.递归列出` 用）。返回**相对路径**、只收文件。
/// 注意返回类型是 `Result<(), JishiError>` 而不是 `R`（`R` 带 `Val`）。
fn 递归收(
    dir: &Path,
    rel: &Path,
    suffix: &str,
    out: &mut Vec<String>,
) -> Result<(), crate::JishiError> {
    let entries = fs::read_dir(dir)
        .map_err(|e| err("文件错误", format!("读目录「{}」失败：{e}", dir.display())))?;
    for entry in entries {
        let entry = entry
            .map_err(|e| err("文件错误", format!("读目录「{}」失败：{e}", dir.display())))?;
        let name = entry.file_name().to_string_lossy().to_string();
        let full = entry.path();
        let 相对 = rel.join(&name);
        if full.is_dir() {
            递归收(&full, &相对, suffix, out)?;
            continue;
        }
        if !suffix.is_empty() {
            let 后缀 = full.extension().map(|e| format!(".{}", e.to_string_lossy()))
                .unwrap_or_default();
            if 后缀 != suffix {
                continue;
            }
        }
        out.push(相对.to_string_lossy().to_string());
    }
    Ok(())
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "路径";
        "连接" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            // ⚠️ 不能直接 `PathBuf::push` —— 见 `pathlike_join`。
            let parts: Vec<String> = args.iter().map(as_text).collect();
            Ok(Val::Str(pathlike_join(&parts)))
        },
        "存在" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("存在", args, 1)?;
            Ok(Val::Bool(Path::new(&as_text(&args[0])).exists()))
        },
        "是文件" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("是文件", args, 1)?;
            Ok(Val::Bool(Path::new(&as_text(&args[0])).is_file()))
        },
        "是目录" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("是目录", args, 1)?;
            Ok(Val::Bool(Path::new(&as_text(&args[0])).is_dir()))
        },
        "绝对路径" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("绝对路径", args, 1)?;
            let p = as_text(&args[0]);
            // `std::path::absolute` 只做词法规范化（不解析符号链接、不加
            // Windows 的 `\\?\` 前缀），是与 Python `resolve()` 最接近的
            let abs = std::path::absolute(&p)
                .map_err(|e| err("值错误", format!("「{p}」转不成绝对路径：{e}")))?;
            Ok(Val::Str(norm_sep(&abs)))
        },
        "父目录" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        "文件名" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("文件名", args, 1)?;
            Ok(Val::Str(file_name_of(&as_text(&args[0]))))
        },
        "后缀" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("后缀", args, 1)?;
            Ok(Val::Str(suffix_of(&file_name_of(&as_text(&args[0])))))
        },
        "无后缀名" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("无后缀名", args, 1)?;
            Ok(Val::Str(stem_of(&file_name_of(&as_text(&args[0])))))
        },
        "当前目录" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("当前目录", args, 0)?;
            let d = std::env::current_dir()
                .map_err(|e| err("文件错误", format!("取当前目录失败：{e}")))?;
            Ok(Val::Str(norm_sep(&d)))
        },
        "主目录" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("主目录", args, 0)?;
            Ok(Val::Str(home_dir()))
        },
        "创建目录" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        "删除" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        "列出" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        "递归列出" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("递归列出", args, 1, 2)?;
            let p = as_text(&args[0]);
            let suffix = if args.len() == 2 { as_text(&args[1]) } else { String::new() };
            let path = Path::new(&p);
            if !path.is_dir() {
                return Err(err("值错误", format!("「{p}」不是目录")));
            }
            let mut out: Vec<String> = Vec::new();
            递归收(path, Path::new(""), &suffix, &mut out)?;
            out.sort();
            Ok(list_new(out.into_iter().map(Val::Str).collect()))
        },
        "重命名" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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
        "大小" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("大小", args, 1)?;
            let p = as_text(&args[0]);
            let meta = fs::metadata(&p)
                .map_err(|e| err("文件错误", format!("读「{p}」的信息失败：{e}")))?;
            if !meta.is_file() {
                return Err(err("值错误", format!("「{p}」不是文件")));
            }
            Ok(Val::Int(meta.len() as i128))
        },
        "修改时间" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
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

// ---------------------------------------------------------------------------
// `连接` 用的路径串联（`pathlib.Path` 的 `a / b`，**不是** `PathBuf::push`）
// ---------------------------------------------------------------------------

/// Python `pathlib.Path` 的 `a / b` 串联语义 —— `路径.连接` 用的就是它。
///
/// ⚠️ **不是 `PathBuf::push`**：Rust 的 `push` 只做「拼 + 绝对段替换」，会把
/// `.` 与空段**原样留着**（`PathBuf::from(".").push("x")` → `./x`），而 pathlib 会
/// 把它们**吃掉**（`Path(".") / "x"` → `x`）。宿主以前就是这么错的：
/// `路径.连接(".", "x")` 在宿主上给 `.\x`、在 Python 上给 `x`
/// （R7.4 顺着 CLI 闸门查 `文件.路径拼接` 时一并查出来的 —— 同族问题）。
///
/// 规则（与 `pathlib` 对齐）：
/// * 段内**两种分隔符都认**（Windows 上 `/` 与 `\`）；
/// * **吃掉**空段与 `.`、**保留** `..`（pathlib 不做消解，`a / ".."` → `a/..`）；
/// * 绝对段（带根 / 带盘符）**重置**前面的累积；⚠️ **只带根不带盘符时保留已有盘符**
///   （`Path("C:/a") / "/b"` → `C:\b`，这是 pathlib 的规矩，照 Rust 写会错）；
/// * 输出用**平台原生分隔符**（Windows `\`、别处 `/`）；结果为空给 `.`。
///
/// ⚠️ **已知边界**：UNC 只覆盖常见的 `\\server\share\…`，`\\?\` 长路径前缀不认
/// （Python 的 `pathlib` 也不用它拼）。这一条是**如实登记**，不是「差不多」。
fn pathlike_join(parts: &[String]) -> String {
    #[cfg(windows)]
    const SEPS: [char; 2] = ['/', '\\'];
    #[cfg(not(windows))]
    const SEPS: [char; 1] = ['/'];
    let sep = std::path::MAIN_SEPARATOR;

    let mut drive = String::new();
    let mut rooted = false;
    let mut comps: Vec<&str> = Vec::new();

    for p in parts {
        let (d, r, rest) = split_drive_root(p);
        if r || !d.is_empty() {
            if !d.is_empty() {
                drive = d;
            }
            rooted = r;
            comps.clear();
        }
        for seg in rest.split(SEPS) {
            if seg.is_empty() || seg == "." {
                continue;
            }
            comps.push(seg);
        }
    }

    if drive.is_empty() && !rooted && comps.is_empty() {
        return ".".to_string();
    }
    let tail = comps.join(&sep.to_string());
    if rooted {
        format!("{drive}{sep}{tail}")
    } else if !drive.is_empty() {
        // 盘符但无根：`C:` + `x` → `C:x`（**不加分隔符**，pathlib 就是这么给的）
        format!("{drive}{tail}")
    } else {
        tail
    }
}

/// 拆出「盘符 / 根 / 其余」，口径照 `ntpath.splitdrive`。
#[cfg(windows)]
fn split_drive_root(p: &str) -> (String, bool, &str) {
    // UNC：`\\server\share\…` → 盘符 `\\server\share`
    if p.starts_with("\\\\") || p.starts_with("//") {
        let mut it = p[2..].split(['/', '\\']);
        let server = it.next().unwrap_or("");
        let share = it.next().unwrap_or("");
        if !server.is_empty() && !share.is_empty() {
            let head = format!("\\\\{server}\\{share}");
            let rest = &p[2 + server.len() + 1 + share.len()..];
            let (rooted, tail) = strip_one_sep(rest);
            return (head, rooted, tail);
        }
        let (_, tail) = strip_one_sep(p);
        return (String::new(), true, tail);
    }
    // `X:` 盘符
    let b = p.as_bytes();
    if b.len() >= 2 && b[1] == b':' && (b[0] as char).is_ascii_alphabetic() {
        let (rooted, tail) = strip_one_sep(&p[2..]);
        return (p[..2].to_string(), rooted, tail);
    }
    let (rooted, tail) = strip_one_sep(p);
    (String::new(), rooted, tail)
}

#[cfg(not(windows))]
fn split_drive_root(p: &str) -> (String, bool, &str) {
    let (rooted, tail) = strip_one_sep(p);
    (String::new(), rooted, tail)
}

/// 吃掉开头**一个**分隔符（pathlib 的「根」只算一层）。
fn strip_one_sep(p: &str) -> (bool, &str) {
    if p.starts_with('/') || p.starts_with('\\') {
        (true, &p[1..])
    } else {
        (false, p)
    }
}
