"""cc-voice 守护进程：按住触发键说话，松开把中文上屏到 Claude Code 终端。

线程分工（低级钩子与 tkinter 各自需要不同的事件循环，无法合并）：
  主线程     tkinter mainloop —— 只画 HUD，绝不做阻塞操作
  trigger    Win32 消息泵 + 低级钩子 —— 只判定边沿、塞队列
  worker     录音 / 识别 / 注入 —— 耗时都在这里
  panel      本地管理面板 HTTP 服务
"""
import os

# Windows 的 C 运行时读不懂 TZ=America/New_York 这种 POSIX 写法，会把本地时间
# 算成 UTC+1 —— 实测从带这个变量的终端拉起守护进程，日志时间快了 5 小时。
# 去掉它就回落到 Windows 系统时区，也就是任务栏上的时间。
os.environ.pop("TZ", None)

import argparse
import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio
import config
import inject
import winapi
from gate import SessionGate
from hud import Hud
from textfix import TextFixer
from trigger import Trigger

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
HISTORY = LOGS / "data" / "history.jsonl"   # 面板读的结构化数据
READABLE = LOGS / "识别记录.log"             # 人看的对齐流水


MIN_VOICED_MS = 150     # 人声帧累计不到这么久就不送识别：约一个音节；键盘单击只占一两帧

MUTEX_NAME = "Local\\cc-voice-daemon"   # 必须与 hooks/session-start.ps1 里探测的名字一致
_MUTEX = None


def single_instance(name: str = MUTEX_NAME) -> bool:
    """同一时刻只允许一个守护进程 —— 三个入口都会尝试拉起它。

    用 Local\\ 而不是 Global\\：Global 命名空间需要 SeCreateGlobalPrivilege，
    普通用户会话下未必创建得了。更要紧的是名字必须和钩子里 OpenExisting 的
    完全一致 —— 曾经一边 Global 一边 Local，钩子永远探不到，于是每开一个
    cc 窗口就多起一个守护进程，多个进程抢同一个麦克风、重复注入。

    句柄必须挂在模块级变量上，否则被 GC 回收后互斥量随之释放。
    """
    global _MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _MUTEX = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.status = "启动中"
        self.recognizer = None
        self.rec_error = None
        self._model_gen = 0
        self.started_at = time.time()
        self.stats = {"count": 0, "chars": 0, "last": None}

        LOGS.mkdir(parents=True, exist_ok=True)
        self.gate = SessionGate(ROOT / "sessions", cfg["gate_mode"])
        self.gate.start()                    # 后台每秒重扫进程树，见 gate.start()
        hot = ROOT / "hotwords.txt"               # 个人词表不入库，首次启动从模板复制
        if not hot.exists() and (ROOT / "hotwords.example.txt").exists():
            hot.write_bytes((ROOT / "hotwords.example.txt").read_bytes())
        self.fixer = TextFixer(hot, ROOT / "rules.txt")
        self.recorder = audio.Recorder(device=cfg["audio"]["device"])
        self.cmd_q: queue.Queue = queue.Queue()      # 钩子线程 -> worker
        # 录音和识别分两条线：识别要 0.5~2s，放在 worker 里会让紧接着的下一次按键排队，
        # 那段话的开头就录不进来（实测「只有杂音，人声 20ms」）。识别按顺序串行做，上屏顺序不乱
        self.asr_q: queue.Queue = queue.Queue()      # worker -> 识别线程
        self.ui_q: queue.Queue = queue.Queue()       # worker -> tkinter 主线程
        self._ui_seq = 0                             # 每换一次灵动岛状态 +1，过期的「回到待机」据此作废
        self._log_lock = threading.Lock()            # 两个线程都会记账
        self.record_t0 = 0.0
        self._pending_pos = None
        self.paused = False
        self._quit = False

        winapi.set_dpi_aware()
        self.root = tk.Tk()
        self.root.withdraw()
        self.hud = Hud(self.root, cfg["hud"].get("opacity", 0.95),
                       position=cfg["hud"].get("position"),
                       on_move=self._save_position,
                       on_double_click=self.open_panel)
        if cfg["hud"]["enabled"]:
            self.root.after(200, self._show_idle)

        threading.Thread(target=self._load_model, name="model", daemon=True).start()
        self.trigger = Trigger(cfg, self._gate,
                               lambda: self.cmd_q.put("down"),
                               lambda: self.cmd_q.put("up"),
                               island=self.hud)
        self.trigger.start()
        threading.Thread(target=self._worker, name="worker", daemon=True).start()
        threading.Thread(target=self._asr_worker, name="asr", daemon=True).start()
        self.root.after(30, self._pump)

    def _gate(self):
        """暂停时一律拦截：触发键原样透传给下层窗口，侧键仍是浏览器的前进后退。"""
        if self.paused:
            return False, "已暂停"
        return self.gate.is_open()

    def set_paused(self, on: bool):
        self.paused = self.hud.paused = bool(on)

    def request_quit(self):
        """由面板线程调用：只置标志，真正退出交给主线程 —— 从 HTTP 线程直接
        关 tkinter 会重现之前那个静默崩溃。"""
        self._quit = True

    def _show_idle(self):
        """首次显示灵动岛，并把焦点还给原来的窗口。

        实测首次 ShowWindow 会让本进程拿到前台（之后就不会再抢了）。不还回去
        的话，用户正在打字的窗口会突然失焦。
        """
        before = winapi.user32.GetForegroundWindow()
        self.hud.show("idle")
        if before and winapi.user32.GetForegroundWindow() != before:
            winapi.user32.SetForegroundWindow(before)

    # -------------------------------------------------------------- 模型
    def reload_model(self):
        """面板换模型/热词后热切换。旧识别器留给正在进行的请求，由 GC 回收。

        面板一次保存会连发设置和热词两个请求，可能先后触发两次重载。只认最后一次：
        否则先起、后完成的那次会用旧热词覆盖掉新的。"""
        self._model_gen += 1
        self.recognizer, self.rec_error = None, None
        threading.Thread(target=self._load_model, args=(self._model_gen,),
                         name="model", daemon=True).start()

    def _load_model(self, gen: int = 0):
        from asr import Recognizer
        try:
            self.status = "加载模型"
            rec = Recognizer(ROOT / "models" / self.cfg["model"],
                             num_threads=self.cfg["num_threads"],
                             language=self.cfg["language"],
                             hotwords=self.fixer.words)   # LLM 模型把热词写进提示词
            if gen != self._model_gen:
                return                             # 加载期间又被重载过：这份设置已过期
            self.recognizer = rec
            self.status = "就绪"
        except Exception as e:                     # 模型缺失/损坏要在面板里看得见
            if gen == self._model_gen:
                self.rec_error = f"{type(e).__name__}: {e}"
                self.status = "模型加载失败"

    # ------------------------------------------------------- 录音/识别
    def _worker(self):
        while True:
            cmd = self.cmd_q.get()
            try:
                if cmd == "down":
                    self._begin()
                elif cmd == "up":
                    self._finish()
            except Exception as e:
                self._fail(e)

    def _asr_worker(self):
        while True:
            job = self.asr_q.get()
            try:
                self._recognize(*job)
            except Exception as e:
                self._fail(e)

    def _fail(self, e: Exception):
        # 出错也要落进识别记录：以前只在灵动岛上闪一句「出错：PortAudioError」，
        # 日志一片空白，事后根本查不到按键为什么没反应
        if isinstance(e, audio.sd.PortAudioError) and audio.mic_denied_by_privacy():
            short = "麦克风被系统禁用"
            why = "麦克风被系统禁用（设置 › 隐私和安全性 › 麦克风 › 允许桌面应用访问你的麦克风）"
        else:
            short = f"出错：{type(e).__name__}"
            why = f"出错：{type(e).__name__}: " + " ".join(str(e).split())   # 流水日志按行对齐
        self._ui("blocked", short)
        try:
            self._record(0.0, 0.0, skipped=why, error=True)
        except OSError:
            pass                           # 这里再抛就出了 except，线程直接死掉

    def _begin(self):
        if self.recognizer is None:
            self._ui("blocked", self.rec_error and "模型加载失败" or "模型加载中…")
            return
        self.record_t0 = time.time()
        self.recorder.start()
        self._ui("listening", "0:00")

    def _finish(self):
        if not self.recorder.recording:
            return
        samples = self.recorder.stop()
        dur = len(samples) / audio.SAMPLE_RATE
        peak = float(np.abs(samples).max()) if samples.size else 0.0

        # 每一次尝试都记账，包括被拦下的。按了没反应时如果什么痕迹都不留，
        # 用户无从判断是没触发、太短、太轻，还是没识别出内容。
        if dur * 1000 < self.cfg["min_duration_ms"]:
            self._record(dur, peak, skipped="太短了，没听清")
            self._ui("blocked", "太短了，没听清", hide_after=900)
            return
        if not samples.any():
            # 真在收音的麦克风总有底噪，整段一个非零样本都没有 = 设备没给声音
            self._record(dur, 0.0, skipped="麦克风没有声音（录到的全是 0：可能被静音、"
                                           "被别的程序独占，或需要重开语音输入）")
            self._ui("blocked", "麦克风没有声音", hide_after=1500)
            return
        if peak < self.cfg["min_level"]:
            self._record(dur, peak, skipped="没听到说话（音量 %.3f）" % peak)
            self._ui("blocked", "没听到说话", hide_after=1100)
            return
        # 人声帧的 RMS 大约只有整段峰值的 1/10：用 /4 时，峰值 0.02~0.03 的真实句子会因
        # 凑不够人声帧被拦；/8 在 TTS 测试里 0.02~0.06 全部放行，稳态噪声和单击仍拦得住
        voiced, a, b = audio.speech_span(samples, self.cfg["min_level"] / 8)
        if voiced < MIN_VOICED_MS:
            self._record(dur, peak, skipped="只有杂音，没听到说话（人声 %dms）" % voiced)
            self._ui("blocked", "没听到说话", hide_after=1100)
            return

        self._ui("thinking")
        self.asr_q.put((samples[a:b], dur, peak))           # 切掉首尾静音再识别

    def _recognize(self, samples, dur, peak):
        rec = self.recognizer
        if rec is None:                                     # 排队期间面板触发了模型重载
            self._record(dur, peak, skipped="模型重新加载中，这句没识别")
            self._ui("blocked", "模型加载中…", hide_after=1100)
            return
        raw, ms = rec.transcribe(samples)
        text = self.fixer.apply(raw)
        if not text:
            self._record(dur, peak, raw=raw, ms=ms, skipped="没有识别到内容")
            self._ui("blocked", "没有识别到内容", hide_after=900)
            return

        inject.paste_text(text, self.cfg["inject"]["method"],
                          self.cfg["inject"]["restore_clipboard_ms"],
                          self.cfg["inject"]["auto_enter"])
        self._record(dur, peak, raw=raw, text=text, ms=ms)
        self._ui("done", text, hide_after=self.cfg["hud"]["done_ms"])

    def _record(self, *a, **kw):
        with self._log_lock:                   # worker 和识别线程都会记账，别让两行交错
            self._write_record(*a, **kw)

    def _write_record(self, dur, peak, raw="", text="", ms=0.0, skipped="", error=False):
        """写两份：给面板的结构化数据，和给人扫的对齐流水。

        两种读者要的东西不一样 —— 面板要能解析，人要能一眼看出哪条没上屏、
        为什么。把它们塞进同一个格式，结果就是两边都难受。
        """
        if not skipped:
            self.stats["count"] += 1
            self.stats["chars"] += len(text)
            self.stats["last"] = text

        now = datetime.now()
        row = {"ts": now.isoformat(timespec="seconds"),
               "raw": raw, "text": text, "skipped": skipped,
               "audio_s": round(dur, 2), "peak": round(peak, 3),
               "infer_ms": round(ms * 1000)}
        if error:
            row["error"] = True
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + chr(10))

        if not READABLE.exists():
            # 表头按数据行的实际列位对齐；中文字符在等宽字体里占 2 个显示宽度，
            # 所以空格数不等于字符数差
            header = ["cc-voice 识别记录    √ = 已上屏    · = 未上屏    × = 出错",
                      "",
                      "日期   时间      时长    音量   耗时      结果",
                      "-" * 76]
            READABLE.write_text("\n".join(header) + "\n", encoding="utf-8")

        mark, body = ("×" if error else "·", skipped) if skipped else ("√", text)
        if raw and raw != text and not skipped:
            body += "        原始: " + raw          # 热词纠正前后不一致时才显示
        line = "%s  %s  %5.1fs  %5.3f  %7s   %s %s" % (
            now.strftime("%m-%d"), now.strftime("%H:%M:%S"), dur, peak,
            ("%dms" % round(ms * 1000)) if ms else "-", mark, body)
        with READABLE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ----------------------------------------------------------- HUD 驱动
    # tkinter/Tcl 不是线程安全的：从 worker 或钩子线程调 root.after() 会破坏
    # 解释器状态，表现为进程凭空消失且没有 traceback（实测踩过）。所有跨线程
    # 的 UI 动作一律经队列，由主线程的 _pump 排空。
    def _ui(self, state, text="", hide_after=0):
        self.ui_q.put((state, text, hide_after))

    def _hide_later(self, ms):
        """回到待机态而不是消失 —— 灵动岛常驻，红灯表示在候命。"""
        self.ui_q.put((None, "", ms))

    def _save_position(self, origin):
        """由钩子线程调用：只记下待办，落盘交给主线程 —— 低级钩子回调里
        做文件 I/O 会拖慢整个系统的输入响应。"""
        self._pending_pos = list(origin)

    def _back_to_idle(self, seq):
        """「完成」提示到点回待机 —— 期间状态换过（比如又开始录音了）就作废，
        以前这里不管三七二十一切回红灯，把正在录音的灵动岛打断了。"""
        if seq == self._ui_seq and not self.recorder.recording:
            self.hud.show(self._idle_state())

    def _idle_state(self):
        return "idle" if self.cfg["hud"]["enabled"] else "hidden"

    def open_panel(self):
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{self.cfg['panel_port']}/")

    def _pump(self):
        """tkinter 侧的每帧工作：排空 UI 队列、喂波形、刷计时、落盘位置。"""
        while True:
            try:
                state, text, hide_after = self.ui_q.get_nowait()
            except queue.Empty:
                break
            if state is not None:
                # 正在录音时只认「聆听」：上一句的识别结果照常上屏，但不能把录音中的
                # 灵动岛切成「完成」
                if self.recorder.recording and state != "listening":
                    continue
                self._ui_seq += 1
                self.hud.show(state, text)
            if hide_after:
                self.root.after(hide_after, self._back_to_idle, self._ui_seq)

        if self.hud.state == "listening":
            self.hud.set_level(self.recorder.level)
            el = time.time() - self.record_t0
            self.hud.text = "%d:%02d" % (int(el // 60), int(el % 60))

        if self._quit:
            self.root.quit()
            return
        if self._pending_pos is not None:
            self.cfg["hud"]["position"], self._pending_pos = self._pending_pos, None
            config.save(self.cfg)
        self.root.after(33, self._pump)

    def run(self):
        self.root.mainloop()


def probe(seconds: int = 20):
    """探测模式：打印你按下的每一个鼠标/键盘事件，用来确认侧键是否存在。"""
    print(f"请在 {seconds} 秒内依次按：鼠标侧键（两个都试）、中键、右 Ctrl。Ctrl+C 提前结束。\n")
    seen = set()

    def log(msg):
        if msg not in seen:
            seen.add(msg)
            print(" ", msg, flush=True)

    cfg = config.load()
    cfg["trigger"] = {"mouse_button": "none", "key": "none", "key_enabled": True}
    t = Trigger(cfg, lambda: (False, "probe"), lambda: None, lambda: None, probe=log)
    t.start()
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    t.stop()
    print("\n看到 'mouse x2 down' = 侧键可用；只看到 'mouse x1' 就把配置改成 x1；"
          "两个都没有说明这只鼠标没有侧键，用右 Ctrl 触发。")


def main():
    ap = argparse.ArgumentParser(prog="cc-voice")
    ap.add_argument("--probe", action="store_true", help="探测鼠标/键盘触发键")
    ap.add_argument("--probe-seconds", type=int, default=20)
    args = ap.parse_args()

    winapi.set_dpi_aware()
    if args.probe:
        probe(args.probe_seconds)
        return
    if not single_instance():
        print("cc-voice 已在运行")
        return

    cfg = config.load()
    app = App(cfg)
    import panel
    panel.serve(app, cfg["panel_port"])
    app.run()


if __name__ == "__main__":
    main()
