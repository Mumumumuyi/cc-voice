"""识别后处理：规则替换 + 热词模糊纠正。

ASR 对专有名词（Claude Code、GitHub、项目代号）天然弱，口音还会让 n/l、平翘舌、前后鼻音
串音（「难点」听成「兰点」、「数据库」听成「数据酷」）。两条通道：
  rules.txt      `原文=>替换` 精确替换，最先执行 —— 治固定口误和标点偏好
  hotwords.txt   一行一个词；按「字对字」的模糊音比对，把读音相同/相近的片段改成热词
                 —— 治「音对字错」。LLM 模型还会把这张表直接写进提示词（见 asr.py）

判定原则是宁可漏改、不可错改：
  * 窗口与热词等长、逐字比对，不做 ±1 字的滑窗（那会把「重新构」吞成「重构」）
  * 纯中文热词：窗口本身是常用词就不动 —— 「借口」不改成「接口」、「分子」不改成「分支」。
    只改「重购」「兰点」这种不成词的串
  * 中英混合热词（如「AI 模型」）靠英文部分锚定上下文，才允许改掉「AI 模星」
  * 英文词只修大小写，或一字之差且长度 ≥4（githob → github）；是前缀关系的不改（agents ≠ agent）
"""
import importlib.util
import os
import re
from functools import lru_cache
from pathlib import Path

_TONE = str.maketrans("āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ", "aaaaeeeeiiiioooouuuuvvvv")
_UNIT = re.compile(r"[A-Za-z0-9]+|[一-鿿]")

try:
    from pypinyin import lazy_pinyin
except ImportError:                                  # 缺了就降级为只修大小写，并在面板上报警
    lazy_pinyin = None


def _is_han(s: str) -> bool:
    return "一" <= s[0] <= "鿿"


def _fuzzy(py: str) -> str:
    """把一个音节归到模糊音等价类：zh/z、ch/c、sh/s、n/l、以及 -ng/-n 前后鼻音不分。"""
    m = re.match(r"(zh|ch|sh|[bpmfdtnlgkhjqxrzcsyw]?)(.*)", py)
    ini, fin = m.group(1), m.group(2)
    ini = {"zh": "z", "ch": "c", "sh": "s", "n": "l"}.get(ini, ini)
    if fin.endswith("ng"):                           # ang/eng/ing/iang/uang/ong -> 去 g
        fin = fin[:-1]
    return ini + fin


@lru_cache(maxsize=4096)
def _keys(han: str) -> tuple:
    """一串汉字 -> 逐字模糊音。按整串取拼音，让多音字按词读（「重构」「银行」）。"""
    return tuple(_fuzzy(p.translate(_TONE)) for p in lazy_pinyin(han))


def _lat_close(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    if min(len(a), len(b)) < 4 or a.startswith(b) or b.startswith(a):
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if abs(len(a) - len(b)) != 1:
        return False
    s, l = (a, b) if len(a) < len(b) else (b, a)
    return any(l[:i] + l[i + 1:] == s for i in range(len(l)))


def _load_lexicon() -> frozenset:
    """jieba 词典里频次 ≥3 的词，用来判断「这个窗口本身是不是个常用词」。只读词表，不做分词。"""
    spec = importlib.util.find_spec("jieba")
    if not spec or not spec.submodule_search_locations:
        return frozenset()
    p = os.path.join(spec.submodule_search_locations[0], "dict.txt")
    words = set()
    with open(p, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) >= 2 and int(parts[1]) >= 3:
                words.add(parts[0])
    return frozenset(words)


# SenseVoice / FunASR 会在文本里混入控制标记：语种 <|zh|>、情感 <|HAPPY|>、
# 事件 <|Speech|>、以及无语音 <|nospeech|>。它们不是识别结果，必须剥掉 ——
# 否则会被原样粘进输入框（实测 <|nospeech|> 上过屏）。
# FunASR-Nano 的 LLM 版对非人声（打字、呼吸）输出的是 /sil，同理。
_TAG = re.compile(r"<\|[^|>]*\|>|/sil\b")


def strip_tags(text: str) -> str:
    return _TAG.sub("", text or "").strip()


class _Hot:
    def __init__(self, word: str):
        self.word = word
        self.units = [m.group() for m in _UNIT.finditer(word)]
        self.han = [_is_han(u) for u in self.units]
        han = "".join(u for u in self.units if _is_han(u))
        self.keys = _keys(han) if han and lazy_pinyin else ()


class TextFixer:
    _lexicon = None                                  # 进程内共享，重载热词时不重读词典

    def __init__(self, hotwords_file: Path, rules_file: Path):
        self.hotwords_file, self.rules_file = Path(hotwords_file), Path(rules_file)
        self.words: list[str] = []                   # 原样的热词表，交给 LLM 模型
        self.hot: list[_Hot] = []
        self.rules: list[tuple[str, str]] = []
        self.warning = None
        if TextFixer._lexicon is None:
            TextFixer._lexicon = _load_lexicon()
        self.reload()

    def reload(self) -> None:
        self.words = []
        if self.hotwords_file.exists():
            for line in self.hotwords_file.read_text(encoding="utf-8").splitlines():
                w = line.split("#")[0].strip()
                if w and w not in self.words:
                    self.words.append(w)
        # 长的先匹配：「Claude Code」要先于「Claude」
        self.hot = sorted((_Hot(w) for w in self.words if _UNIT.search(w)),
                          key=lambda h: -len(h.units))
        self.rules = []
        if self.rules_file.exists():
            for line in self.rules_file.read_text(encoding="utf-8").splitlines():
                line = line.split("#")[0].strip()
                if "=>" in line:
                    src, dst = line.split("=>", 1)
                    if src.strip():
                        self.rules.append((src.strip(), dst.strip()))
        missing = [n for n, ok in (("pypinyin", lazy_pinyin), ("jieba 词典", TextFixer._lexicon)) if not ok]
        self.warning = ("热词模糊纠错不可用：缺少 " + "、".join(missing) +
                        "（在 .venv 里 uv pip install pypinyin jieba）") if missing else None

    def apply(self, text: str) -> str:
        text = strip_tags(text)
        if not text:
            return text
        for src, dst in self.rules:
            text = text.replace(src, dst)
        return self._hotword_pass(text)

    def _hotword_pass(self, text: str) -> str:
        units = [(m.start(), m.end(), m.group()) for m in _UNIT.finditer(text)]
        taken = [False] * len(units)
        edits = {}                                   # 单元下标 -> 新写法
        for h in self.hot:
            k = len(h.units)
            for i in range(len(units) - k + 1):
                if any(taken[i:i + k]):
                    continue
                win = units[i:i + k]
                # 单元之间只允许空白：隔着标点就不是同一个词
                if any(text[win[j][1]:win[j + 1][0]].strip() for j in range(k - 1)):
                    continue
                surf = [u[2] for u in win]
                if [_is_han(s) for s in surf] != h.han:
                    continue
                if all(s == t if hn else s.lower() == t.lower()
                       for s, t, hn in zip(surf, h.units, h.han)):
                    pass                             # 字都对，下面只会改英文大小写
                elif not self._fuzzy_match(surf, h):
                    continue
                for j in range(k):
                    taken[i + j] = True
                    if surf[j] != h.units[j]:
                        edits[i + j] = h.units[j]
        for idx in sorted(edits, reverse=True):
            s, e, _ = units[idx]
            text = text[:s] + edits[idx] + text[e:]
        return text

    def _fuzzy_match(self, surf: list, h: "_Hot") -> bool:
        if not all(_lat_close(s, t) for s, t, hn in zip(surf, h.units, h.han) if not hn):
            return False
        han = "".join(s for s, hn in zip(surf, h.han) if hn)
        if han:
            if not (lazy_pinyin and TextFixer._lexicon) or _keys(han) != h.keys:
                return False
            if all(h.han) and han in TextFixer._lexicon:
                return False                         # 窗口本身就是常用词：多半是用户真这么说的
        return True
