# -*- coding: utf-8 -*-
"""Debug Adapter Protocol（DAP）：把基石调试器接进编辑器（M43 · 方向二）。

编辑器（VS Code 等）通过 DAP 跟「调试适配器」说话。传输方式与 LSP 完全一样
（``Content-Length: N\\r\\n\\r\\n{JSON}``），差别在消息种类：DAP 有 request /
response / event 三种，而且**事件随时可以发**（不必等谁先问）。

用法：``jishi dap`` —— 从标准输入收 DAP 消息，应答写到标准输出。

线程模型（这个模块唯一需要动脑的地方）：

    主线程     DAP 消息循环：读请求、应答、发事件
    脚本线程   跑用户程序；在断点处暂停时**阻塞**在命令队列上等命令

为什么「主线程能直接读调试器状态」是安全的：暂停期间脚本线程只打印与读命令
（见 ``_Session._pause``），不碰执行状态，所以 ``_live_chain()`` /
``_vars_of()`` 读到的是一份静止的快照。``stopped`` 事件也因此在**暂停判定一
成立**就发出去（``_Session.on_pause`` 钩子），编辑器收到便来问栈与变量，
那时数据已经定了。

如实说明的边界（第一版，与命令行版口径一致）：

- **变量不展开**：列表/字典显示成一行文本（``[1, 2, 3]``），与「变量」命令
  同一口径——命令行版也是这样，不做「编辑器里能展开、命令行里不能」的分裂；
- **C 虚拟机不支持就地求值**：那边没有「在任意帧上求值」的入口，``evaluate``
  回一条带原因的 error，而不是给个错值；
- **没有条件断点 / 异常断点 / 函数断点**：命令行版也没有，这里不假装有；
- **只有一条线程**：三个执行器都是单线程跑，``threads`` 恒为一条。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .cli import build_debug_session
from .debugger import _DEAD_LINE_HINT
from .errors import JishiError
from .runtime import jishi_repr, type_name

#: 本适配器只有一条线程（三个执行器都是单线程跑）
THREAD_ID = 1
THREAD_NAME = "主线程"

#: 变量引用的编码：第 i 帧的作用域引用 = `_SCOPE_BASE + i`。
#: 0 按 DAP 规范表示「没有子项」，所以自定义引用从一个够大的数起。
_SCOPE_BASE = 1000

#: 程序跑完之后等客户端断开的时间（秒）。客户端一般会自己发 `disconnect`，
#: 但万一不发，适配器就成了僵尸进程——等这么久，然后自己收场。
_LINGER_SECONDS = 5.0


class _BadRequest(Exception):
    """请求本身不对（参数缺了/不对）。回一条 error 就行，别把适配器掀翻。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _reason_for_dap(reason: str) -> str:
    """会话里的中文暂停原因 → DAP 的 reason（取值是固定的那几个英文词）。"""
    if "入口" in reason:
        return "entry"
    if "断点" in reason:
        return "breakpoint"
    if reason == "暂停":
        return "pause"
    return "step"                       # 单步 / 下一步 / 跳出


# ---------------------------------------------------------------------------
# 分帧
# ---------------------------------------------------------------------------

class _Framer:
    """DAP 的读写分帧（与 LSP 同一套；写的时候加锁，因为两个线程都会发消息）。"""

    def __init__(self, inp, out):
        self._in = inp
        self._out = out
        self._lock = threading.Lock()

    def read(self) -> Optional[dict]:
        """读一帧；对端关闭（EOF）或帧坏了都返回 None。"""
        headers: dict[bytes, bytes] = {}
        while True:
            line = self._in.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break                    # 空行 = 头结束
            name, _, value = line.partition(b":")
            headers[name.strip().lower()] = value.strip()
        try:
            size = int(headers.get(b"content-length") or 0)
        except ValueError:
            return None
        if size <= 0:
            return None
        body = self._in.read(size)
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def write(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        with self._lock:
            try:
                self._out.write(b"Content-Length: %d\r\n\r\n" % len(body))
                self._out.write(body)
                self._out.flush()
            except (OSError, ValueError):
                # 编辑器没了（管道断了）：静默收场，别在退出路径上再抛异常
                pass


class _StreamOut:
    """被调试程序的 stdout / stderr → DAP 的 output 事件。

    `打印` 走的是 `sys.stdout`，所以脚本线程启动前把它换成这个，编辑器的
    「调试控制台」里就能看到程序输出（与调试器自己的话分开着色）。
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, adapter: "DapAdapter", category: str):
        self._adapter = adapter
        self._category = category

    def write(self, text: Any) -> int:
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8", "replace")
        text = str(text)
        if text:
            self._adapter.output(text, self._category)
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    @property
    def buffer(self) -> "_StreamOut":
        # 有代码习惯走 `sys.stdout.buffer.write(b"…")`，写回自己即可
        return self


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------

class DapAdapter:
    """一次 DAP 会话。"""

    #: DAP 请求 → 处理方法
    HANDLERS = {
        "initialize": "_req_initialize",
        "launch": "_req_launch",
        "attach": "_req_attach",
        "setBreakpoints": "_req_set_breakpoints",
        "setFunctionBreakpoints": "_req_no_breakpoints",
        "setExceptionBreakpoints": "_req_no_breakpoints",
        "configurationDone": "_req_configuration_done",
        "threads": "_req_threads",
        "stackTrace": "_req_stack_trace",
        "scopes": "_req_scopes",
        "variables": "_req_variables",
        "evaluate": "_req_evaluate",
        "source": "_req_source",
        "continue": "_req_continue",
        "next": "_req_next",
        "stepIn": "_req_step_in",
        "stepOut": "_req_step_out",
        "pause": "_req_pause",
        "disconnect": "_req_disconnect",
        "terminate": "_req_terminate",
    }

    def __init__(self, *, engine: str = "树遍历", linger: float = _LINGER_SECONDS):
        self.engine = engine
        self.linger = linger
        self.framer = _Framer(getattr(sys.stdin, "buffer", sys.stdin),
                              getattr(sys.stdout, "buffer", sys.stdout))
        #: 启动配置（`launch` 里拿到的）
        self.config: dict = {}
        self.session: Optional[Any] = None
        self.thread: Optional[threading.Thread] = None
        #: 主线程往里放「继续 / 下一步 / …」，脚本线程在断点处取
        self._commands: "queue.Queue[str]" = queue.Queue()
        #: 保护 paused 与命令入队的原子性（两个线程都碰）
        self._state_lock = threading.Lock()
        #: 脚本线程是否停在断点处（主线程据此判断「状态能不能读」）
        self.paused = False
        self.started = False
        self.exited = False
        self.exit_code: Optional[int] = None
        self._finished = threading.Event()
        self._stopping = False
        self._seq = itertools.count(1)

    # -- 协议输出 -----------------------------------------------------------

    def respond(self, req: dict, body: Optional[dict] = None) -> None:
        self.framer.write({"seq": next(self._seq), "type": "response",
                           "request_seq": req.get("seq"), "success": True,
                           "command": req.get("command"), "body": body or {}})

    def error(self, req: dict, message: str) -> None:
        self.framer.write({"seq": next(self._seq), "type": "response",
                           "request_seq": req.get("seq"), "success": False,
                           "command": req.get("command"), "message": message})

    def event(self, name: str, body: Optional[dict] = None) -> None:
        self.framer.write({"seq": next(self._seq), "type": "event",
                           "event": name, "body": body or {}})

    def output(self, text: str, category: str = "console") -> None:
        """把一段文本送到编辑器的「调试控制台」。"""
        self.event("output", {"category": category, "output": text})

    # -- 主循环 -------------------------------------------------------------

    def serve(self) -> int:
        while not self._stopping:
            msg = self.framer.read()
            if msg is None:
                break                       # 对端关了：收工
            if msg.get("type") == "request":
                self._handle(msg)
        return 0

    def _handle(self, req: dict) -> None:
        name = self.HANDLERS.get(req.get("command") or "")
        if name is None:
            # 不认识的请求：空应答。回 error 会让有些客户端直接报「调试器坏了」，
            # 而 DAP 允许客户端试探性地发一些可选请求。
            self.respond(req, {})
            return
        try:
            getattr(self, name)(req)
        except _BadRequest as e:
            self.error(req, e.message)
        except SystemExit:
            raise
        except Exception as e:              # noqa: BLE001 - 兜底：别让适配器静默死掉
            self.error(req, f"调试适配器内部出错（{type(e).__name__}）：{e}")

    # -- 生命周期 -----------------------------------------------------------

    def _req_initialize(self, req: dict) -> None:
        # 能力声明必须**嵌在 `body.capabilities` 里**（DAP 规范）。
        # 平铺在 `body` 顶层的话，编辑器读 `body.capabilities` 得到 undefined，
        # 于是所有开关都退回默认值——表现是「单步按钮灰着」「悬停不求值」这类
        # 莫名其妙的现象，而适配器这边一切正常。**必须是这一层嵌套**。
        self.respond(req, {
            "capabilities": {
                "supportsConfigurationDoneRequest": True,
                "supportsEvaluateForHovers": True,
                "supportsPauseRequest": True,
                "supportsTerminateRequest": True,
                "supportTerminateDebuggee": True,
                # 下面这些**明说不支持**，别让编辑器以为点了有用
                "supportsFunctionBreakpoints": False,
                "supportsConditionalBreakpoints": False,
                "supportsHitConditionalBreakpoints": False,
                "supportsLogPoints": False,
                "supportsSetVariable": False,
                "supportsStepBack": False,
                "supportsRestartRequest": False,
                "exceptionBreakpointFilters": [],
            },
        })
        # 规范：应答之后紧接着发 `initialized`，客户端才知道可以配置断点了
        self.event("initialized")

    def _req_attach(self, req: dict) -> None:
        raise _BadRequest(
            "基石调试器只支持 launch（直接跑一个 .jsh 脚本），不支持 attach"
            "——程序要由本适配器自己启动，才能挂上断点")

    def _req_launch(self, req: dict) -> None:
        args = req.get("arguments") or {}
        program = args.get("program") or args.get("脚本") or args.get("文件")
        if not program:
            raise _BadRequest(
                "启动配置里少了 program（要调试的 .jsh 文件路径）。"
                "launch.json 里写成：{\"type\": \"jishi\", \"request\": \"launch\", "
                "\"name\": \"调试基石脚本\", \"program\": \"${file}\"}")
        engine = args.get("执行器") or args.get("engine") or self.engine
        if engine not in ("树遍历", "vm", "cvm"):
            raise _BadRequest(
                f"不认识的执行器「{engine}」—— 只能是 树遍历 / vm / cvm")

        source, lines = _read_program(str(program))
        stop_on_entry = bool(args.get("stopOnEntry") or args.get("停在入口"))
        try:
            session = build_debug_session(
                engine, source, str(program), lines,
                stop_on_entry=stop_on_entry,
                read_line=self._read_line,
                # 编辑器不是终端：别打「输入「帮助」看命令」这类寒暄
                interactive=False,
                write=lambda text: self.output(text + "\n", "console"),
            )
        except JishiError as e:
            raise _BadRequest(self._explain(e)) from None

        self.session = session
        self.config = {"program": str(program), "engine": engine}
        # 断点可能在 launch 之前就配过（有些客户端会那么发），补上
        if self._pending_breakpoints:
            self.session.add_breakpoints(self._pending_breakpoints)
            self._pending_breakpoints = []
        self.respond(req, {})

    def _req_configuration_done(self, req: dict) -> None:
        # 先把「没 launch」拦下来——**一个请求只能有一个响应**。
        # 原来写成「先 respond({}) 再检查」，于是这种情形会回两条响应
        # （先成功、后失败）：多数客户端取第一条，那条「还没 launch」的提示
        # 就永远显示不出来，用户看到的只是「点了没反应」。
        if self.session is None:
            self.error(req, "还没 launch：先说清楚要跑哪个脚本")
            return
        self.respond(req, {})
        self._start_script()

    def _start_script(self) -> None:
        if self.started:
            return
        self.started = True
        self.session.on_pause = self._on_pause
        self.thread = threading.Thread(target=self._run_script,
                                       name="jishi-debuggee", daemon=True)
        self.thread.start()

    def _run_script(self) -> None:
        """脚本线程：跑用户程序，输出重定向到调试控制台。"""
        code = 0
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _StreamOut(self, "stdout")
        sys.stderr = _StreamOut(self, "stderr")
        try:
            err = self.session.run()
            if err is not None:
                code = 1
                self.output(self._explain(err), "stderr")
        except SystemExit:
            raise
        except BaseException as e:          # noqa: BLE001 - 线程里别静默死掉
            code = 1
            self.output(f"调试器内部出错（{type(e).__name__}）：{e}", "stderr")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            self.exit_code = code
            self.exited = True
            with self._state_lock:
                self.paused = False
            self.event("exited", {"exitCode": code})
            self.event("terminated")
            self._finished.set()
            self._arm_linger_timer()

    def _arm_linger_timer(self) -> None:
        """程序跑完后再等一会儿客户端断开。

        编辑器一般会自动发 `disconnect`；不发的话适配器就挂着不走了——
        与其留个僵尸进程，不如到点自己收场。
        """
        if self.linger is None or self.linger < 0:
            return
        timer = threading.Timer(self.linger, self._quit_now)
        timer.daemon = True
        timer.start()

    @staticmethod
    def _quit_now() -> None:
        # 此刻脚本已经跑完，没有未落盘的缓冲（每条消息都 flush 过）
        os._exit(0)

    def _req_disconnect(self, req: dict) -> None:
        self.respond(req, {})
        self._shutdown()

    def _req_terminate(self, req: dict) -> None:
        self.respond(req, {})
        self._shutdown()

    def _shutdown(self) -> None:
        """让脚本线程走「退出」这条路（`最终` 块照跑），然后收工。"""
        with self._state_lock:
            paused = self.paused
        if paused:
            self._commands.put("退出")
        else:
            # 程序正在跑：请求它在下一个语句边界停下（那时就会读到「退出」）
            if self.session is not None and not self.exited:
                self.session.pause_requested = True
                self._commands.put("退出")
        self._finished.wait(timeout=2.0)
        self._stopping = True
        sys.exit(0)

    # -- 断点 ---------------------------------------------------------------

    def _req_set_breakpoints(self, req: dict) -> None:
        args = req.get("arguments") or {}
        lines = [int((b or {}).get("line") or 0)
                 for b in (args.get("breakpoints") or [])]
        lines = [n for n in lines if n > 0]

        if self.session is None:
            # 还没 launch：先记下来，装配会话时一起加
            self._pending_breakpoints = lines
            self.respond(req, {"breakpoints": [{"verified": True, "line": n}
                                               for n in lines]})
            return

        # DAP 的 setBreakpoints 是**替换**语义（这次给的集合就是全部），
        # 而会话层的 add_breakpoints 是追加 —— 所以先清空当前文件的断点。
        self.session.clear_breakpoints()
        usable, _dead = self.session.add_breakpoints(lines)
        result = []
        for n in lines:
            if n in usable:
                result.append({"verified": True, "line": n})
            else:
                # 「停不住」不是错误，但必须说出来：否则用户看着断点在那儿、
                # 程序却从不停，只会怀疑调试器坏了（与命令行版同一口径）。
                result.append({"verified": False, "line": n,
                               "message": f"第 {n} 行没有可停的语句"
                                          f"（{_DEAD_LINE_HINT}）"})
        self.respond(req, {"breakpoints": result})

    def _req_no_breakpoints(self, req: dict) -> None:
        """函数断点 / 异常断点：如实回空表（能力声明里已说不支持）。"""
        self.respond(req, {"breakpoints": []})

    # -- 读状态 -------------------------------------------------------------

    def _req_threads(self, req: dict) -> None:
        self.respond(req, {"threads": [{"id": THREAD_ID, "name": THREAD_NAME}]})

    def _req_stack_trace(self, req: dict) -> None:
        if self.session is None or not self.paused:
            self.respond(req, {"stackFrames": [], "totalFrames": 0})
            return
        chain = self.session._live_chain()
        frames = []
        for i, (line, label) in enumerate(chain):
            frames.append({
                "id": i + 1,            # frameId 只在一次暂停内有意义，从 1 起
                "name": label,
                "line": line,           # DAP 的 line 是 1 基（与 LSP 不同）
                "column": 1,
                "source": self._source_ref(),
            })
        self.respond(req, {"stackFrames": frames, "totalFrames": len(frames)})

    def _req_scopes(self, req: dict) -> None:
        index = self._frame_index(req)
        self.respond(req, {"scopes": [{
            "name": "变量",
            "variablesReference": _SCOPE_BASE + index,
            "expensive": False,
        }]})

    def _req_variables(self, req: dict) -> None:
        args = req.get("arguments") or {}
        ref = int(args.get("variablesReference") or 0)
        if ref < _SCOPE_BASE:
            self.respond(req, {"variables": []})
            return
        self.respond(req, {"variables": self._frame_variables(ref - _SCOPE_BASE)})

    def _req_evaluate(self, req: dict) -> None:
        args = req.get("arguments") or {}
        expr = str(args.get("expression") or "").strip()
        if not expr:
            raise _BadRequest("求值的表达式是空的")
        if self.session is None or not self.paused:
            raise _BadRequest("程序没停在断点上，现在没法求值")
        if not self.session._can_eval():
            raise _BadRequest("现在没有可求值的帧（程序还没执行到任何语句）")
        try:
            value = self.session._eval_expr(expr)
        except JishiError as e:
            # C 虚拟机的「暂不支持就地求值」也走这里：如实说原因，不给错值
            self.error(req, self._explain(e, with_line=False))
            return
        self.respond(req, {"result": jishi_repr(value, top=True),
                           "type": type_name(value),
                           "variablesReference": 0})

    def _req_source(self, req: dict) -> None:
        args = req.get("arguments") or {}
        want = args.get("source") or {}
        path = want.get("path")
        if self.session is None or (path and os.path.abspath(path)
                                    != os.path.abspath(self.session.filename)):
            raise _BadRequest(f"没有这个源文件：{path}")
        self.respond(req, {"content": "\n".join(self.session.lines)})

    # -- 继续 ---------------------------------------------------------------

    def _req_continue(self, req: dict) -> None:
        self._resume(req, "继续")

    def _req_next(self, req: dict) -> None:
        self._resume(req, "下一步")

    def _req_step_in(self, req: dict) -> None:
        self._resume(req, "单步")

    def _req_step_out(self, req: dict) -> None:
        self._resume(req, "跳出")

    def _resume(self, req: dict, command: str) -> None:
        """把一个「继续类」命令交给脚本线程。

        注意只在**确实停着**的时候塞命令：用户连点两下「继续」时，第二下
        什么都不该做——否则那命令会在下一次暂停时被立刻吃掉，看起来像
        「断点失灵」。
        """
        with self._state_lock:
            can_resume = self.paused and self.session is not None and not self.exited
            if can_resume:
                self.paused = False
        self.respond(req, {"allThreadsContinued": True})
        if not can_resume:
            return
        self.event("continued", {"threadId": THREAD_ID,
                                 "allThreadsContinued": True})
        self._commands.put(command)

    def _req_pause(self, req: dict) -> None:
        if self.session is not None and not self.exited:
            # 下一条语句边界就会停下（`_Session._before_statement` 查这个标志）
            self.session.pause_requested = True
        self.respond(req, {})

    # -- 脚本线程调用的两个钩子 ---------------------------------------------

    def _on_pause(self, reason: str) -> None:
        with self._state_lock:
            self.paused = True
        self.event("stopped", {
            "reason": _reason_for_dap(reason),
            "threadId": THREAD_ID,
            "allThreadsStopped": True,
            "description": reason,      # 中文原话，编辑器显示成暂停原因
        })

    def _read_line(self, prompt: str) -> str:
        """会话在断点处取命令：阻塞等主线程喂（命令只可能是继续类的）。"""
        try:
            return self._commands.get()
        except Exception:               # noqa: BLE001 - 出任何岔子都当「退出」
            return "退出"

    # -- 小工具 -------------------------------------------------------------

    def _frame_index(self, req: dict) -> int:
        args = req.get("arguments") or {}
        frame_id = int(args.get("frameId") or 0)
        index = frame_id - 1
        if index < 0 or index >= 256:
            raise _BadRequest(f"frameId 不对：{frame_id}")
        return index

    def _frame_variables(self, index: int) -> list:
        session = self.session
        if session is None or index < 0:
            return []
        try:
            target = session._frame_target_at(index)
            raw = session._vars_of(target) if target is not None else {}
        except Exception:               # noqa: BLE001 - 取变量失败不该中断会话
            return []
        out = []
        for name in sorted(raw):
            if session._is_noise(name):     # 内建与脱糖临时变量默认不显示
                continue
            value = raw[name]
            out.append({
                "name": name,
                "value": jishi_repr(value, top=True),
                "type": type_name(value),
                "variablesReference": 0,    # 不展开（与命令行版同一口径）
            })
        return out

    def _source_ref(self) -> dict:
        path = self.session.filename if self.session is not None else "<未知>"
        return {"name": os.path.basename(path) or path,
                "path": os.path.abspath(path)}

    @staticmethod
    def _explain(err: JishiError, with_line: bool = True) -> str:
        """把基石错误渲染成一条给编辑器看的说明（带修法建议）。

        `with_line=False` 给「就地求值」用：那时的行号是**表达式自己**的第 1 行
        （表达式从 1 行数起），说给用户听只会误导——他已经知道自己求的是什么。
        """
        text = err.title + (f"：{err.message}" if err.message else "")
        if with_line and getattr(err, "line", None):
            text = f"第 {err.line} 行：{text}"
        if getattr(err, "hint", None):
            text += f"（{err.hint}）"
        return text

    #: 有些客户端会在 launch 之前发 setBreakpoints，先存这儿
    _pending_breakpoints: list = []


def _read_program(path: str) -> tuple[str, list[str]]:
    """读源码（DAP 版：出错抛 `_BadRequest`，不是退出进程）。

    与 `cli._read_source` 同一口径：UTF-8、换行归一。
    """
    p = Path(path)
    if p.is_dir():
        raise _BadRequest(f"「{path}」是个目录 —— 调试要指向一个 .jsh 文件")
    if not p.is_file():
        raise _BadRequest(f"找不到文件「{path}」")
    try:
        source = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise _BadRequest(f"文件「{path}」不是 UTF-8 编码，读不了") from None
    except OSError as e:
        raise _BadRequest(f"读不了文件「{path}」：{e.strerror or e}") from None
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    return source, source.split("\n")


def main(argv: Optional[list[str]] = None) -> int:
    """`jishi dap [--执行器 树遍历|vm|cvm]`。"""
    parser = argparse.ArgumentParser(
        prog="jishi dap",
        description="以 Debug Adapter Protocol 跟编辑器对话（M43）。"
                    "一般由编辑器启动，不用手敲。",
    )
    parser.add_argument("--执行器", dest="engine", default="树遍历",
                        choices=["树遍历", "vm", "cvm"],
                        help="在哪个执行器上调试（默认树遍历）")
    args = parser.parse_args(argv)
    return DapAdapter(engine=args.engine).serve()


if __name__ == "__main__":                  # pragma: no cover
    sys.exit(main())
