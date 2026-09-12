# -*- coding: utf-8 -*-
"""基石标准库：系统。薄封装 Python 的 os/subprocess，中文 API。

提供环境变量、执行外部命令、系统信息。零第三方依赖。
注意：涉及系统访问，沙箱默认拦截（见 sandbox.py 白名单）。
"""

from __future__ import annotations

import os as _os
import platform as _py_platform
import subprocess as _py_subprocess

from ..errors import RunError, RunValueError

__all__ = [
    "系统名", "系统版本", "机器架构", "处理器",
    "当前目录", "改目录", "环境变量", "设环境变量",
    "执行", "退出码", "主机名", "参数",
]


def 系统名() -> str:
    """返回操作系统名：Windows / Linux / Darwin。"""
    return _py_platform.system()


def 系统版本() -> str:
    """返回操作系统版本描述。"""
    return _py_platform.version()


def 机器架构() -> str:
    """返回机器架构，如 AMD64。"""
    return _py_platform.machine()


def 处理器() -> str:
    """返回处理器描述。"""
    return _py_platform.processor() or "未知"


def 当前目录() -> str:
    """返回当前工作目录。"""
    return _os.getcwd()


def 改目录(路径) -> str:
    """切换当前工作目录，返回切换后的目录。"""
    p = str(路径)
    if not _os.path.isdir(p):
        raise RunValueError(f"「{p}」不是目录")
    _os.chdir(p)
    return _os.getcwd()


def 环境变量(名字, 默认: str = "") -> str:
    """读取环境变量；不存在时返回默认值。"""
    return _os.environ.get(str(名字), 默认)


def 设环境变量(名字, 值) -> None:
    """设置一个环境变量。"""
    _os.environ[str(名字)] = str(值)


def 执行(命令, 超时: float = 10.0) -> str:
    """执行一条外部命令，返回标准输出（合并 stdout+stderr）。

    超时（秒）内未结束会抛中文错误。命令建议用列表传入（如 ["dir"]），
    也可用文本（按 shell 解析，注意跨平台差异）。
    """
    try:
        if isinstance(命令, (list, tuple)):
            r = _py_subprocess.run(
                [str(x) for x in 命令], capture_output=True,
                timeout=max(0.1, float(超时)), text=True,
                encoding="utf-8", errors="replace")
        else:
            r = _py_subprocess.run(
                str(命令), capture_output=True, shell=True,
                timeout=max(0.1, float(超时)), text=True,
                encoding="utf-8", errors="replace")
    except _py_subprocess.TimeoutExpired:
        raise RunError(f"命令执行超过 {超时} 秒，已终止")
    except OSError as e:
        raise RunError(f"执行命令失败：{e}")
    out = (r.stdout or "") + (r.stderr or "")
    return out


def 退出码(命令, 超时: float = 10.0) -> int:
    """执行命令并只返回退出码（0 表示成功）。"""
    try:
        if isinstance(命令, (list, tuple)):
            r = _py_subprocess.run(
                [str(x) for x in 命令], capture_output=True,
                timeout=max(0.1, float(超时)))
        else:
            r = _py_subprocess.run(
                str(命令), capture_output=True, shell=True,
                timeout=max(0.1, float(超时)))
    except _py_subprocess.TimeoutExpired:
        raise RunError(f"命令执行超过 {超时} 秒，已终止")
    except OSError as e:
        raise RunError(f"执行命令失败：{e}")
    return r.returncode


def 主机名() -> str:
    """返回当前机器的主机名。"""
    import socket
    return socket.gethostname()


# ---------------------------------------------------------------------------
# 命令行参数（M18.2）
# ---------------------------------------------------------------------------

#: 脚本收到的命令行参数（jishi 脚本.jsh 参数1 参数2），由 CLI 在运行前设置。
#: 存为「参数列表的副本」，避免调用方后续修改影响。
_参数: list = []


def 参数() -> list:
    """返回脚本收到的命令行参数（不含脚本名本身）。

    例如 `jishi 分析.jsh 成绩.csv --排序 分数`，脚本里 `系统.参数()`
    返回 `["成绩.csv", "--排序", "分数"]`。
    """
    return list(_参数)


def _设参数(args) -> None:
    """内部：CLI 在运行脚本前设置命令行参数（M18.2）。"""
    global _参数
    _参数 = list(args)
