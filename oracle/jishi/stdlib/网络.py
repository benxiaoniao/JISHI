# -*- coding: utf-8 -*-
"""基石标准库：网络。薄封装 Python 的 urllib，中文 API + 安全超时。

零第三方依赖（不依赖 requests），默认超时 10 秒。

## M107 补了什么（2026-10-11）

原来只有 `获取` / `提交` / `获取JSON` 三个函数，且都**不能带请求头** ——
而真实接口几乎都要鉴权头 / 自定义 UA（探针「接口汇总」里那个接口就一律
401）。这一轮按探针撞出来的缺口补：

| 新东西 | 解决什么 |
|---|---|
| `获取/提交/获取JSON` 加 `请求头` 参数 | 鉴权头、自定义 UA、`Accept` |
| `提交JSON(网址, 对象, …)` | 只会发表单（`application/x-www-form-urlencoded`）发不了 JSON |
| `获取详情(网址, …)` | 拿不到**状态码 / 响应头**；非 2xx 一律抛错，分不清 404 / 401 / 500 |
| `下载(网址, 路径, …)` | 二进制响应（图片 / zip）存文件，别先解码成文本 |
| **报错带状态码且各引擎一致** | 以前 Python 给 `Unauthorized`、Rust 给「服务器返回了 401」——同一件事两套说法 |

⚠️ **`请求头` 的值只能是 ASCII**（HTTP 头的规范限制）：中文值会被 urllib
拒绝。要传中文就自己先 `编码.url编码` 或用 body。

📌 **`下载` 之前先用 `获取详情` 看状态**更省事：`下载` 走的是「非 2xx 就抛错」
那套（与 `获取` 一致）。

## M206 补了什么（2026-11-17）：联网安全护栏

与 Rust 侧**逐条同一口径**（`rust/src/http.rs`）：响应大小上限、流式下载、
自己跟重定向（上限 5）、**禁止 https→http 降级**、gzip/deflate 自动解压、
代理、**请求头注入拦截**、报错分类。

`选项` 是最后一个参数（`获取`/`获取JSON`/`获取详情`/`下载` 是第 4 个，
`提交`/`提交JSON` 是第 5 个），一个字典装全部：

```
网络.获取(网址, 10, 空, {"大小上限": 1048576, "重定向上限": 3,
                        "允许降级": 假, "代理": "http://127.0.0.1:7890"})
```
"""

from __future__ import annotations

import gzip as _py_gzip
import json as _py_json
import os as _py_os
import socket as _py_socket
import ssl as _py_ssl
import urllib.error as _py_urlerr
import urllib.parse as _py_urlparse
import urllib.request as _py_urllib
import zlib as _py_zlib

from ..errors import RunError, RunTypeError, RunValueError

__all__ = ["获取", "提交", "提交JSON", "获取JSON", "获取详情", "下载"]

_DEFAULT_TIMEOUT = 10.0

#: 内存里收响应的默认上限（与 Rust 侧同一个数：32 MB）
_DEFAULT_LIMIT = 32 * 1024 * 1024
#: 默认跟几次重定向（Rust 侧也是 5）
_DEFAULT_REDIRECTS = 5

#: 请求头里**不许用户写**的名字：这两个由框架自己填，用户再写一遍会打架
#: （`Content-Length` 写错了整个请求就废了）。
#: ⚠️ `Content-Type` **不**在这里 —— 我们自己要设它（表单 / JSON），
#: 用户也可以覆盖（比如发 `text/plain`）。
_保留头 = {"content-length", "host"}


def _头(请求头) -> dict:
    """把基石传进来的「请求头」字典整理成 urllib 要的 `dict[str, str]`。

    - `空` / 没给 → 空表
    - 键值都转成文本（数字头值很常见：`{"X-页码": 3}`）
    - 值里有非 ASCII → 报**能照做**的错（HTTP 头装不下中文，得自己编码）
    - 值里有 CR/LF → 直接拒（**头注入**：那能把请求劈成两半，塞进额外的头）
    """
    if 请求头 is None:
        return {}
    if not isinstance(请求头, dict):
        raise RunTypeError("「请求头」要传一个字典，例如 {\"X-Token\": \"abc\"}")
    出: dict = {}
    for k, v in 请求头.items():
        名字 = k if isinstance(k, str) else str(k)
        值 = v if isinstance(v, str) else str(v)
        try:
            值.encode("latin-1")
        except UnicodeEncodeError:
            raise RunTypeError(
                f"请求头「{名字}」的值里有中文，发不出去",
                hint="HTTP 头只能装 ASCII；要传中文请放进请求体，"
                     "或先用 编码.url编码 编一下")
        try:
            名字.encode("latin-1")
        except UnicodeEncodeError:
            raise RunTypeError(f"请求头**名字**「{名字}」必须是 ASCII（英文数字）")
        if "\r" in 值 or "\n" in 值:
            raise RunValueError(
                f"请求头「{名字}」的值里有换行 —— 那能把请求劈成两半（头注入）")
        if "\r" in 名字 or "\n" in 名字 or ":" in 名字:
            raise RunValueError(f"请求头名字「{名字}」里有冒号或换行，不合法")
        if 名字.lower() in _保留头:
            continue                      # 由框架自己填，别让用户撞车
        出[名字] = 值
    return 出


def _选项(选项) -> dict:
    """把 `选项` 字典读成内部结构（没给 = 全默认，与 Rust 侧同一套键名）。"""
    出 = {
        "大小上限": _DEFAULT_LIMIT,
        "上限已给": False,
        "重定向上限": _DEFAULT_REDIRECTS,
        "允许降级": False,
        "代理": None,
        "解压": True,
    }
    if 选项 is None:
        return 出
    if not isinstance(选项, dict):
        raise RunTypeError(
            "「选项」要传一个字典，例如 {\"大小上限\": 1048576, \"重定向上限\": 3}")
    for k, v in 选项.items():
        名 = k if isinstance(k, str) else str(k)
        if 名 in ("大小上限", "上限"):
            n = int(v)
            if n < 0:
                raise RunValueError("「大小上限」不能是负数（0 = 不限）")
            出["大小上限"] = n
            出["上限已给"] = True
        elif 名 == "重定向上限":
            n = int(v)
            if n < 0:
                raise RunValueError("「重定向上限」不能是负数")
            出["重定向上限"] = n
        elif 名 == "允许降级":
            出["允许降级"] = bool(v)
        elif 名 == "代理":
            s = (v if isinstance(v, str) else str(v)).strip()
            低 = s.lower()
            # `无` / `none` / `直连` / `off` = 连环境变量都不看（强制直连）
            if 低 in ("无", "none", "直连", "off", "不"):
                出["代理"] = "直连"
            else:
                出["代理"] = s or None
        elif 名 == "解压":
            出["解压"] = bool(v)
        else:
            raise RunValueError(
                f"「选项」里没有「{名}」这一项"
                "（认识：大小上限 / 重定向上限 / 允许降级 / 代理 / 解压）")
    return 出


#: 网址里**不许**被编码的字符（结构字符 + `%` 本身）。
#: ⚠️ `%` 必须在里面 —— 否则「已经编码过的网址」会被**二次编码**
#:    （`%E4%B8%AD` → `%25E4%25B8%25AD`），那是台账 R101 的一半。
_网址保留 = ":/?#[]@!$&'()*+,;=%"


def _规范网址(网址) -> str:
    """把网址里**非 ASCII 的字符**自动做百分号编码（台账 R101）。

    以前网址里带中文必须自己 `编码.url编码(关键字)` 再拼 —— 直接拼上去会报
    `'ascii' codec can't encode characters…`，而那句报错跟「网址」两个字毫无关系。

    - 已编码的部分**原样保留**（`%` 在保留集里，不二次编码）；
    - 结构字符 `:/?#[]@!$&'()*+,;=` 原样保留，只转真正的字符（含空格 → `%20`）；
    - ⚠️ **中文域名**不在这里处理（要先做 punycode，是另一件事）。

    与 Rust / Node 逐字节一致（Node 那边是 `encodeURI` 再把 `%25XX` 还原成 `%XX`）。
    """
    return _py_urlparse.quote(str(网址), safe=_网址保留)


class _不跟随(_py_urllib.HTTPRedirectHandler):
    """**不**自动跟重定向 —— 上层要自己数次数、拦跨协议降级。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _开(opener_代理):
    """造 opener。

    🔴 **必须把自定义 handler 传给 `build_opener`**，不能 `build_opener()` 之后再
    `add_handler()` —— 那样默认的 `ProxyHandler` / `HTTPSHandler` 已经排在前头，
    先命中的是它们（症状：设了代理却走了环境代理、给了 CA 仍然「自签不吃」）。
    2026-11-17 两条一起踩。
    """
    额外 = [_不跟随]
    ca = _py_os.environ.get("JISHI_TLS_CA", "").strip()
    if ca:
        额外.append(_py_urllib.HTTPSHandler(
            context=_py_ssl.create_default_context(cafile=ca)))
    if opener_代理 == "直连":
        # 空 ProxyHandler 就是不读环境变量（`选项["代理"] = "无"`）
        额外.append(_py_urllib.ProxyHandler({}))
    elif opener_代理:
        额外.append(_py_urllib.ProxyHandler(
            {"http": opener_代理, "https": opener_代理}))
    return _py_urllib.build_opener(*额外)


def _解压(数据: bytes, 编码: str, 上限: int) -> bytes:
    """gzip / deflate 自动解压。上限与「响应太大」是同一道闸（防解压炸弹）。"""
    编码 = (编码 or "").lower()
    限 = 上限 if 上限 else None
    if "gzip" in 编码:
        出 = _py_gzip.decompress(数据)
    elif "deflate" in 编码:
        try:
            出 = _py_zlib.decompress(数据)
        except _py_zlib.error:
            出 = _py_zlib.decompress(数据, -_py_zlib.MAX_WBITS)
    else:
        return 数据
    if 限 is not None and len(出) > 限:
        raise RunValueError(f"响应太大（解压后 {len(出)} 字节，上限 {限} 字节）")
    return 出


def _读响应(流, 网址: str, 动作: str, 解码: bool, 上限: int):
    """读正文（边读边数字节，超上限当场停）。"""
    块们 = []
    已读 = 0
    while True:
        块 = 流.read(65536)
        if not 块:
            break
        已读 += len(块)
        if 上限 and 已读 > 上限:
            raise RunValueError(
                f"响应太大（已经 {已读} 字节，上限 {上限} 字节）",
                hint="真要更大就传 选项 {\"大小上限\": 字节数}（0 = 不限）")
        块们.append(块)
    return b"".join(块们)


def _归类(网址: str, 动作: str, e: BaseException) -> RunError:
    """把「五种完全不同的失败」分开说 —— 不然用户只看到一句「出错了」。"""
    因 = getattr(e, "reason", e)
    文本 = str(因)
    if isinstance(因, _py_ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in 文本:
        return RunError(f"{动作}「{网址}」失败：证书校验没过（{文本}）")
    if isinstance(因, _py_socket.gaierror):
        return RunError(f"{动作}「{网址}」失败：域名解析不了（{文本}）")
    if isinstance(因, ConnectionRefusedError):
        return RunError(f"{动作}「{网址}」失败：连不上服务器（对方拒绝）")
    if isinstance(因, (_py_socket.timeout, TimeoutError)):
        return RunError(f"{动作}「{网址}」失败：请求超时（{文本}）")
    if isinstance(因, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return RunError(f"{动作}「{网址}」失败：连接被服务器断开了（{文本}）")
    return RunError(f"{动作}「{网址}」失败：{文本}")


def _发(网址, 方法: str, 体, 超时, 请求头, 动作: str, 解码: bool = True,
        选项=None, 落盘=None):
    """统一的「发一个请求并读回结果」——四种动作共用一套报错措辞。

    ⚠️ **不因为 4xx/5xx 抛错**（那是 `获取详情` 的语义）；调用方拿状态自己判。
    """
    o = _选项(选项)
    表 = _头(请求头)
    表.setdefault("User-Agent", "oracle/jishi/0.1")
    if not any(k.lower() == "accept-encoding" for k in 表):
        # 落盘时明确要 identity：gzip 的数据没法边解边写（内存会升高）
        表["Accept-Encoding"] = "identity" if 落盘 else "gzip, deflate"

    url = _规范网址(网址)
    开 = _开(o["代理"])
    超时 = max(0.1, float(超时))
    已跳 = 0
    while True:
        req = _py_urllib.Request(url, data=体, headers=表, method=方法)
        try:
            resp = 开.open(req, timeout=超时)
        except _py_urlerr.HTTPError as e:
            resp = e                      # 3xx / 4xx / 5xx 都是一个「响应」
        except _py_urlerr.URLError as e:
            raise _归类(url, 动作, e)
        except UnicodeEncodeError:
            # ⚠️ 中文**域名**是另一件事：urllib 会把 %XX 的 host 还原成中文再按
            #    latin-1 编码 ⇒ 报一句和「网址」毫无关系的 codec 错。这里说清楚。
            raise RunValueError(
                f"「{网址}」的域名不是 ASCII —— 中文域名要先转 punycode"
                "（例如 中文.cn → xn--fiq228c.cn）")

        with resp:
            状态 = int(getattr(resp, "status", 0) or getattr(resp, "code", 0) or 0)
            头表: dict = {}
            for k, v in getattr(resp, "headers", {}).items():
                头表[k] = (头表[k] + ", " + v) if k in 头表 else v

            # ---- 重定向：自己跟（数次数 + 拦降级）----
            if 状态 in (301, 302, 303, 307, 308) and 头表.get("Location"):
                if 已跳 >= o["重定向上限"]:
                    raise RunValueError(
                        f"{url} 跳转超过 {o['重定向上限']} 次（可能是重定向环）")
                下一站 = _py_urlparse.urljoin(url, 头表["Location"])
                if url.startswith("https://") and 下一站.startswith("http://") \
                        and not o["允许降级"]:
                    raise RunValueError(
                        f"「{url}」想从 https 跳到 http（{下一站}）"
                        "—— 明文降级不安全，已拦下")
                if 状态 == 303 or (状态 in (301, 302) and 方法 not in ("GET", "HEAD")):
                    方法 = "GET"
                    体 = None
                已跳 += 1
                url = 下一站
                continue

            正文 = _读响应(resp, url, 动作, 解码, o["大小上限"] if not 落盘 else o["大小上限"])
            编码 = 头表.get("Content-Encoding", "")
            if o["解压"] and 正文 and ("gzip" in 编码.lower() or "deflate" in 编码.lower()):
                try:
                    正文 = _解压(正文, 编码, o["大小上限"])
                except RunValueError:
                    raise
                except Exception as e:    # noqa: BLE001
                    raise RunValueError(f"「{url}」的响应解压失败：{e}")

            if 落盘 is not None:
                临 = f"{落盘}.part"
                目录 = _py_os.path.dirname(str(落盘))
                if 目录:
                    _py_os.makedirs(目录, exist_ok=True)
                try:
                    with open(临, "wb") as f:
                        f.write(正文)
                    _py_os.replace(临, 落盘)
                except OSError as e:
                    try:
                        _py_os.remove(临)
                    except OSError:
                        pass
                    raise RunError(f"写文件「{落盘}」失败：{e}")
                return {"状态": 状态, "响应头": 头表, "正文": b"", "字节数": len(正文)}

            return {
                "状态": 状态,
                "响应头": 头表,
                "正文": 正文.decode("utf-8", errors="replace") if 解码 else 正文,
                "字节数": len(正文),
            }


def _要成功(结果: dict, 网址, 动作: str) -> dict:
    """非 2xx 就按统一口径报错（`获取` / `提交` / `下载` 走这条）。

    ⚠️ `状态 == 0` 按成功算：那是「传输层没告诉我们状态码」的兜底
    （真实 urllib 响应永远有状态码，这里只是别对「读不到状态」瞎报错）。
    """
    状态 = 结果["状态"]
    if 状态 == 0 or 200 <= 状态 < 300:
        return 结果
    raise RunError(
        f"{动作}「{网址}」失败：HTTP {状态}",
        hint="要自己看状态码请改用 网络.获取详情")


def 获取(网址, 超时: float = _DEFAULT_TIMEOUT, 请求头=None, 选项=None) -> str:
    """用 GET 请求网址，返回响应文本。

    `请求头` 是个字典（可选）：`网络.获取(网址, 10, {"X-Token": "abc"})`。
    `选项` 也是字典（可选，M206）：`{"大小上限": 字节, "重定向上限": 次,
    "允许降级": 布尔, "代理": "http://…", "解压": 布尔}`。
    非 2xx 会报错（要看状态码用 `获取详情`）。
    """
    结果 = _发(网址, "GET", None, 超时, 请求头, "访问", 选项=选项)
    return _要成功(结果, 网址, "访问")["正文"]


def 提交(网址, 数据="", 超时: float = _DEFAULT_TIMEOUT, 请求头=None, 选项=None) -> str:
    """用 POST 提交**表单**数据（文本），返回响应文本。

    Content-Type 固定 `application/x-www-form-urlencoded`；要发 JSON 用 `提交JSON`。
    """
    体 = (数据 if isinstance(数据, (bytes, bytearray))
          else str(数据).encode("utf-8"))
    表 = dict(请求头) if isinstance(请求头, dict) else {}
    表.setdefault("Content-Type", "application/x-www-form-urlencoded")
    结果 = _发(网址, "POST", bytes(体), 超时, 表, "提交到", 选项=选项)
    return _要成功(结果, 网址, "提交到")["正文"]


def 提交JSON(网址, 对象, 超时: float = _DEFAULT_TIMEOUT, 请求头=None, 选项=None) -> str:
    """用 POST 提交 **JSON**，返回响应文本。

    例：`网络.提交JSON(网址, {"名": "小明", "数": 3})`
    —— 会自己带上 `Content-Type: application/json; charset=utf-8`。
    """
    体 = _py_json.dumps(对象, ensure_ascii=False).encode("utf-8")
    表 = dict(请求头) if isinstance(请求头, dict) else {}
    表.setdefault("Content-Type", "application/json; charset=utf-8")
    结果 = _发(网址, "POST", 体, 超时, 表, "提交到", 选项=选项)
    return _要成功(结果, 网址, "提交到")["正文"]


def 获取JSON(网址, 超时: float = _DEFAULT_TIMEOUT, 请求头=None, 选项=None) -> object:
    """用 GET 请求一个返回 JSON 的接口，解析成列表/字典。"""
    文本 = 获取(网址, 超时=超时, 请求头=请求头, 选项=选项)
    try:
        return _py_json.loads(文本)
    except _py_json.JSONDecodeError as e:
        raise RunValueError(
            f"「{网址}」返回的不是有效 JSON：第 {e.lineno} 行第 {e.colno} 列（{e.msg}）")


def 获取详情(网址, 超时: float = _DEFAULT_TIMEOUT, 请求头=None, 选项=None) -> dict:
    """GET 一个网址，把**状态码 / 响应头 / 正文**一起给你，**非 2xx 不报错**。

    返回 `{"状态": 200, "响应头": {...}, "正文": "..."}`。适合「先看状态再决定
    怎么办」的场景（404 是正常的、401 要换令牌……）。

    例：`如果 网络.获取详情(网址)["状态"] == 404：…`
    """
    return _发(网址, "GET", None, 超时, 请求头, "访问", 选项=选项)


def 下载(网址, 路径, 超时: float = _DEFAULT_TIMEOUT, 请求头=None, 选项=None) -> int:
    """GET 一个网址，把响应**原始字节**写进文件，返回字节数。

    二进制内容（图片 / zip / 字体）走这条 —— `获取` 会先按 UTF-8 解码，二进制
    经不起那一步。非 2xx 会报错（要自己看状态码用 `获取详情`）。

    M206 起**边读边写**（不先攒在内存里），先写 `<路径>.part`、成功才改名 ——
    1 GB 的文件也不会占 1 GB 内存。默认不限大小（要限就传 `选项["大小上限"]`）。
    """
    o = _选项(选项)
    if not o["上限已给"]:
        o["上限已给"] = True
        o["大小上限"] = 0                  # 0 = 不限（流式落盘，不占内存）
    结果 = _发(网址, "GET", None, 超时, 请求头, "下载", 解码=False,
               选项={"大小上限": o["大小上限"], "重定向上限": o["重定向上限"],
                     "允许降级": o["允许降级"], "代理": o["代理"], "解压": o["解压"]},
               落盘=str(路径))
    结果 = _要成功(结果, 网址, "下载")
    return int(结果["字节数"])
