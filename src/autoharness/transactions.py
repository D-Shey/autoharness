"""Serialized, recoverable file commits. Journals live outside the repository.

A committed intent is rolled forward on the next CLI operation. Recovery refuses
to overwrite a file edited after interruption. OS locks are released on process
exit; an old lock file is harmless and must never be deleted while in use.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_local = threading.local()


def _directory(root: Path) -> Path:
    from autoharness.recipes import home
    key = hashlib.sha256(os.path.normcase(str(root.resolve())).encode()).hexdigest()
    return home() / 'transactions' / key


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextmanager
def locked(root: Path):
    directory = _directory(root)
    key = str(directory)
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, 'held', set())
        if key in held:
            yield
            return
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'writer.lock').open('a+b') as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            deadline = time.monotonic() + 30
            while True:
                try:
                    stream.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OSError('another autoharness operation holds the project lock')
                    time.sleep(0.05)
            _local.held = held | {key}
            try:
                yield
            finally:
                _local.held = held
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _target(root: Path, rel: str) -> Path:
    root = root.resolve()
    path = root / rel
    if not path.resolve().is_relative_to(root):
        raise OSError('transaction path escapes the project')
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise OSError('transaction cannot write through a filesystem link')
    return path


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def recover(root: Path) -> None:
    with locked(root):
        journal = _directory(root) / 'pending.json'
        if not journal.exists():
            return
        entries = json.loads(journal.read_text('utf-8'))['files']
        writes = []
        # Validate every file before completing any remaining write.
        for entry in entries:
            path = _target(root, entry['path'])
            data = base64.b64decode(entry['after'], validate=True)
            current = path.read_bytes() if path.exists() else None
            if current == data:
                continue
            if _digest(current) != entry['before']:
                raise OSError(f'{path} changed after an interrupted operation; recovery journal: {journal}')
            writes.append((path, data))
        for path, data in writes:
            atomic_write(path, data)
        journal.unlink()


def commit(root: Path, updates: dict[Path, str]) -> None:
    with locked(root):
        recover(root)
        entries = []
        for path, text in updates.items():
            rel = str(path.relative_to(root))
            path = _target(root, rel)
            before = path.read_bytes() if path.exists() else None
            data = text.encode('utf-8')
            entries.append({'path': rel, 'before': _digest(before),
                            'after': base64.b64encode(data).decode('ascii')})
        journal = _directory(root) / 'pending.json'
        atomic_write(journal, json.dumps({'files': entries}).encode('utf-8'))
        recover(root)
