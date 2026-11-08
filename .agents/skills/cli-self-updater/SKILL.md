---
name: cli-self-updater
description: Self-update CLI tool via uv tool upgrade or pip install with rollback safety
---

# cli-self-updater

## When to use
Use when updating a Python command line application in-place from its git repository or release source, detecting the active package manager (`uv`, `pipx`, or `pip`).

## Steps
1. Detect runtime environment:
   - Check if the current workspace specifically matches the tool's own repository (`is_autoharness_git_repo` verifying `pyproject.toml` package name matches `"autoharness"` and `src/autoharness/__init__.py` exists) rather than an arbitrary user git repository.
   - Check if `uv` is available on `PATH` via `shutil.which("uv")`.
   - Check if installed as a `uv tool` or inside an editable git workspace.
2. Determine update strategy:
   - If running inside the package's own git working tree: perform `git pull` then re-install with `uv tool install --editable . --force` (or `pip install -e .`).
   - If installed globally as a uv tool or when `uv` is available: execute `uv tool install --force git+https://github.com/{owner}/{repo}.git`.
   - If running via standard pip/venv: execute `sys.executable -m pip install --upgrade git+https://github.com/{owner}/{repo}.git`.
3. Safe HTTP streaming with size limits:
   - Use `resp.read(MAX_BYTES)` (e.g. 1MB buffer cap) to prevent unbounded memory consumption or denial-of-service from remote HTTP endpoints (source: https://docs.python.org/3/library/urllib.request.html).
4. Atomic file replacement:
   - Always write configuration files atomically via temporary file in the same directory and `os.replace` (source: https://docs.python.org/3/library/os.html#os.replace).
5. Confirmation and approval gating:
   - When running autonomously without `--yes`, verify consent or prompt interactive user before mutating the environment.
6. Post-update verification:
   - Check subprocess exit code == 0.
   - Run new binary version check `--version` to confirm upgrade.

## Facts found here
- A workspace containing `.git` and `pyproject.toml` is NOT necessarily the tool's repo; verify `name = "autoharness"` before executing in-place editable installation.
- In Python `urllib.request`, `response.read(size)` enforces a hard buffer ceiling, preventing unbounded memory exhaustion (source: https://docs.python.org/3/library/urllib.request.html).
- `os.replace(temp_path, path)` atomically overwrites existing destination files on both Windows and POSIX systems when the temp file resides in the same directory/filesystem (source: https://docs.python.org/3/library/os.html#os.replace).
