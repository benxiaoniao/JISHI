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
    iter_size_changed,
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
    __slots__ = ("vars", "parent", "decl_globals", "块引入")

    def __init__(self, parent: Optional["Environment"] = None, func: bool = False,
                 块引入=None):
        self.vars: dict[str, Any] = {}
        self.parent = parent
        #: 这一层**函数帧**里 `全局 甲` 声明过的名字（M210 / A2）；
        #: 模块层是 `None`（块层见 `块引入`）。
        #: 挂在这里而不是解释器的栈上：两条调用路径（`__call__` 与
        #: `_eval_call`）都要用，栈就得两处各压一次、各弹一次 —— 迟早漏一边。
        self.decl_globals: Optional[set[str]] = set() if func else None
        #: 🔴 **M231 完整版**：这一层是**块层**时，装「本块**引入**了哪些名字」。
        #:
        #: `None` = 不是块层（函数层 / 模块层）。非 None（可以是空集）= 块层。
        #: 为什么需要它：块里的 `令` 要分两种 ——
        #:   · **本块引入的**（`令 j = i`）⇒ 落在**本块**（每轮/每次一个新格子，
        #:     闭包才能抓到各自那份）；
        #:   · **外层已有的**（`令 总 = 总 + 1` 的累加）⇒ 落到**外层**去改
        #:     （A2 口径；全仓 122 处这种写法）。
        #: 没有这个集合就只能二选一，两个必坏一个 —— 这是踩出来的。
        self.块引入 = 块引入

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
        """写一个名字。

        📌 **M231（块级作用域）**：按层分别处理，语义**故意不一样**：

        - **函数层 / 模块层**：只写本层（`函数内赋值即局部`，`全局` 声明才有意义）；
        - **块层**：只在「**本块链**」里找名字 —— 而且只认**本块引入的**那些。
          找到了就改那个块层；不是任何块引入的，**退化成外层的普通 set**。

        为什么块层不能「一律本层新建」：`令 总 = 总 + 1` 这种累加写法全仓 122 处
        （自测脚本、业务代码到处都是）。一律本层新建会静默变成「块内新变量」，
        块外还是 0 —— 那是最坏的一种错（不报错、看着对）。

        为什么块层也不能「沿链随便找」：那会**穿透函数边界**——
        `函数 f()：如果 真：令 x = 2` 会把模块层的 `x` 改掉（函数内赋值本该是局部）。
        """
        if self.块引入 is not None:              # 块层
            环境: Optional["Environment"] = self
            while 环境 is not None and 环境.块引入 is not None:
                if name in 环境.块引入 and not isinstance(
                        环境.vars.get(name), _Unbound):
                    环境.vars[name] = value
                    return
                环境 = 环境.parent
            # 不是任何块引入的 ⇒ 当成外层的一次普通赋值（函数层只写本层）
            if 环境 is not None:
                环境.vars[name] = value
                return
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

        call_env = Environment(parent=self.closure, func=True)
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
        # 默认值：`self.defaults` 是**紧凑尾部** —— 尾部 `len(defaults)` 个参数
        # 有默认值（与 C VM 的 `first_def = npos - ndefaults` 同一算法）。
        # ⚠️ **不能**写成「遍历所有参数、`d is not None` 才补」：默认值本身
        # 可能就是 `空`（= None），那样会被当成「没有默认值」而误报缺少参数
        # （2026-10-11 修）。
        n_def = len(self.defaults)
        first_def = npos - n_def
        for i in range(max(first_def, 0), npos):
            p = self.params[i]
            if p not in call_env.vars:
                call_env.set(p, self.defaults[i - first_def])
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
            # 🔴 **M231 完整版（2026-11-25 踩到的）**：**形参也要进 `bound`**。
            #
            # 这里复用编译器收集器是有代价的 —— `_collect_stmts` 会给 AST 节点
            # **挂块信息**（`_体块们` / `_块引入` 等）。而「外层已有」的判据是
            # `scope.bound` 的快照：不预先放形参进去，`函数 f(字段们)：如果 …：
            # 字段们 = [字段们]` 里的 `字段们` 就会被算成「本块引入」，
            # 于是块层 `set` 落进块、出块就没了（形参改不动）——
            # 而字节码那边编译期分配的是形参槽位，两边行为直接分叉。
            #
            # 症状是 `数据库.建索引(库, 表, "类别")` 报「索引字段要给一个非空的
            # 字段名列表」：库里那句 `字段们 = [字段们]`（单个字符串 ≡ 单元素
            # 列表）在树遍历里没生效。查了半天。
            scope.bound.update(self.params)
            _collect_stmts(self.body, scope, {})
            # M210 / A2：`全局` 声明过的**不是**局部（否则会被种上「未赋值」
            # 哨兵，读它就报「赋值之前被用到了」）。
            # 🔴 **M231**：循环体里引入的名字（`scope.块绑定`）**不算**函数局部 ——
            # 它们只在块里有效，函数入口不该给它们种「未赋值」哨兵。
            # 不排除的话，块层的 `令 i` 会撞上函数层那个哨兵（见 `set` 里的说明），
            # 循环变量跑出块层、循环外照样读得到 —— `test_m40_scope` / `test_m80`
            # 的语料 25 都是这么炸的。
            cached = frozenset(scope.bound - scope.globals - scope.块绑定)
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



def _检查块作用域(program, filename: str, lines) -> None:
    """**M231**：解释器没有「编译期」，但「块越界引用」必须在**执行前**报。

    不预检会怎样：树遍历跑完循环、打印一堆东西，然后才在 `返回 i` 那儿报错；
    字节码宿主是编译期直接报 —— 两边输出就一半有一半没有（`test_m80` 的
    语料 25 / `test_m40_scope` 都撞过）。

    ⚠️ 规则**复用编译器的收集器**（`_collect_stmts` 给的 `_块内绑定` /
    `_块引入`），不另写一遍 —— 两份规则迟早会漂，而漂了就是「同一个程序在
    不同执行器里行为不同」。
    """
    import dataclasses

    from .compiler import MODULE_SCOPE, _Scope, _collect_stmts
    from .errors import SemanticNameError

    scopes: dict = {}
    root = _Scope(MODULE_SCOPE, "<模块>")
    _collect_stmts(program.body, root, scopes)

    活跃: list = []           # 当前活跃的循环体块（每层一组名字）
    已退出: set = set()       # 已经走完的循环体**引入**过的名字

    def 查名字(n) -> None:
        if n.id in 已退出 and not any(n.id in s for s in 活跃):
            raise SemanticNameError(
                f"找不到名字「{n.id}」",
                line=getattr(n, "line", 1), col=getattr(n, "col", 1),
                filename=filename,
                hint=(f"「{n.id}」是在**块里**（循环体 / 如果分支 / 尝试体…）"
                      f"`令` 的 —— 它只在那个块里有效（M231 块级作用域）。"
                      f"要在块外用它，就在块**之前**先 `令 {n.id} = …` 声明一下"),
            ).with_source(lines or [])

    def 查表达式(e) -> None:
        if e is None:
            return
        if isinstance(e, list):
            for x in e:
                查表达式(x)
            return
        if isinstance(e, A.Name):
            查名字(e)
            return
        # 🔴 **M231**：匿名函数也是**自己的作用域** —— 它的形参（`函数(名)：名`）
        # 与循环体里的同名变量毫无关系。不开边界的话，参数名撞上「已退出」就误报
        # （`数据库.jsh` 的 `排序按(函数(名)：名)` 撞过）。
        if isinstance(e, A.Lambda):
            存活, 存退 = list(活跃), set(已退出)
            活跃.clear()
            已退出.clear()
            try:
                查表达式(getattr(e, "body", None))
            finally:
                活跃[:] = 存活
                已退出.clear()
                已退出.update(存退)
            return
        # 🔴 **M231**：推导式是**自己的作用域**（`[行[0] 遍历 行 在 有效]`）——
        # `行` 只在那对方括号里有效，撞上外层「已退出」的 `行` 不是错。
        # 这里开一次边界，与 `遍历` 语句对齐（`examples/projects/日志统计` 撞过）。
        if isinstance(e, A.Comprehension):
            存活, 存退 = list(活跃), set(已退出)
            活跃.clear()
            已退出.clear()
            try:
                for 名 in ("elt", "key", "value", "iter", "target", "condition"):
                    查表达式(getattr(e, 名, None))
            finally:
                活跃[:] = 存活
                已退出.clear()
                已退出.update(存退)
            return
        if not dataclasses.is_dataclass(e):
            return
        for f in dataclasses.fields(e):
            值 = getattr(e, f.name, None)
            if isinstance(值, (A.Node, list)):
                查表达式(值)

    def 走语句们(stmts, 边界: bool = False) -> None:
        if 边界:                       # 函数 / 类体是新的块边界
            存活跃, 存退出 = list(活跃), set(已退出)
            活跃.clear()
            已退出.clear()
        for st in stmts:
            走语句(st)
        if 边界:
            活跃[:] = 存活跃
            已退出.clear()
            已退出.update(存退出)

    def 走一块(体, 块) -> None:
        """走一段**块语句**（`如果` 分支 / `尝试` / `捕获` / `最终`）。

        `块` 是编译器收集好的 `(体内绑定, 本块引入)`；`None` 表示空块。
        """
        活跃.append(块[0] if 块 else set())
        走语句们(体)
        活跃.pop()
        if 块:
            已退出.update(块[1])

    def 走语句(st) -> None:
        if isinstance(st, (A.FuncDef, A.ClassDef)):
            for d in (getattr(st, "defaults", None) or []):   # 默认值在外层求值
                查表达式(d)
            走语句们(st.body, 边界=True)
            return
        if isinstance(st, (A.For, A.Loop, A.While)):
            for 名 in ("iter", "times", "test"):
                查表达式(getattr(st, 名, None))
            # ⚠️ **不查循环变量本身**：它是**本块引入**的，`遍历 i` 连着写两次
            #    （第二段的 `i` 撞上第一段的「已退出」）会被误判 —— 与编译器那边
            #    `store_name` 先「摘掉已退出」是同一个道理。
            活跃.append(getattr(st, "_块内绑定", set()))
            走语句们(st.body)
            活跃.pop()
            已退出.update(getattr(st, "_块引入", set()))
            return
        if isinstance(st, A.Assign):
            查表达式(st.value)
            if isinstance(st.target, A.Name):
                # 复合赋值（`+=`）要先读旧值 → 那时才查；
                # 普通 `令 x = v` 是**重新声明**，直接把它从「已退出」摘掉
                # （与编译器 `store_name` 里的 `discard` 逐条对应）。
                if st.op != "=":
                    查名字(st.target)
                已退出.discard(st.target.id)
            else:
                查表达式(st.target)
            return
        if isinstance(st, A.If):
            # M231 完整版：每一段都是块（条件在外层求值）
            体块们 = getattr(st, "_体块们", [])
            for 序, (测, 体) in enumerate(st.branches):
                查表达式(测)
                走一块(体, 体块们[序] if 序 < len(体块们) else None)
            if st.orelse:
                序 = len(st.branches)
                走一块(st.orelse, 体块们[序] if 序 < len(体块们) else None)
            return
        if isinstance(st, A.Try):
            # M231 完整版：尝试体 / 每个捕获体 / 最终体各一个块
            走一块(st.body, getattr(st, "_尝试块", None))
            捕获块们 = getattr(st, "_捕获块们", [])
            for 序, h in enumerate(st.handlers):
                查表达式(getattr(h, "type", None))
                走一块(h.body, 捕获块们[序] if 序 < len(捕获块们) else None)
            if getattr(st, "finalbody", None):
                走一块(st.finalbody, getattr(st, "_最终块", None))
            return
        for f in dataclasses.fields(st):       # 其余语句：表达式部分通用走
            值 = getattr(st, f.name, None)
            if isinstance(值, (A.Node, list)):
                查表达式(值)

    走语句们(program.body)


class Interpreter:
    def __init__(self, filename: str = "<输入>", source_lines=None):
        self.globals = Environment()
        self.filename = filename      # 出错时渲染「文件:行:列」用
        #: **M231**：源码行 —— 块越界的预检报错要画「第几行 + `^`」，与字节码同形
        self.source_lines = source_lines
        self._last_value = _NO_VALUE  # 最近一次顶层表达式语句的值（REPL 回显用）
        #: 最近一次「非直接调用」的调用点（M39）：方法调用 / Python 高阶函数
        #: 回调要拿它当调试器帧的调用点（见 `UserFunction.__call__`）。
        self._pending_call_site: tuple[int, int] = (0, 0)
        #: 调用帧栈（M17.2）：从外到内，出错时挂到 JishiError.trace
        self._call_stack: list = []
        #: 「正在处理的异常」栈（M148）：进入 `捕获` 体时压、离开时弹。
        #: 这比字节码那套「按 ip 区间判断」更直接（这里是 Python 原生
        #: try/except，天然知道自己在不在捕获体里），两边结果**必须一致**
        #: —— 见 `tests/test_m148_cause.py` 的四引擎对拍。
        self._handling: list = []
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
        # 🔴 **M231**：块作用域的越界引用要在**执行前**报（解释器没有编译期，
        #    见 `_检查块作用域` 的说明）。放在最前面 —— 必须在任何 `打印` 之前。
        _检查块作用域(program, self.filename,
                      getattr(program, "_source_lines", None) or self.source_lines)

        # M52：先把「用基石写的标准库」展开进本程序（`导入 统计` →
        # `统计 = __基石库_统计__()`）。没用到基石库时这一步是零开销的
        # （`expand` 先探一遍，没命中就直接返回传入的对象）。见 `jishilib.py`。
        from . import jishilib

        program = jishilib.expand(program)
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
            # 默认值存**紧凑尾部**（只留有默认值的那些）：AST 那份与形参等长
            # 对齐、`None` 表示「没默认值」，但**运行期** `空` 就是 `None`，
            # 拿 `None` 当哨兵会把「默认值是空」误判成「没有默认值」。
            fn = UserFunction(
                stmt.name, stmt.params, stmt.body, env, self,
                defaults=[self.eval_expr(d, env)
                          for d in stmt.defaults if d is not None],
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
        elif isinstance(stmt, A.Global):
            # M210 / A2：`全局 甲, 乙` —— 记在**当前函数帧**上。
            # 模块顶层写它没有意义（那时本来就在全局），当空操作。
            if env.decl_globals is not None:
                env.decl_globals.update(stmt.names)
        elif isinstance(stmt, A.Pass):
            pass
        else:
            raise RunError(f"还不支持的语句：{type(stmt).__name__}",
                           line=getattr(stmt, "line", 1),
                           col=getattr(stmt, "col", 1), filename=self.filename)

    def _set_name(self, name: str, value, env: Environment) -> None:
        """赋值到名字（M210 / A2）：`全局` 声明过的写**模块环境**，其余写当前环境。

        ⚠️ 与字节码那条路必须同语义：那边是「声明过 ⇒ 不进 bound ⇒
        STORE_GLOBAL」，这边就是「写 self.globals」。两边由 `test_m210_global`
        拿同一段程序在四个引擎上对拍。
        """
        if env.decl_globals and name in env.decl_globals:
            self.globals.set(name, value)
        else:
            env.set(name, value)

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
                self._set_name(name, value, env)
            else:
                # 复合赋值：x += v → x = x + v
                old = self._lookup(name, env, stmt)
                op = stmt.op[:-1]  # 去掉 =
                new = R.apply_binop(op, old, value, line=stmt.line,
                                    col=stmt.col, filename=self.filename)
                self._set_name(name, new, env)
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

    def _块env(self, env: Environment, 块信息) -> Environment:
        """**M231**：给一段语句开一个**块层**环境。

        `块信息` 是 `(体内绑定, 本块引入)`（编译器收集时算好的）。块层只认
        「本块引入」的名字（见 `Environment.set`）—— 外层已有的照旧改外层。
        """
        return Environment(parent=env, 块引入=(块信息[1] if 块信息 else set()))

    def _do_if(self, stmt: A.If, env: Environment) -> None:
        # 🔴 **M231 完整版**：`如果` 的每一段都是一个块（条件在外层求值）。
        体块们 = getattr(stmt, "_体块们", [])
        for 序, (test, body) in enumerate(stmt.branches):
            if truthy(self.eval_expr(test, env)):
                块 = 体块们[序] if 序 < len(体块们) else None
                self.exec_block(body, self._块env(env, 块))
                return
        if stmt.orelse is not None:
            序 = len(stmt.branches)
            块 = 体块们[序] if 序 < len(体块们) else None
            self.exec_block(stmt.orelse, self._块env(env, 块))

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
                try:
                    item = next(it)
                except RuntimeError:
                    # 集合 / 字典在遍历中被改了大小：Python 原生异常是英文、无码
                    # 无行列 —— 换成基石错误（D77 §二，文案与四执行器共用一份）。
                    raise iter_size_changed(
                        line=stmt.line, col=stmt.col,
                        filename=self.filename) from None
                # 🔴 **M231：每轮一个新块层**（D1 晚绑定的治本）。
                #   · 块层里 `令 新名字` 落在块层 ⇒ 每轮一个新格子，
                #     所以 `遍历 i 在 …：令 j = i` 之后，每轮那个闭包抓到的
                #     `j` 是各自的（以前全是最后一轮的）；
                #   · 循环变量走 `set`（**块层语义：外层有就改它，没有才新建**）
                #     —— 这样与字节码 VM 的「共享槽位」逐条对齐：外层本来就有
                #     `i` 时，循环后读到的是最后一轮（两边一样）。
                轮 = self._块env(env, (getattr(stmt, "_块内绑定", set()),
                                      getattr(stmt, "_块引入", set())))
                轮.set(stmt.target.id, item)
                try:
                    self.exec_block(stmt.body, 轮)
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
                # M231：每轮一个新块层（循环体内 `令 新名字` 每轮独立）
                self.exec_block(stmt.body,
                                self._块env(env, (getattr(stmt, "_块内绑定", set()),
                                                 getattr(stmt, "_块引入", set()))))
            except RunContinue:
                continue
            except RunBreak:
                break

    def _do_while(self, stmt: A.While, env: Environment) -> None:
        while truthy(self.eval_expr(stmt.test, env)):
            try:
                # M231：同 `_do_loop` —— 每轮新块层。
                # ⚠️ 条件 `stmt.test` 仍在**外层** env 求值，所以
                #    `令 i = 0` + `当 i < 10：i = i + 1` 照旧能停：
                #    块层的 `i = …` 会「改最近那层」（见 `Environment.set`）。
                self.exec_block(stmt.body,
                                self._块env(env, (getattr(stmt, "_块内绑定", set()),
                                                 getattr(stmt, "_块引入", set()))))
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
                    defaults=[self.eval_expr(d, env)
                              for d in node.defaults if d is not None],
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
        # 🔴 **M231 完整版**：`尝试` 体 / 每个 `捕获` 体 / `最终` 体**各是一个块**。
        终块 = getattr(stmt, "_最终块", None)
        终块env = self._块env(env, 终块) if stmt.finalbody is not None else env
        捕获块们 = getattr(stmt, "_捕获块们", [])
        try:
            self.exec_block(stmt.body, self._块env(env, getattr(stmt, "_尝试块", None)))
        except (RunBreak, RunContinue, _ReturnSignal, DebugQuit):
            # 离开控制信号不被捕获，但「最终」必须执行
            if stmt.finalbody is not None:
                self.exec_block(stmt.finalbody, 终块env)
            raise
        except BaseException as exc:
            err = self._as_jishi_error(exc)
            # ⚠️ **调用链要在「捕获的那一刻」补拍**（R5.1）：`_eval_call` 只在
            # 错误**跨出函数边界**时才挂 trace（M17.2），而 VM 是在错误**冒出来的
            # 那一刻**就挂（`vm._dispatch_exception`）。于是「在同一个函数体里抛出、
            # 又被自己的 `尝试` 接住」的错误，两边渲染不一致：
            #     文本(e)   →  树遍历没有「调用链」，VM / C VM 有
            # 在 `尝试` 接住时补一份，此刻栈还完整（`_eval_call` 的 finally 还没弹），
            # 与 VM 的口径就对齐了。已有 trace 的不动（更内层的信息更准）。
            if err.trace is None and self._call_stack:
                err.trace = list(self._call_stack)
            for h in stmt.handlers:
                if self._handler_catches(h, err, env):
                    # ⚠️ **这里曾经给「捕获 为 名字」单独建了一个子作用域**
                    # （`h_env = Environment(env)`），于是**捕获体里的赋值全被
                    # 丢进那个子作用域、出了捕获体就没了**：
                    #
                    #     函数 f()：
                    #         尝试：
                    #             抛出 值错误("x")
                    #         捕获 值错误 为 标记：
                    #             令 结果 = 1        ← 进了子作用域
                    #         返回 结果              ← 树遍历报「赋值之前被用到」，
                    #                                  VM / C VM 给 1
                    #
                    # 全语言**只有这一处**是这样（`如果` / `循环` / `遍历` / `当` /
                    # `尝试` 体 / `最终` 都直接用外层 `env`），文档也早已定了口径：
                    # 「循环变量与「遍历」语句一致（**写入当前作用域**）」。
                    # 所以捕获名就是**当前作用域里的一次赋值**，捕获体也在当前作用域跑
                    # —— 与编译器（把 `h.name` 与捕获体里的赋值都算作本作用域的局部）
                    # 以及两个 VM 一致。R5.1 修。
                    # 捕获名属于**这个捕获块**（M231 完整版）：块外读它要报错。
                    序 = stmt.handlers.index(h)
                    块 = 捕获块们[序] if 序 < len(捕获块们) else None
                    捕块env = self._块env(env, 块)
                    if h.name:
                        捕块env.set(h.name, err)
                    # ⚠️ `_handling` 要盖住**捕获体 + 最终块**（M148）：字节码那边
                    # 的捕获区右边界取的是 `after`（整条语句结束），所以最终块里
                    # 抛出的错误在两边都得挂同一个「起因」。`抛出 e` 自己时由
                    # `make_exception` 挡掉自环，这里不用管。
                    self._handling.append(err)
                    try:
                        try:
                            self.exec_block(h.body, 捕块env)
                        except (RunBreak, RunContinue, _ReturnSignal, DebugQuit):
                            if stmt.finalbody is not None:
                                self.exec_block(stmt.finalbody, 终块env)
                            raise
                        if stmt.finalbody is not None:
                            self.exec_block(stmt.finalbody, 终块env)
                    finally:
                        self._handling.pop()
                    return
            # 没有处理器接住：执行最终块后继续向上抛。
            # `_handling` 照样要盖住最终块 —— 字节码那边这条路（reraise）
            # 的 ip 也在捕获区里，最终块抛出的错误同样挂这个起因（M148）。
            self._handling.append(err)
            try:
                if stmt.finalbody is not None:
                    self.exec_block(stmt.finalbody, 终块env)
            finally:
                self._handling.pop()
            raise err
        else:
            if stmt.finalbody is not None:
                self.exec_block(stmt.finalbody, 终块env)

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
            # M220：**裸 `抛出`** = 重抛当前正在处理的异常（类型/消息/起因链都保住）。
            # 没有可重抛的 ⇒ 「运行期错误」并说清该写在哪 —— 与字节码三个执行器逐字一致。
            if not self._handling:
                raise RunError(
                    "没有正在处理的异常可以重抛 —— 裸「抛出」只能写在"
                    "「捕获」或「最终」里；要抛新错误写 抛出 值错误(\"说明\")",
                    line=stmt.line, col=stmt.col, filename=self.filename)
            raise R.make_exception(self._handling[-1], line=stmt.line, col=stmt.col,
                                   filename=self.filename, cause=None)
        value = self.eval_expr(stmt.value, env)
        raise R.make_exception(value, line=stmt.line, col=stmt.col,
                               filename=self.filename,
                               cause=self._handling[-1] if self._handling else None)

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
                defaults=[self.eval_expr(d, env)
                          for d in (expr.defaults or []) if d is not None],
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
        # ⚠️ 没有候选名就是 **None**（不是空串）—— 与 `runtime.py` 那条
        # 同源实现、以及字节码 VM / C VM 给出来的 JSON 对齐（R7.4 修：
        # 以前这里写 `""`，于是 `--json-errors` 里同一个错三个执行器
        # 两个给 `null`、树遍历给 `""`）。渲染那一边不受影响（`if self.hint:`
        # 空串本来也不印），差的只是机器可读的那份。
        hint = None
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
    from .parser import parse_source

    program = parse_source(source, filename)
    Interpreter(filename=filename).run(program)
