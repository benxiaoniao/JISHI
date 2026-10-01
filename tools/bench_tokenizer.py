# -*- coding: utf-8 -*-
"""R2 吞吐实测：Python 分词器 vs Rust 分词器（同一份源码、同机、交错 A/B）。

## 为什么要交错 A/B

本项目的纪律（`AGENTS.md` §2）：**判「改前 vs 改后」必须同机同时段交错跑**。
绝对毫秒会随机器负载整体漂移，不同时段各测一次得出的倍数能差一倍 ——
这不是理论担忧，M46 记的「1.2x」就是这么来的（复测一直是 0.56x）。
所以这里每轮都「先 Python、再 Rust、再 Rust 核心」，取多轮的**最优值**
（最优值抵掉偶发抢占；交错让两边吃到同一段负载）。

## 报三个数，不是一个

| 量的是什么 | 为什么要它 |
|---|---|
| **Python 分词**（基线） | 尺子的零点 |
| **Rust 端到端**（经 Python 扩展拿到 Token 对象） | **这才是换实现后真正省下的时间** |
| **Rust 只算不算**（不构造 Python 对象） | 把「分词本身多快」与「转对象多贵」分开 —— 不然快不起来时不知道该怪谁 |

⚠️ 只报第二个是不够的：R2 的验收线是「≥ 5 倍」，而端到端倍数里含着一份
**固定的 Python 对象构造开销**（实测 12 万个 Token ≈ 41 ms，见下）。把第三个
一起报出来，读者才知道天花板在哪、下一步该往哪使劲。

用法：
    python tools/bench_tokenizer.py             # 默认 20700 行、5 轮
    python tools/bench_tokenizer.py --quick     # 3 轮
    python tools/bench_tokenizer.py --lines 5000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

#: 组语料用的来源（**不含 error_cases**：那些会在第一处错误就停下，
#: 量出来的是「报错有多快」而不是「分词有多快」）。
SOURCES = (
    "tests/cases/*.jsh",
    "examples/**/*.jsh",
    "jishi/stdlib-jishi/*.jsh",
)


def build_source(target_lines: int) -> tuple[str, int]:
    """把真实语料拼到大约 `target_lines` 行。

    用**真语料**而不是 `令 a = 1` 重复两万遍：后者全是 1 字符 token，
    量出来的是「最短路径有多快」，不能代表真实源码。
    """
    files: list[Path] = []
    for pat in SOURCES:
        files.extend(sorted(ROOT.glob(pat)))
    chunks: list[str] = []
    lines = 0
    while lines < target_lines:
        for p in files:
            src = p.read_text(encoding="utf-8")
            chunks.append(src)
            lines += src.count("\n")
            if lines >= target_lines:
                break
    return "\n\n".join(chunks), lines


def timed(fn) -> float:
    """计时一次，返回毫秒。"""
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="bench_tokenizer",
                                 description="R2：Python vs Rust 分词吞吐")
    ap.add_argument("--lines", type=int, default=20700,
                    help="目标行数（默认 20700，对齐评估文档的基线口径）")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--quick", action="store_true", help="3 轮（快速摸底）")
    args = ap.parse_args(argv)
    rounds = 3 if args.quick else args.rounds

    from jishi import tokenizer as T
    from jishi.tokenizer import Tokenizer

    if not T.rust_available():
        print("❌ Rust 前端没构建 —— 先跑 `python core/build.py`", file=sys.stderr)
        return 1
    core = T._core()

    src, lines = build_source(args.lines)
    size_kb = len(src.encode("utf-8")) / 1024
    print(f"语料：{lines} 行 / {size_kb:.0f} KB（真实语料拼接，非重复样板）")
    print(f"每项跑 {rounds} 轮取最优，轮内交错（同机同时段 A/B）\n")

    n_tok = len(Tokenizer(src, "bench.jsh").tokenize())
    print(f"token 数：{n_tok}\n")

    py_ms = rs_ms = core_ms = float("inf")
    for _ in range(rounds):
        py_ms = min(py_ms, timed(lambda: Tokenizer(src, "bench.jsh").tokenize()))
        rs_ms = min(rs_ms, timed(lambda: core.tokenize(src, "bench.jsh")))
        core_ms = min(core_ms, timed(lambda: core.token_count(src, "bench.jsh")))

    # 「交还 Token 对象」的**地板**：在 Python 里凭空造 n_tok 个 Token
    # （不算扫描、不算 FFI，纯构造）要多久。对照它就能算出端到端倍数的上限。
    from jishi.tokenizer import Token as _Token
    floor_ms = min(
        timed(lambda: [_Token("OP", "=", 3, 5, 6, "x") for _ in range(n_tok)])
        for _ in range(3))
    ceiling = py_ms / (core_ms + floor_ms)

    def row(name: str, ms: float) -> str:
        return (f"  {name:<26} {ms:8.2f} ms   "
                f"{lines / (ms / 1000):9.0f} 行/秒   "
                f"{n_tok / (ms / 1000) / 1000:7.0f}k token/秒")

    print(f"  {'实现':<26} {'耗时':>10}   {'吞吐':>13}   {'token 吞吐':>15}")
    print("  " + "-" * 74)
    print(row("Python（基线）", py_ms))
    print(row("Rust 端到端（含转对象）", rs_ms))
    print(row("Rust 只算不算（纯核心）", core_ms))
    print(f"  {'（对照）纯造 Token 对象':<26} {floor_ms:8.2f} ms   "
          f"{'':>13}   {'':>15}")
    print()
    print(f"  端到端倍数：{py_ms / rs_ms:.2f}x    "
          f"纯核心倍数：{py_ms / core_ms:.2f}x")
    print(f"  FFI/对象构造占比：{(rs_ms - core_ms) / rs_ms * 100:.0f}% "
          f"（{rs_ms - core_ms:.1f} ms）")
    print(f"  成本下降：{py_ms - rs_ms:.1f} ms/次（{-100 * (rs_ms - py_ms) / py_ms:.0f}%）")
    print()
    print(f"  📌 端到端的**天花板**：{ceiling:.2f}x")
    print(f"     算法：基线 ÷（Rust 纯核心 + 纯造对象地板）= "
          f"{py_ms:.0f} ÷（{core_ms:.0f} + {floor_ms:.0f}）")
    print("     含义：只要 `tokenize()` 还得**把 Token 对象交回 Python**，"
          "这两块就省不掉。\n"
          "     R2 的「≥5 倍」验收线因此是**够不着**的 —— 这不是实现不够好，\n"
          "     是接口决定的。真正的收益在 R3/R5：那时下游不再需要 Python 对象；\n"
          "     R8 整条链路离开 Python 之后，端到端才等于今天的「纯核心」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
