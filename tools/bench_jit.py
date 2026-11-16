# -*- coding: utf-8 -*-
"""JIT 与解释器的对照基准（M180）。

    python tools/bench_jit.py              # 默认每档 3 轮取最小
    python tools/bench_jit.py --轮数 5
    python tools/bench_jit.py --只看 累加

## 为什么单开一个工具，不并进 `bench_vs_python.py`

`bench_vs_python.py` 比的是**基石 vs CPython**，跑的是「整个程序」的墙钟时间
（含进程启动、解析、字节码）。JIT 的收益在这个口径下会被稀释：一个 2 千万次的
循环编完只要几十毫秒，而进程启动固定要几十毫秒。

所以这里把口径钉成**同一台机器、同一个二进制、只差 `JISHI_JIT` 这一个环境变量**，
逐个场景量「解释器 ms / JIT ms / 倍数」。它回答的问题很具体：
**这段循环交给机器码之后快了多少。**

⚠️ 与 `bench_vs_python.py` 同一条规矩：**每档多轮取最小**（单轮噪声 ±10%，
一眼看单轮会得出相反结论）。这里取最小而不是中位，是因为要抓的是「这段代码最快
能多快」——它不受后台进程干扰。
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")

#: 每个场景：`(循环体每轮大概干什么, 源码)`。语法必须是「整数 `当` 循环」——
#: 别的形态 JIT 不编（见 `rust/src/jit.rs` 模块文档）。
场景 = {
    "累加": """
函数 主(n)：
    令 i = 0
    令 s = 0
    当 i < n：
        s = s + i
        i = i + 1
    返回 s
打印(主({n}))
""",
    "乘加": """
函数 主(n)：
    令 i = 0
    令 s = 1
    当 i < n：
        s = s + i * 3
        i = i + 1
    返回 s
打印(主({n}))
""",
    "双变量": """
函数 主(n)：
    令 i = 0
    令 a = 1
    令 b = 2
    当 i < n：
        a = a + b
        b = b + 1
        i = i + 1
    返回 a + b
打印(主({n}))
""",
    # ---- M182 新编的三类 ----
    "遍历累加": """
函数 主(n)：
    令 s = 0
    遍历 x 在 范围(0, n)：
        s = s + x
    返回 s
打印(主({n}))
""",
    "带如果": """
函数 主(n)：
    令 i = 0
    令 s = 0
    当 i < n：
        如果 i % 2 == 0：
            s = s + i
        否则：
            s = s - 1
        i = i + 1
    返回 s
打印(主({n}))
""",
    # ---- M183 新编的两类（浮点）----
    "浮点累加": """
函数 主(n)：
    令 i = 0
    令 s = 0.0
    当 i < n：
        s = s + i * 0.5
        i = i + 1
    返回 s
打印(主({n}))
""",
    "浮点积分": """
函数 主(n)：
    令 i = 0
    令 x = 0.0
    令 v = 1.5
    当 i < n：
        x = x + v * 0.01
        v = v - 0.001 * x
        i = i + 1
    返回 x + v
打印(主({n}))
""",
    "带中断": """
函数 主(n)：
    令 i = 0
    令 s = 0
    当 i < n：
        s = s + 1
        如果 i * 3 > n：
            s = s + i
            中断
        i = i + 1
    返回 s
打印(主({n}))
""",
}

#: 每个场景的规模系数（`默认规模 × 系数`）。
#:
#: `遍历累加` 得先造一个长度为 n 的**列表**（`范围()` 是当场物化的），
#: 5 千万个 `Val` 是 1 GB 级 —— 所以它自己小两个数量级。
#: `带如果` 每轮一次 `%`（idiv），解释器侧慢一档，也降一点免得跑太久。
规模系数 = {"遍历累加": 0.02, "带如果": 0.4, "浮点累加": 0.4, "浮点积分": 0.3}

#: 默认规模。取 5000 万：解释器上约 7 秒，JIT 上约 0.1 秒 ——
#: 两边都远超进程启动的固定开销。
默认规模 = 50_000_000


def 跑一次(源码: str, 开: bool) -> tuple[float, str]:
    import hashlib
    import tempfile

    路径 = str(
        Path(tempfile.gettempdir())
        / f"jishi_bench_jit_{hashlib.md5(源码.encode('utf-8')).hexdigest()[:10]}.jsh"
    )
    Path(路径).write_text(源码, encoding="utf-8", newline="\n")
    env = dict(os.environ)
    if not 开:
        env["JISHI_JIT"] = "0"
    t0 = time.perf_counter()
    r = subprocess.run([str(EXE), 路径], capture_output=True, env=env, timeout=900)
    耗 = time.perf_counter() - t0
    if r.returncode != 0:
        raise SystemExit(f"跑失败了：\n{r.stderr.decode('utf-8', 'replace')[:800]}")
    return 耗, r.stdout.decode("utf-8", "replace").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="JIT vs 解释器（M180 v0 / M182 v1 / M183 浮点）")
    ap.add_argument("--轮数", type=int, default=3, help="每档跑几轮，取最小（默认 3）")
    ap.add_argument("--规模", type=int, default=默认规模, help=f"循环次数（默认 {默认规模}）")
    ap.add_argument("--只看", default=None, help="只跑名字里含这个串的场景")
    a = ap.parse_args()

    if not EXE.is_file():
        print(f"没有 {EXE}（先 cargo build --release）")
        return 1

    print(f"规模 {a.规模:,} 次 · 每档 {a.轮数} 轮取最小 · 只差 JISHI_JIT 这一个变量\n")
    print(f"{'场景':<10}{'解释器 ms':>12}{'JIT ms':>10}{'倍数':>9}")
    print("-" * 43)
    倍数们 = []
    for 名, 模板 in 场景.items():
        if a.只看 and a.只看 not in 名:
            continue
        规模 = int(a.规模 * 规模系数.get(名, 1.0))
        源 = 模板.format(n=规模)
        最慢, 出1 = min((跑一次(源, False) for _ in range(a.轮数)), key=lambda x: x[0])
        最快, 出2 = min((跑一次(源, True) for _ in range(a.轮数)), key=lambda x: x[0])
        if 出1 != 出2:
            raise SystemExit(f"「{名}」两种模式结果不同：{出1!r} vs {出2!r}")
        倍 = 最慢 / 最快
        倍数们.append(倍)
        print(f"{名:<10}{最慢 * 1000:>12.0f}{最快 * 1000:>10.0f}{倍:>8.1f}x")
    if 倍数们:
        几 = 1.0
        for x in 倍数们:
            几 *= x
        print("-" * 43)
        print(f"{'几何':<10}{'':>12}{'':>10}{几 ** (1 / len(倍数们)):>8.1f}x")
    print("\n⚠️ 这只是「整数 `当` 循环」这一段；整程序的加速比它小得多")
    print("   （进程启动、解析、打印都不是 JIT 的事）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
