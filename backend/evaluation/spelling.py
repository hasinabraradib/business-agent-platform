"""Suspected Bengali-script misspellings, per 100 Bengali words.

There is no light Bengali spell-checker, so this flags near misses: a word that is not in the
lexicon (evals/data/bn_lexicon.txt) but is one edit away from a lexicon word of three or more
letters (e.g. "দুপুড়" for "দুপুর"). That finds typo-like errors, misses real-word errors and
can flag a valid word missing from the lexicon, so results are "suspected" and listed for
review. Banglish and English are not measured.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from evaluation.dataset import DATA_DIR

BENGALI_WORD = re.compile("[" + chr(0x0980) + "-" + chr(0x09FF) + "]+")
HAS_DIGIT = re.compile("[" + chr(0x09E6) + "-" + chr(0x09EF) + "0-9]")
# Case endings and classifiers, so inflected forms of lexicon words are known words:
# বিরিয়ানির, অর্ডারের, দোকানে, শাড়িগুলো ...
SUFFIXES = sorted(
    ["র", "ের", "এর", "কে", "তে", "য়", "এ", "ে", "টা", "টি", "টার", "টায়", "গুলো", "গুলি",
     "রা", "দের", "ও", "ই"],
    key=len,
    reverse=True,
)  # fmt: skip


# ড় ঢ় য় typed as letter + nukta (two code points) -> one code point, so a wrong letter counts as
# one edit.
NUKTA_FORMS = {
    chr(0x09A1) + chr(0x09BC): chr(0x09DC),
    chr(0x09A2) + chr(0x09BC): chr(0x09DD),
    chr(0x09AF) + chr(0x09BC): chr(0x09DF),
}


def _canonical(word: str) -> str:
    for decomposed, single in NUKTA_FORMS.items():
        word = word.replace(decomposed, single)
    return word


def load_lexicon(path: Path = DATA_DIR / "bn_lexicon.txt") -> set[str]:
    return {
        _canonical(line.strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _within_one_edit(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return any(long[:i] + long[i + 1 :] == short for i in range(len(long)))


def _inflected(word: str, lexicon: set[str]) -> bool:
    return any(
        word.endswith(suffix) and word[: -len(suffix)] in lexicon
        for suffix in SUFFIXES
        if len(word) > len(suffix) + 1
    )


@dataclass
class SpellingReport:
    words: int = 0
    suspected: list[tuple[str, str]] = field(default_factory=list)  # (word, nearest lexicon word)

    @property
    def per_100_words(self) -> float | None:
        return round(100 * len(self.suspected) / self.words, 2) if self.words else None


def check_texts(texts: list[str], lexicon: set[str] | None = None) -> SpellingReport:
    lexicon = {_canonical(w) for w in lexicon} if lexicon is not None else load_lexicon()
    candidates = [w for w in lexicon if len(w) >= 3]
    report = SpellingReport()
    for text in texts:
        for written in BENGALI_WORD.findall(text):
            word = _canonical(written)
            if HAS_DIGIT.search(word):
                continue  # numbers and times such as ৭টা are not words to spell-check
            report.words += 1
            if word in lexicon or _inflected(word, lexicon):
                continue
            nearest = next((c for c in candidates if _within_one_edit(word, c)), None)
            if nearest is not None:
                report.suspected.append((written, nearest))  # as the model wrote it
    return report
