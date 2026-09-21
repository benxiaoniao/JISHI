# -*- coding: utf-8 -*-
"""从项目 logo 生成 VS Code 扩展图标：128×128、圆角白底 PNG。

为什么不抠成透明底：这个 logo 的最亮面是 (252,254,254)，与白底 (254,254,252)
几乎同色——按「接近白就透明」处理会把 logo 自己的高光一起吃掉，而按连通域抠也
区分不开两者。所以保留白底、只做圆角（视觉上像一张小卡片，深浅主题都清楚）。

复用 tools/make_logo.py 里同一套「找内容边界」判据（与纯白差异 > 24），
保证扩展图标与 .ico/.icns 的裁切口径一致。
"""
from pathlib import Path

from PIL import Image, ImageDraw

SRC = Path("assets/基石_logo.png")
DST = Path("editors/vscode-jishi/icon.png")
SIZE = 128
PAD_RATIO = 0.06        # 内容四周留白（相对成品边长）
RADIUS_RATIO = 0.18     # 圆角半径

img = Image.open(SRC).convert("RGB")
w, h = img.size
px = img.load()

# 1) 内容边界
left, top, right, bottom = w, h, 0, 0
for y in range(0, h, 2):
    for x in range(0, w, 2):
        r, g, b = px[x, y]
        if 255 - min(r, g, b) > 24:
            left = min(left, x)
            right = max(right, x)
            top = min(top, y)
            bottom = max(bottom, y)
print(f"源图 {w}×{h}；内容边界 ({left},{top})-({right},{bottom})")

crop = img.crop((left, top, right + 1, bottom + 1))
cw, ch = crop.size

# 2) 补成正方形（白底），内容占比 1 - 2*PAD
side = round(max(cw, ch) / (1 - 2 * PAD_RATIO))
canvas = Image.new("RGB", (side, side), (255, 255, 255))
canvas.paste(crop, ((side - cw) // 2, (side - ch) // 2))

# 3) 缩到 128，再切圆角
icon = canvas.resize((SIZE, SIZE), Image.LANCZOS)
radius = round(SIZE * RADIUS_RATIO)
mask = Image.new("L", (SIZE, SIZE), 0)
ImageDraw.Draw(mask).rounded_rectangle(
    [0, 0, SIZE - 1, SIZE - 1], radius=radius, fill=255)
out = icon.convert("RGBA")
out.putalpha(mask)

DST.parent.mkdir(parents=True, exist_ok=True)
out.save(DST, "PNG")
print(f"已生成 {DST}  {out.size[0]}×{out.size[1]} {out.mode}  "
      f"{DST.stat().st_size} 字节（圆角 {radius}px，边距 {PAD_RATIO:.0%}）")
