"""触发层：低级鼠标/键盘钩子，按下开始、松开结束。

低级钩子必须装在有 Win32 消息循环的线程里，而 tkinter 的 mainloop 不是消息泵，
所以这里自带一个线程。回调里只做「判定 + 塞队列」，任何耗时操作都会拖慢
整个系统的输入响应（超过 LowLevelHooksTimeout 会被系统直接摘钩子）。
"""
import ctypes
import threading
import time

import winapi

import os
from pathlib import Path

DEBUG_DRAG = os.environ.get("CCVOICE_DEBUG_DRAG") == "1"
_DLOG = Path(__file__).resolve().parent.parent / "logs" / "drag.log"

# 排障用，默认关闭：写文件本身就是钩子回调里的 I/O，开着会增加超时风险。
# 只在定位「按了没反应 / 录音断掉」这类问题时临时打开。
DEBUG_TRIG = os.environ.get("CCVOICE_DEBUG_TRIGGER") == "1"
_TLOG = Path(__file__).resolve().parent.parent / "logs" / "trigger.log"


def _dlog(msg: str) -> None:
    with _DLOG.open("a", encoding="utf-8") as f:
        print(msg, file=f)


def _tlog(msg: str) -> None:
    now = time.time()
    with _TLOG.open("a", encoding="utf-8") as f:
        print(f"{time.strftime('%H:%M:%S')}.{int(now % 1 * 1000):03d}  {msg}", file=f)


MOUSE_BUTTONS = {"x1": winapi.XBUTTON1, "x2": winapi.XBUTTON2}
KEYS = {"rctrl": 0xA3, "rshift": 0xA1, "ralt": 0xA5, "capslock": 0x14,
        "f2": 0x71, "f4": 0x73}


class Trigger(threading.Thread):
    """on_down / on_up 在钩子线程里被调用，必须立刻返回。

    gate() 返回 True 时事件被吞掉（不传给下层窗口），否则原样放行 ——
    这样在非 Claude Code 窗口里，侧键仍是浏览器的「后退/前进」。
    """

    daemon = True

    def __init__(self, cfg: dict, gate, on_down, on_up, probe=None, island=None):
        super().__init__(name="trigger")
        self.cfg, self.gate = cfg, gate
        self.on_down, self.on_up, self.probe = on_down, on_up, probe
        self.island = island              # 灵动岛：hit_test / move_by / finish_move
        self._stop = threading.Event()
        self._held = False
        self._hooks = []
        self._drag_from = None
        self._last_click = 0.0

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------ 钩子
    def _mouse(self, n_code, w_param, l_param):
        info = ctypes.cast(l_param, ctypes.POINTER(winapi.MSLLHOOKSTRUCT)).contents
        if self._island_drag(w_param, info):
            return True
        want = MOUSE_BUTTONS.get(self.cfg["trigger"].get("mouse_button", "x2"))
        if w_param in (winapi.WM_XBUTTONDOWN, winapi.WM_XBUTTONUP):
            btn = (info.mouseData >> 16) & 0xFFFF
            down = w_param == winapi.WM_XBUTTONDOWN
            if self.probe:
                self.probe(f"mouse x{btn} {'down' if down else 'up'}")
            if DEBUG_TRIG:
                src = "注入" if info.flags & 0x01 else "真实"      # LLMHF_INJECTED
                _tlog(f"x{btn} {'按下' if down else '松开'}  {src}  held={self._held}")
            if want and btn == want:
                t0 = time.perf_counter()
                swallowed = self._edge(down)
                if DEBUG_TRIG:
                    dt = (time.perf_counter() - t0) * 1000
                    _tlog(f"   _edge {dt:.1f}ms 吞掉={swallowed}"
                          f"{'   <-- 超时风险，钩子可能被摘' if dt > 100 else ''}")
                return swallowed
        elif self.probe and w_param in (winapi.WM_MBUTTONDOWN, winapi.WM_MBUTTONUP):
            self.probe(f"mouse middle {'down' if w_param == winapi.WM_MBUTTONDOWN else 'up'}")
        return False

    def _island_drag(self, w_param, info) -> bool:
        """在消息抵达窗口之前实现拖动。

        必须在钩子里拦截而不是让窗口自己处理：实测 tkinter 处理点击时会把
        窗口顶到前台，而闸门靠前台窗口 PID 判断终端身份，一旦被顶前台语音
        输入就失效。这里吞掉事件，窗口全程保持穿透。
        """
        if self.island is None:
            return False
        if DEBUG_DRAG and w_param in (winapi.WM_LBUTTONDOWN, winapi.WM_LBUTTONUP,
                                      winapi.WM_MOUSEMOVE):
            if w_param != winapi.WM_MOUSEMOVE or self._drag_from:
                _dlog(f"msg=0x{w_param:04X} pt=({info.pt.x},{info.pt.y}) "
                      f"hit={self.island.hit_test(info.pt.x, info.pt.y)} "
                      f"drag={self._drag_from}")
        if w_param == winapi.WM_LBUTTONDOWN:
            if not self.island.hit_test(info.pt.x, info.pt.y):
                return False
            now = time.time()
            if now - self._last_click < 0.4:      # 双击打开管理面板
                self._last_click = 0.0
                self._drag_from = None
                self.island.double_click()
            else:
                self._last_click = now
                self._drag_from = (info.pt.x, info.pt.y)
            return True
        if w_param == winapi.WM_MOUSEMOVE and self._drag_from:
            dx = info.pt.x - self._drag_from[0]
            dy = info.pt.y - self._drag_from[1]
            self._drag_from = (info.pt.x, info.pt.y)
            self.island.move_by(dx, dy)
            return True
        if w_param == winapi.WM_LBUTTONUP and self._drag_from:
            # 补上最后一段位移：光标若被 SetCursorPos 类接口瞬移，中间不会有
            # WM_MOUSEMOVE，只按 move 累积会丢掉整段距离
            dx = info.pt.x - self._drag_from[0]
            dy = info.pt.y - self._drag_from[1]
            if dx or dy:
                self.island.move_by(dx, dy)
            self._drag_from = None
            self.island.finish_move()
            return True
        return False

    def _key(self, n_code, w_param, l_param):
        tr = self.cfg["trigger"]
        if not tr.get("key_enabled"):
            return False
        want = KEYS.get(tr.get("key", "rctrl"))
        info = ctypes.cast(l_param, ctypes.POINTER(winapi.KBDLLHOOKSTRUCT)).contents
        down = w_param in (winapi.WM_KEYDOWN, winapi.WM_SYSKEYDOWN)
        up = w_param in (winapi.WM_KEYUP, winapi.WM_SYSKEYUP)
        if self.probe and (down or up):
            self.probe(f"key vk=0x{info.vkCode:02X} {'down' if down else 'up'}")
        if want and info.vkCode == want:
            if down and self._held:
                return True               # 长按自动重复，吞掉但不重复触发
            if down or up:
                return self._edge(down)
        return False

    def _edge(self, down: bool) -> bool:
        if down:
            if self._held:
                return True
            ok, _ = self.gate()
            if not ok:
                return False              # 闸门关着就完全不干预这个按键
            self._held = True
            self.on_down()
            return True
        if not self._held:
            return False
        self._held = False
        self.on_up()
        return True

    # ------------------------------------------------------------ 线程
    def run(self):
        self._hooks.append(winapi.install_hook(winapi.WH_MOUSE_LL, self._mouse))
        self._hooks.append(winapi.install_hook(winapi.WH_KEYBOARD_LL, self._key))
        winapi.message_loop(self._stop.is_set)
