#!/usr/bin/env python3
"""Validate, create, and execute Noetic skill workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KANBAN_RUNS_DIR = Path.home() / ".noeticai" / "kanban-runs"


class WorkSuiteError(Exception):
    pass


def parse_inline_list(value: str, path: Path, line_no: int) -> list[str]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise WorkSuiteError(f"{path}:{line_no}: unsupported shape, use inline list: [a, b]")
    inner = value[1:-1].strip()
    if not inner:
        return []
    return [item.strip().strip("'\"") for item in inner.split(",")]


def parse_workflow(path: Path) -> list[dict[str, object]]:
    stages: list[dict[str, object]] = []
    in_stages = False

    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()

        if line == "stages:" and indent == 0:
            in_stages = True
            continue

        if not in_stages:
            continue

        if indent == 0:
            in_stages = False
            continue

        if indent == 2 and line.startswith("- "):
            body = line[2:].strip()
            if not body.startswith("id:"):
                raise WorkSuiteError(f"{path}:{line_no}: unsupported stage item, expected '- id: ...'")
            stage_id = body.split(":", 1)[1].strip().strip("'\"")
            if not stage_id:
                raise WorkSuiteError(f"{path}:{line_no}: stage id is required")
            stages.append({"id": stage_id})
            continue

        if indent == 4 and ":" in line and stages:
            key, value = [part.strip() for part in line.split(":", 1)]
            if key in {"skills", "inputs", "outputs"}:
                stages[-1][key] = parse_inline_list(value, path, line_no)
            elif key == "parallel":
                if value not in {"true", "false"}:
                    raise WorkSuiteError(f"{path}:{line_no}: parallel must be true or false")
                stages[-1][key] = value == "true"
            elif key == "id":
                stages[-1][key] = value.strip("'\"")
            else:
                raise WorkSuiteError(f"{path}:{line_no}: unsupported workflow field: {key}")
            continue

        raise WorkSuiteError(f"{path}:{line_no}: unsupported shape")

    if not stages:
        raise WorkSuiteError(f"{path}: stages is required")
    return stages


@dataclass
class TaskPlan:
    skill: str
    stage_id: str
    title: str
    body: str
    role: str
    role_skill: str
    required_skills: list[str]
    outputs: list[str]
    parents: list[str]
    task_id: str | None = None


@dataclass
class TriagePlan:
    title: str
    body: str


def skill_dir(skill: str) -> Path:
    return ROOT / "skills" / skill


def workflow_path(skill: str) -> Path:
    return skill_dir(skill) / "references" / "workflow.yaml"


def known_skills() -> set[str]:
    skills = ROOT / "skills"
    return {path.name for path in skills.iterdir() if path.is_dir() and (path / "SKILL.md").exists()}


def load_workflow(skill: str) -> list[dict[str, object]]:
    path = workflow_path(skill)
    if not path.exists():
        raise WorkSuiteError(f"{path}: workflow.yaml is required")
    return parse_workflow(path)


def validate_skill_workflow(skill: str) -> list[dict[str, object]]:
    if skill not in known_skills():
        raise WorkSuiteError(f"unknown skill: {skill}")

    stages = load_workflow(skill)
    skills = known_skills()
    available_outputs: set[str] = set()
    errors: list[str] = []

    for index, stage in enumerate(stages, 1):
        stage_id = str(stage["id"])
        stage_skills = list(stage.get("skills", []))
        if not stage_skills:
            errors.append(f"stage {index} ({stage_id}): skills is required")
        for stage_skill in stage_skills:
            if stage_skill not in skills:
                errors.append(f"stage {stage_id}: unknown skill '{stage_skill}'")
        for artifact in stage.get("inputs", []):
            if artifact not in available_outputs:
                errors.append(f"stage {stage_id}: input '{artifact}' is not produced by a previous stage")
        for artifact in stage.get("outputs", []):
            available_outputs.add(str(artifact))

    if errors:
        raise WorkSuiteError("\n".join(errors))
    return stages


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


def stage_task_outputs(stage_skills: list[str], outputs: list[str]) -> list[list[str]]:
    if not outputs:
        return [[] for _ in stage_skills]
    if len(stage_skills) == 1:
        return [outputs]
    if len(outputs) == len(stage_skills):
        return [[output] for output in outputs]
    raise WorkSuiteError("multi-skill stages must have zero outputs or one output per skill")


def task_body(entry_skill: str, company: str, stage: dict[str, object], skill: str, outputs: list[str], role: str) -> str:
    inputs = ", ".join(stage.get("inputs", [])) or "none"
    output_text = ", ".join(outputs) or "none"
    if role == "gen":
        return f"""执行 Noetic 编排型报告卡片：{skill}

目标公司：{company}
消费前置 artifact：{inputs}
输出最终 artifact：{output_text}
委派角色 skill：noetic-gen-agent

要求：
- 只综合父任务交接中的 artifact、来源、数据时间和 evidence_gaps
- 不重新取数，不补造缺失信息
- 按该 skill 的 SKILL.md 和 card.yaml 输出报告
- 完成时返回最终报告摘要和关键 evidence_gaps
"""
    return f"""执行 Noetic 知识卡片：{skill}

目标公司：{company}
输入 artifact：{inputs}
输出 artifact：{output_text}
委派角色 skill：noetic-data-agent
必需搭配 skill：noetic-karpathy-llm-wiki

要求：
- 按该 skill 的 SKILL.md 和 card.yaml 执行
- 按 noetic-karpathy-llm-wiki 规范优先检索企业信息库 wiki
- 缺失或过期时补齐公开信息并写回 raw/wiki
- 不编造数据，缺失字段写入 evidence_gaps
- 完成时返回 artifact 摘要、来源、数据时间和 evidence_gaps
"""


def build_task_plan(entry_skill: str, company: str, workspace: str) -> list[TaskPlan]:
    stages = validate_skill_workflow(entry_skill)
    output_to_ref: dict[str, str] = {}
    tasks: list[TaskPlan] = []
    last_stage_refs: list[str] = []

    for stage in stages:
        stage_id = str(stage["id"])
        stage_skills = [str(item) for item in stage.get("skills", [])]
        outputs_by_task = stage_task_outputs(stage_skills, [str(item) for item in stage.get("outputs", [])])
        explicit_parents = [output_to_ref[item] for item in stage.get("inputs", [])]
        stage_is_parallel = bool(stage.get("parallel", False))
        previous_ref: str | None = None
        stage_refs: list[str] = []

        for index, stage_skill in enumerate(stage_skills):
            ref = f"task{len(tasks) + 1}"
            parents = list(explicit_parents)
            if not explicit_parents and last_stage_refs:
                parents.extend(last_stage_refs)
            if previous_ref and not stage_is_parallel:
                parents.append(previous_ref)

            role = "gen" if stage_id == "report" or stage_skill == entry_skill else "data"
            role_skill = "noetic-gen-agent" if role == "gen" else "noetic-data-agent"
            required_skills = [role_skill] if role == "gen" else [role_skill, "noetic-karpathy-llm-wiki"]
            outputs = outputs_by_task[index]
            tasks.append(
                TaskPlan(
                    skill=stage_skill,
                    stage_id=stage_id,
                    title=f"[Noetic] {company} / {stage_id} / {stage_skill}",
                    body=task_body(entry_skill, company, stage, stage_skill, outputs, role),
                    role=role,
                    role_skill=role_skill,
                    required_skills=required_skills,
                    outputs=outputs,
                    parents=parents,
                    task_id=ref,
                )
            )
            for output in outputs:
                output_to_ref[output] = ref
            stage_refs.append(ref)
            previous_ref = ref

        last_stage_refs = stage_refs

    return tasks


def kanban_runs_root() -> Path:
    override = os.environ.get("NOETICAI_KANBAN_RUNS_DIR", "").strip()
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
        title=f"[Noetic] {company} / {label}",
        body=f"""目标公司：{company}
编排型 skill 提示：{hint}
期望交付：企业尽调或投资分析类结构化报告（含 evidence_gaps）

要求：
- 按 Noetic 知识卡片拆分前置 data agent 任务与最终 gen 报告任务
- 不指定 assignee，使用 Hermes 默认 agent 承接任务
- 用角色 skill 区分 data / gen 职责
- 子任务需声明输入/输出 artifact 与依赖关系
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
    tasks = build_task_plan(args.skill, args.company, workspace)
    graph = {
        "skill": args.skill,
        "company": args.company,
        "workspace": workspace,
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
    tasks = build_task_plan(args.skill, args.company, workspace)
    resolved_ids: dict[str, str] = {}

    print(f"Noetic workflow execution plan: {args.skill} ({len(tasks)} tasks)")
    print(f"workspace: {workspace}")
    for task in tasks:
        print(f"- {task.task_id}: {task.role} {task.skill} parents={task.parents or []} outputs={task.outputs or []}")

    if args.dry_run or not args.apply:
        print("\nDry run Hermes Kanban commands:")
        for task in tasks:
            command = hermes_command(task, workspace, resolved_ids, args.tenant)
            print(shlex.join(printable_command(command)))
        return 0

    ensure_workspace_dir(workspace)

    for task in tasks:
        command = hermes_command(task, workspace, resolved_ids, args.tenant)
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        task_id = parse_hermes_task_id(result.stdout)
        resolved_ids[str(task.task_id)] = task_id
        print(f"created {task.task_id}: {task_id}")

    return 0


def command_execute_auto(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.company, args.tenant, args.workspace)
    plan = build_triage_plan(args.company, args.skill)

    print(f"Noetic workflow auto triage: {args.company}")
    print(f"- entry hint: {args.skill or 'none'}")
    print(f"workspace: {workspace}")

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


def command_execute_delegate(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.company, args.tenant, args.workspace)
    tasks = build_task_plan(args.skill, args.company, workspace)
    graph = {
        "mode": "delegate",
        "skill": args.skill,
        "company": args.company,
        "workspace": workspace,
        "instructions": "Delegate ready nodes to subagents using node.required_skills. Data nodes must include both noetic-data-agent and noetic-karpathy-llm-wiki; report nodes use noetic-gen-agent. A node is ready when all parent artifacts are available; if subagents are unavailable, run nodes in the current agent in dependency order.",
        "nodes": [
            {
                "id": task.task_id,
                "stage": task.stage_id,
                "skill": task.skill,
                "role": task.role,
                "role_skill": task.role_skill,
                "required_skills": task.required_skills,
                "title": task.title,
                "parents": task.parents,
                "outputs": task.outputs,
                "prompt": task.body,
            }
            for task in tasks
        ],
        "edges": [{"from": parent, "to": task.task_id} for task in tasks for parent in task.parents],
    }
    print(json.dumps(graph, ensure_ascii=False, indent=2))
    return 0


def command_execute(args: argparse.Namespace) -> int:
    validate_execute_args(args)
    if args.mode == "delegate":
        return command_execute_delegate(args)
    if args.mode == "auto":
        return command_execute_auto(args)
    return command_execute_planned(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Noetic workflow helper")
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
    compile_parser.add_argument("--workspace", help="Hermes workspace; default ~/.noeticai/kanban-runs/<tenant>")
    compile_parser.set_defaults(func=command_compile)

    execute = subparsers.add_parser("execute")
    execute.add_argument("--mode", choices=["planned", "auto", "delegate"], required=True)
    execute.add_argument("--skill")
    execute.add_argument("--company", required=True)
    execute.add_argument(
        "--workspace",
        help="Hermes workspace; default dir:~/.noeticai/kanban-runs/<tenant>",
    )
    execute.add_argument("--tenant", help="Hermes Kanban tenant namespace for this workflow batch")
    execute.add_argument("--dry-run", action="store_true")
    execute.add_argument("--apply", action="store_true")
    execute.add_argument(
        "--dispatch",
        action="store_true",
        help="auto mode only: run hermes kanban dispatch after creating the triage task",
    )
    execute.set_defaults(func=command_execute)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (OSError, subprocess.CalledProcessError, WorkSuiteError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
