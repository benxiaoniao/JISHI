# -*- coding: utf-8 -*-
"""M131：包管理的错误体验 —— **每一堵墙都有名字**。

## 为什么包管理错误也该有 E 码

语言错误的规矩是「错误码 + 行列 + 修法」。包管理**没有行列**（它不是源码问题），
但另外两样一样重要：**一个能查的码**、**一句能照着做的改法**。

以前全是 `raise ValueError("...")` —— 用户看到的是一句话，
没有码就没法 `jishi 错误 <码>` 查全貌，也没法跟别人说「我遇到的是 E4003」。

## 这一组判据盯三件事

1. **每个码都登记在说明表里**（`errcodes.INFO`）—— 有码没说明等于没码；
2. **说明里不许夹 markdown**（CLI 不渲染，`**X**` 会字面显示，很难看）；
3. **`PackageError` 必须同时是 `ValueError`** —— 见下面那条的说明，这是**兼容性护栏**。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import errcodes                    # noqa: E402
from oracle.jishi import packages as PKG             # noqa: E402
from oracle.jishi import cli as JCLI                 # noqa: E402
from oracle.jishi.errors import (PackageBuildError, PackageChecksumError,  # noqa: E402
                                 PackageConflictError, PackageCycleError,
                                 PackageError, PackageIndexError, PackageLockError,
                                 PackageMetaError, PackageMissingDepError,
                                 PackageNotFoundError)


#: 包管理这一段该有的码（少一个就说明某个场景还在抛裸 `ValueError`）
应有 = [
    PackageError, PackageNotFoundError, PackageMetaError, PackageConflictError,
    PackageMissingDepError, PackageCycleError, PackageChecksumError,
    PackageIndexError, PackageLockError, PackageBuildError,
]


# ---------------------------------------------------------------------------
# 一、码和说明都要齐
# ---------------------------------------------------------------------------

def test_每个包管理异常都有自己的码():
    码们 = [c.code for c in 应有]
    assert len(set(码们)) == len(码们), f"码重复了：{码们}"
    for c in 应有:
        assert c.code.startswith("E40"), f"{c.__name__} 的码不在 E40 段：{c.code}"
        assert c.title, f"{c.__name__} 没有标题"


def test_每个包管理码都有说明():
    """有码没说明 = 用户在 `jishi 错误 E4003` 里看到一片空白。"""
    缺 = [c.code for c in 应有 if c.code not in errcodes.INFO]
    assert not 缺, f"这些码没写说明（补 oracle/jishi/errcodes.py）：{缺}"
    for c in 应有:
        信息 = errcodes.INFO[c.code]
        assert 信息.get("what"), f"{c.code} 没有「什么意思」"
        assert 信息.get("fix"), f"{c.code} 没有「怎么改」——装包失败最需要的就是这一句"


def test_包管理说明里不许夹markdown():
    """CLI 不渲染 markdown —— `**强调**` 会字面显示。"""
    坏 = []
    for c in 应有:
        信息 = errcodes.INFO[c.code]
        全 = " ".join([信息.get("what", ""), 信息.get("fix", ""),
                       *[str(x) for x in 信息.get("why") or []]])
        if "**" in 全:
            坏.append(c.code)
    assert not 坏, f"这些说明里有 markdown 加粗（改成「」）：{坏}"


def test_错误总览里能看到包管理段():
    码们 = dict(JCLI._all_error_codes())
    assert "E4001" in 码们, "总览里没有 E4001"
    assert 码们["E4001"] == "索引里没有这个包"


# ---------------------------------------------------------------------------
# 二、`jishi 错误 E400x` 真能查到
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("码", ["E4001", "E4003", "E4006", "E4008"])
def test_命令行能查包管理码(码, capfd):
    断言 = JCLI.main(["错误", 码])
    出 = capfd.readouterr().out
    assert 断言 == 0, 出
    assert 码 in 出, 出
    assert "怎么改" in 出, 出          # 有码还不够，要能照着做


# ---------------------------------------------------------------------------
# 三、真撞上时确实带码（不是只写了类没人用）
# ---------------------------------------------------------------------------

def test_包不存在给E4001():
    with pytest.raises(PackageNotFoundError) as 捕:
        PKG.resolve_package({"包": {}}, "根本没有这个包")
    assert 捕.value.code == "E4001"
    assert "根本没有这个包" in str(捕.value)


def test_自述文件不合格给E4002(tmp_path):
    目录 = tmp_path / "坏包"
    目录.mkdir()
    (目录 / "包.json").write_text('{"名字": "甲"}', encoding="utf-8")   # 缺版本
    with pytest.raises(PackageMetaError) as 捕:
        PKG.read_package_meta(str(目录))
    assert 捕.value.code == "E4002"


def test_缺自述文件也给E4002(tmp_path):
    空 = tmp_path / "空的"
    空.mkdir()
    with pytest.raises(PackageMetaError):
        PKG.read_package_meta(str(空))


def test_打包没模块给E4009(tmp_path):
    目录 = tmp_path / "空包"
    目录.mkdir()
    (目录 / "包.json").write_text(
        '{"名字": "空包", "版本": "1.0.0"}', encoding="utf-8")
    with pytest.raises(PackageBuildError) as 捕:
        PKG.build_package_zip(str(目录), str(tmp_path / "out"))
    assert 捕.value.code == "E4009"


def test_依赖冲突给E4003():
    """冲突由 `resolve_closure` 判出来，CLI 那边转成 `E4003`。

    ⚠️ 不能拿 `_install_closure("甲", ...)` 测 —— 它**一次只接一个根**，
    而冲突是「甲和乙一起要丙」才有的。**测错入口会测成别的事情**
    （第一版就是这么红的：装到「丙没有下载地址」）。所以这里分两段：
    闭包判出冲突，异常类对得上。
    """
    idx = {"格式": PKG.INDEX_FORMAT, "版本": 1, "包": {
        "甲": {"版本": "1.0.0", "依赖": {"丙": ">=2.0.0"}},
        "乙": {"版本": "1.0.0", "依赖": {"丙": "<2.0.0"}},
        "丙": {"版本": "1.5.0"},
    }}
    闭包 = PKG.resolve_closure(idx, ["甲", "乙"])
    assert 闭包["冲突"], "闭包没判出冲突"
    项 = 闭包["冲突"][0]
    # CLI 在 闭包["冲突"] 非空时抛的就是它（见 `_install_closure`）
    assert PackageConflictError.code == "E4003"
    文 = PackageConflictError(f"依赖冲突：「{项['包']}」…").render()
    assert "E4003" in 文 and "依赖冲突" in 文


def test_索引缺下载地址给E4002(monkeypatch, tmp_path):
    """「索引写错了」和「包不存在」要分开报 —— 改法完全不同。"""
    坏索引 = tmp_path / "索引.json"
    坏索引.write_text('{"格式": "jishi-package-index", "版本": 1, '
                      '"包": {"甲": {"版本": "1.0.0"}}}', encoding="utf-8")
    with pytest.raises(PackageMetaError) as 捕:
        JCLI._install_one("甲", {"版本": "1.0.0"}, source=str(坏索引), force=True)
    assert 捕.value.code == "E4002"
    assert "下载地址" in str(捕.value)


# ---------------------------------------------------------------------------
# 四、兼容性护栏：加了码，但不许破坏任何现有的 `except ValueError`
# ---------------------------------------------------------------------------

def test_包管理异常仍然是ValueError():
    """🔴 **这条是护栏，不是凑数**。

    `packages.py` 原来所有失败都是 `raise ValueError(...)`，调用方（CLI、测试、
    别人写的脚本）到处 `except ValueError`。如果 `PackageError` 只继承 `JishiError`，
    那些 `except` **全部接不住** —— 错误会冒到顶层变成 traceback。

    ⇒ 多继承一个 `ValueError`，等于「**加了码，但不换身份**」。
    """
    for c in 应有:
        assert issubclass(c, ValueError), f"{c.__name__} 不是 ValueError，旧代码会漏接"
        assert issubclass(c, PackageError), f"{c.__name__} 不是 PackageError 的子类"


def test_带码异常能被except_ValueError接住():
    try:
        raise PackageChecksumError("校验和不对")
    except ValueError as e:                                  # 模拟旧代码
        assert getattr(e, "code", "") == "E4006"
        assert "E4006" in e.render()
    else:
        pytest.fail("没被 except ValueError 接住 —— 兼容性破了")


def test_渲染不带行列也好看():
    """包管理错误没有行列 —— `render()` 在 `line is None` 时该只打标题+消息。"""
    文 = PackageNotFoundError("索引里没有包「xyz」").render()
    assert 文.startswith("错误 E4001：")
    assert "xyz" in 文
    assert "┌─" not in 文, "不该冒出源码位置框（这不是源码报错）"
