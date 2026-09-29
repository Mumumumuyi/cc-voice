"""Win32 绑定：DPI、前台窗口、剪贴板、SendInput、低级鼠标/键盘钩子。

只封装本项目真正用到的调用，不做通用 winapi 库。
"""
import ctypes
from ctypes import wintypes

user32   = ctypes.WinDLL("user32",   use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

# ---------------------------------------------------------------- DPI
# 本机 175% 缩放：不声明感知的话 tkinter 会被系统拉伸成糊图。
# 声明 PER_MONITOR_AWARE_V2 后所有坐标变成物理像素，HUD 尺寸自己乘 scale()。
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def set_dpi_aware() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except AttributeError:                     # Win8.1 及更早
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)


def scale(hwnd: int = 0) -> float:
    """返回当前窗口所在显示器的缩放倍率（175% -> 1.75）。"""
    try:
        return user32.GetDpiForWindow(hwnd) / 96.0 if hwnd else user32.GetDpiForSystem() / 96.0
    except AttributeError:
        return 1.0


# ---------------------------------------------------------- 前台窗口
def foreground_pid() -> int:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def foreground_title() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


# ------------------------------------------------------------- 剪贴板
CF_UNICODETEXT = 13
GMEM_MOVEABLE  = 0x0002

# 必须显式声明返回类型：ctypes 默认按 32 位有符号 int 处理返回值，而 x64 上
# 句柄和指针是 64 位 —— 被截断并符号扩展后解引用就是访问违例（0xc0000005，
# 实测在 GetClipboardData/GlobalLock 上让整个守护进程静默崩溃）。
user32.GetClipboardData.restype    = wintypes.HANDLE
user32.GetClipboardData.argtypes   = [wintypes.UINT]
user32.SetClipboardData.restype    = wintypes.HANDLE
user32.SetClipboardData.argtypes   = [wintypes.UINT, wintypes.HANDLE]
user32.OpenClipboard.argtypes      = [wintypes.HWND]
kernel32.GlobalAlloc.restype       = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes      = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype        = wintypes.LPVOID
kernel32.GlobalLock.argtypes       = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes     = [wintypes.HGLOBAL]


def _with_clipboard(fn):
    for _ in range(10):                       # 别的进程可能正占着，退让重试
        if user32.OpenClipboard(None):
            try:
                return fn()
            finally:
                user32.CloseClipboard()
        kernel32.Sleep(20)
    raise OSError("无法打开剪贴板")


def clipboard_get() -> str:
    def _get():
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.c_wchar_p(p).value or ""
        finally:
            kernel32.GlobalUnlock(h)
    try:
        return _with_clipboard(_get)
    except OSError:
        return ""


def clipboard_set(text: str) -> None:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)

    def _set():
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, size)
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)   # 所有权移交系统，不要 GlobalFree
    _with_clipboard(_set)


# ----------------------------------------------------------- SendInput
INPUT_KEYBOARD  = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_V, VK_SHIFT, VK_INSERT = 0x11, 0x56, 0x10, 0x2D


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("_pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key(vk: int, up: bool) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD,
                 u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=0,
                                             dwFlags=KEYEVENTF_KEYUP if up else 0,
                                             time=0, dwExtraInfo=0)))


def send_keys(seq) -> None:
    """seq: [(vk, is_up), ...]，一次 SendInput 发完，避免中途被别的输入插队。"""
    arr = (INPUT * len(seq))(*[_key(vk, up) for vk, up in seq])
    user32.SendInput(len(seq), ctypes.byref(arr), ctypes.sizeof(INPUT))


def paste(shift_insert: bool = False) -> None:
    """向前台窗口发送粘贴键。终端里 Shift+Insert 比 Ctrl+V 兼容性更好。"""
    mod, key = (VK_SHIFT, VK_INSERT) if shift_insert else (VK_CONTROL, VK_V)
    send_keys([(mod, False), (key, False), (key, True), (mod, True)])


# ------------------------------------------------------- 低级鼠标钩子
WH_MOUSE_LL      = 14
WM_XBUTTONDOWN   = 0x020B
WM_XBUTTONUP     = 0x020C
WM_LBUTTONDOWN   = 0x0201
WM_LBUTTONUP     = 0x0202
WM_MOUSEMOVE     = 0x0200
WM_MBUTTONDOWN   = 0x0207
WM_MBUTTONUP     = 0x0208
XBUTTON1, XBUTTON2 = 1, 2

WH_KEYBOARD_LL   = 13
WM_KEYDOWN, WM_KEYUP    = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype  = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype     = ctypes.c_long
user32.CallNextHookEx.argtypes    = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]


def install_hook(hook_id: int, callback) -> tuple:
    """callback(nCode, wParam, lParam) -> True 表示吞掉该事件。返回 (hook, 保活的 HOOKPROC)。"""
    @HOOKPROC
    def proc(n_code, w_param, l_param):
        if n_code >= 0:
            try:
                if callback(n_code, w_param, l_param):
                    return 1
            except Exception:                 # 钩子里抛异常会被系统摘掉钩子，必须吞
                pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    handle = user32.SetWindowsHookExW(hook_id, proc, None, 0)
    if not handle:
        raise OSError(f"SetWindowsHookExW({hook_id}) 失败: {ctypes.get_last_error()}")
    return handle, proc                        # proc 必须被调用方持有，否则被 GC 后钩子崩溃


def message_loop(should_stop) -> None:
    """钩子线程的 Win32 消息泵。低级钩子没有消息循环就收不到事件。"""
    msg = wintypes.MSG()
    while not should_stop():
        # PeekMessage + Sleep 而非 GetMessage：GetMessage 会阻塞到有消息，无法响应停止标志
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            kernel32.Sleep(8)


# --------------------------------------------------- 多显示器 / 光标定位
MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


# restype 与 argtypes 必须成对声明，理由见 layered.py 顶部注释
user32.MonitorFromPoint.restype    = wintypes.HMONITOR
user32.MonitorFromPoint.argtypes   = [POINT, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes    = [wintypes.HMONITOR, ctypes.c_void_p]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetParent.restype           = wintypes.HWND
user32.GetParent.argtypes          = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes     = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes      = [wintypes.HWND, ctypes.c_void_p]
user32.GetDpiForWindow.argtypes    = [wintypes.HWND]
user32.ShowWindow.argtypes         = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes       = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT]
user32.GetWindowLongPtrW.argtypes  = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.argtypes  = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
user32.SetWindowRgn.argtypes       = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
user32.SendInput.argtypes          = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]


def cursor_pos() -> tuple[int, int]:
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def monitor_work_area_at(x: int, y: int) -> tuple[int, int, int, int, float]:
    """返回包含点 (x,y) 的显示器工作区 (l, t, r, b) 与该屏缩放倍率。

    悬浮条要跟着「当前正在用的那块屏」走，而不是钉死主屏 —— 否则外接
    显示器上按下侧键，反馈条会出现在笔记本屏幕上。
    """
    hmon = user32.MonitorFromPoint(POINT(x, y), MONITOR_DEFAULTTONEAREST)
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    dpi_x, dpi_y = ctypes.c_uint(96), ctypes.c_uint(96)
    try:
        ctypes.WinDLL("shcore").GetDpiForMonitor(hmon, 0,
                                                 ctypes.byref(dpi_x), ctypes.byref(dpi_y))
    except (AttributeError, OSError):
        pass
    w = mi.rcWork
    return w.left, w.top, w.right, w.bottom, dpi_x.value / 96.0


# ------------------------------------------- 窗口样式：穿透 / 不抢焦点
GWL_EXSTYLE       = -20
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020     # 鼠标事件穿透到下层窗口
WS_EX_NOACTIVATE  = 0x08000000     # 点击不激活、不抢焦点
WS_EX_TOOLWINDOW  = 0x00000080     # 不出现在 Alt+Tab 与任务栏

user32.GetWindowLongPtrW.restype  = ctypes.c_longlong
user32.SetWindowLongPtrW.restype  = ctypes.c_longlong


def make_overlay(hwnd: int) -> None:
    """把窗口变成绝不干扰正常操作的覆盖层：穿透、不抢焦点、不进 Alt+Tab。

    拖动不靠窗口自己收鼠标消息 —— 实测即使带 WS_EX_NOACTIVATE，tkinter 处理
    点击时仍会把窗口顶到前台，而闸门正是靠前台窗口 PID 判断是不是 Claude Code
    终端，一旦被顶前台语音输入就彻底失效。改由已有的低级鼠标钩子在消息抵达
    窗口之前拦下并实现拖动，窗口全程保持穿透。
    """
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)


# ------------------------------------------------------- 定时器精度
# Windows 默认定时器粒度 15.6ms，tkinter 的 after(16) 会被量化成 31ms，
# 短动画只剩 8 帧。临时提到 1ms 能拿到 60fps；这是系统级设置且略增功耗，
# 所以只在动画进行中开启，结束立刻还原（必须成对调用）。
_winmm = ctypes.WinDLL("winmm")
_timer_depth = 0


def timer_precision(enable: bool) -> None:
    global _timer_depth
    if enable:
        if _timer_depth == 0:
            _winmm.timeBeginPeriod(1)
        _timer_depth += 1
    elif _timer_depth > 0:
        _timer_depth -= 1
        if _timer_depth == 0:
            _winmm.timeEndPeriod(1)
