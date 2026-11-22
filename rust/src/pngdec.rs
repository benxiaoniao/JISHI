//! PNG 解码（M205）：**零依赖手写**，复用 `inflate` 的 DEFLATE。
//!
//! ## 为什么自己写
//!
//! `png` crate 在本项目只用在**两处**：托盘图标（`读图标`）与窗口图标
//! （`窗口.设图标`）。为两张小图背一个 crate + 它的依赖树不划算 ——
//! 而这个格式真要用到的能力很小：读 `IHDR` / `PLTE` / `tRNS` / `IDAT`、
//! 五种行过滤器、八种位深/色彩组合、隔行（Adam7）。DEFLATE 我们本来就有
//! （`压缩.解压` 用的同一份 `inflate`），剩下的只是拼字节。
//!
//! ## 支持面（与判据一一对应）
//!
//! | 维度 | 支持 |
//! |---|---|
//! | 色彩类型 | 0 灰度 · 2 RGB · 3 调色板 · 4 灰度+α · 6 RGBA |
//! | 位深 | 灰度 1/2/4/8/16 · 调色板 1/2/4/8 · 其余 8/16 |
//! | 过滤器 | 0 None · 1 Sub · 2 Up · 3 Average · 4 Paeth |
//! | 隔行 | 0 不隔行 · 1 Adam7 |
//! | 透明度 | 调色板 `tRNS` 逐项 · 灰度/RGB 的 `tRNS` 单色 |
//!
//! **输出统一是 8 位 RGBA**（16 位取高字节，与 `png` crate 的 `STRIP_16` 同口径）。
//!
//! ## 不支持就报中文错，绝不静默画错
//!
//! 这一条是本项目的老规矩：位深/色彩组合不合法、调色板缺 `PLTE`、索引越界、
//! CRC 不对、被截断 —— 每种都有自己的话，因为「图标怎么设不上」是作者真会问的问题。

use crate::inflate::inflate_raw_至多;
use crate::zip::crc32;

/// 解码后的图像（8 位 RGBA，逐行紧排）。
pub struct 图像 {
    pub 宽: u32,
    pub 高: u32,
    /// 长度 = `宽 * 高 * 4`，顺序是 R,G,B,A。
    pub rgba: Vec<u8>,
}

const 签名: [u8; 8] = [0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];

/// Adam7 的七趟：(起始 x, 起始 y, x 步长, y 步长)。
const ADAM7: [(u32, u32, u32, u32); 7] = [
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
];

struct 头 {
    宽: u32,
    高: u32,
    位深: u8,
    色彩: u8,
    隔行: bool,
}

fn be32(b: &[u8], p: usize) -> u32 {
    ((b[p] as u32) << 24) | ((b[p + 1] as u32) << 16) | ((b[p + 2] as u32) << 8) | b[p + 3] as u32
}

fn 通道数(色彩: u8) -> usize {
    match 色彩 {
        0 => 1,
        2 => 3,
        3 => 1,
        4 => 2,
        6 => 4,
        _ => 0,
    }
}

fn 色彩名(色彩: u8) -> &'static str {
    match 色彩 {
        0 => "灰度",
        2 => "RGB",
        3 => "调色板",
        4 => "灰度+透明",
        6 => "RGBA",
        _ => "未知",
    }
}

fn 位深允许(色彩: u8, 位深: u8) -> bool {
    match 色彩 {
        0 => matches!(位深, 1 | 2 | 4 | 8 | 16),
        3 => matches!(位深, 1 | 2 | 4 | 8),
        2 | 4 | 6 => matches!(位深, 8 | 16),
        _ => false,
    }
}

/// 解一张 PNG。`标签` 是给报错用的（一般就是文件路径）。
pub fn 解码(数据: &[u8], 标签: &str) -> Result<图像, String> {
    if 数据.len() < 8 || 数据[..8] != 签名 {
        return Err(format!("「{标签}」不是 PNG（文件开头的 8 个字节不对）"));
    }

    let mut 头们: Option<头> = None;
    let mut 调色板: Vec<u8> = Vec::new();
    let mut 透明: Option<Vec<u8>> = None;
    let mut 压缩数据: Vec<u8> = Vec::new();
    let mut 见过图像数据 = false;
    let mut 见过结尾 = false;

    let mut p = 8usize;
    while p + 8 <= 数据.len() {
        let 长 = be32(数据, p) as usize;
        let 类: &[u8] = &数据[p + 4..p + 8];
        let 起 = p + 8;
        let 止 = match 起.checked_add(长) {
            Some(v) => v,
            None => return Err(format!("「{标签}」的块长度不合法（{长} 字节）")),
        };
        if 止 + 4 > 数据.len() {
            return Err(format!(
                "「{标签}」被截断了（块 {} 说它有 {长} 字节，文件里不够）",
                String::from_utf8_lossy(类)
            ));
        }
        let 体 = &数据[起..止];
        let 期望 = be32(数据, 止);
        // CRC 覆盖「块类型 + 块内容」
        let 实得 = {
            let mut 拼 = Vec::with_capacity(4 + 长);
            拼.extend_from_slice(类);
            拼.extend_from_slice(体);
            crc32(&拼)
        };
        if 实得 != 期望 {
            return Err(format!(
                "「{标签}」的 {} 块校验不过（CRC 不一致，文件可能坏了）",
                String::from_utf8_lossy(类)
            ));
        }

        match 类 {
            b"IHDR" => {
                if 头们.is_some() {
                    return Err(format!("「{标签}」有两个 IHDR 块（不合规）"));
                }
                if 长 != 13 {
                    return Err(format!("「{标签}」的 IHDR 长度是 {长}，应当是 13"));
                }
                let 宽 = be32(体, 0);
                let 高 = be32(体, 4);
                let 位深 = 体[8];
                let 色彩 = 体[9];
                let 压缩 = 体[10];
                let 过滤 = 体[11];
                let 隔行 = 体[12];
                if 宽 == 0 || 高 == 0 {
                    return Err(format!("「{标签}」的尺寸是 {宽}×{高} —— 宽高不能是 0"));
                }
                // 防「像素数爆炸」：先按像素数与输出字节数把住，再谈解码
                let 像素 = (宽 as u64) * (高 as u64);
                if 像素 > 100_000_000 {
                    return Err(format!(
                        "「{标签}」是 {宽}×{高}（{像素} 像素）—— 太大了，图标不需要这么大"
                    ));
                }
                if 通道数(色彩) == 0 {
                    return Err(format!("「{标签}」的色彩类型 {色彩} 不认识（PNG 只有 0/2/3/4/6）"));
                }
                if !位深允许(色彩, 位深) {
                    return Err(format!(
                        "「{标签}」是「{}」色彩、{位深} 位深 —— 这个组合 PNG 不允许",
                        色彩名(色彩)
                    ));
                }
                if 压缩 != 0 {
                    return Err(format!("「{标签}」用了压缩方式 {压缩}（PNG 只定义了 0）"));
                }
                if 过滤 != 0 {
                    return Err(format!("「{标签}」的行过滤方式是 {过滤}（PNG 只定义了 0）"));
                }
                if 隔行 > 1 {
                    return Err(format!("「{标签}」的隔行方式是 {隔行}（PNG 只有 0 和 1）"));
                }
                头们 = Some(头 { 宽, 高, 位深, 色彩, 隔行: 隔行 == 1 });
            }
            b"PLTE" => {
                if 长 == 0 || 长 % 3 != 0 {
                    return Err(format!("「{标签}」的调色板长度是 {长}，应当是 3 的倍数"));
                }
                if 长 / 3 > 256 {
                    return Err(format!("「{标签}」的调色板有 {} 项（最多 256）", 长 / 3));
                }
                调色板 = 体.to_vec();
            }
            b"tRNS" => 透明 = Some(体.to_vec()),
            b"IDAT" => {
                见过图像数据 = true;
                压缩数据.extend_from_slice(体);
            }
            b"IEND" => {
                见过结尾 = true;
                break;
            }
            _ => {
                // 首字母大写 = **关键块**，不认识就必须报错（规范要求）；
                // 小写是辅助块（tEXt / gAMA / pHYs…），跳过即可。
                if 类[0].is_ascii_uppercase() {
                    return Err(format!(
                        "「{标签}」里有不认识的必需块「{}」—— 这个解码器不支持它",
                        String::from_utf8_lossy(类)
                    ));
                }
            }
        }
        p = 止 + 4;
    }

    let 头 = 头们.ok_or_else(|| format!("「{标签}」没有 IHDR 块，不是完整的 PNG"))?;
    if !见过结尾 {
        return Err(format!("「{标签}」没有 IEND 块（文件不完整）"));
    }
    if !见过图像数据 {
        return Err(format!("「{标签}」没有 IDAT 块（没有像素数据）"));
    }
    if 头.色彩 == 3 && 调色板.is_empty() {
        return Err(format!("「{标签}」是调色板图片，但没有 PLTE 块"));
    }

    // ---- 解压：zlib 头（2 字节）+ deflate + Adler32（4 字节） ----
    if 压缩数据.len() < 6 {
        return Err(format!("「{标签}」的 IDAT 太短（不到 6 字节）"));
    }
    let cmf = 压缩数据[0];
    let flg = 压缩数据[1];
    if cmf & 0x0F != 8 {
        return Err(format!(
            "「{标签}」的 IDAT 不是 zlib（压缩方法 {}，只认 8 = deflate）",
            cmf & 0x0F
        ));
    }
    if ((cmf as u16) << 8 | flg as u16) % 31 != 0 {
        return Err(format!("「{标签}」的 zlib 头校验不过（CMF/FLG 不合法）"));
    }
    if flg & 0x20 != 0 {
        return Err(format!("「{标签}」的 zlib 用了预设字典 —— 不支持"));
    }

    // ---- 按 IHDR 算「解压后应当有多少字节」，顺手当解压上限 ----
    let 每像素位 = 通道数(头.色彩) * 头.位深 as usize;
    let 行字节 = |宽: u32| (宽 as usize * 每像素位 + 7) / 8;
    let 趟们: &[(u32, u32, u32, u32)] = if 头.隔行 { &ADAM7 } else { &[(0, 0, 1, 1)] };
    let mut 需要 = 0usize;
    for &(x0, y0, dx, dy) in 趟们 {
        let 过宽 = if 头.宽 > x0 { (头.宽 - x0).div_ceil(dx) } else { 0 };
        let 过高 = if 头.高 > y0 { (头.高 - y0).div_ceil(dy) } else { 0 };
        if 过宽 > 0 && 过高 > 0 {
            需要 += (行字节(过宽) + 1) * 过高 as usize;
        }
    }
    let 原 = inflate_raw_至多(&压缩数据[2..], 需要)
        .map_err(|m| format!("「{标签}」的像素数据解不开：{m}"))?;
    if 原.len() != 需要 {
        return Err(format!(
            "「{标签}」的像素数据长度不对（解出 {} 字节，按 IHDR 应当 {} 字节）",
            原.len(),
            需要
        ));
    }

    // ---- 去过滤 + 转 RGBA ----
    let 输出字节 = (头.宽 as usize) * (头.高 as usize) * 4;
    let mut rgba = vec![0u8; 输出字节];
    let bpp = (每像素位 + 7) / 8; // 过滤器按**字节**算左邻，最少 1
    let mut 位 = 0usize;
    for &(x0, y0, dx, dy) in 趟们 {
        let 过宽 = if 头.宽 > x0 { (头.宽 - x0).div_ceil(dx) } else { 0 };
        let 过高 = if 头.高 > y0 { (头.高 - y0).div_ceil(dy) } else { 0 };
        if 过宽 == 0 || 过高 == 0 {
            continue;
        }
        let 每行 = 行字节(过宽);
        let mut 上一行 = vec![0u8; 每行];
        for j in 0..过高 {
            let 过滤 = 原[位];
            位 += 1;
            let mut 行 = 原[位..位 + 每行].to_vec();
            位 += 每行;
            去过滤(过滤, &mut 行, &上一行, bpp, 标签)?;
            let y = y0 + j * dy;
            for i in 0..过宽 as usize {
                let x = x0 + i as u32 * dx;
                let (r, g, b, a) = 取像素(&行, i, &头, &调色板, 透明.as_deref(), 标签)?;
                let o = ((y as usize) * (头.宽 as usize) + x as usize) * 4;
                rgba[o] = r;
                rgba[o + 1] = g;
                rgba[o + 2] = b;
                rgba[o + 3] = a;
            }
            上一行.copy_from_slice(&行);
        }
    }

    Ok(图像 { 宽: 头.宽, 高: 头.高, rgba })
}

/// 五种行过滤器（PNG 规范 9.2）。`上一行` 第一行时是全 0。
fn 去过滤(型: u8, 行: &mut [u8], 上一行: &[u8], bpp: usize, 标签: &str) -> Result<(), String> {
    match 型 {
        0 => {}
        1 => {
            for i in bpp..行.len() {
                行[i] = 行[i].wrapping_add(行[i - bpp]);
            }
        }
        2 => {
            for i in 0..行.len() {
                行[i] = 行[i].wrapping_add(上一行[i]);
            }
        }
        3 => {
            for i in 0..行.len() {
                let 左 = if i >= bpp { 行[i - bpp] as u16 } else { 0 };
                let 上 = 上一行[i] as u16;
                行[i] = 行[i].wrapping_add(((左 + 上) / 2) as u8);
            }
        }
        4 => {
            for i in 0..行.len() {
                let 左 = if i >= bpp { 行[i - bpp] as i16 } else { 0 };
                let 上 = 上一行[i] as i16;
                let 左上 = if i >= bpp { 上一行[i - bpp] as i16 } else { 0 };
                行[i] = 行[i].wrapping_add(paeth(左, 上, 左上) as u8);
            }
        }
        _ => return Err(format!("「{标签}」里有第 {型} 种行过滤器（PNG 只有 0–4）")),
    }
    Ok(())
}

fn paeth(a: i16, b: i16, c: i16) -> i16 {
    let p = a + b - c;
    let (pa, pb, pc) = ((p - a).abs(), (p - b).abs(), (p - c).abs());
    if pa <= pb && pa <= pc {
        a
    } else if pb <= pc {
        b
    } else {
        c
    }
}

/// 取一行里的第 `i` 个**样本**（位深 1/2/4 是位打包的）。
fn 取样本(行: &[u8], i: usize, 位深: u8) -> u32 {
    match 位深 {
        8 => 行[i] as u32,
        16 => ((行[i * 2] as u32) << 8) | 行[i * 2 + 1] as u32,
        d => {
            let 位 = i * d as usize;
            let 字节 = 行[位 / 8];
            let 移 = 8 - d as usize - (位 % 8);
            ((字节 as u32) >> 移) & ((1u32 << d) - 1)
        }
    }
}

/// 把一个样本压成 8 位：16 位取高字节；1/2/4 位按最大值等比放大。
fn 样本到8(v: u32, 位深: u8) -> u8 {
    match 位深 {
        16 => (v >> 8) as u8,
        8 => v as u8,
        d => {
            let 最大 = (1u32 << d) - 1;
            ((v * 255 + 最大 / 2) / 最大) as u8
        }
    }
}

/// 一行里的第 `i` 个像素 → (R,G,B,A)。
fn 取像素(
    行: &[u8],
    i: usize,
    头: &头,
    调色板: &[u8],
    透明: Option<&[u8]>,
    标签: &str,
) -> Result<(u8, u8, u8, u8), String> {
    let 位深 = 头.位深;
    match 头.色彩 {
        0 => {
            let v = 取样本(行, i, 位深);
            let g = 样本到8(v, 位深);
            let a = match 透明 {
                Some(t) if t.len() >= 2 && (((t[0] as u32) << 8) | t[1] as u32) == v => 0,
                _ => 255,
            };
            Ok((g, g, g, a))
        }
        2 => {
            let r = 取样本(行, i * 3, 位深);
            let g = 取样本(行, i * 3 + 1, 位深);
            let b = 取样本(行, i * 3 + 2, 位深);
            let a = match 透明 {
                Some(t) if t.len() >= 6
                    && (((t[0] as u32) << 8) | t[1] as u32) == r
                    && (((t[2] as u32) << 8) | t[3] as u32) == g
                    && (((t[4] as u32) << 8) | t[5] as u32) == b =>
                {
                    0
                }
                _ => 255,
            };
            Ok((样本到8(r, 位深), 样本到8(g, 位深), 样本到8(b, 位深), a))
        }
        3 => {
            let 索引 = 取样本(行, i, 位深) as usize;
            let o = 索引 * 3;
            if o + 2 >= 调色板.len() {
                return Err(format!(
                    "「{标签}」里有个像素的调色板索引是 {索引}，可调色板只有 {} 项",
                    调色板.len() / 3
                ));
            }
            let a = 透明
                .and_then(|t| t.get(索引).copied())
                .unwrap_or(255);
            Ok((调色板[o], 调色板[o + 1], 调色板[o + 2], a))
        }
        4 => {
            let g = 取样本(行, i * 2, 位深);
            let a = 取样本(行, i * 2 + 1, 位深);
            let g8 = 样本到8(g, 位深);
            Ok((g8, g8, g8, 样本到8(a, 位深)))
        }
        _ => {
            let r = 取样本(行, i * 4, 位深);
            let g = 取样本(行, i * 4 + 1, 位深);
            let b = 取样本(行, i * 4 + 2, 位深);
            let a = 取样本(行, i * 4 + 3, 位深);
            Ok((样本到8(r, 位深), 样本到8(g, 位深), 样本到8(b, 位深), 样本到8(a, 位深)))
        }
    }
}

/// 解一张 PNG 并给出 **RGBA 的 SHA-256**（`jishi --png信息` 与判据用）。
///
/// 为什么给摘要而不是把像素打出来：判据要的是「逐字节一致」，
/// 而 13×9 的 RGBA 打成十六进制也有一千多个字符 —— 摘要一行就够，
/// 对不上时还能直接和 PIL 算的那份比。
pub fn 解码摘要(数据: &[u8], 标签: &str) -> Result<(u32, u32, String), String> {
    let 图 = 解码(数据, 标签)?;
    Ok((图.宽, 图.高, crate::crypto::sha256_hex(&图.rgba)))
}
