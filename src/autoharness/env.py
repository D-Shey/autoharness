"""Task-scoped harness environments.

Layout inside a project:

    .autoharness/
        active                  id of the current task
        tasks/<id>/
            task.json           task text, status, capabilities
            mcp.lock.json       MCP servers this task added to .mcp.json
            skills.lock.json    skills this task added to .claude/skills
            permissions.json    what each capability was granted
            harness.md          human-readable event log

AutoHarness only ever removes what it recorded in its own lockfiles, so
teardown never touches servers or skills the user set up by hand.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from autoharness.trust import ScanFinding, is_blocking, scan_skill


class HarnessError(Exception):
    pass


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text("utf-8"))


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    tmp.replace(path)


def _hash_dir(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).replace("\\", "/").encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def parse_skill_spec(spec: str) -> tuple[str, str]:
    """`owner/repo@skill` -> (`owner/repo`, `skill`)."""
    if "@" not in spec or spec.count("/") < 1:
        raise HarnessError(f"skill spec must look like owner/repo@skill, got {spec!r}")
    repo, skill = spec.rsplit("@", 1)
    return repo, skill


def npx_stage_skill(repo: str, skill: str, stage_dir: Path) -> Path:
    """Install a skill into an isolated staging directory using the `skills` CLI."""
    npx = shutil.which("npx")
    if not npx:
        raise HarnessError("npx not found; install Node.js to add skills")
    cmd = [npx, "-y", "skills", "add", repo, "--skill", skill, "-a", "claude-code", "-y", "--copy"]
    proc = subprocess.run(cmd, cwd=stage_dir, capture_output=True, text=True, timeout=300,
                          stdin=subprocess.DEVNULL)
    target = stage_dir / ".claude" / "skills" / skill
    if proc.returncode != 0 or not target.exists():
        raise HarnessError(f"skills CLI failed for {repo}@{skill}:\n{proc.stdout[-800:]}{proc.stderr[-800:]}")
    return target


Stager = Callable[[str, str, Path], Path]


@dataclass
class Project:
    root: Path

    @property
    def state(self) -> Path:
        return self.root / ".autoharness"

    @property
    def mcp_json(self) -> Path:
        return self.root / ".mcp.json"

    @property
    def skills_dir(self) -> Path:
        return self.root / ".claude" / "skills"

    def active_id(self) -> str | None:
        p = self.state / "active"
        return p.read_text("utf-8").strip() if p.exists() else None

    def task(self, task_id: str | None = None) -> "Task":
        task_id = task_id or self.active_id()
        if not task_id:
            raise HarnessError("no active task; run `autoharness init \"<task>\"` first")
        t = Task(self, task_id)
        if not t.dir.exists():
            raise HarnessError(f"unknown task {task_id}")
        return t

    def init_task(self, text: str) -> "Task":
        if (active := self.active_id()) and Task(self, active).info().get("status") == "active":
            raise HarnessError(f"task {active} is still active; run `autoharness teardown` first")
        task_id = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(text.encode()).hexdigest()[:6]
        t = Task(self, task_id)
        _write_json(t.dir / "task.json", {"id": task_id, "task": text, "status": "active",
                                          "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "capabilities": []})
        _write_json(t.dir / "mcp.lock.json", {})
        _write_json(t.dir / "skills.lock.json", {})
        _write_json(t.dir / "permissions.json", {})
        (t.dir / "harness.md").write_text(f"# Harness for task {task_id}\n\n> {text}\n\n", "utf-8")
        (self.state / "active").write_text(task_id, "utf-8")
        gitignore = self.state / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", "utf-8")
        return t


@dataclass
class Task:
    project: Project
    id: str

    @property
    def dir(self) -> Path:
        return self.project.state / "tasks" / self.id

    def info(self) -> dict:
        return _read_json(self.dir / "task.json", {})

    def _update_info(self, **fields) -> None:
        info = self.info()
        info.update(fields)
        _write_json(self.dir / "task.json", info)

    def log(self, line: str) -> None:
        with (self.dir / "harness.md").open("a", encoding="utf-8") as f:
            f.write(f"- `{time.strftime('%H:%M:%S')}` {line}\n")

    def mcp_lock(self) -> dict:
        return _read_json(self.dir / "mcp.lock.json", {})

    def skills_lock(self) -> dict:
        return _read_json(self.dir / "skills.lock.json", {})

    def _grant(self, key: str, perms: dict) -> None:
        grants = _read_json(self.dir / "permissions.json", {})
        grants[key] = perms
        _write_json(self.dir / "permissions.json", grants)

    def note_capability(self, capability: str, reason: str = "") -> None:
        caps = self.info().get("capabilities", [])
        if capability not in caps:
            caps.append(capability)
            self._update_info(capabilities=caps)
        self.log(f"capability **{capability}** needed" + (f": {reason}" if reason else ""))

    # --- MCP -----------------------------------------------------------------

    def add_mcp(self, name: str, config: dict, *, source: str, trust: dict, permissions: dict) -> None:
        if not self.project.mcp_json.exists():
            self._update_info(created_mcp_json=True)
        mcp = _read_json(self.project.mcp_json, {"mcpServers": {}})
        servers = mcp.setdefault("mcpServers", {})
        lock = self.mcp_lock()
        if name in servers and name not in lock:
            raise HarnessError(f"MCP server {name!r} already exists in .mcp.json and is not managed by autoharness")
        servers[name] = config
        _write_json(self.project.mcp_json, mcp)
        lock[name] = {"config": config, "source": source, "trust": trust,
                      "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _write_json(self.dir / "mcp.lock.json", lock)
        self._grant(f"mcp:{name}", permissions)
        self.log(f"added MCP `{name}` from {source} (trust {trust.get('level')}, {trust.get('score')})")

    def remove_mcp(self, name: str) -> None:
        lock = self.mcp_lock()
        if name not in lock:
            raise HarnessError(f"MCP server {name!r} was not added by this task")
        mcp = _read_json(self.project.mcp_json, {"mcpServers": {}})
        mcp.get("mcpServers", {}).pop(name, None)
        if not mcp.get("mcpServers") and set(mcp) <= {"mcpServers"} and self.info().get("created_mcp_json"):
            self.project.mcp_json.unlink(missing_ok=True)
        else:
            _write_json(self.project.mcp_json, mcp)
        del lock[name]
        _write_json(self.dir / "mcp.lock.json", lock)
        self.log(f"removed MCP `{name}`")

    # --- skills --------------------------------------------------------------

    def add_skill(self, spec: str, *, trust: dict, force: bool = False,
                  stage: Stager = npx_stage_skill) -> list[ScanFinding]:
        repo, skill = parse_skill_spec(spec)
        dest = self.project.skills_dir / skill
        lock = self.skills_lock()
        if dest.exists() and skill not in lock:
            raise HarnessError(f"skill {skill!r} already exists in .claude/skills and is not managed by autoharness")
        with tempfile.TemporaryDirectory(prefix="autoharness-stage-") as tmp:
            staged = stage(repo, skill, Path(tmp))
            findings = scan_skill(staged)
            if is_blocking(findings) and not force:
                self.log(f"REJECTED skill `{spec}`: " + "; ".join(f"{f.message} ({f.file}:{f.line})" for f in findings if f.severity == "high"))
                raise SkillRejected(spec, findings)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(staged, dest)
        lock[skill] = {"spec": spec, "sha256": _hash_dir(dest), "trust": trust,
                       "findings": [f.__dict__ for f in findings], "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _write_json(self.dir / "skills.lock.json", lock)
        self._grant(f"skill:{skill}", {"runs_with": "full agent permissions", "scan_findings": len(findings)})
        self.log(f"added skill `{spec}` ({len(findings)} scan findings)")
        return findings

    def remove_skill(self, skill: str) -> None:
        lock = self.skills_lock()
        if skill not in lock:
            raise HarnessError(f"skill {skill!r} was not added by this task")
        dest = self.project.skills_dir / skill
        if dest.exists():
            if _hash_dir(dest) != lock[skill]["sha256"]:
                self.log(f"skill `{skill}` was modified after install; kept a backup")
                shutil.copytree(dest, self.dir / "modified-skills" / skill, dirs_exist_ok=True)
            shutil.rmtree(dest)
        del lock[skill]
        _write_json(self.dir / "skills.lock.json", lock)
        self.log(f"removed skill `{skill}`")

    # --- lifecycle -----------------------------------------------------------

    def teardown(self) -> dict:
        removed = {"mcp": list(self.mcp_lock()), "skills": list(self.skills_lock())}
        for name in removed["mcp"]:
            self.remove_mcp(name)
        for skill in removed["skills"]:
            self.remove_skill(skill)
        self._update_info(status="torn-down", finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.log("teardown complete")
        active = self.project.state / "active"
        if active.exists() and active.read_text("utf-8").strip() == self.id:
            active.unlink()
        return removed


class SkillRejected(HarnessError):
    def __init__(self, spec: str, findings: list[ScanFinding]):
        self.spec, self.findings = spec, findings
        lines = "\n".join(f"  [{f.severity}] {f.message} — {f.file}:{f.line}: {f.excerpt}" for f in findings)
        super().__init__(f"skill {spec} failed the static scan:\n{lines}")
