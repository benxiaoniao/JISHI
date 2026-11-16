# -*- coding: utf-8 -*-
"""M158 · **语言卡上的「一句话说明」必须与真实行为一致**。

## 为什么有这个文件

M154 给 41 个对象方法各写了一句说明（卡片是贴进模型提示词的那份）。
M158 写第 13 章教程时，照着卡片写代码 —— 连撞三次：

| 卡片上写的 | 真实行为 |
|---|---|
| `列表.索引`「**没有给 -1**」 | **报错**（`值错误：列表里没有「9」，找不到它的位置`） |
| `列表.排序按`「原地改」 | **返回新列表**，原列表不动（`排序`/`反转` 才是原地） |
| `文件.读行`「到末尾给空**文本**」 | 给 **`空`**（不是 `""`）⇒ `如果 行 == ""` 判结束会**死循环** |

📌 这正是 D98 那条「文档说谎」的同类问题，而卡片是**给模型看的** ——
错一句，模型就会照着写一段错的代码，而且报错点离原因很远。

## 这个文件怎么防

把「卡片上的说法」逐条写成**探针**，五个执行器各跑一遍，断言：
① 五个说法相同（跨引擎一致）；② 与卡片承诺的行为一致。
改行为或改文案，这条就会红 —— 逼着两边一起改。
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path, 去掉遮蔽提示          # noqa: E402

CVM驱动 = '''\
import sys
sys.path.insert(0, r"{root}")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(newline="\\n")
    except Exception:
        pass
from oracle.jishi import cvm_bind, errors as E

src = open("程序.jsh", encoding="utf-8").read()
try:
    cvm_bind.run_source_c(src, filename="程序.jsh")
except E.JishiError as e:
    e.with_source(src.replace("\\r\\n", "\\n").split("\\n"))
    sys.stderr.write(e.render() + "\\n")
    sys.exit(1)
'''

#: 一张「卡片说法 → 探针」的表。每行由探针打印 `<编号> <观察到的事实>`。
探针 = '''\
函数 取键(行)：
    返回 行

# 1 列表.索引：找到给下标（卡片：返回第一个匹配的位置）
令 a = [10, 20]
打印("1", a.索引(20))

# 2 列表.索引：没有就**报错**（卡片：没有就报错（不是给 -1））
尝试：
    a.索引(9)
捕获 异常 为 e：
    打印("2", e.类型)

# 3 文本.查找：没有给 **-1**（卡片：没有给 -1）
打印("3", "你好世界".查找("无"))

# 4 列表.排序按：**返回新列表**，原列表不动（卡片：返回新列表（原列表不动））
令 b = [3, 1, 2]
令 排好的 = b.排序按(取键)
打印("4", b, 排好的)

# 5 列表.排序：**原地**（对照：卡片没写，但同一族方法别搞反）
令 c = [3, 1, 2]
c.排序()
打印("5", c)

# 6 文件.读行：到末尾给 **空**（卡片：到末尾给 `空`（不是空文本））
导入 文件
文件.写文本("探针.txt", "一\\n")
令 f = 打开("探针.txt")
令 第一 = f.读行()
令 第二 = f.读行()
打印("6", 第一, 第二, 第二 == 空)
f.关闭()
文件.删除文件("探针.txt")

# 7 字典.包含：判的是**键**（卡片：判的是**键**，不是值）
令 d = {"甲": 1}
打印("7", d.包含("甲"), d.包含(1))

# 8 集合.移除 没有就报错；丢弃 不报错（卡片：移除报错 / 丢弃不报错）
令 集 = 集合([1, 2])
尝试：
    集.移除(9)
捕获 异常 为 e：
    打印("8", e.类型, 集.丢弃(9))
'''

#: 每条断言：编号 → (期望看到的那一行)
期望 = {
    "1": "1 1",
    "2": "2 值错误",
    "3": "3 -1",
    "4": "4 [3, 1, 2] [1, 2, 3]",
    "5": "5 [1, 2, 3]",
    "6": "6 一 空 真",
    "7": "7 真 假",
    "8": "8 值错误 空",
}


def _五份(tmp_path: Path) -> dict:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.jsh").write_text(探针, encoding="utf-8", newline="\n")
    (tmp_path / "驱动.py").write_text(
        CVM驱动.format(root=str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    表 = {
        "树遍历": [sys.executable, "-m", "oracle.jishi.cli", "--tree", "程序.jsh"],
        "Python VM": [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
        "C VM": [sys.executable, "驱动.py"],
    }
    rs = rust_exe_path()
    if rs.is_file():
        表["Rust 宿主"] = [str(rs), "程序.jsh"]
    node = shutil.which("node")
    if node:
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(探针, "程序.jsh")), encoding="utf-8")
        表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"),
                           "程序.json"]
    出 = {}
    for 名字, 命令 in 表.items():
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                           timeout=120, input=b"")
        出[名字] = 去掉遮蔽提示((r.stdout + r.stderr).decode("utf-8", "replace"))
    return 出


def test_八个说法五个执行器一致(tmp_path):
    出 = _五份(tmp_path)
    值 = set(出.values())
    assert len(值) == 1, "五个执行器说法不一致：\n" + "\n".join(
        f"—— {k} ——\n{v}" for k, v in 出.items())


@pytest.mark.parametrize("编号", sorted(期望))
def test_说法与真实行为一致(编号, tmp_path):
    """这里是**行为**的判据，不是文案的判据 —— 文案在 `test_m154_card` 里。"""
    出 = _五份(tmp_path)
    行们 = 出[sorted(出)[0]].strip().splitlines()
    实际 = [行 for 行 in 行们 if 行.startswith(编号 + " ")]
    assert 实际, f"探针里没有第 {编号} 条的输出：\n" + "\n".join(行们)
    assert 实际[0] == 期望[编号], (
        f"卡片承诺的是「{期望[编号]}」，实际是「{实际[0]}」—— "
        f"要么改实现，要么改卡片（`oracle/jishi/ai.py` 的 `_方法说明`）")
