"""Where an MCP server may send traffic: the `allowed_hosts` setting.

`allowed_hosts` is a comma-separated list of host patterns (`api.github.com`, `*.example.com`);
empty means no restriction. autoharness can only hold a remote (HTTP/SSE) server to it: it knows
the one host the server lives on and the gateway refuses to connect elsewhere. A local process
(npx, uvx, docker) reaches whatever it likes, so under an allowlist it needs the user's approval.
"""

from __future__ import annotations

from fnmatch import fnmatch
from urllib.parse import urlsplit


def patterns(value: str) -> list[str]:
    return [p.strip().lower() for p in value.split(",") if p.strip()]


def host(config: dict) -> str | None:
    """The host a remote server lives on; None for a local process."""
    return (urlsplit(config["url"]).hostname or "").lower() if config.get("url") else None


def allowed(h: str, allow: list[str]) -> bool:
    return any(fnmatch(h, p) for p in allow)


def check(config: dict, allow: list[str]) -> dict:
    """What the server can reach and whether that fits `allow`: {"host", "enforced", "ok", "reason"}."""
    h = host(config)
    if h is None:
        reason = "local process: autoharness cannot restrict where it connects"
        return {"host": None, "enforced": False, "ok": not allow,
                "reason": reason + ("" if not allow else "; allowed_hosts is set, so the user must approve it")}
    if not allow:
        return {"host": h, "enforced": False, "ok": True, "reason": "allowed_hosts is empty: any host"}
    ok = allowed(h, allow)
    return {"host": h, "enforced": True, "ok": ok,
            "reason": f"{h} is {'on' if ok else 'not on'} allowed_hosts ({', '.join(allow)})"}
