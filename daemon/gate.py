"""会话闸门：判定当前前台窗口是不是「正在跑 Claude Code 的终端」。

不猜窗口标题 —— 标题会变、会被 tmux/ssh 改掉。改看进程血缘：枚举全部进程，
找出所有 claude.exe，从每个往上走到根，沿途的 PID 就是「承载 Claude Code 的
终端」。前台窗口的属主 PID 落在这个集合里即放行。

这套判定**不依赖任何插件**。早期版本靠插件的 SessionStart 钩子写会话文件来
提供这个集合，结果一旦用户关掉插件自启（改用桌面快捷方式手动启停），钩子就
永远不跑、集合永远为空、闸门永远关着 —— 手动启动和「仅 Claude Code 终端」
成了互斥的两个选项。自己枚举进程就没有这个死结。

sessions/*.json 仍会被读取，但只用于在管理面板里显示入口名（cc/cc2/cc3）和
工作目录这类进程表里拿不到的信息，不参与放行判定。
"""
import ctypes
import json
import threading
import time
from ctypes import wintypes
from pathlib import Path

from winapi import foreground_pid

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
MAX_WALK = 16                      # 进程链深度上限，兼作环路保护

# 句柄是 64 位，restype 不声明会被截成 32 位有符号数，后续调用拿到的是
# 符号扩展后的无效句柄（同 winapi.py 剪贴板处踩过的坑）
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(PROCESSENTRY32W)]


def _process_table() -> dict:
    """{pid: (父pid, 进程名小写)}。一次快照拿全表，约 1-3ms。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return {}
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID,
                                          entry.szExeFile.lower())
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(snap))
    return table


class SessionGate:
    def __init__(self, sessions_dir: Path, mode: str = "claude_only"):
        self.dir = Path(sessions_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self._cache: set = set()       # 承载 Claude Code 的终端 PID
        self._claude: list = []        # 每个 Claude Code 进程的 pid
        self._meta: dict = {}          # pid -> 插件钩子提供的 entry / cwd
        self._stamp = 0.0

    def start(self) -> None:
        """后台每秒重扫一次进程树。

        不放主线程：枚举 400+ 进程约 12ms，在 tkinter 的 33ms 帧循环里每秒
        插一次就是每秒掉一帧，灵动岛的波形会一顿一顿。
        也绝不能放进低级钩子回调 —— Windows 有 LowLevelHooksTimeout，回调
        超时会把钩子静默摘掉，表现为「时好时坏地失灵」。
        """
        def loop():
            while True:
                try:
                    self.refresh(force=True)
                except Exception:
                    pass                   # 扫描失败不该让守护进程停摆
                time.sleep(1.0)

        threading.Thread(target=loop, name="gate", daemon=True).start()

    def refresh(self, force: bool = False) -> None:
        if not force and time.time() - self._stamp < 1.0:
            return
        self._stamp = time.time()

        table = _process_table()
        pids, claude = set(), []
        for pid, (_, name) in table.items():
            if name != "claude.exe":
                continue
            claude.append(pid)
            cur = pid
            for _ in range(MAX_WALK):          # 从 claude.exe 一路走到根
                pids.add(cur)
                parent = table.get(cur, (0, ""))[0]
                if parent <= 4 or parent == cur or parent not in table:
                    break
                cur = parent
        self._cache, self._claude = pids, claude

        # 会话文件只补充进程表里没有的信息（入口名、工作目录），不参与放行
        meta = {}
        for f in self.dir.glob("*.json"):
            try:
                # utf-8-sig：PowerShell 5.1 的 Set-Content -Encoding utf8 会写 BOM，
                # 用 utf-8 解码会抛 JSONDecodeError
                info = json.loads(f.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            pid = int(info.get("claude_pid", 0))
            if pid in table:
                meta[pid] = info
            else:
                f.unlink(missing_ok=True)      # 进程没了，顺手清理
        self._meta = meta

    def is_open(self) -> tuple:
        """返回 (是否放行, 原因)。

        在低级鼠标钩子的回调里被调用，所以只做纯内存操作 —— 一次
        GetForegroundWindow 加一次集合查找。进程树的刷新见 refresh()。
        """
        if self.mode == "always":
            return True, "全局模式"
        if not self._claude:
            return False, "没有正在运行的 Claude Code"
        fg = foreground_pid()
        if fg in self._cache:
            return True, "Claude Code 终端 (pid %d)" % fg
        return False, "前台窗口 pid %d 不是 Claude Code 终端" % fg

    def sessions(self) -> list:
        """给管理面板看的会话列表。"""
        self.refresh(force=True)
        return [{"claude_pid": pid,
                 "entry": self._meta.get(pid, {}).get("entry", "claude"),
                 "cwd": self._meta.get(pid, {}).get("cwd", "")}
                for pid in self._claude]
