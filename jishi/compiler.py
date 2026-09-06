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
                 root: _Scope):
        self.cmod = cmod
        self.func_scopes = func_scopes
        self.code: O.Code = cmod.codes[cmod.main]
        self.scope: _Scope = root
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
            pass
        else:
            raise JishiError(f"还不支持的语句：{type(st).__name__}",
                             line=getattr(st, "line", 1),
                             col=getattr(st, "col", 1))

    def _c_assign(self, st: A.Assign):
        target = st.target
        if isinstance(target, A.TargetList):
            # 解包赋值：令 a, b = ...（复合赋值已在 parser 报错）
            self.compile_expr(st.value)
            self.emit(O.Op.UNPACK, st, len(target.names))
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

    def _c_funcdef(self, st: A.FuncDef):
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
        self.store_name(st.name, st)

        # 进入函数体继续发射
        saved_code, saved_scope = self.code, self.scope
        self.code, self.scope = code, sub_scope
        self.compile_body(st.body)
        # 隐式「返回 空」：与树遍历一致（exec_block 跑完返回 None）。
        # 函数体末尾已有 RETURN 时这两条不可达，但能兜住所有提前跳出的路径。
        self.emit(O.Op.LOAD_CONST, st, self.const_idx(None))
        self.emit(O.Op.RETURN, st)
        self.code, self.scope = saved_code, saved_scope

    # 类定义：压基类（若有）+ 逐个方法 + 类名，最后 BUILD_CLASS 组装
    def _c_classdef(self, st: A.ClassDef):
        # 基类在最底（若有）
        if st.base is not None:
            self.load_name(st.base, st)

        n_methods = 0
        for node in st.body:
            if isinstance(node, A.FuncDef):
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
                self.compile_body(node.body)
                self.emit(O.Op.LOAD_CONST, node, self.const_idx(None))
                self.emit(O.Op.RETURN, node)
                self.code, self.scope = saved_code, saved_scope

        # 栈 [基类?, 方法..., 类名] → 类
        self.emit(O.Op.LOAD_CONST, st, self.const_idx(st.name))
        self.emit(O.Op.BUILD_CLASS, st, n_methods, 1 if st.base else 0)
        self.store_name(st.name, st)

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
            local_hints[slot] = (
                f"外层也有一个叫「{name}」的变量。在这个函数里给它赋过值，"
                f"它就成了这个函数的局部变量，赋值之前不能读取")

        return O.Code(
            name=st.name,
            params=list(st.params),
            param_idx=[self.name_idx(p) for p in st.params],
            param_local=param_local,
            param_cell=param_cell,
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
        elif isinstance(e, A.Compare):
            self._c_compare(e)
        elif isinstance(e, A.Call):
            self._c_call(e)
        elif isinstance(e, A.Attr):
            self.compile_expr(e.obj)
            self.emit(O.Op.GET_ATTR, e, self.name_idx(e.attr))
        elif isinstance(e, A.Subscript):
            self.compile_expr(e.obj)
            if isinstance(e.index, A.Slice):
                self._c_slice(e.index, e)
            else:
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
                    source_lines: Optional[list[str]] = None) -> O.CompiledModule:
    """把 AST 编译成 CompiledModule。"""
    cmod = O.CompiledModule(filename=filename)
    main = O.Code(name="<模块>", firstlineno=1)
    cmod.codes.append(main)

    func_scopes: dict[int, _Scope] = {}
    root = _Scope(MODULE_SCOPE, "<模块>")
    _collect_stmts(program.body, root, func_scopes)
    _resolve(root)

    emit = _Emitter(cmod, func_scopes, root)
    emit.code = main
    emit.compile_body(program.body)
    emit.emit(O.Op.HALT, None)
    cmod.main = 0
    return cmod


def compile_source(source: str, filename: str = "<输入>") -> O.CompiledModule:
    """源码 → CompiledModule（含词法/语法分析）。"""
    from .tokenizer import tokenize
    from .parser import parse

    tokens = tokenize(source, filename)
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    program = parse(tokens, lines, filename)
    return compile_program(program, filename, lines)


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
