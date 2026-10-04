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
# 二、未修：集合 / 字典（登记，会过期报红）
# ---------------------------------------------------------------------------


def test_登记_规范侧漏出裸RuntimeError():
    """⚠️ **登记**：集合 / 字典在遍历中被改大小，规范侧把 Python 的 `RuntimeError`
    **原样漏给用户**（英文原文、**没有错误码、没有行列**）—— 违反「报错必须可操作」。

    **修好之后本测试会红**：那时请删掉这条登记、改成「报出基石错误（带码 + 行列）」的
    正向断言，并更新 `docs/设计决策.md` D77 §二。
    """
    for src in (_SET_ADD, _DICT_ADD):
        _, exc = _tree(src)
        assert exc is not None, f"规范侧居然没报错？{src!r}"
        assert type(exc).__name__ == "RuntimeError", (
            f"规范侧不再漏裸异常了（现在是 {type(exc).__name__}）→ 登记过期，请改成正向断言")
        assert "changed size during iteration" in str(exc)


@_need_rust
def test_登记_CVM批量预取吞掉改容器():
    """⚠️ **登记**：C VM 的 `FOR_ITER` 走**批量预取**（`iter_next_batch`），
    批次一取就比宿主迭代器走得远 —— 于是

    * 「遍历中删掉元素」它**还会把删掉的那个吐出来**（列表：给 1 2 3，规范给 1 3）；
    * 「字典改大小」的报错**被整个吞掉**（批次把 2 个键全覆盖，之后不再问宿主）；
    * 「集合改大小」虽然报错，但**把内部字样露给用户**（「内部错误（宿主回调）…」）。

    **修好之后本测红**：那时删登记、改正向断言，并更新 D77 §二第 2、3 条。
    """
    # 列表删除：规范给 1 3，C VM 给 1 2 3（被删的 2 还在批次里）
    assert _cvm(_DEL) == ("1\n2\n3\n", None), (
        "C VM 不再预取到「超出已消费」的位置了 → 登记过期，请改成正向断言")
    # 字典改大小：规范抛错，C VM 静默跑完（错误被批次吞掉）
    assert _cvm(_DICT_ADD) == ("甲\n乙\n", None), (
        "C VM 不再吞掉「字典改大小」了 → 登记过期，请改成正向断言")
    # 集合改大小：C VM 报的错把**内部字样**露出来了
    _, exc = _cvm(_SET_ADD)
    assert exc is not None and "内部错误（宿主回调）" in str(exc), (
        "C VM 的报错不再露内部字样了 → 登记过期，请改成正向断言")
