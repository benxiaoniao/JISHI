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
import json
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


def _dump_bytecode(path: Optional[str], stdin: bool = False) -> int:
    """把源码编译成跨语言字节码 JSON 并输出（M13.1）。"""
    from .compiler import compile_source
    from .serialize import dumps

    if stdin:
        source = sys.stdin.read()
        filename = "<stdin>"
    else:
        source, filename = _read_source(path)
    cmod = compile_source(source, filename)
    print(dumps(cmod))
    return 0


def _run_bytecode(path: Optional[str], stdin: bool = False) -> int:
    """把字节码 JSON 反序列化后执行（M13.1，供跨语言宿主/调试用）。"""
    from .serialize import loads
    from .vm import VM

    if stdin:
        text = sys.stdin.read()
        filename = "<stdin>"
    else:
        text, filename = _read_source(path)
    try:
        cmod = loads(text)
    except ValueError as e:
        print(f"字节码加载失败：{e}", file=sys.stderr)
        return 1
    try:
        VM(cmod, filename).run()
    except Exception as e:  # 兜底：绝不让英文 traceback 直接冒出
        print(f"错误：程序内部出现未预料的问题（{type(e).__name__}）：{e}",
              file=sys.stderr)
        return 1
    return 0


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


def run_file(path: str, show_ast: bool = False,
             json_errors: bool = False, sandbox: bool = False,
             json_result: bool = False, timeout: float = 5.0,
             stdin: bool = False) -> int:
    if stdin:
        source = sys.stdin.read()
        filename = "<stdin>"
    else:
        source, filename = _read_source(path)

    # 沙箱 / 统一协议结果：走 run_sandboxed，输出 protocol JSON
    if sandbox or json_result:
        from .protocol import dumps
        from .sandbox import SandboxOpts, run_sandboxed
        opts = SandboxOpts(timeout=timeout)
        result = run_sandboxed(source, opts, filename)
        sys.stdout.write(dumps(result) + "\n")
        sys.stdout.flush()
        return 0 if result["ok"] else 1

    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    try:
        tokens = tokenize(source, filename)
        program = parse(tokens, lines, filename)
    except JishiError as e:
        if json_errors:
            _print_json_error(e)
        else:
            print(e.render(), file=sys.stderr)
        return 1

    if show_ast:
        print(dump_ast(program))
        return 0

    try:
        Interpreter(filename=filename).run(program)
    except JishiError as e:
        e.with_source(lines)
        if json_errors:
            _print_json_error(e)
        else:
            print(e.render(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n程序被中断", file=sys.stderr)
        return 130
    except Exception as e:  # 兜底：绝不让英文 traceback 直接冒出来
        if json_errors:
            print(json.dumps(
                {"ok": False,
                 "errors": [{"code": "E0000",
                             "title": "内部错误",
                             "message": f"{type(e).__name__}: {e}"}]},
                ensure_ascii=False), file=sys.stderr)
        else:
            print(f"错误：程序内部出现未预料的问题（{type(e).__name__}）：{e}",
                  file=sys.stderr)
        return 1
    return 0


def _print_json_error(e: JishiError) -> None:
    """`--json-errors`：输出 agent 可读的 JSON 错误（M8.3）。"""
    from .ai import error_to_json
    print(json.dumps({"ok": False, "errors": [error_to_json(e)]},
                     ensure_ascii=False), file=sys.stderr)


def _cmd_install(args) -> int:
    """`jishi 安装 包名`：从本地目录 / zip / URL 安装包到 .jishi/packages/。"""
    from . import packages as PKG

    try:
        if args.local_dir:
            PKG.install_package_dir(args.local_dir, args.package, force=args.force)
            print(f"已安装包「{args.package}」（来自目录 {args.local_dir}）")
            return 0
        if args.local_zip:
            PKG.install_package_zip(args.local_zip, args.package, force=args.force)
            print(f"已安装包「{args.package}」（来自 {args.local_zip}）")
            return 0
        if args.from_index:
            # 从远端索引 JSON 取下载地址并拉包
            _install_from_index(args.package, args.from_index, force=args.force)
            return 0
        # 默认：本地包缓存目录直接按名字找，没有则报错提示
        import os
        from .runtime import _find_pkg_dir, _package_dirs
        pdir = _find_pkg_dir(args.package)
        if pdir:
            print(f"包「{args.package}」已在本地缓存：{pdir}")
            return 0
        print(f"找不到包「{args.package}」", file=sys.stderr)
        print("用法：jishi 安装 包名 --local-dir 目录  |  "
              "--local-zip 包.zip  |  --from-index 索引.json", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"安装失败：{e}", file=sys.stderr)
        return 1


def _install_from_index(name: str, index_url: str, *, force: bool = False) -> None:
    """从远端索引拉包：读索引 → 取下载地址 → 下载 zip → 解压安装。"""
    import os
    import tempfile
    import urllib.request

    from . import packages as PKG

    if os.path.isfile(index_url):
        with open(index_url, encoding="utf-8") as f:
            text = f.read()
    else:
        try:
            with urllib.request.urlopen(index_url, timeout=15) as resp:
                text = resp.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"读远端索引「{index_url}」失败：{e}") from e

    try:
        index = PKG.parse_index(text)
        meta = PKG.resolve_package(index, name)
    except ValueError as e:
        raise ValueError(str(e)) from e

    url = meta.get("下载地址")
    if not url:
        raise ValueError(f"索引里包「{name}」没有「下载地址」字段")
    # 下载地址含中文等非 ASCII 时做 URL 编码（urllib 不接受裸非 ASCII URL）
    import urllib.parse
    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        parts = urllib.parse.urlsplit(url)
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc,
             urllib.parse.quote(parts.path), parts.query, parts.fragment))
    tmp = os.path.join(tempfile.gettempdir(), f"jishi-{name}.zip")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, \
                open(tmp, "wb") as out:
            out.write(resp.read())
        PKG.install_package_zip(tmp, name, force=force)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"下载或安装包「{name}」失败：{e}") from e
    finally:
        if os.path.isfile(tmp):
            os.remove(tmp)
    print(f"已安装包「{name}」（版本 {meta.get('版本', '未知')}，来自索引）")


def _cmd_uninstall(args) -> int:
    from . import packages as PKG
    try:
        if PKG.uninstall_package(args.package):
            print(f"已卸载包「{args.package}」")
            return 0
        print(f"包「{args.package}」未安装", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"卸载失败：{e}", file=sys.stderr)
        return 1


def _cmd_list(args) -> int:
    from . import packages as PKG
    pkgs = PKG.list_packages()
    if not pkgs:
        print("（尚未安装任何本地包）")
        return 0
    for p in pkgs:
        desc = f" — {p['描述']}" if p["描述"] else ""
        print(f"{p['名字']} {p['版本']}{desc}")
    return 0


def _run_package_cmd(argv: list[str]) -> int:
    """包管理子命令分发：jishi 安装/卸载/列表（M14.2）。"""
    parser = argparse.ArgumentParser(
        prog="jishi", description="基石包管理")
    sub = parser.add_subparsers(dest="subcommand", metavar="命令", required=True)

    p_install = sub.add_parser("安装", help="安装本地包到 .jishi/packages/")
    p_install.add_argument("package", help="要安装的包名")
    p_install.add_argument("--local-dir", help="从本地目录安装（含 包.json）")
    p_install.add_argument("--local-zip", help="从本地 zip 安装（内含 包名/ 顶层目录）")
    p_install.add_argument("--from-index", help="从远端索引 JSON（本地文件或 URL）拉包")
    p_install.add_argument("--force", action="store_true", help="覆盖已安装的同名包")
    p_install.set_defaults(func=_cmd_install)

    p_uninstall = sub.add_parser("卸载", help="卸载本地包")
    p_uninstall.add_argument("package", help="要卸载的包名")
    p_uninstall.set_defaults(func=_cmd_uninstall)

    p_list = sub.add_parser("列表", help="列出已安装的本地包")
    p_list.set_defaults(func=_cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # 包管理子命令（安装/卸载/列表）走独立解析器，避免与「运行文件」位置参数冲突
    if argv and argv[0] in ("安装", "卸载", "列表"):
        return _run_package_cmd(argv)

    parser = argparse.ArgumentParser(
        prog="jishi", description="基石 —— 中文编程语言解释器")
    parser.add_argument("file", nargs="?", help="要运行的 .jsh 脚本文件")
    parser.add_argument("-i", "--repl", action="store_true",
                        help="进入交互式 REPL")
    parser.add_argument("--ast", action="store_true",
                        help="只显示语法树，不运行")
    parser.add_argument("--json-errors", action="store_true",
                        help="错误以 JSON 输出（供 agent 程序化读取，M8.3）")
    parser.add_argument("--ai-card", action="store_true",
                        help="输出 AI 语言卡（贴进 LLM 系统提示词，M8.1）")
    parser.add_argument("--lang-spec", action="store_true",
                        help="输出机器可读语言规格（配合 --format json）")
    parser.add_argument("--format", choices=("json",), default="json",
                        help="--lang-spec 的输出格式")
    parser.add_argument("--sandbox", action="store_true",
                        help="沙箱执行：超时/输出上限/禁文件写/模块白名单（M9.1）")
    parser.add_argument("--json-result", action="store_true",
                        help="以统一协议 JSON 输出结果（成功失败皆 JSON，M9.2）")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="沙箱超时秒数（默认 5）")
    parser.add_argument("--stdin", action="store_true",
                        help="从标准输入读取源码（配合 --sandbox 供 MCP 调用）")
    parser.add_argument("--dump-bytecode", action="store_true",
                        help="把脚本编译成跨语言字节码并输出 JSON（M13.1）")
    parser.add_argument("--load-bytecode", action="store_true",
                        help="输入的是字节码 JSON 而非源码（配合文件/--stdin，M13.1）")
    parser.add_argument("-v", "--version", action="version",
                        version=f"基石 jishi {VERSION}")
    args = parser.parse_args(argv)

    if args.ai_card:
        from .ai import render_ai_card
        print(render_ai_card())
        return 0
    if args.lang_spec:
        from .ai import lang_spec_json
        print(lang_spec_json())
        return 0

    # M13.1：跨语言字节码（编译 → JSON / JSON → 执行）
    if args.dump_bytecode:
        return _dump_bytecode(args.file, stdin=args.stdin)
    if args.load_bytecode:
        return _run_bytecode(args.file, stdin=args.stdin)

    if args.file or args.stdin:
        return run_file(
            args.file, show_ast=args.ast, json_errors=args.json_errors,
            sandbox=args.sandbox, json_result=args.json_result,
            timeout=args.timeout, stdin=args.stdin)

    from .repl import repl
    return repl()


if __name__ == "__main__":
    sys.exit(main())
