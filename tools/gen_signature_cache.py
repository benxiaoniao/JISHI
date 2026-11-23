# -*- coding: utf-8 -*-
"""把标准库签名表**预生成**成 `oracle/jishi/data/stdlib_sigs.json`（M212 · ⑤）。

## 为什么要有它

`--ai-card` / `--lang-spec` 每次起一个新进程，而 `build_lang_spec()` 要把
**49 个 `.py` + 15 个 `.jsh`** 逐个 `ast.parse` 一遍 —— 本机实测 **0.32 秒**，
比「渲染卡片」本身还贵两倍。LSP 是长驻进程、进程内有缓存（M204），
CLI / 一次性取卡却每次都付。

预生成之后：取卡 = 算一遍内容哈希（~5 ms）+ 读 JSON。目标 ≤0.6 s（原来 0.69~1.22）。

📌 与 `tools/gen_stdlib_sigs.py` 的区别（名字很像，别混）：
那份生成的是**宿主要的参数名表**（`rust/src/stdlib_sigs.rs`，给命名参数调用用；
📌 0.3.7 M225 起 Node 那份 `oracle/node/stdlib_sigs.js` 随宿主退役一并删了）；
本脚本生成的是**Python 侧取卡用的签名缓存**（`.json`）。两者事实源相同、消费方不同。

## 什么时候要重跑

**改了 `oracle/jishi/stdlib/*.py` 或 `oracle/jishi/stdlib-jishi/*.jsh` 就要重跑**，
否则 `tests/test_m212_card_cache.py` 会红（它拿「现算」跟这份表对账）。

    python tools/gen_signature_cache.py          # 重算并写盘
    python tools/gen_signature_cache.py --检查   # 只校验，过期就退出码 1（CI 用）

⚠️ **只缓存签名**，不缓存整份 spec —— 整份缓存会把「本机已装的包」钉在第一次
取到的时刻（M203 的坑，`test_m90` 的漂移检测当场红）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import ai  # noqa: E402


def 生成() -> dict:
    return {
        "说明": "标准库签名表（tools/gen_signature_cache.py 生成，别手改）",
        "格式": ai.签名表格式,
        "文件": ai._签名指纹(),
        "stdlib": ai._stdlib_api_现算(),
    }


def main() -> int:
    检查 = "--检查" in sys.argv
    新 = 生成()
    路径 = ai.签名表路径
    if 检查:
        旧 = None
        if 路径.is_file():
            try:
                旧 = json.loads(路径.read_text(encoding="utf-8"))
            except ValueError:
                旧 = None
        if 旧 != 新:
            print(f"❌ 签名表过期或缺失：{路径}", file=sys.stderr)
            print("   跑 `python tools/gen_signature_cache.py` 重生成"
                  "（改了 stdlib/*.py 或 stdlib-jishi/*.jsh 就要重跑）",
                  file=sys.stderr)
            return 1
        print("✅ 签名表是最新的")
        return 0

    路径.parent.mkdir(parents=True, exist_ok=True)
    # 缩进 1：既好 diff 又别把文件撑太大
    路径.write_text(json.dumps(新, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    函数数 = sum(len(m["functions"]) for m in 新["stdlib"])
    print(f"✅ 已写出 {路径}（{len(新['stdlib'])} 模块 / {函数数} 函数）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
