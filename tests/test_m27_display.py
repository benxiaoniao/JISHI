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

from jishi import cvm_bind                        # noqa: E402
from jishi import serialize                       # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.interpreter import Interpreter         # noqa: E402
from jishi.parser import parse                    # noqa: E402
from jishi.runtime import jishi_repr              # noqa: E402
from jishi.tokenizer import tokenize              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HAS_NODE = NODE is not None


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
    from jishi.repl import _repr_jishi
    assert _repr_jishi(None) == "空"
    assert _repr_jishi(True) == "真"
    assert _repr_jishi("甲") == "'甲'"          # 回显算「容器内」这一侧
    assert _repr_jishi([None]) == "[空]"


# ---------------------------------------------------------------------------
# Node 宿主：显示 + M26 的四个新内建（对拍）
# ---------------------------------------------------------------------------

pytestmark_host = pytest.mark.skipif(
    not HAS_NODE, reason="本机无 Node.js，跳过 JS VM 对拍")


def _compare_host(src: str) -> None:
    py_out = _pvm(src)
    text = serialize.dumps(compile_source(src, "<t>"))
    bc = tmp_bc_path(ROOT / "_tmp_m27_bc.json")
    bc.write_text(text, encoding="utf-8")
    try:
        r = subprocess.run(
            [NODE, str(ROOT / "node" / "index.js"), str(bc)],
            capture_output=True, text=True, encoding="utf-8")
    finally:
        bc.unlink()
    assert r.returncode == 0, f"Node 崩溃：{r.stderr[:400]}"
    assert r.stdout == py_out, (
        f"输出不一致\nPython: {py_out!r}\nNode:   {r.stdout!r}")


@pytestmark_host
def test_node_display_of_none_and_bool():
    _compare_host(
        "打印(空, 真, 假)\n"
        '打印([1, 真, 空, "甲"])\n'
        '打印({"键": 假})\n'
        "打印(`插值 {空} 与 {真}`)\n"
        "打印(类型(空), 类型(真))\n")


@pytestmark_host
def test_node_decimal_builtin():
    _compare_host(
        '打印(0.1 + 0.2 == 0.3)\n'
        '打印(精确("0.1") + 精确("0.2") == 精确("0.3"))\n'
        '打印(精确(0.1) + 精确(0.2))\n'
        '打印(精确("19.99") * 3)\n'
        '打印(精确("19.99") - 精确("20"))\n'
        '打印(精确("10") / 精确("4"))\n'
        '打印(精确("1") / 精确("3"))\n'
        '打印(精确("2.5").舍入(0), 精确("2.345").舍入(2))\n'
        '打印(精确("-3.7").绝对值(), 文本(精确("1.50")))\n'
        '打印(精确("0.3") == 0.3, 精确("1") < 2)\n'
        '打印(类型(精确("1")))\n')


@pytestmark_host
def test_node_decimal_accumulation():
    """累加要走十进制，不能在 JS 里退化成浮点。"""
    _compare_host(
        '令 总 = 精确(0)\n'
        '遍历 x 在 ["0.1", "0.2", "0.3"]：\n'
        '    总 += 精确(x)\n'
        '打印(总)\n'
        '打印(总和([精确("0.1"), 精确("0.2"), 精确("0.3")]))\n'
        '打印(最大([精确("1.5"), 精确("9.25")]))\n'
        '打印(最小([精确("1.5"), 精确("9.25")]))\n')


@pytestmark_host
def test_node_decimal_exp_alignment():
    """指数不同的加减要正确对齐。

    这条是为了锁住一个真实踩到的 bug：JS 侧 `_alignTo` 一开始把方向写反了
    （`e < target` 而不是 `e > target`），于是 `精确("19.99") - 精确("20")`
    算成 20.19 而不是 -0.01——只有「指数不同」的运算才会暴露，
    所以专门列一批不同标度混算的用例。
    """
    _compare_host(
        '打印(精确("19.99") - 精确("20"))\n'
        '打印(精确("100") - 精确("0.001"))\n'
        '打印(精确("0.001") + 精确("100"))\n'
        '打印(精确("1") < 精确("2.5"), 精确("2.5") > 精确("1"))\n'
        '打印(精确("100") / 精确("8"))\n'
        '打印(精确("1.5") * 精确("0.02"))\n')


@pytestmark_host
def test_node_file_and_context_manager():
    """文件读写 + `用 … 为` 自动关闭，Node 侧要能对上。"""
    # 临时文件放 build/ 且名字带进程号：build/ 由 conftest 保证存在（CI 全新
    # 检出没有它），带进程号则并行跑同一文件时不会互相覆写（M35）
    path = f"build/_t27_node_{os.getpid()}.txt"
    _compare_host(
        f'用 打开("{path}", "写") 为 f：\n'
        '    f.写行("第一行")\n'
        '    f.写行("第二行")\n'
        f'用 打开("{path}") 为 f：\n'
        '    遍历 行 在 f：\n'
        '        打印(行)\n'
        f'用 打开("{path}") 为 f：\n'
        '    打印(f.读所有行())\n'
        f'打印(打开("{path}").读())\n')


@pytestmark_host
def test_node_custom_object_print():
    _compare_host(
        '类 分数：\n'
        '    函数 初始化(自身, 分子, 分母)：\n'
        '        自身.分子 = 分子\n'
        '        自身.分母 = 分母\n'
        '    函数 文本(自身)：\n'
        '        返回 文本(自身.分子) + "/" + 文本(自身.分母)\n'
        '令 f = 新建 分数(1, 3)\n'
        '打印(f)\n'
        '打印(文本(f))\n'
        '打印([f, f])\n')
