"""Swarm DAG multi-agent orchestration engine for AutoHarness.

Zero-runtime dependencies (pure Python standard library).
Provides:
  - Directed Acyclic Graph (DAG) task definition and topological scheduling (via graphlib).
  - Parallel worker pool execution with concurrency limits and barrier synchronization.
  - Role-based subtask coordination (Researcher, Coder, Verifier, Critic).
  - Hard verification gates: downstream tasks are blocked if dependencies fail.
  - Automatic synchronization with in-process SQLite FTS5 memory (.autoharness/memory.db).
  - Crash-proof state persistence in .autoharness/swarm/<plan_id>.json.
"""
from __future__ import annotations

from contextlib import contextmanager
import graphlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


class IntentLeaseConflictError(Exception):
    """Raised when an intent lease cannot be acquired due to resource collision with an active lease."""
    pass


@dataclass
class IntentLease:
    lease_id: str
    plan_id: str
    task_id: str
    paths: list[str]
    exclusive: bool = True
    acquired_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: str = "active"  # active, released, expired

    def is_expired(self) -> bool:
        return self.status == "active" and time.time() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentLease:
        return cls(
            lease_id=str(data["lease_id"]),
            plan_id=str(data.get("plan_id") or ""),
            task_id=str(data["task_id"]),
            paths=list(data.get("paths") or []),
            exclusive=bool(data.get("exclusive", True)),
            acquired_at=float(data.get("acquired_at") or time.time()),
            expires_at=float(data.get("expires_at") or 0.0),
            status=str(data.get("status") or "active"),
        )


@dataclass
class SwarmReceipt:
    receipt_id: str
    plan_id: str
    task_id: str
    role: str
    issued_at: float
    issuer: str
    artifacts_hashes: dict[str, str]
    passed_checks: int
    total_checks: int
    signature: str
    status: str = "valid"  # valid, tampered, revoked

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SwarmReceipt:
        return cls(
            receipt_id=str(data["receipt_id"]),
            plan_id=str(data.get("plan_id") or ""),
            task_id=str(data["task_id"]),
            role=str(data.get("role") or "coder"),
            issued_at=float(data.get("issued_at") or time.time()),
            issuer=str(data.get("issuer") or "barrier_sentinel"),
            artifacts_hashes=dict(data.get("artifacts_hashes") or {}),
            passed_checks=int(data.get("passed_checks") or 0),
            total_checks=int(data.get("total_checks") or 0),
            signature=str(data.get("signature") or ""),
            status=str(data.get("status") or "valid"),
        )


@dataclass
class SwarmTask:
    id: str
    name: str
    role: str = "coder"  # researcher, coder, verifier, critic, coordinator
    depends_on: list[str] = field(default_factory=list)
    command: str | None = None
    prompt: str | None = None
    timeout: float = 60.0
    status: str = "pending"  # pending, running, completed, failed, blocked, skipped
    result: dict[str, Any] = field(default_factory=dict)
    expected_artifacts: list[str] = field(default_factory=list)
    barrier_assertions: list[dict[str, Any]] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    isolate_worktree: bool = False  # run in isolated git worktree sandbox (opencode-agent-teams)
    intent_paths: list[str] = field(default_factory=list)  # Morrow intent lease resource paths
    exclusive_lease: bool = True  # exclusive lock on intent_paths while executing
    lease_id: str | None = None  # active lease ID when leased
    receipt: dict[str, Any] | None = None  # Receipt-Gated Completion token (Morrow)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SwarmTask:
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            role=str(data.get("role") or "coder"),
            depends_on=list(data.get("depends_on") or []),
            command=data.get("command"),
            prompt=data.get("prompt"),
            timeout=float(data.get("timeout") or 60.0),
            status=str(data.get("status") or "pending"),
            result=dict(data.get("result") or {}),
            expected_artifacts=list(data.get("expected_artifacts") or []),
            barrier_assertions=list(data.get("barrier_assertions") or []),
            allowed_paths=list(data.get("allowed_paths") or []),
            isolate_worktree=bool(data.get("isolate_worktree", False)),
            intent_paths=list(data.get("intent_paths") or []),
            exclusive_lease=bool(data.get("exclusive_lease", True)),
            lease_id=data.get("lease_id"),
            receipt=dict(data.get("receipt")) if data.get("receipt") else None,
        )


@dataclass
class SwarmPlan:
    id: str
    title: str
    description: str = ""
    tasks: dict[str, SwarmTask] = field(default_factory=dict)
    max_parallel: int = 4
    status: str = "pending"  # pending, running, completed, failed
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    isolate_worktrees: bool = False  # global git worktree isolation for tasks
    enforce_intent_leases: bool = True  # enforce Morrow intent lease verification
    receipt_gated: bool = True  # enforce Morrow receipt-gated completion

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "max_parallel": self.max_parallel,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "isolate_worktrees": self.isolate_worktrees,
            "enforce_intent_leases": self.enforce_intent_leases,
            "receipt_gated": self.receipt_gated,
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SwarmPlan:
        plan = cls(
            id=str(data["id"]),
            title=str(data.get("title") or data["id"]),
            description=str(data.get("description") or ""),
            max_parallel=int(data.get("max_parallel") or 4),
            status=str(data.get("status") or "pending"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            isolate_worktrees=bool(data.get("isolate_worktrees", False)),
            enforce_intent_leases=bool(data.get("enforce_intent_leases", True)),
            receipt_gated=bool(data.get("receipt_gated", True)),
        )
        raw_tasks = data.get("tasks") or {}
        if isinstance(raw_tasks, list):
            for item in raw_tasks:
                task = SwarmTask.from_dict(item)
                plan.tasks[task.id] = task
        elif isinstance(raw_tasks, dict):
            for tid, item in raw_tasks.items():
                if "id" not in item:
                    item["id"] = tid
                plan.tasks[tid] = SwarmTask.from_dict(item)
        return plan


def get_swarm_dir(project_root: Path) -> Path:
    swarm_dir = project_root / ".autoharness" / "swarm"
    swarm_dir.mkdir(parents=True, exist_ok=True)
    return swarm_dir


_SWARM_THREAD_LOCK = threading.RLock()


@contextmanager
def swarm_persistence_lock(project_root: Path):
    """Serialize plan persistence operations to prevent Windows os.replace permission errors and race conditions."""
    swarm_dir = get_swarm_dir(project_root)
    with _SWARM_THREAD_LOCK:
        lock_file = swarm_dir / ".swarm_persistence.lock"
        with lock_file.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + 20.0
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for swarm persistence lock")
                    time.sleep(0.01)
            try:
                yield
            finally:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


class IntentLeaseManager:
    """Morrow-inspired Intent Lease Manager for Swarm DAG.
    
    Provides time-bounded resource locking (TTL) over file paths to prevent concurrency races
    between parallel subagent tasks.
    """
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.leases_file = get_swarm_dir(self.project_root) / "leases.json"

    def _normalize_path(self, path: str) -> str:
        p = Path(path).as_posix().strip("/").lower()
        return p

    def _paths_collide(self, p1: str, p2: str) -> bool:
        n1 = self._normalize_path(p1)
        n2 = self._normalize_path(p2)
        return n1 == n2 or n1.startswith(n2 + "/") or n2.startswith(n1 + "/")

    def _load_leases(self) -> list[IntentLease]:
        with swarm_persistence_lock(self.project_root):
            if not self.leases_file.is_file():
                return []
            try:
                data = json.loads(self.leases_file.read_text("utf-8"))
                leases = [IntentLease.from_dict(item) for item in data]
                now = time.time()
                for l in leases:
                    if l.status == "active" and now > l.expires_at:
                        l.status = "expired"
                return leases
            except (json.JSONDecodeError, OSError):
                return []

    def _save_leases(self, leases: list[IntentLease]) -> None:
        with swarm_persistence_lock(self.project_root):
            active = [l for l in leases if l.status == "active"]
            history = [l for l in leases if l.status != "active"][-150:]
            data = [l.to_dict() for l in (history + active)]
            temp = self.leases_file.parent / f".tmp_leases_{uuid.uuid4().hex[:6]}_{os.getpid()}.json"
            try:
                temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                os.replace(temp, self.leases_file)
            finally:
                if temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass

    def acquire_lease(
        self,
        plan_id: str,
        task_id: str,
        paths: list[str],
        exclusive: bool = True,
        ttl: float = 120.0,
    ) -> IntentLease:
        with _SWARM_THREAD_LOCK:
            leases = self._load_leases()
            now = time.time()
            active_leases = [l for l in leases if l.status == "active" and now <= l.expires_at]

            norm_paths = [self._normalize_path(p) for p in paths if p.strip()]
            if norm_paths:
                for other in active_leases:
                    if other.task_id == task_id:
                        continue
                    if exclusive or other.exclusive:
                        for p in norm_paths:
                            for op in other.paths:
                                if self._paths_collide(p, op):
                                    raise IntentLeaseConflictError(
                                        f"Intent lease collision on path {p!r}: locked by task {other.task_id!r} "
                                        f"(lease {other.lease_id!r}, expires in {int(other.expires_at - now)}s)"
                                    )

            lease = IntentLease(
                lease_id=f"lease_{uuid.uuid4().hex[:10]}",
                plan_id=plan_id,
                task_id=task_id,
                paths=norm_paths,
                exclusive=exclusive,
                acquired_at=now,
                expires_at=now + max(ttl, 0.001),
                status="active",
            )
            leases.append(lease)
            self._save_leases(leases)
            return lease

    def release_lease(self, lease_id: str) -> bool:
        with _SWARM_THREAD_LOCK:
            leases = self._load_leases()
            found = False
            for l in leases:
                if l.lease_id == lease_id and l.status == "active":
                    l.status = "released"
                    found = True
            if found:
                self._save_leases(leases)
            return found

    def list_active_leases(self, plan_id: str | None = None) -> list[dict[str, Any]]:
        leases = self._load_leases()
        now = time.time()
        active = [l for l in leases if l.status == "active" and now <= l.expires_at]
        if plan_id:
            active = [l for l in active if l.plan_id == plan_id]
        return [l.to_dict() for l in active]


def compute_receipt_signature(
    plan_id: str,
    task_id: str,
    artifacts_hashes: dict[str, str],
    passed_checks: int,
    total_checks: int,
    issued_at: float,
) -> str:
    """Compute cryptographic SHA256 digest signature ensuring tamper-proof receipt."""
    sorted_arts = ";".join(f"{k}={v}" for k, v in sorted(artifacts_hashes.items()))
    payload = f"RECEIPT:{plan_id}:{task_id}:{sorted_arts}:{passed_checks}:{total_checks}:{int(issued_at)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def issue_completion_receipt(
    task: SwarmTask,
    plan_id: str,
    project_root: Path,
    barrier_report: dict[str, Any],
    issuer: str = "barrier_sentinel",
) -> SwarmReceipt:
    """Issue a verified SwarmReceipt for completed task with artifact hashes (Morrow receipt-gated completion)."""
    root = project_root.resolve()
    art_hashes = {}
    for art in task.expected_artifacts:
        f_path = (root / art).resolve()
        if f_path.is_file():
            art_hashes[str(Path(art).as_posix())] = compute_file_sha256(f_path)

    now = time.time()
    passed = barrier_report.get("passed_count", 0)
    total = barrier_report.get("total_checks", 0)
    sig = compute_receipt_signature(plan_id, task.id, art_hashes, passed, total, now)

    receipt = SwarmReceipt(
        receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
        plan_id=plan_id,
        task_id=task.id,
        role=task.role,
        issued_at=now,
        issuer=issuer,
        artifacts_hashes=art_hashes,
        passed_checks=passed,
        total_checks=total,
        signature=sig,
        status="valid",
    )

    # Persist receipt
    receipts_dir = get_swarm_dir(project_root) / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_file = receipts_dir / f"{plan_id}_{task.id}.json"
    try:
        receipt_file.write_text(json.dumps(receipt.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass

    return receipt


def verify_completion_receipt(
    receipt_data: dict[str, Any] | SwarmReceipt,
    project_root: Path,
    task: SwarmTask | None = None,
) -> dict[str, Any]:
    """Verify the integrity and authenticity of a task completion receipt."""
    if isinstance(receipt_data, dict):
        receipt = SwarmReceipt.from_dict(receipt_data)
    else:
        receipt = receipt_data

    expected_sig = compute_receipt_signature(
        receipt.plan_id,
        receipt.task_id,
        receipt.artifacts_hashes,
        receipt.passed_checks,
        receipt.total_checks,
        receipt.issued_at,
    )

    violations = []
    if receipt.signature != expected_sig:
        violations.append("Receipt signature mismatch (forged or modified token)")

    root = project_root.resolve()
    for art, exp_hash in receipt.artifacts_hashes.items():
        f_path = (root / art).resolve()
        if not f_path.is_file():
            violations.append(f"Receipt artifact missing on disk: '{art}'")
        else:
            curr_hash = compute_file_sha256(f_path)
            if curr_hash != exp_hash:
                violations.append(f"Artifact '{art}' content was tampered after receipt issue")

    is_valid = len(violations) == 0
    return {
        "valid": is_valid,
        "receipt_id": receipt.receipt_id,
        "task_id": receipt.task_id,
        "status": "valid" if is_valid else "tampered",
        "violations": violations,
        "receipt": receipt.to_dict(),
    }


class SwarmWorktreeManager:
    """opencode-agent-teams inspired Git Worktree Sandboxing for Swarm DAG.
    
    Zero external runtime dependencies (pure Python standard library).
    Creates isolated worktrees for parallel subagent executions to eliminate file races,
    merging verified changes back to the main working tree upon completion.
    """
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.worktrees_dir = (self.project_root / ".autoharness" / "worktrees").resolve()
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    def is_git_repo(self) -> bool:
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            return res.returncode == 0 and res.stdout.strip() == "true"
        except Exception:
            return False

    def create_worktree(self, task_id: str, plan_id: str | None = None) -> Path:
        """Create an isolated worktree or fallback temporary sandbox."""
        clean_name = f"{plan_id or 'plan'}_{task_id}_{uuid.uuid4().hex[:6]}"
        wt_dir = (self.worktrees_dir / clean_name).resolve()

        created_git_wt = False
        if self.is_git_repo():
            try:
                res = subprocess.run(
                    ["git", "worktree", "add", "--detach", str(wt_dir), "HEAD"],
                    cwd=str(self.project_root),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if res.returncode == 0 and wt_dir.is_dir():
                    created_git_wt = True
            except Exception:
                pass

        if not created_git_wt:
            wt_dir.mkdir(parents=True, exist_ok=True)

        # Synchronize non-hidden workspace files from project_root into worktree
        try:
            for item in self.project_root.iterdir():
                if item.name.startswith("."):
                    continue
                dest = wt_dir / item.name
                if item.is_file():
                    shutil.copy2(item, dest)
                elif item.is_dir() and not dest.exists():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
        except Exception:
            pass

        return wt_dir

    def merge_worktree_artifacts(
        self,
        worktree_dir: Path,
        allowed_paths: list[str] | None = None,
        expected_artifacts: list[str] | None = None,
    ) -> list[str]:
        """Merge authorized changes from worktree back into project_root.
        
        Returns list of merged relative paths.
        """
        merged: list[str] = []
        wt_root = worktree_dir.resolve()
        proj_root = self.project_root.resolve()

        if not wt_root.is_dir() or wt_root == proj_root:
            return merged

        targets_to_check = set(expected_artifacts or []) | set(allowed_paths or [])

        if targets_to_check:
            for target_rel in targets_to_check:
                src = (wt_root / target_rel).resolve()
                dst = (proj_root / target_rel).resolve()
                try:
                    dst.relative_to(proj_root)
                except ValueError:
                    continue  # protect against path traversal

                if src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    merged.append(str(Path(target_rel).as_posix()))
        else:
            if self.is_git_repo():
                try:
                    res = subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=str(wt_root),
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if res.returncode == 0:
                        for line in res.stdout.splitlines():
                            if len(line) >= 4:
                                rel = line[3:].strip()
                                if " -> " in rel:
                                    rel = rel.split(" -> ")[1].strip()
                                src = (wt_root / rel).resolve()
                                dst = (proj_root / rel).resolve()
                                try:
                                    dst.relative_to(proj_root)
                                except ValueError:
                                    continue
                                if src.is_file():
                                    dst.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(src, dst)
                                    merged.append(str(Path(rel).as_posix()))
                except Exception:
                    pass

        return merged

    def cleanup_worktree(self, worktree_dir: Path) -> None:
        """Remove worktree safely."""
        wt_root = worktree_dir.resolve()
        proj_root = self.project_root.resolve()
        if not wt_root.is_dir() or wt_root == proj_root:
            return

        if self.is_git_repo():
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_root)],
                    cwd=str(proj_root),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except Exception:
                pass

        if wt_root.exists():
            for _ in range(3):
                try:
                    shutil.rmtree(wt_root, ignore_errors=True)
                    break
                except Exception:
                    time.sleep(0.05)


def save_swarm_plan(plan: SwarmPlan, project_root: Path) -> Path:
    """Save Swarm plan atomically using a temporary file and os.replace under persistence lock."""
    with swarm_persistence_lock(project_root):
        plan.updated_at = time.time()
        swarm_dir = get_swarm_dir(project_root)
        file_path = swarm_dir / f"{plan.id}.json"
        temp_path = swarm_dir / f".tmp_{plan.id}_{os.getpid()}_{int(time.monotonic() * 1000)}_{uuid.uuid4().hex[:6]}.json"
        data_str = json.dumps(plan.to_dict(), indent=2, ensure_ascii=False) + "\n"
        try:
            temp_path.write_text(data_str, encoding="utf-8")
            os.replace(temp_path, file_path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
        return file_path


def load_swarm_plan(plan_id_or_path: str | Path, project_root: Path) -> SwarmPlan:
    with swarm_persistence_lock(project_root):
        path = Path(plan_id_or_path)
        if not path.is_file():
            swarm_dir = get_swarm_dir(project_root)
            candidate = swarm_dir / f"{plan_id_or_path}.json"
            if candidate.is_file():
                path = candidate
            else:
                candidate2 = swarm_dir / str(plan_id_or_path)
                if candidate2.is_file():
                    path = candidate2
                else:
                    raise FileNotFoundError(f"Swarm plan file not found: {plan_id_or_path}")

        data = json.loads(path.read_text(encoding="utf-8"))
        return SwarmPlan.from_dict(data)


def list_swarm_plans(project_root: Path) -> list[dict[str, Any]]:
    with swarm_persistence_lock(project_root):
        swarm_dir = get_swarm_dir(project_root)
        plans = []
        for f in swarm_dir.glob("*.json"):
            if f.name.startswith(".tmp_") or f.name in ("leases.json", "receipts.json"):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                tasks = data.get("tasks", {})
                total = len(tasks)
                completed = sum(1 for t in (tasks.values() if isinstance(tasks, dict) else tasks) if t.get("status") == "completed")
                failed = sum(1 for t in (tasks.values() if isinstance(tasks, dict) else tasks) if t.get("status") == "failed")
                plans.append({
                    "id": data.get("id", f.stem),
                    "title": data.get("title", ""),
                    "status": data.get("status", "unknown"),
                    "total_tasks": total,
                    "completed": completed,
                    "failed": failed,
                    "updated_at": data.get("updated_at", f.stat().st_mtime),
                    "path": str(f),
                })
            except (json.JSONDecodeError, OSError, KeyError, TypeError):
                continue
        plans.sort(key=lambda x: x["updated_at"], reverse=True)
        return plans


def validate_dag(plan: SwarmPlan) -> list[str]:
    """Validate that the plan has all declared dependencies and no cyclic relationships.

    Returns the topological order of task IDs.
    Raises ValueError on missing dependencies or circular graphs.
    """
    for tid, task in plan.tasks.items():
        for dep in task.depends_on:
            if dep not in plan.tasks:
                raise ValueError(f"Task {tid!r} depends on non-existent task {dep!r}")

    # Topological sort & cycle detection using standard library graphlib
    # TopologicalSorter expects mapping: node -> set_of_predecessors
    graph: dict[str, set[str]] = {tid: set(t.depends_on) for tid, t in plan.tasks.items()}
    ts = graphlib.TopologicalSorter(graph)
    try:
        ts.prepare()
    except graphlib.CycleError as e:
        cycle_nodes = e.args[1] if len(e.args) > 1 else []
        cycle_str = " -> ".join(cycle_nodes) if cycle_nodes else str(e)
        raise ValueError(f"Cycle detected in swarm DAG: {cycle_str}") from e

    order: list[str] = []
    while ts.is_active():
        ready = ts.get_ready()
        order.extend(ready)
        ts.done(*ready)
    return order


def compute_execution_tiers(plan: SwarmPlan) -> list[list[str]]:
    """Compute generation tiers of independent tasks that can execute in parallel.

    Tier 0: tasks with no dependencies.
    Tier N+1: tasks whose dependencies are all in Tier <= N.
    """
    validate_dag(plan)
    remaining = dict(plan.tasks)
    tiers: list[list[str]] = []
    resolved: set[str] = set()

    while remaining:
        current_tier = [
            tid for tid, task in remaining.items()
            if set(task.depends_on).issubset(resolved)
        ]
        if not current_tier:
            raise ValueError("Unresolvable cyclic dependency detected during tier computation")
        tiers.append(current_tier)
        resolved.update(current_tier)
        for tid in current_tier:
            del remaining[tid]
    return tiers


def validate_task_barriers(
    task: SwarmTask,
    project_root: Path,
    execution_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate contract barriers, expected artifacts, and role isolation for a swarm task.

    Returns:
        dict containing 'status' ('passed'|'failed'), 'violations', 'passed_count', 'total_checks'.
    """
    violations: list[dict[str, Any]] = []
    passed_count = 0
    total_checks = 0

    root = project_root.resolve()

    # 1. Check expected artifacts
    for art_path in task.expected_artifacts:
        total_checks += 1
        target = (root / art_path).resolve()
        # Security: protect against directory traversal outside project root
        try:
            target.relative_to(root)
        except ValueError:
            violations.append({
                "kind": "path_traversal",
                "target": art_path,
                "message": f"Artifact path {art_path!r} escapes project root",
            })
            continue

        if not target.exists():
            violations.append({
                "kind": "missing_artifact",
                "target": art_path,
                "message": f"Expected artifact '{art_path}' not found on disk",
            })
        elif not target.is_file():
            violations.append({
                "kind": "invalid_artifact_type",
                "target": art_path,
                "message": f"Expected artifact '{art_path}' is a directory, not a file",
            })
        elif target.stat().st_size == 0:
            violations.append({
                "kind": "empty_artifact",
                "target": art_path,
                "message": f"Expected artifact '{art_path}' is empty (0 bytes)",
            })
        else:
            passed_count += 1

    # 2. Check barrier assertions
    for assertion in task.barrier_assertions:
        total_checks += 1
        a_type = assertion.get("type") or assertion.get("kind")
        target_rel = assertion.get("target") or assertion.get("file")
        target = (root / target_rel).resolve() if target_rel else None

        if a_type == "file_exists":
            if target and target.is_file():
                passed_count += 1
            else:
                violations.append({
                    "kind": "assertion_failure",
                    "assertion": "file_exists",
                    "target": target_rel,
                    "message": f"Barrier assertion 'file_exists' failed for '{target_rel}'",
                })
        elif a_type == "file_non_empty":
            if target and target.is_file() and target.stat().st_size > 0:
                passed_count += 1
            else:
                violations.append({
                    "kind": "assertion_failure",
                    "assertion": "file_non_empty",
                    "target": target_rel,
                    "message": f"Barrier assertion 'file_non_empty' failed for '{target_rel}'",
                })
        elif a_type == "file_contains":
            pattern = assertion.get("pattern") or assertion.get("text") or ""
            if not target or not target.is_file():
                violations.append({
                    "kind": "assertion_failure",
                    "assertion": "file_contains",
                    "target": target_rel,
                    "message": f"File '{target_rel}' does not exist for pattern check",
                })
            else:
                try:
                    if target.stat().st_size > 10_000_000:
                        violations.append({
                            "kind": "assertion_failure",
                            "assertion": "file_contains",
                            "target": target_rel,
                            "message": f"File '{target_rel}' exceeds safe read size limit",
                        })
                    else:
                        content = target.read_text("utf-8", errors="replace")
                        if pattern in content:
                            passed_count += 1
                        else:
                            violations.append({
                                "kind": "assertion_failure",
                                "assertion": "file_contains",
                                "target": target_rel,
                                "message": f"File '{target_rel}' does not contain expected pattern: {pattern!r}",
                            })
                except OSError as e:
                    violations.append({
                        "kind": "assertion_failure",
                        "assertion": "file_contains",
                        "target": target_rel,
                        "message": f"Failed to read '{target_rel}': {e}",
                    })
        elif a_type == "min_lines":
            min_lines = int(assertion.get("count") or assertion.get("lines") or 1)
            if not target or not target.is_file():
                violations.append({
                    "kind": "assertion_failure",
                    "assertion": "min_lines",
                    "target": target_rel,
                    "message": f"File '{target_rel}' not found for min_lines check",
                })
            else:
                try:
                    content = target.read_text("utf-8", errors="replace")
                    line_count = len([l for l in content.splitlines() if l.strip()])
                    if line_count >= min_lines:
                        passed_count += 1
                    else:
                        violations.append({
                            "kind": "assertion_failure",
                            "assertion": "min_lines",
                            "target": target_rel,
                            "message": f"File '{target_rel}' has only {line_count} non-empty lines (expected >= {min_lines})",
                        })
                except OSError as e:
                    violations.append({
                        "kind": "assertion_failure",
                        "assertion": "min_lines",
                        "target": target_rel,
                        "message": f"Failed to read '{target_rel}': {e}",
                    })
        elif a_type == "json_valid":
            if not target or not target.is_file():
                violations.append({
                    "kind": "assertion_failure",
                    "assertion": "json_valid",
                    "target": target_rel,
                    "message": f"File '{target_rel}' not found for json validation",
                })
            else:
                try:
                    json.loads(target.read_text("utf-8", errors="replace"))
                    passed_count += 1
                except (json.JSONDecodeError, OSError) as e:
                    violations.append({
                        "kind": "assertion_failure",
                        "assertion": "json_valid",
                        "target": target_rel,
                        "message": f"File '{target_rel}' contains invalid JSON: {e}",
                    })
        elif a_type == "exit_code":
            expected_code = int(assertion.get("code", 0))
            actual_code = execution_result.get("returncode") if execution_result else None
            if actual_code == expected_code:
                passed_count += 1
            else:
                violations.append({
                    "kind": "assertion_failure",
                    "assertion": "exit_code",
                    "message": f"Expected returncode {expected_code}, got {actual_code}",
                })
        else:
            violations.append({
                "kind": "unknown_assertion",
                "assertion": a_type,
                "message": f"Unknown barrier assertion type: {a_type!r}",
            })

    # 3. Check role boundaries if allowed_paths or role restrictions apply
    if task.allowed_paths:
        total_checks += 1
        allowed_normalized = [str(Path(p).as_posix()).rstrip("/") for p in task.allowed_paths]
        for art in task.expected_artifacts:
            art_posix = str(Path(art).as_posix())
            if not any(art_posix == a or art_posix.startswith(a + "/") for a in allowed_normalized):
                violations.append({
                    "kind": "role_violation",
                    "role": task.role,
                    "target": art,
                    "message": f"Task role '{task.role}' declared artifact '{art}' outside allowed_paths {task.allowed_paths}",
                })
        if not any(v.get("kind") == "role_violation" for v in violations):
            passed_count += 1

    # Role specific constraints: researcher/critic cannot author implementation source code
    if task.role in ("researcher", "critic"):
        code_exts = {".py", ".ts", ".js", ".go", ".rs", ".java", ".c", ".cpp"}
        for art in task.expected_artifacts:
            p_obj = Path(art)
            if p_obj.suffix.lower() in code_exts and not str(art).startswith((".autoharness", "scratch")):
                total_checks += 1
                violations.append({
                    "kind": "role_boundary_breach",
                    "role": task.role,
                    "target": art,
                    "message": f"Role '{task.role}' is restricted from authoring implementation source file '{art}'",
                })

    is_passed = len(violations) == 0
    return {
        "status": "passed" if is_passed else "failed",
        "task_id": task.id,
        "task_name": task.name,
        "role": task.role,
        "violations": violations,
        "passed_count": passed_count,
        "total_checks": total_checks,
    }


def verify_swarm_plan_contracts(plan: SwarmPlan, project_root: Path) -> dict[str, Any]:
    """Verify DAG plan structure, topological constraints, and artifact barrier contracts."""
    root = project_root.resolve()
    dag_errors: list[str] = []
    topo_order: list[str] = []
    try:
        topo_order = validate_dag(plan)
    except ValueError as e:
        dag_errors.append(str(e))

    task_reports: dict[str, Any] = {}
    total_violations = 0
    for tid, t in plan.tasks.items():
        b_res = validate_task_barriers(t, root, t.result)
        # Check receipt integrity if task completed and plan is receipt_gated
        if t.status == "completed" and plan.receipt_gated:
            if t.receipt:
                rcpt_v = verify_completion_receipt(t.receipt, root, t)
                if not rcpt_v["valid"]:
                    for viol in rcpt_v["violations"]:
                        b_res["violations"].append({
                            "kind": "receipt_violation",
                            "message": viol,
                        })
            elif t.expected_artifacts:
                b_res["violations"].append({
                    "kind": "missing_receipt",
                    "message": "Task marked completed but lacks a verified SwarmReceipt",
                })
        task_reports[tid] = b_res
        total_violations += len(b_res["violations"])

    all_passed = len(dag_errors) == 0 and total_violations == 0
    return {
        "status": "passed" if all_passed else "failed",
        "plan_id": plan.id,
        "title": plan.title,
        "dag_valid": len(dag_errors) == 0,
        "dag_errors": dag_errors,
        "topological_order": topo_order,
        "total_tasks": len(plan.tasks),
        "total_violations": total_violations,
        "task_reports": task_reports,
    }


def format_swarm_verify_report(report: dict[str, Any]) -> str:
    """Format Swarm contract barrier verification results into clean Markdown."""
    lines = [
        f"# Swarm Plan Contract Verification: `{report.get('plan_id', 'unknown')}`",
        f"- **Title:** {report.get('title', 'N/A')}",
        f"- **Overall Status:** `{'PASSED' if report.get('status') == 'passed' else 'FAILED'}`",
        f"- **DAG Valid:** {'yes' if report.get('dag_valid') else 'no'}",
        f"- **Total Violations:** {report.get('total_violations', 0)}",
    ]
    if report.get("dag_errors"):
        lines.append("\n## DAG Topology Errors")
        for err in report["dag_errors"]:
            lines.append(f"- ❌ {err}")

    lines.append("\n## Task Contract Barriers")
    for tid, tr in report.get("task_reports", {}).items():
        st_icon = "✅" if tr.get("status") == "passed" else "❌"
        lines.append(f"### {st_icon} Task `{tid}` [{tr.get('role', 'coder')}]: {tr.get('task_name', tid)}")
        lines.append(f"- **Checks:** {tr.get('passed_count', 0)} / {tr.get('total_checks', 0)} passed")
        if tr.get("violations"):
            for v in tr["violations"]:
                lines.append(f"  - ⚠️ `[{v.get('kind')}]` {v.get('message')}")

    return "\n".join(lines).strip()


def default_task_executor(task: SwarmTask, project_root: Path, execution_root: Path | None = None) -> dict[str, Any]:
    """Execute a single task node: run command or evaluate step in execution_root (or project_root)."""
    effective_root = (execution_root or project_root).resolve()
    start_time = time.monotonic()
    if task.command:
        try:
            res = subprocess.run(
                task.command,
                shell=True,
                cwd=str(effective_root),
                capture_output=True,
                text=True,
                timeout=task.timeout,
            )
            duration = round(time.monotonic() - start_time, 3)
            return {
                "returncode": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr,
                "duration": duration,
                "success": res.returncode == 0,
                "cwd": str(effective_root),
            }
        except subprocess.TimeoutExpired as e:
            duration = round(time.monotonic() - start_time, 3)
            return {
                "returncode": -1,
                "stdout": e.stdout or "",
                "stderr": f"Execution timed out after {task.timeout}s",
                "duration": duration,
                "success": False,
                "cwd": str(effective_root),
            }
        except Exception as e:
            duration = round(time.monotonic() - start_time, 3)
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": str(e),
                "duration": duration,
                "success": False,
                "cwd": str(effective_root),
            }
    elif task.prompt:
        duration = round(time.monotonic() - start_time, 3)
        return {
            "returncode": 0,
            "stdout": f"Prompt evaluated for role [{task.role}]: {task.prompt}",
            "stderr": "",
            "duration": duration,
            "success": True,
            "cwd": str(effective_root),
        }
    else:
        return {
            "returncode": 0,
            "stdout": "No command or prompt specified; task marked as completed.",
            "stderr": "",
            "duration": 0.0,
            "success": True,
            "cwd": str(effective_root),
        }


def execute_swarm_plan(
    plan: SwarmPlan,
    project_root: Path,
    max_parallel: int | None = None,
    worker_fn: Callable[..., dict[str, Any]] | None = None,
    on_update: Callable[[SwarmPlan, SwarmTask], None] | None = None,
) -> SwarmPlan:
    """Execute a Swarm DAG plan with parallel worker pool, worktree sandboxing, and hard gates.
    
    Features:
      - opencode-agent-teams Git Worktree sandboxing for isolated subagent executions.
      - Morrow Intent Lease resource locking with TTL to eliminate concurrency races.
      - Morrow Receipt-Gated Completion validating cryptographic tokens before marking completed.
      - Downstream tasks are blocked if prerequisite tasks fail.
      - Progress is continuously synced to disk and indexed into memory.
    """
    validate_dag(plan)
    parallel = max_parallel or plan.max_parallel or 4
    executor_fn = worker_fn or default_task_executor

    lease_mgr = IntentLeaseManager(project_root)
    worktree_mgr = SwarmWorktreeManager(project_root)

    plan.status = "running"
    save_swarm_plan(plan, project_root)

    state_lock = threading.Lock()

    def update_task_state(task_id: str, status: str, result: dict[str, Any] | None = None) -> None:
        with state_lock:
            t = plan.tasks[task_id]
            t.status = status
            if result is not None:
                t.result = result
            save_swarm_plan(plan, project_root)
            if on_update:
                try:
                    on_update(plan, t)
                except Exception as e:
                    sys.stderr.write(f"swarm on_update callback error: {e}\n")

    def run_task_in_sandbox(task: SwarmTask, lease: IntentLease | None) -> tuple[str, dict[str, Any]]:
        # 1. Determine execution directory (worktree vs project_root)
        use_wt = (task.isolate_worktree or plan.isolate_worktrees) and (task.command is not None)
        wt_dir = None
        exec_root = project_root
        if use_wt:
            try:
                wt_dir = worktree_mgr.create_worktree(task.id, plan.id)
                exec_root = wt_dir
            except Exception as e:
                sys.stderr.write(f"Failed to create worktree for task {task.id}: {e}\n")
                wt_dir = None
                exec_root = project_root

        # 2. Invoke executor
        res: dict[str, Any] = {}
        try:
            try:
                res = executor_fn(task, project_root, exec_root)
            except TypeError:
                try:
                    res = executor_fn(task, exec_root)
                except TypeError:
                    res = executor_fn(task, project_root)
        except Exception as e:
            res = {
                "returncode": -1,
                "stdout": "",
                "stderr": str(e),
                "duration": 0.0,
                "success": False,
            }

        # 3. Merge worktree artifacts back to project_root if worktree was used
        if wt_dir:
            try:
                merged = worktree_mgr.merge_worktree_artifacts(
                    wt_dir,
                    allowed_paths=task.allowed_paths,
                    expected_artifacts=task.expected_artifacts,
                )
                res["merged_artifacts"] = merged
            except Exception as e:
                res["merge_error"] = str(e)
            finally:
                worktree_mgr.cleanup_worktree(wt_dir)

        # 4. Validate barrier assertions and contract requirements
        barrier_res = validate_task_barriers(task, project_root, res)
        res["barrier_validation"] = barrier_res

        status = "failed"
        if barrier_res["status"] != "passed":
            res["error"] = f"Barrier contract validation failed: {len(barrier_res['violations'])} violation(s)"
        elif not res.get("success", True):
            res["error"] = res.get("stderr") or "Task command failed"
        else:
            # 5. Receipt-Gated Completion (Morrow)
            if plan.receipt_gated:
                try:
                    rcpt = issue_completion_receipt(task, plan.id, project_root, barrier_res)
                    verify_res = verify_completion_receipt(rcpt, project_root, task)
                    if verify_res["valid"]:
                        task.receipt = rcpt.to_dict()
                        res["receipt"] = rcpt.to_dict()
                        status = "completed"
                    else:
                        status = "failed"
                        res["error"] = f"Receipt verification failed: {verify_res['violations']}"
                except Exception as e:
                    status = "failed"
                    res["error"] = f"Failed to issue/verify completion receipt: {e}"
            else:
                status = "completed"

        # 6. Release intent lease
        if lease:
            try:
                lease_mgr.release_lease(lease.lease_id)
                task.lease_id = None
            except Exception as e:
                sys.stderr.write(f"Failed to release lease {lease.lease_id}: {e}\n")

        return status, res

    # Execution loop
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        while True:
            with state_lock:
                pending_tasks = [t for t in plan.tasks.values() if t.status == "pending"]
                running_tasks = [t for t in plan.tasks.values() if t.status == "running"]
                completed_ids = {t.id for t in plan.tasks.values() if t.status == "completed"}
                failed_ids = {t.id for t in plan.tasks.values() if t.status in ("failed", "blocked")}

            if not pending_tasks and not running_tasks:
                break

            # Check for blocked tasks whose dependencies have failed
            for t in list(pending_tasks):
                if any(dep in failed_ids for dep in t.depends_on):
                    update_task_state(t.id, "blocked", {"error": "Prerequisite task failed", "success": False})
                    pending_tasks.remove(t)

            # Find ready tasks (all dependencies completed)
            candidate_tasks = [
                t for t in pending_tasks
                if set(t.depends_on).issubset(completed_ids)
            ]

            if not candidate_tasks and not running_tasks:
                with state_lock:
                    for t in pending_tasks:
                        update_task_state(t.id, "blocked", {"error": "Dependencies could not be satisfied", "success": False})
                break

            # Lease checking for candidates
            dispatched = []
            available_slots = max(0, parallel - len(running_tasks))
            for t in candidate_tasks:
                if len(dispatched) >= available_slots:
                    break
                lease = None
                if plan.enforce_intent_leases:
                    file_paths = [p for p in (t.intent_paths or t.allowed_paths or t.expected_artifacts) if p]
                    if file_paths:
                        try:
                            lease = lease_mgr.acquire_lease(
                                plan.id,
                                t.id,
                                file_paths,
                                exclusive=t.exclusive_lease,
                                ttl=t.timeout + 60.0,
                            )
                            t.lease_id = lease.lease_id
                        except IntentLeaseConflictError:
                            # Intent collision with currently running task; wait for next cycle
                            continue
                dispatched.append((t, lease))

            if not dispatched and not running_tasks:
                # Deadlock in lease configuration: mark blocked
                with state_lock:
                    for t in candidate_tasks:
                        update_task_state(t.id, "blocked", {"error": "Unresolvable Intent Lease conflict", "success": False})
                break

            futures = []
            for t, lease in dispatched:
                update_task_state(t.id, "running")
                future = pool.submit(run_task_in_sandbox, t, lease)
                futures.append((future, t.id))

            # Wait for dispatched tasks
            for fut, tid in futures:
                try:
                    status, res = fut.result()
                    update_task_state(tid, status, res)

                    # Index artifact to project memory
                    try:
                        from autoharness import memory
                        con = memory.get_memory_db(project_root)
                        try:
                            task_obj = plan.tasks[tid]
                            memory.reconcile_and_store(
                                con,
                                kind="swarm_task",
                                source=f"swarm/{plan.id}/{tid}",
                                title=f"Swarm Task [{task_obj.role}]: {task_obj.name}",
                                content=f"Status: {status}\nCommand: {task_obj.command}\nOutput: {res.get('stdout', '')}\nError: {res.get('stderr', '')}",
                                metadata={"plan_id": plan.id, "task_id": tid, "role": task_obj.role, "status": status},
                                doc_id=f"swarm_{plan.id}_{tid}",
                            )
                        finally:
                            con.close()
                    except (OSError, sqlite3.Error, ValueError, ImportError) as e:
                        res["memory_index_error"] = str(e)
                except Exception as e:
                    update_task_state(tid, "failed", {"error": str(e), "success": False})

    with state_lock:
        all_completed = all(t.status == "completed" for t in plan.tasks.values())
        plan.status = "completed" if all_completed else "failed"
        save_swarm_plan(plan, project_root)

    return plan


def create_sample_swarm_plan(title: str, tasks: list[dict[str, Any]] | None = None) -> SwarmPlan:
    """Create a new structured SwarmPlan with reasonable defaults."""
    clean_id = f"swarm_{uuid.uuid4().hex}"
    if not tasks:
        # Default sample 4-stage pipeline: Research -> Code (2 parallel) -> Verify
        tasks = [
            {
                "id": "t1_research",
                "name": "Research and Interface Specification",
                "role": "researcher",
                "depends_on": [],
                "prompt": "Investigate required contracts and existing signatures.",
            },
            {
                "id": "t2_engine",
                "name": "Implement Core Engine Logic",
                "role": "coder",
                "depends_on": ["t1_research"],
                "command": f'"{sys.executable}" -c "print(\'Engine core built\')"',
            },
            {
                "id": "t3_ui_cli",
                "name": "Implement CLI and Presentation",
                "role": "coder",
                "depends_on": ["t1_research"],
                "command": f'"{sys.executable}" -c "print(\'CLI interface built\')"',
            },
            {
                "id": "t4_verify",
                "name": "Integration Testing and Verification Gate",
                "role": "verifier",
                "depends_on": ["t2_engine", "t3_ui_cli"],
                "command": f'"{sys.executable}" -c "print(\'All integration tests passed\')"',
            },
        ]
    plan_dict = {
        "id": clean_id,
        "title": title,
        "description": f"Multi-agent swarm plan for {title}",
        "max_parallel": 3,
        "status": "pending",
        "tasks": tasks,
    }
    return SwarmPlan.from_dict(plan_dict)
