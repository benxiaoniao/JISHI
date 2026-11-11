# -*- coding: utf-8 -*-
"""审计/性能探针：**基石（Rust VM）vs CPython**，同机同时段交替取样。

## 为什么要有这个

`tools/benchmark.py` 只比**基石自己的四个执行器**，从来没有一条「vs CPython」的线 ——
于是「某处还不如 CPython」只能靠印象，改完也没法说「到底快了没有」。

这份脚本补的就是那条线：同一道题的**两份等价实现**（基石 / Python），
在**同一个进程里交替**各跑 N 轮，每档取最小值，再比。

## 判据（比数字更重要）

- **两份程序输出必须逐字相同**，否则「比的不是同一件事」，这一行直接标 `不等价`、
  数字作废（本项目的老规矩：先对齐语义，再谈快慢）；
- **启动成本单列**：`jishi-rs` 是独立进程（约 100ms 启动），CPython 也有启动。
  两个都量出来、都摆在表里，别把启动算进「语言快慢」；
- **交错取样**：A/B/A/B… 而不是「先把 A 全跑完再跑 B」—— 中途一阵负载会把后跑的抬高
  （M45 那条教训）。

用法：
    python build/ev/m160/bench_vs_cpython.py            # 默认每档 3 轮
    python build/ev/m160/bench_vs_cpython.py --轮数 5
    python build/ev/m160/bench_vs_cpython.py --只跑 文本

🔴 **要比两个版本，必须先固定规模**（M179 用血换的）：
    python tools/bench_vs_python.py --json base.json     # 旧二进制 → 存快照（含重复次数）
    python tools/bench_vs_python.py --用基线 base.json   # 新二进制 → 按同一规模跑
    # 每档跑 3 轮、看**中位**：单轮噪声有 ±10%，一眼看单轮会得出相反结论。

为什么要这条：不固定规模时，重复次数是按「单轮 ≥0.4 秒」**动态标定**的，
它随机器负载变 ⇒ 同一份代码两次跑的根本不是同一个规模的程序。M179 实测：
同一二进制跑出 1.54x / 1.32x / 1.61x；某个场景还从 1.28x 掉到 0.74x
（而那次改动**让它更快了**）—— 因为重复次数一多，CPython 3.13 的**自适应特化**
吃到了红利，而解释器没有分层。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# ⚠️ 分平台（M174）：Windows 是 `jishi-rs.exe`，Linux / macOS 是 `jishi-rs`。
EXE = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")
#: 🔴 用 `-S` 跑 CPython：**这台机器的 site/sitecustomize 带约 350 ms 开销**
#: （WorkBuddy 的 shim 注入），默认启动要 416 ms、`-S` 只要 73 ms。
#: 那是**测量环境**的加料，不是 CPython 本身 —— 不排掉的话每个场景都在比「谁启动快」。
PY = [sys.executable, "-S"]

#: 场景：(基石源码, 等价的 Python 源码, 单轮建议重复次数)
场景 = {
    "整数循环累加": (
        "令 总和 = 0\n"
        "遍历 i 在 范围(20000)：\n"
        "    总和 = 总和 + i\n"
        "打印(总和)\n",
        "s = 0\n"
        "for i in range(20000):\n"
        "    s = s + i\n"
        "print(s)\n", 3),

    "递归斐波那契": (
        "函数 斐波(n)：\n"
        "    如果 n < 2：\n"
        "        返回 n\n"
        "    返回 斐波(n - 1) + 斐波(n - 2)\n"
        "打印(斐波(18))\n",
        "def fib(n):\n"
        "    if n < 2:\n"
        "        return n\n"
        "    return fib(n - 1) + fib(n - 2)\n"
        "print(fib(18))\n", 2),

    "当循环倒计数": (
        "令 i = 30000\n"
        "当 i > 0：\n"
        "    i = i - 1\n"
        "打印(i)\n",
        "i = 30000\n"
        "while i > 0:\n"
        "    i = i - 1\n"
        "print(i)\n", 3),

    "列表构建与遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(5000)：\n"
        "    甲.追加(i * 2)\n"
        "令 总和 = 0\n"
        "遍历 v 在 甲：\n"
        "    总和 = 总和 + v\n"
        "打印(总和)\n",
        "a = []\n"
        "for i in range(5000):\n"
        "    a.append(i * 2)\n"
        "s = 0\n"
        "for v in a:\n"
        "    s = s + v\n"
        "print(s)\n", 3),

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
        "a = []\n"
        "for i in range(400):\n"
        "    a.append(i * 1.5)\n"
        "n = 0\n"
        "r = 0\n"
        "while r < 20:\n"
        "    for v in a:\n"
        "        n = n + 1\n"
        "    r = r + 1\n"
        "print(n)\n", 3),

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
        "a = []\n"
        "for i in range(400):\n"
        "    a.append(\"值\" + str(i))\n"
        "n = 0\n"
        "r = 0\n"
        "while r < 20:\n"
        "    for v in a:\n"
        "        n = n + 1\n"
        "    r = r + 1\n"
        "print(n)\n", 3),

    "字典写入密集": (
        "令 字 = {}\n"
        "遍历 i 在 范围(5000)：\n"
        "    字[i] = i * 2\n"
        "打印(长度(字))\n",
        "d = {}\n"
        "for i in range(5000):\n"
        "    d[i] = i * 2\n"
        "print(len(d))\n", 3),

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
        "d = {}\n"
        "for i in range(400):\n"
        "    d[i] = i * 2\n"
        "s = 0\n"
        "r = 0\n"
        "while r < 20:\n"
        "    for i in range(400):\n"
        "        s = s + d[i]\n"
        "    r = r + 1\n"
        "print(s)\n", 3),

    "内建调用密集": (
        "令 甲 = [1, 2, 3, 4, 5]\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    和 = 和 + 长度(甲)\n"
        "打印(和)\n",
        "a = [1, 2, 3, 4, 5]\n"
        "s = 0\n"
        "for i in range(3000):\n"
        "    s = s + len(a)\n"
        "print(s)\n", 3),

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
        "a = []\n"
        "for i in range(400):\n"
        "    a.append(i % 2 == 0)\n"
        "n = 0\n"
        "r = 0\n"
        "while r < 20:\n"
        "    for v in a:\n"
        "        n = n + 1\n"
        "    r = r + 1\n"
        "print(n)\n", 3),

    "字符串拼接": (
        "令 s = \"\"\n"
        "遍历 i 在 范围(2000)：\n"
        "    s = s + \"字\"\n"
        "打印(长度(s))\n",
        "s = \"\"\n"
        "for i in range(2000):\n"
        "    s = s + \"字\"\n"
        "print(len(s))\n", 3),

    "文本比较": (
        "令 甲 = \"abc\"\n"
        "令 计 = 0\n"
        "遍历 i 在 范围(5000)：\n"
        "    如果 甲 == \"abc\"：\n"
        "        计 = 计 + 1\n"
        "打印(计)\n",
        "a = \"abc\"\n"
        "c = 0\n"
        "for i in range(5000):\n"
        "    if a == \"abc\":\n"
        "        c = c + 1\n"
        "print(c)\n", 3),

    "文本下标": (
        "令 甲 = \"一二三四五\"\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(5000)：\n"
        "    和 = 和 + 长度(甲[i % 5])\n"
        "打印(和)\n",
        "a = \"一二三四五\"\n"
        "s = 0\n"
        "for i in range(5000):\n"
        "    s = s + len(a[i % 5])\n"
        "print(s)\n", 3),

    "文本切片": (
        "令 甲 = \"一二三四五六七八九十\"\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    和 = 和 + 长度(甲[2:7])\n"
        "打印(和)\n",
        "a = \"一二三四五六七八九十\"\n"
        "s = 0\n"
        "for i in range(3000):\n"
        "    s = s + len(a[2:7])\n"
        "print(s)\n", 3),

    "遍历文本": (
        "令 甲 = \"一二三四五\"\n"
        "令 计 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    遍历 字 在 甲：\n"
        "        计 = 计 + 1\n"
        "打印(计)\n",
        "a = \"一二三四五\"\n"
        "c = 0\n"
        "for i in range(3000):\n"
        "    for ch in a:\n"
        "        c = c + 1\n"
        "print(c)\n", 3),

    "函数调用密集": (
        "函数 加一(x)：\n"
        "    返回 x + 1\n"
        "令 总和 = 0\n"
        "遍历 i 在 范围(8000)：\n"
        "    总和 = 加一(总和)\n"
        "打印(总和)\n",
        "def add1(x):\n"
        "    return x + 1\n"
        "s = 0\n"
        "for i in range(8000):\n"
        "    s = add1(s)\n"
        "print(s)\n", 3),

    # M191：两个实参的内联（临时槽 2 个，正好验证「槽不够就退解释器」之外的正路）
    "双参数函数调用": (
        "函数 合(a, b)：\n"
        "    返回 a + b * 2\n"
        "令 总和 = 0\n"
        "遍历 i 在 范围(8000)：\n"
        "    总和 = 合(总和, i)\n"
        "打印(总和)\n",
        "def he(a, b):\n"
        "    return a + b * 2\n"
        "s = 0\n"
        "for i in range(8000):\n"
        "    s = he(s, i)\n"
        "print(s)\n", 3),

    # M191：**类的方法调用**。这一条不是拿来「变快」的 —— 它是**基线**：
    # 机器码里没有堆值的表示，`GET_ATTR` 进不了 JIT，所以它能测出
    # 「方法调用密集的场景，JIT 帮不上忙」这件事到底值多少（见 CHANGELOG）。
    "方法调用密集": (
        "类 计数器：\n"
        "    函数 初始化(自己, 起)：\n"
        "        自己.值 = 起\n"
        "    函数 取(自己)：\n"
        "        返回 自己.值\n"
        "令 c = 计数器(3)\n"
        "令 总和 = 0\n"
        "遍历 i 在 范围(8000)：\n"
        "    总和 = 总和 + c.取()\n"
        "打印(总和)\n",
        "class C:\n"
        "    def __init__(self, v):\n"
        "        self.v = v\n"
        "    def get(self):\n"
        "        return self.v\n"
        "c = C(3)\n"
        "s = 0\n"
        "for i in range(8000):\n"
        "    s += c.get()\n"
        "print(s)\n", 3),

    "算术混合运算": (
        "令 总和 = 0\n"
        "遍历 i 在 范围(10000)：\n"
        "    总和 = 总和 + i * 3 - 7 // 2 + i % 5\n"
        "打印(总和)\n",
        "s = 0\n"
        "for i in range(10000):\n"
        "    s = s + i * 3 - 7 // 2 + i % 5\n"
        "print(s)\n", 3),
}


def 跑一次(命令, cwd):
    t0 = time.perf_counter()
    r = subprocess.run(命令, cwd=cwd, capture_output=True, timeout=300, input=b"")
    return time.perf_counter() - t0, r


def 量启动(d):
    """各量 5 次取最优：空程序的墙钟（含进程启动）。"""
    (d / "空.jsh").write_text("", encoding="utf-8")
    (d / "空.py").write_text("", encoding="utf-8")
    甲 = min(跑一次([str(EXE), "空.jsh"], d)[0] for _ in range(5))
    乙 = min(跑一次(PY + ["空.py"], d)[0] for _ in range(5))
    return 甲, 乙


#: 目标单轮时长（秒）。**低于这个数，启动占比太大、噪声也盖过差异**
#: （v1 拿 1~3 ms 的净耗时算倍数，读出「75x」那种离谱数）。
目标时长 = 0.4


def 定重复次数(名, jsh, py, d, 建议, 启基石, 启py, 指定=None):
    """把「跑多久」放大到 ≈目标时长：两边各试跑一次，按实测比例放大。

    为什么两边**用同一个 repeats**：比的是「同一道题谁快」，
    各自调重复次数等于比不同的题。取两边所需倍数的**较大者**。

    `指定` 不为空时**直接用指定值**（来自基线快照）——
    ⚠️ 这一点很关键：动态算出来的次数会随机器瞬时负载变，于是「同一台机器、
    同一份代码」两次跑出来的可能是**不同规模**的程序（实测两次能差 3 倍，
    见 M162 的教训）。要跟基线比，就得跑与基线**同样规模**的那一份。
    """
    if 指定:
        (d / f"{名}.jsh").write_text(jsh * 指定, encoding="utf-8", newline="\n")
        (d / f"{名}.py").write_text(py * 指定, encoding="utf-8", newline="\n")
        return 指定
    (d / f"{名}.jsh").write_text(jsh * 建议, encoding="utf-8", newline="\n")
    (d / f"{名}.py").write_text(py * 建议, encoding="utf-8", newline="\n")
    t甲, _ = 跑一次([str(EXE), f"{名}.jsh"], d)
    t乙, _ = 跑一次(PY + [f"{名}.py"], d)
    # ⚠️ 按**净耗时**（扣掉启动）放大，否则启动一大就永远只跑 1 遍
    净甲 = max(t甲 - 启基石, 1e-4)
    净乙 = max(t乙 - 启py, 1e-4)
    需甲 = max(1, int(目标时长 / 净甲))
    需乙 = max(1, int(目标时长 / 净乙))
    return max(需甲, 需乙)


def _机器指纹() -> dict:
    """记下「这份数字是哪台机器、哪个版本跑的」。

    📌 没有它，跨机器的数字会被当成同一件事比 —— 而基线快照存在的意义
    恰恰是「这台机器上，这一版比上一版是升还是降」。
    """
    import platform
    版本 = ""
    try:
        r = subprocess.run([str(EXE), "--version"], capture_output=True,
                           timeout=30, input=b"")
        版本 = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    except Exception:                                        # noqa: BLE001
        pass
    return {
        "平台": platform.platform(),
        "处理器": platform.processor() or "（取不到）",
        "机器位宽": platform.machine(),
        "python": platform.python_version(),
        "jishi": 版本,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--轮数", type=int, default=3)
    ap.add_argument("--只跑", default="")
    ap.add_argument("--json", default="",
                    help="把结果写成 JSON（建议给一个路径，例如 bench/baseline_cpython.json）")
    ap.add_argument("--用基线", default="",
                    help="按这份基线里的「重复次数」跑（要与基线比时必须给，"
                         "否则两次跑的不是同一规模）")
    a = ap.parse_args()

    # 基线里固化的重复次数（给了 --用基线 才用）
    固定次 = {}
    if a.用基线:
        import json as _j
        基路 = Path(a.用基线)
        if not 基路.is_absolute():
            基路 = ROOT / 基路
        基 = _j.loads(基路.read_text(encoding="utf-8"))
        固定次 = {r["名字"]: r.get("重复") for r in 基.get("场景", [])
                  if r.get("重复")}
        print(f"按基线重复次数跑（{len(固定次)} 个场景）：{基路.name}")

    d = Path(tempfile.mkdtemp(prefix="bench_vs_py_"))
    print(f"工作目录：{d}")
    print(f"基石：{EXE}")
    print(f"CPython：{' '.join(PY)}")
    print()

    启基石, 启py = 量启动(d)
    print(f"启动成本（5 次取最优）：基石 {启基石*1000:.0f} ms · "
          f"CPython {启py*1000:.0f} ms")
    print()

    选 = {k: v for k, v in 场景.items() if (not a.只跑 or a.只跑 in k)}
    print(f"{'场景':<14}{'重复':>5}{'基石(ms)':>10}{'CPython(ms)':>13}"
          f"{'倍数':>9}   备注")
    print("-" * 66)
    结果 = []
    for 名, (jsh, py, 建议) in 选.items():
        次 = 定重复次数(名, jsh, py, d, 建议, 启基石, 启py, 固定次.get(名))
        (d / f"{名}.jsh").write_text(jsh * 次, encoding="utf-8", newline="\n")
        (d / f"{名}.py").write_text(py * 次, encoding="utf-8", newline="\n")

        # 先对齐语义：两边输出必须一模一样（不然比的不是同一件事）
        _t1, r1 = 跑一次([str(EXE), f"{名}.jsh"], d)
        _t2, r2 = 跑一次(PY + [f"{名}.py"], d)
        o1 = (r1.stdout + r1.stderr).decode("utf-8", "replace").strip()
        o2 = (r2.stdout + r2.stderr).decode("utf-8", "replace").strip()
        # ⚠️ Windows 上 CPython 的 stdout 是文本模式（`\r\n`），基石是 `\n` ——
        #    这是**平台换行**差异，不是语义差异，比之前先归一（否则 17 个场景全废）
        o1n, o2n = o1.replace("\r\n", "\n"), o2.replace("\r\n", "\n")
        if o1n != o2n:
            结果.append({"名字": 名, "重复": 次, "不等价": True,
                         "基石ms": None, "cpython ms": None, "倍数": None})
            print(f"{名:<14}{次:>5}{'—':>10}{'—':>13}{'—':>9}   输出不等价："
                  f"{o1[:20]!r} vs {o2[:20]!r}")
            continue

        甲, 乙 = [], []
        for _ in range(a.轮数):
            t, _r = 跑一次([str(EXE), f"{名}.jsh"], d)
            甲.append(t)
            t, _r = 跑一次(PY + [f"{名}.py"], d)
            乙.append(t)
        m甲, m乙 = min(甲), min(乙)
        倍 = m乙 / m甲 if m甲 > 0 else float("inf")
        结果.append({"名字": 名, "重复": 次, "基石ms": round(m甲 * 1000, 1),
                     "cpython ms": round(m乙 * 1000, 1), "倍数": round(倍, 3)})
        尾 = "基石更快" if 倍 >= 1 else "**CPython 更快**"
        print(f"{名:<14}{次:>5}{m甲*1000:>10.1f}{m乙*1000:>13.1f}{倍:>8.2f}x   {尾}")

    print("-" * 66)
    有效 = [r for r in 结果 if r.get("倍数")]
    慢的 = [r for r in 有效 if r["倍数"] < 1]
    几何 = None
    if 有效:
        几何 = 1.0
        for r in 有效:
            几何 *= max(r["倍数"], 1e-6)
        几何 **= (1.0 / len(有效))
        几何 = round(几何, 3)
        print(f"共 {len(有效)} 个有效场景 · 几何平均 **{几何:.2f}x** · "
              f"基石更慢的 {len(慢的)} 个")
        if 慢的:
            print("更慢的：" + "、".join(f"{r['名字']}({r['倍数']:.2f}x)" for r in 慢的))
    print(f"\n（含进程启动：基石约 {启基石*1000:.0f} ms · CPython 约 {启py*1000:.0f} ms；"
          f"单轮已放大到 ≥{目标时长*1000:.0f} ms，启动占比 <10%）")

    if a.json:
        import json as _json
        from datetime import datetime
        快照 = {
            "格式": "jishi-bench-vs-cpython",
            "格式版本": 1,
            "时间": datetime.now().astimezone().isoformat(timespec="seconds"),
            "机器": _机器指纹(),
            "启动ms": {"基石": round(启基石 * 1000, 1),
                       "cpython": round(启py * 1000, 1)},
            "轮数": a.轮数,
            "目标单轮ms": round(目标时长 * 1000),
            "场景": 结果,
            "几何平均": 几何,
            "更慢的": [r["名字"] for r in 慢的],
        }
        路径 = Path(a.json)
        if not 路径.is_absolute():
            路径 = ROOT / 路径
        路径.parent.mkdir(parents=True, exist_ok=True)
        路径.write_text(_json.dumps(快照, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print(f"\n已写入：{路径}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
