# -*- coding: utf-8 -*-
"""M202a · 语言卡认「用基石写的标准库」（`stdlib-jishi/*.jsh`）。

## 为什么这一站必须先做

M202b 要把 `数据库` 从 **rusqlite（Rust 原生）** 换成**纯基石实现**
（`基石数据库` 那个项目的引擎）。换之前有个硬阻塞：

`_stdlib_api()`（语言卡 / `--lang-spec` / AI 卡的唯一来源）**只扫
`oracle/jishi/stdlib/*.py`**（宿主兜底那份）。而 `数据库` 一旦只剩 `.jsh`，
卡里就**一个字都没有** —— AI 写代码时不知道有 `数据库` 这个模块，
人翻 `--lang-spec` 也查不到，`audit_ai_card` 直接红。

`参数 / 容器 / 统计 / 迭代` 从 M52 起就是 `.jsh` 实现 + `.py` 兜底，
所以卡里一直看得到它们 —— **看到的是兜底那份**，不是真跑的那份。这个「看起来
没事」正是最容易把 `数据库` 迁移拖翻的地方。

## 这一站立的三条规矩

1. **只有 `.jsh` 的模块必须进卡**（这不是「将来要有」，是已经要有）；
2. **两边都有的模块以 `.py` 为准**（它是契约），但**函数集合必须一致** ——
   否则卡里说的和真跑的不是一回事，而用户照卡写代码；
3. **卡里的名字必须是用户写得出来的名字**：用 `_export_names` 的规则
   （`__X` → `X`、`_X` 不导出），与 `导入` 之后能看到的名字同一套。
"""

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oracle.jishi import ai, ast_nodes as A, parser  # noqa: E402
from oracle.jishi.jishilib import _export_names, lib_dir  # noqa: E402

#: 目前是「`.jsh` 实现 + `.py` 兜底」的那 4 个（M52 起）
两边都有 = ["参数", "容器", "统计", "迭代"]


def _py里的函数(模块: str) -> set:
    """`.py` 兜底那份导出的函数名（与 `_stdlib_api` 同一套收集规则）。"""
    import ast as _ast

    src = (ROOT / "oracle" / "jishi" / "stdlib" / f"{模块}.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    return {f["name"] for f in ai._收集函数(tree.body)}


def _jsh里的函数(模块: str) -> list:
    """`.jsh` 那份**导出的函数**名（复用 `_基石库_api`，不另写一套扫描）。

    ⚠️ 不能用 `_export_names` 直接比：它连**顶层 `令`**（常量）也会导出，
    而 `.py` 侧只收函数 ⇒ 两边永远不相等，红的是判据不是代码。
    """
    return [f["name"] for f in ai._基石库_api()[模块]]


def test_基石库层的模块都在语言卡里():
    """4 个用基石写的模块，`--lang-spec` 里必须都能查到。"""
    spec = ai.build_lang_spec()
    mods = {m["module"]: m["functions"] for m in spec["stdlib"]}
    for 名 in 两边都有:
        assert 名 in mods, f"语言卡里没有模块「{名}」—— `_stdlib_api` 漏了基石库层"
        assert mods[名], f"模块「{名}」在卡里一个函数都没有"


@pytest.mark.parametrize("模块", 两边都有)
def test_两边都有的模块_函数集合必须一致(模块):
    """🔴 卡以 `.py` 为准，所以 `.py` 与 `.jsh` **必须**一致。

    分叉的后果很具体：用户照卡写 `统计.求和(…, 真)`，真跑的是 `.jsh` 那份，
    参数名或默认值不一样报的是一句「类型错误」—— 而卡上写着可以这么写。
    """
    py = _py里的函数(模块)
    jsh = set(_jsh里的函数(模块))
    assert py == jsh, (
        f"「{模块}」两边导出的函数对不上：\n"
        f"  只在 .py：{sorted(py - jsh)}\n"
        f"  只在 .jsh：{sorted(jsh - py)}")


def test_卡里的签名带默认值():
    """模型拿到 `选项(句柄, 名, 默认, 说明)` 时看不出哪个能省 —— 必须写出来。"""
    spec = ai.build_lang_spec()
    mods = {m["module"]: m["functions"] for m in spec["stdlib"]}
    选项 = [f for f in mods["参数"] if f["name"] == "选项"]
    assert 选项, "参数 模块里没有「选项」"
    assert "默认=" in 选项[0]["sig"], f"签名里没写默认值：{选项[0]['sig']}"


def test_在卡里的名字_用户必须写得出来():
    """导出名走 `_export_names` 的规则：`__X` → `X`，`_X` 不导出。"""
    spec = ai.build_lang_spec()
    mods = {m["module"]: m["functions"] for m in spec["stdlib"]}
    for 模块 in 两边都有:
        名们 = {f["name"] for f in mods[模块]}
        assert not any(n.startswith("_") for n in 名们), (
            f"「{模块}」的卡里出现了下划线开头的名字：{sorted(n for n in 名们 if n.startswith('_'))}")


def test_只有jsh的模块也能进卡(tmp_path, monkeypatch):
    """🔴 这条是 M202b 的前置证明：**没有 `.py` 兜底**的模块也要能进卡。

    做法：把「基石库目录」指到一个只有 `.jsh` 的临时目录 —— 真实的
    `stdlib-jishi/` 里现在 4 个模块都有 `.py` 兜底，不这么造一个，
    这段代码永远不会被执行到（「写了但没跑过」的典型）。
    """
    d = tmp_path / "stdlib-jishi"
    d.mkdir()
    (d / "假模块.jsh").write_text(
        textwrap.dedent('''\
            # 假模块 —— 只用来验「只有 .jsh 时也能进卡」

            函数 __打个招呼(名字, 礼貌=真)：
                """跟你打个招呼（第一行会进语言卡）。"""
                返回 名字

            函数 _内部用的()：
                返回 0
            '''), encoding="utf-8", newline="\n")

    monkeypatch.setattr("oracle.jishi.jishilib.lib_dir", lambda: d)
    mods = {m["module"]: m["functions"] for m in ai._stdlib_api()}

    assert "假模块" in mods, "_只有 .jsh_ 的模块没进卡 —— M202b 会直接卡死在这"
    名们 = {f["name"]: f for f in mods["假模块"]}
    assert "打个招呼" in 名们, f"`__X` 没映射成 `X`：{sorted(名们)}"
    assert not any(n.startswith("_") for n in 名们), "下划线开头的名字漏出来了"
    条 = 名们["打个招呼"]
    assert 条["sig"] == "打个招呼(名字, 礼貌=真)", f"签名不对：{条['sig']}"
    assert "打个招呼" in 条["doc"], f"文档没取到：{条['doc']!r}"
