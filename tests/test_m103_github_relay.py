# -*- coding: utf-8 -*-
"""M103：本地 CONNECT 中继（`tools/github_relay.py`）与发布脚本的「三档网络通道」。

**为什么要有这组判据**：2026-10-09 本机把 GitHub 各域名解析到 `127.0.0.1`
（编辑器/连接器的透明代理），而那个代理对 GitHub 一律 502；直连真 IP 却是通的。
`git` 不支持 `curl --resolve`，hosts 又不可写 —— 于是有了这个中继。

⚠️ 这里的判据**全都不碰外网**（不起真 DoH、不连 GitHub）：
它们钉的是「中继这台机器本身」与「发布脚怎么选通道」。真实连通性靠人工跑
`python tools/github_relay.py --check` 看。
"""

from __future__ import annotations

import socket
import socketserver
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import github_relay                                              # noqa: E402


class _Echo(socketserver.BaseRequestHandler):
    """回声服务器：把收到的字节原样吐回去（用来验证中继确实在搬字节）。"""

    def handle(self) -> None:
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(b"echo:" + data)


class _EchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _read_headers(sock: socket.socket) -> bytes:
    """读到空行为止（CONNECT 的应答头）。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.fixture()
def echo_port():
    srv = _EchoServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_IP_字面量原样返回():
    """git 也可能被配成 IP 地址 —— 中继不能再拿它去查 DoH。"""
    assert github_relay.real_ipv4("127.0.0.1") == "127.0.0.1"
    assert github_relay.real_ipv4("10.0.0.7") == "10.0.0.7"


def test_中继确实在隧道转发(echo_port):
    """**核心判据**：CONNECT 协商成功 + 之后的字节双向搬运。

    用一个本地回声服务器当「上游」，全程不出网。
    """
    server, port = github_relay.start()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        with s:
            s.sendall(f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n"
                      f"Host: 127.0.0.1:{echo_port}\r\n\r\n".encode())
            head = _read_headers(s)
            assert b"200" in head, head
            s.sendall(b"hello")
            assert s.recv(4096) == b"echo:hello"
    finally:
        github_relay.stop(server)


def test_连不上的目标返回_502():
    """上游连不上要是**明确的 502**，不能静默挂住（否则 git 会一直等）。"""
    server, port = github_relay.start()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        with s:
            # 127.0.0.1 上挑一个几乎不可能有人监听的端口
            s.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\nHost: x\r\n\r\n")
            assert b"502" in _read_headers(s)
    finally:
        github_relay.stop(server)


def test_发布脚本的通道选择_显式代理优先(monkeypatch):
    """`JISHI_GIT_PROXY` 给了就用它（**备案通道** —— 网络得走代理时启用）。"""
    import publish_github as pg

    monkeypatch.setattr(pg, "_PROXY_ENV", {})
    monkeypatch.setattr(pg, "_RELAY", None)
    monkeypatch.setenv("JISHI_GIT_PROXY", "http://127.0.0.1:1080")
    pg._setup_git_transport(quiet=True)
    assert pg._TRANSPORT.startswith("代理")
    assert pg._PROXY_ENV["https_proxy"] == "http://127.0.0.1:1080"


def test_发布脚本的通道选择_解析正常就直连(monkeypatch):
    """GitHub 域名解析正常时**什么都不做** —— 直连最干净。"""
    import publish_github as pg

    monkeypatch.setattr(pg, "_PROXY_ENV", {})
    monkeypatch.setattr(pg, "_RELAY", None)
    monkeypatch.delenv("JISHI_GIT_PROXY", raising=False)
    monkeypatch.setattr(pg, "_github_hijacked", lambda: False)
    pg._setup_git_transport(quiet=True)
    assert pg._TRANSPORT == "直连"
    assert pg._PROXY_ENV == {}
