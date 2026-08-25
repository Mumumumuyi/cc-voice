"""把一张 RGBA 位图直接推给窗口（UpdateLayeredWindow）。

tkinter 的 Canvas 没有抗锯齿，色键透明又只有「全透/全不透」两种状态，
曲线边缘必然是硬阶梯。分层窗口走的是另一条路：整窗内容由一张带 alpha
通道的位图决定，边缘可以是任意灰阶 —— 抗锯齿和柔和投影都靠它。

必须注意 UpdateLayeredWindow 要的是**预乘 alpha**的 BGRA。传直通 alpha
会让半透明像素发白发灰，是这条路上最常见的坑。
"""
import ctypes
from ctypes import wintypes

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# 句柄在 x64 上是 64 位，restype 和 argtypes 必须成对声明。
# 只声明 restype 反而更糟：返回值不再被截断，但没声明 argtypes 的调用方会按
# 默认的 32 位 int 去传它，直接 OverflowError（实测 CreateDIBSection 崩在这里）。
# 而句柄数值是浮动的 —— 值小时能跑、值大时崩，就是「有时好有时坏」的来源。
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.HDC, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]

AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01
ULW_ALPHA = 0x02
BI_RGB, DIB_RGB_COLORS = 0, 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


def to_premultiplied_bgra(img) -> bytes:
    """PIL RGBA（直通 alpha） -> Windows 要的预乘 BGRA。"""
    a = np.asarray(img, dtype=np.uint8)
    alpha = a[..., 3].astype(np.uint16)
    rgb = a[..., :3].astype(np.uint16)
    pm = ((rgb * alpha[..., None] + 127) // 255).astype(np.uint8)
    out = np.empty(a.shape, dtype=np.uint8)
    out[..., 0] = pm[..., 2]        # B
    out[..., 1] = pm[..., 1]        # G
    out[..., 2] = pm[..., 0]        # R
    out[..., 3] = a[..., 3]
    return out.tobytes()


class LayeredSurface:
    """绑定到一个 HWND 的分层表面。尺寸不变时复用同一块 DIB。"""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self._size = (0, 0)
        self._screen_dc = user32.GetDC(None)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        self._bitmap = None
        self._old = None
        self._bits = None

    def _ensure(self, w: int, h: int):
        if self._size == (w, h):
            return
        if self._bitmap:
            gdi32.SelectObject(self._mem_dc, self._old)
            gdi32.DeleteObject(self._bitmap)
        bmi = BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth, hdr.biHeight = w, -h        # 负高度 = 自上而下，与 PIL 一致
        hdr.biPlanes, hdr.biBitCount = 1, 32
        hdr.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        self._bitmap = gdi32.CreateDIBSection(self._screen_dc, ctypes.byref(bmi),
                                              DIB_RGB_COLORS, ctypes.byref(bits),
                                              None, 0)
        if not self._bitmap:
            raise OSError("CreateDIBSection 失败")
        self._old = gdi32.SelectObject(self._mem_dc, self._bitmap)
        self._bits = bits
        self._size = (w, h)

    def update(self, img, x: int, y: int, opacity: int = 255):
        """img 为 PIL RGBA；(x, y) 是窗口左上角的屏幕坐标。"""
        w, h = img.size
        self._ensure(w, h)
        data = to_premultiplied_bgra(img)
        ctypes.memmove(self._bits, data, len(data))

        pt_dst, size, pt_src = POINT(x, y), SIZE(w, h), POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, opacity, AC_SRC_ALPHA)
        ok = user32.UpdateLayeredWindow(
            wintypes.HWND(self.hwnd), self._screen_dc, ctypes.byref(pt_dst),
            ctypes.byref(size), self._mem_dc, ctypes.byref(pt_src),
            wintypes.DWORD(0), ctypes.byref(blend), ULW_ALPHA)
        if not ok:
            raise OSError(f"UpdateLayeredWindow 失败: {ctypes.get_last_error()}")

    def close(self):
        if self._bitmap:
            gdi32.SelectObject(self._mem_dc, self._old)
            gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        gdi32.DeleteDC(self._mem_dc)
        user32.ReleaseDC(None, self._screen_dc)
