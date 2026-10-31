# -*- coding: utf-8 -*-
"""M154 · 语言卡要补上「会踩的坑」，方法也要带一句话。

0.2.10 前四个里程碑（M150–M153）都是**真实项目撞出来的**东西，而语言卡
（`--ai-card`，贴进模型系统提示词那份）**一个字都没提**：模型只能照别的语言的
经验猜 —— 猜「`导入 文本` 之后 `文本(x)` 还能用」、猜「`输入()` 读不到会返回空」、
猜「四舍五入是银行家舍入」。这些猜错，代价是白跑一轮。

所以这一站只做两件事（都便宜、都直接提分）：

1. 卡里加一段「**容易踩的坑**」（五条，全部有对应里程碑与判据）；
2. **对象方法带一句话说明** —— 以前只有名字，`列表.索引` 返回位置还是真假？
   `文本.查找` 找不到给什么？`字典.包含` 判键还是判值？全靠猜。

## 判据口径

- 内容在不在（五条坑 / 方法说明）；
- **卡片里提到的错误码必须真的查得到**（`jishi 错误 E20xx` 不能回「没有这个码」）——
  这防的是「卡里写了个不存在的码」这种最难发现的说谎；
- 两个宿主（Python / Rust）三种形态（全量 / 单模块 / 任务）**逐字节一致**。
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                        # noqa: E402


def _卡(*参数: str) -> str:
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "--ai-card", *参数],
                       capture_output=True, cwd=str(ROOT), timeout=60)
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def test_五条坑都在全量卡里():
    """这五条各自对应一个里程碑 —— 少一条就说明有人把卡改回「只讲语法」了。"""
    卡 = _卡()
    for 关键词 in [
        "容易踩的坑",
        "关键字不能当名字",       # M152
        "别名导入",               # M152 / M116（模块遮内建）
        "E2011",                  # M151（输入不是 UTF-8）
        "Ctrl+Z",                 # M151（输入结束）
        "文本.小数位",            # M153
        "半入",                   # M153（四舍五入口径）
        "{{",                     # M152（格式化转义花括号）
    ]:
        assert 关键词 in 卡, f"卡里没提「{关键词}」"


def test_方法带一句话说明():
    """名字猜不出的那几个必须有说明；一眼能看懂的（清空/反转）保持裸名。"""
    卡 = _卡()
    方法段 = 卡[卡.index("## 对象方法"):卡.index("## 运算符")]
    # ⚠️ M158 订正：列表的「索引」**没有就报错**（不是 -1），文本的「查找」才是给 -1。
    #    这条判据以前把两者写成同一句 —— 卡片（给模型看的）错了整整两个版本，
    #    写第 13 章教程时照卡片写 `如果 列表.索引(x) == -1` 直接报错才发现。
    assert "索引（返回第一个匹配的位置，**没有就报错**（不是给 -1））" in 方法段
    assert "查找（返回子串位置，**没有给 -1**）" in 方法段
    assert "包含（判的是**键**，不是值）" in 方法段      # 字典那行
    assert "写（**覆盖**整个文件）" in 方法段
    # ⚠️ M158 订正：末尾给的是 **`空`**（不是空文本）—— 照旧文案写
    #    `如果 行 == ""` 判结束会**死循环**。
    assert "读行（读一行，**到末尾给 `空`**（不是空文本 —— 别拿 `== \"\"` 判结束））" in 方法段
    # 一眼能看懂的不要加嘴（加了会把卡片撑肿，而卡片是花 prompt 预算的）
    assert "反转（" not in 方法段, "「反转」这种不需要说明"
    assert "清空（" not in 方法段


def test_坑段只进全量与任务卡_不进单模块分片():
    """分片的意义就是「只要这一个模块」—— 坑段 700 字能把 3800 字的分片撑到 4500。

    与「已装的包」同一规矩（`test_m149` 钉着那边）。
    """
    assert "容易踩的坑" in _卡()
    assert "容易踩的坑" in _卡("--任务", "文本")
    assert "容易踩的坑" not in _卡("文本")


def test_卡里提到的错误码都查得到():
    """**卡片不许说谎**：卡里写 `E2011`，用户/模型回手一查就得有解释。

    ⚠️ 这条是给「改文案的人」的护栏：`E2010` 曾经真的查不到（码表只从子类抽，
    漏了 `EXTRA_CODES`），卡片却一直印着它。
    """
    卡 = _卡()
    码们 = sorted(set(re.findall(r"\bE\d{4}\b", 卡)))
    assert 码们, "卡里一个错误码都没有？"
    for 码 in 码们:
        r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "错误", 码],
                           capture_output=True, cwd=str(ROOT), timeout=60)
        文本 = (r.stdout + r.stderr).decode("utf-8", "replace")
        assert "没有错误码" not in 文本, f"卡里写了 {码}，但 `jishi 错误 {码}` 查不到"


@pytest.mark.parametrize("参数", [(), ("文本",), ("--任务", "文本")], ids=["全量", "单模块", "任务"])
def test_两个宿主的卡逐字节一致(参数):
    卡 = _卡(*参数)
    rs = rust_exe_path()
    if not rs.is_file():
        pytest.skip("本机没有 Rust 宿主")
    r = subprocess.run([str(rs), "--ai-card", *参数], capture_output=True,
                       cwd=str(ROOT), timeout=60)
    assert (r.stdout + r.stderr).decode("utf-8", "replace") == 卡
