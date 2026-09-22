# -*- coding: utf-8 -*-
"""M43：语法高亮表与代码片段要和**语言事实**对齐。

编辑器里那两张表（`syntaxes/jishi.tmLanguage.json`、`snippets/*.code-snippets`）
是**手写的副本**——语言里新增一个内建、一个新的库模块，如果不顺手同步，
编辑器里就悄悄少一块颜色、少一个片段。这类「悄悄过时」靠眼睛基本发现不了
（M41 的 few-shot 体检就抓到 48 段示例里躺着 4 段真错），所以用测试钉住：

- **覆盖**：`jishi --lang-spec` 里的关键字 / 内建 / 标准库模块 / 异常类型，
  必须都在高亮表里出现；
- **行为**：直接拿高亮表里的正则跑几个字符串，验证 `文本(值)` 是内建函数、
  `文本.左对齐(值)` 是模块成员（同一个名字两种身份，靠「后面跟不跟 `.`」区分）；
- **片段**：把片段占位符填成默认值后必须能**解析**（填错语法的话，用户和
  模型都会照着写错）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi.ai import build_lang_spec                    # noqa: E402
from jishi.errors import JishiError                     # noqa: E402
from jishi.parser import parse                          # noqa: E402
from jishi.tokenizer import tokenize                    # noqa: E402

TMLANG = ROOT / "editors" / "vscode-jishi" / "syntaxes" / "jishi.tmLanguage.json"
SNIPPETS = ROOT / "editors" / "vscode-jishi" / "snippets" / "jishi.code-snippets"


# ---------------------------------------------------------------------------
# 1. 覆盖：语言事实里的词都得在高亮表里出现
# ---------------------------------------------------------------------------

def test_highlight_covers_language_facts():
    """新增的内建 / 模块 / 异常类型必须都在高亮表里有一席之地。"""
    spec = build_lang_spec()
    源 = TMLANG.read_text(encoding="utf-8")
    词 = set(re.findall(r"[\u4e00-\u9fffA-Za-z_]{1,12}", 源))

    分组 = {
        "关键字": list(spec["keywords"]),
        "内建": [b["name"] for b in spec["builtins"]],
        "标准库模块": [m["module"] for m in spec["stdlib"]],
        "异常类型": list(spec["exceptions"]),
    }
    缺 = {名: [w for w in 表 if w not in 词] for 名, 表 in 分组.items()}
    缺 = {名: v for 名, v in 缺.items() if v}
    assert not 缺, (
        "高亮表与语言事实对不上（新加的东西没进高亮表）：\n"
        + "\n".join(f"  {名}缺：{'、'.join(v)}" for 名, v in 缺.items()))


def test_highlight_is_valid_json_with_expected_scopes():
    tm = json.loads(TMLANG.read_text(encoding="utf-8"))
    assert tm["scopeName"] == "source.jishi"
    assert tm["fileTypes"] == ["jsh"]
    scopes = set()

    def 收(节点):
        if isinstance(节点, dict):
            if "name" in 节点:
                scopes.add(节点["name"])
            for v in 节点.values():
                收(v)
        elif isinstance(节点, list):
            for v in 节点:
                收(v)

    收(tm)
    for 要 in ("comment.line.number-sign.jishi", "string.quoted.double.jishi",
               "keyword.control.jishi", "support.function.builtin.jishi",
               "support.namespace.jishi", "support.class.exception.jishi"):
        assert 要 in scopes, f"少了 scope：{要}"


# ---------------------------------------------------------------------------
# 2. 行为：两条正则真的按「后面跟什么」区分身份
# ---------------------------------------------------------------------------

def _规则(scope: str) -> list:
    """把高亮表里某个 scope 的 match 正则**全部**取出来编译。

    同一个 scope 名可能出现在好几处（`keyword.control.jishi` 在类定义行、
    函数定义行、关键字表里都有），所以返回列表，判定统一用 `_命中`。
    """
    tm = json.loads(TMLANG.read_text(encoding="utf-8"))
    找到: list[str] = []

    def 走(节点):
        if isinstance(节点, dict):
            if 节点.get("name") == scope and "match" in 节点:
                找到.append(节点["match"])
            for v in 节点.values():
                走(v)
        elif isinstance(节点, list):
            for v in 节点:
                走(v)

    走(tm)
    assert 找到, f"高亮表里没有 scope = {scope}"
    return [re.compile(m) for m in 找到]


def _命中(规则们: list, 文本: str) -> bool:
    """任意一条正则命中即算命中。"""
    return any(r.search(文本) for r in 规则们)


def test_builtin_and_module_scopes_do_not_overlap():
    """`文本` 既是内建函数名、又是标准库模块名，靠「后面跟不跟 `.`」分身份。

    分不开的话，`文本.左对齐(x)` 里的 `文本` 会被涂成函数色——编辑器里一看
    就是错的，而且这种小错很影响观感。
    """
    内建 = _规则("support.function.builtin.jishi")
    模块 = _规则("support.namespace.jishi")

    assert _命中(内建, "文本(1)")
    assert not _命中(内建, "文本.左对齐(1)")
    assert _命中(模块, "文本.左对齐(1)")
    assert not _命中(模块, "文本(1)")
    # M41 新增的内建与模块也要认
    assert _命中(内建, "带下标([1, 2], 1)")
    assert _命中(内建, "配对([1], [2])")
    assert _命中(模块, "容器.计数([1, 2])")
    assert _命中(模块, "参数.解析([], {})")
    # 名字的一部分不该被当成内建（`文本长度` 不是内建）
    assert not _命中(内建, "文本长度(1)")


def test_keyword_and_exception_scopes():
    关键字 = _规则("keyword.control.jishi")
    assert _命中(关键字, "匹配 甲：")               # M34 模式匹配
    assert _命中(关键字, "用 打开(\"x\") 为 f：")   # M26 上下文管理器
    assert _命中(关键字, "遍历 甲 在 乙：")
    异常 = _规则("support.class.exception.jishi")
    assert _命中(异常, "捕获 值错误：")
    assert _命中(异常, "捕获 文件错误 为 e：")
    字面 = _规则("constant.language.jishi")
    assert _命中(字面, "如果 真：")
    assert not _命中(字面, "真话")                  # 只是开头一样


# ---------------------------------------------------------------------------
# 3. 片段：填上默认值后必须能解析
# ---------------------------------------------------------------------------

def _片段文本(body) -> str:
    """把片段变成一段能拿去解析的代码。

    两处细节都是被真实片段逼出来的：

    - `${2:打印("${3}")}` 的默认值里**又嵌了占位符**，所以填充要认嵌套
      （先用 `[^}]*` 那种简单正则，会把片段切坏——实测踩到）；
    - 片段是「带光标的骨架」，`如果 条件：` 后面本来要用户去填块内容，
      所以见到「块头后面没有更深的缩进」就补一个**空块**（基石的空块就是
      单独一行 `:`）。
    """
    return _补空块(_填占位符(body if isinstance(body, str) else "\n".join(body)))


def _填占位符(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "$" and i + 1 < len(text) and text[i + 1] == "{":
            depth, k = 1, i + 2
            while k < len(text) and depth:          # 找到配对的 `}`（认嵌套）
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                k += 1
            inner = text[i + 2:k - 1]
            _, _, default = inner.partition(":")
            out.append(_填占位符(default) if default else "值")
            i = k
            continue
        if ch == "$" and i + 1 < len(text) and text[i + 1].isdigit():
            i += 1
            while i < len(text) and text[i].isdigit():   # $0 / $1 → 去掉
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _缩进(行: str) -> int:
    return len(行) - len(行.lstrip("\t"))


def _补空块(text: str) -> str:
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()                     # 片段末尾的空行（$0 待的地方）不算块体
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        if not line.rstrip().endswith("："):
            continue
        下 = next((l for l in lines[i + 1:] if l.strip()), None)
        if 下 is None or _缩进(下) <= _缩进(line):
            out.append("\t" * (_缩进(line) + 1) + ":")
    return "\n".join(out)


#: 单独成不了篇的片段：它们必须是某个语句的一部分
_不能单独解析 = {"否则"}


def test_snippets_are_parseable():
    """片段是「正确写法」的模板，填上默认值后必须解析得过。

    M41 的教训：`examples/ai/few-shot.md` 里 48 段示例从没人跑过，一跑发现
    4 段真错。片段同理——用户和 AI 都会照着它写。
    """
    snip = json.loads(SNIPPETS.read_text(encoding="utf-8"))
    assert len(snip) >= 15, f"片段太少了（{len(snip)} 条），是不是被改坏了"
    坏了 = []
    for 名, 项 in snip.items():
        if 名 in _不能单独解析:
            continue
        源 = _片段文本(项["body"])
        try:
            parse(tokenize(源, 名), 源.split("\n"), 名)
        except JishiError as e:
            坏了.append(f"{名}：{e.title}"
                        + (f"（{e.message}）" if e.message else ""))
    assert not 坏了, "这些片段解析不过：\n" + "\n".join(坏了)


def test_snippets_cover_newer_syntax():
    """M36 之后加的写法要有片段：不然用户压根不知道有这些写法。"""
    snip = json.loads(SNIPPETS.read_text(encoding="utf-8"))
    prefixes = {项["prefix"] for 项 in snip.values()}
    for 要 in ("导入", "遍历解包", "带下标", "匹配", "用", "插值"):
        assert 要 in prefixes, f"缺片段：{要}"


def test_snippet_body_is_never_empty():
    snip = json.loads(SNIPPETS.read_text(encoding="utf-8"))
    for 名, 项 in snip.items():
        body = 项["body"]
        文本 = body if isinstance(body, str) else "".join(body)
        assert 文本.strip(), f"片段「{名}」是空的"
        assert 项.get("description"), f"片段「{名}」没写说明"
