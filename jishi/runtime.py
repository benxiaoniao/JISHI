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

import importlib
from typing import Any, Optional

from .errors import (
    EXCEPTION_TYPES,
    JishiError,
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
# 内建函数
# ---------------------------------------------------------------------------

def _b_print(*args):
    print(*args)
    return None


def _b_input(prompt: str = ""):
    return input(prompt)


def _b_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise RunTypeError(f"不能把「{v}」转成整数")


def _b_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise RunTypeError(f"不能把「{v}」转成小数")


def _b_str(v):
    return str(v)


def _b_len(v):
    try:
        return len(v)
    except TypeError:
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


def _b_max(*args):
    # M18.3：单参数且是列表/元组 → max(序列)；多参数 → max(参数们)。
    # 兼容 最大([a,b]) 与 最大(a,b)、最大(a,b,c)。
    try:
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            return max(args[0])
        return max(args)
    except (TypeError, ValueError):
        raise RunTypeError(f"「{args}」不能求最大值，需要一个非空列表或若干数字")


def _b_min(*args):
    try:
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            return min(args[0])
        return min(args)
    except (TypeError, ValueError):
        raise RunTypeError(f"「{args}」不能求最小值，需要一个非空列表或若干数字")


def _b_sum(v):
    try:
        return sum(v)
    except (TypeError, ValueError):
        raise RunTypeError(f"「{v}」不能求和，需要一个全是数字的列表")


def _b_type(v):
    return type_name(v)


def _b_reverse(v):
    try:
        if isinstance(v, str):
            return v[::-1]
        return list(reversed(v))
    except TypeError:
        raise RunTypeError(f"「{v}」不能反转，需要列表或文本")


def new_builtins() -> dict[str, Any]:
    """返回一份全新的内建函数表（每个执行器实例一份）。"""
    return {
        "打印": _Builtin("打印", _b_print, "输出内容到屏幕"),
        "输入": _Builtin("输入", _b_input, "读取用户输入的一行文本"),
        "整数": _Builtin("整数", _b_int, "把值转成整数"),
        "小数": _Builtin("小数", _b_float, "把值转成小数"),
        "文本": _Builtin("文本", _b_str, "把值转成文本"),
        "长度": _Builtin("长度", _b_len, "取列表、文本等的长度"),
        "范围": _Builtin(
            "范围", _b_range,
            "生成一段整数序列（范围(5)、范围(1, 5)、范围(1, 5, 2)）"),
        "最大": _Builtin("最大", _b_max, "取列表中的最大值"),
        "最小": _Builtin("最小", _b_min, "取列表中的最小值"),
        "总和": _Builtin("总和", _b_sum, "求列表中所有数的和"),
        "类型": _Builtin("类型", _b_type, "查看值的类型"),
        "反转": _Builtin("反转", _b_reverse, "反转列表或文本"),
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

def apply_binop(op: str, left, right, *, line=None, col=None,
                filename="<输入>"):
    """二元运算（+ - * / // % **）。"""
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
        try:
            return -v
        except TypeError:
            raise RunTypeError(f"「{v}」不能取负号", line=line, col=col,
                               filename=filename)
    if op == "非":
        return not truthy(v)
    raise RunError(f"不支持的一元运算符「{op}」", line=line, col=col,
                   filename=filename)


def apply_compare(op: str, left, right, *, line=None, col=None,
                  filename="<输入>"):
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
    """

    __slots__ = ("name", "methods", "base")

    def __init__(self, name: str, methods: dict[str, Any],
                 base: Optional["JishiClass"] = None):
        self.name = name
        self.base = base
        # 继承：合并基类方法表，子类同名方法覆盖
        merged: dict[str, Any] = {}
        if base is not None:
            merged.update(base.methods)
        merged.update(methods)
        self.methods = merged

    def __repr__(self):
        return f"<类 {self.name}>"


class JishiInstance:
    """基石类的实例（`新建 狗(...)` 产生）。

    fields 是「字段名 → 值」；方法从类里查（绑定实例作为「自身」首参）。
    """

    __slots__ = ("cls", "fields")

    def __init__(self, cls: JishiClass):
        self.cls = cls
        self.fields: dict[str, Any] = {}

    def __repr__(self):
        return f"<{self.cls.name} 实例>"


class _BoundUserMethod:
    """绑定到实例的方法：调用时把实例塞进第一个参数（「自身」）。"""

    __slots__ = ("fn", "instance", "name", "line", "col", "filename")

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
    if isinstance(obj, Module):
        if attr in obj.attrs:
            return obj.attrs[attr]
        raise SemanticFuncError(
            f"模块「{obj.name}」里没有「{attr}」", line=line, col=col,
            filename=filename,
            hint="可以看看模块里有哪些函数，例如：打印(dir(模块名)) 在后续版本可用")
    if isinstance(obj, JishiInstance):
        # 实例字段优先，其次类方法（绑定实例）
        if attr in obj.fields:
            return obj.fields[attr]
        if attr in obj.cls.methods:
            return _BoundUserMethod(obj.cls.methods[attr], obj, attr,
                                    line, col, filename)
        hint = f"类「{obj.cls.name}」没有字段或方法「{attr}」"
        raise RunTypeError(f"「{type_name(obj)}」没有属性「{attr}」",
                           line=line, col=col, filename=filename, hint=hint)
    if isinstance(obj, JishiClass):
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
    raise RunTypeError(f"不能给「{obj}」的属性「{attr}」赋值",
                       line=line, col=col, filename=filename)


def get_item(obj, index, *, line=None, col=None, filename="<输入>"):
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
    return slice(_norm(start), _norm(stop), _norm(step))


def set_item(obj, index, value, *, line=None, col=None,
             filename="<输入>"):
    try:
        obj[index] = value
    except (IndexError, KeyError, TypeError) as e:
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
        except JishiError:
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

def unpack_values(value, n: int, *, line=None, col=None, filename="<输入>"):
    """解包赋值的共享语义（树遍历 / 字节码 VM / C 宿主回调三处共用）。

    右值必须是列表或元组（含 Python 桥接返回的 tuple），长度必须精确匹配。
    """
    if not isinstance(value, (list, tuple)):
        raise RunTypeError(
            f"解包赋值需要列表，得到了「{type_name(value)}」",
            line=line, col=col, filename=filename)
    if len(value) != n:
        raise RunValueError(
            f"解包需要 {n} 个值，实际有 {len(value)} 个",
            line=line, col=col, filename=filename)
    return list(value)


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
}

DICT_METHODS = {
    "获取": _m_dict_get,
    "键": _m_dict_keys,
    "值": _m_dict_values,
    "包含": _m_dict_contains,
    "更新": _m_dict_update,
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
    if isinstance(obj, dict):
        return DICT_METHODS
    if isinstance(obj, str):
        return STR_METHODS
    return None


# ---------------------------------------------------------------------------
# 值的中文显示（REPL 回显用）
# ---------------------------------------------------------------------------

def jishi_repr(v) -> str:
    if v is None:
        return "空"
    if v is True:
        return "真"
    if v is False:
        return "假"
    if isinstance(v, str):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(jishi_repr(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(
            f"{jishi_repr(k)}：{jishi_repr(val)}" for k, val in v.items()) + "}"
    return str(v)
