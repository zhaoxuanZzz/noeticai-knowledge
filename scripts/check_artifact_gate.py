#!/usr/bin/env python3
"""Runtime artifact quality gate for Agent handoff.json outputs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from card_gate import (  # noqa: E402
    DATA_ROLE_FORBIDDEN_KEYS,
    WIKI_WRITEBACK_STATUSES,
    CardGateError,
    load_skill_gate,
    workflow_output_to_skill,
)


def _load_handoff(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CardGateError(f"{path}: unable to read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CardGateError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CardGateError(f"{path}: handoff must be a JSON object")
    return data


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _check_artifact(name: str, value: Any, spec: dict[str, Any], errors: list[str]) -> None:
    check_type = spec["type"]
    if check_type == "list":
        if not isinstance(value, list):
            errors.append(f"artifacts.{name}: expected list, got {type(value).__name__}")
            return
    elif check_type == "object_or_string":
        if not isinstance(value, (dict, str)):
            errors.append(
                f"artifacts.{name}: expected object or string, got {type(value).__name__}"
            )
            return
    if spec.get("non_empty") and not _is_non_empty(value):
        errors.append(f"artifacts.{name}: must be non-empty")


def _check_required_meta(handoff: dict[str, Any], required_meta: list[str], errors: list[str]) -> None:
    for key in required_meta:
        if key not in handoff:
            errors.append(f"missing required_meta: {key}")
            continue
        value = handoff[key]
        if key == "evidence_gaps" and not isinstance(value, list):
            errors.append("evidence_gaps: must be a list")
        elif key == "sources" and not isinstance(value, list):
            errors.append("sources: must be a list")
        elif key == "wiki_writeback":
            if not isinstance(value, dict):
                errors.append("wiki_writeback: must be an object")
            else:
                status = value.get("status")
                if status not in WIKI_WRITEBACK_STATUSES:
                    errors.append(
                        f"wiki_writeback.status must be one of "
                        f"{sorted(WIKI_WRITEBACK_STATUSES)}; got {status!r}"
                    )
                if "paths" in value and not isinstance(value["paths"], list):
                    errors.append("wiki_writeback.paths must be a list")
        elif key in {"subject", "data_as_of", "run_id"}:
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{key}: must be a non-empty string")


def _behavior_no_fabricated_empty_fill(handoff: dict[str, Any], errors: list[str]) -> None:
    top = handoff.get("evidence_gaps")
    if not isinstance(top, list):
        errors.append("behavior no_fabricated_empty_fill: top-level evidence_gaps must be a list")
    artifacts = handoff.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("behavior no_fabricated_empty_fill: artifacts must be an object")
        return
    nested = artifacts.get("evidence_gaps")
    if not isinstance(nested, list):
        errors.append(
            "behavior no_fabricated_empty_fill: artifacts.evidence_gaps must be a list"
        )


def _behavior_data_role_no_final_report(handoff: dict[str, Any], errors: list[str]) -> None:
    if handoff.get("role") != "data":
        return
    for key in DATA_ROLE_FORBIDDEN_KEYS:
        if key in handoff:
            errors.append(
                f"behavior data_role_no_final_report: top-level key '{key}' forbidden when role=data"
            )
    artifacts = handoff.get("artifacts")
    if isinstance(artifacts, dict):
        for key in DATA_ROLE_FORBIDDEN_KEYS:
            if key in artifacts:
                errors.append(
                    f"behavior data_role_no_final_report: artifacts.{key} forbidden when role=data"
                )


BEHAVIOR_RUNNERS = {
    "no_fabricated_empty_fill": _behavior_no_fabricated_empty_fill,
    "data_role_no_final_report": _behavior_data_role_no_final_report,
}


def check_node(
    plugin_root: Path, skill_id: str, handoff_path: Path, run_id: str | None = None
) -> tuple[int, list[str]]:
    """Run node-mode gate. Returns (exit_code, messages)."""
    try:
        _card, gate = load_skill_gate(plugin_root, skill_id)
    except CardGateError as exc:
        return 2, [str(exc)]

    if gate is None:
        return 0, [f"skip: skill '{skill_id}' has no gate section"]

    try:
        handoff = _load_handoff(handoff_path)
    except CardGateError as exc:
        return 1, [str(exc)]

    errors: list[str] = []
    _check_required_meta(handoff, gate["required_meta"], errors)
    if run_id is not None and handoff.get("run_id") != run_id:
        errors.append(f"run_id: expected {run_id!r}, got {handoff.get('run_id')!r}")

    artifacts = handoff.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("artifacts: must be an object")
        artifacts = {}

    for name in gate["required_outputs"]:
        if name not in artifacts:
            errors.append(f"missing required_output in artifacts: {name}")

    for name, spec in gate["artifact_checks"].items():
        if name not in artifacts:
            errors.append(f"artifact_checks: missing artifacts.{name}")
            continue
        _check_artifact(name, artifacts[name], spec, errors)

    for item in gate["behavior_checks"]:
        runner = BEHAVIOR_RUNNERS.get(item["id"])
        if runner is None:
            errors.append(f"unimplemented behavior_check id: {item['id']}")
            continue
        runner(handoff, errors)

    report_path = handoff.get("report_path")
    if isinstance(report_path, str) and report_path.strip():
        report_file = (handoff_path.parent / report_path).resolve()
        if not report_file.is_file():
            errors.append(f"report_path not readable: {report_path}")

    if errors:
        return 1, errors
    return 0, ["ok"]


def _parse_workflow_stages(path: Path) -> list[dict[str, object]]:
    # Local import to avoid circular dependency with validate_work_suite at module load.
    from validate_work_suite import WorkSuiteError, parse_workflow

    try:
        return parse_workflow(path)
    except WorkSuiteError as exc:
        raise CardGateError(str(exc)) from exc


def _check_handoff_run_id(handoff: dict[str, Any], path: Path, run_id: str, errors: list[str]) -> None:
    if handoff.get("run_id") != run_id:
        errors.append(f"{path}: run_id: expected {run_id!r}, got {handoff.get('run_id')!r}")


def check_final(
    plugin_root: Path, skill_id: str, run_dir: Path, run_id: str | None = None
) -> tuple[int, list[str]]:
    """Run final-mode gate. Returns (exit_code, messages).

    ``run_dir`` is the company knowledge-base root (same as raw/wiki),
    i.e. ``NOETICAI_COMPANY_KB_DIR`` or ``~/.noeticai/company-knowledge``.
    """
    try:
        _card, gate = load_skill_gate(plugin_root, skill_id)
    except CardGateError as exc:
        return 2, [str(exc)]

    if gate is None or not gate.get("final"):
        return 0, [f"skip: skill '{skill_id}' has no gate.final section"]
    if not run_id:
        return 2, ["--run-id is required for --mode final"]

    final = gate["final"]
    workflow_path = plugin_root / "skills" / skill_id / "references" / "workflow.yaml"
    if not workflow_path.exists():
        return 2, [f"{workflow_path}: required for gate.final"]

    try:
        stages = _parse_workflow_stages(workflow_path)
        output_to_skill = workflow_output_to_skill(stages)
    except CardGateError as exc:
        return 2, [str(exc)]

    errors: list[str] = []
    for artifact in final.get("require_parent_artifacts") or []:
        producer = output_to_skill.get(artifact)
        if not producer:
            errors.append(f"cannot resolve parent artifact '{artifact}' to a skill")
            continue
        handoff = run_dir / "artifacts" / run_id / producer / "handoff.json"
        if not handoff.is_file():
            errors.append(f"missing parent handoff for '{artifact}': {handoff}")
            continue
        try:
            _check_handoff_run_id(_load_handoff(handoff), handoff, run_id, errors)
        except CardGateError as exc:
            errors.append(str(exc))

    if final.get("require_report_handoff"):
        report_handoff = run_dir / "artifacts" / run_id / skill_id / "handoff.json"
        if not report_handoff.is_file():
            errors.append(f"missing report handoff: {report_handoff}")
        else:
            try:
                _check_handoff_run_id(_load_handoff(report_handoff), report_handoff, run_id, errors)
            except CardGateError as exc:
                errors.append(str(exc))

    if errors:
        return 1, errors
    return 0, ["ok"]


def main(argv: list[str]) -> int:
    mode = None
    skill_id = None
    handoff = None
    run_dir = None
    run_id = None
    plugin_root = Path(".").resolve()

    args = list(argv)
    while args:
        flag = args.pop(0)
        if flag == "--mode" and args:
            mode = args.pop(0)
        elif flag == "--skill" and args:
            skill_id = args.pop(0)
        elif flag == "--handoff" and args:
            handoff = Path(args.pop(0))
        elif flag == "--run-dir" and args:
            run_dir = Path(args.pop(0))
        elif flag == "--run-id" and args:
            run_id = args.pop(0)
        elif flag == "--plugin-root" and args:
            plugin_root = Path(args.pop(0)).resolve()
        else:
            print(
                "usage: python3 scripts/check_artifact_gate.py "
                "--mode node --skill <id> --handoff <path> [--run-id <id>] [--plugin-root .]\n"
                "       python3 scripts/check_artifact_gate.py "
                "--mode final --skill <id> --run-dir <path> --run-id <id> [--plugin-root .]",
                file=sys.stderr,
            )
            return 2

    if mode not in {"node", "final"} or not skill_id:
        print(
            "usage: python3 scripts/check_artifact_gate.py "
            "--mode node|final --skill <id> ...",
            file=sys.stderr,
        )
        return 2

    if mode == "node":
        if handoff is None:
            print("error: --handoff is required for --mode node", file=sys.stderr)
            return 2
        code, messages = check_node(plugin_root, skill_id, handoff, run_id)
    else:
        if run_dir is None:
            print("error: --run-dir is required for --mode final", file=sys.stderr)
            return 2
        code, messages = check_final(plugin_root, skill_id, run_dir, run_id)

    stream = sys.stdout if code == 0 else sys.stderr
    for message in messages:
        prefix = "OK" if code == 0 else "ERROR"
        print(f"{prefix}: {message}", file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
