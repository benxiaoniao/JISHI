# -*- coding: utf-8 -*-
"""基石标准库：json。薄封装 Python 的 json，默认中文不转义。"""

from __future__ import annotations

import json as _py_json

from ..errors import RunTypeError, RunValueError
from ..runtime import JishiBytes, jishi_repr

__all__ = ["转文本", "解析", "读文件", "写文件", "有效"]


def _找字节(对象) -> None:
    """找结构里有没有字节：JSON 没有二进制，字节必须显式转文本（D79 §三）。"""
    if isinstance(对象, JishiBytes):
        raise RunValueError(
            "JSON 里放不了字节",
            hint="JSON 只有文本，先把字节转成文本再放，比如 编码.base64编码(字节)")
    if isinstance(对象, dict):
        for k, v in 对象.items():
            _找字节(k)
            _找字节(v)
    elif isinstance(对象, (list, tuple, set, frozenset)):
        for x in 对象:
            _找字节(x)


def 转文本(对象, 缩进: int = None) -> str:
    """把列表/字典等转成 JSON 文本（中文不转义成 \\uXXXX）。"""
    _找字节(对象)
    try:
        return _py_json.dumps(对象, ensure_ascii=False, indent=缩进,
                              default=str)
    except (TypeError, ValueError) as e:
        raise RunValueError(f"无法转成 JSON：{e}")


def _拒非常量(名):
    """拒绝 `NaN` / `Infinity` / `-Infinity`。

    ⚠️ Python 的 `json.loads` **默认接受**这三个（那是它的非标准扩展），
    而 JSON 标准（RFC 8259）不允许、JS 的 `JSON.parse` 也拒绝。
    ⇒ 不加这个钩子，「有效」与「解析」在 Python 上就会跟另外两个宿主分岔。
    """
    raise ValueError(f"`{名}` 不是合法的 JSON（JSON 标准只允许数字，不允许 NaN / Infinity）")


def 解析(文本) -> object:
    """把 JSON 文本解析成列表/字典等。

    ⚠️ **比 Python 的 `json.loads` 严**：`NaN` / `Infinity` 一律不收
    （它们本来就不是合法 JSON，只是 Python 顺手放过）。
    """
    try:
        return _py_json.loads(文本, parse_constant=_拒非常量)
    except (TypeError, ValueError):
        # ⚠️ M155：**文案里不带引擎自己的细节**。
        # Python 原来把 `Expecting property name enclosed in double quotes:
        # line 1 column 2 (char 1)` 整段塞进来（英文原文漏给用户），
        # Rust 有自己的中文细节、Node 又是另一句英文 —— 同一段坏 JSON
        # 三个宿主三种话，而且是**用户一眼能看懂几句英文**的问题。
        # 现在统一一句话；位置仍有 `json.解析(...)` 调用处的行列。
        raise RunValueError("不是有效的 JSON 文本")


def 有效(文本) -> bool:
    """这段文本是不是**合法的 JSON**（返回真/假，不抛错）。

    要「先问问再决定怎么处理」就用它 —— 直接 `json.解析` 会抛异常，
    比如「上游偶尔给一段坏数据，坏的就跳过」这种场合。

    口径与 `json.解析` **完全一致**（就是同一个解析器，只是把失败吞成假）：
    `NaN` / `Infinity` **不算**合法 JSON，末尾多逗号也不算。
    """
    if not isinstance(文本, str):
        # ⚠️ 文案要与 Rust / Node **逐字一致**（报错语料跨引擎比）。
        raise RunTypeError(f"「有效」要传文本（现在是「{jishi_repr(文本, top=True)}」）")
    try:
        _py_json.loads(文本, parse_constant=_拒非常量)
    except ValueError:
        return False
    return True


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
