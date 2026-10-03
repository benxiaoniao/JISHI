# -*- coding: utf-8 -*-
"""R3（新 M60）· Rust 解析器（语法 → AST）的守门测试。

R3 要证的和 R2 同一个形状，只是对象换成了（tokens + 行 + 原文）→ AST：

1. **AST 逐项一致** —— 与 Python 解析器产出**完全一样**的节点树。判据是
   `tools/conformance.py compare --stage ast` 全绿，本文件把它跑成一条测试
   （`test_与冻结基线_ast_一致`），并补上**夹具表达不了**的输入。
2. **报错逐字一致** —— 语法错误的码 / 行列 / 文案 / 提示 / `^` 下划线宽度，
   一个字符都不能差（这是本项目对报错的一贯口径，见 M56）。

另外钉住三件 R3 特有的、**容易悄悄错**的事：

- **`source=` 那条通道**（`test_丢了内容就当场报错`）：Rust 侧只吃源码文本，
  Python 的 `parse()` 入口只有 `source_lines` —— 而它有损（`split_lines`
  会去掉末尾空行）。丢了内容**必须当场报错**，不能算出行号偏一位的结果。
- **`parse_expression`**（调试器「查看」用的那条路）：与 `parse` 是**两个**
  入口，两边都要对。
- **不该泄的内部结构**（`test_报错里不出现分段表`）：插值字符串的 `value`
  是一张分段表，直接插进文案会把 Python 的 `repr` 泄给用户。
"""

import os
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import conformance as C                                            # noqa: E402
from jishi import parser as P                                      # noqa: E402
from jishi import tokenizer as T                                   # noqa: E402
from jishi.errors import JishiError                                 # noqa: E402


# ---------------------------------------------------------------------------
# 材料
# ---------------------------------------------------------------------------

def _have_rust() -> bool:
    return T.rust_available()


need_rust = pytest.mark.skipif(
    not _have_rust(),
    reason="Rust 前端未构建（跑 `python core/build.py`）—— 与其假装通过，不如明确跳过")


def _lines(text: str) -> list[str]:
    """与 `compile_source` / `cli` 一样的整行切分（**无损**那种）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _run(src: str, filename: str, frontend: str):
    """按指定前端解析，返回 `("ok", ast_json)` 或 `("err", 错误指纹)`。

    错误指纹取**渲染后的全文 + 类名**：码、标题、文案、行列、提示、
    `^` 下划线全在里面 —— 差一个字符就算不一样。

    ⚠️ 走的是**生产路径**：`parse_source(源码)` —— 与 CLI / 检查器 / LSP
    用的是同一个入口（R3 收尾时改的：原来还要调用方先 `tokenize` 再传
    `source_lines`，那既多跑一遍分词、又埋着「行切分口径」的坑）。
    另一条入口 `parse(tokens, source_lines, ...)`（LSP 增量分词用）单独验，
    见 `test_两条入口结果一致` 与 `test_丢了内容就当场报错`。
    """
    old = os.environ.get(T.FRONTEND_ENV)
    os.environ[T.FRONTEND_ENV] = frontend
    try:
        try:
            ast = P.parse_source(src, filename)
            return ("ok", C.jsonable(ast))
        except JishiError as e:
            return ("err", (type(e).__name__, e.code, e.line, e.col, str(e)))
    finally:
        if old is None:
            os.environ.pop(T.FRONTEND_ENV, None)
        else:
            os.environ[T.FRONTEND_ENV] = old


def _both(src: str, filename: str = "探针.jsh"):
    return _run(src, filename, "python"), _run(src, filename, "rust")


def _describe(py, rs) -> str:
    """两边不一致就返回**能直接读的说明**；一致返回空串。

    ⚠️ 先判「阶段相等」再判「内容相等」—— R2 在这里栽过一次：
    把「两边都报错」当成了「不一致」，白白红了 10 条。
    """
    if py[0] != rs[0]:
        return f"阶段不同：python={py[0]} rust={rs[0]}"
    if py[0] in ("err", "lex"):
        if py[1] == rs[1]:
            return ""
        return (f"报错不一致：\n  python: {py[1][0]} {py[1][1]} "
                f"{py[1][2]}:{py[1][3]}\n{py[1][4]}\n"
                f"  rust  : {rs[1][0]} {rs[1][1]} {rs[1][2]}:{rs[1][3]}\n{rs[1][4]}")
    d = C.first_diff(py[1], rs[1], "ast")
    if not d:
        return ""
    path, exp, got = d
    return (f"AST 不一致：`{path}`\n  基线（python）：{exp!r}\n  Rust          ：{got!r}")


def _same(src: str, filename: str = "探针.jsh") -> None:
    py, rs = _both(src, filename)
    msg = _describe(py, rs)
    assert not msg, f"源码：{src!r}\n{msg}"


# ---------------------------------------------------------------------------
# 一、全语料一致 + 与冻结基线一致（R3 的验收标准本身）
# ---------------------------------------------------------------------------

@need_rust
def test_全语料_ast_一致():
    """85 个语料（含 examples 真项目与基石库源码）两边的 AST 必须**逐项相同**。"""
    bad: list[str] = []
    for _cat, rel in C.collect_corpus():
        src = (ROOT / rel).read_text(encoding="utf-8")
        py, rs = _both(src, rel)
        msg = _describe(py, rs)
        if msg:
            bad.append(f"{rel}\n{msg}")
    assert not bad, "以下语料两边不一致：\n\n" + "\n\n".join(bad[:5])


@need_rust
def test_与冻结基线_ast_一致(tmp_path):
    """**R3 的验收入口**：`emit --frontend rust` → `compare --stage ast` 全绿。"""
    out = tmp_path / "rust_front"
    assert C.main(["emit", "--frontend", "rust", "--out", str(out)]) == 0
    assert C.main(["compare", "--from", str(out), "--stage", "ast"]) == 0
    # 语法阶段的报错也一起比（AST 与「为什么没产出 AST」是同一件事的两面）
    assert C.main(["compare", "--from", str(out), "--stage", "diagnostics"]) == 0


@need_rust
def test_未做到的阶段如实报未提供(tmp_path, capsys):
    """`compare` 必须报「未提供」而不是**当通过**（契约 §3：跳过与通过要分得开）。

    ⚠️ R3 时这条是拿「rust 前端还没做 bytecode」来验的；**R5 之后四个阶段
    它全做了**，于是改成**人工构造**一份缺阶段的产物 —— 这样验的是
    `compare` 的判据本身，而且**不会再被某一步的进度带歪**。
    """
    import json

    out = tmp_path / "rust_front"
    assert C.main(["emit", "--frontend", "rust", "--out", str(out)]) == 0
    # 把 bytecode 那一段抹掉：模拟「这一步还没做」的实现
    for f in out.glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        data.pop("bytecode", None)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    rc = C.main(["compare", "--from", str(out), "--stage", "bytecode"])
    text = capsys.readouterr().out
    assert rc == 1, "bytecode 阶段没提供，compare 却退 0 —— 「没做」被当成「通过」了"
    assert "未提供" in text, text


# ---------------------------------------------------------------------------
# 二、夹具表达不了的输入（文件里存不下，或只在特定调用姿态下才现形）
# ---------------------------------------------------------------------------

@need_rust
@pytest.mark.parametrize("name,src", [
    ("空文件", ""),
    ("只有换行", "\n"),
    ("全是换行", "\n\n\n"),
    ("没有结尾换行", "令 a = 1"),
    ("CRLF 换行", "令 a = 1\r\n打印(a)\r\n"),
    ("孤立 CR", "令 a = 1\r打印(a)\r"),
    ("混合三种换行", "令 a = 1\r\n令 b = 2\r令 c = 3\n打印(a + b + c)"),
    ("开头 BOM", "\ufeff令 a = 1\n打印(a)\n"),
    ("CRLF + BOM", "\ufeff令 a = 1\r\n打印(a)\r\n"),
    ("只有 BOM", "\ufeff"),
    ("空块占位（单独冒号）", "如果 真：\n    :\n"),
    ("块首之后一个空行", "如果 真：\n\n"),
    ("块首之后两个空行", "如果 真：\n\n\n"),
    ("函数块首之后空行", "函数 f()：\n\n"),
    ("遍历块首之后空行", "遍历 x 在 ：\n\n"),
    ("尝试块首之后空行", "尝试：\n\n"),
    ("三引号收尾后空行", '令 s = """甲\n乙"""\n\n'),
    ("行尾反斜杠续行", "令 a = 1 + \\\n    2\n打印(a)\n"),
    ("括号内顶格续行", "令 a = (1 +\n2)\n打印(a)\n"),
    ("深缩进与回退", "如果 真：\n    如果 真：\n        如果 真：\n            令 a = 1\n打印(a)\n"),
    ("emoji 后的列号", '令 a = "😀" + "b"\n打印(a)\n'),
    ("中文引号字符串", "令 s = 「甲乙」\n打印(s)\n"),
    ("整行插值字符串（语句位置）", "`值 {甲}`\n"),
    ("多行插值字符串当语句", "令 甲 = 1\n`值 {甲}` +\n"),
    ("尾随逗号形参表", "函数 f(a,)：\n    返回 a\n"),
    ("条件表达式", '令 等级 = "成年" 如果 1 > 0 否则 "未成年"\n'),
    ("匹配脱糖", "令 x = 1\n匹配 x：\n    情形 1：\n        打印(1)\n    情形 其他：\n        打印(0)\n"),
    ("枚举脱糖", "枚举 颜色：\n    红\n    绿 = 5\n打印(颜色.绿)\n"),
])
def test_边角输入两边一致(name, src):
    _same(src, name)


@need_rust
@pytest.mark.parametrize("name,src", [
    ("split_lines 口径（丢末尾空行）", "如果 真：\n\n"),
    ("split_lines 口径·无报错", "令 a = 1\n"),
    ("split_lines 口径·空文件", ""),
    ("多一行空行", "令 a = 1\n\n\n"),
])
def test_两种行切分口径都给一样的结果(name, src):
    """⚠️ **R3 踩到的那个坑**：`incremental.split_lines` 会去掉末尾空行，
    而 `text.split("\\n")` 不会 —— 于是「反拼行得到源码」这件事有两条口径。

    这条测试把**两种口径都跑一遍**，要求结果一样（Rust 侧吃的是 `source=`
    给的原文，所以本来就该一样）。
    """
    from jishi.incremental import split_lines

    old = os.environ.get(T.FRONTEND_ENV)
    os.environ[T.FRONTEND_ENV] = "rust"
    try:
        tokens = T.tokenize(src, name)

        def go(lines):
            try:
                return C.jsonable(P.parse(tokens, lines, name, source=src))
            except JishiError as e:          # 语言错误也是结果的一种
                return ("err", e.code, e.line, e.col, str(e))

        a = go(split_lines(src))
        b = go(_lines(src))
    finally:
        if old is None:
            os.environ.pop(T.FRONTEND_ENV, None)
        else:
            os.environ[T.FRONTEND_ENV] = old
    assert a == b, f"{name}：两种行切分口径下结果不同"


# ---------------------------------------------------------------------------
# 三、语法错误逐字一致
# ---------------------------------------------------------------------------

@need_rust
@pytest.mark.parametrize("name,src,code", [
    ("缩进对不上", "令 a = 1\n    令 b = 2\n  令 c = 3\n", "E0101"),
    ("块首后无代码", "如果 真：\n打印(1)\n", "E0202"),
    ("块首后跟空行", "如果 真：\n\n", "E0202"),
    ("函数块首后跟空行", "函数 f()：\n\n", "E0202"),
    ("类块首后跟空行", "类 甲：\n\n", "E0202"),
    ("少了冒号", "如果 真\n    打印(1)\n", "E0203"),
    ("函数少了冒号", "函数 f()\n    返回 1\n", "E0203"),
    ("块首之后写了别的", "如果 真：\n令 a = 1\n", "E0202"),
    ("赋值右边没值", "令 a = \n", "E0201"),
    ("遍历少了值", "遍历 a 在 ：\n    打印(a)\n", "E0201"),
    ("散装内容当语句", "`值 {甲}`\n", "E0201"),
    # ⚠️ R4 起这条是 E0205（此前是 E0201）：「期望右括号 + 已经到文件末尾」
    # 单独分了个码。规则与边界见 内部前端契约（未公开） §11.5 —— 中途撞上别的 token
    # （`[1, 2` 后面换行写下一句）**仍然报 E0201**，那时「看到了什么」更有用。
    ("括号没闭合", "令 a = (1 + 2\n", "E0205"),
    ("超() 写在类外", "超().叫()\n", "E0201"),
    ("不能以关键字开头", "情形 1：\n    打印(1)\n", "E0201"),
    ("捕获写在尝试外", "捕获 值错误 为 e：\n    打印(e)\n", "E0201"),
    ("形参缺少名字", "函数 f(1)：\n    返回 1\n", "E0204"),
    ("成员缺名字", "枚举 颜色：\n    = 5\n", "E0204"),
])
def test_语法错误逐字一致(name, src, code):
    py, rs = _both(src, name)
    assert py[0] in ("err", "lex"), f"{name}：Python 侧居然没报错"
    assert py[1] == rs[1], f"{name}：两边报错不一样\n  py: {py[1]}\n  rs: {rs[1]}"
    assert code in str(py[1]), f"{name}：期望 {code}，实际：{str(py[1])[:100]}"


@need_rust
def test_扩展只报已登记的语法错误码():
    """Rust 侧只会抛 E02xx —— 冒出新码就是 Python 侧漏登记（R2 那条的姊妹条）。"""
    from jishi.errors import ParseError, error_class
    assert error_class("E0201") is not JishiError
    assert error_class("E0202") is not JishiError
    assert issubclass(error_class("E0202"), ParseError)
    assert all(c.startswith("E02") for c in
               ("E0200", "E0201", "E0202", "E0203", "E0204", "E0205"))


@need_rust
def test_报错里不出现分段表():
    """⚠️ 插值字符串的 `value` 是一张**分段表**，直接插进文案会把 Python 的
    `repr` 泄给用户（实测漏出来过：`「[('text', '导\\t!=…')]」`）。

    这条钉住「说人话」：两边都不许出现 `('text'` 这种内部结构。
    """
    for src in ["`值 {甲}`\n", "如果 真：\n    `值 {甲}`\n"]:
        py, rs = _both(src, src)
        assert py[0] in ("err", "lex"), f"{src!r}：居然没报错？"
        for tag, got in (("python", py), ("rust", rs)):
            text = str(got[1])
            assert "('text'" not in text, f"{tag} 把分段表泄进了报错：{text}"
            assert "('expr'" not in text, f"{tag} 把分段表泄进了报错：{text}"


# ---------------------------------------------------------------------------
# 四、`source=` 通道：丢了内容必须当场报错
# ---------------------------------------------------------------------------

@need_rust
def test_丢了内容就当场报错(monkeypatch):
    """⚠️ **R3 踩到的那个坑本身**。

    Rust 侧只吃源码文本，而 `parse(tokens, source_lines, filename)` 里那份
    `source_lines` 是**有损**的载体（`incremental.split_lines` 去掉末尾空行）。
    丢了内容时**必须报错**——因为「悄悄少一行」的表现是：编辑器里那条红线
    **整体上移一行**，不报错、不崩溃，只是错得很有说服力。

    ⚠️ 判据是**精确**的：词法器给 EOF 记的行号恒等于「原文的行列表长度」，
    拿它当标尺比一比就知道有没有丢。
    """
    from jishi.incremental import split_lines

    src = "如果 真：\n\n"                      # 末尾空行有意义（错误位置在它上面）
    monkeypatch.setenv(T.FRONTEND_ENV, "rust")
    tokens = T.tokenize(src, "x.jsh")
    with pytest.raises(RuntimeError) as ei:
        P.parse(tokens, split_lines(src), "x.jsh")      # 故意不传 source=
    msg = str(ei.value)
    assert "source=" in msg, msg
    assert "行数不一致" in msg, msg


@need_rust
@pytest.mark.parametrize("src", [
    "令 a = 1\n", "", "\n", "如果 真：\n", "a\r\nb\r\n", "\ufeff令 a = 1\n",
])
def test_无损口径不会被守卫误伤(src, monkeypatch):
    """整行切分（无损）是**绝大多数调用方的口径**，守卫不许在这里误报。

    误报比不报更糟：它会让「正确处理的人」被拦住，然后大家把守卫关掉。
    """
    monkeypatch.setenv(T.FRONTEND_ENV, "rust")
    tokens = T.tokenize(src, "x.jsh")
    try:
        P.parse(tokens, _lines(src), "x.jsh")           # 不传 source= 也该通过
    except JishiError:
        pass                     # 语言错误（比如「块首后没代码」）不是误伤
    except RuntimeError as e:
        pytest.fail(f"{src!r} 是无损口径，却被守卫拦下：{e}")


# ---------------------------------------------------------------------------
# 五、`parse_expression`（调试器「查看」那条路）
# ---------------------------------------------------------------------------

_EXPRS = [
    "1 + 2", "甲 * 乙", "f(1, 2)", "a[0]", "a.b.c", "-1", "非 真", "[1, 2, 3]",
    "{'a': 1}", "(1 + 2) * 3", '"甲" + "乙"', "`值 {甲}`", "x 如果 真 否则 y",
    "1 < 2 < 3", "2 ** 3 ** 2", "1 如果 假 否则 2", "空", "真", "1.5e3",
    "100000000000000000000000000", "f(*a, **b)", "f(a = 1)", "看 甲",
]


@need_rust
@pytest.mark.parametrize("text", _EXPRS)
def test_表达式解析两边一致(text):
    """`parse_expression` 是**另一个入口**（拿的是文本，不是 tokens），
    两边都得对 —— 调试器里「查看 x」走的就是它。"""
    old = os.environ.get(T.FRONTEND_ENV)
    try:
        os.environ[T.FRONTEND_ENV] = "python"
        try:
            a = ("ok", C.jsonable(P.parse_expression(text)))
        except JishiError as e:
            a = ("err", e.code, e.line, e.col, str(e))
        os.environ[T.FRONTEND_ENV] = "rust"
        try:
            b = ("ok", C.jsonable(P.parse_expression(text)))
        except JishiError as e:
            b = ("err", e.code, e.line, e.col, str(e))
    finally:
        if old is None:
            os.environ.pop(T.FRONTEND_ENV, None)
        else:
            os.environ[T.FRONTEND_ENV] = old
    assert a == b, f"{text!r}\n  py: {str(a)[:300]}\n  rs: {str(b)[:300]}"


@need_rust
@pytest.mark.parametrize("text", ["1 2", "x y", "", "   ", "(1", "1 +", "f("])
def test_表达式报错两边一致(text):
    old = os.environ.get(T.FRONTEND_ENV)
    try:
        os.environ[T.FRONTEND_ENV] = "python"
        try:
            a = ("ok",)
            P.parse_expression(text)
        except JishiError as e:
            a = ("err", e.code, e.line, e.col, str(e))
        os.environ[T.FRONTEND_ENV] = "rust"
        try:
            b = ("ok",)
            P.parse_expression(text)
        except JishiError as e:
            b = ("err", e.code, e.line, e.col, str(e))
    finally:
        if old is None:
            os.environ.pop(T.FRONTEND_ENV, None)
        else:
            os.environ[T.FRONTEND_ENV] = old
    assert a == b, f"{text!r}\n  py: {str(a)[:300]}\n  rs: {str(b)[:300]}"


# ---------------------------------------------------------------------------
# 六、端到端：换个前端，程序输出不能变
# ---------------------------------------------------------------------------

#: 小、快、**输出确定**的脚本（不含计时类项目 —— 它们自己的毫秒数每次都不一样）。
E2E_CASES = [
    "tests/cases/01_算术.jsh",
    "tests/cases/02_判断.jsh",
    "tests/cases/06_字符串.jsh",
    "tests/cases/08_中文标点.jsh",
    "tests/cases/11_emoji与码点.jsh",
    "tests/cases/13_全角标点归一化.jsh",
    "tests/cases/14_三引号独占行.jsh",
    "tests/cases/16_插值字符串边界.jsh",
    "examples/01_猜数字.jsh",
    "examples/12_异常处理.jsh",
    "examples/13_面向对象.jsh",
]


@need_rust
@pytest.mark.parametrize("rel", E2E_CASES)
def test_端到端换前端不改输出(rel, tmp_path, monkeypatch):
    """换前端跑出来的东西必须一模一样（stdout 与报错全文）。

    AST 一致是必要条件，不是充分条件 —— 但 R3 之后**多个执行器**都吃这棵树，
    所以端到端这层不能省。
    """
    import io
    from contextlib import redirect_stdout

    from jishi.interpreter import run_source

    src = (ROOT / rel).read_text(encoding="utf-8")

    def play(frontend: str):
        monkeypatch.setenv(T.FRONTEND_ENV, frontend)
        buf = io.StringIO()
        d = tmp_path / frontend
        d.mkdir(exist_ok=True)
        monkeypatch.chdir(d)
        try:
            with redirect_stdout(buf):
                run_source(src, filename=rel)
            return ("ok", buf.getvalue())
        except JishiError as e:
            return ("err", str(e))

    assert play("python") == play("rust"), f"{rel}：两个前端的运行结果不同"


# ---------------------------------------------------------------------------
# 七、随机对拍（比人工想边角更靠谱）
# ---------------------------------------------------------------------------

_FUZZ_ATOMS = (
    list("令如果否则否则如果遍历循环当中断继续函数返回导入在从为真假空与或非是"
         "不是不在次类继承新建尝试捕获最终抛出用匹配情形枚举其他")
    + list("abcXY_019")
    + list("+-*/%=<>()[]{}:.,^")
    + ["**", "//", "==", "!=", "<=", ">=", "+=", "-=", "//=", "**=", "->", "..."]
    + [" ", "\t", "\u3000", "\n", "\n    ", "\n        ", "\n\t", "\n\n",
       "\n            "]
    + ['"', "'", "”", "“", "「", "」", "『", "`", "{", "}", "\\", "#", "-#", "#-",
       "😀", "中", "Ａ", "§", ".", "e", "E", "超", "自己"]
)


@need_rust
@pytest.mark.parametrize("seed", range(6))
def test_随机对拍(seed):
    """随机拼 token 汤（含各种会报错的组合），两边必须**AST 或报错**完全一致。

    固定种子 —— 失败可复现；`pytest -k 随机对拍` 随时能重跑。
    """
    rnd = random.Random(seed * 977 + 41)
    bad: list[str] = []
    for i in range(120):
        n = rnd.randint(1, 45)
        src = "".join(rnd.choice(_FUZZ_ATOMS) for _ in range(n))
        if not src.endswith("\n"):
            src += "\n"
        py, rs = _both(src, f"fuzz{seed}_{i}.jsh")
        msg = _describe(py, rs)
        if msg:
            bad.append(f"seed={seed} #{i} 源码={src!r}\n{msg}")
    assert not bad, "\n\n".join(bad[:3])


# ---------------------------------------------------------------------------
# 八、开关语义（与 R2 同一条：默认 Python，要 Rust 就得真给）
# ---------------------------------------------------------------------------

def test_默认走_Python(monkeypatch):
    monkeypatch.delenv(T.FRONTEND_ENV, raising=False)
    assert P.frontend() == "python"


def test_解析也受同一个开关管(monkeypatch):
    """⚠️ 解析器的开关取自 `tokenizer.frontend()` —— **只有一个开关**。

    两个开关（词法一个、语法一个）早晚会配出「Rust 分词 + Python 解析」
    这种没人测过的组合；R3 特意没这么做。
    """
    monkeypatch.setenv(T.FRONTEND_ENV, "rust")
    assert P.frontend() == "rust"
    monkeypatch.setenv(T.FRONTEND_ENV, "cpp")
    assert P.frontend() == "python"


@need_rust
def test_两条入口结果一致():
    """`parse_source(源码)` 与 `parse(tokens, 行, f, source=源码)` 必须同结果。

    两个入口并存是有理由的（LSP 增量分词手里已经有 tokens，不想再分一次），
    但「两条路」就意味着「两套口径」的风险 —— 所以拿语料钉住它们等价。
    """
    bad: list[str] = []
    for _cat, rel in C.collect_corpus():
        src = (ROOT / rel).read_text(encoding="utf-8")
        for front in ("python", "rust"):
            old = os.environ.get(T.FRONTEND_ENV)
            os.environ[T.FRONTEND_ENV] = front
            try:
                try:
                    a = C.jsonable(P.parse_source(src, rel))
                    tokens = T.tokenize(src, rel)
                    b = C.jsonable(P.parse(tokens, _lines(src), rel, source=src))
                except JishiError as e:
                    a = b = ("err", e.code, e.line, e.col)
                if a != b:
                    bad.append(f"{rel}（{front}）：两条入口结果不同")
            finally:
                if old is None:
                    os.environ.pop(T.FRONTEND_ENV, None)
                else:
                    os.environ[T.FRONTEND_ENV] = old
    assert not bad, "\n".join(bad[:5])
