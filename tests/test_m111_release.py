# -*- coding: utf-8 -*-
"""M111：0.2.3 收口与发布 —— 版本号一致性 + 发版文档四块的锚点。

**为什么要有这一条**：升一次版本号要改到**六处 + 一个要重编的产物** ——

1. `pyproject.toml`（唯一来源）
2. `oracle/jishi/version.py` 的兜底常量 `__FALLBACK__`
3. `frontend/Cargo.toml`
4. `core/Cargo.toml`
5. **生成的** `rust/src/langdata.rs`（`tools/gen_langdata_rs.py` 的产物）
6. 两处测试里的写死期望（`test_m90_langspec_rust.py`）
7. ⚠️ **pyo3 扩展 `oracle/jishi/_core.pyd`** —— 按 `core/Cargo.toml` 编出来，
   **改了版本号要重编**（`python core/build.py`），否则 `--frontend rust` 还是报旧号。
   （这条的判据在 `test_m59_tokenizer_rust.py::test_扩展版本与包版本一致`，
   不在这里重复。）

**手工清点会漏**（M111 这次就是手工改的）。这里把它们钉在一起，漏改一处当场报红
—— 判据钉的是「它们彼此相等」，所以下次改版本时**任意一处**漏了都会红。

另一半盯**发版文档**：README 的四块（技术栈声明 / 性能数据 / 能力清单 / 项目结构）
是「发布全绿」的验收项，得有锚点，不能悄悄消失。
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "pyproject.toml 里找不到 version"
    return m.group(1)


def _rust() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _need_rust(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _rust().exists():
            pytest.skip("Rust 宿主未构建（cd rust && cargo build --release）")
        return fn(*args, **kwargs)
    return wrapper


def _rust_run(*argv: str) -> tuple[str, str, int]:
    r = subprocess.run([str(_rust()), *argv], capture_output=True, input=b"",
                       cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


# ---------------------------------------------------------------------------
# 一、版本号六处同源
# ---------------------------------------------------------------------------

def test_四处版本号一致():
    """`pyproject.toml` 是唯一来源，另外三处必须跟着它。"""
    want = _pyproject_version()

    verpy = (ROOT / "oracle" / "jishi" / "version.py").read_text(encoding="utf-8")
    m = re.search(r'__FALLBACK__\s*=\s*"([^"]+)"', verpy)
    assert m, "version.py 里找不到 __FALLBACK__"
    assert m.group(1) == want, f"version.py 的兜底常量是 {m.group(1)}，应是 {want}"

    for crate in ("frontend", "core"):
        text = (ROOT / crate / "Cargo.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        assert m, f"{crate}/Cargo.toml 里找不到 version"
        assert m.group(1) == want, f"{crate}/Cargo.toml 是 {m.group(1)}，应是 {want}"


def test_生成的langdata跟着来源走():
    """`rust/src/langdata.rs` 是 `tools/gen_langdata_rs.py` 的产物。

    它不进 `cargo` 的版本号体系（`--version` 读的就是这里），所以**最难发现**
    —— `pyproject` 改了、忘了重跑生成器，二进制就会一直报旧版本。
    """
    want = _pyproject_version()
    text = (ROOT / "rust" / "src" / "langdata.rs").read_text(encoding="utf-8")
    m = re.search(r'LANG_VERSION:\s*&str\s*=\s*"([^"]+)"', text)
    assert m, "langdata.rs 里找不到 LANG_VERSION"
    assert m.group(1) == want, (
        f"langdata.rs 是 {m.group(1)}，应是 {want} —— 重跑 "
        f"`python tools/gen_langdata_rs.py`")


def test_测试里的写死期望也跟着():
    """`test_m90` 里有两处写死的版本（语言规格 + 语言卡头），别漏。"""
    want = _pyproject_version()
    text = (ROOT / "tests" / "test_m90_langspec_rust.py").read_text(encoding="utf-8")
    assert f'assert d["version"] == "{want}"' in text, "test_m90 的语言规格期望没跟着改"
    assert f"语言卡 v{want}（" in text, "test_m90 的语言卡头期望没跟着改"


def test_库自己报的版本与pyproject一致():
    """`oracle.jishi.version.VERSION` 走 importlib.metadata（editable 装过才准）。

    打包成单文件时无元数据，回落到 `__FALLBACK__` —— 两条路都该等于 pyproject。
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from oracle.jishi.version import VERSION
    assert VERSION == _pyproject_version(), VERSION


# ---------------------------------------------------------------------------
# 二、二进制与规格自报（不依赖 Python）
# ---------------------------------------------------------------------------

@_need_rust
def test_二进制自报的版本一致():
    out, err, rc = _rust_run("--version")
    assert rc == 0, err
    assert _pyproject_version() in out, out.strip()


@_need_rust
def test_语言规格自报的版本一致():
    out, _, rc = _rust_run("--lang-spec")
    assert rc == 0
    d = json.loads(out)
    assert d["version"] == _pyproject_version(), d["version"]


@_need_rust
def test_语言卡头的版本一致():
    out, _, rc = _rust_run("--ai-card")
    assert rc == 0
    assert out.startswith(f"# 基石（jishi）语言卡 v{_pyproject_version()}（"), out[:80]


# ---------------------------------------------------------------------------
# 三、发版文档四块的锚点
# ---------------------------------------------------------------------------

def test_README四块的标题都在():
    """「发布全绿；`README.md` 四块同步」—— 四块各留一个不会被改名的标题。"""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for title in ("## 当前进度", "## 已经能做什么", "## 项目结构", "## 架构与数据流"):
        assert title in text, f"README 少了「{title}」这一块"


def test_README的技术栈声明说清了正式实现():
    """技术栈声明必须点明「正式实现是 Rust + C，Python / Node 已退役」。

    这是对外最要紧的一句 —— 说错了整份文档都不可信。
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "技术栈声明" in text
    assert "正式实现只有两套" in text
    assert "已退役" in text
    # 「架构与数据流」一节别再退回「前端正在换 Rust / 默认仍是 Python」
    assert "默认仍是 Python" not in text, "架构一节退回了「默认仍是 Python」"
    assert "前端正在换 Rust" not in text, "架构一节退回了「前端正在换 Rust」"


def test_README契约串在场():
    """三个字面串是**站点生成器的接口**，改 README 时不能拆掉
    （拆了站点数字会静默变空）。"""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"测试现状：\d+ 项", text), "少了「测试现状：N 项」"
    assert re.search(r"\d+ 项，\d+ 通过", text), "少了「N 项，N 通过」"
    assert "（当前 **" in text and "/50 可用**" in text, "少了能力探针的「当前 N/50 可用」"


def test_站点版本与语言版本一致():
    """站点是生成物：`site/index.html` 的「当前版本」应当等于语言版本。

    ⚠️ 站点**没重建**（改了 pyproject 却没跑 `build_site.py`）时这条会红 ——
    那正是它要抓的形状。
    """
    index = ROOT / "site" / "index.html"
    if not index.exists():
        pytest.skip("站点未生成")
    text = index.read_text(encoding="utf-8")
    want = _pyproject_version()
    assert f">{want}<" in text or f"版本 {want}" in text, (
        f"站点首页没有 {want} —— 重跑站点生成器")


# ---------------------------------------------------------------------------
# 四、工作流：跑仓库脚本的 job 必须先检出仓库
# ---------------------------------------------------------------------------

def _作业块(text: str) -> dict:
    """把工作流文本按 job 切开（**不引第三方 YAML 库** —— 本项目零第三方依赖）。"""
    lines = text.splitlines()
    i = next((k for k, l in enumerate(lines) if l.strip() == "jobs:"), None)
    if i is None:
        return {}
    块, 当前 = {}, None
    for l in lines[i + 1:]:
        if l.startswith("  ") and not l.startswith("   ") and l.rstrip().endswith(":"):
            当前 = l.strip()[:-1]
            块[当前] = []
        elif 当前 is not None:
            块[当前].append(l)
    return 块


def test_跑仓库脚本的job必须先检出仓库():
    """🔴 为什么有这一条（2026-10-14，M111 **第二次**发版失败）：

    `Release` 的 `release` job 里「生成语言本体版本清单」要跑
    `tools/build_version_manifest.py` —— 但那个 job **从来没 `actions/checkout`**。
    于是 Python 连脚本文件都打不开（`can't open file …` ⇒ **退出码 2**）：
    **三个平台的包都构建好了，最后发布这一步 5 秒就挂。**

    0.2.2 时这一步还不存在（release job 只要「下载产物 + shell 拼校验和」），
    所以一直没暴露 —— **0.2.3 是它第一次真跑**。

    ⇒ 判据：`run:` 里出现 `tools/` 的步骤（**注释行不算**），
    它所在的 job 必须有一个 `actions/checkout` 排在前头。
    """
    import re
    坏 = []
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for jn, lines in _作业块(wf.read_text(encoding="utf-8")).items():
            代码 = [l for l in lines if not l.lstrip().startswith("#")]
            i_checkout = next(
                (k for k, l in enumerate(代码) if "actions/checkout" in l), None)
            for k, l in enumerate(代码):
                if not re.search(r"\btools/", l):
                    continue
                if i_checkout is None or i_checkout > k:
                    坏.append(f"{wf.name} · job「{jn}」· 第 {k} 行跑了 tools/ 里的东西，"
                              f"但该 job 没有先检出仓库")
    assert not 坏, ("这些 job 会在干净 runner 上挂（脚本文件根本不在）：\n  - "
                    + "\n  - ".join(坏))


# ---------------------------------------------------------------------------
# 五、发布资产名必须是纯 ASCII
# ---------------------------------------------------------------------------

def test_发布资产名必须是纯ASCII():
    """🔴 为什么有这一条（2026-10-14 发布 v0.2.3 时踩到）：

    版本清单本来叫 `版本.json` —— 上传后 **GitHub 把它改写成 `default.json`**，
    原名只留在资产的 `label` 字段里（`softprops/action-gh-release` 的源码里就写着
    「GitHub can rewrite uploaded asset names」，并专门做了「把原名恢复进 label」这步）。
    后果很隐蔽：**自建源照常能取到清单**，只有走 **GitHub 兜底**时才 404 ——
    本地测不出来、站点上也看不出来。

    ⇒ 判据：**发布资产名一律纯 ASCII**。这里钉住清单文件名 —— Rust 侧的两个默认地址、
    以及镜像同步脚本认的那个资产名。
    """
    import re

    rust = (ROOT / "rust" / "src" / "selfupdate.rs").read_text(encoding="utf-8")
    块 = re.search(r"DEFAULT_RELEASE_INDEX_URLS[^=]*=\s*\[(.*?)\];", rust, re.S)
    assert 块, "找不到 DEFAULT_RELEASE_INDEX_URLS"
    地址 = re.findall(r'"(https?://[^"]+)"', 块.group(1))
    assert len(地址) == 2, 地址
    for u in 地址:
        名 = u.rsplit("/", 1)[-1]
        assert 名.isascii(), (
            f"发布资产名「{名}」不是纯 ASCII —— GitHub 会把它改写成 `default.json`"
            "（原名只留在 label 里），走 GitHub 那条就永远 404")

    mirror = (ROOT / "tools" / "fetch_release_mirror.py").read_text(encoding="utf-8")
    for 名 in re.findall(r'a\["name"\]\s*==\s*"([^"]+)"', mirror):
        assert 名.isascii(), f"镜像同步脚本在按非 ASCII 名找资产：「{名}」"
