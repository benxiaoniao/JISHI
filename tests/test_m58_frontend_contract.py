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


def _first_corpus_id() -> str:
    """取一个真实存在的语料 id（不写死名字，语料增删都能跑）。"""
    return C.corpus_id(C.collect_corpus()[0][1])


def test_compare_自比自必须全绿(ext, capsys):
    """基线 vs 基线 = 全绿。**尺子先要量得准自己** —— 这条不绿说明工具坏了。"""
    assert C.main(["compare", "--from", str(ext)]) == 0
    out = capsys.readouterr().out
    assert "全部一致" in out, out[-500:]


def test_compare_改坏一处能定位到具体位置(ext, capsys):
    """**R1 的验收标准**：把某个 token 的列号 +1，要能指出是**哪一个**。"""
    cid = _first_corpus_id()
    p = ext / f"{cid}.json"
    d = _reload(p)
    d["tokens"][2]["col"] += 1
    _save(p, d)

    capsys.readouterr()
    assert C.main(["compare", "--from", str(ext)]) == 1
    out = capsys.readouterr().out
    assert "tokens[2].col" in out, f"没指出位置：{out[-600:]}"
    # 还要说出「这是哪个东西」—— 否则人还得自己去数第 3 个 token 是什么
    assert "基线：" in out and "外部：" in out, out[-600:]


def test_compare_改坏一个_AST_节点也能定位(ext, capsys):
    cid = _first_corpus_id()
    p = ext / f"{cid}.json"
    d = _reload(p)
    d["ast"]["body"] = d["ast"]["body"][:-1]        # 少一个语句
    _save(p, d)

    capsys.readouterr()
    assert C.main(["compare", "--from", str(ext)]) == 1
    out = capsys.readouterr().out
    assert "ast" in out and "长度" in out, f"应该报到列表长度上：{out[-600:]}"


def test_stage_只比指定阶段(ext, capsys):
    """R2 只做分词时就只比 tokens —— 别的阶段**不该**参与判断。"""
    cid = _first_corpus_id()
    p = ext / f"{cid}.json"
    d = _reload(p)
    d["ast"] = None                                  # 把 AST 弄坏
    _save(p, d)

    # 全量比 → 会报不一致
    capsys.readouterr()
    assert C.main(["compare", "--from", str(ext)]) == 1
    # 只比 tokens → 绿（因为坏的是 ast）
    capsys.readouterr()
    assert C.main(["compare", "--from", str(ext), "--stage", "tokens"]) == 0
    out = capsys.readouterr().out
    assert "tokens" in out, out[-300:]


def test_缺阶段要报未提供而不是静默通过(ext, capsys):
    """**「没测」与「测过了」必须能分开。**

    外部只给了 tokens（像 R2 阶段那样）时，不带 `--stage` 必须**报「未提供」**
    并把退出码置 1 —— 绝不能因为「没这一项」就当成通过。
    """
    cid = _first_corpus_id()
    p = ext / f"{cid}.json"
    d = _reload(p)
    _save(p, {"语料": d["语料"], "tokens": d["tokens"]})    # 只留 tokens

    capsys.readouterr()
    assert C.main(["compare", "--from", str(ext)]) == 1
    out = capsys.readouterr().out
    assert "未提供" in out, f"缺阶段却没报：{out[-500:]}"


def test_缺语料要报未产出(tmp_path, capsys):
    """外部少产出一个语料 → 报「未产出」（不许当它不存在）。"""
    dst = tmp_path / "partial"
    dst.mkdir()
    # 只放一个语料的产物
    cid = _first_corpus_id()
    shutil.copy(C.BASELINE_DIR / f"{cid}.json", dst / f"{cid}.json")

    capsys.readouterr()
    assert C.main(["compare", "--from", str(dst)]) == 1
    out = capsys.readouterr().out
    assert "外部未产出" in out, out[-500:]


def test_产物读不动要如实报(ext, capsys):
    cid = _first_corpus_id()
    (ext / f"{cid}.json").write_text("{ 这不是 JSON", encoding="utf-8")

    capsys.readouterr()
    assert C.main(["compare", "--from", str(ext)]) == 1
    out = capsys.readouterr().out
    assert "读不动" in out, out[-500:]


def test_目录不存在要报错(capsys):
    assert C.main(["compare", "--from", str(ROOT / "不存在的目录")]) == 1
    assert "找不到产物目录" in capsys.readouterr().err


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
