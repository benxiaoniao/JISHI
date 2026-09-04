# -*- coding: utf-8 -*-
"""基石语言命令行入口。

用法：
    jishi 文件.jsh            运行脚本
    jishi 文件.jsh --ast      只显示语法树
    jishi -i                  交互式 REPL
    jishi                     进入 REPL
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from .errors import JishiError
from .interpreter import Interpreter, run_source
from .parser import parse
from .tokenizer import tokenize

VERSION = "0.1.0"

BANNER = f"""基石 jishi {VERSION} —— 一门像 Python 一样简单易用的中文编程语言
输入「退出」离开；冒号结尾的行会继续输入（多行代码块）。
"""


def _read_source(path: str) -> tuple[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), path
    except FileNotFoundError:
        print(f"找不到文件「{path}」", file=sys.stderr)
        sys.exit(1)
    except UnicodeDecodeError:
        print(f"文件「{path}」不是 UTF-8 编码，无法读取", file=sys.stderr)
        sys.exit(1)


def dump_ast(program, indent: int = 0) -> str:
    """把 AST 转成缩进文本（调试用）。"""
    pad = "  " * indent
    if isinstance(program, (list, tuple)):
        return "\n".join(dump_ast(x, indent) for x in program)
    if not hasattr(program, "line"):
        return pad + repr(program)
    name = type(program).__name__
    fields = []
    for k, v in vars(program).items():
        if k in ("line", "col"):
            continue
        if isinstance(v, (list, tuple)):
            items = "\n".join(dump_ast(x, indent + 1) for x in v)
            fields.append(f"{k}:\n{items}" if items else f"{k}: {type(v).__name__}()")
        elif hasattr(v, "line"):
            fields.append(f"{k}: {dump_ast(v, indent + 1).strip()}")
        else:
            fields.append(f"{k}: {v!r}")
    inner = "\n".join("  " + f for f in fields)
    body = f"{pad}{name}("
    if inner:
        body += "\n" + inner + "\n" + pad
    body += ")"
    return body


def run_file(path: str, show_ast: bool = False) -> int:
    source, filename = _read_source(path)
    try:
        tokens = tokenize(source, filename)
        lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        program = parse(tokens, lines, filename)
    except JishiError as e:
        print(e.render(), file=sys.stderr)
        return 1

    if show_ast:
        print(dump_ast(program))
        return 0

    try:
        Interpreter(filename=filename).run(program)
    except JishiError as e:
        e.with_source(lines)
        print(e.render(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n程序被中断", file=sys.stderr)
        return 130
    except Exception as e:  # 兜底：绝不让英文 traceback 直接冒出来
        print(f"错误：程序内部出现未预料的问题（{type(e).__name__}）：{e}",
              file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jishi", description="基石 —— 中文编程语言解释器")
    parser.add_argument("file", nargs="?", help="要运行的 .jsh 脚本文件")
    parser.add_argument("-i", "--repl", action="store_true",
                        help="进入交互式 REPL")
    parser.add_argument("--ast", action="store_true",
                        help="只显示语法树，不运行")
    parser.add_argument("-v", "--version", action="version",
                        version=f"基石 jishi {VERSION}")
    args = parser.parse_args(argv)

    if args.file:
        return run_file(args.file, show_ast=args.ast)

    from .repl import repl
    return repl()


if __name__ == "__main__":
    sys.exit(main())
