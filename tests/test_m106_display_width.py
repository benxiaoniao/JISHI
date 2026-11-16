# -*- coding: utf-8 -*-
"""M106：文本对齐按**显示宽度**（一个汉字算 2 列）。

台账 R003 的现场：`文本.左对齐("中文", 8)` 按**字符数**补空格，得到「中文」+ 6 个空格
—— 显示宽度是 **10**、不是 8 ⇒ 中文表格必然歪。

这条判据钉三件事：

1. `显示宽度` 的算法（汉字/全角/emoji 算 2，半角算 1，组合符算 0）；
2. 四个对齐函数按显示宽度补齐；
3. 🔴 **ASCII 的行为与改前逐字节一致** —— 英文的显示宽度就是字符数，
   这条改动**不该**让任何纯英文输出变一个字节。
"""

import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi.interpreter import run_source  # noqa: E402


def _跑(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 一、显示宽度怎么算
# ---------------------------------------------------------------------------

def test_显示宽度_汉字算两列半角算一列():
    out = _跑('导入 文本\n打印(文本.显示宽度("中文"), 文本.显示宽度("abc"), '
              '文本.显示宽度(""))\n')
    assert out.strip() == "4 3 0", out


def test_显示宽度_全角标点与emoji():
    out = _跑('导入 文本\n打印(文本.显示宽度("，。"), 文本.显示宽度("😀"))\n')
    assert out.strip() == "4 2", out


def test_显示宽度_组合符不占列():
    """组合符（如「e + 重音」）不占列 —— 直接调函数（.jsh 里写 `\\u` 转义不划算）。"""
    from oracle.jishi.stdlib.文本 import 显示宽度

    assert 显示宽度("e\u0301") == 1
    assert 显示宽度("中文") == 4
    assert 显示宽度("Ａ１") == 4          # 全角字母 / 全角数字
    assert 显示宽度("·") == 1              # U+00B7 是 Ambiguous 类 —— 按 1 列算（表的边界）


# ---------------------------------------------------------------------------
# 二、对齐按显示宽度
# ---------------------------------------------------------------------------

def test_左对齐_按显示宽度():
    out = _跑('导入 文本\n打印("[" + 文本.左对齐("中文", 8) + "]")\n')
    assert out.strip() == "[中文    ]", out          # 4 列 + 4 空格 = 8 列


def test_右对齐_按显示宽度():
    out = _跑('导入 文本\n打印("[" + 文本.右对齐("中文", 8) + "]")\n')
    assert out.strip() == "[    中文]", out


def test_居中与填充_按显示宽度():
    out = _跑('导入 文本\n打印("[" + 文本.居中("中文", 9) + "]", '
              '"[" + 文本.填充("中文", 9, "-") + "]")\n')
    assert out.strip() == "[   中文  ] [---中文--]", out


def test_表格真的对齐了():
    """这条是「为什么值得改」的验收：中文列与英文列**显示宽度**一致。"""
    src = (
        '导入 文本\n'
        '打印(文本.左对齐("名字", 10) + 文本.右对齐("分数", 6))\n'
        '打印(文本.左对齐("小明", 10) + 文本.右对齐("92", 6))\n'
    )
    out = _跑(src)
    行们 = out.splitlines()
    from oracle.jishi.stdlib.文本 import 显示宽度

    assert 显示宽度(行们[0]) == 显示宽度(行们[1]) == 16, out


# ---------------------------------------------------------------------------
# 三、🔴 ASCII 不许变（含「超宽原样返回」与 CPython 的居中偏置）
# ---------------------------------------------------------------------------

def test_ASCII_与改前逐字节一致():
    src = (
        '导入 文本\n'
        '打印("[" + 文本.左对齐("ab", 5) + "]", "[" + 文本.右对齐("ab", 5) + "]")\n'
        '打印("[" + 文本.居中("ab", 5) + "]", "[" + 文本.居中("abc", 6) + "]", '
        '"[" + 文本.居中("a", 4, "*") + "]")\n'
        '打印("[" + 文本.左对齐("abcd", 2) + "]")\n'
        '打印("[" + 文本.填充("abc", 7, "-") + "]")\n'
    )
    want = (
        "[ab   ] [   ab]\n"
        "[  ab ] [ abc  ] [*a**]\n"
        "[abcd]\n"                      # 超宽 → 原样返回
        "[--abc--]\n"
    )
    assert _跑(src) == want


def test_填充字符不合法要报错():
    from oracle.jishi.errors import RunTypeError
    from oracle.jishi.stdlib.文本 import 左对齐

    with pytest.raises(RunTypeError):
        左对齐("ab", 5, "--")


# ---------------------------------------------------------------------------
# 四、🔴 三处实现必须一致（Python 判据基准 / Rust 宿主 / Node 宿主）
# ---------------------------------------------------------------------------

NODE = "node"
RUST_EXE = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")

#: 一段把「宽度计算 + 四种对齐 + 三个 ASCII 边界」都走到的脚本。
_对齐脚本 = (
    '导入 文本\n'
    '打印(文本.显示宽度("中文"), 文本.显示宽度("abc"), 文本.显示宽度("😀"))\n'
    '打印("[" + 文本.左对齐("中文", 8) + "]", "[" + 文本.右对齐("中文", 8) + "]")\n'
    '打印("[" + 文本.居中("中文", 9) + "]", "[" + 文本.填充("中文", 9, "-") + "]")\n'
    '打印("[" + 文本.左对齐("ab", 5, "*") + "]", "[" + 文本.居中("ab", 5) + "]", '
    '"[" + 文本.居中("a", 4, "*") + "]")\n'
    '打印("[" + 文本.左对齐("abcd", 2) + "]")'
)


def _字节码(src: str):
    """把源码编译成字节码 JSON（两个宿主都吃这个）。"""
    from conftest import tmp_bc_path
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    bc = tmp_bc_path(ROOT / "_tmp_m106_bc.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    return bc


def test_与_Rust_宿主一致():
    if not RUST_EXE.is_file():
        pytest.skip("没有 rust 宿主产物（先 `cd rust && cargo build --release`）")
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(_字节码(_对齐脚本))],
                       capture_output=True)
    got = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:400]
    assert got == _跑(_对齐脚本), (
        f"Rust 宿主的对齐与 Python 不一致：\nRust:   {got!r}\nPython: {_跑(_对齐脚本)!r}")


def test_与_Node_宿主一致():
    from shutil import which

    if which(NODE) is None:
        pytest.skip("环境里没有 node")
    r = subprocess.run([NODE, str(ROOT / "oracle" / "node" / "index.js"),
                        str(_字节码(_对齐脚本))],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:400]
    assert r.stdout == _跑(_对齐脚本), (
        f"Node 宿主的对齐与 Python 不一致：\nNode:   {r.stdout!r}\nPython: {_跑(_对齐脚本)!r}")
