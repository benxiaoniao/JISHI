# -*- coding: utf-8 -*-
"""M38 · B3：REPL 增强（历史 / Tab 补全 / 多行编辑）。

三块分开测，因为它们的可测性来源不同：

- `History`   —— 纯逻辑（含落盘/读回，路径可注入）；
- `Completer` —— 纯计算（数据来自 `ai.build_lang_spec()`，不另存一份）；
- `LineEditor`—— 按键序列与输出都可注入，于是「↑ 取历史、Tab 补全、退格删汉字、
  Ctrl-C 放弃当前输入」都能在没有终端的环境里断言。

真终端那层（`read_line`）只保证两件事：**不是终端时退回 `input()`**
（管道/CI 的行为不能变，现有 `tests/test_repl.py` 一直在覆盖），
以及 `JISHI_HISTORY` 能改历史文件位置（否则测试会改开发机的家目录）。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi.lineedit import (                        # noqa: E402
    Completer,
    History,
    LineEditor,
    history_path,
    is_interactive,
    read_line,
)
from jishi.repl import _needs_more                  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: Windows 控制台方向键的前导字节（`msvcrt.getwch` 会先给这个再给码）
_LEAD = "\x00"


# ---------------------------------------------------------------------------
# 1. 历史
# ---------------------------------------------------------------------------

def test_history_up_down_and_pending():
    h = History()
    for line in ("令 甲 = 1", "令 乙 = 2"):
        h.add(line)
    assert h.up() == "令 乙 = 2"
    assert h.up() == "令 甲 = 1"
    assert h.up() == "令 甲 = 1"          # 到顶不越界
    assert h.down() == "令 乙 = 2"
    assert h.down() == ""                 # 翻回底部：还原「还没翻历史时」的输入
    assert h.down() is None               # 已经到底，没有更晚的


def test_history_restores_pending_input():
    """翻上去再翻回来，要把「翻页前正在敲的那半句」还回来。"""
    h = History(["旧的一行"])
    assert h.up("打了一半") == "旧的一行"
    assert h.down() == "打了一半"


def test_history_skips_blank_and_consecutive_duplicates():
    h = History()
    for line in ("令 甲 = 1", "", "   ", "令 甲 = 1", "令 乙 = 2"):
        h.add(line)
    assert h.all_lines() == ["令 甲 = 1", "令 乙 = 2"]


def test_history_limit_keeps_the_newest():
    h = History(limit=3)
    for i in range(5):
        h.add("第{}行".format(i))
    assert h.all_lines() == ["第2行", "第3行", "第4行"]


def test_history_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "历史.txt"
    h = History(["甲", "乙"]).save(p)
    assert p.exists()
    assert History().load(p).all_lines() == ["甲", "乙"]


def test_history_load_missing_file_is_empty(tmp_path):
    assert History().load(tmp_path / "没有这个文件.txt").all_lines() == []


def test_history_load_tolerates_garbage(tmp_path):
    p = tmp_path / "历史.txt"
    p.write_bytes(b"\xff\xfe\x00 not utf-8 at all")
    assert History().load(p).all_lines() == []


def test_history_save_failure_is_silent(tmp_path):
    """落盘失败（父路径是个文件）不能把 REPL 弄崩。"""
    blocker = tmp_path / "挡路的文件"
    blocker.write_text("x", encoding="utf-8")
    assert History(["甲"]).save(blocker / "历史.txt") is False


def test_history_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("JISHI_HISTORY", str(tmp_path / "自定义.txt"))
    assert history_path() == tmp_path / "自定义.txt"
    monkeypatch.delenv("JISHI_HISTORY")
    assert history_path().name == "repl_history.txt"
    assert ".jishi" in str(history_path())


# ---------------------------------------------------------------------------
# 2. 补全
# ---------------------------------------------------------------------------

def test_completer_offers_keywords_builtins_and_stdlib():
    comp = Completer()
    names, _ = comp.candidates("", 0)
    for expected in ("如果", "遍历", "打印", "长度", "数学", "文本"):
        assert expected in names, expected


def test_completer_filters_by_prefix():
    """`candidates` 给的是**候选池**（不过滤），过滤发生在 `complete` 里。"""
    comp = Completer()
    text, cursor, matches = comp.complete("打", 1)
    assert matches and all(m.startswith("打") for m in matches)
    assert "如果" not in matches


def test_completer_unique_match_replaces_word():
    comp = Completer()
    text, cursor, matches = comp.complete("遍", 1)
    assert text == "遍历" and cursor == 2 and matches == ["遍历"]


def test_completer_common_prefix_advances(monkeypatch):
    """多个命中时补到**共同前缀**：`甲` → `甲乙`（有推进就前进一格）。"""

    class _FakeLang:
        stdlib: dict = {}
        def top_level_names(self):
            return ["甲乙丙", "甲乙丁", "别的"]
        def all_methods(self):
            return []

    comp = Completer(lang=_FakeLang())
    text, cursor, matches = comp.complete("甲", 1)
    assert text == "甲乙" and cursor == 2
    assert set(matches) == {"甲乙丙", "甲乙丁"}


def test_completer_includes_user_defined_names():
    comp = Completer(extra_names=lambda: ["我的变量"])
    names, _ = comp.candidates("", 0)
    assert "我的变量" in names


def test_completer_skips_private_names():
    comp = Completer(extra_names=lambda: ["__用_1__", "公开的"])
    names, _ = comp.candidates("", 0)
    assert "公开的" in names and "__用_1__" not in names


def test_completer_after_dot_gives_module_functions():
    comp = Completer()
    names, word = comp.candidates("数学.平方", 5)
    assert word == "平方"
    assert "平方" in names


def test_completer_after_dot_unknown_base_gives_methods():
    """类型不确定时给「方法名全集」——给空候选等于 Tab 没反应，用户不知道为什么。"""
    comp = Completer()
    names, _ = comp.candidates("某个对象.追加", 8)
    assert "追加" in names


def test_completer_extra_names_failure_does_not_crash():
    def boom():
        raise RuntimeError("故意炸")
    comp = Completer(extra_names=boom)
    names, _ = comp.candidates("打印", 2)
    assert "打印" in names


def test_completer_no_match_keeps_text():
    comp = Completer()
    text, cursor, matches = comp.complete("zzz", 3)
    assert (text, cursor, matches) == ("zzz", 3, [])


# ---------------------------------------------------------------------------
# 3. 行编辑器（喂按键，不看终端）
# ---------------------------------------------------------------------------

def _editor(history=None, completer=None) -> LineEditor:
    return LineEditor("> ", history=history, completer=completer)


def test_editor_typing_and_enter():
    ed = _editor()
    assert ed.feed(list("令 甲 = 1") + ["\r"]) == "令 甲 = 1"
    assert ed.done is True


def test_editor_backspace_deletes_one_chinese_char():
    ed = _editor()
    ed.feed(list("中文") + ["\x08"])
    assert ed.buffer == "中"


def test_editor_left_right_and_insert_in_middle():
    ed = _editor()
    ed.feed(list("13") + [_LEAD, "K", "2"])       # 左移一格再插 2
    assert ed.buffer == "123"
    assert ed.cursor == 2


def test_editor_home_end():
    ed = _editor()
    ed.feed(list("甲乙丙") + [_LEAD, "G"] + ["だ"])
    assert ed.buffer == "だ甲乙丙"
    ed.feed([_LEAD, "O"] + ["尾"])
    assert ed.buffer == "だ甲乙丙尾"


def test_editor_delete_key():
    ed = _editor()
    ed.feed(list("甲乙") + [_LEAD, "G", _LEAD, "S"])
    assert ed.buffer == "乙"


def test_editor_arrow_up_down_uses_history():
    h = History(["第一行", "第二行"])
    ed = _editor(history=h)
    ed.feed([_LEAD, "H"])
    assert ed.buffer == "第二行"
    ed.feed([_LEAD, "H"])
    assert ed.buffer == "第一行"
    ed.feed([_LEAD, "P"])
    assert ed.buffer == "第二行"


def test_editor_tab_completes():
    ed = _editor(completer=Completer())
    ed.feed(list("遍"))
    assert ed.complete() == ["遍历"]            # complete() 返回候选表
    assert ed.buffer == "遍历"
    ed2 = _editor(completer=Completer())
    assert ed2.feed(list("遍") + ["\t"]) == "遍历"   # feed() 返回整行
    assert ed2.cursor == 2


def test_editor_ctrl_c_abandons_input():
    ed = _editor()
    ed.feed(list("写了一半") + ["\x03"])
    assert ed.interrupted is True and ed.done is True


def test_editor_ctrl_d_on_empty_is_eof():
    ed = _editor()
    ed.feed(["\x04"])
    assert ed.eof is True


def test_editor_ctrl_d_on_text_deletes_forward():
    ed = _editor()
    ed.feed(list("甲乙") + [_LEAD, "G", "\x04"])
    assert ed.buffer == "乙" and ed.eof is False


def test_editor_escape_is_ignored():
    ed = _editor()
    ed.feed(["\x1b"] + list("甲"))
    assert ed.buffer == "甲"


def test_editor_feed_stops_after_done():
    ed = _editor()
    ed.feed(list("甲") + ["\r"] + list("乙"))
    assert ed.buffer == "甲"


def test_read_line_falls_back_to_input_when_not_a_tty(monkeypatch):
    """不是终端（管道/CI）时必须退回 input()——行编辑不该打扰脚本。"""
    monkeypatch.setattr(sys, "stdin", io.StringIO("令 甲 = 1\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert is_interactive() is False
    assert read_line("> ", history=History(), completer=Completer()) == "令 甲 = 1"


# ---------------------------------------------------------------------------
# 3b. Windows 那条真实路径（假 msvcrt 喂按键）
# ---------------------------------------------------------------------------

pytestmark_windows = pytest.mark.skipif(
    os.name != "nt", reason="msvcrt 只在 Windows 上有")


def _fake_windows_keys(monkeypatch, keys):
    """把 `msvcrt.getwch` 换成按键序列，并接住重绘输出。"""
    import msvcrt
    it = iter(keys)
    monkeypatch.setattr(msvcrt, "getwch", lambda: next(it, "\r"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())


@pytestmark_windows
def test_windows_path_reads_a_line(monkeypatch):
    """真 Windows 分支（逐键 + 重绘）走一遍：它是本机用户实际用的那条路。"""
    from jishi.lineedit import _read_line_windows
    _fake_windows_keys(monkeypatch, list("令 甲 = 1") + ["\r"])
    assert _read_line_windows("> ", History(), Completer()) == "令 甲 = 1"


@pytestmark_windows
def test_windows_path_handles_arrows_and_backspace(monkeypatch):
    from jishi.lineedit import _read_line_windows
    # 输入「甲乙丙」→ 左键两次 → 退格（删「甲」）→ 回车
    keys = list("甲乙丙") + [_LEAD, "K", _LEAD, "K", "\x08", "\r"]
    _fake_windows_keys(monkeypatch, keys)
    assert _read_line_windows("> ", History(), Completer()) == "乙丙"


@pytestmark_windows
def test_windows_path_history_and_tab(monkeypatch):
    from jishi.lineedit import _read_line_windows
    # 上键取历史 → 回车
    _fake_windows_keys(monkeypatch, [_LEAD, "H", "\r"])
    assert _read_line_windows("> ", History(["上一句"]), Completer()) == "上一句"
    # Tab 补全 → 回车
    _fake_windows_keys(monkeypatch, list("遍") + ["\t", "\r"])
    assert _read_line_windows("> ", History(), Completer()) == "遍历"


@pytestmark_windows
def test_windows_path_ctrl_c_and_ctrl_d(monkeypatch):
    from jishi.lineedit import _read_line_windows
    _fake_windows_keys(monkeypatch, list("半句") + ["\x03"])
    with pytest.raises(KeyboardInterrupt):
        _read_line_windows("> ", History(), Completer())
    _fake_windows_keys(monkeypatch, ["\x04"])
    with pytest.raises(EOFError):
        _read_line_windows("> ", History(), Completer())


# ---------------------------------------------------------------------------
# 4. 多行编辑判定
# ---------------------------------------------------------------------------

def test_needs_more_on_block_and_brackets():
    assert _needs_more(["如果 真："]) is True
    assert _needs_more(["令 甲 = 最大("]) is True
    assert _needs_more(["如果 真：", "    打印(1)"]) is True     # 块还没收尾
    assert _needs_more(["令 甲 = 1"]) is False


def test_needs_more_on_unclosed_quotes():
    """引号没写完要续行——判定交给**真词法器**，不自己数引号。"""
    assert _needs_more(['令 甲 = "没写完']) is True
    assert _needs_more(["令 甲 = '没写完"]) is True
    assert _needs_more(['令 甲 = """']) is True
    assert _needs_more(["令 甲 = `插值没写完"]) is True
    assert _needs_more(['令 甲 = "写完了"']) is False


def test_needs_more_does_not_swallow_real_errors():
    """真错了（非法字符）不该继续吞行——否则 REPL 会「等一个永远不来的结尾」。"""
    assert _needs_more(["令 甲 = 1\x07"]) is False


# ---------------------------------------------------------------------------
# 5. REPL 端到端（管道）
# ---------------------------------------------------------------------------

def _run_repl(text: str) -> str:
    proc = subprocess.run([sys.executable, "-m", "jishi.cli", "-i"],
                          input=text, capture_output=True, text=True,
                          encoding="utf-8", cwd=str(ROOT), timeout=30)
    return proc.stdout + proc.stderr


def test_repl_multiline_via_unclosed_triple_quote():
    out = _run_repl('令 甲 = """\n第一行\n第二行\n"""\n打印(甲)\n退出\n')
    assert "第一行" in out and "第二行" in out


def test_repl_history_command():
    out = _run_repl("令 甲 = 1\n历史\n退出\n")
    assert "令 甲 = 1" in out


def test_repl_history_command_when_empty(tmp_path):
    """历史文件是空的（或还没有）时要说清楚，而不是打一片空白。"""
    p = tmp_path / "空历史.txt"
    env = dict(os.environ, JISHI_HISTORY=str(p), PYTHONUTF8="1")
    out = subprocess.run([sys.executable, "-m", "jishi.cli", "-i"],
                         input="历史\n退出\n", capture_output=True, text=True,
                         encoding="utf-8", cwd=str(ROOT), env=env,
                         timeout=30).stdout
    assert "还没有历史" in out


def test_repl_help_mentions_new_features():
    out = _run_repl("帮助\n退出\n")
    for word in ("Tab", "历史", "Ctrl-C"):
        assert word in out, word


def test_repl_writes_history_to_env_path(tmp_path):
    """历史要落到 `JISHI_HISTORY` 指定的文件（测试不该改开发机的家目录）。"""
    p = tmp_path / "历史.txt"
    env = dict(os.environ, JISHI_HISTORY=str(p), PYTHONUTF8="1")
    subprocess.run([sys.executable, "-m", "jishi.cli", "-i"],
                   input="令 唯一标记 = 42\n退出\n", capture_output=True,
                   text=True, encoding="utf-8", cwd=str(ROOT), env=env,
                   timeout=30)
    assert p.exists()
    assert "令 唯一标记 = 42" in p.read_text(encoding="utf-8")


def test_repl_history_is_reused_next_session(tmp_path):
    """下次打开还能翻到上次的输入（历史是**跨会话**的）。"""
    p = tmp_path / "历史.txt"
    env = dict(os.environ, JISHI_HISTORY=str(p), PYTHONUTF8="1")
    subprocess.run([sys.executable, "-m", "jishi.cli", "-i"],
                   input="令 上一轮的 = 7\n退出\n", capture_output=True,
                   text=True, encoding="utf-8", cwd=str(ROOT), env=env,
                   timeout=30)
    out = subprocess.run([sys.executable, "-m", "jishi.cli", "-i"],
                         input="历史\n退出\n", capture_output=True, text=True,
                         encoding="utf-8", cwd=str(ROOT), env=env,
                         timeout=30).stdout
    assert "令 上一轮的 = 7" in out
