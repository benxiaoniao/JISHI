# -*- coding: utf-8 -*-
"""遍历中改容器 —— 三族**都修好了**，这里全是**正向**断言（不许退回去）。

背景与实测见 `docs/设计决策.md` D77。要点：

* **列表**：可批量预取的四种类型（列表 / 元组 / 范围 / 文本）里**只有列表可变**，
  所以「预取超前一截」的危险只落在它身上。Rust 的 VM 与树遍历参考执行器改成
  「容器 + 下标」的活迭代（D77 §一）；C VM 预取超前一截，靠「**变更即失效**」
  兜住（D77 §二，ABI v2）：宿主按**对象身份**发现「被遍历的容器被改了」，
  让 C 侧丢缓冲，下次取批从**已交付计数**那个绝对下标重读。
* **集合 / 字典**：规范侧曾把 Python 的裸 `RuntimeError` 漏给用户；现在统一成
  一条带码（E2000）、带行列、带修法提示的基石错误，六个实现逐字一致。

⚠️ **别只测「移除」**：`排序` / `反转` / `下标赋值` **不改长度**，却一样会让
预取缓冲作废 —— 只测删元素会漏掉它们（见 `_LIST_CASES`）。
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
    from oracle.jishi.interpreter import run_source

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run_source(src, "x.jsh")
        return buf.getvalue(), None
    except BaseException as e:                                   # noqa: BLE001
        return buf.getvalue(), e


def _rust_vm(src: str) -> str:
    from oracle.jishi.compiler import compile_to_json

    tmp = Path(__file__).resolve().parents[1] / "build" / f"_m83_{os.getpid()}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    r = subprocess.run([str(_rust()), "--load-bytecode", str(tmp)], capture_output=True, input=b"")
    return r.stdout.decode("utf-8", "replace")


def _cvm(src: str):
    """C VM：返回 `(打印出来的文本, 抛出来的异常或 None)`。"""
    from oracle.jishi import cvm_bind

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
    from oracle.jishi.compiler import compile_to_json

    tmp = ROOT / "build" / f"_m83_big_{os.getpid()}.json"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    t0 = time.perf_counter()
    r = subprocess.run([str(_rust()), "--load-bytecode", str(tmp)], capture_output=True, input=b"")
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
    from oracle.jishi.compiler import compile_to_json

    for src, want in ((_SET_ADD, "1\n"), (_DICT_ADD, "甲\n")):
        p = ROOT / "build" / f"_m83_guard_{os.getpid()}.json"
        p.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
        r = subprocess.run([str(_rust()), "--load-bytecode", str(p)], capture_output=True, input=b"")
        out = r.stdout.decode("utf-8", "replace")
        err = r.stderr.decode("utf-8", "replace")
        assert out == want, f"stdout 不一致：{out!r}"
        assert _MSG in err and "E2000" in err, f"错误渲染不一致：{err[:200]!r}"


# ---------------------------------------------------------------------------
# 三、C VM 的**列表**批量预取：**已修**（B2「变更即失效」，正向断言）
# ---------------------------------------------------------------------------
#
# ⚠️ 五种改法都要钉：`移除`/`追加`/`清空` 会改长度，而 `排序`/`反转`/`下标赋值`
# **不改长度**却照样让缓冲作废 —— 只测前三种会漏掉后面那一类。

_LIST_CASES = (
    ("追加", _ADD, "1\n2\n99\n"),
    ("移除", _DEL, "1\n3\n"),
    ("清空", '令 列 = [1, 2, 3]\n遍历 x 在 列：\n    打印(x)\n'
             '    如果 x == 1：\n        列.清空()\n', "1\n"),
    ("排序", '令 列 = [3, 1, 2]\n遍历 x 在 列：\n    打印(x)\n'
             '    如果 x == 3：\n        列.排序()\n', "3\n2\n3\n"),
    ("反转", '令 列 = [1, 2, 3]\n遍历 x 在 列：\n    打印(x)\n'
             '    如果 x == 1：\n        列.反转()\n', "1\n2\n1\n"),
    ("下标赋值", '令 列 = [1, 2, 3]\n遍历 x 在 列：\n    打印(x)\n'
                 '    如果 x == 1：\n        列[1] = 99\n', "1\n99\n3\n"),
    ("边遍历边删偶数", '令 列 = [1, 2, 3, 4, 5, 6, 7, 8]\n遍历 x 在 列：\n'
                 '    如果 x % 2 == 0：\n        列.移除(x)\n    否则：\n'
                 '        打印(x)\n打印(列)\n',
     # 📌 只打印出 1 —— 下标一直在前进，而每删一次就把后面一个偶数顶到当前
     # 下标上，于是「偶数全删掉、奇数一个都没轮到」。这正是「按下标活迭代」
     # 的样子（快照实现会给出一串 1 3 5 7）。
     "1\n[1, 3, 5, 7]\n"),
)


#: 「容器不是从变量直接拿来」的那一族：`对象.属性` / `列[0]` / `字["键"]` 每次
#: 求值都会拿到**新句柄号**（宿主 `py2c` 不复用），所以这几条专治「按号认容器」。
_ALIAS_CASES = (
    ("属性里的列表",
     '类 袋：\n    函数 初始化(自身)：\n        自身.项 = [1, 2, 3]\n'
     '令 对 = 新建 袋()\n遍历 x 在 对.项：\n    打印(x)\n'
     '    如果 x == 1：\n        对.项.移除(2)\n', "1\n3\n"),
    ("经方法改属性列表",
     '类 袋：\n    函数 初始化(自身)：\n        自身.项 = [1, 2, 3]\n'
     '    函数 去(自身, v)：\n        自身.项.移除(v)\n'
     '令 对 = 新建 袋()\n遍历 x 在 对.项：\n    打印(x)\n'
     '    如果 x == 1：\n        对.去(2)\n', "1\n3\n"),
    ("字典下标里的列表",
     '令 字 = {"列": [1, 2, 3]}\n遍历 x 在 字["列"]：\n    打印(x)\n'
     '    如果 x == 1：\n        字["列"].移除(2)\n', "1\n3\n"),
    ("绑定方法改列表",
     '令 列 = [1, 2, 3]\n令 去 = 列.移除\n遍历 x 在 列：\n    打印(x)\n'
     '    如果 x == 1：\n        去(2)\n', "1\n3\n"),
    ("嵌套遍历内层删",
     '令 外 = [[1, 2, 3], [4, 5, 6]]\n遍历 行 在 外：\n    遍历 x 在 行：\n'
     '        打印(x)\n        如果 x == 1：\n            行.移除(2)\n',
     "1\n3\n4\n5\n6\n"),
)


@pytest.mark.parametrize("name,src,want", _LIST_CASES,
                         ids=[c[0] for c in _LIST_CASES])
def test_CVM遍历中改列表_与规范逐字一致(name, src, want):
    """C VM 的这一族改法都要与规范（树遍历）**逐字一致**。

    这几条曾被「批量预取超前一截」吞掉（删掉的元素还吐出来 / 改了看不见）。
    修法是「变更即失效」，见 `docs/设计决策.md` D77 §二。
    """
    want_tree, exc = _tree(src)
    assert exc is None, f"{name}：规范侧（树遍历）报错了？{exc!r}"
    assert want_tree == want, f"{name}：规范侧输出与写死值不符（口径变了？）{want_tree!r}"
    got, cexc = _cvm(src)
    assert cexc is None, f"{name}：C VM 报错了 {cexc!r}"
    assert got == want, (
        f"{name}：C VM 给 {got!r}，规范给 {want!r} —— 预取缓冲没被"
        f"「变更即失效」丢掉？（D77 §二）")


@pytest.mark.parametrize("name,src,want", _ALIAS_CASES,
                         ids=[c[0] for c in _ALIAS_CASES])
def test_CVM经别名改被遍历的列表(name, src, want):
    """⚠️ **B2 的关键判据**：宿主每次 `py2c` 都发**新句柄号**，所以
    `对象.属性` / `列[0]` 这类**重新求值**拿到的容器与 `遍历` 那一刻**号不同**
    —— 按句柄号认容器会**漏判**（静默算错）。这几条走的就是那些路，
    它们逼着判定按**对象身份**做（见 `cvm_bind._iter_register` 的说明）。
    """
    want_tree, exc = _tree(src)
    assert exc is None, f"{name}：规范侧报错了？{exc!r}"
    assert want_tree == want, f"{name}：规范侧输出变了？{want_tree!r}"
    got, cexc = _cvm(src)
    assert cexc is None, f"{name}：C VM 报错了 {cexc!r}"
    assert got == want, f"{name}：C VM 给 {got!r}，规范给 {want!r}（D77 §二）"
