# -*- coding: utf-8 -*-
"""生成 `docs/包列表.md` —— 站点「包仓库」那一页的源（M146 收尾）。

## 为什么要生成，而不是手写

站点是**从 Markdown 生成**的（`基石官网/build_site.py` 把 `docs/*.md` 渲染成页面），
所以「有哪些包」要么手写一份（改一个包就要记得改两处，迟早对不上），
要么**从数据生成**。这里选后者：数据源就是 `examples/packages/`（每个包的 `包.json`）。

🔴 **产物必须可复现**：按包名排序、**不写时间戳/机器信息**。
   判据在 `tests/test_m146_selfhost.py::test_包列表页面与数据一致` ——
   谁改了包却没重跑这个脚本，那条会红（网站就会继续说旧话）。

用法：
    python tools/gen_package_list.py           # 写 docs/包列表.md
    python tools/gen_package_list.py --check   # 只比，不一致返回 1（判据用）
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import packages as PKG          # noqa: E402

源目录 = ROOT / "examples" / "packages"
输出 = ROOT / "docs" / "包列表.md"

#: 站点上这份页面要指回主文档，所以链接一律写**主仓库里的相对路径**
#: （`build_site.py` 的 inline() 会把 `.md` 换成 `.html`）。
INDEX_URL = "https://xn--3jsy75e.cn/packages/索引.json"


def _扫包() -> list[dict]:
    """读所有包的 `包.json`，按包名排序返回。

    ⚠️ 排序放在这里（不是靠 `os.listdir` 的顺序）：产物要**稳定可 diff**。
    """
    出 = []
    for d in PKG.scan_packages(str(源目录)):
        meta = PKG.read_package_meta(d)
        出.append({
            "名字": str(meta.get("名字") or "").strip(),
            "版本": str(meta.get("版本") or "").strip(),
            "描述": str(meta.get("描述") or "").strip(),
            "依赖": meta.get("依赖") or {},
            "入口": str(meta.get("入口") or "").strip(),
            "关键字": meta.get("关键字") or [],
            "许可": str(meta.get("许可") or "").strip(),
        })
    出.sort(key=lambda p: p["名字"])
    return 出


def _依赖文本(依赖: dict) -> str:
    if not 依赖:
        return "—"
    return "、".join(f"{k} {v}" for k, v in sorted(依赖.items()))


def 生成() -> str:
    包们 = _扫包()
    行 = [
        "# 包仓库",
        "",
        "这里是能从**自建源**装到的第三方包 —— 全部用基石自己写（纯 `.jsh`），",
        "装到任何一台机器上就能跑，不依赖外部程序。",
        "",
        # ⚠️ 别写成 `<https://…>`：站点的 Markdown 渲染器**不认自动链接**，
        #    会把尖括号原样转义显示成 `&lt;https://…&gt;`。用普通链接就行。
        f"索引地址：[{INDEX_URL}]({INDEX_URL})"
        "（**GitHub 兜底**另有一份，两个源按优先级自动回退）。",
        "",
        "## 一览",
        "",
        "| 包 | 版本 | 依赖 | 说明 |",
        "|---|---|---|---|",
    ]
    for p in 包们:
        行.append(f"| **{p['名字']}** | {p['版本']} | {_依赖文本(p['依赖'])} | {p['描述']} |")
    if not 包们:
        行.append("| — | — | — | 还没有包 |")

    行 += [
        "",
        "## 装一个试试",
        "",
        "```bash",
        "jishi 安装 图片",
        "```",
        "",
        "装完**直接就能用**：`导入 X 从 本地包` 会在**编译期**展开成一次函数调用，",
        "所以 Python / C VM / Node / Rust **五个引擎跑的是同一份字节码**，",
        "不会出现「这个引擎能装不能跑」那种事。",
        "",
        "```jishi",
        "导入 图片 从 本地包",
        "令 图 = 图片.空图(8, 8, [255, 0, 0])",
        "图片.写(图, \"红.png\")",
        "打印(图片.尺寸(图))          # {'宽': 8, '高': 8}",
        "```",
        "",
        "依赖会自动带上（`安装 数学工具` 会先装它依赖的 `基础库`）；",
        "已经装过且版本一致的会**跳过**，不会被当成错误。",
    ]

    # 只给「有入口」的包单独写一段：说明它对外只有一个模块（规范写法）
    有入口 = [p for p in 包们 if p["入口"]]
    if 有入口:
        行 += ["", "## 各包要点", ""]
        for p in 有入口:
            依赖 = (f"，依赖 {_依赖文本(p['依赖'])}" if p["依赖"] else "")
            关键字 = ("（" + "、".join(str(k) for k in p["关键字"]) + "）"
                      if p["关键字"] else "")
            行.append(f"- **{p['名字']}** {p['版本']}{依赖}：入口 `{p['入口']}`{关键字}。")

    行 += [
        "",
        "## 自己发一个",
        "",
        "三条命令（细节见 [教程 10 包管理](docs/tutorial/10_包管理.md)）：",
        "",
        "```bash",
        "# ① 打包 + 生成索引条目（地址前缀指向你托管的地方）",
        "jishi 发布 我的包 --out dist/packages \\",
        "  --base-url https://example.com/packages/下载 --index dist/packages/索引.json",
        "",
        "# ② 或者批量重建整个索引",
        "jishi 索引 packages/ --base-url https://example.com/packages/下载 \\",
        "  --zip-dir dist/packages/下载 --out-index dist/packages/索引.json",
        "",
        "# ③ 别人这样装",
        "jishi 安装 我的包 --index https://example.com/packages/索引.json",
        "```",
        "",
        "> 📌 本页由 `tools/gen_package_list.py` 从 `examples/packages/` **生成**，别手改。",
        "> 改了包却没重跑它，`tests/test_m146_selfhost.py` 会报红。",
        "",
    ]
    return "\n".join(行)


def main() -> int:
    文本 = 生成()
    现在的 = 输出.read_text(encoding="utf-8") if 输出.is_file() else ""
    if "--check" in sys.argv:
        if 现在的 != 文本:
            print(f"❌ {输出.relative_to(ROOT)} 与数据不一致 —— 重跑："
                  f"python tools/gen_package_list.py", file=sys.stderr)
            return 1
        print(f"✓ {输出.relative_to(ROOT)} 与数据一致")
        return 0
    io.open(输出, "w", encoding="utf-8", newline="\n").write(文本)
    print(f"已写出 {输出.relative_to(ROOT)}（{文本.count(chr(10)) + 1} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
