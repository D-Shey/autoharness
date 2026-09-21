"""Curated capability catalog and keyword-based task classification."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources

from autoharness import text


@dataclass
class Capability:
    name: str
    description: str
    keywords: list[str]
    mcp: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    search: list[str] = field(default_factory=list)  # queries for skills.sh / the MCP registry
    requires: dict = field(default_factory=dict)  # evidence teardown insists on (evidence.py)
    review: str = ""  # what an independent critic should look at for this capability


@dataclass
class Catalog:
    capabilities: dict[str, Capability]
    mcp_servers: dict[str, dict]
    trusted_skill_owners: set[str]
    trusted_mcp_publishers: set[str]

    @classmethod
    def load(cls) -> "Catalog":
        raw = json.loads(resources.files("autoharness").joinpath("data/catalog.json").read_text("utf-8"))
        caps = {
            name: Capability(name=name, description=c["description"], keywords=c["keywords"],
                             mcp=c.get("mcp", []), skills=c.get("skills", []), search=c.get("search", []), requires=c.get("requires", {}), review=c.get("review", ""))
            for name, c in raw["capabilities"].items()
        }
        return cls(caps, raw["mcp_servers"], set(raw["trusted_skill_owners"]),
                   set(raw.get("trusted_mcp_publishers", [])))

    def classify(self, task: str) -> list[tuple[Capability, list[str]]]:
        """Return capabilities whose keywords appear in the task, strongest first.

        This is only a hint for the agent; the agent does the real reasoning
        about which capabilities a task needs.
        """
        task_words = text.words(task)
        hits = []
        for cap in self.capabilities.values():
            matched = [k for k in cap.keywords if text.matches(k, task_words)]
            if matched:
                hits.append((cap, matched))
        hits.sort(key=lambda h: len(h[1]), reverse=True)
        return hits
