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
from .parser import parse_source
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
            program = parse_source(source, filename)
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
        if getattr(args, "锁定", False):
            return _install_locked(args)
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
        urls = PKG.default_index_urls()
        print(f"本地没有包「{args.package}」，按优先级尝试 {len(urls)} 个索引源：")
        _install_from_sources(args.package, urls, force=args.force)
        return 0
    except ValueError as e:
        print(f"安装失败：{e}", file=sys.stderr)
        return 1


def _read_index_text(index_url: str) -> str:
    """读索引文本：本地文件直接读，远端 URL 走 HTTP。

    ⚠️ 超时用 ``PKG.INDEX_TIMEOUT``（**故意调短**）—— 源不可达时要快速失败，
    否则用户会以为程序卡死了，而且没有时间去试下一个源（M49a）。
    """
    import os
    import urllib.request

    from . import packages as PKG

    if os.path.isfile(index_url):
        with open(index_url, encoding="utf-8") as f:
            return f.read()
    # 索引地址本身也可能含中文（如 --index https://…/索引.json）
    url = PKG.encode_url(index_url)
    with urllib.request.urlopen(url, timeout=PKG.INDEX_TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def _install_one(name: str, meta: dict, *, source: str, force: bool) -> None:
    """按索引条目下载 zip 并安装（含校验和比对）。"""
    import os

    from .errors import PackageChecksumError
    import tempfile
    import urllib.request

    from . import packages as PKG

    url = meta.get("下载地址")
    if not url:
        # E4002（索引条目不合格）：这条以前是裸 ValueError ——
        # **索引写错**（漏了下载地址）和**包不存在**是两回事，用户要的改法也不同。
        from .errors import PackageMetaError
        raise PackageMetaError(
            f"索引里包「{name}」没有「下载地址」字段。\n"
            f"  · 索引：{source}\n"
            f"  这说明**索引本身**有问题（不是你的环境）—— 去找维护索引的人，"
            f"或者换一个源。")
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
                # ⚠️ 这段文案**两个宿主逐字一致**（宿主侧 `install_one` 有一份同样的）：
                #    改这里就要同步改 `rust/src/packages.rs`，否则 `test_m96` 红。
                #    📌 也别加 `**加粗**` —— 控制台不渲染 markdown，会字面显示星号。
                raise PackageChecksumError(
                    f"包「{name}」的内容和索引里登记的对不上。\n"
                    f"  · 索引写：{want[:16]}…\n"
                    f"  · 实际是：{got[:16]}…\n"
                    f"  两种可能：下载不完整（先重下一次），或者包被换过"
                    f"（那就别装，去找发布者核对）。\n"
                    f"  ⚠️ 没有「跳过校验」这条路 —— 装进来的东西得有人担保。")
        PKG.install_package_zip(tmp, name, force=force)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(
            f"下载或安装包「{name}」失败：{e}\n"
            f"  下载地址来自索引源：{source}\n"
            f"  可换一个源再试：jishi 安装 {name} --index <索引地址>\n"
            f"  或用手上的包文件：jishi 安装 {name} --local-zip 包.zip") from e
    finally:
        if os.path.isfile(tmp):
            os.remove(tmp)
    print(f"已安装包「{name}」（版本 {meta.get('版本', '未知')}）")


def _unreachable_msg(name: str, unreachable: list) -> str:
    """所有索引源都读不到时的中文提示（M49a ②：给可操作的办法，不甩堆栈）。"""
    lines = [f"所有索引源都读不到（试了 {len(unreachable)} 个）："]
    for url, err in unreachable:
        lines.append(f"  ✗ {url}")
        lines.append(f"      {err}")
    lines += [
        "可能的原因与办法：",
        "  · 网络不通或被代理拦了 → 检查网络 / 代理设置后重试",
        f"  · 想换别的索引 → jishi 安装 {name} --index <索引地址>",
        f"  · 手上已有包文件 → jishi 安装 {name} --local-zip 包.zip",
        f"  · 手上已有包目录 → jishi 安装 {name} --local-dir 目录",
    ]
    return "\n".join(lines)


def _install_from_sources(name: str, urls: tuple, *, force: bool = False) -> None:
    """按顺序试每个索引源，第一个能给包的就用它（M49a · 多源回退）。

    为什么要回退：大陆访问 GitHub 不稳（实测 `git fetch` 传 3MB 就 300 秒超时），
    所以默认「自建源优先、GitHub 兜底」。**只要有一个源不可达，体验就退化成
    "时好时坏"** —— 这正是 M49 要修的东西。
    """
    from . import packages as PKG

    unreachable: list[tuple[str, str]] = []   # 读不到索引的源
    reached: list[str] = []                   # 读到了索引、但里面没有这个包
    total = len(urls)

    for i, url in enumerate(urls, 1):
        if total > 1:
            print(f"（{i}/{total}）索引源：{url}")
        try:
            text = _read_index_text(url)
        except Exception as e:  # noqa: BLE001
            unreachable.append((url, f"{type(e).__name__}: {e}"))
            if total > 1 and i < total:
                print("      读不到，换下一个源…")
            continue
        try:
            index = PKG.parse_index(text)
            PKG.resolve_package(index, name)          # 只用来确认「这个源有它」
        except ValueError as e:
            # ⚠️ 这里只写「码 + 标题」，**不要 `{e}`**（M132 对拍抓到的）：
            #    加了码之后 `str(e)` 是「错误 E4001：索引里没有这个包（索引里没有包「X」）」，
            #    套进括号就变成
            #    `索引里没有包「X」（已查 1 个源）：…（错误 E4001：索引里没有这个包（索引里没有包「X」））`
            #    —— 同一句话说了三遍，比不带码更难读。
            #    外层已经说了「索引里没有包」，内层只需要说「哪个码、什么性质」。
            # ⚠️ `parse_index` 抛的是**裸 ValueError**（索引格式坏），那种才原样打出来。
            from .errors import PackageError as _PkgErr
            reached.append(
                f"{url}（{e.code}：{e.title}）" if isinstance(e, _PkgErr)
                else f"{url}（{e}）")
            continue
        _install_closure(name, index, source=url, force=force)
        return

    if reached:
        # 至少有一个源读通了 —— 那不是网络问题，是「索引里确实没这个包」
        raise ValueError(
            f"索引里没有包「{name}」（已查 {len(reached)} 个源）：\n  "
            + "\n  ".join(reached))
    raise ValueError(_unreachable_msg(name, unreachable))


def _install_locked(args) -> int:
    """`jishi 安装 --锁定`：**严格按 `.jishi/包.lock`**，索引变了也不许变。

    ## 为什么这是可复现性的最后一环

    有了闭包解析还不够：索引里 `数学工具` 从 2.0.0 升到 3.0.0，同一条安装命令
    装出来的东西就变了。lock 是「把当时装的那一份钉住」。

    ## 三种对不上，分开说（修法完全不同）

    - **锁里有、索引里没有** ⇒ 换源了或包被撤了 ⇒ 让人「换源或删 lock 重装」；
    - **索引版本变了** ⇒ 这正是 lock 存在的意义 ⇒ **提示但不装**（地址指向新版，装了就是错的）；
    - **校验和变了** ⇒ 🔴 内容被改过 ⇒ **拒绝**，并说清这是安全问题。
    """
    import os

    from . import packages as PKG

    path = _lock_path()
    from .errors import PackageLockError

    if not os.path.isfile(path):
        print(f"错误 {PackageLockError.code}：{PackageLockError.title}"
              f"（没有锁定文件：{path}）", file=sys.stderr)
        print("先不带 --锁定 装一次（会生成它），或者把已有的 lock 放进去。",
              file=sys.stderr)
        return 1
    try:
        with open(path, encoding="utf-8") as f:
            lock = PKG.parse_lock(f.read())
    except (ValueError, OSError) as e:
        print(f"读不了锁定文件：{e}", file=sys.stderr)
        return 1

    urls = PKG.default_index_urls()
    index = None
    源 = ""
    读不到 = []
    for url in urls:
        try:
            index = PKG.parse_index(_read_index_text(url))
            源 = url
            break
        except Exception as e:                              # noqa: BLE001
            读不到.append(f"{url}（{type(e).__name__}: {e}）")
    if index is None:
        print("读不到任何索引源：", file=sys.stderr)
        for 行 in 读不到:
            print(f"  {行}", file=sys.stderr)
        return 1

    计划 = PKG.plan_from_lock(lock, index)

    if 计划["改"]:
        项 = 计划["改"][0]
        print(f"🔴 拒绝安装：「{项['包']}」的内容和锁定文件里记的不一致。", file=sys.stderr)
        print(f"  lock：{项['锁']}", file=sys.stderr)
        print(f"  索引：{项['索引']}", file=sys.stderr)
        print("  这可能是「包被换掉了」（安全问题），也可能是 lock 太旧。",
              file=sys.stderr)
        print("  确认无误后用 `jishi 安装 <包>` 重新解析并生成新 lock。", file=sys.stderr)
        return 1
    if 计划["缺"]:
        项 = 计划["缺"][0]
        print(f"锁定文件里的「{项['包']}」{项['锁版本']} 在索引里没有了（源：{源}）。",
              file=sys.stderr)
        print("  可能在别的源上，或者已被撤下。", file=sys.stderr)
        return 1
    if 计划["变"]:
        项 = 计划["变"][0]
        print(f"「{项['包']}」索引里已经是 {项['索引版本']}，锁定文件写的是 {项['锁版本']}。",
              file=sys.stderr)
        print("  ⚠️ 索引里的下载地址指向的是**新版本**，装下来就和你锁定的不一样了。",
              file=sys.stderr)
        print("  想要新的就删掉 lock 重新装；想要旧的得让索引能查到那个版本。",
              file=sys.stderr)
        return 1

    装序 = 计划["装序"]
    if not 装序:
        print("锁定文件里没有可装的包", file=sys.stderr)
        return 1
    print(f"按锁定文件装 {len(装序)} 个包（源：{源}）：")
    for i, (n, m) in enumerate(装序, 1):
        print(f"  {i}. {n} {m.get('版本', '')}")
    for i, (n, m) in enumerate(装序, 1):
        try:
            _install_one(n, m, source=源, force=getattr(args, "force", False))
        except ValueError as e:
            print(f"装「{n}」（第 {i}/{len(装序)} 个）失败：{e}", file=sys.stderr)
            return 1
    print(f"已按锁定文件装完：{path}")
    return 0


def _install_closure(name: str, index: dict, *, source: str,
                     force: bool = False) -> None:
    """装一个包**和它的全部依赖**（M130）。

    以前 `安装` 只装索引里那一个 —— 于是装「数学工具」会得到一个
    `导入 基础库` 就报错的包，而错误发生在**运行程序时**，离安装很远。

    ## 顺序与失败

    按 `resolve_closure` 给的**拓扑序**装（依赖在前）。任何一步失败就**停下**
    并说清是哪一步 —— 不继续装「装了一半」的包：**半个闭包比没有更糟**，
    它会让人以为装好了（`列表` 里看得见），实际跑起来缺东西。

    ## lock 什么时候写

    **全部装完才写**。中途失败写 lock，等于把「没装成」记成「装成了」。
    """
    from . import packages as PKG
    from .errors import (PackageConflictError, PackageCycleError,
                         PackageMissingDepError)

    闭包 = PKG.resolve_closure(index, [name])

    if 闭包["环"]:
        链 = " → ".join(闭包["环"][0])
        raise PackageCycleError(f"依赖成环，装不了：{链}")
    if 闭包["缺"]:
        项 = 闭包["缺"][0]
        链条 = " → ".join(项["链条"] + [项["包"]]) if 项["链条"] else 项["包"]
        raise PackageMissingDepError(
            f"依赖「{项['包']}」在索引里找不到（{链条}）\n"
            f"  · 索引：{source}\n"
            f"  · 要么这个包名写错了，要么它还没发布到这个源")
    if 闭包["冲突"]:
        项 = 闭包["冲突"][0]
        甲, 乙 = 项["甲"], 项["乙"]
        raise PackageConflictError(
            f"依赖冲突：「{项['包']}」现在只有 {项['版本']}，但被要求了两种不相容的版本 ——\n"
            f"  · {甲['来自']} 要 {甲['约束']}（{' → '.join(甲['链条'])}）\n"
            f"  · {乙['来自']} 要 {乙['约束']}（{' → '.join(乙['链条'])}）\n"
            f"  ⚠️ 基石不会「随便挑一个版本」——那会让装出来的东西不可预测。\n"
            f"  改法：让其中一个包放宽约束，或者先别同时装这两个。")

    装序: list[tuple[str, dict]] = 闭包["装序"]
    if not 装序:
        raise ValueError(f"索引里没有包「{name}」（{source}）")

    if len(装序) > 1:
        print(f"「{name}」需要连依赖一起装，共 {len(装序)} 个（按依赖顺序）：")
        for i, (n, m) in enumerate(装序, 1):
            print(f"  {i}. {n} {m.get('版本', '')}")

    for i, (n, m) in enumerate(装序, 1):
        try:
            _install_one(n, m, source=source, force=force)
        except ValueError:
            # ⚠️ **不要重新包装成 `type(e)(...)`**（M132 对拍抓到的）：
            #    异常自己的 `str()` 已经带「错误 E4006：…（<原因>）」，
            #    再套一层就变成同一句话说了两遍：
            #      `安装失败：错误 E4006：…（装「基础库」时失败：错误 E4006：…（包「基础库」…））`
            #    ⇒ **异常只讲「发生了什么」，上下文（第几个/没写 lock）由这里直接打**。
            #    这样宿主侧只要一条消息就能逐字对上（它没有闭包逻辑，天然是一层）。
            # ⚠️ **只在真的装了多个包时才打这句**（M132 对拍）：单包安装时这行
            #    毫无信息量（「第 1/1 个」），而宿主侧还没有闭包逻辑、天然不打印 ——
            #    两边就为这一句对不上。**「只有一个包」是两边共同的能力范围**，
            #    那就只在「超出共同范围」的地方加话。
            if len(装序) > 1:
                print(f"  ⚠️ 装「{n}」是第 {i}/{len(装序)} 个；前面的 {i - 1} 个已经装上了，"
                      f"没有写 jishi.lock（闭包没装全）。"
                      f"重跑一次 `jishi 安装 {name}` 会自动跳过已装的。",
                      file=sys.stderr, flush=True)
            raise

    _write_lock(PKG.lock_from_closure({"装序": 装序}))
    if len(装序) > 1:
        print(f"已写锁定文件：{_lock_path()}")


def _lock_path() -> str:
    """`jishi.lock` 放哪：跟 `.jishi/packages/` 一起（安装是全局的，lock 也全局）。

    ⚠️ 没放当前目录：现在 `安装` 装到的是**全局** `.jishi/packages/`，
    在项目里放一个 lock 会让人以为「这个项目独立的依赖环境」—— 它并不是。
    等将来有项目级依赖了再谈。
    """
    import os

    from . import packages as PKG
    # `ensure_root()` 就是 `install_package_zip` 用的那个安装根 ⇒ lock 跟它同级
    return os.path.join(os.path.dirname(PKG.ensure_root()), "包.lock")


def _write_lock(lock: dict) -> None:
    import os

    from . import packages as PKG
    path = _lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(PKG.lock_to_text(lock))


def _install_from_index(name: str, index_url: str, *, force: bool = False) -> None:
    """从**指定**索引拉包（`--index` / `--from-index`）。"""
    _install_from_sources(name, (index_url,), force=force)


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
        import oracle.jishi as _jishi
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

    # 7. 包索引可达性（M49a）：`jishi 安装` 能不能用，全看这一步
    if getattr(args, "no_net", False):
        print("  包索引：跳过联网检查（--no-net）")
    else:
        from . import packages as PKG
        urls = PKG.default_index_urls()
        good = 0
        for i, url in enumerate(urls, 1):
            try:
                pkgs = PKG.parse_index(_read_index_text(url))["包"]
                good += 1
                print(f"  索引源 {i}：可达 [OK]（{len(pkgs)} 个包）")
            except Exception as e:  # noqa: BLE001
                # ⚠️ 单个源不通**不算环境异常**：备用源在大陆常常不通，
                # 只要有一个源可达，`jishi 安装` 就能用 —— 只是别把「警告」
                # 报成「异常」，否则用户会去修一个本来就不需要修的东西。
                print(f"  索引源 {i}：不可达 [警告]（{type(e).__name__}: {e}）")
            print(f"      {url}")
        if not good:
            ok = False
            print("    建议：检查网络 / 代理；或用 `--index <索引地址>` 指定别的源")
        elif good < len(urls):
            print(f"    提示：{good}/{len(urls)} 个源可用 —— 主源不通时安装会变慢"
                  "（会先等一个超时再去试下一个）")

    print()
    if ok:
        print("[OK] 环境正常，可以开始写基石代码了。")
        return 0
    print("[警告] 发现环境问题，请按上方建议修复。")
    return 1


def _cmd_test(args) -> int:
    """跑目录下的 `*_test.jsh`（M126）。

    ## 和 `测试` 模块的分工

    断言和用例的**执行**早就有了 —— `测试` 模块（R7.1-c+，11 个函数：
    `用例` / `相等` / `为真` / `近似` / `包含` / `抛出` / `统计` / `汇总` …）。
    它解决「一个文件里跑多个用例、互不干扰、最后汇总」。

    **这里补的是另一半：不用人记文件名。** 以前要一个个 `jishi xxx_test.jsh`
    跑，跑十个文件敲十次；文件多了就没人跑了。现在扫一遍全跑，一次给总览。

    ## 为什么让每个文件自己打结果

    `测试.汇总()` 的输出（`✅ 全部通过（3 个用例）` / `✗ 用例名 · 说明：期望…`）
    已经**比这里能拼的更详细**（它知道每个用例的名字）。再包一层拼一遍，
    只会把同一件事说两次、还可能说得更差。
    ⇒ 只负责**发现 + 调度 + 总览**，明细交给该报的人报。

    ## 两个必须区分的失败

    - **文件里的断言没通过** ⇒ `测试.汇总()` 抛 `断言错误`（E2008）⇒ 记「测试没过」；
    - **文件自己崩了**（别的错）⇒ 那是**程序写错或语言的 bug**，不能算「测试失败」。
      混在一起用户会以为自己的断言写错了。
    """
    from pathlib import Path

    根 = Path(getattr(args, "path", ".") or ".")
    if not 根.is_dir():
        print(f"❌ 找不到目录：{根}", file=sys.stderr)
        return 2

    文件们 = [f for f in sorted(根.rglob("*_test.jsh"))
              if ".jishi" not in f.parts]           # 别扫装好的第三方包
    if not 文件们:
        print(f"没找到 *_test.jsh（在 {根} 下）—— 测试文件名要以 `_test.jsh` 结尾。",
              file=sys.stderr)
        return 2

    过, 断言败, 崩了 = 0, 0, 0

    for f in 文件们:
        相对 = f.relative_to(根) if 根 != Path(".") else f
        print(f"\n{相对}")
        行们: list[str] = []
        try:
            # ⚠️ 不能走 `run_file`：它把 `JishiError` 渲染完就 `return 1`，
            #    异常冒不上来，这里就只能见到退出码、分不清「断言没过」和「崩了」。
            _源码, _名 = _read_source(str(f))
            行们 = _源码.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            _程序 = parse_source(_源码, _名)
            Interpreter(filename=_名).run(_程序)
        except JishiError as e:
            e.with_source(行们)
            if e.code == "E2008":
                断言败 += 1
                print("  ↑ 上面是用例的失败明细；本文件有断言没通过")
            else:
                崩了 += 1
                print(f"  ⚠️ 不是断言失败，是程序出错了（{e.code} {e.title}）")
                print(f"     {e.message}" + (f"（第 {e.line} 行）" if e.line else ""))
        except Exception as e:                      # noqa: BLE001
            崩了 += 1
            print(f"  ⚠️ 不是断言失败，是宿主异常：{type(e).__name__}: {e}")
        else:
            过 += 1

    总 = len(文件们)
    print("\n" + "=" * 48)
    print(f"汇总：{过}/{总} 个测试文件通过"
          + (f"，{断言败} 个有断言没通过" if 断言败 else "")
          + (f"，{崩了} 个程序出错" if 崩了 else ""))
    return 0 if (断言败 == 0 and 崩了 == 0) else 1


# ---------------------------------------------------------------------------
# M137：`自测` —— 用「固定版」跑基石自带的黑盒运行器
# ---------------------------------------------------------------------------

#: 固定版记在哪：`JISHI_HOME`（默认 `~/.jishi`）下的两个纯文本文件，**一行一个词元**。
#:
#: 🔴 **为什么不用 JSON**（这是刻意的）：
#: ① Rust 侧没有 serde_json —— 加第三方依赖要用户拍板，不能为一个小配置破例；
#: ② Windows 路径里的反斜杠在 JSON 里**必须转义**，手写读写最容易在这种地方出错
#:    （`C:\a` 会被当成 `\a` 转义）。而「一行一个词元」**没有转义问题**
#:    （路径里不可能有换行），两边十几行就能读写，也不会漂。
_自测_运行者文件 = "自测-运行者.txt"
_自测_被测文件 = "自测-被测.txt"


def _jishi_home() -> "Path":
    """基石的用户目录：`JISHI_HOME`，没有就 `~/.jishi`。

    ⚠️ **不跟「猜安装目录」那一套**（`rust/src/selfupdate.rs::猜安装目录` 会拐去
    Inno 的安装目录）：固定版是**开发机的偏好**，不该写进安装目录 ——
    一来升级会把它冲掉，二来 Inno 目录未必可写。⇒ 这里只认这两条。
    """
    from pathlib import Path

    指定 = os.environ.get("JISHI_HOME", "").strip()
    return Path(指定) if 指定 else Path.home() / ".jishi"


def _读命令(文件名: str) -> "list[str]":
    """读一份记下来的命令（一行一个词元）；没记过就是空列表。"""
    路径 = _jishi_home() / 文件名
    if not 路径.is_file():
        return []
    文 = 路径.read_text(encoding="utf-8", errors="replace")
    return [行[:-1] if 行.endswith("\r") else 行
            for 行 in 文.split("\n") if 行.strip()]


def _写命令(文件名: str, 命令: "list[str]") -> None:
    目录 = _jishi_home()
    目录.mkdir(parents=True, exist_ok=True)
    (目录 / 文件名).write_text("\n".join(命令) + "\n", encoding="utf-8", newline="\n")


def _删命令(文件名: str) -> bool:
    路径 = _jishi_home() / 文件名
    if 路径.is_file():
        路径.unlink()
        return True
    return False


def _自己命令() -> "list[str]":
    """「当前这个 jishi」怎么被调起来。

    - PyInstaller 打的单文件版 ⇒ 就是它自己；
    - 源码 / 装好的包 ⇒ **`python -m oracle.jishi.cli`**。
      ⚠️ 不能用 `sys.argv[0]`：那可能只是 `jishi` 这个启动器，或者干脆是
      pytest 的 argv（判据里就是这种情况）—— 拿它当命令会得到一条跑不起来的命令行。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "oracle.jishi.cli"]


def _定位命令(命令: "list[str]", 谁: str) -> "tuple[list[str], str]":
    """把命令里的「程序」定成绝对路径。返回 `(命令, 错误说明)`，错误说明空 = 没问题。

    🔴 **找不到就报错，绝不悄悄退回「当前版」**：固定版的意义就是
    「跑尺子的」和「被量的」是**两个**版本；悄悄退回去，自测就变成自欺 ——
    而且它会是**绿的**（拿自己考自己当然过），这比红更糟。
    """
    from pathlib import Path
    import shutil as _shutil

    首 = 命令[0]
    候选 = Path(首)
    if 候选.is_file():
        # ⚠️ 只**绝对化**，不 `resolve()`（不跟符号链接）：与 Rust 侧 `path::absolute`
        #    同一口径。跟了链接会让「钉住的到底是哪个文件」变得不可预期 ——
        #    macOS 上 `bin/python` 是链接，一跟就变成 `python3.13`（M135 的标签判据踩过）。
        return [os.path.abspath(首)] + 命令[1:], ""
    # 不像路径（没有分隔符、也没有 Windows 可执行后缀）⇒ 当命令名去 PATH 找
    if "/" not in 首 and "\\" not in 首 and not 首.lower().endswith((".exe", ".cmd", ".bat")):
        找到 = _shutil.which(首)
        if 找到:
            return [找到] + 命令[1:], ""
    return 命令, f"{谁}找不到这个程序：{首}"


def _取参数(原始: "list[str]"):
    """手工解析 `自测` 的参数。

    ⚠️ **不用 argparse**：`--被测 python -m oracle.jishi.cli` 里那个 `-m`
    是**被测命令的一部分**，argparse 会把它当成自己的选项咬掉。
    这里按「遇到 `--xxx` 才收手」的规则贪心取词元，和 `tools/自测.jsh` 同一套规则。
    """
    固定版 = 被测 = 运行器 = None
    只看 = 忘掉 = 帮助 = False
    透传: "list[str]" = []
    i = 0
    while i < len(原始):
        a = 原始[i]
        if a in ("--固定版", "--被测"):
            i += 1
            值 = []
            while i < len(原始) and not 原始[i].startswith("--"):
                值.append(原始[i])
                i += 1
            if not 值:
                return None, f"「{a}」后面要跟一个命令（比如 {a} C:/jishi/bin/jishi.exe）"
            if a == "--固定版":
                固定版 = 值
            else:
                被测 = 值
        elif a == "--运行器":
            i += 1
            if i >= len(原始):
                return None, "「--运行器」后面要跟一个 .jsh 路径"
            运行器 = 原始[i]
            i += 1
        elif a == "--看":
            只看 = True
            i += 1
        elif a == "--忘掉":
            忘掉 = True
            i += 1
        elif a in ("--帮助", "-h"):
            帮助 = True
            i += 1
        else:
            透传 = 原始[i:]
            break
    return {"固定版": 固定版, "被测": 被测, "运行器": 运行器,
            "只看": 只看, "忘掉": 忘掉, "帮助": 帮助, "透传": 透传}, ""


def _命令文本(命令: "list[str]") -> str:
    return " ".join(命令)


def _cmd_selfrun(原始: "list[str]") -> int:
    """`jishi 自测`：**用「固定版」跑基石自带的黑盒运行器**（M137）。

    ## 它补的是「自举」那一环

    0.2.8 要做的正是「用基石测基石」。但这里有个先有鸡还是先有蛋的问题：
    **新构建的基石要是坏了（连解析都不行），拿它去跑测试就什么都测不出来** ——
    你只会看到一次启动失败，看不出「到底哪 17 条用例挂了」。

    ⇒ 把两件事**分开记住**：
    - **运行者**（固定版）：谁去跑 `tools/自测.jsh` 这把**尺子**；
    - **被测**：谁被量（默认就是你现在手上这个 jishi）。

    尺子用上一版、被量的用新版 —— 新版坏成什么样，尺子都还能把结论说出来。

    ## 用法

    ```bash
    jishi 自测 --固定版 C:/Program\\ Files/jishi/bin/jishi.exe   # 记住并立刻跑一次
    jishi 自测                                                  # 以后就这么跑
    jishi 自测 --被测 rust/target/release/jishi-rs.exe           # 换被测（也会记住）
    jishi 自测 --看                                             # 看现在记着什么
    jishi 自测 --忘掉                                           # 清掉
    jishi 自测 --用例 tests/cases --详细 --路数 8                # 其余参数透传给运行器
    ```

    ## 和 `jishi 测试` 的区别（别混）

    - `jishi 测试` → 跑**你自己项目**里的 `*_test.jsh`（用户视角）；
    - `jishi 自测` → 跑**基石自己**的黑盒运行器（开发基石时用，带固定版）。
    """
    from pathlib import Path
    import subprocess

    参, 错 = _取参数(list(原始))
    if 错:
        print(错, file=sys.stderr)
        return 2
    if 参["帮助"]:
        print("用法：jishi 自测 [--固定版 <命令…>] [--被测 <命令…>] "
              "[--运行器 <路径>] [--看] [--忘掉] [其余参数透传给运行器]")
        print("  与 `jishi 测试` 的区别：`测试` 跑你自己项目里的 *_test.jsh；"
              "`自测` 跑基石自己的 tools/自测.jsh（开发基石时用，带固定版）。")
        return 0

    if 参["忘掉"]:
        删了 = [_删命令(_自测_运行者文件), _删命令(_自测_被测文件)]
        print("已忘掉固定版。" if any(删了) else "本来就没记着什么，不用忘。")
        return 0

    记住的运行者 = _读命令(_自测_运行者文件)
    记住的被测 = _读命令(_自测_被测文件)
    # ⚠️ **显示用原字符串、判存在才用 Path**：Windows 上 `Path("tools/自测.jsh")`
    #    的 `str()` 会归一成 `tools\自测.jsh`，而用户敲的是 `/` —— 两边显示不一致、
    #    与 Rust 侧（保留输入）也对不上。⇒ 谁输入什么就显示什么。
    运行器文本 = 参["运行器"] or "tools/自测.jsh"
    运行器 = Path(运行器文本)

    # 🔴 `--看` **只看不写**：它是「我有记着什么吗」，不是「改成这些」。
    if 参["只看"]:
        运行者 = 记住的运行者 or _自己命令()
        被测 = 记住的被测 or _自己命令()
        print("自测 · 现在记着：")
        print(f"   运行者：{_命令文本(运行者)}"
              + ("" if 记住的运行者 else "（没记固定版 ⇒ 用当前这个 jishi）"))
        print(f"   被测：{_命令文本(被测)}"
              + ("" if 记住的被测 else "（没指定 ⇒ 用当前这个 jishi）"))
        print(f"   运行器：{运行器文本}"
              + ("" if 运行器.is_file() else " ⚠️ 不存在"))
        return 0

    if 参["固定版"]:
        命令, 错 = _定位命令(参["固定版"], "固定版（运行者）")
        if 错:
            print(f"❌ {错}", file=sys.stderr)
            return 2
        _写命令(_自测_运行者文件, 命令)
        参["固定版"] = 命令
    if 参["被测"]:
        命令, 错 = _定位命令(参["被测"], "被测")
        if 错:
            print(f"❌ {错}", file=sys.stderr)
            return 2
        _写命令(_自测_被测文件, 命令)
        参["被测"] = 命令

    运行者 = 参["固定版"] or 记住的运行者 or _自己命令()
    运行者来源 = "固定版" if (参["固定版"] or 记住的运行者) else "当前"
    被测 = 参["被测"] or 记住的被测 or _自己命令()
    被测来源 = "指定" if (参["被测"] or 记住的被测) else "当前"

    if not 运行器.is_file():
        print(f"❌ 找不到运行器脚本：{运行器文本}", file=sys.stderr)
        print("   在仓库根跑，或者用 --运行器 <另一个 .jsh> 指定。", file=sys.stderr)
        return 2

    print(f"自测 · 运行者：{_命令文本(运行者)}（{运行者来源}）")
    print(f"       被测：{_命令文本(被测)}（{被测来源}）")
    print(f"       运行器：{运行器文本}")
    sys.stdout.flush()          # 必须在起子进程之前刷（否则管道里顺序会倒，M135 踩过）

    命令行 = list(运行者) + [运行器文本, "--被测"] + list(被测) + list(参["透传"])
    try:
        码 = subprocess.run(命令行).returncode
    except FileNotFoundError:
        print(f"❌ 起不来：{_命令文本(运行者)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:                  # pragma: no cover - 人手 Ctrl-C
        return 130
    return 码


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


def _all_error_codes() -> list:
    """从 `errors.py` 抽 `(码, 标题)`（**单一来源在那里**），按码排序。"""
    import inspect

    from . import errors as E

    out: dict = {}
    for obj in vars(E).values():
        if (inspect.isclass(obj) and issubclass(obj, E.JishiError)
                and obj is not E.JishiError):
            out.setdefault(obj.code, obj.title)
    return sorted(out.items())


def _cmd_errcode(args) -> int:
    """`jishi 错误 [码]`：查错误码的含义 / 常见成因 / 怎么改（M56 第 7 条）。

    码与标题从 `errors.py` **自动抽**（不另抄一份）；说明在 `oracle/jishi/errcodes.py`
    —— 两边不许有对方没有的码，`tests/test_m56_errcodes.py` 钉着。
    """
    from . import errcodes

    codes = _all_error_codes()
    want = (args.code or "").strip().upper()

    if not want:
        print(f"基石错误码总览（{len(codes)} 个）\n")
        for prefix, name, desc in errcodes.SECTIONS:
            group = [(c, t) for c, t in codes if c.startswith(prefix)]
            if not group:
                continue
            print(f"【{prefix}xx · {name}】{desc}")
            for code, title in group:
                print(f"  {code}  {title}")
            print()
        print("看某个码的详情：jishi 错误 E0301")
        print("（报错里给的那串 `E0301` 就是可以直接贴进来的码）")
        return 0

    # 允许只写数字（用户从报错里抄的时候常常只抄了 0301）
    if not want.startswith("E"):
        want = "E" + want
    if want[1:].isdigit() and len(want) < 5:
        want = "E" + want[1:].zfill(4)      # E301 → E0301
    title = dict(codes).get(want)
    if title is None:
        print(f"没有错误码「{want}」—— 跑 `jishi 错误` 看全部 {len(codes)} 个。",
              file=sys.stderr)
        return 2

    info = errcodes.INFO.get(want)
    print(f"{want}  {title}\n")
    if info is None:                                            # pragma: no cover
        print("（这个码还没有写说明 —— 请补 oracle/jishi/errcodes.py）")
        return 0
    print("什么意思")
    print(f"  {info['what']}\n")
    print("常见成因")
    for reason in info["why"]:
        print(f"  · {reason}")
    print()
    print("怎么改")
    print(f"  {info['fix']}")
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
        program = parse_source(source, filename)
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
        program = parse_source(source, filename)
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
    p_install.add_argument("--锁定", "--locked", dest="锁定", action="store_true",
                           help="严格按 .jishi/包.lock 装（索引变了也不变）")
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
    """工具子命令分发：jishi 医生/新项目/教程（M16）。

    `自测`（M137）**不走 argparse**，见下面的说明。
    """
    # 🔴 `自测` 的参数里可能带**别的程序的命令行**：`--被测 python -m oracle.jishi.cli`
    #    那个 `-m` 是**值**，但 argparse 会把它当自己的选项咬掉（`nargs='+'` 也只认到
    #    下一个以 `-` 开头的词）。⇒ 这条子命令自己解析，规则与 `tools/自测.jsh` 一致
    #    （「遇到 `--xxx` 才收手」）。
    if argv and argv[0] == "自测":
        return _cmd_selfrun(list(argv[1:]))

    parser = argparse.ArgumentParser(
        prog="jishi", description="基石工具")
    sub = parser.add_subparsers(dest="subcommand", metavar="命令", required=True)

    p_doctor = sub.add_parser("医生", help="环境自检（安装位置/版本/C VM/编码/MCP/包索引）")
    p_doctor.add_argument("--no-net", action="store_true",
                          help="跳过联网检查（包索引可达性）")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_test = sub.add_parser("测试", help="跑目录下的 *_test.jsh（M126）")
    p_test.add_argument("path", nargs="?", default=".", help="从哪个目录找（默认当前目录）")
    p_test.add_argument("--详细", action="store_true",
                        help="每个文件的输出和完整错误位置都打出来")
    p_test.set_defaults(func=_cmd_test)

    p_new = sub.add_parser("新项目", help="生成脚手架项目")
    p_new.add_argument("name", help="项目名")
    p_new.set_defaults(func=_cmd_new_project)

    sub.add_parser("教程", help="打印本地教程索引").set_defaults(
        func=_cmd_tutorial)

    p_err = sub.add_parser("错误", help="查错误码：什么意思/常见成因/怎么改（M56）")
    p_err.add_argument("code", nargs="?", metavar="码",
                       help="错误码（如 E0301）；不写则列出全部")
    p_err.set_defaults(func=_cmd_errcode)

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

    包成函数是因为入口有两条：`python -m oracle.jishi.cli` 直接调 `main()`，
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
    _装好错误流()


class _先冲stdout的错误流:
    """往 stderr 写字**之前**，先把 stdout 冲掉。

    🔴 为什么需要（2026-10-25，M135 用基石写测试运行器时撞出来的）：

    运行器先 `打印` 一整份报告，最后 `抛出` 表示失败。Python 的 stdout 在
    **重定向 / 管道 / `capture_output`** 时是**块缓冲**（不是行缓冲）⇒
    错误横幅先出去、报告留在缓冲里最后才出去，顺序倒了。
    而 **Rust / Node 宿主顺序是正的**（它们先刷再写）。

    ⇒ **同一份程序在两个宿主上输出顺序不同，那就是语义分叉**，不是「显示问题」：
    任何「先打印再报错」的程序（测试运行器、批处理、安装进度）都会看见它。

    修在这里（代理 stderr）而不是逐个改 `print(..., file=sys.stderr)`：
    一处覆盖全部写点 —— 包括标准库自己打的提示、以及 Python 的 traceback。
    """

    def __init__(self, 上游):
        self._上游 = 上游

    def write(self, s):
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 —— 管道关了/没 stdout 时不该因刷新而炸
            pass
        return self._上游.write(s)

    def flush(self):
        return self._上游.flush()

    def __getattr__(self, 名):  # isatty / encoding / fileno …全部透传
        return getattr(self._上游, 名)


def _装好错误流() -> None:
    """给 stderr 套上「先冲 stdout」的代理（只套一次；见上面那个类的说明）。

    🔴 **只给「真正的 stderr」套**（2026-10-25 补的）：这个代理是给**真 CLI 运行**用的。
    如果 `sys.stderr` 已经被别人换过（pytest 的 `capsys`、`contextlib.redirect_stderr`、
    IDE 的控制台包装…），套上去就变成**替那个人保管一个引用** —— 那个对象在它自己的
    作用域结束时被关掉，之后所有 `print(file=sys.stderr)` 都会走进一个死流子里，
    **消息静默消失**（CI 上就出现了「退出码 1、两条流都读到空串」这种现场）。

    ⇒ 判据很简单：`sys.stderr is sys.__stderr__` 才套 —— 那时我们拥有真的 stderr，
    写进去就是用户眼前那个终端（或者它被重定向到的文件）。
    """
    if isinstance(sys.stderr, _先冲stdout的错误流):
        return
    if sys.stderr is not sys.__stderr__:
        return
    if sys.stderr is None or not hasattr(sys.stderr, "write"):
        return
    sys.stderr = _先冲stdout的错误流(sys.stderr)


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
    if argv and argv[0] in ("医生", "新项目", "教程", "错误", "格式化", "lsp", "调试",
                            "dap", "源码静态检查", "静态检查", "测试", "自测"):
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
    parser.add_argument("--ai-card", nargs="?", const="", default=None,
                        metavar="模块",
                        help="输出 AI 语言卡（贴进 LLM 系统提示词，M8.1）。"
                             "给模块名则只出这个模块的分片卡（如 --ai-card 文本）；"
                             "配 --任务 出「按任务打包」的分片卡；配 --统计 只看大小（M116）")
    parser.add_argument("--任务", default=None, metavar="名字",
                        help="配 --ai-card 用：按任务打包（表格/文本/文件/网络/数据/并发/测试）")
    parser.add_argument("--统计", action="store_true",
                        help="配 --ai-card 用：只打印各种卡片的大小对比，不打印卡片")
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

    if args.ai_card is not None:
        from .ai import 卡统计, render_ai_card
        if args.统计:
            for 名, 大小 in 卡统计().items():
                print(f"{名}\t{大小}")
            return 0
        模块 = args.ai_card.strip() or None
        if 模块 is not None and 模块.endswith(".jsh"):
            # ⚠️ 防手滑：`--ai-card 脚本.jsh` 会被 argparse 吃成「模块名」，
            #    必须**当场说清楚**，不能静默报「没有这个模块」让人以为卡坏了
            print(f"--ai-card 后面跟的是**模块名**；要跑脚本请写 "
                  f"`jishi {模块}`（或者把 --ai-card 放到脚本名后面）", file=sys.stderr)
            return 2
        if 模块 is None and args.任务 is None and args.file is not None:
            # 同理：`jishi --ai-card 脚本.jsh` 里那个位置被吃掉了，脚本没得跑
            print(f"--ai-card 后面跟的是**模块名**（你给的是「{args.file}」）。"
                  f"只想打印整张卡就写 `jishi --ai-card`；要跑脚本写 `jishi {args.file}`",
                  file=sys.stderr)
            return 2
        try:
            print(render_ai_card(模块=模块, 任务=args.任务))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
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
