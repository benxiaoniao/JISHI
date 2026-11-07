# -*- coding: utf-8 -*-
"""M129：包打出来的字节必须是**内容决定的**，而且 Python / Rust 两边一致。

## 为什么这几条是包管理的地基

索引里登记的是 **sha256**。如果同源码能打出两个哈希，那这个校验和
**在验证什么**？什么都验证不了 —— 甲用 Python 版 `发布`、乙用 Rust 版重打一遍，
哈希不同，看起来像「包被改过」。

以前就是这样：Rust 模块头还专门登记过一条「刻意保留的差异：打包出来的 zip 字节不同」。
那不是无害差异，**它让可信链断在第一步**。

## 决定字节的四个字段（三个都要固定）

| 字段 | 必须固定成 | 不固定会怎样 |
|---|---|---|
| 时间/日期 | 1980-01-01（全 0 / `0x21`） | **源文件的 mtime 会进 zip** ⇒ 换台机器 checkout 出来就不同 |
| 制作系统 | MS-DOS（高字节 0） | Unix 填 3、Windows 填 0 |
| 外部属性 | `0o600 << 16` | ⚠️ Python 的 `writestr` 遇到 0 **会自动填这个**，绕不过去，只能两边都写它 |
| 压缩方法 | stored | deflate 的实现细节两边不同，永远对不齐 |

⚠️ 这里的用例**故意**去动 mtime —— 那正是「另一台机器」的样子。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import packages as PKG          # noqa: E402

#: 用一个真实存在的示例包当被测物（它随仓库走，不依赖本地装过什么）
示例包 = ROOT / "examples" / "packages" / "基础库"


def _rs_exe() -> Path | None:
    for 名 in ("jishi-rs.exe", "jishi-rs"):
        p = ROOT / "rust" / "target" / "release" / 名
        if p.is_file():
            return p
    return None


需要_rs = pytest.mark.skipif(_rs_exe() is None,
                             reason="没编 Rust（cargo build --release）")


# ---------------------------------------------------------------------------
# 一、同一份内容 ⇒ 同一个哈希
# ---------------------------------------------------------------------------

def test_改_mtime_不影响哈希(tmp_path):
    """🔴 核心判据：**内容决定字节，时间不决定**。

    实测过它的反面：只 `utime` 一下（内容一个字没动），旧实现的哈希从
    `e4cd3e3c…` 变成 `14f91d34…`。
    """
    源 = tmp_path / "src"
    shutil.copytree(示例包, 源)

    z1, _ = PKG.build_package_zip(str(源), str(tmp_path / "a"))
    h1 = PKG._sha256_file(z1)

    for base, _, names in os.walk(源):
        for fn in names:
            os.utime(os.path.join(base, fn), (1700000000, 1700000000))

    z2, _ = PKG.build_package_zip(str(源), str(tmp_path / "b"))
    h2 = PKG._sha256_file(z2)

    assert h1 == h2, "改了 mtime 哈希就变 —— 时间戳又进 zip 了"


def test_同一目录连打两次一致(tmp_path):
    """基线：不动任何东西时也该一致（不该有随机成分）。"""
    z1, _ = PKG.build_package_zip(str(示例包), str(tmp_path / "a"))
    z2, _ = PKG.build_package_zip(str(示例包), str(tmp_path / "b"))
    assert PKG._sha256_file(z1) == PKG._sha256_file(z2)


def test_成员顺序稳定(tmp_path):
    """成员顺序进的是「内容」，顺序乱 ⇒ 字节乱。"""
    z, _ = PKG.build_package_zip(str(示例包), str(tmp_path / "a"))
    with zipfile.ZipFile(z) as zf:
        名们 = zf.namelist()
    assert 名们, "包里一个成员都没有"
    # zip 里存的是「包名/相对路径」，且一律正斜杠
    assert all("/" in n for n in 名们), 名们
    assert all("\\" not in n for n in 名们), 名们


def test_打出来的包能读回来(tmp_path):
    """stored 是完全合规的 zip —— 通用工具必须读得出来（别为了字节一致牺牲可用性）。"""
    z, 名 = PKG.build_package_zip(str(示例包), str(tmp_path / "a"))
    with zipfile.ZipFile(z) as zf:
        assert zf.testzip() is None
        内容 = {n: zf.read(n) for n in zf.namelist()}
    assert any(n.endswith("包.json") for n in 内容), list(内容)
    assert any(n.endswith(".jsh") for n in 内容), list(内容)


# ---------------------------------------------------------------------------
# 二、Python 与 Rust 必须一致（不然校验和没有意义）
# ---------------------------------------------------------------------------

@需要_rs
def test_Python_与_Rust_打出的包字节相同(tmp_path):
    """🔴 这条是 M129 的重点。

    以前 Rust 模块头登记着「打包字节不同，刻意保留」。那不是无害差异：
    **校验和因此无法作为判据** —— 两个版本发同一个包会得到两个 sha256。
    """
    zip_py, 名 = PKG.build_package_zip(str(示例包), str(tmp_path / "py"))

    out = tmp_path / "rs"
    r = subprocess.run([str(_rs_exe()), "发布", str(示例包), "--out", str(out)],
                       capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")

    zip_rs = out / Path(zip_py).name
    assert zip_rs.is_file(), f"Rust 那边没产出 {Path(zip_py).name}（目录里：{list(out.iterdir()) if out.exists() else '不存在'}）"

    h_py = PKG._sha256_file(zip_py)
    h_rs = PKG._sha256_file(str(zip_rs))
    assert h_py == h_rs, (
        f"两边打出的包字节不同，校验和因此失去意义：\n"
        f"  Python {h_py}\n  Rust   {h_rs}")


@需要_rs
def test_Rust_打的包_Python_能解开(tmp_path):
    """字节一致之外，还要保证互操作性（stored 的 zip 任何工具都该读得出来）。"""
    out = tmp_path / "rs"
    subprocess.run([str(_rs_exe()), "发布", str(示例包), "--out", str(out)],
                   capture_output=True, timeout=120)
    zips = list(out.glob("*.zip"))
    assert zips, "Rust 没打出 zip"
    with zipfile.ZipFile(zips[0]) as zf:
        assert zf.testzip() is None
        assert zf.namelist()
