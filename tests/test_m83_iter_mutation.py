# -*- coding: utf-8 -*-
"""遍历中改容器 —— **两半**：已修的一半钉正向，未修的一半登记（会过期报红）。

背景与实测见 `docs/设计决策.md` D77。要点：

* **列表**（增 / 删）：**已修**（2026-10-05，D77 §一）。Rust 的 VM 与树遍历参考
  执行器都从「反转快照 + pop」改成「容器 + 下标」的**活迭代**，与规范（Python 的
  `list_iterator` 是按下标活迭代）一致。这两条是**正向**断言，不许退回去。
* **集合 / 字典**（遍历中改大小）：**未修，登记**。规范侧（树遍历 / Python VM）
  把 Python 的 `RuntimeError` **原样漏给用户**（英文、无码、无行列）；C VM 的
  **批量预取**把这件事**整个吞掉**（还能继续吐出已被删掉的元素）。谁把这三处
  修了（包装成基石错误 + 收拾 C VM 的批量），**本文件的登记测试会红** ——
  那时请删掉登记、改成正向断言，并更新 D77。
"""

from __future__ import annotations

import functools
import io
import contextlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_ADD = '令 列 = [1, 2]\n遍历 x 在 列：\n    打印(x)\n    如果 x == 1：\n        列.追加(99)\n'
_DEL = '令 列 = [1, 2, 3]\n遍历 x 在 列：\n    打印(x)\n    如果 x == 1：\n        列.移除(2)\n'
_SET_ADD = '令 集 = {1, 2}\n遍历 x 在 集：\n    打印(x)\n    如果 x == 1：\n        集.添加(99)\n'
_DICT_ADD = '令 字 = {"甲": 1, "乙": 2}\n遍历 k 在 字：\n    打印(k)\n    如果 k == "甲"：\n        字["丙"] = 3\n'


def _rust() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _need_rust(fn):
    """没构建 Rust 宿主就跳过（与其它 walk/host 测试同款装饰器）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _rust().exists():
            pytest.skip("Rust 宿主未构建（cd rust && cargo build --release）")
        return fn(*args, **kwargs)
    return wrapper


def _tree(src: str):
    """树遍历（= 规范）。返回 `(打印出来的文本, 抛出来的异常或 None)`。"""
    from jishi.interpreter import run_source

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run_source(src, "x.jsh")
        return buf.getvalue(), None
    except BaseException as e:                                   # noqa: BLE001
        return buf.getvalue(), e


def _rust_vm(src: str) -> str:
    from jishi.compiler import compile_to_json

    tmp = Path(__file__).resolve().parents[1] / "build" / f"_m83_{os.getpid()}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    r = subprocess.run([str(_rust()), str(tmp)], capture_output=True, input=b"")
    return r.stdout.decode("utf-8", "replace")


def _cvm(src: str):
    """C VM：返回 `(打印出来的文本, 抛出来的异常或 None)`。"""
    from jishi import cvm_bind

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cvm_bind.run_source_c(src, filename="x.jsh")
        return buf.getvalue(), None
    except BaseException as e:                                   # noqa: BLE001
        return buf.getvalue(), e


# ---------------------------------------------------------------------------
# 一、已修：列表的活迭代（正向断言）
# ---------------------------------------------------------------------------


@_need_rust
def test_列表遍历中追加_规范与Rust一致():
    """`列.追加(99)` —— 规范会**看到**新加的元素（活迭代），Rust 也得看到。"""
    want = "1\n2\n99\n"
    assert _tree(_ADD)[0] == want
    assert _rust_vm(_ADD) == want, "Rust VM 退回快照了？见 D77 §一"


@_need_rust
def test_列表遍历中删除_规范与Rust一致():
    """`列.移除(2)` —— 规范会**跳过**被顶上来的那个（index 已前进），Rust 同。"""
    want = "1\n3\n"
    assert _tree(_DEL)[0] == want
    assert _rust_vm(_DEL) == want, "Rust VM 退回快照了？见 D77 §一"


@_need_rust
def test_活迭代没有退回O_n平方():
    """活迭代是「下标 + 每步查 len」——**每步 O(1)**，不许变成 `remove(0)` 那种 O(n²)。"""
    import time

    n = 20000
    src = (f"令 列 = [i 遍历 i 在 范围({n})]\n令 s = 0\n"
           f"遍历 x 在 列：\n    s = s + x\n打印(s)\n")
    from jishi.compiler import compile_to_json

    tmp = ROOT / "build" / f"_m83_big_{os.getpid()}.json"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    t0 = time.perf_counter()
    r = subprocess.run([str(_rust()), str(tmp)], capture_output=True, input=b"")
    dt = time.perf_counter() - t0
    assert r.stdout.decode("utf-8", "replace").strip() == str(n * (n - 1) // 2)
    assert dt < 1.0, f"2 万元素遍历用了 {dt:.2f}s —— 像退回 O(n²) 了"


# ---------------------------------------------------------------------------
# 二、集合 / 字典：**已修**（五执行器一致，正向断言）
# ---------------------------------------------------------------------------

#: 与四个引擎共用的一条文案（Python `errors.iter_size_changed` / Rust
#: `ITER_SIZE_CHANGED` / Node `ITER_SIZE_CHANGED`）。
_MSG = "遍历的过程中改了容器的大小（这次遍历没法继续了）"


def test_集合遍历中改大小_树遍历报基石错误():
    """规范侧（树遍历）不再把 Python 的裸 `RuntimeError` 漏给用户（D77 §二）。"""
    out, exc = _tree(_SET_ADD)
    assert out == "1\n", out
    assert exc is not None and _MSG in str(exc), exc
    assert getattr(exc, "code", None) == "E2000", f"要带错误码：{exc!r}"


def test_字典遍历中改大小_树遍历报基石错误():
    out, exc = _tree(_DICT_ADD)
    assert out == "甲\n", out
    assert exc is not None and _MSG in str(exc), exc
    assert getattr(exc, "code", None) == "E2000", f"要带错误码：{exc!r}"


@_need_rust
def test_集合与字典_Rust侧也报同一条():
    """Rust VM 与 Rust 树遍历参考执行器都要报**同一条**（快照 + 大小护栏）。"""
    from jishi.compiler import compile_to_json

    for src, want in ((_SET_ADD, "1\n"), (_DICT_ADD, "甲\n")):
        p = ROOT / "build" / f"_m83_guard_{os.getpid()}.json"
        p.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
        r = subprocess.run([str(_rust()), str(p)], capture_output=True, input=b"")
        out = r.stdout.decode("utf-8", "replace")
        err = r.stderr.decode("utf-8", "replace")
        assert out == want, f"stdout 不一致：{out!r}"
        assert _MSG in err and "E2000" in err, f"错误渲染不一致：{err[:200]!r}"


# ---------------------------------------------------------------------------
# 三、仍未修：C VM 的**列表**批量预取（登记，会过期报红）
# ---------------------------------------------------------------------------


@_need_rust
def test_登记_CVM批量预取让列表删除看不到():
    """⚠️ **登记**：C VM 的 `FOR_ITER` 走**批量预取**（`iter_next_batch`，一次 256 个），
    批次一取就比宿主迭代器走得远 —— 所以「遍历中删掉的元素**还会被吐出来**」
    （列表：C VM 给 1 2 3，其余五个给 1 3）。

    📌 为什么单独留这一条：它**只与批量有关**（集合/字典那两条已经修掉了 —— 它们
    不走批量）。取舍与两条候选方案见 `docs/设计决策.md` D77 §二：
    「放弃批量」实测要付 **1.81x**（不可接受）→ 已选定 **B2（宿主可寻址批次 +
    变更即失效）**，待实施。

    **修好之后本测红**：那时请删掉这条登记、改成正向断言，并把 D77 §二 标成已交付。
    """
    from jishi.compiler import compile_to_json

    p = ROOT / "build" / f"_m83_cvm_{os.getpid()}.json"
    p.write_text(compile_to_json(_DEL, "x.jsh"), encoding="utf-8")
    r = subprocess.run([str(_rust()), str(p)], capture_output=True, input=b"")
    del r  # Rust 侧本来就是对的；这里只是提醒「RustVM 这一半没问题」
    assert _cvm(_DEL) == ("1\n2\n3\n", None), (
        "C VM 不再预取到「超出已消费」的位置了 → 登记过期（B2 已实施？），"
        "请改成「1 3」的正向断言并更新 D77 §二")
