# -*- coding: utf-8 -*-
"""M37 主线 B1：调试器（断点 / 单步 / 看变量 / 看调用栈）。

测试分四层，对应四条承诺：

1. **行为**（`test_stop_*` / `test_*_steps_*`）：断点落在该落的行、单步能进函数、
   `下一步` 不进去、`跳出` 回到调用处、栈与变量看得见。
2. **可诊断**（`test_error_*` / `test_dead_breakpoint_*`）：出错了要能在**出错那一帧**
   看到变量；断点加在停不住的行上要明确说出来（不许静默无效）。
3. **不改语义**（`test_debugging_does_not_change_output`）：带调试器跑与不带，
   程序自身的输出必须逐字节相同——这是「钩子只读」的验收。
4. **CLI**（`test_cli_*`）：`jishi 调试` 的退出码、`--断点` / `--命令` 的行为，
   以及管道输入中文命令（Windows 上极易变成乱码的那条路）。
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import cli                                   # noqa: E402
from jishi.debugger import Debugger, parse_lines        # noqa: E402
from jishi.errors import JishiError                     # noqa: E402
from jishi.interpreter import Interpreter               # noqa: E402
from jishi.parser import parse, parse_expression        # noqa: E402
from jishi.tokenizer import tokenize                    # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: 演示程序（行号在注释里标出，断言直接引用这些行号）
DEMO = """函数 倍(值)：
    令 果 = 值 * 2
    返回 果

函数 主(甲)：
    令 乙 = 倍(甲)
    返回 乙 + 1

令 结果 = 主(10)
打印("结果 =", 结果)
"""
L_倍 = 1
L_果 = 2
L_倍返回 = 3
L_主 = 5
L_乙 = 6
L_主返回 = 7
L_结果 = 9
L_打印 = 10

_STOP_RE = re.compile(r"^暂停（(?P<reason>[^）]*)） 第 (?P<line>\d+) 行 · (?P<where>.+)$",
                      re.M)


def _run(src: str, script, *, breakpoints=(), stop_on_entry=False,
         name="调试用例.jsh"):
    """跑一场调试会话，返回 (输出, 错误, 调试器)。"""
    lines = src.replace("\r\n", "\n").split("\n")
    program = parse(tokenize(src, name), lines, name)
    buf = io.StringIO()
    with redirect_stdout(buf):
        dbg = Debugger(Interpreter(filename=name), program,
                       source_lines=lines, filename=name,
                       breakpoints=breakpoints, stop_on_entry=stop_on_entry,
                       script=script)
        err = dbg.run()
    return buf.getvalue(), err, dbg


def _stops(out: str) -> list[tuple[int, str, str]]:
    """从会话记录里抽出暂停点：(行号, 在哪, 原因)。"""
    return [(int(m.group("line")), m.group("where"), m.group("reason"))
            for m in _STOP_RE.finditer(out)]


def _run_cli(argv, stdin_text=None):
    """在进程内调 CLI（避开子进程编码差异）。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# 1. 基本行为
# ---------------------------------------------------------------------------

def test_runs_to_completion_without_debugging():
    """不加断点、不停入口 → 就当普通执行，stops 为 0。"""
    out, err, dbg = _run(DEMO, [])
    assert err is None and dbg.stops == 0
    assert "结果 = 21\n" in out
    assert "（程序正常结束" in out


def test_stops_at_entry_by_default():
    out, err, dbg = _run(DEMO, ["继续"], stop_on_entry=True)
    assert err is None
    assert _stops(out)[0] == (L_倍, "模块顶层", "停在入口")
    assert "结果 = 21\n" in out


def test_no_entry_starts_running():
    """`--不停在入口`：直接跑到断点。"""
    out, err, dbg = _run(DEMO, ["继续"], breakpoints=[L_打印])
    assert _stops(out) == [(L_打印, "模块顶层", f"命中第 {L_打印} 行的断点")]


def test_breakpoint_hits_and_shows_variables():
    out, err, dbg = _run(DEMO, ["变量", "继续"], breakpoints=[L_果])
    assert _stops(out)[0] == (L_果, "函数「倍」", f"命中第 {L_果} 行的断点")
    assert "当前帧（函数「倍」）的变量：" in out
    assert "  值 = 10" in out          # 形参在
    assert "  果 = " not in out         # 还没赋值，不该出现


def test_breakpoint_in_loop_hits_every_iteration():
    src = "循环 3 次：\n    打印(\"第\", 1)\n"
    out, err, dbg = _run(src, ["继续"], breakpoints=[2])
    assert dbg.stops == 3
    assert [s[0] for s in _stops(out)] == [2, 2, 2]


def test_step_into_function():
    """在「调用那一行」按单步 → 停在被调用函数的第一条语句上（真·步入）。"""
    out, err, dbg = _run(DEMO, ["单步", "继续"], breakpoints=[L_结果])
    assert [s[0] for s in _stops(out)] == [L_结果, L_乙]


def test_next_steps_over_function():
    """`下一步` 不进入函数：从调用行直接走到本帧的下一条语句。"""
    out, err, dbg = _run(DEMO, ["下一步", "继续"], breakpoints=[L_结果])
    assert [(s[0], s[1]) for s in _stops(out)] == [
        (L_结果, "模块顶层"), (L_打印, "模块顶层")]


def test_out_returns_to_caller():
    out, err, dbg = _run(DEMO, ["跳出", "继续"], breakpoints=[L_果])
    assert [(s[0], s[1]) for s in _stops(out)] == [
        (L_果, "函数「倍」"), (L_主返回, "函数「主」")]


def test_out_at_module_level_acts_like_continue():
    out, err, dbg = _run(DEMO, ["跳出", "继续"], breakpoints=[L_结果])
    assert "已经在最外层了" in out
    assert [s[0] for s in _stops(out)] == [L_结果]


def test_stack_shows_chain_inner_to_outer():
    """栈里每层标的是「这一层**当前停在哪一行**」——

    所以第 0 层是暂停处，之后每层标的是**被它调用的那个函数被调用的行**
    （也就是那一层自己执行到哪了），最后一行才是模块顶层。
    """
    out, err, dbg = _run(DEMO, ["栈", "继续"], breakpoints=[L_果])
    assert "调用栈（由内到外）：" in out
    assert f"  #0 第 {L_果:>4} 行  函数「倍」" in out      # 倍停在它自己第 2 行
    assert f"  #1 第 {L_乙:>4} 行  函数「主」" in out      # 主停在第 6 行（调用倍那一行）
    assert f"  #2 第 {L_结果:>4} 行  模块顶层" in out      # 模块停在第 9 行（调用主那一行）


def test_stack_at_module_level_has_one_frame():
    out, err, dbg = _run(DEMO, ["栈", "继续"], breakpoints=[L_打印])
    assert f"  #0 第 {L_打印:>4} 行  模块顶层" in out
    assert "#1" not in out


# ---------------------------------------------------------------------------
# 2. 看变量：默认藏内建与脱糖临时变量
# ---------------------------------------------------------------------------

def test_vars_hide_builtins_and_temporaries():
    out, err, dbg = _run(DEMO, ["变量", "继续"], breakpoints=[L_打印])
    assert "  长度 = " not in out          # 内建不该冒出来
    assert "  值错误 = " not in out         # 异常类型同理
    assert "（另有" in out and "变量 全部" in out


def test_vars_all_reveals_builtins():
    out, err, dbg = _run(DEMO, ["变量 全部", "继续"], breakpoints=[L_打印])
    assert "  长度 = " in out
    assert "（另有" not in out


def test_vars_hide_desugar_temporaries():
    """`用 … 为` 脱糖留下的 `__用_N__` 不该出现在变量列表里。

    这类临时变量是解析期造的（M26.2），用户在源码里根本看不到它们——
    列出来只会让人怀疑「这个 `__用_1__` 是哪儿来的」。
    """
    from jishi.interpreter import Environment

    lines = DEMO.split("\n")
    program = parse(tokenize(DEMO, "t.jsh"), lines, "t.jsh")
    buf = io.StringIO()
    with redirect_stdout(buf):
        dbg = Debugger(Interpreter(filename="t.jsh"), program,
                       source_lines=lines, script=[])
        env = Environment()
        env.set("__用_1__", object())
        env.set("甲", 1)
        dbg._env = env
        dbg._cmd_vars("")
    assert "甲 = 1" in buf.getvalue()
    assert "__用_1__" not in buf.getvalue()


def test_vars_reports_empty_frame():
    out, err, dbg = _run("令 甲 = 1\n", ["变量", "继续"], stop_on_entry=True)
    assert "（没有）" in out


# ---------------------------------------------------------------------------
# 3. 求值、源码、帮助、命令细节
# ---------------------------------------------------------------------------

def test_eval_expression_in_current_frame():
    out, err, dbg = _run(DEMO, ["查看 值 * 2", "继续"], breakpoints=[L_果])
    assert "值 * 2 = 20" in out


def test_eval_can_call_functions_and_see_containers():
    src = "令 列表 = [1, 2, 3]\n令 甲 = 0\n"
    out, err, dbg = _run(src, ["查看 长度(列表)", "查看 列表[1]", "继续"],
                         breakpoints=[2])
    assert "长度(列表) = 3" in out
    assert "列表[1] = 2" in out


def test_eval_rejects_more_than_one_expression():
    out, err, dbg = _run(DEMO, ["查看 1 2", "继续"], breakpoints=[L_打印])
    assert "只能写一个表达式" in out
    assert err is None            # 命令里的错不该掀翻整个会话


def test_eval_error_keeps_session_alive():
    out, err, dbg = _run(DEMO, ["查看 无此名字", "变量", "继续"],
                         breakpoints=[L_打印])
    assert "找不到这个名字" in out
    assert "当前帧（模块顶层）的变量：" in out     # 还在待命
    assert err is None


def test_source_shows_context_and_marks_current_line():
    out, err, dbg = _run(DEMO, ["源码", "继续"], breakpoints=[L_乙])
    assert "源码（第 1–11 行，标记的是第 6 行）：" in out
    assert f"→  {L_乙} │     令 乙 = 倍(甲)" in out


def test_source_accepts_line_argument():
    out, err, dbg = _run(DEMO, ["源码 2", "继续"], breakpoints=[L_打印])
    assert "标记的是第 2 行" in out


def test_source_rejects_non_number():
    out, err, dbg = _run(DEMO, ["源码 甲", "继续"], breakpoints=[L_打印])
    assert "用法：源码 [行号]" in out


def test_help_lists_core_commands():
    out, err, dbg = _run(DEMO, ["帮助", "继续"], stop_on_entry=True)
    for word in ("继续", "单步", "下一步", "跳出", "断点", "变量", "查看", "栈",
                 "源码", "退出"):
        assert word in out


def test_english_aliases_work():
    out, err, dbg = _run(DEMO, ["bt", "p 值", "c"], breakpoints=[L_果])
    assert "调用栈（由内到外）：" in out
    assert "值 = 10" in out


def test_blank_line_repeats_last_command():
    out, err, dbg = _run(DEMO, ["单步", "", "继续"], stop_on_entry=True)
    # 入口(1) → 单步到 5 → 空行重复「单步」→ 到 9
    assert [s[0] for s in _stops(out)][:3] == [L_倍, L_主, L_结果]


def test_unknown_command_is_pointed_out():
    out, err, dbg = _run(DEMO, ["飞起来", "继续"], stop_on_entry=True)
    assert "不认识这个命令" in out
    assert "帮助" in out


def test_repeat_before_any_command():
    out, err, dbg = _run(DEMO, ["", "继续"], stop_on_entry=True)
    assert "还没有上一条命令" in out


# ---------------------------------------------------------------------------
# 4. 断点管理
# ---------------------------------------------------------------------------

def test_break_list_add_remove_clear():
    out, err, dbg = _run(DEMO, ["断点", "断点 3,7", "断点",
                                "取消断点 3", "断点", "清空断点", "断点", "继续"],
                         stop_on_entry=True)
    assert "（还没有断点，用「断点 行号」加）" in out
    assert "已设断点：第 3 行、第 7 行" in out
    assert "已删除：第 3 行" in out
    assert "已清空 1 个断点" in out
    assert "（还没有断点" in out.split("已清空 1 个断点")[1]


def test_break_on_line_without_statement_warns():
    """第 4 行是空行 —— 断点加上去也不会停，必须当场说出来。"""
    out, err, dbg = _run(DEMO, ["断点 4", "断点", "继续"], stop_on_entry=True)
    assert "没有可停的语句" in out
    assert "第 4 行  ← 这一行没有可停的语句，不会停" in out


def test_block_keyword_lines_cannot_be_breakpoints():
    """「否则」「捕获」「最终」这类**块关键字行**不是独立语句，停不住。

    这是 B1 的已知边界（不是 bug）：它们是块的一部分，AST 里没有对应的语句节点
    （`否则` 那一行连节点都没有）。所以这类断点必须**明确提示**而不是静默无效。
    """
    src = ("如果 假：\n"
           "    打印(1)\n"
           "否则：\n"
           "    打印(2)\n"
           "尝试：\n"
           "    打印(3)\n"
           "捕获：\n"
           "    打印(4)\n"
           "最终：\n"
           "    打印(5)\n")
    lines = src.split("\n")
    program = parse(tokenize(src, "t.jsh"), lines, "t.jsh")
    dbg = Debugger(Interpreter(), program, source_lines=lines, script=[])
    for n in (3, 7, 9):
        assert n not in dbg.stmt_lines
    for n in (1, 2, 4, 5, 6, 8, 10):
        assert n in dbg.stmt_lines


def test_break_bad_line_number():
    out, err, dbg = _run(DEMO, ["断点 甲", "继续"], stop_on_entry=True)
    assert "看不懂这个行号" in out


def test_remove_absent_breakpoint():
    out, err, dbg = _run(DEMO, ["取消断点 3", "继续"], stop_on_entry=True)
    assert "本来就没有断点" in out


def test_parse_lines_accepts_chinese_comma_and_space():
    assert parse_lines(["3,7", "9 11"]) == [3, 7, 9, 11]
    assert parse_lines(["5，6"]) == [5, 6]
    with pytest.raises(ValueError):
        parse_lines(["abc"])


# ---------------------------------------------------------------------------
# 5. 出错现场（「出问题能快速定位」）
# ---------------------------------------------------------------------------

def test_error_snapshot_shows_failing_frame_and_vars():
    src = DEMO.replace("返回 果", "返回 果 + 无此名字")
    out, err, dbg = _run(src, ["继续"], stop_on_entry=True)
    assert err is not None and err.line == L_倍返回
    assert "出错时的现场" in out
    assert "当前帧（函数「倍」）的变量：" in out
    assert "  值 = 10" in out and "  果 = 20" in out
    assert f"  #0 第 {L_倍返回:>4} 行  函数「倍」" in out
    assert f"  #1 第 {L_乙:>4} 行  函数「主」" in out
    assert f"  #2 第 {L_结果:>4} 行  模块顶层" in out


def test_error_snapshot_of_arity_mistake_points_at_caller():
    """参数个数不对时错误在**调用之前**抛出，现场应停在调用者那一帧。"""
    src = DEMO.replace("令 结果 = 主(10)", "令 结果 = 主(10, 20)")
    out, err, dbg = _run(src, ["继续"], stop_on_entry=True)
    assert err is not None
    assert "当前帧（模块顶层）的变量：" in out


def test_error_outside_any_statement_has_no_snapshot():
    """空程序 + 空断点：没有任何语句，出错现场那条路也要能兜住。"""
    out, err, dbg = _run("", [], stop_on_entry=True)
    assert err is None
    assert "（程序正常结束" in out


# ---------------------------------------------------------------------------
# 6. 「退出」必须能真的退出去
# ---------------------------------------------------------------------------

def test_quit_from_pause():
    out, err, dbg = _run(DEMO, ["退出"], stop_on_entry=True)
    assert dbg.quit_requested is True
    assert err is None
    assert "调试会话已结束" in out
    assert "结果 = 21" not in out       # 没跑完


@pytest.mark.parametrize("keyword,body", [
    ("捕获：", "捕获：\n    打印(\"被接住了（不该出现）\")\n"),
    ("捕获 异常 为 e：", "捕获 异常 为 e：\n    打印(\"被接住了（不该出现）\")\n"),
])
def test_quit_is_not_swallowed_by_try_catch(keyword, body):
    """`尝试/捕获` 里按「退出」也必须退出去。

    这是把 `DebugQuit` 做成 `BaseException`（而不是 `JishiError`）的原因：
    `_do_try` 里那个 `except BaseException` 是为翻译 Python 异常写的，
    没有显式放行的话，一句 `捕获：` 就能把「退出」吞掉。
    """
    src = f"尝试：\n    令 甲 = 1\n{body}打印(\"这行不该出现\")\n"
    out, err, dbg = _run(src, ["退出"], stop_on_entry=True)
    assert dbg.quit_requested is True
    assert "被接住了" not in out
    assert "这行不该出现" not in out


def test_quit_runs_finally_blocks():
    """「退出」不是硬杀：`最终` 该跑还是要跑（与 中断/返回 同一处理）。

    注意断点要设**在 尝试 体内**：钩子是「语句执行前」触发的，停在 `尝试：`
    那一行时还没进入 `_do_try`，自然也没有什么 `最终` 要跑。
    """
    src = ("尝试：\n"
           "    令 甲 = 1\n"
           "最终：\n"
           "    打印(\"清理被执行\")\n")
    out, err, dbg = _run(src, ["退出"], breakpoints=[2])
    assert dbg.quit_requested is True
    assert "清理被执行" in out


# ---------------------------------------------------------------------------
# 7. 不改语义（钩子只读）
# ---------------------------------------------------------------------------

def test_debugging_does_not_change_output():
    """带调试器跑（断点 + 单步 + 变量）与不带，程序自身输出必须逐字节相同。

    钩子是「只读」的——它读语句行号、读环境，不写任何东西；这条测试就是
    「调试器不改语义」的验收。断言刻意用**带前缀的一整行**（`结果= …`）：
    会话记录里同时有被回显的源码行，拿裸数字去 count 会误命中。
    """
    src = ("令 合计 = 0\n"
           "遍历 甲 在 [1, 2, 3]：\n"
           "    如果 甲 == 2：\n"
           "        继续\n"
           "    合计 = 合计 + 甲\n"
           "打印(\"结果=\", 合计)\n")
    plain = io.StringIO()
    with redirect_stdout(plain):
        Interpreter(filename="t.jsh").run(parse(
            tokenize(src, "t.jsh"), src.split("\n"), "t.jsh"))
    assert plain.getvalue() == "结果= 4\n"          # 1 + 3（2 被 继续 跳过）
    out, err, dbg = _run(src, ["单步", "单步", "变量", "继续"], stop_on_entry=True)
    assert err is None
    assert out.count("结果= 4") == 1
    assert plain.getvalue() in out


def test_debugging_does_not_change_output_with_control_flow():
    """`中断`/`返回`/`尝试` 混在一起时，带不带调试器结果也要一致（钩子要能穿过它们）。"""
    src = ("函数 找(列表, 目标)：\n"
           "    遍历 项 在 列表：\n"
           "        如果 项 == 目标：\n"
           "            返回 真\n"
           "    返回 假\n"
           "尝试：\n"
           "    打印(\"结果=\", 找([1, 2], 2), 找([1, 2], 9))\n"
           "捕获：\n"
           "    打印(\"不该到这里\")\n")
    plain = io.StringIO()
    with redirect_stdout(plain):
        Interpreter(filename="t.jsh").run(parse(
            tokenize(src, "t.jsh"), src.split("\n"), "t.jsh"))
    assert plain.getvalue() == "结果= 真 假\n"
    out, err, dbg = _run(src, ["继续"], breakpoints=[1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert err is None
    assert out.count("结果= 真 假") == 1
    assert "不该到这里" not in out


def test_debugger_does_not_mutate_breakpoint_semantics():
    src = ("函数 加一(甲)：\n    返回 甲 + 1\n"
           "令 果 = 加一(1)\n打印(果)\n")
    out, err, dbg = _run(src, ["继续"], breakpoints=[1, 2, 3, 4])
    assert err is None and "2" in out


def test_interpreter_has_debugger_slot_by_default():
    assert Interpreter().debugger is None


# ---------------------------------------------------------------------------
# 8. AST 遍历（断点行号的地基）
# ---------------------------------------------------------------------------

def test_statement_lines_cover_nested_blocks():
    """语句行要能找到**嵌套块里的**语句，否则那些行上的断点永远不会触发。"""
    src = ("如果 真：\n"
           "    令 甲 = 1\n"
           "遍历 乙 在 [1]：\n"
           "    当 假：\n"
           "        令 丙 = 2\n"
           "函数 戊()：\n"
           "    尝试：\n"
           "        令 丁 = 3\n"
           "    捕获：\n"
           "        :\n"
           "类 己：\n"
           "    令 庚 = 4\n"
           "    函数 方法(自身)：\n"
           "        返回 庚\n")
    lines = src.split("\n")
    program = parse(tokenize(src, "t.jsh"), lines, "t.jsh")
    dbg = Debugger(Interpreter(), program, source_lines=lines, script=[])
    # 1-8、10-14 都能停；第 9 行的「捕获：」是块关键字行，停不住
    for n in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14):
        assert n in dbg.stmt_lines, f"第 {n} 行应该是可停的语句行"
    assert 9 not in dbg.stmt_lines
    assert 15 not in dbg.stmt_lines        # 末尾空行（split 出来的空串）


def test_statement_lines_exclude_blank_and_comment():
    src = "# 说明\n\n令 甲 = 1\n\n# 又一段\n令 乙 = 2\n"
    lines = src.split("\n")
    program = parse(tokenize(src, "t.jsh"), lines, "t.jsh")
    dbg = Debugger(Interpreter(), program, source_lines=lines, script=[])
    assert dbg.stmt_lines == {3, 6}


def test_parse_expression_ignores_trailing_newline():
    """回归钉：词法器会在末尾补 NEWLINE，漏掉它 `查看 1 + 2` 会报「只能写一个表达式」。"""
    node = parse_expression("1 + 2")
    assert node is not None
    with pytest.raises(JishiError):
        parse_expression("1 2")


# ---------------------------------------------------------------------------
# 9. 读命令的两条路（终端 / 管道）
# ---------------------------------------------------------------------------

def test_eof_on_stdin_ends_session(monkeypatch):
    """管道用尽（EOF）不能空转，要当成「退出」。"""
    import jishi.debugger as D

    class _FakeIn:
        def __init__(self):
            self.buffer = io.BytesIO(b"")
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", _FakeIn())
    lines = DEMO.split("\n")
    program = parse(tokenize(DEMO, "t.jsh"), lines, "t.jsh")
    buf = io.StringIO()
    with redirect_stdout(buf):
        dbg = Debugger(Interpreter(filename="t.jsh"), program,
                       source_lines=lines, stop_on_entry=True,
                       read_line=D._default_read_line)
        err = dbg.run()
    assert dbg.quit_requested is True and err is None
    assert "输入结束了" in buf.getvalue()


def test_piped_commands_are_utf8_decoded(monkeypatch):
    """管道里的中文命令按 UTF-8 解码，并自己回显一遍（管道不替用户回显）。"""
    import jishi.debugger as D

    class _FakeIn:
        def __init__(self, data: bytes):
            self.buffer = io.BytesIO(data)
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", _FakeIn("变量\n继续\n".encode("utf-8")))
    lines = DEMO.split("\n")
    program = parse(tokenize(DEMO, "t.jsh"), lines, "t.jsh")
    buf = io.StringIO()
    with redirect_stdout(buf):
        dbg = Debugger(Interpreter(filename="t.jsh"), program,
                       source_lines=lines, breakpoints=[L_打印],
                       read_line=D._default_read_line)
        err = dbg.run()
    out = buf.getvalue()
    assert err is None
    assert "当前帧（模块顶层）的变量：" in out     # 命令被正确解码并执行
    assert "调试 > 变量\n" in out                   # 且回显了


# ---------------------------------------------------------------------------
# 10. CLI
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, name: str, src: str) -> str:
    f = tmp_path / name
    f.write_text(src, encoding="utf-8")
    return str(f)


def test_cli_debug_runs_with_commands(tmp_path):
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, err = _run_cli(["调试", f, "--不停在入口", "--断点", "2",
                               "--命令", "变量；查看 值 * 2；继续"])
    assert code == 0
    assert "命中第 2 行的断点" in out
    assert "  值 = 10" in out
    assert "值 * 2 = 20" in out
    assert "结果 = 21" in out


def test_cli_debug_stops_at_entry_by_default(tmp_path):
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, _ = _run_cli(["调试", f, "--命令", "继续"])
    assert code == 0 and "停在入口" in out


def test_cli_debug_no_entry_flag(tmp_path):
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, _ = _run_cli(["调试", f, "--不停在入口", "--命令", "变量；继续"])
    assert code == 0 and "停在入口" not in out


def test_cli_debug_error_exit_code_and_report(tmp_path):
    f = _write(tmp_path, "坏的.jsh", DEMO.replace("返回 果", "返回 果 + 无此名字"))
    code, out, err = _run_cli(["调试", f, "--命令", "继续"])
    assert code == 1
    assert "出错时的现场" in out                     # 调试视角
    assert "错误 E0301" in err                       # 正式中文报错仍在 stderr
    assert "调用链（从外到内）" in err


def test_cli_debug_dead_breakpoint_warns_on_stderr(tmp_path):
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, err = _run_cli(["调试", f, "--断点", "4", "--命令", "继续"])
    assert code == 0
    assert "第 4 行 上没有可停的语句" in err
    assert "不会触发" in err


def test_cli_debug_bad_breakpoint_spec(tmp_path):
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, err = _run_cli(["调试", f, "--断点", "甲"])
    assert code == 2
    assert "看不懂这个断点行号" in err


def test_cli_debug_missing_file(tmp_path):
    code, out, err = _run_cli(["调试", str(tmp_path / "没有这个文件.jsh")])
    assert code == 1
    assert "找不到文件" in err


def test_cli_debug_parse_error_is_reported(tmp_path):
    f = _write(tmp_path, "语法错.jsh", "如果 真\n    打印(1)\n")
    code, out, err = _run_cli(["调试", f])
    assert code == 1
    assert "错误 E" in err


def test_cli_debug_help_mentions_breakpoints(tmp_path):
    code, out, err = _run_cli(["调试", "--help"])
    assert code == 0
    assert "--断点" in out and "--命令" in out


def test_cli_debug_accepts_chinese_semicolon(tmp_path):
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, _ = _run_cli(["调试", f, "--不停在入口", "-b", "2",
                             "--命令", "变量；继续"])
    assert code == 0 and "  值 = 10" in out


def test_cli_debug_via_pipe_subprocess(tmp_path):
    """真子进程 + 管道输入：中文命令经 stdin 不该变乱码（Windows GBK 坑）。"""
    f = _write(tmp_path, "演示.jsh", DEMO)
    r = subprocess.run(
        [sys.executable, "-m", "jishi.cli", "调试", f, "--不停在入口", "-b", "2"],
        input="变量\n继续\n".encode("utf-8"),
        capture_output=True, cwd=str(ROOT))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-400:]
    assert "命中第 2 行的断点" in out
    assert "  值 = 10" in out
    assert "结果 = 21" in out
    assert "\ufffd" not in out


# ---------------------------------------------------------------------------
# 11. 文档一致性（现状别说过头）
# ---------------------------------------------------------------------------

def test_docs_state_bytecode_engines_unsupported():
    """文档必须如实说明「只有树遍历执行器能调试」，别让用户以为三处都能用。"""
    doc = (ROOT / "docs" / "调试器.md").read_text(encoding="utf-8")
    assert "树遍历" in doc
    assert "暂不支持" in doc
    assert "C VM" in doc and "Python VM" in doc


def test_ai_card_and_lang_spec_not_promising_debugger():
    """语言卡 / 语言规格是给模型看的规格，调试器是工具，不该混进去。

    判据要**具体到调试器能力**，不能只搜「调试」两个字——M41 加了标准库
    `日志` 模块，它的函数名 `调试(...)`（Python 的 `logger.debug`）是正经
    语言 API，出现在语言卡里是对的。这条测试原先只搜「调试」，于是把
    `日志.调试` 也当成了违规（M41 踩到）。
    """
    from jishi.ai import render_ai_card

    card = render_ai_card()
    for 词 in ("断点", "单步", "调用栈", "调试器", "jishi 调试"):
        assert 词 not in card, f"语言卡里不该出现调试器能力「{词}」"
    # `日志.调试` 属于标准库，应该**在**卡里（对照，免得下次又改错方向）
    assert "日志" in card
