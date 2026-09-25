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


# -- M46：迭代器批量快路径的类型覆盖 -----------------------------------------
#
# M46 阶段一①把「一次 pack + memmove」的快路径从「纯整数」扩到了
# **浮点 / 布尔 / 空 / 文本**（见 `cvm_bind._pack_batch`）。C 侧每次最多
# 填 256 个元素（`JS_ITER_BUF`），所以这里刻意用**跨多批**的长度，
# 并且覆盖「混合类型 → 回退逐个转换」的路径——回退路径与批量路径
# **必须给出逐字节相同的结果**，否则就是把性能优化做成了语义 bug。

_迭代类型用例 = {
    "整数": ('令 甲 = []\n'
             '遍历 i 在 范围(600)：\n'
             '    甲.追加(i * 3)\n'),
    "浮点": ('令 甲 = []\n'
             '遍历 i 在 范围(600)：\n'
             '    甲.追加(i * 1.5)\n'),
    "布尔": ('令 甲 = []\n'
             '遍历 i 在 范围(600)：\n'
             '    甲.追加(i % 2 == 0)\n'),
    "空值": ('令 甲 = []\n'
             '遍历 i 在 范围(600)：\n'
             '    甲.追加(空)\n'),
    "文本": ('令 甲 = []\n'
             '遍历 i 在 范围(600)：\n'
             '    甲.追加("值" + 文本(i))\n'),
}


@pytest.mark.parametrize("name,head", list(_迭代类型用例.items()))
def test_iter_batch_types_match(name, head, monkeypatch):
    """每种元素类型都要跨批次对拍一致（600 > 缓冲 256，必然跨 3 批）。"""
    _compare(head + '遍历 v 在 甲：\n    打印(v)\n',
             f"<迭代批量-{name}>", monkeypatch)


def test_iter_batch_mixed_types_match(monkeypatch):
    """混合类型走**回退**路径（`_pack_batch` 给 None），结果必须与树遍历一致。"""
    _compare('令 甲 = [1, 2.5, "甲", 真, 空, 3, "乙", 假]\n'
             '遍历 v 在 甲：\n'
             '    打印(v)\n',
             "<迭代批量-混合>", monkeypatch)


@pytest.mark.parametrize("head", [
    '["甲", 1, 2, 3]',          # 首个是文本、后面混进非文本
    '["甲", "乙", 空]',          # 文本 + 空
    '[真, 1]',                   # 布尔与整数（bool 是 int 子类，最易判错）
    '[1, 真]',
    '[空, "甲"]',
])
def test_iter_batch_mixed_start_matches(head, monkeypatch):
    """**首个元素的类型决定走哪个分支**，所以「首元素类型 + 后面混别的类型」
    的组合要单独覆盖——尤其文本打头的混合批：早先那版会**边建句柄边发现混类型**，
    中途回退就漏句柄（M46 自查发现，已改成「先确认整批类型、再建句柄」）。"""
    _compare(f'令 甲 = {head}\n'
             f'遍历 v 在 甲：\n'
             f'    打印(v)\n',
             f"<迭代批量-首元素混合{head}>", monkeypatch)


@pytest.mark.parametrize("n", [255, 256, 257, 512, 513])
def test_iter_batch_boundary_lengths(n, monkeypatch):
    """正好卡在批次边界（256）前后的长度——边界差一个元素也要对拍一致。"""
    _compare(f'令 甲 = []\n'
             f'遍历 i 在 范围({n})：\n'
             f'    甲.追加(i)\n'
             f'遍历 v 在 甲：\n'
             f'    打印(v)\n',
             f"<迭代批量-边界{n}>", monkeypatch)


# -- M46 阶段一③：追加「延迟合并写」的冲刷纪律 -------------------------------
#
# C 侧把 `X.追加(v)` 攒在本地，等**下一个宿主回调之前**才落到 Python 列表里
# （`flush_pending`，由 `HC` 宏兜住所有回调点）。这条纪律一旦有漏，症状就是
# 「列表少了最后几条」——而且是**静默**的。所以下面把「追加之后**每一条**
# 观察路径」都过一遍：随便漏哪个冲刷点，用例都会红。

def test_append_then_observe_every_path(monkeypatch):
    """追加之后，所有能「观察」到列表的路径都必须看到那几条。"""
    src = (
        '函数 合计(表)：\n'
        '    令 和 = 0\n'
        '    遍历 v 在 表：\n'
        '        和 = 和 + v\n'
        '    返回 和\n'
        '令 甲 = []\n'
        '遍历 i 在 范围(300)：\n'
        '    甲.追加(i)\n'
        '打印(长度(甲))\n'
        '打印(甲[0], 甲[-1])\n'
        '打印(合计(甲))\n'
        '打印(甲[100:105])\n'
        '打印(总和(甲))\n'
        '令 丙 = [甲]\n'
        '打印(长度(丙[0]))\n'
        '令 字 = {"表": 甲}\n'
        '打印(长度(字["表"]))\n'
        '打印(甲 == 甲, 长度(甲) == 300)\n'
        '打印(长度(文本(甲)) > 0)\n'
        '打印(甲.包含(299))\n'
        '打印(甲 在 丙)\n'
        '甲.排序()\n'
        '打印(甲[0], 甲[-1])\n'
    )
    _compare(src, "<追加后走遍观察路径>", monkeypatch)


def test_append_then_error_caught(monkeypatch):
    """追加之后抛出、被 `尝试/捕获` 接住，再读列表——必须看到全部追加。

    C 侧在 `dispatch_err` 之前补的那次冲刷就是为这个场景：接住错误之后还要
    继续用那个列表，漏冲刷就等于「静默少几条」。
    """
    src = (
        '令 甲 = []\n'
        '遍历 i 在 范围(100)：\n'
        '    甲.追加(i)\n'
        '尝试：\n'
        '    抛出 值错误("测试")\n'
        '捕获 值错误 为 e：\n'
        '    打印("接住了", e.消息)\n'
        '打印(长度(甲), 甲[99])\n'
    )
    _compare(src, "<追加后报错被接住>", monkeypatch)


def test_append_in_function_returning_list(monkeypatch):
    """函数里边追加边返回——跨帧也要冲刷对。"""
    src = (
        '函数 造表(n)：\n'
        '    令 表 = []\n'
        '    遍历 i 在 范围(n)：\n'
        '        表.追加(i * i)\n'
        '    返回 表\n'
        '令 甲 = 造表(500)\n'
        '打印(长度(甲), 甲[499])\n'
        '令 乙 = 造表(3)\n'
        '打印(乙, 长度(乙))\n'
    )
    _compare(src, "<函数里追加后返回>", monkeypatch)


def test_append_nested_lists_match(monkeypatch):
    """嵌套列表：内层攒着的追加不能在外层被观察时漏掉。"""
    src = (
        '令 外层 = []\n'
        '遍历 i 在 范围(60)：\n'
        '    令 内 = []\n'
        '    遍历 j 在 范围(5)：\n'
        '        内.追加(i * 10 + j)\n'
        '    外层.追加(内)\n'
        '打印(长度(外层))\n'
        '令 总 = 0\n'
        '遍历 子 在 外层：\n'
        '    总 = 总 + 长度(子)\n'
        '打印(总, 外层[7][3])\n'
    )
    _compare(src, "<嵌套列表追加>", monkeypatch)


def test_append_mixed_types_then_observe(monkeypatch):
    """一批里混着不同类型：快路径（整批整数）走不了，必须退回落盘且顺序不变。"""
    src = (
        '令 甲 = []\n'
        '遍历 i 在 范围(40)：\n'
        '    如果 i % 3 == 0：\n'
        '        甲.追加("字" + 文本(i))\n'
        '    否则如果 i % 3 == 1：\n'
        '        甲.追加(i * 1.5)\n'
        '    否则：\n'
        '        甲.追加(i)\n'
        '打印(长度(甲))\n'
        '遍历 v 在 甲：\n'
        '    打印(长度(文本(v)))\n'
    )
    _compare(src, "<混合类型追加后观察>", monkeypatch)


# -- M46 阶段一④：字典写入「延迟合并写」的冲刷纪律 ---------------------------
#
# 与 ③ 同一套机制（C 侧攒住、下一个宿主回调前落盘），但**字典写入可能失败**
# （键不可哈希），所以批次里**按项存行列**，下面专门钉「报错行号/顺序/调用链」。

def test_dict_set_then_observe_every_path(monkeypatch):
    """字典写入之后，所有能「观察」到它的路径都必须看到那几条。"""
    src = (
        '函数 取值(字, k)：\n'
        '    返回 字[k]\n'
        '令 字 = {}\n'
        '遍历 i 在 范围(300)：\n'
        '    字[i] = i * 2\n'
        '打印(长度(字))\n'                # 长度 → call
        '打印(字[0], 字[299])\n'          # 下标 → getitem
        '打印(取值(字, 7))\n'             # 当实参 → call
        '打印(7 在 字)\n'                 # 成员测试 → compare/call
        '令 丙 = {"包": 字}\n'
        '打印(长度(丙["包"]))\n'          # 嵌进新字典 → build_dict
        '令 表 = [字]\n'
        '打印(长度(表[0]))\n'             # 嵌进列表 → build_list
        '打印(长度(文本(字)) > 0)\n'      # 文本化 → call
    )
    _compare(src, "<字典写入后走遍观察路径>", monkeypatch)


def test_dict_unhashable_key_after_loop(monkeypatch):
    """循环写完之后用不可哈希键：报错行号必须与树遍历一致。"""
    _compare('令 字 = {}\n'
             '令 键 = [1, 2]\n'
             '遍历 i 在 范围(5)：\n'
             '    字[i] = i\n'
             '字[键] = 9\n',
             "<不可哈希键-循环后>", monkeypatch)


def test_dict_unhashable_key_inside_loop(monkeypatch):
    """循环体内立刻用不可哈希键：报错必须发生，且**报错前那句打印不能执行**
    （冲刷在下一个宿主回调之前，顺序不能乱）。"""
    _compare('令 字 = {}\n'
             '遍历 i 在 范围(5)：\n'
             '    字[[i]] = i\n'
             '    打印("不该出现", i)\n',
             "<不可哈希键-循环内>", monkeypatch)


def test_dict_unhashable_key_with_call_chain(monkeypatch):
    """跨函数时也要带「调用链」——冲刷失败必须走正常的错误路径
    （踩过的坑：`_guard` 早先会拦掉 `note_frames`，把调用链弄丢）。"""
    _compare('函数 填(字)：\n'
             '    遍历 i 在 范围(5)：\n'
             '        字[[i]] = i\n'
             '令 字 = {}\n'
             '填(字)\n',
             "<不可哈希键-带调用链>", monkeypatch)


def test_dict_string_keys_and_nesting(monkeypatch):
    """字符串键 + 嵌套字典：攒批后观察仍要一致。"""
    _compare('令 外层 = {}\n'
             '遍历 i 在 范围(60)：\n'
             '    令 内 = {}\n'
             '    遍历 j 在 范围(4)：\n'
             '        内["k" + 文本(j)] = i * 10 + j\n'
             '    外层["行" + 文本(i)] = 内\n'
             '打印(长度(外层))\n'
             '打印(外层["行7"]["k3"])\n',
             "<字符串键与嵌套字典>", monkeypatch)


def test_list_and_dict_interleaved(monkeypatch):
    """列表追加与字典写入交错攒批：两个批次都要按序落盘。"""
    _compare('令 表 = []\n'
             '令 字 = {}\n'
             '遍历 i 在 范围(80)：\n'
             '    表.追加(i)\n'
             '    字[i] = i * 2\n'
             '打印(长度(表), 长度(字), 表[79], 字[79])\n',
             "<列表与字典交错攒批>", monkeypatch)


# -- M47 阶段二①：C 侧原生字符串（JS_TAG_STR） ------------------------------
#
# 文本在 C VM 里不再走 HOST 句柄，而是 C 自己持有的**不可变 UTF-8 字符串**
# （`cvm/src/jsvm.c` 的 `JsStr`）。于是 `甲 + "字"`、`甲 == "字"` 都在 C 侧
# 就地完成，不再回调宿主——实测「字符串拼接」0.35x → 约 3x、「文本比较」
# 1.1x → 约 12x（详见 tools/benchmark.py 与 CHANGELOG）。
#
# 这一组测试盯三件事：
#   1. **跨语言布局**：Python 侧镜像了 `JsStr` 的头部（为省 FFI），C 侧改了、
#      这边没跟上就会静默读错——用一次性往返把它钉死；
#   2. **边界**：空串 / 含 NUL / 代理项 / 跨批次的长度；
#   3. **快路径确实生效**（否则优化悄悄失效，测试还是全绿）。


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
def test_native_string_layout():
    """Python 镜像的 `_JsStr` 必须与 C 侧的 `JsStr` 逐字段一致。

    这是跨语言契约：`c2py` 直读头部拿字节数（不走 `jsvm_str_len`，
    为的是省掉热路径上的两次 ctypes 往返）。**两侧错位就是静默读错内存**，
    所以这里用 C 侧公开的 `jsvm_str_len` / `jsvm_str_bytes` 当裁判，
    校验 Python 镜像读到的与 C 自己说的完全一样。
    """
    import ctypes

    from jishi import cvm_bind as CB
    from jishi.compiler import compile_source

    cmod = compile_source('令 x = "字"\n', "<layout>")
    host = CB.CvmHost(cmod, "<layout>", list(cmod.names))
    try:
        for text in ("", "a", "值123", "中文与 emoji 🙂", "x" * 300):
            v = host.str2c(text)
            raw = text.encode("utf-8", "surrogatepass")

            # C 侧自己怎么说
            assert host.lib.jsvm_str_len(ctypes.byref(v)) == len(raw)
            ptr = host.lib.jsvm_str_bytes(ctypes.byref(v))
            assert ctypes.string_at(ptr, len(raw)) == raw

            # Python 镜像怎么说（`c2py` 走的就是这条路）
            assert CB._JsStr.from_address(int(v.v.u)).blen == len(raw)
            assert host.c2py(v) == text

        # 非原生字符串：C 侧公开 API 的约定是 -1 / NULL
        assert host.lib.jsvm_str_len(ctypes.byref(CB.JVal())) == -1
        assert not host.lib.jsvm_str_bytes(ctypes.byref(CB.JVal()))
    finally:
        host.destroy()


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
def test_native_string_batch_matches_single():
    """批量构造（`jsvm_str_new_batch`）与逐个构造必须给出完全一样的值。

    文本迭代器走的就是批量这条路（每批最多 256 个），所以它必须与
    `py2c` 的逐个路径逐字节等价——否则「遍历文本」和别的取字符方式会分家。
    """
    import ctypes

    from jishi import cvm_bind as CB
    from jishi.compiler import compile_source

    cmod = compile_source('令 x = "字"\n', "<batch>")
    host = CB.CvmHost(cmod, "<batch>", list(cmod.names))
    try:
        texts = ["", "a", "中文", "🙂", "x" * 300, "\x00", "a\x00b"]
        one = [host.c2py(host.str2c(t)) for t in texts]
        blob = host._str_batch(texts)
        got = [
            host.c2py(CB.JVal.from_buffer_copy(
                blob[i * CB._JVAL_SIZE:(i + 1) * CB._JVAL_SIZE]))
            for i in range(len(texts))
        ]
        assert got == one == texts
        # 含 NUL 的文本也必须原样回来（批量路径同样按长度切，不看 NUL）
        assert got[5] == "\x00" and got[6] == "a\x00b"
    finally:
        host.destroy()


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
def test_native_string_surrogate_roundtrip():
    """落单代理项必须能过 C 边界且**可逆**。

    基石的源码里造不出代理项（没有 `字符()` 那种内建），但宿主传进来的
    Python `str` 可能有（比如读了一个编码错乱的文件）。默认的 UTF-8 编码
    会在这里抛 `UnicodeEncodeError` —— 那就成了「树遍历能跑、C VM 不能跑」。
    所以两侧都走 `surrogatepass`；这条路径**没有对拍覆盖**，只能在这里钉
    （源码里写不出代理项，对拍用例造不出来）。

    顺带验一条语义前提：代理项在 UTF-8 下也是 3 字节且字节序与码点序一致
    （ED A0 80 < EE 80 80），所以 C 侧的字节比较与 Python 的码点比较同序。
    """
    from jishi import cvm_bind as CB
    from jishi.compiler import compile_source

    cmod = compile_source('令 x = "字"\n', "<surrogate>")
    host = CB.CvmHost(cmod, "<surrogate>", list(cmod.names))
    try:
        for text in ("\ud800", "a\udfffb", "\ud800\ud801"):
            assert host.c2py(host.str2c(text)) == text
        a, b, c = "\ud800", "\ue000", "\uffff"
        assert (a < b < c) == (
            a.encode("utf-8", "surrogatepass")
            < b.encode("utf-8", "surrogatepass")
            < c.encode("utf-8", "surrogatepass"))
    finally:
        host.destroy()


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
def test_native_string_avoids_host_binop(monkeypatch):
    """`甲 + "字"` / `甲 == "字"` 必须**不走宿主**（否则快路径悄悄失效）。

    这条是「优化确实生效」的看门狗：把宿主的 `binop`/`compare` 回调换成
    计数器，跑完断言两边都没被调到。对拍只看输出，抓不到「优化退化成慢路径」。
    """
    from jishi import cvm_bind as CB

    calls = {"binop": 0, "compare": 0}
    orig_binop, orig_compare = CB.CvmHost._cb_binop, CB.CvmHost._cb_compare

    def spy_binop(self, *a):
        calls["binop"] += 1
        return orig_binop(self, *a)

    def spy_compare(self, *a):
        calls["compare"] += 1
        return orig_compare(self, *a)

    monkeypatch.setattr(CB.CvmHost, "_cb_binop", spy_binop)
    monkeypatch.setattr(CB.CvmHost, "_cb_compare", spy_compare)

    src = (
        '令 甲 = "a"\n'
        '令 乙 = 甲 + "b"\n'
        '打印(乙 == "ab")\n'
        '令 积 = ""\n'
        '遍历 i 在 范围(50)：\n'
        '    积 = 积 + "字"\n'
        '打印(长度(积), 积 == 甲)\n'
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        CB.run_source_c(src, filename="<native-str>")
    assert buf.getvalue() == "真\n50 假\n"
    assert calls == {"binop": 0, "compare": 0}, (
        f"文本的拼接/比较竟然回调了宿主：{calls}——快路径没生效")


def test_text_concat_and_compare_match(monkeypatch):
    """文本拼接 / 比较 / 身份 / 空串 / 含 NUL / 跨批次长度的对拍。"""
    _compare(
        '令 甲 = ""\n'
        '遍历 i 在 范围(300)：\n'
        '    甲 = 甲 + "字"\n'
        '打印(长度(甲))\n'
        '打印(甲[0:3], 甲[-1])\n'
        '令 乙 = "甲" + "乙" + "丙"\n'
        '打印(乙, 乙 == "甲乙丙", 乙 != "甲乙")\n'
        '打印("a" < "b", "甲" < "乙", "" < "a", "中" > "a")\n'
        '令 表 = ["乙", "甲", "", "a", "🙂", "中"]\n'
        '表.排序()\n'
        '打印(表)\n'
        '打印(甲 是 甲, 乙 不是 "甲")\n',
        "<文本拼接比较>", monkeypatch)


def test_text_with_embedded_nul_matches(monkeypatch):
    """含 NUL 的文本：读回来**不能**被截断。

    Python 侧有一条快路径用 `c_char_p` 读到第一个 NUL 为止（实测比
    `string_at` 快一倍），靠「读到的长度 == blen」来判断有没有被截断。
    这条用例就是钉那个判断的边界（含 NUL / 以 NUL 结尾 / 纯 NUL）。
    """
    _compare(
        '令 空 = ""\n'
        '令 甲 = "x" + "\u0000" + "y"\n'
        '令 乙 = "a" + "\u0000"\n'
        '令 丙 = "\u0000"\n'
        '打印(长度(甲), 长度(乙), 长度(丙), 长度(空))\n'
        '打印(甲 == "x" + "\u0000" + "y", 甲 == "x")\n'
        '打印(乙 == "a", 丙 == 空, 丙 != 空)\n'
        '令 字 = {}\n'
        '字[甲] = 1\n'
        '打印(长度(字), 字[甲], 甲 在 字, "x" 在 字)\n'
        '令 表 = [甲, 乙, 丙]\n'
        '打印(长度(表), 表.包含("a"), 表.包含(丙))\n',
        "<含NUL文本>", monkeypatch)


def test_text_iteration_matches(monkeypatch):
    """逐字符遍历文本：跨批次（256）与含多字节字符都要一致。"""
    _compare(
        '令 长 = ""\n'
        '遍历 i 在 范围(600)：\n'
        '    长 = 长 + "字"\n'
        '令 计 = 0\n'
        '令 各 = ""\n'
        '遍历 字 在 长：\n'
        '    计 = 计 + 1\n'
        '    各 = 各 + 字\n'
        '打印(计, 各 == 长)\n'
        '令 计2 = 0\n'
        '遍历 字 在 "中文🙂测试":\n'
        '    计2 = 计2 + 长度(字)\n'
        '打印(计2)\n',
        "<文本遍历>", monkeypatch)


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
def test_pinned_consts_cleared_on_close(monkeypatch):
    """文本常量的「钉住表」必须随 VM 关闭清空（否则复用 host 会误认指针）。"""
    from jishi import cvm_bind as CB
    from jishi.compiler import compile_source

    cmod = compile_source('令 甲 = "常量文本"\n打印(甲)\n', "<pin>")
    runner = CB.CvmRunner(cmod, "<pin>")
    assert runner.host._str_pinned, "文本常量应当被钉住（否则字面量当参数要重新解码）"
    runner.close()
    assert runner.host._str_pinned == {}


# -- M47 附带修掉：迭代器预取缓冲的「漏引用」（对拍抓不到的那类） -------------
#
# `FOR_ITER` 的批量预取缓冲 `it->buf[0..nbuf-1]` 每个槽位都由**缓冲区自己**
# 持有一份引用，消费时额外 retain 一次给栈。但早先**换批时不释放上一批、
# 销毁时只从 `it->pos` 开始放**（等于假定「消费即转移所有权」，与消费处的
# retain 配不上）——**每消费一个元素漏一份引用**。
#
# 它不会报错、不会让对拍变红，只会让长跑进程（LSP / MCP / REPL）内存一直涨：
# 实测遍历 1000 个文本一次漏约 **33KB**，遍历 400 个内层列表一次漏 **399 个句柄**。
# 可追溯到最早的 v0.2 快照（`retain_val(vm, it->buf[0])` 一直没配对的释放）。
#
# 这里用「**宿主句柄数**」当确定性判据：HOST 句柄的引用计数归零时宿主才把它
# 从 `_handles` 里删，所以漏一份引用 = 表里留一个条目。数字差距极大
# （修复前 403 / 修复后 3–6），不会误报。**别改成用 RSS 判**——那在 CI 上会飘。


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
@pytest.mark.parametrize("name,tail", [
    ("正常跑完", "令 c = 0\n遍历 v 在 甲：\n    c = c + 1\n打印(c)\n"),
    ("中途 中断", "令 c = 0\n遍历 v 在 甲：\n    c = c + 1\n"
                 "    如果 c >= 60：\n        中断\n打印(c)\n"),
    ("中途抛错被接住", "令 c = 0\n尝试：\n    遍历 v 在 甲：\n"
                       "        c = c + 1\n        如果 c >= 60：\n"
                       "            抛出 值错误(\"x\")\n"
                       "捕获 值错误：\n    打印(\"接住\")\n打印(c)\n"),
    ("嵌套两层", "令 d = 0\n遍历 v 在 甲：\n    遍历 w 在 甲：\n"
                 "        d = d + 1\n打印(d)\n"),
])
def test_iter_buffer_releases_every_slot(name, tail, monkeypatch):
    """迭代器缓冲必须把每个槽位都放掉——各条退出路径都要放干净。"""
    from jishi import cvm_bind as CB

    # ⚠️ 规模用 100 个元素（不是 400）：本测试验证的是「迭代器缓冲不漏引用」，
    # 与元素个数无关——100 个元素就足以让「修复前 ~103 个句柄 / 修复后 3–6」
    # 拉开差距。原先用 400，导致「嵌套两层」用例要跑 400×400=16 万次迭代、
    # 实测 43.6s（M51 回归提速时定位到）。降到 100 后嵌套降到 1 万次，
    # 语义与断言完全不变（`left < 30` 仍是几十倍的差距）。
    src = ('令 甲 = []\n'
           '遍历 i 在 范围(100)：\n'
           '    甲.追加([i])\n'      # 每个元素都是一个新列表 → 迭代时各建一个句柄
           + tail)
    runner = CB.CvmRunner(compile_source(src, f"<iter-ref-{name}>"),
                          f"<iter-ref-{name}>")
    try:
        with redirect_stdout(io.StringIO()):
            runner.run()
        left = len(runner.host._handles)
        # 修复前 403；修复后 3–6（还剩的几个是 VM 仍活着时合法持有的）
        assert left < 30, (
            f"「{name}」迭代结束后仍留着 {left} 个宿主句柄"
            "——迭代器预取缓冲漏放引用（见 release_buf 的不变式）")
    finally:
        runner.close()


# -- M48 阶段三②：方法调用融合（METHOD_CALL） --------------------------------
#
# `X.方法(实参)` 本来是 `GET_ATTR` + `CALL`，即**两次**宿主回调；实测每次回调
# 约 4µs（必要工作只 2–3µs，其余是 ctypes 编组的固定开销），于是每次方法调用
# 白花约 4µs。M48 让它编译成一条 `METHOD_CALL`，一次回调做完。
#
# 这一组盯三件事：
#   1. **融合确实生效**（否则优化悄悄失效，普通对拍还是全绿）；
#   2. **默认编译不含新指令**——`METHOD_CALL` 是 C VM 专属，别的宿主
#      （Python VM / Node / Rust）认不出它，给它们发就是「未知指令」；
#   3. **错误行列与老路径逐字一致**（取属性失败用属性名的列、调用失败用
#      左括号的列——老路径里这两件事由两条指令分别上报）。


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
def test_method_call_fusion_is_used(monkeypatch):
    """C VM 跑 `X.方法(...)` 时，必须是**一次**回调（`method_call`），
    而不是 `getattr` + `call` 两次。"""
    from jishi import cvm_bind as CB

    counts = {"method_call": 0, "getattr": 0, "call": 0}
    orig_m = CB.CvmHost._cb_method_call
    orig_g = CB.CvmHost._cb_getattr
    orig_c = CB.CvmHost._cb_call

    def sp_m(self, *a):
        counts["method_call"] += 1
        return orig_m(self, *a)

    def sp_g(self, *a):
        counts["getattr"] += 1
        return orig_g(self, *a)

    def sp_c(self, *a):
        counts["call"] += 1
        return orig_c(self, *a)

    monkeypatch.setattr(CB.CvmHost, "_cb_method_call", sp_m)
    monkeypatch.setattr(CB.CvmHost, "_cb_getattr", sp_g)
    monkeypatch.setattr(CB.CvmHost, "_cb_call", sp_c)

    src = ('令 甲 = "abcdef"\n'
           '令 计 = 0\n'
           '遍历 i 在 范围(50)：\n'
           '    如果 甲.开头是("a")：\n'
           '        计 = 计 + 1\n'
           '打印(计)\n')
    buf = io.StringIO()
    with redirect_stdout(buf):
        CB.run_source_c(src, filename="<fuse>")
    assert buf.getvalue() == "50\n"
    assert counts["method_call"] == 50, f"融合没生效：{counts}"
    assert counts["getattr"] == 0, f"还走了老路径的 GET_ATTR：{counts}"
    _ = counts["call"]  # 循环里的比较等仍会用到普通 CALL，不作断言


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
def test_method_call_fusion_matches_all_shapes(monkeypatch):
    """融合路径的对拍：零参 / 多参 / 嵌套实参 / 链式 / 自定义类 / 报错。"""
    _compare(
        '令 甲 = "abcdef"\n'
        '打印(甲.开头是("ab"), 甲.结尾是("ef"), 甲.替换("a", "x"))\n'
        '令 表 = [3, 1, 2]\n'
        '表.排序()\n'
        '表.追加(9)\n'
        '打印(表, 表.包含(1), 表.索引(3))\n'
        '令 字 = {"甲": 1}\n'
        '打印(字.获取("甲"), 字.获取("乙", 0))\n'
        '打印(甲.去空白().开头是("a"))\n'          # 链式（内层也是融合）
        '打印(长度(甲.拆分("d")))\n'               # 融合结果是别的调用的实参
        '类 箱：\n'
        '    令 初始化(自己, v)：\n'
        '        自己.值 = v\n'
        '    令 取(自己)：\n'
        '        返回 自己.值\n'
        '打印(箱(7).取())\n'
        '令 f = 表.追加\n'                        # 裸属性访问（不融合）
        'f(4)\n'
        '打印(表)\n',
        "<方法调用融合各形态>", monkeypatch)


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
def test_method_call_error_positions_match(monkeypatch):
    """报错行列必须与老路径逐字一致。

    老路径是两条指令：`GET_ATTR` 带**属性名**的列、`CALL` 带**左括号**的列。
    融合成一条之后，编译器把属性名的列塞进 `c` 供「取属性失败」用，
     调用失败仍用指令本身的列——这条用例就是钉这个区分的。
    """
    # 取属性失败：该报属性名的列
    _compare('令 甲 = [1]\n甲.排续()\n', "<方法不存在>", monkeypatch)
    # 调用体内出错：该报左括号的列
    _compare('令 甲 = [1]\n甲.追加()\n', "<方法参数个数>", monkeypatch)
    _compare('令 字 = {"a": 1}\n字.弹出("b")\n', "<弹出缺失键>", monkeypatch)
    # 多参数
    _compare('令 甲 = [1]\n甲.索引()\n', "<索引零参>", monkeypatch)


@pytest.mark.skipif(not HAS_CVM, reason="C 虚拟机动态库不可用")
def test_method_call_falls_back_with_keywords(monkeypatch):
    """带关键字参数时**不融合**（融合版没带 `kw_flat` 表），走老路径。

    为什么必须不融合：内置方法的实现是 `impl(obj, args, node)`，**不收关键字
    参数**。老路径会把关键字打包送进去（于是报 `_BoundMethod.__call__() got
    an unexpected keyword argument`），融合版则会把关键字整个丢掉、当成
    位置参数用 —— **报错文本就不一样了**。这条用例钉住「退化成老路径」。
    """
    from jishi import cvm_bind as CB

    counts = {"method_call": 0}
    orig = CB.CvmHost._cb_method_call

    def sp(self, *a):
        counts["method_call"] += 1
        return orig(self, *a)

    monkeypatch.setattr(CB.CvmHost, "_cb_method_call", sp)
    src = '令 表 = [1, 2, 3]\n打印(表.索引(值=2))\n'
    buf = io.StringIO()
    err = None
    try:
        with redirect_stdout(buf):
            CB.run_source_c(src, filename="<kw>")
    except BaseException as e:          # noqa: BLE001
        err = e
    assert counts["method_call"] == 0, "带关键字参数不该融合（会丢 kw_flat）"
    assert err is not None and "unexpected keyword argument" in str(err), (
        f"带关键字参数的报错应当来自 _BoundMethod 的绑定层，实得：{err!r}")


def test_default_compile_has_no_fused_opcode():
    """**默认编译必须不含 `METHOD_CALL`** —— 它是 C VM 专属指令。

    Python VM 与 Node / Rust 宿主都不认识它（会对未知指令报错）。所以
    `compile_source` 的默认值必须是「不融合」，只有 C VM 的入口显式打开。
    这条是保护其它宿主不被编译器新指令误伤的看门狗。
    """
    from jishi import opcodes as O

    src = '令 甲 = "abc"\n打印(甲.开头是("a"))\n'
    for fuse in (False, None):
        kw = {} if fuse is None else {"fuse_method_call": fuse}
        cmod = compile_source(src, "<默认>", **kw)
        ops = [ins.op for c in cmod.codes for ins in c.instrs]
        assert O.Op.METHOD_CALL not in ops, (
            "默认编译出现了 METHOD_CALL —— 其它语言宿主会报「未知指令」")
        assert O.Op.GET_ATTR in ops, "默认编译应当保留 GET_ATTR 的老形状"

    fused = compile_source(src, "<融合>", fuse_method_call=True)
    ops = [ins.op for c in fused.codes for ins in c.instrs]
    assert O.Op.METHOD_CALL in ops
