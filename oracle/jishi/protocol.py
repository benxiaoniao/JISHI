# -*- coding: utf-8 -*-
"""M9.2 统一执行结果协议：基石执行结果的结构化表示。

供沙箱运行器、MCP Server、CLI `--json-result` 共用，agent 据此程序化
读取执行结果（成功 / 失败、stdout、耗时、错误详情）。

成功：{"ok": true, "value": ..., "stdout": "...", "duration_ms": ...}
失败：{"ok": false, "error": {"code","title","message","line","col","hint","fix"},
       "duration_ms": ...}
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .errors import JishiError


def success(stdout: str = "", value: Any = None,
            duration_ms: float = 0.0) -> dict:
    """构造成功结果。"""
    return {
        "ok": True,
        "value": value,
        "stdout": stdout,
        "duration_ms": round(duration_ms, 2),
    }


def failure(error: JishiError, duration_ms: float = 0.0) -> dict:
    """构造失败结果；error 序列化为 agent 可读的 JSON（复用 M8.3）。"""
    from .ai import error_to_json
    return {
        "ok": False,
        "error": error_to_json(error),
        "duration_ms": round(duration_ms, 2),
    }


def dumps(result: dict, *, ensure_ascii: bool = False) -> str:
    """把结果序列化为 JSON 字符串。"""
    return json.dumps(result, ensure_ascii=ensure_ascii)
