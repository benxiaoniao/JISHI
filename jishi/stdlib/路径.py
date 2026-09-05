# -*- coding: utf-8 -*-
"""基石标准库：路径。薄封装 Python 的 pathlib，中文 API。

零第三方依赖。所有函数返回文本路径，路径拼接用「/」分隔（跨平台）。
"""

from __future__ import annotations

from pathlib import Path as _PyPath

from ..errors import RunValueError

__all__ = [
    "存在", "是文件", "是目录", "绝对路径", "父目录", "文件名",
    "后缀", "无后缀名", "连接", "当前目录", "主目录", "创建目录",
    "删除", "列出", "重命名", "大小", "修改时间",
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


def 列出(路径) -> list:
    """列出目录下所有条目（文件+子目录）的名字，按名字排序。"""
    p = _PyPath(str(路径))
    if not p.is_dir():
        raise RunValueError(f"「{路径}」不是目录")
    return sorted(e.name for e in p.iterdir())


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
