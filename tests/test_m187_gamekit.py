# -*- coding: utf-8 -*-
"""M187 · 游戏工具箱：声音 / 精灵 / 瓦片 / 地图 / 存档 / 场景（26 个函数）。

## 边界（三份实现必须一致）

| 模块 | 命令行 / Node | 页面 / 窗口 |
|---|---|---|
| `声音` | ❌ 平台错（没有音频） | ✅ Web Audio |
| `存档` | ❌ 平台错（没有 localStorage） | ✅ |
| `精灵` `瓦片` | 纯逻辑能用；`画` / `载入` 报**画布**平台错 | ✅ |
| `地图` | ✅（传路径读文件） | ✅（传字典） |
| `场景` | ✅ 纯状态机 | ✅ |

## 判据分三层

1. **三份实现齐全**（直接查源码，缺哪个宿主一眼看得出来）；
2. **对拍**：同一段脚本在 Python / Rust / Node 上输出**逐字相同**（纯逻辑部分）；
3. **wasm 里真画**：假 canvas 记下每次 `画子图` 的参数 —— 瓦片 3×3 必须画出 **9** 块、
   精灵第 3 帧的源矩形必须是 `(2*帧宽, 0, 帧宽, 帧高)`；存档写进去读回来、版本号
   改坏时返回默认（**不抛错**）。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

WASM = ROOT / "site" / "jishi_wasm.wasm"
SHIM = ROOT / "wasm" / "jishi-wasm.js"

模块与函数 = {
    "声音": ["载入", "播放", "停止", "在放", "设音量", "主音量"],
    "精灵": ["新建", "播", "停", "设帧", "更新", "画", "当前帧"],
    "瓦片": ["载入", "画", "碰", "取"],
    "地图": ["读", "层"],
    "存档": ["存", "读", "删", "清"],
    "场景": ["定", "切", "现在"],
}

声音平台错 = ("这个平台没有声音：「声音」模块只在网页 / 窗口里有"
              "（命令行和 Node 上没有音频输出）。")
存档平台错 = ("这个平台没有存档：「存档」要页面的 localStorage"
              "（命令行和 Node 上没有）。")


def _跑(源码: str, tmp_path: Path, 引擎: str) -> str:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
    if 引擎 == "Rust":
        命令 = [str(rust_exe_path()), "程序.jsh"]
    elif 引擎 == "Node":
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
        命令 = ["node", str(ROOT / "oracle" / "node" / "index.js"), "程序.json"]
    else:
        命令 = [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"]
    r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True, timeout=120, input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def test_六个模块三宿主都在():
    """直接查源码：报错时能指出**缺的是哪个宿主、哪个函数**（m51 从另一头盯着）。"""
    py = (ROOT / "oracle" / "jishi" / "stdlib")
    rs = (ROOT / "rust" / "src" / "stdlib" / "gamekit.rs").read_text(encoding="utf-8")
    rs_s = (ROOT / "rust" / "src" / "stdlib" / "sound.rs").read_text(encoding="utf-8")
    js = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    for 模块, 函数们 in 模块与函数.items():
        for 名 in 函数们:
            assert f"def {名}(" in (py / f"{模块}.py").read_text(encoding="utf-8"), \
                f"Python 侧缺 `{模块}.{名}`"
            assert f"'{名}':" in js, f"Node 侧缺 `{模块}.{名}`"
    for 名 in 模块与函数["声音"]:
        assert f'"{名}" =>' in rs_s, f"Rust 侧缺 `声音.{名}`"
    for 模块, 函数们 in 模块与函数.items():
        if 模块 == "声音":
            continue
        for 名 in 函数们:
            assert f'"{名}" =>' in rs, f"Rust 侧缺 `{模块}.{名}`"


def test_声音与存档报平台错_三宿主逐字相同(tmp_path):
    for 源, 期望 in (
        ('导入 声音\n声音.播放(1)\n', 声音平台错),
        ('导入 存档\n存档.存("a", 1)\n', 存档平台错),
    ):
        出 = {名: _跑(源, tmp_path, 名) for 名 in ("Python", "Rust", "Node") if
              (名 != "Rust" or rust_exe_path().is_file())}
        assert len(出) >= 2
        for 名, 文 in 出.items():
            assert 期望 in 文, f"{名} 没报平台错：{文[:300]}"


#: 纯逻辑对拍：精灵 / 瓦片 / 场景 / 地图 —— 三宿主输出必须**逐字相同**。
_纯逻辑 = '''导入 精灵
导入 瓦片
导入 场景
导入 地图

令 精灵甲 = 精灵.新建(1, {"宽": 32, "高": 32, "列数": 4, "行数": 2})
精灵.播(精灵甲, "走", {"帧们": [0, 1, 2, 3], "速度": 10})
精灵.更新(精灵甲, 100)
精灵.更新(精灵甲, 100)
打印(精灵.当前帧(精灵甲))
精灵.设帧(精灵甲, 6)
打印(精灵.当前帧(精灵甲))

令 图 = 瓦片.载入({"图集": 7, "瓦片宽": 32, "瓦片高": 32,
                   "行们": [[0, 1, 2], [3, -1, 5], [6, 7, 空]]})
打印(瓦片.取(图, 0, 0), 瓦片.取(图, 1, 1), 瓦片.取(图, 2, 2), 瓦片.取(图, 9, 9))
打印(瓦片.碰(图, {"x": 0, "y": 0, "宽": 70, "高": 40}))
打印(图["列数"], 图["行数"])

令 地 = 地图.读({"瓦片宽": 16, "瓦片高": 16, "图集": "t.png",
                 "层们": [{"名": "地面", "行们": [[1, 2]]}]})
打印(地["瓦片宽"], 长度(地["层们"]))
打印(地图.层(地, "地面")["名"], 地图.层(地, "没有") == 空)

令 次数 = {"标题": 0, "游戏": 0}
函数 标题_进()：
    次数["标题"] = 次数["标题"] + 1
函数 游戏_进()：
    次数["游戏"] = 次数["游戏"] + 1
场景.定("标题", {"初始化": 标题_进})
场景.定("游戏", {"初始化": 游戏_进})
打印(场景.切("标题"), 场景.切("标题"))
打印(场景.切("游戏"), 场景.现在())
打印(次数["标题"], 次数["游戏"])
'''


def test_纯逻辑对拍_三宿主逐字相同(tmp_path):
    """精灵 / 瓦片 / 场景 / 地图 在三个宿主上输出**一模一样**。

    这几个模块是三份原生实现（路线图原想用 `stdlib-jishi` 写一份，但库层在
    Rust / wasm 里不展开 ⇒ 浏览器试玩页会用不了，见 D111）。三份就得盯住别漂。
    """
    出 = {名: _跑(_纯逻辑, tmp_path, 名) for 名 in ("Python", "Rust", "Node") if
          (名 != "Rust" or rust_exe_path().is_file())}
    assert len(出) == 3, f"三个宿主都得跑起来：{list(出)}"
    基准 = 出["Python"]
    assert "3" in 基准 and "6" in 基准, f"Python 自己就不对：{基准[:400]}"
    for 名, 文 in 出.items():
        assert 文 == 基准, f"{名} 与 Python 不一致：\n--- Python ---\n{基准}\n--- {名} ---\n{文}"


# ---------------------------------------------------------------------------
# 三、wasm 里真画（Node + 假 canvas）
# ---------------------------------------------------------------------------

_JS = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);

// 假的 `Image`：设 src 就立刻「加载完」——否则 `画子图` 会跳过绘制（异步保护）
global.Image = class { set src(v) { if (this.onload) this.onload(); } };
const 记录 = [];
function ctx() {
  const c = { fillStyle: "", globalAlpha: 1 };
  const 无 = () => {};
  for (const m of ["beginPath", "closePath", "fill", "stroke", "moveTo", "rotate",
    "scale", "fillRect", "strokeRect", "fillText", "strokeText"]) c[m] = 无;
  // 记账**变换**：`画子图` 先 translate 到目标框中心、再在 -dw/2 处画
  // ⇒ 直接读 drawImage 的 dx 会拿到「相对量」。这里把 translate 累起来，
  // 记的是**落到画布上的绝对位置**（判据要的是「精灵画在 (100,50)」）。
  let tx = 0, ty = 0;
  const 栈 = [];
  c.save = () => { 栈.push([tx, ty]); };
  c.restore = () => { const s = 栈.pop(); if (s) { tx = s[0]; ty = s[1]; } };
  c.translate = (x, y) => { tx += x; ty += y; };
  c.setTransform = () => { tx = 0; ty = 0; };
  c.arc = 无; c.ellipse = 无; c.clearRect = 无;
  c.measureText = () => ({ width: 8, actualBoundingBoxAscent: 12, actualBoundingBoxDescent: 3 });
  c.getImageData = () => ({ data: [0, 0, 0, 255] });
  c.drawImage = (img, sx, sy, sw, sh, dx, dy, dw, dh) =>
    记录.push(["drawImage", img, sx, sy, sw, sh, tx + dx, ty + dy, dw, dh]);
  return c;
}
const cv = { width: 0, height: 0, style: {}, clientWidth: 320, clientHeight: 200, getContext: () => ctx() };
const doc = { querySelector: (s) => (s === "#画布" ? cv : null), createElement: () => cv,
              body: { appendChild: () => {} }, title: "" };
// 假 localStorage（存档判据要用）
const kv = new Map();
const store = { getItem: (k) => (kv.has(k) ? kv.get(k) : null),
                setItem: (k, v) => kv.set(k, String(v)),
                removeItem: (k) => kv.delete(k) };

const SRC = `导入 图形
导入 精灵
导入 瓦片
导入 存档
导入 场景

令 次数 = {"进": 0}

图形.准备({"宽": 320, "高": 200})
令 图集 = 图形.读图("x.png")

函数 进游戏()：
    次数["进"] = 次数["进"] + 1

场景.定("游戏", {"初始化": 进游戏})
打印("切两次", 场景.切("游戏"), 场景.切("游戏"))

令 图 = 瓦片.载入({"图集": 图集, "瓦片宽": 32, "瓦片高": 32,
                   "行们": [[0, 1, 2], [3, 4, 5], [6, 7, 8]]})
打印("瓦片画了", 瓦片.画(图))

令 甲 = 精灵.新建(图集, {"宽": 16, "高": 16, "列数": 4, "行数": 2})
精灵.设帧(甲, 2)
精灵.画(甲, 100, 50)

存档.存("最高分", {"分": 42, "名": "小明", "记录": [1, 2, 3]})
打印("读回", 存档.读("最高分", 空)["分"])
打印("没存过", 存档.读("不存在", "默认值"))
`;

(async () => {
  const j = await JishiWasm.load(BYTES, { doc, store, raf: () => 0 });
  const r = j.run(SRC);
  if (!r.ok) { console.error(JSON.stringify(r.error)); process.exit(2); }
  // 画子图的参数：drawImage(img, sx, sy, sw, sh, dx, dy, dw, dh)
  const 子图 = 记录.filter((x) => x[0] === "drawImage");
  const 出 = {
    ok: r.ok,
    出: r.stdout,
    瓦片数: 子图.length - 1,          // 最后一条是精灵
    精灵源: 子图[子图.length - 1].slice(2, 6),
    精灵目标: 子图[子图.length - 1].slice(6),
  };
  // 存档：版本号改坏 ⇒ 读回默认值（**不抛错**）
  kv.set("基石存档/最高分", '{"版本":99,"值":{"分":42}}');
  const r2 = j.run('导入 存档\n打印("坏版本", 存档.读("最高分", "默认"))\n');
  出.坏版本 = r2.ok ? r2.stdout.trim() : ("ERR:" + r2.error.message);
  // 清：清掉之后读不到
  const r3 = j.run('导入 存档\n打印(存档.清(), 存档.读("最高分", "空"))\n');
  出.清 = r3.ok ? r3.stdout.trim() : ("ERR:" + r3.error.message);
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


@pytest.fixture(scope="module")
def wasm出(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    d = tmp_path_factory.mktemp("m187")
    脚本 = d / "m187.js"
    脚本.write_text(_JS, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-1200:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_wasm里瓦片画了9块(wasm出):
    """3×3 的瓦片地图 ⇒ **9 次 `画子图`**（可核对的事实，不是「没报错」）。"""
    assert wasm出["ok"], wasm出.get("出")
    assert wasm出["瓦片数"] == 9, f"应当画 9 块，实际 {wasm出['瓦片数']}"


def test_wasm里精灵第3帧的源矩形对(wasm出):
    """第 2 帧（0 起）在 4 列 16×16 的图集上 ⇒ 源矩形 `(32, 0, 16, 16)`。"""
    源 = wasm出["精灵源"]
    assert 源 == [32, 0, 16, 16], f"源矩形应当是 (32,0,16,16)，实际 {源}"
    assert wasm出["精灵目标"] == [100, 50, 16, 16], f"目标位置不对：{wasm出['精灵目标']}"


def test_wasm里存档能写能读(wasm出):
    """写进去、读回来（字典也能存），「没存过」给默认值。"""
    出 = wasm出["出"]
    assert "读回 42" in 出, f"读回来的值不对：{出!r}"
    assert "没存过 默认值" in 出, f"没存过的应当给默认值：{出!r}"
    assert "瓦片画了 9" in 出, f"`瓦片.画` 应当返回 9：{出!r}"


def test_场景初始化每次切进来只跑一次(wasm出):
    """`切` 两次同一个场景 ⇒ `初始化` 只跑 **1** 次（第二次返回假）。"""
    assert "切两次 真 假" in wasm出["出"], f"切两次的结果应当是「真 假」：{wasm出['出']!r}"


def test_存档版本号不对返回默认_不抛错(wasm出):
    """🔴 **存档坏了不该让游戏起不来**：版本不对 ⇒ 返回默认值，不抛错。"""
    assert wasm出["坏版本"].startswith("坏版本 默认"), \
        f"版本不对应当返回默认值，实际 {wasm出['坏版本']!r}"


def test_存档清掉之后读不到(wasm出):
    assert wasm出["清"].startswith("1 空"), f"清掉之后应当读回默认：{wasm出['清']!r}"


# ---------------------------------------------------------------------------
# 四、声音：假 Web Audio（浏览器里真放的那一段）
#
# 这里验的是**浏览器自动播放策略**那一段：没解锁时 `播放` 返回假、不报错；
# 用户动一下（pointerdown → resume）之后才真放。真音频解码在浏览器里才验得了，
# 判据只验「调用顺序与返回值」——那是写游戏的人唯一能观察到的部分。
# ---------------------------------------------------------------------------

_SOUND_JS = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);

// 假 Web Audio：不解真音频，只记「谁被 start 了」
let 放了 = 0;
const 监听 = {};
global.addEventListener = (ev, fn) => { 监听[ev] = fn; };
class 假上下文 {
  constructor() { this.state = "suspended"; this.destination = {}; }
  resume() { this.state = "running"; return Promise.resolve(); }
  createGain() { return { gain: { value: 1 }, connect() {} }; }
  createBufferSource() {
    const 源 = {
      buffer: null, loop: false, playbackRate: { value: 1 }, onended: null,
      connect() {}, start() { 放了 += 1; },
      stop() { const f = 源.onended; 源.onended = null; if (f) f(); },
    };
    return 源;
  }
  decodeAudioData(ab, 好) { setTimeout(() => 好({ 解好了: true }), 0); }
}
global.AudioContext = 假上下文;
global.fetch = () => Promise.resolve({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) });

const doc = { querySelector: () => null, createElement: () => ({}), body: { appendChild: () => {} } };

(async () => {
  const j = await JishiWasm.load(BYTES, { doc, store: null, raf: () => 0 });
  const 出 = {};
  const r1 = j.run('导入 声音\n令 音 = 声音.载入("点.wav")\n打印("未解锁", 声音.播放(音))\n');
  出.未解锁 = r1.ok ? r1.stdout.trim() : ("ERR:" + r1.error.message);
  // 等解码的 promise 走完（decodeAudioData 是异步的）
  await new Promise((r) => setTimeout(r, 30));
  // 用户动一下 —— 浏览器里那一次手势就是解锁钥匙
  if (监听["pointerdown"]) 监听["pointerdown"]();
  // 句柄表在 shim 里 ⇒ 跨 `run` 还是同一个号（1）。这里**故意不重新载入**：
  // 重载会新建一条还没解码完的音，`播放` 照规矩返回假 —— 那是另一条判据的事。
  const r2 = j.run('导入 声音\n令 音 = 1\n打印("放", 声音.播放(音), 声音.在放(音))\n'
                 + '打印("音量", 声音.设音量(音, 0.5), 声音.主音量(0.4), 声音.主音量())\n'
                 + '声音.停止(音)\n打印("停了", 声音.在放(音))\n');
  出.放 = r2.ok ? r2.stdout.trim().split("\n") : ["ERR:" + r2.error.message];
  出.放了 = 放了;
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


@pytest.fixture(scope="module")
def 声音出(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    d = tmp_path_factory.mktemp("m187s")
    脚本 = d / "sound.js"
    脚本.write_text(_SOUND_JS, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-1200:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_声音没解锁时返回假_不报错(声音出):
    """🔴 自动播放策略：用户还没动之前 `播放` 给**假**（不是报错）。"""
    assert 声音出["未解锁"] == "未解锁 假", 声音出["未解锁"]


def test_声音解锁后能放_停止之后在放变假(声音出):
    assert 声音出["放"][0] == "放 真 真", 声音出["放"]
    assert 声音出["放"][1] == "音量 0.5 0.4 0.4", 声音出["放"]
    assert 声音出["放"][2] == "停了 假", 声音出["放"]
    assert 声音出["放了"] == 1, f"应当真的 start 过一次，实际 {声音出['放了']}"
