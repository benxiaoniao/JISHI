# -*- coding: utf-8 -*-
"""M107：网络 + 结构化数据（`网络` 请求头 / JSON 提交 / 状态码 / 下载；
`表格.从记录` / `转记录`；`html` 取文本 / 取链接 / 取表格）。

这一轮补的，都是从「接口汇总」探针（`examples/绕道探针/接口汇总/`）撞出来的
真实缺口：真实接口几乎都要鉴权头（`X-Token`）、只认 `application/json` 的 POST、
会返回 404/401/500 要你按状态码处理 —— 而原来 `网络` 只有 `获取`/`提交`/
`获取JSON` 三个函数、拿不到状态码也发不了 JSON。另外「字典列表 ↔ 表格」之间
没有直接转换（只能落临时 CSV 绕一圈）、网页只能正则硬挖。
"""

import io
import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from conftest import rust_exe_path, tmp_bc_path          # noqa: E402
from oracle.jishi import serialize                        # noqa: E402
from oracle.jishi.interpreter import run_source           # noqa: E402

RUST_EXE = rust_exe_path()


def _跑(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src, filename="<t>")
    return buf.getvalue()


def _字节码(src: str):
    bc = tmp_bc_path(ROOT / "_tmp_m107_bc.json")
    bc.write_text(serialize.dumps(
        __import__("oracle.jishi.compiler", fromlist=["compile_source"])
        .compile_source(src, "<t>")), encoding="utf-8")
    return bc


# ---------------------------------------------------------------------------
# 一、表格 ↔ 记录
# ---------------------------------------------------------------------------

def test_从记录_JSON记录直接进表格():
    assert _跑(
        '导入 表格\n'
        '令 单 = [{"客户":"甲","金额":12},{"客户":"乙","金额":3,"备注":"x"}]\n'
        '令 表 = 表格.从记录(单)\n'
        '打印(表格.表头(表))\n'
        '打印(表格.分组汇总(表, "客户", "金额"))\n'
    ) == (
        "['客户', '金额', '备注']\n"
        "{'甲': {'个数': 1, '总和': 12.0, '平均': 12.0, '最大': 12.0, '最小': 12.0}, "
        "'乙': {'个数': 1, '总和': 3.0, '平均': 3.0, '最大': 3.0, '最小': 3.0}}\n"
    )


def test_转记录_往返不失真():
    assert _跑(
        '导入 表格\n'
        '令 单 = [{"名":"甲","值":1},{"名":"乙","值":2}]\n'
        '令 回来 = 表格.转记录(表格.从记录(单))\n'
        '打印(回来)\n'
    ) == "[{'名': '甲', '值': 1}, {'名': '乙', '值': 2}]\n"


# ---------------------------------------------------------------------------
# 二、html 解析
# ---------------------------------------------------------------------------

def test_html取文本_去标签解实体压空白():
    assert _跑(
        '导入 html\n'
        '打印(html.取文本("<p>你好 <b>世界</b>&amp; 大家</p>"))\n'
        '打印(html.取文本("<script>var x=1;</script><p>正文</p>"))\n'
    ) == "你好 世界& 大家\n正文\n"


def test_html取链接_属性顺序单双引号都不怕():
    assert _跑(
        '导入 html\n'
        '令 页 = \'<a href="/a">一</a><a class="c" HREF=\\\'/b?x=1&amp;y=2\\\'>二</a>\'\n'
        '打印(html.取链接(页))\n'
    ) == "[['一', '/a'], ['二', '/b?x=1&y=2']]\n"


def test_html取表格():
    assert _跑(
        '导入 html\n'
        '令 页 = "<table><thead><tr><th>名</th><th>值</th></tr></thead>'
        '<tbody><tr><td>笔</td><td>2.5</td></tr></tbody></table>"\n'
        '打印(html.取表格(页))\n'
    ) == "[['名', '值'], ['笔', '2.5']]\n"


# ---------------------------------------------------------------------------
# 三、五执行器一致（离线部分：html + 表格）
# ---------------------------------------------------------------------------

_脚本 = (
    '导入 表格\n导入 html\n'
    '令 单 = [{"客户":"甲","金额":12},{"客户":"乙","金额":3,"备注":"x"}]\n'
    '令 表 = 表格.从记录(单)\n'
    '打印(表格.表头(表))\n'
    '打印(表格.分组汇总(表, "客户", "金额"))\n'
    '打印(表格.转记录(表))\n'
    '令 页 = "<ul><li><a href=\'/a?x=1&amp;y=2\'>第一 个</a></li></ul>'
    '<table><tr><th>名</th><th>值</th></tr><tr><td>笔</td><td>2.5</td></tr></table>"\n'
    '打印(html.取文本(页))\n'
    '打印(html.取链接(页))\n'
    '打印(html.取表格(页))\n'
)

_期望 = (
    "['客户', '金额', '备注']\n"
    "{'甲': {'个数': 1, '总和': 12.0, '平均': 12.0, '最大': 12.0, '最小': 12.0}, "
    "'乙': {'个数': 1, '总和': 3.0, '平均': 3.0, '最大': 3.0, '最小': 3.0}}\n"
    "[{'客户': '甲', '金额': 12, '备注': 空}, {'客户': '乙', '金额': 3, '备注': 'x'}]\n"
    "第一 个名值笔2.5\n"
    "[['第一 个', '/a?x=1&y=2']]\n"
    "[['名', '值'], ['笔', '2.5']]\n"
)


def test_五执行器一致():
    assert _跑(_脚本) == _期望
    if not RUST_EXE.is_file():
        pytest.skip("没有 rust 宿主产物")
    bc = _字节码(_脚本)
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:400]
    assert r.stdout.decode("utf-8", "replace") == _期望, "Rust VM 不一致"

    from shutil import which
    if which("node") is None:
        pytest.skip("环境里没有 node")
    r2 = subprocess.run(["node", str(ROOT / "oracle" / "node" / "index.js"),
                         str(bc)], capture_output=True, text=True,
                        encoding="utf-8")
    assert r2.returncode == 0, (r2.stderr or "")[:400]
    assert r2.stdout == _期望, f"Node 不一致：\n{r2.stdout!r}"


# ---------------------------------------------------------------------------
# 四、网络（要联网到本地样例站点）
# ---------------------------------------------------------------------------

class _网(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _回(self, 状态, 内容: bytes, 类型):
        self.send_response(状态)
        self.send_header("Content-Type", 类型)
        self.send_header("Content-Length", str(len(内容)))
        self.end_headers()
        self.wfile.write(内容)

    def do_GET(self):
        if self.headers.get("X-Token") != "sample-token-1234":
            self._回(401, b'{"err": "no token"}', "application/json")
            return
        if self.path == "/json":
            self._回(200, '{"名": "基石", "数": 3}'.encode("utf-8"),
                     "application/json")
        elif self.path == "/404":
            self._回(404, b'{"err": "not found"}', "application/json")
        elif self.path == "/dl":
            self._回(200, b"hello-bytes", "application/octet-stream")
        elif self.path.startswith("/echo"):
            # R101：把**服务端实际收到的原始请求行**回显出来 ——
            # 这样「中文有没有被自动编码」是可观测的（而不是只验证「没报错」）
            self._回(200, self.path.encode("utf-8"), "text/plain")
        else:
            self._回(200, b"ok", "text/plain")

    def do_POST(self):
        if self.path == "/confirm":
            if self.headers.get("Content-Type", "").split(";")[0].strip() \
                    != "application/json":
                self._回(415, b'{"err": "want json"}', "application/json")
                return
            长 = int(self.headers.get("Content-Length") or 0)
            self._回(200, self.rfile.read(长), "application/json")
        else:
            self._回(404, b"", "text/plain")


@pytest.fixture
def 网端口():
    httpd = HTTPServer(("127.0.0.1", 0), _网)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _跑引擎(src: str) -> dict:
    """同一段源码在四种执行器上跑，返回 `{名字: stdout}`。"""
    import tempfile
    import check_engines as ce
    from oracle.jishi.vm import VM
    from oracle.jishi.compiler import compile_source
    import oracle.jishi.cvm_bind as cvm_bind
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        os.environ.pop(k, None)
    run_dir = os.path.join(tempfile.gettempdir(), "_m107net")
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=True)
    rel = "net.jsh"
    open(os.path.join(run_dir, rel), "w", encoding="utf-8").write(src)
    lines = src.split("\n")
    out = {}
    with ce._in_dir(run_dir):
        out["树遍历"] = ce._run(lambda: run_source(src, rel), lines)[0]
        out["字节码VM"] = ce._run(
            lambda: VM(compile_source(src, rel), rel).run(), lines)[0]
        out["C VM"] = ce._run(lambda: cvm_bind.run_source_c(src, rel), lines)[0]
    out["Rust VM"] = ce._run_rust_vm(src, rel, run_dir)[0]
    return out


def test_网络_请求头与状态码与JSON提交(网端口):
    头 = '{"X-Token": "sample-token-1234"}'
    src = (
        '导入 网络\n'
        f'令 B = "http://127.0.0.1:{网端口}"\n'
        f'令 d = 网络.获取JSON(B + "/json", 10, {头})\n'
        '打印(d["名"], d["数"])\n'
        f'令 详 = 网络.获取详情(B + "/404", 10, {头})\n'
        '打印(详["状态"])\n'
        f'令 回 = 网络.提交JSON(B + "/confirm", {{"客户": "甲"}}, 10, {头})\n'
        '打印(回)\n'
    )
    期望 = (
        '基石 3\n'
        '404\n'
        '{"客户": "甲"}\n'
    )
    out = _跑引擎(src)
    assert len(set(out.values())) == 1, out
    assert next(iter(out.values())) == 期望, out


def test_网址里的中文自动编码(网端口):
    """🔴 台账 **R101**：网址里**直接写中文**就行 —— `网络` 会自动百分号编码。

    以前必须自己 `编码.url编码(关键字)` 再拼，否则报
    `'ascii' codec can't encode characters…`（那句跟「网址」两个字毫无关系，
    第一次撞上很难查）。

    判据不能只验「没报错」—— 让服务端**回显它收到的原始请求路径**，
    四种执行器必须回同一串，且那串里中文确实变成了 `%E4%B8%AD` 这种。
    """
    头 = '{"X-Token": "sample-token-1234"}'
    src = (
        '导入 网络\n'
        f'令 B = "http://127.0.0.1:{网端口}"\n'
        # 中文 + 空格；结构字符（`?` `=` `&`）必须原样保留
        f'打印(网络.获取(B + "/echo?q=中文 空格&n=1", 10, {头}))\n'
    )
    出 = _跑引擎(src)
    基准 = 出["树遍历"].strip()
    assert 基准 == "/echo?q=%E4%B8%AD%E6%96%87%20%E7%A9%BA%E6%A0%BC&n=1", 基准
    for 名, v in 出.items():
        assert v.strip() == 基准, f"{名} 与树遍历不一致：{v!r} vs {基准!r}"


def test_已编码的网址不会被二次编码(网端口):
    """R101 的另一半：**已经编码过的**网址必须原样通过（`%` 不能被转成 `%25`）。

    这条最容易做错 —— JS 的 `encodeURI` 就会把 `%` 编成 `%25`，于是
    `%E4%B8%AD` 变成 `%25E4%25B8%25AD`，请求直接发错地方。
    """
    头 = '{"X-Token": "sample-token-1234"}'
    src = (
        '导入 网络\n'
        f'令 B = "http://127.0.0.1:{网端口}"\n'
        f'打印(网络.获取(B + "/echo?q=%E4%B8%AD%E6%96%87", 10, {头}))\n'
    )
    出 = _跑引擎(src)
    基准 = 出["树遍历"].strip()
    assert 基准 == "/echo?q=%E4%B8%AD%E6%96%87", 基准
    for 名, v in 出.items():
        assert v.strip() == 基准, f"{名} 不一致：{v!r}"


def test_网络_下载(网端口):
    头 = '{"X-Token": "sample-token-1234"}'
    src = (
        '导入 网络\n'
        f'令 B = "http://127.0.0.1:{网端口}"\n'
        f'令 n = 网络.下载(B + "/dl", "_m107_dl.bin", 10, {头})\n'
        '打印(n)\n'
    )
    out = _跑引擎(src)
    assert len(set(out.values())) == 1, out
    assert next(iter(out.values())) == "11\n", out


def test_网络_非2xx统一报错(网端口):
    """非 2xx 的报错要带**状态码**，且四个引擎措辞一致。"""
    import check_engines as ce
    from oracle.jishi.errors import JishiError
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        os.environ.pop(k, None)
    with pytest.raises(JishiError) as ei:
        _跑(f'导入 网络\n网络.获取("http://127.0.0.1:{网端口}/404", 10, '
            '{"X-Token": "sample-token-1234"})\n')
    assert "HTTP 404" in str(ei.value), str(ei.value)

    # Rust 侧措辞要一致（含「访问」前缀 + 状态码）
    src = ('导入 网络\n'
           f'网络.获取("http://127.0.0.1:{网端口}/404", 10, '
           '{"X-Token": "sample-token-1234"})\n')
    bc = _字节码(src)
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True)
    assert r.returncode != 0
    assert "HTTP 404" in r.stderr.decode("utf-8", "replace")
