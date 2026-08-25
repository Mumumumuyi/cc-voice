"""用 Pillow 画灵动岛的每一帧。

抗锯齿策略：胶囊轮廓这种曲线在 4 倍超采样下绘制再 LANCZOS 缩小，边缘变成
平滑灰阶。文字交给 PIL 自带的字形抗锯齿，竖直的波形柱本身没有斜边。

柔和感来自真实的高斯模糊投影 —— 只有拿到 alpha 通道才做得到，这也是从
tkinter Canvas 换到分层窗口的主要动机。

底板按「三段切片」生成：药丸在收起/展开之间变宽变窄，若按宽度缓存则每帧
都是缓存未命中（超采样 + 高斯模糊约 10ms，撑不住 60fps）。胶囊的渐变只沿
垂直方向，所以左帽 + 中段横向拉伸 + 右帽在数学上与重画完全等价，且 O(1)。
"""
import math
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# 乳白暖调
PANEL_TOP = (255, 254, 252)
PANEL_BOT = (246, 243, 237)
BORDER = (255, 255, 255)
TEXT = (40, 36, 30)
MUTED = (126, 119, 107)
CLAY = (192, 138, 110)
SAGE = (140, 163, 132)
AMBER = (210, 162, 76)
BAR_BASE = (150, 140, 126)

LED_ON = (74, 190, 118)      # 绿：正在语音输入
LED_IDLE = (214, 98, 80)     # 红：待机
LED_OFF = (170, 164, 154)    # 灰：已暂停

# 225 是实测甜点：在 Claude Code 的深色终端上仍是乳白而非灰，文字可读，
# 边缘与投影保持通透。低于 200 面板会被黑底压成中灰，深色文字失去对比度。
PANEL_ALPHA = 225
BORDER_ALPHA = 190
SHADOW_ALPHA = 42
SHADOW_BLUR = 9              # 逻辑像素
SHADOW_DY = 3

H = 34                       # 药丸高度（96dpi 逻辑像素）
W_MAX = 320                  # 最宽形态，决定窗口画布尺寸
BARS = 20

FONT_CJK = r"C:\Windows\Fonts\msyhl.ttc"      # 微软雅黑 Light，细字重更清秀
FONT_NUM = r"C:\Windows\Fonts\segoeui.ttf"
SS = 4                                        # 曲线超采样倍数


@lru_cache(maxsize=8)
def _font(path: str, px: int):
    try:
        return ImageFont.truetype(path, px)
    except OSError:
        return ImageFont.load_default()


@lru_cache(maxsize=4)
def _panel_full(w: int, h: int, blur: int, dy: int, alpha: int) -> tuple:
    """按最大宽度渲染一次底板（投影 + 胶囊 + 描边），供切片拉伸复用。"""
    pad = blur * 2 + abs(dy) + 2
    cw, ch = w + pad * 2, h + pad * 2

    # 4 倍超采样画轮廓再缩小 —— 抗锯齿的来源
    mask = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, w * SS - 1, h * SS - 1], radius=h * SS // 2, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)

    bw = max(1, round(SS * 1.1))
    inner = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(inner).rounded_rectangle(
        [bw, bw, w * SS - 1 - bw, h * SS - 1 - bw],
        radius=(h * SS - 2 * bw) // 2, fill=255)
    ring = ImageChops.subtract(mask, inner.resize((w, h), Image.LANCZOS))

    # 垂直渐变：顶部近乎纯白，底部落到米白，模拟柔光从上方漫射
    grad = Image.new("RGB", (1, h))
    for i in range(h):
        t = (i / max(1, h - 1)) ** 0.8
        grad.putpixel((0, i), tuple(round(PANEL_TOP[c] + (PANEL_BOT[c] - PANEL_TOP[c]) * t)
                                    for c in range(3)))
    panel = grad.resize((w, h)).convert("RGBA")
    panel.putalpha(mask.point(lambda v: v * alpha // 255))

    # 描边上半段更亮，做出玻璃弧面被顶光扫到的感觉
    edge = Image.new("RGBA", (w, h), BORDER + (0,))
    fade = Image.new("L", (1, h))
    for i in range(h):
        fade.putpixel((0, i), round(BORDER_ALPHA * (1.0 - 0.55 * (i / max(1, h - 1)))))
    edge.putalpha(ImageChops.multiply(ring, fade.resize((w, h))))
    panel.alpha_composite(edge)

    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, SHADOW_ALPHA), (pad, pad + dy), mask)
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(blur)))
    canvas.alpha_composite(panel, (pad, pad))
    return canvas, pad


def panel_for(w: int, h: int, blur: int, dy: int, alpha: int) -> tuple:
    """任意宽度的底板：左帽 + 中段横向拉伸 + 右帽。返回 (图像, 内边距)。"""
    full, pad = _panel_full(W_MAX_PX(h), h, blur, dy, alpha)
    cap = pad + h // 2 + 1                     # 帽子宽度：留白 + 半圆
    if w >= full.width - pad * 2:
        return full.copy(), pad
    out_w = w + pad * 2
    out = Image.new("RGBA", (out_w, full.height), (0, 0, 0, 0))
    out.paste(full.crop((0, 0, cap, full.height)), (0, 0))
    out.paste(full.crop((full.width - cap, 0, full.width, full.height)),
              (out_w - cap, 0))
    mid_w = out_w - cap * 2
    if mid_w > 0:
        # 中段取真实的一小条再拉伸，而不是取 1 像素 —— 1 像素在 LANCZOS 下
        # 会把边缘的半透明像素也拉开，中段会出现一道竖向色带
        strip = full.crop((cap, 0, cap + 8, full.height))
        out.paste(strip.resize((mid_w, full.height), Image.BILINEAR), (cap, 0))
    return out, pad


def W_MAX_PX(h: int) -> int:
    """最大宽度按高度等比推出，保证 _panel_full 只有一份缓存。"""
    return round(W_MAX * h / H)


class Frame:
    """一帧的绘制上下文。坐标全是物理像素，缩放由调用方乘进 scale。"""

    def __init__(self, scale: float, width: float, alpha: int = PANEL_ALPHA):
        self.s = scale
        self.h = round(H * scale)
        self.w = max(self.h, round(width * scale))
        base, pad = panel_for(self.w, self.h,
                              max(2, round(SHADOW_BLUR * scale)),
                              round(SHADOW_DY * scale), alpha)
        self.img = base
        self.pad = pad
        self.d = ImageDraw.Draw(self.img)
        self.left = pad
        self.right = pad + self.w
        self.cy = pad + self.h / 2

    # ------------------------------------------------------------ 图元
    def led(self, x, y, color, radius, glow=1.0):
        """状态灯：外圈柔光 + 实心点。glow 控制呼吸强度。"""
        r = radius
        if glow > 0.02:
            g = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
            ImageDraw.Draw(g).ellipse(
                [x - r * 3.0, y - r * 3.0, x + r * 3.0, y + r * 3.0],
                fill=color + (round(95 * glow),))
            self.img.alpha_composite(g.filter(ImageFilter.GaussianBlur(r * 1.1)))
        self._aa_ellipse(x, y, r, color)

    def _aa_ellipse(self, x, y, r, color):
        """小圆单独超采样：直接画会有明显锯齿，而它正是视线落点。"""
        box = max(4, math.ceil(r * 2) + 4)
        tile = Image.new("L", (box * SS, box * SS), 0)
        c = box * SS / 2
        ImageDraw.Draw(tile).ellipse([c - r * SS, c - r * SS, c + r * SS, c + r * SS],
                                     fill=255)
        solid = Image.new("RGBA", (box, box), color + (255,))
        solid.putalpha(tile.resize((box, box), Image.LANCZOS))
        self.img.alpha_composite(solid, (round(x - box / 2), round(y - box / 2)))

    def bars(self, x, y, width, levels, room):
        """竖直圆头细条。竖边无斜率，不需要抗锯齿处理。"""
        bw = max(2.0, 2.0 * self.s)
        gap = (width - BARS * bw) / max(1, BARS - 1)
        for i, lv in enumerate(levels[-BARS:]):
            bx = x + i * (bw + gap)
            bh = max(bw / 2, (lv ** 0.55) * room)   # 开方压缩：小音量也看得见起伏
            t = i / max(1, BARS - 1)
            col = tuple(round(BAR_BASE[c] + (CLAY[c] - BAR_BASE[c]) * t * 0.65)
                        for c in range(3))
            self.d.rounded_rectangle([bx, y - bh, bx + bw, y + bh],
                                     radius=bw / 2, fill=col + (225,))

    def dots(self, x, y, n, phase):
        """待机态的极简省略号，暗示「在听着」而不喧宾夺主。"""
        r = 1.5 * self.s
        for i in range(n):
            a = 0.35 + 0.4 * (0.5 + 0.5 * math.sin(phase - i * 0.9))
            self._aa_ellipse(x + i * r * 4, y, r,
                             tuple(round(BAR_BASE[c] + (255 - BAR_BASE[c]) * (1 - a))
                                   for c in range(3)))

    def arc(self, x, y, r, phase):
        box = max(8, math.ceil(r * 2) + 6)
        tile = Image.new("RGBA", (box * SS, box * SS), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        c = box * SS / 2
        for i in range(8):                    # 段数够多才读得出是转圈而不是一撇
            td.arc([c - r * SS, c - r * SS, c + r * SS, c + r * SS],
                   start=phase - i * 26, end=phase - i * 26 + 20,
                   fill=CLAY + (round(235 - i * 26),),
                   width=max(SS, round(2.0 * self.s * SS)))
        self.img.alpha_composite(tile.resize((box, box), Image.LANCZOS),
                                 (round(x - box / 2), round(y - box / 2)))

    def check(self, x, y, u):
        box = math.ceil(u * 2) + 8
        tile = Image.new("RGBA", (box * SS, box * SS), (0, 0, 0, 0))
        c = box * SS / 2
        ImageDraw.Draw(tile).line(
            [c - u * SS, c, c - u * SS * 0.15, c + u * SS * 0.85, c + u * SS, c - u * SS * 0.9],
            fill=SAGE + (255,), width=max(SS, round(1.8 * self.s * SS)), joint="curve")
        self.img.alpha_composite(tile.resize((box, box), Image.LANCZOS),
                                 (round(x - box / 2), round(y - box / 2)))

    def text(self, x, y, s, color, px, anchor="lm", font=FONT_CJK, maxw=None):
        f = _font(font, px)
        if maxw:
            while s and self.d.textlength(s, font=f) > maxw:
                s = s[:-1]
                if self.d.textlength(s + "…", font=f) <= maxw:
                    s += "…"
                    break
        self.d.text((x, y), s, font=f, fill=color + (255,), anchor=anchor)

    def measure(self, s, px, font=FONT_CJK) -> float:
        return self.d.textlength(s, font=_font(font, px))


def text_width(s: str, px: int, font: str = FONT_CJK) -> float:
    """不建帧就量文字宽度，用于决定药丸该展开到多宽。"""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    return probe.textlength(s, font=_font(font, px))
