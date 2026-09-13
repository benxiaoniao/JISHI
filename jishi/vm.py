# -*- coding: utf-8 -*-
"""基石字节码虚拟机（M4a，Python 参考实现）。

职责：执行 ``compiler.py`` 产出的 ``CompiledModule``。
**语义一律委托 ``runtime.py``**，本文件只负责「怎么走指令」，
从而与 ``interpreter.py``（树遍历）、C VM（M4b）严格一致，
由 ``tests/test_cvm.py`` 的对拍测试保证。

几个刻意为之的设计：

- **中断/继续用异常穿透**：与树遍历完全一致（那边 ``RunBreak``/
  ``RunContinue`` 就是异常，天然穿透函数调用）。VM 捕获信号后逐层弹帧，
  直到找到带循环处理器的帧；一路没有循环就抛给用户，行为与树遍历一致。
- **未初始化的局部槽/闭包单元**用 ``_UNSET`` 哨兵，读取即报中文错误，
  而不是静默拿到 ``None``。
- **打印等 I/O 一律走 ``runtime.py``**，对拍测试才能用 ``redirect_stdout``
  抓到输出（这一点同样是 C VM 必须遵守的）。
"""

from __future__ import annotations

from typing import Any, Optional

from . import opcodes as O
from . import runtime as R
from .errors import (
    JishiError,
    RunBreak,
    RunContinue,
    RunError,
    RunTypeError,
    SemanticNameError,
)
from .opcodes import (
    BINOP_TO_SYMBOL,
    CMPOP_TO_SYMBOL,
    SYMBOL_TO_CMPOP,
    BinOp,
    CmpOp,
    Op,
    UnOp,
)

#: 帧深度上限，与 C VM 一致
MAX_FRAMES = 1000

_UNSET: Any = object()

#: 返回值挂起哨兵（None 本身是合法返回值，不能用 None 占位）
_PENDING_NONE: Any = object()

#: 需要穿透帧继续传播的循环控制信号
_LOOP_SIGNALS = (RunBreak, RunContinue)


# ---------------------------------------------------------------------------
# 闭包单元 / 函数对象
# ---------------------------------------------------------------------------

class Cell:
    """闭包单元（一个可变的盒子），实现「外层改了内层看得到」。"""

    __slots__ = ("value",)

    def __init__(self, value: Any = _UNSET):
        self.value = value

    def __repr__(self):
        return "<单元 未赋值>" if self.value is _UNSET else f"<单元 {self.value!r}>"


class VmFunction:
    """字节码版基石函数。

    带 ``__call__``，因此可直接交给 Python 的高阶函数
    （``sorted(键=f)``、``map`` 等），与树遍历的 ``UserFunction`` 等价。
    """

    __slots__ = ("name", "code", "cells", "vm", "defaults")

    def __init__(self, name: str, code: O.Code, cells: list[Cell], vm: "VM",
                 defaults: Optional[list] = None):
        self.name = name
        self.code = code
        self.cells = cells
        self.vm = vm
        #: 与 code.params 对齐；None 表示无默认（值已在定义处求好，M7）
        self.defaults = defaults

    def __repr__(self):
        return f"<函数 {self.name}>"

    def __call__(self, *args, **kwargs):
        return self.vm.call_function(self, list(args), kwargs)


class _Handler:
    """异常处理器（SETUP_TRY 登记）。

    ``catch_ip``：异常分发入口（栈顶压异常对象后跳转）；
    ``finally_ip``：finally 代码入口（-1 表示纯捕获）；
    ``sp``：登记时的栈深度（分发时截栈用）。
    """

    __slots__ = ("catch_ip", "finally_ip", "sp", "seq")

    def __init__(self, catch_ip: int, finally_ip: int, sp: int, seq: int):
        self.catch_ip = catch_ip
        self.finally_ip = finally_ip
        self.sp = sp
        self.seq = seq       # 块栈登记序号：越大越内层（越近）


class _Frame:
    """一个栈帧。所有帧共享 VM 的同一条值栈，帧只记住自己的栈底。

    ``loops`` 是循环处理器栈，元素为 ``(记录时的栈深度, 继续目标, 中断目标)``。
    ``handlers`` 是异常处理器栈；``pending_return`` 是被 finally 挂起的返回值。
    """

    __slots__ = ("code", "ip", "base", "locals", "cells", "loops",
                 "handlers", "pending_return", "block_seq")

    def __init__(self, code: O.Code, ip: int, base: int,
                 locals_: list[Any], cells: list[Cell]):
        self.code = code
        self.ip = ip
        self.base = base
        self.locals = locals_
        self.cells = cells
        self.loops: list[tuple[int, int, int]] = []
        self.handlers: list[_Handler] = []
        self.pending_return: Any = _PENDING_NONE
        self.block_seq: int = 0


# ---------------------------------------------------------------------------
# 虚拟机
# ---------------------------------------------------------------------------

class VM:
    def __init__(self, cmod: O.CompiledModule, filename: Optional[str] = None):
        self.cmod = cmod
        self.filename = filename or cmod.filename
        self.consts = cmod.consts
        self.names = cmod.names
        self.kw_names = cmod.kw_names
        self.codes = cmod.codes
        self.globals: dict[str, Any] = R.new_builtins()
        self.stack: list[Any] = []
        self.frames: list[_Frame] = []
        #: 最近一次顶层表达式语句的值（REPL 回显用）
        self.last_value: Any = None

    # -- 对外入口 -----------------------------------------------------------

    def run(self) -> Any:
        """执行模块主代码，返回最后一个顶层表达式语句的值。"""
        main = self.codes[self.cmod.main]
        frame = _Frame(main, 0, 0, [_UNSET] * main.nlocals,
                       [Cell() for _ in range(main.ncells)])
        self.frames.append(frame)
        try:
            self._execute(stop_at=None)
        finally:
            self.frames.clear()
        return self.last_value

    def call_function(self, fn: VmFunction, args: list,
                      kwargs: Optional[dict] = None) -> Any:
        """Python 侧回调基石函数（高阶函数场景），可重入。

        循环控制信号不在此拦截 —— 与树遍历一致，让它穿透回最近的循环。
        """
        code = fn.code
        locals_, values = self._bind(code, args, kwargs or {},
                                     line=code.firstlineno, col=1,
                                     defaults=fn.defaults)
        frame_cells = [Cell() for _ in code.cellvars] + list(fn.cells)
        self._store_cells(code, frame_cells, values)
        base = len(self.stack)
        self.stack.append(None)                     # 返回值占位
        frame = _Frame(code, 0, base, locals_, frame_cells)
        self.frames.append(frame)
        try:
            self._execute(stop_at=frame)
        finally:
            if self.frames and self.frames[-1] is frame:
                self.frames.pop()
        value = self.stack[base]
        del self.stack[base:]
        return value

    # -- 参数绑定 -----------------------------------------------------------

    def _bind(self, code: O.Code, args: list, kwargs: dict, *,
              line: int, col: int,
              defaults: Optional[list] = None) -> tuple[list[Any], list[Any]]:
        """把实参绑到形参上，返回 ``(locals, 参数值列表)``。

        报错措辞与树遍历 ``_eval_call`` 逐字一致。
        defaults 与形参对齐（None 无默认），缺实参时补默认值（M7）。
        M25 起支持 `*参数`（收多余位置实参成列表）与 `**选项`（收未匹配关键字成字典）。
        """
        params = code.params
        nparams = len(params)
        kinds = code.param_kind or [O.ParamKind.NORMAL] * nparams
        star_pos = next((i for i, k in enumerate(kinds)
                         if k == O.ParamKind.VARARGS), -1)
        dstar_pos = next((i for i, k in enumerate(kinds)
                          if k == O.ParamKind.VARKW), -1)
        # 能吃普通位置实参的个数 = *参数 之前的那些
        npos = nparams
        if star_pos >= 0:
            npos = star_pos
        elif dstar_pos >= 0:
            npos = dstar_pos

        if len(args) > npos and star_pos < 0:
            raise RunTypeError(
                f"函数「{code.name}」需要 {npos} 个参数，"
                f"但传了 {len(args) + len(kwargs)} 个",
                line=line, col=col, filename=self.filename)

        bound: dict[str, Any] = {}
        for i in range(min(len(args), npos)):
            bound[params[i]] = args[i]
        if star_pos >= 0:
            bound[params[star_pos]] = list(args[npos:])
        for k, v in kwargs.items():
            if k in params[:npos]:
                bound[k] = v
            elif dstar_pos >= 0:
                extra = bound.setdefault(params[dstar_pos], {})
                extra[k] = v
            else:
                raise RunTypeError(
                    f"函数「{code.name}」没有叫「{k}」的参数",
                    line=line, col=col, filename=self.filename,
                    hint=f"它的参数是：{'、'.join(params[:npos])}")
        if dstar_pos >= 0:
            bound.setdefault(params[dstar_pos], {})
        for i, p in enumerate(params[:npos]):
            d = (defaults or [])[i] if i < len(defaults or []) else None
            if p not in bound and d is not None:
                bound[p] = d
        missing = [p for p in params[:npos] if p not in bound]
        if missing:
            raise RunTypeError(
                f"函数「{code.name}」缺少参数：{'、'.join(missing)}",
                line=line, col=col, filename=self.filename)

        locals_: list[Any] = [_UNSET] * code.nlocals
        values: list[Any] = []
        for i, p in enumerate(params):
            values.append(bound[p])
            slot = code.param_local[i]
            if slot >= 0:
                locals_[slot] = bound[p]
        return locals_, values

    def _store_cells(self, code: O.Code, cells: list[Cell],
                     values: list[Any]) -> None:
        """把落在闭包单元里的参数写进单元（单元变量不在局部槽里）。"""
        for i, v in enumerate(values):
            ci = code.param_cell[i]
            if ci >= 0:
                cells[ci].value = v

    # -- 报错辅助 -----------------------------------------------------------

    def _unbound(self, name: str, line: int, col: int,
                 hint: Optional[str] = None) -> JishiError:
        return SemanticNameError(f"找不到名字「{name}」", line=line, col=col,
                                 filename=self.filename, hint=hint)

    def _cell_name(self, code: O.Code, idx: int) -> str:
        """单元号 → 名字（单元变量在前、自由变量在后）。"""
        if idx < len(code.cellvars):
            return code.cellvars[idx]
        j = idx - len(code.cellvars)
        if 0 <= j < len(code.freevars):
            return code.freevars[j]
        return f"?单元{idx}"

    # -- 主循环 -------------------------------------------------------------

    def _execute(self, stop_at: Optional[_Frame]) -> None:
        stack = self.stack
        frames = self.frames
        consts = self.consts
        names = self.names
        kw_names = self.kw_names
        codes = self.codes
        globs = self.globals
        filename = self.filename

        while frames:
            frame = frames[-1]
            code = frame.code
            instrs = code.instrs
            frame_locals = frame.locals
            frame_cells = frame.cells
            base = frame.base
            local_names = code.local_names
            ip = frame.ip

            try:
                while True:
                    ins = instrs[ip]
                    op = ins.op
                    ip += 1

                    # === 最热路径：加载 / 存储 / 算术 / 跳转 ===
                    if op == Op.LOAD_FAST:
                        v = frame_locals[ins.a]
                        if v is _UNSET:
                            frame.ip = ip
                            raise self._unbound(
                                local_names[ins.a]
                                if ins.a < len(local_names) else f"${ins.a}",
                                ins.line, ins.col,
                                hint=code.local_hints.get(ins.a))
                        stack.append(v)
                    elif op == Op.LOAD_CONST:
                        stack.append(consts[ins.a])
                    elif op == Op.STORE_FAST:
                        frame_locals[ins.a] = stack.pop()
                    elif op == Op.BIN_OP:
                        r = stack.pop()
                        l = stack.pop()
                        stack.append(R.apply_binop(
                            BINOP_TO_SYMBOL[ins.a], l, r,
                            line=ins.line, col=ins.col, filename=filename))
                    elif op == Op.COMPARE:
                        r = stack.pop()
                        l = stack.pop()
                        stack.append(R.apply_compare(
                            CMPOP_TO_SYMBOL[ins.a], l, r,
                            line=ins.line, col=ins.col, filename=filename))
                    elif op == Op.JUMP:
                        ip = ins.a
                    elif op == Op.POP_JUMP_IF_FALSE:
                        if not R.truthy(stack.pop()):
                            ip = ins.a
                    elif op == Op.POP_JUMP_IF_TRUE:
                        if R.truthy(stack.pop()):
                            ip = ins.a
                    elif op == Op.POP_TOP:
                        stack.pop()

                    # === 全局名字 ===
                    elif op == Op.LOAD_GLOBAL:
                        name = names[ins.a]
                        try:
                            stack.append(globs[name])
                        except KeyError:
                            frame.ip = ip
                            raise R.lookup_name(name, globs, line=ins.line,
                                                col=ins.col,
                                                filename=filename)
                    elif op == Op.STORE_GLOBAL:
                        globs[names[ins.a]] = stack.pop()

                    # === 闭包 ===
                    elif op == Op.LOAD_DEREF:
                        v = frame_cells[ins.a].value
                        if v is _UNSET:
                            frame.ip = ip
                            raise self._unbound(
                                self._cell_name(code, ins.a),
                                ins.line, ins.col)
                        stack.append(v)
                    elif op == Op.STORE_DEREF:
                        frame_cells[ins.a].value = stack.pop()
                    elif op == Op.LOAD_CELL:
                        stack.append(frame_cells[ins.a])

                    # === 栈操作 ===
                    elif op == Op.DUP_TOP:
                        stack.append(stack[-1])
                    elif op == Op.DUP_TWO:
                        stack.extend(stack[-2:])
                    elif op == Op.ROT_TWO:
                        stack[-1], stack[-2] = stack[-2], stack[-1]
                    elif op == Op.ROT_THREE:
                        stack[-3], stack[-2], stack[-1] = (
                            stack[-1], stack[-3], stack[-2])

                    # === 一元运算与短路 ===
                    elif op == Op.UNARY_OP:
                        v = stack.pop()
                        stack.append(R.apply_unary(
                            "-" if ins.a == UnOp.NEG else "非", v,
                            line=ins.line, col=ins.col, filename=filename))
                    elif op == Op.JUMP_IF_FALSE_OR_POP:
                        if R.truthy(stack[-1]):
                            stack.pop()
                        else:
                            ip = ins.a

                    # === 调用与返回 ===
                    elif op == Op.CALL:
                        nargs = ins.a
                        kwi = ins.b
                        if kwi >= 0:
                            knames = kw_names[kwi]
                        else:
                            knames = ()
                        nkw = len(knames)
                        fn_base = len(stack) - (1 + nargs + nkw)
                        fn = stack[fn_base]
                        ret = self._do_call(
                            frame, fn_base, nargs, knames, nkw, ins)
                        if ret is None:      # 压了新帧：回外层重新同步
                            frame.ip = ip
                            break
                        # 内建 / 标准库 / Python 生态：结果已压栈
                    elif op == Op.RETURN:
                        value = stack.pop()
                        nxt = self._return_via_finally(frame, value)
                        if nxt >= 0:
                            ip = nxt           # try 块内：先去执行 finally
                        else:
                            del stack[base:]
                            stack.append(value)
                            frames.pop()
                            if frame is stop_at or not frames:
                                return
                            break             # 回到调用者帧继续执行
                    elif op == Op.MAKE_FUNCTION:
                        ncells = ins.b
                        ndefaults = ins.c
                        if ndefaults:
                            tail_values = stack[-ndefaults:]
                            del stack[-ndefaults:]
                            # 扩成与形参表等长（None 占位）。
                            # M25：默认值只在「普通参数」的尾部，`*参数` /
                            # `**选项` 不参与，所以前缀长度按 npos 算——
                            # 用 len(params) 会在 `f(a, b=10, *余)` 这种
                            # 「默认值后面还有收集参数」的写法上错位。
                            sub = codes[ins.a]
                            kinds = sub.param_kind or [0] * len(sub.params)
                            npos = next(
                                (i for i, k in enumerate(kinds)
                                 if k != O.ParamKind.NORMAL),
                                len(sub.params))
                            pad = npos - ndefaults
                            defaults = ([None] * pad + tail_values
                                        + [None] * (len(sub.params) - npos))
                        else:
                            defaults = None
                        if ncells:
                            cells = stack[-ncells:]
                            del stack[-ncells:]
                        else:
                            cells = []
                        stack.append(VmFunction(codes[ins.a].name,
                                                codes[ins.a], cells, self,
                                                defaults=defaults))

                    # === 类定义（M5b）===
                    elif op == Op.BUILD_CLASS:
                        n = ins.a
                        has_base = bool(ins.b)
                        name = stack.pop()
                        methods = {}
                        if n:
                            fns = stack[-n:]
                            del stack[-n:]
                            for fn in fns:
                                methods[fn.name] = fn
                        base = None
                        if has_base:
                            base = stack.pop()
                        stack.append(R.JishiClass(name, methods, base=base))

                    # === 解包（M7）===
                    elif op == Op.UNPACK:
                        n = ins.a
                        # 编译器恒定发射：-1 表示无星号，>=0 是星号位置（M25）
                        star = ins.b
                        v = stack.pop()
                        items = R.unpack_values(
                            v, n, None if star < 0 else star,
                            line=ins.line, col=ins.col, filename=filename)
                        for item in reversed(items):
                            stack.append(item)

                    # === 容器与属性 ===
                    elif op == Op.BUILD_LIST:
                        n = ins.a
                        if n:
                            items = stack[-n:]
                            del stack[-n:]
                        else:
                            items = []
                        stack.append(items)
                    elif op == Op.BUILD_DICT:
                        n = ins.a
                        if n:
                            flat = stack[-(2 * n):]
                            del stack[-(2 * n):]
                        else:
                            flat = []
                        stack.append(R.build_dict(
                            [(flat[i], flat[i + 1])
                             for i in range(0, len(flat), 2)],
                            line=ins.line, col=ins.col, filename=filename))
                    elif op == Op.GET_ATTR:
                        stack.append(R.get_attr(
                            stack.pop(), names[ins.a],
                            line=ins.line, col=ins.col, filename=filename))
                    elif op == Op.LIST_APPEND:
                        val = stack.pop()
                        obj = stack.pop()
                        m = R.get_attr(obj, "追加", line=ins.line,
                                       col=ins.col, filename=filename)
                        R.call_value(m, [val], {}, line=ins.line,
                                     col=ins.col, filename=filename)
                        stack.append(None)      # 追加返回空，保持 CALL 语义
                    elif op == Op.SET_ATTR:
                        val = stack.pop()
                        R.set_attr(stack.pop(), names[ins.a], val,
                                   line=ins.line, col=ins.col,
                                   filename=filename)
                    elif op == Op.GET_ITEM:
                        idx = stack.pop()
                        stack.append(R.get_item(
                            stack.pop(), idx,
                            line=ins.line, col=ins.col, filename=filename))
                    elif op == Op.BUILD_SLICE:
                        # M18.1：按位标志从栈上取 start/stop/step 构造 slice
                        flags = ins.a
                        step = stack.pop() if (flags & 4) else None
                        stop = stack.pop() if (flags & 2) else None
                        start = stack.pop() if (flags & 1) else None
                        stack.append(R.make_slice(
                            start, stop, step,
                            line=ins.line, col=ins.col, filename=filename))
                    elif op == Op.SET_ITEM:
                        val = stack.pop()
                        idx = stack.pop()
                        R.set_item(stack.pop(), idx, val,
                                   line=ins.line, col=ins.col,
                                   filename=filename)

                    # === 循环 ===
                    elif op == Op.GET_ITER:
                        obj = stack.pop()
                        try:
                            stack.append(iter(obj))
                        except TypeError:
                            frame.ip = ip
                            raise RunTypeError(
                                f"「{obj}」不能遍历，遍历需要列表、文本、集合或字典；自定义对象可以定义「迭代()」方法",
                                line=ins.line, col=ins.col,
                                filename=filename)
                    elif op == Op.FOR_ITER:
                        try:
                            stack.append(next(stack[-1]))
                        except StopIteration:
                            stack.pop()       # 耗尽：弹出迭代器
                            ip = ins.a
                    elif op == Op.SETUP_LOOP:
                        frame.block_seq += 1
                        frame.loops.append(
                            (len(stack), ins.a, ins.b, frame.block_seq))
                    elif op == Op.POP_BLOCK:
                        if frame.loops:
                            frame.loops.pop()
                    elif op == Op.BREAK_LOOP:
                        if self._signal_needs_dispatch(frame):
                            # 「循环内的 try」里中断：走异常分发触发 finally，
                            # CHECK_SIGNAL 引向 finally+重抛，最终仍由循环接住
                            frame.ip = ip
                            raise RunBreak("中断", line=ins.line,
                                           col=ins.col, filename=filename)
                        if not frame.loops:
                            raise RunBreak("中断", line=ins.line, col=ins.col,
                                           filename=filename)
                        depth, _cont, brk, _s = frame.loops[-1]
                        del stack[depth:]
                        ip = brk
                    elif op == Op.CONTINUE_LOOP:
                        if self._signal_needs_dispatch(frame):
                            frame.ip = ip
                            raise RunContinue("继续", line=ins.line,
                                              col=ins.col, filename=filename)
                        if not frame.loops:
                            raise RunContinue("继续", line=ins.line,
                                              col=ins.col, filename=filename)
                        depth, cont, _brk, _s = frame.loops[-1]
                        del stack[depth:]
                        ip = cont
                    elif op == Op.LOOP_SETUP:
                        n = stack.pop()
                        try:
                            n = int(n)
                        except (TypeError, ValueError):
                            frame.ip = ip
                            raise RunTypeError(
                                f"「{n}」不是有效的循环次数",
                                line=ins.line, col=ins.col,
                                filename=filename)
                        frame_locals[ins.a] = n

                    # === 模块 ===
                    elif op == Op.IMPORT:
                        src = ins.b   # 0=标准库 1=python 2=本地包
                        alias_name = names[ins.c] if ins.c >= 0 else None
                        stack.append(R.do_import(
                            names[ins.a], src == 1, src == 2,
                            line=ins.line, col=ins.col, filename=filename,
                            alias=alias_name))
                    elif op == Op.STORE_LAST:
                        self.last_value = stack.pop()

                    # === 异常处理（M5a） ===
                    elif op == Op.SETUP_TRY:
                        frame.block_seq += 1
                        frame.handlers.append(
                            _Handler(ins.a, ins.b, len(stack),
                                     frame.block_seq))
                    elif op == Op.POP_TRY:
                        if frame.handlers:
                            frame.handlers.pop()
                    elif op == Op.THROW:
                        v = stack.pop()
                        frame.ip = ip
                        raise R.make_exception(
                            v, line=ins.line, col=ins.col, filename=filename)
                    elif op == Op.CHECK_SIGNAL:
                        # 只查看不弹出：未匹配路径末尾的 THROW 统一负责
                        # 弹出并重新抛出（信号原样传播，由外层循环接住）
                        if isinstance(stack[-1], _LOOP_SIGNALS):
                            ip = ins.a
                    elif op == Op.MATCH_EXC:
                        cond = stack.pop()
                        err = stack[-1]
                        stack.append(R.exception_matches(
                            err, cond, line=ins.line, col=ins.col,
                            filename=filename))
                    elif op == Op.END_FINALLY:
                        pr = frame.pending_return
                        if pr is not _PENDING_NONE:
                            # finally 执行完，继续挂起的返回流程
                            frame.pending_return = _PENDING_NONE
                            nxt = self._return_via_finally(frame, pr)
                            if nxt >= 0:
                                ip = nxt
                            else:
                                del stack[base:]
                                stack.append(pr)
                                frames.pop()
                                if frame is stop_at or not frames:
                                    return
                                break

                    elif op == Op.HALT:
                        frames.pop()
                        return
                    else:  # pragma: no cover
                        frame.ip = ip
                        raise RunError(f"未知指令 {op}", line=ins.line,
                                       col=ins.col, filename=filename)

            except BaseException as exc:
                # 统一异常分发：处理器（try）优先于循环，循环优先于弹帧 —— 
                # 与树遍历/Python 的传播顺序一致（finally 先于循环接住信号）
                frame.ip = ip
                if self._dispatch_exception(exc, stop_at):
                    continue          # 处理器/循环接住：重新同步帧
                raise                 # 一路无人处理：冒给用户

            frame.ip = ip

    # -- 调用分派 -----------------------------------------------------------

    def _do_call(self, frame: _Frame, fn_base: int, nargs: int,
                 knames: tuple, nkw: int, ins: O.Instr):
        """执行一次调用。

        基石函数 → 返回 ``None``（已压新帧，调用方需重新同步）；
        其余（内建/标准库/Python 生态）→ 返回 ``True``（结果已压栈）。
        """
        stack = self.stack
        fn = stack[fn_base]
        args = stack[fn_base + 1: fn_base + 1 + nargs]
        kwargs = (dict(zip(knames, stack[fn_base + 1 + nargs:]))
                  if nkw else {})

        if type(fn) is VmFunction:
            sub = fn.code
            locals_, values = self._bind(sub, args, kwargs,
                                         line=ins.line, col=ins.col,
                                         defaults=fn.defaults)
            if len(self.frames) >= MAX_FRAMES:
                raise RunError(
                    "递归层数太深了，是不是函数忘了写结束条件？",
                    line=ins.line, col=ins.col, filename=self.filename)
            del stack[fn_base:]
            sub_cells = [Cell() for _ in sub.cellvars] + list(fn.cells)
            self._store_cells(sub, sub_cells, values)
            self.frames.append(_Frame(sub, 0, fn_base, locals_, sub_cells))
            return None

        del stack[fn_base:]
        stack.append(R.call_value(fn, args, kwargs, line=ins.line,
                                  col=ins.col, filename=self.filename))
        return True

    def _dispatch_exception(self, exc: BaseException,
                            stop_at: Optional[_Frame] = None) -> bool:
        """异常/信号分发：从当前帧向外找异常处理器或（信号的）循环。

        找到 → 清栈、放好恢复点，返回 True（主循环重新同步即可）；
        找不到 → 帧全部弹净，返回 False（调用方原样抛出）。

        就近原则（与 Python 块栈一致）：处理器与循环谁登记得更深
        （更内层）谁先接。因此：
        - try 内的循环里「中断」→ 循环接住，try 正常结束后才走最终；
        - 循环内的 try 里「中断」→ 先穿最终、再被外层循环接住。

        stop_at 是重入边界（call_function 的方法帧）：异常传播到这一层
        还没被接住，就停止穿透、返回 False，让调用方重新抛出 ——
        与 Python 的函数调用边界一致。
        """
        frames = self.frames
        stack = self.stack
        while frames:
            frame = frames[-1]
            if frame is stop_at:
                return False      # 到达重入边界，交给调用方处理
            h = frame.handlers[-1] if frame.handlers else None
            lp = frame.loops[-1] if frame.loops else None
            if isinstance(exc, _LOOP_SIGNALS):
                if lp and (h is None or lp[3] > h.seq):
                    depth, cont, brk, _s = lp
                    del stack[depth:]
                    frame.ip = brk if isinstance(exc, RunBreak) else cont
                    return True
                if h is not None:
                    frame.handlers.pop()
                    del stack[h.sp:]
                    stack.append(exc)
                    frame.ip = h.catch_ip
                    return True
            elif h is not None:
                frame.handlers.pop()
                del stack[h.sp:]
                stack.append(exc)
                frame.ip = h.catch_ip
                return True
            callee = frames.pop()
            del stack[callee.base:]
        return False

    @staticmethod
    def _signal_needs_dispatch(frame: _Frame) -> bool:
        """中断/继续是否需要走异常分发。

        只有「最近的异常处理器比最近的循环更内层」（循环内嵌 try）
        时才需要；try 包着循环的情形由循环自己接住（就近原则）。
        """
        if not frame.handlers:
            return False
        if not frame.loops:
            return True
        return frame.handlers[-1].seq > frame.loops[-1][3]

    def _return_via_finally(self, frame: _Frame, value: Any) -> int:
        """try 块内的返回：跳去最近的 finally（挂起返回值）。

        返回 finally_ip；没有 finally 则 -1（可直接完成返回流程）。
        """
        stack = self.stack
        while frame.handlers:
            h = frame.handlers.pop()
            if h.finally_ip >= 0:
                del stack[h.sp:]
                frame.pending_return = value
                return h.finally_ip
        return -1


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def run_source(source: str, filename: str = "<输入>") -> Any:
    """源码 → 编译 → 字节码执行。"""
    from .compiler import compile_source

    cmod = compile_source(source, filename)
    return VM(cmod, filename).run()


def run_file(path: str) -> Any:
    from pathlib import Path

    src = Path(path).read_text(encoding="utf-8")
    return run_source(src, path)
