# -*- coding: utf-8 -*-
"""M39 · 主线 B5：字节码执行器的调试器（`jishi 调试 --执行器 vm`）。

这一份测试的核心是**一条承诺**：

> 同一个程序、同一组断点、同一串命令，树遍历与字节码执行器的会话记录
> **逐字相同**。

它把 B1（树遍历）与 B5（字节码）缝成同一个调试体验，也让「以后改任一个
执行器的调试钩子」都有一道硬闸：对拍不过就是行为漂了。

测试分五层：

1. **对拍**（`test_parity_*`）：会话记录逐字相同——含三种循环、逐句单步、
   闭包、出错现场、`退出`、`尝试/最终`、类体、空块。
2. **不变式**（`test_marks_*`、`test_statement_lines_*`）：字节码里的
   「语句起始行」与 AST 的「语句行」在真实语料上**完全一致**。
3. **字节码专有**（`test_vm_*`）：变量（局部 / 本函数单元变量）、在帧里求值、
   出错现场、`退出` 穿 `最终` 且不被 `捕获` 吞掉。
4. **不改语义**（`test_debugging_*`）：带与不带调试器，程序输出逐字节相同。
5. **CLI**（`test_cli_*`）：`--执行器 vm` / `cvm` 的明确行为。
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi.compiler import compile_program                    # noqa: E402
from jishi.debugger import (Debugger, VmDebugger, mark_lines,   # noqa: E402
                            statement_lines)
from jishi.interpreter import Interpreter                     # noqa: E402
from jishi.parser import parse                                # noqa: E402
from jishi.tokenizer import tokenize                          # noqa: E402
from jishi.vm import VM                                       # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

_STOP_RE = re.compile(r"^暂停（(?P<reason>[^）]*)） 第 (?P<line>\d+) 行 · (?P<where>.+)$",
                      re.M)


# ---------------------------------------------------------------------------
# 会话跑法：两个执行器各跑一遍，比会话记录
# ---------------------------------------------------------------------------

def _parse(src: str, name: str = "调试用例.jsh"):
    lines = src.replace("\r\n", "\n").split("\n")
    return parse(tokenize(src, name), lines, name), lines


def run_session(engine: str, src: str, script, *, breakpoints=(),
                stop_on_entry=False, name="调试用例.jsh"):
    """跑一场调试会话，返回 (输出, 错误)。`engine` 是 '树遍历' 或 'vm'。"""
    program, lines = _parse(src, name)
    buf = io.StringIO()
    with redirect_stdout(buf):
        if engine == "vm":
            dbg = VmDebugger(VM(compile_program(program, name, lines), name),
                             source_lines=lines, filename=name,
                             breakpoints=breakpoints,
                             stop_on_entry=stop_on_entry, script=script)
        else:
            dbg = Debugger(Interpreter(filename=name), program,
                           source_lines=lines, filename=name,
                           breakpoints=breakpoints,
                           stop_on_entry=stop_on_entry, script=script)
        err = dbg.run()
    return buf.getvalue(), err


def _both(src, script, *, breakpoints=(), stop_on_entry=False):
    """两个执行器各跑一遍并断言逐字相同，返回字节码那一份的结果。"""
    a, ea = run_session("树遍历", src, script, breakpoints=breakpoints,
                        stop_on_entry=stop_on_entry)
    b, eb = run_session("vm", src, script, breakpoints=breakpoints,
                        stop_on_entry=stop_on_entry)
    assert (ea is None) == (eb is None), (
        f"两个执行器对错误的判定不同：树遍历 {ea} / VM {eb}")
    if ea is not None:
        assert (ea.title, ea.line) == (eb.title, eb.line)
    assert a == b, ("两个执行器的会话记录必须逐字相同\n"
                    "──── 树遍历 ────\n" + a + "\n──── 字节码 VM ────\n" + b)
    return b, eb


def _stops(out: str) -> list[tuple[int, str, str]]:
    """从会话记录里抽出暂停点：(行号, 在哪, 原因)。"""
    return [(int(m.group("line")), m.group("where"), m.group("reason"))
            for m in _STOP_RE.finditer(out)]


#: 覆盖三种循环、函数调用、闭包、求值的演示程序
DEMO = """函数 倍(值)：
    令 果 = 值 * 2
    返回 果

函数 主(甲)：
    令 乙 = 倍(甲)
    返回 乙 + 1

令 结果 = 主(10)
遍历 项 在 [1, 2, 3]：
    结果 = 结果 + 项
令 i = 0
当 i < 2：
    打印(i)
    i = i + 1
循环 1 次：
    结果 = 结果 + 1
打印("结果 =", 结果)
"""
L_倍 = 1
L_果 = 2
L_倍返回 = 3
L_主 = 5
L_乙 = 6
L_主返回 = 7
L_结果 = 9
L_遍历 = 10
L_累加 = 11
L_令i = 12
L_当 = 13
L_打印i = 14
L_i加 = 15
L_循环 = 16
L_循环体 = 17
L_打印 = 18

#: 闭包（单元变量）：内层只**读**外层变量
CLOSURE = """函数 计数器(起始)：
    令 基数 = 起始
    函数 加(步长)：
        返回 基数 + 步长
    返回 加

令 加 = 计数器(10)
令 值 = 加(5)
打印("值 =", 值)
"""

#: 类体：方法定义与类变量都是语句
CLASSY = """类 计数器：
    令 步长 = 3
    函数 初始化(自身, 起点)：
        自身.值 = 起点
    函数 加一次(自身)：
        自身.值 = 自身.值 + 自身.步长
        返回 自身.值

令 c = 计数器(10)
打印(c.加一次())
"""
C_类 = 1
C_步长 = 2
C_初始化 = 3
C_初始化体 = 4
C_加一次 = 5
C_自增值 = 6
C_返回 = 7
C_造 = 9
C_打印 = 10


# ---------------------------------------------------------------------------
# 1. 对拍
# ---------------------------------------------------------------------------

def test_parity_stops_on_entry():
    """停在入口：两个执行器都停在第一条语句上。"""
    out, err = _both(DEMO, ["退出"], stop_on_entry=True)
    assert err is None
    assert _stops(out)[0][0] == L_倍
    assert "（调试会话已结束，程序没有跑完）" in out


def test_parity_full_walk_through_every_statement():
    """一句一句走完整段程序（三种循环、函数进出、类体）：暂停序列一致。"""
    script = ["单步"] * 13 + ["退出"]
    out, err = _both(DEMO, script, stop_on_entry=True)
    assert err is None
    order = [s[0] for s in _stops(out)]
    # 语句执行顺序：类/函数定义 → 调用点 → 进入函数 → 回来 → 三种循环 → 打印
    assert order[:7] == [L_倍, L_主, L_结果, L_乙, L_果, L_倍返回, L_主返回]
    assert order[7] == L_遍历


def test_parity_loop_heads_stop_once_bodies_stop_every_round():
    """**关键语义**：循环头只停一次、循环体每轮都停（三种循环都如此）。

    这条是「语句标记只打在语句首指令上」的直接后果：循环的回边落在语句
    中部，所以回边不会把循环头再触发一次——与树遍历只进一次循环语句一致。
    """
    script = ["继续"] * 9 + ["退出"]
    out, _ = _both(DEMO, script, breakpoints=[L_遍历, L_累加, L_当, L_打印i,
                                              L_循环, L_循环体])
    hits = [s[0] for s in _stops(out)]
    assert hits.count(L_遍历) == 1
    assert hits.count(L_累加) == 3        # 遍历 [1,2,3]
    assert hits.count(L_当) == 1
    assert hits.count(L_打印i) == 2       # 当 i < 2
    assert hits.count(L_循环) == 1
    assert hits.count(L_循环体) == 1      # 循环 1 次


def test_parity_step_over_does_not_enter_function():
    """`下一步` 不进入被调函数：两个执行器都停在同一行。"""
    out, _ = _both(DEMO, ["下一步", "退出"], breakpoints=[L_乙])
    stops = _stops(out)
    assert stops[0][0] == L_乙
    assert stops[1][0] == L_主返回


def test_parity_step_out_returns_to_caller():
    """`跳出` 跑完当前函数回到调用处：栈与暂停点一致。"""
    out, _ = _both(DEMO, ["栈", "跳出", "退出"], breakpoints=[L_果])
    stops = _stops(out)
    assert stops[0] == (L_果, "函数「倍」", f"命中第 {L_果} 行的断点")
    assert stops[1][0] == L_主返回 and stops[1][1] == "函数「主」"
    assert f"  #0 第 {L_果:>4} 行  函数「倍」" in out
    assert f"  #1 第 {L_乙:>4} 行  函数「主」" in out
    assert f"  #2 第 {L_结果:>4} 行  模块顶层" in out


def test_parity_step_out_at_module_level_falls_back_to_continue():
    """在最外层按「跳出」= 继续跑（两个执行器同一句话）。"""
    out, _ = _both(DEMO, ["跳出", "退出"], breakpoints=[L_遍历])
    assert "已经在最外层了，按「继续」处理" in out


def test_parity_closure_reads_cell():
    """闭包：内层读外层变量（单元变量）——变量表与求值结果都要一致。"""
    out, _ = _both(CLOSURE, ["变量", "查看 基数 + 步长", "退出"], breakpoints=[4])
    assert "当前帧（函数「加」）的变量：" in out
    assert "  步长 = 5" in out
    assert "基数 + 步长 = 15" in out


def test_parity_class_body_statements_are_stoppable():
    """类体里的语句（方法定义 / 类变量）也是可停的语句，两个执行器一致。

    M39 之前这里是个**静默失效的断点**：`statement_lines` 说这些行可停，
    但两个执行器都把它们当「类体的一部分」直接跳过，谁也不会停。
    """
    script = ["继续"] * 9 + ["退出"]
    out, _ = _both(CLASSY, script,
                   breakpoints=[C_类, C_步长, C_初始化, C_初始化体,
                                C_加一次, C_自增值, C_返回, C_打印])
    hits = [s[0] for s in _stops(out)]
    # 类体里的每一句（类定义、类变量、两个方法定义）各命中一次
    assert hits.count(C_类) == 1
    assert hits.count(C_步长) == 1
    assert hits.count(C_初始化) == 1
    assert hits.count(C_加一次) == 1
    # 方法体在**被调用**时才命中
    assert hits.count(C_初始化体) == 1      # 计数器(10) 走一次构造
    assert hits.count(C_自增值) == 1
    assert hits.count(C_返回) == 1
    # 顺序也是源码顺序：先建类（类体的每一句按书写顺序），再造实例
    # （构造体在 `令 c = 计数器(10)` 那句里跑），最后走 `打印(...)` 与它的实参
    assert hits == [C_类, C_步长, C_初始化, C_加一次, C_初始化体,
                    C_打印, C_自增值, C_返回]


def test_parity_instance_method_call_gets_a_frame():
    """`实例.方法()` 也登记帧：能停、帧名对、栈上能看到调用点（M39 修）。"""
    src = ("类 狗：\n"
           "    函数 叫(自身)：\n"
           "        返回 \"汪\"\n"
           "\n"
           "令 d = 狗()\n"
           "打印(d.叫())\n")
    out, _ = _both(src, ["栈", "退出"], breakpoints=[3])
    assert _stops(out)[0] == (3, "函数「叫」", "命中第 3 行的断点")
    assert f"  #0 第 {3:>4} 行  函数「叫」" in out
    assert f"  #1 第 {6:>4} 行  模块顶层" in out


def test_parity_callback_from_python_side_gets_a_frame():
    """把基石函数交给 Python 侧（`列表.映射(翻倍)`）：能停，且调用点行号正确。

    调用点必须是**发起映射的那一句**（第 5 行），不能是回调体里那句 `打印`——
    实现时踩到过：内建调用也会更新「最近调用点」，于是第二次回调进来时
    栈上显示的是上一句 `打印` 的行号。现在只有「可能进基石函数的调用」
    才记调用点。
    """
    src = ("函数 翻倍(x)：\n"
           "    打印(x)\n"
           "    返回 x * 2\n"
           "\n"
           "令 结果 = [1, 2].映射(翻倍)\n"
           "打印(结果)\n")
    out, _ = _both(src, ["栈", "继续", "栈", "继续", "退出"], breakpoints=[2])
    assert _stops(out) == [(2, "函数「翻倍」", "命中第 2 行的断点"),
                           (2, "函数「翻倍」", "命中第 2 行的断点")]
    assert f"  #1 第 {5:>4} 行  模块顶层" in out
    assert "[2, 4]" in out


def test_parity_empty_block_is_stoppable():
    """空块 `:` 那一行也停得住（两个执行器都停）。"""
    src = ("如果 真：\n"
           "    :\n"
           "打印(\"过了\")\n")
    out, err = _both(src, ["继续", "退出"], breakpoints=[2])
    assert err is None
    assert _stops(out)[0][0] == 2


def test_parity_error_snapshot():
    """出错现场：出错那一帧的变量与调用栈，两个执行器一字不差。"""
    src = DEMO.replace("返回 果", "返回 果 + 无此名字")
    out, err = _both(src, ["继续"], stop_on_entry=True)
    assert err is not None and err.line == L_倍返回
    assert f"程序在第 {L_倍返回} 行出错了" in out
    assert "当前帧（函数「倍」）的变量：" in out
    assert "  值 = 10" in out and "  果 = 20" in out
    assert f"  #0 第 {L_倍返回:>4} 行  函数「倍」" in out
    assert f"  #1 第 {L_乙:>4} 行  函数「主」" in out
    assert f"  #2 第 {L_结果:>4} 行  模块顶层" in out


def test_parity_quit_runs_finally_and_is_not_caught():
    """`退出`：穿过 `最终`（清理照跑）、不被 `捕获` 吞掉——两个执行器同款。"""
    src = ("尝试：\n"
           "    令 甲 = 1\n"
           "    打印(\"尝试块结束\")\n"
           "捕获：\n"
           "    打印(\"不该被捕获\")\n"
           "最终：\n"
           "    打印(\"清理被执行\")\n")
    out, err = _both(src, ["退出"], breakpoints=[2])
    assert err is None
    assert "清理被执行" in out
    assert "不该被捕获" not in out
    assert "（调试会话已结束，程序没有跑完）" in out


def test_parity_error_still_runs_finally():
    """对照组：真异常也要穿 `最终`（确认上面那条不是把异常机制改坏了）。"""
    src = ("尝试：\n"
           "    令 甲 = 1 / 0\n"
           "捕获 除零错误：\n"
           "    打印(\"接住了\")\n"
           "最终：\n"
           "    打印(\"清理被执行\")\n")
    out, err = _both(src, ["继续"], stop_on_entry=True)
    assert err is None
    assert "接住了" in out and "清理被执行" in out


def test_parity_breakpoint_on_block_keyword_line_warns():
    """`否则：` 这类块关键字行：两个执行器都说「停不住」，不静默无效。"""
    src = ("如果 假：\n"
           "    打印(1)\n"
           "否则：\n"
           "    打印(2)\n")
    out, _ = _both(src, ["断点 3", "退出"], stop_on_entry=True)
    assert "第 3 行" in out
    assert "没有可停的语句" in out


def test_parity_missing_breakpoint_line():
    """断点落在空行上：同样明确提示。"""
    out, _ = _both(DEMO, ["断点 4", "退出"], stop_on_entry=True)
    assert "第 4 行" in out and "没有可停的语句" in out


# ---------------------------------------------------------------------------
# 2. 不变式：字节码语句标记 == AST 语句行
# ---------------------------------------------------------------------------

ALL_JSH = sorted([p for p in (ROOT / "examples").rglob("*.jsh")]
                 + [p for p in (ROOT / "tests" / "cases").rglob("*.jsh")])


@pytest.mark.parametrize("path", ALL_JSH)
def test_marks_match_ast_statement_lines(path):
    """真实语料上：字节码的「语句起始行」与 AST 的「语句行」逐个相同。

    两者一旦分叉，就意味着「某个断点在树遍历能停、在 VM 停不住」——
    正是 B5 最该拦下的那类问题。这条不变式在 M39 抓出过两个真问题
    （类体里的方法定义行、空块 `:` 行，两边都停不住）。
    """
    src = path.read_text(encoding="utf-8")
    program, lines = _parse(src, str(path))
    # ⚠️ M53：`compile_program` 内部会先把「用基石写的标准库」展开
    # （`导入 容器` → `容器 = $造模块(...)`，见 jishilib），会往 AST 里插入
    # 外壳函数的定义。这里**必须做同样的展开**，否则拿「原 AST 的语句行」
    # 去比「展开后字节码的语句标记」，多出来的库行号会让不变式假红。
    # `expand` 是幂等的（找不到 `导入` 就原样返回），所以不必担心叠加。
    from jishi import jishilib

    program = jishilib.expand(program)
    cmod = compile_program(program, str(path), lines)
    assert mark_lines(cmod) == statement_lines(program), str(path)


def test_marks_live_in_the_right_code_object():
    """函数/方法的标记打在**它自己的代码对象**里，而不是全塞进主代码。"""
    program, lines = _parse(CLOSURE)
    cmod = compile_program(program, "x.jsh", lines)
    per_code = {code.name: {ins.stmt_line for ins in code.instrs if ins.stmt_line}
                for code in cmod.codes}
    assert per_code["<模块>"] == {1, 7, 8, 9}
    assert per_code["计数器"] == {2, 3, 5}      # 含内层函数定义行与 `返回 加`
    assert per_code["加"] == {4}


def test_vm_debugger_stmt_lines_come_from_marks():
    """VmDebugger 的「可停行」取自字节码标记，而不是再解析一遍 AST。"""
    program, lines = _parse(CLOSURE)
    cmod = compile_program(program, "x.jsh", lines)
    dbg = VmDebugger(VM(cmod, "x.jsh"), source_lines=lines, filename="x.jsh",
                     script=[])
    assert dbg.stmt_lines == mark_lines(cmod)


def test_class_definition_keeps_its_own_mark():
    """类定义与其第一个方法**抢不到同一条指令**：两条语句各有一个标记。"""
    program, lines = _parse(CLASSY)
    cmod = compile_program(program, "x.jsh", lines)
    marks = sorted(mark_lines(cmod))
    assert C_类 in marks and C_步长 in marks and C_初始化 in marks


# ---------------------------------------------------------------------------
# 3. 字节码专有
# ---------------------------------------------------------------------------

def test_vm_variables_show_locals_not_temporaries():
    """变量表只列用户看得懂的：编译器内部的临时槽（`$tmp`）不出现。

    （`循环 n 次` 的计数器就住在 `$tmp` 槽里，所以这条同时钉住了
    「循环计数器不冒出来烦人」。）
    """
    src = ("循环 2 次：\n"
           "    令 甲 = 1\n"
           "    打印(甲)\n")
    out, _ = run_session("vm", src, ["变量", "继续"], breakpoints=[3])
    assert "  甲 = 1" in out
    assert "$tmp" not in out


def test_vm_variables_at_module_level_are_globals():
    """模块顶层的变量表 = 全局表（与树遍历的模块环境一致），内建默认藏起来。"""
    out, _ = run_session("vm", DEMO, ["变量", "继续"], breakpoints=[L_遍历])
    assert "当前帧（模块顶层）的变量：" in out
    assert "  结果 = 21" in out
    assert "  长度 = " not in out            # 内建不冒出来
    assert "（另有" in out and "变量 全部" in out


def test_vm_vars_all_reveals_builtins():
    out, _ = run_session("vm", DEMO, ["变量 全部", "继续"], breakpoints=[L_遍历])
    assert "  长度 = " in out
    assert "（另有" not in out


def test_vm_variables_inside_method():
    """方法里看变量：自身、参数与局部都在（与树遍历同一份表）。"""
    out, _ = run_session("vm", CLASSY, ["变量", "继续"], breakpoints=[C_返回])
    assert "当前帧（函数「加一次」）的变量：" in out
    assert "  自身 = " in out
    assert "  值 = " not in out            # 值是实例属性，不是局部变量


def test_vm_eval_in_frame_reads_locals():
    """「查看 表达式」用的是**暂停那一刻**的局部槽，不是重新算一遍。"""
    out, _ = run_session("vm", DEMO, ["查看 值 * 2", "查看 果 + 1", "继续"],
                         breakpoints=[L_倍返回])
    assert "值 * 2 = 20" in out
    assert "果 + 1 = 21" in out


def test_vm_eval_uses_globals_at_module_level():
    out, _ = run_session("vm", DEMO, ["查看 结果 * 10", "继续"],
                         breakpoints=[L_遍历])
    assert "结果 * 10 = 210" in out


def test_vm_eval_can_call_a_function():
    """求值里调函数：会真的走一遍调用，结果与直接算一致。"""
    out, _ = run_session("vm", DEMO, ["查看 倍(21)", "继续"],
                         breakpoints=[L_遍历])
    assert "倍(21) = 42" in out


def test_vm_eval_does_not_pause_inside_the_expression():
    """求值期间不触发暂停：命令中途停住会让用户完全摸不着头脑。"""
    out, _ = run_session("vm", DEMO, ["查看 倍(21)", "继续"],
                         breakpoints=[L_遍历, L_果, L_倍返回])
    # 程序自己调用 `倍` 时停在第 2、3 行，再停在第 10 行 = 共三处；
    # 「查看 倍(21)」又跑了一遍函数体，但**没有**再停在第 2、3 行
    assert [s[0] for s in _stops(out)] == [L_果, L_倍返回, L_遍历]
    assert "倍(21) = 42" in out


def test_vm_eval_error_does_not_kill_session():
    """求值出错只报一句中文错误，会话继续（命令循环不许被掀翻）。"""
    out, _ = run_session("vm", DEMO, ["查看 无此名字 + 1", "查看 结果", "继续"],
                         breakpoints=[L_遍历])
    assert "错误：" in out and "无此名字" in out
    assert "  结果 = 21" in out


def test_vm_eval_failure_leaves_program_intact():
    """求值失败后程序照常跑完（栈与帧都要恢复原样）。"""
    out, err = run_session("vm", DEMO,
                           ["查看 无此名字（", "查看 1 / 0", "继续"],
                           breakpoints=[L_遍历])
    assert err is None
    assert "结果 = 28" in out          # 程序自己的输出照旧


def test_vm_error_snapshot_inside_nested_call():
    """三层调用里出错：现场要指向**出错那一帧**，栈从内到外完整。"""
    src = ("函数 三层(项)：\n"
           "    返回 项 + 无此名字\n"
           "\n"
           "函数 二层(项)：\n"
           "    返回 三层(项)\n"
           "\n"
           "函数 一层(项)：\n"
           "    返回 二层(项)\n"
           "\n"
           "打印(一层(1))\n")
    out, err = run_session("vm", src, ["继续"], stop_on_entry=True)
    assert err is not None and err.line == 2
    assert "当前帧（函数「三层」）的变量：" in out
    assert "  项 = 1" in out
    assert f"  #0 第 {2:>4} 行  函数「三层」" in out
    assert f"  #1 第 {5:>4} 行  函数「二层」" in out
    assert f"  #2 第 {8:>4} 行  函数「一层」" in out
    assert f"  #3 第 {10:>4} 行  模块顶层" in out


def test_vm_quit_inside_nested_call_runs_finally_inside_out():
    """在深层函数里按「退出」：一路弹帧，`最终` 由内到外都跑，会话干净收尾。"""
    src = ("函数 内()：\n"
           "    尝试：\n"
           "        令 甲 = 1\n"
           "        打印(\"内层\")\n"
           "    最终：\n"
           "        打印(\"内层清理\")\n"
           "\n"
           "函数 外()：\n"
           "    尝试：\n"
           "        内()\n"
           "    最终：\n"
           "        打印(\"外层清理\")\n"
           "\n"
           "外()\n")
    out, err = run_session("vm", src, ["退出"], breakpoints=[3])
    assert err is None
    assert out.index("内层清理") < out.index("外层清理")
    assert "（调试会话已结束，程序没有跑完）" in out


def test_vm_quit_is_not_swallowed_by_bare_catch():
    """裸 `捕获:` 也吞不掉「退出」（VM 里它走的是「处理器优先、循环不接」）。"""
    src = ("尝试：\n"
           "    令 甲 = 1\n"
           "    打印(\"暂停点之后\")\n"
           "捕获：\n"
           "    打印(\"裸捕获吞掉了退出\")\n")
    out, err = run_session("vm", src, ["退出"], breakpoints=[2])
    assert err is None
    assert "裸捕获吞掉了退出" not in out
    assert "（调试会话已结束，程序没有跑完）" in out


def test_vm_breakpoints_inside_a_loop_body_hit_every_round():
    out, _ = run_session("vm", DEMO, ["继续"] * 4 + ["退出"],
                         breakpoints=[L_累加])
    hits = [s for s in _stops(out) if s[0] == L_累加]
    assert len(hits) == 3


# ---------------------------------------------------------------------------
# 4. 不改语义（带 / 不带调试器，输出逐字节相同）
# ---------------------------------------------------------------------------

def _plain_vm_output(src: str) -> str:
    program, lines = _parse(src)
    buf = io.StringIO()
    with redirect_stdout(buf):
        VM(compile_program(program, "t.jsh", lines), "t.jsh").run()
    return buf.getvalue()


def test_debugging_does_not_change_output():
    """带调试器（断点 + 单步 + 变量 + 求值）与不带，程序输出逐字节相同。"""
    src = ("令 合计 = 0\n"
           "遍历 甲 在 [1, 2, 3]：\n"
           "    如果 甲 == 2：\n"
           "        继续\n"
           "    合计 = 合计 + 甲\n"
           "打印(合计)\n")
    assert _plain_vm_output(src) == "4\n"
    out, err = run_session("vm", src, ["单步", "单步", "变量", "继续"],
                           breakpoints=[2], stop_on_entry=True)
    assert err is None
    assert "\n4\n" in out


def test_debugging_does_not_change_output_with_control_flow():
    """`中断`/`返回`/`尝试` 混在一起时也一样（钩子要能穿过它们）。"""
    src = ("函数 找(列表, 目标)：\n"
           "    遍历 项 在 列表：\n"
           "        如果 项 == 目标：\n"
           "            返回 真\n"
           "    返回 假\n"
           "尝试：\n"
           "    打印(\"甲\", 找([1, 2], 2))\n"
           "    打印(\"乙\", 找([1, 2], 9))\n"
           "捕获：\n"
           "    打印(\"不该到这里\")\n")
    assert _plain_vm_output(src) == "甲 真\n乙 假\n"
    out, err = run_session("vm", src, ["继续"],
                           breakpoints=list(range(1, 11)))
    assert err is None
    assert out.count("甲 真\n") == 1 and out.count("乙 假\n") == 1
    assert "不该到这里" not in out


def test_debugging_does_not_change_output_for_classes():
    """类体 + 方法调用：带与不带调试器结果一致。"""
    assert _plain_vm_output(CLASSY) == "13\n"
    out, err = run_session("vm", CLASSY, ["继续"] * 6 + ["退出"],
                           breakpoints=[C_类, C_步长, C_初始化, C_加一次,
                                        C_自增值, C_返回])
    assert err is None
    assert "\n13\n" in out


def test_debugging_does_not_change_errors():
    """出错也要一致：带调试器跑出来的报错类型与行号，和不带相同。"""
    src = "令 甲 = 1 / 0\n"
    program, lines = _parse(src)
    plain_err = None
    with redirect_stdout(io.StringIO()):
        try:
            VM(compile_program(program, "t.jsh", lines), "t.jsh").run()
        except Exception as e:                    # noqa: BLE001
            plain_err = e
    out, err = run_session("vm", src, ["继续"], stop_on_entry=True)
    assert err is not None and plain_err is not None
    assert (err.title, err.line) == (plain_err.title, plain_err.line)


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------

def _run_cli(argv, cwd=str(ROOT)):
    r = subprocess.run([sys.executable, "-m", "jishi.cli", *argv],
                       capture_output=True, cwd=cwd)
    return (r.returncode, r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def _write(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return str(f)


def test_cli_debug_vm_engine(tmp_path):
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, err = _run_cli(["调试", f, "--执行器", "vm", "--不停在入口",
                               "--断点", str(L_乙),
                               "--命令", "变量；查看 甲 * 2；继续"])
    assert code == 0, err
    assert "（调试执行器：字节码（Python VM）" in out
    assert f"命中第 {L_乙} 行的断点" in out
    assert "  甲 = 10" in out
    assert "甲 * 2 = 20" in out
    assert "结果 = 28" in out


def test_cli_debug_default_engine_is_tree_walk(tmp_path):
    """不写 `--执行器` 时行为与 M37 完全一致（连提示行都不多打）。"""
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, _ = _run_cli(["调试", f, "--不停在入口", "--断点", str(L_乙),
                             "--命令", "变量；继续"])
    assert code == 0
    assert "调试执行器" not in out
    assert f"命中第 {L_乙} 行的断点" in out


def test_cli_debug_two_engines_same_record(tmp_path):
    """CLI 层也对拍：两个执行器的会话记录除「执行器提示行」外逐字相同。"""
    f = _write(tmp_path, "演示.jsh", DEMO)
    tail = ["--不停在入口", "--断点", str(L_乙), "--命令", "变量；栈；继续"]
    _, a, _ = _run_cli(["调试", f] + tail)
    _, b, _ = _run_cli(["调试", f, "--执行器", "vm"] + tail)
    b = b.replace("（调试执行器：字节码（Python VM）；默认是树遍历）\n", "")
    assert a == b


def test_cli_debug_cvm_runs(tmp_path):
    """C VM 也能调试了（M40 补齐）——这里只验证 CLI 不再拒绝、能跑起来。

    三个执行器的会话记录逐字对拍在 `tests/test_m40_cvm_debugger.py`；
    这条留在这里是为了说明「M39 的拒绝」已经被 M40 的实现取代。
    """
    try:
        from jishi import cvm_bind
        cvm_bind.load_lib()
    except Exception:                                # noqa: BLE001
        pytest.skip("C 虚拟机动态库不可用（先 python cvm/build.py）")
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, err = _run_cli(["调试", f, "--执行器", "cvm",
                               "--断点", "2", "--命令", "变量；继续"])
    assert code == 0, err[-400:]
    assert "C 虚拟机" in out
    assert "命中第 2 行的断点" in out


def test_cli_debug_unknown_engine_rejected(tmp_path):
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, _, err = _run_cli(["调试", f, "--执行器", "某种引擎"])
    assert code == 2
    assert "--执行器" in err


def test_cli_debug_vm_via_pipe(tmp_path):
    """真子进程 + 管道：中文命令经 stdin 在字节码执行器下也不该变乱码。"""
    f = _write(tmp_path, "演示.jsh", DEMO)
    r = subprocess.run(
        [sys.executable, "-m", "jishi.cli", "调试", f, "--执行器", "vm",
         "--不停在入口", "-b", str(L_乙)],
        input="变量\n继续\n".encode("utf-8"), capture_output=True, cwd=str(ROOT))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-400:]
    assert "  甲 = 10" in out
    assert "结果 = 28" in out
    assert "\ufffd" not in out


def test_cli_debug_vm_reports_error_with_snapshot(tmp_path):
    """出错时：先打调试视角的现场，再打正式中文报错，退出码 1。"""
    f = _write(tmp_path, "坏的.jsh", DEMO.replace("返回 果", "返回 果 + 无此名字"))
    code, out, err = _run_cli(["调试", f, "--执行器", "vm", "--不停在入口",
                               "--命令", "继续"])
    assert code == 1
    assert "出错时的现场" in out
    assert "函数「倍」" in out
    assert "找不到这个名字" in err
