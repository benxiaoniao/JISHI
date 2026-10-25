# -*- coding: utf-8 -*-
"""`examples/projects/小网站/网站.jsh` 的**单独**判据（2026-10-24，M132）。

## 为什么它不参加「树遍历 vs 字节码」的全量对拍

那条对拍的判据是「两边跑完，stdout 逐字相同」。而这个示例是 **HTTP 服务端**：
`HTTP服务端.启动(8000, 分发)` 之后**永久监听端口、永不返回** ——
两边都跑不完，也就没有「跑完之后的输出」可比。
⇒ `tools/check_walk.py` 的 `不测的语料` 里显式登记了它（**已登记、会打印出来**，
不是悄悄跳过），并留了 `语料超时` 兜底防止别的长驻语料再把工具拖死。

## 但它不能因此就没人管

「不进对拍」不等于「不用测」。它是**唯一一个把 `HTTP服务端` + `HTTP路由` 串起来
的真程序**（M115 + M122 两批能力都靠它兜底），坏了必须有人知道 ——
所以这里单独起进程、真发 HTTP 请求、断言响应。

⚠️ **必须真的杀掉进程**：它是死循环，测试结束后留着会一直占住端口，
下一轮就起不来了（症状：第二次跑必失败、第一次必过 —— 最难查的那种）。

## 🔴 端口 8000 可能被 Windows 保留，**不许静默跳过**（2026-10-25 修）

本机 `netsh int ipv4 show excludedportrange protocol=tcp` 把 **7985–8084**
划进了保留区间（Hyper-V / WSL 的动态保留），于是 bind 8000 报：

```
[WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试
```

**那既不是「被占用」（那是 10048），也不是本项目的 bug。**
旧写法把它当成「被占用」直接 `skip` —— 结果这条判据**永远是灰的**，
而且没人会注意到（灰的没人看，正是 `AGENTS.md` 反复说的坑；实测就是这样默默跳过了一整天）。

⇒ 现在：**8000 能用就跑原文件**（测的就是那份示例）；
**不能用就换一个能用的端口，跑同一份代码的副本**（只替换 `启动(8000,` 那一处；
替换不到就断言报错，绝不「悄悄测了个空」）。**任何情况下都不跳过**，
换端口这件事用 `warnings.warn` 说出来（会出现在 pytest 的 warnings summary 里）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
示例 = ROOT / "examples" / "projects" / "小网站" / "网站.jsh"

#: 示例里写死的端口（`HTTP服务端.启动(8000, 分发)`）。
首选端口 = 8000
#: 8000 不可用时按顺序试（避开 Windows 常保留的那几段）。
备选端口 = (18000, 18080, 28000, 38000)


def _绑不上的原因(port: int) -> str:
    """空串 = 这个端口能绑；否则给一句**人话**原因（别把 10013 说成「被占用」）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return ""
        except OSError as e:
            winerr = getattr(e, "winerror", None)
            if winerr == 10013:
                return ("权限被拒（Windows 把这段端口保留了，查 "
                        "`netsh int ipv4 show excludedportrange protocol=tcp`）")
            if winerr == 10048:
                return "已被别的进程占用"
            return f"{e}"


def _端口空闲(port: int) -> bool:
    return _绑不上的原因(port) == ""


def _挑端口() -> tuple[int, str]:
    """返回 (能用的端口, 换端口的理由)。首选能用时理由是空串。"""
    理由 = _绑不上的原因(首选端口)
    if not 理由:
        return 首选端口, ""
    for p in 备选端口:
        if _端口空闲(p):
            return p, f"{首选端口} 用不了（{理由}）⇒ 改用 {p} 跑同一份代码"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:   # 让系统分一个
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
    return p, f"{首选端口} 和备选都用不了（{理由}）⇒ 让系统分了 {p}"


def _等端口(port: int, 上限秒: float = 20.0) -> bool:
    """轮询到端口能连上为止（服务是异步起来的，不能睡死一个固定值）。"""
    截止 = time.time() + 上限秒
    while time.time() < 截止:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _等放开(port: int, 上限秒: float = 10.0) -> bool:
    截止 = time.time() + 上限秒
    while time.time() < 截止:
        if _端口空闲(port):
            return True
        time.sleep(0.1)
    return False


def _取(port: int, path: str) -> tuple[int, str]:
    # ⚠️ URL 里带中文必须先 percent-encode：`urllib` 直接把非 ASCII 拼进请求行会
    #    `UnicodeEncodeError: 'ascii' codec can't encode`（我第一次就这么红的）。
    地址 = f"http://127.0.0.1:{port}" + urllib.parse.quote(path)
    try:
        with urllib.request.urlopen(地址, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                    # 404 也要能读到正文
        return e.code, e.read().decode("utf-8", "replace")


def _要跑的源(port: int) -> tuple[Path, str]:
    """返回 (要跑的文件, 说明)。8000 能用就跑**原文件**；否则跑只改了端口的副本。"""
    if port == 首选端口:
        return 示例, ""
    原文 = 示例.read_text(encoding="utf-8")
    旧 = f"HTTP服务端.启动({首选端口},"
    assert 旧 in 原文, (
        f"示例里找不到 `{旧}` —— 端口写法改了吗？这条判据的替换就失效了，"
        f"必须跟着改（**不许悄悄跳过**，那就等于没测）")
    目录 = Path(tempfile.gettempdir()) / f"m132_{os.getpid()}_{port}"
    目录.mkdir(parents=True, exist_ok=True)
    副本 = 目录 / 示例.name
    副本.write_text(原文.replace(旧, f"HTTP服务端.启动({port},", 1),
                    encoding="utf-8", newline="\n")
    return 副本, f"（副本：只把端口换成了 {port}）"


@pytest.mark.计时
def test_小网站示例真的能起服务并响应():
    """起进程 → GET `/` → GET 一个不存在的路径 → 杀掉 → 确认端口放开。

    ⚠️ 打 `计时` 标记（进**串行**节）：它会**真的占端口**，并行跑时多个 worker
    抢同一个端口会互相踩；而且它等的是墙上时间（最多 20 秒）。
    """
    assert 示例.is_file(), f"示例文件没了：{示例}"
    端口, 换端口理由 = _挑端口()
    if 换端口理由:
        # ⚠️ 用 warning 而不是 skip：**不许静默变灰**。它会出现在 pytest 的
        #    warnings summary 里（跑一遍就看得见），但判据照常执行。
        warnings.warn(UserWarning(换端口理由), stacklevel=2)
    源, 说明 = _要跑的源(端口)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "oracle.jishi.cli", str(源)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # ⚠️ stdin 给 DEVNULL：示例不读输入，但不给的话某些环境会拖住
        stdin=subprocess.DEVNULL,
    )
    try:
        assert _等端口(端口), (
            f"20 秒内没起来{说明}。stderr：\n"
            + (proc.stderr.read().decode("utf-8", "replace")[:800]
               if proc.poll() is not None else "（进程还活着，但端口没开）"))
        码, 正文 = _取(端口, "/")
        assert 码 == 200, f"GET / 给了 {码}"
        assert 正文.strip(), "GET / 的正文是空的"
        assert "<" in 正文 and ">" in 正文, f"GET / 看起来不是 HTML：{正文[:120]!r}"

        码404, 正文404 = _取(端口, "/没有这个路径")
        assert 码404 == 404, f"不存在的路径该给 404，实际 {码404}"
        # 示例的注释说「404 页会列出所有已注册的路」—— 那正是它比默认 404 有用的地方
        assert 正文404.strip(), "404 正文是空的（应该有一页说清有哪些路由）"
    finally:
        # 🔴 **必须真杀**：它是死循环，留着会占住端口 ——
        #    下一轮必失败、这一轮必过，是最难查的那种坑。
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:                   # pragma: no cover
            pass
        assert _等放开(端口), f"杀掉进程后 {端口} 端口还没放开 —— 下一轮会起不来"
