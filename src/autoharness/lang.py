"""The words of each language autoharness reads a task in, kept out of the code.

Every `data/lang/<code>.json` that ships with autoharness, and every `lang/<code>.json` under the user's
autoharness home (`~/.autoharness`, or AUTOHARNESS_HOME), adds to one merged set:

- `keywords` / `exclude`: per catalog capability, words that point to it and phrases whose words do not
  count (the catalog itself holds the English ones);
- `stopwords`: words too common to search for;
- `endings`: endings a short keyword stem may take ("test" + "s");
- `no_constraints`: the ways to say the user set no constraints;
- `dependency_headings`: headings a SKILL.md lists its dependencies under.

Adding a language is adding a file; no code changes.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources
from pathlib import Path

LISTS = ("stopwords", "endings", "no_constraints", "dependency_headings")
PER_CAPABILITY = ("keywords", "exclude")


def _files() -> list[str]:
    shipped = resources.files("autoharness").joinpath("data/lang")
    texts = [f.read_text("utf-8") for f in sorted(shipped.iterdir(), key=lambda f: f.name)
             if f.name.endswith(".json")]
    home = Path(os.environ.get("AUTOHARNESS_HOME") or Path.home() / ".autoharness") / "lang"
    if home.is_dir():
        texts += [f.read_text("utf-8") for f in sorted(home.glob("*.json"))]
    return texts


@lru_cache(maxsize=None)
def _merged(home: str) -> dict:
    merged: dict = {k: [] for k in LISTS} | {k: {} for k in PER_CAPABILITY}
    for raw in _files():
        data = json.loads(raw)
        for key in LISTS:
            merged[key] += [w for w in data.get(key, []) if w not in merged[key]]
        for key in PER_CAPABILITY:
            for cap, words in data.get(key, {}).items():
                merged[key].setdefault(cap, []).extend(w for w in words if w not in merged[key].get(cap, []))
    return merged


def words() -> dict:
    """Every language's words, merged (cached per autoharness home)."""
    return _merged(os.environ.get("AUTOHARNESS_HOME", ""))


def listed(key: str) -> list[str]:
    return list(words()[key])


def for_capability(key: str, capability: str) -> list[str]:
    return list(words()[key].get(capability, []))
