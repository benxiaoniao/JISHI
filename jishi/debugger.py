# -*- coding: utf-8 -*-
"""基石调试器（M37 · 主线 B1：最小可用 —— 断点 / 单步 / 看变量 / 看调用栈）。

为什么先做树遍历
----------------
路线图的既定顺序是「树遍历 → Python VM → C VM」，B1 落在树遍历上，理由是
**树遍历里「一个语句 = 一次 ``exec_stmt``」本身就是天然的暂停点**：在
``exec_stmt`` 开头挂一个钩子，同一处就同时给出了断点、单步（进入/越过/跳出）、
调用栈与局部变量。字节码执行器没有这种「语句边界」事件（栈机只知道指令，
要么每步比对行号表、要么加新指令），C VM 还要先让宿主回调把帧传出来。
所以本模块**只服务树遍历**，`docs/embed.md` 与路线图如实标注
「Python VM / C VM 暂不支持调试」。

三条设计原则
------------
1. **调试器不改语义**：钩子只读（读语句行号、读环境），暂停时也不动环境。
   所以「带调试器跑」与「不带调试器跑」的输出必须逐字节相同——有测试钉住。
2. **中文命令**，与语言本身一致；同时给英文字母做别名（`c`/`s`/`n`/`b`/`p`…），
   让习惯 gdb 的人手感不掉。
3. **可测**：读命令与写输出都能注入，于是整场会话（含单步序列、出错现场）
   都能在单元测试里用一段命令脚本跑完，不需要真终端。

「退出」为什么用 `DebugQuit`（BaseException）
-------------------------------------------
用户在暂停时按「退出」是想**立刻结束**，而不是让程序里的 `尝试/捕获` 把它接住
——一句 `捕获：` 就能吞掉「退出」的话，用户会觉得调试器失灵。所以 `DebugQuit`
刻意不继承 `JishiError`，并在 `interpreter._do_try` 里显式放行（那边是
`except BaseException`，为的是把 Python 异常翻译成中文）。
"""

from __future__ import annotations

import dataclasses
import sys
from typing import Any, Callable, Iterable, Optional

from . import ast_nodes as A
from .errors import DebugQuit, JishiError
from .runtime import jishi_repr, new_builtins

#: 暂停时显示的主提示符（非交互模式也会把它打出来，好读）
PROMPT = "调试 > "

#: 只有语句才有「可停的行」。表达式节点也有 line/col，但断点落在它们上面是
#: 没有意义的（一个语句里可能有好几个表达式、而且不一定会被求值）。
_STMT_TYPES = (
    A.Assign, A.ExprStmt, A.If, A.For, A.Loop, A.While, A.Break, A.Continue,
    A.FuncDef, A.ClassDef, A.Return, A.Raise, A.Try, A.Import, A.Pass,
)

#: 能返回「继续跑」的命令 → 返回值为恢复模式；其余命令返回 None（继续待命）
_HELP_LINES = (
    "命令（中文或英文别名都行）：",
    "  继续 / c                跑到下一个断点（没有断点就跑完）",
    "  单步 / s                执行下一条语句（会进入被调用的函数）",
    "  下一步 / n              执行到本帧的下一条语句（不进入函数）",
    "  跳出 / o                跑完当前函数，回到调用它的地方",
    "  断点 / b                列出断点；「断点 12」或「断点 12,15」添加",
    "  取消断点 12             删掉某个断点；「清空断点」全删",
    "  变量 / v                看当前帧的变量（「变量 全部」连内建与临时变量）",
    "  查看 表达式 / p         在当前帧里求值，例如「查看 单价 * 数量」",
    "  栈 / bt                 看调用栈（由内到外，带行号）",
    "  源码 [行号] / l         看当前行（或指定行）附近的源码",
    "  帮助 / h                显示这张表",
    "  退出 / q                结束调试会话（程序不再往下跑）",
    "  （直接回车 = 重复上一条命令）",
)

#: 命令别名 → 规范名。让「p」「bt」这类 gdb 习惯也能用。
_ALIASES = {
    "c": "继续", "cont": "继续", "continue": "继续", "r": "继续", "run": "继续",
    "s": "单步", "step": "单步",
    "n": "下一步", "next": "下一步",
    "o": "跳出", "out": "跳出", "finish": "跳出",
    "b": "断点", "break": "断点",
    "v": "变量", "local": "变量", "locals": "变量",
    "p": "查看", "print": "查看", "求值": "查看",
    "bt": "栈", "w": "栈", "where": "栈", "调用栈": "栈",
    "l": "源码", "list": "源码",
    "h": "帮助", "?": "帮助", "help": "帮助",
    "q": "退出", "quit": "退出", "exit": "退出",
    "d": "取消断点", "delete": "取消断点",
}


# ---------------------------------------------------------------------------
# AST 小工具
# ---------------------------------------------------------------------------

def iter_nodes(value: Any) -> Iterable[A.Node]:
    """按 dataclass 字段递归遍历一棵 AST（值可以是节点、列表或普通值）。

    **不写死「每种节点有哪些子节点」是有意的**：AST 每加一种节点（M24 的
    `Lambda`、M36 的条件表达式脱糖…），写死的遍历就得跟着改，漏一处就成了
    「某些行的断点永远不停」——一个非常难查的问题。节点全是 dataclass，
    按字段走就不会漏。
    """
    if isinstance(value, A.Node):
        yield value
        for f in dataclasses.fields(value):
            yield from iter_nodes(getattr(value, f.name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_nodes(item)


def statement_lines(program: A.Program) -> set[int]:
    """程序里所有**语句**的起始行（只有这些行能设断点）。"""
    return {n.line for n in iter_nodes(program) if isinstance(n, _STMT_TYPES)}


def parse_lines(spec: Iterable[Any]) -> list[int]:
    """把「12,15」「12 15」这类行号写法解析成整数列表（中文逗号也认）。"""
    out: list[int] = []
    for item in spec:
        text = str(item).replace("，", ",").replace(",", " ")
        for piece in text.split():
            if not piece.isdigit():
                raise ValueError(piece)
            out.append(int(piece))
    return out


# ---------------------------------------------------------------------------
# 调试器
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class DebugFrame:
    """调试器的调用帧：函数名 + **那一帧的环境**（用于看变量）+ 调用点。

    与 `errors.Frame` 的区别：那个只记「函数名 + 调用点」（渲染错误回溯够用），
    这里必须留着 `env`，否则「在函数里暂停时看局部变量」就无从谈起。
    """

    name: str
    env: Any
    call_line: int
    call_col: int = 1


@dataclasses.dataclass
class _Snapshot:
    """出错现场的快照（M37）。

    为什么要专门存一份：**错误是在语句执行到一半时冒出来的**，等它传到
    `run()` 时，`_eval_call` 的 `finally` 已经把帧全弹掉了——那时再去看
    「当前帧」，看到的会是模块顶层、变量也可能是上一帧的（真实踩到过：
    错误出在函数「主」里，快照却写着「模块顶层」）。所以每执行一条语句就把
    「此刻在哪、哪个帧、有哪些变量、栈长什么样」**复制**一份留底。
    """

    line: int
    label: str
    env: Any
    chain: list[tuple[int, str]]


class Debugger:
    """挂在 `Interpreter` 上的调试器（`interp.debugger = Debugger(...)`）。

    钩子只有两个：`before_stmt`（每个语句执行前）与 `push_frame`/`pop_frame`
    （进出函数）。语义零改动——它们只读不写。
    """

    def __init__(self, interp, program: A.Program, *,
                 source_lines: Iterable[str],
                 filename: str = "<输入>",
                 breakpoints: Iterable[Any] = (),
                 stop_on_entry: bool = True,
                 read_line: Optional[Callable[[str], str]] = None,
                 write: Optional[Callable[[str], None]] = None,
                 script: Optional[Iterable[str]] = None):
        self.interp = interp
        self.program = program
        self.filename = filename
        self.lines = list(source_lines)
        self.breakpoints: set[int] = set()
        self.stop_on_entry = stop_on_entry
        self.quit_requested = False
        #: 程序里「可停的语句行」，设断点时用来提示「这行停不住」
        self.stmt_lines = statement_lines(program)
        #: 语言自带的全局名字（内建函数 + 异常类型 + `空`）。「看变量」默认
        #: 把它们藏起来——模块顶层本来会一次冒出三十多个，把用户的变量淹掉。
        #: 按**名字**判断而不是按值的类型判断：`值错误` 这类异常类型是 Python
        #: 类，而用户写的 `类` 也可能落成 Python 类，按类型判会误伤。
        self._builtin_names = set(new_builtins())

        self._write_line = write or _default_write
        self._read_line = read_line or _default_read_line
        #: 命令脚本（非 None 时按顺序自动执行；用完自动「继续」）
        self._script: Optional[list[str]] = list(script) if script is not None else None

        self._frames: list[DebugFrame] = []
        self._env: Optional[Any] = None
        self._snapshot: Optional[_Snapshot] = None
        self._line = 1
        #: 恢复模式：run / step / next / out；entry 只用于「停在入口」那一次
        self._mode = "entry" if stop_on_entry else "run"
        self._paused_depth = 0
        self._last_cmd: Optional[tuple[str, str]] = None
        #: 暂停次数（会话结束时报给用户，也是测试里判断「停了几次」的依据）
        self.stops = 0
        self._help_shown = False
        self.add_breakpoints(breakpoints)
        interp.debugger = self

    # -- 断点 ---------------------------------------------------------------

    def add_breakpoints(self, spec: Iterable[Any]) -> tuple[list[int], list[int]]:
        """加断点；返回 ``(能停, 停不住)`` 两组行号。

        「停不住」不是错误（行号可能写在空行、注释行、`否则` 行上），但必须
        **说出来**——否则用户看着断点列表里明明有第 12 行、程序却从不停，
        只会怀疑调试器坏了。
        """
        nums = parse_lines(spec)
        usable: list[int] = []
        dead: list[int] = []
        for n in nums:
            self.breakpoints.add(n)
            (usable if n in self.stmt_lines else dead).append(n)
        return sorted(usable), sorted(dead)

    def clear_breakpoints(self) -> None:
        self.breakpoints.clear()

    # -- 执行器调用的钩子 ---------------------------------------------------

    def before_stmt(self, stmt: A.Node, env: Any) -> None:
        """每个语句执行前调用（见 `interpreter.exec_stmt` 开头）。"""
        line = getattr(stmt, "line", None) or self._line
        self._line = line
        self._env = env
        # 顺手留一份现场：错误可能在**这条语句执行到一半**时冒出来，那时帧
        # 已经被 finally 弹掉了（见 _Snapshot 的说明）。
        self._snapshot = _Snapshot(line, self._frame_label(), env,
                                   self._live_chain())
        depth = len(self._frames)
        mode = self._mode
        if mode == "entry":
            self._pause("停在入口")
        elif mode == "step":
            self._pause("单步")
        elif mode == "next" and depth <= self._paused_depth:
            self._pause("下一步")
        elif mode == "out" and depth < self._paused_depth:
            self._pause("跳出")
        elif mode == "run" and line in self.breakpoints:
            self._pause(f"命中第 {line} 行的断点")

    def push_frame(self, name: str, env: Any, call_line: int,
                   call_col: int = 1) -> None:
        self._frames.append(DebugFrame(name, env, call_line, call_col))

    def pop_frame(self) -> None:
        if self._frames:
            self._frames.pop()

    # -- 会话 ---------------------------------------------------------------

    def run(self) -> Optional[JishiError]:
        """跑程序；返回 ``None`` 表示正常跑完或用户主动退出。"""
        try:
            self.interp.run(self.program)
        except DebugQuit:
            self.quit_requested = True
            self._write_line("（调试会话已结束，程序没有跑完）")
            return None
        except JishiError as e:
            self.report_error(e)
            return e
        self._write_line(f"（程序正常结束，本次共暂停 {self.stops} 次）")
        return None

    def report_error(self, err: JishiError) -> None:
        """出错时按**调试视角**打一份现场快照（出错行 + 当前帧变量 + 调用栈）。

        与 `JishiError.render()` 的分工：那个是给所有人看的「错误 + 源码 + ^」，
        这里补的是程序员此刻最想知道的那句「**变量是什么、我是从哪儿调过来的**」。
        CLI 会先让调试器打快照、再打正式错误，两段互补。
        """
        line = err.line if err.line is not None else self._line
        self._write_line("")
        snap = self._snapshot
        where = (f"（最后执行到 第 {snap.line} 行 · {snap.label}）"
                 if snap is not None else "")
        self._write_line(f"程序在第 {line} 行出错了 —— 出错时的现场{where}：")
        if snap is None:
            self._write_line("（错误发生在任何语句之前，没有变量快照）")
            return
        self._dump_vars("", snap.env, snap.label)
        self._dump_stack(snap.chain)
        self._write_line("（想复盘就改脚本、带上同一组断点再跑一次）")

    # -- 暂停与命令 ---------------------------------------------------------

    def _pause(self, reason: str) -> None:
        self.stops += 1
        self._paused_depth = len(self._frames)
        self._write_line("")
        self._write_line(f"暂停（{reason}） 第 {self._line} 行 · {self._frame_label()}")
        self._write_line(self._source_line(self._line))
        if not self._help_shown:
            self._help_shown = True
            self._write_line("（输入「帮助」看命令）")

        while True:
            raw = self._read_command()
            cmd, arg = self._parse_command(raw)
            if cmd is None:
                continue
            if cmd == "重复":
                if self._last_cmd is None:
                    self._write_line("（还没有上一条命令）")
                    continue
                cmd, arg = self._last_cmd
            else:
                self._last_cmd = (cmd, arg)
            try:
                mode = getattr(self, _COMMANDS[cmd])(arg)
            except JishiError as e:
                # 命令本身出错（多半是「查看」里的表达式）不该把调试会话掀翻：
                # 报一句中文错误，继续待命。
                self._write_line(f"错误：{e.title}"
                                 + (f"（{e.message}）" if e.message else ""))
                if e.hint:
                    self._write_line(f"  提示：{e.hint}")
                continue
            if mode:
                self._mode = mode
                return

    def _read_command(self) -> str:
        if self._script is not None:
            if self._script:
                cmd = self._script.pop(0).strip()
                self._write_line(PROMPT + cmd)
                return cmd
            self._write_line(PROMPT + "（命令用完了，自动继续）")
            return "继续"
        return self._read_line(PROMPT)

    def _parse_command(self, raw: str) -> tuple[Optional[str], str]:
        text = (raw or "").strip()
        if not text:
            return "重复", ""
        # split(None, 1) 会把全角空格（中文输入法下很常见）也当分隔符
        parts = text.split(None, 1)
        head = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        head = _ALIASES.get(head, head)
        if head in _COMMANDS:
            return head, arg
        self._write_line(f"不认识这个命令：「{head}」——输入「帮助」看有哪些")
        return None, ""

    # -- 各命令（返回恢复模式，或 None 表示继续待命）--------------------------

    def _cmd_continue(self, arg: str) -> Optional[str]:
        return "run"

    def _cmd_step(self, arg: str) -> Optional[str]:
        return "step"

    def _cmd_next(self, arg: str) -> Optional[str]:
        return "next"

    def _cmd_out(self, arg: str) -> Optional[str]:
        if not self._frames:
            self._write_line("（已经在最外层了，按「继续」处理）")
            return "run"
        return "out"

    def _cmd_break(self, arg: str) -> None:
        if not arg:
            if not self.breakpoints:
                self._write_line("（还没有断点，用「断点 行号」加）")
                return None
            self._write_line("断点：")
            for n in sorted(self.breakpoints):
                if n in self.stmt_lines:
                    self._write_line(f"  第 {n} 行")
                else:
                    self._write_line(f"  第 {n} 行  ← 这一行没有可停的语句，不会停")
            return None
        try:
            nums = parse_lines([arg])
        except ValueError as e:
            self._write_line(f"看不懂这个行号：「{e.args[0]}」"
                             "——写成「断点 12」，多个用逗号或空格隔开")
            return None
        if not nums:
            self._write_line("（没写行号）")
            return None
        usable, dead = self.add_breakpoints(nums)
        if usable:
            self._write_line("已设断点：" + "、".join(f"第 {n} 行" for n in usable))
        if dead:
            nums_txt = "、".join(f"第 {n} 行" for n in dead)
            self._write_line(f"提示：{nums_txt} 上没有可停的语句（{_DEAD_LINE_HINT}）"
                             "——这个断点不会触发")
        return None

    def _cmd_unbreak(self, arg: str) -> None:
        if not arg:
            self._write_line("用法：取消断点 行号（一次删一个，多个用逗号隔开）")
            return None
        try:
            nums = parse_lines([arg])
        except ValueError as e:
            self._write_line(f"看不懂这个行号：「{e.args[0]}」")
            return None
        gone = [n for n in nums if n in self.breakpoints]
        for n in gone:
            self.breakpoints.discard(n)
        if gone:
            self._write_line("已删除：" + "、".join(f"第 {n} 行" for n in gone))
        else:
            self._write_line("（这几个行号上本来就没有断点）")
        return None

    def _cmd_clear(self, arg: str) -> None:
        n = len(self.breakpoints)
        self.clear_breakpoints()
        self._write_line(f"已清空 {n} 个断点")
        return None

    def _cmd_vars(self, arg: str) -> None:
        self._dump_vars(arg)
        return None

    def _cmd_eval(self, arg: str) -> None:
        text = arg.strip()
        if not text:
            self._write_line("用法：查看 表达式（例如 查看 单价 * 数量）")
            return None
        if self._env is None:
            self._write_line("（还没有执行到任何语句，没法求值）")
            return None
        from .parser import parse_expression
        node = parse_expression(text, "<调试>")
        value = self.interp.eval_expr(node, self._env)
        self._write_line(f"  {text} = {jishi_repr(value, top=True)}")
        return None

    def _cmd_stack(self, arg: str) -> None:
        self._dump_stack()
        return None

    def _cmd_source(self, arg: str) -> None:
        center = self._line
        if arg.strip():
            if not arg.strip().isdigit():
                self._write_line("用法：源码 [行号]（不写就按当前行）")
                return None
            center = int(arg.strip())
        span = 5
        lo = max(1, center - span)
        hi = min(len(self.lines), center + span)
        self._write_line(f"源码（第 {lo}–{hi} 行，标记的是第 {center} 行）：")
        for n in range(lo, hi + 1):
            self._write_line(_number_line(n, self.lines[n - 1],
                                          len(str(len(self.lines))),
                                          center))
        return None

    def _cmd_help(self, arg: str) -> None:
        for line in _HELP_LINES:
            self._write_line(line)
        return None

    def _cmd_quit(self, arg: str) -> None:
        raise DebugQuit()

    # -- 输出片段 -----------------------------------------------------------

    def _frame_label(self) -> str:
        if self._frames:
            return f"函数「{self._frames[-1].name}」"
        return "模块顶层"

    def _source_line(self, n: int) -> str:
        text = self.lines[n - 1] if 1 <= n <= len(self.lines) else ""
        return _number_line(n, text, len(str(max(1, len(self.lines)))), n)

    def _is_noise(self, name: str) -> bool:
        """这个全局名字要不要在「看变量」里默认藏起来。

        - `__` 开头的是**脱糖留下的临时变量**（`用 … 为` 的 `__用_N__`、
          `匹配` 的 `__匹配_N__`），对用户没有意义；
        - 内建函数与异常类型（`长度`、`打印`、`值错误`…）在模块顶层会一次
          冒出来三十多个，把用户的变量挤到屏幕外；想看用「变量 全部」。
        """
        return name.startswith("__") or name in self._builtin_names

    def _dump_vars(self, arg: str, env: Any = None, label: Optional[str] = None) -> None:
        """打印某一帧的变量。`env`/`label` 不传时用**当前暂停处**的。"""
        show_all = arg.strip() in ("全部", "all", "-a")
        if env is None and label is None:
            env, label = self._env, self._frame_label()
        self._write_line(f"当前帧（{label}）的变量：")
        if env is None:
            self._write_line("  （还没有执行到任何语句）")
            return
        vars_map = dict(getattr(env, "vars", {}) or {})
        shown: list[tuple[str, Any]] = []
        hidden: list[str] = []
        for name in sorted(vars_map):
            if not show_all and self._is_noise(name):
                hidden.append(name)
            else:
                shown.append((name, vars_map[name]))
        if not shown:
            self._write_line("  （没有）")
        for name, value in shown:
            self._write_line(f"  {name} = {jishi_repr(value, top=True)}")
        if hidden:
            self._write_line(f"  （另有 {len(hidden)} 项内建/临时变量没显示，"
                             "要看用「变量 全部」）")

    def _live_chain(self) -> list[tuple[int, str]]:
        """把「当前在哪一行、各层是从哪里调进来的」算成一张由内到外的表。"""
        if not self._frames:
            return [(self._line, "模块顶层")]
        chain = [(self._line, f"函数「{self._frames[-1].name}」")]
        # self._frames 是**由外到内**存的，而每帧只记了自己的调用点，
        # 所以相邻两层要错位配对；最外层函数的调用点在模块顶层。
        for j in range(len(self._frames) - 1, 0, -1):
            chain.append((self._frames[j].call_line,
                          f"函数「{self._frames[j - 1].name}」"))
        chain.append((self._frames[0].call_line, "模块顶层"))
        return chain

    def _dump_stack(self, chain: Optional[list[tuple[int, str]]] = None) -> None:
        self._write_line("调用栈（由内到外）：")
        for i, (line, label) in enumerate(chain or self._live_chain()):
            self._write_line(f"  #{i} 第 {line:>4} 行  {label}")


#: 命令表：规范名 → Debugger 上的方法名
_COMMANDS = {
    "继续": "_cmd_continue",
    "单步": "_cmd_step",
    "下一步": "_cmd_next",
    "跳出": "_cmd_out",
    "断点": "_cmd_break",
    "取消断点": "_cmd_unbreak",
    "清空断点": "_cmd_clear",
    "变量": "_cmd_vars",
    "查看": "_cmd_eval",
    "栈": "_cmd_stack",
    "源码": "_cmd_source",
    "帮助": "_cmd_help",
    "退出": "_cmd_quit",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _number_line(n: int, text: str, width: int, current: int) -> str:
    """带行号的一行源码：`→  9 │ 令 总额 = 单价 * 数量`（当前行用 → 标出）。"""
    mark = "→" if n == current else " "
    return f"{mark} {n:>{width}} │ {text}"


#: 「这一行为什么停不住」的说明（CLI 与调试器里共用一处措辞）
_DEAD_LINE_HINT = ("空行 / 注释 / 「否则」「捕获」「最终」这类起头的行——"
                   "它们是块的一部分，不是独立语句")


def _default_write(text: str) -> None:
    """默认输出：写一行到标准输出，并 flush。

    flush 是为了让「调试器的话」与「程序的输出」在管道里**顺序正确**——
    程序输出若还在缓冲区里，调试器的提示就会抢在它前面出现。
    """
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _default_read_line(prompt: str) -> str:
    """默认读入：交互终端走文本层，管道输入按「字节 → UTF-8」解码。

    管道为什么不能直接用文本层：Windows 的 `sys.stdin` 默认按本地代码页
    （中文机器是 GBK）解码，`jishi 调试 x.jsh < 命令.txt` 里的中文命令会变成
    乱码——和 M23 源码 stdin 是同一个坑。交互终端反而不该碰 `buffer`
    （控制台本身是 Unicode，文本层才有行编辑）。

    读到 EOF（管道用尽 / Ctrl-D）时返回「退出」：不然命令循环会空转，
    用户只看到一串「不认识这个命令」。

    管道输入还会**自己回显一遍命令**：终端会替用户回显，管道不会，于是记录里
    只剩提示符和命令的输出挤在同一行，读起来前言不搭后语（M37 实测）。
    """
    buf = getattr(sys.stdin, "buffer", None)
    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if buf is not None and not interactive:
        raw = buf.readline()
        if not raw:
            sys.stdout.write("（输入结束了）\n")
            sys.stdout.flush()
            return "退出"
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        return text
    line = sys.stdin.readline()
    if not line:
        return "退出"
    return line.rstrip("\r\n")
