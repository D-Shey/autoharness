"""GitHub update checker and self-updater for AutoHarness.

Pure Python standard library (zero runtime dependencies).
Checks updates from GitHub (https://github.com/D-Shey/autoharness) and provides
automated in-place self-updates via uv tool or pip.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from autoharness import __version__

DEFAULT_REPO = "D-Shey/autoharness"
MAX_HTTP_BYTES = 1_000_000  # 1MB upper bound on HTTP payloads
GITHUB_API_RELEASES = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_RAW_PYPROJECT = "https://raw.githubusercontent.com/{repo}/main/pyproject.toml"


def parse_semver(v: str) -> tuple[int, ...]:
    """Parse version string into a comparable numeric tuple, ignoring leading 'v'."""
    clean = re.sub(r"^[vV]", "", v.strip())
    parts = re.findall(r"\d+", clean)
    return tuple(int(p) for p in parts) if parts else (0,)


def check_for_updates(repo: str = DEFAULT_REPO, timeout: float = 6.0) -> dict[str, Any]:
    """Check GitHub repository for latest released or published version."""
    headers = {
        "User-Agent": f"autoharness/{__version__}",
        "Accept": "application/vnd.github.v3+json",
    }

    latest_version: str | None = None
    release_url: str = f"https://github.com/{repo}"
    notes: str = ""
    source: str = "github_releases"

    # 1. Try GitHub Releases API
    api_url = GITHUB_API_RELEASES.format(repo=repo)
    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(MAX_HTTP_BYTES).decode("utf-8"))
            tag = data.get("tag_name") or data.get("name") or ""
            latest_version = re.sub(r"^[vV]", "", tag.strip())
            release_url = data.get("html_url") or release_url
            notes = data.get("body") or ""
    except urllib.error.HTTPError as e:
        # 404 or other HTTP error: fallback to raw pyproject.toml
        if e.code == 404:
            source = "raw_pyproject"
        else:
            return {
                "status": "error",
                "error": f"GitHub API error (HTTP {e.code}): {e.reason}",
                "current_version": __version__,
                "latest_version": None,
                "update_available": False,
                "repo": repo,
            }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {
            "status": "error",
            "error": f"Network error checking updates: {e}",
            "current_version": __version__,
            "latest_version": None,
            "update_available": False,
            "repo": repo,
        }
    except (json.JSONDecodeError, ValueError) as e:
        return {
            "status": "error",
            "error": f"Failed to parse GitHub API response: {e}",
            "current_version": __version__,
            "latest_version": None,
            "update_available": False,
            "repo": repo,
        }

    # 2. Fallback to raw pyproject.toml if no GitHub release tag was obtained
    if not latest_version:
        source = "raw_pyproject"
        raw_url = GITHUB_RAW_PYPROJECT.format(repo=repo)
        req_raw = urllib.request.Request(raw_url, headers={"User-Agent": f"autoharness/{__version__}"})
        try:
            with urllib.request.urlopen(req_raw, timeout=timeout) as resp:
                raw_text = resp.read(MAX_HTTP_BYTES).decode("utf-8")
                m = re.search(r'''version\s*=\s*["']([^"']+)["']''', raw_text)
                if m:
                    latest_version = m.group(1).strip()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            return {
                "status": "error",
                "error": f"Failed to fetch raw pyproject.toml: {e}",
                "current_version": __version__,
                "latest_version": None,
                "update_available": False,
                "repo": repo,
            }

    if not latest_version:
        return {
            "status": "error",
            "error": f"Could not determine remote version from repository {repo}",
            "current_version": __version__,
            "latest_version": None,
            "update_available": False,
            "repo": repo,
        }

    curr_tuple = parse_semver(__version__)
    latest_tuple = parse_semver(latest_version)
    update_available = latest_tuple > curr_tuple

    return {
        "status": "ok",
        "current_version": __version__,
        "latest_version": latest_version,
        "update_available": update_available,
        "repo": repo,
        "release_url": release_url,
        "source": source,
        "notes": notes.strip(),
    }


def is_autoharness_git_repo(path: Path) -> bool:
    """Verify that a directory is specifically the AutoHarness repository."""
    if not ((path / ".git").is_dir() and (path / "pyproject.toml").is_file()):
        return False
    if not (path / "src" / "autoharness" / "__init__.py").is_file():
        return False
    try:
        content = (path / "pyproject.toml").read_text("utf-8")
        return bool(re.search(r'name\s*=\s*["\']autoharness["\']', content))
    except OSError:
        return False


def detect_install_mode(project_root: Path | None = None) -> str:
    """Detect package manager or installation environment for AutoHarness."""
    check_dir = project_root or Path.cwd()
    if is_autoharness_git_repo(check_dir):
        return "git_repo"

    # Check uv tool directory signature
    exe_str = str(sys.executable).lower()
    prefix_str = str(sys.prefix).lower()
    if "uv" in exe_str or "uv" in prefix_str:
        return "uv_tool"

    if shutil.which("uv"):
        return "uv_tool"

    if "pipx" in exe_str or "pipx" in prefix_str:
        return "pipx"

    return "pip"


def perform_update(
    repo: str = DEFAULT_REPO,
    force: bool = False,
    install_mode: str | None = None,
    project_root: Path | None = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Execute automated self-update to latest version from GitHub."""
    mode = install_mode or detect_install_mode(project_root)
    git_url = f"git+https://github.com/{repo}.git"

    cmd: list[str] = []
    run_fn = runner or subprocess.run
    work_dir: Path | None = None

    if mode == "git_repo":
        root = project_root or Path.cwd()
        work_dir = root
        # In git working tree: perform git pull first, then editable reinstall
        if shutil.which("git"):
            try:
                run_fn(["git", "pull"], shell=False, cwd=str(root), capture_output=True, text=True, timeout=60.0)
            except (subprocess.SubprocessError, OSError):
                pass
        if shutil.which("uv"):
            cmd = ["uv", "tool", "install", "--editable", ".", "--force"]
        else:
            cmd = [sys.executable, "-m", "pip", "install", "-e", "."]
    elif mode == "uv_tool":
        cmd = ["uv", "tool", "install", "--force", git_url]
    elif mode == "pipx":
        cmd = ["pipx", "install", "--force", git_url]
    else:  # pip
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", git_url]

    try:
        res = run_fn(
            cmd,
            shell=False,
            cwd=str(work_dir) if work_dir else None,
            capture_output=True,
            text=True,
            timeout=120.0,
        )
        returncode = res.returncode if hasattr(res, "returncode") else 0
        stdout = res.stdout if hasattr(res, "stdout") else ""
        stderr = res.stderr if hasattr(res, "stderr") else ""
        success = returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        return {
            "status": "error",
            "error": f"Update execution failed: {e}",
            "command": " ".join(cmd),
            "install_mode": mode,
            "previous_version": __version__,
        }

    return {
        "status": "ok" if success else "error",
        "previous_version": __version__,
        "install_mode": mode,
        "command": " ".join(cmd),
        "returncode": returncode,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
        "message": (
            f"Successfully updated AutoHarness from {repo} via {mode}."
            if success
            else f"Update command failed with exit code {returncode}."
        ),
    }
