"""生成 PNG 判据的样例（M205）。

判据只许依赖**被 git 跟踪的文件** ⇒ 这里一次性生成 PNG 与「标准 RGBA」，
两者都进仓库；`tests/test_m205_png.py` 只读它们，不依赖 PIL。

怎么造「标准答案」：**不靠 PIL 的转换**（它的 16 位/调色板口径我们要单独确认），
而是「我们自己知道每个像素该是什么」——
* 8 位那几张：源数据就是答案（灰度复制三份、RGB 补 255、调色板查表）；
* 16 位：取高字节（与 `png` crate 的 `STRIP_16` 同口径，也是 M205 解码器的口径）；
* 1/2/4 位：按最大值等比放大；
* 隔行：手写 Adam7 编码器（PIL 写不了隔行 PNG），像素值仍是自己给的。

用法：`python tools/gen_png_fixtures.py`
"""

from __future__ import annotations

import json
import pathlib
import struct
import zlib

from PIL import Image

出目录 = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "png"
清单 = []


def _ihdr(png: pathlib.Path) -> tuple[int, int, int, int, int]:
    """读 PNG 真实的 IHDR —— **清单里记的是文件里的事实，不是我们的意图**。

    （PIL 会把 6 色调色板压成 4 位：说好「8 位调色板」结果写出 4 位。
    只有读回来才对得上，判据要钉的是「文件长什么样」。）
    """
    b = png.read_bytes()
    i = b.index(b"IHDR")
    return struct.unpack(">IIBBBBB", b[i + 4:i + 17])


def _记(名: str, png: pathlib.Path, 宽: int, 高: int, rgba: bytes, 说明: str, 色彩: str, 位深: str):
    (出目录 / f"{名}.rgba").write_bytes(rgba)
    f宽, f高, f位深, f色彩, _, _, f隔行 = _ihdr(png)
    assert (f宽, f高) == (宽, 高), (名, f宽, f高)
    清单.append({
        "名字": 名,
        "宽": 宽,
        "高": 高,
        "rgba_sha256": __import__("hashlib").sha256(rgba).hexdigest(),
        "说明": 说明,
        "色彩": 色彩,
        "位深": 位深,
        # 文件里的事实（判据用这三项核对「样例确实覆盖了这种形态」）
        "文件位深": f位深,
        "文件色彩": f色彩,
        "文件隔行": f隔行,
        "png": png.name,
    })
    print(f"  ✓ {名}: {宽}×{高} · {色彩}/{位深} · {说明}")


def _块(类: bytes, 体: bytes) -> bytes:
    return struct.pack(">I", len(体)) + 类 + 体 + struct.pack(">I", zlib.crc32(类 + 体) & 0xFFFFFFFF)


def _写png_不隔行(路径: pathlib.Path, 宽: int, 高: int, 位深: int, 色彩: int,
                  扫描行们: list[bytes], 调色板: list[tuple[int, int, int]] | None = None,
                  透明: bytes | None = None):
    体 = b"".join(b"\x00" + 行 for 行 in 扫描行们)  # 过滤器全 0
    数据 = b"\x89PNG\r\n\x1a\n"
    数据 += _块(b"IHDR", struct.pack(">IIBBBBB", 宽, 高, 位深, 色彩, 0, 0, 0))
    if 调色板 is not None:
        数据 += _块(b"PLTE", b"".join(bytes(c) for c in 调色板))
    if 透明 is not None:
        数据 += _块(b"tRNS", 透明)
    数据 += _块(b"IDAT", zlib.compress(体, 9))
    数据 += _块(b"IEND", b"")
    路径.write_bytes(数据)


def _写隔行(路径: pathlib.Path, 宽: int, 高: int, 像素: list[list[tuple[int, int, int, int]]]):
    """RGBA8 + Adam7（过滤器全 0）。PIL 的 PNG 插件写不了隔行，只能手写。"""
    趟们 = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
            (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    体 = bytearray()
    for x0, y0, dx, dy in 趟们:
        列 = list(range(x0, 宽, dx))
        行们 = list(range(y0, 高, dy))
        if not 列 or not 行们:
            continue
        for y in 行们:
            体.append(0)  # 过滤器
            for x in 列:
                体.extend(bytes(像素[y][x]))
    数据 = b"\x89PNG\r\n\x1a\n"
    数据 += _块(b"IHDR", struct.pack(">IIBBBBB", 宽, 高, 8, 6, 0, 0, 1))
    数据 += _块(b"IDAT", zlib.compress(bytes(体), 9))
    数据 += _块(b"IEND", b"")
    路径.write_bytes(数据)


def main():
    出目录.mkdir(parents=True, exist_ok=True)
    for 旧 in 出目录.glob("*"):
        旧.unlink()

    宽, 高 = 13, 9

    # ① RGB8 —— 源数据就是答案
    rgb = [[((x * 17) % 256, (y * 29) % 256, (x * y * 7) % 256) for x in range(宽)] for y in range(高)]
    图 = Image.new("RGB", (宽, 高))
    图.putdata([c for 行 in rgb for c in 行])
    图.save(出目录 / "rgb8.png")
    _记("rgb8", 出目录 / "rgb8.png", 宽, 高,
        b"".join(bytes([*c, 255]) for 行 in rgb for c in 行), "8 位 RGB 渐变", "RGB", "8")

    # ② RGBA8
    rgba = [[((x * 11) % 256, (y * 23) % 256, 128, (x * 20 + y * 9) % 256) for x in range(宽)] for y in range(高)]
    图 = Image.new("RGBA", (宽, 高))
    图.putdata([c for 行 in rgba for c in 行])
    图.save(出目录 / "rgba8.png")
    _记("rgba8", 出目录 / "rgba8.png", 宽, 高,
        b"".join(bytes(c) for 行 in rgba for c in 行), "8 位 RGBA（带半透明）", "RGBA", "8")

    # ③ 灰度 8
    灰 = [[(x * 13 + y * 31) % 256 for x in range(宽)] for y in range(高)]
    图 = Image.new("L", (宽, 高))
    图.putdata([v for 行 in 灰 for v in 行])
    图.save(出目录 / "gray8.png")
    _记("gray8", 出目录 / "gray8.png", 宽, 高,
        b"".join(bytes([v, v, v, 255]) for 行 in 灰 for v in 行), "8 位灰度", "灰度", "8")

    # ④ 灰度 + α
    灰a = [[((x * 7) % 256, (y * 37) % 256) for x in range(宽)] for y in range(高)]
    图 = Image.new("LA", (宽, 高))
    图.putdata([c for 行 in 灰a for c in 行])
    图.save(出目录 / "gray_alpha8.png")
    _记("gray_alpha8", 出目录 / "gray_alpha8.png", 宽, 高,
        b"".join(bytes([v, v, v, a]) for 行 in 灰a for v, a in 行), "8 位灰度+透明", "灰度+透明", "8")

    # ⑤ 调色板（带 tRNS 逐项透明）
    板 = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (200, 200, 0), (0, 200, 200), (90, 90, 90)]
    透明 = bytes([0, 255, 128])  # 前三项各有自己的 α，其余默认 255
    索引 = [[(x + y) % len(板) for x in range(宽)] for y in range(高)]
    平 = [i for 行 in 索引 for i in 行]
    图 = Image.new("P", (宽, 高))
    图.putpalette([c for 项 in 板 for c in 项])
    图.putdata(平)
    图.save(出目录 / "palette8.png", transparency=透明, bits=8)
    _记("palette8", 出目录 / "palette8.png", 宽, 高,
        b"".join(bytes([*板[i], 透明[i] if i < len(透明) else 255]) for i in 平),
        "8 位调色板 + tRNS 逐项透明", "调色板", "8")

    # ⑥ 1 位灰度（位打包）
    位 = [[(x + y) % 2 for x in range(宽)] for y in range(高)]
    图 = Image.new("1", (宽, 高))
    图.putdata([v for 行 in 位 for v in 行])
    图.save(出目录 / "gray1.png")
    _记("gray1", 出目录 / "gray1.png", 宽, 高,
        b"".join(bytes([255 if v else 0] * 3 + [255]) for 行 in 位 for v in 行),
        "1 位灰度（位打包）", "灰度", "1")

    # ⑦ 4 位调色板（位打包 + 调色板）
    图 = Image.new("P", (宽, 高))
    图.putpalette([c for 项 in 板 for c in 项])
    图.putdata(平)
    图.save(出目录 / "palette4.png", bits=4)
    _记("palette4", 出目录 / "palette4.png", 宽, 高,
        b"".join(bytes([*板[i], 255]) for i in 平), "4 位调色板（位打包）", "调色板", "4")

    # ⑧ 16 位灰度 —— 标准答案是「取高字节」
    值16 = [[(x * 4000 + y * 9000) % 65536 for x in range(宽)] for y in range(高)]
    图 = Image.new("I;16", (宽, 高))
    图.putdata([v for 行 in 值16 for v in 行])
    图.save(出目录 / "gray16.png")
    _记("gray16", 出目录 / "gray16.png", 宽, 高,
        b"".join(bytes([v >> 8] * 3 + [255]) for 行 in 值16 for v in 行),
        "16 位灰度（取高字节）", "灰度", "16")

    # ⑨ 隔行（Adam7 · RGBA8）—— 手写
    _写隔行(出目录 / "interlaced.png", 宽, 高, rgba)
    _记("interlaced", 出目录 / "interlaced.png", 宽, 高,
        b"".join(bytes(c) for 行 in rgba for c in 行), "隔行（Adam7）RGBA8", "RGBA", "8")

    # ⑩ 四种过滤器各来一张（PIL 会自动挑，但我们至少要覆盖 Sub/Up/Paeth）
    图 = Image.new("RGB", (宽, 高))
    图.putdata([c for 行 in rgb for c in 行])
    图.save(出目录 / "rgb8_filter.png", optimize=True)
    _记("rgb8_filter", 出目录 / "rgb8_filter.png", 宽, 高,
        b"".join(bytes([*c, 255]) for 行 in rgb for c in 行), "8 位 RGB（编码器自选过滤器）", "RGB", "8")

    # ---- 坏样例（只验报错，不含标准答案）----
    (出目录 / "bad_truncated.png").write_bytes((出目录 / "rgb8.png").read_bytes()[:60])
    坏 = bytearray((出目录 / "rgb8.png").read_bytes())
    坏[-20] ^= 0xFF  # 打在 IDAT 的 CRC 上
    (出目录 / "bad_crc.png").write_bytes(坏)
    _写png_不隔行(出目录 / "bad_color.png", 宽, 高, 4, 2, [b"\x00" * 宽])  # RGB + 4 位 = 非法
    _写png_不隔行(出目录 / "bad_nopalette.png", 宽, 高, 8, 3, [bytes(range(宽))])  # 调色板图但没 PLTE
    # 索引越界：调色板只有 1 项，像素里却出现索引 3（九行都得齐，否则先报长度不对）
    越界行 = bytes([0, 1, 2, 3] * (宽 // 4) + [0] * (宽 - 4 * (宽 // 4)))
    _写png_不隔行(出目录 / "bad_index.png", 宽, 高, 8, 3, [越界行] * 高, 调色板=[(1, 2, 3)])
    # 解压炸弹：IHDR 只声明 13×1，IDAT 却解出 100 倍 —— 必须被「按 IHDR 算出的上限」拦住
    _写png_不隔行(出目录 / "bad_bomb.png", 宽, 1, 8, 6, [b"\x00" * (宽 * 4)])
    图b = (出目录 / "bad_bomb.png").read_bytes()
    位置 = 图b.index(b"IDAT") - 4          # 块长度字段的起点
    旧长 = struct.unpack(">I", 图b[位置:位置 + 4])[0]
    炸弹 = zlib.compress(b"\x00" * (13 * 4 + 1) * 100, 9)
    (出目录 / "bad_bomb.png").write_bytes(
        图b[:位置] + _块(b"IDAT", 炸弹) + 图b[位置 + 4 + 4 + 旧长 + 4:])

    清单.sort(key=lambda x: x["名字"])
    (出目录 / "清单.json").write_text(
        json.dumps({"说明": "由 tools/gen_png_fixtures.py 生成；rx 判据只读它",
                    "样例": 清单}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"\n共 {len(清单)} 张有标准答案的样例 + 6 张坏样例 → {出目录}")


if __name__ == "__main__":
    main()
