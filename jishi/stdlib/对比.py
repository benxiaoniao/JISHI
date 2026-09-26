# -*- coding: utf-8 -*-
"""基石标准库：对比。文本差异（行级 diff）。

**为什么需要**：工具链与测试都要「两份文本差在哪」——改配置文件后想看看改了
什么、跑测试时想把「期望 vs 实际」打清楚。自己写 diff 是明显的绕道
（`difflib` 有现成的，本模块是它的中文薄封装）。

输出是**统一的文本形式**（带 `+`/`-` 前缀），因为基石目前没有「结构化 diff 对象」
这种类型；要自己处理再拿 `对比.逐行()` 的原始结果。
"""

from __future__ import annotations

import difflib as _py_difflib

from ..errors import RunTypeError

__all__ = ["逐行", "统一", "并排", "相似度", "最相似"]


def _lines(值, 名字: str) -> list:
    """把输入统一成「行的列表」：文本按行拆，列表直接用。"""
    if isinstance(值, str):
        return 值.splitlines()
    if isinstance(值, (list, tuple)):
        out = []
        for i, v in enumerate(值):
            if not isinstance(v, str):
                raise RunTypeError(
                    f"「{名字}」的第 {i + 1} 项不是文本：{v!r}")
            out.append(v)
        return out
    raise RunTypeError(f"「{名字}」要传文本或文本列表")


def 逐行(旧, 新, 带行号: bool = False) -> list:
    """返回逐行的差异说明列表，每项形如 `["+", 行号, 内容]` 或 `[" ", 行号, 内容]`。

    - `" "` 两边都有、`"-"` 只在旧里有、`"+"` 只在新里有
    - 行号是**新文本**里的行号（删除的行给的是它原来的位置）
    - `带行号=假` 时行号位是 0

    要自己渲染（上色、写进报告）时用这个；只想看文本就用 `统一()`。
    """
    a = _lines(旧, "逐行")
    b = _lines(新, "逐行")
    out = []
    for tag, i1, i2, j1, j2 in _py_difflib.SequenceMatcher(
            None, a, b).get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                out.append([" ", (j1 + k - i1 + 1) if 带行号 else 0, a[k]])
        elif tag == "delete":
            for k in range(i1, i2):
                out.append(["-", (j1 + 1) if 带行号 else 0, a[k]])
        elif tag == "insert":
            for k in range(j1, j2):
                out.append(["+", (k + 1) if 带行号 else 0, b[k]])
        else:                                   # replace：先全删再全插
            for k in range(i1, i2):
                out.append(["-", (j1 + 1) if 带行号 else 0, a[k]])
            for k in range(j1, j2):
                out.append(["+", (k + 1) if 带行号 else 0, b[k]])
    return out


def 统一(旧, 新, 上下文: int = 3) -> str:
    """返回**统一格式**的差异文本（`--- 旧` / `+++ 新` / `@@` 分块 / `+`-` 行）。

    这是最常用的形式——`git diff` 看到的就是这个。
    `上下文` 是每块差异前后保留几行相同内容（0 就只列差异行）。
    """
    a = _lines(旧, "统一")
    b = _lines(新, "统一")
    if not isinstance(上下文, int) or isinstance(上下文, bool) or 上下文 < 0:
        raise RunTypeError("「上下文」要传 0 或正整数")
    lines = list(_py_difflib.unified_diff(a, b, "旧", "新",
                                          lineterm="", n=上下文))
    return "\n".join(lines)


def 并排(旧, 新, 宽: int = 30) -> str:
    """返回**左右并排**的差异文本，适合打给人看（`|` 分隔两栏）。

    `宽` 是每栏的显示宽度；超出的部分截断并加 `…`。
    """
    if not isinstance(宽, int) or isinstance(宽, bool) or 宽 < 4:
        raise RunTypeError("「宽」要传不小于 4 的整数")
    a = _lines(旧, "并排")
    b = _lines(新, "并排")

    def 截(s: str) -> str:
        # 中文按两个宽度算，这样两栏才对得齐
        w = sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)
        if w <= 宽:
            return s + " " * (宽 - w)
        out, acc = [], 0
        for ch in s:
            cw = 2 if ord(ch) > 0x2E80 else 1
            if acc + cw > 宽 - 1:
                break
            out.append(ch)
            acc += cw
        return "".join(out) + "…" + " " * max(0, 宽 - acc - 1)

    out = []
    for tag, i1, i2, j1, j2 in _py_difflib.SequenceMatcher(
            None, a, b).get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                out.append(f"{截(a[k])} | {截(b[j1 + k - i1])}")
            continue
        n = max(i2 - i1, j2 - j1)
        for k in range(n):
            left = a[i1 + k] if i1 + k < i2 else ""
            right = b[j1 + k] if j1 + k < j2 else ""
            mark = " " if left == right else "~"
            out.append(f"{截(left)} {mark}| {截(right)}")
    return "\n".join(out)


def 相似度(甲, 乙) -> float:
    """返回两段文本的相似度（0 到 1；1 表示完全一样）。

    按**行**比较。用于「这份输出和期望的差多少」这种判断。
    """
    a = _lines(甲, "相似度")
    b = _lines(乙, "相似度")
    return _py_difflib.SequenceMatcher(None, a, b).ratio()


def 最相似(目标, 候选) -> str:
    """从候选列表里挑出与目标**最相似**的那一项（并列时取最靠前的）。

    拼写建议、纠错提示常用。
    """
    if not isinstance(目标, str):
        raise RunTypeError("「最相似」的第一个参数要传文本")
    items = _lines(候选, "最相似")
    if not items:
        return ""
    best, best_r = items[0], -1.0
    for it in items:
        r = _py_difflib.SequenceMatcher(None, 目标, it).ratio()
        if r > best_r:
            best, best_r = it, r
    return best
