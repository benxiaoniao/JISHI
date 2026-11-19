# -*- coding: utf-8 -*-
"""M146 —— **自建源托管**这条链路：命令产出的目录，能被安装器吃下去、装完能用。

这个里程碑不是「写新功能」，而是把「发布到自己的源」变成**可重复的命令**，
并在链路的每一段都留一道能报红的闸：

| 段 | 谁管 | 判据 |
|---|---|---|
| 打包 + 生成索引 | `jishi 索引` / `jishi 发布` | `test_索引命令...`、`test_发布会合并...` |
| 部署（改写成同域） | 官网仓库 `build_index_mirror.py` | 那边 `tests/test_m146_mirror_checksum.py`（校验和必须与 zip 对得上） |
| 装 | `jishi 安装 <包> --index <地址>` | `test_从自建索引...` |
| 用 | `导入 <包> 从 本地包` | 同上（两个宿主各跑一遍，输出逐字比） |

⚠️ 这里**不联网**：用 `file://` 前缀当「自建源」。真源（`https://基石.cn/`）
的同一套流程由 `tools/deploy_push.sh` 部署，线上核验记在 `PROGRESS`/提交信息里。
"""

import json
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_m52_jishilib import RUST                        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
包目录 = ROOT / "examples" / "packages"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _base(dl: Path) -> str:
    """`file:///<绝对路径>` —— 当「自建源」用的前缀。

    ⚠️ 路径里带中文（`D:/code/基石/…`），所以交给 `encode_url` 去编码；
    这里只负责给出**未编码**的原始前缀（与真实源的 `--base-url` 用法一致）。
    """
    return "file:///" + str(dl).replace("\\", "/")


@pytest.fixture()
def 源目录(tmp_path):
    """造一个「自建源」目录：dl/ 放 zip、索引.json 放索引。"""
    dl = tmp_path / "下载"
    dl.mkdir()
    return tmp_path, dl


# ---------------------------------------------------------------------------
# 一、打包 + 生成索引：一条命令产出可托管的目录
# ---------------------------------------------------------------------------

def test_索引命令产出可托管的目录(源目录):
    """`jishi 索引` 的产物必须**自洽**：每条地址指向真 zip、校验和按 zip 算。

    这条以前没人管 —— 包目录是我**手工**改 JSON 拼出来的（顺序不对、
    还漏了「入口」字段）。命令化之后这些都不用手记。
    """
    from oracle.jishi import cli as CLI

    tmp, dl = 源目录
    idx = tmp / "索引.json"
    assert CLI.main(["索引", str(包目录), "--base-url", _base(dl),
                     "--zip-dir", str(dl), "--out-index", str(idx)]) == 0

    数据 = json.loads(idx.read_text(encoding="utf-8"))
    名们 = list(数据["包"])
    assert 名们 == sorted(名们), f"索引应按包名排序（稳定 diff）：{名们}"
    assert set(名们) == {"图片格式转换", "基础库", "数学工具"}

    from urllib.parse import unquote

    from oracle.jishi import packages as PKG

    # ⚠️ 前缀要按**编码后**的形态比：`tmp_path` 里带着用例名（中文），
    #    而 `encode_url` 会把整条地址里的非 ASCII 全百分号编码。
    前缀 = PKG.encode_url(_base(dl))
    for 名, 条 in 数据["包"].items():
        # 地址：百分号编码、指向上面的前缀
        assert 条["下载地址"].startswith(前缀), 条["下载地址"]
        条["下载地址"].encode("ascii")          # 不许有裸非 ASCII
        z = dl / unquote(条["下载地址"].rsplit("/", 1)[-1])
        assert z.is_file(), f"缺 zip：{z}"
        # 校验和必须按**这个 zip** 算 —— 部署脚本就是靠它拦错包的
        assert 条["校验和"] == _sha(z), f"{名} 的校验和与 zip 不符"

    # 「入口」要带进索引（图片格式转换 是单入口包）—— 手工那份漏了它
    assert 数据["包"]["图片格式转换"].get("入口") == "图片格式转换.jsh", \
        数据["包"]["图片格式转换"]
    # 依赖也要带（数学工具 依赖 基础库）
    assert 数据["包"]["数学工具"].get("依赖") == {"基础库": ">=1.0.0"}


def test_发布会合并而不是覆盖索引(tmp_path):
    """`jishi 发布 … --index` 是**增量**：后发的包不能把先发的挤掉。

    真实场景：加一个新包时不该重建整个索引（重建会丢掉别人刚发的包）。
    """
    from oracle.jishi import cli as CLI

    dl = tmp_path / "下载"
    dl.mkdir()
    idx = tmp_path / "索引.json"
    assert CLI.main(["发布", str(包目录 / "基础库"), "--out", str(dl),
                     "--base-url", _base(dl), "--index", str(idx)]) == 0
    第一 = json.loads(idx.read_text(encoding="utf-8"))["包"]
    assert set(第一) == {"基础库"}

    assert CLI.main(["发布", str(包目录 / "数学工具"), "--out", str(dl),
                     "--base-url", _base(dl), "--index", str(idx)]) == 0
    第二 = json.loads(idx.read_text(encoding="utf-8"))["包"]
    assert set(第二) == {"基础库", "数学工具"}, 第二
    assert 第二["基础库"] == 第一["基础库"], "先发的条目被改动了"


# ---------------------------------------------------------------------------
# 二、从「自建源」一条命令装上，两个宿主都能用
# ---------------------------------------------------------------------------

def test_从自建索引一条命令装上就能用(源目录, tmp_path, monkeypatch):
    """M146 的验收原话：**从自建索引装上就能 `导入 图片格式转换` 用**。

    走的是真命令链：`jishi 索引` → `jishi 安装 --index` → `导入` + 真跑。
    两个宿主各跑一遍（Rust 那份要现场编译，正好也验了 M145 的展开）。
    """
    from oracle.jishi import cli as CLI

    tmp, dl = 源目录
    idx = tmp / "索引.json"
    assert CLI.main(["索引", str(包目录), "--base-url", _base(dl),
                     "--zip-dir", str(dl), "--out-index", str(idx)]) == 0

    proj = tmp_path / "工程"
    proj.mkdir()
    monkeypatch.chdir(proj)
    assert CLI.main(["安装", "图片格式转换", "--index", str(idx)]) == 0
    assert (proj / ".jishi" / "packages" / "图片格式转换" / "包.json").is_file()

    源码 = ('导入 图片格式转换 从 本地包\n'
            '令 图 = 图片格式转换.空图(3, 2, [7, 8, 9])\n'
            '图片格式转换.设像素(图, 0, 0, [255, 0, 0])\n'
            '打印(图片格式转换.尺寸(图))\n'
            '打印(图片格式转换.取像素(图, 0, 0))\n')
    (proj / "用包.jsh").write_text(源码, encoding="utf-8", newline="\n")

    py = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "用包.jsh"],
                        cwd=str(proj), capture_output=True, text=True, encoding="utf-8")
    assert py.returncode == 0, py.stderr
    if not RUST.exists():
        pytest.skip("没有编出 jishi-rs")
    rs = subprocess.run([str(RUST), "用包.jsh"], cwd=str(proj),
                        capture_output=True, text=True, encoding="utf-8")
    assert rs.returncode == 0, rs.stderr
    assert py.stdout == rs.stdout, f"两宿主不一致\n{py.stdout}\n{rs.stdout}"
    assert py.stdout.splitlines() == ["{'宽': 3, '高': 2}", "[255, 0, 0]"], py.stdout
