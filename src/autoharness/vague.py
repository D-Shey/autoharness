"""Items that say nothing a reader could act on: a brief's `create <name>` that only restates its area, a lesson or a
skill step that leans on "carefully" or "best practice" and names nothing to find or run. Warnings, never refusals:
a naive linter (write-good flags weasel words the same way, https://github.com/btford/write-good) is wrong often
enough that a refusal on a normal item would cost more than the check is worth."""
import re

# a short, fixed list: each word added is another source of false alarms
VAGUE = re.compile(
    r"(?<![\w-])(?:carefully|careful|properly|correctly|cleanly|best[ -]practices?|clean code|(?:high|good)[ -]quality"
    r"|аккуратн\w*|правильн\w*|качественн\w*|внимательн\w*|как следует|чист\w* код\w*|лучш\w* практик\w*)"
    r"(?![\w-])",
    re.IGNORECASE)
# what a reader can find or run: code in backticks, a number, a file, an identifier, a flag, a path or a URL
CONCRETE = re.compile(r"`[^`\n]+`|\d|\b\w+\.\w+\b|\b[A-Za-z]+_\w+|(?<![\w-])--?[A-Za-z][\w-]*|\w/\w|https?://")
# parts of a skill name that promise nothing on their own
FILLER = {"helper", "helpers", "util", "utils", "utility", "utilities", "tool", "tools", "toolkit", "kit", "guide",
          "guides", "tips", "notes", "misc", "common", "general", "basic", "basics", "stuff", "best", "practice",
          "practices", "skill", "skills", "howto"}
# abbreviations look like a dotted name or a path, and are no more concrete than the words around them
ABBREVIATIONS = re.compile(r"(?<![\w.])(?:i\.e\.|e\.g\.|etc\.|т\.\s?[екдп]\.|and/or|и/или)", re.IGNORECASE)
STEPS = re.compile(r"^##\s+Steps\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)
MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# a step with this many words or fewer once its vague words are gone says nothing else ("Write and test it")
STEP_WORDS = 4


def vague(text: str) -> list[str]:
    """The vague words `text` leans on, or [] when there are none or it also names something concrete."""
    if CONCRETE.search(ABBREVIATIONS.sub(" ", text)):
        return []
    return list(dict.fromkeys(m.group(0).lower() for m in VAGUE.finditer(text)))


def _said(part: str, words: set[str]) -> bool:
    # `log` is said by `logging`, `payments` by `payment`; but `sqlite` is not said by `sql`, nor `webhook` by `web`
    return part in words or any(len(part) >= 3 and w.startswith(part) or len(w) >= 4 and part.startswith(w)
                                for w in words)


def hollow_create(area: str, name: str, query: str) -> str:
    """Why `create <name>` promises nothing beyond its area and search query, or "" when some part of it does."""
    words = {w.lower() for w in re.findall(r"\w+", f"{area} {query}")}
    parts = [p for p in re.split(r"[-_.]", name.lower()) if p]
    if not parts or any(p not in FILLER and not _said(p, words) for p in parts):
        return ""
    return (f"area `{area.strip()}`: `create {name}` names nothing beyond the area and the search `{query.strip()}`: "
            "name what the skill will hold (`payment-webhook-signature`, not `payments-helper`)")


def items(markdown: str) -> list[str]:
    """The list items of `markdown`, each with all its lines: an item runs from its marker to a blank line, the next
    marker or a heading, so its indented and lazy continuation lines are read with it (CommonMark: list items)."""
    found, current = [], None
    for line in markdown.splitlines():
        if MARKER.match(line):
            current = [MARKER.sub("", line, count=1)]
            found.append(current)
        elif not line.strip() or line.lstrip().startswith("#"):
            current = None
        elif current is not None:
            current.append(line.strip())
    return [" ".join(lines).strip() for lines in found]


def vague_step(item: str) -> bool:
    """A step that is nothing but vague words: it names nothing concrete, and without them little is left."""
    return bool(vague(item)) and len(re.findall(r"\w+", VAGUE.sub(" ", item))) <= STEP_WORDS


def vague_steps(skill_md: str) -> list[str]:
    """The items under `## Steps` that are only vague words, with nothing concrete."""
    section = STEPS.search(skill_md)
    return [i for i in items(section.group(1)) if vague_step(i)] if section else []
