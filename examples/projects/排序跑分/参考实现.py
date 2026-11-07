# -*- coding: utf-8 -*-
"""排序跑分（Python 参考实现，只用标准库）。

与「排序.jsh」同功能——用来算「基石版行数 ÷ Python 版行数」。
三种排序的正确性用内置 sorted 当裁判，并打印规模-耗时表。
"""

import random
import sys
import time


def bubble(data):
    out = list(data)
    n = len(out)
    for i in range(n):
        swapped = False
        for j in range(n - i - 1):
            if out[j] > out[j + 1]:
                out[j], out[j + 1] = out[j + 1], out[j]
                swapped = True
        if not swapped:
            break
    return out


def insertion(data):
    out = list(data)
    for i in range(1, len(out)):
        cur, j = out[i], i - 1
        while j >= 0 and out[j] > cur:
            out[j + 1] = out[j]
            j -= 1
        out[j + 1] = cur
    return out


def quick(data):
    if len(data) <= 1:
        return list(data)
    pivot = data[0]
    small = [x for x in data[1:] if x < pivot]
    big = [x for x in data[1:] if x >= pivot]
    return quick(small) + [pivot] + quick(big)


def timeit(func, data):
    start = time.perf_counter()
    result = func(data)
    return (time.perf_counter() - start) * 1000, result


def main():
    sizes = [int(sys.argv[1])] if len(sys.argv) > 1 else [100, 200, 400]
    algos = [("冒泡", bubble), ("插入", insertion), ("快速", quick)]

    print("== 排序跑分 ==")
    print("每种排序各跑一遍，随机整数数据；正确性用内建「排序()」当裁判。")
    print()
    header = "规模".ljust(8)
    for name, _ in algos:
        header += f"{name}(毫秒)".ljust(14)
    print(header)
    print("-" * 40)

    ok = True
    for size in sizes:
        data = [random.randint(0, size * 10) for _ in range(size)]
        expected = sorted(data)
        row = str(size).ljust(8)
        for name, func in algos:
            ms, result = timeit(func, data)
            if result != expected:
                ok = False
                row += "结果不对!".ljust(14)
            else:
                row += f"{round(ms, 2)}".ljust(14)
        print(row)

    print()
    print("三个排序的结果都与内建排序一致 ✅" if ok else
          "有排序算错了 ❌ —— 先用小规模输入打印中间过程看是哪一步")


main()
