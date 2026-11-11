# -*- coding: utf-8 -*-
"""起一个**本地样例站点**给「接口汇总」探针用（不联网、结果可复现）。

为什么要有它：这个探针要撞的是「抓取 + 结构化」这类缺口 —— 请求头校验、
分页、POST JSON、状态码。这些只有在**真的走 HTTP** 时才现形；拿一个本地
文件假装接口，就把要考的东西绕掉了。

## 怎么用

    python "examples/绕道探针/接口汇总/起样例站点.py" 8765
    # 另一个终端（在探针目录里跑）：
    jishi 汇总.jsh http://127.0.0.1:8765

## 它故意设的门槛（对应探针里的绕道条目）

| 路径 | 故意设的坎 | 期望的解决手段 |
|---|---|---|
| `GET /api/list?page=N&q=…` | 不带 `X-Token` 一律 **401** | 请求要能带**自定义请求头** |
| 同上 | `q` 是**中文**关键字 | 网址里的中文要能自动编码 |
| 同上 | 响应里 `next` 是**完整 URL**（含端口） | —— |
| `POST /api/confirm` | 只认 `application/json`，否则 **415** | 请求要能**提交 JSON** |
| `GET /api/status/404` | 故意返回 **404** | 要能**拿到状态码**（而不是一律抛错） |
| `GET /products.html` | 一张 `<table>`，字段散在 HTML 里 | 要能从 HTML 里**取表格/取文本** |

只依赖 Python 标准库（`http.server`），与探针的「参考实现.py」同一份依赖口径。
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

#: 接口要的令牌（映照着「真实 API 都要鉴权」这件事）。
#: ⚠️ 用 ASCII：HTTP 头的**值**也只能装 latin-1，中文值同样发不出去。
令牌 = "sample-token-1234"

#: 两页「订单」数据 —— 探针要把两页并起来、按客户汇总。
第一页 = [
    {"编号": "A-1001", "客户": "甲公司", "金额": 1200.5, "状态": "已付款"},
    {"编号": "A-1002", "客户": "乙公司", "金额": 340.0, "状态": "已付款"},
    {"编号": "A-1003", "客户": "甲公司", "金额": 780.25, "状态": "待付款"},
]
第二页 = [
    {"编号": "A-1004", "客户": "丙公司", "金额": 2000.0, "状态": "已付款"},
    {"编号": "A-1005", "客户": "乙公司", "金额": 99.9, "状态": "已取消"},
]

HTML = """<!doctype html>
<html lang="zh">
<head><meta charset="utf-8"><title>商品列表</title></head>
<body>
<h1>商品列表</h1>
<table>
  <thead><tr><th>商品</th><th>单价</th><th>库存</th></tr></thead>
  <tbody>
    <tr><td>笔记本</td><td>12.50</td><td>40</td></tr>
    <tr><td>钢笔</td><td>38.00</td><td>7</td></tr>
    <tr><td>橡皮</td><td>2.20</td><td>150</td></tr>
  </tbody>
</table>
<p class="备注">价格含税。</p>
</body>
</html>
"""


class 处理器(BaseHTTPRequestHandler):
    # ⚠️ HTTP 的响应头只能装 latin-1，`server_version` 会原样进 `Server` 头
    #     —— 写成中文会当场炸（2026-10-11 自己踩了一次）。
    server_version = "sample-site/1.0"

    def log_message(self, fmt, *a):        # 安静点，别把探针输出刷花
        pass

    # -- 小工具 -------------------------------------------------------------
    def _回(self, 状态: int, 内容: bytes, 类型: str):
        self.send_response(状态)
        self.send_header("Content-Type", 类型)
        self.send_header("Content-Length", str(len(内容)))
        self.end_headers()
        self.wfile.write(内容)

    def _回JSON(self, 状态: int, 对象):
        self._回(状态, json.dumps(对象, ensure_ascii=False).encode("utf-8"),
                 "application/json; charset=utf-8")

    def _基址(self) -> str:
        host = self.headers.get("Host") or "127.0.0.1"
        return f"http://{host}"

    def _令牌对吗(self) -> bool:
        # ⚠️ 请求头**名字**也必须是 ASCII（中文头名发不出去）——
        #    所以头名用 `X-Token`，只有**值**里有中文。
        return self.headers.get("X-Token") == 令牌

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/list":
            查询 = parse_qs(u.query)
            页 = int((查询.get("page") or ["1"])[0])
            关键字 = (查询.get("q") or [""])[0]
            if not self._令牌对吗():
                self._回JSON(401, {"错误": "缺少或无效的 X-Token 请求头"})
                return
            数据 = {1: 第一页, 2: 第二页}.get(页)
            if 数据 is None:
                self._回JSON(404, {"错误": f"没有第 {页} 页"})
                return
            if 关键字:
                数据 = [r for r in 数据 if 关键字 in r["客户"]]
            下一页 = None if 页 >= 2 else \
                f"{self._基址()}/api/list?page={页 + 1}&q={关键字}"
            self._回JSON(200, {"本页": 数据, "下一页": 下一页, "总页数": 2,
                               "关键字": 关键字})
            return
        if u.path == "/api/status/404":
            self._回JSON(404, {"错误": "这个地址是故意做成 404 的"})
            return
        if u.path in ("/products.html", "/"):
            self._回(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._回JSON(404, {"错误": "没这个路径"})

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        长度 = int(self.headers.get("Content-Length") or 0)
        体 = self.rfile.read(长度)
        if u.path == "/api/confirm":
            if self.headers.get("Content-Type", "").split(";")[0].strip() \
                    != "application/json":
                self._回JSON(415, {"错误": "只认 application/json"})
                return
            try:
                对象 = json.loads(体.decode("utf-8"))
            except Exception as e:                     # noqa: BLE001
                self._回JSON(400, {"错误": f"不是合法 JSON：{e}"})
                return
            条数 = len(对象) if isinstance(对象, list) else 1
            self._回JSON(200, {"收到": 对象, "条数": 条数})
            return
        self._回JSON(404, {"错误": "没这个路径"})


def main(argv: list[str]) -> int:
    端口 = int(argv[1]) if len(argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", 端口), 处理器)
    print(f"样例站点已起：http://127.0.0.1:{端口}/  （Ctrl+C 停）")
    print(f"  接口要的令牌：X-Token: {令牌}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
