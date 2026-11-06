# -*- coding: utf-8 -*-
"""M161 · 一段输入别把程序打爆。

四条，共同点是「**输入**决定程序会不会卡死 / 崩掉 / 吃光内存」：

| # | 问题 | 原来 | 现在 |
|---|---|---|---|
| 1 | 正则回溯（ReDoS） | `(a+)+$` 配 28 个 a，**15 秒回不来** | 回溯步数上限 2000 万步 ⇒ **约 0.85 秒**报错 |
| 2 | 递归无上限 | 能跑到 **1500 万层**，静默吃内存 | Rust 补上与 Node/Python 一致的 1000 层上限 |
| 3 | 畸形字节码 panic | `main` 越界 / 常量池清空 ⇒ **Rust panic**（rc=101） | 加载时结构自检 + 执行时常量边界检查 ⇒ 中文报错 |
| 4 | Node `EBUSY` | `网络` 模块 spawn 自己 ⇒ Windows 偶发 `EBUSY` | Worker + Atomics.wait，**子进程整个去掉** |

## 为什么这四条算「语言本体」

它们都不是「谁能读谁的文件」，而是**同一段正确语法的程序，被一段数据搞死**。
用户看到的往往是「卡住、没输出、没报错」或者一串 Rust backtrace —— 最难受的两种。

## 平台差异（如实写）

`正则` 的**步数上限只在 Rust 宿主上生效**：Python 用 CPython 的 `re`、Node 用 JS `RegExp`，
那两个引擎不提供步数钩子，也没法从外面加。发行版是 Rust ⇒ 用户实际受保护；
另外两个是判据基准 ⇒ 它们的 ReDoS 风险记在已知边界里，语料不拿它做对拍。
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

需要Rust = pytest.mark.skipif(not rust_exe_path().is_file(),
                             reason="没有 Rust 宿主（这三条都是 Rust 侧的实现）")


def _跑(源: str, 目录: Path, 超时: float = 60.0, 参数=None):
    p = 目录 / "程序.jsh"
    p.write_text(源, encoding="utf-8", newline="\n")
    t0 = time.perf_counter()
    r = subprocess.run([str(rust_exe_path()), str(p)] + list(参数 or []),
                       cwd=str(目录), capture_output=True, timeout=超时, input=b"")
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace"), \
        time.perf_counter() - t0


# ---------------------------------------------------------------------------
# 一、ReDoS：复杂模式要「被拦」，而不是「挂死」
# ---------------------------------------------------------------------------

@需要Rust
def test_嵌套量词被步数上限拦住(tmp_path):
    甲 = "a" * 28 + "b"          # 20 个 a 时 0.14s、24 个 1.02s、28 个原本 >15s
    码, 文, 用 = _跑('导入 正则\n令 甲 = "' + 甲 + '"\n打印(正则.匹配("(a+)+$", 甲))\n',
                     tmp_path, 超时=90)
    assert 码 != 0, f"竟然算完了：{文[:200]}"
    assert "正则太复杂" in 文, f"报的不是「太复杂」：{文[:300]}"
    assert 用 < 5, f"拦得还是太慢（{用:.1f} 秒）—— 上限没生效？"


@需要Rust
def test_正常正则不受影响(tmp_path):
    """别为了防 ReDoS 把正常用法弄残 —— 常用模式要又快又对。"""
    码, 文, 用 = _跑(
        '导入 正则\n'
        '打印(正则.匹配("(\\\\d+)-(\\\\d+)", "123-456"))\n'
        '打印(正则.替换("(\\\\d+)", "[\\\\1]", "a1b22c333"))\n'
        '打印(长度(正则.查找全部("\\\\w+", "你好 世界 基石")))\n',
        tmp_path)
    assert 码 == 0, 文[:300]
    assert 文.splitlines()[:3] == ["真", "a[1]b[22]c[333]", "3"], 文[:300]
    assert 用 < 2, f"正常正则不该慢（{用:.2f} 秒）"


# ---------------------------------------------------------------------------
# 二、递归：有上限，且给的是「教人改」的话
# ---------------------------------------------------------------------------

@需要Rust
def test_无限递归给中文报错而不是吃光内存(tmp_path):
    码, 文, 用 = _跑("函数 递归(n)：\n    返回 1 + 递归(n + 1)\n打印(递归(0))\n",
                     tmp_path, 超时=30)
    assert 码 != 0
    assert "递归层数太深" in 文, f"报得不对：{文[:300]}"
    assert "结束条件" in 文, f"没教人怎么改：{文[:300]}"
    assert 用 < 10, f"太快不了（{用:.1f} 秒）—— 上限没生效？"


@需要Rust
def test_正常递归照常跑(tmp_path):
    码, 文, _用 = _跑(
        "函数 斐波(n)：\n"
        "    如果 n < 2：\n"
        "        返回 n\n"
        "    返回 斐波(n - 1) + 斐波(n - 2)\n"
        "打印(斐波(18))\n", tmp_path)
    assert 码 == 0 and 文.strip() == "2584", 文[:200]


# ---------------------------------------------------------------------------
# 三、畸形字节码：不许 panic
# ---------------------------------------------------------------------------

def _字节码(目录: Path) -> dict:
    sys.path.insert(0, str(ROOT))
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source
    return json.loads(serialize.dumps(compile_source("打印(1)\n", "x.jsh")))


@需要Rust
@pytest.mark.parametrize("改法", ["main 越界", "常量池清空", "代码块清空", "指令流截半"])
def test_畸形字节码只给中文报错(tmp_path, 改法):
    """四份坏 JSON：**一份都不许 panic**（rc=101 + Rust backtrace 是最糟的失败形态）。"""
    坏 = _字节码(tmp_path)
    if 改法 == "main 越界":
        坏["payload"]["main"] = 99
    elif 改法 == "常量池清空":
        坏["payload"]["consts"] = []
    elif 改法 == "代码块清空":
        坏["payload"]["codes"] = []
    else:
        坏["payload"]["codes"][0]["instrs"] = 坏["payload"]["codes"][0]["instrs"][:3]
    p = tmp_path / "坏.json"
    p.write_text(json.dumps(坏, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([str(rust_exe_path()), "--load-bytecode", str(p)],
                       cwd=str(tmp_path), capture_output=True, timeout=60, input=b"")
    文 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 1, f"rc={r.returncode}（101 = panic）：{文[:300]}"
    assert "panicked" not in 文 and "stack overflow" not in 文, f"崩了：{文[:300]}"
    assert 文.strip(), "一句话都没说就退了"


# ---------------------------------------------------------------------------
# 四、Node 网络：不再 spawn 自己（`EBUSY` 的根）
# ---------------------------------------------------------------------------

def test_Node_网络不再spawn自己():
    """源码级判据：`网络` 那条路不许再出现 `spawnSync(process.execPath)`。

    为什么用源码判据而不是跑一遍：`EBUSY` 是**偶发**的（沙箱里才必现），
    跑一遍绿了不能证明下次不偶发；而「有没有 spawn 自己」是确定的。
    """
    import re
    源码 = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    # 只管道 `netRequest` 那一带（`系统.执行` 等仍需 spawnSync，那是它的正当用途）
    i = 源码.index("function netRequest")
    j = 源码.index("function net要成功", i)
    段 = 源码[i:j]
    assert "spawnSync" not in 段, "网络还在用子进程同步封装 —— EBUSY 会复发"
    assert "Worker" in 段 and "Atomics.wait" in 段, "没看到 Worker + Atomics 的写法"


def test_Node_网络真能跑():
    """不只看源码：真发一次请求（本地起服务，避免外网）。"""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802
            body = "你好".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):                          # 别往 stderr 喷日志
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    端口 = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        node = shutil.which("node")
        if not node:
            pytest.skip("没有 node")
        sys.path.insert(0, str(ROOT))
        from oracle.jishi import serialize
        from oracle.jishi.compiler import compile_source
        import tempfile
        目录 = Path(tempfile.mkdtemp(prefix=f"m161net_{__import__('os').getpid()}_"))
        源 = f'导入 网络\n打印(网络.获取("http://127.0.0.1:{端口}/"))\n'
        (目录 / "程序.json").write_text(
            serialize.dumps(compile_source(源, "程序.jsh")), encoding="utf-8")
        r = subprocess.run([node, str(ROOT / "oracle" / "node" / "index.js"),
                            str(目录 / "程序.json")],
                           cwd=str(目录), capture_output=True, timeout=120, input=b"")
        文 = (r.stdout + r.stderr).decode("utf-8", "replace")
        assert "你好" in 文, f"没拿到内容：{文[:300]}"
        assert "EBUSY" not in 文, f"还是 EBUSY：{文[:300]}"
        shutil.rmtree(目录, ignore_errors=True)
    finally:
        srv.shutdown()
        srv.server_close()
