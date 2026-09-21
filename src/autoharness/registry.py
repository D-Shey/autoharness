"""Clients for public discovery sources: the official MCP registry and skills.sh."""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Callable

MCP_REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"
SKILLS_SEARCH = "https://skills.sh/api/search"

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
