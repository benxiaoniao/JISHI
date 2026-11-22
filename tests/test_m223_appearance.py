# -*- coding: utf-8 -*-
"""M223 判据：产品化外观（无边框 / 最小尺寸 / 拖动 / 主题 / 跨平台剪贴板）。

| # | 这一站做的 | 判据 |
|---|---|---|
| ① | `打开({无边框: 真, 可调整: 假, 最小宽/最小高})` | 真开窗不崩；`读几何` 报出尺寸 |
| ② | `窗口.设主题("浅"/"深"/"跟随")` | **页面 `prefers-color-scheme` 真的变**（浅=假 → 深=真） |
| ③ | `窗口.拖动(句柄)` | 真窗口上调用不报错（拖动本身要鼠标，人工手册过） |
| ④ | `浏览器.匹配(媒体查询)` | 页面里可读 `(prefers-color-scheme: dark)` |
| ⑤ | 主题写错要报错 | 两宿主都**值错误**，不静默当「跟随」 |
| ⑥ | Python 宿主：拖动 / 设主题 做不到 | 明确报「没有原生窗口能力」 |
| ⑦ | `剪贴板` mac / Linux | mac 往返；Linux **有工具往返、没工具返回假且不报错** |
| ⑧ | `剪贴板` Python 宿主保持「故意不做」 | 报错（判据基准不碰系统剪贴板，M185 的原则） |

⚠️ ①②③ 要桌面（`JISHI_NO_GUI=1` 跳过）；⑦ 只在 mac/Linux 跑（本机跳过）。

📌 **最小尺寸对 `窗口.设大小` 不生效**（系统 `WM_GETMINMAXINFO` 只限制用户拖边框）
—— 这是 Windows/tao 的行为，不是 bug；文档与卡里写清。判据不假装它能钳住代码设的尺寸。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUST = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")
WASM = ROOT / "site" / "jishi_wasm.wasm"
SHIM = ROOT / "site" / "jishi-wasm.js"

无界面 = bool(os.environ.get("JISHI_NO_GUI"))
要桌面 = pytest.mark.skipif(
    无界面 or not sys.platform.startswith("win"),
    reason="真开窗要桌面环境（CI 设 JISHI_NO_GUI=1）",
)

pytestmark = pytest.mark.skipif(
    not RUST.is_file(),
    reason="没有发行版二进制（先 cargo build --release）",
)


def _跑(cmd, cwd=None, timeout=180):
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       capture_output=True, timeout=timeout, input=b"")
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _页面(tmp_path: Path, 源码: str, 名: str = "页.html") -> None:
    (tmp_path / 名).write_text(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>M223</title></head>"
        "<body><p>M223</p>\n<script type=\"module\">\n"
        "const 源码 = `" + 源码 + "`;\n"
        "(async () => {\n"
        "  try {\n"
        "    const J = (await import('/__jishi/jishi-wasm.js')).default || window.JishiWasm;\n"
        "    const j = await J.load('/__jishi/jishi_wasm.wasm');\n"
        "    j.run(源码);\n"
        "  } catch (e) { /* 探针页 */ }\n"
        "})();\n"
        "</script></body></html>",
        encoding="utf-8", newline="\n")


def _跑基石(源: str, tmp_path: Path, exe=None, timeout=180):
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    命令 = [str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"]
    return _跑(命令 + ["程序.jsh"], cwd=tmp_path, timeout=timeout)


# ---------------------------------------------------------------------------
# ①②③ 无边框 / 窗口 / 拖动 / 主题
# ---------------------------------------------------------------------------

@要桌面
def test_主题切换让页面配色真的变(tmp_path):
    """`窗口.设主题("深")` 之后，页面里 `prefers-color-scheme: dark` 从假变真。

    页面把 `浏览器.匹配(...)` 的结果写进**原生标题**（`窗口.设标题` 走 IPC），
    脚本再用 `窗口.读标题` 读回来 —— 这条链证明的是「主题真的传到了页面」，
    不是「函数返回了真」。
    """
    _页面(tmp_path, '''导入 窗口
导入 浏览器
函数 报配色()：
    窗口.设标题("深=" + 文本(浏览器.匹配("(prefers-color-scheme: dark)")))
窗口.当("调用", 报配色)
报配色()
''')
    出 = _跑基石('''导入 窗口
令 状态 = {"拍": 0, "浅": "", "深": "", "几何": 空}
令 号 = 窗口.打开({"页面": "页.html", "标题": "M223",
               "无边框": 真, "可调整": 假, "最小宽": 400, "最小高": 300})
函数 每拍()：
    状态["拍"] = 状态["拍"] + 1
    如果 状态["拍"] == 1：
        状态["几何"] = 窗口.读几何(号)
    如果 状态["拍"] == 120：
        窗口.设主题("浅", 号)
    如果 状态["拍"] == 140：
        窗口.调用页面(号, "调用", "")
    如果 状态["拍"] == 160：
        状态["浅"] = 窗口.读标题(号)
        窗口.设主题("深", 号)
    如果 状态["拍"] == 180：
        窗口.调用页面(号, "调用", "")
    如果 状态["拍"] == 200：
        状态["深"] = 窗口.读标题(号)
        窗口.全部关闭()
窗口.常驻(每拍, 20)
打印("几何有", 状态["几何"]["宽"] > 0)
打印("浅色", 状态["浅"])
打印("深色", 状态["深"])
''', tmp_path, exe=RUST)[1]
    assert "几何有 真" in 出, 出
    # ⚠️ 不假设系统初始配色（本机就是深色）：**主动设浅 → 假，再设深 → 真**
    assert "浅色 深=假" in 出, 出
    assert "深色 深=真" in 出, 出


@要桌面
def test_拖动与跟随主题不报错(tmp_path):
    """`拖动` / `设主题("跟随")` 在真窗口上调用不报错（拖动要鼠标，人工过）。"""
    _页面(tmp_path, "导入 窗口\n")
    出 = _跑基石('''导入 窗口
令 状态 = {"拍": 0, "拖动": 假, "跟随": 假}
令 号 = 窗口.打开({"页面": "页.html", "标题": "M223"})
函数 每拍()：
    状态["拍"] = 状态["拍"] + 1
    如果 状态["拍"] == 2：
        状态["拖动"] = 窗口.拖动(号)
        状态["跟随"] = 窗口.设主题("跟随", 号)
    如果 状态["拍"] == 4：
        窗口.全部关闭()
窗口.常驻(每拍, 20)
打印("拖动", 状态["拖动"])
打印("跟随", 状态["跟随"])
''', tmp_path, exe=RUST)[1]
    assert "拖动" in 出 and "跟随 真" in 出, 出


# ---------------------------------------------------------------------------
# ⑤ 主题错字
# ---------------------------------------------------------------------------

def test_主题写错要报值错误(tmp_path):
    源 = '导入 窗口\n窗口.设主题("蓝")\n'
    for 名, 命令 in (("rust", [str(RUST)]),
                    ("python", [sys.executable, "-m", "oracle.jishi.cli"])):
        码, 出 = _跑基石(源, tmp_path, exe=(RUST if 名 == "rust" else None))
        assert 码 != 0, f"{名} 竟然跑成功了：{出[:300]}"
        assert "只认「浅」「深」「跟随」" in 出, (名, 出[:400])


# ---------------------------------------------------------------------------
# ⑥ Python 宿主做不到
# ---------------------------------------------------------------------------

def test_Python宿主拖动与主题报做不到(tmp_path):
    for 调用 in ("窗口.拖动()", "窗口.设主题(\"深\")"):
        码, 出 = _跑基石(f'导入 窗口\n{调用}\n', tmp_path)
        assert 码 != 0, f"{调用} 竟然成功了：{出[:300]}"
        assert "没有原生窗口能力" in 出, (调用, 出[:400])


# ---------------------------------------------------------------------------
# ④ 浏览器.匹配（wasm 探针）
# ---------------------------------------------------------------------------

def test_浏览器匹配在页面里可用(tmp_path):
    if not WASM.is_file():
        pytest.skip("还没构建 wasm（跑 tools/build_wasm.py）")
    (tmp_path / "探.js").write_text(_探针, encoding="utf-8", newline="\n")
    (tmp_path / "源.jsh").write_text(
        '导入 浏览器\n打印(浏览器.匹配("(prefers-color-scheme: dark)"))\n',
        encoding="utf-8", newline="\n")
    码, 出 = _跑(["node", tmp_path / "探.js", SHIM, WASM, tmp_path / "源.jsh"])
    assert 码 == 0, 出[:400]
    行 = [l for l in 出.splitlines() if l.strip().startswith("{")]
    assert 行, 出[:400]
    结果 = json.loads(行[-1])
    assert 结果["ok"], 结果["错误"]
    assert 结果["stdout"].strip() == "假", 结果


# ---------------------------------------------------------------------------
# ⑦ 剪贴板 mac / Linux
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="Windows 的剪贴板由 test_m185 覆盖")
def test_剪贴板在mac与linux的口径(tmp_path):
    """mac 一定往返成功；Linux **有工具就往返、没工具返回假且不报错**。"""
    源 = ('导入 剪贴板\n'
          '令 成 = 剪贴板.写文本("M223 剪贴板自检")\n'
          '打印(成)\n'
          '如果 成：\n'
          '    打印(剪贴板.读文本())\n')
    码, 出 = _跑基石(源, tmp_path, exe=RUST)
    assert 码 == 0, f"没工具时不许报错：{出[-600:]}"
    行 = [l for l in 出.splitlines() if l.strip() and "剪贴板没写上" not in l]
    assert 行 and 行[0] in ("真", "假"), 出
    if 行[0] == "真":
        assert 行[1] == "M223 剪贴板自检", 出
    else:
        # 返回假时**必须**说明原因（不静默）
        assert "剪贴板没写上" in 出, 出


# ---------------------------------------------------------------------------
# ⑧ Python 宿主的剪贴板：故意不做
# ---------------------------------------------------------------------------

def test_Python宿主剪贴板保持故意不做(tmp_path):
    码, 出 = _跑基石('导入 剪贴板\n剪贴板.写文本("x")\n', tmp_path)
    assert 码 != 0
    assert "这个宿主没有剪贴板能力" in 出, 出[:400]


_探针 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const out = [];
const 假页 = { title: "", addEventListener() {}, querySelector: () => null };
globalThis.ipc = { postMessage: () => {} };
globalThis.matchMedia = (q) => ({ matches: false, media: q });
(async () => {
  const j = await JishiWasm.load(fs.readFileSync(process.argv[3]), { doc: 假页 });
  const r = j.run(fs.readFileSync(process.argv[4], "utf8"));
  console.log(JSON.stringify({
    ok: r.ok,
    错误: r.ok ? "" : (r.error.title + " " + r.error.message),
    stdout: (r.stdout || ""),
  }));
})();
'''
