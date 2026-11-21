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
    "执行", "执行详细", "并行执行", "退出码", "打开", "主机名", "参数",
    "CPU核数",
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
        # 按**字节**收再显式解码（M33）：`text=True` 会做通用换行转换，
        # 把命令输出里的 `\r\n` 悄悄改成 `\n`——而 Node / Rust 宿主不转，
        # 于是同一份程序在三个宿主上输出不同。这里只做解码，不碰换行。
        if isinstance(命令, (list, tuple)):
            r = _py_subprocess.run(
                [str(x) for x in 命令], capture_output=True,
                timeout=max(0.1, float(超时)))
        else:
            r = _py_subprocess.run(
                str(命令), capture_output=True, shell=True,
                timeout=max(0.1, float(超时)))
    except _py_subprocess.TimeoutExpired:
        raise RunError(f"命令执行超过 {超时:g} 秒，已终止")
    except OSError as e:
        raise RunError(f"执行命令失败：{e}")
    out = ((r.stdout or b"").decode("utf-8", "replace")
           + (r.stderr or b"").decode("utf-8", "replace"))
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
        raise RunError(f"命令执行超过 {超时:g} 秒，已终止")
    except OSError as e:
        raise RunError(f"执行命令失败：{e}")
    return r.returncode


def 执行详细(命令, 超时: float = 10.0) -> dict:
    """执行一条外部命令，返回 `{"输出", "错误", "退出码"}` —— **两条流分开**。

    ## 为什么不直接用 `执行`

    `执行` 返回的是 **stdout 和 stderr 拼在一起**的一段文本（老接口，为了兼容）。
    但对拍/测试场景那不够用：**「用户看到什么」是两条流加起来，而比较时往往要分开**
    （比如「正常输出必须一字不差，但告警只要求包含某句」）。
    合并之后就只能比一半 —— 那正是 M128 修过的病。

    ## 三个字段

    - `输出`：stdout（原样，**不做换行转换**，见下面 M33 那条注释）；
    - `错误`：stderr（同上）；
    - `退出码`：进程退出码（`0` 表示成功）。

    ⚠️ **「命令跑不起来」（超时 / 找不到可执行文件）仍然抛错**，不塞进返回值：
    那是**程序写错**，不是「命令失败」。命令**跑了但失败**才算正常返回（退出码非 0）。
    """
    出, 错, 码 = _跑一次(命令, 超时)
    return {"输出": 出, "错误": 错, "退出码": 码}


def 并行执行(命令们, 路数=4, 超时: float = 10.0) -> list:
    """**同时**执行一批命令，返回 `[{输出, 错误, 退出码}, …]`，**顺序与输入一致**。

    ## 顺序为什么必须与输入一致

    与 `并发.批量读` / `批量摘要` 一条规矩（M114 定）：**结果顺序 = 输入顺序**。
    不然「第 3 条命令的结果」到底是哪一条，用户得自己数 —— 那比串行还糟。

    ## 路数

    `路数` 是**同时跑几条**（不是「一次跑完」）。`<= 1` 或平台没线程时**顺序执行**，
    结果一模一样，只是不重叠等待。上限 32（护栏：传 100000 会当场把机器打死）。

    ## 超时

    `超时` 是**每条命令各自**的上限，不是整批的总时长。
    """
    # 借用 `并发` 的两个校验：**文案三宿主已经对齐过**，自己再写一份必然分岔
    from .并发 import _列出 as _校验列表, _线程数 as _校验路数

    路数n = _校验路数(路数)
    项们 = _校验列表(命令们, "「并行执行」的第一个参数（命令们）")

    if 路数n <= 1 or len(项们) <= 1:
        return [执行详细(命令, 超时) for 命令 in 项们]

    from concurrent.futures import ThreadPoolExecutor

    # `executor.map` **按提交顺序返回**（不是按完成顺序）—— 正是要的语义
    with ThreadPoolExecutor(max_workers=路数n) as 池:
        return list(池.map(lambda c: 执行详细(c, 超时), 项们))


def _跑一次(命令, 超时: float):
    """跑一条命令，返回 `(输出, 错误, 退出码)` —— `执行` 和 `执行详细` 共用。

    ⚠️ **按字节收再显式解码**（M33）：`text=True` 会做通用换行转换，
    把命令输出里的 `\r\n` 悄悄改成 `\n` —— 而 Node / Rust 宿主不转，
    于是同一份程序在三个宿主上输出不同。这里只做解码，不碰换行。
    """
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
        raise RunError(f"命令执行超过 {超时:g} 秒，已终止")
    except OSError as e:
        raise RunError(f"执行命令失败：{e}")
    出 = (r.stdout or b"").decode("utf-8", "replace")
    错 = (r.stderr or b"").decode("utf-8", "replace")
    return 出, 错, r.returncode


def 打开(路径) -> bool:
    """用系统的**默认程序**打开一个文件（或网址），成功返回真、失败返回假。

    想「生成完报告自动打开」时用它，不必自己按系统拼 `start` / `open` /
    `xdg-open`——三平台差异替你吸收了。**不抛异常**：没有图形界面 / 找不到
    默认程序时返回假，让调用方自己决定怎么办（打印一行提示、或者干脆不管）。

    ⚠️ **目标不存在（且不是网址）时直接返回假、什么都不启动** —— 这条规矩五
    个执行器一致，既能挡掉「路径打错却弹出个空白窗口」，也让行为可判。
    含 `://` 的一律当网址交给系统（`https://…`、`file://…`）。

    注意「成功」只代表**已经交给了系统去打开**，不代表窗口真的弹出来了
    —— 异步启动，不等着那程序退出。
    """
    目标 = str(路径)
    if "://" not in 目标 and not _os.path.exists(目标):
        return False
    try:
        if _os.name == "nt":
            _os.startfile(目标)                      # noqa: S606
            return True
        程序 = "open" if _py_platform.system() == "Darwin" else "xdg-open"
        _py_subprocess.Popen(
            [程序, 目标],
            stdout=_py_subprocess.DEVNULL, stderr=_py_subprocess.DEVNULL)
        return True
    except Exception:                                # noqa: BLE001
        return False


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


def CPU核数() -> int:
    """返回这台机器**能并行干活的 CPU 核数**（决定「开几路并行」时用）。

    ⚠️ 这是**逻辑核数**（含超线程），不是物理核数。容器里可能比宿主机少
    —— 采集到多少就报多少，不猜。
    """
    return _os.cpu_count() or 1
