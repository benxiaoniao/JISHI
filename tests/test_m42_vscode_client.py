# -*- coding: utf-8 -*-
"""M42：VS Code 扩展的语言客户端 + 端到端自检。

这一轮除了把六项能力做进语言服务，还顺手把 `editors/vscode-jishi` 从
「只有语法高亮」升级成了**语言客户端**——因为 M42 的验收标准写的是
「每条能力在 VS Code 里手工演一遍」，没有客户端就演不了。

三块测试：

1. **扩展静态检查**：`package.json` 有 `main`、激活事件与配置项都在；
   三个 JS 文件语法能过（`node --check`）。
2. **端到端自检**（重点）：跑 `editors/vscode-jishi/verify-lsp.js`——
   它用**扩展同一份传输代码**（`lsp-transport.js`）真的起一个
   `jishi lsp` 子进程，走完 initialize → didOpen → 逐项请求，断言每一项
   都真的返回了东西。25 项断言，任何一项挂了都是「编辑器里会没反应」的
   前兆，所以钉进回归。
3. **映射表别写错**：LSP 与 VS Code 的枚举数值**不通用**（例如 LSP 的
   Function=3 对应 VS Code 的 Function=2），注释里写清楚，测试里盯住
   「几个关键映射确实存在」。

Node 不在则跳过（与 `tests/test_m31_node_stdlib.py` 同一约定）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "editors" / "vscode-jishi"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="本机无 Node.js，跳过扩展测试")


def _node(*args: str, env: "dict | None" = None, timeout: int = 120):
    import os

    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([NODE, *args], cwd=str(EXT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=e, timeout=timeout)


# ---------------------------------------------------------------------------
# 1. 扩展静态检查
# ---------------------------------------------------------------------------

def test_package_json_has_main_and_activation():
    pkg = json.loads((EXT / "package.json").read_text(encoding="utf-8"))
    assert pkg.get("main") == "./extension.js", "没有 main，扩展激活不了"
    assert "onLanguage:jishi" in pkg.get("activationEvents", [])
    assert EXT.joinpath("extension.js").exists()


def test_package_json_declares_server_settings():
    """服务端路径必须可配——用户装完 jishi 没进 PATH 时唯一的出路。"""
    pkg = json.loads((EXT / "package.json").read_text(encoding="utf-8"))
    props = pkg["contributes"]["configuration"]["properties"]
    assert "jishi.server.command" in props
    assert props["jishi.server.command"]["default"] == "jishi"
    assert props["jishi.server.args"]["default"] == ["lsp"]
    # trace 开关必须真的被实现（声明了却不生效比不声明更糟）
    assert "jishi.server.trace" in props
    assert "server.trace" in (EXT / "extension.js").read_text(encoding="utf-8")


def test_extension_js_syntax():
    for name in ("extension.js", "lsp-transport.js", "verify-lsp.js"):
        r = _node("--check", name)
        assert r.returncode == 0, f"{name} 语法错：{r.stderr[:300]}"


def test_extension_never_uses_languageclient_dependency():
    """有意不引 `vscode-languageclient`（见 lsp-transport.js 顶部说明）。

    这条同时防「有人顺手 npm install 让扩展变重」——依赖一进来，
    `.vsix` 体积会翻好几倍，而我们要的只是分帧收发。
    """
    pkg = json.loads((EXT / "package.json").read_text(encoding="utf-8"))
    assert "dependencies" not in pkg, "扩展不该有运行时依赖"
    assert (EXT / "lsp-transport.js").exists()


def test_enum_maps_cover_what_server_sends():
    """LSP 与 VS Code 的枚举数值不同，映射表要覆盖服务端会发的取值。"""
    src = (EXT / "extension.js").read_text(encoding="utf-8")
    for kind in ("2:", "3:", "6:", "7:", "9:", "14:"):      # 补全
        assert kind in src, f"补全映射缺 LSP kind {kind}"
    for kind in ("2:", "5:", "12:", "13:"):                 # 符号（与服务端 SYM_* 对齐）
        assert kind in src, f"符号映射缺 LSP kind {kind}"
    for sev in ("1:", "2:", "3:", "4:"):                    # 诊断严重级别
        assert sev in src, f"诊断严重级别缺 {sev}"


# ---------------------------------------------------------------------------
# 2. 端到端自检（真的起一个语言服务子进程）
# ---------------------------------------------------------------------------

def test_lsp_end_to_end_self_check():
    """跑 `verify-lsp.js`：25 项断言，任何一项挂都是「编辑器里没反应」的前兆。"""
    r = _node("verify-lsp.js", env={
        "JISHI_BIN": sys.executable,
        "JISHI_ARGS": "-m jishi.cli lsp",
    }, timeout=180)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"自检未通过：\n{out[-3000:]}"
    assert "失败 0 项" in out, out[-3000:]
    # 六项新能力都要在自检里被真的点过一遍
    for 名称 in ("格式化有应答", "重命名改了定义处", "给出了「改成 长度」的快速修复",
                "签名提示给出了形参表", "工作区符号扫到了磁盘上的别的文件",
                "语义高亮数据合法"):
        assert 名称 in out, f"自检没覆盖「{名称}」"


def test_self_check_detects_broken_server():
    """自检脚本本身要能发现问题——喂一个不存在的可执行文件必须失败。

    不然它就是「永远绿」的假护栏。
    """
    r = _node("verify-lsp.js", "这个可执行文件不存在",
              env={"JISHI_ARGS": ""}, timeout=60)
    assert r.returncode != 0, "自检对不存在的服务器居然通过了"
