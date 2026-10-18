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
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from autoharness import agents, approvals, transactions
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
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)
    if path.name == TASK_RECORD:
        _write_seal(path, text)


# a task's record and its seal: autoharness signs every write, so an edit made around it shows at teardown
TASK_RECORD, TASK_SEAL = "task.json", "task.sig"


def _write_seal(path: Path, text: str) -> None:
    sig = path.with_name(TASK_SEAL)
    tmp = sig.with_suffix(".sig.tmp")
    tmp.write_text(approvals.seal(text.encode("utf-8")) + "\n", "ascii")
    tmp.replace(sig)


def _seal_matches(path: Path) -> bool | None:
    """Whether a task record is what autoharness wrote; None when there is no seal or no key to tell."""
    sig = path.with_name(TASK_SEAL)
    if not (path.exists() and sig.exists()):
        return None
    # written in text mode: on Windows the file holds CRLF where the sealed text had LF
    return approvals.sealed(path.read_bytes().replace(b"\r\n", b"\n"), sig.read_text("ascii"))


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


STATE_GITIGNORE = "*\n!.gitignore\n!config.json\n!kept.json\n!gateway.json\n!followups.json\n!rulings.json\n"
_OLD_GITIGNORES = {"*\n", "*\n!.gitignore\n!config.json\n!kept.json\n",   # written by older versions
                   "*\n!.gitignore\n!config.json\n!kept.json\n!gateway.json\n",
                   "*\n!.gitignore\n!config.json\n!kept.json\n!gateway.json\n!followups.json\n"}
GATEWAY_NAME = "autoharness"


def ensure_state_dir(state: Path) -> None:
    """Create .autoharness/; its .gitignore keeps task state out of git but lets settings, kept.json, gateway.json, followups.json and rulings.json in."""
    state.mkdir(parents=True, exist_ok=True)
    gitignore = state / ".gitignore"
    if not gitignore.exists() or gitignore.read_text("utf-8") in _OLD_GITIGNORES:
        gitignore.write_text(STATE_GITIGNORE, "utf-8")


def proxyable(config: dict) -> bool:
    """Servers the gateway can run: stdio, or HTTP with explicit headers (no OAuth login)."""
    return "command" in config or (config.get("type") == "http" and bool(config.get("headers")))


def _uninstall_mcp(root: Path, agent: Agent, name: str, created: dict | None) -> None:
    try:
        agent.mcp_store(root).remove(name, created_file=created is not None, created_dir=bool(created and created["dir"]))
    except AgentError as e:
        raise HarnessError(str(e)) from e


# a skill name becomes a directory name: letters, digits, `.`, `_`, `-`; not ending in `.` (Windows drops it,
# so `demo.` would name `demo`) and not a Windows device name
SKILL_NAME = re.compile(r"^(?!(?:con|prn|aux|nul|com\d|lpt\d)(?:\.|$))[A-Za-z0-9_][A-Za-z0-9._-]{0,127}(?<!\.)$",
                        re.IGNORECASE | re.ASCII)


def skill_dir(base: Path, name: str) -> Path:
    """`base / name` for a skill name, refused unless it is a plain directory name."""
    if not SKILL_NAME.match(name):
        raise HarnessError(f"{name!r} is not a plain directory name: not touching it; fix the lockfile or "
                           "kept.json by hand")
    return base / name


DEPS_HEADING = re.compile(r"^#{1,6}\s*(?:dependencies|requirements|prerequisites|зависимости|требования)\b",
                          re.IGNORECASE)
TOOL_NAME = re.compile(r"`([A-Za-z][\w.+-]{1,40})`")


def missing_tools(root: Path) -> list[str]:
    """Commands a skill's SKILL.md lists under a Dependencies-like heading that are not on PATH: a skill can
    fit the constraints and still not run here. A heuristic: a listed library that is not a command shows
    up too, so the list is a warning, never a refusal."""
    try:
        lines = (root / "SKILL.md").read_text("utf-8").splitlines()
    except OSError:
        return []
    names, inside = [], False
    for line in lines:
        if line.lstrip().startswith("#"):
            inside = bool(DEPS_HEADING.match(line.strip()))
        elif inside:
            names += [n for n in TOOL_NAME.findall(line) if n not in names]
    return [n for n in names if shutil.which(n) is None]


def existing_skill_dir(base: Path, name: str) -> Path | None:
    """The folder `name` in `base`, or None. Names come from lockfiles and kept.json, which a pull can bring
    (a commit can add ignored files too), so the name must be spelled exactly as the directory listing spells
    it: the file system's own name folding (case, trailing dots, device names) never lets it stand for
    another folder."""
    path = skill_dir(base, name)
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    if name not in entries:
        return None
    if path.is_symlink() or os.path.realpath(path) != os.path.join(os.path.realpath(base), name):
        raise HarnessError(f"{path} is a link (symlink or junction), not a folder autoharness installed: "
                           "not touching it")
    return path if path.is_dir() else None


def folding_twin(base: Path, name: str) -> str | None:
    """Another entry of `base` that a case-insensitive file system would open for `name` (`Mine` for `mine`)."""
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    if name in entries:
        return None
    return next((e for e in entries if e.casefold() == name.casefold()), None)


def _uninstall_skill(skills: Path, name: str, sha256: str, backups: Path) -> bool:
    """Delete an installed skill; back it up first if it was edited. Returns whether it was."""
    dest, backup = existing_skill_dir(skills, name), skill_dir(backups, name)
    if dest is None:
        return False
    modified = _hash_dir(dest) != sha256
    if modified:
        shutil.copytree(dest, backup, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*RUNTIME_JUNK))
    shutil.rmtree(dest)
    return modified


def parse_skill_spec(spec: str) -> tuple[str, str]:
    """`owner/repo@skill` -> (`owner/repo`, `skill`); `local:<name>` -> (`local`, `<name>`)."""
    if spec.startswith("local:"):
        from autoharness import library  # lazy: library imports env
        library.validate_name(spec[len("local:"):])
        return "local", spec[len("local:"):]
    if "@" not in spec or spec.count("/") < 1:
        raise HarnessError(f"skill spec must look like owner/repo@skill or local:<name>, got {spec!r}")
    repo, skill = spec.rsplit("@", 1)
    if not SKILL_NAME.match(skill):   # it becomes a directory name in the agent's skills dir
        raise HarnessError(f"skill name must be letters, digits, `.`, `_` or `-` and not start with `.`, got {skill!r}")
    return repo, skill


def npx_stage_skill(repo: str, skill: str, stage_dir: Path) -> Path:
    """Install a skill into an isolated staging directory using the `skills` CLI."""
    npx = shutil.which("npx")
    if not npx:
        raise HarnessError("npx not found; install Node.js to add skills")
    cmd = [npx, "-y", "skills@1.7.0", "add", repo, "--skill", skill, "-a", "claude-code", "-y", "--copy"]
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

    def _check_kept(self, kind: str, name: str) -> None:
        """Refuse, before anything changes, a kept skill whose name or folder autoharness must not touch."""
        if kind == "skills":
            entry = self.kept()["skills"][name]
            existing_skill_dir(_agent(entry.get("agent", "claude")).skills_path(self.root), name)

    def remove_kept(self, kind: str, name: str, approved: bool = False) -> dict:
        """Uninstall one thing a finished task kept. `kind` is "mcp" or "skills".

        kept.json may come from a pull, so what no task on this machine kept (a pull, an older autoharness,
        another clone) is uninstalled only with the user's yes (`approved`); without it nothing changes and
        the answer carries `approval_required`."""
        kept = self.kept()
        if name not in kept[kind]:
            raise HarnessError(f"{name!r} is not managed by autoharness in this project")
        self._check_kept(kind, name)
        if not approved and not approvals.kept_here(self.root, kind, name, kept[kind][name]):
            return {"approval_required": name, "entry": kept[kind][name]}
        entry = kept[kind].pop(name)
        agent = _agent(entry.get("agent", "claude"))
        result = {"removed": name}
        if kind == "mcp":
            if entry.get("via") == "gateway":
                self._gateway_forget(name)
            else:
                _uninstall_mcp(self.root, agent, name, entry.get("created_mcp_config"))
        else:
            if _uninstall_skill(agent.skills_path(self.root), name, entry["sha256"], self.state / "modified-skills"):
                result["backup"] = str(self.state / "modified-skills" / name)
        self._write_kept(kept)
        approvals.forget_kept(self.root, kind, name)   # after kept.json: a stale record only means asking later
        return result

    def prune(self, approved: bool = False) -> dict:
        kept = self.kept()
        names = {"mcp": list(kept["mcp"]), "skills": list(kept["skills"])}
        for kind, listed in names.items():   # all refusals first: nothing half done
            for name in listed:
                self._check_kept(kind, name)
        removed, waiting = {"mcp": [], "skills": []}, []
        for kind, listed in names.items():
            for name in listed:
                if "approval_required" in self.remove_kept(kind, name, approved):
                    waiting.append(name)
                else:
                    removed[kind].append(name)
        return removed | ({"approval_required": waiting} if waiting else {})

    def gateway_registry(self) -> dict:
        data = _read_json(self.state / "gateway.json", {})
        data.setdefault("installed", [])
        data.setdefault("servers", {})
        return data

    def save_gateway_registry(self, data: dict) -> None:
        ensure_state_dir(self.state)
        _write_json(self.state / "gateway.json", data)

    def gateway_active(self) -> bool:
        return (self.agent.name in self.gateway_registry()["installed"]
                and self.agent.mcp_store(self.root).has(GATEWAY_NAME))

    def install_gateway(self, server: dict) -> None:
        agent, reg = self.agent, self.gateway_registry()
        store = agent.mcp_store(self.root)
        if store.has(GATEWAY_NAME) and agent.name not in reg["installed"]:
            raise HarnessError(f"MCP server {GATEWAY_NAME!r} already exists in {agent.mcp_file} and is not managed by autoharness")
        path = agent.mcp_path(self.root)
        if not path.exists():
            reg.setdefault("created_config", {})[agent.name] = {"dir": not path.parent.exists()}
        native = agent.translate(server)[0]
        if agent.name == "codex":
            native["env_vars"] = self._gateway_env(reg)
        try:
            rendered = store.preview_add(GATEWAY_NAME, native)
        except AgentError as e:
            raise HarnessError(str(e)) from e
        if agent.name not in reg["installed"]:
            reg["installed"].append(agent.name)
        ensure_state_dir(self.state)
        transactions.commit(self.root, {
            path: rendered, self.state / "gateway.json": json.dumps(reg, indent=2) + "\n"})

    def _gateway_env(self, registry: dict) -> list[str]:
        names = {"AUTOHARNESS_HOME", "AUTOHARNESS_GATEWAY_TIMEOUT"}
        for name, cfg in registry["servers"].items():
            if approvals.approved(self.root, name, cfg):
                names.update(agents.ENV_REF.findall(json.dumps(cfg)))
        return sorted(names)

    def codex_gateway_update(self, registry: dict) -> tuple[dict, list[str]]:
        if "codex" not in registry.get("installed", []):
            return {}, []
        store = agents.get("codex").mcp_store(self.root)
        native = store._parse(store._text()).get("mcp_servers", {}).get(GATEWAY_NAME)
        if native is None:
            return {}, ["Codex gateway is listed in the registry but absent from .codex/config.toml; "
                        "run `autoharness --agent codex gateway install` in this project"]
        old = native.get("env_vars", [])
        names = self._gateway_env(registry)
        added = sorted(set(names) - {v for v in old if isinstance(v, str)})
        if not added:
            return {}, []
        native["env_vars"] = old + added
        return {store.path: store.preview_add(GATEWAY_NAME, native)}, [
            f"Restart Codex to forward new gateway environment variables: {', '.join(added)}; "
            "the running gateway cannot receive new environment variables without a restart"]

    def uninstall_gateway(self) -> None:
        agent, reg = self.agent, self.gateway_registry()
        if agent.name not in reg["installed"]:
            raise HarnessError(f"the gateway is not installed for {agent.name}")
        created = reg.get("created_config", {}).pop(agent.name, None)
        _uninstall_mcp(self.root, agent, GATEWAY_NAME, created)
        reg["installed"].remove(agent.name)
        self.save_gateway_registry(reg)

    def _gateway_forget(self, name: str) -> None:
        reg = self.gateway_registry()
        if reg["servers"].pop(name, None) is not None:
            self.save_gateway_registry(reg)
        approvals.forget(self.root, name)

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

    def init_task(self, text: str, mode: str = "full") -> "Task":
        if (active := self.active_id()) and Task(self, active).info().get("status") == "active":
            raise HarnessError(f"task {active} is still active; run `autoharness teardown` first")
        task_id = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(text.encode()).hexdigest()[:6]
        approvals.note_task(self.root, task_id)   # first: if the record cannot be written, nothing starts
        t = Task(self, task_id)
        _write_json(t.dir / "task.json", {"id": task_id, "task": text, "agent": self.agent.name, "status": "active",
                                          "mode": mode, "started_epoch": time.time(),
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

    def tampered(self) -> list[str]:
        """What changed in this task's records outside autoharness: the record itself (when this machine sealed
        it) and the evidence copies (each kept with its sha256)."""
        record = self.dir / TASK_RECORD
        found = [f"task record changed outside autoharness: {record} is not what autoharness wrote"
                 ] if _seal_matches(record) is False else []
        for e in self.info().get("evidence", []):
            copy = Path(e.get("file", ""))
            if e.get("sha256") and copy.is_file() and hashlib.sha256(copy.read_bytes()).hexdigest() != e["sha256"]:
                found.append(f"evidence #{e['n']} changed after it was recorded: {copy}")
        return found

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

    def record_discovery(self, discovered: list[dict]) -> None:
        self._update_info(discovery=discovered)
        for d in discovered:
            if "skipped" in d:
                self.log(f"discovery skipped: {d['skipped']}")
            elif "error" in d:
                self.log(f"searched {d['kind']} '{d['query']}': failed ({d['error']})")
            else:
                names = ", ".join(r["name"] for r in d["results"]) or "nothing"
                self.log(f"searched {d['kind']} '{d['query']}': {names}")

    def note_search(self, kind: str, query: str, names: list[str]) -> None:
        searches = self.info().get("searches", [])
        searches.append({"kind": kind, "query": query, "results": names})
        self._update_info(searches=searches)
        self.log(f"searched {kind} '{query}': {', '.join(names) or 'nothing'}")

    def record_brief(self, brief: dict) -> None:
        self._update_info(brief=brief)
        self.log("brief: " + "; ".join(f"{k}: {v}" for k, v in brief.items()))

    def planned_skill_problems(self) -> tuple[list[str], list[str]]:
        """(blocking, warnings) for the skills the brief said this task would create."""
        from autoharness import library  # lazy: library imports env
        blocking, warnings = [], []
        lock = self.skills_lock()
        for name in self.info().get("planned_skills", []):
            skill_md = self.skills_dir / name / "SKILL.md"
            if name not in lock:
                blocking.append(f"planned skill `{name}` was never created: run `autoharness skill new {name} "
                                f"--description ...`, fill it in and follow it")
            elif skill_md.exists() and library.FILL_MARKER in skill_md.read_text("utf-8"):
                blocking.append(f"planned skill `{name}` is still an unfilled template: fill in every section")
            elif not library.exists(name):
                warnings.append(f"planned skill `{name}` is not in the library: run `autoharness skill save {name}` "
                                f"and ask the user; say so if they declined")
        return blocking, warnings

    def discovery_ran(self) -> bool:
        info = self.info()
        return bool(info.get("searches")) or any("skipped" not in d for d in info.get("discovery", []))

    def note_capability(self, capability: str, reason: str = "") -> None:
        caps = self.info().get("capabilities", [])
        if capability not in caps:
            caps.append(capability)
            self._update_info(capabilities=caps)
        self.log(f"capability **{capability}** needed" + (f": {reason}" if reason else ""))

    # --- MCP -----------------------------------------------------------------

    def add_mcp(self, name: str, config: dict, *, source: str, trust: dict, permissions: dict,
                direct: bool = False) -> list[str]:
        with transactions.locked(self.project.root):
            transactions.recover(self.project.root)
            return self._add_mcp(name, config, source=source, trust=trust,
                                 permissions=permissions, direct=direct)

    def _add_mcp(self, name: str, config: dict, *, source: str, trust: dict, permissions: dict,
                 direct: bool = False) -> list[str]:
        """Add an MCP server: through the gateway when it is installed and can run it, else into the
        agent's MCP file. Returns translation or gateway environment restart warnings."""
        agent = self.agent
        store = agent.mcp_store(self.project.root)
        path = agent.mcp_path(self.project.root)
        lock = self.mcp_lock()
        if name == GATEWAY_NAME:
            raise HarnessError(f"{GATEWAY_NAME!r} is reserved for the autoharness gateway")
        if name in self.project.kept()["mcp"]:
            raise HarnessError(f"MCP server {name!r} is already installed: kept in the project by an earlier task")
        registry = self.project.gateway_registry()
        if name not in lock and (store.has(name) or name in registry["servers"]):
            raise HarnessError(f"MCP server {name!r} already exists and is not managed by autoharness")
        if not direct and proxyable(config) and self.project.gateway_active():
            approvals.approve(self.project.root, name, config)  # the user approved it for `add`
            registry["servers"][name] = config
            lock[name] = {"config": config, "source": source, "trust": trust, "agent": agent.name, "via": "gateway",
                          "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
            extra, warnings = self.project.codex_gateway_update(registry)
            self._commit_mcp(name, lock, permissions, {
                self.project.state / "gateway.json": json.dumps(registry, indent=2) + "\n", **extra})
            self._audit_install_mcp(name, config, source)
            self.log(f"added MCP `{name}` through the gateway from {source} (trust {trust.get('level')}, {trust.get('score')})")
            return warnings
        created = self.info().get("created_mcp_config", {})
        if not path.exists() and agent.name not in created:
            created[agent.name] = {"dir": not path.parent.exists()}
        native, warnings = agent.translate(config)
        try:
            rendered = store.preview_add(name, native)
        except AgentError as e:
            raise HarnessError(str(e)) from e
        lock[name] = {"config": config, "source": source, "trust": trust, "agent": agent.name,
                      "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        info = self.info() | {"created_mcp_config": created}
        self._commit_mcp(name, lock, permissions, {
            path: rendered, self.dir / "task.json": json.dumps(info, indent=2, ensure_ascii=False) + "\n"})
        self._audit_install_mcp(name, config, source)
        self.log(f"added MCP `{name}` to {agent.mcp_file} from {source} (trust {trust.get('level')}, {trust.get('score')})")
        for w in warnings:
            self.log(f"warning for `{name}`: {w}")
        return warnings

    def _commit_mcp(self, name, lock, permissions, updates):
        grants = _read_json(self.dir / "permissions.json", {})
        grants[f"mcp:{name}"] = permissions
        info_path = self.dir / "task.json"
        info = json.loads(updates[info_path]) if info_path in updates else self.info()
        info["mcp_seen"] = sorted(set(info.get("mcp_seen", [])) | {name})
        updates[info_path] = json.dumps(info, indent=2, ensure_ascii=False) + "\n"
        updates[self.dir / "mcp.lock.json"] = json.dumps(lock, indent=2, ensure_ascii=False) + "\n"
        updates[self.dir / "permissions.json"] = json.dumps(grants, indent=2, ensure_ascii=False) + "\n"
        updates[self.dir / TASK_SEAL] = approvals.seal(updates[info_path].encode("utf-8")) + "\n"
        transactions.commit(self.project.root, updates)

    def _audit_install_mcp(self, name: str, config: dict, source: str) -> None:
        from autoharness import audit
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        audit.append(self, "install", kind="mcp", name=name, source=source,
                     version=config.get("version"), fingerprint=fingerprint)

    def _require_here(self) -> None:
        """Uninstall what the lockfiles name only for a task started on this machine: a commit can add them."""
        if not approvals.task_here(self.project.root, self.id):
            raise HarnessError(f"task {self.id} was not started on this machine (.autoharness/tasks can arrive in "
                               "a commit, or an older autoharness started it): not uninstalling what its lockfiles "
                               "name. End it with `autoharness teardown --keep`; then, after the user's yes, "
                               "`autoharness remove mcp|skill <name> --approve` removes its items one at a time "
                               "(or `autoharness prune --approve` everything kept)")

    def remove_mcp(self, name: str, reason: str = "requested") -> None:
        self._require_here()
        lock = self.mcp_lock()
        if name not in lock:
            raise HarnessError(f"MCP server {name!r} was not added by this task")
        if lock[name].get("via") == "gateway":
            self.project._gateway_forget(name)
        else:
            _uninstall_mcp(self.project.root, self.agent, name, self._created_mcp_config())
        del lock[name]
        _write_json(self.dir / "mcp.lock.json", lock)
        from autoharness import audit
        audit.append(self, "remove", kind="mcp", name=name, reason=reason)
        self.log(f"removed MCP `{name}`")

    # --- skills --------------------------------------------------------------

    def add_skill(self, spec: str, *, trust: dict, force: bool = False,
                  stage: Stager = npx_stage_skill, vetted_sha256: str | None = None) -> list[ScanFinding]:
        """Install a skill. With `vetted_sha256`, only the very files that were vetted: `vet` and `add`
        each download the skill, and it may have changed upstream in between."""
        repo, skill = parse_skill_spec(spec)
        dest = skill_dir(self.skills_dir, skill)
        lock = self.skills_lock()
        if skill in self.project.kept()["skills"]:
            raise HarnessError(f"skill {skill!r} is already installed: kept in the project by an earlier task")
        if (twin := folding_twin(self.skills_dir, skill)) is not None:
            raise HarnessError(f"{self.agent.skills_dir}/{twin} answers to the name {skill!r} on a case-insensitive "
                               "file system: not touching it")
        existing = existing_skill_dir(self.skills_dir, skill)
        if existing is not None and skill not in lock:
            raise HarnessError(f"skill {skill!r} already exists in {self.agent.skills_dir} and is not managed by autoharness")
        if existing is not None and _hash_dir(existing) != lock[skill].get("sha256"):
            # the lockfile may come from a pull: replace only the very files this task installed
            raise HarnessError(f"{self.agent.skills_dir}/{skill} is not what this task installed (edited since, or "
                               "the lockfile says otherwise): not replacing it; remove or rename it yourself")
        with tempfile.TemporaryDirectory(prefix="autoharness-stage-") as tmp:
            staged = stage(repo, skill, Path(tmp))
            if vetted_sha256 and _hash_dir(staged) != vetted_sha256:
                self.log(f"NOT added skill `{spec}`: its files changed since they were vetted")
                raise ContentChanged(spec)
            findings = scan_skill(staged)
            if is_blocking(findings) and not force:
                self.log(f"REJECTED skill `{spec}`: " + "; ".join(f"{f.message} ({f.file}:{f.line})" for f in findings if f.severity == "high"))
                raise SkillRejected(spec, findings)
            if existing is not None:
                shutil.rmtree(existing)
            shutil.copytree(staged, dest)
        lock[skill] = {"spec": spec, "sha256": _hash_dir(dest), "trust": trust,
                       "findings": [f.__dict__ for f in findings], "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _write_json(self.dir / "skills.lock.json", lock)
        self._update_info(skills_seen=sorted(set(self.info().get("skills_seen", [])) | {skill}))
        from autoharness import audit
        audit.append(self, "install", kind="skill", name=skill, source=spec,
                     version=None, fingerprint=lock[skill]["sha256"])
        self._grant(f"skill:{skill}", {"runs_with": "full agent permissions", "scan_findings": len(findings)})
        self.log(f"added skill `{spec}` ({len(findings)} scan findings)")
        return findings

    def remove_skill(self, skill: str, reason: str = "requested") -> None:
        self._require_here()
        lock = self.skills_lock()
        if skill not in lock:
            raise HarnessError(f"skill {skill!r} was not added by this task")
        if _uninstall_skill(self.skills_dir, skill, lock[skill]["sha256"], self.dir / "modified-skills"):
            self.log(f"skill `{skill}` was modified after install; kept a backup")
        del lock[skill]
        _write_json(self.dir / "skills.lock.json", lock)
        from autoharness import audit
        audit.append(self, "remove", kind="skill", name=skill, reason=reason)
        self.log(f"removed skill `{skill}`")

    def new_skill(self, name: str, description: str) -> Path:
        """Scaffold a skill the agent will write itself; it is tracked like any installed skill."""
        from autoharness import library  # lazy: library imports env
        library.validate_name(name)
        dest = self.skills_dir / name
        if dest.exists():
            raise HarnessError(f"{self.agent.skills_dir}/{name} already exists")
        if library.exists(name):
            raise HarnessError(f"skill {name!r} is already in the library; install it with `add skill local:{name}`")
        if name in self.project.kept()["skills"]:
            raise HarnessError(f"skill {name!r} is already kept in this project")
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text(library.template(name, description), "utf-8")
        lock = self.skills_lock()
        lock[name] = {"spec": f"local:{name}", "source": "generated", "sha256": _hash_dir(dest),
                      "trust": {"level": "high", "score": 100}, "findings": [],
                      "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _write_json(self.dir / "skills.lock.json", lock)
        self._update_info(skills_seen=sorted(set(self.info().get("skills_seen", [])) | {name}))
        from autoharness import audit
        audit.append(self, "install", kind="skill", name=name, source="generated",
                     version=None, fingerprint=lock[name]["sha256"])
        self.log(f"created skill `{name}`")
        return dest

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
        here = approvals.task_here(self.project.root, self.id)   # else a later prune asks the user first
        entries = {"mcp": {n: e | stamp | {"created_mcp_config": self._created_mcp_config()} for n, e in mcp.items()},
                   "skills": {n: e | stamp for n, e in skills.items()}}
        for kind, named in entries.items():
            for name, entry in named.items():
                if not here and name in kept[kind]:
                    # a task from a pull must not replace what is kept: the original would drop out of tracking
                    self.log(f"not keeping `{name}`: kept.json already lists it and this task was not started here")
                    continue
                kept[kind][name] = entry
                if here:
                    approvals.note_kept(self.project.root, kind, name, entry)
        self.project._write_kept(kept)
        _write_json(self.dir / "mcp.lock.json", {})
        _write_json(self.dir / "skills.lock.json", {})
        for name in [*mcp, *skills]:
            self.log(f"kept `{name}` in the project")
        return {"mcp": list(mcp), "skills": list(skills)}

    def teardown(self, policy: str = "keep") -> dict:
        """End the task. `keep` leaves its harness installed at project level, `remove` uninstalls it."""
        from autoharness import library  # lazy: library imports env
        nothing = {"mcp": [], "skills": []}
        unsaved = [s for s, e in self.skills_lock().items() if e.get("source") == "generated"
                   and not library.exists(s) and existing_skill_dir(self.skills_dir, s)]
        backups = {}
        if policy == "keep":
            result = {"removed": nothing, "kept": self._keep_all()}
        elif policy == "remove":
            self._require_here()
            for skill in self.skills_lock():   # all refusals first: nothing half torn down
                existing_skill_dir(self.skills_dir, skill)
            for skill in unsaved:
                backups[skill] = skill_dir(self.dir / "generated-skills", skill)
                shutil.copytree(existing_skill_dir(self.skills_dir, skill), backups[skill], dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(*RUNTIME_JUNK))
                self.log(f"skill `{skill}` was never saved to the library; copied to {backups[skill]}")
            removed = {"mcp": list(self.mcp_lock()), "skills": list(self.skills_lock())}
            for name in removed["mcp"]:
                self.remove_mcp(name, reason="teardown")
            for skill in removed["skills"]:
                self.remove_skill(skill, reason="teardown")
            result = {"removed": removed, "kept": nothing}
        else:
            raise HarnessError(f"unknown teardown policy {policy!r}")
        if unsaved:
            result["unsaved_generated"] = [{"name": s, "backup": str(backups[s]) if s in backups else None}
                                           for s in unsaved]
        self._update_info(status="kept" if policy == "keep" else "torn-down",
                          mcp_installed=sorted(set(self.info().get("mcp_seen", [])) |
                                               set(result["kept"]["mcp"] + result["removed"]["mcp"])),
                          skills_installed=sorted(set(self.info().get("skills_seen", [])) |
                                                  set(result["kept"]["skills"] + result["removed"]["skills"])),
                          finished=time.strftime("%Y-%m-%dT%H:%M:%S"), finished_epoch=time.time())
        self.log(f"teardown complete (policy: {policy})")
        approvals.forget_task(self.project.root, self.id)   # nothing left to uninstall through its lockfiles
        active = self.project.state / "active"
        if active.exists() and active.read_text("utf-8").strip() == self.id:
            active.unlink()
        return result


class ContentChanged(HarnessError):
    def __init__(self, spec: str):
        self.spec = spec
        super().__init__(f"skill {spec} changed since it was vetted")


class SkillRejected(HarnessError):
    def __init__(self, spec: str, findings: list[ScanFinding]):
        self.spec, self.findings = spec, findings
        lines = "\n".join(f"  [{f.severity}] {f.message} — {f.file}:{f.line}: {f.excerpt}" for f in findings)
        super().__init__(f"skill {spec} failed the static scan:\n{lines}")
