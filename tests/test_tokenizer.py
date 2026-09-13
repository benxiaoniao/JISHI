# -*- coding: utf-8 -*-
"""tokenizer 单元测试：缩进、全角、分词消歧、字符串、数字、注释。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jishi.tokenizer import tokenize
from jishi.errors import (
    LexFullWidthSpaceError,
    LexIndentError,
    LexKeywordGlueError,
    LexStringError,
    LexTabError,
)


def types(tokens):
    return [t.type for t in tokens]


def values(tokens):
    return [(t.type, t.value) for t in tokens]


# ---------------------------------------------------------------------------
# 基础流
# ---------------------------------------------------------------------------

def test_basic_statement():
    toks = tokenize('打印("你好")')
    assert values(toks) == [
        ("NAME", "打印"), ("OP", "("), ("STRING", "你好"),
        ("OP", ")"), ("NEWLINE", "\n"), ("EOF", None),
    ]


def test_known_escapes():
    toks = tokenize(r'令 s = "a\nb\tc\\d\"e"')
    s = [t for t in toks if t.type == "STRING"][0]
    assert s.value == 'a\nb\tc\\d"e'


def test_unknown_escape_preserved():
    """未知转义保留反斜杠，这样 Windows 路径可以直写。"""
    toks = tokenize('令 p = "C:\\Users\\文档\\a.txt"')
    s = [t for t in toks if t.type == "STRING"][0]
    assert s.value == "C:\\Users\\文档\\a.txt"


def test_windows_path_and_tab_coexist():
    """制表符转义仍然生效；只有未知转义才保留反斜杠。"""
    toks = tokenize(r'令 p = "a\tb"')
    s = [t for t in toks if t.type == "STRING"][0]
    assert s.value == "a\tb"


def test_variable_assignment():
    toks = tokenize("令 分数 = 60")
    assert values(toks)[:5] == [
        ("KEYWORD", "令"), ("NAME", "分数"), ("OP", "="),
        ("NUMBER", 60), ("NEWLINE", "\n"),
    ]


# ---------------------------------------------------------------------------
# 缩进
# ---------------------------------------------------------------------------

def test_indent_dedent():
    src = "如果 真：\n    打印(1)\n打印(2)"
    toks = tokenize(src)
    t = types(toks)
    assert "INDENT" in t
    assert "DEDENT" in t
    # INDENT 在第一个 NEWLINE 之后
    assert t[t.index("NEWLINE") + 1] == "INDENT"


def test_nested_indent():
    src = "如果 真：\n    遍历 列表：\n        打印(1)\n    打印(2)\n打印(3)"
    toks = tokenize(src)
    t = types(toks)
    # 两次缩进两级，两次退回一级
    assert t.count("INDENT") == 2
    assert t.count("DEDENT") == 2


def test_bad_indent_level():
    src = "如果 真：\n    打印(1)\n  打印(2)"
    # 3 空格对不上 4 空格的缩进栈 -> 缩进错误
    with pytest.raises(LexIndentError):
        tokenize(src)


def test_empty_and_comment_lines_do_not_emit():
    src = "如果 真：\n\n    # 注释\n    打印(1)\n\n打印(2)"
    toks = tokenize(src)
    t = types(toks)
    # 空行/注释行之间不应出现多余的 INDENT/DEDENT/NEWLINE
    # 只允许 1 个 INDENT 和 1 个 DEDENT
    assert t.count("INDENT") == 1
    assert t.count("DEDENT") == 1


def test_ignore_indent_inside_brackets():
    src = "打印(1,\n      2,\n      3)"
    toks = tokenize(src)
    assert "INDENT" not in types(toks)
    # 括号内换行不产生 NEWLINE
    assert types(toks).count("NEWLINE") == 1


# ---------------------------------------------------------------------------
# 全角字符
# ---------------------------------------------------------------------------

def test_fullwidth_punctuation_normalized():
    src = "令 分数 ＝ 60：打印（分数）"
    toks = tokenize(src)
    vals = values(toks)
    assert ("OP", "=") in vals
    assert ("OP", ":") in vals
    assert ("OP", "(") in vals
    assert ("OP", ")") in vals


def test_fullwidth_space_in_indent_raises():
    src = "如果 真：\n\u3000\u3000打印(1)"
    with pytest.raises(LexFullWidthSpaceError):
        tokenize(src)


def test_tab_and_space_mixed_raises():
    src = "如果 真：\n \t打印(1)"
    with pytest.raises(LexTabError):
        tokenize(src)


# ---------------------------------------------------------------------------
# 中文分词消歧
# ---------------------------------------------------------------------------

def test_keyword_glue_error():
    src = "如果分数 > 60："
    with pytest.raises(LexKeywordGlueError) as ei:
        tokenize(src)
    assert "你是不是想写「如果 分数」" in ei.value.hint


def test_compound_keyword_not_glue():
    # 「否则如果」整体是关键字，不应报粘连
    src = "否则如果 分数 == 60："
    toks = tokenize(src)
    assert ("KEYWORD", "否则如果") in values(toks)


def test_cjk_identifier():
    toks = tokenize("令 总分_total = 100")
    assert ("NAME", "总分_total") in values(toks)


def test_keyword_then_fullwidth_paren_ok():
    toks = tokenize("如果（分数 > 60）：")
    vals = values(toks)
    assert ("KEYWORD", "如果") in vals
    assert ("OP", "(") in vals


# ---------------------------------------------------------------------------
# 字符串
# ---------------------------------------------------------------------------

def test_chinese_quotes_as_delimiters():
    toks = tokenize('打印("你好，世界！")')
    assert ("STRING", "你好，世界！") in values(toks)


def test_curly_quote_delimiters():
    toks = tokenize('令 s = “中文引号”')
    assert ("STRING", "中文引号") in values(toks)


def test_triple_quote_multiline():
    src = '令 s = """第一行\n第二行"""'
    toks = tokenize(src)
    assert ("STRING", "第一行\n第二行") in values(toks)


def test_escape_sequences():
    toks = tokenize(r'打印("a\tb\\n")')
    assert ("STRING", "a\tb\\n") in values(toks)


def test_unterminated_string():
    with pytest.raises(LexStringError):
        tokenize('令 s = "没结束')


# ---------------------------------------------------------------------------
# 数字
# ---------------------------------------------------------------------------

def test_numbers():
    src = "令 a = 1\n令 b = 1.5\n令 c = 2e3\n令 d = .5"
    vals = values(tokenize(src))
    assert ("NUMBER", 1) in vals
    assert ("NUMBER", 1.5) in vals
    assert ("NUMBER", 2000.0) in vals
    assert ("NUMBER", 0.5) in vals


# ---------------------------------------------------------------------------
# 注释
# ---------------------------------------------------------------------------

def test_line_comment():
    toks = tokenize("令 a = 1 # 这是注释\n打印(a)")
    assert ("NUMBER", 1) in values(toks)


def test_block_comment():
    src = "令 a = 1\n#- 多行\n注释 -#\n打印(a)"
    vals = values(tokenize(src))
    assert ("NUMBER", 1) in vals
    assert ("NAME", "打印") in vals
    # 注释内容不出现
    assert ("NAME", "注释") not in vals
    assert ("NAME", "多行") not in vals


def test_reserved_future_keyword_passes_lexer():
    # 类等未来关键字能过词法，语法阶段再报"暂不支持"
    toks = tokenize("类 狗：")
    assert ("KEYWORD", "类") in values(toks)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
