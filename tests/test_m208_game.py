# -*- coding: utf-8 -*-
"""M208 · 游戏深化：操控 · 粒子 · 物理 · 寻路 · 瓦片动画 · 声音通道 · 帧统计。

## 这一站解决的是什么

M187 给了「精灵 / 瓦片 / 地图 / 存档 / 场景」，能画出会动的画面了；真做一个游戏时
撞出来的是这七件：

| 缺什么 | 谁撞出来的 |
|---|---|
| **按键映射**（动作 → 键） | `如果 按着("a") 或 按着("ArrowLeft")` 满篇改；边沿语义在 `或` 里会多消费 |
| **手柄** | 「键盘能玩、手柄不能玩」——摇杆与按钮要单独接 |
| **粒子** | 爆炸 / 烟 / 星星 —— 每帧手写 200 个圆圈，代码比游戏逻辑还长 |
| **A\\* 寻路** | 怪追人只会直线撞墙 |
| **瓦片动画** | 岩浆 / 瀑布 / 传送门 —— 一张静图看着像贴纸 |
| **物理（重力 / 台阶）** | 手写「往下掉、撞墙停、上台阶」写到第三遍就该进标准库了 |
| **声音通道** | 想「音效 0.8、音乐 0.4」得逐个声音设音量 |
| **帧耗时直方图** | 「有时候一顿」—— 说不出卡在哪、卡多少帧 |

## 判据怎么验

- **纯逻辑那几个**（粒子 / 物理 / 寻路 / 绑定表 / 动画）：三宿主（Rust / Python / Node）
  **跑同一段脚本，输出逐字对比** —— 包括浮点。粒子还额外钉「同种子 ⇒ 同结果」。
- **平台独占那几个**（手柄 / 粒子.画 / 帧统计 / 声音通道）：命令行报**同一句**平台错；
  wasm 侧用假 DOM / 假手柄 / 手工喂帧真跑。
- 帧统计的数据来自 rAF 时间戳，所以判据**自己喂帧**（0 / 16 / 32 …），完全确定。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path, tmp_bc_path  # noqa: E402

RUST_EXE = rust_exe_path()
WASM = ROOT / "site" / "jishi_wasm.wasm"
SHIM = ROOT / "wasm" / "jishi-wasm.js"

平台画布 = ("这个平台没有画布：「图形」模块只在网页 / 窗口里有"
            "（命令行和 Node 上没有画布）。要做图表请用「表格」，要出网页请用「站点」。")
平台操控 = ("这个平台没有键盘 / 手柄：「操控」模块只在网页 / 窗口里有"
            "（命令行和 Node 上没有输入设备）。")
平台声音 = ("这个平台没有声音：「声音」模块只在网页 / 窗口里有"
            "（命令行和 Node 上没有音频输出）。")


# ---------------------------------------------------------------------------
# 一、三宿主跑同一段脚本，输出逐字对比
# ---------------------------------------------------------------------------

def _跑三宿主(源: str, tmp: Path, 名: str) -> dict:
    """Rust / Python / Node 各跑一遍，返回 `{"rs"/"py"/"js": (rc, 输出)}`。

    ⚠️ 三边比的都是 **stdout**（脚本自己 `打印` 的那些）。浮点要靠脚本自己
    `文本.小数位(…)` 截断 —— 这既是判据的写法，也是**给用户看的写法**。
    """
    文件 = tmp / f"{名}-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    出: dict = {}

    if RUST_EXE.is_file():
        r = subprocess.run([str(RUST_EXE), str(文件)], capture_output=True,
                           text=True, encoding="utf-8", timeout=120)
        出["rs"] = (r.returncode, (r.stdout or "").replace("\r\n", "\n"))
    else:
        出["rs"] = None

    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                       capture_output=True, text=True, encoding="utf-8",
                       timeout=180, cwd=str(ROOT))
    出["py"] = (r.returncode, (r.stdout or "").replace("\r\n", "\n"))

    if shutil.which("node") is None:
        出["js"] = None
    else:
        from oracle.jishi import serialize
        from oracle.jishi.compiler import compile_source
        bc = tmp_bc_path(tmp / f"{名}-{os.getpid()}.json")
        bc.write_text(serialize.dumps(compile_source(源, str(文件))), encoding="utf-8")
        出["js"] = (r.returncode, (r.stdout or "").replace("\r\n", "\n"))
    return 出


def _三边一致(源: str, tmp: Path, 名: str) -> str:
    出 = _跑三宿主(源, tmp, 名)
    有 = {k: v for k, v in 出.items() if v is not None}
    assert 有, "一个宿主都跑不了"
    for k, (码, 文) in 有.items():
        assert 码 == 0, f"[{k}] 退出码 {码}：{文[:400]}"
    基准 = 有["rs"][1] if "rs" in 有 else 有["py"][1]
    for k, (_, 文) in 有.items():
        assert 文 == 基准, f"[{k}] 与基准不一致：\n基准={基准!r}\n实得={文!r}"
    return 基准


# ---------------------------------------------------------------------------
# 二、粒子
# ---------------------------------------------------------------------------

_粒子源 = '''导入 粒子
导入 文本 为 文

令 系 = 粒子.新建({"寿命": 2.0, "速度": [50, 150], "角度": [0, 360], "重力": 200,
                 "尺寸": [2, 4], "颜色": ["#f00", "#0f0"], "种子": 42})
粒子.发射(系, 100, 50, 6)
遍历 i 在 范围(4)：
    粒子.更新(系, 0.05)
打印(粒子.数量(系))
遍历 p 在 系["粒子们"]：
    打印(文.小数位(p["x"], 3), 文.小数位(p["y"], 3), 文.小数位(p["寿"], 3), p["颜色"])
'''


def test_粒子三宿主逐字一致(tmp_path):
 """同一种子 ⇒ 同一场爆炸，而且**三个宿主长出一模一样的粒子**。"""
 出 = _三边一致(_粒子源, tmp_path, "part")
 行 = 出.strip().splitlines()
 assert 行[0] == "6", 出
 assert len(行) == 7, 出


def test_粒子同种子可复现(tmp_path):
 源 = '''导入 粒子
导入 文本 为 文
函数 一次()：
    令 系 = 粒子.新建({"速度": 80, "角度": 45, "重力": 0, "种子": 7})
    粒子.发射(系, 0, 0, 4)
    粒子.更新(系, 0.1)
    令 出 = []
    遍历 p 在 系["粒子们"]：
        出.追加(文.小数位(p["x"], 4))
    返回 出
打印(一次() == 一次())
'''
 assert _三边一致(源, tmp_path, "seed") .strip() == "真"


def test_粒子不同种子不一样(tmp_path):
 源 = '''导入 粒子
导入 文本 为 文
函数 一次(种)：
    令 系 = 粒子.新建({"速度": [50, 150], "角度": [0, 360], "重力": 0, "种子": 种})
    粒子.发射(系, 0, 0, 5)
    令 出 = []
    遍历 p 在 系["粒子们"]：
        出.追加(文.小数位(p["速x"], 3))
    返回 出
打印(一次(1) == 一次(2))
'''
 assert _三边一致(源, tmp_path, "seed2").strip() == "假"


def test_粒子寿命到了就消失(tmp_path):
 源 = '''导入 粒子
令 系 = 粒子.新建({"寿命": 0.5, "速度": 10, "种子": 1})
粒子.发射(系, 0, 0, 3)
打印(粒子.更新(系, 0.3))
打印(粒子.更新(系, 0.3))
打印(粒子.数量(系))
打印(粒子.清(系))
'''
 出 = _三边一致(源, tmp_path, "life").strip().splitlines()
 assert 出 == ["3", "0", "0", "0"], 出


def test_粒子的错误都说得清(tmp_path):
 坏 = [
    ('粒子.发射(粒子.新建(), 0, 0, -1)', "不能是负数"),
    ('粒子.发射(粒子.新建(), 0, 0, 6000)', "最多 5000"),
    ('粒子.新建({"生命周期": 1})', "没有「生命周期」"),
    ('粒子.新建({"形状": "三角"})', "只有「矩形」和「圆」"),
    ('粒子.新建({"种子": -5})', "种子不能是负数"),
    ('粒子.新建({"寿命": [2, 1]})', "区间反了"),
  ]
 for i, (句, 要) in enumerate(坏):
  源 = f"导入 粒子\n{句}\n"
  文件 = tmp_path / f"bad{i}-{os.getpid()}.jsh"
  文件.write_text(源, encoding="utf-8", newline="\n")
  r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                     capture_output=True, text=True, encoding="utf-8",
                     cwd=str(ROOT), timeout=120)
  出 = (r.stdout or "") + (r.stderr or "")
  assert 要 in 出, f"`{句}` 没报出「{要}」：{出[:300]}"
  assert "Traceback" not in 出, 出[:400]


def test_粒子画在命令行报平台错(tmp_path):
 文件 = tmp_path / f"pdraw-{os.getpid()}.jsh"
 文件.write_text("导入 粒子\n粒子.画(粒子.新建())\n", encoding="utf-8", newline="\n")
 r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                    capture_output=True, text=True, encoding="utf-8",
                    cwd=str(ROOT), timeout=120)
 出 = (r.stdout or "") + (r.stderr or "")
 assert 平台画布 in 出, 出[:400]


# ---------------------------------------------------------------------------
# 三、物理
# ---------------------------------------------------------------------------

_图 = '''令 地 = 瓦片.载入({"图集": 1, "瓦片宽": 16, "瓦片高": 16,
    "行们": [[-1,-1,-1,-1,-1],[-1,-1,-1,-1,-1],[-1,-1,-1,-1,-1],[-1,-1,-1,-1,-1],
             [1,1,1,1,1]]})
'''


def test_物理自由落体落到地面(tmp_path):
 源 = "导入 物理\n导入 瓦片\n导入 文本 为 文\n" + _图 + '''令 体 = 物理.新建({"x": 8, "y": 0, "宽": 12, "高": 12, "重力": 1600})
遍历 i 在 范围(60)：
    物理.步(体, 地, 0.016, {"实心": [1]})
打印(文.小数位(体["y"], 3), 体["在地"])
'''
 出 = _三边一致(源, tmp_path, "fall").strip()
 assert 出.endswith("真"), 出
 assert abs(float(出.split()[0]) - 51.6) < 2.0, f"没落在第四行顶上（16*4-12=52）：{出}"


def test_物理空中不能跳_在地才能跳(tmp_path):
 源 = "导入 物理\n导入 瓦片\n" + _图 + '''令 体 = 物理.新建({"x": 8, "y": 0, "宽": 12, "高": 12, "重力": 1600})
打印(物理.跳(体))
遍历 i 在 范围(60)：
    物理.步(体, 地, 0.016, {"实心": [1]})
打印(体["在地"])
打印(物理.跳(体))
打印(体["速y"] < 0)
'''
 出 = _三边一致(源, tmp_path, "jump").strip().splitlines()
 assert 出 == ["假", "真", "真", "真"], 出


def test_物理摩擦把水平速度刹住(tmp_path):
 源 = "导入 物理\n导入 瓦片\n导入 文本 为 文\n" + _图 + '''令 体 = 物理.新建({"x": 8, "y": 40, "宽": 12, "高": 12, "重力": 1600, "摩擦": 8})
体["速x"] = 200
遍历 i 在 范围(120)：
    物理.步(体, 地, 0.016, {"实心": [1]})
打印(文.小数位(体["速x"], 3))
'''
 出 = _三边一致(源, tmp_path, "fric").strip()
 assert 出 == "0.000", 出


def _台阶图(行们):
 return ("令 地 = 瓦片.载入({\"图集\": 1, \"瓦片宽\": 16, \"瓦片高\": 16, \"行们\": "
         + json.dumps(行们) + "})\n")


def test_物理台阶能上一级_没有台阶就撞停(tmp_path):
 """台阶 16（正好一格）：抬一下就能站上去；不给台阶就撞停。这是「能不能上楼梯」的分水岭。

 地图：地面在第 4 行，第 3 行第 3~5 列有一块 16px 高的平台。角色从左边走过去：
 没有台阶 ⇒ 撞在 x≈36 停住；有台阶 ⇒ 抬一格站上去，继续走到平台尽头。
 """
 行们 = [[-1] * 9, [-1] * 9, [-1] * 9, [-1, -1, -1, 2, 2, 2, 2, 2, 2], [2] * 9]
 源 = ("导入 物理\n导入 瓦片\n导入 文本 为 文\n" + _台阶图(行们) +
      '''令 甲 = 物理.新建({"x": 8, "y": 52, "宽": 12, "高": 12, "重力": 1600})
甲["速x"] = 120
遍历 i 在 范围(25)：
    物理.步(甲, 地, 0.016, {"实心": [2]})
令 乙 = 物理.新建({"x": 8, "y": 52, "宽": 12, "高": 12, "重力": 1600})
乙["速x"] = 120
遍历 i 在 范围(25)：
    物理.步(乙, 地, 0.016, {"实心": [2], "台阶": 16})
打印(文.小数位(甲["x"], 1), 文.小数位(乙["x"], 1), 文.小数位(乙["y"], 1))
''')
 出 = _三边一致(源, tmp_path, "step").strip()
 甲x, 乙x, 乙y = [float(x) for x in 出.split()]
 assert 乙x > 甲x + 16, f"有台阶的没走更远：甲={甲x} 乙={乙x}"
 assert 乙y < 45, f"上了台阶却没抬高（应当站在第 3 行顶上 y≈36）：乙y={乙y}"


def test_物理高速不穿墙(tmp_path):
 """一帧走 300px 的大 dt + 大速度：不分步就会整块穿过去。"""
 # 墙竖在**角色那一行**（第 1 行第 5 列）：x 80..96 挡住去路
 行们 = [[-1] * 8, [-1] * 5 + [3] + [-1] * 2, [-1] * 8]
 源 = ("导入 物理\n导入 瓦片\n导入 文本 为 文\n" + _台阶图(行们) +
      '''令 体 = 物理.新建({"x": 20, "y": 20, "宽": 12, "高": 12, "重力": 0})
体["速x"] = 3000
物理.步(体, 地, 0.1, {"实心": [3]})
打印(体["x"] + 12 <= 80.001, 文.小数位(体["x"], 2))
''')
 出 = _三边一致(源, tmp_path, "wall").strip().split()
 assert 出[0] == "真", f"穿墙了：x={出[1]}（墙在第 1 格，右边界 16）"


# ---------------------------------------------------------------------------
# 四、寻路
# ---------------------------------------------------------------------------

def test_寻路直线与绕过墙(tmp_path):
 直 = [[-1] * 5 for _ in range(3)]
 墙 = [[-1] * 5, [-1, 1, 1, -1, -1], [-1] * 5]
 源 = ("导入 地图\n导入 瓦片\n" + _台阶图(直) + _台阶图(墙).replace("令 地", "令 墙") +
      '''打印(长度(地图.寻路(地, [0, 0], [4, 0], {"实心": [1]})))
令 路 = 地图.寻路(墙, [0, 1], [4, 1], {"实心": [1]})
打印(长度(路) > 5)
打印(路[0], 路[长度(路) - 1])
''')
 出 = _三边一致(源, tmp_path, "path").strip().splitlines()
 assert 出[0] == "5", 出
 assert 出[1] == "真", f"绕墙竟然不绕：{出}"
 assert 出[2] == "[0, 1] [4, 1]", 出


def test_寻路对角不许穿墙角(tmp_path):
 """只有「对角」相邻的两个空格：不许斜着钻过去，只能绕。"""
 # 只有对角相邻的两个空格，两侧都是墙 —— 真实规则不许「斜着钻过去」
 行们 = [[-1, 1], [1, -1]]
 源 = ("导入 地图\n导入 瓦片\n" + _台阶图(行们) +
      '''打印(长度(地图.寻路(地, [0, 0], [1, 1], {"实心": [1]})))
''')
 出 = _三边一致(源, tmp_path, "corner").strip()
 assert 出 == "0", f"从墙角缝里钻过去了（长度 {出}）—— 对角必须两侧都通"


def test_寻路走不通给空_起终点实心或出界才报错(tmp_path):
 断 = [[-1, 1, -1]]
 源 = ("导入 地图\n导入 瓦片\n" + _台阶图(断) +
      '打印(长度(地图.寻路(地, [0, 0], [2, 0], {"实心": [1]})))\n')
 assert _三边一致(源, tmp_path, "nopath").strip() == "0"

 坏 = [
    ('地图.寻路(地, [1, 0], [2, 0], {"实心": [1]})', "是实心瓦片"),
    ('地图.寻路(地, [0, 0], [9, 0], {"实心": [1]})', "出界了"),
  ]
 for i, (句, 要) in enumerate(坏):
  文件 = tmp_path / f"pbad{i}-{os.getpid()}.jsh"
  文件.write_text("导入 地图\n导入 瓦片\n" + _台阶图(断) + 句 + "\n",
                  encoding="utf-8", newline="\n")
  r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                     capture_output=True, text=True, encoding="utf-8",
                     cwd=str(ROOT), timeout=120)
  出 = (r.stdout or "") + (r.stderr or "")
  assert 要 in 出, f"`{句}` 没报出「{要}」：{出[:300]}"


def test_寻路起点等于终点(tmp_path):
 源 = "导入 地图\n导入 瓦片\n" + _台阶图([[-1, -1]]) + '''令 路 = 地图.寻路(地, [1, 0], [1, 0], {"实心": [1]})
打印(长度(路), 路[0])
'''
 出 = _三边一致(源, tmp_path, "same").strip()
 assert 出 == "1 [1, 0]", 出


# ---------------------------------------------------------------------------
# 五、瓦片动画
# ---------------------------------------------------------------------------

def test_瓦片动画按帧率推进(tmp_path):
 源 = '''导入 瓦片
令 图 = 瓦片.载入({"图集": 1, "瓦片宽": 8, "瓦片高": 8, "行们": [[4, 5, 6]]})
瓦片.加动画(图, 4, [4, 5, 6], 10)
打印(瓦片.动画帧(图, 4))
瓦片.更新动画(图, 0.05)
打印(瓦片.动画帧(图, 4))
瓦片.更新动画(图, 0.06)
打印(瓦片.动画帧(图, 4))
瓦片.更新动画(图, 0.3)
打印(瓦片.动画帧(图, 4))
遍历 i 在 范围(3)：
    瓦片.更新动画(图, 0.1)
打印(瓦片.动画帧(图, 4))
'''
 出 = _三边一致(源, tmp_path, "anim").strip().splitlines()
 # 帧率 10 ⇒ 每 0.1 秒一帧；跨多帧要一次跳够（0.3 秒 = 3 帧）
 assert 出 == ["4", "4", "5", "5", "5"], 出


def test_瓦片动画的错误(tmp_path):
 坏 = [
    ('瓦片.加动画(图, -1, [1])', "编号不能是负数"),
    ('瓦片.加动画(图, 1, [])', "不能是空列表"),
    ('瓦片.加动画(图, 1, [1, -1])', "是负数"),
    ('瓦片.加动画(图, 1, [1], 0)', "帧率要大于 0"),
  ]
 for i, (句, 要) in enumerate(坏):
  文件 = tmp_path / f"abad{i}-{os.getpid()}.jsh"
  文件.write_text('导入 瓦片\n令 图 = 瓦片.载入({"图集": 1, "瓦片宽": 8, "瓦片高": 8, '
                  '"行们": [[1, 2]]})\n' + 句 + "\n",
                  encoding="utf-8", newline="\n")
  r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                     capture_output=True, text=True, encoding="utf-8",
                     cwd=str(ROOT), timeout=120)
  出 = (r.stdout or "") + (r.stderr or "")
  assert 要 in 出, f"`{句}` 没报出「{要}」：{出[:300]}"


# ---------------------------------------------------------------------------
# 六、操控（绑定是纯逻辑，查键要设备）
# ---------------------------------------------------------------------------

def test_操控绑定表三宿主一致(tmp_path):
 源 = '''导入 操控
打印(操控.绑定("左", ["a", "ArrowLeft"]))
打印(操控.绑定("右", "d"))
打印(操控.绑定("左", ["A", "A"]))
打印(操控.绑定表())
打印(操控.解绑("右"))
打印(操控.解绑("没有这个"))
打印(操控.绑定表())
打印(操控.绑定("跳", []))
'''
 出 = _三边一致(源, tmp_path, "ctl").strip().splitlines()
 assert 出[0] == "真" and 出[1] == "真", 出
 assert 出[2] == "真", "重复绑定同一动作应当返回真（覆盖）"
 assert "左" in 出[3] and "右" in 出[3], 出[3]
 # 同一动作绑两次同一个键 → 去重
 assert 出[3].count("A") == 1, f"键没去重：{出[3]}"
 assert 出[4] == "真" and 出[5] == "假", 出
 assert "右" not in 出[6], 出[6]
 assert 出[7] == "假", "绑空列表应当返回假（等于解绑）"


def test_操控查键在命令行报平台错(tmp_path):
 for i, 句 in enumerate(['操控.按下("左")', '操控.按过("左")',
                         '操控.轴("左", "右")', '操控.手柄()']):
  源 = ("导入 操控\n操控.绑定(\"左\", [\"a\"])\n操控.绑定(\"右\", [\"d\"])\n"
       + 句 + "\n")
  文件 = tmp_path / f"ctlbad{i}-{os.getpid()}.jsh"
  文件.write_text(源, encoding="utf-8", newline="\n")
  r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                     capture_output=True, text=True, encoding="utf-8",
                     cwd=str(ROOT), timeout=120)
  出 = (r.stdout or "") + (r.stderr or "")
  assert 平台操控 in 出, f"`{句}` 没报平台错：{出[:300]}"


# ---------------------------------------------------------------------------
# 七、声音通道 / 帧统计：命令行报同一句话
# ---------------------------------------------------------------------------

def test_声音通道四个函数命令行报平台错(tmp_path):
 for i, 句 in enumerate(['声音.建通道("音效")', '声音.通道音量("音效", 0.5)',
                         '声音.播到("音效", 1)', '声音.通道们()']):
  文件 = tmp_path / f"snd{i}-{os.getpid()}.jsh"
  文件.write_text("导入 声音\n" + 句 + "\n", encoding="utf-8", newline="\n")
  r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                     capture_output=True, text=True, encoding="utf-8",
                     cwd=str(ROOT), timeout=120)
  出 = (r.stdout or "") + (r.stderr or "")
  assert 平台声音 in 出, f"`{句}` 没报平台错：{出[:300]}"


def test_帧统计在命令行报没有帧数据(tmp_path):
 for 句 in ['图形.帧统计()', '图形.帧统计(20)']:
  文件 = tmp_path / f"fs-{os.getpid()}.jsh"
  文件.write_text("导入 图形\n" + 句 + "\n", encoding="utf-8", newline="\n")
  r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                     capture_output=True, text=True, encoding="utf-8",
                     cwd=str(ROOT), timeout=120)
  出 = (r.stdout or "") + (r.stderr or "")
  assert "还没有帧数据" in 出, f"`{句}` 没报「还没有帧数据」：{出[:300]}"


# ---------------------------------------------------------------------------
# 八、wasm 侧（真跑）：手柄 / 按键 / 粒子画 / 瓦片动画画 / 帧统计
# ---------------------------------------------------------------------------

_JS = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);
const 模式 = process.argv[4];

// ---- 假 canvas（只记指令，判据看 _记录）----
function 造世界() {
  const 记录 = [];
  const c = new Proxy({}, {
    get: (t, k) => {
      if (k === "getImageData") return (x, y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4) });
      if (k === "measureText") return (s) => ({ width: String(s).length * 8 });
      if (k === "createLinearGradient" || k === "createRadialGradient") {
        return () => ({ __grad: 1, addColorStop() {} });
      }
      return (...a) => { 记录.push([k].concat(a)); };
    },
    set: () => true
  });
  const cv = { width: 0, height: 0, style: {}, clientWidth: 800, clientHeight: 600,
               getContext: () => c };
  const doc = { querySelector: () => cv, createElement: () => cv,
                body: { appendChild() {} } };
  return { doc, 记录 };
}

// ---- 假 window：能派发按键 ----
function 装假窗口() {
  const 听 = {};
  globalThis.addEventListener = (名, fn) => { (听[名] = 听[名] || []).push(fn); };
  globalThis.派 = (名, ev) => { for (const f of (听[名] || [])) f(ev); };
}

// ---- 假 Image：`src` 一设就「加载完」（判据要看 drawImage 的参数）----
function 装假图() {
  globalThis.Image = class {
    constructor() { this.width = 64; this.height = 64; }
    set src(v) { this._src = v; if (this.onload) this.onload(); }
    get src() { return this._src; }
  };
}

// ---- 假手柄 ----
function 装假手柄(接上) {
  // ⚠️ Node 18+ 自带一个**只读的** `globalThis.navigator`（没有 getGamepads），
  //    直接 `globalThis.navigator = …` 在非严格模式下**静默失败** ——
  //    于是判据拿到的是「没连手柄」。必须 defineProperty 覆盖。
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      getGamepads: () => [接上 ? {
        connected: true,
        axes: [0.9, -0.5, 0.05, 0.05],
        buttons: [{ pressed: true }, { pressed: false }, { pressed: false },
                  { pressed: false }, { pressed: false }, { pressed: false },
                  { pressed: false, value: 0.7 }, { pressed: false, value: 0 }]
      } : null]
    }
  });
}

(async () => {
  const { doc, 记录 } = 造世界();
  装假窗口();
  装假图();
  装假手柄(模式 === "手柄");

  let 帧序列 = [];
  const j = await JishiWasm.load(BYTES, {
    doc,
    raf: () => 0,
    root: globalThis
  });

  function 跑(源) {
    const r = j.run(源);
    if (!r.ok) { console.log(JSON.stringify({ 错: r.error.message })); process.exit(0); }
    return r.stdout;
  }

  if (模式 === "手柄") {
    装假手柄(true);
    跑(`导入 操控
操控.绑定("左", ["a", "ArrowLeft"])
`);
    const 源 = `导入 操控
令 柄 = 操控.手柄()
打印(柄["连接"], 柄["左x"], 柄["左y"], 柄["按钮们"][0], 柄["扳机们"][0])
打印(操控.手柄(3)["连接"])
`;
    console.log(JSON.stringify({ 出: 跑(源) }));
    process.exit(0);
  }

  if (模式 === "按键") {
    // ⚠️ 输入监听是 `图形.准备` 时挂上去的（shim `_g准备` → `_挂输入`）——
    //    不准备画布就没有 keydown 监听，判据永远读到「没按」。
    跑(`导入 图形
导入 操控
图形.准备({"宽": 100, "高": 100})
操控.绑定("左", ["a", "ArrowLeft"])
操控.绑定("右", ["d", "ArrowRight"])
`);
    const 探 = `导入 操控
打印(操控.按下("左"), 操控.按下("右"), 操控.轴("左", "右"))
`;
    const 前 = 跑(探);
    派("keydown", { key: "a", repeat: false, preventDefault() {} });
    const 中 = 跑(探);
    // 边沿：按过只真一次
    const 边 = `导入 操控
打印(操控.按过("左"))
打印(操控.按过("左"))
`;
    派("keydown", { key: "d", repeat: false, preventDefault() {} });
    const 边1 = 跑(边);
    派("keyup", { key: "a" });
    派("keyup", { key: "d" });
    const 后 = 跑(探);
    // 两个都按着 ⇒ 轴 0
    派("keydown", { key: "a", repeat: false, preventDefault() {} });
    派("keydown", { key: "d", repeat: false, preventDefault() {} });
    const 轴0 = 跑(探);
    // 没绑定的动作：要说清「哪个动作」+「先绑定」
    const 未 = j.run(`导入 操控
操控.绑定("左", ["a"])
打印(操控.按下("跳跃"))
`);
    const 未绑 = 未.ok ? "没报错" : ((未.error.message || "") + " | "
      + (未.error.hint || ""));
    console.log(JSON.stringify({ 前, 中, 边1, 后, 轴0, 未绑 }));
    process.exit(0);
  }

  if (模式 === "粒画") {
    跑(`导入 图形
图形.准备({"宽": 200, "高": 100})
`);
    const 源 = `导入 图形
导入 粒子
令 系 = 粒子.新建({"寿命": 1, "速度": 0, "角度": 0, "重力": 0, "尺寸": 3,
                 "颜色": ["#f00"], "形状": "矩形", "种子": 1})
粒子.发射(系, 50, 40, 2)
粒子.画(系)
`;
    跑(源);
    // 两次 发射+画：指令里应当出现 4 次 rect
    const 前 = 记录.length;
    跑(源);
    // 纯填充矩形走的是 `fillRect`（`rect` 只在描边/圆角那条路上）
    const 增 = 记录.slice(前).filter((x) => x[0] === "fillRect").length;
    console.log(JSON.stringify({ 增, 总: 记录.length }));
    process.exit(0);
  }

  if (模式 === "瓦片画") {
    const 源 = `导入 图形
导入 瓦片
图形.准备({"宽": 200, "高": 100})
令 集 = 图形.读图("tile.png")
令 图 = 瓦片.载入({"图集": 集, "瓦片宽": 8, "瓦片高": 8, "行们": [[4, -1, -1]]})
瓦片.加动画(图, 4, [4, 5, 6], 10)
瓦片.更新动画(图, 0.15)
打印(瓦片.动画帧(图, 4))
瓦片.画(图)
`;
    const 出 = 跑(源);
    const 画 = 记录.filter((x) => x[0] === "drawImage");
    console.log(JSON.stringify({ 出, 次数: 画.length,
      最后一次: 画.length ? 画[画.length - 1] : null }));
    process.exit(0);
  }

  if (模式 === "帧统计") {
    const 源 = `导入 图形
图形.准备({"宽": 100, "高": 100})
函数 开始()：
    空
函数 每帧(号, 间隔)：
    如果 号 == 8：
        令 s = 图形.帧统计()
        打印("帧数", s["帧数"], "最大", s["最大"], "卡顿", s["卡顿"])
        打印("桶2", s["桶们"][2]["数"])
        图形.清帧统计()
        尝试：
            图形.帧统计()
            打印("清空没生效")
        捕获 异常 为 e：
            打印("清空后", e.消息[0:6], 间隔 == 16)
图形.运行(开始, 每帧)
`;
    const r = j.run(源);
    if (!r.ok) { console.log(JSON.stringify({ 错: r.error.message })); process.exit(0); }
    // 手工喂帧：0 / 16 / 32 …（第一帧 dt = 0，不记）
    let 出 = "";
    for (let i = 0; i <= 9; i++) {
      const x = j.frame(i * 16);
      出 += x.out || "";
    }
    console.log(JSON.stringify({ 出, 帧中: j.framesActive() }));
    process.exit(0);
  }

  console.log(JSON.stringify({ 错: "未知模式 " + 模式 }));
})();
'''


@pytest.fixture(scope="module")
def _wasm可用():
 if not WASM.is_file():
  pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
 if shutil.which("node") is None:
  pytest.skip("没有 node")


def _跑js(tmp: Path, 模式: str, _wasm可用=None):
 脚本 = tmp / f"m208-{模式}-{os.getpid()}.js"
 脚本.write_text(_JS, encoding="utf-8", newline="\n")
 r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM), 模式],
                    capture_output=True, timeout=180)
 assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:800]
 文 = r.stdout.decode("utf-8", "replace").strip()
 行 = [x for x in 文.splitlines() if x.strip().startswith("{")]
 assert 行, 文[:400]
 d = json.loads(行[-1])
 assert "错" not in d, d
 return d


def test_wasm_手柄(tmp_path, _wasm可用):
 d = _跑js(tmp_path, "手柄")
 首 = d["出"].strip().splitlines()[0].split()
 assert 首[0] == "真", d
 # 摇杆 0.9 / -0.5；0.05 在死区里 ⇒ 0
 assert abs(float(首[1]) - 0.9) < 1e-6, 首
 assert abs(float(首[2]) + 0.5) < 1e-6, 首
 assert 首[3] == "真", 首
 assert abs(float(首[4]) - 0.7) < 1e-6, 首
 assert d["出"].strip().splitlines()[1] == "假", "没连的手柄该给 连接: 假"


def test_wasm_按键与边沿(tmp_path, _wasm可用):
 d = _跑js(tmp_path, "按键")
 assert d["前"].strip() == "假 假 0", d["前"]
 assert d["中"].strip() == "真 假 -1", d["中"]
 assert d["边1"].strip().splitlines() == ["真", "假"], d["边1"]
 assert d["后"].strip() == "假 假 0", d["后"]
 assert d["轴0"].strip() == "真 真 0", f"两边都按着该给 0：{d['轴0']}"
 assert "跳跃" in d["未绑"] and "先写" in d["未绑"], f"未绑定的报错没说清：{d['未绑']}"


def test_wasm_粒子的画走批量(tmp_path, _wasm可用):
 d = _跑js(tmp_path, "粒画")
 # 2 个粒子 ⇒ 2 次 rect（批量里每项一次）
 assert d["增"] == 2, f"2 个粒子应当画 2 个 fillRect：{d}"


def test_wasm_瓦片动画真的画成另一帧(tmp_path, _wasm可用):
 d = _跑js(tmp_path, "瓦片画")
 assert d["次数"] == 1, d
 帧 = int(d["出"].strip())
 assert 帧 == 5, f"0.15 秒（帧率 10）应当走到第 5 帧：{d['出']!r}"
 参 = d["最后一次"]
 # drawImage(图, 源x, 源y, 源宽, 源高, …)：帧 5、列数 3 ⇒ 源 (16, 8)
 assert abs(float(参[2]) - 16) < 1e-6 and abs(float(参[3]) - 8) < 1e-6, \
     f"画的不是第 5 帧的图块（应当是源 (16, 8)）：{参}"


def test_wasm_帧统计能定位卡顿(tmp_path, _wasm可用):
 d = _跑js(tmp_path, "帧统计")
 出 = d["出"]
 assert "帧数 8" in 出, f"第一帧 dt=0 不该记，九帧应当数出 8：{出}"
 assert "最大 16" in 出, 出
 assert "卡顿 0" in 出, out_ok(出)
 # 8 个 16ms 的样本全在 [16,33) 这一桶（桶们[2]）
 assert "桶2 8" in 出, 出
 # 清完再读 ⇒ 报「还没有帧数据」
 assert "清空后 还没有帧" in 出, 出


def out_ok(出):
 return 出[:400]
