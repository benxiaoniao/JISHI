# -*- coding: utf-8 -*-
"""基石语言三执行器性能基准。

场景全部用基石源码写就，同一份代码分别交给：
  1. 树遍历解释器（M0，jishi/interpreter.py）
  2. Python 字节码虚拟机（M4a，jishi/vm.py）
  3. C 字节码虚拟机（M4b，cvm/bin/jsvm.dll）

用法：
    python tools/benchmark.py            # 默认时长（每场景约 1 秒）
    python tools/benchmark.py --quick    # 快速摸底（每场景约 0.2 秒）

输出：各场景的耗时与相对树遍历的倍数。耗时用「固定重复次数、
取最优一轮」的方式测，可复现（不受机器瞬时负载抖动影响）。
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi.interpreter import run_source as run_tree          # noqa: E402
from jishi import vm as vm_mod                                # noqa: E402

#: 各场景的基石源码与建议重复次数（保证单轮耗时可测又不至于太久）
SCENARIOS = {
    "整数循环累加": (
        "令 总和 = 0\n"
        "遍历 i 在 范围(20000)：\n"
        "    总和 = 总和 + i\n"
        "打印(总和)\n",
        3),
    "递归斐波那契": (
        "函数 斐波(n)：\n"
        "    如果 n < 2：\n"
        "        返回 n\n"
        "    返回 斐波(n - 1) + 斐波(n - 2)\n"
        "打印(斐波(18))\n",
        2),
    "当循环倒计数": (
        "令 i = 30000\n"
        "当 i > 0：\n"
        "    i = i - 1\n"
        "打印(i)\n",
        3),
    "列表构建与遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(5000)：\n"
        "    甲.追加(i * 2)\n"
        "令 总和 = 0\n"
        "遍历 v 在 甲：\n"
        "    总和 = 总和 + v\n"
        "打印(总和)\n",
        3),
    "字符串拼接": (
        "令 s = \"\"\n"
        "遍历 i 在 范围(2000)：\n"
        "    s = s + \"字\"\n"
        "打印(长度(s))\n",
        3),
    "函数调用密集": (
        "函数 加一(x)：\n"
        "    返回 x + 1\n"
        "令 总和 = 0\n"
        "遍历 i 在 范围(8000)：\n"
        "    总和 = 加一(总和)\n"
        "打印(总和)\n",
        3),
    "算术混合运算": (
        "令 总和 = 0\n"
        "遍历 i 在 范围(10000)：\n"
        "    总和 = 总和 + i * 3 - 7 // 2 + i % 5\n"
        "打印(总和)\n",
        3),
}

EXECUTORS = [
    ("树遍历", "run_tree"),
    ("Python VM", "run_vm"),
    ("C VM", "run_cvm"),
]


def _run_tree(src: str) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_tree(src, filename="<基准>")


def _run_vm(src: str) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.run_source(src, filename="<基准>")


def _run_cvm(src: str) -> None:
    from jishi import cvm_bind

    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, filename="<基准>")


_RUNNERS = {"run_tree": _run_tree, "run_vm": _run_vm, "run_cvm": _run_cvm}


def _best_time(fn, src: str, repeats: int, tries: int = 3) -> float:
    """重复 repeats 次为一轮，测 tries 轮取最优（秒）。"""
    best = float("inf")
    for _ in range(tries):
        t0 = time.perf_counter()
        for _ in range(repeats):
            fn(src)
        best = min(best, time.perf_counter() - t0)
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description="基石三执行器性能基准")
    ap.add_argument("--quick", action="store_true", help="快速摸底模式")
    args = ap.parse_args()

    tries = 2 if args.quick else 3
    print(f"基石语言性能基准（每场景取 {tries} 轮最优）\n")

    header = f"{'场景':<10} {'遍数':>4}  " + "".join(
        f"{name:>10} " for name, _ in EXECUTORS)
    print(header)
    print("-" * len(header))

    speed_rows: list[tuple[str, list[float]]] = []

    for name, (src, repeats) in SCENARIOS.items():
        times: dict[str, float] = {}
        for label, key in EXECUTORS:
            times[label] = _best_time(_RUNNERS[key], src, repeats, tries)
        base = times["树遍历"]
        cells = []
        ratios = []
        for label, _ in EXECUTORS:
            t = times[label]
            cells.append(f"{t * 1000:>8.1f}ms")
            ratios.append(base / t if t > 0 else 0.0)
        print(f"{name:<10} ×{repeats:<3} " + " ".join(cells))
        speed_rows.append((name, ratios))

    print()
    print("相对树遍历的提速倍数（>1 = 更快）：")
    print(f"{'场景':<10}  " + "".join(f"{name:>10} " for name, _ in EXECUTORS))
    print("-" * 46)
    for name, ratios in speed_rows:
        cells = []
        for r in ratios:
            cells.append(f"{r:>9.1f}x " if r >= 1 else f"{r:>9.2f}x ")
        print(f"{name:<10}  " + " ".join(cells))

    print("\n已知架构边界：")
    print("  字符串拼接：字符串在 C VM 中是 HOST 句柄（C 侧无法直接操作 Python str，")
    print("  每次拼接需回调 Python），而树遍历直接调 Python 的 C 级 str.__add__，")
    print("  故该场景 C VM 无法超过树遍历，是透明记录的架构边界，而非待修缺陷。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
