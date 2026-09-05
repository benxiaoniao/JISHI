# -*- coding: utf-8 -*-
"""基石字节码跨语言序列化（M13.1）。

把 ``CompiledModule`` 序列化成**平台无关的 JSON 中间格式**，供任意宿主
（Python C 绑定 / 独立 C / Node / Rust）加载执行，脱离 Python 编译前端。

设计要点：
- 格式是纯 JSON（可读、易调试、任何语言都好解析），延续项目零依赖气质；
- 常量池只含标量（文本/整数/浮点/空/布尔）——基石的字面量列表/字典是运行时
  ``BUILD_LIST``/``BUILD_DICT`` 构造的，不进常量池，所以格式可以纯数据化；
- 带版本号 + 内容校验和，宿主先验版本再加载，防「新编译器产物喂旧引擎」。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import opcodes as O

#: 序列化格式版本号。格式变更（增删字段、改编码）时 +1，宿主据此拒绝不兼容产物。
FORMAT_VERSION = 1


def _const_to_json(c: Any) -> dict:
    """把常量池里的一个标量编码成 JSON 可表达的结构。"""
    if c is None:
        return {"t": "none"}
    if c is True:
        return {"t": "true"}
    if c is False:
        return {"t": "false"}
    if isinstance(c, int):
        return {"t": "int", "v": c}
    if isinstance(c, float):
        return {"t": "float", "v": c}
    if isinstance(c, str):
        return {"t": "str", "v": c}
    raise ValueError(
        f"常量池里出现了非标量值 {c!r}（类型 {type(c).__name__}）——"
        f"基石字面量列表/字典应在运行时构造，不应进常量池。"
        f"这是编译器 bug，请反馈。")


def _const_from_json(d: dict) -> Any:
    t = d["t"]
    if t == "none":
        return None
    if t == "true":
        return True
    if t == "false":
        return False
    if t == "int":
        return d["v"]
    if t == "float":
        return d["v"]
    if t == "str":
        return d["v"]
    raise ValueError(f"未知常量类型 {t!r}")


def _payload_json(cmod: O.CompiledModule) -> dict:
    """把编译产物转成 payload dict（序列化与校验和共用同一份结构）。"""
    codes = []
    for code in cmod.codes:
        codes.append({
            "name": code.name,
            "params": code.params,
            "param_idx": code.param_idx,
            "param_local": code.param_local,
            "param_cell": code.param_cell,
            "nlocals": code.nlocals,
            "local_names": code.local_names,
            "cellvars": code.cellvars,
            "freevars": code.freevars,
            "firstlineno": code.firstlineno,
            # 指令流：扁平数组，每条指令 6 个 int32（op,a,b,c,line,col）
            "instrs": [x for ins in code.instrs for x in ins.as_tuple()],
        })
    return {
        "consts": [_const_to_json(c) for c in cmod.consts],
        "names": cmod.names,
        "kw_names": cmod.kw_names,
        "codes": codes,
        "main": cmod.main,
        "filename": cmod.filename,
    }


def _checksum(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")).hexdigest()


def dump_json(cmod: O.CompiledModule) -> str:
    """把编译产物序列化成 JSON 字符串（跨语言中间格式）。"""
    payload = _payload_json(cmod)
    body = {
        "format": "jishi-bytecode",
        "version": FORMAT_VERSION,
        "opcode_names": O.OP_NAMES,   # 指令号 → 名字表（宿主校验指令集一致性）
        "checksum": _checksum(payload),
        "payload": payload,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def loads_json(text: str) -> dict:
    """反序列化 JSON 字节码，校验版本号与校验和，返回内部 dict。"""
    body = json.loads(text)
    if body.get("format") != "jishi-bytecode":
        raise ValueError("不是基石字节码格式")
    if body.get("version") != FORMAT_VERSION:
        raise ValueError(
            f"字节码版本 {body.get('version')} 与当前引擎 {FORMAT_VERSION} "
            f"不兼容，请用配套版本重新编译")
    payload = body["payload"]
    if _checksum(payload) != body.get("checksum"):
        raise ValueError("字节码校验和不匹配（文件已损坏）")
    return body


def payload_to_cmod(payload: dict) -> O.CompiledModule:
    """把反序列化后的 payload 还原成 CompiledModule（供 Python VM/C VM 用）。"""
    consts = [_const_from_json(c) for c in payload["consts"]]
    codes = []
    for cd in payload["codes"]:
        instrs = []
        flat = cd["instrs"]
        assert len(flat) % 6 == 0, "指令流长度必须是 6 的倍数"
        for i in range(0, len(flat), 6):
            op, a, b, c, line, col = flat[i:i + 6]
            instrs.append(O.Instr(op, a, b, c, line, col))
        codes.append(O.Code(
            name=cd["name"], params=cd["params"],
            param_idx=cd["param_idx"], param_local=cd["param_local"],
            param_cell=cd["param_cell"], instrs=instrs,
            nlocals=cd["nlocals"], local_names=cd["local_names"],
            cellvars=cd["cellvars"], freevars=cd["freevars"],
            firstlineno=cd["firstlineno"],
        ))
    return O.CompiledModule(
        consts=consts, names=payload["names"],
        kw_names=payload["kw_names"], codes=codes,
        main=payload["main"], filename=payload.get("filename", "<输入>"),
    )


def dumps(cmod: O.CompiledModule) -> str:
    """alias：序列化为 JSON 字符串。"""
    return dump_json(cmod)


def loads(text: str) -> O.CompiledModule:
    """alias：反序列化回 CompiledModule。"""
    return payload_to_cmod(loads_json(text)["payload"])
