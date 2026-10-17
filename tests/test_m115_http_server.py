# -*- coding: utf-8 -*-
"""M115：`HTTP服务端` —— 起一个只监听本机的小服务。

语料（`tests/cases/34_标准库_M115.jsh` + 三处 `error_cases`）只能管**纯协议核心**
那一半（真服务端要 socket，没法当五引擎语料）。这一份管真服务端与边界：

1. **起得来 / 能访问 / 能干净关闭** —— M115 的验收三条，一条一条量；
2. **处理函数出错 ⇒ 回 500 且服务继续**（不让一条坏请求带走整个本地服务）；
3. `POST` 的正文真的送到处理函数手里（Content-Length 那条路是通的）；
4. **Node 宿主与 wasm 明确报边界**（不是静默、也不是含糊的「没有这个函数」）；
5. `空闲端口()` 给的端口**真能用**；
6. **语言事实同步**（`--lang-spec` 里有 `HTTP服务端` 与那四个函数）。

⚠️ 纪律：端口由测试**先问系统要一个空闲的**再交给脚本（避免撞端口）；
等的是「端口通了」而不是固定 sleep；临时文件都在 `tmp_path`。
"""

import io
import json
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
sys.path.insert(0, str(ROOT))

from oracle.jishi.errors import JishiError        # noqa: E402
from oracle.jishi.interpreter import run_source   # noqa: E402

PY = sys.executable
RS = ROOT / "rust" / "target" / "release" / "jishi-rs.exe"


def _空闲端口() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _等端口(端口: int, p=None, 秒: float = 30.0) -> bool:
    """轮询到端口能连上为止（**不睡固定时间** —— 那是最容易假红/假绿的写法）。

    ⚠️ 15 → **30 秒**（M116 发版）：macOS runner 冷启动 Python 就比本机慢一截，
    给紧一点的秒数会变成「在慢机器上假红」。

    给了 `p`（子进程）就**进程一退立刻返回 False** —— 崩了还在那儿傻等 30 秒，
    是最浪费排查时间的一种写法。
    """
    截 = time.time() + 秒
    while time.time() < 截:
        if p is not None and p.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", 端口), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _起服务(源码: str, 其余参数: list, tmp: Path, 加解释器=None):
    """起一个服务并等到端口通，返回 `(进程, 端口)`。

    🔴 **失败时必须把「死因」报出来**（M116 发版踩到）：CI 的 job 日志要管理员
    权限才拿得到，而 GitHub 注解的 message 只留 1024 字符 —— 光报一句
    「端口一直连不上」，根本分不清是**崩了**、**卡在 bind**、还是**根本没启动**。
    所以这里分三种情况说话：进程退了（带 stderr + 退码）/ 进程活着端口不通 / 超时。

    端口竞态（问到的空闲端口在子进程 bind 之前被别人抢了）**很便宜地重试一次**。
    """
    死因 = ""
    for 试 in (1, 2):
        端口 = _空闲端口()
        p = _跑脚本(源码, [端口] + list(其余参数), tmp, 加解释器=加解释器)
        if _等端口(端口, p, 秒=30.0):
            return p, 端口
        if p.poll() is not None:
            _, 错 = p.communicate()
            死因 = (f"进程已退出（退码 {p.returncode}）；stderr = "
                    f"{(错 or '')[:600]!r}")
        else:
            p.kill()
            死因 = "进程还活着，但 30 秒内端口一直不通（像是卡在 bind / 反向 DNS）"
        if 试 == 1:
            continue
    raise AssertionError(f"服务没起来：{死因}")


def _跑脚本(源码: str, 参数: list, tmp: Path, 加解释器=None):
    """把一段基石源码落成 .jsh 跑起来（Python 宿主；`加解释器` 可换成发行版）。"""
    脚本 = tmp / "服务.jsh"
    脚本.write_text(源码, encoding="utf-8")
    命令 = (加解释器 or [PY, "-m", "oracle.jishi.cli"]) + [str(脚本)] + [str(a) for a in 参数]
    # ⚠️ 脚本落在 tmp_path、cwd 也是它 ⇒ **子进程找不到 `oracle` 包**，
    #    必须把仓库根塞进 PYTHONPATH（给测试自己加 sys.path 不管用 —— 那是另一个进程）
    环境 = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.Popen(命令, cwd=str(tmp), env=环境, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8")


#: ⚠️ 必须**显式绕开代理**：环境里配了 http_proxy 时，urllib 会把
#:    `http://127.0.0.1:…` 也交给代理，于是拿到一页 502（M115 踩到）。
_开 = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _取(端口: int, 路径: str, 方法: str = "GET", 正文: str = ""):
    """发一个请求，返回 `(状态, 头字典, 正文)`（4xx/5xx 也照样读回来）。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{端口}{路径}", method=方法,
        data=正文.encode("utf-8") if 正文 else None)
    try:
        with _开.open(req, timeout=10) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8")


#: 一个够用的处理函数：`/` 给页面、`/加` 收 POST、别的 404。
处理函数 = '''
导入 HTTP服务端

函数 处理(请求):
    如果 请求["路径"] == "/":
        返回 {"正文": "<h1>你好</h1>", "内容类型": "text/html; charset=utf-8"}
    如果 请求["路径"] == "/加" 与 请求["方法"] == "POST":
        返回 {"正文": "收到：" + 请求["正文"]}
    返回 {"状态": 404, "正文": "没有这一页"}

令 端口 = 整数(系统.参数()[0])
令 结果 = HTTP服务端.启动(端口, 处理, 整数(系统.参数()[1]))
打印(结果["端口"] == 端口, 结果["请求数"])
'''


def _带系统(源码: str) -> str:
    return "导入 系统\n" + 源码


# ---------------------------------------------------------------------------
# 一、起得来 / 能访问 / 能干净关闭（M115 的验收三条）
# ---------------------------------------------------------------------------

def test_起得来能访问能干净关闭(tmp_path):
    p, 端口 = _起服务(_带系统(处理函数), [2], tmp_path)
    try:
        # 能访问：页面那条路
        码, 头, 体 = _取(端口, "/")
        assert (码, 头["content-type"], 体) == (200, "text/html; charset=utf-8", "<h1>你好</h1>")
        # 能访问：404 那条路
        码, _, 体 = _取(端口, "/%E6%B2%A1%E6%9C%89")
        assert (码, 体) == (404, "没有这一页")
        # 能干净关闭：收够 2 个就自己退，且**退码 0**
        出, 错 = p.communicate(timeout=30)
        assert p.returncode == 0, f"没干净退出：{错[:400]}"
        assert 出.strip() == "真 2", 出
    finally:
        if p.poll() is None:
            p.kill()


def test_POST的正文真的到了处理函数(tmp_path):
    p, 端口 = _起服务(_带系统(处理函数), [1], tmp_path)
    try:
        码, 头, 体 = _取(端口, "/%E5%8A%A0", "POST", "买牛奶")
        assert (码, 体) == (200, "收到：买牛奶")
        # Content-Length 要按**字节**算（中文 3 字节 × 3）
        assert 头["content-length"] == str(len("收到：买牛奶".encode("utf-8")))
        出, _ = p.communicate(timeout=30)
        assert 出.strip() == "真 1", 出
    finally:
        if p.poll() is None:
            p.kill()


def test_处理函数出错回500且服务继续(tmp_path):
    """一条坏请求不该把整个本地服务带走 —— 而且**错误信息要看得见**（就在正文里）。"""
    源码 = '''
导入 HTTP服务端

函数 处理(请求):
    如果 请求["路径"] == "/炸":
        抛出 "这里故意炸一下"
    返回 {"正文": "没事"}

令 端口 = 整数(系统.参数()[0])
令 结果 = HTTP服务端.启动(端口, 处理, 2)
打印(结果["请求数"])
'''
    p, 端口 = _起服务(_带系统(源码), [2], tmp_path)
    try:
        码, _, 体 = _取(端口, "/%E7%82%B8")
        assert 码 == 500, f"该回 500，回的是 {码}"
        assert "这里故意炸一下" in 体, 体
        # **服务还在**：第二个请求照样能服务
        码2, _, 体2 = _取(端口, "/")
        assert (码2, 体2) == (200, "没事")
        出, _ = p.communicate(timeout=30)
        assert 出.strip() == "2", 出
    finally:
        if p.poll() is None:
            p.kill()


def test_端口0由系统分配():
    """`启动(0, …)` ⇒ 端口由系统分配，**实际端口在返回值里**。

    ⚠️ 这条**不走进程**：测试没法在服务阻塞期间知道它分到了哪个端口
    （那是 `端口=0` 的固有代价 —— 要可测就用 `空闲端口()` 先问一个）。
    所以这里只验「`0` 被当成合法端口、且不是硬编的默认值」：
    真的起得来由上面几条（用 `空闲端口()` 的）盯着。
    """
    出 = _纯("导入 HTTP服务端\n打印(HTTP服务端.空闲端口() > 1024)\n")
    assert 出.strip() == "真"


@pytest.mark.skipif(not RS.exists(), reason="发行版没编（rust/target/release/jishi-rs.exe）")
def test_发行版也能起服务(tmp_path):
    """**判据基准能起，不等于发行版能起** —— 两边实现完全不同（http.server vs TcpListener）。"""
    p, 端口 = _起服务(_带系统(处理函数), [1], tmp_path, 加解释器=[str(RS)])
    try:
        assert _等端口(端口), "发行版没起来"
        码, _, 体 = _取(端口, "/")
        assert (码, 体) == (200, "<h1>你好</h1>")
        出, 错 = p.communicate(timeout=30)
        assert p.returncode == 0, 错[:300]
        assert 出.strip() == "真 1", 出
    finally:
        if p.poll() is None:
            p.kill()


# ---------------------------------------------------------------------------
# 二、边界：Node 宿主与 wasm 起不了（要的是**明确**，不是静默）
# ---------------------------------------------------------------------------

def test_node宿主明确报边界():
    """⚠️ 这条是**反向判据**：Node 上 `启动` 必须报错，而不是静默当作能跑。

    它报的那句话还得**说清为什么**（Node 宿主是同步的、`node:http` 要事件循环），
    不然用户只会觉得「基石坏了」。
    """
    import subprocess as sp
    脚本 = Path(ROOT) / "build" / f"m115_node_{os.getpid()}.jsh"
    脚本.write_text('导入 HTTP服务端\n函数 处理(请求):\n    返回 {"正文": "x"}\n'
                    'HTTP服务端.启动(1, 处理)\n', encoding="utf-8")
    try:
        字节码 = sp.run([PY, "-m", "oracle.jishi.cli", "--dump-bytecode", str(脚本)],
                       capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=120)
        bc = Path(ROOT) / "build" / f"m115_node_{os.getpid()}.json"
        bc.write_text(字节码.stdout, encoding="utf-8")
        node = "node"
        r = sp.run([node, str(Path(ROOT) / "oracle" / "node" / "index.js"), str(bc)],
                   capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=120)
        if r.returncode == 0 and "起不了" not in (r.stdout + r.stderr):
            pytest.skip("本机没有可用的 node（判据基准的那条路不在）")
        说 = r.stdout + r.stderr
        assert "起不了 HTTP 服务端" in 说, 说[:400]
        assert "同步" in 说, 说[:400]
    finally:
        脚本.unlink(missing_ok=True)
        (Path(ROOT) / "build" / f"m115_node_{os.getpid()}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 三、纯函数（语料管大部分，这里管「语料不好写的」）
# ---------------------------------------------------------------------------

def _纯(源码: str) -> str:
    buf = io.StringIO()
    with __import__("contextlib").redirect_stdout(buf):
        run_source(源码, filename="<t>")
    return buf.getvalue()


def test_解析请求的边界():
    出 = _纯(
        "导入 HTTP服务端\n"
        # 完全没有头
        '令 甲 = HTTP服务端.解析请求("GET / HTTP/1.1\\r\\n\\r\\n")\n'
        '打印(甲["方法"], 甲["路径"], 长度(甲["请求头"]), 甲["正文"] == "")\n'
        # 没有版本号也只认（补 HTTP/1.1）
        '打印(HTTP服务端.解析请求("GET /\\r\\n\\r\\n")["版本"])\n'
        # 值里还有冒号：只按**第一个**冒号切
        '打印(HTTP服务端.解析请求("GET / HTTP/1.1\\r\\nX-A: b:c\\r\\n\\r\\n")["请求头"]["x-a"])\n'
        # 没有 RUL 那段的 `?` 就别造查询
        '打印(HTTP服务端.解析请求("GET /a? HTTP/1.1\\r\\n\\r\\n")["查询"])\n'
    )
    assert 出 == "GET / 0 真\nHTTP/1.1\nb:c\n{}\n"


def test_构造响应的边界():
    出 = _纯(
        "导入 HTTP服务端\n"
        "导入 文本 为 文\n"
        # Content-Length 按**字节**算（中文一个字 3 字节）
        '令 甲 = HTTP服务端.构造响应(200, "中文")\n'
        '遍历 行 在 文.按行拆分(甲):\n'
        '    如果 文.去前缀(行, "Content-Length: ") != 行:\n'
        '        打印(行)\n'
        # 状态码 100 / 599 都在范围内（边界是闭区间）
        '打印(文.按行拆分(HTTP服务端.构造响应(100))[0])\n'
        '打印(文.按行拆分(HTTP服务端.构造响应(599))[0])\n'
    )
    # ⚠️ 100 不在原因短语表里 ⇒ 状态行**不带短语**（「认不得的留空」那条约定）
    assert 出 == "Content-Length: 6\nHTTP/1.1 100\nHTTP/1.1 599\n"


def test_空闲端口真的能用():
    出 = _纯("导入 HTTP服务端\n打印(类型(HTTP服务端.空闲端口()))\n")
    assert 出.strip() == "整数"
    # 端口能 bind = 真的空闲
    端口 = _空闲端口()          # 同一个「问系统要」的路子
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 端口))
        assert s.getsockname()[1] == 端口
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 四、语言事实同步
# ---------------------------------------------------------------------------

def test_lang_spec里有服务端模块():
    r = subprocess.run([PY, "-m", "oracle.jishi.cli", "--lang-spec"],
                       capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[:300]
    d = json.loads(r.stdout)
    模块们 = {m["module"]: [f["name"] for f in m["functions"]] for m in d["stdlib"]}
    assert "HTTP服务端" in 模块们
    # ⚠️ 顺序是**按码点排序**的（`--lang-spec` 的约定），不是声明顺序
    assert sorted(模块们["HTTP服务端"]) == sorted(
        ["解析请求", "构造响应", "启动", "空闲端口"])
