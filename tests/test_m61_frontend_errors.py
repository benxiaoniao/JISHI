# -*- coding: utf-8 -*-
"""R4（新 M61）· 前端报错的守门测试。

R4 要证三件事：

1. **可达性** —— 错误码表是对用户与模型的**承诺**，所以每个前端码要么有语料钉住，
   要么被**显式登记**为不可达，而且登记得**经得起复核**（不是写一句就算）。
   R4 第一次跑就抓到两笔：E0205 是死码、E0204 一个语料都没有。
2. **事实字段一个都不能丢** —— 扩展回传的七个事实（码 / 消息 / 行 / 列 / 提示 /
   下划线 / 修正）在**两条还原路径**上都要活下来。R4 之前词法那条漏了「修正」，
   而恰好**没有一个词法错误带 `fix`**，于是怎么测都是绿的 —— 这类「两份清单并存、
   恰好没用到」的缺口，只有**照着字段表往返一遍**才抓得住。
3. **两个前端逐字一致** —— 由 `conformance.py compare --stage diagnostics` 管
   （契约 v2 起连 `下划线` / `修正` / **`渲染`** 一起比）。本文件再补两条：
   渲染出来的文本里**不许出现内部表示**；以及 **E0205 接上之后要真的会出现、
   而且出现在对的地方**（它此前是死码 —— 见 `tools/probe_errcodes.py`）。
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import probe_errcodes as PE                                         # noqa: E402
from jishi import errors as E                                       # noqa: E402
from jishi import parser as P                                       # noqa: E402
from jishi import tokenizer as T                                    # noqa: E402


class _FakeExt(Exception):
    """冒牌扩展异常：只按契约挂「事实」属性，别的什么都不做。"""


def _fake_ext(**facts) -> _FakeExt:
    exc = _FakeExt("冒牌扩展错误")
    for k, v in facts.items():
        setattr(exc, k, v)
    return exc


#: 每个事实字段 → 「还原之后从哪里读回来」。表驱动是为了让**加字段的人**
#: 只改 `EXTENSION_FIELDS` 一处，这个测试立刻会盯着他。
_FIELD_GETTER = {
    "码": lambda e: e.code,
    "消息": lambda e: e.message,
    "行": lambda e: e.line,
    "列": lambda e: e.col,
    "提示": lambda e: e.hint,
    "下划线": lambda e: e.underline,
    "修正": lambda e: (e.fix or {}).get("old") == "旧的",
}

_SAMPLE = {
    "码": "E0105", "消息": "冒牌消息", "行": 1, "列": 2,
    "提示": "冒牌提示", "下划线": (2, 3), "修正": ("旧的", "新的"),
}


def test_事实字段表是完整的():
    """`_FIELD_GETTER` 与 `EXTENSION_FIELDS` 必须一一对应。

    这一条看着像「测自己」，其实是在守一件真事：**加字段的人必须同时想清楚
    「怎么验证它活下来了」**。两处对不上就报红，不给「加了字段没人管」留口子。
    """
    assert set(_FIELD_GETTER) == set(E.EXTENSION_FIELDS), (
        f"字段表与验证表对不上：\n  契约 {sorted(E.EXTENSION_FIELDS)}\n"
        f"  验证 {sorted(_FIELD_GETTER)}")


def test_事实字段全都不丢_统一出口():
    """`errors.from_extension` —— 两条路径共用的那一个出口。"""
    err = E.from_extension(_fake_ext(**_SAMPLE), filename="冒烟.jsh",
                           lines=["甲乙丙"])
    for field, get in _FIELD_GETTER.items():
        assert get(err), f"「{field}」在还原时丢了"
    assert err.source_line == "甲乙丙", "源码行没按 `行` 现取"


def test_事实字段全都不丢_语法路径():
    """`parser._make_parse_error` —— 语法那条路（R3 起就在读「修正」）。"""
    err = P._make_parse_error(_fake_ext(**_SAMPLE), ["甲乙丙"], "冒烟.jsh")
    for field, get in _FIELD_GETTER.items():
        assert get(err), f"「{field}」在语法路径上丢了"


def test_事实字段全都不丢_词法路径(monkeypatch):
    """`tokenizer._rust_tokenize` —— **R4 之前这条漏读「修正」**。

    复现手法：把 `_core()` 换成冒牌模块，让它抛一个带全部事实的 `LexError`。
    ⚠️ 这条必须走**真的那条代码路径**（而不是直接调 `from_extension`），
    否则「谁忘了调统一出口」这件事就测不出来了。
    """
    sample = dict(_SAMPLE)

    class _FakeMod:
        LexError = _FakeExt

        @staticmethod
        def tokenize(text, filename):
            raise _fake_ext(**sample)

    monkeypatch.setattr(T, "_core", lambda: _FakeMod)
    with pytest.raises(E.JishiError) as ei:
        T._rust_tokenize("甲乙丙\n", "冒烟.jsh")
    err = ei.value
    assert isinstance(err, E.LexError), "词法错误必须落在词法家族里"
    for field, get in _FIELD_GETTER.items():
        assert get(err), f"「{field}」在词法路径上丢了"


def test_码查不到时落回家族根():
    """扩展报了一个没登记的码 —— 落回 `LexError` / `ParseError`，**别掉到 `JishiError`**。

    「码 → 类」是 Python 侧现查的，所以一个不认识的码不该让词法错误变成
    「未知错误」（那会让调用方的 `except LexError` 漏接）。这里把家族兜底钉住。
    """
    lex = E.from_extension(_fake_ext(码="E9999", 消息="没登记"),
                           filename="冒烟.jsh", lines=["甲"],
                           default_cls=E.LexError)
    par = E.from_extension(_fake_ext(码="E9999", 消息="没登记"),
                           filename="冒烟.jsh", lines=["甲"],
                           default_cls=E.ParseError)
    assert isinstance(lex, E.LexError) and lex.code == "E0100"
    assert isinstance(par, E.ParseError) and par.code == "E0200"


# ---------------------------------------------------------------------------
# 一、可达性：每个前端码要么有语料钉住，要么登记为不可达（且登记经得起复核）
# ---------------------------------------------------------------------------

def test_前端错误码都要有语料或登记():
    codes = PE.front_codes()
    hits = PE.corpus_hits()
    gaps = [c for c in sorted(codes)
            if c not in hits and c not in PE.UNREACHABLE]
    assert not gaps, (
        "这些前端错误码既没有语料钉住、也没登记为不可达："
        f"{gaps}\n"
        "  有语料 → 它会被冻进基线，两个前端必须逐字一致；\n"
        "  不可达 → 在 tools/probe_errcodes.py 的 UNREACHABLE 里写清楚原因。")


def test_不可达登记没过期():
    """「不可达」不是许愿：码没了要删登记，**死码被人接上了也要回去复核**。"""
    problems = PE.verify_unreachable(PE.front_codes())
    assert not problems, "不可达登记经不起复核：\n  · " + "\n  · ".join(problems)


def test_错误码表的承诺是真能兑现的():
    """`errcodes.py` 里「写得出来」的码，必须在实现里**存在**。

    防的是「文档里有一张表，实现里没有这个类」这种反向漂移
    （`tests/test_m56_errcodes.py` 管另一边：类是有的、说明别落下）。
    """
    from jishi import errcodes as EC

    known = set(PE.front_codes())
    documented = {c for c in EC.INFO if c.startswith(PE.FRONT_PREFIXES)}
    assert documented <= known, (
        f"`errcodes.py` 里有实现里不存在的码：{sorted(documented - known)}")


# ---------------------------------------------------------------------------
# 二、面向用户的那一面
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "tests/error_cases/44_缺变量名.jsh",
    "tests/error_cases/45_缺属性名.jsh",
])
def test_新语料的渲染是完整的(rel):
    """R4 补的两个 E0204 语料：源码行与 `^` 下划线都得在。

    ⚠️ 只断言「报了这个码」是不够的 —— R3 就栽在「码对了、下划线丢了」上。
    """
    import contextlib
    import io

    from jishi.interpreter import run_source

    src = (ROOT / rel).read_text(encoding="utf-8")
    with pytest.raises(E.JishiError) as ei:
        with contextlib.redirect_stdout(io.StringIO()):
            run_source(src, filename=rel.split("/")[-1])
    err = ei.value
    assert err.code == "E0204"
    assert err.source_line, "缺源码行"
    assert err.underline, "缺下划线"
    rendered = err.render()
    assert "~^" in rendered, f"渲染里没有下划线：\n{rendered}"


# ---------------------------------------------------------------------------
# 三、E0205：接上一个「死码」之后，要钉住它现在真的会出现、且出现在对的地方
# ---------------------------------------------------------------------------

#: `(源, 期望的右括号, 开括号所在列)` —— 都要求「一直读到文件末尾都没等到右括号」。
_UNCLOSED_CASES = [
    ("令 甲 = (1 + 2\n", ")", 7),         # 圆括号
    ("令 甲 = [1, 2\n", "]", 7),          # 方括号
    ("令 甲 = {“a”: 1\n", "}", 7),        # 花括号
    ("打印((1)\n", ")", 3),               # 嵌套：内层是闭合的，没闭合的是**外层**那个
    ("令 甲 = 【1, 2\n", "]", 7),         # 全角也要归一化后再判
]


@pytest.mark.parametrize("src,closing,col", _UNCLOSED_CASES)
def test_括号没闭合报E0205并指向开括号(src, closing, col):
    """E0205 以前是**死码**（见 `tools/probe_errcodes.py`），2026-10-01 接上。

    两条硬要求：
    1. **位置指在开括号上**，不是 EOF —— EOF 那个 token 的位置是「最后一行第 1 列」、
       `source_line` 还是空的，指着它连源码行都显示不出来；
    2. **提示要说人话**（指哪改哪），而不是让用户自己往回找。
    """
    with pytest.raises(E.JishiError) as ei:
        P.parse_source(src, "冒烟.jsh")
    err = ei.value
    assert err.code == "E0205", f"应该报 E0205，实际 {err.code}：{err.message}"
    assert err.line == 1 and err.col == col, (
        f"应指向开括号（1:{col}），实际 {err.line}:{err.col}")
    assert err.source_line, "没有源码行 —— 说明报在了 EOF 上"
    assert f"「{closing}」" in err.message, f"消息里应说清缺的是哪个右括号：{err.message}"
    assert err.hint and f"对应的「{closing}」" in err.hint, (
        f"提示应告诉用户补哪个：{err.hint}")
    assert "^" in err.render(), "渲染里没有下划线"


def test_括号没闭合_两个前端逐字一致():
    """新接的码必须在两个前端上逐字一致（这才是 R 线的验收方式）。"""
    for src, _c, _col in _UNCLOSED_CASES:
        got = {}
        for front in ("python", "rust"):
            old = os.environ.get(T.FRONTEND_ENV)
            os.environ[T.FRONTEND_ENV] = front
            try:
                P.parse_source(src, "冒烟.jsh")
                got[front] = "OK"
            except E.JishiError as e:
                got[front] = (e.code, e.message, e.line, e.col, e.underline, e.hint)
            finally:
                if old is None:
                    os.environ.pop(T.FRONTEND_ENV, None)
                else:
                    os.environ[T.FRONTEND_ENV] = old
        assert got["python"] == got["rust"], f"{src!r} 两个前端不一致：{got}"
        assert got["python"] != "OK"


def test_中途撞上别的token仍报E0201():
    """⚠️ **别为了把码归拢而把信息丢掉**：只有「走到文件末尾都没等到」才算 E0205。

    `[1, 2` 后面换行写了下一句时，报「需要「]」，但看到了「令」」比
    「括号没闭合」更有用 —— 它直接指出**冲突发生在哪里**。
    """
    with pytest.raises(E.JishiError) as ei:
        P.parse_source("令 甲 = [1, 2\n令 丙 = 3\n", "冒烟.jsh")
    err = ei.value
    assert err.code == "E0201", f"这条不该报 E0205：{err.message}"
    assert "令" in err.message, "应说出「看到了什么」"


# ---------------------------------------------------------------------------
# 四、凡是内部表示，都不许落到用户眼前
# ---------------------------------------------------------------------------

def test_到文件末尾不再说None():
    """`EOF` token 的 `value` 是 Python 的 `None` —— 不给它名字就会印进中文报错。

    和 R3 那个「插值字符串把 `[('text', …)]` 泄出来」是同一类毛病。
    """
    with pytest.raises(E.JishiError) as ei:
        P.parse_source("打印(\n", "冒烟.jsh")
    err = ei.value
    assert "None" not in err.message, f"把 `None` 泄给用户了：{err.message}"
    assert "文件末尾" in err.message, f"应说人话：{err.message}"


#: 「内部表示」的结构性标记 —— 中文报错里**不可能**有正当理由出现的东西。
#:
#: ⚠️ **故意不把 `None` / `True` / `False` 放进这张表**：它们可能是
#: **用户在源码里写的字**（英文关键字容错提示会原样引用：`「True」是英文写法…`），
#: 文本级扫描**分不清「引用用户写的字」与「泄漏内部表示」** —— 那就是误报。
#: 误报比不报更糟：走对路的人被拦下之后，大家会把这条测试关掉。
#: 「到文件末尾泄漏 `None`」那一条由 `test_到文件末尾不再说None` **精确**钉住。
_BANNED_INTERNAL = (
    "[('",          # list-of-tuples repr —— R3 抓到的那个「插值字符串分段表」
    "<class '",     # 类 repr
    " at 0x",       # 对象 repr 的地址
    "Traceback",    # Python 回溯
)


def test_冻结的报错文案不许泄露内部表示():
    """扫一遍**全部冻结基线**：标题 / 消息 / 提示里不许出现结构性的内部表示。

    ⚠️ 只扫这三个字段，**不扫 `渲染`** —— 渲染里含用户自己的源码行，
    用户写 `打印(None)` 完全合法，扫渲染会误报。
    """
    import json

    banned = _BANNED_INTERNAL
    bad: list[str] = []
    for p in sorted((ROOT / "tests" / "conformance" / "baseline").glob("*.json")):
        dg = json.loads(p.read_text(encoding="utf-8")).get("diagnostics")
        if not dg:
            continue
        for field in ("标题", "消息", "提示"):
            text = dg.get(field) or ""
            for mark in banned:
                if mark in text:
                    bad.append(f"{p.stem.split('__')[-1]} 的「{field}」里有 {mark!r}：{text}")
    assert not bad, "报错文案泄露内部表示：\n  · " + "\n  · ".join(bad)
