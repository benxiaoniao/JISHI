# -*- coding: utf-8 -*-
"""R6 主体 · **内建宿主接口**：内建从 `fn(&[Val], &mut VM)` 改成
`fn(&[Val], &mut dyn BuiltinHost)`，于是**树遍历参考执行器能跑同一张内建表**。

## 为什么要有这个接口（D68 §四的拍板：方案 a）

内建的签名原来是 `&mut VM` —— 内建**只能**在字节码 VM 里跑，树遍历执行器
（`walk.rs`）就用不上，只能另写一份。**两份一定会漂**，而漂了之后「两个独立
实现互拍」这条验证资产就废了（互拍的全部价值来自「语义独立」）。

改成 `&mut dyn BuiltinHost` 之后：`VM` 与 `Walker` 各自实现这个接口，
**内建只有一份实现**（`new_builtins()` 那张表），两个执行器都调它。

## 本文件盯住的东西

1. `21_内建调用.jsh` 三条路（树遍历 = 字节码 VM = `.out`）逐字一致；
2. 树遍历**用的是共用那张表**（不是自己写的）；
3. 接口**故意做小**：只有 5 个方法 —— 谁悄悄加方法而没实现，这里报红；
4. **报错前已经打印的内容要吐出来**（字节码 VM 在 R6.2 统一过，参考执行器
   必须一致，否则互拍工具会把「少半截输出」判成不一致）；
5. 「找不到名字」的候选名池**含内建名**（`长渡` → 提示「长度」）。
"""

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


def _tmp(obj, name: str) -> Path:
    p = Path(tempfile.gettempdir()) / f"{os.getpid()}_{name}"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def _both(src: str) -> tuple[str, str, str]:
    """同一段源码：`(树遍历 stdout, 树遍历 stderr, 字节码 VM stdout)`。"""
    ast_p = _tmp(C.jsonable(parse_source(src, "x.jsh")), "_m76_ast.json")
    bc_p = _tmp(json.loads(compile_to_json(src, "x.jsh")), "_m76_bc.json")
    wo, we = _run(["--walk", str(ast_p)])
    vo, _ = _run(["--load-bytecode", str(bc_p)])
    return wo, we, vo


@need_rust
def test_内建调用语料三条路逐字一致():
    """`21_内建调用.jsh`：树遍历 = 字节码 VM = `.out`。"""
    doc = json.loads((BASELINE / "tests__cases__21_内建调用.json")
                     .read_text(encoding="utf-8"))
    ast_p = _tmp(doc["ast"], "_m76_c21_ast.json")
    bc_p = _tmp(doc["bytecode"], "_m76_c21_bc.json")
    wo, we = _run(["--walk", str(ast_p)])
    vo, _ = _run(["--load-bytecode", str(bc_p)])
    assert we == "", we
    assert wo == vo, f"树遍历与字节码 VM 不一致\n{wo!r}\n{vo!r}"
    expected = (CASES / "21_内建调用.out").read_text(encoding="utf-8")
    assert wo == expected, f"{wo!r} != {expected!r}"


@need_rust
@pytest.mark.parametrize("src", [
    '打印(长度("基石语言"))\n',
    "打印(范围(3))\n",
    "打印(最大(1, 5, 3), 最小(2, 8, 4))\n",
    "打印(总和(范围(4)))\n",
    "打印(整数(3.7), 小数(2), 文本(12))\n",
    '打印(类型(1), 类型(1.5), 类型("甲"))\n',
    "打印(带下标(范围(3), 1))\n",
    "打印(配对(范围(3), 范围(3)))\n",
    "打印(反转(范围(3)))\n",
    '打印(字符(65), 序数("A"))\n',
    "打印(位与(6, 3), 位或(4, 1), 左移(1, 3), 右移(8, 2))\n",
    "打印(1 + 2 * 3, 10 / 4, 10 // 4, 10 % 4, 2 ** 10)\n",
    '打印(精确("0.1") + 精确("0.2"))\n',
    "打印(集合())\n",
    # 需要宿主能力的那个内建：两边都该报「不能当上下文管理器用」（口径一致）
    "打印(进入上下文(1))\n",
])
def test_逐条内建在两个执行器上一致(src):
    """逐条对拍 —— 出问题能立刻指到是哪个内建（语料那条只能指到整个文件）。"""
    wo, we, vo = _both(src)
    assert wo == vo, f"{src!r}\n树遍历={wo!r}\n字节码={vo!r}\n{we}"


@need_rust
def test_树遍历用的是共用那张内建表():
    """🔴 这是这一轮改动的**全部意义**：内建只有一份实现。

    结构钉住：`walk.rs` 必须调 `new_builtins()`，而且**不许有注册内建的动作**
    （一旦有人图省事在树遍历里再写一份 `"打印" =>`，两份实现就开始漂了）。
    """
    walk = (ROOT / "rust" / "src" / "walk.rs").read_text(encoding="utf-8")
    assert "new_builtins()" in walk, "树遍历没用共用的内建表"
    assert "Val::Builtin" in walk, "树遍历没走 Val::Builtin 这条路"
    # ⚠️ 别按「内建名字符串在不在文件里」判 —— `"类型"` 同时是 AST 的字段名
    #    （`n.get("类型")`），那样会假报。**按注册动作**判才对。
    assert "m.insert(" not in walk, "walk.rs 里在自建内建表"
    assert 'Val::Builtin("' not in walk, "walk.rs 里在注册内建（内建只该有一份）"


@need_rust
def test_接口故意做小_只有六个方法():
    """`BuiltinHost` 的**方法数**要钉住。

    每加一个方法，树遍历侧就多一处实现负担（它只实现了 `format_value` /
    `write_output` / `make_iter` 那几处，其余明确报「还没实现」）。
    加方法不是不行 —— 但要**有意识地**加，所以这条会先报红。

    📌 **第六个方法 `write_stderr` 是 2026-10-05 有意识加的**：`测试` 模块的
    `汇总` 把失败明细写 **stderr**、汇总行写 stdout（Python 侧就是这个口径），
    宿主只有一个 stdout 缓冲就对不上。加它的时候这条测试照设计报红了一次。
    """
    lib = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    m = re.search(r"pub\(crate\) trait BuiltinHost \{(.*?)\n\}", lib, re.S)
    assert m, "lib.rs 里找不到 BuiltinHost 的声明"
    names = sorted(re.findall(r"fn (\w+)\(", m.group(1)))
    assert names == sorted(["format_value", "lookup_method", "make_iter",
                            "call_value", "write_output", "write_stderr"]), names


@need_rust
def test_两个执行器都实现了这套接口():
    """`VM` 与 `Walker` 都必须实现 `BuiltinHost`（否则编译就过不了，这条是双保险）。"""
    lib = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    walk = (ROOT / "rust" / "src" / "walk.rs").read_text(encoding="utf-8")
    assert "impl BuiltinHost for VM {" in lib
    assert "impl BuiltinHost for Walker {" in walk


@need_rust
def test_报错前已经打印的内容要吐出来():
    """🔴 字节码 VM 在 R6.2 统一过：**出错前写进输出流的要吐出来**。

    参考执行器一度把「已产生的输出」丢了（`run_walk` 返回 `Result<String,_>`
    而不是元组）—— 于是 `02_计算器` 那种「先打印、再读输入读到 EOF 报错」的
    程序，树遍历 stdout 是空的、字节码 VM 有半截，互拍工具立刻判成不一致。
    """
    doc = json.loads((BASELINE / "examples__tutorial__02_计算器.json")
                     .read_text(encoding="utf-8"))
    ast_p = _tmp(doc["ast"], "_m76_calc_ast.json")
    bc_p = _tmp(doc["bytecode"], "_m76_calc_bc.json")
    wo, we = _run(["--walk", str(ast_p)])
    vo, _ = _run(["--load-bytecode", str(bc_p)])
    assert wo != "", "出错前的输出被丢了"
    assert we != "", "这个程序本来就该报错（读到 EOF）"
    assert wo == vo, f"树遍历={wo!r}\n字节码={vo!r}"


@need_rust
def test_找不到名字的候选名池含内建名():
    """`长渡` 要提示「你是不是想写「长度」？」—— 而「长度」**只作为内建存在**，
    本文件里从没定义过它，所以候选池必须是「已定义的名字 + **内建名**」。

    ⚠️ 这条正是 R6 主体上一轮（字节码 VM 把 `globals` 改成数组）踩过的坑：
    池子被缩小成「本文件里出现过的名字」→ 提示直接没了。
    """
    _, we, _ = _both("打印(长渡(1))\n")
    assert "找不到名字" in we, we
    assert "长度" in we, f"候选提示没带上内建名：{we!r}"


@need_rust
def test_进度仪表零不一致():
    """`tools/check_walk.py`：允许「还没实现」，**不允许「不一致」**。"""
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_walk.py")],
                       capture_output=True, cwd=str(ROOT))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")[-800:]
    assert "不一致 0" in out, out
    # ⚠️ 判「至少涨到哪」而不是「正好等于几」—— 后面每补一步这个数都会变。
    # 内建调用接上那一步落地时是 11（第 ① 步之后是 25）。
    m = re.search(r"一致 (\d+)", out)
    assert m and int(m.group(1)) >= 11, out
