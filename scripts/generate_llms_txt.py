#!/usr/bin/env python3
"""Generate llms.txt and llms-full.txt for AutoHarness documentation indexing."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LLMS_TXT = """# AutoHarness

> AutoHarness is an empirical harness for AI coding agents (Gemini, Claude Code, Antigravity, Cursor, Codex). It automates dynamic tool discovery, security vetting, sandbox containment, and empirical verification with independent critic reviews.

AutoHarness enables AI agents to start almost bare and build the exact harness each task requires: references, documentation, tools (MCP servers and skills), and empirical verification checks. All installed tools are scanned for prompt injection and data exfiltration, pinned in lockfiles, and cleanly torn down or persisted as recipes and lessons across projects.

## Core Documentation

- [Project Overview](README.md): Comprehensive guide to AutoHarness philosophy, core workflow, architecture, and security model.
- [Installation and Agent Setup](docs/installation.md): Instructions for configuring AutoHarness with Gemini, Claude Code, Antigravity, Cursor, Codex, and shell hooks.
- [CLI Reference](docs/reference.md): Detailed documentation for all commands (`init`, `brief`, `need`, `search`, `vet`, `add`, `verify`, `review`, `critic`, `teardown`).
- [Security and Trust Model](docs/security.md): Threat model, static scanner rules, sandbox containment, network egress restrictions, and veto policies.
- [Contributing Guide](CONTRIBUTING.md): Code standards, zero external runtime dependency rules, development workflows, and testing.

## Design Specifications

- [Architecture and Design Principles](docs/design/README.md): Core design decisions, minimal mechanisms, and task lifecycle guarantees.
- [MCP Gateway Architecture](docs/design/specs/2026-09-21-gateway-design.md): Gateway proxying, runtime tool injection without restarts, and permission isolation.
- [Dynamic Generated Skills Design](docs/design/specs/2026-09-21-generated-skills-design.md): Architecture for generating and verifying on-the-fly skills when registry candidates do not cover requirements.

## Key Capabilities and Concepts

- [Task Modes](docs/reference.md): Task lifecycle modes (`light` for chores, `full` for capability-driven tasks, `reviewed` for mandatory independent critic reviews).
- [Recipes and Lessons](docs/reference.md): Persistent harness patterns (`recipes`) and cumulative operational insights (`lessons`) carried across projects.
- [AST Outlining](src/autoharness/vet.py): Structural compaction of large code files preserving imports, classes, and method signatures while folding function bodies to defeat hidden payload attacks.
- [Empirical Verification](docs/reference.md): Verifiable evidence collection, command execution logging (`verify run`), and independent critic reviews (`critic`).

## Optional

- [Changelog](CHANGELOG.md): Version history, new capabilities, and evolutionary changes.
- [Security Disclosure Policy](SECURITY.md): Vulnerability reporting procedures and response commitments.
- [Full Documentation Compilation](llms-full.txt): Complete, concatenated repository documentation in a single file for full-context LLM ingestion.
"""

FULL_DOCS = [
    ("README.md", "Project Overview & Workflow"),
    ("docs/installation.md", "Installation and Agent Setup"),
    ("docs/reference.md", "CLI Command Reference"),
    ("docs/security.md", "Security & Trust Architecture"),
    ("docs/design/README.md", "Architecture & Design Philosophy"),
    ("docs/design/specs/2026-09-21-gateway-design.md", "Specification: MCP Gateway"),
    ("docs/design/specs/2026-09-21-generated-skills-design.md", "Specification: Generated Skills"),
    ("CONTRIBUTING.md", "Contributing Guidelines"),
]


def generate_llms_txt() -> str:
    # Verify all linked files exist (except llms-full.txt which is created alongside)
    link_pattern = re.compile(r"\[.*?\]\((.*?)\)")
    for target in link_pattern.findall(LLMS_TXT):
        target_file = target.split("#")[0]
        if target_file == "llms-full.txt":
            continue
        target_path = ROOT / target_file
        if not target_path.exists():
            raise FileNotFoundError(f"Broken link in llms.txt: {target} (path {target_path} not found)")
    return LLMS_TXT


def generate_llms_full_txt() -> str:
    sections = [
        "# AutoHarness Full Documentation",
        "",
        "> Complete concatenated documentation for AutoHarness, optimized for LLM context ingestion.",
        "",
        "---",
        "",
    ]
    for rel_path, title in FULL_DOCS:
        file_path = ROOT / rel_path
        if not file_path.exists():
            raise FileNotFoundError(f"Missing doc file for llms-full.txt: {file_path}")
        content = file_path.read_text(encoding="utf-8").strip()
        sections.append(f"<!-- START FILE: {rel_path} ({title}) -->")
        sections.append(f"## {title} (`{rel_path}`)")
        sections.append("")
        sections.append(content)
        sections.append("")
        sections.append(f"<!-- END FILE: {rel_path} -->")
        sections.append("")
        sections.append("---")
        sections.append("")

    return "\n".join(sections)


def main() -> None:
    llms_full_content = generate_llms_full_txt()
    llms_full_path = ROOT / "llms-full.txt"
    llms_full_path.write_text(llms_full_content, encoding="utf-8", newline="\n")
    print(f"Wrote {llms_full_path} ({len(llms_full_content)} chars)")

    llms_txt_content = generate_llms_txt()
    llms_txt_path = ROOT / "llms.txt"
    llms_txt_path.write_text(llms_txt_content, encoding="utf-8", newline="\n")
    print(f"Wrote {llms_txt_path} ({len(llms_txt_content)} chars)")


if __name__ == "__main__":
    main()
