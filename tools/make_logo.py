# -*- coding: utf-8 -*-
"""基石 logo 资产处理：源图 → assets/基石_logo.png（白底、裁剪、512px）。

为什么不做透明化（诚实评估 ROI，2026-09-05）：
- 第一版源图是 JPEG，透明背景被转成棋盘格烤进图片；
- 作者手工清理后的版本背景是带纹理的白纸（大量 248–252 亮度噪点），
  而 logo 本体最亮面是 (252,254,254)——两者颜色重叠，
  阈值/泛洪抠图必然要么留噪点、要么吞掉本体亮面；
- README / Gitee / GitHub 页面本身就是白底，白底 logo 显示无缝；
- 若将来拿到真正的透明 PNG 原版（AI 生成工具可直接导出），换上即可，
  本脚本的裁剪 + 缩放逻辑对透明 PNG 同样适用。

用法：
    python tools/make_logo.py [源图] [输出]
默认：基石_logo.jpeg → assets/基石_logo.png（512x512，白底）

pillow 仅是本脚本（开发期图像处理）的依赖，基石运行时仍零第三方依赖。
"""

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def convert(src: Path, dst: Path, size: int = 512) -> None:
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

    # 3. 缩放
    img.thumbnail((size, size), Image.LANCZOS)

    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    print(f"源图 {w}x{h} → 输出 {img.size[0]}x{img.size[1]}（白底）→ {dst}")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "基石_logo.jpeg"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "assets" / "基石_logo.png"
    if not src.exists():
        raise SystemExit(f"找不到源图 {src}")
    convert(src, dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
