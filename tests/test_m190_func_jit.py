# -*- coding: utf-8 -*-
"""M190 · 函数级 JIT（跨帧）：`斐波` 这类自递归函数整体编成机器码。

## 这一站为什么不是「循环内联」

`斐波` 一个循环都没有，而 M180–M184 的 JIT 只认「抬头 → 回跳」那一段 ——
给它加循环内联等于给没点火的发动机加油。所以改成**函数级**编译（D114）。

## 判据分三层

1. **数值**：`斐波(0..20)` 与关掉 JIT 逐值相同；`斐波(24)` 不崩；
2. **语义**：浮点参数、函数被重新赋值、`数(1500)` 这三种情况，**行为与关掉 JIT 一模一样**
   （包括最后那句「递归层数太深了」的中文报错，而不是 SIGSEGV）；
3. **性能**：`斐波` 从 0.5x 级提到 **≥1.5x**（这条打 `计时`，进串行节）。
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

斐波 = (
    "函数 斐波(n)：\n"
    "    如果 n < 2：\n"
    "        返回 n\n"
    "    返回 斐波(n - 1) + 斐波(n - 2)\n"
)


def _跑(源: str, tmp_path: Path, 名="脚本.jsh", 开jit=True, 报错也收=True) -> tuple:
    脚本 = tmp_path / 名
    脚本.write_text(源, encoding="utf-8", newline="\n")
    环境 = dict(__import__("os").environ)
    if not 开jit:
        环境["JISHI_JIT"] = "0"
    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path), env=环境)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    return r.returncode, 出


@pytest.fixture(scope="module", autouse=True)
def _要有宿主():
    if not rust_exe_path().is_file():
        pytest.skip("没有 Rust 宿主（cargo build --release）")


def test_斐波逐值一致(tmp_path):
    """0–20 全跑一遍，两种模式必须一字不差。"""
    for n in (0, 1, 2, 3, 5, 8, 10, 13, 16, 18, 20):
        源 = 斐波 + f"打印(斐波({n}))\n"
        码1, 出1 = _跑(源, tmp_path, f"a{n}.jsh", 开jit=False)
        码2, 出2 = _跑(源, tmp_path, f"b{n}.jsh", 开jit=True)
        assert (码1, 出1) == (码2, 出2), f"n={n} 两种模式结果不同：{出1!r} vs {出2!r}"


def test_深一点的递归也不崩(tmp_path):
    """`斐波(24)`：机器码用自己的帧，曾经在这附近撞过栈（见 D114 的坑）。"""
    码, 出 = _跑(斐波 + "打印(斐波(24))\n", tmp_path)
    assert 码 == 0, 出[-800:]
    assert 出.strip().endswith("46368"), 出[-200:]


def test_浮点参数走解释器(tmp_path):
    """机器码只认 `i64`；喂 `2.5` 必须回到解释器，结果要对。"""
    源 = (
        "函数 累加(n)：\n"
        "    如果 n <= 0：\n"
        "        返回 0\n"
        "    返回 n + 累加(n - 1)\n"
        "打印(累加(100))\n"
        "打印(累加(2.5))\n"
    )
    _, 出 = _跑(源, tmp_path)
    assert "5050" in 出 and "4.5" in 出, 出[-400:]


def test_函数被重新赋值后仍然正确(tmp_path):
    """基石里函数是值：`斐波 = 别的` 之后，机器码里那个「调自己」的落点必须重新核对。"""
    源 = (
        "函数 别的(n)：\n"
        "    返回 42\n\n"
        + 斐波
        + '打印(斐波(12))\n'
        "斐波 = 别的\n"
        "打印(斐波(12))\n"
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-400:]
    assert "144" in 出 and "42" in 出, 出[-400:]


def test_递归上限与解释器同一句话(tmp_path):
    """`数(1500)` 必须报「递归层数太深了」，**不能**是 SIGSEGV。

    机器码的递归不建解释器帧，所以它自己数深度（与 `MAX_FRAMES` 同一个上限）；
    另外「已经很深了就别进机器码」这条闸也在这儿 —— 解释器一帧很大，
    1000 层时原生栈本来就用得差不多了。
    """
    源 = (
        "函数 数(n)：\n"
        "    如果 n <= 0：\n"
        "        返回 0\n"
        "    返回 1 + 数(n - 1)\n"
        "打印(数(1500))\n"
    )
    码1, 出1 = _跑(源, tmp_path, "j1.jsh", 开jit=False)
    码2, 出2 = _跑(源, tmp_path, "j2.jsh", 开jit=True)
    assert "递归层数太深" in 出1, 出1[-300:]
    assert "递归层数太深" in 出2, 出2[-300:]
    assert 码1 == 码2, f"退码不同：{码1} vs {码2}"


@pytest.mark.skipif(
    __import__("platform").machine().lower() not in ("x86_64", "amd64", "arm64", "aarch64"),
    reason="函数级 JIT 只有 x86-64 与 ARM64 两个后端（别的架构上「编不了 ⇒ 走解释器」）")
def test_真的编上了(tmp_path):
    """`JISHI_JIT_DEBUG` 里要出现「编好了」——否则上面那些绿可能只是「从来没编过」。"""
    import os
    脚本 = tmp_path / "编.jsh"
    脚本.write_text(斐波 + "打印(斐波(16))\n", encoding="utf-8", newline="\n")
    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=120, env={**os.environ, "JISHI_JIT_DEBUG": "1"})
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert "编好了" in 出, 出[-600:]


@pytest.mark.计时
def test_递归比解释器快一倍以上(tmp_path):
    """数字**当场量**：同一份脚本、同一台机器，开 / 关 JIT 各跑 3 次取最小。

    门槛 1.5x：0.3.3 时这个是 0.53x（比 CPython 慢一倍），函数级 JIT 之后 2x 上下。
    """
    import time
    源 = 斐波 + "打印(斐波(22))\n"
    脚本 = tmp_path / "快.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")

    def 量(开: bool) -> float:
        次 = []
        for _ in range(3):
            t = time.perf_counter()
            r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                               timeout=180,
                               env={} if 开 else {"JISHI_JIT": "0", "PATH": ""})
            assert r.returncode == 0
            次.append(time.perf_counter() - t)
        return min(次)

    关 = 量(False)
    开 = 量(True)
    assert 开 < 关 / 1.5, f"开 JIT {开*1000:.0f} ms / 关 {关*1000:.0f} ms —— 没到 1.5x"


# ---------------------------------------------------------------------------
# ARM64 后端（M190 的第二刀）
# ---------------------------------------------------------------------------
#
# 🔴 开发机是 x86-64，**跑不了 ARM 码**。所以这一层只能「让它在 x86 上编一遍、
# 把字节打出来」，检查结构对不对（与 M184 循环 JIT 的 `[jit-arm]` 同一套办法）。
# **真正的执行**由 CI 的 macos-arm64 覆盖 —— 上面那七条数值/语义判据在那儿
# 跑的就是 ARM 机器码。这里能证的是「发对了指令、跳转落点补对了」，
# 证不了「跑起来对不对」，这两件事不能互相替代。
#
# 在 ARM 上这些判据没有探针可拿（那时后端就是真后端），所以那边 skip。

import re                                                        # noqa: E402

机器 = __import__("platform").machine().lower()

只在x86 = pytest.mark.skipif(
    机器 in ("arm64", "aarch64"),
    reason="ARM 探针只在 x86-64 上出现；ARM 上由真跑覆盖（更强）")


def _arm字节(tmp_path, 源=None, 名="arm.jsh") -> bytes:
    """`JISHI_JIT_DUMP_ARM` 让 x86-64 也把函数级 ARM 码编一遍并打出来。"""
    import os
    脚本 = tmp_path / 名
    脚本.write_text(源 or (斐波 + "打印(斐波(16))\n"), encoding="utf-8", newline="\n")
    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path),
                       env={**os.environ, "JISHI_JIT_DUMP_ARM": "1"})
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    行们 = [行 for 行 in 出.splitlines() if 行.startswith("[jit-arm-func]")]
    assert 行们, f"没有 ARM 探针输出（函数级后端编不出来？）：\n{出[-800:]}"
    m = re.match(r"\[jit-arm-func\] (\d+) 字节：(.+)$", 行们[-1])
    assert m, f"探针输出格式不对：{行们[-1][:120]}"
    十六 = m.group(2).split()
    assert int(m.group(1)) == len(十六), f"字节数 {m.group(1)} 与列表长度 {len(十六)} 对不上"
    码 = bytes(int(x, 16) for x in 十六)
    assert len(码) % 4 == 0, f"ARM64 指令定长 4 字节，长度却是 {len(码)}"
    return 码


def _字(码: bytes) -> list:
    return [int.from_bytes(码[i:i + 4], "little") for i in range(0, len(码), 4)]


@只在x86
def test_arm后端也编得出来(tmp_path):
    """这一刀之前 ARM 上是「编不了 ⇒ 永远走解释器」，等于没加速。"""
    码 = _arm字节(tmp_path)
    assert len(码) > 100, f"ARM 码只有 {len(码)} 字节，不像一整个函数"


@只在x86
def test_arm骨架首尾对(tmp_path):
    """序言压 `x29/x30`、尾声必须 `mov sp,x29` → `ldp` → `ret`。

    🔴 M184 在 macOS 上段错误就是尾声没把 `sp` 还完 —— 这里没有那个问题
    （只压了 16 字节，`mov sp,x29` 一次抹平），但**顺序**仍要钉住：
    `mov sp,x29` 必须在 `ldp` 之前，`ret` 必须在最后。
    """
    字 = _字(_arm字节(tmp_path))
    assert 字[0] == 0xA9BF7BFD, f"第一条不是 stp x29,x30,[sp,#-16]!：{字[0]:08X}"
    assert 字[1] == 0x910003FD, f"第二条不是 mov x29,sp：{字[1]:08X}"
    assert 字[-1] == 0xD65F03C0, f"最后一条不是 ret：{字[-1]:08X}"
    assert 0xA8C17BFD in 字, "没有 ldp x29,x30,[sp],#16"
    # 每一个 ret 之前都必须是 ldp（两个出口：正常 / 去优化）
    for i, w in enumerate(字):
        if w == 0xD65F03C0 and i > 0:
            assert 字[i - 1] == 0xA8C17BFD, f"第 {i} 条 ret 前面不是 ldp（sp 没还完）"
    # `mov sp, x29` 至少出现两次（两个出口），且都在 ldp 前面那条之前
    assert 字.count(0x910003BF) == 2, f"mov sp,x29 出现 {字.count(0x910003BF)} 次，应为 2"


@只在x86
def test_arm递归打回函数入口(tmp_path):
    """`bl` 的目标必须是 **offset 0**（这个函数自己的起点），不能是别处。

    🔴 这是整站的命门：`bl` 打偏了不会报错，只会跑一段别的东西。
    """
    字 = _字(_arm字节(tmp_path))
    回跳 = []
    for i, w in enumerate(字):
        if (w & 0xFC000000) == 0x94000000:                   # bl
            imm26 = w & 0x03FF_FFFF
            if imm26 & 0x0200_0000:                          # 符号扩展
                imm26 -= 1 << 26
            回跳.append((i, i + imm26))
    assert 回跳, "一条 bl 都没有 —— 递归调用没编进去"
    for 位, 到 in 回跳:
        assert 到 == 0, f"第 {位} 条的 bl 跳到指令 {到}，不是函数入口 0"
    assert len(回跳) == 2, f"`斐波` 里有两次递归调用，编出来 {len(回跳)} 条 bl"


@只在x86
def test_arm带着状态指针与深度上限(tmp_path):
    """状态指针进帧、进一层数到 1000、溢出与超深都退去优化。

    这三件少了任何一件，语义就和解释器不一样了：
    没有状态指针 ⇒ 去优化信号传不回来（内层溢出、外层当正常值用）；
    没有深度上限 ⇒ `数(1500)` 在 JIT 里跑通、在解释器里报错（**改了语义**）；
    没有 `b.vs` ⇒ 整数溢出静默变成错误的数。
    """
    字 = _字(_arm字节(tmp_path))
    # stur x3, [x29, #-80]：状态指针落帧（帧布局 = 槽1 + 栈8 + 指针1 = 80）
    assert 0xF81B03A3 in 字, "没有把 x3（状态指针）存进帧里"
    # cmp x10, #1000 + b.ge
    assert 0xF10FA15F in 字, "没有 cmp x10, #1000（递归上限）"
    assert any((w & 0xFF00001F) == 0x5400000A for w in 字), "没有 b.ge（超深就去优化）"
    # b.vs：溢出跳去优化
    assert any((w & 0xFF00001F) == 0x54000006 for w in 字), "没有 b.vs（溢出没检查）"


@只在x86
def test_arm乘法要另查溢出(tmp_path):
    """`mul` 在 ARM64 上**不置 NZCV** ⇒ 必须用 `smulh` 与「结果的符号扩展」比。

    ⚠️ 这条不能拿 `斐波` 验：它只有加减。用一个带乘法的自递归函数
    （循环里反复调，好让它过热度阈值）。
    """
    源 = (
        "函数 阶乘(n)：\n"
        "    如果 n <= 1：\n"
        "        返回 1\n"
        "    返回 n * 阶乘(n - 1)\n"
        "令 i = 0\n"
        "令 s = 0\n"
        "当 i < 250：\n"
        "    s = s + 阶乘(6)\n"
        "    i = i + 1\n"
        "打印(s)\n"
    )
    字 = _字(_arm字节(tmp_path, 源, "mul.jsh"))
    assert any((w & 0xFFE0FC00) == 0x9B407C00 for w in 字), "没有 smulh（乘法溢出没查）"
    # 乘完还要跟一条「比结果符号扩展」
    assert any((w & 0xFFE0FC00) == 0xEB80FC00 for w in 字), "没有 cmp …, …, asr #63"


@只在x86
def test_arm跳转落点全在范围内(tmp_path):
    """所有 `b` / `b.cond` / `cbz` 的落点必须落在 [0, 指令数) 里。

    🔴 落点算错在 ARM 上比 x86 更响：`imm = 0` 是「跳到**自己**」，
    `b.cond` 就是原地打转的死循环 —— 而且**不报错**，只是卡住。
    """
    字 = _字(_arm字节(tmp_path))
    总 = len(字)
    条数 = 0
    for i, w in enumerate(字):
        op6 = w & 0xFC000000
        if op6 == 0x14000000:                                # b
            imm = w & 0x03FF_FFFF
            位 = 26
        elif (w & 0xFF000010) == 0x54000000:                 # b.cond
            imm = (w >> 5) & 0x7FFFF
            位 = 19
        elif (w & 0x7E000000) == 0x34000000:                 # cbz / cbnz
            imm = (w >> 5) & 0x7FFFF
            位 = 19
        else:
            continue
        if imm & (1 << (位 - 1)):
            imm -= 1 << 位
        assert 0 <= i + imm < 总, f"第 {i} 条跳转落到指令 {i + imm}（共 {总} 条）"
        条数 += 1
    assert 条数 >= 6, f"只数到 {条数} 条跳转，不像一个有分支与递归的函数"
