# -*- coding: utf-8 -*-
"""R6 主体 · 树遍历参考执行器 —— 第 ① 步：**控制流 + 字面量**。

这一步补上：`If` / `While` / `For` / `Loop` / `Break` / `Continue` / `Pass`、
`Compare`（**含链式**）/ `BoolOp` / `UnaryOp` / `List` / `Dict` / `Subscript`（读）、
`TargetList`（解包，含 `*余`）、复合赋值 `+=` 那一族，以及三个「藏在别处」的字面量：

* `真` / `假` 在前端是 **`Num` 且 `value` 是布尔**（不是单独的 `Bool` 节点）；
* `空` 在 AST 里是 **`Name`**（编译期才换成 `none` 常量）；
* `中断` / `继续` 是关键字（**不是 `跳出`** —— 这个我第一版就写错过）。

⚠️ 语义一律**走 `lib.rs` 里共用的那些函数**（`compare` / `get_item` /
`unpack_values` / `make_iterator` / `binop`），本文件只做「AST 遍历」那一层；
这条有结构测试钉着（`test_语义函数不许在 walk_rs 里重写`）。
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
from oracle.jishi.compiler import compile_to_json                         # noqa: E402
from oracle.jishi.errors import JishiError                                # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree               # noqa: E402
from oracle.jishi.parser import parse_source                              # noqa: E402

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
    """`(树遍历 stdout, 树遍历 stderr, 字节码 stdout, 参考树遍历解释器 stdout)`。"""
    ast_p = _write(C.jsonable(parse_source(src, "x.jsh")), "_m77_ast.json")
    bc_p = _write(json.loads(compile_to_json(src, "x.jsh")), "_m77_bc.json")
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
    # 字面量
    "打印(真, 假, 空)",
    "打印([1, 2, 3])",
    '打印([1, "甲", [2, 3], 真])',
    '打印({1: 2, "甲": [3]})',
    # 一元 / 二元
    "打印(-3, -3.5, -真)",
    "打印(非 真, 非 0, 非 1)",
    "打印(1 + 2 * 3, 7 // 2, 7 % 3, 2 ** 8, 10 / 4)",
    # 比较
    "打印(1 < 2, 2 <= 2, 3 > 4, 3 >= 3, 1 == 1, 1 != 2)",
    "打印(1 < 2 < 3, 1 < 5 < 3, 3 > 2 > 1, 1 == 1 == 1)",
    '打印(1 在 [1, 2], 5 不在 [1, 2], 空 是 空, 1 不是 2)',
    '打印("a" 在 "abc", "b" 在 {"b": 1})',
    # 连词（结果一定是真/假，不是短路值本身）
    "打印(真 与 真, 真 与 假, 假 或 真, 1 或 2, 0 与 5)",
    # 控制流
    "令 x = 0\n如果 1 < 2：\n    x = 1\n否则：\n    x = 2\n打印(x)",
    "令 x = 0\n如果 假：\n    x = 1\n否则如果 真：\n    x = 2\n否则：\n    x = 3\n打印(x)",
    "令 i = 0\n当 i < 3：\n    i += 1\n打印(i)",
    "令 i = 0\n当 真：\n    i += 1\n    如果 i >= 4：\n        中断\n打印(i)",
    "令 s = 0\n遍历 x 在 [1, 2, 3, 4]：\n    如果 x == 2：\n        继续\n    s += x\n打印(s)",
    "令 s = 0\n循环 3：\n    s += 1\n打印(s)",
    "令 i = 0\n循环 5：\n    如果 i == 2：\n        中断\n    i += 1\n打印(i)",
    "遍历 i 在 范围(3)：\n    遍历 j 在 范围(3)：\n        如果 j == 2：\n            中断\n        打印(i, j)",
    "遍历 a, b 在 配对([1, 2], [3, 4])：\n    打印(a, b)",
    "令 x, y = 1, 2\n打印(x, y)",
    "令 a, *余 = [1, 2, 3]\n打印(a, 余)",
    '遍历 x 在 "甲乙"：\n    打印(x)',
    "令 d = {\"甲\": 1, \"乙\": 2}\n遍历 k 在 d：\n    打印(k)",
    # 下标
    "令 l = [10, 20, 30]\n打印(l[0], l[-1], l[1])",
    '令 s = "甲乙丙"\n打印(s[0], s[-1])',
    '令 d = {"甲": 1}\n打印(d["甲"])',
    "令 l = [[1, 2], [3, 4]]\n打印(l[1][0])",
    # 组合
    "令 总数 = 0\n遍历 i 在 范围(10)：\n    如果 i % 2 == 0：\n        总数 += i\n打印(总数)",
    "令 阶乘 = 1\n令 n = 5\n当 n > 1：\n    阶乘 *= n\n    n -= 1\n打印(阶乘)",
    "令 表 = []\n打印(长度(表))",
]


@need_rust
def test_控制流语料三条路逐字一致():
    """`22_控制流.jsh`：树遍历 = 字节码 VM = `.out`。"""
    doc = json.loads((BASELINE / "tests__cases__22_控制流.json")
                     .read_text(encoding="utf-8"))
    ast_p = _write(doc["ast"], "_m77_c22_ast.json")
    bc_p = _write(doc["bytecode"], "_m77_c22_bc.json")
    wo, we = _run(["--walk", str(ast_p)])
    vo, _ = _run(["--load-bytecode", str(bc_p)])
    assert we == "", we
    assert wo == vo, f"树遍历与字节码 VM 不一致\n{wo!r}\n{vo!r}"
    assert wo == (CASES / "22_控制流.out").read_text(encoding="utf-8")


@need_rust
@pytest.mark.parametrize("src", SHAPES)
def test_逐形状一致(src):
    """每个形状单独一条 —— 出错能直接指到是哪个形状。"""
    wo, we, vo, _ = _three(src)
    assert wo == vo, f"{src!r}\n树遍历={wo!r}\n字节码={vo!r}\n{we}"


@need_rust
@pytest.mark.parametrize("src", SHAPES)
def test_与参考树遍历解释器也一致(src):
    """⚠️ 三路对拍：**树遍历执行器 vs 字节码 VM vs Python 树遍历解释器**。

    只跟字节码 VM 比是不够的 —— 两边可能「一起错」（比如共用了一个坏函数）。
    Python 那份是第三套独立实现，它才是「语言该是什么样」的参照。
    """
    wo, we, _vo, ref = _three(src)
    if ref.startswith("【错】"):
        assert we.startswith("【错】") or ref[3:].split("（")[0] in we, (
            f"{src!r}: 参考实现报错、树遍历没报\n{ref!r}\n{we!r}")
    else:
        assert wo == ref, f"{src!r}\n树遍历={wo!r}\n参考={ref!r}\n{we}"


@need_rust
def test_链式比较要短路():
    """`1 > 2 > 会报错的东西` —— 第二节**根本不该求值**（Python 语义）。

    不短路的话，那节的求值就会抛错；短路了才什么都不发生。
    """
    src = '令 d = {}\n打印(1 > 2 > d["没有这个键"])\n'
    wo, we, vo, _ = _three(src)
    assert wo == "假\n", (wo, we)
    assert we == "", we
    assert wo == vo


@need_rust
def test_连词结果一定是真假而不是短路值():
    """`1 或 2` 给「真」而不是 `1` —— 与其他语言不同，是**有意的**。

    见 `interpreter._eval_boolop -> bool` 与 `core/compiler.rs::c_boolop`
    （那句注释：「与树遍历一致：结果一定是真/假，不是短路值本身」）。
    """
    wo, we, _, _ = _three("打印(1 或 2, 0 与 5, \"甲\" 与 1)\n")
    assert we == "", we
    assert wo == "真 假 真\n", wo


@need_rust
def test_中断与继续只作用于最近的循环():
    """内层 `中断` 不该把外层也带出去。"""
    src = ("遍历 i 在 范围(3)：\n"
           "    遍历 j 在 范围(5)：\n"
           "        如果 j == 1：\n"
           "            中断\n"
           "        打印(i, j)\n")
    wo, we, vo, _ = _three(src)
    assert we == "", we
    assert wo == "0 0\n1 0\n2 0\n", wo
    assert wo == vo


@need_rust
def test_空与真假的字面量认三种形态():
    """`真` / `假` 是 `Num`（value 是布尔），`空` 是 `Name` —— 都要认。

    前端就是这么编的（`ast_nodes.Num` + `parser._parse_atom` 那支），
    按「节点类型」直觉写会在这里翻车。
    """
    ast = C.jsonable(parse_source("打印(真, 假, 空)\n", "x.jsh"))
    call = ast["body"][0]["expr"]
    kinds = [a["类型"] for a in call["args"]]
    assert kinds == ["Num", "Num", "Name"], kinds      # ← 契约本身长这样
    assert call["args"][0]["value"] is True
    assert call["args"][2]["id"] == "空"
    wo, we, _, _ = _three("打印(真, 假, 空)\n")
    assert we == "", we
    assert wo == "真 假 空\n", wo


@need_rust
def test_取负号对非数字要报错而不是给空():
    """🔴 这个是**顺手修的既有 bug**：`unary_neg` 原来 `_ => Val::None_`。

    `-「x」` 在三个 Python 执行器上都报「类型错误（「x」不能取负号）」，
    而 Rust 宿主**打印出「空」** —— 一个不报错的错答案。
    （Node 侧给 `NaN`，是**另一笔**，还没修，见 D70 §五的登记。）
    """
    wo, we, _, _ = _three('令 a = "x"\n打印(-a)\n')
    assert wo == "", wo
    assert "不能取负号" in we, we
    # 数字照旧
    wo2, we2, _, _ = _three("打印(-5, -2.5)\n")
    assert we2 == "" and wo2 == "-5 -2.5\n", (wo2, we2)


@need_rust
def test_循环次数错的中文文案():
    """`循环 "a"` 的报错要与其它执行器逐字一致。"""
    _, we, _, _ = _three('循环 "a"：\n    打印(1)\n')
    assert "不是有效的循环次数" in we, we


@need_rust
def test_语义函数不许在_walk_rs_里重写():
    """结构钉住：`walk.rs` 只做「AST 遍历」，语义一律调 `lib.rs` 里共用那份。

    要抓的是「编译器 vs 求值」的差异，**不是**两份 `+` / 两份 `取下标` 谁对。
    一旦有人在 walk.rs 里另写一套，互拍就变成了两份实现互拍 —— 那还比什么。
    """
    walk = (ROOT / "rust" / "src" / "walk.rs").read_text(encoding="utf-8")
    for fn in ("binop(", "compare(", "get_item(", "unpack_values(",
               "make_iterator(", "iter_next(", "truthy(", "list_new(",
               "dict_new(", "display("):
        assert fn in walk, f"walk.rs 没有调用共用的 `{fn}`"
    # 不许自己实现这些语义（按「函数定义」判，不是按出现）
    for banned in ("fn binop", "fn compare", "fn get_item", "fn iter_next",
                   "fn unpack_values", "fn list_new", "fn dict_new"):
        assert not re.search(rf"\b{banned}\b", walk), f"walk.rs 里自己实现了 `{banned}`"


@need_rust
def test_进度仪表零不一致():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_walk.py")],
                       capture_output=True, cwd=str(ROOT))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")[-800:]
    assert "不一致 0" in out, out
    # ⚠️ 判「至少涨到哪」而不是「正好等于几」—— 后面每补一步这个数都会变，
    # 钉死就变成每天改测试（而且改了也不代表什么）。第 ① 步落地时是 25。
    m = re.search(r"一致 (\d+)", out)
    assert m and int(m.group(1)) >= 25, out
