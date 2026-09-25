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

import array as _array
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
from .errors import (DebugQuit, Frame, JishiError, RunBreak, RunContinue,
                     RunError, RunTypeError, RunValueError)
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
    # 调试支持（M40）：语句起始表 + 语句钩子
    lib.jsvm_set_stmt_marks.argtypes = [c_void_p, POINTER(c_int32), c_int]
    lib.jsvm_set_stmt_hook.argtypes = [c_void_p, c_void_p, c_void_p]
    # 帧查询（M40）：**必须声明 argtypes**，否则 ctypes 会把 vm 指针当普通整数
    # 传（OverflowError: int too long to convert）
    lib.jsvm_frame_meta.restype = c_int
    lib.jsvm_frame_meta.argtypes = [c_void_p, c_int, POINTER(c_int32)]
    lib.jsvm_dump_locals.restype = c_int
    lib.jsvm_dump_locals.argtypes = [c_void_p, c_int]
    lib.jsvm_dump_globals.restype = c_int
    lib.jsvm_dump_globals.argtypes = [c_void_p]
    lib.jsvm_track_handle.argtypes = [c_void_p, c_uint32]
    # M47 阶段二①：宿主侧构造原生字符串（UTF-8 字节 → JS_TAG_STR）
    lib.jsvm_str_new.restype = c_int
    lib.jsvm_str_new.argtypes = [c_void_p, c_char_p, c_int32, JPV]
    lib.jsvm_str_len.restype = c_int32
    lib.jsvm_str_len.argtypes = [JPV]
    lib.jsvm_str_bytes.restype = c_void_p
    lib.jsvm_str_bytes.argtypes = [JPV]
    # M47 阶段二①：批量构造（文本迭代器整批一次建，省 n-1 次 ctypes 往返）
    lib.jsvm_str_new_batch.restype = c_int
    lib.jsvm_str_new_batch.argtypes = [c_void_p, c_char_p, c_void_p, c_int, JPV]
    lib.jsvm_func_retain.argtypes = [c_void_p, JPV]
    lib.jsvm_func_release.argtypes = [c_void_p, JPV]
    lib.jsvm_call_function.restype = c_int
    lib.jsvm_call_function.argtypes = [
        c_void_p, JPV, JPV, c_int,
        POINTER(c_int), JPV, c_int, JPV,
        c_int, c_int,        # call_line, call_col（M40：重入建帧的调用点）
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


class _JsStr(Structure):
    """原生字符串（`JS_TAG_STR`）的头部，与 `cvm/src/jsvm.c` 的 `JsStr`
    逐字段对齐；字节数据紧跟其后。

    ⚠️ 这是**内部布局**，不是公开 ABI 的一部分。之所以在 Python 里镜像它
    而不是走 `jsvm_str_bytes`/`jsvm_str_len`：`c2py` 在宿主回调的热路径上
    （每个文本参数一次），镜像之后零 FFI 就能拿到字节；走函数则每次多两次
    ctypes 往返。**改 C 侧必须同步这里**，
    `tests/test_cvm.py::test_native_string_layout` 有往返测试钉住它。

    第三方宿主没有这个便利，请用头文件里的 `jsvm_str_new` /
    `jsvm_str_len` / `jsvm_str_bytes`。
    """

    _fields_ = [("refcnt", c_int32), ("blen", c_int32)]


#: 原生字符串头部字节数（= C 侧 `sizeof(JsStr)`）。
_JS_STR_HDR = ctypes.sizeof(_JsStr)

#: JVal 的字节大小（批量转换用）
_JVAL_SIZE = ctypes.sizeof(JVal)

#: 批量构造 JVal 数组的 struct.Struct 缓存（M20 引入，M46 泛化）。
#: 布局：uint8 tag + 7 字节填充 + 8 字节值（小端），与 `JVal` 内存布局一致。
#: 两种格式只差「值槽怎么解释」：
#:   `B7xq` ── 整数 / 布尔 / 空（8 字节整数槽）
#:   `B7xd` ── 浮点（8 字节 double 槽）
#: （M47 阶段二①之前还有 `B7xQ` 宿主句柄格 —— 文本改原生字符串后
#:  Python 拿不到其指针，那条路已删，见 `_pack_batch` 的文本分支。）
_BATCH_FMT: dict[tuple[str, int], _struct.Struct] = {}
_LITTLE_ENDIAN = _sys.byteorder == "little"

# ⚠️ M41 的教训留在这里（现在不用异常了，但同类坑还在）：
# 早先批量快路径用「一个哨兵异常」表示「这批混了非整数」，
# 而 `except (A, B, 实例)` 会在运行时炸（TypeError: catching classes that
# do not inherit from BaseException）。**要抛就抛类，别抛实例**。
# M46 改成 `_pack_batch` 返回 `None`，不再需要那个哨兵。


def _batch_fmt(kind: str, n: int) -> _struct.Struct:
    key = (kind, n)
    st = _BATCH_FMT.get(key)
    if st is None:
        st = _struct.Struct("<" + kind * n)
        _BATCH_FMT[key] = st
    return st


def _jfunc_copy(src: JVal) -> JVal:
    x = JVal()
    x.tag = src.tag
    x.v.u = src.v.u
    return x


#: 不套 `_guard` 的宿主回调（M46 阶段一④）。两类：
#:  - **错误处理期间必须照常跑**的：`note_frames` 就是来记调用链的
#:    （它恰好在 pending 已设置时才被调）；
#:  - **返回值被 C 侧忽略**的：`release_handle` 在 `release_val` 里调，没人看它返回什么
#:    ——套上会把「冲刷失败」的标志白白吃掉，真正该失败的回调反而放行（实测踩到）。
_GUARD_EXEMPT = frozenset({
    "note_frames", "debug_local", "release_handle", "raise_internal",
    "error_handle", "is_signal", "match_error", "make_error",
})


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
#: 批量追加（M46 阶段一③）：handle + C 侧值数组（**借用**）+ 个数。
#: C 侧「追加延迟合并写」的落盘口——把「循环里追加 N 次」的 N 次回调压成 1 次。
CB_LIST_APPEND_MANY = CFUNCTYPE(c_int, c_uint32, _JPV, c_int)
#: 批量字典写入（M46 阶段一④）：handle + 键值数组（每项 2 个，借用）
#: + 行列数组（每项 2 个）+ 条数。行列**按项传**，落盘失败时报错行号才准。
CB_DICT_SETITEM_MANY = CFUNCTYPE(c_int, c_uint32, _JPV,
                                 POINTER(c_int32), c_int)
#: 方法调用融合（M48 阶段三②）：obj + 方法名下标 + 实参数组 + 实参数
#: + 结果 + **两组**行列（GET_ATTR 的 / CALL 的）——取属性失败报前者、
#: 调用失败报后者，与「先 getattr 再 call」逐字一致。
CB_METHOD_CALL = CFUNCTYPE(c_int, _JPV, c_int, _JPV, c_int, _JPV,
                           c_int, c_int, c_int, c_int)
CB_MAKE_SLICE = CFUNCTYPE(c_int, _JPV, _JPV, _JPV, _JPV, c_int, c_int)
# 变长关键字参数（M25）：名字下标数组 + 值数组 → 字典
CB_PACK_KWARGS = CFUNCTYPE(c_int, POINTER(c_int32), _JPV, c_int, _JPV,
                           c_int, c_int)
# 报错现场（M40）：扁平帧数组 + 层数（每层 3 个 int32：code_idx/行/列）
CB_NOTE_FRAMES = CFUNCTYPE(c_int, POINTER(c_int32), c_int)
# 语句钩子（M40）：每条语句首指令执行前回调。
# 参数：宿主上下文指针（未用，用实例属性取）、代码下标、行号、当前帧数。
CB_STMT_HOOK = CFUNCTYPE(c_int, c_void_p, c_int, c_int, c_int)
# 调试：把一帧的一个变量交给宿主（M40）。
# 参数：槽位号、是否单元变量、名字表下标（C 侧用不上，宿主自己按槽位查）、值指针。
CB_DEBUG_LOCAL = CFUNCTYPE(c_int, c_int, c_int, c_int, _JPV)


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
        # M40 追加：同上，**只能加在末尾**（插中间会让后续字段全部错位）
        ("note_frames", CB_NOTE_FRAMES),
        ("debug_local", CB_DEBUG_LOCAL),
        # M46 追加：同上，必须与 cvm/include/jsvm.h 的 JsHost 逐字段对齐
        ("list_append_many", CB_LIST_APPEND_MANY),
        ("dict_setitem_many", CB_DICT_SETITEM_MANY),
        # M48 追加：同上（**差一行就是 NULL 函数指针 → 段错误**，M48 踩过）
        ("method_call", CB_METHOD_CALL),
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
        #: 常量文本的「钉住表」：原生字符串指针 → 宿主侧原件（M47 阶段二①）。
        #: 常量在 `jsvm_destroy` 之前不会被释放，所以按指针认回原件是安全的
        #: （见 `pin_str`）。只装常量，所以条目数 = 程序的文本字面量数，有界。
        self._str_pinned: dict[int, str] = {}
        #: 影子全局表（报错建议 / do_import 用），初始为内建
        self.globals = R.new_builtins()
        self.pending: Optional[JishiError] = None
        #: 「延迟合并写的冲刷刚刚失败」的一次性标志（M46 阶段一④）——见 `_guard`。
        self._flush_failed = False

        self.vm = self.lib.jsvm_create()

        # 回调表：CFUNCTYPE 实例必须保活，挂在 self 上
        #
        # M46 阶段一④：**每个宿主回调都先套一层「有待抛的错就直接失败」**。
        # 为什么需要：C 侧「延迟合并写」在**调用回调之前**会先冲刷，而冲刷
        # 可能失败（例如字典键不可哈希）。那个失败的返回值被 `HC` 宏丢掉了，
        # 所以必须靠这里把错误接住、并让**当前这次回调也失败**——否则错误会被
        # 吞掉、后续语句照跑（实测踩到：`字[[i]] = i` 在 C VM 里静默不报错）。
        # 包一层之后：冲刷失败 → pending 已设 → 紧接着那次回调返回 -1 →
        # C 侧 `goto dispatch_err`，报错位置/顺序与树遍历一致。
        for _name in [n for n in dir(type(self)) if n.startswith("_cb_")]:
            if _name[4:] in _GUARD_EXEMPT:
                continue
            setattr(self, _name, self._guard(getattr(self, _name)))
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
            note_frames=CB_NOTE_FRAMES(self._cb_note_frames),
            debug_local=CB_DEBUG_LOCAL(self._cb_debug_local),
            list_append_many=CB_LIST_APPEND_MANY(self._cb_list_append_many),
            dict_setitem_many=CB_DICT_SETITEM_MANY(
                self._cb_dict_setitem_many),
            method_call=CB_METHOD_CALL(self._cb_method_call),
        )
        self.lib.jsvm_set_host(self.vm, byref(self._cb))

    # -- 调试支持（M40）-------------------------------------------------------

    def set_stmt_hook(self, hook) -> None:
        """装语句钩子：C 侧每条语句首指令执行前调 `hook(code_idx, line, nframes)`。

        钩子由调试会话提供；返回非 0 表示请求中止。**只读**——C 侧不因它改变
        任何执行状态，不装时开销只是一次指针判空。
        """
        self._stmt_hook = hook                 # 保活：C 侧持有函数指针
        cb = (CB_STMT_HOOK(self._trampoline_stmt_hook)
              if hook is not None else c_void_p(0))
        self._stmt_hook_cb = cb
        self.lib.jsvm_set_stmt_hook(self.vm, cb, None)

    def _trampoline_stmt_hook(self, ud, code_idx, line, nframes):
        hook = getattr(self, "_stmt_hook", None)
        if hook is None:
            return 0
        try:
            return int(hook(code_idx, line, nframes))
        except BaseException:                  # noqa: BLE001 - 钩子出错不该崩 VM
            return 0

    def dump_locals(self, depth: int, names: list, code, out: dict) -> None:
        """把 C 侧第 `depth` 帧的变量收进 `out`（名字 → 值）。

        值以**借用**形式从 C 侧传过来（宿主不得持有），这里当场转成 Python 对象
        存进 out，所以生命周期安全。
        """
        self._dump_names = list(names)
        self._dump_cells = list(code.cellvars) + list(code.freevars)
        self._dump_out = out
        self._dump_globals = False
        try:
            self.lib.jsvm_dump_locals(self.vm, depth)
        finally:
            self._dump_out = None

    def dump_globals(self, out: dict) -> None:
        """把 C 侧**已赋值**的全局变量收进 `out`。

        模块顶层的变量在 C 侧的全局数组里（宿主那侧只有内建影子表），
        所以「在模块顶层看变量」必须走这条路。
        """
        self._dump_names = list(self.names)
        self._dump_cells = []
        self._dump_out = out
        self._dump_globals = True
        try:
            self.lib.jsvm_dump_globals(self.vm)
        finally:
            self._dump_out = None

    def _cb_debug_local(self, slot, is_cell, name_idx, val):
        out = getattr(self, "_dump_out", None)
        if out is None:
            return 0
        try:
            if is_cell == 2:                 # 全局：slot 就是名字表下标
                names = self._dump_names
            elif is_cell:
                names = self._dump_cells
            else:
                names = self._dump_names
            if slot < len(names):
                name = names[slot]
                # 编译器内部临时槽、以及宿主侧的内建名字都不显示
                if not name.startswith("$tmp"):
                    out[name] = self.c2py(val[0])
            return 0
        except BaseException:                  # noqa: BLE001 - 转换失败跳过这一项
            return 0

    # -- 生命周期 -----------------------------------------------------------

    def destroy(self):
        if getattr(self, "vm", None):
            self.lib.jsvm_destroy(self.vm)
            self.vm = None
        # 钉住表里的指针在 `jsvm_destroy` 之后才真正失效，必须跟着清掉
        # （否则复用同一 host 时会拿旧指针误认回别的文本）。
        self._str_pinned.clear()

    # -- 值转换 ---------------------------------------------------------------

    def _new_handle(self, obj: Any) -> int:
        hid = self._next_handle
        self._next_handle += 1
        self._handles[hid] = obj
        self.lib.jsvm_track_handle(self.vm, hid)
        return hid

    def pin_str(self, v: JVal, obj: str) -> None:
        """登记一份**宿主原创、且与常量同寿命**的文本（M47 阶段二①）。

        只该用于 `_load` 里的字符串常量：它们由 `consts` 数组持有引用，
        在 `jsvm_destroy` 之前**绝不会被释放**，所以「按指针认回原件」是安全的
        ——不存在「指针被回收后又被别的文本复用」这一类错。

        效果：`打印("字面量")`、`长度("abc")`、`字["键"]` 这些「字面量当参数」
        的常见写法可以直接拿回原对象，省掉一次 ctypes 解码。
        这里**不额外持有**对象——`cmod.consts` 本来就一直活着。
        """
        self._str_pinned[int(v.v.u)] = obj

    def py2c(self, obj: Any) -> JVal:
        """Python 对象 → JVal。产生的 C 侧引用（字符串/句柄/FUNC）归接收槽位所有。"""
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
        if isinstance(obj, str):
            return self.str2c(obj)
        if isinstance(obj, CvmFunction):
            v = _jfunc_copy(obj._raw)          # 借用其引用再 retain 一次
            self.lib.jsvm_func_retain(self.vm, byref(v))
            return v
        return _jhost(self._new_handle(obj))

    def str2c(self, s: str) -> JVal:
        """Python 文本 → 原生字符串（M47 阶段二①）。

        用 ``surrogatepass`` 编码：Python 的 ``str`` 允许落单代理项
        （``chr(0xD800)``），默认编码会抛 ``UnicodeEncodeError``——而
        「能在树遍历里跑通的程序在 C VM 里也必须跑通」是硬约束。
        代理项的 UTF-8 三字节序仍与码点序一致（ED A0 80… < EE 80 80…），
        所以比较语义不受影响；回程也按 ``surrogatepass`` 解码，可逆。
        """
        raw = s.encode("utf-8", "surrogatepass")
        out = JVal()
        if self.lib.jsvm_str_new(self.vm, raw, len(raw), byref(out)) != 0:
            raise RunError("内部错误：原生字符串分配失败（内存不足）")
        return out

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
        if t == O.T_STR:
            ptr = int(v.v.u)
            # 常量文本：直接认回宿主原件（省一次 ctypes 解码）——见 `pin_str`
            pinned = self._str_pinned.get(ptr)
            if pinned is not None:
                return pinned
            # 直读内部头部拿字节数（见 `_JsStr` 的说明：省两次 FFI 往返）
            blen = _JsStr.from_address(ptr).blen
            # 快路径用 `c_char_p`（实测 0.52µs，`string_at` 要 1.13µs）：
            # 它按 C 字符串读，到第一个 NUL 为止。C 侧保证在 bytes[blen] 补 0，
            # 所以「读到的长度 == blen」就等价于「文本里没有 NUL」。含 NUL 的
            # 文本（\x00 能在基石里拼出来）长度会对不上，回退到带长度的读法。
            raw = c_char_p(ptr + _JS_STR_HDR).value
            if len(raw) != blen:
                raw = ctypes.string_at(ptr + _JS_STR_HDR, blen)
            return raw.decode("utf-8", "surrogatepass")
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

    def _guard(self, fn):
        """包一层：**冲刷失败后的紧接着那次回调**直接失败（M46 阶段一④）。

        为什么需要：C 侧「延迟合并写」在**调用回调之前**会先冲刷，而冲刷可能
        失败（例如字典键不可哈希），且那个失败的返回值会被 `HC` 宏丢掉。
        所以必须让紧接着的那次回调失败，C 侧才会 `goto dispatch_err` 把错误
        抛出来（否则错误被静默吞掉，后续语句还会照跑）。

        ⚠️ 两个坑（都实测踩过）：
        1. **不能拿 `self.pending` 当判据**——`note_frames`（记调用链）正是在
           `pending` 已设置时才被调，一拦就丢「调用链」（9 项对拍变红）。
        2. **不能做成「拦一次就清」**——冲刷失败后 C 侧会先释放待落盘的值，
           那会调 `release_handle`，而它的返回值在 C 侧被忽略，标志就被它
           白吃掉了（真正该失败的 `build_list` 反而放行）。
        所以：用**持续标志**（错误被取走时清），并**豁免错误处理类回调**。
        """
        def wrapper(*args):
            if self._flush_failed:
                return -1
            return fn(*args)
        return wrapper

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
            # 记下调用位置：这一支里如果有基石函数被回调（`列表.映射(基石函数)`、
            # 方法调用），重入建帧时要用它当调用点，否则调试器的调用栈会显示
            # 「第 0 行」（M40）。
            self._pending_call_site = (line, col)
            c2py = self.c2py
            f = c2py(fn[0])
            # 参数个数走特化分支（M46 阶段一②）：`长度(甲)`、`甲.追加(x)` 这类
            # 一元/二元调用占绝对多数，省掉 `range()` + 列表推导的那点开销。
            if nargs == 1:
                arglist = [c2py(args[0])]
            elif nargs == 2:
                arglist = [c2py(args[0]), c2py(args[1])]
            elif nargs <= 0:
                arglist = []
            else:
                arglist = [c2py(args[i]) for i in range(nargs)]
            if nkw:
                kwargs = {}
                names = self.names
                for k in range(nkw):
                    kwargs[names[kwname_idx[k]]] = c2py(kwvals[k])
            else:
                kwargs = None       # 无关键字参数时连空 dict 都不建
            r = R.call_value(f, arglist, kwargs, line=line, col=col,
                             filename=self.filename)
            # 结果写回：**直接写 JVal 的字段**，省掉构造一个 ctypes `Structure`
            # （多数调用返回的就是个数，构造 JVal 的那一步在剖析里占得不小）。
            # 只覆盖最常见的四类结果，其余仍走 `py2c`——语义（含越界报错）不变。
            # 注意每个分支都把值槽**写满**：C 侧那个 `JVal out` 在 C 里是
            # 未初始化的，只写 tag 会把垃圾留在值槽里。
            o = out[0]
            tr = type(r)
            if tr is int:
                if _I64_MIN <= r <= _I64_MAX:
                    o.tag = O.T_INT
                    o.v.i = r
                    return 0
                # 越界 → 落到 py2c，由 _jint 报中文错
            elif tr is bool:
                o.tag = O.T_TRUE if r else O.T_FALSE
                o.v.u = 0
                return 0
            elif tr is float:
                o.tag = O.T_FLOAT
                o.v.d = r
                return 0
            elif r is None:
                o.tag = O.T_NULL
                o.v.u = 0
                return 0
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

    def _pack_batch(self, chunk: list):
        """把一批**同类型**的值打成 JVal 数组的字节串；不适合批量就返回 None。

        M20 只认整数；**M46 阶段一①扩到浮点 / 布尔 / 空**；**M47 阶段二①
        扩到文本**（文本走 `_str_batch`，一次 FFI 建整批）。这几种的值槽
        都是 8 字节、与 `JVal` 布局一致（1 字节 tag + 7 字节填充 + 8 字节小端值），
        所以能一次 `pack` + `memmove` 灌进 C 数组，省掉逐个构造 ctypes
        `Structure` 的开销（那是纯 Python 层面的对象分配，最贵的一步）。

        **一趟**遍历（M41 的经验）：边判类型边装，发现混类型立即返回 None，
        不在前面先跑一遍 `all(...)` 预扫描（文本分支是例外，见 `_str_batch`）。

        返回 None 的几种情况（调用方回退逐个 `py2c`，**语义完全不变**）：
        混合类型 / 批次里出现别的类型 / 整数溢出 int64 / 不认识的类型。
        """
        n = len(chunk)
        t0 = type(chunk[0])
        flat: list = []
        if t0 is bool:
            # 布尔必须排在 int **前面**判：Python 里 bool 是 int 的子类，
            # 靠 `type()` 区分（`type(True) is bool`），不能用 isinstance。
            for v in chunk:
                if type(v) is not bool:
                    return None
                flat.append(O.T_TRUE if v else O.T_FALSE)
                flat.append(0)
            kind = "B7xq"
        elif t0 is int:
            for v in chunk:
                if type(v) is not int:
                    return None
                flat.append(O.T_INT)
                flat.append(v)
            kind = "B7xq"
        elif t0 is float:
            for v in chunk:
                if type(v) is not float:
                    return None
                flat.append(O.T_FLOAT)
                flat.append(v)
            kind = "B7xd"
        elif t0 is str:
            # M47 阶段二①起，文本在 C 侧是**原生字符串**（`JS_TAG_STR`）：
            # 它的字节由 C 侧持有，Python 这边拿不到指针，没法像整数/浮点那样
            # 用 `struct.pack` 直接拼出 JVal。所以改走 C 侧的**批量构造**
            # （一次 FFI 建整批，见 `_str_batch`）——「遍历一段文本」每批最多
            # 256 个元素，逐个建光 ctypes 往返就要 0.87µs × 256。
            return self._str_batch(chunk)
        else:
            return None
        try:
            return _batch_fmt(kind, n).pack(*flat)
        except (_struct.error, OverflowError):
            return None          # 整数越界等 → 回退（走 _jint 报中文错）

    def _str_batch(self, chunk: list):
        """一批文本 → 原生字符串的 JVal 数组字节串（M47 阶段二①）。

        与整数/浮点的批量路径不同：原生字符串的字节由 C 侧持有，Python 造不出
        JVal，所以走 `jsvm_str_new_batch` —— 把整批 UTF-8 拼成一块连续的 `buf`，
        配上 n+1 个偏移，**一次 FFI** 建完整批（逐个建是一批 256 次往返）。

        语义与逐个 `py2c` 完全等价（同样 UTF-8 + `surrogatepass` 编解码）。
        混类型返回 None 让调用方回退：**注意这里必须先编码完再调 C**——
        半路返回时 C 侧还没建任何东西，才不会有半个批次漏掉。
        """
        n = len(chunk)
        if n == 0:
            return None
        encoded = []
        for v in chunk:
            if type(v) is not str:
                return None
            encoded.append(v.encode("utf-8", "surrogatepass"))
        buf = b"".join(encoded)
        offs = _array.array("i", [0])          # 'i' = C int = int32（与 C 侧一致）
        acc = 0
        for e in encoded:
            acc += len(e)
            offs.append(acc)
        out = (JVal * n)()
        rc = self.lib.jsvm_str_new_batch(
            self.vm, buf, c_void_p(offs.buffer_info()[0]), n, out)
        if rc != 0:
            raise RunError("内部错误：原生字符串批量分配失败（内存不足）")
        return ctypes.string_at(ctypes.addressof(out), n * _JVAL_SIZE)

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
            # 快路径（M20 引入，**M46 泛化**）：整批同类型时一次 pack + memmove
            # 写入 C 侧数组，省掉逐个构造 JVal 的 ctypes 开销。
            # M20 只认整数（实测快 ~9x）；M46 阶段一把浮点 / 布尔 / 空 / 文本
            # 也纳入。混合类型 / 大端平台 / 溢出 → `_pack_batch` 给 None，
            # 回退逐个 py2c，**语义完全不变**（三执行器对拍钉住）。
            if chunk and _LITTLE_ENDIAN:
                blob = self._pack_batch(chunk)
                if blob is not None:
                    ctypes.memmove(out, blob, len(chunk) * _JVAL_SIZE)
                    n[0] = len(chunk)
                    return 0
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
        # `抛出 值`：规范化成异常对象，原值所有权转移给返回的异常句柄。
        # **例外**：调试器的「退出」(`DebugQuit`) 不是给用户看的异常，
        # 不能被包装（否则用户程序一句 `捕获` 就把它接住了，M39/M40）。
        # Python VM 在 THROW 处有同样的特判，这里对齐。
        try:
            v = self.c2py(value[0])
            if isinstance(v, DebugQuit):
                out[0] = self.py2c(v)
                return 0
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
        """异常是不是「信号」（M40 起含调试器的 `退出`）。

        约定与 C 侧 `dispatch_exception` 一致：
          0 = 不是信号（普通异常，`捕获` 可以接）
          1 = `中断`（循环接）
          2 = `继续`（循环接）
          3 = **卸载类**（`退出`）：只穿 `最终` 块，**既不被 `捕获` 接住、
              也不被循环接住**——与 Python VM 的 `_UNWIND_SIGNALS` 同一语义。
        """
        try:
            e = self.c2py(exc[0])
            if isinstance(e, RunBreak):
                out[0] = 1
            elif isinstance(e, RunContinue):
                out[0] = 2
            elif isinstance(e, DebugQuit):
                out[0] = 3
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

    def _cb_note_frames(self, flat, depth):
        """记下「出错那一刻的调用链」（M40）。

        C 侧在建帧时记了每帧的**调用点**，出错时把帧栈（除主帧）拍成扁平数组
        递过来，这里翻成 `errors.Frame` 挂到 pending 异常上——于是 C VM 的函数内
        报错也能印出「调用链（从外到内）」，与树遍历 / Python VM 长得一样。

        口径与 `vm._frames_to_trace` **逐字对齐**：调用点而不是当前执行行、
        跳过模块主帧、匿名函数的 code 名显示成「匿名函数」。
        """
        try:
            chain = []
            codes = self.cmod.codes
            for i in range(depth):
                code_idx, line, col = (flat[i * 3], flat[i * 3 + 1],
                                       flat[i * 3 + 2])
                name = (codes[code_idx].name
                        if 0 <= code_idx < len(codes) else "?")
                if name.startswith("<"):
                    name = "匿名函数"
                chain.append(Frame(name, line or None, col or None))
            if self.pending is not None and self.pending.trace is None:
                self.pending.trace = chain
            return 0
        except BaseException:            # noqa: BLE001 - 记现场失败不该改变语义
            return 0

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

    def _cb_list_append_many(self, hid, vals, n):
        """批量追加落盘（M46 阶段一③）：把 C 侧攒下的 n 个值一次追加到列表。

        C 侧「追加延迟合并写」把「循环里追加 N 次」的 N 次宿主回调压成 1 次
        （实测每次回调固定约 2.4µs，是 C VM 最大的固定开销）。

        ⚠️ **只有 C 侧确认过「目标是纯 Python 列表」的批次才走这里**
        （见 `JsVm.islist`：那标记只在 `build_list` 造列表时置位）。
        所以这里直接 `extend` 即可——`list.extend` 不会失败，也就不存在
        「这一批报错该算哪一行」的歧义；不是 list 的目标仍走单条
        `_cb_list_append`（同步回调），错误语义一模一样。

        快路径：整批都是整数时一次 `unpack` + `extend`，省掉逐个 `c2py`——
        「循环追加数字」正是这一步的主战场。
        """
        try:
            n = int(n)
            if n <= 0:
                return 0
            o = self._handles[int(hid)]
            if _LITTLE_ENDIAN:
                raw = ctypes.string_at(vals, n * _JVAL_SIZE)
                flat = _batch_fmt("B7xq", n).unpack(raw)
                if flat[0::2].count(O.T_INT) == n:
                    o.extend(flat[1::2])
                    return 0
            c2py = self.c2py
            o.extend([c2py(vals[i]) for i in range(n)])
            return 0
        except BaseException as e:
            # 冲刷失败：让紧接着那次回调也失败，错误才会被 C 侧接住（见 _guard）
            self._flush_failed = True
            return self._fail(e)

    def _cb_method_call(self, obj, name_idx, args, nargs, out,
                        gline, gcol, line, col):
        """`X.方法(实参)` —— 一次回调做完「取方法 + 调用」（M48 阶段三②）。

        语义必须与「先 `_cb_getattr` 再 `_cb_call`」**逐字一致**：
        - 取属性失败 → 用 **GET_ATTR 的行列**（gline/gcol）报错
        - 调用失败   → 用 **CALL 的行列**（line/col）报错
        所以这里没有走 `call_value` 的「取属性」分支，而是自己拆成两步、
        各带各的行列——这样报错文本与两次回调的老路径完全相同。

        省掉的是：一次 ctypes 编组（实测约 4µs）+ 一次 `_BoundMethod` 句柄
        往返。生成.jsh 里「开头是」被调 88286 次，正是靠这条才把该项目
        那 47% 的回调耗时压下来。
        """
        try:
            c2py = self.c2py
            o = c2py(obj[0])
            name = self.names[name_idx]
            m = R.get_attr(o, name, line=gline, col=gcol,
                           filename=self.filename)
            arglist = [c2py(args[i]) for i in range(nargs)]
            r = R.call_value(m, arglist, None, line=line, col=col,
                             filename=self.filename)
            out[0] = self.py2c(r)
            return 0
        except BaseException as e:
            return self._fail(e)

    def _cb_dict_setitem_many(self, hid, kvs, lc, n):
        """批量字典写入落盘（M46 阶段一④）：把 C 侧攒下的 n 条 `字[键] = 值` 一次落盘。

        ⚠️ **只有 C 侧确认过「目标是纯 Python 字典」的批次才走这里**
        （`JsVm.hkind == HK_DICT`，只在 `build_dict` 造字典时置位）。

        与 ③ 的 `list_append_many` 有一处关键不同：**字典写入可能失败**
        （键不可哈希），所以**行列是按项传进来的**，走 `R.set_item` 逐条
        落盘、报错位置与逐条写入**完全一致**——不能拿整批第一条的行号糊。

        快路径：整批「键和值都是整数」时一次 `unpack` 后直接赋值——
        整数必定可哈希，赋值不可能失败，所以可以不套 `R.set_item`
        （省掉每条一次 Python 函数调用）。顺序不变，同键后者覆盖 ✅。
        """
        try:
            n = int(n)
            if n <= 0:
                return 0
            o = self._handles[int(hid)]
            if _LITTLE_ENDIAN:
                raw = ctypes.string_at(kvs, n * 2 * _JVAL_SIZE)
                flat = _batch_fmt("B7xq", n * 2).unpack(raw)
                if flat[0::2].count(O.T_INT) == n * 2:
                    vals = flat[1::2]
                    for i in range(0, 2 * n, 2):
                        o[vals[i]] = vals[i + 1]
                    return 0
            c2py = self.c2py
            for i in range(n):
                R.set_item(o, c2py(kvs[i * 2]), c2py(kvs[i * 2 + 1]),
                           line=int(lc[i * 2]), col=int(lc[i * 2 + 1]),
                           filename=self.filename)
            return 0
        except BaseException as e:
            # 冲刷失败：让紧接着那次回调也失败，错误才会被 C 侧接住（见 _guard）
            self._flush_failed = True
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
        # 文案走 runtime 的单一来源（M40）：树遍历 / 字节码 VM 报的是同一条，
        # 对拍比的是报错全文，差一个字就判成「执行器不一致」。
        self.pending = R.unbound_local_error(
            name, line=line, col=col, filename=self.filename,
            hint=code.local_hints.get(slot))
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
        """Python 高阶函数回调基石函数（重入 C VM）。

        `_pending_call_site` 是**宿主记下的最近一次调用位置**（`_cb_call` 里
        从 C 侧收到的 line/col）：方法调用、`列表.映射(基石函数)` 这类都是从
        `_cb_call` 转进重入的，重入建帧时用它当「本帧的调用点」——否则调试器
        的调用栈最外层会显示「第 0 行」（M40）。
        """
        call_line, call_col = getattr(self, "_pending_call_site", (0, 0))
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
            c_knames, c_kwvals, len(kw_names), byref(out), call_line, call_col)
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
        # 错误被取走 = 冲刷失败的「封锁」到此结束（见 _guard）
        self._flush_failed = False
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

        # 常量池：数字内联，文本走原生字符串，其余走句柄
        consts = (JVal * max(len(cmod.consts), 1))()
        for i, c in enumerate(cmod.consts):
            v = self.host.py2c(c)
            consts[i] = v
            # 文本常量额外登记进「钉住表」：它们与常量数组同寿命，此后
            # `c2py` 遇到它们可直接认回原件，省掉一次 ctypes 解码
            # （见 `CvmHost.pin_str`）。
            if type(c) is str:
                self.host.pin_str(v, c)

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
        self._load_stmt_marks()

    def _load_stmt_marks(self) -> None:
        """把「语句起始表」交给 C 侧（M40，调试用）。

        与指令流等长，第 i 项是「第 i 条指令所属语句的行号」，0 = 不是语句首。
        单独一张表而不是往指令里加字段：指令宽度（6 个 int32）是跨语言字节码
        格式的一部分，为一个调试功能改它不划算（见 `opcodes.Code.stmt_marks`）。
        """
        lib = self.host.lib
        marks: list[int] = []
        for code in self.cmod.codes:
            marks.extend(code.stmt_marks)
        c_marks = (c_int32 * max(len(marks), 1))(*marks)
        self._keep.append(c_marks)
        lib.jsvm_set_stmt_marks(self.host.vm, c_marks, len(marks))

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

    # M48 阶段三②：**只有这里**打开方法调用融合——`METHOD_CALL` 是 C VM
    # 专属指令（Python VM / Node / Rust 不认识它）。别的入口一律用默认值
    # （关闭），于是它们拿到的产物与以前逐字节相同。
    cmod = compile_source(source, filename, fuse_method_call=True)
    runner = CvmRunner(cmod, filename)
    try:
        return runner.run()
    finally:
        runner.close()
