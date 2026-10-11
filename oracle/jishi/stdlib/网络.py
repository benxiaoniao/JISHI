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
"""

from __future__ import annotations

import json as _py_json
import os as _py_os
import urllib.error as _py_urlerr
import urllib.request as _py_urllib

from ..errors import RunError, RunTypeError, RunValueError

__all__ = ["获取", "提交", "提交JSON", "获取JSON", "获取详情", "下载"]

_DEFAULT_TIMEOUT = 10.0

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
        if 名字.lower() in _保留头:
            continue                      # 由框架自己填，别让用户撞车
        出[名字] = 值
    return 出


def _读响应(resp, 网址: str, 动作: str, 解码: bool = True):
    """把 urllib 的响应（可能是 HTTPError）读成我们要的东西。

    404 / 500 这类**不是异常** —— 它们只是「状态码 + 正文」，交给调用方判。
    连不上 / 超时 / 域名解析不了才是异常。
    """
    try:
        正文 = resp.read()
    except Exception as e:                # noqa: BLE001
        raise RunError(f"{动作}「{网址}」失败：读响应出错（{e}）")
    头表: dict = {}
    头 = getattr(resp, "headers", None)
    if 头 is not None:
        for k, v in 头.items():
            if k in 头表:
                头表[k] = 头表[k] + ", " + v
            else:
                头表[k] = v
    return {
        "状态": int(getattr(resp, "status", 0) or getattr(resp, "code", 0) or 0),
        "响应头": 头表,
        "正文": 正文.decode("utf-8", errors="replace") if 解码 else 正文,
    }


def _发(网址, 方法: str, 体, 超时, 请求头, 动作: str, 解码: bool = True):
    """统一的「发一个请求并读回结果」——四种动作共用一套报错措辞。

    ⚠️ **不因为 4xx/5xx 抛错**（那是 `获取详情` 的语义）；调用方拿状态自己判。
    """
    表 = _头(请求头)
    表["User-Agent"] = 表.get("User-Agent", "oracle/jishi/0.1")
    try:
        req = _py_urllib.Request(str(网址), data=体, headers=表,
                                 method=方法)
    except Exception as e:                # noqa: BLE001
        raise RunError(f"{动作}「{网址}」失败：{e}")
    try:
        with _py_urllib.urlopen(req, timeout=max(0.1, float(超时))) as resp:
            return _读响应(resp, str(网址), 动作, 解码)
    except _py_urlerr.HTTPError as e:
        # 4xx/5xx：urllib 把它当异常抛，但它其实是**一个正常响应**
        return _读响应(e, str(网址), 动作, 解码)
    except _py_urlerr.URLError as e:
        raise RunError(f"{动作}「{网址}」失败：{e.reason}")
    except Exception as e:                # noqa: BLE001
        raise RunError(f"{动作}「{网址}」失败：{e}")


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


def 获取(网址, 超时: float = _DEFAULT_TIMEOUT, 请求头=None) -> str:
    """用 GET 请求网址，返回响应文本。

    `请求头` 是个字典（可选）：`网络.获取(网址, 10, {"X-Token": "abc"})`。
    非 2xx 会报错（要看状态码用 `获取详情`）。
    """
    结果 = _发(网址, "GET", None, 超时, 请求头, "访问")
    return _要成功(结果, 网址, "访问")["正文"]


def 提交(网址, 数据="", 超时: float = _DEFAULT_TIMEOUT, 请求头=None) -> str:
    """用 POST 提交**表单**数据（文本），返回响应文本。

    Content-Type 固定 `application/x-www-form-urlencoded`；要发 JSON 用 `提交JSON`。
    """
    体 = (数据 if isinstance(数据, (bytes, bytearray))
          else str(数据).encode("utf-8"))
    表 = dict(请求头) if isinstance(请求头, dict) else {}
    表.setdefault("Content-Type", "application/x-www-form-urlencoded")
    结果 = _发(网址, "POST", bytes(体), 超时, 表, "提交到")
    return _要成功(结果, 网址, "提交到")["正文"]


def 提交JSON(网址, 对象, 超时: float = _DEFAULT_TIMEOUT, 请求头=None) -> str:
    """用 POST 提交 **JSON**，返回响应文本。

    例：`网络.提交JSON(网址, {"名": "小明", "数": 3})`
    —— 会自己带上 `Content-Type: application/json; charset=utf-8`。
    """
    体 = _py_json.dumps(对象, ensure_ascii=False).encode("utf-8")
    表 = dict(请求头) if isinstance(请求头, dict) else {}
    表.setdefault("Content-Type", "application/json; charset=utf-8")
    结果 = _发(网址, "POST", 体, 超时, 表, "提交到")
    return _要成功(结果, 网址, "提交到")["正文"]


def 获取JSON(网址, 超时: float = _DEFAULT_TIMEOUT, 请求头=None) -> object:
    """用 GET 请求一个返回 JSON 的接口，解析成列表/字典。"""
    文本 = 获取(网址, 超时=超时, 请求头=请求头)
    try:
        return _py_json.loads(文本)
    except _py_json.JSONDecodeError as e:
        raise RunValueError(
            f"「{网址}」返回的不是有效 JSON：第 {e.lineno} 行第 {e.colno} 列（{e.msg}）")


def 获取详情(网址, 超时: float = _DEFAULT_TIMEOUT, 请求头=None) -> dict:
    """GET 一个网址，把**状态码 / 响应头 / 正文**一起给你，**非 2xx 不报错**。

    返回 `{"状态": 200, "响应头": {...}, "正文": "..."}`。适合「先看状态再决定
    怎么办」的场景（404 是正常的、401 要换令牌……）。

    例：`如果 网络.获取详情(网址)["状态"] == 404：…`
    """
    return _发(网址, "GET", None, 超时, 请求头, "访问")


def 下载(网址, 路径, 超时: float = _DEFAULT_TIMEOUT, 请求头=None) -> int:
    """GET 一个网址，把响应**原始字节**写进文件，返回字节数。

    二进制内容（图片 / zip / 字体）走这条 —— `获取` 会先按 UTF-8 解码，二进制
    经不起那一步。非 2xx 会报错（要自己看状态码用 `获取详情`）。
    """
    结果 = _发(网址, "GET", None, 超时, 请求头, "下载", 解码=False)
    正文 = _要成功(结果, 网址, "下载")["正文"]
    目录 = _py_os.path.dirname(str(路径))
    if 目录:
        _py_os.makedirs(目录, exist_ok=True)
    try:
        with open(str(路径), "wb") as f:
            f.write(正文)
    except OSError as e:
        raise RunError(f"写文件「{路径}」失败：{e}")
    return len(正文)
