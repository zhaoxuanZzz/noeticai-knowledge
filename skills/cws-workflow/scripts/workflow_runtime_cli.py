"""Delegate and atomic-loop command groups for workflow_cli."""

from __future__ import annotations

import argparse
import json
from functools import partial
from pathlib import Path
from typing import Any, Callable

from delegate_runner import DelegateRunnerError
from workflow_contract import TaskPlan, WorkSuiteError
from workflow_planning import artifact_root, build_task_plan, company_kb_root


Resolver = Callable[..., str]


def structured_delegate_nodes(
    entry_skill: str,
    run_id: str,
    tasks: list[TaskPlan],
    frozen_kb: bool = False,
    loop_enabled: bool = False,
) -> list[dict[str, object]]:
    run_root = artifact_root(run_id)
    return [
        {
            "id": task.task_id,
            "stage": task.stage_id,
            "skill": task.skill,
            "parents": task.parents,
            "handoff_path": str(run_root / task.skill / "handoff.json"),
            "node_gate": {"mode": "node", "run_id": run_id},
            "final_gate": (
                {"mode": "final", "skill": entry_skill, "run_id": run_id}
                if task.skill == entry_skill
                else None
            ),
            "role": task.role,
            "role_skill": task.role_skill,
            "required_skills": task.required_skills,
            "title": task.title,
            "outputs": task.outputs,
            "frozen_kb": frozen_kb,
            "loop_enabled": loop_enabled,
            "prompt": task.body,
        }
        for task in tasks
    ]


def command_execute_delegate(
    args: argparse.Namespace,
    resolve_workspace: Resolver,
    resolve_run_id: Resolver,
) -> int:
    workspace = resolve_workspace(args.company, args.tenant, args.workspace)
    run_id = resolve_run_id(args.company, args.run_id)
    tasks = build_task_plan(
        args.skill,
        args.company,
        workspace,
        run_id,
        frozen_kb=args.frozen_kb,
        include_gate_instructions=False,
    )
    graph = {
        "mode": "delegate",
        "skill": args.skill,
        "company": args.company,
        "workspace": workspace,
        "run_id": run_id,
        "frozen_kb": args.frozen_kb,
        "loop": args.loop,
        "instructions": "Delegate ready nodes to subagents using node.required_skills. Data nodes must include both cws-data-agent and cws-karpathy-llm-wiki; report nodes use cws-gen-agent. Makers only write the requested artifacts. After a maker returns, the parent must call delegate complete; the runner performs node/final gates and only passed nodes unlock downstream work. If subagents are unavailable, run nodes in the current agent in dependency order.",
        "nodes": structured_delegate_nodes(
            args.skill, run_id, tasks, args.frozen_kb, args.loop
        ),
        "edges": [
            {"from": parent, "to": task.task_id}
            for task in tasks
            for parent in task.parents
        ],
    }
    print(json.dumps(graph, ensure_ascii=False, indent=2))
    return 0


def command_delegate_init(
    args: argparse.Namespace,
    resolve_workspace: Resolver,
    resolve_run_id: Resolver,
) -> int:
    from delegate_runner import initialize, load_state, ready_nodes

    run_id = resolve_run_id(args.company, args.run_id)
    workspace = resolve_workspace(args.company, None, None)
    tasks = build_task_plan(
        args.skill,
        args.company,
        workspace,
        run_id,
        frozen_kb=args.frozen_kb,
        include_gate_instructions=False,
    )
    try:
        result = initialize(
            company_kb_root(),
            run_id,
            args.skill,
            args.company,
            structured_delegate_nodes(
                args.skill, run_id, tasks, args.frozen_kb, args.loop
            ),
            frozen_kb=args.frozen_kb,
        )
        if args.loop:
            from atomic_loop import enable_delegate_loop

            enable_delegate_loop(
                company_kb_root(), run_id, max_attempts=args.max_attempts
            )
            state_path, state = load_state(company_kb_root(), run_id)
            result = {
                "run_id": run_id,
                "state_path": str(state_path),
                "ready": ready_nodes(state),
            }
    except DelegateRunnerError as exc:
        raise WorkSuiteError(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_delegate_start(args: argparse.Namespace) -> int:
    from delegate_runner import load_state, start

    try:
        _path, state = load_state(company_kb_root(), args.run_id)
        if state["nodes"].get(args.node, {}).get("loop_enabled"):
            from atomic_loop import next_attempt

            result = next_attempt(company_kb_root(), args.run_id, args.node)
        else:
            result = start(company_kb_root(), args.run_id, args.node)
    except DelegateRunnerError as exc:
        raise WorkSuiteError(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_delegate_complete(args: argparse.Namespace) -> int:
    from delegate_runner import complete, load_state

    try:
        _path, state = load_state(company_kb_root(), args.run_id)
        if state["nodes"].get(args.node, {}).get("loop_enabled"):
            if not args.lease_id:
                raise DelegateRunnerError("--lease-id is required for loop-enabled nodes")
            from atomic_loop import complete_attempt

            result = complete_attempt(
                company_kb_root(), args.run_id, args.lease_id, args.node
            )
        else:
            result = complete(company_kb_root(), args.run_id, args.node)
    except DelegateRunnerError as exc:
        raise WorkSuiteError(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


def command_delegate_status(args: argparse.Namespace) -> int:
    from delegate_runner import load_state, state_view

    try:
        _path, state = load_state(company_kb_root(), args.run_id)
    except DelegateRunnerError as exc:
        raise WorkSuiteError(str(exc)) from exc
    print(json.dumps(state_view(state), ensure_ascii=False, indent=2))
    return 0


def command_delegate_ready(args: argparse.Namespace) -> int:
    from delegate_runner import load_state, ready_nodes

    try:
        _path, state = load_state(company_kb_root(), args.run_id)
    except DelegateRunnerError as exc:
        raise WorkSuiteError(str(exc)) from exc
    print(
        json.dumps(
            {"run_id": args.run_id, "ready": ready_nodes(state)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_delegate_fail(args: argparse.Namespace) -> int:
    from delegate_runner import fail, load_state

    try:
        _path, state = load_state(company_kb_root(), args.run_id)
        if state["nodes"].get(args.node, {}).get("loop_enabled"):
            if not args.lease_id:
                raise DelegateRunnerError("--lease-id is required for loop-enabled nodes")
            from atomic_loop_support import fail_attempt

            result = fail_attempt(
                company_kb_root(),
                args.run_id,
                args.node,
                args.lease_id,
                args.reason,
            )
        else:
            result = fail(company_kb_root(), args.run_id, args.node, args.reason)
    except DelegateRunnerError as exc:
        raise WorkSuiteError(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_loop_init(
    args: argparse.Namespace,
    resolve_run_id: Resolver,
) -> int:
    from atomic_loop import initialize_loop

    run_id = resolve_run_id(args.company, args.run_id)
    result = initialize_loop(
        company_kb_root(),
        run_id,
        args.skill,
        args.company,
        Path(args.input).resolve(),
        max_attempts=args.max_attempts,
        lease_seconds=args.lease_seconds,
        max_elapsed_seconds=args.max_elapsed_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_loop_next(args: argparse.Namespace) -> int:
    from atomic_loop import next_attempt

    result = next_attempt(company_kb_root(), args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_loop_complete(args: argparse.Namespace) -> int:
    from atomic_loop import complete_attempt

    result = complete_attempt(company_kb_root(), args.run_id, args.lease_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


def command_loop_status(args: argparse.Namespace) -> int:
    from atomic_loop import loop_status

    print(json.dumps(loop_status(company_kb_root(), args.run_id), ensure_ascii=False, indent=2))
    return 0


def command_loop_cancel(args: argparse.Namespace) -> int:
    from atomic_loop import cancel_loop

    result = cancel_loop(company_kb_root(), args.run_id, args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def configure_runtime_parsers(
    subparsers: Any,
    resolve_workspace: Resolver,
    resolve_run_id: Resolver,
) -> None:
    delegate = subparsers.add_parser("delegate")
    commands = delegate.add_subparsers(dest="delegate_command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--skill", required=True)
    init.add_argument("--company", required=True)
    init.add_argument("--run-id")
    init.add_argument("--frozen-kb", action="store_true")
    init.add_argument("--loop", action="store_true")
    init.add_argument("--max-attempts", type=int, default=3)
    init.set_defaults(
        func=partial(
            command_delegate_init,
            resolve_workspace=resolve_workspace,
            resolve_run_id=resolve_run_id,
        )
    )
    for name, handler in (
        ("start", command_delegate_start),
        ("complete", command_delegate_complete),
    ):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
        command.add_argument("--node", required=True)
        if name == "complete":
            command.add_argument("--lease-id")
        command.set_defaults(func=handler)
    status = commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    status.set_defaults(func=command_delegate_status)
    ready = commands.add_parser("ready")
    ready.add_argument("--run-id", required=True)
    ready.set_defaults(func=command_delegate_ready)
    fail = commands.add_parser("fail")
    fail.add_argument("--run-id", required=True)
    fail.add_argument("--node", required=True)
    fail.add_argument("--lease-id")
    fail.add_argument("--reason", required=True)
    fail.set_defaults(func=command_delegate_fail)

    loop = subparsers.add_parser("loop")
    commands = loop.add_subparsers(dest="loop_command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--skill", required=True)
    init.add_argument("--company", required=True)
    init.add_argument("--run-id")
    init.add_argument("--input", required=True)
    init.add_argument("--max-attempts", type=int, default=3)
    init.add_argument("--lease-seconds", type=int, default=3600)
    init.add_argument("--max-elapsed-seconds", type=int, default=86400)
    init.set_defaults(func=partial(command_loop_init, resolve_run_id=resolve_run_id))
    next_command = commands.add_parser("next")
    next_command.add_argument("--run-id", required=True)
    next_command.set_defaults(func=command_loop_next)
    complete = commands.add_parser("complete")
    complete.add_argument("--run-id", required=True)
    complete.add_argument("--lease-id", required=True)
    complete.set_defaults(func=command_loop_complete)
    status = commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    status.set_defaults(func=command_loop_status)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("--run-id", required=True)
    cancel.add_argument("--reason", required=True)
    cancel.set_defaults(func=command_loop_cancel)
