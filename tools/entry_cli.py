# -*- coding: utf-8 -*-
"""PyInstaller 打包入口：jishi CLI。

在包外，用绝对导入调用 jishi.cli:main，避免 PyInstaller 直接打包
「包内模块」时相对导入（`from .errors import ...`）找不到父包的坑。
并强制 stdout/stderr 用 UTF-8，规避 Windows 控制台 GBK 编码乱码。
"""

import sys

# 强制 UTF-8 输出，避免 Windows GBK 控制台遇到中文/特殊符号崩溃（M16.5）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from jishi.cli import main

if __name__ == "__main__":
    sys.exit(main())
