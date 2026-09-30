# -*- coding: utf-8 -*-
"""M42 语言服务补能力：格式化 / 重命名 / 快速修复 / 签名提示 / 工作区符号 / 语义高亮。

六项能力**每项都有协议级测试**（喂 JSON-RPC 参数、断言响应结构），
风格与 `tests/test_m21_lsp.py` 一致：直接调 `LspServer.dispatch`，不走 stdio。

几条测试专门盯「容易错但不容易发现」的地方，都是这轮真踩到的：

- **语义高亮的差量编码**：`deltaLine > 0` 时 `deltaStartCharacter` 必须是
  **绝对值**（相对行首），只有同一行内才是相对量。混了的话客户端只在**跨行**
  时解错位——单行测试永远发现不了。
- **UTF-16 位置单位**：词法器给的行列按 Python 字符数算，而 LSP 默认 UTF-16
  码元；emoji 这类天文平面字符占 2 个码元，不换算就会偏。
- **用户绑定名优先于标准库名**：M41 新增的模块名（`容器`/`迭代`/`参数`/`日志`/
  `测试`）**都是常用词**，`令 参数 = 1` 之后 `参数` 指的是变量而不是模块。
- **签名提示要在「文档解析不了」时也能用**：正在敲调用、括号还没配上，
  那正是最需要它的时候——所以另有一条 token 扫描的兜底路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi.lsp import (                              # noqa: E402
    LspServer,
    SEMANTIC_MODIFIERS,
    SEMANTIC_TYPES,
)

ROOT = Path(__file__).resolve().parents[1]
URI = "file:///测试.jsh"


def _server(root: "Path | None" = None) -> LspServer:
    srv = LspServer()
    srv._write = lambda obj: None
    if root is not None:
        srv.root = root
    return srv


def _open(srv: LspServer, text: str, uri: str = URI, version: int = 1) -> None:
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                  "params": {"textDocument": {"uri": uri, "version": version,
                                              "text": text}}})


def _at(line: int, char: int) -> dict:
    return {"textDocument": {"uri": URI},
            "position": {"line": line, "character": char}}


def _decode_tokens(data: list[int]) -> list[tuple[int, int, int, str, list[str]]]:
    """把差量编码的语义高亮解回 `(行, 列, 长度, 类型, 修饰)`。

    这里**按 LSP 规范**解：`deltaLine > 0` 时列是绝对值。
    """
    out = []
    line = 0
    char = 0
    for i in range(0, len(data), 5):
        dl, dc, length, ttype, mods = data[i:i + 5]
        line += dl
        char = dc if dl else char + dc
        修饰 = [SEMANTIC_MODIFIERS[k] for k in range(len(SEMANTIC_MODIFIERS))
                if mods & (1 << k)]
        out.append((line, char, length, SEMANTIC_TYPES[ttype], 修饰))
    return out


# ---------------------------------------------------------------------------
# 能力声明
# ---------------------------------------------------------------------------

def test_initialize_declares_all_six_capabilities():
    caps = _server().on_initialize({})["capabilities"]
    assert caps["documentFormattingProvider"] is True
    assert caps["renameProvider"] == {"prepareProvider": True}
    assert caps["codeActionProvider"]["codeActionKinds"] == ["quickfix",
                                                             "source.format"]
    assert caps["signatureHelpProvider"]["triggerCharacters"] == ["(", "，", ","]
    assert caps["workspaceSymbolProvider"] is True
    legend = caps["semanticTokensProvider"]["legend"]
    assert legend["tokenTypes"] == list(SEMANTIC_TYPES)
    assert legend["tokenModifiers"] == list(SEMANTIC_MODIFIERS)
    assert caps["semanticTokensProvider"]["full"] is True


def test_m42_methods_are_registered():
    """六项能力的方法名都要在派发表里（漏注册在编辑器里就是「没反应」）。"""
    h = LspServer.HANDLERS
    for m in ("textDocument/formatting", "textDocument/prepareRename",
              "textDocument/rename", "textDocument/codeAction",
              "textDocument/signatureHelp", "workspace/symbol",
              "textDocument/semanticTokens/full"):
        assert m in h, f"没注册 {m}"


def test_initialize_reads_workspace_root(tmp_path):
    srv = LspServer()
    srv._write = lambda obj: None
    srv.on_initialize({"rootUri": "file:///" + tmp_path.as_posix()})
    assert srv.root is not None
    assert srv.root.name == tmp_path.name
    srv2 = LspServer()
    srv2._write = lambda obj: None
    srv2.on_initialize({"workspaceFolders": [
        {"uri": "file:///" + tmp_path.as_posix()}]})
    assert srv2.root is not None


# ---------------------------------------------------------------------------
# ① 格式化
# ---------------------------------------------------------------------------

def test_formatting_returns_whole_doc_edit():
    srv = _server()
    _open(srv, "函数 甲(a):\n  返回 a\n令 x=1\n")
    edits = srv.on_formatting({"textDocument": {"uri": URI}})
    assert len(edits) == 1
    e = edits[0]
    # 全文替换：从 (0,0) 起
    assert e["range"]["start"] == {"line": 0, "character": 0}
    # 缩进补成 4 空格、赋值两边有空格
    assert e["newText"] == "函数 甲(a):\n    返回 a\n令 x = 1\n"


def test_formatting_returns_nothing_when_already_formatted():
    srv = _server()
    _open(srv, "令 甲 = 1\n打印(甲)\n")
    assert srv.on_formatting({"textDocument": {"uri": URI}}) == []


def test_formatting_does_not_touch_broken_source():
    """语法有错时什么都不做——正在打字，别把半截代码搅乱。"""
    srv = _server()
    _open(srv, "函数 甲(:\n  返回\n")
    assert srv.on_formatting({"textDocument": {"uri": URI}}) == []


def test_formatting_unknown_doc_is_empty():
    assert _server().on_formatting({"textDocument": {"uri": URI}}) == []


# ---------------------------------------------------------------------------
# ② 重命名
# ---------------------------------------------------------------------------

_RENAME_SRC = (
    "函数 求平均(分数们)：\n"
    "    令 总数 = 0\n"
    "    返回 总数\n"
    "\n"
    "令 成绩 = [85, 92]\n"
    "打印(求平均(成绩))\n"
)


def test_prepare_rename_on_module_level_function():
    srv = _server()
    _open(srv, _RENAME_SRC)
    r = srv.on_prepare_rename(_at(0, 3))          # 光标在「求平均」上
    assert r is not None
    assert r["placeholder"] == "求平均"
    assert r["range"]["start"] == {"line": 0, "character": 3}


def test_prepare_rename_refuses_builtin_keyword_and_stdlib():
    srv = _server()
    _open(srv, "导入 容器\n打印(容器.计数([1, 2]))\n令 甲 = 真\n")
    # 内建 `打印`
    assert srv.on_prepare_rename(_at(1, 1)) is None
    # 标准库模块 `容器`
    assert srv.on_prepare_rename(_at(0, 4)) is None
    # 值关键字 `真`
    assert srv.on_prepare_rename(_at(2, 7)) is None


def test_prepare_rename_refuses_parameter_and_local():
    """形参与函数内局部变量**不支持**改名（没有作用域分析，改名会改坏代码）。

    这是有意为之的边界，不是漏做：宁可明确说不支持，也不要给一个错的编辑。
    """
    srv = _server()
    _open(srv, _RENAME_SRC)
    assert srv.on_prepare_rename(_at(0, 8)) is None      # 形参「分数们」
    assert srv.on_prepare_rename(_at(1, 7)) is None      # 局部变量「总数」


def test_rename_edits_definition_and_all_uses():
    srv = _server()
    _open(srv, _RENAME_SRC)
    out = srv.on_rename({**_at(0, 3), "newName": "算平均"})
    edits = out["changes"][URI]
    位置 = sorted((e["range"]["start"]["line"],
                   e["range"]["start"]["character"]) for e in edits)
    # 定义处（第 0 行第 3 列）+ 调用处（第 5 行第 3 列）
    assert 位置 == [(0, 3), (5, 3)]
    assert all(e["newText"] == "算平均" for e in edits)


def test_rename_top_level_variable():
    srv = _server()
    _open(srv, _RENAME_SRC)
    out = srv.on_rename({**_at(4, 2), "newName": "分数表"})
    行 = sorted(e["range"]["start"]["line"] for e in out["changes"][URI])
    assert 行 == [4, 5]


def test_rename_rejects_invalid_new_name():
    srv = _server()
    _open(srv, _RENAME_SRC)
    for bad in ("1甲", "甲 乙", "", "甲-乙"):
        assert srv.on_rename({**_at(0, 3), "newName": bad}) is None


def test_rename_rejects_unknown_and_local():
    srv = _server()
    _open(srv, _RENAME_SRC)
    assert srv.on_rename({**_at(0, 8), "newName": "别的"}) is None   # 形参
    assert srv.on_rename({**_at(3, 0), "newName": "别的"}) is None   # 空行


# ---------------------------------------------------------------------------
# ③ 快速修复
# ---------------------------------------------------------------------------

def test_code_action_offers_fix_for_english_keyword():
    srv = _server()
    _open(srv, "if 真：\n    打印(1)\n")
    assert srv.fixes[URI], "英文关键字应当带出修法建议"
    fx = srv.fixes[URI][0]
    assert fx["title"] == "改成「如果」"
    actions = srv.on_code_action({
        "textDocument": {"uri": URI}, "range": fx["range"]})
    assert actions[0]["title"] == "改成「如果」"
    assert actions[0]["kind"] == "quickfix"
    assert actions[0]["isPreferred"] is True
    edit = actions[0]["edit"]["changes"][URI][0]
    assert edit["newText"] == "如果"
    assert edit["range"] == fx["range"]


def test_code_action_offers_fix_for_python_builtin():
    srv = _server()
    _open(srv, "打印(len([1, 2]))\n")
    actions = srv.on_code_action({
        "textDocument": {"uri": URI}, "range": srv.fixes[URI][0]["range"]})
    assert actions[0]["edit"]["changes"][URI][0]["newText"] == "长度"


def test_code_action_includes_format_action():
    """格式化属于 `source.format` 类，和修错不在一个菜单里，互不干扰。"""
    srv = _server()
    _open(srv, "令 甲=1\n")
    kinds = {a["kind"] for a in srv.on_code_action(
        {"textDocument": {"uri": URI}, "range": {}})}
    assert "source.format" in kinds
    assert "quickfix" not in kinds          # 这份文档没有可修的错误


def test_code_action_respects_only_filter():
    srv = _server()
    # 故意用 2 空格缩进：这样「格式化」才真的有改动可给
    _open(srv, "if 真：\n  打印(1)\n")
    rng = srv.fixes[URI][0]["range"]
    only_qf = srv.on_code_action({"textDocument": {"uri": URI}, "range": rng,
                                 "context": {"only": ["quickfix"]}})
    assert [a["kind"] for a in only_qf] == ["quickfix"]
    only_fmt = srv.on_code_action({"textDocument": {"uri": URI}, "range": rng,
                                  "context": {"only": ["source.format"]}})
    assert [a["kind"] for a in only_fmt] == ["source.format"]


def test_code_action_empty_when_range_missess_everything():
    srv = _server()
    _open(srv, "if 真：\n    打印(1)\n")
    # 光标在第 1 行（诊断在第 0 行）——不挨着，不给修复
    actions = srv.on_code_action({"textDocument": {"uri": URI},
                                  "range": {"start": {"line": 1, "character": 0},
                                            "end": {"line": 1, "character": 0}}})
    assert [a["kind"] for a in actions] == []


def test_code_action_unknown_doc_is_empty():
    assert _server().on_code_action({"textDocument": {"uri": URI},
                                     "range": {}}) == []


# ---------------------------------------------------------------------------
# ④ 签名提示
# ---------------------------------------------------------------------------

def test_signature_help_user_function():
    srv = _server()
    _open(srv, "函数 甲(参数1, 参数2):\n    返回 参数1\n甲(1, 2)\n")
    r = srv.on_signature_help(_at(2, 4))         # 光标在第一个实参之后
    assert r["signatures"][0]["label"] == "甲(参数1, 参数2)"
    assert r["activeParameter"] == 1
    assert r["activeSignature"] == 0


def test_signature_help_works_when_document_does_not_parse():
    """括号还没闭合——正是最需要签名提示的时候（走 token 兜底）。"""
    srv = _server()
    _open(srv, "函数 甲(参数1, 参数2):\n    返回 参数1\n甲(1, \n")
    assert srv.programs[URI] is None, "这份文档本来就解析不过"
    r = srv.on_signature_help(_at(2, 5))
    assert r is not None
    assert r["signatures"][0]["label"] == "甲(参数1, 参数2)"


def test_signature_help_stdlib_method():
    srv = _server()
    _open(srv, "导入 容器\n容器.计数([1, 2])\n")
    r = srv.on_signature_help(_at(1, 10))
    assert r["signatures"][0]["label"] == "计数(可迭代)"
    assert r["activeParameter"] == 0


def test_signature_help_counts_commas_outside_nested_literals():
    """实参里有列表时，里面的逗号不能算成「第几个实参」。"""
    srv = _server()
    _open(srv, "打印(配对([1, 2], [3, 4]), \"x\")\n")
    outer = srv.on_signature_help(_at(0, 19))    # 第二个实参处
    assert outer["signatures"][0]["label"].startswith("配对(")
    assert outer["activeParameter"] == 1
    inner = srv.on_signature_help(_at(0, 8))     # 第一个实参里（列表内部）
    assert inner["activeParameter"] == 0


def test_signature_help_none_outside_call():
    srv = _server()
    _open(srv, "令 甲 = 1\n")
    assert srv.on_signature_help(_at(0, 3)) is None


def test_signature_help_none_for_unknown_callee():
    srv = _server()
    _open(srv, "随便写个名字(1)\n")
    assert srv.on_signature_help(_at(0, 8)) is None


# ---------------------------------------------------------------------------
# ⑤ 工作区符号 + 跨文件跳转
# ---------------------------------------------------------------------------

def _工作区(tmp_path: Path) -> Path:
    (tmp_path / "工具.jsh").write_text(
        "函数 算总和(数们)：\n    返回 1\n", encoding="utf-8")
    (tmp_path / "另一个.jsh").write_text(
        "函数 打招呼()：\n    返回 2\n", encoding="utf-8")
    return tmp_path


def test_workspace_symbol_lists_files_on_disk(tmp_path):
    srv = _server(_工作区(tmp_path))
    names = [s["name"] for s in srv.on_workspace_symbol({"query": ""})]
    assert "算总和" in names and "打招呼" in names


def test_workspace_symbol_filters_by_query(tmp_path):
    srv = _server(_工作区(tmp_path))
    names = [s["name"] for s in srv.on_workspace_symbol({"query": "总和"})]
    assert names == ["算总和"]


def test_workspace_symbol_includes_open_docs(tmp_path):
    srv = _server(_工作区(tmp_path))
    _open(srv, "函数 只在内存里()：\n    返回 1\n")
    names = [s["name"] for s in srv.on_workspace_symbol({"query": ""})]
    assert "只在内存里" in names


def test_workspace_symbol_carries_location(tmp_path):
    srv = _server(_工作区(tmp_path))
    item = [s for s in srv.on_workspace_symbol({"query": "打招呼"})][0]
    assert item["location"]["uri"].endswith(".jsh")
    assert item["location"]["range"]["start"] == {"line": 0, "character": 0}


def test_workspace_symbol_empty_without_root():
    srv = _server()
    srv.root = None
    assert srv.on_workspace_symbol({"query": ""}) == []


def test_cross_file_definition_jumps_on_unique_hit(tmp_path):
    srv = _server(_工作区(tmp_path))
    d = srv._cross_file_definition("file:///别处.jsh", "打招呼")
    assert d is not None
    assert d["uri"].endswith("%E5%8F%A6%E4%B8%80%E4%B8%AA.jsh") or \
        d["uri"].endswith("另一个.jsh")


def test_cross_file_definition_gives_up_when_ambiguous(tmp_path):
    """同名符号在多个文件里都有 → 不跳。跳错地方比跳不过去更费时间。"""
    (tmp_path / "甲.jsh").write_text("函数 主程序()：\n    返回 1\n",
                                     encoding="utf-8")
    (tmp_path / "乙.jsh").write_text("函数 主程序()：\n    返回 2\n",
                                     encoding="utf-8")
    srv = _server(tmp_path)
    assert srv._cross_file_definition("file:///别处.jsh", "主程序") is None


def test_definition_prefers_local_over_cross_file(tmp_path):
    _工作区(tmp_path)
    srv = _server(tmp_path)
    _open(srv, "函数 打招呼()：\n    返回 9\n打招呼()\n")
    d = srv.on_definition(_at(2, 1))
    assert d["uri"] == URI               # 本地命中，不去翻磁盘


def test_definition_falls_back_to_other_file(tmp_path):
    _工作区(tmp_path)
    srv = _server(tmp_path)
    _open(srv, "打印(打招呼())\n")
    d = srv.on_definition(_at(0, 4))
    assert d is not None and d["uri"] != URI


def test_workspace_index_ignores_jishi_and_hidden_dirs(tmp_path):
    (tmp_path / ".jishi" / "packages").mkdir(parents=True)
    (tmp_path / ".jishi" / "packages" / "依赖.jsh").write_text(
        "函数 不该出现()：\n    返回 1\n", encoding="utf-8")
    (tmp_path / "正常.jsh").write_text("函数 该出现()：\n    返回 1\n",
                                      encoding="utf-8")
    srv = _server(tmp_path)
    names = [s["name"] for s in srv.on_workspace_symbol({"query": ""})]
    assert "该出现" in names and "不该出现" not in names


# ---------------------------------------------------------------------------
# ⑥ 语义高亮
# ---------------------------------------------------------------------------

_SEM_SRC = (
    "导入 容器\n"
    "函数 甲(参数):\n"
    "    令 结果 = 容器.计数([1, 2])\n"
    "    返回 结果\n"
)


def _tokens_of(srv: LspServer, uri: str = URI) -> list:
    data = srv.on_semantic_tokens_full({"textDocument": {"uri": uri}})["data"]
    assert len(data) % 5 == 0, "差量数据必须是 5 的倍数"
    return _decode_tokens(data)


def test_semantic_tokens_are_sorted_and_non_overlapping():
    srv = _server()
    _open(srv, _SEM_SRC)
    toks = _tokens_of(srv)
    位置 = [(t[0], t[1]) for t in toks]
    assert 位置 == sorted(位置), "token 必须按位置递增（客户端靠差量解码）"
    assert len(set(位置)) == len(位置), "同一位置不能重复上色"


def test_semantic_tokens_classify_keyword_module_function_parameter():
    srv = _server()
    _open(srv, _SEM_SRC)
    表 = {t[1]: t for t in _tokens_of(srv) if t[0] == 1}
    assert 表[0][3] == "keyword"                       # 函数
    assert 表[3][3] == "function"                      # 甲
    assert "declaration" in 表[3][4]
    assert "declaration" in 表[5][4]                    # 形参 参数
    assert 表[5][3] == "parameter"
    # 第 2 行的 `容器` 是标准库模块
    行2 = {t[1]: t for t in _tokens_of(srv) if t[0] == 2}
    assert 行2[11][3] == "namespace"
    assert "defaultLibrary" in 行2[11][4]
    assert 行2[14][3] == "property"                     # `.计数`


def test_semantic_tokens_treat_shadowed_stdlib_name_as_user_binding():
    """`令 参数 = 1` 之后 `参数` 是变量，不是标准库模块。

    M41 加的模块名（容器/迭代/参数/日志/测试）都是常用词，顺序反了就会把
    用户变量上成模块色——这条是那次的回归钉子。
    """
    srv = _server()
    _open(srv, "令 参数 = 1\n打印(参数)\n")
    toks = [t for t in _tokens_of(srv) if t[2] == 2]     # 长度为 2 的 token
    参数们 = [t for t in toks if t[3] in ("variable", "namespace")]
    assert 参数们, "应当有「参数」这个 token"
    assert all(t[3] == "variable" for t in 参数们), \
        f"「参数」被当成了标准库模块：{参数们}"


def test_semantic_tokens_utf16_columns_after_emoji():
    """emoji 占 2 个 UTF-16 码元，它后面的名字列号要按码元算。"""
    srv = _server()
    src = '打印("🎉", 甲)\n'
    _open(srv, "令 甲 = 1\n" + src)
    行1 = [t for t in _tokens_of(srv) if t[0] == 1]
    甲 = [t for t in 行1 if t[2] == 1 and t[1] > 5]
    assert 甲, f"没找到「甲」的 token：{行1}"
    # 字符位置是 9（打0 印1 (2 "3 🎉4 "5 ,6 空7 甲8 → 等等，见下），
    # 而 UTF-16 位置比它多 1（🎉 占两个码元）
    assert 甲[0][1] == 9, f"列号没按 UTF-16 换：{甲[0]}"


def test_semantic_tokens_empty_for_unknown_doc():
    assert _server().on_semantic_tokens_full(
        {"textDocument": {"uri": URI}}) == {"data": []}


def test_semantic_tokens_survive_broken_source():
    """文档解析不了时仍要能上色（词法层是好的，只有语法不完整）。"""
    srv = _server()
    _open(srv, "函数 甲(参数):\n    返回 参数\n甲(1, \n")
    toks = _tokens_of(srv)
    assert toks, "解析失败也应当按词法给高亮"


# ---------------------------------------------------------------------------
# 与既有能力的一致性
# ---------------------------------------------------------------------------

def test_close_clears_new_state():
    srv = _server()
    _open(srv, "if 真：\n    打印(1)\n")
    assert srv.fixes[URI]
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didClose",
                  "params": {"textDocument": {"uri": URI}}})
    assert URI not in srv.fixes
    assert URI not in srv.programs


def test_new_handlers_never_raise_on_garbage_params():
    """客户端传空参数/错参数时不能崩（崩了整条语言服务就断了）。"""
    srv = _server()
    _open(srv, _SEM_SRC)
    for method, params in (
        ("textDocument/formatting", {}),
        ("textDocument/prepareRename", {}),
        ("textDocument/rename", {"newName": "x"}),
        ("textDocument/codeAction", {}),
        ("textDocument/signatureHelp", {}),
        ("workspace/symbol", {}),
        ("textDocument/semanticTokens/full", {}),
    ):
        sent = []
        srv._write = sent.append
        srv.dispatch({"jsonrpc": "2.0", "id": 1, "method": method,
                      "params": params})
        assert sent and "error" not in sent[-1], f"{method} 崩了：{sent[-1:]}"
