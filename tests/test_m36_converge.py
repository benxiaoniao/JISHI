# -*- coding: utf-8 -*-
"""M36 主线 A（语言面最后收敛）：位运算内建 + `字典.合并`。

这一批的共同点：**不动语法**（关 0）。位运算 `& | ^ << >>` 日常业务代码罕见，
为它加五个运算符要动三执行器 + 两宿主 + 词法器，收益不成比例；改成 6 个内建，
零语法成本。`字典合并 |` 同理，一个方法就够。

测试关注三件事：
1. **三执行器逐字节一致**（硬约束 #1）；
2. **错误路径是明确报错**（类型不符、位数非法），不静默算错；
3. **不破坏别的值**（`字典.合并` 返回新字典，双方都不动）。
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi import cvm_bind                        # noqa: E402
from jishi import serialize                       # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.errors import JishiError               # noqa: E402
from jishi.interpreter import Interpreter         # noqa: E402
from jishi.parser import parse                    # noqa: E402
from jishi.tokenizer import tokenize              # noqa: E402
from conftest import rust_exe_path                # noqa: E402

NODE = shutil.which("node")
CARGO = shutil.which("cargo")


@pytest.fixture(scope="session")
def rust_exe() -> Path:
    """Rust 宿主可执行文件（没有 cargo 就跳过；路径按平台取名，见 conftest）。"""
    exe = rust_exe_path()
    if not CARGO:
        pytest.skip("本机无 cargo，跳过 Rust 对拍")
    if exe.exists():
        return exe
    r = subprocess.run(
        [CARGO, "build", "--release", "--manifest-path",
         str(ROOT / "rust" / "Cargo.toml")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT))
    if r.returncode != 0:
        pytest.skip(f"cargo build 失败：{r.stderr[-200:]}")
    return exe


def _run_host(src: str, host: str, rust_exe: "Path | None", tmp: Path) -> str:
    """把源码编成字节码丢给宿主跑，返回 stdout（宿主非零退出直接报错）。"""
    bc = tmp / f"_m36_{host}.json"
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    cmd = ([NODE, str(ROOT / "node" / "index.js"), str(bc)] if host == "node"
           else [str(rust_exe), str(bc)])
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"{host} 非零退出（rc={r.returncode}）：{r.stderr[-300:]}"
    return r.stdout


def _all_five(src: str, rust_exe: Path, tmp: Path) -> str:
    """三执行器 + Node + Rust **五路逐字节一致**。"""
    want = _all(src)
    if NODE:
        got = _run_host(src, "node", None, tmp)
        assert got == want, f"Node 与 Python 不一致\nPython: {want!r}\nNode:   {got!r}"
    got = _run_host(src, "rust", rust_exe, tmp)
    assert got == want, f"Rust 与 Python 不一致\nPython: {want!r}\nRust:   {got!r}"
    return want


def _tree(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        Interpreter(filename="<t>").run(
            parse(tokenize(src, "<t>"), src.split("\n"), "<t>"))
    return buf.getvalue()


def _py(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _c(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _all(src: str) -> str:
    """三执行器逐字节一致（不同就报错）。"""
    t, p, c = _tree(src), _py(src), _c(src)
    assert t == p == c, f"三执行器不一致：\n树遍历={t!r}\nPythonVM={p!r}\nCVM={c!r}"
    return p


def _err(src: str) -> str:
    """三执行器都必须报错，返回错误首行（文案可不同，但都得是基石异常）。"""
    first = None
    for label, fn in (("树遍历", _tree), ("Python VM", _py), ("C VM", _c)):
        with pytest.raises(JishiError) as ei:
            fn(src)
        line = str(ei.value).splitlines()[0]
        assert line.startswith("错误 "), f"{label} 的错误不像基石异常：{line}"
        first = first or line
    return first


# ---------------------------------------------------------------------------
# 1. 位运算内建
# ---------------------------------------------------------------------------

def test_bitwise_basics_agree_everywhere():
    """与/或/异或/取反 —— 用 12(1100) 与 10(1010) 这组能看出每一位的数。"""
    assert _all(
        "打印(位与(12, 10))\n"
        "打印(位或(12, 10))\n"
        "打印(位异或(12, 10))\n"
        "打印(位取反(0), 位取反(5))\n"
    ) == "8\n14\n6\n-1 -6\n"


def test_shift_basics_agree_everywhere():
    assert _all(
        "打印(左移(1, 4), 左移(3, 3))\n"
        "打印(右移(16, 4), 右移(17, 4))\n"     # 向下取整：17>>4 = 1
        "打印(右移(-1, 1))\n"                  # 算术右移：-1 >> 1 = -1
    ) == "16 24\n1 1\n-1\n"


def test_bitwise_mask_idiom():
    """真实惯用法：标志位（读=1、写=2、执行=4）。"""
    assert _all(
        "令 读 = 1\n令 写 = 2\n令 执行 = 4\n"
        "令 权限 = 位或(读, 执行)\n"
        "打印(位与(权限, 读) > 0, 位与(权限, 写) > 0, 位与(权限, 执行) > 0)\n"
        "打印(位异或(权限, 读) == 执行)\n"
    ) == "真 假 真\n真\n"


def test_bitwise_is_usable_inside_expressions_and_loops():
    """内建也是普通值，能进表达式、能循环里用（不是语法糖，没有特殊待遇）。"""
    assert _all(
        "令 奇偶 = []\n"
        "遍历 x 在 范围(4)：\n"
        "    奇偶.追加(位与(x, 1))\n"
        "打印(奇偶)\n"
    ) == "[0, 1, 0, 1]\n"


# ---------------------------------------------------------------------------
# 2. 位运算的错误路径（明确报错，不静默算）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src, keyword", [
    ("打印(位与(1.5, 2))\n", "需要整数"),        # 小数
    ("打印(位或(真, 1))\n", "需要整数"),         # 布尔是独立类型，不当 1/0 算
    ("打印(位异或(\"甲\", 1))\n", "需要整数"),   # 文本
    ("打印(位与(空, 1))\n", "需要整数"),         # 空
    ("打印(左移(1, -1))\n", "不能是负数"),       # 负位数
    ("打印(右移(8, 1.5))\n", "需要整数"),        # 位数是小数
    ("打印(左移(1, 2000000))\n", "太大"),        # 防「1 << 10**9 申请 125MB」
])
def test_bitwise_errors_are_explicit(src, keyword):
    assert keyword in _err(src)


def test_bit_not_rejects_bool_too():
    """`位取反(真)` 也要拦：布尔进位运算是「逻辑值/标志位」混用。"""
    assert "需要整数" in _err("打印(位取反(真))\n")


# ---------------------------------------------------------------------------
# 3. 字典.合并
# ---------------------------------------------------------------------------

def test_dict_merge_returns_new_dict_and_leaves_both_alone():
    assert _all(
        "令 默认 = {\"甲\": 1, \"乙\": 2}\n"
        "令 用户 = {\"乙\": 9, \"丙\": 3}\n"
        "令 全 = 默认.合并(用户)\n"
        "打印(全)\n"
        "打印(默认)\n"          # 原字典不动
        "打印(用户)\n"          # 参数也不动
    ) == ("{'甲': 1, '乙': 9, '丙': 3}\n"
          "{'甲': 1, '乙': 2}\n"
          "{'乙': 9, '丙': 3}\n")


def test_dict_merge_later_wins():
    """同名键以**参数**为准（与「更新」一致，也和 Python 3.9 的 `|` 一致）。"""
    assert _all("打印({\"甲\": 1}.合并({\"甲\": 2}))\n") == "{'甲': 2}\n"


def test_dict_merge_with_empty():
    assert _all(
        "打印({\"甲\": 1}.合并({}))\n"
        "打印({}.合并({\"甲\": 1}))\n"
    ) == "{'甲': 1}\n{'甲': 1}\n"


def test_dict_merge_is_shallow_not_deep():
    """浅合并：值的**对象本身**共享（"甲" 那个列表两边是同一个）。"""
    assert _all(
        "令 内 = [1]\n"
        "令 a = {\"x\": 内}\n"
        "令 b = a.合并({})\n"
        "b[\"x\"].追加(2)\n"
        "打印(a[\"x\"], b[\"x\"])\n"
    ) == "[1, 2] [1, 2]\n"


def test_dict_merge_does_not_mutate_self_like_update_does():
    """「更新」就地改自己、「合并」给新字典——两者语义要分得开。"""
    assert _all(
        "令 a = {\"甲\": 1}\n"
        "令 b = a.更新({\"乙\": 2})\n"      # 更新返回空、改的是 a
        "打印(a)\n"
        "打印(b)\n"
        "令 c = {\"甲\": 1}\n"
        "令 d = c.合并({\"乙\": 2})\n"
        "打印(c)\n"
        "打印(d)\n"
    ) == ("{'甲': 1, '乙': 2}\n"
          "空\n"
          "{'甲': 1}\n"
          "{'甲': 1, '乙': 2}\n")


@pytest.mark.parametrize("arg", ["[1, 2]", "\"甲\"", "1", "空"])
def test_dict_merge_rejects_non_dict(arg):
    assert "需要一个字典参数" in _err(f"打印({{\"甲\": 1}}.合并({arg}))\n")


def test_dict_merge_arity():
    """参数个数不对，三个执行器都要拦下。"""
    assert "参数" in _err("打印({\"甲\": 1}.合并())\n")
    assert "参数" in _err("打印({\"甲\": 1}.合并({}, {}))\n")


# ---------------------------------------------------------------------------
# 4. 这些内建不该被误当成语法糖
# ---------------------------------------------------------------------------

def test_bitwise_names_are_plain_names_not_keywords():
    """位运算名是**普通标识符**：可以被遮蔽、也能当字典键（内建不是关键字）。"""
    assert _all(
        "令 位与 = 函数(a, b)：a + b\n"        # 遮蔽内建
        "打印(位与(1, 2))\n"
        "令 d = {\"位与\": 1}\n"
        "打印(d[\"位与\"])\n"
    ) == "3\n1\n"


def test_probe_reports_no_open_questions():
    """探针必须报「无悬案」——这是主线 A 的验收判据（M36）。"""
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "probe_capabilities.py")],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    assert r.returncode == 0, f"探针报告还有待定项：\n{r.stdout[-600:]}"
    assert "无悬案" in r.stdout


# ---------------------------------------------------------------------------
# 5. 三元表达式（M36 A1）：解析期脱糖成匿名函数立即调用
# ---------------------------------------------------------------------------

def test_ternary_basic():
    assert _all(
        '令 年龄 = 20\n'
        '令 等级 = "成年" 如果 年龄 >= 18 否则 "未成年"\n'
        '打印(等级)\n'
    ) == "成年\n"


def test_ternary_only_evaluates_one_side():
    """**只求值选中的那一边**——条件表达式最关键的性质（不是「两个都算」）。"""
    assert _all(
        '函数 f(名)：\n'
        '    打印(名)\n'
        '    返回 名\n'
        '打印(f("甲") 如果 真 否则 f("乙"))\n'
        '打印(f("丙") 如果 假 否则 f("丁"))\n'
    ) == "甲\n甲\n丁\n丁\n"


def test_ternary_is_right_associative():
    assert _all(
        '令 a = 2\n'
        '打印("一" 如果 a == 1 否则 "二" 如果 a == 2 否则 "其他")\n'
    ) == "二\n"


def test_ternary_binds_looser_than_and_or():
    """优先级比「与/或」低（与 Python 同级）。"""
    assert _all("打印(假 或 真 如果 假 否则 假)\n") == "假\n"


def test_ternary_in_arguments_and_subscripts():
    assert _all(
        '令 d = {"k": 5}\n'
        '打印(最大(1, 5 如果 真 否则 9))\n'
        '打印(d["k"] 如果 "k" 在 d 否则 -1)\n'
    ) == "5\n5\n"


def test_ternary_nests_inside_comprehension_element():
    """推导式**元素**可以是三元（Python 亦然）。"""
    assert _all(
        '打印([("偶" 如果 x % 2 == 0 否则 "奇") 遍历 x 在 范围(4)])\n'
    ) == "['偶', '奇', '偶', '奇']\n"


def test_ternary_does_not_break_comprehension_filter():
    """**回归钉**：推导式的「如果」是过滤子句，不能被迭代对象吃成三元。

    这是实现 M36 时真踩到的坑；Python 也是靠「comp_for 的可迭代对象只到
    or_test 层级」避开的（见 parser._parse_comp_tail 的 allow_ternary=False）。
    """
    assert _all("打印([x * 2 遍历 x 在 范围(5) 如果 x % 2 == 0])\n") == "[0, 4, 8]\n"


def test_ternary_missing_else_is_explicit_error():
    assert "否则" in _err("打印(1 如果 真)\n")


def test_ternary_does_not_break_match_guard():
    """**回归钉（第二个同类位置）**：`情形 值 如果 条件：` 里的「如果」是**守卫**。

    给「已有的关键字」加表达式用法时，把该关键字**所有既有位置**都过一遍才对：
    M36 第一次撞的是推导式过滤子句，第二次就是这里（全量回归抓到的）。
    想拿条件表达式当值，加括号即可。
    """
    assert _all(
        '令 加分 = 真\n'
        '匹配 90：\n'
        '    情形 90 如果 加分：\n'
        '        打印("含加分")\n'
        '    情形 90：\n'
        '        打印("普通")\n'
    ) == "含加分\n"


def test_ternary_still_works_next_to_guard_and_filter():
    """三个「如果」同场共存：条件表达式 / 匹配守卫 / 推导式过滤。"""
    assert _all(
        '令 加分 = 假\n'
        '匹配 90：\n'
        '    情形 90 如果 加分：\n'
        '        打印("含加分")\n'
        '    情形 90：\n'
        '        打印("普通")\n'
        '打印("优" 如果 加分 否则 "常")\n'
        '打印([x 如果 x > 0 否则 0 遍历 x 在 [-1, 2]])\n'
        '打印([x 遍历 x 在 范围(4) 如果 x % 2 == 0])\n'
    ) == "普通\n常\n[0, 2]\n[0, 2]\n"


# ---------------------------------------------------------------------------
# 6. 宿主同步：这些内建 / 方法在 Node / Rust 上也要一致
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src", [
    "打印(位与(12, 10), 位或(12, 10), 位异或(12, 10), 位取反(0), 位取反(5))\n",
    "打印(左移(1, 4), 左移(3, 3), 右移(16, 4), 右移(-1, 1), 右移(1, 200))\n",
    "打印(左移(1, 60))\n",                    # 60 位：JS 必须走 BigInt 才对得上
    "打印(位与(2 ** 40, 2 ** 40 - 1))\n",
    '令 a = {"甲": 1, "乙": 2}\n令 b = {"乙": 9}\n'
    '令 c = a.合并(b)\n打印(c)\n打印(a)\n',
    '令 a = {"甲": 1}\n打印(a.更新({"乙": 2}))\n打印(a)\n',
    '令 a = {"甲": 1}\n打印(a.弹出("甲"))\n打印(a)\n打印(a.弹出("无", "兜底"))\n',
    '令 年龄 = 20\n打印("成年" 如果 年龄 >= 18 否则 "未成年")\n',
])
def test_hosts_agree_on_m36_additions(src, rust_exe, tmp_path):
    """三执行器 + Node + Rust 五路一致。"""
    _all_five(src, rust_exe, tmp_path)
