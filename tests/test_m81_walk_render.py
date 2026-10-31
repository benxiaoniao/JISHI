# -*- coding: utf-8 -*-
"""R6 主体 · 树遍历参考执行器 —— 第 ⑥ 步：**报错渲染对齐**。

这一步让树遍历执行器的报错渲染与字节码 VM 的 `render_error` **逐字一致**：

* **头行**（D73 已抽成 `error_head`）；
* **源码行框**：`  ┌─ 文件:行:列` + `  │` + `行号 │ 源码` + `    │ ^` + `  │`；
* **调用链**：`调用链（从外到内）： 名字 ← 第 N 行`；
* **提示语**：`  提示：…`。

## 实现上的三处关键

1. **源码行从外面给**：树遍历的输入是 AST JSON、里面没有源码，所以 `--walk`
   多了一个**可选**的「源文件路径」参数（`jishi-rs --walk AST.json 源文件.jsh`）。
   渲染与字节码 VM **共用 `render_error_with`**（源码行来自一个回调），一处不漂。
2. **错误对象补位置**：共用的 `binop` / `get_item` / `compare` … 返回的错误**不带
   行列**，树遍历在 `eval` / `exec_stmt` 的分派层用「出错节点」的位置补上
   （对齐字节码 VM 的 `fill_error_loc`，那边用「出错指令」的位置，两者天然一致）。
   ⚠️ 踩过一个坑：`?` 在 `match` arm 里会**提前 return、绕过**补位置 —— 于是
   `exec_import` 等的错误没有位置。改成 `.map(...)` 让错误也走补位置。
3. **调用链**：Walker 维护 `call_stack`（只含「直接函数调用」的帧），出错时拍成
   `trace`。⚠️ **方法调用 / 类实例化 / 内建回调不进栈**（Python 侧 `_eval_call`
   只有 `UserFunction` 直接调用才 append）。

## 顺手登记的既有分叉（都是字节码 VM 的，不是本执行器的）

* **方法调用 / 类实例化的调用链**：Python 规范**没有**方法帧（`实例.方法()` 走
  `UserFunction.__call__`，明确「不动 _call_stack」），字节码 VM 曾多一个方法帧 ——
  已在 R6 收口时修掉（见 `test_方法调用的调用链不进栈` / `test_类实例化的调用链不进栈`）。
* **递归无深度限制**：Python 报「递归层数太深了」+ 提示，两个 Rust 宿主一个爆内存、
  一个栈溢出（都是 `无递归深度限制` 的后果，表现不同而已）。**这条仍未修**（VM 的活）。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

os.environ.setdefault("JISHI_FRONTEND", "python")

import conformance as C                                            # noqa: E402
from oracle.jishi.compiler import compile_to_json                         # noqa: E402
from oracle.jishi.parser import parse_source                              # noqa: E402

RUST = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")


def _render_both(src: str) -> tuple[str, str, str, str]:
    """跑树遍历执行器与字节码 VM，返回 `(walk stdout, walk stderr, vm stdout, vm stderr)`。

    ⚠️ **filename 用绝对路径**（同一份）：VM 的 filename 来自字节码，树遍历的来自
    `--walk` 的源文件路径参数 —— 传同一个绝对路径字符串，两边都能读到源码、名字也一致。
    """
    # ⚠️ 文件名带 pid：并行时多个 worker 共享临时目录，固定名会互相覆盖。
    pid = os.getpid()
    tmp = Path(tempfile.gettempdir())
    srcfile = tmp / f"{pid}_m81_src.jsh"
    srcfile.write_text(src, encoding="utf-8")
    rel = str(srcfile)                       # 绝对路径，两边都用它
    ast = C.jsonable(parse_source(src, rel))
    (tmp / f"{pid}_m81_ast.json").write_text(json.dumps(ast, ensure_ascii=False), encoding="utf-8")
    (tmp / f"{pid}_m81_bc.json").write_text(compile_to_json(src, rel), encoding="utf-8")

    w = subprocess.run([str(RUST), "--walk", str(tmp / f"{pid}_m81_ast.json"), rel],
                       capture_output=True, input=b"")
    v = subprocess.run([str(RUST), "--load-bytecode", str(tmp / f"{pid}_m81_bc.json")],
                       capture_output=True, input=b"")
    return (w.stdout.decode("utf-8", "replace"), w.stderr.decode("utf-8", "replace"),
            v.stdout.decode("utf-8", "replace"), v.stderr.decode("utf-8", "replace"))


SHAPES = [
    # 源码行 + `^`
    "打印(1 / 0)\n",
    "令 甲 = [1]\n打印(甲[9])\n",
    "令 字典 = {}\n打印(字典[\"键\"])\n",
    # 提示语
    "打印(长渡(1))\n",
    # 调用链（直接函数调用）
    "函数 外()：\n    函数 内()：\n        打印(1 / 0)\n    内()\n外()\n",
    "函数 甲()：\n    函数 乙()：\n        函数 丙()：\n            打印(1 / 0)\n        丙()\n    乙()\n甲()\n",
    # 参数个数错（位置在 Call 节点）
    "函数 f(a)：\n    返回 a\nf()\n",
    # 方法调用的报错位置（在「取属性」那处，不是方法名）
    "令 列 = [1]\n列.追加()\n",
    "令 字典 = {\"a\": 1}\n字典.弹出(\"b\")\n",
    # 类型错误 / 不可调用
    "令 甲 = 1\n甲()\n",
    # 复合赋值
    "令 甲 = [1, 2]\n甲[0] += \"x\"\n",
    # 抛出后未捕获
    "抛出 值错误(\"坏了\")\n",
    # 切片步长错
    "令 甲 = [1, 2]\n打印(甲[::0])\n",
]


@need_rust
@pytest.mark.parametrize("src", SHAPES)
def test_报错渲染逐字一致(src: str):
    """树遍历 = 字节码 VM，**完整渲染**（stdout + 报错的 stderr）逐字一致。

    ⚠️ 以前 check_walk 只比 stdout 与报错首行；这一步把「源码行 / `^` / 调用链 /
    提示」全比进去，任何一处漂都会红。
    """
    wo, we, vo, ve = _render_both(src)
    assert wo == vo, f"stdout 不一致：{wo!r} vs {vo!r}"
    assert we.rstrip("\n") == ve.rstrip("\n"), f"报错渲染不一致：\n{we!r}\nvs\n{ve!r}"


@need_rust
def test_无源文件路径时退回没有源码行():
    """`--walk` 不传源文件路径时，位置行照打、源码行没有（与 VM 读不到源文件同口径）。"""
    src = "打印(1 / 0)\n"
    tmp = Path(tempfile.gettempdir())
    pid = os.getpid()
    rel = str(tmp / f"{pid}_m81_nosrc.jsh")
    ast = C.jsonable(parse_source(src, rel))
    (tmp / f"{pid}_m81_ast2.json").write_text(json.dumps(ast, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([str(RUST), "--walk", str(tmp / f"{pid}_m81_ast2.json")],
                       capture_output=True, input=b"")
    err = r.stderr.decode("utf-8", "replace")
    assert "错误 E2001" in err
    # 没有源码行：`^` 只有在读得到源码行时才会打（`N │ 源码` 那一行也没有）
    assert "^" not in err, err
    assert "│ 打印" not in err, err


@need_rust
def test_方法调用的调用链不进栈():
    """方法调用（`实例.方法()`）**不进调用链** —— 对齐 Python 侧 `_eval_call`
    的口径（只有 `UserFunction` 直接调用才进 `_call_stack`，方法走
    `UserFunction.__call__` 不动栈）。D74 §三登记的「字节码 VM 多一个方法帧」已修。"""
    src = "类 甲：\n    函数 算(自身)：\n        打印(1 / 0)\n令 对 = 新建 甲()\n对.算()\n"
    wo, we, vo, ve = _render_both(src)
    assert wo == vo == ""
    assert "调用链" not in we, f"规范（树遍历）不该有方法帧：{we!r}"
    assert "调用链" not in ve, f"字节码 VM 也不该有方法帧：{ve!r}"


@need_rust
def test_类实例化的调用链不进栈():
    """类实例化（`初始化`）也不该有方法帧。D74 §三登记的构造帧已修。"""
    src = "类 甲：\n    函数 初始化(自身)：\n        打印(1 / 0)\n令 对 = 新建 甲()\n"
    wo, we, vo, ve = _render_both(src)
    assert wo == vo == ""
    assert "调用链" not in we, f"规范（树遍历）不该有构造帧：{we!r}"
    assert "调用链" not in ve, f"字节码 VM 也不该有构造帧：{ve!r}"


@need_rust
def test_方法里直接调函数仍进链():
    """方法里**直接调用**一个全局函数，那个函数的帧仍要进链（方法帧本身不进）。"""
    src = "函数 内()：\n    打印(1 / 0)\n类 甲：\n    函数 算(自身)：\n        内()\n令 对 = 新建 甲()\n对.算()\n"
    wo, we, vo, ve = _render_both(src)
    assert "内 ←" in we, f"规范该有「内」帧：{we!r}"
    assert "算 ←" not in we, f"规范不该有「算」方法帧：{we!r}"
    assert "内 ←" in ve, f"字节码 VM 该有「内」帧：{ve!r}"
    assert "算 ←" not in ve, f"字节码 VM 不该有「算」方法帧：{ve!r}"


# ---------------------------------------------------------------------------
# 结构钉住
# ---------------------------------------------------------------------------

def test_渲染格式只有一份():
    """`render_error_with` 是**唯一**的报错渲染（字节码 VM 与树遍历共用）。

    以前 `run_walk` 自己拼头行，于是「值错误 vs 数值不对」「空消息多括号」这类漂移
    只靠逐字比才抓得到。现在源码框 / 调用链 / 提示也走同一份，一处不漂。
    """
    lib = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    walk = (ROOT / "rust" / "src" / "walk.rs").read_text(encoding="utf-8")
    assert lib.count("pub(crate) fn render_error_with") == 1, "渲染函数应当只有一份"
    assert "render_error_with" in lib[lib.index("pub fn run_walk"):], \
        "run_walk 没用共用的渲染函数"
    # walk.rs 不许自己拼源码框 / 调用链（排除注释，只看渲染用的精确字符串）
    assert "┌─" not in walk, "walk.rs 里出现了自己拼的源码框 —— 请走 render_error_with"
    assert "调用链（从外到内）" not in walk, \
        "walk.rs 里出现了自己拼的调用链 —— 请走 render_error_with"


def test_调用栈只含直接函数调用():
    """Walker 的 `call_stack` 只在 `eval_call` 的 `Val::WalkFn`（未绑定）处 push，
    方法调用 / 内建回调 / 类实例化一律 `call_walk_fn(…, None)` —— 与 Python 侧
    `_eval_call` 的口径一致（只有 `UserFunction` 直接调用进 `_call_stack`）。"""
    walk = (ROOT / "rust" / "src" / "walk.rs").read_text(encoding="utf-8")
    assert "call_stack.push" in walk, "没有 push 调用栈的地方"
    # 三处调用点都必须传 None（方法 / 内建回调 / 类实例化）
    assert walk.count("call_walk_fn(f, args, &[], &[], None)") >= 1, \
        "call_value 里调 call_walk_fn 必须传 None（方法/回调不进调用栈）"
