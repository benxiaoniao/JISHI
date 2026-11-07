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

#: 赋值运算符：后面跟的 `*` 是解包星号（`令 a, *余 = …`）而不是乘号（M25）
_ASSIGN_OPS = frozenset(["=", "+=", "-=", "*=", "/=", "//=", "%=", "**="])

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


def _scan_code(line: str) -> tuple["int | None", int]:
    """扫一行，返回 ``(注释起始下标或 None, 括号净增量)``。

    **必须走状态机扫，不能用 ``line.find("#")``**：字符串里的 `#` 不是注释
    （M38 B4 实测：`令 甲 = "含 # 号"` 会被从字符串中间劈开，格式化直接报
    「字符串没有正常结束」）。同一个扫描器顺手算括号净增量，供续行判定用——
    以前这两件事各写一份字符串处理，两边都得单独记得「跳过字符串里的括号」。

    识别：反引号插值字符串、三引号、单/双引号、中文引号、``\\`` 转义；
    注释（``#``）之后不再扫描。
    """
    i = 0
    n = len(line)
    delta = 0
    while i < n:
        ch = line[i]
        if ch == "#":
            return i, delta
        if ch == "\\":
            i += 2
            continue
        if ch == "`":                      # 反引号文本插值（M7）
            i += 1
            while i < n and line[i] != "`":
                i += 2 if line[i] == "\\" else 1
            i += 1
            continue
        if ch in _TRIPLE_QUOTES:
            if line.startswith(ch * 3, i):
                end = line.find(ch * 3, i + 3)
                if end < 0:
                    break                  # 跨行三引号：本行到末尾都是字符串
                i = end + 3
                continue
            close = _PAIRED.get(ch, ch)
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == close:
                    break
                i += 1
            i += 1
            continue
        if ch in "([{":
            delta += 1
        elif ch in ")]}":
            delta -= 1
        i += 1
    return None, delta


def _split_comment(body: str) -> tuple[str, str]:
    """把一行拆成「代码」与「注释」两段（没有注释时第二段为空串）。"""
    idx = _scan_code(body)[0]
    if idx is None:
        return body, ""
    return body[:idx], body[idx:].rstrip()


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
    prev_unpack_star = False
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
        elif norm == "(" and prev_norm == "函数":
            # 匿名函数（M24.4）：`函数(x)：x * 2` 的括号紧贴关键字，
            # 别写成 `函数 (x)`（具名函数是 `函数 名字(`，不受这条影响）
            need_space = False
        elif prev_norm in _NO_SPACE_AFTER:
            need_space = False
        elif prev_unary:
            need_space = False
        elif prev_unpack_star:
            # 星号解包（M25）：*参数 / **选项 / 令 甲, *余 —— 星号紧贴名字，
            # 写成 `* 参数` 虽仍合法，但不是惯例写法
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

        # 判定当前 token 是否是「解包星号」（M25）：`f(*参数)`、`令 a, *余 = …`
        is_unpack_star = False
        if norm in ("*", "**"):
            if first or prev_norm in ("(", "[", "{", ",", "=", "->"):
                is_unpack_star = True
            elif prev_norm in _ASSIGN_OPS:
                is_unpack_star = True

        prev_wide_colon = (norm == ":" and text == "：")
        prev_norm, prev_type, prev_unary = norm, t.type, is_unary
        prev_unpack_star = is_unpack_star
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
    # 开头的 BOM 原样保留（Windows 编辑器常带）：剥掉它参与解析，最后再放回去。
    # 不这么做的话，`--check` 会把每个带 BOM 的文件都判成「没格式化」。
    bom = ""
    if src.startswith("\ufeff"):
        bom, src = "\ufeff", src[1:]
    lines = src.split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()
    if not lines:
        return bom

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
        #: 去掉行首缩进后的内容。**必须在这里算**：括号续行分支与普通代码行
        #: 分支都要用它，若在其中一处才算，另一处会拿到上一轮循环里的旧值
        #: （M38 B4 实测：续行被整段替换成上一行的内容，输出直接语法错）。
        body = raw[len(lead):]

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

        # ---- 括号内的续行：保留原有相对缩进，但注释也要留住 --------------
        if bracket_depth > 0:
            code_part, comment = _split_comment(body)
            pieces = _respacify(code_part, tk).strip() if code_part.strip() else ""
            if pieces:
                line_out = lead + pieces
                if comment:
                    line_out += "  " + comment
            else:
                line_out = lead + comment
            out.append(line_out.rstrip())
            bracket_depth = max(0, bracket_depth + _scan_code(body)[1])
            continue

        # ---- 普通代码行：缩进归一 + 间距归一 ---------------------------
        level = levels.get(line_no, prev_level)
        prev_level = level

        code, comment = _split_comment(body)
        pieces = _respacify(code, tk)
        line_out = unit * level + pieces
        if comment:
            if pieces:
                line_out += "  " + comment
            else:
                line_out = unit * level + comment
        out.append(line_out.rstrip())

        bracket_depth = max(0, bracket_depth + _scan_code(code)[1])

    # 去掉末尾空行，统一以一个换行结束
    while out and out[-1] == "":
        out.pop()
    return bom + "\n".join(out) + "\n"