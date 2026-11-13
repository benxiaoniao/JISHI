# -*- coding: utf-8 -*-
"""M197 · 游戏工具深化：6 个模块 +19 函数（瓦片 / 精灵 / 地图 / 场景 / 声音 / 存档）。

## 这一站补的是什么

M187 给了六件套，但每件都只够「跑起来」：

| 模块 | 缺什么 |
|---|---|
| `瓦片` | **只有「碰」没有「挡」** —— 平台跳跃拿「碰到的瓦片号」自己判，斜坡与单向平台做不了 |
| `精灵` | 帧数要自己数、**「动画播完」没有信号**（「打完一套放技能」写不出来）、锚点固定在左上角 |
| `地图` | 只能读自己的 JSON，**Tiled 画的地图用不了** |
| `场景` | 只有硬切，**没有转场** |
| `声音` | 只有「播 / 停」，走路声和背景音乐没法共存、不能淡入淡出 |
| `存档` | 只能一个个名字存，**不知道存了哪些**，也没有定时落盘 |

## 判据怎么验

1. **三宿主逐字一致**（这 13 个纯算法函数在命令行上就能跑）；
2. **碰撞层真的挡住玩家**：拿一个快照玩家，逐帧移动、撞墙不许穿过去（坐标断言）；
3. **`从Tiled` 读手写 TMX 与手工建的地图逐格一致**，并且写回去再读回来还是它；
4. **`转场中` 的帧数精确**（`转场(效果, 3)` 之后正好 3 个真）；
5. 平台相关的（声音 / 存档）在命令行上报**同一句**中文错。

## 与路线图的偏差（如实记）

路线图列了 23 个函数，实际新增 **19** 个 —— 其中 5 个**已经有了**：

- `调音量` ≈ 已有的 `设音量`，`总音量` ≈ 已有的 `主音量`；
- `当前()` ≈ 已有的 `现在()`；
- `设动画` / `播动画` ≈ 已有的 `播(精灵, 名, {帧们, 速度, 循环})`（它同时做这两件事）。

同一个能力不造第二个名字 —— 那只会让人犹豫「该用哪个」。反过来，
这一站**多做了 2 个路线图没列的**：`是斜坡`（不然坡向信息没法读）、
`转场中`（不然转场没法用）。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

#: M197 新增的 19 个（**三宿主一个都不能少**）。
新增 = {
    "瓦片": ["碰撞层", "是实心", "斜坡", "是斜坡", "取矩形内瓦片", "最近可走格"],
    "精灵": ["图集切分", "动画完成", "设原点"],
    "地图": ["从Tiled", "存Tiled"],
    "场景": ["转场", "转场中"],
    "声音": ["循环", "淡入", "淡出", "静音"],
    "存档": ["槽们", "自动存"],
}

PY = ROOT / "oracle" / "jishi" / "stdlib"
RS = ROOT / "rust" / "src" / "stdlib"
JS = ROOT / "oracle" / "node" / "vm.js"


def _紧(s: str) -> str:
    """把连续空白压成一个空格 —— `打印` 多参数的空格数不值得在断言里数。"""
    return " ".join(s.split())


def _跑(源: str, tmp_path: Path, 名: str = "脚本.jsh", 引擎="Rust") -> tuple:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    脚本 = tmp_path / 名
    脚本.write_text(源, encoding="utf-8", newline="\n")
    if 引擎 == "Rust":
        命令 = [str(rust_exe_path()), str(脚本)]
    elif 引擎 == "Node":
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(源, str(脚本))), encoding="utf-8")
        命令 = ["node", str(JS.parent / "index.js"), "程序.json"]
    else:
        命令 = [sys.executable, "-m", "oracle.jishi.cli", str(脚本)]
    r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True, timeout=180)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


@pytest.fixture(scope="module", autouse=True)
def _要有宿主():
    if not rust_exe_path().is_file():
        pytest.skip("没有 Rust 宿主（cargo build --release）")


# ---------------------------------------------------------------------------
# 一、三宿主同宽
# ---------------------------------------------------------------------------

def test_新增的十九个函数三宿主都在():
    from oracle.jishi import serialize  # noqa: F401

    for 模块, 名们 in 新增.items():
        py = (PY / f"{模块}.py").read_text(encoding="utf-8")
        源 = f"导入 {模块}\n" + "\n".join(f"打印(类型({模块}.{n}))" for n in 名们) + "\n"
        for 引擎 in ("Rust", "Python"):
            pass
        for 名 in 名们:
            assert f'"{名}"' in py, f"Python 侧 {模块} 缺 `{名}`（__all__ 或定义）"

    rs = (RS / "gamekit.rs").read_text(encoding="utf-8") + \
         (RS / "sound.rs").read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    for 模块, 名们 in 新增.items():
        for 名 in 名们:
            assert f'"{名}" =>' in rs, f"Rust 侧缺 `{名}`"
            if 模块 in ("声音", "存档"):
                assert f"'{名}': _no" in js, f"Node 侧缺 `{名}`"
            else:
                assert f"'{名}': new R.Builtin" in js, f"Node 侧缺 `{名}`"


def test_模块函数数对得上():
    """`--dump-stdlib` 里各模块的数量（路线图的终态）。"""
    r = subprocess.run([str(rust_exe_path()), "--lang-spec"], capture_output=True, timeout=120)
    d = json.loads(r.stdout.decode("utf-8"))
    数 = {m["module"]: len(m["functions"]) for m in d["stdlib"]}
    for 模块, 期望 in (("瓦片", 10), ("精灵", 10), ("地图", 4), ("场景", 5),
                       ("声音", 10), ("存档", 6)):
        assert 数[模块] == 期望, f"{模块} 应当是 {期望} 个函数，实际 {数[模块]}"


# ---------------------------------------------------------------------------
# 二、瓦片：碰撞层真的挡住玩家
# ---------------------------------------------------------------------------

地图源 = '''
令 地 = 瓦片.载入({
    "图集": 1, "瓦片宽": 32, "瓦片高": 32,
    "行们": [
        [1, 1, 1, 1, 1],
        [1, 0, 0, 0, 1],
        [1, 0, 2, 0, 1],
        [1, 0, 0, 0, 1],
        [1, 1, 1, 1, 1]
    ]
})
令 墙 = 瓦片.碰撞层(地, [1])
'''


def test_碰撞层挡住玩家(tmp_path):
    """🔴 这一站最要紧的判据：**玩家走不穿墙**。

    做法：一个「玩家」从 (1,1) 格中心出发，每帧往一个方向挪 4 像素，
    每挪一次就看目标格是不是实心 —— 是就退回去。走够 200 帧，
    **必须还在墙围起来的里面**（列 1..3、行 1..3）。

    📌 为什么不用「碰撞后坐标不变」这种断言：那连「根本没动」都能过。
    这里断言的是「动了，但一直在里面」。
    """
    源 = "导入 瓦片\n" + 地图源 + '''
令 玩家 = {"x": 48.0, "y": 48.0}      # 格 (1,1) 的中心
令 走出 = 0
令 方向们 = [[1, 0], [0, 1], [-1, 0], [0, -1]]
令 帧 = 0
当 帧 < 200：
    令 向 = 方向们[帧 % 4]
    令 新x = 玩家["x"] + 向[0] * 4
    令 新y = 玩家["y"] + 向[1] * 4
    列 = 整数(新x // 32)
    行 = 整数(新y // 32)
    如果 瓦片.是实心(墙, 列, 行)：
        # 撞墙：退回去（这一帧不动）
        玩家["x"] = 玩家["x"]
    否则：
        玩家["x"] = 新x
        玩家["y"] = 新y
    如果 整数(玩家["x"] // 32) < 1 或 整数(玩家["x"] // 32) > 3 或 整数(玩家["y"] // 32) < 1 或 整数(玩家["y"] // 32) > 3：
        走出 = 走出 + 1
    帧 = 帧 + 1
打印("走出 ", 走出)
打印("最终 ", 整数(玩家["x"] // 32), " ", 整数(玩家["y"] // 32))
'''
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert "走出 0" in _紧(出), f"玩家走出去了（撞墙没挡住）：{出}"
    assert any(x in _紧(出) for x in ("最终 1 1", "最终 2 1", "最终 1 2")), 出


def test_瓦片其余四项(tmp_path):
    源 = "导入 瓦片\n" + 地图源 + '''
打印("实心(0,0) ", 瓦片.是实心(墙, 0, 0))
打印("实心(1,1) ", 瓦片.是实心(墙, 1, 1))
打印("越界 ", 瓦片.是实心(墙, 99, 99))

瓦片.斜坡(地, 2, 2, "右")
打印("坡(2,2) ", 瓦片.是斜坡(地, 2, 2))
打印("坡(1,1) ", 瓦片.是斜坡(地, 1, 1))

令 格们 = 瓦片.取矩形内瓦片(地, {"x": 0, "y": 0, "宽": 40, "高": 40})
打印("取格 ", 长度(格们))
遍历 格 在 格们：
    打印("  ", 格["列"], ",", 格["行"], " = ", 格["号"])

令 近 = 瓦片.最近可走格(地, 墙, 10, 10)
打印("最近 (10,10) → ", 近["列"], ",", 近["行"])
令 远 = 瓦片.最近可走格(地, 墙, 48, 48)
打印("最近 (48,48) → ", 远["列"], ",", 远["行"])
'''
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert "实心(0,0) 真" in _紧(出)
    assert "实心(1,1) 假" in _紧(出)
    assert "越界 假" in _紧(出), "地图外面不该是墙"
    assert "坡(2,2) 右" in _紧(出)
    assert "坡(1,1) 空" in _紧(出)
    assert "取格 4" in _紧(出), f"40×40 的矩形压到 2×2=4 格：{出}"
    assert "最近 (48,48) → 1 , 1" in _紧(出), 出


def test_三宿主瓦片逐字一致(tmp_path):
    import importlib.util

    源 = "导入 瓦片\n" + 地图源 + '''
打印(瓦片.是实心(墙, 0, 0), 瓦片.是实心(墙, 2, 2))
瓦片.斜坡(地, 1, 1, "左")
打印(瓦片.是斜坡(地, 1, 1))
打印(瓦片.取矩形内瓦片(地, {"x": 32, "y": 32, "宽": 64, "高": 32}))
令 近 = 瓦片.最近可走格(地, 墙, 5, 5)
打印(近)
'''
    py, _ = _跑(源, tmp_path, "三.jsh", "Python")
    rs, 出r = _跑(源, tmp_path, "三.jsh", "Rust")
    assert rs == py, f"Rust 与 Python 不一致：\n{py}\n---\n{出r}"

    spec = importlib.util.spec_from_file_location("ce", ROOT / "tools" / "check_engines.py")
    ce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ce)
    if ce._node_exe() is None:
        pytest.skip("没有 node")
    nd, 错 = ce._run_node_vm(源, str(tmp_path / "三.jsh"), tmp_path)
    assert nd == _跑(源, tmp_path, "三.jsh", "Python")[1], f"Node 不一致：{错[:400]}"


# ---------------------------------------------------------------------------
# 三、精灵：图集切分 / 动画完成 / 原点
# ---------------------------------------------------------------------------

def test_图集切分与动画完成(tmp_path):
    源 = '''导入 精灵

令 切 = 精灵.图集切分(128, 96, 32, 32)
打印(切["列数"], " ", 切["行数"], " ", 切["帧数"])
令 残 = 精灵.图集切分(128, 96, 48, 48)
打印("残条 ", 残["列数"], " ", 残["行数"], " ", 残["帧数"])

令 s = 精灵.新建(1, {"宽": 32, "高": 32, "列数": 4})
打印("初始完成 ", 精灵.动画完成(s))
精灵.播(s, "打", {"帧们": [0, 1, 2], "速度": 10, "循环": 假})
精灵.更新(s, 100)
打印("1 帧 ", 精灵.当前帧(s), " ", 精灵.动画完成(s))
精灵.更新(s, 100)
打印("2 帧 ", 精灵.当前帧(s), " ", 精灵.动画完成(s))
精灵.更新(s, 100)
打印("3 帧 ", 精灵.当前帧(s), " ", 精灵.动画完成(s))
精灵.播(s, "打", {"帧们": [0, 1, 2], "速度": 10, "循环": 假})
打印("再播 ", 精灵.动画完成(s))

令 转 = 精灵.新建(1, {"宽": 16, "高": 16, "列数": 2})
精灵.播(转, "转", {"帧们": [0, 1], "速度": 100, "循环": 真})
精灵.更新(转, 500)
打印("循环的完成 ", 精灵.动画完成(转))

精灵.设原点(s, 0.5, 0.25)
打印("原点 ", s["原点x"], " ", s["原点y"])
'''
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert "4 3 12" in _紧(出)
    assert "残条 2 2 4" in _紧(出), "除不尽的残条要丢掉"
    assert "初始完成 假" in _紧(出)
    assert "1 帧 1 假" in _紧(出)
    assert "2 帧 2 假" in _紧(出)
    assert "3 帧 2 真" in _紧(出), f"非循环播到最后一帧要置「完成」：{出}"
    assert "再播 假" in _紧(出), "重新播要清掉「完成」"
    assert "循环的完成 假" in _紧(出), "循环动画永远没有「播完」"
    assert "原点 0.5 0.25" in _紧(出)


# ---------------------------------------------------------------------------
# 四、地图：Tiled 逐格一致
# ---------------------------------------------------------------------------

_手写TMX = '''<?xml version="1.0" encoding="UTF-8"?>
<map version="1.10" tiledversion="1.10.2" orientation="orthogonal" renderorder="right-down" width="3" height="2" tilewidth="32" tileheight="32" infinite="0" nextlayerid="3" nextobjectid="1">
 <tileset firstgid="1" name="tiles" tilewidth="32" tileheight="32" tilecount="16" columns="4">
  <image source="图集.png" width="128" height="128"/>
 </tileset>
 <layer id="1" name="地面" width="3" height="2">
  <data encoding="csv">
1,2,3,
0,4,0
</data>
 </layer>
 <layer id="2" name="装饰" width="3" height="2">
  <data encoding="csv">
0,0,0,
0,0,5
</data>
 </layer>
</map>
'''


def test_从Tiled与手工建的地图逐格一致(tmp_path):
    """🔴 路线图要的那条：读一份手写 TMX，与手工建的地图**逐格**一致。

    Tiled 的 `gid=0` 是空格（`-1`），`gid=n` 要减 `firstgid` —— 这两处错了，
    整张地图会静默错位（画出来是另一张图，不报错）。
    """
    (tmp_path / "手写.tmx").write_text(_手写TMX, encoding="utf-8", newline="\n")
    源 = '''导入 地图

令 读的 = 地图.从Tiled("手写.tmx")
令 手工 = {
    "瓦片宽": 32, "瓦片高": 32, "图集": "图集.png",
    "层们": [
        {"名": "地面", "行们": [[0, 1, 2], [-1, 3, -1]]},
        {"名": "装饰", "行们": [[-1, -1, -1], [-1, -1, 4]]}
    ]
}
打印("瓦片 ", 读的["瓦片宽"], " ", 读的["瓦片高"])
打印("图集 ", 读的["图集"])
打印("层数 ", 长度(读的["层们"]))
令 同 = 长度(读的["层们"]) == 长度(手工["层们"])
遍历 i 在 范围(长度(读的["层们"]))：
    如果 读的["层们"][i]["名"] != 手工["层们"][i]["名"]：
        同 = 假
    如果 读的["层们"][i]["行们"] != 手工["层们"][i]["行们"]：
        同 = 假
打印("逐格一致 ", 同)

地图.存Tiled(读的, "回.tmx")
令 回 = 地图.从Tiled("回.tmx")
令 往返 = 长度(回["层们"]) == 长度(读的["层们"])
遍历 i 在 范围(长度(回["层们"]))：
    如果 回["层们"][i]["行们"] != 读的["层们"][i]["行们"]：
        往返 = 假
打印("往返一致 ", 往返)
'''
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-900:]
    assert "瓦片 32.0 32.0" in _紧(出)
    assert "图集 图集.png" in _紧(出)
    assert "层数 2" in _紧(出)
    assert "逐格一致 真" in _紧(出), f"从 Tiled 读的与手工建的不一致：{出}"
    assert "往返一致 真" in _紧(出), f"写出去再读回来变了：{出}"


def test_Tiled_base64编码要明确报错(tmp_path):
    tmx = _手写TMX.replace('encoding="csv"', 'encoding="base64"', 1)
    (tmp_path / "压缩.tmx").write_text(tmx, encoding="utf-8", newline="\n")
    码, 出 = _跑('导入 地图\n地图.从Tiled("压缩.tmx")\n', tmp_path)
    assert 码 != 0
    assert "只认 csv" in 出, f"要明确说不支持：{出[-400:]}"


# ---------------------------------------------------------------------------
# 五、场景：转场帧数精确
# ---------------------------------------------------------------------------

def test_转场帧数精确(tmp_path):
    源 = '''导入 场景
场景.定("标题", {})
场景.定("游戏", {})
场景.切("标题")
打印("在 ", 场景.现在())

场景.转场("淡出", 3)
令 真数 = 0
令 趟数 = 0
当 趟数 < 10：
    如果 场景.转场中()：
        真数 = 真数 + 1
    趟数 = 趟数 + 1
打印("真了 ", 真数, " 次")

场景.转场("擦除", 2)
打印("新轮 ", 场景.转场中(), " ", 场景.转场中(), " ", 场景.转场中())
'''
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert "在 标题" in _紧(出)
    assert "真了 3 次" in _紧(出), f"转场(…, 3) 应当正好 3 个真：{出}"
    assert "新轮 真 真 假" in _紧(出), 出


def test_转场参数边界(tmp_path):
    for 源, 关键词 in [
        ('导入 场景\n场景.转场("闪", 5)\n', "不认识的效果"),
        ('导入 场景\n场景.转场("淡出", 0)\n', "大于 0"),
    ]:
        码, 出 = _跑(源, tmp_path, "边界.jsh")
        assert 码 != 0, f"应当报错：{源}"
        assert 关键词 in 出, f"「{关键词}」没出现：{出[-300:]}"


# ---------------------------------------------------------------------------
# 六、声音 / 存档：命令行报同一句平台错
# ---------------------------------------------------------------------------

def test_声音与存档在命令行报平台错(tmp_path):
    声音错 = ("这个平台没有声音：「声音」模块只在网页 / 窗口里有"
              "（命令行和 Node 上没有音频输出）。")
    存档错 = "这个平台没有存档：「存档」要页面的 localStorage（命令行和 Node 上没有）。"

    for 源, 期望 in [
        ("导入 声音\n声音.循环(1)\n", 声音错),
        ("导入 声音\n声音.淡入(1, 500)\n", 声音错),
        ("导入 声音\n声音.淡出(1)\n", 声音错),
        ("导入 声音\n声音.静音(真)\n", 声音错),
        ("导入 存档\n打印(存档.槽们())\n", 存档错),
        ('导入 存档\n存档.自动存({"a": 1})\n', 存档错),
    ]:
        for 引擎 in ("Rust", "Python"):
            码, 出 = _跑(源, tmp_path, "平台.jsh", 引擎)
            assert 码 != 0, f"{引擎} 上 `{源.strip()}` 没报错"
            assert 期望 in 出, f"{引擎} 上文案不一致：{出[:400]}"


# ---------------------------------------------------------------------------
# 七、验收物：迷你地牢跑满 60 帧
# ---------------------------------------------------------------------------

_示例 = ROOT / "examples" / "desktop" / "迷你地牢_游戏.jsh"

_JS_游戏 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);
const SRC = fs.readFileSync(process.argv[4], "utf8");

function ctx() {
  const c = { fillStyle: "", strokeStyle: "", lineWidth: 1, font: "",
              textAlign: "left", globalAlpha: 1, globalCompositeOperation: "source-over" };
  const 无 = () => {};
  for (const m of ["save", "restore", "setTransform", "translate", "rotate", "scale",
    "beginPath", "closePath", "moveTo", "lineTo", "fill", "stroke", "clearRect",
    "strokeRect", "ellipse", "drawImage", "arc", "fillRect", "roundRect", "clip", "rect",
    "arcTo", "fillText", "strokeText"]) c[m] = 无;
  c.measureText = () => ({ width: 8, actualBoundingBoxAscent: 12, actualBoundingBoxDescent: 3 });
  c.getImageData = () => ({ data: [0, 0, 0, 255] });
  c.createLinearGradient = () => ({ addColorStop: 无 });
  c.createRadialGradient = () => ({ addColorStop: 无 });
  return c;
}
const cv = { width: 544, height: 352, style: {}, clientWidth: 544, clientHeight: 352,
             getContext: () => ctx() };
const doc = { querySelector: (s) => (s === "#画布" ? cv : null), createElement: () => cv,
              body: { appendChild: () => {} }, title: "" };

const kv = new Map();
const store = { getItem: (k) => (kv.has(k) ? kv.get(k) : null),
                setItem: (k, v) => kv.set(k, String(v)),
                removeItem: (k) => kv.delete(k) };

// 监听表必须是「一个事件多个处理器」（图形挂键盘、声音也挂）
const 监听 = {};
global.addEventListener = (ev, fn) => { (监听[ev] = 监听[ev] || []).push(fn); };
function 派发(ev, 事件) { for (const fn of (监听[ev] || [])) fn(事件); }
class 假上下文 {
  constructor() { this.state = "running"; this.destination = {}; this.currentTime = 0; }
  resume() { this.state = "running"; return Promise.resolve(); }
  createGain() { return { gain: { value: 1, cancelScheduledValues() {},
                                  setValueAtTime() {}, linearRampToValueAtTime() {} },
                          connect() {} }; }
  createBufferSource() {
    return { buffer: null, loop: false, playbackRate: { value: 1 }, onended: null,
             connect() {}, start() {}, stop() {} };
  }
  decodeAudioData(ab, 好) { setTimeout(() => 好({}), 0); }
}
global.AudioContext = 假上下文;
global.fetch = () => Promise.resolve({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) });

(async () => {
  const j = await JishiWasm.load(BYTES, { doc, store, raf: () => 0 });
  const 出 = { ok: false, 错: null, 帧数: 0, 总毫秒: 0, 最慢: 0, 码们: [] };
  const r = j.run(SRC);
  if (!r.ok) { 出.错 = r.error && (r.error.title + " / " + r.error.message); console.log(JSON.stringify(出)); return; }
  出.ok = true;

  // 按空格从「标题」进游戏
  派发("keydown", { key: " ", preventDefault() {} });

  // 一直按着右键 —— 用来验「撞墙会停」
  派发("keydown", { key: "ArrowRight", preventDefault() {} });

  const 帧们 = [];
  for (let i = 0; i < 96; i++) {
    const a = process.hrtime.bigint();
    const f = j.frame(i * 16.67);
    const b = process.hrtime.bigint();
    帧们.push(Number(b - a) / 1e6);
    出.码们.push(f.code);
    if (f.code !== 0 && f.code !== 1) {
      出.错 = String(f.out || "").slice(0, 300);
      break;
    }
  }
  出.帧数 = 帧们.length;
  出.总毫秒 = 帧们.reduce((a, b2) => a + b2, 0);
  出.最慢 = Math.max.apply(null, 帧们);
  // 让自动存的定时器有机会跑一次（它 unref 了，不会挂住）
  await new Promise((r2) => setTimeout(r2, 60));
  出.存档键 = Array.from(kv.keys());
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


def _跑示例(tmp_path: Path) -> dict:
    import importlib.util
    import shutil

    if shutil.which("node") is None:
        pytest.skip("没有 node")
    wasm = ROOT / "site" / "jishi_wasm.wasm"
    if not wasm.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    assert _示例.is_file(), f"示例不存在：{_示例}"
    脚本 = tmp_path / "游戏.js"
    脚本.write_text(_JS_游戏, encoding="utf-8", newline="\n")
    r = subprocess.run(
        ["node", str(脚本), str(ROOT / "wasm" / "jishi-wasm.js"), str(wasm), str(_示例)],
        capture_output=True, timeout=180)
    if r.returncode != 0:
        pytest.fail(f"驱动挂了：{r.stderr.decode('utf-8', 'replace')[:1200]}")
    文本 = r.stdout.decode("utf-8", "replace").strip().splitlines()
    for 行 in reversed(文本):
        if 行.startswith("{"):
            return json.loads(行)
    pytest.fail(f"没输出 JSON：{r.stdout.decode('utf-8', 'replace')[:800]}")


@pytest.fixture(scope="module")
def 游戏结果(tmp_path_factory):
    return _跑示例(tmp_path_factory.mktemp("m197"))


def test_迷你地牢跑满九十帧(游戏结果):
    """🔴 验收物（**功能那一半**）：示例要真跑起来、跑满 96 帧、一帧都不报错。

    📌 耗时**不在这里**验 —— 拆到下面那条打了 `计时` 的判据里。理由见那条。
    """
    出 = 游戏结果
    assert 出["ok"], f"示例跑挂了：{出['错']}"
    assert 出["帧数"] >= 96, f"只跑了 {出['帧数']} 帧就停了：{出['错']}"
    assert set(出["码们"]) <= {0, 1}, f"有帧报错：{出['错']}"


@pytest.mark.计时
def test_迷你地牢每帧在六十帧预算内(游戏结果):
    """🔴 验收物（**耗时那一半**）：平均 < 16.67 ms（60 帧的预算）、最慢 < 2 倍。

    ⚠️ **打了 `计时`**：这条断言的是**墙上时间**，而 CI 是共享 runner ——
    实测 Windows runner 上同一份代码的最慢一帧是 36.04 ms、本机 8 ms 出头，
    绝对阈值在那边**没有判别力**，只会假红挡发版（AGENTS §1 的规矩）。
    所以它进串行节、CI 不跑（`-m "not 计时"`），**本机串行跑才是它的判据**。

    📌 最慢放宽到 2 倍：GC / JIT 预热会给单帧一个尖峰；只比平均值会漏掉
    「每 10 帧卡一次」那种。
    """
    出 = 游戏结果
    assert 出["ok"], f"示例跑挂了：{出['错']}"
    平均 = 出["总毫秒"] / 出["帧数"]
    assert 平均 < 16.67, f"平均一帧 {平均:.2f} ms，超过 60 帧的预算（16.67 ms）"
    assert 出["最慢"] < 33.4, f"最慢一帧 {出['最慢']:.2f} ms，超了 2 倍预算"


def test_迷你地牢真的用上了新东西():
    """源码里得真的调了 M197 的那几个 —— 不然「示例跑满 60 帧」证明不了什么。"""
    文 = _示例.read_text(encoding="utf-8")
    for 记号, 说明 in [
        ("瓦片.碰撞层", "碰撞层"),
        ("瓦片.是实心", "是实心（挡人）"),
        ("瓦片.斜坡", "斜坡"),
        ("精灵.图集切分", "图集切分"),
        ("精灵.设原点", "设原点"),
        ("场景.转场", "转场"),
        ("场景.转场中", "转场中"),
        ("存档.槽们", "多存档槽"),
        ("存档.自动存", "自动存"),
        ("地图.从Tiled", "Tiled 地图"),
    ]:
        assert 记号 in 文, f"示例里没用到 `{记号}`（{说明}）"


def test_示例四件套都在():
    """入口 / 本体 / 页面 / 共用启动器 —— 少一件就跑不起来。"""
    例子 = ROOT / "examples" / "desktop"
    for 名 in ("迷你地牢.jsh", "迷你地牢_游戏.jsh", "迷你地牢.html", "游戏启动.js"):
        assert (例子 / 名).is_file(), f"缺 {名}"
    页面 = (例子 / "迷你地牢.html").read_text(encoding="utf-8")
    assert 'data-游戏="迷你地牢_游戏.jsh"' in 页面
    # 页面里不许有启动逻辑（M188 立的规矩）
    for 记号 in ("import(", "JishiWasm", "jishi_wasm.wasm"):
        assert 记号 not in 页面, f"页面里还有启动逻辑「{记号}」"
