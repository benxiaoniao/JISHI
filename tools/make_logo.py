# -*- coding: utf-8 -*-
"""基石 logo 资产处理：源图 → PNG / ICO / ICNS（三平台图标，M35）。

产物（都放 `assets/`，进版本控制）：

| 文件 | 用途 |
|------|------|
| `基石_logo.png` | 512 宽（保持原长宽比），README / 网站 / GitHub 用 |
| `基石.ico` | Windows：安装程序、快捷方式、`jishi.exe` 图标 |
| `基石.icns` | macOS：`.app` / 安装包图标 |

为什么不做透明化（诚实评估 ROI，2026-09-05 定，M35 复核仍成立）：
- 第一版源图是 JPEG，透明背景被转成棋盘格烤进图片；
- 作者手工清理后的版本背景是带纹理的白纸（大量 248–252 亮度噪点），
  而 logo 本体最亮面是 (252,254,254)——两者颜色重叠，
  阈值/泛洪抠图必然要么留噪点、要么吞掉本体亮面；
- README / Gitee / GitHub 页面、Windows 安装向导与图标位都是浅色底，
  白底 logo 显示无缝；
- 若将来拿到真正的透明 PNG 原版（AI 生成工具可直接导出），换上即可，
  本脚本的裁剪 + 缩放逻辑对透明 PNG 同样适用。

用法：
    python tools/make_logo.py                 # 源图 → png + ico + icns 全套
    python tools/make_logo.py 源图 输出.png   # 只出 PNG（旧行为）

pillow 仅是本脚本（开发期图像处理）的依赖，基石运行时仍零第三方依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

#: Windows 图标要覆盖的尺寸。16/24/32 是列表与小图标视图，48 是中图标，
#: 256 是大图标与「超大图标」视图；64/128 补中间档。
#: 少了哪一档，资源管理器在那个视图下就得把 256 硬缩下来，边缘发虚。
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

#: macOS 的 ICNS 档位（Pillow 会按尺寸映射到 icns 类型码）。
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def crop_and_scale(src: Path, size: int = 512, square: bool = True) -> Image.Image:
    """源图 → 裁掉白边、留 4% 边距、缩到 `size` 见方的 RGB 图。

    `square=False` 时**不补成正方形**（保持原始长宽比，只把长边缩到 `size`）。
    README / 站点用的是这种（沿用既有 `基石_logo.png` 的形态）；
    图标（ico/icns）必须正方形——图标位是方的，宁可四周补白也不能拉变形。
    """
    img = Image.open(src).convert("RGB")
    w, h = img.size
    px = img.load()

    # 1. 找内容边界：与纯白差异明显的像素（阈值保守，宁可多留边）
    min_x, min_y, max_x, max_y = w, h, 0, 0
    step = 2                                    # 隔行扫描足够找边界
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y]
            if 255 - min(r, g, b) > 24:         # 明显不是背景
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)

    # 2. 裁剪 + 留 4% 白边
    if max_x > min_x and max_y > min_y:
        pad_x = int((max_x - min_x) * 0.04)
        pad_y = int((max_y - min_y) * 0.04)
        box = (max(0, min_x - pad_x), max(0, min_y - pad_y),
               min(w, max_x + pad_x), min(h, max_y + pad_y))
        img = img.crop(box)

    # 3. 缩到目标尺寸。补成正方形再做（图标用），否则直接缩长边（文档用）
    if square:
        side = max(img.size)
        padded = Image.new("RGB", (side, side), (255, 255, 255))
        padded.paste(img, ((side - img.size[0]) // 2, (side - img.size[1]) // 2))
        img = padded
    img.thumbnail((size, size), Image.LANCZOS)
    return img



def _report(tag: str, path: Path, extra: str = "") -> None:
    size = path.stat().st_size
    shown = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"
    print(f"  {tag:<5} {path.name:<18} {shown:>9}  {extra}")


def _boost_contrast(img: Image.Image, factor: float) -> Image.Image:
    """围绕中灰抬对比（纯 PIL 查表，不引 ImageEnhance）。"""
    lut = [max(0, min(255, int((i - 128) * factor + 128))) for i in range(256)]
    if img.mode == "RGB":
        return img.point(lut * 3)
    return img.point(lut)


def make_png(src: Path, dst: Path, size: int = 512,
             square: bool = False) -> Image.Image:
    """写出 PNG。默认**不补正方形**——README / 站点的用图沿用原长宽比。"""
    img = crop_and_scale(src, size, square=square)
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    _report("PNG", dst, f"{img.size[0]}×{img.size[1]}")
    return img


def make_ico(base: Image.Image, dst: Path) -> None:
    """多尺寸 ICO（16…256）。

    逐尺寸自己重采样再交给 Pillow 打包，而不是让 `save(sizes=…)` 从一个
    大图一次性缩：logo 里有「基石」二字的细横竖笔画，16×16 下直接缩会糊成
    一团；逐档 LANCZOS + 小尺寸略抬对比，是图标设计的常规做法。
    """
    frames = []
    for s in ICO_SIZES:
        f = base.resize((s, s), Image.LANCZOS)
        if s <= 32:
            f = _boost_contrast(f, 1.18)
        frames.append(f)
    dst.parent.mkdir(parents=True, exist_ok=True)
    big = frames[-1]
    big.save(dst, format="ICO",
             sizes=[(f.size[0], f.size[1]) for f in frames],
             append_images=frames[:-1])
    _report("ICO", dst, " ".join(str(s) for s in ICO_SIZES) + " 像素")


def make_icns(base: Image.Image, dst: Path) -> bool:
    """多尺寸 ICNS（macOS）。

    `.icns` 是一串「类型码 + PNG 载荷」的容器，Pillow 的 IcnsImagePlugin
    能写现代 PNG 型 icns（macOS 10.7+ 认）。若当前 Pillow 版本不支持保存，
    返回 False 让调用方提示——**不假装成功**。
    """
    big = base.resize((1024, 1024), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        big.save(dst, format="ICNS")
    except (KeyError, OSError, ValueError):
        return False
    if not dst.exists() or dst.stat().st_size == 0:
        return False
    _report("ICNS", dst, " ".join(str(s) for s in ICNS_SIZES) + " 像素")
    return True


def main() -> int:
    args = list(sys.argv[1:])
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    src = Path(args[0]) if args else ROOT / "基石_logo.jpeg"
    if not src.exists():
        raise SystemExit(f"找不到源图 {src}")
    if len(args) > 1:
        make_png(src, Path(args[1]))            # 旧行为：只出 PNG
        return 0

    assets = ROOT / "assets"
    print(f"源图：{src.name}")
    # README / 站点用图：保持原长宽比（不补正方形，避免无谓改动品牌资产）
    make_png(src, assets / "基石_logo.png", square=False)
    # 图标：正方形底图（图标位是方的）
    icon_base = crop_and_scale(src, 512, square=True)
    make_ico(icon_base, assets / "基石.ico")
    if not make_icns(icon_base, assets / "基石.icns"):
        print("  提示：当前 Pillow 不支持写 ICNS，macOS 包会退回用 PNG"
              "（不影响 Windows / Linux）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
