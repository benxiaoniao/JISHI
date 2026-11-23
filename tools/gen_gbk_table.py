# -*- coding: utf-8 -*-
"""生成 GBK 码表的三份产物（**M204 起唯一来源**）。

来源：`oracle/jishi/data/gbk_whatwg.txt`（WHATWG 的 gbk，M204 从 encoding_rs 导出、
再与「基石编码库」的独立参考实现逐条对拍过：解码 24069 / 编码 24085，0 差异）。

产物（三份都由这一个脚本生成，**别手改**）：

| 文件 | 谁用 |
|---|---|
| `rust/src/gbk_table.rs` | Rust 宿主（替换 `encoding_rs`） |
| `oracle/jishi/stdlib/_gbk_table.py` | Python 参考实现 |

📌 **M225（0.3.7）**：Node 宿主退役，`oracle/node/gbk_table.js` 那份产物**已删**。

用法（改了数据才要重跑；产物签入仓库）：
    python tools/gen_gbk_table.py
"""
from __future__ import annotations

import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "oracle" / "jishi" / "data" / "gbk_whatwg.txt"
OUT_RS = ROOT / "rust" / "src" / "gbk_table.rs"
OUT_PY = ROOT / "oracle" / "jishi" / "stdlib" / "_gbk_table.py"

每行 = 191  # 低字节 0x40..0xFE 共 191 个


def 读数据() -> tuple[str, dict[int, int], dict[int, str]]:
    串 = None
    单字节: dict[int, int] = {}
    专用: dict[int, str] = {}
    for ln in DATA.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("槽位串: "):
            串 = ln[len("槽位串: "):]
        elif ln.startswith("单字节: "):
            a, b = ln[len("单字节: "):].split("=")
            单字节[int(a, 16)] = int(b, 16)
        elif ln.startswith("编码专用: "):
            a, b = ln[len("编码专用: "):].split("=")
            专用[int(a, 16)] = b.lower()
    assert 串 is not None, "数据文件里没有「槽位串」"
    assert len(串) == 0x7F * 每行, f"槽位串长度不对：{len(串)}"
    return 串, 单字节, 专用


def 查表(串: str, 码: int) -> int:
    """码号 → 码位（0 = 没映射）。单字节走 ASCII/€，双字节走槽位。"""
    if 码 < 0x80:
        return 码
    if 码 == 0x80:
        return 0x20AC
    hi, lo = 码 >> 8, 码 & 0xFF
    if not (0x81 <= hi <= 0xFE and 0x40 <= lo <= 0xFE):
        return 0
    i = (hi - 0x81) * 每行 + (lo - 0x40)
    return ord(串[i])


def 建反查(串: str, 专用: dict[int, str]) -> list[tuple[int, int]]:
    """码位 → 码号（按码位升序，给二分查找用）。

    ⚠️ 同一个码位可能对应多个码号（WHATWG 的 index 里有重复行）——
    取**第一个**（码号小的），与 encoding_rs 的编码方向一致。
    """
    见: dict[int, int] = {}
    for i, c in enumerate(串):
        if c == "\u0000":
            continue
        码 = ((0x81 + i // 每行) << 8) | (0x40 + i % 每行)
        见.setdefault(ord(c), 码)
    for cp, bs in 专用.items():
        assert cp not in 见, f"编码专用里 {cp:04X} 与正常表冲突"
        见[cp] = int(bs, 16)
    见[0x20AC] = 0x80   # WHATWG 编码器把欧元固定编成 0x80（表里 A2E3 也解成 €，但编码只认 0x80）
    return sorted(见.items())


def 写_rs(串: str, 单字节: dict[int, int], 反查: list[tuple[int, int]]) -> None:
    # ⚠️ 别把两张表摊成 Rust 源码：槽位 24257 + 反查 23957 条，
    #    源码会变成 ~600 KB（编译慢、diff 没法看）。改成两个小二进制（共 ~145 KB）
    #    用 `include_bytes!` 吃进来 —— 零解析开销，也与「数据就是数据」的直觉一致。
    槽位表 = b"".join((ord(c)).to_bytes(2, "little") for c in 串)
    反查表 = b"".join(cp.to_bytes(2, "little") + 码.to_bytes(2, "little")
                       for cp, 码 in 反查)
    (OUT_RS.parent / "gbk_slots.bin").write_bytes(槽位表)
    (OUT_RS.parent / "gbk_rev.bin").write_bytes(反查表)
    文 = f'''// 由 `tools/gen_gbk_table.py` 生成，**不要手改**。
//
// GBK 码表（WHATWG Encoding Standard 的 gbk）。数据源：
// `oracle/jishi/data/gbk_whatwg.txt`（M204 从 encoding_rs 导出，并与
// 「基石编码库」的独立参考实现逐条对拍：解码 24069 / 编码 24085，0 差异）。
//
// 为什么自带这份表（M204）：`编码.GBK文本` / `文本GBK` 是唯一的第三方依赖
// （`encoding_rs`）用途，为两个函数背一个 crate 不划算；而且 wasm 也能少一层。

/// 槽位表：每项 2 字节（小端 u16），槽位 i → 码位（0 = 没映射）。
const 槽位原始: &[u8; {len(串) * 2}] = include_bytes!("gbk_slots.bin");

/// 反查表：每项 4 字节（码位 u16 + 码号 u16，小端），按码位**升序**。
const 反查原始: &[u8; {len(反查) * 4}] = include_bytes!("gbk_rev.bin");

/// 槽位数（= 0x7F * 191）。
pub const 槽位个数: usize = {len(串)};

/// 槽位 i → 码位（U+0000 = 没映射）。槽位码号是纯算术，见 `槽位码号`。
#[inline]
pub fn 槽位(i: usize) -> u16 {{
    debug_assert!(i < 槽位个数);
    u16::from_le_bytes([槽位原始[i * 2], 槽位原始[i * 2 + 1]])
}}

/// 码位 → 码号（查不到给 0）。
pub fn 查编码(cp: u16) -> u16 {{
    let 条数 = {len(反查)};
    let (mut 低, mut 高) = (0usize, 条数);
    while 低 < 高 {{
        let 中 = (低 + 高) / 2;
        let 表码位 = u16::from_le_bytes([反查原始[中 * 4], 反查原始[中 * 4 + 1]]);
        match 表码位.cmp(&cp) {{
            core::cmp::Ordering::Less => 低 = 中 + 1,
            core::cmp::Ordering::Greater => 高 = 中,
            core::cmp::Ordering::Equal => {{
                return u16::from_le_bytes([反查原始[中 * 4 + 2], 反查原始[中 * 4 + 3]]);
            }}
        }}
    }}
    0
}}

/// 槽位下标 → 码号。
#[inline]
pub fn 槽位码号(i: usize) -> u16 {{
    (((0x81 + i / {每行}) as u16) << 8) | ((0x40 + i % {每行}) as u16)
}}
'''
    io.open(OUT_RS, "w", encoding="utf-8", newline="\n").write(文)


def 写_py(串: str, 单字节: dict[int, int], 反查: list[tuple[int, int]]) -> None:
    字面 = json.dumps(串, ensure_ascii=False)
    文 = f'''# 由 `tools/gen_gbk_table.py` 生成，**不要手改**。
#
# GBK 码表（WHATWG）。数据源 `oracle/jishi/data/gbk_whatwg.txt`
# （M204 从 encoding_rs 导出 + 与「基石编码库」参考实现逐条对拍 0 差异）。
#
# ⚠️ 换掉 Python 自带 `gbk` codec 的理由：内置 codec 是**传统 GBK**，
# 在 GB18030 的少数边缘码位（U+E5E5 / PUA）上与 WHATWG 不一致，
# 而 Rust 侧以前用 encoding_rs（WHATWG）⇒ 两边本来就在这些码位上不同。
# M204 起三端统一走这张表。

每行 = {每行}

槽位串 = {字面}

#: 对外只给这两个（`test_m121` 要求每个标准库模块声明 `__all__` 且与成员一致）
__all__ = ["码号到码位", "反查表", "槽位串", "每行", "编码专用"]

#: 18 个「编码专用」PUA 码位 → 码号（解码方向收不到它们，编码要认）
编码专用: dict[int, int] = {{
{chr(10).join(f"    {cp:#06x}: {码:#06x}," for cp, 码 in 反查 if cp >= 0xE000)}
}}

#: 码位 → 码号。**首次用时才建**（24000 条，摊源码要 540 KB，建一次 ~5 ms）
_反查: dict[int, int] | None = None


def 反查表() -> dict[int, int]:
    global _反查
    if _反查 is None:
        表 = {{}}
        for i, c in enumerate(槽位串):
            if c == chr(0):
                continue
            表.setdefault(ord(c), ((0x81 + i // 每行) << 8) | (0x40 + i % 每行))
        表.update(编码专用)
        表[0x20AC] = 0x80   # 与 Rust 侧同一条：欧元固定 0x80
        _反查 = 表
    return _反查


def 码号到码位(码: int) -> int:
    """码号 → 码位（0 = 没映射）。"""
    if 码 < 0x80:
        return 码
    if 码 == 0x80:
        return 0x20AC
    高, 低 = 码 >> 8, 码 & 0xFF
    if not (0x81 <= 高 <= 0xFE and 0x40 <= 低 <= 0xFE):
        return 0
    return ord(槽位串[(高 - 0x81) * 每行 + (低 - 0x40)])
'''
    io.open(OUT_PY, "w", encoding="utf-8", newline="\n").write(文)


def main() -> int:
    串, 单字节, 专用 = 读数据()
    反查 = 建反查(串, 专用)
    写_rs(串, 单字节, 反查)
    写_py(串, 单字节, 反查)
    print(f"槽位 {len(串)}（有效 {sum(1 for c in 串 if c != chr(0))}）· 反查 {len(反查)} 条")
    for p in (OUT_RS, OUT_PY):
        print(f"  写出 {p.relative_to(ROOT)}（{p.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
