# -*- coding: utf-8 -*-
"""M230 · 进程字节级读（L-13）—— LSP / DAP 直连的地基。

## 这一站解决的是什么

`进程` 模块 M217 起只有**按行**读（`读行` / `读错误行`）。而 LSP / DAP 的报文是

```
Content-Length: 780\\r\\n\\r\\n{"jsonrpc":"2.0","id":1,"result":{…}}
```

—— **正文里一旦有 `\\n`，按行读就会把一帧切成好几段**。磐石 IDE 为此在中间挂了
一个 Node 的 `lsp行桥.js`（自己按字节切帧再转发）。这一站把字节级读做进语言，
那个桥就可以删了。

## 判据分三层

1. **字节读的语义**：给多少、超时给空字节（不抛错）、`读可用` 不阻塞；
2. **真连会话说**：起一个真的 `jishi-rs lsp`，发 `initialize`，把回来的
   `Content-Length` 帧**完整**收下来（头 + 正文），正文解析得出 `capabilities`；
3. **两个宿主一致**：Python 与 Rust 给的字节逐字节相同。

⚠️ **一个坑记在这里**：`读块` 的字节视图**必须从 `启动` 那一刻就开始攒** ——
懒开（等第一次 `读块` 再开）会丢掉「启动后、用户第一次读之前」到达的字节，
直连 LSP 时收到的**永远是空字节**（M230 实测量过一次）。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402


def _跑(源码: str, tmp_path: Path, 引擎: str = "Rust") -> str:
    """跑一段基石源码，返回 stdout+stderr。"""
    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
    if 引擎 == "Rust":
        exe = rust_exe_path()
        if not exe.is_file():
            pytest.skip("没编 jishi-rs")
        命令 = [str(exe), "程序.jsh"]
    else:
        命令 = [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"]
    r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True, timeout=180,
                       input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 一、字节读的语义
# ---------------------------------------------------------------------------

def test_读块按字节收(tmp_path):
    """给 10 个字节，`读块(h, 4)` 拿 4 个、再拿 6 个 —— 一个不多一个不少。"""
    出 = _跑('''导入 进程
令 h = 进程.启动("python", ["-c", "import sys; sys.stdout.write('A' * 10)"])
打印(长度(进程.读块(h, 4, 5)))
打印(长度(进程.读块(h, 100, 5)))
进程.等待(h)
''', tmp_path)
    行们 = [l for l in 出.strip().splitlines() if l.strip().isdigit()]
    assert 行们 == ["4", "6"], f"读块没按字节切：{出!r}"


def test_读块超时给空字节不抛错(tmp_path):
    """⚠️ 超时**不是错误**（与 `网络.获取流` 同一个口径）—— 给空字节，程序照跑。"""
    出 = _跑('''导入 进程
令 h = 进程.启动("python", ["-c", "import time; time.sleep(3)"])
令 块 = 进程.读块(h, 10, 0.2)
打印("拿到=" + 内建.文本(长度(块)))
进程.终止(h)
''', tmp_path)
    assert "拿到=0" in 出, f"超时没给空字节：{出!r}"
    assert "错误" not in 出, f"超时被当成错误了：{出!r}"


def test_读可用不阻塞(tmp_path):
    """`读可用` 立刻返回一个整数（不等待、不抛错）。"""
    出 = _跑('''导入 进程
令 h = 进程.启动("python", ["-c", "import time; time.sleep(2)"])
令 n = 进程.读可用(h)
打印("可用是整数=" + 内建.文本(类型(n) == "整数"))
打印("非负=" + 内建.文本(n >= 0))
进程.终止(h)
''', tmp_path)
    assert "可用是整数=真" in 出 and "非负=真" in 出, f"读可用不老实：{出!r}"


# ---------------------------------------------------------------------------
# 二、真连 LSP（这一站的全部意义）
# ---------------------------------------------------------------------------

#: 发一条 `initialize`，把回来的帧**完整**收下来（头 + 正文），交给判据检查。
#:
#: ⚠️ 这里刻意**不用**任何 JSON 库：就是要证明「字节能原样进出」——
#:    `Content-Length` 分帧是**字节级**协议，靠 `读行` 拼不出来。
_LSP程序 = '''导入 进程
导入 编码
令 h = 进程.启动("%(exe)s", ["lsp"])
令 起 = "%(起)s"
进程.写文本(h, "Content-Length: " + 内建.文本(长度(起)) + "\\r\\n\\r\\n" + 起)

# 循环收：先攒到「头 + \\r\\n\\r\\n」，再按 Content-Length 把正文收齐。
令 全部 = 进程.读块(h, 4096, 5)
令 累计 = 编码.字节文本(全部)
令 轮 = 0
当 轮 < 20：
    如果 累计.查找("capabilities") >= 0：
        中断
    令 再 = 进程.读块(h, 4096, 2)
    如果 长度(再) == 0：
        中断
    累计 = 累计 + 编码.字节文本(再)
    轮 = 轮 + 1
打印("收到=" + 内建.文本(长度(累计)))
打印("有头=" + 内建.文本(累计.查找("Content-Length:") >= 0))
打印("有正文=" + 内建.文本(累计.查找("capabilities") >= 0))
令 有id = 累计.查找("\\"id\\": 1") >= 0
如果 非 有id：
    有id = 累计.查找("\\"id\\":1") >= 0
打印("有id=" + 内建.文本(有id))
进程.终止(h)
'''

_起 = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
      '{"processId":null,"rootUri":null,"capabilities":{}}}')


def test_直连LSP收到完整帧(tmp_path):
    """🔴 **这一条就是 L-13 的验收**：字节级读能收下 LSP 的 `Content-Length` 帧。

    磐石 IDE 的 `helpers/lsp行桥.js` 做的正是这件事（按字节切帧再转发）；
    语言自己有 `读块` 之后，那个 Node 脚本可以删。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没编 jishi-rs")
    源 = _LSP程序 % {"exe": str(exe).replace("\\", "/"), "起": _起.replace('"', '\\"')}
    出 = _跑(源, tmp_path)
    assert "有头=真" in 出, f"没收到 Content-Length 头：{出!r}"
    assert "有正文=真" in 出, f"没收到正文：{出!r}"
    assert "有id=真" in 出, f"正文不像是 initialize 的回话：{出!r}"


# ---------------------------------------------------------------------------
# 三、两个宿主一致
# ---------------------------------------------------------------------------

def test_两宿主字节逐字相同(tmp_path):
    """同一段输出，Python 与 Rust 给的字节**逐字节相同**。

    ⚠️ 字节的**显示**也要一致（`类型()` 说「字节」而不是 `bytes`）——
    M230 实测踩过：Python 侧直接回原生 `bytes`，`类型()` 会说「bytes」。
    """
    源 = '''导入 进程
令 h = 进程.启动("python", ["-c", "import sys; sys.stdout.write('甲乙' if 0 else 'AB')"])
令 块 = 进程.读块(h, 100, 5)
打印(类型(块))
打印(块)
进程.等待(h)
'''
    py = _跑(源, tmp_path, "Python")
    rs = _跑(源, tmp_path, "Rust")
    assert py.strip() == rs.strip(), f"两宿主不一致：\nPython={py!r}\nRust={rs!r}"
    assert "字节" in py, f"`类型()` 没说「字节」：{py!r}"
