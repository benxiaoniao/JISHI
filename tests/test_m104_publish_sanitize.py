# -*- coding: utf-8 -*-
"""M104：发布脚本的「抹引用」只许动文档（`.md`），**不许动源码/测试**。

## 为什么单独立一条

2026-10-09 才逮到的真 bug：`_sanitize()` 抹内部文档引用时，把
`.py` / `.json` / `.txt` 也算进了「要抹的文件」里。后果是
`tests/test_m58_frontend_contract.py` 里那句

    assert "前端契约.md" in names, ...

在**发布快照**里被抹成了

    assert "内部前端契约（未公开）" in names, ...

于是这条用例在 **CI（跑的就是屏蔽快照）上必失败**，而在**本地源码树
（没有这一步）永远绿** —— 一句「CI 红、本地绿」卡了很久，根因就在这里。

抹引用的目的是「公开**文档**里不留死链」，那是 `.md` 的事；源码与测试里的
路径字面量不是链接。这条判据把「只动 `.md`」钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import publish_github as pg                                          # noqa: E402

#: 测试里那句「像真测试一样断言文件名」的代码（**必须逐字节不变**）。
_SAMPLE_PY = ('def test_x():\n'
              '    assert "前端契约.md" in names, f"没进屏蔽列表：{names}"\n')


def _make_min_snapshot(tmp: Path) -> Path:
    """造一棵最小可跑的「快照」树。

    ⚠️ `_sanitize` 会**无条件**读 `README.md` 与 `docs/设计决策.md`，
    所以这两份必须有，否则它自己会崩 —— 那不是本判据要测的东西。
    """
    (tmp / "docs").mkdir(parents=True)
    (tmp / "tests").mkdir()
    (tmp / "README.md").write_text("# 基石\n\n公开文档：docs/语言规格.md\n",
                                   encoding="utf-8")
    (tmp / "docs" / "设计决策.md").write_text("# 设计决策\n", encoding="utf-8")
    # 内部文档：应被整个删掉
    (tmp / "docs" / "前端契约.md").write_text("# 前端契约\n", encoding="utf-8")
    (tmp / "docs" / "路线图.md").write_text("# 路线图\n", encoding="utf-8")
    # 公开文档里指向内部文档的引用：应被中性化
    (tmp / "docs" / "语言规格.md").write_text(
        "见 [前端契约](前端契约.md) 与 `前端契约.md`。\n", encoding="utf-8")
    # 源码 / 测试里的同名字面量：**不该被改**
    (tmp / "tests" / "sample.py").write_text(_SAMPLE_PY, encoding="utf-8")
    return tmp


def test_只中性化_md_源码与测试原样不动(tmp_path):
    snap = _make_min_snapshot(tmp_path)
    pg._sanitize(snap)

    # 1. 内部文档删干净了
    assert not (snap / "docs" / "前端契约.md").exists()
    assert not (snap / "docs" / "路线图.md").exists()

    # 2. 公开文档里的引用被中性化了
    spec = (snap / "docs" / "语言规格.md").read_text(encoding="utf-8")
    assert "前端契约.md" not in spec, spec
    assert "内部前端契约（未公开）" in spec, spec

    # 3. 🔴 **源码/测试一个字没动** —— 这条就是本文件存在的理由
    assert (snap / "tests" / "sample.py").read_text(encoding="utf-8") == _SAMPLE_PY


def test_内部文档名单是脚本自己的事实():
    """判据依赖 `INTERNAL_DOCS` 里确实有 `前端契约.md`（别哪天被删了还以为在测）。"""
    names = {n for n, _ in pg.INTERNAL_DOCS}
    assert "前端契约.md" in names, names
