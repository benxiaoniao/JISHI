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

mod x86_64;

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

/// 申请一块**可读可写可执行**的内存。
///
/// ⚠️ 这是 W^X 上的妥协：写机器码的时候要可写，跑的时候要可执行。正规做法是
/// 「先 RW 写完再 mprotect 成 RX」，但那要多一次系统调用 + 一次跨页风险；
/// 这里内存是**我们自己刚从系统拿的整页**，写和跑的窗口极短。macOS 上
/// `MAP_JIT` 是 Apple Silicon 的硬要求（见 M183），x86-64 上直接
/// `PROT_EXEC` 也能过。
#[cfg(all(
    target_arch = "x86_64",
    any(target_os = "windows", target_os = "linux", target_os = "macos")
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
        const PROT_RWX: i32 = 7;      // READ | WRITE | EXEC
        const MAP_PRIVATE: i32 = 0x02;
        extern "C" {
            fn mmap(地: *mut u8, 长: usize, 护: i32, 旗: i32, 句: i32, 偏: i64) -> *mut u8;
        }
        let p = unsafe { mmap(std::ptr::null_mut(), 长, PROT_RWX, MAP_PRIVATE | MAP_ANON, -1, 0) };
        // MAP_FAILED 是 -1
        if p as isize == -1 {
            None
        } else {
            Some(内存块 { 地: p, 长 })
        }
    }
}

#[cfg(all(
    target_arch = "x86_64",
    any(target_os = "windows", target_os = "linux", target_os = "macos")
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
// 中间表示（IR）：一台「机器栈 + 槽」的小栈机
// ---------------------------------------------------------------------------

/// 二元运算。⚠️ 没有 `/`（结果会变浮点）与 `**`（要快速幂）。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(super) enum 运算 {
    加,
    减,
    乘,
    /// 向下取整的整除（`-7 // 3 == -3`）
    整除,
    /// 向下取整的取模（`-7 % 3 == 2`）
    取模,
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
    /// 把字面量推进栈
    压数(i64),
    /// `遍历`：把「当前索引处的元素」推进栈
    压元素,
    /// `遍历`：索引 + 1
    步进,
    /// 弹出两个、算一下、把结果推回去
    二元(运算),
    /// 弹出、取负、推回
    取负,
    /// 弹出、存进槽
    存槽(u8),
    /// 弹出、扔掉（表达式语句多余的值）
    丢弃,
    /// 弹出两个比较，把 `0` / `1` 推回栈
    比压(比较),
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
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum 槽位 {
    局部(usize),
    全局(usize),
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
    /// 标签总数（后端据此建表）。
    标数: usize,
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
fn 认循环(ins: &[crate::Instr], 常量们: &[Val], 起: usize, 终: usize) -> Result<方案, &'static str> {
    let 跳 = ins.get(终).ok_or("回跳那条指令越界")?;
    if 跳.op != op::JUMP || 跳.a < 0 || 跳.a as usize != 起 {
        return Err("这条回边不是跳回抬头的 JUMP");
    }
    if 终 <= 起 {
        return Err("循环只有一条回跳（空循环）");
    }
    let 首 = ins.get(起).ok_or("抬头越界")?;
    let 遍历 = 首.op == op::FOR_ITER;

    let mut 分配 = 分配::default();
    let mut 出: Vec<指令> = Vec::new();
    let mut 深: i32 = 0;              // 模拟栈深：负数就说明不是我们想的样子
    let mut 出口: Option<usize> = None;
    let mut 有比较出口 = false;       // 出口必须来自「比较」——`当 真` 那种不编
    let mut 中断口: usize = 0;
    let mut i: usize;
    /// 哪些 ip 是跳转落点（只有它们需要 `标`）。
    let mut 是目标 = vec![false; 终 - 起 + 1];

    // 🔴 **只给「真的有人跳过去」的 ip 打标签**（M182 的第二个真 bug）。
    // 第一版给每条字节码都打，结果 `压扁` 的两条融合形态全失效 ——
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
    出.push(指令::标(标头));

    if 遍历 {
        // `遍历` 的形状（见 `frontend/src/compiler.rs` 的 `c_for`）：
        //   GET_ITER / SETUP_LOOP <top> / top: FOR_ITER <exit> / STORE … / JUMP top
        //
        // ⚠️ 循环头**必须**在「查个数 + 取元素」之前：回跳回来要重新查一次，
        //    否则回跳就等于原地打转（M182 撞过：`标(标头)` 打在取元素之后，
        //    结果 `遍历` 要么空输出、要么死循环）。
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
        出.push(指令::越界跳(标出));
        出.push(指令::压元素);
        深 = 1;
        i = 起 + 1;
    } else {
        i = 起;
    }

    while i < 终 {
        if 是目标[i - 起] {
            出.push(指令::标(标签基 + (i - 起) as u16));
        }
        let x = ins.get(i).ok_or("循环体越界")?;
        match x.op {
            op::LOAD_FAST | op::LOAD_GLOBAL => {
                if x.a < 0 {
                    return Err("变量槽下标是负数");
                }
                let 位 = if x.op == op::LOAD_FAST {
                    槽位::局部(x.a as usize)
                } else {
                    槽位::全局(x.a as usize)
                };
                出.push(指令::压槽(分配.取(位).ok_or("这个循环用了超过 4 个变量")?));
                深 += 1;
                i += 1;
            }
            op::LOAD_CONST => {
                出.push(指令::压数(取整(常量们, x.a).ok_or("常量不是 i64 范围内的整数")?));
                深 += 1;
                i += 1;
            }
            op::BIN_OP => {
                if 深 < 2 {
                    return Err("二元运算前面栈不够深");
                }
                出.push(指令::二元(
                    认运算(x.a).ok_or("运算不支持（只编 + - * // %）")?,
                ));
                深 -= 1;
                i += 1;
            }
            op::UNARY_OP if x.a == 0 => {
                if 深 < 1 {
                    return Err("取负前面没有操作数");
                }
                出.push(指令::取负);
                i += 1;
            }
            op::STORE_FAST | op::STORE_GLOBAL => {
                if x.a < 0 {
                    return Err("变量槽下标是负数");
                }
                if 深 < 1 {
                    return Err("赋值前面没有值");
                }
                let 位 = if x.op == op::STORE_FAST {
                    槽位::局部(x.a as usize)
                } else {
                    槽位::全局(x.a as usize)
                };
                出.push(指令::存槽(分配.取(位).ok_or("这个循环用了超过 4 个变量")?));
                深 -= 1;
                i += 1;
            }
            op::POP_TOP => {
                if 深 < 1 {
                    return Err("丢弃前面没有值");
                }
                出.push(指令::丢弃);
                深 -= 1;
                i += 1;
            }
            op::COMPARE => {
                if 深 < 2 {
                    return Err("比较前面栈不够深");
                }
                // 🔴 **比较的结果只能直接用在分支上**：`x = (a < b)` 会把布尔值
                // 存进槽，而机器码只有整数 —— 那样循环结束后槽里会变成 `0/1`
                // 而不是 `真/假`（`类型()` 与 `打印` 都看得见）。所以整个循环不编。
                let 跟 = ins.get(i + 1).ok_or("比较后面没指令")?;
                if 跟.op != op::POP_JUMP_IF_FALSE && 跟.op != op::POP_JUMP_IF_TRUE {
                    return Err("比较的结果被存起来了（布尔值不在 JIT 的类型里）");
                }
                出.push(指令::比压(认比较(x.a).ok_or("比较码不认识")?));
                深 -= 1;
                i += 1;
            }
            op::POP_JUMP_IF_FALSE | op::POP_JUMP_IF_TRUE => {
                if x.a < 0 {
                    return Err("跳转目标是负数");
                }
                if 深 < 1 {
                    return Err("条件跳转前面没有值");
                }
                let 目标 = x.a as usize;
                let 标 = 标号(目标, 起, 终, 遍历, 中断口)
                    .ok_or("跳到了循环外面（不是出口也不是清理块）")?;
                if 目标 > 终 {
                    // 跳出循环 = 循环条件。**只允许一次**，而且必须来自比较
                    // （`当 真` 那种没有比较的循环编出来就是死循环 —— 不编）。
                    if 出口.is_some() && !遍历 {
                        return Err("出现了第二个「跳出循环」的分支");
                    }
                    if ins.get(i.wrapping_sub(1)).map(|p| p.op) != Some(op::COMPARE) {
                        return Err("循环条件不是比较（`当 真` 那种不编）");
                    }
                    出口 = Some(目标);
                    有比较出口 = true;
                }
                出.push(if x.op == op::POP_JUMP_IF_FALSE {
                    指令::零跳(标)
                } else {
                    指令::非零跳(标)
                });
                深 -= 1;
                i += 1;
            }
            op::JUMP => {
                if x.a < 0 {
                    return Err("跳转目标是负数");
                }
                let 标 = 标号(x.a as usize, 起, 终, 遍历, 中断口)
                    .ok_or("跳到了循环外面（不是出口也不是清理块）")?;
                出.push(指令::跳(标));
                i += 1;
            }
            op::CONTINUE_LOOP => {
                出.push(指令::跳(if 遍历 { 标续 } else { 标头 }));
                i += 1;
            }
            op::BREAK_LOOP => {
                出.push(指令::跳(if 遍历 { 标断 } else { 标出 }));
                i += 1;
            }
            // 函数调用 / 属性 / 下标 / 嵌套循环（`SETUP_LOOP`）/ 异常 … 一律不认。
            _ => return Err("循环体里有 JIT 不认识的指令（调用 / 下标 / 属性 / 嵌套循环 / 异常…）"),
        }
    }

    if 深 != 0 {
        return Err("循环体结束时栈不平（有半截表达式）");
    }
    let 出口 = 出口.ok_or("没找到循环出口")?;
    if !遍历 && !有比较出口 {
        return Err("循环条件不是比较");
    }

    let 中断 = if 遍历 { 中断口 } else { 出口 };

    if 遍历 {
        // 回跳的那条 `JUMP` 换成「打标 → 索引 +1 → 跳头」；`继续` 也从这儿过。
        出.push(指令::标(标签基 + (终 - 起) as u16));
        出.push(指令::标(标续));
        出.push(指令::步进);
        出.push(指令::跳(标头));
    } else {
        // 🔴 `如果 …:` 的假分支常常**正好落在回跳那条 `JUMP` 上**（ip == 终）——
        // 这个标签必须打，否则那条跳转没有落点（见 `发::收尾` 的说明）。
        出.push(指令::标(标签基 + (终 - 起) as u16));
        出.push(指令::跳(标头));
    }

    let 标数 = 标签基 as usize + (终 - 起) + 1;
    Ok(方案 {
        指令们: 压扁(&出),
        槽们: 分配.表,
        出口,
        中断口: 中断,
        是遍历: 遍历,
        标数,
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
fn 压扁(步们: &[指令]) -> Vec<指令> {
    let 取 = |k: usize| 步们.get(k).copied();
    let mut 出 = Vec::with_capacity(步们.len());
    let mut i = 0;
    while i < 步们.len() {
        if let (Some(a), Some(b)) = (当源(步们[i]), 取(i + 1).and_then(当源)) {
            // `[取, 取, 二元, 存槽]` → 「直算并存」（只对 + - * 成立：
            // 整除/取模要查除零，走自己的一条路）
            if let (Some(指令::二元(运算::加 | 运算::减 | 运算::乘)), Some(指令::存槽(k))) =
                (取(i + 2), 取(i + 3))
            {
                let op = match 取(i + 2) {
                    Some(指令::二元(o)) => o,
                    _ => unreachable!(),
                };
                出.push(指令::算存(a, op, b, k));
                i += 4;
                continue;
            }
            // `[取, 取, 比压, 零跳]` → 「直算并跳」
            if let (Some(指令::比压(c)), Some(指令::零跳(标))) = (取(i + 2), 取(i + 3)) {
                出.push(指令::比跳(a, c, b, 标));
                i += 4;
                continue;
            }
            if let (Some(指令::比压(c)), Some(指令::非零跳(标))) = (取(i + 2), 取(i + 3)) {
                出.push(指令::比真跳(a, c, b, 标));
                i += 4;
                continue;
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
    出口: usize,
    中断口: usize,
    是遍历: bool,
    入口: 入口,
    /// 只是为了让内存在 `Drop` 时还回去（`入口` 指着它）。
    _块: 内存块,
}

impl 已编 {
    /// 这个循环用到的槽（顺序 = 摊平缓冲里各元素的位置）。
    pub fn 槽们(&self) -> &[槽位] {
        &self.槽们
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
    let 方案 = match 认循环(指令们, 常量们, 起, 终) {
        Ok(x) => x,
        Err(因) => {
            if std::env::var("JISHI_JIT_DEBUG").is_ok() || std::env::var("JISHI_JIT").as_deref() == Ok("调试") {
                eprintln!("[jit] 抬头 {起} 回跳 {终} 编不了：{因}");
            }
            return None;
        }
    };
    let 码 = match x86_64::生成(&方案) {
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
    let 块 = 申请(码.len())?;
    // SAFETY: `码` 是刚发出来的机器码，`块` 至少有 `码.len()` 字节可写可执行。
    unsafe {
        std::ptr::copy_nonoverlapping(码.as_ptr(), 块.地, 码.len());
    }
    // SAFETY: 约定好的签名是 `extern "C" fn(*mut i64, *const i64) -> i64`，
    // 生成码就是这么写的（两个参数从 rcx/rdx 或 rdi/rsi 取，返回值放 rax）。
    let 入口: 入口 = unsafe { std::mem::transmute(块.地) };
    Some(已编 {
        槽们: 方案.槽们,
        出口: 方案.出口,
        中断口: 方案.中断口,
        是遍历: 方案.是遍历,
        入口,
        _块: 块,
    })
}

// ---------------------------------------------------------------------------
// 热点计数
// ---------------------------------------------------------------------------

/// 回跳多少次之后开始编。**一次编译几十微秒**，对几百次的循环不值当。
const 阈值: u32 = 1000;

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

    /// 这个回边又转了一圈。返回 `true` = 该进 [`crate::jit::编译`] 那条路了
    /// （**跨阈值那一次**，以及**编好之后的每一次**）。
    #[inline]
    pub fn 记一步(&mut self, 代码号: usize, 回跳ip: usize) -> bool {
        let c = &mut self.计数[位次(代码号, 回跳ip)];
        if *c == 已编好 {
            return true;
        }
        if *c < 阈值 {
            *c += 1;
            return *c == 阈值;
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
}
