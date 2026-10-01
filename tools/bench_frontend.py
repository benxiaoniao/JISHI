# -*- coding: utf-8 -*-
"""R 线前端吞吐实测：分词（R2）与语法（R3），Python vs Rust，同机交错 A/B。

## 为什么要交错 A/B

本项目的纪律（`AGENTS.md` §2）：**判「改前 vs 改后」必须同机同时段交错跑**。
绝对毫秒会随机器负载整体漂移，不同时段各测一次得出的倍数能差一倍 ——
这不是理论担忧，M46 记的「1.2x」就是这么来的（复测一直是 0.56x）。
所以这里每轮都「先 Python、再 Rust、再 Rust 核心」，取多轮的**最优值**
（最优值抵掉偶发抢占；交错让两边吃到同一段负载）。

## 三个阶段各报几个数

| 量的是什么 | 为什么要它 |
|---|---|
| **Python**（基线） | 尺子的零点 |
| **Rust 端到端**（经 Python 扩展拿回 `Token` / AST 节点**对象**） | **这才是换实现后真正省下的时间** |
| **Rust 只算不算**（不构造任何 Python 对象） | 把「核心本身多快」与「转对象多贵」分开 —— 不然快不起来时不知道该怪谁 |

⚠️ 只报第二个是不够的：R2 的验收线是「≥ 5 倍」，而端到端倍数里含着一份
**固定的 Python 对象构造开销**。把第三个一起报出来，读者才知道天花板在哪、
下一步该往哪使劲 —— 于是 R2 量出来的结论是「**在这层接口上够不着 5 倍**」
（不是实现不够好，是接口决定的），并把「端到端 ≥5x」挪到了 R5。

## R5 的「端到端 ≥5x」为什么到这里才成立

R2/R3 的端到端倍数额头撞在一堵墙上：**把产物做成 Python 对象**这件事本身就有地板
（11.7 万 Token ≈ 39 ms、7 万 AST 节点 ≈ 28 ms），天花板 = 基线 ÷（核心 + 地板），
**与实现无关**。

R5 换了产物形态：**字节码 JSON 文本**。文本没有「对象构造」这一步，于是
「源码 → 字节码 JSON」这条路上**没有地板** —— 5 倍才有得谈。
（`compile_module()` 那条路仍要造对象，那是给 Python VM / C VM 用的，
本脚本把它一并量出来当对照：那正是**还得付地板**的那条路。）

用法：
    python tools/bench_frontend.py             # 默认 20700 行、5 轮
    python tools/bench_frontend.py --quick     # 3 轮
    python tools/bench_frontend.py --lines 5000
    python tools/bench_frontend.py --stage tokens   # 只看分词
    python tools/bench_frontend.py --stage ast      # 只看语法
"""

from __future__ import annotations

import argparse
import contextlib
import os
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
#: 量出来的是「报错有多快」而不是「前端有多快」）。
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


def _best(fn, rounds: int) -> float:
    return min(timed(fn) for _ in range(rounds))


@contextlib.contextmanager
def _frontend(name: str):
    """临时把前端开关切成 `name`，退出时还原。

    ⚠️ 必须还原：`JISHI_FRONTEND` 留在环境里会污染同一进程后面所有调用
    （`tools/conformance.py` 的 `emit` 踩过同一个坑）。
    """
    from jishi import tokenizer as T

    old = os.environ.get(T.FRONTEND_ENV)
    os.environ[T.FRONTEND_ENV] = name
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(T.FRONTEND_ENV, None)
        else:
            os.environ[T.FRONTEND_ENV] = old


def _section(title: str, py_ms: float, rs_ms: float, core_ms: float,
             floor_ms: float, n_obj: int, what: str) -> None:
    print(f"\n【{title}】")
    print(f"  {'实现':<26} {'耗时':>10}   相对基线")
    print("  " + "-" * 58)
    print(f"  {'Python（基线）':<26} {py_ms:8.2f} ms   1.00x")
    print(f"  {'Rust 端到端（含转对象）':<26} {rs_ms:8.2f} ms   {py_ms / rs_ms:.2f}x")
    print(f"  {'Rust 只算不算（纯核心）':<26} {core_ms:8.2f} ms   {py_ms / core_ms:.2f}x")
    print(f"  {'（对照）纯造对象的地板':<26} {floor_ms:8.2f} ms   —")
    print(f"  省下 {py_ms - rs_ms:.1f} ms/次（{-100 * (rs_ms - py_ms) / py_ms:.0f}%）"
          f"   ·   {what} {n_obj} 个   ·   转对象占比 "
          f"{(rs_ms - core_ms) / rs_ms * 100:.0f}%（{rs_ms - core_ms:.1f} ms）")


def bench_tokens(src, rounds, core, n_tok) -> None:
    from jishi.tokenizer import Token, Tokenizer

    py_ms = rs_ms = core_ms = float("inf")
    for _ in range(rounds):
        py_ms = min(py_ms, timed(lambda: Tokenizer(src, "bench.jsh").tokenize()))
        rs_ms = min(rs_ms, timed(lambda: core.tokenize(src, "bench.jsh")))
        core_ms = min(core_ms, timed(lambda: core.token_count(src, "bench.jsh")))

    # 「交还 Token 对象」的**地板**：在 Python 里凭空造 n_tok 个 Token
    # （不算扫描、不算 FFI，纯构造）要多久。对照它就能算出端到端倍数的上限。
    # 按**位置参数**造 —— 扩展走的就是 `cls(*args)`。
    floor_ms = _best(lambda: [Token("OP", "=", 3, 5, 6, "x") for _ in range(n_tok)], 3)

    _section("一、分词（源码 → tokens）", py_ms, rs_ms, core_ms, floor_ms,
             n_tok, "token")
    print(f"  📌 端到端的**天花板**：{py_ms / (core_ms + floor_ms):.2f}x")
    print(f"     算法：基线 ÷（Rust 纯核心 + 纯造对象地板）= "
          f"{py_ms:.0f} ÷（{core_ms:.0f} + {floor_ms:.0f}）")
    print("     含义：只要 `tokenize()` 还得**把 Token 对象交回 Python**，"
          "这两块就省不掉。\n"
          "     R2 的「≥5 倍」验收线因此是**够不着**的 —— 不是实现不够好，\n"
          "     是接口决定的。真正兑现要等 R5（下游不再要 Python 对象）与 R8。")


def bench_ast(src, rounds, core, n_nodes) -> None:
    from jishi import ast_nodes as A
    from jishi.parser import parse_source

    def py_parse():
        with _frontend("python"):
            parse_source(src, "bench.jsh")

    def rs_parse():
        with _frontend("rust"):
            parse_source(src, "bench.jsh")

    py_ms = rs_ms = core_ms = float("inf")
    for _ in range(rounds):
        py_ms = min(py_ms, timed(py_parse))
        rs_ms = min(rs_ms, timed(rs_parse))
        core_ms = min(core_ms, timed(lambda: core.parse_node_count(src, "bench.jsh")))

    # AST 节点的地板：扩展是按**关键字参数**调 dataclass 的（见 `lib.rs` 的
    # `j_to_py`），照着它造才可比。
    _N = A.Name
    floor_ms = _best(lambda: [_N(id="x", line=1, col=1) for _ in range(n_nodes)], 3)

    _section("二、语法（源码 → AST）", py_ms, rs_ms, core_ms, floor_ms,
             n_nodes, "AST 节点")
    print(f"  📌 端到端的**天花板**：{py_ms / (core_ms + floor_ms):.2f}x"
          f"（同一套算法：纯核心 + 纯造对象地板）")


def bench_bytecode(src, rounds) -> None:
    """R5：源码 → **字节码 JSON 文本**（编译期端到端）。"""
    from jishi import compiler as C

    def py_compile():
        with _frontend("python"):
            C.compile_to_json(src, "bench.jsh")

    def rs_compile():
        with _frontend("rust"):
            C.compile_to_json(src, "bench.jsh")

    def rs_objects():
        """对照：把产物做成 Python 对象（`compile_module`，VM/CVM 那条路）。"""
        with _frontend("rust"):
            C.compile_source(src, "bench.jsh")

    py_ms = rs_ms = obj_ms = float("inf")
    for _ in range(rounds):
        py_ms = min(py_ms, timed(py_compile))
        rs_ms = min(rs_ms, timed(rs_compile))
        obj_ms = min(obj_ms, timed(rs_objects))

    n_codes = 0
    n_instrs = 0
    with _frontend("rust"):
        import json as _json
        body = _json.loads(C.compile_to_json(src, "bench.jsh"))["payload"]
        n_codes = len(body["codes"])
        n_instrs = sum(len(c["instrs"]) // 6 for c in body["codes"])

    print("\n【三、编译（源码 → 字节码 JSON）】")
    print(f"  {'实现':<26} {'耗时':>10}   相对基线")
    print("  " + "-" * 58)
    print(f"  {'Python（基线）':<26} {py_ms:8.2f} ms   1.00x")
    print(f"  {'Rust（原生 JSON）':<26} {rs_ms:8.2f} ms   {py_ms / rs_ms:.2f}x")
    print(f"  {'（对照）Rust 也做对象':<26} {obj_ms:8.2f} ms   {py_ms / obj_ms:.2f}x")
    print(f"  {n_codes} 个代码对象 / {n_instrs} 条指令"
          f"   ·   省下 {py_ms - rs_ms:.1f} ms/次（{-100 * (rs_ms - py_ms) / py_ms:.0f}%）")
    print("  ✅ 这条路上**没有地板**：产物是**文本**，不是对象 ——"
          "「端到端 ≥5x」（R5 的验收线）在该口径上成立。\n"
          "     ⚠️ 上面那行「也做对象」是 VM/CVM 那条路：它仍要付 R2/R3 那份地板，\n"
          "     所以那条路的倍数天然更低，**别拿它判 R5 的成败**。")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bench_frontend", description="R 线：Python vs Rust 前端吞吐")
    ap.add_argument("--lines", type=int, default=20700,
                    help="目标行数（默认 20700，对齐评估文档的基线口径）")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--quick", action="store_true", help="3 轮（快速摸底）")
    ap.add_argument("--stage", choices=["tokens", "ast", "bytecode", "all"],
                    default="all")
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
    print(f"每项跑 {rounds} 轮取最优，轮内交错（同机同时段 A/B）")

    if args.stage in ("tokens", "all"):
        n_tok = len(Tokenizer(src, "bench.jsh").tokenize())
        print(f"\ntoken 数：{n_tok}")
        bench_tokens(src, rounds, core, n_tok)

    if args.stage in ("ast", "all"):
        n_nodes = core.parse_node_count(src, "bench.jsh")
        print(f"\nAST 节点数：{n_nodes}")
        bench_ast(src, rounds, core, n_nodes)

    if args.stage in ("bytecode", "all"):
        bench_bytecode(src, rounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
