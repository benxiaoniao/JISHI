# -*- coding: utf-8 -*-
"""基石字节码编译器（M4）：AST → CompiledModule。

编译分三步：

1. **收集**：遍历 AST，为每个作用域记录 ``bound``（被绑定的名字）与
   ``used``（被读取的名字），同时建好作用域树。
2. **定种**：后序遍历作用域树（最深的子作用域先算），判定哪些名字是
   局部变量、哪些要提升为闭包单元、哪些是全局。
3. **发射**：逐节点生成指令，每条指令带源码行列。

第 2 步的**后序**顺序是关键：必须先处理内层，才能在外层发出任何指令
之前确定「这个变量要变成单元」，否则会出现前半段用 ``STORE_FAST``、
后半段用 ``LOAD_DEREF`` 的分裂。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from . import ast_nodes as A
from . import opcodes as O
from .errors import JishiError

MODULE_SCOPE = "module"
FUNC_SCOPE = "function"


# ---------------------------------------------------------------------------
# 作用域
# ---------------------------------------------------------------------------

class _Scope:
    __slots__ = ("kind", "name", "bound", "used", "cellvars", "freevars",
                 "children", "parent")

    def __init__(self, kind: str, name: str = "<模块>",
                 parent: Optional["_Scope"] = None):
        self.kind = kind
        self.name = name
        self.bound: set[str] = set()
        self.used: set[str] = set()
        self.cellvars: set[str] = set()   # 本作用域绑定、被内层引用
        self.freevars: set[str] = set()   # 本作用域引用、由外层绑定
        self.children: list["_Scope"] = []
        self.parent = parent

    @property
    def cells(self) -> list[str]:
        """单元数组布局：sorted(cellvars) ++ sorted(freevars)。"""
        return sorted(self.cellvars) + sorted(self.freevars)

    def cell_index(self, name: str) -> int:
        cv = sorted(self.cellvars)
        if name in self.cellvars:
            return cv.index(name)
        return len(cv) + sorted(self.freevars).index(name)


# ---------------------------------------------------------------------------
# 第 1 步：收集
# ---------------------------------------------------------------------------

def _collect_stmts(stmts, scope: _Scope, func_scopes: dict[int, _Scope]):
    for st in stmts:
        _collect_stmt(st, scope, func_scopes)


def _collect_stmt(st: A.Node, scope: _Scope,
                  func_scopes: dict[int, _Scope]):
    if isinstance(st, A.Assign):
        if isinstance(st.target, A.Name):
            scope.bound.add(st.target.id)
            if st.op != "=":          # 复合赋值要先读旧值
                scope.used.add(st.target.id)
        elif isinstance(st.target, A.TargetList):
            for name_node in st.target.names:
                scope.bound.add(name_node.id)
        _collect_expr(st.value, scope, func_scopes)
    elif isinstance(st, A.ExprStmt):
        _collect_expr(st.expr, scope, func_scopes)
    elif isinstance(st, A.If):
        for test, body in st.branches:
            _collect_expr(test, scope, func_scopes)
            _collect_stmts(body, scope, func_scopes)
        if st.orelse is not None:
            _collect_stmts(st.orelse, scope, func_scopes)
    elif isinstance(st, A.For):
        _collect_expr(st.iter, scope, func_scopes)
        if isinstance(st.target, A.Name):
            scope.bound.add(st.target.id)
        _collect_stmts(st.body, scope, func_scopes)
    elif isinstance(st, A.Loop):
        _collect_expr(st.times, scope, func_scopes)
        _collect_stmts(st.body, scope, func_scopes)
    elif isinstance(st, A.While):
        _collect_expr(st.test, scope, func_scopes)
        _collect_stmts(st.body, scope, func_scopes)
    elif isinstance(st, A.FuncDef):
        scope.bound.add(st.name)
        for d in getattr(st, "defaults", []) or []:
            if d is not None:      # 默认值在定义处作用域求值（M7）
                _collect_expr(d, scope, func_scopes)
        sub = _Scope(FUNC_SCOPE, st.name, scope)
        sub.bound.update(st.params)
        scope.children.append(sub)
        func_scopes[id(st)] = sub
        _collect_stmts(st.body, sub, func_scopes)
    elif isinstance(st, A.ClassDef):
        scope.bound.add(st.name)
        if st.base is not None:
            scope.used.add(st.base)
        # 方法体是独立函数作用域
        for node in st.body:
            if isinstance(node, A.FuncDef):
                sub = _Scope(FUNC_SCOPE, node.name, scope)
                sub.bound.update(node.params)
                scope.children.append(sub)
                func_scopes[id(node)] = sub
                _collect_stmts(node.body, sub, func_scopes)
            elif isinstance(node, A.Assign):
                # 类变量（M23.3）：值在「定义类的外层作用域」里求值，
                # 所以里面用到的名字算外层的作用域引用。
                _collect_expr(node.value, scope, func_scopes)
    elif isinstance(st, A.Return):
        if st.value is not None:
            _collect_expr(st.value, scope, func_scopes)
    elif isinstance(st, A.Import):
        scope.bound.add(st.alias or st.name)


def _collect_expr(e: A.Node, scope: _Scope, func_scopes: dict[int, _Scope]):
    if isinstance(e, A.Name):
        scope.used.add(e.id)
    elif isinstance(e, (A.BinOp,)):
        _collect_expr(e.left, scope, func_scopes)
        _collect_expr(e.right, scope, func_scopes)
    elif isinstance(e, A.UnaryOp):
        _collect_expr(e.operand, scope, func_scopes)
    elif isinstance(e, A.BoolOp):
        for v in e.values:
            _collect_expr(v, scope, func_scopes)
    elif isinstance(e, A.Lambda):
        # 匿名函数（M24.4）在表达式位置出现，也是一个嵌套函数作用域，
        # 收集方式与 FuncDef 一致（这样闭包捕获、freevars 都对）。
        sub = _Scope(FUNC_SCOPE, e.name, scope)
        sub.bound.update(e.params)
        scope.children.append(sub)
        func_scopes[id(e)] = sub
        for d in e.defaults or []:
            if d is not None:
                _collect_expr(d, scope, func_scopes)
        _collect_stmts(e.body, sub, func_scopes)
    elif isinstance(e, A.Compare):
        _collect_expr(e.left, scope, func_scopes)
        for c in e.comparators:
            _collect_expr(c, scope, func_scopes)
    elif isinstance(e, A.Call):
        _collect_expr(e.func, scope, func_scopes)
        for a in e.args:
            _collect_expr(a, scope, func_scopes)
        for _name, v in e.keywords:
            _collect_expr(v, scope, func_scopes)
    elif isinstance(e, A.Attr):
        _collect_expr(e.obj, scope, func_scopes)
    elif isinstance(e, A.Subscript):
        _collect_expr(e.obj, scope, func_scopes)
        _collect_expr(e.index, scope, func_scopes)
    elif isinstance(e, A.List):
        for el in e.elements:
            _collect_expr(el, scope, func_scopes)
    elif isinstance(e, A.Dict):
        for k in e.keys:
            _collect_expr(k, scope, func_scopes)
        for v in e.values:
            _collect_expr(v, scope, func_scopes)
    elif isinstance(e, A.Comprehension):
        _collect_expr(e.iter, scope, func_scopes)
        if isinstance(e.target, A.Name):
            scope.bound.add(e.target.id)     # 循环变量与「遍历」语句一致
        if e.elt is not None:
            _collect_expr(e.elt, scope, func_scopes)
        if e.key is not None:
            _collect_expr(e.key, scope, func_scopes)
        if e.value is not None:
            _collect_expr(e.value, scope, func_scopes)
        if e.condition is not None:
            _collect_expr(e.condition, scope, func_scopes)


# ---------------------------------------------------------------------------
# 第 2 步：定种（后序）
# ---------------------------------------------------------------------------

def _resolve(scope: _Scope) -> None:
    for child in scope.children:
        _resolve(child)
    if scope.kind != FUNC_SCOPE:
        return
    for name in sorted(scope.used):
        if name in scope.bound:
            continue
        chain = [scope]
        found = None
        e = scope.parent
        while e is not None:
            if name in e.bound or name in e.cellvars:
                found = e
                break
            chain.append(e)
            e = e.parent
        if found is None or found.kind != FUNC_SCOPE:
            continue          # 全局（内建函数、导入的名字）
        found.cellvars.add(name)
        for s in chain:
            s.freevars.add(name)


# ---------------------------------------------------------------------------
# 第 3 步：发射
# ---------------------------------------------------------------------------

@dataclass
class _CodeCtx:
    """发射函数体时保存外层上下文。"""

    code: O.Code
    scope: _Scope


class _Emitter:
    def __init__(self, cmod: O.CompiledModule, func_scopes: dict[int, _Scope],
                 root: _Scope, fuse_method_call: bool = False):
        self.cmod = cmod
        self.func_scopes = func_scopes
        self.code: O.Code = cmod.codes[cmod.main]
        self.scope: _Scope = root
        #: 是否把 `X.方法(实参)` 编译成融合指令 `METHOD_CALL`（M48 阶段三②）。
        #: **只有 C VM 打开**（见 `interpreter` 的编译入口）：Python VM 与
        #: Node/Rust 宿主不认识这条指令，给它们发就是「未知指令」。
        #: 关闭时照旧发 `GET_ATTR` + `CALL`，语义完全相同、只是慢一点。
        self.fuse_method_call = fuse_method_call
        self._const_cache: dict[tuple, int] = {}
        self._name_cache: dict[str, int] = {}
        self._tmp_seq = 0

    # -- 表管理 -------------------------------------------------------------

    def const_idx(self, v: Any) -> int:
        key = (type(v).__name__, v)
        hit = self._const_cache.get(key)
        if hit is not None:
            return hit
        self.cmod.consts.append(v)
        idx = len(self.cmod.consts) - 1
        self._const_cache[key] = idx
        return idx

    def name_idx(self, name: str) -> int:
        hit = self._name_cache.get(name)
        if hit is not None:
            return hit
        self.cmod.names.append(name)
        idx = len(self.cmod.names) - 1
        self._name_cache[name] = idx
        return idx

    def kw_idx(self, names: list[str]) -> int:
        self.cmod.kw_names.append(names)
        return len(self.cmod.kw_names) - 1

    # -- 指令发射 -----------------------------------------------------------

    def emit(self, op: int, node: A.Node = None, a: int = 0, b: int = 0,
             c: int = 0) -> int:
        line, col = (1, 1)
        if node is not None:
            line = getattr(node, "line", 1) or 1
            col = getattr(node, "col", 1) or 1
        self.code.instrs.append(O.Instr(op, a, b, c, line, col))
        return len(self.code.instrs) - 1

    def here(self) -> int:
        return len(self.code.instrs)

    def patch(self, idx: int, target: int, field: str = "a") -> None:
        setattr(self.code.instrs[idx], field, target)

    def new_tmp(self) -> int:
        """在当前代码对象里申请一个隐藏局部槽（如循环计数器）。"""
        slot = self.code.nlocals
        self.code.nlocals += 1
        self._tmp_seq += 1
        self.code.local_names.append(f"$tmp{self._tmp_seq}")
        return slot

    # -- 名字访问 -----------------------------------------------------------

    def load_name(self, name: str, node: A.Node) -> int:
        scope = self.scope
        if name in scope.cellvars or name in scope.freevars:
            return self.emit(O.Op.LOAD_DEREF, node, scope.cell_index(name))
        if scope.kind == FUNC_SCOPE and name in scope.bound:
            return self.emit(O.Op.LOAD_FAST, node, self._local_slot(name))
        return self.emit(O.Op.LOAD_GLOBAL, node, self.name_idx(name))

    def store_name(self, name: str, node: A.Node) -> int:
        scope = self.scope
        if name in scope.cellvars or name in scope.freevars:
            return self.emit(O.Op.STORE_DEREF, node, scope.cell_index(name))
        if scope.kind == FUNC_SCOPE and name in scope.bound:
            return self.emit(O.Op.STORE_FAST, node, self._local_slot(name))
        return self.emit(O.Op.STORE_GLOBAL, node, self.name_idx(name))

    def _local_slot(self, name: str) -> int:
        try:
            return self.code.local_names.index(name)
        except ValueError:  # pragma: no cover - 定种正确时不会走到
            raise JishiError(f"（内部）局部变量「{name}」没有分配槽位",
                             line=1, col=1)

    # -- 语句 ---------------------------------------------------------------

    def compile_body(self, stmts):
        for st in stmts:
            self.compile_stmt(st)

    def compile_stmt(self, st: A.Node):
        """编译一条语句，**顺手在它的第一条指令上打一个「语句起始」标**（B5）。

        标记只标**首指令**：循环的回边（`遍历`→FOR_ITER、`当`→条件、
        `循环 n 次`→计数器判断）都落在语句中部，因此循环头只停一次，
        与树遍历「一个语句一次 `exec_stmt`」严格对齐。
        """
        start = len(self.code.instrs)
        self._compile_stmt(st)
        self._mark_start(start, st)

    def _mark_start(self, start: int, st: A.Node) -> None:
        """给 `start` 处那条指令打上「语句起始」标（这一段真发射了指令才算）。

        不变式：**打标行的集合 == AST 里语句行的集合**。`tests/test_m39_vm_debugger.py`
        拿 `examples/` 与 `tests/cases/` 全量对拍这条不变式——破了就意味着
        「某一行在树遍历停得住、在字节码停不住」。
        """
        if len(self.code.instrs) > start:
            self.code.instrs[start].stmt_line = getattr(st, "line", 1) or 1

    def _emit_nop(self, node: A.Node) -> None:
        """发一条「空操作」（常量入栈再弹出）并就地打上语句起始标。

        为什么需要它：语句标记是**打在指令上**的，而有些语句本身不产生任何
        效果（空块 `:`、类定义的开头）。若不为它们占一条指令，「语句起始」
        就无处安放 —— 这一行在树遍历里停得住、在字节码里停不住。
        两条指令、不动栈、无副作用。
        """
        start = len(self.code.instrs)
        self.emit(O.Op.LOAD_CONST, node, self.const_idx(None))
        self.emit(O.Op.POP_TOP, node)
        self._mark_start(start, node)

    def _compile_stmt(self, st: A.Node):
        if isinstance(st, A.Assign):
            self._c_assign(st)
        elif isinstance(st, A.ExprStmt):
            self.compile_expr(st.expr)
            # 模块顶层的表达式语句要保留值给 REPL 回显（与树遍历 _last_value 一致）
            if self.scope.kind == MODULE_SCOPE:
                self.emit(O.Op.STORE_LAST, st)
            else:
                self.emit(O.Op.POP_TOP, st)
        elif isinstance(st, A.If):
            self._c_if(st)
        elif isinstance(st, A.For):
            self._c_for(st)
        elif isinstance(st, A.Loop):
            self._c_loop(st)
        elif isinstance(st, A.While):
            self._c_while(st)
        elif isinstance(st, A.Break):
            self.emit(O.Op.BREAK_LOOP, st)
        elif isinstance(st, A.Continue):
            self.emit(O.Op.CONTINUE_LOOP, st)
        elif isinstance(st, A.FuncDef):
            self._c_funcdef(st)
        elif isinstance(st, A.ClassDef):
            self._c_classdef(st)
        elif isinstance(st, A.Return):
            if st.value is not None:
                self.compile_expr(st.value)
            else:
                self.emit(O.Op.LOAD_CONST, st, self.const_idx(None))
            self.emit(O.Op.RETURN, st)
        elif isinstance(st, A.Import):
            self._c_import(st)
        elif isinstance(st, A.Try):
            self._c_try(st)
        elif isinstance(st, A.Raise):
            self._c_raise(st)
        elif isinstance(st, A.Pass):
            # 空块占位（`:` / `通过`）：树遍历里它是一次真实的 `exec_stmt`
            # （调试器会停在这一行），字节码里得占一条指令（B5）
            self._emit_nop(st)
        else:
            raise JishiError(f"还不支持的语句：{type(st).__name__}",
                             line=getattr(st, "line", 1),
                             col=getattr(st, "col", 1))

    def _c_assign(self, st: A.Assign):
        target = st.target
        if isinstance(target, A.TargetList):
            # 解包赋值：令 a, b = ...（复合赋值已在 parser 报错）
            star = getattr(target, "star_index", None)
            self.compile_expr(st.value)
            self.emit(O.Op.UNPACK, st, len(target.names),
                      -1 if star is None else star)
            for name_node in target.names:
                self.store_name(name_node.id, st)
            return
        if isinstance(target, A.Name):
            if st.op == "=":
                self.compile_expr(st.value)
                self.store_name(target.id, st)
            else:
                self.load_name(target.id, st)
                self.compile_expr(st.value)
                self.emit(O.Op.BIN_OP, st,
                          O.SYMBOL_TO_BINOP[st.op[:-1]])
                self.store_name(target.id, st)
            return
        if isinstance(target, A.Subscript):
            self.compile_expr(target.obj)
            self.compile_expr(target.index)
            if st.op == "=":
                self.compile_expr(st.value)
                self.emit(O.Op.SET_ITEM, st)
            else:
                self.emit(O.Op.DUP_TWO, st)
                self.emit(O.Op.GET_ITEM, st)
                self.compile_expr(st.value)
                self.emit(O.Op.BIN_OP, st, O.SYMBOL_TO_BINOP[st.op[:-1]])
                self.emit(O.Op.SET_ITEM, st)
            return
        if isinstance(target, A.Attr):
            name_i = self.name_idx(target.attr)
            self.compile_expr(target.obj)
            if st.op == "=":
                self.compile_expr(st.value)
                self.emit(O.Op.SET_ATTR, st, name_i)
            else:
                # 复合属性赋值：先读旧值再写回。树遍历解释器同样语义
                # （_do_assign 的 Attr 分支），两执行器保持一致
                self.emit(O.Op.DUP_TOP, st)
                self.emit(O.Op.GET_ATTR, st, name_i)
                self.compile_expr(st.value)
                self.emit(O.Op.BIN_OP, st, O.SYMBOL_TO_BINOP[st.op[:-1]])
                self.emit(O.Op.SET_ATTR, st, name_i)
            return
        raise JishiError("不支持的赋值目标", line=st.line, col=st.col)

    def _c_if(self, st: A.If):
        end_jumps: list[int] = []
        for test, body in st.branches:
            self.compile_expr(test)
            jf = self.emit(O.Op.POP_JUMP_IF_FALSE, test)
            self.compile_body(body)
            end_jumps.append(self.emit(O.Op.JUMP, st))
            self.patch(jf, self.here())
        if st.orelse is not None:
            self.compile_body(st.orelse)
        end = self.here()
        for j in end_jumps:
            self.patch(j, end)

    def _c_for(self, st: A.For):
        self.compile_expr(st.iter)
        self.emit(O.Op.GET_ITER, st)
        setup = self.emit(O.Op.SETUP_LOOP, st)
        top = self.here()
        patch_top = self.emit(O.Op.FOR_ITER, st)
        self.store_name(st.target.id, st)
        self.compile_body(st.body)
        self.emit(O.Op.JUMP, st, top)
        # 迭代结束：FOR_ITER 已弹出迭代器
        exit_iter = self.here()
        self.emit(O.Op.POP_BLOCK, st)
        end_jump = self.emit(O.Op.JUMP, st)
        # 中断：栈被回退到 SETUP_LOOP 处（迭代器还在），手动弹出
        cleanup = self.here()
        self.emit(O.Op.POP_TOP, st)
        self.emit(O.Op.POP_BLOCK, st)
        end = self.here()
        self.patch(patch_top, exit_iter)
        self.patch(end_jump, end)
        self.patch(setup, top, "a")        # 继续 → 回到 FOR_ITER
        self.patch(setup, cleanup, "b")    # 中断 → 清理后出循环

    def _c_loop(self, st: A.Loop):
        slot = self.new_tmp()
        self.compile_expr(st.times)
        self.emit(O.Op.LOOP_SETUP, st, slot)
        setup = self.emit(O.Op.SETUP_LOOP, st)
        top = self.here()
        self.emit(O.Op.LOAD_FAST, st, slot)
        self.emit(O.Op.LOAD_CONST, st, self.const_idx(0))
        self.emit(O.Op.COMPARE, st, O.CmpOp.GT)
        to_exit = self.emit(O.Op.POP_JUMP_IF_FALSE, st)
        self.compile_body(st.body)
        cont = self.here()
        self.emit(O.Op.LOAD_FAST, st, slot)
        self.emit(O.Op.LOAD_CONST, st, self.const_idx(1))
        self.emit(O.Op.BIN_OP, st, O.BinOp.SUB)
        self.emit(O.Op.STORE_FAST, st, slot)
        self.emit(O.Op.JUMP, st, top)
        exit_at = self.here()
        self.emit(O.Op.POP_BLOCK, st)
        end = self.here()
        self.patch(to_exit, exit_at)
        self.patch(setup, cont, "a")
        self.patch(setup, exit_at, "b")

    def _c_while(self, st: A.While):
        setup = self.emit(O.Op.SETUP_LOOP, st)
        top = self.here()
        self.compile_expr(st.test)
        to_exit = self.emit(O.Op.POP_JUMP_IF_FALSE, st)
        self.compile_body(st.body)
        self.emit(O.Op.JUMP, st, top)
        exit_at = self.here()
        self.emit(O.Op.POP_BLOCK, st)
        end = self.here()
        self.patch(to_exit, exit_at)
        self.patch(setup, top, "a")
        self.patch(setup, exit_at, "b")

    def _emit_function_object(self, st) -> None:
        """把 FuncDef / Lambda 编译成**一个留在栈上的函数对象**。

        闭包单元、默认值求值、MAKE_FUNCTION 三件事对具名函数和匿名函数
        完全一样，因此共用这一处（M24.4 抽出来的）。调用方负责决定这个
        函数对象接下来怎么用：具名函数 `store_name` 存起来，匿名函数直接
        当着表达式的结果用。
        """
        sub_scope = self.func_scopes[id(st)]
        code = self._make_code(st, sub_scope)
        code_idx = len(self.cmod.codes)
        self.cmod.codes.append(code)

        # 闭包：按子作用域 freevars 顺序，从当前帧取单元
        cur = self.scope
        for name in code.freevars:
            self.emit(O.Op.LOAD_CELL, st, cur.cell_index(name))
        # 默认参数：在定义处作用域求值，压栈（M7）
        ndefaults = 0
        for d in st.defaults:
            if d is not None:
                self.compile_expr(d)
                ndefaults += 1
        self.emit(O.Op.MAKE_FUNCTION, st, code_idx, len(code.freevars),
                  ndefaults)

        # 进入函数体继续发射
        saved_code, saved_scope = self.code, self.scope
        self.code, self.scope = code, sub_scope
        self._c_check_annotations(st)
        self.compile_body(st.body)
        # 隐式「返回 空」：与树遍历一致（exec_block 跑完返回 None）。
        # 函数体末尾已有 RETURN 时这两条不可达，但能兜住所有提前跳出的路径。
        self.emit(O.Op.LOAD_CONST, st, self.const_idx(None))
        self.emit(O.Op.RETURN, st)
        self.code, self.scope = saved_code, saved_scope

    def _c_funcdef(self, st: A.FuncDef):
        self._emit_function_object(st)
        self.store_name(st.name, st)

    # 类定义：压基类（若有）+ 逐个方法 + 类名，最后 BUILD_CLASS 组装
    def _c_classdef(self, st: A.ClassDef):
        # 类定义本身也是一条语句，先占一条指令：类体里第一个方法/类变量的
        # 首指令与「类定义的首指令」会是同一条（类定义还没发射任何东西时
        # 就开始编类体了），两条语句抢一个指令下标，标记只能活一个——
        # 少了这条空操作，「函数 方法(自身)：」那一行就永远停不住（M39 踩到）。
        self._emit_nop(st)
        # 基类在最底（若有）
        if st.base is not None:
            self.load_name(st.base, st)

        n_methods = 0
        value_slots: list[tuple[A.Node, int]] = []   # 类变量：值先算好，最后挂上
        for node in st.body:
            if isinstance(node, A.Pass):
                # 空类占位（`:`）：同样占一条指令，让这一行在调试器里停得住
                self._emit_nop(node)
            elif isinstance(node, A.Assign):
                # 类变量（M23.3）：值在「定义类的外层作用域」求值。
                # **求值就地进行**（占一个临时槽），等类造出来再 SET_ATTR ——
                # 这样副作用的发生顺序与树遍历（源码顺序）一致。
                # 以前是「先造类、再按源码顺序求值」，于是同一个程序在两个
                # 执行器里副作用顺序不同（调试器看到的语句顺序也不同，M39 发现）。
                start = len(self.code.instrs)
                slot = self.new_tmp()
                self.compile_expr(node.value)
                self.emit(O.Op.STORE_FAST, node, slot)
                value_slots.append((node, slot))
                self._mark_start(start, node)
            elif isinstance(node, A.FuncDef):
                # 方法定义也是**一条语句**（树遍历里调试器会停在这一行，
                # 见 interpreter._do_classdef 里的 before_stmt），所以要打标
                start = len(self.code.instrs)
                sub_scope = self.func_scopes[id(node)]
                code = self._make_code(node, sub_scope)
                code_idx = len(self.cmod.codes)
                self.cmod.codes.append(code)
                # 方法：闭包单元 + MAKE_FUNCTION（不进作用域，压栈留待组装）
                cur = self.scope
                for name in code.freevars:
                    self.emit(O.Op.LOAD_CELL, node, cur.cell_index(name))
                ndefaults = 0
                for d in getattr(node, "defaults", []) or []:
                    if d is not None:
                        self.compile_expr(d)
                        ndefaults += 1
                self.emit(O.Op.MAKE_FUNCTION, node, code_idx,
                          len(code.freevars), ndefaults)
                n_methods += 1
                # 进入方法体继续发射
                saved_code, saved_scope = self.code, self.scope
                self.code, self.scope = code, sub_scope
                # 方法的类型标注校验（M24.3/M25）：方法与普通函数走的是两条
                # 发射路径（这里为了把方法函数对象留在栈上待 BUILD_CLASS），
                # 所以这行不能漏——否则「写在方法上的标注」在字节码执行器里
                # 等于没写，只有树遍历会查。
                self._c_check_annotations(node)
                self.compile_body(node.body)
                self.emit(O.Op.LOAD_CONST, node, self.const_idx(None))
                self.emit(O.Op.RETURN, node)
                self.code, self.scope = saved_code, saved_scope
                self._mark_start(start, node)

        # 栈 [基类?, 方法..., 类名] → 类
        self.emit(O.Op.LOAD_CONST, st, self.const_idx(st.name))
        self.emit(O.Op.BUILD_CLASS, st, n_methods, 1 if st.base else 0)

        # 类变量（M23.3）：类造好后逐个挂上。栈上此时是 [类]，
        # 每次 DUP_TOP 出一份「目标」，再压入值，SET_ATTR 吃掉值和那份目标，
        # 原类仍留在栈上留给下一次——这样连续多个类变量也对。
        # 值是**在源码位置**就算好的（见上面的 value_slots），这里只负责挂。
        for node, slot in value_slots:
            self.emit(O.Op.DUP_TOP, node)
            self.emit(O.Op.LOAD_FAST, node, slot)
            self.emit(O.Op.SET_ATTR, node, self.name_idx(node.target.id))

        self.store_name(st.name, st)

    def _c_check_annotations(self, st: A.FuncDef):
        """在函数体最前面发射「按类型标注校验实参」（M24.3）。

        为什么要发射成字节码、而不是在调用方（Python 侧）校验：
        三个执行器里，参数绑定分别在 Python（树遍历/参考 VM）和 C（C VM）
        完成，只有「把校验放进函数自己的代码」才能让三者走**同一条**路径，
        不必改 C，也不会出现「某个执行器不检查」的语义偏差。

        只有写了标注的参数才产生指令；没写标注的函数一个字节都不多。
        不认识类型名由 `检查实参` 内部跳过（标注释义为文档）。
        """
        anns = list(getattr(st, "annotations", []) or [])
        if not anns:
            return
        checked = [(i, name) for i, name in enumerate(anns) if name]
        if not checked:
            return

        self.load_name("检查实参", st)
        # 扁平描述：[参数名, 类型名, …]
        for i, name in checked:
            self.emit(O.Op.LOAD_CONST, st, self.const_idx(st.params[i]))
            self.emit(O.Op.LOAD_CONST, st, self.const_idx(name))
        self.emit(O.Op.BUILD_LIST, st, len(checked) * 2)
        # 实参值列表（顺序与描述一致）
        for i, _name in checked:
            self.load_name(st.params[i], st)
        self.emit(O.Op.BUILD_LIST, st, len(checked))
        self.emit(O.Op.LOAD_CONST, st, self.const_idx(st.name))
        self.emit(O.Op.CALL, st, 3, -1)
        self.emit(O.Op.POP_TOP, st)

    def _make_code(self, st: A.FuncDef, scope: _Scope) -> O.Code:
        cellnames = sorted(scope.cellvars)
        cellvars = set(cellnames)
        locals_ordered = [p for p in st.params if p not in cellvars]
        locals_ordered += sorted(scope.bound - cellvars - set(st.params))
        # 参数槽位映射：被捕获的参数进闭包单元，其余按出现顺序占局部槽
        param_local: list[int] = []
        param_cell: list[int] = []
        slot = 0
        for p in st.params:
            if p in cellvars:
                param_local.append(-1)
                param_cell.append(cellnames.index(p))
            else:
                param_local.append(slot)
                param_cell.append(-1)
                slot += 1
        # 「本函数赋过值、外层也有同名变量」的槽位：赋值前读取会撞上
        # 「赋值即局部」的规则，给一句说得清原因的提示
        slot_of = {n: i for i, n in enumerate(locals_ordered)}
        outer_bound: set[str] = set()
        e = scope.parent
        while e is not None:
            outer_bound |= e.bound
            outer_bound |= e.cellvars
            e = e.parent
        local_hints: dict[int, str] = {}
        for name in sorted((set(locals_ordered) | cellvars) & outer_bound):
            slot = slot_of.get(name)
            if slot is None:
                continue
            # 文本来自 runtime 的单一来源：树遍历侧报同一条错时用的是同一份
            # 文案（M40 起三执行器的报错要对拍，差一个字都算不一致）
            from .runtime import UNBOUND_SHADOW_HINT
            local_hints[slot] = UNBOUND_SHADOW_HINT.format(name=name)

        return O.Code(
            name=st.name,
            params=list(st.params),
            param_idx=[self.name_idx(p) for p in st.params],
            param_local=param_local,
            param_cell=param_cell,
            # 形参种类（M25）：与 params 对齐；缺省（老 AST / 匿名函数未填）当普通
            param_kind=list(getattr(st, "param_kind", []) or [])
                       or [O.ParamKind.NORMAL] * len(st.params),
            nlocals=len(locals_ordered),
            local_names=locals_ordered,
            cellvars=cellnames,
            freevars=sorted(scope.freevars),
            local_hints=local_hints,
            firstlineno=st.line,
        )

    def _c_import(self, st: A.Import):
        alias_i = -1
        if st.alias:
            alias_i = self.name_idx(st.alias)
        # b 编码导入来源：0=标准库 1=python 2=本地包
        src = 1 if st.from_python else (2 if st.from_local else 0)
        self.emit(O.Op.IMPORT, st, self.name_idx(st.name), src, alias_i)
        self.store_name(st.alias or st.name, st)

    # 尝试 / 捕获 / 最终（M5a）
    #
    # 布局（SETUP_TRY 的 a=捕获入口 b=finally入口，-1 表示无）：
    #     SETUP_TRY  catch, fin
    #     <尝试块>
    #     POP_TRY
    #     JUMP       fin_start          # 正常路径
    # catch:                             # 栈顶 = 异常对象（VM 放入）
    #     CHECK_SIGNAL unmatched        # 中断/继续不进捕获 → 走 finally+重抛
    #     <逐个捕获：DUP/条件/MATCH_EXC/POP_JUMP_IF_FALSE/存名字/块体/JUMP fin_start>
    # unmatched:                        # 栈顶 = 未匹配异常或信号
    #     <finally 副本>
    #     THROW                          # 继续向外传播
    # fin_start:                         # 正常路径的 finally（无则直接 after）
    #     <finally 副本>
    #     END_FINALLY                    # 有挂起返回则继续返回，否则 fall through
    # after:
    def _c_try(self, st: A.Try):
        setup = self.emit(O.Op.SETUP_TRY, st)
        self.compile_body(st.body)
        self.emit(O.Op.POP_TRY, st)
        to_fin = self.emit(O.Op.JUMP, st)          # 正常路径 → fin_start

        catch_at = self.here()
        to_unmatched = self.emit(O.Op.CHECK_SIGNAL, st)
        handler_done: list[int] = []               # 各捕获体结束的 JUMP（回填到 fin_start）
        for h in st.handlers:
            if h.type is not None:
                self.emit(O.Op.DUP_TOP, h)         # [err, err]
                self.compile_expr(h.type)          # [err, err, cond]
                self.emit(O.Op.MATCH_EXC, h)       # [err, bool]
                to_next = self.emit(O.Op.POP_JUMP_IF_FALSE, h)
                if h.name:                          # 匹配成功：绑定 e
                    self.store_name(h.name, h)
                else:
                    self.emit(O.Op.POP_TOP, h)
                self.compile_body(h.body)
                handler_done.append(self.emit(O.Op.JUMP, h))
                self.patch(to_next, self.here())   # 不匹配 → 下一个捕获
            else:
                # 裸捕获：全接（信号已被 CHECK_SIGNAL 排除在 unmatched）
                if h.name:
                    self.store_name(h.name, h)
                else:
                    self.emit(O.Op.POP_TOP, h)
                self.compile_body(h.body)
                handler_done.append(self.emit(O.Op.JUMP, h))

        # 未匹配路径：执行 finally 后继续抛（栈顶的 err 由 THROW 弹出）
        unmatched_at = self.here()
        if st.finalbody is not None:
            self.compile_body(st.finalbody)
        self.emit(O.Op.THROW, st)

        # 正常路径的 finally
        fin_start = self.here()
        if st.finalbody is not None:
            self.compile_body(st.finalbody)
            self.emit(O.Op.END_FINALLY, st)

        # 回填跳转
        self.patch(setup, catch_at, "a")
        self.patch(setup, fin_start if st.finalbody is not None else -1, "b")
        self.patch(to_fin, fin_start)
        self.patch(to_unmatched, unmatched_at)
        for j in handler_done:
            self.patch(j, fin_start)

    def _c_raise(self, st: A.Raise):
        if st.value is not None:
            self.compile_expr(st.value)
        else:
            self.emit(O.Op.LOAD_CONST, st, self.const_idx(None))
        self.emit(O.Op.THROW, st)

    # -- 表达式 -------------------------------------------------------------

    def compile_expr(self, e: A.Node):
        if isinstance(e, A.Num):
            self.emit(O.Op.LOAD_CONST, e, self.const_idx(e.value))
        elif isinstance(e, A.Str):
            self.emit(O.Op.LOAD_CONST, e, self.const_idx(e.value))
        elif isinstance(e, A.Name):
            if e.id == "空":        # 与树遍历一致：空 是常量，不走名字查找
                self.emit(O.Op.LOAD_CONST, e, self.const_idx(None))
            else:
                self.load_name(e.id, e)
        elif isinstance(e, A.List):
            for el in e.elements:
                self.compile_expr(el)
            self.emit(O.Op.BUILD_LIST, e, len(e.elements))
        elif isinstance(e, A.Dict):
            for k, v in zip(e.keys, e.values):
                self.compile_expr(k)
                self.compile_expr(v)
            self.emit(O.Op.BUILD_DICT, e, len(e.keys))
        elif isinstance(e, A.Comprehension):
            self._c_comprehension(e)
        elif isinstance(e, A.BinOp):
            self.compile_expr(e.left)
            self.compile_expr(e.right)
            self.emit(O.Op.BIN_OP, e, O.SYMBOL_TO_BINOP[e.op])
        elif isinstance(e, A.UnaryOp):
            self.compile_expr(e.operand)
            self.emit(O.Op.UNARY_OP, e, O.SYMBOL_TO_UNOP[e.op])
        elif isinstance(e, A.BoolOp):
            self._c_boolop(e)
        elif isinstance(e, A.Lambda):
            # 匿名函数（M24.4）：函数对象直接留在栈上当表达式结果
            self._emit_function_object(e)
        elif isinstance(e, A.Compare):
            self._c_compare(e)
        elif isinstance(e, A.Call):
            self._c_call(e)
        elif isinstance(e, A.Attr):
            self.compile_expr(e.obj)
            self.emit(O.Op.GET_ATTR, e, self.name_idx(e.attr))
        elif isinstance(e, A.Slice):
            # 切片本身也是值（M24.2）：读 a[1:3] 与写 a[1:3] = … 共用这条路径
            self._c_slice(e, e)
        elif isinstance(e, A.Subscript):
            self.compile_expr(e.obj)
            self.compile_expr(e.index)
            self.emit(O.Op.GET_ITEM, e)
        else:
            raise JishiError(f"还不支持的表达式：{type(e).__name__}",
                             line=getattr(e, "line", 1),
                             col=getattr(e, "col", 1))

    def _c_slice(self, s: A.Slice, host: A.Node):
        """编译切片：按 start/stop/step 是否存在，依次压栈，用位标志标记（M18.1）。"""
        flags = 0
        # 压栈顺序：start, stop, step（存在才压），flags 位标记哪些存在
        if s.start is not None:
            self.compile_expr(s.start)
            flags |= 1
        if s.stop is not None:
            self.compile_expr(s.stop)
            flags |= 2
        if s.step is not None:
            self.compile_expr(s.step)
            flags |= 4
        self.emit(O.Op.BUILD_SLICE, host, flags)

    def _c_boolop(self, e: A.BoolOp):
        # 与树遍历一致：结果一定是真/假，不是短路值本身
        jump_op = (O.Op.POP_JUMP_IF_FALSE if e.op == "与"
                   else O.Op.POP_JUMP_IF_TRUE)
        to_end = []
        for v in e.values[:-1]:
            self.compile_expr(v)
            to_end.append(self.emit(jump_op, v))
        self.compile_expr(e.values[-1])
        last_jump = self.emit(jump_op, e)
        # 全部通过
        self.emit(O.Op.LOAD_CONST, e,
                  self.const_idx(True if e.op == "与" else False))
        end_jump = self.emit(O.Op.JUMP, e)
        short_at = self.here()
        self.emit(O.Op.LOAD_CONST, e,
                  self.const_idx(False if e.op == "与" else True))
        end = self.here()
        for j in to_end:
            self.patch(j, short_at)
        self.patch(last_jump, short_at)
        self.patch(end_jump, end)

    def _c_compare(self, e: A.Compare):
        """链式比较：1 < x < 5，中途短路，结果为布尔。

        中间每一对的栈布局（以 ``L < R`` 为例，R 要留给下一对）：
        ``[L] → 求值 R → [L,R] → DUP_TOP → [L,R,R] → ROT_THREE → [R,L,R]
        → COMPARE → [R, 布尔]``
        - 布尔为假：``JUMP_IF_FALSE_OR_POP`` 保留栈顶跳走 → ``[R, 假]``
        - 布尔为真：弹出布尔 → ``[R]``，R 成为下一对的左值
        """
        n = len(e.ops)
        self.compile_expr(e.left)
        short_jumps: list[int] = []
        for i, (op, comp) in enumerate(zip(e.ops, e.comparators)):
            last = (i == n - 1)
            self.compile_expr(comp)
            if not last:
                self.emit(O.Op.DUP_TOP, e)
                self.emit(O.Op.ROT_THREE, e)      # [L,R,R] → [R,L,R]
            self.emit(O.Op.COMPARE, e, O.SYMBOL_TO_CMPOP[op])
            if not last:
                short_jumps.append(self.emit(O.Op.JUMP_IF_FALSE_OR_POP, e))
        # 走到这里说明每一对都成立，栈顶就是最终布尔值
        if short_jumps:
            end_jump = self.emit(O.Op.JUMP, e)
            # 短路出口：栈上是 [中间值, 假] → 交换后丢掉中间值，只留「假」
            short_at = self.here()
            self.emit(O.Op.ROT_TWO, e)
            self.emit(O.Op.POP_TOP, e)
            end = self.here()
            for j in short_jumps:
                self.patch(j, short_at)
            self.patch(end_jump, end)

    def _c_call(self, e: A.Call):
        # M11 优化：X.追加(单参数) → LIST_APPEND 指令，省一次 getattr 回调
        if (not e.keywords and len(e.args) == 1
                and isinstance(e.func, A.Attr)
                and e.func.attr == "追加"):
            self.compile_expr(e.func.obj)
            self.compile_expr(e.args[0])
            self.emit(O.Op.LIST_APPEND, e)
            return
        # M48 阶段三②「方法调用融合」：`X.方法(实参)` → 一条 METHOD_CALL。
        # 顺序必须是 **[obj, 实参...]**——与「先 GET_ATTR 再压实参」的栈形状
        # 完全一致，只是少了那条 GET_ATTR（省一次宿主回调，实测约 4µs）。
        # 只在 C VM 打开（Python VM / Node / Rust 不认识这条指令）。
        # 带关键字参数、或实参里含 `*`/`**` 时不融合：那条路要 kw_flat 表，
        # 融合版没带，**退化成老路径即可**（语义不变）。
        if (self.fuse_method_call and not e.keywords
                and isinstance(e.func, A.Attr)):
            self.compile_expr(e.func.obj)
            for a in e.args:
                self.compile_expr(a)
            self.emit(O.Op.METHOD_CALL, e, len(e.args),
                      self.name_idx(e.func.attr), e.func.col)
            return
        self.compile_expr(e.func)
        for a in e.args:
            self.compile_expr(a)
        kw_i = -1
        if e.keywords:
            kw_i = self.kw_idx([name for name, _ in e.keywords])
            for _name, v in e.keywords:
                self.compile_expr(v)
        self.emit(O.Op.CALL, e, len(e.args), kw_i)

    def _c_comprehension(self, e: A.Comprehension):
        """推导式（M7）：编译为「临时结果 + 迭代 + 条件 + 追加」的指令序列。

        循环变量与「遍历」语句一致（写入当前作用域），结果存隐藏临时槽。
        """
        tmp = self.new_tmp()
        # 结果槽：空列表 / 空字典
        if e.kind == "list":
            self.emit(O.Op.BUILD_LIST, e, 0)
        else:
            self.emit(O.Op.BUILD_DICT, e, 0)
        self.emit(O.Op.STORE_FAST, e, tmp)

        # 迭代
        self.compile_expr(e.iter)
        self.emit(O.Op.GET_ITER, e)
        top = self.here()
        patch_exit = self.emit(O.Op.FOR_ITER, e)
        self.store_name(e.target.id, e.target)

        # 条件（可选）
        skip = None
        if e.condition is not None:
            self.compile_expr(e.condition)
            skip = self.emit(O.Op.POP_JUMP_IF_FALSE, e.condition)

        # 追加元素
        if e.kind == "list":
            self.emit(O.Op.LOAD_FAST, e, tmp)
            self.emit(O.Op.GET_ATTR, e, self.name_idx("追加"))
            self.compile_expr(e.elt)
            self.emit(O.Op.CALL, e, 1, -1)
            self.emit(O.Op.POP_TOP, e)      # 追加返回 空，丢弃
        else:
            self.emit(O.Op.LOAD_FAST, e, tmp)
            self.compile_expr(e.key)
            self.compile_expr(e.value)
            self.emit(O.Op.SET_ITEM, e)

        cont = self.here()
        if skip is not None:
            self.patch(skip, cont)
        self.emit(O.Op.JUMP, e, top)

        exit_at = self.here()
        self.patch(patch_exit, exit_at)
        self.emit(O.Op.LOAD_FAST, e, tmp)   # 结果入栈


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def compile_program(program: A.Program, filename: str = "<输入>",
                    source_lines: Optional[list[str]] = None,
                    fuse_method_call: bool = False) -> O.CompiledModule:
    """把 AST 编译成 CompiledModule。

    `fuse_method_call`（M48 阶段三②）：把 `X.方法(实参)` 编译成一条
    `METHOD_CALL` 而不是 `GET_ATTR` + `CALL`。**只有 C VM 该打开**——
    Python VM 与 Node/Rust 宿主不认识那条指令。默认关闭，于是默认产物
    与之前逐字节相同。
    """
    cmod = O.CompiledModule(filename=filename)
    main = O.Code(name="<模块>", firstlineno=1)
    cmod.codes.append(main)

    func_scopes: dict[int, _Scope] = {}
    root = _Scope(MODULE_SCOPE, "<模块>")
    _collect_stmts(program.body, root, func_scopes)
    _resolve(root)

    emit = _Emitter(cmod, func_scopes, root, fuse_method_call=fuse_method_call)
    emit.code = main
    emit.compile_body(program.body)
    emit.emit(O.Op.HALT, None)
    cmod.main = 0
    return cmod


def compile_source(source: str, filename: str = "<输入>",
                   fuse_method_call: bool = False) -> O.CompiledModule:
    """源码 → CompiledModule（含词法/语法分析）。"""
    from .tokenizer import tokenize
    from .parser import parse

    tokens = tokenize(source, filename)
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    program = parse(tokens, lines, filename)
    return compile_program(program, filename, lines,
                           fuse_method_call=fuse_method_call)


def compile_debug_expr(cmod: O.CompiledModule, frame_code: O.Code,
                       text: str) -> O.Code:
    """把一句调试命令里的表达式编译成**能在当前帧上跑**的小代码对象（B5）。

    「查看 单价 * 数量」要看见的是**暂停那一刻**的值，所以不能另起一个环境去
    求值：这段代码与所在帧**共用局部槽与闭包单元**（VM 侧 `eval_in_frame`），
    因此名字解析必须与该帧一致——局部名走 `LOAD_FAST`、单元名走 `LOAD_DEREF`、
    其余走 `LOAD_GLOBAL`（内建、导入的模块、模块级变量都在全局表里）。

    返回的小代码对象会**追加进同一个 `cmod.codes`**：常量表、名字表也跟着
    复用同一份，于是它在运行中的 VM 上直接可执行（VM 的 `self.codes` 就是
    这个 list）。
    """
    from .parser import parse_expression

    node = parse_expression(text, "<调试>")
    scope = _Scope(FUNC_SCOPE, "<查看>")
    # 与所在帧同名同槽：局部槽表整体复用（LOAD_FAST 的下标必须对得上）
    scope.bound = set(frame_code.local_names) | set(frame_code.params)
    scope.cellvars = set(frame_code.cellvars)
    scope.freevars = set(frame_code.freevars)
    func_scopes: dict[int, _Scope] = {}
    _collect_expr(node, scope, func_scopes)
    _resolve(scope)

    code = O.Code(name="<查看>",
                  nlocals=frame_code.nlocals,
                  local_names=list(frame_code.local_names),
                  cellvars=list(frame_code.cellvars),
                  freevars=list(frame_code.freevars),
                  firstlineno=0)
    emit = _Emitter(cmod, func_scopes, scope)
    emit.code = code
    try:
        emit.compile_expr(node)
    except KeyError:                   # pragma: no cover - 兜底，别甩英文 KeyError
        raise JishiError("「查看」里不能定义函数或类，只能看一个表达式",
                         line=1, col=1)
    emit.emit(O.Op.RETURN)
    cmod.codes.append(code)
    return code


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m jishi.compiler 文件.jsh``：打印反汇编。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法：python -m jishi.compiler 文件.jsh")
        return 2
    from pathlib import Path
    src = Path(argv[0]).read_text(encoding="utf-8")
    cmod = compile_source(src, argv[0])
    print(O.disassemble(cmod))
    return 0


if __name__ == "__main__":
    sys.exit(main())
