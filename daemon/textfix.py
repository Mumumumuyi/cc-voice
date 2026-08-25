"""识别后处理：热词强制纠正 + 规则替换。

ASR 对专有名词（Claude、useEffect、pnpm）天然弱，因为训练语料里少见。
两条通道：
  hotwords.txt   一行一个词，按拼音相似度模糊匹配后强制替换 —— 治「音对字错」
  rules.txt      `原文=>替换` 精确替换 —— 治固定口误和标点偏好
"""
import re
from pathlib import Path

_TONE = str.maketrans("āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ", "aaaaeeeeiiiioooouuuuvvvv")


def _pinyin(text: str):
    """无第三方依赖的粗粒度拼音：只对中文用声母韵母近似，英文原样小写。"""
    try:
        from pypinyin import lazy_pinyin
        return [p.translate(_TONE) for p in lazy_pinyin(text)]
    except ImportError:
        return list(text.lower())


def _norm(seq):
    """把易混音归一：zh/z、ch/c、sh/s、n/l、in/ing、en/eng —— 中文 ASR 的主要错源。"""
    out = []
    for p in seq:
        p = re.sub(r"^zh", "z", p)
        p = re.sub(r"^ch", "c", p)
        p = re.sub(r"^sh", "s", p)
        p = re.sub(r"^([bpmfdtnlgkhjqxzcsryw]*)n$", r"\1n", p)
        p = re.sub(r"ing$", "in", p)
        p = re.sub(r"eng$", "en", p)
        p = re.sub(r"^l", "n", p)
        out.append(p)
    return out


def _ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


# SenseVoice / FunASR 会在文本里混入控制标记：语种 <|zh|>、情感 <|HAPPY|>、
# 事件 <|Speech|>、以及无语音 <|nospeech|>。它们不是识别结果，必须剥掉 ——
# 否则会被原样粘进输入框（实测 <|nospeech|> 上过屏）。
_TAG = re.compile(r"<\|[^|>]*\|>")


def strip_tags(text: str) -> str:
    return _TAG.sub("", text or "").strip()


class TextFixer:
    def __init__(self, hotwords_file: Path, rules_file: Path, threshold: float = 0.82):
        self.hotwords_file, self.rules_file = Path(hotwords_file), Path(rules_file)
        self.threshold = threshold
        self.hotwords: list[tuple[str, str]] = []     # (词, 归一化拼音串)
        self.rules: list[tuple[str, str]] = []
        self.reload()

    def reload(self) -> None:
        self.hotwords = []
        if self.hotwords_file.exists():
            for line in self.hotwords_file.read_text(encoding="utf-8").splitlines():
                w = line.split("#")[0].strip()
                if w:
                    self.hotwords.append((w, "".join(_norm(_pinyin(w)))))
        self.rules = []
        if self.rules_file.exists():
            for line in self.rules_file.read_text(encoding="utf-8").splitlines():
                line = line.split("#")[0].strip()
                if "=>" in line:
                    src, dst = line.split("=>", 1)
                    if src.strip():
                        self.rules.append((src.strip(), dst.strip()))

    def apply(self, text: str) -> str:
        text = strip_tags(text)
        if not text:
            return text
        for src, dst in self.rules:
            text = text.replace(src, dst)
        return self._hotword_pass(text)

    def _hotword_pass(self, text: str) -> str:
        """在中文连续片段上滑窗，找与热词拼音高度相似的片段并替换。"""
        if not self.hotwords:
            return text
        for word, want in self.hotwords:
            if word in text:
                continue                              # 已经对了
            n = len(word)
            # 只在等长与 ±1 长度的窗口里找，避免 O(n²) 全扫
            for size in (n, n - 1, n + 1):
                if size < 1 or size > len(text):
                    continue
                for i in range(len(text) - size + 1):
                    chunk = text[i:i + size]
                    if not re.search(r"[\u4e00-\u9fff]", chunk):
                        continue
                    got = "".join(_norm(_pinyin(chunk)))
                    if got and _ratio(got, want) >= self.threshold:
                        text = text[:i] + word + text[i + size:]
                        break
                else:
                    continue
                break
        return text
