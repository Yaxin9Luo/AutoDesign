"""Deterministic language checks for English narration contracts."""

from __future__ import annotations

import re
import unicodedata


_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")


def is_substantially_english(
    value: str,
    *,
    min_words: int = 3,
    min_letters: int = 20,
    min_latin_ratio: float = 0.8,
) -> bool:
    """Return true when prose is predominantly English, not just an acronym."""
    text = unicodedata.normalize("NFKC", " ".join(str(value or "").split()))
    if len(_ENGLISH_WORD_RE.findall(text)) < min_words:
        return False
    alphabetic = [char for char in text if char.isalpha()]
    if len(alphabetic) < min_letters:
        return False
    latin_letters = sum(
        "LATIN" in unicodedata.name(char, "")
        for char in alphabetic
    )
    return latin_letters / len(alphabetic) >= min_latin_ratio
