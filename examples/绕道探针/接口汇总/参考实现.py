# -*- coding: utf-8 -*-
"""接口汇总 —— 只用 Python 标准库的参考实现（对照 `汇总.jsh`）。

同一件事：抓分页 JSON 接口（带鉴权头 + 中文关键字）→ 按客户汇总 →
导出 CSV / JSON → 从 HTML 里取表 → 提交 JSON 回执 → 按状态码判断。

只依赖 `urllib` / `json` / `csv` / `html.parser`（无第三方库）。
"""

from __future__ import annotations

import csv
import html
import json
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser


def 获取JSON(网址: str, 请求头: dict) -> dict:
    req = urllib.request.Request(网址, headers=请求头)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def 取全部订单(基址: str, 关键字: str) -> list:
    全部 = []
    页号 = 1
    while True:
        网址 = f"{基址}/api/list?page={页号}&q={urllib.parse.quote(关键字)}"
        一页 = 获取JSON(网址, {"X-Token": "sample-token-1234"})
        全部 += 一页["本页"]
        if 一页["下一页"] is None:
            return 全部
        页号 += 1


def 按客户汇总(订单们: list) -> dict:
    桶: dict = {}
    for 单 in 订单们:
        桶.setdefault(单["客户"], []).append(单["金额"])
    return {客: {"总额": sum(v), "笔数": len(v)} for 客, v in 桶.items()}


class 取表(HTMLParser):
    """只取页面里**第一张表**：`[ [格, …], … ]`。"""

    def __init__(self):
        super().__init__()
        self.在表 = 0
        self.行 = None
        self.格 = None
        self.出: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.在表 += 1
        elif self.在表 and tag == "tr":
            self.行 = []
        elif self.在表 and self.行 is not None and tag in ("td", "th"):
            self.格 = []

    def handle_endtag(self, tag):
        if tag == "table":
            self.在表 -= 1
        elif tag == "tr" and self.行 is not None:
            self.出.append(self.行)
            self.行 = None
        elif tag in ("td", "th") and self.格 is not None:
            self.行.append("".join(self.格))
            self.格 = None

    def handle_data(self, data):
        if self.格 is not None:
            self.格.append(data)


def 取商品表(基址: str) -> list:
    req = urllib.request.Request(f"{基址}/products.html")
    with urllib.request.urlopen(req, timeout=10) as resp:
        p = 取表()
        p.feed(resp.read().decode("utf-8"))
        return p.出


def 提交JSON(网址: str, 对象) -> str:
    req = urllib.request.Request(
        网址, data=json.dumps(对象, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def main(argv: list[str]) -> int:
    if not argv:
        print("用法：参考实现.py <接口基址>")
        return 0
    基址 = argv[0]
    令牌头 = {"X-Token": "sample-token-1234"}

    订单们 = 取全部订单(基址, "公司")
    print(f"共 {len(订单们)} 条订单")

    汇总 = 按客户汇总(订单们)
    记录 = [{"客户": k, "总额": v["总额"], "笔数": v["笔数"]}
            for k, v in 汇总.items()]

    with open("_接口汇总_客户.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["客户", "总额", "笔数"])
        w.writeheader()
        w.writerows(记录)
    json.dump(记录, open("_接口汇总_客户.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    表 = 取商品表(基址)
    for 行 in 表:
        print(f"  {行[0]} ｜ {行[1]} ｜ {行[2]}")

    回执 = 提交JSON(f"{基址}/api/confirm", 记录)
    print(f"  回执：{回执}")

    # 按状态码判断：urllib 对 404 抛 HTTPError，我们抓它的 code
    import urllib.error
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{基址}/api/status/404", headers=令牌头),
            timeout=10)
    except urllib.error.HTTPError as e:
        print(f"  状态码是 {e.code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
