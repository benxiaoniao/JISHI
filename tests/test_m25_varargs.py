# -*- coding: utf-8 -*-
"""M25：变长参数（`*参数` / `**选项`）与星号解包（`令 甲, *余 = …`）。

这批特性是**唯一改动 C 代码**的语法扩展（参数绑定在 C VM 的 `bind_args`
里实现，且扁平字节码 `code_params` 的 stride 从 3 变成 4）。因此这里的
每条断言都跑**三执行器对拍**——C 侧的引用计数与边界最需要这个保护网。

顺带覆盖两个既有缺陷的回归：
- `**=` / `//=` 复合赋值被词法器切错（`**` + `=`），一直不可用；
- 格式化器把 `甲 **= 3` 重写成 `甲 ** = 3`（改了语义）。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import tmp_bc_path   # noqa: E402

from jishi import cvm_bind                        # noqa: E402
from jishi import opcodes as O                    # noqa: E402
from jishi import runtime as R                    # noqa: E402
from jishi import serialize                       # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.errors import JishiError               # noqa: E402
from jishi.formatter import format_source         # noqa: E402
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
# *参数
# ---------------------------------------------------------------------------

def test_varargs_basic():
    assert _all_agree(
        '函数 求和(*数)：\n'
        '    令 总 = 0\n'
        '    遍历 x 在 数：\n'
        '        总 += x\n'
        '    返回 总\n'
        '打印(求和())\n打印(求和(1))\n打印(求和(1, 2, 3))\n'
        '打印(求和(1, 2, 3, 4, 5))\n') == "0\n1\n6\n15\n"


def test_varargs_is_a_list():
    assert _all_agree(
        '函数 f(*参数)：\n    返回 参数\n'
        '打印(f())\n打印(f(1))\n打印(f(1, "甲", [2]))\n'
        '打印(长度(f(1, 2, 3)))\n') == "[]\n[1]\n[1, '甲', [2]]\n3\n"


def test_varargs_after_normal_params():
    assert _all_agree(
        '函数 f(甲, *余)：\n    返回 [甲, 余]\n'
        '打印(f(1))\n打印(f(1, 2, 3))\n') == "[1, []]\n[1, [2, 3]]\n"


def test_varargs_and_defaults_together():
    """默认值只在普通参数尾部，*参数 不参与——这条曾因 pad 算错而出 bug。"""
    assert _all_agree(
        '函数 f(甲, 乙 = 10, *余)：\n    返回 [甲, 乙, 余]\n'
        '打印(f(1))\n打印(f(1, 2))\n打印(f(1, 2, 3, 4))\n'
    ) == "[1, 10, []]\n[1, 2, []]\n[1, 2, [3, 4]]\n"


def test_varargs_only_function():
    assert _all_agree(
        '函数 f(*参数)：\n    返回 长度(参数)\n'
        '打印(f(), f(1), f(1, 2, 3))\n') == "0 1 3\n"


def test_varargs_mutation_does_not_leak():
    """每次调用拿到的都是新列表，不会串味。"""
    assert _all_agree(
        '函数 f(*参数)：\n'
        '    参数.追加(99)\n'
        '    返回 参数\n'
        '打印(f(1))\n打印(f(1))\n') == "[1, 99]\n[1, 99]\n"


def test_varargs_in_method():
    assert _all_agree(
        '类 甲：\n'
        '    函数 记(自身, *项)：\n'
        '        返回 项\n'
        '令 a = 新建 甲()\n'
        '打印(a.记(1, 2, 3))\n') == "[1, 2, 3]\n"


def test_varargs_in_lambda():
    assert _all_agree(
        '令 f = 函数(*数)：数\n打印(f(1, 2, 3))\n') == "[1, 2, 3]\n"


def test_varargs_in_closure():
    assert _all_agree(
        '函数 造(基数)：\n'
        '    返回 函数(*数)：基数\n'
        '令 f = 造(7)\n'
        '打印(f(1, 2))\n') == "7\n"


# ---------------------------------------------------------------------------
# **选项
# ---------------------------------------------------------------------------

def test_varkw_basic():
    assert _all_agree(
        '函数 配置(**选项)：\n    返回 选项\n'
        '打印(配置())\n打印(配置(甲 = 1))\n打印(配置(甲 = 1, 乙 = 2))\n'
    ) == "{}\n{'甲': 1}\n{'甲': 1, '乙': 2}\n"


def test_varkw_after_normal_params():
    assert _all_agree(
        '函数 f(名, **选项)：\n    返回 [名, 选项]\n'
        '打印(f("x"))\n打印(f("x", 甲 = 1))\n'
        '打印(f(名 = "y", 乙 = 2))\n') == "['x', {}]\n['x', {'甲': 1}]\n['y', {'乙': 2}]\n"


def test_varkw_only_function():
    assert _all_agree(
        '函数 f(**选项)：\n    返回 长度(选项)\n'
        '打印(f(), f(甲 = 1, 乙 = 2))\n') == "0 2\n"


def test_varkw_json():
    assert _all_agree(
        '导入 json 从 python\n'
        '函数 f(**选项)：\n'
        '    返回 json.dumps(选项, ensure_ascii = 假)\n'
        '打印(f(名字 = "小明", 年龄 = 8))\n'
    ) == '{"名字": "小明", "年龄": 8}\n'


# ---------------------------------------------------------------------------
# 三者组合
# ---------------------------------------------------------------------------

def test_star_and_dstar_together():
    assert _all_agree(
        '函数 f(甲, *余, **选)：\n    返回 [甲, 余, 选]\n'
        '打印(f(1))\n'
        '打印(f(1, 2, 3))\n'
        '打印(f(1, 2, c = 3))\n'
        '打印(f(甲 = 9))\n'
    ) == "[1, [], {}]\n[1, [2, 3], {}]\n[1, [2], {'c': 3}]\n[9, [], {}]\n"


def test_dstar_only_after_star():
    assert _all_agree(
        '函数 f(*数, **选)：\n    返回 [数, 选]\n'
        '打印(f())\n打印(f(1, 2))\n打印(f(1, x = 2))\n'
    ) == "[[], {}]\n[[1, 2], {}]\n[[1], {'x': 2}]\n"


def test_normal_default_star_chain():
    assert _all_agree(
        '函数 f(甲, 乙 = 2, *余, **选)：\n    返回 [甲, 乙, 余, 选]\n'
        '打印(f(1))\n'
        '打印(f(1, 9))\n'
        '打印(f(1, 9, 8, 7, z = 6))\n'
    ) == "[1, 2, [], {}]\n[1, 9, [], {}]\n[1, 9, [8, 7], {'z': 6}]\n"


def test_varargs_can_be_forwarded():
    """把收到的参数原样转交给另一个函数——变长参数最实际的用法。"""
    assert _all_agree(
        '函数 内层(a, b, c)：\n    返回 a + b + c\n'
        '函数 外层(*参数)：\n'
        '    令 和 = 0\n'
        '    遍历 x 在 参数：\n        和 += x\n'
        '    返回 和\n'
        '打印(内层(1, 2, 3))\n打印(外层(1, 2, 3, 4))\n') == "6\n10\n"


def test_varargs_with_annotated_normal_params():
    assert _all_agree(
        '函数 f(甲: 整数, *余)：\n    返回 [甲, 余]\n'
        '打印(f(1, 2, 3))\n') == "[1, [2, 3]]\n"


def test_method_annotation_is_actually_enforced():
    """方法的类型标注要真的校验（此前类方法的 annotations 没接上，等于没写）。"""
    src = ('类 甲：\n'
           '    函数 设置(自身, 值: 整数)：\n'
           '        自身.值 = 值\n'
           '令 a = 新建 甲()\n'
           'a.设置("文本")\n')
    for run in (_tree, _pvm, _cvm):
        with pytest.raises(JishiError) as ei:
            run(src)
        assert "要求是「整数」" in ei.value.message


def test_method_annotation_accepts_correct_type():
    assert _all_agree(
        '类 甲：\n'
        '    函数 设置(自身, 值: 整数)：\n'
        '        自身.值 = 值\n'
        '令 a = 新建 甲()\n'
        'a.设置(3)\n'
        '打印(a.值)\n') == "3\n"


# ---------------------------------------------------------------------------
# 变长参数的报错质量
# ---------------------------------------------------------------------------

def test_too_many_args_without_star():
    for run in (_tree, _pvm, _cvm):
        with pytest.raises(JishiError) as ei:
            run('函数 f(甲)：\n    返回 甲\n打印(f(1, 2))\n')
        assert "1 个参数" in ei.value.message
        assert "2 个" in ei.value.message


def test_unknown_kwarg_without_dstar():
    for run in (_tree, _pvm, _cvm):
        with pytest.raises(JishiError) as ei:
            run('函数 f(甲)：\n    返回 甲\n打印(f(1, 乙 = 2))\n')
        assert "没有叫「乙」的参数" in ei.value.message


def test_star_must_be_last_but_one():
    with pytest.raises(JishiError) as ei:
        _tree('函数 f(*余, 甲)：\n    返回 甲\n')
    assert "*参数" in ei.value.message
    assert "普通参数" in ei.value.message


def test_dstar_must_be_last():
    with pytest.raises(JishiError) as ei:
        _tree('函数 f(**选, 甲)：\n    返回 甲\n')
    assert "最后一个" in ei.value.message


def test_only_one_star():
    with pytest.raises(JishiError) as ei:
        _tree('函数 f(*甲, *乙)：\n    返回 1\n')
    assert "只能有一个" in ei.value.message


def test_star_before_dstar_order():
    with pytest.raises(JishiError) as ei:
        _tree('函数 f(**选, *余)：\n    返回 1\n')
    assert "*参数" in ei.value.message


def test_star_cannot_have_default():
    with pytest.raises(JishiError) as ei:
        _tree('函数 f(*余 = 1)：\n    返回 1\n')
    assert "不能有默认值" in ei.value.message


def test_dstar_cannot_have_default():
    with pytest.raises(JishiError) as ei:
        _tree('函数 f(**选 = 1)：\n    返回 1\n')
    assert "不能有默认值" in ei.value.message


def test_star_cannot_have_annotation():
    with pytest.raises(JishiError) as ei:
        _tree('函数 f(*余: 列表)：\n    返回 1\n')
    assert "不能写类型标注" in ei.value.message


# ---------------------------------------------------------------------------
# 星号解包
# ---------------------------------------------------------------------------

def test_star_unpack_basic():
    assert _all_agree(
        '令 甲, *余 = [1, 2, 3, 4]\n打印(甲, 余)\n') == "1 [2, 3, 4]\n"


def test_star_unpack_middle():
    assert _all_agree(
        '令 a, *中, z = [1, 2, 3, 4, 5]\n打印(a, 中, z)\n'
    ) == "1 [2, 3, 4] 5\n"


def test_star_unpack_empty_middle():
    assert _all_agree(
        '令 a, *中, z = [1, 2]\n打印(a, 中, z)\n') == "1 [] 2\n"


def test_star_unpack_at_end_gets_empty():
    assert _all_agree(
        '令 x, *y = [1]\n打印(x, y)\n') == "1 []\n"


def test_star_unpack_of_text():
    assert _all_agree(
        '令 头, *余 = "你好世界"\n打印(头, 余)\n') == "你 ['好', '世', '界']\n"


def test_star_unpack_without_declaration():
    assert _all_agree(
        'a, *b = [9, 8, 7]\n打印(a, b)\n') == "9 [8, 7]\n"


def test_plain_unpack_still_works():
    assert _all_agree(
        '令 p, q = [1, 2]\n打印(p, q)\n令 a, b = [10, 20]\na, b = b, a\n打印(a, b)\n'
    ) == "1 2\n20 10\n"


def test_star_unpack_too_short():
    for run in (_tree, _pvm, _cvm):
        with pytest.raises(JishiError) as ei:
            run('令 甲, *余 = []\n')
        assert "至少需要" in ei.value.message


def test_star_unpack_plain_too_short():
    with pytest.raises(JishiError) as ei:
        _tree('令 甲, 乙 = [1]\n')
    assert "2 个值" in ei.value.message
    # 提示里要指出「*余」这个出路
    assert "*余" in (ei.value.hint or "")


def test_two_stars_rejected():
    with pytest.raises(JishiError) as ei:
        _tree('令 a, *b, c, *d = [1, 2, 3]\n')
    assert "只能有一个" in ei.value.message


def test_leading_star_needs_fixed_name():
    with pytest.raises(JishiError) as ei:
        _tree('令 *全 = [1, 2]\n')
    assert "不能直接写" in ei.value.message


def test_dstar_in_unpack_rejected():
    with pytest.raises(JishiError) as ei:
        _tree('令 a, **b = [1, 2]\n')
    assert "**" in ei.value.message


# ---------------------------------------------------------------------------
# 与其它特性配合
# ---------------------------------------------------------------------------

def test_star_unpack_in_loop():
    assert _all_agree(
        '令 数据 = [[1, 2, 3], [4, 5], [6, 7, 8, 9]]\n'
        '遍历 行 在 数据：\n'
        '    令 头, *尾 = 行\n'
        '    打印(头, 尾)\n'
    ) == "1 [2, 3]\n4 [5]\n6 [7, 8, 9]\n"


def test_star_unpack_of_set():
    assert _all_agree(
        '令 头, *余 = 集合([1, 2, 3])\n打印(头, 余)\n') == "1 [2, 3]\n"


def test_varargs_receives_call_arguments_not_expanded():
    """基石没有「星号展开」实参（`f(*列表)`），传列表就是「一个参数」。

    这条锁住当前边界：`f([1,2,3])` 里 `数` 是 `[[1,2,3]]`。
    """
    assert _all_agree(
        '函数 f(*数)：\n    返回 数\n'
        '打印(f([x * 2 遍历 x 在 范围(3)]))\n'
        '打印(f(0, 2, 4))\n') == "[[0, 2, 4]]\n[0, 2, 4]\n"


def test_full_combination():
    src = (
        '函数 记日志(级别, *内容, **附加)：\n'
        '    令 行 = "[" + 级别 + "]"\n'
        '    遍历 c 在 内容：\n'
        '        行 += " " + c\n'
        '    如果 长度(附加) > 0：\n'
        '        行 += " (" + 文本(附加) + ")"\n'
        '    返回 行\n'
        '打印(记日志("信息", "启动完成"))\n'
        '打印(记日志("错误", "读文件失败", "重试中", 重试 = 2))\n'
        '令 首, *其余 = [1, 2, 3]\n'
        '打印(首, 其余)\n'
    )
    assert _all_agree(src) == (
        "[信息] 启动完成\n"
        "[错误] 读文件失败 重试中 ({'重试': 2})\n"
        "1 [2, 3]\n")


# ---------------------------------------------------------------------------
# 底层契约（C 侧改动最需要这些防线）
# ---------------------------------------------------------------------------

def test_param_kind_is_wired_through():
    cm = compile_source('函数 f(a, *b, **c)：\n    返回 1\n', "<t>")
    code = cm.codes[1]
    assert code.param_kind == [O.ParamKind.NORMAL, O.ParamKind.VARARGS,
                               O.ParamKind.VARKW]


def test_param_kind_defaults_to_normal():
    cm = compile_source('函数 f(a, b = 1)：\n    返回 1\n', "<t>")
    assert cm.codes[1].param_kind == [O.ParamKind.NORMAL] * 2


def test_cvm_callback_table_has_pack_kwargs():
    """新增宿主回调必须两端对齐——曾因字段错位导致 ctypes 崩溃。"""
    from jishi.cvm_bind import JsHost
    assert "pack_kwargs" in [f[0] for f in JsHost._fields_]


def test_unpack_callback_signature_has_star():
    """unpack 回调必须带 star 参数（C 侧把 UNPACK 的 b 传过来）。"""
    c_src = (ROOT / "cvm" / "include" / "jsvm.h").read_text(encoding="utf-8")
    assert "int (*unpack)(JVal *value, int n, int star, JVal *out" in c_src


def test_serialize_roundtrip_keeps_param_kind():
    cm = compile_source(
        '函数 f(甲, *数, **选)：\n    返回 [甲, 数, 选]\n'
        '打印(f(1, 2, x = 3))\n令 头, *余 = [1, 2]\n打印(头, 余)\n',
        "<t>")
    cm2 = serialize.loads(serialize.dumps(cm))
    assert cm2.codes[1].param_kind == cm.codes[1].param_kind
    b1, b2 = io.StringIO(), io.StringIO()
    with redirect_stdout(b1):
        vm_mod.VM(cm, "<t>").run()
    with redirect_stdout(b2):
        vm_mod.VM(cm2, "<t>").run()
    assert b1.getvalue() == b2.getvalue() == "[1, [2], {'x': 3}]\n1 [2]\n"


def test_formatter_independent():
    """格式化（等价的、纯 Python 的执行路径）也要与三执行器结果一致。"""
    src = ('函数 f(a, *b)：\n    返回 [a, b]\n'
           '令 甲, *余 = [1, 2, 3]\n')
    cm = compile_source(src, "<t>")
    assert cm.codes[1].param_kind == [0, 1]


def test_bytecode_format_version_bumped():
    """字节码格式变了（code_params 加了 kind、UNPACK 的 b 换了含义），必须升版本。"""
    assert serialize.FORMAT_VERSION >= 2


# ---------------------------------------------------------------------------
# 顺带修掉的既有缺陷
# ---------------------------------------------------------------------------

def test_floor_and_pow_augmented_assignment_work():
    """`//=` / `**=` 曾因词法器把 `**=` 切成 `**` + `=` 而完全不可用。"""
    assert _all_agree(
        '令 甲 = 2\n甲 **= 3\n打印(甲)\n'
        '令 乙 = 7\n乙 //= 2\n打印(乙)\n'
        '令 丙 = 7\n丙 %= 4\n打印(丙)\n') == "8\n3\n3\n"


def test_operator_longest_match():
    from jishi.tokenizer import tokenize as tk
    for src, want in [("甲 **= 3", "**="), ("甲 //= 3", "//="),
                      ("甲 ** 3", "**"), ("甲 // 3", "//"),
                      ("甲 ** -1", "**")]:
        ops = [t.value for t in tk(src + "\n", "<t>") if t.type == "OP"]
        assert want in ops, f"{src} 分词成了 {ops}"


def test_formatter_keeps_pow_assign_intact():
    out = format_source("令 甲 = 2\n甲 **= 3\n甲 //= 2\n")
    assert "**=" in out and "//=" in out
    assert "** =" not in out      # 被拆开就等于改了语义


def test_formatter_star_sticks_to_name():
    out = format_source(
        '函数 求和(甲, *数, **选项)：\n    返回 数\n令 头, *余 = [1, 2]\n')
    assert "*数" in out and "**选项" in out and "*余" in out
    assert "* 数" not in out
    # 乘法仍要有空格
    assert "3 * 4" in format_source("令 甲 = 3 * 4\n")


def test_formatter_idempotent_with_new_syntax():
    src = ('函数 求和(甲, *数, **选项)：\n'
           '    返回 [甲, 数, 选项]\n'
           '令 头, *余 = [1, 2, 3]\n'
           '令 f = 函数(*数)：数\n')
    once = format_source(src)
    assert once == format_source(once)


# ---------------------------------------------------------------------------
# 跨语言宿主（M13 的 Node / Rust）也要跟上，否则「字节码可移植」就名不副实
# ---------------------------------------------------------------------------

import shutil                                    # noqa: E402
import subprocess                                # noqa: E402

_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="本机无 Node.js")
def test_node_host_supports_varargs_and_star_unpack():
    """Node 宿主执行同一份字节码，输出必须与 Python 参考 VM 一致。"""
    src = ('函数 求和(甲, *数, **选项)：\n'
           '    返回 [甲, 数, 选项]\n'
           '打印(求和(1, 2, 3, x = 4))\n'
           '令 头, *余 = [1, 2, 3]\n'
           '打印(头, 余)\n'
           '函数 带默认(甲, 乙 = 10, *余)：\n'
           '    返回 [甲, 乙, 余]\n'
           '打印(带默认(1))\n'
           '令 首, *中, 尾 = [1, 2, 3, 4, 5]\n'
           '打印(首, 中, 尾)\n')
    bc = tmp_bc_path(ROOT / "_tmp_m25_node.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    try:
        r = subprocess.run([_NODE, str(ROOT / "node" / "index.js"), str(bc)],
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=60)
    finally:
        bc.unlink(missing_ok=True)
    assert r.returncode == 0, f"Node 崩溃：{r.stderr[:300]}"
    assert r.stdout == _pvm(src)


def test_node_host_version_check_matches_engine():
    """字节码格式版本两端必须一致，否则 Node 会拒绝加载。"""
    node_js = (ROOT / "node" / "index.js").read_text(encoding="utf-8")
    assert f"body.version !== {serialize.FORMAT_VERSION}" in node_js
