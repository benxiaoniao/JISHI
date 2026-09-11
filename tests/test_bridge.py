# -*- coding: utf-8 -*-
"""M3：Python 生态桥接测试。"""
import io
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, ".")

from jishi.interpreter import run_source
from jishi.errors import JishiError


def run_capture(src: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src)
    return out.getvalue()


# -- 导入与调用 -------------------------------------------------------------

def test_import_python_module():
    out = run_capture("导入 math 从 python\n打印(math.sqrt(16))\n")
    assert out.strip() == "4.0"


def test_import_with_alias():
    out = run_capture("导入 math 从 python 为 m\n打印(m.floor(2.9))\n")
    assert out.strip() == "2"


def test_json_roundtrip():
    out = run_capture(
        '导入 json 从 python\n'
        '令 s = json.dumps({"姓名"： "小明"})\n'
        '令 d = json.loads(s)\n'
        '打印(d["姓名"])\n')
    assert out.strip() == "小明"


def test_python_object_chain():
    out = run_capture(
        "导入 datetime 从 python\n"
        "令 现在 = datetime.datetime.now()\n"
        "打印(现在.year > 2000)\n")
    assert out.strip() == "True"


def test_python_module_variable():
    out = run_capture("导入 math 从 python\n打印(math.pi > 3.14)\n")
    assert out.strip() == "True"


def test_string_module():
    out = run_capture('导入 string 从 python\n打印(string.digits)\n')
    assert out.strip() == "0123456789"


def test_private_members_hidden():
    with pytest.raises(JishiError):
        run_source("导入 math 从 python\n打印(math._private_thing)\n")


def test_python_object_method():
    out = run_capture(
        "导入 json 从 python\n"
        "令 对象 = json.loads('{\"分数\": 90}')\n"
        "打印(对象[\"分数\"])\n")
    assert out.strip() == "90"


def test_python_object_attr_error_hint():
    with pytest.raises(JishiError) as ei:
        run_source("导入 math 从 python\nmath.pi.不存在xyz\n")
    msg = str(ei.value)
    assert "没有属性" in msg or "没有方法" in msg


def test_stdlib_module_still_works():
    # 基石标准库与 Python 模块互不干扰
    out = run_capture(
        "导入 随机\n导入 math 从 python\n"
        "打印(长度(随机.洗牌([1, 2, 3])))\n打印(math.sqrt(4))\n")
    lines = out.strip().splitlines()
    assert lines == ["3", "2.0"]


def test_python_native_method_fallback():
    # 基石类型也能用 Python 原生方法（渐进式过渡的隐藏福利）
    out = run_capture("令 a = [3, 1, 2]\na.sort()\n打印(a)\n")
    assert out.strip() == "[1, 2, 3]"


# -- 关键字参数 -------------------------------------------------------------

def test_keyword_arg_to_python():
    """中文用户最需要的能力：ensure_ascii=假 保住中文。"""
    out = run_capture(
        '导入 json 从 python\n'
        '打印(json.dumps({"姓名"： "小明"}, ensure_ascii=假))\n')
    assert out.strip() == '{"姓名": "小明"}'


def test_multiple_keyword_args():
    out = run_capture(
        '导入 json 从 python\n'
        '打印(json.dumps([1, 2], indent=2, ensure_ascii=假))\n')
    assert out.strip().startswith("[\n  1")


def test_keyword_arg_to_jishi_function():
    out = run_capture(
        "函数 介绍(姓名, 年龄)：\n"
        "    返回 姓名 + \"今年\" + 文本(年龄) + \"岁\"\n"
        "打印(介绍(年龄 = 8, 姓名 = \"小红\"))\n"
        "打印(介绍(\"小刚\", 年龄 = 20))\n")
    assert out.strip().splitlines() == ["小红今年8岁", "小刚今年20岁"]


def test_keyword_arg_missing_error():
    with pytest.raises(JishiError):
        run_source("函数 求和(a, b)：\n    返回 a + b\n求和(a = 1)\n")


def test_keyword_arg_unknown_error():
    with pytest.raises(JishiError):
        run_source("函数 求和(a, b)：\n    返回 a + b\n求和(1, 2, 多余 = 9)\n")


# -- 回调：基石函数传给 Python ---------------------------------------------

def test_jishi_function_as_callback():
    out = run_capture(
        "导入 builtins 从 python\n"
        "函数 加倍(x)：\n"
        "    返回 x * 2\n"
        "令 结果 = builtins.list(builtins.map(加倍, [1, 2, 3]))\n"
        "打印(结果)\n")
    assert out.strip() == "[2, 4, 6]"


def test_jishi_function_as_sort_key():
    out = run_capture(
        "导入 builtins 从 python\n"
        "函数 取长度(x)：\n"
        "    返回 长度(x)\n"
        "令 排好 = builtins.sorted([\"aaa\", \"a\", \"aa\"], key = 取长度)\n"
        "打印(排好)\n")
    assert out.strip() == "['a', 'aa', 'aaa']"


def test_callback_wrong_args_error():
    with pytest.raises(JishiError):
        run_source(
            "导入 builtins 从 python\n"
            "函数 需要两个(a, b)：\n"
            "    返回 a + b\n"
            "打印(builtins.list(builtins.map(需要两个, [1, 2, 3])))\n")


# -- 错误翻译与提示 ---------------------------------------------------------

def test_import_unknown_python_module():
    with pytest.raises(JishiError) as ei:
        run_source("导入 肯定没这个模块 从 python\n")
    assert "Python 里没有模块" in str(ei.value)


def test_python_file_not_found_translated():
    with pytest.raises(JishiError) as ei:
        run_source('导入 os 从 python\nos.listdir("肯定不存在的目录xyz")\n')
    assert "找不到文件或目录" in str(ei.value)


def test_python_valueerror_translated():
    # int("abc") 的 ValueError 应被翻译成中文错误
    with pytest.raises(JishiError) as ei:
        run_source('导入 builtins 从 python\n打印(builtins.int("abc"))\n')
    assert "E2" in str(ei.value) or "错误" in str(ei.value)


def test_english_bool_hint():
    """从 Python 代码抄来 True/False 时要有中文提示。"""
    with pytest.raises(JishiError) as ei:
        run_source("打印(True)\n")
    assert "「真」" in str(ei.value)


def test_english_none_hint():
    with pytest.raises(JishiError) as ei:
        run_source("打印(None)\n")
    assert "「空」" in str(ei.value)
