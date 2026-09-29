"""生成 cc-voice 桌面快捷方式的图标 assets/icon.ico。

图形：左边三根声波柱，右边一个文字光标，意思是「说的话变成光标处的字」。
配色取自灵动岛（daemon/render.py）：暖炭色底板、乳白声波、陶土色光标。
32px 及以下另画一版：两根粗柱 + 粗一号的工字光标，细节多了会糊。
改完形状重新生成（可传一个输出根目录，先生成到别处预览）：
    .venv\\Scripts\\python tools\\make_icon.py
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
N = 1024                                   # 母版边长，各尺寸都从它缩小

BG_TOP, BG_BOT = (62, 56, 48), (34, 31, 27)
EDGE = (255, 255, 255, 26)                 # 一圈很淡的亮边，深色壁纸上也看得出轮廓
BAR = (246, 243, 237)                      # 同 render.py 的 PANEL_BOT
CARET = (222, 132, 98)                     # 陶土色提亮一档

FULL = dict(inset=40, radius=230, edge=6, bar_w=88, gap=64, bars=(260, 480, 340),
            caret_gap=96, stem=60, caret_h=600, serif_w=140, serif_h=48)
SMALL = dict(inset=0, radius=210, edge=0, bar_w=150, gap=110, bars=(330, 560),
             caret_gap=120, stem=100, caret_h=760, serif_w=260, serif_h=90)   # 光标比声波细，才不像第三根柱子
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def render(g, size):
    i = g["inset"]
    plate = (i, i, N - 1 - i, N - 1 - i)

    grad = Image.linear_gradient("L").resize((N, N))      # 上黑下白，当插值系数用
    bg = Image.composite(Image.new("RGB", (N, N), BG_BOT), Image.new("RGB", (N, N), BG_TOP), grad)
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle(plate, g["radius"], fill=255)
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    img.paste(bg, (0, 0), mask)

    d = ImageDraw.Draw(img)
    if g["edge"]:
        d.rounded_rectangle(plate, g["radius"], outline=EDGE, width=g["edge"])

    caret_w = max(g["stem"], g["serif_w"])
    n = len(g["bars"])
    total = n * g["bar_w"] + (n - 1) * g["gap"] + g["caret_gap"] + caret_w
    x, cy = (N - total) / 2, N / 2
    for h in g["bars"]:
        d.rounded_rectangle((x, cy - h / 2, x + g["bar_w"], cy + h / 2), g["bar_w"] / 2, fill=BAR)
        x += g["bar_w"] + g["gap"]
    x += g["caret_gap"] - g["gap"]
    mid, top, bot = x + caret_w / 2, cy - g["caret_h"] / 2, cy + g["caret_h"] / 2
    parts = [(mid - g["stem"] / 2, top, mid + g["stem"] / 2, bot)]
    if g["serif_w"]:
        sw, sh = g["serif_w"] / 2, g["serif_h"]
        parts += [(mid - sw, top, mid + sw, top + sh), (mid - sw, bot - sh, mid + sw, bot)]
    for p in parts:
        d.rounded_rectangle(p, min(p[2] - p[0], p[3] - p[1]) / 4, fill=CARET)

    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    out = ROOT / "assets"
    out.mkdir(parents=True, exist_ok=True)
    frames = [render(SMALL if s <= 32 else FULL, s) for s in SIZES]
    frames[-1].save(out / "icon.ico", format="ICO",
                    sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print("已生成", out / "icon.ico")
