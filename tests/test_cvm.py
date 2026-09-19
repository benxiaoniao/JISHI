# -*- coding: utf-8 -*-
"""对拍测试：同一份 .jsh，树遍历解释器与字节码 VM 必须逐字节一致。

这是 M4 的**硬保障**。字节码编译器和 VM 可以随便重构，
但只要这个文件全绿，就说明「换执行器不改变任何可观察行为」：

- 正常用例：stdout 逐字节相同
- 错误用例：渲染出的中文报错文本完全相同（含行列号）
- 需要输入的示例：用同一段脚本化输入喂给两个执行器

M4a：树遍历 vs Python 字节码 VM。
M4b：再叠加 C 虚拟机（三个执行器一致）。C 动态库不存在时 C 用例跳过。
"""

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jishi import vm as vm_mod
from jishi.compiler import compile_source
from jishi.errors import JishiError
from jishi.interpreter import run_source as run_tree
from jishi.vm import VM

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).parent / "cases"
ERROR_CASES = Path(__file__).parent / "error_cases"
EXAMPLES = ROOT / "examples"

#: C VM 动态库是否可用（不可用则三执行器用例降级为跳过）
def _cvm_available() -> bool:
    try:
        from jishi import cvm_bind
        cvm_bind.load_lib()
        return True
    except Exception:
        return False

HAS_CVM = _cvm_available()

if not HAS_CVM:
    # 对拍会降级为「树遍历 vs 字节码」双执行器（仍有效，但不是三重对拍）。
    # 必须显式告知，避免误以为 C VM 也一起验证了。
    import warnings

    warnings.warn(
        "C 虚拟机动态库不可用，三执行器对拍降级为双执行器"
        "（先运行 python cvm/build.py 构建）",
        stacklevel=2)

#: 需要读输入的示例，用同一段脚本化输入喂给两个执行器（够跑完即可）
SCRIPTED_INPUT = ["50", "25", "12", "6", "3", "1", "2", "7"]


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------

def _scripted_input():
    """每次调用返回下一条脚本化输入，用尽后循环（两次运行完全同构）。"""
    state = {"i": 0}

    def _input(prompt=""):
        v = SCRIPTED_INPUT[state["i"] % len(SCRIPTED_INPUT)]
        state["i"] += 1
        return v

    return _input


def _run_tree(src: str, filename: str) -> tuple[str, str]:
    buf = io.StringIO()
    err = ""
    with redirect_stdout(buf):
        try:
            run_tree(src, filename=filename)
        except JishiError as e:
            err = str(e)
        except BaseException as e:              # 非基石错误也要对得上
            err = f"{type(e).__name__}: {e}"
    return buf.getvalue(), err


def _run_vm(src: str, filename: str) -> tuple[str, str]:
    buf = io.StringIO()
    err = ""
    with redirect_stdout(buf):
        try:
            vm_mod.run_source(src, filename=filename)
        except JishiError as e:
            err = str(e)
        except BaseException as e:
            err = f"{type(e).__name__}: {e}"
    return buf.getvalue(), err


def _run_cvm(src: str, filename: str) -> tuple[str, str]:
    from jishi import cvm_bind

    buf = io.StringIO()
    err = ""
    with redirect_stdout(buf):
        try:
            cvm_bind.run_source_c(src, filename=filename)
        except JishiError as e:
            err = str(e)
        except BaseException as e:
            err = f"{type(e).__name__}: {e}"
    return buf.getvalue(), err


def _compare(src: str, filename: str, monkeypatch) -> None:
    import random as _random
    import tempfile

    def _run_isolated(runner) -> tuple[str, str]:
        """在独立临时目录里执行，隔离文件副作用（如记账程序写 账本.csv）。"""
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            try:
                return runner()
            finally:
                os.chdir(old_cwd)

    runners = [(_run_tree, "树遍历"), (_run_vm, "字节码")]
    if HAS_CVM:
        runners.append((_run_cvm, "C虚拟机"))

    results = []
    for runner, label in runners:
        monkeypatch.setattr("builtins.input", _scripted_input())
        _random.seed(20260901)      # 所有执行器用同一段随机序列
        got = _run_isolated(lambda: runner(src, filename))
        results.append((label, got))

    first_label, first = results[0]
    for other_label, other in results[1:]:
        assert first == other, (
            f"「{filename}」执行器结果不一致\n"
            f"  {first_label}：{first!r}\n"
            f"  {other_label}：{other!r}")


def _list(directory: Path, pattern: str = "*.jsh") -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.glob(pattern))


# ---------------------------------------------------------------------------
# 黄金用例 / 错误快照 / 示例
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("jsh", _list(CASES))
def test_cases_match(jsh, monkeypatch):
    _compare((CASES / jsh).read_text(encoding="utf-8"), jsh, monkeypatch)


@pytest.mark.parametrize("jsh", _list(ERROR_CASES))
def test_error_cases_match(jsh, monkeypatch):
    _compare((ERROR_CASES / jsh).read_text(encoding="utf-8"), jsh,
             monkeypatch)


@pytest.mark.parametrize("jsh", _list(EXAMPLES))
def test_examples_match(jsh, monkeypatch):
    _compare((EXAMPLES / jsh).read_text(encoding="utf-8"), jsh, monkeypatch)


@pytest.mark.parametrize("jsh", _list(EXAMPLES / "tutorial"))
def test_tutorial_examples_match(jsh, monkeypatch):
    src = (EXAMPLES / "tutorial" / jsh).read_text(encoding="utf-8")
    _compare(src, jsh, monkeypatch)


# ---------------------------------------------------------------------------
# 定向用例：专挑编译器容易写错的地方
# ---------------------------------------------------------------------------

SNIPPETS = {
    "三段链式比较_成立": "打印(1 < 2 < 3 < 4)",
    "三段链式比较_中段短路": "打印(1 < 2 < 0 < 4)",
    "三段链式比较_首段短路": "打印(9 < 2 < 3 < 4)",
    "四段链式比较": "x = 2\n打印(0 < x <= 2 < 3 < 10)",
    "与_全部为真": "打印(真 与 真 与 真)",
    "与_中间为假": "打印(真 与 假 与 真)",
    "或_首个为真": "打印(真 或 假 或 假)",
    "或_全假": "打印(假 或 假 或 假)",
    "与或混用": "打印((1 < 2 与 3 > 2) 或 假)",
    "嵌套循环与中断": (
        "遍历 i 在 范围(5)：\n"
        "    遍历 j 在 范围(5)：\n"
        "        如果 j == 2：\n"
        "            中断\n"
        "        打印(i, j)\n"
        "    如果 i == 1：\n"
        "        中断\n"),
    "嵌套循环与继续": (
        "遍历 i 在 范围(3)：\n"
        "    遍历 j 在 范围(4)：\n"
        "        如果 j % 2 == 0：\n"
        "            继续\n"
        "        打印(i, j)\n"),
    "当循环里的中断": (
        "i = 0\n"
        "当 真：\n"
        "    i = i + 1\n"
        "    如果 i > 3：\n"
        "        中断\n"
        "    打印(i)\n"),
    "循环N次里的中断": (
        "循环 10 次：\n"
        "    打印(\"跑\")\n"
        "    中断\n"),
    "循环N次里的继续": (
        "计 = 0\n"
        "循环 5 次：\n"
        "    计 = 计 + 1\n"
        "    如果 计 % 2 == 0：\n"
        "        继续\n"
        "    打印(计)\n"),
    "中断穿透函数调用": (
        "函数 提前退出()：\n"
        "    中断\n"
        "遍历 i 在 范围(5)：\n"
        "    打印(\"第\", i)\n"
        "    如果 i == 1：\n"
        "        提前退出()\n"),
    "递归阶乘": (
        "函数 阶乘(n)：\n"
        "    如果 n <= 1：\n"
        "        返回 1\n"
        "    返回 n * 阶乘(n - 1)\n"
        "打印(阶乘(10))\n"),
    "递归斐波那契": (
        "函数 斐波(n)：\n"
        "    如果 n < 2：\n"
        "        返回 n\n"
        "    返回 斐波(n - 1) + 斐波(n - 2)\n"
        "打印(斐波(15))\n"),
    "只读闭包": (
        "函数 外层(名字)：\n"
        "    招呼 = \"你好，\" + 名字\n"
        "    函数 内层()：\n"
        "        打印(招呼)\n"
        "    内层()\n"
        "外层(\"基石\")\n"),
    "闭包捕获参数": (
        "函数 造(甲, 乙)：\n"
        "    函数 求和()：\n"
        "        返回 甲 + 乙\n"
        "    返回 求和\n"
        "打印(造(3, 4)())\n"),
    "闭包捕获后调用多次": (
        "函数 造(基数)：\n"
        "    函数 加倍(x)：\n"
        "        返回 基数 + x\n"
        "    返回 加倍\n"
        "f = 造(100)\n"
        "打印(f(1), f(2), f(3))\n"),
    "函数作回调": (
        "函数 平方(x)：\n"
        "    返回 x * x\n"
        "打印(总和([平方(1), 平方(2), 平方(3)]))\n"),
    "内建map用基石函数": (
        "导入 json 从 python\n"
        "函数 加倍(x)：\n"
        "    返回 x * 2\n"
        "打印(json.dumps([加倍(1), 加倍(2)]))\n"),
    "关键字参数": (
        "导入 json 从 python\n"
        "打印(json.dumps({\"名\": \"基石\"}, ensure_ascii=假))\n"),
    "关键字参数混合位置": (
        "函数 介绍(姓名, 年纪, 城市)：\n"
        "    返回 姓名 + \"(\" + 文本(年纪) + \")来自\" + 城市\n"
        "打印(介绍(\"张三\", 30, \"未知\"))\n"
        "打印(介绍(\"李四\", 25, 城市=\"北京\"))\n"),
    "复合赋值_下标": (
        "甲 = [1, 2, 3]\n"
        "甲[0] += 10\n"
        "甲[1] *= 5\n"
        "甲[2] -= 1\n"
        "打印(甲)\n"),
    "复合赋值_字典": (
        "丁 = {\"计\": 1}\n"
        "丁[\"计\"] += 41\n"
        "打印(丁[\"计\"])\n"),
    "复合赋值_全局": (
        "计 = 1\n"
        "函数 加()：\n"
        "    计 = 2\n"
        "    返回 计\n"
        "打印(加())\n"
        "打印(计)\n"),
    "多分支如果": (
        "函数 评级(分)：\n"
        "    如果 分 >= 90：\n"
        "        返回 \"优\"\n"
        "    否则如果 分 >= 80：\n"
        "        返回 \"良\"\n"
        "    否则如果 分 >= 60：\n"
        "        返回 \"及格\"\n"
        "    否则：\n"
        "        返回 \"不及格\"\n"
        "打印(评级(95), 评级(85), 评级(70), 评级(50))\n"),
    "遍历文本": (
        "遍历 字 在 \"基石\"：\n"
        "    打印(字)\n"),
    "遍历字典": (
        "丁 = {\"甲\": 1, \"乙\": 2}\n"
        "遍历 键 在 丁.键()：\n"
        "    打印(键, 丁[键])\n"),
    "空列表与空字典": (
        "甲 = []\n"
        "丁 = {}\n"
        "打印(甲, 丁, 长度(甲), 长度(丁))\n"),
    "嵌套函数定义": (
        "函数 外()：\n"
        "    函数 中()：\n"
        "        函数 内()：\n"
        "            返回 \" innermost \"\n"
        "        返回 \"中\" + 内()\n"
        "    返回 中()\n"
        "打印(外())\n"),
    "返回空": (
        "函数 什么也不做()：\n"
        "    返回\n"
        "打印(什么也不做())\n"),
    "无返回值的函数": (
        "函数 只打印()：\n"
        "    打印(\"打印了\")\n"
        "打印(只打印())\n"),
    "一元与非": "打印(-5, 非 真, 非 假, 非 空)",
    "整数与浮点混算": "打印(7 / 2, 7 // 2, 7.0 + 1, 2 ** 0.5)",
    "变量遮蔽内建": "打印 = 1\n打印2 = 打印 + 1\n打印3 = 打印2",

    # --- M5a 异常处理 ---
    "捕获值错误": (
        "尝试：\n"
        "    令 x = 整数(\"abc\")\n"
        "捕获 类型错误 为 e：\n"
        "    打印(\"捕获到:\", e.类型, e.消息)\n"),
    "抛出与捕获": (
        "函数 折扣(原价, 折)：\n"
        "    如果 折 <= 0 或 折 > 1：\n"
        "        抛出 值错误(\"折扣要在 0 到 1 之间\")\n"
        "    返回 原价 * 折\n"
        "尝试：\n"
        "    打印(折扣(100, 2))\n"
        "捕获 值错误 为 e：\n"
        "    打印(\"错误:\", e.消息)\n"),
    "裸捕获全接": (
        "尝试：\n"
        "    抛出 \"余额不足\"\n"
        "捕获 为 e：\n"
        "    打印(\"接住:\", e.消息)\n"),
    "最终块总是执行": (
        "尝试：\n"
        "    打印(1)\n"
        "    抛出 异常(\"坏了\")\n"
        "捕获 异常：\n"
        "    打印(\"捕获\")\n"
        "最终：\n"
        "    打印(\"收尾\")\n"),
    "多个捕获按序匹配": (
        "尝试：\n"
        "    抛出 键错误(\"没有这个键\")\n"
        "捕获 类型错误：\n"
        "    打印(\"类型错误\")\n"
        "捕获 键错误：\n"
        "    打印(\"键错误\")\n"
        "捕获 异常：\n"
        "    打印(\"兜底\")\n"),
    "基类捕获抓子类": (
        "尝试：\n"
        "    抛出 值错误(\"具体的错\")\n"
        "捕获 运行期错误 为 e：\n"
        "    打印(\"用基类接住:\", e.类型)\n"),
    "未捕获异常冒泡": (
        "函数 内层()：\n"
        "    抛出 值错误(\"深层错误\")\n"
        "函数 外层()：\n"
        "    内层()\n"
        "尝试：\n"
        "    外层()\n"
        "捕获 值错误 为 e：\n"
        "    打印(\"顶层接住:\", e.消息)\n"),
    "try内返回带最终": (
        "函数 f()：\n"
        "    尝试：\n"
        "        返回 1\n"
        "    最终：\n"
        "        打印(\"清理\")\n"
        "打印(f())\n"),
    "循环内try中断带最终": (
        "遍历 i 在 范围(4)：\n"
        "    尝试：\n"
        "        打印(i)\n"
        "        如果 i == 2：\n"
        "            中断\n"
        "    最终：\n"
        "        打印(\"收\", i)\n"),
    "try内中断穿透函数": (
        "函数 提()：\n"
        "    中断\n"
        "遍历 i 在 范围(5)：\n"
        "    打印(\"第\", i)\n"
        "    如果 i == 1：\n"
        "        提()\n"),
    "最终覆盖返回": (
        "函数 g()：\n"
        "    尝试：\n"
        "        返回 \"原来的\"\n"
        "    最终：\n"
        "        返回 \"覆盖的\"\n"
        "打印(g())\n"),

    # --- M5b 面向对象 ---
    "类与实例": (
        "类 狗：\n"
        "    函数 初始化(自身, 名字)：\n"
        "        自身.名字 = 名字\n"
        "    函数 叫(自身)：\n"
        "        打印(自身.名字, \"汪汪\")\n"
        "令 d = 新建 狗(\"旺财\")\n"
        "d.叫()\n"),
    "类继承与覆盖": (
        "类 动物：\n"
        "    函数 初始化(自身, 名)：\n"
        "        自身.名 = 名\n"
        "    函数 叫(自身)：\n"
        "        打印(自身.名, \"在叫\")\n"
        "类 猫 继承 动物：\n"
        "    函数 叫(自身)：\n"
        "        打印(自身.名, \"喵喵\")\n"
        "令 c = 新建 猫(\"咪咪\")\n"
        "c.叫()\n"),
    "类字段与多方法": (
        "类 账户：\n"
        "    函数 初始化(自身, 余额)：\n"
        "        自身.余额 = 余额\n"
        "    函数 存(自身, 数额)：\n"
        "        自身.余额 = 自身.余额 + 数额\n"
        "    函数 查(自身)：\n"
        "        返回 自身.余额\n"
        "令 a = 新建 账户(100)\n"
        "a.存(50)\n"
        "打印(a.查())\n"),
    "类无构造方法": (
        "类 点：\n"
        "    函数 描述(自身)：\n"
        "        返回 \"一个点\"\n"
        "令 p = 新建 点()\n"
        "打印(p.描述())\n"),
    "类继承用基类构造": (
        "类 基：\n"
        "    函数 初始化(自身, x)：\n"
        "        自身.x = x\n"
        "    函数 取(自身)：\n"
        "        返回 自身.x\n"
        "类 子 继承 基：\n"
        "    函数 加倍(自身)：\n"
        "        返回 自身.取() * 2\n"
        "令 s = 新建 子(21)\n"
        "打印(s.加倍())\n"),

    # --- M7 表达力 ---
    "多赋值列表": (
        "令 a, b = [1, 2]\n"
        "打印(a, b)\n"),
    "多赋值交换": (
        "令 a = 1\n"
        "令 b = 2\n"
        "a, b = b, a\n"
        "打印(a, b)\n"),
    "多值返回与解包": (
        "函数 两数()：\n"
        "    返回 3, 4\n"
        "令 x, y = 两数()\n"
        "打印(x + y)\n"),
    "多赋值嵌套元素": (
        "令 a, b = [\"甲\", [1, 2]]\n"
        "打印(a, b)\n"),

    # --- M7.2 默认参数 ---
    "默认参数缺省与覆盖": (
        "函数 打招呼(名字, 语气 = \"你好\")：\n"
        "    打印(语气, 名字)\n"
        "打招呼(\"小明\")\n"
        "打招呼(\"小明\", \"欢迎\")\n"),
    "默认值引用先前定义": (
        "令 基 = 10\n"
        "函数 f(a, b = 基)：\n"
        "    返回 a + b\n"
        "打印(f(1))\n"),
    "默认值定义处求值一次": (
        "令 计 = 0\n"
        "函数 f(a = 计)：\n"
        "    返回 a\n"
        "计 = 5\n"
        "打印(f())\n"),
    "关键字参数配默认": (
        "函数 f(a, b = 9)：\n"
        "    打印(a, b)\n"
        "f(a = 1)\n"
        "f(1, b = 2)\n"),
    "方法默认参数": (
        "类 面积：\n"
        "    函数 算(自身, 倍 = 2)：\n"
        "        返回 倍 * 10\n"
        "令 m = 新建 面积()\n"
        "打印(m.算())\n"
        "打印(m.算(3))\n"),

    # --- M7.3 推导式 ---
    "列表推导": (
        "打印([x * 2 遍历 x 在 [1, 2, 3]])\n"),
    "列表推导带条件": (
        "打印([x * 2 遍历 x 在 [1, 2, 3, 4] 如果 x > 2])\n"),
    "字典推导": (
        "打印({x: x * x 遍历 x 在 [1, 2, 3]})\n"),
    "字典推导带条件": (
        "打印({x: x * x 遍历 x 在 [1, 2, 3, 4] 如果 x > 2})\n"),
    "推导式遍历文本": (
        "打印([x 遍历 x 在 \"你好\"])\n"),
    "函数内推导式": (
        "函数 f()：\n"
        "    返回 [x 遍历 x 在 [1, 2, 3]]\n"
        "打印(f())\n"),

    # --- M7.4 文本插值 ---
    "插值基本": (
        "令 名字 = \"小明\"\n"
        "令 年龄 = 18\n"
        "打印(`你好 {名字}，今年 {年龄} 岁`)\n"),
    "插值表达式": (
        "令 a = 3\n"
        "令 b = 4\n"
        "打印(`{a} + {b} = {a + b}`)\n"),
    "插值字面花括号": (
        "打印(`使用 {{花括号}} 转义`)\n"),
    "插值纯文本": (
        "打印(`无插值`)\n"),
    "插值嵌套调用": (
        "令 数据 = [1, 2, 3]\n"
        "打印(`长度 {长度(数据)}`)\n"),
    # -- M18.1 切片 --
    "切片_起止": "令 a = [1,2,3,4,5]\n打印(a[1:3])",
    "切片_倒序": "令 a = [1,2,3,4,5]\n打印(a[::-1])",
    "切片_省略起": "令 a = [1,2,3,4,5]\n打印(a[:2])",
    "切片_省略止": "令 a = [1,2,3,4,5]\n打印(a[2:])",
    "切片_步长": "令 a = [1,2,3,4,5]\n打印(a[::2])",
    "切片_全复制": "令 a = [1,2,3,4,5]\n打印(a[:])",
    "切片_负步长": "令 a = [1,2,3,4,5]\n打印(a[4:1:-1])",
    "切片_字符串": '令 s = "你好世界"\n打印(s[1:3])',
    "切片_字符串倒序": '令 s = "你好世界"\n打印(s[::-1])',
    "切片_变量下标": "令 a = [10,20,30,40]\n令 i = 1\n令 j = 3\n打印(a[i:j])",
    "切片_嵌套表达式": "令 a = [0,1,2,3,4,5,6]\n打印(a[1+1:2*3])",
}


@pytest.mark.parametrize("name", sorted(SNIPPETS))
def test_snippet_matches(name, monkeypatch):
    _compare(SNIPPETS[name], f"<{name}>", monkeypatch)


# ---------------------------------------------------------------------------
# 编译产物自检
# ---------------------------------------------------------------------------

def test_all_snippets_compile():
    """每个定向用例都能编译，且指令流的跳转目标都在范围内。"""
    for name, src in SNIPPETS.items():
        cmod = compile_source(src, f"<{name}>")
        for ci, code in enumerate(cmod.codes):
            n = len(code.instrs)
            for i, ins in enumerate(code.instrs):
                for field in ("a", "b", "c"):
                    pass
                if ins.op in _JUMP_OPS:
                    assert 0 <= ins.a <= n, (
                        f"「{name}」代码对象 #{ci} 第 {i} 条指令跳转越界："
                        f"→{ins.a}（共 {n} 条）")


def test_module_always_halts():
    """模块主代码必须以 HALT 结尾，否则 VM 会跑飞。"""
    from jishi.opcodes import Op

    for name, src in SNIPPETS.items():
        cmod = compile_source(src, f"<{name}>")
        assert cmod.codes[cmod.main].instrs[-1].op == Op.HALT, (
            f"「{name}」模块代码没有以 HALT 结尾")


def test_function_bodies_always_return():
    """每个函数体末尾都要有 RETURN（隐式「返回 空」也算）。"""
    from jishi.opcodes import Op

    for name, src in SNIPPETS.items():
        cmod = compile_source(src, f"<{name}>")
        for ci, code in enumerate(cmod.codes):
            if ci == cmod.main:
                continue
            assert code.instrs[-1].op == Op.RETURN, (
                f"「{name}」代码对象 #{ci}（{code.name}）没有以 RETURN 结尾")


_JUMP_OPS: set[int] = set()


def _init_jump_ops():
    from jishi.opcodes import Op

    _JUMP_OPS.update({
        Op.JUMP, Op.POP_JUMP_IF_FALSE, Op.POP_JUMP_IF_TRUE,
        Op.JUMP_IF_FALSE_OR_POP, Op.FOR_ITER,
    })


_init_jump_ops()


# ---------------------------------------------------------------------------
# 作用域：函数里给外层同名变量赋值 → 「赋值即局部」
# ---------------------------------------------------------------------------

def test_inner_assignment_to_outer_name_is_local(monkeypatch):
    """内层函数给**外层同名**变量赋值：应当「赋值即局部」（与 Python 一致）。

    这条原先（M39）是 `xfail(strict=True)`：树遍历按「先向外找、找到就用」
    静默算出另一个结果，字节码执行器却报「找不到这个名字」——一边给值一边报错。
    M40 补齐了树遍历（进函数时给「本函数赋过值的名字」占上未绑定位），
    于是三个执行器**连报错文案都逐字相同**，钉子转成正常断言。
    """
    src = ('函数 计数器(起始)：\n'
           '    令 当前 = 起始\n'
           '    函数 加(步长)：\n'
           '        当前 = 当前 + 步长\n'
           '        返回 当前\n'
           '    返回 加\n'
           '\n'
           '令 加 = 计数器(10)\n'
           '打印(加(5))\n')
    _compare(src, "<内层赋值即局部>", monkeypatch)


def test_read_local_before_assignment_is_an_error(monkeypatch):
    """函数体里「赋值之前读自己」要报错，而不是穿透到外层同名变量（M40）。"""
    src = ('令 x = 99\n'
           '函数 f()：\n'
           '    打印(x)\n'
           '    令 x = 1\n'
           'f()\n')
    _compare(src, "<赋值前读局部>", monkeypatch)
