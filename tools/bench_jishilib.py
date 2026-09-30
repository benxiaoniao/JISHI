# -*- coding: utf-8 -*-
"""基石库层的性能对比：**同一份工作，两种实现各要多久**（M52）。

## 为什么要有它

M52 让「纯逻辑标准库」可以用**基石**写一份、五个引擎共用（`jishi/stdlib-jishi/`）。
好处是**不再写三遍**、不再漂移；代价是**慢** —— 库代码走字节码，
而宿主层那份是各语言的原生实现。

代价必须**可度量、可复核**，不能凭感觉说「大概几倍」。本脚本就是那把尺子。

## 三个规矩（都是踩过坑的）

1. **同机同时段交错 A/B**：绝对毫秒会随机器负载大幅漂移
   （同一场景同一代码，今天量到过 31ms 也量到过 52ms），由它算出的倍数跟着漂。
   所以交替跑「基石库版 / 宿主层版」各 N 轮，各取最小值。
2. **计时放在基石程序内部**（`时间.高精度时间()`）：编译时间不计入，
   而且三个执行器口径一致。
3. **报倍数，也报绝对值**：只报倍数看不出量级。

## 用法

    python tools/bench_jishilib.py             # 打表
    python tools/bench_jishilib.py --markdown  # Markdown 表（贴文档用）

切换两版靠环境变量 `JISHI_NO_JISHILIB=1`（关掉基石库层 → 导入回落宿主层）。
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 基准程序：把「编译时间」排除在外，只量执行。
#: `循环 3000 次` 里调 `统计.求和(10 个数)` —— 这是标准库被高频调用时的形态。
SRC = """\
导入 统计
导入 时间
令 数据 = [1,2,3,4,5,6,7,8,9,10]
令 t0 = 时间.高精度时间()
令 总 = 0
循环 3000 次：
    总 = 总 + 统计.求和(数据)
打印(时间.高精度时间() - t0)
"""

ROUNDS = 6
CALLS = 3000


def _once(no_lib: bool, runner) -> float:
    if no_lib:
        os.environ["JISHI_NO_JISHILIB"] = "1"
    else:
        os.environ.pop("JISHI_NO_JISHILIB", None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        runner(SRC)
    return float(buf.getvalue().strip())


def _bench(runner) -> tuple:
    """交错跑两版，各取最小值。"""
    lib, host = [], []
    for _ in range(ROUNDS):
        lib.append(_once(False, runner))
        host.append(_once(True, runner))
    return min(lib), min(host)


def _engines() -> list:
    from jishi import cvm_bind
    from jishi import vm as vm_mod
    from jishi.compiler import compile_source
    from jishi.interpreter import run_source

    return [
        ("树遍历", run_source),
        ("Python VM", lambda s: vm_mod.VM(compile_source(s, "<基准>"), "<基准>").run()),
        ("C VM", lambda s: cvm_bind.run_source_c(s, "<基准>")),
    ]


def main(argv: list[str]) -> int:
    markdown = "--markdown" in argv
    rows = []
    for name, runner in _engines():
        lib, host = _bench(runner)
        rows.append((name, host, lib, lib / host))

    if markdown:
        print(f"| 引擎 | 宿主层 | 基石库 | 倍数 |")
        print("|------|--------|--------|------|")
        for name, host, lib, ratio in rows:
            print(f"| {name} | {host * 1e6:.2f} µs | {lib * 1e6:.2f} µs | "
                  f"**{ratio:.1f}x** |")
    else:
        print(f"{'引擎':10s} {'宿主层(µs)':>12s} {'基石库(µs)':>12s} {'倍数':>8s}")
        print("-" * 46)
        for name, host, lib, ratio in rows:
            print(f"{name:10s} {host * 1e6:12.2f} {lib * 1e6:12.2f} {ratio:7.1f}x")
    print(f"\n（{CALLS} 次 `统计.求和(10 个数)`；交错跑 {ROUNDS} 轮取最小值；"
          f"计时在程序内部，编译不计入）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
