# -*- coding: utf-8 -*-
"""基石运行时语义 —— **单一事实来源**。

三个执行器共用这里的实现，保证语义严格一致：

- ``interpreter.py``  树遍历解释器（M0，阶段一参考实现）
- ``vm.py``           Python 字节码虚拟机（M4a，调试基线）
- ``cvm_bind.py``     C 虚拟机的宿主回调桥接（M4b）

凡是「语义」都放这里：对象方法表、内建函数、属性/下标/调用/导入、
错误翻译。执行器只负责「怎么走」，不负责「走出来是什么」。
"""

from __future__ import annotations

import decimal
import importlib
from typing import Any, Optional

from .errors import (
    EXCEPTION_TYPES,
    JishiError,
    RunAssertionError,
    RunCustomError,
    RunError,
    RunFileError,
    RunIndexError,
    RunKeyError,
    RunNotCallableError,
    RunTypeError,
    RunValueError,
    RunZeroDivisionError,
    SemanticFuncError,
    SemanticNameError,
    translate_python_exception,
)

# 从 Python 代码抄过来时容易直接写英文常量，给出中文写法的提示
PY_NAME_HINT = {
    "True": "基石里布尔值写「真」，不是 Python 的 True",
    "False": "基石里布尔值写「假」，不是 Python 的 False",
    "None": "基石里空值写「空」，不是 Python 的 None",
    "null": "基石里空值写「空」，不是 null",
}


def unbound_local_error(name: str, *, line=None, col=None,
                        filename="<输入>", hint: Optional[str] = None):
    """「局部变量还没赋过值就被读」的报错（M40）——**三个执行器共用的单一来源**。

    这条消息不能各写各的：对拍测试比的是 `(输出, 报错全文)` 整对，文案差一个字
    就判成「执行器不一致」。所以树遍历（`interpreter._lookup`）、字节码 VM
    （`vm._unbound`）、C VM（`cvm_bind._cb_raise_unbound_local`）都调这里。

    `hint` 由调用方给「外层确实有同名变量」时的更具体说法（编译器把它按槽位存进
    `Code.local_hints`，树遍历侧则由 `_Unbound` 随身带着）；给不出就用通用说法。
    """
    return SemanticNameError(
        f"名字「{name}」在赋值之前被用到了", line=line, col=col,
        filename=filename,
        hint=hint or (f"「{name}」在这个函数里被赋过值，所以它是局部变量；"
                      "函数一进来它还没值。想读外层同名的那个，就换个名字。"))


#: 「外层也有同名变量」时的那句提示。**编译器与树遍历共用这一份文本**——
#: 两边各写一遍迟早会漂，而漂了就是「同一个错误在两个执行器里提示不同」。
UNBOUND_SHADOW_HINT = (
    "外层也有一个叫「{name}」的变量。在这个函数里给它赋过值，"
    "它就成了这个函数的局部变量，赋值之前不能读取")


# ---------------------------------------------------------------------------
# 基础对象
# ---------------------------------------------------------------------------

class Module:
    """标准库模块或 Python 模块在基石里的包装。"""

    __slots__ = ("name", "attrs")

    def __init__(self, name: str, attrs: dict[str, Any]):
        self.name = name
        self.attrs = attrs

    def __repr__(self):
        return f"<模块 {self.name}>"


class _Builtin:
    """基石内建函数（带中文名称与说明）。"""

    __slots__ = ("name", "fn", "doc")

    def __init__(self, name, fn, doc=""):
        self.name = name
        self.fn = fn
        self.doc = doc

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)

    def __repr__(self):
        return f"<内建 {self.name}>"


class ExcType:
    """基石异常类型（内建名字：`值错误`、`类型错误`…）。

    既可调用构造异常实例（`值错误("折扣不对")`），也可作为 `捕获` 的
    匹配条件（`捕获 值错误 为 e:`）。
    """

    __slots__ = ("name", "cls")

    def __init__(self, name: str, cls: type):
        self.name = name
        self.cls = cls

    def __call__(self, message: str = ""):
        return self.cls(message)

    def __repr__(self):
        return f"<异常类型 {self.name}>"


def type_name(v) -> str:
    """基石类型名（中文）。"""
    if v is None:
        return "空"
    if type(v) is bool:
        return "布尔"
    if isinstance(v, int):
        return "整数"
    if isinstance(v, float):
        return "小数"
    if isinstance(v, str):
        return "文本"
    if isinstance(v, list):
        return "列表"
    if isinstance(v, dict):
        return "字典"
    if isinstance(v, JishiSet):
        return "集合"
    if isinstance(v, JishiDecimal):
        return "精确小数"
    if isinstance(v, JishiFile):
        return "文件"
    if isinstance(v, Module):
        return "模块"
    if isinstance(v, _Builtin):
        return "内建函数"
    if isinstance(v, ExcType):
        return "异常类型"
    if isinstance(v, JishiClass):
        return f"类 {v.name}"
    if isinstance(v, JishiInstance):
        return f"{v.cls.name} 实例"
    if isinstance(v, JishiError):
        return "异常"
    # 标准库内部类型可以自带中文名（M33）：`表格._Table` 不然会漏成
    # 英文类名 `_Table`，与另外两个宿主（都显示「表格」）对不上
    alias = getattr(v, "_jishi_type_name", None)
    if isinstance(alias, str):
        return alias
    name = type(v).__name__
    if name == "UserFunction":
        return "函数"
    if name == "VmFunction":
        return "函数"
    return name


def truthy(v) -> bool:
    if v is None:
        return False
    return bool(v)


# ---------------------------------------------------------------------------
# 集合（M24.1）
# ---------------------------------------------------------------------------

class JishiDecimal:
    """精确十进制小数（M26.3）：`精确("19.99")` 的返回值。

    存在的理由只有一个：二进制浮点表示不了 0.1，于是 `0.1 + 0.2 == 0.3`
    是假——金额、对账场景里这种「差一分钱」最让人头大。十进制的 0.1
    就是 0.1，能算准。

    实现借用 Python 的 `decimal`（默认 28 位有效数字），但**只**在用户
    显式写 `精确(…)` 时才启用：普通 `0.1` 仍是二进制浮点，与 Python
    一致。不偷偷改小数的语义——对教学语言来说「可预期」比「聪明」重要。
    """

    __slots__ = ("value",)

    def __init__(self, value: decimal.Decimal):
        self.value = value

    def __bool__(self):
        return bool(self.value)

    def __int__(self):
        return int(self.value)

    def __float__(self):
        return float(self.value)

    def __eq__(self, other):
        # 与普通数字比较时按「十进制写法」对齐，否则 `精确("0.3") == 0.3`
        # 会是假（0.3 的二进制真值并非 0.3）——那正是用户最想不通的地方。
        if isinstance(other, JishiDecimal):
            return self.value == other.value
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            try:
                return self.value == _to_decimal(other)
            except JishiError:
                return NotImplemented
        return NotImplemented

    #: 定义了 __eq__ 就没有默认 __hash__ 了（和集合元素不可哈希一致）
    __hash__ = None

    # 下面几个 Python 侧比较运算符，是给「基石自己的工具」用的：
    # 最大/最小/列表.排序 都走 Python 的 max/sorted，没有它们会直接 TypeError。
    # 基石语言里的比较仍走宿主 apply_compare（三个执行器同一套），不受影响。
    def _other(self, other):
        """把对面的数转成 Decimal；不是数字则返回 None。"""
        if isinstance(other, JishiDecimal):
            return other.value
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            try:
                return _to_decimal(other)
            except JishiError:
                return None
        return None

    def __lt__(self, other):
        o = self._other(other)
        return NotImplemented if o is None else self.value < o

    def __le__(self, other):
        o = self._other(other)
        return NotImplemented if o is None else self.value <= o

    def __gt__(self, other):
        o = self._other(other)
        return NotImplemented if o is None else self.value > o

    def __ge__(self, other):
        o = self._other(other)
        return NotImplemented if o is None else self.value >= o

    # 算术运算符同理：基石的 `+` 走宿主 apply_binop，这几个是给 Python 侧
    # 用的（总和/归约 内部会用 Python 的 `+`）。两边语义一致：都是十进制算。
    def _op(self, other, fn):
        o = self._other(other)
        if o is None:
            return NotImplemented
        return JishiDecimal(fn(self.value, o))

    def __add__(self, other):
        return self._op(other, lambda a, b: a + b)

    def __radd__(self, other):
        return self._op(other, lambda a, b: b + a)

    def __sub__(self, other):
        return self._op(other, lambda a, b: a - b)

    def __rsub__(self, other):
        return self._op(other, lambda a, b: b - a)

    def __mul__(self, other):
        return self._op(other, lambda a, b: a * b)

    def __rmul__(self, other):
        return self._op(other, lambda a, b: b * a)

    def __truediv__(self, other):
        return self._op(other, lambda a, b: a / b)

    def __rtruediv__(self, other):
        return self._op(other, lambda a, b: b / a)

    def __neg__(self):
        return JishiDecimal(-self.value)

    def __abs__(self):
        return JishiDecimal(abs(self.value))

    def __repr__(self):
        return str(self.value)


def _to_decimal(v, *, line=None, col=None, filename="<输入>"):
    """把值转成 ``decimal.Decimal``（精确小数的内部表示）。

    小数（float）**先转 str 再转**：`Decimal(0.1)` 会把二进制尾巴原样带进来
    （0.1000000000000000055511151231257827…），`Decimal(str(0.1))` 才是用户
    眼里的 0.1——这一步是「精确」能不能名副其实的关键。
    """
    if isinstance(v, JishiDecimal):
        return v.value
    if isinstance(v, bool):
        raise RunTypeError(
            f"布尔值「{'真' if v else '假'}」不能当精确小数",
            line=line, col=col, filename=filename)
    if isinstance(v, int):
        return decimal.Decimal(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise RunTypeError(f"「{v}」不能变成精确小数（不是有限数）",
                               line=line, col=col, filename=filename)
        return decimal.Decimal(str(v))
    if isinstance(v, str):
        try:
            return decimal.Decimal(v.strip())
        except decimal.InvalidOperation:
            raise RunTypeError(
                f"不能把「{v}」当作精确小数",
                line=line, col=col, filename=filename,
                hint="要写数字，例如 精确(\"19.99\")；"
                     "要和文本拼一起就写 文本(精确值) + \"元\"")
    raise RunTypeError(f"不能把「{type_name(v)}」转成精确小数",
                       line=line, col=col, filename=filename)


class JishiSet:
    """基石集合：**按插入顺序**去重的集合。

    内部用 dict 当有序哈希表，而不是内建 `set`。原因：内建 set 的迭代顺序
    依赖字符串哈希种子（每个进程不同），会让同一个程序两次运行输出不同，
    也会破坏三执行器的逐字节对拍。有序还有一个好处：打印结果稳定可读。

    代价与 Python 一致：元素必须是不可变的（数字、文本、元组…），
    列表/字典/集合本身不能放进来。
    """

    __slots__ = ("_d",)

    def __init__(self, items=None):
        self._d: dict[Any, None] = {}
        if items is not None:
            for it in items:
                self._add(it)

    def _add(self, v) -> None:
        try:
            self._d[v] = None
        except TypeError:
            raise RunTypeError(
                f"「{v}」不能放进集合",
                hint="集合的元素要是不可变的（数字、文本…）；"
                     "列表和字典放不进去")

    def __contains__(self, v) -> bool:
        try:
            return v in self._d
        except TypeError:
            return False

    def __len__(self) -> int:
        return len(self._d)

    def __iter__(self):
        return iter(self._d)

    def __eq__(self, other):
        if isinstance(other, JishiSet):
            # 集合相等与顺序无关（与 Python 一致）
            return len(self._d) == len(other._d) and all(
                k in other._d for k in self._d)
        return NotImplemented

    #: 定义了 __eq__ 就没有默认 __hash__ 了，这与 Python 的 set 不可哈希一致
    __hash__ = None

    def __repr__(self):
        if not self._d:
            return "集合()"          # 空集合不用 {}，那是空字典
        return "{" + ", ".join(jishi_repr(k) for k in self._d) + "}"


def _as_iterable(v, what: str, *, line=None, col=None, filename="<输入>"):
    """把一个值当成可迭代对象取元素；不行就给中文错。"""
    if isinstance(v, (list, tuple, JishiSet)):
        return list(v)
    if isinstance(v, str):
        return list(v)
    if isinstance(v, dict):
        return list(v.keys())
    try:
        return list(v)
    except TypeError:
        raise RunTypeError(
            f"「{v}」不能当{what}用",
            line=line, col=col, filename=filename,
            hint="需要列表、文本、集合或字典")


# ---------------------------------------------------------------------------
# 内建函数
# ---------------------------------------------------------------------------

def _b_print(*args):
    """输出到屏幕。走 `jishi_repr(top=True)`，所以 空/真/假 显示为中文，
    与 REPL 回显、`文本()`、字符串插值用的是同一套规则（M27）。
    """
    print(*[jishi_repr(a, top=True) for a in args])
    return None


def _b_input(prompt: str = ""):
    return input(prompt)


def _b_int(v):
    """`整数(值)`：转成整数。

    文本转不动时抛**值错误**（不是类型错误）——与 Python 的 `int("abc")`
    给 `ValueError` 一致，也与本项目的 `"abc".转整数()` 一致（M41 统一）。
    传进来的东西**类型**就不对（比如列表）才是类型错误。
    """
    if isinstance(v, (list, dict, JishiSet)):
        raise RunTypeError(f"「{jishi_repr(v, top=True)}」不能转成整数")
    try:
        return int(v)
    except ValueError:
        raise RunValueError(f"「{v}」不能转成整数")
    except TypeError:
        raise RunTypeError(f"「{jishi_repr(v, top=True)}」不能转成整数")


def _b_float(v):
    """`小数(值)`：转成小数。归类规则同 `整数`（M41）。"""
    if isinstance(v, (list, dict, JishiSet)):
        raise RunTypeError(f"「{jishi_repr(v, top=True)}」不能转成小数")
    try:
        return float(v)
    except ValueError:
        raise RunValueError(f"「{v}」不能转成小数")
    except TypeError:
        raise RunTypeError(f"「{jishi_repr(v, top=True)}」不能转成小数")


def _b_str(v):
    """`文本(值)`：转成文本。与 `打印` 同一套规则（M27），所以
    `文本(空)` 给「空」而不是 `None`；插值 `` `值：{空}` `` 也因此一致。
    """
    return jishi_repr(v, top=True)


def _b_exact(v):
    """`精确(值)`（M26.3）：把数字或数字文本变成精确小数。

    金额/统计场景用它避开浮点误差：`精确(0.1) + 精确(0.2) == 精确(0.3)`
    为真，而 `0.1 + 0.2 == 0.3` 为假。
    """
    return JishiDecimal(_to_decimal(v))


# -- 上下文管理器（M26.2）--------------------------------------------------

def _ctx_lookup(obj, name: str):
    """找对象上名为 ``name`` 的方法，返回「无参可调用」；没有则返回 None。

    自定义对象的方法存在类的方法表里（调用时把实例塞进首参），内建类型的
    方法存在各自的 ``*_METHODS`` 表里（调用约定是 ``impl(obj, args, node)``）。
    这里把两种约定统一成一种，「进入上下文 / 退出上下文」就不必关心对象
    到底是什么类型。

    ⚠️ **第三条路：宿主对象**（M50 修）。**标准库返回的对象**（比如
    `时间.计时()` 给的计时器）既不是 `JishiInstance`，`method_table` 也不认
    它们——于是即使类里写了 `进入` / `退出`，`用 … 为 …：` 也会报
    「不能当上下文管理器用」。修法是再兜一层：宿主对象有同名可调用属性就用它
    （调用约定是普通 Python 方法，无参）。
    三个执行器共用本函数，所以**改这一处就够**。
    """
    if isinstance(obj, JishiInstance):
        fn = obj.cls.methods.get(name)
        if fn is not None:
            return lambda: fn(obj)
        return None
    table = method_table(obj)
    if table is not None and name in table:
        impl = table[name]
        return lambda: _BoundMethod(impl, obj, name, None, None, "<输入>")()
    fn = getattr(obj, name, None)
    if callable(fn):
        return fn
    return None


def _b_enter_context(obj):
    """`用 <表达式> 为 名字：` 脱糖后的第一步（M26.2）。

    有 `进入` 方法就调用它、用返回值绑定名字（对应 Python 的 ``__enter__``）；
    否则若对象能自己收拾（有 `退出` 或 `关闭`），就绑定对象本身——文件、
    锁这类「进入即自身」的写法不必硬写一个 `进入`。
    两样都没有就在**这里**报错：比等到 finally 里才报，位置准得多。
    """
    enter = _ctx_lookup(obj, "进入")
    if enter is not None:
        return enter()
    if (_ctx_lookup(obj, "退出") is not None
            or _ctx_lookup(obj, "关闭") is not None):
        return obj
    name = type_name(obj)
    if isinstance(obj, (list, dict)):
        hint = (f"「{name}」用完不需要收拾，所以不能这样写；"
                f"只是想在块里用一下的话，直接写普通语句就行")
    else:
        hint = ("上下文管理器要有「退出」或「关闭」方法（离开时自动调用），"
                "也可以再定义「进入」来决定绑定什么值。"
                "最常见的用法：用 打开(\"数据.txt\") 为 f：")
    raise RunTypeError(f"「{name}」不能当上下文管理器用", hint=hint)


def _b_exit_context(obj):
    """离开 `用 … 为` 代码块时调用（M26.2）：优先 `退出`，其次 `关闭`。

    调用时机由脱糖后的 `尝试/最终` 保证——正常结束、`返回`、`中断`、
    `抛出` 都会走到这里。返回值忽略（不像 Python 那样能吞异常：
    吞异常会让「出问题快速找到」变难，有意不做）。
    """
    for name in ("退出", "关闭"):
        fn = _ctx_lookup(obj, name)
        if fn is not None:
            fn()
            return None
    return None


# -- 文件（M26.2）----------------------------------------------------------

#: 「打开」的中文模式名 → Python 模式串
_FILE_MODES = {
    "读": "r",
    "写": "w",
    "追加": "a",
    "读写": "r+",
}
#: 也认英文模式，照顾从别的语言过来的习惯
_FILE_MODES_EN = ("r", "w", "a", "r+", "w+", "a+")


def _b_open(path, mode="读"):
    """打开文件，返回文件对象（M26.2）。

    模式用中文：读 / 写 / 追加 / 读写（也认 r/w/a/r+/w+/a+）。
    一律按 UTF-8 读写——Windows 上默认编码是 GBK，中文会乱码。
    """
    if not isinstance(path, str):
        raise RunTypeError(
            f"「打开」的第一个参数要是文件路径，得到了「{type_name(path)}」",
            hint="例如：打开(\"数据.txt\")")
    if not isinstance(mode, str):
        raise RunTypeError(
            f"「打开」的第二个参数（模式）要是文本，得到了「{type_name(mode)}」")
    py_mode = _FILE_MODES.get(mode)
    if py_mode is None and mode in _FILE_MODES_EN:
        py_mode = mode
    if py_mode is None:
        raise RunTypeError(
            f"不认识的打开模式「{mode}」",
            hint="可用的模式：读、写、追加、读写（也认 r/w/a/r+/w+/a+）")
    try:
        handle = open(path, py_mode, encoding="utf-8")
    except Exception as e:
        raise translate_python_exception(e)
    return JishiFile(handle, path, mode)


def _b_len(v):
    try:
        return len(v)
    except TypeError:
        # 定义了「迭代」的自定义对象也能求长度（M24.1）：既然能遍历，
        # 数一数有几个元素是自然的事，不必再要求作者额外写一个方法。
        if isinstance(v, JishiInstance) and "迭代" in v.cls.methods:
            return sum(1 for _ in v)
        raise RunTypeError(f"「{v}」没有长度")


def _b_range(*args):
    try:
        if len(args) == 1:
            return list(range(args[0]))
        if len(args) == 2:
            return list(range(args[0], args[1]))
        if len(args) == 3:
            return list(range(args[0], args[1], args[2]))
    except (TypeError, ValueError):
        raise RunTypeError(
            f"「范围」需要 1 到 3 个整数参数，例如 范围(5)、范围(1, 5)、范围(1, 5, 2)")
    raise RunTypeError("「范围」最多接受 3 个参数")


def _b_enumerate(*args):
    """`带下标(可迭代, 起始=0)`：造 [下标, 元素] 对。

    与 Python 的 `enumerate` 同语义，但**立刻返回列表**而不是惰性迭代器——
    本项目的迭代协议允许惰性，可惰性对象在调试器「看变量」、跨宿主对拍、
    `文本()` 渲染时都得额外照顾；而它几乎总是直接进 `遍历`，先算出来
    反而更简单（与 `范围` 的做法一致）。
    """
    if not 1 <= len(args) <= 2:
        raise RunTypeError(
            "「带下标」需要 1 到 2 个参数，例如 带下标(列表)、带下标(列表, 1)")
    seq = _as_iterable(args[0], "「带下标」的内容")
    try:
        start = int(args[1]) if len(args) == 2 else 0
    except (TypeError, ValueError):
        raise RunTypeError(f"「带下标」的起始值要是整数，得到了「{args[1]}」")
    return [[start + i, v] for i, v in enumerate(seq)]


def _b_zip(*args):
    """`配对(甲, 乙, …)`：把多个可迭代按位置配成 [甲[i], 乙[i], …]。

    与 Python 的 `zip` 同语义（**取最短**，多余的丢弃）。零参数报错
    （Python 的 `zip()` 给空迭代器，那多半是写错了）。
    """
    if not args:
        raise RunTypeError("「配对」至少要给一个可迭代对象")
    seqs = [_as_iterable(a, "「配对」的内容") for a in args]
    return [list(t) for t in zip(*seqs)]


def _single_iterable_arg(v):
    """单参数调用 最大/最小 时，判断这个参数是不是「一整个序列」。

    是序列就按元素求最值；不是（数字、文本、布尔…）就当成「唯一的一个值」，
    与 Python 的 max(序列) / max(a) 语义一致。文本刻意当标量处理，保持既有行为。
    """
    if isinstance(v, (list, tuple, JishiSet)):
        return v
    if isinstance(v, dict):
        return v.keys()
    if isinstance(v, JishiInstance) and "迭代" in v.cls.methods:
        return v
    return None


def _b_max(*args):
    # M18.3：单参数且是列表/元组 → max(序列)；多参数 → max(参数们)。
    # M24.1 扩展：集合、字典（按键）、定义了「迭代」的自定义对象同样按序列处理。
    try:
        if len(args) == 1:
            seq = _single_iterable_arg(args[0])
            if seq is not None:
                return max(seq)
        return max(args)
    except (TypeError, ValueError):
        raise RunTypeError(f"「{args}」不能求最大值，需要一个非空列表或若干数字")


def _b_min(*args):
    try:
        if len(args) == 1:
            seq = _single_iterable_arg(args[0])
            if seq is not None:
                return min(seq)
        return min(args)
    except (TypeError, ValueError):
        raise RunTypeError(f"「{args}」不能求最小值，需要一个非空列表或若干数字")


def _b_sum(v):
    """求列表所有元素的和。

    精确小数列表要显式给十进制起点：Python 的 `sum` 默认从整数 0 开始，
    而 `0 + Decimal` 会 TypeError——`总和([精确("0.1"), …])` 不该因此失败。
    """
    try:
        items = list(v)
    except TypeError:
        raise RunTypeError(f"「{v}」不能求和，需要一个全是数字的列表")
    start = 0
    for it in items:
        if isinstance(it, JishiDecimal):
            start = JishiDecimal(decimal.Decimal(0))
            break
    try:
        return sum(items, start)
    except (TypeError, ValueError):
        raise RunTypeError(f"「{v}」不能求和，需要一个全是数字的列表")


def _as_bit_int(v, func: str) -> int:
    """位运算的参数必须是**整数**。

    为什么不收 `真`/`假`：Python 里 `True & 1` 给 1，但那会让「标志位」和
    「逻辑值」混在一起；基石把布尔当**独立类型**（`类型(真)` 是「布尔」），
    位运算只对整数有意义——明确报错比静默按 1/0 算清楚。
    """
    if isinstance(v, bool) or not isinstance(v, int):
        raise RunTypeError(
            f"「{func}」需要整数，但收到「{jishi_repr(v, top=True)}」"
            f"（{type_name(v)}）")
    return v


def _as_shift_count(v, func: str) -> int:
    """位移位数：非负整数，且**不能大到把内存撑爆**。

    `1 << 10**9` 在 Python 里会真的去申请 125 MB——所以给个上限（100 万位，
    约 125 KB），超过就报错说明。这不算限制正常用法：基石整数本身在 C VM /
    Rust 侧到 64 位 / 128 位就界了，真要移一百万位请用标准库的大数运算。
    """
    n = _as_bit_int(v, func)
    if n < 0:
        raise RunTypeError(f"「{func}」的位数不能是负数（收到 {n}）")
    if n > 1_000_000:
        raise RunTypeError(f"「{func}」的位数太大（收到 {n}，上限 1000000）")
    return n


def _b_bit_and(a, b):
    """按位与。用于标志位、掩码这类场合（日常业务代码很少见）。"""
    return _as_bit_int(a, "位与") & _as_bit_int(b, "位与")


def _b_bit_or(a, b):
    """按位或。"""
    return _as_bit_int(a, "位或") | _as_bit_int(b, "位或")


def _b_bit_xor(a, b):
    """按位异或。"""
    return _as_bit_int(a, "位异或") ^ _as_bit_int(b, "位异或")


def _b_bit_not(a):
    """按位取反（`~x`，Python 语义：`~x == -x - 1`）。"""
    return ~_as_bit_int(a, "位取反")


def _b_shl(a, n):
    """左移：`左移(1, 4)` 给 16。"""
    return _as_bit_int(a, "左移") << _as_shift_count(n, "左移")


def _b_shr(a, n):
    """右移：`右移(16, 4)` 给 1。"""
    return _as_bit_int(a, "右移") >> _as_shift_count(n, "右移")


def _b_type(v):
    return type_name(v)


def _b_reverse(v):
    try:
        if isinstance(v, str):
            return v[::-1]
        return list(reversed(v))
    except TypeError:
        raise RunTypeError(f"「{v}」不能反转，需要列表或文本")


def _b_assert(condition, message=None):
    """断言（M23.1）：条件为假就抛「断言错误」。

    故意用 `RunAssertionError` 而不是 `SemanticFuncError`，这样它能被
    `捕获 断言错误 为 e` 接住，也能被 `捕获 运行期错误` 兜住。
    """
    if truthy(condition):
        return None
    # 标题（「断言没通过」）由 RunAssertionError 提供，这里只放用户的消息，
    # 否则渲染出来会是「断言没通过（断言没通过：…）」。
    raise RunAssertionError(str(message) if message is not None else "条件不成立")


#: 内建类型名（用于 `类型(值)` 比较，也用于「是实例」误用时给提示）
_TYPE_NAMES = frozenset(
    {"整数", "小数", "文本", "列表", "字典", "布尔", "空", "函数"})


def _b_super(instance, class_name):
    """超（M23.4）：解析期被脱糖成 `超(自身, "定义类名")`，这里实现语义。

    普通用户不需要直接调用它，写 `超().方法(...)` 即可。
    """
    if not isinstance(instance, JishiInstance):
        raise RunTypeError(
            f"「超()」只能用在自己的方法里（第一参数该是实例，"
            f"现在拿到的是「{type_name(instance)}」）",
            hint="在方法里写 超().方法名(参数)，例如 超().初始化(名字)")
    defining = None
    cls = instance.cls
    seen = 0
    while cls is not None and seen < 64:
        if cls.name == class_name:
            defining = cls
            break
        cls = cls.base
        seen += 1
    if defining is None:
        raise RunTypeError(
            f"「超()」找不到定义类「{class_name}」——"
            f"它不在「{instance.cls.name}」的继承链上",
            hint="这通常说明类的继承关系被改过，检查一下「继承」写的基类")
    return JishiSuper(instance, defining)


def _b_set_ctor(*args):
    """集合（M24.1）：`集合()` 造空集合，`集合(可迭代)` 去重造集合。

    去重后保持**首次出现**的顺序，例如 `集合([3, 1, 3, 2])` → `{3, 1, 2}`。
    """
    if len(args) > 1:
        raise RunTypeError(
            f"「集合」最多接受 1 个参数，但传了 {len(args)} 个",
            hint="用法：集合() 造空集合，集合([1, 2, 2]) 去重")
    if not args:
        return JishiSet()
    return JishiSet(_as_iterable(args[0], "集合的初始内容"))


#: 类型标注里认识的类型名 → 判定函数（M24.3）。
#: **只检查认识的名字**：不认识的一律跳过（例如作者自定义的 `商品`、`订单`），
#: 这样标注既能当文档用，又不会因为名字不在表里就误报。
_TYPE_PREDICATES = {
    "整数": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "小数": lambda v: isinstance(v, float),
    "文本": lambda v: isinstance(v, str),
    "列表": lambda v: isinstance(v, list),
    "字典": lambda v: isinstance(v, dict),
    "集合": lambda v: isinstance(v, JishiSet),
    "布尔": lambda v: isinstance(v, bool),
    "空": lambda v: v is None,
    "函数": callable,
    "任意": lambda v: True,
    "值": lambda v: True,
}


def _b_check_args(desc, values, func_name):
    """按类型标注校验实参（M24.3，给编译器发射的函数前导调用）。

    参数：
      desc      扁平描述 `[参数名, 类型名, 参数名, 类型名, …]`
      values    实参值列表（与 desc 里的参数一一对应）
      func_name 函数名（只为拼出好读的报错）

    只在类型名**认识**时校验；不认识的当作纯文档跳过。
    """
    for i in range(0, len(desc) - 1, 2):
        pname = desc[i]
        tname = desc[i + 1]
        pred = _TYPE_PREDICATES.get(tname)
        if pred is None:
            continue                        # 不认识的标注：只作文档
        if i // 2 >= len(values):
            continue
        value = values[i // 2]
        if not pred(value):
            raise RunTypeError(
                f"函数「{func_name}」的参数「{pname}」要求是「{tname}」，"
                f"但传进来的是「{type_name(value)}」",
                hint=f"检查一下调用处传的第 {i // 2 + 1} 个参数，"
                     f"或者把标注改成实际要传的类型")


def _instance_of(actual: "JishiClass", target: "JishiClass") -> bool:
    """沿基类链判断 actual 是不是 target 的（子）类。"""
    seen = 0
    while actual is not None and seen < 64:
        if actual is target:
            return True
        actual = actual.base
        seen += 1
    return False


def _b_isinstance(value, cls):
    """是实例（M23.1）：判断值是不是某个类（或其子类）的实例。"""
    if not isinstance(cls, JishiClass):
        hint = "用法：是实例(对象, 类名)"
        # 常见误用：第二参数写成内建转换函数（整数/文本…），想判断内建类型。
        # 基线语言里内建类型用 `类型(值) == "整数"` 判断，直接给出来省得试错。
        if isinstance(cls, _Builtin) and cls.name in _TYPE_NAMES:
            hint = (f"「{cls.name}」是转换函数，不是类。"
                    f"判断内建类型请写：类型(值) == \"{cls.name}\"")
        raise RunTypeError(
            f"「是实例」的第二个参数要是一个类，但给的是「{type_name(cls)}」",
            hint=hint)
    if not isinstance(value, JishiInstance):
        return False
    return _instance_of(value.cls, cls)


def is_internal_builtin(name: str, b: Any = None) -> bool:
    """这个内建名是不是**内部用**的（不该出现在用户可见的语言规格 / 补全里）。

    判据两条（M40 统一）：

    - 名字以 `$` 开头：用户**写不出来**（词法器把 `$` 判成无法识别的字符），
      例如文本插值脱糖出的 `$文本`；
    - 说明以「内部用」开头：靠编译器发射、用户不需要知道的，例如 `检查实参`。

    这类名字一旦漏进 `ai.build_lang_spec()`，AI 提示词与编辑器补全就会把
    `$文本` 当成可写的函数推荐出去——「内部的东西不该出现在用户的选项里」。
    """
    if name.startswith("$"):
        return True
    doc = getattr(b, "doc", "") or ""
    return doc.startswith("内部用")


def new_builtins() -> dict[str, Any]:
    """返回一份全新的内建函数表（每个执行器实例一份）。"""
    return {
        "打印": _Builtin("打印", _b_print, "输出内容到屏幕"),
        "输入": _Builtin("输入", _b_input, "读取用户输入的一行文本"),
        "整数": _Builtin("整数", _b_int, "把值转成整数"),
        "小数": _Builtin("小数", _b_float, "把值转成小数"),
        "文本": _Builtin("文本", _b_str, "把值转成文本"),
        "精确": _Builtin(
            "精确", _b_exact,
            "精确(\"19.99\")：转成精确小数，金额/对账用它避开浮点误差"),
        "打开": _Builtin(
            "打开", _b_open,
            "打开(路径, 模式=读)：返回文件对象，常配「用 … 为 f：」自动关闭"),
        "进入上下文": _Builtin(
            "进入上下文", _b_enter_context,
            "供「用 … 为」脱糖使用：有 进入 就调用，否则返回对象自身"),
        "退出上下文": _Builtin(
            "退出上下文", _b_exit_context,
            "供「用 … 为」脱糖使用：离开代码块时调用 退出 或 关闭"),
        "长度": _Builtin("长度", _b_len, "取列表、文本等的长度"),
        "范围": _Builtin(
            "范围", _b_range,
            "生成一段整数序列（范围(5)、范围(1, 5)、范围(1, 5, 2)）"),
        "带下标": _Builtin(
            "带下标", _b_enumerate,
            "带下标(可迭代, 起始=0)：造 [下标, 元素] 对，"
            "遍历时想同时拿到序号就用它"),
        "配对": _Builtin(
            "配对", _b_zip,
            "配对(甲, 乙, …)：按位置把多个列表配成 [甲[i], 乙[i], …]，"
            "长度取最短的那个"),
        "最大": _Builtin("最大", _b_max, "取列表中的最大值"),
        "最小": _Builtin("最小", _b_min, "取列表中的最小值"),
        "总和": _Builtin("总和", _b_sum, "求列表中所有数的和"),
        "类型": _Builtin("类型", _b_type, "查看值的类型"),
        # 位运算：**不做语法**（`& | ^ << >>` 日常罕见，且要动三执行器），
        # 改成一小组内建（M36「关 0」）——标志位/掩码/协议解析够用了
        "位与": _Builtin("位与", _b_bit_and, "位与(甲, 乙)：按位与，标志位/掩码用"),
        "位或": _Builtin("位或", _b_bit_or, "位或(甲, 乙)：按位或"),
        "位异或": _Builtin("位异或", _b_bit_xor, "位异或(甲, 乙)：按位异或"),
        "位取反": _Builtin("位取反", _b_bit_not, "位取反(甲)：按位取反，等价于 -甲 - 1"),
        "左移": _Builtin("左移", _b_shl, "左移(值, 位数)：值 × 2^位数"),
        "右移": _Builtin("右移", _b_shr, "右移(值, 位数)：值 ÷ 2^位数（向下取整）"),
        "反转": _Builtin("反转", _b_reverse, "反转列表或文本"),
        "断言": _Builtin(
            "断言", _b_assert,
            "断言(条件, 消息?)：条件不成立就报错，用来尽早发现「不该发生」"),
        "是实例": _Builtin(
            "是实例", _b_isinstance,
            "是实例(值, 类)：判断值是不是这个类（或其子类）造出来的"),
        "超": _Builtin(
            "超", _b_super,
            "超()：在方法里调基类的同名方法，例如 超().初始化(名字)"),
        "集合": _Builtin(
            "集合", _b_set_ctor,
            "集合(可迭代)：去重造集合（保持首次出现的顺序）；集合() 造空集合"),
        "检查实参": _Builtin(
            "检查实参", _b_check_args,
            "内部用：按类型标注校验函数实参（由编译器在函数前导发射）"),
        # 文本插值脱糖的目标（M40）：**内部保留名**，不是给用户调的。
        # 名字带 `$` 是因为词法器不认这个字符、用户写不出来，因此永远不会被
        # `导入 文本` 这类用户代码遮蔽（原先脱糖成内建 `文本`，一句导入就打断
        # 所有 f-string）。与 parser.INTERP_TO_TEXT 是同一个名字。
        "$文本": _Builtin("$文本", _b_str, "内部用：文本插值里的值→文本"),
        "空": None,
        # 异常类型：既可 `抛出 值错误("…")`，也可 `捕获 值错误 为 e`
        **{name: ExcType(name, cls) for name, cls in EXCEPTION_TYPES.items()},
    }


# ---------------------------------------------------------------------------
# 异常：`抛出` 的规范化与 `捕获` 的匹配（M5a）
# ---------------------------------------------------------------------------

def make_exception(value: Any, *, line: Optional[int] = None,
                   col: Optional[int] = None,
                   filename: str = "<输入>") -> JishiError:
    """把 `抛出` 的值规范化成异常对象。

    - 已经是异常对象 → 原样抛出；
    - 异常类型（`值错误` 这类内建名）→ 造一个不带消息的实例；
    - 其他任意值 → 包装成「异常」，原值留在 `e.消息` 里。

    抛出点才补行列号，因此异常即使被捕获再抛出，位置也指向最新的抛出点。
    """
    if isinstance(value, JishiError):
        exc = value
    elif isinstance(value, ExcType):
        exc = value.cls()
    else:
        exc = RunCustomError(_display(value))

    if exc.line is None:
        exc.line = line
        exc.col = col
    if not exc.filename or exc.filename == "<输入>":
        exc.filename = filename
    return exc


def exception_matches(exc: BaseException, cond: Any, *,
                      line: Optional[int] = None,
                      col: Optional[int] = None,
                      filename: str = "<输入>") -> bool:
    """`捕获 …` 是否接得住这个异常。

    - 不写条件（裸 `捕获:`）→ 全接；
    - 写异常类型 → 按 isinstance 匹配，基类抓得住子类
      （`异常` 最宽，`值错误` 只接值错误）。
    """
    if cond is None:
        return True
    if isinstance(cond, ExcType):
        return isinstance(exc, cond.cls)
    raise RunTypeError(
        f"「捕获」后面只能写异常类型（比如 类型错误、值错误），"
        f"不能写{type_name(cond)}",
        line=line, col=col, filename=filename)


def _display(v: Any) -> str:
    """值的中文显示（与 `打印` 一致的格式，用于包装抛出值的消息）。"""
    if v is None:
        return "空"
    if v is True:
        return "真"
    if v is False:
        return "假"
    if isinstance(v, str):
        return v
    return str(v)


# ---------------------------------------------------------------------------
# 名字查找（未定义时给中文报错与"你是不是想写"建议）
# ---------------------------------------------------------------------------

def lookup_name(name: str, pool, *, line=None, col=None,
                filename="<输入>"):
    """在名字池（可迭代对象）里查名字；查不到抛中文错误。"""
    if name in pool:
        return pool[name]
    hint = PY_NAME_HINT.get(name)
    if hint:
        raise SemanticNameError(f"找不到名字「{name}」", line=line, col=col,
                                filename=filename, hint=hint)
    import difflib
    matches = difflib.get_close_matches(name, list(pool), n=3, cutoff=0.5)
    if matches:
        hint = "你是不是想写「" + "」或「".join(matches) + "」？"
    raise SemanticNameError(f"找不到名字「{name}」", line=line, col=col,
                            filename=filename, hint=hint)


# ---------------------------------------------------------------------------
# 运算
# ---------------------------------------------------------------------------

#: 精确小数支持的二元运算（与普通数字一致）
_DECIMAL_OPS = ("+", "-", "*", "/", "//", "%", "**")


def _decimal_binop(op, left, right, *, line=None, col=None, filename="<输入>"):
    """至少一边是精确小数的二元运算：两边先转 Decimal，算完还是精确小数。"""
    if op not in _DECIMAL_OPS:
        raise RunTypeError(f"精确小数不支持「{op}」运算",
                           line=line, col=col, filename=filename)
    a = _to_decimal(left, line=line, col=col, filename=filename)
    b = _to_decimal(right, line=line, col=col, filename=filename)
    try:
        if op == "/" and b == 0:
            raise RunZeroDivisionError("", line=line, col=col,
                                       filename=filename)
        if op in ("//", "%") and b == 0:
            raise RunZeroDivisionError("", line=line, col=col,
                                       filename=filename)
        return JishiDecimal({
            "+": lambda: a + b,
            "-": lambda: a - b,
            "*": lambda: a * b,
            "/": lambda: a / b,
            "//": lambda: a // b,
            "%": lambda: a % b,
            "**": lambda: a ** b,
        }[op]())
    except JishiError:
        raise
    except Exception as e:
        raise translate_python_exception(e, line=line, col=col,
                                         filename=filename)


def _decimal_compare(op, left, right, *, line=None, col=None,
                     filename="<输入>"):
    """精确小数参与的比较：两边转 Decimal 再比（0.3 就是 0.3）。"""
    a = _to_decimal(left, line=line, col=col, filename=filename)
    b = _to_decimal(right, line=line, col=col, filename=filename)
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == "<":
        return a < b
    if op == ">":
        return a > b
    if op == "<=":
        return a <= b
    if op == ">=":
        return a >= b
    raise RunTypeError(f"精确小数不能做「{op}」比较",
                       line=line, col=col, filename=filename)


def apply_binop(op: str, left, right, *, line=None, col=None,
                filename="<输入>"):
    """二元运算（+ - * / // % **）。"""
    # 精确小数（M26.3）：任一边是它就走十进制运算。C VM 的整数/浮点快路径
    # 不认识这个宿主类型，会自动回落到这里，所以三执行器仍然一致。
    if isinstance(left, JishiDecimal) or isinstance(right, JishiDecimal):
        return _decimal_binop(op, left, right, line=line, col=col,
                              filename=filename)
    try:
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            if right == 0:
                raise RunZeroDivisionError("", line=line, col=col,
                                           filename=filename)
            return left / right
        if op == "//":
            if right == 0:
                raise RunZeroDivisionError("", line=line, col=col,
                                           filename=filename)
            return left // right
        if op == "%":
            if right == 0:
                raise RunZeroDivisionError("", line=line, col=col,
                                           filename=filename)
            return left % right
        if op == "**":
            return left ** right
    except JishiError:
        raise
    except Exception as e:
        raise translate_python_exception(e, line=line, col=col,
                                         filename=filename)
    raise RunError(f"不支持的运算符「{op}」", line=line, col=col,
                   filename=filename)


def apply_unary(op: str, v, *, line=None, col=None, filename="<输入>"):
    if op == "-":
        if isinstance(v, JishiDecimal):
            return JishiDecimal(-v.value)
        try:
            return -v
        except TypeError:
            raise RunTypeError(f"「{v}」不能取负号", line=line, col=col,
                               filename=filename)
    if op == "非":
        return not truthy(v)
    raise RunError(f"不支持的一元运算符「{op}」", line=line, col=col,
                   filename=filename)


def contains(container, item, *, line=None, col=None, filename="<输入>"):
    """成员测试（M23.2）：``item 在 container`` 的语义。

    支持列表（按值）、字典（按键）、文本（找子串），与 `列表.包含()`、
    `字典.包含()`、`文本.包含()` 完全一致——三者本来就是同一个 ``in``。
    其他类型给中文报错，并说清「什么才能做成员测试」。
    """
    try:
        return item in container
    except TypeError:
        raise RunTypeError(
            f"「{item}」不能在「{type_name(container)}」里找",
            line=line, col=col, filename=filename,
            hint="「在」只能用于列表、字典（按「键」找）、文本（找子串）")


def _identity_snapshot(value):
    """给「是/不是」用的同一性指纹。

    基石里 `是` 的语义定为**同一性判断**，与 Python 的 ``is`` 一致：
    - 空、真、假 是单例，只和自己同一；
    - 数字/文本按值比较（Python 对 int/str 有小整数缓存与驻留，
      若严格按 ``is`` 会给用户「有时真有时假」的随机感，故按值统一）；
    - 列表/字典/实例按对象身份（两个内容相同的列表不是同一个东西）。
    """
    if value is None or type(value) is bool:
        return ("单例", value)
    if isinstance(value, (int, float, str)):
        return ("值", type(value).__name__, value)
    return ("身份", id(value))


def apply_compare(op: str, left, right, *, line=None, col=None,
                  filename="<输入>"):
    # 精确小数（M26.3）：算术比较交给十进制做（混合 0.3 也能相等）。
    # 成员测试/同一性（在·不在·是·不是）不走这里——`精确("1") 在 [1]`
    # 应该按元素比较，而不是让「精确」拒绝参加。
    if op in ("==", "!=", "<", ">", "<=", ">=") and (
            isinstance(left, JishiDecimal) or isinstance(right,
                                                         JishiDecimal)):
        return _decimal_compare(op, left, right, line=line, col=col,
                                filename=filename)
    try:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        if op == ">=":
            return left >= right
        # 成员测试（M23.2）
        if op == "在":
            return contains(right, left, line=line, col=col, filename=filename)
        if op == "不在":
            return not contains(right, left, line=line, col=col,
                                filename=filename)
        # 同一性（M23.2）
        if op == "是":
            return _identity_snapshot(left) == _identity_snapshot(right)
        if op == "不是":
            return _identity_snapshot(left) != _identity_snapshot(right)
    except TypeError:
        raise RunTypeError(f"不能比较「{left}」和「{right}」",
                           line=line, col=col, filename=filename)
    raise RunError(f"不支持的比较「{op}」", line=line, col=col,
                   filename=filename)


# ---------------------------------------------------------------------------
# 类与实例（M5b）
# ---------------------------------------------------------------------------

class JishiClass:
    """基石类（`类 狗:` 定义产生的对象）。

    methods 是「方法名 → 函数」；继承时复制基类方法表，子类可覆盖。
    class_vars 是类变量（M23.3）：写在类体里、所有实例共享的名字。
    """

    __slots__ = ("name", "methods", "base", "class_vars")

    def __init__(self, name: str, methods: dict[str, Any],
                 base: Optional["JishiClass"] = None,
                 class_vars: Optional[dict[str, Any]] = None):
        self.name = name
        self.base = base
        # 继承：合并基类方法表，子类同名方法覆盖
        merged: dict[str, Any] = {}
        if base is not None:
            merged.update(base.methods)
        merged.update(methods)
        self.methods = merged

        # 类变量同样合并基类的（浅拷贝，与方法的处理保持一致）。
        # 查找顺序与 Python 一致：实例字段 → 类变量（含基类）→ 方法。
        merged_vars: dict[str, Any] = {}
        if base is not None:
            merged_vars.update(base.class_vars)
        merged_vars.update(class_vars or {})
        self.class_vars = merged_vars

    def __repr__(self):
        return f"<类 {self.name}>"


class JishiSuper:
    """`超()` 的返回值（M23.4）：从**定义类**的基类起查方法，并绑定实例。

    查找从「定义类的基类」开始，而不是从实例的类开始——这是 Python
    `super()` 的关键语义：在 `狗` 的方法里写 `超()`，只会往 `动物` 及更上层
    找，不会又回到 `狗` 自己（否则就是自递归）。
    """

    __slots__ = ("instance", "defining", "line", "col", "filename")

    def __init__(self, instance: "JishiInstance", defining: "JishiClass",
                 line=None, col=None, filename="<输入>"):
        self.instance = instance
        self.defining = defining
        self.line = line
        self.col = col
        self.filename = filename

    def __repr__(self):
        return f"<超 {self.defining.name}>"


class JishiInstance:
    """基石类的实例（`新建 狗(...)` 产生）。

    fields 是「字段名 → 值」；方法从类里查（绑定实例作为「自身」首参）。
    """

    __slots__ = ("cls", "fields")

    def __init__(self, cls: JishiClass):
        self.cls = cls
        self.fields: dict[str, Any] = {}

    def __repr__(self):
        """自定义打印（M26.1）：类里定义了 ``文本(自身)`` 就用它的返回值。

        `打印(对象)` 与 `文本(对象)` 在三个执行器里都是走宿主的 `str()`，
        C VM 调内建也落在同一个 Python 内建上，所以只改这一处就三处一致。

        没定义 ``文本`` 时回落到 ``<类名 实例>``（保持既有行为）。
        """
        fn = self.cls.methods.get("文本")
        if fn is None:
            return f"<{self.cls.name} 实例>"
        result = fn(self)
        if not isinstance(result, str):
            raise RunTypeError(
                f"类「{self.cls.name}」的「文本」方法返回了"
                f"「{type_name(result)}」，要返回文本",
                hint="「文本」方法的返回值会被 打印/文本() 直接使用，"
                     "所以必须是文本，例如 返回 \"甲(\" + 文本(自身.x) + \")\"")
        return result

    def __iter__(self):
        """迭代协议（M24.1）：让自定义对象能直接用于 `遍历`。

        约定：类里定义 ``迭代(自身)``，返回一个可遍历的东西（列表、文本、
        集合、字典…）。三个执行器都走 Python 的 ``iter()``（C VM 的迭代
        封装 `_Iter` 也是 `iter(obj)`），所以只在这里实现一处即可。

        取不到迭代器时抛 ``TypeError``，由外层翻译成中文报错并提示
        「可以定义 迭代() 方法」；返回自身会无限递归，单独拦下。
        """
        fn = self.cls.methods.get("迭代")
        if fn is None:
            raise TypeError("not iterable")
        result = fn(self)
        if result is self:
            raise RunTypeError(
                f"类「{self.cls.name}」的「迭代」方法返回了实例自己，"
                f"这样会无限循环",
                hint="「迭代」应返回一个列表、文本或集合，不是自身")
        return iter(result)


class _BoundUserMethod:
    """绑定到实例的方法：调用时把实例塞进第一个参数（「自身」）。"""

    __slots__ = ("fn", "instance", "name", "line", "col", "filename")

    #: 给 `类型()` 用的中文名（见 type_name 的钩子）。不写的话会漏出
    #: 内部类名 `_BoundUserMethod`，与另外两个宿主也不一致（M34）
    _jishi_type_name = "方法"

    def __init__(self, fn, instance, name, line, col, filename):
        self.fn = fn
        self.instance = instance
        self.name = name
        self.line = line
        self.col = col
        self.filename = filename

    def __call__(self, *args, **kwargs):
        try:
            return self.fn(self.instance, *args, **kwargs)
        except JishiError:
            raise
        except Exception as e:
            raise translate_python_exception(
                e, line=self.line, col=self.col, filename=self.filename)

    def __repr__(self):
        return f"<方法 {self.name}>"


# ---------------------------------------------------------------------------
# 属性 / 下标
# ---------------------------------------------------------------------------

def get_attr(obj, attr, *, line=None, col=None, filename="<输入>"):
    """属性/方法访问：模块 → 基石中文方法 → Python 公开属性。"""
    if isinstance(obj, JishiSuper):
        # 超()（M23.4）：从定义类的**基类**起找，方法绑定实例。
        base = obj.defining.base
        if base is None:
            raise RunTypeError(
                f"类「{obj.defining.name}」没有基类，"
                f"用不了「超()」——它用来调基类的方法",
                line=line or obj.line, col=col or obj.col,
                filename=filename)
        if attr in base.methods:
            return _BoundUserMethod(base.methods[attr], obj.instance, attr,
                                    line or obj.line, col or obj.col, filename)
        if attr in base.class_vars:
            return base.class_vars[attr]
        raise RunTypeError(
            f"基类「{base.name}」里没有「{attr}」",
            line=line or obj.line, col=col or obj.col, filename=filename,
            hint=f"基类「{base.name}」的方法：{'、'.join(sorted(base.methods))}")
    if isinstance(obj, Module):
        if attr in obj.attrs:
            return obj.attrs[attr]
        raise SemanticFuncError(
            f"模块「{obj.name}」里没有「{attr}」", line=line, col=col,
            filename=filename,
            hint="可以看看模块里有哪些函数，例如：打印(dir(模块名)) 在后续版本可用")
    if isinstance(obj, JishiInstance):
        # 实例字段优先，其次类变量（含基类），最后类方法（绑定实例）
        if attr in obj.fields:
            return obj.fields[attr]
        if attr in obj.cls.class_vars:
            return obj.cls.class_vars[attr]
        if attr in obj.cls.methods:
            return _BoundUserMethod(obj.cls.methods[attr], obj, attr,
                                    line, col, filename)
        hint = f"类「{obj.cls.name}」没有字段或方法「{attr}」"
        raise RunTypeError(f"「{type_name(obj)}」没有属性「{attr}」",
                           line=line, col=col, filename=filename, hint=hint)
    if isinstance(obj, JishiClass):
        if attr in obj.class_vars:
            return obj.class_vars[attr]        # 类变量（M23.3）
        if attr in obj.methods:
            return obj.methods[attr]   # 类上的方法（未绑定，调用时需传实例）
        hint = f"类「{obj.name}」的方法：{'、'.join(sorted(obj.methods))}"
        raise RunTypeError(f"「类 {obj.name}」没有方法「{attr}」",
                           line=line, col=col, filename=filename, hint=hint)
    table = method_table(obj)
    if table is not None and attr in table:
        impl = table[attr]
        return _BoundMethod(impl, obj, attr, line, col, filename)
    if not attr.startswith("_"):
        try:
            return getattr(obj, attr)
        except AttributeError:
            pass
    if table is not None:
        hint = f"可用的方法：{'、'.join(sorted(table))}"
        title = f"「{type_name(obj)}」没有方法「{attr}」"
    else:
        hint = _attr_hint(obj, attr)
        title = f"「{type_name(obj)}」没有属性「{attr}」"
    raise RunTypeError(title, line=line, col=col, filename=filename, hint=hint)


def set_attr(obj, attr, value, *, line=None, col=None,
             filename="<输入>"):
    if isinstance(obj, Module):
        obj.attrs[attr] = value
        return
    if isinstance(obj, JishiInstance):
        obj.fields[attr] = value
        return
    if isinstance(obj, JishiClass):
        # 类变量（M23.3）：`类名.计数 = 5`。写在类自己的类变量表里，
        # 不影响已造出来的实例字段。
        obj.class_vars[attr] = value
        return
    raise RunTypeError(f"不能给「{obj}」的属性「{attr}」赋值",
                       line=line, col=col, filename=filename)


def get_item(obj, index, *, line=None, col=None, filename="<输入>"):
    if isinstance(index, slice) and not isinstance(obj, (list, tuple, str)):
        # 字典等类型不支持切片：报清楚「什么才能切片」，别漏出
        # `unhashable type: 'slice'` 这种英文（M30，与两个宿主一致）
        raise RunTypeError(
            f"「{type_name(obj)}」不能切片，切片需要列表或文本",
            line=line, col=col, filename=filename)
    try:
        return obj[index]
    except (IndexError, KeyError, TypeError) as e:
        raise translate_python_exception(e, line=line, col=col,
                                         filename=filename)


def make_slice(start, stop, step, *, line=None, col=None,
               filename="<输入>"):
    """构造切片（M18.1）：`列表[起:止:步长]` 的三个分量转成 Python slice。

    分量可以是 None（省略），也可以是整数或空（表示不指定）。
    """
    def _norm(v):
        # 空/None → None；其它值原样（整数、表达式结果）
        if v is None:
            return None
        return v
    step = _norm(step)
    if step == 0:
        # 先拦下：Python 的原文是英文 `slice step cannot be zero`（M30）
        raise RunValueError("切片的步长不能为 0",
                            line=line, col=col, filename=filename)
    return slice(_norm(start), _norm(stop), step)


def set_item(obj, index, value, *, line=None, col=None,
             filename="<输入>"):
    try:
        obj[index] = value
    except (IndexError, KeyError, TypeError, ValueError) as e:
        # ValueError：切片赋值长度不匹配（`d[::2] = [1,2]` 而目标有 3 个位置），
        # 不接住的话会漏成英文报错。
        raise translate_python_exception(e, line=line, col=col,
                                         filename=filename)


def _attr_hint(obj: Any, attr: str) -> str:
    names = [n for n in dir(obj) if not n.startswith("_") and n.isidentifier()]
    if not names:
        return ""
    shown = "、".join(names[:8]) + ("…" if len(names) > 8 else "")
    return f"可用的属性或方法：{shown}"


class _BoundMethod:
    """基石对象方法的绑定结果（如「列表.追加」）。

    记录取属性时的行列，报错位置与树遍历解释器保持一致。
    """

    __slots__ = ("impl", "obj", "name", "line", "col", "filename")

    #: 同上：别把 `_BoundMethod` 漏给用户（M34）
    _jishi_type_name = "内建方法"

    def __init__(self, impl, obj, name, line, col, filename):
        self.impl = impl
        self.obj = obj
        self.name = name
        self.line = line
        self.col = col
        self.filename = filename

    def __call__(self, *args):
        try:
            return self.impl(self.obj, list(args), self)
        except JishiError:
            raise
        except Exception as e:
            raise translate_python_exception(
                e, line=self.line, col=self.col, filename=self.filename)

    def __repr__(self):
        return f"<方法 {self.name}>"


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------

def call_value(fn, args, kwargs=None, *, line=None, col=None,
               filename="<输入>"):
    """调用一个非基石函数的可调用对象（内建 / 标准库 / Python 桥接）。"""
    if isinstance(fn, _Builtin):
        fn = fn.fn
    if isinstance(fn, Module):
        raise SemanticFuncError(
            f"模块「{fn.name}」不能直接调用，要用「{fn.name}.函数名(...)」",
            line=line, col=col, filename=filename)
    if isinstance(fn, JishiClass):
        # 实例化：新建 类(...) → 造实例，若定义了「初始化」则调用构造方法
        return make_instance(fn, args, kwargs or {}, line=line, col=col,
                             filename=filename)
    if callable(fn):
        try:
            return fn(*args, **(kwargs or {}))
        except JishiError as e:
            # 内建/标准库抛的中文错误常常不带行列（它们不知道调用点在哪），
            # 这里补上调用位置，报错才能指回用户写的那一行（M23.1）。
            if e.line is None and line is not None:
                e.line = line
                e.col = col
                if not e.filename or e.filename == "<输入>":
                    e.filename = filename
            raise
        except Exception as e:
            raise translate_python_exception(e, line=line, col=col,
                                             filename=filename)
    raise RunNotCallableError(f"「{fn}」不能调用", line=line, col=col,
                              filename=filename,
                              hint="只有函数或带括号的对象才能调用")


def make_instance(cls: JishiClass, args: list, kwargs: dict, *,
                  line=None, col=None, filename="<输入>") -> JishiInstance:
    """造一个类实例，并调用「初始化」构造方法（若定义了）。"""
    inst = JishiInstance(cls)
    init = cls.methods.get("初始化")
    if init is not None:
        init(inst, *args, **kwargs)
    elif args or kwargs:
        raise RunTypeError(
            f"类「{cls.name}」没有构造方法「初始化」，不能传参数",
            line=line, col=col, filename=filename)
    return inst


# ---------------------------------------------------------------------------
# 容器构造
# ---------------------------------------------------------------------------

def unpack_values(value, n: int, star_index=None, *, line=None, col=None,
                  filename="<输入>"):
    """解包赋值的共享语义（树遍历 / 字节码 VM / C 宿主回调三处共用）。

    右值必须是列表、元组、集合或文本（含 Python 桥接返回的 tuple）。

    - 无星号（`star_index is None`）：长度必须**精确匹配** n；
    - 有星号（M25，`令 甲, *余 = …`）：位置 ``star_index`` 收集「其余」成
      列表（可以为空），其余 n-1 个位置取首尾对应的单个元素，
      因此要求长度 >= n - 1。

    返回长度为 n 的列表，第 ``star_index`` 项是列表。
    """
    if not isinstance(value, (list, tuple, str, JishiSet)):
        raise RunTypeError(
            f"解包赋值需要列表，得到了「{type_name(value)}」",
            line=line, col=col, filename=filename,
            hint="列表、文本、集合都能解包（字典解出来的是键）")
    if isinstance(value, dict):
        seq = list(value.keys())
    else:
        seq = list(value)
    if star_index is None:
        if len(seq) != n:
            raise RunValueError(
                f"解包需要 {n} 个值，实际有 {len(seq)} 个",
                line=line, col=col, filename=filename,
                hint="想「收剩下的」可以写「*余」，例如 令 甲, *余 = 列表")
        return seq

    # 带星号：星号前有 star_index 个固定元素，星号后有 n-1-star_index 个
    nhead = star_index
    ntail = n - 1 - star_index
    if len(seq) < nhead + ntail:
        raise RunValueError(
            f"解包至少需要 {nhead + ntail} 个值，实际有 {len(seq)} 个",
            line=line, col=col, filename=filename)
    head = seq[:nhead]
    tail = seq[len(seq) - ntail:] if ntail > 0 else []
    middle = seq[nhead:len(seq) - ntail] if ntail > 0 else seq[nhead:]
    return head + [middle] + tail


def build_dict(pairs, *, line=None, col=None, filename="<输入>"):
    """pairs 为 [(键, 值), ...]。"""
    out = {}
    for k, v in pairs:
        try:
            hash(k)
        except TypeError:
            raise RunTypeError(f"「{k}」不能作为字典的键", line=line, col=col,
                               filename=filename)
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 导入守卫（M9.1 沙箱）：沙箱运行器设置此回调，do_import 在真正导入前先询问。
# 回调签名 guard(name, from_python, line, col, filename) -> None；
# 拒绝时自行 raise（JishiError）。
# ---------------------------------------------------------------------------

_IMPORT_GUARD = None


def do_import(name: str, from_python: bool = False, from_local: bool = False,
              *, line=None, col=None, filename="<输入>",
              alias: Optional[str] = None) -> Module:
    if _IMPORT_GUARD is not None:
        _IMPORT_GUARD(name, from_python, from_local, line, col, filename)
    if from_local:
        return _import_local_package(name, line=line, col=col,
                                     filename=filename)
    if from_python:
        return _import_python(name, line=line, col=col, filename=filename)
    try:
        mod = importlib.import_module(f"jishi.stdlib.{name}")
    except ImportError:
        raise SemanticFuncError(
            f"没有找到标准库模块「{name}」", line=line, col=col,
            filename=filename,
            hint="试试：导入 随机 / 导入 数学 / 导入 时间 / 导入 文本")
    attrs = {k: v for k, v in vars(mod).items() if not k.startswith("_")}
    # 有状态模块（`日志` / `测试`）声明这个钩子：**每次导入造一份新状态**。
    # 否则状态挂在模块级全局上，同一进程里跑第二个执行器（对拍、测试、
    # REPL 里连跑两段）会串——M41 的五路对拍测试就是这么发现的。
    hook = getattr(mod, "__基石新建__", None)
    if hook is not None:
        attrs = hook()
    # 用了别名就不会遮蔽内建，不提示（M18.4）
    if alias is None:
        _warn_shadow_builtin(name)
    return Module(name, attrs)


#: 已提示过「模块名遮蔽内建」的名字（M18.4，避免重复刷屏）
_warned_shadow: set = set()


def _warn_shadow_builtin(name: str) -> None:
    """标准库模块名与内建函数同名时，打印一次中文提示（M18.4）。

    例如 `导入 文本` 会遮蔽内建转换函数 `文本(...)`——模块「文本」被绑定到
    名字「文本」，之后 `文本(123)` 会报「模块不能直接调用」。
    """
    if name in _warned_shadow:
        return
    builtin_names = set(new_builtins().keys())
    if name in builtin_names:
        _warned_shadow.add(name)
        import sys as _sys
        print(
            f"提示：「{name}」既是标准库模块名，又是内建函数名。"
            f"导入后，内建的 {name}(...) 会被模块「{name}」遮蔽。"
            f"如需两者都用，建议用别名导入：导入 {name} 为 别的名字",
            file=_sys.stderr)


# ---------------------------------------------------------------------------
# 本地包（M10.2）：`导入 名字 从 本地包`，从 .jishi/packages/ 加载
# ---------------------------------------------------------------------------

def _package_dirs() -> list[str]:
    """本地包搜索目录（先项目后全局）。"""
    import os
    dirs = []
    cwd = os.path.join(os.getcwd(), ".jishi", "packages")
    home = os.path.join(os.path.expanduser("~"), ".jishi", "packages")
    for d in (cwd, home):
        if d not in dirs:
            dirs.append(d)
    return dirs


def _find_pkg_dir(name: str) -> Optional[str]:
    """在本地包搜索目录里找包目录，找不到返回 None。"""
    import os
    for base in _package_dirs():
        cand = os.path.join(base, name)
        if os.path.isdir(cand):
            return cand
    return None


def _read_pkg_meta(pkg_dir: str, name: str, *, line=None, col=None,
                   filename="<输入>") -> dict:
    """读某包目录里的 包.json 并校验「名字」一致，返回元信息 dict。"""
    import json
    import os

    meta = {}
    meta_path = os.path.join(pkg_dir, "包.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise SemanticFuncError(
                f"本地包「{name}」的 包.json 读不了：{e}",
                line=line, col=col, filename=filename)
    if meta.get("名字") and meta["名字"] != name:
        raise SemanticFuncError(
            f"包目录「{name}」里的 包.json 声明名字为「{meta['名字']}」，不一致",
            line=line, col=col, filename=filename)
    return meta


# 正在导入的包栈（检测循环依赖，如「甲 依赖 乙，乙 依赖 甲」）
_IMPORT_STACK: list[str] = []


def _import_local_package(name: str, *, line=None, col=None,
                          filename="<输入>") -> Module:
    """从本地包缓存目录加载一个基石包（含依赖解析，M10.2）。

    包结构：`.jishi/packages/名字/` 下含 `包.json`（名字/版本/依赖/入口/描述/
    许可）与若干 `.jsh` 模块。执行后把「顶层定义的名字」作为包成员导出。

    入口（包.json 的「入口」字段，M14.1）：若声明了入口模块，则只执行该
    模块（便于把「内部实现模块」与「对外主模块」分开）；否则向后兼容地
    执行包内所有 .jsh 模块。

    依赖（包.json 的「依赖」字段，名字 → 版本约束）：先递归加载依赖包，
    校验其版本满足约束，再把依赖包对象注入到本包模块的全局命名空间，
    本包的 .jsh 模块里可直接用 `依赖名.成员` 而无需重复导入。
    """
    import os

    pkg_dir = _find_pkg_dir(name)
    if pkg_dir is None:
        raise SemanticFuncError(
            f"没有找到本地包「{name}」", line=line, col=col,
            filename=filename,
            hint=f"请把包放到 .jishi/packages/{name}/ 目录（含 包.json 与 .jsh 模块）")

    if name in _IMPORT_STACK:
        chain = " → ".join(_IMPORT_STACK + [name])
        raise SemanticFuncError(
            f"检测到循环依赖：{chain}", line=line, col=col, filename=filename)

    meta = _read_pkg_meta(pkg_dir, name, line=line, col=col, filename=filename)

    # 递归解析依赖，并做版本约束校验
    from .packages import version_satisfies

    deps: dict[str, Any] = {}
    for dep_name, constraint in (meta.get("依赖") or {}).items():
        _IMPORT_STACK.append(name)
        try:
            dep_mod = _import_local_package(
                dep_name, line=line, col=col, filename=filename)
        finally:
            _IMPORT_STACK.pop()
        dep_dir = _find_pkg_dir(dep_name)
        dep_meta = _read_pkg_meta(dep_dir, dep_name, line=line, col=col,
                                  filename=filename) if dep_dir else {}
        dep_version = dep_meta.get("版本") or "0.0.0"
        if not version_satisfies(dep_version, constraint):
            raise SemanticFuncError(
                f"包「{name}」依赖「{dep_name}」的版本要满足 {constraint!r}，"
                f"但装的是 {dep_version}",
                line=line, col=col, filename=filename,
                hint=(f"装个符合要求的版本重新试试；如果多个包对「{dep_name}」"
                      f"的要求互相打架（钻石依赖），跑 `jishi 检查` 能看到完整链条"))
        deps[dep_name] = dep_mod

    # 决定要执行的模块：有「入口」只跑入口，否则跑全部 .jsh
    from .interpreter import Interpreter
    from .parser import parse
    from .tokenizer import tokenize

    entry = (meta.get("入口") or "").strip()
    jsh_files = sorted(f for f in os.listdir(pkg_dir) if f.endswith(".jsh"))
    if entry:
        # 入口可带或不带 .jsh 后缀
        if not entry.endswith(".jsh"):
            entry += ".jsh"
        if entry not in jsh_files:
            raise SemanticFuncError(
                f"本地包「{name}」的 包.json 声明入口「{meta.get('入口')}」，"
                f"但包里没有这个模块文件",
                line=line, col=col, filename=filename)
        jsh_files = [entry]
    if not jsh_files:
        raise SemanticFuncError(
            f"本地包「{name}」里没有 .jsh 模块文件",
            line=line, col=col, filename=filename)

    builtin_names = set(new_builtins().keys())
    exports: dict[str, Any] = {}
    for fn in jsh_files:
        path = os.path.join(pkg_dir, fn)
        with open(path, encoding="utf-8") as f:
            source = f.read()
        interp = Interpreter(filename=path)
        # 注入依赖包，供本包模块直接引用（`依赖名.成员`）
        for dep_name, dep_mod in deps.items():
            interp.globals.set(dep_name, dep_mod)
        tokens = tokenize(source, path)
        lines = source.replace("\r\n", "\n").split("\n")
        program = parse(tokens, lines, path)
        interp.run(program)
        for k, v in interp.globals.vars.items():
            if k not in builtin_names and not k.startswith("_"):
                exports[k] = v

    return Module(name, exports)


def _import_python(name: str, *, line=None, col=None,
                   filename="<输入>") -> Module:
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError:
        raise SemanticFuncError(
            f"Python 里没有模块「{name}」", line=line, col=col,
            filename=filename,
            hint="先确认模块名拼写正确；第三方库需要先在 Python 环境里安装"
                 "（pip 安装后即可直接导入）")
    except ImportError as e:
        raise RunError(f"模块「{name}」导入失败：{e}", line=line, col=col,
                       filename=filename)
    attrs = {k: v for k, v in vars(mod).items() if not k.startswith("_")}
    return Module(name, attrs)


# ---------------------------------------------------------------------------
# 对象方法系统（M1）：列表 / 字典 / 字符串
# 方法统一签名：impl(obj, args, node) -> Any
# ---------------------------------------------------------------------------

def _need_args(name: str, args: list, node, lo: int, hi: int):
    if not (lo <= len(args) <= hi):
        need = f"{lo} 个" if lo == hi else f"{lo} 到 {hi} 个"
        raise RunTypeError(f"方法「{name}」需要 {need}参数，但传了 {len(args)} 个",
                           line=node.line, col=node.col)


# -- 列表方法 --------------------------------------------------------------

def _m_list_append(obj, args, node):
    _need_args("追加", args, node, 1, 1)
    obj.append(args[0])


def _m_list_insert(obj, args, node):
    _need_args("插入", args, node, 2, 2)
    obj.insert(args[0], args[1])


def _m_list_remove(obj, args, node):
    _need_args("移除", args, node, 1, 1)
    try:
        obj.remove(args[0])
    except ValueError:
        raise RunValueError(f"列表里没有「{args[0]}」，没法移除",
                            line=node.line, col=node.col)


def _m_list_pop(obj, args, node):
    _need_args("弹出", args, node, 0, 1)
    try:
        if args:
            return obj.pop(args[0])
        return obj.pop()
    except IndexError:
        raise RunValueError("列表是空的，没有东西可以弹出",
                            line=node.line, col=node.col)


def _m_list_sort(obj, args, node):
    _need_args("排序", args, node, 0, 0)
    try:
        obj.sort()
    except TypeError:
        raise RunTypeError("列表里的元素类型不同，没法排序",
                           line=node.line, col=node.col)


def _m_list_reverse(obj, args, node):
    _need_args("反转", args, node, 0, 0)
    obj.reverse()


def _m_list_clear(obj, args, node):
    _need_args("清空", args, node, 0, 0)
    obj.clear()


def _m_list_index(obj, args, node):
    _need_args("索引", args, node, 1, 1)
    try:
        return obj.index(args[0])
    except ValueError:
        raise RunValueError(f"列表里没有「{args[0]}」，找不到它的位置",
                            line=node.line, col=node.col)


def _m_list_count(obj, args, node):
    _need_args("计数", args, node, 1, 1)
    return obj.count(args[0])


def _as_func(fn, name, node):
    """检查一个参数确实是可调用的，并返回它（报错说清是哪个方法的参数）。"""
    if not callable(fn):
        raise RunTypeError(
            f"「{name}」需要一个函数作为参数，但给的是「{type_name(fn)}」",
            line=node.line, col=node.col,
            hint="可以传具名函数（函数 加倍(x)：…），"
                 "或匿名函数（函数(x)：x * 2）")
    return fn


def _m_list_map(obj, args, node):
    """映射（M24.4）：把每个元素过一遍函数，返回新列表。"""
    _need_args("映射", args, node, 1, 1)
    f = _as_func(args[0], "映射", node)
    return [call_value(f, [x], line=node.line, col=node.col)
            for x in obj]


def _m_list_filter(obj, args, node):
    """过滤（M24.4）：保留函数返回「真」的元素，返回新列表。"""
    _need_args("过滤", args, node, 1, 1)
    f = _as_func(args[0], "过滤", node)
    return [x for x in obj
            if truthy(call_value(f, [x], line=node.line, col=node.col))]


def _m_list_sort_by(obj, args, node):
    """排序按（M24.4）：按函数算出的「排序键」从小到大排，返回新列表。"""
    _need_args("排序按", args, node, 1, 1)
    f = _as_func(args[0], "排序按", node)
    try:
        return sorted(obj, key=lambda x: call_value(
            f, [x], line=node.line, col=node.col))
    except TypeError:
        raise RunTypeError(
            "「排序按」算出来的排序键没法互相比较",
            line=node.line, col=node.col,
            hint="排序键要都是数字，或都是文本，不能混着来")


def _m_list_reduce(obj, args, node):
    """归约（M24.4）：把列表收敛成一个值。归约(函数(累计, 元素)：…, 初值)。"""
    _need_args("归约", args, node, 2, 2)
    f = _as_func(args[0], "归约", node)
    acc = args[1]
    for x in obj:
        acc = call_value(f, [acc, x], line=node.line, col=node.col)
    return acc


def _m_list_contains(obj, args, node):
    _need_args("包含", args, node, 1, 1)
    return args[0] in obj


# -- 字典方法 --------------------------------------------------------------

def _m_dict_get(obj, args, node):
    _need_args("获取", args, node, 1, 2)
    default = args[1] if len(args) == 2 else None
    return obj.get(args[0], default)


def _m_dict_keys(obj, args, node):
    _need_args("键", args, node, 0, 0)
    return list(obj.keys())


def _m_dict_values(obj, args, node):
    _need_args("值", args, node, 0, 0)
    return list(obj.values())


def _m_dict_contains(obj, args, node):
    _need_args("包含", args, node, 1, 1)
    return args[0] in obj


def _m_dict_update(obj, args, node):
    _need_args("更新", args, node, 1, 1)
    if not isinstance(args[0], dict):
        raise RunTypeError(f"「更新」需要一个字典参数，但传了「{args[0]}」",
                           line=node.line, col=node.col)
    obj.update(args[0])


def _m_dict_merge(obj, args, node):
    """返回**新字典**：先放自己、再放参数（同名键以参数为准）。

    与「更新」的分工：`更新` 就地改自己（返回空），`合并` 双方都不动、
    返回一个新字典——写数据流水线（`令 全 = 默认配置.合并(用户配置)`）时更顺手。
    """
    _need_args("合并", args, node, 1, 1)
    if not isinstance(args[0], dict):
        raise RunTypeError(
            f"「合并」需要一个字典参数，但传了「{args[0]}」（{type_name(args[0])}）",
            line=node.line, col=node.col)
    merged = dict(obj)
    merged.update(args[0])
    return merged


def _m_dict_pop(obj, args, node):
    _need_args("弹出", args, node, 1, 2)
    if len(args) == 2:
        return obj.pop(args[0], args[1])
    try:
        return obj.pop(args[0])
    except KeyError:
        raise RunKeyError(f"字典里没有键「{args[0]}」",
                          line=node.line, col=node.col)


def _m_dict_clear(obj, args, node):
    _need_args("清空", args, node, 0, 0)
    obj.clear()


# -- 字符串方法 ------------------------------------------------------------

def _m_str_split(obj, args, node):
    _need_args("拆分", args, node, 0, 1)
    sep = args[0] if args else None
    return obj.split(sep)


def _m_str_replace(obj, args, node):
    _need_args("替换", args, node, 2, 2)
    return obj.replace(args[0], args[1])


def _m_str_find(obj, args, node):
    _need_args("查找", args, node, 1, 1)
    return obj.find(args[0])


def _m_str_upper(obj, args, node):
    _need_args("大写", args, node, 0, 0)
    return obj.upper()


def _m_str_lower(obj, args, node):
    _need_args("小写", args, node, 0, 0)
    return obj.lower()


def _m_str_strip(obj, args, node):
    _need_args("去空白", args, node, 0, 0)
    return obj.strip()


def _m_str_startswith(obj, args, node):
    _need_args("开头是", args, node, 1, 1)
    return obj.startswith(args[0])


def _m_str_endswith(obj, args, node):
    _need_args("结尾是", args, node, 1, 1)
    return obj.endswith(args[0])


def _m_str_contains(obj, args, node):
    _need_args("包含", args, node, 1, 1)
    return args[0] in obj


def _m_str_to_int(obj, args, node):
    _need_args("转整数", args, node, 0, 0)
    try:
        return int(obj)
    except ValueError:
        raise RunValueError(f"「{obj}」不能转成整数",
                            line=node.line, col=node.col)


def _m_str_to_float(obj, args, node):
    _need_args("转小数", args, node, 0, 0)
    try:
        return float(obj)
    except ValueError:
        raise RunValueError(f"「{obj}」不能转成小数",
                            line=node.line, col=node.col)


LIST_METHODS = {
    "追加": _m_list_append,
    "插入": _m_list_insert,
    "移除": _m_list_remove,
    "弹出": _m_list_pop,
    "排序": _m_list_sort,
    "反转": _m_list_reverse,
    "清空": _m_list_clear,
    "索引": _m_list_index,
    "计数": _m_list_count,
    "包含": _m_list_contains,
    "映射": _m_list_map,
    "过滤": _m_list_filter,
    "排序按": _m_list_sort_by,
    "归约": _m_list_reduce,
}

# -- 集合方法（M24.1）-----------------------------------------------------

def _m_set_add(obj, args, node):
    _need_args("添加", args, node, 1, 1)
    obj._add(args[0])


def _m_set_remove(obj, args, node):
    _need_args("移除", args, node, 1, 1)
    if args[0] not in obj:
        raise RunValueError(f"集合里没有「{args[0]}」，没法移除",
                            line=node.line, col=node.col,
                            hint="不确定有没有时用「丢弃」，它不会报错")
    obj._d.pop(args[0], None)


def _m_set_discard(obj, args, node):
    _need_args("丢弃", args, node, 1, 1)
    try:
        obj._d.pop(args[0], None)
    except TypeError:
        pass


def _m_set_contains(obj, args, node):
    _need_args("包含", args, node, 1, 1)
    return args[0] in obj


def _set_pair(op_name, fn):
    """生成「并集/交集/差集/对称差」四个方法的实现（参数接受任意可迭代）。"""
    def impl(obj, args, node):
        _need_args(op_name, args, node, 1, 1)
        other = _as_iterable(args[0], f"「{op_name}」的另一个集合",
                             line=node.line, col=node.col)
        return fn(obj, other)
    return impl


def _m_set_clear(obj, args, node):
    _need_args("清空", args, node, 0, 0)
    obj._d.clear()


def _m_set_to_list(obj, args, node):
    _need_args("转列表", args, node, 0, 0)
    return list(obj)


def _m_set_copy(obj, args, node):
    _need_args("复制", args, node, 0, 0)
    return JishiSet(obj)


SET_METHODS = {
    "添加": _m_set_add,
    "移除": _m_set_remove,
    "丢弃": _m_set_discard,
    "包含": _m_set_contains,
    "并集": _set_pair("并集", lambda a, b: JishiSet(list(a) + list(b))),
    "交集": _set_pair("交集", lambda a, b: JishiSet([x for x in a if x in b])),
    "差集": _set_pair("差集", lambda a, b: JishiSet([x for x in a if x not in b])),
    "对称差": _set_pair(
        "对称差",
        lambda a, b: JishiSet([x for x in a if x not in b]
                              + [x for x in b if x not in a])),
    "清空": _m_set_clear,
    "转列表": _m_set_to_list,
    "复制": _m_set_copy,
}


# -- 文件（M26.2）----------------------------------------------------------

class JishiFile:
    """文件对象：`打开(路径, 模式)` 的返回值。

    刻意做成「真句柄」而不是一次读进内存：大文件能按行遍历、不必整个装进
    列表，而且「用完要关」这件事才有意义——这正是上下文管理器存在的理由。

    它自带 `关闭`（所以能当上下文管理器）与迭代器协议
    （所以能 `遍历 行 在 f`）。打印时显示路径与状态，便于排错。
    """

    __slots__ = ("handle", "path", "mode", "closed")

    def __init__(self, handle, path: str, mode: str):
        self.handle = handle
        self.path = path
        self.mode = mode
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        line = self.handle.readline()
        if line == "":
            raise StopIteration
        return line.rstrip("\r\n")

    def __repr__(self):
        state = "已关闭" if self.closed else self.mode
        return f"<文件 {self.path}（{state}）>"


def _file_handle(obj, node, name: str):
    """取底层句柄；文件已关闭时给中文报错（而不是漏出 Python 的 ValueError）。"""
    if obj.closed:
        raise RunValueError(
            f"文件「{obj.path}」已经关闭了，不能再「{name}」",
            line=node.line, col=node.col,
            hint="「用 打开(…) 为 f：」的块一结束，文件就自动关了；"
                 "该读该写的要在块里做完")
    return obj.handle


def _m_file_read(obj, args, node):
    _need_args("读", args, node, 0, 1)
    h = _file_handle(obj, node, "读")
    return h.read(args[0] if args else -1)


def _m_file_read_line(obj, args, node):
    _need_args("读行", args, node, 0, 0)
    h = _file_handle(obj, node, "读行")
    line = h.readline()
    if line == "":
        return None                     # 读到末尾：给「空」，便于判断
    return line.rstrip("\r\n")


def _m_file_read_lines(obj, args, node):
    _need_args("读所有行", args, node, 0, 0)
    h = _file_handle(obj, node, "读所有行")
    return [ln.rstrip("\r\n") for ln in h]


def _m_file_write(obj, args, node):
    _need_args("写", args, node, 1, 1)
    h = _file_handle(obj, node, "写")
    text = args[0]
    if not isinstance(text, str):
        raise RunTypeError(
            f"「写」需要文本，得到了「{type_name(text)}」",
            line=node.line, col=node.col,
            hint="先用 文本(…) 转一下，例如 f.写(文本(42))")
    h.write(text)
    return None


def _m_file_write_line(obj, args, node):
    _need_args("写行", args, node, 1, 1)
    h = _file_handle(obj, node, "写行")
    text = args[0]
    if not isinstance(text, str):
        raise RunTypeError(
            f"「写行」需要文本，得到了「{type_name(text)}」",
            line=node.line, col=node.col,
            hint="先用 文本(…) 转一下")
    h.write(text + "\n")
    return None


def _m_file_close(obj, args, node):
    _need_args("关闭", args, node, 0, 0)
    if not obj.closed:                  # 重复关闭不报错，幂等更省心
        obj.handle.close()
        obj.closed = True
    return None


def _m_file_flush(obj, args, node):
    _need_args("刷新", args, node, 0, 0)
    _file_handle(obj, node, "刷新").flush()
    return None


def _m_file_tell(obj, args, node):
    _need_args("位置", args, node, 0, 0)
    return _file_handle(obj, node, "位置").tell()


def _m_file_seek(obj, args, node):
    _need_args("定位", args, node, 1, 1)
    _file_handle(obj, node, "定位").seek(args[0])
    return None


FILE_METHODS = {
    "读": _m_file_read,
    "读行": _m_file_read_line,
    "读所有行": _m_file_read_lines,
    "写": _m_file_write,
    "写行": _m_file_write_line,
    "关闭": _m_file_close,
    "刷新": _m_file_flush,
    "位置": _m_file_tell,
    "定位": _m_file_seek,
}


# -- 精确小数方法（M26.3）--------------------------------------------------

def _m_dec_round(obj, args, node):
    _need_args("舍入", args, node, 0, 1)
    digits = args[0] if args else 2
    if isinstance(digits, bool) or not isinstance(digits, int):
        raise RunTypeError(
            f"「舍入」的位数要是整数，得到了「{type_name(digits)}」",
            line=node.line, col=node.col,
            hint="例如 钱.舍入(2) 保留两位；负数表示舍到十位、百位")
    # 金额场景用「四舍五入」（ROUND_HALF_UP），而不是 Python 默认的
    # 银行家舍入（0.5 舍到偶数）——后者更「标准」但不合用户直觉
    q = decimal.Decimal(1).scaleb(-digits)
    rounded = obj.value.quantize(q, rounding=decimal.ROUND_HALF_UP)
    # 舍到十位/百位（digits 为负）时 Decimal 会写成 1.2E+3，
    # 换回普通写法（1200）——科学计数法在金额场景里没人想看
    return JishiDecimal(decimal.Decimal(format(rounded, "f")))


def _m_dec_abs(obj, args, node):
    _need_args("绝对值", args, node, 0, 0)
    return JishiDecimal(abs(obj.value))


def _m_dec_str(obj, args, node):
    _need_args("转文本", args, node, 0, 0)
    return str(obj.value)


DECIMAL_METHODS = {
    "舍入": _m_dec_round,
    "绝对值": _m_dec_abs,
    "转文本": _m_dec_str,
}

DICT_METHODS = {
    "获取": _m_dict_get,
    "键": _m_dict_keys,
    "值": _m_dict_values,
    "包含": _m_dict_contains,
    "更新": _m_dict_update,
    "合并": _m_dict_merge,
    "弹出": _m_dict_pop,
    "清空": _m_dict_clear,
}

STR_METHODS = {
    "拆分": _m_str_split,
    "替换": _m_str_replace,
    "查找": _m_str_find,
    "大写": _m_str_upper,
    "小写": _m_str_lower,
    "去空白": _m_str_strip,
    "开头是": _m_str_startswith,
    "结尾是": _m_str_endswith,
    "包含": _m_str_contains,
    "转整数": _m_str_to_int,
    "转小数": _m_str_to_float,
}


def method_table(obj) -> Optional[dict]:
    if isinstance(obj, list):
        return LIST_METHODS
    if isinstance(obj, JishiSet):
        return SET_METHODS
    if isinstance(obj, JishiFile):
        return FILE_METHODS
    if isinstance(obj, JishiDecimal):
        return DECIMAL_METHODS
    if isinstance(obj, dict):
        return DICT_METHODS
    if isinstance(obj, str):
        return STR_METHODS
    return None


# ---------------------------------------------------------------------------
# 值的中文显示（REPL 回显用）
# ---------------------------------------------------------------------------

def jishi_repr(v, *, top: bool = False) -> str:
    """把基石的值格式化成可读文本 —— **值→文本的唯一实现**（M27）。

    以前这件事有两份几乎一样的代码（这里的 `jishi_repr` 与 REPL 里的
    `_repr_jishi`），于是慢慢漂移：`打印(空)` 走 Python 的 `str()` 给出
    `None`、布尔给出 `True`/`False`，而 REPL 回显与 `类型()` 用的是中文
    「空/真/假」。同一个值有两种写法，用户得猜哪个算数。

    现在只有一个函数、一个语义：

    - ``top=True``：直接展示这个值（`打印(值)`、`文本(值)`、插值 `{值}`）。
      文本**不加引号**，因为用户要的就是那串字。
    - ``top=False``：嵌套在容器里或 REPL 回显。文本**加引号**，这样
      `打印(["甲", 甲变量])` 能看出哪个是文本；与 Python 的 `print` /
      `repr` 分工一致。
    - 空 / 真 / 假 一律中文化，与源码里的写法一致。
    - 字典用半角 `: ` 分隔：打印出来的形式要能**直接抄回源码**
      （基石源码写 `{"甲": 1}`），对教学语言这点比好看更重要。
    """
    if v is None:
        return "空"
    if v is True:                       # 必须用 is：True == 1，先判布尔
        return "真"
    if v is False:
        return "假"
    if isinstance(v, str):
        return v if top else repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(jishi_repr(x) for x in v) + "]"
    if isinstance(v, tuple):
        # 元组只可能来自 Python 桥接的返回值（基石源码没有元组字面量）
        return "(" + ", ".join(jishi_repr(x) for x in v) + ")"
    if isinstance(v, dict):
        return "{" + ", ".join(
            f"{jishi_repr(k)}: {jishi_repr(x)}" for k, x in v.items()) + "}"
    # 其余（自定义对象、文件、精确小数、函数、类、模块…）都有自己的
    # __repr__/__str__：自定义对象在这里会走到用户定义的「文本」方法。
    return str(v)
