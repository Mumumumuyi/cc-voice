"""配置读写。改动即时落盘，管理面板和守护进程共用同一份。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"

DEFAULTS = {
    # funasr-nano-llm 带 Qwen3-0.6B 解码器：能结合上下文、热词直接参与解码。
    # 2026-09-27 本机 TTS 基准（105 句，含热词和模糊音纠错）字错率 1.36%，纯声学的
    # funasr-nano 是 7.3%；
    # 代价是每句多等约 1~3 秒、常驻内存约 1.1GB。要快就在面板切回 funasr-nano
    "model": "funasr-nano-llm",
    "num_threads": 4,
    "language": "auto",              # 中英混说时 auto 比钉死 zh 更准
    "trigger": {
        "mouse_button": "x2",        # x2 / x1 / middle / none
        "key": "rctrl",              # rctrl / rshift / ralt / capslock / none
        "key_enabled": True,         # 鼠标没侧键时的兜底，实测可用后可关
    },
    "gate_mode": "claude_only",      # claude_only / always
    "inject": {
        "method": "ctrl_v",          # ctrl_v / shift_insert
        "auto_enter": False,         # 识别完是否直接回车发送
        "restore_clipboard_ms": 400,
    },
    "hud": {
        "enabled": True,             # 关掉则灵动岛完全不显示
        "done_ms": 1800,             # 展示识别结果多久后收回待机态
        "opacity": 0.95,
        "position": None,            # 拖动后记住的屏幕坐标，null = 底部居中
    },
    "audio": {"device": None},
    "min_duration_ms": 300,          # 低于此时长视为误触，不送识别
    # ASR 模型没有「静音」这个输出：喂它底噪，它一定会硬猜出几个字并上屏
    # （实测 3.9s 环境噪声被识别成「就现来个的对什对后」）。静音判定必须在
    # 送进模型之前做：先看峰值过不过这道底线，再看「比本段底噪高 10dB 的人声帧」
    # 够不够 150ms（audio.speech_span）。后一道是相对量，不怕麦克风整体变小声 ——
    # 旧的 0.045 单阈值在换麦后把峰值 0.042 的几秒钟真实说话都扔了。
    "min_level": 0.015,              # 峰值底线：你的安静房间 ≈0.0004，最轻的真实说话 ≈0.028
    "panel_port": 8731,
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load() -> dict:
    if CONFIG_FILE.exists():
        try:
            return _merge(DEFAULTS, json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass                      # 配置损坏时退回默认值，别让守护进程起不来
    return dict(DEFAULTS)


def save(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
