//! 字典 / 集合用的**快哈希**（M212）。
//!
//! ## 为什么不用 `std::collections::HashMap` 的默认哈希
//!
//! 默认是 **SipHash-1-3**：抗哈希洪水，代价是每个键都要跑一轮 SipHash
//! （字符串还要先算整个长度再分块）。而字典是解释器最热的容器 ——
//! `字典查找密集` / `字典写入密集` 两个基准里，哈希能占到相当一块。
//!
//! ## 换成什么
//!
//! **FxHash**（rustc 自己内部用的那套）—— 一条 `rotate_left(5) ^ word` 再乘一个
//! 奇数常量，8 字节键就两三条指令。
//!
//! ## 🔴 但**必须保留随机种子**
//!
//! 固定种子的快哈希有一个致命问题：**哈希洪水**。攻击者（或者一个不小心把
//! 用户输入当键的程序）能构造出成百上千个撞到同一个桶的键，字典退化成链表，
//! 一次查找从 O(1) 变成 O(n)。`HashMap` 的默认 SipHash 之所以存在，就是为了挡它。
//!
//! 所以这里**每进程一个随机种子**（取自 `RandomState`，与 std 同一熵源），
//! 同一个进程内所有字典共用。攻击者要撞桶，得先猜中这个种子。
//!
//! 📌 实测（M212，Win x86-64 release，`tools/bench_vs_python.py` 固定规模 3 轮中位）：
//! 见 CHANGELOG —— **A/B 不到位就删**（M164 给文本下标加 ASCII 分支反而慢 3.3%
//! 就是先例，那时候的处理办法是整段回滚，不是留着「说不定有用」）。

use std::collections::HashMap;
use std::hash::{BuildHasher, Hasher};
use std::sync::OnceLock;

/// FxHash 的乘法常量（rustc `rustc-hash` 用的那个奇数）。
const 常量: u64 = 0x517c_c1b7_2722_0a95;

/// 进程级随机种子：只取一次，之后所有字典共用。
fn 种子() -> u64 {
    static 种: OnceLock<u64> = OnceLock::new();
    *种.get_or_init(|| {
        // 借 `RandomState` 取一次系统随机（与 std 的 HashMap 同一熵源）。
        // ⚠️ wasm 上没有真正的随机源，std 会退回一个弱种子 —— 那里也没有
        //    「来自网络的键」，挡不住洪水不是问题（见 D113 的同一条道理）。
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u64(0x9E37_79B9_7F4A_7C15);
        h.finish() | 1                  // 保证偶数常量也参与混合（不必为 0）
    })
}

/// FxHash 风格的 64 位混合 + 进程随机种子。
#[derive(Clone, Copy)]
pub(crate) struct 快哈希 {
    值: u64,
}

impl 快哈希 {
    #[inline]
    fn 混(&mut self, w: u64) {
        self.值 = (self.值.rotate_left(5) ^ w).wrapping_mul(常量);
    }
}

impl Hasher for 快哈希 {
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        // 按 8 字节一组读（小端），尾巴按实际长度补。
        // 字符串键（字段名）走这条；短键通常一轮就完。
        let mut 余 = bytes;
        while 余.len() >= 8 {
            let mut w = [0u8; 8];
            w.copy_from_slice(&余[..8]);
            self.混(u64::from_le_bytes(w));
            余 = &余[8..];
        }
        if !余.is_empty() {
            let mut w = [0u8; 8];
            w[..余.len()].copy_from_slice(余);
            // 长度也要混进去：否则 "a\0" 与 "a" 撞成同一个值
            self.混(u64::from_le_bytes(w) ^ (余.len() as u64) << 56);
        }
    }

    #[inline]
    fn write_u8(&mut self, i: u8) { self.混(i as u64); }
    #[inline]
    fn write_u32(&mut self, i: u32) { self.混(i as u64); }
    #[inline]
    fn write_u64(&mut self, i: u64) { self.混(i); }
    #[inline]
    fn write_u128(&mut self, i: u128) {
        self.混(i as u64);
        self.混((i >> 64) as u64);
    }
    #[inline]
    fn write_usize(&mut self, i: usize) { self.混(i as u64); }

    #[inline]
    fn finish(&self) -> u64 { self.值 }
}

/// `HashMap` 的建造者：每次 `build_hasher` 都从**同一个**进程种子起算。
#[derive(Clone, Copy, Default)]
pub(crate) struct 快哈希建造者;

impl BuildHasher for 快哈希建造者 {
    type Hasher = 快哈希;
    #[inline]
    fn build_hasher(&self) -> 快哈希 { 快哈希 { 值: 种子() } }
}

/// 字典 / 集合内部用的哈希表类型（键类型不变，只换哈希）。
pub(crate) type 快表<K, V> = HashMap<K, V, 快哈希建造者>;

#[cfg(test)]
mod tests {
    use super::*;
    use std::hash::Hash;

    fn 哈<T: Hash>(v: &T) -> u64 {
        let mut h = 快哈希 { 值: 种子() };
        v.hash(&mut h);
        h.finish()
    }

    #[test]
    fn 同值同哈希_异值异哈希() {
        assert_eq!(哈(&"甲"), 哈(&"甲"));
        assert_ne!(哈(&"甲"), 哈(&"乙"));
        // 短键的尾巴补零不能和「真的多一个零字节」撞
        assert_ne!(哈(&"a"), 哈(&"a\0"));
        assert_ne!(哈(&1i128), 哈(&2i128));
    }

    #[test]
    fn 长键也稳() {
        let a = "很长的键".repeat(200);
        let b = format!("{a}x");
        assert_eq!(哈(&a), 哈(&a));
        assert_ne!(哈(&a), 哈(&b));
    }

    #[test]
    fn 表能正常存取() {
        let mut 表: 快表<String, i32> = 快表::default();
        for i in 0..1000 {
            表.insert(format!("k{i}"), i);
        }
        for i in 0..1000 {
            assert_eq!(表.get(&format!("k{i}")), Some(&i));
        }
        assert_eq!(表.len(), 1000);
    }
}
