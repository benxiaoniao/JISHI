# -*- coding: utf-8 -*-
"""基石标准库：路径。薄封装 Python 的 pathlib，中文 API。

零第三方依赖。所有函数返回文本路径，路径拼接用「/」分隔（跨平台）。
"""

from __future__ import annotations

import os as _py_os
import shutil as _py_shutil
import tempfile as _py_tempfile
from pathlib import Path as _PyPath

from ..errors import RunValueError, RunFileError

__all__ = [
    "存在", "是文件", "是目录", "绝对路径", "父目录", "文件名",
    "后缀", "无后缀名", "连接", "当前目录", "主目录", "创建目录",
    "删除", "递归删除", "列出", "递归列出", "按模式找", "重命名",
    "大小", "修改时间", "系统临时目录", "临时目录", "临时文件",
    "分隔符",
]


def 连接(*部分) -> str:
    """把若干路径片段连接成一个路径（用系统分隔符）。"""
    if not 部分:
        return "."
    p = _PyPath(str(部分[0]))
    for seg in 部分[1:]:
        p = p / str(seg)
    return str(p)


def 存在(路径) -> bool:
    """路径是否存在（文件或目录）。"""
    return _PyPath(str(路径)).exists()


def 是文件(路径) -> bool:
    """路径是否是一个已存在的文件。"""
    return _PyPath(str(路径)).is_file()


def 是目录(路径) -> bool:
    """路径是否是一个已存在的目录。"""
    return _PyPath(str(路径)).is_dir()


def 绝对路径(路径) -> str:
    """把相对路径转成绝对路径。"""
    return str(_PyPath(str(路径)).resolve())


def 父目录(路径) -> str:
    """返回路径的父目录。"""
    return str(_PyPath(str(路径)).parent)


def 文件名(路径) -> str:
    """返回路径的最后一段（文件名或目录名）。"""
    return _PyPath(str(路径)).name


def 后缀(路径) -> str:
    """返回文件后缀（含点，如 .txt），无后缀返回空文本。"""
    return _PyPath(str(路径)).suffix


def 无后缀名(路径) -> str:
    """返回去掉后缀的文件名（如 a.txt → a）。"""
    return _PyPath(str(路径)).stem


def 当前目录() -> str:
    """返回当前工作目录。"""
    return str(_PyPath.cwd())


def 主目录() -> str:
    """返回当前用户主目录。"""
    return str(_PyPath.home())


def 创建目录(路径, 递归: bool = False) -> str:
    """创建目录；递归=真 时自动创建缺失的父目录。返回目录路径。"""
    p = _PyPath(str(路径))
    if 递归:
        p.mkdir(parents=True, exist_ok=True)
    else:
        p.mkdir(exist_ok=True)
    return str(p)


def 删除(路径) -> bool:
    """删除文件或空目录，成功返回真；不存在返回假。"""
    p = _PyPath(str(路径))
    if p.is_file():
        p.unlink()
        return True
    if p.is_dir():
        p.rmdir()
        return True
    return False


def 递归删除(路径) -> bool:
    """递归删除：文件直接删，目录连同**里面的所有东西**一起删；不存在返回假。

    ⚠️ 这是**破坏性**操作，删掉就找不回来了（不进回收站）。`路径.删除` 只能删
    空目录，清理一个临时的目录树（比如打包用的暂存区）就得靠它。
    """
    p = _PyPath(str(路径))
    if p.is_file():
        p.unlink()
        return True
    if p.is_dir():
        try:
            _py_shutil.rmtree(p)
        except OSError as e:
            raise RunFileError(f"删除目录「{路径}」失败：{e}")
        return True
    return False


def 列出(路径) -> list:
    """列出目录下所有条目（文件+子目录）的名字，按名字排序。"""
    p = _PyPath(str(路径))
    if not p.is_dir():
        raise RunValueError(f"「{路径}」不是目录")
    return sorted(e.name for e in p.iterdir())


def 递归列出(路径, 后缀: str = "") -> list:
    """递归列出目录下所有文件（含子目录），返回**相对路径**列表，已排序。

    `后缀` 非空时只留这个后缀的文件，例如 `递归列出("examples", ".jsh")`
    列出所有基石源文件。写 `基石自述站` 时数 `examples/` 下的 .jsh 要手写
    递归，正是这个函数要消掉的（Python 那边一个 `rglob`）。
    """
    p = _PyPath(str(路径))
    if not p.is_dir():
        raise RunValueError(f"「{路径}」不是目录")
    出 = []
    for e in p.rglob("*"):
        if not e.is_file():
            continue
        if 后缀 and e.suffix != 后缀:
            continue
        出.append(str(e.relative_to(p)))
    return sorted(出)


#: 通配模式的规则（**五引擎共用同一套小规则**，不依赖各语言的正则/fnmatch
#: —— 它们在 `[!…]`、未闭合 `[`、范围边界这些细节上互相对不齐）：
#:   `*` 任意多个字符（含 0 个）；`?` 恰好一个字符；
#:   `[abc]` 字符集、`[a-z]` 范围、`[!abc]` 取反；
#:   `[` 没有配对的 `]` 时按**普通字符** `[`；集里第一个字符若是 `]` 按普通字符；
#:   `^` **不是**取反标记（按普通字符，与 Python 的 `fnmatch` 一致）；
#:   其余字符按字面匹配（`\` **不是**转义符）。**区分大小写**。


def _解析集合(p: str, 起: int):
    """从 `p[起] == '['` 解析一个字符集；没有配对的 `]` 就返回 None。

    返回 `(是否取反, [(起, 止), …], 下一位下标)`。
    """
    n = len(p)
    i = 起 + 1
    否定 = False
    if i < n and p[i] == "!":
        否定 = True
        i += 1
    项: list = []
    if i < n and p[i] == "]":          # 紧跟的 `]` 是普通字符
        项.append(("]", "]"))
        i += 1
    while i < n and p[i] != "]":
        if i + 2 < n and p[i + 1] == "-" and p[i + 2] != "]":
            项.append((p[i], p[i + 2]))
            i += 3
        else:
            项.append((p[i], p[i]))
            i += 1
    if i >= n:                         # 没有配对的 `]`
        return None
    return (否定, 项, i + 1)


def _模式匹配(模式: str, 名: str) -> bool:
    """判断文件名 `名` 是否匹配通配 `模式`（规则见 `_解析集合` 上面那段）。"""
    p, s = 模式, 名
    pi = si = 0
    星位 = -1          # 最近一个 `*` 在模式里的位置
    星后 = 0           # 那个 `*` 之后、名字里已经被吃掉的位置
    while si < len(s):
        吃 = False
        if pi < len(p):
            c = p[pi]
            if c == "*":
                星位 = pi
                星后 = si
                pi += 1
                吃 = True
            elif c == "?":
                pi += 1
                si += 1
                吃 = True
            elif c == "[":
                集 = _解析集合(p, pi)
                if 集 is None:
                    if s[si] == "[":
                        pi += 1
                        si += 1
                        吃 = True
                else:
                    否定, 项, 下一位 = 集
                    if (any(a <= s[si] <= b for a, b in 项)) != 否定:
                        pi = 下一位
                        si += 1
                        吃 = True
            elif c == s[si]:
                pi += 1
                si += 1
                吃 = True
        if 吃:
            continue
        if 星位 >= 0:                  # 回溯：让那个 `*` 多吞一个字符
            pi = 星位 + 1
            星后 += 1
            si = 星后
        else:
            return False
    while pi < len(p) and p[pi] == "*":
        pi += 1
    return pi == len(p)


def 按模式找(目录, 模式: str = "*", 递归: bool = True) -> list:
    """按**通配模式**找出目录下匹配的文件，返回**相对路径**列表，已排序。

    模式匹配的是**文件名**（不含目录那一截），支持 `*` / `?` / `[abc]` /
    `[a-z]` / `[!abc]`。例如 `按模式找("下载", "报告*.md")`、
    `按模式找("日志", "2026-??.log")`。

    `递归=真`（默认）时子目录也找；只找当前这一层就传 `递归=假`。
    ⚠️ 匹配**区分大小写**，且规则是基石自己定的小集合（见文件里 `_解析集合`
    上面的说明）—— 这样五个执行器的结果**逐字节一致**，不受各语言 glob
    实现差异影响。

    与 `递归列出` 的区别：那个只能按**后缀**筛（`.jsh`），这个能按**任意模式**
    筛（前缀 / 中间段 / 多个条件），是「文件批处理」最常用的一个。
    """
    根 = _PyPath(str(目录))
    if not 根.is_dir():
        raise RunValueError(f"「{目录}」不是目录")
    模式文 = str(模式)
    出: list = []

    def 走(基: _PyPath, 相对: _PyPath) -> None:
        for e in sorted(基.iterdir(), key=lambda x: x.name):
            if e.is_dir():
                if 递归:
                    走(e, 相对 / e.name)
                continue
            if _模式匹配(模式文, e.name):
                出.append(str(相对 / e.name))

    走(根, _PyPath("."))
    return sorted(出)


def 系统临时目录() -> str:
    """返回**系统临时目录**的路径（只指出位置，不在磁盘上动任何东西）。"""
    return str(_PyPath(_py_tempfile.gettempdir()))


def 临时目录(前缀: str = "") -> str:
    """**新建**一个唯一的临时目录并返回它的路径。

    名字由系统生成，不会与别人撞（多进程同时跑也安全）。「暂存区」这类用法
    最适合它：把中间产物丢进去、处理完再 `路径.递归删除(路径)` 清掉。
    用完一定要删，否则临时目录会越积越多。
    """
    try:
        return str(_PyPath(_py_tempfile.mkdtemp(prefix=str(前缀))))
    except OSError as e:
        raise RunFileError(f"建不了临时目录：{e}")


def 临时文件(后缀: str = "") -> str:
    """**新建**一个唯一的空临时文件并返回它的路径（名字由系统生成）。

    适合「先落到临时文件、处理完再改名」这类流程。用完记得 `路径.删除(路径)`。
    """
    try:
        fd, 名 = _py_tempfile.mkstemp(suffix=str(后缀))
    except OSError as e:
        raise RunFileError(f"建不了临时文件：{e}")
    _py_os.close(fd)
    return str(_PyPath(名))


def 重命名(旧路径, 新路径) -> str:
    """把文件或目录重命名，返回新路径。"""
    p = _PyPath(str(旧路径))
    if not p.exists():
        raise RunValueError(f"「{旧路径}」不存在")
    p.rename(str(新路径))
    return str(新路径)


def 大小(路径) -> int:
    """返回文件大小（字节）。"""
    p = _PyPath(str(路径))
    if not p.is_file():
        raise RunValueError(f"「{路径}」不是文件")
    return p.stat().st_size


def 修改时间(路径) -> str:
    """返回文件最后修改时间（年-月-日 时:分:秒）。"""
    import datetime as _dt
    p = _PyPath(str(路径))
    if not p.exists():
        raise RunValueError(f"「{路径}」不存在")
    return _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime(
        "%Y-%m-%d %H:%M:%S")


def 分隔符() -> str:
    """返回系统的**路径分隔符**：Windows 是 `\\`，其余是 `/`。

    ⚠️ 绝大多数情况**用不着它** —— `路径.连接` 已经按分隔符拼好了。
    它只在「要把路径显示给用户」或「解析别人给的路径」时才需要：
    例如把 `a/b/c` 拆成三段时，得知道按哪个字符拆。
    """
    return _py_os.sep
