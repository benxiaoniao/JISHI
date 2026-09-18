# -*- coding: utf-8 -*-
"""M38 · B2：LSP 增量同步（增量文本 + 按行复用前缀的增量分词）。

背景（实测，别猜）：LSP 以前每收到一次编辑就整文件 tokenize+parse：
253 行 10ms、1253 行 44ms、5003 行 182ms、20003 行 759ms——一千行以上就有
可感的卡顿。B2 做两件事：

1. **协议层**：`textDocumentSync.change = 2`（增量），客户端只发改动的那一段，
   服务端按区间应用（行列按 UTF-16 码元换算）；顺带做版本乱序保护。
2. **实现层**：编辑只影响改动行之后的内容，所以前缀的 token 与每行入行状态
   可以复用（`jishi.incremental`），只重算后缀。

**正确性靠对拍**：随机编辑下，增量分词结果必须与全量分词逐项相同。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi.errors import JishiError                        # noqa: E402
from jishi.incremental import IncrementalDocument, split_lines   # noqa: E402
from jishi.lsp import LspServer, _apply_change, _utf16_len, _utf16_to_index  # noqa: E402
from jishi.tokenizer import Tokenizer, tokenize            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
URI = "file:///调试.jsh"

#: 对拍用的语料：包含注释、三引号、括号续行、字符串里的 # 号、多行注释、
#: 深层缩进——这些都是「行级状态」真正被用到的地方。
SRC = '''# 顶部注释
导入 数学

令 配置 = {
    "名字": "示例",   # 行尾注释
    "阈值": 3,
}

令 说明 = """
  这里有三引号
  里面还有 括号( 和 # 号
"""

函数 计算(数据, 阈值 = 2)：
    # 注释行
    合计 = 0
    遍历 项 在 数据：
        如果 项 > 阈值：
            合计 = 合计 + 项 * 2
        否则：
            合计 = 合计 - 1
    返回 {"小计": 合计, "说明": 说明}


类 盒子：
    容量 = 10

    函数 装(自身, 值)：
        返回 值 + 自身.容量


打印(计算([1, 2, 3]), 盒子().装(1))
'''


def _sig(tokens):
    return [(t.type, t.value, t.line, t.col, t.end_col) for t in tokens]


def _full_sig(text: str):
    return _sig(tokenize(text, "<对拍>"))


# ---------------------------------------------------------------------------
# 1. IncrementalDocument：行为
# ---------------------------------------------------------------------------

def test_same_text_is_a_noop():
    inc = IncrementalDocument(SRC, "<对拍>")
    assert inc.update(SRC) is None


def test_append_at_end_relexes_only_the_tail():
    inc = IncrementalDocument(SRC, "<对拍>")
    n0 = len(inc.lines)
    start = inc.update(SRC + "令 追加 = 1\n")
    assert start == n0                        # 从「原最后一行之后」开始
    assert inc.last_relexed == 1              # 只重算新加的那一行
    assert _sig(inc.tokens) == _full_sig(SRC + "令 追加 = 1\n")


def test_edit_in_the_middle_only_relexes_the_suffix():
    inc = IncrementalDocument(SRC, "<对拍>")
    lines = SRC.split("\n")
    lines[20] = "    合计 = 0  # 改了这一行"
    new = "\n".join(lines)
    start = inc.update(new)
    assert start == 20
    # 只重算「这一行及之后」，前面的行直接复用
    assert inc.last_relexed == len(inc.lines) - 20
    assert _sig(inc.tokens) == _full_sig(new)


def test_edit_on_first_line_relexes_everything():
    inc = IncrementalDocument(SRC, "<对拍>")
    new = "令 甲 = 1\n" + SRC.split("\n", 1)[1]
    assert inc.update(new) == 0
    assert _sig(inc.tokens) == _full_sig(new)


@pytest.mark.parametrize("edit", [
    lambda ls: ls[:1] + ["令 甲 = 1"] + ls[1:],              # 开头插入
    lambda ls: ls + ["令 尾 = 2", ""],                        # 末尾追加
    lambda ls: ls[:-1],                                      # 删掉最后一行
    lambda ls: [],                                           # 清空
    lambda ls: ["令 只剩一行 = 1"],                            # 只剩一行
    lambda ls: ls[:5] + ls[6:],                              # 中间删一行
    lambda ls: [l + "  # 尾巴" for l in ls],                  # 每行都改
    lambda ls: ["    " + l for l in ls],                      # 整体缩进
])
def test_various_edit_shapes_match_full_tokenize(edit):
    inc = IncrementalDocument(SRC, "<对拍>")
    new = "\n".join(edit(SRC.split("\n")))
    if new == SRC:
        return
    inc.update(new)
    assert _sig(inc.tokens) == _full_sig(new)


def test_edit_inside_triple_quoted_string():
    """三引号字符串内部被改：跨行字符串状态必须能正确续跑。"""
    inc = IncrementalDocument(SRC, "<对拍>")
    new = SRC.replace("  这里有三引号", "  这里改成了别的东西")
    inc.update(new)
    assert _sig(inc.tokens) == _full_sig(new)


def test_edit_inside_bracket_continuation():
    """括号续行内部被改：从 `bracket_depth > 0` 的状态续跑。"""
    inc = IncrementalDocument(SRC, "<对拍>")
    new = SRC.replace('    "阈值": 3,', '    "阈值": 30,')
    inc.update(new)
    assert _sig(inc.tokens) == _full_sig(new)


def test_edit_inside_block_comment():
    """多行注释（`#- … -#`）内部被改：注释状态要能正确续跑。"""
    src = "#- 块注释\n中间\n-#\n令 甲 = 1\n"
    inc = IncrementalDocument(src, "<对拍>")
    new = "#- 块注释\n中间改过了\n-#\n令 甲 = 1\n"
    inc.update(new)
    assert _sig(inc.tokens) == _full_sig(new)


def test_random_edits_match_full_tokenize():
    """随机编辑对拍（含各种形状与错误输入）——B2 的核心正确性保障。"""
    rng = random.Random(20260919)
    inc = IncrementalDocument(SRC, "<对拍>")
    for i in range(60):
        lines = inc.text.split("\n")
        k = rng.randrange(max(1, len(lines)))
        kind = rng.choice(["edit", "insert", "delete", "indent", "blob"])
        if kind == "insert":
            lines.insert(k, "令 新{0} = {0}".format(i))
        elif kind == "delete" and len(lines) > 2:
            del lines[k]
        elif kind == "indent":
            lines[k] = "    " + lines[k]
        elif kind == "blob":
            lines.insert(k, '令 文本 = "含 # 号 \\" 与 ) 括号"')
        else:
            lines[k] = lines[k] + "  # 改动{0}".format(i)
        new = "\n".join(lines)
        if new == inc.text:
            continue
        try:
            inc.update(new)
            got = ("ok", _sig(inc.tokens))
        except JishiError as e:
            got = ("err", (e.code, e.line, e.col))
        try:
            want = ("ok", _full_sig(new))
        except JishiError as e:
            want = ("err", (e.code, e.line, e.col))
        assert got == want, "第 {0} 次编辑（{1}）增量与全量不一致".format(i, kind)


def test_indentation_error_keeps_object_consistent():
    """编辑写出临时性缩进错：抛错，但对象仍然自洽（下次自动走全量）。"""
    inc = IncrementalDocument("如果 真：\n    打印(1)\n", "<对拍>")
    bad = "如果 真：\n    打印(1)\n  打印(2)\n"     # 缩进 2：缩进栈里没有这一层
    with pytest.raises(JishiError):
        inc.update(bad)
    # 文本要跟上（否则下次算出来的「改动行」是错的）
    assert inc.text == bad
    assert inc.anchors is None
    # 下次编辑：全量兜底，结果仍然正确
    good = bad.replace("  打印(2)", "    打印(2)")
    assert inc.update(good) == 0
    assert inc.anchors is not None
    assert _sig(inc.tokens) == _full_sig(good)


def test_crlf_is_normalized_like_the_lexer():
    inc = IncrementalDocument(SRC, "<对拍>")
    assert inc.update(SRC.replace("\n", "\r\n")) is None   # 归一化后是同一份文本
    assert _sig(inc.tokens) == _full_sig(SRC)


def test_split_lines_matches_lexer():
    """行切分必须与词法器**完全一致**（差一行，锚点下标就全错）。"""
    for text in ("", "令 甲 = 1", "令 甲 = 1\n", "令 甲 = 1\n\n\n", "\n\n",
                 "\r\n令 甲 = 1\r\n\r\n"):
        assert split_lines(text) == Tokenizer(text, "<对拍>").lines


def test_big_file_only_relexes_the_tail():
    """结构性断言（不看耗时，CI 上才稳定）：500 行的文件改最后一行只重算 1 行。"""
    src = "".join("令 变量{0} = {0}\n".format(i) for i in range(500))
    inc = IncrementalDocument(src, "<对拍>")
    inc.update(src.replace("令 变量499 = 499", "令 变量499 = 0"))
    assert inc.last_relexed == 1
    inc.update(inc.text.replace("令 变量0 = 0", "令 变量0 = 9"))
    assert inc.last_relexed == 500


# ---------------------------------------------------------------------------
# 2. UTF-16 位置换算（编辑范围必须按码元算）
# ---------------------------------------------------------------------------

def test_utf16_units_count_surrogate_pairs():
    assert _utf16_len("abc") == 3
    assert _utf16_len("中文") == 2
    assert _utf16_len("😀") == 2                  # 天文平面：Python 里是 1 个字符
    assert _utf16_len("中😀a") == 4


def test_utf16_to_index_clamps():
    s = "中😀a"
    assert _utf16_to_index(s, 0) == 0
    assert _utf16_to_index(s, 1) == 1
    assert _utf16_to_index(s, 3) == 2
    assert _utf16_to_index(s, 99) == 3
    assert _utf16_to_index(s, -5) == 0


def test_apply_change_after_emoji():
    """在 emoji 之后插入：范围按 UTF-16 码元算，不能按字符下标算。

    这一行是 `令 甲 = "😀"`：`令 甲 = "` 占 7 个码元，emoji 占 **2** 个
    （码元 7–8），收尾引号从码元 9 开始，整行 10 个码元。
    「码元 9」正是最容易写错的一格（它是 emoji 之后、引号之前），
    所以把两个边界都钉住。
    """
    text = '令 甲 = "😀"\n'
    at9 = _apply_change(text, {
        "range": {"start": {"line": 0, "character": 9},
                  "end": {"line": 0, "character": 9}},
        "text": "X",
    })
    assert at9 == '令 甲 = "😀X"\n'                # 落在引号之前
    at10 = _apply_change(text, {
        "range": {"start": {"line": 0, "character": 10},
                  "end": {"line": 0, "character": 10}},
        "text": " + 后缀",
    })
    assert at10 == '令 甲 = "😀" + 后缀\n'          # 落在引号之后


def test_apply_change_full_replacement_without_range():
    assert _apply_change("旧", {"text": "新"}) == "新"


def test_apply_change_reversed_range_is_safe():
    """客户端把 start/end 给反了：不能把文档改坏。"""
    assert _apply_change("abcdef", {
        "range": {"start": {"line": 0, "character": 4},
                  "end": {"line": 0, "character": 2}},
        "text": "X",
    }) == "abXef"


# ---------------------------------------------------------------------------
# 3. LSP 协议层
# ---------------------------------------------------------------------------

def _server() -> LspServer:
    srv = LspServer()
    srv._write = lambda obj: None
    return srv


def _open(srv: LspServer, text: str, version: int = 1) -> None:
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                  "params": {"textDocument": {"uri": URI, "version": version,
                                              "text": text}}})


def _change(srv: LspServer, changes: list, version: int = 2) -> None:
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didChange",
                  "params": {"textDocument": {"uri": URI, "version": version},
                             "contentChanges": changes}})


def _text(srv: LspServer) -> str:
    return srv.docs[URI]["text"]


def test_incremental_change_replaces_a_range():
    srv = _server()
    _open(srv, "令 甲 = 1\n令 乙 = 2\n")
    # `令 乙 = 2` 里「乙」在第 2 个字符（0 基），区间半开 [2, 3)
    _change(srv, [{"range": {"start": {"line": 1, "character": 2},
                             "end": {"line": 1, "character": 3}},
                   "text": "丙"}])
    assert _text(srv) == "令 甲 = 1\n令 丙 = 2\n"


def test_incremental_change_inserts_multi_line():
    srv = _server()
    _open(srv, "令 甲 = 1\n")
    _change(srv, [{"range": {"start": {"line": 0, "character": 0},
                             "end": {"line": 0, "character": 0}},
                   "text": "令 前 = 0\n"}])
    assert _text(srv) == "令 前 = 0\n令 甲 = 1\n"


def test_multiple_changes_are_applied_in_order():
    srv = _server()
    _open(srv, "甲\n乙\n")
    _change(srv, [
        {"range": {"start": {"line": 0, "character": 0},
                   "end": {"line": 0, "character": 1}}, "text": "甲甲"},
        {"range": {"start": {"line": 1, "character": 0},
                   "end": {"line": 1, "character": 1}}, "text": "乙乙"},
    ])
    assert _text(srv) == "甲甲\n乙乙\n"


def test_full_replacement_is_still_supported():
    """客户端降级用全量同步（不给 range）时也要对。"""
    srv = _server()
    _open(srv, "令 甲 = 1\n")
    _change(srv, [{"text": "令 乙 = 2\n"}])
    assert _text(srv) == "令 乙 = 2\n"


def test_out_of_order_version_is_ignored():
    """旧版本（或重复版本）不能覆盖新文本，否则诊断会「改完又变回去」。"""
    srv = _server()
    _open(srv, "令 甲 = 1\n", version=5)
    _change(srv, [{"text": "令 新 = 1\n"}], version=6)
    assert _text(srv) == "令 新 = 1\n"
    _change(srv, [{"text": "令 旧 = 1\n"}], version=6)      # 重复版本
    assert _text(srv) == "令 新 = 1\n"
    _change(srv, [{"text": "令 更旧 = 1\n"}], version=3)     # 旧版本
    assert _text(srv) == "令 新 = 1\n"
    _change(srv, [{"text": "令 更新 = 1\n"}], version=7)     # 新版本 → 应用
    assert _text(srv) == "令 更新 = 1\n"


def test_change_without_open_does_not_crash():
    """没 open 过就收到 didChange：忽略即可（协议上不会发生），但**不能崩**。"""
    srv = _server()
    _change(srv, [{"text": "令 甲 = 1\n"}])
    assert URI not in srv.docs


def test_diagnostics_follow_incremental_edits():
    """增量编辑之后，诊断要是**新文本**的（不是旧文本的残留）。"""
    srv = _server()
    sent = []
    srv._write = sent.append
    _open(srv, "令 甲 = 1\n打印(甲)\n")
    assert sent[-1]["params"]["diagnostics"] == []
    # 把第 1 行改成打错的内建名
    _change(srv, [{"range": {"start": {"line": 1, "character": 0},
                             "end": {"line": 1, "character": 2}},
                   "text": "打应"}])
    diags = sent[-1]["params"]["diagnostics"]
    assert any("打应" in d["message"] for d in diags)


def test_incremental_and_full_diagnostics_agree():
    """端到端对拍：增量编辑出来的文档，诊断要与「全量打开同一文本」一致。"""
    steps = [
        "令 甲 = 1\n",
        "令 甲 = 1\n令 乙 = 甲 + 1\n",
        "令 甲 = 1\n令 乙 = 甲 + 无此\n",
        "令 甲 = 1\n令 乙 = 甲 + 1\n打印(乙)\n",
    ]
    inc_srv = _server()
    _open(inc_srv, steps[0])
    for i, text in enumerate(steps[1:], start=2):
        _change(inc_srv, [{"text": text}], version=i)

    full_srv = _server()
    _open(full_srv, steps[-1])

    assert _text(inc_srv) == steps[-1]
    assert inc_srv.diagnostics[URI] == full_srv.diagnostics[URI]
    assert [s["name"] for s in inc_srv.symbols[URI]] == \
        [s["name"] for s in full_srv.symbols[URI]]


def test_save_reuses_tokens_without_relexing():
    """didSave 不该重新分词（文本没变）。"""
    srv = _server()
    _open(srv, SRC)
    inc = srv.docs[URI]["inc"]
    before = inc.last_relexed
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didSave",
                  "params": {"textDocument": {"uri": URI}}})
    assert inc.last_relexed == before
    assert inc.update(SRC) is None


def test_close_drops_document_and_diagnostics():
    srv = _server()
    _open(srv, "令 甲 = 1\n")
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didClose",
                  "params": {"textDocument": {"uri": URI}}})
    assert URI not in srv.docs
    assert URI not in srv.diagnostics


# ---------------------------------------------------------------------------
# 4. 顺带修掉的一个词法器旧 bug（被增量续跑暴露出来）
# ---------------------------------------------------------------------------

def test_toplevel_continuation_line_is_tokenized():
    """回归钉：**顶格的括号续行**以前会被整行跳过。

    `content_start` 在「括号内续行」那一支**没有被赋值**，用的是上一轮的残留值：
    续行写在第 0 列时，扫描起点落在行尾之后 → 整行扫不出 token。这个 bug 一直
    藏着（正常写法续行都有缩进，恰好被跳过的是空白），是 M38 B2 从
    `bracket_depth > 0` 的状态续跑时以 `UnboundLocalError` 的形式暴露的。
    """
    src = "如果 真：\n    令 甲 = 最大(\n1,\n2,\n)\n    打印(甲)\n"
    vals = [(t.type, t.value) for t in tokenize(src, "<顶格续行>")
            if t.type not in ("INDENT", "DEDENT", "NEWLINE", "EOF")]
    assert ("NUMBER", 1) in vals and ("NUMBER", 2) in vals
    assert ("OP", ",") in vals
