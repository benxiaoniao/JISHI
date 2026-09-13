# -*- coding: utf-8 -*-
"""基石标准库：加密。薄封装 Python 的 hashlib，中文 API。

提供常见的摘要算法（md5 / sha1 / sha256 / sha512）。零第三方依赖。
"""

from __future__ import annotations

import hashlib as _py_hashlib

from ..errors import RunValueError
from ..runtime import jishi_repr

__all__ = [
    "摘要", "md5", "sha1", "sha256", "sha512",
    "文件摘要", "支持的算法",
]

# 算法名 → hashlib 构造器
_算法表 = {
    "md5": _py_hashlib.md5,
    "sha1": _py_hashlib.sha1,
    "sha256": _py_hashlib.sha256,
    "sha512": _py_hashlib.sha512,
}


def 摘要(文本, 算法: str = "sha256") -> str:
    """返回文本的摘要（十六进制小写）。算法可为 md5/sha1/sha256/sha512。"""
    algo = str(算法).lower().replace("-", "").replace("_", "")
    if algo not in _算法表:
        raise RunValueError(
            f"不支持的摘要算法「{算法}」，可选：md5/sha1/sha256/sha512")
    data = (文本 if isinstance(文本, bytes)
            else jishi_repr(文本, top=True).encode("utf-8"))
    return _算法表[algo](data).hexdigest()


def md5(文本) -> str:
    """返回文本的 MD5 摘要。"""
    return 摘要(文本, "md5")


def sha1(文本) -> str:
    """返回文本的 SHA-1 摘要。"""
    return 摘要(文本, "sha1")


def sha256(文本) -> str:
    """返回文本的 SHA-256 摘要。"""
    return 摘要(文本, "sha256")


def sha512(文本) -> str:
    """返回文本的 SHA-512 摘要。"""
    return 摘要(文本, "sha512")


def 文件摘要(文件路径, 算法: str = "sha256") -> str:
    """返回文件的摘要（分块读取，不一次性载入内存）。"""
    algo = str(算法).lower().replace("-", "").replace("_", "")
    if algo not in _算法表:
        raise RunValueError(
            f"不支持的摘要算法「{算法}」，可选：md5/sha1/sha256/sha512")
    h = _算法表[algo]()
    try:
        with open(str(文件路径), "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError as e:
        raise RunValueError(f"读文件「{文件路径}」失败：{e}")
    return h.hexdigest()


def 支持的算法() -> list:
    """返回可用的摘要算法名列表。"""
    return sorted(_算法表.keys())
