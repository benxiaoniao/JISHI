# -*- coding: utf-8 -*-
"""源码静态检查（M44 目标三）—— 不运行程序，只看源码能看出什么问题。

四个检查：

| 问题码 | 名字 | 说明 |
|---|---|---|
| `name.undefined` | 未定义名 | 与编辑器实时诊断**同一条规则**（共用 `scope.py`，不另写一份） |
| `name.shadow` | 遮蔽内建 | 模块级的绑定撞上**内建函数 / 异常类型** |
| `name.unused` | 未使用变量 | 函数里绑定过、全文件从未读取 |
| `compare.self` | 可疑相等 | `甲 == 甲`、`甲 是 甲` 这类恒真（或恒假）比较 |

## 为什么一切都取「保守口径」

静态检查一旦误报，用户就会学会忽略它——那还不如不检查。所以每个检查都刻意
留出余量，宁可漏报：

- **未定义名**：任一作用域绑定过就不报；作为属性基址出现（`基础库.平方`）也不报
  （包依赖是运行时注入命名空间，静态看不到）；`$` 开头的内部保留名跳过。
  唯一实现是 LSP 那条 `_undefined_warnings`，本模块调的是同一份 `scope.py`。
- **未使用变量**：只查**函数体内**的绑定。模块级不查——它很可能是给别的文件
  用的（导出语义静态看不出来）。而且只要这个名字在文件里被**读过任何一次**就不报。
  判断「读过」时**只排除绑定位置、不排除属性基址**：`表.追加(1)` 里的 `表`
  是属性基址但显然是使用（M44 实现时踩到：照抄 LSP 的口径会把这种读漏掉，
  于是满屏误报「未使用」）。
- **遮蔽内建**：只认**内建函数与异常类型**，只报**模块级**绑定，且跳过导入名 /
  形参 / 循环变量 / 推导式变量 / 捕获名。第一版把标准库模块名也算进来、也不区分
  作用域，在仓库自己的语料上误报 33 条（`导入 随机`、形参叫「路径」全中招），
  逐条判定后全部判为误报，于是收窄成现在这样。名字对照的是语言规格
  （`ai.build_lang_spec()`，不另抄一份）。
- **可疑相等**：只报**同一个名字**在两边，别的比较一律不管。

## 已知边界（不粉饰）

- **单文件分析**：不解析 `导入` 进来的其他文件。所以「A 文件定义、B 文件使用」
  的情况在 B 里会被报成未定义名——这与编辑器实时诊断的口径一致（它也只分析
  当前文档）。
- **不做控制流分析**：`如果 假: 令 甲 = 1` 里的 `甲` 一样算「绑定过」。
  静态检查不做可达性判断，报「未使用」也只看「有没有被读过」。
- **不检查未使用的形参**：回调、接口实现里不用的形参很常见，报了纯属噪音。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from . import ast_nodes as A
from . import scope
from .errors import JishiError
from .langdata import LangData
from .parser import parse
from .tokenizer import tokenize

__all__ = [
    "Issue", "LEVEL_ERROR", "LEVEL_WARNING", "CHECK_CATALOG",
    "check_program", "check_source", "check_file", "check_paths",
    "iter_jsh_files", "parse_source",
]

#: 问题级别（与 LSP 诊断的两级一致）
LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

#: 与编辑器一致的「值关键字」——任何文件都可能用，不算未定义
LITERAL_KEYWORDS = ("真", "假", "空")

#: 检查项目录（机器可读）。`ai.build_lang_spec()["check_codes"]` 直接投影这里，
#: **不另抄一份**——加了新检查项却忘了登记时，`tests/test_m44_ai_contract.py` 会红。
CHECK_CATALOG: tuple[dict, ...] = (
    {"code": "name.undefined", "level": LEVEL_WARNING, "name": "未定义名",
     "doc": "名字在这份文件里没有定义过（单文件分析，与编辑器实时诊断同一口径）"},
    {"code": "name.shadow", "level": LEVEL_WARNING, "name": "遮蔽内建",
     "doc": "模块级的绑定撞上内建函数或异常类型"},
    {"code": "name.unused", "level": LEVEL_WARNING, "name": "未使用变量",
     "doc": "函数体内绑定过、全文件从未读取"},
    {"code": "compare.self", "level": LEVEL_WARNING, "name": "可疑相等",
     "doc": "同一个名字出现在比较两边，结果恒定"},
    {"code": "check.read", "level": LEVEL_ERROR, "name": "读不了文件",
     "doc": "检查时文件读不出来（路径或权限问题）"},
)

#: 恒真/恒假的比较运算符：同一名字在两边时一定是写错了
_SELF_COMPARE_OPS = ("==", "!=", "是", "不是")

#: 「遮蔽内建」只对照这两类名字。**标准库模块名故意不在此列**——
#: `导入 随机` 就是在导入那个模块（正常用法），形参叫「路径」「日志」也太自然了。
#: 第一版把标准库模块也算进来，在仓库自己的语料上误报 33 条，全部判为误报。
_RESERVED_KINDS = ("内建函数", "异常类型")

#: 「遮蔽内建」不看这些绑定来源
#: - 导入名：`导入 随机` / `导入 文本 为 文本库` 都是设计好的模块导入方式；
#: - 形参 / 循环变量 / 推导式变量 / 捕获名：作用域极小，且中文里撞名太正常
#:   （`函数 读文件(路径)`）。
_SHADOW_SKIP_KINDS = ("导入名", "形参", "循环变量", "推导式变量", "捕获名")


def _是内部名(name: str) -> bool:
    """编译器/解析器发射的内部名，用户写不出来，也不该报给用户。

    两类都在这里挡掉：
    - `$` 开头：f-string 脱糖的目标（`$文本`，M40）；
    - `__` 开头：解包与 `用 … 为` 的临时变量（`__遍历项_57_5__`、`__用_1__`）。
      判据与 `debugger.py` 的显示过滤保持一致（`name.startswith("__")`）。

    第一版只挡了 `$`，于是在语料上报了 5 条「`__遍历项_57_5__` 从未被读取」
    ——全是我自己生成的隐藏变量。
    """
    return name.startswith("__") or name.startswith("$")


def _定义体内的节点(program: A.Program) -> set[int]:
    """所有位于某个函数/类**体内部**的节点 id（顶层语句本身不算）。

    `遮蔽内建` 用它把「模块级变量」和「函数里的局部变量」分开：模块级的变量会
    整份文件挡住内建，值得报；函数里的临时变量（`令 类型 = "A"`）只在这个函数里
    有效，报了纯属噪音。
    """
    ids: set[int] = set()

    def descend(node: Any, inside: bool) -> None:
        for child in scope.child_nodes(node):
            if inside:
                ids.add(id(child))
            nested = inside or isinstance(child, (A.FuncDef, A.ClassDef, A.Lambda))
            descend(child, nested)

    descend(program, False)
    return ids


@dataclasses.dataclass
class Issue:
    """一条检查结果。字段刻意与 `--json-errors` 的输出对齐，便于脚本统一消费。"""

    code: str                 # 问题码，如 name.undefined
    level: str                # error / warning
    line: int
    col: int
    message: str
    hint: str = ""
    name: str = ""            # 涉及的名字（没有就是空串）

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "line": self.line,
            "col": self.col,
            "message": self.message,
            "hint": self.hint,
            "name": self.name,
        }

    def format(self, path: str = "") -> str:
        """人类可读的一行（外加缩进的提示行）。"""
        loc = f"{path}:{self.line}:{self.col}" if path else f"{self.line}:{self.col}"
        out = f"{loc}: [{self.level}] {self.code}：{self.message}"
        if self.hint:
            out += f"\n    提示：{self.hint}"
        return out


# ---------------------------------------------------------------------------
# 解析入口（错误按「问题」返回，不往外抛——检查器不该因为一个语法错就崩）
# ---------------------------------------------------------------------------

def parse_source(text: str, filename: str = "<检查>") -> A.Program:
    """把源码文本解析成 AST（词法 + 语法）。"""
    return parse(tokenize(text, filename), text.split("\n"), filename)


# ---------------------------------------------------------------------------
# 四类检查
# ---------------------------------------------------------------------------

def _分块节点(body: Iterable[Any]) -> Iterator[Any]:
    """遍历一段语句块里的节点，但**不进入嵌套的函数/类**。

    嵌套定义的局部是它自己的事，算在外层函数头上会张冠李戴。
    """
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (A.FuncDef, A.ClassDef, A.Lambda)):
            continue
        if not isinstance(node, A.Node):
            continue
        yield node
        stack.extend(scope.child_nodes(node))


def _绑定(node: Any) -> Iterator[A.Name]:
    """一个「绑定语句」绑定的名字节点（赋值 / 遍历 / 推导式的目标）。"""
    for n in scope.flatten(getattr(node, "target", None)):
        if isinstance(n, A.Name):
            yield n


def _未定义名(program: A.Program, lang: LangData) -> list[Issue]:
    """名字没定义过（与 LSP 实时诊断同一口径）。"""
    known = set(lang.builtins) | set(lang.keywords) | set(lang.exceptions)
    known.update(LITERAL_KEYWORDS)
    known.update(lang.stdlib)
    known.update(scope.defined_names(program))
    # 绑定位置不算「使用」；属性基址**也不参与**判断（可能来自包依赖注入）
    marks = scope.binding_marks(program) | scope.attr_base_marks(program)

    out: list[Issue] = []
    seen: set[tuple[str, int]] = set()
    for node in scope.load_names(program, marks):
        if node.id in known or _是内部名(node.id):
            continue
        key = (node.id, node.line)
        if key in seen:
            continue
        seen.add(key)
        out.append(Issue(
            code="name.undefined", level=LEVEL_WARNING,
            line=node.line, col=node.col,
            message=f"名字「{node.id}」在这份文件里没有定义过",
            hint="要么是拼错了，要么它来自别的文件（本命令只做单文件分析）；"
                 "若来自包依赖，那是运行时注入的，静态看不到。",
            name=node.id,
        ))
    return out


def _所有绑定(program: A.Program) -> list[tuple[str, Any, str]]:
    """收集所有「绑定」：(名字, 位置节点, 种类)。"""
    out: list[tuple[str, Any, str]] = []

    def add(name: Optional[str], node: Any, kind: str) -> None:
        if name:
            out.append((name, node, kind))

    for node in scope.walk_nodes(program):
        if isinstance(node, A.Assign):
            for n in _绑定(node):
                add(n.id, n, "变量")
        elif isinstance(node, A.For):
            for n in _绑定(node):
                add(n.id, n, "循环变量")
        elif isinstance(node, A.Comprehension):
            for n in _绑定(node):
                add(n.id, n, "推导式变量")
        elif isinstance(node, A.FuncDef):
            add(node.name, node, "函数名")
            for p in node.params:
                add(p, node, "形参")
        elif isinstance(node, A.ClassDef):
            add(node.name, node, "类名")
        elif isinstance(node, A.Import):
            add(node.alias or node.name, node, "导入名")
        elif isinstance(node, A.ExceptHandler):
            add(node.name, node, "捕获名")
        elif isinstance(node, A.Lambda):
            for p in node.params:
                add(p, node, "形参")
    return out


def _遮蔽内建(program: A.Program, lang: LangData) -> list[Issue]:
    """绑定的名字撞上**内建函数或异常类型**。

    刻意不开的三扇门（开了就是误报，见 `_RESERVED_KINDS` / `_SHADOW_SKIP_KINDS`
    的注释）：标准库模块名、导入名、形参、循环变量、推导式变量、捕获名；
    变量还只在**模块级**报（模块级的变量会整份文件挡住内建，函数里的不会）。

    同一名字只报第一次（重复赋值同一个被遮蔽的名字，报一次就够了）。
    """
    reserved: dict[str, str] = {}
    for name in lang.builtins:
        reserved[name] = "内建函数"
    for name in lang.exceptions:
        reserved.setdefault(name, "异常类型")

    inside = _定义体内的节点(program)
    out: list[Issue] = []
    seen: set[str] = set()
    for name, node, kind in _所有绑定(program):
        if name not in reserved or name in seen or _是内部名(name):
            continue
        if reserved[name] not in _RESERVED_KINDS or kind in _SHADOW_SKIP_KINDS:
            continue
        if kind == "变量" and id(node) in inside:
            continue            # 函数里的局部变量：只影响那个函数，不报
        seen.add(name)
        line, col = A.loc_from(node)
        out.append(Issue(
            code="name.shadow", level=LEVEL_WARNING,
            line=line, col=col,
            message=f"{kind}「{name}」遮蔽了{reserved[name]}「{name}」",
            hint=f"这个作用域里原来的「{name}」就被挡住了——换个名字（如"
                 f"「我的{name}」）能避免后面的人看错。",
            name=name,
        ))
    return out


def _未使用变量(program: A.Program) -> list[Issue]:
    """函数体内绑定过、但全文件从未读取的名字。

    「读取」的口径**只排除绑定位置**（不排除属性基址）：`表.追加(1)` 算使用。
    """
    used = {n.id for n in scope.load_names(program, scope.binding_marks(program))}

    out: list[Issue] = []
    for fn in [n for n in scope.walk_nodes(program) if isinstance(n, A.FuncDef)]:
        seen: set[str] = set()
        for node in _分块节点(fn.body):
            if not isinstance(node, (A.Assign, A.For)):
                continue
            for n in _绑定(node):
                if n.id in seen or n.id in used or _是内部名(n.id):
                    continue
                seen.add(n.id)
                hint = ("只是想把循环跑固定次数的话，用「循环 N 次」可以不引入变量。"
                        if isinstance(node, A.For)
                        else "删掉这个绑定，或确认它是不是本该赋给别的名字。")
                out.append(Issue(
                    code="name.unused", level=LEVEL_WARNING,
                    line=n.line, col=n.col,
                    message=f"变量「{n.id}」在函数「{fn.name}」里绑定后从未被读取",
                    hint=hint, name=n.id,
                ))
    return out


def _可疑相等(program: A.Program) -> list[Issue]:
    """`甲 == 甲`（`!=` / `是` / `不是` 同理）：同一个名字在两边，结果恒定。"""
    out: list[Issue] = []
    for node in scope.walk_nodes(program):
        if not isinstance(node, A.Compare):
            continue
        prev = node.left
        for op, right in zip(node.ops, node.comparators):
            if (isinstance(prev, A.Name) and isinstance(right, A.Name)
                    and prev.id == right.id and op in _SELF_COMPARE_OPS):
                out.append(Issue(
                    code="compare.self", level=LEVEL_WARNING,
                    line=prev.line, col=prev.col,
                    message=f"「{prev.id} {op} {prev.id}」两边是同一个名字，"
                            f"这个比较的结果是恒定的",
                    hint="是不是该拿另外两个值来比？",
                    name=prev.id,
                ))
            prev = right
    return out


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def check_program(program: A.Program,
                  lang: Optional[LangData] = None) -> list[Issue]:
    """对一棵已解析的 AST 跑全部检查，按位置排序返回。"""
    lang = lang or LangData()
    issues: list[Issue] = []
    issues += _未定义名(program, lang)
    issues += _遮蔽内建(program, lang)
    issues += _未使用变量(program)
    issues += _可疑相等(program)
    issues.sort(key=lambda i: (i.line, i.col, i.code))
    return issues


def check_source(text: str, filename: str = "<检查>",
                 lang: Optional[LangData] = None) -> list[Issue]:
    """检查一段源码文本。**语法错也当一条问题返回**（不往外抛）。"""
    try:
        program = parse_source(text, filename)
    except JishiError as e:
        message = e.title if not e.message else f"{e.title}：{e.message}"
        return [Issue(
            code=e.code, level=LEVEL_ERROR,
            line=e.line or 1, col=e.col or 1,
            message=message, hint=e.hint or "",
        )]
    return check_program(program, lang)


def check_file(path: str | Path,
               lang: Optional[LangData] = None) -> list[Issue]:
    """检查一个 `.jsh` 文件。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return [Issue(code="check.read", level=LEVEL_ERROR, line=1, col=1,
                      message=f"读不了文件：{e}", hint="")]
    return check_source(text, str(p), lang)


#: 目录扫描时跳过的目录名（与工作区符号索引同一口径）
SKIP_DIRS = (".jishi", ".git", ".venv", "build-env", "node_modules", "__pycache__")


def iter_jsh_files(root: str | Path) -> Iterator[Path]:
    """按路径收集待检查的 `.jsh` 文件（是文件就返回它，是目录就递归找）。"""
    p = Path(root)
    if p.is_file():
        yield p
        return
    if not p.is_dir():
        return
    for child in sorted(p.rglob("*.jsh")):
        parts = set(child.parts)
        if parts & set(SKIP_DIRS):
            continue
        if any(part.startswith(".") and part != "." for part in child.parts):
            continue
        yield child


def check_paths(paths: Iterable[str | Path],
                lang: Optional[LangData] = None) -> list[tuple[Path, Issue]]:
    """批量检查（文件或目录），返回 [(文件, 问题)]，按文件、位置排序。"""
    lang = lang or LangData()
    out: list[tuple[Path, Issue]] = []
    seen: set[Path] = set()
    for raw in paths:
        for path in iter_jsh_files(raw):
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            for issue in check_file(path, lang):
                out.append((path, issue))
    out.sort(key=lambda pair: (str(pair[0]), pair[1].line, pair[1].col, pair[1].code))
    return out
