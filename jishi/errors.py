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

    def render(self, *, color: bool = False) -> str:
        """把错误渲染成带源码行与 ^ 指示的多行文本。"""
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
        trace_block = self._render_trace()
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

    def with_source(self, source_lines: list[str]) -> "JishiError":
        """补充源码行上下文后返回自身（就地补全，方便链式使用）。"""
        if self.line is not None and 1 <= self.line <= len(source_lines):
            self.source_line = source_lines[self.line - 1]
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

    code = "E2008"
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
    m = re.search(
        r"unsupported operand type\(s\) for (\S+): '(\w+)' and '(\w+)'", msg)
    if m:
        a = _PY_TYPE_ZH.get(m.group(2), m.group(2))
        b = _PY_TYPE_ZH.get(m.group(3), m.group(3))
        return f"「{a}」和「{b}」不能做「{m.group(1)}」运算"
    m = re.search(r"can't multiply sequence by non-int of type '(\w+)'", msg)
    if m:
        a = _PY_TYPE_ZH.get(m.group(1), m.group(1))
        return f"序列只能乘整数，不能乘「{a}」"
    m = re.search(r"'(\w+)' object does not support item assignment", msg)
    if m:
        a = _PY_TYPE_ZH.get(m.group(1), m.group(1))
        return f"「{a}」不能按下标赋值"
    return ""


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
        return wrap(RunIndexError, str(exc) or "")
    if isinstance(exc, KeyError):
        return wrap(RunKeyError, str(exc) or "")
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
        e.code = "E2010"
        e.title = "输入结束"
        return e
    if isinstance(exc, KeyboardInterrupt):
        return JishiError("用户按下了 Ctrl+C 中断程序", code="E2100",
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
