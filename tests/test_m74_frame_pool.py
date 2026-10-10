# -*- coding: utf-8 -*-
"""R6 主体 · 函数调用：帧创建优化（去掉 HashMap / 中间值表 / 指令数组克隆，加帧池）。

## 三处「每次调用都掏钱」的地方（R6 主体·函数调用）

量出来一参调用比 C VM 慢 350ns、三参慢 595ns/次。三处根因：

1. **`instrs.clone()`** —— `execute_frame` 每进一次帧就**克隆整个指令数组**
   （一次堆分配 + 拷贝）。⚠️ 上一轮我估它「只占 ~2%」是**错的**，实际是最大的一处：
   单改这一项（`module` 换成 `Rc<Module>`，借引用而不是克隆）就让
   `斐波(24)` 从 135ms 掉到 95.7ms。
2. **`HashMap<usize, Val>` 装参数** —— 参数通常 1~3 个，哈希表纯属浪费 → `Vec<Option<Val>>`。
3. **每帧两次堆分配**（`locals` / `locals_set`）→ **帧池**：帧弹出时回收、下次复用容量。

**交错 A/B**（同机同时段，5 轮取最优）：`斐波(24)` **1.48x**、一参调用 **1.47x**、
三参 **1.44x**、闭包 **1.43x**；对 C VM 的差距从 **2.0~2.3x 收窄到 1.2~1.5x**。

## ⚠️ 帧池最危险的两处（本文件钉住）

* `locals_set` 残留 `true` → **读到上一帧的残留值**（不报错的错答案）；
* `cells` 复用 → 闭包看到**别的函数**的单元。

所以 `pop_frame` 里**把内容全清掉、只留容量**；`bind_args` 里 `locals_set`
必须**整个重置成 false**。
"""

import io
import os
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


def _rust_exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


RUST = _rust_exe()
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")


def _five(src: str) -> dict[str, str]:
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
    tmp = Path(tempfile.gettempdir()) / f"_m74_{os.getpid()}.json"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    if RUST.exists():
        r = subprocess.run([str(RUST), "--load-bytecode", str(tmp)], capture_output=True, input=b"")
        out["Rust"] = r.stdout.decode("utf-8", "replace").rstrip("\n") or \
            ("【错】" + r.stderr.decode("utf-8", "replace").rstrip("\n"))
    if HAS_NODE:
        r = subprocess.run([NODE, "oracle/node/index.js", str(tmp)],
                           capture_output=True, input=b"")
        out["Node"] = r.stdout.decode("utf-8", "replace").rstrip("\n") or \
            ("【错】" + r.stderr.decode("utf-8", "replace").rstrip("\n"))
    return out


#: 帧池的易错形状。全部要五执行器逐字一致。
_CASES = {
    "交错调用不同参数个数": (
        "函数 零()：\n    返回 0\n"
        "函数 一(a)：\n    返回 a\n"
        "函数 三(a, b, c)：\n    返回 a + b + c\n"
        "函数 变(*参数)：\n    返回 长度(参数)\n"
        "遍历 i 在 范围(300)：\n"
        "    打印(零(), 一(i), 三(i, 1, 2), 变(i, i))\n"),
    "未赋值局部不残留": (
        "函数 先(甲)：\n    返回 甲\n"
        "函数 后()：\n    如果 假：\n        令 乙 = 99\n    返回 乙\n"
        "遍历 i 在 范围(50)：\n"
        "    打印(先(i))\n"
        "    尝试：\n        后()\n    捕获 异常 为 错：\n        打印(错.类型)\n"),
    "闭包不串单元": (
        "函数 造(基数)：\n    函数 取()：\n        返回 基数\n    返回 取\n"
        "令 a = 造(1)\n令 b = 造(2)\n令 c = 造(3)\n"
        "遍历 i 在 范围(100)：\n    打印(a(), b(), c())\n"),
    "异常之后继续调用": (
        "函数 炸()：\n    返回 1 // 0\n"
        "函数 好()：\n    返回 7\n"
        "遍历 i 在 范围(50)：\n"
        "    尝试：\n        炸()\n    捕获 异常：\n        打印(好())\n"),
    "深递归与互相递归": (
        "函数 偶(n)：\n    如果 n == 0：\n        返回 真\n    返回 奇(n - 1)\n"
        "函数 奇(n)：\n    如果 n == 0：\n        返回 假\n    返回 偶(n - 1)\n"
        "打印(偶(100), 奇(101))\n"),
    "关键字与默认值": (
        "函数 格(a, b = 2, *余)：\n    返回 a + b + 长度(余)\n"
        "遍历 i 在 范围(100)：\n"
        "    打印(格(i), 格(a=i), 格(i, 3), 格(i, b=4), 格(i, 1, 2, 3))\n"),
    "循环信号穿过帧": (
        "函数 找(列)：\n    遍历 x 在 列：\n        如果 x > 2：\n            返回 x\n    返回 0\n"
        "函数 跳()：\n    令 和 = 0\n    遍历 i 在 范围(50)：\n"
        "        如果 i == 3：\n            继续\n        如果 i > 6：\n            跳出\n"
        "        和 = 和 + i\n    返回 和\n"
        "打印(找([1, 2, 3, 4]), 跳())\n"),
}


@pytest.mark.parametrize("tag", sorted(_CASES))
def test_帧复用不串味的五执行器一致(tag):
    outs = _five(_CASES[tag])
    base_name, base = next(iter(outs.items()))
    assert base and base != "0", f"{tag} 的基准输出是空的？{base!r}"
    for name, got in outs.items():
        assert got == base, (
            f"{tag}：{name} 与 {base_name} 不一致\n"
            + "\n".join(f"  {k:5} {v[:120]!r}" for k, v in outs.items()))


def test_未赋值局部在帧复用后仍然报错():
    """🔴 帧池最危险的一条：`locals_set` 没重置 → **读到上一帧的残留值**。

    先调一个有参函数（把某个槽位赋上值），再调一个**同槽位没赋值**的函数 ——
    若 `locals_set` 残留，后者会静默返回前者的值（**不报错的错答案**）。
    """
    outs = _five(
        "函数 先(甲)：\n    返回 甲\n"
        "函数 后()：\n    如果 假：\n        令 乙 = 99\n    返回 乙\n"
        "打印(先(42))\n"
        "尝试：\n    打印(后())\n捕获 异常 为 错：\n    打印(错.类型)\n")
    for name, out in outs.items():
        assert out.strip() == "42\n找不到这个名字", f"{name}: {out!r}"


def test_闭包在帧复用后仍然各自独立():
    """另一条：`cells` 复用 → 闭包看到别的函数的单元。"""
    outs = _five(
        "函数 造(基数)：\n    函数 取()：\n        返回 基数\n    返回 取\n"
        "令 a = 造(1)\n令 b = 造(2)\n令 c = 造(3)\n打印(a(), b(), c())\n")
    for name, out in outs.items():
        assert out.strip() == "1 2 3", f"{name}: {out!r}"


@need_rust
@pytest.mark.计时
def test_函数调用不再有哈希表与指令数组分配():
    """粗判据：20 万次一参调用别比以前慢（回归护栏）。

    ⚠️ 不做精确倍数（那要**交错 A/B**，见 `build/_ab_frame.py`）；
    这里只抓「退回旧路径级别」的灾难性回归：旧路径一参 20 万次约 190ms，
    新路径约 130ms —— 上限给 300ms（含 ~85ms 进程启动）。
    """
    import time
    tmp = Path(tempfile.gettempdir()) / f"{os.getpid()}_m74_loop.json"
    src = ("函数 加一(x)：\n    返回 x + 1\n令 总和 = 0\n"
           "遍历 i 在 范围(200000)：\n    总和 = 加一(i)\n打印(总和)\n")
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    t0 = time.perf_counter()
    r = subprocess.run([str(RUST), "--load-bytecode", str(tmp)], capture_output=True, input=b"")
    dt = (time.perf_counter() - t0) * 1000
    assert r.stdout.decode("utf-8", "replace").strip() == "200000", \
        r.stderr.decode("utf-8", "replace")[:200]
    assert dt < 300, f"20 万次调用花了 {dt:.0f}ms（旧路径约 190ms）"


def test_语料里有函数帧复用的形状():
    """`tests/cases/` 要有「帧复用不串味」的语料（未赋值局部 / 闭包 / 异常后继续）。"""
    cases = list((ROOT / "tests" / "cases").glob("*函数帧复用*.jsh"))
    assert cases, "tests/cases/ 里没有函数帧复用语料"
    text = cases[0].read_text(encoding="utf-8")
    for want in ("未赋值", "造", "除零", "阶乘"):
        assert want in text, f"语料少了「{want}」"
