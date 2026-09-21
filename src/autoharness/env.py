"""Task-scoped harness environments.

Layout inside a project:

    .autoharness/
        active                  id of the current task
        config.json             project settings (see config.py)
        kept.json               what finished tasks left installed (teardown policy `keep`)
        modified-skills/        backups of kept skills edited before removal
        tasks/<id>/
            task.json           task text, status, capabilities
            task.json           also records which agent the task targets
            mcp.lock.json       MCP servers this task added (canonical config)
            skills.lock.json    skills this task added to the agent's skills dir
            permissions.json    what each capability was granted
            harness.md          human-readable event log

AutoHarness only ever removes what it recorded in its own lockfiles or in
kept.json, so it never touches servers or skills the user set up by hand.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from autoharness import agents
from autoharness.agents import Agent, AgentError
from autoharness.trust import ScanFinding, is_blocking, scan_skill


class HarnessError(Exception):
    pass


def _agent(name: str | None) -> Agent:
    try:
        return agents.get(name)
    except AgentError as e:
        raise HarnessError(str(e)) from e


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text("utf-8"))


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    tmp.replace(path)


# Files a skill's own scripts or the OS create at runtime; they are not user edits.
RUNTIME_JUNK = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "*.pyc", ".DS_Store", "Thumbs.db")


def _is_junk(rel: Path) -> bool:
    return any(part == pat or fnmatch.fnmatch(part, pat) for part in rel.parts for pat in RUNTIME_JUNK)


def _hash_dir(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and not _is_junk(p.relative_to(root)):
            h.update(str(p.relative_to(root)).replace("\\", "/").encode())
            h.update(p.read_bytes())
    return h.hexdigest()


STATE_GITIGNORE = "*\n!.gitignore\n!config.json\n!kept.json\n"


def ensure_state_dir(state: Path) -> None:
    """Create .autoharness/; its .gitignore keeps task state out of git but lets settings and kept.json in."""
    state.mkdir(parents=True, exist_ok=True)
    gitignore = state / ".gitignore"
    if not gitignore.exists() or gitignore.read_text("utf-8") == "*\n":  # "*\n" = written by older versions
        gitignore.write_text(STATE_GITIGNORE, "utf-8")


def _uninstall_mcp(root: Path, agent: Agent, name: str, created: dict | None) -> None:
    try:
        agent.mcp_store(root).remove(name, created_file=created is not None, created_dir=bool(created and created["dir"]))
    except AgentError as e:
        raise HarnessError(str(e)) from e


def _uninstall_skill(dest: Path, sha256: str, backup: Path) -> bool:
    """Delete an installed skill; back it up first if it was edited. Returns whether it was."""
    modified = dest.exists() and _hash_dir(dest) != sha256
    if modified:
        shutil.copytree(dest, backup, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*RUNTIME_JUNK))
    if dest.exists():
        shutil.rmtree(dest)
    return modified


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
    agent_name: str | None = None   # None: detect from the environment

    @property
    def state(self) -> Path:
        return self.root / ".autoharness"

    @property
    def agent(self) -> Agent:
        return _agent(self.agent_name)

    @property
    def mcp_path(self) -> Path:
        return self.agent.mcp_path(self.root)

    @property
    def skills_dir(self) -> Path:
        return self.agent.skills_path(self.root)

    def kept(self) -> dict:
        return _read_json(self.state / "kept.json", {"mcp": {}, "skills": {}})

    def _write_kept(self, kept: dict) -> None:
        ensure_state_dir(self.state)
        _write_json(self.state / "kept.json", kept)

    def remove_kept(self, kind: str, name: str) -> dict:
        """Uninstall one thing a finished task kept. `kind` is "mcp" or "skills"."""
        kept = self.kept()
        if name not in kept[kind]:
            raise HarnessError(f"{name!r} is not managed by autoharness in this project")
        entry = kept[kind].pop(name)
        agent = _agent(entry.get("agent", "claude"))
        result = {"removed": name}
        if kind == "mcp":
            _uninstall_mcp(self.root, agent, name, entry.get("created_mcp_config"))
        else:
            backup = self.state / "modified-skills" / name
            if _uninstall_skill(agent.skills_path(self.root) / name, entry["sha256"], backup):
                result["backup"] = str(backup)
        self._write_kept(kept)
        return result

    def prune(self) -> dict:
        kept = self.kept()
        removed = {"mcp": list(kept["mcp"]), "skills": list(kept["skills"])}
        for kind, names in removed.items():
            for name in names:
                self.remove_kept(kind, name)
        return removed

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
        if self.agent_name and self.agent_name != t.agent.name:
            raise HarnessError(f"task {task_id} targets {t.agent.name}, not {self.agent_name}")
        return t

    def init_task(self, text: str) -> "Task":
        if (active := self.active_id()) and Task(self, active).info().get("status") == "active":
            raise HarnessError(f"task {active} is still active; run `autoharness teardown` first")
        task_id = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(text.encode()).hexdigest()[:6]
        t = Task(self, task_id)
        _write_json(t.dir / "task.json", {"id": task_id, "task": text, "agent": self.agent.name, "status": "active",
                                          "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "capabilities": []})
        _write_json(t.dir / "mcp.lock.json", {})
        _write_json(t.dir / "skills.lock.json", {})
        _write_json(t.dir / "permissions.json", {})
        (t.dir / "harness.md").write_text(f"# Harness for task {task_id}\n\n> {text}\n\n", "utf-8")
        (self.state / "active").write_text(task_id, "utf-8")
        ensure_state_dir(self.state)
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

    @property
    def agent(self) -> Agent:
        return _agent(self.info().get("agent", "claude"))  # tasks from before adapters were Claude-only

    @property
    def skills_dir(self) -> Path:
        return self.agent.skills_path(self.project.root)

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

    def add_mcp(self, name: str, config: dict, *, source: str, trust: dict, permissions: dict) -> list[str]:
        """Write `config` (canonical form) into the agent's MCP file; returns translation warnings."""
        agent = self.agent
        store = agent.mcp_store(self.project.root)
        path = agent.mcp_path(self.project.root)
        lock = self.mcp_lock()
        if name in self.project.kept()["mcp"]:
            raise HarnessError(f"MCP server {name!r} is already installed: kept in the project by an earlier task")
        if name not in lock and store.has(name):
            raise HarnessError(f"MCP server {name!r} already exists in {agent.mcp_file} and is not managed by autoharness")
        created = self.info().get("created_mcp_config", {})
        if not path.exists() and agent.name not in created:
            created[agent.name] = {"dir": not path.parent.exists()}
            self._update_info(created_mcp_config=created)
        native, warnings = agent.translate(config)
        try:
            store.add(name, native)
        except AgentError as e:
            raise HarnessError(str(e)) from e
        lock[name] = {"config": config, "source": source, "trust": trust, "agent": agent.name,
                      "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _write_json(self.dir / "mcp.lock.json", lock)
        self._grant(f"mcp:{name}", permissions)
        self.log(f"added MCP `{name}` to {agent.mcp_file} from {source} (trust {trust.get('level')}, {trust.get('score')})")
        for w in warnings:
            self.log(f"warning for `{name}`: {w}")
        return warnings

    def remove_mcp(self, name: str) -> None:
        lock = self.mcp_lock()
        if name not in lock:
            raise HarnessError(f"MCP server {name!r} was not added by this task")
        _uninstall_mcp(self.project.root, self.agent, name, self._created_mcp_config())
        del lock[name]
        _write_json(self.dir / "mcp.lock.json", lock)
        self.log(f"removed MCP `{name}`")

    # --- skills --------------------------------------------------------------

    def add_skill(self, spec: str, *, trust: dict, force: bool = False,
                  stage: Stager = npx_stage_skill) -> list[ScanFinding]:
        repo, skill = parse_skill_spec(spec)
        dest = self.skills_dir / skill
        lock = self.skills_lock()
        if skill in self.project.kept()["skills"]:
            raise HarnessError(f"skill {skill!r} is already installed: kept in the project by an earlier task")
        if dest.exists() and skill not in lock:
            raise HarnessError(f"skill {skill!r} already exists in {self.agent.skills_dir} and is not managed by autoharness")
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
        if _uninstall_skill(self.skills_dir / skill, lock[skill]["sha256"], self.dir / "modified-skills" / skill):
            self.log(f"skill `{skill}` was modified after install; kept a backup")
        del lock[skill]
        _write_json(self.dir / "skills.lock.json", lock)
        self.log(f"removed skill `{skill}`")

    # --- lifecycle -----------------------------------------------------------

    def _created_mcp_config(self) -> dict | None:
        created = self.info().get("created_mcp_config", {}).get(self.agent.name)
        if created is None and self.info().get("created_mcp_json"):  # lockfiles from before adapters
            created = {"dir": False}
        return created

    def _keep_all(self) -> dict:
        """Hand everything this task installed over to the project's kept.json."""
        kept = self.project.kept()
        mcp, skills = self.mcp_lock(), self.skills_lock()
        stamp = {"task": self.id, "agent": self.agent.name}
        for name, entry in mcp.items():
            kept["mcp"][name] = entry | stamp | {"created_mcp_config": self._created_mcp_config()}
        for skill, entry in skills.items():
            kept["skills"][skill] = entry | stamp
        self.project._write_kept(kept)
        _write_json(self.dir / "mcp.lock.json", {})
        _write_json(self.dir / "skills.lock.json", {})
        for name in [*mcp, *skills]:
            self.log(f"kept `{name}` in the project")
        return {"mcp": list(mcp), "skills": list(skills)}

    def teardown(self, policy: str = "keep") -> dict:
        """End the task. `keep` leaves its harness installed at project level, `remove` uninstalls it."""
        nothing = {"mcp": [], "skills": []}
        if policy == "keep":
            result = {"removed": nothing, "kept": self._keep_all()}
        elif policy == "remove":
            removed = {"mcp": list(self.mcp_lock()), "skills": list(self.skills_lock())}
            for name in removed["mcp"]:
                self.remove_mcp(name)
            for skill in removed["skills"]:
                self.remove_skill(skill)
            result = {"removed": removed, "kept": nothing}
        else:
            raise HarnessError(f"unknown teardown policy {policy!r}")
        self._update_info(status="kept" if policy == "keep" else "torn-down",
                          finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.log(f"teardown complete (policy: {policy})")
        active = self.project.state / "active"
        if active.exists() and active.read_text("utf-8").strip() == self.id:
            active.unlink()
        return result


class SkillRejected(HarnessError):
    def __init__(self, spec: str, findings: list[ScanFinding]):
        self.spec, self.findings = spec, findings
        lines = "\n".join(f"  [{f.severity}] {f.message} — {f.file}:{f.line}: {f.excerpt}" for f in findings)
        super().__init__(f"skill {spec} failed the static scan:\n{lines}")
