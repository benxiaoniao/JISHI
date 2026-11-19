# -*- coding: utf-8 -*-
"""`Val` 的大小 —— **结构钉**（2026-10-05 建立，同日完成瘦身）。

## 为什么需要它

`Val` 是值栈、局部槽、容器元素的**每格单位**。它的大小一旦悄悄变大
（最典型的：**把大载荷内联进某个变体**），代价是「没报错、只是变慢」——
任何对拍都抓不住。反过来，瘦身是**有实据的性能收益**，所以这个数要么别动、
动就要有意识。

⚠️ **本项目在这个数上踩过坑**：`docs/设计决策.md` 里长期写着「`Val` 32 字节」，
而 2026-10-05 用 `--dump-val-size` 实测是 **128 字节** ——
文档那条**是错的**（当年没人量过，源头是把 `Class` / `Func` 的**内联载荷**
按直觉估成了小结构）。教训：**「以为」不能代替「量过」**（同 D53）。

## 实测与瘦身（2026-10-05，`jishi-rs --dump-val-size`）

| 项 | 字节 | 说明 |
|---|---|---|
| `Val`（瘦身前） | 128 | 撑大它的是**内联的 `Class`（120）** |
| `Val`（瘦身后） | **32** | 把 `Class` / `Func` / `Module` 载荷**装箱** |
| `Class` 载荷 | 120 | 类型本身没变，只是不再内联 |
| `Func` 载荷 | 88 | 同上 |
| `ExcValue` 载荷 | 72 | 它本来就是 `Rc`（一直没内联） |
| `Module` 载荷 | 72 | 两半都装箱 |
| `ListIter` | 16 | 列表活迭代器的状态（D77 §一） |

**交错 A/B（同机同时段、各取最优、每负载 5 轮）**：

| 负载（N = 200 万） | 128 字节 | 32 字节 | 倍数 |
|---|---|---|---|
| 建列表 + 求和 | 1409.8 ms | **1149.0 ms** | **1.23x** |
| 只求和（对照，不建列表） | 301.1 ms | **181.2 ms** | **1.66x** |
| 递归斐波(27) | 340.7 ms | 360.3 ms | 0.95x（噪声内，函数调用密集**持平**） |
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


def _sizes() -> dict:
    if not _rust().exists():
        pytest.skip("Rust 宿主未构建（cd rust && cargo build --release）")
    r = subprocess.run([str(_rust()), "--dump-val-size"], capture_output=True, input=b"")
    return json.loads(r.stdout.decode("utf-8", "replace"))


def test_Val_的大小是32字节():
    """`Val` = **32 字节**（2026-10-05 瘦身后）。

    ⚠️ **再改小** → 请更新此断言 + `docs/设计决策.md` D77，并补**交错 A/B** 的实测；
    **改大** → 多半是把某个大载荷内联进来了（`Class` 120 / `Func` 88 / 模块载荷 72
    都是当年的元凶）——那属于「没报错、只是变慢」，**必须有意为之**。
    """
    info = _sizes()
    assert info["val"] == 32, (
        f"`Val` 现在 {info['val']} 字节（2026-10-05 瘦身后记录是 32）。"
        "改小请更新本断言与 D77 并补 A/B 实测；改大请检查是不是内联了大载荷。")


def test_大载荷保持装箱():
    """**机制钉子**：那三个大载荷必须**大于** `Val`（说明它们确实是**装箱**的，
    没有内联）—— 一旦有人「顺手」把它们拆开内联，`Val` 就会立刻回胖，而这条会先红。"""
    info = _sizes()
    for key, name in (("class", "Class"), ("func", "Func"), ("module_payload", "Module 载荷")):
        assert info[key] > info["val"], (
            f"{name} 载荷 {info[key]} 字节 ≤ Val {info['val']} —— 它是不是被内联回来了？"
            "内联 = Val 回胖 = 值栈/容器每格成本翻几倍（见 D77 §三）。")
