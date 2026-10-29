# -*- coding: utf-8 -*-
"""生成 `oracle/node/gbk_table.js` —— Node 宿主用的 **GBK 编码表**。

为什么要它：Node 只有 `TextDecoder('gbk')`（**解码**），**没有**编码方向的实现
（`new TextEncoder('gbk')` 不报错，但永远按 UTF-8 编 —— 静默错，最坏的那种）。
而 Python / Rust 都有真 codec。

为什么是「码位顺序串」而不是对象：直接摊成 `{"字": 0x8140, …}` 有 **305 KB**；
按「槽位顺序」拼成一个字符串只有 **~72 KB** —— 槽位到码号是纯算术：

    槽位 i = (高字节 - 0x81) * 191 + (低字节 - 0x40)
    码号   = ((0x81 + floor(i / 191)) << 8) | (0x40 + i % 191)

没映射的槽位放 U+0000（GBK 里那些空档）。

用法（改了才要重跑；产物签入仓库）：
    python tools/gen_gbk_table.py
"""

from __future__ import annotations

import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "oracle" / "node" / "gbk_table.js"

高们 = range(0x81, 0xFF)      # 0x81..0xFE
低们 = range(0x40, 0xFF)      # 0x40..0xFE（191 个）
每行 = len(低们)

模板 = """// 由 `tools/gen_gbk_table.py` 生成，**不要手改**。
//
// GBK 的「码位 -> 字符」表：槽位 i 对应码号
//   ((0x81 + floor(i/191)) << 8) | (0x40 + i % 191)
// 没映射的槽位是 U+0000。
//
// 为什么 Node 需要它：`TextDecoder('gbk')` 只能**解**，编不了；
// `new TextEncoder('gbk')` 不报错但永远按 UTF-8 编（静默错）。
// Python / Rust 都有真 codec，所以只有这一份是生成的。
//
// 共 __映射数__ 个有效槽位 / __总数__ 个槽位。

const 串 = __串__;

/** 每档低字节 0x40..0xFE 共 191 个。 */
const 每行 = 191;

/** 槽位下标 -> GBK 双字节码号。 */
function 槽位码号(i) {
  return ((0x81 + Math.floor(i / 每行)) << 8) | (0x40 + (i % 每行));
}

module.exports = { 串, 每行, 槽位码号 };
"""


def 建表() -> str:
    字符们 = []
    for 高 in 高们:
        for 低 in 低们:
            try:
                c = bytes([高, 低]).decode("gbk")
            except UnicodeDecodeError:
                c = ""
            # 只收「恰好一个字符」的槽位：GBK 有几位会解成两个码位（组合符），
            # 那些进不了「字符 -> 码号」的单射表，跳过。
            字符们.append(c if len(c) == 1 else "\u0000")
    return "".join(字符们)


def main() -> int:
    串 = 建表()
    映射数 = sum(1 for c in 串 if c != "\u0000")
    # json.dumps 会正确转义引号 / 反斜杠 / 控制符，直接当 JS 字符串字面量用
    字面 = json.dumps(串, ensure_ascii=False)
    js = (模板.replace("__映射数__", str(映射数))
              .replace("__总数__", str(len(串)))
              .replace("__串__", 字面))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(js)
    print(f"写出 {OUT.relative_to(ROOT)}（{映射数} 个有效槽位，"
          f"{len(js.encode('utf-8')) // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
