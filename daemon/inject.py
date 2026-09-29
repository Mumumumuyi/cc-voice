"""把文本送进前台窗口。

用剪贴板 + 粘贴键，而不是逐字符 SendInput：中文走 KEYEVENTF_UNICODE 逐字发送
在终端里容易丢字、且长句要几百次系统调用。剪贴板一次到位。

代价是会覆盖用户剪贴板，所以贴完延时还原 —— 延时不能太短，目标窗口读剪贴板
是异步的，还原太快会贴出上一份内容。
"""
import threading
import time

import winapi

# 连着两句贴得很近时（上一句还没还原剪贴板，下一句又来了），只在第一句前存一次用户的
# 原剪贴板，由最后一句负责还原。否则第二句会把第一句的文字当「原剪贴板」存下来，
# 第一句的还原还可能在第二句粘贴前一刻把剪贴板改回去，贴出错的内容。
_lock = threading.Lock()
_gen = 0
_pending = False                          # 有一份原剪贴板等着还原
_saved = None


def paste_text(text: str, method: str = "ctrl_v", restore_ms: int = 400,
               press_enter: bool = False) -> None:
    global _gen, _pending, _saved
    if not text:
        return
    with _lock:
        _gen += 1
        gen = _gen
        if not _pending:
            _saved, _pending = winapi.clipboard_get(), True
        winapi.clipboard_set(text)
        time.sleep(0.03)                 # 让剪贴板变更通知先落地
        winapi.paste(shift_insert=(method == "shift_insert"))
        if press_enter:
            time.sleep(0.05)
            winapi.send_keys([(0x0D, False), (0x0D, True)])   # VK_RETURN

    def _restore():
        global _pending
        time.sleep(restore_ms / 1000.0)
        with _lock:
            if gen != _gen:
                return                    # 之后又贴过一句，由那一句负责还原
            _pending = False
            try:
                if _saved:
                    winapi.clipboard_set(_saved)
            except OSError:
                pass                      # 还原失败不影响主流程，静默即可

    threading.Thread(target=_restore, daemon=True).start()
