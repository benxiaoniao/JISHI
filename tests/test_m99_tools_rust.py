# -*- coding: utf-8 -*-
r"""R8.4（前置）：`医生` / `新项目` / `教程` 搬进 Rust 宿主。

## 为什么这三件必须先搬

发行包已经改走 **cargo 单二进制**（R8.3），而这三个命令以前**只有 Python 版有** ——
不补就换包 = **用户可见的功能倒退**（`jishi 医生` 正是安装说明里让用户跑的
第一个命令）。

## 对齐口径（本文件按这两条分节）

| 命令 | 口径 |
|---|---|
| `新项目` | **逐字对拍**：stdout / stderr / 退码，外加**产物文件逐字节**（含 Windows 上的 `\r\n`） |
| `教程` | **逐字对拍**：stdout / 退码 |
| `医生` | 🔴 **有意重写，不对拍** —— 见下 |

### `医生` 为什么不能照搬

Python 那版报的是「Python：3.13」「C VM：已加载」「MCP Server：可用」——
**单二进制世界里这三样根本不存在**。照搬等于给用户看三条**假的检查项**。
所以它改成报这个二进制真正该关心的事（版本 / 可执行文件 / PATH / 包目录 / 终端 /
索引源），判据也换成**独立判据**（写死期望、不 import oracle.jishi）。

⚠️ 一条容易漏的：诊断命令**不许有副作用**。第一版用了 `packages::ensure_root()`
把 `.jishi/packages` 建在了**当前目录** —— 判据里专门钉了这条。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")
TMP_SUFFIX = f"{os.getpid()}"

need_rust = pytest.mark.skipif(
    not EXE.is_file(),
    reason="缺 rust/target/release/jishi-rs（跑 `cd rust && cargo build --release`）"
           "—— **跳过而不是替人构建**")


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    return env


def _py(args: list[str], cwd: Path) -> tuple[int, bytes, bytes]:
    """跑 Python 版：`python -m oracle.jishi.cli <args>`。"""
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", *args],
                       capture_output=True, cwd=str(cwd), env=_env(), timeout=120)
    return r.returncode, r.stdout, r.stderr


def _rs(args: list[str], cwd: Path) -> tuple[int, bytes, bytes]:
    """跑 Rust 版。"""
    r = subprocess.run([str(EXE), *args],
                       capture_output=True, cwd=str(cwd), env=_env(), timeout=120)
    return r.returncode, r.stdout, r.stderr


def _both(args: list[str], tmp: Path, name: str) -> str:
    """同一个工作目录里先跑 Python 再跑 Rust（**清理两次运行之间产生的文件**）。

    ⚠️ 必须**同一个 cwd**：`新项目` 会把绝对路径打进屏幕，各跑各的目录就永远不等。
    """
    work = tmp / name
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir(parents=True)
    return str(work)


# ---------------------------------------------------------------------------
# 一、对拍：`教程` / `新项目`
# ---------------------------------------------------------------------------

@need_rust
def test_对拍_教程(tmp_path):
    """10 章教程索引：stdout / stderr / 退码**逐字节一致**。"""
    work = _both(["教程"], tmp_path, "教程")
    p = _py(["教程"], Path(work))
    r = _rs(["教程"], Path(work))
    assert r == p, f"退码 py={p[0]} rs={r[0]}\n--- py ---\n{p[1].decode('utf-8')}\n--- rs ---\n{r[1].decode('utf-8')}"


@need_rust
def test_对拍_新项目_含产物逐字节(tmp_path):
    """脚手架：**屏幕输出 + 两个产物文件**都要逐字节一致。

    ⚠️ 产物里钉的是「按 Python 的文本模式写」—— Windows 上 `\n` 要变成 `\r\n`。
    Rust 的 `fs::write` 是原样字节，差一个 `\\r` 就不一致，而**只有逐字节比才发现得了**。
    """
    work = Path(_both(["新项目", "我的项目"], tmp_path, "np"))

    # 先 Python：跑完把产物挪开，再跑 Rust（同 cwd，屏幕上的绝对路径才可比）
    pc, po, pe = _py(["新项目", "我的项目"], work)
    (work / "我的项目").rename(work / "_py产物")
    rc, ro, re_ = _rs(["新项目", "我的项目"], work)

    assert (rc, ro, re_) == (pc, po, pe), (
        f"退码 py={pc} rs={rc}\n--- py stdout ---\n{po.decode('utf-8')}"
        f"\n--- rs stdout ---\n{ro.decode('utf-8')}")

    names = sorted(p.name for p in (work / "_py产物").iterdir())
    assert names == ["hello.jsh", "说明.md"], names
    for n in names:
        a = (work / "_py产物" / n).read_bytes()
        b = (work / "我的项目" / n).read_bytes()
        assert a == b, f"{n} 逐字节不同：\npy={a!r}\nrs={b!r}"


@need_rust
def test_对拍_新项目_目录已存在(tmp_path):
    """重名：**不许覆盖**，stderr 同一句话 + 退码 1。"""
    work = Path(_both(["新项目", "甲"], tmp_path, "dup"))
    _py(["新项目", "甲"], work)          # 先让 Python 建出来
    p = _py(["新项目", "甲"], work)
    r = _rs(["新项目", "甲"], work)
    assert p[0] == r[0] == 1, (p[0], r[0])
    assert r[2] == p[2], f"stderr 不同：\npy={p[2]!r}\nrs={r[2]!r}"
    assert "已存在" in r[2].decode("utf-8")


@need_rust
def test_对拍_新项目_缺参数退码2(tmp_path):
    """缺参数：Python 走 argparse（英文 usage），宿主给一句中文 —— **只对齐退码 2**。"""
    work = Path(_both(["新项目"], tmp_path, "noarg"))
    p = _py(["新项目"], work)
    r = _rs(["新项目"], work)
    assert p[0] == r[0] == 2, (p[0], r[0])


# ---------------------------------------------------------------------------
# 二、独立判据：`医生`（有意重写，不 import oracle.jishi、不与 Python 比）
# ---------------------------------------------------------------------------

@need_rust
def test_独立_医生_单二进制该查的东西(tmp_path):
    """写死期望：查的是**单二进制真正依赖的东西**，不是 Python 那套。"""
    work = tmp_path / f"doc_{TMP_SUFFIX}"
    work.mkdir()
    rc, out, err = _rs(["医生", "--no-net"], work)
    text = out.decode("utf-8")

    assert rc == 0, f"退码 {rc}\n{text}"
    for want in ("== 基石环境自检 ==", "版本：", "引擎：Rust 单二进制",
                 "可执行文件：", "安装位置：", "PATH：", "包目录（搜索顺序）：",
                 "标准输出：", "包索引：跳过联网检查"):
        assert want in text, f"少了「{want}」：\n{text}"

    # 🔴 这两条是**这次重写的理由**：单二进制里没有 Python、没有 C VM
    assert "Python：" not in text, "医生不该再报 Python 版本（单二进制里没有它）"
    assert "C VM" not in text, "医生不该再报 C VM（单二进制里没有它）"


@need_rust
def test_独立_医生_不许有副作用(tmp_path):
    """诊断命令**只读**。

    ⚠️ 第一版用 `packages::ensure_root()` 报「用户目录」，那会在**当前目录**
    建出 `.jishi/packages/` —— 跑一次自检就往人家项目里塞东西，不能接受。
    """
    work = tmp_path / f"side_{TMP_SUFFIX}"
    work.mkdir()
    _rs(["医生", "--no-net"], work)
    leftovers = sorted(p.name for p in work.iterdir())
    assert leftovers == [], f"`医生` 在工作目录里留下了东西：{leftovers}"
