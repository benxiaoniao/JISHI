# -*- coding: utf-8 -*-
"""基石标准库：HTTP 服务端（M115）。起一个**只监听本机**的小服务。

分两半，边界如实说：

| 一半 | 函数 | 哪些平台能跑 |
|---|---|---|
| **纯协议核心**（只是文本处理） | `解析请求` / `构造响应` | **五个引擎都能跑** ⇒ 进语料、逐字节对拍 |
| **真服务端**（要 socket） | `启动` / `空闲端口` | **Python / Rust / C VM** 能起；<br>**Node 宿主**与**浏览器试玩页（wasm）明确报错** |

⚠️ 为什么 Node 起不了：`node:http` 是**异步 + 事件循环**，而基石的 VM 是**同步**的
—— 没有「同步泵事件循环」的 API，阻塞住了事件循环就跑不动（除非把服务端塞到子进程里，
那又调不到你的基石函数了）。取舍见 `docs/设计决策.md` **D81**。

## 三条约定（跨引擎一致的前提）

1. **只监听 `127.0.0.1`** —— 局域网里的手机连不上，这是**安全默认**。
2. **一问一答就关**（`Connection: close`，不做 keep-alive）—— 本地小服务够用，
   而且「一个请求 = 一次 `处理函数` 调用」，行为最好推理。
3. **正文按 UTF-8**：不是合法 UTF-8 的字节会被替换成 `` （本地小服务面向文本；
   要传二进制用 `编码.base64编码` 转一道）。

## 典型用法

```python
导入 HTTP服务端

函数 处理(请求):
    如果 请求["路径"] == "/":
        返回 {"正文": "<h1>你好</h1>", "内容类型": "text/html; charset=utf-8"}
    返回 {"状态": 404, "正文": "没有这一页"}

HTTP服务端.启动(8000, 处理, 最多请求数=1)   # 收 1 个请求就**干净退出**
```
"""

from __future__ import annotations

import socket
from urllib.parse import unquote_to_bytes

from ..errors import RunTypeError, RunValueError
from ..runtime import type_name

__all__ = ["解析请求", "构造响应", "启动", "空闲端口"]

#: 状态码 → 原因短语。**认不得的状态码就留空**（不硬编一个假的英文短语）。
_原因 = {
    200: "OK", 201: "Created", 202: "Accepted", 204: "No Content",
    301: "Moved Permanently", 302: "Found", 303: "See Other",
    304: "Not Modified", 307: "Temporary Redirect", 308: "Permanent Redirect",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    409: "Conflict", 413: "Content Too Large", 415: "Unsupported Media Type",
    429: "Too Many Requests",
    500: "Internal Server Error", 501: "Not Implemented",
    502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout",
}

_默认内容类型 = "text/plain; charset=utf-8"

#: 响应的**头名字**里不许用户自己写的（框架自己填，再写一遍会打架）。
#: ⚠️ 与 `网络` 模块那条对称：**头名与头值都只能是 ASCII / latin-1**。
_自填 = {"content-length", "content-type", "connection"}


def _要整数(值, 谁: str) -> int:
    """**只收整数**（布尔、浮点、文本都不收）—— 三宿主同一句文案。

    ⚠️ 不能用 `int(值)`：它会把 `200.5` 悄悄截成 `200`、把 `"8000"` 也认了，
    那样 Python 收、Rust / Node 不收，同一段程序换个引擎就报错。
    """
    if isinstance(值, bool) or not isinstance(值, int):
        raise RunTypeError(f"{谁}要是整数")
    return 值


def _要文本(值, 谁: str) -> str:
    if isinstance(值, str):
        return 值
    raise RunTypeError(f"{谁}要是文本，得到了「{type_name(值)}」")


def _要字典(值, 谁: str) -> dict:
    if 值 is None:
        return {}
    if isinstance(值, dict):
        return 值
    raise RunTypeError(f"{谁}要是一个字典，得到了「{type_name(值)}」")


def _解百分号(片段: str) -> str:
    """`%XX` 解码（UTF-8）。⚠️ **`+` 不当作空格** —— 与 `编码.url解码` 一个口径。"""
    return unquote_to_bytes(片段).decode("utf-8", "replace")


def _去空白(文本: str) -> str:
    """去两端空白 —— **只去空格与制表符**。

    ⚠️ 不能用 `str.strip()`：它按**全 Unicode 空白**去，各语言那张表**对不齐**
    （M107 `html` 那条教训同族：`isspace` / `\\s` 在 U+001C–1F、U+0085 上不一样）。
    """
    return 文本.strip(" \t")


def _切段(文本: str) -> list:
    """按**空格 / 制表符**切（丢掉空段）。请求行用它，同样是为了跨语言对齐。"""
    清洗 = _去空白(文本).replace("\t", " ")
    return [p for p in 清洗.split(" ") if p]


def 解析请求(请求文本) -> dict:
    """把一段**原始 HTTP 请求**文本解析成字典。纯函数，不碰 socket。

    返回 `{"方法", "路径", "查询", "版本", "请求头", "正文"}`：

    - `请求头` 的名字**统一成小写**（HTTP 头名本来就不区分大小写）；
    - `查询` 是 `?a=1&b=2` 解出来的字典（值做过 `%XX` 解码）；
    - 头与正文按 `\\r\\n\\r\\n` 分开；正文按 UTF-8 解（不合法就替换）。
    """
    原始 = _要文本(请求文本, "「解析请求」的参数")
    if "\r\n\r\n" in 原始:
        头部, 正文 = 原始.split("\r\n\r\n", 1)
    else:
        头部, 正文 = 原始, ""
    行们 = 头部.split("\r\n")
    if not 行们 or not _去空白(行们[0]):
        raise RunValueError("这不是一个 HTTP 请求：第一行是空的")
    首行 = _切段(行们[0])
    if len(首行) < 2:
        raise RunValueError(
            f"这不是一个 HTTP 请求：第一行该是「方法 路径 版本」，现在是「{行们[0]}」")
    方法 = 首行[0]
    目标 = 首行[1]
    版本 = 首行[2] if len(首行) > 2 else "HTTP/1.1"
    if "?" in 目标:
        路径, 查询串 = 目标.split("?", 1)
    else:
        路径, 查询串 = 目标, ""
    查询 = {}
    for 片段 in 查询串.split("&"):
        if not 片段:
            continue
        键, _, 值 = 片段.partition("=")
        查询[_解百分号(键)] = _解百分号(值)
    请求头 = {}
    for 行 in 行们[1:]:
        行 = _去空白(行)
        if not 行:
            continue
        if ":" not in 行:
            raise RunValueError(f"请求头这一行没有冒号：「{行}」")
        名字, _, 值 = 行.partition(":")
        请求头[_去空白(名字).lower()] = _去空白(值)
    return {
        "方法": 方法,
        "路径": _解百分号(路径),
        "查询": 查询,
        "版本": 版本,
        "请求头": 请求头,
        "正文": 正文,
    }


# ⚠️ 默认值写**字面量**而不是 `_默认内容类型` 那个常量：语言卡的签名是
#    从 AST 反解的，常量名会原样漏进卡片（模型看到「内容类型=_默认内容类型」
#    只会一脸问号）。
def 构造响应(状态: int = 200, 正文: str = "",
             内容类型: str = "text/plain; charset=utf-8", 头=None) -> str:
    """把「状态 + 正文 + 头」拼成一段**完整的 HTTP 响应**文本。纯函数。

    `Content-Length` 按**字节**算（不是字符数）—— 中文正文才不会截断。
    `头` 里**不许**再写 `Content-Type` / `Content-Length` / `Connection`
    （框架自己填），写了会明确报错。
    """
    码 = _要整数(状态, "状态码")
    if not 100 <= 码 <= 599:
        raise RunValueError(f"状态码要在 100–599 之间，现在是 {码}")
    体 = _要文本(正文, "「构造响应」的正文")
    类型 = _要文本(内容类型, "「构造响应」的内容类型")
    额外 = _要字典(头, "「构造响应」的头")
    自定义 = []
    for 名字, 值 in 额外.items():
        键 = str(名字)
        if 键.lower() in _自填:
            raise RunValueError(
                f"「{键}」由框架自己填，不要在「头」里再写一遍")
        if not 键.isascii() or not str(值).isascii():
            raise RunTypeError(f"请求头「{键}」的名字与值只能是 ASCII（英文数字）")
        自定义.append(f"{键}: {值}")
    数据 = 体.encode("utf-8")
    短语 = _原因.get(码, "")
    行们 = [f"HTTP/1.1 {码}" + (f" {短语}" if 短语 else "")]
    行们.append(f"Content-Type: {类型}")
    行们.extend(自定义)
    行们.append(f"Content-Length: {len(数据)}")
    行们.append("Connection: close")
    return "\r\n".join(行们) + "\r\n\r\n" + 体


def _要可调用(值):
    if 值 is None or not callable(值):
        raise RunTypeError(
            "「启动」要一个**处理函数**：它收一个「请求」字典，返回响应"
            "（文本，或 {状态, 正文, 内容类型, 头} 字典）")
    return 值


def _把应答变成文本(应答) -> str:
    """处理函数的返回值 → 完整响应文本。两种写法都收：

    - **文本** —— 你自己拼好的完整响应（用 `构造响应`）；
    - **字典** —— `{状态, 正文, 内容类型, 头}`，框架替你拼（日常用这种）。
    """
    if isinstance(应答, str):
        return 应答
    if isinstance(应答, dict):
        return 构造响应(
            状态=应答.get("状态", 200),
            正文=应答.get("正文", ""),
            内容类型=应答.get("内容类型", _默认内容类型),
            头=应答.get("头"),
        )
    if 应答 is None:
        return 构造响应()
    raise RunTypeError(
        f"处理函数要返回文本或字典，得到了「{type_name(应答)}」")


def 空闲端口() -> int:
    """问系统要一个**当前空闲的端口**，返回它的号。

    ⚠️ **有竞态**：问完到你真去 `启动`，中间可能被别的程序抢走。
    本地小服务够用；要绝对可靠就直接 `启动(0, …)`（由系统分配，返回值里有实际端口）。
    """
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _读原始请求(处理) -> str:
    """把 http.server **已经拆开**的请求重拼回**原始请求文本**。

    ⚠️ 两个坑（都踩过）：
    ① `BaseHTTPRequestHandler.handle_one_request()` 在你被调到之前，
       **已经把请求行读进 `self.raw_requestline`、把头读进 `self.headers`** ——
       所以这条路是**重拼**，不是再从 `self.rfile` 读一遍（那样读到的是正文，
       解析会报「第一行是空的」）。`headers` 保留了原始大小写，
       小写归一仍然由 `解析请求` 做（只有一份实现）。
    ② `self.rfile` 现在停在**正文**处 ⇒ 只按 `Content-Length` 读那么多。
    """
    首行 = 处理.raw_requestline.decode("latin-1").rstrip("\r\n")
    头行们 = [f"{名}: {值}" for 名, 值 in 处理.headers.items()]
    try:
        长度 = int(处理.headers.get("Content-Length") or "0")
    except ValueError:
        长度 = 0
    体 = 处理.rfile.read(长度).decode("utf-8", "replace") if 长度 > 0 else ""
    return "\r\n".join([首行] + 头行们) + "\r\n\r\n" + 体


def 启动(端口: int = 8000, 处理函数=None, 最多请求数: int = 0) -> dict:
    """起一个 HTTP 服务，**阻塞**着收请求，每个请求调一次 `处理函数`。

    返回 `{"端口": 实际端口, "请求数": 收到几个}`。

    - `端口 = 0` ⇒ 由系统分配一个空闲端口（**实际端口在返回值里**）；
    - `最多请求数 = 0`（默认）⇒ **一直跑**，直到 `Ctrl-C`；
      给个正数 ⇒ **处理够那么多请求**就干净退出（判据与自测靠它）。
      ⚠️ 数的是**真正被 `处理函数` 处理过的**请求 —— 连上就断的空连接
      （端口探测、健康检查）**不算**。
    """
    import http.server

    处理函数 = _要可调用(处理函数)
    端口号 = _要整数(端口, "端口")
    if not 0 <= 端口号 <= 65535:
        raise RunValueError(f"端口要在 0–65535 之间，现在是 {端口号}")
    上限 = _要整数(最多请求数, "最多请求数")
    if 上限 < 0:
        raise RunValueError(f"最多请求数不能是负数，现在是 {上限}")

    记录 = {"请求数": 0}

    class 处理器(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"      # 一问一答就关（约定 ②）

        def log_message(self, *args):      # 别往 stderr 打 —— stderr 是对拍的一部分
            pass

        def _写(self, 应答: str):
            self.wfile.write(应答.encode("utf-8"))
            记录["请求数"] += 1

        def _服务(self):
            from ..errors import JishiError
            try:
                请求 = 解析请求(_读原始请求(self))
            except JishiError as 错:
                # 请求本身不合法 ⇒ 400（与 Rust 侧同：解析失败给 400）
                self._写(构造响应(400, str(错.message)))
                return
            try:
                应答 = _把应答变成文本(处理函数(请求))
            except JishiError as 错:
                # ⚠️ 处理函数出错 ⇒ 回 500（正文就是那句错），**服务继续** ——
                # 一条坏请求不该让整个本地服务挂掉，浏览器里还直接看得见错在哪。
                # 正文只用 `.message`：那句本来就是三宿主逐字对齐的，
                # 用整段渲染（带位置与 `^`）反而各说各的。
                应答 = 构造响应(500, str(错.message))
            except Exception as 错:                      # noqa: BLE001
                应答 = 构造响应(500, str(错))
            self._写(应答)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _服务

    # 🔴 **不用 `http.server.HTTPServer` 的 `server_bind`**（M116 发版在 macOS 上炸出来的）：
    #    它在 bind 之后会调 `socket.getfqdn(host)` 做**反向 DNS**，而我们**根本不用**
    #    `server_name`。macOS 的 CI runner 上这一步能卡 30 秒往上（mDNSResponder 超时），
    #    于是「服务起不来」—— 而 ubuntu / windows 上它快得看不出来，**只在 mac 上红**。
    #    `--sandbox` 之外没有人依赖 `server_name`，跳过它不改变任何可观测行为。
    class _本地服务(http.server.HTTPServer):
        def server_bind(self):
            import socketserver
            socketserver.TCPServer.server_bind(self)     # 只 bind，不做 getfqdn
            self.server_port = self.server_address[1]

    服务 = _本地服务(("127.0.0.1", 端口号), 处理器)
    实际 = int(服务.server_address[1])
    try:
        # ⚠️ **按「处理过的请求数」判停，不是按「接受过的连接数」**：
        #    `handle_request()` 每接受**一个连接**就返回一次 —— 连上就断
        #    （端口探测、健康检查）也算一次，那样配额会被白白吃掉
        #    （M115 自己踩到：测试的端口探测把「最多请求数=1」用掉了，
        #      真实请求反而被拒）。计数只在 `_写` 里加，这里按它判停。
        if 上限 == 0:
            while True:
                服务.handle_request()
        else:
            while 记录["请求数"] < 上限:
                服务.handle_request()
    finally:
        服务.server_close()
    return {"端口": 实际, "请求数": 记录["请求数"]}
