# -*- coding: utf-8 -*-
"""M21.2 语言服务器：诊断 / 补全 / 悬停 / 跳转 / stdio 分帧。

测试直接调用 ``LspServer.dispatch``（等价于收到一条 LSP 消息），
另有一个端到端用例走真实的 ``Content-Length`` 分帧，验证传输层。
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from jishi.lsp import LspServer                     # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
URI = "file:///测试.jsh"

SRC = (
    "函数 求平均(分数们)：\n"
    "    令 总数 = 0\n"
    "    遍历 分数 在 分数们：\n"
    "        总数 += 分数\n"
    "    返回 总数 / 长度(分数们)\n"
    "\n"
    "令 成绩 = [85, 92]\n"
    "打印(求平均(成绩))\n"
)


def _server() -> LspServer:
    srv = LspServer()
    srv._write = lambda obj: None          # 单测不需要真实输出
    return srv


def _open(srv: LspServer, text: str, version: int = 1) -> None:
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                  "params": {"textDocument": {"uri": URI, "version": version,
                                              "text": text}}})


def _frames(blob: bytes) -> list:
    out = []
    i = 0
    while i < len(blob):
        end = blob.find(b"\r\n\r\n", i)
        if end < 0:
            break
        length = int(blob[i:end].decode("ascii").split(":", 1)[1].strip())
        body = blob[end + 4:end + 4 + length]
        out.append(json.loads(body.decode("utf-8")))
        i = end + 4 + length
    return out


def _frame(obj: dict) -> bytes:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data


# ---------------------------------------------------------------------------
# 生命周期与能力
# ---------------------------------------------------------------------------

def test_initialize_capabilities():
    srv = _server()
    caps = srv.on_initialize({})["capabilities"]
    # M38 B2：改成**增量同步**（change = 2）——客户端只发改动的那一段，
    # 服务端按行复用前缀重新分词（以前是全量 1，每次按键重收整份文件）
    assert caps["textDocumentSync"]["change"] == 2
    assert caps["textDocumentSync"]["openClose"] is True
    assert caps["hoverProvider"] is True
    assert caps["definitionProvider"] is True
    assert caps["completionProvider"]["triggerCharacters"] == ["."]


def test_unknown_method_returns_error_not_crash():
    srv = _server()
    sent = []
    srv._write = sent.append
    srv.dispatch({"jsonrpc": "2.0", "id": 9, "method": "无此方法", "params": {}})
    assert sent[-1]["error"]["code"] == -32601


def test_exit_stops_loop():
    srv = _server()
    srv.dispatch({"jsonrpc": "2.0", "method": "exit", "params": {}})
    assert srv._stopped is True


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------

def test_syntax_error_diagnostic_has_range_and_code():
    srv = _server()
    sent = []
    srv._write = sent.append
    _open(srv, "如果 真\n    打印(1)\n")
    diag = sent[-1]["params"]["diagnostics"]
    assert len(diag) == 1
    d = diag[0]
    assert d["severity"] == 1
    assert d["source"] == "jishi"
    assert d["code"].startswith("E")
    assert d["range"]["start"]["line"] == 0
    assert "冒号" in d["message"]


def test_undefined_name_warning():
    srv = _server()
    sent = []
    srv._write = sent.append
    _open(srv, "令 甲 = 1\n打印(打应(甲))\n")
    diag = sent[-1]["params"]["diagnostics"]
    assert len(diag) == 1
    assert diag[0]["severity"] == 2
    assert "打应" in diag[0]["message"]


def test_clean_source_has_no_diagnostics():
    srv = _server()
    sent = []
    srv._write = sent.append
    _open(srv, SRC)
    assert sent[-1]["params"]["diagnostics"] == []


def test_no_false_positive_on_package_injected_name():
    """包依赖注入的名字（`基础库.平方`）不应被当成未定义。"""
    srv = _server()
    sent = []
    srv._write = sent.append
    _open(srv, "函数 立方(x)：\n    返回 基础库.平方(x) * x\n")
    assert sent[-1]["params"]["diagnostics"] == []


def test_did_change_refreshes_diagnostics():
    srv = _server()
    sent = []
    srv._write = sent.append
    _open(srv, SRC)
    assert sent[-1]["params"]["diagnostics"] == []
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didChange",
                  "params": {"textDocument": {"uri": URI, "version": 2},
                             "contentChanges": [{"text": "打应(1)\n"}]}})
    assert any("打应" in d["message"]
               for d in sent[-1]["params"]["diagnostics"])


def test_did_close_clears_diagnostics():
    srv = _server()
    sent = []
    srv._write = sent.append
    _open(srv, SRC)
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didClose",
                  "params": {"textDocument": {"uri": URI}}})
    assert sent[-1]["params"]["diagnostics"] == []
    assert URI not in srv.docs


# ---------------------------------------------------------------------------
# 补全
# ---------------------------------------------------------------------------

def _completion(srv: LspServer, line: int, char: int) -> list:
    sent = []
    srv._write = sent.append
    srv.dispatch({"jsonrpc": "2.0", "id": 1, "method": "textDocument/completion",
                  "params": {"textDocument": {"uri": URI},
                             "position": {"line": line, "character": char}}})
    return sent[-1]["result"]["items"]


def test_completion_offers_keywords_builtins_stdlib_symbols():
    srv = _server()
    _open(srv, SRC)
    labels = [i["label"] for i in _completion(srv, 9, 0)]
    assert "如果" in labels
    assert "打印" in labels
    assert "随机" in labels
    assert "求平均" in labels


def test_completion_prefix_filter():
    srv = _server()
    _open(srv, SRC)
    labels = [i["label"] for i in _completion(srv, 7, 2)]     # 光标在「打印」之后
    assert labels
    assert all(l.startswith("打印") for l in labels)


def test_completion_after_dot_gives_stdlib_functions():
    srv = _server()
    _open(srv, "导入 随机\n令 甲 = 随机.\n")
    items = _completion(srv, 1, 9)                            # 点在 `.` 之后
    labels = [i["label"] for i in items]
    assert "随机整数" in labels
    assert any(i.get("detail", "").startswith("随机整数(") for i in items)


def test_completion_labels_are_unique():
    srv = _server()
    _open(srv, "令 甲 = 1\n甲 = 2\n甲 = 3\n")
    labels = [i["label"] for i in _completion(srv, 4, 0)]
    assert len(labels) == len(set(labels))


# ---------------------------------------------------------------------------
# 悬停与跳转
# ---------------------------------------------------------------------------

def _hover(srv: LspServer, line: int, char: int):
    sent = []
    srv._write = sent.append
    srv.dispatch({"jsonrpc": "2.0", "id": 1, "method": "textDocument/hover",
                  "params": {"textDocument": {"uri": URI},
                             "position": {"line": line, "character": char}}})
    return sent[-1].get("result")


def test_hover_keyword():
    srv = _server()
    _open(srv, SRC)
    r = _hover(srv, 2, 5)                                     # 「遍历」
    assert r and "关键字" in r["contents"]["value"]


def test_hover_builtin_has_doc():
    srv = _server()
    _open(srv, SRC)
    r = _hover(srv, 7, 1)                                     # 「打印」
    assert r and "内建函数" in r["contents"]["value"]


def test_hover_document_symbol():
    srv = _server()
    _open(srv, SRC)
    r = _hover(srv, 0, 3)                                     # 「求平均」
    assert r and "本文件第 1 行" in r["contents"]["value"]


def test_hover_unknown_returns_none():
    srv = _server()
    _open(srv, "令 甲 = 1\n")
    assert _hover(srv, 0, 20) is None


def test_definition_points_to_symbol():
    srv = _server()
    _open(srv, SRC)
    sent = []
    srv._write = sent.append
    srv.dispatch({"jsonrpc": "2.0", "id": 1, "method": "textDocument/definition",
                  "params": {"textDocument": {"uri": URI},
                             "position": {"line": 7, "character": 4}}})
    r = sent[-1]["result"]
    assert r["uri"] == URI
    assert r["range"]["start"]["line"] == 0
    assert r["range"]["start"]["character"] == 0


def test_document_symbols():
    srv = _server()
    _open(srv, SRC)
    sent = []
    srv._write = sent.append
    srv.dispatch({"jsonrpc": "2.0", "id": 1,
                  "method": "textDocument/documentSymbol",
                  "params": {"textDocument": {"uri": URI}}})
    names = [s["name"] for s in sent[-1]["result"]]
    assert "求平均" in names
    assert "成绩" in names
    # 同一变量多次赋值只出现一次
    assert names.count("总数") == 1


def test_unpacked_names_count_as_defined():
    """解包出来的名字要算「已定义」——`令 甲, 乙 = …` 与 `遍历 序, 项 在 …`。

    来由（M41 修）：`_flatten` 原先对 `TargetList` 只 yield 它自己，里面的
    `Name` 一个都收不到，于是解包变量在 LSP 眼里「从未定义」——
    `test_corpus_has_no_false_positive_warnings` 报出来了。这条缺陷在
    `令 甲, 乙 = …`（M25）上早就存在，M41 的 `遍历` 解包让它暴露得更明显。
    """
    from jishi import lsp as lsp_mod
    from jishi.parser import parse
    from jishi.tokenizer import tokenize

    def 已定义(src: str) -> set[str]:
        return lsp_mod._defined_names(
            parse(tokenize(src, "<t>"), src.split("\n"), "<t>"))

    assert {"甲", "乙"} <= 已定义("令 甲, 乙 = [1, 2]\n")
    assert {"头", "尾"} <= 已定义("令 头, *尾 = [1, 2, 3]\n")
    assert {"序", "项"} <= 已定义(
        "遍历 序, 项 在 带下标([1, 2], 1):\n    打印(序, 项)\n")


def test_unpacked_loop_vars_no_false_positive():
    """解包遍历的循环体里用那些名字，不该报「没有定义过」。"""
    src = ("令 对们 = [[\"小明\", 8]]\n"
           "遍历 名字, 年龄 在 对们：\n"
           "    打印(名字, 年龄)\n")
    srv = _server()
    sent = []
    srv._write = sent.append
    _open(srv, src)
    diag = sent[-1]["params"]["diagnostics"]
    assert [d for d in diag if "没有定义过" in d.get("message", "")] == []


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------

def test_stdio_transport_end_to_end():
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen",
         "params": {"textDocument": {"uri": URI, "version": 1, "text": SRC}}},
        {"jsonrpc": "2.0", "id": 2, "method": "textDocument/hover",
         "params": {"textDocument": {"uri": URI},
                    "position": {"line": 0, "character": 3}}},
        {"jsonrpc": "2.0", "id": 3, "method": "shutdown"},
        {"jsonrpc": "2.0", "method": "exit"},
    ]
    stdin = io.BytesIO(b"".join(_frame(m) for m in messages))
    stdout = io.BytesIO()
    assert LspServer(stdin=stdin, stdout=stdout).serve() == 0

    got = _frames(stdout.getvalue())
    assert [g.get("id") for g in got if "id" in g] == [1, 2, 3]
    diag = [g for g in got if g.get("method") == "textDocument/publishDiagnostics"]
    assert diag and diag[0]["params"]["diagnostics"] == []


def test_corpus_has_no_false_positive_warnings():
    """回归防线：真实语料（都能正常跑）不应冒出「未定义」警告。"""
    srv = _server()
    files = sorted(ROOT.glob("examples/**/*.jsh")) + sorted(
        ROOT.glob("tests/cases/*.jsh"))
    assert files, "语料为空，检查路径"
    problems = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        uri = p.as_uri()
        srv.docs[uri] = {"text": text, "version": 1}
        srv._analyze(uri)
        warns = [d for d in srv.diagnostics.get(uri, [])
                 if d["severity"] == 2]
        if warns:
            problems.append((p.name, warns[0]["message"]))
    assert problems == [], f"出现误报：{problems[:5]}"
