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

## 按行 vs 按字节（M230）

| 要读什么 | 用哪个 | 什么时候用 |
|---|---|---|
| **一行一行**的文本输出 | `读行` / `读错误行` | 普通命令行程序（`git`、`npm`、自写的脚本）：它们按行 flush |
| **一帧一帧**的分帧协议 | `读块` / `读可用` | LSP / DAP（`Content-Length: N\r\n\r\n{…}`）、自己定长分帧的协议 |

🔴 **为什么要分两种**：`读行` 把 `\n` 当分隔符 —— 而分帧协议的**正文里完全可能
有 `\n`**。按行读会把一帧切碎，症状是「报文解析到一半就报格式错」，而日志里
看着是完整的（因为日志按行打）。

⚠️ **一个句柄只用一种读法**：两个视图**独立消费**（同一段字节会被两边各看到一次），
混用不会崩，但你会重复读到同一段输出。
"""

from __future__ import annotations

import os as _py_os
import queue as _py_queue
import subprocess as _py_subprocess
import threading as _py_threading
import time as _py_time

from ..errors import RunError, RunTypeError, RunValueError
from ..runtime import JishiBytes
from ..runtime import type_name as _类型名

__all__ = [
    "启动", "写文本", "写行", "读行", "读错误行",
    "在运行", "终止", "等待", "退出码",
    # M217 顺带补的一对（数据库的 `.lock` 要用：记属主、强杀之后回收陈旧锁）
    "进程号", "存在",
    # M230（L-13）：字节级读（LSP / DAP 的 Content-Length 分帧要用）
    "读块", "读可用",
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
        #: **M230**：stdout 的**原始字节**（`读块` / `读可用` 用）。
        #: 与 `_出队` 是两个**独立**视图：混用不会崩，但同一段输出会两边各看到一次。
        self._字节箱 = _字节箱()
        # 🔴 守护线程：主程序退出时**不等**它们。子进程（或它的孙进程）攥着
        #    管道不放时，非守护线程会让解释器在退出时干等 —— 就是那个 8.4 秒。
        _py_threading.Thread(target=_泵,
                             args=(popen.stdout, self._出队, self._出完, self._字节箱),
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


class _字节箱:
    """**M230**：stdout 的原始字节（`读块` / `读可用` 的视图）。

    🔴 **一启动就攒**（不是「第一次读块才开」）。为什么：泵线程从 `启动` 那一刻
    就在读，而 `读块` 是用户后面才调的 —— 懒开的话，那之前到达的字节已经被
    **读走并丢掉**了（M230 实测：直连 `jishi lsp` 收到的永远是空字节）。
    代价是所有子进程输出多存一份字节副本；对「交互式子进程」这个量级可以接受。
    """

    def __init__(self):
        self._锁 = _py_threading.Lock()
        self._信号 = _py_threading.Condition(self._锁)
        self._buf = bytearray()
        self.要 = True
        self.完 = False

    def 加(self, 块: bytes):
        with self._锁:
            if not self.要:
                return
            self._buf.extend(块)
            self._信号.notify_all()

    def 收工(self):
        with self._锁:
            self.完 = True
            self._信号.notify_all()

    def 开(self):
        with self._锁:
            self.要 = True

    def 可用(self) -> int:
        with self._锁:
            return len(self._buf)

    def 取(self, 最多, 超时):
        """取最多 `最多` 字节；超时 / 已结束 ⇒ `b""`（**不抛错**）。"""
        with self._锁:
            def 有货():
                if not self._buf:
                    return None
                块 = bytes(self._buf[:最多])
                del self._buf[:最多]
                return 块

            if 超时 is None:
                while True:
                    got = 有货()
                    if got is not None:
                        return got
                    if self.完:
                        return b""
                    self._信号.wait()
            截止 = _py_time.monotonic() + 超时
            while True:
                got = 有货()
                if got is not None:
                    return got
                if self.完:
                    return b""
                剩 = 截止 - _py_time.monotonic()
                if 剩 <= 0:
                    return b""
                self._信号.wait(剩)


def 读块(句柄, 最多=65536, 超时=None):
    """**M230**：读最多这么多**字节**（LSP / DAP 的 `Content-Length` 分帧要用）。

    🔴 要这个的理由：LSP / DAP 的报文是 `Content-Length: 42\r\n\r\n{…}` ——
    正文里一旦有 `\n`，按行读就会被切碎。磐石 IDE 之前为此在中间挂了个 Node 的
    `lsp行桥.js`，这一条做完那个桥可以删。

    ⚠️ **超时给空字节，不抛错** —— 与 `网络.获取流` 同一个口径：读超时是
    「这一拍没东西」，不是「出错了」。
    """
    h = _句柄(句柄, "读块")
    if isinstance(最多, bool) or not isinstance(最多, int) or 最多 <= 0:
        raise RunTypeError(
            f"「读块」的第二个参数（最多几个字节）要正整数，给的是「{_类名(最多)}」")
    h._字节箱.开()
    # ⚠️ 包成 `JishiBytes`（与 `文件.读字节` 一致）：直接回原生 `bytes` 的话
    # `类型()` 会说「bytes」，与 Rust 侧的「字节」对不上（M230 实测踩到）。
    return JishiBytes(h._字节箱.取(min(int(最多), 64 * 1024 * 1024), _超时(超时, "读块")))


def 读可用(句柄) -> int:
    """**M230**：现在有多少字节可读（**不阻塞**）。

    用途：写「读一批、处理一批、没了就干别的」那种循环，不必靠超时去探。
    """
    h = _句柄(句柄, "读可用")
    h._字节箱.开()
    return h._字节箱.可用()


def _泵(流, 队, 完, 字节箱=None):
    """把管道里的**字节**读进来（后台线程）：切行给 `队`，原样存进 `字节箱`。

    📌 **M230**：以前只会按行（`readline`），于是 LSP / DAP 那种
    「`Content-Length: 42\r\n\r\n{…}`」的帧没法精确收 —— 正文里一旦有 `\n`
    就会被切碎。现在同一遍读里做两件事：

    - **切行**（`队`）→ `读行` 照旧；
    - **原样字节**（`字节箱`）→ `读块` / `读可用`（只在第一次 `读块` 之后才攒，
      所以普通用途一个字节都不多存）。

    ⚠️ 读的是**字节**再自己解码：文本模式下的 `readline` 遇到畸形 UTF-8
    会抛 `UnicodeDecodeError`，那个异常在后台线程里没人接 —— 线程直接死掉，
    队列就永远等不到 `完`，`读行(超时=省略)` 会挂到天荒地老。
    """
    尾 = b""
    try:
        while True:
            块 = 流.read(65536)
            if not 块:
                break
            if 字节箱 is not None:
                字节箱.加(块)
            尾 += 块
            while True:
                位 = 尾.find(b"\n")
                if 位 < 0:
                    break
                行, 尾 = 尾[:位], 尾[位 + 1:]
                队.put(行.rstrip(b"\r").decode("utf-8", "replace"))
    except (ValueError, OSError):
        pass
    finally:
        # 最后一段没有换行的内容也是内容（子进程退出前打的那句往往就漏在这里）
        if 尾:
            try:
                队.put(尾.rstrip(b"\r").decode("utf-8", "replace"))
            except Exception:                                      # noqa: BLE001
                pass
        try:
            流.close()
        except OSError:
            pass
        完.set()
        if 字节箱 is not None:
            字节箱.收工()


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
