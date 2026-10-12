# -*- coding: utf-8 -*-
"""备份归档器 —— Python 参考实现（对照用，不进任何测试）。

做和 `备份.jsh` 一模一样的事，但用 Python 标准库：
`datetime` 算日期、`hmac` 签名、`hashlib.pbkdf2_hmac` 存口令、`secrets` 生盐、
`gzip` 压单文件、`zipfile` 打包。

两相对照就知道「这件事本来该多短」—— 差出来的那点，就是基石还没长出来的部分。
"""

from __future__ import annotations

import calendar
import datetime
import gzip
import hashlib
import hmac
import os
import re
import secrets
import zipfile

源目录 = "样本"
归档目录 = "归档"
保留天数 = 30


def 箱子路径(日):
    return os.path.join(归档目录, "备份-" + 日 + ".zip")


def 今天():
    return datetime.date.today().strftime("%Y-%m-%d")


def 解析(日期文本):
    return datetime.datetime.strptime(str(日期文本).strip(), "%Y-%m-%d").date()


def 加月(月数, 日期文本):
    d = 解析(日期文本)
    total = d.year * 12 + (d.month - 1) + int(月数)
    y, m = divmod(total, 12)
    m += 1
    return f"{y:04d}-{m:02d}-{min(d.day, calendar.monthrange(y, m)[1]):02d}"


def 加年(年数, 日期文本):
    return 加月(int(年数) * 12, 日期文本)


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ]|$)")


def 解析ISO(文本):
    m = _ISO.match(str(文本).strip())
    if not m:
        raise ValueError(f"「{文本}」不是有效的 ISO 日期")
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    datetime.date(y, mo, d)                      # 校验是不是真日期
    return f"{y:04d}-{mo:02d}-{d:02d}"


def 减天数(天数, 日期文本):
    return (解析(日期文本) - datetime.timedelta(days=int(天数))).strftime("%Y-%m-%d")


def 早于(a, b):
    return 解析(a) < 解析(b)


def 相差天数(a, b):
    return (解析(a) - 解析(b)).days


def 摘要(文本):
    return hashlib.sha256(str(文本).encode("utf-8")).hexdigest()


def HMAC(键, 内容):
    return hmac.new(str(键).encode("utf-8"), str(内容).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def 安全随机字节(长度):
    return secrets.token_hex(int(长度))


def 口令摘要(口令, 盐, 迭代=1000):
    return hashlib.pbkdf2_hmac("sha256", str(口令).encode("utf-8"),
                               str(盐).encode("utf-8"), int(迭代)).hex()


def 打包今天():
    os.makedirs(归档目录, exist_ok=True)
    箱子 = 箱子路径(今天())
    with zipfile.ZipFile(箱子, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(源目录):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, os.path.dirname(源目录)))
    return 箱子


def 递归列出(目录):
    out = []
    for root, _, files in os.walk(目录):
        for fn in files:
            full = os.path.join(root, fn)
            out.append(os.path.relpath(full, 目录))
    return sorted(out)


def 压日志():
    # 名字排序：与基石侧 `路径.递归列出` 的 sorted 一致
    return 源目录, [n for n in 递归列出(源目录) if n.endswith(".log")]


def 做日志():
    全部 = "".join(open(os.path.join(源目录, n), encoding="utf-8").read()
                   for n in 压日志()[1])
    目标 = os.path.join(归档目录, "日志.gz")
    with gzip.open(目标, "wb") as f:
        f.write(全部.encode("utf-8"))
    return 目标, len(全部)


def 解压日志(路径):
    with gzip.open(路径, "rb") as f:
        return f.read().decode("utf-8")


def 清单文本(箱子):
    行 = ["# 备份清单", "", "归档：" + os.path.basename(箱子), ""]
    for 名 in 递归列出(源目录):
        全 = os.path.join(源目录, 名)
        行.append(f"- {名}  {os.path.getsize(全)} 字节  sha256 "
                  + 摘要(open(全, encoding="utf-8").read()))
    return "\n".join(行) + "\n"


def 存口令(口令):
    盐 = 安全随机字节(16)
    return f"盐 {盐}\n口令摘要 {口令摘要(口令, 盐, 1000)}\n"


def 清理旧归档():
    最早 = 减天数(保留天数, 今天())
    删了 = 0
    for 名 in sorted(os.listdir(归档目录)):
        if not (名.startswith("备份-") and 名.endswith(".zip")):
            continue
        日 = 名[3:13]
        if 早于(日, 最早):
            os.remove(os.path.join(归档目录, 名))
            删了 += 1
    return 删了


def 日志跨度(文本):
    行 = 文本.splitlines()
    if not 行:
        return 0
    return 相差天数(今天(), 解析ISO(行[0]))


def 主程序(口令="示例口令"):
    print("=== 备份归档器 ===")
    箱子 = 打包今天()
    print(f"已打包 {os.path.basename(箱子)}（{os.path.getsize(箱子)} 字节）")

    日志, 原长 = 做日志()
    print(f"已压日志 日志.gz（原文 {原长} 字符）")

    清单 = 清单文本(箱子)
    open(os.path.join(归档目录, "清单.txt"), "w",
         encoding="utf-8", newline="").write(清单)
    print(f"清单 {len(清单)} 字符")
    print(f"签名 {HMAC(口令, 清单)}")

    open(os.path.join(归档目录, "口令.txt"), "w",
         encoding="utf-8", newline="").write(存口令(口令))
    print("已写 口令.txt（盐 + 口令摘要，不存口令本身）")

    print(f"清掉过期归档 {清理旧归档()} 份")

    计划 = [加月(1, 今天()), 加年(1, 今天())]
    print(f"下次备份 {计划[0]} · 年度复盘 {计划[1]}")

    回来 = 解压日志(日志)
    print(f"日志解压回来 {len(回来)} 字符 · 时间跨度 {日志跨度(回来)} 天")

    print("（想单独取出 说明.md，见绕道 R301）")


if __name__ == "__main__":
    import sys
    主程序(sys.argv[1] if len(sys.argv) > 1 else "示例口令")
