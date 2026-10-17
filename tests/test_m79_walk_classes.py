# -*- coding: utf-8 -*-
"""R6 主体 · 树遍历参考执行器 —— 第 ④ 步：**类与继承**。

📌 **核心决定：不另造一套「类」。** 树遍历产出的就是宿主那个 `Class`
（方法表里放 `Val::WalkFn`）—— 于是 `是实例` / `超()` / `get_attr` /
`set_attr` / `display` / `type_label` / `identity_key` **全都直接可用**，
不用各写一份（也就不会漂）。方法里的 `self` 用「绑定方法」表达：
`Val::WalkFn` 多一个 `self_val`，调用时它落到第一个形参（`自身`）上 ——
与字节码侧的 `BoundMethod` 是同一个意思。

顺带两件必须一起做的事：
* **内建类型的方法表**（`列.追加(…)` / `字典.…` / 文本方法…）也从 `impl VM`
  搬成了**自由函数**（`call_builtin_method`，宿主参数 `&mut dyn BuiltinHost`）
  —— 那 248 行只用到一个宿主能力（`call_value`），搬出来就能两边共用。
* `遍历 自定义对象` 要走宿主的 `make_iter`（自定义对象会先调它的 `迭代()`），
  不能直接调 `make_iterator` —— 否则「定义了 `迭代()` 的对象」遍历不了。
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
    ast_p = _write(C.jsonable(parse_source(src, "x.jsh")), "_m79_ast.json")
    bc_p = _write(json.loads(compile_to_json(src, "x.jsh")), "_m79_bc.json")
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
    # 基本：类 / 方法 / 初始化 / 属性
    "类 甲：\n    函数 初始化(自身, 名)：\n        自身.名 = 名\n    函数 取(自身)：\n        返回 自身.名\n令 对 = 新建 甲(\"x\")\n打印(对.名, 对.取())",
    # 继承 + 超()
    "类 父：\n    函数 说(自身)：\n        返回 \"父\"\n类 子 继承 父：\n    函数 说(自身)：\n        返回 超().说() + \"子\"\n打印(新建 子().说())",
    "类 父：\n    函数 初始化(自身)：\n        自身.名 = 1\n类 子 继承 父：\n    函数 初始化(自身)：\n        超().初始化()\n        自身.名 = 2\n打印(新建 子().名)",
    # 是实例 / 类型
    "类 父：\n    函数 初始化(自身)：\n        自身.名 = 1\n类 子 继承 父：\n    函数 说(自身)：\n        返回 1\n令 对 = 新建 子()\n打印(是实例(对, 子), 是实例(对, 父), 类型(对), 类型(子))",
    # 类变量
    "类 甲：\n    令 版本 = \"1.0\"\n    函数 初始化(自身)：\n        自身.计 = 0\n令 对 = 新建 甲()\n打印(甲.版本, 对.版本)",
    # 属性赋值 / 复合赋值 / 链式
    "类 甲：\n    函数 初始化(自身)：\n        自身.计 = 0\n    函数 加(自身, 数)：\n        自身.计 += 数\n        返回 自身\n令 对 = 新建 甲()\n对.加(3).加(4)\n打印(对.计)",
    "类 甲：\n    函数 初始化(自身)：\n        自身.计 = 0\n令 对 = 新建 甲()\n对.计 += 5\n打印(对.计)",
    # 文本() 钩子（打印实例时用它）
    "类 点：\n    函数 初始化(自身, 横, 纵)：\n        自身.横 = 横\n        自身.纵 = 纵\n    函数 文本(自身)：\n        返回 \"(\" + 文本(自身.横) + \",\" + 文本(自身.纵) + \")\"\n打印(新建 点(1, 2))",
    # 迭代() / 长度(自定义对象)
    "类 序：\n    函数 初始化(自身, 项)：\n        自身.项 = 项\n    函数 迭代(自身)：\n        返回 自身.项\n遍历 x 在 新建 序([1, 2, 3])：\n    打印(x)",
    "类 序：\n    函数 初始化(自身, 项)：\n        自身.项 = 项\n    函数 迭代(自身)：\n        返回 自身.项\n打印(长度(新建 序([1, 2, 3, 4])))",
    # 下标作赋值目标
    "令 列 = [1, 2, 3]\n列[0] = 9\n列[-1] = 7\n打印(列)",
    "令 字典 = {}\n字典[\"新\"] = 1\n打印(字典)",
    "令 表 = [[1, 2], [3, 4]]\n表[0][1] = 9\n打印(表)",
    "令 列 = [1, 2]\n列[0] += 10\n打印(列)",
    # 内建类型的方法（走共用的方法表）
    "令 列 = [1]\n列.追加(2)\n打印(列, 列.弹出(), 列)",
    "令 文本值 = \"甲\"\n打印(长度(文本值), 文本值)",
    # 错误路径
    "类 甲：\n    函数 初始化(自身, 名)：\n        自身.名 = 名\n打印(新建 甲())",
    "类 甲：\n    函数 初始化(自身)：\n        自身.名 = 1\n令 对 = 新建 甲()\n打印(对.没有这个属性)",
    "类 甲：\n    函数 说(自身, 甲参数)：\n        返回 甲参数\n令 对 = 新建 甲()\n对.说()",
    "类 甲：\n    函数 初始化(自身, 名)：\n        自身.名 = 名\n打印(新建 甲(\"x\").名)",
    "类 甲：\n    函数 说(自身)：\n        返回 1\n打印(新建 甲().说())",
]


@need_rust
def test_类语料三条路逐字一致():
    doc = json.loads((BASELINE / "tests__cases__24_类.json")
                     .read_text(encoding="utf-8"))
    ast_p = _write(doc["ast"], "_m79_c24_ast.json")
    bc_p = _write(doc["bytecode"], "_m79_c24_bc.json")
    wo, we = _run(["--walk", str(ast_p)])
    vo, _ = _run(["--load-bytecode", str(bc_p)])
    assert we == "", we
    assert wo == vo, f"树遍历与字节码 VM 不一致\n{wo!r}\n{vo!r}"
    assert wo == (CASES / "24_类.out").read_text(encoding="utf-8")


@need_rust
@pytest.mark.parametrize("src", SHAPES)
def test_逐形状一致(src):
    wo, we, vo, _ = _three(src)
    assert wo == vo, f"{src!r}\n树遍历={wo!r}\n字节码={vo!r}\n{we}"


@need_rust
@pytest.mark.parametrize("src", SHAPES)
def test_与参考树遍历解释器也一致(src):
    wo, we, _vo, ref = _three(src)
    if ref.startswith("【错】"):
        assert wo == "" or we, f"{src!r}: 参考实现报错、树遍历没报\n{ref!r}"
    else:
        assert wo == ref, f"{src!r}\n树遍历={wo!r}\n参考={ref!r}\n{we}"


@need_rust
def test_树遍历不另造一套类():
    """结构钉住：`walk.rs` 必须造**宿主的** `Class` / `Instance`。

    一旦有人在树遍历里另写一个「自己的类」，`是实例` / `超()` / `get_attr`
    就会长出第二份语义 —— 而这份代码存在的全部意义就是「不要第二份」。
    """
    walk = (ROOT / "rust" / "src" / "walk.rs").read_text(encoding="utf-8")
    assert "Class {" in walk and "Instance {" in walk, "walk.rs 没造宿主的类/实例"
    for fn in ("get_attr(", "set_attr(", "set_item(", "call_builtin_method("):
        assert fn in walk, f"walk.rs 没调共用的 `{fn}`"
    for banned in ("fn get_attr", "fn set_attr", "fn set_item",
                   "fn call_builtin_method"):
        assert not re.search(rf"\b{banned}\b", walk), f"walk.rs 自己实现了 `{banned}`"


@need_rust
def test_绑定方法显示成方法而不是函数():
    """`甲.方法` 的显示/类型必须与字节码侧的 `BoundMethod` 一致（`<方法>` / `方法`）。"""
    src = "类 甲：\n    函数 说(自身)：\n        返回 1\n打印(类型(新建 甲().说), 新建 甲().说)\n"
    wo, we, vo, _ = _three(src)
    assert we == "", we
    assert wo == vo, (wo, vo)


@need_rust
def test_迭代返回自身要报错():
    """「迭代()」返回对象自身 → 会无限循环，必须拦（与字节码 VM 同一句文案）。"""
    src = ("类 甲：\n    函数 迭代(自身)：\n        返回 自身\n"
           "遍历 x 在 新建 甲()：\n    打印(x)\n")
    _, we, _, _ = _three(src)
    assert "无限循环" in we, we


@need_rust
def test_进度仪表零不一致():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_walk.py")],
                       capture_output=True, cwd=str(ROOT))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")[-800:]
    assert "不一致 0" in out, out
    m = re.search(r"一致 (\d+)", out)
    assert m and int(m.group(1)) >= 53, out
