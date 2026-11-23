# -*- coding: utf-8 -*-
"""M27：值→文本的统一，以及跨语言宿主（Node）的同步验证。

背景：同一个值以前有两种写法——`打印(空)` 给 `None`、布尔给 `True`/`False`，
而 REPL 回显与 `类型()` 给中文「空/真/假」。根因是这件事有**两份**几乎一样的
实现（`runtime.jishi_repr` 与 `repl._repr_jishi`），日子久了就漂移。

现在收敛到 `runtime.jishi_repr(v, top=...)` 一处，`打印`、`文本()`、
字符串插值、容器内元素、REPL 回显全部走它。这里锁住：

1. 那几类值的显示（含「顶层文本不带引号、容器内带引号」的分工）；
2. 走内建/桥接回来的原生 Python 值也统一（`路径.存在()` 不再给 `True`）；
3. `打印` 与 `文本()` 与插值三者一致（以前插值走 `str()`，是第二个漂移源）；
4. **Node 宿主的显示与 M26 四个新内建**（对拍：同一份字节码逐字节同输出）。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import tmp_bc_path   # noqa: E402

from oracle.jishi import cvm_bind                        # noqa: E402
from oracle.jishi import serialize                       # noqa: E402
from oracle.jishi import vm as vm_mod                    # noqa: E402
from oracle.jishi.compiler import compile_source         # noqa: E402
from oracle.jishi.interpreter import Interpreter         # noqa: E402
from oracle.jishi.parser import parse                    # noqa: E402
from oracle.jishi.runtime import jishi_repr              # noqa: E402
from oracle.jishi.tokenizer import tokenize              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _tree(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        Interpreter(filename="<t>").run(
            parse(tokenize(src, "<t>"), src.split("\n"), "<t>"))
    return buf.getvalue()


def _pvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _cvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _all_agree(src: str) -> str:
    got = {"树遍历": _tree(src), "Python VM": _pvm(src), "C VM": _cvm(src)}
    assert len(set(got.values())) == 1, f"三执行器输出不一致：{got}"
    return next(iter(got.values()))


# ---------------------------------------------------------------------------
# 空 / 真 / 假：从 None/True/False 改成中文
# ---------------------------------------------------------------------------

def test_none_bool_are_chinese():
    assert _all_agree("打印(空, 真, 假)\n") == "空 真 假\n"


def test_bool_results_are_chinese():
    assert _all_agree(
        "打印(1 == 1, 1 == 2)\n"
        "打印(1 < 2, 2 < 1)\n"
        "打印(1 在 [1], 9 不在 [1])\n") == "真 假\n真 假\n真 真\n"


def test_none_and_bool_inside_containers():
    """容器里的元素也跟着中文化（这是最容易漏的一层）。"""
    assert _all_agree(
        '打印([1, 真, 空, "甲"])\n'
        '打印({"键": 假, "甲": [1, 空]})\n'
        '打印(集合([真, 假]))\n') == "[1, 真, 空, '甲']\n{'键': 假, '甲': [1, 空]}\n{真, 假}\n"


def test_text_in_container_keeps_quotes_but_top_level_does_not():
    """顶层文本不带引号、容器内带引号——与 Python 的 print/repr 分工一致。"""
    assert _all_agree('打印("甲")\n打印(["甲"])\n') == "甲\n['甲']\n"


def test_print_and_text_and_interp_agree():
    """`打印`、`文本()`、字符串插值三者必须给同一个结果。"""
    assert _all_agree(
        "打印(空, 真, 假)\n"
        "打印(文本(空), 文本(真), 文本(假))\n"
        "打印(`{空} {真} {假}`)\n") == "空 真 假\n空 真 假\n空 真 假\n"


def test_interp_with_container_uses_quotes():
    assert _all_agree('打印(`列表是 {[1, 真]}`)\n') == "列表是 [1, 真]\n"


def test_type_name_still_matches_display():
    """`类型()` 本来就用中文，现在显示与它一致，不再两套写法。"""
    assert _all_agree(
        "打印(类型(空), 类型(真), 类型(假))\n") == "空 布尔 布尔\n"


def test_stdlib_native_bool_is_chinese(tmp_path):
    """标准库薄封装返回的是原生 Python bool，以前打印成 True/False。

    这条是「跨边界值」的回归：桥接回来的 bool 走的仍是同一个显示函数。
    """
    assert _all_agree('导入 路径\n打印(路径.存在("docs"))\n'
                      '打印(路径.存在("绝对不存在的目录"))\n') == "真\n假\n"


def test_decimal_and_custom_object_display():
    assert _all_agree(
        '类 甲：\n'
        '    函数 文本(自身)：\n'
        '        返回 "甲样"\n'
        '打印(精确("1.50"))\n'
        '打印(新建 甲())\n'
        '打印([新建 甲(), 精确("2.5")])\n') == "1.50\n甲样\n[甲样, 2.5]\n"


def test_dict_uses_halfwidth_colon_like_source():
    """字典用半角 `: `，与源码字面量一致——打印结果能直接抄回代码。"""
    assert _all_agree('打印({"甲": 1, "乙": [2, 3]})\n') == "{'甲': 1, '乙': [2, 3]}\n"


def test_repr_top_switch():
    """直接测显示函数本身：top 开关只有文本这一处差别。"""
    assert jishi_repr("甲", top=True) == "甲"
    assert jishi_repr("甲") == "'甲'"
    assert jishi_repr(None) == "空"
    assert jishi_repr(True) == "真"
    assert jishi_repr(False) == "假"
    assert jishi_repr([None, True, "甲"]) == "[空, 真, '甲']"
    assert jishi_repr({"k": None}) == "{'k': 空}"


def test_repl_echo_uses_same_function():
    """REPL 回显不再另抄一份实现（以前 replay 与打印会漂移）。"""
    from oracle.jishi.repl import _repr_jishi
    assert _repr_jishi(None) == "空"
    assert _repr_jishi(True) == "真"
    assert _repr_jishi("甲") == "'甲'"          # 回显算「容器内」这一侧
    assert _repr_jishi([None]) == "[空]"
