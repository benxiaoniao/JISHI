# -*- coding: utf-8 -*-
"""M55 · 「字符是什么」定下来：内建 `序数`/`字符` + 文本按码点 + 内建的账。

## 这个文件钉什么

M55 回语言本体，主线是**把「一个字符」这件事定义清楚** —— 因为 `序数`/`字符`
就是「码点 ↔ 字符」，它们要成立，先得五个引擎对「一个字符」有同一个答案。

三块：

1. **`序数` / `字符`**：`序数("中")` 给 20013、`字符(20013)` 给 `"中"`，
   互为反函数，五个引擎逐字节一致（含错误路径）。
2. **文本按码点**：M55 实测发现 **Node 侧把文本当 UTF-16 码元** ——
   `长度("😀")` 给 2（Python / Rust 给 1）、`"😀"[0]` 只拿到半个代理项
   （打出来是 `�`）、`"a😀b".查找("b")` 给 3（另两个给 2）。已修。
3. **内建的账**：`--dump-builtins` 检测当场抓到 `断言` / `是实例` 在
   Node 与 Rust 上**根本没有**（报「找不到名字」），`断言错误` 这个捕获类型
   也缺着。都补齐了；参数个数不对时 Rust 会 **panic 崩溃**、Python 漏
   **英文原生错误**，也一并收口（详见 `tests/test_m51_consistency.py` 的
   「一之三」节与 `docs/设计决策.md` D46）。
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path, tmp_bc_path      # noqa: E402

from jishi import serialize                          # noqa: E402
from jishi import vm as vm_mod                       # noqa: E402
from jishi.compiler import compile_source            # noqa: E402
from jishi.errors import JishiError                  # noqa: E402
from jishi.interpreter import run_source as tree_run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
RUST = rust_exe_path()


def _capture(fn, *a) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


def _host_run(exe: list, src: str, tag: str) -> tuple:
    bc = tmp_bc_path(ROOT / f"_tmp_m55_{tag}.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    r = subprocess.run(exe + [str(bc)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def _engines(tag: str = "a"):
    """五引擎：`(名字, 跑一段源码 → (是否正常退出, 输出或错误文本))`。"""
    def wrap(fn):
        def go(src):
            try:
                return True, fn(src)
            except JishiError as e:
                return False, f"{getattr(e, 'title', '')} {e}"
        return go

    def pyvm(src):
        return _capture(lambda: vm_mod.VM(compile_source(src, "<t>"), "<t>").run())

    def cvm(src):
        from jishi import cvm_bind
        return _capture(cvm_bind.run_source_c, src, "<t>")

    out = [("树遍历", wrap(lambda s: _capture(tree_run, s))),
           ("Python VM", wrap(pyvm)), ("C VM", wrap(cvm))]
    if NODE:
        out.append(("Node", lambda s: _host_run(
            [NODE, str(ROOT / "node" / "index.js")], s, tag)))
    if RUST.exists():
        out.append(("Rust", lambda s: _host_run([str(RUST)], s, tag)))
    return out


def _five_agree(src: str) -> str:
    """五个引擎 stdout 逐字节一致且都正常退出；返回那份输出。"""
    base_label, base = None, None
    for label, run in _engines():
        ok, out = run(src)
        assert ok, f"{label} 没跑通：{out[:200]}"
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, (
                f"{label} 与 {base_label} 输出不一致\n"
                f"{base_label}: {base!r}\n{label}: {out!r}")
    return base


# ---------------------------------------------------------------------------
# 一、序数 / 字符
# ---------------------------------------------------------------------------

def test_序数与字符互为反函数():
    src = ('打印(序数("中"), 序数("A"), 序数("0"), 序数(" "))\n'
           '打印(字符(20013), 字符(65), 字符(48), 字符(32))\n'
           '打印(字符(序数("基")) == "基", 序数(字符(20013)) == 20013)\n')
    assert _five_agree(src) == "20013 65 48 32\n中 A 0  \n真 真\n"


def test_非BMP字符的序数():
    """`😀` 是**一个**码点（128512）—— 不是两个 UTF-16 码元。

    这条正是 M55 修 Node 的原因：JS 的 `"😀".length` 是 2。
    """
    src = ('令 脸 = "😀"\n'
           '打印(长度(脸), 序数(脸))\n'
           '打印(字符(序数(脸)) == 脸)\n')
    assert _five_agree(src) == "1 128512\n真\n"


def test_序数的错误路径一致():
    """三执行器同为 E 码异常；两宿主彼此同类型（跨语言宿主的错误目录不同，见 M54）。"""
    cases = {
        "空文本": '序数("")',
        "两个字符": '序数("中文")',
        "整数": '序数(65)',
        "码点负数": '字符(-1)',
        "码点超上限": '字符(1114112)',
        "文本": '字符("中")',
        "布尔": '字符(真)',
        "小数": '字符(1.5)',
    }
    for name, expr in cases.items():
        outs = {}
        for label, run in _engines("e"):
            ok, out = run(f"打印({expr})\n")
            assert not ok, f"{label} 本该报错却跑通了（{name}）"
            outs[label] = out
        for label in ("树遍历", "Python VM", "C VM"):
            assert "E2002" in outs[label] or "E2005" in outs[label], (
                f"{name}：{label} 报的不是类型/值错误 —— {outs[label][:140]!r}")
        first = {l: outs[l].strip().splitlines()[0]
                 for l in ("Node", "Rust") if l in outs}
        if len(first) == 2:
            assert first["Node"] == first["Rust"], (
                f"{name}：两宿主报错首行不同\nNode: {first['Node']}\nRust: {first['Rust']}")


def test_字符不收布尔():
    """布尔是**独立类型**（与位运算内建同一取舍）——

    Python 里 `chr(True)` 会静默给出 `"\\x01"`，那太意外了。
    """
    outs = {}
    for label, run in _engines("b"):
        ok, out = run('打印(字符(真))\n')
        assert not ok, f"{label} 居然收下了布尔"
        outs[label] = out
    assert "布尔" in outs["树遍历"], outs["树遍历"][:140]


# ---------------------------------------------------------------------------
# 二、文本按码点（Node 的 UTF-16 修正）
# ---------------------------------------------------------------------------

def test_长度与下标都按码点():
    """⚠️ 这是 M55 修的**真 bug**：Node 侧 `长度("😀")` 曾给 2。

    JS 字符串原生是 UTF-16，`"😀".length === 2`、`"😀"[0]` 只是半个代理项
    （打印出来是 `�`）。切片与遍历**本来就对**（用的 `[...s]` / `for…of`），
    漏的是 `长度` / 单下标 / `查找` / `反转` 四处。
    """
    src = ('令 脸 = "😀"\n'
           '打印(长度(脸), 长度("中文"), 长度("a😀b"))\n'
           '打印(脸[0], "a😀b"[1], "a😀b"[2])\n'
           '打印("中文"[1])\n')
    assert _five_agree(src) == "1 2 3\n😀 😀 b\n文\n"


def test_查找给的是码点下标():
    """`查找` 返回**码点**下标（对齐 Python 的 `str.find`），不是 UTF-16 下标。"""
    src = ('打印("a😀b".查找("b"), "你好吗".查找("吗"), "abc".查找("z"))\n'
           '打印("😀x".查找("x"), "".查找("a"))\n')
    assert _five_agree(src) == "2 2 -1\n1 -1\n"


def test_文本反转按码点():
    """`反转("a😀b")` 给 `b😀a` —— 不是把 😀 拆成两个半代理项再反。"""
    src = '打印(反转("a😀b"), 反转("中文"), 反转(""))\n'
    assert _five_agree(src) == "b😀a 文中 \n"


def test_遍历与集合把文本当码点序列():
    """这两条**本来就对**（Node 用的是按码点迭代），钉住免得以后改坏。"""
    src = ('打印(集合("a😀b"))\n'
           '遍历 c 在 "a😀b"：\n'
           '    打印(c)\n')
    assert _five_agree(src) == "{'a', '😀', 'b'}\na\n😀\nb\n"


# ---------------------------------------------------------------------------
# 三、内建的账：参数个数不对时不再崩 / 不再漏英文
# ---------------------------------------------------------------------------

def test_参数个数不对时三执行器给中文类型错误():
    """⚠️ M55 修：以前给的是 Python 原生英文 ——

    `_b_len() missing 1 required positional argument: 'v'`
    （英文 + Python 内部函数名，对用户和模型都是天书）。
    现在统一走 `_Builtin.check_arity`，文案与 Rust 侧逐字对齐。
    """
    cases = {
        "长度()": "「长度」需要 1 个参数，但传了 0 个",
        "位与(1)": "「位与」需要 2 个参数，但传了 1 个",
        "打开()": "「打开」需要 1 到 2 个参数，但传了 0 个",
    }
    for expr, want in cases.items():
        outs = {}
        for label, run in _engines("ar"):
            ok, out = run(f"打印({expr})\n")
            assert not ok, f"{label} 本该报错"
            outs[label] = out
        for label in ("树遍历", "Python VM", "C VM"):
            assert want in outs[label], (
                f"{expr}：{label} 文案不对 —— {outs[label][:140]!r}")
        # Rust 侧 M55 也补上了同类检查与同一句文案（宿主渲染前缀不同）
        assert want in outs["Rust"], f"{expr}：Rust 文案不对 —— {outs['Rust'][:140]!r}"


def test_内建参数个数不对时Rust不再崩():
    """⚠️ M55 修：Rust 侧 12 个内建**没有参数个数检查**，少给参数直接 panic。

    `长度()` 会打 `thread 'main' panicked at src/lib.rs:…` —— 那不是报错，
    是**宿主崩溃**（退出码非 0、输出是 Rust 的 panic 信息）。
    """
    for expr in ("长度()", "整数()", "类型()", "反转()", "总和()",
                 "文本()", "小数()", "检查实参()", "最大()", "最小()"):
        ok, out = _host_run([str(RUST)], f"打印({expr})\n", "panic")
        assert not ok, f"{expr} 本该报错"
        assert "panicked" not in out, f"{expr} Rust 仍然 panic：{out[:200]}"
        assert "错误" in out, f"{expr} Rust 报的不是基石错误：{out[:200]}"
