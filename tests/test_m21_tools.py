# -*- coding: utf-8 -*-
"""M21.1 格式化工具：幂等 / 语义不变 / 注释保留 / CLI 行为。

语义保障用的是「token 流等价」：格式化前后把源码重新分词，忽略
INDENT/DEDENT/NEWLINE/EOF 与行列坐标后，token 的类型与值必须逐一对齐。
这比「跑一遍比输出」更严格（能发现只在分支里才暴露的改动），也不依赖运行。
"""

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import cli                            # noqa: E402
from jishi.formatter import format_source        # noqa: E402
from jishi.tokenizer import tokenize             # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: 可格式化语料：examples 与 tests/cases。
#: tests/error_cases 故意带语法错误，格式化器应当直接报错，单独测。
CORPUS = sorted(ROOT.glob("examples/**/*.jsh")) + sorted(
    ROOT.glob("tests/cases/*.jsh"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_cli(argv, stdin_text=None):
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# 语料级：幂等 + 语义不变
# ---------------------------------------------------------------------------

def test_corpus_is_enough():
    assert len(CORPUS) >= 20, f"语料太少（{len(CORPUS)}），检查路径"


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_format_is_idempotent(path):
    """格式化两次 == 格式化一次。"""
    src = _read(path)
    once = format_source(src, str(path))
    assert format_source(once, str(path)) == once


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_format_preserves_tokens(path):
    """语义不变：token 流（类型 + 值）在格式化前后完全一致。"""
    src = _read(path)
    out = format_source(src, str(path))

    def signature(text):
        return [
            (t.type, t.value)
            for t in tokenize(text, str(path))
            if t.type not in ("INDENT", "DEDENT", "NEWLINE", "EOF")
        ]

    assert signature(out) == signature(src)


# ---------------------------------------------------------------------------
# 规则级
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src,want", [
    ("令 甲=1\n", "令 甲 = 1\n"),
    ("打印( 甲 )\n", "打印(甲)\n"),
    ("令 乙 = 甲[0]\n", "令 乙 = 甲[0]\n"),
    ("令 丙=[1,2,3]\n", "令 丙 = [1, 2, 3]\n"),
    ("令 丁={'甲':1}\n", "令 丁 = {'甲': 1}\n"),
    ("令 戊 = 甲[1:3]\n", "令 戊 = 甲[1:3]\n"),
    ("令 负=-1\n", "令 负 = -1\n"),
    ("令 差=甲-乙\n", "令 差 = 甲 - 乙\n"),
    ("令 幂=2**10\n", "令 幂 = 2 ** 10\n"),
    ("令 累+=1\n", "令 累 += 1\n"),
    ("如果 甲>0：\n    打印(1)\n", "如果 甲 > 0：\n    打印(1)\n"),
    ("遍历 甲 在 范围(3)：\n    打印(甲)\n",
     "遍历 甲 在 范围(3)：\n    打印(甲)\n"),
])
def test_spacing_rules(src, want):
    assert format_source(src) == want


def test_normalizes_indent_to_four_spaces():
    src = "如果 真：\n  打印(1)\n   打印(2)\n"
    out = format_source(src)
    # 第二行缩进更深，属于新的层级
    assert out.startswith("如果 真：\n    打印(1)\n        打印(2)\n")


def test_custom_indent_width():
    assert format_source("如果 真：\n    打印(1)\n", indent=2) == \
        "如果 真：\n  打印(1)\n"


def test_preserves_comments():
    src = (
        "# 顶部注释\n"
        "令 甲 = 1  # 行尾注释\n"
        "函数 加一(x)：\n"
        "    # 块内注释\n"
        "    返回 x + 1\n"
    )
    out = format_source(src)
    for c in ("# 顶部注释", "# 行尾注释", "# 块内注释"):
        assert c in out


def test_independent_comment_keeps_top_level():
    """顶层注释不应被上一层的缩进带走。"""
    src = "如果 真：\n    打印(1)\n# 顶层注释\n令 甲 = 1\n"
    out = format_source(src)
    assert "\n# 顶层注释\n" in out


def test_preserves_block_comment_verbatim():
    src = "#- 块注释开头\n     内部缩进原样保留\n-#\n令  甲=1\n"
    out = format_source(src)
    assert "     内部缩进原样保留" in out
    assert "令 甲 = 1" in out


def test_preserves_multiline_string():
    src = '令 文本 = """第一行\n  第二行原样\n第三行"""\n令  甲=1\n'
    out = format_source(src)
    assert "  第二行原样" in out
    assert "令 甲 = 1" in out


def test_keeps_original_quote_style():
    """不重新加引号：中文引号与转义原样保留。"""
    src = '打印("你好")\n打印(\'单引号\')\n打印("路径 C:\\新\\a.txt")\n'
    out = format_source(src)
    assert '"你好"' in out
    assert "'单引号'" in out
    assert "C:\\新\\a.txt" in out


def test_empty_source():
    assert format_source("") == ""
    assert format_source("\n\n") == ""


# ---------------------------------------------------------------------------
# 错误处理与 CLI
# ---------------------------------------------------------------------------

def test_raises_on_unlexable_source():
    from jishi.errors import JishiError
    with pytest.raises(JishiError):
        format_source('打印("没有结束\n')


def test_cli_format_stdin():
    code, out, _ = _run_cli(["格式化", "--stdin"], "令  甲=1\n")
    assert code == 0
    assert out == "令 甲 = 1\n"


def test_cli_format_check_clean():
    code, _, err = _run_cli(["格式化", "--stdin", "--check"], "令 甲 = 1\n")
    assert code == 0
    assert err == ""


def test_cli_format_check_dirty():
    code, _, err = _run_cli(["格式化", "--stdin", "--check"], "令  甲=1\n")
    assert code == 1
    assert "没有按统一风格" in err


def test_cli_format_write(tmp_path):
    p = tmp_path / "甲.jsh"
    p.write_text("令  甲=1\n", encoding="utf-8")
    code, _, _ = _run_cli(["格式化", str(p), "--write"])
    assert code == 0
    assert _read(p) == "令 甲 = 1\n"


def test_cli_format_reports_error():
    code, _, err = _run_cli(["格式化", "--stdin"], '打印("没有结束\n')
    assert code == 1
    assert "错误" in err


def test_cli_format_requires_file():
    code, _, err = _run_cli(["格式化"])
    assert code == 2
    assert "请给出要格式化的文件" in err
