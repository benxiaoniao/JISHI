# -*- coding: utf-8 -*-
"""「`导入 文本` 会遮蔽内建 `文本(...)`」这条提示：**五个引擎都要打，且逐字一致**。

## 这条判据是怎么来的（2026-10-26）

`tools/check_cli.py` 一直报两处 ✗：同一份输入，`jishi`（Python）的 stderr 有一行、
`jishi-rs` 的是空的。查下来是这条提示**只有 Python 宿主打了**。

判断结论：**这是真差异，不是「两种语言不同」** ——

- 提示守的是**同一个坑**：`导入 文本` 之后 `文本(123)` 就用不了了（模块不能直接调用），
  而且**两个宿主掉进同一个坑**。既然坑一样，提醒就该一样；
- 而且丢失它的是**产品那一侧**：发行包是 `jishi-rs` 单二进制，
  ⇒ 用户在正式版里**得不到**提示，只会撞上以后那句「模块不能直接调用」；
- 它确实不是「语言语义」（程序算出来的东西两边一样），
  所以 `tools/check_engines.py` 早就把这一行**当进程副产物剥掉**再对拍 ——
  但那解决的是「打几次」的不稳定，不是「有没有」的缺失。

⇒ 补齐 Rust 与 Node（文案、去重口径、别名豁免三条都对齐 Python），
本判据把三者钉在一起。

## 为什么全用子进程跑

去重是**进程级**的（一个进程里每个名字只打一次）。在 pytest 进程里连跑
五个引擎，第一个跑完就把名字记进「已提示」集合，后面全都不打 ——
那不是引擎的差别，是**夹具自己串了**。⇒ 一个宿主一个子进程，各算各的。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import serialize                        # noqa: E402
from oracle.jishi.compiler import compile_source          # noqa: E402

PY = sys.executable
RUST = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")
NODE = os.environ.get("JISHI_NODE", "node")
宿主们 = ["树遍历", "Python VM", "C VM", "Node", "Rust"]


def _临时(名: str, 后缀: str) -> Path:
    return Path(tempfile.gettempdir()) / f"m138_{os.getpid()}_{名}{后缀}"


def _跑(宿: str, 源: str, 名: str) -> tuple[int, str, str]:
    """在一个**全新进程**里跑这段程序，返回 (退出码, stdout, stderr)。"""
    文件 = _临时(名, ".jsh")
    文件.write_text(源, encoding="utf-8", newline="\n")
    if 宿 == "树遍历":
        命令 = [PY, "-m", "oracle.jishi.cli", str(文件)]
    elif 宿 == "Rust":
        命令 = [str(RUST), str(文件)]
    elif 宿 == "Node":
        字节 = _临时(名, ".json")
        字节.write_text(serialize.dumps(compile_source(源, str(文件))),
                        encoding="utf-8", newline="\n")
        命令 = [NODE, str(ROOT / "oracle" / "node" / "index.js"), str(字节)]
    elif 宿 == "Python VM":
        命令 = [PY, "-c",
                "import sys;from oracle.jishi.compiler import compile_source as c;"
                "from oracle.jishi import vm as v;"
                "p=sys.argv[1];s=open(p,encoding='utf-8').read();"
                "v.VM(c(s,p),p).run()", str(文件)]
    elif 宿 == "C VM":
        命令 = [PY, "-c",
                "import sys;from oracle.jishi import cvm_bind;"
                "p=sys.argv[1];cvm_bind.run_source_c(open(p,encoding='utf-8').read(),p)",
                str(文件)]
    else:                                                    # pragma: no cover
        raise AssertionError(宿)
    r = subprocess.run(命令, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return r.returncode, r.stdout, r.stderr


def _要构建(宿: str) -> None:
    if 宿 == "Rust" and not RUST.is_file():
        pytest.skip(f"没构建 {RUST.name}（cd rust && cargo build --release）")


#: 命中范围极小：模块名 ∩ 内建函数名 = {文本}（其余 29 个模块名都不撞）
遮蔽源 = "导入 文本\n打印(1)\n"
#: 用了别名就不遮蔽 ⇒ 不该提示
别名源 = "导入 文本 为 文字\n打印(1)\n"
#: 同一个名字导入两次：一个进程里只提醒一次
两次源 = "导入 文本\n导入 文本\n打印(1)\n"


@pytest.mark.parametrize("宿", 宿主们)
def test_每个引擎都会提醒(宿):
    _要构建(宿)
    码, 出, 错 = _跑(宿, 遮蔽源, f"hint_{宿}")
    assert 码 == 0, f"{宿}：\n{出}\n{错}"
    assert 出.strip() == "1", f"{宿} 的 stdout：{出!r}"
    assert "既是标准库模块名，又是内建函数名" in 错, f"{宿} 没提醒：{错!r}"


def test_五个引擎的提示逐字一致():
    """这条盯的就是「只有 Python 会提醒」那个窟窿。

    ⚠️ 只比**提示那一行**，不比整段 stderr：各宿主的退出/渲染细节本来就有
    既有分叉（见 `check_engines` 的「报错渲染」一类），这里要钉的是**提醒本身**。
    """
    if not RUST.is_file():
        pytest.skip(f"没构建 {RUST.name}（cd rust && cargo build --release）")
    行们 = {}
    for 宿 in 宿主们:
        码, 出, 错 = _跑(宿, 遮蔽源, f"same_{宿}")
        assert 码 == 0, 宿
        行们[宿] = [ln for ln in 错.splitlines() if "既是标准库模块名" in ln]
        assert len(行们[宿]) == 1, f"{宿} 该正好一行提示，实际 {行们[宿]}"
    基准 = 行们["树遍历"]
    for 宿 in 宿主们[1:]:
        assert 行们[宿] == 基准, f"{宿} 与树遍历的提示不一致：\n{行们[宿]}\n{基准}"


@pytest.mark.parametrize("宿", 宿主们)
def test_一个进程里只提醒一次(宿):
    _要构建(宿)
    码, 出, 错 = _跑(宿, 两次源, f"twice_{宿}")
    assert 码 == 0, f"{宿}：{错}"
    次数 = 错.count("既是标准库模块名")
    assert 次数 == 1, f"{宿} 提醒了 {次数} 次（该只提醒一次 —— 否则刷屏）：\n{错}"


@pytest.mark.parametrize("宿", 宿主们)
def test_用了别名就不提醒(宿):
    """`导入 文本 为 文字` 时内建没被遮蔽 —— 再提醒就是误导。"""
    _要构建(宿)
    码, 出, 错 = _跑(宿, 别名源, f"alias_{宿}")
    assert 码 == 0, f"{宿}：{错}"
    assert "既是标准库模块名" not in 错, f"{宿} 用了别名还提醒：{错!r}"


def test_提示要说清出路():
    """报错/警告必须**可操作**（核心二）：不只说「被遮蔽了」，还要给替代写法。"""
    码, 出, 错 = _跑("树遍历", 遮蔽源, "action")
    assert 码 == 0
    assert "别名导入" in 错, 错
    assert "导入 文本 为 别的名字" in 错, f"要给出可直接照抄的写法：{错}"


def test_提示不改语义_只用别名才不遮蔽():
    """提醒归提醒：**程序算出来的东西一个字都不变**（两边都还能正常打印）。"""
    码, 出, 错 = _跑("树遍历", 遮蔽源, "sem")
    assert (码, 出.strip()) == (0, "1")
    码2, 出2, 错2 = _跑("树遍历", 别名源, "sem2")
    assert (码2, 出2.strip(), 错2) == (0, "1", "")
