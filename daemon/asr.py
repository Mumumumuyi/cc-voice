"""sherpa-onnx 离线识别封装。

只做一件事：float32 波形 -> 中文文本。按目录里的文件认模型类型，不按目录名：
  sense_voice   model*.onnx + tokens.txt          纯声学 CTC，快（~0.1s），但没有语言模型，
                                                  「难点/兰点」「重构/重购」这类近音只能靠后处理
  funasr_nano   encoder_adaptor + llm + embedding  带 Qwen3-0.6B 解码器，能结合上下文，
                                                  热词直接写进提示词参与解码
  qwen3_asr     conv_frontend + encoder + decoder  同上，Qwen3-ASR
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


def _find(root: Path, stem: str):
    """root 下以 stem 开头的 .onnx，有 int8 版就用 int8。"""
    cands = sorted(root.glob(f"{stem}*.onnx"))
    int8 = [p for p in cands if "int8" in p.name]
    return (int8 or cands or [None])[0]


def kind_of(model_dir: Path) -> str:
    if _find(model_dir, "encoder_adaptor"):
        return "funasr_nano"
    if _find(model_dir, "conv_frontend"):
        return "qwen3_asr"
    return "sense_voice"


# 配置里的语种代码 -> 各模型要的写法。auto 一律留空，让模型自己判断（中英混说时更准）
_LANG = {"funasr_nano": {"zh": "中文", "en": "英文", "yue": "粤语"},
         "qwen3_asr": {"zh": "Chinese", "en": "English", "yue": "Cantonese"}}


class Recognizer:
    def __init__(self, model_dir, num_threads: int = 4, language: str = "auto",
                 use_itn: bool = True, hotwords=()):
        self.model_dir = d = Path(model_dir)
        self.kind = kind_of(d)
        # 两种 LLM 模型都按逗号/分号/换行切热词，词里本身带这些符号的只能丢掉
        hw = ",".join(w for w in hotwords if not any(c in w for c in ",;，；"))
        lang = _LANG.get(self.kind, {}).get(language, "")
        self._stream_lang = lang if self.kind == "qwen3_asr" else ""
        t0 = time.time()
        if self.kind == "funasr_nano":
            tok = next(d.rglob("vocab.json")).parent
            self.rec = sherpa_onnx.OfflineRecognizer.from_funasr_nano(
                encoder_adaptor=str(_find(d, "encoder_adaptor")),
                llm=str(_find(d, "llm")),
                embedding=str(_find(d, "embedding")),
                tokenizer=str(tok),
                num_threads=num_threads,
                language=lang,
                itn=use_itn,
                hotwords=hw,
            )
        elif self.kind == "qwen3_asr":
            tok = next(d.rglob("vocab.json")).parent
            self.rec = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
                conv_frontend=str(_find(d, "conv_frontend")),
                encoder=str(_find(d, "encoder")),
                decoder=str(_find(d, "decoder")),
                tokenizer=str(tok),
                num_threads=num_threads,
                hotwords=hw,
            )
        else:
            model, tokens = _pick(d)
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
        # LLM 模型的上下文只有 512 token（提示词 + 音频 + 输出共用）。实测 22 秒起音频被
        # 截掉一半、24 秒起直接返回空串 —— 长录音必须先切段
        parts = [samples] if self.kind == "sense_voice" else _split(samples, sample_rate)
        texts = []
        for part in parts:
            stream = self.rec.create_stream()
            if self._stream_lang:
                stream.set_option("language", self._stream_lang)
            stream.accept_waveform(sample_rate, part)
            self.rec.decode_stream(stream)
            texts.append(stream.result.text.strip())
        return "".join(texts), time.time() - t0


def _split(x: np.ndarray, sr: int, max_s: float = 10.0, min_s: float = 6.0) -> list:
    """超过 max_s 就在 [min_s, max_s] 区间里能量最低的 20ms 处下刀 —— 字与字之间的停顿，
    不会把一个字切成两半。10 秒 ≈ 230 个音频 token，给热词提示词和输出各留出余量。"""
    out, i, hop = [], 0, sr // 50
    while x.size - i > max_s * sr:
        lo, hi = i + int(min_s * sr), i + int(max_s * sr)
        k = (hi - lo) // hop
        e = (x[lo:lo + k * hop].reshape(k, hop) ** 2).mean(axis=1)
        cut = lo + int(np.argmin(e)) * hop + hop // 2
        out.append(x[i:cut])
        i = cut
    out.append(x[i:])
    return out
