"""AST 遍历与作用域分析 —— LSP 与静态检查共用的一份。

**为什么要单独一个模块**：编辑器里的实时诊断（`lsp.py`）和命令行的
`jishi 源码静态检查`（`checker.py`）要回答的是同一批问题——「这个名字定义过吗」
「这个名字是绑定还是读取」。两处各写一份，迟早会漂移成「编辑器说不报、命令行说
报」。所以这里是**唯一实现**，两边都 import 它（沿用本项目「数据源单一」的约定，
同 `ai.build_lang_spec()`）。

公开的是不带下划线的名字；`lsp.py` 用 `from .scope import ... as _xxx` 引入，
保持它内部原有的调用点不变。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterator, Optional

from . import ast_nodes as A

__all__ = [
    "flatten", "child_nodes", "walk_nodes", "defined_names",
    "binding_marks", "load_names", "attr_base_marks", "attr_of",
    "is_bound_target",
]


def flatten(value: Any) -> Iterator[A.Node]:
    """把任意字段值里的 AST 节点摊平出来（支持列表/元组嵌套）。

    **`TargetList` 要摊成里面的名字**（M41 修）：`令 甲, 乙 = …` 与
    `遍历 序, 项 在 …` 的 target 都是 `TargetList`，它自己**不是** `Name`——
    原先只 yield 出 `TargetList` 本身，于是解包出来的名字在 LSP 眼里
    「从未定义」（`test_corpus_has_no_false_positive_warnings` 报出来）。
    这条缺陷在 `令 甲, 乙 = …` 上早就存在，M41 的 `遍历` 解包让它暴露得更明显。
    """
    if isinstance(value, A.TargetList):
        for n in value.names:
            yield from flatten(n)
    elif isinstance(value, A.Node):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from flatten(item)


def child_nodes(node: Any) -> Iterator[A.Node]:
    """遍历一个 AST 节点的直接子节点。"""
    if not dataclasses.is_dataclass(node):
        return
    for f in dataclasses.fields(node):
        try:
            value = getattr(node, f.name)
        except AttributeError:
            continue
        yield from flatten(value)


def walk_nodes(root: Any) -> Iterator[A.Node]:
    """深度优先遍历一棵 AST（含 root 自己）。

    只走**语法子节点**，不进入 `FuncDef` / `ClassDef` 之外的特殊结构——
    也就是说函数体是走得到的，需要「不进入嵌套函数」的检查自己截断。
    """
    stack = [root]
    while stack:
        node = stack.pop()
        if not isinstance(node, A.Node):
            continue
        yield node
        stack.extend(child_nodes(node))


# ---------------------------------------------------------------------------
# 定义与使用
# ---------------------------------------------------------------------------

def defined_names(program: A.Program) -> set[str]:
    """文档内所有被绑定的名字（宽松收集：任何作用域都算已定义）。"""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, A.FuncDef):
            names.add(node.name)
            names.update(p for p in node.params if p)
        elif isinstance(node, A.ClassDef):
            names.add(node.name)
        elif isinstance(node, A.ExceptHandler):
            if node.name:
                names.add(node.name)
        elif isinstance(node, A.Import):
            names.add(node.alias or node.name)
        elif isinstance(node, (A.Assign, A.For, A.Comprehension)):
            for n in flatten(getattr(node, "target", None)):
                if isinstance(n, A.Name):
                    names.add(n.id)
        for child in child_nodes(node):
            walk(child)

    walk(program)
    return names


def binding_marks(program: A.Program) -> set[int]:
    """标出处于「绑定位置」的 Name 节点（这些不算使用）。

    用 `id(node)` 当键——AST 节点是**可变对象**，`id()` 在整个分析期间稳定，
    且比给节点加字段干净（不改动 AST 本身）。
    """
    marks: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, (A.Assign, A.For, A.Comprehension)):
            for n in flatten(getattr(node, "target", None)):
                if isinstance(n, A.Name):
                    marks.add(id(n))
        for child in child_nodes(node):
            walk(child)

    walk(program)
    return marks


def load_names(program: A.Program, marks: set[int]) -> list[A.Name]:
    """文档里所有「读取使用」的名字节点。"""
    out: list[A.Name] = []

    def walk(node: Any) -> None:
        if isinstance(node, A.Name) and id(node) not in marks:
            out.append(node)
        for child in child_nodes(node):
            walk(child)

    walk(program)
    return out


def attr_of(node: Any) -> Optional[A.Name]:
    """`甲.乙` 的「甲」是 Name 就返回它（否则 None）。"""
    if isinstance(node, A.Attr):
        obj = getattr(node, "obj", None)
        if isinstance(obj, A.Name):
            return obj
    return None


def attr_base_marks(program: A.Program) -> set[int]:
    """标出「属性基址」的 Name 节点（`基础库.平方` 里的 `基础库`）。

    这类名字可能来自包依赖注入（见 `包.json` 的依赖项）或 Python 桥接，
    文件内无法静态得知，因此不做「未定义」判断，避免误报。
    """
    marks: set[int] = set()

    def walk(node: Any) -> None:
        base = attr_of(node)
        if base is not None:
            marks.add(id(base))
        for child in child_nodes(node):
            walk(child)

    walk(program)
    return marks


def is_bound_target(node: Any) -> bool:
    """`node` 是不是「赋值目标」（`Assign` / `For` / 推导式的 target）。"""
    return isinstance(node, (A.Assign, A.For, A.Comprehension))
