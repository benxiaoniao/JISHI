# -*- coding: utf-8 -*-
"""C VM 内存泄漏探针：反复跑同一个程序，量「每次净增多少字节」。

为什么单独有这个工具：`JS_TAG_STR` / 迭代器缓冲这些是 **C 侧 malloc** 出来的，
**引用计数漏减不会报错、也不会让对拍变红**——只会让长跑进程（LSP / MCP / REPL）
的内存一直涨。这类 bug 只能靠「反复跑 + 看内存」抓。

用法：
    python tools/leak_probe.py            # 跑内置场景表
    python tools/leak_probe.py 文件.jsh    # 只量一个程序

判读方法（重要）：
- **必须预热**（默认 200 次）再看，否则会把「堆第一次长上去」当成泄漏
  （实测：预热不足时同一场景会报 600 字节/次，预热后是 0.0）。
- 取**多窗口的最小值**（默认 3 窗），malloc 的碎片抖动会把单窗读数抬高。
- 判据是「**是否线性增长**」，不是某个绝对阈值。基线（空程序）在
  Windows/mingw 上就有约 ±200 字节/次的噪声。

已知边界：只在 Windows 上能量（用 `GetProcessMemoryInfo`）；其它平台直接退出 0。

M47 阶段二①的实战：这个探针抓到「迭代器预取缓冲每消费一个元素漏一份引用」
——遍历 1000 个文本一次漏约 33KB，修复后回落到噪声水平。
"""
from __future__ import annotations

import ctypes
import gc
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _PMC(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS。

    ⚠️ **字段一个都不能少**（2×DWORD + 8×SIZE_T = 72 字节）：结构体定义小了
    `GetProcessMemoryInfo` 会**静默失败**、读数恒为 0，探针就把自己骗过去了
    （第一版就是这么写的，白高兴一场）。
    """

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_k32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC),
                                        ctypes.c_uint32]
_psapi.GetProcessMemoryInfo.restype = ctypes.c_int


def rss_bytes() -> int:
    c = _PMC()
    c.cb = ctypes.sizeof(c)
    if not _psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(),
                                       ctypes.byref(c), c.cb):
        raise RuntimeError("GetProcessMemoryInfo 失败（结构体大小不匹配？）")
    return c.WorkingSetSize


def measure(src: str, window: int = 200, warm: int = 200,
            windows: int = 3) -> float:
    """反复跑 `src`，返回「每次净增字节数」（多窗口取最小值）。"""
    from jishi import cvm_bind

    for _ in range(warm):
        with redirect_stdout(io.StringIO()):
            cvm_bind.run_source_c(src, filename="<leak>")

    best = None
    for _ in range(windows):
        gc.collect()
        before = rss_bytes()
        for _ in range(window):
            with redirect_stdout(io.StringIO()):
                cvm_bind.run_source_c(src, filename="<leak>")
        gc.collect()
        per = (rss_bytes() - before) / window
        best = per if best is None else min(best, per)
    assert best is not None
    return best


BUILD = '令 甲 = []\n遍历 i 在 范围(300)：\n    甲.追加("值" + 文本(i))\n'
SUM = BUILD + '令 s = ""\n遍历 v 在 甲：\n    s = s + v\n'
DICT = SUM + '令 字 = {}\n遍历 i 在 范围(200)：\n    字["k" + 文本(i)] = s\n'

#: 内置场景表：**每条对应一类 C 侧分配**，分开量才知道漏在哪
CASES: dict[str, str] = {
    "空程序（基线噪声）": "令 x = 1\n",
    "文本拼接（原生字符串分配）": '令 s = ""\n遍历 i 在 范围(200)：\n    s = s + "字"\n',
    "列表构建 + 文本遍历": BUILD + "打印(长度(甲))\n",
    "列表 + 累加拼接": SUM + "打印(长度(s))\n",
    "列表 + 累加 + 字典写": DICT + "打印(长度(字))\n",
    "全流程 + 遍历字典": DICT + "遍历 键 在 字：\n    令 x = 字[键]\n打印(长度(字))\n",
    "遍历 1000 个短文本": '令 甲 = []\n遍历 i 在 范围(1000)：\n    甲.追加("字")\n令 c = 0\n遍历 v 在 甲：\n    c = c + 1\n',
    "遍历 400 个内层列表": '令 甲 = []\n遍历 i 在 范围(400)：\n    甲.追加([i])\n令 c = 0\n遍历 v 在 甲：\n    c = c + 1\n',
    "遍历中途中断": '令 甲 = []\n遍历 i 在 范围(400)：\n    甲.追加([i])\n令 c = 0\n遍历 v 在 甲：\n    c = c + 1\n    如果 c >= 300：\n        中断\n',
    "嵌套两层遍历": '令 甲 = []\n遍历 i 在 范围(400)：\n    甲.追加([i])\n令 d = 0\n遍历 v 在 甲：\n    遍历 w 在 甲：\n        d = d + 1\n',
    "文本比较": '令 c = 0\n遍历 i 在 范围(200)：\n    如果 "甲" == "甲"：\n        c = c + 1\n',
    "函数里返回拼接": '函数 加(s)：\n    返回 s + "!"\n令 总 = ""\n遍历 i 在 范围(200)：\n    总 = 加(总)\n',
    "异常被接住后继续": '令 甲 = []\n遍历 i 在 范围(200)：\n    甲.追加([i])\n尝试：\n    遍历 v 在 甲：\n        抛出 值错误("x")\n捕获 值错误：\n    打印("接住")\n',
}

#: 判定阈值（字节/次）：基线噪声就有 ±200，超过这个数才值得查
THRESHOLD = 1500


def main(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("本探针只在 Windows 上量内存（GetProcessMemoryInfo），跳过。")
        return 0

    if len(argv) > 1:
        path = Path(argv[1])
        cases = {str(path): path.read_text(encoding="utf-8")}
    else:
        cases = CASES

    print("C VM 内存泄漏探针（预热 200 次 + 3 窗取最小；单位：字节/次）\n")
    print(f"{'场景':26s} {'净增':>10s}")
    worst = 0.0
    worst_name = ""
    for name, src in cases.items():
        per = measure(src)
        if per > worst:
            worst, worst_name = per, name
        print(f"{name:26s} {per:10.1f}{'  ← 偏高' if per > THRESHOLD else ''}")

    print()
    if worst > THRESHOLD:
        print(f"❌ 「{worst_name}」{worst:.0f} 字节/次，超过阈值 {THRESHOLD}"
              "——疑似泄漏（先确认是不是线性增长，再查引用计数）")
        return 1
    print(f"✅ 最大 {worst:.0f} 字节/次，在噪声范围内（阈值 {THRESHOLD}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
