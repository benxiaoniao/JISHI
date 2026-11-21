# -*- coding: utf-8 -*-
"""成绩分析（Python 参考实现，只用标准库）。

与「分析.jsh」同功能、同输出口径——用来算「基石版行数 ÷ Python 版行数」。
写法按 Python 的常规习惯（英文标识符 + 内置函数 + csv 模块），不刻意写长。
"""

import csv
import sys

SUBJECTS = ["语文", "数学", "英语"]


def to_number(text):
    try:
        return float(text)
    except ValueError:
        return 0


def total_of(row):
    return sum(int(to_number(row[i])) for i in range(1, len(row)))


def analyze(path):
    with open(path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header, data = rows[0], [r for r in rows[1:] if r]

    print("== 成绩分析 ==")
    print(f"共 {len(data)} 名学生")
    print()

    ranked = sorted(([total_of(r), r[0]] for r in data), reverse=True)
    print("总分排名：")
    for i, (score, name) in enumerate(ranked, 1):
        print(f"  {i}. {name} {score} 分")
    print()

    print("各科情况：")
    for i, subject in enumerate(SUBJECTS, 1):
        scores = [to_number(r[i]) for r in data]
        print(f"  {subject}：平均 {sum(scores) / len(scores):.1f}，最高 {max(scores)}")
    print()

    print("不及格名单：")
    failed = False
    for row in data:
        for i in range(1, len(row)):
            if to_number(row[i]) < 60:
                print(f"  {row[0]}（{SUBJECTS[i - 1]} {row[i]}）")
                failed = True
    if not failed:
        print("  全部及格，很棒！")


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        print("请输入 CSV 文件路径（直接回车用 成绩.csv）：")
        path = input().strip() or "成绩.csv"
    try:
        analyze(path)
    except FileNotFoundError:
        print(f"读文件失败：找不到文件 {path}")
        print("请确认文件存在，且第一行是「姓名,语文,数学,英语」表头")


main()
