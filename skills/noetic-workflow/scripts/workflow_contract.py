"""Parsing and validation for the supported workflow YAML subset."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class WorkSuiteError(Exception):
    pass


def parse_inline_list(value: str, path: Path, line_no: int) -> list[str]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise WorkSuiteError(f"{path}:{line_no}: unsupported shape, use inline list: [a, b]")
    inner = value[1:-1].strip()
    return [] if not inner else [item.strip().strip("'\"") for item in inner.split(",")]


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


def stage_task_outputs(stage_skills: list[str], outputs: list[str]) -> list[list[str]]:
    if not outputs:
        return [[] for _ in stage_skills]
    if len(stage_skills) == 1:
        return [outputs]
    if len(outputs) == len(stage_skills):
        return [[output] for output in outputs]
    raise WorkSuiteError("multi-skill stages must have zero outputs or one output per skill")
