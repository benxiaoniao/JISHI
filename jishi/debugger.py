# -*- coding: utf-8 -*-
"""基石调试器（M37 · B1 起点；M39 · B5 补齐字节码执行器）。

两个执行器，一套会话
--------------------
调试会话里真正需要「执行器知识」的只有五件事：**当前帧的深度、名字、变量、
调用链，以及怎么求一个表达式的值**。其余（断点表、命令循环、单步模式判定、
源码显示、出错现场渲染）与执行器无关。所以本模块的结构是：

- `_Session`：执行器无关的会话（命令、断点、输出、暂停判定）；
- `Debugger`：树遍历执行器（`interpreter.py`）—— 钩子是 `before_stmt`
  （每个语句执行前）与 `push_frame`/`pop_frame`；
- `VmDebugger`：字节码执行器（`vm.py`）—— 钩子是 `before_instr`
  （每条指令），靠编译器打的「**语句起始标记**」认出语句边界。

于是**同一个程序、同一组断点与单步命令，两个执行器的会话记录逐字相同**——
这不是巧合，是 `tests/test_m39_vm_debugger.py` 里逐项对拍钉住的性质。

字节码执行器怎么会有「语句边界」
--------------------------------
栈机只知道指令，但**编译器知道**：`compile_stmt` 会给每个语句的**第一条指令**
打一个标（`opcodes.Code.stmt_marks`），调试器只在标记处暂停。标「首指令」
而不是「语句范围内的每一行」是刻意的：循环的回边（`遍历`→FOR_ITER、
`当`→条件、`循环 n 次`→计数器判断）都落在语句中部，于是循环头只停一次，
与树遍历里 `A.For`/`A.While` 只被 `exec_stmt` 进入一次**完全一致**。

三条设计原则
------------
1. **调试器不改语义**：钩子只读（读标记、读帧），暂停时也不动任何状态。
   所以「带调试器跑」与「不带调试器跑」的输出必须逐字节相同——有测试钉住。
2. **中文命令**，与语言本身一致；同时给英文字母做别名（`c`/`s`/`n`/`b`/`p`…），
   让习惯 gdb 的人手感不掉。
3. **可测**：读命令与写输出都能注入，于是整场会话（含单步序列、出错现场）
   都能在单元测试里用一段命令脚本跑完，不需要真终端。

「退出」为什么用 `DebugQuit`（BaseException）
-------------------------------------------
用户在暂停时按「退出」是想**立刻结束**，而不是让程序里的 `尝试/捕获` 把它接住
——一句 `捕获：` 就能吞掉「退出」的话，用户会觉得调试器失灵。所以 `DebugQuit`
刻意不继承 `JishiError`，两个执行器都要显式放行它：

- 树遍历：`interpreter._do_try` 里那段是 `except BaseException`（为把 Python
  异常翻译成中文），控制信号元组里显式带上它；
- 字节码：VM 主循环的 `except BaseException` 会把它当用户异常去分发，所以
  `vm._dispatch_exception` 让它**跳过循环目标、只走处理器链**，`vm.THROW`
  也不许把它包装成用户异常。

两边合起来的效果一样：**`最终` 块照跑，`捕获` 接不住**——像正常控制流离开，
不是硬杀。

已知边界（不粉饰）
------------------
- **C VM 还不能调试**：它的帧与局部变量都在 C 侧，要先让宿主回调把帧传出来；
  理由与设计草图见 `docs/调试器.md`。`jishi 调试 --执行器 cvm` 会明确拒绝，
  不静默降级。
- **空块 `:` 那一行在字节码执行器上停不住**：它不发射任何指令，标不上「语句
  起始」。CLI 会明确提示「这一行没有可停的语句」（树遍历则停得住）。
- **块关键字行不能设断点**（`否则：` / `捕获：` / `最终：`）：它们是块的一部分，
  不是独立语句（`否则` 那一行连 AST 节点都没有）。
- **从 Python 侧回调进来的基石函数不会在其中暂停**（如 `列表.映射` 的回调），
  与 M17 错误调用栈同一限制。
"""

from __future__ import annotations

import dataclasses
import sys
from typing import Any, Callable, Iterable, Optional

from . import ast_nodes as A
from .errors import DebugQuit, JishiError, RunError
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


def mark_lines(cmod) -> set[int]:
    """字节码里所有「语句起始标记」的行号（= VM 调试器可停的行）。

    与 `statement_lines()` 是同一件事的两种说法：前者问 AST，后者问字节码。
    两者应当相等，`tests/test_m39_vm_debugger.py` 会拿真实程序对拍；
    差一处就是「某个断点在两个执行器里表现不同」，必须当场发现。
    """
    return {ins.stmt_line for code in cmod.codes for ins in code.instrs
            if ins.stmt_line}


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
# 会话（执行器无关）
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class DebugFrame:
    """树遍历调试器的调用帧：函数名 + **那一帧的环境**（用于看变量）+ 调用点。

    与 `errors.Frame` 的区别：那个只记「函数名 + 调用点」（渲染错误回溯够用），
    这里必须留着 `env`，否则「在函数里暂停时看局部变量」就无从谈起。
    字节码执行器不需要它——那里的 `_Frame` 自己就带着局部槽与单元。
    """

    name: str
    env: Any
    call_line: int
    call_col: int = 1


@dataclasses.dataclass
class _Snapshot:
    """出错现场的快照（M37）。

    为什么要专门存一份：**错误是在语句执行到一半时冒出来的**，等它传到
    `run()` 时，调用帧已经被弹干净了——那时再去看「当前帧」，看到的会是
    模块顶层、变量也可能是上一帧的（真实踩到过：错误出在函数「主」里，
    快照却写着「模块顶层」）。所以每执行一条语句就把「此刻在哪、哪个帧、
    有哪些变量、栈长什么样」**复制**一份留底。

    `target` 是执行器自己的「帧句柄」：树遍历是 `Environment`，字节码是
    `vm._Frame`——它由 `_vars_of` 解释，会话本身不需要认识。

    `vars` 是**当场抄下来的变量表**（M40 补）：C 虚拟机的帧活在 C 侧、按
    「第几层」编号，一旦弹帧那个编号就指向别的帧（或越界），报错时再去问只会
    得到空表。所以三个执行器统一在「执行语句之前」把变量复制一份——顺带也更
    准确：报错那一刻语句可能只执行了一半，读活环境会读到半成品。
    """

    line: int
    label: str
    target: Any
    chain: list[tuple[int, str]]
    vars: dict


class _Session:
    """调试会话中与执行器无关的部分。

    子类需要提供五件「执行器知识」（都是只读的）：
    `_depth()` / `_frame_label()` / `_frame_target()` / `_vars_of()` /
    `_live_chain()` / `_eval_expr()`，外加 `_start()`（怎么把程序跑起来）。
    """

    #: 给用户看的执行器名字（CLI 提示用；会话输出里不出现，两种执行器才逐字相同）
    executor_name = "未知"

    def __init__(self, *, source_lines: Iterable[str], filename: str = "<输入>",
                 breakpoints: Iterable[Any] = (), stop_on_entry: bool = True,
                 read_line: Optional[Callable[[str], str]] = None,
                 write: Optional[Callable[[str], None]] = None,
                 script: Optional[Iterable[str]] = None,
                 stmt_lines: Iterable[int] = (),
                 interactive: bool = True):
        self.filename = filename
        self.lines = list(source_lines)
        self.breakpoints: set[int] = set()
        self.stop_on_entry = stop_on_entry
        self.quit_requested = False
        #: 前端是不是「敲命令的终端」。false 时省掉命令行专属的寒暄
        #: （「输入「帮助」看命令」）——编辑器里没有命令输入这回事，那是纯噪音。
        self.interactive = interactive
        #: 「运行中请求暂停」（DAP 的 pause 请求用；命令行会话用不到）。
        #: 在下一个语句边界生效——调试器本来就是按语句停的。
        self.pause_requested = False
        #: 程序里「可停的语句行」，设断点时用来提示「这行停不住」
        self.stmt_lines = set(stmt_lines)
        #: 语言自带的全局名字（内建函数 + 异常类型 + `空`）。「看变量」默认
        #: 把它们藏起来——模块顶层本来会一次冒出三十多个，把用户的变量淹掉。
        #: 按**名字**判断而不是按值的类型判断：`值错误` 这类异常类型是 Python
        #: 类，而用户写的 `类` 也可能落成 Python 类，按类型判会误伤。
        self._builtin_names = set(new_builtins())

        self._write_line = write or _default_write
        self._read_line = read_line or _default_read_line
        #: 命令脚本（非 None 时按顺序自动执行；用完自动「继续」）
        self._script: Optional[list[str]] = list(script) if script is not None else None

        #: 暂停时的回调（DAP 用，M43）。命令行会话不用它。
        self.on_pause: Optional[Callable[[str], None]] = None
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

    # -- 子类要实现的「执行器知识」------------------------------------------

    def _start(self) -> None:
        """把程序跑起来（返回即正常结束）。"""
        raise NotImplementedError

    def _depth(self) -> int:
        """当前**用户函数**的层数（模块顶层 = 0）。

        三种单步全靠它区分：`下一步` = 深度不增加，`跳出` = 深度变小。
        """
        raise NotImplementedError

    def _frame_label(self) -> str:
        raise NotImplementedError

    def _frame_target(self) -> Any:
        """当前帧的句柄（交给 `_vars_of` 解释），没有则 None。"""
        raise NotImplementedError

    def _vars_of(self, target: Any) -> dict:
        """帧句柄 → 变量表（名字 → 值）。"""
        raise NotImplementedError

    def _live_chain(self) -> list[tuple[int, str]]:
        """调用栈：由内到外的 (行号, 在哪)。"""
        raise NotImplementedError

    def _eval_expr(self, text: str) -> Any:
        """在当前帧里求一个表达式的值。"""
        raise NotImplementedError

    def _can_eval(self) -> bool:
        return self._frame_target() is not None

    def _frame_target_at(self, index: int) -> Any:
        """按调用栈下标取帧句柄（0 = 当前帧，与 `_live_chain()` 同一顺序）。

        DAP（M43）让用户在编辑器里点调用栈的**任意一帧**看变量，所以需要
        「按层取帧」。默认只认当前帧——将来新增执行器不实现它也能用，
        代价只是别的层变量显示为空。
        """
        return self._frame_target() if index == 0 else None

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

    # -- 会话 ---------------------------------------------------------------

    def run(self) -> Optional[JishiError]:
        """跑程序；返回 ``None`` 表示正常跑完或用户主动退出。"""
        try:
            self._start()
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
        self._dump_vars("", snap.target, snap.label, frozen=snap.vars)
        self._dump_stack(snap.chain)
        self._write_line("（想复盘就改脚本、带上同一组断点再跑一次）")

    # -- 暂停与命令 ---------------------------------------------------------

    def _snapshot_vars(self) -> dict:
        """此刻的变量表（**复制**一份，供出错现场用）。取不到就返回空表。"""
        try:
            target = self._frame_target()
            return dict(self._vars_of(target)) if target is not None else {}
        except Exception:                    # noqa: BLE001 - 取不到就不显示
            return {}

    def _before_statement(self, line: int, target: Any) -> None:
        """执行器每进入一个语句就调这里（两种执行器共用的暂停判定）。"""
        self._line = line
        # 顺手留一份现场：错误可能在**这条语句执行到一半**时冒出来，那时帧
        # 已经被弹掉了（见 _Snapshot 的说明）。变量也**当场复制**——C VM 的
        # 帧句柄在弹帧后就失效了，事后再问只会得到空表。
        self._snapshot = _Snapshot(line, self._frame_label(),
                                   self._frame_target(),
                                   self._live_chain(),
                                   self._snapshot_vars())
        depth = self._depth()
        mode = self._mode
        if self.pause_requested:
            # 「暂停」请求优先：用户刚点的按钮，就地停下最符合预期
            self.pause_requested = False
            self._pause("暂停")
        elif mode == "entry":
            self._pause("停在入口")
        elif mode == "step":
            self._pause("单步")
        elif mode == "next" and depth <= self._paused_depth:
            self._pause("下一步")
        elif mode == "out" and depth < self._paused_depth:
            self._pause("跳出")
        elif mode == "run" and line in self.breakpoints:
            self._pause(f"命中第 {line} 行的断点")

    def _pause(self, reason: str) -> None:
        self.stops += 1
        self._paused_depth = self._depth()
        if self.on_pause is not None:
            # DAP（M43）：先把「停了」告诉编辑器 —— 它一收到就会来问栈与变量，
            # 而那必须在**状态静止**时读。此刻脚本线程正要往下走去读命令，
            # 不会改执行状态（`_pause` 里只有打印和读命令），所以是安全的。
            self.on_pause(reason)
        self._write_line("")
        self._write_line(f"暂停（{reason}） 第 {self._line} 行 · {self._frame_label()}")
        self._write_line(self._source_line(self._line))
        if self.interactive and not self._help_shown:
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
        if self._depth() == 0:
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
        if not self._can_eval():
            self._write_line("（还没有执行到任何语句，没法求值）")
            return None
        value = self._eval_expr(text)
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

    def _dump_vars(self, arg: str, target: Any = None,
                   label: Optional[str] = None,
                   frozen: Optional[dict] = None) -> None:
        """打印某一帧的变量。`target` / `label` 不传时用**当前暂停处**的。

        `frozen` 是「出错现场」抄下来的那份变量（见 `_Snapshot.vars`）：
        传了就用它，不再去问执行器（那时帧可能已经没了）。
        """
        show_all = arg.strip() in ("全部", "all", "-a")
        if target is None and label is None:
            target, label = self._frame_target(), self._frame_label()
        self._write_line(f"当前帧（{label}）的变量：")
        if target is None and frozen is None:
            self._write_line("  （还没有执行到任何语句）")
            return
        vars_map = frozen if frozen is not None else self._vars_of(target)
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
            # 不报具体条数：各执行器「看得见的全局」本来就不同——C 侧的名字表
            # 只收程序用到的名字，树遍历/Python VM 那边则把整套内建都放进来。
            # 报数字会让三个执行器的记录对不上，而这数字对用户没有意义（M40）。
            self._write_line("  （另有内建/临时变量没显示，要看用「变量 全部」）")

    def _dump_stack(self, chain: Optional[list[tuple[int, str]]] = None) -> None:
        self._write_line("调用栈（由内到外）：")
        for i, (line, label) in enumerate(chain or self._live_chain()):
            self._write_line(f"  #{i} 第 {line:>4} 行  {label}")


# ---------------------------------------------------------------------------
# 树遍历执行器
# ---------------------------------------------------------------------------

class Debugger(_Session):
    """挂在 `Interpreter` 上的调试器（`interp.debugger = Debugger(...)`）。

    钩子只有两个：`before_stmt`（每个语句执行前）与 `push_frame`/`pop_frame`
    （进出函数）。语义零改动——它们只读不写。
    """

    executor_name = "树遍历"

    def __init__(self, interp, program: A.Program, *,
                 source_lines: Iterable[str],
                 filename: str = "<输入>",
                 breakpoints: Iterable[Any] = (),
                 stop_on_entry: bool = True,
                 read_line: Optional[Callable[[str], str]] = None,
                 write: Optional[Callable[[str], None]] = None,
                 script: Optional[Iterable[str]] = None,
                 interactive: bool = True):
        super().__init__(source_lines=source_lines, filename=filename,
                         breakpoints=breakpoints, stop_on_entry=stop_on_entry,
                         read_line=read_line, write=write, script=script,
                         stmt_lines=statement_lines(program),
                         interactive=interactive)
        self.interp = interp
        self.program = program
        self._frames: list[DebugFrame] = []
        self._env: Optional[Any] = None
        interp.debugger = self

    # -- 执行器调用的钩子 ---------------------------------------------------

    def before_stmt(self, stmt: A.Node, env: Any) -> None:
        """每个语句执行前调用（见 `interpreter.exec_stmt` 开头）。"""
        self._env = env
        self._before_statement(getattr(stmt, "line", None) or self._line, env)

    def push_frame(self, name: str, env: Any, call_line: int,
                   call_col: int = 1) -> None:
        self._frames.append(DebugFrame(name, env, call_line, call_col))

    def pop_frame(self) -> None:
        if self._frames:
            self._frames.pop()

    # -- 执行器知识 ---------------------------------------------------------

    def _start(self) -> None:
        self.interp.run(self.program)

    def _depth(self) -> int:
        return len(self._frames)

    def _frame_label(self) -> str:
        if self._frames:
            return f"函数「{self._frames[-1].name}」"
        return "模块顶层"

    def _frame_target(self) -> Any:
        return self._env

    def _frame_target_at(self, index: int) -> Any:
        # `self._frames` 是由外到内存的，而栈表是由内到外，所以要倒着数；
        # 最后一层（下标 = 帧数）是模块顶层，句柄就是解释器的全局环境。
        if index < len(self._frames):
            return self._frames[-1 - index].env
        if index == len(self._frames):
            return self.interp.globals
        return None

    def _vars_of(self, target: Any) -> dict:
        # 「赋值即局部」给每个会被赋值的名字先占了一个未绑定哨兵（M40）：
        # 它代表「这个名字是本函数的局部变量，但还没轮到赋值」，不是值。
        # 与字节码执行器把 `_UNSET` 槽位跳过是同一口径——不然调试器会显示
        # `果 = <未绑定 果>`，用户会以为变量里真存了个东西。
        from .interpreter import _Unbound
        return {k: v for k, v in (getattr(target, "vars", {}) or {}).items()
                if not isinstance(v, _Unbound)}

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

    def _eval_expr(self, text: str) -> Any:
        from .parser import parse_expression
        node = parse_expression(text, "<调试>")
        return self.interp.eval_expr(node, self._env)


# ---------------------------------------------------------------------------
# 字节码执行器（M39 · B5）
# ---------------------------------------------------------------------------

class VmDebugger(_Session):
    """挂在 `VM` 上的调试器（`vm.debugger = VmDebugger(...)`）。

    钩子只有一个：`before_instr`（每条指令），靠编译器在**语句首指令**上打的
    标记认出语句边界。帧从 `vm.frames` 现取（不另存一份），所以「退出」这类
    把帧一路弹空的路径也不会让调试器的视图与执行器失步。
    """

    executor_name = "字节码（Python VM）"

    def __init__(self, vm, *,
                 source_lines: Iterable[str],
                 filename: str = "<输入>",
                 breakpoints: Iterable[Any] = (),
                 stop_on_entry: bool = True,
                 read_line: Optional[Callable[[str], str]] = None,
                 write: Optional[Callable[[str], None]] = None,
                 script: Optional[Iterable[str]] = None,
                 stmt_lines: Optional[Iterable[int]] = None,
                 interactive: bool = True):
        super().__init__(source_lines=source_lines, filename=filename,
                         breakpoints=breakpoints, stop_on_entry=stop_on_entry,
                         read_line=read_line, write=write, script=script,
                         # 「可停的行」直接取字节码里打的语句标记——比再解析一遍
                         # AST 更准：它就是执行器真正停得下来的那些行
                         stmt_lines=(mark_lines(vm.cmod) if stmt_lines is None
                                     else stmt_lines),
                         interactive=interactive)
        self.vm = vm
        #: 「查看 表达式」求值期间不许再触发暂停（否则会在一条命令中途停下）
        self._evaluating = False
        vm.debugger = self

    # -- 执行器调用的钩子 ---------------------------------------------------

    def before_statement(self, line: int, frame) -> None:
        """每进入一个语句前调用（见 `vm._execute` 主循环）。

        VM 侧已经把「这条指令是不是语句起始」查好了（`Code.stmt_marks`），
        这里只管暂停判定——于是它和树遍历的 `before_stmt` 是同一件事的两种
        触发方式，会话行为因此逐字一致。
        """
        if self._evaluating:
            # 「查看 表达式」求值中：不许在一条命令中途再暂停
            return
        self._before_statement(line, frame)

    # -- 执行器知识 ---------------------------------------------------------

    def _start(self) -> None:
        self.vm.run()

    def _frames(self) -> list:
        """**用户函数**帧（不含模块主帧），与树遍历的 `_frames` 对齐。"""
        frames = self.vm.frames
        return frames[1:] if len(frames) > 1 else []

    def _depth(self) -> int:
        return len(self._frames())

    def _frame_label(self) -> str:
        frames = self._frames()
        if frames:
            return f"函数「{frames[-1].code.name}」"
        return "模块顶层"

    def _frame_target(self) -> Any:
        return self.vm.frames[-1] if self.vm.frames else None

    def _frame_target_at(self, index: int) -> Any:
        # `vm.frames` 由外到内（[0] 是模块主帧），倒着数即可
        frames = self.vm.frames
        return frames[-1 - index] if index < len(frames) else None

    def _vars_of(self, target: Any) -> dict:
        from .vm import _UNSET

        if target is None:
            return {}
        if target.code is self.vm.codes[self.vm.cmod.main]:
            # 模块顶层：普通变量都在全局表里（与树遍历的模块环境一致）
            return dict(self.vm.globals)
        out: dict[str, Any] = {}
        code = target.code
        for i, name in enumerate(code.local_names):
            if name.startswith("$tmp"):     # 编译器内部的临时槽（循环计数器…）
                continue
            if i >= len(target.locals):
                break
            value = target.locals[i]
            if value is not _UNSET:         # 还没赋过值的槽位不显示（与树遍历一致）
                out[name] = value
        # 本函数**自己的**单元变量（被内层函数捕获的局部变量）照常显示；
        # 但**自由变量**（从外层捕获进来的）不显示——树遍历那边的函数环境里
        # 只有本函数自己绑定的名字，列出来两边就不一致了。
        # 想看它们用「查看 基数」：`LOAD_DEREF` 读得到，只是不进变量表。
        for i in range(len(code.cellvars)):
            if i >= len(target.cells):
                break
            cell = target.cells[i]
            if cell.value is not _UNSET:
                out[self.vm._cell_name(code, i)] = cell.value
        return out

    def _live_chain(self) -> list[tuple[int, str]]:
        frames = self._frames()
        if not frames:
            return [(self._line, "模块顶层")]
        chain = [(self._line, f"函数「{frames[-1].code.name}」")]
        for j in range(len(frames) - 1, 0, -1):
            chain.append((frames[j].call_line,
                          f"函数「{frames[j - 1].code.name}」"))
        chain.append((frames[0].call_line, "模块顶层"))
        return chain

    def _eval_expr(self, text: str) -> Any:
        from .compiler import compile_debug_expr

        frame = self.vm.frames[-1]
        code = compile_debug_expr(self.vm.cmod, frame.code, text)
        self._evaluating = True
        try:
            return self.vm.eval_in_frame(code, frame)
        finally:
            self._evaluating = False


class CvmDebugger(_Session):
    """C 虚拟机上的调试会话（M40）。

    与另外两个执行器的差别只在**「执行器知识」怎么问**：帧、局部变量、调用点
    全都在 C 侧，所以每一步都要过一次宿主回调。命令循环、断点、暂停判定、
    出错现场全部复用 `_Session`——这正是把基类抽出来的目的。

    C 侧为它加了三个入口（`cvm/include/jsvm.h`）：
    `jsvm_set_stmt_marks`（语句起始表）、`jsvm_set_stmt_hook`（语句回调）、
    `jsvm_frame_meta` / `jsvm_dump_locals`（问帧与变量）。
    都是**只读**的：不挂调试器时 C 侧只多一次指针判空。

    **已知边界**：`查看 表达式`（就地求值）暂不支持——C 侧没有「在任意帧上求
    一个表达式」的入口，要做得把编译好的求值代码塞进当前帧执行，属另一件事。
    会话会明确说「C 虚拟机暂不支持就地求值」，而不是静默给个错值。
    """

    executor_name = "C 虚拟机"

    def __init__(self, runner, *,
                 source_lines: Iterable[str],
                 filename: str = "<输入>",
                 breakpoints: Iterable[Any] = (),
                 stop_on_entry: bool = True,
                 read_line: Optional[Callable[[str], str]] = None,
                 write: Optional[Callable[[str], None]] = None,
                 script: Optional[Iterable[str]] = None,
                 stmt_lines: Optional[Iterable[int]] = None,
                 interactive: bool = True):
        cmod = runner.cmod
        super().__init__(source_lines=source_lines, filename=filename,
                         breakpoints=breakpoints, stop_on_entry=stop_on_entry,
                         read_line=read_line, write=write, script=script,
                         stmt_lines=(mark_lines(cmod) if stmt_lines is None
                                     else stmt_lines),
                         interactive=interactive)
        self.runner = runner
        self.cmod = cmod
        #: 暂停时 C 侧回调填进来：当前行、代码下标、帧数（含模块主帧）
        self._hook_line = 0
        self._hook_code = -1
        self._hook_nframes = 0
        self._evaluating = False
        self._aborted_by_quit = False
        self._hook = None            # 保活：C 侧持有函数指针
        runner.host.set_stmt_hook(self._on_stmt)

    # -- C 侧回调 -----------------------------------------------------------

    def _on_stmt(self, code_idx: int, line: int, nframes: int) -> int:
        """C 侧每条语句首指令执行前调这里（见 `jsvm.c` 主循环）。

        返回 1 = 请求中止（「退出」）；C 侧会把它当「卸载类信号」走一遍异常
        分发，于是 `最终` 块照常执行、`捕获` 接不住它。
        """
        if self._evaluating:
            return 0
        self._hook_line = line
        self._hook_code = code_idx
        self._hook_nframes = nframes
        if self._aborted_by_quit:
            # **已经在退出流程里了**（此刻正在跑 `最终` 块）：不能再请求一次
            # 中止——否则 finally 的第一条语句就会被打断，用户看到的「清理」
            # 一行都不会执行（实测踩到）。让它跑完，信号会在 END_FINALLY 后
            # 由 C 侧继续向外传播。
            return 0
        try:
            self._before_statement(line, None)
        except DebugQuit:
            self._aborted_by_quit = True
            self.quit_requested = True
            self.runner.host.pending = DebugQuit()
            return 1
        return 0

    # -- 执行器知识 ---------------------------------------------------------

    def _start(self) -> None:
        try:
            self.runner.run()
        except BaseException:
            # 「退出」时 C 侧会把 `DebugQuit` 作为未接住的异常交回宿主，
            # `runner.run()` 原样抛出（它是 BaseException，不是 JishiError）。
            # 认出来交给 `_Session.run()` 的 DebugQuit 分支统一收尾——
            # 会话结束语与另两个执行器一字不差。
            if self._aborted_by_quit:
                raise DebugQuit() from None
            raise

    def _depth(self) -> int:
        """用户函数层数 = 帧数 - 1（模块主帧不算）。"""
        return max(0, self._hook_nframes - 1)

    def _frame_label(self) -> str:
        if self._hook_nframes <= 1:
            return "模块顶层"
        return f"函数「{self._code_name(0)}」"

    def _frame_target(self) -> Any:
        # C 侧没有「帧对象」可拿，用 (深度, 代码下标) 当句柄
        return (0, self._hook_code) if self._hook_code >= 0 else None

    def _frame_target_at(self, index: int) -> Any:
        # 向 C 侧问第 index 层（0 = 最内层）的代码下标，凑成同一种句柄
        meta = self._frame_meta(index)
        return (index, meta[0]) if meta is not None else None

    def _code_name(self, depth: int) -> str:
        meta = self._frame_meta(depth)
        if meta is None:
            return "?"
        return self.cmod.codes[meta[0]].name

    def _frame_meta(self, depth: int) -> Optional[tuple[int, int, int]]:
        """问 C 侧要一帧的 (代码下标, 调用点行, 调用点列)。"""
        from ctypes import c_int32

        out = (c_int32 * 3)()
        if self.runner.host.lib.jsvm_frame_meta(
                self.runner.host.vm, depth, out) != 0:
            return None
        return (out[0], out[1], out[2])

    def _vars_of(self, target: Any) -> dict:
        if target is None:
            return {}
        depth, code_idx = target
        code = self.cmod.codes[code_idx]
        out: dict[str, Any] = {}
        if code_idx == self.cmod.main:
            # 模块顶层：变量都在 C 侧的全局表里（与另外两个执行器一致）。
            # 注意**不能**用宿主侧的 `globals` 影子表——那里只有内建，
            # 用户定义的全局变量在 C 侧，只有 `jsvm_dump_globals` 问得到。
            try:
                self.runner.host.dump_globals(out)
            except Exception:                 # noqa: BLE001
                return out
            return out
        names = list(code.local_names)
        cells = code.cellvars + code.freevars
        try:
            self.runner.host.dump_locals(depth, names, code, out)
        except Exception:                     # noqa: BLE001 - 看变量失败不该中断会话
            return out
        # 自由变量（从外层捕获进来的）不显示，与另外两个执行器口径一致
        for i in range(len(code.cellvars), len(cells)):
            out.pop(cells[i], None)
        return out

    def _live_chain(self) -> list[tuple[int, str]]:
        """调用栈（由内到外）：与另外两个执行器**同一口径**。

        `jsvm_frame_meta(depth)` 的 depth 从最内层数起（0 = 当前帧），而一帧的
        `call_line` 是「**它**被调进来时，调用方写在第几行」。所以链条上第 d 层
        该配的行号是 **`meta(d-1).call_line`**（= 更内层那一帧的调用点），
        不是 `meta(d).call_line`——后者会错位一层（实测：`主` 被标成第 9 行，
        实际第 9 行是「主被调用」的位置，而「倍被调用」才是第 6 行）。

        与 `VmDebugger._live_chain` / `Debugger._live_chain` 逐字一致。
        """
        n = max(1, self._hook_nframes)          # 含模块主帧
        chain = [(self._hook_line, self._frame_label())]
        for d in range(1, n):
            meta = self._frame_meta(d - 1)
            call_line = meta[1] if meta else self._hook_line
            if d == n - 1:
                chain.append((call_line, "模块顶层"))
            else:
                chain.append((call_line, f"函数「{self._code_name(d)}」"))
        return chain

    def _can_eval(self) -> bool:
        """C 侧没有「在任意帧上求一个表达式」的入口，所以就地求值不支持。

        返回 True 让 `_cmd_eval` 走到 `_eval_expr`，由它给出**具体原因**
        （「暂不支持就地求值」）——比笼统的「还没有执行到任何语句，没法求值」
        有用得多：后者会让人以为是暂停位置的问题。
        """
        return self._frame_target() is not None

    def _eval_expr(self, text: str) -> Any:
        # 用 RunError（有具体错误码/标题），而不是抽象基类 JishiError
        raise RunError(
            "C 虚拟机暂不支持就地求值",
            hint="「查看 表达式」只在树遍历 / 字节码执行器上可用；"
                 "C 虚拟机要看值请用「变量」，或改用 --执行器 vm")


#: 命令表：规范名 → 会话上的方法名
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
