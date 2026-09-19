# -*- coding: utf-8 -*-
"""M5a：异常处理（尝试/捕获/最终/抛出）单元测试。

覆盖树遍历解释器的异常语义；字节码 VM 与 C VM 的一致性由
tests/test_cvm.py 的对拍测试保证（同一批异常用例三执行器逐字节一致）。
"""

import io
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, ".")

from jishi.errors import JishiError, RunTypeError, RunValueError
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


# -- 抛出与捕获 -------------------------------------------------------------

def test_raise_and_catch():
    out = run_capture(
        "函数 折扣(原价, 折)：\n"
        "    如果 折 <= 0：\n"
        "        抛出 值错误(\"折扣不能是负数\")\n"
        "    返回 原价 * 折\n"
        "尝试：\n"
        "    打印(折扣(100, -1))\n"
        "捕获 值错误 为 e：\n"
        "    打印(e.类型, e.消息)\n")
    assert "值错误" in out and "折扣不能是负数" in out


def test_raise_non_error_value_wrapped():
    """抛出非异常值会被包装成「异常」，原值留在 e.消息。"""
    out = run_capture(
        "尝试：\n"
        "    抛出 \"余额不足\"\n"
        "捕获 为 e：\n"
        "    打印(e.类型, e.消息)\n")
    assert "异常" in out and "余额不足" in out


def test_raise_exception_type_name():
    """捕获 值错误 时 e.类型 是「值错误」。"""
    out = run_capture(
        "尝试：\n"
        "    抛出 值错误(\"x\")\n"
        "捕获 值错误 为 e：\n"
        "    打印(e.类型)\n")
    assert "值错误" in out


def test_bare_except_catches_everything():
    out = run_capture(
        "尝试：\n"
        "    抛出 异常(\"坏了\")\n"
        "捕获：\n"
        "    打印(\"全接\")\n")
    assert "全接" in out


def test_base_class_catches_subclass():
    """运行期错误 是 值错误 的基类，能抓子类。"""
    out = run_capture(
        "尝试：\n"
        "    抛出 值错误(\"具体的\")\n"
        "捕获 运行期错误 为 e：\n"
        "    打印(e.类型)\n")
    assert "值错误" in out


def test_multi_handler_order():
    out = run_capture(
        "尝试：\n"
        "    抛出 键错误(\"k\")\n"
        "捕获 类型错误：\n"
        "    打印(\"一\")\n"
        "捕获 键错误：\n"
        "    打印(\"二\")\n"
        "捕获 异常：\n"
        "    打印(\"三\")\n")
    assert out.strip() == "二"


def test_catch_builtin_error():
    """内建函数抛出的错误也能被捕获。

    归类按 Python 来（M41 修正）：`整数("abc")` 是**值错误**
    （原先抛「类型错误」，于是写 `捕获 值错误` 接不住——真实项目里真踩到过，
    见 `docs/真实项目.md` 的别扭点清单 #2）。
    """
    out = run_capture(
        "尝试：\n"
        "    令 x = 整数(\"abc\")\n"
        "捕获 值错误 为 e：\n"
        "    打印(\"接住了\", e.类型)\n")
    assert "接住了" in out


def test_catch_builtin_type_error_still_type_error():
    """类型本身不对（不是文本转不动）仍归「类型错误」。"""
    out = run_capture(
        "尝试：\n"
        "    令 x = 整数([1, 2])\n"
        "捕获 类型错误 为 e：\n"
        "    打印(\"接住了\", e.类型)\n")
    assert "接住了" in out


# -- 最终块 ----------------------------------------------------------------

def test_finally_runs_on_normal():
    out = run_capture(
        "尝试：\n"
        "    打印(\"体\")\n"
        "最终：\n"
        "    打印(\"收尾\")\n")
    assert out == "体\n收尾\n"


def test_finally_runs_on_exception():
    out = run_capture(
        "尝试：\n"
        "    抛出 值错误(\"x\")\n"
        "捕获 值错误：\n"
        "    打印(\"捕获\")\n"
        "最终：\n"
        "    打印(\"收尾\")\n")
    assert out == "捕获\n收尾\n"


def test_finally_runs_on_uncaught():
    """异常未被捕获时，最终块仍执行，异常继续向外抛。"""
    with pytest.raises(JishiError):
        with redirect_stdout(io.StringIO()):
            run_source(
                "尝试：\n"
                "    抛出 值错误(\"x\")\n"
                "最终：\n"
                "    打印(\"收尾\")\n")


def test_finally_overrides_return():
    """最终块里的返回覆盖 try 块里的返回。"""
    out = run_capture(
        "函数 g()：\n"
        "    尝试：\n"
        "        返回 \"原\"\n"
        "    最终：\n"
        "        返回 \"覆盖\"\n"
        "打印(g())\n")
    assert out.strip() == "覆盖"


def test_finally_runs_before_return():
    """正常返回时最终块先执行。"""
    out = run_capture(
        "函数 f()：\n"
        "    尝试：\n"
        "        返回 1\n"
        "    最终：\n"
        "        打印(\"清理\")\n"
        "打印(f())\n")
    assert out == "清理\n1\n"


# -- 异常传播 ----------------------------------------------------------------

def test_exception_propagates_through_calls():
    """异常穿透函数调用栈，在顶层被捕获。"""
    out = run_capture(
        "函数 内层()：\n"
        "    抛出 值错误(\"深层\")\n"
        "函数 外层()：\n"
        "    内层()\n"
        "尝试：\n"
        "    外层()\n"
        "捕获 值错误 为 e：\n"
        "    打印(e.消息)\n")
    assert "深层" in out


def test_uncaught_error_has_location():
    err = run_error("抛出 值错误(\"裸\")\n")
    assert err.类型 == "值错误"
    assert "数值不对" in str(err)


def test_break_not_caught_by_except():
    """中断 不是异常，不被 捕获 接住（与 Python 一致）。"""
    out = run_capture(
        "遍历 i 在 范围(3)：\n"
        "    尝试：\n"
        "        如果 i == 1：\n"
        "            中断\n"
        "        打印(i)\n"
        "    捕获 异常：\n"
        "        打印(\"不该捕获\")\n")
    assert out.strip() == "0"


def test_exception_in_loop_body_with_finally():
    """循环体内 try 的中断先触发最终，再中断循环。"""
    out = run_capture(
        "遍历 i 在 范围(3)：\n"
        "    尝试：\n"
        "        打印(i)\n"
        "        如果 i == 1：\n"
        "            中断\n"
        "    最终：\n"
        "        打印(\"收\", i)\n")
    assert out == "0\n收 0\n1\n收 1\n"
