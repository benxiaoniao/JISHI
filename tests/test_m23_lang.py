# -*- coding: utf-8 -*-
"""M23 语言功能扩展（A 档）：断言 / 在·不在 / 是·不是 / 是实例 / 类变量 / 超()。

每个特性都做**三执行器对拍**（树遍历 / Python VM / C VM 逐字节一致），
另加中文报错质量与边界用例。抓 bug 的能力主要来自对拍——语法特性最容易
出现「树遍历对、VM 不对」的偏差。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import cvm_bind                        # noqa: E402
from jishi import opcodes as O                    # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.errors import JishiError               # noqa: E402
from jishi.interpreter import Interpreter         # noqa: E402
from jishi.parser import parse                    # noqa: E402
from jishi.runtime import apply_compare           # noqa: E402
from jishi.tokenizer import tokenize              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 三执行器对拍脚手架
# ---------------------------------------------------------------------------

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
    """断言三执行器输出一致，返回输出（顺便当「对拍」用）。"""
    got = {"树遍历": _tree(src), "Python VM": _pvm(src), "C VM": _cvm(src)}
    assert len(set(got.values())) == 1, f"三执行器输出不一致：{got}"
    return next(iter(got.values()))


# ---------------------------------------------------------------------------
# M23.1 断言
# ---------------------------------------------------------------------------

def test_assert_passes_and_returns_none():
    assert _all_agree('断言(1 == 1, "应该相等")\n打印("过")\n') == "过\n"


def test_assert_raises_assertion_error():
    for run in (_tree, _pvm, _cvm):
        with pytest.raises(JishiError) as ei:
            run('断言(假, "故意失败")\n')
        assert ei.value.code == "E2008"
        assert "故意失败" in ei.value.message


def test_assert_without_message_has_default():
    with pytest.raises(JishiError) as ei:
        _tree("断言(假)\n")
    assert ei.value.code == "E2008"
    assert "条件不成立" in ei.value.message


def test_assert_message_not_duplicated_in_render():
    """标题已是「断言没通过」，消息里不该再重复一遍。"""
    with pytest.raises(JishiError) as ei:
        _tree('断言(假, "甲应该是 2")\n')
    text = ei.value.render()
    assert text.count("断言没通过") == 1
    assert "甲应该是 2" in text


def test_assert_catchable_as_assertion_error():
    out = _all_agree(
        '尝试：\n'
        '    断言(假, "内幕")\n'
        '捕获 断言错误 为 e：\n'
        '    打印("接住:", e.消息)\n')
    assert out == "接住: 内幕\n"


def test_assert_catchable_as_runtime_base():
    out = _all_agree(
        '尝试：\n'
        '    断言(假)\n'
        '捕获 运行期错误 为 e：\n'
        '    打印("基类接住:", e.类型)\n')
    assert out == "基类接住: 断言错误\n"


def test_assert_truthiness_follows_jishi_rules():
    """空/0/空文本/空列表 都算假；非空都算真。"""
    out = _all_agree(
        '断言(1)\n断言("文本")\n断言([1])\n断言(真)\n'
        '尝试：\n    断言(0)\n捕获 断言错误：\n    打印("0 是假")\n'
        '尝试：\n    断言(空)\n捕获 断言错误：\n    打印("空 是假")\n'
        '尝试：\n    断言("")\n捕获 断言错误：\n    打印("空文本 是假")\n'
        '尝试：\n    断言([])\n捕获 断言错误：\n    打印("空列表 是假")\n')
    assert out == "0 是假\n空 是假\n空文本 是假\n空列表 是假\n"


def test_assert_error_carries_position():
    """断言报错要能指回用户写的那一行（内建调用点补位）。"""
    src = '令 甲 = 1\n断言(甲 == 2, "应该是 2")\n'
    with pytest.raises(JishiError) as ei:
        _tree(src)
    e = ei.value
    assert e.line == 2
    assert e.col is not None


# ---------------------------------------------------------------------------
# M23.1 是实例
# ---------------------------------------------------------------------------

_ISINSTANCE_SRC = (
    '类 动物：\n'
    '    函数 初始化(自身)：\n'
    '        自身.名字 = "x"\n'
    '类 狗 继承 动物：\n'
    '    函数 叫(自身)：\n'
    '        返回 "汪"\n'
    '类 猫：\n'
    '    函数 初始化(自身)：\n'
    '        空\n'
    '令 d = 新建 狗()\n'
    '打印(是实例(d, 狗), 是实例(d, 动物), 是实例(d, 猫), 是实例(1, 狗))\n')


def test_isinstance_including_inheritance_chain():
    assert _all_agree(_ISINSTANCE_SRC) == "真 真 假 假\n"


def test_isinstance_three_level_inheritance():
    out = _all_agree(
        '类 甲：\n    函数 a(自身)：\n        返回 1\n'
        '类 乙 继承 甲：\n    函数 b(自身)：\n        返回 2\n'
        '类 丙 继承 乙：\n    函数 c(自身)：\n        返回 3\n'
        '令 x = 新建 丙()\n'
        '打印(是实例(x, 甲), 是实例(x, 乙), 是实例(x, 丙))\n')
    assert out == "真 真 真\n"


def test_isinstance_non_instance_is_false():
    assert _all_agree('类 甲：\n    函数 a(自身)：\n        返回 1\n'
                      '打印(是实例(1, 甲), 是实例("文本", 甲), 是实例(空, 甲))\n'
                      ) == "假 假 假\n"


def test_isinstance_rejects_non_class_with_type_hint():
    """第二参数写成内建转换函数时，提示改用 类型()。"""
    with pytest.raises(JishiError) as ei:
        _tree("打印(是实例(1, 整数))\n")
    assert "整数" in ei.value.hint
    assert "类型(值)" in ei.value.hint


def test_isinstance_rejects_non_class_generically():
    with pytest.raises(JishiError) as ei:
        _tree("打印(是实例(1, 2))\n")
    assert "要是一个类" in ei.value.message


# ---------------------------------------------------------------------------
# M23.2 成员测试 在 / 不在
# ---------------------------------------------------------------------------

def test_membership_in_list():
    assert _all_agree(
        '令 名单 = ["小明", "小红"]\n'
        '打印("小明" 在 名单, "小刚" 在 名单, "小刚" 不在 名单)\n'
    ) == "真 假 真\n"


def test_membership_in_dict_uses_keys():
    assert _all_agree(
        '令 电话 = {"小明": "138"}\n'
        '打印("小明" 在 电话, "138" 在 电话, "小红" 不在 电话)\n'
    ) == "真 假 真\n"


def test_membership_in_text_is_substring():
    assert _all_agree(
        '打印("明" 在 "小明", "小明" 在 "小明", "大" 不在 "小明")\n'
    ) == "真 真 真\n"


def test_membership_in_range():
    assert _all_agree("打印(1 在 范围(5), 9 在 范围(5))\n") == "真 假\n"


def test_membership_in_comprehension_result():
    assert _all_agree(
        '令 偶 = [x 遍历 x 在 范围(10) 如果 x % 2 == 0]\n'
        '打印(4 在 偶, 5 不在 偶)\n') == "真 真\n"


def test_membership_bad_container_gives_chinese_error():
    for run in (_tree, _pvm, _cvm):
        with pytest.raises(JishiError) as ei:
            run("打印(1 在 5)\n")
        assert "不能在" in ei.value.message
        assert "列表" in ei.value.hint


def test_membership_in_condition_and_loop():
    assert _all_agree(
        '令 允许 = ["甲", "乙"]\n'
        '遍历 名字 在 ["甲", "丙", "乙"]：\n'
        '    如果 名字 在 允许：\n'
        '        打印(名字, "允许")\n'
        '    否则：\n'
        '        打印(名字, "拒绝")\n'
    ) == "甲 允许\n丙 拒绝\n乙 允许\n"


# ---------------------------------------------------------------------------
# M23.2 同一性 是 / 不是
# ---------------------------------------------------------------------------

def test_identity_with_none():
    assert _all_agree(
        '令 x = 空\n打印(x 是 空, x 不是 空, 1 是 空, 0 不是 空)\n'
    ) == "真 假 假 真\n"


def test_identity_with_bool_literals():
    assert _all_agree("打印(真 是 真, 真 不是 假, 0 是 假)\n"
                      ) == "真 真 假\n"


def test_identity_compares_numbers_by_value():
    """数字/文本按值比较 —— 避免 Python 的 `is` 给出「时真时假」的随机感。"""
    assert _all_agree(
        '打印(1 是 1, 300 是 300, 1.5 是 1.5, "甲" 是 "甲")\n'
    ) == "真 真 真 真\n"


def test_identity_of_containers_is_by_object():
    """两个内容相同的列表不是同一个东西。"""
    assert _all_agree(
        '令 甲 = [1, 2]\n'
        '令 乙 = [1, 2]\n'
        '令 丙 = 甲\n'
        '打印(甲 是 乙, 甲 是 丙, 甲 不是 乙)\n'
    ) == "假 真 真\n"


def test_identity_of_instances():
    assert _all_agree(
        '类 甲：\n    函数 a(自身)：\n        返回 1\n'
        '令 x = 新建 甲()\n'
        '令 y = 新建 甲()\n'
        '令 z = x\n'
        '打印(x 是 y, x 是 z)\n') == "假 真\n"


def test_identity_functions_by_position():
    assert _all_agree(
        '函数 f()：\n    返回 1\n'
        '令 g = f\n'
        '打印(f 是 g, f 不是 g)\n') == "真 假\n"


# ---------------------------------------------------------------------------
# M23.2 链式比较与优先级
# ---------------------------------------------------------------------------

def test_chain_with_keyword_comparators():
    assert _all_agree(
        '令 甲 = 5\n'
        '如果 1 < 甲 不是 空：\n'
        '    打印("过了")\n') == "过了\n"


def test_membership_has_comparison_precedence():
    """`1 + 1 在 [2]` 应解析成 `(1+1) 在 [2]`（算术比比较紧）。"""
    assert _all_agree("打印(1 + 1 在 [2])\n") == "真\n"


def test_not_precedence_is_python_like():
    """`非` 比比较松、比「与/或」紧（M23 修正，与 Python 的 not 一致）。

    修正前 `非 年龄 >= 18` 会被解析成 `(非 年龄) >= 18`，恒为假 ——
    官方教程就踩过这个坑（「未成年人」分支永不触发，静默给错答案）。
    """
    assert _all_agree(
        '令 年龄 = 10\n'
        '如果 非 年龄 >= 18：\n'
        '    打印("未成年")\n'
        '令 年龄2 = 20\n'
        '如果 非 年龄2 >= 18：\n'
        '    打印("不该进")\n'
        '否则：\n'
        '    打印("成年")\n') == "未成年\n成年\n"


def test_not_binds_looser_than_comparison():
    assert _all_agree("打印(非 1 == 2, 非 1 == 1)\n") == "真 假\n"


def test_not_binds_tighter_than_and_or():
    assert _all_agree(
        '令 甲 = 真\n'
        '打印(非 甲 与 假, 非 假 与 真, 非 (假 与 真))\n'
    ) == "假 真 真\n"


def test_not_works_with_membership():
    assert _all_agree(
        '令 名单 = [1, 2]\n'
        '打印(非 9 在 名单, 非 1 在 名单)\n') == "真 假\n"


def test_english_not_in_hint_suggests_bu_zai():
    """英文 `not in` 要提示「不在」（只提示「非」会把人带偏）。"""
    with pytest.raises(JishiError) as ei:
        _tree("打印(1 not in [1])\n")
    assert "不在" in ei.value.message
    assert ei.value.fix == {"old": "not in", "new": "不在"}


def test_english_is_not_hint_suggests_bu_shi():
    with pytest.raises(JishiError) as ei:
        _tree("打印(1 is not 空)\n")
    assert "不是" in ei.value.message


# ---------------------------------------------------------------------------
# M23.3 类变量
# ---------------------------------------------------------------------------

_CLASSVAR_SRC = (
    '类 计数器：\n'
    '    总数 = 0\n'
    '    名字 = "计数器"\n'
    '    函数 初始化(自身)：\n'
    '        自身.编号 = 1\n'
    '    函数 加(自身, n)：\n'
    '        计数器.总数 += n\n'
    '        返回 计数器.总数\n'
    '令 a = 新建 计数器()\n'
    '令 b = 新建 计数器()\n'
    '打印(计数器.总数, 计数器.名字)\n'
    '打印(a.加(3), a.加(2))\n'
    '打印(b.加(10))\n'
    '打印(a.总数, b.总数)\n'
)


def test_class_var_shared_across_instances():
    assert _all_agree(_CLASSVAR_SRC) == "0 计数器\n3 5\n15\n15 15\n"


def test_class_var_assignment_via_class_name():
    assert _all_agree(
        '类 甲：\n    值 = 1\n    函数 a(自身)：\n        返回 1\n'
        '甲.值 = 9\n打印(甲.值)\n') == "9\n"


def test_class_var_read_from_instance():
    assert _all_agree(
        '类 甲：\n    值 = 7\n    函数 a(自身)：\n        返回 1\n'
        '令 x = 新建 甲()\n打印(x.值)\n') == "7\n"


def test_instance_field_shadows_class_var():
    """实例字段优先于同名类变量（与 Python 一致）。"""
    assert _all_agree(
        '类 甲：\n    值 = 1\n    函数 改(自身)：\n        自身.值 = 99\n'
        '令 x = 新建 甲()\n'
        'x.改()\n'
        '打印(x.值, 甲.值)\n') == "99 1\n"


def test_class_var_inherited_and_overridable():
    assert _all_agree(
        '类 甲：\n    值 = 1\n    函数 a(自身)：\n        返回 1\n'
        '类 乙 继承 甲：\n    值 = 2\n    函数 b(自身)：\n        返回 2\n'
        '类 丙 继承 甲：\n    函数 c(自身)：\n        返回 3\n'
        '令 y = 新建 乙()\n'
        '令 z = 新建 丙()\n'
        '打印(乙.值, 甲.值, 丙.值, y.值, z.值)\n') == "2 1 1 2 1\n"


def test_class_var_can_use_outer_name():
    """类变量的值在「定义类的外层作用域」求值。"""
    assert _all_agree(
        '令 基数 = 10\n'
        '类 甲：\n'
        '    值 = 基数 * 2\n'
        '    函数 a(自身)：\n        返回 1\n'
        '打印(甲.值)\n') == "20\n"


def test_class_var_mutable_shared_object():
    """浅拷贝：基类里的可变类变量被两边共享（与 Python 一致）。"""
    assert _all_agree(
        '类 甲：\n'
        '    记录 = []\n'
        '    函数 记(自身, v)：\n        甲.记录.追加(v)\n'
        '令 a = 新建 甲()\n'
        '令 b = 新建 甲()\n'
        'a.记(1)\n'
        'b.记(2)\n'
        '打印(a.记录)\n') == "[1, 2]\n"


def test_class_body_rejects_other_statements():
    with pytest.raises(JishiError) as ei:
        _tree('类 甲：\n    如果 真：\n        打印(1)\n')
    assert "类体" in ei.value.message


# ---------------------------------------------------------------------------
# M23.4 超() 调父类
# ---------------------------------------------------------------------------

_SUPER_SRC = (
    '类 动物：\n'
    '    函数 初始化(自身, 名字)：\n'
    '        自身.名字 = 名字\n'
    '    函数 叫(自身)：\n'
    '        返回 自身.名字 + "叫了一声"\n'
    '类 狗 继承 动物：\n'
    '    函数 初始化(自身, 名字, 品种)：\n'
    '        超().初始化(名字)\n'
    '        自身.品种 = 品种\n'
    '    函数 叫(自身)：\n'
    '        返回 超().叫() + "，汪汪"\n'
    '令 d = 新建 狗("旺财", "柴犬")\n'
    '打印(d.名字, d.品种)\n'
    '打印(d.叫())\n')


def test_super_calls_base_constructor():
    assert _all_agree(_SUPER_SRC) == "旺财 柴犬\n旺财叫了一声，汪汪\n"


def test_super_multilevel_is_not_self_recursive():
    """多级继承：`超()` 指向「定义类的基类」，不会绕回自己。"""
    out = _all_agree(
        '类 甲：\n    函数 说(自身)：\n        返回 "甲"\n'
        '类 乙 继承 甲：\n    函数 说(自身)：\n        返回 超().说() + "乙"\n'
        '类 丙 继承 乙：\n    函数 说(自身)：\n        返回 超().说() + "丙"\n'
        '令 x = 新建 丙()\n'
        '打印(x.说())\n')
    assert out == "甲乙丙\n"


def test_super_in_nested_chain_constructed_correctly():
    assert _all_agree(
        '类 甲：\n    函数 初始化(自身)：\n        自身.路径 = "甲"\n'
        '类 乙 继承 甲：\n'
        '    函数 初始化(自身)：\n'
        '        超().初始化()\n'
        '        自身.路径 = 自身.路径 + "乙"\n'
        '类 丙 继承 乙：\n'
        '    函数 初始化(自身)：\n'
        '        超().初始化()\n'
        '        自身.路径 = 自身.路径 + "丙"\n'
        '令 x = 新建 丙()\n'
        '打印(x.路径)\n') == "甲乙丙\n"


def test_super_with_arguments_passes_through():
    assert _all_agree(
        '类 甲：\n'
        '    函数 求和(自身, a, b)：\n        返回 a + b\n'
        '类 乙 继承 甲：\n'
        '    函数 求和(自身, a, b)：\n        返回 超().求和(a, b) * 10\n'
        '令 x = 新建 乙()\n'
        '打印(x.求和(2, 3))\n') == "50\n"


def test_super_reads_base_class_var():
    assert _all_agree(
        '类 甲：\n    值 = 5\n    函数 a(自身)：\n        返回 1\n'
        '类 乙 继承 甲：\n    函数 读(自身)：\n        返回 超().值\n'
        '令 x = 新建 乙()\n'
        '打印(x.读())\n') == "5\n"


def test_super_without_base_gives_clear_error():
    with pytest.raises(JishiError) as ei:
        _tree('类 甲：\n    函数 喊(自身)：\n        返回 超().喊()\n'
              '令 x = 新建 甲()\nx.喊()\n')
    assert "没有基类" in ei.value.message


def test_super_missing_method_lists_available():
    with pytest.raises(JishiError) as ei:
        _tree('类 甲：\n    函数 好的(自身)：\n        返回 1\n'
              '类 乙 继承 甲：\n    函数 b(自身)：\n        返回 超().没有的()\n'
              '令 x = 新建 乙()\nx.b()\n')
    assert "没有" in ei.value.message
    assert "好的" in (ei.value.hint or "")


def test_super_outside_class_is_parse_error():
    with pytest.raises(JishiError) as ei:
        parse(tokenize("超().叫()\n", "<t>"), ["超().叫()"], "<t>")
    assert "只能在类的方法里用" in ei.value.message
    assert "提示" in ei.value.render()


def test_super_in_method_without_self_param():
    with pytest.raises(JishiError) as ei:
        parse(tokenize('类 甲：\n    函数 f()：\n        返回 超().f()\n', "<t>"),
              ["", "", ""], "<t>")
    assert "自身" in ei.value.message


# ---------------------------------------------------------------------------
# 交叉验证与回归防线
# ---------------------------------------------------------------------------

def test_opcode_table_covers_new_comparisons():
    """新增的语义比较必须在 SYMBOL_TO_CMPOP 里（编译器靠它查码）。"""
    for sym in ("在", "不在", "是", "不是"):
        assert sym in O.SYMBOL_TO_CMPOP
    # CMPOP_TO_SYMBOL 是反向表（码 → 符号），C VM 回调靠它还原语义
    for code, sym in ((O.CmpOp.IN, "在"), (O.CmpOp.NOT_IN, "不在"),
                      (O.CmpOp.IS, "是"), (O.CmpOp.IS_NOT, "不是")):
        assert O.CMPOP_TO_SYMBOL[code] == sym


def test_new_cmpops_are_outside_cvm_fast_path():
    """0-5 是 C VM 的快路径，新码必须 > 5 才能落回宿主（零 C 改动的前提）。"""
    new = [O.CmpOp.IN, O.CmpOp.NOT_IN, O.CmpOp.IS, O.CmpOp.IS_NOT]
    assert all(code > O.CmpOp.GE for code in new)


def test_apply_compare_directly():
    assert apply_compare("在", 1, [1, 2]) is True
    assert apply_compare("不在", 3, [1, 2]) is True
    assert apply_compare("是", None, None) is True
    assert apply_compare("不是", 1, None) is True


def test_keywords_do_not_break_common_identifiers():
    """`不在列表`、`不是我` 这类正常变量名不能被当成关键字粘连。"""
    out = _all_agree(
        '令 不在列表 = 真\n'
        '令 不是我 = 假\n'
        '令 是实例化的 = 1\n'
        '打印(不在列表, 不是我, 是实例化的)\n')
    assert out == "真 假 1\n"


def test_existing_builtin_name_still_works():
    """`是实例` 是内建名，不能被分词器切坏。"""
    assert _all_agree(_ISINSTANCE_SRC) == "真 真 假 假\n"


def test_roundtrip_features_together():
    """把六项特性写在同一段程序里，三执行器仍要一致。"""
    src = (
        '类 库存：\n'
        '    上限 = 100\n'
        '    函数 初始化(自身, 货)：\n'
        '        自身.货 = 货\n'
        '    函数 加(自身, n)：\n'
        '        断言(n > 0, "数量要为正")\n'
        '        如果 自身.货 + n > 库存.上限：\n'
        '            抛出 值错误("超过上限")\n'
        '        自身.货 += n\n'
        '        返回 自身.货\n'
        '类 特价库存 继承 库存：\n'
        '    上限 = 50\n'
        '    函数 加(自身, n)：\n'
        '        断言("特价" 不是 空)\n'
        '        返回 超().加(n)\n'
        '令 s = 新建 特价库存(10)\n'
        '打印(s.加(5))\n'
        '打印(是实例(s, 库存), 是实例(s, 特价库存))\n'
        '打印(库存.上限, 特价库存.上限)\n'
        '打印(5 在 范围(10), 11 不在 范围(10))\n')
    assert _all_agree(src) == "15\n真 真\n100 50\n真 真\n"
