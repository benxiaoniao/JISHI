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


# -- 7.3 列表/字典推导式 ----------------------------------------------------

def test_list_comprehension():
    out = run_capture("打印([x * 2 遍历 x 在 [1, 2, 3]])")
    assert out == "[2, 4, 6]\n"


def test_list_comprehension_with_condition():
    out = run_capture(
        "打印([x * 2 遍历 x 在 [1, 2, 3, 4] 如果 x > 2])")
    assert out == "[6, 8]\n"


def test_dict_comprehension():
    out = run_capture("打印({x: x * x 遍历 x 在 [1, 2, 3]})")
    assert out == "{1: 1, 2: 4, 3: 9}\n"


def test_dict_comprehension_with_condition():
    out = run_capture(
        "打印({x: x * x 遍历 x 在 [1, 2, 3, 4] 如果 x > 2})")
    assert out == "{3: 9, 4: 16}\n"


def test_comprehension_over_string():
    out = run_capture("打印([x 遍历 x 在 \"你好\"])")
    assert out == "['你', '好']\n"


def test_comprehension_in_function():
    out = run_capture(
        "函数 f()：\n"
        "    返回 [x 遍历 x 在 [1, 2, 3]]\n"
        "打印(f())\n")
    assert out == "[1, 2, 3]\n"


def test_comprehension_nested_use():
    out = run_capture(
        "令 总和 = 总和([x 遍历 x 在 [1, 2, 3]])\n"
        "打印(总和)\n")
    assert out == "6\n"


# -- 7.4 文本插值 ------------------------------------------------------------

def test_fstring_basic():
    out = run_capture(
        "令 名字 = \"小明\"\n"
        "令 年龄 = 18\n"
        "打印(`你好 {名字}，今年 {年龄} 岁`)\n")
    assert out == "你好 小明，今年 18 岁\n"


def test_fstring_expression():
    out = run_capture(
        "令 a = 3\n"
        "令 b = 4\n"
        "打印(`{a} + {b} = {a + b}`)\n")
    assert out == "3 + 4 = 7\n"


def test_fstring_literal_braces():
    out = run_capture("打印(`使用 {{花括号}} 转义`)\n")
    assert out == "使用 {花括号} 转义\n"


def test_fstring_plain_text():
    out = run_capture("打印(`无插值`)\n")
    assert out == "无插值\n"


def test_fstring_nested_call():
    out = run_capture(
        "令 数据 = [1, 2, 3]\n"
        "打印(`长度 {长度(数据)}`)\n")
    assert out == "长度 3\n"


def test_fstring_unclosed():
    err = run_error("打印(`你好 {名字})")
    assert "插值字符串没有正常结束" in str(err) or \
        "插值表达式没有闭合" in str(err)


# -- 7.5 容错解析：英文关键字/内建 → 中文建议 -----------------------------

def test_en_keyword_def():
    err = run_error("def f():\n    return 1")
    assert "函数" in str(err) and "def" in str(err)


def test_en_builtin_print():
    err = run_error("print(1)")
    assert "打印" in str(err)


def test_en_value_true():
    err = run_error("令 x = True")
    assert "真" in str(err)


def test_en_operator_and():
    err = run_error("令 x = 1 and 2")
    assert "与" in str(err)


def test_en_unsupported_lambda():
    err = run_error("lambda x: x")
    assert "lambda" in str(err)


def test_en_word_no_false_positive():
    # 英文短变量名 / 中文变量不应被误伤
    out = run_capture("令 a = 5\n令 b = 3\n打印(a + b)\n")
    assert out == "8\n"


def test_en_word_chinese_var_ok():
    out = run_capture("令 名称 = \"测试\"\n打印(名称)\n")
    assert out == "测试\n"
