# -*- coding: utf-8 -*-
"""基石 C 虚拟机的 ctypes 绑定与宿主回调实现（M4b）。

职责边界：
- **本文件**：加载 ``cvm/bin/jsvm.dll``（Linux 为 ``.so``）、把
  ``CompiledModule`` 序列化成 C 侧需要的扁平数组、实现 ``JsHost``
  回调表 —— 一切复杂语义（打印/文本/列表/字典/导入/Python 生态/中文报错）
  回调进 Python，委托 ``runtime.py``，与树遍历解释器共用同一份措辞。
- **C 侧（cvm/src/jsvm.c）**：只保留热路径 —— 整数/浮点算术、比较、
  跳转、局部槽读写、栈操作、基石函数调用与返回、循环计数与闭包单元。

错误协议：宿主回调出错时把 ``JishiError`` 存进 ``pending`` 并返回 -1，
C 主循环立即停止；``run_source_c`` 统一把 ``pending`` 重新抛出。
回调收到的 line/col 由指令携带，保证报错行列与树遍历逐字一致。
"""

from __future__ import annotations

import ctypes
import struct as _struct
import sys as _sys
from ctypes import (CFUNCTYPE, POINTER, Structure, Union, byref, c_char_p,
                    c_double, c_int, c_int32, c_int64, c_uint8, c_uint32,
                    c_uint64, c_void_p)
from pathlib import Path
from typing import Any, Optional

from . import opcodes as O
from . import runtime as R
from .errors import (JishiError, RunBreak, RunContinue, RunError, RunTypeError,
                     RunValueError)
from .opcodes import BINOP_TO_SYMBOL, CMPOP_TO_SYMBOL, BinOp, CmpOp, UnOp

# ---------------------------------------------------------------------------
# 动态库定位与加载
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
_BIN = _ROOT / "cvm" / "bin"
#: wheel 内预编译动态库的目录（M11：pip install 即含 C VM，无需本机 gcc）
_NATIVE = Path(__file__).resolve().parent / "_native"


def _lib_name() -> str:
    import sys

    if sys.platform == "win32":
        return "jsvm.dll"
    if sys.platform == "darwin":
        return "libjsvm.dylib"
    return "libjsvm.so"


def _lib_path() -> Path:
    import sys

    # 1. PyInstaller 打包后：DLL 解压到 _MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / _lib_name()
        if cand.exists():
            return cand
    # 2. 包内预编译（wheel 安装后，pip install 即含 C VM）
    pkg = _NATIVE / _lib_name()
    if pkg.exists():
        return pkg
    # 3. 回退：源码树开发模式（cvm/bin/，由 python cvm/build.py 构建）
    dev = _BIN / _lib_name()
    if dev.exists():
        return dev
    raise FileNotFoundError(
        f"找不到 C 虚拟机动态库（{_lib_name()}），请先运行：python cvm/build.py")


_LIB: Optional[ctypes.CDLL] = None


def load_lib() -> ctypes.CDLL:
    """加载动态库并声明函数原型（进程内只做一次）。"""
    global _LIB
    if _LIB is not None:
        return _LIB
    lib = ctypes.CDLL(str(_lib_path()))

    JPV = POINTER(JVal)  # noqa: E402  （JVal 定义在后，运行时才求值）

    lib.jsvm_create.restype = c_void_p
    lib.jsvm_create.argtypes = []
    lib.jsvm_destroy.argtypes = [c_void_p]
    lib.jsvm_set_host.argtypes = [c_void_p, c_void_p]
    lib.jsvm_load_module.restype = c_int
    lib.jsvm_load_module.argtypes = [
        c_void_p,
        JPV, c_int,          # consts, nconsts
        c_int,               # nnames
        JPV,                 # globals_in（所有权转移）
        POINTER(c_int32), c_int,   # instrs, ninstrs
        POINTER(c_int32), c_int,   # code_meta, ncodes
        POINTER(c_int32),          # code_params
        POINTER(c_int32), POINTER(c_int32), c_int,  # kw_flat, kw_off, nkw
        c_int,               # main_idx
    ]
    lib.jsvm_run.restype = c_int
    lib.jsvm_run.argtypes = [c_void_p]
    lib.jsvm_get_last.argtypes = [c_void_p, JPV]
    lib.jsvm_track_handle.argtypes = [c_void_p, c_uint32]
    lib.jsvm_func_retain.argtypes = [c_void_p, JPV]
    lib.jsvm_func_release.argtypes = [c_void_p, JPV]
    lib.jsvm_call_function.restype = c_int
    lib.jsvm_call_function.argtypes = [
        c_void_p, JPV, JPV, c_int,
        POINTER(c_int), JPV, c_int, JPV,
    ]
    lib.jsvm_new_func_from.argtypes = [c_void_p, JPV, JPV]
    lib.jsvm_retain.argtypes = [c_void_p, JPV]
    lib.jsvm_release.argtypes = [c_void_p, JPV]
    lib.jsvm_func_code.restype = c_int
    lib.jsvm_func_code.argtypes = [c_void_p, JPV]
    lib.jsvm_global_set.restype = c_int
    lib.jsvm_global_set.argtypes = [c_void_p, c_int]
    lib.jsvm_take_pending_err.argtypes = [c_void_p, JPV]

    _LIB = lib
    return lib


# ---------------------------------------------------------------------------
# 值表示：JVal（与 jsvm.c 的定义逐字段一致）
# ---------------------------------------------------------------------------

class _JValUnion(Union):
    _fields_ = [("i", c_int64), ("d", c_double), ("u", c_uint64)]


class JVal(Structure):
    _fields_ = [("tag", c_uint8), ("pad", c_uint8 * 7), ("v", _JValUnion)]


def _jnull() -> JVal:
    x = JVal(); x.tag = O.T_NULL; return x


def _jbool(b: bool) -> JVal:
    x = JVal(); x.tag = O.T_TRUE if b else O.T_FALSE; return x


#: C 虚拟机的整数是 64 位（`JVal` 刻意保持 16 字节布局：扩宽到 i128 会破坏
#: ctypes 边界与 M20 的批量转换快路径）。
_I64_MAX = (1 << 63) - 1
_I64_MIN = -(1 << 63)


def _jint(i: int) -> JVal:
    """Python 整数 → JVal 整数。

    **必须自己检查范围**：ctypes 赋给 `c_int64` 时是**静默截断**——
    `2 ** 64` 会变成 0、`i64_MAX + 1` 会变成负数。这是最危险的一类错
    （不报错但结果错），所以在这里拦下报中文错（M32）。

    注意这是 C 虚拟机特有的边界：Python 虚拟机的 int 是任意精度；
    Node 宿主用 BigInt、Rust 宿主到 i128。C 侧扩大范围要先改 `JVal` 布局，
    那会动到 ctypes 边界与批量转换的性能路径，不在本轮范围内。
    """
    if not (_I64_MIN <= i <= _I64_MAX):
        raise RunValueError(
            f"整数「{i}」超出 C 虚拟机的 64 位整数范围（约 ±9.2e18）"
            f"——Python 虚拟机的整数是任意精度，可以换那个执行器")
    x = JVal(); x.tag = O.T_INT; x.v.i = i; return x


def _jfloat(d: float) -> JVal:
    x = JVal(); x.tag = O.T_FLOAT; x.v.d = d; return x


def _jhost(h: int) -> JVal:
    x = JVal(); x.tag = O.T_HOST; x.v.u = h; return x


#: JVal 的字节大小（批量转换用）
_JVAL_SIZE = ctypes.sizeof(JVal)

#: 批量构造「全整数」JVal 数组的 struct.Struct 缓存（M20 热路径优化）。
#: 布局：uint8 tag + 7 字节填充 + int64 值，与 JVal 内存布局一致（仅小端）。
_INT_BATCH_FMT: dict[int, _struct.Struct] = {}
_LITTLE_ENDIAN = _sys.byteorder == "little"


def _int_batch_fmt(n: int) -> _struct.Struct:
    st = _INT_BATCH_FMT.get(n)
    if st is None:
        st = _struct.Struct("<" + "B7xq" * n)
        _INT_BATCH_FMT[n] = st
    return st


def _jfunc_copy(src: JVal) -> JVal:
    x = JVal()
    x.tag = src.tag
    x.v.u = src.v.u
    return x


# ---------------------------------------------------------------------------
# 宿主回调签名
# ---------------------------------------------------------------------------

_JPV = POINTER(JVal)
_IPV = POINTER(c_int)

CB_BINOP = CFUNCTYPE(c_int, c_int, _JPV, _JPV, _JPV, c_int, c_int)
CB_UNARY = CFUNCTYPE(c_int, c_int, _JPV, _JPV, c_int, c_int)
CB_COMPARE = CFUNCTYPE(c_int, c_int, _JPV, _JPV, _JPV, c_int, c_int)
CB_TRUTHY = CFUNCTYPE(c_int, _JPV, POINTER(c_int), c_int, c_int)
CB_CALL = CFUNCTYPE(c_int, _JPV, _JPV, c_int, _IPV, _JPV, c_int, _JPV,
                    c_int, c_int)
CB_GETATTR = CFUNCTYPE(c_int, _JPV, c_int, _JPV, c_int, c_int)
CB_SETATTR = CFUNCTYPE(c_int, _JPV, c_int, _JPV, c_int, c_int)
CB_GETITEM = CFUNCTYPE(c_int, _JPV, _JPV, _JPV, c_int, c_int)
CB_SETITEM = CFUNCTYPE(c_int, _JPV, _JPV, _JPV, c_int, c_int)
CB_BUILD_LIST = CFUNCTYPE(c_int, _JPV, c_int, _JPV, c_int, c_int)
CB_BUILD_DICT = CFUNCTYPE(c_int, _JPV, c_int, _JPV, c_int, c_int)
CB_ITER_NEW = CFUNCTYPE(c_int, _JPV, _JPV, POINTER(c_int), c_int, c_int)
CB_ITER_NEXT = CFUNCTYPE(c_int, _JPV, _JPV, POINTER(c_int), c_int, c_int)
CB_ITER_NEXT_BATCH = CFUNCTYPE(c_int, _JPV, _JPV, c_int, POINTER(c_int),
                               c_int, c_int)
CB_ERROR_HANDLE = CFUNCTYPE(c_int, _JPV)
CB_MAKE_ERROR = CFUNCTYPE(c_int, _JPV, c_int, c_int, _JPV)
CB_MATCH_ERROR = CFUNCTYPE(c_int, _JPV, _JPV, POINTER(c_int), c_int, c_int)
CB_IS_SIGNAL = CFUNCTYPE(c_int, _JPV, POINTER(c_int))
CB_MAKE_CLASS = CFUNCTYPE(c_int, _JPV, _JPV, _JPV, c_int, _JPV,
                          c_int, c_int)
CB_UNPACK = CFUNCTYPE(c_int, _JPV, c_int, c_int, _JPV, c_int, c_int)
CB_DO_IMPORT = CFUNCTYPE(c_int, c_int, c_int, _JPV, c_int, c_int)
CB_TO_INT = CFUNCTYPE(c_int, _JPV, _JPV, c_int, c_int)
CB_RELEASE_HANDLE = CFUNCTYPE(c_int, c_uint32)
CB_RAISE_NAME = CFUNCTYPE(c_int, c_int, c_int, c_int)
CB_RAISE_UNBOUND_LOCAL = CFUNCTYPE(c_int, c_int, c_int, c_int, c_int)
CB_RAISE_UNBOUND_CELL = CFUNCTYPE(c_int, c_int, c_int, c_int, c_int)
CB_RAISE_ZERO_DIV = CFUNCTYPE(c_int, c_int, c_int)
CB_RAISE_RECURSION = CFUNCTYPE(c_int, c_int, c_int)
CB_RAISE_NOT_ITER = CFUNCTYPE(c_int, _JPV, c_int, c_int)
CB_RAISE_BREAK = CFUNCTYPE(c_int, c_int, c_int)
CB_RAISE_CONTINUE = CFUNCTYPE(c_int, c_int, c_int)
CB_RAISE_ARGC = CFUNCTYPE(c_int, c_int, c_int, c_int, c_int)
CB_RAISE_BAD_KW = CFUNCTYPE(c_int, c_int, c_int, c_int, c_int)
CB_RAISE_MISSING = CFUNCTYPE(c_int, c_int, _IPV, c_int, c_int, c_int)
CB_RAISE_INTERNAL = CFUNCTYPE(c_int, c_char_p)
CB_LIST_APPEND = CFUNCTYPE(c_int, _JPV, _JPV, c_int, c_int)
CB_MAKE_SLICE = CFUNCTYPE(c_int, _JPV, _JPV, _JPV, _JPV, c_int, c_int)
# 变长关键字参数（M25）：名字下标数组 + 值数组 → 字典
CB_PACK_KWARGS = CFUNCTYPE(c_int, POINTER(c_int32), _JPV, c_int, _JPV,
                           c_int, c_int)


class JsHost(Structure):
    """与 jsvm.c 的 JsHost 逐字段对应的回调表。"""

    _fields_ = [
        ("binop", CB_BINOP),
        ("unary", CB_UNARY),
        ("compare", CB_COMPARE),
        ("truthy", CB_TRUTHY),
        ("call", CB_CALL),
        ("getattr", CB_GETATTR),
        ("setattr", CB_SETATTR),
        ("getitem", CB_GETITEM),
        ("setitem", CB_SETITEM),
        ("build_list", CB_BUILD_LIST),
        ("build_dict", CB_BUILD_DICT),
        ("iter_new", CB_ITER_NEW),
        ("iter_next", CB_ITER_NEXT),
        ("iter_next_batch", CB_ITER_NEXT_BATCH),
        ("do_import", CB_DO_IMPORT),
        ("to_int", CB_TO_INT),
        ("release_handle", CB_RELEASE_HANDLE),
        ("error_handle", CB_ERROR_HANDLE),
        ("make_error", CB_MAKE_ERROR),
        ("make_class", CB_MAKE_CLASS),
        ("unpack", CB_UNPACK),
        ("match_error", CB_MATCH_ERROR),
        ("is_signal", CB_IS_SIGNAL),
        ("raise_name", CB_RAISE_NAME),
        ("raise_unbound_local", CB_RAISE_UNBOUND_LOCAL),
        ("raise_unbound_cell", CB_RAISE_UNBOUND_CELL),
        ("raise_zero_div", CB_RAISE_ZERO_DIV),
        ("raise_recursion", CB_RAISE_RECURSION),
        ("raise_not_iter", CB_RAISE_NOT_ITER),
        ("raise_break", CB_RAISE_BREAK),
        ("raise_continue", CB_RAISE_CONTINUE),
        ("raise_argc", CB_RAISE_ARGC),
        ("raise_bad_kw", CB_RAISE_BAD_KW),
        ("raise_missing", CB_RAISE_MISSING),
        ("raise_internal", CB_RAISE_INTERNAL),
        ("list_append", CB_LIST_APPEND),
        ("make_slice", CB_MAKE_SLICE),
        # M25 追加：必须与 cvm/include/jsvm.h 的 JsHost 逐字段对齐
        ("pack_kwargs", CB_PACK_KWARGS),
    ]


# ---------------------------------------------------------------------------
# 基石函数回流（Python 高阶函数回调 C VM 里的基石函数）
# ---------------------------------------------------------------------------

class CvmFunction:
    """C VM 中的基石函数在 Python 侧的包装。

    带 ``__call__``，因此可直接交给 Python 高阶函数（sorted/map 等）；
    调用时经 ``jsvm_call_function`` 重入 C VM。
    """

    __slots__ = ("_host", "_raw")

    def __init__(self, host: "CvmHost", raw: JVal):
        self._host = host
        self._raw = _jfunc_copy(raw)
        host.lib.jsvm_func_retain(host.vm, byref(self._raw))

    def __del__(self):
        try:
            self._host.lib.jsvm_func_release(self._host.vm,
                                             byref(self._raw))
        except Exception:
            pass

    @property
    def name(self) -> str:
        ci = self._host.lib.jsvm_func_code(self._host.vm, byref(self._raw))
        codes = self._host.cmod.codes
        return codes[ci].name if 0 <= ci < len(codes) else "?"

    def __repr__(self):
        return f"<函数 {self.name}>"

    def __call__(self, *args, **kwargs):
        return self._host.call_jishi_function(self._raw, args, kwargs)


# ---------------------------------------------------------------------------
# 宿主实现
# ---------------------------------------------------------------------------

#: 可安全批量预取的类型（list/范围/文本/字典的迭代无副作用，
#: 提前多取几个元素不会改变可观察行为；生成器等保持逐个拉取）
_BATCHABLE_TYPES = (list, tuple, range)


class _Iter:
    # 宿主侧迭代器对象：C 侧每次回调从这里拿数据。
    __slots__ = ("seq", "pos", "it")

    def __init__(self, obj: Any):
        t = type(obj)
        if t in _BATCHABLE_TYPES:
            self.seq = obj          # 支持切片，按 pos 批量取
            self.pos = 0
            self.it = None
        elif isinstance(obj, str):
            self.seq = list(obj)    # 文本按字批量
            self.pos = 0
            self.it = None
        elif isinstance(obj, dict):
            self.seq = list(obj.keys())
            self.pos = 0
            self.it = None
        else:
            self.seq = None         # 通用迭代器：逐个拉取
            self.pos = 0
            self.it = iter(obj)

    @property
    def batchable(self) -> bool:
        return self.seq is not None


class CvmHost:
    """宿主侧：句柄表、影子全局表、pending 错误与全部回调。"""

    def __init__(self, cmod: O.CompiledModule, filename: str,
                 names: list[str]):
        self.lib = load_lib()
        self.cmod = cmod
        #: 扩展后的名字表（编译器不把关键字参数名放进 names，
        #: C 侧 kw_flat 又需要名字下标，这里统一补齐）
        self.names = names
        self.filename = filename
        #: 句柄表：句柄号 → Python 对象（C 侧引用计数归零时删除）
        self._handles: dict[int, Any] = {}
        self._next_handle = 1
        #: 函数包装缓存（M23 发现）：JVal.v.u（JsFunc* 指针）→ CvmFunction。
        #: 不在缓存里复用一个包装，会让同一个函数每次跨界都变成新对象，
        #: `f 是 g` 在 C VM 下判不等、与另外两个执行器不一致。
        #: 缓存持强引用 ⇒ 底层 JsFunc 不会被释放 ⇒ 指针不会被复用，键安全。
        self._func_cache: dict[int, "CvmFunction"] = {}
        #: 影子全局表（报错建议 / do_import 用），初始为内建
        self.globals = R.new_builtins()
        self.pending: Optional[JishiError] = None

        self.vm = self.lib.jsvm_create()

        # 回调表：CFUNCTYPE 实例必须保活，挂在 self 上
        self._cb = JsHost(
            binop=CB_BINOP(self._cb_binop),
            unary=CB_UNARY(self._cb_unary),
            compare=CB_COMPARE(self._cb_compare),
            truthy=CB_TRUTHY(self._cb_truthy),
            call=CB_CALL(self._cb_call),
            getattr=CB_GETATTR(self._cb_getattr),
            setattr=CB_SETATTR(self._cb_setattr),
            getitem=CB_GETITEM(self._cb_getitem),
            setitem=CB_SETITEM(self._cb_setitem),
            build_list=CB_BUILD_LIST(self._cb_build_list),
            build_dict=CB_BUILD_DICT(self._cb_build_dict),
            iter_new=CB_ITER_NEW(self._cb_iter_new),
            iter_next=CB_ITER_NEXT(self._cb_iter_next),
            iter_next_batch=CB_ITER_NEXT_BATCH(self._cb_iter_next_batch),
            do_import=CB_DO_IMPORT(self._cb_do_import),
            to_int=CB_TO_INT(self._cb_to_int),
            release_handle=CB_RELEASE_HANDLE(self._cb_release_handle),
            error_handle=CB_ERROR_HANDLE(self._cb_error_handle),
            make_error=CB_MAKE_ERROR(self._cb_make_error),
            make_class=CB_MAKE_CLASS(self._cb_make_class),
            unpack=CB_UNPACK(self._cb_unpack),
            match_error=CB_MATCH_ERROR(self._cb_match_error),
            is_signal=CB_IS_SIGNAL(self._cb_is_signal),
            raise_name=CB_RAISE_NAME(self._cb_raise_name),
            raise_unbound_local=CB_RAISE_UNBOUND_LOCAL(
                self._cb_raise_unbound_local),
            raise_unbound_cell=CB_RAISE_UNBOUND_CELL(
                self._cb_raise_unbound_cell),
            raise_zero_div=CB_RAISE_ZERO_DIV(self._cb_raise_zero_div),
            raise_recursion=CB_RAISE_RECURSION(self._cb_raise_recursion),
            raise_not_iter=CB_RAISE_NOT_ITER(self._cb_raise_not_iter),
            raise_break=CB_RAISE_BREAK(self._cb_raise_break),
            raise_continue=CB_RAISE_CONTINUE(self._cb_raise_continue),
            raise_argc=CB_RAISE_ARGC(self._cb_raise_argc),
            raise_bad_kw=CB_RAISE_BAD_KW(self._cb_raise_bad_kw),
            raise_missing=CB_RAISE_MISSING(self._cb_raise_missing),
            raise_internal=CB_RAISE_INTERNAL(self._cb_raise_internal),
            list_append=CB_LIST_APPEND(self._cb_list_append),
            make_slice=CB_MAKE_SLICE(self._cb_make_slice),
            pack_kwargs=CB_PACK_KWARGS(self._cb_pack_kwargs),
        )
        self.lib.jsvm_set_host(self.vm, byref(self._cb))

    # -- 生命周期 -----------------------------------------------------------

    def destroy(self):
        if getattr(self, "vm", None):
            self.lib.jsvm_destroy(self.vm)
            self.vm = None

    # -- 值转换 ---------------------------------------------------------------

    def _new_handle(self, obj: Any) -> int:
        hid = self._next_handle
        self._next_handle += 1
        self._handles[hid] = obj
        self.lib.jsvm_track_handle(self.vm, hid)
        return hid

    def py2c(self, obj: Any) -> JVal:
        """Python 对象 → JVal。产生的 C 侧引用（句柄/FUNC）归接收槽位所有。"""
        if obj is None:
            return _jnull()
        if obj is True:
            return _jbool(True)
        if obj is False:
            return _jbool(False)
        if isinstance(obj, int):
            return _jint(obj)
        if isinstance(obj, float):
            return _jfloat(obj)
        if isinstance(obj, CvmFunction):
            v = _jfunc_copy(obj._raw)          # 借用其引用再 retain 一次
            self.lib.jsvm_func_retain(self.vm, byref(v))
            return v
        return _jhost(self._new_handle(obj))

    def c2py(self, v: JVal) -> Any:
        """JVal → Python 对象（借用语义：不改 C 侧引用计数）。"""
        t = v.tag
        if t == O.T_NULL:
            return None
        if t == O.T_FALSE:
            return False
        if t == O.T_TRUE:
            return True
        if t == O.T_INT:
            return int(v.v.i)
        if t == O.T_FLOAT:
            return float(v.v.d)
        if t == O.T_HOST:
            return self._handles[int(v.v.u)]
        if t == O.T_FUNC:
            # 同一函数对象必须映射到同一包装（见 _func_cache 的说明）。
            # `v.v.u` 是 C 侧 struct JsFunc* 指针，函数活着期间唯一且稳定。
            key = int(v.v.u)
            cached = self._func_cache.get(key)
            if cached is not None:
                return cached
            wrapper = CvmFunction(self, v)
            self._func_cache[key] = wrapper
            return wrapper
        raise RunError(f"内部错误：C 侧传来不可转换的值（标签 {t}）")

    def _take(self, v: JVal) -> Any:
        """转换并接管所有权（C 侧引用随之释放）。"""
        obj = self.c2py(v)
        self.lib.jsvm_release(self.vm, byref(v))
        return obj

    # -- 错误存档 -------------------------------------------------------------

    def _fail(self, e: BaseException) -> int:
        if self.pending is None:
            if isinstance(e, JishiError):
                self.pending = e
            else:
                self.pending = RunError(
                    f"内部错误（宿主回调）：{type(e).__name__}: {e}")
        return -1

    # -- 值栈借用数组的转换 ---------------------------------------------------

    @staticmethod
    def _ptr_to_list(p: _JPV, n: int) -> list[JVal]:
        return [p[i] for i in range(n)]

    # =======================================================================
    # 语义回调（全部委托 runtime.py，与树遍历共用同一份措辞）
    # =======================================================================

    def _cb_binop(self, op, a, b, out, line, col):
        try:
            r = R.apply_binop(BINOP_TO_SYMBOL[op], self.c2py(a[0]),
                              self.c2py(b[0]), line=line, col=col,
                              filename=self.filename)
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_unary(self, op, a, out, line, col):
        try:
            r = R.apply_unary("-" if op == UnOp.NEG else "非",
                              self.c2py(a[0]), line=line, col=col,
                              filename=self.filename)
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_compare(self, op, a, b, out, line, col):
        try:
            r = R.apply_compare(CMPOP_TO_SYMBOL[op], self.c2py(a[0]),
                                self.c2py(b[0]), line=line, col=col,
                                filename=self.filename)
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_truthy(self, a, out, line, col):
        try:
            out[0] = 1 if R.truthy(self.c2py(a[0])) else 0
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_call(self, fn, args, nargs, kwname_idx, kwvals, nkw, out,
                 line, col):
        try:
            f = self.c2py(fn[0])
            arglist = [self.c2py(args[i]) for i in range(nargs)]
            kwargs = {}
            for k in range(nkw):
                kwargs[self.names[kwname_idx[k]]] = self.c2py(kwvals[k])
            r = R.call_value(f, arglist, kwargs, line=line, col=col,
                             filename=self.filename)
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_getattr(self, obj, name_idx, out, line, col):
        try:
            r = R.get_attr(self.c2py(obj[0]), self.names[name_idx],
                           line=line, col=col, filename=self.filename)
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_setattr(self, obj, name_idx, val, line, col):
        try:
            R.set_attr(self.c2py(obj[0]), self.names[name_idx],
                       self.c2py(val[0]), line=line, col=col,
                       filename=self.filename)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_getitem(self, a, b, out, line, col):
        try:
            r = R.get_item(self.c2py(a[0]), self.c2py(b[0]),
                           line=line, col=col, filename=self.filename)
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_setitem(self, a, b, val, line, col):
        try:
            R.set_item(self.c2py(a[0]), self.c2py(b[0]), self.c2py(val[0]),
                       line=line, col=col, filename=self.filename)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_build_list(self, items, n, out, line, col):
        try:
            r = [self.c2py(items[i]) for i in range(n)]
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_build_dict(self, flat, n, out, line, col):
        try:
            pairs = [(self.c2py(flat[i]), self.c2py(flat[i + 1]))
                     for i in range(0, 2 * n, 2)]
            r = R.build_dict(pairs, line=line, col=col,
                             filename=self.filename)
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_iter_new(self, obj, out, batchable, line, col):
        try:
            o = self.c2py(obj[0])
            wrapper = _Iter(o)
            try:
                if not wrapper.batchable:
                    iter(wrapper.it)      # 触发「不能遍历」检查
            except TypeError:
                raise RunTypeError(
                    f"「{o}」不能遍历，遍历需要列表、文本、集合或字典；自定义对象可以定义「迭代()」方法",
                    line=line, col=col, filename=self.filename)
            batchable[0] = 1 if wrapper.batchable else 0
            out[0] = self.py2c(wrapper)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_iter_next(self, it, out, done, line, col):
        try:
            wrapper = self.c2py(it[0])
            try:
                v = next(wrapper.it)
            except StopIteration:
                done[0] = 1
                return 0
            out[0] = self.py2c(v)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_iter_next_batch(self, it, out, cap, n, line, col):
        try:
            wrapper = self.c2py(it[0])
            if wrapper.seq is not None:
                chunk = wrapper.seq[wrapper.pos:wrapper.pos + cap]
                wrapper.pos += len(chunk)
            else:
                chunk = []
                for _ in range(cap):
                    try:
                        chunk.append(next(wrapper.it))
                    except StopIteration:
                        break
            # 快路径（M20）：整批都是整数时，一次 pack + memmove 写入 C 侧
            # 数组，省掉逐个构造 JVal 的 ctypes 开销（实测约快 9 倍）。
            # 混合类型 / 大端平台 / 溢出时回退到逐个 py2c，语义不变。
            if chunk and _LITTLE_ENDIAN and all(type(v) is int for v in chunk):
                try:
                    fmt = _int_batch_fmt(len(chunk))
                    flat: list = []
                    for v in chunk:
                        flat.append(O.T_INT)
                        flat.append(v)
                    ctypes.memmove(out, fmt.pack(*flat),
                                   len(chunk) * _JVAL_SIZE)
                    n[0] = len(chunk)
                    return 0
                except (_struct.error, OverflowError):
                    pass          # 回退逐个
            for i, v in enumerate(chunk):
                out[i] = self.py2c(v)
            n[0] = len(chunk)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_do_import(self, name_idx, src, out, line, col):
        try:
            # src 编码导入来源：0=标准库 1=python 2=本地包
            r = R.do_import(self.names[name_idx], src == 1, src == 2,
                            line=line, col=col, filename=self.filename)
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_to_int(self, a, out, line, col):
        try:
            n = self.c2py(a[0])
            try:
                n = int(n)
            except (TypeError, ValueError):
                raise RunTypeError(
                    f"「{n}」不是有效的循环次数",
                    line=line, col=col, filename=self.filename)
            out[0] = _jint(n)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_release_handle(self, hid):
        try:
            self._handles.pop(int(hid), None)
            return 0
        except BaseException as e:
            return self._fail(e)

    # -- 异常协议（M5a） ------------------------------------------------------

    def _cb_error_handle(self, out):
        # C 侧分发时取 pending 异常对象：转成 HOST 句柄交给 C 侧（持引用）
        try:
            if self.pending is None:
                out[0] = JVal()      # UNSET：无 pending（内部错误）
                return -1
            exc = self.pending
            self.pending = None
            out[0] = self.py2c(exc)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_make_error(self, value, line, col, out):
        # `抛出 值`：规范化成异常对象，原值所有权转移给返回的异常句柄
        try:
            v = self.c2py(value[0])
            exc = R.make_exception(v, line=line, col=col,
                                   filename=self.filename)
            out[0] = self.py2c(exc)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_match_error(self, exc, cond, out, line, col):
        try:
            matched = R.exception_matches(
                self.c2py(exc[0]), self.c2py(cond[0]),
                line=line, col=col, filename=self.filename)
            out[0] = 1 if matched else 0
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_is_signal(self, exc, out):
        try:
            e = self.c2py(exc[0])
            if isinstance(e, RunBreak):
                out[0] = 1
            elif isinstance(e, RunContinue):
                out[0] = 2
            else:
                out[0] = 0
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_make_class(self, name, base, methods, nmethods, out, line, col):
        # 组装 JishiClass：方法名从 FUNC 的 code 名读，基类 UNSET 表示无
        try:
            cls_name = self.c2py(name[0])
            base_cls = None
            if base[0].tag != O.T_UNSET:
                b = self.c2py(base[0])
                if not isinstance(b, R.JishiClass):
                    raise RunTypeError(
                        f"「{b}」不是类，不能继承",
                        line=line, col=col, filename=self.filename)
                base_cls = b
            meth_dict = {}
            for i in range(nmethods):
                fn = self.c2py(methods[i])
                ci = self.lib.jsvm_func_code(self.vm, byref(methods[i]))
                code = self.cmod.codes[ci]
                meth_dict[code.name] = fn
            out[0] = self.py2c(R.JishiClass(cls_name, meth_dict,
                                            base=base_cls))
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_unpack(self, value, n, star, out, line, col):
        """把可迭代解成 n 个元素写入 out（所有权归 C）。

        ``star >= 0`` 时是带星号解包（M25），该位置的元素是「其余」的列表。
        """
        try:
            obj = self.c2py(value[0])
            items = R.unpack_values(obj, n, None if star < 0 else star,
                                    line=line, col=col,
                                    filename=self.filename)
            for i, item in enumerate(items):
                out[i] = self.py2c(item)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_pack_kwargs(self, name_idxs, vals, n, out, line, col):
        """把未匹配的关键字收成字典（M25，`**选项` 用）。

        name_idxs 是名字表下标（C 侧没有「下标 → 字符串」的能力，所以由
        宿主来翻译）。n 为 0 时两个指针都可能为空。
        """
        try:
            pairs = []
            for i in range(n):
                pairs.append((self.names[name_idxs[i]], self.c2py(vals[i])))
            out[0] = self.py2c(R.build_dict(
                pairs, line=line, col=col, filename=self.filename))
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_list_append(self, obj, val, line, col):
        """列表追加（M11）：obj.追加(val)，语义与 get_attr+call 完全一致。

        对非列表对象调用「追加」会走 get_attr 的通用报错，措辞不变。
        """
        try:
            o = self.c2py(obj[0])
            v = self.c2py(val[0])
            if type(o) is list:      # 快路径：最常见场景，省 get_attr+call 分发
                o.append(v)
                return 0
            m = R.get_attr(o, "追加", line=line, col=col,
                           filename=self.filename)
            R.call_value(m, [v], {}, line=line, col=col,
                         filename=self.filename)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_make_slice(self, start, stop, step, out, line, col):
        """切片（M18）：start/stop/step 三个分量（UNSET 表省略）→ slice HOST 句柄。"""
        try:
            def conv(v):
                # UNSET 标签表示「省略」→ None
                if v.tag == O.T_UNSET:
                    return None
                return self.c2py(v)
            s = R.make_slice(conv(start[0]), conv(stop[0]), conv(step[0]),
                             line=line, col=col, filename=self.filename)
            out[0] = self.py2c(s)
            return 0
        except BaseException as e:
            return self._fail(e)

    # -- 报错族（措辞与 vm.py 逐字一致） -------------------------------------

    def _cb_raise_name(self, name_idx, line, col):
        try:
            # 影子表只在内建赋值时准确；用户全局惰性从 C 侧查询
            pool = dict(self.globals)
            for i, name in enumerate(self.names):
                if name not in pool and self.lib.jsvm_global_set(self.vm, i):
                    pool[name] = None
            R.lookup_name(self.names[name_idx], pool,
                          line=line, col=col, filename=self.filename)
            return 0    # 不可达：lookup_name 必然抛错
        except JishiError as e:
            return self._fail(e)
        except BaseException as e:
            return self._fail(e)

    def _cb_raise_unbound_local(self, code_idx, slot, line, col):
        code = self.cmod.codes[code_idx]
        name = (code.local_names[slot]
                if slot < len(code.local_names) else f"${slot}")
        self.pending = RunError(
            f"找不到名字「{name}」", line=line, col=col,
            filename=self.filename, hint=code.local_hints.get(slot))
        return -1

    def _cb_raise_unbound_cell(self, code_idx, cell_idx, line, col):
        code = self.cmod.codes[code_idx]
        if cell_idx < len(code.cellvars):
            name = code.cellvars[cell_idx]
        else:
            j = cell_idx - len(code.cellvars)
            name = (code.freevars[j]
                    if 0 <= j < len(code.freevars) else f"?单元{cell_idx}")
        self.pending = RunError(
            f"找不到名字「{name}」", line=line, col=col,
            filename=self.filename)
        return -1

    def _cb_raise_zero_div(self, line, col):
        try:
            # 借 apply_binop 生成与树遍历逐字一致的除零文案
            R.apply_binop("//", 1, 0, line=line, col=col,
                          filename=self.filename)
            return 0
        except JishiError as e:
            return self._fail(e)
        except BaseException as e:
            return self._fail(e)

    def _cb_raise_recursion(self, line, col):
        self.pending = RunError(
            "递归层数太深了，是不是函数忘了写结束条件？",
            line=line or 1, col=col or 1, filename=self.filename)
        return -1

    def _cb_raise_not_iter(self, obj, line, col):
        o = self.c2py(obj[0])
        self.pending = RunTypeError(
            f"「{o}」不能遍历，遍历需要列表、文本、集合或字典；自定义对象可以定义「迭代()」方法",
            line=line, col=col, filename=self.filename)
        return -1

    def _cb_raise_break(self, line, col):
        self.pending = RunBreak("中断", line=line, col=col,
                                filename=self.filename)
        return -1

    def _cb_raise_continue(self, line, col):
        self.pending = RunContinue("继续", line=line, col=col,
                                   filename=self.filename)
        return -1

    def _cb_raise_argc(self, code_idx, got, line, col):
        code = self.cmod.codes[code_idx]
        nparams = len(code.params)
        self.pending = RunTypeError(
            f"函数「{code.name}」需要 {nparams} 个参数，"
            f"但传了 {got} 个",
            line=line or code.firstlineno, col=col or 1,
            filename=self.filename)
        return -1

    def _cb_raise_bad_kw(self, code_idx, name_idx, line, col):
        code = self.cmod.codes[code_idx]
        k = self.names[name_idx]
        self.pending = RunTypeError(
            f"函数「{code.name}」没有叫「{k}」的参数",
            line=line or code.firstlineno, col=col or 1,
            filename=self.filename,
            hint=f"它的参数是：{'、'.join(code.params)}")
        return -1

    def _cb_raise_missing(self, code_idx, missing, nmissing, line, col):
        code = self.cmod.codes[code_idx]
        miss = [code.params[missing[i]] for i in range(nmissing)]
        self.pending = RunTypeError(
            f"函数「{code.name}」缺少参数：{'、'.join(miss)}",
            line=line or code.firstlineno, col=col or 1,
            filename=self.filename)
        return -1

    def _cb_raise_internal(self, msg):
        text = msg.decode("utf-8", "replace") if isinstance(msg, bytes) \
            else str(msg)
        self.pending = RunError(text)
        return -1

    # -- 基石函数回流 ---------------------------------------------------------

    def call_jishi_function(self, raw: JVal, args: tuple, kwargs: dict) -> Any:
        """Python 高阶函数回调基石函数（重入 C VM）。"""
        c_args = (JVal * max(len(args), 1))()
        for i, a in enumerate(args):
            c_args[i] = self.py2c(a)
        kw_names = list(kwargs)
        c_knames = (c_int32 * max(len(kw_names), 1))()
        name_index = {n: i for i, n in enumerate(self.names)}
        for i, k in enumerate(kw_names):
            if k not in name_index:
                # 关键字名不在名字表里：扩表不可行，直接报参数错误
                raise RunTypeError(f"没有叫「{k}」的参数",
                                   filename=self.filename)
            c_knames[i] = name_index[k]
        c_kwvals = (JVal * max(len(kw_names), 1))()
        for i, v in enumerate(kwargs.values()):
            c_kwvals[i] = self.py2c(v)

        out = JVal()
        rc = self.lib.jsvm_call_function(
            self.vm, byref(raw), c_args, len(args),
            c_knames, c_kwvals, len(kw_names), byref(out))
        for i in range(len(args)):
            self.lib.jsvm_release(self.vm, byref(c_args[i]))
        for i in range(len(kw_names)):
            self.lib.jsvm_release(self.vm, byref(c_kwvals[i]))
        if rc != 0:
            # 异常在 C 侧穿透到重入边界（stop_depth）后返回：从 C 取回异常对象
            err = JVal()
            self.lib.jsvm_take_pending_err(self.vm, byref(err))
            if err.tag != O.T_UNSET:
                raise self._take(err)
            raise self._pop_pending()
        return self._take(out)

    def _pop_pending(self) -> JishiError:
        e = self.pending
        self.pending = None
        if e is None:
            e = RunError("内部错误：C VM 失败但没有留下错误信息")
        return e


# ---------------------------------------------------------------------------
# 模块序列化与执行入口
# ---------------------------------------------------------------------------

class CvmRunner:
    """把 CompiledModule 喂给 C VM 并运行。"""

    def __init__(self, cmod: O.CompiledModule, filename: Optional[str] = None):
        self.cmod = cmod
        self.filename = filename or cmod.filename
        # 关键字参数名可能不在名字表里，C 的 kw_flat 需要下标 → 补齐
        names = list(cmod.names)
        name_index = {n: i for i, n in enumerate(names)}
        for kwl in cmod.kw_names:
            for k in kwl:
                if k not in name_index:
                    name_index[k] = len(names)
                    names.append(k)
        self.host = CvmHost(cmod, self.filename, names)
        self._keep = []          # 防 GC：所有传给 C 的数组保活
        self._name_index = name_index
        self._load()

    # -- 序列化 ---------------------------------------------------------------

    def _load(self):
        lib = self.host.lib
        cmod = self.cmod

        # 指令流与代码元数据
        instrs: list[int] = []
        code_meta: list[int] = []
        code_params: list[int] = []
        for code in cmod.codes:
            # 注意：params_off 以 int32 为单位（C 侧按字节指针推进），
            # 每个参数占 4 个 int32（M25 起加了一个 kind），因此直接取
            # len(code_params)。
            code_meta.extend([
                len(instrs) // 6, len(code.instrs), code.nlocals,
                len(code.cellvars), len(code.params),
                len(code_params),
            ])
            for ins in code.instrs:
                instrs.extend(ins.as_tuple())
            for i in range(len(code.params)):
                # 每个形参 4 个 int32：idx / local / cell / kind（M25）
                kinds = code.param_kind or [0] * len(code.params)
                code_params.extend([
                    code.param_idx[i], code.param_local[i],
                    code.param_cell[i],
                    kinds[i] if i < len(kinds) else 0,
                ])

        # 关键字参数名表（按调用点展开成名字下标）
        name_index = self._name_index
        nnames = len(self.host.names)
        kw_flat: list[int] = []
        kw_off: list[int] = [0]
        for kwl in cmod.kw_names:
            for k in kwl:
                kw_flat.append(name_index[k])
            kw_off.append(len(kw_flat))

        # 常量池：数字内联，其余走句柄
        consts = (JVal * max(len(cmod.consts), 1))()
        for i, c in enumerate(cmod.consts):
            consts[i] = self.host.py2c(c)

        # 初始全局（内建函数），长度 = 名字表长度
        globals_in = (JVal * max(nnames, 1))()
        for i, name in enumerate(cmod.names):
            if name in self.host.globals:
                globals_in[i] = self.host.py2c(self.host.globals[name])
            else:
                globals_in[i] = JVal()   # UNSET

        c_instrs = (c_int32 * max(len(instrs), 1))(*instrs)
        c_meta = (c_int32 * max(len(code_meta), 1))(*code_meta)
        c_params = (c_int32 * max(len(code_params), 1))(*code_params)
        c_kwflat = (c_int32 * max(len(kw_flat), 1))(*kw_flat)
        c_kwoff = (c_int32 * max(len(kw_off), 1))(*kw_off)

        rc = lib.jsvm_load_module(
            self.host.vm, consts, len(cmod.consts), nnames, globals_in,
            c_instrs, len(instrs) // 6,
            c_meta, len(cmod.codes),
            c_params, c_kwflat, c_kwoff, len(cmod.kw_names),
            cmod.main)
        if rc != 0:
            self.host.destroy()
            raise RunError("内部错误：字节码装载失败")
        self._keep.extend([consts, globals_in, c_instrs, c_meta,
                           c_params, c_kwflat, c_kwoff])

    # -- 执行 ---------------------------------------------------------------

    def run(self) -> Any:
        """执行模块主代码；出错时抛出与树遍历一致的中文异常。"""
        try:
            rc = self.host.lib.jsvm_run(self.host.vm)
        except BaseException:
            raise
        if rc != 0:
            # 优先取回 C 侧无人接住的异常对象；退回 Python 侧 pending
            err = JVal()
            self.host.lib.jsvm_take_pending_err(self.host.vm, byref(err))
            if err.tag != O.T_UNSET:
                raise self.host._take(err)
            raise self.host._pop_pending()
        out = JVal()
        self.host.lib.jsvm_get_last(self.host.vm, byref(out))
        if out.tag == O.T_UNSET:
            return None
        return self.host._take(out)

    def close(self):
        self.host.destroy()


def run_source_c(source: str, filename: str = "<输入>") -> Any:
    """源码 → 编译 → C 字节码虚拟机执行（三执行器之一）。"""
    from .compiler import compile_source

    cmod = compile_source(source, filename)
    runner = CvmRunner(cmod, filename)
    try:
        return runner.run()
    finally:
        runner.close()
