# -*- coding: utf-8 -*-
"""基石语言交互式 REPL（M1 增强版）。

- 顶层表达式回显结果（打印出的中文值用真/假/空表示）
- 多行块输入：冒号结尾继续读，块内缩进行继续读，空行结束块
- 命令：退出 / 帮助 / 清空
"""

from __future__ import annotations

import sys

from .cli import BANNER
from .errors import JishiError
from .interpreter import Interpreter
from .parser import parse
from .tokenizer import tokenize

_EXIT_WORDS = {"退出", "quit", "exit", "q"}
_HELP_WORDS = {"帮助", "help", "h", "？"}
_CLEAR_WORDS = {"清空", "clear"}

HELP_TEXT = """可用命令：
  退出 / quit / q        离开基石
  帮助 / help / h        显示这份帮助
  清空 / clear           清空当前多行输入（用于放弃写了一半的代码块）

内建函数：
  打印(...)  输入(...)  整数(...)  小数(...)  文本(...)  长度(...)
  范围(起, 止, 步长)  最大(列表)  最小(列表)  总和(列表)
  类型(...)  反转(...)

对象方法（举例）：
  列表：追加 / 插入 / 移除 / 弹出 / 排序 / 反转 / 清空 / 索引 / 计数 / 包含
  字典：获取 / 键 / 值 / 包含 / 更新 / 弹出 / 清空
  文本：拆分 / 替换 / 查找 / 大写 / 小写 / 去空白 / 开头是 / 结尾是 / 包含 / 转整数 / 转小数

标准库：导入 随机（随机整数 / 随机小数 / 洗牌）
"""


def _repr_jishi(v) -> str:
    """把基石的值格式化为可读文本（真/假/空 中文化）。"""
    if v is None:
        return "空"
    if v is True:
        return "真"
    if v is False:
        return "假"
    if isinstance(v, str):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_repr_jishi(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(
            f"{_repr_jishi(k)}：{_repr_jishi(val)}" for k, val in v.items()) + "}"
    return str(v)


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
    # 括号未闭合
    if _unclosed_parens("\n".join(buffer)):
        return True
    # 处于未闭合的缩进块内（块体行有缩进）→ 允许继续输入块内语句
    if _block_depth(buffer) > 0:
        return True
    return False


def _exec_source(interp: Interpreter, source: str,
                 filename: str = "<交互>") -> bool:
    """执行一段输入；成功且顶层表达式有值时返回 True（用于回显）。"""
    interp._last_value = None
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
    if interp._last_value is not None:
        print(_repr_jishi(interp._last_value))
    return True


def repl() -> int:
    print(BANNER)
    interp = Interpreter()
    buffer: list[str] = []

    while True:
        prompt = "基石> " if not buffer else "    ... "
        try:
            line = input(prompt)
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
                _exec_source(interp, "\n".join(buffer))
                buffer = []
            continue

        buffer.append(line)
        if not _needs_more(buffer):
            _exec_source(interp, "\n".join(buffer))
            buffer = []

    return 0
