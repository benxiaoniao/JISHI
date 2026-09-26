# -*- coding: utf-8 -*-
"""基石库层（M52）：**用基石自己写的标准库**，一份代码五个引擎共用。

## 它解决什么

标准库原来是「同一套算法用三种语言各写一遍」（Python / JS / Rust）。
`统计` 这类纯逻辑模块，改一处要改三处，还容易悄悄漂移 —— M51 的漂移检测
就是为了抓这个（当时 Node 少了 12 个函数、Rust 少了 6 个，**都是静默的**）。

从 M52 起，纯逻辑模块写进 `jishi/stdlib-jishi/*.jsh`（**就是基石源码**），
经本模块在**编译期展开**进主程序，于是五个引擎执行的是**同一份字节码**——
**结构上不可能不一致**，比「靠对拍发现漂移」强一个量级。

## 它怎么工作（关键设计）

    jishi/stdlib-jishi/统计.jsh          ← 用基石写的库，只写一遍
                │ 编译期
                ▼
    函数 __基石库_统计__():               ← 把整个模块包成**一个函数**
        <模块源码整体缩进一级>
        返回 {"求和": 求和, "平均": 平均, …}   ← 顶层名字打包成字典

主程序里的 `导入 统计` 被**改写**成：

    统计 = __基石库_统计__()

于是「库」在字节码层面**就是一次普通的函数调用**——三个执行器与两个跨语言
宿主**一个字都不用改**，它们只是在跑同一份字节码。相比「运行时加载库字节码」，
这省掉了：新指令、新的值类型、payload 新字段、两宿主的加载逻辑，以及
「跨 VM 调用」这个大坑（字节码的全局表是每个 VM 一份，函数跨 VM 调用会拿到
错误的常量表——本层第一版就是这么撞上的）。

### 为什么用「函数」而不是「模块对象」

字节码里没有「模块级作用域」：`LOAD_GLOBAL` 查的是**当前 VM 的全局表**，
而全局表是整个 VM 一份。若在运行时执行模块主代码，模块里的函数定义会落进
主程序的全局表 —— 既污染命名空间，模块内互相调用也分不清彼此。
**包成函数之后**，模块的顶层名字全变成那个函数的**局部变量**，
模块内互相引用走**闭包**（cellvars/freevars），天然隔离。

### 已知代价（如实记）

- 模块在运行时是**字典**，不是「模块对象」：`打印(统计)` 显示字典内容，
  而不是 `<模块 统计>`（写进 `docs/设计决策.md`）。
- 主程序要**整体重编译**才会看到库的改动（库的编译产物本身有缓存）。
- 模块内若在顶层写有副作用的语句，展开后仍在**原来的位置**执行
  （`导入` 被就地替换，不提前也不推后）。

## 回落链

`导入 X` → ① **基石库层**（`jishi/stdlib-jishi/X.jsh`，编译期展开）
         → ② **宿主层**（`jishi/stdlib/X.py` 与两个宿主各自的标准库）

所以迁移是**渐进**的：搬一个模块就少一份重复，宿主层随时兜底；
而且**序列化格式没变**，存量字节码产物完全不受影响。

## 三条约定（写这个目录的模块要守）

① 引别的模块写成 `导入 X 为 _X`。导出时**排除下划线开头的名字**，
   否则内部依赖会漏到 `统计.数学` 这种地方。
② 报错**类型**要与 Python 侧一致（`类型错误` / `值错误`）—— 对拍测试比类型；
   文案允许略有出入。
③ **热路径别放这里**：走字节码比宿主原语慢（M52 实测见 `docs/设计决策.md`）。
"""

from __future__ import annotations

import copy
import os
import sys
from typing import Optional

from . import ast_nodes as A

#: 外壳函数名的前缀。用双下划线 + 中文前缀，撞车概率极低；
#: 它只在编译期出现，用户写不出来（内部名字）。
_SHELL_PREFIX = "__基石库_"


def lib_dir() -> str:
    """基石库目录（`jishi/stdlib-jishi/`）。

    与 `ai._stdlib_dir` 同一套路：源码树里就是包内的那个目录；
    PyInstaller 单文件模式会解压到 `sys._MEIPASS`。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = os.path.join(meipass, "jishi", "stdlib-jishi")
        if os.path.isdir(cand):
            return cand
    return os.path.join(os.path.dirname(__file__), "stdlib-jishi")


def module_path(name: str) -> Optional[str]:
    """这个模块有没有基石库实现？有就返回它的 `.jsh` 路径。"""
    path = os.path.join(lib_dir(), f"{name}.jsh")
    return path if os.path.isfile(path) else None


def has(name: str) -> bool:
    """`name` 是不是「用基石写的」标准库模块。

    环境变量 `JISHI_NO_JISHILIB=1` 可以**整体关掉这一层**，让导入全部回落到
    宿主层。两个用处：① 性能对比要在同一进程里交替跑两版（见 M52 的基准）；
    ② 怀疑基石库层有问题时，一句话就能排除它（诊断用）。
    """
    if os.environ.get("JISHI_NO_JISHILIB") == "1":
        return False
    return module_path(name) is not None


def names() -> list:
    """基石库现有全部模块名（供文档与一致性检测用）。"""
    d = lib_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f[:-4] for f in os.listdir(d) if f.endswith(".jsh"))


def shell_name(name: str) -> str:
    """模块 → 外壳函数名（`统计` → `__基石库_统计__`）。"""
    return f"{_SHELL_PREFIX}{name}__"


# ---------------------------------------------------------------------------
# 包装：模块源码 → 外壳函数
# ---------------------------------------------------------------------------

def _parse(source: str, filename: str):
    from .parser import parse
    from .tokenizer import tokenize

    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return parse(tokenize(source, filename), lines, filename)


def _export_names(program) -> list:
    """模块顶层**对外导出**的名字：`[(导出名, 源码里的名字), …]`。

    命名规则（三条，与 Python 的 `__all__` 精神一致但更省事）：

    - `__X` → 导出为 **`X`**（显式改名）。要它的原因很实在：**有的导出名是
      关键字，写不成函数名** —— `参数.新建` 里的 `新建` 就是（`新建 类()` 的
      `新建`），只能内部叫别的名字，导出时映射回去。
    - `_X` → **私有，不导出**（模块自己的辅助函数、`导入 X 为 _X` 的内部依赖）。
    - 其余 → 原名导出。
    """
    out: list = []
    for node in program.body:
        if isinstance(node, (A.FuncDef, A.ClassDef)):
            name = node.name
        elif isinstance(node, A.Assign) and isinstance(node.target, A.Name):
            name = node.target.id
        else:
            continue
        if name.startswith("__") and len(name) > 2:
            pair = (name[2:], name)
        elif not name.startswith("_"):
            pair = (name, name)
        else:
            continue
        if pair not in out:
            out.append(pair)
    return out


def shell_source(name: str) -> str:
    """把模块源码包成外壳函数源码。

    缩进规则：**非空行整体再加一级缩进**，空行保持空（空行带空格虽然多半也能
    解析，但没必要给词法器的缩进栈添麻烦）。
    """
    path = module_path(name)
    if path is None:
        raise KeyError(name)
    with open(path, encoding="utf-8") as f:
        source = f.read()

    exports = _export_names(_parse(source, path))
    body = []
    for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        body.append(("    " + line) if line.strip() else "")
    pairs = ", ".join(f'"{out}": {src}' for out, src in exports)
    return f"函数 {shell_name(name)}()：\n" + "\n".join(body) + f"\n    返回 {{{pairs}}}\n"


def shell_def(name: str) -> A.FuncDef:
    """解析出外壳函数的 `FuncDef` 节点（每次给定一个新对象，供安全改写）。

    ⚠️ **行号要整体减 1**：外壳源码比模块源码多了一行函数头，不减的话
    `统计.jsh` 里第 2 行的错误会被报成第 3 行 —— 中文报错的行列是这项目的
    立身之本之一，不能差一行。
    """
    path = module_path(name)
    program = _parse(shell_source(name), path or f"{name}.jsh")
    for node in program.body:
        if isinstance(node, A.FuncDef) and node.name == shell_name(name):
            _shift_lines(node, -1)
            return node
    raise AssertionError(f"外壳函数没生成出来：{name}")     # pragma: no cover


def _shift_lines(node, delta: int) -> None:
    """递归把所有节点的行号加上 `delta`（最小 1）。"""
    import dataclasses

    line = getattr(node, "line", None)
    if isinstance(line, int):
        node.line = max(1, line + delta)
    for f in dataclasses.fields(node):
        v = getattr(node, f.name)
        if isinstance(v, A.Node):
            _shift_lines(v, delta)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, A.Node):
                    _shift_lines(x, delta)
                elif isinstance(x, tuple):
                    for y in x:
                        if isinstance(y, A.Node):
                            _shift_lines(y, delta)


# ---------------------------------------------------------------------------
# AST 改写：`导入 统计` → `统计 = __基石库_统计__()`
# ---------------------------------------------------------------------------

def _rewrite(node, fn):
    """深度优先重写：先重写子节点，再用 `fn` 处理本节点（`fn` 可返回新节点）。"""
    import dataclasses

    for f in dataclasses.fields(node):
        v = getattr(node, f.name)
        if isinstance(v, A.Node):
            setattr(node, f.name, _rewrite(v, fn))
        elif isinstance(v, list):
            setattr(node, f.name, [_map_item(x, fn) for x in v])
    return fn(node)


def _map_item(x, fn):
    if isinstance(x, A.Node):
        return _rewrite(x, fn)
    if isinstance(x, tuple):
        return tuple(_map_item(y, fn) for y in x)
    return x


def expand(program):
    """把主程序里**所有** `导入 <基石库模块>` 就地改写成「调外壳函数」。

    返回值是**新的** `Program`（先深拷贝再改，不动调用方给的 AST —— 编译器
    是纯函数更好推理）。没用到基石库时原样返回，开销就是一次深拷贝。

    嵌套位置（函数体内、条件分支里）也会被改写 —— 一律**就地**变成赋值语句，
    所以「什么时候导入」的语义不变。
    """
    targets = _collect(program)
    if not targets:
        return program

    new_program = copy.deepcopy(program)
    shells: dict = {}
    counter = {"n": 0}

    def visit(node):
        if (isinstance(node, A.Import) and not node.from_python
                and not node.from_local and has(node.name)):
            shell = shell_name(node.name)
            if shell not in shells:
                shells[shell] = shell_def(node.name)
            binding = node.alias or node.name
            shell_call = A.Call(
                line=node.line, col=node.col,
                func=A.Name(line=node.line, col=node.col, id=shell),
                args=[], keywords=[],
            )
            # `统计 = $造模块("统计", __基石库_统计__())`
            # 外面那层 `$造模块` 把导出字典包成**模块对象** —— 于是
            # `统计.求和(...)` 的语法一个字都不用改，`打印(统计)` 也还是
            # `<模块 统计>`。它是个内部内建（`$` 开头，用户写不出来）。
            return A.Assign(
                line=node.line, col=node.col,
                target=A.Name(line=node.line, col=node.col, id=binding),
                op="=",
                value=A.Call(
                    line=node.line, col=node.col,
                    func=A.Name(line=node.line, col=node.col, id="$造模块"),
                    args=[A.Str(line=node.line, col=node.col, value=node.name),
                          shell_call],
                    keywords=[],
                ),
            )
        return node

    # ⚠️ 顶层节点的**替换结果要接住**：`_rewrite` 只负责把子节点的替换写回去，
    # 它自己返回的（可能被换掉的）顶层节点必须由调用方收下 —— 漏了这一句，
    # `导入 统计` 会原样留着，于是运行时又走回宿主层（第一版就这么错的：
    # 外壳函数老老实实编译进去了，可代码跑的还是宿主层那份）。
    new_program.body = [_rewrite(node, visit) for node in new_program.body]

    # 外壳函数定义插在最前面，保证任何一处改写后的调用都能找到它
    shell_defs = [shells[k] for k in shells]
    new_program.body = shell_defs + new_program.body
    counter["n"] = len(shell_defs)
    return new_program


def _collect(program) -> set:
    """先探一遍有没有用到基石库模块（没用到就省掉深拷贝）。"""
    import dataclasses

    found: set = set()

    def walk(node):
        if isinstance(node, A.Import):
            if (not node.from_python and not node.from_local and has(node.name)):
                found.add(node.name)
            return
        for f in dataclasses.fields(node):
            v = getattr(node, f.name)
            if isinstance(v, A.Node):
                walk(v)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, A.Node):
                        walk(x)
                    elif isinstance(x, tuple):
                        for y in x:
                            if isinstance(y, A.Node):
                                walk(y)

    for node in program.body:
        walk(node)
    return found
