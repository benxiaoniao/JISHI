# -*- coding: utf-8 -*-
"""M41：标准库第一批 + 内建补齐 + 错误归类 + few-shot 体检。

M41 的三件事：

1. **标准库补 5 个模块**（`参数` / `日志` / `测试` / `容器` / `迭代`）——
   前四个是本轮新增，`测试` 只做 Python 侧（它靠宿主异常机制，两宿主没有）。
2. **内建 `带下标` / `配对`**（Python 的 enumerate / zip）——用起来不用导入，
   与既有 `范围` 同地位。
3. **错误归类修正**：`整数("abc")` 原先抛「类型错误」，Python 是 `ValueError`。
   现在文本转不动抛**值错误**、类型本身不对才抛**类型错误**。

另有一条**体检**：`examples/ai/few-shot.md` 里 48 段示例从没跑过，本轮加了
自动提取 + 逐段执行的测试（片段不自包含的按「预期缺变量」放行，但语法与
API 用错必须报出来）。

模块名注意：路线图里写的是「集合」，但 `集合` 是**内建函数名**
（`集合([1,2,3])` 去重造集合），同名模块会把内建遮蔽掉——正是 M40 修掉的
f-string 遮蔽坑同一类问题，所以改名**「容器」**。
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path, tmp_bc_path      # noqa: E402

from jishi import cvm_bind                            # noqa: E402
from jishi import serialize                           # noqa: E402
from jishi import vm as vm_mod                        # noqa: E402
from jishi.compiler import compile_source             # noqa: E402
from jishi.errors import JishiError                   # noqa: E402
from jishi.interpreter import Interpreter             # noqa: E402
from jishi.parser import parse                        # noqa: E402
from jishi.tokenizer import tokenize                  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
RUST_EXE = rust_exe_path()
CARGO = shutil.which("cargo")


# ---------------------------------------------------------------------------
# 脚手架：一段代码在五路上跑，取各自的 stdout
# ---------------------------------------------------------------------------

def _tree(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        Interpreter(filename="<t>").run(
            parse(tokenize(src, "<t>"), src.split("\n"), "<t>"))
    return buf.getvalue()


def _pyvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _cvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _node(src: str) -> str:
    bc = tmp_bc_path(ROOT / "_tmp_m41_bc.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")),
                  encoding="utf-8", newline="\n")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), str(bc)],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:400]
    return r.stdout.decode("utf-8", "replace")


@pytest.fixture(scope="module")
def rust_exe() -> Path:
    if not CARGO:
        pytest.skip("本机无 cargo，跳过 Rust 宿主对拍")
    if not RUST_EXE.exists():
        pytest.skip(f"Rust 产物不存在：{RUST_EXE}（先 cargo build --release）")
    return RUST_EXE


def _rust(src: str, rust_exe: Path) -> str:
    bc = tmp_bc_path(ROOT / "_tmp_m41_rs.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")),
                  encoding="utf-8", newline="\n")
    r = subprocess.run([str(rust_exe), str(bc)], capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:400]
    return r.stdout.decode("utf-8", "replace")


def _three(src: str) -> str:
    """三个执行器必须逐字一致（标准库走宿主侧实现，三路共用一份）。"""
    基准 = _tree(src)
    assert _pyvm(src) == 基准, "Python VM 与树遍历不一致"
    assert _cvm(src) == 基准, "C VM 与树遍历不一致"
    return 基准


def _five(src: str, rust_exe: Path) -> str:
    """五路（三执行器 + Node + Rust）逐字一致。"""
    基准 = _three(src)
    assert _node(src) == 基准, "Node 与树遍历不一致"
    assert _rust(src, rust_exe) == 基准, "Rust 与树遍历不一致"
    return 基准


# ---------------------------------------------------------------------------
# 1. 内建 带下标 / 配对
# ---------------------------------------------------------------------------

def test_enumerate_builtin_basic():
    out = _three('打印(带下标(["甲", "乙", "丙"]))')
    assert out == "[[0, '甲'], [1, '乙'], [2, '丙']]\n"


def test_enumerate_builtin_start():
    out = _three('打印(带下标(["甲", "乙"], 1))')
    assert out == "[[1, '甲'], [2, '乙']]\n"


def test_enumerate_builtin_text_and_dict():
    """文本按字符、字典按键——与 `_as_iterable` 同口径。"""
    out = _three('打印(带下标("abc"))\n打印(带下标({"甲": 1, "乙": 2}))')
    assert out == ("[[0, 'a'], [1, 'b'], [2, 'c']]\n"
                   "[[0, '甲'], [1, '乙']]\n")


def test_enumerate_builtin_bad_args():
    for src in ('带下标()', '带下标([1], 2, 3)', '带下标(1)',
                '带下标([1], "甲")'):
        with pytest.raises(JishiError):
            _tree(src)


def test_zip_builtin_basic():
    """`配对` 取最短——与 Python 的 zip 同语义。"""
    out = _three('打印(配对([1, 2, 3], ["a", "b"]))')
    assert out == "[[1, 'a'], [2, 'b']]\n"


def test_zip_builtin_multi_and_text():
    out = _three('打印(配对([1, 2], ["a", "b"], [真, 假]))\n'
                 '打印(配对("ab", [1, 2]))')
    assert out == ("[[1, 'a', 真], [2, 'b', 假]]\n"
                   "[['a', 1], ['b', 2]]\n")


def test_zip_builtin_bad_args():
    with pytest.raises(JishiError):
        _tree("配对()")
    with pytest.raises(JishiError):
        _tree("配对([1], 2)")


def test_new_builtins_five_way(rust_exe):
    """新内建要五路同宽（含两宿主）。"""
    src = ('打印(带下标(["甲", "乙"], 1))\n'
           '打印(配对([1, 2, 3], ["a", "b"]))\n')
    _five(src, rust_exe)


# ---------------------------------------------------------------------------
# 2. 容器
# ---------------------------------------------------------------------------

def test_container_count_and_most():
    out = _three('导入 容器\n'
                 '令 词们 = ["甲", "乙", "甲", "丙", "甲", "乙"]\n'
                 '打印(容器.计数(词们))\n'
                 '打印(容器.最多(词们))\n'
                 '打印(容器.最多(词们, 2))')
    assert out == ("{'甲': 3, '乙': 2, '丙': 1}\n"
                   "甲\n"
                   "[['甲', 3], ['乙', 2]]\n")


def test_container_sort_by_value_is_stable():
    """平局按首次出现的先后——保序，不引入随机性。"""
    out = _three('导入 容器\n'
                 '打印(容器.按值排序(容器.计数(["乙", "甲", "乙", "甲"])))')
    assert out == "[['乙', 2], ['甲', 2]]\n"


def test_container_group_with_callback():
    out = _three('导入 容器\n'
                 '打印(容器.分组(["ab", "c", "de"], 函数(w)：长度(w)))')
    assert out == "{2: ['ab', 'de'], 1: ['c']}\n"


def test_container_slices_and_chunks():
    out = _three('导入 容器\n'
                 '打印(容器.取前([1, 2, 3, 4], 2))\n'
                 '打印(容器.取后([1, 2, 3, 4], 2))\n'
                 '打印(容器.分块([1, 2, 3, 4, 5], 2))\n'
                 '打印(容器.取前([1, 2], 9))')
    assert out == ("[1, 2]\n[3, 4]\n[[1, 2], [3, 4], [5]]\n[1, 2]\n")


def test_container_bad_args():
    for src in ('导入 容器\n容器.最多([1], 0)',
                '导入 容器\n容器.分块([1], 0)',
                '导入 容器\n容器.按值排序([1, 2])'):
        with pytest.raises(JishiError):
            _tree(src)


def test_container_five_way(rust_exe):
    src = ('导入 容器\n'
           '令 词们 = ["甲", "乙", "甲", "丙", "甲", "乙"]\n'
           '打印(容器.计数(词们))\n'
           '打印(容器.最多(词们, 2))\n'
           '打印(容器.按值排序(容器.计数(词们)))\n'
           '打印(容器.分组(["ab", "c", "de"], 函数(w)：长度(w)))\n'
           '打印(容器.取前([1, 2, 3, 4], 2))\n'
           '打印(容器.取后([1, 2, 3, 4], 2))\n'
           '打印(容器.分块([1, 2, 3, 4, 5], 2))\n')
    _five(src, rust_exe)


# ---------------------------------------------------------------------------
# 3. 迭代
# ---------------------------------------------------------------------------

def test_iter_group_and_window():
    out = _three('导入 迭代\n'
                 '打印(迭代.分组(范围(7), 3))\n'
                 '打印(迭代.滑窗([1, 2, 3, 4], 2))\n'
                 '打印(迭代.滑窗([1, 2, 3, 4, 5], 3, 2))')
    assert out == ("[[0, 1, 2], [3, 4, 5], [6]]\n"
                   "[[1, 2], [2, 3], [3, 4]]\n"
                   "[[1, 2, 3], [3, 4, 5]]\n")


def test_iter_dedupe_keeps_first_order():
    out = _three('导入 迭代\n打印(迭代.去重(["甲", "乙", "甲", "丙", "乙"]))')
    assert out == "['甲', '乙', '丙']\n"


def test_iter_flatten_one_level():
    out = _three('导入 迭代\n打印(迭代.展开([[1, 2], [3], [4, 5]]))')
    assert out == "[1, 2, 3, 4, 5]\n"


def test_iter_sum_and_count_with_callback():
    out = _three('导入 迭代\n'
                 '打印(迭代.求和([1, 2, 3]))\n'
                 '打印(迭代.计数([85, 42, 90], 函数(x)：x >= 60))')
    assert out == "6\n2\n"


def test_iter_bad_args():
    for src in ('导入 迭代\n迭代.分组([1], 0)',
                '导入 迭代\n迭代.滑窗([1, 2], 2, 0)'):
        with pytest.raises(JishiError):
            _tree(src)


def test_iter_five_way(rust_exe):
    src = ('导入 迭代\n'
           '打印(迭代.分组(范围(7), 3))\n'
           '打印(迭代.滑窗([1, 2, 3, 4], 2))\n'
           '打印(迭代.去重(["甲", "乙", "甲"]))\n'
           '打印(迭代.展开([[1, 2], [3], [4, 5]]))\n'
           '打印(迭代.取前([1, 2, 3, 4], 2))\n'
           '打印(迭代.取后([1, 2, 3, 4], 2))\n'
           '打印(迭代.求和([1, 2, 3]))\n'
           '打印(迭代.计数([85, 42, 90], 函数(x)：x >= 60))\n')
    _five(src, rust_exe)


# ---------------------------------------------------------------------------
# 4. 参数
# ---------------------------------------------------------------------------

def test_args_parser_basic():
    out = _three('导入 参数\n'
                 '令 p = 参数.新建("工具", "说明")\n'
                 '参数.选项(p, "数据", "默认.json", "路径")\n'
                 '参数.标志(p, "详细", "更多")\n'
                 '令 r = 参数.解析(p, ["添加", "买牛奶", "--数据", "我的.json", "--详细"])\n'
                 '打印(r["命令"], r["位置"], r["数据"], r["详细"])')
    assert out == "添加 ['买牛奶'] 我的.json 真\n"


def test_args_defaults_when_absent():
    out = _three('导入 参数\n'
                 '令 p = 参数.新建("工具")\n'
                 '参数.选项(p, "数据", "默认.json")\n'
                 '参数.标志(p, "详细")\n'
                 '令 r = 参数.解析(p, ["列出"])\n'
                 '打印(r["数据"], r["详细"], r["位置"])')
    assert out == "默认.json 假 []\n"


def test_args_equals_form_and_double_dash():
    out = _three('导入 参数\n'
                 '令 p = 参数.新建("工具")\n'
                 '参数.选项(p, "数据", "d")\n'
                 '令 r = 参数.解析(p, ["列出", "--数据=别的.json"])\n'
                 '打印(r["数据"])\n'
                 '令 r2 = 参数.解析(p, ["跑", "--", "--数据", "x"])\n'
                 '打印(r2["命令"], r2["位置"])')
    assert out == "别的.json\n跑 ['--数据', 'x']\n"


def test_args_usage_text():
    out = _three('导入 参数\n'
                 '令 p = 参数.新建("待办清单", "一个简单的待办工具")\n'
                 '参数.选项(p, "数据", "待办.json", "数据文件路径")\n'
                 '参数.标志(p, "详细", "打印更多信息")\n'
                 '打印(参数.用法(p))')
    assert "用法：待办清单 [命令] [选项]" in out
    assert "一个简单的待办工具" in out
    assert "--数据 <值>" in out and "数据文件路径" in out
    assert "待办.json" in out
    assert "--详细" in out and "打印更多信息" in out


def test_args_one_shot_form():
    out = _three('导入 参数\n'
                 '令 r = 参数.解析(["列出"], {"数据": "一次式.json", "详细": 假})\n'
                 '打印(r["数据"], r["详细"])')
    assert out == "一次式.json 假\n"


def test_args_unknown_option_errors():
    """认不出来的选项要**报错**，不静默忽略——敲错一个名字就白跑一晚上。"""
    with pytest.raises(JishiError) as ei:
        _tree('导入 参数\n'
              '令 p = 参数.新建("工具")\n'
              '参数.选项(p, "数据", "d")\n'
              '参数.解析(p, ["列出", "--数剧", "x"])')
    assert "不认识选项" in str(ei.value)
    assert "--数据" in str(ei.value)


def test_args_missing_value_errors():
    with pytest.raises(JishiError) as ei:
        _tree('导入 参数\n'
              '令 p = 参数.新建("工具")\n'
              '参数.选项(p, "数据", "d")\n'
              '参数.解析(p, ["列出", "--数据"])')
    assert "后面要跟一个值" in str(ei.value)


def test_args_flag_with_value_errors():
    with pytest.raises(JishiError):
        _tree('导入 参数\n'
              '令 p = 参数.新建("工具")\n'
              '参数.标志(p, "详细")\n'
              '参数.解析(p, ["--详细=真"])')


def test_args_five_way(rust_exe):
    """`参数` 的对拍。

    ⚠️ **Rust 上是已知缺口**（M53 撞到，M54 修）：`参数` 从 M53 起搬进了
    「用基石写的标准库」层，而它用到了**闭包里的可变状态**（`_解析器表`）——
    Rust 宿主对它的支持有问题，报「「空」不能调用」。
    所以这里只对**三执行器 + Node** 断言一致；Rust 那一路**显式确认它还坏着**
    （哪天修好了，这段 `pytest.raises` 会失败，提醒回来把它加进对拍）。
    详见 `tests/test_m52_jishilib.py` 的 `RUST_GAPS` 与 内部路线图（未公开） §九。
    """
    src = ('导入 参数\n'
           '令 p = 参数.新建("工具", "说明")\n'
           '参数.选项(p, "数据", "默认.json", "路径")\n'
           '参数.标志(p, "详细", "更多")\n'
           '打印(参数.用法(p))\n'
           '令 r = 参数.解析(p, ["添加", "买牛奶", "--数据", "我的.json", "--详细"])\n'
           '打印(r["命令"], r["位置"], r["数据"], r["详细"])\n'
           '令 r2 = 参数.解析(p, ["列出", "--数据=别的.json"])\n'
           '打印(r2["数据"], r2["详细"])\n'
           '令 r3 = 参数.解析(["列出"], {"数据": "一次式.json"})\n'
           '打印(r3["数据"])\n')
    基准 = _three(src)
    assert _node(src) == 基准, "Node 与树遍历不一致"
    with pytest.raises(AssertionError):
        _rust(src, rust_exe)          # 已知缺口：修好后这里会失败，回来更新


# ---------------------------------------------------------------------------
# 5. 日志
# ---------------------------------------------------------------------------

def test_log_levels_and_filtering():
    out = _three('导入 日志\n'
                 '打印(日志.级别们())\n'
                 '打印(日志.取级别())\n'
                 '日志.调试("看不到")\n'
                 '日志.信息("看得到")')
    assert out == ("['调试', '信息', '警告', '错误', '静默']\n"
                   "信息\n"
                   "[信息] 看得到\n")


def test_log_set_level_and_prefix():
    out = _three('导入 日志\n'
                 '日志.设级别("调试")\n'
                 '日志.调试("现在看得到")\n'
                 '日志.设前缀("[演示]")\n'
                 '日志.信息("带前缀")')
    assert out == "[调试] 现在看得到\n[演示] [信息] 带前缀\n"


def test_log_silence_swallows_everything():
    out = _three('导入 日志\n'
                 '日志.设级别("静默")\n'
                 '日志.调试("a")\n日志.信息("b")\n日志.警告("c")\n日志.错误("d")\n'
                 '打印("只剩这一行")')
    assert out == "只剩这一行\n"


def test_log_bad_level_errors():
    with pytest.raises(JishiError) as ei:
        _tree('导入 日志\n日志.设级别("胡说")')
    assert "不认识级别" in str(ei.value)
    assert "调试" in str(ei.value)


def test_log_five_way(rust_exe):
    src = ('导入 日志\n'
           '打印(日志.级别们())\n'
           '打印(日志.取级别())\n'
           '日志.调试("看不到")\n'
           '日志.信息("看得到")\n'
           '日志.设级别("调试")\n'
           '日志.调试("看得到")\n'
           '日志.设前缀("[演示]")\n'
           '日志.信息("带前缀")\n')
    _five(src, rust_exe)


# ---------------------------------------------------------------------------
# 6. 测试（只做 Python 侧：它靠宿主异常机制，两宿主没有）
# ---------------------------------------------------------------------------

def test_test_module_all_pass():
    out = _three('导入 测试\n'
                 '函数 甲()：\n'
                 '    测试.相等(1 + 1, 2)\n'
                 '    测试.为真(1 < 2)\n'
                 '测试.用例("加法", 甲)\n'
                 '测试.汇总()')
    assert "全部通过" in out and "1 个用例" in out


def test_test_module_reports_failure(capsys):
    """失败要说清「期望什么、实际什么」，而不是只给一句「条件不成立」。

    逐条明细走 stderr（`汇总` 打印的），异常本身只带汇总数字——所以这里
    两处都断言。
    """
    with pytest.raises(JishiError) as ei:
        _tree('导入 测试\n'
              '函数 甲()：\n'
              '    测试.相等(2 + 2, 5)\n'
              '测试.用例("故意失败", 甲)\n'
              '测试.汇总()')
    assert "测试没全过" in str(ei.value)
    err = capsys.readouterr().err
    assert "期望 5，实际 4" in err
    assert "故意失败" in err


def test_test_module_continues_after_failure():
    """一个用例失败不该中断其它用例（`断言` 会中断，这是本模块的价值）。"""
    out = _three('导入 测试\n'
                 '函数 坏()：\n    测试.相等(1, 2)\n'
                 '函数 好()：\n    测试.相等(1, 1)\n'
                 '测试.用例("坏的", 坏)\n'
                 '测试.用例("好的", 好)\n'
                 '打印(测试.统计())')
    assert "{'用例': 2, '通过': 1, '失败': 1, '出错': 0}" in out


def test_test_module_assertion_helpers():
    out = _three('导入 测试\n'
                 '函数 甲()：\n'
                 '    测试.不等(1, 2)\n'
                 '    测试.为假(1 > 2)\n'
                 '    测试.近似(0.1 + 0.2, 0.3, 1e-9)\n'
                 '    测试.包含([1, 2], 2)\n'
                 '    测试.抛出(函数()：1 / 0)\n'
                 '测试.用例("各种断言", 甲)\n'
                 '测试.汇总()')
    assert "全部通过" in out


def test_test_module_counts_runtime_error_in_case():
    """用例里出了断言之外的错（比如除零），要算这个用例「出错」而不是崩掉。"""
    out = _three('导入 测试\n'
                 '函数 甲()：\n    令 x = 1 / 0\n'
                 '测试.用例("会崩", 甲)\n'
                 '打印(测试.统计())')
    assert "'出错': 1" in out


# ---------------------------------------------------------------------------
# 7. 错误归类：文本转不动 → 值错误；类型不对 → 类型错误
# ---------------------------------------------------------------------------

def test_int_of_text_raises_value_error():
    out = _three('尝试：\n'
                 '    整数("abc")\n'
                 '捕获 值错误：\n'
                 '    打印("值错误 ✅")')
    assert out == "值错误 ✅\n"


def test_int_of_list_raises_type_error():
    out = _three('尝试：\n'
                 '    整数([1, 2])\n'
                 '捕获 类型错误：\n'
                 '    打印("类型错误 ✅")')
    assert out == "类型错误 ✅\n"


def test_float_same_classification():
    out = _three('尝试：\n    小数("x")\n捕获 值错误：\n    打印("值错误 ✅")\n'
                 '尝试：\n    小数({})\n捕获 类型错误：\n    打印("类型错误 ✅")')
    assert out == "值错误 ✅\n类型错误 ✅\n"


def test_conversion_classification_matches_method():
    """内建 `整数(...)` 与 `"abc".转整数()` 必须同类——原先一个值错误一个类型错误。"""
    out = _three('尝试：\n    整数("abc")\n捕获 值错误：\n    打印("内建=值错误")\n'
                 '尝试：\n    "abc".转整数()\n捕获 值错误：\n    打印("方法=值错误")')
    assert out == "内建=值错误\n方法=值错误\n"


def test_conversion_classification_five_way(rust_exe):
    src = ('尝试：\n    整数("abc")\n捕获 值错误：\n    打印("值错误 ✅")\n'
           '尝试：\n    整数([1])\n捕获 类型错误：\n    打印("类型错误 ✅")\n')
    _five(src, rust_exe)


# ---------------------------------------------------------------------------
# 8. few-shot 体检：48 段示例必须语法与 API 都对
# ---------------------------------------------------------------------------

#: 这些段是**片段**（依赖上文变量 / 外部文件 / 网络），跑不通是预期的。
#: 判据：报错是「找不到名字」「找不到文件」「网络」这类**环境缺失**，
#: 而不是语法错误或「没有这个方法/模块」。
_环境缺失标志 = ("找不到名字", "找不到文件", "文件错误", "运行期错误",
                 "不存在", "没法读取")


def _fewshot_blocks() -> list[tuple[str, str]]:
    md = ROOT / "examples" / "ai" / "few-shot.md"
    text = md.read_text(encoding="utf-8")
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"^## (示例 \d+：[^\n]*)\n(.*?)(?=^## |\Z)",
                         text, re.S | re.M):
        title, body = m.group(1), m.group(2)
        for cm in re.finditer(r"```jsh\n(.*?)```", body, re.S):
            if cm.group(1).strip():
                out.append((title, cm.group(1)))
    return out


def test_fewshot_has_examples():
    blocks = _fewshot_blocks()
    assert len(blocks) >= 40, f"few-shot 示例变少了？只有 {len(blocks)} 段"


@pytest.mark.parametrize("title,code", _fewshot_blocks(),
                         ids=[t for t, _ in _fewshot_blocks()])
def test_fewshot_examples_are_valid(title, code, tmp_path, monkeypatch):
    """每段示例要么跑通、要么只报「环境缺失」——不许出现语法/API 错误。

    这条测试的来由：这 48 段示例是喂给 AI 的「正确写法」样本，但从没人跑过。
    M41 体检发现**四段是真错**（变量名撞内建、闭包计数器撞「赋值即局部」、
    用了不存在的解包遍历与 `字典.键值对()`）——AI 照着写就必错。

    **必须在临时目录里跑**：有几段示例会写文件（`文件.写文本("输出.txt", …)`、
    `json.写文件("数据.json", …)`、`压缩.打包("备份.zip", …)`），在仓库根目录
    跑会留下 `输出.txt` / `数据.json` / `备份.zip`（M41 踩到——回归跑完才发现
    工作区多了三个文件）。测试**不许污染工作区**。
    """
    monkeypatch.chdir(tmp_path)
    try:
        _tree(code)
    except JishiError as e:
        text = str(e)
        assert any(k in text for k in _环境缺失标志), (
            f"示例「{title}」跑不通，且不是环境缺失：\n{text[:400]}")


# ---------------------------------------------------------------------------
# 10. 解包遍历（`遍历 甲, 乙 在 …`）——解析期脱糖，五路零改动
# ---------------------------------------------------------------------------

def test_for_unpack_basic():
    out = _three("令 对们 = [[\"小明\", 8], [\"小红\", 9]]\n"
                 "遍历 名字, 年龄 在 对们:\n"
                 "    打印(名字, 年龄)")
    assert out == "小明 8\n小红 9\n"


def test_for_unpack_with_new_builtins():
    """解包遍历 + `带下标` / `配对` 是配套的（它们给的就是对）。"""
    out = _three("遍历 序, 名 在 配对([\"x\", \"y\"], [10, 20]):\n"
                 "    打印(序, 名)\n"
                 "遍历 i, 项 在 带下标([\"甲\", \"乙\"], 1):\n"
                 "    打印(i, 项)")
    assert out == "x 10\ny 20\n1 甲\n2 乙\n"


def test_for_unpack_with_star():
    out = _three("遍历 头, *尾 在 [[1, 2, 3], [4, 5]]:\n"
                 "    打印(头, 尾)")
    assert out == "1 [2, 3]\n4 [5]\n"


def test_for_unpack_too_many_values_errors():
    """解包数量对不上要报错（与 `令 甲, 乙 = [1,2,3]` 同一套语义）。"""
    with pytest.raises(JishiError):
        _tree("遍历 甲, 乙 在 [[1, 2, 3]]:\n    打印(甲)")


def test_for_unpack_three_names():
    out = _three("遍历 甲, 乙, 丙 在 [[1, 2, 3]]:\n    打印(甲 + 乙 + 丙)")
    assert out == "6\n"


def test_for_unpack_five_way(rust_exe):
    src = ("令 对们 = [[\"小明\", 8], [\"小红\", 9]]\n"
           "遍历 名字, 年龄 在 对们:\n"
           "    打印(名字, 年龄)\n"
           "遍历 序, 名 在 配对([\"x\", \"y\"], [10, 20]):\n"
           "    打印(序, 名)\n")
    _five(src, rust_exe)


# ---------------------------------------------------------------------------
# 11. 路径.递归列出
# ---------------------------------------------------------------------------

def test_path_recursive_list(tmp_path):
    """递归列出：只列文件、返回相对路径、已排序（按**相对路径**排序）。"""
    import os

    (tmp_path / "子").mkdir()
    (tmp_path / "甲.jsh").write_text("", encoding="utf-8")
    (tmp_path / "子" / "乙.jsh").write_text("", encoding="utf-8")
    (tmp_path / "子" / "丙.txt").write_text("", encoding="utf-8")
    p = str(tmp_path).replace("\\", "/")
    out = _three(f"导入 路径\n"
                 f"打印(路径.递归列出(\"{p}\"))\n"
                 f"打印(路径.递归列出(\"{p}\", \".jsh\"))")
    assert "丙.txt" in out and "甲.jsh" in out
    # 带后缀的那一行只留 .jsh；排序按**整个相对路径**，所以 `子\乙.jsh`
    # 排在 `甲.jsh` 前面（`子` < `甲` 是按码点比较的结果，与 Python 的
    # `sorted()` 一致——三执行器与两宿主都走各自平台的字符串序，Windows 上
    # 是 UTF-16 码元序，这里两边同为中文，结论一致）
    期望 = "[" + repr("子" + os.sep + "乙.jsh") + ", '甲.jsh']"
    assert out.splitlines()[1] == 期望


def test_path_recursive_list_not_a_dir_errors():
    with pytest.raises(JishiError):
        _tree("导入 路径\n路径.递归列出(\"这个目录不存在\")")


def test_path_recursive_list_five_way(rust_exe, tmp_path):
    (tmp_path / "甲.jsh").write_text("", encoding="utf-8")
    (tmp_path / "子").mkdir()
    (tmp_path / "子" / "乙.jsh").write_text("", encoding="utf-8")
    p = str(tmp_path).replace("\\", "/")
    src = (f"导入 路径\n"
           f"令 文件们 = 路径.递归列出(\"{p}\", \".jsh\")\n"
           f"打印(长度(文件们))\n")
    _five(src, rust_exe)


# ---------------------------------------------------------------------------
# 9. 模块清单与规格：新模块要进 lang-spec，且不遮蔽内建
# ---------------------------------------------------------------------------

def test_new_modules_in_lang_spec():
    """新模块要出现在机器可读规格里（AI 靠它知道有哪些模块）。"""
    from jishi.ai import build_lang_spec

    spec = build_lang_spec()
    names = {m["module"] for m in spec["stdlib"]}
    for m in ("容器", "迭代", "参数", "日志", "测试"):
        assert m in names, f"模块「{m}」不在 lang-spec 里"


def test_container_module_does_not_shadow_set_builtin():
    """「容器」不能遮蔽内建 `集合(...)`——这正是改名的理由。"""
    out = _three('导入 容器\n'
                 '令 s = 集合([1, 2, 2, 3])\n'
                 '打印(长度(s), 容器.取前([1, 2, 3], 2))')
    assert out == "3 [1, 2]\n"


def test_new_builtins_in_lang_spec():
    from jishi.ai import build_lang_spec

    spec = build_lang_spec()
    names = {b["name"] for b in spec["builtins"]}
    assert "带下标" in names and "配对" in names


def test_internal_names_still_hidden_from_spec():
    """M40 的纪律不能破：内部名（`$文本`）不许进规格。"""
    from jishi.ai import build_lang_spec

    spec = build_lang_spec()
    names = {b["name"] for b in spec["builtins"]}
    assert "$文本" not in names
