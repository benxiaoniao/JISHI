# -*- coding: utf-8 -*-
"""R7.3：语言服务器搬进 Rust —— `jishi-rs lsp` vs `jishi lsp`。

三半：

* **对拍**（§一~§二）：同一串 LSP 报文喂两边，比**原始字节**；再用**编辑器自带的**
  `editors/vscode-jishi/verify-lsp.js` 两边各跑一遍（验收原文就是这个）。
* **独立判据**（§三）：不 import oracle.jishi、不 spawn Python 的**写死期望** ——
  机机器上不装 Python 也要能用。
* **性能护栏**（§四，打 `计时`）：每次编辑的耗时**不许比 Python 版慢**
  （验收原文的「增量指标不倒退」）。

R7.3 的验收原文：「用编辑器自带的同一份自检（`verify-lsp.js`）两边各跑一遍 →
**报文逐字一致**；且**机器上不装 Python** 也能用」。
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_CLEAN = "\n".join([
    "导入 容器",
    "函数 求平均(分数们)：",
    "    令 总数 = 0",
    "    返回 总数",
    "",
    "令 成绩 = [85, 92]",
    "打印(求平均(成绩))",
    "",
])
_BAD = "打印(len([1, 2]))\n"


def _rust() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _need_rust(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _rust().exists():
            pytest.skip("Rust 宿主未构建（cd rust && cargo build --release）")
        return fn(*args, **kwargs)
    return wrapper


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    return env


def _frame(obj: dict) -> bytes:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data


def _split_msgs(data: bytes) -> list[bytes]:
    out: list[bytes] = []
    while data:
        head, sep, rest = data.partition(b"\r\n\r\n")
        if not sep:
            break
        n = int(head.split(b":")[1].strip())
        out.append(rest[:n])
        data = rest[n:]
    return out


def _run(cmd: list[str], payload: bytes, cwd, env: dict | None = None):
    r = subprocess.run(cmd, input=payload, capture_output=True,
                       env=env if env is not None else _env(), cwd=str(cwd))
    return r.stdout, r.stderr, r.returncode


def _uri(name: str) -> str:
    return "file:///" + name


def _script(ws: str) -> list[dict]:
    """覆盖 18 个方法的报文序列（比 verify-lsp.js 更全：含版本乱序、多段编辑）。"""
    ws_uri = "file:///" + ws.replace("\\", "/")
    seq: list[dict] = []

    def req(i, method, params):
        seq.append({"jsonrpc": "2.0", "id": i, "method": method, "params": params})

    def note(method, params):
        seq.append({"jsonrpc": "2.0", "method": method, "params": params})

    req(1, "initialize", {"processId": os.getpid(), "rootUri": ws_uri,
                          "capabilities": {}})
    note("initialized", {})
    note("textDocument/didOpen", {"textDocument": {
        "uri": _uri("干净.jsh"), "languageId": "jishi", "version": 1, "text": _CLEAN}})
    note("textDocument/didOpen", {"textDocument": {
        "uri": _uri("有错.jsh"), "languageId": "jishi", "version": 1, "text": _BAD}})
    note("textDocument/didOpen", {"textDocument": {
        "uri": _uri("引用.jsh"), "languageId": "jishi", "version": 1,
        "text": "打印(打招呼())\n"}})

    req(2, "textDocument/completion", {"textDocument": {"uri": _uri("干净.jsh")},
                                       "position": {"line": 6, "character": 3}})
    req(3, "textDocument/completion", {"textDocument": {"uri": _uri("干净.jsh")},
                                       "position": {"line": 0, "character": 3}})
    req(4, "textDocument/completion", {"textDocument": {"uri": _uri("干净.jsh")},
                                       "position": {"line": 2, "character": 8}})
    req(5, "textDocument/hover", {"textDocument": {"uri": _uri("干净.jsh")},
                                  "position": {"line": 1, "character": 4}})
    req(6, "textDocument/hover", {"textDocument": {"uri": _uri("干净.jsh")},
                                  "position": {"line": 0, "character": 1}})
    req(7, "textDocument/hover", {"textDocument": {"uri": _uri("有错.jsh")},
                                  "position": {"line": 0, "character": 4}})
    req(8, "textDocument/definition", {"textDocument": {"uri": _uri("干净.jsh")},
                                       "position": {"line": 6, "character": 5}})
    req(9, "textDocument/definition", {"textDocument": {"uri": _uri("引用.jsh")},
                                       "position": {"line": 0, "character": 4}})
    req(10, "textDocument/documentSymbol", {"textDocument": {"uri": _uri("干净.jsh")}})
    req(11, "workspace/symbol", {"query": "求"})
    req(12, "workspace/symbol", {"query": ""})
    req(13, "textDocument/formatting", {"textDocument": {"uri": _uri("有错.jsh")},
                                        "options": {}})
    req(14, "textDocument/prepareRename", {"textDocument": {"uri": _uri("干净.jsh")},
                                           "position": {"line": 1, "character": 4}})
    req(15, "textDocument/rename", {"textDocument": {"uri": _uri("干净.jsh")},
                                    "position": {"line": 1, "character": 4},
                                    "newName": "平均值"})
    req(16, "textDocument/rename", {"textDocument": {"uri": _uri("干净.jsh")},
                                    "position": {"line": 1, "character": 4},
                                    "newName": "1坏名字"})
    req(17, "textDocument/codeAction", {"textDocument": {"uri": _uri("有错.jsh")},
                                        "range": {"start": {"line": 0, "character": 4},
                                                  "end": {"line": 0, "character": 4}},
                                        "context": {"diagnostics": []}})
    req(18, "textDocument/signatureHelp", {"textDocument": {"uri": _uri("干净.jsh")},
                                           "position": {"line": 6, "character": 7}})
    req(19, "textDocument/semanticTokens/full",
        {"textDocument": {"uri": _uri("干净.jsh")}})
    req(20, "textDocument/semanticTokens/full",
        {"textDocument": {"uri": _uri("有错.jsh")}})
    note("textDocument/didChange", {"textDocument": {"uri": _uri("干净.jsh"), "version": 2},
                                    "contentChanges": [
                                        {"range": {"start": {"line": 4, "character": 0},
                                                   "end": {"line": 4, "character": 0}},
                                         "text": "令 新变量 = 1\n"}]})
    note("textDocument/didChange", {"textDocument": {"uri": _uri("干净.jsh"), "version": 1},
                                    "contentChanges": [
                                        {"range": {"start": {"line": 0, "character": 0},
                                                   "end": {"line": 0, "character": 0}},
                                         "text": "X"}]})
    note("textDocument/didChange", {"textDocument": {"uri": _uri("有错.jsh"), "version": 2},
                                    "contentChanges": [
                                        {"range": {"start": {"line": 0, "character": 3},
                                                   "end": {"line": 0, "character": 6}},
                                         "text": "长度"},
                                        {"text": "打印(长度([1, 2]))\n"}]})
    req(21, "textDocument/completion", {"textDocument": {"uri": _uri("干净.jsh")},
                                        "position": {"line": 6, "character": 3}})
    note("textDocument/didSave", {"textDocument": {"uri": _uri("干净.jsh")}})
    note("textDocument/didClose", {"textDocument": {"uri": _uri("有错.jsh")}})
    req(22, "textDocument/documentSymbol", {"textDocument": {"uri": _uri("干净.jsh")}})
    req(23, "shutdown", {})
    note("exit", {})
    return seq


def _workspace() -> tempfile.TemporaryDirectory:
    d = tempfile.TemporaryDirectory(prefix="jishi-lsp-rust-")
    (Path(d.name) / "别的.jsh").write_text("函数 打招呼()：\n    返回 1\n", encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 一、报文逐字节一致
# ---------------------------------------------------------------------------


@_need_rust
def test_报文逐字节一致():
    """33 条报文（覆盖 18 个方法 + 增量编辑 + 版本乱序）喂两边，比原始字节。"""
    with _workspace() as ws:
        payload = b"".join(_frame(m) for m in _script(ws))
        py_out, _, py_rc = _run([sys.executable, "-m", "oracle.jishi.cli", "lsp"], payload, ws)
        rs_out, _, rs_rc = _run([str(_rust()), "lsp"], payload, ws)

    assert py_rc == rs_rc, f"退出码不同：py={py_rc} rust={rs_rc}"
    assert py_out == rs_out, _first_diff_msg(py_out, rs_out)


def _first_diff_msg(py: bytes, rs: bytes) -> str:
    if len(py) != len(rs):
        head = f"长度不同：py={len(py)} rust={len(rs)}\n"
    else:
        head = ""
    pa, ra = _split_msgs(py), _split_msgs(rs)
    for i in range(max(len(pa), len(ra))):
        x = pa[i] if i < len(pa) else b"<none>"
        y = ra[i] if i < len(ra) else b"<none>"
        if x != y:
            return (f"{head}第 {i + 1} 条报文不同（共 py={len(pa)} / rust={len(ra)} 条）：\n"
                    f"  py  : {x[:400]!r}\n  rust: {y[:400]!r}")
    return head + "报文条数与内容都相同，但整串字节不同（分帧？）"


# ---------------------------------------------------------------------------
# 二、编辑器自带的自检：两边各跑一遍
# ---------------------------------------------------------------------------


def _node() -> str | None:
    return shutil.which("node")


@_need_rust
def test_verify_lsp_js_两边都通过():
    """验收原文：用 `editors/vscode-jishi/verify-lsp.js` 对两边各跑一遍。"""
    node = _node()
    if node is None:
        pytest.skip("没有 node，跑不了编辑器自带的自检")
    script = ROOT / "editors" / "vscode-jishi" / "verify-lsp.js"
    assert script.exists(), "编辑器自检脚本不在了？"

    def run_one(bin_path: str, args: str) -> str:
        env = _env()
        env["JISHI_BIN"] = bin_path
        env["JISHI_ARGS"] = args
        r = subprocess.run([node, str(script)], capture_output=True,
                           env=env, cwd=str(ROOT))
        return r.stdout.decode("utf-8", "replace")

    py = run_one(sys.executable, "-m oracle.jishi.cli lsp")
    rs = run_one(str(_rust()), "lsp")
    assert "通过 25 项，失败 0 项" in py, f"Python 版自检没全过：\n{py[-800:]}"
    assert "通过 25 项，失败 0 项" in rs, f"Rust 版自检没全过：\n{rs[-800:]}"


# ---------------------------------------------------------------------------
# 三、独立判据（**不 import oracle.jishi、不 spawn Python** —— 不装 Python 也要能用）
# ---------------------------------------------------------------------------

_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"capabilities": {}}}

#: 人工核对过（与 Python 侧逐字节比过）之后写死的诊断报文。
_DIAG_BAD = (
    '{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": '
    '{"uri": "file:///t.jsh", "diagnostics": [{"range": {"start": {"line": 0, '
    '"character": 3}, "end": {"line": 0, "character": 6}}, "severity": 1, '
    '"code": "E0201", "source": "jishi", "message": "[E0201] 这里出现了意外的内容：'
    '「len」是 Python 的内建函数，基石里叫「长度」\\n提示：把 len(…) 改成 长度(…)"}], '
    '"version": 1}}'
)


@_need_rust
def test_独立判据_初始化声明():
    payload = b"".join([_frame(_INIT), _frame({"jsonrpc": "2.0", "method": "exit", "params": {}})])
    out, err, rc = _run([str(_rust()), "lsp"], payload, ROOT)
    assert rc == 0 and err == b"", (rc, err[:200])
    msgs = _split_msgs(out)
    assert len(msgs) == 1, msgs
    d = json.loads(msgs[0])
    caps = d["result"]["capabilities"]
    assert d["id"] == 1
    # 增量同步（2）—— 这条是性能的前提
    assert caps["textDocumentSync"] == {"openClose": True, "change": 2, "save": True}
    assert caps["codeActionProvider"] == {
        "codeActionKinds": ["quickfix", "source.format"]}
    assert caps["renameProvider"] == {"prepareProvider": True}
    assert caps["semanticTokensProvider"]["legend"]["tokenTypes"] == [
        "keyword", "function", "variable", "parameter", "namespace", "property"]
    assert caps["signatureHelpProvider"] == {"triggerCharacters": ["(", "，", ","]}
    assert d["result"]["serverInfo"]["name"] == "jishi-lsp"


@_need_rust
def test_独立判据_诊断报文逐字节():
    """`打印(len([1, 2]))` 的诊断报文 —— 写死，整串比。"""
    payload = b"".join([
        _frame(_INIT),
        _frame({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": "file:///t.jsh",
                             "text": _BAD, "version": 1}}}),
        _frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}}),
        _frame({"jsonrpc": "2.0", "method": "exit", "params": {}}),
    ])
    out, _, rc = _run([str(_rust()), "lsp"], payload, ROOT)
    assert rc == 0
    msgs = _split_msgs(out)
    assert [m.decode("utf-8") for m in msgs][1] == _DIAG_BAD


@_need_rust
def test_独立判据_语法错的文件也能开():
    """语法错（不是词法错）也要能打开并出诊断 —— 这条与「词法错不注册」是两面。"""
    payload = b"".join([
        _frame(_INIT),
        _frame({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": "file:///t.jsh",
                             "text": "令 甲 = \n", "version": 1}}}),
        _frame({"jsonrpc": "2.0", "id": 2, "method": "textDocument/hover",
                "params": {"textDocument": {"uri": "file:///t.jsh"},
                           "position": {"line": 0, "character": 2}}}),
        _frame({"jsonrpc": "2.0", "method": "exit", "params": {}}),
    ])
    out, _, _ = _run([str(_rust()), "lsp"], payload, ROOT)
    text = out.decode("utf-8")
    assert '"code": "E0201"' in text, text[:400]
    # 文档注册了（hover 有应答，哪怕是 null 也得有 id 2 的回复）
    assert '"id": 2' in text, text[:400]


@_need_rust
def test_独立判据_没有PATH也能跑():
    """清空环境也要能当语言服务器 —— 全靠二进制自己，**不 shell out 找 python**。

    （验收原文「机器上不装 Python 也能用」的近似验证：连 PATH 都不给它。）
    """
    import tempfile as _tf
    with _tf.TemporaryDirectory(prefix="jishi-lsp-nopath-") as ws:
        payload = b"".join([
            _frame(_INIT),
            _frame({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
                "textDocument": {"uri": "file:///t.jsh",
                                 "text": _BAD, "version": 1}}}),
            _frame({"jsonrpc": "2.0", "method": "exit", "params": {}}),
        ])
        env = {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")} \
            if os.name == "nt" else {}
        out, err, rc = _run([str(_rust()), "lsp"], payload, ws, env=env)
    assert rc == 0, (rc, err[:200])
    assert _DIAG_BAD.encode("utf-8") in out


# ---------------------------------------------------------------------------
# 四、性能护栏：每次编辑的耗时不许比 Python 版慢
# ---------------------------------------------------------------------------


def _bench_payload(text: str, edits: int) -> bytes:
    uri = "file:///t.jsh"
    out = [_frame(_INIT),
           _frame({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                   "params": {"textDocument": {"uri": uri, "text": text, "version": 1}}})]
    line = max(0, text.count("\n") // 2)
    for k in range(edits):
        out.append(_frame({"jsonrpc": "2.0", "method": "textDocument/didChange",
                           "params": {"textDocument": {"uri": uri, "version": 2 + k},
                                      "contentChanges": [
                                          {"range": {"start": {"line": line, "character": 3},
                                                     "end": {"line": line, "character": 3}},
                                           "text": "9"}]}}))
    out.append(_frame({"jsonrpc": "2.0", "method": "exit", "params": {}}))
    return b"".join(out)


def _mk_lines(n: int) -> str:
    lines = ["令 甲 = 1"]
    for i in range(1, n):
        lines.append(f"令 变量{i} = {i}")
    return "\n".join(lines) + "\n"


@pytest.mark.计时
@_need_rust
def test_计时_每次编辑不慢于Python(tmp_path):
    """同一串编辑喂两边，减掉启动成本后**每次编辑**的耗时。

    ⚠️ 打 `计时`（墙钟护栏，并行时不跑）。基线同机同时段测，只断言
    「Rust **不慢于** Python」—— 实测快 ~6 倍，留足余量防载荷抖动。
    """
    text = _mk_lines(5003)
    zero = _bench_payload(text, 0)
    edits = _bench_payload(text, 20)

    def net(cmd: list[str]) -> float:
        t0 = min(_timeit(cmd, zero) for _ in range(2))
        t1 = min(_timeit(cmd, edits) for _ in range(2))
        return (t1 - t0) / 20

    py_ms = net([sys.executable, "-m", "oracle.jishi.cli", "lsp"])
    rs_ms = net([str(_rust()), "lsp"])
    print(f"\n5003 行 · 每次编辑：Python {py_ms:.1f}ms / Rust {rs_ms:.1f}ms")
    assert rs_ms <= py_ms, (
        f"Rust 版每次编辑 {rs_ms:.1f}ms 比 Python 版 {py_ms:.1f}ms 还慢 —— 增量指标倒退了")


def _timeit(cmd: list[str], data: bytes) -> float:
    t = time.perf_counter()
    subprocess.run(cmd, input=data, capture_output=True, env=_env(), cwd=str(ROOT))
    return (time.perf_counter() - t) * 1000
