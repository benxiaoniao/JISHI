# -*- coding: utf-8 -*-
"""基石标准库：json。薄封装 Python 的 json，默认中文不转义。"""

from __future__ import annotations

import json as _py_json

from ..errors import RunValueError

__all__ = ["转文本", "解析", "读文件", "写文件"]


def 转文本(对象, 缩进: int = None) -> str:
    """把列表/字典等转成 JSON 文本（中文不转义成 \\uXXXX）。"""
    try:
        return _py_json.dumps(对象, ensure_ascii=False, indent=缩进,
                              default=str)
    except (TypeError, ValueError) as e:
        raise RunValueError(f"无法转成 JSON：{e}")


def 解析(文本) -> object:
    """把 JSON 文本解析成列表/字典等。"""
    try:
        return _py_json.loads(文本)
    except (TypeError, ValueError) as e:
        raise RunValueError(f"不是有效的 JSON 文本：{e}")


def 读文件(路径) -> object:
    """从文件读入并解析 JSON。"""
    import io
    # `newline=""`：不做换行转换。默认的通用换行会把 `\r\n` 悄悄改成 `\n`，
    # 与 Node 宿主（`fs.readFileSync`）和 Rust 宿主（`fs::read`）就对不上了（M33）
    with io.open(路径, "r", encoding="utf-8", newline="") as f:
        return 解析(f.read())


def 写文件(路径, 对象, 缩进: int = None) -> None:
    """把对象转成 JSON 并写入文件（中文不转义）。"""
    import io
    # 同 `读文件`：显式 `newline=""`，Windows 上不把 `\n` 变成 `\r\n`（M33）
    with io.open(路径, "w", encoding="utf-8", newline="") as f:
        f.write(转文本(对象, 缩进=缩进))
