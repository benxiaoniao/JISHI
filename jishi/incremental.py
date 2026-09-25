# -*- coding: utf-8 -*-
"""按行增量分词（M38 · 主线 B2）。

为什么需要它（**先量了再说**，别猜）：LSP 以前每收到一次编辑就把整份文件
tokenize + parse 一遍。实测每敲一个字符的成本是

| 文件行数 | tokenize | parse | 合计 |
|---------|---------:|------:|-----:|
| 253     |   6.1 ms | 3.8 ms | 9.9 ms |
| 1253    |  25.6 ms | 18.6 ms | 44 ms |
| 5003    | 103.6 ms | 77.9 ms | 182 ms |
| 20003   | 430 ms   | 328 ms  | 759 ms |

也就是**一千行以上就开始有可感的卡顿**（路线图里那句「大文件有卡顿」，
实测坐实）。而编辑只影响改动行之后的内容——改动行**之前**的 token 与词法
状态都不变（依据见 `tokenizer.LineAnchor`），所以前缀可以直接复用。

本模块只解决**分词**那一半。`parse` 目前仍是全量的（5000 行约 78 ms）：
增量解析要维护「每个语法节点的子树缓存 + 编辑后的失效范围」，复杂度高一个
量级，而当前收益只有几十毫秒。**先做成这一半、并如实标注另一半还没做**，
比做一个半成品强（`docs/lsp.md` 里有同样的说明）。

正确性不靠推理靠对拍：随机编辑下，增量结果必须与全量分词**逐项相同**
（`tests/test_m38_lsp_incremental.py`）。
"""

from __future__ import annotations

from typing import Optional

from .tokenizer import LineAnchor, Token, Tokenizer


def split_lines(text: str) -> list[str]:
    """与词法器**完全一致**的行切分（含「去掉末尾空行」那条规则）。

    行切分必须与 `Tokenizer` 对齐，否则 `lines` 与 `anchors` 会错位一格，
    增量复用的下标就全错了——这种错误不会立刻暴露，只会在某些编辑后
    「诊断莫名其妙对不上」。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


class IncrementalDocument:
    """一份源码的「可增量重新分词」表示。

    用法：``update(新文本)``；返回**重新分词的起始行号**（0 起），
    文本没变则返回 ``None``（调用方据此跳过整个分析）。
    """

    def __init__(self, text: str, filename: str = "<输入>") -> None:
        self.filename = filename
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = split_lines(self.text)
        tk = Tokenizer(self.text, filename)
        self.tokens: list[Token] = tk.tokenize()
        self.anchors: "Optional[list[LineAnchor]]" = tk.anchors
        #: 上一次重新分词的起始行号
        self.last_start: Optional[int] = None
        #: 上一次重新分词覆盖的行数（度量用：测试断言「只重算后缀」）
        self.last_relexed = len(self.lines)

    # -- 增量入口 -----------------------------------------------------------

    def update(self, text: str) -> Optional[int]:
        """文本变了就增量重分词，返回重分词的起始行号；没变返回 ``None``。

        **出错时的状态是有意这样设计的**：一次编辑写出了临时性的缩进错
        （编辑途中很常见——比如正在缩进/取消缩进某一行）会让重分词抛错，
        此时文本要跟上（否则下次算出来的「改动行」是错的），而锚点必须
        **作废**（它对应的是旧文本），下次走全量。也就是说：失败之后这个
        对象仍然是自洽的，只是退化成了「下次全量」。
        """
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if text == self.text and self.anchors is not None:
            return None
        new_lines = split_lines(text)
        if self.anchors is None:            # 上次分词失败过：只能全量重来
            return self._full(text)

        old_lines = self.lines
        # 共同前缀：第一处不同的行就是「改动行」。**按行文本比较**而不是听
        # 客户端说的改动范围——客户端给的 range 只对「它自己的文本」成立，
        # 而我们这边的文本可能已经因为版本乱序之类的原因不一样了；自己算
        # 出来的前缀是**自证**的（前缀相同的行，分词结果必然相同）。
        limit = min(len(old_lines), len(new_lines))
        start = 0
        while start < limit and old_lines[start] == new_lines[start]:
            start += 1

        anchor = self._anchor_for(start)
        tk = Tokenizer(text, self.filename)
        try:
            tail, tail_anchors = tk._run(start, anchor, anchor.token_index)
        except Exception:                    # noqa: BLE001 —— 状态要自洽再上抛
            self.text, self.lines = text, new_lines
            self.anchors = None
            self.last_start, self.last_relexed = 0, len(new_lines)
            raise

        self.tokens = self.tokens[:anchor.token_index] + tail
        self.anchors = self.anchors[:start] + tail_anchors
        self.lines = new_lines
        self.text = text
        self.last_start = start
        self.last_relexed = max(0, len(new_lines) - start)
        return start

    def _full(self, text: str) -> int:
        """全量重分词（首次、或上次失败后的兜底）。"""
        self.text = text
        self.lines = split_lines(text)
        tk = Tokenizer(text, self.filename)
        self.tokens = tk.tokenize()          # 再抛就交给调用方（它会报诊断）
        self.anchors = tk.anchors
        self.last_start = 0
        self.last_relexed = len(self.lines)
        return 0

    def _anchor_for(self, start: int) -> LineAnchor:
        """取「进入第 start 行」的锚点。

        锚点列表长度是 `旧行数 + 1`（末尾那个是 EOF 锚点），所以
        `start == 旧行数`（在文件末尾追加）时取到的正是 EOF 锚点。
        """
        idx = min(max(0, start), len(self.anchors) - 1)
        return self.anchors[idx]
