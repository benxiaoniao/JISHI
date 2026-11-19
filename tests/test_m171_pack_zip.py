# -*- coding: utf-8 -*-
"""M171 · `压缩.打包确定`：**可复现**的打包。

## 为什么它值得单独一组判据

发版产物、站点构建、安装包 —— 落盘的东西一旦带上「这次的时间」，
「这次到底改了没有」就再也说不清了。M109 已经为 gzip 踩过一遍
（`filename=""` + `mtime=0`），这里是同一件事在 zip 上的版本。

会漂的三样，每一样都得定死：

| 会漂的东西 | 不定死会怎样 |
|---|---|
| 条目时间 | 文件 mtime 每次都参与 ⇒ 打两次两个包 |
| 条目顺序 | 走目录的顺序跟着文件系统走 |
| 权限 / 制作系统 | ZipInfo 的默认值跟着**平台**走（Unix=3 / Windows=0）|

⚠️ **跨引擎不逐字节相同**是已知的、有意的：三个宿主的压缩算法不一样
（Python / Node 用 deflate，Rust 侧只有 stored）。所以判据的口径是
「**同一个引擎、同样的输入 ⇒ 同样的字节**」+「**解出来的条目一样**」。
"""

import sys
import zipfile

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402


def _造树(tmp_path):
    """一棵小目录树：两层 + 一个中文名（顺带验 UTF-8 条目名）。"""
    源 = tmp_path / "源"
    (源 / "bin").mkdir(parents=True)
    (源 / "share" / "子").mkdir(parents=True)
    (源 / "bin" / "a.exe").write_bytes(b"A")
    (源 / "share" / "b.txt").write_bytes(b"BB")
    (源 / "share" / "子" / "说明.txt").write_bytes("中文内容".encode("utf-8"))
    return 源


def _跑(源: str, tmp_path, exe=None):
    import subprocess

    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    命令 = ([str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"])
    r = subprocess.run(命令 + ["程序.jsh"], cwd=str(tmp_path),
                       capture_output=True, timeout=180, input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace"), r.returncode


程序 = """\
导入 压缩
压缩.打包确定("一.zip", "源")
压缩.打包确定("二.zip", "源")
打印(压缩.列出内容("一.zip"))
"""


# ---------------------------------------------------------------------------
# 一、可复现
# ---------------------------------------------------------------------------

def test_同一引擎打两次字节相同(tmp_path):
    _造树(tmp_path)
    出, 码 = _跑(程序, tmp_path)
    assert 码 == 0, 出[:400]
    a = (tmp_path / "一.zip").read_bytes()
    b = (tmp_path / "二.zip").read_bytes()
    assert a == b, (
        f"两次打包的字节不同（{len(a)} vs {len(b)}）—— "
        "时间/顺序/权限里有一样还跟着磁盘走")


def test_条目按名字排序且用的是正斜杠(tmp_path):
    """顺序定死才可复现；分隔符统一 `/`（Windows 的 `\\` 在 zip 里是非法的）。"""
    _造树(tmp_path)
    _跑(程序, tmp_path)
    名们 = zipfile.ZipFile(tmp_path / "一.zip").namelist()
    assert 名们 == sorted(名们), f"条目没排序：{名们}"
    assert all("\\" not in n for n in 名们), 名们
    assert "share/子/说明.txt" in 名们, f"中文名条目没进包：{名们}"


@pytest.mark.skipif(not rust_exe_path().is_file(), reason="没编 jishi-rs")
def test_Rust_也可复现且条目一致(tmp_path):
    源 = _造树(tmp_path)
    出, 码 = _跑(程序.replace('"源"', '"源"'), tmp_path, exe=rust_exe_path())
    assert 码 == 0, 出[:400]
    a = (tmp_path / "一.zip").read_bytes()
    b = (tmp_path / "二.zip").read_bytes()
    assert a == b, "Rust 侧两次打包字节不同"
    # 条目清单要与 Python 侧**完全一致**（压缩方式不同不影响「包里有什么」）
    名们 = zipfile.ZipFile(tmp_path / "一.zip").namelist()
    出2, _ = _跑(程序, tmp_path)
    名们2 = zipfile.ZipFile(tmp_path / "一.zip").namelist()
    assert 名们 == 名们2, f"两引擎的条目不一样：{名们} vs {名们2}"


# ---------------------------------------------------------------------------
# 二、把话说清楚：传错了要报中文错
# ---------------------------------------------------------------------------

def test_传文件列表不给基础目录要报错(tmp_path):
    _造树(tmp_path)
    出, 码 = _跑('导入 压缩\n压缩.打包确定("x.zip", ["源/bin/a.exe"])\n', tmp_path)
    assert 码 == 1, 出[:300]
    assert "基础" in 出 and "条目名" in 出, 出[:400]


def test_目录里没有文件要报错(tmp_path):
    (tmp_path / "空").mkdir()
    出, 码 = _跑('导入 压缩\n压缩.打包确定("x.zip", "空")\n', tmp_path)
    assert 码 == 1, 出[:300]
    assert "空" in 出 and "没有文件" in 出, 出[:400]


def test_路径不存在要报错(tmp_path):
    出, 码 = _跑('导入 压缩\n压缩.打包确定("x.zip", "不存在")\n', tmp_path)
    assert 码 == 1, 出[:300]
    assert "既不是目录也不是列表" in 出, 出[:400]
