# -*- coding: utf-8 -*-
"""基石语言的错误体系：错误码、中文消息模板、源码片段渲染。

错误码分段：
    E01xx  词法错误
    E02xx  语法错误
    E03xx  语义错误
    E2xxx  运行期错误
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 调用帧（M17.2 调用栈回溯）
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    """调用栈里的一帧：当前正在执行的函数，以及「它在第几行被调用」。"""

    func: str                 # 函数名
    line: Optional[int] = None   # 调用点行号（1 起）
    col: Optional[int] = None    # 调用点列号（1 起）


def _safe_text(s: str) -> str:
    """把文本里无法编码的码位（如孤立代理字符）换成 U+FFFD。

    源码含非法字节时（例如按错误编码读进来的内容），错误信息本身不该再
    抛 ``UnicodeEncodeError``——那会让用户看到英文堆栈，与我们「中文报错」
    的承诺相悖。宁可把那个字显示成 ``?``（U+FFFD），也要把中文解释送出来。
    """
    if not isinstance(s, str):
        return str(s)
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "replace").decode("utf-8")


# ---------------------------------------------------------------------------
# 基础错误类
# ---------------------------------------------------------------------------

#: 少数错误码是**运行期临时挂上**的（没有独立异常类）。在这里登记，
#: 好让语言规格的错误码表（`ai.build_lang_spec()["error_codes"]`）一并报出来，
#: 不漏；使用时直接引用常量，改码只改这一处，不会两边漂移。
#: 「起因」链最多摊几层（M148）。再深的只印一句，避免「捕获里包捕获」刷屏。
#: ⚠️ 三个宿主必须用**同一个数**，否则同一个错误的渲染长度就不一样了。
MAX_CAUSE_DEPTH = 5

CODE_INPUT_END = "E2010"
CODE_INTERRUPTED = "E2100"

EXTRA_CODES = {
    CODE_INPUT_END: "输入结束",
    CODE_INTERRUPTED: "程序被中断",
}


class JishiError(Exception):
    """所有基石错误的基类。携带错误码、中文标题、行列位置与修正建议。"""
    code = "E0000"
    title = "未知错误"
    def __init__(
        self,
        message: str = "",
        *,
        line: Optional[int] = None,
        col: Optional[int] = None,
        source_line: Optional[str] = None,
        filename: str = "<输入>",
        hint: Optional[str] = None,
        underline: Optional[tuple[int, int]] = None,  # (起, 止) 列区间（1 起）
        fix: Optional[dict] = None,
        trace: Optional[list["Frame"]] = None,
        cause: Optional["JishiError"] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col
        self.source_line = source_line
        self.filename = filename
        self.hint = hint
        self.underline = underline
        #: 机器可读的修复建议（M8.3）：{"old": str, "new": str}，
        #: 配合 line/col 定位，agent 可直接做文本替换实现自愈。
        self.fix = fix
        #: 调用栈回溯（M17.2）：从外到内的调用帧列表；None 表示无回溯。
        self.trace = trace
        #: 「起因」链（M148）：**正在处理某个异常时又抛出的新异常**，
        #: 会把被处理的那个挂在这里。真实痛点：`文件读不了 → 解析失败 →
        #: 业务报错` 三层各自换过一次说法，只留最外层就等于把根子丢了。
        #: 见 `render`（链会一起印出来）与 `原因` / `根因`（给程序读）。
        self.cause = cause

    def render(self, *, color: bool = False) -> str:
        """把错误渲染成带源码行与 ``^`` 指示的多行文本，**带上整条「起因」链**（M148）。

        每一层都用自己的 `render_one()` 渲染（各带自己的源码行、调用链、提示），
        层与层之间只隔一行 ``起因：``：既好读，也好 grep（`grep -A3 起因：`）。

        ⚠️ 三个宿主（Python / Rust / Node）必须**逐字一致** ——
        段落顺序是「本层 → 调用链 → 提示 → 起因：→ 下一层」。
        """
        parts = [self.render_one(trace=True)]
        cause = self.cause
        seen = {id(self)}
        depth = 0
        while cause is not None:
            if id(cause) in seen:
                # 自环（`抛出 e` 把 e 自己挂成原因那种）理论上在挂的时候就挡掉了，
                # 但异常对象是可以被程序直接改的 —— 渲染这一侧也留一道，别死循环。
                parts.append("起因：（这条链绕回来了，就此打住）")
                break
            seen.add(id(cause))
            if depth >= MAX_CAUSE_DEPTH:
                parts.append(f"起因：（还更深，只摊到这里）")
                break
            depth += 1
            parts.append("起因：")
            # 只有**最外层**印调用链：起因层印位置就够了，四层链各带一遍
            # 「从外到内」会淹没真正的信息（三个宿主都这么办）。
            parts.append(cause.render_one(trace=False))
            cause = cause.cause
        # 统一兜底：标题/消息/提示/回溯里任何一段混进非法码位，都不能让
        # 渲染本身抛异常（见 _safe_text 说明）。
        return _safe_text("\n".join(parts))

    def render_one(self, *, trace: bool = True) -> str:
        """只渲染**这一层**（不带起因链）；`render` 拿它拼整条链。

        `trace=False` 时不印调用链 —— 起因层只用位置说话。
        """
        head = f"错误 {self.code}：{self.title}"
        if self.message:
            head += f"（{self.message}）"
        if self.line is None:
            body = head
        else:
            loc = f"  ┌─ {self.filename}:{self.line}:{self.col or 1}"
            lines = [head, loc, "  │"]
            if self.source_line:
                src = _safe_text(
                    self.source_line.rstrip("\n").rstrip("\r"))
                lines.append(f"{self.line:>3} │ {src}")
                # 构造 ^ 指示行
                start, end = self.underline or (self.col, self.col)
                if start is None:
                    start = self.col
                if end is None:
                    end = start
                caret_col = max(1, start)
                width = max(1, end - start + 1)
                pad = " " * (caret_col - 1)
                marker = "~" * max(0, width - 1) + "^"
                lines.append(f"    │ {pad}{marker}")
            lines.append("  │")
            body = "\n".join(lines)
        # 调用栈回溯（M17.2）
        trace_block = self._render_trace() if trace else ""
        parts = [body]
        if trace_block:
            parts.append(trace_block)
        if self.hint:
            parts.append(f"  提示：{self.hint}")
        # 统一兜底：标题/消息/提示/回溯里任何一段混进非法码位，都不能让
        # 渲染本身抛异常（见 _safe_text 说明）。
        return _safe_text("\n".join(parts))

    def _render_trace(self) -> str:
        """把调用链渲染成「从外到内」的文本块；无回溯返回空串。"""
        if not self.trace:
            return ""
        # 取最近若干帧（从外到内，限制长度防刷屏）
        frames = self.trace[-20:] if len(self.trace) > 20 else self.trace
        lines = ["调用链（从外到内）："]
        for fr in frames:
            where = f"第 {fr.line} 行" if fr.line is not None else "未知位置"
            lines.append(f"  {fr.func} ← {where}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.render()

    # -- 作为「异常对象」被基石代码读取的属性 ---------------------------------
    # `捕获 值错误 为 e` 之后可以读 `e.消息` / `e.类型`；
    # 用属性而非方法，让 runtime.get_attr 的 getattr 回落直接命中。

    #: 异常类型名（`捕获 值错误` 的匹配依据，也是 `e.类型` 的值）。
    #: 与渲染标题 title 分离：title 是「数值不对」这类对用户更友好的
    #: 描述，exc_name 是「值错误」这类规范的分类名。默认回落到 title。
    exc_name: Optional[str] = None

    @property
    def 消息(self) -> str:
        """异常的中文说明（`e.消息`）。"""
        return self.message

    @property
    def 类型(self) -> str:
        """异常的类型名，如「值错误」（`e.类型`）。"""
        return self.exc_name or self.title

    @property
    def 原因(self):
        """上一层异常（`e.原因`）；没有就是 `空`（M148）。

        什么时候会有：**正在处理某个异常的时候又抛出一个新的**。典型是
        「读文件失败 → 换成自己的话再抛」这种包装：

        ```python
        尝试:
            内容 = 文件.读文本(路径)
        捕获 文件错误 为 e:
            抛出 值错误("配置读不了")     # 这条的 `原因` 就是刚才那个文件错误
        ```

        串起来看，才看得出「业务层那句话」底下到底是什么。
        """
        return self.cause

    @property
    def 根因(self):
        """链**最底下**那一层（`e.根因`）；没有原因时返回自己（M148）。

        写日志时常用：`日志.错误("启动失败", e.消息, "根因：", e.根因.消息)`。
        """
        cur = self
        seen = {id(self)}
        while cur.cause is not None and id(cur.cause) not in seen:
            cur = cur.cause
            seen.add(id(cur))
        return cur

    def with_source(self, source_lines: list[str]) -> "JishiError":
        """补充源码行上下文后返回自身（就地补全，方便链式使用）。

        ⚠️ **要顺着「起因」链一路补**（M148）：只补最外层的话，链上每一层
        都只剩「错误 E2007：…”」这一行、没有源码行和 `^` —— 而链的价值
        恰恰是「看到根子发生在**哪一行**」。
        """
        cur, seen = self, set()
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if cur.line is not None and 1 <= cur.line <= len(source_lines):
                cur.source_line = source_lines[cur.line - 1]
            cur = cur.cause
        return self


# ---------------------------------------------------------------------------
# 词法错误 E01xx
# ---------------------------------------------------------------------------

class LexError(JishiError):
    code = "E0100"
    title = "词法错误"


class LexIndentError(LexError):
    code = "E0101"
    title = "缩进错误"


class LexTabError(LexError):
    code = "E0102"
    title = "缩进混用了 Tab 和空格"


class LexFullWidthSpaceError(LexError):
    code = "E0103"
    title = "缩进中出现了全角空格"


class LexStringError(LexError):
    code = "E0104"
    title = "字符串没有正常结束"


class LexCharError(LexError):
    code = "E0105"
    title = "无法识别的字符"


class LexKeywordGlueError(LexError):
    code = "E0106"
    title = "关键字与后面的内容粘连了"


# ---------------------------------------------------------------------------
# 语法错误 E02xx
# ---------------------------------------------------------------------------

class ParseError(JishiError):
    code = "E0200"
    title = "语法错误"


class ParseUnexpectedError(ParseError):
    code = "E0201"
    title = "这里出现了意外的内容"


class ParseBlockError(ParseError):
    code = "E0202"
    title = "这个语句需要一个代码块"


class ParseMissingColonError(ParseError):
    code = "E0203"
    title = "这里少了冒号「：」"


class ParseMissingNameError(ParseError):
    code = "E0204"
    title = "这里需要一个名字"


class ParseUnclosedError(ParseError):
    code = "E0205"
    title = "括号没有闭合"


# ---------------------------------------------------------------------------
# 语义错误 E03xx
# ---------------------------------------------------------------------------

class SemanticError(JishiError):
    code = "E0300"
    title = "语义错误"


class SemanticNameError(SemanticError):
    code = "E0301"
    title = "找不到这个名字"


class SemanticFuncError(SemanticError):
    code = "E0302"
    title = "找不到这个函数"


class SemanticTypeError(SemanticError):
    code = "E0303"
    title = "类型不匹配"


class SemanticRedefineError(SemanticError):
    code = "E0304"
    title = "这个名字不能重复定义"


class SemanticNotCallableError(SemanticError):
    code = "E0305"
    title = "这个东西不能调用"


# ---------------------------------------------------------------------------
# 运行期错误 E2xxx
# ---------------------------------------------------------------------------

class RunError(JishiError):
    code = "E2000"
    title = "运行期错误"


#: 「遍历时改了容器的大小」——Python 侧的原生异常是 `RuntimeError`（英文、无码、无行列），
#: 各引擎都要把它换成**同一条**基石错误（D77 §二）：带码、带行列、带修法。
#: ⚠️ 文案在这里**只有一份**，五个执行器逐字引用（对拍盯得住）。
ITER_SIZE_CHANGED = "遍历的过程中改了容器的大小（这次遍历没法继续了）"
ITER_SIZE_CHANGED_HINT = "要边遍历边改，就先把元素取出来（或复制一份）再遍历"


def iter_size_changed(line=None, col=None, filename: str = "<输入>",
                      source_line=None) -> "RunError":
    """把「遍历中改容器大小」造成的那条 RuntimeError 转成基石错误。"""
    return RunError(ITER_SIZE_CHANGED, line=line, col=col,
                    source_line=source_line, filename=filename,
                    hint=ITER_SIZE_CHANGED_HINT)


class RunZeroDivisionError(RunError):
    code = "E2001"
    exc_name = "除零错误"
    title = "不能除以零"


class RunTypeError(RunError):
    code = "E2002"
    exc_name = "类型错误"
    title = "类型错误"


class RunIndexError(RunError):
    code = "E2003"
    exc_name = "索引错误"
    title = "索引越界"


class RunKeyError(RunError):
    code = "E2004"
    exc_name = "键错误"
    title = "找不到这个键"


class RunNotCallableError(RunError):
    code = "E2006"
    title = "这个东西不能调用"


class RunValueError(RunError):
    code = "E2005"
    exc_name = "值错误"
    title = "数值不对"


class RunFileError(RunError):
    code = "E2007"
    exc_name = "文件错误"
    title = "文件错误"


class RunAssertionError(RunError):
    """`断言` 没通过（M23.1）。可被 `捕获 断言错误 为 e` 接住。"""

    code = "E2008"
    exc_name = "断言错误"
    title = "断言没通过"


class RunStopIteration(RunError):
    """内部信号：循环耗尽，不应直接展示给用户。"""

    code = "E2998"
    title = "（内部）迭代结束"


class RunCustomError(RunError):
    """`抛出 值` 抛出的非异常值会被包装成这一类，类型名就是「异常」。"""

    # M44 修：这里原本也是 E2008，与 `RunAssertionError`（断言没通过）**重码**。
    # 是给 M44 做机器可读错误码表时才发现的——同一份表里出现两个 E2008，
    # 谁也没法用它定位问题。`E2008` 归「断言错误」（那是既有文档与测试的用法），
    # 这条改用空着的 E2009。
    code = "E2009"
    title = "异常"


class RunBreak(RunError):
    """内部信号：中断循环。"""

    code = "E2999"
    title = "（内部）中断"


class RunContinue(RunError):
    """内部信号：继续下一轮循环。"""

    code = "E2997"
    title = "（内部）继续"


class DebugQuit(BaseException):
    """调试器要求「立刻结束会话」（M37）。

    刻意**不继承 `JishiError`**：用户在调试时按「退出」是想马上停，而不是
    让程序里的 `尝试/捕获` 接住它——若它是 JishiError，一句 `捕获：` 就能把
    「退出」吞掉，用户会觉得调试器失灵（一个很隐蔽的坑）。

    但 `_do_try` 里那段是 `except BaseException`（为的是把 Python 异常翻译成
    中文），所以**那边要显式放行**——与 `RunBreak`/`RunContinue` 同一处理方式。
    另外 `UserFunction.__call__`、`_eval_call` 都只捕获 JishiError，会自然放过它。
    """


# ---------------------------------------------------------------------------
# 异常类型注册表（M5a）
# ---------------------------------------------------------------------------

#: 基石异常类型名 → 异常类。既是内建构造器的来源（`值错误("…")`），
#: 也是 `捕获 值错误` 的匹配依据（用 isinstance，因此基类能抓子类）。
EXCEPTION_TYPES: dict[str, type] = {
    "异常": JishiError,
    "运行期错误": RunError,
    "类型错误": RunTypeError,
    "值错误": RunValueError,
    "索引错误": RunIndexError,
    "键错误": RunKeyError,
    "除零错误": RunZeroDivisionError,
    "文件错误": RunFileError,
    "断言错误": RunAssertionError,
}


# ---------------------------------------------------------------------------
# Python 异常 → 基石中文错误 翻译
# ---------------------------------------------------------------------------

#: Python 类型名 → 基石类型名（翻译错误消息用）
_PY_TYPE_ZH = {
    "int": "整数", "float": "小数", "str": "文本", "bool": "布尔",
    "list": "列表", "dict": "字典", "tuple": "元组", "set": "集合",
    "JishiSet": "集合", "JishiDecimal": "精确小数", "JishiInstance": "对象",
    "JishiFile": "文件", "NoneType": "空",
}


def _type_error_zh(msg: str) -> str:
    """把常见的 Python 二元运算类型错误翻成中文（M30）。

    这些消息原来**漏成英文**（`can only concatenate list (not "int") to list`），
    对中文教学语言来说读起来费劲。认不出的模式返回空串、保留原文——
    宁可不翻，也不要翻错。
    """
    m = re.search(r"can only concatenate (\w+) \(not \"(\w+)\"\) to \w+", msg)
    if m:
        a = _PY_TYPE_ZH.get(m.group(1), m.group(1))
        b = _PY_TYPE_ZH.get(m.group(2), m.group(2))
        return f"「{a}」只能和「{a}」相加，不能和「{b}」相加"
    # ⚠️ `**` 的原文是**另一个格式**（R6.4 补）：
    # `unsupported operand type(s) for ** or pow(): 'int' and 'str'` ——
    # 运算符后面跟着 ` or pow()`，所以下面那条 `(\S+):` 匹配不到，
    # 于是 `1 ** "a"` 一直漏英文。**先试这一条，再试通用那条。**
    m = re.search(
        r"unsupported operand type\(s\) for (\S+) or \w+\(\): '(\w+)' and '(\w+)'", msg)
    if m:
        a = _PY_TYPE_ZH.get(m.group(2), m.group(2))
        b = _PY_TYPE_ZH.get(m.group(3), m.group(3))
        return f"「{a}」和「{b}」不能做「{m.group(1)}」运算"
    m = re.search(
        r"unsupported operand type\(s\) for (\S+): '(\w+)' and '(\w+)'", msg)
    if m:
        a = _PY_TYPE_ZH.get(m.group(2), m.group(2))
        b = _PY_TYPE_ZH.get(m.group(3), m.group(3))
        return f"「{a}」和「{b}」不能做「{m.group(1)}」运算"
    # 标准库函数的**参数个数**错（R6.4 补）。原文两种：
    #   `属性文本() takes 1 positional argument but 2 were given`（多给）
    #   `写表格() missing 1 required positional argument: '行列表'`（少给）
    # ⚠️ 标准库函数是**真 Python 函数**，靠 Python 自己抛 TypeError —— 于是
    # 这两句**一直漏成英文**（内建那条路 M55 已经改成 `check_arity` 了，
    # 标准库这条路没人管）。文案与 Rust 的 `need` / `need_range` 对齐。
    m = re.search(
        r"(\w+)\(\) takes (\d+) positional arguments? but (\d+) (?:were|was) given",
        msg)
    if m:
        return f"函数「{m.group(1)}」需要 {m.group(2)} 个参数，但传了 {m.group(3)} 个"
    m = re.search(r"(\w+)\(\) missing (\d+) required positional arguments?", msg)
    if m:
        # ⚠️ 「少给」时 Python 只说**缺几个**、不说「一共传了几个」——
        # 所以这句只能给到「缺少 N 个必填参数」（宿主能给出更全的说法）。
        return f"函数「{m.group(1)}」缺少 {m.group(2)} 个必填参数"
    m = re.search(r"can't multiply sequence by non-int of type '(\w+)'", msg)
    if m:
        a = _PY_TYPE_ZH.get(m.group(1), m.group(1))
        return f"序列只能乘整数，不能乘「{a}」"
    m = re.search(r"'(\w+)' object does not support item assignment", msg)
    if m:
        a = _PY_TYPE_ZH.get(m.group(1), m.group(1))
        return f"「{a}」不能按下标赋值"
    # R6.2：`整数[0]` 这类原来整个漏成英文（`'int' object is not subscriptable`）
    m = re.search(r"'(\w+)' object is not subscriptable", msg)
    if m:
        a = _PY_TYPE_ZH.get(m.group(1), m.group(1))
        return f"「{a}」不能取下标"
    return ""


#: Python 的越界消息 → 中文。「认不出就保留原文」——与 `_type_error_zh` 同一条规矩。
_INDEX_ZH = {
    "list index out of range": "列表下标越界",
    "list assignment index out of range": "列表下标越界",
    "string index out of range": "文本下标越界",
    "tuple index out of range": "元组下标越界",
}


def _index_error_zh(msg: str) -> str:
    """`IndexError` 的消息 → 中文（认不出就原样返回）。"""
    return _INDEX_ZH.get(msg.strip(), msg)


def _key_error_zh(exc: KeyError) -> str:
    """`KeyError` → `字典里没有键「乙」`。

    ⚠️ 不能直接用 `str(exc)`：那是**键的 repr**（文本键会带一层引号：
    `'乙'`），而 repr 的转义规则又是「另一门语言要整套复刻」的那种东西
    （与 R3 那个「插值字符串把分段表泄出来」同一个家族）。
    """
    if not exc.args:
        return "字典里没有这个键"
    k = exc.args[0]
    if isinstance(k, str):
        return f"字典里没有键「{k}」"
    if k is True:
        return "字典里没有键「真」"
    if k is False:
        return "字典里没有键「假」"
    if k is None:
        return "字典里没有键「空」"
    if isinstance(k, (int, float)):
        return f"字典里没有键「{k}」"
    return "字典里没有这个键"


def translate_python_exception(
    exc: BaseException,
    *,
    line: Optional[int] = None,
    col: Optional[int] = None,
    filename: str = "<输入>",
) -> JishiError:
    """把 Python 侧抛出的异常翻译成基石的中文错误。"""

    def wrap(cls, msg: str) -> JishiError:
        return cls(msg, line=line, col=col, filename=filename)

    if isinstance(exc, ZeroDivisionError):
        return wrap(RunZeroDivisionError, "")
    if isinstance(exc, FileNotFoundError):
        # 只留路径，不带 Python 的英文 strerror（中英混排读起来更费劲，
        # 而路径本身才是排错要的信息）
        target = f"：{exc.filename}" if exc.filename else ""
        return wrap(RunFileError, f"找不到文件或目录{target}")
    if isinstance(exc, PermissionError):
        target = f"：{exc.filename}" if exc.filename else ""
        return wrap(RunFileError, f"没有权限，访问被拒绝{target}")
    if isinstance(exc, OSError):
        return wrap(RunFileError, f"文件或系统操作出错（{exc}）")
    if isinstance(exc, RecursionError):
        return RunError(
            "递归层数太深了",
            line=line, col=col, filename=filename,
            hint="函数反复调用自己没有停下来——是不是忘了写结束条件（比如 如果 n <= 0：返回 …）？")
    if isinstance(exc, OverflowError):
        return wrap(RunValueError, f"数字太大，超出可表示范围（{exc}）")
    if isinstance(exc, (TypeError, AttributeError)):
        detail = str(exc) if str(exc) else ""
        # 二元运算的类型不匹配：Python 的原文是英文，这里翻成人话（M30）
        return wrap(RunTypeError, _type_error_zh(detail) or detail)
    if isinstance(exc, IndexError):
        # ⚠️ **R6.2 起翻成中文**：以前直接把 Python 的英文往上传，
        # 于是 `e.消息` 是 `list index out of range` —— 一门中文教学语言里漏
        # 英文，正是 `_type_error_zh` 上面那段注释说的「中英混排读起来更费劲」。
        # 认不出的模式仍然保留原文（**宁可不翻，也不要翻错**）。
        return wrap(RunIndexError, _index_error_zh(str(exc)))
    if isinstance(exc, KeyError):
        # KeyError 的 `str()` 是**键的 repr**（`'乙'`，带引号），所以不能沿用
        # 原文，要自己拼。与宿主侧 `字典里没有键「乙」` 逐字对齐。
        return wrap(RunKeyError, _key_error_zh(exc))
    if isinstance(exc, ValueError):
        msg = str(exc) or ""
        # 切片赋值长度不匹配：Python 的原文是英文，这里翻成人话（M24.2）
        m2 = re.search(r"size (\d+) to extended slice of size (\d+)", msg)
        if m2:
            # 带上具体个数，比 Python 原文的英文更好用（M30）
            return wrap(
                RunValueError,
                f"切片赋值长度不匹配：目标有 {m2.group(2)} 个位置，"
                f"却给了 {m2.group(1)} 个值")
        if "extended slice" in msg:
            return wrap(
                RunValueError,
                "切片赋值时两边的长度对不上（带步长的切片要求个数正好相等）",
                )
        return wrap(RunValueError, msg)
    if isinstance(exc, EOFError):
        e = JishiError(
            "输入已经结束，程序没有更多内容可读了",
            line=line, col=col, filename=filename)
        e.code = CODE_INPUT_END
        e.title = "输入结束"
        return e
    if isinstance(exc, KeyboardInterrupt):
        return JishiError("用户按下了 Ctrl+C 中断程序", code=CODE_INTERRUPTED,
                          title="程序被中断", line=line, col=col, filename=filename)
    if isinstance(exc, JishiError):
        return exc
    return wrap(RunError, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 修正建议（供 suggest.py 使用）
# ---------------------------------------------------------------------------

def render_suggestion(candidates: list[str], limit: int = 3) -> str:
    """把候选词渲染成「你是不是想写…」提示文本。"""
    if not candidates:
        return ""
    top = candidates[:limit]
    return "你是不是想写「" + "」或「".join(top) + "」？"


# ---------------------------------------------------------------------------
# 全角字符工具
# ---------------------------------------------------------------------------

_FULLWIDTH_MAP = {
    "　": " ",  # U+3000 全角空格
    "：": ":",
    "；": ";",
    "，": ",",
    "（": "(",
    "）": ")",
    "［": "[",
    "］": "]",
    "｛": "{",
    "｝": "}",
    "＂": '"',
    "＇": "'",
    "！": "!",
    "？": "?",
    "＝": "=",
    "＋": "+",
    "－": "-",
    "＊": "*",
    "／": "/",
    "％": "%",
    "＜": "<",
    "＞": ">",
    "＆": "&",
    "｜": "|",
    "＾": "^",
    "～": "~",
    "＠": "@",
    "＃": "#",
    "＿": "_",
    "￥": "$",
    "《": "<",
    "》": ">",
    "【": "[",
    "】": "]",
    "「": '"',
    "」": '"',
    "『": "'",
    "』": "'",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "．": ".",
    "、": ",",
}


def normalize_fullwidth(ch: str) -> str:
    """把单个全角标点/空白字符归一化为半角（在字符串与注释之外使用）。"""
    if ch in _FULLWIDTH_MAP:
        return _FULLWIDTH_MAP[ch]
    # 其他全角字符（如全角数字字母）不做归一化，避免语义混淆
    return ch


def char_display_width(ch: str) -> int:
    """字符显示宽度：CJK 及全角字符按 2 格计，用于 ^ 指示对齐。"""
    if unicodedata.east_asian_width(ch) in ("F", "W"):
        return 2
    return 1


# ---------------------------------------------------------------------------
# 错误码 → 异常类（R 线用）
# ---------------------------------------------------------------------------

_ERROR_BY_CODE: "dict[str, type]" = {}


def error_class(code: str, default: "type | None" = None) -> type:
    """按错误码查异常类。

    **R 线（前端换 Rust）的接口约定**：扩展只回传「码 / 消息 / 行 / 列 / 提示 /
    下划线 / 修正」这些**事实**，类由 Python 侧按码现查 —— 于是 Rust 那边**没有**
    「码 → 类」的映射表，也就不会与这里漂移。

    惰性建表；同码取先注册的那个（正常情况错误码不重复，M44 修过一次重码）。
    """
    if not _ERROR_BY_CODE:
        for obj in list(globals().values()):
            if (isinstance(obj, type) and issubclass(obj, JishiError)
                    and obj is not JishiError):
                _ERROR_BY_CODE.setdefault(obj.code, obj)
    return _ERROR_BY_CODE.get(code, default or JishiError)


#: Rust 前端扩展回传的**事实字段**（前端契约的一部分）。
#:
#: ⚠️ 这张表是「有哪些事实」的**单一来源**：`from_extension` 就照它解析，
#: `tests/test_m61_frontend_errors.py` 还会拿它做一次「往返测试」——
#: 往扩展异常上摆满这些字段，过一遍还原，断言一个都没丢。
#:
#: 「为什么要这么做」（R4 踩出来的）：R2/R3 时词法与语法**各抄了一份字段清单**，
#: 而词法那份漏了「修正」。当时恰好**没有一个词法错误带 `fix`**，所以怎么测都是绿的
#: —— 哪天有人给词法错误加个修复建议（比如全角标点→半角），两个前端就会静默分岔。
#: 教训：**两份清单并存时，「恰好没用到」不等于「没问题」。**
EXTENSION_FIELDS: tuple[str, ...] = (
    "码", "消息", "行", "列", "提示", "下划线", "修正",
)


def from_extension(exc: BaseException, *, filename: str,
                   lines: "list[str]", default_cls: "type | None" = None
                   ) -> JishiError:
    """把扩展回传的「事实」还原成一个 `JishiError` —— **唯一的还原出口**。

    两条前端路径（`tokenizer._rust_tokenize` 与 `parser._make_parse_error`）
    **都必须走这里**：多一个出口就多一份「谁忘了补」的机会（R3 已经栽过两次）。

    * `码` → 异常类由 `error_class` 现查（Rust 侧没有那张表，也就漂不了）；
    * `default_cls` 是「码查不到时兜底用谁」：词法那条传 `LexError`、语法那条传
      `ParseError` —— **保留「词法错误必是词法类」这层家族保证**（码查得到时它不生效）；
    * 源码行不跟着传，由 `with_source(lines)` 按 `行` 现取 —— 少传一份少一份漂移；
    * `修正` 在扩展侧是 `(old, new)` 元组，到这里变成契约里的 `{"old","new"}`。
    """
    cls = error_class(getattr(exc, "码", ""), default_cls)
    fix = getattr(exc, "修正", None)
    err = cls(
        getattr(exc, "消息", ""),
        line=getattr(exc, "行", None),
        col=getattr(exc, "列", None),
        filename=filename,
        hint=getattr(exc, "提示", None),
        underline=getattr(exc, "下划线", None),
        fix=({"old": fix[0], "new": fix[1]} if fix else None),
    )
    return err.with_source(list(lines))


# ---------------------------------------------------------------------------
# 包管理（E40xx，M131）
# ---------------------------------------------------------------------------
# ⚠️ 这些**不是语言错误**，是工具链的错。但同样给码，理由和后面对 `--json-errors`
#    的支持一样：**用户遇到的每一堵墙都该有名字**，而不是只看到一句 ValueError。
#
# ⚠️ 它们**没有行列位置**（`render()` 在 `line is None` 时只打标题+消息）——
#    装包失败要说的是「你该动什么」，不是「第 3 行第 5 列」。

class PackageError(JishiError, ValueError):
    """包管理错误的基类（装包 / 发版 / 锁定文件）。

    ⚠️ 单独一个基类的意义：CLI 里可以 `except PackageError` 一把接住，
    而不是去数「我到底 raise 了哪几种 ValueError」—— 后者迟早漏一个，
    而漏掉的那个会以**裸 ValueError 冒到用户面前**（`安装失败：` 后面一句什么都没说）。

    ⚠️ **同时继承 `ValueError`**（M131）：`packages.py` 原来所有失败都是
    `raise ValueError(...)`，调用方到处 `except ValueError`。加码时若只继承
    `JishiError`，那些 `except` 会**全部接不住** —— 错误会冒到顶层变成 traceback。
    多继承一个，等于「加了码，但不破坏任何现有的接法」。
    """
    code = "E4000"
    title = "包管理错误"


class PackageNotFoundError(PackageError):
    code = "E4001"
    title = "索引里没有这个包"


class PackageMetaError(PackageError):
    code = "E4002"
    title = "包的自述文件不合格"


class PackageConflictError(PackageError):
    code = "E4003"
    title = "依赖冲突"


class PackageMissingDepError(PackageError):
    code = "E4004"
    title = "依赖在索引里找不到"


class PackageCycleError(PackageError):
    code = "E4005"
    title = "依赖成环"


class PackageChecksumError(PackageError):
    code = "E4006"
    title = "包内容和校验和对不上"


class PackageIndexError(PackageError):
    code = "E4007"
    title = "读不到索引源"


class PackageLockError(PackageError):
    code = "E4008"
    title = "锁定文件对不上"


class PackageBuildError(PackageError):
    code = "E4009"
    title = "打包失败"


class PackageVersionError(PackageError):
    """包要求的**基石版本**比现在高（M145，E4010）。

    为什么要单独的码：这条的修法与别的包错误**完全不同** ——
    不是「换个源」「重下一次」，而是「**升级语言本体**」。混进 E4002
    （自述文件不合格）会让人去改 包.json，方向就错了。
    """
    code = "E4010"
    title = "基石版本不够"
