# -*- coding: utf-8 -*-
"""基石语言的 AST 节点定义。所有节点统一携带行列号，供中文报错定位。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(kw_only=True)
class Node:
    """AST 节点基类：所有节点携带行列号。"""

    line: int = 1
    col: int = 1


def loc_from(node: Any) -> tuple[int, int]:
    """从任意节点/Token 取 (line, col)。"""
    line = getattr(node, "line", 1) or 1
    col = getattr(node, "col", 1) or 1
    return line, col


# ---------------------------------------------------------------------------
# 语句
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class Program(Node):
    body: list[Node] = field(default_factory=list)


@dataclass(kw_only=True)
class Assign(Node):
    """赋值。op 支持 =、+=、-=、*=、/=、//=、%=、**="""

    target: Node  # Name
    op: str
    value: Node


@dataclass(kw_only=True)
class ExprStmt(Node):
    """表达式语句（如函数调用）"""

    expr: Node


@dataclass(kw_only=True)
class If(Node):
    """如果。branches 为 [(条件, 语句块)]，orelse 为可选的否则块。"""

    branches: list[tuple[Node, list[Node]]]
    orelse: Optional[list[Node]] = None


@dataclass(kw_only=True)
class For(Node):
    """遍历：for 目标 in 可迭代对象"""

    target: Node  # Name
    iter: Node
    body: list[Node]


@dataclass(kw_only=True)
class Loop(Node):
    """循环 N 次"""

    times: Node
    body: list[Node]


@dataclass(kw_only=True)
class While(Node):
    """当：while 条件"""

    test: Node
    body: list[Node]


@dataclass(kw_only=True)
class Break(Node):
    pass


@dataclass(kw_only=True)
class Continue(Node):
    pass


@dataclass(kw_only=True)
class FuncDef(Node):
    name: str
    params: list[str]
    body: list[Node]
    #: 与 params 等长；None 表示该参数无默认值（M7 默认参数）
    defaults: list = field(default_factory=list)
    #: 与 params 等长；类型标注的**文本**（M24.3），None 表示没写（M24.3）。
    #: 存文本而不是 AST 是有意的：标注不参与求值，只作文档与可选校验的键，
    #: 这样也不会因为标注里写了表达式而产生副作用或作用域问题。
    annotations: list = field(default_factory=list)
    #: 形参种类（M25）：ParamKind.NORMAL / VARARGS(`*参数`) / VARKW(`**选项`)。
    #: 与 params 对齐；只记整数（不引 opcodes，保持 AST 层无依赖）。
    param_kind: list = field(default_factory=list)
    #: 返回类型标注的文本（M24.3），None 表示没写
    returns: Optional[str] = None


@dataclass(kw_only=True)
class Lambda(Node):
    """匿名函数表达式（M24.4）：`函数(x)：x * 2`。

    只能写**单个表达式**作函数体（需要多行就写具名函数）。
    字段与 FuncDef 对齐，好让编译器复用 `_make_code` 那套逻辑
    （闭包单元、默认值、MAKE_FUNCTION 全都一致）。
    """

    params: list[str]
    body: list[Node]
    defaults: list = field(default_factory=list)
    annotations: list = field(default_factory=list)
    #: 形参种类（M25），与 params 对齐；与 FuncDef 同义
    param_kind: list = field(default_factory=list)
    name: str = "匿名函数"


@dataclass(kw_only=True)
class ClassDef(Node):
    """类：类 名 继承 基类:（方法即函数定义，构造方法约定名「初始化」）。"""

    name: str
    base: Optional[str] = None
    body: list[Node] = field(default_factory=list)


@dataclass(kw_only=True)
class Return(Node):
    value: Optional[Node] = None


@dataclass(kw_only=True)
class Raise(Node):
    """抛出：raise 表达式（表达式可省，表示重新抛出当前异常）。"""

    value: Optional[Node] = None


@dataclass(kw_only=True)
class ExceptHandler(Node):
    """捕获 类型 为 名字：

    type 为 None 表示裸捕获（全接）；name 为 None 表示不绑定变量。
    """

    type: Optional[Node] = None
    name: Optional[str] = None
    body: list[Node] = field(default_factory=list)


@dataclass(kw_only=True)
class Try(Node):
    """尝试 / 捕获 / 最终。

    handlers 为 ExceptHandler 列表；finalbody 为可选的最终块。
    """

    body: list[Node] = field(default_factory=list)
    handlers: list[ExceptHandler] = field(default_factory=list)
    finalbody: Optional[list[Node]] = None


@dataclass(kw_only=True)
class Import(Node):
    name: str
    from_python: bool = False
    from_local: bool = False
    alias: Optional[str] = None


@dataclass(kw_only=True)
class Pass(Node):
    """占位语句：什么都不做（用于空块）"""


# ---------------------------------------------------------------------------
# 表达式
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class Num(Node):
    value: float | int


@dataclass(kw_only=True)
class Str(Node):
    value: str


@dataclass(kw_only=True)
class Name(Node):
    id: str


@dataclass(kw_only=True)
class TargetList(Node):
    """解包赋值目标：令 a, b = ... 中的「a, b」（仅简单名字）。

    `star_index`（M25）不为 None 时表示带星号：`令 a, *余 = …`，
    该位置的元素收集「其余」成列表，其余位置取首尾对应的单个元素。
    """

    names: list[Name]
    star_index: Optional[int] = None


@dataclass(kw_only=True)
class List(Node):
    elements: list[Node]


@dataclass(kw_only=True)
class Dict(Node):
    keys: list[Node]
    values: list[Node]


@dataclass(kw_only=True)
class Comprehension(Node):
    """推导式（M7）：[elt 遍历 x 在 it] / [elt 遍历 x 在 it 如果 cond]
    及字典推导 {k: v 遍历 x 在 it 如果 cond}。

    kind: "list" 或 "dict"；dict 时用 key/value，list 时用 elt。
    target/iter/condition 与 For/If 一致；condition 可为 None。
    """

    kind: str
    elt: Node
    key: Node
    value: Node
    target: Node
    iter: Node
    condition: Node


@dataclass(kw_only=True)
class BinOp(Node):
    left: Node
    op: str
    right: Node


@dataclass(kw_only=True)
class UnaryOp(Node):
    op: str  # "-", "非"
    operand: Node


@dataclass(kw_only=True)
class BoolOp(Node):
    op: str  # "与", "或"
    values: list[Node]


@dataclass(kw_only=True)
class Compare(Node):
    """比较。支持链式：1 < x < 5 → left=1, ops=["<", "<"], comparators=[x, 5]"""

    left: Node
    ops: list[str]
    comparators: list[Node]


@dataclass(kw_only=True)
class Call(Node):
    """函数调用。keywords 为 [(参数名, 值表达式)]，用于「f(a, 名字=值)」"""

    func: Node
    args: list[Node] = field(default_factory=list)
    keywords: list[tuple[str, Node]] = field(default_factory=list)


@dataclass(kw_only=True)
class Attr(Node):
    obj: Node
    attr: str


@dataclass(kw_only=True)
class Subscript(Node):
    obj: Node
    index: Node


@dataclass(kw_only=True)
class Slice(Node):
    """切片（M18.1）：`列表[起:止:步长]`，三个分量均可为 None（省略）。"""
    start: Optional["Node"] = None
    stop: Optional["Node"] = None
    step: Optional["Node"] = None
