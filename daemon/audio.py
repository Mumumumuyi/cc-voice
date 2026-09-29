"""麦克风采集：按下开始、松开结束的定长录音。

回调线程只做 append，不做任何重活 —— sounddevice 的回调超时会导致丢帧。
音量包络单独抽出来给 HUD 画波形。
"""
import threading

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000


class Recorder:
    def __init__(self, device=None, sample_rate: int = SAMPLE_RATE):
        self.device = device
        self.sample_rate = sample_rate
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self.level = 0.0                      # 0..1，HUD 每帧读它画波形

    def _cb(self, indata, frames, time_info, status):
        block = indata[:, 0].copy()
        with self._lock:
            self._chunks.append(block)
        peak = float(np.abs(block).max())
        # 慢降快升：让波形跟得上说话、又不会一停就塌成直线
        self.level = peak if peak > self.level else self.level * 0.75 + peak * 0.25

    def start(self) -> None:
        if self._stream is not None:
            return
        with self._lock:
            self._chunks = []
        self.level = 0.0
        self._stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32",
            blocksize=512, device=self.device, callback=self._cb,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream is None:
            return np.zeros(0, dtype=np.float32)
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self.level = 0.0
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    @property
    def recording(self) -> bool:
        return self._stream is not None


def speech_span(x: np.ndarray, floor: float, sr: int = SAMPLE_RATE,
                frame_ms: int = 20) -> tuple[int, int, int]:
    """(人声毫秒数, 起点, 终点)：这段录音里有多少「人声帧」，以及切掉首尾静音后的范围。

    旧门控只看整段峰值，可峰值随麦克风增益、离麦远近整体漂移 —— 换麦之后，几秒钟的
    真实说话因为峰值 0.042 < 0.045 被扔掉。这里看相对量：以本段最安静的 10% 帧当底噪，
    比底噪高约 10dB 且 RMS 过 floor 的帧才算人声。整体变小声时两者同比缩小，比值不变；
    风扇这类稳态噪声没有起伏，一帧都过不了；键盘单击只占一两帧，凑不够时长。
    """
    n = sr * frame_ms // 1000
    k = x.size // n
    if k == 0:
        return 0, 0, x.size
    rms = np.sqrt((x[:k * n].reshape(k, n) ** 2).mean(axis=1))
    active = np.flatnonzero(rms > max(float(np.percentile(rms, 10)) * 3.0, floor))
    if active.size == 0:
        return 0, 0, x.size
    start = max(0, int(active[0]) * n - sr // 4)                # 前留 250ms：清辅音起头能量低
    end = min(x.size, (int(active[-1]) + 1) * n + sr * 2 // 5)  # 后留 400ms：尾音会拖长
    return int(active.size) * frame_ms, start, end


def mic_denied_by_privacy() -> bool:
    """Windows 麦克风隐私开关被关掉时，PortAudio 只报一句
    'Unanticipated host error [MME error 1]'，看不出原因 —— 直接读系统的同意开关。

    三级开关任一为 Deny 都打不开：整机（HKLM）、本用户的应用、本用户的桌面应用
    （NonPackaged，本项目的 pythonw 属于这一类）。"""
    import winreg
    key = (r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
           r"\ConsentStore\microphone")
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, key), (winreg.HKEY_CURRENT_USER, key),
                      (winreg.HKEY_CURRENT_USER, key + r"\NonPackaged")):
        try:
            with winreg.OpenKey(hive, sub) as k:
                if winreg.QueryValueEx(k, "Value")[0] == "Deny":
                    return True
        except OSError:
            pass
    return False


def list_devices() -> list[dict]:
    out = []
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append({"index": i, "name": d["name"],
                        "channels": d["max_input_channels"],
                        "default": i == default_in})
    return out
