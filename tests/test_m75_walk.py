# -*- coding: utf-8 -*-
"""R6 主体 · **树遍历参考执行器**（`rust/src/walk.rs`）第一个切片。

## 它是干什么的

与字节码 VM **互为参考实现**：同一段源码，两个独立实现跑出来必须一样。
差异 = **编译器 / 字节码生成**的问题（字节码 VM 自己抓不出来 —— 它照着错的
字节码跑，一切"正常"）。

## 为什么吃 AST JSON 而不是「宿主自己 parse」

AST 已经是**契约产物**（`docs/前端契约.md` §4.2，两个前端都产它）。
吃 JSON 就不用把 `core` 的 pyo3 依赖解耦进宿主，也与 R5「产物优先给文本」
同一条思路。

## 现在的进度（如实记）

已实现：`Program` / `Assign`(`=`) / `ExprStmt` / `Name` / `Num` / `Str` /
`BinOp` / `Call`（**调用**——目标必须是名字，内建走**共用**的那张表，
见 `tests/test_m76_walk_builtins.py`）。
**未实现的节点明确报「还没实现「X」」** —— 不静默给错值。
进度与待办清单随时可查：`python tools/check_walk.py`。

⚠️ 还没做「报错渲染与其它执行器逐字对齐」（要复用 `render_error`），
所以现阶段只对拍**成功路径的 stdout**。
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASELINE = ROOT / "tests" / "conformance" / "baseline"
CASES = ROOT / "tests" / "cases"


def _rust_exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


RUST = _rust_exe()
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")


def _run(args: list[str]) -> tuple[str, str]:
    r = subprocess.run([str(RUST)] + args, capture_output=True, input=b"")
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def _baseline(stem: str) -> dict:
    return json.loads((BASELINE / f"{stem}.json").read_text(encoding="utf-8"))


def _write_tmp(obj, name: str) -> Path:
    p = Path(tempfile.gettempdir()) / f"{os.getpid()}_{name}"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


@need_rust
def test_算术语料的两个实现结果一致():
    """最基本的判据：`01_算术.jsh` 的 AST 与字节码，两条路跑出来一样。

    ⚠️ 这一条同时是「链路打通」的证明：AST JSON → 树遍历 → 输出，全程不经过
    字节码；而且输出与 `.out` 期望值一致。
    """
    doc = _baseline("tests__cases__01_算术")
    ast_p = _write_tmp(doc["ast"], "_m75_ast.json")
    bc_p = _write_tmp(doc["bytecode"], "_m75_bc.json")
    walk_out, walk_err = _run(["--walk", str(ast_p)])
    vm_out, _ = _run(["--load-bytecode", str(bc_p)])
    assert walk_err == "", walk_err
    assert walk_out == vm_out, f"树遍历与字节码 VM 不一致\n{walk_out!r}\n{vm_out!r}"
    expected = (CASES / "01_算术.out").read_text(encoding="utf-8")
    assert walk_out == expected, f"{walk_out!r} != {expected!r}"


@need_rust
def test_未实现的节点明确报错而不是静默给值():
    """🔴 本项目的硬规矩：**不许静默给错值**。

    ⚠️ **这条测试被「实现掉」过两次**（先是 `If`、再是 `ClassDef` —— 它们分别在第 ①、
    第 ④ 步落地了）。**别再拿「某个真节点」当例子**：每落一步就得改一次测试，
    而改测试的成本会诱导人「顺手把这条删了」。
    所以改成拿一个**根本不存在的节点类型**来钉 —— 它测的是**同一条机制**
    （分派表落到「还没实现」分支），而且永远稳定。
    """
    tmp = Path(tempfile.gettempdir()) / f"{os.getpid()}_m75_unimpl.json"
    kind_name = "还没实现的节点类型"
    ast = {"类型": "Program", "body": [{"类型": kind_name, "line": 1, "col": 1}]}
    tmp.write_text(json.dumps(ast, ensure_ascii=False), encoding="utf-8")
    out, err = _run(["--walk", str(tmp)])
    assert out == "", f"未实现的节点不该有输出：{out!r}"
    assert "还没实现" in err and kind_name in err, err


@need_rust
def test_运算符表与字节码契约一致():
    """`walk.rs` 的 `binop_code` 必须与 `frontend/src/opcodes.rs` 的 `BINOP_*` 一致。

    ⚠️ 两张表分处两个 crate —— 用**读文件比对**的方式把它钉住（本项目的漂移检测惯例）。
    （R7.1-a 之前宿主**不能**依赖那边：前端住在 `core/`，而 `core` 链着 pyo3；
    现在前端是独立的 `frontend/` crate，宿主**可以**依赖它了 —— 但这条读文件比对
    照样有效，先留着。）
    """
    opcodes = (ROOT / "frontend" / "src" / "opcodes.rs").read_text(encoding="utf-8")
    walk = (ROOT / "rust" / "src" / "walk.rs").read_text(encoding="utf-8")
    import re
    for name, op in (("ADD", "+"), ("SUB", "-"), ("MUL", "*"), ("DIV", "/"),
                     ("FLOORDIV", "//"), ("MOD", "%"), ("POW", "**")):
        m = re.search(rf'BINOP_{name}: i64 = (\d+);', opcodes)
        assert m, f"opcodes.rs 里找不到 BINOP_{name}"
        code = m.group(1)
        got = re.search(rf'"{re.escape(op)}" => (\d+),', walk)
        assert got, f"walk.rs 里找不到「{op}」的映射"
        assert got.group(1) == code, (
            f"「{op}」：walk.rs={got.group(1)} 而 opcodes.rs={code}")


@need_rust
def test_进度仪表能跑():
    """`tools/check_walk.py` 是这件事的进度仪表 —— 它必须能跑、且**零不一致**。

    ⚠️ 「还没实现」是允许的（第一个切片）；**「不一致」不是** ——
    那说明两个实现有真分歧，要立刻查。
    """
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_walk.py")],
        capture_output=True, cwd=str(ROOT))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")[-500:]
    assert "不一致 0" in out, out
    assert "一致 7" in out or "一致 " in out, out
