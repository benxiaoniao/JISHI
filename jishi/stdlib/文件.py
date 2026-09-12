# -*- coding: utf-8 -*-
"""基石标准库：文件。薄封装 Python 的 pathlib / shutil。"""

from __future__ import annotations

import shutil as _py_shutil
from pathlib import Path as _py_Path, PurePosixPath as _py_PurePosixPath

from ..errors import RunFileError

__all__ = [
    "写文本", "读文本", "追加文本", "写行", "按行读",
    "文件存在", "是文件", "是目录", "列出目录",
    "创建目录", "删除文件", "复制", "文件大小",
    "路径拼接", "文件名", "扩展名",
]


def _path(路径) -> _py_Path:
    return _py_Path(str(路径))


def 写文本(路径, 内容: str) -> None:
    """把内容写进文件（覆盖原有内容）。"""
    _path(路径).write_text(str(内容), encoding="utf-8")


def 读文本(路径) -> str:
    """读取整个文本文件的内容。"""
    p = _path(路径)
    if not p.is_file():
        raise RunFileError(f"找不到文件「{路径}」，没法读取")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise RunFileError(f"文件「{路径}」不是 UTF-8 编码，没法按文本读取")


def 追加文本(路径, 内容: str) -> None:
    """把内容追加到文件末尾（文件不存在则创建）。"""
    with open(_path(路径), "a", encoding="utf-8") as f:
        f.write(str(内容))


def 写行(路径, 行列表: list) -> None:
    """把列表里的每一行写进文件（自动加换行，覆盖原内容）。"""
    with open(_path(路径), "w", encoding="utf-8") as f:
        for 行 in 行列表:
            f.write(str(行) + "\n")


def 按行读(路径) -> list:
    """按行读取文件，返回每行内容的列表（不含换行符）。"""
    return 读文本(路径).splitlines()


def 文件存在(路径) -> bool:
    """判断文件或目录是否存在。"""
    return _path(路径).exists()


def 是文件(路径) -> bool:
    """判断路径是不是一个文件。"""
    return _path(路径).is_file()


def 是目录(路径) -> bool:
    """判断路径是不是一个目录。"""
    return _path(路径).is_dir()


def 列出目录(路径: str = ".") -> list:
    """列出目录里的所有文件和子目录名字，返回列表。"""
    p = _path(路径)
    if not p.is_dir():
        raise RunFileError(f"目录「{路径}」不存在，没法列出内容")
    return sorted(item.name for item in p.iterdir())


def 创建目录(路径) -> None:
    """创建目录（自动创建中间层级，已存在则不报错）。"""
    _path(路径).mkdir(parents=True, exist_ok=True)


def 删除文件(路径) -> None:
    """删除文件。"""
    p = _path(路径)
    if p.is_file():
        p.unlink()
    elif p.is_dir():
        raise RunFileError(f"「{路径}」是目录，请先清空再删里面的文件；删目录的功能暂未提供")
    else:
        raise RunFileError(f"找不到文件「{路径}」，没法删除")


def 复制(源路径, 目标路径) -> None:
    """复制文件。"""
    try:
        _py_shutil.copyfile(str(源路径), str(目标路径))
    except OSError:
        raise RunFileError(f"无法把「{源路径}」复制到「{目标路径}」")


def 文件大小(路径) -> int:
    """返回文件的字节数。"""
    p = _path(路径)
    if not p.is_file():
        raise RunFileError(f"找不到文件「{路径}」，没法获取大小")
    return p.stat().st_size


def 路径拼接(*部分) -> str:
    """把几段路径拼在一起，用 / 分隔。"""
    return str(_py_PurePosixPath(*[str(p) for p in 部分]))


def 文件名(路径) -> str:
    """取出路径里的文件名部分。"""
    return _py_Path(str(路径).replace("\\", "/")).name


def 扩展名(路径) -> str:
    """取出文件的扩展名（带点），例如 a.jsh → .jsh。"""
    return _py_Path(str(路径).replace("\\", "/")).suffix
