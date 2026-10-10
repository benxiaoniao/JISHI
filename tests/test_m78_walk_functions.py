# -*- coding: utf-8 -*-
"""R6 主体 · 树遍历参考执行器 —— 第 ③ 步：**函数与闭包**。

补上 `FuncDef` / `Return` / `Lambda`，以及**词法作用域链**（`walk.rs` 的 `Scope`）。

⚠️ 树遍历这边与字节码那边是**两套做法、同一套语义**：
* 字节码：编译器算好「本函数赋过值的名字」→ 分配槽位 + 单元表（`freevars`）；
* 树遍历：**读沿链找、写只写当前作用域** —— 就是 Python 侧 `Environment` 那套。

两套做法最容易差在「**赋值即局部**」这条上，所以本文件里它有一条专门的测试
（放错顺序的症状是**静默拿到外层的值**，不报错）。
"""

import contextlib
import io
import json
import os
import re
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
from oracle.jishi import cvm_bind                                         # noqa: E402
from oracle.jishi.compiler import compile_source, compile_to_json          # noqa: E402
from oracle.jishi.errors import JishiError                                # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree               # noqa: E402
from oracle.jishi.parser import parse_source                              # noqa: E402
from oracle.jishi.vm import VM                                            # noqa: E402

BASELINE = ROOT / "tests" / "conformance" / "baseline"
CASES = ROOT / "tests" / "cases"

RUST = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")


def _run(args: list[str]) -> tuple[str, str]:
    r = subprocess.run([str(RUST)] + args, capture_output=True, input=b"")
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def _write(obj, name: str) -> Path:
    p = Path(tempfile.gettempdir()) / f"{os.getpid()}_{name}"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def _three(src: str) -> tuple[str, str, str, str]:
    """`(树遍历 stdout, 树遍历 stderr, 字节码 VM stdout, Python 树遍历解释器 stdout)`。"""
    ast_p = _write(C.jsonable(parse_source(src, "x.jsh")), "_m78_ast.json")
    bc_p = _write(json.loads(compile_to_json(src, "x.jsh")), "_m78_bc.json")
    wo, we = _run(["--walk", str(ast_p)])
    vo, _ = _run(["--load-bytecode", str(bc_p)])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run_tree(src, "x.jsh")
        ref = buf.getvalue()
    except JishiError as e:                                         # noqa: PERF203
        ref = "【错】" + str(e).splitlines()[0]
    return wo, we, vo, ref


SHAPES = [
    "函数 f(x)：\n    返回 x * 2\n打印(f(3))",
    "函数 f()：\n    打印(1)\nf()",
    "函数 f(a, b=10)：\n    返回 a + b\n打印(f(1), f(1, 2))",
    "函数 f(*参数)：\n    返回 长度(参数)\n打印(f(1, 2, 3))",
    "函数 f(**选项)：\n    返回 长度(选项)\n打印(f(甲=1, 乙=2))",
    "函数 f(a, b)：\n    返回 a - b\n打印(f(b=1, a=5))",
    "函数 f(a, b=2, *余)：\n    返回 [a, b, 余]\n打印(f(1), f(1, 9, 8, 7))",
    "函数 斐(n)：\n    如果 n < 2：\n        返回 n\n    返回 斐(n-1) + 斐(n-2)\n打印(斐(10))",
    "函数 外(x)：\n    函数 内(y)：\n        返回 x + y\n    返回 内\n打印(外(10)(5))",
    "函数 外()：\n    令 计 = 0\n    函数 加()：\n        返回 计\n    返回 加\n打印(外()())",
    "令 双 = 函数(x)：x * 2\n打印(双(21))",
    "函数 f()：\n    返回\n打印(f())",
    "函数 f()：\n    返回 1\n令 g = f\n打印(g())",
    "函数 f(x)：\n    如果 x > 0：\n        返回 \"正\"\n    否则：\n        返回 \"负\"\n打印(f(1), f(-1))",
    "函数 f()：\n    遍历 i 在 范围(5)：\n        如果 i == 3：\n            返回 i\n    返回 -1\n打印(f())",
    # 错误路径也要一致
    "函数 f(a)：\n    返回 a\nf()",
    "函数 f(a)：\n    返回 a\nf(1, 2)",
    "函数 f(a)：\n    返回 a\nf(乙=1)",
]


@need_rust
def test_函数语料三条路逐字一致():
    doc = json.loads((BASELINE / "tests__cases__23_函数与闭包.json")
                     .read_text(encoding="utf-8"))
    ast_p = _write(doc["ast"], "_m78_c23_ast.json")
    bc_p = _write(doc["bytecode"], "_m78_c23_bc.json")
    wo, we = _run(["--walk", str(ast_p)])
    vo, _ = _run(["--load-bytecode", str(bc_p)])
    assert we == "", we
    assert wo == vo, f"树遍历与字节码 VM 不一致\n{wo!r}\n{vo!r}"
    assert wo == (CASES / "23_函数与闭包.out").read_text(encoding="utf-8")


@need_rust
@pytest.mark.parametrize("src", SHAPES)
def test_逐形状一致(src):
    wo, we, vo, _ = _three(src)
    assert wo == vo, f"{src!r}\n树遍历={wo!r}\n字节码={vo!r}\n{we}"


@need_rust
@pytest.mark.parametrize("src", SHAPES)
def test_与参考树遍历解释器也一致(src):
    """三路对拍：树遍历 / 字节码 VM / Python 树遍历解释器。"""
    wo, we, _vo, ref = _three(src)
    if ref.startswith("【错】"):
        assert "【错】" in we or we, f"{src!r}: 参考实现报错、树遍历没报\n{ref!r}"
    else:
        assert wo == ref, f"{src!r}\n树遍历={wo!r}\n参考={ref!r}\n{we}"


@need_rust
def test_赋值即局部_先读后赋要报错():
    """🔴 这一步最容易写错的一条：**「赋值即局部」必须比「沿链找」先判**。

    一个名字只要在本函数里赋过值，它在这个函数里就**只是**局部变量 ——
    读它**不许**穿透到外层。顺序放反的症状是**静默拿到外层的值**：
    不报错、结果是错的（本项目最恨的那一类）。
    """
    src = "令 甲 = 1\n函数 f()：\n    打印(甲)\n    甲 = 2\nf()\n"
    wo, we, vo, ref = _three(src)
    assert "赋值之前被用到了" in we, we
    assert "【错】" in ref, ref
    assert wo == "" == vo, (wo, vo)


@need_rust
def test_外层同名时给更具体的那句提示():
    """外层有同名变量 → 提示要说清「它成了局部变量」（与编译器那份逐字相同）。"""
    _, we, _, _ = _three("令 甲 = 1\n函数 f()：\n    打印(甲)\n    甲 = 2\nf()\n")
    assert "它就成了这个函数的局部变量" in we, we


@need_rust
def test_外层没有同名时用通用提示():
    _, we, _, _ = _three("函数 f()：\n    打印(丙)\n    丙 = 2\nf()\n")
    assert "赋值之前被用到了" in we, we
    assert "外层也有一个叫" not in we, we


@need_rust
def test_默认值在定义处求值():
    """`函数 f(x=值)` 的默认值**只在定义时算一次**（Python 语义）。

    ⚠️ 这条要是写成「每次调用时求值」，两边**大部分情况都对**，
    只有在默认值有副作用（或依赖被改过的变量）时才露馅 —— 所以专门测。
    """
    src = ("令 基准 = 10\n"
           "函数 f(x=基准)：\n"
           "    返回 x\n"
           "令 基准 = 99\n"
           "打印(f())\n")
    wo, we, _, ref = _three(src)
    assert we == "", we
    assert wo == "10\n", wo
    assert wo == ref, (wo, ref)


@need_rust
def test_闭包捕获的是同一个作用域():
    """闭包能看见**定义处**的变量，而不是调用处的。"""
    src = ("令 基数 = 1\n"
           "函数 造()：\n"
           "    函数 取()：\n"
           "        返回 基数\n"
           "    返回 取\n"
           "令 取一 = 造()\n"
           "令 基数 = 2\n"
           "打印(取一())\n")
    wo, we, vo, _ = _three(src)
    assert we == "", we
    assert wo == vo, (wo, vo)


@need_rust
def test_返回要穿出循环():
    """`返回` 在循环里要**穿出循环**（只有函数体能接住它）。

    写成 `_ => {}` 就会把 `返回` 吞掉 —— 函数白跑一趟、还回 `空`。
    """
    src = "函数 f()：\n    循环 5：\n        返回 3\n    返回 99\n打印(f())\n"
    wo, we, vo, _ = _three(src)
    assert we == "", we
    assert wo == "3\n", wo
    assert wo == vo


@need_rust
def test_形参绑定走共用_错误文案同一份():
    """结构钉住：`walk.rs` 不许自己写「需要几个参数」这类检查 —— 必须调
    `lib.rs` 的 `bind_params`。两边各写一遍，文案迟早会漂，而用户看到的就是那句话。
    """
    walk = (ROOT / "rust" / "src" / "walk.rs").read_text(encoding="utf-8")
    assert "bind_params(" in walk, "walk.rs 没有调共用的 bind_params"
    for banned in ("需要 {} 个参数", "缺少参数：", "没有叫"):
        assert banned not in walk, f"walk.rs 里自己拼了形参错误的文案：{banned}"


@need_rust
def test_类型函数值五个执行器都给函数():
    """🔴 这一条钉住一个**刚修掉的既有 bug**。

    `runtime.type_name` 的兜底是一份手写清单（按类名对齐三个执行器的函数包装类），
    原来只列了 `UserFunction` / `VmFunction`，**漏了 `CvmFunction`** ——
    于是 C VM 上 `类型(某函数)` 打出**内部类名** `CvmFunction`（不报错、只是不一致）。
    这个 bug 一直躺着，是因为**此前没有任何语料拿 `类型()` 问过函数** ——
    是第 ③ 步的语料第一次问它，五执行器体检当场报红。
    """
    src = "函数 加倍(数)：\n    返回 数 * 2\n打印(类型(加倍))\n"
    outs = {}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_tree(src, "x.jsh")
    outs["树遍历"] = buf.getvalue()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        VM(compile_source(src, "x.jsh"), "x.jsh").run()
    outs["PythonVM"] = buf.getvalue()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cvm_bind.run_source_c(src, "x.jsh")
    outs["CVM"] = buf.getvalue()
    bc_p = _write(json.loads(compile_to_json(src, "x.jsh")), "_m78_ty_bc.json")
    outs["Rust"], _ = _run(["--load-bytecode", str(bc_p)])
    r = subprocess.run(["node", "oracle/node/index.js", str(bc_p)],
                       capture_output=True, input=b"")
    outs["Node"] = r.stdout.decode("utf-8", "replace")
    assert set(outs.values()) == {"函数\n"}, outs


@need_rust
def test_进度仪表零不一致():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_walk.py")],
                       capture_output=True, cwd=str(ROOT))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")[-800:]
    assert "不一致 0" in out, out
    m = re.search(r"一致 (\d+)", out)
    assert m and int(m.group(1)) >= 39, out
