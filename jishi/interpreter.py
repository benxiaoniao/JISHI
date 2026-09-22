# -*- coding: utf-8 -*-
"""基石语言的树遍历解释器（阶段一参考实现）。

职责：直接对 AST 求值。语义自主可控，报错信息全中文。
M0 支持：变量、赋值、算术/比较/逻辑、调用、属性、下标、
列表/字典、如果/遍历/循环N次/当/中断/继续、函数、返回、导入标准库。

**语义实现统一放在 ``runtime.py``**：本文件只负责「怎么遍历 AST」，
与 ``vm.py``（字节码 VM）、``cvm_bind.py``（C VM 宿主桥接）共用同一套语义，
三者行为严格一致（由 ``tests/test_cvm.py`` 的对拍测试保障）。
"""

from __future__ import annotations

from typing import Any, Optional

from . import ast_nodes as A
from . import runtime as R
from .opcodes import ParamKind
from .errors import (
    DebugQuit,
    Frame,
    JishiError,
    RunBreak,
    RunContinue,
    RunError,
    RunTypeError,
    SemanticNameError,
    translate_python_exception,
)
from .runtime import (
    Module,
    _Builtin,
    call_value,
    do_import,
    get_attr,
    get_item,
    method_table,
    new_builtins,
    truthy,
    type_name,
)


# ---------------------------------------------------------------------------
# 运行环境（作用域链）
# ---------------------------------------------------------------------------

class _Unbound:
    """「这个名字是本函数的局部变量，但还没赋过值」的占位（M40）。

    与字节码执行器里的 `_UNSET` 是同一件事的两种说法：函数体里**被赋过值**的
    名字一律算局部（Python 的规则），进函数时先占上未绑定位，于是

    - 函数体内、赋值**之前**读它 → 报「赋值之前被用到了」，而不是穿透到外层；
    - 外层有同名变量也不受影响（内层遮蔽）。

    这修掉了 M39 对拍时发现的那条明确分歧（树遍历原先「先向外找、找到就用」，
    字节码执行器却报错）。判定「哪些名字是局部」直接复用编译器的收集器，
    见 `UserFunction._bound_names`。
    """

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:              # pragma: no cover - 只为调试好看
        return f"<未绑定 {self.name}>"


class Environment:
    __slots__ = ("vars", "parent")

    def __init__(self, parent: Optional["Environment"] = None):
        self.vars: dict[str, Any] = {}
        self.parent = parent

    def get(self, name: str) -> Any:
        env = self
        while env is not None:
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        raise KeyError(name)

    def contains(self, name: str) -> bool:
        env = self
        while env is not None:
            if name in env.vars:
                return True
            env = env.parent
        return False

    def set(self, name: str, value: Any) -> None:
        self.vars[name] = value


# ---------------------------------------------------------------------------
# 用户函数
# ---------------------------------------------------------------------------

class UserFunction:
    __slots__ = ("name", "params", "defaults", "body", "closure", "interp",
                 "annotations", "param_kind", "_assigned")

    def __init__(self, name, params, body, closure, interp,
                 defaults=None, annotations=None, param_kind=None):
        self.name = name
        self.params = params
        #: 与 params 对齐；None 表示无默认。值已在定义处求好（M7）
        self.defaults = defaults or []
        self.body = body
        self.closure = closure
        self.interp = interp
        #: 类型标注文本（M24.3），与 params 对齐，None 表示没写
        self.annotations = annotations or []
        #: 形参种类（M25）：ParamKind.*，与 params 对齐
        self.param_kind = param_kind or []
        #: 惰性缓存的「本函数赋过值的名字」（M40「赋值即局部」），见 `_bound_names`
        self._assigned = None

    def __repr__(self):
        return f"<函数 {self.name}>"

    def _bind_args(self, args, kwargs, *, line=None, col=None,
                   filename="<输入>"):
        """把实参绑到形参上，返回新的调用环境。

        抽出来给两处共用（M25）：树遍历的普通调用（`_eval_call`）与
        Python 侧回调（`__call__`，例如 `列表.映射(基石函数)`）。
        两条路的绑定语义必须完全一致，否则会出现「直接调对、通过高阶函数调错」
        这种很难查的偏差。支持 `*参数` / `**选项` 与默认值。
        """
        kinds = self.param_kind or []
        nparams = len(self.params)
        star_pos = next((i for i, k in enumerate(kinds)
                         if k == ParamKind.VARARGS), -1)
        dstar_pos = next((i for i, k in enumerate(kinds)
                          if k == ParamKind.VARKW), -1)
        # 能吃普通位置实参的个数 = *参数 之前的那些
        npos = nparams
        if star_pos >= 0:
            npos = star_pos
        elif dstar_pos >= 0:
            npos = dstar_pos

        if len(args) > npos and star_pos < 0:
            raise RunTypeError(
                f"函数「{self.name}」需要 {npos} 个参数，"
                f"但传了 {len(args) + len(kwargs)} 个",
                line=line, col=col, filename=filename)

        call_env = Environment(parent=self.closure)
        for i in range(min(len(args), npos)):
            call_env.set(self.params[i], args[i])
        if star_pos >= 0:
            call_env.set(self.params[star_pos], list(args[npos:]))
        extra: dict = {}
        for name, value in kwargs.items():
            if name in self.params[:npos]:
                call_env.set(name, value)
            elif dstar_pos >= 0:
                extra[name] = value
            else:
                raise RunTypeError(
                    f"函数「{self.name}」没有叫「{name}」的参数",
                    line=line, col=col, filename=filename,
                    hint=f"它的参数是：{'、'.join(self.params[:npos])}")
        if dstar_pos >= 0:
            call_env.set(self.params[dstar_pos], extra)
        for i, p in enumerate(self.params[:npos]):
            d = self.defaults[i] if i < len(self.defaults) else None
            if p not in call_env.vars and d is not None:
                call_env.set(p, d)
        # 只查本层（call_env.vars），避免闭包里的同名变量造成误判
        missing = [p for p in self.params[:npos] if p not in call_env.vars]
        if missing:
            raise RunTypeError(
                f"函数「{self.name}」缺少参数：{'、'.join(missing)}",
                line=line, col=col, filename=filename)

        # 类型标注校验（M24.3）：放在绑定之后、进入函数体之前。
        # 与字节码 VM 的「前导调用」同一套语义，保证三执行器一致；
        # 放在 _bind_args 里是为了让 Python 侧回调（__call__）也校验——
        # 否则「直接调用会查、传给高阶函数不查」，方法调用更会整条漏掉
        #（方法就是走 __call__ 的）。
        anns = self.annotations
        if anns:
            desc: list = []
            vals: list = []
            for p, a in zip(self.params, anns):
                if a:
                    desc += [p, a]
                    vals.append(call_env.vars.get(p))
            if desc:
                R._b_check_args(desc, vals, self.name)
        # 「赋值即局部」（M40）：本函数里**赋过值**的名字，此刻先占一个「未赋值」
        # 哨兵，于是「赋值之前读它」会报错——两个字节码执行器一直是这个语义
        # （编译器早就实现好了，还专门为它写了提示语），树遍历原先按「先向外找、
        # 找到就用」静默给值，是一条已记录的三执行器分歧。
        # 名字从编译器的收集器来（见 `_bound_names`），不另写一遍。
        for name in self._bound_names():
            if name not in call_env.vars:
                call_env.vars[name] = _Unbound(name)
        return call_env

    def _bound_names(self) -> frozenset:
        """本函数体里「被赋过值」的名字（= 这个函数的局部变量）。

        **直接复用编译器的收集器**（`compiler._collect_stmts`）：那份逻辑已经
        决定了字节码执行器把哪些名字当局部变量，另写一遍迟早会漂，而漂了就是
        「同一个程序在不同执行器里行为不同」——正是本项目最防的一类问题。
        """
        cached = self._assigned
        if cached is None:
            from .compiler import FUNC_SCOPE, _Scope, _collect_stmts

            scope = _Scope(FUNC_SCOPE, self.name)
            _collect_stmts(self.body, scope, {})
            cached = frozenset(scope.bound)
            self._assigned = cached
        return cached

    def __call__(self, *args, **kwargs):
        """让基石函数可以被 Python 侧调用（M3 桥接：map/sorted 等高阶函数回调）。

        B5 起这里也登记**调试器帧**（M39）：`实例.方法()` 与
        `列表.映射(列表, 基石函数)` 都是从这条路过来的，不登记的话，调试器在
        方法体里会把「我在哪一层」显示成「模块顶层」。调用点由调用方经
        `interp._pending_call_site` 递进来（拿不到就是 0，不猜）。

        只登记**调试器**的帧、不动 `_call_stack`（那是错误回溯用的）：错误回溯
        的帧语义归 M17 管，改它会牵动报错渲染，不在调试器的职责里。
        """
        call_env = self._bind_args(args, kwargs)
        dbg = self.interp.debugger
        if dbg is None:
            try:
                self.interp.exec_block(self.body, call_env)
            except _ReturnSignal as r:
                return r.value
            return None
        line, col = self.interp._pending_call_site
        dbg.push_frame(self.name, call_env, line, col)
        try:
            self.interp.exec_block(self.body, call_env)
        except _ReturnSignal as r:
            return r.value
        finally:
            dbg.pop_frame()
        return None


class _ReturnSignal(Exception):
    def __init__(self, value):
        self.value = value


# ---------------------------------------------------------------------------
# 解释器
# ---------------------------------------------------------------------------

#: 「还没有过顶层表达式语句」的哨兵（M27）。
#: 不能用 None —— `空` 本身就是合法的表达式值，否则 REPL 里输入 `空` 回车
#: 什么都不显示（而 `真` 会显示 `真`），两种写法看起来像不一致。
_NO_VALUE = object()


class Interpreter:
    def __init__(self, filename: str = "<输入>"):
        self.globals = Environment()
        self.filename = filename      # 出错时渲染「文件:行:列」用
        self._last_value = _NO_VALUE  # 最近一次顶层表达式语句的值（REPL 回显用）
        #: 最近一次「非直接调用」的调用点（M39）：方法调用 / Python 高阶函数
        #: 回调要拿它当调试器帧的调用点（见 `UserFunction.__call__`）。
        self._pending_call_site: tuple[int, int] = (0, 0)
        #: 调用帧栈（M17.2）：从外到内，出错时挂到 JishiError.trace
        self._call_stack: list = []
        #: 调试器（M37）：非 None 时每次执行语句前会调它的 before_stmt。
        #: 平时是 None，多出来的开销只有一次属性判断。
        self.debugger = None
        self._register_builtins()

    # -- 内建函数 -----------------------------------------------------------

    def _register_builtins(self):
        for name, value in new_builtins().items():
            self.globals.set(name, value)

    @staticmethod
    def _type_name(v) -> str:
        return type_name(v)

    # -- 顶层执行 -----------------------------------------------------------

    def run(self, program: A.Program) -> None:
        try:
            self.exec_block(program.body, self.globals)
        except JishiError as e:
            # 挂上调用栈回溯（M17.2）：错误对象没有显式 trace 时才补
            if e.trace is None and self._call_stack:
                e.trace = list(self._call_stack)
            raise
        except RecursionError:
            # M17.1：递归超限在解释器递归调用 exec_block 时直接冒出，
            # 绕过 _as_jishi_error，这里兜住翻译成中文，并带上当前调用链
            err = translate_python_exception(
                RecursionError("递归层数太深了"),
                filename=self.filename)
            err.trace = list(self._call_stack) if self._call_stack else None
            raise err

    def exec_block(self, stmts: list[A.Node], env: Environment) -> Any:
        """顺序执行语句块；返回 Return 信号值（函数体用）。"""
        for stmt in stmts:
            self.exec_stmt(stmt, env)
        return None

    # -- 语句 ---------------------------------------------------------------

    def exec_stmt(self, stmt: A.Node, env: Environment) -> None:
        if self.debugger is not None:
            # 调试钩子（M37）：**挂在「每个语句执行前」这一处**就够——断点、
            # 单步（进/过/出）、看变量、看调用栈全都由此推出。钩子只读不写，
            # 所以「带调试器跑」的输出与不带时逐字节相同。
            self.debugger.before_stmt(stmt, env)
        if isinstance(stmt, A.Assign):
            self._do_assign(stmt, env)
        elif isinstance(stmt, A.ExprStmt):
            self._last_value = self.eval_expr(stmt.expr, env)
        elif isinstance(stmt, A.If):
            self._do_if(stmt, env)
        elif isinstance(stmt, A.For):
            self._do_for(stmt, env)
        elif isinstance(stmt, A.Loop):
            self._do_loop(stmt, env)
        elif isinstance(stmt, A.While):
            self._do_while(stmt, env)
        elif isinstance(stmt, A.Break):
            raise RunBreak("中断", line=stmt.line, col=stmt.col, filename=self.filename)
        elif isinstance(stmt, A.Continue):
            raise RunContinue("继续", line=stmt.line, col=stmt.col, filename=self.filename)
        elif isinstance(stmt, A.FuncDef):
            fn = UserFunction(
                stmt.name, stmt.params, stmt.body, env, self,
                defaults=[self.eval_expr(d, env) if d is not None else None
                          for d in stmt.defaults],
                annotations=list(getattr(stmt, "annotations", []) or []),
                param_kind=list(getattr(stmt, "param_kind", []) or []))
            env.set(stmt.name, fn)
        elif isinstance(stmt, A.ClassDef):
            self._do_classdef(stmt, env)
        elif isinstance(stmt, A.Return):
            value = self.eval_expr(stmt.value, env) if stmt.value else None
            raise _ReturnSignal(value)
        elif isinstance(stmt, A.Import):
            self._do_import(stmt, env)
        elif isinstance(stmt, A.Try):
            self._do_try(stmt, env)
        elif isinstance(stmt, A.Raise):
            self._do_raise(stmt, env)
        elif isinstance(stmt, A.Pass):
            pass
        else:
            raise RunError(f"还不支持的语句：{type(stmt).__name__}",
                           line=getattr(stmt, "line", 1),
                           col=getattr(stmt, "col", 1), filename=self.filename)

    def _do_assign(self, stmt: A.Assign, env: Environment) -> None:
        value = self.eval_expr(stmt.value, env)
        target = stmt.target
        if isinstance(target, A.TargetList):
            # 解包赋值：令 a, b = ...
            if stmt.op != "=":
                raise RunTypeError("多赋值（解包）只能用「=」",
                                   line=stmt.line, col=stmt.col,
                                   filename=self.filename)
            values = R.unpack_values(
                value, len(target.names),
                getattr(target, "star_index", None),
                line=stmt.line, col=stmt.col, filename=self.filename)
            for name_node, v in zip(target.names, values):
                env.set(name_node.id, v)
            return
        if isinstance(target, A.Name):
            name = target.id
            if stmt.op == "=":
                env.set(name, value)
            else:
                # 复合赋值：x += v → x = x + v
                old = self._lookup(name, env, stmt)
                op = stmt.op[:-1]  # 去掉 =
                new = R.apply_binop(op, old, value, line=stmt.line,
                                    col=stmt.col, filename=self.filename)
                env.set(name, new)
            return
        if isinstance(target, A.Subscript):
            obj = self.eval_expr(target.obj, env)
            index = self.eval_expr(target.index, env)
            if stmt.op != "=":
                # 走 get_item / set_item 而不是裸下标：它们会把 Python 的
                # KeyError / IndexError 翻成中文的「找不到这个键」/「索引越界」。
                # 裸下标会漏成「程序内部出现未预料的问题（KeyError）：'新词'」
                # ——M45 写调试题集时撞到（`频次[新词] += 1` 统计词频是极常见写法）。
                old = R.get_item(obj, index, line=stmt.line, col=stmt.col,
                                 filename=self.filename)
                R.set_item(obj, index,
                           R.apply_binop(stmt.op[:-1], old, value,
                                         line=stmt.line, col=stmt.col,
                                         filename=self.filename),
                           line=stmt.line, col=stmt.col, filename=self.filename)
            else:
                R.set_item(obj, index, value, line=stmt.line, col=stmt.col, filename=self.filename)
            return
        if isinstance(target, A.Attr):
            obj = self.eval_expr(target.obj, env)
            if stmt.op == "=":
                R.set_attr(obj, target.attr, value, line=stmt.line,
                           col=stmt.col, filename=self.filename)
            else:
                # 复合属性赋值：先读旧值，做运算，再写回（与字节码 VM 一致）
                old = get_attr(obj, target.attr, line=stmt.line, col=stmt.col, filename=self.filename)
                new = R.apply_binop(stmt.op[:-1], old, value,
                                    line=stmt.line, col=stmt.col, filename=self.filename)
                R.set_attr(obj, target.attr, new, line=stmt.line, col=stmt.col, filename=self.filename)
            return
        raise RunError("不支持的赋值目标", line=stmt.line, col=stmt.col, filename=self.filename)

    def _do_if(self, stmt: A.If, env: Environment) -> None:
        for test, body in stmt.branches:
            if truthy(self.eval_expr(test, env)):
                self.exec_block(body, env)
                return
        if stmt.orelse is not None:
            self.exec_block(stmt.orelse, env)

    def _do_for(self, stmt: A.For, env: Environment) -> None:
        iterable = self.eval_expr(stmt.iter, env)
        try:
            it = iter(iterable)
        except TypeError:
            raise RunTypeError(
                f"「{iterable}」不能遍历，遍历需要列表、文本、集合或字典；自定义对象可以定义「迭代()」方法",
                line=stmt.line, col=stmt.col, filename=self.filename)
        try:
            while True:
                item = next(it)
                env.set(stmt.target.id, item)
                try:
                    self.exec_block(stmt.body, env)
                except RunContinue:
                    continue
                except RunBreak:
                    break
        except StopIteration:
            pass

    def _eval_comprehension(self, expr: A.Comprehension, env: Environment):
        """推导式（M7）：[elt 遍历 x 在 it 如果 cond] / {k: v 遍历 ...}。

        循环变量直接写入当前环境（与「遍历」语句一致，会覆盖同名外层变量）。
        """
        iterable = self.eval_expr(expr.iter, env)
        try:
            it = iter(iterable)
        except TypeError:
            raise RunTypeError(
                f"「{iterable}」不能遍历，遍历需要列表、文本、集合或字典；自定义对象可以定义「迭代()」方法",
                line=expr.line, col=expr.col, filename=self.filename)
        if expr.kind == "list":
            result: list = []
            for item in it:
                env.set(expr.target.id, item)
                if expr.condition is not None and not R.truthy(
                        self.eval_expr(expr.condition, env)):
                    continue
                result.append(self.eval_expr(expr.elt, env))
            return result
        result_dict = {}
        for item in it:
            env.set(expr.target.id, item)
            if expr.condition is not None and not R.truthy(
                    self.eval_expr(expr.condition, env)):
                continue
            k = self.eval_expr(expr.key, env)
            v = self.eval_expr(expr.value, env)
            result_dict[k] = v
        return result_dict

    def _do_loop(self, stmt: A.Loop, env: Environment) -> None:
        n = self.eval_expr(stmt.times, env)
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise RunTypeError(f"「{n}」不是有效的循环次数",
                               line=stmt.line, col=stmt.col, filename=self.filename)
        for _ in range(n):
            try:
                self.exec_block(stmt.body, env)
            except RunContinue:
                continue
            except RunBreak:
                break

    def _do_while(self, stmt: A.While, env: Environment) -> None:
        while truthy(self.eval_expr(stmt.test, env)):
            try:
                self.exec_block(stmt.body, env)
            except RunContinue:
                continue
            except RunBreak:
                break

    def _do_import(self, stmt: A.Import, env: Environment) -> None:
        mod = do_import(stmt.name, stmt.from_python, stmt.from_local,
                        line=stmt.line, col=stmt.col, filename=self.filename,
                        alias=stmt.alias)
        env.set(stmt.alias or stmt.name, mod)

    # 类定义：把方法（函数）收集进 JishiClass；继承时复制基类方法表。
    def _do_classdef(self, stmt: A.ClassDef, env: Environment) -> None:
        base = None
        if stmt.base is not None:
            base_obj = self._lookup(stmt.base, env, stmt)
            if not isinstance(base_obj, R.JishiClass):
                raise RunTypeError(
                    f"「{stmt.base}」不是类，不能继承",
                    line=stmt.line, col=stmt.col, filename=self.filename)
            base = base_obj

        methods: dict[str, Any] = {}
        class_vars: dict[str, Any] = {}
        for node in stmt.body:
            if self.debugger is not None:
                # 类体里的每一句也是语句：不告诉调试器的话，「函数 叫(自身)：」
                # 这一行永远停不住，而 `statement_lines` 又说它可停——一个
                # **静默失效**的断点（M39 做字节码调试器时对拍才发现的）。
                self.debugger.before_stmt(node, env)
            if isinstance(node, A.FuncDef):
                fn = UserFunction(
                    node.name, node.params, node.body, env, self,
                    defaults=[self.eval_expr(d, env) if d is not None
                              else None for d in node.defaults],
                    annotations=list(getattr(node, "annotations", []) or []),
                    param_kind=list(getattr(node, "param_kind", []) or []))
                methods[node.name] = fn
            elif (isinstance(node, A.Assign)
                  and isinstance(node.target, A.Name)
                  and node.op == "="):
                # 类变量（M23.3）。值在「定义类的外层作用域」里求值——
                # 这样 `计数 = 某个外部常量` 能用外层名字；写进类变量表，
                # 也不污染外层作用域。
                class_vars[node.target.id] = self.eval_expr(node.value, env)
            elif isinstance(node, A.Pass):
                # 空类占位（`通过` / `:`）——什么都不做（M35）。
                # 解析期已保证类体里只有这三种节点（见 parser._check_class_body），
                # 所以这里跳过即可；空类就是空类。
                continue
            else:
                # 解析期拦不住的（只在 AST 被程序化构造时才可能到这儿）
                raise RunError(
                    f"类体里只能写方法（函数）、类变量（名字 = 值）"
                    f"或空类占位（:），不支持「{type(node).__name__}」",
                    line=node.line, col=node.col, filename=self.filename)
        # JishiClass 构造时自动合并基类方法（继承），子类同名覆盖
        cls = R.JishiClass(stmt.name, methods, base=base,
                           class_vars=class_vars)
        env.set(stmt.name, cls)

    # 尝试 / 捕获 / 最终：借 Python 原生 try/except/finally 的传播语义。
    # 「中断/继续/返回」是异常（RunBreak/RunContinue/_ReturnSignal），
    # 因此它们穿出「尝试」块时「最终」照样执行 —— 行为与 Python 一致。
    # `DebugQuit`（M37，调试器的「退出」）也走这条：下面那个 `except
    # BaseException` 是为「把 Python 异常翻译成中文」而写的，若不显式放行，
    # 用户程序里一句 `捕获：` 就能把「退出」吞掉（见 errors.DebugQuit）。
    def _do_try(self, stmt: A.Try, env: Environment) -> None:
        try:
            self.exec_block(stmt.body, env)
        except (RunBreak, RunContinue, _ReturnSignal, DebugQuit):
            # 离开控制信号不被捕获，但「最终」必须执行
            if stmt.finalbody is not None:
                self.exec_block(stmt.finalbody, env)
            raise
        except BaseException as exc:
            err = self._as_jishi_error(exc)
            for h in stmt.handlers:
                if self._handler_catches(h, err, env):
                    if h.name:
                        h_env = Environment(env)
                        h_env.set(h.name, err)
                    else:
                        h_env = env
                    try:
                        self.exec_block(h.body, h_env)
                    except (RunBreak, RunContinue, _ReturnSignal, DebugQuit):
                        if stmt.finalbody is not None:
                            self.exec_block(stmt.finalbody, env)
                        raise
                    if stmt.finalbody is not None:
                        self.exec_block(stmt.finalbody, env)
                    return
            # 没有处理器接住：执行最终块后继续向上抛
            if stmt.finalbody is not None:
                self.exec_block(stmt.finalbody, env)
            raise err
        else:
            if stmt.finalbody is not None:
                self.exec_block(stmt.finalbody, env)

    def _handler_catches(self, h: A.ExceptHandler, err: JishiError,
                         env: Environment) -> bool:
        if h.type is None:
            return True
        cond = self.eval_expr(h.type, env)
        return R.exception_matches(err, cond, line=h.line, col=h.col,
                                   filename=self.filename)

    def _as_jishi_error(self, exc: BaseException) -> JishiError:
        if isinstance(exc, JishiError):
            return exc
        return translate_python_exception(exc, filename=self.filename)

    # 抛出
    def _do_raise(self, stmt: A.Raise, env: Environment) -> None:
        if stmt.value is None:
            raise RunTypeError(
                "「抛出」后面要写一个值（比如 抛出 值错误(\"说明\")）",
                line=stmt.line, col=stmt.col, filename=self.filename)
        value = self.eval_expr(stmt.value, env)
        raise R.make_exception(value, line=stmt.line, col=stmt.col,
                               filename=self.filename)

    # -- 表达式 -------------------------------------------------------------

    def eval_expr(self, expr: A.Node, env: Environment) -> Any:
        if isinstance(expr, A.Num):
            return expr.value
        if isinstance(expr, A.Str):
            return expr.value
        if isinstance(expr, A.Name):
            if expr.id == "空":
                return None
            return self._lookup(expr.id, env, expr)
        if isinstance(expr, A.Lambda):
            # 匿名函数（M24.4）：造一个与具名函数完全同构的 UserFunction
            # （闭包 = 当前环境），这样调用、报错、类型标注校验全都复用既有逻辑。
            return UserFunction(
                expr.name, expr.params, expr.body, env, self,
                defaults=[self.eval_expr(d, env) if d is not None else None
                          for d in (expr.defaults or [])],
                annotations=list(getattr(expr, "annotations", []) or []),
                param_kind=list(getattr(expr, "param_kind", []) or []))
        if isinstance(expr, A.List):
            return [self.eval_expr(e, env) for e in expr.elements]
        if isinstance(expr, A.Dict):
            return R.build_dict(
                [(self.eval_expr(k, env), self.eval_expr(v, env))
                 for k, v in zip(expr.keys, expr.values)],
                line=expr.line, col=expr.col, filename=self.filename)
        if isinstance(expr, A.Comprehension):
            return self._eval_comprehension(expr, env)
        if isinstance(expr, A.BinOp):
            return R.apply_binop(
                expr.op,
                self.eval_expr(expr.left, env),
                self.eval_expr(expr.right, env),
                line=expr.line, col=expr.col, filename=self.filename)
        if isinstance(expr, A.UnaryOp):
            return R.apply_unary(expr.op, self.eval_expr(expr.operand, env),
                                 line=expr.line, col=expr.col, filename=self.filename)
        if isinstance(expr, A.BoolOp):
            return self._eval_boolop(expr, env)
        if isinstance(expr, A.Compare):
            return self._eval_compare(expr, env)
        if isinstance(expr, A.Call):
            return self._eval_call(expr, env)
        if isinstance(expr, A.Attr):
            return get_attr(self.eval_expr(expr.obj, env), expr.attr,
                            line=expr.line, col=expr.col, filename=self.filename)
        if isinstance(expr, A.Slice):
            # 把切片本身当值求值（M24.2）：这样读 `a[1:3]` 和写 `a[1:3] = …`
            # 走同一条求值路径，赋值切片不需要另写一套逻辑。
            return R.make_slice(
                self.eval_expr(expr.start, env) if expr.start is not None else None,
                self.eval_expr(expr.stop, env) if expr.stop is not None else None,
                self.eval_expr(expr.step, env) if expr.step is not None else None,
                line=expr.line, col=expr.col, filename=self.filename)
        if isinstance(expr, A.Subscript):
            obj = self.eval_expr(expr.obj, env)
            return get_item(obj, self.eval_expr(expr.index, env),
                            line=expr.line, col=expr.col,
                            filename=self.filename)
        raise RunError(f"还不支持的表达式：{type(expr).__name__}",
                       line=getattr(expr, "line", 1),
                       col=getattr(expr, "col", 1), filename=self.filename)

    def _lookup(self, name: str, env: Environment, node: A.Node) -> Any:
        try:
            value = env.get(name)
        except KeyError:
            value = None
        else:
            if not isinstance(value, _Unbound):
                return value
            # 是本函数的局部变量、但这一句之前还没赋过值（M40「赋值即局部」）。
            # 报「赋值之前被用到了」而不是「找不到名字」——后者会让人以为
            # 是拼错了，实际是顺序问题。文案走单一来源，三个执行器逐字一致。
            raise R.unbound_local_error(name, line=node.line, col=node.col,
                                        filename=self.filename,
                                        hint=self._shadow_hint(name, env))
        # 英文 True/False/None：从 Python 代码抄过来时常见，给中文写法提示
        hint = R.PY_NAME_HINT.get(name)
        if hint:
            raise SemanticNameError(
                f"找不到名字「{name}」",
                line=node.line, col=node.col, hint=hint, filename=self.filename)
        # 未定义：给中文报错与"你是不是想写"建议
        candidates = self._suggest(name)
        hint = ""
        if candidates:
            hint = "你是不是想写「" + "」或「".join(candidates) + "」？"
        raise SemanticNameError(
            f"找不到名字「{name}」",
            line=node.line, col=node.col, hint=hint, filename=self.filename)

    def _shadow_hint(self, name: str, env: Environment) -> str:
        """外层也有同名变量时，给一句说得更具体的提示（M40）。

        口径与编译器**完全一致**：那边是「本函数赋过值、外层也 bound 了同一个
        名字」就写进 `Code.local_hints`（字节码执行器报错时用它）；这里就是
        照着同一条件在环境链上问一遍。两边都指向 `runtime.UNBOUND_SHADOW_HINT`
        这一份文本——对拍比的是报错全文，差一个字就算不一致。
        """
        e = env.parent
        while e is not None:
            if name in e.vars and not isinstance(e.vars[name], _Unbound):
                return R.UNBOUND_SHADOW_HINT.format(name=name)
            e = e.parent
        return ""

    def _suggest(self, name: str) -> list[str]:
        import difflib
        pool = list(self.globals.vars.keys())
        return difflib.get_close_matches(name, pool, n=3, cutoff=0.5)

    def _eval_boolop(self, expr: A.BoolOp, env: Environment) -> bool:
        if expr.op == "与":
            for v in expr.values:
                if not truthy(self.eval_expr(v, env)):
                    return False
            return True
        else:  # 或
            for v in expr.values:
                if truthy(self.eval_expr(v, env)):
                    return True
            return False

    def _eval_compare(self, expr: A.Compare, env: Environment) -> bool:
        left = self.eval_expr(expr.left, env)
        for op, right_node in zip(expr.ops, expr.comparators):
            right = self.eval_expr(right_node, env)
            if not R.apply_compare(op, left, right, line=expr.line,
                                   col=expr.col, filename=self.filename):
                return False  # 短路：后续不再求值
            left = right  # 链式：中间值传给下一对
        return True

    def _eval_call(self, expr: A.Call, env: Environment) -> Any:
        fn = self.eval_expr(expr.func, env)
        args = [self.eval_expr(a, env) for a in expr.args]
        kwargs = {name: self.eval_expr(v, env) for name, v in expr.keywords}
        if isinstance(fn, UserFunction):
            # 绑定实参（含 *参数 / **选项）统一走 UserFunction._bind_args，
            # 与 Python 侧回调 __call__ 共用一份逻辑，避免两处漂移（M25）
            call_env = fn._bind_args(args, kwargs, line=expr.line,
                                     col=expr.col, filename=self.filename)
            pushed = False
            try:
                self._call_stack.append(Frame(fn.name, expr.line, expr.col))
                if self.debugger is not None:
                    # 调试器自己另存一份帧（多带 env，才能看那一帧的局部变量）
                    self.debugger.push_frame(fn.name, call_env, expr.line,
                                             expr.col)
                    pushed = True
                self.exec_block(fn.body, call_env)
            except _ReturnSignal as r:
                return r.value
            except JishiError as e:
                # 异常在函数体内冒出：此刻快照调用栈（M17.2），
                # 之后 finally 弹栈、异常继续上抛，栈里的帧不能丢
                if e.trace is None:
                    e.trace = list(self._call_stack)
                raise
            except RecursionError:
                # 递归超限：同样在函数体内冒出，快照调用栈后翻译（M17.1+M17.2）
                err = translate_python_exception(
                    RecursionError("递归层数太深了"),
                    filename=self.filename)
                err.trace = list(self._call_stack)
                raise err
            finally:
                self._call_stack.pop()
                if pushed:
                    self.debugger.pop_frame()
            return None
        # 其余（内建 / 标准库 / Python 生态 / 绑定了实例的方法）走 call_value。
        # 把调用点记下来：**只有「接下来可能进基石函数」的那几类调用**才记——
        # `实例.方法()`、`列表.映射(基石函数)`、`类(参数)`（会进 `初始化`）。
        # 不区分的话，回调体里的一句 `打印(x)` 就会把调用点改成它自己，
        # 第二次回调进来时栈上就显示成上一句 `打印` 的行号（真踩到过，M39）。
        if isinstance(fn, (R._BoundMethod, R._BoundUserMethod, R.JishiClass)):
            self._pending_call_site = (expr.line, expr.col)
        return call_value(fn, args, kwargs, line=expr.line, col=expr.col,
                          filename=self.filename)


def run_source(source: str, filename: str = "<输入>") -> None:
    """便捷入口：源码 → 执行。"""
    from .tokenizer import tokenize
    from .parser import parse

    tokens = tokenize(source, filename)
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    program = parse(tokens, lines, filename)
    Interpreter(filename=filename).run(program)
