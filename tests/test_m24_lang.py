# -*- coding: utf-8 -*-
"""M24 语言功能扩展（B 档）：集合 / 迭代协议 / 切片赋值 / 类型标注 / 匿名函数。

每个特性都做**三执行器对拍**（树遍历 / Python VM / C VM 逐字节一致）。
对拍是这批特性的主要防线：它们都改了「表达式求值」或「函数对象构造」，
最容易出现「树遍历对、VM 或 C VM 不对」的偏差。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import cvm_bind                        # noqa: E402
from jishi import runtime as R                    # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.errors import JishiError               # noqa: E402
from jishi.interpreter import Interpreter         # noqa: E402
from jishi.parser import parse                    # noqa: E402
from jishi.tokenizer import tokenize              # noqa: E402

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
# M24.1 集合
# ---------------------------------------------------------------------------

def test_set_literal_dedups_and_keeps_order():
    assert _all_agree('令 s = {3, 1, 3, 2, 1}\n打印(s, 长度(s))\n'
                      ) == "{3, 1, 2} 3\n"


def test_set_literal_elements_can_be_expressions():
    assert _all_agree('令 甲 = 2\n打印({甲, 甲 * 2, 1 + 0})\n') == "{2, 4, 1}\n"


def test_empty_braces_is_still_dict():
    """`{}` 保持空字典（与 Python 一致），空集合要写 集合()。"""
    assert _all_agree('打印(类型({}), 类型(集合()))\n') == "字典 集合\n"


def test_set_ctor_from_list_dedups():
    assert _all_agree('打印(集合([3, 1, 3, 2]))\n打印(集合())\n'
                      ) == "{3, 1, 2}\n集合()\n"


def test_set_ctor_from_text():
    assert _all_agree('打印(集合("你好你好"))\n') == "{'你', '好'}\n"


def test_set_membership_and_len():
    assert _all_agree(
        '令 s = {1, 2, 3}\n打印(2 在 s, 9 在 s, 9 不在 s, 长度(s))\n'
    ) == "真 假 真 3\n"


def test_set_equality_ignores_order():
    assert _all_agree('打印({1, 2} == {2, 1}, {1, 2} == {1, 3})\n'
                      ) == "真 假\n"


def test_set_add_remove_clear():
    assert _all_agree(
        '令 s = {1}\n'
        's.添加(2)\n'
        '打印(s)\n'
        's.移除(1)\n'
        '打印(s)\n'
        's.丢弃(99)\n'          # 没有也不报错
        '打印(s)\n'
        's.清空()\n'
        '打印(s, 长度(s))\n'
    ) == "{1, 2}\n{2}\n{2}\n集合() 0\n"


def test_set_remove_missing_errors_with_hint():
    with pytest.raises(JishiError) as ei:
        _tree('令 s = {1}\ns.移除(9)\n')
    assert "没有" in ei.value.message
    assert "丢弃" in (ei.value.hint or "")


def test_set_algebra():
    assert _all_agree(
        '令 a = {1, 2, 3}\n'
        '打印(a.并集([3, 4]))\n'
        '打印(a.交集([2, 3, 9]))\n'
        '打印(a.差集([2]))\n'
        '打印(a.对称差([3, 4]))\n'
    ) == "{1, 2, 3, 4}\n{2, 3}\n{1, 3}\n{1, 2, 4}\n"


def test_set_to_list_copy_and_iteration():
    assert _all_agree(
        '令 s = {2, 1}\n'
        '打印(s.转列表())\n'
        '令 c = s.复制()\n'
        'c.添加(9)\n'
        '打印(s, c)\n'
        '遍历 x 在 s：\n'
        '    打印("元素", x)\n'
    ) == "[2, 1]\n{2, 1} {2, 1, 9}\n元素 2\n元素 1\n"


def test_set_comprehension_dedups():
    assert _all_agree(
        '打印({x % 3 遍历 x 在 范围(9)})\n') == "{0, 1, 2}\n"


def test_set_used_for_dedup_pipeline():
    assert _all_agree(
        '令 词 = ["甲", "乙", "甲", "丙", "乙"]\n'
        '打印(集合(词))\n'
        '打印(集合(词).转列表())\n') == "{'甲', '乙', '丙'}\n['甲', '乙', '丙']\n"


def test_set_rejects_mutable_element():
    with pytest.raises(JishiError) as ei:
        _tree('打印(集合([[1, 2]]))\n')
    assert "不能放进集合" in ei.value.message
    assert "不可变" in (ei.value.hint or "")


def test_jishi_set_is_not_python_set():
    """有意不用内建 set：迭代顺序必须稳定，否则三执行器对拍会随机失败。"""
    s = R.JishiSet([3, 1, 2, 3])
    assert list(s) == [3, 1, 2]
    assert type(s).__name__ == "JishiSet"


def test_set_ordering_is_deterministic_across_runs():
    """同一段程序跑两次输出必须一样（内建 set 做不到这点）。"""
    src = '令 s = {}\n打印(集合(["丙", "甲", "乙", "甲"]))\n'
    assert _tree(src) == _tree(src)


# ---------------------------------------------------------------------------
# M24.1 迭代协议
# ---------------------------------------------------------------------------

_ITER_PROTO_SRC = (
    '类 盒子：\n'
    '    函数 初始化(自身, 项)：\n'
    '        自身.项 = 项\n'
    '    函数 迭代(自身)：\n'
    '        返回 自身.项\n'
    '令 b = 新建 盒子([10, 20, 30])\n'
    '遍历 x 在 b：\n'
    '    打印(x)\n')


def test_custom_object_iteration_in_for():
    assert _all_agree(_ITER_PROTO_SRC) == "10\n20\n30\n"


def test_custom_object_works_with_builtins_that_iterate():
    assert _all_agree(
        _ITER_PROTO_SRC +
        '打印(总和(b), 长度(b))\n'
        '打印([x * 2 遍历 x 在 b])\n'
        '打印(最大(b), 最小(b))\n'
    ) == "10\n20\n30\n60 3\n[20, 40, 60]\n30 10\n"


def test_iterator_method_may_yield_set_or_text():
    assert _all_agree(
        '类 词袋：\n'
        '    函数 初始化(自身)：\n'
        '        自身.词 = 集合(["甲", "乙"])\n'
        '    函数 迭代(自身)：\n'
        '        返回 自身.词\n'
        '令 w = 新建 词袋()\n'
        '遍历 x 在 w：\n'
        '    打印(x)\n') == "甲\n乙\n"


def test_non_iterable_object_error_mentions_protocol():
    with pytest.raises(JishiError) as ei:
        _tree('类 甲：\n    函数 a(自身)：\n        返回 1\n'
              '令 x = 新建 甲()\n遍历 i 在 x：\n    打印(i)\n')
    assert "不能遍历" in ei.value.message
    assert "迭代()" in ei.value.message


def test_iterator_returning_self_is_blocked():
    with pytest.raises(JishiError) as ei:
        _tree('类 甲：\n    函数 迭代(自身)：\n        返回 自身\n'
              '令 x = 新建 甲()\n遍历 i 在 x：\n    打印(i)\n')
    assert "无限循环" in ei.value.message


# ---------------------------------------------------------------------------
# M24.2 切片赋值
# ---------------------------------------------------------------------------

def test_slice_assignment_basic():
    assert _all_agree(
        '令 a = [1, 2, 3, 4, 5]\n'
        'a[1:3] = [9, 9]\n打印(a)\n'
        'a[:2] = [0]\n打印(a)\n'
        'a[1:] = []\n打印(a)\n'
    ) == "[1, 9, 9, 4, 5]\n[0, 9, 4, 5]\n[0]\n"


def test_slice_assignment_with_step():
    assert _all_agree(
        '令 d = [1, 2, 3, 4, 5]\nd[::2] = [7, 8, 9]\n打印(d)\n'
    ) == "[7, 2, 8, 4, 9]\n"


def test_slice_assignment_grows_and_shrinks():
    assert _all_agree(
        '令 a = [1, 2, 3]\n'
        'a[1:1] = [8, 9]\n打印(a)\n'          # 插入
        'a[0:3] = [0]\n打印(a)\n'             # 收缩
    ) == "[1, 8, 9, 2, 3]\n[0, 2, 3]\n"


def test_slice_read_still_works():
    assert _all_agree(
        '令 a = [1, 2, 3, 4]\n打印(a[1:3], a[::-1], a[:], a[::2])\n'
        '打印("你好世界"[1:3])\n'
    ) == "[2, 3] [4, 3, 2, 1] [1, 2, 3, 4] [1, 3]\n好世\n"


def test_slice_assignment_with_variable_bounds():
    assert _all_agree(
        '令 a = [1, 2, 3, 4]\n令 起 = 1\n令 止 = 3\na[起:止] = [0]\n打印(a)\n'
    ) == "[1, 0, 4]\n"


def test_index_assignment_unchanged():
    assert _all_agree(
        '令 a = [1, 2, 3]\na[0] = 9\na[-1] = 8\na[1] += 10\n打印(a)\n'
    ) == "[9, 12, 8]\n"


def test_slice_assignment_length_mismatch_is_chinese():
    with pytest.raises(JishiError) as ei:
        _tree('令 d = [1, 2, 3, 4, 5]\nd[::2] = [1, 2]\n')
    assert "长度" in ei.value.message
    assert "extended slice" not in ei.value.message


def test_slice_of_text_is_read_only():
    """文本切片只能读；写要报错（Python 同样不允许）。"""
    with pytest.raises(JishiError):
        _tree('令 s = "你好"\ns[0:1] = "x"\n')


# ---------------------------------------------------------------------------
# M24.3 类型标注
# ---------------------------------------------------------------------------

def test_annotation_parses_and_runs():
    assert _all_agree(
        '函数 折扣(价格: 小数, 比例: 小数) -> 小数：\n'
        '    返回 价格 * 比例\n'
        '打印(折扣(100.0, 0.8))\n') == "80.0\n"


def test_annotation_checked_in_all_executors():
    src = '函数 折扣(价格: 小数)：\n    返回 价格\n打印(折扣(100))\n'
    for run in (_tree, _pvm, _cvm):
        with pytest.raises(JishiError) as ei:
            run(src)
        assert "要求是「小数」" in ei.value.message
        assert "整数" in ei.value.message


def test_annotation_error_names_function_and_param():
    with pytest.raises(JishiError) as ei:
        _tree('函数 问候(名字: 文本)：\n    返回 名字\n打印(问候(123))\n')
    assert "问候" in ei.value.message
    assert "名字" in ei.value.message
    assert "第 1 个参数" in (ei.value.hint or "")


def test_all_known_type_names():
    assert _all_agree(
        '函数 f(a: 整数, b: 小数, c: 文本, d: 列表, e: 字典, g: 集合, h: 布尔)：\n'
        '    返回 1\n'
        '打印(f(1, 1.5, "x", [1], {"k": 1}, {1}, 真))\n') == "1\n"


def test_bool_is_not_int():
    """布尔不该被当成整数（Python 里 bool 是 int 子类，这里刻意区分）。"""
    with pytest.raises(JishiError) as ei:
        _tree('函数 f(a: 整数)：\n    返回 a\n打印(f(真))\n')
    assert "布尔" in ei.value.message


def test_unknown_annotation_is_documentation_only():
    """认不出的标注名（自定义类型）只作文档，不误报。"""
    assert _all_agree(
        '函数 处理(订单: 订单, 数量: 整数)：\n'
        '    返回 数量\n'
        '打印(处理("任何东西", 5))\n') == "5\n"


def test_annotation_with_default_value():
    assert _all_agree(
        '函数 f(x: 整数, y: 整数 = 10)：\n    返回 x + y\n'
        '打印(f(1), f(1, 2))\n') == "11 3\n"


def test_annotation_on_method():
    assert _all_agree(
        '类 甲：\n'
        '    函数 设置(自身, 值: 整数)：\n'
        '        自身.值 = 值\n'
        '令 a = 新建 甲()\n'
        'a.设置(3)\n'
        '打印(a.值)\n') == "3\n"


def test_annotation_does_not_affect_unannotated_calls():
    """没写标注的参数完全不检查、零额外指令。"""
    assert _all_agree(
        '函数 f(a, b)：\n    返回 a + b\n打印(f("甲", "乙"))\n') == "甲乙\n"


def test_return_annotation_is_parsed_but_not_enforced():
    """返回标注目前只作文档（参数标注才校验）——这条锁住当前边界。"""
    assert _all_agree(
        '函数 f() -> 文本：\n    返回 123\n打印(f())\n') == "123\n"


def test_annotation_syntax_is_strict_about_type_name():
    with pytest.raises(JishiError):
        _tree('函数 f(x: 1 + 1)：\n    返回 x\n')


# ---------------------------------------------------------------------------
# M24.4 匿名函数
# ---------------------------------------------------------------------------

def test_lambda_basic_and_assigned():
    assert _all_agree(
        '令 加倍 = 函数(x)：x * 2\n打印(加倍(5))\n'
        '打印(函数(x, y)：x + y)\n') == "10\n<函数 匿名函数>\n"


def test_lambda_with_default_value():
    assert _all_agree(
        '令 f = 函数(x, y = 100)：x + y\n打印(f(1), f(1, 2))\n'
    ) == "101 3\n"


def test_lambda_captures_closure():
    assert _all_agree(
        '令 基数 = 10\n令 f = 函数(x)：x + 基数\n打印(f(5))\n'
    ) == "15\n"


def test_lambda_curried():
    assert _all_agree(
        '令 加 = 函数(x)：函数(y)：x + y\n打印(加(3)(4))\n') == "7\n"


def test_lambda_passed_to_named_function():
    assert _all_agree(
        '函数 应用(f, v)：\n    返回 f(v)\n'
        '打印(应用(函数(x)：x * x, 6))\n') == "36\n"


def test_lambda_in_named_function_body():
    assert _all_agree(
        '函数 造(基数)：\n'
        '    返回 函数(x)：x + 基数\n'
        '令 f = 造(100)\n'
        '打印(f(1))\n') == "101\n"


def test_lambda_with_annotation():
    assert _all_agree(
        '令 f = 函数(x: 整数)：x * 2\n打印(f(3))\n') == "6\n"


def test_lambda_annotation_is_checked():
    with pytest.raises(JishiError) as ei:
        _tree('令 f = 函数(x: 整数)：x * 2\n打印(f("甲"))\n')
    assert "要求是「整数」" in ei.value.message


def test_lambda_requires_single_line():
    with pytest.raises(JishiError) as ei:
        parse(tokenize('令 f = 函数(x)：\n', "<t>"), ["令 f = 函数(x)：", ""], "<t>")
    assert "一行" in ei.value.message


# ---------------------------------------------------------------------------
# M24.4 数据流水线（映射 / 过滤 / 排序按 / 归约）
# ---------------------------------------------------------------------------

def test_map_filter_reduce():
    assert _all_agree(
        '令 数 = [1, 2, 3, 4, 5]\n'
        '打印(数.映射(函数(x)：x * x))\n'
        '打印(数.过滤(函数(x)：x % 2 == 1))\n'
        '打印(数.归约(函数(累计, x)：累计 + x, 0))\n'
        '打印(数.归约(函数(累计, x)：累计 * x, 1))\n'
    ) == "[1, 4, 9, 16, 25]\n[1, 3, 5]\n15\n120\n"


def test_sort_by_with_lambda():
    assert _all_agree(
        '令 人 = [{"名字": "小明", "年龄": 20}, {"名字": "小红", "年龄": 18}]\n'
        '打印(人.排序按(函数(p)：p["年龄"]).映射(函数(p)：p["名字"]))\n'
    ) == "['小红', '小明']\n"


def test_sort_by_objects():
    assert _all_agree(
        '类 商品：\n'
        '    函数 初始化(自身, 名, 价)：\n'
        '        自身.名 = 名\n'
        '        自身.价 = 价\n'
        '令 货 = [新建 商品("甲", 30), 新建 商品("乙", 10)]\n'
        '打印(货.排序按(函数(g)：g.价).映射(函数(g)：g.名))\n'
    ) == "['乙', '甲']\n"


def test_pipeline_chain():
    assert _all_agree(
        '令 数 = 范围(10)\n'
        '打印(数.过滤(函数(x)：x % 2 == 0).映射(函数(x)：x * 10))\n'
    ) == "[0, 20, 40, 60, 80]\n"


def test_pipeline_accepts_named_function():
    assert _all_agree(
        '函数 加倍(x)：\n    返回 x * 2\n'
        '打印([1, 2].映射(加倍))\n') == "[2, 4]\n"


def test_pipeline_rejects_non_function():
    with pytest.raises(JishiError) as ei:
        _tree('打印([1, 2].映射(3))\n')
    assert "需要一个函数" in ei.value.message
    assert "匿名函数" in (ei.value.hint or "")


def test_reduce_needs_initial_value():
    with pytest.raises(JishiError) as ei:
        _tree('打印([1, 2].归约(函数(a, b)：a + b))\n')
    assert "2 个" in ei.value.message


def test_sort_by_comparable_keys_ok():
    """同类型排序键正常排序。"""
    assert _all_agree('打印([3, 1, 2].排序按(函数(x)：x))\n') == "[1, 2, 3]\n"


def test_sort_by_incomparable_keys_is_chinese():
    """排序键类型混着来（数字与文本）要报中文错，不能漏出英文。"""
    with pytest.raises(JishiError) as ei:
        _tree('令 混合 = [{"键": 1}, {"键": "甲"}]\n'
              '打印(混合.排序按(函数(x)：x["键"]))\n')
    assert "比较" in ei.value.message


def test_map_on_empty_list():
    assert _all_agree('打印([].映射(函数(x)：x))\n打印([].过滤(函数(x)：真))\n'
                      ) == "[]\n[]\n"


# ---------------------------------------------------------------------------
# 组合与回归防线
# ---------------------------------------------------------------------------

def test_all_m24_features_together():
    src = (
        '类 成绩表：\n'
        '    函数 初始化(自身, 记录: 列表)：\n'
        '        自身.记录 = 记录\n'
        '    函数 迭代(自身)：\n'
        '        返回 自身.记录\n'
        '    函数 及格名字(自身) -> 列表：\n'
        '        返回 自身.记录.过滤(函数(r)：r["分"] >= 60).映射(函数(r)：r["名"])\n'
        '\n'
        '令 表 = 新建 成绩表([{"名": "小明", "分": 90}, {"名": "小红", "分": 50},\n'
        '                    {"名": "小刚", "分": 70}])\n'
        '打印(表.及格名字())\n'
        '打印(集合(表.记录.映射(函数(r)：r["分"])))\n'
        '遍历 r 在 表：\n'
        '    打印(r["名"], r["分"])\n'
        '令 分 = [r["分"] 遍历 r 在 表]\n'
        '分[0:2] = [95, 55]\n'
        '打印(分)\n'
    )
    assert _all_agree(src) == (
        "['小明', '小刚']\n{90, 50, 70}\n"
        "小明 90\n小红 50\n小刚 70\n[95, 55, 70]\n")


def test_list_methods_table_has_pipeline():
    for name in ("映射", "过滤", "排序按", "归约"):
        assert name in R.LIST_METHODS


def test_set_methods_table():
    for name in ("添加", "移除", "丢弃", "包含", "并集", "交集", "差集",
                 "对称差", "清空", "转列表", "复制"):
        assert name in R.SET_METHODS


def test_lambda_is_expression_not_statement():
    """具名 `函数 名(...)：` 仍然是语句（要缩进块），匿名 `函数(...)：` 是表达式。"""
    with pytest.raises(JishiError):
        # 语句位置的 `函数 加倍(x)：x*2` 少了代码块 → 解析报错
        parse(tokenize("函数 加倍(x)：x * 2\n", "<t>"),
              ["函数 加倍(x)：x * 2"], "<t>")


def test_build_slice_read_write_share_one_path():
    """切片读写共用 Slice 求值路径：读改成写、写改成读都不该崩。"""
    assert _all_agree(
        '令 a = [1, 2, 3, 4]\n'
        '令 片段 = a[1:3]\n'
        'a[0:2] = 片段\n'
        '打印(a)\n') == "[2, 3, 3, 4]\n"
