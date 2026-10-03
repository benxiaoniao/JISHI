//! 标准库 `文件`（Python 侧 `jishi/stdlib/文件.py`，16 个函数）。
//!
//! **换行保真**：写 `\n` 就是 `\n`，不做平台转换。
//!
//! Python 的文本模式在 Windows 上会把 `\n` 悄悄写成 `\r\n`，于是「同一份
//! 程序在两个平台产出的文件字节不同」，也与 Node 的 `fs`（不转换）对不上。
//! M31 在 Python 侧显式 `newline=""` 定下这条规矩；本宿主天然就是字节保真
//! （`fs::write` 不转换），两边因此逐字节一致。

use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::Path;

use super::path::{file_name_of, suffix_of};
use super::text::split_lines;
use super::{as_text, need, table, to_vec, R};
use crate::{err, JishiError, Val, VM};

fn read_utf8(path: &str, on_missing: impl FnOnce() -> String) -> Result<String, JishiError> {
    if !Path::new(path).is_file() {
        return Err(err("文件错误", on_missing()));
    }
    let bytes =
        fs::read(path).map_err(|e| err("文件错误", format!("读文件「{path}」失败：{e}")))?;
    String::from_utf8(bytes)
        .map_err(|_| err("文件错误", format!("文件「{path}」不是 UTF-8 编码，没法按文本读取")))
}

/// 字节写（不做换行转换）。
fn write_bytes(path: &str, data: &[u8]) -> Result<(), JishiError> {
    fs::write(path, data).map_err(|e| err("文件错误", format!("写文件「{path}」失败：{e}")))
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "文件";
        "写文本" => |args: &[Val], _vm: &mut VM| -> R {
            need("写文本", args, 2)?;
            let p = as_text(&args[0]);
            write_bytes(&p, as_text(&args[1]).as_bytes())?;
            Ok(Val::None_)
        },
        "读文本" => |args: &[Val], _vm: &mut VM| -> R {
            need("读文本", args, 1)?;
            let p = as_text(&args[0]);
            Ok(Val::Str(read_utf8(&p, || format!("找不到文件「{p}」，没法读取"))?))
        },
        "追加文本" => |args: &[Val], _vm: &mut VM| -> R {
            need("追加文本", args, 2)?;
            let p = as_text(&args[0]);
            let mut f = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&p)
                .map_err(|e| err("文件错误", format!("打开「{p}」准备追加失败：{e}")))?;
            f.write_all(as_text(&args[1]).as_bytes())
                .map_err(|e| err("文件错误", format!("往「{p}」追加内容失败：{e}")))?;
            Ok(Val::None_)
        },
        "写行" => |args: &[Val], _vm: &mut VM| -> R {
            need("写行", args, 2)?;
            let p = as_text(&args[0]);
            let lines = to_vec(&args[1])?;
            let mut buf = String::new();
            for line in &lines {
                buf.push_str(&as_text(line));
                buf.push('\n');
            }
            write_bytes(&p, buf.as_bytes())?;
            Ok(Val::None_)
        },
        "按行读" => |args: &[Val], _vm: &mut VM| -> R {
            need("按行读", args, 1)?;
            let p = as_text(&args[0]);
            let text = read_utf8(&p, || format!("找不到文件「{p}」，没法读取"))?;
            let items: Vec<Val> = split_lines(&text).into_iter().map(Val::Str).collect();
            Ok(crate::list_new(items))
        },
        "文件存在" => |args: &[Val], _vm: &mut VM| -> R {
            need("文件存在", args, 1)?;
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
        "列出目录" => |args: &[Val], _vm: &mut VM| -> R {
            let p = if args.is_empty() { ".".to_string() } else { as_text(&args[0]) };
            let path = Path::new(&p);
            if !path.is_dir() {
                return Err(err("文件错误", format!("目录「{p}」不存在，没法列出内容")));
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
        "创建目录" => |args: &[Val], _vm: &mut VM| -> R {
            need("创建目录", args, 1)?;
            let p = as_text(&args[0]);
            // 与 Python 侧一致：自动创建中间层级、已存在也不报错
            fs::create_dir_all(&p)
                .map_err(|e| err("文件错误", format!("创建目录「{p}」失败：{e}")))?;
            Ok(Val::None_)
        },
        "删除文件" => |args: &[Val], _vm: &mut VM| -> R {
            need("删除文件", args, 1)?;
            let p = as_text(&args[0]);
            let path = Path::new(&p);
            if path.is_file() {
                fs::remove_file(path)
                    .map_err(|e| err("文件错误", format!("删除「{p}」失败：{e}")))?;
                Ok(Val::None_)
            } else if path.is_dir() {
                Err(err(
                    "文件错误",
                    format!("「{p}」是目录，请先清空再删里面的文件；删目录的功能暂未提供"),
                ))
            } else {
                Err(err("文件错误", format!("找不到文件「{p}」，没法删除")))
            }
        },
        "复制" => |args: &[Val], _vm: &mut VM| -> R {
            need("复制", args, 2)?;
            let src = as_text(&args[0]);
            let dst = as_text(&args[1]);
            fs::copy(&src, &dst)
                .map_err(|_| err("文件错误", format!("无法把「{src}」复制到「{dst}」")))?;
            Ok(Val::None_)
        },
        "文件大小" => |args: &[Val], _vm: &mut VM| -> R {
            need("文件大小", args, 1)?;
            let p = as_text(&args[0]);
            match fs::metadata(&p) {
                Ok(m) if m.is_file() => Ok(Val::Int(m.len() as i128)),
                _ => Err(err("文件错误", format!("找不到文件「{p}」，没法获取大小"))),
            }
        },
        "路径拼接" => |args: &[Val], _vm: &mut VM| -> R {
            // 对齐 Python 侧：用的是 `PurePosixPath`，**总是 `/`**
            if args.is_empty() {
                return Ok(Val::Str(".".to_string()));
            }
            let mut acc = as_text(&args[0]);
            for seg in &args[1..] {
                let seg = as_text(seg);
                if seg.starts_with('/') {
                    acc = seg;
                } else if acc.is_empty() {
                    acc = seg;
                } else if acc.ends_with('/') {
                    acc.push_str(&seg);
                } else {
                    acc.push('/');
                    acc.push_str(&seg);
                }
            }
            Ok(Val::Str(acc))
        },
        "文件名" => |args: &[Val], _vm: &mut VM| -> R {
            need("文件名", args, 1)?;
            Ok(Val::Str(file_name_of(&as_text(&args[0]))))
        },
        "扩展名" => |args: &[Val], _vm: &mut VM| -> R {
            need("扩展名", args, 1)?;
            Ok(Val::Str(suffix_of(&file_name_of(&as_text(&args[0])))))
        },
    }
}
