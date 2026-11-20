"""Subsystem commands: gateway, memory, codemap, swarm, drive, permissions, sentinel, stencil, slot, probe."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from autoharness import approvals, config, egress
from autoharness.commands.common import EXIT_APPROVAL, need_approval, out
from autoharness.env import HarnessError


def unapproved_servers(project) -> list[str]:
    """Servers in gateway.json the gateway will not run: not approved on this machine (a pull, an older
    autoharness, or the project moved)."""
    servers = project.gateway_registry()["servers"]
    return sorted(n for n, c in servers.items() if not approvals.approved(project.root, n, c))


def cmd_gateway(args, project, catalog):
    if args.action == "serve":
        from autoharness.gateway import serve
        return serve(project.root)
    agent = project.agent
    if args.action == "install" and args.user:
        # no --project: the gateway serves the project the agent runs in, found from its working directory
        path = project.install_user_gateway({"command": sys.executable, "args": ["-m", "autoharness", "gateway",
                                                                                  "serve"]})
        out({"status": "ok", "installed": agent.name, "file": str(path),
             "next": f"One last restart: ask the user to {agent.reload_hint}. After that, in every project, servers "
                     "added with `add mcp` are available at once through harness_tools / harness_call."})
    elif args.action == "uninstall" and args.user:
        path = project.uninstall_user_gateway()
        out({"status": "ok", "uninstalled": agent.name, "file": str(path),
             "next": f"Ask the user to {agent.reload_hint} to drop the gateway."})
    elif args.action == "install":
        server = {"command": sys.executable, "args": ["-m", "autoharness", "--project", str(project.root),
                                                       "gateway", "serve"]}
        project.install_gateway(server)
        out({"status": "ok", "installed": agent.name, "file": agent.mcp_file, "config": server,
             "next": f"One last restart: ask the user to {agent.reload_hint}. After that, servers added with "
                     "`add mcp` are available at once through harness_tools / harness_call."})
    elif args.action == "uninstall":
        project.uninstall_gateway()
        out({"status": "ok", "uninstalled": agent.name,
             "next": f"Ask the user to {agent.reload_hint} to drop the gateway."})
    elif args.action == "approve":
        return gateway_approve(args, project)
    else:
        reg = project.gateway_registry()
        state_file = project.state / "gateway.state.json"
        state = json.loads(state_file.read_text("utf-8")) if state_file.exists() else None
        unapproved = unapproved_servers(project)
        out({"installed": project.gateway_active(), "user": project.user_gateway_active(),
             "agents": reg["installed"], "servers": sorted(reg["servers"]),
             "unapproved": unapproved, "running": state and state.get("servers"),
             "state_updated": state and state.get("updated")}
            | ({"next": "The gateway does not run unapproved servers (gateway.json may come from a pull). "
                        "Show the user each one's config: `autoharness gateway approve <name>`."} if unapproved else {}))
    return 0


def gateway_approve(args, project) -> int:
    """Let the gateway run servers in gateway.json that were not added on this machine, after the user's yes:
    one by name, or with --all every one waiting (after upgrading, servers added by an older autoharness)."""
    servers = project.gateway_registry()["servers"]
    if args.all:
        chosen = {n: c for n, c in servers.items() if not approvals.approved(project.root, n, c)}
        if not chosen:
            out({"status": "ok", "approved": [], "next": "Nothing is waiting for approval."})
            return 0
    elif not args.name:
        raise HarnessError("name the server (`autoharness gateway approve <name>`) or pass --all")
    elif args.name not in servers:
        raise HarnessError(f"{args.name!r} is not in .autoharness/gateway.json")
    else:
        chosen = {args.name: servers[args.name]}
    allow = config.allowed_hosts(project.root)
    if not args.approve:
        out({"status": "approval_required", "what": f"let the gateway run {', '.join(chosen)}",
             "servers": {n: {"config": c, "egress": egress.check(c, allow)} for n, c in chosen.items()},
             "next": "These servers were not approved on this machine: gateway.json may have come from a pull, or "
                     "they were added before autoharness asked for approval. Show the user each config (the command "
                     "it runs, the env vars it reads, where it connects). Re-run with --approve only if they agree."})
        return EXIT_APPROVAL
    for name, server in chosen.items():
        approvals.approve(project.root, name, server)
    updates, warnings = project.codex_gateway_update(project.gateway_registry())
    if updates:
        from autoharness import transactions
        transactions.commit(project.root, updates)
    out({"status": "ok", "approved": sorted(chosen), "warnings": warnings,
         "next": " ".join(warnings) if warnings else "The gateway starts them within a second; check with harness_tools."})
    return 0


def cmd_memory(args, project, catalog) -> int:
    from autoharness import memory
    if args.action == "index":
        counts = memory.index_project_memory(project.root)
        out({"status": "ok", "indexed": counts, "next": "Memory indexed in .autoharness/memory.db."})
        return 0
    elif args.action == "search":
        from autoharness import vector
        con = memory.get_memory_db(project.root)
        try:
            mode = getattr(args, "mode", "hybrid")
            if mode == "vector":
                results = vector.search_vectors(con, args.query, kind=args.kind, limit=args.limit)
            elif mode == "fts":
                results = memory.search_memory(con, args.query, kind=args.kind, limit=args.limit)
            else:  # hybrid
                results = vector.hybrid_search(con, args.query, kind=args.kind, limit=args.limit)
        finally:
            con.close()
        out({"status": "ok", "query": args.query, "mode": getattr(args, "mode", "hybrid"), "kind": args.kind, "matches_count": len(results), "results": results})
        return 0
    elif args.action == "reconcile":
        prune = getattr(args, "prune_orphans", False)
        result = memory.reconcile_memory_drift(project.root, prune_orphans=prune, kind=getattr(args, "kind", "all"))
        if getattr(args, "json", False):
            out(result)
        else:
            report_md = memory.format_memory_reconcile_report(result)
            sys.stdout.write(report_md + "\n")
        return 0
    else:  # status
        con = memory.get_memory_db(project.root)
        try:
            cur = con.cursor()
            rows = cur.execute("SELECT kind, count(*) as cnt FROM memory_docs GROUP BY kind").fetchall()
            by_kind = {r["kind"]: r["cnt"] for r in rows}
            total = sum(by_kind.values())
        finally:
            con.close()
        out({"status": "ok", "database": str(project.root / ".autoharness" / "memory.db"), "total_docs": total, "by_kind": by_kind})
        return 0


def cmd_codemap(args, project, catalog) -> int:
    from autoharness import codemap
    target = (project.root / args.path) if args.path else project.root
    cmap = codemap.generate_codemap(target)
    if getattr(args, "arch", False):
        dep_graph = codemap.build_dependency_graph(cmap)
        cycles = codemap.detect_architecture_cycles(dep_graph)
        violations = codemap.detect_boundary_violations(dep_graph)
        arch_data = {
            "modules_count": len(dep_graph["modules"]),
            "edges_count": len(dep_graph["edges"]),
            "cycles": cycles,
            "violations": violations,
            "dep_graph": dep_graph,
        }
        if args.json:
            out({"status": "ok", "arch": arch_data})
        else:
            report = codemap.format_architecture_report(arch_data)
            print(report)
        if getattr(args, "fail_on_cycle", False) and (cycles or violations):
            return 1
        return 0
    if getattr(args, "check_shims", False):
        shims = []
        if target.is_file():
            shims = codemap.detect_anti_shims(target)
        else:
            for p in sorted(target.rglob("*.py")):
                if not any(part in p.parts for part in codemap.DEFAULT_EXCLUDES):
                    shims.extend(codemap.detect_anti_shims(p))
        if getattr(args, "json", False):
            out({"status": "warning" if shims else "ok", "total_shims": len(shims), "shims": shims})
        else:
            report = codemap.format_anti_shim_report(shims)
            print(report)
        if getattr(args, "fail_on_cycle", False) and shims:
            return 1
        return 0
    if getattr(args, "impact", None):
        call_graph = codemap.build_call_graph(cmap)
        impact_data = codemap.analyze_impact(call_graph, args.impact)
        if args.json:
            out({"status": "ok", "impact": impact_data})
        else:
            report = codemap.format_impact_report(impact_data)
            print(report)
        return 0
    if getattr(args, "callers", None):
        call_graph = codemap.build_call_graph(cmap)
        callers = codemap.find_callers(call_graph, args.callers)
        out({"status": "ok", "symbol": args.callers, "callers_count": len(callers), "callers": callers})
        return 0
    if args.query:
        matches = codemap.search_codemap(cmap, args.query)
        out({"status": "ok", "query": args.query, "matches_count": len(matches), "matches": matches})
        return 0
    if args.json:
        out({"status": "ok", "codemap": cmap})
        return 0
    tree_text = codemap.format_codemap_tree(cmap)
    print(tree_text)
    return 0


def cmd_swarm(args, project, catalog) -> int:
    from autoharness import swarm
    action = args.action
    if action == "init":
        title = args.title or "Multi-Agent Workflow"
        tasks_data = None
        if args.file:
            plan_path = Path(args.file)
            if not plan_path.is_file():
                plan_path = project.root / args.file
            if not plan_path.is_file():
                out({"status": "error", "error": f"Tasks file not found: {args.file}"})
                return 1
            try:
                loaded = json.loads(plan_path.read_text("utf-8"))
                if isinstance(loaded, dict) and "tasks" in loaded:
                    tasks_data = loaded["tasks"]
                elif isinstance(loaded, list):
                    tasks_data = loaded
            except Exception as e:
                out({"status": "error", "error": f"Failed to parse tasks file: {e}"})
                return 1
        plan = swarm.create_sample_swarm_plan(title, tasks=tasks_data)
        saved_file = swarm.save_swarm_plan(plan, project.root)
        out({
            "status": "ok",
            "plan_id": plan.id,
            "title": plan.title,
            "tasks_count": len(plan.tasks),
            "file": str(saved_file),
            "next": f"Run this swarm plan with `autoharness swarm run {plan.id}` or inspect with `autoharness swarm status {plan.id}`.",
        })
        return 0

    if action == "list":
        plans = swarm.list_swarm_plans(project.root)
        out({"status": "ok", "total_plans": len(plans), "plans": plans})
        return 0

    if action == "status":
        plan_id = args.plan_id
        if not plan_id:
            plans = swarm.list_swarm_plans(project.root)
            if not plans:
                out({"status": "error", "error": "No swarm plans found. Create one with `autoharness swarm init`."})
                return 1
            plan_id = plans[0]["id"]
        try:
            plan = swarm.load_swarm_plan(plan_id, project.root)
            out({"status": "ok", "plan": plan.to_dict()})
            return 0
        except Exception as e:
            out({"status": "error", "error": str(e)})
            return 1

    if action == "run":
        plan_id = args.plan_id
        if not plan_id:
            plans = swarm.list_swarm_plans(project.root)
            if not plans:
                out({"status": "error", "error": "No swarm plan to run. Create one with `autoharness swarm init`."})
                return 1
            plan_id = plans[0]["id"]
        try:
            plan = swarm.load_swarm_plan(plan_id, project.root)
            parallel = args.parallel or plan.max_parallel or 4
            executed = swarm.execute_swarm_plan(plan, project.root, max_parallel=parallel)
            out({
                "status": "ok",
                "plan_id": executed.id,
                "plan_status": executed.status,
                "tasks": {tid: {"status": t.status, "duration": t.result.get("duration", 0)} for tid, t in executed.tasks.items()},
                "next": f"Check details with `autoharness swarm status {executed.id}`.",
            })
            return 0 if executed.status == "completed" else 1
        except Exception as e:
            out({"status": "error", "error": str(e)})
            return 1

    if action == "verify":
        plan_id = args.plan_id
        if not plan_id:
            plans = swarm.list_swarm_plans(project.root)
            if not plans:
                out({"status": "error", "error": "No swarm plans found to verify."})
                return 1
            plan_id = plans[0]["id"]
        try:
            plan = swarm.load_swarm_plan(plan_id, project.root)
            report = swarm.verify_swarm_plan_contracts(plan, project.root)
            if getattr(args, "json", False):
                out(report)
            else:
                print(swarm.format_swarm_verify_report(report))
            return 0 if report.get("status") == "passed" else 1
        except Exception as e:
            out({"status": "error", "error": str(e)})
            return 1

    out({"status": "error", "error": f"Unknown swarm action: {action}"})
    return 1


def cmd_drive(args, project, catalog) -> int:
    from autoharness import drive
    action = getattr(args, "action", "run") or "run"
    if action == "status":
        state = drive.load_drive_state(project.root)
        queue = drive.get_drive_queue(project.root)
        out({
            "status": "ok",
            "drive_status": state.get("status", "idle"),
            "session_id": state.get("session_id"),
            "current_task": state.get("current_task"),
            "completed_count": len(state.get("completed", [])),
            "failed_count": len(state.get("failed", [])),
            "open_queue_count": len(queue),
            "open_queue": queue,
            "state": state,
        })
        return 0
    if action == "queue":
        queue = drive.get_drive_queue(project.root)
        out({
            "status": "ok",
            "total_open": len(queue),
            "queue": queue,
        })
        return 0
    if action == "rollback":
        ok, msg = drive.rollback_changes(project.root)
        out({"status": "ok" if ok else "error", "message": msg})
        return 0 if ok else 1
    if action == "run":
        max_tasks = getattr(args, "max_tasks", 3) or 3
        dry_run = getattr(args, "dry_run", False)
        followup_id = getattr(args, "followup", None)
        test_cmd = getattr(args, "test_cmd", None)
        res = drive.run_drive_loop(
            project.root,
            max_tasks=max_tasks,
            dry_run=dry_run,
            followup_id=followup_id,
            test_command=test_cmd,
        )
        out(res)
        return 0 if res.get("status") != "error" else 1
    out({"status": "error", "error": f"Unknown drive action: {action}"})
    return 1


def cmd_update(args, project, catalog) -> int:
    from autoharness import updater
    repo = getattr(args, "repo", None) or updater.DEFAULT_REPO
    check_only = getattr(args, "check", False)
    force = getattr(args, "force", False)
    yes = getattr(args, "yes", False)

    check_res = updater.check_for_updates(repo)
    if check_only:
        out(check_res)
        return 0 if check_res.get("status") != "error" else 1

    if check_res.get("status") == "error" and not force:
        out(check_res)
        return 1

    if not check_res.get("update_available") and not force:
        out({
            "status": "ok",
            "message": f"AutoHarness is up to date (version {updater.__version__}).",
            "check": check_res,
        })
        return 0

    if not yes:
        if sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            target_v = check_res.get("latest_version") or "latest"
            sys.stdout.write(f"Update available ({updater.__version__} -> {target_v}). Install now? [y/N]: ")
            sys.stdout.flush()
            ans = sys.stdin.readline().strip().lower()
            if ans not in ("y", "yes"):
                out({"status": "cancelled", "message": "Update cancelled by user."})
                return 0
        else:
            target_v = check_res.get("latest_version") or "latest"
            return need_approval(
                f"self-update to {target_v} from {repo}",
                {"latest_version": target_v, "repo": repo, "current_version": updater.__version__},
            )

    res = updater.perform_update(repo=repo, force=force, project_root=project.root)
    out({"status": res["status"], "update": res, "check": check_res})
    return 0 if res.get("status") == "ok" else 1


def cmd_permissions(args, project, catalog) -> int:
    from autoharness import permissions
    action = getattr(args, "action", "status") or "status"
    if action == "enable":
        res = permissions.enable_project_permissions(project)
        out(res)
        return 0
    if action == "disable":
        res = permissions.disable_project_permissions(project)
        out(res)
        return 0
    if action == "simulate":
        from autoharness import consequence
        action_type = getattr(args, "action_type", "command") or "command"
        target = getattr(args, "target", "") or "unknown"
        sim = consequence.simulate_action(action_type, target, project_root=project.root if project else None)
        if getattr(args, "json", False):
            out(sim)
        else:
            print(consequence.format_consequence_manifest(sim))
        return 0
    res = permissions.check_agent_permissions(project)
    out(res)
    return 0


def cmd_simulate(args, project, catalog) -> int:
    from autoharness import consequence
    action_type = getattr(args, "action_type", "command") or "command"
    target = getattr(args, "target", "") or ""
    proj_root = project.root if project else None
    sim = consequence.simulate_action(action_type, target, project_root=proj_root)
    if getattr(args, "json", False):
        out(sim)
    else:
        manifest = consequence.format_consequence_manifest(sim)
        print(manifest)
    return 0


def cmd_skim(args, project, catalog) -> int:
    import subprocess
    from autoharness import compression
    target = getattr(args, "target", "") or ""
    kind = getattr(args, "kind", "auto") or "auto"
    max_lines = getattr(args, "max_lines", 60) or 60
    no_save = getattr(args, "no_save", False)
    save_raw = not no_save

    content = ""
    exit_code = 0
    target_path = Path(target)
    if target_path.is_file():
        try:
            content = target_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            out({"status": "error", "error": f"Failed to read file {target}: {e}"})
            return 1
    else:
        try:
            proc = subprocess.run(
                target,
                shell=True,
                cwd=project.root if project else None,
                capture_output=True,
                text=True,
                errors="replace",
                check=False
            )
            content = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
            exit_code = proc.returncode
        except (OSError, subprocess.SubprocessError) as e:
            out({"status": "error", "error": f"Failed to execute command {target}: {e}"})
            return 1

    proj_root = str(project.root) if project else None
    res = compression.compress_output(
        content,
        kind=kind,
        max_lines=max_lines,
        save_raw=save_raw,
        save_dir=proj_root
    )
    if getattr(args, "json", False):
        out({"status": "ok", "exit_code": exit_code, **res})
    else:
        print(compression.format_compressed_report(res))
    return exit_code


def cmd_sentinel(args, project, catalog) -> int:
    from autoharness import sentinel
    if not project or not project.root:
        out({"status": "error", "error": "No project root found for sentinel operation."})
        return 1

    action = getattr(args, "action", "status") or "status"
    raw_json = getattr(args, "json", False)

    if action == "snapshot":
        label = getattr(args, "label", "") or "checkpoint"
        snap = sentinel.capture_snapshot(project.root, label=label)
        if raw_json:
            out({"status": "ok", "snapshot": snap})
        else:
            print(f"Captured snapshot {snap.get('id')} ({snap.get('label')}) at tree state {snap.get('tree_state')[:12]}")
        return 0

    if action == "rollback":
        snap_id = getattr(args, "snapshot_id", None)
        res = sentinel.rollback_to_snapshot(project.root, snapshot_id=snap_id)
        if raw_json:
            out(res)
        else:
            print(f"Rollback complete: {len(res.get('restored_files', []))} tracked restored, {len(res.get('removed_files', []))} untracked removed.")
        return 0

    if action == "run":
        pre_flight = getattr(args, "pre_flight", False)
        if pre_flight:
            sentinel.capture_snapshot(project.root, label="pre-flight")
        test_cmd = getattr(args, "test_command", "pytest") or "pytest"
        auto_rollback = getattr(args, "auto_rollback", False) and not pre_flight
        check_shims = not getattr(args, "no_shims", False)
        res = sentinel.run_sentinel_verification(
            project.root,
            test_command=test_cmd,
            auto_rollback=auto_rollback,
            check_shims=check_shims
        )
        if raw_json:
            out(res)
        else:
            print(sentinel.format_sentinel_report(res))
        return 0 if res.get("status") == "passed" else 1

    # default: status
    stat = sentinel.get_sentinel_status(project.root)
    if raw_json:
        out(stat)
    else:
        active = stat.get("active_snapshot")
        if active:
            print(f"Active Baseline Snapshot: {active.get('id')} ({active.get('label')}) from {active.get('timestamp')}")
        else:
            print("Sentinel status: idle (no active baseline snapshot).")
    return 0


def cmd_compact(args, project, catalog) -> int:
    from autoharness import context_pruning
    action = getattr(args, "action", "run") or "run"
    raw_json = getattr(args, "json", False)
    proj_root = project.root if project else None

    if action == "status":
        if not proj_root:
            out({"status": "error", "error": "No project root found."})
            return 1
        sid = getattr(args, "target", None) or None
        stat = context_pruning.get_session_compaction_status(proj_root, session_id=sid)
        if raw_json:
            out(stat)
        else:
            print(f"Folded sessions: {stat.get('sessions_count', 0)}, Tokens saved: ~{stat.get('total_tokens_saved', 0)}")
        return 0

    if action == "prune":
        if not proj_root:
            out({"status": "error", "error": "No project root found."})
            return 1
        kl = getattr(args, "keep_last", None)
        keep = 5 if kl is None else int(kl)
        res = context_pruning.prune_folded_sessions(proj_root, keep_latest=keep)
        if raw_json:
            out(res)
        else:
            print(f"Pruned {res.get('removed_files', 0)} files, freed {res.get('freed_bytes', 0)} bytes.")
        return 0

    # run compaction
    target = getattr(args, "target", "") or ""
    target_path = Path(target) if target else None

    if not target_path or not target_path.is_file():
        if proj_root:
            folded_base = proj_root / ".autoharness" / "folded"
            potential = list(folded_base.glob("*.full.jsonl")) if folded_base.is_dir() else []
            if potential:
                target_path = potential[0]

    if not target_path or not target_path.is_file():
        res = {"status": "error", "error": f"Target transcript file not found: {target or '<none>'}"}
        if raw_json:
            out(res)
        else:
            print(f"Error: {res['error']}")
        return 1

    kl = getattr(args, "keep_last", None)
    keep_last = 5 if kl is None else int(kl)
    es = getattr(args, "epoch_size", None)
    epoch_size = 10 if es is None else int(es)
    ans = getattr(args, "anchor_steps", None)
    anchor_steps = 1 if ans is None else int(ans)
    res = context_pruning.compact_session_transcript(
        target_path,
        project_root=proj_root,
        keep_last=keep_last,
        epoch_size=epoch_size,
        anchor_steps=anchor_steps,
        save_disk=True,
    )
    if raw_json:
        out(res)
    else:
        print(context_pruning.format_compaction_report(res))
    return 0 if res.get("status") == "ok" else 1


def cmd_stencil(args, project, catalog) -> int:
    from autoharness import evidence, stencil
    action = getattr(args, "action", "scan") or "scan"
    task = project.task() if project and project.active_id() else None
    proj_root = project.root if project else Path.cwd()

    if action in ("scan", "extract"):
        src_str = getattr(args, "source", "") or ""
        if not src_str:
            out({"status": "error", "error": "Source file required for stencil scan/extract"})
            return 1
        src = Path(src_str)
        if not src.is_absolute() and project:
            src = project.root / src
        out_str = getattr(args, "output", "") or ""
        out_path = Path(out_str) if out_str else None
        if out_path and not out_path.is_absolute() and project:
            out_path = project.root / out_path
        mode = getattr(args, "mode", "auto") or "auto"
        threshold = getattr(args, "threshold", 0.5)
        if threshold is None:
            threshold = 0.5
        max_points = getattr(args, "max_points", 250) or 250
        res = stencil.scan_stencil(
            src,
            project_root=proj_root,
            mode=mode,
            threshold=float(threshold),
            max_points=int(max_points),
            output=out_path,
            task=task,
        )
        out(res)
        return 0 if res.get("status") == "ok" else 1

    elif action == "list":
        res = stencil.list_stencils(proj_root)
        out(res)
        return 0

    elif action == "show":
        src_str = getattr(args, "source", "") or ""
        if not src_str:
            out({"status": "error", "error": "Stencil id required: autoharness stencil show <id>"})
            return 1
        res = stencil.show_stencil(src_str, proj_root)
        out(res)
        return 0 if res.get("status") == "ok" else 1

    elif action == "check":
        s_str = getattr(args, "source", "") or ""
        t_str = getattr(args, "target", "") or ""
        if not s_str or not t_str:
            out({"status": "error", "error": "Both stencil source and target files are required for check"})
            return 1
        s_path = Path(s_str)
        t_path = Path(t_str)
        if not s_path.is_absolute() and project and s_path.exists():
            s_path = project.root / s_path
        if not t_path.is_absolute() and project:
            t_path = project.root / t_path
        res = stencil.check_stencil(s_path, t_path, project_root=proj_root)
        out(res)
        return 0 if res.get("conforms") else 1

    elif action == "add":
        if not task:
            out({"status": "error", "error": "no active task"})
            return 1
        file_str = getattr(args, "source", "") or ""
        if not file_str:
            out({"status": "error", "error": "file required to add stencil evidence"})
            return 1
        p = Path(file_str)
        if not p.is_absolute():
            p = project.root / p
        note = getattr(args, "note", "") or ""
        item = evidence.add(task, p, "stencil", note=note)
        out({"status": "ok", "evidence": item})
        return 0

    out({"status": "error", "error": f"Unknown action: {action}"})
    return 1


def cmd_slot(args, project, catalog) -> int:
    from autoharness import slots
    action = getattr(args, "action", "inspect") or "inspect"
    task_info = None
    try:
        t = project.task()
        if t:
            task_info = t.info()
    except HarnessError:
        task_info = None

    if action == "export":
        data = slots.export_harness_slots(project.root, active_task_info=task_info)
        out({"status": "ok", "slots": data})
        return 0

    if action == "diff":
        target = Path(args.target) if getattr(args, "target", None) else None
        if not target or not target.is_file():
            out({"status": "error", "error": f"Target slots JSON file not found: {args.target}"})
            return 1
        try:
            head_slots = json.loads(target.read_text(encoding="utf-8"))
        except Exception as e:
            out({"status": "error", "error": f"Failed to parse target slots file: {e}"})
            return 1
        base_slots = slots.inspect_harness_slots(project.root, active_task_info=task_info)
        diff_res = slots.diff_harness_slots(base_slots, head_slots)
        out({"status": "ok", "diff": diff_res})
        return 0

    # default: inspect
    data = slots.inspect_harness_slots(project.root, active_task_info=task_info)
    if getattr(args, "json", False):
        out({"status": "ok", "slots": data})
    else:
        report = slots.format_slots_report(data)
        print(report)
    return 0


def cmd_probe(args, project, catalog) -> int:
    from autoharness import probe, slots
    task_info = None
    task_prompt = getattr(args, "prompt", None) or "Active task probe execution"
    try:
        t = project.task()
        if t:
            task_info = t.info()
            task_prompt = task_info.get("prompt") or task_info.get("task") or task_prompt
    except HarnessError:
        task_info = None

    k = getattr(args, "k", 2) or 2
    base_slots = slots.inspect_harness_slots(project.root, active_task_info=task_info)
    probe_res = probe.run_propose_probe_compose(base_slots, task_prompt=task_prompt, k=k)

    if getattr(args, "json", False):
        out({"status": "ok", "probe": probe_res})
    else:
        report = probe.format_probe_report(probe_res)
        print(report)
    return 0





__all__ = [name for name in globals() if not name.startswith("__")]
