# -*- coding: utf-8 -*-
"""基石语言的语法分析器：递归下降 + 优先级爬升，产出 AST。

M0 支持语法：
    语句：赋值、表达式语句、如果/否则如果/否则、遍历、循环N次、当、
          中断、继续、函数定义、返回、导入、占位（空）
    表达式：算术、比较、逻辑、一元、调用、属性、下标、列表/字典字面量
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from . import ast_nodes as A
from .errors import (
    ParseBlockError,
    ParseMissingColonError,
    ParseMissingNameError,
    ParseUnexpectedError,
)
#: 形参种类常量（M25）。从 opcodes 取，保证与 C 侧 JS_PARAM_* 同源；
#: opcodes 是纯数据定义、不反向依赖 parser，没有循环导入风险。
from .opcodes import ParamKind
from .tokenizer import Token, tokenize
# 二元运算符优先级（数值越大越紧）
# 注意：关键字形式的运算符（与/或/在/不在/是/不是）也登记在这里，
# 让需要优先级的代码统一走 _BIN_PREC[op]。
_BIN_PREC = {
    "或": 1,
    "与": 2,
    "==": 3, "!=": 3, "<": 3, ">": 3, "<=": 3, ">=": 3,
    "在": 3, "不在": 3, "是": 3, "不是": 3,
    "+": 4, "-": 4,
    "*": 5, "/": 5, "//": 5, "%": 5,
    "**": 6,
}

# 未来关键字：解析到即报"将在后续版本支持"
_FUTURE_KEYWORDS = {
}

#: 「匹配」的写法提示（M34.1）。报错里反复要用，抽出来免得抄漏。
_MATCH_HINT = """写法是：
匹配 分数：
    情形 90：
        打印("优秀")
    情形 80, 85：
        打印("良好")
    情形 其他：
        打印("继续努力")

「情形」后面可以写多个值（逗号分隔，命中任一个即可）、
可以加守卫（`情形 90 如果 有加分：`），
「情形 其他」是兜底，要放在最后。"""

#: 「类体里只能写什么」的提示（M35）
_CLASS_HINT = """类体里只放两样东西：

类 甲：
    计数 = 0                # 1. 类变量（名字 = 值）
    函数 打招呼(自身)：      # 2. 方法（函数）
        打印("你好")

空类写一个单独的冒号占位：
类 空类：
    :"""

#: 「枚举」的写法提示（M34.2）
_ENUM_HINT = """写法是：
枚举 颜色：
    红 = 1
    绿 = 2

成员也可以不写值，让它自动接着编号：
枚举 方向：
    上
    下
    左
    右

用的时候写 `颜色.红`；取名字和值用 `颜色.红.名字` / `颜色.红.值`；
遍历所有成员用 `遍历 c 在 颜色.全部：`。"""

# M7.5 容错解析：LLM 写中文代码时 fallback 回英文的高频错误 → 中文建议。
# 英文语句关键字 → 中文对应
_EN_STMT_HINTS = {
    "def": "函数", "if": "如果", "else": "否则", "elif": "否则如果",
    "for": "遍历", "while": "当", "return": "返回", "import": "导入",
    "from": "从", "class": "类", "try": "尝试", "except": "捕获",
    "finally": "最终", "raise": "抛出", "break": "中断", "continue": "继续",
    "with": "用",
}
# 英文值/运算符关键字 → 中文对应
_EN_VALUE_HINTS = {
    "True": "真", "False": "假", "None": "空",
    "and": "与", "or": "或", "not": "非",
    "in": "在", "not in": "不在", "is": "是", "is not": "不是",
}
# 英文内建函数 → 中文对应（基石已有这些内建）
_EN_BUILTIN_HINTS = {
    "print": "打印", "input": "输入", "len": "长度", "range": "范围",
    "int": "整数", "float": "小数", "str": "文本", "type": "类型",
    "min": "最小", "max": "最大", "sum": "总和", "reversed": "反转",
}
# 英文语法基石没有对应 → 直接给说明
_EN_UNSUPPORTED = {
    "pass": "空代码块直接写一个冒号「:」占位，不需要 pass",
    "lambda": "基石没有匿名函数，请用「函数 名字(参数)：」定义",
    "with": "基石暂不支持 with，可用「尝试/最终」管理资源",
    "del": "基石没有 del，变量不需要手动删除",
    "global": "基石没有 global，函数内可直接读写外层变量",
    "yield": "基石暂不支持生成器 yield",
    "async": "基石暂不支持异步 async",
    "await": "基石暂不支持异步 await",
}

# 比较运算符（支持链式）
_COMPARE_OPS = ("==", "!=", "<", ">", "<=", ">=")
#: 关键字形式的比较运算符（M23.2）：`x 在 列表`、`x 不在 列表`、
#: `x 是 空`、`x 不是 空`。与符号比较同级，同样支持链式。
_KEYWORD_CMP_OPS = ("在", "不在", "是", "不是")
#: 全部比较运算符（符号 + 关键字）
_ALL_CMP_OPS = _COMPARE_OPS + _KEYWORD_CMP_OPS

#: 「非」的优先级（M23 修正）：与比较同级 → 它能吃掉右边的比较表达式，
#: 但吃不掉「与/或」。这样 `非 a == b` = `非 (a == b)`（Python 的 not 语义），
#: `非 a 与 b` = `(非 a) 与 b`。
_NOT_PREC = 3

#: `超()` 在 AST 里的名字（M23.4，非关键字，靠解析期脱糖识别）
SUPER_NAME = "超"


# ---------------------------------------------------------------------------
# `超()` 脱糖（M23.4）
# ---------------------------------------------------------------------------

def _child_nodes(node: A.Node):
    """产出一个 AST 节点的直接子节点（含嵌套在列表/元组里的）。"""
    if not dataclasses.is_dataclass(node):
        return
    for f in dataclasses.fields(node):
        try:
            value = getattr(node, f.name)
        except AttributeError:
            continue
        if isinstance(value, A.Node):
            yield value
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, A.Node):
                    yield item
                elif isinstance(item, (list, tuple)):
                    for sub in item:
                        if isinstance(sub, A.Node):
                            yield sub


def _desugar_super(root: A.Node, defining: str, self_param: Optional[str],
                   filename: str) -> None:
    """把类方法体里的 `超()` 就地改写成 `超(自身, "定义类名")`（M23.4）。

    为什么要脱糖而不是在运行时靠 `实例.类.基类` 去猜：**多级继承会猜错**。
    若 `狗` 继承 `动物`、`动物` 继承 `生物`，在 `狗` 的方法里写 `超()`，
    运行时拿到的 `实例.类` 是 `狗`，基类恰好是 `动物`，看着对；但若 `动物`
    的方法里也写 `超()`，同一套逻辑又会拿到 `狗` 的基类 `动物`——自递归。
    解析期我们知道「这段代码写在哪个类里」，直接写死定义类名，语义就准确了，
    与 Python 的零参数 `super()` 一致（Python 靠编译器塞 `__class__` 单元）。
    """
    stack: list[A.Node] = list(_child_nodes(root))
    while stack:
        node = stack.pop()

        if isinstance(node, A.ClassDef):
            # 嵌套类由它自己的 _parse_classdef 处理，跳过
            continue

        if (isinstance(node, A.Call) and isinstance(node.func, A.Name)
                and node.func.id == SUPER_NAME
                and not node.args and not node.keywords):
            fn = node.func
            if not self_param:
                raise ParseUnexpectedError(
                    f"「{SUPER_NAME}()」要写在方法里（方法的第一个参数是"
                    f"「自身」），现在这个方法没有参数，取不到实例",
                    line=fn.line, col=fn.col, filename=filename)
            node.args = [
                A.Name(id=self_param, line=fn.line, col=fn.col),
                A.Str(value=defining, line=fn.line, col=fn.col),
            ]
            # 打标记：让收尾的「漏用检查」知道这个 `超` 是合法的
            setattr(fn, "_super_ok", True)

        stack.extend(_child_nodes(node))


def _check_no_stray_super(program: A.Program, filename: str) -> None:
    """检查有没有漏在类方法外的 `超`，给一句能照着改的提示。"""
    stack: list[A.Node] = [program]
    while stack:
        node = stack.pop()
        if (isinstance(node, A.Name) and node.id == SUPER_NAME
                and not getattr(node, "_super_ok", False)):
            raise ParseUnexpectedError(
                f"「{SUPER_NAME}()」只能在类的方法里用（用它调基类的同名方法）",
                line=node.line, col=node.col, filename=filename,
                hint="写法：在方法里写 超().方法名(参数)"
                     "——例如 超().初始化(名字)")
        stack.extend(_child_nodes(node))


# 赋值运算符
_ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "//=", "%=", "**="}


class Parser:
    def __init__(self, tokens: list[Token], source_lines: list[str],
                 filename: str = "<输入>"):
        self.tokens = tokens
        self.source_lines = source_lines
        self.filename = filename
        self.pos = 0
        #: 「用 … 为」脱糖时给保存上下文对象的临时变量编号（M26.2）
        self._with_seq = 0
        #: 「匹配」脱糖时给保存待匹配值的临时变量编号（M34.1）
        self._match_seq = 0

    # -- 基础工具 ----------------------------------------------------------

    def _peek(self, k: int = 0) -> Token:
        idx = min(self.pos + k, len(self.tokens) - 1)
        return self.tokens[idx]

    def _next(self) -> Token:
        t = self.tokens[self.pos]
        if t.type != "EOF":
            self.pos += 1
        return t

    def _at(self, type_: str, value=None) -> bool:
        t = self._peek()
        return t.type == type_ and (value is None or t.value == value)

    def _at_op(self, op: str) -> bool:
        t = self._peek()
        return t.type == "OP" and t.value == op

    def _at_kw(self, kw: str) -> bool:
        t = self._peek()
        return t.type == "KEYWORD" and t.value == kw

    def _check_en_word(self, t: Token, *, mode: str = "stmt") -> None:
        """容错解析（M7.5）：英文关键字/内建 → 给出中文建议。

        mode 决定检查哪些类别：
        - "stmt"    语句开头：英文语句关键字 / 无对应语法 / 值关键字
        - "atom"    表达式原子：值关键字 + 内建函数（仅调用位置）
        - "newline" 行尾残留：仅值/运算符关键字（and/or/not/in/is/True…）
        """
        w = t.value
        if mode != "newline" and w in _EN_STMT_HINTS:
            self._err(
                ParseUnexpectedError,
                f"「{w}」是英文关键字，基石用中文——你是不是想写「{_EN_STMT_HINTS[w]}」？",
                hint="把英文关键字换成对应的中文关键字即可。\n"
                     f"例如：{w} → {_EN_STMT_HINTS[w]}",
                tok=t, fix={"old": w, "new": _EN_STMT_HINTS[w]})
        if w in _EN_VALUE_HINTS:
            cn = _EN_VALUE_HINTS[w]
            self._err(
                ParseUnexpectedError,
                f"「{w}」是英文写法，基石用中文——你是不是想写「{cn}」？",
                hint=f"把「{w}」换成「{cn}」即可",
                tok=t, fix={"old": w, "new": cn})
        if mode != "newline" and w in _EN_UNSUPPORTED:
            self._err(
                ParseUnexpectedError,
                f"「{w}」是 Python 的写法，基石里没有这个关键字",
                hint=_EN_UNSUPPORTED[w],
                tok=t)
        if mode == "atom" and w in _EN_BUILTIN_HINTS and self._at_op("("):
            self._err(
                ParseUnexpectedError,
                f"「{w}」是 Python 的内建函数，基石里叫「{_EN_BUILTIN_HINTS[w]}」",
                hint=f"把 {w}(…) 改成 {_EN_BUILTIN_HINTS[w]}(…)",
                tok=t, fix={"old": w, "new": _EN_BUILTIN_HINTS[w]})

    def _err(self, exc: type, msg: str = "", hint: Optional[str] = None,
             tok: Optional[Token] = None, fix: Optional[dict] = None):
        t = tok or self._peek()
        raise exc(msg, line=t.line, col=t.col, source_line=t.source_line,
                  filename=self.filename,
                  hint=hint, underline=(t.col, t.end_col), fix=fix).with_source(
                      self.source_lines)

    def _expect_op(self, op: str):
        if self._at_op(op):
            return self._next()
        t = self._peek()
        # 容错解析（M7.5 扩展）：表达式里冒出英文运算符要先说清（M23 补）。
        # 典型：`1 not in [1]` —— 报「需要「)」」对用户毫无帮助，
        # 直接告诉他中文该写「不在」。
        if t.type == "NAME" and t.value in _EN_VALUE_HINTS:
            # 两词组合（`not in`、`is not`）要整体替换成「不在」「不是」，
            # 只提示「非」是错的——用户照着改还是错的。
            nxt = self._peek(1)
            pair = f"{t.value} {nxt.value}" if nxt.type == "NAME" else None
            if pair in _EN_VALUE_HINTS:
                cn = _EN_VALUE_HINTS[pair]
                self._err(
                    ParseUnexpectedError,
                    f"「{pair}」是英文写法，基石用中文——"
                    f"你是不是想写「{cn}」？",
                    hint=f"把「{pair}」换成「{cn}」即可",
                    tok=t, fix={"old": pair, "new": cn})
            cn = _EN_VALUE_HINTS[t.value]
            self._err(
                ParseUnexpectedError,
                f"「{t.value}」是英文运算符，基石用中文——"
                f"你是不是想写「{cn}」？",
                hint=f"把「{t.value}」换成「{cn}」即可",
                tok=t, fix={"old": t.value, "new": cn})
        self._err(ParseUnexpectedError,
                  f"这里需要「{op}」，但看到了「{t.value}」")

    def _expect_kw(self, kw: str):
        if self._at_kw(kw):
            return self._next()
        self._err(ParseUnexpectedError,
                  f"这里需要关键字「{kw}」，但看到了「{self._peek().value}」")

    def _expect_name(self, what: str = "名字") -> Token:
        if self._peek().type == "NAME":
            return self._next()
        self._err(ParseMissingNameError, f"这里需要一个{what}")

    def _expect_attr_name(self) -> Token:
        """`.` 后面的属性名 —— **允许关键字**（M34）。

        为什么要放开：属性名位置没有歧义（`.` 之后必然是名字），而关键字
        表是会长大的。M34 加 `匹配` 时，`正则.匹配`（M33 刚写的标准库函数、
        真实项目在用）立刻解析不了——「加一个关键字就撞掉一个既有 API」
        这种事不该反复发生。

        变量名、参数名、字典键那些位置**仍然不许**用关键字（与 Python 一致），
        这里只放开最无歧义的一处。

        顺带把属性名走一遍容错解析：写英文关键字时给中文建议。
        """
        t = self._peek()
        if t.type == "NAME":
            return self._next()
        if t.type == "KEYWORD":
            self._next()
            return t
        self._err(ParseMissingNameError, "这里需要一个属性名")

    def _expect_newline(self):
        if self._at("NEWLINE"):
            return self._next()
        # 容错解析（M7.5）：行尾残留英文运算符（and/or/not/in/is）→ 中文建议
        nxt = self._peek()
        if nxt.type == "NAME" and nxt.value in _EN_VALUE_HINTS:
            self._check_en_word(nxt, mode="newline")
        self._err(ParseUnexpectedError,
                  f"这一行没写完，但下一行已经开始了——是不是少了个符号？")

    # -- 程序入口 ----------------------------------------------------------

    def parse(self) -> A.Program:
        body = self._parse_statements(until=("EOF",))
        return A.Program(body=body)

    def _parse_statements(self, until: tuple[str, ...]) -> list[A.Node]:
        stmts: list[A.Node] = []
        while True:
            t = self._peek()
            if t.type in until or t.type == "EOF":
                break
            if t.type == "NEWLINE":
                self._next()
                continue
            if t.type == "DEDENT":
                if "DEDENT" in until:
                    break
                self._err(ParseUnexpectedError, "这里多了一个退格缩进", tok=t)
            stmt = self._parse_statement()
            # 解析期脱糖的语句（如 M26.2 的「用 … 为」）会返回多条语句，
            # 在这里一次性展平：后面的编译器/解释器只看到普通语句。
            if isinstance(stmt, list):
                stmts.extend(stmt)
                continue
            stmts.append(stmt)
            # 块语句（如果/遍历/循环/当/函数/尝试）自带结构，以 DEDENT 结束，
            # 不需要（也没有）行尾 NEWLINE
            if isinstance(stmt, (A.If, A.For, A.Loop, A.While, A.FuncDef,
                                 A.ClassDef, A.Try, A.Pass)):
                continue
            if not self._at("NEWLINE") and self._peek().type != "EOF":
                self._expect_newline()
            elif self._at("NEWLINE"):
                self._next()
        return stmts

    # -- 语句 --------------------------------------------------------------

    def _parse_statement(self) -> A.Node:
        t = self._peek()
        if t.type == "KEYWORD":
            kw = t.value
            # 真/假/空/非 作为值开头的表达式语句
            if kw in ("真", "假", "空", "非"):
                return self._parse_expr_stmt()
            if kw == "令":
                return self._parse_assign(decl=True)
            if kw == "如果":
                return self._parse_if()
            if kw == "遍历":
                return self._parse_for()
            if kw == "循环":
                return self._parse_loop()
            if kw == "当":
                return self._parse_while()
            if kw == "中断":
                self._next()
                return A.Break(line=t.line, col=t.col)
            if kw == "继续":
                self._next()
                return A.Continue(line=t.line, col=t.col)
            if kw == "函数":
                return self._parse_funcdef()
            if kw == "类":
                return self._parse_classdef()
            if kw == "返回":
                return self._parse_return()
            if kw == "导入":
                return self._parse_import()
            if kw == "尝试":
                return self._parse_try()
            if kw == "用":
                return self._parse_with()
            if kw == "匹配":
                return self._parse_match()
            if kw == "枚举":
                return self._parse_enum()
            if kw == "情形":
                self._err(ParseUnexpectedError,
                          "「情形」只能写在「匹配」的代码块里",
                          hint=_MATCH_HINT, tok=t)
            if kw == "抛出":
                return self._parse_raise()
            if kw in ("捕获", "最终"):
                self._err(ParseUnexpectedError,
                          f"「{kw}」必须跟在「尝试」的代码块后面",
                          hint="写法是：\n尝试：\n    …\n捕获 值错误 为 e：\n    …",
                          tok=t)
            if kw in _FUTURE_KEYWORDS:
                self._err(ParseUnexpectedError,
                          f"「{kw}」是保留关键字，{_FUTURE_KEYWORDS[kw]}"
                          f"功能将在后续版本支持", tok=t)
            self._err(ParseUnexpectedError,
                      f"不能以关键字「{kw}」开头写这句话", tok=t)

        if t.type == "NAME":
            # 容错解析（M7.5）：英文关键字/内建 → 中文建议
            self._check_en_word(t, mode="stmt")
            # 赋值（含 a[0] = v、a.b = v） or 表达式语句
            if self._looks_like_assign():
                return self._parse_assign(decl=False)
            return self._parse_expr_stmt()

        # 直接以值开头的表达式语句：1 + 2、"你好"、[1, 2]、-5、(1+2)、真、非 x
        if (t.type in ("NUMBER", "STRING")
                or (t.type == "OP" and t.value in ("[", "(", "-"))
                or (t.type == "KEYWORD" and t.value in ("真", "假", "空", "非"))):
            return self._parse_expr_stmt()

        if t.type == "OP" and t.value == ":":
            # 空块占位：单独的冒号行表示什么都不做
            self._next()
            return A.Pass(line=t.line, col=t.col)

        self._err(ParseUnexpectedError,
                  f"不知道怎么写这句话：{self._describe(t)}", tok=t)

    def _describe(self, t: Token) -> str:
        if t.type == "KEYWORD":
            return f"关键字「{t.value}」"
        if t.type == "NAME":
            return f"名字「{t.value}」"
        if t.type == "OP":
            return f"符号「{t.value}」"
        return f"「{t.value}」"

    # 前瞻：判断从当前位置开始是否是「名字 + 下标/属性 + 赋值符」的赋值语句
    def _looks_like_assign(self) -> bool:
        if self._peek().type != "NAME":
            return False
        i = self.pos + 1
        n = len(self.tokens)
        while i < n:
            t = self.tokens[i]
            if t.type == "OP" and t.value == "[":
                depth = 1
                i += 1
                while i < n and depth > 0:
                    c = self.tokens[i]
                    if c.type == "OP" and c.value == "[":
                        depth += 1
                    elif c.type == "OP" and c.value == "]":
                        depth -= 1
                    i += 1
                continue
            if (t.type == "OP" and t.value == "."
                    and i + 1 < n and self.tokens[i + 1].type == "NAME"):
                i += 2
                continue
            break
        return (i < n and self.tokens[i].type == "OP"
                and self.tokens[i].value in _ASSIGN_OPS)

    # 赋值
    def _parse_assign(self, decl: bool) -> A.Node:
        if decl:
            start = self._next()  # 「令」
            if self._at_op("*"):
                # `令 *余 = …`：Python 也要求写成 `*余, = …`。
                # 与其让用户对着「这里需要一个变量名」猜，不如直接说清。
                star_tok = self._peek()
                self._err(
                    ParseUnexpectedError,
                    "「令」后面不能直接写「*」——星号解包至少要有一个固定名字",
                    tok=star_tok,
                    hint="写成 令 甲, *余 = 列表（星号放中间或末尾），"
                         "或只收集全部：令 余 = 列表")
            target_tok = self._expect_name("变量名")
            target: A.Node = A.Name(id=target_tok.value, line=target_tok.line,
                                    col=target_tok.col)
            # 多赋值：令 a, b = ...；M25 起支持星号：令 a, *余 = ...
            if self._at_op(","):
                names = [target]
                star_index: Optional[int] = None
                while self._at_op(","):
                    self._next()
                    if self._at_op("*"):
                        star_tok = self._next()
                        if star_index is not None:
                            self._err(ParseUnexpectedError,
                                      "解包赋值里只能有一个「*」", tok=star_tok)
                        star_index = len(names)
                    elif self._at_op("**"):
                        self._err(ParseUnexpectedError,
                                  "解包赋值里只能用「*」收集剩下的，"
                                  "「**」是收集关键字用的",
                                  tok=self._peek())
                    tok = self._expect_name("变量名")
                    names.append(A.Name(id=tok.value, line=tok.line,
                                        col=tok.col))
                target = A.TargetList(names=names, star_index=star_index,
                                      line=start.line, col=start.col)
        else:
            start = self._peek()
            target = self._parse_postfix()  # 支持 名字[下标] = v、名字.属性 = v
        op_tok = self._next()
        if op_tok.type != "OP" or op_tok.value not in _ASSIGN_OPS:
            self._err(ParseUnexpectedError,
                      f"这里需要一个赋值符号（=、+= 等），但看到了"
                      f"「{op_tok.value}」", tok=op_tok)
        if isinstance(target, A.TargetList) and op_tok.value != "=":
            self._err(ParseUnexpectedError,
                      "多赋值（解包）只能用「=」，不能用复合赋值", tok=op_tok)
        value = (self._parse_value_tuple()
                 if isinstance(target, A.TargetList) else self._parse_expr())
        return A.Assign(target=target, op=op_tok.value, value=value,
                        line=start.line, col=start.col)

    # 表达式语句
    def _parse_expr_stmt(self) -> A.Node:
        # 元组赋值 lookahead：名字 , 名字 … = 形式 → 解包赋值
        tup = self._try_parse_tuple_assign()
        if tup is not None:
            return tup
        start = self._peek()
        expr = self._parse_expr()
        return A.ExprStmt(expr=expr, line=start.line, col=start.col)

    def _parse_value_tuple(self) -> A.Node:
        """赋值右值：表达式 (, 表达式)*。多个时包装成列表（多值即列表）。"""
        first = self._parse_expr()
        if not self._at_op(","):
            return first
        start = self._peek()
        elements = [first]
        while self._at_op(","):
            self._next()
            elements.append(self._parse_expr())
        return A.List(elements=elements, line=start.line, col=start.col)

    def _parse_fstring(self, tok: Token) -> A.Node:
        """把 FSTRING 分段组装成字符串拼接（M7 文本插值）。

        「你好 {名字}」→ "你好 " + 文本(名字)（编译期脱糖为 + 链）。
        """
        parts: list = tok.value   # [("text", str), ("expr", str), ...]
        nodes: list[A.Node] = []
        for kind, val in parts:
            if kind == "text":
                nodes.append(A.Str(value=val, line=tok.line, col=tok.col))
            else:
                sub_tokens = tokenize(val, self.filename)
                sub = Parser(sub_tokens, [val], self.filename)
                expr = sub._parse_expr()
                nodes.append(A.Call(
                    func=A.Name(id="文本", line=tok.line, col=tok.col),
                    args=[expr], line=tok.line, col=tok.col))
        if not nodes:
            return A.Str(value="", line=tok.line, col=tok.col)
        result = nodes[0]
        for n in nodes[1:]:
            result = A.BinOp(left=result, op="+", right=n,
                             line=tok.line, col=tok.col)
        return result

    def _parse_comp_tail(self, *, kind: str, elt: Optional[A.Node],                         key: Optional[A.Node] = None,
                         value: Optional[A.Node] = None,
                         line: int, col: int) -> A.Node:
        """解析推导式尾部：遍历 变量 在 可迭代 [如果 条件]（M7）。

        调用时「遍历」关键字尚未消费，当前游标指向它。
        """
        self._expect_kw("遍历")
        target_tok = self._expect_name("循环变量名")
        self._expect_kw("在")
        it = self._parse_expr()
        target = A.Name(id=target_tok.value, line=target_tok.line,
                        col=target_tok.col)
        condition = None
        if self._at_kw("如果"):
            self._next()
            condition = self._parse_expr()
        return A.Comprehension(
            kind=kind, elt=elt, key=key, value=value,
            target=target, iter=it, condition=condition,
            line=line, col=col)

    def _try_parse_tuple_assign(self) -> Optional[A.Node]:
        """探测「名字 , 名字 … = 表达式」；不匹配则返回 None 不消耗 token。

        M25 起支持星号解包：`令 甲, *余 = [1, 2, 3]`、`令 *头, 尾 = …`。
        """
        i = 0
        specs: list[tuple[bool, Token]] = []      # (是否带星号, 名字 token)
        while True:
            t = self._peek(i)
            is_star = False
            if t.type == "OP" and t.value == "*":
                is_star = True
                t = self._peek(i + 1)
                i += 1
            if t.type != "NAME":
                return None
            nxt = self._peek(i + 1)
            if nxt.type == "OP" and nxt.value == ",":
                specs.append((is_star, t))
                i += 2
                continue
            if nxt.type == "OP" and nxt.value == "=" and len(specs) >= 1:
                specs.append((is_star, t))     # 最后一个名字
                break
            return None

        # 确认是解包赋值：真正消耗这些 token
        first = specs[0][1]
        nodes: list[A.Name] = []
        star_index: Optional[int] = None
        for k, (is_star, tok) in enumerate(specs):
            if is_star:
                self._next()                   # 消费「*」
                if star_index is not None:
                    self._err(ParseUnexpectedError,
                              "解包赋值里只能有一个「*」", tok=tok)
                star_index = k
            name_tok = self._next()            # 消费名字
            nodes.append(A.Name(id=name_tok.value, line=name_tok.line,
                                col=name_tok.col))
            if k < len(specs) - 1:
                self._next()                   # 消费逗号
        self._next()                           # 消费「=」
        value = self._parse_value_tuple()
        return A.Assign(
            target=A.TargetList(names=nodes, star_index=star_index,
                                line=first.line, col=first.col),
            op="=", value=value, line=first.line, col=first.col)

    # 如果 / 否则如果 / 否则
    def _parse_if(self) -> A.Node:
        start = self._next()  # 如果
        branches = []
        test = self._parse_expr()
        self._expect_colon("如果")
        body = self._parse_block()
        branches.append((test, body))
        orelse = None
        while self._at_kw("否则") or self._at_kw("否则如果"):
            tok = self._next()
            if tok.value == "否则如果" or self._at_kw("如果"):
                # 「否则如果」整体，或「否则 如果」分开写
                if tok.value == "否则":
                    self._next()  # 消费「如果」
                t2 = self._parse_expr()
                self._expect_colon("否则如果")
                b2 = self._parse_block()
                branches.append((t2, b2))
            else:
                self._expect_colon("否则")
                orelse = self._parse_block()
                break
        return A.If(branches=branches, orelse=orelse,
                    line=start.line, col=start.col)

    # 遍历
    def _parse_for(self) -> A.Node:
        start = self._next()
        target_tok = self._expect_name("循环变量名")
        self._expect_kw("在")
        it = self._parse_expr()
        self._expect_colon("遍历")
        body = self._parse_block()
        return A.For(target=A.Name(id=target_tok.value,
                                   line=target_tok.line, col=target_tok.col),
                     iter=it, body=body, line=start.line, col=start.col)

    # 循环 N 次
    def _parse_loop(self) -> A.Node:
        start = self._next()
        times = self._parse_expr()
        if self._at_kw("次"):
            self._next()
        self._expect_colon("循环")
        body = self._parse_block()
        return A.Loop(times=times, body=body, line=start.line, col=start.col)

    # 当
    def _parse_while(self) -> A.Node:
        start = self._next()
        test = self._parse_expr()
        self._expect_colon("当")
        body = self._parse_block()
        return A.While(test=test, body=body, line=start.line, col=start.col)

    # 函数
    def _parse_params(self) -> tuple[list[str], list, list, list]:
        """解析 `( … )` 里的形参表，返回 (params, defaults, annotations, kinds)。

        支持（M25）：普通参数、默认值 `y = 1`、类型标注 `x: 整数`、
        `*参数`（收集多余的位置实参成列表）、`**选项`（收集未匹配的关键字成字典）。

        顺序用一个三态标记约束（与 Python 对齐，都尽早报中文错）：
        ``普通… → *参数? → **选项?``。简化点：`*参数` 之后不允许再有普通参数
        （那属于「只能关键字传参」的进阶写法，暂不开放，直接给中文提示）。
        """
        self._expect_op("(")
        params: list[str] = []
        defaults: list = []
        annotations: list = []
        kinds: list[int] = []
        #: "normal" → 还能接普通参数；"starred" → 只接 **选项；"done" → 收尾
        state = "normal"
        star_tok = None

        if not self._at_op(")"):
            while True:
                kind = ParamKind.NORMAL
                if self._at_op("**") or self._at_op("*"):
                    is_dstar = self._peek().value == "**"
                    tok = self._next()
                    if is_dstar:
                        if state == "done":
                            self._err(ParseUnexpectedError,
                                      "「**选项」只能有一个", tok=tok)
                        state = "done"
                        kind = ParamKind.VARKW
                    else:
                        if state != "normal":
                            self._err(
                                ParseUnexpectedError,
                                "「*参数」只能有一个，而且要写在「**选项」前面",
                                tok=tok)
                        state = "starred"
                        star_tok = tok
                        kind = ParamKind.VARARGS
                else:
                    if state == "starred":
                        self._err(
                            ParseUnexpectedError,
                            "「*参数」后面不能再有普通参数——"
                            "多余的位置实参都归它收了",
                            tok=star_tok,
                            hint="要传更多参数请用关键字，"
                                 "或把普通参数写在「*参数」前面")
                    if state == "done":
                        self._err(ParseUnexpectedError,
                                  "「**选项」必须是最后一个参数",
                                  tok=self._peek())

                p = self._expect_name("参数名")
                params.append(p.value)
                kinds.append(kind)

                ann = self._parse_annotation_opt()
                if kind != ParamKind.NORMAL and ann is not None:
                    self._err(
                        ParseUnexpectedError,
                        f"「{'**' if kind == ParamKind.VARKW else '*'}"
                        f"{p.value}」是收集参数，不能写类型标注",
                        tok=p,
                        hint="它收进来的元素类型不固定；"
                             "要检查请在函数体里用 断言")
                annotations.append(ann)

                if self._at_op("="):
                    if kind != ParamKind.NORMAL:
                        self._err(
                            ParseUnexpectedError,
                            f"「{'**' if kind == ParamKind.VARKW else '*'}"
                            f"{p.value}」是收集参数，不能有默认值",
                            tok=p,
                            hint="它没收到东西时自然是空列表/空字典")
                    self._next()
                    defaults.append(self._parse_expr())
                else:
                    defaults.append(None)

                if self._at_op(","):
                    self._next()
                    if self._at_op(")"):     # 尾随逗号
                        break
                    continue
                break

        # 有默认值的参数必须排在最后（与 Python 一致）
        seen_default = False
        for i, d in enumerate(defaults):
            if d is not None:
                seen_default = True
            elif seen_default and kinds[i] == ParamKind.NORMAL:
                self._err(ParseUnexpectedError,
                          f"参数「{params[i]}」没有默认值，"
                          f"但它排在有默认值的参数后面——"
                          f"有默认值的参数要放在最后")
        self._expect_op(")")
        return params, defaults, annotations, kinds

    def _parse_funcdef(self) -> A.Node:
        start = self._next()
        name_tok = self._expect_name("函数名")
        params, defaults, annotations, kinds = self._parse_params()
        # 返回类型标注（M24.3）：-> 类型名
        returns = None
        if self._at_op("->"):
            self._next()
            returns = self._parse_annotation_name("返回类型")
        self._expect_colon("函数")
        body = self._parse_block()
        return A.FuncDef(name=name_tok.value, params=params, body=body,
                         defaults=defaults, annotations=annotations,
                         param_kind=kinds, returns=returns,
                         line=start.line, col=start.col)

    def _parse_annotation_name(self, what: str) -> str:
        """读取一个类型标注名（支持 `模块.名字` 这种带点的写法）。

        限定成「名字[.名字]*」而不是任意表达式：标注是给人看的类型名，
        写成表达式只会让人困惑，尽早报错更友好。
        """
        tok = self._expect_name(what)
        text = tok.value
        while self._at_op("."):
            self._next()
            text += "." + self._expect_name(what).value
        return text

    def _parse_annotation_opt(self) -> Optional[str]:
        """可选的 `: 类型名` 参数标注；没写返回 None。"""
        if not self._at_op(":"):
            return None
        self._next()
        return self._parse_annotation_name("类型名")

    # 类
    def _err_at(self, exc: type, msg: str, node: A.Node,
                hint: Optional[str] = None):
        """按 AST 节点位置报错（块解析完之后拿不到 token 时用）。

        块语句（类体、函数体）解析完之后，`self._peek()` 已经指到块后面去了，
        用 `_err` 会把错误标在下一行。这里按节点自带的 line/col 合成一个
        最小 token，让下划线落在**出错那一行**上。
        """
        line = getattr(node, "line", 1) or 1
        col = getattr(node, "col", 1) or 1
        src = (self.source_lines[line - 1]
               if 0 < line <= len(self.source_lines) else "")
        tok = Token("NAME", "", line=line, col=col, end_col=col + 1,
                    source_line=src)
        self._err(exc, msg, hint=hint, tok=tok)

    def _check_class_body(self, body: list[A.Node]) -> None:
        """类体只允许「方法 / 类变量 / 占位」——**解析期**统一校验（M35）。

        为什么要在解析期拦：不拦的话三个执行器给的答案**各不相同**，而且
        一个比一个糟（M35 实测）：

        | 类体里写了 | 树遍历 | Python VM / C VM |
        |---|---|---|
        | `打印("…")` | 报「不支持 ExprStmt」 | **静默忽略**（副作用没发生） |
        | `x += 5` | 报「不支持 Assign」 | 当成 `x = 5` **静默算错** |
        | `x[0] = 1` | 报「不支持 Assign」 | 抛英文 `'Subscript' object has no attribute 'id'` |
        | `"说明"` | 报「不支持 ExprStmt」 | 静默忽略 |
        | `:`（空类占位） | 报「不支持 Pass」 | 静默忽略（**但空类就该这么写**） |

        在解析期拦下来，一处覆盖全部执行器（它们都要过 parser），
        而且把「静默算错 / 英文内部错」统一成一条说得清的中文提示。
        顺便让 `:`（空类占位）在三个执行器上都合法——这是空类的自然写法。

        （注：空块在基石里是**单独一行冒号**，没有 Python 那样的 `pass` 关键字；
        M32 时曾把这条记成「树遍历不支持类体里写 通过」——那是个误判：
        `通过` 从来不是关键字，它是个名字，于是被当成表达式语句。）
        """
        for node in body:
            if isinstance(node, A.FuncDef):
                continue
            if isinstance(node, A.Pass):
                continue                      # 空类占位：单独一行「:」
            if (isinstance(node, A.Assign)
                    and isinstance(node.target, A.Name) and node.op == "="):
                continue                      # 类变量
            kind = type(node).__name__
            if isinstance(node, A.Assign):
                if isinstance(node.target, A.Name):
                    msg = (f"类体里不能用「{node.target.id} {node.op}」——"
                           f"类变量只能在类体里用一次性的「名字 = 值」声明")
                else:
                    msg = ("类体里不能对下标或属性赋值，"
                           "只能用「名字 = 值」定义类变量")
                hint = ("类变量要在类体里用一次性的「=」声明：\n"
                        "类 甲：\n"
                        "    计数 = 0          # 类变量\n"
                        "    函数 加(自身)：    # 方法\n"
                        "        甲.计数 += 1   # 改类变量写在方法里")
            elif isinstance(node, A.ExprStmt):
                if (isinstance(node.expr, A.Name)
                        and node.expr.id == "通过"):
                    # 从 Python 借来的写法，特意点名——否则用户会以为
                    # 「通过」是关键字只是位置不对（M35 实测确实有人这么写）
                    self._err_at(
                        ParseUnexpectedError,
                        "类体里不能写「通过」——它不是关键字，只是个普通名字",
                        node,
                        hint=("空类用「单独一行冒号」占位：\n"
                              "类 空类：\n"
                              "    :\n\n"
                              "（基石没有 Python 的 `pass`；`通过` 在别的地方"
                              "也只是个名字，写了会报「找不到这个名字」。）"))
                    continue
                msg = "类体里不能写「表达式语句」（比如调用函数）"
                hint = ("类体里只放声明，不放会执行的东西：\n"
                        "类 甲：\n"
                        "    计数 = 0\n"
                        "    函数 打招呼(自身)：\n"
                        "        打印(\"你好\")   # 想执行就写进方法里\n\n"
                        "想在类上写说明文字，请用注释（# 开头）。")
            else:
                msg = (f"类体里只能写方法（函数）、类变量（名字 = 值）"
                       f"或空类占位（:），不能写「{kind}」")
                hint = _CLASS_HINT
            self._err_at(ParseUnexpectedError, msg, node, hint=hint)

    def _parse_classdef(self) -> A.Node:
        start = self._next()
        name_tok = self._expect_name("类名")
        base = None
        if self._at_kw("继承"):
            self._next()
            base = self._expect_name("基类名").value
        self._expect_colon("类")
        body = self._parse_block()
        self._check_class_body(body)

        # M23.4：方法体里的 `超()` → `超(自身, "定义类名")`。
        # 解析期就知道「写在哪个类里」，比运行时猜实例的类更准
        #（多级继承时运行时猜会错，见 _desugar_super 的说明）。
        for node in body:
            if isinstance(node, A.FuncDef):
                self_param = node.params[0] if node.params else None
                _desugar_super(node, name_tok.value, self_param, self.filename)

        return A.ClassDef(name=name_tok.value, base=base, body=body,
                          line=start.line, col=start.col)

    # 返回
    def _parse_return(self) -> A.Node:
        start = self._next()
        value = None
        if not self._at("NEWLINE") and self._peek().type != "EOF":
            value = self._parse_expr()
            # 多值返回：返回 a, b → 返回列表（基石的多值即列表）
            if self._at_op(","):
                elements = [value]
                while self._at_op(","):
                    self._next()
                    elements.append(self._parse_expr())
                value = A.List(elements=elements, line=start.line,
                               col=start.col)
        return A.Return(value=value, line=start.line, col=start.col)

    # 导入
    def _parse_import(self) -> A.Node:
        start = self._next()
        name_tok = self._expect_name("模块名")
        from_python = False
        from_local = False
        alias = None
        if self._at_kw("从"):
            self._next()
            src = self._expect_name("来源")
            if src.value == "python":
                from_python = True
            elif src.value == "本地包":
                from_local = True
            else:
                self._err(ParseUnexpectedError,
                          f"只能「从 python」或「从 本地包」导入，"
                          f"看到了「{src.value}」", tok=src)
            if self._at_kw("为"):
                self._next()
                alias_tok = self._expect_name("别名")
                alias = alias_tok.value
        elif self._at_kw("为"):
            # M18.4：普通标准库导入也支持别名（导入 文本 为 t），避免遮蔽内建
            self._next()
            alias_tok = self._expect_name("别名")
            alias = alias_tok.value
        return A.Import(name=name_tok.value, from_python=from_python,
                        from_local=from_local, alias=alias,
                        line=start.line, col=start.col)

    # 抛出
    def _parse_raise(self) -> A.Node:
        start = self._next()
        if self._at("NEWLINE") or self._at("DEDENT") or self._peek().type == "EOF":
            self._err(ParseUnexpectedError,
                      "「抛出」后面要写一个值（比如 抛出 值错误(\"说明\")）",
                      tok=start)
        value = self._parse_expr()
        return A.Raise(value=value, line=start.line, col=start.col)

    # 上下文管理器（M26.2）
    def _parse_with(self):
        """`用 <表达式> 为 <名字>：` —— **解析期脱糖**，不新增 AST 节点。

        脱糖成：

            __用_N__ = <表达式>
            名字 = 进入上下文(__用_N__)
            尝试：
                <原来的代码块>
            最终：
                退出上下文(__用_N__)

        好处是三个执行器一行都不用改（`尝试/最终` 的语义早就对齐了，
        `返回`/`中断` 穿透 finally 也早就处理过），行为必然一致。
        `进入上下文`/`退出上下文` 只是普通内建，而 C VM 调内建本来就走
        宿主 —— 与 M23 的 `超()`、M24 的集合字面量同一手法。

        为什么留一个临时变量：**退出要作用在原来的上下文对象上**，不能
        用绑定后的名字。`进入(自身)` 完全可以返回另一个东西（Python 的
        `__enter__` 也允许），拿返回值去 `退出` 就会找错对象。临时变量名
        带双下划线，用户不会撞上。
        """
        start = self._next()                    # 用
        context = self._parse_expr()
        if not self._at_kw("为"):
            self._err(ParseUnexpectedError,
                      "「用」后面要用「为」接一个名字",
                      hint="写法是：\n用 打开(\"数据.txt\") 为 f：\n    内容 = f.读()",
                      tok=self._peek())
        self._next()                            # 为
        name_tok = self._expect_name("名字")
        self._expect_colon("用")
        body = self._parse_block()

        self._with_seq += 1
        tmp = f"__用_{self._with_seq}__"
        pos = {"line": start.line, "col": start.col}
        hold = A.Assign(
            target=A.Name(id=tmp, **pos), op="=", value=context, **pos)
        bind = A.Assign(
            target=A.Name(id=name_tok.value,
                          line=name_tok.line, col=name_tok.col),
            op="=",
            value=A.Call(func=A.Name(id="进入上下文", **pos),
                         args=[A.Name(id=tmp, **pos)], **pos),
            **pos)
        cleanup = A.Try(
            body=body,
            handlers=[],
            finalbody=[A.ExprStmt(
                expr=A.Call(func=A.Name(id="退出上下文", **pos),
                            args=[A.Name(id=tmp, **pos)],
                            **pos),
                **pos)],
            **pos)
        # 返回三条语句；_parse_statements 会把 list 展平
        return [hold, bind, cleanup]

    # 模式匹配（M34.1）
    def _parse_match(self):
        """`匹配 <表达式>：` —— **解析期脱糖**成 if/elif 链，不新增 AST 节点。

        脱糖成：

            __匹配_N__ = <表达式>
            如果 __匹配_N__ == <值①>：
                …
            否则如果 __匹配_N__ == <值②> 或 __匹配_N__ == <值③>：
                …
            否则：
                …

        「解析期脱糖优先」在本项目已经验证过三次（M23 的 `超()`、M24 的
        集合字面量、M26 的 `用 … 为`），这是第四次：**三个执行器一行都不用
        改**，因为 `如果/否则如果/否则` 与 `==`/`或`/`与` 的语义早就对齐了。

        待匹配值先落到临时变量，所以 `<表达式>` **只求值一次**
        （`匹配 取下一个()` 不会因为分支多就跑多次）。
        """
        start = self._next()                       # 匹配
        subject = self._parse_expr()
        self._expect_colon("匹配")
        cases = self._parse_case_block()
        if not cases:
            self._err(ParseBlockError,
                      "「匹配」里至少要写一个「情形」",
                      hint=_MATCH_HINT, tok=start)
        return self._desugar_match(subject, cases, start)

    def _parse_case_block(self) -> list:
        """解析「匹配」的代码块：里面只能是一串「情形」子块。

        不能用 `_parse_block`——它解析的是普通语句，而这里每一行都必须是
        「情形 …：」开头（出了别的就报错，免得用户把普通语句写进来却不生效）。
        """
        if not self._at("NEWLINE"):
            self._err(ParseBlockError,
                      "「匹配」后面必须换行，然后缩进写「情形」",
                      hint=_MATCH_HINT)
        self._next()                               # NEWLINE
        if not self._at("INDENT"):
            self._err(ParseBlockError,
                      "「匹配」的代码块里要写「情形」",
                      hint=_MATCH_HINT)
        self._next()                               # INDENT
        cases = []
        while not self._at("DEDENT") and not self._at("EOF"):
            if self._at("NEWLINE"):
                self._next()
                continue
            if not self._at_kw("情形"):
                self._err(ParseUnexpectedError,
                          "「匹配」的代码块里只能写「情形」，"
                          f"但看到了{self._describe(self._peek())}",
                          hint=_MATCH_HINT)
            cases.append(self._parse_case())
        if self._at("DEDENT"):
            self._next()
        return cases

    def _parse_case(self) -> tuple:
        """一条「情形」：模式（可多个，逗号分隔）+ 可选守卫「如果 条件」。"""
        start = self._next()                       # 情形
        t = self._peek()
        wildcard = False
        # 两种「不带值」的写法：
        #   `情形 其他：`        通配，兜底（不带守卫时必须放最后）
        #   `情形 如果 条件：`    只有条件，相当于这一条是普通的 if 分支
        # 后者让「按条件一段段筛」写起来很自然：
        #     匹配 分数：
        #         情形 如果 分数 >= 90：……
        #         情形 如果 分数 >= 60：……
        #         情形 其他：……
        if self._at_kw("如果"):
            wildcard = True
        elif t.type == "NAME" and t.value in ("其他", "_"):
            nxt = self._peek(1)
            if ((nxt.type == "OP" and nxt.value == ":")
                    or (nxt.type == "KEYWORD" and nxt.value == "如果")):
                # 想匹配一个**名叫「其他」的变量**，写 `情形 (其他)：` 即可——
                # 加括号后走普通表达式解析，不会被当成通配。
                self._next()
                wildcard = True
        patterns: list[A.Node] = []
        if not wildcard:
            patterns.append(self._parse_expr())
            while self._at_op(","):
                self._next()
                patterns.append(self._parse_expr())
        guard = None
        if self._at_kw("如果"):
            self._next()
            guard = self._parse_expr()
        self._expect_colon("情形")
        body = self._parse_block()
        return (wildcard, patterns, guard, body, start)

    def _desugar_match(self, subject, cases, start) -> list:
        pos = {"line": start.line, "col": start.col}
        self._match_seq += 1
        tmp = f"__匹配_{self._match_seq}__"
        bind = A.Assign(target=A.Name(id=tmp, **pos), op="=",
                        value=subject, **pos)

        branches: list = []
        orelse: Optional[list] = None
        for i, (wildcard, patterns, guard, body, ctok) in enumerate(cases):
            cpos = {"line": ctok.line, "col": ctok.col}
            if wildcard:
                if guard is None:
                    # 裸兜底：它把剩下的情况全接住，所以必须最后
                    if i != len(cases) - 1:
                        self._err(ParseUnexpectedError,
                                  "「情形 其他」要放在最后一条"
                                  "（它把剩下的情况全接住）",
                                  hint=_MATCH_HINT, tok=ctok)
                    orelse = body
                else:
                    # 带守卫的通配（`情形 其他 如果 条件：`）就是个普通条件分支，
                    # 可以放在中间——不成立时继续往下试
                    branches.append((guard, body))
                continue
            tests = [
                A.Compare(left=A.Name(id=tmp, **cpos), ops=["=="],
                          comparators=[p], **cpos)
                for p in patterns
            ]
            test = tests[0] if len(tests) == 1 else A.BoolOp(
                op="或", values=tests, **cpos)
            if guard is not None:
                # 守卫写成 `模式 如果 条件`，等价于「模式命中**且**条件成立」
                test = A.BoolOp(op="与", values=[test, guard], **cpos)
            branches.append((test, body))

        out: list[A.Node] = [bind]
        if not branches:
            # 只有一条无守卫的「情形 其他」——块体无条件执行，摊平即可
            out.extend(orelse or [])
            return out
        out.append(A.If(branches=branches, orelse=orelse, **pos))
        return out

    # 枚举（M34.2）
    def _parse_enum(self):
        """`枚举 <名字>：` —— **解析期脱糖**成「类 + 成员实例」，不新增 AST 节点。

            enum 颜色:
                红 = 1
                绿 = 2

        脱糖成：

            类 颜色：
                函数 初始化(自身, 名字, 值)：
                    自身.名字 = 名字
                    自身.值 = 值
                函数 文本(自身)：
                    返回 "颜色." + 自身.名字
            颜色.红 = 新建 颜色("红", 1)
            颜色.绿 = 新建 颜色("绿", 2)
            颜色.全部 = [颜色.红, 颜色.绿]

        为什么成员是**实例**而不是普通值：这样 `打印(颜色.红)` 给
        `颜色.红`（靠 `文本()` 方法，M27 已支持），还能取 `.名字` / `.值`。
        若成员直接是 `1`，打印出来就是 `1`，枚举「用名字代替魔法数字」的
        意义就没了一半。

        `==` 按身份（同一次创建即相等），与 Python 的 Enum 一致：
        `颜色.红 == 颜色.红` 为真、`颜色.红 == 1` 为假。
        `颜色.全部` 是给 `遍历` 用的清单——**类对象本身不可遍历**
        （`迭代()` 是实例方法），所以给一份清单更顺手。
        """
        start = self._next()                       # 枚举
        name_tok = self._expect_name("枚举名")
        self._expect_colon("枚举")
        members = self._parse_enum_members()
        if not members:
            self._err(ParseBlockError,
                      "「枚举」里至少要写一个成员",
                      hint=_ENUM_HINT, tok=start)
        return self._desugar_enum(name_tok.value, members, start, name_tok)

    def _parse_enum_members(self) -> list:
        """解析枚举体：每行 `成员 = 值`，或只写 `成员` 让值自动编号。"""
        if not self._at("NEWLINE"):
            self._err(ParseBlockError,
                      "「枚举」后面必须换行，然后缩进写成员",
                      hint=_ENUM_HINT)
        self._next()                               # NEWLINE
        if not self._at("INDENT"):
            self._err(ParseBlockError,
                      "「枚举」需要一个代码块：换行缩进，写「成员 = 值」",
                      hint=_ENUM_HINT)
        self._next()                               # INDENT
        members: list = []                         # [(名字 token, 值节点)]
        next_auto: Optional[int] = 1
        while not self._at("DEDENT") and not self._at("EOF"):
            if self._at("NEWLINE"):
                self._next()
                continue
            mtok = self._expect_name("成员名")
            if self._at_op("="):
                self._next()
                value = self._parse_expr()
                # 值是整数字面量才允许后面的成员自动接着编号
                v = getattr(value, "value", None)
                if (isinstance(value, A.Num) and isinstance(v, int)
                        and not isinstance(v, bool)):
                    next_auto = v + 1
                else:
                    next_auto = None
                members.append((mtok, value))
            else:
                if next_auto is None:
                    self._err(ParseUnexpectedError,
                              f"成员「{mtok.value}」要写值",
                              hint="上一个成员的值不是整数，没法自动编号，"
                                   "所以这里要写成「名字 = 值」",
                              tok=mtok)
                members.append((mtok, A.Num(
                    value=next_auto, line=mtok.line, col=mtok.col)))
                next_auto += 1
            if self._at("NEWLINE"):
                self._next()
        if self._at("DEDENT"):
            self._next()
        return members

    def _desugar_enum(self, cls_name: str, members: list, start, name_tok) -> list:
        pos = {"line": start.line, "col": start.col}
        mpos = {"line": name_tok.line, "col": name_tok.col}
        self_name = A.Name(id="自身", **mpos)

        def _field(name: str) -> A.Attr:
            return A.Attr(obj=self_name, attr=name, **mpos)

        init_fn = A.FuncDef(
            name="初始化",
            params=["自身", "名字", "值"],
            body=[
                A.Assign(target=_field("名字"), op="=",
                         value=A.Name(id="名字", **mpos), **mpos),
                A.Assign(target=_field("值"), op="=",
                         value=A.Name(id="值", **mpos), **mpos),
            ],
            **mpos)
        repr_fn = A.FuncDef(
            name="文本",
            params=["自身"],
            body=[A.Return(
                value=A.BinOp(left=A.Str(value=cls_name + ".", **mpos),
                              op="+", right=_field("名字"), **mpos),
                **mpos)],
            **mpos)
        out: list[A.Node] = [A.ClassDef(
            name=cls_name, base=None, body=[init_fn, repr_fn], **pos)]

        refs = []
        for mtok, value in members:
            kpos = {"line": mtok.line, "col": mtok.col}
            attr = A.Attr(obj=A.Name(id=cls_name, **pos), attr=mtok.value,
                          **kpos)
            out.append(A.Assign(
                target=attr, op="=",
                value=A.Call(func=A.Name(id=cls_name, **pos),
                             args=[A.Str(value=mtok.value, **kpos), value],
                             **kpos),
                **kpos))
            refs.append(A.Attr(obj=A.Name(id=cls_name, **pos),
                               attr=mtok.value, **kpos))
        out.append(A.Assign(
            target=A.Attr(obj=A.Name(id=cls_name, **pos), attr="全部", **pos),
            op="=", value=A.List(elements=refs, **pos), **pos))
        return out

    # -- 块 ----------------------------------------------------------------

    def _expect_colon(self, kw: str):
        if self._at_op(":"):
            self._next()
            return
        self._err(ParseMissingColonError,
                  f"「{kw}」后面要跟冒号「：」，然后换行缩进写代码块")

    def _parse_block(self) -> list[A.Node]:
        if not self._at("NEWLINE"):
            self._err(ParseBlockError,
                      "冒号后面必须换行，然后缩进写代码块",
                      hint="例如：\n如果 真：\n    打印(1)")
        self._next()  # NEWLINE
        if self._at("INDENT"):
            self._next()
            stmts = self._parse_statements(until=("DEDENT",))
            if self._at("DEDENT"):
                self._next()
            return stmts
        # 无缩进块
        if self._at_op(":"):
            self._next()
            return [A.Pass(line=self._peek().line, col=self._peek().col)]
        self._err(ParseBlockError,
                  "这个语句需要一个代码块：换行后缩进，或写一个冒号表示空块",
                  hint="例如：\n如果 真：\n    打印(1)")

    # 尝试 / 捕获 / 最终
    def _parse_try(self) -> A.Node:
        start = self._next()  # 尝试
        self._expect_colon("尝试")
        body = self._parse_block()

        handlers: list[A.ExceptHandler] = []
        finalbody = None
        while self._at_kw("捕获"):
            h_tok = self._next()  # 捕获
            htype = None
            hname = None
            # 捕获 类型错误 为 e: / 捕获 为 e: / 捕获:
            if self._peek().type == "NAME":
                htype = self._parse_expr()
                if self._at_kw("为"):
                    self._next()
                    n = self._expect_name("异常变量的名字")
                    hname = n.value
            elif self._at_kw("为"):
                self._next()
                n = self._expect_name("异常变量的名字")
                hname = n.value
            self._expect_colon("捕获")
            hbody = self._parse_block()
            handlers.append(A.ExceptHandler(
                type=htype, name=hname, body=hbody,
                line=h_tok.line, col=h_tok.col))

        if self._at_kw("最终"):
            f_tok = self._next()
            self._expect_colon("最终")
            finalbody = self._parse_block()

        if not handlers and finalbody is None:
            self._err(ParseUnexpectedError,
                      "「尝试」后面至少要有一个「捕获」或一个「最终」",
                      tok=start,
                      hint="写法是：\n尝试：\n    …\n捕获 异常 为 e：\n    …")
        return A.Try(body=body, handlers=handlers, finalbody=finalbody,
                     line=start.line, col=start.col)

    # 抛出
    def _binop_of(self, t: Token) -> Optional[str]:
        """把 token 识别成二元运算符名；不是运算符返回 None。

        运算符有两类写法（M23.2 起）：
        - 符号：``+ - * / // % ** == != < > <= >=``（token 类型 OP）
        - 关键字：``与 或 在 不在 是 不是``（token 类型 KEYWORD）

        统一成同一个字符串，让优先级表 ``_BIN_PREC`` 一处生效。
        注意「与/或」不在这里返回——它们由 _parse_expr 里更靠前的分支
        单独处理（要合并同优先级链，形态与 BinOp 不同）。
        """
        if t.type == "OP" and t.value in _BIN_PREC:
            return t.value
        if t.type == "KEYWORD" and t.value in _KEYWORD_CMP_OPS:
            return t.value
        return None

    # -- 表达式：优先级爬升 --------------------------------------------------

    def _parse_expr(self, min_prec: int = 0) -> A.Node:
        # 「非」是**前缀**运算符，但优先级不是「最紧」，而是比比较运算松、
        # 比「与/或」紧 —— 与 Python 的 ``not`` 一致（M23 修正）。
        #
        # 为什么必须这样：`非 年龄 >= 18` 若按「非最紧」解析会变成
        # `(非 年龄) >= 18`，结果是「假 >= 18」恒为假，**静默给出错误答案**
        # （官方教程 docs/tutorial/03 就踩过这个坑，「未成年人」分支永不触发）。
        # 按 Python 语义解析成 `非 (年龄 >= 18)`，才符合所有人的阅读直觉。
        t0 = self._peek()
        if t0.type == "KEYWORD" and t0.value == "非":
            self._next()
            operand = self._parse_expr(_NOT_PREC)
            left: A.Node = A.UnaryOp(op="非", operand=operand,
                                     line=t0.line, col=t0.col)
        else:
            left = self._parse_unary()
        while True:
            t = self._peek()
            if t.type == "KEYWORD" and t.value in ("与", "或"):
                prec = _BIN_PREC[t.value]
                if prec < min_prec:
                    break
                op = t.value
                self._next()
                # 合并同优先级链：a 与 b 与 c
                right = self._parse_expr(prec + 1)
                if isinstance(left, A.BoolOp) and left.op == op:
                    left.values.append(right)
                else:
                    left = A.BoolOp(op=op, values=[left, right],
                                    line=t.line, col=t.col)
                continue
            op = self._binop_of(t)
            if op is not None:
                prec = _BIN_PREC[op]
                if prec < min_prec:
                    break
                self._next()
                if op == "**":
                    # 右结合
                    right = self._parse_expr(prec)
                else:
                    right = self._parse_expr(prec + 1)
                if op in _ALL_CMP_OPS:
                    # 链式比较：1 < x < 5，收集连续的比较运算，中间表达式只求值一次
                    ops = [op]
                    comparators = [right]
                    while True:
                        nxt = self._peek()
                        nxt_op = self._binop_of(nxt)
                        if nxt_op is None or nxt_op not in _ALL_CMP_OPS:
                            break
                        prec2 = _BIN_PREC[nxt_op]
                        if prec2 < min_prec:
                            break
                        self._next()
                        comparators.append(self._parse_expr(prec2 + 1))
                        ops.append(nxt_op)
                    left = A.Compare(left=left, ops=ops,
                                     comparators=comparators,
                                     line=t.line, col=t.col)
                else:
                    left = A.BinOp(left=left, op=op, right=right,
                                   line=t.line, col=t.col)
                continue
            break
        return left

    def _parse_unary(self) -> A.Node:
        t = self._peek()
        if t.type == "OP" and t.value == "-":
            self._next()
            operand = self._parse_unary()
            return A.UnaryOp(op="-", operand=operand, line=t.line, col=t.col)
        if t.type == "KEYWORD" and t.value == "非":
            # 走到这里说明「非」出现在不该出现的位置（正常路径由
            # _parse_expr 的前缀分支处理）。保留兜底以免直接崩。
            self._next()
            operand = self._parse_expr(_NOT_PREC)
            return A.UnaryOp(op="非", operand=operand, line=t.line, col=t.col)
        return self._parse_postfix()

    def _parse_postfix(self) -> A.Node:
        node = self._parse_atom()
        while True:
            t = self._peek()
            if t.type == "OP" and t.value == "(":
                self._next()
                args: list[A.Node] = []
                keywords: list[tuple[str, A.Node]] = []
                if not self._at_op(")"):
                    while True:
                        # 关键字参数：名字 = 值。调用内部没有赋值语义，
                        # 所以「f(a = 1)」里的 a = 1 一律按名字参数处理
                        if (self._peek().type == "NAME"
                                and self._peek(1).type == "OP"
                                and self._peek(1).value == "="):
                            kw_tok = self._next()
                            self._next()  # 消费 =
                            keywords.append((kw_tok.value,
                                             self._parse_expr()))
                        else:
                            args.append(self._parse_expr())
                        if self._at_op(","):
                            self._next()
                            if self._at_op(")"):  # 尾随逗号
                                break
                            continue
                        break
                self._expect_op(")")
                node = A.Call(func=node, args=args, keywords=keywords,
                              line=t.line, col=t.col)
            elif t.type == "OP" and t.value == ".":
                self._next()
                attr_tok = self._expect_attr_name()
                node = A.Attr(obj=node, attr=attr_tok.value,
                              line=t.line, col=t.col)
            elif t.type == "OP" and t.value == "[":
                self._next()
                # M18.1 切片：看是否含冒号（列表[1:3] / [::-1] / [:] / [::2]）
                if self._looks_like_slice():
                    node = self._parse_slice(node, t)
                else:
                    index = self._parse_expr()
                    self._expect_op("]")
                    node = A.Subscript(obj=node, index=index,
                                       line=t.line, col=t.col)
            else:
                break
        return node

    def _looks_like_slice(self) -> bool:
        """向前看：当前下标位置是否是一个切片（含冒号）。

        判断依据：在匹配的 `]` 之前出现一个 `:`（且不在字符串/嵌套括号里）。
        普通下标也可能含冒号吗？字典值/表达式里没有 `:` 运算符，所以
        下标表达式里出现 `:` 只能是切片。
        """
        depth = 0
        i = 0
        while True:
            t = self._peek(i)
            if t.type == "EOF":
                return False
            if t.type == "OP":
                if t.value in ("[", "(", "{"):
                    depth += 1
                elif t.value in ("]", ")", "}"):
                    if t.value == "]" and depth == 0:
                        return False  # 到 ] 了还没见到冒号 → 普通下标
                    depth -= 1
                elif t.value == ":" and depth == 0:
                    return True
            i += 1

    def _parse_slice(self, node: A.Node, open_tok) -> A.Node:
        """解析 `[起:止:步长]`，各分量可省略，产出一个 A.Slice 节点。"""
        start: Optional[A.Node] = None
        stop: Optional[A.Node] = None
        step: Optional[A.Node] = None

        # 第一个分量（可能为空，即 [:...]）
        if not self._at_op(":"):
            start = self._parse_expr()

        if self._at_op(":"):
            self._next()  # 消费第一个 :
            # 第二个分量（可能为空，即 [a:...] 或 [a::step]）
            if not self._at_op(":") and not self._at_op("]"):
                stop = self._parse_expr()
            if self._at_op(":"):
                self._next()  # 消费第二个 :
                if not self._at_op("]"):
                    step = self._parse_expr()

        self._expect_op("]")
        return A.Subscript(obj=node, index=A.Slice(
            start=start, stop=stop, step=step,
            line=open_tok.line, col=open_tok.col),
            line=open_tok.line, col=open_tok.col)

    def _parse_set_literal(self, first: A.Node, open_tok) -> A.Node:
        """解析集合字面量 `{a, b, c}`（M24.1）。

        调用前已消费第一个元素（``first``），且已确认它后面不是「:」。

        **实现方式是解析期脱糖**成 `集合([元素…])`（与 M23 的 `超()` 同一手法）：
        这样树遍历 / Python VM / C VM 都走同一条既有指令路径，三执行器行为
        必然一致，也不需要新增字节码或改 C。集合推导式 `{x 遍历 x 在 it}`
        同样脱糖成 `集合([x 遍历 x 在 it])`。

        注意 `{}` 仍是空字典（与 Python 一致），空集合写 `集合()`。
        """
        line, col = open_tok.line, open_tok.col

        def wrap(inner: A.Node) -> A.Node:
            return A.Call(func=A.Name(id="集合", line=line, col=col),
                          args=[inner], keywords=[], line=line, col=col)

        if self._at_kw("遍历"):
            comp = self._parse_comp_tail(
                kind="list", elt=first, key=None, value=None,
                line=line, col=col)
            self._expect_op("}")
            return wrap(comp)

        elements = [first]
        while self._at_op(","):
            self._next()
            if self._at_op("}"):        # 尾随逗号
                break
            elements.append(self._parse_expr())
        self._expect_op("}")
        return wrap(A.List(elements=elements, line=line, col=col))

    def _parse_lambda(self) -> A.Node:
        """匿名函数（M24.4）：`函数(x)：x * 2`。

        函数体只允许**一个表达式**（自动成为返回值）——需要多行逻辑就写具名
        函数，这样语法保持简单，也避免和「缩进块」纠缠。参数部分与具名函数
        完全一致（默认值、类型标注都支持）。
        """
        start = self._next()                    # 消费「函数」
        params, defaults, annotations, kinds = self._parse_params()
        self._expect_op(":")
        if self._at("NEWLINE") or self._at("EOF") or self._at("DEDENT"):
            # 冒号后面直接换行：匿名函数要求写在同一行
            self._err(ParseUnexpectedError,
                      "匿名函数要写成一行：「函数(参数)：表达式」",
                      tok=start,
                      hint="需要多行代码块请用「函数 名字(参数)：」定义具名函数")
        body_expr = self._parse_expr()
        return A.Lambda(
            params=params,
            body=[A.Return(value=body_expr, line=body_expr.line,
                           col=body_expr.col)],
            defaults=defaults, annotations=annotations, param_kind=kinds,
            line=start.line, col=start.col)

    def _parse_atom(self) -> A.Node:
        t = self._peek()
        if t.type == "NUMBER":
            self._next()
            return A.Num(value=t.value, line=t.line, col=t.col)
        if t.type == "STRING":
            self._next()
            return A.Str(value=t.value, line=t.line, col=t.col)
        if t.type == "FSTRING":
            self._next()
            return self._parse_fstring(t)
        if t.type == "KEYWORD":
            if t.value == "真":
                self._next()
                return A.Num(value=True, line=t.line, col=t.col)
            if t.value == "假":
                self._next()
                return A.Num(value=False, line=t.line, col=t.col)
            if t.value == "空":
                self._next()
                return A.Name(id="空", line=t.line, col=t.col)
            if t.value == "新建":
                # 新建 类(...) 是语法糖：等价于 类(...)，实例化靠类可调用
                self._next()
                name_tok = self._expect_name("类名")
                return A.Name(id=name_tok.value, line=t.line, col=t.col)
            if t.value == "函数":
                # 匿名函数表达式（M24.4）：函数(x)：x * 2
                return self._parse_lambda()
            self._err(ParseUnexpectedError,
                      f"表达式里不能直接用关键字「{t.value}」", tok=t)
        if t.type == "NAME":
            self._next()
            # 容错解析（M7.5）：值关键字（True/False/None）、内建函数、lambda 等
            self._check_en_word(t, mode="atom")
            return A.Name(id=t.value, line=t.line, col=t.col)
        if t.type == "OP" and t.value == "[":
            self._next()
            # 空列表 []
            if self._at_op("]"):
                self._next()
                return A.List(elements=[], line=t.line, col=t.col)
            first = self._parse_expr()
            # 推导式：[elt 遍历 x 在 it 如果 cond]
            if self._at_kw("遍历"):
                comp = self._parse_comp_tail(
                    kind="list", elt=first, line=t.line, col=t.col)
                self._expect_op("]")
                return comp
            elements = [first]
            while self._at_op(","):
                self._next()
                if self._at_op("]"):  # 尾随逗号 [1, 2,]
                    break
                elements.append(self._parse_expr())
            self._expect_op("]")
            return A.List(elements=elements, line=t.line, col=t.col)
        if t.type == "OP" and t.value == "{":
            self._next()
            # 空字典 {}
            if self._at_op("}"):
                self._next()
                return A.Dict(keys=[], values=[], line=t.line, col=t.col)
            k = self._parse_expr()
            if not self._at_op(":"):
                # 集合字面量（M24.1）：第一个元素后面不是「:」就是集合，
                # 例如 {1, 2, 3}。`{}` 仍按空字典处理（与 Python 一致）。
                return self._parse_set_literal(k, t)
            self._expect_op(":")
            v = self._parse_expr()
            # 字典推导式：{k: v 遍历 x 在 it 如果 cond}
            if self._at_kw("遍历"):
                comp = self._parse_comp_tail(
                    kind="dict", elt=None, key=k, value=v,
                    line=t.line, col=t.col)
                self._expect_op("}")
                return comp
            keys = [k]
            values = [v]
            while self._at_op(","):
                self._next()
                if self._at_op("}"):  # 尾随逗号
                    break
                kk = self._parse_expr()
                self._expect_op(":")
                vv = self._parse_expr()
                keys.append(kk)
                values.append(vv)
            self._expect_op("}")
            return A.Dict(keys=keys, values=values, line=t.line, col=t.col)
        if t.type == "OP" and t.value == "(":
            self._next()
            inner = self._parse_expr()
            self._expect_op(")")
            return inner
        self._err(ParseUnexpectedError,
                  f"这里需要一个值，但看到了{self._describe(t)}", tok=t)


def parse(tokens: list[Token], source_lines: list[str],
          filename: str = "<输入>") -> A.Program:
    program = Parser(tokens, source_lines, filename).parse()
    # 类方法里的 `超()` 已在 _parse_classdef 里脱糖；剩下还叫 `超` 的
    # 就是写在类外用错了，给一句能照着改的提示（M23.4）。
    _check_no_stray_super(program, filename)
    return program
