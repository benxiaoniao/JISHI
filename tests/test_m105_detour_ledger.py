# -*- coding: utf-8 -*-
"""M105 绕道台账：**量尺本身**要有人守。

三件事：

1. `docs/绕道台账.md` 必须与探针 README 里的条目**逐字一致** ——
   台账是**扫出来的投影**，谁手改了就会红。
2. 自检的「过期报红」真的会红：函数已经补上了却还标着「缺」、模块名写错、
   `增强` 指向一个不存在的函数…… 都要报出来（否则这条纪律就是纸面上的）。
3. 每个探针项目**真的能跑**（不是死代码），且**至少有一条绕道**（否则量尺失效）。

⚠️ 纪律（`AGENTS.md` §1）：跑探针一律 `chdir` 到 `tmp_path`，绝不在仓库根读写真实文件
—— 探针会在当前目录写 `账目.json` / `待办.html` / `样本/`。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import detour_ledger as dl  # noqa: E402
from oracle.jishi.interpreter import run_source  # noqa: E402

PROBES = ROOT / "examples" / "绕道探针"


def _条目(**kw) -> dict:
    base = {"编号": "T1", "类型": "缺", "目标": "文件.原子写", "位置": "",
            "说明": "测试用", "项目": "记账本", "来源": "测试"}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 一、台账是投影：手改就对不上
# ---------------------------------------------------------------------------

def test_台账与探针里的条目一致():
    entries = dl.collect()
    assert entries, "一条绕道都没扫到 —— examples/绕道探针/*/README.md 还在吗？"
    assert dl.render(entries) == dl.LEDGER.read_text(encoding="utf-8"), (
        "docs/绕道台账.md 与探针里的条目对不上。"
        "跑 `python tools/detour_ledger.py` 重新生成 —— **不要手改台账**")


def test_台账自检现在是过的():
    problems = dl.check(dl.collect())
    assert problems == [], "台账自检没过：\n  - " + "\n  - ".join(problems)


def test_编号全仓库唯一():
    编号 = [e["编号"] for e in dl.collect()]
    重复 = {x for x in 编号 if 编号.count(x) > 1}
    assert not 重复, f"编号重复：{重复}（编号是台账的引用锚点）"


def test_每个探针项目都至少有一条绕道():
    """**量尺失效的判据**：一个探针项目若一条绕道都撞不出来，它就不该叫探针
    （多半是写得太浅，或者不涉及新能力）。"""
    有 = {e["项目"] for e in dl.collect()}
    全部 = {p.name for p in PROBES.iterdir() if p.is_dir()}
    缺 = 全部 - 有
    assert not 缺, f"这些探针项目一条绕道都没记：{缺}"


# ---------------------------------------------------------------------------
# 二、「已经能做」与「还做不了」必须分得开（过期报红）
# ---------------------------------------------------------------------------

def test_补上了的函数不许还标着缺():
    p = dl.check([_条目(类型="缺", 目标="文本.左对齐")])
    assert p and "已经有了" in p[0], p


def test_新增模块已经存在要报红():
    p = dl.check([_条目(类型="新增模块", 目标="文本")])
    assert p and "已经有" in p[0], p


def test_增强指向不存在的函数要报红():
    # ⚠️ 用一个**永远不存在**的函数名（`文件` 模块在、`某不存在的函数` 不在），
    # 别用真的待补函数（如 `文件.原子写` 这种）—— 它一旦被补上，这条测试就自己红了。
    p = dl.check([_条目(类型="增强", 目标="文件.某不存在的函数")])
    assert p and "还不存在" in p[0], p


def test_模块名写错要报红():
    p = dl.check([_条目(类型="缺", 目标="文本t.左对齐")])
    assert p and "不存在" in p[0], p


def test_位置指到不存在的文件要报红():
    p = dl.check([_条目(位置="没有这个.jsh:1")])
    assert p and "不存在" in p[0], p


def test_行号超出文件长度要报红():
    p = dl.check([_条目(位置="记账.jsh:99999")])
    assert p and "超出" in p[0], p


# ---------------------------------------------------------------------------
# 三、探针真的能跑（不是摆着看的死代码）
# ---------------------------------------------------------------------------

def _跑(项目: str, 脚本: str, 参数们: list[str], monkeypatch, tmp_path) -> str:
    """在 tmp_path 里跑一个探针，返回 stdout。"""
    import oracle.jishi.stdlib.系统 as 系统模块

    系统模块._设参数(参数们)
    src = (PROBES / 项目 / 脚本).read_text(encoding="utf-8")
    buf = io.StringIO()
    monkeypatch.chdir(tmp_path)
    try:
        with redirect_stdout(buf):
            run_source(src)
    finally:
        系统模块._设参数([])
    return buf.getvalue()


def test_探针_记账本_记入并汇总(monkeypatch, tmp_path):
    out = _跑("记账本", "记账.jsh", ["记", "25.5", "餐饮", "午饭"], monkeypatch, tmp_path)
    assert "已记：25.50 元 / 餐饮 / 午饭" in out, out

    out = _跑("记账本", "记账.jsh", ["记", "100", "交通", "打车"], monkeypatch, tmp_path)
    assert "已记：100.00 元 / 交通 / 打车" in out, out

    out = _跑("记账本", "记账.jsh", ["汇总"], monkeypatch, tmp_path)
    assert "餐饮" in out and "25.50" in out, out
    assert "合计" in out and "125.50" in out, out

    out = _跑("记账本", "记账.jsh", ["查", "餐饮"], monkeypatch, tmp_path)
    assert "「餐饮」共 1 笔" in out, out

    out = _跑("记账本", "记账.jsh", [], monkeypatch, tmp_path)
    assert "共 2 笔" in out, out          # 数据真的落在磁盘上了


def test_探针_并发批量_能算摘要(monkeypatch, tmp_path):
    样本 = tmp_path / "样本"
    样本.mkdir()
    for i in range(3):
        (样本 / f"数据{i}.bin").write_bytes(bytes([i]) * 1024)

    out = _跑("并发批量", "批量.jsh", ["样本"], monkeypatch, tmp_path)
    assert "共 3 个文件" in out, out
    assert "逐个处理用了" in out, out


def test_探针_本地小服务_生成静态页(monkeypatch, tmp_path):
    out = _跑("本地小服务", "服务.jsh", [], monkeypatch, tmp_path)
    assert "已生成" in out, out
    page = (tmp_path / "待办.html").read_text(encoding="utf-8")
    assert "<li>买菜</li>" in page, page
    assert 'charset="utf-8"' in page, page
