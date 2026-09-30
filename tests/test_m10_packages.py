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


# ---------------------------------------------------------------------------
# 依赖解析（M10.2 补齐：包.json 的「依赖」字段 + 版本约束 + 循环检测）
# ---------------------------------------------------------------------------

def _write_pkg(base, name, meta, modules):
    """在 base 下建包目录，写 包.json 与若干 .jsh 模块。"""
    import json
    pkg = base / name
    pkg.mkdir()
    (pkg / "包.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    for fn, src in modules.items():
        (pkg / fn).write_text(src, encoding="utf-8")
    return pkg


@pytest.fixture
def dep_pkgs(tmp_path, monkeypatch):
    """一个「甲依赖乙」的两层包结构。"""
    _write_pkg(tmp_path, "乙", {"名字": "乙", "版本": "1.0.0"},
               {"工具.jsh": "函数 翻倍(x):\n    返回 x * 2\n"})
    _write_pkg(tmp_path, "甲", {"名字": "甲", "版本": "2.0.0", "依赖": {"乙": ">=1.0.0"}},
               {"主.jsh": "函数 算(x):\n    返回 乙.翻倍(x) + 1\n"})
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    return tmp_path


def test_dependency_resolution(dep_pkgs):
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    with redirect_stdout(out):
        run_source("导入 甲 从 本地包\n打印(甲.算(5))\n")
    assert out.getvalue() == "11\n"


def test_dependency_version_mismatch(tmp_path, monkeypatch):
    _write_pkg(tmp_path, "乙", {"名字": "乙", "版本": "0.9.0"},
               {"工具.jsh": "令 x = 1\n"})
    _write_pkg(tmp_path, "甲", {"名字": "甲", "版本": "2.0.0", "依赖": {"乙": ">=1.0.0"}},
               {"主.jsh": "令 y = 乙.x\n"})
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    with pytest.raises(JishiError) as ei:
        run_source("导入 甲 从 本地包\n")
    assert "版本" in str(ei.value) and "0.9.0" in str(ei.value)


def test_dependency_cycle(tmp_path, monkeypatch):
    _write_pkg(tmp_path, "甲", {"名字": "甲", "依赖": {"乙": "*"}},
               {"a.jsh": "令 x = 1\n"})
    _write_pkg(tmp_path, "乙", {"名字": "乙", "依赖": {"甲": "*"}},
               {"b.jsh": "令 y = 2\n"})
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    with pytest.raises(JishiError) as ei:
        run_source("导入 甲 从 本地包\n")
    assert "循环依赖" in str(ei.value)


def test_dependency_missing(tmp_path, monkeypatch):
    _write_pkg(tmp_path, "甲", {"名字": "甲", "依赖": {"不存在的包": "*"}},
               {"a.jsh": "令 x = 1\n"})
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    with pytest.raises(JishiError) as ei:
        run_source("导入 甲 从 本地包\n")
    assert "没有找到本地包" in str(ei.value)


# ---------------------------------------------------------------------------
# 远端索引格式（M10.2 补齐：JSON 索引格式定义 + 版本约束解析）
# ---------------------------------------------------------------------------

def test_version_satisfies():
    from jishi.packages import version_satisfies
    assert version_satisfies("1.2.3", "*") is True
    assert version_satisfies("1.2.3", "") is True
    assert version_satisfies("1.2.3", "1.2.3") is True
    assert version_satisfies("1.2.3", "==1.2.3") is True
    assert version_satisfies("1.2.3", ">=1.0.0") is True
    assert version_satisfies("0.9.0", ">=1.0.0") is False
    assert version_satisfies("1.2.3", "<2.0.0") is True
    assert version_satisfies("1.2.3", "^1.0.0") is True
    assert version_satisfies("2.0.0", "^1.0.0") is False


def test_parse_index_ok():
    from jishi.packages import parse_index, resolve_package
    idx = parse_index("""{
        "格式": "jishi-package-index",
        "版本": 1,
        "包": {"某包": {"版本": "1.0.0", "下载地址": "https://例/某包.zip"}}
    }""")
    meta = resolve_package(idx, "某包")
    assert meta["版本"] == "1.0.0"


def test_parse_index_bad_format():
    from jishi.packages import parse_index
    with pytest.raises(ValueError) as ei:
        parse_index('{"格式": "别的", "版本": 1, "包": {}}')
    assert "不是基石包索引" in str(ei.value)


def test_parse_index_bad_version():
    from jishi.packages import parse_index
    with pytest.raises(ValueError) as ei:
        parse_index('{"格式": "jishi-package-index", "版本": 99, "包": {}}')
    assert "不兼容" in str(ei.value)


def test_parse_index_missing_packages():
    from jishi.packages import parse_index
    with pytest.raises(ValueError) as ei:
        parse_index('{"格式": "jishi-package-index", "版本": 1}')
    assert "包" in str(ei.value)
