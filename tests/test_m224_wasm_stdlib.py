# -*- coding: utf-8 -*-
"""M224 判据：wasm 引擎的标准库必须与 Rust 宿主一致（发版闸门）。

## 这条判据的由来（用户实测）

0.3.5 的发行包里，CLI（Rust 宿主）与 `bin/jishi_wasm.wasm`（页面 / 窗口模式
都靠它）**不是同一版标准库**：wasm 停在 0.3.4，`操控` / `粒子` 导不进去、
`地图.寻路` 报「找不到这个函数」。而当时的判据只验「两份素材在不在」——
文件在、内容旧，看起来一切正常。

三条：
1. `tools/probe_wasm_stdlib.py` 零差异（逐模块逐函数对拍）；
2. `--dump-stdlib` 报的是**真实能力**：52 模块 / 600 函数（含内嵌库 `数据库`；
   0.3.5 那个口径少一个模块，正好遮掩了 wasm 落后的问题）；
3. wasm 里**真的能导入**用户报过的那三个（不靠清单，真跑一段）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUST = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")
WASM = ROOT / "site" / "jishi_wasm.wasm"
SHIM = ROOT / "site" / "jishi-wasm.js"

pytestmark = pytest.mark.skipif(
    not RUST.is_file() or not WASM.is_file() or shutil.which("node") is None,
    reason="要 jishi-rs + site 的 wasm 素材 + node（先 cargo build --release / "
           "python tools/build_wasm.py）",
)


def _跑(cmd, timeout=180):
    r = subprocess.run([str(c) for c in cmd], cwd=str(ROOT),
                       capture_output=True, timeout=timeout, input=b"")
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def test_wasm与rust的标准库逐条一致():
    码, 出 = _跑([sys.executable, ROOT / "tools" / "probe_wasm_stdlib.py"])
    assert 码 == 0, 出[-1200:]
    assert "逐条一致" in 出, 出


def test_dump_stdlib报的是真实能力():
    """52 模块 / 600 函数，且**含内嵌库模块 `数据库`**。

    ⚠️ 这条盯的是「口径」：0.3.5 的 `--dump-stdlib` 只报 51 / 579（漏了
    `数据库`），用户照它判断「宿主有什么」—— 掩盖了 wasm 落后一个版本的事。
    """
    码, 出 = _跑([RUST, "--dump-stdlib"])
    assert 码 == 0
    d = json.loads(出)
    assert len(d) == 54, f"应当是 54 个模块，实际 {len(d)}"
    assert sum(len(v) for v in d.values()) == 621   # M236：外部 +7（54 模块）
    assert "数据库" in d, "内嵌库模块 `数据库` 没报上来"


def test_wasm里用户报过的三个真的能用():
    """`导入 操控` / `导入 粒子` / `地图.寻路` —— 0.3.5 里全挂了的那三个。"""
    探针 = tmp_path_probe()
    码, 出 = _跑(["node", str(探针), str(SHIM), str(WASM)])
    assert 码 == 0, 出[-800:]
    结果 = json.loads(出.strip().splitlines()[-1])
    for 项 in 结果:
        assert 项["ok"], f"wasm 里「{项['源']}」失败：{项['错']}"


def tmp_path_probe() -> Path:
    d = ROOT / "build"
    d.mkdir(exist_ok=True)
    p = d / f"_m224_wasm_probe_{os.getpid()}.js"
    p.write_text(_探针, encoding="utf-8", newline="\n")
    return p


_探针 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const 假页 = { title: "", addEventListener() {}, querySelector: () => null };
globalThis.ipc = { postMessage: () => {} };
const 用例 = [
  '导入 操控\n打印(类型(操控))',
  '导入 粒子\n打印(类型(粒子))',
  '导入 地图\n打印(类型(地图.寻路))',
];
(async () => {
  const j = await JishiWasm.load(fs.readFileSync(process.argv[3]), { doc: 假页 });
  const 出 = [];
  for (const src of 用例) {
    const r = j.run(src);
    出.push({ 源: src.split("\n")[0], ok: r.ok, 错: r.ok ? "" : (r.error.message || "") });
  }
  console.log(JSON.stringify(出));
})();
'''


# ---------------------------------------------------------------------------
# 第 4 条：`图形.帧错误()` —— 帧循环出错时页面里读得到（M224 加的）
# ---------------------------------------------------------------------------

def test_帧循环出错页面里读得到(tmp_path):
    """帧循环里抛错 → 循环停、**错误能从 `图形.帧错误()` 读回来**。

    用户实测：页面模式下帧循环出错只进 `console.error`，桌面窗口里看不见，
    黑屏无从下手。这条钉住「错误留在引擎里、页面读得到」。
    """
    探针 = tmp_path / "帧探.js"
    探针.write_text(_帧探针, encoding="utf-8", newline="\n")
    (tmp_path / "源.jsh").write_text(_帧源, encoding="utf-8", newline="\n")
    码, 出 = _跑(["node", str(探针), str(SHIM), str(WASM), str(tmp_path / "源.jsh")])
    assert 码 == 0, 出[-800:]
    结果 = json.loads([l for l in 出.splitlines() if l.strip().startswith("{")][-1])
    assert 结果["帧码"] == 2, f"第二帧应当报错（码 2），实际 {结果['帧码']}"
    错 = 结果["错误"]
    assert "E2001" in 错 and "不能除以零" in 错, f"「帧错误」没拿到原因：{错}"
    # 出错即停：再喂一帧返回 3（没会话）
    assert 结果["再喂"] == 3, f"出错后登记没清（再喂返回 {结果['再喂']}）"
    # 空的时候也给空串（不是报错）
    assert 结果["没跑时空"] is True


_帧源 = r"""导入 图形

函数 初始化()：
    图形.准备({"宽": 320, "高": 200})

函数 每帧(号, 间隔)：
    如果 号 == 0：
        返回
    令 甲 = 1 / 0

图形.运行(初始化, 每帧)

函数 查错误()：
    打印(图形.帧错误())
"""

_帧探针 = r"""
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const 记录 = [];
function ctx() {
  const c = {
    fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "",
    globalAlpha: 1,
    setTransform(){}, translate(){}, rotate(){}, scale(){}, save(){}, restore(){},
    fillRect(){}, strokeRect(){}, clearRect(){}, beginPath(){}, closePath(){},
    arc(){}, ellipse(){}, moveTo(){}, lineTo(){}, fill(){}, stroke(){},
    fillText(){}, strokeText(){}, drawImage(){}, roundRect(){}, rect(){},
    setLineDash(){}, resetTransform(){},
    measureText: (t) => ({ width: String(t).length * 8,
      actualBoundingBoxAscent: 12, actualBoundingBoxDescent: 3 }),
    getImageData: (x, y) => ({ data: [x & 255, y & 255, 7, 255] }),
    putImageData(){},
    createLinearGradient: () => ({ addColorStop(){} }),
    createRadialGradient: () => ({ addColorStop(){} }),
    createPattern: () => null,
  };
  return c;
}
const cv = { width: 320, height: 200, getContext: () => ctx(),
  addEventListener(){}, style: {} };
const doc = { querySelector: (s) => (s === "#画布" ? cv : null),
  createElement: () => cv, body: { appendChild(){} }, title: "" };
globalThis.ipc = { postMessage: () => {} };
(async () => {
  const j = await JishiWasm.load(fs.readFileSync(process.argv[3]), { doc, raf: () => 0 });
  const r = j.run(fs.readFileSync(process.argv[4], "utf8"));
  if (!r.ok) { console.log(JSON.stringify({ 帧码: -1, 还在跑: false, 错误: r.error.message })); return; }
  // 没跑帧之前：错误应当是空的
  const 空 = j.call("查错误").stdout.trim();
  const 码1 = j.frame(0);      // 第一帧：号 0 → 返回
  const 码2 = j.frame(16);     // 第二帧：1/0 → 出错
  const 查 = j.call("查错误").stdout.trim();
  const 再喂 = j.frame(32).code;
  console.log(JSON.stringify({
    帧码: 码2.code, 再喂, 错误: 查, 没跑时空: 空 === "",
  }));
})();
"""
