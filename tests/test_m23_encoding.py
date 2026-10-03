# -*- coding: utf-8 -*-
"""编码健壮性：中文源码经 stdin / 子进程 / MCP 通道时不能变乱码。

背景（2026-09-12 发 v0.1.4 前实测发现）：打包版 PyInstaller 产物不继承
``PYTHONUTF8``，``sys.stdin`` 会按 Windows 本地代码页（GBK）解码，中文源码
变成 ``鎵撳嵃`` 这类乱码；且乱码再触发词法错误时，报错渲染会因孤立代理字符
抛 ``UnicodeEncodeError``，最终用户看到的是 PyInstaller 的英文堆栈。

这组测试锁住修复，避免回归。
"""

import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import cli                              # noqa: E402
from jishi.errors import JishiError, _safe_text     # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# _read_stdin：按字节读 + UTF-8 解码
# ---------------------------------------------------------------------------

class _FakeStdin:
    """模拟「有 buffer 的文本流」：buffer 里是原始字节，文本层用错误编码。"""

    def __init__(self, data: bytes, text_encoding: str = "gbk"):
        self.buffer = io.BytesIO(data)
        self.encoding = text_encoding

    def read(self) -> str:                        # 模拟按本地代码页解码
        return self.buffer.getvalue().decode(self.encoding, "replace")


def test_read_stdin_decodes_utf8_bytes_not_locale(monkeypatch):
    """核心回归：即使文本层是 GBK，也要按 UTF-8 读出来。"""
    src = '打印("你好")\n'
    fake = _FakeStdin(src.encode("utf-8"), text_encoding="gbk")
    monkeypatch.setattr(sys, "stdin", fake)

    got = cli._read_stdin()
    assert got == src
    assert "打印" in got                        # 不是「鎵撳嵃」
    assert "\ufffd" not in got                   # 没有替换字符


def test_read_stdin_falls_back_without_buffer(monkeypatch):
    """测试/嵌入场景 stdin 是 StringIO（无 buffer）时要能用。"""
    monkeypatch.setattr(sys, "stdin", io.StringIO("令 甲 = 1\n"))
    assert cli._read_stdin() == "令 甲 = 1\n"


def test_read_stdin_replaces_invalid_bytes(monkeypatch):
    """非法 UTF-8 字节 → U+FFFD（不抛异常），让词法器给中文报错。"""
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"\xff\xfe bad"))
    got = cli._read_stdin()
    assert "\ufffd" in got


def test_format_stdin_roundtrip_with_gbk_text_layer(monkeypatch):
    """端到端：GBK 文本层下 `jishi 格式化 --stdin` 仍正确格式化中文。"""
    fake = _FakeStdin("令  甲=1\n".encode("utf-8"), text_encoding="gbk")
    monkeypatch.setattr(sys, "stdin", fake)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["格式化", "--stdin"])
    assert code == 0
    assert out.getvalue() == "令 甲 = 1\n"


def test_run_stdin_with_chinese_via_gbk_text_layer(monkeypatch):
    """端到端：GBK 文本层下 `jishi --stdin` 能正确执行中文源码。"""
    fake = _FakeStdin('打印("你好")\n'.encode("utf-8"), text_encoding="gbk")
    monkeypatch.setattr(sys, "stdin", fake)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--stdin"])
    assert code == 0
    assert "你好" in out.getvalue()


# ---------------------------------------------------------------------------
# 报错渲染的编码健壮性
# ---------------------------------------------------------------------------

def test_safe_text_passes_normal_text_through():
    assert _safe_text("正常中文 abc") == "正常中文 abc"


def test_safe_text_cleans_lone_surrogates():
    bad = "乱码\udca4文本"
    fixed = _safe_text(bad)
    fixed.encode("utf-8")                       # 不抛异常即通过
    assert "\udca4" not in fixed
    assert "文本" in fixed


def test_error_render_never_raises_on_surrogate_source_line():
    """含孤立代理字符的源码行，渲染报错时不能崩。"""
    e = JishiError("无法识别的字符", line=1, col=2,
                   source_line="浠\udca4 鐢\udcb2=1", filename="<stdin>")
    text = e.render()
    text.encode("utf-8")                        # 不抛异常
    assert "无法识别的字符" in text


def test_error_render_survives_surrogate_in_message_and_hint():
    e = JishiError("消息\udca4", line=1, col=1, source_line="x",
                   hint="提示\udcb2")
    text = e.render()
    text.encode("utf-8")
    assert "提示" in text


# ---------------------------------------------------------------------------
# 入口脚本：三个流都要设 UTF-8
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["entry_cli.py", "entry_mcp.py"])
def test_entry_scripts_configure_all_three_streams(name):
    """入口脚本必须管 stdin——只改 stdout/stderr 就是本次那个 bug。"""
    src = (ROOT / "tools" / name).read_text(encoding="utf-8")
    assert "sys.stdin" in src, f"{name} 没处理 stdin"
    assert "sys.stdout" in src
    assert "sys.stderr" in src
    assert 'errors="replace"' in src, f"{name} 未给流加 errors=replace"


def test_entry_scripts_source_is_importable_without_side_effect():
    """入口脚本应能在「非主程序」下安全导入（不真的跑 main）。"""
    import importlib.util

    for name in ("entry_cli.py", "entry_mcp.py"):
        spec = importlib.util.spec_from_file_location(
            f"_probe_{name}", ROOT / "tools" / name)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)             # 不应抛异常
        assert hasattr(mod, "main")


# ---------------------------------------------------------------------------
# MCP 通道：子进程必须显式 UTF-8
# ---------------------------------------------------------------------------

def test_mcp_subprocess_uses_utf8_encoding():
    """MCP 的 _run_subprocess 必须显式 encoding='utf-8'。

    只开文本模式而不指定编码时，Python 会按本地代码页（GBK）编码子进程输入，
    中文源码在管道里就坏了。
    """
    src = (ROOT / "jishi" / "mcp_server.py").read_text(encoding="utf-8")
    assert 'encoding="utf-8"' in src
    # 裸的文本模式（不给编码）不允许出现在 subprocess.run 调用里
    assert "subprocess.run(\n            args, input=script, capture_output=True, text=True" \
        not in src


def test_mcp_server_reads_stdin_as_bytes():
    src = (ROOT / "jishi" / "mcp_server.py").read_text(encoding="utf-8")
    assert '_iter_stdin_lines' in src
    assert 'getattr(sys.stdin, "buffer", None)' in src


def test_mcp_roundtrip_chinese_over_stdio():
    """真起一个 MCP 服务进程，走 stdio 发中文源码，结果不能乱码。"""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "run_script",
                    "arguments": {"script": '打印("你好，基石")\n'}}},
    ]
    payload = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs)
    proc = subprocess.run(
        [sys.executable, "-m", "jishi.mcp_server"],
        input=payload.encode("utf-8"), capture_output=True,
        timeout=90, cwd=str(ROOT))

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[:400]
    out = proc.stdout.decode("utf-8", "replace")
    assert out.strip(), "MCP 服务没有任何输出"
    # 中文必须原样出现在响应里，不能是乱码
    assert "你好，基石" in out
    assert "鎵撳嵃" not in out
    # 子进程必须真的跑起来了（不是「子进程异常退出」）
    assert "子进程异常退出" not in out


# ---------------------------------------------------------------------------
# 打包版（frozen）CLI 定位
# ---------------------------------------------------------------------------

def test_cli_command_uses_python_in_source_mode(monkeypatch):
    """源码运行时用 [python, -m, jishi.cli]。"""
    from jishi import mcp_server

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    cmd = mcp_server._cli_command()
    assert cmd == [sys.executable, "-m", "jishi.cli"]


def test_cli_command_prefers_sibling_jishi_when_frozen(tmp_path, monkeypatch):
    """打包后 sys.executable 是自己，必须改用发行目录里同级的 jishi。

    发行版布局：bin/jishi/jishi.exe 与 bin/jishi-mcp/jishi-mcp.exe。
    """
    from jishi import mcp_server

    suffix = ".exe" if __import__("os").name == "nt" else ""
    mcp_dir = tmp_path / "bin" / "jishi-mcp"
    mcp_dir.mkdir(parents=True)
    sibling = tmp_path / "bin" / "jishi" / f"jishi{suffix}"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("", encoding="utf-8")
    fake_self = mcp_dir / f"jishi-mcp{suffix}"
    fake_self.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_self))

    cmd = mcp_server._cli_command()
    assert cmd == [str(sibling)]
    assert "-m" not in cmd                      # 不能再试图按模块调用


def test_cli_command_same_dir_fallback(tmp_path, monkeypatch):
    """同目录就有 jishi 时优先用它（某些打包布局）。"""
    from jishi import mcp_server

    suffix = ".exe" if __import__("os").name == "nt" else ""
    d = tmp_path / "bin"
    d.mkdir()
    same = d / f"jishi{suffix}"
    same.write_text("", encoding="utf-8")
    fake_self = d / f"jishi-mcp{suffix}"
    fake_self.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_self))
    assert mcp_server._cli_command() == [str(same)]


def test_mcp_server_build_entry_exists():
    """发行版要打 jishi-mcp，入口脚本必须在。"""
    assert (ROOT / "tools" / "entry_mcp.py").is_file()
    src = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
    assert "entry_mcp.py" in src
