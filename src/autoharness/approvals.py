"""What the user approved, and what autoharness installed, on this machine.

`.autoharness/gateway.json` and `kept.json` sit in the project and may be committed, so a pull or a merged
pull request can change them. Neither is proof of anything done here:

- the gateway runs a server only when this exact config was approved here: `add mcp` records it (the user
  approved it there), `autoharness gateway approve <name>` does it for a server someone else added;
- `remove` and `prune` uninstall a kept skill or server only when a task on this machine kept it (or the
  user says yes), so a pulled `kept.json` naming the user's own skill or server cannot make them delete it;
- a task uninstalls what its lockfiles name only when it was started on this machine: `.autoharness/tasks/`
  is ignored by git, but a commit can still add it.

The records live in the user's autoharness home, outside every repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


def digest(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()


def _file(name: str) -> Path:
    from autoharness import recipes  # lazy: recipes imports env, which imports this
    return recipes.home() / name


GATEWAY = "gateway-approved.json"
KEPT = "kept-here.json"


def _key(root: Path) -> str:
    return os.path.normcase(str(Path(root).resolve()))


def _load(name: str, strict: bool = False) -> dict:
    """The records; {} if there are none. Unreadable counts as none for reading (nothing runs, nothing is
    removed), but `strict` (every write) raises instead: saving over it would wipe every other project's."""
    path = _file(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        if strict:
            raise OSError(f"{path} is unreadable ({e}): fix or remove it; not overwriting it") from e
        return {}


def _save(name: str, data: dict) -> None:
    path = _file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    for attempt in range(100):
        try:
            tmp.replace(path)
            return
        except PermissionError:   # Windows: a reader has the file open for a moment
            if attempt == 99:
                raise
            time.sleep(0.02)


STALE_LOCK = 10.0   # seconds: a lock this old was left by a writer that died


@contextmanager
def _locked(name: str):
    """One writer at a time: two agents in parallel must not lose each other's records."""
    lock = _file(name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 2 * STALE_LOCK
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):   # Windows: PermissionError while the old lock is deleted
            try:
                stale = time.time() - lock.stat().st_mtime > STALE_LOCK
            except OSError:
                stale = False   # gone already (try again), or not ours to read (the deadline ends it)
            if stale:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass


def _set(name: str, root: Path, key: str, value) -> None:
    with _locked(name):
        data = _load(name, strict=True)
        data.setdefault(_key(root), {})[key] = value
        _save(name, data)


def _drop(name: str, root: Path, key: str) -> None:
    with _locked(name):
        data = _load(name, strict=True)
        if data.get(_key(root), {}).pop(key, None) is not None:
            _save(name, data)


def _get(name: str, root: Path, key: str):
    return _load(name).get(_key(root), {}).get(key)


# --- gateway servers ------------------------------------------------------------

def approve(root: Path, name: str, config: dict) -> None:
    _set(GATEWAY, root, name, digest(config))


def forget(root: Path, name: str) -> None:
    _drop(GATEWAY, root, name)


def approved(root: Path, name: str, config: dict) -> bool:
    return _get(GATEWAY, root, name) == digest(config)


# --- what tasks here kept in the project -------------------------------------------

def note_kept(root: Path, kind: str, name: str, entry: dict) -> None:
    """`kind` is "mcp" or "skills", as in kept.json. The whole entry counts, not just its name: a pulled
    kept.json that keeps the name but changes the agent or the config must not pass for it."""
    _set(KEPT, root, f"{kind}:{name}", digest(entry))


def forget_kept(root: Path, kind: str, name: str) -> None:
    _drop(KEPT, root, f"{kind}:{name}")


def kept_here(root: Path, kind: str, name: str, entry: dict) -> bool:
    return _get(KEPT, root, f"{kind}:{name}") == digest(entry)


def note_task(root: Path, task_id: str) -> None:
    _set(KEPT, root, f"task:{task_id}", True)


def task_here(root: Path, task_id: str) -> bool:
    return bool(_get(KEPT, root, f"task:{task_id}"))


def forget_task(root: Path, task_id: str) -> None:
    _drop(KEPT, root, f"task:{task_id}")
