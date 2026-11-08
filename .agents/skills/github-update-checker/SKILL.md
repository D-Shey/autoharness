---
name: github-update-checker
description: Check latest available release or commits from GitHub repository using standard library urllib
---

# github-update-checker

## When to use
Use when checking whether a local Python CLI or package installation has newer releases or commits published to GitHub without third-party network dependencies.

## Steps
1. Determine repository coordinates (owner and repo name, e.g. `D-Shey/autoharness`).
2. Construct request URL:
   - Primary: GitHub Releases API (`https://api.github.com/repos/{owner}/{repo}/releases/latest`).
   - Fallback: Raw file content (`https://raw.githubusercontent.com/{owner}/{repo}/main/pyproject.toml`).
3. Set mandatory HTTP headers:
   - `User-Agent`: identification header (e.g. `autoharness/{version}`) required by GitHub API.
   - `Accept`: `application/vnd.github.v3+json` or `application/json`.
4. Perform HTTP request via `urllib.request.urlopen` with a strict timeout (e.g. 5.0 seconds).
5. Parse version from response:
   - From release tag: strip leading `v` (e.g. `v1.0.0` -> `1.0.0`).
   - From raw pyproject.toml: extract `version = "..."` via regular expression.
6. Compare with local `__version__` using tuple-based semver parsing `tuple(int(x) for x in re.findall(r'\d+', v))`.
7. Return structured status: `{"update_available": bool, "current_version": str, "latest_version": str, "release_url": str}`.

## How to verify
1. Run unit test with mocked HTTP response: verify semver comparison detects both newer and equal versions.
2. Run test with network error/timeout: verify graceful error handling without crashing the CLI.

## Facts found here
- GitHub API enforces a User-Agent header; missing User-Agent results in HTTP 403 Forbidden.
