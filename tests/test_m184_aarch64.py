# -*- coding: utf-8 -*-
"""M184：JIT 的第二个后端 —— AArch64（ARM64）。

## 这一组判据为什么长这样

开发机是 **Windows x86-64**，跑不了 ARM64 机器码。所以「ARM 后端对不对」这件事
被拆成三层，每一层**都能在这里验**（除了最后一层）：

| 层 | 谁来验 | 这一组管不管 |
|---|---|---|
| 每条指令的 32 位编码对不对 | 开发期用 capstone 逐条反汇编核对（`build/ev/m184/enc*.py`，不进仓库） | ❌ 不进判据（capstone 是第三方，不能进闸门） |
| **生成的字节结构对不对** | 这里 —— 靠 `JISHI_JIT_DUMP_ARM` 探针在 x86 上也能编一遍 | ✅ **这一组的主要工作** |
| **机器码跑起来对不对** | CI 的 `macos-latest`（arm64）真跑 M182/M183 那 17 个形态 | ⚠️ 本机做不到，见 `docs/路线图-0.3.x.md` §M184 |

## 判据的核心手法：**全量白名单**

最有力的一条是 `test_每条指令都认得出`：把生成的每个 32 位字都丢进一张
「我认识的指令」表，**任何一个字认不出来就红**。它的价值在于「预期之外」——
如果后端某天多发了一条指令（或者 imm 位没补、编码写错位段），这一条立刻响，
不需要事先想到那个场景。

反过来也如实说：**白名单本身也可能错**（我按同一份编码表写两边）。
真正的一锤定音是 CI 上的真机执行 —— 那里错一个位段就是算错数，跑不过 M182 的
「开/关 JIT 逐字节对拍」。
"""

import hashlib
import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")

是已知架构 = platform.machine().lower() in ("amd64", "x86_64", "arm64", "aarch64")
需要jit = pytest.mark.skipif(
    not 是已知架构 or not EXE.is_file(),
    reason="要在 x86-64 或 aarch64 上先 cargo build --release",
)


# ---------------------------------------------------------------------------
# 拿字节
# ---------------------------------------------------------------------------

def _跑(源码: str, 环境: dict, 超时=180):
    # 🔴 文件名**每次都不一样**：`_跑` 会写 `.jsh` / `.out` / `.err` 三个文件，
    # 复用同一个名字时出过一次「明明有输出却读到空文件」的怪事（M184 查了很久）。
    # 判据跑得不快，不值得为省几个临时文件冒这个险。
    路径 = str(
        Path(tempfile.gettempdir())
        / f"jishi_m184_{os.getpid()}_{hashlib.md5(源码.encode('utf-8')).hexdigest()[:10]}"
          f"_{os.urandom(4).hex()}.jsh"
    )
    Path(路径).write_text(源码, encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env.update(环境)
    出 = 路径 + ".out"
    错 = 路径 + ".err"
    with open(出, "wb") as fo, open(错, "wb") as fe:
        r = subprocess.run(
            [str(EXE), 路径], stdout=fo, stderr=fe, env=env, timeout=超时,
        )
    # 走文件而不是管道：`capture_output` 在某些情况下会**读不到子进程已经写出的
    # 内容**（本项目在 WebView2 那件事上踩过同类坑）。判据要的是「子进程到底
    # 写了什么」，所以直接落盘最稳。
    r.stdout = Path(出).read_bytes().decode("utf-8", "replace")
    r.stderr = Path(错).read_bytes().decode("utf-8", "replace")
    r.调试文件 = 路径          # type: ignore[attr-defined]
    r.调试环境 = {k: v for k, v in env.items() if k.startswith("JISHI_")}  # type: ignore[attr-defined]
    return r


def _arm(源码: str, 应当编=True):
    """跑一遍，拿 **ARM64 后端**编出来的字节；编不了返回 `None`。

    `JISHI_JIT_DUMP_ARM` 让 `编译()` 顺手再编一遍 aarch64 —— 在 x86-64 上也一样，
    这正是本机唯一能碰到 ARM 后端的口子。设了它就会**强制打开 JIT**（不受
    `JISHI_JIT=0` 影响），所以这个探针是确定性的。

    `应当编=False` 用于「这个循环本来就不该进 JIT」的用例（末尾 `继续` 那类
    没有回边的形态）：那时**一行输出都没有**才是正常的。
    """
    r = _跑(源码, {"JISHI_JIT_DUMP_ARM": "1", "JISHI_JIT": "调试"})
    for 行 in r.stderr.splitlines():
        m = re.match(r"\[jit-arm\] (\d+) 字节：(.+)$", 行)
        if m:
            十六 = m.group(2).split()
            assert int(m.group(1)) == len(十六), f"字节数和列表长度对不上：{行[:80]}"
            return bytes(int(x, 16) for x in 十六)
        if "编不了" in 行:
            return None
    if not 应当编:
        return None
    raise AssertionError(
        f"没有 ARM 探针输出（这个循环应当能编，却没有）：\n"
        f"  退出码={r.returncode}\n  标准输出={r.stdout!r}\n  标准错误={r.stderr!r}\n"
        f"  文件={r.调试文件}（{Path(r.调试文件).stat().st_size} 字节）")


def _字(码: bytes):
    assert len(码) % 4 == 0, f"ARM64 指令必须是 4 字节定长，长度却是 {len(码)}"
    return [int.from_bytes(码[i:i + 4], "little") for i in range(0, len(码), 4)]


# ---------------------------------------------------------------------------
# 指令白名单（mask, 值, 名字）—— 顺序：固定值优先，然后更长的 mask 优先
# ---------------------------------------------------------------------------

固定 = [
    (0xD65F03C0, "ret"),
    (0xA9BF53F3, "stp x19,x20,[sp,#-16]!"),
    (0xF81F0FF5, "str x21,[sp,#-16]!"),
    (0xA9BF7BFD, "stp x29,x30,[sp,#-16]!"),
    (0x910003FD, "mov x29,sp"),
    (0xF9400BB5, "ldr x21,[x29,#16]"),
    (0xA94253B3, "ldp x19,x20,[x29,#32]"),
    (0x910003BF, "mov sp,x29"),
    (0xA8C17BFD, "ldp x29,x30,[sp],#16"),
]

模式 = [
    # ---- 栈式求值（Rt 可变）----
    (0xFFFFFFE0, 0xF81F0FE0, "str xn,[sp,#-16]!（压栈）"),
    (0xFFFFFFE0, 0xF84107E0, "ldr xn,[sp],#16（弹栈）"),
    # ---- 槽读写（偏移在 bits[21:10]，Rt 可变）----
    (0xFFC00000, 0xF9400000, "ldr x?, [x21,#off]"),
    (0xFFC00000, 0xF9000000, "str x?, [x21,#off]"),
    (0xFFC00000, 0xFD400000, "ldr d?, [x21,#off]"),
    (0xFFC00000, 0xFD000000, "str d?, [x21,#off]"),
    (0xFFE00C00, 0xF8400000, "ldur（遍历的个数）"),
    (0xFFE00C00, 0xF8600800, "ldr x?,[x20,x19,lsl#3]（取元素）"),
    # ---- 立即数 ----
    (0xFFE0001F, 0x52800000, "mov w0,#码"),
    (0xFF800000, 0xD2800000, "movz"),
    (0xFF800000, 0xF2800000, "movk"),
    # ---- 乘除（比 add/sub 更具体，必须排在它们前面）----
    (0xFFE0FC00, 0x9B007C00, "mul"),
    # msub 的 Ra 在 bits[14:10]（可变）—— 不能沿用 mul 那个 mask
    (0xFFE08000, 0x9B008000, "msub"),
    (0xFFE0FC00, 0x9B407C00, "smulh"),
    (0xFFE0FC00, 0x9AC00C00, "sdiv"),
    (0xFFE0FC1F, 0xEB80FC1F, "cmp xn, xm, asr #63"),
    (0xFFE0FC00, 0x1E602800, "fadd"),
    (0xFFE0FC00, 0x1E603800, "fsub"),
    (0xFFE0FC00, 0x1E600800, "fmul"),
    (0xFFE0FC00, 0x1E601800, "fdiv"),
    # ---- 浮点搬运（Rn 在 bits[9:5]，Rd 在 bits[4:0]）----
    (0xFFFFFC00, 0x9E670000, "fmov d,x"),
    (0xFFFFFC00, 0x9E660000, "fmov x,d"),
    (0xFFFFFC00, 0x9E620000, "scvtf"),
    (0xFFFFFC00, 0x1E614000, "fneg"),
    (0xFFE0FC1F, 0x1E602000, "fcmp d,d"),
    (0xFFFFFC1F, 0x1E602008, "fcmp d,#0.0"),
    # ---- 条件与三操作数 ----
    # cset 的 cond 在 bits[15:12]（可变）：用 0xFFFFE000 会把它当固定位
    (0xFFFF0FE0, 0x9A9F07E0, "cset"),
    (0xFFE0FFE0, 0xCB0003E0, "neg"),
    (0xFFE0FFE0, 0xEB0003E0, "negs"),
    (0xFFE0FC1F, 0xEB00001F, "cmp xn, xm"),
    (0xFFFFFC1F, 0xF100001F, "cmp xn, #0"),
    (0xFFC0001F, 0xB100001F, "cmn（问除数是不是 -1）"),
    (0xFF200000, 0xCA000000, "eor"),
    (0xFFE0FFE0, 0xAA0003E0, "mov（orr）"),
    (0xFF200000, 0x8B000000, "add"),
    (0xFF200000, 0xCB000000, "sub"),
    (0xFF200000, 0xAB000000, "adds"),
    (0xFF200000, 0xEB000000, "subs"),
    (0xFF800000, 0x91000000, "add #imm"),
    (0xFF800000, 0xD1000000, "sub #imm"),
    # ---- 控制流 ----
    (0xFF000010, 0x54000000, "b.cond"),
    (0xFC000000, 0x14000000, "b"),
    # ⚠️ cbz 与 cbnz 只差 bit24 —— mask 里必须留着它，否则两者认成同一个
    (0x7F000000, 0x34000000, "cbz"),
    (0x7F000000, 0x35000000, "cbnz"),
]


def _类(w: int):
    for 值, 名 in 固定:
        if w == 值:
            return 名
    for mask, 值, 名 in 模式:
        if (w & mask) == 值:
            return 名
    return None


# ---------------------------------------------------------------------------
# 脚本
# ---------------------------------------------------------------------------

整数循环 = """
函数 主(n)：
    令 i = 0
    令 s = 0
    当 i < n：
        s = s + i
        i = i + 1
    返回 s
打印(主(200000))
"""

乘法 = """
函数 主(n)：
    令 i = 0
    令 s = 1
    当 i < n：
        s = (s * 3) % 1000003
        i = i + 1
    返回 s
打印(主(200000))
"""

浮点 = """
函数 主(n)：
    令 i = 0
    令 s = 0.0
    当 i < n：
        如果 s < 100.0：
            s = s + i * 0.5
        否则：
            s = s / 2.0
        i = i + 1
    返回 s
打印(主(200000))
"""

遍历 = """
函数 主(数据)：
    令 s = 0
    遍历 x 在 数据：
        如果 x % 2 == 0：
            s = s + x
    返回 s
打印(主(范围(0, 200000)))
"""

中断 = """
函数 主(n)：
    令 i = 0
    令 找 = -1
    当 i < n：
        如果 i > 5000：
            找 = i
            中断
        i = i + 1
    返回 找
打印(主(200000))
"""


# ---------------------------------------------------------------------------
# 一、结构
# ---------------------------------------------------------------------------

@需要jit
def test_序言与尾声逐字正确():
    """序言 5 条 + 尾声 6 条，**逐字**比对。

    这 11 条是手写汇编的「定式」，一个字节都不该变：
    保存 callee-saved → 立帧 → 搬缓冲指针 → …… → 恢复 → 返回。
    它们错了的表现是**调用者的寄存器被踩**（返回后跳到垃圾地址），
    最难查的那种 —— 所以在字节层面就钉死。
    """
    字 = _字(_arm(整数循环))
    序言 = [0xA9BF53F3, 0xF81F0FF5, 0xA9BF7BFD, 0x910003FD, 0xAA0003F5]
    assert 字[:5] == 序言, "序言不对：" + " ".join(f"{w:08X}" for w in 字[:5])

    # 末尾 7 条 = 恢复 5 条（含**补回栈的 `add sp, sp, #32`**）+ `mov w0` + `ret`
    尾声 = [0xF9400BB5, 0xA94253B3, 0x910003BF, 0xA8C17BFD, 0x910083FF]
    assert 字[-1] == 0xD65F03C0, "最后一条必须是 ret"
    assert 字[-7:-2] == 尾声, "尾声不对：" + " ".join(f"{w:08X}" for w in 字[-7:-2])
    assert 字[-2] == 0x52800000 | (1 << 5), "最后一个出口（去优化）的返回码必须是 1"


@需要jit
def test_尾声要把栈指针补回原样():
    """🔴 **M184 在 macOS/ARM 上的段错误就是这个**，单独钉一条。

    序言压了 **48 字节**（`stp x19,x20` + `str x21` + `stp x29,x30`），
    而原来的尾声只还了 **16**：

    ```text
    mov sp, x29               ; sp = S-48（把 sp 钉回帧基 —— 去优化出口必须这么做）
    ldp x29, x30, [sp], #16   ; sp = S-32
    ret                       ; ❌ 返回时 sp 比进来时少 32 字节
    ```

    调用者一用栈就崩，退出码 **-11（SIGSEGV）**。而 `--jit-selftest`
    （不碰栈）照过、**16 条字节判据也全过** —— 因为每条指令单看都是对的，
    错的是「一共还了多少」。只有 CI 的 macos-arm64 真跑才照得出来。

    ⇒ 这条判据盯的正是那个差额：尾部必须有 `add sp, sp, #32`。
    """
    for 名, 源 in (("整数", 整数循环), ("乘法", 乘法), ("浮点", 浮点), ("遍历", 遍历)):
        字 = _字(_arm(源))
        末尾 = 字[-7:-2]
        assert 末尾[4] == 0x910083FF, (
            f"{名}：尾声最后一条必须是 `add sp, sp, #32`（序言压 48、"
            f"`mov sp,x29`+`ldp` 只还 16）—— 实际 {末尾[4]:08X}"
            f"（少了它 = 返回时 sp 少 32 字节 = 调用者栈被毁 = 段错误）")
        # 序言 3 条 pre-index 各 -16 ⇒ -48；尾声 `ldp` +16 与 `add` +32 ⇒ +48
        assert 16 * 3 == 16 + 32, "常量对不上（改栈帧布局时两边一起改）"


@需要jit
def test_三个出口的返回码是_0_2_1():
    """正常 / `中断` / 去优化 —— 三个出口按顺序排，返回码 0 / 2 / 1。

    顺序与 x86-64 后端一致（`走完` → `中断` → `去优化`），
    这样两个后端的机器码布局能对照着看。
    """
    字 = _字(_arm(中断))
    码们 = [w for w in 字 if (w & 0xFFE0001F) == 0x52800000]
    值 = [((w >> 5) & 0xFFFF) for w in 码们]
    assert 值 == [0, 2, 1], f"三个出口的返回码应当是 [0, 2, 1]，实际 {值}"


@需要jit
def test_每条指令都认得出():
    """🔴 **全量白名单**：任何一个 32 位字都必须是「我认识的指令」。

    这是这一组里最有价值的一条 —— 它不需要我事先想到某个 bug。
    后端多发一条指令、imm 位没补、位段写错，都会在这里冒出来。
    """
    未知 = []
    for 名, 源 in (("整数", 整数循环), ("乘法", 乘法), ("浮点", 浮点), ("遍历", 遍历)):
        字 = _字(_arm(源))
        for k, w in enumerate(字):
            if _类(w) is None:
                未知.append(f"{名} 第 {k} 条：{w:08X}")
    assert not 未知, "出现了不认识的指令（位段写错 / imm 没补？）：\n" + "\n".join(未知)


@需要jit
def test_所有跳转落点都在函数体内():
    """每条跳转的目标必须落在 `[0, 条数)` 里，而且既有前向也有后向。

    `imm` 没补的后果在 ARM 上比 x86 更响：`imm = 0` 就是「跳到自己」，
    `b.cond` 会变成「条件成立就原地打转」= **死循环**。
    """
    for 名, 源 in (("整数", 整数循环), ("乘法", 乘法), ("浮点", 浮点), ("遍历", 遍历)):
        字 = _字(_arm(源))
        n = len(字)
        前向 = 后向 = 0
        for k, w in enumerate(字):
            目标 = None
            if (w & 0xFC000000) == 0x14000000:  # b
                imm = w & 0x03FF_FFFF
                imm = imm - (1 << 26) if imm & (1 << 25) else imm
                目标 = k + imm
            elif (w & 0xFF000010) == 0x54000000:  # b.cond
                imm = (w >> 5) & 0x7FFFF
                imm = imm - (1 << 19) if imm & (1 << 18) else imm
                目标 = k + imm
            elif (w & 0x7F000000) in (0x34000000, 0x35000000):  # cbz / cbnz
                imm = (w >> 5) & 0x7FFFF
                imm = imm - (1 << 19) if imm & (1 << 18) else imm
                目标 = k + imm
            if 目标 is None:
                continue
            assert 0 <= 目标 < n, f"{名} 第 {k} 条跳到 {目标}（一共 {n} 条）—— 越界"
            assert 目标 != k, f"{名} 第 {k} 条跳到自己 —— imm 没补，会死循环"
            if 目标 > k:
                前向 += 1
            else:
                后向 += 1
        assert 前向 >= 1, f"{名}：没有一条前向跳（循环出口去哪儿了）"
        assert 后向 >= 1, f"{名}：没有一条后向跳（回边去哪儿了）"


# ---------------------------------------------------------------------------
# 二、每个形态发的是「该发的那几条」
# ---------------------------------------------------------------------------

def _有(字, 名):
    return [k for k, w in enumerate(字) if _类(w) == 名]


@需要jit
def test_整数加法走_adds_加bvs():
    """整数 `+` 必须用 `adds` + `b.vs`（**不能**用 `add`）—— 溢出要能去优化。"""
    字 = _字(_arm(整数循环))
    assert _有(字, "adds"), "没有 adds：整数加法没做溢出检查"
    assert _有(字, "b.cond"), "没有条件跳：溢出之后没路可走"


@需要jit
def test_乘法走三件套():
    """`s *= 3` 必须是 `smulh` + `mul` + `cmp x?, x?, asr #63` + `b.ne`。

    🔴 ARM 的 `mul` **不置 NZCV**（和 x86 的 `imul` 不一样），所以不能 `b.vs`。
    少了 `smulh` 这条，乘法溢出就**静默回绕** —— 数会错，但不报错。
    """
    字 = _字(_arm(乘法))
    assert _有(字, "smulh"), "没有 smulh：乘法溢出检查缺了"
    assert _有(字, "mul"), "没有 mul"
    assert len(_有(字, "cmp xn, xm, asr #63")) == len(_有(字, "smulh")), (
        "每个 smulh 都要跟一条 cmp-asr63（否则比什么）")


@需要jit
def test_除法走_sdiv_与向下取整修正():
    """`//` / `%` = `sdiv` + `msub`（求余）+ 异号修正，外加除零与 `-1` 两条保护。"""
    字 = _字(_arm(乘法))
    assert _有(字, "sdiv"), "没有 sdiv"
    assert _有(字, "msub"), "没有 msub：余数是拿被除数减商×除数算的"
    assert _有(字, "cmn（问除数是不是 -1）"), "没有 cmn：`i64::MIN / -1` 会被静默回绕"
    assert _有(字, "eor"), "没有 eor：异号判断（余 ^ 除数）缺了"
    # 走的是 `negs`（要顺带查 `i64::MIN / -1` 溢出），不是 `neg`
    assert _有(字, "negs"), "没有 negs：没走「除数 == -1」那条路"
    # 除零检查在 `cmn` **之前**：`cbnz 除数, 继续` / `b 去优化` / `cmn 除数, #1`
    # （中间的 `b` 是「除数为零 ⇒ 去优化」那条路，所以往回最多看 3 条）
    for k in _有(字, "cmn（问除数是不是 -1）"):
        前面 = [_类(字[j]) for j in range(max(0, k - 3), k)]
        assert "cbnz" in 前面, f"第 {k} 条 cmn 前面没有 cbnz（除零没查）：{前面}"


@需要jit
def test_遍历走_ldur_与索引寻址():
    """`遍历` 取个数用 `ldur [x20,#-8]`、取元素用 `ldr [x20,x19,lsl#3]`。"""
    字 = _字(_arm(遍历))
    assert _有(字, "ldur（遍历的个数）"), "没有 ldur：个数没读"
    assert _有(字, "ldr x?,[x20,x19,lsl#3]（取元素）"), "没有索引寻址：元素没读"
    # 序言里 `add x20, x1, #8`：把元素基址挪到「跳过个数」的位置
    assert 0x91002034 in 字, "没有 `add x20, x1, #8`（元素基址没设）"


@需要jit
def test_浮点发的是_ARM_那一套():
    """`fadd` / `fmul` / `fdiv` / `fcmp` / `fmov` 各自出现，且**没有整数 add** 混进浮点域。"""
    字 = _字(_arm(浮点))
    for 名 in ("fadd", "fmul", "fdiv", "fcmp d,d", "fcmp d,#0.0"):
        assert _有(字, 名), f"没有 {名}"
    # `/` 的除零检查：fcmp x?, #0.0 后面紧跟着 b.eq
    for k in _有(字, "fcmp d,#0.0"):
        assert _类(字[k + 1]) == "b.cond", f"第 {k} 条 fcmp #0 后面不是条件跳（除零没查）"


@需要jit
def test_浮点小于用交换操作数():
    """🔴 `s < 100.0` 必须发成 `fcmp 100.0, s` + `cset gt`（**不是** `fcmp s,100.0` + `lt`）。

    ARM 的 `fcmp` 在无序（有 NaN）时置 `N=0,Z=0,C=1,V=1`，于是 `LT` 与 `LE`
    会给出**真** —— 「NaN < 100」变成真，而正确语义是假。交换操作数改成 `GT`/`GE`
    就对了（LLVM 在 AArch64 上也是这么干的）。

    怎么从字节上看出来：`cset` 的 cond 字段（bits[15:12]）里存的是
    `invert(cond)`，所以 `gt`(1100) → 存 `0011`；`lt`(1011) → 存 `1010`。
    这条断言盯的是「源程序里写的是 `<`，但发出来的不是 `lt`」。
    """
    字 = _字(_arm(浮点))
    cs = [w for w in 字 if (w & 0xFFFF0FE0) == 0x9A9F07E0]
    assert cs, "没有 cset"
    条件们 = sorted(((w >> 12) & 0xF) for w in cs)
    # `cset` 里存的是 **invert(cond)**：gt(0b1100) → 0b1101 = 13；lt(0b1011) → 0b1010 = 10
    assert 10 not in 条件们, (
        "出现了 `lt`（cset 里存的 invert 是 10）—— 让 `NaN < x` 变成真了；"
        "`<` 必须交换操作数用 gt/ge")
    assert 13 in 条件们, (
        f"没有 gt（invert 是 13）—— `s < 100.0` 应当发成 `fcmp 100.0, s` + cset gt；"
        f"实际条件们={条件们}")


@需要jit
def test_混合提升走_scvtf():
    """`s + i * 0.5` 里 `i` 是整数槽 ⇒ 必须先 `scvtf` 转成浮点。"""
    字 = _字(_arm(浮点))
    assert _有(字, "scvtf"), "没有 scvtf：整数没转浮点，或者整个循环没编"


@需要jit
def test_乘法溢出检查在算存形态里也有():
    """压扁成 `算存` 的乘法（`s = s * 常数`）同样要 `smulh`。

    `算存` 是一条「不碰栈」的融合形态，跟 `二元` 是两条不同的发射路径 ——
    只在 `二元` 里加 `smulh` 会漏掉最常见的那种写法。
    """
    字 = _字(_arm(乘法))
    assert len(_有(字, "smulh")) >= 1
    for k in _有(字, "smulh"):
        # smulh 后面第二条应当是 mul，第三条是 cmp-asr63
        assert _类(字[k + 1]) == "mul", f"第 {k} 条 smulh 后面不是 mul"
        assert _类(字[k + 2]) == "cmp xn, xm, asr #63", f"第 {k} 条 smulh 后面没跟 cmp-asr63"


# ---------------------------------------------------------------------------
# 三、已知限制（写下来，别让人以为是漏了）
# ---------------------------------------------------------------------------

@需要jit
def test_末尾的继续_进不了JIT_如实记():
    """循环体最后一句是 `继续` 时，JIT **看不见**这个循环。

    原因：那种写法让编译器把末尾那条回跳 `JUMP` 优化掉了，只剩下一条
    `CONTINUE_LOOP`；而热点信号只认 `JUMP`（M180 定的：回边是唯一的免费信号）。
    所以这段代码照常正确，只是**一直走解释器**。

    📌 这不是 M184 引入的（M182 起就是这样），这一站也没修 —— 修它要动
    「回边」的定义（把 `CONTINUE_LOOP` 也算回边），那是另一次改动。
    这里把现状**钉住**：哪天有人修了，这条会红，正好提醒他改文档。
    """
    末尾继续 = """
函数 主(n)：
    令 i = 0
    令 s = 0
    当 i < n：
        s = s + i
        i = i + 1
        继续
    返回 s
打印(主(200000))
"""
    assert _arm(末尾继续, 应当编=False) is None, (
        "末尾 `继续` 的循环现在能编了 —— 那是好事，但要把 CHANGELOG 与路线图里"
        "「末尾 继续 进不了 JIT」这条限制改掉")

    中间继续 = """
函数 主(n)：
    令 i = 0
    令 s = 0
    当 i < n：
        i = i + 1
        s = s + i
        如果 i % 3 == 0：
            继续
        s = s + 1
    返回 s
打印(主(200000))
"""
    assert _arm(中间继续) is not None, "`继续` 在中间时应当能编（M182 的判据覆盖过）"


# ---------------------------------------------------------------------------
# 四、这一站不许伤到 x86-64（两个后端共用一个前端）
# ---------------------------------------------------------------------------

@需要jit
def test_开与关jit结果一致_并确认这次真的编了():
    """同一段源码，开 / 关 JIT 的输出逐字节相同，且调试行说「编好了」。

    ⚠️ 这一条在 **aarch64 上也跑**（CI 的 macos-arm64）—— 那里跑的才是
    **真的 ARM 机器码**，是这一站唯一的一锤定音。
    """
    for 名, 源 in (("整数", 整数循环), ("乘法", 乘法), ("浮点", 浮点), ("遍历", 遍历)):
        开 = _跑(源, {"JISHI_JIT": "调试"})
        关 = _跑(源, {"JISHI_JIT": "0"})
        assert 开.returncode == 0, f"{名}：开 JIT 退出码 {开.returncode}\n{开.stderr[-800:]}"
        assert 关.returncode == 0, f"{名}：关 JIT 退出码 {关.returncode}"
        assert 开.stdout == 关.stdout, f"{名}：JIT 与解释器输出不一致\n[{开.stdout}] vs [{关.stdout}]"
        assert "编好了" in 开.stderr, f"{名}：这次没编（判据会假绿）\n{开.stderr[-600:]}"


def test_zzz_诊断():
    r = _跑(整数循环, {"JISHI_JIT_DUMP_ARM": "1", "JISHI_JIT": "调试"})
    print("\n__DIAG__ rc=", repr(r.returncode), "out=", repr(r.stdout[:30]),
          "err=", repr(r.stderr[:100]), "file=", r.调试文件, "env=", r.调试环境)


# ---------------------------------------------------------------------------
# 五、端到端自检：写一条「返回 42」的机器码，真跳进去（路线图 §M184 的开工第一条）
# ---------------------------------------------------------------------------

@需要jit
def test_最小闭环_返回42():
    """🔴 这是**唯一真执行机器码**的一条，也是 Apple Silicon 上最先会响的一条。

    ARM64 上 `MAP_JIT` / `pthread_jit_write_protect_np` / 刷 I-cache 三件事
    少一件，失败方式都是**启动即死或跑出垃圾**（没有异常、没有栈）——
    有了这条，就能把「这台机器的 JIT 环境不通」和「编码器写错了」分开。

    在 x86-64 上它验的是同一条链的 x86 版本（`mov eax,42; ret`）。
    """
    r = subprocess.run([str(EXE), "--jit-selftest"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, (
        f"JIT 自检失败（退出码 {r.returncode}）：{r.stderr.strip()}\n"
        f"—— 在这台机器上，JIT 环境本身就是不通的，别去查编码")
    assert r.stdout.strip() == "42", f"自检返回 {r.stdout.strip()!r}，应当是 42"
