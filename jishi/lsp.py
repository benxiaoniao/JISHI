# -*- coding: utf-8 -*-
"""基石语言服务器（M21.2）—— JSON-RPC over stdio 的最小 LSP 实现。

提供四件事（对应 M21 的「补全 / 跳转 / 悬停文档 / 实时诊断」）：

1. **实时诊断**：打开或改动文件即重新词法+语法分析，把中文报错（含错误码、
   行列、修改提示）转成 LSP 诊断推给编辑器；另外附带一条保守的
   「名字没定义过」警告。
2. **补全**：关键字 / 内建函数 / 标准库模块 / 文档内符号；在 `甲.` 之后
   给标准库函数或容器方法。
3. **悬停**：关键字、内建函数、标准库模块与函数、文档内符号的中文说明。
4. **跳转定义**：跳到文档内定义的函数 / 类 / 变量 / 导入处。

**数据源单一**：关键字、内建、标准库签名都取自 ``ai.build_lang_spec()``，
不在这里另抄一份，避免与语言本体漂移。

协议要点：LSP 用 ``Content-Length`` 头分帧；行列是 **0 基**，而基石内部
报错是 **1 基**，转换集中在 ``_range_from_error`` 与 ``_pos`` 两处。
**位置单位按 LSP 默认的 UTF-16 码元**（``_utf16_len`` / ``_utf16_to_index``）：
中文在 BMP 内一个字符算一个码元，但 emoji 这类天文平面字符算两个，
不换算的话编辑会落在错位置（M38 B2）。

同步方式：M38 B2 起是**增量同步**（``textDocumentSync.change = 2``）：客户端
只发改动的那一段，服务端按行复用前缀重新分词（``jishi.incremental``）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from . import ast_nodes as A
from .errors import JishiError
from .incremental import IncrementalDocument
from .langdata import LangData
from .parser import parse
# 作用域分析只有一份实现（M44 抽到 scope.py）：编辑器诊断与命令行
# `jishi 源码静态检查` 共用，免得两边漂移成「编辑器不报、命令行报」。
# 这里保留原有的下划线私有名，既有调用点一个字都不用改。
from .scope import (
    attr_base_marks as _attr_base_marks,
    binding_marks as _binding_marks,
    child_nodes as _child_nodes,
    defined_names as _defined_names,
    flatten as _flatten,
    load_names as _load_names,
)
from .tokenizer import tokenize
from .version import VERSION

# --- JSON-RPC 错误码 ---
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

# --- 文本同步方式（LSP: TextDocumentSyncKind）---
SYNC_NONE = 0
SYNC_FULL = 1
SYNC_INCREMENTAL = 2

# --- LSP 诊断严重级别 ---
SEVERITY_ERROR = 1
SEVERITY_WARNING = 2

# --- LSP 补全类型 ---
KIND_METHOD = 2
KIND_FUNCTION = 3
KIND_CLASS = 7
KIND_MODULE = 9
KIND_VARIABLE = 6
KIND_KEYWORD = 14

# --- LSP 符号类型 ---
SYM_MODULE = 2
SYM_CLASS = 5
SYM_FUNCTION = 12
SYM_VARIABLE = 13

#: 所有文件都可能用到的「值关键字」，不算未定义
_LITERAL_KEYWORDS = ("真", "假", "空")

# --- 语义高亮（M42.6）---
#: 服务端声明的 token 类型表；客户端按**下标**回传，所以顺序就是契约。
#: 只列「TextMate 语法看不出来」的那些——字符串/数字/注释由扩展自带的
#: 语法文件负责，语义层不重复上色（重复反而会和主题打架）。
SEMANTIC_TYPES = (
    "keyword",      # 语法关键字 + 值关键字（真/假/空）
    "function",     # 内建函数、标准库函数、用户函数
    "variable",     # 用户变量
    "parameter",    # 用户函数的形参
    "namespace",    # 标准库模块（`容器`、`数学`…）
    "property",     # 属性名（`名.长度` 里的 `长度`）
)
SEMANTIC_MODIFIERS = (
    "declaration",     # 这是「定义处」而不是使用处
    "defaultLibrary",  # 来自语言/标准库（不是用户代码）
    "readonly",        # 值关键字（真/假/空）等不可赋值
)
_SEM_TYPE = {name: i for i, name in enumerate(SEMANTIC_TYPES)}
_SEM_MOD = {name: 1 << i for i, name in enumerate(SEMANTIC_MODIFIERS)}

#: 语义高亮只扫这些 token 类型（其余交给扩展的语法文件）
_SEM_SKIP_TOKENS = ("NEWLINE", "INDENT", "DEDENT", "EOF", "OP",
                    "STRING", "NUMBER")

# --- 快速修复（M42.3）---
CODE_ACTION_QUICKFIX = "quickfix"
CODE_ACTION_SOURCE = "source.format"


# ---------------------------------------------------------------------------
# AST 遍历工具
# ---------------------------------------------------------------------------
def _collect_symbols(program: A.Program) -> list[dict]:
    """收集文档内定义：函数 / 类 / 变量 / 导入（供跳转与文档符号）。"""
    out: list[dict] = []

    def walk(node: Any, container: Optional[str] = None) -> None:
        if isinstance(node, A.FuncDef):
            out.append({"name": node.name, "kind": SYM_FUNCTION,
                        "node": node, "container": container,
                        "detail": "(" + ", ".join(node.params) + ")"})
            for child in node.body:
                walk(child, node.name)
            return
        if isinstance(node, A.ClassDef):
            out.append({"name": node.name, "kind": SYM_CLASS,
                        "node": node, "container": container, "detail": "类"})
            for child in node.body:
                walk(child, node.name)
            return
        if isinstance(node, A.Assign):
            for n in _flatten(node.target):
                if isinstance(n, A.Name):
                    out.append({"name": n.id, "kind": SYM_VARIABLE,
                                "node": n, "container": container,
                                "detail": "变量"})
        elif isinstance(node, A.For):
            for n in _flatten(node.target):
                if isinstance(n, A.Name):
                    out.append({"name": n.id, "kind": SYM_VARIABLE,
                                "node": n, "container": container,
                                "detail": "循环变量"})
        elif isinstance(node, A.Import):
            out.append({"name": node.alias or node.name, "kind": SYM_MODULE,
                        "node": node, "container": container,
                        "detail": "来自 Python" if node.from_python else "模块"})
        for child in _child_nodes(node):
            walk(child, container)

    walk(program)
    # 同一名字在同一作用域重复出现（如反复赋值）只保留第一次
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for s in out:
        key = (s["name"], s["kind"], s["container"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped
# ---------------------------------------------------------------------------
# 位置换算
# ---------------------------------------------------------------------------

def _pos(line0: int, char0: int) -> dict:
    return {"line": max(0, line0), "character": max(0, char0)}


def _range_from_error(err: JishiError) -> dict:
    """JishiError 的 1 基行列 → LSP 的 0 基区间（末端不含）。"""
    line = max(1, err.line or 1)
    start, end = err.underline or (err.col, err.col)
    start = max(1, start or 1)
    end = max(start, end or start)
    return {"start": _pos(line - 1, start - 1), "end": _pos(line - 1, end)}


def _utf16_len(s: str) -> int:
    """文本在 LSP 位置单位（UTF-16 码元）下的长度。

    LSP 默认 ``positionEncoding = utf-16``：中文等 BMP 字符算 1，emoji 这类
    天文平面字符算 2（Python 里它们都是 1 个字符）。**入向的编辑范围必须按
    这个换算**——错一格就会把文档改坏，而改坏之后所有后续诊断都是错的
    （出向的位置同理，M38 B2）。
    """
    return len(s.encode("utf-16-le")) // 2


def _utf16_to_index(s: str, units: int) -> int:
    """UTF-16 码元数 → Python 字符下标（越界时夹到字符串两端）。"""
    if units <= 0:
        return 0
    if units >= _utf16_len(s):
        return len(s)
    # 逐字符累加：单行的长度有限，够用；比一次 encode 再切更省内存
    total = 0
    for i, ch in enumerate(s):
        total += 2 if ord(ch) > 0xFFFF else 1
        if total > units:
            # 落在代理对中间（理论上不会发生）：退到字符边界
            return i + 1
        if total == units:
            return i + 1
    return len(s)


def _offset_to_pos(text: str, offset: int) -> dict:
    """字符偏移 → (行, 列)，行 0 基、列按 UTF-16 码元。"""
    offset = max(0, min(offset, len(text)))
    line = text.count("\n", 0, offset)
    last_nl = text.rfind("\n", 0, offset)
    char = _utf16_len(text[last_nl + 1:offset])
    return _pos(line, char)


def _pos_to_offset(text: str, line0: int, char0: int) -> int:
    """(行, 列) 0 基（列按 UTF-16 码元）→ 字符偏移。"""
    lines = text.split("\n")
    line0 = max(0, min(line0, len(lines) - 1))
    offset = sum(len(l) + 1 for l in lines[:line0])
    return offset + _utf16_to_index(lines[line0], char0)


def _whole_doc_range(text: str) -> dict:
    """整篇文档的区间（格式化用：LSP 要求一次给全文替换）。"""
    lines = text.split("\n")
    return {"start": _pos(0, 0),
            "end": _pos(len(lines) - 1, _utf16_len(lines[-1]))}


def _uri_to_path(uri: Any) -> "Optional[Path]":
    """`file:///x/y.jsh` → `Path`。非 file: 协议或空值一律返回 None。

    只做必要的百分号解码（空格、中文路径都会出现），不引 urllib 解析器——
    客户端给的基本就是 `file://` + 路径。
    """
    if not uri or not isinstance(uri, str):
        return None
    if not uri.startswith("file://"):
        return None
    from urllib.parse import unquote, urlparse

    p = urlparse(uri)
    路径 = unquote(p.path)
    if p.netloc and p.netloc not in ("", "localhost"):
        # Windows 的 `file:///C:/...` 走 path；UNC 走 netloc
        路径 = f"//{p.netloc}{路径}"
    if len(路径) > 2 and 路径[0] == "/" and 路径[2] == ":":
        路径 = 路径[1:]            # Windows 盘符前多一个斜杠
    try:
        return Path(路径)
    except (ValueError, OSError):
        return None


def _path_to_uri(path: "Path | str") -> str:
    """`Path` → `file:///...`（与 `_uri_to_path` 互逆，供跨文件跳转回 uri）。"""
    from urllib.parse import quote

    p = Path(path).resolve().as_posix()
    if not p.startswith("/"):
        p = "/" + p                  # Windows: C:/x → /C:/x
    return "file://" + quote(p, safe="/:()~!$&'*,;=@+[]")


def _utf16_col(line_text: str, char_col0: int) -> int:
    """字符列（0 基）→ UTF-16 码元列。

    **为什么必须换**：词法器给的行列是**按 Python 字符数**算的，而 LSP 默认
    位置单位是 UTF-16 码元。中文在 BMP 内两者一致，但 emoji 这类天文平面
    字符算 2 个码元——不换算的话，光标落在那之后的所有位置都会偏一格。
    """
    return _utf16_len(line_text[:max(0, char_col0)])


def _name_refs(program: A.Program, name: str) -> list[A.Name]:
    """文档里所有**名字等于 `name` 的 Name 节点**（含解包目标里的名字）。

    `_flatten` 会把 `TargetList` 摊成里面的 `Name`，所以 `令 甲, 乙 = …`
    的解包目标也能被收集到（M41 修的那条 LSP 缺陷）。
    """
    out: list[A.Name] = []

    def walk(node: Any) -> None:
        if isinstance(node, A.Name) and node.id == name:
            out.append(node)
        for child in _child_nodes(node):
            walk(child)

    walk(program)
    return out


def _child_nodes_of_kind(node: Any, cls: type) -> list:
    """收集直接子节点里指定类型的那些（用于找 `Attr` 的基址/属性名）。"""
    return [c for c in _child_nodes(node) if isinstance(c, cls)]


def _def_name_col(line_text: str, name: str, hint_col0: int) -> int:
    """在定义所在行里找**名字本身**的列（0 基字符列）。

    AST 只记「定义语句」的行列——对 `函数 甲(…)` 来说那是 `函数` 的位置，
    而不是 `甲`。重命名要把名字本身换掉，所以从提示列往后找一次名字。
    找不到就退回提示列：位置略偏好过整条编辑丢掉。
    """
    idx = line_text.find(name, max(0, hint_col0))
    if idx < 0:
        idx = line_text.find(name)
    return idx if idx >= 0 else max(0, hint_col0)


def _is_valid_name(name: str) -> bool:
    """是不是合法的基石标识符（重命名前先挡一道）。

    规则与词法器一致：首字符为字母/下划线/汉字，其后还可有数字。
    不允许的话改完直接编不过——那不是重命名，是制造语法错误。
    """
    if not name:
        return False
    if name[0].isdigit():
        return False
    return all(_is_word_char(ch) for ch in name)


def _ranges_touch(a: dict, b: dict) -> bool:
    """两个 LSP 区间是否相交（`b` 为空区间时退化成「点是否在 a 内」）。

    编辑器传过来的 range 经常是个空区间（光标处），这时按「点在不在诊断里」
    判断——不然右键菜单里永远看不到快速修复。
    """
    if not a or not b:
        return True                       # 没给 range：按「全都要」处理
    a0, a1 = _pos_key(a.get("start")), _pos_key(a.get("end"))
    b0, b1 = _pos_key(b.get("start")), _pos_key(b.get("end"))
    if b0 == b1:                          # 空区间
        return a0 <= b0 <= a1
    return not (b1 < a0 or b0 > a1)


def _pos_key(p: Any) -> tuple:
    p = p or {}
    return (int(p.get("line", 0)), int(p.get("character", 0)))


def _binding_positions(program: Any) -> set[tuple[int, int]]:
    """所有「绑定位置」的 `(行, 列)`（1 基）——语义高亮标 `declaration` 用。"""
    if program is None:
        return set()
    out: set[tuple[int, int]] = set()
    for n in _flatten_targets(program):
        out.add((n.line, n.col))
    return out


def _flatten_targets(program: Any) -> list[A.Name]:
    """收集所有处在「绑定位置」的 Name 节点（含解包目标）。"""
    marks = _binding_marks(program)
    out: list[A.Name] = []

    def walk(node: Any) -> None:
        if isinstance(node, A.Name) and id(node) in marks:
            out.append(node)
        for child in _child_nodes(node):
            walk(child)

    walk(program)
    return out


def _param_positions(tokens) -> set[tuple[int, int]]:
    """形参在源码里的位置（`函数 甲(甲, 乙)：` 里那两个名字）。

    **为什么要单独算**：AST 的 `FuncDef.params` 只是一串名字（不记位置），
    所以「形参的定义处」在 AST 里根本不存在。但语义高亮要给它标
    `declaration`，只能从 token 流里数出来：

        `函数` → 下一个 NAME 是**函数名**（跳过）→ `(` 进入形参表
        → 按括号深度收名字（默认值里的括号不会数错）→ 配对的 `)` 结束
    """
    out: set[tuple[int, int]] = set()
    待函数名 = False
    等左括号 = False
    深度 = 0
    for t in tokens:
        if 待函数名:
            if t.type == "KEYWORD":
                continue                     # `函数 甲` 之间不会有别的关键字
            if t.type == "NAME":
                待函数名 = False
                等左括号 = True
                continue
            待函数名 = False
            等左括号 = False
            continue
        if t.type == "OP":
            v = str(t.value)
            if 等左括号:
                if v == "(":
                    深度 = 1
                    等左括号 = False
                    continue
                等左括号 = False
                continue
            if 深度 > 0:
                if v == "(":
                    深度 += 1
                elif v == ")":
                    深度 -= 1
                continue
            continue
        if 深度 > 0:
            if t.type == "NAME":
                out.add((t.line, t.col))
            continue
        if t.type == "KEYWORD" and t.value == "函数":
            待函数名 = True
    return out


def _call_context(text: str, offset: int) -> "Optional[tuple[str, int]]":
    """光标处的调用上下文 → `(被调用的名字, 第几个实参)`。

    做法是从光标往回扫，配对括号：遇到未闭合的 `(` 就认定「这是当前调用」，
    再往前读被调用的名字（支持 `模块.函数`）。同时数**顶层逗号**得出实参序号
    ——括号里的逗号、字符串里的逗号都不算，否则 `打印(配对(a, b), 1)` 会数错。
    """
    depth = 0
    i = min(offset, len(text)) - 1
    quote: "Optional[str]" = None
    while i >= 0:
        ch = text[i]
        if quote is not None:
            if ch == quote and (i == 0 or text[i - 1] != "\\"):
                quote = None
            i -= 1
            continue
        if ch in "\"'":
            quote = ch
            i -= 1
            continue
        if ch in ")]}":
            depth += 1
        elif ch == "(":
            if depth == 0:
                break                     # 就是它：当前调用的左括号
            depth -= 1
        elif ch in "[{":
            # 往回收，遇到的开括号可能是「光标所在的字面量」（比如
            # `计数([1, 2|])`——`[` 开着、`]` 还在光标右边）。
            # 这种既不是调用括号、也不是待配对的，**绕过它继续往外找**，
            # 否则实参里一有列表/字典，签名提示就整个失效（M42 踩到）。
            if depth > 0:
                depth -= 1
        i -= 1
    else:
        return None                       # 没找到未闭合的括号

    open_at = i
    # 往前读被调用者：标识符（可带 `.` 分段）
    j = open_at - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    end = j + 1
    while j >= 0 and (_is_word_char(text[j]) or text[j] == "."):
        j -= 1
    callee = text[j + 1:end]
    if not callee or callee.startswith(".") or callee.endswith("."):
        return None

    # 数顶层逗号（从中缀处的括号到光标）
    depth = 0
    逗号 = 0
    quote = None
    k = open_at + 1
    while k < offset:
        ch = text[k]
        if quote is not None:
            if ch == quote and text[k - 1] != "\\":
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            逗号 += 1
        k += 1
    return callee, 逗号


def _sig_from_params(name: str, params: list[str], detail: str = "") -> dict:
    """由形参表造一条签名（标签里逐个标出每个形参的位置）。"""
    标签 = f"{name}({', '.join(params)})"
    出 = []
    at = len(name) + 1
    for p in params:
        出.append({"label": [at, at + len(p)], "documentation": ""})
        at += len(p) + 2
    return {"label": 标签, "parameters": 出,
            "documentation": {"kind": "markdown", "value": detail or ""}}


def _sig_from_text(第一行: str, 全文: str = "") -> "Optional[dict]":
    """从「`名字(甲, 乙)：说明`」这类文本里抠出签名。

    内建函数的说明就是这个格式（`ai.build_lang_spec()` 的产物），所以直接
    解析它的第一行即可——不另建一份参数表，避免与语言本体漂移。
    """
    s = (第一行 or "").strip()
    if "(" not in s or ")" not in s:
        return None
    head, _, rest = s.partition("(")
    名字 = head.strip()
    参数段, _, 尾 = rest.partition(")")
    if not 名字:
        return None
    params = [p.strip() for p in 参数段.split(",") if p.strip()]
    说明 = 尾.strip(" ：:") or 全文.strip()
    return _sig_from_params(名字, params, 说明)


def _apply_change(text: str, change: dict) -> str:
    """把一条 ``contentChange`` 应用到文本上（M38 B2）。

    没有 ``range`` 的是**全量替换**（客户端用 Full 同步、或我们没声明
    incremental 时的兜底）；有 ``range`` 的按区间替换——区间是**半开**的
    ``[start, end)``，且行列按 UTF-16 码元。
    """
    new_text = change.get("text")
    if new_text is None:
        return text
    rng = change.get("range")
    if not rng:
        return new_text
    start = rng.get("start") or {}
    end = rng.get("end") or {}
    a = _pos_to_offset(text, int(start.get("line", 0)), int(start.get("character", 0)))
    b = _pos_to_offset(text, int(end.get("line", 0)), int(end.get("character", 0)))
    if b < a:                            # 客户端给反了：别把文本改坏
        a, b = b, a
    return text[:a] + new_text + text[b:]


def _word_at(text: str, line0: int, char0: int) -> tuple[str, int, int]:
    """取光标处的「词」及其偏移区间（用于悬停/跳转）。

    词字符：字母数字下划线 + 中日韩汉字。若光标右侧紧邻词字符，取右侧。
    """
    is_word = _is_word_char
    start = _pos_to_offset(text, line0, char0)
    n = len(text)
    if start < n and not is_word(text[start]) and start > 0 and is_word(text[start - 1]):
        start -= 1
    left = start
    while left > 0 and is_word(text[left - 1]):
        left -= 1
    right = start
    while right < n and is_word(text[right]):
        right += 1
    return text[left:right], left, right


def _is_word_char(ch: str) -> bool:
    if ch.isalnum() or ch == "_":
        return True
    return "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"


# ---------------------------------------------------------------------------
# 语言元数据（单一数据源）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------

class LspServer:
    def __init__(self, stdin=None, stdout=None) -> None:
        self.r_in = stdin if stdin is not None else sys.stdin.buffer
        self.w_out = stdout if stdout is not None else sys.stdout.buffer
        self.docs: dict[str, dict] = {}
        self.symbols: dict[str, list[dict]] = {}
        #: uri -> 最近一次分析出的诊断（供编辑器外的调用方/测试读取）
        self.diagnostics: dict[str, list[dict]] = {}
        #: uri -> 由报错里的「修法建议」生成的快速修复（M42.3）。
        #: 与 diagnostics **分开存**：诊断是要发给客户端的协议数据，
        #: 修复是服务端的内部知识（多塞字段会污染协议）。
        self.fixes: dict[str, list[dict]] = {}
        #: uri -> 最近一次成功解析出的 AST（重命名/语义高亮要用；
        #: 解析失败时置 None——那说明文档此刻连语法都不完整）
        self.programs: dict[str, Any] = {}
        #: 工作区根目录（M42.5，`initialize` 时从 rootUri/workspaceFolders 取）
        self.root: Optional[Path] = None
        #: 工作区文件索引：路径 -> {"mtime", "text", "symbols"}（M42.5）
        self._file_cache: dict[str, dict] = {}
        self._lang: Optional[LangData] = None
        self._stopped = False

    # -- 元数据（首次用到时才构建，加快启动） ---------------------------------

    @property
    def lang(self) -> LangData:
        if self._lang is None:
            self._lang = LangData()
        return self._lang

    # -- 传输 ---------------------------------------------------------------

    def _read_message(self) -> "Optional[dict]":
        length = None
        while True:
            raw = self.r_in.readline()
            if not raw:
                return None
            line = raw.decode("ascii", "replace").strip()
            if line == "":
                break
            if line.lower().startswith("content-length:"):
                try:
                    length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    return None
        if length is None:
            return None
        body = self.r_in.read(length)
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _write(self, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        head = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        self.w_out.write(head + data)
        self.w_out.flush()

    def _reply(self, msg_id: Any, result: Any = None,
               error: "Optional[dict]" = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self._write(msg)

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # -- 主循环 -------------------------------------------------------------

    def serve(self) -> int:
        while not self._stopped:
            msg = self._read_message()
            if msg is None:
                break
            self.dispatch(msg)
        return 0

    def dispatch(self, msg: dict) -> None:
        """处理一条消息（测试可直接调用，不必走 stdio）。"""
        method = msg.get("method")
        msg_id = msg.get("id")
        if method is None:
            return                      # 响应类消息，忽略
        handler = self.HANDLERS.get(method)
        if handler is None:
            if msg_id is not None:
                self._reply(msg_id, error={
                    "code": METHOD_NOT_FOUND,
                    "message": f"未实现的方法：{method}"})
            return
        try:
            result = handler(self, msg.get("params") or {})
        except Exception as e:          # noqa: BLE001 —— 绝不让异常打断循环
            if msg_id is not None:
                self._reply(msg_id, error={
                    "code": INTERNAL_ERROR,
                    "message": f"内部错误：{type(e).__name__}: {e}"})
            return
        if msg_id is not None:
            self._reply(msg_id, result=result)

    # -- 生命周期 -----------------------------------------------------------

    def on_initialize(self, params: dict) -> dict:
        self.root = self._workspace_root(params)
        return {
            "capabilities": {
                # 2 = 增量同步（M38 B2）：客户端只发改动的那一段。
                # 以前是 1（全量），等于每次按键都重收整份文件。
                "textDocumentSync": {
                    "openClose": True,
                    "change": SYNC_INCREMENTAL,
                    "save": True,
                },
                "completionProvider": {"triggerCharacters": ["."]},
                "hoverProvider": True,
                "definitionProvider": True,
                "documentSymbolProvider": True,
                # --- M42 新增的六项 ---
                "documentFormattingProvider": True,
                "renameProvider": {"prepareProvider": True},
                "codeActionProvider": {
                    "codeActionKinds": [CODE_ACTION_QUICKFIX,
                                        CODE_ACTION_SOURCE],
                },
                "signatureHelpProvider": {"triggerCharacters": ["(", "，", ","]},
                "workspaceSymbolProvider": True,
                "semanticTokensProvider": {
                    "legend": {"tokenTypes": list(SEMANTIC_TYPES),
                               "tokenModifiers": list(SEMANTIC_MODIFIERS)},
                    "full": True,
                },
            },
            "serverInfo": {"name": "jishi-lsp", "version": VERSION},
        }

    def _workspace_root(self, params: dict) -> "Optional[Path]":
        """从 `initialize` 参数里取工作区根目录（取不到就返回 None）。

        三种客户端习惯都要认：`workspaceFolders`（现代）、`rootUri`（次之）、
        `rootPath`（老客户端）。全都没有时退回「当前进程的工作目录」——
        客户端一般就用工作区当 cwd 启动我们。
        """
        folders = params.get("workspaceFolders") or []
        for f in folders:
            u = (f or {}).get("uri")
            p = _uri_to_path(u)
            if p is not None:
                return p
        p = _uri_to_path(params.get("rootUri"))
        if p is not None:
            return p
        rp = params.get("rootPath")
        if rp:
            return Path(str(rp))
        return Path.cwd()

    def on_shutdown(self, params: dict) -> None:
        return None

    def on_exit(self, params: dict) -> None:
        self._stopped = True
        return None

    # -- 文档同步 -----------------------------------------------------------

    def _uri(self, params: dict) -> str:
        return ((params.get("textDocument") or {}).get("uri")) or ""

    def on_did_open(self, params: dict) -> None:
        doc = params.get("textDocument") or {}
        uri = doc.get("uri") or ""
        text = doc.get("text") or ""
        self.docs[uri] = {
            "text": text,
            "version": doc.get("version"),
            # 增量文档对象（M38 B2）：它自己维护 token 与每行的入行状态锚点
            "inc": IncrementalDocument(text, uri),
        }
        self._analyze(uri)
        return None

    def on_did_change(self, params: dict) -> None:
        """接收编辑（M38 B2：增量同步）。

        三件事按这个顺序做，缺一个都会出问题：

        1. **版本乱序保护**：编辑器偶尔会把旧版本重发过来，若照单全收，
           诊断会「改完又变回去」。所以收到的版本不比当前新时直接忽略。
        2. **按区间应用编辑**：客户端只发改动的那一段（全量替换也认）；
           区间行列按 UTF-16 码元换算（见 `_apply_change`）。
        3. **重分析**：`_analyze` 内部走增量分词（文本没变则整个跳过）。
        """
        uri = self._uri(params)
        changes = params.get("contentChanges") or []
        doc = self.docs.get(uri)
        if doc is None or not changes:
            return None
        version = (params.get("textDocument") or {}).get("version")
        cur = doc.get("version")
        if isinstance(version, int) and isinstance(cur, int) and version <= cur:
            return None                  # 旧版本/重复版本：不覆盖新文本
        text = doc.get("text") or ""
        for ch in changes:
            text = _apply_change(text, ch)
        doc["text"] = text
        doc["version"] = version
        self._analyze(uri)
        return None

    def on_did_close(self, params: dict) -> None:
        uri = self._uri(params)
        self.docs.pop(uri, None)
        self.symbols.pop(uri, None)
        self.diagnostics.pop(uri, None)
        self.programs.pop(uri, None)
        self.fixes.pop(uri, None)
        self._notify("textDocument/publishDiagnostics",
                     {"uri": uri, "diagnostics": []})
        return None

    def on_did_save(self, params: dict) -> None:
        self._analyze(self._uri(params))
        return None

    # -- 分析与诊断 ---------------------------------------------------------

    def _analyze(self, uri: str) -> None:
        doc = self.docs.get(uri)
        if doc is None:
            return
        text = doc.get("text") or ""
        diags: list[dict] = []
        try:
            inc = doc.get("inc")
            if inc is None:
                lines = text.split("\n")
                tokens = tokenize(text, uri)
            else:
                # 增量分词：文本没变是空操作，改了只重算改动行之后的部分
                inc.update(text)
                tokens, lines = inc.tokens, inc.lines
            program = parse(tokens, lines, uri)
        except JishiError as e:
            rng = _range_from_error(e)
            diags.append({
                "range": rng,
                "severity": SEVERITY_ERROR,
                "code": e.code,
                "source": "jishi",
                "message": self._error_text(e),
            })
            self.symbols[uri] = []
            self.programs[uri] = None            # 语法都不完整：别拿去改名/上色
            self.fixes[uri] = self._quick_fixes_from(e, rng)
        else:
            self.symbols[uri] = _collect_symbols(program)
            self.programs[uri] = program
            self.fixes[uri] = []
            diags.extend(self._undefined_warnings(program))
        self.diagnostics[uri] = diags
        self._notify("textDocument/publishDiagnostics",
                     {"uri": uri, "diagnostics": diags,
                      "version": doc.get("version")})

    def _error_text(self, e: JishiError) -> str:
        parts = [f"{e.title}"]
        if e.message:
            parts.append(e.message)
        text = "：".join(parts) if len(parts) > 1 else parts[0]
        text = f"[{e.code}] {text}"
        if e.hint:
            text += f"\n提示：{e.hint}"
        return text

    @staticmethod
    def _quick_fixes_from(e: JishiError, rng: dict) -> list[dict]:
        """把报错里的「修法建议」变成可点的快速修复（M42.3）。

        `errors.JishiError.fix` 的形状是 `{"old": 原文, "new": 替换为}`，
        位置就是报错**划线的那个区间**（`underline` 与 `fix.old` 指向同一段
        文本）——所以直接把 `rng` 那段替换成 `new` 即可，不用再猜位置。
        """
        fx = getattr(e, "fix", None)
        if not fx:
            return []
        old = str(fx.get("old") or "")
        new = str(fx.get("new") or "")
        if not old or old == new:
            return []
        return [{"title": f"改成「{new}」", "range": rng, "newText": new}]

    def _undefined_warnings(self, program: A.Program) -> list[dict]:
        """保守的「名字没定义过」警告：全文档任一作用域绑定过就不报。

        已知边界：属性基址（`基础库.平方`）不参与判断——包依赖是运行时
        注入命名空间的，静态看不到。
        """
        lang = self.lang
        known = set(lang.builtins) | set(lang.keywords) | set(lang.exceptions)
        known.update(_LITERAL_KEYWORDS)
        known.update(lang.stdlib)
        known.update(_defined_names(program))
        marks = _binding_marks(program)
        marks |= _attr_base_marks(program)

        out: list[dict] = []
        seen: set[tuple[str, int]] = set()
        for node in _load_names(program, marks):
            if node.id in known:
                continue
            # 内部保留名（`$文本`，文本插值脱糖的目标）不算「用户符号」：
            # 它不在语言规格里（有意过滤掉，不推荐给用户），但插值展开后
            # 会真的出现在 AST 里——不跳过就会满屏误报「名字「$文本」没有
            # 定义过」（M40 全量回归抓到）。
            if node.id.startswith("$"):
                continue
            key = (node.id, node.line)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "range": {"start": _pos(node.line - 1, node.col - 1),
                          "end": _pos(node.line - 1, node.col - 1 + len(node.id))},
                "severity": SEVERITY_WARNING,
                "source": "jishi",
                "message": f"名字「{node.id}」在这份文件里没有定义过",
                "relatedInformation": None,
            })
        return out

    # -- 补全 ---------------------------------------------------------------

    def on_completion(self, params: dict) -> dict:
        uri = self._uri(params)
        doc = self.docs.get(uri)
        pos = (params.get("position") or {})
        if doc is None:
            return {"isIncomplete": False, "items": []}
        text = doc["text"]
        off = _pos_to_offset(text, pos.get("line", 0),
                             pos.get("character", 0))
        dot_base, word = self._completion_context(text, off)
        lang = self.lang

        items: list[dict] = []
        if dot_base:
            items.extend(self._attr_items(dot_base))
        else:
            for name, desc in lang.keywords.items():
                items.append({"label": name, "kind": KIND_KEYWORD,
                              "detail": desc,
                              "documentation": {"kind": "markdown",
                                                "value": f"**关键字**：{desc}"}})
            for name, doc_text in lang.builtins.items():
                items.append({"label": name, "kind": KIND_FUNCTION,
                              "detail": self._first_line(doc_text),
                              "documentation": {"kind": "markdown",
                                                "value": doc_text or name}})
            for mod in lang.stdlib:
                items.append({"label": mod, "kind": KIND_MODULE,
                              "detail": "标准库模块",
                              "documentation": {
                                  "kind": "markdown",
                                  "value": f"标准库模块 `{mod}`，"
                                           f"用 `导入 {mod}` 后使用"}})
            for sym in self.symbols.get(uri, []):
                items.append({"label": sym["name"],
                              "kind": self._item_kind(sym["kind"]),
                              "detail": sym.get("detail", ""),
                              "documentation": {
                                  "kind": "markdown",
                                  "value": (f"`{sym['name']}{sym.get('detail', '')}`"
                                            f"（本文件第 {sym['node'].line} 行）")}})

        if word:
            items = [it for it in items if it["label"].startswith(word)]
        return {"isIncomplete": False, "items": self._dedupe(items)}

    @staticmethod
    def _dedupe(items: list[dict]) -> list[dict]:
        """同名条目只留第一条（同一变量多次赋值会重复出现）。"""
        seen: set[str] = set()
        out: list[dict] = []
        for it in items:
            if it["label"] in seen:
                continue
            seen.add(it["label"])
            out.append(it)
        return out

    def _completion_context(self, text: str, off: int) -> tuple[str, str]:
        """返回 (点号基址, 前缀)。

        - 光标紧跟 `.`（或压在 `.` 上）：基址为点号前的标识符、前缀为空，
          对应 `随机.` 这类属性补全；
        - 否则基址为空、前缀为光标前的标识符片段，做普通前缀补全。
        """
        n = len(text)
        p = off
        while p > 0 and _is_word_char(text[p - 1]):
            p -= 1
        prefix = text[p:off]

        dot_at = -1
        if p > 0 and text[p - 1] == ".":
            dot_at = p - 1
        elif off > 0 and text[off - 1] == ".":
            dot_at = off - 1
            prefix = ""
        elif off < n and text[off] == ".":
            dot_at = off
            prefix = ""

        if dot_at < 0:
            return "", prefix
        q = dot_at
        while q > 0 and _is_word_char(text[q - 1]):
            q -= 1
        return text[q:dot_at], prefix

    def _item_kind(self, sym_kind: int) -> int:
        if sym_kind == SYM_FUNCTION:
            return KIND_FUNCTION
        if sym_kind == SYM_CLASS:
            return KIND_CLASS
        if sym_kind == SYM_MODULE:
            return KIND_MODULE
        return KIND_VARIABLE

    def _dot_base_unused(self, text: str, word_start: int) -> str:
        """（保留占位：属性补全已并入 _completion_context）"""
        return ""

    def _attr_items(self, base: str) -> list[dict]:
        lang = self.lang
        if base in lang.stdlib:
            return [
                {"label": f["name"], "kind": KIND_FUNCTION,
                 "detail": f.get("sig", ""),
                 "documentation": {"kind": "markdown",
                                   "value": (f"`{f.get('sig') or f['name']}`\n\n"
                                             f"{f.get('doc') or ''}")}}
                for f in lang.stdlib[base]
            ]
        # 变量类型未知：把三类容器的常用方法都给出
        return [
            {"label": name, "kind": KIND_METHOD, "detail": "方法",
             "documentation": {"kind": "markdown", "value": f"方法 `{name}`"}}
            for name in lang.all_methods()
        ]

    @staticmethod
    def _first_line(text: str) -> str:
        return (text or "").strip().split("\n")[0]

    # -- 悬停 ---------------------------------------------------------------

    def on_hover(self, params: dict) -> "Optional[dict]":
        uri = self._uri(params)
        doc = self.docs.get(uri)
        pos = params.get("position") or {}
        if doc is None:
            return None
        text = doc["text"]
        word, w_start, w_end = _word_at(text, pos.get("line", 0),
                                        pos.get("character", 0))
        if not word:
            return None
        body = self._describe(uri, word)
        if body is None:
            return None
        return {
            "contents": {"kind": "markdown", "value": body},
            "range": {"start": _offset_to_pos(text, w_start),
                      "end": _offset_to_pos(text, w_end)},
        }

    def _describe(self, uri: str, word: str) -> "Optional[str]":
        lang = self.lang
        if word in lang.keywords:
            return f"**{word}**（关键字）—— {lang.keywords[word]}"
        if word in lang.builtins:
            return f"**{word}**（内建函数）\n\n{lang.builtins[word]}"
        if word in lang.stdlib:
            funcs = lang.stdlib[word]
            lines = [f"**{word}**（标准库模块）", ""]
            for f in funcs[:12]:
                lines.append(f"- `{f.get('sig') or f['name']}` — {f.get('doc') or ''}")
            if len(funcs) > 12:
                lines.append(f"- …… 共 {len(funcs)} 个函数")
            return "\n".join(lines)
        if word in lang.exceptions:
            return f"**{word}**（异常类型）"
        for sym in self.symbols.get(uri, []):
            if sym["name"] == word:
                kind = {SYM_FUNCTION: "函数", SYM_CLASS: "类",
                        SYM_MODULE: "模块", SYM_VARIABLE: "变量"}.get(
                            sym["kind"], "符号")
                detail = sym.get("detail", "")
                return (f"**{word}**（{kind}，本文件第 {sym['node'].line} 行）\n\n"
                        f"```\n{word}{detail}\n```")
        return None

    # -- 跳转定义 -----------------------------------------------------------

    def on_definition(self, params: dict) -> "Optional[dict]":
        uri = self._uri(params)
        doc = self.docs.get(uri)
        pos = params.get("position") or {}
        if doc is None:
            return None
        word, _, _ = _word_at(doc["text"], pos.get("line", 0),
                              pos.get("character", 0))
        if not word:
            return None
        for sym in self.symbols.get(uri, []):
            if sym["name"] == word:
                node = sym["node"]
                return {
                    "uri": uri,
                    "range": {
                        "start": _pos(node.line - 1, node.col - 1),
                        "end": _pos(node.line - 1, node.col - 1 + len(word)),
                    },
                }
        # 本地没有 → 去工作区别的文件里找（M42.5）。唯一命中才跳，
        # 理由见 `_cross_file_definition`。
        return self._cross_file_definition(uri, word)

    # -- 文档符号 -----------------------------------------------------------

    def on_document_symbol(self, params: dict) -> list:
        uri = self._uri(params)
        out = []
        for sym in self.symbols.get(uri, []):
            node = sym["node"]
            out.append({
                "name": sym["name"],
                "kind": sym["kind"],
                "detail": sym.get("detail", ""),
                "range": {"start": _pos(node.line - 1, node.col - 1),
                          "end": _pos(node.line - 1, node.col - 1 + len(sym["name"]))},
                "selectionRange": {
                    "start": _pos(node.line - 1, node.col - 1),
                    "end": _pos(node.line - 1, node.col - 1 + len(sym["name"]))},
            })
        return out

    # -- 格式化（M42.1）-----------------------------------------------------

    def on_formatting(self, params: dict) -> list:
        """把已有的格式化器接到 LSP 上（一份实现，两处用）。

        **语法有错时什么都不做**：编辑器里正在打字，半截代码是常态，
        这时候「帮你格式化」只会把用户正在写的东西搅乱。评审意见一致：
        宁可这次不动，也不要动坏。
        """
        uri = self._uri(params)
        doc = self.docs.get(uri)
        if doc is None:
            return []
        text = doc.get("text") or ""
        from .formatter import format_source

        try:
            out = format_source(text, uri)
        except JishiError:
            return []                    # 还在写：不动
        except Exception:                # noqa: BLE001 - 格式化器出错不该拖垮服务
            return []
        if out == text:
            return []
        # 全文替换：LSP 的约定是一次给整篇（区间从 (0,0) 到最后一行行尾）
        return [{"range": _whole_doc_range(text), "newText": out}]

    # -- 重命名（M42.2）-----------------------------------------------------

    def on_prepare_rename(self, params: dict) -> "Optional[dict]":
        """先回答「这个位置能改名吗」——不能就返回 null，编辑器会禁用输入框。

        **边界（有意为之）**：只支持**模块级**符号（函数 / 类 / 顶层变量 /
        导入名）。函数形参与函数内的局部变量**不支持**——AST 不记形参的位置，
        而且没有作用域分析就改名会把「另一个函数里同名的局部变量」一起改掉，
        那是在**改坏用户代码**。做不到就明确说不做（返回 null），
        比给一个错的编辑安全得多。
        """
        uri = self._uri(params)
        doc = self.docs.get(uri)
        if doc is None:
            return None
        pos = params.get("position") or {}
        word, w_start, w_end = _word_at(doc["text"], pos.get("line", 0),
                                        pos.get("character", 0))
        if not word or not self._renameable(uri, word):
            return None
        return {
            "range": {"start": _offset_to_pos(doc["text"], w_start),
                      "end": _offset_to_pos(doc["text"], w_end)},
            "placeholder": word,
        }

    def _renameable(self, uri: str, word: str) -> bool:
        """这个名字能不能整篇改名（见 `on_prepare_rename` 的边界说明）。"""
        lang = self.lang
        if word in lang.keywords or word in _LITERAL_KEYWORDS:
            return False
        if word in lang.builtins or word in lang.stdlib or word in lang.exceptions:
            return False
        for sym in self.symbols.get(uri, []):
            if sym["name"] == word and sym.get("container") is None:
                return True
        return False

    def on_rename(self, params: dict) -> "Optional[dict]":
        """整篇改名：定义处 + 所有引用处，一次给出所有编辑。"""
        uri = self._uri(params)
        doc = self.docs.get(uri)
        new_name = (params.get("newName") or "").strip()
        if doc is None or not new_name:
            return None
        pos = params.get("position") or {}
        word, _, _ = _word_at(doc["text"], pos.get("line", 0),
                              pos.get("character", 0))
        if not word or not self._renameable(uri, word):
            return None
        # 新名字必须是合法的标识符（中文/字母/数字/下划线，不以数字开头），
        # 否则改完就编不过——这不是「重命名」，是「制造语法错误」。
        if not _is_valid_name(new_name):
            return None
        program = self.programs.get(uri)
        if program is None:
            return None

        text = doc["text"]
        lines = text.split("\n")
        edits: list[dict] = []
        seen: set[tuple[int, int]] = set()

        def 加(line1: int, char_col0: int) -> None:
            key = (line1, char_col0)
            if key in seen:
                return
            seen.add(key)
            line_text = lines[line1 - 1] if 0 < line1 <= len(lines) else ""
            start = _utf16_col(line_text, char_col0)
            edits.append({
                "range": {"start": _pos(line1 - 1, start),
                          "end": _pos(line1 - 1, start + _utf16_len(word))},
                "newText": new_name,
            })

        # ① 定义处（除了 Name 节点，还要管 `函数 甲(…)` 这种「名字不在 AST 里」的）
        for sym in self.symbols.get(uri, []):
            if sym["name"] != word or sym.get("container") is not None:
                continue
            node = sym["node"]
            if isinstance(node, (A.FuncDef, A.ClassDef)):
                line_text = lines[node.line - 1] if 0 < node.line <= len(lines) else ""
                加(node.line, _def_name_col(line_text, word, node.col - 1))
        # ② 所有引用（含解包目标里的名字）
        for n in _name_refs(program, word):
            加(n.line, n.col - 1)

        if not edits:
            return None
        return {"changes": {uri: edits}}

    # -- 快速修复（M42.3）---------------------------------------------------

    def on_code_action(self, params: dict) -> list:
        """把报错里的「修法建议」变成可点的快速修复。

        另外附一条「格式化本文档」——它属于 `source.format` 类，编辑器把它
        放在「重构」菜单里，不会干扰修错的主流程。
        """
        uri = self._uri(params)
        if uri not in self.docs:
            return []
        rng = params.get("range") or {}
        only = ((params.get("context") or {}).get("only")) or []
        out: list[dict] = []

        if not only or CODE_ACTION_QUICKFIX in only:
            for fx in self.fixes.get(uri, []):
                if not _ranges_touch(fx["range"], rng):
                    continue
                out.append({
                    "title": f"改成「{fx['newText']}」",
                    "kind": CODE_ACTION_QUICKFIX,
                    "isPreferred": True,
                    "edit": {"changes": {uri: [{"range": fx["range"],
                                                "newText": fx["newText"]}]}},
                })

        if not only or CODE_ACTION_SOURCE in only:
            if self.on_formatting({"textDocument": {"uri": uri}}):
                out.append({
                    "title": "格式化本文档",
                    "kind": CODE_ACTION_SOURCE,
                    "edit": {"changes": {uri: self.on_formatting(
                        {"textDocument": {"uri": uri}})}},
                })
        return out

    # -- 签名提示（M42.4）---------------------------------------------------

    def on_signature_help(self, params: dict) -> "Optional[dict]":
        uri = self._uri(params)
        doc = self.docs.get(uri)
        if doc is None:
            return None
        pos = params.get("position") or {}
        off = _pos_to_offset(doc["text"], pos.get("line", 0),
                            pos.get("character", 0))
        ctx = _call_context(doc["text"], off)
        if ctx is None:
            return None
        callee, active = ctx
        sig = self._signature_for(uri, callee)
        if sig is None:
            return None
        return {"signatures": [sig], "activeSignature": 0,
                "activeParameter": active}

    def _signature_for(self, uri: str, callee: str) -> "Optional[dict]":
        """给一个被调用的名字找签名。

        三类来源，优先级从具体到一般：标准库函数（`容器.计数`）→ 文档内
        用户函数 → 内建函数（从它的文档首行里抠参数表）。
        """
        lang = self.lang
        if "." in callee:
            base, _, member = callee.rpartition(".")
            for f in lang.stdlib.get(base, []):
                if f["name"] == member:
                    return _sig_from_text(f.get("sig") or member,
                                          f.get("doc") or "")
            return None
        for sym in self.symbols.get(uri, []):
            if sym["name"] == callee and sym["kind"] == SYM_FUNCTION:
                node = sym["node"]
                params = list(getattr(node, "params", []) or [])
                return _sig_from_params(callee, params, sym.get("detail", ""))
        # 文档此刻解析不了（括号还没闭合——正是最需要签名提示的时候），
        # 就从 token 流里把 `函数 名字(甲, 乙)` 扫出来。见 `_params_from_tokens`。
        params = self._params_from_tokens(uri, callee)
        if params:
            return _sig_from_params(callee, params)
        if callee in lang.builtins:
            return _sig_from_text(lang.builtins[callee].split("\n")[0],
                                  lang.builtins[callee])
        return None

    def _params_from_tokens(self, uri: str, callee: str) -> "Optional[list[str]]":
        """从 token 流里扫出某个用户函数的形参表（AST 不可用时的兜底）。

        **为什么值得单独写一条**：签名提示最常用的时刻恰恰是「正在写这个调用，
        括号还没配上」——那会儿整篇文档解析不过，AST 根本不存在。但词法层是
        好的，`函数 甲(参数1, 参数2)：` 的 token 序列照样完整可读。
        """
        doc = self.docs.get(uri)
        if doc is None:
            return None
        inc = doc.get("inc")
        try:
            tokens = list(inc.tokens if inc is not None
                          else tokenize(doc.get("text") or "", uri))
        except JishiError:
            return None
        for i, t in enumerate(tokens):
            if t.type != "KEYWORD" or t.value != "函数":
                continue
            # 期望：函数 NAME ( …
            if i + 1 >= len(tokens) or tokens[i + 1].type != "NAME":
                continue
            if str(tokens[i + 1].value) != callee:
                continue
            j = i + 2
            if j >= len(tokens) or tokens[j].type != "OP" or tokens[j].value != "(":
                continue
            j += 1
            params: list[str] = []
            星 = ""
            while j < len(tokens):
                tk = tokens[j]
                if tk.type == "OP" and tk.value == ")":
                    return params
                if tk.type == "OP" and tk.value in ("*", "**"):
                    星 = str(tk.value)
                    j += 1
                    continue
                if tk.type == "OP" and tk.value == ",":
                    星 = ""
                    j += 1
                    continue
                if tk.type == "NAME":
                    params.append(星 + str(tk.value))
                    星 = ""
                j += 1
            return params
        return None

    # -- 工作区符号与跨文件跳转（M42.5）--------------------------------------

    def on_workspace_symbol(self, params: dict) -> list:
        """全工作区搜符号（VS Code 的「转到工作区中的符号」）。

        来源两块：**已打开**的文档（内存里的符号表，最新）+ **磁盘上**的
        .jsh 文件（按 mtime 缓存的索引）。查询按「子串、不区分大小写」匹配，
        结果封顶 200 条——工作区符号是给人翻的列表，不是数据导出。
        """
        query = (params.get("query") or "").strip().lower()
        out: list[dict] = []
        for uri, syms in self.symbols.items():
            for sym in syms:
                out.append(self._workspace_symbol_item(sym, uri))
        for path, info in self._workspace_index().items():
            if _path_to_uri(path) in self.docs:
                continue                     # 已打开的用内存里那份
            for sym in info.get("symbols", []):
                out.append(self._workspace_symbol_item(sym, _path_to_uri(path)))
        if query:
            out = [s for s in out if query in s["name"].lower()]
        return out[:200]

    @staticmethod
    def _workspace_symbol_item(sym: dict, uri: str) -> dict:
        node = sym["node"]
        return {
            "name": sym["name"],
            "kind": sym["kind"],
            "containerName": sym.get("container") or "",
            "location": {
                "uri": uri,
                "range": {"start": _pos(node.line - 1, node.col - 1),
                          "end": _pos(node.line - 1,
                                      node.col - 1 + len(sym["name"]))},
            },
        }

    def _workspace_index(self) -> dict[str, dict]:
        """工作区里的 .jsh 索引：路径 → {mtime, symbols}（按 mtime 失效）。

        有意的封顶与跳过：文件数上限 400、跳过隐藏目录与 `.jishi`（第三方包
        缓存）、单文件超过 512KB 不读。索引是为了「跳转/搜索好用」，
        不是为了建全项目数据库——真做那个得上后台任务，不该卡住编辑器。
        """
        if self.root is None or not self.root.is_dir():
            return {}
        out: dict[str, dict] = {}
        count = 0
        for p in sorted(self.root.rglob("*.jsh")):
            if count >= 400:
                break
            parts = set(p.parts)
            if ".jishi" in parts or "node_modules" in parts:
                continue
            if any(seg.startswith(".") and seg not in (".", "..")
                   for seg in p.relative_to(self.root).parts[:-1]):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > 512 * 1024:
                continue
            count += 1
            key = str(p)
            cached = self._file_cache.get(key)
            if cached is not None and cached.get("mtime") == st.st_mtime:
                out[key] = cached
                continue
            try:
                text = p.read_text(encoding="utf-8")
                program = parse(tokenize(text, str(p)), text.split("\n"),
                                str(p))
                syms = _collect_symbols(program)
            except (JishiError, OSError, UnicodeDecodeError):
                syms = []
            info = {"mtime": st.st_mtime, "symbols": syms}
            self._file_cache[key] = info
            out[key] = info
        return out

    def _cross_file_definition(self, uri: str, word: str) -> "Optional[dict]":
        """本地找不到定义时，去工作区别的文件里找。

        **只在「唯一命中」时才跳**：同名符号出现在多个文件里（比如每个文件
        都有个 `主程序`）跳过去等于误导，这时宁可不跳——「跳错地方」比
        「跳不过去」更费时间。
        """
        hits: list[tuple[Path, dict]] = []
        for path, info in self._workspace_index().items():
            if _path_to_uri(path) == uri:
                continue
            for sym in info.get("symbols", []):
                # 只要模块级符号：函数内的同名局部变量不算「跨文件定义」
                if sym["name"] == word and sym.get("container") is None:
                    hits.append((Path(path), sym))
                    break
        if len(hits) != 1:
            return None
        path, sym = hits[0]
        node = sym["node"]
        return {
            "uri": _path_to_uri(path),
            "range": {"start": _pos(node.line - 1, node.col - 1),
                      "end": _pos(node.line - 1,
                                  node.col - 1 + len(sym["name"]))},
        }

    # -- 语义高亮（M42.6）---------------------------------------------------

    def on_semantic_tokens_full(self, params: dict) -> dict:
        """整篇语义高亮（差量编码：每项 5 个整数）。

        只上「语法文件看不出来」的色：关键字 / 内建与标准库函数（标
        `defaultLibrary`）/ 标准库模块 / 用户变量与函数。字符串、数字、注释
        交给扩展自带的 TextMate 语法——语义层再上一遍只会跟主题打架。
        """
        uri = self._uri(params)
        doc = self.docs.get(uri)
        if doc is None:
            return {"data": []}
        return {"data": self._semantic_data(uri, doc)}

    def _semantic_data(self, uri: str, doc: dict) -> list[int]:
        text = doc.get("text") or ""
        lines = text.split("\n")
        inc = doc.get("inc")
        tokens = inc.tokens if inc is not None else tokenize(text, uri)
        lang = self.lang
        program = self.programs.get(uri)

        params: set[str] = set()
        funcs: set[str] = set()
        classes: set[str] = set()
        modules: set[str] = set()
        variables: set[str] = set()
        for sym in self.symbols.get(uri, []):
            node = sym["node"]
            if isinstance(node, A.FuncDef):
                params.update(p for p in (node.params or []) if p)
            if sym["kind"] == SYM_FUNCTION:
                funcs.add(sym["name"])
            elif sym["kind"] == SYM_CLASS:
                classes.add(sym["name"])
            elif sym["kind"] == SYM_MODULE:
                modules.add(sym["name"])
            else:
                variables.add(sym["name"])
        binds = _binding_positions(program) if program is not None else set()
        binds |= _param_positions(tokens)     # 形参的定义处（AST 里没有位置）

        raw: list[tuple[int, int, int, int, int]] = []
        prev_op: "Optional[str]" = None
        prev_kw: "Optional[str]" = None
        for t in tokens:
            if t.type == "OP":
                prev_op = str(t.value)
                prev_kw = None
                continue
            if t.type != "NAME" and t.type != "KEYWORD":
                prev_op = None
                prev_kw = None
                continue
            name = str(t.value)
            类型: "Optional[str]" = None
            修饰 = 0
            if t.type == "KEYWORD":
                类型 = "keyword"
                if name in _LITERAL_KEYWORDS:
                    修饰 |= _SEM_MOD["readonly"]
                prev_kw = name
            else:
                if prev_op == ".":
                    类型 = "property"
                elif name in _LITERAL_KEYWORDS:
                    类型 = "keyword"
                    修饰 |= _SEM_MOD["readonly"]
                elif name in lang.keywords:
                    类型 = "keyword"
                # **用户绑定的名字优先于标准库/内建**（M42）：`令 参数 = 1`
                # 之后 `参数` 指的是那个变量，不是标准库模块。而且 M41 新增的
                # 模块名（容器/迭代/参数/日志/测试）**都是常用词**，当变量名
                # 的概率不低——顺序反了就会把用户变量上成模块色。
                elif name in params:
                    类型 = "parameter"
                elif name in funcs or name in classes:
                    类型 = "function"
                elif name in modules:
                    类型 = "namespace"
                    if name in lang.stdlib:
                        # `导入 容器` 之后 `容器` 既是用户绑定的名字、也确实是
                        # 标准库模块——标上 defaultLibrary，主题才好区分
                        # 「自己的模块」和「标准库」。
                        修饰 |= _SEM_MOD["defaultLibrary"]
                elif name in variables:
                    类型 = "variable"
                elif name in lang.stdlib:
                    类型 = "namespace"
                    修饰 |= _SEM_MOD["defaultLibrary"]
                elif name in lang.builtins:
                    类型 = "function"
                    修饰 |= _SEM_MOD["defaultLibrary"]
                else:
                    # 未定义的名字也按变量上色：语义高亮管的是「这是什么」，
                    # 「对不对」由诊断说。
                    类型 = "variable"
                if prev_kw in ("函数", "类"):
                    修饰 |= _SEM_MOD["declaration"]
                if (t.line, t.col) in binds:
                    修饰 |= _SEM_MOD["declaration"]
                prev_kw = None
            prev_op = None
            if 类型 is None:
                continue
            line_text = lines[t.line - 1] if 0 < t.line <= len(lines) else ""
            start = _utf16_col(line_text, t.col - 1)
            end = _utf16_col(line_text, t.end_col)
            length = end - start
            if length <= 0:
                continue                 # 空 token 会让客户端解码错位，丢掉
            raw.append((t.line - 1, start, length, _SEM_TYPE[类型], 修饰))

        raw.sort(key=lambda x: (x[0], x[1]))
        data: list[int] = []
        prev_line = 0
        prev_char = 0
        for (line, char, length, ttype, mods) in raw:
            # 差量编码（LSP 规范）：`deltaLine > 0` 时 **`deltaStartCharacter`
            # 是绝对值**（相对行首），只有同一行内才是相对上一个 token。
            # 混了的话客户端会解出错位的位置——而且只在跨行时错（M42 踩到）。
            if line == prev_line:
                dchar = char - prev_char
            else:
                dchar = char
            data.extend([line - prev_line, dchar, length, ttype, mods])
            prev_line, prev_char = line, char
        return data

    # -- 方法表 -------------------------------------------------------------

    HANDLERS: dict = {}


LspServer.HANDLERS = {
    "initialize": LspServer.on_initialize,
    "initialized": lambda self, p: None,
    "shutdown": LspServer.on_shutdown,
    "exit": LspServer.on_exit,
    "textDocument/didOpen": LspServer.on_did_open,
    "textDocument/didChange": LspServer.on_did_change,
    "textDocument/didClose": LspServer.on_did_close,
    "textDocument/didSave": LspServer.on_did_save,
    "textDocument/completion": LspServer.on_completion,
    "textDocument/hover": LspServer.on_hover,
    "textDocument/definition": LspServer.on_definition,
    "textDocument/documentSymbol": LspServer.on_document_symbol,
    # --- M42 新增的六项 ---
    "textDocument/formatting": LspServer.on_formatting,
    "textDocument/prepareRename": LspServer.on_prepare_rename,
    "textDocument/rename": LspServer.on_rename,
    "textDocument/codeAction": LspServer.on_code_action,
    "textDocument/signatureHelp": LspServer.on_signature_help,
    "workspace/symbol": LspServer.on_workspace_symbol,
    "textDocument/semanticTokens/full": LspServer.on_semantic_tokens_full,
}


def main(argv: "Optional[list[str]]" = None) -> int:
    """`jishi lsp` 入口：在 stdio 上跑语言服务器。"""
    server = LspServer()
    return server.serve()


if __name__ == "__main__":
    sys.exit(main())
