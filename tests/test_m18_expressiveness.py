# -*- coding: utf-8 -*-
"""M18 表达便利：切片 / argv / 最小最大多参数 / 导入遮蔽治理。"""

import io
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

sys.path.insert(0, ".")

from jishi import runtime
from jishi.cli import main
from jishi.interpreter import run_source


def _run(src, filename="<输入>"):
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src, filename)
    return out.getvalue()


# ---------------------------------------------------------------------------
# 18.1 切片
# ---------------------------------------------------------------------------

def test_slice_basic():
    assert _run("打印([1,2,3,4,5][1:3])") == "[2, 3]\n"


def test_slice_reverse():
    assert _run("打印([1,2,3,4,5][::-1])") == "[5, 4, 3, 2, 1]\n"


def test_slice_omitted():
    assert _run("打印([1,2,3,4,5][:2])") == "[1, 2]\n"
    assert _run("打印([1,2,3,4,5][2:])") == "[3, 4, 5]\n"
    assert _run("打印([1,2,3,4,5][:])") == "[1, 2, 3, 4, 5]\n"


def test_slice_step():
    assert _run("打印([1,2,3,4,5][::2])") == "[1, 3, 5]\n"


def test_slice_negative_step():
    assert _run("打印([1,2,3,4,5][4:1:-1])") == "[5, 4, 3]\n"


def test_slice_string():
    assert _run('打印("你好世界"[1:3])') == "好世\n"


def test_slice_variable_index():
    assert _run("令 a=[10,20,30,40]\n令 i=1\n令 j=3\n打印(a[i:j])") == "[20, 30]\n"


# ---------------------------------------------------------------------------
# 18.2 argv
# ---------------------------------------------------------------------------

def test_argv_passed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.jsh").write_text(
        "导入 系统\n打印(系统.参数())\n", encoding="utf-8")
    code, out, _ = _run_cli(["a.jsh", "成绩.csv", "--排序", "分数"])
    assert code == 0
    assert "['成绩.csv', '--排序', '分数']" in out


def test_argv_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.jsh").write_text(
        "导入 系统\n打印(系统.参数())\n", encoding="utf-8")
    code, out, _ = _run_cli(["a.jsh"])
    assert code == 0
    assert "[]" in out


def test_argv_double_dash(tmp_path, monkeypatch):
    """`--` 之后全算脚本参数（与 Python 约定一致）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.jsh").write_text(
        "导入 系统\n打印(系统.参数())\n", encoding="utf-8")
    code, out, _ = _run_cli(["a.jsh", "--", "--timeout", "--sandbox"])
    assert code == 0
    assert "['--timeout', '--sandbox']" in out


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# 18.3 最小/最大 多参数
# ---------------------------------------------------------------------------

def test_min_multi_args():
    assert _run("打印(最小(5, 3))") == "3\n"
    assert _run("打印(最小(5, 3, 9))") == "3\n"


def test_min_list_compat():
    """单列表调用保持兼容。"""
    assert _run("打印(最小([5, 3]))") == "3\n"


def test_max_multi_args():
    assert _run("打印(最大(5, 3))") == "5\n"
    assert _run("打印(最大(5, 3, 9))") == "9\n"


def test_max_list_compat():
    assert _run("打印(最大([5, 3]))") == "5\n"


# ---------------------------------------------------------------------------
# 18.4 导入遮蔽治理
# ---------------------------------------------------------------------------

def test_import_text_warns(tmp_path, monkeypatch):
    runtime._warned_shadow.clear()   # 隔离跨测试的模块级状态
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.jsh").write_text("导入 文本\n", encoding="utf-8")
    code, _, err = _run_cli(["a.jsh"])
    assert code == 0
    assert "遮蔽" in err          # 有中文提示
    assert "别名" in err          # 建议用别名导入


def test_import_text_warns_once():
    """同一进程内重复导入只提示一次（不刷屏）。"""
    runtime._warned_shadow.clear()
    err = io.StringIO()
    with redirect_stderr(err):
        _run("导入 文本\n")
        _run("导入 文本\n")
    # 两次导入只打印一次提示
    assert err.getvalue().count("遮蔽") == 1


def test_import_alias_no_warning(tmp_path, monkeypatch):
    """用别名导入不应提示遮蔽。"""
    runtime._warned_shadow.clear()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.jsh").write_text(
        '导入 文本 为 t\n打印(t.计数("香蕉苹果", "香"))\n',
        encoding="utf-8")
    code, out, err = _run_cli(["a.jsh"])
    assert code == 0
    assert "遮蔽" not in err
    assert "1" in out
