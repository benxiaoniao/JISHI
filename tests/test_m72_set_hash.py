# -*- coding: utf-8 -*-
"""R6 主体 · 集合 O(n) → O(1)（保序结构 + 哈希索引 + 墓碑删除）。

## 为什么动集合

`JSet` 原来是裸 `Vec<Val>`：`添加` 要去重得线性扫（O(n)）、`包含` 是 O(n)、
`移除` 是 `Vec::remove`（O(n)）—— 于是「建 n 个元素的集合」是 O(n²)。

改成 `Set { items: Vec<Option<Val>>, holes, index: HashMap<DictKey, usize> }`：

* `items` **保序**（迭代顺序必须确定 —— 用无序哈希集合会让同一程序两次运行
  输出不同，也会让跨宿主对拍随机失败）；
* `index` 让 `包含` / `添加` 的去重 / `移除` 的定位 O(1)；
* **删除打墓碑**（置 `None`）而不是移动尾巴 —— 保序集合删中间元素如果前移
  整个尾巴就是 O(n)，「删 n 个」还是 O(n²)。空洞过半时压缩一次（摊还 O(1)）。

## 顺带撞出 / 修掉的既有分叉（都在本文件钉住）

🔴 **Rust 的 `==` 少了「布尔与数值」这一支**：`真 == 1` / `假 == 0` /
`真 == 1.0` 在树遍历 / Python VM / C VM / **Node** 上都是 `真`
（Python 里 `bool` 是 `int` 的子类，四侧都照这个语义），**只有 Rust 给 `假`**。
于是 `{真, 1}` 在 Rust 上是两个元素、`1 在 [真]` 是假、`d[真]` 与 `d[1]` 是两个键。

语料里从前没有这个形状（`tests/cases/` 里**一个集合语料都没有**），
所以全语料体检一直绿的。修法是一条 match 分支 + **键归一也要跟着改**
（`==` 说相等而哈希键不同的话，字典 / 集合就会「查得到」变「查不到」）。

⚠️ **返工点**：`==` 与哈希键是一对，改一个必须改另一个 —— 本文件
`test_布尔与数值是同一元素` 与 `test_同一件事三个执行器一个说法` 分别钉住两侧。
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
need_node = pytest.mark.skipif(not HAS_NODE, reason="本机无 Node.js")


def _rust_exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


RUST = _rust_exe()
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")


def _five(src: str) -> dict[str, str]:
    """五个执行器各跑一遍，返回 `{执行器: stdout 或首行报错}`。"""
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
    tmp = Path(tempfile.gettempdir()) / f"_m72_{os.getpid()}.json"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    if RUST.exists():
        r = subprocess.run([str(RUST), str(tmp)], capture_output=True, input=b"")
        out["Rust"] = r.stdout.decode("utf-8", "replace") or \
            ("【错】" + r.stderr.decode("utf-8", "replace").splitlines()[0])
    if HAS_NODE:
        r = subprocess.run([NODE, "node/index.js", str(tmp)],
                           capture_output=True, input=b"")
        out["Node"] = r.stdout.decode("utf-8", "replace") or \
            ("【错】" + r.stderr.decode("utf-8", "replace").splitlines()[0])
    return out


#: 每一组都是「集合重构的易错点」，五执行器必须逐字一致。
_CASES = {
    "字面量去重保序": '令 甲 = {1, 2, 2, 3}\n打印(甲)\n打印(甲.转列表())\n',
    "跨类型是同一元素": '令 甲 = {1}\n甲.添加(1.0)\n打印(甲)\n打印(长度(甲))\n打印(1.0 在 甲)\n',
    "删除后仍保序": '令 甲 = {1, 2, 3, 4, 5}\n甲.移除(3)\n打印(甲)\n甲.移除(1)\n打印(甲)\n打印(2 在 甲, 5 在 甲, 3 在 甲)\n',
    "删过半仍保序": (
        '令 甲 = 集合()\n遍历 i 在 范围(20)：\n    甲.添加(i)\n'
        '遍历 i 在 范围(16)：\n    甲.移除(i)\n'
        '打印(甲)\n打印(长度(甲))\n打印(0 在 甲, 16 在 甲)\n'
        '甲.添加(99)\n打印(甲)\n'),
    "交错删加": (
        '令 甲 = 集合()\n遍历 i 在 范围(60)：\n    甲.添加(i)\n'
        '遍历 i 在 范围(60)：\n    如果 i % 2 == 0：\n        甲.移除(i)\n'
        '遍历 i 在 范围(60)：\n    如果 i % 3 == 0：\n        甲.丢弃(i)\n'
        '打印(长度(甲))\n打印(甲)\n打印(59 在 甲, 57 在 甲)\n'),
    "集合运算": (
        '令 甲 = {1, 2, 3}\n令 乙 = {2, 3, 4}\n'
        '打印(甲.并集(乙))\n打印(甲.交集(乙))\n打印(甲.差集(乙))\n打印(甲.对称差(乙))\n'),
    "顺序无关相等": '打印({1, 2, 3} == {3, 2, 1})\n打印({1, 2} == {1, 3})\n',
    "遍历顺序稳定": '令 甲 = 集合([9, 1, 9, 5])\n遍历 x 在 甲：\n    打印(x)\n',
    "非整数浮点": '令 甲 = {1.5, 2.5}\n打印(甲)\n甲.移除(1.5)\n打印(甲)\n打印(1.5 在 甲, 2.5 在 甲)\n',
    "大集合": (
        '令 甲 = 集合()\n遍历 i 在 范围(3000)：\n    甲.添加(i % 500)\n'
        '打印(长度(甲))\n打印(499 在 甲)\n甲.移除(250)\n打印(250 在 甲, 251 在 甲)\n'),
}


@pytest.mark.parametrize("tag", sorted(_CASES))
def test_集合语义五执行器逐字一致(tag):
    outs = _five(_CASES[tag])
    base_name, base = next(iter(outs.items()))
    for name, got in outs.items():
        assert got == base, (
            f"{tag}：{name} 与 {base_name} 不一致\n"
            + "\n".join(f"  {k:5} {v!r}" for k, v in outs.items()))


def test_保序去重():
    """集合**按首次出现顺序**排列（`集合([3,1,3,2])` → `{3, 1, 2}`）。

    ⚠️ 这条是「为什么不能用 `HashSet`」的判据：无序哈希集合的迭代顺序依赖
    哈希种子，同一程序两次运行可能不同，跨宿主对拍也会随机失败。
    """
    outs = _five('令 甲 = 集合([3, 1, 3, 2])\n打印(甲)\n打印(甲.转列表())\n')
    for name, out in outs.items():
        assert out.strip() == "{3, 1, 2}\n[3, 1, 2]", f"{name}: {out!r}"


def test_布尔与数值是同一元素():
    """`真 == 1`、`假 == 0`、`真 == 1.0` 都是真 —— 于是集合 / 字典里

    `{真, 1}` 只有一个元素、`d[真]` 与 `d[1]` 是同一个键。

    🔴 这一条是 R6 主体撞出来的**既有分叉**：四个执行器一直给「真」
    （Python 里 `bool` 是 `int` 的子类），只有 Rust 的 `==` 给「假」。
    """
    outs = _five('令 甲 = {真, 1, 假, 0}\n打印(甲)\n打印(长度(甲))\n'
                 '打印(真 == 1, 假 == 0, 真 == 1.0)\n'
                 '令 字 = {}\n字[真] = 1\n字[1] = 2\n打印(长度(字))\n')
    for name, out in outs.items():
        assert out.strip() == "{真, 假}\n2\n真 真 真\n1", f"{name}: {out!r}"


def test_布尔与数值在列表里也相等():
    """`[真, 1] == [1, 1]` 为真（`==` 是逐元素比的，同一支逻辑）。"""
    outs = _five('打印([真, 1] == [1, 1])\n打印(1 在 [真])\n')
    for name, out in outs.items():
        assert out.strip() == "真\n真", f"{name}: {out!r}"


@need_rust
def test_集合写入是线性():
    """n 翻倍耗时别翻四倍（O(n²) 的红线）。

    ⚠️ 只做「别太慢」的粗判据（进程启动 ~10ms，n=50000 若还是 O(n²) 会到几秒）。
    """
    tmp = Path(tempfile.gettempdir()) / "_m72_big.json"
    src = "令 甲 = 集合()\n遍历 i 在 范围(50000)：\n    甲.添加(i)\n打印(长度(甲))\n"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    t0 = time.perf_counter()
    r = subprocess.run([str(RUST), str(tmp)], capture_output=True, input=b"")
    dt = time.perf_counter() - t0
    assert r.stdout.decode("utf-8", "replace").strip() == "50000", \
        r.stderr.decode("utf-8", "replace")[:200]
    assert dt < 3.0, f"n=50000 的集合写入花了 {dt:.1f}s —— 像还是 O(n²)"


@need_rust
def test_集合删除是线性():
    """**删除**也要线性 —— 这是「墓碑 + 摊还压缩」那条路的判据。

    ⚠️ 保序集合如果删除时前移整个尾巴（`Vec::remove`），「删 n 个」就是 O(n²)：
    R6 主体第一版就是这样，n=16000 的批量删除要 **1845ms**；
    改成打墓碑之后同一场景 **37ms**（约 49 倍）。
    """
    tmp = Path(tempfile.gettempdir()) / "_m72_del.json"
    src = ("令 甲 = 集合()\n遍历 i 在 范围(50000)：\n    甲.添加(i)\n"
           "遍历 i 在 范围(50000)：\n    甲.移除(i)\n打印(长度(甲))\n")
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    t0 = time.perf_counter()
    r = subprocess.run([str(RUST), str(tmp)], capture_output=True, input=b"")
    dt = time.perf_counter() - t0
    assert r.stdout.decode("utf-8", "replace").strip() == "0", \
        r.stderr.decode("utf-8", "replace")[:200]
    assert dt < 3.0, f"n=50000 的批量删除花了 {dt:.1f}s —— 像还是 O(n²)"


def test_不能放进集合的报错三个执行器一个说法():
    """「不能放进集合」的**文案**必须逐字相同（message 短、hint 单列一行）。

    🔴 修之前是三个说法：Python 侧 message 短 + hint 一行；Rust 把 hint
    揉进 message 且**少了后半句**；Node 又是第三个变体（多写「集合本身」）。
    判据取**整段渲染**（含「提示：」那一行），不只看第一行。
    """
    src = '令 甲 = {1}\n甲.添加([1, 2])\n'
    tmp = Path(tempfile.gettempdir()) / "_m72_err.json"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            run_tree(src, "x.jsh")
    except BaseException as e:                                    # noqa: BLE001
        base = str(e)
    assert "提示：集合的元素要是不可变的（数字、文本…）；列表和字典放不进去" in base, base

    if RUST.exists():
        r = subprocess.run([str(RUST), str(tmp)], capture_output=True, input=b"")
        rust_err = r.stderr.decode("utf-8", "replace")
        assert "不能放进集合" in rust_err and "提示：" in rust_err, rust_err
        assert "，集合的元素要是不可变的" not in rust_err, \
            "Rust 又把 hint 揉进 message 了"
    if HAS_NODE:
        r = subprocess.run([NODE, "node/index.js", str(tmp)],
                           capture_output=True, input=b"")
        node_err = r.stderr.decode("utf-8", "replace")
        assert "不能放进集合" in node_err and "提示：" in node_err, node_err
        assert "，集合的元素要是不可变的" not in node_err, \
            "Node 又把 hint 揉进 message 了"


def test_语料里有集合():
    """`tests/cases/` 必须**有**集合语料。

    ⚠️ R6 主体之前**一个都没有** —— 于是集合从来没进过四阶段夹具与全语料
    体检的视野，它的问题（Bool/数值不同元素、文案三分叉）一直没人知道。
    **判据没覆盖到的地方，错了也没人知道。**
    """
    cases = list((ROOT / "tests" / "cases").glob("*集合*.jsh"))
    assert cases, "tests/cases/ 里没有集合语料"
    text = cases[0].read_text(encoding="utf-8")
    assert "真 == 1" in text, "语料没把「布尔与数值是同一元素」钉住"
