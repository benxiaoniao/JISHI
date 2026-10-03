# -*- coding: utf-8 -*-
"""R2（新 M59）· Rust 分词器的守门测试。

R2 要证两件事，缺一不可：

1. **逐 token 一致** —— 与 Python 分词器产出**完全相同**的 token 流
   （不只是「能跑」）。判据是 `tools/conformance.py compare --stage tokens`
   全绿，本文件把它跑成一条测试（`test_与基线一致`），并补上**夹具表达不了**
   的那些输入（CRLF / 孤立 CR / BOM：`Path.read_text` 会把它们归一化掉，
   所以只能拿字符串直接喂）。
2. **吞吐有实测** —— `tools/bench_frontend.py` 公布三个数；
   ⚠️ **端到端的「≥5 倍」是够不着的**（接口决定的，见那里的天花板推导）——
   所以 2026-10-01 **拍板把这条验收线移到 R5**，本层只认「**纯核心 ≥5x**」。
   本文件不假装它达标，只钉住「确实更快」这件事别退化。

另外两类测试是老规矩：
- **漂移检测**（M51 起的口径）：Rust 侧的关键字 / 粘连前缀 / 运算符三张表
  由扩展**自己报出来**，与 Python 侧逐项比 —— 手工抄一份表迟早会飘。
- **开关语义**：默认走 Python；要 Rust 而没有扩展时必须**明确报错**，
  绝不静默回落（「跳过」与「通过」必须分得开）。
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


def _run(src: str, filename: str, frontend: str):
    """按指定前端分词，返回 `("ok", tokens)` 或 `("err", 错误指纹)`。

    错误指纹取**渲染后的全文 + 类名**：文案、行列、提示、`^` 下划线
    全在里面 —— 差一个字符就算不一样（这是本项目对报错的一致口径）。
    """
    old = os.environ.get(T.FRONTEND_ENV)
    os.environ[T.FRONTEND_ENV] = frontend
    try:
        try:
            return ("ok", T.tokenize(src, filename))
        except JishiError as e:
            return ("err", (type(e).__name__, str(e)))
    finally:
        if old is None:
            os.environ.pop(T.FRONTEND_ENV, None)
        else:
            os.environ[T.FRONTEND_ENV] = old


def _both(src: str, filename: str = "探针.jsh"):
    """两边各跑一次（Python 与 Rust），返回两个结果。"""
    return _run(src, filename, "python"), _run(src, filename, "rust")


def _describe(py, rs) -> str:
    """两边不一致就返回**能直接读的说明**；一致返回空串。

    ⚠️ 报错分支必须先判「相等」再决定要不要报 —— 这里一开始漏了这一步，
    于是**只要两边都报错就判失败**（85 个语料里有 10 个本来就在词法阶段
    报错，白白红了 10 条）。判据把「都错了」当成了「不一样」。
    """
    if py[0] != rs[0]:
        return f"一边成功一边报错：python={py[0]} rust={rs[0]}"
    if py[0] == "err":
        if py[1] == rs[1]:
            return ""
        return (f"报错不一致：\n  python: {py[1][0]}\n{py[1][1]}\n"
                f"  rust  : {rs[1][0]}\n{rs[1][1]}")
    d = C.first_diff([C.jsonable(t) for t in py[1]],
                     [C.jsonable(t) for t in rs[1]], "tokens")
    if not d:
        return ""
    path, exp, got = d
    return (f"产物不一致：`{path}`\n  基线（python）：{exp!r}\n"
            f"  Rust          ：{got!r}")


def _same(src: str, filename: str = "探针.jsh") -> None:
    py, rs = _both(src, filename)
    msg = _describe(py, rs)
    assert not msg, f"源码：{src!r}\n{msg}"


# ---------------------------------------------------------------------------
# 一、全语料逐 token 一致（R2 的验收标准本身）
# ---------------------------------------------------------------------------

@need_rust
def test_全语料逐_token_一致():
    """84 个语料（含 examples 真项目与基石库源码）两边的 token 流必须**逐项相同**。"""
    bad: list[str] = []
    for _cat, rel in C.collect_corpus():
        src = (ROOT / rel).read_text(encoding="utf-8")
        py, rs = _both(src, rel)
        msg = _describe(py, rs)
        if msg:
            bad.append(f"{rel}\n{msg}")
    assert not bad, "以下语料两边不一致：\n\n" + "\n\n".join(bad[:5])


@need_rust
def test_与冻结基线逐_token_一致(tmp_path):
    """**R2 的验收入口**：`emit --frontend rust` → `compare --stage tokens` 全绿。

    这条比上面那条更狠一点：它走的是**契约**（内部前端契约（未公开））规定的
    产物格式与 id 规则，顺带把「产出的目录能不能被尺子读」也验了。
    """
    out = tmp_path / "rust_front"
    assert C.main(["emit", "--frontend", "rust", "--out", str(out)]) == 0
    assert (out / f"{C.corpus_id(C.collect_corpus()[0][1])}.json").is_file()
    assert C.main(["compare", "--from", str(out), "--stage", "tokens"]) == 0


# ---------------------------------------------------------------------------
# 二、夹具表达不了的输入（文件里存不下：read_text 会把它们归一化掉）
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
    ("BOM 在中间（非法字符）", "令 a = 1\n\ufeff打印(a)\n"),
    ("CRLF + BOM", "\ufeff令 a = 1\r\n打印(a)\r\n"),
    ("只有 BOM", "\ufeff"),
    ("行尾空白", "令 a = 1   \n打印(a)\t\n"),
    ("全角空格作分隔符", "令\u3000a\u3000=\u30001\n打印(a)\n"),
    ("未闭合的多行注释", "令 a = 1\n#- 一直开着\n打印(a)\n"),
    ("多行注释里恰好有 -#", "令 a = 1\n#- 中间 -# 后面还有\n打印(a)\n"),
    ("三引号独占行的收尾", '令 s = """甲\n乙\n"""\n打印(s)\n'),
    ("三引号收尾后还有代码", '令 s = """甲\n乙""" + "丙"\n打印(s)\n'),
    ("行尾反斜杠（转义符没内容）", '令 s = "abc\\\n'),
    ("深缩进与回退", "如果 真：\n    如果 真：\n        如果 真：\n            令 a = 1\n打印(a)\n"),
    ("括号内顶格续行", "令 a = (1 +\n2)\n打印(a)\n"),
    ("括号内缩进续行", "令 a = (1 +\n     2)\n打印(a)\n"),
    ("emoji 后同一行的列号", '令 a = "😀" + "b"\n打印(a)\n'),
    ("纯 CJK 与混排标识符", "令 甲1 = 1\n令 甲a = 2\n打印(甲1 + 甲a)\n"),
    ("中文引号字符串", "令 s = 「甲乙」\n打印(s)\n"),
    ("中文引号成对之外的右引号开头", "令 s = ”甲乙”\n打印(s)\n"),
])
def test_边角输入两边一致(name, src):
    _same(src, name)


@need_rust
@pytest.mark.parametrize("ch", list("§¥±×÷≈∞°µ€§¡¿"))
def test_无法识别的字符两边一致(ch):
    """非 ASCII 又不认识的字符 → E0105，两边文案（含 U+XXXX）必须一致。"""
    py, rs = _both(f"令 a = 1\n令 b = a {ch} 2\n")
    assert py[0] == rs[0] == "err"
    assert py[1] == rs[1], f"{py[1]} != {rs[1]}"


# ---------------------------------------------------------------------------
# 三、八类词法错误逐字一致
# ---------------------------------------------------------------------------

@need_rust
@pytest.mark.parametrize("name,src,code", [
    ("缩进对不上", "令 a = 1\n    令 b = 2\n  令 c = 3\n", "E0101"),
    ("Tab 与空格混用", "如果 真：\n\t 令 a = 1\n", "E0102"),
    ("行首全角空格", "令 a = 1\n\u3000\u3000令 b = 2\n", "E0103"),
    ("字符串没结束", '令 s = "abc\n', "E0104"),
    ("字符串末尾转义符", '令 s = "abc\\\n', "E0104"),
    ("三引号到文件尾没闭合", '令 s = """甲\n乙\n', "E0104"),
    ("插值表达式没闭合", '令 s = `值 {甲`\n', "E0104"),
    ("插值字符串没结束", '令 s = `没有结尾\n', "E0104"),
    ("无法识别的字符", "令 a = § 1\n", "E0105"),
    ("关键字粘连", "如果分数 > 60：\n    打印(1)\n", "E0106"),
    ("关键字粘连（四字）", "否则如果分数 > 60：\n    打印(1)\n", "E0106"),
])
def test_词法错误逐字一致(name, src, code):
    py, rs = _both(src, name)
    assert py[0] == "err", f"{name}：Python 侧居然没报错"
    assert py[1] == rs[1], f"{name}：两边报错不一样\n  py: {py[1]}\n  rs: {rs[1]}"
    assert code in py[1][1], f"{name}：期望 {code}，实际：{py[1][1][:80]}"


@need_rust
def test_扩展只报已登记的词法错误码():
    """Rust 侧只会抛这七个码（E0101–E0106）。冒出新码就是 Python 侧漏登记。"""
    known = set(T.lex_error_classes())
    assert known, "从 jishi.errors 里一个词法错误类都没扫到 —— 映射坏了"
    assert all(c.startswith("E01") for c in known), known


# ---------------------------------------------------------------------------
# 四、端到端：换个前端，程序输出不能变
# ---------------------------------------------------------------------------

#: 小、快、**输出确定**的脚本（不含计时类项目 —— 它们自己的毫秒数每次都不一样，
#: 拿它当判据只会得出假失败）。
E2E_CASES = [
    "tests/cases/01_算术.jsh",
    "tests/cases/02_判断.jsh",
    "tests/cases/06_字符串.jsh",
    "tests/cases/08_中文标点.jsh",
    "tests/cases/11_emoji与码点.jsh",
    "tests/cases/13_全角标点归一化.jsh",
    "tests/cases/14_三引号独占行.jsh",
    "tests/cases/16_插值字符串边界.jsh",
]


@need_rust
@pytest.mark.parametrize("rel", E2E_CASES)
def test_端到端换前端不改输出(rel, tmp_path, monkeypatch):
    """**换个前端，跑出来的东西必须一模一样**（stdout 与报错全文）。

    分词一致是必要条件，不是充分条件 —— 「token 一样但跑出来不同」正是这套
    夹具最怕的隐蔽偏差。所以再补一层端到端对拍。
    """
    import io
    from contextlib import redirect_stdout

    from jishi.errors import JishiError
    from jishi.interpreter import run_source

    src = (ROOT / rel).read_text(encoding="utf-8")

    def play(frontend: str):
        monkeypatch.setenv(T.FRONTEND_ENV, frontend)
        buf = io.StringIO()
        d = tmp_path / frontend
        d.mkdir(exist_ok=True)
        monkeypatch.chdir(d)          # 有的脚本会写相对路径的文件
        try:
            with redirect_stdout(buf):
                run_source(src, filename=rel)
            return ("ok", buf.getvalue())
        except JishiError as e:
            return ("err", str(e))

    assert play("python") == play("rust"), f"{rel}：两个前端的运行结果不同"


# ---------------------------------------------------------------------------
# 五、随机对拍（比人工想边角更靠谱）
# ---------------------------------------------------------------------------

_FUZZ_ATOMS = (
    list("令如果否则否则如果遍历循环当中断继续函数返回导入在从为真假空与或非是"
         "不是不在次类继承新建尝试捕获最终抛出用匹配情形枚举")
    + list("abcXY_019")
    + list("+-*/%=<>()[]{}:.,^")
    + ["**", "//", "==", "!=", "<=", ">=", "+=", "-=", "//=", "**=", "->"]
    + [" ", "\t", "\u3000", "\n", "\n    ", "\n\t"]
    + ['"', "'", "“", "”", "「", "」", "『", "`", "{", "}", "\\", "#", "-#", "#-",
       "😀", "中", "Ａ", "§"]
)


@need_rust
@pytest.mark.parametrize("seed", range(6))
def test_随机对拍(seed):
    """随机拼 token 汤（含各种会报错的组合），两边必须**结果或报错**完全一致。

    固定种子 —— 失败可复现；`pytest -k 随机对拍` 随时能重跑。
    这类测试比人工列边角更能挖出「只在某个组合下才分叉」的差异。
    """
    rnd = random.Random(seed * 977 + 13)
    bad: list[str] = []
    for i in range(120):
        n = rnd.randint(1, 60)
        src = "".join(rnd.choice(_FUZZ_ATOMS) for _ in range(n))
        if not src.endswith("\n"):
            src += "\n"
        py, rs = _both(src, f"fuzz{seed}_{i}.jsh")
        msg = _describe(py, rs)
        if msg:
            bad.append(f"seed={seed} #{i} 源码={src!r}\n{msg}")
    assert not bad, "\n\n".join(bad[:3])


# ---------------------------------------------------------------------------
# 六、漂移检测（M51 起的老规矩：宿主自己报家底）
# ---------------------------------------------------------------------------

@need_rust
def test_关键字表与_Python_侧逐项同序():
    """⚠️ **顺序也要一样**：粘连检查依「长度降序 + 稳定排序」，
    同长度时的顺序就是插入顺序 —— 表对了但顺序错了，提示语就会换个关键字。"""
    got = list(_core().keyword_table())
    assert got == list(T.KEYWORDS), (
        f"关键字表对不上\n  Rust  : {got}\n  Python: {list(T.KEYWORDS)}")


@need_rust
def test_粘连前缀表与_Python_侧一致():
    """Python 侧那张表是**算出来的**（长度降序 + 剔掉豁免与单字），
    这里照同一个算法算一遍再比 —— 比手抄一份可靠。"""
    want = [k for k in sorted(T.KEYWORDS, key=len, reverse=True)
            if len(k) >= 2 and k not in T._GLUE_CHECK_SKIP]
    assert list(_core().glue_table()) == want, (
        f"粘连前缀表对不上\n  Rust  : {list(_core().glue_table())}\n"
        f"  Python: {want}")


@need_rust
def test_运算符表与_Python_侧同序():
    want = sorted(T.OPERATORS, key=len, reverse=True)
    assert list(_core().operator_table()) == want, (
        f"运算符表对不上\n  Rust  : {list(_core().operator_table())}\n"
        f"  Python: {want}")


@need_rust
def test_扩展版本与包版本一致():
    """扩展是按某个版本编出来的 —— 印出来，出问题时对得上。"""
    assert _core().core_version() == __import__("jishi.version", fromlist=["VERSION"]).VERSION


# ---------------------------------------------------------------------------
# 七、开关语义
# ---------------------------------------------------------------------------

def test_默认走_Python(monkeypatch):
    monkeypatch.delenv(T.FRONTEND_ENV, raising=False)
    assert T.frontend() == "python"


@pytest.mark.parametrize("val", ["", "python", "Python", "RUSTY", "cpp"])
def test_只认准_rust(monkeypatch, val):
    monkeypatch.setenv(T.FRONTEND_ENV, val)
    assert T.frontend() == "python", f"{val!r} 不该被当成 rust"


def test_rust_开关生效(monkeypatch):
    monkeypatch.setenv(T.FRONTEND_ENV, "rust")
    assert T.frontend() == "rust"


def test_要_rust_却没有扩展时明确报错(monkeypatch):
    """**绝不许静默回落** —— 「跳过」与「通过」必须分得开（R0 立下的规矩）。

    ⚠️ 只把 `sys.modules['jishi._core']` 置空是**不够**的：
    `jishi` 包上已经挂了 `_core` 这个属性，`from . import _core` 会命中
    `_handle_fromlist` 的 `hasattr` 短路、根本不走导入 —— 测试就永远绿。
    要连属性一起摘掉，才算真的模拟出「扩展没构建」。
    """
    import jishi
    monkeypatch.setenv(T.FRONTEND_ENV, "rust")
    monkeypatch.delattr(jishi, "_core", raising=False)
    monkeypatch.setitem(sys.modules, "jishi._core", None)
    with pytest.raises(RuntimeError) as ei:
        T.tokenize("令 a = 1\n")
    msg = str(ei.value)
    assert "core/build.py" in msg, msg
    assert "JISHI_FRONTEND" in msg, msg


def _core():
    return T._core()
