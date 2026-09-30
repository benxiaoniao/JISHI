# -*- coding: utf-8 -*-
"""基石语言交互式 REPL。

M1 起的能力：顶层表达式回显结果、多行块输入、命令（退出/帮助/清空）。

M38 B3 补上三件「用起来顺手」的事（路线图 B3「历史、多行编辑、Tab 补全」）：

1. **历史**：真终端里用 ↑/↓ 翻（Windows 自己实现逐键编辑，POSIX 交给
   `readline`），并落盘到 `~/.jishi/repl_history.txt`，下次还在。
2. **Tab 补全**：关键字 / 内建 / 标准库模块 / 已定义的变量；`模块.` 之后给
   该模块的函数，`对象.` 之后给方法。数据取自 `ai.build_lang_spec()`
   （经 `jishi.langdata`），**不另抄一份**语言元数据。
3. **多行编辑更准**：除了「冒号结尾 / 括号未闭合 / 块未闭合」，还用**真词法器**
   判断「引号（含三引号）没写完」——手写一个引号扫描器迟早与词法器漂移。

行编辑那部分在 `jishi.lineedit`，按键序列可注入，所以「上键取历史、Tab 补全、
退格删汉字」这些都能在没有终端的环境里单测。
"""

from __future__ import annotations

import sys

from .cli import BANNER
from .errors import JishiError, LexStringError
from .interpreter import Interpreter, _NO_VALUE
from .lineedit import Completer, History, read_line
from .parser import parse
from .runtime import jishi_repr
from .tokenizer import tokenize

_EXIT_WORDS = {"退出", "quit", "exit", "q"}
_HELP_WORDS = {"帮助", "help", "h", "？"}
_CLEAR_WORDS = {"清空", "clear"}
_HISTORY_WORDS = {"历史", "history"}

HELP_TEXT = """可用命令：
  退出 / quit / q        离开基石
  帮助 / help / h        显示这份帮助
  清空 / clear           清空当前多行输入（用于放弃写了一半的代码块）
  历史 / history         看看最近用过哪些输入

编辑：
  Tab                    补全（关键字 / 内建 / 标准库 / 已定义的变量；`模块.` 给函数）
  ↑ / ↓                  翻历史（真终端里才可用；历史会存到 ~/.jishi/repl_history.txt）
  Ctrl-C                 放弃当前输入，回到干净的一行
  多行块                 冒号结尾、括号或引号没闭合时自动续行，空行结束并执行

内建函数：
  打印(...)  输入(...)  整数(...)  小数(...)  文本(...)  长度(...)
  范围(起, 止, 步长)  最大(列表)  最小(列表)  总和(列表)
  类型(...)  反转(...)

对象方法（举例）：
  列表：追加 / 插入 / 移除 / 弹出 / 排序 / 反转 / 清空 / 索引 / 计数 / 包含
  字典：获取 / 键 / 值 / 包含 / 更新 / 合并 / 弹出 / 清空
  文本：拆分 / 替换 / 查找 / 大写 / 小写 / 去空白 / 开头是 / 结尾是 / 包含 / 转整数 / 转小数

标准库：导入 随机（随机整数 / 随机小数 / 洗牌）
"""

#: 「历史」命令默认显示多少条
_HISTORY_SHOW = 20


def _repr_jishi(v) -> str:
    """把基石的值格式化为可读文本（REPL 回显用）。

    委托给 `runtime.jishi_repr(top=False)`——**不要在这里另抄一份**。
    以前这里抄了一份，结果与 `打印` 漂移：REPL 回显「空/真/假」，
    而 `打印` 给出 `None`/`True`/`False`（M27 统一）。
    回显用 top=False（文本带引号），与 Python REPL 的分工一致：
    `>>> "甲"` 回显 `'甲'`，而 `打印("甲")` 输出 `甲`。
    """
    return jishi_repr(v)


def _unclosed_parens(text: str) -> bool:
    """括号是否未闭合（忽略字符串内的括号）。"""
    depth = 0
    in_str = None
    for ch in text:
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in "\"'“”":
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth > 0


def _unclosed_string(text: str) -> bool:
    """引号（含三引号）是否没写完——**问真词法器**，不自己数引号。

    手写一个「引号扫描器」是最容易与词法器漂移的地方（中文引号、三引号、
    反引号插值、转义……），所以这里直接把缓冲交给 `tokenize`：它报
    「字符串没有正常结束」就说明该继续读，报别的错说明是**真错了**，
    不该继续吞行。
    """
    try:
        tokenize(text, "<交互>")
    except LexStringError:
        return True
    except JishiError:
        return False
    return False


def _block_depth(buffer: list[str]) -> int:
    """当前打开的缩进块层数（模拟缩进栈）。"""
    stack = [0]
    for line in buffer:
        s = line.strip()
        if not s:
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        while len(stack) > 1 and indent < stack[-1]:
            stack.pop()
        if s.endswith(("：", ":")):
            # 冒号行：块开始（无论缩进是否增长）
            if indent >= stack[-1]:
                stack.append(indent)
        elif indent > stack[-1]:
            stack.append(indent)
    return len(stack) - 1


def _needs_more(buffer: list[str]) -> bool:
    """判断是否需要继续读入下一行。"""
    if not buffer:
        return False
    last = buffer[-1]
    stripped = last.strip()
    if not stripped:
        return False
    # 冒号结尾 → 块还没写内容
    if stripped.endswith(("：", ":")):
        return True
    joined = "\n".join(buffer)
    # 括号未闭合
    if _unclosed_parens(joined):
        return True
    # 引号（含三引号）没写完
    if _unclosed_string(joined):
        return True
    # 处于未闭合的缩进块内（块体行有缩进）→ 允许继续输入块内语句
    if _block_depth(buffer) > 0:
        return True
    return False


def _exec_source(interp: Interpreter, source: str,
                 filename: str = "<交互>") -> bool:
    """执行一段输入；成功且顶层表达式有值时返回 True（用于回显）。"""
    interp._last_value = _NO_VALUE
    try:
        lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        program = parse(tokenize(source, filename), lines, filename)
    except JishiError as e:
        # M17.4：补源码行上下文，让 REPL 报错也能显示源码行 + ^ 指示
        e.with_source(source.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
        print(e.render(), file=sys.stderr)
        return False
    try:
        interp.run(program)
    except JishiError as e:
        e.with_source(source.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
        print(e.render(), file=sys.stderr)
        return False
    except KeyboardInterrupt:
        print("\n（已中断）", file=sys.stderr)
        return False
    if interp._last_value is not _NO_VALUE:
        print(_repr_jishi(interp._last_value))
    return True


def repl() -> int:
    print(BANNER)
    interp = Interpreter()
    # 历史：启动时读回上次的（读不到就当空的），退出时写回
    history = History().load()
    # 补全：顶层名字来自语言规格 + 本次会话已经定义过的变量
    completer = Completer(extra_names=lambda: list(interp.globals.vars))
    buffer: list[str] = []

    try:
        while True:
            prompt = "基石> " if not buffer else "    ... "
            try:
                line = read_line(prompt, history=history, completer=completer)
            except EOFError:
                print()
                if buffer:  # EOF 时执行未闭合的多行输入（对齐 Python REPL 行为）
                    _exec_source(interp, "\n".join(buffer))
                    buffer = []
                break
            except KeyboardInterrupt:
                print("\n（已中断）")
                buffer = []
                continue

            stripped = line.strip()

            # 命令：退出只在顶层；清空随时可用（放弃写了一半的代码块）
            if stripped in _EXIT_WORDS and not buffer:
                break
            if stripped in _HELP_WORDS and not buffer:
                print(HELP_TEXT)
                continue
            if stripped in _HISTORY_WORDS and not buffer:
                _show_history(history)
                continue
            if stripped in _CLEAR_WORDS:
                if buffer:
                    print("（已放弃当前输入）")
                    buffer = []
                else:
                    print("（没有正在输入的内容）")
                continue

            # 空行：结束多行输入并执行
            if stripped == "":
                if buffer:
                    history.add("\n".join(buffer))   # 整块记一条，翻历史能取回来
                    _exec_source(interp, "\n".join(buffer))
                    buffer = []
                continue

            buffer.append(line)
            history.add(line)
            if not _needs_more(buffer):
                _exec_source(interp, "\n".join(buffer))
                buffer = []
    finally:
        history.save()
    return 0


def _show_history(history: History) -> None:
    entries = history.all_lines()
    if not entries:
        print("（还没有历史）")
        return
    tail = entries[-_HISTORY_SHOW:]
    start = len(entries) - len(tail) + 1
    for i, line in enumerate(tail, start=start):
        print(f"{i:>4}  {line}")
    if len(entries) > len(tail):
        print(f"（只显示最近 {len(tail)} 条，共 {len(entries)} 条）")
