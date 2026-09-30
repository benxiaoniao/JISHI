# -*- coding: utf-8 -*-
"""M1-2：列表/字典/字符串中文方法测试。"""
import sys
import io
import pytest
from contextlib import redirect_stdout

sys.path.insert(0, ".")

from jishi.interpreter import run_source
from jishi.errors import JishiError


def run_capture(src: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src)
    return out.getvalue()


# -- 列表方法 --------------------------------------------------------------

def test_list_append():
    out = run_capture("令 a = [1, 2]\na.追加(3)\n打印(a)\n")
    assert out.strip() == "[1, 2, 3]"


def test_list_insert():
    out = run_capture("令 a = [1, 3]\na.插入(1, 2)\n打印(a)\n")
    assert out.strip() == "[1, 2, 3]"


def test_list_remove():
    out = run_capture("令 a = [1, 2, 3]\na.移除(2)\n打印(a)\n")
    assert out.strip() == "[1, 3]"


def test_list_remove_not_found_error():
    with pytest.raises(JishiError):
        run_source("令 a = [1, 2]\na.移除(9)\n")


def test_list_pop():
    out = run_capture("令 a = [1, 2, 3]\n令 x = a.弹出()\n打印(x)\n打印(a)\n")
    assert out.strip().splitlines() == ["3", "[1, 2]"]


def test_list_sort():
    out = run_capture("令 a = [3, 1, 2]\na.排序()\n打印(a)\n")
    assert out.strip() == "[1, 2, 3]"


def test_list_reverse():
    out = run_capture("令 a = [1, 2, 3]\na.反转()\n打印(a)\n")
    assert out.strip() == "[3, 2, 1]"


def test_list_index_count_contains():
    out = run_capture(
        "令 a = [5, 5, 6]\n打印(a.索引(6))\n打印(a.计数(5))\n打印(a.包含(5))\n"
        "打印(a.包含(9))\n")
    assert out.strip().splitlines() == ["2", "2", "真", "假"]


def test_list_clear():
    out = run_capture("令 a = [1, 2]\na.清空()\n打印(长度(a))\n")
    assert out.strip() == "0"


# -- 字典方法 --------------------------------------------------------------

def test_dict_get():
    out = run_capture('令 d = {"a"：1}\n打印(d.获取("a"))\n打印(d.获取("b", 0))\n'
                      '打印(d.获取("b"))\n')
    assert out.strip().splitlines() == ["1", "0", "空"]


def test_dict_keys_values():
    out = run_capture('令 d = {"a"：1, "b"：2}\n打印(d.键())\n打印(d.值())\n')
    assert out.strip().splitlines() == ["['a', 'b']", "[1, 2]"]


def test_dict_contains_update_pop():
    out = run_capture(
        '令 d = {"a"：1}\n打印(d.包含("a"))\n'
        'd.更新({"b"：2})\n打印(d)\n'
        '令 x = d.弹出("a")\n打印(x)\n打印(d)\n')
    assert out.strip().splitlines() == ["真", "{'a': 1, 'b': 2}", "1",
                                        "{'b': 2}"]


def test_dict_pop_missing_error():
    with pytest.raises(JishiError):
        run_source('令 d = {"a"：1}\nd.弹出("b")\n')


# -- 字符串方法 ------------------------------------------------------------

def test_str_split():
    out = run_capture('令 s = "a,b,c"\n打印(s.拆分(","))\n打印("1 2".拆分())\n')
    assert out.strip().splitlines() == ["['a', 'b', 'c']", "['1', '2']"]


def test_str_replace_find():
    out = run_capture('令 s = "hello world"\n打印(s.替换("world", "基石"))\n'
                      '打印(s.查找("world"))\n打印(s.查找("zzz"))\n')
    assert out.strip().splitlines() == ["hello 基石", "6", "-1"]


def test_str_upper_lower_strip():
    out = run_capture('令 s = "  AbC  "\n打印(s.大写())\n打印(s.小写())\n'
                      '打印(s.去空白())\n')
    assert out.splitlines() == ["  ABC  ", "  abc  ", "AbC"]


def test_str_startswith_endswith_contains():
    out = run_capture('令 s = "基石语言"\n打印(s.开头是("基石"))\n'
                      '打印(s.结尾是("语言"))\n打印(s.包含("石"))\n'
                      '打印(s.包含("xx"))\n')
    assert out.strip().splitlines() == ["真", "真", "真", "假"]


def test_str_to_int_float():
    out = run_capture('打印("42".转整数())\n打印("2.5".转小数())\n')
    assert out.strip().splitlines() == ["42", "2.5"]


def test_str_to_int_error():
    with pytest.raises(JishiError):
        run_source('"abc".转整数()\n')


# -- 错误提示 --------------------------------------------------------------

def test_unknown_method_hint():
    with pytest.raises(JishiError) as ei:
        run_source("令 a = [1]\na.排续()\n")
    assert "可用" in str(ei.value) or "方法" in str(ei.value)


def test_method_wrong_arg_count():
    with pytest.raises(JishiError):
        run_source("令 a = [1]\na.追加()\n")
