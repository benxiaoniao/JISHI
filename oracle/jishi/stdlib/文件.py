# -*- coding: utf-8 -*-
"""基石标准库：文件。薄封装 Python 的 pathlib / shutil。"""

from __future__ import annotations

import os as _py_os
import shutil as _py_shutil
import tempfile as _py_tempfile
from pathlib import Path as _py_Path, PurePosixPath as _py_PurePosixPath

from ..errors import RunFileError
from ..runtime import jishi_repr, JishiBytes

__all__ = [
    "写文本", "读文本", "追加文本", "写行", "按行读",
    "读字节", "写字节",
    "文件存在", "是文件", "是目录", "列出目录",
    "创建目录", "删除文件", "复制", "复制目录", "文件大小", "原子写",
    "追加字节",
    "路径拼接", "文件名", "扩展名",
]


def _path(路径) -> _py_Path:
    return _py_Path(str(路径))


# 读写都显式 `newline=""`：**不做换行转换**。
#
# Python 的文本模式在 Windows 上会把 `\n` 自动写成 `\r\n`，读的时候又转回来。
# 这会让「同一份程序在不同平台产出的文件字节不同」，也会与其它语言的宿主
# （Node 的 `fs` 不转换）对不上。基石的定位是「同一份程序到哪都一样」，
# 所以标准库统一按字节保真来（M31）。`按行读` 用 splitlines，两种换行都认。


def 写文本(路径, 内容: str) -> None:
    """把内容写进文件（覆盖原有内容）。"""
    with open(_path(路径), "w", encoding="utf-8", newline="") as f:
        f.write(jishi_repr(内容, top=True))


def 读文本(路径) -> str:
    """读取整个文本文件的内容。"""
    p = _path(路径)
    if not p.is_file():
        raise RunFileError(f"找不到文件「{路径}」，没法读取")
    try:
        with open(p, encoding="utf-8", newline="") as f:
            return f.read()
    except UnicodeDecodeError:
        raise RunFileError(f"文件「{路径}」不是 UTF-8 编码，没法按文本读取")


def 读字节(路径) -> JishiBytes:
    """把整个文件读成**字节**（不做任何编码转换）。

    图片、压缩包、可执行文件这些二进制就得用它；文本才用 `读文本`。
    """
    p = _path(路径)
    if not p.is_file():
        raise RunFileError(f"找不到文件「{路径}」，没法读取")
    try:
        with open(p, "rb") as f:
            return JishiBytes(f.read())
    except OSError as e:
        raise RunFileError(f"读文件「{路径}」失败：{e}")


def 写字节(路径, 字节) -> None:
    """把字节写进文件（覆盖原有内容）。"""
    if not isinstance(字节, JishiBytes):
        raise RunFileError(
            f"「写字节」要传字节，得到了「{jishi_repr(字节, top=True)}」")
    try:
        with open(_path(路径), "wb") as f:
            f.write(字节.value)
    except OSError as e:
        raise RunFileError(f"写文件「{路径}」失败：{e}")


def 追加文本(路径, 内容: str) -> None:
    """把内容追加到文件末尾（文件不存在则创建）。"""
    with open(_path(路径), "a", encoding="utf-8", newline="") as f:
        f.write(jishi_repr(内容, top=True))


def 写行(路径, 行列表: list) -> None:
    """把列表里的每一行写进文件（自动加换行，覆盖原内容）。"""
    with open(_path(路径), "w", encoding="utf-8", newline="") as f:
        for 行 in 行列表:
            f.write(jishi_repr(行, top=True) + "\n")


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


def 原子写(路径, 内容: str) -> None:
    """**原子写入**：先写同目录下的临时文件，再改名覆盖目标。

    直接覆盖写有个隐患：写到一半程序崩了（或断电），目标文件就变成半截内容
    —— 账本、索引、配置这类「坏了就麻烦」的文件最怕这个。原子写保证
    **要么是完整的旧内容、要么是完整的新内容**，不会出现半截。

    做法：临时文件与目标**同目录**（保证改名是同盘操作，`os.replace` 才是
    原子的），写完再改名。任何一步出错都会清掉临时文件并把错误抛出来。
    """
    p = _path(路径)
    目录 = p.parent if str(p.parent) else _py_Path(".")
    值 = jishi_repr(内容, top=True)
    try:
        目录.mkdir(parents=True, exist_ok=True)
        fd, 临时 = _py_tempfile.mkstemp(
            dir=str(目录), prefix=p.name + ".", suffix=".tmp")
    except OSError as e:
        raise RunFileError(f"写「{路径}」失败：建不了临时文件（{e}）")
    try:
        with _py_os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(值)
        _py_os.replace(临时, str(p))
    except OSError as e:
        try:
            _py_os.unlink(临时)
        except OSError:
            pass
        raise RunFileError(f"写「{路径}」失败：{e}")


def 路径拼接(*部分) -> str:
    """把几段路径拼在一起，用 / 分隔。"""
    return str(_py_PurePosixPath(*[str(p) for p in 部分]))


def 文件名(路径) -> str:
    """取出路径里的文件名部分。"""
    return _py_Path(str(路径).replace("\\", "/")).name


def 扩展名(路径) -> str:
    """取出文件的扩展名（带点），例如 a.jsh → .jsh。"""
    return _py_Path(str(路径).replace("\\", "/")).suffix


def 追加字节(路径, 内容) -> int:
    """把字节**追加到文件末尾**（文件不存在就新建），返回写进了多少字节。

    与 `写字节` 的分工：`写字节` 是整份覆盖，这个是往后接 —— 分片下载、
    边跑边写日志流都用它。
    """
    # ⚠️ 基石里的字节是 `JishiBytes`（不是 Python 的 `bytes`）—— 与 `写字节` 同一个判据。
    if not isinstance(内容, JishiBytes):
        raise RunFileError(
            f"「追加字节」要的是**字节**（现在是「{jishi_repr(内容, top=True)}」）；"
            "文本请用 `追加文本`")
    p = _path(路径)
    try:
        with open(p, "ab") as f:
            n = f.write(内容.value)
    except OSError as e:
        raise RunFileError(f"往「{路径}」追加失败：{e}")
    return n


def 复制目录(源目录, 目标目录) -> str:
    """把**整棵目录树**复制到目标位置（源目录下的层级原样保留），返回目标路径。

    ⚠️ **目标已存在就报错** —— 自动合并/覆盖太容易把东西搞乱（`shutil.copytree`
    也是这个规矩）。要覆盖就先 `路径.递归删除(目标目录)`。
    """
    源 = _path(源目录)
    目标 = _path(目标目录)
    if not 源.is_dir():
        raise RunFileError(f"「{源目录}」不是目录，没法复制")
    if 目标.exists():
        raise RunFileError(
            f"「{目标目录}」已经存在，没法复制目录过去（先 `路径.递归删除` 它）")
    try:
        _py_shutil.copytree(str(源), str(目标))
    except OSError as e:
        raise RunFileError(f"复制目录「{源目录}」到「{目标目录}」失败：{e}")
    return str(目标)
