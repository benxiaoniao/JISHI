//! DEFLATE 解压（RFC 1951），零依赖手写。
//!
//! 为什么非得有它：`压缩.解压` 要能读**别人打的** zip —— Python 的
//! `zipfile` 默认用 `ZIP_DEFLATED`、Node 的也是，所以「只支持 stored」
//! 等于解压功能没法用。压缩（写）那边可以用 stored 绕过去，解压这边绕不过。
//!
//! 三种块都支持：
//!
//! - `00` 原样存（stored）
//! - `01` 固定霍夫曼
//! - `10` 动态霍夫曼（**绝大多数真实文件都是这种**）
//!
//! 解码用的是 zlib `puff` 那种「按码长计数 + 符号表」的经典做法：
//! 不用建树，按位逐层缩小范围即可。

/// 位读取器（DEFLATE 是**低位在前**的位序）。
struct Bits<'a> {
    data: &'a [u8],
    pos: usize,
    buf: u64,
    n: u32,
}

impl<'a> Bits<'a> {
    fn new(data: &'a [u8]) -> Self {
        Bits { data, pos: 0, buf: 0, n: 0 }
    }

    fn need(&mut self, k: u32) -> Result<(), String> {
        while self.n < k {
            if self.pos >= self.data.len() {
                return Err("数据提前结束了".to_string());
            }
            self.buf |= (self.data[self.pos] as u64) << self.n;
            self.pos += 1;
            self.n += 8;
        }
        Ok(())
    }

    fn bits(&mut self, k: u32) -> Result<u32, String> {
        if k == 0 {
            return Ok(0);
        }
        self.need(k)?;
        let v = (self.buf & ((1u64 << k) - 1)) as u32;
        self.buf >>= k;
        self.n -= k;
        Ok(v)
    }

    /// 清掉当前字节里剩下的位（stored 块用）。
    fn align(&mut self) {
        let drop = self.n % 8;
        self.buf >>= drop;
        self.n -= drop;
    }
}

/// 规范霍夫曼表（按码长计数 + 按码排序的符号表）。
struct Huff {
    count: [u16; 16],
    symbol: Vec<u16>,
}

impl Huff {
    fn build(lengths: &[u8]) -> Result<Huff, String> {
        let mut count = [0u16; 16];
        for &l in lengths {
            count[l as usize] += 1;
        }
        if count[0] as usize == lengths.len() {
            // 全零：没有任何符号，解码时必然报错，这里先放行
            return Ok(Huff { count, symbol: Vec::new() });
        }
        count[0] = 0;
        // 校验码表没有过订阅
        let mut left: i32 = 1;
        for len in 1..16 {
            left <<= 1;
            left -= count[len] as i32;
            if left < 0 {
                return Err("霍夫曼码表不合法（过订阅）".to_string());
            }
        }
        let mut offs = [0u16; 16];
        for len in 1..15 {
            offs[len + 1] = offs[len] + count[len];
        }
        let mut symbol = vec![0u16; lengths.len()];
        for (sym, &l) in lengths.iter().enumerate() {
            if l != 0 {
                symbol[offs[l as usize] as usize] = sym as u16;
                offs[l as usize] += 1;
            }
        }
        Ok(Huff { count, symbol })
    }

    fn decode(&self, b: &mut Bits) -> Result<u16, String> {
        let mut code: i32 = 0;
        let mut first: i32 = 0;
        let mut index: i32 = 0;
        for len in 1..16 {
            code |= b.bits(1)? as i32;
            let cnt = self.count[len] as i32;
            if code - cnt < first {
                let at = (index + (code - first)) as usize;
                return self
                    .symbol
                    .get(at)
                    .copied()
                    .ok_or_else(|| "霍夫曼码表解不开".to_string());
            }
            index += cnt;
            first += cnt;
            first <<= 1;
            code <<= 1;
        }
        Err("霍夫曼码不合法".to_string())
    }
}

const LEN_BASE: [u16; 29] = [
    3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131,
    163, 195, 227, 258,
];
const LEN_EXTRA: [u8; 29] = [
    0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0,
];
const DIST_BASE: [u16; 30] = [
    1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537,
    2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577,
];
const DIST_EXTRA: [u8; 30] = [
    0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13,
    13,
];

fn fixed_tables() -> (Huff, Huff) {
    let mut lit = vec![0u8; 288];
    for (i, l) in lit.iter_mut().enumerate() {
        *l = if i < 144 {
            8
        } else if i < 256 {
            9
        } else if i < 280 {
            7
        } else {
            8
        };
    }
    let dist = vec![5u8; 30];
    (
        Huff::build(&lit).expect("固定码表一定合法"),
        Huff::build(&dist).expect("固定码表一定合法"),
    )
}

fn codes(b: &mut Bits, lit: &Huff, dist: &Huff, out: &mut Vec<u8>) -> Result<(), String> {
    loop {
        let sym = lit.decode(b)?;
        if sym < 256 {
            out.push(sym as u8);
            continue;
        }
        if sym == 256 {
            return Ok(());
        }
        let li = sym as usize - 257;
        if li >= 29 {
            return Err("长度码越界".to_string());
        }
        let len = LEN_BASE[li] as usize + b.bits(LEN_EXTRA[li] as u32)? as usize;
        let ds = dist.decode(b)? as usize;
        if ds >= 30 {
            return Err("距离码越界".to_string());
        }
        let d = DIST_BASE[ds] as usize + b.bits(DIST_EXTRA[ds] as u32)? as usize;
        if d > out.len() {
            return Err("回溯距离超过了已有数据".to_string());
        }
        // 逐字节复制：`d` 可能小于 `len`（自重叠），不能用切片一次性拷
        let start = out.len() - d;
        for k in 0..len {
            let byte = out[start + k];
            out.push(byte);
        }
    }
}

/// 动态霍夫曼：先读码长表，再按 3 位一组展开。
fn dynamic(b: &mut Bits, out: &mut Vec<u8>) -> Result<(), String> {
    const ORDER: [usize; 19] = [
        16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15,
    ];
    let hlit = b.bits(5)? as usize + 257;
    let hdist = b.bits(5)? as usize + 1;
    let hclen = b.bits(4)? as usize + 4;
    let mut clens = [0u8; 19];
    for i in 0..hclen {
        clens[ORDER[i]] = b.bits(3)? as u8;
    }
    let clh = Huff::build(&clens)?;

    let total = hlit + hdist;
    let mut lengths = vec![0u8; total];
    let mut i = 0;
    while i < total {
        let sym = clh.decode(b)?;
        match sym {
            0..=15 => {
                lengths[i] = sym as u8;
                i += 1;
            }
            16 => {
                if i == 0 {
                    return Err("码长重复没有可重复的对象".to_string());
                }
                let prev = lengths[i - 1];
                let n = 3 + b.bits(2)? as usize;
                for _ in 0..n {
                    if i >= total {
                        return Err("码长表越界".to_string());
                    }
                    lengths[i] = prev;
                    i += 1;
                }
            }
            17 => {
                let n = 3 + b.bits(3)? as usize;
                i += n;
            }
            18 => {
                let n = 11 + b.bits(7)? as usize;
                i += n;
            }
            _ => return Err("码长码非法".to_string()),
        }
        if i > total {
            return Err("码长表越界".to_string());
        }
    }
    let lit = Huff::build(&lengths[..hlit])?;
    let dist = Huff::build(&lengths[hlit..])?;
    codes(b, &lit, &dist, out)
}

/// 解压一段**裸** deflate 数据（没有 zlib 的两字节头）。
pub(crate) fn inflate_raw(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut b = Bits::new(data);
    let mut out: Vec<u8> = Vec::new();
    loop {
        let last = b.bits(1)?;
        let kind = b.bits(2)?;
        match kind {
            0 => {
                b.align();
                let len = b.bits(16)? as usize;
                let _nlen = b.bits(16)?;
                for _ in 0..len {
                    out.push(b.bits(8)? as u8);
                }
            }
            1 => {
                let (lit, dist) = fixed_tables();
                codes(&mut b, &lit, &dist, &mut out)?;
            }
            2 => dynamic(&mut b, &mut out)?,
            _ => return Err("压缩块类型 3 是保留值".to_string()),
        }
        if last == 1 {
            return Ok(out);
        }
    }
}
