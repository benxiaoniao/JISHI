# -*- coding: utf-8 -*-
"""本地包的**编译期展开**（M145）—— 让 `导入 图片格式转换 从 本地包` 在五个引擎上都能跑。

## 为什么不是「运行时加载」

M145 一开始是照 Python 参考实现那样**运行时**加载的（读 `包.json`、跑包里的
`.jsh`、把顶层名字收成模块对象）。在 Rust 宿主上它**当场崩了**：

    thread 'main' panicked at: index out of bounds: the len is 1 but the index is 19

原因：字节码里 `Val::Func` 只记「代码在**本模块**代码表的第几段」，而包的代码
在另一个模块里 —— 一调用就按主程序的代码表去取，越界。这正是
`jishilib.py` 模块文档里早就写着的那句话：

    运行时加载库字节码…以及「跨 VM 调用」这个大坑（字节码的全局表是每个
    VM 一份，函数跨 VM 调用会拿到错误的常量表 —— 本层第一版就是这么撞上的）

而且 `Node` 与 C VM **连编译器都没有**：它们只会跑字节码，读不了 `.jsh`
源码。所以「运行时加载」这条路对它们根本不通。

⇒ 走**同一条已经验证过的路**：编译期把包展开成一个函数（与基石库层一模一样），
于是包里的函数就是**普通函数**，五个引擎跑的是同一份东西，跨 VM 不存在了。

## 展开成什么

```jishi
导入 图片格式转换 从 本地包
图片格式转换.空图(64, 64)
```

编译期变成：

```jishi
函数 __本地包_图片格式转换__():
    <图片格式转换.jsh 的源码，整体缩进一级>
    返回 {"空图": 空图, "尺寸": 尺寸, …}

图片格式转换 = $造模块("图片格式转换", __本地包_图片格式转换__())
图片格式转换.空图(64, 64)
```

`$造模块` 把导出字典包成**模块对象** —— 所以 `打印(图片格式转换)` 仍然是 `<模块 图片格式转换>`，
不是一坨字典（这点比基石库层早年的做法更好，见 `jishilib.py` 的「已知代价」）。

依赖（`包.json` 的「依赖」）也展开：先在程序顶部放依赖的外壳函数，再在包函数
体里注入一行 `基础库 = $造模块("基础库", __本地包_基础库__())` —— 于是包里
可以直接写 `基础库.某某(...)`，与 Python 参考实现的「依赖注入」同义。

## 三条取舍（写下来，免得以后反复讨论）

1. **「运行时才拼出来的」导入不行**（与基石库层同一限制）：展开发生在编译期。
   不过语言现在**没有动态执行那种内建**，所以这条代价目前为零 ——
   宿主的 `IMPORT` 分支留着兜底，真走到那儿会给一句可操作的错。
2. **一个包有多份 `.jsh` 且没声明「入口」时，展开后它们共享一个作用域**
   （源码按文件名排序拼在一个函数里）。Python 的运行时加载是「每个文件
   一个独立环境」，这里更简单也更可预测 —— 但确实是差别，写在文档里。
   📌 规范写法是**声明「入口」**（包只对外一个模块），那样就没有这个问题。
3. **改了包要重编译**（与基石库层一样）：包是编进程序的，不是运行时读的。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Optional

from . import ast_nodes as A
from .errors import SemanticFuncError

#: 外壳函数名前缀（与基石库层区分开，方便看报错时一眼认出是哪一层）。
_SHELL_PREFIX = "__本地包_"


def shell_name(name: str) -> str:
    """包名 → 外壳函数名（`图片格式转换` → `__本地包_图片格式转换__`）。"""
    return f"{_SHELL_PREFIX}{name}__"


# ---------------------------------------------------------------------------
# 找包（与 runtime._package_dirs 同一顺序：先项目后全局）
# ---------------------------------------------------------------------------

#: 追加包根的**环境变量名**（`os.pathsep` 分隔）。
#:
#: 给 `jishi 测试 <包目录>` 用的（M147）：包自带的测试里写着
#: `导入 自己 从 本地包`，而**跑包的测试**这件事本身不该要求「先把包装一遍」。
#: 于是跑测试时把这个包的**父目录**塞进来，它就能当作包根被找到。
#:
#: 🔴 **为什么用环境变量、而不是内存里的全局变量**：这个「额外包根」要被
#: **四处**同时看到 —— Python 编译期展开（本文件）、Python 运行时导入
#: （`runtime._package_dirs`）、Rust 宿主（`packages::package_dirs`）、
#: Rust 前端 / pyo3（`frontend/src/localpkg.rs`）。四处各塞一个全局变量既难对，
#: 也漏 pyo3 那条（Python 侧设不到 pyd 里）。环境变量一次设好，四边都读得到。
PKG_ROOTS_ENV = "JISHI_PKG_ROOTS"


def _环境包根() -> list:
    raw = (os.environ.get(PKG_ROOTS_ENV) or "").strip()
    if not raw:
        return []
    return [d for d in raw.split(os.pathsep) if d]


def package_dirs() -> list:
    """包搜索目录：**额外根（`JISHI_PKG_ROOTS`）→ 项目 `.jishi/packages` → 用户目录**。"""
    dirs = []
    for d in (_环境包根()
              + [os.path.join(os.getcwd(), ".jishi", "packages"),
                 os.path.join(os.path.expanduser("~"), ".jishi", "packages")]):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def find_pkg_dir(name: str) -> Optional[str]:
    for base in package_dirs():
        cand = os.path.join(base, name)
        if os.path.isdir(cand):
            return cand
    return None


def _fail(msg: str, node, filename: str, hint: Optional[str] = None):
    """包相关的编译期报错：**类型码与运行时那条一致**（E0302）。"""
    raise SemanticFuncError(
        msg, line=getattr(node, "line", None), col=getattr(node, "col", None),
        filename=filename, hint=hint)


def _read_meta(pkg_dir: str, name: str, node, filename: str) -> dict:
    """读 `包.json`。**容忍不存在**（当空表），但「存在却读不懂」要报出来。

    ⚠️ 这里**不复用 `packages.read_package_meta`**：那个会校验字段白名单，
    比参考实现的运行时加载**更严** —— 于是会出现「Python 能跑、宿主报错」。
    包管理的校验仍在安装时做，这里只管「怎么把它编进来」。
    """
    path = os.path.join(pkg_dir, "包.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _fail(f"本地包「{name}」的 包.json 读不了：{e}", node, filename)
    if not isinstance(meta, dict):
        _fail(f"本地包「{name}」的 包.json 读不了：顶层要是 JSON 对象", node, filename)
    声明 = str(meta.get("名字") or "").strip()
    if 声明 and 声明 != name:
        _fail(f"包目录「{name}」里的 包.json 声明名字为「{声明}」，不一致", node, filename)
    return meta


def module_files(pkg_dir: str, name: str, meta: dict, node, filename: str) -> list:
    """要编进来的 `.jsh`：有「入口」只编入口，否则按文件名排序全编。"""
    files = sorted(f for f in os.listdir(pkg_dir) if f.endswith(".jsh"))
    entry = str(meta.get("入口") or "").strip()
    if entry:
        if not entry.endswith(".jsh"):
            entry += ".jsh"
        if entry not in files:
            _fail(f"本地包「{name}」的 包.json 声明入口「{meta.get('入口')}」，"
                  f"但包里没有这个模块文件", node, filename)
        return [entry]
    if not files:
        _fail(f"本地包「{name}」里没有 .jsh 模块文件", node, filename)
    return files


# ---------------------------------------------------------------------------
# 包 → 外壳函数
# ---------------------------------------------------------------------------

def _shift(node, delta: int) -> None:
    from .jishilib import _shift_lines

    _shift_lines(node, delta)


def shell_def(name: str, node, filename: str, stack: Optional[list] = None) -> list:
    """生成 `[依赖的外壳函数…, 本包的外壳函数]`（依赖在前，可直接插进程序顶部）。

    `node` 是**那条 `导入` 语句** —— 报错位置用它（行列要落在用户写的那一行）。
    """
    stack = list(stack or [])
    if name in stack:
        chain = " → ".join(stack + [name])
        _fail(f"检测到循环依赖：{chain}", node, filename)
    stack.append(name)

    pkg_dir = find_pkg_dir(name)
    if pkg_dir is None:
        _fail(f"没有找到本地包「{name}」", node, filename,
              hint=f"请把包放到 .jishi/packages/{name}/ 目录（含 包.json 与 .jsh 模块）")

    meta = _read_meta(pkg_dir, name, node, filename)

    # ---- 依赖：先各自的壳，再在本包体里注入 ----
    from .packages import version_satisfies

    defs: list = []
    依赖名: list = []
    for dep_name, constraint in (meta.get("依赖") or {}).items():
        defs += shell_def(dep_name, node, filename, stack)
        dep_dir = find_pkg_dir(dep_name)
        dep_meta = _read_meta(dep_dir, dep_name, node, filename) if dep_dir else {}
        dep_version = str(dep_meta.get("版本") or "") or "0.0.0"
        if not version_satisfies(dep_version, str(constraint)):
            _fail(f"包「{name}」依赖「{dep_name}」的版本要满足 {constraint!r}，"
                  f"但装的是 {dep_version}", node, filename,
                  hint=(f"装个符合要求的版本重新试试；如果多个包对「{dep_name}」"
                        f"的要求互相打架（钻石依赖），跑 `jishi 检查` 能看到完整链条"))
        依赖名.append(dep_name)

    # ---- 源码拼起来（多文件共享一个作用域，见模块文档第 2 条取舍）----
    片段 = []
    for fn in module_files(pkg_dir, name, meta, node, filename):
        path = os.path.join(pkg_dir, fn)
        try:
            with open(path, encoding="utf-8") as f:
                片段.append((path, f.read()))
        except OSError as e:
            _fail(f"本地包「{name}」的模块「{fn}」读不了：{e}", node, filename)

    from .parser import parse_source
    from .jishilib import _export_names

    源码 = "".join(s.replace("\r\n", "\n").replace("\r", "\n") + "\n"
                   for _p, s in 片段)
    导出 = _export_names(parse_source(源码, 片段[0][0]))
    正文 = [("    " + line) if line.strip() else ""
            for line in 源码.split("\n")]
    pairs = ", ".join(f'"{out}": {src}' for out, src in 导出)
    壳源码 = (f"函数 {shell_name(name)}()：\n"
              + "\n".join(正文)
              + f"\n    返回 {{{pairs}}}\n")

    程序 = parse_source(壳源码, 片段[0][0])
    for 节点 in 程序.body:
        if isinstance(节点, A.FuncDef) and 节点.name == shell_name(name):
            # 行号整体减掉「函数头 1 行」
            _shift(节点, -1)
            # 依赖注入**用 AST 节点插**，不写进源码：`$造模块` 在宿主的词法器里
            # 写不出来（`$` 不是合法字符），写进源码只有 Python 能解析。
            节点.body = [_dep_assign(d, 节点.line) for d in 依赖名] + 节点.body
            defs.append(节点)
            return defs
    raise AssertionError(f"外壳函数没生成出来：{name}")     # pragma: no cover


# ---------------------------------------------------------------------------
# AST 改写：`导入 图片格式转换 从 本地包` → `图片格式转换 = $造模块("图片格式转换", __本地包_图片格式转换__())`
# ---------------------------------------------------------------------------

def _dep_assign(dep_name: str, line: int):
    """`依赖名 = $造模块("依赖名", __本地包_依赖名__())` —— 一个赋值节点。

    ⚠️ 一定要造**节点**而不是拼源码：`$造模块` 是内部名字（`$` 不是合法字符），
    Python 词法器恰好放行，宿主的词法器不放行 —— 拼源码就只有一边能跑。
    """
    def call(fn_id, args):
        return A.Call(line=line, col=1,
                      func=A.Name(line=line, col=1, id=fn_id),
                      args=args, keywords=[])

    return A.Assign(
        line=line, col=1,
        target=A.Name(line=line, col=1, id=dep_name),
        op="=",
        value=call("$造模块", [
            A.Str(line=line, col=1, value=dep_name),
            call(shell_name(dep_name), []),
        ]),
    )


def _is_local_import(node) -> bool:
    return isinstance(node, A.Import) and bool(getattr(node, "from_local", False))


# ---------------------------------------------------------------- 扫描与改写
#
# 🔴 **这里与 `jishilib.py` 有意不同：展开下探到「所有块体」。**
#    基石库层那套 `_collect` / `_rewrite` 只认「列表里的节点」，`如果 的 branches`
#    （列表套元组）里的块体**不进去** —— 于是 `如果 真：导入 统计` 不会被展开，
#    运行时回落到宿主层（那边有原生实现，所以看不出来）。
#    但本地包**没有宿主层兜底**：Python 会回落到运行时加载（能跑），
#    宿主只能报错（跑不了）—— 同一段代码两个结果，正是最该避免的那种分叉。
#    `localpkg` 是新写的一层、没有历史字节码要保，所以**下探到底**：
#    函数体、分支体、循环体、`尝试/捕获` 体里的导入都会被展开。


def _collect(program) -> list:
    """先探一遍有哪些本地包导入（没用到就省掉深拷贝）。"""
    found: list = []

    def walk(v):
        if isinstance(v, A.Node):
            if _is_local_import(v):
                if v.name not in found:
                    found.append(v.name)
                return
            for f in dataclasses.fields(v):
                walk(getattr(v, f.name))
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    import dataclasses

    for node in program.body:
        walk(node)
    return found


def _rewrite(node, fn):
    """深度优先重写：**所有字段**都下探（含 `branches` 的块体、元组里的节点）。"""
    import dataclasses

    for f in dataclasses.fields(node):
        setattr(node, f.name, _map(getattr(node, f.name), fn))
    return fn(node)


def _map(v, fn):
    if isinstance(v, A.Node):
        return _rewrite(v, fn)
    if isinstance(v, list):
        return [_map(x, fn) for x in v]
    if isinstance(v, tuple):
        return tuple(_map(x, fn) for x in v)
    return v


def expand(program, filename: str = "<输入>"):
    """把所有 `导入 … 从 本地包` 就地改写成「调外壳函数」。

    返回**新的** `Program`（先深拷贝再改，不动调用方给的 AST）。
    没有本地包导入时原样返回 —— 开销就是一次浅扫。
    """
    if not _collect(program):
        return program

    新程序 = copy.deepcopy(program)
    壳们: dict = {}

    def visit(node):
        if not _is_local_import(node):
            return node
        name = node.name
        if name not in 壳们:
            壳们[name] = shell_def(name, node, filename)
        binding = node.alias or name
        调用 = A.Call(
            line=node.line, col=node.col,
            func=A.Name(line=node.line, col=node.col, id=shell_name(name)),
            args=[], keywords=[],
        )
        return A.Assign(
            line=node.line, col=node.col,
            target=A.Name(line=node.line, col=node.col, id=binding),
            op="=",
            value=A.Call(
                line=node.line, col=node.col,
                func=A.Name(line=node.line, col=node.col, id="$造模块"),
                args=[A.Str(line=node.line, col=node.col, value=name), 调用],
                keywords=[],
            ),
        )

    新程序.body = [_rewrite(node, visit) for node in 新程序.body]

    # 壳函数插在最前面（依赖的在各自的前面，`shell_def` 已排好序）
    头部: list = []
    已放: set = set()
    for name in 壳们:
        for d in 壳们[name]:
            if d.name not in 已放:
                已放.add(d.name)
                头部.append(d)
    新程序.body = 头部 + 新程序.body
    return 新程序
