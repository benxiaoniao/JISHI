# -*- coding: utf-8 -*-
"""基石语言的词法分析器。

核心职责：
1. 缩进栈：生成 INDENT / DEDENT（Python 式缩进敏感）
2. 全角标点归一化（字符串与注释之外），U+3000 缩进报错，Tab/空格混用报错
3. 中文分词消歧：一段连续 CJK 恰等于关键字才算关键字；
   若以关键字为前缀（如「如果分数」）则报"粘连"错误并给出建议
4. 行列精确定位（基于原始源码，供中文报错渲染）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .errors import (
    LexCharError,
    LexFullWidthSpaceError,
    LexIndentError,
    LexKeywordGlueError,
    LexStringError,
    LexTabError,
    normalize_fullwidth,
)

# ---------------------------------------------------------------------------
# 字符类别
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_ASCII_START = re.compile(r"[A-Za-z_]")
_ASCII_PART = re.compile(r"[A-Za-z0-9_]")
_DIGIT = re.compile(r"[0-9]")

# ---------------------------------------------------------------------------
# 关键字表
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, str] = {
    # 词法意义：值是该关键字在语法里的身份
    "令": "VAR",            # 变量声明 / 赋值
    "如果": "IF",
    "否则": "ELSE",
    "否则如果": "ELIF",
    "遍历": "FOR",
    "循环": "LOOP",         # 循环 N 次
    "当": "WHILE",
    "中断": "BREAK",
    "继续": "CONTINUE",
    "函数": "FUNC",
    "返回": "RETURN",
    "导入": "IMPORT",
    "在": "IN",             # 遍历 x 在 列表
    "从": "FROM",           # 导入 x 从 python
    "为": "AS",             # 导入 x 从 python 为 y
    "真": "TRUE",
    "假": "FALSE",
    "空": "NONE",
    "与": "AND",
    "或": "OR",
    "非": "NOT",
    "是": "IS",             # 是实例判断（M23.2）：甲 是 空
    "不是": "IS_NOT",       # M23.2
    "不在": "NOT_IN",       # M23.2：x 不在 列表
    "次": "CI",             # 循环 N 次 中的助词
    # 预留给后续阶段，M0 解析到会提示"该功能将在后续版本支持"
    "类": "CLASS",
    "继承": "EXTENDS",
    "新建": "NEW",
    "尝试": "TRY",
    "捕获": "EXCEPT",
    "最终": "FINALLY",
    "抛出": "RAISE",
    "用": "WITH",           # 上下文管理器（M26.2）：用 打开(…) 为 f：
    "匹配": "MATCH",        # 模式匹配（M34.1）：匹配 x：情形 1：
    "情形": "CASE",         # 模式匹配的分支（M34.1）
    "枚举": "ENUM",         # 枚举（M34.2）：枚举 颜色：红 = 1
}

#: 不做「关键字粘连」检查的关键字。
#:
#: 粘连检查（`如果分数` → 提示「你是不是想写 如果 分数」）对多数关键字很有用，
#: 但对以下这些是**净损失**：它们本身就是常用词的前缀，正常标识符会大面积误报。
#: 例如 `不在列表`、`不是我`、`是实例`（内建名）——这些是合理变量名，
#: 不该被当成「关键字粘连」而报错。宁可不提示，也不能拦下正确代码。
#: `用`（M26.2）同理：「用户」「用途」「用力」都是正常标识符。
#: `匹配`（M34.1）同理：「匹配组」「匹配器」「匹配度」在数据/文本处理里
#: 是高频变量名（真实项目「网页采集」里就有一个 `匹配组`）。
#: `枚举`（M34.2）同理：「枚举值」「枚举类」「枚举型」都很常见。
_GLUE_CHECK_SKIP = frozenset({"不是", "不在", "是", "用", "匹配", "枚举"})


# 多字符运算符（按长度降序匹配）
OPERATORS = [
    "**", "//", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "//=", "%=", "**=",
    "->",                       # M24.3 返回类型标注：函数 f() -> 整数：
    "+", "-", "*", "/", "%", "<", ">", "=", "(", ")", "[", "]", "{", "}", ",", ".",
    ":", "^",
]

#: 按长度降序的运算符表，供最长匹配使用（M25 修正）。
#: 用 sorted 而不是手排列表：以后往 OPERATORS 里加运算符时不必再操心顺序，
#: 也不会因为「短运算符写在前面」把长运算符吃掉。
_OPERATORS_LONGEST_FIRST = sorted(OPERATORS, key=len, reverse=True)

# ---------------------------------------------------------------------------
# Token 定义
# ---------------------------------------------------------------------------

TOKEN_TYPES = {
    "INDENT", "DEDENT", "NEWLINE", "NUMBER", "STRING", "NAME",
    "KEYWORD", "OP", "EOF",
}


@dataclass
class Token:
    type: str
    value: object
    line: int = 1
    col: int = 1
    end_col: int = 1
    source_line: str = ""

    def __repr__(self) -> str:
        return f"Token({self.type}, {self.value!r}, {self.line}:{self.col})"


@dataclass(frozen=True)
class LineAnchor:
    """进入某一**行之前**的词法状态（M38 B2，供增量分词复用）。

    为什么可以这样复用的依据很朴素：**处理第 N 行时需要的全部历史，就是
    这七个值**——缩进栈、括号深度、跨行三引号累积的内容与起点、以及
    「正处在多行注释里」这个标志。行文本本身在 ``self.lines`` 里。

    于是「只改了第 L 行之后的内容」时，直接把 L 之前的 token 拿来用、
    从 L 的锚点续跑即可，不必从头分词（实测 5000 行文件从 104ms 降到约一半）。
    正确性由对拍保证：随机编辑下增量结果必须与全量分词**逐项相同**。
    """

    token_index: int                 # 本行第一个 token 在整份 token 流里的下标
    indent_stack: tuple
    bracket_depth: int
    pending_triple: "Optional[tuple]"
    pending_quote: str
    pending_line: int
    pending_col: int
    in_block_comment: bool


# ---------------------------------------------------------------------------
# 词法分析器
# ---------------------------------------------------------------------------

class Tokenizer:
    def __init__(self, text: str, filename: str = "<输入>"):
        # 统一换行
        self.filename = filename
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        # 开头的 BOM（U+FEFF）：Windows 记事本等编辑器存「UTF-8 带 BOM」时会写上它，
        # 用户看不见，却会被词法器判成「无法识别的字符」（M38 B4 实测：带 BOM 的
        # 文件连跑都跑不起来）。**只剥开头那一个**——文件中间的 BOM 仍按非法字符报错，
        # 否则「不可见字符混进代码」这类问题会被悄悄放过去。
        if self.text.startswith("\ufeff"):
            self.text = self.text[1:]
        self.lines = self.text.split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()  # 去掉末尾空行
        # 跨行三引号字符串状态
        self._pending_triple: Optional[list[str]] = None
        self._pending_quote: str = ""
        self._pending_line: int = 1
        self._pending_col: int = 1

    # -- 主流程 ------------------------------------------------------------

    def tokenize(self) -> list[Token]:
        """全量分词，并把**每一行的入行状态**记进 ``self.anchors``。

        锚点只在增量分词（``jishi.incremental``）里用；普通调用者忽略它即可。
        """
        tokens, anchors = self._run(0, None, 0)
        self.anchors = anchors
        return tokens

    def _run(self, start_idx: int, state: "Optional[LineAnchor]",
             token_offset: int = 0) -> "tuple[list[Token], list[LineAnchor]]":
        """从第 ``start_idx`` 行、按 ``state`` 给出的词法状态跑到文件末尾。

        ``token_offset`` 是这批 token 在**整份 token 流**里的起始下标：
        增量分词时前半段是复用来的，锚点里记的必须是绝对下标，否则第二次
        增量就会错位。
        """
        tokens: list[Token] = []
        anchors: list[LineAnchor] = []
        if state is None:
            indent_stack: list[int] = [0]
            bracket_depth = 0
            in_block_comment = False
            self._pending_triple = None
            self._pending_quote = ""
            self._pending_line = 1
            self._pending_col = 1
        else:
            indent_stack = list(state.indent_stack)
            bracket_depth = state.bracket_depth
            in_block_comment = state.in_block_comment
            self._pending_triple = (None if state.pending_triple is None
                                    else list(state.pending_triple))
            self._pending_quote = state.pending_quote
            self._pending_line = state.pending_line
            self._pending_col = state.pending_col

        def anchor() -> LineAnchor:
            return LineAnchor(
                token_offset + len(tokens), tuple(indent_stack), bracket_depth,
                None if self._pending_triple is None
                else tuple(self._pending_triple),
                self._pending_quote, self._pending_line, self._pending_col,
                in_block_comment)

        line_idx = start_idx
        n_lines = len(self.lines)

        while line_idx < n_lines:
            raw = self.lines[line_idx]
            line_no = line_idx + 1
            anchors.append(anchor())

            # 跨行三引号字符串延续
            if self._pending_triple is not None:
                tok, rest, done = self._continue_triple(raw, line_no)
                if done:
                    tokens.append(tok)
                    for t in rest:
                        if t.type == "OP" and t.value in "([{":
                            bracket_depth += 1
                        elif t.type == "OP" and t.value in ")]}":
                            bracket_depth = max(0, bracket_depth - 1)
                        tokens.append(t)
                    if bracket_depth == 0:
                        # 收尾引号后面即使**什么都没有**也要收这一行（M39）：
                        # 之前这里多一个 `and rest`，于是
                        #     令 a = """第一行
                        #     第二行
                        #     """
                        # （收尾引号独占一行——最自然的写法）不会补 NEWLINE，
                        # 解析器接着把下一行当成同一句，报「这一行没写完」。
                        # 一个「文档里没写、测试没跑过、一写就报错」的坑。
                        tokens.append(Token("NEWLINE", "\n", line_no, 1, 1, raw))
                line_idx += 1
                continue

            # 多行注释：跳过整行（不参与缩进处理）
            if in_block_comment:
                end = raw.find("-#")
                if end >= 0:
                    in_block_comment = False
                line_idx += 1
                continue

            # 行首缩进处理（仅当不在括号内）
            if bracket_depth == 0:
                indent_w, tab_used, bad_chars, indent_text = self._measure_indent(raw, line_no)
                content_start = len(indent_text)
                content = raw[content_start:]
            else:
                indent_w, tab_used, bad_chars, indent_text = 0, False, [], ""
                # `content_start` 必须显式归零：续行从第 0 列开始扫。
                # 以前这一支**不赋值**，用的是上一轮的残留值——顶格写的续行
                # 会被跳过前几个真字符、甚至整行扫不出 token（M38 B2 写增量
                # 分词、从括号中间续跑时才暴露成 UnboundLocalError）。
                content_start = 0
                content = raw

            # 空行 / 纯注释行：不发 NEWLINE，不处理缩进
            stripped = content.strip()
            if stripped == "" or stripped.startswith("#"):
                if stripped.startswith("#-"):
                    in_block_comment = True
                    # 同行可能立即结束
                    if "-#" in content[content.find("#-"):]:
                        in_block_comment = False
                line_idx += 1
                continue

            if bracket_depth == 0:
                # 缩进错误检测
                if bad_chars:
                    raise bad_chars[0].with_source(self.lines)
                if indent_w > indent_stack[-1]:
                    indent_stack.append(indent_w)
                    tokens.append(Token("INDENT", indent_w, line_no, 1, 1, raw))
                elif indent_w < indent_stack[-1]:
                    while indent_stack and indent_w < indent_stack[-1]:
                        indent_stack.pop()
                        tokens.append(Token("DEDENT", indent_w, line_no, 1, 1, raw))
                    if not indent_stack or indent_w != indent_stack[-1]:
                        raise LexIndentError(
                            "这个缩进层级对不上前面的代码",
                            line=line_no, col=1, source_line=raw,
                            filename=self.filename,
                        ).with_source(self.lines)
            else:
                # 括号内换行：忽略缩进，不产生 token
                pass

            # 分词本行
            toks = self._tokenize_line(raw, content_start, line_no)
            for t in toks:
                if t.type == "OP" and t.value in "([{":
                    bracket_depth += 1
                elif t.type == "OP" and t.value in ")]}":
                    bracket_depth = max(0, bracket_depth - 1)

            tokens.extend(toks)
            if bracket_depth == 0 and self._pending_triple is None:
                tokens.append(Token("NEWLINE", "\n", line_no,
                                    max(1, content_start + 1), 1, raw))

            line_idx += 1

        # EOF 锚点：供「在文件末尾继续写」时复用（进 EOF 之前的状态）
        anchors.append(anchor())
        # 文件结束时还停在跨行三引号里 → 字符串没写完。
        # 以前这里**不检查**：整段内容被静默丢掉，用户只会看到「少了一个值」
        # 这种对不上号的报错（M38 B3 做 REPL 续行判定时发现）。
        # 报错位置指向字符串**开头**，那才是用户要改的地方。
        if self._pending_triple is not None:
            raise LexStringError(
                "字符串没有正常结束，是不是漏了结束引号？",
                line=self._pending_line, col=self._pending_col,
                source_line=(self.lines[self._pending_line - 1]
                             if 0 <= self._pending_line - 1 < len(self.lines)
                             else None),
                filename=self.filename).with_source(self.lines)
        # EOF：补齐 DEDENT
        for _ in range(len(indent_stack) - 1):
            tokens.append(Token("DEDENT", 0, n_lines, 1, 1, ""))
        tokens.append(Token("EOF", None, n_lines, 1, 1, ""))
        return tokens, anchors

    # -- 缩进测量 ----------------------------------------------------------

    def _measure_indent(self, raw: str, line_no: int):
        """返回 (宽度, 是否用过tab, 错误列表, 缩进文本)。宽度按 tab=4 折算。"""
        w = 0
        tab_used = False
        bad: list = []
        i = 0
        n = len(raw)
        while i < n:
            ch = raw[i]
            if ch == " ":
                w += 1
                i += 1
            elif ch == "\t":
                tab_used = True
                w += 4 - (w % 4)
                i += 1
            elif ch == "\u3000":
                bad.append(LexFullWidthSpaceError(
                    "行首缩进里不能用全角空格（U+3000），请改用半角空格",
                    line=line_no, col=i + 1, source_line=raw,
                    filename=self.filename,
                ))
                i += 1  # 继续测量，收集全部错误
            else:
                break
        indent_text = raw[:i]
        # Tab 与空格在同一行混用
        if "\t" in indent_text and " " in indent_text:
            bad.append(LexTabError(
                "同一行缩进里混用了 Tab 和空格，请统一",
                line=line_no, col=indent_text.find(" ") + 1, source_line=raw,
                filename=self.filename,
            ))
        return w, tab_used, bad, indent_text

    # -- 行内分词 ----------------------------------------------------------

    def _tokenize_line(self, raw: str, start: int, line_no: int) -> list[Token]:
        tokens: list[Token] = []
        i = start
        n = len(raw)

        def emit(ttype, value, col, end_col):
            tokens.append(Token(ttype, value, line_no, col, end_col, raw))

        while i < n:
            ch = raw[i]

            # 空白（含全角空格，作分隔符）
            if ch == " " or ch == "\u3000":
                i += 1
                continue
            if ch == "\t":
                i += 1
                continue

            # 注释
            if ch == "#":
                if i + 1 < n and raw[i + 1] == "-":
                    end = raw.find("-#", i + 2)
                    if end >= 0:
                        i = end + 2
                        continue
                    # 未闭合的多行注释：本行其余部分都是注释，由主循环处理
                    return tokens
                break  # 单行注释到行尾

            # 字符串 / 插值字符串
            if ch in "'\"“”‘’「」『』`":
                if ch == "`":
                    tok, i = self._scan_fstring(raw, i, line_no)
                    tokens.append(tok)
                    continue
                tok, i = self._scan_string(raw, i, line_no)
                if tok is not None:
                    tokens.append(tok)
                else:
                    return tokens  # 三引号字符串挂起，本行到此结束
                continue

            # 数字
            if _DIGIT.match(ch) or (ch == "." and i + 1 < n and _DIGIT.match(raw[i + 1])):
                tok, i = self._scan_number(raw, i, line_no)
                tokens.append(tok)
                continue

            # 词（标识符 / 关键字）
            if _CJK_RE.match(ch) or _ASCII_START.match(ch):
                tok, i = self._scan_word(raw, i, line_no)
                if tok is not None:
                    tokens.append(tok)
                continue

            # 全角标点归一化（一对一映射，不参与多字符运算符匹配）
            norm = normalize_fullwidth(ch)
            if norm != ch:
                emit("OP", norm, i + 1, i + 2)
                i += 1
                continue

            # 半角运算符：多字符优先。
            # 必须按**长度降序**尝试（M25 修正）：按 OPERATORS 列表顺序会
            # 让 `**=` 先匹配到 `**` 再匹配 `=`，`//=` 同理会拆成 `//` + `=`
            # —— 于是「幂赋值 / 整除赋值」这两个运算符实际上一直不可用，
            # 而且格式化器会把 `甲 **= 3` 重写成 `甲 ** = 3`。
            matched = None
            for op in _OPERATORS_LONGEST_FIRST:
                if raw.startswith(op, i):
                    matched = op
                    break
            if matched:
                emit("OP", matched, i + 1, i + len(matched) + 1)
                i += len(matched)
                continue

            raise LexCharError(
                f"无法识别的字符「{ch}」（U+{ord(ch):04X}）",
                line=line_no, col=i + 1, source_line=raw,
                filename=self.filename,
                hint="如果这是全角字符，请改用半角；或在中文输入法下重敲一遍",
            ).with_source(self.lines)

        return tokens

    # -- 字符串扫描 --------------------------------------------------------

    # 中文引号配对表：起始引号 -> 结束引号
    _PAIRED_QUOTES = {"“": "”", "‘": "’", "「": "」", "『": "』"}

    def _scan_fstring(self, raw: str, i: int, line_no: int):
        """扫描反引号插值字符串（M7）：`你好 {名字}，今年 {年龄} 岁`。

        产出 FSTRING token，value 为分段列表：
        [("text", str), ("expr", str), ("text", str), ...]。
        `{{` / `}}` 表示字面花括号。
        """
        start_col = i + 1
        i += 1                      # 跳过反引号
        n = len(raw)
        parts: list[tuple[str, str]] = []
        text_buf: list[str] = []

        def flush_text():
            if text_buf:
                parts.append(("text", "".join(text_buf)))
                text_buf.clear()

        while i < n:
            ch = raw[i]
            if ch == "`":
                flush_text()
                i += 1
                return (Token("FSTRING", parts, line_no, start_col, i, raw), i)
            if ch == "\\":
                text_buf.append(self._escape(raw, i, line_no))
                i += 2
                continue
            if ch == "{":
                # {{ → 字面 {；否则进入表达式段
                if i + 1 < n and raw[i + 1] == "{":
                    text_buf.append("{")
                    i += 2
                    continue
                flush_text()
                i += 1
                # 扫描表达式源码，直到匹配的 }（支持嵌套花括号）
                depth = 1
                expr_start = i
                while i < n:
                    c = raw[i]
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    i += 1
                if i >= n or depth != 0:
                    raise LexStringError(
                        "插值表达式没有闭合，是不是漏了「}」？",
                        line=line_no, col=start_col, source_line=raw,
                        filename=self.filename).with_source(self.lines)
                parts.append(("expr", raw[expr_start:i]))
                i += 1
                continue
            if ch == "}":
                # }} → 字面 }
                if i + 1 < n and raw[i + 1] == "}":
                    text_buf.append("}")
                    i += 2
                    continue
                # 单独的 } 当作普通文本
                text_buf.append(ch)
                i += 1
                continue
            text_buf.append(ch)
            i += 1

        flush_text()
        raise LexStringError(
            "插值字符串没有正常结束，是不是漏了反引号？",
            line=line_no, col=i + 1, source_line=raw,
            filename=self.filename).with_source(self.lines)

    def _scan_string(self, raw: str, i: int, line_no: int):
        quote = raw[i]
        start_col = i + 1
        close = self._PAIRED_QUOTES.get(quote, quote)
        # 三引号
        triple = False
        if i + 2 < len(raw) and raw[i:i + 3] == quote * 3:
            triple = True
            i += 3
        else:
            i += 1

        n = len(raw)
        buf: list[str] = []
        while i < n:
            ch = raw[i]
            if ch == "\\":
                buf.append(self._escape(raw, i, line_no))
                i += 2
                continue
            if triple:
                if raw.startswith(quote * 3, i):
                    i += 3
                    return Token("STRING", "".join(buf), line_no,
                                 start_col, i, raw), i
                buf.append(ch)
                i += 1
            else:
                if ch == close:
                    i += 1
                    return Token("STRING", "".join(buf), line_no,
                                 start_col, i, raw), i
                if ch == "\n":
                    break
                buf.append(ch)
                i += 1

        # 三引号字符串未在本行闭合：挂起，等后续行
        if triple:
            self._pending_triple = buf + ["\n"]
            self._pending_quote = quote
            self._pending_line = line_no
            self._pending_col = start_col
            return None, i

        raise LexStringError(
            "字符串没有正常结束，是不是漏了结束引号？",
            line=line_no, col=i + 1, source_line=raw, filename=self.filename,
        ).with_source(self.lines)

    def _continue_triple(self, raw: str, line_no: int):
        """继续扫描跨行三引号字符串。返回 (token, 剩余行token, 是否闭合)。"""
        buf = self._pending_triple
        quote = self._pending_quote
        i = 0
        n = len(raw)
        while i < n:
            ch = raw[i]
            if ch == "\\":
                buf.append(self._escape(raw, i, line_no))
                i += 2
                continue
            if raw.startswith(quote * 3, i):
                i += 3
                start_line, start_col = self._pending_line, self._pending_col
                self._pending_triple = None
                tok = Token("STRING", "".join(buf), start_line, start_col, i, raw)
                rest = self._tokenize_line(raw, i, line_no)
                return tok, rest, True
            buf.append(ch)
            i += 1
        # 仍未闭合：把本行内容（含换行）并入
        buf.append("\n")
        return None, [], False

    def _escape(self, raw: str, i: int, line_no: int):
        if i + 1 >= len(raw):
            raise LexStringError("字符串末尾的转义符没有内容",
                                 line=line_no, col=i + 1, source_line=raw,
                                 filename=self.filename).with_source(self.lines)
        c = raw[i + 1]
        table = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\",
                 "'": "'", '"': '"', "0": "\0"}
        if c in table:
            return table[c]
        # 未知转义：原样保留反斜杠与字符。
        # 这样 Windows 路径 "C:\Users\文档\a.txt" 可以直写，不必转义成 \\。
        return "\\" + c

    # -- 数字扫描 ----------------------------------------------------------

    def _scan_number(self, raw: str, i: int, line_no: int):
        n = len(raw)
        start = i
        while i < n and _DIGIT.match(raw[i]):
            i += 1
        if i < n and raw[i] == ".":
            i += 1
            while i < n and _DIGIT.match(raw[i]):
                i += 1
        if i < n and raw[i] in "eE":
            j = i + 1
            if j < n and raw[j] in "+-":
                j += 1
            if j < n and _DIGIT.match(raw[j]):
                i = j
                while i < n and _DIGIT.match(raw[i]):
                    i += 1
        text = raw[start:i]
        value = float(text) if ("." in text or "e" in text or "E" in text) else int(text)
        return Token("NUMBER", value, line_no, start + 1, i, raw), i

    # -- 词扫描（标识符 / 关键字） ------------------------------------------

    def _scan_word(self, raw: str, i: int, line_no: int):
        n = len(raw)
        start = i
        # 吞下连续词字符：CJK / ASCII 字母数字下划线
        while i < n:
            ch = raw[i]
            if _CJK_RE.match(ch) or _ASCII_START.match(ch) or _DIGIT.match(ch):
                i += 1
            else:
                break
        word = raw[start:i]

        # 是否纯 CJK 词（决定关键字判定）
        pure_cjk = bool(_CJK_RUN_RE.fullmatch(word))

        if pure_cjk:
            if word in KEYWORDS:
                return Token("KEYWORD", word, line_no, start + 1, i, raw), i
            # 以「多字关键字」为前缀 → 粘连错误。
            # 单字关键字（类/当/真/空/非/令…）与海量常用词冲突（类型/当时/真实/
            # 空间/命令…），不能做前缀检查；只有多字关键字（如果/循环/导入…）
            # 的粘连才是用户真正常犯的错，误报率低、提示价值高。
            for kw in sorted(KEYWORDS, key=len, reverse=True):
                if kw in _GLUE_CHECK_SKIP:
                    continue
                if len(kw) >= 2 and word.startswith(kw) and len(word) > len(kw):
                    raise LexKeywordGlueError(
                        f"「{word}」被当成了一个名字，但它的开头是关键字「{kw}」",
                        line=line_no, col=start + 1, source_line=raw,
                        filename=self.filename,
                        hint=f"你是不是想写「{kw} {word[len(kw):]}」？关键字和后面的内容之间要留空格",
                        underline=(start + 1, start + len(kw)),
                    ).with_source(self.lines)
            return Token("NAME", word, line_no, start + 1, i, raw), i

        # 混合词 / ASCII 词：一律标识符
        return Token("NAME", word, line_no, start + 1, i, raw), i


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def tokenize(text: str, filename: str = "<输入>") -> list[Token]:
    return Tokenizer(text, filename).tokenize()
