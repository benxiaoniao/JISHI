//! 文本的 16 字节表示（M214b）。
//!
//! ## 为什么需要它
//!
//! M214 实测：**遍历 2 万字符比遍历 2 万元素的列表慢 11 倍**
//! （2.13 ms vs 0.18 ms，每字符多花 ~96 ns）。那 96 ns 不是解释器循环，
//! 是**每个字符一次 `Rc<str>` 的构造 + 引用计数 + 释放**：
//!
//! ```text
//! 槽.with(...)        TLS 访问          ~5 ns
//! borrow_mut()        RefCell 借用      ~3 ns
//! Rc::clone           原子增            ~8 ns
//! Drop                原子减 + 判零      ~8 ns
//! --- 剩下的还有 malloc/free（未命中时）
//! ```
//!
//! CPython 的 `for ch in s` 之所以快，是因为它有**单字符 intern 缓存**，
//! 循环体里只做 INCREF/DECREF。我们的单字符缓存（`单字符`）解决了一半，
//! 但**没命中时仍然 malloc**，而 `Rc<str>` 的计数开销跑不掉。
//!
//! ## 两个约束决定了形状
//!
//! 1. **`JStr` 必须 16 字节**，否则 `Val` 从 24 涨回 32（M214 刚降下来）。
//! 2. **堆串必须一次分配**。M214 试过 `Rc<Heap>{ Box<str>, … }` 的两层形态：
//!    单场景快 20×，但堆串变两次分配（Rc 头 + 字符串缓冲），字符串拼接 /
//!    比较 / 切片 / 文本列表**全掉 20~40%**，19 场景几何 1.75 → 1.40 ——
//!    整段回退（见 CHANGELOG M214）。`Rc<str>` 是**一次分配**（头与字节同块），
//!    这是它唯一的、也是决定性的优势。
//!
//! `Rc<str>` 是 fat pointer（16 字节），装不进 16 字节的枚举槽（还要留 tag）。
//! 所以这里自己写一个 **thin pointer + 手工引用计数** 的堆串：
//!
//! ```text
//! ┌──────────┬──────────┬─────────┬────────┬───────────────────┐
//! │ strong:8 │  len:4   │  n:4    │ ascii:1│ 字节（数据）…      │
//! └──────────┴──────────┴─────────┴────────┴───────────────────┘
//!   一次 malloc，头与数据连续 —— 和 `Rc<str>` 的布局是一个思路。
//! ```
//!
//! ## 内联（SSO）
//!
//! ≤7 字节的串**完全不分配**：`[len:u8, bytes:[u8;7]]`。这正好覆盖
//! 「单个 BMP 字符」（≤3 字节）、大部分标识符、`"y"`/`"true"` 这类小常量。
//!
//! ## 安全边界
//!
//! - `JStr` **不可变**，和 `Rc<str>` 一样按内容比较（生产代码里没有拿指针
//!   当语义用的地方，M214 查过）。
//! - `Cell` 不是 `Sync` ⇒ `JStr` 不是 `Send`/`Sync`。这和 `Rc`/`RefCell`
//!   一致（VM 本来就是单线程的），不是新限制。
//! - `Hdr` 与 `Val` 之间有 `unsafe` 的裸指针，但所有生命周期都由引用计数管：
//!   `Clone` 加一、`Drop` 减一，减到 0 才 `dealloc`。`from_str` 是唯一入口。
//! - **`HDR` 必须是 8 的倍数**（数据区要按 8 对齐，`str` 只要求 1，但
//!   8 对齐能让 `Vec<u8>` 那类 memcpy 走更快的路径）。
use std::alloc::{alloc, dealloc, handle_alloc_error, Layout};
use std::cell::Cell;
use std::cmp::Ordering;
use std::fmt;
use std::hash::{Hash, Hasher};
use std::ops::Deref;
use std::ptr::NonNull;

/// 内联上限：`JStr` 只有 16 字节，`[len:1][bytes:7]` 是能塞下的最大值。
/// 正好覆盖「单个 BMP 字符」（UTF-8 ≤3 字节）与大部分短标识符。
const 内联上限: usize = 7;

/// 堆头大小：`strong:8 + len:4 + n:4 + ascii:1` = 17，按 8 对齐取 24。
///
/// 🔴 改这个常量要同步改下面 `Hdr` 的字段顺序 —— 两者是靠 `#[repr(C)]`
/// 与「字段总长 ≤ HDR」这个约定对齐的，编译器不会替我们检查。
const HDR: usize = 24;

/// 堆串的头部（紧贴着字符串字节）。
#[repr(C)]
struct Hdr {
    /// 强引用计数。到 0 就整块 `dealloc`。
    strong: Cell<usize>,
    /// 字符数（**按需**缓存）。0 = 还没算过。
    ///
    /// 🔴 没有它，非 ASCII 长串的每次下标都要 `chars().count()` —— M214 实测
    /// 2 万中文取尾 **4.7 µs**，其中大头就是这一步；缓存后 0.24 µs。
    n: Cell<u32>,
    /// 字节数。
    len: u32,
    /// 是否纯 ASCII：0 = 没算过，1 = 是，2 = 否。
    ascii: Cell<u8>,
}

/// thin pointer 的堆串（8 字节）。
pub(crate) struct HeapStr {
    p: NonNull<Hdr>,
}

/// 16 字节的文本值。
///
/// `Inline` 是 SSO（≤7 字节），`Heap` 是一次分配的引用计数串。
pub(crate) enum JStr {
    /// 内联串：**`[0]` 是长度，`[1..=len]` 是 UTF-8 字节**，其余为 0。
    ///
    /// 为什么是「一个 8 字节数组」而不是 `{ len: u8, bytes: [u8;7] }`：
    /// 必须是这种布局，`Clone` 才是一次 8 字节 load/store、`==` 才是**一次
    /// 整数比较**（M214b 实测：分开写字段时 `[u8;7]` 的比较会调 `memcmp`，
    /// 在「短串比较」这种热路径上白花一次函数调用）。
    Inline([u8; 内联上限 + 1]),
    Heap(HeapStr),
}

impl Clone for HeapStr {
    /// 🔴 **不是 `#[derive(Clone)]`** —— 派生只会复制那个指针（double free）。
    fn clone(&self) -> Self {
        unsafe { self.p.as_ref().strong.set(self.p.as_ref().strong.get() + 1) };
        HeapStr { p: self.p }
    }
}

impl Drop for HeapStr {
    fn drop(&mut self) {
        unsafe {
            let h = self.p.as_ref();
            let c = h.strong.get();
            if c > 1 {
                h.strong.set(c - 1);
                return;
            }
            // 最后一份：整块还回去。`len` 必须在 `dealloc` **之前**读出来
            // （之后这块内存就不属于我们了）。
            let 总长 = HDR + h.len as usize;
            let layout = Layout::from_size_align_unchecked(总长, 8);
            dealloc(self.p.as_ptr() as *mut u8, layout);
        }
    }
}

impl HeapStr {
    /// 从字节建堆串（调用方保证是合法 UTF-8 且长度 > 内联上限）。
    fn 新(s: &str) -> Self {
        let b = s.as_bytes();
        debug_assert!(b.len() > 内联上限);
        debug_assert!(b.len() <= u32::MAX as usize, "单个文本超过 4 GB");
        unsafe {
            let 总长 = HDR + b.len();
            let layout = Layout::from_size_align_unchecked(总长, 8);
            let raw = alloc(layout) as *mut u8;
            if raw.is_null() {
                handle_alloc_error(layout);
            }
            let h = raw as *mut Hdr;
            h.write(Hdr {
                strong: Cell::new(1),
                n: Cell::new(0),
                len: b.len() as u32,
                ascii: Cell::new(0), // 懒算：建串时不扫一遍
            });
            std::ptr::copy_nonoverlapping(b.as_ptr(), raw.add(HDR), b.len());
            HeapStr { p: NonNull::new_unchecked(h) }
        }
    }

    #[inline]
    fn 头(&self) -> &Hdr {
        unsafe { self.p.as_ref() }
    }

    #[inline]
    fn as_str(&self) -> &str {
        unsafe {
            let h = self.头();
            let data = (self.p.as_ptr() as *const u8).add(HDR);
            std::str::from_utf8_unchecked(std::slice::from_raw_parts(data, h.len as usize))
        }
    }

    #[inline]
    fn is_ascii(&self) -> bool {
        let h = self.头();
        match h.ascii.get() {
            1 => true,
            2 => false,
            _ => {
                let 是 = self.as_str().is_ascii();
                h.ascii.set(if 是 { 1 } else { 2 });
                是
            }
        }
    }

    #[inline]
    fn char_count(&self) -> usize {
        let h = self.头();
        if self.is_ascii() {
            return h.len as usize;
        }
        let n = h.n.get();
        if n != 0 {
            return n as usize;
        }
        let n = self.as_str().chars().count();
        h.n.set(n as u32);
        n
    }
}

impl JStr {
    /// 空串（避免调用方到处写 `"".into()`）。
    #[inline]
    pub(crate) fn empty() -> Self {
        JStr::Inline([0; 内联上限 + 1])
    }

    /// 单个 ASCII 字符 —— **一次 8 字节写入**，没有 `encode_utf8` 也没有拷贝。
    ///
    /// 遍历文本的热路径（`iter_next` 的 ASCII 分支）直接用它。
    #[inline]
    pub(crate) fn from_ascii(b: u8) -> Self {
        debug_assert!(b < 0x80);
        // 一个常量就被编译成「把立即数写进 8 字节槽」
        JStr::Inline([1, b, 0, 0, 0, 0, 0, 0])
    }

    /// 从「一个字符的 UTF-8 字节」（≤4 字节、调用方保证合法）建内联串。
    ///
    /// 遍历文本的非 ASCII 分支用它：**直接从原串拷那几个字节**，
    /// 不 `chars().next()` 解码、也不再 `encode_utf8` 编码一遍。
    #[inline]
    pub(crate) fn from_utf8_prefix(b: &[u8]) -> Self {
        debug_assert!(!b.is_empty() && b.len() <= 4);
        // 先拼成一个 u32（≤4 字节的拷贝会被内联成一次 load），
        // 再和长度一起拼成 8 字节 —— 全程没有 `memcpy` 调用。
        let mut w = [0u8; 4];
        w[..b.len()].copy_from_slice(b);
        let v = u32::from_le_bytes(w) as u64;
        let raw = ((b.len() as u64) | (v << 8)).to_le_bytes();
        JStr::Inline(raw)
    }

    /// 从 `&str` 建：≤7 字节走内联，否则一次分配。
    pub(crate) fn new(s: &str) -> Self {
        let b = s.as_bytes();
        if b.len() <= 内联上限 {
            let mut raw = [0u8; 内联上限 + 1];
            raw[0] = b.len() as u8;
            raw[1..=b.len()].copy_from_slice(b);
            return JStr::Inline(raw);
        }
        JStr::Heap(HeapStr::新(s))
    }

    #[inline]
    pub(crate) fn as_str(&self) -> &str {
        match self {
            JStr::Inline(raw) => unsafe {
                // 安全：`raw[1..=len]` 是 `new` / `from_ascii` 从合法 UTF-8
                // 复制的前缀，未用部分恒为 0（`0x00` 是合法 UTF-8）。
                std::str::from_utf8_unchecked(&raw[1..1 + raw[0] as usize])
            },
            JStr::Heap(h) => h.as_str(),
        }
    }

    /// 是否纯 ASCII（内联串直接扫那几个字节，堆串走缓存）。
    #[inline]
    pub(crate) fn is_ascii(&self) -> bool {
        match self {
            JStr::Inline(raw) => raw[1..1 + raw[0] as usize].is_ascii(),
            JStr::Heap(h) => h.is_ascii(),
        }
    }

    /// 字符数（**不是**字节数）。堆串按需缓存。
    #[inline]
    pub(crate) fn char_count(&self) -> usize {
        match self {
            // 内联串 ≤7 字节：先看是不是 ASCII（一次 `is_ascii` 判断，
            // 英文短串直接给 len），否则才数（最多 7 个字符）。
            JStr::Inline(raw) => {
                let n = raw[0] as usize;
                if raw[1..1 + n].is_ascii() { n } else { self.as_str().chars().count() }
            }
            JStr::Heap(h) => h.char_count(),
        }
    }

    /// 第 `i` 个字符（已保证 `i < char_count()`）。
    ///
    /// ASCII 堆串直接取字节 —— 这就是 M214b 那条「长 ASCII 下标 ≤0.10 µs」
    /// 的实现（不再每次都扫一遍前缀 `is_ascii`）。
    #[inline]
    pub(crate) fn char_at(&self, i: usize) -> Option<char> {
        match self {
            JStr::Inline(_) => self.as_str().chars().nth(i),
            JStr::Heap(h) => {
                if h.is_ascii() {
                    // 安全：ASCII 串的「第 i 个字符」就是「第 i 个字节」
                    unsafe {
                        let data = (h.p.as_ptr() as *const u8).add(HDR);
                        return Some(*data.add(i) as char);
                    }
                }
                self.as_str().chars().nth(i)
            }
        }
    }
}

impl Deref for JStr {
    type Target = str;
    #[inline]
    fn deref(&self) -> &str {
        self.as_str()
    }
}

impl From<&str> for JStr {
    #[inline]
    fn from(s: &str) -> Self {
        JStr::new(s)
    }
}

impl From<String> for JStr {
    #[inline]
    fn from(s: String) -> Self {
        JStr::new(&s)
    }
}

impl From<&String> for JStr {
    #[inline]
    fn from(s: &String) -> Self {
        JStr::new(s)
    }
}

impl From<char> for JStr {
    /// 单字符内联（`[len][utf8 字节]`），**不分配**。
    ///
    /// 这是「遍历文本」那条判据的核心：M214 实测每字符 96 ns（Rc 计数 + 释放），
    /// 内联之后没有堆、没有引用计数。
    #[inline]
    fn from(c: char) -> Self {
        // ASCII（遍历英文文本的 99%）走下面那条**单次 8 字节写入**；
        // 其余走 `encode_utf8` + 拷贝（≤4 字节）。
        let n = c as u32;
        if n < 0x80 {
            return JStr::from_ascii(n as u8);
        }
        let mut buf = [0u8; 4];
        let s = c.encode_utf8(&mut buf);
        JStr::new(s)
    }
}

impl Clone for JStr {
    // 🔴 `inline(always)` 不是随手加的：M214b 实测「常量装载」慢 10%，
    // 就是因为这个 match 没被内联 —— `Val::clone` 里调进来再调出去，
    // 每次多一次函数调用。加上之后回到基线（甚至更快）。
    #[inline(always)]
    fn clone(&self) -> Self {
        match self {
            // 内联：**一次 8 字节 load/store**，不动引用计数 ——
            // 这是「遍历文本」变快的关键（M214 时每字符要两趟原子加）。
            JStr::Inline(raw) => JStr::Inline(*raw),
            JStr::Heap(h) => JStr::Heap(h.clone()),
        }
    }
}

impl PartialEq for JStr {
    #[inline]
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            // 🔴 **同一块堆串直接判真** —— `Rc<str>` 的 `PartialEq` 就是这么做的，
            // 少了这一条就**丢了一个快路径**：`令 s = 常量串` 之后再 `s == 常量串`
            // 两边是同一个 `Rc`（clone 出来的），本该零成本。M214b 第一版漏了它，
            // 「短串热路径」实测慢 8%，补上就回来了。
            (JStr::Heap(a), JStr::Heap(b)) if a.p == b.p => true,
            // 内联串：**一次 8 字节整数比较**（长度在里面，不同长度自然不等），
            // 不走 `memcmp`、不进 `as_str`。
            (JStr::Inline(a), JStr::Inline(b)) => a == b,
            _ => self.as_str() == other.as_str(),
        }
    }
}

impl Eq for JStr {}

impl PartialOrd for JStr {
    #[inline]
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for JStr {
    #[inline]
    fn cmp(&self, other: &Self) -> Ordering {
        self.as_str().cmp(other.as_str())
    }
}

impl Hash for JStr {
    #[inline]
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.as_str().hash(state)
    }
}

impl fmt::Debug for JStr {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(self.as_str(), f)
    }
}

impl fmt::Display for JStr {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 尺寸() {
        assert_eq!(std::mem::size_of::<JStr>(), 16, "JStr 涨了 ⇒ Val 也会涨回 32");
        assert_eq!(std::mem::align_of::<JStr>(), 8);
        assert_eq!(std::mem::size_of::<HeapStr>(), 8);
        assert!(std::mem::size_of::<Hdr>() <= HDR, "Hdr 超出 HDR 常量");
    }

    #[test]
    fn 内联与堆的边界() {
        let 七 = "abcdefg";
        let 八 = "abcdefgh";
        assert_eq!(七.len(), 内联上限);
        assert!(matches!(JStr::new(七), JStr::Inline(_)));
        assert!(matches!(JStr::new(八), JStr::Heap(_)));
        assert_eq!(JStr::new(七).as_str(), 七);
        assert_eq!(JStr::new(八).as_str(), 八);
    }

    #[test]
    fn 中文往返() {
        for s in ["", "中", "中文", "中文ab", "一二三四五六七", "😀", "中文😀abc"] {
            let j = JStr::new(s);
            assert_eq!(&*j, s, "内容不符：{s}");
            assert_eq!(j.char_count(), s.chars().count(), "字符数不符：{s}");
            for (i, c) in s.chars().enumerate() {
                assert_eq!(j.char_at(i), Some(c), "{s} 的第 {i} 个字符");
            }
            assert_eq!(j.char_at(s.chars().count()), None);
        }
    }

    #[test]
    fn 引用计数不泄漏也不提前释放() {
        let 原 = JStr::new("这是一个超过七个字节的长字符串");
        let 甲 = 原.clone();
        let 乙 = 甲.clone();
        drop(原);
        assert_eq!(&*甲, "这是一个超过七个字节的长字符串");
        drop(乙);
        assert_eq!(&*甲, "这是一个超过七个字节的长字符串");
        let 丙 = 甲.clone();
        drop(甲);
        assert_eq!(&*丙, "这是一个超过七个字节的长字符串");
    }

    #[test]
    fn 相等按内容不按指针() {
        let a = JStr::new("同样的一句话超过七个字节");
        let b = JStr::new("同样的一句话超过七个字节");
        assert_eq!(a, b);
        assert_ne!(a.as_str().as_ptr(), b.as_str().as_ptr()); // 不同的块，仍判相等
        let c = JStr::new("短");
        let d = JStr::new("短");
        assert_eq!(c, d);
    }

    #[test]
    fn 内联串的相等是一次整数比较() {
        // 「abc」与「abd」只差一个字节 —— 长度相同
        assert_ne!(JStr::new("abc"), JStr::new("abd"));
        assert_ne!(JStr::new("ab"), JStr::new("abc"));
        assert_eq!(JStr::new("abc"), JStr::new("abc"));
        // 8 字节全满（7 个 ASCII 字节 + 长度字节）的边界
        assert_eq!(JStr::new("abcdefg"), JStr::new("abcdefg"));
        assert_ne!(JStr::new("abcdefg"), JStr::new("abcdefh"));
    }

    #[test]
    fn ascii缓存只算一次() {
        let j = JStr::new("a-long-ascii-string-without-chinese");
        assert!(j.is_ascii());
        assert_eq!(j.char_count(), 35);
        assert_eq!(j.char_at(3), Some('o'));
        let k = JStr::new("a-long-string-with-中文-inside");
        assert!(!k.is_ascii());
        assert_eq!(k.char_count(), "a-long-string-with-中文-inside".chars().count());
        assert_eq!(k.char_at(20), "a-long-string-with-中文-inside".chars().nth(20));
    }
}
