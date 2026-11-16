# -*- coding: utf-8 -*-
"""下载目录整理器 —— Python 参考实现（对照用，不进任何测试）。

做和 `整理.jsh` 一模一样的事，但用 Python 标准库：
`glob` 按模式找、`os.replace` 原子写、`tempfile` 临时目录、
`shutil.rmtree` 递归删、`webbrowser` 打开。

两相对照就知道「这件事本来该多短」—— 差出来的那点，就是基石还没长出来的部分。
"""

from __future__ import annotations

import glob
import os
import shutil
import tempfile
import webbrowser
import zipfile

源目录 = "样本"
归档目录 = "归档"


def 待整理(目录):
    return sorted(os.path.basename(p) for p in
                  glob.glob(os.path.join(目录, "报告*.md")))


def 移到归档(目录, 名字们):
    os.makedirs(归档目录, exist_ok=True)
    结果 = []
    for 名 in 名字们:
        新 = os.path.join(归档目录, "2026-" + 名)
        os.replace(os.path.join(目录, 名), 新)
        结果.append(新)
    return 结果


def 写索引(新路径们):
    行 = ["# 归档索引", "", f"共 {len(新路径们)} 份报告：", ""]
    for p in 新路径们:
        行.append(f"- {os.path.basename(p)}（{os.path.getsize(p)} 字节）")
    索引 = os.path.join(归档目录, "索引.md")
    # 原子写：先写同目录下的临时文件，再 os.replace 改名（跨平台原子）。
    目录 = os.path.dirname(索引) or "."
    fd, 临时 = tempfile.mkstemp(dir=目录, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(行) + "\n")
    os.replace(临时, 索引)
    return 索引


def 暂存并打包(新路径们):
    暂存 = tempfile.mkdtemp(prefix="整理-")
    工作 = os.path.join(暂存, "打包")
    os.makedirs(工作)
    for p in 新路径们:
        shutil.copyfile(p, os.path.join(工作, os.path.basename(p)))
    with open(os.path.join(工作, "清单.txt"), "w",
              encoding="utf-8", newline="") as f:
        f.write("\n".join(新路径们))
    with zipfile.ZipFile(os.path.join(归档目录, "报告.zip"), "w") as z:
        for 名 in sorted(os.listdir(工作)):
            z.write(os.path.join(工作, 名), os.path.join("打包", 名))
    shutil.rmtree(暂存)


def 数日志(目录):
    return len(glob.glob(os.path.join(目录, "*.log")))


def 主程序():
    print("=== 下载目录整理器 ===")
    名字们 = 待整理(源目录)
    print(f"找到 {len(名字们)} 份待整理的报告")

    新路径们 = 移到归档(源目录, 名字们)
    for p in 新路径们:
        print(f"  已归档 {os.path.basename(p)}")

    索引 = 写索引(新路径们)
    print(f"已写索引 {os.path.basename(索引)}（原子写：先写临时再改名）")

    暂存并打包(新路径们)
    print("已打包 报告.zip")

    print(f"有 {数日志(源目录)} 个日志想压成 .gz，"
          "但「压缩」没有 gzip 单文件（见绕道 R206）")

    if os.environ.get("整理_自动打开", "") != "1":
        print(f"索引就绪：{索引}（设 整理_自动打开=1 会自动打开）")
    else:
        ok = webbrowser.open(os.path.abspath(索引))
        print("已用默认程序打开索引" if ok
              else "`系统.打开` 没能唤起默认程序（多半没有图形界面）")


if __name__ == "__main__":
    主程序()
