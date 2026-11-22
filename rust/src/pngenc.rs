//! PNG 编码（M207）：**零依赖**，把 RGBA 像素写成合法的 PNG。
//!
//! ## 为什么需要它
//!
//! `图形.截图()` 要给出「能被解码器读回来」的字节。两条路：
//!
//! 1. 页面里 `canvas.toDataURL("image/png")` —— 但那是**宿主**（浏览器）的编码器，
//!    命令行/判据里没有，而且我们要能在任何宿主上验证；
//! 2. 自己编 —— 于是有了这份。
//!
//! ## 为什么可以「不压缩」
//!
//! DEFLATE 的 **stored** 块（`BFINAL|BTYPE=00`）是**合法**的 deflate：不压缩、但有
//! 块头。PNG 只要求 `IDAT` 是 zlib 流，并不要求它真的压过。我们没有压缩器
//! （只有 `inflate`，见 `docs/embed.md`），用 stored 块就能写出**任何解码器都能读**
//! 的 PNG —— 文件大一些（约 4/3 的原始像素），换来的是「不用为了截图去写一个 LZ77」。
//!
//! 📌 与 `zip` 里的选择同一条思路：**压缩可以不做，解压必须做**（别人给的文件
//! 一定是压过的）。

/// 一张 RGBA8 图 → PNG 字节。
///
/// `像素` 长度必须是 `宽 * 高 * 4`（逐行 R,G,B,A）。长度不对返回 `None` ——
/// 这种错是**调用方**算错了尺寸，不该在编码器里悄悄裁剪。
pub(crate) fn 编码_rgba(宽: u32, 高: u32, 像素: &[u8]) -> Option<Vec<u8>> {
    let 需要 = (宽 as usize).checked_mul(高 as usize)?.checked_mul(4)?;
    if 像素.len() != 需要 || 宽 == 0 || 高 == 0 {
        return None;
    }

    // ---- 每行前面加一个过滤器字节（0 = None，最省事也最快）----
    let 行字节 = 宽 as usize * 4;
    let mut 原始 = Vec::with_capacity((行字节 + 1) * 高 as usize);
    for y in 0..高 as usize {
        原始.push(0);
        原始.extend_from_slice(&像素[y * 行字节..(y + 1) * 行字节]);
    }

    let mut 出 = Vec::with_capacity(原始.len() + 原始.len() / 256 + 128);
    出.extend_from_slice(&[0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A]);

    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&宽.to_be_bytes());
    ihdr.extend_from_slice(&高.to_be_bytes());
    ihdr.push(8); // 位深
    ihdr.push(6); // 色彩类型 6 = RGBA
    ihdr.push(0); // 压缩方法
    ihdr.push(0); // 过滤方法
    ihdr.push(0); // 不隔行
    块(&mut 出, b"IHDR", &ihdr);
    块(&mut 出, b"IDAT", &zlib存块(&原始));
    块(&mut 出, b"IEND", &[]);
    Some(出)
}

fn 块(出: &mut Vec<u8>, 类: &[u8; 4], 体: &[u8]) {
    出.extend_from_slice(&(体.len() as u32).to_be_bytes());
    出.extend_from_slice(类);
    出.extend_from_slice(体);
    let mut 校验 = Vec::with_capacity(4 + 体.len());
    校验.extend_from_slice(类);
    校验.extend_from_slice(体);
    出.extend_from_slice(&crate::zip::crc32(&校验).to_be_bytes());
}

/// zlib 包装 + 全 stored 块的 deflate。
fn zlib存块(数据: &[u8]) -> Vec<u8> {
    let mut 出 = Vec::with_capacity(数据.len() + 数据.len() / 65535 * 5 + 16);
    // CMF/FLG：(0x78, 0x01) —— CM=8(deflate)、CINFO=7(32K 窗口)、
    // 校验位让 (0x78<<8 | 0x01) % 31 == 0（zlib 的合法头）
    出.push(0x78);
    出.push(0x01);
    if 数据.is_empty() {
        出.push(0x01); // BFINAL=1, BTYPE=00
        出.extend_from_slice(&[0x00, 0x00, 0xFF, 0xFF]);
    } else {
        let mut i = 0;
        while i < 数据.len() {
            let 长 = (数据.len() - i).min(65535);
            let 末 = i + 长 == 数据.len();
            出.push(if 末 { 0x01 } else { 0x00 });
            出.extend_from_slice(&(长 as u16).to_le_bytes());
            出.extend_from_slice(&(!(长 as u16)).to_le_bytes());
            出.extend_from_slice(&数据[i..i + 长]);
            i += 长;
        }
    }
    出.extend_from_slice(&adler32(数据).to_be_bytes());
    出
}

fn adler32(数据: &[u8]) -> u32 {
    const 模: u32 = 65521;
    let (mut a, mut b) = (1u32, 0u32);
    for 段 in 数据.chunks(5552) {
        for &x in 段 {
            a += x as u32;
            b += a;
        }
        a %= 模;
        b %= 模;
    }
    (b << 16) | a
}
