# -*- coding: utf-8 -*-
"""基石字节码指令集 —— **唯一事实来源**。

C 侧的 ``cvm/include/opcodes.h`` 由本模块自动生成
（``python cvm/build.py --gen-header``），保证两侧指令号永远同步。

指令格式：每条 6 个字段 ``(op, a, b, c, line, col)``。
- ``a/b/c``：操作数，最多三个（``IMPORT`` 需要 名字/是否来自python/别名 三个）
- ``line/col``：源码位置，报错时原样回传宿主，保证中文报错行列准确
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 值标签（JVal.tag），与 cvm/include/jval.h 保持一致
# ---------------------------------------------------------------------------

T_UNSET = 0
T_NULL = 1
T_FALSE = 2
T_TRUE = 3
T_INT = 4
T_FLOAT = 5
T_HOST = 6
T_CELL = 7
T_FUNC = 8
T_ITER = 9

TAG_NAMES = {
    T_UNSET: "UNSET", T_NULL: "NULL", T_FALSE: "FALSE", T_TRUE: "TRUE",
    T_INT: "INT", T_FLOAT: "FLOAT", T_HOST: "HOST", T_CELL: "CELL",
    T_FUNC: "FUNC", T_ITER: "ITER",
}


# ---------------------------------------------------------------------------
# 指令
# ---------------------------------------------------------------------------

class Op:
    """指令号。用类属性而非 IntEnum：C 头生成与调试都要按名字取号。"""

    HALT = 0
    LOAD_CONST = 1
    LOAD_GLOBAL = 2
    STORE_GLOBAL = 3
    LOAD_FAST = 4
    STORE_FAST = 5
    LOAD_DEREF = 6
    STORE_DEREF = 7
    LOAD_CELL = 8
    POP_TOP = 9
    DUP_TOP = 10
    ROT_TWO = 11
    ROT_THREE = 12
    BIN_OP = 13
    UNARY_OP = 14
    COMPARE = 15
    JUMP = 16
    POP_JUMP_IF_FALSE = 17
    POP_JUMP_IF_TRUE = 18
    JUMP_IF_FALSE_OR_POP = 19
    CALL = 20
    RETURN = 21
    MAKE_FUNCTION = 22
    BUILD_LIST = 23
    BUILD_DICT = 24
    GET_ATTR = 25
    SET_ATTR = 26
    GET_ITEM = 27
    SET_ITEM = 28
    GET_ITER = 29
    FOR_ITER = 30
    IMPORT = 31
    DUP_TWO = 32           # 复制栈顶两个（复合下标赋值用）
    LOOP_SETUP = 33        # 「循环 n 次」取次数并转成整数
    SETUP_LOOP = 34        # 登记循环处理器：a=继续目标 b=中断目标
    POP_BLOCK = 35         # 注销循环处理器
    BREAK_LOOP = 36        # 中断（跨函数传播，与树遍历一致）
    CONTINUE_LOOP = 37     # 继续（跨函数传播，与树遍历一致）
    STORE_LAST = 38        # 顶层表达式语句：弹出并存为「最近的值」（REPL 回显）
    # -- M5a 异常处理 --
    SETUP_TRY = 39         # 登记异常处理器：a=捕获入口 b=finally入口（-1 无）
    POP_TRY = 40           # 注销异常处理器（正常离开 try 块）
    THROW = 41             # 弹出值，规范化成异常后抛出
    CHECK_SIGNAL = 42      # 栈顶是循环信号(中断/继续)则弹出并跳 a（进入未匹配路径）
    MATCH_EXC = 43         # [err,err,cond]→[err,布尔]；信号一律不匹配
    END_FINALLY = 44       # finally 末尾：有挂起返回则继续返回，否则继续执行
    # -- M5b 面向对象 --
    BUILD_CLASS = 45       # 栈 [基类?, 方法...] → 类；a=方法数 b=是否有基类
    # -- M7 表达力 --
    UNPACK = 46            # 解包：弹出栈顶可迭代，压 a 个元素（第一个在栈顶）
    # -- M11 性能 --
    LIST_APPEND = 47       # 列表追加：[obj, val] → obj.追加(val)，弹 2 压 0
    # -- M18 表达力 --
    BUILD_SLICE = 48       # 切片：按 a 的位标志从栈上取分量构造 slice，压 1


#: 指令号 → 名字（按号排序，C 头生成依赖此顺序）
OP_NAMES: list[str] = [
    name for name, _ in sorted(
        ((k, v) for k, v in vars(Op).items() if not k.startswith("_")),
        key=lambda kv: kv[1])
]

#: 名字 → 指令号
OP_BY_NAME: dict[str, int] = {name: i for i, name in enumerate(OP_NAMES)}

#: 每条指令的操作数个数（反汇编用）
OP_ARITY = {
    Op.HALT: 0,
    Op.LOAD_CONST: 1,
    Op.LOAD_GLOBAL: 1,
    Op.STORE_GLOBAL: 1,
    Op.LOAD_FAST: 1,
    Op.STORE_FAST: 1,
    Op.LOAD_DEREF: 1,
    Op.STORE_DEREF: 1,
    Op.LOAD_CELL: 1,
    Op.POP_TOP: 0,
    Op.DUP_TOP: 0,
    Op.ROT_TWO: 0,
    Op.ROT_THREE: 0,
    Op.BIN_OP: 1,
    Op.UNARY_OP: 1,
    Op.COMPARE: 1,
    Op.JUMP: 1,
    Op.POP_JUMP_IF_FALSE: 1,
    Op.POP_JUMP_IF_TRUE: 1,
    Op.JUMP_IF_FALSE_OR_POP: 1,
    Op.CALL: 2,
    Op.RETURN: 0,
    Op.MAKE_FUNCTION: 2,
    Op.BUILD_LIST: 1,
    Op.BUILD_DICT: 1,
    Op.GET_ATTR: 1,
    Op.SET_ATTR: 1,
    Op.GET_ITEM: 0,
    Op.SET_ITEM: 0,
    Op.GET_ITER: 0,
    Op.FOR_ITER: 1,
    Op.IMPORT: 3,
    Op.DUP_TWO: 0,
    Op.LOOP_SETUP: 1,
    Op.SETUP_LOOP: 2,
    Op.POP_BLOCK: 0,
    Op.BREAK_LOOP: 0,
    Op.CONTINUE_LOOP: 0,
    Op.STORE_LAST: 0,
    Op.SETUP_TRY: 2,
    Op.POP_TRY: 0,
    Op.THROW: 0,
    Op.CHECK_SIGNAL: 1,
    Op.MATCH_EXC: 0,
    Op.END_FINALLY: 0,
    Op.BUILD_CLASS: 2,
    Op.UNPACK: 1,
    Op.LIST_APPEND: 0,
    Op.BUILD_SLICE: 1,
}


# --- 子操作码 ---------------------------------------------------------------

class BinOp:
    ADD = 0
    SUB = 1
    MUL = 2
    DIV = 3
    FLOORDIV = 4
    MOD = 5
    POW = 6


SYMBOL_TO_BINOP = {
    "+": BinOp.ADD, "-": BinOp.SUB, "*": BinOp.MUL, "/": BinOp.DIV,
    "//": BinOp.FLOORDIV, "%": BinOp.MOD, "**": BinOp.POW,
}
BINOP_TO_SYMBOL = {v: k for k, v in SYMBOL_TO_BINOP.items()}


class UnOp:
    NEG = 0
    NOT = 1


SYMBOL_TO_UNOP = {"-": UnOp.NEG, "非": UnOp.NOT}


class CmpOp:
    EQ = 0
    NE = 1
    LT = 2
    GT = 3
    LE = 4
    GE = 5


SYMBOL_TO_CMPOP = {
    "==": CmpOp.EQ, "!=": CmpOp.NE, "<": CmpOp.LT,
    ">": CmpOp.GT, "<=": CmpOp.LE, ">=": CmpOp.GE,
}
CMPOP_TO_SYMBOL = {v: k for k, v in SYMBOL_TO_CMPOP.items()}


# ---------------------------------------------------------------------------
# 指令 / 代码对象 / 编译结果
# ---------------------------------------------------------------------------

@dataclass
class Instr:
    op: int
    a: int = 0
    b: int = 0
    c: int = 0
    line: int = 0
    col: int = 0

    def as_tuple(self) -> tuple[int, int, int, int, int, int]:
        return (self.op, self.a, self.b, self.c, self.line, self.col)


@dataclass
class Code:
    """一个函数（或模块主代码）的字节码。"""

    name: str = "<模块>"
    params: list[str] = field(default_factory=list)
    param_idx: list[int] = field(default_factory=list)  # 参数名在 names[] 的下标
    #: 每个参数分到哪个位置：二选一，另一个为 -1。
    #: 被内层函数捕获的参数（单元变量）不在局部槽里，
    #: 因此「第 i 个参数 → 第 i 个局部槽」并不成立，必须显式记录。
    param_local: list[int] = field(default_factory=list)
    param_cell: list[int] = field(default_factory=list)
    instrs: list[Instr] = field(default_factory=list)
    nlocals: int = 0
    local_names: list[str] = field(default_factory=list)
    cellvars: list[str] = field(default_factory=list)   # 有序
    freevars: list[str] = field(default_factory=list)   # 有序
    #: 局部槽号 → 提示文本。仅给「本函数赋过值、外层也有同名变量」的槽位，
    #: 用于把「找不到名字」升级成一句说得清原因的中文提示。
    local_hints: dict[int, str] = field(default_factory=dict)
    firstlineno: int = 1

    @property
    def ncells(self) -> int:
        return len(self.cellvars) + len(self.freevars)


@dataclass
class CompiledModule:
    """一个模块编译后的全部产物；C 侧只需这几张扁平数组。"""

    consts: list[Any] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    kw_names: list[list[str]] = field(default_factory=list)
    codes: list[Code] = field(default_factory=list)
    main: int = 0
    filename: str = "<输入>"


# ---------------------------------------------------------------------------
# 反汇编（调试用）
# ---------------------------------------------------------------------------

def disassemble(cmod: CompiledModule) -> str:
    out: list[str] = []
    for ci, code in enumerate(cmod.codes):
        head = f"── 代码对象 #{ci} {code.name}"
        if code.params:
            head += f"（参数：{'、'.join(code.params)}）"
        out.append(head)
        if code.local_names:
            out.append(f"   局部变量：{'、'.join(code.local_names)}")
        if code.cellvars:
            out.append(f"   单元变量：{'、'.join(code.cellvars)}")
        if code.freevars:
            out.append(f"   自由变量：{'、'.join(code.freevars)}")
        for i, ins in enumerate(code.instrs):
            name = OP_NAMES[ins.op]
            arity = OP_ARITY[ins.op]
            parts = []
            if arity >= 1:
                parts.append(_operand_text(cmod, ins.op, ins.a, 0))
            if arity >= 2:
                parts.append(_operand_text(cmod, ins.op, ins.b, 1))
            if arity >= 3:
                parts.append(_operand_text(cmod, ins.op, ins.c, 2))
            out.append(f"   {i:>4} {name:<20}{' '.join(parts)}"
                       f"   @{ins.line}:{ins.col}")
        out.append("")
    return "\n".join(out)


def _operand_text(cmod: CompiledModule, op: int, value: int,
                  which: int) -> str:
    if op == Op.LOAD_CONST:
        return f"const[{value}]={cmod.consts[value]!r}"
    if op in (Op.LOAD_GLOBAL, Op.STORE_GLOBAL, Op.GET_ATTR, Op.SET_ATTR):
        return f"name[{value}]={_name(cmod, value)!r}"
    if op == Op.IMPORT:
        if which == 0:
            return f"name[{value}]={_name(cmod, value)!r}"
        if which == 1:
            return "从python" if value else "标准库"
        return "无别名" if value < 0 else f"别名={_name(cmod, value)!r}"
    if op == Op.LOAD_FAST or op == Op.STORE_FAST:
        code = _owner_code(cmod, op)
        return f"local[{value}]"
    if op in (Op.LOAD_DEREF, Op.STORE_DEREF, Op.LOAD_CELL):
        return f"cell[{value}]"
    if op == Op.BIN_OP:
        return BINOP_TO_SYMBOL.get(value, str(value))
    if op == Op.UNARY_OP:
        return "取负" if value == UnOp.NEG else "非"
    if op == Op.COMPARE:
        return {v: k for k, v in SYMBOL_TO_CMPOP.items()}.get(value, str(value))
    if op in (Op.JUMP, Op.POP_JUMP_IF_FALSE, Op.POP_JUMP_IF_TRUE,
              Op.JUMP_IF_FALSE_OR_POP, Op.FOR_ITER):
        return f"→{value}"
    if op == Op.CALL:
        if which == 0:
            return f"args={value}"
        return "无关键字" if value < 0 else f"kw={cmod.kw_names[value]}"
    if op == Op.MAKE_FUNCTION:
        if which == 0:
            return f"code#{value}"
        return f"cells={value}"
    if op == Op.BUILD_LIST:
        return f"n={value}"
    if op == Op.BUILD_DICT:
        return f"pairs={value}"
    return str(value)


def _name(cmod: CompiledModule, idx: int) -> str:
    if 0 <= idx < len(cmod.names):
        return cmod.names[idx]
    return f"?{idx}"


def _owner_code(cmod: CompiledModule, op: int) -> Optional[Code]:
    """反汇编时用于把局部槽号解析成名字（尽力而为）。"""
    for code in cmod.codes:
        for ins in code.instrs:
            if ins.op == op:
                return code
    return None
