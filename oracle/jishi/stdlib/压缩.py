# -*- coding: utf-8 -*-
"""基石标准库：压缩。薄封装 Python 的 zipfile / gzip，中文 API。

- **zip**（`打包` / `解压` / `列出内容`）：一箱多件，能装目录树。
- **gzip**（`压缩文件` / `解压文件`）：**单个文件**一个流，解压时不用先读目录表
  —— 日志轮转、备份单份产物常用它。

零第三方依赖。
"""

from __future__ import annotations

import gzip as _py_gzip
import os as _os
import zipfile as _py_zip

from ..errors import RunError, RunValueError
from ..runtime import jishi_repr, JishiBytes

__all__ = [
    "打包", "解压", "列出内容", "压缩文件", "解压文件", "解压成员",
]


def 打包(zip路径, *文件或目录) -> str:
    """把若干文件或目录打包进一个 zip。返回 zip 路径。

    目录会被递归收录；目录条目以「目录名/」作为前缀。
    """
    import os
    if not 文件或目录:
        raise RunValueError("打包至少需要提供一个文件或目录")
    # 允许第一参数是列表（基石调用常传列表）
    items = 文件或目录
    if len(items) == 1 and isinstance(items[0], (list, tuple)):
        items = tuple(items[0])
    try:
        with _py_zip.ZipFile(str(zip路径), "w", _py_zip.ZIP_DEFLATED) as zf:
            for item in items:
                p = str(item)
                if os.path.isdir(p):
                    for root, _, files in os.walk(p):
                        for fn in files:
                            full = os.path.join(root, fn)
                            arc = os.path.relpath(full, os.path.dirname(p))
                            zf.write(full, arc)
                elif os.path.isfile(p):
                    zf.write(p, os.path.basename(p))
                else:
                    raise RunValueError(f"「{p}」不存在，无法打包")
    except OSError as e:
        raise RunError(f"打包到「{zip路径}」失败：{e}")
    return str(zip路径)


def 解压(zip路径, 目标目录: str = "") -> str:
    """把 zip 解压到目标目录（默认当前目录），返回目标目录。"""
    import os
    target = str(目标目录) if 目标目录 else "."
    if not _os.path.isfile(str(zip路径)):
        raise RunValueError(f"「{zip路径}」不是文件")
    try:
        os.makedirs(target, exist_ok=True)
        with _py_zip.ZipFile(str(zip路径)) as zf:
            zf.extractall(target)
    except (_py_zip.BadZipFile, OSError) as e:
        raise RunError(f"解压「{zip路径}」失败：{e}")
    return target


def 列出内容(zip路径) -> list:
    """返回 zip 里的条目名列表（含目录，按原始顺序）。"""
    if not _os.path.isfile(str(zip路径)):
        raise RunValueError(f"「{zip路径}」不是文件")
    try:
        with _py_zip.ZipFile(str(zip路径)) as zf:
            return zf.namelist()
    except _py_zip.BadZipFile as e:
        raise RunError(f"「{zip路径}」不是有效的 zip：{e}")


def 压缩文件(gz路径, 内容) -> str:
    """把**一份内容**压成 `.gz` 单文件，返回这个 gz 的路径。

    与 `打包`（zip）的分工：zip 是「一箱多件、要目录表」，gzip 是「一个文件
    一个流」—— 日志轮转、单个产物存档用它更合适（解压端不用先解析目录表）。
    ⚠️ 文本按 UTF-8 编码后压；`解压文件` 再按 UTF-8 解回来。
    """
    值 = jishi_repr(内容, top=True)
    目标 = str(gz路径)
    目录 = _os.path.dirname(目标)
    try:
        if 目录:
            _os.makedirs(目录, exist_ok=True)
        # `mtime=0`：同一份内容每次压出来的**头部一个字节都不变**（否则 gzip
        # 会把当前时间写进头里，两份「内容相同」的产物就永远不一样）。
        # `filename=""`：**不写原始文件名** —— 传了 fileobj 时 gzip 默认会把
        # `fileobj.name` 写进头里的 FNAME 字段，于是「同一份内容存成不同的文件名」
        # 会压出不同的字节（Node / Rust 那边都不写 FNAME）。
        with open(目标, "wb") as f:
            with _py_gzip.GzipFile(filename="", fileobj=f, mode="wb",
                                   mtime=0) as gz:
                gz.write(值.encode("utf-8"))
    except OSError as e:
        raise RunError(f"压缩到「{gz路径}」失败：{e}")
    return 目标


def 解压文件(gz路径) -> str:
    """把 `.gz` 单文件解开，返回里面的文本（按 UTF-8 解码）。

    ⚠️ 解出来的**不是 UTF-8 文本**时会明确报错（基石里文本就是文本；
    要处理二进制请走 `编码` 模块）。
    """
    目标 = str(gz路径)
    if not _os.path.isfile(目标):
        raise RunValueError(f"「{gz路径}」不是文件")
    try:
        with _py_gzip.open(目标, "rb") as gz:
            原 = gz.read()
    except (OSError, EOFError) as e:
        raise RunError(f"解压「{gz路径}」失败：{e}")
    try:
        return 原.decode("utf-8")
    except UnicodeDecodeError:
        raise RunError(f"「{gz路径}」解出来的不是 UTF-8 文本，没法按文本返回")


def 解压成员(zip路径, 成员名) -> JishiBytes:
    """只取出 zip 里的**一个成员**，返回它的字节（不用把整包解开）。

    「备份包里我只想要里面那一份配置」就是这条 —— `解压` 会把整棵树铺到磁盘上，
    而这个只在内存里取一件。成员名要写 zip 里的完整路径（`列出内容` 给的就是它）。
    """
    目标 = str(zip路径)
    if not _os.path.isfile(目标):
        raise RunValueError(f"「{zip路径}」不是文件")
    名 = str(成员名)
    try:
        with _py_zip.ZipFile(目标) as zf:
            try:
                数据 = zf.read(名)
            except KeyError:
                # ⚠️ 只报**文件名**，不回显完整路径（M121 定）：
                #    ① 路径对用户没价值 —— 他是刚刚自己传进来的；
                #    ② 完整路径会让三宿主的报错**天然不一致**（临时目录带随机串、
                #       分隔符平台相关），而报错语料是要跨引擎逐字对拍的。
                raise RunValueError(
                    f"「{_os.path.basename(目标)}」里没有「{名}」这个成员"
                    "（先用 `压缩.列出内容` 看看有哪些）")
    except _py_zip.BadZipFile as e:
        raise RunError(f"「{目标}」不是有效的 zip：{e}")
    return JishiBytes(数据)
