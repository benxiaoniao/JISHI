# -*- coding: utf-8 -*-
"""parser 单元测试：语句、表达式、块、报错。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jishi.tokenizer import tokenize
from jishi.parser import parse
from jishi import ast_nodes as A
from jishi.errors import (
    LexKeywordGlueError,
    ParseBlockError,
    ParseMissingColonError,
    ParseUnexpectedError,
)


def parse_src(src):
    tokens = tokenize(src)
    lines = src.replace("\r\n", "\n").split("\n")
    return parse(tokens, lines)


# ---------------------------------------------------------------------------
# 语句
# ---------------------------------------------------------------------------

def test_assign():
    prog = parse_src("令 分数 = 60")
    stmt = prog.body[0]
    assert isinstance(stmt, A.Assign)
    assert stmt.target.id == "分数"
    assert isinstance(stmt.value, A.Num)
    assert stmt.value.value == 60


def test_assign_without_decl():
    prog = parse_src("分数 = 60")
    stmt = prog.body[0]
    assert isinstance(stmt, A.Assign)


def test_expr_stmt_call():
    prog = parse_src('打印("你好")')
    stmt = prog.body[0]
    assert isinstance(stmt, A.ExprStmt)
    assert isinstance(stmt.expr, A.Call)
    assert isinstance(stmt.expr.func, A.Name)
    assert stmt.expr.func.id == "打印"


def test_if_elif_else():
    src = ("如果 分数 > 60：\n"
           "    打印(1)\n"
           "否则如果 分数 == 60：\n"
           "    打印(2)\n"
           "否则：\n"
           "    打印(3)")
    prog = parse_src(src)
    stmt = prog.body[0]
    assert isinstance(stmt, A.If)
    assert len(stmt.branches) == 2
    assert stmt.orelse is not None
    assert len(stmt.branches[0][1]) == 1  # 每个分支 1 条语句


def test_for():
    prog = parse_src("遍历 分数 在 列表：\n    打印(分数)")
    stmt = prog.body[0]
    assert isinstance(stmt, A.For)
    assert stmt.target.id == "分数"
    assert isinstance(stmt.iter, A.Name)


def test_loop_times():
    prog = parse_src("循环 10 次：\n    打印(1)")
    stmt = prog.body[0]
    assert isinstance(stmt, A.Loop)
    assert stmt.times.value == 10


def test_loop_without_ci():
    prog = parse_src("循环 10：\n    打印(1)")
    stmt = prog.body[0]
    assert isinstance(stmt, A.Loop)


def test_while():
    prog = parse_src("当 分数 < 100：\n    中断")
    stmt = prog.body[0]
    assert isinstance(stmt, A.While)


def test_funcdef():
    src = "函数 求和(a, b)：\n    返回 a + b"
    prog = parse_src(src)
    stmt = prog.body[0]
    assert isinstance(stmt, A.FuncDef)
    assert stmt.name == "求和"
    assert stmt.params == ["a", "b"]
    assert isinstance(stmt.body[0], A.Return)


def test_break_continue():
    prog = parse_src("中断\n继续")
    assert isinstance(prog.body[0], A.Break)
    assert isinstance(prog.body[1], A.Continue)


def test_import():
    prog = parse_src("导入 随机")
    stmt = prog.body[0]
    assert isinstance(stmt, A.Import)
    assert stmt.name == "随机"
    assert not stmt.from_python


def test_import_from_python():
    prog = parse_src("导入 pandas 从 python 为 pd")
    stmt = prog.body[0]
    assert isinstance(stmt, A.Import)
    assert stmt.name == "pandas"
    assert stmt.from_python
    assert stmt.alias == "pd"


def test_empty_block_with_colon():
    prog = parse_src("如果 真：\n    ：")
    stmt = prog.body[0]
    assert isinstance(stmt, A.If)
    assert isinstance(stmt.branches[0][1][0], A.Pass)


# ---------------------------------------------------------------------------
# 表达式
# ---------------------------------------------------------------------------

def test_binop_precedence():
    prog = parse_src("令 x = 1 + 2 * 3")
    value = prog.body[0].value
    assert isinstance(value, A.BinOp)
    assert value.op == "+"
    assert isinstance(value.right, A.BinOp)
    assert value.right.op == "*"


def test_paren_override():
    prog = parse_src("令 x = (1 + 2) * 3")
    value = prog.body[0].value
    assert isinstance(value, A.BinOp)
    assert value.op == "*"
    assert isinstance(value.left, A.BinOp)


def test_compare():
    prog = parse_src("令 x = 分数 >= 60")
    value = prog.body[0].value
    assert isinstance(value, A.Compare)
    assert value.ops == [">="]
    assert len(value.comparators) == 1


def test_compare_chained():
    prog = parse_src("令 x = 1 < 分数 < 10")
    value = prog.body[0].value
    assert isinstance(value, A.Compare)
    assert value.ops == ["<", "<"]
    assert len(value.comparators) == 2


def test_boolop():
    prog = parse_src("令 x = 分数 > 60 与 分数 < 100")
    value = prog.body[0].value
    assert isinstance(value, A.BoolOp)
    assert value.op == "与"
    assert len(value.values) == 2


def test_not():
    prog = parse_src("令 x = 非 真")
    value = prog.body[0].value
    assert isinstance(value, A.UnaryOp)
    assert value.op == "非"


def test_unary_minus():
    prog = parse_src("令 x = -5")
    value = prog.body[0].value
    assert isinstance(value, A.UnaryOp)
    assert value.op == "-"


def test_call_attr_subscript():
    prog = parse_src("令 x = 随机.随机整数(1, 100)")
    value = prog.body[0].value
    assert isinstance(value, A.Call)
    assert isinstance(value.func, A.Attr)
    assert value.func.attr == "随机整数"


def test_list_dict_literal():
    prog = parse_src('令 x = [1, 2, 3]\n令 y = {"a"：1, "b"：2}')
    assert isinstance(prog.body[0].value, A.List)
    assert isinstance(prog.body[1].value, A.Dict)


def test_power_right_assoc():
    prog = parse_src("令 x = 2 ** 3 ** 2")
    value = prog.body[0].value
    assert value.op == "**"
    assert value.right.op == "**"


# ---------------------------------------------------------------------------
# 报错
# ---------------------------------------------------------------------------

def test_missing_colon():
    with pytest.raises(ParseMissingColonError):
        parse_src("如果 真\n    打印(1)")


def test_block_required():
    with pytest.raises(ParseBlockError):
        parse_src("如果 真： 打印(1)")


def test_unexpected_keyword_start():
    with pytest.raises(ParseUnexpectedError):
        parse_src("否则 打印(1)")


def test_class_parsed():
    """M5b：类关键字已实现，不再是「未来关键字」。"""
    prog = parse_src("类 狗：\n    函数 叫(自身)：\n        返回 1")
    assert prog.body[0].name == "狗"


def test_compound_assign_parsed():
    prog = parse_src("令 x = 1\nx += 2")
    assert prog.body[1].op == "+="


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
