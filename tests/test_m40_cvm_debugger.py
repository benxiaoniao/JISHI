# -*- coding: utf-8 -*-
"""M40：C 虚拟机上的调试器——**三执行器会话记录逐字对拍**。

B5（M39）把调试器扩到了字节码执行器，C VM 当时判定为「暂不做」（理由：帧与
局部槽都在 C 侧，要另建一套宿主回调）。M40 把它补上了，做法与 Python VM 同源：

- C 侧新增**语句起始表**（与指令流平行的 int32 数组）+ **语句钩子**
  （只在语句首指令回调一次，不挂调试器时开销只是一次指针判空）；
- 帧与变量经 `jsvm_frame_meta` / `jsvm_dump_locals` 回调问出来；
- 命令循环、断点、暂停判定、出错现场全部复用 `_Session` 基类。

验收标准与 B5 一样：同一脚本 + 同一组断点 + 同一串命令，三个执行器的
**会话记录一字不差**。
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi.compiler import compile_program          # noqa: E402
from jishi.debugger import Debugger, VmDebugger, CvmDebugger  # noqa: E402
from jishi.interpreter import Interpreter           # noqa: E402
from jishi.parser import parse                      # noqa: E402
from jishi.tokenizer import tokenize                # noqa: E402
from jishi.vm import VM                             # noqa: E402


def _cvm_available() -> bool:
    try:
        from jishi import cvm_bind
        cvm_bind.load_lib()
        return True
    except Exception:                                # noqa: BLE001
        return False


HAS_CVM = _cvm_available()
pytestmark = pytest.mark.skipif(not HAS_CVM,
                                reason="C 虚拟机动态库不可用（先 python cvm/build.py）")


def _parse(src: str, name: str):
    lines = src.replace("\r\n", "\n").split("\n")
    return parse(tokenize(src, name), lines, name), lines


def run_session(engine: str, src: str, script, *, breakpoints=(),
                stop_on_entry: bool = False, name: str = "调试用例.jsh"):
    """跑一场调试会话，返回 ``(会话记录, 错误)``。"""
    program, lines = _parse(src, name)
    buf = io.StringIO()
    with redirect_stdout(buf):
        if engine == "vm":
            dbg = VmDebugger(VM(compile_program(program, name, lines), name),
                             source_lines=lines, filename=name,
                             breakpoints=breakpoints,
                             stop_on_entry=stop_on_entry, script=script)
        elif engine == "cvm":
            from jishi.cvm_bind import CvmRunner

            cmod = compile_program(program, name, lines)
            dbg = CvmDebugger(CvmRunner(cmod, name), source_lines=lines,
                              filename=name, breakpoints=breakpoints,
                              stop_on_entry=stop_on_entry, script=script)
        else:
            dbg = Debugger(Interpreter(filename=name), program,
                           source_lines=lines, filename=name,
                           breakpoints=breakpoints,
                           stop_on_entry=stop_on_entry, script=script)
        err = dbg.run()
    return buf.getvalue(), err


def _all_three(src: str, script, *, breakpoints=(), stop_on_entry: bool = False):
    """三个执行器各跑一遍并断言**逐字相同**，返回 C VM 那一份的结果。"""
    got = {}
    for engine in ("树遍历", "vm", "cvm"):
        got[engine] = run_session(engine, src, script,
                                  breakpoints=breakpoints,
                                  stop_on_entry=stop_on_entry)
    base_out, base_err = got["树遍历"]
    for engine in ("vm", "cvm"):
        out, err = got[engine]
        assert (base_err is None) == (err is None), (
            f"对错误的判定不同：树遍历 {base_err} / {engine} {err}")
        if base_err is not None:
            assert (base_err.title, base_err.line) == (err.title, err.line)
        assert base_out == out, (
            f"会话记录必须逐字相同\n──── 树遍历 ────\n{base_out}\n"
            f"──── {engine} ────\n{out}")
    return got["cvm"]


_STOP_RE = re.compile(r"暂停（(?P<reason>[^）]*)） 第 (?P<line>\d+) 行 · (?P<where>.*)")


def _stops(out: str) -> list[tuple[int, str, str]]:
    return [(int(m.group("line")), m.group("where"), m.group("reason"))
            for m in _STOP_RE.finditer(out)]


#: 覆盖函数调用、循环、闭包、类、异常的演示程序
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

L_倍体 = 2
L_主体 = 6
L_结果 = 9
L_遍历 = 10
L_打印 = 19


# ---------------------------------------------------------------------------
# 一、语句标记与可停行
# ---------------------------------------------------------------------------

def test_stmt_marks_reach_c_side():
    """C 侧拿到的语句起始表与指令流等长，非零项就是语句行。"""
    src = "令 甲 = 1\n打印(甲)\n"
    program, lines = _parse(src, "x.jsh")
    cmod = compile_program(program, "x.jsh", lines)
    from jishi.cvm_bind import CvmRunner

    runner = CvmRunner(cmod, "x.jsh")
    marks = list(runner._keep[-1])
    total = sum(len(c.instrs) for c in cmod.codes)
    assert len(marks) == total, "语句起始表必须与指令流等长"
    marked = {m for m in marks if m}
    assert marked == {1, 2}


def test_cvm_stoppable_lines_match_tree_walk():
    """三个执行器的「可停行」集合一致。"""
    program, lines = _parse(DEMO, "x.jsh")
    cmod = compile_program(program, "x.jsh", lines)
    from jishi.cvm_bind import CvmRunner
    from jishi.debugger import mark_lines, statement_lines

    assert mark_lines(cmod) == statement_lines(program)
    runner = CvmRunner(cmod, "x.jsh")
    marks = list(runner._keep[-1])
    assert {m for m in marks if m} == statement_lines(program)


# ---------------------------------------------------------------------------
# 二、三执行器逐字对拍
# ---------------------------------------------------------------------------

def test_parity_breakpoint_and_vars():
    out, err = _all_three(DEMO, ["变量", "继续"],
                          breakpoints=[L_倍体])
    assert err is None
    assert "命中第 2 行的断点" in out
    assert "  值 = 10" in out


def test_parity_stack_shows_full_chain():
    out, _ = _all_three(DEMO, ["栈", "继续"], breakpoints=[L_倍体])
    assert "调用栈（由内到外）：" in out
    assert f"  #0 第 {L_倍体:>4} 行  函数「倍」" in out
    assert f"  #1 第 {L_主体:>4} 行  函数「主」" in out
    assert f"  #2 第 {L_结果:>4} 行  模块顶层" in out


def test_parity_step_into_and_over():
    """断点处按「单步」：停在下一条**语句**（进函数则停在被调函数第一句）。

    注意命令的消费时机：`继续` 会让会话立刻返回、**不等**后续命令，
    所以「单步」是在**下一次暂停时**才被读到的。因此脚本要写
    `继续 → 命中断点 → 单步 → 停到下一句`，而不是指望一次暂停吃两条命令。
    """
    out, _ = _all_three(DEMO, ["继续", "单步", "退出"],
                        breakpoints=[L_结果, L_主体])
    lines = [s[0] for s in _stops(out)]
    assert lines[:2] == [L_结果, L_主体]


def test_parity_next_steps_over_call():
    """`下一步` 不进入函数：从调用行直接走到本帧的下一条语句。"""
    out, _ = _all_three(DEMO, ["继续", "下一步", "退出"],
                        breakpoints=[L_主体, L_主体 + 1])
    stops = [(s[0], s[1]) for s in _stops(out)]
    assert stops[:2] == [(L_主体, "函数「主」"),
                         (L_主体 + 1, "函数「主」")]


def test_parity_step_out_returns_to_caller():
    """`跳出`：从被调函数回到调用方（两个执行器都停在调用者的下一句）。"""
    out, _ = _all_three(DEMO, ["继续", "跳出", "退出"],
                        breakpoints=[L_倍体, L_主体 + 1])
    stops = [(s[0], s[1]) for s in _stops(out)]
    assert stops[0] == (L_倍体, "函数「倍」")
    assert stops[1][1] == "函数「主」"


def test_parity_loops_stop_once_at_head():
    """循环头只停一次、循环体每轮都停——三个执行器一致。"""
    out, _ = _all_three(DEMO, ["继续"] * 12 + ["退出"],
                        breakpoints=[L_遍历])
    hits = [s[0] for s in _stops(out)]
    assert hits.count(L_遍历) == 1


def test_parity_stop_on_entry():
    out, _ = _all_three(DEMO, ["退出"], stop_on_entry=True)
    assert "停在入口" in out


def test_parity_empty_script_runs_to_completion():
    out, err = _all_three(DEMO, [])
    assert err is None
    assert "（程序正常结束" in out
    assert "结果 = 28" in out


def test_parity_module_level_only():
    """只在模块顶层停：栈里只有「模块顶层」一层。"""
    src = "令 甲 = 1\n令 乙 = 甲 + 1\n打印(乙)\n"
    out, _ = _all_three(src, ["栈", "继续"], breakpoints=[2])
    assert "  #0 第    2 行  模块顶层" in out
    assert "#1" not in out


def test_parity_closure_variables():
    """闭包：本函数自己的单元变量照常显示。"""
    src = ("函数 计数器(起始)：\n"
           "    令 当前 = 起始\n"
           "    函数 加(步长)：\n"
           "        返回 当前 + 步长\n"
           "    返回 加\n"
           "令 加 = 计数器(10)\n"
           "打印(加(5))\n")
    out, err = _all_three(src, ["变量", "继续"], breakpoints=[4])
    assert err is None
    assert "  步长 = 5" in out


def test_parity_class_method():
    """类方法里的暂停：帧名与变量都要一致。"""
    src = ("类 计数器：\n"
           "    函数 初始化(自身, 起点)：\n"
           "        自身.值 = 起点\n"
           "    函数 加(自身, 步长)：\n"
           "        自身.值 = 自身.值 + 步长\n"
           "        返回 自身.值\n"
           "令 c = 计数器(10)\n"
           "打印(c.加(5))\n")
    out, err = _all_three(src, ["栈", "变量", "继续"], breakpoints=[5])
    assert err is None
    assert "函数「加」" in out
    assert "  步长 = 5" in out


def test_parity_nested_loops_and_try():
    """`尝试/捕获` + 嵌套循环混在一起，暂停序列也要一致。"""
    src = ("令 合计 = 0\n"
           "遍历 甲 在 [1, 2]：\n"
           "    遍历 乙 在 [1, 2]：\n"
           "        合计 = 合计 + 甲 * 乙\n"
           "尝试：\n"
           "    合计 = 合计 + 1\n"
           "捕获 值错误：\n"
           "    合计 = 0\n"
           "打印(合计)\n")
    out, err = _all_three(src, ["继续"] * 20 + ["退出"],
                          breakpoints=[1, 2, 3, 4, 5, 6, 7, 9])
    assert err is None
    assert "\n10\n" in out              # 程序自己打印的合计


def test_parity_error_snapshot():
    """出错现场：出错那一帧的变量与调用栈，三个执行器一致。"""
    src = ("函数 内层(值)：\n"
           "    返回 值 / 0\n"
           "\n"
           "函数 外层(值)：\n"
           "    令 中间 = 值 + 1\n"
           "    返回 内层(中间)\n"
           "\n"
           "打印(外层(1))\n")
    out, err = _all_three(src, ["继续"], breakpoints=[1])
    assert err is not None
    assert "出错时的现场" in out
    assert "当前帧（函数「内层」）的变量：" in out
    assert "  值 = 2" in out
    assert "  #0 第    2 行  函数「内层」" in out
    assert "  #1 第    6 行  函数「外层」" in out
    assert "  #2 第    8 行  模块顶层" in out


def test_parity_quit_runs_finally():
    """「退出」穿 `最终` 块——三个执行器一致。

    断点要设在**尝试体内**：钩子是「语句执行前」触发的，停在 `尝试：` 那一行
    时还没进 try，自然没有 `最终` 要跑（与 M37 同一个注意点）。
    """
    src = ("尝试：\n"
           "    令 甲 = 1\n"
           "最终：\n"
           "    打印(\"清理被执行\")\n")
    out, err = _all_three(src, ["退出"], breakpoints=[2])
    assert err is None
    assert "清理被执行" in out
    assert "（调试会话已结束，程序没有跑完）" in out


def test_parity_debugging_does_not_change_output():
    """带调试器跑与不带，程序自身输出必须相同（钩子只读）。"""
    plain = io.StringIO()
    with redirect_stdout(plain):
        from jishi.vm import run_source as vm_run
        vm_run(DEMO, filename="x.jsh")
    out, err = _all_three(DEMO, ["单步", "单步", "变量", "继续"],
                          stop_on_entry=True)
    assert err is None
    assert plain.getvalue() == "0\n1\n结果 = 28\n"
    assert "\n0\n" in out and "结果 = 28\n" in out


def test_parity_dead_breakpoint_warning():
    """断点加在空行/注释行上：三个执行器都要说「这一行没有可停的语句」。"""
    out, _ = _all_three(DEMO, ["断点 4", "退出"], stop_on_entry=True)
    assert "第 4 行" in out
    assert "没有可停的语句" in out


# ---------------------------------------------------------------------------
# 三、C VM 特有的边界（如实标注，不静默）
# ---------------------------------------------------------------------------

def test_cvm_inplace_eval_is_refused_clearly():
    """C VM 不支持就地求值：要**明确说出来**，不能静默给错值。"""
    out, err = run_session("cvm", DEMO, ["查看 值 * 2", "继续"],
                           breakpoints=[L_倍体])
    assert err is None
    assert "暂不支持就地求值" in out
    assert "值 * 2 =" not in out


def test_tree_walk_and_vm_do_support_inplace_eval():
    """另两个执行器照常支持——免得上面那条把功能一起关掉。"""
    out, _ = run_session("vm", DEMO, ["查看 值 * 2", "继续"],
                         breakpoints=[L_倍体])
    assert "值 * 2 = 20" in out


def test_cvm_vars_hide_builtins_by_default():
    """内建/异常类型默认藏起来（与另两个执行器同一口径）。"""
    src = "令 我的变量 = 1\n打印(我的变量)\n"
    out, _ = run_session("cvm", src, ["变量", "继续"], breakpoints=[2])
    assert "  我的变量 = 1" in out
    assert "  长度 = " not in out
    assert "变量 全部" in out


def test_cvm_internal_tmp_slots_are_hidden():
    """编译器内部临时槽（`$tmpN`）不显示。"""
    src = ("令 合计 = 0\n"
           "遍历 项 在 [1, 2, 3]：\n"
           "    合计 = 合计 + 项\n"
           "打印(合计)\n")
    out, _ = run_session("cvm", src, ["变量", "继续"], breakpoints=[3])
    assert "$tmp" not in out
    assert "  合计 = " in out


# ---------------------------------------------------------------------------
# 四、CLI 端到端
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _run_cli(args, **kw):
    r = subprocess.run([sys.executable, "-m", "jishi.cli"] + args,
                       capture_output=True, cwd=str(ROOT), **kw)
    return (r.returncode, r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def test_cli_cvm_engine_runs(tmp_path):
    f = _write(tmp_path, "演示.jsh", DEMO)
    code, out, err = _run_cli(["调试", f, "--执行器", "cvm",
                               "--不停在入口", "--断点", "2",
                               "--命令", "变量；继续"])
    assert code == 0, err[-400:]
    assert "C 虚拟机" in out
    assert "命中第 2 行的断点" in out
    assert "  值 = 10" in out


def test_cli_three_engines_agree(tmp_path):
    """命令行端到端：三个执行器的会话记录逐字相同（提示行除外）。"""
    f = _write(tmp_path, "演示.jsh", DEMO)
    outs = []
    for engine in ("树遍历", "vm", "cvm"):
        code, out, err = _run_cli(["调试", f, "--执行器", engine,
                                   "--断点", "2", "--命令", "栈；变量；继续"])
        assert code == 0, err[-400:]
        outs.append(out)
    # 各执行器会多打一行「（调试执行器：…）」的提示，去掉它再比
    norm = [re.sub(r"^（调试执行器：.*\n", "", o, flags=re.M) for o in outs]
    assert norm[0] == norm[1] == norm[2], (
        "──── 树遍历 ────\n" + norm[0] + "\n──── 字节码 ────\n" + norm[1]
        + "\n──── C 虚拟机 ────\n" + norm[2])
