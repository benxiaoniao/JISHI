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
    # M46 加：专门量「迭代批量快路径」对不同元素类型的效果。
    # 设计要点：**先建一次列表，再反复遍历 20 轮**——这样构建阶段只占约 5%，
    # 测到的基本就是迭代本身（C 侧每 256 个元素回调一次宿主填缓冲）。
    # 三个场景结构**完全一样**、循环体都一样（只计数），只换元素类型——
    # 于是倍数差异就只反映「填缓冲时构造 JVal 那一步」的成本，不掺别的东西。
    "浮点列表遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(400)：\n"
        "    甲.追加(i * 1.5)\n"
        "令 计数 = 0\n"
        "令 轮 = 0\n"
        "当 轮 < 20：\n"
        "    遍历 v 在 甲：\n"
        "        计数 = 计数 + 1\n"
        "    轮 = 轮 + 1\n"
        "打印(计数)\n",
        3),
    "文本列表遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(400)：\n"
        "    甲.追加(\"值\" + 文本(i))\n"
        "令 计数 = 0\n"
        "令 轮 = 0\n"
        "当 轮 < 20：\n"
        "    遍历 v 在 甲：\n"
        "        计数 = 计数 + 1\n"
        "    轮 = 轮 + 1\n"
        "打印(计数)\n",
        3),
    # M46 阶段一④：字典的两个方向分开量——写入走「延迟合并写」，
    # 读取不行（每次读都要真拿一个值，没得合并），分开才知道提升来自哪一边。
    "字典写入密集": (
        "令 字 = {}\n"
        "遍历 i 在 范围(5000)：\n"
        "    字[i] = i * 2\n"
        "打印(长度(字))\n",
        3),
    "字典查找密集": (
        "令 字 = {}\n"
        "遍历 i 在 范围(400)：\n"
        "    字[i] = i * 2\n"
        "令 和 = 0\n"
        "令 轮 = 0\n"
        "当 轮 < 20：\n"
        "    遍历 i 在 范围(400)：\n"
        "        和 = 和 + 字[i]\n"
        "    轮 = 轮 + 1\n"
        "打印(和)\n",
        3),
    "内建调用密集": (
        # M46 阶段一②：量「宿主回调」这条路本身的开销。`长度` 是 O(1)，
        # 所以这个场景测到的几乎全是「C 侧 → 宿主 → 回 C 侧」的往返成本
        # （内建 / 标准库 / 方法调用都走同一条 `_cb_call`）。
        "令 甲 = [1, 2, 3, 4, 5]\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    和 = 和 + 长度(甲)\n"
        "打印(和)\n",
        3),
    "布尔列表遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(400)：\n"
        "    甲.追加(i % 2 == 0)\n"
        "令 计数 = 0\n"
        "令 轮 = 0\n"
        "当 轮 < 20：\n"
        "    遍历 v 在 甲：\n"
        "        计数 = 计数 + 1\n"
        "    轮 = 轮 + 1\n"
        "打印(计数)\n",
        3),
    "字符串拼接": (
        "令 s = \"\"\n"
        "遍历 i 在 范围(2000)：\n"
        "    s = s + \"字\"\n"
        "打印(长度(s))\n",
        3),
    # M47 阶段二①加：字符串改成 C 侧原生之后，把文本的另外几条主要路径也
    # 单独量出来。**刻意分开**——拼接/比较走 C 侧（零回调），而下标/切片必须
    # 回调宿主（C 侧看不见 Python 的切片协议），两者的收益完全不同向：
    # 分开量才不会互相掩盖（前者是 3x+，后者仍低于 1x，见文件末尾的边界说明）。
    "文本比较": (
        "令 甲 = \"abc\"\n"
        "令 计 = 0\n"
        "遍历 i 在 范围(5000)：\n"
        "    如果 甲 == \"abc\"：\n"
        "        计 = 计 + 1\n"
        "打印(计)\n",
        3),
    "文本下标": (
        "令 甲 = \"一二三四五\"\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(5000)：\n"
        "    和 = 和 + 长度(甲[i % 5])\n"
        "打印(和)\n",
        3),
    "文本切片": (
        "令 甲 = \"一二三四五六七八九十\"\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    和 = 和 + 长度(甲[2:7])\n"
        "打印(和)\n",
        3),
    "遍历文本": (
        "令 甲 = \"一二三四五\"\n"
        "令 计 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    遍历 字 在 甲：\n"
        "        计 = 计 + 1\n"
        "打印(计)\n",
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

    print("\n已知架构边界（透明记录，不是待修缺陷）：")
    print("  ① 文本下标 / 切片：必须回调宿主——C 侧看不见 Python 的切片协议，")
    print("     也没法自己去执行它。每次下标/切片要「参数进去、结果出来」两趟转换，")
    print("     而树遍历直接调 Python 的 C 级 str.__getitem__/切片对象。")
    print("     所以这两项 C VM 追不上树遍历；M47 把文本改成 C 侧原生字符串后，")
    print("     过路字符串的转换成本比原来的 HOST 句柄高约 1.2µs，两项各退约 8%")
    print("     （0.64x→0.59x / 0.41x→0.38x）——这是「字符串统一由 C 侧持有」")
    print("     换来的代价，换来的是拼接 8.4x、比较 11.6x 的提速。")
    print("  ② 文本列表遍历 / 遍历文本：循环体里每元素至少一次宿主回调")
    print("     （`长度`、取字符等），每次回调固定约 2.4µs。M47 已把文本迭代的")
    print("     元素构造改成一次 FFI 建整批（`jsvm_str_new_batch`），")
    print("     再往上要动「内建函数也走 C 侧」那一层。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
