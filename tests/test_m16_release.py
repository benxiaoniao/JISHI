# -*- coding: utf-8 -*-
"""M16 发行版：版本号单一来源 + 工具子命令（医生/新项目/教程）。"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import cli
from jishi import version as _ver


def test_version_single_source():
    """版本号单一来源：version.py / ai / mcp_server / cli 一致。"""
    from jishi import ai, mcp_server
    assert _ver.VERSION == ai.VERSION == mcp_server.VERSION == cli.VERSION
    # 版本号应是非空的语义化版本串（不写死具体数字，避免每次发版改测试）
    assert _ver.VERSION.count(".") == 2


def test_version_fallback():
    """打包后无包元数据时回退到内置常量。"""
    # 兜底常量与当前版本都应是语义化版本串（不写死具体数字，避免每次发版改测试）
    assert _ver.__FALLBACK__.count(".") == 2
    assert _ver.VERSION.count(".") == 2


def test_version_matches_pyproject():
    """`pyproject.toml` 是版本号的唯一来源——运行时读到的必须与它一致。

    不一致的典型原因是「改了 pyproject，但环境里的元数据还是旧的」。这种不一致
    很隐蔽：文档写 0.1.25、`jishi --版本` 说 0.1.17，两边各自看都像是对的
    （M43 真实踩到：源码目录里躺着一个陈旧的 `jishi.egg-info`，它挡在
    `sys.path` 前面，`importlib.metadata` 先扫到它就返回了旧版本）。

    修法：`python -m pip install -e . --no-deps --no-build-isolation`，
    并确认源码目录里没有过期的 `*.egg-info`（那是老式 editable 安装的遗留物）。
    """
    import re

    root = Path(__file__).resolve().parents[1]
    src = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', src, re.M)
    assert m, "pyproject.toml 里找不到 version"
    期望 = m.group(1)
    assert _ver.VERSION == 期望, (
        f"pyproject 写的是 {期望}，运行时读到 {_ver.VERSION}"
        "——多半是改了版本号没重装：pip install -e . --no-deps")
    assert _ver.__FALLBACK__ == 期望, (
        f"兜底常量是 {_ver.__FALLBACK__}，与 pyproject 的 {期望} 不一致"
        "（打包成单文件后没有元数据，用的就是它）")


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            code = cli.main(argv)
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


def test_cmd_doctor():
    code, out = _run_cli(["医生"])
    assert code == 0
    assert f"版本：{_ver.VERSION}" in out
    assert "C VM" in out


def test_cmd_tutorial():
    code, out = _run_cli(["教程"])
    assert code == 0
    assert "10 章" in out or "第10章" in out
    assert "包管理" in out


def test_cmd_new_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out = _run_cli(["新项目", "我的项目"])
    assert code == 0
    assert (tmp_path / "我的项目" / "hello.jsh").is_file()
    assert (tmp_path / "我的项目" / "说明.md").is_file()


def test_cmd_new_project_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "已存在").mkdir()
    code, _ = _run_cli(["新项目", "已存在"])
    assert code == 1


def test_run_file_still_works(tmp_path, monkeypatch):
    """加子命令后，jishi 文件.jsh 运行路径不受影响。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.jsh").write_text('打印("你好")\n', encoding="utf-8")
    code, out = _run_cli(["a.jsh"])
    assert code == 0
    assert out == "你好\n"
