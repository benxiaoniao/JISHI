# -*- coding: utf-8 -*-
"""M221 判据：窗口生命周期与常驻 + 通知。

| # | 这一站做的 | 判据 |
|---|---|---|
| ① | `窗口.打开({隐藏启动: 真})` + 显示 / 隐藏 / 可见 | 初始「不可见」→ 显示后「可见」→ 隐藏后「不可见」 |
| ② | `窗口.关闭到托盘(真)` | 在常驻循环里关 → 窗口看不见、进程还活着 |
| ③ | `窗口.打开({询问关闭: 真})` | 脚本请求关窗 → 页面收到「关闭前」且窗口**没关** |
| ④ | `窗口.常驻(每拍, 间隔)` —— M218 许诺的「窗口不白」 | 回调真的被逐拍调到 |
| ⑤ | `托盘.当("单击"/"双击")` | 脚本里明确报「只在页面里」；事件名进白名单 |
| ⑥ | `通知.发` / `通知.关闭` / `通知.当` | Windows 返回真；Python 返回假 + stderr 同一句；`当` 报错 |
| ⑦ | `窗口.最小化到托盘` / `全部关闭`（强制） | 收起来但进程活着；「全部关闭」跳过询问 |

⚠️ **真开窗的几条**要桌面环境：CI 上 `JISHI_NO_GUI=1` 跳过（同 M174/M175）。

📌 托盘点击 / 通知点击的**真点击**没法自动化（要鼠标），这一版判据只钉住
「事件名进了白名单 + 派发通道可用」；真点击在真机手册里人工过一遍
（`docs/实测记录.md`）。
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
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>M221</title></head>"
        "<body><p>M221</p>\n<script type=\"module\">\n"
        "const 源码 = `" + 源码 + "`;\n"
        "(async () => {\n"
        "  try {\n"
        "    const J = (await import('/__jishi/jishi-wasm.js')).default || window.JishiWasm;\n"
        "    const j = await J.load('/__jishi/jishi_wasm.wasm');\n"
        "    j.run(源码);\n"
        "  } catch (e) { /* 探针页，出错不做事 */ }\n"
        "})();\n"
        "</script></body></html>",
        encoding="utf-8", newline="\n")


def _跑基石(源: str, tmp_path: Path, exe=None, 环境=None, timeout=120):
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    命令 = [str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"]
    return _跑(命令 + ["程序.jsh"], cwd=tmp_path, timeout=timeout)


# ---------------------------------------------------------------------------
# ① 隐藏启动 / 显示 / 隐藏 / 可见
# ---------------------------------------------------------------------------

@要桌面
def test_隐藏启动与显隐可见(tmp_path):
    _页面(tmp_path, "导入 窗口\n")
    出 = _跑基石('''导入 窗口
令 号 = 窗口.打开({"页面": "页.html", "标题": "M221", "隐藏启动": 真})
打印("初始", 窗口.可见(号))
打印("显示", 窗口.显示(号))
打印("显示后", 窗口.可见(号))
打印("隐藏", 窗口.隐藏(号))
打印("隐藏后", 窗口.可见(号))
打印("收托盘", 窗口.最小化到托盘(号))
打印("在运行", 窗口.在运行(号))
窗口.关闭(号)
''', tmp_path, exe=RUST)[1]
    行 = [l for l in 出.splitlines() if l.strip()]
    assert 行 == ["初始 假", "显示 真", "显示后 真", "隐藏 真", "隐藏后 假",
                "收托盘 真", "在运行 真"], 出


# ---------------------------------------------------------------------------
# ② 关到托盘 + ④ 常驻
# ---------------------------------------------------------------------------

@要桌面
def test_关到托盘与常驻按拍(tmp_path):
    """`关闭到托盘(真)` 之后关窗 = **藏起来**；`常驻` 的回调逐拍真被调到。

    ⚠️ `窗口.关闭()` 是**排队事件**（走 EventLoopProxy），不进事件循环不生效
    —— 所以这两条必须在 `常驻` 里测，别在 `等()` 之前断言（M221 自检踩过）。
    """
    _页面(tmp_path, "导入 窗口\n")
    出 = _跑基石('''导入 窗口
令 状态 = {"拍": 0, "可见": 真, "活着": 假, "拍到": 假}
令 号 = 窗口.打开({"页面": "页.html", "标题": "M221"})
函数 每拍()：
    状态["拍"] = 状态["拍"] + 1
    如果 状态["拍"] == 2：
        窗口.关闭(号)
    如果 状态["拍"] == 4：
        状态["拍到"] = 真
        状态["可见"] = 窗口.可见(号)
        状态["活着"] = 窗口.在运行(号)
        窗口.关闭到托盘(假)
        窗口.关闭(号)
窗口.关闭到托盘(真)
窗口.常驻(每拍, 20)
打印("拍到", 状态["拍到"])
打印("关到托盘后可见", 状态["可见"])
打印("关到托盘后在运行", 状态["活着"])
''', tmp_path, exe=RUST)[1]
    行 = [l for l in 出.splitlines() if l.strip()]
    assert 行 == ["拍到 真", "关到托盘后可见 假", "关到托盘后在运行 真"], 出


# ---------------------------------------------------------------------------
# ③ 询问关闭
# ---------------------------------------------------------------------------

@要桌面
def test_询问关闭先问页面(tmp_path):
    """点 × / 脚本请求关窗 ⇒ 先派发「关闭前」，**窗口不自动关**。

    页面处理函数只改标题（表示它真的跑到了）；脚本随后读标题作证，
    再 `全部关闭()`（强制）收尾。
    """
    _页面(tmp_path, '''导入 窗口
函数 要关了()：
    窗口.设标题("页面收到关闭前")
窗口.当("关闭前", 要关了)
''')
    出 = _跑基石('''导入 窗口
令 状态 = {"拍": 0, "标题": "", "活着": 假, "兜底": 假}
令 号 = 窗口.打开({"页面": "页.html", "标题": "M221 询问", "询问关闭": 真})
函数 每拍()：
    状态["拍"] = 状态["拍"] + 1
    如果 状态["拍"] == 150：
        窗口.关闭(号)
    如果 状态["拍"] == 170：
        状态["标题"] = 窗口.读标题(号)
        状态["活着"] = 窗口.在运行(号)
        窗口.全部关闭()
    如果 状态["拍"] == 500：
        状态["兜底"] = 真
        窗口.全部关闭()
窗口.常驻(每拍, 20)
打印("标题", 状态["标题"])
打印("询问后还活着", 状态["活着"])
打印("走到兜底没有", 状态["兜底"])
''', tmp_path, exe=RUST, timeout=180)[1]
    assert "标题 页面收到关闭前" in 出, 出
    assert "询问后还活着 真" in 出, 出
    assert "走到兜底没有 假" in 出, 出


# ---------------------------------------------------------------------------
# ⑤ 托盘.当
# ---------------------------------------------------------------------------

def test_托盘当在脚本里报只在页面里(tmp_path):
    源 = '导入 托盘\n函数 处理()：\n    打印("点了")\n托盘.当("单击", 处理)\n'
    for 名, 命令 in (("rust", [str(RUST)]),
                    ("python", [sys.executable, "-m", "oracle.jishi.cli"])):
        码, 出 = _跑基石(源, tmp_path, exe=(RUST if 名 == "rust" else None))
        assert 码 != 0, f"{名} 竟然跑成功了：{出[:300]}"
        assert "托盘.当" in 出 and "页面" in 出, (名, 出[:400])


def test_托盘当只认单击双击(tmp_path):
    """事件名写错要**报错**（静默不生效的订阅最难查）。"""
    源 = '导入 托盘\n函数 处理()：\n    打印("点了")\n托盘.当("三击", 处理)\n'
    码, 出 = _跑基石(源, tmp_path, exe=RUST)
    assert 码 != 0
    assert "只认「单击」「双击」" in 出, 出[:400]


def test_事件白名单含新事件(tmp_path):
    """`窗口.当` 的四个新事件必须能登记（写进页面的那段代码真的跑得通）。"""
    if not WASM.is_file():
        pytest.skip("还没构建 wasm（跑 tools/build_wasm.py）")
    for 事件 in ("托盘单击", "托盘双击", "关闭前", "通知点击"):
        (tmp_path / "探.js").write_text(_探针, encoding="utf-8", newline="\n")
        (tmp_path / "源.jsh").write_text(
            f'导入 窗口\n函数 处理(值)：\n    打印(值)\n窗口.当("{事件}", 处理)\n',
            encoding="utf-8", newline="\n")
        码, 出 = _跑(["node", tmp_path / "探.js", SHIM, WASM, tmp_path / "源.jsh"])
        assert 码 == 0, 出[:400]
        行 = [l for l in 出.splitlines() if l.strip().startswith("{")]
        assert 行, 出[:400]
        结果 = json.loads(行[-1])
        assert 结果["ok"], f"「{事件}」登记失败：{结果['错误']}"


# ---------------------------------------------------------------------------
# ⑥ 通知
# ---------------------------------------------------------------------------

def test_通知发在Python宿主返回假并说明(tmp_path):
    """没有通知能力的宿主：**返回假 + stderr 一句**（不报错、不静默）。"""
    码, 出 = _跑基石('导入 通知\n打印(通知.发("t", "b"))\n打印(通知.关闭())\n',
                    tmp_path)
    assert 码 == 0, 出[:400]
    行 = [l for l in 出.splitlines() if l.strip() and "通知没发出去" not in l]
    assert 行 == ["假", "假"], 出
    assert "通知没发出去：这个宿主没有系统通知能力" in 出, 出[:400]


@要桌面
def test_通知发在Windows宿主返回真(tmp_path):
    码, 出 = _跑基石('导入 通知\n打印(通知.发("基石", "M221 判据"))\n',
                    tmp_path, exe=RUST)
    assert 码 == 0, 出[:400]
    assert 出.strip().splitlines()[-1] == "真", 出


def test_通知当报只在页面里(tmp_path):
    源 = '导入 通知\n函数 处理()：\n    打印("点")\n通知.当("点击", 处理)\n'
    码, 出 = _跑基石(源, tmp_path, exe=RUST)
    assert 码 != 0
    assert "只在**页面里**有效" in 出 or "页面" in 出, 出[:400]


# ---------------------------------------------------------------------------
# ⑦ 没有窗口能力时的报错口径
# ---------------------------------------------------------------------------

def test_Python宿主的生命周期函数报做不到(tmp_path):
    for 调用, 关键 in (
        ("窗口.显示()", "没有原生窗口能力"),
        ("窗口.可见()", "没有原生窗口能力"),
        ("窗口.关闭到托盘(真)", "没有原生窗口能力"),
        ("窗口.常驻(打印)", "没有原生窗口能力"),
    ):
        码, 出 = _跑基石(f'导入 窗口\n{调用}\n', tmp_path)
        assert 码 != 0, f"{调用} 竟然成功了：{出[:300]}"
        assert 关键 in 出, (调用, 出[:400])


_探针 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const 假页 = { title: "", addEventListener() {}, querySelector: () => null };
globalThis.ipc = { postMessage: () => {} };
(async () => {
  const j = await JishiWasm.load(fs.readFileSync(process.argv[3]), { doc: 假页 });
  const r = j.run(fs.readFileSync(process.argv[4], "utf8"));
  console.log(JSON.stringify({ ok: r.ok, 错误: r.ok ? "" : (r.error.title + " " + r.error.message),
                                stdout: "" }));
})();
'''
