# -*- coding: utf-8 -*-
"""基石标准库：标识。生成唯一标识（uuid 式）与短随机串。

**为什么需要**：生成唯一文件名 / 任务 ID / 临时目录名是很常见的需求，
在此之前只能用「时间戳 + 计数」凑——同一毫秒内会撞、也看不出随机性。

约定与其它标准库一致：薄封装 Python 标准库，**语义在三执行器 + 两宿主上对齐**；
函数名用中文，报错中文。
"""

from __future__ import annotations

import uuid as _py_uuid

from ..errors import RunValueError

__all__ = ["唯一标识", "唯一标识短", "唯一标识字节"]


def 唯一标识(去掉横线: bool = False) -> str:
    """返回一个全球唯一标识（UUID 第 4 版，随机生成）。

    默认带横线（`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`，36 个字符）；
    传 `真` 则去掉横线（32 个字符），适合当文件名。
    """
    u = str(_py_uuid.uuid4())
    return u.replace("-", "") if 去掉横线 else u


def 唯一标识短() -> str:
    """返回一个**较短**的唯一标识（22 个字符，URL 安全）。

    用 UUID 的 128 位随机数做 base64url 编码，去掉了填充符。
    比 `唯一标识()` 短，撞的概率仍然可以忽略（2^128 空间）。
    适合当 URL 片段、短链接后缀。
    """
    u = _py_uuid.uuid4()
    return _b64url(u.bytes)


def 唯一标识字节() -> list:
    """返回 16 个字节（0-255 的整数列表）形式的唯一标识。

    需要把标识写进二进制格式、或自己再做编码时用。
    """
    return list(_py_uuid.uuid4().bytes)


#: base64url 的字符表（`-` `_` 而不是 `+` `/`，且不带 `=` 填充）
_B64URL = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _b64url(data: bytes) -> str:
    """把字节串编码成 base64url（去掉填充）。"""
    out = []
    n = len(data)
    for i in range(0, n, 3):
        chunk = data[i:i + 3]
        bits = 0
        for j, b in enumerate(chunk):
            bits |= b << (16 - 8 * j)
        out.append(_B64URL[(bits >> 18) & 63])
        out.append(_B64URL[(bits >> 12) & 63])
        if len(chunk) > 1:
            out.append(_B64URL[(bits >> 6) & 63])
        if len(chunk) > 2:
            out.append(_B64URL[bits & 63])
    return "".join(out)
