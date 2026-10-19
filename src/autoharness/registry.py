"""Clients for public discovery sources: Anthropic's plugin marketplace, the official MCP registry and skills.sh."""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Callable

MCP_REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"
SKILLS_SEARCH = "https://skills.sh/api/search"
# Anthropic's curated plugin marketplace for Claude Code: one file, fast, and many plugins bring an MCP server
MARKETPLACE_REPO = "anthropics/claude-plugins-official"
MARKETPLACE = f"https://raw.githubusercontent.com/{MARKETPLACE_REPO}/main/.claude-plugin/marketplace.json"
MARKETPLACE_PREFIX = "claude-plugins-official/"

Fetcher = Callable[[str], dict]


def _curl_get_json(url: str, timeout: float) -> dict:
    curl = shutil.which("curl")
    if not curl:
        raise OSError("curl not available")
    proc = subprocess.run([curl, "-sSfL", "-m", str(int(timeout)), "-A", "autoharness/0.1", url],
                          capture_output=True, timeout=timeout + 5)
    if proc.returncode != 0:
        raise OSError(proc.stderr.decode("utf-8", "replace").strip() or f"curl exit {proc.returncode}")
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except ValueError as e:
        raise OSError(f"bad JSON from {url}: {e}") from e


def http_get_json(url: str, timeout: float = 20.0, retries: int = 1) -> dict:
    """GET JSON with curl when it is installed, else urllib with curl as a last resort.

    curl goes first because on some Windows setups Python's TLS reads stall until the
    timeout (measured: urllib 9-15 s per search, curl 1-3 s).
    """
    if shutil.which("curl"):
        try:
            return _curl_get_json(url, timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass
    req = urllib.request.Request(url, headers={"User-Agent": "autoharness/0.1", "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, http.client.HTTPException, ValueError):
            # ValueError covers a truncated or non-JSON body
            if attempt < retries:
                time.sleep(1)
    return _curl_get_json(url, timeout)


def quick_get_json(url: str, timeout: float) -> dict:
    """One attempt within `timeout`: for searches that run under a budget, where falling back from curl to
    urllib and back would triple the wait."""
    if shutil.which("curl"):
        return _curl_get_json(url, timeout)
    req = urllib.request.Request(url, headers={"User-Agent": "autoharness/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _raw_url(source, path: str) -> str | None:
    """Where a marketplace plugin's file lives on GitHub: the plugin's `source` is a path in the marketplace
    repo, a repository URL (pinned by `sha`), or a directory in one (`git-subdir`)."""
    if isinstance(source, str):
        return f"https://raw.githubusercontent.com/{MARKETPLACE_REPO}/main/{source.lstrip('./')}/{path}"
    m = re.match(r"https?://github\.com/([^/]+/[^/.]+)", source.get("url", "") or source.get("repo", ""))
    if not m:
        if source.get("source") == "github" and source.get("repo"):
            m = re.match(r"(.+)", source["repo"])
        else:
            return None
    ref = source.get("sha") or source.get("ref") or "HEAD"
    sub = source.get("path", "").strip("/")
    return f"https://raw.githubusercontent.com/{m.group(1)}/{ref}/{sub + '/' if sub else ''}{path}"


def marketplace_plugins(fetch: Fetcher = http_get_json) -> list[dict]:
    return fetch(MARKETPLACE).get("plugins", [])


def search_marketplace(query: str, limit: int = 10, fetch: Fetcher = http_get_json) -> list[dict]:
    """Marketplace plugins whose name or description has the query's words, the name counting most."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2] or [query.lower()]
    ranked = []
    for p in marketplace_plugins(fetch):
        name, about = p.get("name", "").lower(), f"{p.get('description', '')} {p.get('category', '')}".lower()
        hits = sum(3 for w in words if w in name) + sum(1 for w in words if re.search(rf"\b{re.escape(w)}", about))
        if hits:
            ranked.append((hits, p))
    ranked.sort(key=lambda h: h[0], reverse=True)
    return [p for _, p in ranked[:limit]]


def marketplace_servers(plugin: dict, fetch: Fetcher = http_get_json) -> dict[str, dict]:
    """The MCP servers a marketplace plugin brings, {name: config}: from its `.mcp.json`, else the
    `mcpServers` of its plugin manifest. {} when it brings none (a skills-only plugin)."""
    if isinstance(plugin.get("mcpServers"), dict):
        return plugin["mcpServers"]
    for path in (".mcp.json", ".claude-plugin/plugin.json"):
        url = _raw_url(plugin.get("source", ""), path)
        if not url:
            return {}
        try:
            data = fetch(url)
        except Exception:  # noqa: BLE001 - a plugin without the file
            continue
        servers = data.get("mcpServers", data if path == ".mcp.json" else {})
        servers = {k: v for k, v in servers.items() if isinstance(v, dict) and ("command" in v or "url" in v)}
        if servers:
            return servers
    return {}


def get_marketplace(name: str, fetch: Fetcher = http_get_json) -> dict | None:
    """`claude-plugins-official/<plugin>` with its servers, or None."""
    wanted = name.removeprefix(MARKETPLACE_PREFIX)
    plugin = next((p for p in marketplace_plugins(fetch) if p.get("name") == wanted), None)
    if plugin is None:
        return None
    return {"plugin": plugin, "servers": marketplace_servers(plugin, fetch)}


def search_mcp(query: str, limit: int = 10, fetch: Fetcher = http_get_json) -> list[dict]:
    """Search the official MCP registry. Returns raw `{server, _meta}` entries (latest versions)."""
    qs = urllib.parse.urlencode({"search": query, "limit": limit, "version": "latest"})
    data = fetch(f"{MCP_REGISTRY}?{qs}")
    return data.get("servers", [])[:limit]


def get_mcp(name: str, fetch: Fetcher = http_get_json) -> dict | None:
    """Look up one registry server by exact name."""
    for entry in search_mcp(name, limit=20, fetch=fetch):
        if entry.get("server", {}).get("name") == name:
            return entry
    return None


def search_skills(query: str, limit: int = 10, fetch: Fetcher = http_get_json) -> list[dict]:
    """Search skills.sh. Each result gets a `spec` of the form `owner/repo@skill`."""
    data = fetch(f"{SKILLS_SEARCH}?{urllib.parse.urlencode({'q': query})}")
    results = []
    for s in data.get("skills", [])[:limit]:
        results.append({
            "spec": f"{s['source']}@{s.get('skillId') or s['name']}",
            "name": s["name"],
            "source": s["source"],
            "installs": s.get("installs", 0),
        })
    return results
