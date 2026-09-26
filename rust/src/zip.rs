//! 标准库 `压缩`（Python 侧 `jishi/stdlib/压缩.py`，3 个函数）。
//!
//! **zip 容器与 deflate 解压都自己写**（没有 `zip` / `flate2` crate）。
//!
//! ## 打包为什么是「不压缩」的
//!
//! 写 zip 需要 DEFLATE **压缩器**（LZ77 + 两套霍夫曼树），那是几百行的活；
//! 而 zip 规范里本来就有 `stored`（方法 0，原样存）这种合法形式。
//! 所以 `打包` 写的是 stored 条目：**文件大一点，但完全合规**，
//! Python 的 `zipfile` 读得出来（测试里就是拿它当裁判的）。
//!
//! ## 解压必须支持 deflate
//!
//! 别人打的包（Python 的 `zipfile` 默认 `ZIP_DEFLATED`、Node 的也是）
//! 全是压缩过的，所以**解压器躲不掉**：`inflate.rs` 里是完整的
//! DEFLATE 解码（stored / 固定霍夫曼 / 动态霍夫曼三种块都支持）。
//!
//! 与 Python 侧的行为差异只有一处、且不影响正确性：`打包` 出来的
//! **字节不同**（stored vs deflate），但**解出来的内容完全一样**。

use std::collections::HashMap;
use std::fs;
use std::path::Path;

use crate::inflate::inflate_raw;
use crate::stdlib::{as_text, need, need_range, table, R};
use crate::{err, JishiError, Val, VM};

// ---------------------------------------------------------------------------
// CRC-32
// ---------------------------------------------------------------------------

fn crc32(data: &[u8]) -> u32 {
    let mut crc: u32 = 0xFFFF_FFFF;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
        }
    }
    !crc
}

// ---------------------------------------------------------------------------
// 读 zip
// ---------------------------------------------------------------------------

struct Entry {
    name: String,
    method: u16,
    csize: usize,
    usize_: usize,
    crc: u32,
    local_off: usize,
}

fn u16at(b: &[u8], i: usize) -> u16 {
    u16::from_le_bytes([b[i], b[i + 1]])
}

fn u32at(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
}

/// 从中央目录读出条目表（Python 的 `namelist()` 就是这个顺序）。
fn read_entries(data: &[u8]) -> Result<Vec<Entry>, JishiError> {
    let bad = || err("运行期错误", "不是有效的 zip：找不到中央目录");
    if data.len() < 22 {
        return Err(bad());
    }
    // EOCD 在末尾，可能带着注释，所以从后往前找签名
    let mut eocd = None;
    let start = data.len().saturating_sub(22 + 65535);
    for i in (start..=data.len() - 22).rev() {
        if u32at(data, i) == 0x0605_4B50 {
            eocd = Some(i);
            break;
        }
    }
    let e = eocd.ok_or_else(bad)?;
    let total = u16at(data, e + 10) as usize;
    let cd_off = u32at(data, e + 16) as usize;
    let mut out = Vec::with_capacity(total);
    let mut p = cd_off;
    for _ in 0..total {
        if p + 46 > data.len() || u32at(data, p) != 0x0201_4B50 {
            return Err(bad());
        }
        let method = u16at(data, p + 10);
        let crc = u32at(data, p + 16);
        let csize = u32at(data, p + 20) as usize;
        let usize_ = u32at(data, p + 24) as usize;
        let nlen = u16at(data, p + 28) as usize;
        let elen = u16at(data, p + 30) as usize;
        let clen = u16at(data, p + 32) as usize;
        let local_off = u32at(data, p + 42) as usize;
        let name = String::from_utf8_lossy(&data[p + 46..p + 46 + nlen]).to_string();
        out.push(Entry { name, method, csize, usize_, crc, local_off });
        p += 46 + nlen + elen + clen;
    }
    Ok(out)
}

/// 从本地文件头拿到数据起点，并取出该条目的数据。
fn entry_data<'a>(data: &'a [u8], e: &Entry) -> Result<&'a [u8], JishiError> {
    let p = e.local_off;
    if p + 30 > data.len() || u32at(data, p) != 0x0403_4B50 {
        return Err(err("运行期错误", format!("不是有效的 zip：条目「{}」的本地头读不出来", e.name)));
    }
    let nlen = u16at(data, p + 26) as usize;
    let elen = u16at(data, p + 28) as usize;
    let start = p + 30 + nlen + elen;
    if start + e.csize > data.len() {
        return Err(err("运行期错误", format!("不是有效的 zip：条目「{}」的数据不完整", e.name)));
    }
    Ok(&data[start..start + e.csize])
}

/// 取出并（必要时）解压一个条目，顺手校验 CRC。
fn extract(data: &[u8], e: &Entry) -> Result<Vec<u8>, JishiError> {
    let raw = entry_data(data, e)?;
    let out = match e.method {
        0 => raw.to_vec(),
        8 => inflate_raw(raw).map_err(|m| {
            err("运行期错误", format!("解压「{}」失败：{m}", e.name))
        })?,
        m => {
            return Err(err(
                "运行期错误",
                format!("条目「{}」用了不支持的压缩方式（{m}）", e.name),
            ))
        }
    };
    if crc32(&out) != e.crc {
        return Err(err(
            "运行期错误",
            format!("条目「{}」的内容校验不过（CRC 不一致），压缩包可能坏了", e.name),
        ));
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// 写 zip（stored）
// ---------------------------------------------------------------------------

struct Out {
    buf: Vec<u8>,
    central: Vec<u8>,
    count: u16,
}

impl Out {
    fn new() -> Self {
        Out { buf: Vec::new(), central: Vec::new(), count: 0 }
    }

    fn push_u16(b: &mut Vec<u8>, v: u16) {
        b.extend_from_slice(&v.to_le_bytes());
    }
    fn push_u32(b: &mut Vec<u8>, v: u32) {
        b.extend_from_slice(&v.to_le_bytes());
    }

    /// 加一个 stored 条目。
    fn add(&mut self, arc_name: &str, data: &[u8]) {
        // zip 规范要求条目名用 `/`（Windows 的 `\` 要换掉）
        let name = arc_name.replace('\\', "/");
        let nb = name.as_bytes();
        let crc = crc32(data);
        let off = self.buf.len();

        let b = &mut self.buf;
        Self::push_u32(b, 0x0403_4B50);
        Self::push_u16(b, 20); // 解压所需版本
        Self::push_u16(b, 0x0800); // 通用标志：文件名是 UTF-8
        Self::push_u16(b, 0); // 方法 0 = stored
        Self::push_u16(b, 0); // 时间
        Self::push_u16(b, 0x21); // 日期（1980-01-01，与「不写时间」等价的占位）
        Self::push_u32(b, crc);
        Self::push_u32(b, data.len() as u32);
        Self::push_u32(b, data.len() as u32);
        Self::push_u16(b, nb.len() as u16);
        Self::push_u16(b, 0); // extra 长度
        b.extend_from_slice(nb);
        b.extend_from_slice(data);

        let c = &mut self.central;
        Self::push_u32(c, 0x0201_4B50);
        Self::push_u16(c, 20); // 制作版本
        Self::push_u16(c, 20); // 所需版本
        Self::push_u16(c, 0x0800);
        Self::push_u16(c, 0);
        Self::push_u16(c, 0);
        Self::push_u16(c, 0x21);
        Self::push_u32(c, crc);
        Self::push_u32(c, data.len() as u32);
        Self::push_u32(c, data.len() as u32);
        Self::push_u16(c, nb.len() as u16);
        Self::push_u16(c, 0);
        Self::push_u16(c, 0);
        Self::push_u16(c, 0);
        Self::push_u16(c, 0);
        Self::push_u32(c, 0);
        Self::push_u32(c, off as u32);
        c.extend_from_slice(nb);

        self.count += 1;
    }

    fn finish(mut self) -> Vec<u8> {
        let cd_off = self.buf.len();
        let cd_size = self.central.len();
        self.buf.extend_from_slice(&self.central);
        let b = &mut self.buf;
        Self::push_u32(b, 0x0605_4B50);
        Self::push_u16(b, 0);
        Self::push_u16(b, 0);
        Self::push_u16(b, self.count);
        Self::push_u16(b, self.count);
        Self::push_u32(b, cd_size as u32);
        Self::push_u32(b, cd_off as u32);
        Self::push_u16(b, 0);
        self.buf
    }
}

/// 递归收集一个目录下的文件：`(磁盘路径, 归档名)`。
///
/// 与 Python 侧一致：归档名是「相对被收录目录的**父目录**」的路径
/// （`os.path.relpath(full, os.path.dirname(p))`），所以收录 `build/子`
/// 得到的是 `子/文件`。
fn walk(dir: &str, prefix: &str, out: &mut Vec<(String, String)>) {
    let entries = match fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return,
    };
    let mut items: Vec<_> = entries.filter_map(|e| e.ok()).collect();
    items.sort_by_key(|e| e.file_name());
    for item in items {
        let full = format!("{dir}/{}", item.file_name().to_string_lossy());
        let arc = format!("{prefix}/{}", item.file_name().to_string_lossy());
        if item.path().is_dir() {
            walk(&full, &arc, out);
        } else {
            out.push((full, arc));
        }
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "压缩";
        "打包" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("打包", args, 2, usize::MAX)?;
            let zip_path = as_text(&args[0]);
            // 允许第一个参数是列表（基石调用常传列表）
            let items: Vec<Val> = if args.len() == 2 && matches!(args[1], Val::List(_)) {
                crate::stdlib::to_vec(&args[1])?
            } else {
                args[1..].to_vec()
            };
            if items.is_empty() {
                return Err(err("值错误", "打包至少需要提供一个文件或目录"));
            }
            let mut files: Vec<(String, String)> = Vec::new();
            for it in &items {
                let p = as_text(it);
                if Path::new(&p).is_dir() {
                    // 前缀是「被收录目录自己的名字」——等价于 Python 的
                    // `relpath(full, dirname(p))`
                    let base = p
                        .trim_end_matches(['/', '\\'])
                        .rsplit(['/', '\\'])
                        .next()
                        .unwrap_or("")
                        .to_string();
                    walk(&p, &base, &mut files);
                } else if Path::new(&p).is_file() {
                    let base = p
                        .rsplit(['/', '\\'])
                        .next()
                        .unwrap_or("")
                        .to_string();
                    files.push((p.clone(), base));
                } else {
                    return Err(err("值错误", format!("「{p}」不存在，无法打包")));
                }
            }
            let mut out = Out::new();
            for (full, arc) in &files {
                let data = fs::read(full).map_err(|e| {
                    err("运行期错误", format!("读「{full}」失败：{e}"))
                })?;
                out.add(arc, &data);
            }
            fs::write(&zip_path, out.finish())
                .map_err(|e| err("运行期错误", format!("打包到「{zip_path}」失败：{e}")))?;
            Ok(Val::Str(zip_path))
        },
        "解压" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("解压", args, 1, 2)?;
            let zip_path = as_text(&args[0]);
            let target = if args.len() > 1 && !matches!(args[1], Val::None_) {
                as_text(&args[1])
            } else {
                ".".to_string()
            };
            if !Path::new(&zip_path).is_file() {
                return Err(err("值错误", format!("「{zip_path}」不是文件")));
            }
            let data = fs::read(&zip_path)
                .map_err(|e| err("运行期错误", format!("读「{zip_path}」失败：{e}")))?;
            let entries = read_entries(&data)?;
            fs::create_dir_all(&target)
                .map_err(|e| err("运行期错误", format!("创建目录「{target}」失败：{e}")))?;
            for e in &entries {
                // 目录条目（以 / 结尾）只建目录
                let dest = format!("{target}/{}", e.name);
                if e.name.ends_with('/') {
                    fs::create_dir_all(dest.trim_end_matches('/'))
                        .map_err(|x| err("运行期错误", format!("创建目录失败：{x}")))?;
                    continue;
                }
                if let Some(parent) = Path::new(&dest).parent() {
                    let _ = fs::create_dir_all(parent);
                }
                let content = extract(&data, e)?;
                fs::write(&dest, &content)
                    .map_err(|x| err("运行期错误", format!("写「{dest}」失败：{x}")))?;
            }
            Ok(Val::Str(target))
        },
        "列出内容" => |args: &[Val], _vm: &mut VM| -> R {
            need("列出内容", args, 1)?;
            let zip_path = as_text(&args[0]);
            if !Path::new(&zip_path).is_file() {
                return Err(err("值错误", format!("「{zip_path}」不是文件")));
            }
            let data = fs::read(&zip_path)
                .map_err(|e| err("运行期错误", format!("读「{zip_path}」失败：{e}")))?;
            let entries = read_entries(&data)?;
            Ok(crate::list_new(
                entries.into_iter().map(|e| Val::Str(e.name)).collect(),
            ))
        },
    }
}
