# -*- coding: utf-8 -*-
"""R5（新 M62）· Rust 编译器的守门测试。

R5 要证四件事：

1. **字节码逐字节一致** —— 与 Python 编译器产出的 JSON **逐字节**相同
   （连 `checksum` 一起：它是 payload 的 SHA-256，差一个字节就不一样）。
   判据是 `tools/conformance.py compare --stage bytecode` 全绿，本文件把它
   跑成一条测试，并补上随机源码对拍。
2. **两个前端下三个执行器行为一致** —— 字节码一致还不够：`compile_source()`
   交出去的是 `CompiledModule` 对象，里面有一项**不在 JSON 里**的字段
   （`local_hints`）。只给 JSON 的话那句中文提示会静默消失，两个前端就有
   语义差异了 —— 所以这里专门验它还在。
3. **指令表与浮点写法不许漂** —— Rust 侧是那份编号的第**三**份拷贝
   （另两份是两个宿主），浮点的 JSON 文本要等于 Python 的 `repr`。
   这两样都「不能靠眼看」，只能拿数据对拍。
4. **`fuse_method_call` 旗标**（C VM 专属指令）也要跟着一起搬对。
"""

import json
import os
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import conformance as C                                            # noqa: E402
from jishi import compiler as _C                                   # noqa: E402
from jishi import opcodes as O                                     # noqa: E402
from jishi import tokenizer as T                                   # noqa: E402
from jishi.errors import JishiError                                 # noqa: E402


def _have_rust() -> bool:
    return T.rust_available()


need_rust = pytest.mark.skipif(
    not _have_rust(),
    reason="Rust 前端未构建（跑 `python core/build.py`）—— 与其假装通过，不如明确跳过")


def _core():
    return T._core()


def _both(src: str, filename: str, *, fuse: bool = False):
    """按两个前端各编译一次，返回 `("ok", json文本)` 或 `("err", 指纹)`。"""
    got = {}
    for front in ("python", "rust"):
        old = os.environ.get(T.FRONTEND_ENV)
        os.environ[T.FRONTEND_ENV] = front
        try:
            try:
                got[front] = ("ok", _C.compile_to_json(src, filename,
                                                       fuse_method_call=fuse))
            except JishiError as e:
                got[front] = ("err", (type(e).__name__, e.code, e.line, e.col,
                                      str(e)))
        finally:
            if old is None:
                os.environ.pop(T.FRONTEND_ENV, None)
            else:
                os.environ[T.FRONTEND_ENV] = old
    return got


def _corpus() -> "list[str]":
    return [rel for _cat, rel in C.collect_corpus()]


# ---------------------------------------------------------------------------
# 一、与基线 / 与 Python 前端一致
# ---------------------------------------------------------------------------

@need_rust
def test_与基线一致():
    """全语料在 `bytecode` 阶段与冻结基线一致（R5 的验收入口）。"""
    outs = {}
    for rel in _corpus():
        src = (ROOT / rel).read_text(encoding="utf-8")
        outs[rel] = _both(src, rel)
    bad = [rel for rel, got in outs.items()
           if got["python"] != got["rust"]]
    assert not bad, f"两个前端的字节码不一致：{bad[:5]}"


@need_rust
def test_字节码逐字节一致():
    """不只是「JSON 相等」，而是**文本逐字节**相等（含 checksum）。"""
    bad: "list[str]" = []
    for rel in _corpus():
        src = (ROOT / rel).read_text(encoding="utf-8")
        got = _both(src, rel)
        if got["python"] != got["rust"]:
            py, rs = got["python"], got["rust"]
            detail = ""
            if py[0] == rs[0] == "ok":
                a, b = py[1], rs[1]
                for i, (x, y) in enumerate(zip(a, b)):
                    if x != y:
                        detail = f"首处差异 @{i}：py…{a[max(0, i - 60):i + 60]!r} rs…{b[max(0, i - 60):i + 60]!r}"
                        break
                else:
                    detail = f"长度不同：{len(a)} vs {len(b)}"
            bad.append(f"{rel}：{detail or got}")
    assert not bad, "字节码不是逐字节一致：\n  · " + "\n  · ".join(bad[:5])


@need_rust
def test_随机源码对拍():
    """随机源码对拍 —— 固定几个种子，几万份。

    ⚠️ 语料是「发现了才补的」，随机对拍负责抓**没想到要测**的形状。
    两边**只有一种合法结果**：要么都编译成功且字节码逐字节相同，
    要么都报同一个错（同一处行列、同一句文案）。
    """
    from test_m60_parser_rust import _FUZZ_ATOMS as atoms      # noqa: F401
    rnd = random.Random(20261002)
    bad = 0
    for _ in range(30000):
        n = rnd.randint(1, 40)
        src = "".join(rnd.choice(atoms) for _ in range(n))
        if rnd.random() < 0.75 and not src.endswith("\n"):
            src += "\n"
        got = _both(src, "随机.jsh")
        if got["python"] != got["rust"]:
            bad += 1
            if bad <= 3:
                print("对拍不一致：", repr(src), "\n  py:", str(got["python"])[:200],
                      "\n  rs:", str(got["rust"])[:200])
    assert bad == 0, f"{bad} 处不一致"


@need_rust
def test_方法调用融合旗标():
    """`fuse_method_call=True`（C VM 专属的 `METHOD_CALL`）也要一致。"""
    srcs = [
        # ⚠️ `X.追加(单参)` 走的是**更早**的 LIST_APPEND 优化（M11），
        # 压根轮不到融合 —— 两边的行为都要照做，别拿它当融合的例子。
        "令 甲 = [1, 2]\n甲.追加(3)\n打印(甲)\n",
        "令 甲 = 甲\n甲.叫(3)\n",
        "打印(文本(1).长度())\n",
    ]
    fused = 0
    for src in srcs:
        got = _both(src, "融合.jsh", fuse=True)
        assert got["python"] == got["rust"], f"{src!r} 融合产物不一致"
        if got["python"][0] != "ok":
            continue
        body = json.loads(got["python"][1])
        ops = [ins[0] for cd in body["payload"]["codes"]
               for ins in [cd["instrs"][i:i + 6]
                           for i in range(0, len(cd["instrs"]), 6)]]
        fused += ops.count(O.Op.METHOD_CALL)
    assert fused, "一个 METHOD_CALL 都没融合出来 —— 旗标没起作用"


# ---------------------------------------------------------------------------
# 二、`local_hints`：不在 JSON 里、却影响报错文案的那一项
# ---------------------------------------------------------------------------

_SHADOW_SRC = """令 甲 = 1
函数 f()：
    打印(甲)
    令 甲 = 2
    返回 甲
f()
"""


@need_rust
def test_局部提示没丢():
    """`Code.local_hints` 不在字节码 JSON 里，但**报错文案要用它**。

    只从 JSON 还原产物的话，「赋值前读局部」那句「外层也有一个叫「甲」的变量…」
    会静默消失 —— 两个前端就有了语义差异。所以 Rust 侧**另给一份真对象**
    （`compile_module`），这里把这条钉住。
    """
    both = {}
    for front in ("python", "rust"):
        old = os.environ.get(T.FRONTEND_ENV)
        os.environ[T.FRONTEND_ENV] = front
        try:
            both[front] = _C.compile_source(_SHADOW_SRC, "影子.jsh")
        finally:
            if old is None:
                os.environ.pop(T.FRONTEND_ENV, None)
            else:
                os.environ[T.FRONTEND_ENV] = old
    for front, cmod in both.items():
        hints = [c.local_hints for c in cmod.codes if c.local_hints]
        assert hints, f"{front} 前端的 local_hints 空了"
        assert "外层也有一个叫「甲」的变量" in json.dumps(hints, ensure_ascii=False), \
            f"{front} 前端的提示文案不对：{hints}"


@need_rust
def test_两个前端的对象产物同形():
    """`compile_source()` 交出的对象也要**逐字段**同形（不只是 JSON 同）。"""
    src = (ROOT / "examples/13_面向对象.jsh").read_text(encoding="utf-8")
    got = {}
    for front in ("python", "rust"):
        old = os.environ.get(T.FRONTEND_ENV)
        os.environ[T.FRONTEND_ENV] = front
        try:
            cmod = _C.compile_source(src, "对象.jsh")
            got[front] = [
                (c.name, c.params, c.param_idx, c.param_local, c.param_cell,
                 c.param_kind, c.nlocals, c.local_names, c.cellvars,
                 c.freevars, c.firstlineno,
                 [(i.op, i.a, i.b, i.c, i.line, i.col, i.stmt_line)
                  for i in c.instrs],
                 sorted(c.local_hints.items()))
                for c in cmod.codes
            ]
        finally:
            if old is None:
                os.environ.pop(T.FRONTEND_ENV, None)
            else:
                os.environ[T.FRONTEND_ENV] = old
    assert got["python"] == got["rust"]


# ---------------------------------------------------------------------------
# 三、两样「不能靠眼看」的东西：指令表、浮点写法
# ---------------------------------------------------------------------------

@need_rust
def test_指令表不漂移():
    """Rust 侧的指令表与 `opcodes.py` 逐项相同（号与名字都算）。"""
    mine = dict(_core().opcode_table())
    assert mine == O.OP_BY_NAME, (
        f"指令表不一致\n  只在 Rust：{set(mine) - set(O.OP_BY_NAME)}\n"
        f"  只在 Python：{set(O.OP_BY_NAME) - set(mine)}\n"
        f"  号不同的是："
        f"{ {k: (mine[k], O.OP_BY_NAME[k]) for k in mine if k in O.OP_BY_NAME and mine[k] != O.OP_BY_NAME[k]} }")


@need_rust
def test_浮点repr与Python一致():
    """`repr(float)` 的 Rust 复刻 —— 拿**数据**对拍，别靠眼看。

    为什么要专门测：字节码 JSON 里的浮点是 `repr` 文本；Rust 的 `{}` 不写指数
    （`1e30` 会印成 30 个 0）、`{:e}` 的指数不补零（`1e-5` vs `1e-05`）。
    科学计数与定点的**分界**（`decpt <= -4 || decpt > 16`）也是 Python 特有的。
    """
    import math

    vals = [0.0, -0.0, 1.0, -1.0, 0.5, 2.5, 1e5, 1e15, 1e16, 1e17, 1e-4, 1e-5,
            0.001, 1e30, 1e-30, 1.5e-7, 9.9e-5, 123456789012345680.0,
            3.141592653589793, 1e308, 5e-324, 2.2250738585072014e-308]
    rnd = random.Random(5)
    for _ in range(4000):
        vals.append(rnd.uniform(-1e6, 1e6))
    for _ in range(4000):
        vals.append(rnd.uniform(-1, 1) * 10 ** rnd.randint(-320, 308))
    for _ in range(2000):
        vals.append(float(rnd.randint(-10 ** 18, 10 ** 18)))
    bad = []
    for v in vals:
        if math.isinf(v) or math.isnan(v):
            continue
        want = repr(v)
        got = _core().float_repr(v)
        if got != want:
            bad.append((v, want, got))
    assert not bad, ("浮点写法不一致（前 5 条）：\n  · "
                     + "\n  · ".join(f"{v!r}: py {w!r} vs rs {g!r}"
                                    for v, w, g in bad[:5]))


@need_rust
def test_编译器能报错而不是崩():
    """语法错要**报同一个错**（Rust 侧的报错必须与 Python 侧逐字相同）。"""
    srcs = ["如果 真：\n\n", "令 甲 = (1 + 2\n", "打印((1)\n", "令 = 1\n"]
    for src in srcs:
        got = _both(src, "坏.jsh")
        assert got["python"] == got["rust"], f"{src!r} 报错不一致：{got}"
        assert got["python"][0] == "err", f"{src!r} 应该报错"


# ---------------------------------------------------------------------------
# 四、端到端：三个执行器在两个前端下跑出来必须一样
# ---------------------------------------------------------------------------

@need_rust
def test_三执行器端到端一致():
    """最硬的一条：**两个前端 × 三个执行器**，跑出来逐字节相同。

    字节码一致 + 对象产物同形 ⇒ 行为一致；这条把它们串起来当真跑一遍。
    """
    import contextlib
    import io as _io

    from jishi import cvm_bind
    from jishi.interpreter import run_source
    from jishi.vm import VM

    srcs = [
        (ROOT / "examples/13_面向对象.jsh").read_text(encoding="utf-8"),
        "令 甲 = [1, 2, 3]\n遍历 项 在 甲：\n    打印(项 * 2)\n",
        "函数 外(n)：\n    令 累 = 0\n    函数 内()：\n        非局部 累\n"
        "        累 += n\n    内()\n    返回 累\n打印(外(7))\n",
    ]

    def run(fn):
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                fn()
            return buf.getvalue()
        except BaseException as e:                                    # noqa: BLE001
            return f"ERR {type(e).__name__}: {e}"

    old_in = sys.stdin
    sys.stdin = _io.StringIO("")        # 钉死：示例可能 `输入()`
    try:
        for src in srcs:
            got = {}
            for front in ("python", "rust"):
                old = os.environ.get(T.FRONTEND_ENV)
                os.environ[T.FRONTEND_ENV] = front
                try:
                    got[front] = (
                        run(lambda: run_source(src, "端到端.jsh")),
                        run(lambda: VM(compile_source_(src), "端到端.jsh").run()),
                        run(lambda: cvm_bind.run_source_c(src, "端到端.jsh")),
                    )
                finally:
                    if old is None:
                        os.environ.pop(T.FRONTEND_ENV, None)
                    else:
                        os.environ[T.FRONTEND_ENV] = old
            assert got["python"] == got["rust"], (
                f"两个前端的三执行器结果不同：\n  py {got['python']}\n  rs {got['rust']}")
    finally:
        sys.stdin = old_in


def compile_source_(src: str):
    """按当前 `JISHI_FRONTEND` 编译成 `CompiledModule`。"""
    return _C.compile_source(src, "端到端.jsh")
