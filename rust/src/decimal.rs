//! 精确十进制小数（M29）——**零依赖**实现。
//!
//! 与 Python 侧 `decimal.Decimal`（默认 28 位有效数字、ROUND_HALF_EVEN）
//! 和 JS 侧 `JishiDecimal` 对齐：
//!
//! - 值 = 系数 × 10^exp，系数是任意精度整数（避免 `i128` 在
//!   「28 位 × 28 位」时溢出：那需要 56 位有效数字）；
//! - 加减乘算得准；除法算到 28 位有效数字再按 half-even 收尾，
//!   并去掉尾随零（`10/4` 是 `2.5` 而不是 `2.5000…`）；
//! - 刻意**不**统一去尾零：`精确("1.50")` 显示就是 `1.50`、
//!   `精确("1.50") * 2` 是 `3.00`（Python 的 Decimal 也保留标度）。
//!
//! 为什么不用现成的 crate：`rust/Cargo.toml` 的定位是零第三方依赖
//! （见 docs/embed.md 的支持程度表）。手写 BigInt 的代价换的是
//! 「拿到源码 + 一个标准工具链就能跑起来」。

use std::cmp::Ordering;

/// 进制（每个肢体存 0..10^9-1，十进制好读好打印）。
const BASE: u64 = 1_000_000_000;
/// 每个肢体的十进制位数
const LIMB_DIGITS: usize = 9;
/// 有效数字位数（与 Python `decimal` 的默认上下文一致）
pub const PRECISION: usize = 28;

// ---------------------------------------------------------------------------
// 大整数
// ---------------------------------------------------------------------------

/// 任意精度整数：符号 + 绝对值（小端、base 10^9、无前导零）。
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct BigInt {
    neg: bool,
    mag: Vec<u32>,
}

fn trim(v: &mut Vec<u32>) {
    while let Some(&last) = v.last() {
        if last == 0 {
            v.pop();
        } else {
            break;
        }
    }
}

fn cmp_mag(a: &[u32], b: &[u32]) -> Ordering {
    if a.len() != b.len() {
        return a.len().cmp(&b.len());
    }
    for i in (0..a.len()).rev() {
        if a[i] != b[i] {
            return a[i].cmp(&b[i]);
        }
    }
    Ordering::Equal
}

fn add_mag(a: &[u32], b: &[u32]) -> Vec<u32> {
    let (long, short) = if a.len() >= b.len() { (a, b) } else { (b, a) };
    let mut out: Vec<u32> = Vec::with_capacity(long.len() + 1);
    let mut carry: u64 = 0;
    for i in 0..long.len() {
        let s = long[i] as u64 + short.get(i).copied().unwrap_or(0) as u64 + carry;
        out.push((s % BASE) as u32);
        carry = s / BASE;
    }
    if carry > 0 {
        out.push(carry as u32);
    }
    out
}

/// a - b，要求 a >= b（绝对值）。
fn sub_mag(a: &[u32], b: &[u32]) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::with_capacity(a.len());
    let mut borrow: i64 = 0;
    for i in 0..a.len() {
        let mut d = a[i] as i64 - b.get(i).copied().unwrap_or(0) as i64 - borrow;
        if d < 0 {
            d += BASE as i64;
            borrow = 1;
        } else {
            borrow = 0;
        }
        out.push(d as u32);
    }
    trim(&mut out);
    out
}

/// 绝对值 × 一个小整数（k < BASE）。
fn mul_small_mag(a: &[u32], k: u32) -> Vec<u32> {
    if k == 0 || a.is_empty() {
        return Vec::new();
    }
    let mut out: Vec<u32> = Vec::with_capacity(a.len() + 1);
    let mut carry: u64 = 0;
    for &limb in a {
        let p = limb as u64 * k as u64 + carry;
        out.push((p % BASE) as u32);
        carry = p / BASE;
    }
    while carry > 0 {
        out.push((carry % BASE) as u32);
        carry /= BASE;
    }
    out
}

fn mul_mag(a: &[u32], b: &[u32]) -> Vec<u32> {
    if a.is_empty() || b.is_empty() {
        return Vec::new();
    }
    let mut out = vec![0u64; a.len() + b.len()];
    for (i, &x) in a.iter().enumerate() {
        let mut carry: u64 = 0;
        for (j, &y) in b.iter().enumerate() {
            let cur = out[i + j] + x as u64 * y as u64 + carry;
            out[i + j] = cur % BASE;
            carry = cur / BASE;
        }
        let mut k = i + b.len();
        while carry > 0 {
            let cur = out[k] + carry;
            out[k] = cur % BASE;
            carry = cur / BASE;
            k += 1;
        }
    }
    let mut mag: Vec<u32> = out.into_iter().map(|x| x as u32).collect();
    trim(&mut mag);
    mag
}

/// 绝对值的整除（截断）：返回 (商, 余数)。
///
/// 逐肢长除法：每步把余数乘 BASE 再补一个肢体，商的这一位用二分查找
/// （0..BASE-1）——比逐次减法快得多，且实现只有几十行。
fn divmod_mag(a: &[u32], b: &[u32]) -> (Vec<u32>, Vec<u32>) {
    if b.is_empty() {
        return (Vec::new(), Vec::new());
    } // 调用方挡住除零
    if cmp_mag(a, b) == Ordering::Less {
        return (Vec::new(), a.to_vec());
    }
    let mut rem: Vec<u32> = Vec::new();
    let mut quo = vec![0u32; a.len()];
    for i in (0..a.len()).rev() {
        rem.insert(0, a[i]); // rem = rem * BASE + a[i]
        trim(&mut rem);
        let (mut lo, mut hi) = (0u32, (BASE - 1) as u32);
        let mut best = 0u32;
        while lo <= hi {
            let mid = lo + (hi - lo) / 2;
            let prod = mul_small_mag(b, mid);
            if cmp_mag(&prod, &rem) != Ordering::Greater {
                best = mid;
                if mid == hi {
                    break;
                }
                lo = mid + 1;
            } else {
                if mid == 0 {
                    break;
                }
                hi = mid - 1;
            }
        }
        if best > 0 {
            let prod = mul_small_mag(b, best);
            rem = sub_mag(&rem, &prod);
        }
        quo[i] = best;
    }
    trim(&mut quo);
    (quo, rem)
}

impl BigInt {
    pub fn zero() -> Self {
        BigInt {
            neg: false,
            mag: Vec::new(),
        }
    }

    pub fn is_zero(&self) -> bool {
        self.mag.is_empty()
    }

    /// 从 i128 构造（M32：`Val::Int` 已从 i64 提到 i128）。
    pub fn from_i128(v: i128) -> Self {
        let neg = v < 0;
        let mut u = v.unsigned_abs();
        let mut mag = Vec::new();
        while u > 0 {
            mag.push((u % BASE as u128) as u32);
            u /= BASE as u128;
        }
        BigInt {
            neg: neg && !mag.is_empty(),
            mag,
        }
    }

    pub fn from_i64(v: i64) -> Self {
        let neg = v < 0;
        let mut u = (v as i128).unsigned_abs() as u128;
        let mut mag = Vec::new();
        while u > 0 {
            mag.push((u % BASE as u128) as u32);
            u /= BASE as u128;
        }
        BigInt {
            neg: neg && !mag.is_empty(),
            mag,
        }
    }

    /// 从纯数字串（不含符号）构造。
    pub fn from_digits(digits: &str) -> Self {
        let bytes: Vec<u8> = digits.bytes().filter(|b| b.is_ascii_digit()).collect();
        let mut mag = Vec::new();
        let mut i = bytes.len();
        while i > 0 {
            let start = i.saturating_sub(LIMB_DIGITS);
            let chunk = std::str::from_utf8(&bytes[start..i]).unwrap_or("0");
            mag.push(chunk.parse::<u32>().unwrap_or(0));
            i = start;
        }
        trim(&mut mag);
        BigInt { neg: false, mag }
    }

    /// 10^k（k 可以很大，直接按肢体摆位，不必反复乘）。
    pub fn pow10(k: u32) -> Self {
        let q = (k as usize) / LIMB_DIGITS;
        let r = (k as usize) % LIMB_DIGITS;
        let mut mag = vec![0u32; q];
        mag.push(10u32.pow(r as u32));
        BigInt { neg: false, mag }
    }

    pub fn neg(&self) -> Self {
        if self.is_zero() {
            return self.clone();
        }
        BigInt {
            neg: !self.neg,
            mag: self.mag.clone(),
        }
    }

    pub fn abs(&self) -> Self {
        BigInt {
            neg: false,
            mag: self.mag.clone(),
        }
    }

    pub fn cmp(&self, other: &Self) -> Ordering {
        match (self.neg, other.neg) {
            (false, true) => Ordering::Greater,
            (true, false) => Ordering::Less,
            (false, false) => cmp_mag(&self.mag, &other.mag),
            (true, true) => cmp_mag(&other.mag, &self.mag),
        }
    }

    pub fn add(&self, other: &Self) -> Self {
        if self.neg == other.neg {
            BigInt {
                neg: self.neg,
                mag: add_mag(&self.mag, &other.mag),
            }
        } else {
            match cmp_mag(&self.mag, &other.mag) {
                Ordering::Equal => BigInt::zero(),
                Ordering::Greater => BigInt {
                    neg: self.neg,
                    mag: sub_mag(&self.mag, &other.mag),
                },
                Ordering::Less => BigInt {
                    neg: other.neg,
                    mag: sub_mag(&other.mag, &self.mag),
                },
            }
        }
    }

    pub fn sub(&self, other: &Self) -> Self {
        self.add(&other.neg())
    }

    pub fn mul(&self, other: &Self) -> Self {
        let mag = mul_mag(&self.mag, &other.mag);
        BigInt {
            neg: self.neg != other.neg && !mag.is_empty(),
            mag,
        }
    }

    /// 截断除法（商向零取整），与 Python `Decimal` 的 //、% 一致。
    pub fn divmod(&self, other: &Self) -> (Self, Self) {
        let (qm, rm) = divmod_mag(&self.mag, &other.mag);
        let qneg = self.neg != other.neg && !qm.is_empty();
        let rneg = self.neg && !rm.is_empty();
        (BigInt { neg: qneg, mag: qm }, BigInt { neg: rneg, mag: rm })
    }

    pub fn mul10(&self) -> Self {
        self.mul(&BigInt::pow10(1))
    }

    /// 十进制位数（0 算 1 位）。
    pub fn digits(&self) -> usize {
        if self.mag.is_empty() {
            return 1;
        }
        let top = self.mag[self.mag.len() - 1];
        (self.mag.len() - 1) * LIMB_DIGITS + top.to_string().len()
    }

    pub fn to_string(&self) -> String {
        if self.mag.is_empty() {
            return "0".to_string();
        }
        let mut out = String::new();
        if self.neg {
            out.push('-');
        }
        out.push_str(&self.mag[self.mag.len() - 1].to_string());
        for i in (0..self.mag.len() - 1).rev() {
            out.push_str(&format!("{:0>9}", self.mag[i]));
        }
        out
    }
}

// ---------------------------------------------------------------------------
// 十进制小数
// ---------------------------------------------------------------------------

/// 精确小数：值 = `coef` × 10^`exp`。
#[derive(Clone)]
pub struct Dec {
    pub coef: BigInt,
    pub exp: i32,
}

/// 舍入方式：`HalfEven` 是 Python Decimal 的默认；`HalfUp` 是金额场景的
/// 「四舍五入」（0.5 一律进位，更合直觉）。
#[derive(Clone, Copy, PartialEq)]
pub enum Round {
    HalfEven,
    HalfUp,
}

impl Dec {
    pub fn zero() -> Self {
        Dec {
            coef: BigInt::zero(),
            exp: 0,
        }
    }

    /// 解析十进制字符串（可带正负号、小数点、指数）。
    ///
    /// 不接受 `1_000`、`0x10`、`NaN`、`Infinity` 这类 Python `Decimal`
    /// 也不认或我们不想支持的写法——给中文错由调用方负责。
    pub fn parse(s: &str) -> Option<Dec> {
        let t = s.trim();
        if t.is_empty() {
            return None;
        }
        let (neg, rest) = match t.as_bytes()[0] {
            b'-' => (true, &t[1..]),
            b'+' => (false, &t[1..]),
            _ => (false, t),
        };
        // 拆指数
        let (mant, e) = match rest.find(['e', 'E']) {
            Some(i) => {
                let ex: i32 = rest[i + 1..].parse().ok()?;
                (&rest[..i], ex)
            }
            None => (rest, 0),
        };
        let (int_part, frac_part) = match mant.find('.') {
            Some(i) => (&mant[..i], &mant[i + 1..]),
            None => (mant, ""),
        };
        if int_part.is_empty() && frac_part.is_empty() {
            return None;
        }
        if !int_part.bytes().all(|b| b.is_ascii_digit())
            || !frac_part.bytes().all(|b| b.is_ascii_digit())
        {
            return None;
        }
        let digits = format!("{int_part}{frac_part}");
        let mut coef = BigInt::from_digits(&digits);
        if neg && !coef.is_zero() {
            coef = coef.neg();
        }
        Some(Dec {
            coef,
            exp: e - frac_part.len() as i32,
        })
    }

    /// 从 `f64` 构造：**先转字符串再解析**。
    ///
    /// 这一步是「精确」能不能名副其实的关键：`0.1` 的二进制真值并不是
    /// 0.1，但 `format!("{}", 0.1)` 是 "0.1"。Python 侧同样走 `str(v)`。
    pub fn from_f64(f: f64) -> Option<Dec> {
        if !f.is_finite() {
            return None;
        }
        let mut s = format!("{f}");
        // Python 的 str(1.0) 是 "1.0"（保留标度），Rust 的 Display 给 "1"
        if !s.contains('.') && !s.contains(['e', 'E']) {
            s.push_str(".0");
        }
        Dec::parse(&s)
    }

    fn aligned(&self, target: i32) -> BigInt {
        let mut c = self.coef.clone();
        let mut e = self.exp;
        while e > target {
            c = c.mul10();
            e -= 1;
        }
        c
    }

    // 加减乘的结果都要按 28 位有效数字舍入（M32）：Python 的 Decimal 每次
    // 运算都走 context（prec=28, ROUND_HALF_EVEN），
    // `Decimal("9999999999999999999999999999") + 1` 给的是
    // `1.000000000000000000000000000E+28` 而不是 29 位的精确值。
    // 以前只给 div 加了舍入，于是「加减乘超 28 位」时各宿主给出不同的数
    // ——那属于「静默算错」，比报错更糟。
    // 注意：构造（`精确("…")`）**不**舍入，与 Python 的 Decimal(str) 一致。

    pub fn add(&self, o: &Dec) -> Dec {
        let e = self.exp.min(o.exp);
        Dec {
            coef: self.aligned(e).add(&o.aligned(e)),
            exp: e,
        }
        .round_to_significant(PRECISION)
    }

    pub fn sub(&self, o: &Dec) -> Dec {
        let e = self.exp.min(o.exp);
        Dec {
            coef: self.aligned(e).sub(&o.aligned(e)),
            exp: e,
        }
        .round_to_significant(PRECISION)
    }

    pub fn mul(&self, o: &Dec) -> Dec {
        Dec {
            coef: self.coef.mul(&o.coef),
            exp: self.exp + o.exp,
        }
        .round_to_significant(PRECISION)
    }

    /// 除法：算到 28 位有效数字，ROUND_HALF_EVEN，再去尾零。
    pub fn div(&self, o: &Dec) -> Option<Dec> {
        if o.coef.is_zero() {
            return None;
        }
        let extra = (PRECISION + 2) as u32;
        let shift = BigInt::pow10(extra);
        let num = self.coef.mul(&shift);
        let (mut q, rem) = num.divmod(&o.coef);
        // half-even：余数超过一半就进位；正好一半时把商凑成偶数
        let twice = rem.abs().mul(&BigInt::from_i64(2));
        let den = o.coef.abs();
        match twice.cmp(&den) {
            Ordering::Greater => {
                let up = BigInt::from_i64(if self.coef.neg == o.coef.neg { 1 } else { -1 });
                q = q.add(&up);
            }
            Ordering::Equal => {
                let (half, _) = q.divmod(&BigInt::from_i64(2));
                if !(q.sub(&half.mul(&BigInt::from_i64(2)))).is_zero() {
                    let up = BigInt::from_i64(if self.coef.neg == o.coef.neg { 1 } else { -1 });
                    q = q.add(&up);
                }
            }
            Ordering::Less => {}
        }
        let res = Dec {
            coef: q,
            exp: self.exp - o.exp - extra as i32,
        };
        Some(res.round_to_significant(PRECISION).strip_zeros())
    }

    /// 整除 / 取余：两边对齐到同一标度后做整数截断除法
    /// （与 Python `Decimal` 的 `//`、`%` 一致，商向零取整）。
    pub fn divmod_floor(&self, o: &Dec) -> Option<(Dec, Dec)> {
        if o.coef.is_zero() {
            return None;
        }
        let e = self.exp.min(o.exp);
        let (q, r) = self.aligned(e).divmod(&o.aligned(e));
        Some((Dec { coef: q, exp: 0 }, Dec { coef: r, exp: e }))
    }

    /// 整数次幂（精确）。非整数指数返回 None——那是近似运算，
    /// 各执行器的实现细节会漂移，宁可不支持。
    pub fn powi(&self, n: i64) -> Option<Dec> {
        if n == 0 {
            return Some(Dec {
                coef: BigInt::from_i64(1),
                exp: 0,
            });
        }
        let mut base = if n < 0 { self.recip()? } else { self.clone() };
        let mut e = n.unsigned_abs();
        let mut acc = Dec {
            coef: BigInt::from_i64(1),
            exp: 0,
        };
        while e > 0 {
            if e & 1 == 1 {
                acc = acc.mul(&base);
            }
            e >>= 1;
            if e > 0 {
                base = base.mul(&base);
            }
        }
        Some(acc)
    }

    fn recip(&self) -> Option<Dec> {
        let one = Dec {
            coef: BigInt::from_i64(1),
            exp: 0,
        };
        one.div(self)
    }

    pub fn neg(&self) -> Dec {
        Dec {
            coef: self.coef.neg(),
            exp: self.exp,
        }
    }
    pub fn abs(&self) -> Dec {
        Dec {
            coef: self.coef.abs(),
            exp: self.exp,
        }
    }

    pub fn cmp(&self, o: &Dec) -> Ordering {
        let e = self.exp.min(o.exp);
        self.aligned(e).cmp(&o.aligned(e))
    }

    pub fn is_zero(&self) -> bool {
        self.coef.is_zero()
    }

    /// 舍到某个 10 的幂位：0 → 个位，-2 → 分位，2 → 百位。
    pub fn quantize10(&self, target_exp: i32, mode: Round) -> Dec {
        if self.exp > target_exp {
            // 系数要「补零」到自己更粗的标度上：Python 的 `quantize` 是**精确**
            // 调整到给定指数，不只是舍入（`Decimal("1234").quantize(Decimal("0.01"))`
            // 给 `1234.00`）。少了这一步，`舍入(2)` 会原地返回 `1234`（M32）。
            return Dec {
                coef: self.coef.mul(&BigInt::pow10((self.exp - target_exp) as u32)),
                exp: target_exp,
            };
        }
        if self.exp == target_exp {
            return self.clone();
        }
        let drop = (target_exp - self.exp) as u32;
        let p = BigInt::pow10(drop);
        let abs = self.coef.abs();
        let (mut q, rem) = abs.divmod(&p);
        let twice = rem.mul(&BigInt::from_i64(2));
        let bump = match mode {
            Round::HalfUp => twice.cmp(&p) != Ordering::Less,
            Round::HalfEven => match twice.cmp(&p) {
                Ordering::Greater => true,
                Ordering::Less => false,
                Ordering::Equal => {
                    let (half, _) = q.divmod(&BigInt::from_i64(2));
                    !q.sub(&half.mul(&BigInt::from_i64(2))).is_zero()
                }
            },
        };
        if bump {
            q = q.add(&BigInt::from_i64(1));
        }
        let signed = if self.coef.neg { q.neg() } else { q };
        Dec {
            coef: signed,
            exp: target_exp,
        }
    }

    /// 舍入到 n 位有效数字（half-even）。
    pub fn round_to_significant(&self, n: usize) -> Dec {
        let d = self.coef.digits();
        if d <= n {
            return self.clone();
        }
        self.quantize10(self.exp + (d - n) as i32, Round::HalfEven)
    }

    /// 去掉尾随零（只在小数位上做，不改变 `25` 这类整数的形态）。
    pub fn strip_zeros(&self) -> Dec {
        let ten = BigInt::pow10(1);
        let mut c = self.coef.clone();
        let mut e = self.exp;
        while e < 0 {
            let (q, r) = c.divmod(&ten);
            if !r.is_zero() {
                break;
            }
            c = q;
            e += 1;
        }
        Dec { coef: c, exp: e }
    }

    /// 定点写法 —— 对应 Python 的 `format(v, "f")`：`1.2E+3` → `1200`。
    ///
    /// `舍入` 的最后一步要用它：Python 侧是
    /// `Decimal(format(rounded, "f"))`，把科学计数法换回普通写法。
    pub fn to_fixed_string(&self) -> String {
        let sign = if self.coef.neg { "-" } else { "" };
        let abs = self.coef.abs();
        let digits = if abs.is_zero() {
            "0".repeat(std::cmp::max(1, -self.exp) as usize)
        } else {
            abs.to_string()
        };
        if self.exp >= 0 {
            return format!("{sign}{digits}{}", "0".repeat(self.exp as usize));
        }
        let point = digits.len() as i32 + self.exp;
        if point > 0 {
            let p = point as usize;
            format!("{}{}.{}", sign, &digits[..p], &digits[p..])
        } else {
            format!("{}0.{}{}", sign, "0".repeat((-point) as usize), digits)
        }
    }

    /// `str(Decimal)` 的写法（对齐 CPython 的 `_pydecimal.__str__`）。
    ///
    /// 规则：`leftdigits = exp + 系数位数`；**只有 `exp <= 0` 且
    /// `leftdigits > -6` 时才用定点**，否则用科学计数法（点前留 1 位、
    /// 指数带正负号、大写 `E`）。
    ///
    /// M32 才补上科学计数法分支——以前无条件补零，于是
    /// `精确("9999999999999999999999999999") + 1` 打出 29 位数字串，
    /// 而 Python 给 `1.000000000000000000000000000E+28`。
    pub fn to_string(&self) -> String {
        let neg = self.coef.neg;
        // 系数为 0 时按标度补足位数：Python 的 `_int` 对 `Decimal("0.00")` 是 "000"
        let digits = if self.coef.is_zero() {
            "0".repeat(std::cmp::max(1, -self.exp) as usize)
        } else {
            self.coef.abs().to_string()
        };
        let leftdigits = self.exp + digits.len() as i32;
        let dotplace = if self.exp <= 0 && leftdigits > -6 {
            leftdigits
        } else {
            1
        };
        let (intpart, fracpart) = if dotplace <= 0 {
            (
                "0".to_string(),
                format!(".{}{}", "0".repeat((-dotplace) as usize), digits),
            )
        } else if dotplace as usize >= digits.len() {
            (
                format!(
                    "{}{}",
                    digits,
                    "0".repeat(dotplace as usize - digits.len())
                ),
                String::new(),
            )
        } else {
            let p = dotplace as usize;
            (digits[..p].to_string(), format!(".{}", &digits[p..]))
        };
        let exp_part = if leftdigits == dotplace {
            String::new()
        } else {
            format!(
                "E{}{}",
                if leftdigits - dotplace >= 0 { "+" } else { "" },
                leftdigits - dotplace
            )
        };
        format!(
            "{}{}{}{}",
            if neg { "-" } else { "" },
            intpart,
            fracpart,
            exp_part
        )
    }

    /// 正好是整数时给 `i64`（给 `**` 的指数用）；有小位数或放不下则 None。
    pub fn to_i64(&self) -> Option<i64> {
        let s = self.strip_zeros();
        if s.exp < 0 {
            return None;
        }
        s.to_string().parse::<i64>().ok()
    }
}
