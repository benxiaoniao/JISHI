# -*- coding: utf-8 -*-
"""「小网站」的 Python 参考实现 —— 与 `网站.jsh` **功能等价**。

用途有两个：
1. `tools/loc_ratio.py` 拿它跟基石版比行数（度量「中文写起来到底省不省」）；
2. 想验证基石版行为对不对时，跑这个看「标准答案」。

    python 参考实现.py            # 打开 http://127.0.0.1:8000/

⚠️ 不要把它当「更好的写法」—— 这里刻意**用 Python 标准库最朴素的写法**
（`http.server` + 手写路由），这样对比才公平：比的是**语言**，不是框架。
"""
import http.server
import socketserver
import urllib.parse

页面类型 = "text/html; charset=utf-8"


def 首页(请求, 参数):
    链接们 = []
    for 序 in (1, 2, 3):
        地址 = f"/文章/{序}"
        链接们.append(f'<li><a href="{地址}">第 {序} 篇</a></li>')
    return 构造响应(200, f"<h1>基石小站</h1><p>用中文写的 HTTP 服务端。</p><ul>{''.join(链接们)}</ul>")


def 文章(请求, 参数):
    号 = 参数["id"]
    if 号 in ("1", "2", "3"):
        中文 = {"1": "一", "2": "二", "3": "三"}[号]
        return 构造响应(200, f'<h1>第{中文}篇</h1><p>路由参数 id = {号}。</p><a href="/">回首页</a>')
    return 构造响应(404, f'<h1>没有第 {号} 篇</h1><a href="/">回首页</a>')


def 关于(请求, 参数):
    谁 = 请求["查询"].get("名字", "陌生人")
    return 构造响应(200, f"<h1>关于</h1><p>你好，{谁}。</p>")


# 路由表：方法 → [(路径段, 处理函数)]，注册顺序即优先级（同特异度）
表 = []


def 加路由(方法, 路径, 处理):
    表.append({"方法": 方法.upper(), "段": 切段(路径), "处理": 处理})


def 切段(路径):
    return [] if 路径 == "/" else 路径.lstrip("/").split("/")


def 匹配(方法, 路径):
    """返回 (处理, 参数) 或 None。规则与基石版一致：静态优先、空段不算参数。"""
    段们 = 切段(路径)
    最佳 = None
    for 路由 in 表:
        if 路由["方法"] != "*" and 路由["方法"] != 方法.upper():
            continue
        if len(路由["段"]) != len(段们):
            continue
        参数 = {}
        能匹配 = True
        for 模式, 实际 in zip(路由["段"], 段们):
            if 模式.startswith(":"):
                if 实际 == "":
                    能匹配 = False
                    break
                参数[模式[1:]] = 实际
            elif 模式 != 实际:
                能匹配 = False
                break
        if not 能匹配:
            continue
        参数数 = sum(1 for s in 路由["段"] if s.startswith(":"))
        if 最佳 is None or 参数数 < 最佳[2]:
            最佳 = (路由["处理"], 参数, 参数数)
    return None if 最佳 is None else (最佳[0], 最佳[1])


def 路径们():
    return [f'{r["方法"]} {"/" + "/".join(r["段"]) if r["段"] else "/"}' for r in 表]


def 构造响应(状态, 正文):
    return 状态, 正文.encode("utf-8")


def 分发(请求):
    命中 = 匹配(请求["方法"], 请求["路径"])
    if 命中 is None:
        行们 = "".join(f"<li><code>{路}</code></li>" for 路 in 路径们())
        return 构造响应(404, f'<h1>404</h1><p>没有这个地址。已经有的：</p><ul>{行们}</ul>'
                              '<a href="/">回首页</a>')
    return 命中[0](请求, 命中[1])


class 处理器(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        解析 = urllib.parse.urlparse(self.path)
        请求 = {"方法": "GET", "路径": urllib.parse.unquote(解析.path),
                "查询": dict(urllib.parse.parse_qsl(解析.query))}
        状态, 字节 = 分发(请求)
        self.send_response(状态)
        self.send_header("Content-Type", 页面类型)
        self.send_header("Content-Length", str(len(字节)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(字节)

    def log_message(self, *a):      # 别刷屏
        pass


if __name__ == "__main__":
    加路由("GET", "/", 首页)
    加路由("GET", "/关于", 关于)
    加路由("GET", "/文章/:id", 文章)
    print("路由表：", " · ".join(路径们()))
    print("打开 http://127.0.0.1:8000/   （Ctrl-C 停）")
    with socketserver.TCPServer(("127.0.0.1", 8000), 处理器) as s:
        s.serve_forever()
