# -*- coding: utf-8 -*-
"""R6 主体 · 字典 O(n²) → O(n)（有序哈希表）+ 顺带修掉两个跨语言分叉。

## 为什么动字典

R6.0 的度量里「字典写入密集」是 Rust VM **唯一落后树遍历**的场景（0.30x），
根因是 `JDict = Vec<(Val,Val)>` **线性查找** —— 写入是 O(n²)。
改成「Vec 保序 + HashMap 索引」之后查找 O(1)：`n` 到 16000 也瞬间完成。

⚠️ 为什么不是直接 `HashMap<Val,_>`：`Val` 的 `==` 是**跨类型数值相等**
（`1 == 1.0 == 精确("1")` 都是真），而浮点 / 精确小数 / 整数的哈希天然不同，
直接给 `Val` 实现 `Hash` 会「哈希与相等不一致」。所以哈希键单独规范化成
`DictKey`（整数 / 文本 / 布尔 / 空；其它键回退线性）。

## 顺带撞出 / 修掉的两个分叉（都在本文件钉住）

1. **Rust 字典 `==` 是顺序相关的**：旧实现直接比 `Vec<(Val,Val)>`，
   `{1:2,3:4} == {3:4,1:2}` 在 Rust 给 `假`，其它四个执行器给 `真`。
   改成**顺序无关**（对齐 Python 的 dict `==`）。
2. **Node 字典把 `1` 和 `1.0` 当两个键**：原生 `Map` 用 SameValueZero
   （按**对象身份**），`FloatBox(1.0)` 和 `1` 是两个键 —— `字[1]=10; 字[1.0]=99`
   会变成 3 个键。改成先按 valueEq 找键。
"""

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import cvm_bind                                        # noqa: E402
from oracle.jishi.compiler import compile_source, compile_to_json        # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree              # noqa: E402
from oracle.jishi.vm import VM                                           # noqa: E402

NODE = shutil.which("node")
HAS_NODE = NODE is not None
need_node = pytest.mark.skipif(not HAS_NODE, reason="本机无 Node.js")


def _rust_exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


RUST = _rust_exe()
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")


def _five(src: str) -> dict[str, str]:
    """五个执行器各跑一遍，返回 `{执行器: stdout}`。"""
    out = {}
    for kind, fn in (("tree", lambda: run_tree(src, "x.jsh")),
                     ("PyVM", lambda: VM(compile_source(src, "x.jsh"), "x.jsh").run()),
                     ("CVM", lambda: cvm_bind.run_source_c(src, "x.jsh"))):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                fn()
            out[kind] = buf.getvalue()
        except BaseException as e:                                # noqa: BLE001
            out[kind] = "【错】" + str(e).splitlines()[0]
    tmp = Path(tempfile.gettempdir()) / f"_m71_{os.getpid()}.json"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    if RUST.exists():
        r = subprocess.run([str(RUST), "--load-bytecode", str(tmp)], capture_output=True, input=b"")
        out["Rust"] = r.stdout.decode("utf-8", "replace") or \
            ("【错】" + r.stderr.decode("utf-8", "replace").splitlines()[0])
    if HAS_NODE:
        r = subprocess.run([NODE, "oracle/node/index.js", str(tmp)],
                           capture_output=True, input=b"")
        out["Node"] = r.stdout.decode("utf-8", "replace") or \
            ("【错】" + r.stderr.decode("utf-8", "replace").splitlines()[0])
    return out


#: 每一组都是「字典重构的易错点」，五执行器必须逐字一致。
_CASES = {
    "跨类型数值键是同一键": '令 字 = {}\n字[1] = 10\n字[1.0] = 99\n字[2] = 20\n打印(字)\n打印(长度(字))\n',
    "顺序无关相等": '打印({1: 2, 3: 4} == {3: 4, 1: 2})\n打印({1: 2} == {1: 3})\n打印({1: 2} == {1: 2, 3: 4})\n',
    "删除后重排仍一致": '令 字 = {}\n字[1] = 1\n字[2] = 2\n字[3] = 3\n打印(字.弹出(2))\n字[3] = 33\n打印(字)\n字[4] = 4\n打印(字)\n',
    "删除后查原键报错": '令 字 = {"甲": 1, "乙": 2}\n字.弹出("甲")\n尝试：\n    打印(字["甲"])\n捕获 异常 为 错：\n    打印(错.类型)\n',
    "更新跨类型键": '令 字 = {1: 1}\n字.更新({1.0: 99, 2: 2})\n打印(字)\n打印(长度(字))\n',
    "合并跨类型键": '令 字 = {1: 1}\n令 新 = 字.合并({1.0: 99})\n打印(字)\n打印(新)\n',
    "包含跨类型键": '令 字 = {1: "甲"}\n打印(字.包含(1))\n打印(字.包含(1.0))\n打印(字.包含(2))\n',
    "大字典写入": '令 字 = {}\n遍历 i 在 范围(3000)：\n    字[i] = i * 2\n打印(长度(字))\n打印(字[0])\n打印(字[2999])\n',
}


@pytest.mark.parametrize("tag", sorted(_CASES))
def test_字典语义五执行器逐字一致(tag):
    outs = _five(_CASES[tag])
    base_name, base = next(iter(outs.items()))
    for name, got in outs.items():
        assert got == base, (
            f"{tag}：{name} 与 {base_name} 不一致\n"
            + "\n".join(f"  {k:5} {v!r}" for k, v in outs.items()))


def test_跨类型数值键是同一键():
    """`1` 和 `1.0` 在字典里是**同一个键**（Python 语义：`1 == 1.0` 且哈希一致）。

    ⚠️ 这是 Node 的原生 `Map` 曾经错的点（SameValueZero 按对象身份）——
    `字[1]=10; 字[1.0]=99` 会变成 3 个键。Rust 侧的 `DictKey` 也靠「整数浮点
    归一化到 Int」来保证这一点。
    """
    outs = _five(_CASES["跨类型数值键是同一键"])
    for name, out in outs.items():
        assert out.strip() == "{1: 99, 2: 20}\n2", f"{name}: {out!r}"


def test_字典相等与顺序无关():
    """`{1:2,3:4} == {3:4,1:2}` 为真 —— 对齐 Python dict 的 `==`。

    ⚠️ Rust 旧实现直接比 `Vec`（顺序相关），这个形状会给出 `假`。
    """
    outs = _five(_CASES["顺序无关相等"])
    for name, out in outs.items():
        assert out.strip() == "真\n假\n假", f"{name}: {out!r}"


@need_rust
@pytest.mark.计时
def test_大字典写入是线性():
    """n 翻倍耗时别翻四倍（O(n²) 的红线）。用 Rust 二进制跑两个规模比墙钟。

    ⚠️ 只做「别太慢」的粗判据：进程启动 ~98ms，n=50000 的写入若还是 O(n²)
    会到几秒，O(n) 则 <100ms。所以判据是「n=50000 一次跑完 < 3 秒」。
    """
    tmp = Path(tempfile.gettempdir()) / f"{os.getpid()}_m71_big.json"
    src = "令 字 = {}\n遍历 i 在 范围(50000)：\n    字[i] = i\n打印(长度(字))\n"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    import time
    t0 = time.perf_counter()
    r = subprocess.run([str(RUST), "--load-bytecode", str(tmp)], capture_output=True, input=b"")
    dt = time.perf_counter() - t0
    assert r.stdout.decode("utf-8", "replace").strip() == "50000", r.stderr.decode("utf-8", "replace")[:200]
    assert dt < 3.0, f"n=50000 的字典写入花了 {dt:.1f}s —— 像还是 O(n²)"
