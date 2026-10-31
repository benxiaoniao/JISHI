# -*- coding: utf-8 -*-
"""M158 · 教程第 13 章（`HTTP服务端`）。

## 一、纯函数那一半：五个执行器逐字相同

`解析请求` / `构造响应` / `路由表` 只是文本处理，五个引擎都能跑 ⇒ 进语料、逐字节对拍。
示例 `examples/tutorial/13_HTTP接口.jsh` 就是「不起服务也能测」的写法。

## 二、真服务那一半：只有三个平台能起

`启动` 要 socket。Node 宿主与浏览器试玩页**明确报错**（D81 的取舍），
所以 `examples/tutorial/13_HTTP服务.jsh` 在 `check_engines` / `check_walk` / `check_cli`
三处**显式登记**（各自写了理由）。它的端到端由本文件的 `计时` 用例真打 HTTP 验。

## 三、这一章顺带修的三处「卡片说谎」（见 `test_m158_card_truth.py`）

写教程时照卡片写代码，连撞三次：`列表.索引` 说给 -1（实际报错）、
`列表.排序按` 说原地改（实际返回新列表）、`文件.读行` 说给空文本（实际给 `空`）。
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path, 去掉遮蔽提示          # noqa: E402

示例接口 = ROOT / "examples" / "tutorial" / "13_HTTP接口.jsh"
示例服务 = ROOT / "examples" / "tutorial" / "13_HTTP服务.jsh"

CVM驱动 = '''\
import sys
sys.path.insert(0, r"{root}")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(newline="\\n")
    except Exception:
        pass
from oracle.jishi import cvm_bind, errors as E

src = open("程序.jsh", encoding="utf-8").read()
try:
    cvm_bind.run_source_c(src, filename="程序.jsh")
except E.JishiError as e:
    e.with_source(src.replace("\\r\\n", "\\n").split("\\n"))
    sys.stderr.write(e.render() + "\\n")
    sys.exit(1)
'''


def _五份(源码: str, tmp_path: Path) -> dict:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
    (tmp_path / "驱动.py").write_text(
        CVM驱动.format(root=str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    表 = {
        "树遍历": [sys.executable, "-m", "oracle.jishi.cli", "--tree", "程序.jsh"],
        "Python VM": [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
        "C VM": [sys.executable, "驱动.py"],
    }
    rs = rust_exe_path()
    if rs.is_file():
        表["Rust 宿主"] = [str(rs), "程序.jsh"]
    node = shutil.which("node")
    if node:
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
        表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"),
                           "程序.json"]
    出 = {}
    for 名字, 命令 in 表.items():
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                           timeout=120, input=b"")
        出[名字] = 去掉遮蔽提示((r.stdout + r.stderr).decode("utf-8", "replace"))
    return 出


def _一致(源码: str, tmp_path: Path) -> str:
    出 = _五份(源码, tmp_path)
    值 = set(出.values())
    assert len(值) == 1, "五个执行器说法不一致：\n" + "\n".join(
        f"—— {k} ——\n{v}" for k, v in 出.items())
    return 值.pop()


# ---------------------------------------------------------------------------
# 一、纯函数：示例 + 路由语义 + 响应拼装
# ---------------------------------------------------------------------------

def test_纯函数示例五引擎逐字一致(tmp_path):
    文本 = _一致(示例接口.read_text(encoding="utf-8"), tmp_path)
    for 片段 in ("已注册： ['GET /', 'GET /成绩/:姓名', 'GET /排行']",
               "HTTP/1.1 200 OK",
               'GET /成绩/小明 → HTTP/1.1 200 OK ｜ {"姓名": "小明", "语文": 92, "数学": 88}',
               "GET /成绩/不存在 → HTTP/1.1 404 Not Found",
               'GET /排行?前=2 → HTTP/1.1 200 OK ｜ [{"名次": 1, "姓名": "小红", "总分": 182}',
               "GET /没有这个路径 → HTTP/1.1 404 Not Found ｜ 没有这一页",
               "POST / → HTTP/1.1 404 Not Found ｜ 没有这一页"):
        assert 片段 in 文本, f"缺「{片段}」：\n{文本}"


def test_末尾斜杠严格区分(tmp_path):
    """`/a` 与 `/a/` 是两条路 —— 教程 §13.8 坑③。"""
    文本 = _一致(
        '导入 HTTP服务端\n'
        '导入 HTTP路由\n'
        '函数 甲(请求, 参数)：\n'
        '    返回 HTTP服务端.构造响应(200, "甲")\n'
        '令 表 = HTTP路由.路由表()\n'
        'HTTP路由.加路由(表, "GET", "/a", 甲)\n'
        '打印(HTTP路由.匹配(表, "GET", "/a") != 空)\n'
        '打印(HTTP路由.匹配(表, "GET", "/a/") == 空)\n', tmp_path)
    assert 文本.strip().splitlines() == ["真", "真"], 文本


def test_参数段不吞空段(tmp_path):
    """`/成绩/` 匹配不上 `/成绩/:姓名`（否则 `参数["姓名"]` 是空串）。"""
    文本 = _一致(
        '导入 HTTP服务端\n'
        '导入 HTTP路由\n'
        '函数 甲(请求, 参数)：\n'
        '    返回 HTTP服务端.构造响应(200, 参数["姓名"])\n'
        '令 表 = HTTP路由.路由表()\n'
        'HTTP路由.加路由(表, "GET", "/成绩/:姓名", 甲)\n'
        '打印(HTTP路由.匹配(表, "GET", "/成绩/") == 空)\n'
        '令 命中 = HTTP路由.匹配(表, "GET", "/成绩/小明")\n'
        '打印(命中["参数"])\n', tmp_path)
    assert 文本.strip().splitlines() == ["真", "{'姓名': '小明'}"], 文本


def test_方法大写后比较且星号通吃(tmp_path):
    文本 = _一致(
        '导入 HTTP服务端\n'
        '导入 HTTP路由\n'
        '函数 甲(请求, 参数)：\n'
        '    返回 HTTP服务端.构造响应(200, "甲")\n'
        '令 表 = HTTP路由.路由表()\n'
        'HTTP路由.加路由(表, "get", "/小写", 甲)\n'
        'HTTP路由.加路由(表, "*", "/随便", 甲)\n'
        '打印(HTTP路由.匹配(表, "GET", "/小写") != 空)\n'
        '打印(HTTP路由.匹配(表, "DELETE", "/随便") != 空)\n', tmp_path)
    assert 文本.strip().splitlines() == ["真", "真"], 文本


def test_内容长度按字节算(tmp_path):
    """`你好` 是 2 个字符、**6 个字节** —— 教程 §13.3 特意点了一句。"""
    文本 = _一致('导入 HTTP服务端\n'
              '打印(HTTP服务端.构造响应(200, "你好", "text/html; charset=utf-8"))\n',
              tmp_path)
    assert "Content-Length: 6" in 文本, 文本
    assert "Connection: close" in 文本, 文本


def test_请求头名字统一小写且查询已解码(tmp_path):
    文本 = _一致('导入 HTTP服务端\n'
              '令 请求 = HTTP服务端.解析请求('
              '"GET /查?a=1&b=%E4%BD%A0 HTTP/1.1\\r\\nContent-Type: text/plain\\r\\n\\r\\n")\n'
              '打印(请求["查询"])\n'
              '打印(请求["请求头"])\n', tmp_path)
    行 = 文本.strip().splitlines()
    assert 行[0] == "{'a': '1', 'b': '你'}", 行
    assert 行[1] == "{'content-type': 'text/plain'}", 行


# ---------------------------------------------------------------------------
# 二、平台边界：Node 明确报错（不是「算得不一样」）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("node"), reason="本机无 Node.js")
def test_Node_宿主起不了服务时说明白(tmp_path):
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    源码 = '导入 HTTP服务端\nHTTP服务端.启动(8000, 空, 1)\n'
    (tmp_path / "程序.json").write_text(
        serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
    r = subprocess.run([shutil.which("node"),
                        str(ROOT / "oracle" / "node" / "index.js"), "程序.json"],
                       cwd=str(tmp_path), capture_output=True, timeout=60, input=b"")
    文本 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert "这个平台起不了 HTTP 服务端" in 文本, 文本
    assert "同步" in 文本 and "事件循环" in 文本, f"没说清为什么：{文本!r}"


# ---------------------------------------------------------------------------
# 三、真起服务（进串行节：真占端口 + 等墙上时间）
# ---------------------------------------------------------------------------

def _端口空闲(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _挑端口(首选: int = 8137) -> int:
    for 候选 in (首选, 首选 + 1, 首选 + 2, 0):
        if 候选 == 0 or _端口空闲(候选):
            return 候选
    return 0


def _等端口(port: int, 上限秒: float = 20.0) -> bool:
    截止 = time.time() + 上限秒
    while time.time() < 截止:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _取(port: int, path: str) -> tuple:
    # ⚠️ URL 里带中文要先 percent-encode，否则 urllib 拼请求行时报 ascii 编码错
    地址 = f"http://127.0.0.1:{port}" + urllib.parse.quote(path)
    try:
        with urllib.request.urlopen(地址, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:            # 404 也要能读到正文
        return e.code, e.read().decode("utf-8", "replace")


def _起(源文件: Path, 端口: int, tmp_path: Path):
    """把示例里写死的 8000 换成测试端口再跑（写法变了要报出来，不许悄悄失效）。"""
    原文 = 源文件.read_text(encoding="utf-8")
    # ⚠️ 只认「变量那一行」：以前认 `启动(8000,`，结果先命中了**注释里**那句，
    #    真调用还留在 8000 —— 服务起在 8000、测试连 8137，20 秒等不到端口。
    旧 = "令 端口 = 8000"
    assert 旧 in 原文, (
        f"示例里找不到 `{旧}` —— 端口写法改了吗？这条替换失效了，必须跟着改")
    副本 = tmp_path / 源文件.name
    副本.write_text(原文.replace(旧, f"令 端口 = {端口}", 1),
                    encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.Popen(
        [sys.executable, "-m", "oracle.jishi.cli", str(副本)],
        cwd=str(tmp_path), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL)


@pytest.mark.计时
def test_示例真的能起服务并回应(tmp_path):
    """起进程 → 打三条路 → 收满 10 个请求后**自己退出** → 输出里有统计行。

    ⚠️ 打 `计时`（进串行节）：真占端口、等墙上时间，并行跑会互相踩。
    """
    assert 示例服务.is_file(), f"示例没了：{示例服务}"
    端口 = _挑端口()
    proc = _起(示例服务, 端口, tmp_path)
    try:
        assert _等端口(端口), "20 秒内没起来"
        状态, 正文 = _取(端口, "/")
        assert 状态 == 200 and "成绩查询" in 正文, (状态, 正文)
        状态, 正文 = _取(端口, "/成绩/小明")
        assert 状态 == 200 and '"语文": 92' in 正文, (状态, 正文)
        状态, 正文 = _取(端口, "/排行")
        assert 状态 == 200 and "名次" in 正文, (状态, 正文)
        状态, 正文 = _取(端口, "/没有这个路径")
        assert 状态 == 404 and "没有这一页" in 正文, (状态, 正文)
        # 示例设的是「服务 10 个请求就退」：把剩下的补齐，让它自然结束
        for _ in range(10):
            if proc.poll() is not None:
                break
            _取(端口, "/")
        out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    文本 = out.decode("utf-8", "replace") + err.decode("utf-8", "replace")
    assert "服务已起：http://127.0.0.1:" in 文本, 文本
    assert "服务已停：共处理" in 文本, f"没干净退出：{文本!r}"


@pytest.mark.计时
def test_处理函数抛错回500而服务继续(tmp_path):
    """教程 §13.8 坑④：一条坏请求不该让整个本地服务挂掉。"""
    端口 = _挑端口(8151)
    源 = ('导入 HTTP服务端\n'
         '导入 HTTP路由\n'
         '函数 炸(请求, 参数)：\n'
         '    令 x = 1 / 0\n'
         '函数 好(请求, 参数)：\n'
         '    返回 HTTP服务端.构造响应(200, "还活着")\n'
         '令 表 = HTTP路由.路由表()\n'
         'HTTP路由.加路由(表, "GET", "/炸", 炸)\n'
         'HTTP路由.加路由(表, "GET", "/好", 好)\n'
         '函数 处理(请求)：\n'
         '    令 命中 = HTTP路由.匹配(表, 请求["方法"], 请求["路径"])\n'
         '    如果 命中 == 空:\n'
         '        返回 HTTP服务端.构造响应(404, "没有这一页")\n'
         '    返回 命中["处理"](请求, 命中["参数"])\n'
         '令 端口 = 8000\n'
         # 只要 2 个请求（/炸 与 /好）—— 凑够就自己退出，测试不用 kill
         'HTTP服务端.启动(端口, 处理, 2)\n')
    (tmp_path / "炸.jsh").write_text(源, encoding="utf-8", newline="\n")
    proc = _起(tmp_path / "炸.jsh", 端口, tmp_path)
    try:
        assert _等端口(端口), "20 秒内没起来"
        状态, 正文 = _取(端口, "/炸")
        assert 状态 == 500, (状态, 正文)
        assert "不能除以零" in 正文, 正文
        状态, 正文 = _取(端口, "/好")          # 服务**还活着**
        assert 状态 == 200 and "还活着" in 正文, (状态, 正文)
        out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, (out + err).decode("utf-8", "replace")
