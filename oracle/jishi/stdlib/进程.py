# -*- coding: utf-8 -*-
"""基石标准库：进程。`subprocess` 的句柄式封装 —— 起一个长期跑的子进程，边写边读。

## 为什么要有这个模块

`系统.执行` 是「跑完拿输出」：一条命令、一次返回。但有一类活它干不了：

- 交互式子进程（REPL、`ffmpeg` 的进度、`ssh` 会话）；
- 要**边喂边读**的（把数据流进去、同时读结果，不然管道一满两边互锁）；
- stderr 与 stdout 要分开处理的（错误要实时看到，不能等它跑完）。

## 三条设计口径（三宿主必须一致）

1. **句柄是宿主对象**：`类型()` 给「进程」，显示为 `<进程>`。
2. **`读行` 永远不会无限期卡住调用方**：`超时` 省略 = 一直等（还是会被 EOF
   或 `终止` 解开）；给了秒数就按时返回 `空`。**读到 EOF 也给 `空`**。
3. **孙进程不许拖住我们**：`等待` 只等**直接子进程**（`child.wait()`），
   绝不去「读到管道 EOF」；读线程是**守护线程**，程序退出不等它。
   （老坑：子进程再起一个孙进程并继承了 stdout，管道就永远不 EOF ——
   那时「读到 EOF 才返回」会让一个 0.2 秒的活变成 8.4 秒。）

## 编码

一律 UTF-8（`errors="replace"`）：子进程的字节不是合法 UTF-8 时给替换符，
**不抛错**（否则一句乱码输出就能把整个会话打断）。
"""

from __future__ import annotations

import os as _py_os
import queue as _py_queue
import subprocess as _py_subprocess
import threading as _py_threading

from ..errors import RunError, RunTypeError, RunValueError
from ..runtime import type_name as _类型名

__all__ = [
    "启动", "写文本", "写行", "读行", "读错误行",
    "在运行", "终止", "等待", "退出码",
    # M217 顺带补的一对（数据库的 `.lock` 要用：记属主、强杀之后回收陈旧锁）
    "进程号", "存在",
]


class 进程句柄:
    """子进程句柄。下划线开头的字段是**实现**，不是给用户用的 API。"""

    #: `类型()` 读它（与 Rust 的 `NativeObj.type_name` 同口径）
    _jishi_type_name = "进程"

    def __init__(self, popen):
        self._p = popen
        self._出队: _py_queue.Queue = _py_queue.Queue()
        self._错队: _py_queue.Queue = _py_queue.Queue()
        self._出完 = _py_threading.Event()
        self._错完 = _py_threading.Event()
        # 🔴 守护线程：主程序退出时**不等**它们。子进程（或它的孙进程）攥着
        #    管道不放时，非守护线程会让解释器在退出时干等 —— 就是那个 8.4 秒。
        _py_threading.Thread(target=_泵, args=(popen.stdout, self._出队, self._出完),
                             daemon=True).start()
        if popen.stderr is not None:
            _py_threading.Thread(target=_泵,
                                 args=(popen.stderr, self._错队, self._错完),
                                 daemon=True).start()
        else:
            # `合并错误=真`：stderr 已经并进 stdout，这条队列**立刻就是空的**
            self._错完.set()

    def __repr__(self) -> str:  # pragma: no cover - 只用于显示
        # 与 Rust 的 `<{} 对象>` / Node 的 `<${typeName} 对象>` 逐字一致
        return "<进程 对象>"


def _泵(流, 队, 完):
    """把管道里的行推进队列（后台线程）。EOF 后置 `完`。

    ⚠️ 读的是**字节**再自己解码：文本模式下的 `readline` 遇到畸形 UTF-8
    会抛 `UnicodeDecodeError`，那个异常在后台线程里没人接 —— 线程直接死掉，
    队列就永远等不到 `完`，`读行(超时=省略)` 会挂到天荒地老。
    """
    try:
        for 原始 in iter(流.readline, b""):
            队.put(原始.decode("utf-8", "replace").rstrip("\r\n"))
    except (ValueError, OSError):
        pass
    finally:
        try:
            流.close()
        except OSError:
            pass
        完.set()


def _句柄(值, 名) -> 进程句柄:
    if not isinstance(值, 进程句柄):
        raise RunTypeError(
            f"「{名}」要传 `进程.启动` 拿到的句柄（现在是「{_类名(值)}」）")
    return 值


def _类名(值) -> str:
    """基石的类型名（与 Rust 的 `type_label` / Node 的 `typeName` 同口径）。

    ⚠️ 不能用 `type(v).__name__` —— 那会漏出 `str` / `dict` 这种英文，
    而另外两个宿主说的是「文本」「字典」（四引擎对拍会当场抓到）。
    """
    return _类型名(值)


def _文本(值, 名) -> str:
    if not isinstance(值, str):
        raise RunTypeError(f"「{名}」要传文本（现在是「{_类名(值)}」）")
    return 值


def _超时(值, 名):
    if 值 is None:
        return None
    if isinstance(值, bool) or not isinstance(值, (int, float)):
        raise RunTypeError(f"「{名}」的超时要给秒数（省略 = 一直等）")
    if 值 < 0:
        raise RunValueError(f"「{名}」的超时不能是负数（给的是 {值}）")
    return float(值)


def _拿(句柄: 进程句柄, 队, 完, 超时):
    """取一行；`超时` 为 None 就阻塞到「有行 / 已结束」。返回 `(拿到了吗, 行)`。"""
    if 超时 is None:
        while True:
            try:
                return True, 队.get(timeout=0.5)
            except _py_queue.Empty:
                if 完.is_set() and 队.empty():
                    return False, None
    try:
        return True, 队.get(timeout=超时)
    except _py_queue.Empty:
        return False, None


def 启动(命令, 参数们=None, 选项=None) -> 进程句柄:
    """起一个子进程，返回**句柄**。

    - `参数们`：文本列表（**argv 形态，不过 shell** —— 空格、引号、中文原样传）。
    - `选项`：字典，可省。支持的键：
      - `"目录"`：子进程的工作目录；
      - `"环境"`：字典，**追加/覆盖**到当前环境变量上（不是整体替换）；
      - `"合并错误"`：真 ⇒ stderr 并进 stdout（那时 `读错误行` 永远给 `空`）。
    """
    命令 = _文本(命令, "启动")
    if 参数们 is None:
        参数们 = []
    if not isinstance(参数们, (list, tuple)):
        raise RunTypeError("「启动」的参数们要给文本列表")
    argv = [_文本(x, "启动") for x in 参数们]
    目录 = None
    环境 = None
    合并 = False
    if 选项 is not None:
        if not isinstance(选项, dict):
            raise RunTypeError("「启动」的选项要给字典（可省略）")
        目录值 = 选项.get("目录")
        if 目录值 not in (None, ""):
            目录 = str(目录值)
        环境值 = 选项.get("环境")
        if 环境值 is not None:
            if not isinstance(环境值, dict):
                raise RunValueError("「启动」的「环境」要给字典")
            环境 = dict(_py_os.environ)
            环境.update({str(k): str(v) for k, v in 环境值.items()})
        合并 = bool(选项.get("合并错误", False))
    try:
        p = _py_subprocess.Popen(
            [命令] + argv,
            stdin=_py_subprocess.PIPE,
            stdout=_py_subprocess.PIPE,
            stderr=_py_subprocess.STDOUT if 合并 else _py_subprocess.PIPE,
            cwd=目录,
            env=环境,
            bufsize=0,
        )
    except FileNotFoundError:
        raise RunError(f"起不来进程「{命令}」：找不到这个程序")
    except OSError as e:
        raise RunError(f"起不来进程「{命令}」：{e}")
    return 进程句柄(p)


def 写文本(句柄, 内容) -> bool:
    """往子进程的标准输入写一段文本（**不自动换行**），返回真。"""
    h = _句柄(句柄, "写文本")
    内容 = _文本(内容, "写文本")
    try:
        h._p.stdin.write(内容.encode("utf-8"))
        h._p.stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        raise RunError("写不进这个进程：它的标准输入已经关了（子进程退出了？）")
    return True


def 写行(句柄, 内容="") -> bool:
    """往子进程的标准输入写一行（**自动补换行符**），返回真。"""
    return 写文本(句柄, _文本(内容, "写行") + "\n")


def 读行(句柄, 超时=None):
    """读一行（去掉行尾换行）。超时 / 已无输出 ⇒ `空`。

    `超时` 省略 = 一直等（子进程退出时会给 `空`，不会永远挂着）。
    """
    h = _句柄(句柄, "读行")
    拿到, 行 = _拿(h, h._出队, h._出完, _超时(超时, "读行"))
    return 行 if 拿到 else None


def 读错误行(句柄, 超时=None):
    """读 stderr 的一行。超时 / 没有输错 ⇒ `空`。"""
    h = _句柄(句柄, "读错误行")
    拿到, 行 = _拿(h, h._错队, h._错完, _超时(超时, "读错误行"))
    return 行 if 拿到 else None


def 在运行(句柄) -> bool:
    """子进程还在跑吗？（**只看直接子进程**，不看它起的孙进程）"""
    h = _句柄(句柄, "在运行")
    return h._p.poll() is None


def _码(rc):
    """退出码归一：**被信号杀死一律给 -1**（不是正常退出）。

    🔴 三宿主要对得上：POSIX 上 `subprocess` 给 `-15`（SIGTERM 的负值），
    Rust 那边 `ExitStatus::code()` 是 `None` ⇒ -1，Node 也从 `signal` 推 -1。
    Windows 上 `terminate` 走 TerminateProcess，三边都能拿到 **1**，不用归一。
    """
    if rc is None:
        return None
    if rc < 0:
        return -1
    return rc


def 终止(句柄) -> bool:
    """终止子进程。本来就没在跑 ⇒ 返回假（不报错，幂等）。

    先礼后兵：`terminate()` → 等 2 秒 → 还活着就 `kill()` → 再等 2 秒。
    """
    h = _句柄(句柄, "终止")
    if h._p.poll() is not None:
        return False
    try:
        h._p.terminate()
    except OSError:
        pass
    try:
        h._p.wait(timeout=2)
    except _py_subprocess.TimeoutExpired:
        try:
            h._p.kill()
        except OSError:
            pass
        try:
            h._p.wait(timeout=2)
        except _py_subprocess.TimeoutExpired:
            pass
    return True


def 等待(句柄, 超时=None):
    """等子进程结束，返回**退出码**；超时给 `空`。

    ⚠️ 等的是**直接子进程**，不是「管道读到 EOF」—— 子进程起了孙进程、
    孙进程攥着 stdout 不放时，后一种写法会白等（那 8.4 秒就是这么来的）。
    """
    h = _句柄(句柄, "等待")
    try:
        return _码(h._p.wait(timeout=_超时(超时, "等待")))
    except _py_subprocess.TimeoutExpired:
        return None


def 退出码(句柄):
    """子进程的退出码；**还在跑就给 `空`**（等价于 `等待(超时=0)`）。"""
    h = _句柄(句柄, "退出码")
    return _码(h._p.poll())


# ---------------------------------------------------------------------------
# 进程号 / 存活查询（M217 顺带补：数据库 `.lock` 的陈旧锁回收要用）
# ---------------------------------------------------------------------------

def 进程号() -> int:
    """本进程的进程号（PID）。写锁文件、打日志前缀时用。"""
    return _py_os.getpid()


def 存在(号) -> bool:
    """那个进程号还活着吗？

    ⚠️ 只能说「**看起来**还活着」：号被系统复用过 ⇒ 会给真；
    别的用户的进程（不给查）⇒ 也算活着。

    🔴 反方向的错（把活的判成死的）后果更重 —— 那会让两个进程同时写一个库，
    所以这里一律**保守**：拿不准就当活着。
    """
    if isinstance(号, bool) or not isinstance(号, int):
        raise RunTypeError("「存在」要传进程号（整数），比如 进程.进程号()")
    if 号 <= 0:
        return False
    if _py_os.name == "nt":
        import ctypes as _ct
        from ctypes import wintypes as _wt

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        # 🔴 必须 `use_last_error=True`：`ctypes.windll` **不维护** GetLastError，
        #    那样 `get_last_error()` 永远是 0，于是「没权限的活进程」会被判成死的。
        核 = _ct.WinDLL("kernel32", use_last_error=True)
        核.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
        核.OpenProcess.restype = _wt.HANDLE
        核.GetExitCodeProcess.argtypes = [_wt.HANDLE, _ct.POINTER(_wt.DWORD)]
        核.GetExitCodeProcess.restype = _wt.BOOL
        核.CloseHandle.argtypes = [_wt.HANDLE]
        核.CloseHandle.restype = _wt.BOOL
        h = 核.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, 号)
        if not h:
            # 5 = 没权限（进程**活着**，只是不归我管）；87 = 没有这个进程
            return _ct.get_last_error() == 5
        try:
            码 = _ct.c_ulong()
            if not 核.GetExitCodeProcess(h, _ct.byref(码)):
                return False
            return 码.value == 259  # STILL_ACTIVE
        finally:
            核.CloseHandle(h)
    try:
        _py_os.kill(号, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
