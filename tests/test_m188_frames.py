# -*- coding: utf-8 -*-
"""M188 · 帧耗时：一帧画「200 瓦片 + 50 精灵」要多久。

## 为什么要量

M186 定了门槛（16 ms = 60 帧/秒，33 ms = 30 帧/秒），但**一直没在真实规模上量过**。
这一站补齐：拿 `瓦片.画` + `精灵.画` 这两个「每帧调用次数最多」的东西压一压。

## 量的是什么、不是什么

- 量的是**引擎侧**（wasm 里跑 `每帧` + 回吐绘制指令）的开销，
  用假 canvas 把 `drawImage` 记成计数 —— **不含真正的光栅化**（那部分在 GPU/浏览器里）；
- 所以这条是**下限**：真机上只会更慢，不会更快。门槛按 16 ms 卡，留了余量。

📌 打 `计时`（进串行节）：它断言的是**墙上时间**，并行负载会把数字抬成假红。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT                                          # noqa: E402

WASM = ROOT / "site" / "jishi_wasm.wasm"
SHIM = ROOT / "wasm" / "jishi-wasm.js"
素材 = ROOT / "examples" / "desktop"

_JS = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);

let 画了 = 0;
function ctx() {
  const c = { fillStyle: "", strokeStyle: "", lineWidth: 1, font: "",
              textAlign: "left", globalAlpha: 1 };
  const 无 = () => {};
  for (const m of ["save", "restore", "setTransform", "translate", "rotate", "scale",
    "beginPath", "closePath", "moveTo", "lineTo", "fill", "stroke", "clearRect",
    "strokeRect", "ellipse", "roundRect", "arc", "fillText"]) c[m] = 无;
  c.measureText = () => ({ width: 8 });
  c.getImageData = () => ({ data: [0, 0, 0, 255] });
  c.fillRect = 无;
  c.drawImage = () => { 画了 += 1; };
  return c;
}
const cv = { width: 800, height: 480, style: {}, clientWidth: 800, clientHeight: 480, getContext: ctx };
const doc = { querySelector: () => cv, createElement: () => cv, body: { appendChild: () => {} } };
global.Image = class { set src(v) { if (this.onload) this.onload(); } };
global.fetch = () => Promise.resolve({ ok: true, text: () => Promise.resolve("") });

// 25 列 × 8 行实心瓦片 = 200 块（视口正好 25×15 格，所以不会被裁掉）
const SRC = `导入 图形
导入 瓦片
导入 精灵

令 格 = 32
令 列数 = 25
令 行数 = 15
令 状态 = {"图": 0, "精": 0}

函数 初始化()：
    图形.准备({"宽": 800, "高": 480, "背景": "#0b1020"})
    令 行们 = []
    令 r = 0
    当 r < 行数：
        令 行 = []
        令 c = 0
        当 c < 列数：
            如果 r >= 7：
                行.追加(1)
            否则：
                行.追加(-1)
            c = c + 1
        行们.追加(行)
        r = r + 1
    状态["图"] = 瓦片.载入({"图集": 图形.读图("瓦片图.png"), "瓦片宽": 格, "瓦片高": 格, "行们": 行们})
    状态["精"] = 精灵.新建(图形.读图("小人.png"), {"宽": 32, "高": 32, "列数": 4, "行数": 1})

函数 每帧(号, 间隔毫秒)：
    图形.清屏("#0b1020")
    图形.设相机(0.0, 0.0)
    瓦片.画(状态["图"], {"x": 0.0, "y": 0.0, "宽": 800, "高": 480})
    令 i = 0
    当 i < 50：
        精灵.画(状态["精"], 20 + i * 15, 40 + (i % 7) * 40, {"透明度": 0.9})
        i = i + 1
    图形.文本("压测", 14, 26, {"字号": 18, "填充": "#fff"})

图形.运行(初始化, 每帧)
`;

(async () => {
  const j = await JishiWasm.load(BYTES, { doc, store: null, raf: () => 0 });
  const r = j.run(SRC);
  if (!r.ok) { console.error(JSON.stringify(r.error)); process.exit(2); }
  await new Promise((r) => setTimeout(r, 30));

  const 样本 = [];
  let 基线 = 0;
  for (let 帧 = 1; 帧 <= 180; 帧++) {
    const t0 = process.hrtime.bigint();
    j.frame(帧 * 16);
    const t1 = process.hrtime.bigint();
    if (帧 === 30) 基线 = 画了;
    if (帧 > 30) 样本.push(Number(t1 - t0) / 1e6);   // 前 30 帧是热身（JIT/图就绪）
  }
  样本.sort((a, b) => a - b);
  const 取 = (q) => 样本[Math.min(样本.length - 1, Math.floor(样本.length * q))];
  console.log(JSON.stringify({
    帧数: 样本.length,
    中位: +取(0.5).toFixed(3),
    p95: +取(0.95).toFixed(3),
    最大: +样本[样本.length - 1].toFixed(3),
    每帧画几笔: Math.round((画了 - 基线) / 150),
  }));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


@pytest.fixture(scope="module")
def 帧耗时(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    d = tmp_path_factory.mktemp("m188帧")
    # 素材用真实的两张 PNG（假 Image 立刻「加载完」，所以每次都是真去画）
    for 名 in ("瓦片图.png", "小人.png"):
        shutil.copyfile(素材 / 名, d / 名)
    脚本 = d / "frames.js"
    脚本.write_text(_JS, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=300, cwd=str(d))
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, 出[-1500:]
    return json.loads(出.strip().splitlines()[-1])


@pytest.mark.计时
def test_200瓦片加50精灵一帧不到16毫秒(帧耗时):
    """60 帧/秒的预算是 16.7 ms。这条卡 16 ms，并且**把数字打出来**。

    📌 只量引擎侧（不含真光栅化）⇒ 这是**下限**；真机会更慢，所以门槛留了余量。
    """
    中位, p95 = 帧耗时["中位"], 帧耗时["p95"]
    print(f"\n  一帧（200 瓦片 + 50 精灵 + 1 行文字）：中位 {中位} ms · "
          f"p95 {p95} ms · 最大 {帧耗时['最大']} ms · 每帧 {帧耗时['每帧画几笔']} 笔绘制")
    assert 帧耗时["每帧画几笔"] >= 240, \
        f"每帧只画了 {帧耗时['每帧画几笔']} 笔 —— 压测没真跑起来（应当是 200+50+1 上下）"
    assert 中位 < 16.0, f"一帧中位 {中位} ms，超过 60 帧/秒的预算（16 ms）"
    assert p95 < 33.0, f"p95 {p95} ms，抖到 30 帧/秒以下了"
