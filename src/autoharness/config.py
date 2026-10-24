"""Settings, layered: command flag > $AUTOHARNESS_<KEY> > project > global > default.

    <project>/.autoharness/config.json    per project (can be committed)
    ~/.autoharness/config.json            for every project (AUTOHARNESS_HOME overrides)

`teardown`: `keep` leaves what a task installed in the project when the task
ends, `remove` takes it all down.
`allowed_hosts`: comma-separated host patterns MCP servers may reach (see egress.py);
empty, the default, means no restriction.
`min_mode`: the lowest level `init` starts a task at: `any` (the default) leaves it to the agent, `full` or
`reviewed` raise a lighter choice to that level.
`journal`: the project's journal each task adds an entry to, relative to the project (empty, the default, means
`JOURNAL.md` when it exists); `off` asks for none (see journal.py).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from autoharness.env import HarnessError, ensure_state_dir
from autoharness.recipes import home

# first value is the default; None takes free text and defaults to ""
SETTINGS: dict[str, tuple[str, ...] | None] = {"teardown": ("keep", "remove"), "allowed_hosts": None,
                                               "min_mode": ("any", "full", "reviewed"), "journal": None}


def global_path() -> Path:
    return home() / "config.json"


def project_path(root: Path) -> Path:
    return root / ".autoharness" / "config.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def _check(key: str, value: str, where: str) -> str:
    if key not in SETTINGS:
        raise HarnessError(f"unknown setting {key!r}; known: {', '.join(SETTINGS)}")
    if SETTINGS[key] is not None and value not in SETTINGS[key]:
        raise HarnessError(f"{where}: {key} must be one of {', '.join(SETTINGS[key])}, not {value!r}")
    return value


def set_value(path: Path, key: str, value: str) -> None:
    _check(key, value, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.name == ".autoharness":
        ensure_state_dir(path.parent)
    data = _load(path)
    data[key] = value
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


def resolve(root: Path, key: str, flag: str | None = None) -> tuple[str, str]:
    """Effective value of `key` and where it came from: flag, env, project, global or default."""
    if flag is not None:
        return _check(key, flag, "flag"), "flag"
    env = f"AUTOHARNESS_{key.upper()}"
    if os.environ.get(env):
        return _check(key, os.environ[env], f"${env}"), "env"
    for source, path in (("project", project_path(root)), ("global", global_path())):
        if key in (data := _load(path)):
            return _check(key, data[key], str(path)), source
    return (SETTINGS[key] or ("",))[0], "default"


def teardown_policy(root: Path, flag: str | None = None) -> tuple[str, str]:
    return resolve(root, "teardown", flag)


def allowed_hosts(root: Path) -> list[str]:
    """The allowlist; a config it cannot read allows nothing rather than everything."""
    from autoharness.egress import patterns
    try:
        value = resolve(root, "allowed_hosts")[0]
    except (ValueError, OSError):
        return ["<unreadable allowed_hosts: nothing allowed>"]
    return patterns(value) if isinstance(value, str) else ["<allowed_hosts is not a string: nothing allowed>"]
