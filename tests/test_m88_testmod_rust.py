# -*- coding: utf-8 -*-
"""`测试` 模块搬进 Rust 宿主（2026-10-05）—— 两套判据。

## 背景

`测试` 是 Python 侧独有的自检模块，宿主以前**没有**（挂在 m51 的
`HOST_EXEMPT` 里）。用户指出：**Rust 最终要替代 Python，这个模块 Rust 也要有**。
于是：宿主补了 `stdlib/testmod.rs`（11 个函数）+ 它需要的那点宿主能力
（往 **stderr** 写 —— `测试.汇总` 的失败明细走 stderr、汇总行走 stdout）。

## 两套判据（都要有）

* **`test_漂移_*`**：与 Python CLI 逐字对拍（stdout / stderr / 退出码）。
  抓漂移最有效，但**前提是 Python 还在**。
* **`test_独立判据_*`**：写死的期望值，只跑宿主二进制。
  **R8 之后 Python 退场，这一半仍然要绿。**

## 已知边界（如实登记，别当没有）

1. `用例` 的**返回值**：Python 返回的记录 dict 里 `失败` 是 **Python 元组**的列表；
   宿主的 `Val` 没有元组类型，用**二元列表**代替（`test_已知边界_*` 把宿主这边
   钉住，免得悄悄变成别的东西）。
2. 「用例里出错」的**摘要文案**：Python 会把原生异常（如 `TypeError`）的类名与
   英文消息带进摘要，宿主一律给中文基石错误。两者都是「这个用例错了」。
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 全过的那条路（用例里的断言一个都不失败）
_PROG_OK = """导入 测试

函数 加法()：
    测试.相等(1 + 1, 2)
    测试.为真(1 < 2)

测试.用例("加法", 加法)
测试.汇总()
"""

#: 有失败 + 有用例崩了的那条路（走 stderr + 汇总抛错 → 退出码 1）
_PROG_BAD = """导入 测试

函数 有失败()：
    测试.相等(1, 2, "加法错了")
    测试.为假(真)

测试.用例("有失败", 有失败)
测试.汇总()
"""


def _rust() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _need_rust(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _rust().exists():
            pytest.skip("Rust 宿主未构建（cd rust && cargo build --release）")
        return fn(*args, **kwargs)
    return wrapper


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    return env


def _run_rust(prog_json: Path):
    r = subprocess.run([str(_rust()), "--load-bytecode", str(prog_json)], capture_output=True,
                       input=b"", env=_env(), cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _run_py(prog: Path):
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(prog)],
                       capture_output=True, input=b"", env=_env(), cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _compile(src: str, tag: str) -> tuple[Path, Path]:
    """写 .jsh + 编成字节码 JSON（都带 pid，并行安全）。"""
    d = ROOT / "build" / f"_m88_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    jsh = d / f"{tag}.jsh"
    jsh.write_text(src, encoding="utf-8")
    js = d / f"{tag}.json"
    sys.path.insert(0, str(ROOT))
    from oracle.jishi.compiler import compile_to_json
    js.write_text(compile_to_json(src, str(jsh)), encoding="utf-8")
    return jsh, js


# ---------------------------------------------------------------------------
# 一、与 Python 对拍
# ---------------------------------------------------------------------------


@_need_rust
def test_漂移_全过的那条路():
    jsh, js = _compile(_PROG_OK, "ok")
    rs, py = _run_rust(js), _run_py(jsh)
    assert rs == py, f"stdout/stderr/退出码 有差异：\n  Rust: {rs!r}\n  Py  : {py!r}"
    assert rs[2] == 0 and "✅ 全部通过" in rs[0]


@_need_rust
def test_漂移_有失败的那条路():
    """失败明细走 stderr、汇总行走 stdout、汇总抛错 → 退出码 1。"""
    jsh, js = _compile(_PROG_BAD, "bad")
    rs, py = _run_rust(js), _run_py(jsh)
    assert rs == py, f"stdout/stderr/退出码 有差异：\n  Rust: {rs!r}\n  Py  : {py!r}"
    assert rs[2] == 1 and "❌" in rs[0] and "✗" in rs[1]


@_need_rust
def test_漂移_统计与重置():
    src = ('导入 测试\n\n'
           '函数 甲()：\n    测试.相等(1, 1)\n'
           '函数 乙()：\n    测试.相等(1, 2)\n\n'
           '测试.用例("甲", 甲)\n测试.用例("乙", 乙)\n'
           '打印(测试.统计())\n测试.重置()\n打印(测试.统计())\n')
    jsh, js = _compile(src, "stats")
    rs, py = _run_rust(js), _run_py(jsh)
    assert rs == py, f"有差异：\n  Rust: {rs!r}\n  Py  : {py!r}"
    assert rs[2] == 0


# ---------------------------------------------------------------------------
# 二、独立判据（**不依赖 Python**，Python 退场后仍要绿）
# ---------------------------------------------------------------------------


@_need_rust
def test_独立判据_全过的输出是写死的():
    _, js = _compile(_PROG_OK, "ok2")
    out, err, code = _run_rust(js)
    assert out == "✅ 全部通过 （1 个用例）\n", out
    assert err == "" and code == 0


@_need_rust
def test_独立判据_失败的输出是写死的():
    _, js = _compile(_PROG_BAD, "bad2")
    out, err, code = _run_rust(js)
    assert out == "❌ 通过 0 / 失败 2 / 出错 0 （1 个用例）\n", out
    # 失败明细在 **stderr**，一行一条（`✗ 用例名 · 说明：详情`）
    assert err.startswith(
        "✗ 有失败 · 加法错了：期望 2，实际 1\n"
        "✗ 有失败 · 为假：期望为假，实际是 真\n"), err
    # 汇总抛断言错误 → CLI 退码 1
    assert "错误 E2008：断言没通过（测试没全过：失败 2 项、出错 0 项）" in err
    assert code == 1


@_need_rust
def test_独立判据_宿主自报家底里有测试模块():
    """`--dump-stdlib` 是宿主自报家底（m51 用它当事实源）—— `测试` 得在里面，
    且 11 个函数一个不少。"""
    r = subprocess.run([str(_rust()), "--dump-stdlib"], capture_output=True,
                       input=b"", env=_env(), cwd=str(ROOT))
    assert r.returncode == 0
    table = json.loads(r.stdout.decode("utf-8"))
    assert "测试" in table, f"宿主家底里没有 `测试`：{sorted(table)}"
    assert sorted(table["测试"]) == sorted([
        "用例", "相等", "不等", "为真", "为假", "近似", "包含",
        "抛出", "汇总", "重置", "统计"]), table["测试"]


@_need_rust
def test_独立判据_抛出返回的是异常对象():
    src = ('导入 测试\n'
           '令 异 = 测试.抛出(函数()：1 / 0)\n'
           '打印(类型(异))\n')
    _, js = _compile(src, "throw")
    out, err, code = _run_rust(js)
    assert err == "" and code == 0
    # ⚠️ `类型(异常对象)` 给的是「异常」（四引擎一致）—— 不是具体类型名。
    # 具体类型在 `e.类型` 上。
    assert out == "异常\n", out


@_need_rust
def test_已知边界_用例返回值里是二元列表():
    """**已知边界**（见文件头）：Python 的 `失败` 里是元组，宿主用二元列表。

    这条钉的是**宿主自己的行为**（不是与 Python 比）—— 免得哪天悄悄变成
    别的东西。Python 侧那半边在 `test_m41_stdlib.py` 里。
    """
    src = ('导入 测试\n'
           '函数 甲()：\n    测试.相等(1, 2)\n'
           '令 条 = 测试.用例("甲", 甲)\n'
           '打印(条["失败"])\n')
    _, js = _compile(src, "ret")
    out, err, code = _run_rust(js)
    assert err == "" and code == 0
    # 嵌套在容器里的文本按 `repr` 走 —— 单引号（与 Python 侧同形）
    assert out == "[[\'相等\', \'期望 2，实际 1\']]\n", out
