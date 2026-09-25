# -*- coding: utf-8 -*-
"""M13.2 Node.js 绑定测试：Python VM 与 JS VM 对拍。

要求本机装有 Node.js（node 命令在 PATH 中），否则自动跳过。
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, ".")

from conftest import tmp_bc_path   # noqa: E402

import pytest

from jishi.compiler import compile_source
from jishi import serialize, vm as vm_mod

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HAS_NODE = NODE is not None

pytestmark = pytest.mark.skipif(
    not HAS_NODE, reason="本机无 Node.js，跳过 JS VM 对拍")


def _node_run(bytecode_text: str) -> tuple[int, str]:
    bc = tmp_bc_path(ROOT / "_tmp_bc.json")
    bc.write_text(bytecode_text, encoding="utf-8")
    r = subprocess.run(
        [NODE, str(ROOT / "node" / "index.js"), str(bc)],
        capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout


def _py_run(src: str) -> str:
    cmod = compile_source(src, "<t>")
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(cmod, "<t>").run()
    return buf.getvalue()


def _compare(src: str) -> None:
    py_out = _py_run(src)
    text = serialize.dumps(compile_source(src, "<t>"))
    rc, node_out = _node_run(text)
    assert rc == 0, f"Node 崩溃"
    assert node_out == py_out, (
        f"输出不一致\nPython: {py_out!r}\nNode:   {node_out!r}")


def test_js_vm_arithmetic():
    _compare('打印(1 + 2 * 3)\n打印(7 / 2, 7 // 2, 7.0 + 1, 2 ** 0.5)')


def test_js_vm_list_and_dict():
    _compare(
        '令 甲 = [1, 2, 3]\n'
        '甲.追加(4)\n'
        '打印(甲)\n'
        '令 d = {"键": "值"}\n'
        '打印(d.键(), d.值())\n')


def test_js_vm_control_flow():
    _compare(
        '令 总和 = 0\n'
        '遍历 i 在 范围(1, 6):\n'
        '    如果 i % 2 == 0:\n'
        '        继续\n'
        '    总和 += i\n'
        '打印(总和)\n')


def test_js_vm_function_and_recursion():
    _compare(
        '函数 阶(n):\n'
        '    如果 n <= 1:\n'
        '        返回 1\n'
        '    返回 n * 阶(n - 1)\n'
        '打印(阶(6))\n')


def test_js_vm_closure():
    _compare(
        '函数 造(甲):\n'
        '    函数 取():\n'
        '        返回 甲 * 2\n'
        '    返回 取\n'
        '令 f = 造(21)\n'
        '打印(f())\n')


def test_js_vm_class_and_inheritance():
    _compare(
        '类 动物:\n'
        '    函数 叫(自身):\n'
        '        返回 "动物"\n'
        '类 狗 继承 动物:\n'
        '    函数 叫(自身):\n'
        '        返回 "汪汪"\n'
        '令 d = 新建 狗()\n'
        '打印(d.叫())\n')


def test_js_vm_exception():
    _compare(
        '尝试:\n'
        '    抛出 值错误("出错了")\n'
        '捕获 值错误 为 e:\n'
        '    打印(e.类型, e.消息)\n')


def test_js_vm_exception_base_catch():
    _compare(
        '尝试:\n'
        '    抛出 值错误("具体的错")\n'
        '捕获 运行期错误 为 e:\n'
        '    打印("用基类接住:", e.类型)\n')


def test_js_vm_comprehension_and_unpack():
    _compare(
        '令 平方 = [x * x 遍历 x 在 [1, 2, 3, 4, 5]]\n'
        '打印(总和(平方))\n'
        '令 a, b = [10, 20]\n'
        'a, b = b, a\n'
        '打印(a, b)\n')


def test_js_vm_default_param_and_interp():
    _compare(
        '函数 打招呼(名字, 语气 = "你好"):\n'
        '    打印(`{语气} {名字}`)\n'
        '打招呼("小明")\n'
        '打招呼("小红", "欢迎")\n')


def test_js_vm_stdlib_math():
    _compare('导入 数学\n打印(数学.开方(144))\n打印(数学.阶乘(5))\n')
