# -*- coding: utf-8 -*-
"""M10.2 包管理雏形：本地包导入（导入 名字 从 本地包）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import runtime
from jishi.errors import JishiError
from jishi.interpreter import run_source


@pytest.fixture
def local_pkg(tmp_path, monkeypatch):
    """在临时目录创建一个本地包，并把搜索目录指向它。"""
    pkg = tmp_path / "数学工具"
    pkg.mkdir()
    (pkg / "包.json").write_text('{"名字": "数学工具", "版本": "1.0.0"}',
                                 encoding="utf-8")
    (pkg / "计算.jsh").write_text(
        "函数 平方(x):\n    返回 x * x\n\n令 版本号 = \"1.0.0\"\n",
        encoding="utf-8")
    monkeypatch.setattr(runtime, "_package_dirs",
                        lambda: [str(tmp_path)])
    return tmp_path


def test_local_package_import_function(local_pkg):
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    with redirect_stdout(out):
        run_source("导入 数学工具 从 本地包\n打印(数学工具.平方(8))\n")
    assert out.getvalue() == "64\n"


def test_local_package_import_variable(local_pkg):
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    with redirect_stdout(out):
        run_source("导入 数学工具 从 本地包\n打印(数学工具.版本号)\n")
    assert out.getvalue() == "1.0.0\n"


def test_local_package_alias(local_pkg):
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    with redirect_stdout(out):
        run_source("导入 数学工具 从 本地包 为 工具\n打印(工具.平方(5))\n")
    assert out.getvalue() == "25\n"


def test_local_package_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    with pytest.raises(JishiError) as ei:
        run_source("导入 不存在的包 从 本地包\n")
    assert "没有找到本地包" in str(ei.value)


def test_local_package_no_jsh(tmp_path, monkeypatch):
    pkg = tmp_path / "空包"
    pkg.mkdir()
    (pkg / "包.json").write_text('{"名字": "空包"}', encoding="utf-8")
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    with pytest.raises(JishiError) as ei:
        run_source("导入 空包 从 本地包\n")
    assert "没有 .jsh" in str(ei.value)


def test_local_package_name_mismatch(tmp_path, monkeypatch):
    pkg = tmp_path / "甲"
    pkg.mkdir()
    (pkg / "包.json").write_text('{"名字": "乙"}', encoding="utf-8")
    (pkg / "a.jsh").write_text("令 x = 1\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    with pytest.raises(JishiError) as ei:
        run_source("导入 甲 从 本地包\n")
    assert "不一致" in str(ei.value)
