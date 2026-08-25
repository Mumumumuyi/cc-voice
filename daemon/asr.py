"""sherpa-onnx 离线识别封装。

只做一件事：float32 波形 -> 中文文本。模型目录自适应（SenseVoice / FunASR-Nano
的文件名不同），避免把某一版模型的布局硬编码进来。
"""
import time
from pathlib import Path

import numpy as np
import sherpa_onnx


def _pick(model_dir: Path, prefer_int8: bool = True):
    """在模型目录里挑出 (模型文件, tokens 文件)。"""
    tokens = model_dir / "tokens.txt"
    if not tokens.exists():
        found = list(model_dir.rglob("tokens.txt"))
        if not found:
            raise FileNotFoundError(f"{model_dir} 下找不到 tokens.txt")
        tokens = found[0]
    root = tokens.parent
    onnx = sorted(root.glob("*.onnx"))
    if not onnx:
        raise FileNotFoundError(f"{root} 下找不到 .onnx 模型")
    int8 = [p for p in onnx if "int8" in p.name]
    if prefer_int8 and int8:
        return int8[0], tokens
    plain = [p for p in onnx if "int8" not in p.name]
    return (plain or onnx)[0], tokens


class Recognizer:
    def __init__(self, model_dir, num_threads: int = 4, language: str = "auto",
                 use_itn: bool = True):
        self.model_dir = Path(model_dir)
        model, tokens = _pick(self.model_dir)
        self.model_path = model
        t0 = time.time()
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model),
            tokens=str(tokens),
            num_threads=num_threads,
            use_itn=use_itn,          # 逆文本归一化：把「二零二六年」变成「2026年」并补标点
            language=language,        # auto 让模型自己判中/英/粤，中英混说时比钉死 zh 更准
            debug=False,
        )
        self.load_seconds = time.time() - t0

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> tuple[str, float]:
        """返回 (文本, 推理耗时秒)。samples 为 float32、范围 [-1, 1]。"""
        if samples.size == 0:
            return "", 0.0
        t0 = time.time()
        stream = self.rec.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self.rec.decode_stream(stream)
        return stream.result.text.strip(), time.time() - t0
