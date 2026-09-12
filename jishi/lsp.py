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
"""

from __future__ import annotations

import dataclasses
import json
import sys
from typing import Any, Optional

from . import ast_nodes as A
from .errors import JishiError
from .parser import parse
from .tokenizer import tokenize
from .version import VERSION

# --- JSON-RPC 错误码 ---
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

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


# ---------------------------------------------------------------------------
# AST 遍历工具
# ---------------------------------------------------------------------------

def _flatten(value: Any):
    """把任意字段值里的 AST 节点摊平出来（支持列表/元组嵌套）。"""
    if isinstance(value, A.Node):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item)


def _child_nodes(node: Any):
    """遍历一个 AST 节点的直接子节点。"""
    if not dataclasses.is_dataclass(node):
        return
    for f in dataclasses.fields(node):
        try:
            value = getattr(node, f.name)
        except AttributeError:
            continue
        yield from _flatten(value)


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


def _defined_names(program: A.Program) -> set[str]:
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
            for n in _flatten(getattr(node, "target", None)):
                if isinstance(n, A.Name):
                    names.add(n.id)
        for child in _child_nodes(node):
            walk(child)

    walk(program)
    return names


def _binding_marks(program: A.Program) -> set[int]:
    """标出处于「绑定位置」的 Name 节点（这些不算使用）。"""
    marks: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, (A.Assign, A.For, A.Comprehension)):
            for n in _flatten(getattr(node, "target", None)):
                if isinstance(n, A.Name):
                    marks.add(id(n))
        for child in _child_nodes(node):
            walk(child)

    walk(program)
    return marks


def _load_names(program: A.Program, marks: set[int]) -> list[A.Name]:
    """文档里所有「读取使用」的名字节点。"""
    out: list[A.Name] = []

    def walk(node: Any) -> None:
        if isinstance(node, A.Name) and id(node) not in marks:
            out.append(node)
        for child in _child_nodes(node):
            walk(child)

    walk(program)
    return out


def _attr_base_marks(program: A.Program) -> set[int]:
    """标出「属性基址」的 Name 节点（`基础库.平方` 里的 `基础库`）。

    这类名字可能来自包依赖注入（见 `包.json` 的依赖项）或 Python 桥接，
    文件内无法静态得知，因此不做「未定义」判断，避免误报。
    """
    marks: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, A.Attr):
            obj = getattr(node, "obj", None)
            if isinstance(obj, A.Name):
                marks.add(id(obj))
        for child in _child_nodes(node):
            walk(child)

    walk(program)
    return marks


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


def _offset_to_pos(text: str, offset: int) -> dict:
    """字符偏移 → (行, 列)，均 0 基。"""
    offset = max(0, min(offset, len(text)))
    line = text.count("\n", 0, offset)
    last_nl = text.rfind("\n", 0, offset)
    char = offset - (last_nl + 1)
    return _pos(line, char)


def _pos_to_offset(text: str, line0: int, char0: int) -> int:
    """(行, 列) 0 基 → 字符偏移。"""
    lines = text.split("\n")
    line0 = max(0, min(line0, len(lines) - 1))
    offset = sum(len(l) + 1 for l in lines[:line0])
    return offset + max(0, char0)


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

class LangData:
    """从 ``ai.build_lang_spec()`` 提炼补全/悬停需要的数据。"""

    def __init__(self) -> None:
        from .ai import build_lang_spec

        spec = build_lang_spec()
        self.spec = spec
        self.keywords: dict[str, str] = dict(spec.get("keywords") or {})
        self.builtins: dict[str, str] = {
            b["name"]: b.get("doc", "")
            for b in (spec.get("builtins") or [])
        }
        self.stdlib: dict[str, list[dict]] = {
            m["module"]: list(m.get("functions") or [])
            for m in (spec.get("stdlib") or [])
        }
        self.methods: dict[str, list[str]] = dict(spec.get("methods") or {})
        self.exceptions: set[str] = set(spec.get("exceptions") or [])

    def all_methods(self) -> list[str]:
        out: list[str] = []
        for names in self.methods.values():
            for n in names:
                if n not in out:
                    out.append(n)
        return out


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
        return {
            "capabilities": {
                "textDocumentSync": 1,               # 1 = 全量同步
                "completionProvider": {"triggerCharacters": ["."]},
                "hoverProvider": True,
                "definitionProvider": True,
                "documentSymbolProvider": True,
            },
            "serverInfo": {"name": "jishi-lsp", "version": VERSION},
        }

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
        self.docs[uri] = {"text": doc.get("text") or "",
                          "version": doc.get("version")}
        self._analyze(uri)
        return None

    def on_did_change(self, params: dict) -> None:
        uri = self._uri(params)
        changes = params.get("contentChanges") or []
        if not changes:
            return None
        # textDocumentSync=1（全量）：最后一段就是完整文本
        self.docs[uri] = {"text": changes[-1].get("text") or "",
                          "version": (params.get("textDocument") or {}).get("version")}
        self._analyze(uri)
        return None

    def on_did_close(self, params: dict) -> None:
        uri = self._uri(params)
        self.docs.pop(uri, None)
        self.symbols.pop(uri, None)
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
        text = doc["text"]
        lines = text.split("\n")
        diags: list[dict] = []
        try:
            tokens = tokenize(text, uri)
            program = parse(tokens, lines, uri)
        except JishiError as e:
            diags.append({
                "range": _range_from_error(e),
                "severity": SEVERITY_ERROR,
                "code": e.code,
                "source": "jishi",
                "message": self._error_text(e),
            })
            self.symbols[uri] = []
        else:
            self.symbols[uri] = _collect_symbols(program)
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
        return None

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
}


def main(argv: "Optional[list[str]]" = None) -> int:
    """`jishi lsp` 入口：在 stdio 上跑语言服务器。"""
    server = LspServer()
    return server.serve()


if __name__ == "__main__":
    sys.exit(main())
