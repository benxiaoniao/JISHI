//! x86-64 后端：把 IR 发成机器码。
//!
//! 只用手写的那几十条指令，**零依赖**（没有 cranelift / llvm）。
//!
//! ## 寄存器怎么分
//!
//! | 寄存器 | 谁用 | 说明 |
//! |---|---|---|
//! | `r8`–`r11` | 变量槽（最多 4 个） | **caller-saved**，返回路径上不用 `pop` |
//! | `rax` / `rdx` / `rcx` | 临时（栈式表达式的左右操作数） | |
//! | `rbx` | `遍历` 的**索引** | callee-saved ⇒ 进场 `push`、三条出口都 `pop` |
//! | `rsi` | `遍历` 的**元素基址** | 同上（Windows 的参数 2 本来在 `rdx`，进场搬过来） |
//! | `rbp` | 栈帧（只为了「去优化时能恢复 `rsp`」） | |
//!
//! 🔴 **槽缓冲指针存在 `[rbp-8]`** 而不是留在参数寄存器里：`//` `%` 要用 `rcx`
//! 做除数（Windows 的参数 1 就是 `rcx`），留着它写回时就被踩了。
//!
//! ## 三条出口
//!
//! | 标签 | 含义 | 写回槽？ | 返回码 |
//! |---|---|---|---|
//! | 1 | 正常跑完（`当` 条件假 / `遍历` 元素耗尽） | ✅ | 0 |
//! | 3 | `中断` | ✅ | 2 |
//! | 2 | 去优化（溢出 / 除零 / `MIN / -1`） | ❌ | 1 |
//!
//! ⚠️ **去优化绝对不能写回**：写回一半，解释器再从抬头重跑，就把已经做过的加法
//! 又做了一遍。宁可整段丢掉。

use super::{方案, 指令, 比较, 运算, 源, 类型, 标优, 标出, 标断};

// ---------------------------------------------------------------------------
// 寄存器
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
const RBX: 寄 = 寄 { 号: 3, 扩: false };
const RSP: 寄 = 寄 { 号: 4, 扩: false };
const RBP: 寄 = 寄 { 号: 5, 扩: false };
const RSI: 寄 = 寄 { 号: 6, 扩: false };

/// 浮点槽用 `xmm0`–`xmm3`（Windows 与 System V 上都是 **caller-saved**，
/// 所以不用保存）。下标与整数槽一样，只是落在另一组寄存器里。
const 槽x: [u8; 4] = [0, 1, 2, 3];

/// 浮点的临时寄存器：`xmm4` / `xmm5`（左右操作数）、`xmm6`（零）。
///
/// ⚠️ 不能用 `xmm0`–`xmm3`：那是槽。也不能用 `xmm6`–`xmm15`：Windows 上
/// 它们是 **callee-saved**，用了就得多 push/pop。
const X4: u8 = 4;
const X5: u8 = 5;
const X6: u8 = 6;

/// 槽寄存器只有四个：`r8`–`r11`（caller-saved）。
const 槽寄: [寄; 4] = [
    寄 { 号: 0, 扩: true }, // r8
    寄 { 号: 1, 扩: true }, // r9
    寄 { 号: 2, 扩: true }, // r10
    寄 { 号: 3, 扩: true }, // r11
];

/// 参数寄存器 —— **Windows 和 System V 不一样**。
#[cfg(windows)]
const 参1: 寄 = 寄 { 号: 1, 扩: false }; // rcx
#[cfg(not(windows))]
const 参1: 寄 = 寄 { 号: 7, 扩: false }; // rdi

#[cfg(windows)]
const 参2: 寄 = 寄 { 号: 2, 扩: false }; // rdx
#[cfg(not(windows))]
const 参2: 寄 = 寄 { 号: 6, 扩: false }; // rsi

// ---------------------------------------------------------------------------
// 发射器
// ---------------------------------------------------------------------------

struct 发 {
    码: Vec<u8>,
    /// 待补的 32 位相对跳转：(rel32 的起始位置, 标签号)
    补: Vec<(usize, u16)>,
    标: Vec<Option<usize>>,
}

impl 发 {
    fn new(标数: usize) -> Self {
        发 { 码: Vec::with_capacity(512), 补: Vec::new(), 标: vec![None; 标数] }
    }

    /// 临时标签（后端自己用的那种，比如除法里的两个落点）。
    fn 新标(&mut self) -> u16 {
        self.标.push(None);
        (self.标.len() - 1) as u16
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
    fn 取(&mut self, 目: 寄, 基: 寄, 偏: i8) {
        self.rex(true, 目.扩, false, 基.扩);
        self.b(0x8B);
        self.modrm(0b01, 目, 基);
        self.b(偏 as u8);
    }

    /// `mov [基 + 偏], 源`（`89 /r`）
    fn 存(&mut self, 基: 寄, 偏: i8, 源: 寄) {
        self.rex(true, 源.扩, false, 基.扩);
        self.b(0x89);
        self.modrm(0b01, 源, 基);
        self.b(偏 as u8);
    }

    /// `mov 目, 源`（`89 /r`，mod=11）
    fn 搬(&mut self, 目: 寄, 源: 寄) {
        self.rex(true, 源.扩, false, 目.扩);
        self.b(0x89);
        self.modrm(0b11, 源, 目);
    }

    /// `mov 目, imm64`（`REX.W B8+rd io`）
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

    /// `cmp 目, imm8`（`83 /7 ib`，符号扩展到 64 位）
    fn 比立即(&mut self, 目: 寄, 值: i8) {
        self.rex(true, false, false, 目.扩);
        self.b(0x83);
        self.modrm(0b11, 寄 { 号: 7, 扩: false }, 目);
        self.b(值 as u8);
    }

    /// `imul 目, 源`（`REX.W 0F AF /r`，注意 reg 是**目的**）
    fn 乘(&mut self, 目: 寄, 源: 寄) {
        self.rex(true, 目.扩, false, 源.扩);
        self.b(0x0F);
        self.b(0xAF);
        self.modrm(0b11, 目, 源);
    }

    /// `mov 目, [基 + 索×8 + 偏]`（`8B /r` + SIB）—— 只有 `遍历` 用。
    fn 载下标(&mut self, 目: 寄, 基: 寄, 索: 寄, 偏: i8) {
        self.rex(true, 目.扩, 索.扩, 基.扩);
        self.b(0x8B);
        self.modrm(0b01, 目, 寄 { 号: 4, 扩: false });      // rm=100 ⇒ 后面跟 SIB
        self.b((3 << 6) | ((索.号 & 7) << 3) | (基.号 & 7)); // scale=8, index, base
        self.b(偏 as u8);
    }

    /// `cmp 目, [基]`（`3B /r`，mod=00）—— 比 `遍历` 的索引和元素个数。
    fn 比存(&mut self, 目: 寄, 基: 寄) {
        self.rex(true, 目.扩, false, 基.扩);
        self.b(0x3B);
        self.modrm(0b00, 目, 基);
    }

    /// `test 目, 目`（`85 /r`）
    fn 测(&mut self, 目: 寄) {
        self.rex(true, 目.扩, false, 目.扩);
        self.b(0x85);
        self.modrm(0b11, 目, 目);
    }

    /// `xor 目, 源`（`31 /r`）
    fn 异或(&mut self, 目: 寄, 源: 寄) {
        self.rex(true, 源.扩, false, 目.扩);
        self.b(0x31);
        self.modrm(0b11, 源, 目);
    }

    /// `xor 目, 目`（32 位形态：顺手把高 32 位清零）
    fn 清零(&mut self, 目: 寄) {
        self.rex(false, 目.扩, false, 目.扩);
        self.b(0x31);
        self.modrm(0b11, 目, 目);
    }

    /// `inc 目`（`FF /0`）
    fn 加一(&mut self, 目: 寄) {
        self.rex(true, false, false, 目.扩);
        self.b(0xFF);
        self.modrm(0b11, 寄 { 号: 0, 扩: false }, 目);
    }

    /// `dec 目`（`FF /1`）
    fn 减一(&mut self, 目: 寄) {
        self.rex(true, false, false, 目.扩);
        self.b(0xFF);
        self.modrm(0b11, 寄 { 号: 1, 扩: false }, 目);
    }

    /// `neg 目`（`F7 /3`）
    fn 取负(&mut self, 目: 寄) {
        self.rex(true, false, false, 目.扩);
        self.b(0xF7);
        self.modrm(0b11, 寄 { 号: 3, 扩: false }, 目);
    }

    /// `idiv 目`（`F7 /7`）—— ⚠️ 除零与 `MIN / -1` 是 **SIGFPE**（进程直接死），
    /// 调用方必须先查过这两条。
    fn 除(&mut self, 目: 寄) {
        self.rex(true, false, false, 目.扩);
        self.b(0xF7);
        self.modrm(0b11, 寄 { 号: 7, 扩: false }, 目);
    }

    /// `cqo`：把 `rax` 的符号扩展到 `rdx:rax`（`idiv` 的前置）
    fn 阔号(&mut self) {
        self.b(0x48);
        self.b(0x99);
    }

    /// `setcc al` + `movzx rax, al`：把刚才那次 `cmp` 的结果变成 0 / 1 放进 `rax`。
    fn 比成(&mut self, 比: 比较) {
        self.b(0x0F);
        self.b(真条件(比));
        self.modrm(0b11, 寄 { 号: 0, 扩: false }, RAX);
        self.b(0x48);
        self.b(0x0F);
        self.b(0xB6);
        self.modrm(0b11, RAX, RAX);
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

    /// `66 0F xx /r`：`xmm目 ← xmm目 <op> xmm源`（SSE 标量，都是 mod=11）
    fn sse(&mut self, 目: u8, 源: u8, 码: u8) {
        self.b(0x66);
        self.b(0x0F);
        self.b(码);
        self.modrm(0b11, 寄 { 号: 目, 扩: false }, 寄 { 号: 源, 扩: false });
    }

    /// `F2 0F 10 /r`：`movsd xmm, [基 + 偏]`
    fn 载x(&mut self, 目: u8, 基: 寄, 偏: i8) {
        self.b(0xF2);
        self.b(0x0F);
        self.b(0x10);
        self.modrm(0b01, 寄 { 号: 目, 扩: false }, 基);
        self.b(偏 as u8);
    }

    /// `F2 0F 11 /r`：`movsd [基 + 偏], xmm`
    fn 存x(&mut self, 基: 寄, 偏: i8, 源: u8) {
        self.b(0xF2);
        self.b(0x0F);
        self.b(0x11);
        self.modrm(0b01, 寄 { 号: 源, 扩: false }, 基);
        self.b(偏 as u8);
    }

    /// `66 REX.W 0F 6E /r`：`movq xmm, r64`（把整数寄存器里的**位**搬进 xmm）
    fn 入x(&mut self, 目: u8, 源: 寄) {
        self.b(0x66);
        self.rex(true, false, false, 源.扩);
        self.b(0x0F);
        self.b(0x6E);
        self.modrm(0b11, 寄 { 号: 目, 扩: false }, 源);
    }

    /// `66 REX.W 0F 7E /r`：`movq r64, xmm`
    fn 出x(&mut self, 目: 寄, 源: u8) {
        self.b(0x66);
        self.rex(true, false, false, 目.扩);
        self.b(0x0F);
        self.b(0x7E);
        self.modrm(0b11, 寄 { 号: 源, 扩: false }, 目);
    }

    /// `F2 REX.W 0F 2A /r`：`cvtsi2sd xmm, r64`（整数 → 浮点）
    fn 转浮(&mut self, 目: u8, 源: 寄) {
        self.b(0xF2);
        self.rex(true, false, false, 源.扩);
        self.b(0x0F);
        self.b(0x2A);
        self.modrm(0b11, 寄 { 号: 目, 扩: false }, 源);
    }

    /// `66 0F 2E /r`：`ucomisd xmm目, xmm源` —— 浮点比较（置 ZF/PF/CF）
    fn 比x(&mut self, 目: u8, 源: u8) {
        self.sse(目, 源, 0x2E);
    }

    /// `setcc r/m8`：把条件码写成一个字节（`目` 只用 AL / CL 这种低位寄存器）
    fn 设cc(&mut self, 码: u8, 目: 寄) {
        self.b(0x0F);
        self.b(码);
        self.modrm(0b11, 寄 { 号: 0, 扩: false }, 目);
    }

    /// `and al, cl` / `or al, cl`（8 位，只有低两位有意义）
    fn 字节与(&mut self, 码: u8) {
        self.b(码);
        self.b(0xC8); // modrm: al ← al <op> cl
    }

    /// 跳转：`码` 是 1 个字节（`E9`）或 2 个字节（`0F 8x`），后面跟 rel32。
    fn 跳(&mut self, 码: &[u8], 标: u16) {
        for x in 码 {
            self.b(*x);
        }
        self.补.push((self.码.len(), 标));
        self.码.extend_from_slice(&[0, 0, 0, 0]);
    }

    fn 打标(&mut self, 标: u16) {
        self.标[标 as usize] = Some(self.码.len());
    }

    /// 补齐所有 `rel32` 落点。
    ///
    /// 🔴 **有落点没打上就整段作废**（返回 `None`）。这个检查是 M182 补的：
    /// 第一版遇到「跳到一个没打过的标签」时 `continue` 跳过，那 4 个字节就留在
    /// `0` —— 而 `rel32 = 0` 的意思是「跳到自己」，也就是**这条跳转直接失效**。
    /// 症状极难查：`如果 i < 25000：s = s + 1` 会变成「每圈都加」，而程序不崩、
    /// 不报错，只是数字不对。（真因是 `当` 循环那条落在 `终` 上的标签没打。）
    fn 收尾(mut self) -> Option<Vec<u8>> {
        for (位, 标) in &self.补 {
            let 到 = self.标.get(*标 as usize).copied().flatten()?;
            let 差 = 到 as i64 - (*位 as i64 + 4);
            self.码[*位..*位 + 4].copy_from_slice(&(差 as i32).to_le_bytes());
        }
        Some(self.码)
    }
}

/// 比较成立时的 `setcc` 第二字节：0 `==`、1 `!=`、2 `<`、3 `>`、4 `<=`、5 `>=`。
fn 真条件(比: 比较) -> u8 {
    match 比 {
        比较::等 => 0x94,     // sete
        比较::不等 => 0x95,   // setne
        比较::小于 => 0x9C,   // setl
        比较::大于 => 0x9F,   // setg
        比较::小于等 => 0x9E, // setle
        比较::大于等 => 0x9D, // setge
    }
}

/// 「比较**不**成立就跳」的 `Jcc` 第二字节。
fn 假跳(比: 比较) -> u8 {
    match 比 {
        比较::等 => 0x85,     // jne
        比较::不等 => 0x84,   // je
        比较::小于 => 0x8D,   // jge
        比较::大于 => 0x8E,   // jle
        比较::小于等 => 0x8F, // jg
        比较::大于等 => 0x8C, // jl
    }
}

/// 「比较成立就跳」的 `Jcc` 第二字节。
fn 真跳(比: 比较) -> u8 {
    match 比 {
        比较::等 => 0x84,     // je
        比较::不等 => 0x85,   // jne
        比较::小于 => 0x8C,   // jl
        比较::大于 => 0x8F,   // jg
        比较::小于等 => 0x8E, // jle
        比较::大于等 => 0x8D, // jge
    }
}

/// 把一个操作数搬进指定寄存器。
fn 装(f: &mut 发, 目: 寄, x: 源) {
    match x {
        源::槽(k) => f.搬(目, 槽寄[k as usize]),
        源::数(v) => f.立即(目, v),
    }
}

/// 压一个立即数进栈。
fn 压数(f: &mut 发, v: i64) {
    if i32::try_from(v).is_ok() {
        f.b(0x68);   // push imm32（符号扩展到 64 位）
        f.码.extend_from_slice(&(v as i32).to_le_bytes());
    } else {
        f.立即(RAX, v);
        f.压(RAX);
    }
}

/// 整数 `+ - *`：弹出两个、算、溢出就跳去优化、结果压回。
fn 发整数算(f: &mut 发, op: 运算) {
    f.弹(RDX);                        // 右操作数
    f.弹(RAX);                        // 左操作数
    match op {
        运算::加 => f.算(0x01, RAX, RDX),
        运算::减 => f.算(0x29, RAX, RDX),
        _ => f.乘(RAX, RDX),
    }
    f.跳(&[0x0F, 0x80], 标优);        // jo 去优化
    f.压(RAX);
}

/// 浮点的 `+ - * /`（M183）。
///
/// 🔴 **`/` 的除零必须查**：基石里 `1.0 / 0.0` 报 `E2001 不能除以零`，
/// **不是 `inf`**（Python 的 `ZeroDivisionError` 同款）。而 `divsd` 会安安静静
/// 给一个 `inf` —— 那条语义差别肉眼看不出来，只有判据能抓。所以先 `ucomisd`
/// 跟 0 比：`±0.0` → 去优化（交给解释器报错），NaN → 放行（NaN 不是零）。
fn 发浮点算(f: &mut 发, op: 运算, 左转: bool, 右转: bool) {
    f.弹(RDX);
    f.弹(RAX);
    if 左转 {
        f.转浮(X4, RAX);
    } else {
        f.入x(X4, RAX);
    }
    if 右转 {
        f.转浮(X5, RDX);
    } else {
        f.入x(X5, RDX);
    }
    match op {
        运算::加 => f.sse(X4, X5, 0x58),   // addsd
        运算::减 => f.sse(X4, X5, 0x5C),   // subsd
        运算::乘 => f.sse(X4, X5, 0x59),   // mulsd
        运算::除法 => {
            let 跳过 = f.新标();
            f.sse(X6, X6, 0x57);           // xorpd xmm6, xmm6（0.0）
            f.比x(X5, X6);                 // ucomisd 除数, 0
            // jp 先跳：NaN 会同时置 ZF 和 PF，而 NaN **不是**除零
            f.跳(&[0x0F, 0x8A], 跳过);     // jp
            f.跳(&[0x0F, 0x84], 标优);     // je → 去优化（解释器报 E2001）
            f.打标(跳过);
            f.sse(X4, X5, 0x5E);           // divsd
        }
        _ => {}
    }
    f.出x(RAX, X4);
    f.压(RAX);
}

/// 浮点的比较 —— **NaN 是这一段的全部难点**。
///
/// `ucomisd a, b` 在「无序」（有 NaN）时置 `PF=1`、`ZF=1`、`CF=1`，于是：
///
/// | 比较 | 直接 `setcc` 会错成 | 正确做法 |
/// |---|---|---|
/// | `a == b` | `sete` 把 NaN 当「相等」 | `sete` **且** `setnp` |
/// | `a < b`  | `setb` 把 NaN 当「小于」 | `setb` **且** `setnp` |
/// | `a <= b` | `setbe` 把 NaN 当「小于等于」 | `setbe` **且** `setnp` |
/// | `a != b` | `setne` 把 NaN 当「相等」⇒ 给假 | `setne` **或** `setp`（NaN 必不相等） |
/// | `a > b`  | `seta` 在无序时本就给假 ✅ | 直接 `seta` |
/// | `a >= b` | `setae` 在无序时本就给假 ✅ | 直接 `setae` |
fn 发浮点比(f: &mut 发, 比较: 比较, 左转: bool, 右转: bool) {
    f.弹(RDX);
    f.弹(RAX);
    if 左转 {
        f.转浮(X4, RAX);
    } else {
        f.入x(X4, RAX);
    }
    if 右转 {
        f.转浮(X5, RDX);
    } else {
        f.入x(X5, RDX);
    }
    f.比x(X4, X5);
    match 比较 {
        比较::大于 => f.设cc(0x97, RAX),      // seta
        比较::大于等 => f.设cc(0x93, RAX),    // setae
        比较::小于 => {
            f.设cc(0x92, RAX);                // setb
            f.设cc(0x9B, RCX);                // setnp
            f.字节与(0x20);                    // and al, cl
        }
        比较::小于等 => {
            f.设cc(0x96, RAX);                // setbe
            f.设cc(0x9B, RCX);                // setnp
            f.字节与(0x20);
        }
        比较::等 => {
            f.设cc(0x94, RAX);                // sete
            f.设cc(0x9B, RCX);                // setnp
            f.字节与(0x20);
        }
        比较::不等 => {
            f.设cc(0x95, RAX);                // setne
            f.设cc(0x9A, RCX);                // setp
            f.字节与(0x08);                    // or al, cl
        }
    }
    // movzx rax, al
    f.b(0x48);
    f.b(0x0F);
    f.b(0xB6);
    f.modrm(0b11, RAX, RAX);
    f.压(RAX);
}

/// `//` `%`：**向下取整**语义（x86 的 `idiv` 是截断，所以要修）。
///
/// 三道检查，少一条就是进程直接死（`idiv` 出错是 **SIGFPE**，不是异常）：
/// 1. 除数为 0 → 去优化（解释器报 `E2001`）；
/// 2. 除数 `-1` → 单独走（`MIN / -1` 会溢出；`neg` 有 `jo` 兜着）；
/// 3. 余数与除数**异号**时 `商 -= 1`、`余 += 除数`（这才叫向下取整）。
fn 发除模(f: &mut 发, op: 运算) {
    let 正常 = f.新标();
    let 完成 = f.新标();

    f.弹(RDX);                        // 右（除数）
    f.弹(RAX);                        // 左（被除数）
    f.搬(RCX, RDX);
    f.测(RCX);
    f.跳(&[0x0F, 0x84], 标优);        // 除数为 0 → 去优化
    f.比立即(RCX, -1);
    f.跳(&[0x0F, 0x85], 正常);        // 除数 != -1 → 正常路
    // 除数 == -1：商 = -被除数（`MIN` 会溢出 → 去优化），余数恒为 0
    f.取负(RAX);
    f.跳(&[0x0F, 0x80], 标优);        // jo
    f.清零(RDX);
    f.跳(&[0xE9], 完成);
    f.打标(正常);
    f.阔号();
    f.除(RCX);                        // 商 → rax，余 → rdx
    f.测(RDX);
    f.跳(&[0x0F, 0x84], 完成);        // 余 0 → 不用修
    // 借栈比一下符号（`push` / `pop` 不改标志位，所以 `pop` 之后的 `jns` 还看 `xor` 的结果）
    f.压(RDX);
    f.异或(RDX, RCX);
    f.弹(RDX);
    f.跳(&[0x0F, 0x89], 完成);        // jns：同号 → 不用修
    f.减一(RAX);
    f.算(0x01, RDX, RCX);             // 余 += 除数
    f.打标(完成);
    if op == 运算::整除 {
        f.压(RAX);
    } else {
        f.压(RDX);
    }
}

/// 三条出口共用的「收尾」：恢复栈、按需写回、返回码。
fn 收(f: &mut 发, 方案: &方案, 返回码: i32, 写回: bool) {
    // 槽缓冲指针（进场时存在 [rbp-8]）——**必须在恢复 rsp 之前**取出来
    f.取(RDX, RBP, -8);
    f.搬(RSP, RBP);
    f.弹(RBP);
    if 方案.是遍历 {
        f.弹(RSI);
        f.弹(RBX);
    }
    if 写回 {
        for (k, 型) in 方案.槽型.iter().enumerate() {
            match 型 {
                类型::整数 => f.存(RDX, (k * 8) as i8, 槽寄[k]),
                类型::浮点 => f.存x(RDX, (k * 8) as i8, 槽x[k]),
            }
        }
    }
    f.b(0xB8);                                  // mov eax, imm32
    f.码.extend_from_slice(&返回码.to_le_bytes());
    f.b(0xC3);                                  // ret
}

/// 把 IR 发成机器码。**有任何一条跳转找不到落点就返回 `None`**（宁可不编）。
pub(super) fn 生成(方案: &方案) -> Option<Vec<u8>> {
    let mut f = 发::new(方案.标数);

    // 1) 保存 callee-saved（只有 `遍历` 用 rbx / rsi）
    if 方案.是遍历 {
        f.压(RBX);
        f.压(RSI);
    }
    // 2) 装槽（**必须在动参数寄存器之前**）
    //
    //    整数槽进 `r8`–`r11`，浮点槽进 `xmm0`–`xmm3`；两者在缓冲里都占 8 字节，
    //    靠 `方案.槽型` 决定怎么解释那 8 个字节（`Val::Int` 的 i64 / `Val::Float`
    //    的 f64 位模式）。
    for (k, 型) in 方案.槽型.iter().enumerate() {
        match 型 {
            类型::整数 => f.取(槽寄[k], 参1, (k * 8) as i8),
            类型::浮点 => f.载x(槽x[k], 参1, (k * 8) as i8),
        }
    }
    // 3) 栈帧
    f.压(RBP);
    // ⚠️ 方向别写反：这里是 **mov rbp, rsp**（把当前栈顶记进 rbp）。
    //    写成 `mov rsp, rbp` 就是「把栈指针设成一个垃圾值」，下一步 push 立刻段错误。
    f.搬(RBP, RSP);
    // 4) 槽缓冲指针存到 [rbp-8]（除法要用 rcx）
    f.压(参1);
    // 5) `遍历`：元素基址搬进 rsi、索引清零
    if 方案.是遍历 {
        #[cfg(windows)]
        f.搬(RSI, 参2);
        f.清零(RBX);
    }

    for 步 in &方案.指令们 {
        match *步 {
            指令::标(标) => f.打标(标),
            指令::跳(标) => f.跳(&[0xE9], 标),
            指令::零跳(标) => {
                f.弹(RAX);
                f.测(RAX);
                f.跳(&[0x0F, 0x84], 标);
            }
            指令::非零跳(标) => {
                f.弹(RAX);
                f.测(RAX);
                f.跳(&[0x0F, 0x85], 标);
            }
            指令::越界跳(标) => {
                f.比存(RBX, RSI);
                f.跳(&[0x0F, 0x8D], 标);      // jge：索引 >= 个数
            }
            指令::压槽(k) => match 方案.槽型[k as usize] {
                类型::整数 => f.压(槽寄[k as usize]),
                类型::浮点 => {
                    f.出x(RAX, 槽x[k as usize]);
                    f.压(RAX);
                }
            },
            指令::压数(v) => 压数(&mut f, v),
            指令::压浮点(位) => 压数(&mut f, 位 as i64),
            指令::压元素 => {
                // [rsi + 8 + rbx*8]：第 0 个是个数，元素从第 1 个起
                f.载下标(RAX, RSI, RBX, 8);
                f.压(RAX);
            }
            指令::步进 => f.加一(RBX),
            指令::二元 { 运算: op, 类型, 左转, 右转 } => match 类型 {
                类型::整数 => match op {
                    运算::加 | 运算::减 | 运算::乘 => 发整数算(&mut f, op),
                    运算::除法 => {}
                    _ => 发除模(&mut f, op),
                },
                类型::浮点 => 发浮点算(&mut f, op, 左转, 右转),
            },
            指令::取负浮点 => {
                f.弹(RAX);
                f.入x(X4, RAX);
                f.立即(RAX, i64::MIN);   // 只有最高位是 1 = f64 的符号位
                f.入x(X5, RAX);
                f.sse(X4, X5, 0x57);     // xorpd：翻符号位（-0.0 也照翻）
                f.出x(RAX, X4);
                f.压(RAX);
            }
            指令::取负 => {
                f.弹(RAX);
                f.取负(RAX);
                f.跳(&[0x0F, 0x80], 标优);    // jo（-i64::MIN 会溢出）
                f.压(RAX);
            }
            指令::存槽(k) => match 方案.槽型[k as usize] {
                类型::整数 => f.弹(槽寄[k as usize]),
                类型::浮点 => {
                    f.弹(RAX);
                    f.入x(槽x[k as usize], RAX);
                }
            },
            指令::丢弃 => f.弹(RAX),
            指令::比压 { 比较: c, 类型, 左转, 右转 } => match 类型 {
                类型::整数 => {
                    f.弹(RDX);
                    f.弹(RAX);
                    f.算(0x39, RAX, RDX);      // cmp rax, rdx
                    f.比成(c);                 // al = 0/1
                    f.压(RAX);
                }
                类型::浮点 => 发浮点比(&mut f, c, 左转, 右转),
            },
            指令::算存(左, op, 右, k) => {
                装(&mut f, RAX, 左);
                装(&mut f, RDX, 右);
                match op {
                    运算::加 => f.算(0x01, RAX, RDX),
                    运算::减 => f.算(0x29, RAX, RDX),
                    _ => f.乘(RAX, RDX),
                }
                f.跳(&[0x0F, 0x80], 标优);    // jo 去优化
                f.搬(槽寄[k as usize], RAX);
            }
            指令::比跳(左, c, 右, 标) => {
                装(&mut f, RAX, 左);
                装(&mut f, RDX, 右);
                f.算(0x39, RAX, RDX);
                f.跳(&[0x0F, 假跳(c)], 标);
            }
            指令::比真跳(左, c, 右, 标) => {
                装(&mut f, RAX, 左);
                装(&mut f, RDX, 右);
                f.算(0x39, RAX, RDX);
                f.跳(&[0x0F, 真跳(c)], 标);
            }
        }
    }

    // 正常出口（0）
    f.打标(标出);
    收(&mut f, 方案, 0, true);
    // `中断`（2）—— **要写回**：跳出循环之后那些变量还要用
    f.打标(标断);
    收(&mut f, 方案, 2, true);
    // 去优化（1）—— **不写回**
    f.打标(标优);
    收(&mut f, 方案, 1, false);

    f.收尾()
}
