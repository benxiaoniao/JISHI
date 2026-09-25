# -*- coding: utf-8 -*-
"""REPL 的行编辑层（M38 · 主线 B3：历史 / Tab 补全 / 光标编辑）。

设计上刻意把三件事分开，各自可测：

- ``History``    —— 历史记录（进程内翻页 + 落盘），**纯逻辑**，直接单测；
- ``Completer``  —— 候选计算，**纯函数式**（输入「已输入的文本 + 光标位置」
                    输出「新文本 + 候选表」），直接单测；
- ``LineEditor`` —— 把按键序列变成一行文本，**按键来源与输出都可注入**，
                    所以「上键取历史、Tab 补全、退格删汉字」这些都能在
                    没有终端的环境里测（喂一段假按键，断言结果）。

平台差异只留在最外层 ``read_line``：Windows 用 ``msvcrt`` 逐键读（``getwch``
会把方向键拆成 ``\x00``/``\xe0`` + 一个码），POSIX 优先交给 ``readline``
（它自带历史与补全）。**不是终端时一律回退到 ``input()``**——管道输入
（脚本、CI）不该被行编辑打扰，这条回退也正好被现有 REPL 测试覆盖着。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

#: 历史文件上限（条）。太大反而让上下翻页变笨重
HISTORY_LIMIT = 500

#: 历史文件位置：`JISHI_HISTORY` 可覆盖（测试用），否则落在用户目录
_ENV_HISTORY = "JISHI_HISTORY"


def history_path() -> Path:
    """历史文件路径（默认 ``~/.jishi/repl_history.txt``）。"""
    env = os.environ.get(_ENV_HISTORY)
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".jishi" / "repl_history.txt"


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------

class History:
    """REPL 历史：进程内上下翻 + 可选落盘。

    两条刻意的规则：**相邻重复不重复记**（连按回车不该刷屏），
    **上限截断**（超上限丢最旧的）。
    """

    def __init__(self, entries: "Optional[Iterable[str]]" = None,
                 limit: int = HISTORY_LIMIT) -> None:
        self.limit = max(1, int(limit))
        self.entries: list[str] = []
        for e in (entries or []):
            self._append(e)
        #: 翻页游标：len(entries) 表示「不在历史里」（当前输入行）
        self._cursor = len(self.entries)
        #: 翻页前的「当前输入」，翻回底部时要还原
        self._pending = ""

    def _append(self, line: str) -> None:
        line = line.rstrip("\r\n")
        if not line.strip():
            return
        if self.entries and self.entries[-1] == line:
            return
        self.entries.append(line)
        del self.entries[:-self.limit]

    def add(self, line: str) -> None:
        self._append(line)
        self.reset()

    def reset(self) -> None:
        self._cursor = len(self.entries)
        self._pending = ""

    def up(self, current: str = "") -> Optional[str]:
        """上一条（到顶了返回第一条本身，不越界）。"""
        if not self.entries:
            return None
        if self._cursor >= len(self.entries):
            self._pending = current
            self._cursor = len(self.entries) - 1
        elif self._cursor > 0:
            self._cursor -= 1
        return self.entries[self._cursor]

    def down(self) -> Optional[str]:
        """下一条；翻过最后一条回到「翻页前的那份输入」。"""
        if not self.entries or self._cursor >= len(self.entries):
            return None
        self._cursor += 1
        if self._cursor >= len(self.entries):
            return self._pending
        return self.entries[self._cursor]

    def all_lines(self) -> list[str]:
        return list(self.entries)

    # -- 落盘 ---------------------------------------------------------------

    def load(self, path: "Optional[Path]" = None) -> "History":
        """从文件读历史（文件不存在/读不了都当空，不打扰用户）。"""
        p = Path(path) if path is not None else history_path()
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return self
        for line in text.split("\n"):
            if line.strip():
                self._append(line)
        self.reset()
        return self

    def save(self, path: "Optional[Path]" = None) -> bool:
        """写回历史文件。**失败不报错**（只读目录、无权限都很常见）。"""
        p = Path(path) if path is not None else history_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(self.entries) + "\n", encoding="utf-8",
                         newline="\n")
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# 补全
# ---------------------------------------------------------------------------

#: 词字符（与 LSP 的 `_is_word_char` 同口径：字母数字下划线 + 中日韩）
def is_word_char(ch: str) -> bool:
    if ch.isalnum() or ch == "_":
        return True
    return "\u4e00" <= ch <= "\u9fff"


class Completer:
    """补全候选计算：顶层名字 + `模块.` 成员 + `对象.` 方法。

    名字数据全部来自 `LangData`（也就是 `ai.build_lang_spec()`），
    REPL 自己**不另存一份**关键字/内建表——那边新增一个内建，这里自动就有。
    """

    def __init__(self, lang: "Optional[object]" = None,
                 extra_names: "Optional[Callable[[], Iterable[str]]]" = None):
        #: 延迟构建：REPL 启动不该为了补全先扫一遍标准库
        self._lang = lang
        #: 额外的顶层名字来源（REPL 用它把「已经定义过的变量」也算进来）
        self._extra = extra_names

    @property
    def lang(self):
        if self._lang is None:
            from .langdata import LangData
            self._lang = LangData()
        return self._lang

    def candidates(self, text: str, cursor: int) -> "tuple[list[str], str]":
        """返回 ``(候选表, 前缀)``。

        - `前缀.` 之后 → 该标准库模块的函数 / 该对象类型的方法；
        - 否则 → 顶层名字（关键字 / 内建 / 标准库模块 / 用户已定义的变量）。
        """
        head = text[:cursor]
        word = _word_before(head)
        dot = _dot_base_before(head, len(word))
        if dot is not None:
            return self._members(dot), word
        return self._top_level(), word

    def _members(self, base: str) -> list[str]:
        lang = self.lang
        stdlib = getattr(lang, "stdlib", {}) or {}
        if base in stdlib:
            return [f["name"] for f in stdlib[base]]
        # 不是标准库模块：给出「方法名」的全集（求稳：不确定类型时宁可多给，
        # 也不给空——空候选在交互里等于「Tab 没反应」，用户不知道为什么）
        return list(getattr(lang, "all_methods", lambda: [])())

    def _top_level(self) -> list[str]:
        names = list(getattr(self.lang, "top_level_names", lambda: [])())
        if self._extra is not None:
            try:
                names += [n for n in self._extra() if n]
            except Exception:            # noqa: BLE001 —— 补全绝不能把 REPL 弄崩
                pass
        out: list[str] = []
        seen: set[str] = set()
        for n in names:
            if n and n not in seen and not n.startswith("__"):
                seen.add(n)
                out.append(n)
        return out

    def complete(self, text: str, cursor: int) -> "tuple[str, int, list[str]]":
        """补全：返回 ``(新文本, 新光标, 候选列表)``。

        唯一命中就替换成它；多个命中补到**共同前缀**（有推进就前进一格），
        候选列表交给调用方展示——这与 shell 的手感一致。
        """
        cands, word = self.candidates(text, cursor)
        if not cands:
            return text, cursor, []
        matches = sorted(c for c in cands if c.startswith(word)) if word else cands
        if not matches:
            return text, cursor, []
        if len(matches) == 1 or not word:
            new_word = matches[0] if len(matches) == 1 else _common_prefix(matches)
        else:
            new_word = _common_prefix(matches)
        start = cursor - len(word)
        new_text = text[:start] + new_word + text[cursor:]
        return new_text, start + len(new_word), matches


def _common_prefix(words: list[str]) -> str:
    if not words:
        return ""
    base = words[0]
    for w in words[1:]:
        i = 0
        while i < len(base) and i < len(w) and base[i] == w[i]:
            i += 1
        base = base[:i]
        if not base:
            break
    return base


def _word_before(text: str) -> str:
    """光标前那一段「词」（连续词字符）。"""
    i = len(text)
    while i > 0 and is_word_char(text[i - 1]):
        i -= 1
    return text[i:]


def _dot_base_before(text: str, word_len: int) -> "Optional[str]":
    """若是 `甲.前缀` 的形状，返回 `甲`（否则 None）。"""
    i = len(text) - word_len
    if i <= 0 or text[i - 1] != ".":
        return None
    j = i - 1
    while j > 0 and is_word_char(text[j - 1]):
        j -= 1
    base = text[j:i - 1]
    return base or None


# ---------------------------------------------------------------------------
# 行编辑器（按键序列可注入）
# ---------------------------------------------------------------------------

class LineEditor:
    """把一段按键序列变成一行文本。

    按键约定（与 ``msvcrt`` 的 `getwch` 对齐，所以 Windows 侧几乎只是转发）：

    - 普通字符：插入到光标处；
    - ``\\r`` / ``\\n``：回车，结束；
    - ``\\x08`` / ``\\x7f``：退格（删光标前一个字符）；
    - ``\\x00`` / ``\\xe0`` 开头的双字节：``K`` 左、``M`` 右、``G`` 行首、
      ``O`` 行尾、``H`` 上（历史）、``P`` 下（历史）；
    - ``\\t``：补全；
    - ``\\x03``：Ctrl-C（中断当前输入）；``\\x04``：Ctrl-D（空行时退出）。
    """

    def __init__(self, prompt: str = "> ", *,
                 history: "Optional[History]" = None,
                 completer: "Optional[Completer]" = None,
                 write: "Optional[Callable[[str], None]]" = None) -> None:
        self.prompt = prompt
        self.history = history
        self.completer = completer
        self._write = write or (lambda s: None)
        self.buffer = ""
        self.cursor = 0
        self.done = False
        self.interrupted = False
        self.eof = False

    # -- 编辑动作 -----------------------------------------------------------

    def insert(self, ch: str) -> None:
        self.buffer = self.buffer[:self.cursor] + ch + self.buffer[self.cursor:]
        self.cursor += len(ch)

    def backspace(self) -> None:
        if self.cursor > 0:
            self.buffer = self.buffer[:self.cursor - 1] + self.buffer[self.cursor:]
            self.cursor -= 1

    def delete(self) -> None:
        if self.cursor < len(self.buffer):
            self.buffer = self.buffer[:self.cursor] + self.buffer[self.cursor + 1:]

    def move(self, delta: int) -> None:
        self.cursor = max(0, min(len(self.buffer), self.cursor + delta))

    def to_start(self) -> None:
        self.cursor = 0

    def to_end(self) -> None:
        self.cursor = len(self.buffer)

    def set_text(self, text: str) -> None:
        self.buffer = text
        self.cursor = len(text)

    def history_up(self) -> None:
        if self.history is None:
            return
        got = self.history.up(self.buffer)
        if got is not None:
            self.set_text(got)

    def history_down(self) -> None:
        if self.history is None:
            return
        got = self.history.down()
        if got is not None:
            self.set_text(got)

    def complete(self) -> list[str]:
        if self.completer is None:
            return []
        text, cursor, matches = self.completer.complete(self.buffer, self.cursor)
        self.buffer, self.cursor = text, cursor
        return matches

    # -- 主循环 -------------------------------------------------------------

    def feed(self, keys: "Iterable[str]") -> str:
        """喂一串按键，返回最终的那一行（未回车/被中断时也返回已输入内容）。"""
        it = iter(keys)
        for key in it:
            if self.done:
                break
            if key in ("\x00", "\xe0"):
                code = next(it, "")
                self._special(code)
            else:
                self._plain(key)
        return self.buffer

    def _plain(self, key: str) -> None:
        if key in ("\r", "\n"):
            self.done = True
        elif key in ("\x08", "\x7f"):
            self.backspace()
        elif key == "\t":
            self.complete()
        elif key == "\x03":                   # Ctrl-C
            self.interrupted = True
            self.done = True
        elif key == "\x04":                   # Ctrl-D
            if not self.buffer:
                self.eof = True
                self.done = True
            else:
                self.delete()
        elif key == "\x1b":                   # 裸 ESC：忽略（避免吞掉后续）
            pass
        elif key >= " ":
            self.insert(key)

    def _special(self, code: str) -> None:
        """`\\x00`/`\\xe0` 之后的码（Windows 控制台的方向键就长这样）。"""
        if code == "K":
            self.move(-1)
        elif code == "M":
            self.move(1)
        elif code == "G":
            self.to_start()
        elif code == "O":
            self.to_end()
        elif code == "H":
            self.history_up()
        elif code == "P":
            self.history_down()
        elif code == "S":
            self.delete()


# ---------------------------------------------------------------------------
# 与真实终端对接
# ---------------------------------------------------------------------------

def is_interactive() -> bool:
    """能不能做逐键编辑（真终端 + 能读单键）。"""
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return False
    except Exception:                        # noqa: BLE001 —— 被替换过的流
        return False
    if os.name == "nt":
        try:
            import msvcrt                # noqa: F401
        except Exception:                # noqa: BLE001
            return False
    return True


def read_line(prompt: str, *, history: "Optional[History]" = None,
              completer: "Optional[Completer]" = None) -> str:
    """读一行；不能逐键编辑时退回 ``input()``。

    返回值语义与 `input()` 对齐（去掉了行尾换行）。**不捕获 EOFError/
    KeyboardInterrupt**——调用方（REPL）自己决定怎么处理。
    """
    if not is_interactive():
        return input(prompt)

    if os.name != "nt":
        return _read_line_readline(prompt, history, completer)
    return _read_line_windows(prompt, history, completer)


def _read_line_readline(prompt, history, completer) -> str:
    """POSIX：把历史和补全交给 `readline`（它自带行编辑）。

    注意职责划分：**本会话新输入的行走 readline 自己的历史**（它每次
    `input()` 都会自动记一笔），我们这份 `History` 只负责「跨会话落盘」。
    所以这里既不 add 也不同步，避免同一条被记两遍。
    """
    try:
        import readline
    except Exception:                        # noqa: BLE001
        return input(prompt)
    if history is not None:
        readline.clear_history()
        for line in history.all_lines():
            readline.add_history(line)
    if completer is not None:
        readline.set_completer(
            lambda text, state: _readline_matches(completer, text, state))
        readline.parse_and_bind("tab: complete")
    return input(prompt)


def _readline_matches(completer: "Completer", text: str, state: int):
    """readline 的补全回调（它按 state 逐个要候选）。"""
    buf = _readline_buffer()
    cands, _ = completer.candidates(buf, len(buf))
    matches = sorted(c for c in cands if c.startswith(text))
    return matches[state] if state < len(matches) else None


def _readline_buffer() -> str:
    try:
        import readline
        return readline.get_line_buffer()
    except Exception:                        # noqa: BLE001
        return ""


def _read_line_windows(prompt, history, completer) -> str:
    """Windows：`msvcrt.getwch()` 逐键读 + 自己重绘整行。

    重绘用「`\\r` 回到行首 + 提示符 + 整行 + 清到行尾」，比逐格擦写简单得多，
    而且中文（全角）宽度问题不会积累——每帧都是从行首重画的。
    """
    import msvcrt

    editor = LineEditor(prompt, history=history, completer=completer)
    out = sys.stdout.write

    def redraw() -> None:
        out("\r" + prompt + editor.buffer + "\x1b[K")
        # 光标回到正确位置
        tail = len(editor.buffer) - editor.cursor
        if tail > 0:
            out("\x1b[{}D".format(tail))
        sys.stdout.flush()

    out(prompt)
    sys.stdout.flush()
    while not editor.done:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            editor.feed([ch, msvcrt.getwch()])
        elif ch == "\t":
            matches = editor.complete()
            redraw()
            if len(matches) > 1:
                out("\n" + "  ".join(matches[:30])
                    + (" …（{} 个）".format(len(matches)) if len(matches) > 30 else "")
                    + "\n")
                redraw()
        elif ch == "\x03" or ch == "\x04":
            editor.feed([ch])
        elif ch in ("\r", "\n"):
            editor.feed([ch])
            out("\n")
        else:
            editor.feed([ch])
            redraw()

    if editor.interrupted:
        raise KeyboardInterrupt
    if editor.eof:
        raise EOFError
    return editor.buffer
