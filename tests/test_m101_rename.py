# -*- coding: utf-8 -*-
r"""R8.4b-2：**改名** —— `jishi` 留给单二进制，Python 实现退成 `jishi-py`。

## 为什么必须退名

装了 Python 包就会在 PATH 上放一个 `jishi`，而它跑的是**另一套实现**；发行包
（R8.3 起是 cargo 单二进制）放的也叫 `jishi`。**两条路同名** ⇒ 用户分不清自己敲的
是哪一个，出了问题也说不清是哪套实现。

所以：**`jishi` = 单二进制**（正式那份）· **`jishi-py` = Python 实现**
（改语言的人的入口 + 测试的判据基准）。功能一个没少，只是名字不再打架。

⚠️ 本文件只钉**名字契约**（谁该叫什么）。包内布局由 `test_m98` 钉，
「Python 实现仍然是判据基准」由各对拍测试钉。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")


def _scripts() -> dict:
    """读 `pyproject.toml` 的 `[project.scripts]`（那张表就是「装了之后有哪些命令」）。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    in_sec = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_sec = s == "[project.scripts]"
            continue
        if in_sec and "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def test_Python_包不再占用_jishi_这个名字():
    """`jishi` 与 `jishi-mcp` 都必须**让出来**。"""
    sc = _scripts()
    assert "jishi" not in sc, f"`jishi` 还留在 [project.scripts]，两条实现同名了：{sc}"
    assert "jishi-mcp" not in sc, f"`jishi-mcp` 也该退名：{sc}"


def test_Python_实现不再对外提供安装入口():
    """R8.4b-2：`[project.scripts]` **整段清空** —— 不再对外提供安装入口。

    （先退成 `jishi-py`，随后拍板「不再对外提供」⇒ 干脆不留入口。
    想看 Python 实现走源码内的 `python -m oracle.jishi.cli`。）
    """
    sc = _scripts()
    assert sc == {}, f"[project.scripts] 该是空的（不再对外提供安装入口）：{sc}"


def test_Python_实现仍然可用_只是换了名字():
    """退名不是删功能：`python -m oracle.jishi.cli` 这条路照旧。"""
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "--version"],
                       capture_output=True, cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:300]
    assert "0.2" in r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(not EXE.is_file(),
                    reason="缺 rust/target/release/jishi-rs（跳过而不是替人构建）")
def test_顶上来的那份自称_jishi():
    """🔴 关键一条：单二进制报的版本行里是 **`jishi`**（不是 `jishi-rs`）——
    用户看到的名字必须与文档里写的、以及扩展调用的一致。"""
    r = subprocess.run([str(EXE), "--version"], capture_output=True, cwd=str(ROOT), timeout=120)
    text = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert "基石 jishi" in text, text
    assert "jishi-rs" not in text, f"版本行还露着内部名字：{text}"


def test_发行脚本把_Rust_产物改名成_jishi(tmp_path):
    """包内那份叫 `jishi`（不是 `jishi-rs`）—— 名字在**打包时**对齐。

    ⚠️ 仓库里的 cargo 产物仍叫 `jishi-rs`（构建细节；两者同时存在才能做对拍），
    用户看到的只有包里的那个名字。
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import build_release
    assert build_release._build_exe_name() == ("jishi.exe" if os.name == "nt" else "jishi")
    assert build_release._rust_exe_name().startswith("jishi-rs")
