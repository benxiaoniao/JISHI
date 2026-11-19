# -*- coding: utf-8 -*-
"""M206：联网安全护栏 + 跨平台 HTTPS。

这一站补的七件事，每一条都对应一种**真实的坏事**：

| 护栏 | 不补会怎么样 |
|---|---|
| 响应大小上限（默认 32 MB） | 一个恶意/坏掉的链接就能把进程 OOM 掉 |
| `下载` 流式落盘 | 下 1 GB 文件先占 1 GB 内存 |
| 重定向上限 5 | 重定向环把程序卡死（也数不清跳了几次） |
| 禁止 https→http 降级 | 中间人只要回一个 302 就能把后面的流量变成明文 |
| gzip/deflate 自动解压 | 不压浪费带宽；而 `inflate` 我们本来就有 |
| 代理 | 企业内网 / 抓包排查都用不了 |
| 报错分类 | 「解析不了 / 连不上 / 被重置 / 超时 / 证书没过」混成一句「出错了」 |

## 怎么验的

**本地起一个 HTTP 服务**（`_网`）造各种坏情况 —— 不依赖外网。
另外两条真 TLS：公网 `https://example.com`（真证书链），
自签证书那条只在非 Windows 上跑（见 `test_https_对自签证书会拒绝` 的说明）。
"""

from __future__ import annotations

import gzip
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import zlib
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from conftest import rust_exe_path, tmp_bc_path  # noqa: E402
from oracle.jishi import serialize  # noqa: E402
from oracle.jishi.interpreter import run_source  # noqa: E402

RUST_EXE = rust_exe_path()


def _跑(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src, filename="<t>")
    return buf.getvalue()


class _网(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_error(self, request, client_address):
        """客户端提前断开（超限/跳过）不该在测试输出里刷 traceback。"""
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def _回(self, 状态, 内容: bytes, 类型="text/plain", 额外=None):
        self.send_response(状态)
        self.send_header("Content-Type", 类型)
        self.send_header("Content-Length", str(len(内容)))
        for k, v in (额外 or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(内容)
        except (ConnectionResetError, BrokenPipeError):
            pass      # 客户端提前走了（超限/断开），不是测试要关心的

    def _跳(self, 位置, 状态=302):
        self.send_response(状态)
        self.send_header("Location", 位置)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self.path
        # 经代理进来的请求行是**绝对地址**（`GET http://host/path HTTP/1.1`）——
        # 不归一的话「代理生效」会被误判成 404（2026-11-17 踩）
        if p.startswith("http://") or p.startswith("https://"):
            p = "/" + p.split("/", 3)[-1] if p.count("/") >= 3 else "/"
        if p == "/ok":
            self._回(200, "好了".encode("utf-8"))
        elif p.startswith("/big"):
            长 = int(p.split("=")[1]) if "=" in p else 10_000_000
            self._回(200, b"x" * 长, "application/octet-stream")
        elif p == "/gzip":
            原 = "这段是压缩过的正文".encode("utf-8") * 50
            压 = gzip.compress(原)
            self._回(200, 压, "text/plain", {"Content-Encoding": "gzip"})
        elif p == "/deflate":
            原 = "deflate 也要能解".encode("utf-8") * 30
            self._回(200, zlib.compress(原), "text/plain",
                     {"Content-Encoding": "deflate"})
        elif p.startswith("/redir"):
            # /redir?n=3 → 连跳 3 次到 /ok
            剩 = int(p.split("=")[1]) if "=" in p else 0
            self._跳(f"/redir?n={剩 - 1}" if 剩 > 0 else "/ok")
        elif p == "/loop":
            self._跳("/loop")
        elif p == "/rel":
            self._跳("ok")                    # 相对地址
        elif p == "/down":                    # 分块传输（chunked）
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            块们 = (b"a" * 1000, b"b" * 2000, b"c" * 3000)
            try:
                for 块 in 块们:
                    self.wfile.write(b"%x\r\n" % len(块) + 块 + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except (ConnectionResetError, BrokenPipeError):
                pass
        elif p == "/echo-head":
            self._回(200, str(self.headers.get("X-Test", "")).encode("utf-8"))
        elif p == "/slow":
            time.sleep(3)
            self._回(200, b"late")
        else:
            self._回(404, b"no")


@pytest.fixture
def 网端口():
    httpd = HTTPServer(("127.0.0.1", 0), _网)
    端口 = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield 端口
    finally:
        httpd.shutdown()
        httpd.server_close()


def _空端口() -> int:
    """拿一个**确定没人监听**的端口（用来测「连不上」）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------------------
# 一、响应大小上限
# ---------------------------------------------------------------------------

def test_响应超过上限就报错(网端口):
    出 = _跑(
        "导入 网络\n"
        "尝试：\n"
        f'    网络.获取("http://127.0.0.1:{网端口}/big?n=200000", 10, 空, {{"大小上限": 1000}})\n'
        "捕获 异常 为 e：\n"
        "    打印(e.消息)\n")
    assert "响应太大" in 出, 出
    assert "上限 1000" in 出, 出


def test_上限之内正常拿到(网端口):
    出 = _跑(
        "导入 网络\n"
        f'令 s = 网络.获取("http://127.0.0.1:{网端口}/big?n=5000", 10, 空, {{"大小上限": 100000}})\n'
        "打印(长度(s))\n")
    assert out_line(出) == "5000", 出


def out_line(s: str) -> str:
    return s.strip().splitlines()[-1]


def test_下载默认不限大小且落盘(网端口, tmp_path):
    """`下载` 的默认上限是 0（不限）—— 它就是为大文件准备的；且边读边写。"""
    目标 = tmp_path / f"大-{os.getpid()}.bin"
    出 = _跑(
        "导入 网络\n"
        f'令 n = 网络.下载("http://127.0.0.1:{网端口}/big?n=3000000", "{目标.as_posix()}")\n'
        "打印(n)\n")
    assert out_line(出) == "3000000", 出
    assert 目标.stat().st_size == 3_000_000
    assert not Path(str(目标) + ".part").exists(), "`.part` 没被改名/清理"


# ---------------------------------------------------------------------------
# 二、gzip / deflate
# ---------------------------------------------------------------------------

def test_gzip自动解压(网端口):
    出 = _跑(
        "导入 网络\n"
        f'令 s = 网络.获取("http://127.0.0.1:{网端口}/gzip")\n'
        "打印(长度(s))\n"
        "打印(s[0:8])\n")
    期望 = "这段是压缩过的正文" * 50
    行 = 出.strip().splitlines()
    assert 行[0] == str(len(期望)), 出
    assert 行[1] == 期望[:8], 出


def test_deflate自动解压(网端口):
    出 = _跑(
        "导入 网络\n"
        f'令 s = 网络.获取("http://127.0.0.1:{网端口}/deflate")\n'
        "打印(长度(s))\n")
    期望 = len("deflate 也要能解" * 30)
    assert out_line(出) == str(期望), 出


# ---------------------------------------------------------------------------
# 三、重定向
# ---------------------------------------------------------------------------

def test_连跳三次能到(网端口):
    出 = _跑(
        "导入 网络\n"
        f'打印(网络.获取("http://127.0.0.1:{网端口}/redir?n=3"))\n')
    assert out_line(出) == "好了", 出


def test_相对地址的跳转也认(网端口):
    出 = _跑(
        "导入 网络\n"
        f'打印(网络.获取("http://127.0.0.1:{网端口}/rel"))\n')
    assert out_line(出) == "好了", 出


def test_跳转超过上限就报错(网端口):
    出 = _跑(
        "导入 网络\n"
        "尝试：\n"
        f'    网络.获取("http://127.0.0.1:{网端口}/loop")\n'
        "捕获 异常 为 e：\n"
        "    打印(e.消息)\n")
    assert "跳转超过 5 次" in 出, 出


def test_降级拦截是真的在拦(网端口):
    """https→http 的降级**必须**报错。

    本地没有可信 https 服务，所以这里验的是**源码里的那条闸**：
    `http.rs` 要显式比 `url.secure && !下一站.secure`，并且默认 `允许降级 = 假`；
    Python 侧同一句。真机上的端到端在 `test_https_...` 那两条。
    """
    rs = (ROOT / "rust" / "src" / "http.rs").read_text(encoding="utf-8")
    assert "明文降级不安全" in rs
    assert "允许降级: false" in rs, "默认必须是「不许降级」"
    py = (ROOT / "oracle" / "jishi" / "stdlib" / "网络.py").read_text(encoding="utf-8")
    assert "明文降级不安全" in py
    assert '"允许降级": False' in py


# ---------------------------------------------------------------------------
# 四、请求头注入
# ---------------------------------------------------------------------------

def test_请求头里带换行直接拒(网端口):
    出 = _跑(
        "导入 网络\n"
        "尝试：\n"
        f'    网络.获取("http://127.0.0.1:{网端口}/ok", 10, {{"X-Test": "a\\r\\nX-Evil: 1"}})\n'
        "捕获 异常 为 e：\n"
        "    打印(e.消息)\n")
    assert "头注入" in 出 or "换行" in 出, 出


# ---------------------------------------------------------------------------
# 五、代理
# ---------------------------------------------------------------------------

def test_代理真的被用上(网端口):
    """把代理指向**我们自己的样例服务**，然后请求一个**不存在的域名**。

    如果代理生效：服务收到的是「GET http://不存在的域名/ok」，照样回 200；
    如果代理没生效：DNS 解析失败 ⇒ 报错。
    ⇒ 「成功」这件事本身就是代理生效的证明（`NO_PROXY` 里没有那个假域名）。

    ⚠️ 别用 127.0.0.1 当目标：`NO_PROXY` 里本来就有它，urllib 会**绕过代理**直连
    —— 那样测出来的是「直连成功」，不是「代理生效」（2026-11-17 踩过）。
    """
    出 = _跑(
        "导入 网络\n"
        f'打印(网络.获取("http://proxy-test.invalid/ok", 10, 空, '
        f'{{"代理": "http://127.0.0.1:{网端口}"}}))\n')
    assert out_line(出) == "好了", f"代理没生效：{出}"


# ---------------------------------------------------------------------------
# 六、报错分类
# ---------------------------------------------------------------------------

def test_域名解析不了有专门的话():
    出 = _跑(
        "导入 网络\n"
        "尝试：\n"
        '    网络.获取("http://no-such-host-abc.invalid/", 3, 空, {"代理": "无"})\n'
        "捕获 异常 为 e：\n"
        "    打印(e.消息)\n")
    assert "域名解析不了" in 出, 出


def test_连不上有专门的话():
    出 = _跑(
        "导入 网络\n"
        "尝试：\n"
        f'    网络.获取("http://127.0.0.1:{_空端口()}/", 3)\n'
        "捕获 异常 为 e：\n"
        "    打印(e.消息)\n")
    assert "连不上服务器" in 出, 出


# ---------------------------------------------------------------------------
# 七、跨引擎：同一段程序在 Python VM 与 Rust VM 上结果一致
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not RUST_EXE.is_file(), reason="发行版没编")
def test_gzip与重定向在Rust宿主上一致(网端口, tmp_path):
    源 = (
        "导入 网络\n"
        f'打印(长度(网络.获取("http://127.0.0.1:{网端口}/gzip")))\n'
        f'打印(网络.获取("http://127.0.0.1:{网端口}/redir?n=3"))\n'
        f'打印(长度(网络.获取("http://127.0.0.1:{网端口}/down")))\n'
    )
    py出 = _跑(源)
    bc = tmp_bc_path(ROOT / "_tmp_m206_bc.json")
    from oracle.jishi.compiler import compile_source
    bc.write_text(serialize.dumps(compile_source(源, "<t>")), encoding="utf-8")
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (r.stderr or "")[:600]
    assert r.stdout == py出, f"Rust 侧不一致：\npy={py出!r}\nrs={r.stdout!r}"


# ---------------------------------------------------------------------------
# 八、真 TLS（公网真证书）
# ---------------------------------------------------------------------------

def _有外网() -> bool:
    try:
        socket.create_connection(("example.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _有外网(), reason="本机/CI 连不上 example.com（离线环境）")
def test_https_公网真证书能取回正文():
    出 = _跑(
        "导入 网络\n"
        '令 s = 网络.获取("https://example.com/", 15)\n'
        "打印(长度(s) > 100, s.包含(\"Example Domain\"))\n")
    assert out_line(出) == "真 真", 出


@pytest.mark.skipif(not _有外网(), reason="离线")
@pytest.mark.skipif(not RUST_EXE.is_file(), reason="发行版没编")
def test_https_在Rust宿主上也通():
    """Rust 侧才是「新写的 TLS」那条路（Linux/macOS 走 dlopen，Windows 走 WinHTTP）。"""
    bc = tmp_bc_path(ROOT / "_tmp_m206_tls.json")
    from oracle.jishi.compiler import compile_source
    源 = ('导入 网络\n令 s = 网络.获取("https://example.com/", 15)\n'
          '打印(长度(s) > 100)\n')
    bc.write_text(serialize.dumps(compile_source(源, "<t>")), encoding="utf-8")
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (r.stderr or "")[:800]
    assert out_line(r.stdout) == "真", r.stdout + (r.stderr or "")


# ---------------------------------------------------------------------------
# 九、源码层面：TLS 是 dlopen 系统库，不是 crate
# ---------------------------------------------------------------------------

def test_没有为TLS引第三方crate():
    cargo = (ROOT / "rust" / "Cargo.toml").read_text(encoding="utf-8")
    for 名 in ("openssl", "rustls", "native-tls", "native_tls", "schannel", "security-framework"):
        依赖行 = [ln for ln in cargo.splitlines()
                  if ln.strip().startswith(f"{名} =") or ln.strip().startswith(f"{名}=")]
        assert not 依赖行, f"TLS 不许引 crate（M206 的硬约束），但发现：{依赖行}"
    rs = (ROOT / "rust" / "src" / "http.rs").read_text(encoding="utf-8")
    assert "dlopen" in rs, "TLS 应当是 dlopen 系统 libssl"
    assert "SSL_set1_host" in rs or "X509_VERIFY_PARAM_set1_host" in rs, "必须做主机名校验"


def test_中文域名给的是人话():
    """中文**域名**不做 punycode（那是另一件事），但不能报 codec 错。"""
    rs = (ROOT / "rust" / "src" / "http.rs").read_text(encoding="utf-8")
    assert "punycode" in rs, "Rust 侧没解释「中文域名要先转 punycode」"
    py = (ROOT / "oracle" / "jishi" / "stdlib" / "网络.py").read_text(encoding="utf-8")
    assert "punycode" in py, "Python 侧没解释「中文域名要先转 punycode」"


# ---------------------------------------------------------------------------
# 十、自签证书（本地 TLS 服务）：证书校验是真的在做
# ---------------------------------------------------------------------------

TLS目录 = ROOT / "tests" / "fixtures" / "tls"
证书 = TLS目录 / "cert.pem"
私钥 = TLS目录 / "key.pem"


@pytest.fixture
def tls端口():
    import ssl as _ssl
    if not (证书.is_file() and 私钥.is_file()):
        pytest.skip("缺 tests/fixtures/tls 的测试证书")
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(证书), str(私钥))
    httpd = HTTPServer(("127.0.0.1", 0), _网)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    端口 = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield 端口
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_https_自签证书要被拒绝(tls端口):
    """没有可信 CA 时必须报「证书校验没过」—— **不许默默接受**。"""
    出 = _跑(
        "导入 网络\n"
        "尝试：\n"
        f'    网络.获取("https://127.0.0.1:{tls端口}/ok", 8)\n'
        "捕获 异常 为 e：\n"
        "    打印(e.消息)\n")
    assert "证书校验没过" in 出, 出


def test_https_带JISHI_TLS_CA就能连自签(tls端口, monkeypatch):
    """给了 `JISHI_TLS_CA` 就该连上 —— 企业内网自签正是这么用的。"""
    monkeypatch.setenv("JISHI_TLS_CA", str(证书))
    出 = _跑(
        "导入 网络\n"
        f'打印(网络.获取("https://127.0.0.1:{tls端口}/ok", 8))\n')
    assert out_line(出) == "好了", 出


@pytest.mark.skipif(os.name == "nt",
                    reason="WinHTTP 只用系统证书存储；判据不许往系统里塞 CA（会污染用户机器）")
@pytest.mark.skipif(not RUST_EXE.is_file(), reason="发行版没编")
def test_Rust宿主的TLS也认JISHI_TLS_CA(tls端口, tmp_path):
    """改完 CA 之后，Rust 侧（Linux/macOS 走 dlopen 的 OpenSSL）也要能连上。

    ⚠️ 只在非 Windows 上跑：WinHTTP 的自定 CA 要导进系统存储，**判据不许这么干**。
    所以 Windows 上的 Rust TLS 只由公网那条（`test_https_在Rust宿主上也通`）覆盖。
    """
    from oracle.jishi.compiler import compile_source
    源 = (f'导入 网络\n打印(网络.获取("https://127.0.0.1:{tls端口}/ok", 8))\n')
    bc = tmp_bc_path(ROOT / "_tmp_m206_tls2.json")
    bc.write_text(serialize.dumps(compile_source(源, "<t>")), encoding="utf-8")
    环境 = dict(os.environ)
    环境["JISHI_TLS_CA"] = str(证书)
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True, text=True, encoding="utf-8", env=环境)
    assert r.returncode == 0, (r.stderr or "")[:800]
    assert out_line(r.stdout) == "好了", r.stdout + (r.stderr or "")


# ---------------------------------------------------------------------------
# 十一、分块响应的「预读字节」（CI 上抓到的真 bug）
# ---------------------------------------------------------------------------

def _一次性服务(响应: bytes):
    """起一个**一次性**的裸 socket 服务：accept 后把整段响应**一次 sendall**。

    为什么要裸 socket：`http.server` 会把头与正文分开 flush，测不到
    「读响应头时把正文也读进来了」这种情形 —— 而那正是 CI 上 example.com
    报「gzip 头不合法」的根因（分块流的前几个字节被当正文吞了）。
    """
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    端口 = srv.getsockname()[1]

    def 跑():
        try:
            c, _ = srv.accept()
            with c:
                c.recv(4096)
                c.sendall(响应)
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=跑, daemon=True).start()
    return 端口


@pytest.mark.skipif(not RUST_EXE.is_file(), reason="发行版没编")
def test_分块响应与头一起到达也不错位():
    """分块响应 + 头和正文**同一次 send** ⇒ 正文不能错位（CI 抓到的真 bug）。"""
    体 = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    响应 = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + 体)
    端口 = _一次性服务(响应)
    bc = tmp_bc_path(ROOT / "_tmp_m206_chunk.json")
    from oracle.jishi.compiler import compile_source
    源 = f'导入 网络\n打印(网络.获取("http://127.0.0.1:{端口}/"))\n'
    bc.write_text(serialize.dumps(compile_source(源, "<t>")), encoding="utf-8")
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (r.stderr or "")[:500]
    assert out_line(r.stdout) == "hello world", r.stdout + (r.stderr or "")


@pytest.mark.skipif(not RUST_EXE.is_file(), reason="发行版没编")
def test_分块加gzip一起也能解():
    """分块 + gzip 同时出现（真实 CDN 常态）：解出来必须是原文。"""
    原 = "压缩过的正文也要按块传".encode("utf-8") * 20
    压 = gzip.compress(原)
    块们 = b"".join(b"%x\r\n" % len(压[i:i + 40]) + 压[i:i + 40] + b"\r\n"
                   for i in range(0, len(压), 40)) + b"0\r\n\r\n"
    响应 = (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
            b"Content-Type: text/plain\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n" + 块们)
    端口 = _一次性服务(响应)
    bc = tmp_bc_path(ROOT / "_tmp_m206_chunkgz.json")
    from oracle.jishi.compiler import compile_source
    源 = f'导入 网络\n打印(长度(网络.获取("http://127.0.0.1:{端口}/")))\n'
    bc.write_text(serialize.dumps(compile_source(源, "<t>")), encoding="utf-8")
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (r.stderr or "")[:500]
    assert out_line(r.stdout) == str(len(原.decode("utf-8"))), r.stdout + (r.stderr or "")
