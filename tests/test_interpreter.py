# -*- coding: utf-8 -*-
"""interpreter 单元测试：语句、表达式、控制流、函数、导入、报错。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jishi.interpreter import run_source
from jishi.errors import (
    JishiError,
    RunTypeError,
    SemanticFuncError,
    SemanticNameError,
)


def run_capture(src, inputs=None):
    """执行源码，返回打印输出列表。"""
    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    if inputs:
        import unittest.mock
        with redirect_stdout(out), \
             unittest.mock.patch("builtins.input", side_effect=inputs):
            run_source(src)
    else:
        with redirect_stdout(out):
            run_source(src)
    return out.getvalue()


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

def test_hello():
    out = run_capture('打印("你好，世界！")')
    assert out.strip() == "你好，世界！"


def test_multiple_args_print():
    out = run_capture('打印("分数是", 60)')
    assert out.strip() == "分数是 60"


def test_assign_and_arith():
    out = run_capture("令 x = 1 + 2 * 3\n打印(x)")
    assert out.strip() == "7"


def test_string_concat():
    out = run_capture('令 s = "你" + "好"\n打印(s)')
    assert out.strip() == "你好"


def test_fullwidth_punctuation_runs():
    out = run_capture("令 分数 ＝ 60\n打印（分数）")
    assert out.strip() == "60"


# ---------------------------------------------------------------------------
# 控制流
# ---------------------------------------------------------------------------

def test_if_elif_else():
    src = ("令 x = 60\n"
           "如果 x > 60：\n    打印(1)\n"
           "否则如果 x == 60：\n    打印(2)\n"
           "否则：\n    打印(3)")
    assert run_capture(src).strip() == "2"


def test_loop_times():
    out = run_capture("循环 3 次：\n    打印(1)")
    assert out.strip() == "1\n1\n1"


def test_for_list():
    src = "遍历 x 在 [1, 2, 3]：\n    打印(x)"
    assert run_capture(src).strip() == "1\n2\n3"


def test_while_and_break():
    src = ("令 x = 0\n"
           "当 真：\n"
           "    x += 1\n"
           "    如果 x >= 3：\n"
           "        中断\n"
           "打印(x)")
    assert run_capture(src).strip() == "3"


def test_continue():
    src = ("遍历 x 在 [1, 2, 3, 4]：\n"
           "    如果 x % 2 == 0：\n"
           "        继续\n"
           "    打印(x)")
    assert run_capture(src).strip() == "1\n3"


# ---------------------------------------------------------------------------
# 函数
# ---------------------------------------------------------------------------

def test_funcdef_return():
    src = ("函数 求和(a, b)：\n"
           "    返回 a + b\n"
           "打印(求和(3, 4))")
    assert run_capture(src).strip() == "7"


def test_func_no_return():
    src = ("函数 打招呼()：\n"
           "    打印(1)\n"
           "打招呼()")
    assert run_capture(src).strip() == "1"


def test_local_scope():
    src = ("函数 加一(x)：\n"
           "    x += 1\n"
           "    返回 x\n"
           "令 x = 10\n"
           "打印(加一(x))\n"
           "打印(x)")
    out = run_capture(src)
    assert out.strip() == "11\n10"


def test_recursion():
    src = ("函数 阶乘(n)：\n"
           "    如果 n <= 1：\n"
           "        返回 1\n"
           "    返回 n * 阶乘(n - 1)\n"
           "打印(阶乘(5))")
    assert run_capture(src).strip() == "120"


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

def test_list_subscript():
    out = run_capture("令 a = [10, 20, 30]\n打印(a[1])")
    assert out.strip() == "20"


def test_dict():
    src = '令 d = {"语文"：95, "数学"：88}\n打印(d["数学"])'
    assert run_capture(src).strip() == "88"


def test_len_builtin():
    out = run_capture('令 a = [1, 2, 3]\n打印(长度(a))\n打印(长度("你好"))')
    assert out.strip() == "3\n2"


def test_int_float_str():
    src = ('打印(整数("42") + 1)\n'
           '打印(小数("1.5") + 0.5)\n'
           '打印(文本(99) + "号")')
    out = run_capture(src)
    assert out.strip() == "43\n2.0\n99号"


def test_input_builtin():
    out = run_capture('令 名字 = 输入("你是谁：")\n打印("你好，", 名字)',
                      inputs=["小石"])
    assert out.strip() == "你好， 小石"


def test_boolop_and_compare():
    src = ("令 x = 70\n"
           "如果 x > 60 与 x < 100：\n    打印(\"合格\")\n"
           "否则：\n    打印(\"不合格\")")
    assert run_capture(src).strip() == "合格"


def test_import_stdlib():
    src = ("导入 随机\n"
           "令 n = 随机.随机整数(1, 2)\n"
           "打印(n)")
    out = run_capture(src)
    assert out.strip() in ("1", "2")


# ---------------------------------------------------------------------------
# 报错
# ---------------------------------------------------------------------------

def test_undefined_name_suggests():
    with pytest.raises(SemanticNameError) as ei:
        run_capture("令 平均分 = 60\n打印(平均)\n打印(平均分)")
    # 第一个「平均」应该建议「平均分」
    assert "平均分" in str(ei.value)


def test_divide_by_zero():
    with pytest.raises(JishiError) as ei:
        run_capture("令 x = 1 / 0")
    assert "除以零" in str(ei.value)


def test_type_error_message_chinese():
    with pytest.raises(RunTypeError):
        run_capture('令 x = 1 + "a"')


def test_module_missing_attr():
    with pytest.raises(SemanticFuncError):
        run_capture("导入 随机\n随机.不存在函数()")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
