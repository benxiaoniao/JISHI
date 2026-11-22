# -*- coding: utf-8 -*-
"""M188 · 三个能玩的示例（本文件先覆盖**打砖块**）。

## 为什么判据要「跑起来」而不是「语法没错」

游戏这种东西，最容易出的错是**看不出错**：球穿墙、分数不动、掉球不判负、
存档没写进去 —— 页面照样正常显示，只是「不好玩」。所以这里用 **Node + 假 canvas
+ 假 Web Audio + 假 localStorage** 真跑帧循环，断言的都是**可核对的事实**：

1. `图形.运行` 之后帧循环**真的在跑**（`framesActive()`），第一帧就画了 40 块砖；
2. 跑几百帧之后**分数会变**（球真的撞上了砖，且计分真的走了）；
3. 按住 → 把挡板挪开 ⇒ 球掉下去 ⇒ **判负 + 最高分写进 localStorage**；
4. 碰砖时**声音真的 `start()` 了一次**（不是「调了播放没崩」）。

📌 判据只喂**固定的时间戳**（16 ms 一帧），不看墙钟 —— 同一个源永远同一个结果。

## 另两条：文件齐不齐、有没有把源码抄成两份

`打砖块.jsh` 是**唯一**源码：窗口页 `fetch` 它，官网试玩页也用它。
如果哪天有人把源码内联进 HTML，两份就会漂 —— 那是最难查的一类问题，
所以这里直接查「HTML 里没有游戏源码」。

⚠️ 真窗口那条要桌面（`JISHI_NO_GUI=1` 跳过）；CI 上是**空白**，见 CHANGELOG。

📌 **真浏览器里另有一份人工证据**（不进 CI）：`play.html` 用 Edge 无头截过图 ——
砖块 8×5、挡板、球、分数栏、备案页脚都在，页面没有报错框。这一步捞到两个
「只有真浏览器才照得出」的错：`import("jishi-wasm.js")` 少 `./` 被当成裸模块名；
官网 `site/jishi_wasm.wasm` 停在 10 月的老版本（shim 要 ABI 4、拿到 1）。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

WASM = ROOT / "site" / "jishi_wasm.wasm"
SHIM = ROOT / "wasm" / "jishi-wasm.js"
例子 = ROOT / "examples" / "desktop"
#: 三个示例共用的启动器（页面里只有一行 `<script src="游戏启动.js">`）
启动器 = 例子 / "游戏启动.js"
音效 = 例子 / "碰.wav"

#: (名字, 入口, 本体, 页面, 页面里**不许**出现的源码记号)
#: 记号用来抓「有人把游戏源码抄进 HTML」——每一份挑 3 个只有本体会有的片段。
游戏们 = [
    ("打砖块", 例子 / "打砖块.jsh", 例子 / "打砖块_游戏.jsh", 例子 / "打砖块.html",
     ("图形.运行", "函数 每帧", "砖色们")),
    ("贪吃蛇", 例子 / "贪吃蛇.jsh", 例子 / "贪吃蛇_游戏.jsh", 例子 / "贪吃蛇.html",
     ("图形.运行", "函数 每帧", "找空位")),
    ("平台跳跃", 例子 / "平台跳跃.jsh", 例子 / "平台跳跃_游戏.jsh", 例子 / "平台跳跃.html",
     ("图形.运行", "函数 每帧", "实心")),
]

#: 打砖块的三件（下面那一大段帧循环判据只针对它）
砖_入口, 砖_本体, 砖_页面, _ = 游戏们[0][1:]


# ---------------------------------------------------------------------------
# 一、文件齐不齐（最便宜的一条，先红先知道）
# ---------------------------------------------------------------------------

def test_每个示例的四件套都在():
    """入口 / 本体 / 页面 + 共用的启动器与音效 —— 少一件的表现都是
    「窗口开了但什么都没发生」。"""
    assert 启动器.is_file(), "缺 游戏启动.js（三个页面共用它）"
    assert 音效.is_file(), "缺 碰.wav"
    for 名, 入, 本, 页, _ in 游戏们:
        for f in (入, 本, 页):
            assert f.is_file(), f"「{名}」缺 {f.name}"


@pytest.mark.parametrize("名,入口,本体,页面,记号们", 游戏们)
def test_入口只开窗_游戏本体单独一份(名, 入口, 本体, 页面, 记号们):
    """桌面入口只管开窗（`窗口.打开` + `窗口.等`），游戏逻辑在另一个文件里。

    为什么必须分开：官网试玩页跑的是**同一份**本体，它已经在页面里了，
    不能再 `窗口.打开`（没有第二个窗口可开）。
    """
    文 = 入口.read_text(encoding="utf-8")
    assert "窗口.打开(" in 文 and "窗口.等(" in 文, f"{名} 的入口没开窗 / 没等窗口"
    assert 页面.name in 文, f"{名} 的入口没指向页面"
    assert "图形.运行" not in 文, f"{名} 的入口里不该有游戏逻辑（那是本体的活）"


@pytest.mark.parametrize("名,入口,本体,页面,记号们", 游戏们)
def test_页面没有把游戏源码抄一份(名, 入口, 本体, 页面, 记号们):
    """🔴 **源码只有一份**：页面只用 `data-游戏` 指过去，不许内联。

    抄进 HTML 就会漂（改了一边、另一边还是老的，而且在页面上完全看不出来）。
    """
    文 = 页面.read_text(encoding="utf-8")
    assert 本体.name in 文, f"{名} 的页面没指向源码"
    assert "游戏启动.js" in 文, f"{名} 的页面没引用共用启动器"
    for 记号 in 记号们:
        assert 记号 not in 文, f"{名} 的页面里出现了游戏源码片段「{记号}」—— 抄了一份"


def test_启动器两条路径都有_窗口与官网():
    """窗口里素材挂在 `/__jishi/`，官网上与页面同级 —— 两条都得在，
    否则一边是白页面（M173 踩过：wasm 写成相对路径，窗口里 404）。

    ⚠️ 同级那条**必须带 `./`**：不带 `./` 的模块名会被当成裸模块名，
    浏览器报 `Failed to resolve module specifier`（M188 真浏览器截图才发现，
    Node 判据照不出来 —— 判据里 import 的是绝对路径）。

    🔴 这段逻辑**只有一份**（`游戏启动.js`）：三个页面各抄一遍的话，
    这种错就得改三处，而且新加的那个一定忘。
    """
    文 = 启动器.read_text(encoding="utf-8")
    assert "/__jishi/jishi-wasm.js" in 文 and "/__jishi/jishi_wasm.wasm" in 文
    assert '"./jishi-wasm.js"' in 文 and '"./jishi_wasm.wasm"' in 文
    assert 'import("jishi-wasm.js")' not in 文, "少 `./` 会被当成裸模块名"


def test_页面不再各自写启动逻辑():
    """页面里不许出现 `import(` / `JishiWasm.load` —— 那些都在启动器里。
    页一多，抄一份就等于给自己埋一个「只有这个页面坏的」bug。"""
    for 名, _, _, 页面, _ in 游戏们:
        文 = 页面.read_text(encoding="utf-8")
        for 记号 in ("import(", "JishiWasm", "jishi_wasm.wasm"):
            assert 记号 not in 文, f"{名} 的页面里还有启动逻辑「{记号}」（应当在 游戏启动.js）"


# ---------------------------------------------------------------------------
# 二、在假画布上真玩一遍
# ---------------------------------------------------------------------------

_JS = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);
const SRC = fs.readFileSync(process.argv[4], "utf8");

// ---- 假 canvas：只记账 ----
const 记账 = { 清屏: 0, 矩形: 0, 圆: 0, 首帧矩形: 0, 文本: [] };
function ctx() {
  const c = { fillStyle: "", strokeStyle: "", lineWidth: 1, font: "",
              textAlign: "left", globalAlpha: 1 };
  const 无 = () => {};
  for (const m of ["save", "restore", "setTransform", "translate", "rotate", "scale",
    "beginPath", "closePath", "moveTo", "lineTo", "fill", "stroke", "clearRect",
    "strokeRect", "ellipse", "drawImage"]) c[m] = 无;
  c.measureText = () => ({ width: 8, actualBoundingBoxAscent: 12, actualBoundingBoxDescent: 3 });
  c.getImageData = () => ({ data: [0, 0, 0, 255] });
  c.fillRect = (x, y, w, h) => {
    if (x === 0 && y === 0 && w === 640 && h === 480) 记账.清屏 += 1;
    else 记账.矩形 += 1;
  };
  c.roundRect = () => { 记账.矩形 += 1; };
  c.arc = () => { 记账.圆 += 1; };
  c.fillText = (t) => 记账.文本.push(String(t));
  return c;
}
const cv = { width: 640, height: 480, style: {}, clientWidth: 640, clientHeight: 480,
             getContext: () => ctx() };
const doc = { querySelector: (s) => (s === "#画布" ? cv : null), createElement: () => cv,
              body: { appendChild: () => {} }, title: "" };

// ---- 假 localStorage（存档）----
const kv = new Map();
const store = { getItem: (k) => (kv.has(k) ? kv.get(k) : null),
                setItem: (k, v) => kv.set(k, String(v)),
                removeItem: (k) => kv.delete(k) };

// ---- 假 Web Audio（声音）----
// ⚠️ 监听表必须是**一个事件多个处理器**：`图形` 挂键盘，`声音` 也挂键盘（解锁用）。
//    只留最后一个的话，空间键被「解锁」那个处理器吃掉 —— 表现是「按空格没反应」，
//    而真浏览器里两个都在（M188 在这里卡过一次，别再用「一个事件一个函数」的假环境）。
let 放了 = 0;
const 监听 = {};
global.addEventListener = (ev, fn) => { (监听[ev] = 监听[ev] || []).push(fn); };
function 派发(ev, 事件) {
  for (const fn of (监听[ev] || [])) fn(事件);
}
class 假上下文 {
  constructor() { this.state = "running"; this.destination = {}; }
  resume() { this.state = "running"; return Promise.resolve(); }
  createGain() { return { gain: { value: 1 }, connect() {} }; }
  createBufferSource() {
    const 源 = { buffer: null, loop: false, playbackRate: { value: 1 }, onended: null,
      connect() {}, start() { 放了 += 1; },
      stop() { const f = 源.onended; 源.onended = null; if (f) f(); } };
    return 源;
  }
  decodeAudioData(ab, 好) { setTimeout(() => 好({ 解好了: 1 }), 0); }
}
global.AudioContext = 假上下文;
global.fetch = () => Promise.resolve({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) });

(async () => {
  const j = await JishiWasm.load(BYTES, { doc, store, raf: () => 0 });
  const r = j.run(SRC);
  if (!r.ok) { console.error(JSON.stringify(r.error)); process.exit(2); }
  const 出 = { ok: r.ok, 帧中: j.framesActive(), 错: null };
  await new Promise((r) => setTimeout(r, 30));

  // 第 1 帧：砖块 / 挡板 / 球 / 分数栏都得画出来
  j.frame(16);
  出.清屏 = 记账.清屏;
  出.首帧矩形 = 记账.矩形;
  出.第一帧文本 = 记账.文本.slice(-3);

  // 之后按住 →（把挡板开到右边），球就会掉下去 ⇒ 判负 + 存最高分
  let 帧 = 1;
  while (帧 < 1500) {
    帧 += 1;
    派发("keydown", { key: "ArrowRight", preventDefault() {} });
    j.frame(帧 * 16);
    if (记账.文本.some((t) => t.startsWith("结束"))) break;
  }
  出.帧数 = 帧;
  出.清屏末 = 记账.清屏;
  出.矩形末 = 记账.矩形;
  出.圆末 = 记账.圆;
  出.分数们 = 记账.文本.filter((t) => t.startsWith("分数 "));
  出.结束 = 记账.文本.filter((t) => t.startsWith("结束")).slice(-2);
  出.放了 = 放了;
  出.存档 = Object.fromEntries([...kv.entries()]);
  // 结束之后按空格应当能重开（读不到「结束」了）
  派发("keyup", { key: "ArrowRight" });
  记账.文本.length = 0;
  派发("keydown", { key: " ", preventDefault() {} });
  const r2 = j.frame(帧 * 16 + 16);
  出.重开后 = 记账.文本.slice(-4);
  出.帧错 = j._帧错 == null ? null : String(j._帧错);
  if (r2.code !== 0) 出.码 = r2.code;
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


@pytest.fixture(scope="module")
def 玩过(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    d = tmp_path_factory.mktemp("m188")
    脚本 = d / "play.js"
    脚本.write_text(_JS, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM), str(砖_本体)],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-1200:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_帧循环真在跑_第一帧就画了砖(玩过):
    """`图形.运行` 之后是**常驻 VM**（D110）—— 帧循环不在跑的话后面全是空话。"""
    assert 玩过["ok"], 玩过.get("错")
    assert 玩过["帧中"], "`图形.运行` 之后帧循环没在跑"
    # 8 × 5 = 40 块砖 + 挡板 = 41 个矩形
    assert 玩过["首帧矩形"] >= 41, f"第一帧只画了 {玩过['首帧矩形']} 个矩形"
    assert 玩过["清屏"] >= 2, "清屏没被调用（准备 1 次 + 每帧 1 次）"
    assert any("分数" in t for t in 玩过["第一帧文本"]), 玩过["第一帧文本"]


def test_跑起来分数会变(玩过):
    """球真的撞上砖、计分真的走了 —— 不是「画了 40 个方块」就算过。"""
    分 = [int(t.split(" ")[1]) for t in 玩过["分数们"] if t.split(" ")[1].isdigit()]
    assert 分, f"没画出过分数：{玩过['分数们'][:3]}"
    assert max(分) > 0, f"跑完了分数还是 0（球没撞到砖？）：{max(分)}"
    assert max(分) % 10 == 0, f"每块砖 10 分，分数应当是 10 的倍数：{max(分)}"


def test_球在画_并且用圆画的(玩过):
    assert 玩过["圆末"] >= 玩过["帧数"] - 2, \
        f"每帧都该画一个球：{玩过['圆末']} 个圆 / {玩过['帧数']} 帧"


def test_掉球判负_最高分写进存档(玩过):
    """🔴 这条是**闭环**：输入（按住 →）→ 物理 → 判负 → 存档。

    只验「画了东西」的话，一个永远不结束的游戏也能全绿。
    """
    assert 玩过["结束"], f"按住 → 把挡板挪开之后球该掉下去：{玩过['帧数']} 帧还没结束"
    存档 = 玩过["存档"]
    键 = "基石存档/打砖块最高分"
    assert 键 in 存档, f"没存最高分：{list(存档)}"
    包 = json.loads(存档[键])
    assert 包["版本"] == 1 and 包["值"] >= 10, f"存进去的值不对：{包}"
    assert "打砖块最高分" in 存档.get("基石存档/索引", ""), "存档索引没记上（`清()` 会漏）"


def test_碰砖时声音真的响过(玩过):
    """`start()` 真被调用过 —— 「调了 `播放` 没崩」不算验过。"""
    assert 玩过["放了"] > 0, "一次都没真放过"


def test_结束之后按空格能重开(玩过):
    重开 = 玩过["重开后"]
    assert any("分数 0" in t for t in 重开), f"按空格之后没重开：{重开}"
    assert not any(t.startswith("结束") for t in 重开), f"还停在结束页：{重开}"


# ---------------------------------------------------------------------------
# 三、贪吃蛇：纯网格逻辑（没有浮点物理，出事一定不赖碰撞）
# ---------------------------------------------------------------------------

_JS_蛇 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);
const SRC = fs.readFileSync(process.argv[4], "utf8");

// ---- 假 canvas ----
const 记账 = { 清屏: 0, 矩形: 0, 线: 0, 圆: 0, 每帧矩形: [], 文本: [] };
function ctx() {
  const c = { fillStyle: "", strokeStyle: "", lineWidth: 1, font: "",
              textAlign: "left", globalAlpha: 1 };
  const 无 = () => {};
  for (const m of ["save", "restore", "setTransform", "translate", "rotate", "scale",
    "beginPath", "closePath", "moveTo", "lineTo", "fill", "stroke", "clearRect",
    "strokeRect", "ellipse", "drawImage", "arc", "fillText", "measureText", "getImageData"]) c[m] = 无;
  c.measureText = () => ({ width: 8 });
  c.getImageData = () => ({ data: [0, 0, 0, 255] });
  c.fillRect = (x, y, w, h) => {
    if (x === 0 && y === 0 && w === 480 && h === 480) 记账.清屏 += 1;
    else 记账.矩形 += 1;
  };
  c.roundRect = () => { 记账.矩形 += 1; };
  c.lineTo = () => { 记账.线 += 1; };       // 网格线走 moveTo + lineTo
  c.arc = () => { 记账.圆 += 1; };
  c.fillText = (t) => 记账.文本.push(String(t));
  return c;
}
const cv = { width: 480, height: 480, style: {}, clientWidth: 480, clientHeight: 480,
             getContext: () => ctx() };
const doc = { querySelector: (s) => (s === "#画布" ? cv : null), createElement: () => cv,
              body: { appendChild: () => {} }, title: "" };

const kv = new Map();
const store = { getItem: (k) => (kv.has(k) ? kv.get(k) : null),
                setItem: (k, v) => kv.set(k, String(v)),
                removeItem: (k) => kv.delete(k) };

let 放了 = 0;
const 监听 = {};
global.addEventListener = (ev, fn) => { (监听[ev] = 监听[ev] || []).push(fn); };
function 派发(ev, 事件) { for (const fn of (监听[ev] || [])) fn(事件); }
class 假上下文 {
  constructor() { this.state = "running"; this.destination = {}; }
  resume() { return Promise.resolve(); }
  createGain() { return { gain: { value: 1 }, connect() {} }; }
  createBufferSource() {
    const 源 = { buffer: null, loop: false, playbackRate: { value: 1 }, onended: null,
      connect() {}, start() { 放了 += 1; },
      stop() { const f = 源.onended; 源.onended = null; if (f) f(); } };
    return 源;
  }
  decodeAudioData(ab, 好) { setTimeout(() => 好({ ok: 1 }), 0); }
}
global.AudioContext = 假上下文;
global.fetch = () => Promise.resolve({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) });

(async () => {
  const j = await JishiWasm.load(BYTES, { doc, store, raf: () => 0 });
  const r = j.run(SRC);
  if (!r.ok) { console.error(JSON.stringify(r.error)); process.exit(2); }
  const 出 = { ok: r.ok, 帧中: j.framesActive() };
  await new Promise((r) => setTimeout(r, 30));

  // 第 1 帧：3 节蛇 + 食物（圆）+ 网格线
  j.frame(16);
  出.首帧矩形 = 记账.矩形;
  出.首帧圆 = 记账.圆;
  出.首帧线 = 记账.线;
  出.第一帧文本 = 记账.文本.slice(-3);

  // 不按任何键：蛇本来就朝右 —— 会先吃到正前方 4 格那颗，然后一路撞到右墙
  let 帧 = 1;
  while (帧 < 600) {
    帧 += 1;
    j.frame(帧 * 16);
    记账.每帧矩形.push(记账.矩形);
    if (记账.文本.some((t) => t.startsWith("撞上了"))) break;
  }
  出.帧数 = 帧;
  出.最长蛇 = Math.max(...记账.每帧矩形);   // 一帧里画了几个矩形 = 蛇几节
  出.矩形总数 = 记账.矩形;
  出.分数们 = 记账.文本.filter((t) => t.startsWith("分数 "));
  出.结束 = 记账.文本.filter((t) => t.startsWith("撞上了")).slice(-1);
  出.放了 = 放了;
  出.存档 = Object.fromEntries([...kv.entries()]);
  // 空格重开
  记账.文本.length = 0;
  派发("keydown", { key: " ", preventDefault() {} });
  j.frame(帧 * 16 + 16);
  出.重开后 = 记账.文本.slice(-4);
  出.帧错 = j._帧错 == null ? null : String(j._帧错);
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


@pytest.fixture(scope="module")
def 蛇跑过(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    d = tmp_path_factory.mktemp("m188s")
    脚本 = d / "snake.js"
    脚本.write_text(_JS_蛇, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM), str(游戏们[1][2])],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-1200:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_贪吃蛇帧循环在跑_第一帧画了3节蛇(蛇跑过):
    assert 蛇跑过["ok"]
    assert 蛇跑过["帧中"], "`图形.运行` 之后帧循环没在跑"
    assert 蛇跑过["首帧矩形"] == 3, f"开局应当是 3 节蛇，实际 {蛇跑过['首帧矩形']} 个矩形"
    assert 蛇跑过["首帧圆"] == 1, "食物没画出来"
    assert 蛇跑过["首帧线"] >= 19, f"网格线没画够：{蛇跑过['首帧线']}"
    assert any("分数" in t for t in 蛇跑过["第一帧文本"]), 蛇跑过["第一帧文本"]


def test_贪吃蛇吃到会长一节(蛇跑过):
    """吃到那颗之后蛇从 3 节变 4 节 —— 「分数变了」不等于「蛇长了」。"""
    assert 蛇跑过["最长蛇"] >= 4, f"蛇没变长（最多 {蛇跑过['最长蛇']} 节）"
    分 = [int(t.split(" ")[1]) for t in 蛇跑过["分数们"] if t.split(" ")[1].isdigit()]
    assert max(分) >= 10, f"吃到了却没加分：{max(分)}"


def test_贪吃蛇撞墙判负_最高分进存档(蛇跑过):
    """🔴 闭环：一路朝右 → 撞墙 → 判负 → 存档。食物位置固定在前方，
    所以这条是**确定性的**（不靠随机数运气）。"""
    assert 蛇跑过["结束"], f"没撞到墙：跑了 {蛇跑过['帧数']} 帧还在玩"
    存档 = 蛇跑过["存档"]
    键 = "基石存档/贪吃蛇最高分"
    assert 键 in 存档, f"没存最高分：{list(存档)}"
    assert json.loads(存档[键])["值"] == 10, f"存进去的值不对：{存档[键]}"


def test_贪吃蛇按空格重开(蛇跑过):
    重开 = 蛇跑过["重开后"]
    assert any("分数 0" in t for t in 重开), f"按空格之后没重开：{重开}"
    assert not any(t.startswith("撞上了") for t in 重开), f"还停在结束页：{重开}"


# ---------------------------------------------------------------------------
# 四、wasm 里的随机数（M188 撞出来的一颗大雷，单独钉一条）
# ---------------------------------------------------------------------------

_JS_随机 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);
function ctx() {
  const c = {}; const 无 = () => {};
  for (const m of ["save", "restore", "setTransform", "translate", "rotate", "scale",
    "beginPath", "closePath", "moveTo", "lineTo", "fill", "stroke", "clearRect",
    "strokeRect", "ellipse", "drawImage", "fillRect", "roundRect", "arc", "fillText"]) c[m] = 无;
  c.measureText = () => ({ width: 8 });
  c.getImageData = () => ({ data: [0, 0, 0, 255] });
  return c;
}
const cv = { width: 200, height: 200, style: {}, clientWidth: 200, clientHeight: 200, getContext: ctx };
const doc = { querySelector: () => cv, createElement: () => cv, body: { appendChild: () => {} } };

const SRC = `导入 随机
令 计数 = {"各": {}}
令 序 = []
循环 30 次：
    令 v = 随机.随机整数(1, 6)
    序.追加(v)
    如果 v < 1 或 v > 6：
        打印("越界 ", v)
    如果 v 在 计数["各"]：
        计数["各"][v] = 计数["各"][v] + 1
    否则：
        计数["各"][v] = 1
打印("种数 ", 长度(计数["各"]))
打印("序 ", 序)
`;

(async () => {
  const 结果 = [];
  for (let i = 0; i < 2; i++) {
    const j = await JishiWasm.load(BYTES, { doc, store: null, raf: () => 0 });
    const r = j.run(SRC);
    if (!r.ok) { console.error("ERR " + JSON.stringify(r.error)); process.exit(2); }
    结果.push(r.stdout.trim());
  }
  console.log(JSON.stringify({ 结果 }));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


def test_wasm里随机数能用_不再炸引擎(tmp_path):
    """🔴 `wasm32-unknown-unknown` **没有时钟** —— `SystemTime::now()` 在那边是
    **直接 panic**。所以 `随机` 模块曾经在浏览器 / 窗口里**一调就炸**，
    而且报的是 `RuntimeError: unreachable`（看不出跟随机数有关）。

    修法：shim 加载时用 `Math.random()` 喂一颗种子（`jishi_seed` 导出）。
    这条判据同时钉三件事：不炸、不越界、两次加载的序列不一样。
    """
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    脚本 = tmp_path / "rand.js"
    脚本.write_text(_JS_随机, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=120)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, 出[-1200:]
    两次 = json.loads(出.strip().splitlines()[-1])["结果"]
    for 文 in 两次:
        assert "越界" not in 文, f"随机整数越界了：{文}"
        种数 = int(文.split("种数 ")[1].split("\n")[0])
        assert 种数 >= 3, f"30 次只出了 {种数} 种（随机数退化了？）"
    序们 = [文.split("序 ")[1].strip() for 文 in 两次]
    assert 序们[0] != 序们[1], f"两次加载的序列一模一样 —— 种子没接上：{序们[0]}"


_JS_时间 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);
function ctx() {
  const c = {}; const 无 = () => {};
  for (const m of ["save", "restore", "setTransform", "translate", "rotate", "scale",
    "beginPath", "closePath", "moveTo", "lineTo", "fill", "stroke", "clearRect",
    "strokeRect", "ellipse", "roundRect", "arc", "fillText", "fillRect", "drawImage"]) c[m] = 无;
  c.measureText = () => ({ width: 8 });
  c.getImageData = () => ({ data: [0, 0, 0, 255] });
  return c;
}
const cv = { width: 200, height: 200, style: {}, clientWidth: 200, clientHeight: 200, getContext: ctx };
const doc = { querySelector: () => cv, createElement: () => cv, body: { appendChild: () => {} } };

const SRC = `导入 时间
打印("戳 ", 时间.时间戳())
打印("今天 ", 时间.今天())
打印("现在 ", 时间.现在())
打印("偏移 ", 时间.时区偏移())
`;

(async () => {
  const j = await JishiWasm.load(BYTES, { doc, store: null, raf: () => 0 });
  const r = j.run(SRC);
  console.log(JSON.stringify({ ok: r.ok, out: r.stdout,
    err: r.ok ? null : (r.error && r.error.title) }));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


def test_wasm里时间能用_不再炸也不停在1970(tmp_path):
    """🔴 `时间` 模块在 wasm 里**也踩了同一颗雷**（和 `随机` 一个根因），
    而且它还有第二种坏法：**不报错、永远返回 1970-01-01**。

    - `wasm32-unknown-unknown` 没有时钟 ⇒ `std::time::SystemTime::now()` panic；
    - 没有本地时区 API ⇒ 那个 `cfg` 分支以前是**硬写 `(1970,1,1,…)`**。

    修法（D113 的第二刀）：宿主喂「现在」（`jishi_now`，加载时 + 每帧）与
    「时区偏移」（`jishi_tz`，加载时一次）。这条判据钉三件事：
    **不炸**、**时间戳是真的现在**（> 2020 年）、**日期不是 1970**。
    """
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    脚本 = tmp_path / "now.js"
    脚本.write_text(_JS_时间, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=120)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, 出[-800:]
    结果 = json.loads(出.strip().splitlines()[-1])
    assert 结果["ok"], f"炸了：{结果['err']}"
    文 = 结果["out"] or ""
    戳 = float(文.split("戳 ")[1].split("\n")[0].strip())
    assert 戳 > 1_600_000_000, f"时间戳不像现在：{戳}"
    今天 = 文.split("今天 ")[1].split("\n")[0].strip()
    assert not 今天.startswith("1970"), f"日期还停在 1970：{今天}"
    assert 今天[:2] in ("20", "21"), f"日期不像话：{今天}"
    偏移 = int(文.split("偏移 ")[1].split("\n")[0].replace("+", "").split(":")[0])
    assert -14 <= 偏移 <= 14, f"时区偏移不像话：{偏移}"


# ---------------------------------------------------------------------------
# 五、精灵选项的三种写法（M188 撞出来的第二颗雷，单独钉一条）
# ---------------------------------------------------------------------------

_JS_精灵 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);
let 画了 = 0;
function ctx() {
  const c = {}; const 无 = () => {};
  for (const m of ["save", "restore", "setTransform", "translate", "rotate", "scale",
    "beginPath", "closePath", "moveTo", "lineTo", "fill", "stroke", "clearRect",
    "strokeRect", "ellipse", "roundRect", "arc", "fillText"]) c[m] = 无;
  c.measureText = () => ({ width: 8 });
  c.getImageData = () => ({ data: [0, 0, 0, 255] });
  c.fillRect = 无;
  c.drawImage = () => { 画了 += 1; };
  return c;
}
const cv = { width: 320, height: 200, style: {}, clientWidth: 320, clientHeight: 200, getContext: ctx };
const doc = { querySelector: () => cv, createElement: () => cv, body: { appendChild: () => {} } };
global.Image = class { set src(v) { if (this.onload) this.onload(); } };
global.fetch = () => Promise.resolve({ ok: true, text: () => Promise.resolve("") });

const SRC = `导入 图形
导入 精灵
图形.准备({"宽": 320, "高": 200})
令 图 = 图形.读图("x.png")
令 精 = 精灵.新建(图, {"宽": 16, "高": 16, "列数": 4, "行数": 1})
精灵.画(精, 10, 20, 空)
打印("空 OK")
精灵.画(精, 10, 20, {"翻转": "横"})
打印("翻转 OK")
精灵.画(精, 10, 20, {"旋转": 30})
打印("旋转 OK")
精灵.画(精, 10, 20, {"缩放": 2})
打印("缩放 OK")
精灵.画(精, 10, 20, {"透明度": 0.5})
打印("透明度 OK")
`;

(async () => {
  const j = await JishiWasm.load(BYTES, { doc, store: null, raf: () => 0 });
  const r = j.run(SRC);
  console.log(JSON.stringify({ ok: r.ok, out: r.stdout, err: r.ok ? null : r.error.message, 画了 }));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


def test_精灵画的选项都能用_不再拼坏JSON(tmp_path):
    """🔴 `精灵.画` 的 `翻转 / 旋转 / 缩放 / 透明度` 曾经**全都用不了**。

    拼选项时键名用了 `jsonw::esc`（**自带引号**），外面又包了一层 ⇒
    得到 `{""翻转"":"横"}` ⇒ shim 一 `JSON.parse` 就报
    「Expected ':' after property name at position 3」——
    报错里一个字都没提「翻转」，只有真跑那个分支才照得出来。

    这条把四种写法和「不传选项」各跑一遍，全过才算过。
    """
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    脚本 = tmp_path / "sprite.js"
    脚本.write_text(_JS_精灵, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=120)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, 出[-800:]
    结果 = json.loads(出.strip().splitlines()[-1])
    assert 结果["ok"], f"炸了：{结果['err']}"
    for 词 in ("空", "翻转", "旋转", "缩放", "透明度"):
        assert f"{词} OK" in (结果["out"] or ""), f"「{词}」这条没走通：{结果['out']!r}"
    assert 结果["画了"] == 5, f"应当画 5 次，实际 {结果['画了']}"


# ---------------------------------------------------------------------------
# 六、平台跳跃：瓦片 + 相机 + 重力（三个里唯一同时用上三个模块的）
# ---------------------------------------------------------------------------

_JS_跳 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);
const SRC = fs.readFileSync(process.argv[4], "utf8");

// ---- 假 canvas：累计 translate，记**绝对**目标坐标 ----
const 记账 = { 清屏: 0, 子图: 0, 矩形: 0, 文本: [], 帧: [] };
let 帧内子图 = 0, 帧内末 = null;
function ctx() {
  const c = { fillStyle: "", strokeStyle: "", lineWidth: 1, font: "",
              textAlign: "left", globalAlpha: 1 };
  const 无 = () => {};
  for (const m of ["beginPath", "closePath", "moveTo", "lineTo", "fill", "stroke",
    "clearRect", "strokeRect", "ellipse", "rotate", "scale"]) c[m] = 无;
  let tx = 0, ty = 0;
  const 栈 = [];
  c.save = () => { 栈.push([tx, ty]); };
  c.restore = () => { const s = 栈.pop(); if (s) { tx = s[0]; ty = s[1]; } };
  c.translate = (x, y) => { tx += x; ty += y; };
  c.setTransform = () => { tx = 0; ty = 0; };
  c.measureText = () => ({ width: 8 });
  c.getImageData = () => ({ data: [0, 0, 0, 255] });
  c.fillRect = (x, y, w, h) => {
    if (x === 0 && y === 0 && (w === 800 || w === 640 || w === 480)) 记账.清屏 += 1;
    else 记账.矩形 += 1;
  };
  c.roundRect = () => { 记账.矩形 += 1; };
  c.arc = 无;
  c.fillText = (t) => 记账.文本.push(String(t));
  c.drawImage = (img, sx, sy, sw, sh, dx, dy, dw, dh) => {
    记账.子图 += 1; 帧内子图 += 1;
    帧内末 = [tx + dx, ty + dy];
  };
  return c;
}
const cv = { width: 800, height: 480, style: {}, clientWidth: 800, clientHeight: 480,
             getContext: () => ctx() };
const doc = { querySelector: (s) => (s === "#画布" ? cv : null), createElement: () => cv,
              body: { appendChild: () => {} }, title: "" };
// 假 `Image`：设 src 就立刻「加载完」（否则 `画子图` 会跳过绘制）
global.Image = class { set src(v) { if (this.onload) this.onload(); } };
global.fetch = () => Promise.resolve({ ok: true, text: () => Promise.resolve("") });
const kv = new Map();
const store = { getItem: (k) => (kv.has(k) ? kv.get(k) : null),
                setItem: (k, v) => kv.set(k, String(v)),
                removeItem: (k) => kv.delete(k) };

const 监听 = {};
global.addEventListener = (ev, fn) => { (监听[ev] = 监听[ev] || []).push(fn); };
function 派发(ev, 事件) { for (const fn of (监听[ev] || [])) fn(事件); }

(async () => {
  const j = await JishiWasm.load(BYTES, { doc, store, raf: () => 0 });
  const r = j.run(SRC);
  if (!r.ok) { console.error(JSON.stringify(r.error)); process.exit(2); }
  const 出 = { ok: r.ok, 帧中: j.framesActive() };
  let 帧 = 0;
  function 走一帧() {
    帧 += 1;
    帧内子图 = 0; 帧内末 = null;
    j.frame(帧 * 16);
    记账.帧.push({ 末: 帧内末, 数: 帧内子图 });
  }
  await new Promise((r) => setTimeout(r, 30));

  走一帧();
  出.首帧子图 = 记账.帧[0].数;      // 瓦片 + 主角
  出.首帧主角 = 记账.帧[0].末;

  // ① 静置：重力把人压到地面上，位置应当稳定
  for (let i = 0; i < 30; i++) 走一帧();
  出.落地y = 记账.帧[记账.帧.length - 1].末[1];

  // ② 跳一下：y 应当先变小（往上），而且最终回到地面
  派发("keydown", { key: " ", preventDefault() {} });
  走一帧();
  派发("keyup", { key: " ", preventDefault() {} });
  let 最高 = 出.落地y;
  for (let i = 0; i < 40; i++) { 走一帧(); 最高 = Math.min(最高, 记账.帧[记账.帧.length - 1].末[1]); }
  出.跳最高 = 最高;

  // ③ 往左走：左边有个坑，掉下去会回到起点
  for (let i = 0; i < 40; i++) 走一帧();
  出.落地后y = 记账.帧[记账.帧.length - 1].末[1];
  const 左相 = [];
  派发("keydown", { key: "ArrowLeft", preventDefault() {} });
  for (let i = 0; i < 100; i++) {
    走一帧();
    const 末 = 记账.帧[记账.帧.length - 1].末;
    左相.push(末 ? Math.round(末[0]) : null);
  }
  派发("keyup", { key: "ArrowLeft", preventDefault() {} });
  出.左相 = 左相;

  // ④ 一路向右 + 连跳，跑到终点
  派发("keydown", { key: "ArrowRight", preventDefault() {} });
  let 到了 = 0;
  for (let i = 0; i < 900; i++) {
    if (i % 8 === 0) {
      派发("keydown", { key: " ", preventDefault() {} });
      派发("keyup", { key: " ", preventDefault() {} });
    }
    走一帧();
    if (记账.文本.some((t) => t.startsWith("过关"))) { 到了 = 帧; break; }
  }
  派发("keyup", { key: "ArrowRight", preventDefault() {} });

  出.帧数 = 帧;
  出.过关帧 = 到了;
  出.过关 = 记账.文本.filter((t) => t.startsWith("过关")).slice(-1);
  出.存档 = Object.fromEntries([...kv.entries()]);
  出.帧错 = j._帧错 == null ? null : String(j._帧错);
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


@pytest.fixture(scope="module")
def 跳过(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    d = tmp_path_factory.mktemp("m188p")
    脚本 = d / "platform.js"
    脚本.write_text(_JS_跳, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM), str(游戏们[2][2])],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-1500:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_平台跳跃第一帧就画了瓦片和主角(跳过):
    """瓦片真的画出来了（不是「地图读了就算」），而且主角是最后一条 `画子图`。"""
    assert 跳过["ok"], 跳过.get("错")
    assert 跳过["帧中"], "`图形.运行` 之后帧循环没在跑"
    # 视口 800 宽 = 25 列；只有实心格子才画 ⇒ 地面两行 50 块 + 空中砖台，50 上下
    assert 跳过["首帧子图"] >= 40, f"第一帧只画了 {跳过['首帧子图']} 块（瓦片没出来？）"
    主 = 跳过["首帧主角"]
    assert 主 is not None, "没画主角"
    # 起点列 4 → x = 128；站在第 16 行地面上 → y = 16*32-32 = 480
    assert abs(主[0] - 128) <= 2 and abs(主[1] - 384) <= 2, f"主角起点不对：{主}"


def test_平台跳跃重力会把人压到地面(跳过):
    """静置 30 帧后位置稳定 —— 重力 + 地面碰撞真的在起作用（不是一直往下掉）。"""
    assert abs(跳过["落地y"] - 跳过["落地后y"]) <= 2, \
        f"位置不稳定：{跳过['落地y']} → {跳过['落地后y']}"
    assert abs(跳过["落地y"] - 384) <= 2, f"没站在地面上：y={跳过['落地y']}"


def test_平台跳跃能跳起来(跳过):
    """按空格之后 y **变小**（画布 y 向下），而且不是「一直飞」—— 后面回到地面。"""
    assert 跳过["跳最高"] <= 跳过["落地y"] - 40, \
        f"跳不起来（最高 {跳过['跳最高']}，地面 {跳过['落地y']}）"


def test_平台跳跃掉进坑里会回到起点(跳过):
    """左边是个坑：往左走 ⇒ 掉下去 ⇒ **回到起点重来**。

    这条抓的是「掉出世界」那条判定 —— 没有它，掉下去的人只会看着空白屏幕。
    📌 判据看的是**序列**（x 先走到负数、紧接着跳回起点），不是某一帧的快照：
    按住左键时它掉下去又往前走，快照拍在哪一帧全看运气。
    """
    相 = 跳过["左相"]
    assert min(相) < 0, f"没走到坑里（最小 x = {min(相)}）"
    有重开 = any(相[i] < 0 and 相[i + 1] >= 120 for i in range(len(相) - 1))
    assert 有重开, f"掉下去之后没回起点：{相}"



def test_平台跳跃跑到终点会过关并记最快(跳过):
    """🔴 闭环：一路向右 + 连跳 ⇒ 到旗子 ⇒ 过关 + 最快用时进存档。

    这条同时验了「台阶能跳上去」—— 走不过去的地方卡住的话到不了终点。
    """
    assert 跳过["过关"], f"跑了 {跳过['帧数']} 帧也没到终点"
    存档 = 跳过["存档"]
    键 = "基石存档/平台跳跃最快"
    assert 键 in 存档, f"没存最快用时：{list(存档)}"
    值 = json.loads(存档[键])["值"]
    assert 0 < 值 < 60, f"用时不像话：{值}"


# ---------------------------------------------------------------------------
# 七、真窗口里跑一遍（CI 上是空白，见 CHANGELOG）
# ---------------------------------------------------------------------------

_无界面 = os.environ.get("JISHI_NO_GUI") == "1"

需要桌面 = pytest.mark.skipif(
    _无界面 or not sys.platform.startswith("win"),
    reason="要 Windows 桌面（CI 上是空白，见 CHANGELOG）")


def _win32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowW.restype = wintypes.HWND
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return u


def _等窗口(u, 标题, 秒=25.0):
    import time
    deadline = time.time() + 秒
    while time.time() < deadline:
        h = u.FindWindowW(None, 标题)
        if h:
            return h
        time.sleep(0.25)
    return None


@需要桌面
def test_打砖块在真窗口里跑起来(tmp_path):
    """真窗口 + 真 WebView2 + 真 wasm：断言页面把「引擎起来了」报给了宿主。

    断言的是**页面里跑通了**，不是「窗口建出来了」—— 后者是半个证据
    （M173 的教训：素材没带 ⇒ 白窗口、零报错）。
    """
    import time

    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 Rust 宿主（cargo build --release）")

    日志 = tmp_path / "窗口.log"
    env = {**os.environ, "JISHI_WINDOW_DEBUG": "1"}
    with open(日志, "w", encoding="utf-8") as f:
        进程 = subprocess.Popen([str(exe), str(砖_入口)], cwd=str(例子),
                                stdout=f, stderr=subprocess.STDOUT, env=env)
    try:
        u = _win32()
        hwnd = _等窗口(u, "打砖块")
        assert hwnd, "窗口没起来"
        # 等页面 + wasm 引擎起来（真 WebView2 首次启动要一两秒）
        期限 = time.time() + 25
        while time.time() < 期限:
            if "wasm 引擎起来了" in 日志.read_text(encoding="utf-8", errors="replace"):
                break
            time.sleep(0.3)
    finally:
        if 进程.poll() is None:
            u = _win32()
            h = u.FindWindowW(None, "打砖块")
            if h:
                u.PostMessageW(h, 0x0010, 0, 0)      # WM_CLOSE
            try:
                进程.wait(timeout=10)
            except subprocess.TimeoutExpired:
                进程.kill()

    文 = 日志.read_text(encoding="utf-8", errors="replace")
    assert "wasm 引擎起来了" in 文, f"窗口里的引擎没起来：\n{文[-1200:]}"
    assert "引擎失败" not in 文, f"页面报错了：\n{文[-1200:]}"
