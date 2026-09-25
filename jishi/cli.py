# -*- coding: utf-8 -*-
"""基石语言命令行入口。

用法：
    jishi 文件.jsh            运行脚本
    jishi 文件.jsh --ast      只显示语法树
    jishi 调试 文件.jsh       调试（断点/单步/看变量与调用栈，M37）
    jishi dap                 以 Debug Adapter Protocol 服务编辑器
                              （M43；由 VS Code 之类的编辑器启动，不手敲）
    jishi -i                  交互式 REPL
    jishi                     进入 REPL
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from .errors import JishiError
from .interpreter import Interpreter, run_source
from .parser import parse
from .tokenizer import tokenize
from .version import VERSION

BANNER = f"""基石 jishi {VERSION} —— 一门像 Python 一样简单易用的中文编程语言
输入「退出」离开；冒号结尾的行会继续输入（多行代码块）。
"""


def _read_source(path: str) -> tuple[str, str]:
    # 目录要**先判**：Windows 上 open(目录) 抛的是 PermissionError 而不是
    # IsADirectoryError（M38 B4 实测），靠异常类型分会漏掉，给出的提示也就
    # 变成「没有权限」这种误导性的话。
    if os.path.isdir(path):
        print(f"「{path}」是个目录。要整个目录一起处理请用："
              f"「jishi 格式化 {path} --check」或「… --write」；"
              "只想看一个文件就把文件名写全", file=sys.stderr)
        sys.exit(2)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), path
    except FileNotFoundError:
        print(f"找不到文件「{path}」", file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print(f"没有权限读取「{path}」", file=sys.stderr)
        sys.exit(2)
    except UnicodeDecodeError:
        print(f"文件「{path}」不是 UTF-8 编码，无法读取", file=sys.stderr)
        sys.exit(1)
    except OSError as e:                # 兜底：别让英文 OSError 直接冒出来
        print(f"读不了文件「{path}」：{e.strerror or e}", file=sys.stderr)
        sys.exit(2)


def _read_stdin() -> str:
    """从标准输入读源码（按「字节 → UTF-8」读）。

    不能用 ``sys.stdin.read()``：Windows 下标准输入默认按本地代码页
    （中文机器是 GBK）解码，中文源码经管道/子进程传入会变乱码
    （实测 ``打印`` 会变成 ``鎵撳嵃``）。走字节层再显式 UTF-8 解码，
    结果不受环境编码与 ``PYTHONUTF8`` 是否设置影响。

    测试里 ``sys.stdin`` 可能被替换成 ``StringIO``（没有 ``buffer``），
    此时回退到文本层读取。
    """
    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:
        return sys.stdin.read()
    data = buf.read()
    if isinstance(data, str):           # 已是文本（被包装过）
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # 非 UTF-8 字节：替换成 U+FFFD，让词法器给出「无法识别的字符」
        # 的中文报错，而不是在解码处直接崩掉。
        return data.decode("utf-8", "replace")


def _dump_bytecode(path: Optional[str], stdin: bool = False) -> int:
    """把源码编译成跨语言字节码 JSON 并输出（M13.1）。"""
    from .compiler import compile_source
    from .serialize import dumps

    if stdin:
        source = _read_stdin()
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
        text = _read_stdin()
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
             stdin: bool = False, argv: Optional[list[str]] = None) -> int:
    if argv is not None:
        # M18.2：把命令行参数注入「系统.参数()」
        from .stdlib import 系统 as _sys_mod
        _sys_mod._设参数(argv)
    if stdin:
        source = _read_stdin()
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
    # M47 ③：先查 AST 缓存（键 = 源码内容哈希 + 版本），命中就省掉分词与语法分析
    # （360 行的真实项目实测省 ~12ms）。**只对真实文件用缓存**——stdin / 片段
    # 没有稳定的路径当键，缓存了反而会串味。模块惰性导入，不拖慢 CLI 启动。
    program = None
    if not stdin:
        from . import cache as _cache
        program = _cache.load(source, filename)
    if program is None:
        try:
            tokens = tokenize(source, filename)
            program = parse(tokens, lines, filename)
        except JishiError as e:
            if json_errors:
                _print_json_error(e)
            else:
                print(e.render(), file=sys.stderr)
            return 1
        if not stdin:
            from . import cache as _cache
            _cache.store(source, filename, program)

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
    """`jishi 安装 包名`：从本地目录 / zip / 索引 安装包到 .jishi/packages/。"""
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
        if args.index:
            # 显式 --index（M22.1）
            _install_from_index(args.package, args.index, force=args.force)
            return 0
        # 默认：本地包缓存目录直接按名字找
        import os
        from .runtime import _find_pkg_dir
        pdir = _find_pkg_dir(args.package)
        if pdir:
            print(f"包「{args.package}」已在本地缓存：{pdir}")
            return 0
        # 本地没有 → 走默认远端索引（M22.1）；加 --no-index 可关掉
        if getattr(args, "no_index", False):
            print(f"找不到包「{args.package}」", file=sys.stderr)
            print("用法：jishi 安装 包名 [--local-dir 目录 | --local-zip 包.zip | "
                  "--index 索引.json]", file=sys.stderr)
            return 1
        url = PKG.default_index_url()
        print(f"本地没有，尝试默认索引：{url}")
        _install_from_index(args.package, url, force=args.force)
        return 0
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
        # 索引地址本身也可能含中文（如 --index https://…/索引.json）
        url = PKG.encode_url(index_url)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
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
    # 地址里含中文等非 ASCII 时统一编码（urllib 不接受裸非 ASCII URL）；
    # 手写的索引可能没编码，这里兜住。
    url = PKG.encode_url(url)
    tmp = os.path.join(tempfile.gettempdir(), f"jishi-{name}.zip")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, \
                open(tmp, "wb") as out:
            out.write(resp.read())
        # 校验和（M22.2）：索引里有「校验和」就必须对得上，防止下载被篡改
        want = str(meta.get("校验和") or "").strip()
        if want:
            want = want.split(":", 1)[1] if want.startswith("sha256:") else want
            got = PKG._sha256_file(tmp)
            if got.lower() != want.lower():
                raise ValueError(
                    f"包「{name}」的校验和对不上（索引写 {want[:16]}…，"
                    f"实际 {got[:16]}…），文件可能被改动过，已放弃安装")
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


def _cmd_doctor(args) -> int:
    """`jishi 医生`：环境自检，异常给中文修复建议（M16.3）。"""
    import os
    import sys as _sys

    ok = True
    print("== 基石环境自检 ==")

    # 1. 版本
    print(f"  版本：{VERSION}")

    # 2. Python 版本
    py_ver = f"{_sys.version_info.major}.{_sys.version_info.minor}"
    print(f"  Python：{py_ver}")

    # 3. 安装位置
    try:
        import jishi as _jishi
        print(f"  安装位置：{os.path.dirname(os.path.abspath(_jishi.__file__))}")
    except Exception:  # noqa: BLE001
        print("  安装位置：未知")

    # 4. C VM 是否加载成功
    try:
        from . import cvm_bind as _cvm
        _cvm.load_lib()
        print("  C VM：已加载 [OK]")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  C VM：加载失败 [异常]（{e}）")
        print("    建议：运行 python cvm/build.py 重新构建动态库")

    # 5. 终端编码
    enc = getattr(_sys.stdout, "encoding", None) or "未知"
    print(f"  终端编码：{enc}")
    if enc and enc.lower() not in ("utf-8", "utf8", "cp65001"):
        print("    提示：建议使用 UTF-8 编码终端（Windows 可执行 chcp 65001）")

    # 6. MCP 可用性
    try:
        from . import mcp_server
        print("  MCP Server：可用 [OK]")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  MCP Server：异常 [异常]（{e}）")

    print()
    if ok:
        print("[OK] 环境正常，可以开始写基石代码了。")
        return 0
    print("[警告] 发现环境问题，请按上方建议修复。")
    return 1


def _cmd_new_project(args) -> int:
    """`jishi 新项目 名字`：生成脚手架（M16.5）。"""
    import os
    name = args.name
    target = os.path.join(os.getcwd(), name)
    if os.path.exists(target):
        print(f"目录「{name}」已存在，换个名字吧", file=sys.stderr)
        return 1
    os.makedirs(target)
    hello = f"""# {name} —— 你的第一个基石项目

令 名字 = "世界"
打印("你好，" + 名字)
打印(`这是 {name} 项目的第一行代码`)
"""
    with open(os.path.join(target, "hello.jsh"), "w", encoding="utf-8") as f:
        f.write(hello)
    with open(os.path.join(target, "说明.md"), "w", encoding="utf-8") as f:
        f.write(f"# {name}\n\n运行 `jishi hello.jsh` 查看效果。\n")
    print(f"已创建项目「{name}」：")
    print(f"  {target}/hello.jsh")
    print(f"  {target}/说明.md")
    print()
    print(f"下一步：cd {name} && jishi hello.jsh")
    return 0


def _cmd_tutorial(args) -> int:
    """`jishi 教程`：打印本地教程索引（M16.5）。"""
    print("== 基石教程（10 章）==")
    chapters = [
        ("01", "你好基石", "运行基石、打印、变量、注释"),
        ("02", "数与文本", "运算、字符串方法、输入、列表"),
        ("03", "判断与循环", "如果/否则、遍历/循环/当、链式比较"),
        ("04", "函数", "定义函数、作用域、递归"),
        ("05", "字典与结构化数据", "键值对、嵌套、计数器"),
        ("06", "文件与标准库", "文件读写、数学/时间/表格模块"),
        ("07", "综合实战", "记账程序：需求→设计→实现"),
        ("08", "语法糖", "多赋值解包/默认参数/推导式/文本插值"),
        ("09", "沙箱与 MCP", "沙箱三道保险/统一协议/MCP Server"),
        ("10", "包管理", "安装/卸载/列表/依赖解析"),
    ]
    for num, title, desc in chapters:
        print(f"  第{num}章 {title}：{desc}")
    print()
    print("完整教程见 docs/tutorial/ 目录，或访问项目 README 的教程索引。")
    return 0


def _cmd_format(args) -> int:
    """统一缩进与词间间距（M21.1）。

    保留注释与字符串原样，只调整排版；公式化的输出可重复格式化而不变。
    """
    from .formatter import format_source

    if args.stdin:
        src = _read_stdin()
        name = "<stdin>"
    else:
        if not args.file:
            print("请给出要格式化的文件，或用 --stdin 从标准输入读取",
                  file=sys.stderr)
            return 2
        from pathlib import Path
        if Path(args.file).is_dir():
            # 目录要**说明白做什么**：不 --check 也不 --write 时把整目录的内容
            # 打到屏幕上没有意义。README 里一直写着 `--check examples/`，
            # 而这条路在 M38 B4 之前是抛英文 PermissionError 的（实测）。
            if not (args.check or args.write):
                print(f"「{args.file}」是目录，请说明要做什么：\n"
                      f"  jishi 格式化 {args.file} --check   # 只检查（未格式化则退出码 1）\n"
                      f"  jishi 格式化 {args.file} --write   # 就地改写目录里所有 .jsh",
                      file=sys.stderr)
                return 2
            return _format_tree(Path(args.file), args)
        src, name = _read_source(args.file)

    if args.write and args.stdin:
        print("--write 不能和 --stdin 一起用", file=sys.stderr)
        return 2

    try:
        out = format_source(src, name, indent=args.indent)
    except JishiError as e:
        e.with_source(src.replace("\r\n", "\n").split("\n"))
        print(e.render(), file=sys.stderr)
        return 1

    if args.check:
        if src == out:
            return 0
        print(f"{name} 没有按统一风格书写"
              f"（跑 jishi 格式化 {args.file or '-'} --write 可自动修）",
              file=sys.stderr)
        return 1

    if args.write:
        with open(args.file, "w", encoding="utf-8", newline="\n") as f:
            f.write(out)
        print(f"已格式化 {args.file}")
        return 0

    sys.stdout.write(out)
    return 0


def _format_tree(root, args) -> int:
    """`jishi 格式化 <目录>`：递归处理目录下的 .jsh（M38 B4）。

    两件事要分清楚：

    - **没格式化**（跑一遍会变）→ 算「不合风格」，`--check` 时退出码 1；
    - **解析不了**（本来就写错了，或有读不了的文件）→ 单独报，**不混进**
      「不合风格」里——两者的修法完全不同（一个跑 `--write`，一个得先改代码）。

    语法错的文件不该让整轮处理中断：报告它、继续处理其余文件。
    """
    from .formatter import format_source

    files = sorted(p for p in root.rglob("*.jsh") if p.is_file())
    if not files:
        print(f"「{root}」里没有 .jsh 文件", file=sys.stderr)
        return 2

    unformatted: list = []
    broken: list = []
    for p in files:
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            broken.append(f"{p}（读不了：{e}）")
            continue
        try:
            out = format_source(src, str(p), indent=args.indent)
        except JishiError as e:
            # 格式化器只依赖词法层：能走到这里的多半是**词法错**（未闭合字符串、
            # 非法字符）。语法错（漏冒号之类）照样能排版——用户可能正写到一半。
            broken.append(f"{p}（{e.code} {e.title}——先修好这处词法错误，再格式化）")
            continue
        if src != out:
            unformatted.append(p)
            if args.write:
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(out)
                print(f"已格式化 {p}")

    if args.check:
        if unformatted:
            print(f"{len(unformatted)}/{len(files)} 个文件没有按统一风格书写"
                  f"（跑 jishi 格式化 {root} --write 可自动修）：", file=sys.stderr)
            for p in unformatted:
                print(f"  {p}", file=sys.stderr)
        else:
            print(f"全部 {len(files)} 个文件都符合统一风格")
    else:                                # --write
        n = len(unformatted)
        print(f"共 {len(files)} 个文件，改写了 {n} 个"
              + ("（其余本来就符合）" if len(files) > n else ""))

    for b in broken:
        print(f"跳过：{b}", file=sys.stderr)
    if args.check:
        return 1 if (unformatted or broken) else 0
    # --write：改写了是**成功**，只有「没处理掉的文件」才算失败
    return 1 if broken else 0


def build_debug_session(engine: str, source: str, filename: str,
                        lines, *, program=None, breakpoints=(),
                        stop_on_entry: bool = True,
                        read_line=None, write=None, script=None,
                        interactive: bool = True):
    """按执行器名装配一个调试会话。

    「谁来跑、怎么编译」随执行器而变，但**会话参数是同一套**——放在一处，
    命令行（`jishi 调试`）与编辑器（`jishi dap`）两条入口才不会漂移。

    `engine` 取 `树遍历` / `vm` / `cvm`；`program` 传解析好的 AST 可省一次解析。
    出错抛 `JishiError`：调用方自己决定怎么呈现（CLI 打源码，DAP 回 error）。
    """
    from .debugger import CvmDebugger, Debugger, VmDebugger

    if program is None:
        program = parse(tokenize(source, filename), lines, filename)
    common = dict(source_lines=lines, filename=filename, breakpoints=breakpoints,
                  stop_on_entry=stop_on_entry, read_line=read_line, write=write,
                  script=script, interactive=interactive)
    if engine == "vm":
        from .compiler import compile_program
        from .vm import VM

        cmod = compile_program(program, filename, lines)
        return VmDebugger(VM(cmod, filename), **common)
    if engine == "cvm":
        from .compiler import compile_program
        from .cvm_bind import CvmRunner

        cmod = compile_program(program, filename, lines)
        return CvmDebugger(CvmRunner(cmod, filename), **common)
    return Debugger(Interpreter(filename=filename), program, **common)


def _cmd_debug(args) -> int:
    """`jishi 调试 脚本.jsh [--执行器 树遍历|vm|cvm]`：跑一场调试会话。

    与 `jishi 脚本.jsh` 的唯一差别是先挂上调试器再跑，于是能在断点处停下看
    变量与调用栈。**三个执行器都能调**（M40 起齐了）：默认树遍历（M37 · B1），
    `--执行器 vm` 走字节码 Python VM（M39 · B5），`--执行器 cvm` 走 C 虚拟机
    （M40），三者的会话记录逐字相同。
    """
    from .debugger import parse_lines

    engine = args.engine or "树遍历"
    if engine == "cvm":
        # 三条执行器的调试器都齐了（M40 起），这里只剩「动态库在不在」这一件事
        try:
            from . import cvm_bind as _cvm
            _cvm.load_lib()
        except Exception as e:              # noqa: BLE001
            print(f"用不了 C 虚拟机：{e}\n"
                  "请先构建：python cvm/build.py，或改用 --执行器 树遍历 / vm",
                  file=sys.stderr)
            return 2

    source, filename = _read_source(args.file)
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    try:
        program = parse(tokenize(source, filename), lines, filename)
    except JishiError as e:
        e.with_source(lines)
        print(e.render(), file=sys.stderr)
        return 1

    try:
        wanted = parse_lines(args.breakpoints)
    except ValueError as e:
        print(f"看不懂这个断点行号：「{e.args[0]}」——"
              "写法是 --断点 12 或 --断点 12,15", file=sys.stderr)
        return 2

    script = None
    if args.commands:
        # 允许用中文分号（中文输入法下最常见的写法）
        script = [c.strip() for c in args.commands.replace("；", ";").split(";")
                  if c.strip()]

    try:
        dbg = build_debug_session(
            engine, source, filename, lines, program=program,
            breakpoints=wanted, stop_on_entry=not args.no_entry, script=script)
    except JishiError as e:                 # 编译期错误（语法错在前一步已拦）
        e.with_source(lines)
        print(e.render(), file=sys.stderr)
        return 1
    if args.engine:                     # 用户显式选了执行器：说一声用的哪个
        print(f"（调试执行器：{dbg.executor_name}；默认是树遍历）")

    # 断点写在空行/注释行上时**当场就说**：不然用户看着断点加上了、程序却从不停，
    # 只会怀疑调试器坏了（这类「静默无效」是本项目最想避免的一类问题）。
    dead = sorted(n for n in wanted if n not in dbg.stmt_lines)
    if dead:
        from .debugger import _DEAD_LINE_HINT
        print("提示：" + "、".join(f"第 {n} 行" for n in dead)
              + f" 上没有可停的语句（{_DEAD_LINE_HINT}），"
                "这几个断点不会触发", file=sys.stderr)

    try:
        err = dbg.run()
    except KeyboardInterrupt:
        # 暂停时按 Ctrl-C：与 `jishi 脚本.jsh` 的说法保持一致
        print("\n调试会话被中断", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        # 兜底：调试器本身出的问题也不该把英文堆栈甩给用户（M17 的方针）
        print(f"错误：调试器内部出现未预料的问题（{type(e).__name__}）：{e}",
              file=sys.stderr)
        return 1
    if err is not None:
        err.with_source(lines)
        print(err.render(), file=sys.stderr)
        return 1
    return 0


def _cmd_dap(args) -> int:
    """`jishi dap [--执行器 …]`：以 DAP 跟编辑器对话（M43）。

    由编辑器（VS Code 等）当子进程启动，不面向手敲——所以这里不打印任何
    「给你看」的东西：协议里的 error 才是对客户端的交代（多打一行普通文本
    会污染 stdout，编辑器解析不了）。
    """
    from .dap import main as dap_main

    return dap_main(["--执行器", args.engine] if args.engine else [])


def _cmd_publish(args) -> int:
    """`jishi 发布 <包目录>`：打包 + 生成索引条目（M22.2）。

    只做「本地产物 + 给出条目」：真正的托管是静态文件（推仓库 / 传 CDN），
    不做账号体系与上传接口——与项目「零依赖、静态托管」的取向一致。
    """
    import json as _json

    from . import packages as PKG

    pkg_dir = args.path
    out_dir = args.out
    try:
        zip_path, name = PKG.build_package_zip(pkg_dir, out_dir)
        checksum = PKG._sha256_file(zip_path)
        entry = PKG.index_entry(
            pkg_dir, base_url=args.base_url,
            zip_name=os.path.basename(zip_path))
        if args.base_url:
            entry["校验和"] = f"sha256:{checksum}"
    except ValueError as e:
        print(f"发布失败：{e}", file=sys.stderr)
        return 1

    size_kb = os.path.getsize(zip_path) / 1024
    print(f"已打包：{zip_path}（{size_kb:.1f} KB）")
    print(f"校验和：sha256:{checksum}")

    if not args.base_url:
        print("未指定 --base-url，索引条目里不含下载地址；"
              "发布时请用 --base-url https://…/下载 指定托管前缀")

    print("索引条目：")
    print(_json.dumps({name: entry}, ensure_ascii=False, indent=2))

    if args.index:
        try:
            if os.path.isfile(args.index):
                with open(args.index, encoding="utf-8") as f:
                    old = PKG.parse_index(f.read())
            else:
                old = PKG.make_index({})
            merged = PKG.merge_index(old, name, entry)
            with open(args.index, "w", encoding="utf-8", newline="\n") as f:
                f.write(PKG.index_to_text(merged))
        except (ValueError, OSError) as e:
            print(f"写入索引「{args.index}」失败：{e}", file=sys.stderr)
            return 1
        print(f"已更新索引：{args.index}")
    return 0


def _cmd_index(args) -> int:
    """`jishi 索引 <目录>`：扫描目录下的包，批量重建索引（M22.2）。"""
    import json as _json

    from . import packages as PKG

    try:
        dirs = PKG.scan_packages(args.path)
    except ValueError as e:
        print(f"生成索引失败：{e}", file=sys.stderr)
        return 1
    if not dirs:
        print(f"「{args.path}」下没找到任何包（子目录需含 包.json）", file=sys.stderr)
        return 1

    if args.zip_dir:
        os.makedirs(args.zip_dir, exist_ok=True)

    packages: dict[str, dict] = {}
    for d in dirs:
        try:
            meta = PKG.read_package_meta(d)
            name = meta["名字"]
            zip_name = f"{name}-{meta['版本']}.zip"
            entry = PKG.index_entry(d, base_url=args.base_url, zip_name=zip_name)
            if args.zip_dir or args.checksum:
                zip_path, _ = PKG.build_package_zip(d, args.zip_dir or args.out)
                entry["校验和"] = f"sha256:{PKG._sha256_file(zip_path)}"
            packages[name] = entry
        except ValueError as e:
            print(f"跳过「{d}」：{e}", file=sys.stderr)

    if not packages:
        print("没有可打包的包", file=sys.stderr)
        return 1

    text = PKG.index_to_text(PKG.make_index(packages))
    if args.out_index:
        with open(args.out_index, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"已生成索引：{args.out_index}（含 {len(packages)} 个包）")
    else:
        print(text)
    return 0


def _cmd_check(args) -> int:
    """`jishi 检查`：检测已装包的依赖冲突（钻石依赖，M22.3）。"""
    from . import packages as PKG
    from .runtime import _find_pkg_dir

    installed = [p["名字"] for p in PKG.list_packages()]
    roots = args.packages or installed
    if not roots:
        print("（尚未安装任何本地包）")
        return 0

    conflicts = PKG.detect_conflicts(roots, find_dir=_find_pkg_dir)
    if not conflicts:
        print(f"检查了 {len(roots)} 个包，未发现依赖冲突")
        return 0
    for c in conflicts:
        print(PKG.format_conflict(c))
    print(f"\n共发现 {len(conflicts)} 处依赖冲突")
    return 1


def _cmd_static_check(args) -> int:
    """`jishi 源码静态检查`：不运行程序，只看源码有问题（M44）。

    与 `jishi 检查`（**包依赖冲突检测**，M22.3）是两件事——那个查的是已装包的
    版本冲突，这个查的是源码本身。命名上刻意错开，免得两条命令互相顶替。

    退出码：发现问题为 1（供 CI / 提交钩子用），没发现为 0。
    """
    from .checker import LEVEL_ERROR, check_paths
    from .langdata import LangData

    paths = list(args.paths) if args.paths else ["."]
    rows = check_paths(paths, LangData())

    if args.json:
        payload = {
            "ok": not rows,
            "issues": [{"file": str(p), **i.to_dict()} for p, i in rows],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if rows else 0

    if not rows:
        print(f"检查了 {len(paths)} 个路径，没发现问题。")
        return 0

    for path, issue in rows:
        print(issue.format(str(path)))
    errors = sum(1 for _, i in rows if i.level == LEVEL_ERROR)
    print(f"\n共 {len(rows)} 个问题（错误 {errors} 个 / 警告 {len(rows) - errors} 个），"
          f"涉及 {len({str(p) for p, _ in rows})} 个文件。")
    print("（只是提醒，不改你的代码；确认无误可以照常运行）")
    return 1


def _cmd_lsp(args) -> int:
    """启动语言服务器（M21.2，stdio 上的 LSP）。

    编辑器通过 stdio 与本进程通信：实时诊断、补全、悬停、跳转定义。
    """
    from .lsp import main as lsp_main

    return lsp_main(None)


def _run_package_cmd(argv: list[str]) -> int:
    """包管理子命令分发：安装/卸载/列表/发布/索引/检查（M14.2 / M22）。"""
    parser = argparse.ArgumentParser(
        prog="jishi", description="基石包管理")
    sub = parser.add_subparsers(dest="subcommand", metavar="命令", required=True)

    p_install = sub.add_parser("安装", help="安装包到 .jishi/packages/")
    p_install.add_argument("package", help="要安装的包名")
    p_install.add_argument("--local-dir", help="从本地目录安装（含 包.json）")
    p_install.add_argument("--local-zip", help="从本地 zip 安装（内含 包名/ 顶层目录）")
    p_install.add_argument("--from-index", help="从指定索引 JSON（本地文件或 URL）拉包")
    p_install.add_argument("--index", help="同 --from-index（M22.1）")
    p_install.add_argument("--no-index", action="store_true",
                           help="不联网：本地找不到就直接报错")
    p_install.add_argument("--force", action="store_true", help="覆盖已安装的同名包")
    p_install.set_defaults(func=_cmd_install)

    p_uninstall = sub.add_parser("卸载", help="卸载本地包")
    p_uninstall.add_argument("package", help="要卸载的包名")
    p_uninstall.set_defaults(func=_cmd_uninstall)

    p_list = sub.add_parser("列表", help="列出已安装的本地包")
    p_list.set_defaults(func=_cmd_list)

    p_pub = sub.add_parser("发布", help="打包并生成索引条目（M22.2）")
    p_pub.add_argument("path", help="包目录（含 包.json）")
    p_pub.add_argument("--out", default="dist/packages",
                       help="zip 输出目录（默认 dist/packages）")
    p_pub.add_argument("--base-url", default="",
                       help="下载地址前缀，如 https://…/packages/下载")
    p_pub.add_argument("--index", default="",
                       help="把条目合并进这个索引文件（不存在则新建）")
    p_pub.set_defaults(func=_cmd_publish)

    p_idx = sub.add_parser("索引", help="扫描目录批量重建索引（M22.2）")
    p_idx.add_argument("path", help="包目录的父目录（或单个包目录）")
    p_idx.add_argument("--base-url", default="",
                       help="下载地址前缀，如 https://…/packages/下载")
    p_idx.add_argument("--out-index", default="",
                       help="索引输出路径（默认打印到标准输出）")
    p_idx.add_argument("--zip-dir", default="",
                       help="同时在此目录生成 zip（顺带算校验和）")
    p_idx.add_argument("--out", default="dist/packages",
                       help="--zip-dir 未给时的临时打包目录")
    p_idx.add_argument("--checksum", action="store_true",
                       help="为每个包算 sha256 写进索引（需打包）")
    p_idx.set_defaults(func=_cmd_index)

    p_check = sub.add_parser("检查", help="检测依赖冲突（钻石依赖，M22.3）")
    p_check.add_argument("packages", nargs="*",
                         help="要检查的包名（默认检查已装的全部）")
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


def _run_tool_cmd(argv: list[str]) -> int:
    """工具子命令分发：jishi 医生/新项目/教程（M16）。"""
    parser = argparse.ArgumentParser(
        prog="jishi", description="基石工具")
    sub = parser.add_subparsers(dest="subcommand", metavar="命令", required=True)

    sub.add_parser("医生", help="环境自检（安装位置/版本/C VM/编码/MCP）").set_defaults(
        func=_cmd_doctor)

    p_new = sub.add_parser("新项目", help="生成脚手架项目")
    p_new.add_argument("name", help="项目名")
    p_new.set_defaults(func=_cmd_new_project)

    sub.add_parser("教程", help="打印本地教程索引").set_defaults(
        func=_cmd_tutorial)

    p_fmt = sub.add_parser("格式化", help="统一缩进与间距（M21.1）")
    p_fmt.add_argument("file", nargs="?", help="要格式化的 .jsh 文件")
    p_fmt.add_argument("-w", "--write", action="store_true",
                       help="原地写回文件（默认输出到标准输出）")
    p_fmt.add_argument("--check", action="store_true",
                       help="只检查是否已格式化（未格式化时退出码 1，供 CI 用）")
    p_fmt.add_argument("--stdin", action="store_true",
                       help="从标准输入读取源码")
    p_fmt.add_argument("--indent", type=int, default=4,
                       help="每级缩进的空格数（默认 4）")
    p_fmt.set_defaults(func=_cmd_format)

    sub.add_parser("lsp", help="启动语言服务器（stdio，M21.2）").set_defaults(
        func=_cmd_lsp)

    p_dbg = sub.add_parser("调试", help="调试脚本：断点/单步/看变量与调用栈（M37）")
    p_dbg.add_argument("file", help="要调试的 .jsh 脚本")
    p_dbg.add_argument("-b", "--断点", dest="breakpoints", action="append",
                       default=[], metavar="行号",
                       help="断点行号；可重复，也可写「--断点 5,12」")
    p_dbg.add_argument("--不停在入口", dest="no_entry", action="store_true",
                       help="不自动停在第一句（默认会停在第一句）")
    p_dbg.add_argument("--命令", dest="commands", default=None, metavar="命令串",
                       help="按顺序自动执行的调试命令（用分号分隔），"
                            "供演示与自动化；命令用完会自动继续")
    p_dbg.add_argument("--执行器", dest="engine", default=None,
                       choices=["树遍历", "vm", "cvm"],
                       help="在哪个执行器上调试：树遍历（默认）/ vm（字节码 "
                            "Python VM）/ cvm（C 虚拟机）")
    p_dbg.set_defaults(func=_cmd_debug)

    p_dap = sub.add_parser(
        "dap", help="以 Debug Adapter Protocol 服务编辑器（M43）")
    p_dap.add_argument("--执行器", dest="engine", default=None,
                       choices=["树遍历", "vm", "cvm"],
                       help="在哪个执行器上调试（默认树遍历）")
    p_dap.set_defaults(func=_cmd_dap)

    # 注意与包管理那条 `检查` 区分：`检查` 查的是**包依赖冲突**，
    # 这条查的是**源码**。名字由用户定（2026-09-22）：叫「源码静态检查」，
    # 别名「静态检查」——两条命令都带「检查」，但一个是包、一个是源码，
    # 所以正式名字里点明「源码」。
    p_static = sub.add_parser(
        "源码静态检查", aliases=["静态检查"],
        help="不运行程序，检查源码问题（M44：未定义名/遮蔽内建/未使用变量/可疑相等）")
    p_static.add_argument("paths", nargs="*", metavar="路径",
                          help="要检查的 .jsh 文件或目录（默认当前目录，递归找 .jsh）")
    p_static.add_argument("--json", action="store_true",
                          help="输出机器可读 JSON（供脚本与 AI 消费）")
    p_static.set_defaults(func=_cmd_static_check)

    args = parser.parse_args(argv)
    return args.func(args)


def _setup_io() -> None:
    """把标准输出/错误固定成「UTF-8 + 不转换换行」（M33）。

    - **UTF-8**：Windows 的 Cmd 默认不是 UTF-8，中文会 `UnicodeEncodeError`。
    - **不转换换行**：文本模式在 Windows 上会把 `\n` 写成 `\r\n`，于是同一份
      程序在 Python 侧输出 CRLF、在 Node / Rust 宿主输出 LF——「同一份程序到哪
      都一样」在重定向输出时就破了（M33 端到端对拍撞出来的）。
      只改输出；读入仍走通用换行，`\n` 与 `\r\n` 都认。

    包成函数是因为入口有两条：`python -m jishi.cli` 直接调 `main()`，
    PyInstaller 打包版走 `tools/entry_cli.py`——两条路都得设。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        try:
            stream.reconfigure(newline="\n")
        except Exception:  # noqa: BLE001
            pass


def main(argv: Optional[list[str]] = None) -> int:
    _setup_io()
    if argv is None:
        argv = sys.argv[1:]
    # 包管理子命令（安装/卸载/列表/发布/索引/检查）走独立解析器，
    # 避免与「运行文件」位置参数冲突
    if argv and argv[0] in ("安装", "卸载", "列表", "发布", "索引", "检查"):
        return _run_package_cmd(argv)
    # 工具子命令（医生/新项目/教程/格式化/lsp/调试/dap/源码静态检查，
    # M16 / M21 / M37 / M43 / M44）
    if argv and argv[0] in ("医生", "新项目", "教程", "格式化", "lsp", "调试", "dap",
                            "源码静态检查", "静态检查"):
        return _run_tool_cmd(argv)

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
    parser.add_argument("--no-cache", action="store_true",
                        help="本次不读也不写 AST 缓存（M47③；等价于 JISHI_NO_CACHE=1）")
    parser.add_argument("--dump-bytecode", action="store_true",
                        help="把脚本编译成跨语言字节码并输出 JSON（M13.1）")
    parser.add_argument("--load-bytecode", action="store_true",
                        help="输入的是字节码 JSON 而非源码（配合文件/--stdin，M13.1）")
    parser.add_argument("-v", "--version", action="version",
                        version=f"基石 jishi {VERSION}")
    # M18.2：用 parse_known_args 捕获脚本名之后的额外参数（传给 系统.参数()）
    args, rest = parser.parse_known_args(argv)

    if args.no_cache:
        # 走环境变量而不是层层传参：`cache.enabled()` 认这一个开关，
        # 免得 run_file / 沙箱 / 字节码几条调用链各传一份（M47③）
        os.environ["JISHI_NO_CACHE"] = "1"

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
            timeout=args.timeout, stdin=args.stdin, argv=rest)

    from .repl import repl
    return repl()


if __name__ == "__main__":
    sys.exit(main())
