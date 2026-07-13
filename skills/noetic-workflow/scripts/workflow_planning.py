"""Task planning for Noetic workflow stages."""

from __future__ import annotations

import os
from pathlib import Path

from workflow_contract import TaskPlan, stage_task_outputs, validate_skill_workflow


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COMPANY_KB_DIR = Path.home() / ".noeticai" / "company-knowledge"


def company_kb_root() -> Path:
    override = os.environ.get("NOETICAI_COMPANY_KB_DIR", "").strip()
    return Path(override).expanduser() if override else DEFAULT_COMPANY_KB_DIR


def artifact_root(run_id: str) -> Path:
    return company_kb_root() / "artifacts" / run_id


def task_body(
    entry_skill: str,
    company: str,
    stage: dict[str, object],
    skill: str,
    outputs: list[str],
    role: str,
    run_id: str,
) -> str:
    inputs = ", ".join(stage.get("inputs", [])) or "none"
    output_text = ", ".join(outputs) or "none"
    handoff = artifact_root(run_id) / skill / "handoff.json"
    node_gate = (
        f"python3 {ROOT / 'scripts' / 'check_artifact_gate.py'} --mode node "
        f"--skill {skill} --handoff {handoff} --run-id {run_id} --plugin-root {ROOT}"
    )
    final_gate = ""
    if skill == entry_skill:
        final_gate = (
            "\n- 最终报告 node gate 通过后，再运行终局 gate：\n"
            f"  python3 {ROOT / 'scripts' / 'check_artifact_gate.py'} --mode final "
            f"--skill {entry_skill} --run-dir {company_kb_root()} --run-id {run_id} --plugin-root {ROOT}\n"
        )
    gate_rules = f"""运行隔离与 gate：
- noetic_gate: skill={skill} run_id={run_id}
- 本次 run_id：{run_id}
- handoff 必须写入：{handoff}
- handoff 顶层必须包含相同的 run_id：{run_id}
- handoff.artifacts 必须覆盖该 skill card.yaml 的 gate.required_outputs（或全部 card.yaml outputs）
- 完成前必须运行：{node_gate}
- node gate 非 0 时不得标记完成或交接下游。{final_gate}"""
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
{gate_rules}
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
{gate_rules}
"""


def build_task_plan(entry_skill: str, company: str, workspace: str, run_id: str) -> list[TaskPlan]:
    stages = validate_skill_workflow(entry_skill)
    output_to_ref: dict[str, str] = {}
    tasks: list[TaskPlan] = []
    last_stage_refs: list[str] = []
    for stage in stages:
        stage_id = str(stage["id"])
        stage_skills = [str(item) for item in stage.get("skills", [])]
        outputs_by_task = stage_task_outputs(stage_skills, [str(item) for item in stage.get("outputs", [])])
        explicit_parents = [output_to_ref[item] for item in stage.get("inputs", [])]
        previous_ref: str | None = None
        stage_refs: list[str] = []
        for index, stage_skill in enumerate(stage_skills):
            ref = f"task{len(tasks) + 1}"
            parents = list(explicit_parents) or list(last_stage_refs)
            if previous_ref and not bool(stage.get("parallel", False)):
                parents.append(previous_ref)
            role = "gen" if stage_id == "report" or stage_skill == entry_skill else "data"
            role_skill = "noetic-gen-agent" if role == "gen" else "noetic-data-agent"
            required_skills = [role_skill] if role == "gen" else [role_skill, "noetic-karpathy-llm-wiki"]
            outputs = outputs_by_task[index]
            tasks.append(TaskPlan(
                skill=stage_skill, stage_id=stage_id,
                title=f"[Noetic] {company} / {stage_id} / {stage_skill}",
                body=task_body(entry_skill, company, stage, stage_skill, outputs, role, run_id),
                role=role, role_skill=role_skill, required_skills=required_skills,
                outputs=outputs, parents=parents, task_id=ref,
            ))
            for output in outputs:
                output_to_ref[output] = ref
            stage_refs.append(ref)
            previous_ref = ref
        last_stage_refs = stage_refs
    return tasks
