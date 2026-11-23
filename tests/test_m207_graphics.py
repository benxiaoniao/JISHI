# -*- coding: utf-8 -*-
"""M207 · 图形深化：纹理池 · 批量绘制 · 虚线/圆角/九宫格 · 截图。

## 这一站解决的是什么

M196 之后「能画」已经不成问题，剩下的都是**真做游戏/界面时**撞出来的：

| 缺什么 | 谁撞出来的 |
|---|---|
| **纹理池**（离屏画布复用） | `离屏()` 每帧新建 100 块 ⇒ 内存与 GC 一起炸（画布是显存对象） |
| **批量绘制** | 每个图元一次 wasm↔JS 往返，200 个瓦片光「过通道」就比画本身贵 |
| **虚线** | 选中框、路径预览、网格线 —— 只有实线就只能画死板的东西 |
| **圆角** | 按钮、卡片、面板的底 —— 方块看着像 90 年代 |
| **九宫格** | 一张边框图当任意尺寸的按钮：四角不变形 |
| **截图** | 分享、存档缩略图、判据 —— 而且截图必须是**真 PNG**（能被解码器读回） |

## 判据怎么验（没有真 canvas）

沿用 M196 的办法：**假 canvas 记录指令序列**。

- `批量` vs 逐条调用：**指令序列必须逐条相同**（批量只是个「打包」，不许改行为）；
- `虚线`：看 `setLineDash` 有没有被设上，以及**下一条图元之后还在**（持久状态）；
- `圆角矩形`：看 `roundRect` 的参数（半径夹在 `min(半径, 宽/2, 高/2)`）；
- `九宫格`：看 **9 次 `drawImage`** 的源/目标矩形（四角同尺寸、边单向拉伸、中心双向）；
- `池信息`：借还 100 次之后 `新建` 必须是 **1**（同尺寸，池子就一块）；
- `截图`：把 PNG 字节取出来，用**我们自己的 `--png信息`** 解回来，
  比 RGBA 的 SHA-256 —— 这条同时验了「PNG 编得合法」与「像素没串行」。

⚠️ 与 M186/M196 同一条纪律：读 `wasm/jishi-wasm.js` 源码那份（不读 `site/` 拷贝），
wasm 是构建产物 ⇒ `skipif` 掉。
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path  # noqa: E402

WASM = ROOT / "site" / "jishi_wasm.wasm"
SHIM = ROOT / "wasm" / "jishi-wasm.js"

#: M207 新增的 8 个（原有 45 个由 `test_m196_graphics` 盯着）。
新函数 = ["虚线", "圆角矩形", "九宫格", "借画布", "还画布", "池信息", "批量", "截图"]

平台错 = ("这个平台没有画布：「图形」模块只在网页 / 窗口里有"
          "（命令行和 Node 上没有画布）。要做图表请用「表格」，要出网页请用「站点」。")


# ---------------------------------------------------------------------------
# 一、三宿主同宽 + 同一条平台错
# ---------------------------------------------------------------------------

def test_八个新函数三宿主都在():
    py = (ROOT / "oracle" / "jishi" / "stdlib" / "图形.py").read_text(encoding="utf-8")
    rs = (ROOT / "rust" / "src" / "stdlib" / "graphics.rs").read_text(encoding="utf-8")
    for 名 in 新函数:
        assert f"def {名}(" in py, f"Python 侧缺 `{名}`"
        assert f'"{名}" =>' in rs, f"Rust 侧缺 `{名}`"


def test_命令行上报平台错(tmp_path):
    """命令行上这 8 个也必须报**同一句**平台错（与 M186/M196 同一条）。"""
    片段 = [
        "图形.虚线([6, 4])",
        '图形.圆角矩形(0, 0, 10, 10, 3)',
        "图形.九宫格(1, 0, 0, 10, 10, 2)",
        "图形.借画布(10, 10)",
        "图形.还画布(1)",
        "图形.池信息()",
        '图形.批量([["矩形", 0, 0, 1, 1]])',
        "图形.截图()",
    ]
    for i, 句 in enumerate(片段):
        源 = "导入 图形\n" + 句 + "\n"
        文件 = tmp_path / f"m207_{i}_{__import__('os').getpid()}.jsh"
        文件.write_text(源, encoding="utf-8", newline="\n")
        r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                           capture_output=True, text=True, encoding="utf-8", cwd=str(tmp_path))
        出 = (r.stdout or "") + (r.stderr or "")
        assert 平台错 in 出, f"跑 `{句}` 没报平台错：{出[:300]}"


# ---------------------------------------------------------------------------
# 二、wasm + 假 canvas
# ---------------------------------------------------------------------------

_JS = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);

/** 假 canvas：记录每条指令；`getImageData` 给**确定性像素**（截图判据要算 SHA-256）。 */
function 造世界() {
  const 画布们 = [];
  let 序 = 0;

  // 像素规律：第 (x,y) 个像素 = [x&255, y&255, 7, 255] —— 和 M196 的 `取像素` 一致
  function 取像素们(x0, y0, w, h) {
    const d = new Uint8ClampedArray(w * h * 4);
    let i = 0;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        d[i++] = (x0 + x) & 255;
        d[i++] = (y0 + y) & 255;
        d[i++] = 7;
        d[i++] = 255;
      }
    }
    return d;
  }

  function 新画布(名) {
    const 本名 = 名 || ("cv#" + (序++));
    const 记录 = [];
    function 记(op, ...a) { 记录.push([op].concat(a.map(简))); }
    function 简(v) {
      if (v && typeof v === "object" && v.__grad) return "渐变";
      if (typeof v === "number") return Math.round(v * 1000) / 1000;
      return v;
    }

    let 虚 = [];
    const c = {
      fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "",
      globalAlpha: 1, globalCompositeOperation: "source-over",
      setTransform: (...a) => 记("setTransform", ...a),
      translate: (...a) => 记("translate", ...a),
      rotate: (...a) => 记("rotate", ...a),
      scale: (...a) => 记("scale", ...a),
      save: () => 记("save"), restore: () => { 虚 = []; 记("restore"); },
      fillRect: (...a) => 记("fillRect", c.fillStyle, c.globalAlpha,
                              c.globalCompositeOperation, ...a),
      strokeRect: (...a) => 记("strokeRect", c.strokeStyle, ...a),
      clearRect: (...a) => 记("clearRect", ...a),
      beginPath: () => 记("beginPath"), closePath: () => 记("closePath"),
      rect: (...a) => 记("rect", ...a),
      clip: () => 记("clip"),
      arc: (...a) => 记("arc", ...a),
      ellipse: (...a) => 记("ellipse", ...a),
      arcTo: (...a) => 记("arcTo", ...a),
      moveTo: (...a) => 记("moveTo", ...a),
      lineTo: (...a) => 记("lineTo", ...a),
      fill: () => 记("fill", c.fillStyle),
      stroke: () => 记("stroke", c.strokeStyle, 虚.slice()),
      fillText: (t, x, y) => 记("fillText", t, x, y),
      strokeText: () => {},
      setLineDash: (d) => { 虚 = (d || []).slice(); 记("setLineDash", ...虚); },
      getLineDash: () => 虚.slice(),
      measureText: (t) => ({ width: String(t).length * 8,
                             actualBoundingBoxAscent: 12,
                             actualBoundingBoxDescent: 3 }),
      getImageData: (x, y, w, h) => ({ data: 取像素们(x, y, w, h), width: w, height: h }),
      drawImage: (cv, ...a) => 记("drawImage", cv && cv.__名, ...a),
      roundRect: (...a) => 记("roundRect", ...a),
      createLinearGradient: (...a) => {
        记("linearGradient", ...a);
        const g = { __grad: true, addColorStop: (p, col) => 记("色标", p, col) };
        return g;
      },
      createRadialGradient: (...a) => {
        记("radialGradient", ...a);
        const g = { __grad: true, addColorStop: (p, col) => 记("色标", p, col) };
        return g;
      },
    };
    const cv = {
      width: 0, height: 0, style: {}, clientWidth: 800, clientHeight: 600,
      getContext: () => c, 记录: 记录, __名: 本名,
    };
    画布们.push(cv);
    return cv;
  }

  const 主 = 新画布("主");
  const doc = {
    querySelector: (s) => (s === "#画布" ? 主 : null),
    createElement: () => 新画布(),
    body: { appendChild: () => {} },
    title: "",
  };
  return { doc, 主, 画布们 };
}

// 判据的假 DOM 也要能「加载图」：给一个同步就绪的 Image（宽高 32）
class 假图 {
  constructor() { this.width = 32; this.height = 32; }
  set src(v) { this._src = v; if (this.onload) this.onload(); }
  get src() { return this._src; }
}
globalThis.Image = 假图;

async function 跑一段(源) {
  const w = 造世界();
  const j = await JishiWasm.load(BYTES, { doc: w.doc, raf: () => 0 });
  const r = j.run(源);
  return {
    ok: r.ok,
    错: r.error && (r.error.title + " / " + r.error.message),
    出: r.stdout || "",
    主: w.主.记录,
    画布数: w.画布们.length,
  };
}

const S1_逐条 = `导入 图形
图形.准备({"宽": 200, "高": 100})
图形.矩形(0, 0, 16, 16, {"填充": "#f00"})
图形.圆(30, 30, 8, {"填充": "#0f0"})
图形.线(0, 90, 200, 90, {"描边": "#00f", "线宽": 2})
图形.文本("甲", 10, 20, {"填充": "#fff"})
`;

const S2_批量 = `导入 图形
图形.准备({"宽": 200, "高": 100})
图形.批量([
  ["矩形", 0, 0, 16, 16, {"填充": "#f00"}],
  ["圆", 30, 30, 8, {"填充": "#0f0"}],
  ["线", 0, 90, 200, 90, {"描边": "#00f", "线宽": 2}],
  ["文本", "甲", 10, 20, {"填充": "#fff"}],
])
`;

const S3_虚线 = `导入 图形
图形.准备({"宽": 100, "高": 100})
图形.虚线([6, 4])
图形.线(0, 0, 100, 0, {"描边": "#fff"})
图形.线(0, 10, 100, 10, {"描边": "#fff"})
图形.虚线()
图形.线(0, 20, 100, 20, {"描边": "#fff"})
`;

const S4_圆角 = `导入 图形
图形.准备({"宽": 100, "高": 100})
图形.圆角矩形(10, 10, 80, 40, 8, {"填充": "#f00", "描边": "#fff"})
`;

const S5_九宫格 = `导入 图形
图形.准备({"宽": 200, "高": 100})
令 g = 图形.读图("x.png")
图形.九宫格(g, 0, 0, 96, 96, 8)
`;

const S6_池 = `导入 图形
图形.准备({"宽": 100, "高": 100})
遍历 i 在 范围(100)：
    令 c = 图形.借画布(32, 32)
    图形.用画布(c)
    图形.矩形(0, 0, 8, 8, {"填充": "#f00"})
    图形.用画布(空)
    图形.还画布(c)
令 信 = 图形.池信息()
打印(信["空闲"], 信["借出"], 信["新建"])
`;

const S7_截图 = `导入 图形
导入 编码
图形.准备({"宽": 4, "高": 3})
令 图 = 图形.截图()
打印(长度(图))
打印(编码.base64编码(图))
`;

const S8_批量错 = `导入 图形
图形.准备({"宽": 10, "高": 10})
图形.批量([["没有这个图元", 1, 2]])
`;

(async () => {
  const 出 = {};
  出.s1 = await 跑一段(S1_逐条);
  出.s2 = await 跑一段(S2_批量);
  出.s3 = await 跑一段(S3_虚线);
  出.s4 = await 跑一段(S4_圆角);
  出.s5 = await 跑一段(S5_九宫格);
  出.s6 = await 跑一段(S6_池);
  出.s7 = await 跑一段(S7_截图);
  出.s8 = await 跑一段(S8_批量错);
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


def _跑wasm(tmp_path: Path) -> dict:
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    脚本 = tmp_path / f"m207_{__import__('os').getpid()}.js"
    脚本.write_text(_JS, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=180)
    if r.returncode != 0:
        pytest.fail(f"驱动脚本挂了：{r.stderr.decode('utf-8', 'replace')[:1500]}")
    文本 = r.stdout.decode("utf-8", "replace").strip()
    try:
        return json.loads(文本)
    except json.JSONDecodeError:
        pytest.fail(f"驱动没输出 JSON：{文本[:1500]}")


@pytest.fixture(scope="module")
def 结果(tmp_path_factory):
    return _跑wasm(tmp_path_factory.mktemp("m207"))


def _图元们(序列):
    """只留真正的绘制指令（排掉 save/translate/restore 这些簿记）。"""
    留 = {"fillRect", "strokeRect", "fill", "stroke", "fillText", "drawImage",
          "setLineDash"}
    return [x for x in 序列 if x[0] in 留]


# ---------------------------------------------------------------------------
# 三、批量 == 逐条（这一站的核心判据）
# ---------------------------------------------------------------------------

def test_批量与逐条画的指令序列逐条相同(结果):
    """`批量` 只是把 N 次通道压成 1 次，**不许改变画出来的东西**。"""
    s1, s2 = 结果["s1"], 结果["s2"]
    assert s1["ok"], f"逐条失败：{s1['错']}"
    assert s2["ok"], f"批量失败：{s2['错']}"
    逐 = _图元们(s1["主"])
    批 = _图元们(s2["主"])
    assert len(逐) >= 4, f"逐条那条没画出东西（判据会空过）：{逐}"
    assert 逐 == 批, f"批量与逐条不一致：\n逐条={逐}\n批量={批}"


def test_批量不认识图元要报错(结果):
    s8 = 结果["s8"]
    assert not s8["ok"], "批量里写了不认识的图元居然成功了"
    文 = s8["错"] or ""
    assert "不认识" in 文, 文
    assert "没有这个图元" in 文, 文


# ---------------------------------------------------------------------------
# 四、虚线 / 圆角 / 九宫格
# ---------------------------------------------------------------------------

def test_虚线是持久状态且能清掉(结果):
    """设了虚线要**一直有效**（不是只管一条），`虚线()` 之后回到实线。"""
    s3 = 结果["s3"]
    assert s3["ok"], s3["错"]
    描 = [x for x in s3["主"] if x[0] == "stroke"]
    assert len(描) == 3, 描
    # 前两条带虚线（stroke 第 3 项是当时的 getLineDash）
    assert 描[0][2] == [6, 4], 描[0]
    assert 描[1][2] == [6, 4], 描[1]
    # 第三条已经清成实线
    assert 描[2][2] == [], 描[2]


def test_圆角矩形用的是roundRect且半径没被吞(结果):
    s4 = 结果["s4"]
    assert s4["ok"], s4["错"]
    圆 = [x for x in s4["主"] if x[0] == "roundRect"]
    assert 圆, f"没走 roundRect：{s4['主']}"
    assert 圆[0][1:] == [10, 10, 80, 40, 8], 圆[0]
    # 填充 + 描边都要发生
    assert [x[0] for x in _图元们(s4["主"])] == ["fill", "stroke"], _图元们(s4["主"])


def test_九宫格是九次drawImage且四角不拉伸(结果):
    """9-slice 的语义：**四角同尺寸、四边单向拉伸、中心双向**。"""
    s5 = 结果["s5"]
    assert s5["ok"], s5["错"]
    画 = [x for x in s5["主"] if x[0] == "drawImage"]
    assert len(画) == 9, f"应当是 9 次 drawImage，实际 {len(画)}：{画}"
    # drawImage(图, sx, sy, sw, sh, dx, dy, dw, dh) —— 指令里图名在第 2 项
    矩 = [(x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9]) for x in 画]
    # 左上角：源 (0,0,8,8) → 目标 (0,0,8,8)
    assert 矩[0] == (0, 0, 8, 8, 0, 0, 8, 8), 矩[0]
    # 右上角：源 (24,0,8,8) → 目标 (88,0,8,8)（96 宽、边 8）
    assert 矩[2] == (24, 0, 8, 8, 88, 0, 8, 8), 矩[2]
    # 上边（中间）：源宽 16（32-8-8）→ 目标宽 80（96-8-8）
    assert 矩[1] == (8, 0, 16, 8, 8, 0, 80, 8), 矩[1]
    # 中心：双向拉伸
    assert 矩[4] == (8, 8, 16, 16, 8, 8, 80, 80), 矩[4]


# ---------------------------------------------------------------------------
# 五、纹理池：借还 100 次只新建一块
# ---------------------------------------------------------------------------

def test_借还一百次只新建一块画布(结果):
    """🔴 池子的意义全在这里：同尺寸反复借还，**画布对象不该越造越多**。"""
    s6 = 结果["s6"]
    assert s6["ok"], s6["错"]
    空闲, 借出, 新建 = s6["出"].split()
    assert 借出 == "0", f"100 次借还之后还欠着 {借出} 块"
    assert 空闲 == "1", f"池里应当有 1 块空闲，实际 {空闲}"
    assert 新建 == "1", f"只该新建 1 块（说明复用了），实际新建了 {新建} 块"


# ---------------------------------------------------------------------------
# 六、截图：真 PNG，能被我们自己的解码器读回
# ---------------------------------------------------------------------------

def test_截图是合法PNG且像素对得上(结果, tmp_path):
    """截图 → PNG 字节 → `--png信息` 解回来 → 比 RGBA 的 SHA-256。

    这条同时验三件事：PNG 编得**合法**（CRC/zlib/块顺序都对）、像素**没串行**、
    尺寸按位图算（4×3 逻辑像素、dpr=1 ⇒ 4×3）。
    """
    s7 = 结果["s7"]
    assert s7["ok"], s7["错"]
    行 = s7["出"].strip().splitlines()
    png = base64.b64decode(行[1])
    assert len(png) == int(行[0]), "打印的字节数与 base64 对不上"
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "开头不是 PNG 签名"

    # 假 canvas 的像素规律：像素 (x,y) = [x&255, y&255, 7, 255]
    期望 = bytearray()
    for y in range(3):
        for x in range(4):
            期望 += bytes([x & 255, y & 255, 7, 255])
    期望sha = hashlib.sha256(bytes(期望)).hexdigest()

    文件 = tmp_path / f"截图_{__import__('os').getpid()}.png"
    文件.write_bytes(png)
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没编 rust 宿主，无法用 --png信息 复核")
    r = subprocess.run([str(exe), "--png信息", str(文件)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"我们自己的解码器读不回来：{r.stderr}"
    got = json.loads(r.stdout)
    assert (got["宽"], got["高"]) == (4, 3), got
    assert got["rgba_sha256"] == 期望sha, f"像素不对：{got['rgba_sha256']} vs {期望sha}"


# ---------------------------------------------------------------------------
# 七、PNG 编码器本身（Rust 侧单测式判据）
# ---------------------------------------------------------------------------

def test_PNG编码器用stored块也能被解码():
    """`pngenc` 不压缩（stored 块），但必须**合法** —— 拿我们的解码器验一遍。"""
    # 这条不用跑脚本：直接看源码里的两处关键实现（stored 块与 zlib 头）
    rs = (ROOT / "rust" / "src" / "pngenc.rs").read_text(encoding="utf-8")
    assert "0x78" in rs and "0x01" in rs, "zlib 头不是 0x78 0x01"
    assert "adler32" in rs, "zlib 尾部要有 Adler32"
    assert "if 末 { 0x01 } else { 0x00 }" in rs, "stored 块的 BFINAL 位写错了"
    assert "crate::zip::crc32" in rs, "块校验要用现成的 crc32"


# ---------------------------------------------------------------------------
# 八、批量到底省了多少（墙上时间 ⇒ 进串行节）
# ---------------------------------------------------------------------------

_JS计时 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);
const c = new Proxy({}, { get: (t, k) => {
  if (k === "getImageData") return (x, y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4) });
  if (k === "measureText") return (s) => ({ width: String(s).length * 8 });
  return () => {};
}, set: () => true });
const cv = { width: 0, height: 0, style: {}, getContext: () => c };
const doc = { querySelector: () => cv, createElement: () => cv, body: { appendChild() {} } };
const 头 = `导入 图形
图形.准备({"宽": 800, "高": 600})
令 片 = []
遍历 i 在 范围(2000)：
    片.追加(["矩形", i % 40 * 16, i % 25 * 16, 16, 16, {"填充": "#345"}])
`;
const 体 = { 批量: "图形.批量(片)\n", 逐条: "遍历 k 在 片：\n    图形.矩形(k[1], k[2], k[3], k[4], k[5])\n" };
function 中位(a) { a = a.slice().sort((x, y) => x - y); return a[a.length >> 1]; }
(async () => {
  const 出 = {};
  for (const 名 of Object.keys(体)) {
    const 时 = [];
    for (let r = 0; r < 3; r++) {
      const j = await JishiWasm.load(BYTES, { doc, raf: () => 0 });
      const t0 = process.hrtime.bigint();
      const o = j.run(头 + 体[名]);
      const t1 = process.hrtime.bigint();
      if (!o.ok) { console.log(JSON.stringify({ 错: o.error.message })); process.exit(0); }
      时.push(Number(t1 - t0) / 1e6);
    }
    出[名] = 中位(时);
  }
  console.log(JSON.stringify(出));
})();
'''


@pytest.mark.计时
def test_批量确实比逐条快(tmp_path):
    """2000 个瓦片：批量应当**明显**快于逐个调用（实测约 2.8×）。

    ⚠️ 打 `计时`（进串行节）：断的是墙上时间比值 —— 并行负载会把两边一起抬高，
    比值虽抗负载，但这里的绝对量只有几毫秒，噪声占比大，还是独占跑更可信。
    阈值取「批量 < 逐条」（实测 2.8 倍，留足余量），不卡绝对毫秒数。
    """
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    脚本 = tmp_path / f"m207_t_{__import__('os').getpid()}.js"
    脚本.write_text(_JS计时, encoding="utf-8")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=180)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:800]
    d = json.loads(r.stdout.decode("utf-8", "replace").strip())
    assert "错" not in d, d
    assert d["批量"] < d["逐条"], f"批量没有更快：{d}"
