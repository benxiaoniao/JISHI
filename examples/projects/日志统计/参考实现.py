# -*- coding: utf-8 -*-
"""日志统计（Python 参考实现，只用标准库）。

与「统计.jsh」同功能、同输出口径——用来算「基石版行数 ÷ Python 版行数」。
日志格式：`IP - 方法 路径 状态码`。
"""

import sys
from collections import Counter


def parse(path):
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 4:
                records.append((parts[0], parts[2], parts[3]))
    return records


def show(title, counter, order=None):
    print(title)
    items = counter.most_common() if order == "count" else counter.items()
    for key, count in items:
        print(f"  {key}：{count} 次")


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        print("请输入日志文件路径（直接回车用 访问.log）：")
        path = input().strip() or "访问.log"
    try:
        records = parse(path)
    except OSError:
        print(f"读日志失败：找不到文件或目录：{path}")
        return

    ips = Counter(r[0] for r in records)
    codes = Counter(r[2] for r in records)
    paths = Counter(r[1] for r in records)

    print("== 日志统计 ==")
    print(f"总请求数：{len(records)}")
    print(f"独立 IP 数：{len(ips)}")
    print()
    show("状态码分布：", codes)
    print()
    show("访问最多的路径：", paths, order="count")
    print()
    print("404 请求的 IP：")
    for ip, _ in Counter(r[0] for r in records if r[2] == "404").items():
        print(f"  {ip}")


main()
