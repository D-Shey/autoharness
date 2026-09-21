"""Word-level text matching shared by task classification and recipe lookup.

No stemmer library (stdlib only), so two cheap rules stand in for one:

- words of STEM letters or more compare by their first STEM letters
  ("документация" / "документацию", "исследуй" / "исследовать"), as long as
  the shared start covers all but the last SLACK letters of the keyword, so a
  short word is not taken for a long keyword that merely starts with it
  ("light" is not "lighthouse");
- shorter keywords are written as stems in the catalog ("баз", "тест", "форм")
  and match a word that is the stem plus a common English or Russian ending
  ("базе", "тесты", "forms"), but not a longer word that merely starts with it
  ("формат", "вебинар", "format");
- keywords under three letters ("pr", "бд", "ci") match only as whole words.
"""

from __future__ import annotations

import os
import re

WORD = re.compile(r"[^\W_]+", re.UNICODE)
STEM = 5
SLACK = 4
STOP = {"the", "and", "for", "with", "from", "into", "this", "that",
        "для", "как", "что", "это", "или", "при", "без", "все", "под"}
ENDINGS = {"", "s", "es", "ed", "ing", "er", "ers",
           "а", "я", "у", "ю", "е", "и", "ы", "ой", "ей", "ом", "ем", "ам", "ям", "ах", "ях",
           "ов", "ев", "ами", "ями", "ет", "ют", "ит", "ят", "ут"}


def words(text: str) -> list[str]:
    return [w.lower() for w in WORD.findall(text)]


def stems(text: str) -> set[str]:
    """Stemmed content words, for overlap scores."""
    return {w[:STEM] for w in words(text) if len(w) >= 3 and w not in STOP}


def _word_matches(keyword: str, word: str) -> bool:
    if len(keyword) < 3:
        return word == keyword
    if len(keyword) >= STEM:
        shared = len(os.path.commonprefix([keyword, word]))
        return shared >= max(STEM, len(keyword) - SLACK)
    return word.startswith(keyword) and word[len(keyword):] in ENDINGS


def matches(keyword: str, text_words: list[str]) -> bool:
    """True if every word of `keyword` (one word or a phrase) is in `text_words`."""
    parts = words(keyword)
    return bool(parts) and all(any(_word_matches(p, w) for w in text_words) for p in parts)
