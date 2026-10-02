# -*- coding: utf-8 -*-
"""R6.5 · **负索引**：五个执行器都要认 `列[-1]`。

## 缘起

R6.4 顺带撞见的分叉：三个 Python 执行器底下就是 Python 的 list / str，
**天然支持负索引**（`列[-1]` 是最后一个元素）；而两个宿主**显式要求
`index >= 0`**，于是同一个程序在 Python 上给 `3`、在宿主上报「索引错误」。

语料里从没用过负索引，所以这一步谁也没发现。用户拍板：**让宿主实现它**
（对齐 Python 的语义）。

## 这一轮改了什么

| 形状 | 宿主（改前） | 现在 |
|---|---|---|
| `列[-1]` / `文本[-1]` | 索引错误 | 最后一个元素 |
| `列[-1] = 9` | 索引错误 | 改最后一个元素 |
| `列.弹出(-1)` | 索引错误 | 弹最后一个 |
| `列.插入(-1, 9)` | Rust 给 `[1,2,3,9]`（负数当巨大 usize 后被夹到末尾） | `[1,2,9,3]`（Python 的夹取规则） |
| `列[-4]`（越界） | 索引错误 ✓ | 索引错误（**折算后仍越界才报**，不绕回开头） |
| `文[0] = "丙"` | Rust 少一个类型名 | 「文本」不能按下标赋值 |

⚠️ 还有一条**通用提醒**：负索引折算里「长度」是**元素个数** ——
文本按**码点**数（`"😀"[-1]` 要拿到完整的 😀），不是字节数、也不是 UTF-16 码元数。
"""

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi import cvm_bind                                        # noqa: E402
from jishi.compiler import compile_source, compile_to_json        # noqa: E402
from jishi.interpreter import run_source as run_tree              # noqa: E402
from jishi.vm import VM                                           # noqa: E402

NODE = shutil.which("node")
MK = "~_~"
REL = "build/_m68_src.jsh"

#: 形状名 → `(前置语句, 结果表达式)`；包进小函数里跑（同名变量不冲突）。
SHAPES: dict[str, tuple[str, str]] = {
    "列表读[-1]": ('令 列 = [1, 2, 3]', '列[-1]'),
    "列表读[-2]": ('令 列 = [1, 2, 3]', '列[-2]'),
    "列表读[-3]": ('令 列 = [1, 2, 3]', '列[-3]'),
    "列表读[-4]越界": ('令 列 = [1, 2, 3]', '列[-4]'),
    "文本读[-1]": ('', '"甲乙丙"[-1]'),
    "文本读[-3]": ('', '"甲乙丙"[-3]'),
    "文本读[-4]越界": ('', '"甲乙丙"[-4]'),
    "emoji文本读[-1]": ('', '"a😀b"[-1]'),
    "列表写[-1]": ('令 列 = [1, 2, 3]\n列[-1] = 9', '列'),
    "列表写[-3]": ('令 列 = [1, 2, 3]\n列[-3] = 9', '列'),
    "列表写[-4]越界": ('令 列 = [1, 2, 3]\n列[-4] = 9', '列'),
    "切片[-2:]": ('令 列 = [1, 2, 3, 4]', '列[-2:]'),
    "切片[:-1]": ('令 列 = [1, 2, 3, 4]', '列[:-1]'),
    "切片[::-1]": ('令 列 = [1, 2, 3]', '列[::-1]'),
    "文本切片[-2:]": ('', '"甲乙丙丁"[-2:]'),
    "列表弹出(-1)": ('令 列 = [1, 2, 3]\n列.弹出(-1)', '列'),
    "列表弹出(-9)越界": ('令 列 = [1, 2, 3]', '列.弹出(-9)'),
    "列表插入(-1)": ('令 列 = [1, 2, 3]\n列.插入(-1, 9)', '列'),
    "列表插入(-9)过头": ('令 列 = [1, 2, 3]\n列.插入(-9, 9)', '列'),
    "文本取下标赋值": ('令 文 = "甲乙"\n文[0] = "丙"', '文'),
    "大整数下标转负数": ('令 列 = [1, 2, 3]', '列[-1 * 1]'),
    "字典读[-1]": ('令 字 = {1: "甲"}', '字[-1]'),
}


def _build() -> str:
    parts = []
    for i, (name, (pre, expr)) in enumerate(SHAPES.items()):
        fn = [f"函数 形状{i}()："]
        fn += ["    " + ln for ln in pre.splitlines()]
        fn.append(f"    返回 {expr}")
        body = "".join("    " + ln + "\n" for ln in "\n".join(fn).splitlines())
        parts.append(
            "尝试：\n" + body
            + f'    打印("{name}{MK}" + 文本(形状{i}()).替换("\\n", "⏎"))\n'
            + "捕获 异常 为 错误：\n"
            + f'    打印("{name}{MK}【错误】" + 错误.类型 + ":" + 错误.消息.替换("\\n", "⏎"))\n'
        )
    return "".join(parts)


def _cap(fn) -> str:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
    except BaseException as e:                                     # noqa: BLE001
        return buf.getvalue() + f"\n【没人接住】{type(e).__name__}:{str(e).splitlines()[0]}\n"
    return buf.getvalue()


def _parse(text: str) -> dict[str, str]:
    d: dict[str, str] = {}
    cur = None
    for ln in text.splitlines():
        if MK in ln:
            head, _, rest = ln.partition(MK)
            if head in SHAPES:
                cur = head
                d[cur] = rest
                continue
        if cur is not None:
            d[cur] += "\n" + ln
    return d


@pytest.fixture(scope="module")
def engine_outputs() -> dict[str, dict[str, str]]:
    """五个执行器各跑一次合成程序 → `{引擎: {形状: 结果}}`。"""
    src = _build()
    (ROOT / REL).write_text(src, encoding="utf-8")
    outs: dict[str, str] = {
        "树遍历": _cap(lambda: run_tree(src, REL)),
        "Python VM": _cap(lambda: VM(compile_source(src, REL), REL).run()),
        "C VM": _cap(lambda: cvm_bind.run_source_c(src, REL)),
    }
    tmp = Path(tempfile.gettempdir()) / "_m68.json"
    tmp.write_text(compile_to_json(src, REL), encoding="utf-8")
    rust = ROOT / "rust/target/release/jishi-rs.exe"
    if rust.exists():
        outs["Rust"] = subprocess.run([str(rust), str(tmp)], capture_output=True,
                                      input=b"", cwd=str(ROOT)).stdout.decode("utf-8", "replace")
    if NODE:
        outs["Node"] = subprocess.run([NODE, str(ROOT / "node/index.js"), str(tmp)],
                                      capture_output=True, input=b"",
                                      cwd=str(ROOT)).stdout.decode("utf-8", "replace")
    return {k: _parse(v) for k, v in outs.items()}


def test_负索引全部形状五执行器一致(engine_outputs):
    """21 条形状逐条比 —— 有差异就把「哪几个引擎给了什么」打出来。"""
    bad = []
    for name in SHAPES:
        vals = {eng: d.get(name, "<没这一行>") for eng, d in engine_outputs.items()}
        if len(set(vals.values())) > 1:
            bad.append((name, vals))
    assert not bad, "\n".join(
        [f"❌ {n}\n" + "\n".join(f"     {e}: {v[:100]}" for e, v in vs.items())
         for n, vs in bad])


@pytest.mark.parametrize("name,want", [
    ("列表读[-1]", "3"),
    ("列表读[-3]", "1"),
    ("文本读[-1]", "丙"),
    ("emoji文本读[-1]", "b"),          # ⚠️ 按**码点**数，不是 UTF-16 码元
    ("列表写[-1]", "[1, 2, 9]"),
    ("列表写[-3]", "[9, 2, 3]"),
    ("列表弹出(-1)", "[1, 2]"),
    ("列表插入(-1)", "[1, 2, 9, 3]"),  # Python 的 insert 夹取规则
    ("列表插入(-9)过头", "[9, 1, 2, 3]"),
    ("切片[-2:]", "[3, 4]"),
    ("切片[::-1]", "[3, 2, 1]"),
    ("字典读[-1]", "【错误】键错误:字典里没有键「-1」"),   # 字典的键不做负索引
])
def test_负索引的具体语义(engine_outputs, name, want):
    """逐条钉住「Python 的语义到底是什么」—— 免得以后谁把某一侧改歪了。"""
    for eng, d in engine_outputs.items():
        got = d.get(name, "<没这一行>")
        assert got == want, f"{eng}：{name} 给出 {got!r}，期望 {want!r}"


@pytest.mark.parametrize("name", ["列表读[-4]越界", "文本读[-4]越界",
                                  "列表写[-4]越界", "列表弹出(-9)越界"])
def test_折算完还越界就照常报错(engine_outputs, name):
    """⚠️ 负索引**不是**绕回开头：长度 3 的列表上 `列[-4]` 照样越界。

    这条容易写错成「取模」（`-4 % 3 = 2` 会悄悄给出第 3 个元素）——
    那又是一个「不报错的错答案」。
    """
    for eng, d in engine_outputs.items():
        got = d.get(name, "<没这一行>")
        assert got.startswith("【错误】索引错误"), f"{eng}：{name} 给出 {got!r}"


def test_文本按下标赋值要说出类型名(engine_outputs):
    """两个宿主里 Rust 原来只说「不能按下标赋值」，没说**是哪个东西**
    （R6.2 在「不能取下标」上修过同一类问题，这处当时漏了）。"""
    for eng, d in engine_outputs.items():
        got = d.get("文本取下标赋值", "")
        assert got == "【错误】类型错误:「文本」不能按下标赋值", f"{eng}：{got!r}"
