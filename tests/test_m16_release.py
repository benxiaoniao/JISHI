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


# ---------------------------------------------------------------------------
# 打包契约：`packages` 里列的目录必须真的存在于「干净检出」
# ---------------------------------------------------------------------------
#
# 2026-09-21 起 CI 连续 6 次全红、四平台都倒在「安装项目（可编辑）」，报
#     error: package directory 'jishi/_native' does not exist
# 根因：pyproject 的 `packages` 列了 `jishi._native`，setuptools 在**生成构建
# 元数据**阶段就会检查该目录是否存在；而这个目录里只有 `.dll/.so/.dylib`，
# 那些扩展名被 .gitignore 排除、不入库 → 干净检出里目录整个消失 → 构建失败。
#
# **本地永远复现不出来**：开发机里 dll 早就构建好了，目录一直在。
# 必须用 `git archive` 导出一棵干净树才看得见。这条测试就干这件事。

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    """读 pyproject.toml（用 tomllib；3.11+ 标准库，3.10 用 tomli 兜底）。"""
    try:
        import tomllib as _t
    except ModuleNotFoundError:                                    # pragma: no cover
        import tomli as _t  # type: ignore
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return _t.loads(text)


def test_packages_directories_exist_in_clean_checkout():
    """`packages` 里每个目录都必须在 git 里存在（哪怕只有一个占位文件）。

    判据不是「本地磁盘上有这个目录」（开发机上当然有），而是
    **「git 跟踪的文件能让这个目录被检出来」**——这正是 CI 的处境。
    """
    import subprocess
    cfg = _pyproject()
    pkgs = cfg["tool"]["setuptools"]["packages"]

    # git 里所有被跟踪的路径（干净检出后能拿到的东西就是这些）
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8")
    assert out.returncode == 0, "需要 git 环境来判定「干净检出」"
    tracked = out.stdout.splitlines()

    for dotted in pkgs:
        rel = dotted.replace(".", "/")
        prefix = rel + "/"
        assert any(p.startswith(prefix) for p in tracked), (
            f"packages 里的 {dotted!r}（目录 {rel}/）在 git 里没有任何被跟踪的文件——"
            f"干净检出时该目录不存在，`pip install -e .` 会直接失败"
            f"（error: package directory '{rel}' does not exist）。"
            f"修法：在该目录放一个 .gitkeep 并入库。"
        )


def test_native_dir_is_tracked_but_libs_are_not():
    """`jishi/_native`：目录要入库（占位），动态库本身不入库（各平台产物）。

    这两条**必须同时成立**——只满足一边都会出问题：
    - 目录不入库 → CI 构建失败（见上一条测试）；
    - 动态库入库 → 仓库里塞进二进制、且跨平台产物互相覆盖。
    """
    import subprocess
    out = subprocess.run(["git", "ls-files", "jishi/_native"],
                         cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8")
    files = out.stdout.splitlines()
    assert files, "jishi/_native 在 git 里是空的——干净检出会缺目录"
    for f in files:
        assert not f.endswith((".dll", ".so", ".dylib")), (
            f"动态库不该入库：{f}"
        )
