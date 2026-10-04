# -*- coding: utf-8 -*-
"""`Val` 的大小 —— **结构钉**（2026-10-05 补）。

## 为什么需要它

`Val` 是值栈、局部槽、容器元素的**每格单位**。它的大小一旦悄悄变大
（最典型的：**把大载荷内联进某个变体**），代价是「没报错、只是变慢」——
任何对拍都抓不住。反过来，瘦身是**有实据的性能收益**（见下表），
所以这个数要么别动、动就要有意识。

⚠️ **本项目踩过这个坑**：`docs/设计决策.md` 里长期写着「`Val` 32 字节」，
而 2026-10-05 用 `--dump-val-size` 实测是 **128 字节** ——
文档那条**是错的**（当时没人量过，源头是把 `Class` / `Func` 的**内联载荷**
按直觉估成了小结构）。所以这个钉子的第一价值就是：**别再让「以为」代替「量过」**。

## 实测（2026-10-05，`jishi-rs --dump-val-size`）

| 项 | 字节 |
|---|---|
| `Val` | **128** |
| `Class`（内联载荷，撑大它的主因） | 120 |
| `Func` | 88 |
| `ExcValue` | 72 |
| `Module` 的载荷 `(String, HashMap)` | 72 |
| `ListIter`（迭代器状态） | 16 |

**瘦身目标**：把这几个大载荷装箱（`Box`/`Rc`）就能把 `Val` 压到 **32 字节**
（剩下的最大载荷是 `String` 24 + tag）。收益上限**实测过**（N = 200 万）：

| 负载 | 净耗时 |
|---|---|
| 只求和（不建列表，`遍历 范围(N)`） | 317 ms |
| 建列表 + 求和（列表载荷 244 MB = 200 万 × 128 B） | **1460 ms** |

即「造一个 200 万元素的列表再遍历」比「不造列表」多花 **1143 ms**，其中很大
一块就是 244 MB 的写 + 读；瘦到 32 字节（61 MB）理论上能把这块摊薄约 4 倍。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _rust() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _need() -> None:
    if not _rust().exists():
        pytest.skip("Rust 宿主未构建（cd rust && cargo build --release）")


def test_Val_的大小有钉子():
    """`Val` = 128 字节（2026-10-05 实测）。

    ⚠️ **改小了是好事**：请把这里的数、`docs/设计决策.md` 里的记录一起更新，
    并把「瘦身前后的实测对比」写进 D77（没有对比数字不许改表示）。
    **改大了要警惕**：多半是把某个大载荷内联进来了 —— 那属于「没报错、只是变慢」。
    """
    _need()
    r = subprocess.run([str(_rust()), "--dump-val-size"], capture_output=True, input=b"")
    info = json.loads(r.stdout.decode("utf-8", "replace"))
    assert info["val"] == 128, (
        f"`Val` 现在 {info['val']} 字节（原记录 128）。"
        "改小 → 请更新此断言 + D77 的实测对比；改大 → 检查是不是内联了大载荷。")
    # 载荷明细也钉住：谁把 `Val` 撑大，一眼可见。
    assert info["class"] == 120, f"Class 载荷 {info['class']}"
    assert info["func"] == 88, f"Func 载荷 {info['func']}"
    assert info["list_iter"] == 16, f"ListIter {info['list_iter']}"


def test_内联载荷不能无限长():
    """`Val` 必须 ≥ 每个内联载荷 + 标签 —— 这条是**机制说明**，防的是「把
    `Val` 压到比载荷还小」这种自相矛盾的改动。"""
    _need()
    r = subprocess.run([str(_rust()), "--dump-val-size"], capture_output=True, input=b"")
    info = json.loads(r.stdout.decode("utf-8", "replace"))
    biggest = max(info["class"], info["func"], info["exc_value"], info["module_payload"])
    assert info["val"] >= biggest, f"Val {info['val']} 小于最大载荷 {biggest}"
