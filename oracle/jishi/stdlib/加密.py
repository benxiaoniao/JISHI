# -*- coding: utf-8 -*-
"""基石标准库：加密。薄封装 Python 的 hashlib，中文 API。

提供常见的摘要算法（md5 / sha1 / sha256 / sha512）。零第三方依赖。
"""

from __future__ import annotations

import hashlib as _py_hashlib
import hmac as _py_hmac
import secrets as _py_secrets

from ..errors import RunValueError, RunError
from ..runtime import jishi_repr, JishiBytes

__all__ = [
    "摘要", "md5", "sha1", "sha256", "sha512",
    "文件摘要", "HMAC", "口令摘要", "安全随机字节", "支持的算法",
]

#: 口令摘要默认迭代次数。
#:
#: 为什么是 10 万：口令通常又短又弱，**单次摘要挡不住离线穷举** ——
#: 迭代次数就是「每次猜的代价」。10 万次是当前（2026 年）的常见下限。
#: ⚠️ 迭代次数要**跟摘要一起存下来**，否则以后提高下限老口令就验不过。
PBKDF2_默认迭代 = 100000

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


def _归一摘要算法(算法) -> str:
    """摘要算法名 → 归一化（md5/sha1/sha256/sha512），不认就报错。

    `文件摘要` 与 `并发.批量摘要` 共用这一条，**保证「空列表」与
    「列表里每一项」报的是同一句话**（M114）。
    """
    algo = str(算法).lower().replace("-", "").replace("_", "")
    if algo not in _算法表:
        raise RunValueError(
            f"不支持的摘要算法「{算法}」，可选：md5/sha1/sha256/sha512")
    return algo


def 文件摘要(文件路径, 算法: str = "sha256") -> str:
    """返回文件的摘要（分块读取，不一次性载入内存）。"""
    algo = _归一摘要算法(算法)
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


def _归一算法(算法, 动作: str) -> str:
    algo = str(算法).lower().replace("-", "").replace("_", "")
    if algo not in _算法表:
        raise RunValueError(
            f"{动作}不支持的算法「{算法}」，可选：md5/sha1/sha256/sha512")
    return algo


def _字节(值) -> bytes:
    """值 → 字节：字节原样，其余按 `jishi_repr(top=True)` 再 UTF-8（与 `摘要` 同）。"""
    if isinstance(值, JishiBytes):
        return 值.value
    if isinstance(值, bytes):
        return 值
    return jishi_repr(值, top=True).encode("utf-8")


def HMAC(键, 文本, 算法: str = "sha256") -> str:
    """用**密钥**给文本做 HMAC 签名（十六进制小写）。

    摘要回答「内容变没变」，HMAC 还回答「**是不是知道密钥的人做的**」——
    清单、回执、回调这类要防篡改的地方用它（光有摘要，谁都能重算一遍）。
    """
    algo = _归一算法(算法, "HMAC ")
    return _py_hmac.new(_字节(键), _字节(文本), algo).hexdigest()


# ⚠️ 默认值写字面量（同 HTTP服务端 那条）：语言卡从 AST 反解签名，
#    常量名会漏进卡片。改这里的数就同步改 PBKDF2_默认迭代。
def 口令摘要(口令, 盐, 迭代: int = 100000, 算法: str = "sha256") -> str:
    """把**口令**揉进摘要（PBKDF2），十六进制小写。存口令只该存它。

    `盐` 是每个口令一份的随机串（用 `安全随机字节(16)` 生成），它的作用是
    让「同一个口令」在不同账号上算出**不同的摘要** —— 否则一撞库就全暴露。

    ⚠️ **`迭代` 与 `盐` 要跟摘要一起存下来**：以后提高迭代次数时，老口令
    得用老参数才验得过。
    """
    algo = _归一算法(算法, "口令摘要 ")
    次数 = int(迭代)
    if 次数 < 1:
        raise RunValueError(f"迭代次数要是正整数，现在是 {迭代}")
    return _py_hashlib.pbkdf2_hmac(algo, _字节(口令), _字节(盐), 次数).hex()


def 安全随机字节(长度: int) -> JishiBytes:
    """生成一串**不可预测**的随机字节，返回**字节**。

    与 `随机` 模块的区别：那个是伪随机、**明说了不能做密码学用途**；
    这个取的是操作系统的熵源，适合做盐、令牌、回调随机串。
    M113 起返回真正的字节（以前返回十六进制文本——那是没有字节类型时的凑合）。
    要十六进制文本就再套一层 `编码.十六进制编码(...)`。
    """
    n = int(长度)
    if n < 1:
        raise RunValueError(f"长度要是正整数，现在是 {长度}")
    try:
        return JishiBytes(_py_secrets.token_bytes(n))
    except (OSError, NotImplementedError) as e:      # 没有熵源
        raise RunError(f"取不到系统随机源：{e}")
