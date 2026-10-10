# -*- coding: utf-8 -*-
"""R5.1 · 「尝试」块的作用域 + 两个执行器独立性缺口 —— 四个既有 bug 的守门测试。

R5（前端换 Rust）是**逐行移植**，而逐行移植 = 一次最彻底的代码审查：
为了把每一行搬对，必须读懂它。读懂之后就看见了四个**与 R5 无关**的既有缺口
（详见 `docs/设计决策.md` D59、`docs/前端契约.md` §12.4）：

| # | 在哪 | 症状 |
|---|---|---|
| ① | `compiler._collect_stmt` 没有 `Try` 分支 | 「尝试/捕获/最终」里的赋值被编成**全局**、闭包变量**捕获不到**、块内**定义函数/类直接崩** |
| ② | `vm.Builder` 的 `BUILD_CLASS` 用了局部名 `base` | 撞掉帧的**栈基址** → 「**在函数里定义类**」必然炸（VM 独有） |
| ③ | `interpreter._do_try` 给「捕获 … 为 名字」建子作用域 | **捕获体里的赋值出了捕获体就没了**（树遍历独有） |
| ④ | 调用链挂载时机不一 | 「**显式 `抛出`** 的错误」在树遍历 / C VM 下**没有调用链**（除零那种有） |

这个文件的立场：**这些缺口一个都不许回来**。判据不是「看起来对」，而是
「**两个前端 × 三个执行器**跑出来逐字节相同」—— 这是本项目的第 1 条硬约束。
"""

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import tokenizer as T                                   # noqa: E402
from oracle.jishi.compiler import compile_source                          # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree               # noqa: E402
from oracle.jishi.vm import VM                                            # noqa: E402


def _have_rust() -> bool:
    return T.rust_available()


def _have_cvm() -> bool:
    try:
        from oracle.jishi import cvm_bind
        cvm_bind.load_lib()
        return True
    except Exception:                                              # noqa: BLE001
        return False


HAS_RUST = _have_rust()
HAS_CVM = _have_cvm()

# ---------------------------------------------------------------------------
# 工具：在两个前端 × 三个执行器上跑同一段源码，逐字节比
# ---------------------------------------------------------------------------

_SAVED_STDIN = None


def _pinned_stdin():
    """钉死 stdin：`输入()` 会去读终端，不钉就会把测试挂住。"""
    global _SAVED_STDIN
    if _SAVED_STDIN is None:
        _SAVED_STDIN = sys.stdin
    sys.stdin = io.StringIO("")


def _unpin_stdin():
    if _SAVED_STDIN is not None:
        sys.stdin = _SAVED_STDIN


def _run(fn) -> str:
    """跑一次，返回 stdout（正常）或**完整报错文本**（出错）。"""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()
    except BaseException as e:                                     # noqa: BLE001
        # 报错文本本身就是可观察行为：把首行类型 + 渲染出的中文报错一起收进来
        return buf.getvalue() + "「报错」" + type(e).__name__ + ":" + str(e)


def _all_engines(src: str, filename: str = "冒烟.jsh") -> dict:
    """`{前端: (树遍历, Python VM, C VM)}` —— 前端用环境变量切。"""
    got = {}
    for front in ("python", "rust"):
        if front == "rust" and not HAS_RUST:
            continue
        old = os.environ.get(T.FRONTEND_ENV)
        os.environ[T.FRONTEND_ENV] = front
        try:
            row = [
                _run(lambda: run_tree(src, filename)),
                _run(lambda: VM(compile_source(src, filename), filename).run()),
            ]
            if HAS_CVM:
                from oracle.jishi import cvm_bind
                row.append(_run(lambda: cvm_bind.run_source_c(src, filename)))
            got[front] = tuple(row)
        finally:
            if old is None:
                os.environ.pop(T.FRONTEND_ENV, None)
            else:
                os.environ[T.FRONTEND_ENV] = old
    return got


def _assert_agree(src: str, filename: str = "冒烟.jsh"):
    """两个前端 × 三个执行器，六个结果逐字节相同。"""
    _pinned_stdin()
    try:
        got = _all_engines(src, filename)
    finally:
        _unpin_stdin()
    names = ("树遍历", "Python VM") + (("C VM",) if HAS_CVM else ())
    ref_front, ref = next(iter(got.items()))
    for front, row in got.items():
        for name, a, b in zip(names, ref, row):
            assert a == b, (
                f"两个前端 / 执行器不一致：{front} 的「{name}」\n"
                f"  参照（{ref_front}）: {a!r}\n  实际（{front}）  : {b!r}\n"
                f"源码：\n{src}")
    return ref


# ---------------------------------------------------------------------------
# ① 「尝试 / 捕获 / 最终」里的名字收集
# ---------------------------------------------------------------------------

#: `(说明, 源码, 期望的 stdout)` —— 每个都配了**模块级同名变量**，
#: 跑完再看它有没有被改掉（不然全局与局部恰好等价，缺口看不出来）。
_TRY_SCOPE_CASES = [
    ("尝试里赋值是局部",
     "令 甲 = 100\n"
     "函数 f()：\n"
     "    尝试：\n"
     "        令 甲 = 1\n"
     "    捕获 值错误：\n"
     "        令 忽略 = 0\n"
     "    返回 甲\n"
     "打印(f())\n打印(甲)\n",
     "1\n100\n"),

    ("收尾块里赋值是局部",
     "令 甲 = 100\n"
     "函数 f()：\n"
     "    尝试：\n"
     "        令 忽略 = 0\n"
     "    最终：\n"
     "        令 甲 = 9\n"
     "    返回 甲\n"
     "打印(f())\n打印(甲)\n",
     "9\n100\n"),

    ("捕获体里赋值是局部",
     "令 甲 = 100\n"
     "函数 f()：\n"
     "    尝试：\n"
     "        抛出 值错误(\"x\")\n"
     "    捕获 值错误：\n"
     "        令 甲 = 5\n"
     "    返回 甲\n"
     "打印(f())\n打印(甲)\n",
     "5\n100\n"),

    ("闭包变量只在尝试里读",
     "函数 外()：\n"
     "    令 外层的 = 42\n"
     "    函数 内()：\n"
     "        尝试：\n"
     "            返回 外层的\n"
     "        捕获 值错误：\n"
     "            返回 0\n"
     "    返回 内()\n"
     "打印(外())\n",
     "42\n"),

    ("抛出的表达式用闭包变量",
     "函数 外()：\n"
     "    令 消息 = \"炸弹\"\n"
     "    函数 内()：\n"
     "        尝试：\n"
     "            抛出 值错误(消息)\n"
     "        捕获 值错误 为 接住的：\n"
     "            返回 长度(文本(接住的)) > 0\n"
     "    返回 内()\n"
     "打印(外())\n",
     "真\n"),

    ("块内定义函数",
     "函数 外()：\n"
     "    尝试：\n"
     "        函数 加倍(n)：\n"
     "            返回 n * 2\n"
     "    捕获 值错误：\n"
     "        令 忽略 = 0\n"
     "    返回 加倍(21)\n"
     "打印(外())\n",
     "42\n"),

    ("块内定义类",
     "函数 外()：\n"
     "    尝试：\n"
     "        类 盒子：\n"
     "            令 值 = 7\n"
     "    捕获 值错误：\n"
     "        令 忽略 = 0\n"
     "    返回 盒子.值\n"
     "打印(外())\n",
     "7\n"),

    ("捕获名是局部（不泄漏到外层）",
     "令 接住的 = \"外面的\"\n"
     "函数 f()：\n"
     "    尝试：\n"
     "        抛出 值错误(\"x\")\n"
     "    捕获 值错误 为 接住的：\n"
     "        返回 长度(文本(接住的)) > 0\n"
     "    返回 假\n"
     "打印(f())\n打印(接住的)\n",
     "真\n外面的\n"),
]


@pytest.mark.parametrize(
    "case", _TRY_SCOPE_CASES, ids=[c[0] for c in _TRY_SCOPE_CASES])
def test_尝试块里的名字收集(case):
    """① 的守门：六个结果（两前端 × 三执行器）逐字节一致，且等于期望输出。"""
    _name, src, expect = case
    got = _assert_agree(src)
    assert got[0] == expect, f"输出不对：{got[0]!r} ≠ {expect!r}"


def test_尝试里的赋值不再漏成全局():
    """把 ① 的根因**正面钉住**：那句 `令 甲 = 1` 必须是 STORE_FAST，不是 STORE_GLOBAL。

    只看输出会漏 —— 输出一样也可能是因为「全局恰好等价」。所以直接反汇编，
    盯着那条指令。这是 ① 唯一的「机器可读」判据。
    """
    from oracle.jishi import opcodes as O

    src = ("函数 f()：\n"
           "    尝试：\n"
           "        令 甲 = 1\n"
           "    捕获 值错误：\n"
           "        令 忽略 = 0\n"
           "    返回 甲\n"
           "打印(f())\n")
    cmod = compile_source(src, "冒烟.jsh")
    fcode = next(c for c in cmod.codes if c.name == "f")
    ops = [O.OP_NAMES[i.op] for i in fcode.instrs]
    assert "STORE_GLOBAL" not in ops, (
        "「尝试」里的赋值又漏成全局了：\n  " + "\n  ".join(ops))
    assert "STORE_FAST" in ops, ops
    assert "甲" in fcode.local_names, fcode.local_names


def test_块内定义函数不再崩():
    """① 的第三副面孔：`函数` / `类` 写在「尝试」里时，`func_scopes` 必须登记。

    没登记的症状是**发射期 KeyError**（编译期直接崩），比「报错」严重得多 ——
    用户写一个很自然的程序就撞上，而且报的是个光秃秃的整数。
    """
    for src in (
        "函数 外()：\n    尝试：\n        函数 内()：\n            返回 1\n"
        "    捕获 值错误：\n        令 忽略 = 0\n    返回 内()\n",
        "函数 外()：\n    尝试：\n        类 盒：\n            令 值 = 1\n"
        "    捕获 值错误：\n        令 忽略 = 0\n    返回 盒.值\n",
    ):
        cmod = compile_source(src, "冒烟.jsh")      # 不该抛异常
        assert len(cmod.codes) >= 2, cmod.codes


# ---------------------------------------------------------------------------
# ② 「在函数里定义类」—— VM 独有：`base` 撞掉帧的栈基址
# ---------------------------------------------------------------------------

_CLASS_IN_FUNC_CASES = [
    ("函数里定义类 + 类变量",
     "函数 外()：\n"
     "    类 盒子：\n"
     "        令 值 = 7\n"
     "    返回 盒子.值\n"
     "打印(外())\n",
     "7\n"),

    ("函数里定义类 + 方法",
     "函数 外()：\n"
     "    类 盒子：\n"
     "        函数 取(自身)：\n"
     "            返回 7\n"
     "    返回 盒子().取()\n"
     "打印(外())\n",
     "7\n"),

    ("函数里定义类 + 之后还有调用",
     "函数 外(n)：\n"
     "    类 盒子：\n"
     "        令 值 = 7\n"
     "    打印(\"在函数里\", n)\n"
     "    返回 盒子.值 + n\n"
     "打印(外(1))\n",
     "在函数里 1\n8\n"),

    ("类在如果里",
     "函数 外()：\n"
     "    如果 真：\n"
     "        类 盒子：\n"
     "            令 值 = 7\n"
     "    返回 盒子.值\n"
     "打印(外())\n",
     "7\n"),

    ("类在循环里",
     "函数 外()：\n"
     "    令 和 = 0\n"
     "    循环 2 次：\n"
     "        类 盒子：\n"
     "            令 值 = 7\n"
     "        和 += 盒子.值\n"
     "    返回 和\n"
     "打印(外())\n",
     "14\n"),

    ("类在尝试里",
     "函数 外()：\n"
     "    尝试：\n"
     "        类 盒子：\n"
     "            令 值 = 7\n"
     "    捕获 值错误：\n"
     "        令 忽略 = 0\n"
     "    返回 盒子.值\n"
     "打印(外())\n",
     "7\n"),
]


@pytest.mark.parametrize(
    "case", _CLASS_IN_FUNC_CASES, ids=[c[0] for c in _CLASS_IN_FUNC_CASES])
def test_函数里定义类(case):
    """② 的守门：`BUILD_CLASS` 不许再动帧的栈基址。

    症状曾经是「函数返回后调用方拿到**上一个值**」—— 错误信息还指着调用行，
    看起来像「打印不能调用」这种莫名其妙的话（`del stack[None:]` 清空了整个栈）。
    ⚠️ **模块级定义类侥幸没事**（模块帧以 HALT 结束、不执行 RETURN），
    所以以前没人发现；这份用例专门盯「**在函数里**」这个形状。
    """
    _name, src, expect = case
    got = _assert_agree(src)
    assert got[0] == expect, f"输出不对：{got[0]!r} ≠ {expect!r}"


# ---------------------------------------------------------------------------
# ③ 「捕获 … 为 名字」不改变作用域
# ---------------------------------------------------------------------------

def test_捕获体里的赋值能出捕获体():
    """③ 的守门：树遍历曾经给捕获体单独建子作用域，赋值出了捕获体就没了。

    判据就是「捕获体里赋的值，捕获体之后读得到」—— 另外两个执行器一直是这么做的。
    """
    src = ("函数 f()：\n"
           "    尝试：\n"
           "        抛出 值错误(\"x\")\n"
           "    捕获 值错误 为 e：\n"
           "        令 结果 = 1\n"
           "    返回 结果\n"
           "打印(f())\n")
    got = _assert_agree(src)
    assert got[0] == "1\n", f"捕获体里的赋值又丢了：{got[0]!r}"


# ---------------------------------------------------------------------------
# ④ 错误必须有调用链 —— 显式 `抛出` 与运行期错误一视同仁
# ---------------------------------------------------------------------------

_TRACE_CASES = [
    ("未捕获·嵌套调用里除零",
     "函数 甲()：\n    返回 乙()\n函数 乙()：\n    返回 1 / 0\n甲()\n"),

    ("未捕获·嵌套调用里抛",
     "函数 甲()：\n    返回 乙()\n函数 乙()：\n    抛出 值错误(\"x\")\n甲()\n"),

    ("捕获后 文本(e)·同帧",
     "函数 f()：\n"
     "    尝试：\n"
     "        抛出 值错误(\"x\")\n"
     "    捕获 值错误 为 e：\n"
     "        令 忽略 = 0\n"
     "    返回 文本(e)\n"
     "打印(f())\n"),

    ("捕获后 文本(e)·嵌套",
     "函数 乙()：\n    抛出 值错误(\"x\")\n"
     "函数 甲()：\n"
     "    尝试：\n"
     "        返回 乙()\n"
     "    捕获 值错误 为 e：\n"
     "        返回 文本(e)\n"
     "打印(甲())\n"),
]


@pytest.mark.parametrize(
    "case", _TRACE_CASES, ids=[c[0] for c in _TRACE_CASES])
def test_报错文本逐字一致含调用链(case):
    """④ 的守门：**报错文本本身就是可观察行为**，六个结果要逐字节相同。

    特别是「显式 `抛出`」那条路 —— 树遍历与 C VM 曾经都丢掉调用链，而
    「除零」那种运行期错误有。同一种错误、两种长相，会让用户以为
    「为什么这个错不告诉我调到哪里了」。
    """
    _name, src = case
    got = _assert_agree(src)
    assert "错误" in got[0], got[0]


def test_显式抛出的错误也有调用链():
    """④ 的正面判据：把「**抛出**」写出来，也得看到调用链（与除零一致）。"""
    src = ("函数 甲()：\n    返回 乙()\n"
           "函数 乙()：\n    抛出 值错误(\"x\")\n甲()\n")
    got = _assert_agree(src)
    assert "调用链（从外到内）" in got[0], f"没有调用链：\n{got[0]}"


# ---------------------------------------------------------------------------
# 整份语料过一遍三个执行器（语料只管前端的产物，这里补运行期那一半）
# ---------------------------------------------------------------------------

def test_语料在三个执行器上都一致():
    """`tests/cases/17_尝试块作用域.jsh` 在两个前端 × 三个执行器上逐字节一致。

    ⚠️ 夹具（`conformance.py`）比的是**前端产物**；「跑起来一样」是另一件事，
    只有真跑才知道 —— ②（类在函数里）就是这么漏过去的。
    """
    src = (ROOT / "tests" / "cases" / "17_尝试块作用域.jsh").read_text(
        encoding="utf-8")
    expect = (ROOT / "tests" / "cases" / "17_尝试块作用域.out").read_text(
        encoding="utf-8")
    got = _assert_agree(src, "17_尝试块作用域.jsh")
    assert got[0] == expect, f"与黄金输出不一致：\n{got[0]!r}\n≠\n{expect!r}"
