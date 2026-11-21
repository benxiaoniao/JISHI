# -*- coding: utf-8 -*-
"""M209 · 升级通道：CNB 首选（源顺序 · 看源 · 探源 · 三份清单一致）。

## 为什么这一站要单独有判据

源的**顺序**是行为契约：改一下，全国用户的升级路径就变了（官网服务器流量有限，
是用户明确定下的「只作兜底」）。而它藏在一个 `const` 里 —— 没有判据，
下次谁顺手调个顺序谁都不知道。

## 判据分两类

- **不需要网络**：顺序、短名解析、`--看源` 的输出、`--看源 --探` 用**离线夹具**
  （本地文件当清单）验「一致 / 不一致 / 全取不到」三条路。
- **需要网络**：真连三个源的那条**不进 CI**（`JISHI_ONLINE_TEST=1` 才跑），
  发版时改用 `python tools/check_mirrors.py`。

⚠️ 2026-11-17 实测定下的口径：三份清单**整份并不相同** —— 官网留全量历史
（12 个版本），CNB / GitHub 是发布资产（只带最新那一版）。所以「一致」的含义是
**最新版本号 + 那一版的记录**一致，不是整份字节相同。`--严格` 才要求整份相同。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path  # noqa: E402

RS = rust_exe_path()
需要rust = pytest.mark.skipif(not RS.is_file(), reason="发行版没编（cargo build --release）")


# ---------------------------------------------------------------------------
# 一、源顺序：Rust 常量 ↔ 发布检查脚本，必须是同一份
# ---------------------------------------------------------------------------

def _rust源们() -> list[str]:
    """从 `selfupdate.rs` 里抠出 `DEFAULT_RELEASE_INDEX_URLS` 的三条地址。

    不做「跑一遍看输出」是有意的：这里要的是**源码里的顺序**，
    而不是某次运行的输出（后者会被 `JISHI_RELEASE_INDEX` 之类影响）。
    """
    源 = (ROOT / "rust" / "src" / "selfupdate.rs").read_text(encoding="utf-8")
    起 = 源.index("pub const DEFAULT_RELEASE_INDEX_URLS")
    止 = 源.index("];", 起)
    块 = 源[起:止]
    # `CNB_RELEASE_INDEX` 是个常量名，要把它指向的地址也读出来
    cnb = re.search(r'pub const CNB_RELEASE_INDEX: &str =\s*\n?\s*"(https://[^"]+)"', 源)
    cnb地址 = cnb.group(1) if cnb else ""
    出 = []
    for 行 in 块.splitlines():
        行 = 行.strip()
        if 行.startswith("CNB_RELEASE_INDEX,"):
            出.append(cnb地址)
        elif 行.startswith('"https://'):
            m = re.search(r'"(https://[^"]+)"', 行)
            if m:
                出.append(m.group(1))
    return 出


def test_源顺序是_cnb_github_官网():
    源们 = _rust源们()
    assert len(源们) == 3, 源们
    assert "cnb.cool" in 源们[0], f"第一条必须是 CNB（用户 2026-11-16 定）：{源们}"
    assert "github.com" in 源们[1], f"第二条该是 GitHub：{源们}"
    assert "xn--3jsy75e.cn" in 源们[2], f"第三条该是官网（只作兜底）：{源们}"


def test_发布检查脚本与Rust的源顺序一致():
    sys.path.insert(0, str(ROOT / "tools"))
    import check_mirrors                      # noqa: E402

    名们 = [x[0] for x in check_mirrors.SOURCES]
    assert 名们 == ["cnb", "github", "官网"], 名们
    # 地址也要对得上（同一份顺序、同一批地址 —— 改一处不改另一处就会漂）
    rust = _rust源们()
    assert "cnb.cool" in check_mirrors.SOURCES[0][1]
    assert "github.com" in check_mirrors.SOURCES[1][1]
    assert "xn--3jsy75e.cn" in check_mirrors.SOURCES[2][1]
    assert "cnb.cool" in rust[0]


# ---------------------------------------------------------------------------
# 二、清单判定：口径本身（离线，纯函数）
# ---------------------------------------------------------------------------

def _造清单(版本: str, 历史: bool = False) -> str:
    """造一份合法清单。`历史` = 多加一个旧版本（模拟官网留全史）。"""
    d = {
        "格式": "jishi-release-index",
        "格式版本": 1,
        "最新": 版本,
        "版本": {
            版本: {
                "日期": "2026-11-17",
                "平台": {
                    "windows-x64": {
                        "包": {"文件": f"jishi-{版本}-windows-x64.zip", "大小": 1234,
                             "校验和": "sha256:" + "0" * 64}
                    }
                },
            }
        },
    }
    if 历史:
        d["版本"]["0.0.9"] = {"日期": "2026-01-01", "平台": {}}
    return json.dumps(d, ensure_ascii=False)


def _项(名: str, 文本: str, 版本: str) -> tuple:
    import hashlib

    import tools.check_mirrors as cm
    d = json.loads(文本)
    录 = cm.规范化(d["版本"][版本])
    return (名, len(文本.encode("utf-8")), hashlib.sha256(文本.encode("utf-8")).hexdigest(),
            版本, 录)


def test_判定_三份一致时通过(capsys):
    import tools.check_mirrors as cm
    a = _造清单("0.3.5")
    结果 = [_项("cnb", a, "0.3.5"), _项("github", a, "0.3.5"), _项("官网", a, "0.3.5")]
    assert cm.判定(结果, []) == 0
    出 = capsys.readouterr().out
    assert "逐字节一致" in 出, 出


def test_判定_最新版本不同要红(capsys):
    import tools.check_mirrors as cm
    结果 = [
        _项("cnb", _造清单("0.3.5"), "0.3.5"),
        _项("github", _造清单("0.3.4"), "0.3.4"),
    ]
    assert cm.判定(结果, []) == 1
    assert "「最新」不一致" in capsys.readouterr().out


def test_判定_最新相同但那一版记录不同要红(capsys):
    """同一版号、不同内容（比如只同步了一半字段）—— 最阴的一种，必须红。"""
    import tools.check_mirrors as cm
    a = _造清单("0.3.5")
    b = a.replace("1234", "9999")
    结果 = [_项("cnb", a, "0.3.5"), _项("github", b, "0.3.5")]
    assert cm.判定(结果, []) == 1
    assert "记录不一样" in capsys.readouterr().out


def test_判定_有源取不到就要红(capsys):
    import tools.check_mirrors as cm
    a = _造清单("0.3.5")
    结果 = [_项("cnb", a, "0.3.5"), _项("github", a, "0.3.5")]
    assert cm.判定(结果, [("官网", "连接超时")]) == 1
    assert "取不到" in capsys.readouterr().out


def test_判定_官网留全史不算不一致(capsys):
    """官网留了历史版本 ⇒ 整份字节不同，但「最新 + 那版记录」一致 ⇒ 通过。"""
    import tools.check_mirrors as cm
    a = _造清单("0.3.5")
    b = _造清单("0.3.5", 历史=True)
    结果 = [_项("cnb", a, "0.3.5"), _项("github", a, "0.3.5"), _项("官网", b, "0.3.5")]
    assert cm.判定(结果, []) == 0
    出 = capsys.readouterr().out
    assert "历史留档" in 出, 出
    # 严格模式才要求整份相同
    assert cm.判定(结果, [], 严格=True) == 1


# ---------------------------------------------------------------------------
# 三、`jishi 升级 --看源`（离线：不 --探 就不取网络）
# ---------------------------------------------------------------------------

def _跑(参数, 环境=None):
    环 = dict(os.environ)
    环.pop("JISHI_RELEASE_INDEX", None)
    if 环境:
        环.update(环境)
    return subprocess.run([str(RS), *参数], capture_output=True, text=True,
                          encoding="utf-8", timeout=120, env=环)


@需要rust
def test_看源按顺序列三条且标出短名():
    r = _跑(["升级", "--看源"])
    出 = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, 出[:400]
    assert "1. cnb" in 出, 出
    assert "2. github" in 出, 出
    assert "3. 官网" in 出, 出
    assert 出.index("cnb") < 出.index("github") < 出.index("官网"), 出
    # 说明文字要讲清「官网只作兜底」—— 这是这条判据真正守的东西
    assert "兜底" in 出, 出
    # 不要把人看的地址里塞防缓存时间戳
    assert "?t=" not in 出, 出


@需要rust
def test_看源可以只看指定的通道(tmp_path):
    r = _跑(["升级", "--看源", "--源", "cnb"])
    出 = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, 出
    assert "cnb.cool" in 出
    assert "github.com" not in 出, "只指定了一个源，不该把别的也列出来"


@需要rust
def test_看源认不出的源标成自定义(tmp_path):
    清单 = tmp_path / "我的清单.json"
    清单.write_text(_造清单("0.3.5"), encoding="utf-8")
    r = _跑(["升级", "--看源", "--源", str(清单)])
    出 = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, 出
    assert "自定义源" in 出, 出


@需要rust
def test_看源探_离线夹具_一致时通过(tmp_path):
    甲 = tmp_path / "甲.json"
    乙 = tmp_path / "乙.json"
    文 = _造清单("0.3.5")
    甲.write_text(文, encoding="utf-8")
    乙.write_text(文, encoding="utf-8")
    r = _跑(["升级", "--看源", "--探", "--源", str(甲), "--源", str(乙)])
    出 = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, 出
    assert "逐字节一致：是" in 出, 出
    assert "最新 0.3.5" in 出, 出


@需要rust
def test_看源探_离线夹具_版本不同时红(tmp_path):
    甲 = tmp_path / "甲.json"
    乙 = tmp_path / "乙.json"
    甲.write_text(_造清单("0.3.5"), encoding="utf-8")
    乙.write_text(_造清单("0.3.4"), encoding="utf-8")
    r = _跑(["升级", "--看源", "--探", "--源", str(甲), "--源", str(乙)])
    出 = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 1, 出
    assert "不一致" in 出, 出


@需要rust
def test_看源探_全取不到时说人话(tmp_path):
    没 = tmp_path / "没有这个文件.json"
    r = _跑(["升级", "--看源", "--探", "--源", str(没)])
    出 = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 1, 出
    assert "全部取不到" in 出, 出


# ---------------------------------------------------------------------------
# 四、真连三个源（不进 CI；发版时跑 tools/check_mirrors.py）
# ---------------------------------------------------------------------------

@需要rust
@pytest.mark.skipif(os.environ.get("JISHI_ONLINE_TEST") != "1",
                    reason="要联网；设 JISHI_ONLINE_TEST=1 才跑（发版用 tools/check_mirrors.py）")
def test_线上三份清单一致():
    r = _跑(["升级", "--看源", "--探"])
    出 = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, 出[:800]
