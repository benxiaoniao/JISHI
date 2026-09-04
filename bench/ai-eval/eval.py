# -*- coding: utf-8 -*-
"""基石评测集（JishiEval）—— 度量 LLM 写基石代码的一次通过率。

用法：
    python bench/ai-eval/eval.py --self-test    用标准答案自测（验证评测集正确）
    python bench/ai-eval/eval.py --list          列出全部任务

评测维度：给定自然语言任务，LLM 生成基石代码，运行后 stdout 与断言一致即通过。
标准答案自测必须 100% 通过——这是「语法改动不得使通过率下降」的回归基线。
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout

sys.path.insert(0, ".")

from jishi.interpreter import run_source

#: 评测任务：(任务描述, 标准答案, 期望 stdout)
TASKS = [
    {
        "id": "001_计算平均值",
        "task": "写一个函数计算列表 [2, 4, 6, 8] 的平均值，并打印结果。",
        "answer": "函数 平均值(数据):\n    返回 总和(数据) / 长度(数据)\n打印(平均值([2, 4, 6, 8]))",
        "stdout": "5.0\n",
    },
    {
        "id": "002_列表求和",
        "task": "计算 [1, 2, 3, 4, 5] 所有元素的总和并打印。",
        "answer": "打印(总和([1, 2, 3, 4, 5]))",
        "stdout": "15\n",
    },
    {
        "id": "003_过滤偶数",
        "task": "从 [1, 2, 3, 4, 5, 6] 里筛选出所有偶数，打印结果列表。",
        "answer": "打印([x 遍历 x 在 [1, 2, 3, 4, 5, 6] 如果 x % 2 == 0])",
        "stdout": "[2, 4, 6]\n",
    },
    {
        "id": "004_平方映射",
        "task": "用推导式把 [1, 2, 3, 4, 5] 每个数平方，打印新列表。",
        "answer": "打印([x * x 遍历 x 在 [1, 2, 3, 4, 5]])",
        "stdout": "[1, 4, 9, 16, 25]\n",
    },
    {
        "id": "005_斐波那契",
        "task": "输出斐波那契数列前 8 项（每行一个，从 0 开始）。",
        "answer": "令 a = 0\n令 b = 1\n循环 8 次:\n    打印(a)\n    a, b = b, a + b",
        "stdout": "0\n1\n1\n2\n3\n5\n8\n13\n",
    },
    {
        "id": "006_素数判断",
        "task": "写一个函数判断 17 是不是素数，打印结果（布尔值）。",
        "answer": "函数 是素数(n):\n    如果 n < 2:\n        返回 假\n    遍历 i 在 范围(2, n):\n        如果 n % i == 0:\n            返回 假\n    返回 真\n打印(是素数(17))",
        "stdout": "True\n",
    },
    {
        "id": "007_字符串插值",
        "task": "用文本插值打印「你好 小明，今年 18 岁」。",
        "answer": "令 名字 = \"小明\"\n令 年龄 = 18\n打印(`你好 {名字}，今年 {年龄} 岁`)",
        "stdout": "你好 小明，今年 18 岁\n",
    },
    {
        "id": "008_默认参数",
        "task": "写带默认参数的函数打招呼(名字, 语气=\"你好\")，分别用默认和自定义语气各调一次。",
        "answer": "函数 打招呼(名字, 语气 = \"你好\"):\n    打印(语气, 名字)\n打招呼(\"小明\")\n打招呼(\"小明\", \"欢迎\")",
        "stdout": "你好 小明\n欢迎 小明\n",
    },
    {
        "id": "009_多赋值交换",
        "task": "用多赋值交换两个变量的值：a=1、b=2 交换后打印 a 和 b。",
        "answer": "令 a = 1\n令 b = 2\na, b = b, a\n打印(a, b)",
        "stdout": "2 1\n",
    },
    {
        "id": "010_异常处理",
        "task": "用尝试/捕获处理除零：计算 1/0 并打印「除数不能是零」。",
        "answer": "尝试:\n    令 结果 = 1 / 0\n捕获 除零错误 为 e:\n    打印(\"除数不能是零\")",
        "stdout": "除数不能是零\n",
    },
    {
        "id": "011_类与对象",
        "task": "定义账户类，支持存钱取钱；建一个余额 100 的账户，存 50 后打印余额。",
        "answer": "类 账户:\n    函数 初始化(自身, 余额):\n        自身.余额 = 余额\n    函数 存(自身, 数额):\n        自身.余额 += 数额\n令 我的 = 新建 账户(100)\n我的.存(50)\n打印(我的.余额)",
        "stdout": "150\n",
    },
    {
        "id": "012_字典统计频次",
        "task": "统计列表 [\"a\", \"b\", \"a\"] 中每个元素出现次数，打印字典。",
        "answer": "令 数据 = [\"a\", \"b\", \"a\"]\n令 频次 = {}\n遍历 x 在 数据:\n    如果 频次.包含(x):\n        频次[x] += 1\n    否则:\n        频次[x] = 1\n打印(频次)",
        "stdout": "{'a': 2, 'b': 1}\n",
    },
]


def run_code(src: str) -> str:
    """运行基石代码，返回 stdout。"""
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src, "<评测>")
    return out.getvalue()


def self_test() -> tuple[int, int]:
    """用标准答案自测，返回 (通过数, 总数)。"""
    passed = 0
    total = len(TASKS)
    for t in TASKS:
        try:
            out = run_code(t["answer"])
            ok = out == t["stdout"]
        except Exception as e:  # noqa: BLE001
            ok = False
            out = f"<异常: {type(e).__name__}: {e}>"
        if ok:
            passed += 1
            print(f"  [通过] {t['id']}")
        else:
            print(f"  [失败] {t['id']}\n        期望 {t['stdout']!r}\n        实际 {out!r}")
    return passed, total


def main() -> int:
    argv = sys.argv[1:]
    if "--list" in argv:
        for t in TASKS:
            print(f"{t['id']}: {t['task']}")
        return 0
    print(f"基石评测集（JishiEval）—— 共 {len(TASKS)} 题，标准答案自测：")
    passed, total = self_test()
    print(f"\n结果：{passed}/{total} 通过"
          f"（{'✓ 全部通过' if passed == total else '✗ 有失败'}）")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
