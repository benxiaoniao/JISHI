# -*- coding: utf-8 -*-
"""M30：JS（Node）宿主补齐到 Python 侧。

这一轮把 JS 宿主缺的七块一次补齐，全部与 Python VM 逐字节对拍：

1. **成员测试** `在` / `不在`（列表按值、字典按「键」、文本找子串、集合按元素）
2. **同一性** `是` / `不是`（单例 / 数字文本按值 / 容器与实例按身份）
3. **容器相等按值** `[1, 2] == [1, 2]`，以及 `truthy` 对空容器给假
4. **集合类型**（`{1, 2}` 字面量、`集合(…)`、推导式、11 个方法）
5. **列表高阶方法** `映射` / `过滤` / `排序按` / `归约`
6. **类型标注的参数校验**（编译器已把校验发射成前导字节码，
   实现 `检查实参` 内建即自动生效）
7. **自定义可迭代对象**（`长度` / `最大` / `最小` / 遍历）

值得记一笔的是：1、2 两块的缺失与 Rust 侧 M28 暴露的**是同一个问题**——
`CMPOP` 只映射了 0–5（`==`…`>=`），M23.2 的语义比较码 6–9 根本没接，
于是 `2 在 [1, 2]` 会报「不支持的比较「undefined」」。
`docs/embed.md` 的支持程度表当时把这两行给 JS 标了 ✅，**那是错的**——
不跑就不知道，这轮实测才纠正过来。

另外 3 里的 `truthy` 也是同类「静默算错」：JS 的 `Boolean([])` 是 `true`，
而 Python 的 `bool([])` 是 `False`，所以 `如果 列表：` 的判断会整个反过来。
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import tmp_bc_path   # noqa: E402

from jishi import cvm_bind                        # noqa: E402
from jishi import serialize                       # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.interpreter import Interpreter         # noqa: E402
from jishi.errors import JishiError               # noqa: E402
from jishi.parser import parse                    # noqa: E402
from jishi.tokenizer import tokenize              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

HAS_NODE = NODE is not None
pytestmark = pytest.mark.skipif(not HAS_NODE, reason="本机无 Node.js，跳过 JS VM 对拍")


# ---------------------------------------------------------------------------
# 对拍脚手架
# ---------------------------------------------------------------------------

def _py_run(src: str) -> str:
    """Python VM（参考实现）跑一遍，取 stdout。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _tree_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        Interpreter(filename="<t>").run(
            parse(tokenize(src, "<t>"), src.split("\n"), "<t>"))
    return buf.getvalue()


def _c_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _node_run(src: str) -> tuple[int, str, str]:
    bc = tmp_bc_path(ROOT / "_tmp_m30_bc.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), str(bc)],
                       capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout, r.stderr


def _agree(src: str) -> str:
    """Python VM 与 Node 必须逐字节一致（Node 不得非零退出）。"""
    py = _py_run(src)
    rc, out, err = _node_run(src)
    assert rc == 0, f"Node 非零退出：rc={rc}\n{err[:400]}"
    assert out == py, f"输出不一致\nPython: {py!r}\nNode:   {out!r}"
    return out


def _agree_all(src: str) -> str:
    """树遍历 / Python VM / C VM / Node 四路一致（关键语义用）。"""
    t = _tree_run(src)
    p = _py_run(src)
    c = _c_run(src)
    rc, out, err = _node_run(src)
    assert t == p == c, f"三执行器不一致：树遍历={t!r} PythonVM={p!r} CVM={c!r}"
    assert rc == 0, f"Node 非零退出：rc={rc}\n{err[:400]}"
    assert out == p, f"Node 与 Python VM 不一致\nPython: {p!r}\nNode:   {out!r}"
    return out


def _node_err(src: str) -> str:
    """跑一段必定报错的代码，返回 Node 的错误输出（要求是人话，不是 JS 堆栈）。"""
    rc, _out, err = _node_run(src)
    assert rc != 0, "这段代码本该报错，但 Node 正常退出"
    assert "JishiError" not in err.splitlines()[0], f"错误没被接住：{err[:300]}"
    assert err.startswith("错误（"), f"错误输出不规范：{err[:200]}"
    return err


# ---------------------------------------------------------------------------
# 1. 成员测试 `在` / `不在`
# ---------------------------------------------------------------------------
# 此前 CMPOP 只映射 0–5，`在`(6)/`不在`(7) 落成 `undefined` 直接报错。

def test_node_membership_list_and_dict():
    assert _agree_all(
        '打印(2 在 [1, 2], 9 在 [1, 2])\n'
        '打印(9 不在 [1, 2], 2 不在 [1, 2])\n'
        '打印("a" 在 {"a": 1}, "x" 在 {"a": 1})\n'
        '打印(3 在 范围(5), 9 在 范围(5))\n'
    ) == "真 假\n真 假\n真 假\n真 假\n"


def test_node_membership_string():
    assert _agree('打印("好" 在 "你好", "x" 在 "你好")\n') == "真 假\n"


def test_node_membership_nested_value_equality():
    """列表成员是**按值**找的（Python 的 `in` 用 `==`），不是比引用。"""
    assert _agree(
        '打印([1, 2] 在 [[1, 2], [3]])\n'
        '打印({"a": 1} 在 [{"a": 1}])\n'
    ) == "真\n真\n"


def test_node_membership_type_error_is_chinese():
    assert "不能在" in _node_err('打印(1 在 "abc")\n')
    assert "不能在" in _node_err("打印(1 在 5)\n")


# ---------------------------------------------------------------------------
# 2. 同一性 `是` / `不是`
# ---------------------------------------------------------------------------

def test_node_identity_singletons_and_values():
    assert _agree_all(
        "令 x = 空\n打印(x 是 空, x 不是 空)\n"
        "令 t = 真\n打印(t 是 真, t 是 假)\n"
        "令 a = 1\n令 b = 1\n打印(a 是 b)\n"
        '令 s = "甲"\n打印(s 是 "甲")\n'
    ) == "真 假\n真 假\n真\n真\n"


def test_node_identity_containers_by_reference():
    """容器按对象身份：两个内容相同的列表**不是**同一个东西。"""
    assert _agree_all(
        "令 p = [1]\n令 q = [1]\n令 r = p\n打印(p 是 r, p 是 q)\n"
        "令 m = {}\n令 n = {}\n打印(m 是 m, m 是 n)\n"
    ) == "真 假\n真 假\n"


def test_node_identity_instances():
    assert _agree(
        "类 甲：\n    函数 初始化(自身)：\n        空\n"
        "令 a = 新建 甲()\n令 b = a\n令 c = 新建 甲()\n"
        "打印(a 是 b, a 是 c, a 不是 c)\n"
    ) == "真 假 真\n"


# ---------------------------------------------------------------------------
# 3. 容器相等按值 + 空容器为假
# ---------------------------------------------------------------------------

def test_node_container_equality_by_value():
    assert _agree_all(
        "打印([1, 2] == [1, 2], [1] == [2])\n"
        '打印([[1], [2]] == [[1], [2]])\n'
        '打印({"a": 1} == {"a": 1}, {"a": 1} == {"a": 2})\n'
        '打印({"a": [1]} == {"a": [1]})\n'
        "打印([1] == 1, [1] == \"甲\")\n"
    ) == "真 假\n真\n真 假\n真\n假 假\n"


def test_node_number_and_bool_equality():
    """`1 == 真` 为真（Python 里 True == 1），但 `2 == 真` 为假。"""
    assert _agree("打印(1 == 真, 0 == 假, 2 == 真)\n") == "真 真 假\n"


def test_node_empty_container_is_falsy():
    """空容器为假——JS 的 `Boolean([])` 是 true，不特判判断就反了。"""
    assert _agree_all(
        '如果 []：\n    打印("空列表为真")\n否则：\n    打印("空列表为假")\n'
        '如果 [0]：\n    打印("非空为真")\n否则：\n    打印("非空为假")\n'
        '如果 {}：\n    打印("空字典为真")\n否则：\n    打印("空字典为假")\n'
        '如果 集合()：\n    打印("空集合为真")\n否则：\n    打印("空集合为假")\n'
        '如果 ""：\n    打印("空文本为真")\n否则：\n    打印("空文本为假")\n'
    ) == "空列表为假\n非空为真\n空字典为假\n空集合为假\n空文本为假\n"


# ---------------------------------------------------------------------------
# 4. 集合类型
# ---------------------------------------------------------------------------

def test_node_set_literal_and_ctor():
    assert _agree_all(
        "打印({1, 2, 2, 3})\n"
        "打印(集合([3, 1, 3, 2]))\n"
        "打印(集合(), 长度(集合()))\n"
        "打印(类型(集合()))\n"
        '打印(集合(["甲", "乙", "甲"]))\n'
    ) == "{1, 2, 3}\n{3, 1, 2}\n集合() 0\n集合\n{'甲', '乙'}\n"


def test_node_set_dedup_follows_python_equality():
    """去重按 Python 的相等性：`1 == 1.0 == 真`，所以只留一个。"""
    assert _agree(
        "打印(集合([1, 1.0, 真, 2]))\n"
        "打印(集合([0, 假, 1]))\n"
        '打印(集合([1, "1"]))\n'
    ) == "{1, 2}\n{0, 1}\n{1, '1'}\n"


def test_node_set_comprehension():
    assert _agree_all(
        "打印({x % 3 遍历 x 在 范围(9)})\n"
        "打印({x 遍历 x 在 范围(6) 如果 x % 2 == 0})\n"
    ) == "{0, 1, 2}\n{0, 2, 4}\n"


def test_node_set_methods():
    assert _agree_all(
        "打印(集合([1, 2]).并集([2, 3]))\n"
        "打印(集合([1, 2, 3]).交集([2, 3, 4]))\n"
        "打印(集合([1, 2, 3]).差集([2]))\n"
        "打印(集合([1, 2]).对称差([2, 3]))\n"
        "打印(集合([2, 1]).转列表())\n"
    ) == "{1, 2, 3}\n{2, 3}\n{1, 3}\n{1, 3}\n[2, 1]\n"


def test_node_set_mutating_methods():
    assert _agree_all(
        "令 s = 集合()\n"
        "s.添加(1)\ns.添加(2)\ns.添加(1)\ns.丢弃(2)\n"
        "打印(s, 1 在 s, 2 在 s)\n"
        "令 t = 集合([1, 2])\n"
        "t.移除(1)\n打印(t)\n"
        "t.清空()\n打印(t, 长度(t))\n"
    ) == "{1} 真 假\n{2}\n集合() 0\n"


def test_node_set_copy_is_independent():
    assert _agree(
        "令 a = 集合([1])\n令 b = a.复制()\nb.添加(2)\n打印(a, b)\n"
    ) == "{1} {1, 2}\n"


def test_node_set_membership_and_equality():
    """集合相等与顺序无关；集合与列表比是假（与 Python 一致）。"""
    assert _agree_all(
        "打印(2 在 {1, 2}, 9 在 {1, 2})\n"
        "打印(2 不在 {1, 2}, 9 不在 {1, 2})\n"
        "打印(集合([1, 2]) == 集合([2, 1]), 集合([1]) == 集合([2]))\n"
        "打印(集合([1]) == [1])\n"
    ) == "真 假\n假 真\n真 假\n假\n"


def test_node_set_iteration_order_is_insertion_order():
    """迭代顺序 = 插入顺序（刻意不用哈希序：同一程序每次运行都要一样）。"""
    assert _agree_all(
        "遍历 x 在 集合([3, 1, 2])：\n    打印(x)\n"
        "令 s = 0\n遍历 x 在 集合([1, 2, 3])：\n    s += x\n打印(s)\n"
    ) == "3\n1\n2\n6\n"


def test_node_set_with_builtins():
    assert _agree_all(
        "打印(最大(集合([3, 1])), 最小(集合([3, 1])), 总和(集合([1, 2])))\n"
        "打印(长度(集合([1, 1, 2])))\n"
    ) == "3 1 3\n2\n"


def test_node_set_unpack():
    assert _agree(
        "令 a, *余 = 集合([1, 2, 3])\n打印(a, 余)\n"
        "令 甲, *中, 尾 = 集合([1, 2, 3])\n打印(甲, 中, 尾)\n"
    ) == "1 [2, 3]\n1 [2] 3\n"


def test_node_set_can_hold_instances():
    """实例可进集合（Python 侧它有默认的 `object.__hash__`，按身份）。"""
    assert _agree(
        "类 甲：\n    函数 初始化(自身, v)：\n        自身.v = v\n"
        "令 a = 新建 甲(1)\n"
        "令 s = 集合([a, a])\n"
        "打印(长度(s))\n"
    ) == "1\n"


def test_node_set_rejects_unhashable():
    """列表/字典/集合/精确小数不能进集合，要报中文错。"""
    for src, word in [
        ("打印(集合([[1]]))\n", "放进集合"),
        ('打印(集合([{"a": 1}]))\n', "放进集合"),
        ("打印(集合([集合([1])]))\n", "放进集合"),
        ('打印(集合([精确("1")]))\n', "放进集合"),
    ]:
        assert word in _node_err(src), src


def test_node_set_ctor_rejects_extra_args():
    assert "最多接受 1 个参数" in _node_err("打印(集合([1], [2]))\n")


# ---------------------------------------------------------------------------
# 5. 列表高阶方法（数据流水线）
# ---------------------------------------------------------------------------

def test_node_list_map_filter_reduce():
    assert _agree_all(
        "打印([1, 2, 3].映射(函数(x)：x * x))\n"
        "打印([1, 2, 3, 4].过滤(函数(x)：x % 2 == 0))\n"
        "打印([1, 2, 3, 4].归约(函数(累, x)：累 + x, 0))\n"
        '打印(["甲", "乙"].归约(函数(累, x)：累 + x, ""))\n'
    ) == "[1, 4, 9]\n[2, 4]\n10\n甲乙\n"


def test_node_list_sort_by():
    assert _agree_all(
        "打印([3, 1, 2].排序按(函数(x)：x))\n"
        "打印([3, 1, 2].排序按(函数(x)：-x))\n"
        '令 人 = [{"年": 20}, {"年": 18}]\n'
        '打印(人.排序按(函数(p)：p["年"]))\n'
    ) == "[1, 2, 3]\n[3, 2, 1]\n[{'年': 18}, {'年': 20}]\n"


def test_node_pipeline_chaining():
    assert _agree(
        "打印([1, 2, 3, 4, 5].过滤(函数(x)：x > 2).映射(函数(x)：x * 10))\n"
    ) == "[30, 40, 50]\n"


def test_node_map_with_named_function_and_closure():
    assert _agree(
        "函数 三倍(x)：\n    返回 x * 3\n"
        "令 基 = 10\n"
        "打印([1, 2].映射(三倍))\n"
        "打印([1, 2].映射(函数(x)：x + 基))\n"
    ) == "[3, 6]\n[11, 12]\n"


def test_node_map_does_not_mutate_original():
    """高阶方法返回新列表，不动原列表（不用 Python 的原地 sort 之类）。"""
    assert _agree(
        "令 a = [3, 1]\n打印(a.排序按(函数(x)：x))\n打印(a)\n"
    ) == "[1, 3]\n[3, 1]\n"


def test_node_map_callback_may_mutate_outer_list():
    assert _agree(
        "令 表 = []\n[1, 2, 3].映射(函数(x)：表.追加(x * 2))\n打印(表)\n"
    ) == "[2, 4, 6]\n"


def test_node_sort_by_incomparable_keys_is_chinese():
    """排序键混着数字和文本要报中文错，不能悄悄给出错顺序。"""
    assert "排序按" in _node_err(
        '令 混 = [{"k": 1}, {"k": "甲"}]\n打印(混.排序按(函数(x)：x["k"]))\n')


def test_node_map_needs_callable():
    assert "需要一个函数" in _node_err("打印([1].映射(3))\n")


# ---------------------------------------------------------------------------
# 6. 类型标注的参数校验
# ---------------------------------------------------------------------------
# 编译器把校验发射成函数自己的前导字节码（调 `检查实参` 内建），
# 所以 JS 侧实现那个内建就自动获得这项能力。

def test_node_annotation_passes():
    assert _agree_all(
        "函数 折扣(价格: 小数, 比例: 小数) -> 小数：\n"
        "    返回 价格 * 比例\n"
        "打印(折扣(100.5, 0.8))\n"
    ) == "80.4\n"


def test_node_annotation_rejects_wrong_type():
    assert "要求是" in _node_err(
        "函数 折扣(价格: 整数)：\n    返回 价格\n打印(折扣(\"甲\"))\n")


def test_node_annotation_ignores_unknown_names():
    """认不出的标注名（自定义类型）只作文档，不误报。"""
    assert _agree(
        "函数 f(订单: 订单类型)：\n    返回 1\n打印(f(1))\n"
    ) == "1\n"


def test_node_annotation_knows_set():
    assert _agree(
        "函数 f(x: 集合)：\n    返回 长度(x)\n打印(f(集合([1, 2])))\n"
    ) == "2\n"


# ---------------------------------------------------------------------------
# 7. 自定义可迭代对象
# ---------------------------------------------------------------------------

_ITER_CLASS = (
    "类 盒：\n"
    "    函数 初始化(自身, 项)：\n"
    "        自身.项 = 项\n"
    "    函数 迭代(自身)：\n"
    "        返回 自身.项\n"
)


def test_node_custom_iterable_in_for_loop():
    assert _agree_all(
        _ITER_CLASS + "令 b = 新建 盒([1, 2, 3])\n"
        "遍历 x 在 b：\n    打印(x)\n"
        "令 s = 0\n遍历 x 在 b：\n    s += x\n打印(s)\n"
    ) == "1\n2\n3\n6\n"


def test_node_custom_iterable_with_builtins():
    assert _agree(
        _ITER_CLASS + "令 b = 新建 盒([3, 1, 2])\n"
        "打印(长度(b), 最大(b), 最小(b), 总和(b))\n"
    ) == "3 3 1 6\n"


def test_node_iter_returning_self_is_rejected():
    assert "无限循环" in _node_err(
        "类 甲：\n    函数 迭代(自身)：\n        返回 自身\n"
        "遍历 x 在 新建 甲()：\n    打印(x)\n")


def test_node_uniterable_is_chinese():
    assert "不能遍历" in _node_err("遍历 x 在 5：\n    打印(x)\n")


# ---------------------------------------------------------------------------
# 9. 切片（指令 BUILD_SLICE，JS 侧此前完全没实现）
# ---------------------------------------------------------------------------
# 横向探测没覆盖到它——是**跑真实示例项目**（`examples/projects/日志统计`，
# 里面用了 `对[::-1]`）才撞出来的。顺手把 Python 侧两条英文漏出的报错也翻了。

def test_node_slice_read():
    assert _agree_all(
        "令 a = [1, 2, 3, 4, 5]\n"
        "打印(a[1:3])\n打印(a[:2], a[3:])\n打印(a[-2:], a[:-2])\n"
        "打印(a[::2], a[1::2])\n打印(a[::-1])\n打印(a[3:0:-1])\n"
        "打印(a[10:20], a[-100:2])\n打印(a[2:1])\n"
    ) == "[2, 3]\n[1, 2] [4, 5]\n[4, 5] [1, 2, 3]\n[1, 3, 5] [2, 4]\n" \
         "[5, 4, 3, 2, 1]\n[4, 3, 2]\n[] [1, 2]\n[]\n"


def test_node_slice_text():
    assert _agree('令 s = "你好世界"\n打印(s[1:3], s[::-1])\n') == "好世 界世好你\n"


def test_node_slice_write():
    assert _agree_all(
        "令 a = [1, 2, 3, 4, 5]\na[1:3] = [9, 9]\n打印(a)\n"
        "令 b = [1, 2, 3, 4]\nb[1:3] = [7]\n打印(b)\n"
        "令 c = [1, 4]\nc[1:1] = [2, 3]\n打印(c)\n"
        "令 d = [1, 2, 3]\nd[1:] = []\n打印(d)\n"
        "令 e = [1, 2, 3, 4, 5]\ne[::2] = [7, 8, 9]\n打印(e)\n"
    ) == "[1, 9, 9, 4, 5]\n[1, 7, 4]\n[1, 2, 3, 4]\n[1]\n[7, 2, 8, 4, 9]\n"


def test_node_slice_with_other_features():
    """切片与遍历 / 高阶方法 / 复合赋值配合（真实项目里就是这么用的）。"""
    assert _agree(
        "令 a = [3, 1, 2]\n遍历 x 在 a[::-1]：\n    打印(x)\n"
        "令 b = [5, 4, 3, 2, 1]\n打印(b[1:4].映射(函数(x)：x * 10))\n"
        "令 c = [1, 2, 3]\nc[1:] += [99]\n打印(c)\n"
    ) == "2\n1\n3\n[40, 30, 20]\n[1, 2, 3, 99]\n"


def test_node_slice_errors():
    assert "步长不能为 0" in _node_err("令 a = [1, 2, 3]\n打印(a[::0])\n")
    assert "长度不匹配" in _node_err("令 a = [1, 2, 3, 4, 5]\na[::2] = [1, 2]\n")
    assert "不能切片" in _node_err('令 d = {"a": 1}\n打印(d[1:2])\n')


# ---------------------------------------------------------------------------
# 10. 二元运算的类型分派（JS 的运算符会静默给出别扭结果）
# ---------------------------------------------------------------------------
# 直接用 JS 运算符兜底会踩三个坑：`[1] + [2]` 变字符串 `"12"`、
# `1 + 空` 当 0、`-7 % 3` 的符号与 Python 相反。

def test_node_container_arithmetic():
    assert _agree_all(
        "打印([1, 2] + [3])\n"
        '打印(["甲"] + ["乙"])\n'
        "打印([1] * 3, 3 * [1])\n"
        '打印("ab" * 2, 2 * "ab")\n'
        '打印("ab" * 0, "ab" * -1)\n'
    ) == "[1, 2, 3]\n['甲', '乙']\n[1, 1, 1] [1, 1, 1]\nabab abab\n \n"


def test_node_list_concat_makes_new_list():
    assert _agree(
        "令 a = [1]\n令 b = [2]\n打印(a + b, a, b)\n"
    ) == "[1, 2] [1] [2]\n"


def test_node_modulo_follows_python_sign():
    """Python 的 `%` 结果符号跟**除数**一致（JS 的是截断语义）。"""
    assert _agree_all(
        "打印(-7 % 3, 7 % -3, -7.5 % 3, 7 % 3)\n"
        "打印(-7 // 3, 7 // -3)\n"
    ) == "2 -2 1.5 1\n-3 -3\n"


def test_node_arithmetic_type_errors_are_chinese():
    """运算类型不匹配要报中文（此前 Python 侧这几条也漏英文）。"""
    for src, word in [
        ("打印([1] + 1)\n", "只能和"),
        ('打印("a" + 1)\n', "只能和"),
        ('打印([1] * "a")\n', "只能乘整数"),
        ("打印(集合([1]) + 集合([2]))\n", "不能做"),
        ("打印(1 + 空)\n", "不能做"),
    ]:
        assert word in _node_err(src), src


def test_node_power_float_boxing():
    """`2 ** -1` 是小数、`2 ** 2` 是整数——与 Python 的类型一致。"""
    assert _agree("打印(2 ** 2, 2 ** -1, 2.0 ** 2, 4 ** 0.5)\n") == "4 0.5 4.0 2.0\n"


# ---------------------------------------------------------------------------
# 11. 错误输出规范化（此前直接甩 JS 堆栈）
# ---------------------------------------------------------------------------

def test_node_error_output_is_readable():
    err = _node_err("打印(1 / 0)\n")
    assert "错误（除零错误）" in err
    assert "第 1 行" in err


# ---------------------------------------------------------------------------
# 12. 标准库（表格 / 文本 / json / 正则 / 系统）
# ---------------------------------------------------------------------------
# 补齐这些是**跑真实示例项目撞出来的**：成绩分析用 `表格` + `文本.格式化`，
# 日志统计用 `文本.拆分` + 切片。所以「宿主补齐」的验收标准之一就是
# 「`examples/projects/` 里的项目能在宿主上原样跑」。

def test_node_stdlib_table_functions():
    assert _agree(
        "导入 表格\n"
        "打印(表格.转置([[1, 2], [3, 4]]))\n"
        "打印(表格.转置([]))\n"
    ) == "[[1, 3], [2, 4]]\n[]\n"


def test_node_stdlib_table_read_and_query(tmp_path):
    """读 CSV → 表头/数据行/挑选/筛选/排序/汇总（与 Python 侧同一套语义）。"""
    f = tmp_path / "成绩.csv"
    f.write_text("姓名,语文,数学\n小明,90,95\n小红,85,88\n小刚,55,60\n",
                 encoding="utf-8")
    src = (
        "导入 表格\n"
        f'令 t = 表格.读表格("{f.as_posix()}")\n'
        "打印(表格.表头(t))\n"
        "打印(表格.数据行(t))\n"
        "打印(表格.挑选(t, \"姓名\"))\n"
        "打印(表格.挑选(t, [\"姓名\", \"数学\"]))\n"
        "打印(表格.筛选(t, \"姓名\", \"小红\"))\n"
        "打印(表格.汇总(t, \"数学\"))\n"
    )
    assert _agree(src) == (
        "['姓名', '语文', '数学']\n"
        "[['小明', '90', '95'], ['小红', '85', '88'], ['小刚', '55', '60']]\n"
        "['小明', '小红', '小刚']\n"
        "[['小明', '95'], ['小红', '88'], ['小刚', '60']]\n"
        "<表格 1 行 x 3 列>\n"
        "{'个数': 3, '总和': 243.0, '平均': 81.0, '最大': 95.0, '最小': 60.0}\n"
    )


def test_node_stdlib_text_module():
    assert _agree_all(
        "导入 文本\n"
        '打印(文本.格式化("{}今年{}岁", "小明", 12))\n'
        '打印(文本.格式化("平均 {:.1f}", 83.75))\n'
        '打印(文本.右对齐("7", 3, "0"), 文本.左对齐("甲", 4, "*"))\n'
        '打印(文本.补零(7, 3), 文本.补零(-7, 4))\n'
        '打印(文本.拼接([1, "甲", 真, 空], "-"))\n'
        "打印(文本.重复(\"x\", 3), 文本.计数(\"abab\", \"a\"))\n"
        '打印(文本.切成三段("a=b", "="))\n'
        '打印(文本.按行拆分("a\\nb\\nc"))\n'
    ) == ("小明今年12岁\n平均 83.8\n007 甲***\n007 -007\n"
          "1-甲-真-空\nxxx 2\n['a', '=', 'b']\n['a', 'b', 'c']\n")


def test_node_stdlib_json_module():
    assert _agree_all(
        "导入 json\n"
        '打印(json.转文本({"甲": 1, "乙": [1, 2]}))\n'
        '打印(json.转文本({"甲": 1}, 2))\n'
        "打印(json.转文本([]), json.转文本({}))\n"
        '打印(json.解析("[1, 2, 3]"))\n'
        '令 s = json.转文本({"甲": [1, {"乙": 真}]})\n打印(json.解析(s))\n'
    ) == ('{"甲": 1, "乙": [1, 2]}\n{\n  "甲": 1\n}\n[] {}\n[1, 2, 3]\n'
          "{'甲': [1, {'乙': 真}]}\n")


def test_node_stdlib_regex_module():
    assert _agree_all(
        "导入 正则\n"
        '打印(正则.匹配("\\\\d+", "123abc"), 正则.匹配("\\\\d+", "abc123"))\n'
        '打印(正则.搜索("\\\\d+", "abc123def456"))\n'
        '打印(正则.查找全部("\\\\d+", "a1b22c333"))\n'
        '打印(正则.替换("\\\\d", "#", "a1b2"))\n'
        '打印(正则.拆分("[,;]", "a,b;c"))\n'
        '打印(正则.分组("(\\\\w)(\\\\d)", "a1"))\n'
    ) == "真 假\n123\n['1', '22', '333']\na#b#\n['a', 'b', 'c']\n['a', '1']\n"


def test_node_stdlib_system_module():
    """平台名**不能写死**：开发机是 Windows、CI 是 Linux（M35 实测：
    CI 全红就是这一行 `== "Windows…"` 造成的——`_agree` 本身已经在对拍
    两个宿主，所以这里只校验稳定部分 + 平台名非空就够）。"""
    out = _agree(
        "导入 系统\n"
        "打印(系统.系统名())\n"
        '打印(系统.环境变量("一定不存在的变量_xyz", "兜底"))\n'
        "打印(长度(系统.当前目录()) > 0)\n"
        "打印(长度(系统.主机名()) >= 0)\n"
    )
    assert out.split("\n")[0].strip(), "系统.系统名() 不该是空串"
    assert out.endswith("兜底\n真\n真\n")


def test_node_stdlib_system_args():
    """`系统.参数()`：`node index.js 字节码.json a b` → ['a', 'b']。"""
    bc = tmp_bc_path(ROOT / "_tmp_m30_args.json")
    bc.write_text(serialize.dumps(compile_source("导入 系统\n打印(系统.参数())\n", "<t>")),
                  encoding="utf-8")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), str(bc), "甲", "乙"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:300]
    assert r.stdout == "['甲', '乙']\n"


# ---------------------------------------------------------------------------
# 13. 输入 `输入()` 与标准输入
# ---------------------------------------------------------------------------
# 此前 JS 侧的 `输入()` **永远返回空串**（不读 stdin），真实项目里
# 「按回车用默认路径」这类交互直接失效。

def test_node_input_reads_stdin():
    bc = tmp_bc_path(ROOT / "_tmp_m30_in.json")
    bc.write_text(serialize.dumps(compile_source(
        '令 名字 = 输入("请输入名字：")\n打印("你好 " + 名字)\n', "<t>")),
        encoding="utf-8")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), str(bc)],
                       capture_output=True, text=True, encoding="utf-8",
                       input="小明\n")
    assert r.returncode == 0, r.stderr[:300]
    # 提示语与结果都在 stdout，中文完整（同步写，不会被吞字符）
    assert r.stdout == "请输入名字：你好 小明\n"


def test_node_input_eof_is_chinese():
    """stdin 到末尾时报「输入结束」，与 Python 的 `input()` 一致。"""
    bc = tmp_bc_path(ROOT / "_tmp_m30_in2.json")
    bc.write_text(serialize.dumps(compile_source("令 x = 输入()\n打印(x)\n", "<t>")),
                  encoding="utf-8")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), str(bc)],
                       capture_output=True, text=True, encoding="utf-8", input="")
    assert r.returncode != 0
    assert "输入结束" in r.stderr


# ---------------------------------------------------------------------------
# 14. 真实示例项目端到端（宿主补齐的验收标准）
# ---------------------------------------------------------------------------
# 这两个项目用到：切片、集合（日志统计的推导式）、表格、文本.格式化、
# 系统.参数 / `输入()`、上下文管理器、文件读写——正好压过本轮补的大部分能力。

def _run_project(project: str, script: str, arg: str, feed: str | None) -> None:
    src_path = ROOT / "examples" / "projects" / project / script
    if not src_path.exists():
        pytest.skip(f"示例项目不存在：{src_path}")
    src = src_path.read_text(encoding="utf-8")
    bc = tmp_bc_path(ROOT / "_tmp_m30_proj.json")
    bc.write_text(serialize.dumps(compile_source(src, str(src_path))), encoding="utf-8")
    cwd = str(src_path.parent)
    r_py = subprocess.run(
        [sys.executable, "-m", "jishi.cli", script, arg] if feed is None
        else [sys.executable, "-m", "jishi.cli", script],
        capture_output=True, text=True, encoding="utf-8", cwd=cwd, input=feed)
    r_node = subprocess.run(
        [NODE, str(ROOT / "node" / "index.js"), str(bc), arg] if feed is None
        else [NODE, str(ROOT / "node" / "index.js"), str(bc)],
        capture_output=True, text=True, encoding="utf-8", cwd=cwd, input=feed)
    assert r_node.returncode == r_py.returncode, (
        f"退出码不同：py={r_py.returncode} node={r_node.returncode}\n"
        f"node stderr={r_node.stderr[:300]}")
    assert r_node.stdout == r_py.stdout, (
        f"输出不同\nPython: {r_py.stdout[:400]!r}\nNode:   {r_node.stdout[:400]!r}")


def test_node_real_project_grade_analysis():
    """成绩分析：表格 + 文本.格式化 + 系统.参数。"""
    _run_project("成绩分析", "分析.jsh", "成绩.csv", None)


def test_node_real_project_log_stats_via_stdin():
    """日志统计：切片（`[::-1]`）+ 集合 + 上下文管理器 + `输入()` 交互。"""
    _run_project("日志统计", "统计.jsh", "", "\n")


def test_node_throw_and_catch_still_works():
    """规范化错误输出不能影响程序内的 `尝试/捕获`。"""
    assert _agree_all(
        "尝试：\n    抛出 值错误(\"出错了\")\n"
        "捕获 值错误 为 e：\n    打印(\"接住\")\n"
        "尝试：\n    打印(1 / 0)\n"
        "捕获 除零错误 为 e：\n    打印(\"接了除零\")\n"
    ) == "接住\n接了除零\n"
