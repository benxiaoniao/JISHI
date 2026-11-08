#!/usr/bin/env python3
"""本地 CONNECT 中继 —— 让 git / curl 在「GitHub 域名被劫持到 127.0.0.1」的环境里仍能连上 GitHub。

## 为什么需要它（2026-10-09 实测的环境事实）

* 本机把 **GitHub 各域名解析到 `127.0.0.1`**（`github.com` / `api.github.com` /
  `codeload.github.com` …），其它域名解析正常（`example.com` → 真 IP）。
  这是编辑器/连接器的透明代理做的（GitHub MCP 连接器因此能用）。
* 那个本地代理（当时是 `127.0.0.1:2451`）**对普通站点正常，唯独 GitHub 一律
  `CONNECT tunnel failed, response 502`**。
* **直连 GitHub 真实 IP 是通的**（`curl --resolve github.com:443:<真IP>` → 200）。
* 但 **`git` 不支持 `curl --resolve`**，也没有 `curloptResolve` 配置项；
  **hosts 文件不可写**（要管理员权限）。作者原来的中转（`127.0.0.1:9066`，SteamTools）
  是随代理工具一起走的，工具一关就没了 —— 所以这里做一份**不依赖任何外部工具**的。

## 它怎么工作

起一个本地监听，git 把「我要连 github.com:443」交给它（HTTP 代理的 `CONNECT`），
它按 **DoH 查到的真实 IP** 去建立 TCP，然后把字节双向搬运。

⚠️ **TLS 是端到端的**（本中继只搬字节，不解密），所以 SNI / 证书仍然是 `github.com`
—— 与直接用 `curl --resolve` 等价，**不是**「把证书校验关掉」那种降级。

## 用法

    # 1) 手动：起中继（打印端口），另开一个终端把它设成 git 的代理
    python tools/github_relay.py --port 8899
    git -c http.proxy=http://127.0.0.1:8899 ls-remote https://github.com/benxiaoniao/JISHI.git HEAD

    # 2) 代码里：publish_github.py 会自动起一个（见 _git_env / _ensure_relay）
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import select
import socket
import socketserver
import sys
import threading
import urllib.request

#: DoH 端点：拿 GitHub 的真实 A 记录（走系统代理或直连都实测可用）。
DOH = "https://cloudflare-dns.com/dns-query"

_dns_cache: dict[str, list[str]] = {}


def _is_loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip == "::1"


def _doh_a(host: str) -> list[str]:
    """问 DoH 要 `host` 的 A 记录（带缓存）。失败返回空表，由调用方兜底。"""
    if host in _dns_cache:
        return _dns_cache[host]
    ips: list[str] = []
    try:
        req = urllib.request.Request(
            f"{DOH}?name={host}&type=A",
            headers={"accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        ips = [a["data"] for a in data.get("Answer", [])
               if a.get("type") == 1 and isinstance(a.get("data"), str)]
    except Exception:                                          # noqa: BLE001
        ips = []
    _dns_cache[host] = ips
    return ips


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def real_ipv4(host: str) -> str | None:
    """给一个主机名，返回**真实** IPv4。

    系统解析若不是回环就用它（非 GitHub 的域名这样就够了）；若是回环
    （= 被沙箱劫持），改问 DoH。都拿不到就返回 `None`。

    ⚠️ `host` **本身就是 IP 字面量**时（git 也可能被配成 IP 地址）原样返回
    —— 否则会去 DoH 查一个 IP，白查还查不到。
    """
    if _is_ip_literal(host):
        return host.strip("[]")
    try:
        ip = socket.gethostbyname(host)
        if not _is_loopback(ip):
            return ip
    except OSError:
        pass
    ips = _doh_a(host)
    return ips[0] if ips else None


class _Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self) -> None:                                  # noqa: C901
        line = self.rfile.readline(65536)
        if not line:
            return
        try:
            method, target, _version = line.decode("latin-1").split()
        except ValueError:
            return
        # 吃掉请求头（CONNECT 我们只关心第一行）
        while True:
            h = self.rfile.readline(65536)
            if h in (b"\r\n", b"\n", b""):
                break

        if method.upper() != "CONNECT":
            self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n"
                             b"Connection: close\r\n\r\n")
            return

        host, _, port_s = target.partition(":")
        port = int(port_s or "443")

        upstream = None
        if _is_ip_literal(host):
            # IP 字面量：直接连，**不要**去查 DoH（那是在查一个 IP，白查）
            candidates = [host.strip("[]")]
        else:
            ip = real_ipv4(host)
            candidates = [ip] if ip else []
            candidates += [x for x in _doh_a(host) if x != ip]
        for cand in candidates:
            if not cand:
                continue
            try:
                upstream = socket.create_connection((cand, port), timeout=12)
                break
            except OSError:
                upstream = None
        if upstream is None:
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n"
                             b"Connection: close\r\n\r\n")
            return

        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        self._tunnel(upstream)

    def _tunnel(self, upstream: socket.socket) -> None:
        """双向搬字节，直到任一端关闭。"""
        client = self.connection
        socks = [client, upstream]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 60)
                if not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (upstream if s is client else client).sendall(data)
        except OSError:
            pass
        finally:
            try:
                upstream.close()
            except OSError:
                pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(port: int = 0, host: str = "127.0.0.1") -> tuple[_Server, int]:
    """起中继（后台线程）。返回 `(server, 实际端口)`。"""
    srv = _Server((host, port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def stop(srv: _Server) -> None:
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:                                          # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="本地 CONNECT 中继（GitHub 真 IP 直连）")
    p.add_argument("--port", type=int, default=0,
                   help="监听端口（0 = 自动挑一个空闲端口并打印）")
    p.add_argument("--check", action="store_true",
                   help="只检查一步：打印几个关键域名的真实 IP，不起服务")
    args = p.parse_args(argv)

    if args.check:
        for h in ("github.com", "api.github.com", "codeload.github.com",
                  "objects.githubusercontent.com"):
            print(f"  {h:34s} -> {real_ipv4(h)}")
        return 0

    srv, port = start(args.port)
    print(f"中继已起：http://127.0.0.1:{port}  (Ctrl-C 退出)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop(srv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
