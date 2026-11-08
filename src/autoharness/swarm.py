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

import graphlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "max_parallel": self.max_parallel,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
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


def save_swarm_plan(plan: SwarmPlan, project_root: Path) -> Path:
    """Save Swarm plan atomically using a temporary file and os.replace."""
    plan.updated_at = time.time()
    swarm_dir = get_swarm_dir(project_root)
    file_path = swarm_dir / f"{plan.id}.json"
    temp_path = swarm_dir / f".tmp_{plan.id}_{os.getpid()}_{int(time.monotonic() * 1000)}.json"
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
    swarm_dir = get_swarm_dir(project_root)
    plans = []
    for f in swarm_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
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


def default_task_executor(task: SwarmTask, project_root: Path) -> dict[str, Any]:
    """Execute a single task node: run command or evaluate step."""
    start_time = time.monotonic()
    if task.command:
        # Run command in project root
        try:
            res = subprocess.run(
                task.command,
                shell=True,
                cwd=str(project_root),
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
            }
        except subprocess.TimeoutExpired as e:
            duration = round(time.monotonic() - start_time, 3)
            return {
                "returncode": -1,
                "stdout": e.stdout or "",
                "stderr": f"Execution timed out after {task.timeout}s",
                "duration": duration,
                "success": False,
            }
        except Exception as e:
            duration = round(time.monotonic() - start_time, 3)
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": str(e),
                "duration": duration,
                "success": False,
            }
    elif task.prompt:
        # Informational prompt task (e.g. spec generation, review)
        duration = round(time.monotonic() - start_time, 3)
        return {
            "returncode": 0,
            "stdout": f"Prompt evaluated for role [{task.role}]: {task.prompt}",
            "stderr": "",
            "duration": duration,
            "success": True,
        }
    else:
        return {
            "returncode": 0,
            "stdout": "No command or prompt specified; task marked as completed.",
            "stderr": "",
            "duration": 0.0,
            "success": True,
        }


def execute_swarm_plan(
    plan: SwarmPlan,
    project_root: Path,
    max_parallel: int | None = None,
    worker_fn: Callable[[SwarmTask, Path], dict[str, Any]] | None = None,
    on_update: Callable[[SwarmPlan, SwarmTask], None] | None = None,
) -> SwarmPlan:
    """Execute a Swarm DAG plan with parallel worker pool and hard gates.

    Downstream tasks are blocked if prerequisite tasks fail.
    Progress is continuously synced to disk and indexed into memory.
    """
    validate_dag(plan)
    parallel = max_parallel or plan.max_parallel or 4
    executor_fn = worker_fn or default_task_executor

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
                except Exception:
                    pass

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
            ready_tasks = [
                t for t in pending_tasks
                if set(t.depends_on).issubset(completed_ids)
            ]

            if not ready_tasks and not running_tasks:
                # Deadlock or remaining tasks are blocked
                with state_lock:
                    for t in pending_tasks:
                        update_task_state(t.id, "blocked", {"error": "Dependencies could not be satisfied", "success": False})
                break

            # Dispatch ready tasks up to concurrency capacity
            futures = []
            for t in ready_tasks:
                update_task_state(t.id, "running")
                future = pool.submit(executor_fn, t, project_root)
                futures.append((future, t.id))

            # Wait for at least one task to complete before next scheduling cycle
            for fut, tid in futures:
                try:
                    res = fut.result()
                    status = "completed" if res.get("success", True) else "failed"
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
                    except Exception:
                        pass
                except Exception as e:
                    update_task_state(tid, "failed", {"error": str(e), "success": False})

    with state_lock:
        all_completed = all(t.status == "completed" for t in plan.tasks.values())
        plan.status = "completed" if all_completed else "failed"
        save_swarm_plan(plan, project_root)

    return plan


def create_sample_swarm_plan(title: str, tasks: list[dict[str, Any]] | None = None) -> SwarmPlan:
    """Create a new structured SwarmPlan with reasonable defaults."""
    clean_id = f"swarm_{int(time.time() * 1000) % 1000000}"
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
                "command": "python -c \"print('Engine core built')\"",
            },
            {
                "id": "t3_ui_cli",
                "name": "Implement CLI and Presentation",
                "role": "coder",
                "depends_on": ["t1_research"],
                "command": "python -c \"print('CLI interface built')\"",
            },
            {
                "id": "t4_verify",
                "name": "Integration Testing and Verification Gate",
                "role": "verifier",
                "depends_on": ["t2_engine", "t3_ui_cli"],
                "command": "python -c \"print('All integration tests passed')\"",
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
