# -*- coding: utf-8 -*-
"""R6.1 · Rust VM `遍历` 的守门测试。

R6.0 量出来：Rust VM 的 `遍历` 是 **O(n²)**（规模翻倍、耗时 ×4）。
根因是迭代器的实现 —— 「列表 + 每次 `remove(0)`」：

```rust
Val::List(l) => { ... Ok(Some(l.remove(0))) }     // Vec::remove(0) 要整体左移
```

对照另外两处：Python VM 用 `next(stack[-1])`、Node 用 `makeIterator(obj).next()`
—— **都是真迭代器**，只有 Rust 这份是「列表 + 从头弹」。

修法：快照**反转**后从**尾部** `pop()`（顺序不变、每步 O(1)）。
⚠️ 「反转」这件事让**顺序正确性**成了这个修复的**头号风险** —— 所以本文件
两个都盯：**顺序逐字节一致** + **不许退回 O(n²)**。
"""

import io
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi.compiler import compile_to_json, compile_source         # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree               # noqa: E402
from oracle.jishi.vm import VM                                            # noqa: E402


def _exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


EXE = _exe()
need_rust_vm = pytest.mark.skipif(
    not EXE.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）"
           "—— 与其假装通过，不如明确跳过")

#: 临时字节码路径（带进程号，避免并发互相覆盖）
_BC = ROOT / "build" / f"_m64_iter_{os.getpid()}.json"


def _tree(src: str) -> str:
    buf = io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO("")           # 钉死：`输入()` 会去读终端
    try:
        with redirect_stdout(buf):
            run_tree(src, "遍历.jsh")
        return buf.getvalue()
    except BaseException as e:                                     # noqa: BLE001
        return buf.getvalue() + "「报错」" + type(e).__name__ + ":" + str(e)
    finally:
        sys.stdin = old


def _vm(src: str) -> str:
    buf = io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        with redirect_stdout(buf):
            VM(compile_source(src, "遍历.jsh"), "遍历.jsh").run()
        return buf.getvalue()
    except BaseException as e:                                     # noqa: BLE001
        return buf.getvalue() + "「报错」" + type(e).__name__ + ":" + str(e)
    finally:
        sys.stdin = old


def _rust(src: str, timeout: float = 120.0):
    """用 Rust 前端编字节码 → 交给 Rust VM 跑。返回 `(stdout, 秒)`。"""
    _BC.parent.mkdir(parents=True, exist_ok=True)
    _BC.write_text(compile_to_json(src, "遍历.jsh"), encoding="utf-8")
    t0 = time.perf_counter()
    r = subprocess.run([str(EXE), "--load-bytecode", str(_BC)], capture_output=True,
                       input=b"", timeout=timeout)
    dt = time.perf_counter() - t0
    return r.stdout.decode("utf-8", errors="replace"), dt


# ---------------------------------------------------------------------------
# 一、顺序正确性 —— 反转快照的头号风险
# ---------------------------------------------------------------------------

#: `(说明, 源码, 期望输出)` —— 覆盖**每一种可遍历的容器** + 控制流。
_ORDER_CASES = [
    ("列表", "遍历 项 在 [1, 2, 3]：\n    打印(项)\n", "1\n2\n3\n"),
    ("文本按码点", "遍历 字 在 \"abc\"：\n    打印(字)\n", "a\nb\nc\n"),
    ("文本含 emoji", "遍历 字 在 \"a😀b\"：\n    打印(字)\n", "a\n😀\nb\n"),
    ("集合按插入序", "令 集 = {1, 2, 3}\n遍历 项 在 集：\n    打印(项)\n",
     "1\n2\n3\n"),
    ("字典遍历键", "令 字 = {\"甲\": 1, \"乙\": 2, \"丙\": 3}\n"
     "遍历 键 在 字：\n    打印(键)\n", "甲\n乙\n丙\n"),
    ("空列表（一次都不进）",
     "遍历 项 在 []：\n    打印(\"不该出现\")\n打印(\"完了\")\n", "完了\n"),
    ("中断", "遍历 项 在 [1, 2, 3, 4]：\n    如果 项 == 3：\n        中断\n"
     "    打印(项)\n打印(\"出循环\")\n", "1\n2\n出循环\n"),
    ("继续", "遍历 项 在 [1, 2, 3, 4]：\n    如果 项 % 2 == 0：\n        继续\n"
     "    打印(项)\n", "1\n3\n"),
    ("嵌套两层", "遍历 甲 在 [1, 2]：\n    遍历 乙 在 [\"x\", \"y\"]：\n"
     "        打印(甲, 乙)\n", "1 x\n1 y\n2 x\n2 y\n"),
    ("遍历里改**别的**列表不影响本次遍历",
     "令 甲 = [1, 2, 3]\n令 乙 = []\n遍历 项 在 甲：\n    乙.追加(项 * 2)\n    打印(项)\n打印(乙)\n",
     "1\n2\n3\n[2, 4, 6]\n"),
    ("函数里返回的列表再遍历",
     "函数 造()：\n    返回 [3, 1, 2]\n遍历 项 在 造()：\n    打印(项)\n",
     "3\n1\n2\n"),
    ("遍历 range 的结果", "遍历 项 在 范围(3)：\n    打印(项)\n", "0\n1\n2\n"),
]


@need_rust_vm
@pytest.mark.parametrize("case", _ORDER_CASES, ids=[c[0] for c in _ORDER_CASES])
def test_遍历顺序三处一致(case):
    """列表 / 文本 / 集合 / 字典 / 控制流：树遍历 = Python VM = **Rust VM**。

    ⚠️ Rust VM 的输出**只比 stdout**：它的报错渲染少源码行/`^`/调用链
    （已登记的宿主设计，见 `tools/check_engines.py`）。本节的用例都不报错。
    """
    name, src, expect = case
    tree, vm = _tree(src), _vm(src)
    rust, _ = _rust(src)
    assert tree == vm == rust, (
        f"{name}：三处不一致\n  树遍历 : {tree!r}\n  Python VM: {vm!r}\n"
        f"  Rust VM  : {rust!r}")
    assert tree == expect, f"{name}：输出不对 {tree!r} ≠ {expect!r}"


@need_rust_vm
def test_遍历不消耗原容器():
    """快照语义：`遍历` 之后原列表**一个元素都不能少**。

    这一条是迭代器实现的**红线**（Rust 侧那条注释专门写过）：以前列表是值类型，
    「不消耗原列表」是白捡的；换成共享容器后必须显式克隆，否则
    `遍历 x 在 a` 会把 `a` 搬空。
    """
    src = ("令 列 = [1, 2, 3]\n"
           "遍历 项 在 列：\n"
           "    打印(项)\n"
           "打印(列)\n")
    tree, vm = _tree(src), _vm(src)
    rust, _ = _rust(src)
    assert tree == vm == rust
    assert tree.endswith("[1, 2, 3]\n"), tree


# ---------------------------------------------------------------------------
# ⚠️ 一个**还没定论**的分岔（写在这里，别让它被忘掉）
# ---------------------------------------------------------------------------
#
# 「**遍历的时候往被遍历的那个列表里加东西**」—— 各执行器不一样：
#
#     函数 f()：
#         令 列 = [1, 2, 3]
#         遍历 项 在 列：
#             列.追加(项)     ← 边遍历边往原列表加
#             打印(项)
#
# | 执行器 | 行为 |
# |---|---|
# | 树遍历 / Python VM / C VM / **Node** | **活迭代器** → 一直循环下去（与 Python 一致） |
# | **Rust VM** | **快照** → 打印 1 2 3 就结束 |
#
# 四个是「活」、一个是「快照」—— **Rust VM 是那个少数**。
# 这是**预存**分岔（Rust 侧 `make_iterator` 一直在克隆快照），
# 与 R6.1 修 O(n²) 无关；R6.1 只是**保持**了这个行为没动它。
#
# ⚠️ **为什么不写成测试**：写成测试就等于**让三个执行器死循环**（测试会挂住）。
# 「三处死循环、Rust 跑完」这件事本身说明该由作者定「要哪一种语义」：
#   * 要「活」：Rust 侧改成**下标游标**（照 Node 那份，3 行），O(1) 且与四个一致；
#   * 要「快照」：另外四处都要改，而且要说明「为什么与 Python 不同」。
# 定下来之前，这个形状**不要进语料**（`tests/cases/`）—— 夹具会跑它。


# ---------------------------------------------------------------------------
# 二、O(n²) 不许回来
# ---------------------------------------------------------------------------

def _net_seconds(n: int, *, tries: int = 3) -> float:
    """`遍历 n 次`（空循环体）的**净耗时**：扣掉「空程序」的进程启动成本。

    ⚠️ **零点必须当场量**（R6.1 踩过）：进程启动成本会漂 —— 实测同一台机器上
    从 8 ms 变成过 85 ms（刚编出来的 exe 被实时扫描）。拿旧零点减新数据，
    会把「启动变慢」读成「VM 变慢」。
    """
    base = float("inf")
    for _ in range(tries):
        _, dt = _rust("令 空转 = 1\n")
        base = min(base, dt)
    best = float("inf")
    for _ in range(tries):
        _, dt = _rust(f"令 计数 = 0\n遍历 i 在 范围({n})：\n    计数 = 1\n打印(计数)\n")
        best = min(best, dt)
    return max(best - base, 0.0)


def _net_pair(n_small: int, n_large: int, *, tries: int = 5) -> "tuple[float, float]":
    """**交错**量出两个规模的净耗时。

    ⚠️ 为什么必须交错（2026-10-10 在 CI 上栽过）：原来先把 base 量 3 次、
    再把 small 量 3 次、最后把 large 量 3 次 —— 中途来一阵负载（共享 runner
    很常见），**后量到的那档被整体抬高**，于是「比值」量到的是**负载**而不是
    **量级**：同一份代码本机比值 ~4，CI 上读到过 **20.9**（差的正是这一档）。
    交错之后两档看到的是同一段负载；每档取 min（最不受打扰的那次）。
    """
    base = small = large = float("inf")
    exprs = {
        "base": "令 空转 = 1\n",
        "small": f"令 计数 = 0\n遍历 i 在 范围({n_small})：\n    计数 = 1\n打印(计数)\n",
        "large": f"令 计数 = 0\n遍历 i 在 范围({n_large})：\n    计数 = 1\n打印(计数)\n",
    }
    for _ in range(tries):
        base = min(base, _rust(exprs["base"])[1])
        small = min(small, _rust(exprs["small"])[1])
        large = min(large, _rust(exprs["large"])[1])
    return max(small - base, 0.0), max(large - base, 0.0)


@need_rust_vm
def test_遍历不是平方的():
    """**翻倍测试**：元素数 ×4，耗时该是 ×4 上下；平方的话会是 ×16。

    阈值放得很宽（< 9 倍）—— 性能断言在 CI 上容易被负载带歪，
    所以这里只钉「**量级**」这件事：线性 4× 与平方 16× 之间差得很远，
    9 这个界两边都够不着。
    """
    # 规模取大一点：净耗时太小的话噪声占主导（000 那种护栏会直接跳过）
    small, large = _net_pair(20000, 80000)
    # n 太小的时候噪声占主导（两者都可能接近 0），那种情况不判定
    if small < 0.002 or large < 0.002:
        pytest.skip(f"耗时太短、噪声占主导（{small * 1000:.1f} / "
                    f"{large * 1000:.1f} ms），本次不判定")
    ratio = large / small
    assert ratio < 9, (
        f"`遍历` 像是退回了 O(n²)：元素数 ×4，耗时 ×{ratio:.1f}"
        f"（20000 → {small * 1000:.1f} ms，80000 → {large * 1000:.1f} ms）。"
        f"检查 `iter_next` 是不是又用上了 `remove(0)`（那是 O(n) 搬移）")


@need_rust_vm
@pytest.mark.计时
def test_两万次遍历在一秒内():
    """绝对阈值兜底：n=20000 净耗时必须 **< 1 秒**。

    修前这里是 **~2.5 秒**、修后 ~5 ms —— 两边差 500 倍，所以 1 秒这个界
    既不会被负载带歪，也拦得住「退回平方」。
    """
    dt = _net_seconds(20000, tries=2)
    assert dt < 1.0, (
        f"n=20000 的 `遍历` 用了 {dt:.2f} 秒（修后应在毫秒级，修前是 2.5 秒）"
        f"—— 大概率是迭代器又变成 O(n²) 了")
