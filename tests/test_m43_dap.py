# -*- coding: utf-8 -*-
"""M43 · DAP（Debug Adapter Protocol）协议级测试。

调试器**本体**由 M37 / M39 / M40 测过了（三执行器会话记录逐字对拍）；这里只测
**协议这一层**：消息分帧对不对、请求与应答配不配得上、事件该在什么时候发、
状态机的边界（没 launch 就来问栈、连点两次继续、程序跑完再来求值……）。

做法是**真的起一个 `jishi dap` 子进程**按 DAP 说话，而不是在进程内调适配器——
分帧与线程模型正是最容易出错的地方，绕过它们测就等于没测。

实测踩到的一个坑写在这里：收消息**必须放在独立线程 + 队列**里。第一版直接在
主线程 `readline()`，于是「超时」判据根本不生效（阻塞在 read 上），子进程一退
出就被误判成超时——查了半天才发现是测试脚手架的问题。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 测试用的目标脚本（行号是有意的：第 3 行在函数里，第 5/6 行在模块层，第 4 行空行）
SRC = (
    "函数 倍(数)：\n"
    "    令 果 = 数 * 2\n"
    "    返回 果\n"
    "\n"
    "令 甲 = 倍(3)\n"
    "令 乙 = 倍(甲)\n"
    "打印(乙)\n"
)


def _cvm_available() -> bool:
    try:
        from jishi import cvm_bind

        cvm_bind.load_lib()
        return True
    except Exception:                       # noqa: BLE001 - 没编 C 内核就跳过
        return False


class DapClient:
    """一个真的 `jishi dap` 子进程 + DAP 收发小工具。"""

    def __init__(self, engine: "str | None" = None, cwd: Path = ROOT):
        cmd = [sys.executable, "-m", "jishi.cli", "dap"]
        if engine:
            cmd += ["--执行器", engine]
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(cwd), env=env)
        self.events: list = []
        self.responses: list = []
        self._q: "queue.Queue" = queue.Queue()
        self._seq = 0
        threading.Thread(target=self._read_loop, daemon=True).start()

    # -- 收发 -------------------------------------------------------------

    def _read_loop(self) -> None:
        out = self.proc.stdout
        while True:
            headers = {}
            while True:
                line = out.readline()
                if not line:                # 对端关了
                    self._q.put(None)
                    return
                line = line.strip()
                if not line:
                    break
                name, _, value = line.partition(b":")
                headers[name.strip().lower()] = value.strip()
            try:
                size = int(headers.get(b"content-length") or 0)
            except ValueError:
                self._q.put(None)
                return
            body = out.read(size)
            if not body:
                self._q.put(None)
                return
            try:
                self._q.put(json.loads(body.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._q.put(None)
                return

    def send(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body))
        self.proc.stdin.write(body)
        self.proc.stdin.flush()

    def recv(self, timeout: float = 10.0):
        """取一条消息。超时返回字符串 `"TIMEOUT"`，对端关闭返回 None。"""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return "TIMEOUT"

    def wait(self, kind: str, name: str, timeout: float = 10.0):
        """等到指定的 response / event；顺路把途经的事件记进 `self.events`。"""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return "TIMEOUT"
            msg = self.recv(timeout=left)
            if msg in (None, "TIMEOUT"):
                return msg
            if msg.get("type") == "event":
                self.events.append(msg)
            elif msg.get("type") == "response":
                self.responses.append(msg)
            if msg.get("type") == kind:
                key = "command" if kind == "response" else "event"
                if msg.get(key) == name:
                    return msg

    def request(self, command: str, arguments: "dict | None" = None) -> dict:
        """发请求，返回应答（不检查 success —— 有的测试就是来看失败的）。"""
        self._seq += 1
        self.send({"seq": self._seq, "type": "request", "command": command,
                   "arguments": arguments or {}})
        return self.wait("response", command)

    def event_names(self) -> list:
        return [e["event"] for e in self.events]

    def console(self) -> str:
        return "".join(e["body"]["output"] for e in self.events
                       if e["event"] == "output")

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:                   # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        return self.proc.returncode


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------

@pytest.fixture
def dap():
    client = DapClient()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "目标.jsh"
    p.write_text(SRC, encoding="utf-8", newline="\n")
    return p


def 开一场调试(client, script, *, lines=(3,), stop_on_entry=False,
               engine=None) -> dict:
    """初始化 → launch → 下断点 → configurationDone，返回第一个 stopped 事件。"""
    init = client.request("initialize", {"clientID": "pytest",
                                         "adapterID": "jishi"})
    assert init.get("success") is True, init
    client.wait("event", "initialized")
    launch = client.request("launch", {"program": str(script),
                                       "stopOnEntry": stop_on_entry})
    assert launch.get("success") is True, launch
    client.request("setBreakpoints", {
        "source": {"path": str(script)},
        "breakpoints": [{"line": n} for n in lines]})
    client.request("configurationDone")
    return client.wait("event", "stopped")


# ---------------------------------------------------------------------------
# 1. 握手与能力声明
# ---------------------------------------------------------------------------

def test_initialize_declares_capabilities_and_signals_initialized(dap):
    """能力声明要**诚实**：说不支持的，编辑器就不会给你画出没用的按钮。

    注意取的是 `body.capabilities`——**规范要求嵌这一层**。平铺在 body 顶层
    的话编辑器读不到（它固定读 capabilities），所有开关退化成默认值；
    这条测试原来就是照着实现的写法写的，于是「实现和测试一起错」，
    直到拿一个独立客户端跑真实场景才发现。
    """
    init = dap.request("initialize", {"clientID": "pytest", "adapterID": "jishi"})
    assert init["success"] is True
    caps = init["body"]["capabilities"]
    assert caps["supportsConfigurationDoneRequest"] is True
    assert caps["supportsEvaluateForHovers"] is True
    assert caps["supportsPauseRequest"] is True
    # 会话里确实做不到的，一律声明 false（不假装）
    assert caps["supportsSetVariable"] is False
    assert caps["supportsFunctionBreakpoints"] is False
    assert caps["supportsConditionalBreakpoints"] is False
    assert caps["supportsStepBack"] is False
    assert caps["exceptionBreakpointFilters"] == []
    # 规范：应答之后紧接着发 initialized，客户端才知道可以配断点了
    assert dap.wait("event", "initialized") != "TIMEOUT"


def test_initialize_body_shape_follows_spec(dap):
    """`initialize` 的 body **只能**有 `capabilities` 这一层（规范就这么定的）。

    这条是防「又平铺回去」的：上一条测试读的是 `body.capabilities`，
    但它不检查 body 里还有没有别的键——万一有人把能力项既嵌一层又平铺一份，
    上一条依然会过。
    """
    init = dap.request("initialize", {"clientID": "pytest", "adapterID": "jishi"})
    body = init["body"]
    assert list(body.keys()) == ["capabilities"], body.keys()
    assert isinstance(body["capabilities"], dict)
    assert "supportsConfigurationDoneRequest" not in body   # 不许平铺


def test_unknown_request_gets_empty_success(dap):
    """不认识的请求回空应答——回 error 会让某些客户端直接报「调试器坏了」。"""
    dap.request("initialize", {})
    r = dap.request("这个请求不存在")
    assert r["success"] is True


# ---------------------------------------------------------------------------
# 2. launch 的参数校验（错在哪儿要说清楚）
# ---------------------------------------------------------------------------

def test_configuration_done_without_launch_gets_exactly_one_response(dap):
    """没 launch 就 configurationDone：要**只回一个失败响应**，不是两个。

    DAP 规定一个请求只能有一个响应。原来写成「先回成功、再报错」，于是多数
    客户端取第一条（成功），那条「还没 launch」的提示**永远显示不出来**——
    用户看到的只是「点了没反应」，而适配器这边看不出任何异常。
    """
    dap.request("initialize", {})
    dap.wait("event", "initialized")
    first = dap.request("configurationDone")        # 故意不 launch
    assert first["success"] is False
    assert "launch" in first["message"]
    # 紧接着不该再冒出第二个响应
    second = dap.recv(timeout=3)
    assert not (isinstance(second, dict)
                and second.get("type") == "response"), second


def test_launch_without_program_says_what_is_missing(dap):
    dap.request("initialize", {})
    r = dap.request("launch", {})
    assert r["success"] is False
    assert "program" in r["message"] and "launch.json" in r["message"]


def test_launch_missing_file_reports_chinese_error(dap, tmp_path):
    dap.request("initialize", {})
    r = dap.request("launch", {"program": str(tmp_path / "不存在.jsh")})
    assert r["success"] is False
    assert "找不到文件" in r["message"]


def test_launch_bad_engine_is_rejected(dap, script):
    dap.request("initialize", {})
    r = dap.request("launch", {"program": str(script), "执行器": "c++"})
    assert r["success"] is False
    assert "执行器" in r["message"]


def test_launch_syntax_error_is_reported_with_hint(dap, tmp_path):
    """语法错的脚本要在 launch 就说，而不是等 configurationDone 之后静默不动。"""
    bad = tmp_path / "坏.jsh"
    bad.write_text("函数 (：\n", encoding="utf-8", newline="\n")
    dap.request("initialize", {})
    r = dap.request("launch", {"program": str(bad)})
    assert r["success"] is False
    assert "第 1 行" in r["message"]


def test_attach_is_refused_with_reason(dap):
    """本适配器只支持 launch：程序得由它自己启动才挂得上断点。"""
    dap.request("initialize", {})
    r = dap.request("attach", {})
    assert r["success"] is False
    assert "launch" in r["message"]


# ---------------------------------------------------------------------------
# 3. 断点
# ---------------------------------------------------------------------------

def test_breakpoint_stops_and_reports_stack(dap, script):
    stopped = 开一场调试(dap, script, lines=(3,))
    assert stopped != "TIMEOUT", "没等到 stopped"
    body = stopped["body"]
    assert body["reason"] == "breakpoint"
    assert body["threadId"] == 1
    assert "第 3 行" in body["description"]      # 中文原话带给编辑器

    st = dap.request("stackTrace", {"threadId": 1})
    frames = st["body"]["stackFrames"]
    assert [f["name"] for f in frames] == ["函数「倍」", "模块顶层"]
    assert [f["line"] for f in frames] == [3, 5]     # DAP 的 line 是 1 基
    assert frames[0]["source"]["path"].endswith("目标.jsh")


def test_breakpoint_on_blank_line_is_unverified(dap, script):
    """断点落在空行上不算错，但**必须说出来**——否则用户以为调试器坏了。"""
    dap.request("initialize", {})
    dap.request("launch", {"program": str(script)})
    r = dap.request("setBreakpoints", {
        "source": {"path": str(script)},
        "breakpoints": [{"line": 2}, {"line": 4}]})   # 第 4 行是空行
    bps = r["body"]["breakpoints"]
    assert bps[0] == {"verified": True, "line": 2}
    assert bps[1]["verified"] is False
    assert "没有可停的语句" in bps[1]["message"]


def test_set_breakpoints_replaces_instead_of_appending(dap, script):
    """DAP 的 setBreakpoints 是**替换**语义：再设一次就该只剩新的那些。"""
    开一场调试(dap, script, lines=(3,))
    dap.request("continue", {"threadId": 1})
    dap.wait("event", "stopped")                 # 第 6 行的断点？没设，所以是跑完
    # 清空断点后再跑一遍：第 3 行不该再停
    dap.request("setBreakpoints", {"source": {"path": str(script)},
                                   "breakpoints": []})
    dap.request("continue", {"threadId": 1, "singleThread": True})


# ---------------------------------------------------------------------------
# 4. 变量与求值
# ---------------------------------------------------------------------------

def test_variables_of_current_and_outer_frame(dap, script):
    """能点任意一帧看变量——这正是「按层取帧」接口（_frame_target_at）的用途。"""
    开一场调试(dap, script, lines=(3,))
    st = dap.request("stackTrace", {"threadId": 1})
    frames = st["body"]["stackFrames"]

    sc = dap.request("scopes", {"frameId": frames[0]["id"]})
    ref = sc["body"]["scopes"][0]["variablesReference"]
    assert ref >= 1000                      # 自定义引用（0 是「没有子项」）
    va = dap.request("variables", {"variablesReference": ref})
    got = {v["name"]: (v["value"], v["type"]) for v in va["body"]["variables"]}
    assert got["数"] == ("3", "整数")
    assert got["果"] == ("6", "整数")

    sc2 = dap.request("scopes", {"frameId": frames[1]["id"]})
    va2 = dap.request("variables", {
        "variablesReference": sc2["body"]["scopes"][0]["variablesReference"]})
    names = [v["name"] for v in va2["body"]["variables"]]
    assert "倍" in names                    # 模块顶层自己定义的函数
    assert "打印" not in names              # 内建默认藏起来（与命令行版同一口径）


def test_variables_of_bogus_reference_is_empty(dap):
    dap.request("initialize", {})
    r = dap.request("variables", {"variablesReference": 0})
    assert r["body"]["variables"] == []


def test_evaluate_in_frame(dap, script):
    开一场调试(dap, script, lines=(3,))
    r = dap.request("evaluate", {"expression": "数 * 10", "context": "watch"})
    assert r["success"] is True
    assert r["body"]["result"] == "30"
    assert r["body"]["type"] == "整数"


def test_evaluate_error_has_no_bogus_line_number(dap, script):
    """求值出错时不写「第 1 行」——那是**表达式自己**的行号，对用户是误导。"""
    开一场调试(dap, script, lines=(3,))
    r = dap.request("evaluate", {"expression": "没有这个名字"})
    assert r["success"] is False
    assert "没有这个名字" in r["message"]
    assert "第 1 行" not in r["message"]


def test_evaluate_before_any_pause_is_refused(dap, script):
    """程序在跑的时候求值没有意义，要明确说，不能给个错值。"""
    dap.request("initialize", {})
    dap.request("launch", {"program": str(script), "stopOnEntry": False})
    r = dap.request("evaluate", {"expression": "1 + 1"})
    assert r["success"] is False
    assert "没停在断点上" in r["message"]


# ---------------------------------------------------------------------------
# 5. 单步与继续
# ---------------------------------------------------------------------------

def test_next_steps_over_function_calls(dap, script):
    """「下一步」不下钻：第 3 行（返回）之后应停在模块层的第 6 行。"""
    开一场调试(dap, script, lines=(6,))
    dap.request("next", {"threadId": 1})
    stopped = dap.wait("event", "stopped")
    assert stopped["body"]["description"] == "下一步"
    st = dap.request("stackTrace", {"threadId": 1})
    assert [f["name"] for f in st["body"]["stackFrames"]] == ["模块顶层"]
    # 第 6 行执行完 → 停在下一条语句（第 7 行的 `打印(乙)`）
    assert st["body"]["stackFrames"][0]["line"] == 7


def test_step_in_enters_function(dap, script):
    开一场调试(dap, script, lines=(5,))
    dap.request("stepIn", {"threadId": 1})
    dap.wait("event", "stopped")
    st = dap.request("stackTrace", {"threadId": 1})
    names = [f["name"] for f in st["body"]["stackFrames"]]
    assert names[0] == "函数「倍」"          # 「单步」钻进函数里了


def test_step_out_returns_to_caller(dap, script):
    开一场调试(dap, script, lines=(3,))       # 停在函数里
    dap.request("stepOut", {"threadId": 1})
    dap.wait("event", "stopped")
    st = dap.request("stackTrace", {"threadId": 1})
    assert [f["name"] for f in st["body"]["stackFrames"]] == ["模块顶层"]


def test_continue_while_running_is_ignored(dap, tmp_path):
    """程序**正在跑**的时候发来的「继续」，只该被应答——不发事件、更不入队。

    入了队就会攒下来，等下次命中断点时被立刻吃掉，表现是「断点失灵」，
    而且极难排查。

    这里用**无限循环**把「正在跑」这个状态稳住：程序跑得比消息往返快得多
    （30 万次循环只要 0.3 秒），想靠「刚 continue 完那一瞬间」构造出这个
    窗口是不可靠的——第一版就是这么写的，结果测了个寂寞。
    """
    loop = tmp_path / "循环.jsh"
    loop.write_text(
        "令 和 = 0\n"
        "当 真：\n"
        "    和 = 和 + 1\n",
        encoding="utf-8", newline="\n")
    dap.request("initialize", {})
    dap.request("launch", {"program": str(loop), "stopOnEntry": False})
    dap.request("configurationDone")
    for _ in range(2):
        assert dap.request("continue", {"threadId": 1})["success"] is True
    assert dap.event_names().count("continued") == 0, dap.event_names()

    # 接着「暂停」能立刻停下：说明那两下确实没进队列（否则会立刻被消费掉）
    dap.request("pause", {"threadId": 1})
    stopped = dap.wait("event", "stopped", timeout=15)
    assert stopped != "TIMEOUT" and stopped["body"]["reason"] == "pause"

    # 程序还在跑的时候断开：要走「退出」这条路干净收场（不是硬杀进程）
    assert dap.request("disconnect")["success"] is True
    assert dap.close() == 0


def test_pause_while_running_stops_at_next_statement(dap, tmp_path):
    """运行中点「暂停」：在下一个语句边界停下（调试器本来就是按语句停的）。"""
    loop = tmp_path / "循环.jsh"
    loop.write_text(
        "令 和 = 0\n"
        "遍历 i 在 范围(300000)：\n"
        "    和 = 和 + i\n"
        "打印(和)\n",
        encoding="utf-8", newline="\n")
    dap.request("initialize", {})
    dap.request("launch", {"program": str(loop), "stopOnEntry": False})
    dap.request("configurationDone")
    r = dap.request("pause", {"threadId": 1})
    assert r["success"] is True
    stopped = dap.wait("event", "stopped", timeout=15)
    assert stopped != "TIMEOUT", "暂停请求没让程序停下来"
    assert stopped["body"]["reason"] == "pause"
    assert "暂停" in stopped["body"]["description"]


# ---------------------------------------------------------------------------
# 6. 输出、结束与退出码
# ---------------------------------------------------------------------------

def test_program_output_goes_to_console(dap, script):
    """程序自己打印的东西走 output 事件（stdout 分类），不能被当成协议数据。"""
    开一场调试(dap, script, lines=(3,))
    dap.request("setBreakpoints", {"source": {"path": str(script)},
                                   "breakpoints": []})
    dap.request("continue", {"threadId": 1})
    dap.wait("event", "terminated", timeout=15)
    text = dap.console()
    assert "12" in text                      # `打印(乙)` 的输出
    assert "暂停（命中第 3 行的断点）" in text   # 调试器自己的话也在（console 分类）
    cats = {e["body"]["category"] for e in dap.events if e["event"] == "output"}
    assert "stdout" in cats and "console" in cats


def test_console_has_no_cli_chatter(dap, script):
    """编辑器里不该出现命令行专属的寒暄（「输入「帮助」看命令」）。

    那条提示是给终端用户的；编辑器靠按钮操作、没有「输入命令」这回事，
    打出来纯属噪音——而且它混在程序输出里，用户会以为那是程序打印的。
    靠会话的 `interactive=False` 关掉；命令行那条路仍照打。
    """
    开一场调试(dap, script, lines=(3,))
    dap.request("setBreakpoints", {"source": {"path": str(script)},
                                   "breakpoints": []})
    dap.request("continue", {"threadId": 1})
    dap.wait("event", "terminated", timeout=15)
    text = dap.console()
    assert "暂停（命中第 3 行的断点）" in text    # 暂停提示还在（有信息价值）
    assert "帮助" not in text                    # 但命令行的寒暄没了


def test_exit_code_is_one_when_program_fails(dap, tmp_path):
    boom = tmp_path / "出错.jsh"
    boom.write_text("令 甲 = 1\n打印(甲 / 0)\n", encoding="utf-8", newline="\n")
    dap.request("initialize", {})
    dap.request("launch", {"program": str(boom), "stopOnEntry": False})
    dap.request("configurationDone")
    dap.wait("event", "terminated", timeout=15)
    exited = [e["body"] for e in dap.events if e["event"] == "exited"]
    assert exited and exited[0]["exitCode"] == 1
    assert "除" in dap.console() or "零" in dap.console()


def test_stop_on_entry(dap, script):
    dap.request("initialize", {})
    dap.request("launch", {"program": str(script), "stopOnEntry": True})
    dap.request("setBreakpoints", {"source": {"path": str(script)},
                                   "breakpoints": []})
    dap.request("configurationDone")
    stopped = dap.wait("event", "stopped")
    assert stopped["body"]["reason"] == "entry"


def test_disconnect_ends_the_process(dap, script):
    开一场调试(dap, script, lines=(3,))
    r = dap.request("disconnect")
    assert r["success"] is True
    assert dap.close() == 0


# ---------------------------------------------------------------------------
# 7. 三个执行器行为一致（DAP 这一层也要一致）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine", ["树遍历", "vm", "cvm"])
def test_three_executors_agree(engine, tmp_path):
    """同一脚本 + 同一组断点，三个执行器的**协议交互结果**逐项相同。

    求值例外：C 虚拟机明确回「暂不支持就地求值」，所以这里不比 evaluate。
    """
    if engine == "cvm" and not _cvm_available():
        pytest.skip("C 内核没编译（python cvm/build.py）")
    p = tmp_path / "目标.jsh"
    p.write_text(SRC, encoding="utf-8", newline="\n")
    client = DapClient(engine=engine)
    try:
        stopped = 开一场调试(client, p, lines=(3,))
        assert stopped["body"]["description"] == "命中第 3 行的断点"
        st = client.request("stackTrace", {"threadId": 1})
        assert [f["name"] for f in st["body"]["stackFrames"]] == ["函数「倍」", "模块顶层"]
        assert [f["line"] for f in st["body"]["stackFrames"]] == [3, 5]
        va = client.request("variables", {"variablesReference": 1000})
        assert [(v["name"], v["value"]) for v in va["body"]["variables"]] == \
            [("数", "3"), ("果", "6")]
        client.request("setBreakpoints", {"source": {"path": str(p)},
                                          "breakpoints": []})
        client.request("continue", {"threadId": 1})
        client.wait("event", "terminated", timeout=15)
        assert client.event_names() == [
            "initialized", "stopped", "output", "output", "output",
            "continued", "output", "output", "output", "exited", "terminated"]
        assert client.console() == (
            "\n暂停（命中第 3 行的断点） 第 3 行 · 函数「倍」\n"
            "→ 3 │     返回 果\n"
            "12\n（程序正常结束，本次共暂停 1 次）\n")
    finally:
        client.close()


def test_cvm_evaluate_says_why_not_supported(tmp_path):
    """C 虚拟机不支持就地求值——要说原因与替代方案，不能给错值。"""
    if not _cvm_available():
        pytest.skip("C 内核没编译（python cvm/build.py）")
    p = tmp_path / "目标.jsh"
    p.write_text(SRC, encoding="utf-8", newline="\n")
    client = DapClient(engine="cvm")
    try:
        开一场调试(client, p, lines=(3,))
        r = client.request("evaluate", {"expression": "数 * 10"})
        assert r["success"] is False
        assert "暂不支持就地求值" in r["message"]
        assert "--执行器 vm" in r["message"]        # 给出替代方案
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 8. 扩展声明与适配器的契约（改一边忘一边 = 编辑器里「点了没反应」）
# ---------------------------------------------------------------------------

PKG = ROOT / "editors" / "vscode-jishi" / "package.json"
EXT = ROOT / "editors" / "vscode-jishi" / "extension.js"


def test_vscode_extension_declares_debugger():
    """扩展要真的把调试器接上：package.json 声明 + extension.js 注册工厂。

    M42 的教训是「改 LSP 后要同步三处」，调试这边同理——只改服务端，
    VS Code 里就没有「基石调试」这个选项；只改 package.json，点了没反应。
    """
    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    debuggers = pkg["contributes"]["debuggers"]
    assert [d["type"] for d in debuggers] == ["jishi"]
    assert debuggers[0]["languages"] == ["jishi"]
    assert "onDebugResolve:jishi" in pkg["activationEvents"]

    props = debuggers[0]["configurationAttributes"]["launch"]["properties"]
    # launch.json 里能配的键，要和 dap.py 里读的键对得上
    assert {"program", "stopOnEntry", "执行器"} <= set(props)
    assert props["执行器"]["enum"] == ["树遍历", "vm", "cvm"]

    js = EXT.read_text(encoding="utf-8")
    assert "registerDebugAdapterDescriptorFactory('jishi'" in js
    assert "'dap'" in js                       # 起的是 `jishi dap` 子进程
    assert "registerDebugConfigurationProvider('jishi'" in js


def test_dap_cli_rejects_unknown_engine():
    """`--执行器` 的取值由 argparse 兜住；报错里要把可选值列出来。"""
    r = subprocess.run(
        [sys.executable, "-m", "jishi.cli", "dap", "--执行器", "c++"],
        capture_output=True, cwd=str(ROOT))
    assert r.returncode != 0
    err = r.stderr.decode("utf-8", "replace")
    assert "树遍历" in err and "cvm" in err


def test_dap_is_reachable_from_cli():
    """`jishi dap` 要能跑，也要在 cli 的用法说明里露面（别只藏在源码里）。"""
    r = subprocess.run([sys.executable, "-m", "jishi.cli", "dap", "--help"],
                       capture_output=True, cwd=str(ROOT))
    assert r.returncode == 0
    assert "在哪个执行器上调试" in r.stdout.decode("utf-8", "replace")
    cli_src = (ROOT / "jishi" / "cli.py").read_text(encoding="utf-8")
    assert "jishi dap" in cli_src
    assert '"dap"' in cli_src               # 工具子命令的分发列表里要有它
