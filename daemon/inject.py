"""把文本送进前台窗口。

用剪贴板 + 粘贴键，而不是逐字符 SendInput：中文走 KEYEVENTF_UNICODE 逐字发送
在终端里容易丢字、且长句要几百次系统调用。剪贴板一次到位。

代价是会覆盖用户剪贴板，所以贴完延时还原 —— 延时不能太短，目标窗口读剪贴板
是异步的，还原太快会贴出上一份内容。
"""
import threading
import time

import winapi


def paste_text(text: str, method: str = "ctrl_v", restore_ms: int = 400,
               press_enter: bool = False) -> None:
    if not text:
        return
    saved = winapi.clipboard_get()
    winapi.clipboard_set(text)
    time.sleep(0.03)                     # 让剪贴板变更通知先落地
    winapi.paste(shift_insert=(method == "shift_insert"))
    if press_enter:
        time.sleep(0.05)
        winapi.send_keys([(0x0D, False), (0x0D, True)])   # VK_RETURN

    def _restore():
        time.sleep(restore_ms / 1000.0)
        try:
            if saved:
                winapi.clipboard_set(saved)
        except OSError:
            pass                          # 还原失败不影响主流程，静默即可

    threading.Thread(target=_restore, daemon=True).start()
