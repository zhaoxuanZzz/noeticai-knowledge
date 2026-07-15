#!/usr/bin/env python3
"""Validate, create, and execute CWS skill workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

from workflow_contract import (
    TaskPlan,
    TriagePlan,
    WorkSuiteError,
    known_skills,
    parse_workflow,
    validate_auto_plan,
    validate_skill_workflow,
    workflow_path,
)
from workflow_planning import build_task_plan, company_kb_root, task_body
from delegate_runner import DelegateRunnerError
from workflow_runtime_cli import command_execute_delegate, configure_runtime_parsers


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KANBAN_RUNS_DIR = Path.home() / ".cws" / "kanban-runs"
def command_validate(args: argparse.Namespace) -> int:
    stages = validate_skill_workflow(args.skill)
    print(f"OK: {args.skill} workflow ({len(stages)} stages)")
    return 0


def command_create(args: argparse.Namespace) -> int:
    if args.skill not in known_skills():
        raise WorkSuiteError(f"unknown skill: {args.skill}")

    path = workflow_path(args.skill)
    if path.exists() and not args.force:
        raise WorkSuiteError(f"{path}: already exists; pass --force to overwrite")

    if args.from_template:
        source = Path(args.from_template)
        text = source.read_text(encoding="utf-8")
        parse_workflow(source)
    else:
        text = f"name: {args.skill}\nstages:\n  - id: report\n    skills: [{args.skill}]\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"created: {path}")
    return 0


def resolve_run_id(company: str, requested: str | None) -> str:
    if requested:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", requested):
            raise WorkSuiteError("--run-id must use lowercase letters, digits, and hyphens")
        return requested
    return f"run-{date.today().strftime('%Y%m%d')}-{company_slug(company)}-{uuid.uuid4().hex[:8]}"


def kanban_runs_root() -> Path:
    override = os.environ.get("CWS_KANBAN_RUNS_DIR", "").strip()
    return Path(override).expanduser() if override else DEFAULT_KANBAN_RUNS_DIR


def company_slug(company: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    if ascii_slug:
        return ascii_slug[:32]
    digest = hashlib.sha256(company.encode("utf-8")).hexdigest()
    return digest[:8]


def default_tenant(company: str) -> str:
    return f"batch-{date.today().strftime('%Y%m%d')}-{company_slug(company)}"


def resolve_workspace(company: str, tenant: str | None, workspace: str | None) -> str:
    if workspace:
        return workspace
    run_id = tenant or default_tenant(company)
    run_dir = kanban_runs_root() / run_id
    return f"dir:{run_dir.resolve()}"


def ensure_workspace_dir(workspace: str) -> None:
    if workspace.startswith("dir:"):
        Path(workspace[4:]).mkdir(parents=True, exist_ok=True)


def build_triage_plan(company: str, skill_hint: str | None) -> TriagePlan:
    label = skill_hint or "workflow"
    hint = skill_hint or "由编排器根据需求判断"
    return TriagePlan(
        title=f"[CWS] {company} / {label}",
        body=f"""目标公司：{company}
编排型 skill 提示：{hint}
期望交付：企业尽调或投资分析类结构化报告（含 evidence_gaps）

要求：
- 按 CWS 知识卡片拆分前置 data agent 任务与最终 gen 报告任务
- 不指定 assignee，使用 Hermes 默认 agent 承接任务
- 用角色 skill 区分 data / gen 职责
- 子任务需声明输入/输出 artifact 与依赖关系
- auto 拆图不保证静态 workflow 的 gate 合规；不得标记为已通过编排 gate
""",
    )


def hermes_command(task: TaskPlan, workspace: str, resolved_ids: dict[str, str], tenant: str | None = None) -> list[str]:
    command = [
        "hermes",
        "kanban",
        "create",
        task.title,
        "--body",
        task.body,
        "--skill",
        task.skill,
        "--workspace",
        workspace,
        "--json",
    ]
    if tenant:
        command.extend(["--tenant", tenant])
    for parent in task.parents:
        command.extend(["--parent", resolved_ids.get(parent, parent)])
    return command


def hermes_triage_command(plan: TriagePlan, workspace: str, tenant: str | None = None) -> list[str]:
    command = [
        "hermes",
        "kanban",
        "create",
        plan.title,
        "--body",
        plan.body,
        "--triage",
        "--workspace",
        workspace,
        "--json",
    ]
    if tenant:
        command.extend(["--tenant", tenant])
    return command


def parse_hermes_task_id(stdout: str) -> str:
    data = json.loads(stdout)
    task_id = str(data.get("id") or data.get("task_id") or data.get("task", {}).get("id"))
    if not task_id or task_id == "None":
        raise WorkSuiteError(f"cannot read task id from hermes output: {stdout}")
    return task_id


def printable_command(command: list[str]) -> list[str]:
    printed = list(command)
    body_index = printed.index("--body") + 1
    printed[body_index] = "<task body>"
    return printed


def command_compile(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.company, args.tenant, args.workspace)
    run_id = resolve_run_id(args.company, args.run_id)
    tasks = build_task_plan(args.skill, args.company, workspace, run_id)
    graph = {
        "skill": args.skill,
        "company": args.company,
        "workspace": workspace,
        "run_id": run_id,
        "nodes": [
            {
                "id": task.task_id,
                "stage": task.stage_id,
                "skill": task.skill,
                "role": task.role,
                "role_skill": task.role_skill,
                "required_skills": task.required_skills,
                "title": task.title,
                "body": task.body,
                "outputs": task.outputs,
            }
            for task in tasks
        ],
        "edges": [{"from": parent, "to": task.task_id} for task in tasks for parent in task.parents],
    }
    print(json.dumps(graph, ensure_ascii=False, indent=2))
    return 0


def validate_execute_args(args: argparse.Namespace) -> None:
    if args.loop and args.mode not in {"planned", "delegate", "auto"}:
        raise WorkSuiteError("--loop is only supported for workflow execution")
    if args.frozen_kb and args.mode != "delegate":
        raise WorkSuiteError("--frozen-kb is only supported for delegate mode")
    if args.plan and (args.mode != "auto" or not args.loop):
        raise WorkSuiteError("--plan requires --mode auto --loop")
    if args.mode == "auto" and args.loop and not args.skill:
        raise WorkSuiteError("--skill is required for auto loop")
    if args.mode in {"planned", "delegate"}:
        if not args.skill:
            raise WorkSuiteError(f"--skill is required for {args.mode} mode")
        if args.dispatch:
            raise WorkSuiteError("--dispatch is only supported for auto mode")
        return
    if args.dispatch and not args.apply:
        raise WorkSuiteError("--dispatch requires --apply")
    if args.skill and args.skill not in known_skills():
        raise WorkSuiteError(f"unknown skill: {args.skill}")


def command_execute_planned(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.company, args.tenant, args.workspace)
    run_id = resolve_run_id(args.company, args.run_id)
    tasks = build_task_plan(
        args.skill,
        args.company,
        workspace,
        run_id,
        include_gate_instructions=not args.loop,
    )
    if args.loop:
        for task in tasks:
            context_path = (
                company_kb_root()
                / "artifacts"
                / run_id
                / "contexts"
                / f"{task.task_id}.json"
            )
            task.body += (
                "\nCWS Runner 上下文：\n"
                f"- cws_loop: run_id={run_id} node={task.task_id}\n"
                f"- 当前 attempt 上下文：{context_path}\n"
                "- 只写入该上下文所指向 maker-context.json 的 output_dir。\n"
            )
    return execute_kanban_plan(args, tasks, run_id, workspace)


def execute_kanban_plan(
    args: argparse.Namespace,
    tasks: list[TaskPlan],
    run_id: str,
    workspace: str,
) -> int:
    resolved_ids: dict[str, str] = {}

    print(f"CWS workflow execution plan: {args.skill} ({len(tasks)} tasks)")
    print(f"workspace: {workspace}")
    print(f"run_id: {run_id}")
    print(f"loop: {'enabled' if args.loop else 'disabled'}")
    for task in tasks:
        print(f"- {task.task_id}: {task.role} {task.skill} parents={task.parents or []} outputs={task.outputs or []}")

    if args.dry_run or not args.apply:
        print("\nDry run Hermes Kanban commands:")
        for task in tasks:
            command = hermes_command(task, workspace, resolved_ids, args.tenant)
            print(shlex.join(printable_command(command)))
        return 0

    ensure_workspace_dir(workspace)

    if args.loop:
        from atomic_loop import enable_delegate_loop
        from delegate_runner import initialize
        from workflow_runtime_cli import structured_delegate_nodes

        initialize(
            company_kb_root(),
            run_id,
            args.skill,
            args.company,
            structured_delegate_nodes(args.skill, run_id, tasks, loop_enabled=True),
        )
        enable_delegate_loop(
            company_kb_root(), run_id, max_attempts=args.max_attempts
        )

    for task in tasks:
        command = hermes_command(task, workspace, resolved_ids, args.tenant)
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        task_id = parse_hermes_task_id(result.stdout)
        resolved_ids[str(task.task_id)] = task_id
        if args.loop:
            from atomic_loop import bind_kanban_task

            bind_kanban_task(company_kb_root(), run_id, str(task.task_id), task_id)
        print(f"created {task.task_id}: {task_id}")

    if args.dispatch:
        subprocess.run(
            ["hermes", "kanban", "dispatch"],
            text=True,
            capture_output=True,
            check=True,
        )
        print("dispatched: nudged Hermes kanban dispatcher")

    return 0


def build_auto_task_plan(
    path: Path,
    entry_skill: str,
    company: str,
    run_id: str,
) -> list[TaskPlan]:
    tasks: list[TaskPlan] = []
    for node in validate_auto_plan(path, entry_skill):
        skill = str(node["skill"])
        node_id = str(node["id"])
        role = "gen" if skill == entry_skill else "data"
        role_skill = "cws-gen-agent" if role == "gen" else "cws-data-agent"
        outputs = [str(item) for item in node["outputs"]]
        stage = {"id": node_id, "inputs": list(node["inputs"])}
        tasks.append(
            TaskPlan(
                skill=skill,
                stage_id=node_id,
                title=f"[CWS] {company} / {node_id} / {skill}",
                body=task_body(
                    entry_skill,
                    company,
                    stage,
                    skill,
                    outputs,
                    role,
                    run_id,
                    include_gate_instructions=False,
                ),
                role=role,
                role_skill=role_skill,
                required_skills=(
                    [role_skill]
                    if role == "gen"
                    else [role_skill, "cws-karpathy-llm-wiki"]
                ),
                outputs=outputs,
                parents=[str(item) for item in node["parents"]],
                task_id=node_id,
            )
        )
    return tasks


def command_execute_auto(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.company, args.tenant, args.workspace)
    if args.plan:
        run_id = resolve_run_id(args.company, args.run_id)
        tasks = build_auto_task_plan(
            Path(args.plan).resolve(), args.skill, args.company, run_id
        )
        for task in tasks:
            context_path = (
                company_kb_root()
                / "artifacts"
                / run_id
                / "contexts"
                / f"{task.task_id}.json"
            )
            task.body += (
                "\nCWS Runner 上下文：\n"
                f"- cws_loop: run_id={run_id} node={task.task_id}\n"
                f"- 当前 attempt 上下文：{context_path}\n"
                "- 只写入该上下文所指向 maker-context.json 的 output_dir。\n"
            )
        print(f"validated auto plan: {args.plan}")
        return execute_kanban_plan(args, tasks, run_id, workspace)
    plan = build_triage_plan(args.company, args.skill)
    if args.loop:
        run_id = resolve_run_id(args.company, args.run_id)
        plan_path = company_kb_root() / "artifacts" / run_id / "auto-plan.json"
        submit_command = [
                "python3",
                str(Path(__file__).resolve()),
                "execute",
                "--mode",
                "auto",
                "--loop",
                "--plan",
                str(plan_path),
                "--skill",
                args.skill,
                "--company",
                args.company,
                "--run-id",
                run_id,
                "--max-attempts",
                str(args.max_attempts),
                "--apply",
            ]
        if args.tenant:
            submit_command.extend(["--tenant", args.tenant])
        if args.workspace:
            submit_command.extend(["--workspace", args.workspace])
        if args.dispatch:
            submit_command.append("--dispatch")
        submit = shlex.join(submit_command)
        plan.body += f"""

Auto Loop 计划交付：
- cws_auto_plan: run_id={run_id}
- 只生成结构化候选计划，不直接创建执行子任务
- 将候选计划写入：{plan_path}
- JSON 顶层为 nodes；每个节点包含 id、skill、parents、inputs、outputs
- 写入后运行以下命令，由 CWS 校验并提交 Kanban：
  {submit}
"""

    print(f"CWS workflow auto triage: {args.company}")
    print(f"- entry hint: {args.skill or 'none'}")
    print(f"workspace: {workspace}")
    print("gate: auto mode does not guarantee static workflow gate compliance")

    command = hermes_triage_command(plan, workspace, args.tenant)

    if args.dry_run or not args.apply:
        print("\nDry run Hermes Kanban command:")
        print(shlex.join(printable_command(command)))
        return 0

    ensure_workspace_dir(workspace)
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    task_id = parse_hermes_task_id(result.stdout)
    print(f"created triage: {task_id}")
    print("\nNext steps:")
    print("- ensure gateway is running: hermes gateway start")
    print("- ensure kanban.auto_decompose is true in ~/.hermes/config.yaml")
    print(f"- watch decomposition: hermes kanban show {task_id}")
    print("- or: hermes kanban list")

    if args.dispatch:
        subprocess.run(["hermes", "kanban", "dispatch"], text=True, capture_output=True, check=True)
        print("dispatched: nudged Hermes kanban dispatcher")

    return 0


def command_execute(args: argparse.Namespace) -> int:
    validate_execute_args(args)
    if args.mode == "delegate":
        return command_execute_delegate(args, resolve_workspace, resolve_run_id)
    if args.mode == "auto":
        return command_execute_auto(args)
    return command_execute_planned(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CWS workflow helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--skill", required=True)
    validate.set_defaults(func=command_validate)

    create = subparsers.add_parser("create")
    create.add_argument("--skill", required=True)
    create.add_argument("--from-template")
    create.add_argument("--force", action="store_true")
    create.set_defaults(func=command_create)

    compile_parser = subparsers.add_parser("compile")
    compile_parser.add_argument("--skill", required=True)
    compile_parser.add_argument("--company", required=True)
    compile_parser.add_argument("--tenant", help="Hermes Kanban tenant namespace for this workflow batch")
    compile_parser.add_argument("--workspace", help="Hermes workspace; default ~/.cws/kanban-runs/<tenant>")
    compile_parser.add_argument("--run-id", help="Artifact run namespace; generated when omitted")
    compile_parser.set_defaults(func=command_compile)

    execute = subparsers.add_parser("execute")
    execute.add_argument("--mode", choices=["planned", "auto", "delegate"], required=True)
    execute.add_argument("--skill")
    execute.add_argument("--company", required=True)
    execute.add_argument(
        "--workspace",
        help="Hermes workspace; default dir:~/.cws/kanban-runs/<tenant>",
    )
    execute.add_argument("--tenant", help="Hermes Kanban tenant namespace for this workflow batch")
    execute.add_argument("--run-id", help="Artifact run namespace; generated for planned/delegate when omitted")
    execute.add_argument("--dry-run", action="store_true")
    execute.add_argument("--apply", action="store_true")
    execute.add_argument("--frozen-kb", action="store_true")
    execute.add_argument("--loop", action="store_true")
    execute.add_argument("--max-attempts", type=int, default=3)
    execute.add_argument("--plan", help="auto loop candidate plan JSON")
    execute.add_argument(
        "--dispatch",
        action="store_true",
        help="auto mode only: run hermes kanban dispatch after creating the triage task",
    )
    execute.set_defaults(func=command_execute)

    configure_runtime_parsers(subparsers, resolve_workspace, resolve_run_id)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (DelegateRunnerError, OSError, subprocess.CalledProcessError, WorkSuiteError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
