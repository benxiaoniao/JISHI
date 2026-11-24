# -*- coding: utf-8 -*-
"""M227：Node 退役后的「功能回填核查」—— 用一份**快照**证明能力一格没少。

## 为什么要有这条

「删了 16.6k 行」听着像减法，但用户对这次退役的硬要求是「**千万别影响到语言本身
的功能**」。光说「测试全绿」不够：测试可能恰好没覆盖到被删掉的那一块。

所以做法是**退役前后各拍一张照**：

1. 退役**之前**，从 `oracle/node/` 跑一次 `--dump-stdlib`，把「Node 当年暴露的
   模块 × 函数」固化成 `tests/data/node_stdlib_snapshot.json`（纯数据）；
2. 退役**之后**，拿它与今天的 `jishi --dump-stdlib` 逐模块逐函数对账。

两边都是 **52 模块 / 601 函数** —— 这就是「一格没少」的硬证据。

📌 **M229 起这条判据多了一件事**：新加的函数必须**在这里签字**（`新增` 那张表）。
这样它同时管两头 —— 「一格没少」和「没悄悄多出来」。

⚠️ 快照是**纯数据**，判据只读它（不依赖 git 历史、不依赖 node）—— 符合
AGENTS 的红线「判据只许依赖被 git 跟踪的文件」。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUST = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if __import__("os").name == "nt" else "jishi-rs")
快照 = ROOT / "tests" / "data" / "node_stdlib_snapshot.json"


def _今天() -> dict[str, list[str]]:
    """今天的标准库清单（`jishi-rs --dump-stdlib`）。"""
    r = subprocess.run([str(RUST), "--dump-stdlib"], capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:400]
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def 今天() -> dict[str, list[str]]:
    if not RUST.is_file():
        pytest.skip("没编 jishi-rs（先 cargo build --release）")
    return _今天()


def test_Node快照里的模块一个不少(今天):
    """Node 当年有的**模块**，今天一个都不许少。"""
    旧 = json.loads(快照.read_text(encoding="utf-8"))
    缺 = [m for m in 旧 if m not in 今天]
    assert not 缺, f"这些模块随 Node 一起消失了：{缺}"


def test_Node快照里的函数一个不少(今天):
    """逐模块逐函数对账 —— 这条是「功能回填」的主判据。"""
    旧 = json.loads(快照.read_text(encoding="utf-8"))
    缺: dict[str, list[str]] = {}
    for 模块, 函数们 in 旧.items():
        少 = [f for f in 函数们 if f not in 今天.get(模块, [])]
        if 少:
            缺[模块] = 少
    assert not 缺, f"这些函数在 Node 退役后没了落点：{缺}"


#: Node 退役**之后**新增的函数 —— 加一个就要在这里签个字。
#:
#: ⚠️ 为什么要有这张表：`test_Node快照里的函数一个不少` 只管「不许少」，
#: 少了会红；但「悄悄多出来一个谁也不知道的函数」它管不着。加了这张表，
#: 函数数才是**可解释的**（今天的 = Node 时代 + 这里登记的）。
新增: dict[str, list[str]] = {
    # M229：桌面营造要的「Qt 式控页」
    "浏览器": ["有元素", "取们", "取属性", "设属性", "焦点"],
    "窗口": ["送宿主"],
    # M230：LSP / DAP 的字节级读
    "进程": ["读块", "读可用"],
}


def test_总数与三处口径一致(今天):
    """52 模块 / 601 + 登记的新增 函数 —— 与 `lang-spec` 和 Python 侧三处对得上。

    ⚠️ 数字**写死**是有意的：退役不该改能力表，改了就得有人在这里签字。
    """
    旧 = json.loads(快照.read_text(encoding="utf-8"))
    assert len(今天) == 52, f"模块数变了：{len(今天)}"
    应得 = 601 + sum(len(v) for v in 新增.values())
    assert sum(len(v) for v in 今天.values()) == 应得, \
        f"函数数变了：{sum(len(v) for v in 今天.values())}（应为 {应得}；" \
        f"加了函数就往 `新增` 里登记一笔）"
    # 模块集合不许变
    assert set(今天) == set(旧), "模块集合与 Node 时代不同"
    # 每个模块 = Node 时代的函数 ∪ 该模块登记的新增（**多一个少一个都红**）
    for 模块 in 旧:
        该新增 = set(新增.get(模块, []))
        assert sorted(今天[模块]) == sorted(set(旧[模块]) | 该新增), \
            f"`{模块}` 的函数与「Node 时代 + 登记的新增」对不上：\n" \
            f"  多出来的：{sorted(set(今天[模块]) - set(旧[模块]) - 该新增)}\n" \
            f"  少了　　：{sorted(set(旧[模块]) - set(今天[模块]))}"


def test_Python侧也还在():
    """Python 是判据基准：它的 `__all__` 不许因为删 Node 而缩水。

    ⚠️ 只看**函数名**（`def` 与 `__all__`），不跑代码 —— 这是静态对账。
    """
    import importlib

    旧 = json.loads(快照.read_text(encoding="utf-8"))
    缺: dict[str, list[str]] = {}
    for 模块, 函数们 in 旧.items():
        try:
            m = importlib.import_module(f"oracle.jishi.stdlib.{模块}")
        except ImportError:
            continue                       # 内嵌库（`数据库`）在 stdlib-jishi，另算
        有 = set(getattr(m, "__all__", [])) | {
            n for n in dir(m) if not n.startswith("_") and callable(getattr(m, n, None))
        }
        少 = [f for f in 函数们 if f not in 有]
        if 少:
            缺[模块] = 少
    assert not 缺, f"Python 侧缺了这些（Node 走之前是有的）：{缺}"


def test_Node走得最深的三块都有正式实现():
    """`进程` / `网络.获取流` / `HTTP服务端.启动非阻塞` —— Node 侧是三套 Worker 才撑住的，
    退役后必须由 Rust 与 Python 双双接住（不然就是「功能真丢了」）。"""
    # ⚠️ 模块不都在 `stdlib/` 下：`网络` 的主体在 `rust/src/http.rs`（`stdlib` 那里只有壳）
    rs = "\n".join(p.read_text(encoding="utf-8")
                   for p in (ROOT / "rust" / "src").rglob("*.rs"))
    # 📌 `进程.读块`（字节级读）是 M230 才加的 —— 这里只钉**退役时已经有的**
    for 名 in ("启动", "读行", "读错误行", "等待", "终止", "退出码",
               "启动非阻塞", "获取流", "SSE"):
        assert f'"{名}" =>' in rs, f"Rust 侧缺 `{名}`"

    py = (ROOT / "oracle" / "jishi" / "stdlib")
    for 文件, 名们 in (
        ("进程.py", ("读行", "写行", "在运行", "终止", "等待", "退出码")),
        ("网络.py", ("获取流", "SSE")),
        ("HTTP服务端.py", ("启动非阻塞", "处理一轮")),
    ):
        文 = (py / 文件).read_text(encoding="utf-8")
        for 名 in 名们:
            assert f"def {名}(" in 文 or f'"{名}"' in 文, f"Python 侧 `{文件}` 缺 `{名}`"
