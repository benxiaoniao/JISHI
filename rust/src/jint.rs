//! 整数的**存储形态**：语义仍是 `i128`，但**对齐按 8**。
//!
//! ## 为什么要这一层
//!
//! `Val` 一直是 **32 字节**，卡在 `Int(i128)` 上：`i128` 的**对齐是 16**，
//! 枚举整体也跟着 16 对齐 ⇒ 16（载荷）+ 8（判别标签）向上取整到 **32**。
//! 而 `Val` 的大小直接决定：栈上每个值槽的复制量、`Vec<Val>` 的缓存密度、
//! 函数帧的 memcpy 量。24 比 32 少 25%。
//!
//! M165b 把 `Str` 从 `String` 换成 `Rc<str>` 时注释里写「降到 24」——
//! **那一步只解决了 `String` 的 24 字节，没解决 i128 的 16 对齐**，
//! 所以 `--dump-val-size` 一直报 32。M214 补上这一层。
//!
//! ## 为什么不是 `Int(i64)` + 溢出装箱
//!
//! 那条路会**改变语义路径**：每个算术点都要判断溢出、每个匹配点都要多处理
//! 一个 `Big` 分支 —— 而现在的语义（超出 i128 明确报错）是对的、判据也都在。
//! 用 `JInt` 只是把**同一份 `i128`** 换了个对齐 8 的壳子：
//!
//! ```text
//! JInt { lo: u64, hi: u64 }   // 小端：lo 是低 64 位
//! ```
//!
//! `get()` 把它拼回 `i128`，`new()` 反向拆。**没有新的语义分支**，
//! 编译器会把所有需要改的调用点报出来（比新增一个 `Big` 变体安全得多）。
//!
//! ## 代价
//!
//! 每次取整数多两条 `mov`（拼高低位），编译器通常能把它和后续运算合并；
//! A/B 见 `CHANGELOG` 的 M214 一行。换来的是 `Val` 24 字节。

use std::cmp::Ordering;
use std::hash::{Hash, Hasher};

/// 对齐 8 的 16 字节整数壳。**语义与 `i128` 完全一致**（含符号）。
#[repr(C, align(8))]
#[derive(Clone, Copy)]
pub(crate) struct JInt {
    lo: u64,
    hi: i64,
}

impl JInt {
    #[inline]
    pub(crate) const fn new(v: i128) -> JInt {
        JInt {
            lo: v as u64,
            hi: (v >> 64) as i64,
        }
    }

    #[inline]
    pub(crate) const fn get(self) -> i128 {
        ((self.hi as u128) << 64 | self.lo as u128) as i128
    }
}

impl From<i128> for JInt {
    #[inline]
    fn from(v: i128) -> JInt {
        JInt::new(v)
    }
}

impl From<i64> for JInt {
    #[inline]
    fn from(v: i64) -> JInt {
        JInt::new(v as i128)
    }
}

impl From<usize> for JInt {
    #[inline]
    fn from(v: usize) -> JInt {
        JInt::new(v as i128)
    }
}

impl PartialEq for JInt {
    #[inline]
    fn eq(&self, other: &Self) -> bool {
        self.get() == other.get()
    }
}

impl Eq for JInt {}

impl PartialOrd for JInt {
    #[inline]
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for JInt {
    #[inline]
    fn cmp(&self, other: &Self) -> Ordering {
        self.get().cmp(&other.get())
    }
}

impl Hash for JInt {
    #[inline]
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.get().hash(state)
    }
}

impl std::fmt::Display for JInt {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.get().fmt(f)
    }
}

impl std::fmt::Debug for JInt {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.get().fmt(f)
    }
}

#[cfg(test)]
mod tests {
    use super::JInt;

    #[test]
    fn 大小与对齐是这一层存在的理由() {
        assert_eq!(std::mem::size_of::<JInt>(), 16);
        assert_eq!(std::mem::align_of::<JInt>(), 8);
    }

    #[test]
    fn 往返不丢位() {
        for v in [
            0i128,
            1,
            -1,
            i64::MAX as i128,
            i64::MIN as i128,
            i64::MAX as i128 + 1,
            i128::MAX,
            i128::MIN,
            123456789012345678901234567890,
        ] {
            assert_eq!(JInt::new(v).get(), v, "{v}");
        }
    }

    #[test]
    fn 比较与哈希按数值() {
        assert!(JInt::new(-5) < JInt::new(3));
        assert!(JInt::new(i128::MAX) > JInt::new(i64::MAX as i128));
        assert_eq!(JInt::new(7), JInt::new(7));
        assert_ne!(JInt::new(7), JInt::new(8));
    }
}
