# -*- coding: utf-8 -*-
"""语言卡（ai-card / lang-spec）准确性审计。

## 为什么要有它

语言卡是**给模型看的唯一依据**（AGENTS 核心二）。它一旦与实现不符，
模型写出来的代码第一次运行就撞墙 —— 而且模型没有「猜」的余地，它只会照卡抄。

2026-11-03 真实项目（`文档检索工具`）撞出来的第一条：

    语言卡：`参数.解析(句柄, 参数们=空)`      ← 看着可省略
    实际：  省略就直接报「还要一个参数列表」    ← 卡在说谎

## 这项审计查什么

1. **假默认值**：卡上标了 `=默认`，实际省略就报「参数不够」。
   做法是**真的少传一个参数去调它**，只看报错里有没有 arity 类字样
   （「需要 N 个参数」「还要一个」……）—— 类型不对报的错不算。
2. **两层签名不一致**：`参数/容器/统计/迭代` 四个模块有**两份实现** ——
   宿主层 `stdlib/*.py`（卡就是从它生成的）与基石库层 `stdlib-jishi/*.jsh`
   （Python / C VM / 树遍历**真正执行的那一份**）。两份的参数名/顺序/默认值
   必须一致，否则「卡描述的是 A，跑的是 B」。
3. **卡自身坏了**：签名解析不出来、必填数大于参数个数。
4. **模块清单**：卡里的模块表与真实模块目录不一致（多一个少一个）。

## 怎么用

    python tools/audit_ai_card.py          # 报告 + 有内题时退出码 1
    python tools/audit_ai_card.py --json   # 机器可读（CI / 发版脚本用）

📌 **发版前必跑**（`docs/版本收口流程.md` 已列入）。加标准库函数、改签名、
改默认值之后都要跑 —— 它与 `test_m51`（三宿主同宽）是一对：
那条盯「宿主有没有这个函数」，这条盯「卡上写的对不对」。
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 报错里出现这些字样 = 「参数不够」，也就是「那个默认值是假的」。
ARITY_HINT = re.compile(
    r"需要\s*\d+\s*个参数|需要\s*\d+\s*到\s*\d+\s*个参数|还要一个|缺少|参数不够|至少要")

#: 基石库层（`stdlib-jishi/*.jsh`）—— 这些模块有**两份**实现，要对齐。
库层目录 = ROOT / "oracle" / "jishi" / "stdlib-jishi"


def _宿主模块():
    """逐个 import 宿主层模块（`oracle/jishi/stdlib/*.py`）。"""
    import importlib
    import pkgutil

    import oracle.jishi.stdlib as pkg

    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name.startswith("_"):
            continue
        try:
            yield m.name, importlib.import_module(f"oracle.jishi.stdlib.{m.name}")
        except Exception as e:                        # pragma: no cover
            yield m.name, e


def _查假默认() -> list[dict]:
    """卡上标了 `=默认`、实际省略就报「参数不够」的函数。

    ⚠️ **探测要在一个临时目录里做**：我们是**真的去调用**那些标准库函数，
    其中有些是带副作用的（`文件.写文本("占位")`、`表格.写表格`）——
    在仓库根跑一遍，就会凭空多出一个叫 `占位` 的文件（真踩过）。
    这正是本项目的老规矩：**别在 cwd 里读写真实文件**。
    """
    import os
    import tempfile

    出 = []
    原目录 = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="audit_card_") as 临时:
        os.chdir(临时)
        try:
            return _查假默认_在临时目录(出)
        finally:
            os.chdir(原目录)


def _查假默认_在临时目录(出: list) -> list[dict]:
    for 名, mod in _宿主模块():
        if isinstance(mod, Exception):
            continue
        for fname, fn in vars(mod).items():
            if fname.startswith("_") or not inspect.isfunction(fn):
                continue
            if getattr(fn, "__module__", "") != mod.__name__:
                continue
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                continue
            ps = list(sig.parameters.values())
            if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in ps):
                continue
            必填 = [p for p in ps if p.default is inspect.Parameter.empty]
            if not any(p.default is not inspect.Parameter.empty for p in ps):
                continue
            try:
                fn(*(["占位"] * len(必填)))
            except Exception as e:
                msg = str(e)
                if ARITY_HINT.search(msg):
                    出.append({
                        "模块": 名, "函数": fname,
                        "签名": f"{fname}{sig}",
                        "问题": "卡上标着可以省略，实际省略就报参数不够",
                        "报错": msg.splitlines()[0][:120],
                    })
    return 出


_JSH函数头 = re.compile(r"^函数\s+([^\s(（]+)\s*[(（]([^)）]*)[)）]\s*[:：]\s*$")


def _拆参数(串: str) -> list[tuple[str, bool]]:
    """`"甲, 乙=空, 丙 = 1"` → `[("甲", False), ("乙", True), ("丙", True)]`。"""
    出 = []
    for 段 in re.split(r"[,，]", 串):
        段 = 段.strip()
        if not 段:
            continue
        名 = re.split(r"[=＝]", 段, maxsplit=1)[0].strip()
        有默认 = bool(re.search(r"[=＝]", 段))
        出.append((名.lstrip("*"), 有默认))
    return 出


def _查两层签名() -> list[dict]:
    """基石库层与宿主层的参数名/默认值必须一致（否则「卡说 A、跑的是 B」）。"""
    出 = []
    for 文件 in sorted(库层目录.glob("*.jsh")):
        模块 = 文件.stem
        try:
            mod = __import__(f"oracle.jishi.stdlib.{模块}", fromlist=["x"])
        except Exception as e:                        # pragma: no cover
            out = {"模块": 模块, "问题": "宿主层没有同名模块，无法比对", "细节": str(e)}
            出.append(out)
            continue
        for 行 in 文件.read_text(encoding="utf-8").splitlines():
            m = _JSH函数头.match(行)
            if not m:
                continue
            原 = m.group(1)
            名 = 原[2:] if 原.startswith("__") else 原
            if 名.startswith("_"):
                continue
            jsh参数 = _拆参数(m.group(2))
            fn = getattr(mod, 名, None)
            if not inspect.isfunction(fn):
                # 库层多出来的函数：卡里不会有它 —— 要么补进宿主层，要么改名
                出.append({"模块": 模块, "函数": 名,
                           "问题": "基石库层有这个函数，宿主层没有（语言卡不会列出它）"})
                continue
            py = list(inspect.signature(fn).parameters.values())
            py名 = [p.name for p in py]
            jsh名 = [n for n, _ in jsh参数]
            if py名 != jsh名:
                出.append({"模块": 模块, "函数": 名,
                           "问题": "参数名/顺序不一致",
                           "宿主层": py名, "基石库层": jsh名})
                continue
            py默认 = [p.default is not inspect.Parameter.empty for p in py]
            jsh默认 = [有 for _, 有 in jsh参数]
            if py默认 != jsh默认:
                出.append({"模块": 模块, "函数": 名,
                           "问题": "「哪些参数有默认值」不一致",
                           "宿主层": py默认, "基石库层": jsh默认})
    return 出


def _查卡自身() -> list[dict]:
    """卡里的签名能不能解析、必填数是否超过参数个数。"""
    from oracle.jishi.ai import build_lang_spec

    出 = []
    d = build_lang_spec()
    for m in d["stdlib"]:
        for f in m["functions"]:
            sig = f.get("sig") or ""
            mm = re.match(r"^([^()（）]+)[(（](.*)[)）]$", sig)
            if not mm:
                出.append({"模块": m["module"], "签名": sig, "问题": "签名解析不出来"})
                continue
            参数 = _拆参数(mm.group(2))
            必填 = [n for n, 有默认 in 参数 if not 有默认]
            声明 = f.get("min_args")
            if isinstance(声明, int) and 声明 != len(必填):
                出.append({"模块": m["module"], "签名": sig,
                           "问题": f"必填数对不上：卡说 {声明}，签名里 {len(必填)}"})
    return 出


def _查模块清单() -> list[dict]:
    """卡里的模块 == 真实模块目录（多一个少一个都要报）。"""
    from oracle.jishi.ai import build_lang_spec
    import pkgutil

    import oracle.jishi.stdlib as pkg

    真 = {m.name for m in pkgutil.iter_modules(pkg.__path__) if not m.name.startswith("_")}
    卡 = {m["module"] for m in build_lang_spec()["stdlib"]}
    出 = []
    for 名 in sorted(真 - 卡):
        出.append({"模块": 名, "问题": "模块存在，但语言卡里没有它"})
    for 名 in sorted(卡 - 真):
        出.append({"模块": 名, "问题": "语言卡里有它，但没有这个模块"})
    return 出


def 审计() -> dict:
    报告 = {
        "假默认值": _查假默认(),
        "两层签名": _查两层签名(),
        "卡自身": _查卡自身(),
        "模块清单": _查模块清单(),
    }
    报告["问题总数"] = sum(len(v) for v in 报告.values() if isinstance(v, list))
    return 报告


def main(argv: list[str]) -> int:
    报告 = 审计()
    if "--json" in argv:
        print(json.dumps(报告, ensure_ascii=False, indent=2))
        return 1 if 报告["问题总数"] else 0

    print("== 语言卡审计 ==")
    for 节, 项 in 报告.items():
        if not isinstance(项, list):
            continue
        print(f"\n【{节}】{len(项)} 处")
        for x in 项:
            print("  - " + json.dumps(x, ensure_ascii=False))
    print(f"\n合计 {报告['问题总数']} 处问题。")
    if 报告["问题总数"]:
        print("📌 语言卡是**给模型看的唯一依据** —— 它写错了，模型照抄必撞墙。")
        print("   改法：要么改实现去满足卡，要么改签名让卡如实反映（别两边都不动）。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
