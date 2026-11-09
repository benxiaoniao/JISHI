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
入口 = 例子 / "打砖块.jsh"
游戏源 = 例子 / "打砖块_游戏.jsh"
页面 = 例子 / "打砖块.html"
音效 = 例子 / "碰.wav"


# ---------------------------------------------------------------------------
# 一、文件齐不齐（最便宜的一条，先红先知道）
# ---------------------------------------------------------------------------

def test_打砖块四件套都在():
    """入口 / 本体 / 页面 / 音效 —— 少一件的表现都是「窗口开了但什么都没发生」。"""
    for f in (入口, 游戏源, 页面, 音效):
        assert f.is_file(), f"缺 {f.relative_to(ROOT)}"


def test_入口只开窗_游戏本体单独一份():
    """桌面入口只管开窗（`窗口.打开` + `窗口.等`），游戏逻辑在另一个文件里。

    为什么必须分开：官网试玩页跑的是**同一份**本体，它已经在页面里了，
    不能再 `窗口.打开`（没有第二个窗口可开）。
    """
    文 = 入口.read_text(encoding="utf-8")
    assert "窗口.打开(" in 文 and "窗口.等(" in 文, "入口没开窗 / 没等窗口"
    assert "打砖块.html" in 文, "入口没指向页面"
    assert "图形.运行" not in 文, "入口里不该有游戏逻辑（那是本体的活）"


def test_页面没有把游戏源码抄一份():
    """🔴 **源码只有一份**：页面只 fetch 它，不许内联。

    M188 的约定：窗口里与官网试玩页跑的是同一份 `打砖块_游戏.jsh`。抄进 HTML
    就会漂（改了一边、另一边还是老的，而且在页面上完全看不出来）。
    """
    文 = 页面.read_text(encoding="utf-8")
    assert "打砖块_游戏.jsh" in 文, "页面没有 fetch 源码"
    for 记号 in ("图形.运行", "函数 每帧", "砖色们"):
        assert 记号 not in 文, f"页面里出现了游戏源码片段「{记号}」—— 源码被抄了一份"


def test_页面两条路径都有_窗口与官网():
    """窗口里素材挂在 `/__jishi/`，官网上与页面同级 —— 两条都得在，
    否则一边是白页面（M173 踩过：wasm 写成相对路径，窗口里 404）。

    ⚠️ 同级那条**必须带 `./`**：`import("jishi-wasm.js")` 会被当成裸模块名，
    浏览器报 `Failed to resolve module specifier`（M188 真浏览器截图才发现，
    Node 判据照不出来 —— 判据里 import 的是绝对路径）。
    """
    文 = 页面.read_text(encoding="utf-8")
    assert "/__jishi/jishi-wasm.js" in 文 and "/__jishi/jishi_wasm.wasm" in 文
    assert '"./jishi-wasm.js"' in 文 and '"./jishi_wasm.wasm"' in 文
    assert 'import("jishi-wasm.js")' not in 文, "少 `./` 会被当成裸模块名"


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
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM), str(游戏源)],
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
# 三、真窗口里跑一遍（CI 上是空白，见 CHANGELOG）
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
        进程 = subprocess.Popen([str(exe), str(入口)], cwd=str(例子),
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
