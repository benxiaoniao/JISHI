# -*- coding: utf-8 -*-
"""基石安全审计（M160·0.3.0 起点）：**能跑的探针**，不是清单。

## 为什么要有这个

「安全」在这个项目里一直是**说法**多、**判据**少：`--sandbox` 文档写着
「不允许导入文件模块」，于是大家以为「碰不到文件系统」。
2026-11-01 一做探针，发现**内建 `打开()` 完全不受管**，白名单模块 `json` 里
还有 `读文件` —— 沙箱里照样读、照样写。

⇒ 结论：**安全承诺必须有攻击探针兜着**，否则它只是一句文案。
这个文件就是那批探针：每条都能复现、每条都能进 CI。

## 用法

    python tools/audit_security.py            # 跑全部，按「拦住了没」出表
    python tools/audit_security.py -v         # 连被拦时的那句错也打出来

退出码：**0 = 全部按预期拦住**；1 = 有探针穿透（这就是缺陷）。

## 探针怎么加

往 `探针们` 里加一条：`(名字, 程序源码, 期望)`，`期望` 是 `"拦"` 或 `"放"`。
"放" 用于「这条本来该允许」的对照（没有对照就分不清「拦得好」和「全拦了」）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "rust" / "target" / "release" / "jishi-rs.exe"

#: 受害文件：随便挑一个「本来不该被沙箱读到」的真实文件
受害者 = "D:/code/基石/README.md"
#: 同上，但内容是**合法 JSON** —— 用来测「白名单模块里的读文件」：
#: 拿 README 去测会被「不是有效的 JSON」挡住，看起来像拦住了，其实**已经读进来了**
受害者_json = "D:/code/基石/tests/conformance/MANIFEST.json"


def _探针(受害者路径: str, 工作目录: Path):
    """返回 [(名字, 源码, 期望)]。

    ⚠️ 基石**没有** `r"..."` 原始字符串前缀 ⇒ Windows 路径要手写成正斜杠。
    """
    写目标 = str(工作目录 / "沙箱写出来.txt").replace("\\", "/")
    越界目标 = str(工作目录 / "tmp").replace("\\", "/")
    return [
        # ---- 对照：这些**本来该被拦**（沙箱的承诺）----
        ("导入 文件", "导入 文件\n打印(1)\n", "拦"),
        ("导入 表格", "导入 表格\n打印(1)\n", "拦"),
        ("导入 网络", "导入 网络\n打印(1)\n", "拦"),
        ("导入 系统", "导入 系统\n打印(1)\n", "拦"),
        ("导入 路径", "导入 路径\n打印(1)\n", "拦"),
        ("导入 压缩", "导入 压缩\n打印(1)\n", "拦"),
        ("导入 配置", "导入 配置\n打印(1)\n", "拦"),

        # ---- 绕过面①②③：能力不在「导入」这一层 ----
        ("内建 打开() 读文件",
         f'令 f = 打开("{受害者路径}")\n打印(长度(f.读()))\n', "拦"),
        ("内建 打开() 写文件",
         f'令 f = 打开("{写目标}", "写")\nf.写("pwned")\nf.关闭()\n打印(1)\n', "拦"),
        ("json.读文件（白名单模块里的读）",
         '导入 json\n'
         f'令 数 = json.读文件("{受害者_json}")\n'
         '打印(长度(数) > 0)\n', "拦"),

        # ---- 对照：这些真的该放行（别为了安全把语言弄残）----
        ("纯计算（数学/文本）",
         "导入 数学\n导入 文本\n打印(数学.开方(16) + 长度(\"abc\"))\n", "放"),
    ]


def _跑(源: str, 工作目录: Path, 超时: float = 30.0):
    p = 工作目录 / "探针.jsh"
    p.write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run(
        [str(EXE), str(p), "--sandbox", "--json-result"],
        cwd=str(工作目录), capture_output=True, timeout=超时, input=b"")
    文本 = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    # ⚠️ `--json-result` 的 JSON **在最后一行**，前面可能夹着模块遮蔽提示之类的杂音
    #    （曾把「稳过的纯计算」误判成「被拦」）。从后往前找第一个能解析的。
    for 行 in reversed(文本.splitlines()):
        行 = 行.strip()
        if not 行.startswith("{"):
            continue
        try:
            d = json.loads(行)
        except Exception:                                    # noqa: BLE001
            continue
        msg = ((d.get("error") or {}) or {}).get("message", "") or ""
        return bool(d.get("ok")), msg, (d.get("stdout") or "").strip()
    return False, 文本[:200], ""


def main() -> int:
    ap = argparse.ArgumentParser(description="基石安全审计探针")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if not EXE.is_file():
        print(f"❌ 先构建宿主：cd rust && cargo build --release（缺 {EXE}）",
              file=sys.stderr)
        return 2

    目录 = Path(tempfile.mkdtemp(prefix="jishi_audit_"))
    (目录 / "tmp").mkdir(exist_ok=True)
    写目标 = 目录 / "沙箱写出来.txt"

    行们, 穿透 = [], []
    for 名, 源, 期望 in _探针(受害者, 目录):
        放行了, 消息, 输出 = _跑(源, 目录)
        实际 = "放" if 放行了 else "拦"
        对 = 实际 == 期望
        if not 对:
            穿透.append((名, 期望, 实际, 消息, 输出))
        行们.append((名, 期望, 实际, 对, 消息, 输出))

    # 文件到底有没有被写出来（比「程序说成功了」更硬的证据）
    写出来了 = 写目标.exists()
    if 写出来了:
        写目标.unlink()

    宽 = max(len(r[0]) for r in 行们)
    print(f"{'探针':<{宽}}  期望  实际  结果")
    print("-" * (宽 + 18))
    for 名, 期望, 实际, 对, 消息, 输出 in 行们:
        标记 = "✅" if 对 else "❌ **穿透**"
        print(f"{名:<{宽}}  {期望:^4}  {实际:^4}  {标记}")
        if a.verbose or not 对:
            尾 = (消息 or 输出 or "（无输出）").replace("\n", " ")[:100]
            print(f"{'':<{宽}}       ↳ {尾}")

    print()
    if 写出来了:
        print("🔴 **沙箱里真的写出了文件**：d 盘临时目录下的「沙箱写出来.txt」")
    if 穿透:
        print(f"🔴 有 {len(穿透)} 条穿透预期：")
        for 名, 期望, 实际, 消息, 输出 in 穿透:
            print(f"   · {名}：期望「{期望}」，实际「{实际}」")
        return 1
    print("✅ 所有探针都按预期（该拦的拦住、该放的放行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
