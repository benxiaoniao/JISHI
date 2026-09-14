# -*- coding: utf-8 -*-
"""M13.1 跨语言字节码序列化测试。"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout

sys.path.insert(0, ".")

import pytest

from jishi.compiler import compile_source
from jishi import serialize, vm as vm_mod
from jishi import opcodes as O


def _run_vm(cmod, filename="<t>"):
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(cmod, filename).run()
    return buf.getvalue()


def _roundtrip(src):
    cmod = compile_source(src, "<t>")
    text = serialize.dumps(cmod)
    cmod2 = serialize.loads(text)
    return cmod, cmod2


def test_roundtrip_scalars():
    src = '令 a = "你好"\n令 b = 42\n令 c = 3.14\n令 d = 空\n令 e = 真\n令 f = 假\n打印(a, b, c, d, e, f)'
    cmod, cmod2 = _roundtrip(src)
    assert _run_vm(cmod) == _run_vm(cmod2)


def test_roundtrip_full_features():
    src = (
        "函数 阶(n):\n"
        "    如果 n <= 1:\n"
        "        返回 1\n"
        "    返回 n * 阶(n - 1)\n"
        "令 平方 = [x * x 遍历 x 在 [1, 2, 3, 4, 5]]\n"
        "令 名 = \"小明\"\n"
        "函数 打招呼(名字, 语气 = \"你好\"):\n"
        "    打印(`{语气} {名字}，平方和 {总和(平方)}`)\n"
        "打招呼(名)\n"
        "打印(阶(6))\n")
    cmod, cmod2 = _roundtrip(src)
    assert _run_vm(cmod) == _run_vm(cmod2)
    assert "你好 小明，平方和 55\n720\n" == _run_vm(cmod2)


def test_roundtrip_class_and_exception():
    src = (
        "类 账户:\n"
        "    函数 初始化(自身, 余额):\n"
        "        自身.余额 = 余额\n"
        "    函数 存(自身, 数额):\n"
        "        自身.余额 += 数额\n"
        "令 a = 新建 账户(100)\n"
        "a.存(50)\n"
        "打印(a.余额)\n"
        "尝试:\n"
        "    抛出 值错误(\"出错了\")\n"
        "捕获 值错误 为 e:\n"
        "    打印(e.类型, e.消息)\n")
    cmod, cmod2 = _roundtrip(src)
    assert _run_vm(cmod) == _run_vm(cmod2)
    assert _run_vm(cmod2) == "150\n值错误 出错了\n"


def test_roundtrip_closure():
    src = (
        "函数 造(甲):\n"
        "    函数 取():\n"
        "        返回 甲 * 2\n"
        "    返回 取\n"
        "令 f = 造(21)\n"
        "打印(f())\n")
    cmod, cmod2 = _roundtrip(src)
    assert _run_vm(cmod) == _run_vm(cmod2) == "42\n"


def test_checksum_detects_corruption():
    cmod = compile_source("打印(1)", "<t>")
    text = serialize.dumps(cmod)
    # 破坏 payload（改一个字节码数字）
    body = __import__("json").loads(text)
    body["payload"]["codes"][0]["instrs"][0] = 999
    import json
    corrupted = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    with pytest.raises(ValueError, match="校验和"):
        serialize.loads_json(corrupted)


def test_version_mismatch_rejected():
    cmod = compile_source("打印(1)", "<t>")
    text = serialize.dumps(cmod)
    body = __import__("json").loads(text)
    body["version"] = 999
    import json
    bumped = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    with pytest.raises(ValueError, match="版本"):
        serialize.loads_json(bumped)


def test_format_contains_opcode_names():
    cmod = compile_source("打印(1)", "<t>")
    text = serialize.dumps(cmod)
    body = __import__("json").loads(text)
    assert body["opcode_names"] == O.OP_NAMES
    assert body["format"] == "jishi-bytecode"
    assert body["version"] == serialize.FORMAT_VERSION
