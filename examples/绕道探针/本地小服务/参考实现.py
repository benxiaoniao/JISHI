# -*- coding: utf-8 -*-
"""本地小服务（Python 参考实现，只用标准库）。

与「服务.jsh」**同一个需求** —— 但它起的是**真的服务**：
浏览器里点一下就能加一条待办。（基石版只能生成一张静态页，见绕道 R006。）

跑：`python 参考实现.py [端口]`，然后浏览器打开 http://127.0.0.1:8000
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT = ["买菜", "写周报", "给猫铲屎"]

PAGE = """<!doctype html>
<html><meta charset="utf-8"><title>待办</title>
<h1>待办清单</h1>
<form method="get" action="/add"><input name="item" autofocus><button>加一条</button></form>
<ul>{items}</ul>
</html>
"""


def load():
    try:
        with open("待办.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return list(DEFAULT)


def save(items):
    with open("待办.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        items = load()
        if url.path == "/add":
            value = parse_qs(url.query).get("item", [""])[0].strip()
            if value:
                items.append(value)
                save(items)
        data = PAGE.format(
            items="".join(f"<li>{i}</li>" for i in items)
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静音，免得刷屏
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"服务起在 http://127.0.0.1:{port}（Ctrl-C 停）")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
