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

⚠️ **必须真的杀掉进程**：它是死循环，测试结束后留着会一直占住 8000 端口，
下一轮就起不来了（症状：第二次跑必失败、第一次必过 —— 最难查的那种）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
示例 = ROOT / "examples" / "projects" / "小网站" / "网站.jsh"

#: 示例里写死的端口（`HTTP服务端.启动(8000, 分发)`）。
端口 = 8000


def _端口空闲(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


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


def _取(path: str) -> tuple[int, str]:
    # ⚠️ URL 里带中文必须先 percent-encode： 直接把非 ASCII 拼进请求行会
    #    （我第一次就这么红了）。
    import urllib.parse as _up
    地址 = f"http://127.0.0.1:{端口}" + _up.quote(path)
    try:
        with urllib.request.urlopen(地址, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                    # 404 也要能读到正文
        return e.code, e.read().decode("utf-8", "replace")


@pytest.mark.计时
def test_小网站示例真的能起服务并响应():
    """起进程 → GET `/` → GET 一个不存在的路径 → 杀掉。

    ⚠️ 打 `计时` 标记（进**串行**节）：它会**真的占端口**，并行跑时多个 worker
    抢同一个 8000 会互相踩；而且它等的是墙上时间（最多 20 秒）。
    """
    if not _端口空闲(端口):
        pytest.skip(f"127.0.0.1:{端口} 已被占用（别的进程/上一轮没杀干净）")
    assert 示例.is_file(), f"示例文件没了：{示例}"

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "oracle.jishi.cli", str(示例)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # ⚠️ stdin 给 DEVNULL：示例不读输入，但不给的话某些环境会拖住
        stdin=subprocess.DEVNULL,
    )
    try:
        assert _等端口(端口), (
            "20 秒内没起来。stderr：\n"
            + (proc.stderr.read().decode("utf-8", "replace")[:800] if proc.poll() is not None else "（进程还活着，但端口没开）"))
        码, 正文 = _取("/")
        assert 码 == 200, f"GET / 给了 {码}"
        assert 正文.strip(), "GET / 的正文是空的"
        assert "<" in 正文 and ">" in 正文, f"GET / 看起来不是 HTML：{正文[:120]!r}"

        码404, 正文404 = _取("/没有这个路径")
        assert 码404 == 404, f"不存在的路径该给 404，实际 {码404}"
        # 示例的注释说「404 页会列出所有已注册的路」—— 那正是它比默认 404 有用的地方
        assert 正文404.strip(), "404 正文是空的（应该有一页说清有哪些路由）"
    finally:
        # 🔴 **必须真杀**：它是死循环，留着会占住 8000 端口
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:                   # pragma: no cover
            pass
        # 杀掉之后再确认端口放开了（否则下一轮必 skip，问题会被掩盖）
        放开 = False
        for _ in range(50):
            if _端口空闲(端口):
                放开 = True
                break
            time.sleep(0.1)
        assert 放开, f"杀掉进程后 {端口} 端口还没放开 —— 下一轮会起不来"
