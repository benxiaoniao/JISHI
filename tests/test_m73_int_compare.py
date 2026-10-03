# -*- coding: utf-8 -*-
"""R6 主体 · 整数循环：全局变量表数组化 + 整数比较走 i128。

## 两件事

### 一、全局变量从 `HashMap<String, Val>` 改成**按名字下标索引的数组**

⚠️ C VM **一直**是 `JVal *globals;  /* 按名字下标索引 */`（`cvm/src/jsvm.c`），
Python 侧也是「按 names 顺序把内建铺成数组」再交给 C —— **只有 Rust 侧
用 HashMap**，而且每次 `LOAD_GLOBAL` 还先 `names[a].clone()`（**分配一个
String**）再哈希查找。

量出来的差距（n=500000 的 `当` 循环，交错 A/B）：

```
                    改前        改后
模块级变量          190.7ms     55.8ms      3.42x
全局读写（无算术）   173.9ms     74.8ms      2.32x
```

而且改前「同样的逻辑放模块级」比「放函数内（局部变量）」**慢一倍多**
（198 vs 91ms），而 C VM 两者一样（26 vs 28ms）—— 差的正是这一处。

内建在加载时一次性铺进去（`init_globals`），未定义的名字留 `None`
（读它报「找不到名字」，候选名提示的池子仍是「已定义的名字」）。

### 二、整数比较：**不能再转 f64**

🔴 这条同时是**正确性修复**。原来的路径是「两边都 `as f64` 再比」，
超过 2^53 就开始失真：

```
2 ** 62 > 2 ** 62 - 1     →  另外四个执行器：真     Rust：假   ✗
```

语料里没有任何大整数比较（`tests/cases/` 里连集合语料都刚补上），
所以它一直是绿的。现在整数对整数**直接用 `i128` 比**。

⚠️ 注意 `==` / `!=` 本来就精确（走 `Val::eq`），出问题的只有 `< > <= >=` 四支。
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi import cvm_bind                                        # noqa: E402
from jishi.compiler import compile_source, compile_to_json        # noqa: E402
from jishi.interpreter import run_source as run_tree              # noqa: E402
from jishi.vm import VM                                           # noqa: E402

NODE = shutil.which("node")
HAS_NODE = NODE is not None


def _rust_exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


RUST = _rust_exe()
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")


def _five(src: str) -> dict[str, str]:
    """五个执行器各跑一遍，返回 `{执行器: stdout 或完整报错渲染}`。

    ⚠️ 报错取**完整渲染**（不只首行）—— 「找不到名字」的**候选提示**在后面的
    行上，只比首行就把要测的东西截掉了。
    """
    out = {}
    for kind, fn in (("tree", lambda: run_tree(src, "x.jsh")),
                     ("PyVM", lambda: VM(compile_source(src, "x.jsh"), "x.jsh").run()),
                     ("CVM", lambda: cvm_bind.run_source_c(src, "x.jsh"))):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                fn()
            out[kind] = buf.getvalue().rstrip("\n")
        except BaseException as e:                                # noqa: BLE001
            out[kind] = ("【错】" + str(e)).rstrip("\n")
    tmp = Path(tempfile.gettempdir()) / f"_m73_{os.getpid()}.json"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    if RUST.exists():
        r = subprocess.run([str(RUST), str(tmp)], capture_output=True, input=b"")
        out["Rust"] = r.stdout.decode("utf-8", "replace").rstrip("\n") or \
            ("【错】" + r.stderr.decode("utf-8", "replace").rstrip("\n"))
    if HAS_NODE:
        r = subprocess.run([NODE, "node/index.js", str(tmp)],
                           capture_output=True, input=b"")
        out["Node"] = r.stdout.decode("utf-8", "replace").rstrip("\n") or \
            ("【错】" + r.stderr.decode("utf-8", "replace").rstrip("\n"))
    return out


#: 每一组都要五执行器逐字一致。
_CASES = {
    "2^53 边界": '打印(2 ** 53 + 1 > 2 ** 53)\n打印(2 ** 53 + 1 == 2 ** 53)\n',
    "2^62 边界": '打印(2 ** 62 > 2 ** 62 - 1)\n打印(2 ** 62 + 1 > 2 ** 62)\n打印(2 ** 62 >= 2 ** 62)\n',
    "大字面量": '令 甲 = 9007199254740993\n令 乙 = 9007199254740992\n打印(甲 > 乙, 甲 == 乙)\n',
    "四个比较运算": '令 甲 = 2 ** 62\n令 乙 = 甲 - 1\n打印(甲 > 乙, 甲 < 乙, 甲 >= 乙, 甲 <= 乙)\n',
    "混小数": '打印(1 < 1.5, 2.0 == 2, 3 > 2.5)\n',
    "全局读写": '令 甲 = 1\n甲 = 甲 + 1\n令 乙 = 甲\n打印(甲, 乙)\n',
    "全局同名遮蔽内建": '打印(长度([1, 2]))\n令 长度 = 5\n打印(长度)\n',
    "函数内读全局": '令 全局甲 = 7\n函数 取()：\n    返回 全局甲 + 1\n打印(取())\n',
    "函数内写全局": '令 全局乙 = 1\n函数 加()：\n    全局乙 = 全局乙 + 10\n加()\n打印(全局乙)\n',
}


@pytest.mark.parametrize("tag", sorted(_CASES))
def test_整数与全局语义五执行器逐字一致(tag):
    outs = _five(_CASES[tag])
    base_name, base = next(iter(outs.items()))
    for name, got in outs.items():
        assert got == base, (
            f"{tag}：{name} 与 {base_name} 不一致\n"
            + "\n".join(f"  {k:5} {v!r}" for k, v in outs.items()))


def test_大整数比较是精确的():
    """超过 2^53 的整数比较**不能**转成 f64 再比。

    🔴 这一条钉住一个既有 bug：Rust 的 `compare` 原来把所有数值比较都走
    `to_num_pair`（两边 `as f64`），于是 `2 ** 62 > 2 ** 62 - 1` 给「假」，
    而另外四个执行器都精确比较。语料里从没有大整数比较，所以一直没被看见。
    """
    outs = _five('打印(2 ** 62 > 2 ** 62 - 1)\n打印(2 ** 53 + 1 > 2 ** 53)\n'
                 '打印(9007199254740993 == 9007199254740992)\n')
    for name, out in outs.items():
        assert out.strip() == "真\n真\n假", f"{name}: {out!r}"


def test_未定义全局的报错与其它执行器一致():
    """`globals` 改成数组之后，报错路径（「找不到名字」+ 候选提示）也要一致。

    ⚠️ **两种形状都要测**，它们走的不是同一条路：

    * `打印(长渡([1]))` —— 要提示「长度」，而**「长度」根本没出现在本文件里**
      （`names` 只有 `['打印', '长渡']`）→ 候选池必须包含**全部内建**；
    * `令 数据 = 1\\n打印(打应(数据))` —— 还要把**用户定义过的全局**并进池子。

    🔴 我第一版只拿了「`names` 里已定义的槽位」当池子，于是**第一种形状的提示
    直接没了**。自建对拍当时只用了第二种形状（它恰好把「长度」放进了 `names`），
    没抓住 —— 是全量回归里的 `test_m51_consistency.py::test_M56_宿主也给候选名`
    抓出来的。**用例要挑「会让口径分叉的数据」**（本项目的 M51 老教训）。
    """
    for src in ('打印(长渡([1]))\n', '令 数据 = 1\n打印(打应(数据))\n'):
        outs = _five(src)
        base = outs["tree"]
        assert "你是不是想写" in base, (src, base)
        for name, got in outs.items():
            assert got == base, f"{src!r} 的 {name}: {got!r} != {base!r}"


@need_rust
def test_模块级循环不是灾难性慢():
    """模块级变量的读写是**一次数组索引**（不是 HashMap + String 分配）。

    ⚠️ 粗判据（进程启动 ~85ms，n=500000 若还是老的 HashMap 路径约 190ms，
    新的数组路径约 56ms）：给个宽上限 3 秒，抓的是「退回 O(n²) 或整条路变慢」。
    精确的倍数看 `build/_ab_bench.py` 那种**交错 A/B**，不在单元测试里做。
    """
    tmp = Path(tempfile.gettempdir()) / "_m73_loop.json"
    src = ("令 i = 500000\n当 i > 0：\n    i = i - 1\n打印(i)\n")
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    t0 = time.perf_counter()
    r = subprocess.run([str(RUST), str(tmp)], capture_output=True, input=b"")
    dt = time.perf_counter() - t0
    assert r.stdout.decode("utf-8", "replace").strip() == "0", \
        r.stderr.decode("utf-8", "replace")[:200]
    assert dt < 3.0, f"n=500000 的模块级当循环花了 {dt:.1f}s"


def test_语料里有整数比较():
    """`tests/cases/` 必须有「超过 2^53 的整数比较」语料（判据要覆盖到）。"""
    cases = list((ROOT / "tests" / "cases").glob("*整数比较*.jsh"))
    assert cases, "tests/cases/ 里没有整数比较语料"
    text = cases[0].read_text(encoding="utf-8")
    assert "2 ** 62" in text, "语料没覆盖到 2^62（2^53 以上）"
