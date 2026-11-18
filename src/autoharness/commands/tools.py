"""Tool management commands: add, vet, remove, prune, status, config."""
from __future__ import annotations

from pathlib import Path

from autoharness import agents, answers, config, egress, evidence, history, lessons, library, reviewers, runs, vet
from autoharness.commands.common import EXIT_APPROVAL, cli_ref, need_approval, out
from autoharness.commands.discovery import local_mcp_name, resolve_mcp, resolve_skill
from autoharness.commands.reviews import RETOLD, read_answer
from autoharness.commands.subsystems import unapproved_servers
from autoharness.env import (ContentChanged, HarnessError, SkillRejected, Task, missing_tools, npx_stage_skill,
                             parse_skill_spec, skill_dir)
from autoharness.hooks import _reviewer_can_start, write_lock


def already_kept(project, kind: str, name: str) -> int:
    out({"status": "ok", "already_installed": name, "kept": project.kept()[kind][name].get("task"),
         "next": "An earlier task kept this in the project; use it as is."})
    return 0


def cmd_add(args, project, catalog):
    task = project.task()
    if task.info().get("mode", "full") == "light":
        raise HarnessError("adding tools requires full mode; run `autoharness mode full` first")
    if args.kind == "mcp" and (args.as_name or local_mcp_name(args.name)) in project.kept()["mcp"]:
        return already_kept(project, "mcp", args.as_name or local_mcp_name(args.name))
    if args.kind == "mcp" and args.name in [b.name for b in agents.builtin_mcp(project.agent.name)]:
        out({"status": "ok", "already_installed": args.name, "builtin": True,
             "next": "This server is built into the agent: nothing to install, use its tools as they are."})
        return 0
    if args.kind == "skill" and parse_skill_spec(args.name)[1] in project.kept()["skills"]:
        return already_kept(project, "skills", parse_skill_spec(args.name)[1])
    if (gate := vet_gate(task, args)) is not None:
        return gate
    vetted = vet.verdict(task, args.kind, args.name) or {}
    retold = [f"its vet verdict was recorded from your retelling, not from the reviewer's answer file: tell the user"
              ] if vetted and "answer_id" not in vetted else []
    retold += history.warning(history.rejections(project, exclude=task.id), args.kind, args.name)
    if args.kind == "mcp":
        report, source = cli_ref("resolve_mcp", resolve_mcp)(catalog, args.name)
        if not report["config"]:
            raise HarnessError(f"{args.name} has no installable configuration")
        vetted = vet.verdict(task, "mcp", args.name)
        if vetted and vetted["sha256"] != vet.mcp_digest(report["config"]):
            out({"status": "vet_required", "what": f"mcp {args.name}",
                 "next": f"Its configuration changed since it was vetted: run `autoharness vet mcp {args.name}` again."})
            return 1
        reach = egress.check(report["config"], config.allowed_hosts(project.root))
        if not reach["ok"]:
            report = report | {"requires_approval": True, "reasons": report["reasons"] + [reach["reason"]]}
        if report["requires_approval"] and not args.approve:
            return need_approval(f"add MCP server {args.name}", report | {"egress": reach})
        local = args.as_name or local_mcp_name(args.name)
        warnings = retold + task.add_mcp(local, report["config"], source=source, permissions=report["permissions"],
                                trust={"score": report["score"], "level": report["level"]}, direct=args.direct)
        secrets = report["permissions"].get("env") or report["permissions"].get("secrets", [])
        agent = task.agent
        env_note = (f" The user must set env vars: {', '.join(secrets)} (visible to the agent process)." if secrets else "")
        if task.mcp_lock()[local].get("via") == "gateway":
            out({"status": "ok", "added": local, "agent": agent.name, "via": "gateway", "file": ".autoharness/gateway.json",
                 "secrets_to_set": secrets, "warnings": warnings,
                 "next": ((" ".join(warnings) + ". Then call harness_tools / harness_call.") if warnings else
                          "Available now through the autoharness gateway, no restart: call harness_tools, then "
                          "harness_call (or the native <server>__<tool> tools if your client shows them; a client that "
                          "defers tools, such as Claude Code, needs them loaded with tool search first).") + env_note})
        else:
            tip = ("" if project.gateway_active() else
                   " Tip: suggest the user run `autoharness gateway install` once; after one last restart, "
                   "servers added later are available without restarting.")
            out({"status": "ok", "added": local, "agent": agent.name, "file": agent.mcp_file,
                 "config": agent.translate(report["config"])[0], "warnings": warnings,
                 "secrets_to_set": secrets,
                 "next": f"MCP servers load at session start: ask the user to {agent.reload_hint}." + env_note
                         + (" Resolve the warnings before relying on this server." if warnings else "") + tip})
    else:
        report = cli_ref("resolve_skill", resolve_skill)(catalog, args.name)
        if report["requires_approval"] and not args.approve:
            return need_approval(f"add skill {args.name}", report)
        try:
            stage = (library.stage_from_library if args.name.startswith("local:")
                     else cli_ref("npx_stage_skill", npx_stage_skill))
            findings = task.add_skill(args.name, force=args.force, stage=stage, vetted_sha256=vetted.get("sha256"),
                                      trust={"score": report["score"], "level": report["level"]})
        except ContentChanged:
            out({"status": "vet_required", "what": f"skill {args.name}",
                 "next": f"Its files changed since they were vetted (upstream, between the two downloads): run "
                         f"`autoharness vet skill {args.name}` again and give the new prompt to a fresh reviewer."})
            return 1
        except SkillRejected as e:
            out({"status": "rejected", "skill": e.spec, "findings": [f.__dict__ for f in e.findings],
                 "next": "Do not install. Pick another skill, or show the findings to the user; --force only on their explicit say-so."})
            return 1
        skill = parse_skill_spec(args.name)[1]
        if args.name.startswith("local:"):
            lessons.mark_used(task, "skill", skill)
        where = skill_dir(task.skills_dir, skill)
        missing = missing_tools(where)
        warnings = retold + ([f"its dependencies name {', '.join(missing)}, not found on PATH: check that the parts "
                              "you need run here before relying on it (a library listed there is not a command)"]
                             if missing else [])
        out({"status": "ok", "added": skill, "skill_md": str(where / "SKILL.md"),
             **({"missing_tools": missing} if missing else {}), **({"warnings": warnings} if warnings else {}),
             "scan_findings": [f.__dict__ for f in findings],
             "next": f"Read {where / 'SKILL.md'} now and follow it: a running session may not list a new skill "
                     "until it restarts. Smoke-test the parts you will use before relying on it."})
    return 0


def vet_gate(task: Task, args) -> int | None:
    """While the brief has constraints, a candidate needs a vet verdict, and a `breaks` one the user's yes."""
    if not vet.constraints(task) or args.name.startswith("local:"):
        return None
    v = vet.verdict(task, args.kind, args.name)
    if v is None:
        out({"status": "vet_required", "what": f"{args.kind} {args.name}",
             "next": f"The brief has constraints. Run `autoharness vet {args.kind} {args.name}`, give its prompt to a "
                     "fresh subagent, record its answer, then add again."})
        return 1
    if v["verdict"] == "breaks" and not args.approve:
        out({"status": "approval_required", "what": f"add {args.kind} {args.name}", "vet": v,
             "next": "The vetting reviewer found that it breaks the user's constraints. Pick another candidate, "
                     "or show the user the verdict; re-run with --approve only if they agree."})
        return EXIT_APPROVAL
    return None


def cmd_vet(args, project, catalog):
    task = project.task()
    if not vet.constraints(task):
        raise HarnessError("the brief records no constraints to vet against; record `autoharness brief "
                           "--constraints \"<the user's words>\"` first (or there is nothing to vet)")
    if args.verdict and not args.answer_file and cli_ref("_reviewer_can_start", _reviewer_can_start)(project.agent.name):
        # a retold verdict cannot be told from the author's own: where a reviewer can be started, it answers itself
        raise HarnessError(f"the vet verdict comes from a reviewer that did not pick this candidate, not from you: "
                           f"`autoharness vet {args.kind} {args.name} --run {project.agent.name}` starts one and records "
                           "its answer (or give the prompt to a fresh subagent and pass its saved answer with "
                           "--answer-file)")
    if args.answer_file or args.verdict:
        subject = f"{args.kind}:{args.name}"
        answer = read_answer(task, args.answer_file, "vet", vet.VERDICTS, subject) if args.answer_file else None
        verdict, reason = (answer["verdict"], answer["reason"] or answer["body"]) if answer else (args.verdict,
                                                                                                  args.text or "")
        ignored = (["--verdict/--text were not used: they come from the answer file"]
                   if answer and (args.verdict or args.text) else [])
        v = vet.record(task, args.kind, args.name, verdict, reason,
                       {"answer_id": answer["id"]} | ({"warnings": ignored} if ignored else {}) if answer
                       else {"warnings": [RETOLD]})
        if answer:
            v["answer_file"] = answers.consume(task, answer)
        out({"status": "ok", "vetted": args.name} | v | {
            "next": f"`autoharness add {args.kind} {args.name}`." if v["verdict"] == "fits" else
                    "Pick another candidate; install this one only if the user agrees after seeing the verdict."})
        return 0
    if args.kind == "mcp":
        report, source = cli_ref("resolve_mcp", resolve_mcp)(catalog, args.name)
        if not report["config"]:
            raise HarnessError(f"{args.name} has no installable configuration")
        report = report | {"egress": egress.check(report["config"], config.allowed_hosts(project.root))}
        content = vet.mcp_content(args.name, report, source)
        vet.note_pending(task, "mcp", args.name, vet.mcp_digest(report["config"]))
    else:
        repo, skill = parse_skill_spec(args.name)
        stage = (library.stage_from_library if args.name.startswith("local:")
                 else cli_ref("npx_stage_skill", npx_stage_skill))
        content, digest = vet.skill_content(args.name, stage, repo, skill)
        vet.note_pending(task, "skill", args.name, digest)
    nonce = answers.issue(task, "vet", f"{args.kind}:{args.name}")
    text = vet.prompt(task, args.kind, args.name, content) + answers.instruction(nonce)
    prompt_file = evidence.save_prompt(task, "vet", text)
    if args.run:  # an agent with no subagents of its own: autoharness starts the reviewer, read-only
        answer_file = task.dir / "answers" / f"run-vet-{nonce}.txt"
        answer_file.parent.mkdir(exist_ok=True)
        try:
            reviewers.run(args.run, project.root, Path(prompt_file), answer_file, args.timeout, nonce)
        except HarnessError as e:
            with write_lock(project):
                answers.cancel(task, nonce)
            raise HarnessError(f"{e}. Give the prompt in {prompt_file} to a reviewer yourself and record its "
                               f"answer with `autoharness vet {args.kind} {args.name} --answer-file <file>`") from e
        task.log(f"vet of {args.name} run by {args.run} (answer {nonce})")
        args.answer_file, args.run = str(answer_file), None
        return cmd_vet(args, project, catalog)
    out({"status": "ok", "prompt": text, "answer_id": nonce, "prompt_file": prompt_file,
         "next": "Give `prompt` to a fresh subagent that did not pick this candidate (Claude Code: the Agent tool), "
                 "unchanged. Save its answer unedited to a file and record it: `autoharness vet "
                 f"{args.kind} {args.name} --answer-file <file>`. No subagents at hand? `autoharness vet "
                 f"{args.kind} {args.name} --run {reviewers.available_run_hint()}` starts the reviewer and records its answer."})
    return 0


def cmd_remove(args, project, catalog):
    """Remove from the active task if it added this, else from what earlier tasks kept in the project."""
    task = project.task() if project.active_id() else None
    if task and args.kind == "mcp" and args.name in task.mcp_lock():
        task.remove_mcp(args.name, reason=args.reason or "requested")
    elif task and args.kind == "skill" and args.name in task.skills_lock():
        task.remove_skill(args.name, reason=args.reason or "requested")
    else:
        result = project.remove_kept("mcp" if args.kind == "mcp" else "skills", args.name, approved=args.approve)
        if "approval_required" in result:
            return kept_elsewhere([args.name], {"entry": result["entry"]})
        out({"status": "ok"} | result)
        return 0
    out({"status": "ok", "removed": args.name})
    return 0


def kept_elsewhere(names: list[str], extra: dict) -> int:
    out({"status": "approval_required", "what": f"uninstall {', '.join(names)}"} | extra
        | {"next": "kept.json lists these, but no task on this machine kept them as listed: a pull, an older "
                   "autoharness, another clone, or an entry changed since. They may be the user's own. Show the "
                   "user the names (and entries). Re-run with --approve only if they agree to all of them; for "
                   "some, `autoharness remove mcp|skill <name> --approve` one at a time. The rest stay installed "
                   "and listed."})
    return EXIT_APPROVAL


def cmd_prune(args, project, catalog):
    removed = project.prune(approved=args.approve)
    waiting = removed.pop("approval_required", [])
    if waiting:
        return kept_elsewhere(waiting, {"removed": removed, "waiting": waiting})
    out({"status": "ok", "removed": removed})
    return 0


def kept_summary(project) -> dict:
    kept = project.kept()
    return {"mcp": {k: {"task": v.get("task"), "agent": v.get("agent")} for k, v in kept["mcp"].items()},
            "skills": {k: {"spec": v["spec"], "task": v.get("task")} for k, v in kept["skills"].items()}}


STATUS_LOG_LINES = 30


def discovery_line(d: dict) -> str:
    """One search of `init`/`mode full` as a line of `status`."""
    if "skipped" in d:
        return f"skipped: {d['skipped']}"
    head = f"{d.get('kind', '?')}: {d.get('query', '?')}"
    tail = f"failed: {d['error']}" if "error" in d else f"{len(d.get('results', []))} found"
    return f"{head} ({tail})" + (f" [recipe {d['recipe']}]" if d.get("recipe") else "")


def cmd_status(args, project, catalog):
    unapproved = ({"gateway_unapproved": names}
                  if project.gateway_active() and (names := unapproved_servers(project)) else {})
    if not args.task_id and not project.active_id():
        out({"task": None, "kept": kept_summary(project)} | unapproved)
        return 0
    task = project.task(args.task_id)
    info, log = task.info(), (task.dir / "harness.md").read_text("utf-8")
    if not args.full:
        # init already showed the search results; repeating them here would only fill the agent's context
        info = {k: v for k, v in info.items() if k not in ("discovery", "started_epoch")} | (
            {"discovery": [discovery_line(d) for d in info["discovery"]]} if info.get("discovery") else {})
        lines = log.splitlines()
        if len(lines) > STATUS_LOG_LINES:
            log = "\n".join([f"... {len(lines) - STATUS_LOG_LINES} earlier lines: `autoharness status --full`",
                             *lines[-STATUS_LOG_LINES:]])
    out({"task": info, "mcp": task.mcp_lock(),
         "skills": {k: {"spec": v["spec"], "trust": v["trust"]} for k, v in task.skills_lock().items()},
         "kept": kept_summary(project)} | unapproved | {"checked": runs.summary(task), "log": log})
    return 0


def cmd_config(args, project, catalog):
    if args.action == "set":
        if args.value is None or (not args.value and config.SETTINGS.get(args.key) is not None):
            raise HarnessError("usage: autoharness config set <key> <value> [--global]")
        config.set_value(config.global_path() if args.glob else config.project_path(project.root), args.key, args.value)
    keys = [args.key] if args.key else list(config.SETTINGS)
    out({k: dict(zip(("value", "source"), config.resolve(project.root, k))) for k in keys})
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
