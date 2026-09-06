# -*- coding: utf-8 -*-
"""基石语言的语法分析器：递归下降 + 优先级爬升，产出 AST。

M0 支持语法：
    语句：赋值、表达式语句、如果/否则如果/否则、遍历、循环N次、当、
          中断、继续、函数定义、返回、导入、占位（空）
    表达式：算术、比较、逻辑、一元、调用、属性、下标、列表/字典字面量
"""

from __future__ import annotations

from typing import Optional

from . import ast_nodes as A
from .errors import (
    ParseBlockError,
    ParseMissingColonError,
    ParseMissingNameError,
    ParseUnexpectedError,
)
from .tokenizer import Token, tokenize
# 二元运算符优先级（数值越大越紧）
_BIN_PREC = {
    "或": 1,
    "与": 2,
    "==": 3, "!=": 3, "<": 3, ">": 3, "<=": 3, ">=": 3,
    "+": 4, "-": 4,
    "*": 5, "/": 5, "//": 5, "%": 5,
    "**": 6,
}

# 未来关键字：解析到即报"将在后续版本支持"
_FUTURE_KEYWORDS = {
}

# M7.5 容错解析：LLM 写中文代码时 fallback 回英文的高频错误 → 中文建议。
# 英文语句关键字 → 中文对应
_EN_STMT_HINTS = {
    "def": "函数", "if": "如果", "else": "否则", "elif": "否则如果",
    "for": "遍历", "while": "当", "return": "返回", "import": "导入",
    "from": "从", "class": "类", "try": "尝试", "except": "捕获",
    "finally": "最终", "raise": "抛出", "break": "中断", "continue": "继续",
}
# 英文值/运算符关键字 → 中文对应
_EN_VALUE_HINTS = {
    "True": "真", "False": "假", "None": "空",
    "and": "与", "or": "或", "not": "非", "in": "在", "is": "==",
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

# 赋值运算符
_ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "//=", "%=", "**="}


class Parser:
    def __init__(self, tokens: list[Token], source_lines: list[str],
                 filename: str = "<输入>"):
        self.tokens = tokens
        self.source_lines = source_lines
        self.filename = filename
        self.pos = 0

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
        self._err(ParseUnexpectedError,
                  f"这里需要「{op}」，但看到了「{self._peek().value}」")

    def _expect_kw(self, kw: str):
        if self._at_kw(kw):
            return self._next()
        self._err(ParseUnexpectedError,
                  f"这里需要关键字「{kw}」，但看到了「{self._peek().value}」")

    def _expect_name(self, what: str = "名字") -> Token:
        if self._peek().type == "NAME":
            return self._next()
        self._err(ParseMissingNameError, f"这里需要一个{what}")

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
            target_tok = self._expect_name("变量名")
            target: A.Node = A.Name(id=target_tok.value, line=target_tok.line,
                                    col=target_tok.col)
            # 多赋值：令 a, b = ...
            if self._at_op(","):
                names = [target]
                while self._at_op(","):
                    self._next()
                    tok = self._expect_name("变量名")
                    names.append(A.Name(id=tok.value, line=tok.line,
                                        col=tok.col))
                target = A.TargetList(names=names, line=start.line,
                                      col=start.col)
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
        """探测「名字 , 名字 … = 表达式」；不匹配则返回 None 不消耗 token。"""
        i = 0
        t = self._peek(i)
        if t.type != "NAME":
            return None
        names = []
        while True:
            t = self._peek(i)
            nxt = self._peek(i + 1)
            if t.type == "NAME" and nxt.type == "OP" and nxt.value == ",":
                names.append(t)
                i += 2
                continue
            if (t.type == "NAME" and nxt.type == "OP" and nxt.value == "="
                    and len(names) >= 1):
                names.append(t)   # 最后一个名字
                break
            return None
        # 确认是解包赋值：真正消耗这些 token
        start = self._next()  # 第一个名字
        nodes = [A.Name(id=start.value, line=start.line, col=start.col)]
        for tok in names[1:]:
            self._next()      # 逗号
            tok2 = self._next()  # 名字
            nodes.append(A.Name(id=tok2.value, line=tok2.line, col=tok2.col))
        self._next()          # =
        value = self._parse_value_tuple()
        return A.Assign(target=A.TargetList(names=nodes, line=start.line,
                                            col=start.col),
                        op="=", value=value,
                        line=start.line, col=start.col)

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
    def _parse_funcdef(self) -> A.Node:
        start = self._next()
        name_tok = self._expect_name("函数名")
        self._expect_op("(")
        params: list[str] = []
        defaults: list = []
        if not self._at_op(")"):
            while True:
                p = self._expect_name("参数名")
                params.append(p.value)
                # 默认参数：名字 = 表达式（M7）
                if self._at_op("="):
                    self._next()
                    defaults.append(self._parse_expr())
                else:
                    defaults.append(None)
                if self._at_op(","):
                    self._next()
                    if self._at_op(")"):  # 尾随逗号
                        break
                    continue
                break
        # 有默认值的参数必须排在最后（与 Python 一致）
        seen_default = False
        for d in defaults:
            if d is not None:
                seen_default = True
            elif seen_default:
                self._err(ParseUnexpectedError,
                          f"参数「{params[defaults.index(None)]}」没有默认值，"
                          f"但它排在有默认值的参数后面——"
                          f"有默认值的参数要放在最后", tok=name_tok)
        self._expect_op(")")
        self._expect_colon("函数")
        body = self._parse_block()
        return A.FuncDef(name=name_tok.value, params=params, body=body,
                         defaults=defaults,
                         line=start.line, col=start.col)

    # 类
    def _parse_classdef(self) -> A.Node:
        start = self._next()
        name_tok = self._expect_name("类名")
        base = None
        if self._at_kw("继承"):
            self._next()
            base = self._expect_name("基类名").value
        self._expect_colon("类")
        body = self._parse_block()
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

    # 尝试 / 捕获 / 最终
    def _parse_try(self) -> A.Node:
        start = self._next()
        self._expect_colon("尝试")
        body = self._parse_block()

        handlers: list[A.ExceptHandler] = []
        while self._at_kw("捕获"):
            h = self._parse_except_handler()
            handlers.append(h)

        finalbody = None
        if self._at_kw("最终"):
            self._next()
            self._expect_colon("最终")
            finalbody = self._parse_block()

        if not handlers and finalbody is None:
            self._err(ParseBlockError,
                      "「尝试」后面至少要有「捕获」或「最终」",
                      hint="写法是：\n尝试：\n    …\n捕获 值错误 为 e：\n    …",
                      tok=self._peek())

        return A.Try(body=body, handlers=handlers, finalbody=finalbody,
                     line=start.line, col=start.col)

    def _parse_except_handler(self) -> A.ExceptHandler:
        start = self._next()  # 捕获
        typ = None
        name = None
        # 可选类型：捕获 值错误 为 e / 捕获 为 e / 捕获:
        if not self._at_op(":") and not self._at_kw("为"):
            typ = self._parse_expr()
        if self._at_kw("为"):
            self._next()
            name = self._expect_name("捕获到的异常变量名").value
        self._expect_colon("捕获")
        body = self._parse_block()
        return A.ExceptHandler(type=typ, name=name, body=body,
                               line=start.line, col=start.col)

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
    # -- 表达式：优先级爬升 --------------------------------------------------

    def _parse_expr(self, min_prec: int = 0) -> A.Node:
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
            if t.type == "OP" and t.value in _BIN_PREC:
                op = t.value
                prec = _BIN_PREC[op]
                if prec < min_prec:
                    break
                self._next()
                if op == "**":
                    # 右结合
                    right = self._parse_expr(prec)
                else:
                    right = self._parse_expr(prec + 1)
                if op in _COMPARE_OPS:
                    # 链式比较：1 < x < 5，收集连续的比较运算，中间表达式只求值一次
                    ops = [op]
                    comparators = [right]
                    while True:
                        nxt = self._peek()
                        if (nxt.type != "OP" or nxt.value not in _COMPARE_OPS):
                            break
                        prec2 = _BIN_PREC[nxt.value]
                        if prec2 < min_prec:
                            break
                        self._next()
                        comparators.append(self._parse_expr(prec2 + 1))
                        ops.append(nxt.value)
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
            self._next()
            operand = self._parse_unary()
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
                attr_tok = self._expect_name("属性名")
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
    return Parser(tokens, source_lines, filename).parse()
