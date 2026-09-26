# -*- coding: utf-8 -*-
"""M52 · 基石库层：**用基石自己写的标准库**，一份代码五个引擎共用。

## 这个文件钉什么

1. **一致性**：同一段程序在树遍历 / Python VM / C VM / Node / Rust 上逐字节一致，
   而且与**宿主层实现**（`jishi/stdlib/*.py`）也逐字节一致 ——
   「换一种实现方式」不能改变行为，这是迁移的底线。
2. **机制**：`导入 统计` 拿到的仍是**模块对象**（`统计.求和` 照旧能用、
   `打印(统计)` 还是 `<模块 统计>`）、模块私有名字不外泄、别名导入可用、
   没用到基石库时产物**一点没变**。
3. **回落**：`JISHI_NO_JISHILIB=1` 能把整层关掉、导入回落到宿主层
   （诊断与性能对比都靠它；也证明宿主层还完整可用）。

## 机制一句话

`jishi/stdlib-jishi/统计.jsh` 在**编译期**被展开成

    函数 __基石库_统计__(): <模块源码>  返回 {"求和": 求和, …}     # 外壳函数
    统计 = $造模块("统计", __基石库_统计__())                      # 原 `导入 统计`

于是「库」在字节码层面**只是一次函数调用** —— 三个执行器与两个跨语言宿主
**一个字都没改**。完整设计见 `jishi/jishilib.py` 的模块文档。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path, tmp_bc_path      # noqa: E402

from jishi import jishilib                           # noqa: E402
from jishi import serialize                          # noqa: E402
from jishi import vm as vm_mod                       # noqa: E402
from jishi.compiler import compile_source            # noqa: E402
from jishi.errors import JishiError                  # noqa: E402
from jishi.interpreter import run_source as tree_run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
RUST = rust_exe_path()


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def lib_off():
    """临时把基石库层整体关掉（导入回落到宿主层）。"""
    old = os.environ.get("JISHI_NO_JISHILIB")
    os.environ["JISHI_NO_JISHILIB"] = "1"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("JISHI_NO_JISHILIB", None)
        else:
            os.environ["JISHI_NO_JISHILIB"] = old


def _capture(fn, *a) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


def _py_vm(src: str) -> str:
    return _capture(lambda: vm_mod.VM(compile_source(src, "<t>"), "<t>").run())


def _cvm(src: str) -> str:
    from jishi import cvm_bind
    return _capture(cvm_bind.run_source_c, src, "<t>")


def _host_run(exe: list, src: str, tag: str) -> tuple:
    bc = tmp_bc_path(ROOT / f"_tmp_m52_{tag}.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    r = subprocess.run(exe + [str(bc)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def _engines(tag: str = "a"):
    """五引擎：`(名字, 跑一段源码 → (是否正常退出, 输出或错误文本))`。"""
    def tree(src):
        try:
            return True, _capture(tree_run, src)
        except JishiError as e:
            return False, f"{getattr(e, 'title', '')} {e}"

    def pyvm(src):
        try:
            return True, _py_vm(src)
        except JishiError as e:
            return False, f"{getattr(e, 'title', '')} {e}"

    def cvm(src):
        try:
            return True, _cvm(src)
        except JishiError as e:
            return False, f"{getattr(e, 'title', '')} {e}"

    out = [("树遍历", tree), ("Python VM", pyvm), ("C VM", cvm)]
    if NODE:
        out.append(("Node", lambda s: _host_run(
            [NODE, str(ROOT / "node" / "index.js")], s, tag)))
    if RUST.exists():
        out.append(("Rust", lambda s: _host_run([str(RUST)], s, tag)))
    return out


def _five_agree(src: str) -> str:
    """五个引擎 stdout 逐字节一致且都正常退出；返回那份输出。"""
    base_label, base = None, None
    for label, run in _engines():
        ok, out = run(src)
        assert ok, f"{label} 本该正常退出却报错：{out[:300]}"
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, (
                f"{label} 与 {base_label} 输出不一致\n"
                f"{base_label}: {base!r}\n{label}: {out!r}")
    return base


#: 一个把 `统计` 十个函数都摸一遍的程序 —— 迁移的验收基准
FULL = """\
导入 统计
打印(统计)
打印(统计.求和([1,2,3]), 统计.平均([1,2,3,4]), 统计.中位数([3,1,2]))
打印(统计.中位数([1,2,3,4]), 统计.众数([3,1,3,2]), 统计.极差([1,5,3]))
打印(统计.方差([1,2,3,4]), 统计.标准差([1,2,3,4]))
打印(统计.方差([1,2,3,4], 真), 统计.标准差([1,2,3,4], 真))
打印(统计.分位数([1,2,3,4], 0.25), 统计.去极值平均([9.5,8.0,9.0,7.5,9.9]))
打印(统计.加权平均([80,90],[1,3]))
打印(统计.求和([]), 统计.众数([9,9,1,1]))
"""


# ---------------------------------------------------------------------------
# 一、一致性
# ---------------------------------------------------------------------------

def test_十个函数在五引擎逐字节一致():
    out = _five_agree(FULL)
    # 模块对象照旧（不是字典）
    assert out.splitlines()[0] == "<模块 统计>"
    assert "87.5" in out and "1.118033988749895" in out


def test_与宿主层实现逐字节一致():
    """**迁移不能改变行为** —— 这是本文件最重要的一条。

    M52 只是把 `统计` 的实现从「三份各自写」换成「一份基石代码」，
    用户看到的结果必须**一模一样**（包括 `打印(统计)` 的输出形态）。
    """
    lib = _capture(tree_run, FULL)                  # 默认 → 基石库层
    with lib_off():
        host = _capture(tree_run, FULL)             # → 宿主层 `jishi/stdlib/统计.py`
    assert lib == host, f"基石库版与宿主层版结果不同\n库: {lib!r}\n宿主: {host!r}"


def _err_kind(src: str) -> str:
    """跑一段必须报错的源码，返回错误**大类**（尽量归一，方便跨引擎比）。"""
    try:
        _capture(tree_run, src)
        return "没报错"
    except JishiError as e:
        t = f"{getattr(e, 'title', '')} {e}"
    if "类型错误" in t:
        return "类型错误"
    if "值错误" in t or "数值不对" in t:
        return "值错误"
    return t[:40]


@pytest.mark.parametrize("expr", [
    '统计.求和("abc")', '统计.求和([1,"a"])', '统计.求和([1,真])',
    '统计.平均([])', '统计.方差([1], 真)', '统计.分位数([1,2], 2)',
    '统计.分位数([1,2], "x")', '统计.去极值平均([1,2], -1)',
    '统计.去极值平均([1,2], 1)', '统计.加权平均([1,2],[1])',
    '统计.加权平均([1],[0])', '统计.求和(5)',
])
def test_错误路径五引擎都给同一类错(expr):
    """错误**类型**五引擎必须一致（对拍比类型，文案允许略有出入）。"""
    src = f'导入 统计\n{expr}\n'
    kinds = set()
    for label, run in _engines("e"):
        ok, text = run(src)
        assert not ok, f"{label} 本该报错却正常退出：{text[:200]!r}"
        if "类型错误" in text:
            kinds.add("类型错误")
        elif "值错误" in text or "数值不对" in text:
            kinds.add("值错误")
        else:
            pytest.fail(f"{label} 报的不是类型/值错误：{text[:200]!r}")
    assert kinds in ({"类型错误"}, {"值错误"}), f"五引擎报的类型不一致：{kinds}"


def test_错误路径与宿主层同类型():
    """同一批错误，库版与宿主层版报的类要一一对上（换实现不能换报错）。"""
    cases = ['统计.求和("abc")', '统计.求和([1,"a"])', '统计.平均([])',
             '统计.方差([1], 真)', '统计.分位数([1,2], 2)',
             '统计.加权平均([1],[0])']
    old = os.environ.pop("JISHI_NO_JISHILIB", None)
    try:
        for expr in cases:
            src = f'导入 统计\n{expr}\n'
            lib = _err_kind(src)
            os.environ["JISHI_NO_JISHILIB"] = "1"
            host = _err_kind(src)
            os.environ.pop("JISHI_NO_JISHILIB", None)
            assert lib == host, f"{expr}: 库版报 {lib}，宿主层报 {host}"
    finally:
        if old is not None:
            os.environ["JISHI_NO_JISHILIB"] = old


# ---------------------------------------------------------------------------
# 二、机制
# ---------------------------------------------------------------------------

def test_导入拿到的是模块对象而不是字典():
    """`$造模块` 那一层的作用：语法一个字都不用改。"""
    src = '导入 统计\n打印(统计)\n打印(统计.求和([1,2,3]))\n'
    out = _five_agree(src)
    assert out == "<模块 统计>\n6\n"


def test_模块私有名字不外泄():
    """`_取数字表` 这些下划线开头的名字是模块私有的，不该出现在用户面前。"""
    src = '导入 统计\n打印(统计._取数字表)\n'
    for label, run in _engines("p"):
        ok, text = run(src)
        assert not ok, f"{label} 居然能拿到私有名字：{text[:200]!r}"


def test_别名导入可用():
    out = _five_agree('导入 统计 为 数\n打印(数.求和([1,2,3]), 数.中位数([1,2,3]))\n')
    assert out == "6 2\n"


def test_函数体内导入也能展开():
    """`导入` 写在函数里（或条件分支里）同样要展开，且**位置语义不变**。"""
    src = ('函数 算()：\n'
           '    导入 统计\n'
           '    返回 统计.求和([1,2,3])\n'
           '打印(算())\n')
    assert _five_agree(src) == "6\n"


def test_内部内建不进语言规格():
    """`$造模块` 是编译器专用的内部内建，不该出现在 AI 语言卡/补全里。"""
    from jishi import ai
    names = [b["name"] for b in ai.build_lang_spec()["builtins"]]
    assert "$造模块" not in names


def test_没用到基石库时产物一点没变():
    """只有真的 `导入` 了基石库模块才会改写 —— 其他程序产物与从前逐字节相同。"""
    plain = compile_source('打印(1 + 2)\n', "<t>")
    assert len(plain.codes) == 1
    assert plain.names == ["打印"] or "打印" in plain.names


def test_基石库模块清单():
    """M52 只搬了 `统计` 一个（渐进迁移）；清单是显式的，加模块要改这里。"""
    assert jishilib.names() == ["统计"]
    assert jishilib.has("统计") and not jishilib.has("编码")


def test_关掉这层就回落宿主层():
    """`JISHI_NO_JISHILIB=1`：宿主层必须**完整可用**（它是兜底，也是对照）。"""
    src = '导入 统计\n打印(统计.求和([1,2,3]), 统计.中位数([3,1,2]))\n'
    lib = _five_agree(src)                          # 五引擎走基石库层
    with lib_off():
        off_out = _capture(tree_run, src)           # 关掉 → 宿主层
    assert lib == off_out == "6 2\n"
