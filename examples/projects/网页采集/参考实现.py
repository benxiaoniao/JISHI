# -*- coding: utf-8 -*-
"""网页采集（Python 参考实现，只用标准库）。

与「采集.jsh」同功能——用来算「基石版行数 ÷ Python 版行数」。
抓网页 → 正则提取链接 → 存 CSV → 统计域名分布。
"""

import csv
import re
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen

PATTERN = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def extract(html):
    links = []
    for url, title in PATTERN.findall(html):
        if url.startswith("http"):
            links.append([re.sub("<[^>]+>", "", title).strip(), url])
    return links


def main():
    if len(sys.argv) > 1:
        url = sys.argv[1].strip()
    else:
        print("请输入要采集的网址：")
        url = input().strip()
    if not url:
        print("网址不能为空")
        return

    print(f"正在抓取 {url} ...")
    try:
        req = Request(url, headers={"User-Agent": "jishi-demo"})
        html = urlopen(req, timeout=10).read().decode("utf-8", "replace")
    except Exception as exc:                      # noqa: BLE001 - 演示：网络什么都能错
        print(f"抓取失败：{exc}")
        return

    links = extract(html)
    print(f"共提取到 {len(links)} 个链接")
    print()

    with open("链接.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["标题", "网址"])
        writer.writerows(links)
    print("已保存到 链接.csv")
    print()

    domains = {}
    for _, link in links:
        domain = urlparse(link).netloc
        domains[domain] = domains.get(domain, 0) + 1
    print("域名分布（去重后）：")
    for domain, count in domains.items():
        print(f"  {domain}：{count} 个")


main()
