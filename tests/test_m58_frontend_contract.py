# -*- coding: utf-8 -*-
"""R1（新 M58）· 前端契约的守门测试。

R1 只做两件事：**定义接口（`docs/前端契约.md`）** 与 **给一把能定位的尺子
（`compare --from`）**。所以这个文件的重点也是两条：

1. **尺子准不准** —— 拿外地产物比对时，差异要**定位到 `路径.字段[下标]`**，
   还要**说出那个元素是什么**（只报「不一致」等于没报）；
2. **「没测」不许伪装成「通过」** —— 外部缺阶段、缺语料、产物读不动，
   都必须**如实报出来**（这是本项目反复踩的坑：M49 的校验被静默跳过、
   R0 的 bug 差点被冻成标准）。

验收标准（照 `docs/架构迁移路径.md`）：**改坏一处，能指出具体位置。**
"""

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import conformance as C                                            # noqa: E402


# ---------------------------------------------------------------------------
# 一、小单元：路径解析与「这是什么元素」
# ---------------------------------------------------------------------------

def test_walk_按路径取值():
    d = {"a": {"b": [10, 20, {"c": 3}]}}
    assert C._walk(d, "a.b[2].c") == 3
    assert C._walk(d, "a.b[0]") == 10
    assert C._walk(d, "") is d
    assert C._walk(d, "a.z") is C._MISS
    assert C._walk(d, "a.b[9]") is C._MISS


def test_context_说出出错的是哪个元素():
    """**这是 R1 的「做舒服」那半**：光有坐标不够，还要知道坐标上是什么。"""
    base = {
        "tokens": [{"type": "OP", "value": "=", "line": 2, "col": 5}],
        "ast": {"类型": "Program", "body": [{"类型": "Name", "名字": "a"}]},
        "bytecode": {"payload": {"codes": [{"name": "<模块>"}]}},
    }
    # token：要带上行列（最直观）
    got = C._context(base, "tokens[0].col")
    assert "OP" in got and "=" in got and "第 2 行" in got, got
    # AST 节点：给节点类型 + 一个能认出来的字段
    got = C._context(base, "ast.body[0].名字")
    assert "Name" in got and "a" in got, got
    # 函数 / 代码块
    assert "<模块>" in C._context(base, "bytecode.payload.codes[0].nlocals")
    # 根上用 `语料` 兜底
    assert C._context({"语料": "x.jsh"}, "长度") == "x.jsh"
    # 拿不到就给空串（**不硬凑**）
    assert C._context({}, "nope.deep") == ""


def test_context_对列表给项数():
    assert C._context({"tokens": [1, 2, 3]}, "tokens.长度") == "3 项"


# ---------------------------------------------------------------------------
# ⚠️ **本文件的判据只比**「`conformance` 自己打出来的那几行」**，所以一律用
# `_跑比对()`（`redirect_stdout/redirect_stderr` 到内存），**不用任何捕获夹具**。
#
# 📌 历史（两次踩，值得记）：这里原来写的是「一律用 `capfd` 而不是 `capsys`」——
#    理由是对的（`capsys` 依赖 `sys.stdout` 没被别人换掉，而 CI 上恰恰被换过）。
#    但 2026-10-25 / 26 又出现「退出码对、抓到的 stdout 是空串、pytest 自己的
#    `Captured stdout` 里却有内容」—— 换回 `capfd`/`capsys` 只是**换个赌注**。
#    ⇒ 结论：**判据要自己接管它依赖的东西**，别赌夹具行为。

# 二、compare：拿外地产物与基线比
# ---------------------------------------------------------------------------

@pytest.fixture
def ext(tmp_path):
    """把真实基线整个拷一份当「外地产物」（4 MB，拷贝很快）。

    为什么要**全量**拷：`compare` 对「外部未产出」的语料会报问题 ——
    只拷几个语料的话，剩下 70 个全在报「未产出」，测试就没法看别的了。
    """
    dst = tmp_path / "ext"
    shutil.copytree(C.BASELINE_DIR, dst)
    return dst


def _reload(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _save(p: Path, d: dict) -> None:
    p.write_text(C.dumps(d), encoding="utf-8")


@contextlib.contextmanager
def _显式捕获():
    """`with _显式捕获() as (出, 错):` —— **进程内、纯内存**的捕获。

    🔴 为什么要用它（2026-10-25，CI 上红了三轮才定下来）：
    这两条判据用 `capfd` 时，CI 上出现过「退出码对、但 stdout 和 stderr **都读到空串**」
    —— 本地（Windows）永远复现不出来。`capfd` 走的是**文件描述符 + 临时文件**，
    中间隔着 Python 自己的输出缓冲；`capsys` 又依赖「`sys.stdout` 没被别人换掉」。
    ⇒ 这一小段干脆**自己在调用前后把两个流接管掉**：`redirect_stdout` 的落点是内存，
    没有缓冲、没有 fd、也不依赖夹具 —— 谁在别处怎么折腾都影响不到它。

    📌 只给**这两条**用（其余仍用 `capfd`，那套是 2026-10-22 定的、一直是绿的）。
    """
    出, 错 = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(出), contextlib.redirect_stderr(错):
        yield 出, 错


def _跑比对(参数):
    """显式接管两条流跑一次 `C.main`，返回 `(退出码, stdout, stderr)`。

    🔴 **为什么不直接用 `capfd`**（2026-10-25 / 26 各踩一次）：
    CI 上出现过「退出码对、但抓到的 stdout 是**空串**」，而 pytest 自己的
    `Captured stdout` 里**明明有内容** —— 说明是**捕获夹具**没拿到，不是程序没打。
    换夹具（capfd ↔ capsys）只是换个赌注，**没有解决「赌」本身**。
    ⇒ 进程内 `redirect_stdout/redirect_stderr` 到内存：没有 fd、没有缓冲、
    不依赖任何夹具，谁在别处怎么折腾都影响不到。

    ⚠️ 顺带把**退出码与两条流**一起返回 —— 失败信息就能自证
    （是「没打印」还是「提前退了」）。
    """
    出, 错 = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(出), contextlib.redirect_stderr(错):
        码 = C.main(参数)
    return 码, 出.getvalue(), 错.getvalue()


def _现场(码, 出, 错) -> str:
    """失败现场：**退出码 + 两条流**。

    🔴 两条流**都空**时再补一段「可疑事实」（2026-10-26）：那时 `compare` 一定是
    提前返回了，而它的两条提前返回（「没有基线」「找不到产物目录」）都会往 stderr
    打一句 —— 两处都空就说明我们还没弄清真相。⇒ 把 `MANIFEST` / 基线目录 / 语料数
    直接摆出来，下一次红就能一眼定性，不用再猜一轮。
    """
    文 = f"退出码={码}\n--- stdout ---\n{出}\n--- stderr ---\n{错}"
    if not 出 and not 错:
        文 += (f"\n（可疑：两条流都空）MANIFEST 存在={C.MANIFEST.is_file()} "
               f"基线目录存在={C.BASELINE_DIR.is_dir()} "
               f"C.ROOT={C.ROOT} 语料数={len(C.collect_corpus())}")
    return 文


def _first_corpus_id() -> str:
    """取一个真实存在的语料 id（不写死名字，语料增删都能跑）。"""
    return C.corpus_id(C.collect_corpus()[0][1])


def test_compare_自比自必须全绿(ext):
    """基线 vs 基线 = 全绿。**尺子先要量得准自己** —— 这条不绿说明工具坏了。"""
    码, 出, 错 = _跑比对(["compare", "--from", str(ext)])
    assert 码 == 0, _现场(码, 出, 错)
    assert "全部一致" in 出, _现场(码, 出, 错)


def test_compare_改坏一处能定位到具体位置(ext):
    """**R1 的验收标准**：把某个 token 的列号 +1，要能指出是**哪一个**。"""
    cid = _first_corpus_id()
    p = ext / f"{cid}.json"
    d = _reload(p)
    d["tokens"][2]["col"] += 1
    _save(p, d)

    码, 出, 错 = _跑比对(["compare", "--from", str(ext)])
    # 🔴 失败信息要带**退出码与两条流**（2026-10-26）：CI 上这条只报「out 是空串」，
    #    而空的原因可能在 stderr（「没有基线」「找不到产物目录」都是提前返回）。
    现场 = _现场(码, 出, 错)
    assert 码 == 1, 现场
    assert "tokens[2].col" in 出, f"没指出位置：\n{现场}"
    # 还要说出「这是哪个东西」—— 否则人还得自己去数第 3 个 token 是什么
    assert "基线：" in 出 and "外部：" in 出, 现场


def test_compare_改坏一个_AST_节点也能定位(ext):
    cid = _first_corpus_id()
    p = ext / f"{cid}.json"
    d = _reload(p)
    d["ast"]["body"] = d["ast"]["body"][:-1]        # 少一个语句
    _save(p, d)

    码, 出, 错 = _跑比对(["compare", "--from", str(ext)])
    现场 = _现场(码, 出, 错)
    assert 码 == 1, 现场
    assert "ast" in 出 and "长度" in 出, f"应该报到列表长度上：\n{现场}"


def test_缺语料要报未产出(tmp_path):
    """外部少产出一个语料 → 报「未产出」（不许当它不存在）。"""
    dst = tmp_path / "partial"
    dst.mkdir()
    # 只放一个语料的产物
    cid = _first_corpus_id()
    shutil.copy(C.BASELINE_DIR / f"{cid}.json", dst / f"{cid}.json")

    码, 出, 错 = _跑比对(["compare", "--from", str(dst)])
    现场 = _现场(码, 出, 错)
    assert 码 == 1, 现场
    assert "外部未产出" in 出, f"该报未产出：\n{现场}"


def test_产物读不动要如实报(ext):
    cid = _first_corpus_id()
    (ext / f"{cid}.json").write_text("{ 这不是 JSON", encoding="utf-8")

    码, 出, 错 = _跑比对(["compare", "--from", str(ext)])
    现场 = _现场(码, 出, 错)
    assert 码 == 1, 现场
    # ⚠️ 失败时**连 stderr 一起带出来**（2026-10-22 定的）：只报「out 是空」没法判断
    #    是「工具没打印」还是「工具提前退了」。2026-10-26 起连退出码一起报。
    assert "读不动" in 出, f"该说读不动：\n{现场}"


def test_stage_只比指定阶段(ext):
    """R2 只做分词时就只比 tokens —— 别的阶段**不该**参与判断。"""
    cid = _first_corpus_id()
    p = ext / f"{cid}.json"
    d = _reload(p)
    d["ast"] = None                                  # 把 AST 弄坏
    _save(p, d)

    # 全量比 → 会报不一致
    码1, 出1, 错1 = _跑比对(["compare", "--from", str(ext)])
    assert 码1 == 1, f"全量该报不一致：\n{_现场(码1, 出1, 错1)}"
    # 只比 tokens → 绿（因为坏的是 ast）
    码2, 出2, 错2 = _跑比对(["compare", "--from", str(ext), "--stage", "tokens"])
    现场 = _现场(码2, 出2, 错2)
    assert 码2 == 0, f"只比 tokens 该绿：\n{现场}"
    assert "tokens" in 出2, f"没打出阶段名：\n{现场}"


def test_缺阶段要报未提供而不是静默通过(ext):
    """**「没测」与「测过了」必须能分开。**

    外部只给了 tokens（像 R2 阶段那样）时，不带 `--stage` 必须**报「未提供」**
    并把退出码置 1 —— 绝不能因为「没这一项」就当成通过。
    """
    cid = _first_corpus_id()
    p = ext / f"{cid}.json"
    d = _reload(p)
    _save(p, {"语料": d["语料"], "tokens": d["tokens"]})    # 只留 tokens

    码, 出, 错 = _跑比对(["compare", "--from", str(ext)])
    现场 = _现场(码, 出, 错)
    assert 码 == 1, f"缺阶段时该退 1：\n{现场}"
    assert "未提供" in 出, f"缺阶段却没报：\n{现场}"


def test_目录不存在要报错():
    码, 出, 错 = _跑比对(["compare", "--from", str(ROOT / "不存在的目录")])
    现场 = _现场(码, 出, 错)
    assert 码 == 1, 现场
    assert "找不到产物目录" in 错, f"该在 stderr 说清：\n{现场}"


# ---------------------------------------------------------------------------
# 三、契约文档本身
# ---------------------------------------------------------------------------

#: **这份检出是不是「发布快照」**（master 上那份被屏蔽过的版本）。
#:
#: `tools/publish_github.py` 会把内部文档（`docs/路线图.md` / `docs/前端契约.md` /
#: `docs/一致性夹具.md` / 架构评估那几份）从快照里整个剔掉再 force-push ——
#: 而 **CI 跑的正是这份快照**（仓库里只有 master）。于是「要求内部文档在场」的
#: 用例会**在 CI 上必红、在源仓库里永远绿**（2026-10-09 抓到，Release 一直因此挂）。
#: 这类用例在快照上**跳过**：跳过是**可见**的（pytest 会报 skip），
#: 比把断言改宽容诚实。源仓库 / 本地跑时照常严格。
_发布快照 = not (ROOT / "docs" / "路线图.md").is_file()


@pytest.mark.skipif(_发布快照, reason="发布快照：内部文档被 publish_github.py 剔掉了")
def test_契约文档在且带版本号():
    """R1 的交付之一是**带版本号的 schema 文档** —— 钉住它在。"""
    doc = ROOT / "docs" / "前端契约.md"
    assert doc.is_file(), "R1 的契约文档不见了"
    t = doc.read_text(encoding="utf-8")
    for key in ("FORMAT_VERSION", "tokens", "ast", "bytecode", "diagnostics",
                "compare", "码点", "LF"):
        assert key in t, f"契约文档里少了「{key}」"
    # 版本号要和工具里的一致（两边漂了就会各说各话）
    assert str(C.FORMAT_VERSION) in t, "文档里的版本号与 tools/conformance.py 对不上"


def test_契约文档是内部文档():
    """契约规定的是 R 线的接口，R 线定型前不外发（漏了就会进公开仓库）。"""
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib
    pub = importlib.import_module("publish_github")
    names = {n for n, _ in pub.INTERNAL_DOCS}
    assert "前端契约.md" in names, f"契约文档没进屏蔽列表：{names}"
