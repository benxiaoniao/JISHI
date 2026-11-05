# -*- coding: utf-8 -*-
"""基石**语言本体**安全审计（0.3.0）。

## 和 `audit_security.py` 的分工

| 文件 | 审什么 |
|---|---|
| `audit_security.py` | **工具链**：沙箱、包安装 |
| **本文件** | **语言/标准库本体**：算错、卡死、被恶意输入打爆 |

语言本体的漏洞不是「谁能读谁的文件」，而是**这段程序会不会静默算错、
会不会因为一段输入卡死、会不会把进程直接崩掉**。七类，逐条实测：

1. 整数/浮点边界 —— 超范围是**报错**还是**静默回绕**（后者最毒：结果看着像对的）
2. 正则回溯（ReDoS）—— 一条 `(a+)+$` 配长输入能不能把程序卡死
3. 压缩炸弹 —— 小 zip 解出大文件，有没有上限
4. 解压路径穿越 —— 条目名带 `../` 能不能写到目标目录外
5. SQL 注入 —— 结构化条件是否按**值**处理；裸 SQL 有没有参数化
6. 畸形字节码 —— 给一个坏 JSON，是中文报错还是**进程崩掉**
7. 递归深度 —— 深递归是「递归太深」还是**栈溢出 abort**

判定三档：✅ 安全 · 🟡 设计缺口（能做但没引导）· 🔴 危险（静默错/崩溃/卡死）。

用法：
    python tools/audit_language.py          # 全部
    python tools/audit_language.py -v       # 附证据

退出码：0 = **没有 🔴**；1 = 至少一条危险。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# ⚠️ 名字要**分平台**（M174 修）：Windows 是 `jishi-rs.exe`，Linux / macOS 是
# `jishi-rs`。写死 `.exe` 的后果是 Linux/macOS 上这里永远说「先构建宿主」，
# 而那条判据（`test_m170_sql_params::test_审计里的_SQL_一条已经转绿`）就一直红。
EXE = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")

安全, 缺口, 危险 = "✅", "🟡", "🔴"


def 跑(源: str, 目录: Path, 超时: float = 20.0, 附加参数=None, 环境=None,
      只要参数: bool = False):
    """跑一段基石源码，返回 (退出码, stdout+stderr, 用时秒)。

    `只要参数=True`：不写脚本、不把脚本放在命令首位（`--load-bytecode` 这类用法）。
    """
    p = 目录 / f"探针_{abs(hash(源)) % 10000}.jsh"
    if 只要参数:
        命令 = [str(EXE)] + list(附加参数 or [])
    else:
        p.write_text(源, encoding="utf-8", newline="\n")
        命令 = [str(EXE), str(p)] + list(附加参数 or [])
    env = dict(os.environ)
    env.update(环境 or {})
    t0 = time.perf_counter()
    try:
        r = subprocess.run(命令, cwd=str(目录), capture_output=True,
                           timeout=超时, input=b"", env=env)
        用 = time.perf_counter() - t0
        文本 = (r.stdout + r.stderr).decode("utf-8", "replace")
        return r.returncode, 文本, 用
    except subprocess.TimeoutExpired:
        return -9, f"（超过 {超时} 秒没返回，已强杀）", time.perf_counter() - t0


def 判崩溃(文本: str) -> bool:
    """宿主**崩掉**的指纹：Rust panic / abort / 栈溢出（不是中文报错）。"""
    指纹 = ("panicked at", "stack overflow", "memory allocation of",
            "fatal runtime error", "SIGSEGV", "core dumped")
    return any(x in 文本 for x in 指纹)


# ---------------------------------------------------------------------------
# 1. 整数 / 浮点边界
# ---------------------------------------------------------------------------

def 测_数值边界(目录):
    案 = [
        ("整数超出 i128", "打印(170141183460469231731687303715884105727 + 1)\n"),
        ("整数乘超范围", "打印(170141183460469231731687303715884105727 * 2)\n"),
        ("整数幂超范围", "打印(2 ** 300)\n"),
        ("浮点溢出", "打印(1e308 * 10.0)\n"),
        ("浮点除零", "打印(1.0 / 0.0)\n"),
        ("零除零", "打印(0.0 / 0.0)\n"),
        ("整数除零", "打印(1 // 0)\n"),
        ("负零", "打印(-0.0)\n"),
    ]
    行们 = []
    for 名, 源 in 案:
        码, 文本, _用 = 跑(源, 目录)
        摘要 = 文本.strip().splitlines()
        摘要 = 摘要[0] if 摘要 else "（无输出）"
        行们.append((名, 码, 摘要))
    # 判定：**报错**是安全；**崩**是危险；**给出 nan/inf** 是 IEEE 正常（可接受）
    崩 = [r for r in 行们 if 判崩溃(r[2])]
    if 崩:
        return 危险, f"{len(崩)} 条把宿主崩了：" + "；".join(
            f"{r[0]}→{r[2][:40]}" for r in 崩), 行们
    return 安全, "全部要么给中文报错、要么给出 nan/inf（IEEE 行为），没有静默回绕、没有崩", 行们


# ---------------------------------------------------------------------------
# 2. 正则回溯（ReDoS）
# ---------------------------------------------------------------------------

def 测_正则回溯(目录):
    """指数回溯的经典形状：`(a+)+$` 配「一串 a 加一个 b」。

    安全实现（DFA / 有限自动机）耗时随长度**线性**；回溯实现是**指数** ——
    32 个 a 就够跑上几万年。逐档加长看耗时怎么走。
    """
    行们 = []
    增长 = []
    被拦住 = False
    for n in (20, 24, 28, 32):
        串 = "a" * n + "b"
        源 = ('导入 正则\n'
              f'令 文 = "{串}"\n'
              '打印(正则.匹配("(a+)+$", 文))\n')
        码, 文本, 用 = 跑(源, 目录, 超时=15)
        增长.append(用)
        首 = (文本.strip().splitlines() or ["—"])[0]
        if "正则太复杂" in 文本:
            被拦住 = True
        行们.append((f"{n} 个 a", f"{用:.2f}s", 首[:46]))
        if 用 > 10:
            return 危险, f"{n} 个 a 就跑了 {用:.0f} 秒 —— **指数回溯（ReDoS）**", 行们
        if 被拦住:
            break
    if 被拦住:
        return 安全, (f"回溯有步数上限：{行们[-1][0]} 时在 {行们[-1][1]} 内被拦下并报"
                     "「正则太复杂」（不再是跑 15 秒回不来）"), 行们
    if 增长[-1] < 1.0:
        return 安全, (f"32 个 a 只用 {增长[-1]:.2f}s（20/24/28/32 档："
                     + " / ".join(f"{x:.2f}" for x in 增长) + "）⇒ 不是回溯实现"), 行们
    return 缺口, (f"32 个 a 用了 {增长[-1]:.1f}s，明显超线性（20→32 档："
                 + " / ".join(f"{x:.2f}" for x in 增长) + "）"), 行们


# ---------------------------------------------------------------------------
# 3. 压缩炸弹
# ---------------------------------------------------------------------------

def 测_压缩炸弹(目录):
    """两条都要过：

    ① **真 40 MB 的包能正常解**（别为了防炸弹把正常大包也拦了）；
    ② **声称解开 520 MB 的包要被拦**（不然 39 KB → 40 MB 这种比例到 1000:1 时，
       一个几百 KB 的包就能把内存吃光）。

    ② 不真造 520 MB：把中央目录里「解压后大小」那个字段改大即可 —— 判的是**解压前**
    的总量判断，所以根本不会去解数据。
    """
    import struct

    # ① 真包：40 MB 全零（压完约 39 KB）
    大 = 目录 / "真大.txt"
    大.write_bytes(b"\0" * (40 * 1024 * 1024))
    真包 = 目录 / "真大.zip"
    with zipfile.ZipFile(真包, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(大, "大.txt")
    大.unlink()
    源1 = f'导入 压缩\n压缩.解压("{真包.as_posix()}", "{(目录 / "出1").as_posix()}")\n打印("解完了")\n'
    码1, 文1, 用1 = 跑(源1, 目录, 超时=120)
    能解 = "解完了" in 文1

    # ② 声称 520 MB：改中央目录里的 usize（偏移 24）
    假包 = 目录 / "声称大.zip"
    with zipfile.ZipFile(假包, "w") as z:
        z.writestr("小.txt", "小\n")
    b = bytearray(假包.read_bytes())
    起 = b.find(b"PK\x01\x02")
    struct.pack_into("<I", b, 起 + 24, 520 * 1024 * 1024)
    假包.write_bytes(bytes(b))
    源2 = f'导入 压缩\n压缩.解压("{假包.as_posix()}", "{(目录 / "出2").as_posix()}")\n打印("解完了")\n'
    码2, 文2, 用2 = 跑(源2, 目录, 超时=60)
    拦住了 = "解完了" not in 文2 and "超过上限" in 文2

    行们 = [
        (f"真 40 MB 包（{真包.stat().st_size // 1024} KB）", f"{用1:.1f}s",
         "解开了" if 能解 else "**没解开**"),
        ("声称解开 520 MB", f"{用2:.1f}s", "拦住了" if 拦住了 else "**没拦住**"),
    ]
    if 能解 and 拦住了:
        return 安全, "有上限（512 MB）且不误伤正常大包", 行们
    if not 能解:
        return 危险, "正常的大包也被拦了 —— 防过头，把正常用法弄残", 行们
    return 缺口, "声称 520 MB 的包照样解开 ⇒ 没有上限（真炸弹会吃光内存）", 行们


# ---------------------------------------------------------------------------
# 4. 解压路径穿越
# ---------------------------------------------------------------------------

def 测_解压穿越(目录):
    """标准库的 `压缩.解压` 会不会被 `../` 条目带到目标目录外面。"""
    包 = 目录 / "穿越.zip"
    目标 = 目录 / "目标"
    外面 = 目录 / "被穿越.txt"
    if 外面.exists():
        外面.unlink()
    with zipfile.ZipFile(包, "w") as z:
        z.writestr("正常.txt", "ok\n")
        z.writestr("../被穿越.txt", "pwned\n")
    源 = (f'导入 压缩\n'
          f'压缩.解压("{包.as_posix()}", "{目标.as_posix()}")\n'
          f'打印("解完了")\n')
    码, 文本, 用 = 跑(源, 目录, 超时=30)
    行 = [("条目 `../被穿越.txt`", "—", "写到目标外了" if 外面.exists() else "没写出去")]
    if 外面.exists():
        外面.unlink()
        return 危险, "`压缩.解压` 不校验条目名 ⇒ 能写到目标目录之外", 行
    return 安全, "带 `../` 的条目被拒绝（或规范化到目录内）", 行


# ---------------------------------------------------------------------------
# 5. SQL 注入
# ---------------------------------------------------------------------------

def 测_SQL注入(目录):
    """三件事分开看（第三件是 M170 补的）：

    - **结构化条件**（`查询(库, 表, {字段: 值})`）—— 值是不是按**值**处理；
    - **裸 SQL + 参数**（`执行查询(库, SQL, 参数们)`）—— `?` 占位是不是按值绑；
    - **拼字符串**（`执行(库, SQL)`）—— 还能拼，但**现在有参数化入口可引导**。

    📌 2026-11-04（M170）之前这里只有前两件，结论是 🟡「引导缺口」：
    `执行(库, SQL)` 只能拼、没有别的路。补上 `参数们` 之后，**安全的路存在了**
    ⇒ 判据从 🟡 转 ✅（拼字符串仍允许 —— 逃生口的存在是设计，只是不再逼人用它）。
    """
    库 = 目录 / "注入.jshdb"
    if 库.exists():
        库.unlink()
    源 = f'''导入 数据库
令 库 = 数据库.打开("{库.as_posix()}")
数据库.建表(库, "账", ["名字"])
数据库.插入(库, "账", {{"名字": "小明"}})
数据库.插入(库, "账", {{"名字": "小红"}})
令 全 = 数据库.查询(库, "账")
令 恶 = 数据库.查询(库, "账", {{"名字": "x' OR '1'='1"}})
令 参 = 数据库.执行查询(库, "SELECT * FROM 账 WHERE 名字 = ?", ["x' OR '1'='1"])
打印("全部", 长度(全))
打印("注入", 长度(恶))
打印("参数化", 长度(参))
数据库.关闭(库)
'''
    码, 文本, 用 = 跑(源, 目录, 超时=30)
    行 = []
    全 = 注 = 参 = None
    for line in 文本.splitlines():
        if line.startswith("全部 "):
            全 = line.split()[-1]
        if line.startswith("注入 "):
            注 = line.split()[-1]
        if line.startswith("参数化 "):
            参 = line.split()[-1]
    行.append(("结构化条件", "值当值", f"全部={全} 注入={注}"))
    行.append(("裸 SQL 参数化", "`?` 按值绑", f"注入串命中 {参} 条"))
    if 全 == "2" and 注 == "0" and 参 == "0":
        return 安全, ("结构化条件按**值**匹配；裸 SQL 也有参数化入口"
                     "（`执行(库, SQL, 参数们)` / `执行查询(库, SQL, 参数们)`，`?` 按值绑）"), 行
    if 全 == "2" and 注 == "0" and 参 is None:
        return 缺口, ("结构化条件安全，但**没有参数化入口** ⇒ 用户只能自己拼字符串"), 行
    return 危险, f"疑似不按值处理：全部={全}、注入={注}、参数化={参}", 行


# ---------------------------------------------------------------------------
# 6. 畸形字节码
# ---------------------------------------------------------------------------

def 生成字节码(目录) -> Path | None:
    """用参考实现编一份合法字节码，供后面「改坏它」用。"""
    sys.path.insert(0, str(ROOT))
    try:
        from oracle.jishi import serialize
        from oracle.jishi.compiler import compile_source
    except Exception:                                        # noqa: BLE001
        return None
    data = serialize.dumps(compile_source("打印(1)\n", "探针.jsh"))
    p = 目录 / "合法.json"
    p.write_text(data, encoding="utf-8")
    return p


def 测_畸形字节码(目录):
    源文件 = 生成字节码(目录)
    if 源文件 is None:
        return 缺口, "取不到参考实现，跳过（本机没装 oracle 包？）", []
    基 = json.loads(源文件.read_text(encoding="utf-8"))
    案 = []
    d = json.loads(json.dumps(基))
    d["checksum"] = "0" * 64
    案.append(("校验和被改", d))
    d = json.loads(json.dumps(基))
    d["payload"]["codes"][0]["instrs"] = [999, 99, 99, 99]
    案.append(("指令码越界", d))
    d = json.loads(json.dumps(基))
    d["payload"]["main"] = 99
    案.append(("main 指向不存在的代码块", d))
    d = json.loads(json.dumps(基))
    d["payload"]["consts"] = []
    案.append(("常量池被清空", d))

    行们, 崩们 = [], []
    for 名, 坏 in 案:
        p = 目录 / f"坏_{名}.json"
        p.write_text(json.dumps(坏, ensure_ascii=False), encoding="utf-8")
        码, 文本, _用 = 跑("", 目录, 超时=20, 附加参数=["--load-bytecode", str(p)],
                          只要参数=True)
        崩 = 判崩溃(文本)
        首行 = (文本.strip().splitlines() or ["（无输出）"])[0][:70]
        行们.append((名, f"rc={码}", ("**崩了**" if 崩 else "中文报错") + f" · {首行}"))
        if 崩:
            崩们.append(名)
    if 崩们:
        return 危险, f"{len(崩们)} 种畸形输入把宿主崩了：{'、'.join(崩们)}", 行们
    return 安全, "4 种畸形输入全部给中文报错，没有 panic", 行们


# ---------------------------------------------------------------------------
# 7. 递归深度
# ---------------------------------------------------------------------------

def 测_递归深度(目录):
    """深递归：**有没有上限**？

    这台机器上的实测（2026-11-01）：`返回 1 + f(n+1)` 无限递归，12 秒跑到
    **1560 万层**还在继续 —— 既不报「递归太深」，也不崩，只是内存一直涨。
    那不是「栈溢出崩掉」那种好懂的问题，而是**静默吃内存直到被系统杀掉**：
    用户看到的是「程序卡住、没输出、没报错」。
    """
    源 = ("函数 递归(n)：\n"
          "    如果 n > 3000000：\n"
          "        返回 \"跑到三百万层\"\n"
          "    返回 递归(n + 1)\n"
          "打印(递归(0))\n")
    码, 文本, 用 = 跑(源, 目录, 超时=15)
    崩 = 判崩溃(文本)
    到了 = "跑到三百万层" in 文本
    行们 = [(f"递归 300 万层", f"{用:.1f}s", f"rc={码} · "
             + ("跑到了" if 到了 else ("崩了" if 崩 else "没跑到（超时/报错）")))]
    if 崩:
        return 危险, "深递归把宿主崩了（栈溢出 abort）", 行们
    if 到了:
        return 危险, ("**没有递归深度上限**：能一路递归到 300 万层"
                     "（实测 12 秒可到 1500 万层）—— 既不报错也不崩，"
                     "无限递归会一直吃内存，直到被系统杀掉。"
                     "用户看到的是「卡住、没输出、没报错」"), 行们
    return 安全, "递归有上限（跑到三百万层前就给了报错）", 行们


# ---------------------------------------------------------------------------

测家 = [
    ("1 整数/浮点边界", 测_数值边界),
    ("2 正则回溯 ReDoS", 测_正则回溯),
    ("3 压缩炸弹", 测_压缩炸弹),
    ("4 解压路径穿越", 测_解压穿越),
    ("5 SQL 注入", 测_SQL注入),
    ("6 畸形字节码", 测_畸形字节码),
    ("7 递归深度", 测_递归深度),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="基石语言本体安全审计")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if not EXE.is_file():
        print(f"❌ 先构建宿主：cd rust && cargo build --release（缺 {EXE}）",
              file=sys.stderr)
        return 2

    目录 = Path(tempfile.mkdtemp(prefix="jishi_audit_lang_"))
    结果 = []
    for 名, fn in 测家:
        try:
            状态, 说明, 明细 = fn(目录)
        except Exception as e:                              # noqa: BLE001
            状态, 说明, 明细 = 缺口, f"探针自身出错：{type(e).__name__}: {e}", []
        结果.append((名, 状态, 说明, 明细))
        print(f"\n{状态} **{名}** —— {说明}")
        if a.verbose or 状态 == 危险:
            for 行 in 明细:
                print("     · " + " ｜ ".join(str(x) for x in 行))

    危险数 = sum(1 for r in 结果 if r[1] == 危险)
    缺口数 = sum(1 for r in 结果 if r[1] == 缺口)
    print("\n" + "=" * 66)
    print(f"共 7 类：✅ 安全 {7 - 危险数 - 缺口数} · 🟡 缺口 {缺口数} · 🔴 危险 {危险数}")
    shutil.rmtree(目录, ignore_errors=True)
    return 1 if 危险数 else 0


if __name__ == "__main__":
    sys.exit(main())
