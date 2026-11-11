//! JIT（M180 v0 → **M182 v1**）：把**热点循环**编成机器码。
//!
//! ## v1 编什么（v0 只编第一种）
//!
//! | 形态 | v0 | v1 |
//! |---|---|---|
//! | 整数 `当` 循环，条件是一个比较 | ✅ | ✅ |
//! | 循环体里的 `如果 / 否则 / 否则如果` | ❌ | ✅ |
//! | `中断` / `继续` | ❌ | ✅ |
//! | `遍历` **整数列表**（`范围()` 造的也算） | ❌ | ✅ |
//! | `//` `%`（**向下取整**语义，要修正 x86 的截断） | ❌ | ✅ |
//! | 浮点 / 文本 / 字典 / 函数调用 / 下标 / 属性 / `/` / `**` | ❌ | ❌ |
//!
//! ⚠️ **`/` 编不了，不是漏了**：基石里整数 `/` 的结果是**浮点**（`7 / 2` → `3.5`），
//! 一条整数循环编到一半突然出浮点，槽的类型就变了 —— 那要等 M183 的浮点 JIT。
//! 所以这一版只编 `//` 与 `%`（结果仍是整数）。
//!
//! ## 为什么先抽一份 IR（M182 的第一件事）
//!
//! v0 是「边认边发」：一个 `当` 循环、一个条件跳转，跳转补丁随手就打。一旦要加
//! 块结构（`如果`）和第二种循环（`遍历`），**两个后端各维护一套补丁表必然分叉** ——
//! 而 M184 还要加 ARM64。所以先把「认形状」和「发机器码」拆开：
//!
//! ```text
//! 字节码 ──认循环(前端)──▶ 指令[]（约 16 条的栈机）──生成(后端)──▶ 机器码
//! ```
//!
//! 🔴 **IR 只做「让两个后端共享」，不做优化**：没有寄存器分配（每个变量一个槽寄存器），
//! 没有公共子表达式消除。它一旦长出「方便优化」的指令，ARM 后端就得跟着实现一遍。
//!
//! ## 正确性靠三道闸（照旧）
//!
//! 1. **类型守卫**（进 JIT 之前）：循环用到的每个槽必须是「赋过值的 `整数`」且在 `i64` 内；
//!    `遍历` 还要「剩下那些元素全是 `i64` 整数」。任何一条不满足 → 不进 JIT。
//! 2. **去优化即不写回**：机器码里每条 `+ - *` 后面跟一条 `jo`，溢出就跳到出口**不写回**；
//!    `//` `%` 的除零、`i64::MIN / -1` 也走这条路（`idiv` 那两条是 **SIGFPE**，不是异常）。
//!    局部变量保持**进 JIT 之前**的样子，解释器从头再跑这个循环（它用 `i128`，不会溢出）。
//! 3. **失败即黑名单**：编不出来 / 跑不了就记成「这个循环别试了」。
//!
//! ## `遍历` 为什么能摊平（这一版的关键论证）
//!
//! `遍历 列表` 用的是 **`Val::ListIter`（活迭代器：共享列表 + 下标）**，不是快照 ——
//! 所以「先把元素拷出来」只在**循环体碰不到这个列表**时才成立。而这个条件是**编译期
//! 就能保证**的：我们的前端只认「取槽 / 取数 / 四则 / 比较 / 分支 / 存槽」，
//! **调用、下标、属性、方法一律不认** ⇒ 循环体根本没有能力改那个列表。
//!
//! 于是：进 JIT 时把 `list[idx..]` 摊平成 `[i64]`（`元素[0]` 是个数，元素从 `元素[1]`
//! 开始），机器码按索引读；跑完由**宿主**把迭代器从栈上弹掉（正常结束）或跳到
//! `SETUP_LOOP.b` 的清理块（`中断`）。迭代器的 `idx` 我们**一个字都不改** ——
//! 去优化时解释器还要从它接着走。
//!
//! ## 状态放在一块 `i64` 缓冲里（照旧）
//!
//! `Val` 是带标签的枚举，机器码要按它的内存布局构造值是**耦合到崩溃级**的
//! （加一个变体就悄悄错位）。所以进 JIT 前把槽摊平成 `[i64]`，跑完再写回。

// 这个模块里有一批只在 `jit` 特征 + x86-64 下才用到的助手（比如 `RCX` 常量、
// 各平台的 `归还`），关掉那种组合时它们就是死代码 —— 不值得为它们加两打 `cfg`。
#![allow(dead_code)]

use crate::op;
use crate::Val;
use std::collections::HashMap;

mod aarch64;
mod x86_64;

/// 这个构建/平台到底有没有 JIT。
///
/// ⚠️ 返回值是**编译期**决定的：非 x86-64 一律 `false`，`编译()` 永远给 `None`。
pub fn 支持() -> bool {
    cfg!(any(
        all(
            target_arch = "x86_64",
            any(target_os = "windows", target_os = "linux", target_os = "macos")
        ),
        // M184：ARM64 后端。**Windows-on-ARM 未测**（见模块文档的「未验」清单），
        // 所以这里不写 `target_os = "windows"` —— 少一个平台就少一句谎话。
        all(
            target_arch = "aarch64",
            any(target_os = "linux", target_os = "macos")
        ),
    ))
}

// ---------------------------------------------------------------------------
// 平台相关：可执行内存
// ---------------------------------------------------------------------------

/// 一块可执行内存。`Drop` 时还回去。
struct 内存块 {
    地: *mut u8,
    长: usize,
}

impl Drop for 内存块 {
    fn drop(&mut self) {
        归还(self.地, self.长);
    }
}

/// 申请一块**可读可写可执行**的内存。
///
/// ⚠️ 这是 W^X 上的妥协：写机器码的时候要可写，跑的时候要可执行。正规做法是
/// 「先 RW 写完再 mprotect 成 RX」，但那要多一次系统调用 + 一次跨页风险；
/// 这里内存是**我们自己刚从系统拿的整页**，写和跑的窗口极短。macOS 上
/// `MAP_JIT` 是 Apple Silicon 的硬要求（见 M183），x86-64 上直接
/// `PROT_EXEC` 也能过。
#[cfg(any(
    all(
        target_arch = "x86_64",
        any(target_os = "windows", target_os = "linux", target_os = "macos")
    ),
    all(target_arch = "aarch64", any(target_os = "linux", target_os = "macos")),
))]
fn 申请(长: usize) -> Option<内存块> {
    if 长 == 0 {
        return None;
    }
    #[cfg(windows)]
    {
        // VirtualAlloc(PAGE_EXECUTE_READWRITE)
        const PAGE_EXECUTE_READWRITE: u32 = 0x40;
        const MEM_COMMIT: u32 = 0x1000;
        const MEM_RESERVE: u32 = 0x2000;
        extern "system" {
            fn VirtualAlloc(地: *mut u8, 长: usize, 型: u32, 护: u32) -> *mut u8;
        }
        let p = unsafe { VirtualAlloc(std::ptr::null_mut(), 长, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE) };
        if p.is_null() {
            None
        } else {
            Some(内存块 { 地: p, 长 })
        }
    }
    #[cfg(not(windows))]
    {
        #[cfg(not(target_os = "macos"))]
        const MAP_ANON: i32 = 0x20;
        #[cfg(target_os = "macos")]
        const MAP_ANON: i32 = 0x1000;
        // 🔴 Apple Silicon 上**没有 MAP_JIT 就 mmap 不出可执行页**（返回 EPERM）。
        // x86-64 的 macOS 也认这个标志，所以两边一起带。
        #[cfg(target_os = "macos")]
        const MAP_JIT: i32 = 0x0800;
        #[cfg(not(target_os = "macos"))]
        const MAP_JIT: i32 = 0;
        const PROT_RWX: i32 = 7;      // READ | WRITE | EXEC
        const MAP_PRIVATE: i32 = 0x02;
        extern "C" {
            fn mmap(地: *mut u8, 长: usize, 护: i32, 旗: i32, 句: i32, 偏: i64) -> *mut u8;
        }
        let p = unsafe {
            mmap(std::ptr::null_mut(), 长, PROT_RWX, MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0)
        };
        // MAP_FAILED 是 -1
        if p as isize == -1 {
            None
        } else {
            Some(内存块 { 地: p, 长 })
        }
    }
}

#[cfg(any(
    all(
        target_arch = "x86_64",
        any(target_os = "windows", target_os = "linux", target_os = "macos")
    ),
    all(target_arch = "aarch64", any(target_os = "linux", target_os = "macos")),
))]
fn 归还(地: *mut u8, 长: usize) {
    // Windows 的 `VirtualFree` 只认 MEM_RELEASE（长度必须传 0），所以那边不用它
    let _ = 长;
    #[cfg(windows)]
    {
        const MEM_RELEASE: u32 = 0x8000;
        extern "system" {
            fn VirtualFree(地: *mut u8, 长: usize, 型: u32) -> i32;
        }
        unsafe { VirtualFree(地, 0, MEM_RELEASE); }
    }
    #[cfg(not(windows))]
    {
        extern "C" {
            fn munmap(地: *mut u8, 长: usize) -> i32;
        }
        unsafe { munmap(地, 长); }
    }
}

#[cfg(not(any(
    all(
        target_arch = "x86_64",
        any(target_os = "windows", target_os = "linux", target_os = "macos")
    ),
    all(target_arch = "aarch64", any(target_os = "linux", target_os = "macos")),
)))]
fn 申请(_长: usize) -> Option<内存块> {
    None
}

#[cfg(not(any(
    all(
        target_arch = "x86_64",
        any(target_os = "windows", target_os = "linux", target_os = "macos")
    ),
    all(target_arch = "aarch64", any(target_os = "linux", target_os = "macos")),
)))]
fn 归还(_地: *mut u8, _长: usize) {}

/// 允许 / 禁止往 `MAP_JIT` 那块内存里写。
///
/// 只在 **macOS + ARM64** 上有意义：Apple Silicon 的 `MAP_JIT` 页默认是
/// 「可执行不可写」，要写机器码得先把**本线程**的 JIT 写权限打开。
///
/// ⚠️ 这是 **per-thread** 状态：在哪个线程写就在哪个线程开。我们的 VM 是
/// 单线程跑脚本的，编译与执行同线程；写成「写前开、写完关」是为了不依赖这个前提。
#[inline]
fn jit写允许() {
    #[cfg(all(target_arch = "aarch64", target_os = "macos"))]
    unsafe {
        extern "C" {
            fn pthread_jit_write_protect_np(enabled: i32);
        }
        pthread_jit_write_protect_np(0);
    }
}

#[inline]
fn jit写禁止() {
    #[cfg(all(target_arch = "aarch64", target_os = "macos"))]
    unsafe {
        extern "C" {
            fn pthread_jit_write_protect_np(enabled: i32);
        }
        pthread_jit_write_protect_np(1);
    }
}

/// 写完机器码之后刷**指令缓存**。
///
/// 🔴 ARM64 上这一步**不能省**：I-cache 与 D-cache 不保证一致，刚写进去的字节
/// 可能还没进指令流水线 —— 表现是「跑的是旧代码」或「跑到半条指令」，而且
/// 只在真机上偶发（QEMU 常常掩盖它）。x86 有硬件一致性，这里是空操作。
#[inline]
fn 刷指令缓存(地: *mut u8, 长: usize) {
    #[cfg(all(target_arch = "aarch64", target_os = "macos"))]
    unsafe {
        extern "C" {
            fn sys_icache_invalidate(start: *const u8, len: usize);
        }
        sys_icache_invalidate(地, 长);
    }
    #[cfg(all(target_arch = "aarch64", not(target_os = "macos")))]
    unsafe {
        extern "C" {
            fn __clear_cache(begin: *mut u8, end: *mut u8);
        }
        __clear_cache(地, 地.add(长));
    }
    #[cfg(not(target_arch = "aarch64"))]
    {
        let _ = (地, 长);
    }
}

// ---------------------------------------------------------------------------
// 中间表示（IR）：一台「机器栈 + 槽」的小栈机
// ---------------------------------------------------------------------------

/// 二元运算。⚠️ 没有 `**`（要快速幂）。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(super) enum 运算 {
    加,
    减,
    乘,
    /// **真除法**：基石里整数 `/` 也出浮点（`7 / 2 == 3.5`），所以它**永远是浮点运算**
    除法,
    /// 向下取整的整除（`-7 // 3 == -3`）—— 只有整数版
    整除,
    /// 向下取整的取模（`-7 % 3 == 2`）—— 只有整数版
    取模,
}

/// 一个槽 / 一个中间值是**整数**还是**浮点**（M183）。
///
/// 为什么必须在编译期定死：整数的加法是 `add rax, rdx` + `jo`，浮点的是
/// `addsd xmm4, xmm5` —— 两条完全不同的机器码，靠运行时判断就没意义了。
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum 类型 {
    整数,
    浮点,
}

/// 比较。顺序与字节码的 `CMPOP_*` 一致。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(super) enum 比较 {
    等,
    不等,
    小于,
    大于,
    小于等,
    大于等,
}

/// 一个操作数：某个槽寄存器，或者一个字面量。
#[derive(Clone, Copy)]
pub(super) enum 源 {
    槽(u8),
    数(i64),
}

/// IR 指令。
///
/// 设计口径：**跟字节码同构，但不含 `Val` / 宿主**。栈就是真的机器栈
/// （`push` / `pop`），所以「任意嵌套表达式」不需要寄存器分配。
///
/// 两条**融合**形态（`算存` / `比跳`）是给常见语句走的捷径：它们不碰栈，
/// 实测比栈式快一倍（5000 万次累加 118 ms → 59 ms）。融合由 [`压扁`] 做，
/// 坏了也只影响快慢，不影响语义。
#[derive(Clone, Copy)]
pub(super) enum 指令 {
    /// 打一个标签（落点）
    标(u16),
    /// 无条件跳
    跳(u16),
    /// 弹出栈顶，为 0 就跳（`如果` 的「假则跳」）
    零跳(u16),
    /// 弹出栈顶，非 0 就跳
    非零跳(u16),
    /// `遍历`：索引 ≥ 个数就跳（走正常出口）
    越界跳(u16),
    /// 把槽推进栈
    压槽(u8),
    /// 把整数字面量推进栈
    压数(i64),
    /// 把浮点字面量推进栈（`f64::to_bits` 的 64 位原样压进去）
    压浮点(u64),
    /// `遍历`：把「当前索引处的元素」推进栈
    压元素,
    /// `遍历`：索引 + 1
    步进,
    /// 弹出两个、算一下、把结果推回去
    ///
    /// `左转` / `右转`：这一侧的操作数是不是**整数要转成浮点**（混合运算）。
    /// 转换在机器码里做（`cvtsi2sd`），所以 IR 不用插额外的「转浮点」指令。
    二元 {
        运算: 运算,
        类型: 类型,
        左转: bool,
        右转: bool,
    },
    /// 弹出、取负、推回
    取负,
    /// 弹出、取负（浮点：`xorpd` 翻符号位）、推回
    取负浮点,
    /// 弹出、存进槽
    存槽(u8),
    /// 弹出、扔掉（表达式语句多余的值）
    丢弃,
    /// 弹出两个比较，把 `0` / `1` 推回栈
    ///
    /// 浮点的比较**不能**直接 `setcc`：`ucomisd` 在 NaN 上置 `PF=1`，而
    /// `sete`/`setb` 会把「无序」当成相等/小于。后端会按比较码补 `setnp`
    /// （`==` / `<` / `<=`）或 `setp`（`!=`）。见 `x86_64::发浮点比`。
    比压 {
        比较: 比较,
        类型: 类型,
        左转: bool,
        右转: bool,
    },
    /// `槽 = 左 <运算> 右` —— 不碰栈
    算存(源, 运算, 源, u8),
    /// `左 <比较> 右` 为**假**时跳（不碰栈）
    比跳(源, 比较, 源, u16),
    /// `左 <比较> 右` 为**真**时跳（不碰栈）
    比真跳(源, 比较, 源, u16),
}

// 保留标签：每个后端都认这几号，IR 里的跳转指向它们。
/// 循环头（`当` 的条件 / `遍历` 的「查个数 + 取元素」）
pub(super) const 标头: u16 = 0;
/// 正常出口（跑完整个循环）
pub(super) const 标出: u16 = 1;
/// 去优化（不写回，交回解释器重跑）
pub(super) const 标优: u16 = 2;
/// `中断` 出口（跳过循环剩下的部分，但**要写回**）
pub(super) const 标断: u16 = 3;
/// `继续` 的落点（`遍历` 要在这儿做「索引 + 1」）
pub(super) const 标续: u16 = 4;
/// 字节码下标用的标签从这一号起：`标签基 + (ip - 起)`
pub(super) const 标签基: u16 = 8;

/// 一个变量槽到底在哪儿。
///
/// 函数里的变量是**局部槽**（`LOAD_FAST`），**模块顶层**的是**全局槽**
/// （`LOAD_GLOBAL`）—— 脚本和示例里的循环大多写在顶层，只认局部的话 JIT
/// 对它们一点用都没有。
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum 槽位 {
    局部(usize),
    全局(usize),
    /// **只活在机器码里**的临时槽（M191，内联小函数时用来存实参）。
    ///
    /// 🔴 它不对应任何 VM 变量：运行期**既不读也不写回**（`试jit` 的取值循环与
    /// `写回jit槽` 都要跳过）。混进写回就是把一个凭空的值塞进用户的变量。
    临时(usize),
}

/// 一个能编的循环。
pub(super) struct 方案 {
    指令们: Vec<指令>,
    /// 用到的槽（顺序 = 机器寄存器 r8/r9/r10/r11 的顺序）。
    槽们: Vec<槽位>,
    /// 正常跑完时解释器接着跑的 ip。
    出口: usize,
    /// `中断` 时解释器跳到的 ip（`当` = 出口；`遍历` = `SETUP_LOOP.b` 的清理块）。
    中断口: usize,
    /// 是不是 `遍历` 循环（决定要不要摊平元素、要不要弹迭代器）。
    是遍历: bool,
    /// 每个槽的类型（顺序与 `槽们` 一致）。
    槽型: Vec<类型>,
    /// 标签总数（后端据此建表）。
    标数: usize,
    /// **内联调用点**：`(全局槽号, 编译期绑定的函数 id)`（M191）。
    /// 运行期每次进循环前要核对 `globals[k]` 仍指向那个函数 ——
    /// 基石里函数是值，能被重新赋值，换掉了就必须退回解释器
    /// （否则算出来的东西完全不对，而且**不会报错**）。
    调用点: Vec<(usize, usize)>,
}

/// 寄存器编号 ↔ 槽：同一个槽出现多次时复用同一个寄存器。
#[derive(Default)]
struct 分配 {
    表: Vec<槽位>,
}

impl 分配 {
    /// 给一个槽分配寄存器。超过 4 个就给 `None`（只备了 `r8`–`r11` —— 它们
    /// **不必**被调用者保存，省掉返回路径上的 `pop`）。
    fn 取(&mut self, 槽: 槽位) -> Option<u8> {
        if let Some(i) = self.表.iter().position(|x| *x == 槽) {
            return Some(i as u8);
        }
        if self.表.len() >= 4 {
            return None;
        }
        self.表.push(槽);
        Some(self.表.len() as u8 - 1)
    }
}

fn 取整(常量们: &[Val], 下标: i64) -> Option<i64> {
    match 常量们.get(usize::try_from(下标).ok()?) {
        // `i64` 装不下就不能编：机器码只做 64 位算术，而那部分由解释器负责。
        Some(Val::Int(v)) => i64::try_from(*v).ok(),
        _ => None,
    }
}

/// 字节码里的二元运算码 → IR 的运算（`/` 与 `**` 不认）。
fn 认运算(码: i64) -> Option<运算> {
    match 码 {
        0 => Some(运算::加),
        1 => Some(运算::减),
        2 => Some(运算::乘),
        // ⚠️ `/` 在基石里**出浮点**（`7 / 2 == 3.5`），所以它只在浮点域里编
        3 => Some(运算::除法),
        4 => Some(运算::整除),
        5 => Some(运算::取模),
        _ => None,
    }
}

fn 认比较(码: i64) -> Option<比较> {
    match 码 {
        0 => Some(比较::等),
        1 => Some(比较::不等),
        2 => Some(比较::小于),
        3 => Some(比较::大于),
        4 => Some(比较::小于等),
        5 => Some(比较::大于等),
        _ => None,
    }
}

/// 把字节码 ip 映射成标签号。
///
/// - 跳出循环范围（`>` 终）⇒ 正常出口；
/// - `遍历` 的清理块 ⇒ `中断` 出口；
/// - 循环范围内 ⇒ `标签基 + (ip - 起)`；
/// - 其余（比如跳进别的循环、跳到函数开头）⇒ `None`（这个循环不编）。
fn 标号(ip: usize, 起: usize, 终: usize, 遍历: bool, 中断口: usize) -> Option<u16> {
    if ip > 终 {
        return Some(标出);
    }
    if 遍历 && ip == 中断口 {
        return Some(标断);
    }
    if ip >= 起 {
        return Some(标签基 + (ip - 起) as u16);
    }
    None
}

/// 尝试把 `[起, 终]` 认成一个能编的循环。认不出来就给 **为什么**。
///
/// `终` 是那条回跳 `JUMP` 的下标（含）；`起` 是它跳回去的抬头。
///
/// 📌 返回理由而不是 `None`：`JISHI_JIT=调试` 要能告诉用户「这个循环为什么没编」
/// ——「没变快」是 JIT 最难查的一类问题，光说「不支持」等于没说。
/// 前端的定型状态：**操作数栈上每一格是什么类型** + 每个槽定成什么类型。
///
/// 为什么要「栈型」：`i + 2.5` 里 `i` 是整数槽，得在机器码里 `cvtsi2sd` 转一下。
/// 光看槽的类型不够 —— 决定一条运算是什么类型的，是**它的两个操作数**。
struct 前端 {
    出: Vec<指令>,
    栈: Vec<类型>,
    槽型: HashMap<槽位, 类型>,
    分配: 分配,
}

impl 前端 {
    fn 压(&mut self, t: 类型) {
        self.栈.push(t);
    }
    fn 弹(&mut self) -> Option<类型> {
        self.栈.pop()
    }
    fn 取槽(&mut self, 位: 槽位) -> Result<u8, &'static str> {
        self.分配.取(位).ok_or("这个循环用了超过 4 个变量")
    }
    /// 给一个槽定类型。**同一个槽在一个循环里只许一种类型** ——
    /// 一会儿整数一会儿浮点，机器码里那个寄存器就没法选了。
    fn 定槽(&mut self, 位: 槽位, t: 类型) -> Result<(), &'static str> {
        match self.槽型.get(&位) {
            Some(旧) if *旧 != t => {
                Err("同一个变量在循环里换了类型（整数 ↔ 浮点）—— 这种循环交给解释器")
            }
            _ => {
                self.槽型.insert(位, t);
                Ok(())
            }
        }
    }
}

/// 把 `[起, 终]` 里**引用到的**槽都找出来（`LOAD_*` / `STORE_*`）。
///
/// 用来给前端「喂类型提示」：编译要发生在类型守卫之前，而槽的类型只有运行时
/// 才知道（`令 s = 0.0` 写在循环外面）。所以先在宿主那边把当前值的类型扫出来，
/// 再带着提示去编译；真进 JIT 之前由守卫**逐槽核对**，不一致就不进。
pub fn 扫槽(ins: &[crate::Instr], 起: usize, 终: usize) -> Vec<槽位> {
    let mut 出: Vec<槽位> = Vec::new();
    let mut 加 = |位: 槽位| {
        if !出.contains(&位) {
            出.push(位)
        }
    };
    for x in ins.get(起..终).unwrap_or(&[]) {
        match x.op {
            op::LOAD_FAST | op::STORE_FAST if x.a >= 0 => 加(槽位::局部(x.a as usize)),
            op::LOAD_GLOBAL | op::STORE_GLOBAL if x.a >= 0 => 加(槽位::全局(x.a as usize)),
            _ => {}
        }
    }
    出
}

/// 尝试把 `[起, 终]` 认成一个能编的循环。认不出来就给 **为什么**。
///
/// `提示` = 这些槽**当前值的类型**（宿主在编译前扫的）。没提示到的槽按整数算
/// —— 真值若是浮点，守卫会拒掉（宁可走解释器，不许编错）。
/// 一个「可以内联进循环体」的小函数（M191）。
///
/// 只在**编译期**用：`认循环` 把它的步们直接展开成 IR，机器码里既没有 `call`
/// 也没有帧 —— 这是「函数调用密集」那个场景唯一能快到 1.5x 的办法
/// （真发一条调用要建帧、存参、判溢出、返回，比 CPython 的特化调用还贵）。
pub struct 内联体 {
    /// 编译期绑定的那个函数的 `Func.id` —— 运行期守卫拿它比
    /// 「`globals[k]` 还是不是同一个函数」。
    pub 函数id: usize,
    参数数: usize,
    步们: Vec<内联步>,
}

impl 内联体 {
    /// 形参个数（`认循环` 要拿它核对 `CALL` 的实参个数）。
    pub fn 参数数(&self) -> usize {
        self.参数数
    }
}

/// 内联体里的一步：跟 IR 的「压多少、算什么」一一对应。
#[derive(Clone, Copy)]
enum 内联步 {
    参(usize),
    数(i64),
    算(运算),
}

/// 认一个函数能不能**原地内联**（M191 的第一道闸）。
///
/// ## 只认什么
///
/// 形态必须是「**一条 `返回 <纯表达式>`**」：
///
/// ```text
/// LOAD_FAST/LOAD_CONST …   ← 只允许压操作数（参数 / 整数常量）
/// BIN_OP {+ - * // %}      ← 只允许四则（不含 `/`，它在基石里出浮点）
/// RETURN
/// ```
///
/// 参数最多 **2** 个（每个占一个临时槽，而槽寄存器一共只有 4 个 ——
/// 循环自己还要用掉一两个）。
///
/// ## 为什么这么窄
///
/// 内联是**把别人的字节码搬进自己的帧**，搬进来的每一条都得能在这台栈机上表达。
/// 只要出现 `STORE_FAST`（有局部变量）、跳转（有分支）、`CALL`（嵌套调用）、
/// 属性/下标（要动堆），就得先解决「槽分配 / 标签重编号 / 堆屏障」——
/// 那是另一站的事。**认不出来就不编**，退回解释器，语义一点不变。
pub fn 可内联(
    指令们: &[crate::Instr],
    常量们: &[Val],
    参数数: usize,
    函数id: usize,
) -> Option<内联体> {
    if 参数数 == 0 || 参数数 > 2 {
        return None;
    }
    let 码 = 有效指令(指令们, 常量们);
    if 码.len() < 2 || 码.last()?.op != op::RETURN {
        return None;
    }
    let mut 步们 = Vec::with_capacity(码.len() - 1);
    let mut 深度: i32 = 0;
    for x in &码[..码.len() - 1] {
        match x.op {
            op::LOAD_FAST => {
                let i = usize::try_from(x.a).ok()?;
                if i >= 参数数 {
                    return None; // 引用了不存在的形参（不该发生，但别猜）
                }
                步们.push(内联步::参(i));
                深度 += 1;
            }
            op::LOAD_CONST => {
                let v = 取整(常量们, x.a)?;
                if v < i32::MIN as i64 || v > i32::MAX as i64 {
                    // 常量太大要 movz+movk 四条；理论上能编，但这一版不收
                    return None;
                }
                步们.push(内联步::数(v));
                深度 += 1;
            }
            op::BIN_OP => {
                // 只认整数四则：`/` 在基石里是浮点运算，`//` `%` 是整数
                let 算 = 认运算(x.a)?;
                if 算 == 运算::除法 {
                    return None;
                }
                if 深度 < 2 {
                    return None;
                }
                步们.push(内联步::算(算));
                深度 -= 1;
            }
            _ => return None,
        }
    }
    if 深度 != 1 {
        return None; // 返回值之外还有半截表达式 / 根本没返回值
    }
    Some(内联体 { 函数id, 参数数, 步们 })
}

fn 认循环(
    ins: &[crate::Instr],
    常量们: &[Val],
    起: usize,
    终: usize,
    提示: &HashMap<槽位, 类型>,
    内联表: &HashMap<usize, 内联体>,
) -> Result<方案, &'static str> {
    let 跳 = ins.get(终).ok_or("回跳那条指令越界")?;
    if 跳.op != op::JUMP || 跳.a < 0 || 跳.a as usize != 起 {
        return Err("这条回边不是跳回抬头的 JUMP");
    }
    if 终 <= 起 {
        return Err("循环只有一条回跳（空循环）");
    }
    let 首 = ins.get(起).ok_or("抬头越界")?;
    let 遍历 = 首.op == op::FOR_ITER;

    let mut 前 = 前端 { 出: Vec::new(), 栈: Vec::new(), 槽型: 提示.clone(), 分配: 分配::default() };
    let mut 出口: Option<usize> = None;
    let mut 有比较出口 = false;       // 出口必须来自「比较」——`当 真` 那种不编
    let mut 中断口: usize = 0;
    let mut i: usize;
    // 哪些 ip 是跳转落点（只有它们需要 `标`）
    let mut 是目标 = vec![false; 终 - 起 + 1];

    // 🔴 **只给「真的有人跳过去」的 ip 打标签**（M182 的第二个真 bug）。
    // 第一版给每条字节码都打，结果 `压扁` 的融合形态全失效 ——
    // 融合要求 `[取,取,运算,存槽]` **相邻**，中间插一个 `标` 就再也认不出来
    // （症状：正确但慢，5000 万次累加 58 ms 退到 131 ms）。
    是目标[0] = true;
    for k in 起..终 {
        let 目 = match ins.get(k) {
            Some(x) => match x.op {
                op::JUMP | op::POP_JUMP_IF_FALSE | op::POP_JUMP_IF_TRUE => x.a,
                _ => continue,
            },
            None => continue,
        };
        if 目 >= 0 && (目 as usize) <= 终 && (目 as usize) >= 起 {
            是目标[目 as usize - 起] = true;
        }
    }

    // 循环头的标签：`当` 的条件、`遍历` 的「查个数 + 取元素」，回跳都到这儿
    前.出.push(指令::标(标头));

    if 遍历 {
        // `遍历` 的形状（见 `frontend/src/compiler.rs` 的 `c_for`）：
        //   GET_ITER / SETUP_LOOP <top> / top: FOR_ITER <exit> / STORE … / JUMP top
        //
        // ⚠️ 循环头**必须**在「查个数 + 取元素」之前：回跳回来要重新查一次，
        //    否则回跳就等于原地打转。
        let 设 = ins.get(起.checked_sub(1).ok_or("抬头在最前面，不可能是 遍历")?)
            .ok_or("抬头前面没有 SETUP_LOOP")?;
        if 设.op != op::SETUP_LOOP || 设.a < 0 || 设.a as usize != 起 {
            return Err("抬头前面不是指向它的 SETUP_LOOP（不是 遍历 循环）");
        }
        if 设.b < 0 {
            return Err("SETUP_LOOP 没带清理块（`中断` 无路可走）");
        }
        中断口 = 设.b as usize;
        let 出ip = usize::try_from(首.a).map_err(|_| "FOR_ITER 的出口是负数")?;
        if 出ip <= 终 {
            return Err("FOR_ITER 的出口在循环里面");
        }
        出口 = Some(出ip);
        前.出.push(指令::越界跳(标出));
        前.出.push(指令::压元素);
        // 元素恒为整数（浮点列表这一版不编，见模块文档）
        前.压(类型::整数);
        i = 起 + 1;
    } else {
        i = 起;
    }

    // M191：`LOAD_GLOBAL`（内联表点名）与它的 `CALL` 之间的握手。
    // 两条指令之间不能有别的动作 —— 有就说明这个「调用点」不是直接调用。
    let mut 待内联: Option<usize> = None;
    // 内联点的全局槽号（运行期要核对 `globals[k]` 仍指向同一个函数 = 单态守卫）
    let mut 调用点: Vec<(usize, usize)> = Vec::new();

    while i < 终 {
        if 是目标[i - 起] {
            前.出.push(指令::标(标签基 + (i - 起) as u16));
        }
        let x = ins.get(i).ok_or("循环体越界")?;
        match x.op {
            // ── M191：内联一个「小函数」的调用点 ──
            //
            // `加一(s)` 编译成 `LOAD_GLOBAL k; LOAD_FAST s; CALL 1`。
            // 这里**不物化函数值**（机器码里没有「函数」这种值），只在类型栈上
            // 占一个位，等下面 `CALL` 把实参搬进临时槽、把被调者的表达式展开。
            op::LOAD_GLOBAL
                if x.a >= 0 && 内联表.contains_key(&(x.a as usize)) =>
            {
                // ⚠️ **不能要求「紧跟 CALL」**：字节码的顺序是
                // `LOAD_GLOBAL f; <实参们>; CALL n` —— 实参夹在中间。
                // 这里只登记「接下来这个调用要用内联」，占一个栈位；
                // 真到 `CALL` 那一步才展开（那时才知道实参个数）。
                if 待内联.is_some() {
                    return Err("连着两个内联调用（嵌套调用这一版不编）");
                }
                待内联 = Some(x.a as usize);
                前.压(类型::整数); // 占位：与 M190 的 LOAD_GLOBAL 同理，不物化但要占栈位
                i += 1;
            }
            op::CALL => {
                let 槽号 = 待内联.take().ok_or(
                    "循环体里有 JIT 不认识的调用（只内联「一条 返回 表达式」的小函数）",
                )?;
                let 体 = 内联表.get(&槽号).ok_or("内联表的键对不上")?;
                let 实参 = usize::try_from(x.a).map_err(|_| "实参个数是负数")?;
                if 实参 != 体.参数数 {
                    return Err("实参个数和形参个数不一样（调用点与函数不匹配）");
                }
                调用点.push((槽号, 体.函数id));
                // 弹实参（栈顶是最后一个）+ 弹那个占位
                for _ in 0..实参 {
                    let t = 前.弹().ok_or("调用前面实参不够")?;
                    if t != 类型::整数 {
                        return Err("内联只支持整数实参");
                    }
                }
                if 前.弹().is_none() {
                    return Err("调用前面没有函数值");
                }
                // 实参搬到临时槽：栈顶是最后一个实参，所以要倒着搬
                for idx in (0..实参).rev() {
                    let k = 前.取槽(槽位::临时(idx))?;
                    前.出.push(指令::存槽(k));
                }
                // 展开被调者的表达式（参数 → 临时槽，常量 → 压数，运算 → 二元）
                for 步 in 体.步们.iter().copied() {
                    match 步 {
                        内联步::参(i) => {
                            let k = 前.取槽(槽位::临时(i))?;
                            前.出.push(指令::压槽(k));
                        }
                        内联步::数(v) => 前.出.push(指令::压数(v)),
                        内联步::算(op) => 前.出.push(指令::二元 {
                            运算: op,
                            类型: 类型::整数,
                            左转: false,
                            右转: false,
                        }),
                    }
                }
                前.压(类型::整数); // 返回值已经压在栈上
                i += 1;
            }
            op::LOAD_FAST | op::LOAD_GLOBAL => {
                if x.a < 0 {
                    return Err("变量槽下标是负数");
                }
                let 位 = if x.op == op::LOAD_FAST {
                    槽位::局部(x.a as usize)
                } else {
                    槽位::全局(x.a as usize)
                };
                let t = 前.槽型.get(&位).copied().unwrap_or(类型::整数);
                let k = 前.取槽(位)?;
                前.出.push(指令::压槽(k));
                前.压(t);
                i += 1;
            }
            op::LOAD_CONST => {
                let c = 常量们
                    .get(usize::try_from(x.a).map_err(|_| "常量下标是负数")?)
                    .ok_or("常量下标越界")?;
                match c {
                    Val::Int(v) => {
                        let v = i64::try_from(*v).map_err(|_| "常量不是 i64 范围内的整数")?;
                        前.出.push(指令::压数(v));
                        前.压(类型::整数);
                    }
                    Val::Float(f) => {
                        前.出.push(指令::压浮点(f.to_bits()));
                        前.压(类型::浮点);
                    }
                    _ => return Err("常量不是整数也不是浮点（精确小数 / 文本 / 空 都不编）"),
                }
                i += 1;
            }
            op::BIN_OP => {
                let 右 = 前.弹().ok_or("二元运算前面栈不够深")?;
                let 左 = 前.弹().ok_or("二元运算前面栈不够深")?;
                let 算 = 认运算(x.a).ok_or("运算不支持（只编 + - * / // %）")?;
                let 整侧 = 左 == 类型::整数 && 右 == 类型::整数;
                match 算 {
                    // `/` 在基石里**总是浮点**（`7 / 2 == 3.5`）—— 两边是整数也要转
                    运算::除法 => {
                        前.出.push(指令::二元 {
                            运算: 算,
                            类型: 类型::浮点,
                            左转: 左 == 类型::整数,
                            右转: 右 == 类型::整数,
                        });
                        前.压(类型::浮点);
                    }
                    // `//` `%` 这一版只编整数（浮点要 floor / fmod，M183 不做）
                    运算::整除 | 运算::取模 => {
                        if !整侧 {
                            return Err("浮点的 // 和 % 还没编（这一版只编浮点的 + - * /）");
                        }
                        前.出.push(指令::二元 {
                            运算: 算,
                            类型: 类型::整数,
                            左转: false,
                            右转: false,
                        });
                        前.压(类型::整数);
                    }
                    _ => {
                        if 整侧 {
                            前.出.push(指令::二元 {
                                运算: 算,
                                类型: 类型::整数,
                                左转: false,
                                右转: false,
                            });
                            前.压(类型::整数);
                        } else {
                            前.出.push(指令::二元 {
                                运算: 算,
                                类型: 类型::浮点,
                                左转: 左 == 类型::整数,
                                右转: 右 == 类型::整数,
                            });
                            前.压(类型::浮点);
                        }
                    }
                }
                i += 1;
            }
            op::UNARY_OP if x.a == 0 => {
                let t = 前.弹().ok_or("取负前面没有操作数")?;
                if t == 类型::整数 {
                    前.出.push(指令::取负);
                } else {
                    前.出.push(指令::取负浮点);
                }
                前.压(t);
                i += 1;
            }
            op::STORE_FAST | op::STORE_GLOBAL => {
                if x.a < 0 {
                    return Err("变量槽下标是负数");
                }
                let t = 前.弹().ok_or("赋值前面没有值")?;
                 let 位 = if x.op == op::STORE_FAST {
                    槽位::局部(x.a as usize)
                } else {
                    槽位::全局(x.a as usize)
                };
                let k = 前.取槽(位)?;
                前.定槽(位, t)?;
                前.出.push(指令::存槽(k));
                i += 1;
            }
            op::POP_TOP => {
                if 前.弹().is_none() {
                    return Err("丢弃前面没有值");
                }
                前.出.push(指令::丢弃);
                i += 1;
            }
            op::COMPARE => {
                let 右 = 前.弹().ok_or("比较前面栈不够深")?;
                let 左 = 前.弹().ok_or("比较前面栈不够深")?;
                // 🔴 **比较的结果只能直接用在分支上**：`x = (a < b)` 会把布尔值
                // 存进槽，而机器码只有整数/浮点 —— 那样循环结束后槽里会变成
                // `0/1` 而不是 `真/假`（`类型()` 与 `打印` 都看得见）。
                let 跟 = ins.get(i + 1).ok_or("比较后面没指令")?;
                if 跟.op != op::POP_JUMP_IF_FALSE && 跟.op != op::POP_JUMP_IF_TRUE {
                    return Err("比较的结果被存起来了（布尔值不在 JIT 的类型里）");
                }
                let 浮 = 左 == 类型::浮点 || 右 == 类型::浮点;
                前.出.push(指令::比压 {
                    比较: 认比较(x.a).ok_or("比较码不认识")?,
                    类型: if 浮 { 类型::浮点 } else { 类型::整数 },
                    左转: 浮 && 左 == 类型::整数,
                    右转: 浮 && 右 == 类型::整数,
                });
                // 布尔不参与算术：压一个占位类型，只有 `零跳/非零跳` 认它
                前.压(类型::浮点);
                i += 1;
            }
            op::POP_JUMP_IF_FALSE | op::POP_JUMP_IF_TRUE => {
                if x.a < 0 {
                    return Err("跳转目标是负数");
                }
                if 前.弹().is_none() {
                    return Err("条件跳转前面没有值");
                }
                let 目标 = x.a as usize;
                let 标 = 标号(目标, 起, 终, 遍历, 中断口)
                    .ok_or("跳到了循环外面（不是出口也不是清理块）")?;
                if 目标 > 终 {
                    if 出口.is_some() && !遍历 {
                        return Err("出现了第二个「跳出循环」的分支");
                    }
                    if ins.get(i.wrapping_sub(1)).map(|p| p.op) != Some(op::COMPARE) {
                        return Err("循环条件不是比较（`当 真` 那种不编）");
                    }
                    出口 = Some(目标);
                    有比较出口 = true;
                }
                前.出.push(if x.op == op::POP_JUMP_IF_FALSE {
                    指令::零跳(标)
                } else {
                    指令::非零跳(标)
                });
                i += 1;
            }
            op::JUMP => {
                if x.a < 0 {
                    return Err("跳转目标是负数");
                }
                let 标 = 标号(x.a as usize, 起, 终, 遍历, 中断口)
                    .ok_or("跳到了循环外面（不是出口也不是清理块）")?;
                前.出.push(指令::跳(标));
                i += 1;
            }
            op::CONTINUE_LOOP => {
                前.出.push(指令::跳(if 遍历 { 标续 } else { 标头 }));
                i += 1;
            }
            op::BREAK_LOOP => {
                前.出.push(指令::跳(if 遍历 { 标断 } else { 标出 }));
                i += 1;
            }
            // 函数调用 / 属性 / 下标 / 嵌套循环（`SETUP_LOOP`）/ 异常 … 一律不认。
            _ => return Err("循环体里有 JIT 不认识的指令（调用 / 下标 / 属性 / 嵌套循环 / 异常…）"),
        }
    }

    if 待内联.is_some() {
        // `LOAD_GLOBAL` 挂了内联的号，但它的 `CALL` 没出现（被分支跳过去了？）
        return Err("内联调用点的 CALL 不见了");
    }
    if !前.栈.is_empty() {
        return Err("循环体结束时栈不平（有半截表达式）");
    }
    let 出口 = 出口.ok_or("没找到循环出口")?;
    if !遍历 && !有比较出口 {
        return Err("循环条件不是比较");
    }

    let 中断 = if 遍历 { 中断口 } else { 出口 };

    if 遍历 {
        前.出.push(指令::标(标签基 + (终 - 起) as u16));
        前.出.push(指令::标(标续));
        前.出.push(指令::步进);
        前.出.push(指令::跳(标头));
    } else {
        // 🔴 `如果 …:` 的假分支常常**正好落在回跳那条 `JUMP` 上**（ip == 终）——
        // 这个标签必须打，否则那条跳转没有落点。
        前.出.push(指令::标(标签基 + (终 - 起) as u16));
        前.出.push(指令::跳(标头));
    }

    let 槽们: Vec<槽位> = 前.分配.表.clone();
    let 槽型: Vec<类型> = 槽们
        .iter()
        .map(|位| 前.槽型.get(位).copied().unwrap_or(类型::整数))
        .collect();
    let 标数 = 标签基 as usize + (终 - 起) + 1;
    Ok(方案 {
        指令们: 压扁(&前.出),
        槽们,
        槽型,
        出口,
        中断口: 中断,
        是遍历: 遍历,
        标数,
        调用点,
    })
}

fn 当源(x: 指令) -> Option<源> {
    match x {
        指令::压槽(k) => Some(源::槽(k)),
        指令::压数(v) => Some(源::数(v)),
        _ => None,
    }
}

/// 局部优化：把「三个操作数 + 融合形态」压成不碰栈的指令。
///
/// 只压三种**恰好**是「整个表达式就是一个二元运算/一个比较」的形态 ——
/// 中途栈上从来没出现过第三个值，所以既不需要寄存器分配，也不用判断嵌套。
///
/// ⚠️ **只压整数**：浮点的 `算存` / `比跳` 要另写一套 XMM 的版本，而浮点的
/// 栈式开销本来就不是瓶颈（SSE 的寄存器压力比整数小得多）。少一套融合形态，
/// 就少一处 NaN 语义写错的地方。
fn 压扁(步们: &[指令]) -> Vec<指令> {
    let 取 = |k: usize| 步们.get(k).copied();
    let mut 出 = Vec::with_capacity(步们.len());
    let mut i = 0;
    while i < 步们.len() {
        if let (Some(a), Some(b)) = (当源(步们[i]), 取(i + 1).and_then(当源)) {
            if let Some(指令::二元 { 运算: op, 类型: 类型::整数, 左转: false, 右转: false }) =
                取(i + 2)
            {
                if matches!(op, 运算::加 | 运算::减 | 运算::乘) {
                    if let Some(指令::存槽(k)) = 取(i + 3) {
                        出.push(指令::算存(a, op, b, k));
                        i += 4;
                        continue;
                    }
                }
            }
            if let Some(指令::比压 { 比较: c, 类型: 类型::整数, 左转: false, 右转: false }) =
                取(i + 2)
            {
                match 取(i + 3) {
                    Some(指令::零跳(标)) => {
                        出.push(指令::比跳(a, c, b, 标));
                        i += 4;
                        continue;
                    }
                    Some(指令::非零跳(标)) => {
                        出.push(指令::比真跳(a, c, b, 标));
                        i += 4;
                        continue;
                    }
                    _ => {}
                }
            }
        }
        出.push(步们[i]);
        i += 1;
    }
    出
}

// ---------------------------------------------------------------------------
// 编好的循环
// ---------------------------------------------------------------------------

/// 跑一次的结果。**这三个值决定宿主接下来干什么**，别合并。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum 结果 {
    /// 整个循环跑完了（`当`：条件为假；`遍历`：元素耗尽）。
    跑完,
    /// 中途去优化（溢出 / 除零 / 类型不合）—— 槽**没写回**，解释器从头重跑。
    去优化,
    /// 撞上 `中断`。
    中断,
}

/// 生成码的签名：`(槽缓冲, 元素缓冲) -> 结果码`。
///
/// 第二个参数只有 `遍历` 用：`[个数, 元素0, 元素1, …]`（第 0 个是个数）。
/// 其它循环传 `&[]`（生成码不会碰它）。
type 入口 = extern "C" fn(*mut i64, *const i64) -> i64;

/// 一段编好的循环。
pub struct 已编 {
    槽们: Vec<槽位>,
    槽型: Vec<类型>,
    出口: usize,
    中断口: usize,
    是遍历: bool,
    /// 内联调用点：`(全局槽号, 编译期绑定的函数 id)`（M191 的单态守卫）。
    调用点: Vec<(usize, usize)>,
    入口: 入口,
    /// 只是为了让内存在 `Drop` 时还回去（`入口` 指着它）。
    _块: 内存块,
}

impl 已编 {
    /// 这个循环用到的槽（顺序 = 摊平缓冲里各元素的位置）。
    pub fn 槽们(&self) -> &[槽位] {
        &self.槽们
    }

    /// 每个槽的类型（顺序与 `槽们` 一致）。宿主据此摊平 / 写回。
    pub fn 槽型(&self) -> &[类型] {
        &self.槽型
    }

    /// 内联调用点：`(全局槽号, 编译期绑定的函数 id)`。
    ///
    /// 宿主每次进循环前核对 `globals[槽号]` 仍指向那个 id —— 这就是**单态守卫**。
    pub fn 调用点(&self) -> &[(usize, usize)] {
        &self.调用点
    }

    /// 正常跑完时解释器该接着跑的指令下标。
    pub fn 出口(&self) -> usize {
        self.出口
    }

    /// `中断` 时解释器该跳到的指令下标。
    pub fn 中断口(&self) -> usize {
        self.中断口
    }

    /// 是不是 `遍历` 循环（宿主据此摊平元素、并在正常结束时弹掉迭代器）。
    pub fn 是遍历(&self) -> bool {
        self.是遍历
    }

    /// 跑完整个循环。
    ///
    /// 返回 [`结果::去优化`] 时 `值` **保持原样**（机器码没写回），调用方应当
    /// 把循环原封不动交回解释器重跑。
    pub fn 跑(&self, 值: &mut [i64], 元素: &[i64]) -> 结果 {
        debug_assert_eq!(值.len(), self.槽们.len());
        if self.是遍历 {
            debug_assert!(!元素.is_empty(), "遍历 循环必须带元素缓冲（第 0 个是个数）");
        }
        match (self.入口)(值.as_mut_ptr(), 元素.as_ptr()) {
            0 => 结果::跑完,
            2 => 结果::中断,
            _ => 结果::去优化,
        }
    }
}

/// 按目标架构选后端。
///
/// ⚠️ **两个模块都不加 `cfg` 门**：这样在 x86-64 的开发机上也能调
/// `aarch64::生成()` 验编码（见 `tests/test_m184_aarch64.py`）。
/// 只有「能不能跑」是分开的 —— 那由 `目标架构` 之后的 `申请()` 决定。
fn 后端生成(方案: &方案) -> Option<Vec<u8>> {
    #[cfg(target_arch = "aarch64")]
    {
        aarch64::生成(方案)
    }
    #[cfg(not(target_arch = "aarch64"))]
    {
        x86_64::生成(方案)
    }
}

/// 写一条「返回 42」的最小机器码进可执行内存，**真调用一次**（M184）。
///
/// 这是路线图 §M184 那条「开工第一件事」的常驻版：Apple Silicon 上 JIT 的失败
/// 模式是**启动即死**（缺 `MAP_JIT` / 缺 `allow-jit` 权限 / 没刷 I-cache），
/// 不是异常、没有栈、什么都没有。有了这条自检，CI 就能把
/// 「这台机器的 JIT 环境不通」和「编码器写错了」**分开** —— 少查一天。
///
/// 它验证的正是「写机器码 → 刷缓存 → 跳进去 → 拿到返回值」这条完整链：
/// `申请`（macOS 带 MAP_JIT）· `jit写允许/禁止`（`pthread_jit_write_protect_np`）
/// · `刷指令缓存`（`sys_icache_invalidate`）。
pub fn 自检() -> Result<i64, String> {
    if !支持() {
        return Err("这个架构没有 JIT 后端（只有 x86-64 与 ARM64）".to_string());
    }
    // `mov w0, #42` / `ret`
    #[cfg(target_arch = "aarch64")]
    let 码: Vec<u8> = {
        let mut v = Vec::with_capacity(8);
        v.extend_from_slice(&(0x5280_0000u32 | (42 << 5)).to_le_bytes());
        v.extend_from_slice(&0xD65F03C0u32.to_le_bytes());
        v
    };
    // `mov eax, 42` / `ret`
    #[cfg(not(target_arch = "aarch64"))]
    let 码: Vec<u8> = vec![0xB8, 42, 0, 0, 0, 0xC3];

    let 块 = 申请(码.len()).ok_or_else(|| {
        "申请可执行内存失败（macOS 上多半是缺 MAP_JIT 或没有 JIT 权限）".to_string()
    })?;
    jit写允许();
    // SAFETY: `块` 至少有 `码.len()` 字节，且刚申请、还没人用过。
    unsafe {
        std::ptr::copy_nonoverlapping(码.as_ptr(), 块.地, 码.len());
    }
    刷指令缓存(块.地, 码.len());
    jit写禁止();
    // SAFETY: 约定好的签名是 `extern "C" fn() -> i64`；上面写的正是「返回 42」。
    let f: extern "C" fn() -> i64 = unsafe { std::mem::transmute(块.地) };
    Ok(f())
}

/// 编译一个循环。编不出来（形态不认识 / 平台不支持 / 拿不到内存）给 `None`。
pub fn 编译(
    指令们: &[crate::Instr],
    常量们: &[Val],
    起: usize,
    终: usize,
    提示: &HashMap<槽位, 类型>,
    内联表: &HashMap<usize, 内联体>,
) -> Option<已编> {
    if !支持() {
        return None;
    }
    let 方案 = match 认循环(指令们, 常量们, 起, 终, 提示, 内联表) {
        Ok(x) => x,
        Err(因) => {
            if std::env::var("JISHI_JIT_DEBUG").is_ok() || std::env::var("JISHI_JIT").as_deref() == Ok("调试") {
                eprintln!("[jit] 抬头 {起} 回跳 {终} 编不了：{因}");
            }
            return None;
        }
    };
    let 码 = match 后端生成(&方案) {
        Some(x) => x,
        None => {
            if std::env::var("JISHI_JIT_DEBUG").is_ok() || std::env::var("JISHI_JIT").as_deref() == Ok("调试") {
                eprintln!("[jit] 抬头 {起} 回跳 {终} 编不了：有跳转找不到落点");
            }
            return None;
        }
    };
    if std::env::var("JISHI_JIT_DUMP").is_ok() {
        let 十六: Vec<String> = 码.iter().map(|b| format!("{b:02X}")).collect();
        eprintln!("[jit] {} 字节：{}", 码.len(), 十六.join(" "));
    }
    // ARM64 编码探针（M184）：
    //
    // 开发机是 x86-64，**跑不了** ARM 机器码，那怎么验第二个后端的编码？
    // 让它**在这个架构上也编一遍**、把字节打出来 —— 判据就能在 x86 上检查
    // 「该发的指令发了、跳转偏移补对了」。CI 的 macos-arm64 上再拿同一份探针
    // 与**真正在跑的那份**对比，两边必须逐字节相同。
    //
    // 正常情况下没人会设这个变量；它只在判据里出现，零运行时开销（一次 getenv）。
    if std::env::var("JISHI_JIT_DUMP_ARM").is_ok() {
        match aarch64::生成(&方案) {
            Some(码) => {
                let 十六: Vec<String> = 码.iter().map(|b| format!("{b:02X}")).collect();
                eprintln!("[jit-arm] {} 字节：{}", 码.len(), 十六.join(" "));
            }
            None => eprintln!("[jit-arm] 编不了（跳转落点缺失或超出范围）"),
        }
    }
    let 块 = 申请(码.len())?;
    // SAFETY: `码` 是刚发出来的机器码，`块` 至少有 `码.len()` 字节可写可执行。
    // `jit写允许` 只在 macOS + ARM64 上真的做事（见它的说明）。
    jit写允许();
    unsafe {
        std::ptr::copy_nonoverlapping(码.as_ptr(), 块.地, 码.len());
    }
    刷指令缓存(块.地, 码.len());
    jit写禁止();
    // SAFETY: 约定好的签名是 `extern "C" fn(*mut i64, *const i64) -> i64`，
    // 生成码就是这么写的（两个参数从 rcx/rdx 或 rdi/rsi 取，返回值放 rax）。
    let 入口: 入口 = unsafe { std::mem::transmute(块.地) };
    Some(已编 {
        槽们: 方案.槽们,
        槽型: 方案.槽型,
        出口: 方案.出口,
        中断口: 方案.中断口,
        是遍历: 方案.是遍历,
        调用点: 方案.调用点,
        入口,
        _块: 块,
    })
}

// ---------------------------------------------------------------------------
// 热点计数
// ---------------------------------------------------------------------------

/// 回跳多少次之后开始编。**一次编译几十微秒**，对几百次的循环不值当。
/// 默认热度阈值：一条回边转够这么多圈才试着编。
///
/// 📌 **按 VM 可改**（M186）：常驻 VM 里一帧最多一次回边 ⇒ 1000 圈要 ~17 秒
/// 才热起来，游戏等不起 ⇒ `图形.运行` 之后由 wasm 入口调 `设阈值(60)`。
/// 一次性程序用默认值（它们根本活不到 1000 圈以外）。
const 默认阈值: u32 = 1000;

/// 「这个循环已经编好了」的哨兵。
///
/// 🔴 M182 修的一个真缺陷：原来是「计数到阈值 ⇒ 试一次编译」，编好之后计数停在
/// 阈值上，`记一步` 从此返回 `false` ⇒ **编好的循环再也没被进过**。
/// 后果：一个函数被调用 100 次，只有跨阈值那一次享受 JIT，另 99 次全走解释器。
/// 现在编好之后把计数钉成这个哨兵，`记一步` 每次都返回 `true`（进 JIT 之前
/// 仍要过类型守卫），而 `封存`（黑名单）还是 `u32::MAX`。
const 已编好: u32 = u32::MAX - 1;

/// JIT 的全部可变状态。挂在 `VM` 上。
pub struct 状态 {
    开: bool,
    /// 环境变量把它关掉了（`JISHI_JIT=0`）。
    ///
    /// 🔴 **优先级最高**：`--不用jit` 只是「这次别用」，而 `JISHI_JIT=0` 是
    /// 「这个环境里别用」—— 判据与 A/B 靠的正是后者，命令行再给一遍 `设开(真)`
    /// 不能把它翻回来。
    环境关: bool,
    已关: bool,
    /// `JISHI_JIT=调试` 时把「编了哪段 / 为什么没编」打到 stderr。
    调试: bool,
    /// 直接映射的热度计数（`(ip + 代码号 * 64) & 511`）。
    ///
    /// 🔴 **为什么不是 `HashMap`**：这条计数在**每一次回跳**上都要摸一次。
    /// `HashMap` 一次查找 ~20 ns，而一次循环迭代才 ~100 ns —— 那就是 20% 的税。
    /// 直接映射表是一次乘加 + 一次数组索引（~1 ns），代价是**可能撞车**：
    /// 撞了只是「某个循环少算了几次热度」（少编一个循环，慢但正确）。
    计数: [u32; 512],
    /// `(代码号, 抬头) → 结果`。`None` = 试过了，编不出来（黑名单）。
    结果: HashMap<(usize, usize), Option<已编>>,
    /// 热度阈值（M186 起是**每个 VM 一份**，见 `默认阈值` 的说明）。
    阈值: u32,
    /// **函数级 JIT 的表**（M190）：`Func.id → 状态`。
    ///
    /// 与循环那张表分开：循环的键是 `(代码号, 抬头)`，函数的键是**函数身份**
    /// （`Func.id`）—— 同一个函数可能被多个循环调用，热度该合起来算。
    函数表: HashMap<usize, 函数态>,
}

impl Default for 状态 {
    fn default() -> Self {
        Self::new()
    }
}

fn 位次(代码号: usize, 回跳ip: usize) -> usize {
    (回跳ip.wrapping_add(代码号.wrapping_mul(64))) & 511
}

impl 状态 {
    pub fn new() -> Self {
        // 环境变量能一票关掉（判据、A/B、用户排障都用得上）
        let 原 = std::env::var("JISHI_JIT").unwrap_or_default();
        // 🔴 **ARM64 编码探针自己开**（M184）：设了 `JISHI_JIT_DUMP_ARM` 就强制打开
        // JIT 与调试输出，**不受 `JISHI_JIT=0` 影响**。
        //
        // 为什么：判据只关心「ARM 后端把这段字节码编成什么」，这件事与「这次运行
        // 用不用 JIT」无关。M184 的第一版没这么写，判据就时好时坏 —— 同一个脚本
        // 在这条判据里能编、在那条里编不了，查了很久才发现是开关状态在作怪。
        // 探针必须**确定性**，不能依赖任何运行时开关。
        let 探针 = std::env::var("JISHI_JIT_DUMP_ARM").is_ok();
        let 环境关 = 原 == "0" && !探针;
        let 开 = !环境关 && 支持();
        let 调试 = 探针 || 原 == "调试" || 原 == "debug" || std::env::var("JISHI_JIT_DEBUG").is_ok();
        状态 { 开, 环境关, 已关: false, 调试, 计数: [0; 512], 结果: HashMap::new(),
               阈值: 默认阈值, 函数表: HashMap::new() }
    }

    /// 运行时开关（`--不用jit`）。关掉之后**已经编好的也不再进**。
    ///
    /// ⚠️ `JISHI_JIT=0` 时这里**永远是关**（见 `环境关` 的说明）。
    pub fn 设开(&mut self, 开: bool) {
        self.开 = 开 && !self.环境关 && 支持();
    }

    pub fn 开着(&self) -> bool {
        self.开 && !self.环境关 && !self.已关
    }

    pub fn 调试么(&self) -> bool {
        self.调试
    }

    /// 改这个 VM 的热度阈值（M186，常驻 VM 用）。
    ///
    /// ⚠️ **已经记下的计数不清零**：一个已经转了 500 圈的循环，阈值降到 60 之后
    /// 下一圈（第 501 圈）就会命中 `*c == 阈值` 吗？不会 —— 条件是 `*c < 阈值`，
    /// 500 已经不满足了，于是它**永远**停在等阈值那一步。所以降阈值必须配
    /// **把超过新阈值的计数夹回去**，否则「降阈值」在最需要它的那个循环上失效。
    pub fn 设阈值(&mut self, n: u32) {
        let n = n.max(1);
        self.阈值 = n;
        for c in self.计数.iter_mut() {
            // `已编好` / 封存 的哨兵值原样留着（它们是 u32::MAX 与 MAX-1）
            if *c < u32::MAX - 1 && *c > n {
                *c = n;
            }
        }
    }

    /// 这个回边又转了一圈。返回 `true` = 该进 [`crate::jit::编译`] 那条路了
    /// （**跨阈值那一次**，以及**编好之后的每一次**）。
    #[inline]
    pub fn 记一步(&mut self, 代码号: usize, 回跳ip: usize) -> bool {
        let c = &mut self.计数[位次(代码号, 回跳ip)];
        if *c == 已编好 {
            return true;
        }
        if *c < self.阈值 {
            *c += 1;
            return *c == self.阈值;
        }
        false
    }

    /// 编好了：以后每次回边都进 JIT。
    pub fn 标记已编(&mut self, 代码号: usize, 回跳ip: usize) {
        self.计数[位次(代码号, 回跳ip)] = 已编好;
    }

    /// 把热度钉死（编不了 / 跑不了都走这里）—— 以后不再试。
    pub fn 封存(&mut self, 代码号: usize, 回跳ip: usize) {
        self.计数[位次(代码号, 回跳ip)] = u32::MAX;
    }

    pub fn 取(&self, 代码号: usize, 抬头: usize) -> Option<&Option<已编>> {
        self.结果.get(&(代码号, 抬头))
    }

    pub fn 存(&mut self, 代码号: usize, 抬头: usize, 值: Option<已编>) {
        self.结果.insert((代码号, 抬头), 值);
    }

    /// 编好的循环数（判据与统计用）。
    pub fn 已编数(&self) -> usize {
        self.结果.values().filter(|x| x.is_some()).count()
    }

    /// 编好的**函数**数（M190，判据与统计用）。
    pub fn 已编函数数(&self) -> usize {
        self.函数表.values().filter(|x| matches!(x, 函数态::编好(_))).count()
    }

    /// 这个函数第 N 次被调用 —— 返回 `true` 表示「该试着编了」。
    ///
    /// 🔴 与循环 JIT 同一条规矩：**编不了就封存**，以后不再试（否则每个调用点
    /// 都要走一遍「认形态 → 失败」，那比不编还慢）。
    pub fn 函数记一次(&mut self, id: usize) -> bool {
        let 阈值 = self.阈值;
        函数该编(&mut self.函数表, id, 阈值)
    }

    /// 取编好的函数（没编好 / 封存了都给 `None`）。
    pub fn 函数取(&self, id: usize) -> Option<&已编函数> {
        match self.函数表.get(&id) {
            Some(函数态::编好(x)) => Some(x),
            _ => None,
        }
    }

    /// 存编译结果：`None` = 编不了，封存。
    pub fn 函数存(&mut self, id: usize, 值: Option<已编函数>) {
        match 值 {
            Some(x) => {
                self.函数表.insert(id, 函数态::编好(x));
            }
            None => {
                self.函数表.insert(id, 函数态::封存);
            }
        }
    }

    /// 这个函数是不是已经封存了（编不了 / 跑不了）。
    pub fn 函数封存了(&self, id: usize) -> bool {
        matches!(self.函数表.get(&id), Some(函数态::封存))
    }
}

// ---------------------------------------------------------------------------
// 函数级 JIT（M190，见 D114）
// ---------------------------------------------------------------------------

/// 函数级 JIT 认得出来的形态。
///
/// 只有这么点信息是有意的：**认得出什么**与**怎么发码**分开写，
/// 前者在这里、后者在 `x86_64::生成函数` / （将来的）`aarch64::生成函数`。
#[derive(Clone)]
pub struct 函数方案 {
    /// 形参个数（1–3）。
    pub 参数数: usize,
    /// 用到的局部槽个数（含形参），帧大小按它算。
    pub 槽数: usize,
    /// 编译期看到的「调自己」用的全局槽号 —— **进机器码之前**宿主必须核对
    /// `globals[k]` 仍然指向这个函数（基石里函数是值，能被重新赋值）。
    pub 自递归全局: Vec<usize>,
}

/// 去掉编译器**自动补的那条「返回 空」尾巴**：`LOAD_CONST <非整数>; RETURN`。
///
/// 为什么必须去：每个函数末尾都有一条隐式 `返回 空`（fib 的字节码第 19–20 条），
/// 而函数级 JIT 只认整数 —— 那条尾巴会让**所有**函数都判「常量不是整数」，
/// 于是这一站对任何函数都不生效（写第一版时就撞在这上面）。
/// 它在这种函数里是**不可达**的（每条路径都显式 `返回`），去掉不影响语义；
/// 真走到那儿就由后端发 `jmp 去优化`，交给解释器。
pub fn 有效指令<'a>(指令们: &'a [crate::Instr], 常量们: &[Val]) -> &'a [crate::Instr] {
    let mut 长 = 指令们.len();
    if 长 >= 2 {
        let 倒二 = &指令们[长 - 2];
        let 倒一 = &指令们[长 - 1];
        let 是空常量 = 常量们
            .get(倒二.a as usize)
            .map(|v| !matches!(v, Val::Int(_)))
            .unwrap_or(false);
        if 倒二.op == crate::op::LOAD_CONST && 倒一.op == crate::op::RETURN && 是空常量 {
            长 -= 2;
        }
    }
    &指令们[..长]
}

/// 认一个函数能不能编（M190 第一道闸）。
///
/// 只认这样一种函数：**参数与返回全整数、无闭包、无默认参数**，函数体只用
/// `+ - *` / 比较 / `如果` / `返回` / **对自己的一次调用**。
/// 别的一律 `Err`（调用方据此把这个函数封存，以后不再试）。
pub fn 认函数(
    指令们: &[crate::Instr],
    常量们: &[Val],
    参数数: usize,
) -> Result<函数方案, String> {
    let 指令们 = 有效指令(指令们, 常量们);
    if 参数数 == 0 || 参数数 > 3 {
        return Err("函数级 JIT 只认 1~3 个形参".into());
    }
    if 指令们.is_empty() {
        return Err("函数体是空的".into());
    }
    if 指令们.len() > 200 {
        return Err("函数太长（>200 条指令）".into());
    }
    let 总 = 指令们.len();
    let mut 槽数 = 参数数;
    let mut 自递归全局: Vec<usize> = Vec::new();
    // 值栈：`Some(k)` = 这个值来自 `LOAD_GLOBAL k`（可以当被调者），`None` = 整数。
    let mut 种: Vec<Option<usize>> = Vec::new();

    for ins in 指令们 {
        match ins.op {
            crate::op::LOAD_CONST => {
                取整(常量们, ins.a).ok_or("常量不是整数（浮点/文本留给下一版）")?;
                种.push(None);
            }
            crate::op::LOAD_FAST => {
                if !(0..8).contains(&ins.a) {
                    return Err("局部槽号越界（只认 0–7）".into());
                }
                槽数 = 槽数.max(ins.a as usize + 1);
                种.push(None);
            }
            crate::op::STORE_FAST => {
                if !(0..8).contains(&ins.a) {
                    return Err("局部槽号越界（只认 0–7）".into());
                }
                槽数 = 槽数.max(ins.a as usize + 1);
                if 种.pop().is_none() {
                    return Err("存槽时值栈是空的".into());
                }
            }
            crate::op::POP_TOP => {
                if 种.pop().is_none() {
                    return Err("弹栈时值栈是空的".into());
                }
            }
            crate::op::LOAD_GLOBAL => {
                if ins.a < 0 {
                    return Err("全局槽号是负数".into());
                }
                种.push(Some(ins.a as usize));
            }
            crate::op::BIN_OP => {
                match 认运算(ins.a) {
                    Some(运算::加) | Some(运算::减) | Some(运算::乘) => {}
                    _ => {
                        return Err("函数级 JIT 只认 + - *（整除/取余/真除留给下一版）".into())
                    }
                }
                if 种.len() < 2 {
                    return Err("二元运算前面值栈不够深".into());
                }
                种.pop();
                种.pop();
                种.push(None);
            }
            crate::op::COMPARE => {
                认比较(ins.a).ok_or("比较码不认识")?;
                if 种.len() < 2 {
                    return Err("比较前面值栈不够深".into());
                }
                种.pop();
                种.pop();
                种.push(None);
            }
            crate::op::POP_JUMP_IF_FALSE | crate::op::POP_JUMP_IF_TRUE => {
                if !(0..=总 as i64).contains(&ins.a) {
                    return Err("跳转目标越界".into());
                }
                if 种.pop().is_none() {
                    return Err("条件跳转前面没有值".into());
                }
            }
            crate::op::JUMP => {
                if !(0..=总 as i64).contains(&ins.a) {
                    return Err("跳转目标越界".into());
                }
            }
            crate::op::RETURN => {
                if 种.pop().is_none() {
                    return Err("返回时值栈是空的".into());
                }
            }
            crate::op::CALL => {
                let 实参 = ins.a as usize;
                if 实参 != 参数数 {
                    return Err("调用的实参个数与形参不一致".into());
                }
                if 种.len() < 实参 + 1 {
                    return Err("调用时值栈不够深".into());
                }
                let 起点 = 种.len() - 实参 - 1;
                match 种[起点] {
                    Some(k) => {
                        if !自递归全局.contains(&k) {
                            自递归全局.push(k);
                        }
                    }
                    None => return Err("被调者不是全局函数（闭包/局部值不认）".into()),
                }
                for _ in 0..实参 + 1 {
                    种.pop();
                }
                种.push(None); // 调用结果当整数
            }
            _ => {
                return Err(
                    "函数体里有 JIT 不认识的指令（下标 / 属性 / 方法 / 嵌套 / 异常…）".into(),
                )
            }
        }
    }

    Ok(函数方案 { 参数数, 槽数, 自递归全局 })
}

/// 编好的函数：一块可执行内存 + 调用它要的元信息。
pub struct 已编函数 {
    参数数: usize,
    自递归全局: Vec<usize>,
    /// `extern "C" fn(a, b, c, 旗: *mut i64) -> i64`
    ///
    /// 🔴 **为什么不是返回 `(i64, i64)`**（第一版就是那么写的，Windows 上直接错）：
    /// Win64 上 **16 字节的返回结构体走隐藏指针（sret）**，占用第 1 个参数寄存器
    /// `rcx` —— 于是我们的 `n` 跑到 `rdx` 去，`rcx` 变成了返回缓冲的地址，
    /// 机器码拿「指针」当 `n` 算，`n < 2` 永远不成立 ⇒ **无限递归**。
    /// 症状极具迷惑性：小规模（还没到热度阈值）全对，一旦真编上就撞栈。
    /// 改成「返回值 + 一个旗指针」：参数全在寄存器里，返回值只有一个 `i64`。
    入口: extern "C" fn(i64, i64, i64, *mut i64) -> i64,
    /// 只是为了让内存在 `Drop` 时还回去（`入口` 指着它）。
    _块: 内存块,
}

impl 已编函数 {
    /// 形参个数。
    pub fn 参数数(&self) -> usize {
        self.参数数
    }

    /// 编译期看到的「调自己」用的全局槽号（调用前要核对）。
    pub fn 自递归全局(&self) -> &[usize] {
        &self.自递归全局
    }

    /// 跑一次。返回 `(值, 标志)`：标志 0 = 正常，1 = 去优化（调用方走解释器）。
    /// `起始深` = 调用点当前的解释器帧数（机器码从它接着往 1000 数）。
    pub fn 调用(&self, 实参: [i64; 3], 起始深: usize) -> (i64, i64) {
        let mut 状态: [i64; 2] = [0, 起始深 as i64];
        let v = (self.入口)(实参[0], 实参[1], 实参[2], 状态.as_mut_ptr());
        (v, 状态[0])
    }
}

fn 后端生成函数(
    形: &函数方案,
    指令们: &[crate::Instr],
    常量们: &[Val],
) -> Option<Vec<u8>> {
    #[cfg(target_arch = "aarch64")]
    {
        aarch64::生成函数(形, 指令们, 常量们)
    }
    #[cfg(not(target_arch = "aarch64"))]
    {
        x86_64::生成函数(形, 指令们, 常量们)
    }
}

/// 编译一个函数。编不出来（形态不认识 / 平台不支持 / 拿不到内存）给 `None`。
pub fn 编译函数(
    形: &函数方案,
    指令们: &[crate::Instr],
    常量们: &[Val],
) -> Option<已编函数> {
    if !支持() {
        return None;
    }
    let 码 = 后端生成函数(形, 指令们, 常量们)?;
    if 码.is_empty() {
        return None;
    }
    if std::env::var("JISHI_JIT_DUMP").is_ok() {
        let 十六: Vec<String> = 码.iter().map(|b| format!("{b:02X}")).collect();
        eprintln!("[jit-func] {} 字节：{}", 码.len(), 十六.join(" "));
    }
    // ARM64 探针（M190 的第二刀）—— 与循环 JIT 的 `[jit-arm]` 同一个思路：
    // 本机是 x86-64，跑不了 ARM 码，那就在这个架构上也**编一遍**把字节打出来，
    // 判据才能检查「该发的指令发了、跳转偏移补对了」。
    // CI 的 macos-arm64 上再拿同一份探针与**真正在跑的那份**对比，必须逐字节相同。
    #[cfg(not(target_arch = "aarch64"))]
    if std::env::var("JISHI_JIT_DUMP_ARM").is_ok() {
        match aarch64::生成函数(形, 指令们, 常量们) {
            Some(码) => {
                let 十六: Vec<String> = 码.iter().map(|b| format!("{b:02X}")).collect();
                eprintln!("[jit-arm-func] {} 字节：{}", 码.len(), 十六.join(" "));
            }
            None => eprintln!("[jit-arm-func] 编不了"),
        }
    }
    let 块 = 申请(码.len())?;
    jit写允许();
    // SAFETY: `块` 至少有 `码.len()` 字节，且刚申请、还没人用过。
    unsafe {
        std::ptr::copy_nonoverlapping(码.as_ptr(), 块.地, 码.len());
    }
    刷指令缓存(块.地, 码.len());
    jit写禁止();
    // SAFETY: 约定好的签名是 `extern "C" fn(i64, i64, i64) -> (i64, i64)`，
    // 正好与发射器写的序言/尾声一致（见 D114）。
    let 入口: extern "C" fn(i64, i64, i64, *mut i64) -> i64 =
        unsafe { std::mem::transmute(块.地) };
    Some(已编函数 { 参数数: 形.参数数, 自递归全局: 形.自递归全局.clone(), 入口, _块: 块 })
}

// ---------------------------------------------------------------------------
// 函数级 JIT 的热度表（M190）
// ---------------------------------------------------------------------------

/// 一个函数在 JIT 眼里的状态。
pub enum 函数态 {
    /// 还差多少圈到阈值。
    计(u32),
    /// 编好了。
    编好(已编函数),
    /// 编不了 / 跑不了 —— 以后不再试（与循环 JIT 的「封存」同一条规矩）。
    封存,
}

/// 函数级 JIT 的函数表与热度（挂在 [`状态`] 上）。
pub fn 函数该编(表: &mut HashMap<usize, 函数态>, id: usize, 阈值: u32) -> bool {
    match 表.get_mut(&id) {
        None => {
            表.insert(id, 函数态::计(1));
            false
        }
        Some(函数态::计(n)) => {
            *n += 1;
            if *n >= 阈值 {
                true
            } else {
                false
            }
        }
        _ => false,
    }
}
