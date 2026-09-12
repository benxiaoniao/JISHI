# -*- coding: utf-8 -*-
"""PyInstaller 打包入口：jishi-mcp MCP Server。

MCP 是 JSON-RPC over stdio：请求从 stdin 进、响应往 stdout 出，两端都可能
夹带中文源码。打包版不继承 ``PYTHONUTF8``，所以三个流都要显式 UTF-8，
否则客户端发来的中文源码会按 GBK 解码成乱码（2026-09-12 发 v0.1.4 前发现）。
"""

import sys


def _force_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


_force_utf8()

from jishi.mcp_server import main

if __name__ == "__main__":
    sys.exit(main())
