"""Ontology 2.0: living natural-language semantic map for AI-native projects.

Implements Kai-Fu Lee's Ontology 2.0 pattern (AI Native, Ch. 6):
- Human- and LLM-readable Markdown map stored in `.autoharness/ontology.md`
  (Glossary, Authoritative Sources of Truth, Entities & Lifecycles, Causal Links,
  and Judgment Templates).
- Selective token-efficient querying instead of dumping entire docs into context.
- Ground-truth reconciliation against workspace files and AST symbols to prevent drift.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from autoharness import memory, transactions

CANONICAL_SECTIONS: dict[str, str] = {
    "glossary": "Glossary",
    "sources": "Sources of Truth",
    "sources_of_truth": "Sources of Truth",
    "entities": "Entities & Lifecycles",
    "links": "Causal Links",
    "causal_links": "Causal Links",
    "judgments": "Judgment Templates",
    "judgment_templates": "Judgment Templates",
}

DEFAULT_ONTOLOGY_TEMPLATE = """# Project Ontology 2.0

Living natural-language semantic map of domain terms, canonical sources of truth, entity lifecycles, causal links, and judgment templates.

## Glossary
<!-- Define project-specific meanings and disambiguate overloaded terms -->

## Sources of Truth
<!-- Map each domain area to its authoritative file/module and name deprecated/draft files to avoid -->

## Entities & Lifecycles
<!-- Describe core entities, valid state transitions, owner modules, and invariants -->

## Causal Links
<!-- Document how changes in one component propagate to downstream modules or invariants -->

## Judgment Templates
<!-- Record trade-off principles and decision criteria when specs leave choices open -->
"""

_BOLD_TERM_RE = re.compile(r"^\s*[-*]\s+\*\*([^*]+)\*\*\s*[:\-—]\s*(.+)$")
_PLAIN_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)$")


def ontology_path(project_root: Path) -> Path:
    """Return the canonical path to `.autoharness/ontology.md`."""
    return project_root.resolve() / ".autoharness" / "ontology.md"


_SECTION_ALIASES = {
    "glossary": "glossary",
    "terms": "glossary",
    "sources": "sources_of_truth",
    "sources_of_truth": "sources_of_truth",
    "entities": "entities",
    "links": "causal_links",
    "causal_links": "causal_links",
    "judgments": "judgment_templates",
    "judgment_templates": "judgment_templates",
}


def _normalize_section_key(heading: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "_", heading.strip().lower()).strip("_")
    if clean in _SECTION_ALIASES:
        return _SECTION_ALIASES[clean]
    if clean in CANONICAL_SECTIONS:
        return clean
    if "glossary" in clean or "term" in clean:
        return "glossary"
    if "source" in clean or "truth" in clean or "canonical" in clean:
        return "sources_of_truth"
    if "entit" in clean or "lifecycle" in clean:
        return "entities"
    if "causal" in clean or "link" in clean:
        return "causal_links"
    if "judgment" in clean or "tradeoff" in clean:
        return "judgment_templates"
    return clean or "notes"


def _parse_entry(raw_line: str, section_key: str, section_title: str, line: int = 0) -> dict[str, Any]:
    body = raw_line.strip()
    term = ""
    if m := _BOLD_TERM_RE.match(body):
        term = m.group(1).strip()
        text = m.group(2).strip()
    elif m := _PLAIN_BULLET_RE.match(body):
        text = m.group(1).strip()
        if ":" in text.split("`", 1)[0]:
            left, right = text.split(":", 1)
            if len(left.strip()) <= 60:
                term, text = left.strip(), right.strip()
    else:
        text = body
    paths, symbols = memory.extract_code_references(body)
    return {
        "section": section_key,
        "section_title": section_title,
        "term": term,
        "text": text,
        "raw": body,
        "line": line,
        "paths": sorted(paths),
        "symbols": sorted(symbols),
    }


def parse_ontology(raw_text: str) -> dict[str, Any]:
    """Parse an ontology Markdown document into structured sections and entries."""
    sections: dict[str, list[dict[str, Any]]] = {
        "glossary": [],
        "sources_of_truth": [],
        "entities": [],
        "causal_links": [],
        "judgment_templates": [],
    }
    current_key = ""
    current_title = ""
    in_comment = False

    for line_no, line in enumerate(raw_text.splitlines(), 1):
        stripped = line.strip()
        if "<!--" in stripped and "-->" not in stripped:
            in_comment = True
            continue
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        if stripped.startswith("## "):
            current_title = stripped[3:].strip()
            current_key = _normalize_section_key(current_title)
            sections.setdefault(current_key, [])
            continue
        if not current_key or not stripped or stripped.startswith("# "):
            continue
        if _PLAIN_BULLET_RE.match(line):
            sections[current_key].append(_parse_entry(line, current_key, current_title, line=line_no))

    entries = [e for items in sections.values() for e in items]
    return {"sections": sections, "entries": entries, "entry_count": len(entries)}


def init_ontology(project_root: Path, overwrite: bool = False) -> dict[str, Any]:
    """Create `.autoharness/ontology.md` with the Ontology 2.0 template if absent."""
    root = project_root.resolve()
    path = ontology_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with transactions.locked(root):
        existed = path.is_file()
        if existed and not overwrite:
            return {"status": "exists", "path": str(path), "created": False}
        transactions.atomic_write(path, DEFAULT_ONTOLOGY_TEMPLATE.encode("utf-8"))
        return {"status": "ok", "path": str(path), "created": True, "overwritten": existed}


MAX_ONTOLOGY_BYTES = 2 * 1024 * 1024


def load_ontology(project_root: Path) -> dict[str, Any]:
    """Load and parse `.autoharness/ontology.md` if present."""
    path = ontology_path(project_root)
    if not path.is_file():
        return {
            "exists": False,
            "path": str(path),
            "raw": "",
            "sections": {k: [] for k in ("glossary", "sources_of_truth", "entities", "causal_links", "judgment_templates")},
            "entries": [],
            "entry_count": 0,
        }
    if path.stat().st_size > MAX_ONTOLOGY_BYTES:
        raise ValueError(f"ontology file {path} exceeds maximum size ({MAX_ONTOLOGY_BYTES} bytes)")
    raw = path.read_text("utf-8", errors="replace")
    parsed = parse_ontology(raw)
    return {"exists": True, "path": str(path), "raw": raw, **parsed}


def add_ontology_entry(
    project_root: Path,
    section: str,
    entry: str,
    term: str = "",
) -> dict[str, Any]:
    """Append a structured entry to `.autoharness/ontology.md` atomically and idempotently."""
    clean_entry = entry.strip()
    if not clean_entry:
        raise ValueError("ontology entry cannot be empty")
    if "\n" in clean_entry or "\r" in clean_entry or "\n" in term or "\r" in term or "\n" in section or "\r" in section:
        raise ValueError("ontology section, entry, and term cannot contain newlines")
    sec_key = _normalize_section_key(section)
    sec_title = CANONICAL_SECTIONS.get(sec_key, section.strip().title() or "Notes")

    root = project_root.resolve()
    path = ontology_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    bullet = f"- **{term.strip()}**: {clean_entry}" if term.strip() else (
        clean_entry if clean_entry.startswith(("- ", "* ")) else f"- {clean_entry}"
    )

    with transactions.locked(root):
        if not path.is_file():
            transactions.atomic_write(path, DEFAULT_ONTOLOGY_TEMPLATE.encode("utf-8"))
        elif path.stat().st_size > MAX_ONTOLOGY_BYTES:
            raise ValueError(f"ontology file {path} exceeds maximum size ({MAX_ONTOLOGY_BYTES} bytes)")
        raw = path.read_text("utf-8", errors="replace")
        parsed = parse_ontology(raw)
        for existing in parsed["sections"].get(sec_key, []):
            if existing["raw"].strip() == bullet.strip():
                return {"status": "unchanged", "path": str(path), "section": sec_title, "entry": bullet}

        lines = raw.splitlines()
        insert_idx: int | None = None
        for idx, line in enumerate(lines):
            s = line.strip()
            if s.startswith("## ") and _normalize_section_key(s[3:].strip()) == sec_key:
                insert_idx = idx + 1
                while insert_idx < len(lines) and not lines[insert_idx].strip().startswith("## "):
                    insert_idx += 1
                while insert_idx > idx + 1 and not lines[insert_idx - 1].strip():
                    insert_idx -= 1
                break

        if insert_idx is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"## {sec_title}")
            lines.append(bullet)
        else:
            lines.insert(insert_idx, bullet)

        updated = "\n".join(lines).rstrip() + "\n"
        transactions.atomic_write(path, updated.encode("utf-8"))
    return {"status": "added", "path": str(path), "section": sec_title, "entry": bullet}


def query_ontology(project_root: Path, query: str = "", limit: int = 8) -> dict[str, Any]:
    """Query the project's Ontology 2.0 map selectively without loading full raw prose."""
    clamped = max(0, int(limit))
    loaded = load_ontology(project_root)
    if not loaded["exists"]:
        return {"exists": False, "query": query, "matches": [], "total_entries": 0}

    entries = loaded["entries"]
    q_clean = (query or "").strip().lower()
    if not q_clean:
        return {
            "exists": True,
            "query": "",
            "matches": entries[:clamped] if clamped > 0 else [],
            "total_entries": len(entries),
        }

    tokens = [t for t in re.findall(r"[\w\u0400-\u04FF]+", q_clean) if len(t) >= 2]
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in entries:
        haystack = f"{item['term']} {item['text']} {' '.join(item['paths'])} {' '.join(item['symbols'])}".lower()
        score = 0
        if q_clean in haystack:
            score += 5
        if item["term"] and q_clean in item["term"].lower():
            score += 4
        for tok in tokens:
            if tok in haystack:
                score += 2
            if item["term"] and tok in item["term"].lower():
                score += 2
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["section"], pair[1]["term"]))
    matches = [item | {"relevance": score} for score, item in scored[:clamped]] if clamped > 0 else []
    return {
        "exists": True,
        "query": query,
        "matches": matches,
        "total_entries": len(entries),
    }


def validate_ontology(project_root: Path) -> dict[str, Any]:
    """Reconcile `.autoharness/ontology.md` paths, AST symbols, and term uniqueness against repo reality."""
    root = project_root.resolve()
    loaded = load_ontology(root)
    if not loaded["exists"]:
        return {
            "status": "missing",
            "exists": False,
            "path": loaded["path"],
            "total_entries": 0,
            "valid_entries": 0,
            "broken_paths": [],
            "missing_symbols": [],
            "conflicts": [],
            "drift_rate": 0.0,
        }

    entries = loaded["entries"]
    ast_cache: dict[Path, set[str]] = {}
    broken_paths: list[dict[str, str]] = []
    missing_symbols: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    valid_entries = 0

    seen_terms: dict[tuple[str, str], str] = {}
    for item in entries:
        item_broken = False
        term_norm = item["term"].strip().lower()
        if term_norm and item["section"] in ("glossary", "sources_of_truth"):
            key = (item["section"], term_norm)
            if key in seen_terms and seen_terms[key] != item["text"]:
                conflicts.append({
                    "section": item["section_title"],
                    "term": item["term"],
                    "reason": f"Duplicate or conflicting definition for '{item['term']}' in {item['section_title']}",
                })
                item_broken = True
            else:
                seen_terms[key] = item["text"]

        existing_py: list[Path] = []
        for p_str in item["paths"]:
            p_norm = p_str.replace("\\", "/")
            if ":" in p_norm:
                parts = p_norm.split(":")
                if not (len(parts[0]) == 1 and parts[0].isalpha()):
                    p_norm = parts[0]
            try:
                p_path = (root / p_norm).resolve()
                if not p_path.is_relative_to(root):
                    continue
            except (ValueError, OSError):
                continue
            if not p_path.exists():
                broken_paths.append({"path": p_norm, "section": item["section_title"], "entry": item["raw"]})
                item_broken = True
            elif p_path.suffix == ".py":
                existing_py.append(p_path)
                if p_path not in ast_cache:
                    ast_cache[p_path] = memory.collect_file_ast_symbols(p_path)

        if existing_py and item["symbols"]:
            all_syms = set()
            for py_p in existing_py:
                all_syms.update(ast_cache.get(py_p, set()))
            for sym in item["symbols"]:
                if sym not in all_syms and not sym.startswith("_"):
                    missing_symbols.append({
                        "symbol": sym,
                        "paths": ", ".join(item["paths"]),
                        "section": item["section_title"],
                    })
                    item_broken = True

        if not item_broken:
            valid_entries += 1

    total = len(entries)
    issue_entries = total - valid_entries
    drift_rate = round(issue_entries / max(1, total), 4) if total else 0.0
    status = "drifted" if (broken_paths or missing_symbols or conflicts) else "healthy"
    return {
        "status": status,
        "exists": True,
        "path": loaded["path"],
        "total_entries": total,
        "valid_entries": valid_entries,
        "broken_paths": broken_paths,
        "missing_symbols": missing_symbols,
        "conflicts": conflicts,
        "drift_rate": drift_rate,
    }


def format_ontology_context(project_root: Path, query: str = "", max_entries: int = 12) -> str:
    """Render a compact Ontology 2.0 summary block for critic or task planning prompts."""
    res = query_ontology(project_root, query=query, limit=max_entries)
    if not res["exists"] or not res["matches"]:
        return ""
    lines = ["Project Ontology 2.0 (canonical terms, sources of truth, invariants, and judgment templates):"]
    for item in res["matches"]:
        prefix = f"[{item['section_title']}] "
        label = f"**{item['term']}**: " if item["term"] else ""
        lines.append(f"- {prefix}{label}{item['text']}")
    return "\n".join(lines)


def format_ontology_report(data: dict[str, Any]) -> str:
    """Format ontology inspection or validation output into readable Markdown."""
    if not data.get("exists"):
        return (
            "# Project Ontology 2.0\n\n"
            "No `.autoharness/ontology.md` found yet. Initialize one with `autoharness ontology init`."
        )
    if "broken_paths" in data:
        badge = f"`{data['status'].upper()}`"
        lines = [
            "# Ontology 2.0 Reconciliation Report",
            "",
            f"- **Status:** {badge}",
            f"- **Entries Scanned:** {data.get('total_entries', 0)}",
            f"- **Valid Entries:** {data.get('valid_entries', 0)}",
            f"- **Drift Rate:** {round(data.get('drift_rate', 0.0) * 100, 1)}%",
            "",
        ]
        for bp in data.get("broken_paths", []):
            lines.append(f"- Broken path `{bp['path']}` in *{bp['section']}*")
        for ms in data.get("missing_symbols", []):
            lines.append(f"- Missing AST symbol `{ms['symbol']}` in `{ms['paths']}` (*{ms['section']}*)")
        for cf in data.get("conflicts", []):
            lines.append(f"- Conflict: {cf['reason']}")
        if data["status"] == "healthy":
            lines.append("All ontology entries match current workspace files and AST symbols.")
        return "\n".join(lines)

    matches = data.get("matches") or data.get("entries") or []
    lines = [f"# Project Ontology 2.0 ({len(matches)} entries shown)", ""]
    for item in matches:
        label = f"**{item['term']}**: " if item.get("term") else ""
        lines.append(f"- [{item.get('section_title', item.get('section', ''))}] {label}{item['text']}")
    return "\n".join(lines)
