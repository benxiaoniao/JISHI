# -*- coding: utf-8 -*-
"""M7 表达力打磨：语法糖单元测试（树遍历；三执行器一致性由对拍保证）。"""

import io
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, ".")

from jishi.errors import JishiError
from jishi.interpreter import run_source


def run_capture(src: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src)
    return out.getvalue()


def run_error(src: str) -> JishiError:
    with pytest.raises(JishiError) as ei:
        run_source(src)
    return ei.value


# -- 7.1 多赋值与解包 --------------------------------------------------------

def test_unpack_list():
    assert run_capture("令 a, b = [1, 2]\n打印(a, b)") == "1 2\n"


def test_unpack_swap():
    out = run_capture("令 a = 1\n令 b = 2\na, b = b, a\n打印(a, b)")
    assert out == "2 1\n"


def test_multi_return_and_unpack():
    out = run_capture(
        "函数 两数()：\n"
        "    返回 3, 4\n"
        "令 x, y = 两数()\n"
        "打印(x + y)\n")
    assert out == "7\n"


def test_multi_return_is_list():
    out = run_capture("函数 f()：\n    返回 1, 2\n打印(f())")
    assert out == "[1, 2]\n"


def test_unpack_count_mismatch():
    err = run_error("令 a, b, c = [1, 2]")
    assert "解包需要 3 个值，实际有 2 个" in str(err)


def test_unpack_non_list():
    err = run_error("令 a, b = 5")
    assert "解包赋值需要列表" in str(err)


def test_unpack_no_compound():
    with pytest.raises(JishiError):
        run_source("令 a, b = [1, 2]\na, b += [1, 2]")


def test_unpack_in_function_scope():
    out = run_capture(
        "函数 f()：\n"
        "    令 a, b = [1, 2]\n"
        "    返回 a + b\n"
        "打印(f())\n")
    assert out == "3\n"


# -- 7.2 默认参数 ------------------------------------------------------------

def test_default_param_basic():
    out = run_capture(
        "函数 打招呼(名字, 语气 = \"你好\")：\n"
        "    打印(语气, 名字)\n"
        "打招呼(\"小明\")\n"
        "打招呼(\"小明\", \"欢迎\")\n")
    assert out == "你好 小明\n欢迎 小明\n"


def test_default_evaluated_once_at_def():
    out = run_capture(
        "令 计 = 0\n"
        "函数 f(a = 计)：\n"
        "    返回 a\n"
        "计 = 5\n"
        "打印(f())\n")
    assert out == "0\n"      # 定义处求值（与 Python 一致）


def test_default_param_order_error():
    err = run_error("函数 f(a = 1, b)：\n    返回 a")
    assert "有默认值的参数要放在最后" in str(err)


def test_default_with_kwarg():
    out = run_capture(
        "函数 f(a, b = 9)：\n"
        "    打印(a, b)\n"
        "f(a = 1)\n"
        "f(1, b = 2)\n")
    assert out == "1 9\n1 2\n"


def test_method_default_param():
    out = run_capture(
        "类 面积：\n"
        "    函数 算(自身, 倍 = 2)：\n"
        "        返回 倍 * 10\n"
        "令 m = 新建 面积()\n"
        "打印(m.算(), m.算(3))\n")
    assert out == "20 30\n"
