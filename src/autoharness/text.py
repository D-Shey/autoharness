"""Word-level text matching shared by task classification and recipe lookup.

No stemmer library (stdlib only), so two cheap rules stand in for one:

- words of STEM letters or more compare by their first STEM letters
  ("documentation" / "documentations", "research" / "researching"), as long as
  the shared start covers all but the last SLACK letters of the keyword and
  the word is at most ENDING letters shorter than it, so a short word is not
  taken for a long keyword that merely starts with it ("light" is not
  "lighthouse", "document" is not "documentation");
- shorter keywords are written as stems in the catalog ("test", "form")
  and match a word that is the stem plus a common ending of any language
  autoharness reads ("tests", "forms"), but not a longer word that merely
  starts with it ("format");
- keywords under three letters ("pr", "ci") match only as whole words.

Stopwords and endings come from the language files (lang.py).
"""

from __future__ import annotations

import os
import re

from autoharness import lang

WORD = re.compile(r"[^\W_]+", re.UNICODE)
STEM = 5
SLACK = 4
ENDING = 2  # letters an inflected word may be shorter than its keyword
STOP = set(lang.listed("stopwords"))
ENDINGS = set(lang.listed("endings")) | {""}


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
        # an ending changes a word by a letter or two; a word much shorter than the keyword is another
        # word that happens to start it ("document" is not "documentation")
        return shared >= max(STEM, len(keyword) - SLACK) and len(word) >= len(keyword) - ENDING
    return word.startswith(keyword) and word[len(keyword):] in ENDINGS


def matches(keyword: str, text_words: list[str]) -> bool:
    """True if every word of `keyword` (one word or a phrase) is in `text_words`."""
    parts = words(keyword)
    return bool(parts) and all(any(_word_matches(p, w) for w in text_words) for p in parts)


def without(text_words: list[str], phrases: list[str]) -> list[str]:
    """`text_words` with every run of consecutive words that matches one of `phrases` taken out, so a
    keyword inside a constraint ("only the standard library") does not count as the task's subject."""
    out, i = [], 0
    parts = [words(p) for p in phrases]
    while i < len(text_words):
        hit = next((len(p) for p in parts if p and all(
            i + j < len(text_words) and _word_matches(w, text_words[i + j]) for j, w in enumerate(p))), 0)
        if hit:
            i += hit
        else:
            out.append(text_words[i])
            i += 1
    return out
