# -*- coding: utf-8 -*-
"""M201 · 下游版本跟随：包声明「基石版本」+ `tools/下游回归.py`。

## 这一站要解决什么

语言仓库里的判据只测**语言自己**。第一个真实下游（`基石数据库`，纯基石写的
数据库引擎）用的是**上一版**语言写的 —— 语义改一点、报错文案改一句，它可能
悄悄坏掉，而本地全量依旧全绿。两件事：

1. **版本声明**：包的 `包.json` 写 `"基石版本": ">=0.3.4"`，装/导入时校验。
   以前只在 `jishi 安装`（包管理）那条路上校验，而「clone 下来直接放进
   `.jishi/packages/`」完全绕过它 —— 声明等于没写。
2. **下游回归**：`tools/下游回归.py` 把清单里的项目真跑一遍（自检 / 冒烟 / 对拍），
   发版前与 CI 各跑一次。

## 为什么这几条判据值得写

「版本声明」这种功能最容易写成「有这条代码，但用户实际走的那条路不校验」——
所以这里**两条路都测**：Rust 前端（`jishi-rs`，用户装的）与 Python 前端
（`oracle.jishi.cli`，参考实现 / 判据基准）。两边都得报同一句话。

## 为什么三平台都跑

这几条只做「造包 + 起子进程」，没有注册表 / 原生窗口那些平台专属的东西；
而「版本声明挡不挡得住」正是**所有平台**都该成立的事。`rust_exe_path()` 在
非 Windows 上给的是不带 `.exe` 的那个名字（conftest 里分平台取）。

## 关于「Python 前端多 6 条不一致」

`tools/下游回归.py --引擎 python` 跑 `基石数据库` 的对拍是 **94/100**（rust 是
100/100）。查过了，**不是 jishi-db 的问题**：那 6 条是「Python 内置 `数据库` 模块」
与「Rust 内置 `数据库` 模块」的既有文案差异（`给过来的却是 列表` vs
`给过来的却是「['名']」` 这类），jishi-db 复刻的是 Rust 的文案。
属 M112 的老账，M201 只负责**把它暴露出来**（写进路线图与 CHANGELOG），不在这里顺手改。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conftest import rust_exe_path  # noqa: E402

def _写包(根: Path, 名字: str, 基石版本=None) -> Path:
    """造一个最小本地包（一个模块、一个函数）。"""
    包 = 根 / ".jishi" / "packages" / 名字
    包.mkdir(parents=True)
    meta = {"名字": 名字, "版本": "0.1.0"}
    if 基石版本 is not None:
        meta["基石版本"] = 基石版本
    (包 / "包.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (包 / "模.jsh").write_text("函数 打招呼()：\n    返回 `嗨`\n", encoding="utf-8")
    return 包


def _跑(引擎: str, 脚本: Path, 目录: Path):
    """用指定前端跑一份脚本，返回 `(退出码, 输出)`。"""
    if 引擎 == "rust":
        命令 = [str(rust_exe_path()), str(脚本)]
    else:
        命令 = [sys.executable, "-m", "oracle.jishi.cli", str(脚本)]
    环境 = dict(os.environ, PYTHONPATH=str(ROOT))
    r = subprocess.run(命令, cwd=str(目录), capture_output=True, timeout=180,
                       env=环境)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


引擎们 = pytest.mark.parametrize("引擎", ["rust", "py"])


# ---------------------------------------------------------------------------
# 一、「基石版本」声明
# ---------------------------------------------------------------------------


@引擎们
def test_版本满足就能导入(tmp_path, 引擎):
    目录 = tmp_path / "工程"
    目录.mkdir()
    _写包(目录, "甲包", ">=0.1.0")
    (目录 / "主.jsh").write_text(
        "导入 甲包 从 本地包 为 甲\n打印(甲.打招呼())\n", encoding="utf-8", newline="\n")
    码, 文 = _跑(引擎, 目录 / "主.jsh", 目录)
    assert 码 == 0, 文[-500:]
    assert "嗨" in 文, 文[-300:]


@引擎们
def test_版本不够要说清怎么升(tmp_path, 引擎):
    """🔴 这条错**必须**把人指向「升级语言」，而不是让他去翻包代码。

    没有这句时，用户看到的是「没有找到名字 X」—— 会以为包写错了。
    """
    目录 = tmp_path / "工程"
    目录.mkdir()
    _写包(目录, "乙包", ">=9.9.9")
    (目录 / "主.jsh").write_text(
        "导入 乙包 从 本地包 为 乙\n打印(乙.打招呼())\n", encoding="utf-8", newline="\n")
    码, 文 = _跑(引擎, 目录 / "主.jsh", 目录)
    assert 码 != 0, f"版本不够却跑起来了：{文[-300:]}"
    assert "基石版本" in 文, 文[-400:]
    assert "9.9.9" in 文, f"得说清要哪一版：{文[-400:]}"
    assert "jishi 升级" in 文, f"得给一条能走的路：{文[-400:]}"


@引擎们
def test_字段类型不对要报错不能静默(tmp_path, 引擎):
    """写成数字（`"基石版本": 3`）时**不能当成没写** —— 那会让用户以为声明生效了。"""
    目录 = tmp_path / "工程"
    目录.mkdir()
    包 = _写包(目录, "丙包")
    (包 / "包.json").write_text(
        json.dumps({"名字": "丙包", "版本": "0.1.0", "基石版本": 3},
                   ensure_ascii=False), encoding="utf-8")
    (目录 / "主.jsh").write_text(
        "导入 丙包 从 本地包 为 丙\n打印(丙.打招呼())\n", encoding="utf-8", newline="\n")
    码, 文 = _跑(引擎, 目录 / "主.jsh", 目录)
    assert 码 != 0, f"类型写错却当没写：{文[-300:]}"
    assert "基石版本" in 文 and "文本" in 文, 文[-400:]


@引擎们
def test_没写这个字段就不管_老包照用(tmp_path, 引擎):
    目录 = tmp_path / "工程"
    目录.mkdir()
    _写包(目录, "丁包")            # 不写「基石版本」
    (目录 / "主.jsh").write_text(
        "导入 丁包 从 本地包 为 丁\n打印(丁.打招呼())\n", encoding="utf-8", newline="\n")
    码, 文 = _跑(引擎, 目录 / "主.jsh", 目录)
    assert 码 == 0, f"没声明的老包不该被拦：{文[-400:]}"


# ---------------------------------------------------------------------------
# 二、下游回归工具
# ---------------------------------------------------------------------------


def _工具(*参数):
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "下游回归.py"), *参数],
        capture_output=True, timeout=300)


def test_仓库里的清单能解析():
    """清单是**发布的闸门**，写错了（少 url / 少步骤）就等于闸门没关上。"""
    r = _工具("--清单")
    文 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, 文[-600:]
    assert "清单 OK" in 文, 文[-400:]
    assert "基石数据库" in 文, f"清单里第一个下游不见了：{文[-400:]}"


def test_清单写错要指出来(tmp_path):
    """🔴 「静默跳过」是最坏的：清单里少一个 url，工具就该停下来说清楚。"""
    坏 = tmp_path / "坏清单.toml"
    坏.write_text('[[downstream]]\nname = "甲"\n', encoding="utf-8")   # 缺 url
    r = _工具("--清单", "--清单路径", str(坏))
    文 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode != 0, f"清单缺 url 却通过了：{文[-400:]}"
    assert "url" in 文, f"得指出缺哪个字段：{文[-400:]}"


def test_清单缺步骤也要说(tmp_path):
    坏 = tmp_path / "空步骤.toml"
    坏.write_text(
        '[[downstream]]\nname = "甲"\nurl = "https://example.com/x.git"\n',
        encoding="utf-8")
    r = _工具("--清单", "--清单路径", str(坏))
    文 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode != 0, f"没有步骤却通过了：{文[-400:]}"
    assert "步骤" in 文, 文[-400:]
