# -*- coding: utf-8 -*-
"""M1-1：链式比较与内建函数扩充测试。"""
import sys
import io
from contextlib import redirect_stdout

sys.path.insert(0, ".")

from jishi.interpreter import run_source


def run_capture(src: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src)
    return out.getvalue()


def test_chained_compare_true():
    out = run_capture("令 x = 5\n打印(1 < x < 10)\n")
    assert out.strip() == "True"


def test_chained_compare_false():
    out = run_capture("令 x = 15\n打印(1 < x < 10)\n")
    assert out.strip() == "False"


def test_chained_compare_mixed_ops():
    out = run_capture("令 x = 5\n打印(1 < x <= 5)\n")
    assert out.strip() == "True"


def test_chained_compare_short_circuit():
    # 第一对 1 < 0 为假 → 短路，不再求值 (1 / 0)；若没短路会抛除零错误
    out = run_capture("打印(1 < 0 < (1 / 0))\n")
    assert out.strip() == "False"


def test_chained_compare_chain_of_three():
    out = run_capture("令 x = 5\n打印(1 < x < 10 < 20)\n")
    assert out.strip() == "True"


def test_builtin_range_one():
    out = run_capture("打印(范围(5))\n")
    assert out.strip() == "[0, 1, 2, 3, 4]"


def test_builtin_range_two():
    out = run_capture("打印(范围(2, 6))\n")
    assert out.strip() == "[2, 3, 4, 5]"


def test_builtin_range_three():
    out = run_capture("打印(范围(0, 10, 3))\n")
    assert out.strip() == "[0, 3, 6, 9]"


def test_builtin_range_in_for():
    out = run_capture("令 总和 = 0\n遍历 i 在 范围(1, 101)：\n    总和 += i\n打印(总和)\n")
    assert out.strip() == "5050"


def test_builtin_max_min_sum():
    out = run_capture("令 列表 = [3, 9, 1, 7]\n打印(最大(列表))\n打印(最小(列表))\n打印(总和(列表))\n")
    assert out.strip().splitlines() == ["9", "1", "20"]


def test_builtin_type():
    out = run_capture(
        '打印(类型(1))\n打印(类型(1.5))\n打印(类型("你好"))\n'
        '打印(类型([1]))\n打印(类型({"a"：1}))\n打印(类型(真))\n打印(类型(空))\n'
        "函数 f()：\n    返回 1\n打印(类型(f))\n")
    assert out.strip().splitlines() == [
        "整数", "小数", "文本", "列表", "字典", "布尔", "空", "函数"]


def test_builtin_reverse_list():
    out = run_capture("打印(反转([1, 2, 3]))\n")
    assert out.strip() == "[3, 2, 1]"


def test_builtin_reverse_string():
    out = run_capture('打印(反转("abc"))\n')
    assert out.strip() == "cba"


def test_range_error():
    import pytest
    from jishi.errors import JishiError
    with pytest.raises(JishiError):
        run_source("打印(范围(1, 2, 3, 4))\n")
