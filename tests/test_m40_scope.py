# -*- coding: utf-8 -*-
"""M40：函数作用域语义「赋值即局部」——三个执行器必须完全一致。

背景（M39 对拍挖出来的明确分歧）：函数里给**外层同名**变量赋值时，
字节码执行器按 Python 的「赋值即局部」报「找不到这个名字」，树遍历却按
「先向外找、找到就用」静默算出另一个结果——一边报错一边给值。
M40 把树遍历补齐（进函数时给「本函数赋过值的名字」占上未绑定哨兵），
这里把口径钉死：**连报错文案都要逐字相同**。
"""

from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi import runtime as R                      # noqa: E402
from jishi.errors import JishiError                 # noqa: E402
from jishi.interpreter import Interpreter           # noqa: E402
from jishi.parser import parse                      # noqa: E402
from jishi.tokenizer import tokenize                # noqa: E402
from jishi.vm import run_source as vm_run_source    # noqa: E402


def _cvm_available() -> bool:
    try:
        from jishi import cvm_bind
        cvm_bind.load_lib()
        return True
    except Exception:                                # noqa: BLE001
        return False


HAS_CVM = _cvm_available()


def _run(engine: str, src: str) -> tuple[str, str]:
    """跑一段源码，返回 ``(标准输出, 报错全文)``。"""
    buf = io.StringIO()
    err = ""
    with redirect_stdout(buf):
        try:
            if engine == "树遍历":
                lines = src.replace("\r\n", "\n").split("\n")
                Interpreter(filename="<用例>").run(
                    parse(tokenize(src, "<用例>"), lines, "<用例>"))
            elif engine == "字节码":
                vm_run_source(src, filename="<用例>")
            else:
                from jishi import cvm_bind
                cvm_bind.run_source_c(src, filename="<用例>")
        except JishiError as e:
            err = str(e)
    return buf.getvalue(), err


def _engines() -> list[str]:
    out = ["树遍历", "字节码"]
    if HAS_CVM:
        out.append("C虚拟机")
    return out


def _compare(src: str) -> None:
    results = [(name, _run(name, src)) for name in _engines()]
    first_name, first = results[0]
    for other_name, other in results[1:]:
        assert first == other, (
            f"执行器结果不一致\n  {first_name}：{first!r}\n  {other_name}：{other!r}")


# ---------------------------------------------------------------------------
# 一致：结果与报错都要一样
# ---------------------------------------------------------------------------

def test_inner_assignment_shadows_outer_name():
    """内层给外层同名变量赋值 → 它是**内层**的局部变量，赋值前读会报错。"""
    _compare("函数 计数器(起始)：\n"
             "    令 当前 = 起始\n"
             "    函数 加(步长)：\n"
             "        当前 = 当前 + 步长\n"
             "        返回 当前\n"
             "    返回 加\n"
             "\n"
             "令 加 = 计数器(10)\n"
             "打印(加(5))\n")


def test_read_local_before_assignment_errors_with_shadow_hint():
    """外层有同名变量时，提示要说清「它成了这个函数的局部变量」。"""
    out, err = _run("树遍历", "令 x = 99\n"
                              "函数 f()：\n"
                              "    打印(x)\n"
                              "    令 x = 1\n"
                              "f()\n")
    assert out == ""
    assert "名字「x」在赋值之前被用到了" in err
    assert "外层也有一个叫「x」的变量" in err
    _compare("令 x = 99\n"
             "函数 f()：\n"
             "    打印(x)\n"
             "    令 x = 1\n"
             "f()\n")


def test_closure_read_of_outer_variable_still_works():
    """**只读**外层变量（不赋值）不受影响：闭包照常捕获。"""
    _compare("函数 外()：\n"
             "    令 x = 1\n"
             "    函数 内()：\n"
             "        返回 x + 1\n"
             "    返回 内\n"
             "打印(外()())\n")


def test_local_used_after_assignment_is_fine():
    """先赋值后使用：正常。"""
    _compare("函数 f()：\n"
             "    令 合计 = 0\n"
             "    遍历 i 在 [1, 2, 3]：\n"
             "        合计 = 合计 + i\n"
             "    返回 合计\n"
             "打印(f())\n")


def test_parameter_counts_as_local():
    """形参也是局部变量：与外层同名时同样遮蔽。"""
    _compare("令 x = 99\n"
             "函数 f(x)：\n"
             "    返回 x\n"
             "打印(f(1))\n")


def test_import_alias_is_local():
    """`导入 数学 为 别名` 绑定的是局部名（模块作用域则进全局）。"""
    _compare("函数 f()：\n"
             "    导入 数学 为 m\n"
             "    返回 m.开方(16)\n"
             "打印(f())\n")


def test_loop_target_is_local_to_function():
    """`遍历` 的循环变量也在函数作用域里。"""
    _compare("令 i = 99\n"
             "函数 f()：\n"
             "    遍历 i 在 [1, 2]：\n"
             "        打印(i)\n"
             "    返回 i\n"
             "打印(f())\n")


def test_nested_function_shadowing_two_levels():
    """两层嵌套、层层同名：每一层各是各的局部变量。"""
    _compare("函数 甲()：\n"
             "    令 v = 1\n"
             "    函数 乙()：\n"
             "        令 v = 2\n"
             "        函数 丙()：\n"
             "            令 v = 3\n"
             "            返回 v\n"
             "        返回 丙() + v\n"
             "    返回 乙() + v\n"
             "打印(甲())\n")


def test_tuple_unpacking_binds_locals():
    """元组解包赋的值也是局部绑定。"""
    _compare("令 甲 = 9\n"
             "令 乙 = 9\n"
             "函数 f()：\n"
             "    令 甲, 乙 = 1, 2\n"
             "    返回 甲 + 乙\n"
             "打印(f())\n")


def test_augmented_assignment_counts_as_binding():
    """`+=` 也算「赋过值」→ 是局部变量（与 Python 一致）。"""
    _compare("令 计数 = 100\n"
             "函数 f()：\n"
             "    计数 += 1\n"
             "    返回 计数\n"
             "打印(f())\n")


# ---------------------------------------------------------------------------
# 报错文案是单一来源
# ---------------------------------------------------------------------------

def test_unbound_message_comes_from_one_place():
    """三个执行器共用 `runtime.unbound_local_error`——文案不许各写各的。"""
    e = R.unbound_local_error("甲", line=3, col=5, filename="x.jsh")
    assert "名字「甲」在赋值之前被用到了" in str(e)
    assert "「甲」在这个函数里被赋过值" in str(e)
    # 给了 hint 就用给的（C VM 那条路带编译器存的槽位提示）
    e2 = R.unbound_local_error("甲", line=1, col=1, hint="外层也有一个叫「甲」的变量")
    assert "外层也有一个叫「甲」的变量" in str(e2)


def test_shadow_hint_text_is_shared_with_compiler():
    """编译器写进 `local_hints` 的文本与树遍历用的是同一份。"""
    src = ("令 x = 1\n"
           "函数 f()：\n"
           "    打印(x)\n"
           "    令 x = 2\n")
    lines = src.split("\n")
    from jishi.compiler import compile_program
    cmod = compile_program(parse(tokenize(src, "x.jsh"), lines, "x.jsh"),
                           "x.jsh", lines)
    hints = [h for code in cmod.codes for h in code.local_hints.values()]
    assert hints, "编译器应当给「外层同名」的槽位留下提示"
    assert all(h == R.UNBOUND_SHADOW_HINT.format(name="x") for h in hints)


# ---------------------------------------------------------------------------
# 内部保留名不外露（f-string 的脱糖目标）
# ---------------------------------------------------------------------------

def test_fstring_survives_text_module_import():
    """`导入 文本` 不该打断 f-string（M40 修的真缺陷）。

    插值原先脱糖成内建 `文本(值)`，而 `文本` 正好是本项目最常用的标准库名，
    一句 `导入 文本` 就把它遮蔽 → **所有 f-string 报「模块不能直接调用」**。
    现在脱糖到用户写不出的内部名 `$文本`。
    """
    _compare("导入 文本\n"
             "令 名字 = \"世界\"\n"
             "打印(`你好 {名字}！{1 + 2}`)\n")


def test_fstring_works_in_every_host():
    """插值在纯标准库场景下也正常（回归钉子）。"""
    _compare("令 单价 = 3\n令 数量 = 4\n打印(`合计 {单价 * 数量} 元`)\n")


def test_internal_builtins_are_hidden_from_language_spec():
    """内部名（`$文本` / `检查实参`）不该出现在语言规格里。"""
    from jishi.ai import build_lang_spec
    names = {b["name"] for b in build_lang_spec()["builtins"]}
    assert "$文本" not in names
    assert "检查实参" not in names
    assert "文本" in names                  # 用户可见的那个照旧在


def test_internal_builtins_are_hidden_from_completion():
    """补全同样不该推荐内部名。"""
    from jishi.langdata import LangData
    d = LangData()
    assert "$文本" not in d.builtins
    assert "检查实参" not in d.builtins


def test_dollar_names_are_not_writable_by_users():
    """`$` 是用户写不出来的字符——这正是它适合当内部名的原因。"""
    from jishi.errors import JishiError as _E
    with pytest.raises(_E):
        tokenize("令 $甲 = 1", "<用例>")


def test_is_internal_builtin_rule():
    """判据两条：`$` 开头，或说明以「内部用」开头。"""
    from jishi.runtime import _Builtin, is_internal_builtin
    assert is_internal_builtin("$文本")
    assert is_internal_builtin("检查实参", _Builtin("检查实参", lambda: None,
                                                 "内部用：按类型标注校验"))
    assert not is_internal_builtin("文本", _Builtin("文本", lambda v: v,
                                                 "把值转成文本"))


def test_internal_name_does_not_trigger_undefined_warning():
    """内部名 `$文本` 不该被 LSP 当成「没定义过的名字」（M40 全量回归抓到）。

    它**不在**语言规格里（有意过滤，不推荐给用户），但插值展开后会真的出现在
    AST 里——不跳过就会满屏误报。这条是那次回归失败的钉子。
    """
    from jishi.lsp import LspServer

    srv = LspServer()
    src = "令 甲 = 1\n打印(`值 {甲}`)\n"
    uri = "file:///tmp/插值.jsh"
    srv.docs[uri] = {"text": src, "version": 1}
    srv._analyze(uri)
    warns = [d for d in srv.diagnostics.get(uri, []) if d["severity"] == 2]
    assert warns == [], [w["message"] for w in warns]


def test_new_projects_are_formatter_clean():
    """新增的真实项目必须本来就合规（`jishi 格式化 --check` 全绿）。

    这条与 `test_m38_formatter_edges.py::test_corpus_dir_is_already_formatted`
    是同一件事，但这里**只挑 M40 新增的两个项目**——语料目录那条一旦失败很难
    一眼看出是哪个新文件带进来的（M40 就踩到过：手改文件后忘了重跑格式化器）。
    """
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("待办清单", "排序跑分"):
        f = root / "examples" / "projects" / name
        r = subprocess.run(
            [sys.executable, "-m", "jishi.cli", "格式化", str(f), "--check"],
            capture_output=True, cwd=str(root))
        assert r.returncode == 0, (
            f"{name} 有文件不合格式化风格：\n"
            + r.stderr.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# CLI 端到端
# ---------------------------------------------------------------------------

def test_cli_reports_shadowing_error(tmp_path):
    """命令行跑一个「赋值前读局部」的脚本：中文报错 + 退出码 1。"""
    f = tmp_path / "遮蔽.jsh"
    f.write_text("令 x = 1\n函数 f()：\n    打印(x)\n    令 x = 2\nf()\n",
                 encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "jishi.cli", str(f)],
                       capture_output=True, cwd=str(ROOT))
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 1
    assert "名字「x」在赋值之前被用到了" in err
    assert "Traceback" not in err


def test_cli_fstring_with_text_import(tmp_path):
    """命令行端到端：`导入 文本` + f-string 要能跑出结果。"""
    f = tmp_path / "插值.jsh"
    f.write_text("导入 文本\n令 甲 = 6\n打印(`结果 {甲 * 7}`)\n",
                 encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "jishi.cli", str(f)],
                       capture_output=True, cwd=str(ROOT))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-300:]
    assert "结果 42" in out
