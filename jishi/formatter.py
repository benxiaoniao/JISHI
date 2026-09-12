# -*- coding: utf-8 -*-
"""基石代码格式化器（M21.1）。

设计取舍：

- **保留注释**：词法器为了语法分析把 ``#`` 注释丢掉了，格式化器必须自己
  重建「代码 / 注释」边界，否则格式化会把用户注释吃掉；
- **保留字面量原样**：字符串 token 直接从原始文本按列切片取出，不重新加
  引号，因此引号风格（含中文引号）、转义、插值字符串都原样保留；
- **不拆行、不并行**：只做「缩进归一」与「词间间距归一」，行结构不变，
  所以格式化前后语义必然一致（`tests/test_m21_tools.py` 有对拍保障）；
- **幂等**：格式化结果再格式化一次，输出不变。

全角冒号 ``：`` 会被词法器归一成 ``:`` 参与间距判断，但输出时仍写回
原文里的全角形式——中文代码里 ``如果 x > 0：`` 比半角更自然。
"""

from __future__ import annotations

from .tokenizer import Tokenizer, tokenize

#: 紧跟其后不留空格的符号：`) ] } , . :`
_NO_SPACE_BEFORE = frozenset(")]},.:")

#: 其后不留空格的符号：`( [ { .`
_NO_SPACE_AFTER = frozenset("([{.")

#: 二元运算符（两侧都留空格）
_BINARY_OPS = frozenset([
    "+", "-", "*", "/", "//", "%", "**", "^",
    "==", "!=", "<", ">", "<=", ">=",
    "=", "+=", "-=", "*=", "/=", "//=", "%=", "**=",
])

#: 可能是「正负号」的符号（按上下文判定是一元还是二元）
_SIGN_OPS = frozenset(["-", "+"])

#: 这些关键字后面跟的是表达式，所以紧随的 `-`/`+` 是一元号
_VALUE_KEYWORDS = frozenset(["真", "假", "空"])

#: 字符串引号配对（与词法器保持一致）
_PAIRED = {"“": "”", "‘": "’", "「": "」", "『": "』"}

#: 支持三引号形式的引号字符
_TRIPLE_QUOTES = ('"', "'", "“", "‘", "「", "『")


def _line_levels(source: str, filename: str) -> dict[int, int]:
    """用缩进 token 算出每一「代码行」的缩进层级（0 为顶层）。"""
    levels: dict[int, int] = {}
    level = 0
    for tok in tokenize(source, filename):
        if tok.type == "INDENT":
            level += 1
        elif tok.type == "DEDENT":
            level = max(0, level - 1)
        elif tok.type in ("NEWLINE", "EOF"):
            continue
        else:
            # 只记每行第一个内容 token 的层级
            if tok.line not in levels:
                levels[tok.line] = level
    return levels


def _scan_string_state(line: str, in_triple: bool, quote: str):
    """扫描一行的字符串状态，返回 (行末是否仍在三引号内, 引号字符)。

    只关心「跨行三引号字符串」——这类行必须原样保留，不能重排空格。
    行内 ``#`` 之后视为注释，不再扫描（注释里的引号不是语法引号）。
    """
    i = 0
    n = len(line)
    while i < n:
        if in_triple:
            if line.startswith(quote * 3, i):
                in_triple = False
                quote = ""
                i += 3
                continue
            i += 1
            continue

        ch = line[i]
        if ch == "#":
            break                      # 注释：本行后面不再是代码
        if ch == "\\":
            i += 2
            continue
        if ch in _TRIPLE_QUOTES:
            if line.startswith(ch * 3, i):
                in_triple = True
                quote = ch
                i += 3
                continue
            # 普通字符串：跳到配对引号
            close = _PAIRED.get(ch, ch)
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == close:
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return in_triple, quote


def _find_comment(body: str, from_idx: int) -> "int | None":
    """在 ``body[from_idx:]`` 里找注释起始下标（``#`` 的位置）。

    调用方保证 ``from_idx`` 之前已经是完整 token，因此这里只需朴素查找。
    """
    idx = body.find("#", from_idx)
    return idx if idx >= 0 else None


def _indent_width(lead: str) -> int:
    """缩进文本的宽度（tab 按 4 折算，与词法器保持一致）。"""
    w = 0
    for ch in lead:
        if ch == "\t":
            w += 4 - (w % 4)
        elif ch == " ":
            w += 1
        else:
            w += 2                     # 全角空格等
    return w


def _width_level_map(lines: list[str], levels: dict[int, int]) -> dict[int, int]:
    """由代码行建立「原始缩进宽度 → 层级」映射，供注释行归位。"""
    m: dict[int, int] = {}
    for i, raw in enumerate(lines):
        lv = levels.get(i + 1)
        if lv is None:
            continue
        lead = raw[:len(raw) - len(raw.lstrip())]
        m.setdefault(_indent_width(lead), lv)
    return m


def _level_of_width(w: int, m: dict[int, int]) -> int:
    """缩进宽度折算层级：精确命中优先，否则取不超过它的最大已知宽度。"""
    if w in m:
        return m[w]
    best = None
    for known in m:
        if known <= w and (best is None or known > best):
            best = known
    return m[best] if best is not None else 0


def _respacify(code: str, tk: Tokenizer) -> str:
    """把一行代码按统一间距规则重新拼接（token 文本仍取自原文切片）。"""
    toks = tk._tokenize_line(code, 0, 1)
    tk._pending_triple = None          # 清掉可能的副作用
    if not toks:
        return code.strip()

    out: list[str] = []
    first = True
    prev_norm = None
    prev_type = None
    prev_unary = False
    prev_value = False
    prev_wide_colon = False
    brackets: list[str] = []

    for t in toks:
        if t.type == "OP":
            norm = str(t.value)
        elif t.type == "KEYWORD":
            norm = str(t.value)
        else:
            norm = None                # 名字/数字/字符串：不参与符号间距规则

        # 注意：词法器里 OP 的 end_col 比其它 token 多 1（历史约定不一致）：
        # OP 的区间是 [col-1, end_col-1)，其余 token 是 [col-1, end_col)。
        if t.type == "OP":
            text = code[t.col - 1:t.end_col - 1]
        else:
            text = code[t.col - 1:t.end_col]
        if not text:
            text = str(t.value)

        if first:
            need_space = False
        elif norm is not None and norm in _NO_SPACE_BEFORE:
            need_space = False
        elif norm in ("(", "[") and prev_value:
            need_space = False         # 调用/下标：f(x)、甲[0] 紧贴
        elif prev_norm in _NO_SPACE_AFTER:
            need_space = False
        elif prev_unary:
            need_space = False
        elif prev_wide_colon:
            need_space = False         # 全角冒号自带间距，后面不再补空格
        elif prev_norm == ":" and brackets and brackets[-1] == "[":
            need_space = False         # 切片 a[1:3] 内部不留空格
        elif prev_norm == ":" and not brackets:
            need_space = False         # 块首冒号只出现在行尾，保险
        else:
            need_space = True

        if need_space:
            out.append(" ")
        out.append(text)

        # 记录括号栈（决定 `:` 是切片还是字典）
        if norm in ("(", "[", "{"):
            brackets.append(norm)
        elif norm in (")", "]", "}"):
            if brackets:
                brackets.pop()

        # 判定当前 token 是否一元正负号
        is_unary = False
        if norm in _SIGN_OPS:
            if first:
                is_unary = True
            elif prev_norm in _BINARY_OPS or prev_norm in ("(", "[", "{", ",", ":"):
                is_unary = True
            elif prev_type == "KEYWORD" and prev_norm not in _VALUE_KEYWORDS:
                is_unary = True

        prev_wide_colon = (norm == ":" and text == "：")
        prev_norm, prev_type, prev_unary = norm, t.type, is_unary
        prev_value = (
            t.type in ("NAME", "NUMBER", "STRING", "FSTRING")
            or norm in (")", "]", "}")
            or (t.type == "KEYWORD" and norm in _VALUE_KEYWORDS)
        )
        first = False

    return "".join(out)


def format_source(source: str, filename: str = "<输入>",
                  indent: int = 4) -> str:
    """格式化基石源码，返回新源码（不修改入参）。"""
    src = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = src.split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()
    if not lines:
        return ""

    levels = _line_levels(src, filename)
    width_to_level = _width_level_map(lines, levels)
    tk = Tokenizer(src, filename)
    unit = " " * max(1, int(indent))

    out: list[str] = []
    in_block_comment = False
    in_triple = False
    triple_quote = ""
    bracket_depth = 0
    prev_level = 0

    for idx, raw in enumerate(lines):
        line_no = idx + 1
        stripped = raw.strip()
        lead = raw[:len(raw) - len(raw.lstrip())]

        # ---- 块注释内部：原样保留 -------------------------------------
        if in_block_comment:
            out.append(raw.rstrip())
            if "-#" in raw:
                in_block_comment = False
            continue

        # ---- 三引号字符串内部：原样保留 -------------------------------
        if in_triple:
            out.append(raw.rstrip())
            in_triple, triple_quote = _scan_string_state(
                raw, in_triple, triple_quote)
            continue

        # ---- 空行 -----------------------------------------------------
        if stripped == "":
            out.append("")
            continue

        # ---- 块注释起始行：原样保留 -----------------------------------
        if stripped.startswith("#-"):
            out.append(raw.rstrip())
            if "-#" not in stripped[2:]:
                in_block_comment = True
            continue

        # ---- 本行含三引号（开或闭）：整行原样保留 ---------------------
        new_triple, new_quote = _scan_string_state(raw, False, "")
        if new_triple or stripped.startswith(('"""', "'''")):
            out.append(raw.rstrip())
            in_triple, triple_quote = new_triple, new_quote
            continue

        # ---- 只有注释的行：按它自己的原始缩进宽度定位层级 ---------------
        if stripped.startswith("#"):
            level = _level_of_width(_indent_width(lead), width_to_level)
            out.append(unit * level + stripped)
            continue

        # ---- 括号内的续行：保留原有相对缩进 ---------------------------
        if bracket_depth > 0:
            body = raw[len(lead):]
            out.append(lead + _respacify(body, tk).strip())
            bracket_depth += _bracket_delta(body)
            bracket_depth = max(0, bracket_depth)
            continue

        # ---- 普通代码行：缩进归一 + 间距归一 ---------------------------
        level = levels.get(line_no, prev_level)
        prev_level = level
        body = raw[len(lead):]

        comment = None
        code = body
        cpos = _find_comment(body, 0)
        if cpos is not None:
            code = body[:cpos]
            comment = body[cpos:].rstrip()

        pieces = _respacify(code, tk)
        line_out = unit * level + pieces
        if comment:
            if pieces:
                line_out += "  " + comment
            else:
                line_out = unit * level + comment
        out.append(line_out.rstrip())

        bracket_depth = max(0, bracket_depth + _bracket_delta(code))

    # 去掉末尾空行，统一以一个换行结束
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def _bracket_delta(text: str) -> int:
    """粗略统计一行里未配对括号的净增量（仅用于续行判定）。"""
    delta = 0
    i = 0
    n = len(text)
    in_str = False
    quote = ""
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                in_str = False
            i += 1
            continue
        if ch == "#":
            break
        if ch in "\"'“”‘’「」『』":
            in_str = True
            quote = _PAIRED.get(ch, ch)
            i += 1
            continue
        if ch in "([{":
            delta += 1
        elif ch in ")]}":
            delta -= 1
        i += 1
    return delta
