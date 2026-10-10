# -*- coding: utf-8 -*-
r"""R8.3：发行包改成 **cargo 单二进制**。

## 这一件在做什么

R8 的验收之一是「三平台包可用 + 体积 ≤5MB / 启动 ≤100ms」。以前发行包由
**PyInstaller** 打（跑的是 Python 宿主），体积约 20MB；R8.3 改成
`cargo build --release` 的产物 —— **一个文件**。

## 🔴 顺带修掉的一个真 bug（本文件的主要判据）

以前 PyInstaller 是 **onedir**，产物落在 `bin/oracle/jishi/jishi.exe`（**多套了一层目录**），
而 `tools/install.sh` / `tools/install.ps1` 的自检写的是**平铺**的
`"$BIN_DIR/jishi" --version` / `& "$binDir\jishi.exe"`：

| | 包里的实际路径 | 安装脚本期望的路径 |
|---|---|---|
| 0.2.1（PyInstaller） | `bin/oracle/jishi/jishi.exe` | `bin/jishi.exe` ← **对不上** |

后果：**Linux / macOS 用 tar.gz + install.sh 装完，自检必失败**，而且 PATH 里加的是
`~/.jishi/bin`，那下面是个**目录** `oracle/jishi/`，所以 `jishi` 命令**根本不在 PATH 上**。
Windows 因为推荐走 Inno Setup（`.iss` 里写的是嵌套路径）才没暴露。

换成单二进制之后 `bin/jishi`（或 `bin/jishi.exe`）**本身就是文件**，安装脚本
**一个字不用改就对了**。本文件的判据就是把这条「布局契约」钉住。

## 判据分两层

* **结构层**（永远跑，不需要 cargo）：默认引擎是 rust；安装脚本假设的是平铺 `bin/`；
  发行脚本产出的是 `bin/<exe>` 而不是 `bin/<exe>/`。
* **产物层**（有 `dist/release/*.zip|tar.gz` 才跑，没有就 skip）：包内 `bin/` 真的是
  平铺的、只有一个可执行文件、能跑起来。

⚠️ **完整验收要真的构建一次**：`python tools/build_release.py --engine rust`
（Windows 上还会调 Inno Setup 出 setup.exe），三平台另由
`.github/workflows/release.yml` 在打 tag 时各跑一遍。本文件不替人跑 cargo
（那是几十秒的构建，塞进每次回归不合适）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_release  # noqa: E402

DIST = ROOT / "dist" / "release"
EXE = "jishi.exe" if os.name == "nt" else "jishi"
ZIPS = sorted(DIST.glob("jishi-*.zip"))
TARS = sorted(DIST.glob("jishi-*.tar.gz"))


# ---------------------------------------------------------------------------
# 一、结构层：默认走 cargo + 布局契约
# ---------------------------------------------------------------------------

def test_默认引擎是_cargo():
    """R8.3 的开关：**默认**必须是 rust（`--engine python` 只为 R8.4 前兜底）。"""
    d = build_release._make_parser().parse_args([])
    assert d.engine == "rust", d.engine
    # 显式选 python 仍然要能用（R8.4 之前不拆掉这条路）
    assert build_release._make_parser().parse_args(["--engine", "python"]).engine == "python"
    # 发行包里的可执行名是**用户看到的那个**（jishi），不是仓库里的 jishi-rs
    assert build_release._build_exe_name() == EXE


def test_安装脚本假设的是平铺_bin():
    """把「安装脚本要的是平铺 `bin/jishi`」这条钉住。

    ⚠️ 这条看着像在测字符串，其实测的是**契约**：只要哪天有人把发行布局改回嵌套，
    安装脚本就会静默失效（自检失败 + PATH 指错），而**没有任何其它测试会发现**。
    """
    sh = (ROOT / "tools" / "install.sh").read_text(encoding="utf-8")
    ps = (ROOT / "tools" / "install.ps1").read_text(encoding="utf-8")
    # 自检要指向**平铺**的可执行文件
    assert '"$BIN_DIR/jishi" --version' in sh, "install.sh 的自检不再指向平铺 bin/jishi"
    assert '"$binDir\\jishi.exe" --version' in ps, "install.ps1 的自检不再指向平铺 bin\\jishi.exe"
    # 进 PATH 的是 bin/ 本身（不是 bin/oracle/jishi/）
    assert ".jishi/bin:" in sh, "install.sh 该把 ~/.jishi/bin 加进 PATH"
    assert '$installDir\\bin"' in ps, "install.ps1 该把 <安装目录>\\bin 加进 PATH"


def test_发行脚本产出的是单文件不是目录():
    """源码层的判据：rust 那条路是 `copy2`（文件），python 那条才是 `copytree`（目录）。"""
    src = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
    rust = src.split("def _build_rust_into")[1].split("def _build_python_into")[0]
    assert "shutil.copy2(src, dst)" in rust, "rust 引擎应当只复制**一个文件**"
    assert "copytree" not in rust, "rust 引擎不该产生目录"


# ---------------------------------------------------------------------------
# 二、产物层：有包就验，没有就跳过（不替人构建）
# ---------------------------------------------------------------------------

has_pkg = pytest.mark.skipif(
    not (ZIPS or TARS),
    reason="没有 dist/release 下的发行包；先跑 "
           "`python tools/build_release.py --engine rust`")


def _zip_names(z: Path) -> list[str]:
    """打开 zip 读清单；**打不开时给一句能照做的话**。

    ⚠️ 来历（2026-10-10）：一次「拉 0.2.2 镜像时网络断掉」在 `dist/release/`
    留下 **9 字节的占位文件**（内容其实是 404 页），`zipfile` 直接抛
    `BadZipFile: File is not a zip file` —— 从报错里看不出该干什么。
    """
    try:
        with zipfile.ZipFile(z) as zf:
            return zf.namelist()
    except zipfile.BadZipFile as e:
        raise AssertionError(
            f"{z.name} 打不开（{e}）—— 大概是**残包/占位文件**"
            f"（只有 {z.stat().st_size} 字节）。删掉它再跑："
            f"`rm -f 'dist/release/{z.name}'*`") from e


def _tar_names(t: Path) -> list[str]:
    """同 `_zip_names`，给 tar.gz 用。"""
    try:
        with tarfile.open(t) as tf:
            return tf.getnames()
    except tarfile.TarError as e:
        raise AssertionError(
            f"{t.name} 打不开（{e}）—— 大概是**残包/占位文件**"
            f"（只有 {t.stat().st_size} 字节）。删掉它再跑："
            f"`rm -f 'dist/release/{t.name}'*`") from e


def _bin_entries(names: list[str]) -> list[str]:
    """包内 `bin/` 的**直接子项**（相对包根，去掉包名那一层）。"""
    out = []
    for n in names:
        parts = n.split("/")
        if len(parts) >= 3 and parts[1] == "bin":
            out.append("/".join(parts[1:]))
    return sorted(out)


def _is_legacy(names: list[str]) -> bool:
    """老包（PyInstaller onedir）—— 里面有 `_internal/`，布局是嵌套的。

    ⚠️ `dist/release/` 里可能还躺着 0.2.1 早期用 PyInstaller 打的包；
    本文件判的是**cargo 引擎**的产物，所以遇到老包**跳过**（并让测试说清楚跳过了几个）。
    """
    return any("/_internal/" in n for n in names)


@has_pkg
def test_产物_包内_bin_是平铺的():
    """**R8.3 的核心判据**：cargo 打的包里，`bin/` 下只有一个可执行文件、没有中间目录。"""
    checked, skipped = 0, 0
    for z in ZIPS:
        names = _zip_names(z)
        if _is_legacy(names):
            skipped += 1
            continue
        tops = _bin_entries(names)
        assert tops == [f"bin/{EXE}"], f"{z.name} 的 bin/ 不是平铺的：{tops[:6]}"
        checked += 1
    for t in TARS:
        names = _tar_names(t)
        if _is_legacy(names):
            skipped += 1
            continue
        tops = _bin_entries(names)
        assert tops == [f"bin/{EXE}"], f"{t.name} 的 bin/ 不是平铺的：{tops[:6]}"
        checked += 1
    if not checked:
        pytest.skip(f"{len(ZIPS) + len(TARS)} 个包全是 PyInstaller 老包，"
                    "没有 cargo 产物可验 —— 跑 `python tools/build_release.py --engine rust`")


@has_pkg
def test_产物_体积与文件数_换cargo的收益看得见():
    """单二进制把发行包从「几百个文件 / 20MB」压到「十几个文件 / 3MB 量级」。"""
    modern = []
    for z in ZIPS:
        if not _is_legacy(_zip_names(z)):
            modern.append(z)
    if not modern:
        pytest.skip("dist/release 里只有 PyInstaller 老包；"
                    "跑 `python tools/build_release.py --engine rust`")
    z = modern[0]
    names = [n for n in _zip_names(z) if not n.endswith("/")]
    assert len(names) < 40, f"{z.name} 里 {len(names)} 个文件 —— 是不是又打成 onedir 了？"
    mb = z.stat().st_size / 1024 / 1024
    assert mb < 8, f"{z.name} 有 {mb:.2f} MB（cargo 单二进制应在 3MB 量级）"
    # 顺便：语言卡随包走（离线不缺料）
    assert any(n.endswith("share/ai-card.md") for n in names), names
