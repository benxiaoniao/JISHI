# -*- coding: utf-8 -*-
"""M5b：面向对象（类/继承/新建）单元测试。

覆盖树遍历解释器的类语义；字节码 VM 与 C VM 的一致性由
tests/test_cvm.py 的对拍测试保证。
"""

import io
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, ".")

from jishi.errors import JishiError, RunTypeError
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


# -- 基本类 ----------------------------------------------------------------

def test_class_instantiate_and_method():
    out = run_capture(
        "类 狗：\n"
        "    函数 初始化(自身, 名字)：\n"
        "        自身.名字 = 名字\n"
        "    函数 叫(自身)：\n"
        "        打印(自身.名字, \"汪汪\")\n"
        "令 d = 新建 狗(\"旺财\")\n"
        "d.叫()\n")
    assert out.strip() == "旺财 汪汪"


def test_class_field_access():
    out = run_capture(
        "类 账户：\n"
        "    函数 初始化(自身, 余额)：\n"
        "        自身.余额 = 余额\n"
        "    函数 查(自身)：\n"
        "        返回 自身.余额\n"
        "令 a = 新建 账户(100)\n"
        "打印(a.查())\n")
    assert out.strip() == "100"


def test_method_mutates_field():
    out = run_capture(
        "类 账户：\n"
        "    函数 初始化(自身, 余额)：\n"
        "        自身.余额 = 余额\n"
        "    函数 存(自身, 数额)：\n"
        "        自身.余额 = 自身.余额 + 数额\n"
        "    函数 查(自身)：\n"
        "        返回 自身.余额\n"
        "令 a = 新建 账户(100)\n"
        "a.存(50)\n"
        "a.存(25)\n"
        "打印(a.查())\n")
    assert out.strip() == "175"


def test_two_instances_independent():
    out = run_capture(
        "类 计数器：\n"
        "    函数 初始化(自身)：\n"
        "        自身.计 = 0\n"
        "    函数 加(自身)：\n"
        "        自身.计 = 自身.计 + 1\n"
        "        返回 自身.计\n"
        "令 a = 新建 计数器()\n"
        "令 b = 新建 计数器()\n"
        "a.加()\n"
        "a.加()\n"
        "打印(a.计, b.计)\n")
    assert out.strip() == "2 0"


def test_class_without_constructor():
    out = run_capture(
        "类 点：\n"
        "    函数 描述(自身)：\n"
        "        返回 \"一个点\"\n"
        "令 p = 新建 点()\n"
        "打印(p.描述())\n")
    assert out.strip() == "一个点"


# -- 继承 ----------------------------------------------------------------

def test_inheritance_method_override():
    out = run_capture(
        "类 动物：\n"
        "    函数 叫(自身)：\n"
        "        返回 \"在叫\"\n"
        "类 猫 继承 动物：\n"
        "    函数 叫(自身)：\n"
        "        返回 \"喵喵\"\n"
        "令 c = 新建 猫()\n"
        "打印(c.叫())\n")
    assert out.strip() == "喵喵"


def test_inheritance_uses_base_method():
    """子类没覆盖的方法用基类的。"""
    out = run_capture(
        "类 动物：\n"
        "    函数 叫(自身)：\n"
        "        返回 \"在叫\"\n"
        "类 狗 继承 动物：\n"
        "    函数 摇尾(自身)：\n"
        "        返回 \"摇尾巴\"\n"
        "令 d = 新建 狗()\n"
        "打印(d.叫(), d.摇尾())\n")
    assert out.strip() == "在叫 摇尾巴"


def test_inheritance_uses_base_constructor():
    """子类没定义「初始化」，用基类的构造方法。"""
    out = run_capture(
        "类 基：\n"
        "    函数 初始化(自身, x)：\n"
        "        自身.x = x\n"
        "    函数 取(自身)：\n"
        "        返回 自身.x\n"
        "类 子 继承 基：\n"
        "    函数 加倍(自身)：\n"
        "        返回 自身.取() * 2\n"
        "令 s = 新建 子(21)\n"
        "打印(s.加倍())\n")
    assert out.strip() == "42"


def test_inherit_non_class_errors():
    err = run_error(
        "令 动物 = 5\n"
        "类 猫 继承 动物：\n"
        "    函数 叫(自身)：\n"
        "        返回 \"喵\"\n")
    assert "不是类" in str(err)


# -- 错误处理 ----------------------------------------------------------------

def test_call_with_args_no_constructor():
    err = run_error(
        "类 点：\n"
        "    函数 描述(自身)：\n"
        "        返回 \"点\"\n"
        "令 p = 新建 点(1, 2)\n")
    assert "没有构造方法" in str(err)


def test_access_missing_method():
    err = run_error(
        "类 狗：\n"
        "    函数 叫(自身)：\n"
        "        返回 \"汪\"\n"
        "令 d = 新建 狗()\n"
        "d.不存在的方法()\n")
    assert "没有属性" in str(err)


def test_method_missing_self_is_normal_param():
    """方法首参「自身」是普通参数，缺了也按参数错误报。"""
    err = run_error(
        "类 狗：\n"
        "    函数 叫(自身)：\n"
        "        返回 \"汪\"\n"
        "令 d = 新建 狗()\n"
        "d.叫(1)\n")
    assert "参数" in str(err)
