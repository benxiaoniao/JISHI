# -*- coding: utf-8 -*-
"""基石评测集（JishiEval）—— 度量 LLM 写基石代码的一次通过率。

用法：
    python bench/ai-eval/eval.py --self-test    用标准答案自测（验证评测集正确）
    python bench/ai-eval/eval.py --list         列出全部任务

评测维度：给定自然语言任务，LLM 生成基石代码，运行后 stdout 与断言一致即通过。
标准答案自测必须 100% 通过——这是「语法改动不得使通过率下降」的回归基线。

覆盖维度（50 题）：
    基础运算 / 字符串 / 列表 / 字典 / 控制流 / 函数与递归 / 闭包 /
    类与继承 / 异常 / 推导式 / 解包 / 默认参数 / 文本插值 /
    标准库（数学 / 文本 / json / 日期 / 正则）
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, ".")

from jishi.errors import JishiError
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
        "stdout": "真\n",
    },
    {
        "id": "007_字符串插值",
        "task": "用文本插值打印「你好 小明，今年 18 岁」。",
        "answer": "令 名字 = \"小明\"\n令 年龄 = 18\n打印(`你好 {名字}，今年 {年龄} 岁`)",
        "stdout": "你好 小明，今年 18 岁\n",
    },
    {
        "id": "008_默认参数",
        "task": "写带默认参数的函数打招呼(名字, 语气=\"你好\")，用「小明」这个名字分别用默认语气和自定义语气「欢迎」各调一次。",
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
        "task": "用尝试/捕获处理除零：计算 1/0，捕获「除零错误」异常，打印「除数不能是零」。",
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
    {
        "id": "013_幂运算",
        "task": "计算并打印 2 的 10 次方。",
        "answer": "打印(2 ** 10)",
        "stdout": "1024\n",
    },
    {
        "id": "014_取余与整除",
        "task": "计算 10 对 3 取余，以及 10 整除 3，分两行打印。",
        "answer": "打印(10 % 3)\n打印(10 // 3)",
        "stdout": "1\n3\n",
    },
    {
        "id": "015_字符串重复",
        "task": "把字符串 \"ab\" 重复 3 次并打印。",
        "answer": "打印(\"ab\" * 3)",
        "stdout": "ababab\n",
    },
    {
        "id": "016_类型判断",
        "task": "打印 5 的类型和 \"你好\" 的类型，各一行。",
        "answer": "打印(类型(5))\n打印(类型(\"你好\"))",
        "stdout": "整数\n文本\n",
    },
    {
        "id": "017_链式比较",
        "task": "设 x=5，判断 1 < x < 10 和 1 < x < 3 的结果，各一行打印。",
        "answer": "令 x = 5\n打印(1 < x < 10)\n打印(1 < x < 3)",
        "stdout": "真\n假\n",
    },
    {
        "id": "018_字符串转大写",
        "task": "把字符串 \"hello\" 转成大写并打印。",
        "answer": "打印(\"hello\".大写())",
        "stdout": "HELLO\n",
    },
    {
        "id": "019_字符串拆分",
        "task": "把字符串 \"a,b,c\" 按逗号拆成列表并打印。",
        "answer": "打印(\"a,b,c\".拆分(\",\"))",
        "stdout": "['a', 'b', 'c']\n",
    },
    {
        "id": "020_字符串替换",
        "task": "把字符串 \"hello world\" 里的 world 替换成 基石，打印结果。",
        "answer": "打印(\"hello world\".替换(\"world\", \"基石\"))",
        "stdout": "hello 基石\n",
    },
    {
        "id": "021_字符串去空白",
        "task": "把字符串 \"  hi  \" 两端的空白去掉并打印。",
        "answer": "打印(\"  hi  \".去空白())",
        "stdout": "hi\n",
    },
    {
        "id": "022_字符串转整数",
        "task": "把字符串 \"42\" 转成整数后加 1，打印结果。",
        "answer": "打印(\"42\".转整数() + 1)",
        "stdout": "43\n",
    },
    {
        "id": "023_字符串判断",
        "task": "判断 \"hello\" 是否以 he 开头、以 lo 结尾，各一行打印。",
        "answer": "打印(\"hello\".开头是(\"he\"))\n打印(\"hello\".结尾是(\"lo\"))",
        "stdout": "真\n真\n",
    },
    {
        "id": "024_列表插入",
        "task": "在列表 [1, 3] 的位置 1 插入数字 2，打印结果列表。",
        "answer": "令 甲 = [1, 3]\n甲.插入(1, 2)\n打印(甲)",
        "stdout": "[1, 2, 3]\n",
    },
    {
        "id": "025_列表排序",
        "task": "把列表 [3, 1, 2] 原地排序后打印。",
        "answer": "令 甲 = [3, 1, 2]\n甲.排序()\n打印(甲)",
        "stdout": "[1, 2, 3]\n",
    },
    {
        "id": "026_列表弹出",
        "task": "从列表 [1, 2, 3] 弹出最后一个元素并打印它，再打印剩余列表。",
        "answer": "令 甲 = [1, 2, 3]\n打印(甲.弹出())\n打印(甲)",
        "stdout": "3\n[1, 2]\n",
    },
    {
        "id": "027_列表索引计数",
        "task": "在列表 [1, 2, 1, 3] 里，打印数字 2 的位置索引，再打印数字 1 出现的次数（只打印数字本身，不要加文字标签）。",
        "answer": "令 甲 = [1, 2, 1, 3]\n打印(甲.索引(2))\n打印(甲.计数(1))",
        "stdout": "1\n2\n",
    },
    {
        "id": "028_列表乘法",
        "task": "用乘法生成包含 4 个 0 的列表并打印。",
        "answer": "打印([0] * 4)",
        "stdout": "[0, 0, 0, 0]\n",
    },
    {
        "id": "029_字典键值",
        "task": "定义一个变量 d 存字典 {\"a\": 1, \"b\": 2}，分两行打印 d 的所有键、所有值（只打印键列表和值列表本身，不加文字标签）。",
        "answer": "令 d = {\"a\": 1, \"b\": 2}\n打印(d.键())\n打印(d.值())",
        "stdout": "['a', 'b']\n[1, 2]\n",
    },
    {
        "id": "030_字典获取默认值",
        "task": "从字典 {\"a\": 1} 获取键 a 的值，以及不存在的键 x 的值（默认 0）。",
        "answer": "令 d = {\"a\": 1}\n打印(d.获取(\"a\"))\n打印(d.获取(\"x\", 0))",
        "stdout": "1\n0\n",
    },
    {
        "id": "031_字典下标赋值",
        "task": "给字典 {\"a\": 1} 增加键 b 值为 2，打印整个字典。",
        "answer": "令 d = {\"a\": 1}\nd[\"b\"] = 2\n打印(d)",
        "stdout": "{'a': 1, 'b': 2}\n",
    },
    {
        "id": "032_否则如果分级",
        "task": "设分数=85，用 如果/否则如果 打印等级：>=90 优、>=80 良、>=60 及格、否则不及格。",
        "answer": "令 分数 = 85\n如果 分数 >= 90:\n    打印(\"优\")\n否则如果 分数 >= 80:\n    打印(\"良\")\n否则如果 分数 >= 60:\n    打印(\"及格\")\n否则:\n    打印(\"不及格\")",
        "stdout": "良\n",
    },
    {
        "id": "033_循环中断",
        "task": "用遍历从 1 循环到 100，累加到一个总和变量里；当循环变量 i 大于 5 时中断，打印累加和（只打印数字）。",
        "answer": "令 总和 = 0\n遍历 i 在 范围(1, 100):\n    如果 i > 5:\n        中断\n    总和 += i\n打印(总和)",
        "stdout": "15\n",
    },
    {
        "id": "034_循环继续",
        "task": "用遍历累加 1 到 5 之间的所有奇数（跳过偶数，用「继续」跳过），打印结果（只打印数字）。",
        "answer": "令 总和 = 0\n遍历 i 在 范围(1, 6):\n    如果 i % 2 == 0:\n        继续\n    总和 += i\n打印(总和)",
        "stdout": "9\n",
    },
    {
        "id": "035_当循环累加",
        "task": "用 当 循环累加 1 到 5 的和，打印结果。",
        "answer": "令 i = 1\n令 总和 = 0\n当 i <= 5:\n    总和 += i\n    i += 1\n打印(总和)",
        "stdout": "15\n",
    },
    {
        "id": "036_内建反转",
        "task": "用内建函数反转列表 [1, 2, 3] 和字符串 \"abc\"，各一行打印。",
        "answer": "打印(反转([1, 2, 3]))\n打印(反转(\"abc\"))",
        "stdout": "[3, 2, 1]\ncba\n",
    },
    {
        "id": "037_递归阶乘",
        "task": "写递归函数计算 6 的阶乘并打印。",
        "answer": "函数 阶(n):\n    如果 n <= 1:\n        返回 1\n    返回 n * 阶(n - 1)\n打印(阶(6))",
        "stdout": "720\n",
    },
    {
        "id": "038_递归累加",
        "task": "写递归函数计算 1 到 10 的和并打印。",
        "answer": "函数 累加(n):\n    如果 n <= 0:\n        返回 0\n    返回 n + 累加(n - 1)\n打印(累加(10))",
        "stdout": "55\n",
    },
    {
        "id": "039_闭包读自由变量",
        "task": "写一个函数，它返回一个闭包，闭包读外层参数并乘以 2；用 21 调用后打印结果。",
        "answer": "函数 造(甲):\n    函数 取():\n        返回 甲 * 2\n    返回 取\n令 f = 造(21)\n打印(f())",
        "stdout": "42\n",
    },
    {
        "id": "040_嵌套函数",
        "task": "写一个具名函数「外」，函数体里把局部变量「值」设为 5，再定义一个具名函数「内」读「值」并加 1；调用外并打印结果（只打印数字）。",
        "answer": "函数 外():\n    令 值 = 5\n    函数 内():\n        返回 值 + 1\n    返回 内()\n打印(外())",
        "stdout": "6\n",
    },
    {
        "id": "041_多值返回解包",
        "task": "写一个名为「两数」的函数，它同时返回 3 和 4 两个数；解包到 a、b 后打印它们的和。",
        "answer": "函数 两数():\n    返回 3, 4\n令 a, b = 两数()\n打印(a + b)",
        "stdout": "7\n",
    },
    {
        "id": "042_继承方法覆盖",
        "task": "定义动物类有叫方法返回动物，狗类继承并覆盖叫返回汪汪，建狗实例打印叫。",
        "answer": "类 动物:\n    函数 叫(自身):\n        返回 \"动物\"\n类 狗 继承 动物:\n    函数 叫(自身):\n        返回 \"汪汪\"\n令 d = 新建 狗()\n打印(d.叫())",
        "stdout": "汪汪\n",
    },
    {
        "id": "043_类多方法",
        "task": "定义矩形类，初始化长宽，有面积和周长方法；建 3x4 矩形，分两行打印面积、周长两个数字（不要加文字标签）。",
        "answer": "类 矩形:\n    函数 初始化(自身, 长, 宽):\n        自身.长 = 长\n        自身.宽 = 宽\n    函数 面积(自身):\n        返回 自身.长 * 自身.宽\n    函数 周长(自身):\n        返回 (自身.长 + 自身.宽) * 2\n令 r = 新建 矩形(3, 4)\n打印(r.面积())\n打印(r.周长())",
        "stdout": "12\n14\n",
    },
    {
        "id": "044_抛出与捕获",
        "task": "用抛出抛一个值错误（消息「出错了」），用捕获接住后，在同一行打印异常类型和消息（用逗号分隔，如「值错误 出错了」）。",
        "answer": "尝试:\n    抛出 值错误(\"出错了\")\n捕获 值错误 为 e:\n    打印(e.类型, e.消息)",
        "stdout": "值错误 出错了\n",
    },
    {
        "id": "045_最终块",
        "task": "写一个带最终块的尝试，先打印「尝试」再打印「最终」。",
        "answer": "尝试:\n    打印(\"尝试\")\n最终:\n    打印(\"最终\")",
        "stdout": "尝试\n最终\n",
    },
    {
        "id": "046_数学最大公约数",
        "task": "导入数学，打印 24 和 36 的最大公约数。",
        "answer": "导入 数学\n打印(数学.最大公约数(24, 36))",
        "stdout": "12\n",
    },
    {
        "id": "047_数学开方与四舍五入",
        "task": "导入数学，打印 144 的开方，再打印 3.14159 保留两位小数的结果（只打印两个数字）。",
        "answer": "导入 数学\n打印(数学.开方(144))\n打印(数学.四舍五入(3.14159, 2))",
        "stdout": "12.0\n3.14\n",
    },
    {
        "id": "048_文本拼接重复",
        "task": "导入文本，把 [\"a\",\"b\",\"c\"] 用 - 连接，再把 \"ab\" 重复 3 次，各一行打印。",
        "answer": "导入 文本\n打印(文本.拼接([\"a\", \"b\", \"c\"], \"-\"))\n打印(文本.重复(\"ab\", 3))",
        "stdout": "a-b-c\nababab\n",
    },
    {
        "id": "049_json转文本解析",
        "task": "先导入 json 模块，把字典 {\"键\": 42} 转成文本，再解析回对象，打印解析结果里「键」对应的值。",
        "answer": "导入 json\n令 t = json.转文本({\"键\": 42})\n令 r = json.解析(t)\n打印(r[\"键\"])",
        "stdout": "42\n",
    },
    {
        "id": "050_日期相差天数",
        "task": "导入日期模块，用日期字符串 \"2026-09-06\" 和 \"2026-09-01\" 直接调用相差天数，打印结果。",
        "answer": "导入 日期\n打印(日期.相差天数(\"2026-09-06\", \"2026-09-01\"))",
        "stdout": "5\n",
    },
]


def run_code(src: str) -> str:
    """运行基石代码，返回 stdout。"""
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src, "<评测>")
    return out.getvalue()


def run_code_result(src: str) -> tuple[bool, str, dict | None]:
    """运行基石代码，返回 (是否成功, stdout, 结构化错误 dict|None)。

    出错时若异常带 fix 字段（英文关键字/内建误写），一并返回。
    """
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            run_source(src, "<评测>")
        return True, out.getvalue(), None
    except JishiError as e:
        err = {
            "code": e.code, "message": e.message,
            "line": e.line, "col": e.col, "hint": e.hint,
        }
        if e.fix:
            err["fix"] = e.fix
        return False, out.getvalue(), err
    except Exception as e:  # noqa: BLE001 —— 兜底，避免评测被单个崩溃打断
        return False, out.getvalue(), {
            "code": "E9xxx", "message": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# M12.2 / M12.3：LLM 接入 + 自愈闭环（零第三方依赖，urllib 调 OpenAI 兼容接口）
# ---------------------------------------------------------------------------

_LANG_CARD_CACHE: str | None = None


def _lang_card() -> str:
    """语言卡（只读一次，供 LLM 作为系统提示）。"""
    global _LANG_CARD_CACHE
    if _LANG_CARD_CACHE is None:
        from jishi.ai import render_ai_card
        _LANG_CARD_CACHE = render_ai_card()
    return _LANG_CARD_CACHE


def _llm_config() -> dict | None:
    """从环境变量读 LLM 接入配置；无 key 返回 None（优雅降级）。"""
    key = os.environ.get("JISHI_EVAL_API_KEY", "").strip()
    if not key:
        return None
    return {
        "api_key": key,
        "base_url": os.environ.get(
            "JISHI_EVAL_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "model": os.environ.get("JISHI_EVAL_MODEL", "gpt-4o-mini"),
    }


def _chat(cfg: dict, system: str, user: str,
          retries: int = 5, backoff: float = 2.0) -> str:
    """调 OpenAI 兼容 /chat/completions，返回助手文本。零第三方依赖。

    遇到 429（限流）按指数退避重试；其余 HTTP 错误也重试（网络抖动）。
    """
    import time
    import urllib.error
    import urllib.request

    # temperature=0：回归基线必须可复现（否则每次跑通过率漂移，无法做门槛）
    temperature = float(os.environ.get("JISHI_EVAL_TEMPERATURE", "0"))
    body = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }).encode("utf-8")

    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{cfg['base_url']}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg['api_key']}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = backoff ** attempt
                print(f"      （限流，{wait:.0f}s 后重试 {attempt + 1}/{retries - 1}）",
                      file=sys.stderr)
                time.sleep(wait)
            elif e.code >= 500:
                wait = backoff ** attempt
                print(f"      （服务端错误 {e.code}，{wait:.0f}s 后重试）",
                      file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except Exception as e:  # noqa: BLE001 —— 网络抖动重试
            last_err = e
            wait = backoff ** attempt
            print(f"      （网络错误，{wait:.0f}s 后重试）", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"LLM 调用失败（重试 {retries} 次）：{last_err}")


def _extract_code(text: str) -> str:
    """从 LLM 回复里提取代码块（```jsh/```jishi/```基石/``` 包裹），否则原样返回。"""
    import re
    m = re.search(r"```(?:jsh|jishi|基石)?\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    return text.strip()


def _apply_fix(src: str, err: dict) -> str | None:
    """据结构化错误做文本替换（自愈），成功返回新源码，否则 None。

    目前只支持「整词替换」类 fix（英文关键字/内建 → 中文）。
    """
    fix = err.get("fix")
    if not fix:
        return None
    old, new = fix.get("old"), fix.get("new")
    if not old or not new:
        return None
    # 词边界替换：只替换独立出现的英文词（避免误伤变量名里的子串）
    import re
    pat = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])")
    if not pat.search(src):
        return None
    return pat.sub(new, src)


def eval_llm(cfg: dict, heal: bool = False) -> None:
    """用 LLM 生成代码跑全部任务，统计一次通过率与自愈率。"""
    import time

    system = (
        "你是一位精通「基石」（jishi）中文编程语言的专家。\n"
        "用户会用自然语言描述任务，你只输出基石代码，不要解释、不要多余文字。\n"
        "铁律：只用中文关键字（函数/如果/遍历/返回/打印/令/尝试/捕获 等），"
        "绝不写英文关键字；缩进用空格，冒号后换行缩进写代码块。\n\n"
        "以下是基石语言卡（语法与标准库速查）：\n\n" + _lang_card()
    )

    total = len(TASKS)
    first_pass = 0
    healed = 0
    errored = 0
    interval = float(os.environ.get("JISHI_EVAL_INTERVAL", "0"))
    print(f"JishiEval LLM 评测：共 {total} 题，模型 {cfg['model']}\n")

    for t in TASKS:
        try:
            code = _extract_code(_chat(cfg, system, t["task"]))
        except Exception as e:  # noqa: BLE001 —— 单题 LLM 失败不中断整体
            errored += 1
            print(f"  [调用失败] {t['id']}  {e}")
            continue

        ok, out, err = run_code_result(code)
        if ok and out == t["stdout"]:
            first_pass += 1
            print(f"  [一次通过] {t['id']}")
        elif heal:
            # 自愈闭环：读 fix 字段做文本替换，最多 2 轮
            rounds = 0
            cur = code
            while rounds < 2 and err and "fix" in err:
                nxt = _apply_fix(cur, err)
                if nxt is None or nxt == cur:
                    break
                cur = nxt
                rounds += 1
                ok, out, err = run_code_result(cur)
            if ok and out == t["stdout"]:
                healed += 1
                print(f"  [自愈通过] {t['id']}（{rounds} 轮）")
            else:
                print(f"  [失败] {t['id']}"
                      f"{'  ' + err.get('message','')[:40] if err else ''}")
        else:
            print(f"  [失败] {t['id']}"
                  f"{'  ' + err.get('message','')[:40] if err else ''}")

        if interval > 0:
            time.sleep(interval)

    print(f"\n一次通过率：{first_pass}/{total} = {first_pass/total*100:.0f}%")
    if errored:
        print(f"调用失败：{errored} 题（不计入通过率分母）")
    if heal:
        print(f"自愈率：{healed}/{total-first_pass}"
              f" = {healed/(total-first_pass)*100 if total>first_pass else 0:.0f}%"
              f"（未一次通过题中，两次内自愈成功的比例）")
        print(f"最终通过：{first_pass+healed}/{total}"
              f" = {(first_pass+healed)/total*100:.0f}%")


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

    # M12.2/M12.3：LLM 评测（需环境变量 JISHI_EVAL_API_KEY）
    if "--llm" in argv:
        cfg = _llm_config()
        if cfg is None:
            print("未检测到 JISHI_EVAL_API_KEY，无法进行 LLM 评测。\n"
                  "请设置：JISHI_EVAL_API_KEY（必填）、JISHI_EVAL_BASE_URL、\n"
                  "JISHI_EVAL_MODEL（可选，默认 gpt-4o-mini）后重试。\n"
                  "或用 --self-test 只跑标准答案自测。")
            return 2
        eval_llm(cfg, heal="--heal" in argv)
        return 0

    print(f"基石评测集（JishiEval）—— 共 {len(TASKS)} 题，标准答案自测：")
    passed, total = self_test()
    print(f"\n结果：{passed}/{total} 通过"
          f"（{'✓ 全部通过' if passed == total else '✗ 有失败'}）")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
