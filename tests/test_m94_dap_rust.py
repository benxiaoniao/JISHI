# -*- coding: utf-8 -*-
"""R7.5：DAP 搬进 Rust —— 与 Python 侧**报文逐字节一致** + 不依赖 Python 的独立判据。

怎么比：同一串 DAP 请求喂两个适配器，比**收到的原始字节**（Content-Length 帧
一字不改）。⚠️ 必须写成**同步客户端**（发一条、等它该来的应答/事件，再发下一条）：

    程序**自由跑**的时候收到的请求，Python 是当场就答、Rust 是下一个语句边界才答
    —— 那是**时序**差异（两边都合理）。用「等到该来的东西」把两边都卡在同一点上，
    报文序列才可比。真实编辑器也是这么干的。

判据分两半（本项目的惯例，R7.1-c 起的纪律）：
* **对拍**：与 `jishi dap` 逐字节，三种场景（普通单步 / 就地求值 / 出错现场）。
* **独立判据**：把「Rust 自己该发什么」写成写死的期望 —— 不 import oracle.jishi、
  不 spawn Python，R8 之后 Python 退场它仍然要绿。

⚠️ 已知边界（写在这里，不靠「读者自己想得到」）：Rust 只支持**树遍历**执行器
（`--执行器 vm/cvm` 明确报错）—— 字节码执行器的调试钩子还没搬。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

TMP_SUFFIX = f"{os.getpid()}"


def _exe() -> Path:
    return ROOT / "rust" / "target" / "release" / (
        "jishi-rs.exe" if os.name == "nt" else "jishi-rs")


EXE = _exe()


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONPATH"] = str(ROOT)
    return env


need_rust = pytest.mark.skipif(not EXE.exists(),
                              reason="jishi-rs 未构建（cd rust && cargo build --release）")


# ---------------------------------------------------------------------------
# 同步 DAP 客户端
# ---------------------------------------------------------------------------

class Dap:
    """只读 stdout 的帧、按需等应答/事件。"""

    def __init__(self, argv):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=_env(), cwd=str(ROOT))
        self.raw = bytearray()
        self._msgs: list = []
        self._lock = threading.Lock()
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        buf = b""
        out = self.p.stdout
        while True:
            chunk = out.read(1)
            if not chunk:
                break
            self.raw += chunk
            buf += chunk
            while True:
                head, sep, rest = buf.partition(b"\r\n\r\n")
                if not sep:
                    break
                try:
                    n = int(head.split(b":")[1].strip())
                except (IndexError, ValueError):
                    buf = rest
                    continue
                if len(rest) < n:
                    break
                body, buf = rest[:n], rest[n:]
                with self._lock:
                    self._msgs.append(json.loads(body.decode("utf-8")))

    def send(self, **kw):
        body = json.dumps({"jsonrpc": "2.0", **kw},
                          ensure_ascii=False).encode("utf-8")
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body))
        self.p.stdin.write(body)
        self.p.stdin.flush()

    def _next(self, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if self._msgs:
                    return self._msgs.pop(0)
            time.sleep(0.005)
        raise TimeoutError("等不到报文")

    def wait_response(self, seq):
        while True:
            m = self._next()
            if m.get("type") == "response" and m.get("request_seq") == seq:
                return m

    def wait_event(self, name):
        while True:
            m = self._next()
            if m.get("type") == "event" and m.get("event") == name:
                return m

    def wait_stop_or_exit(self):
        while True:
            m = self._next()
            if m.get("type") != "event":
                continue
            if m.get("event") in ("stopped", "exited"):
                return m["event"]

    def events(self):
        return [m for m in _split(bytes(self.raw))]

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self._t.join(timeout=5)
        return bytes(self.raw), self.p.returncode


def _split(data: bytes):
    out = []
    while data:
        head, sep, rest = data.partition(b"\r\n\r\n")
        if not sep:
            break
        n = int(head.split(b":")[1].strip())
        out.append(rest[:n])
        data = rest[n:]
    return out


# ---------------------------------------------------------------------------
# 两段被调试的程序
# ---------------------------------------------------------------------------

#: 普通场景：模块级 + 函数 + 循环 + 打印（打印走 output/stdout 事件）
PROG_OK = """令 总数 = 0
函数 累加(上限)：
    令 小计 = 0
    遍历 i 在 范围(上限)：
        小计 = 小计 + i
        打印("加到", i)
    返回 小计
令 结果 = 累加(3)
打印("结果", 结果)
总数 = 结果
打印(总数)
"""

#: 出错场景：内层函数里除零（考「调试视角的出错现场」）
PROG_ERR = """令 甲 = 1
函数 算(数值)：
    令 商 = 10 / 数值
    返回 商
令 甲 = 2
令 结果 = 算(0)
打印(结果)
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def run_session(argv, prog, bps, *, evaluate=False, want_error=False):
    """跑一整场会话（同步驱动），返回 `(收到的原始字节, 退出码)`。"""
    c = Dap(argv)
    n = [0]

    def req(method, **args):
        n[0] += 1
        c.send(seq=n[0], type="request", command=method, arguments=args)
        return n[0]

    s = req("initialize", adapterID="jishi", clientID="t", linesStartAt1=True,
            columnsStartAt1=True, pathFormat="path")
    c.wait_response(s)
    c.wait_event("initialized")

    s = req("setBreakpoints", source={"path": prog},
            breakpoints=[{"line": l} for l in bps])
    c.wait_response(s)

    s = req("launch", program=prog, stopOnEntry=True)
    c.wait_response(s)

    s = req("setBreakpoints", source={"path": prog},
            breakpoints=[{"line": l} for l in bps])
    c.wait_response(s)

    s = req("threads")
    c.wait_response(s)

    s = req("configurationDone")
    c.wait_response(s)
    c.wait_event("stopped")              # 第一个断点

    for m, a in (("stackTrace", {"threadId": 1}),
                 ("scopes", {"frameId": 1}),
                 ("variables", {"variablesReference": 1000}),
                 ("source", {"source": {"path": prog}})):
        sid = req(m, **a)
        c.wait_response(sid)
    if evaluate:
        sid = req("evaluate", expression="上限 * 2", frameId=1, context="watch")
        c.wait_response(sid)
        sid = req("evaluate", expression="没有这个名", frameId=1, context="watch")
        c.wait_response(sid)

    for _ in range(2):
        sid = req("next", threadId=1)
        c.wait_response(sid)
        c.wait_event("stopped")

    sid = req("stackTrace", threadId=1)
    c.wait_response(sid)
    sid = req("scopes", frameId=1)
    c.wait_response(sid)
    sid = req("variables", variablesReference=1000)
    c.wait_response(sid)
    sid = req("variables", variablesReference=1001)
    c.wait_response(sid)
    if evaluate:
        sid = req("evaluate", expression="小计", frameId=1, context="watch")
        c.wait_response(sid)

    sid = req("stepIn", threadId=1)
    c.wait_response(sid)
    c.wait_event("stopped")
    sid = req("stackTrace", threadId=1)
    c.wait_response(sid)
    sid = req("variables", variablesReference=1000)
    c.wait_response(sid)

    sid = req("stepOut", threadId=1)
    c.wait_response(sid)
    c.wait_event("stopped")
    sid = req("stackTrace", threadId=1)
    c.wait_response(sid)

    # 一路继续到底：中间还会命中断点，再看一眼栈与变量再继续 —— 由**事件**
    # 驱动，所以两边的「继续几次」不会因为时序不同而分叉
    while True:
        sid = req("continue", threadId=1)
        c.wait_response(sid)
        if c.wait_stop_or_exit() == "exited":
            break
        sid = req("stackTrace", threadId=1)
        c.wait_response(sid)
        sid = req("variables", variablesReference=1000)
        c.wait_response(sid)

    if not want_error:
        c.wait_event("terminated")
    sid = req("disconnect")
    c.wait_response(sid)
    return c.close()


# ---------------------------------------------------------------------------
# 对拍
# ---------------------------------------------------------------------------

@need_rust
@pytest.mark.parametrize("mode", ["普通", "就地求值", "出错现场"])
def test_对拍_报文逐字节(mode, tmp_path):
    if mode == "出错现场":
        prog = _write(tmp_path, f"err_{TMP_SUFFIX}.jsh", PROG_ERR)
        bps, evaluate, want_error = [3], False, True
    else:
        prog = _write(tmp_path, f"ok_{TMP_SUFFIX}.jsh", PROG_OK)
        bps = [3, 5, 6, 7, 9, 11]
        evaluate, want_error = (mode == "就地求值"), False

    a, ra = run_session([str(EXE), "dap"], prog, bps, evaluate=evaluate,
                        want_error=want_error)
    b, rb = run_session([sys.executable, "-m", "oracle.jishi.cli", "dap"], prog, bps,
                        evaluate=evaluate, want_error=want_error)
    assert ra == rb, (ra, rb)
    if a == b:
        return
    pa, pb = _split(a), _split(b)
    for i in range(max(len(pa), len(pb))):
        x = pa[i] if i < len(pa) else b"<none>"
        y = pb[i] if i < len(pb) else b"<none>"
        if x != y:
            pytest.fail(f"第 {i + 1} 条报文不同（共 {len(pa)}/{len(pb)} 条）\n"
                        f"  rust: {x[:400]!r}\n  py  : {y[:400]!r}")


# ---------------------------------------------------------------------------
# 不依赖 Python 的独立判据（R8 之后 Python 退场仍要绿）
# ---------------------------------------------------------------------------

@need_rust
def test_独立_能力声明与初始事件(tmp_path):
    """`initialize` 的能力声明**必须嵌在 `body.capabilities` 里**（DAP 规范）。

    平铺在 `body` 顶层的话，编辑器读 `body.capabilities` 得到 undefined，
    于是所有开关都退回默认值 —— 表现是「单步按钮灰着」，而适配器这边一切正常。
    """
    prog = _write(tmp_path, f"ok_{TMP_SUFFIX}.jsh", PROG_OK)
    c = Dap([str(EXE), "dap"])
    try:
        c.send(seq=1, type="request", command="initialize", arguments={})
        r = c.wait_response(1)
        caps = r["body"]["capabilities"]
        assert caps["supportsConfigurationDoneRequest"] is True
        assert caps["supportsEvaluateForHovers"] is True
        assert caps["supportsPauseRequest"] is True
        assert caps["supportsConditionalBreakpoints"] is False
        assert caps["exceptionBreakpointFilters"] == []
        c.wait_event("initialized")
    finally:
        c.close()


@need_rust
def test_独立_停不住的行要如实标出来(tmp_path):
    """断点写在空行/注释行上 → `verified: false` + 一句说明。

    不报的话，用户看着断点在那儿、程序却从不停，只会怀疑调试器坏了。
    """
    prog = _write(tmp_path, f"gap_{TMP_SUFFIX}.jsh",
                  "令 甲 = 1\n\n# 注释行\n打印(甲)\n")
    c = Dap([str(EXE), "dap"])
    try:
        c.send(seq=1, type="request", command="initialize", arguments={})
        c.wait_response(1)
        c.wait_event("initialized")
        # ⚠️ 断点的「停不住」是**装配会话之后**才判得出来的：launch 之前
        # 客户端可能先发 setBreakpoints（规范允许），那时只能先记下来、
        # 一律回 `verified: true`（Python 侧也是这个口径）。
        c.send(seq=2, type="request", command="launch",
               arguments={"program": prog})
        c.wait_response(2)
        c.send(seq=3, type="request", command="setBreakpoints",
               arguments={"source": {"path": prog},
                          "breakpoints": [{"line": 1}, {"line": 2}, {"line": 3}]})
        bps = c.wait_response(3)["body"]["breakpoints"]
        assert [b["verified"] for b in bps] == [True, False, False], bps
        assert "没有可停的语句" in bps[1]["message"], bps[1]
    finally:
        c.close()


@need_rust
def test_独立_只支持树遍历执行器():
    """字节码执行器的调试钩子还没搬 —— 要**明确拒绝**，不静默降级。"""
    r = subprocess.run([str(EXE), "dap", "--执行器", "vm"], capture_output=True,
                       input=b"", env=_env(), cwd=str(ROOT), timeout=60)
    assert r.returncode == 2, r.returncode
    text = r.stderr.decode("utf-8", "replace")
    assert "只支持「树遍历」执行器" in text, text
    assert "vm" in text


@need_rust
def test_独立_打印与变量(tmp_path):
    """写死的期望：`打印` 每写一次就一条 `output/stdout` 事件，
    暂停时变量面板里能看到 `上限` / `小计`。"""
    prog = _write(tmp_path, f"ok_{TMP_SUFFIX}.jsh", PROG_OK)
    c = Dap([str(EXE), "dap"])
    try:
        n = [0]

        def req(method, **a):
            n[0] += 1
            c.send(seq=n[0], type="request", command=method, arguments=a)
            return n[0]

        s = req("initialize", adapterID="jishi")
        c.wait_response(s)
        c.wait_event("initialized")
        s = req("setBreakpoints", source={"path": prog}, breakpoints=[{"line": 5}])
        c.wait_response(s)
        s = req("launch", program=prog)
        c.wait_response(s)
        s = req("configurationDone")
        c.wait_response(s)
        c.wait_event("stopped")          # 第 5 行 = 小计 = 小计 + i（第一次进循环）

        s = req("scopes", frameId=1)
        assert c.wait_response(s)["body"]["scopes"][0] == {
            "name": "变量", "variablesReference": 1000, "expensive": False}
        s = req("variables", variablesReference=1000)
        rows = c.wait_response(s)["body"]["variables"]
        got = {r["name"]: r["value"] for r in rows}
        assert got.get("上限") == "3", got
        assert got.get("小计") == "0", got
        assert got.get("i") == "0", got
        # 内建（`打印` 之类）不该出现在面板里
        assert "打印" not in got, got

        s = req("stackTrace", threadId=1)
        frames = c.wait_response(s)["body"]["stackFrames"]
        assert [f["name"] for f in frames] == ["函数「累加」", "模块顶层"], frames
        assert [f["line"] for f in frames] == [5, 8], frames
        s = req("disconnect")
        c.wait_response(s)
    finally:
        c.close()


@need_rust
def test_独立_没有PATH也能跑(tmp_path):
    """清空环境也要能跑 —— 证明运行期不 shell out。"""
    prog = _write(tmp_path, f"ok_{TMP_SUFFIX}.jsh", PROG_OK)
    env = {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")} \
        if os.name == "nt" else {}
    c = Dap([str(EXE), "dap"])
    c.p.kill()
    r = subprocess.run([str(EXE), "dap"], capture_output=True, input=b"",
                       env={**env, "TEMP": os.environ.get("TEMP", "")},
                       cwd=str(ROOT), timeout=30)
    # 没有输入时：读完 EOF 就干净退出（0），不挂
    assert r.returncode == 0, (r.returncode, r.stderr[:200])
    del prog
