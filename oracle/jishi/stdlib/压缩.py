# -*- coding: utf-8 -*-
"""基石标准库：压缩。薄封装 Python 的 zipfile，中文 API。

提供 zip 的打包 / 解压 / 查看。零第三方依赖。
"""

from __future__ import annotations

import os as _os
import zipfile as _py_zip

from ..errors import RunError, RunValueError

__all__ = ["打包", "解压", "列出内容"]


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
