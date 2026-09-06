# -*- coding: utf-8 -*-
"""M9.1 沙箱运行器：给基石脚本加资源上限与危险操作拦截。

面向「被大模型调用」的场景——LLM 生成的代码可能死循环、刷屏、写文件、
连网络，沙箱把这些都挡在安全边界内。

- 超时（默认 5 秒，threading 实现；超时后返回错误，泄漏线程随进程回收）
- stdout 上限（默认 10KB，超限报错）
- 模块白名单：标准库默认只放行无副作用的 随机/数学/文本/时间；
  文件/表格 需 allow_write；Python 桥接默认禁，开启后仍禁网络/危险模块
- 递归深度：依赖 Python 默认 RecursionError（已翻译成中文错误）
"""

from __future__ import annotations

import io
import sys
import threading
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from typing import Any, Optional

from . import runtime
from .errors import JishiError, RunError, translate_python_exception
from .protocol import failure, success


@dataclass
class SandboxOpts:
    """沙箱选项。"""
    timeout: float = 5.0                 # 执行超时（秒）
    max_stdout: int = 10_000             # stdout 上限（字符）
    allow_write: bool = False            # 允许文件写（导入 文件/表格）
    allow_network: bool = False          # 允许网络（Python 桥接的网络模块）
    allow_python_import: bool = False    # 允许 Python 桥接导入
    allow_modules: frozenset = field(    # 标准库白名单（无副作用模块恒允许）
        default_factory=lambda: frozenset(
            {"随机", "数学", "文本", "时间", "json", "日期", "正则", "加密"}))
    extra_stdlib: frozenset = field(default_factory=frozenset)


#: Python 桥接中禁止的「网络/危险」模块（沙箱默认拦截）
_BLOCKED_PYTHON_MODULES = frozenset({
    "socket", "urllib", "urllib2", "urllib3", "http", "requests",
    "httplib", "httplib2", "ftplib", "smtplib", "telnetlib",
    "subprocess", "os", "sys", "shutil", "ctypes", "pathlib",
    "email", "imaplib", "poplib",
})


class _LimitedStringIO(io.StringIO):
    """带上限的 stdout buffer，超出上限即抛错。"""

    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, s: str) -> int:
        if self.tell() + len(s) > self.limit:
            raise RunError(f"输出超过上限（{self.limit} 字符）")
        return super().write(s)


def _make_guard(opts: SandboxOpts):
    """构造导入守卫回调：按 opts 放行/拦截模块。"""
    def guard(name, from_python, from_local, line, col, filename):
        if from_local:
            # 本地包导入涉及文件系统读取，沙箱默认拦截
            if not opts.allow_write:
                raise RunError(
                    f"沙箱禁止「从 本地包」导入（涉及文件访问）",
                    line=line, col=col, filename=filename,
                    hint="如需本地包，请显式开启 allow_write")
            return
        if from_python:
            if not opts.allow_python_import:
                raise RunError(
                    "沙箱禁止「从 python」导入模块",
                    line=line, col=col, filename=filename,
                    hint="如需 Python 桥接，请显式开启 allow_python_import")
            root = name.split(".")[0]
            if root in _BLOCKED_PYTHON_MODULES and not opts.allow_network:
                raise RunError(
                    f"沙箱禁止导入模块「{name}」（网络/系统访问被拦截）",
                    line=line, col=col, filename=filename)
        else:
            if name in ("文件", "表格"):
                if not opts.allow_write:
                    raise RunError(
                        f"沙箱禁止导入「{name}」（涉及文件读写）",
                        line=line, col=col, filename=filename,
                        hint="如需文件读写，请显式开启 allow_write")
                return   # 允许文件读写
            if name in ("路径", "压缩"):
                if not opts.allow_write:
                    raise RunError(
                        f"沙箱禁止导入「{name}」（涉及文件系统访问）",
                        line=line, col=col, filename=filename,
                        hint="如需文件系统访问，请显式开启 allow_write")
                return
            if name == "网络":
                if not opts.allow_network:
                    raise RunError(
                        "沙箱禁止导入「网络」（涉及网络访问）",
                        line=line, col=col, filename=filename,
                        hint="如需网络访问，请显式开启 allow_network")
                return
            if name == "系统":
                if not opts.allow_network:
                    raise RunError(
                        "沙箱禁止导入「系统」（涉及执行外部命令/系统访问）",
                        line=line, col=col, filename=filename,
                        hint="如需系统访问，请显式开启 allow_network")
                return
            if (name not in opts.allow_modules
                    and name not in opts.extra_stdlib):
                raise RunError(
                    f"沙箱不允许导入模块「{name}」",
                    line=line, col=col, filename=filename)
    return guard


def run_sandboxed(source: str, opts: Optional[SandboxOpts] = None,
                  filename: str = "<输入>") -> dict:
    """在沙箱里执行基石源码，返回 protocol 结果 dict。

    每次调用独立执行（无状态残留）；导入守卫/超时/stdout 上限均在此生效。
    """
    from .interpreter import run_source

    opts = opts or SandboxOpts()
    start = time.perf_counter()
    buf = _LimitedStringIO(opts.max_stdout)
    outcome: dict = {}

    def target():
        try:
            with redirect_stdout(buf):
                run_source(source, filename)
            outcome["stdout"] = buf.getvalue()
        except BaseException as e:  # noqa: BLE001 —— 捕获一切，交给主线程判断
            outcome["error"] = e

    prev_guard = runtime._IMPORT_GUARD
    runtime._IMPORT_GUARD = _make_guard(opts)
    try:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(opts.timeout)
        duration = (time.perf_counter() - start) * 1000.0

        if t.is_alive():
            # 超时：线程仍在跑（无法强制终止），放弃结果，靠进程回收线程
            return failure(
                RunError(
                    f"执行超时（{opts.timeout} 秒）",
                    hint="可能是有死循环，检查循环条件或递归结束条件"),
                duration)

        if "error" in outcome:
            err = outcome["error"]
            if isinstance(err, JishiError):
                return failure(err, duration)
            # 非基石错误（如 RecursionError）→ 走中文翻译（M17.1）
            return failure(translate_python_exception(err, filename=filename),
                           duration)

        return success(stdout=outcome.get("stdout", ""),
                       duration_ms=duration)
    finally:
        runtime._IMPORT_GUARD = prev_guard


def eval_sandboxed(expr: str, opts: Optional[SandboxOpts] = None,
                   filename: str = "<输入>") -> dict:
    """在沙箱里求值一个表达式，返回 protocol 结果（value 为表达式结果）。"""
    from .interpreter import Interpreter
    from .parser import parse
    from .tokenizer import tokenize

    opts = opts or SandboxOpts()
    start = time.perf_counter()
    buf = _LimitedStringIO(opts.max_stdout)
    outcome: dict = {}

    def target():
        try:
            with redirect_stdout(buf):
                tokens = tokenize(expr, filename)
                lines = expr.replace("\r\n", "\n").split("\n")
                program = parse(tokens, lines, filename)
                interp = Interpreter(filename=filename)
                value = None
                for stmt in program.body:
                    value = interp.exec_stmt(stmt, interp.globals)
                # 表达式语句的值在 _last_value；这里取最后一个表达式值
                outcome["value"] = getattr(interp, "_last_value", value)
            outcome["stdout"] = buf.getvalue()
        except BaseException as e:
            outcome["error"] = e

    prev_guard = runtime._IMPORT_GUARD
    runtime._IMPORT_GUARD = _make_guard(opts)
    try:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(opts.timeout)
        duration = (time.perf_counter() - start) * 1000.0

        if t.is_alive():
            return failure(
                RunError(f"执行超时（{opts.timeout} 秒）",
                         hint="可能是有死循环，检查循环条件或递归结束条件"),
                duration)
        if "error" in outcome:
            err = outcome["error"]
            if isinstance(err, JishiError):
                return failure(err, duration)
            return failure(translate_python_exception(err, filename=filename),
                           duration)

        return success(stdout=outcome.get("stdout", ""),
                       value=outcome.get("value"),
                       duration_ms=duration)
    finally:
        runtime._IMPORT_GUARD = prev_guard
