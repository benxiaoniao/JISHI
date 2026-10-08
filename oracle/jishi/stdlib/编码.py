# -*- coding: utf-8 -*-
"""基石标准库：编码。base64 / 十六进制 / URL 的编解码。

**为什么需要**：数据交换到处都要编解码，而 `加密` 模块只有摘要（md5/sha…）——
想把图片存成文本、把中文放进 URL、把字节打印出来，之前都得自己手写。

名字里的「编码」两字容易和「字符编码（UTF-8/GBK）」混，所以字符编码相关的
函数单独一组，名字带「文本」前缀，一看就知道是干什么的。
"""

from __future__ import annotations

import base64 as _py_b64
import binascii as _py_binascii
import urllib.parse as _py_url

from ..errors import RunValueError

__all__ = [
    "base64编码", "base64解码", "十六进制编码", "十六进制解码",
    "url编码", "url解码", "文本字节", "字节文本",
]


def _to_bytes(值, 名字: str) -> bytes:
    """文本按 UTF-8 转字节；已经是字节列表的就直接用。"""
    if isinstance(值, str):
        return 值.encode("utf-8")
    if isinstance(值, bytes):
        return 值
    if isinstance(值, list):
        out = bytearray()
        for i, v in enumerate(值):
            if isinstance(v, bool) or not isinstance(v, int):
                raise RunValueError(f"「{名字}」里第 {i + 1} 个不是 0-255 的整数")
            if not 0 <= v <= 255:
                raise RunValueError(
                    f"「{名字}」里第 {i + 1} 个超出范围（要 0-255）：{v}")
            out.append(v)
        return bytes(out)
    raise RunValueError(f"「{名字}」要传文本、字节列表或 bytes")


def _want_bytes(值, 名字: str) -> bytes:
    """解码类函数**只收**文本/字节——列表也能收，但要说清楚是啥。"""
    if isinstance(值, str):
        # 解码输入必须是 ASCII 可见字符（base64 / hex 都是）；中文会报错，
        # 这里提前给一句人话，而不是让底层抛 UnicodeEncodeError。
        try:
            return 值.encode("ascii")
        except UnicodeEncodeError:
            raise RunValueError(
                f"「{名字}」的内容应当是 base64 / 十六进制字符，"
                f"但里面出现了非 ASCII 字符")
    if isinstance(值, bytes):
        return 值
    if isinstance(值, list):
        return _to_bytes(值, 名字)
    raise RunValueError(f"「{名字}」要传文本或字节列表")


def base64编码(值) -> str:
    """把文本或字节编码成 base64 文本（标准字母表，**带 `=` 填充**）。

    base64 用 64 个可见字符表示任意字节，适合把二进制塞进文本协议
    （JSON / 配置文件 / URL 参数）。代价是体积涨约 1/3。
    """
    return _py_b64.b64encode(_to_bytes(值, "base64编码")).decode("ascii")


def base64解码(值):
    """把 base64 文本还原成**字节列表**（0-255 的整数）。

    返回列表而不是文本：base64 里装的可能是任意二进制（图片、压缩包…），
    不一定能当文本读。要当文本用就再套一层 `编码.字节文本()`。
    """
    raw = _want_bytes(值, "base64解码")
    try:
        return list(_py_b64.b64decode(raw, validate=True))
    except (_py_binascii.Error, ValueError) as e:
        raise RunValueError(f"这不是合法的 base64 内容：{e}")


def 十六进制编码(值, 大写: bool = False) -> str:
    """把文本或字节编码成十六进制文本。默认小写。"""
    hexed = _to_bytes(值, "十六进制编码").hex()
    return hexed.upper() if 大写 else hexed


def 十六进制解码(值):
    """把十六进制文本还原成**字节列表**。

    允许中间有空格和 `0x` 前缀（从别处抄来的十六进制常常带着这些），
    也允许奇数长度（前面补一个 0，`"abc"` 当成 `0abc`）。
    """
    raw = _want_bytes(值, "十六进制解码")
    text = raw.decode("ascii", "strict").replace(" ", "").replace("\t", "")
    text = text.replace("0x", "").replace("0X", "").replace(",", "")
    if not text:
        return []
    if len(text) % 2:
        text = "0" + text
    try:
        return list(bytes.fromhex(text))
    except ValueError as e:
        raise RunValueError(f"这不是合法的十六进制内容：{e}")


def url编码(值, 保留斜杠: bool = True) -> str:
    """把文本编码成可以安全放进 URL 的形式（中文会变成 `%E4%B8%AD` 这样）。

    默认**保留 `/`**（拼路径时常用）；要整段当查询参数传就设 `保留斜杠=假`。
    """
    safe = "/" if 保留斜杠 else ""
    return _py_url.quote(str(值), safe=safe)


def url解码(值) -> str:
    """把 URL 编码的文本还原（`%E4%B8%AD` → `中`）。"""
    if not isinstance(值, str):
        raise RunValueError("「url解码」要传文本")
    try:
        return _py_url.unquote(值, errors="strict")
    except (UnicodeDecodeError, ValueError) as e:
        raise RunValueError(f"这不是合法的 URL 编码内容：{e}")


def 文本字节(值) -> list:
    """把文本按 UTF-8 转成字节列表（0-255 的整数）。

    想「看一个中文字占几个字节」或手动处理二进制时用。
    """
    if not isinstance(值, str):
        raise RunValueError("「文本字节」要传文本")
    return list(值.encode("utf-8"))


def 字节文本(值) -> str:
    """把字节列表按 UTF-8 还原成文本。

    解不出合法 UTF-8（比如截断了一半的中文）时**明确报错**，不返回乱码。
    """
    raw = _to_bytes(值, "字节文本")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RunValueError(
            f"这些字节不是合法的 UTF-8 文本（可能被截断或本来就是二进制）：{e}")
