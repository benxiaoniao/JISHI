# -*- coding: utf-8 -*-
"""PyInstaller 打包入口：jishi CLI。

在包外，用绝对导入调用 jishi.cli:main，避免 PyInstaller 直接打包
「包内模块」时相对导入（`from .errors import ...`）找不到父包的坑。
并强制 stdin/stdout/stderr 用 UTF-8，规避 Windows 控制台 GBK 编码乱码。

三个流都要管：

- **stdout/stderr**：打包版不会继承 ``PYTHONUTF8`` 的 UTF-8 模式，打印中文
  会 ``UnicodeEncodeError``（M16.5 踩过）；
- **stdin**：``--stdin`` 与 MCP 通道的源码都从这里进来，漏改会让中文源码
  变乱码（实测 ``打印`` → ``鎵撳嵃``，2026-09-12 发 v0.1.4 前发现）；
- 三者都加 ``errors="replace"``：万一内容里混进非法码位（如孤立代理字符），
  报错渲染也不该再抛异常——那会让用户看到 PyInstaller 的英文堆栈，
  与「中文报错」的承诺相悖。
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
    # 输出**不做换行转换**（M33）：Windows 的文本模式会把 `\n` 写成 `\r\n`，
    # 于是同一份程序在 Python 侧输出 CRLF、在 Node / Rust 宿主输出 LF
    # ——「同一份程序到哪都一样」在重定向输出时就破了。（读入不受影响。）
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(newline="\n")
        except Exception:  # noqa: BLE001
            pass


_force_utf8()

from jishi.cli import main

if __name__ == "__main__":
    sys.exit(main())
