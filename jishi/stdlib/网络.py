# -*- coding: utf-8 -*-
"""基石标准库：网络。薄封装 Python 的 urllib，中文 API + 安全超时。

零第三方依赖（不依赖 requests），默认超时 10 秒。
"""

from __future__ import annotations

import json as _py_json
import urllib.error as _py_urlerr
import urllib.request as _py_urllib

from ..errors import RunError, RunValueError

__all__ = ["获取", "提交", "获取JSON"]

_DEFAULT_TIMEOUT = 10.0


def 获取(网址, 超时: float = _DEFAULT_TIMEOUT) -> str:
    """用 GET 请求网址，返回响应文本。"""
    try:
        req = _py_urllib.Request(网址, headers={"User-Agent": "jishi/0.1"})
        with _py_urllib.urlopen(req, timeout=max(0.1, float(超时))) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except _py_urlerr.URLError as e:
        raise RunError(f"访问「{网址}」失败：{e.reason}")
    except Exception as e:  # noqa: BLE001
        raise RunError(f"访问「{网址}」出错：{e}")


def 提交(网址, 数据="", 超时: float = _DEFAULT_TIMEOUT) -> str:
    """用 POST 请求网址，提交表单数据（文本），返回响应文本。"""
    try:
        body = (数据 if isinstance(数据, bytes)
                else str(数据).encode("utf-8"))
        req = _py_urllib.Request(
            网址, data=body,
            headers={"User-Agent": "jishi/0.1",
                     "Content-Type": "application/x-www-form-urlencoded"})
        with _py_urllib.urlopen(req, timeout=max(0.1, float(超时))) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except _py_urlerr.URLError as e:
        raise RunError(f"提交到「{网址}」失败：{e.reason}")
    except Exception as e:  # noqa: BLE001
        raise RunError(f"提交到「{网址}」出错：{e}")


def 获取JSON(网址, 超时: float = _DEFAULT_TIMEOUT) -> object:
    """用 GET 请求一个返回 JSON 的接口，解析成列表/字典。"""
    try:
        return _py_json.loads(获取(网址, 超时=超时))
    except _py_json.JSONDecodeError as e:
        raise RunValueError(f"「{网址}」返回的不是有效 JSON：{e}")
