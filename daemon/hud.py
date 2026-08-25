"""灵动岛：常驻屏幕的乳白药丸，按状态变宽变窄，可随意拖动。

形态：
  待机 idle       红灯 + 三个呼吸点，最窄
  聆听 listening  绿灯 + 实时波形 + 计时
  识别 thinking   绿灯 + 转圈
  完成 done       绿灯 + 对勾 + 识别结果，按文字长度展开
  受阻 blocked    琥珀灯 + 原因

交互：
  拖动    左键按住移动，松开记住位置
  双击    打开管理面板

三个实现要点：

1) 去掉 WS_EX_TRANSPARENT 才能拖动（穿透窗口收不到鼠标消息），但保留
   WS_EX_NOACTIVATE —— 否则拖完焦点会从终端跑到药丸上。分层窗口在
   ULW_ALPHA 下完全透明的像素自动穿透点击，所以窗口可以一直保持最大
   尺寸，药丸缩小时周围空白照样不挡操作。

2) 宽度形变用 easeOutBack，天然带一点回弹，接近 iOS 灵动岛的手感。

3) 渲染走 render.py + layered.py，不用 tkinter Canvas：Canvas 没有抗锯齿、
   色键透明只有全透/全不透两态，曲线边缘必然是硬阶梯。tkinter 在这里只
   负责提供 HWND、事件绑定和 after() 定时器。
"""
import math
import time
import tkinter as tk

from PIL import Image

import render
import winapi
from layered import LayeredSurface

ALPHA = 0.95                 # 整体不透明度，叠加在逐像素 alpha 之上；配置可覆盖
MORPH_MS = 300               # 宽度形变时长
IN_MS, OUT_MS = 260, 170     # 首次出现 / 隐藏的缩放淡入淡出
K_IN, K_OUT = 0.86, 0.92
BACK = 1.5                   # easeOutBack 回弹强度
FPS_ANIM, FPS_IDLE = 16, 40

W_IDLE = 62                  # 各形态的目标宽度（96dpi 逻辑像素）
W_LISTEN = 216
W_THINK = 128
# 58 = 左边距 13 + 灯 11 + 对勾 14 + 右边距 13 再留 7 的余量。算少了末字会被
# maxw 截掉（实测 46 时「可折叠的」丢了「的」）。
W_TEXT_PAD = 58              # 文字态：文字宽度之外的固定占位


def _ease_out_back(t: float, back: float = BACK) -> float:
    u = t - 1.0
    return u * u * ((back + 1.0) * u + back) + 1.0


class Hud:
    """只在 tkinter 主线程被调用；其它线程通过 App 的队列间接驱动。"""

    def __init__(self, root: tk.Tk, opacity: float = ALPHA, position=None,
                 on_move=None, on_double_click=None):
        self.root = root
        self.opacity = opacity
        self.on_move = on_move
        self.on_double_click = on_double_click

        self.state = "idle"
        self.paused = False
        self.scale = 1.0
        self.levels = [0.0] * render.BARS
        self.tick = 0
        self.text = ""
        self._visible = False
        self._anim = None
        self._t0 = 0.0
        self._origin = position
        self._canvas = (0, 0)
        self._w = W_IDLE            # 当前宽度（逻辑像素）
        self._w_from = W_IDLE
        self._w_to = W_IDLE
        self._morph_t0 = 0.0
        self._drag = None

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.geometry("10x10+0+0")
        self.win.withdraw()
        self.win.update_idletasks()
        # 之后一律用 ShowWindow(SW_SHOWNOACTIVATE) 显示：tkinter 的 deiconify()
        # 会把窗口顶到前台，而闸门靠前台窗口 PID 判断终端身份，被顶前台后
        # 语音输入会彻底失效（实测启动即抢焦点）。
        self.hwnd = winapi.user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        winapi.make_overlay(self.hwnd)
        self.surface = LayeredSurface(self.hwnd)

        self._animate()

    # ------------------------------------------------------------ 定位
    def _measure_canvas(self):
        l, t, r, b, sc = winapi.monitor_work_area_at(*winapi.cursor_pos())
        self.scale = sc
        probe, _ = render.panel_for(round(render.W_MAX * sc), round(render.H * sc),
                                    max(2, round(render.SHADOW_BLUR * sc)),
                                    round(render.SHADOW_DY * sc), render.PANEL_ALPHA)
        self._canvas = probe.size
        return l, t, r, b

    def _place(self):
        """首次定位在光标所在屏的底部居中；之后由用户拖动决定。"""
        l, t, r, b = self._measure_canvas()
        cw, ch = self._canvas
        if self._origin is None:
            self._origin = (l + (r - l - cw) // 2,
                            b - ch - int(max(6, (b - t) * 0.010)))
        else:                                  # 换了显示器/分辨率时拉回可见区域
            x, y = self._origin
            self._origin = (max(l - cw // 3, min(x, r - cw * 2 // 3)),
                            max(t, min(y, b - ch // 2)))

    # ------------------------------------- 拖动（由低级鼠标钩子线程调用）
    def hit_test(self, x: int, y: int) -> bool:
        """屏幕坐标是否落在可见的药丸上。窗口画布比药丸大，只认药丸本体。"""
        if not self._visible or not self._origin:
            return False
        cw, ch = self._canvas
        cx = self._origin[0] + cw / 2
        cy = self._origin[1] + ch / 2
        return (abs(x - cx) <= self._w * self.scale / 2
                and abs(y - cy) <= render.H * self.scale / 2)

    def move_by(self, dx: int, dy: int) -> None:
        """钩子线程直接改坐标，渲染循环下一帧自然跟上 —— 元组赋值是原子的，
        不需要加锁，也不能在这里碰 tkinter。"""
        self._origin = (self._origin[0] + dx, self._origin[1] + dy)

    def finish_move(self) -> None:
        if self.on_move:
            self.on_move(self._origin)          # 落盘记住位置

    def double_click(self) -> None:
        if self.on_double_click:
            self.on_double_click()

    # -------------------------------------------------------- 状态切换
    def show(self, state: str, text: str = ""):
        if state == "hidden":
            if self._visible and self._anim != "out":
                self._begin_anim("out")
            return
        self.state, self.text = state, text
        self._morph_to(self._target_width())
        if not self._visible or self._anim == "out":
            self._visible = True
            self._begin_anim("in")
            self._place()
            winapi.user32.ShowWindow(self.hwnd, 4)      # SW_SHOWNOACTIVATE
        winapi.user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0013)   # HWND_TOPMOST

    def _target_width(self) -> float:
        if self.state == "listening":
            return W_LISTEN
        if self.state == "thinking":
            return W_THINK
        if self.state in ("done", "blocked"):
            px = max(10, round(12.5 * self.scale))
            need = render.text_width(self.text, px) / self.scale + W_TEXT_PAD
            return max(W_THINK, min(render.W_MAX, need))
        return W_IDLE

    def _morph_to(self, w: float):
        if abs(w - self._w_to) < 0.5:
            return
        self._w_from, self._w_to = self._w, w
        self._morph_t0 = time.time()
        winapi.timer_precision(True)
        self.root.after(MORPH_MS + 60, lambda: winapi.timer_precision(False))

    def _begin_anim(self, mode: str) -> None:
        if self._anim is None:
            winapi.timer_precision(True)     # 与 _end_anim 成对
        self._anim, self._t0 = mode, time.time()

    def _end_anim(self) -> None:
        if self._anim is not None:
            self._anim = None
            winapi.timer_precision(False)

    def set_level(self, level: float):
        self.levels = self.levels[1:] + [max(0.0, min(1.0, level))]

    # ------------------------------------------------------------ 动画
    def _animate(self):
        delay = FPS_IDLE
        if self._visible:
            self.tick += 1
            try:
                step = self._step()
                if step is not None:
                    morphing = self._advance_width()
                    self._draw(*step)
                    if self._anim or morphing:
                        delay = FPS_ANIM
            except (tk.TclError, OSError):
                pass
        self.root.after(delay, self._animate)

    def _advance_width(self) -> bool:
        if abs(self._w - self._w_to) < 0.3:
            self._w = self._w_to
            return False
        t = min(1.0, (time.time() - self._morph_t0) * 1000.0 / MORPH_MS)
        self._w = self._w_from + (self._w_to - self._w_from) * _ease_out_back(t)
        return t < 1.0

    def _step(self):
        """推进淡入/淡出，返回 (缩放, 不透明度)；None 表示本帧无需绘制。

        进度按墙钟时间算而不是帧计数 —— 掉帧时按帧计数会让动画拖长，
        在快机器上又会一闪而过。
        """
        if self._anim is None:
            return 1.0, 1.0
        dur = IN_MS if self._anim == "in" else OUT_MS
        t = min(1.0, (time.time() - self._t0) * 1000.0 / dur)
        if self._anim == "in":
            k, a = K_IN + (1.0 - K_IN) * _ease_out_back(t), min(1.0, t * 1.7)
        else:
            e = t * t
            k, a = 1.0 - (1.0 - K_OUT) * e, 1.0 - e
        if t >= 1.0:
            was_out = self._anim == "out"
            self._end_anim()
            if was_out:
                self._visible = False
                winapi.user32.ShowWindow(self.hwnd, 0)  # SW_HIDE
                return None
        return k, a

    # ------------------------------------------------------------ 绘制
    def _draw(self, k: float, opacity: float):
        s = self.scale
        f = render.Frame(s, self._w)
        pad_in = 13 * s
        x0, x1, cy = f.left, f.right, f.cy

        led_x = x0 + pad_in
        led_r = 3.4 * s
        if self.state in ("listening", "thinking", "done"):
            # 绿：正在语音输入。呼吸更快更亮
            f.led(led_x, cy, render.LED_ON, led_r,
                  0.65 + 0.35 * math.sin(self.tick * 0.30))
        elif self.state == "blocked":
            f.led(led_x, cy, render.AMBER, led_r, 0.5)
        elif self.paused:
            f.led(led_x, cy, render.LED_OFF, led_r, 0.0)   # 灰且不呼吸 = 已暂停
        else:
            # 红：待机。呼吸慢而弱，存在感低
            f.led(led_x, cy, render.LED_IDLE, led_r,
                  0.22 + 0.22 * math.sin(self.tick * 0.10))

        cx = led_x + 11 * s
        if self.state == "idle":
            f.dots(cx + 2 * s, cy, 3, self.tick * 0.14)
        elif self.state == "listening":
            f.text(x1 - pad_in, cy, self.text or "0:00", render.MUTED,
                   max(10, round(11 * s)), anchor="rm", font=render.FONT_NUM)
            f.bars(cx, cy, (x1 - pad_in - 32 * s) - cx, self.levels, f.h / 2 - 9 * s)
        elif self.state == "thinking":
            f.arc(cx + 5 * s, cy, 5.4 * s, (self.tick * 11) % 360)
            f.text(cx + 15 * s, cy, "正在识别", render.MUTED, max(10, round(12 * s)))
        elif self.state == "done":
            f.check(cx + 2 * s, cy, 3.2 * s)
            f.text(cx + 14 * s, cy, self.text, render.TEXT,
                   max(10, round(12.5 * s)), maxw=x1 - pad_in - (cx + 14 * s))
        elif self.state == "blocked":
            f.text(cx, cy, self.text, render.MUTED, max(10, round(12 * s)),
                   maxw=x1 - pad_in - cx)

        img = f.img
        if abs(k - 1.0) > 0.002:
            # 缩放动画直接缩放成品位图 —— 这正是缩放的数学定义，比重画一遍
            # 更快，也不会因为整数取整让内容在帧间跳动
            img = img.resize((max(1, round(img.width * k)),
                              max(1, round(img.height * k))), Image.LANCZOS)
        cw, ch = self._canvas
        out = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        out.alpha_composite(img, ((cw - img.width) // 2, (ch - img.height) // 2))
        self.surface.update(out, *self._origin,
                            opacity=max(0, min(255, round(255 * self.opacity * opacity))))
