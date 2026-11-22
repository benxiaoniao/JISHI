# -*- coding: utf-8 -*-
"""R7.4：MCP 服务器搬进 Rust（`jishi-rs mcp`）。

验收原文（`docs/路线图.md` §11.8 的 R7.4）：
② MCP **7 个工具报文逐字一致**；③ `run_script` 的隔离与超时语义一致
（**不是「差不多」，是「真 kill」**）。

两半照 R7.1 起的规矩：

* **对拍**：同一串报文（换行分隔的 JSON-RPC）喂 `jishi-rs mcp` 与
  `python -m oracle.jishi.mcp_server`，比 **stdout 原始字节**。
  只有 `duration_ms` 是墙上时间，比之前归一 —— 其余一个字节都不许差。
* **独立判据**：不 import oracle.jishi、不 spawn Python，把该吐的报文**写死**。

⚠️ **超时那一格不进对拍**：本机 Python 侧的沙箱超时会把输出整个丢掉
（详见 `test_m92_cli_rust.py` 模块头）。「真 kill」这半只钉宿主：
死循环 + `timeout` 必须**几秒内**带着 `执行超时（… 秒）` 回来。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


EXE = _exe()
need_rust = pytest.mark.skipif(
    not EXE.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）"
           "—— 与其假装通过，不如明确跳过")

DUR = re.compile(rb'duration_ms[^0-9]*[-0-9][0-9.eE+-]*')
DUR_ZERO = b'duration_ms": 0'


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    return env


def _ask(cmd, msgs, timeout: float = 300.0):
    """把一串消息（字典或裸字符串）按换行分隔喂进去，收原始输出。"""
    lines = []
    for m in msgs:
        lines.append(m if isinstance(m, str) else json.dumps(m, ensure_ascii=False))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    r = subprocess.run(list(cmd), input=payload, capture_output=True, env=_env(),
                       cwd=str(ROOT), timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def _rs(msgs, timeout: float = 300.0):
    return _ask([str(EXE), "mcp"], msgs, timeout)


def _py(msgs, timeout: float = 300.0):
    return _ask([sys.executable, "-m", "oracle.jishi.mcp_server"], msgs, timeout)


def _call(i, name, args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


#: 一串覆盖全部 7 个工具 + 协议边角的报文。
#: ⚠️ **没有死循环那一格**（见模块头）—— 它在下面单独钉。
MSGS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    # --- run_script ---
    _call(3, "run_script", {"script": "打印(1 + 1)\n"}),
    _call(4, "run_script", {"script": "导入 文件\n"}),
    _call(5, "run_script", {"script": "打印(1 / 0)\n"}),
    _call(6, "run_script", {"script": "如果 真\n"}),
    _call(7, "run_script", {"script": "print(1)\n"}),
    _call(8, "run_script", {"script": "遍历 i 在 范围(3000)：\n    打印(\"一二三四五\")\n"}),
    _call(9, "run_script", {"script": "令 甲 = 1\n甲\n"}),
    # --- eval_expr ---
    _call(10, "eval_expr", {"expr": "1 + 2 * 3"}),
    _call(11, "eval_expr", {"expr": "\"甲\" + \"乙\""}),
    _call(12, "eval_expr", {"expr": "[1, 2, 3]"}),
    _call(13, "eval_expr", {"expr": "1.5 + 1.5"}),
    _call(14, "eval_expr", {"expr": "2 ** 100"}),
    _call(15, "eval_expr", {"expr": "1 / 0"}),
    _call(16, "eval_expr", {"expr": "令 x = 1"}),
    _call(17, "eval_expr", {"expr": "导入 文件"}),
    # --- check_source ---
    _call(18, "check_source", {"source": "打印(len([1, 2]))\n"}),
    _call(19, "check_source", {"source": "令 甲 = 1\n令 乙 = 2\n打印(甲)\n"}),
    _call(20, "check_source", {"source": "甲 == 甲\n"}),
    # --- lookup_error ---
    _call(21, "lookup_error", {"code": "E2002"}),
    _call(22, "lookup_error", {"code": "E2999"}),
    _call(23, "lookup_error", {"code": "name.undefined"}),
    _call(24, "lookup_error", {"code": "NOPE"}),
    _call(25, "lookup_error", {}),
    # --- lookup_stdlib ---
    _call(26, "lookup_stdlib", {"module": "数学"}),
    _call(27, "lookup_stdlib", {"module": "数学", "function": "开方"}),
    _call(28, "lookup_stdlib", {"module": "数学", "function": "没有"}),
    _call(29, "lookup_stdlib", {"module": "没有这个模块"}),
    _call(30, "lookup_stdlib", {}),
    # --- format_source ---
    _call(31, "format_source", {"source": "令 甲=1\n打印( 甲 )\n"}),
    _call(32, "format_source", {"source": "令 甲 = 1\n打印(甲)\n"}),
    _call(33, "format_source", {"source": "如果 真\n"}),
    _call(34, "format_source", {"source": "令 甲=1\n", "indent": 2}),
    # --- describe_language ---
    _call(35, "describe_language", {}),
    # --- 协议边角 ---
    {"jsonrpc": "2.0", "id": 36, "method": "ping"},
    {"jsonrpc": "2.0", "id": 37, "method": "无此方法"},
    _call(38, "不存在的工具", {}),
    {"jsonrpc": "2.0", "id": 39, "method": "tools/call"},
    "这不是 JSON",
    {"jsonrpc": "2.0", "id": "字符串id", "method": "ping"},
]


@need_rust
def test_对拍_报文逐字节():
    """同一串报文喂两边 → stdout **逐字节**一致（只有 `duration_ms` 归一）。"""
    a, b = _rs(MSGS), _py(MSGS)
    # 报文条数先对一下：**每一条请求都该有应答**，只有两条例外 ——
    # 通知（`notifications/initialized`）按协议不回，坏 JSON 那行两边都跳过。
    want = len(MSGS) - 2
    assert len(a[0].splitlines()) == len(b[0].splitlines()) == want, \
        (len(a[0].splitlines()), len(b[0].splitlines()), want)
    # 逐条报不同在哪（几万字节直接 diff 看不出东西）
    la, lb = DUR.sub(DUR_ZERO, a[0]).splitlines(), DUR.sub(DUR_ZERO, b[0]).splitlines()
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else b"<none>"
        y = lb[i] if i < len(lb) else b"<none>"
        assert x == y, f"第 {i + 1} 条不同：\n  rust={x[:400]!r}\n  py  ={y[:400]!r}"
    assert (a[1], a[2]) == (b[1], b[2]), (a[1][:200], b[1][:200], a[2], b[2])


# ---------------------------------------------------------------------------
# 独立判据（不 import oracle.jishi、不 spawn Python）
# ---------------------------------------------------------------------------

@need_rust
def test_独立_initialize整串():
    out, err, rc = _rs([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"capabilities": {}}}])
    assert (err, rc) == (b"", 0)
    got = json.loads(out.decode("utf-8"))
    assert got["jsonrpc"] == "2.0" and got["id"] == 1
    res = got["result"]
    assert res["protocolVersion"] == "2024-11-05"
    assert res["capabilities"] == {"tools": {}}
    assert res["serverInfo"]["name"] == "jishi-mcp"
    # 版本号与语言规格同一个来源（那条另有漂移检测钉着）
    assert re.fullmatch(r"\d+\.\d+\.\d+", res["serverInfo"]["version"])
    # 键顺序也是内容（客户端可能逐字转发）
    assert out.decode("utf-8").startswith('{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion"')


@need_rust
def test_独立_tools_list七个工具():
    out, _, _ = _rs([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    tools = json.loads(out.decode("utf-8"))["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "run_script", "eval_expr", "describe_language", "check_source",
        "lookup_error", "lookup_stdlib", "format_source"]
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert isinstance(t["inputSchema"]["properties"], dict)
    # `describe_language` 的 schema **没有** `required` 键（Python 侧就没有）
    dl = [t for t in tools if t["name"] == "describe_language"][0]
    assert "required" not in dl["inputSchema"]
    # 别的都有
    rs = [t for t in tools if t["name"] == "run_script"][0]
    assert rs["inputSchema"]["required"] == ["script"]


@need_rust
def test_独立_run_script结果整串():
    out, _, rc = _rs([_call(3, "run_script", {"script": "打印(1 + 1)\n"})])
    assert rc == 0
    text = json.loads(out.decode("utf-8"))["result"]["content"][0]["text"]
    assert DUR.sub(DUR_ZERO, text.encode("utf-8")).decode("utf-8") == \
        '{"ok": true, "value": null, "stdout": "2\\n", "duration_ms": 0}'
    # 内容块的外形（MCP 客户端按这个取文本）
    assert json.loads(out.decode("utf-8"))["result"]["content"][0]["type"] == "text"


@need_rust
def test_独立_lookup_error整串():
    out, _, _ = _rs([_call(4, "lookup_error", {"code": "E2002"})])
    text = json.loads(out.decode("utf-8"))["result"]["content"][0]["text"]
    assert text == ('{"ok": true, "kind": "错误码", "code": "E2002", '
                    '"class": "RunTypeError", "title": "类型错误", "internal": false}')


@need_rust
def test_独立_未知工具与方法():
    out, _, _ = _rs([_call(5, "不存在的工具", {}),
                     {"jsonrpc": "2.0", "id": 6, "method": "无此方法"}])
    la = out.decode("utf-8").splitlines()
    assert json.loads(la[0])["result"]["content"][0]["text"] == \
        '{"ok": false, "error": {"message": "未知工具 不存在的工具"}}'
    assert json.loads(la[1])["error"] == {"code": -32601, "message": "方法未实现：无此方法"}


@need_rust
def test_独立_坏JSON被跳过():
    """解析不了的行**直接跳过**（与 Python 一样），后面的照样处理。"""
    out, _, _ = _rs(["这不是 JSON", {"jsonrpc": "2.0", "id": 7, "method": "ping"}])
    la = out.decode("utf-8").splitlines()
    assert len(la) == 1 and json.loads(la[0]) == {"jsonrpc": "2.0", "id": 7, "result": {}}


@need_rust
def test_独立_没有PATH也能跑():
    """清空环境也要能跑 —— `run_script` 走的是**自己**（不是 shell、不是 Python）。"""
    env = {} if os.name != "nt" else {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")}
    payload = (json.dumps(_call(1, "run_script", {"script": "打印(6 * 7)\n"}),
                          ensure_ascii=False) + "\n").encode("utf-8")
    r = subprocess.run([str(EXE), "mcp"], input=payload, capture_output=True,
                       env=env, cwd=str(ROOT), timeout=120)
    text = json.loads(r.stdout.decode("utf-8"))["result"]["content"][0]["text"]
    assert '"stdout": "42\\n"' in text, text


@need_rust
@pytest.mark.计时
def test_独立_run_script到点真kill():
    """**「真 kill」的验收就在这里**：死循环 + 0.6 秒超时，必须很快回来，
    而且带回沙箱那条 `执行超时（0.6 秒）`。

    ⚠️ Python 侧的同一格**做不到**：它会跑十几秒（泄漏的守护线程抢 GIL），
    而且收尾时把输出整个丢掉 —— 所以它不进对拍，只钉宿主。
    """
    t0 = time.perf_counter()
    out, err, rc = _rs([_call(1, "run_script",
                              {"script": "令 i = 0\n当 真：\n    i = i + 1\n",
                               "timeout": 0.6})], timeout=60)
    dt = time.perf_counter() - t0
    assert (err, rc) == (b"", 0)
    text = json.loads(out.decode("utf-8"))["result"]["content"][0]["text"]
    assert '"message": "执行超时（0.6 秒）"' in text, text
    assert dt < 15.0, f"run_script 超时用了 {dt:.1f}s —— 没做到「到点就 kill」"
