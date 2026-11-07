//! JIT v0（M180）：把**整数 `当` 循环**编成真的机器码。
//!
//! ## 这一版编的是什么（砍到最小）
//!
//! **一种形态**：`当` 循环，条件与循环体只用**局部变量 + 整数字面量**，
//! 算术只有 `+ - *`（含取负），比较只有 `== != < > <= >=`。
//!
//! ```text
//! 函数 累加(n)：
//!     令 i = 0
//!     令 s = 0
//!     当 i < n：          ← 这一整个循环被编成一段 x86-64
//!         s = s + i
//!         i = i + 1
//!     返回 s
//! ```
//!
//! ## 不编的（**如实列出，别让人以为有**）
//!
//! 浮点 / 精确小数 / 文本 / 列表 / 字典 / 函数调用 / 方法调用 / `遍历`（`FOR_ITER`
//! 的迭代器在栈上）/ 嵌套循环 / `如果` 分支 / `跳出` / 除法（要报除零错误）。
//! 碰到任何一条就**不编**，退回解释器 —— 慢，但一定对。
//!
//! ## 正确性靠三道闸
//!
//! 1. **类型守卫**（进 JIT 之前）：循环用到的每个局部槽必须是「赋过值的 `整数`」，
//!    且值落在 `i64` 内。任何一条不满足 → 不跑 JIT。
//! 2. **溢出即去优化**：机器码里每条 `+ - *` 后面跟一条 `jo`（溢出跳转），
//!    溢出就**不写回**直接返回 1 —— 局部变量保持**进 JIT 之前**的样子，
//!    解释器从头再跑这个循环（它用 `i128`，不会溢出）。
//! 3. **失败即黑名单**：编不出来 / 跑不了就记成「这个循环别试了」，
//!    以后再热也不再尝试（不会每次都白花编译的力气）。
//!
//! ## 为什么状态放在一块 `i64` 缓冲里，而不是直接读写 `Val`
//!
//! `Val` 是带标签的枚举（`Int(i128)`），机器码要按它的内存布局构造值是**耦合到
//! 崩溃级**的（加一个变体就悄悄错位）。改成：进 JIT 前把用到的局部槽摊平成
//! `[i64]`，跑完再写回。摊平是**每个循环一次**，摊到几百万次迭代上等于零。
//!
//! ## 平台
//!
//! 只覆盖 **x86-64**（Windows / Linux / macOS）。ARM（Apple Silicon、ARM 服务器）
//! 没有后端 —— `支持()` 返回 `假`，一切照旧走解释器。这是路线图里写明的范围
//! （「先做一个只编整数循环的原型，量出真实收益再决定要不要上 cranelift」）。

// 这个模块里有一批只在 `jit` 特征 + x86-64 下才用到的助手（比如 `RCX` 常量、
// 各平台的 `归还`），关掉那种组合时它们就是死代码 —— 不值得为它们加两打 `cfg`。
#![allow(dead_code)]

use crate::op;
use crate::Val;
use std::collections::HashMap;

/// 这个构建/平台到底有没有 JIT。
///
/// ⚠️ 返回值是**编译期**决定的：非 x86-64 一律 `false`，`编译()` 永远给 `None`。
pub fn 支持() -> bool {
    cfg!(all(
        target_arch = "x86_64",
        any(target_os = "windows", target_os = "linux", target_os = "macos")
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

#[cfg(all(target_arch = "x86_64", windows))]
fn 申请(长: usize) -> Option<内存块> {
    // 至少一页：`VirtualAlloc` 会按页向上取整，这里不自己算。
    const MEM_COMMIT: u32 = 0x1000;
    const MEM_RESERVE: u32 = 0x2000;
    const PAGE_EXECUTE_READWRITE: u32 = 0x40;
    extern "system" {
        fn VirtualAlloc(地址: *mut u8, 长: usize, 类型: u32, 保护: u32) -> *mut u8;
    }
    let 长 = 长.max(64);
    // SAFETY: 标准 Win32 调用；参数都是常量。
    let p = unsafe { VirtualAlloc(std::ptr::null_mut(), 长, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE) };
    if p.is_null() {
        None
    } else {
        Some(内存块 { 地: p, 长 })
    }
}

#[cfg(all(target_arch = "x86_64", windows))]
fn 归还(地: *mut u8, _长: usize) {
    const MEM_RELEASE: u32 = 0x8000;
    extern "system" {
        fn VirtualFree(地址: *mut u8, 长: usize, 类型: u32) -> i32;
    }
    if !地.is_null() {
        // SAFETY: 这块内存是上面 `VirtualAlloc` 要来的，只还一次（`Drop`）。
        unsafe { VirtualFree(地, 0, MEM_RELEASE) };
    }
}

#[cfg(all(target_arch = "x86_64", any(target_os = "linux", target_os = "macos")))]
fn 申请(长: usize) -> Option<内存块> {
    extern "C" {
        fn mmap(地址: *mut u8, 长: usize, 保护: i32, 标志: i32, 句柄: i32, 偏移: i64) -> *mut u8;
    }
    const PROT_READ: i32 = 1;
    const PROT_WRITE: i32 = 2;
    const PROT_EXEC: i32 = 4;
    const MAP_PRIVATE: i32 = 0x02;
    // ⚠️ **匿名映射的常量两个系统不一样**：Linux 是 0x20、macOS 是 0x1000。
    //    写错不会崩 —— `mmap` 返回失败，JIT 自动关掉（那是最难发现的一种「不生效」，
    //    所以这里显式分叉 + 注释）。
    #[cfg(target_os = "linux")]
    const MAP_ANON: i32 = 0x20;
    #[cfg(target_os = "macos")]
    const MAP_ANON: i32 = 0x1000;
    let 长 = 长.max(64);
    // SAFETY: 标准 POSIX 调用；`fd = -1` + `MAP_ANON` 表示不映射任何文件。
    let p = unsafe {
        mmap(
            std::ptr::null_mut(),
            长,
            PROT_READ | PROT_WRITE | PROT_EXEC,
            MAP_PRIVATE | MAP_ANON,
            -1,
            0,
        )
    };
    // 失败时返回的是 `MAP_FAILED`（= -1），不是 NULL。
    if p.is_null() || p as isize == -1 {
        None
    } else {
        Some(内存块 { 地: p, 长 })
    }
}

#[cfg(all(target_arch = "x86_64", any(target_os = "linux", target_os = "macos")))]
fn 归还(地: *mut u8, 长: usize) {
    extern "C" {
        fn munmap(地址: *mut u8, 长: usize) -> i32;
    }
    if !地.is_null() && 地 as isize != -1 {
        // SAFETY: 与 `申请` 成对。
        unsafe { munmap(地, 长) };
    }
}

#[cfg(not(all(
    target_arch = "x86_64",
    any(target_os = "windows", target_os = "linux", target_os = "macos")
)))]
fn 申请(_长: usize) -> Option<内存块> {
    None
}

#[cfg(not(all(
    target_arch = "x86_64",
    any(target_os = "windows", target_os = "linux", target_os = "macos")
)))]
fn 归还(_地: *mut u8, _长: usize) {}

// ---------------------------------------------------------------------------
// 中间表示：把字节码「认」成一条条栈指令
// ---------------------------------------------------------------------------

/// 一条要发的机器码步骤。
///
/// ## 为什么是「机器栈」而不是「寄存器分配」
///
/// 第一版把循环体认成 `目标 = 左 <运算> 右` 这种**扁平**形态，遇到
/// `s = s + i * 3` 就不认识了 —— 因为 `i * 3` 是一个**子表达式**，它的结果
/// 在字节码里是压在栈上的临时值。要支持任意表达式，最省事、最不容易错的做法
/// 就是照着字节码的栈语义发：`push` / `pop` 就是 CPU 的 `push` / `pop`。
///
/// 代价是每步多一次栈读写（约 1 个周期），换来的是「任意嵌套的表达式都能编」。
/// 而解释器每步要取指令 + 分派 + 处理 `Val`，那是几十纳秒 —— 差两个数量级。
#[derive(Clone, Copy)]
enum 步 {
    /// 把某个局部槽推进栈
    取槽(u8),
    /// 把字面量推进栈
    取数(i64),
    /// 弹出两个、算一下、把结果推回去（`0 +` / `1 -` / `2 *`）
    运算(u8),
    /// 弹出、取负、推回
    取负,
    /// 弹出、存进某个局部槽
    存槽(u8),
    /// 弹出、扔掉（表达式语句多余的值）
    丢弃,
    /// 弹出两个比较，条件**不成立**时跳到出口
    比较跳(u8),
    /// 回跳循环头
    回跳,
    /// `目标 = 左 <运算> 右` —— **不碰栈**，直接 `mov / op / mov`。
    ///
    /// 这是 `压扁` 认出来的快捷形态：常见循环体（`s = s + i`、`i = i + 1`）都是它。
    /// 为什么值得单列：走栈的版本每步要 `push push pop pop`，实测比这个慢一倍
    /// （5000 万次的累加：118 ms → 59 ms）。
    算存(源, u8, 源, u8),
    /// `比较 + 条件跳` —— 同样不碰栈（循环条件都是这个形态）。
    比跳(源, u8, 源),
}

/// 一个操作数：某个局部槽（在寄存器里），或者一个字面量。
#[derive(Clone, Copy)]
enum 源 {
    槽(u8),
    数(i64),
}

fn 当源(x: 步) -> Option<源> {
    match x {
        步::取槽(k) => Some(源::槽(k)),
        步::取数(v) => Some(源::数(v)),
        _ => None,
    }
}

/// 局部优化：把 `[取, 取, 运算, 存槽]` 和 `[取, 取, 比较跳]` 压成不碰栈的形态。
///
/// 为什么只压这两种：它们**恰好**是「整个表达式就是一个二元运算」—— 中途栈上
/// 从来没出现过第三个值，所以既不需要寄存器分配，也不用判断嵌套。更复杂的表达式
/// （`a + b * c`）留给栈式，慢一点但一定对。
fn 压扁(步们: &[步]) -> Vec<步> {
    let 取 = |k: usize| 步们.get(k).copied();
    let mut 出 = Vec::with_capacity(步们.len());
    let mut i = 0;
    while i < 步们.len() {
        if let (Some(a), Some(b)) = (当源(步们[i]), 取(i + 1).and_then(当源)) {
            // `[取, 取, 运算, 存槽]` → 一条「直算并存」
            if let (Some(步::运算(op)), Some(步::存槽(k))) = (取(i + 2), 取(i + 3)) {
                出.push(步::算存(a, op, b, k));
                i += 4;
                continue;
            }
            // `[取, 取, 比较跳]` → 一条「直算并跳」
            if let Some(步::比较跳(c)) = 取(i + 2) {
                出.push(步::比跳(a, c, b));
                i += 3;
                continue;
            }
        }
        出.push(步们[i]);
        i += 1;
    }
    出
}

/// 一个「变量槽」到底在哪儿。
///
/// 为什么要分两种：函数里的变量是**局部槽**（`LOAD_FAST`），而**模块顶层**的变量
/// 是**全局槽**（`LOAD_GLOBAL`）—— 脚本和示例里的循环大多写在顶层，只认局部的话
/// JIT 对它们一点用都没有（`当循环倒计数` 那个基准正是顶层）。
///
/// 两者在宿主里都是「按下标访问的数组」（`frames[fi].locals` / `globals`），
/// 所以摊平/写回的逻辑只差一行。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum 槽位 {
    局部(usize),
    全局(usize),
}

/// 一个能编的循环。
struct 方案 {
    步们: Vec<步>,
    /// 用到的槽（顺序 = 机器寄存器 r8/r9/r10/r11 的顺序）。
    槽们: Vec<槽位>,
    /// 条件不成立时跳到的指令下标（解释器从这里接着跑）。
    出口: usize,
}

/// 寄存器编号 ↔ 槽：同一个槽出现多次时复用同一个寄存器。
#[derive(Default)]
struct 分配 {
    表: Vec<槽位>,
}

impl 分配 {
    /// 给一个槽分配寄存器。超过 4 个就给 `None`（这一版只备了 `r8`–`r11`
    /// 四个 —— 它们**不必**被调用者保存，省掉两条返回路径上的 `pop`）。
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
        // `i64` 装不下就不能编：机器码只做 64 位算术，而 `i128` 的那部分
        // 由解释器负责（见「去优化」）。
        Some(Val::Int(v)) => i64::try_from(*v).ok(),
        _ => None,
    }
}

/// 尝试把 `[起, 终]` 认成「一个整数 `当` 循环」。认不出来就给 `None`。
///
/// `终` 是那条回跳 `JUMP` 的下标（含）；`起` 是它跳回去的抬头。
fn 认循环(ins: &[crate::Instr], 常量们: &[Val], 起: usize, 终: usize) -> Option<方案> {
    let 跳 = ins.get(终)?;
    if 跳.op != op::JUMP || 跳.a < 0 || 跳.a as usize != 起 || 终 <= 起 {
        return None;
    }
    let mut 分配 = 分配::default();
    let mut 步们: Vec<步> = Vec::new();
    let mut 深: i32 = 0;          // 模拟栈深：负数就说明这份字节码不是我们想的样子
    let mut 比较数 = 0;
    let mut 有语句 = false;
    // ⚠️ 名字**不能叫 `出口`**：本文件里有个同名函数，结构体字面量里的简写会解析到它
    // （第一次写就撞上了，编译器报「expected usize, found fn item」）。
    let mut 出口位: usize = 0;
    let mut i = 起;

    while i < 终 {
        let x = ins.get(i)?;
        match x.op {
            op::LOAD_FAST | op::LOAD_GLOBAL => {
                if x.a < 0 {
                    return None;
                }
                let 位 = if x.op == op::LOAD_FAST {
                    槽位::局部(x.a as usize)
                } else {
                    槽位::全局(x.a as usize)
                };
                步们.push(步::取槽(分配.取(位)?));
                深 += 1;
                i += 1;
            }
            op::LOAD_CONST => {
                步们.push(步::取数(取整(常量们, x.a)?));
                深 += 1;
                i += 1;
            }
            op::BIN_OP => {
                // 只认 `+ - *`：除法要报「不能除以零」、`**` 要跑快速幂，
                // 那两条的正确性成本远高于收益。
                if !(0..=2).contains(&x.a) || 深 < 2 {
                    return None;
                }
                步们.push(步::运算(x.a as u8));
                深 -= 1;
                i += 1;
            }
            op::UNARY_OP if x.a == 0 => {
                if 深 < 1 {
                    return None;
                }
                步们.push(步::取负);
                i += 1;
            }
            op::STORE_FAST | op::STORE_GLOBAL => {
                if x.a < 0 || 深 < 1 {
                    return None;
                }
                let 位 = if x.op == op::STORE_FAST {
                    槽位::局部(x.a as usize)
                } else {
                    槽位::全局(x.a as usize)
                };
                步们.push(步::存槽(分配.取(位)?));
                深 -= 1;
                有语句 = true;
                i += 1;
            }
            op::POP_TOP => {
                if 深 < 1 {
                    return None;
                }
                步们.push(步::丢弃);
                深 -= 1;
                有语句 = true;
                i += 1;
            }
            op::COMPARE => {
                // 循环条件：`… <左> <右> COMPARE <比较码> POP_JUMP_IF_FALSE <出口>`。
                //
                // 🔴 三条硬要求，少一条就可能把**循环体里的 `如果`** 当成循环条件：
                //  ① 栈深恰好 2（左右两个操作数）；
                //  ② 它必须是**第一条语句**（前面没有 `STORE` / `POP_TOP`）；
                //  ③ 出口在循环**后面**。
                // ②③ 一起，`如果 …: 跳出` 那种（它的跳转目标也在循环后面）就进不来了 ——
                // 而且那条路中间必然有 `BREAK_LOOP`，也是我们认不出的指令。
                if 深 != 2 || 有语句 || 比较数 > 0 {
                    return None;
                }
                if !(0..=5).contains(&x.a) {
                    return None;
                }
                let 跳假 = ins.get(i + 1)?;
                if 跳假.op != op::POP_JUMP_IF_FALSE || 跳假.a < 0 {
                    return None;
                }
                let 出口 = 跳假.a as usize;
                if 出口 <= 终 {
                    return None;
                }
                步们.push(步::比较跳(x.a as u8));
                深 = 0;
                比较数 += 1;
                出口位 = 出口;
                i += 2;
            }
            // 函数调用 / 属性 / 下标 / `跳出` / 嵌套循环 … 一律不认。
            _ => return None,
        }
    }

    // 收尾：栈要平（每条语句都弹干净了），而且**必须正好有一个**循环条件。
    // 没有条件的循环（`当 真`）编出来就是死循环 —— 解释器也是死循环，但我们
    // 不编它：那种循环本来就没有「跑完」这回事，交给解释器更诚实。
    if 深 != 0 || 比较数 != 1 || i != 终 {
        return None;
    }
    步们.push(步::回跳);
    // 认完了再压扁：认的时候保持「一条字节码一条步」，逻辑清楚；
    // 压扁是纯局部的重写，坏了也只影响快慢，不影响语义。
    Some(方案 { 步们: 压扁(&步们), 槽们: 分配.表, 出口: 出口位 })
}

// ---------------------------------------------------------------------------
// x86-64 发射器（只用手写的那几条指令）
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
struct 寄 {
    /// 低 3 位
    号: u8,
    /// 是不是 r8–r15（要在 REX 里置位）
    扩: bool,
}

const RAX: 寄 = 寄 { 号: 0, 扩: false };
const RCX: 寄 = 寄 { 号: 1, 扩: false };
const RDX: 寄 = 寄 { 号: 2, 扩: false };
const RBP: 寄 = 寄 { 号: 5, 扩: false };
const RSP: 寄 = 寄 { 号: 4, 扩: false };

/// 槽寄存器只有四个：`r8`–`r11`。
///
/// 为什么不用 `r12`–`r15`（它们也是通用寄存器）：那四个是**被调用者保存**的，
/// 用了就得在三条返回路径（正常退出 / 去优化）上都 `pop` 回去 —— 多几处能写错的
/// 地方，而循环里的变量很少超过四个。超了就不编（见 `分配::取`）。
const 槽寄: [寄; 4] = [
    寄 { 号: 0, 扩: true }, // r8
    寄 { 号: 1, 扩: true }, // r9
    寄 { 号: 2, 扩: true }, // r10
    寄 { 号: 3, 扩: true }, // r11
];

/// 第一个参数（`*mut i64`）放在哪个寄存器 —— **Windows 和 System V 不一样**。
#[cfg(windows)]
const 参寄: 寄 = 寄 { 号: 1, 扩: false }; // rcx
#[cfg(not(windows))]
const 参寄: 寄 = 寄 { 号: 7, 扩: false }; // rdi（下面 `取寄` 用 7，见 `REX.B`）

/// 标签：0 = 循环头，1 = 正常退出，2 = 去优化。
struct 发 {
    码: Vec<u8>,
    /// 待补的 32 位相对跳转：(rel32 的起始位置, 标签号)
    补: Vec<(usize, u8)>,
    标: [Option<usize>; 3],
}

impl 发 {
    fn new() -> Self {
        发 { 码: Vec::with_capacity(256), 补: Vec::new(), 标: [None; 3] }
    }

    fn b(&mut self, x: u8) {
        self.码.push(x);
    }

    /// REX 前缀。任何一位是 1 就要发（`0x40` 那一位是固定的）。
    fn rex(&mut self, w: bool, r: bool, x: bool, b: bool) {
        if w || r || x || b {
            self.b(0x40 | ((w as u8) << 3) | ((r as u8) << 2) | ((x as u8) << 1) | (b as u8));
        }
    }

    fn modrm(&mut self, md: u8, reg: 寄, rm: 寄) {
        self.b((md << 6) | ((reg.号 & 7) << 3) | (rm.号 & 7));
    }

    /// `mov 目, [基 + 偏]`（`8B /r`，mod=01 用 8 位位移）
    fn 取(&mut self, 目: 寄, 基: 寄, 偏: u8) {
        self.rex(true, 目.扩, false, 基.扩);
        self.b(0x8B);
        self.modrm(0b01, 目, 基);
        self.b(偏);
    }

    /// `mov [基 + 偏], 源`（`89 /r`）
    fn 存(&mut self, 基: 寄, 偏: u8, 源: 寄) {
        self.rex(true, 源.扩, false, 基.扩);
        self.b(0x89);
        self.modrm(0b01, 源, 基);
        self.b(偏);
    }

    /// `mov 目, 源`（`89 /r`，mod=11）
    fn 搬(&mut self, 目: 寄, 源: 寄) {
        self.rex(true, 源.扩, false, 目.扩);
        self.b(0x89);
        self.modrm(0b11, 源, 目);
    }

    /// `mov 目, imm64`（`REX.W B8+rd id`）
    fn 立即(&mut self, 目: 寄, 值: i64) {
        self.rex(true, false, false, 目.扩);
        self.b(0xB8 | (目.号 & 7));
        self.码.extend_from_slice(&值.to_le_bytes());
    }

    /// `add` / `sub` / `cmp`：`目 <运算> 源`（`01` / `29` / `39` 都是 `r/m, r`）
    fn 算(&mut self, 操作码: u8, 目: 寄, 源: 寄) {
        self.rex(true, 源.扩, false, 目.扩);
        self.b(操作码);
        self.modrm(0b11, 源, 目);
    }

    /// `imul 目, 源`（`REX.W 0F AF /r`，注意 reg 是**目的**）
    fn 乘(&mut self, 目: 寄, 源: 寄) {
        self.rex(true, 目.扩, false, 源.扩);
        self.b(0x0F);
        self.b(0xAF);
        self.modrm(0b11, 目, 源);
    }

    /// `push reg`（`50+rd`）
    fn 压(&mut self, r: 寄) {
        if r.扩 {
            self.b(0x41);
        }
        self.b(0x50 | (r.号 & 7));
    }

    /// `pop reg`（`58+rd`）
    fn 弹(&mut self, r: 寄) {
        if r.扩 {
            self.b(0x41);
        }
        self.b(0x58 | (r.号 & 7));
    }

    /// 跳转：`码` 是 1 个字节（`E9`）或 2 个字节（`0F 8x`），后面跟 rel32。
    fn 跳(&mut self, 码: &[u8], 标: u8) {
        for x in 码 {
            self.b(*x);
        }
        self.补.push((self.码.len(), 标));
        self.码.extend_from_slice(&[0, 0, 0, 0]);
    }

    fn 打标(&mut self, 标: u8) {
        self.标[标 as usize] = Some(self.码.len());
    }

    fn 收尾(mut self) -> Vec<u8> {
        for (位, 标) in &self.补 {
            let 到 = match self.标[*标 as usize] {
                Some(x) => x,
                None => continue,   // 不该发生（每条跳转都有落点）
            };
            let 差 = 到 as i64 - (*位 as i64 + 4);
            self.码[*位..*位 + 4].copy_from_slice(&(差 as i32).to_le_bytes());
        }
        self.码
    }
}

/// 把一个操作数搬进指定寄存器。
fn 装(f: &mut 发, 目: 寄, x: 源) {
    match x {
        源::槽(k) => f.搬(目, 槽寄[k as usize]),
        源::数(v) => f.立即(目, v),
    }
}

/// 把中间表示发成机器码。
///
/// ## 栈帧只用 `rbp` 一个寄存器
///
/// 表达式是通过**机器栈**求值的，所以在去优化那一刻栈上可能还压着半个表达式的
/// 中间值 —— 直接 `ret` 会让 `rsp` 回不去（调用方继续用一个歪掉的栈，
/// 那是「随时会炸」的状态）。所以进场先 `push rbp; mov rbp, rsp`，
/// 两条返回路径都 `mov rsp, rbp; pop rbp` 把栈恢复干净。
fn 生成(方案: &方案) -> Vec<u8> {
    let mut f = 发::new();
    let 参 = 参寄;

    // 进场：先把摊平的值装进寄存器（这时 `参` 还没被别的指令动过）
    for (k, _) in 方案.槽们.iter().enumerate() {
        f.取(槽寄[k], 参, (k * 8) as u8);
    }
    f.压(RBP);                 // push rbp
    // ⚠️ 方向别写反：这里是 **mov rbp, rsp**（把当前栈顶记进 rbp）。
    //    写成 `mov rsp, rbp` 就是「把栈指针设成一个垃圾值」，下一步 push 立刻段错误
    //    —— M180 第一次跑就撞上了，机器码里是 `48 89 EC` 而不是 `48 89 E5`。
    f.搬(RBP, RSP);            // mov rbp, rsp
    f.打标(0);

    for 步 in &方案.步们 {
        match *步 {
            步::取槽(k) => f.压(槽寄[k as usize]),
            步::取数(v) => {
                if i32::try_from(v).is_ok() {
                    f.b(0x68);   // push imm32（符号扩展到 64 位）
                    f.码.extend_from_slice(&(v as i32).to_le_bytes());
                } else {
                    f.立即(RAX, v);
                    f.压(RAX);
                }
            }
            步::运算(op) => {
                f.弹(RDX);                        // 右操作数
                f.弹(RAX);                        // 左操作数
                match op {
                    0 => f.算(0x01, RAX, RDX),    // add rax, rdx
                    1 => f.算(0x29, RAX, RDX),    // sub rax, rdx
                    _ => f.乘(RAX, RDX),          // imul rax, rdx
                }
                // 溢出就**不写回**，直接去优化（见模块文档第二道闸）
                f.跳(&[0x0F, 0x80], 2);           // jo 去优化
                f.压(RAX);
            }
            步::取负 => {
                f.弹(RAX);
                f.rex(true, false, false, false);
                f.b(0xF7);                        // neg rax
                f.modrm(0b11, 寄 { 号: 3, 扩: false }, RAX);
                f.跳(&[0x0F, 0x80], 2);           // jo 去优化（-i64::MIN 会溢出）
                f.压(RAX);
            }
            步::存槽(k) => f.弹(槽寄[k as usize]),
            步::丢弃 => f.弹(RAX),
            步::比较跳(c) => {
                f.弹(RDX);
                f.弹(RAX);
                f.算(0x39, RAX, RDX);             // cmp rax, rdx
                // 条件**不成立**时跳到出口 ⇒ 用相反的条件码
                f.跳(&[0x0F, 反条件(c)], 1);
            }
            步::回跳 => f.跳(&[0xE9], 0),          // jmp 循环头
            步::算存(左, op, 右, k) => {
                装(&mut f, RAX, 左);
                装(&mut f, RDX, 右);
                match op {
                    0 => f.算(0x01, RAX, RDX),
                    1 => f.算(0x29, RAX, RDX),
                    _ => f.乘(RAX, RDX),
                }
                f.跳(&[0x0F, 0x80], 2);           // jo 去优化
                f.搬(槽寄[k as usize], RAX);
            }
            步::比跳(左, c, 右) => {
                装(&mut f, RAX, 左);
                装(&mut f, RDX, 右);
                f.算(0x39, RAX, RDX);             // cmp rax, rdx
                f.跳(&[0x0F, 反条件(c)], 1);
            }
        }
    }

    // 正常退出：恢复栈、写回、返回 0
    f.打标(1);
    f.搬(RSP, RBP);            // mov rsp, rbp
    f.弹(RBP);                 // pop rbp
    for (k, _) in 方案.槽们.iter().enumerate() {
        f.存(参, (k * 8) as u8, 槽寄[k]);
    }
    f.算(0x31, RAX, RAX);      // xor eax, eax
    f.b(0xC3);                 // ret

    // 去优化：恢复栈、返回 1（**不写回** —— 局部变量保持进 JIT 之前的样子）
    f.打标(2);
    f.搬(RSP, RBP);
    f.弹(RBP);
    f.b(0xB8);
    f.码.extend_from_slice(&1i32.to_le_bytes());   // mov eax, 1
    f.b(0xC3);                 // ret

    f.收尾()
}

/// 「条件成立就继续」的比较码 → 「条件不成立就跳走」的 `Jcc` 第二字节。
///
/// 比较码与 `compare()` 一致：0 `==`、1 `!=`、2 `<`、3 `>`、4 `<=`、5 `>=`。
/// `Jcc` 的 `0x8x`：`84 je` `85 jne` `8C jl` `8D jge` `8E jle` `8F jg`。
fn 反条件(比较码: u8) -> u8 {
    match 比较码 {
        0 => 0x85,   // 相等 → 不等就跳
        1 => 0x84,   // 不等 → 相等就跳
        2 => 0x8D,   // 小于 → 大于等于就跳
        3 => 0x8E,   // 大于 → 小于等于就跳
        4 => 0x8F,   // 小于等于 → 大于就跳
        _ => 0x8C,   // 大于等于 → 小于就跳
    }
}

// ---------------------------------------------------------------------------
// 编好的循环
// ---------------------------------------------------------------------------

type 入口 = extern "C" fn(*mut i64) -> i64;

/// 一段编好的循环。
pub struct 已编 {
    槽们: Vec<槽位>,
    /// 循环结束时解释器该接着跑的指令下标。
    出口: usize,
    入口: 入口,
    /// 只是为了让内存在 `Drop` 时还回去（`入口` 指着它）。
    _块: 内存块,
}

impl 已编 {
    /// 这个循环用到的槽（顺序 = 摊平缓冲里各元素的位置）。
    pub fn 槽们(&self) -> &[槽位] {
        &self.槽们
    }

    /// 循环结束时解释器该接着跑的指令下标。
    pub fn 出口(&self) -> usize {
        self.出口
    }

    /// 跑完整个循环。
    ///
    /// 返回 `true` = 正常跑完，`值` 里是**循环结束时的结果**（调用方负责写回局部变量）；
    /// 返回 `false` = 中途去优化（溢出），`值` **保持原样**，调用方应当原封不动地
    /// 把循环交回解释器重跑。
    pub fn 跑(&self, 值: &mut [i64]) -> bool {
        debug_assert_eq!(值.len(), self.槽们.len());
        (self.入口)(值.as_mut_ptr()) == 0
    }
}

/// 编译一个循环。编不出来（形态不认识 / 平台不支持 / 拿不到内存）给 `None`。
pub fn 编译(
    指令们: &[crate::Instr],
    常量们: &[Val],
    起: usize,
    终: usize,
) -> Option<已编> {
    if !支持() {
        return None;
    }
    let 方案 = 认循环(指令们, 常量们, 起, 终)?;
    let 码 = 生成(&方案);
    if std::env::var("JISHI_JIT_DUMP").is_ok() {
        let 十六: Vec<String> = 码.iter().map(|b| format!("{b:02X}")).collect();
        eprintln!("[jit] {} 字节：{}", 码.len(), 十六.join(" "));
    }
    let 块 = 申请(码.len())?;
    // SAFETY: `码` 是刚发出来的机器码，`块` 至少有 `码.len()` 字节可写可执行。
    unsafe {
        std::ptr::copy_nonoverlapping(码.as_ptr(), 块.地, 码.len());
    }
    // SAFETY: 约定好的签名是 `extern "C" fn(*mut i64) -> i64`，生成码就是这么写的
    // （参数从 rcx / rdi 取，返回值放 rax）。
    let 入口: 入口 = unsafe { std::mem::transmute(块.地) };
    Some(已编 { 槽们: 方案.槽们, 出口: 方案.出口, 入口, _块: 块 })
}

// ---------------------------------------------------------------------------
// 热点计数
// ---------------------------------------------------------------------------

/// 回跳多少次之后开始编。**一次编译几十微秒**，对几百次的循环不值当。
const 阈值: u32 = 1000;

/// JIT 的全部可变状态。挂在 `VM` 上。
pub struct 状态 {
    开: bool,
    /// 环境变量把它关掉了（`JISHI_JIT=0`）。
    ///
    /// 🔴 **优先级最高**：`--不用jit` 只是「这次别用」，而 `JISHI_JIT=0` 是
    /// 「这个环境里别用」—— 判据与 A/B 靠的正是后者，命令行再给一遍 `设开(真)`
    /// 不能把它翻回来（M180 写判据时踩的：`vm.设jit(true)` 无条件覆盖了环境变量，
    /// 于是「关掉 JIT」那一组跑的还是 JIT，A/B 得到 1.0x）。
    环境关: bool,
    已关: bool,
    /// `JISHI_JIT=调试` 时把「编了哪段 / 为什么没编」打到 stderr。
    ///
    /// 为什么要有：JIT 不生效时用户只会觉得「没变快」，看不出是**形态不认识**
    /// 还是**类型对不上**。排障时这一行就是全部线索。
    调试: bool,
    /// 直接映射的热度计数（`(ip + 代码号 * 64) & 511`）。
    ///
    /// 🔴 **为什么不是 `HashMap`**：这条计数在**每一次回跳**上都要摸一次。
    /// `HashMap` 一次查找 ~20 ns，而一次循环迭代才 ~100 ns —— 那就是 20% 的税。
    /// 直接映射表是一次乘加 + 一次数组索引（~1 ns），代价是**可能撞车**：
    /// 撞了只是「某个循环少算了几次热度」（少编一个循环，慢但正确），
    /// 不会编错东西（真编之前还要认形态 + 类型守卫）。
    计数: [u32; 512],
    /// `(代码号, 抬头) → 结果`。`None` = 试过了，编不出来（黑名单）。
    结果: HashMap<(usize, usize), Option<已编>>,
}

impl Default for 状态 {
    fn default() -> Self {
        Self::new()
    }
}

impl 状态 {
    pub fn new() -> Self {
        // 环境变量能一票关掉（判据、A/B、用户排障都用得上）
        let 原 = std::env::var("JISHI_JIT").unwrap_or_default();
        let 环境关 = 原 == "0";
        let 开 = !环境关 && 支持();
        let 调试 = 原 == "调试" || 原 == "debug" || std::env::var("JISHI_JIT_DEBUG").is_ok();
        状态 { 开, 环境关, 已关: false, 调试, 计数: [0; 512], 结果: HashMap::new() }
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

    /// 这个回边又转了一圈。返回 `true` = 刚好到阈值，值得试一次。
    #[inline]
    pub fn 记一步(&mut self, 代码号: usize, 回跳ip: usize) -> bool {
        let 位 = (回跳ip.wrapping_add(代码号.wrapping_mul(64))) & 511;
        let c = &mut self.计数[位];
        if *c < 阈值 {
            *c += 1;
            return *c == 阈值;
        }
        false
    }

    /// 把热度钉死（编过 / 编不了 / 跑不了都走这里）—— 以后不再试。
    pub fn 封存(&mut self, 代码号: usize, 回跳ip: usize) {
        let 位 = (回跳ip.wrapping_add(代码号.wrapping_mul(64))) & 511;
        self.计数[位] = u32::MAX;
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
}
