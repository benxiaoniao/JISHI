# -*- coding: utf-8 -*-
"""PyInstaller 打包入口：jishi-mcp MCP Server。"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from jishi.mcp_server import main

if __name__ == "__main__":
    sys.exit(main())
