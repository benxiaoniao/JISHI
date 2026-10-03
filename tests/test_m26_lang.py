# -*- coding: utf-8 -*-
"""M26（语言功能 C 档）：自定义对象打印 / 上下文管理器 `用 … 为` / 精确小数。

三项都是「标准库优先」型，只有 `用 … 为` 碰语法——而它在**解析期脱糖**成
「绑定 + 尝试/最终」（`进入上下文`/`退出上下文` 只是普通内建），所以三个
执行器一行都没改。这里每条断言都跑**三执行器对拍**，用来锁住这个结论：
以后谁改动执行器，脱糖出来的语义漂了就会被抓出来。

顺带覆盖一处既有缺陷：树遍历侧 `_parse_try` 曾有两份定义（后者覆盖前者），
死代码已清掉。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import cvm_bind                        # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.errors import JishiError               # noqa: E402
from jishi.formatter import format_source         # noqa: E402
from jishi.interpreter import Interpreter         # noqa: E402
from jishi.parser import parse                    # noqa: E402
from jishi.tokenizer import tokenize              # noqa: E402


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


def _p(path: Path) -> str:
    """路径转成基石源码里能直接用的写法（正斜杠，省得和转义符打架）。"""
    return str(path).replace("\\", "/")


# ---------------------------------------------------------------------------
# 自定义对象打印（文本() 方法）
# ---------------------------------------------------------------------------

def test_custom_text_method_used_by_print():
    assert _all_agree(
        '类 分数：\n'
        '    函数 初始化(自身, 分子, 分母)：\n'
        '        自身.分子 = 分子\n'
        '        自身.分母 = 分母\n'
        '    函数 文本(自身)：\n'
        '        返回 文本(自身.分子) + "/" + 文本(自身.分母)\n'
        '打印(新建 分数(1, 3))\n') == "1/3\n"


def test_custom_text_method_used_by_str_builtin():
    assert _all_agree(
        '类 甲：\n'
        '    函数 文本(自身)：\n'
        '        返回 "甲"\n'
        '打印(文本(新建 甲()))\n') == "甲\n"


def test_custom_text_inside_containers():
    """列表/字典里的对象也跟着用「文本」——打印容器时不会掉回 <甲 实例>。"""
    assert _all_agree(
        '类 甲：\n'
        '    函数 文本(自身)：\n'
        '        返回 "甲"\n'
        '令 a = 新建 甲()\n'
        '打印([a, a])\n'
        '打印({"键": a})\n') == "[甲, 甲]\n{'键': 甲}\n"


def test_custom_text_is_inherited():
    assert _all_agree(
        '类 基：\n'
        '    函数 文本(自身)：\n'
        '        返回 "基"\n'
        '类 子 继承 基：\n'
        '    函数 其他(自身)：\n'
        '        返回 1\n'
        '打印(新建 子())\n') == "基\n"


def test_without_text_method_keeps_old_display():
    """没写「文本」时保持原样，避免破坏既有代码的输出。"""
    assert _all_agree(
        '类 裸：\n'
        '    函数 初始化(自身)：\n'
        '        自身.x = 1\n'
        '打印(新建 裸())\n') == "<裸 实例>\n"


def test_text_method_must_return_text():
    with pytest.raises(JishiError) as ei:
        _tree('类 甲：\n'
              '    函数 文本(自身)：\n'
              '        返回 1\n'
              '打印(新建 甲())\n')
    assert "要返回文本" in ei.value.message
    assert "整数" in ei.value.message


# ---------------------------------------------------------------------------
# 上下文管理器 用 … 为
# ---------------------------------------------------------------------------

def test_with_basic_read_write(tmp_path):
    f = _p(tmp_path / "a.txt")
    assert _all_agree(
        f'用 打开("{f}", "写") 为 文件：\n'
        '    文件.写行("第一行")\n'
        '    文件.写行("第二行")\n'
        f'用 打开("{f}") 为 文件：\n'
        '    遍历 行 在 文件：\n'
        '        打印(行)\n') == "第一行\n第二行\n"


def test_with_closes_file_after_block(tmp_path):
    """块结束后文件自动关闭——拿块外的名字再读会被中文报错拦住。"""
    f = _p(tmp_path / "b.txt")
    with pytest.raises(JishiError) as ei:
        _tree(f'用 打开("{f}", "写") 为 文件：\n'
              '    文件.写("内容")\n'
              '文件.读()\n')
    assert "已经关闭" in ei.value.message


def test_with_closes_on_exception():
    src = ('类 甲：\n'
           '    函数 初始化(自身)：\n'
           '        自身.关 = 假\n'
           '    函数 关闭(自身)：\n'
           '        自身.关 = 真\n'
           '令 对象 = 新建 甲()\n'
           '尝试：\n'
           '    用 对象 为 句柄：\n'
           '        抛出 "出错了"\n'
           '捕获 为 e：\n'
           '    打印("接住了")\n'
           '打印(对象.关)\n')
    assert _all_agree(src) == "接住了\n真\n"


def test_with_closes_on_return():
    assert _all_agree(
        '类 甲：\n'
        '    函数 初始化(自身)：\n'
        '        自身.关 = 假\n'
        '    函数 关闭(自身)：\n'
        '        自身.关 = 真\n'
        '函数 试()：\n'
        '    令 对象 = 新建 甲()\n'
        '    用 对象 为 句柄：\n'
        '        返回 对象.关\n'
        '    返回 "没走到"\n'
        '打印(试())\n') == "假\n"


def test_with_closes_on_break():
    assert _all_agree(
        '类 甲：\n'
        '    函数 初始化(自身)：\n'
        '        自身.关 = 假\n'
        '    函数 关闭(自身)：\n'
        '        自身.关 = 真\n'
        '令 对象 = 新建 甲()\n'
        '遍历 i 在 [1, 2, 3]：\n'
        '    用 对象 为 句柄：\n'
        '        中断\n'
        '打印(对象.关)\n') == "真\n"


def test_with_custom_enter_and_exit():
    """有「进入」时绑定的是它的返回值，且「退出」优先于「关闭」。"""
    assert _all_agree(
        '类 日志：\n'
        '    函数 初始化(自身)：\n'
        '        自身.足迹 = []\n'
        '    函数 进入(自身)：\n'
        '        自身.足迹.追加("进入")\n'
        '        返回 "值"\n'
        '    函数 退出(自身)：\n'
        '        自身.足迹.追加("退出")\n'
        '    函数 关闭(自身)：\n'
        '        自身.足迹.追加("关闭")\n'
        '令 记 = 新建 日志()\n'
        '用 记 为 v：\n'
        '    记.足迹.追加(v)\n'
        '打印(记.足迹)\n') == "['进入', '值', '退出']\n"


def test_with_exit_targets_original_object_not_entered_value():
    """`进入` 返回别的对象时，退出仍要作用在**原来的上下文对象**上。

    这是脱糖方案的真实陷阱：初版把「绑定后的名字」直接传给了 `退出上下文`，
    于是 `进入` 一返回字符串/包装对象，退出就找错对象、静默失效（写这批
    测试时被抓到）。现在多留一个临时变量保存原对象——与 Python 的
    `__enter__` / `__exit__` 语义一致。
    """
    assert _all_agree(
        '类 包装：\n'
        '    函数 初始化(自身)：\n'
        '        自身.退出过 = 假\n'
        '    函数 进入(自身)：\n'
        '        返回 {"内容": 1}\n'
        '    函数 退出(自身)：\n'
        '        自身.退出过 = 真\n'
        '令 包 = 新建 包装()\n'
        '用 包 为 数据：\n'
        '    打印(数据["内容"])\n'
        '打印(包.退出过)\n') == "1\n真\n"


def test_with_only_close_method_uses_itself():
    assert _all_agree(
        '类 门：\n'
        '    函数 初始化(自身)：\n'
        '        自身.关 = 假\n'
        '    函数 关闭(自身)：\n'
        '        自身.关 = 真\n'
        '令 门1 = 新建 门()\n'
        '用 门1 为 g：\n'
        '    打印(g.关)\n'
        '打印(门1.关)\n') == "假\n真\n"


def test_with_nested(tmp_path):
    f1 = _p(tmp_path / "n1.txt")
    f2 = _p(tmp_path / "n2.txt")
    assert _all_agree(
        f'用 打开("{f1}", "写") 为 a：\n'
        f'    a.写("甲")\n'
        f'    用 打开("{f2}", "写") 为 b：\n'
        f'        b.写("乙")\n'
        f'打印(打开("{f1}").读() + 打开("{f2}").读())\n') == "甲乙\n"


def test_with_rejects_non_context():
    with pytest.raises(JishiError) as ei:
        _tree('用 [1, 2] 为 f：\n    打印(f)\n')
    assert "不能当上下文管理器用" in ei.value.message
    assert "列表" in ei.value.message


def test_with_requires_as_keyword():
    with pytest.raises(JishiError) as ei:
        _tree('用 打开("x.txt")：\n    打印(1)\n')
    assert "「为」" in ei.value.message


def test_with_is_not_a_plain_expression():
    """`用` 成了关键字：不能再用它当变量名（如实锁住这个代价）。"""
    with pytest.raises(JishiError):
        _tree('令 用 = 1\n')


# ---------------------------------------------------------------------------
# 文件对象
# ---------------------------------------------------------------------------

def test_open_read_all_and_readline(tmp_path):
    f = _p(tmp_path / "d.txt")
    assert _all_agree(
        f'用 打开("{f}", "写") 为 文件：\n'
        '    文件.写("甲\\n乙\\n")\n'
        f'用 打开("{f}") 为 文件：\n'
        '    打印(文件.读())\n'
        f'用 打开("{f}") 为 文件：\n'
        '    打印(文件.读行())\n'
        '    打印(文件.读行())\n'
        '    打印(文件.读行())\n') == "甲\n乙\n\n甲\n乙\n空\n"


def test_open_read_all_lines(tmp_path):
    f = _p(tmp_path / "e.txt")
    assert _all_agree(
        f'用 打开("{f}", "写") 为 文件：\n'
        '    文件.写行("甲")\n'
        '    文件.写行("乙")\n'
        f'用 打开("{f}") 为 文件：\n'
        '    打印(文件.读所有行())\n') == "['甲', '乙']\n"


def test_open_append_mode(tmp_path):
    f = _p(tmp_path / "f.txt")
    assert _all_agree(
        f'用 打开("{f}", "写") 为 文件：\n'
        '    文件.写("一")\n'
        f'用 打开("{f}", "追加") 为 文件：\n'
        '    文件.写("二")\n'
        f'用 打开("{f}") 为 文件：\n'
        '    打印(文件.读())\n') == "一二\n"


def test_open_accepts_english_mode(tmp_path):
    f = _p(tmp_path / "g.txt")
    assert _all_agree(
        f'用 打开("{f}", "w") 为 文件：\n'
        '    文件.写("x")\n'
        f'用 打开("{f}", "r") 为 文件：\n'
        '    打印(文件.读())\n') == "x\n"


def test_file_utf8_chinese_roundtrip(tmp_path):
    f = _p(tmp_path / "h.txt")
    assert _all_agree(
        f'用 打开("{f}", "写") 为 文件：\n'
        '    文件.写行("你好，世界")\n'
        f'用 打开("{f}") 为 文件：\n'
        '    打印(文件.读行())\n') == "你好，世界\n"


def test_file_position_and_seek(tmp_path):
    f = _p(tmp_path / "i.txt")
    assert _all_agree(
        f'用 打开("{f}", "写") 为 文件：\n'
        '    文件.写("abcdef")\n'
        f'用 打开("{f}") 为 文件：\n'
        '    文件.读(2)\n'
        '    打印(文件.位置())\n'
        '    文件.定位(0)\n'
        '    打印(文件.读(1))\n') == "2\na\n"


def test_print_of_none_and_bool_is_chinese():
    """`打印(空)` 显示「空」、布尔显示「真/假」，与 `类型()` 一致（M27）。

    这条曾经是「如实锁住不一致」：M26 时 `打印` 给的是 `None`/`True`/`False`，
    而 REPL 回显与 `类型()` 给中文。M27 把值→文本收敛到 `jishi_repr` 一处，
    不一致消除，这条测试改成正面断言。
    """
    assert _all_agree('打印(空, 真, 假)\n') == "空 真 假\n"
    assert _all_agree('打印(类型(空), 类型(真))\n') == "空 布尔\n"


def test_file_repeated_close_is_fine(tmp_path):
    f = _p(tmp_path / "j.txt")
    assert _all_agree(
        f'用 打开("{f}", "写") 为 文件：\n'
        '    文件.写("x")\n'
        '    文件.关闭()\n') == ""


def test_open_missing_file_is_chinese():
    with pytest.raises(JishiError) as ei:
        _tree('用 打开("build/绝对不存在的文件.txt") 为 文件：\n    打印(1)\n')
    assert "找不到文件或目录" in ei.value.message
    assert "No such file" not in ei.value.message      # 不混英文原文


def test_open_bad_mode_is_chinese(tmp_path):
    f = _p(tmp_path / "k.txt")
    with pytest.raises(JishiError) as ei:
        _tree(f'用 打开("{f}", "乱写") 为 文件：\n    打印(1)\n')
    assert "不认识的打开模式" in ei.value.message


def test_open_non_text_path_is_chinese():
    with pytest.raises(JishiError) as ei:
        _tree('打印(打开(123))\n')
    assert "第一个参数要是文件路径" in ei.value.message


def test_file_write_requires_text(tmp_path):
    f = _p(tmp_path / "l.txt")
    with pytest.raises(JishiError) as ei:
        _tree(f'用 打开("{f}", "写") 为 文件：\n    文件.写(42)\n')
    assert "需要文本" in ei.value.message


def test_file_prints_path_and_state(tmp_path):
    f = _p(tmp_path / "m.txt")
    assert _all_agree(
        f'令 文件 = 打开("{f}", "写")\n'
        '打印(文件)\n'
        '文件.关闭()\n'
        '打印(文件)\n') == f"<文件 {f}（写）>\n<文件 {f}（已关闭）>\n"


# ---------------------------------------------------------------------------
# 精确小数
# ---------------------------------------------------------------------------

def test_decimal_fixes_the_float_gap():
    """同一句比较，普通小数是假、精确小数是真——这就是它存在的理由。"""
    assert _all_agree(
        '打印(0.1 + 0.2 == 0.3)\n'
        '打印(精确("0.1") + 精确("0.2") == 精确("0.3"))\n') == "假\n真\n"


def test_decimal_converts_float_without_binary_tail():
    """精确(0.1) 走 str 转换，不会把 0.1000000000000000055… 带进来。"""
    assert _all_agree('打印(精确(0.1) + 精确(0.2))\n') == "0.3\n"


def test_decimal_arithmetic():
    assert _all_agree(
        '令 价 = 精确("19.99")\n'
        '打印(价 * 3)\n'
        '打印(价 - 精确("20"))\n'
        '打印(价 + 精确("0.01"))\n'
        '打印(精确(3) * 精确(4))\n') == "59.97\n-0.01\n20.00\n12\n"


def test_decimal_division_keeps_precision():
    assert _all_agree(
        '打印(精确("10") / 精确("4"))\n'
        '打印(精确("1") / 精确("3"))\n'
    ) == "2.5\n0.3333333333333333333333333333\n"


def test_decimal_compare_with_plain_number():
    assert _all_agree(
        '打印(精确("0.3") == 0.3)\n'
        '打印(精确("1") < 2)\n'
        '打印(精确("2") >= 精确("2"))\n') == "真\n真\n真\n"


def test_decimal_accumulation_is_exact():
    """累加 0.1/0.2/0.3 三次，普通小数会飘，精确小数不会。"""
    assert _all_agree(
        '令 总 = 0.0\n'
        '遍历 x 在 [0.1, 0.2, 0.3]：\n'
        '    总 += x\n'
        '打印(总)\n'
        '令 精确总 = 精确(0)\n'
        '遍历 x 在 ["0.1", "0.2", "0.3"]：\n'
        '    精确总 += 精确(x)\n'
        '打印(精确总)\n') == "0.6000000000000001\n0.6\n"


def test_decimal_rounding_is_half_up():
    """金额场景按「四舍五入」，不是 Python 默认的银行家舍入。"""
    assert _all_agree(
        '打印(精确("2.5").舍入(0))\n'
        '打印(精确("2.345").舍入(2))\n'
        '打印(精确("1.005").舍入(2))\n'
        '打印(精确("1.5").舍入())\n'
        '打印(精确("1234.5").舍入(-2))\n') == "3\n2.35\n1.01\n1.50\n1200\n"


def test_decimal_negative_and_abs():
    assert _all_agree(
        '打印(-精确("1.5"))\n'
        '打印(精确("-3.7").绝对值())\n') == "-1.5\n3.7\n"


def test_decimal_type_and_text():
    assert _all_agree(
        '打印(类型(精确("1")))\n'
        '打印(文本(精确("1.50")))\n'
        '打印(整数(精确("3.9")))\n'
        '打印(小数(精确("3.5")))\n'
        '打印(精确(3))\n') == "精确小数\n1.50\n3\n3.5\n3\n"


def test_decimal_works_with_builtins():
    """总和/最大/最小/排序 对精确小数也要能用（走 Python 侧比较与加法）。"""
    assert _all_agree(
        '打印(总和([精确("0.1"), 精确("0.2"), 精确("0.3")]))\n'
        '打印(最大([精确("1.5"), 精确("9.25")]))\n'
        '打印(最小([精确("1.5"), 精确("9.25")]))\n'
        '令 表 = [精确("3.3"), 精确("1.1"), 精确("2.2")]\n'
        '表.排序()\n'
        '打印(表)\n') == "0.6\n9.25\n1.5\n[1.1, 2.2, 3.3]\n"


def test_decimal_membership():
    assert _all_agree(
        '打印(精确("1") 在 [精确("1"), 精确("2")])\n'
        '打印(精确("3") 在 [精确("1")])\n') == "真\n假\n"


def test_decimal_bad_text_is_chinese():
    with pytest.raises(JishiError) as ei:
        _tree('打印(精确("abc"))\n')
    assert "不能把「abc」当作精确小数" in ei.value.message


def test_decimal_rejects_bool():
    with pytest.raises(JishiError) as ei:
        _tree('打印(精确(真))\n')
    assert "布尔值" in ei.value.message


def test_decimal_rejects_container():
    with pytest.raises(JishiError) as ei:
        _tree('打印(精确([1]))\n')
    assert "不能把「列表」转成精确小数" in ei.value.message


def test_decimal_divide_by_zero():
    with pytest.raises(JishiError) as ei:
        _tree('打印(精确("1") / 精确("0"))\n')
    assert ei.value.code == "E2001"      # 与普通除零同一个错误类型


def test_decimal_round_digits_must_be_int():
    with pytest.raises(JishiError) as ei:
        _tree('打印(精确("1").舍入("两位"))\n')
    assert "位数要是整数" in ei.value.message


def test_decimal_does_not_change_plain_float_semantics():
    """普通小数仍然是二进制浮点（不偷偷改成十进制）。"""
    assert _all_agree(
        '打印(0.1 + 0.2)\n'
        '打印(类型(0.1))\n'
        '打印(1 / 3)\n') == "0.30000000000000004\n小数\n0.3333333333333333\n"


# ---------------------------------------------------------------------------
# 工具链兼容
# ---------------------------------------------------------------------------

def test_formatter_is_idempotent_with_with_statement():
    src = ('用 打开("数据.txt") 为 文件：\n'
           '    文件.写行("甲")\n'
           '用 对象 为 句柄：\n'
           '    打印(句柄)\n')
    once = format_source(src)
    assert once == format_source(once)
    assert '用 打开("数据.txt") 为 文件：' in once


def test_formatter_is_idempotent_with_exact():
    src = '令 价 = 精确("19.99")\n打印(价 * 2)\n'
    once = format_source(src)
    assert once == format_source(once)


def test_decorated_keyword_is_in_language_spec():
    """`用` 进了关键字表，语言元数据（LSP 补全/高亮）才会认得它。"""
    from jishi.ai import build_lang_spec
    spec = build_lang_spec()
    keywords = str(spec.get("keywords", ""))
    assert "用" in keywords
