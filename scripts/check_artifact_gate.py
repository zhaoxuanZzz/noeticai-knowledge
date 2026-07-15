#!/usr/bin/env python3
"""Runtime artifact quality gate for Agent handoff.json outputs."""

from __future__ import annotations

import datetime as dt
import json
import sys
import uuid
from dataclasses import dataclass, field
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
from gate_judge_adapter import (  # noqa: E402
    RUNTIME_MIN_CONFIDENCE,
    apply_confidence_gate,
    build_judge_request,
    minimize_handoff,
    run_judge_once,
)
from semantic_gate import (  # noqa: E402
    SemanticGateError,
    evaluate_final_context,
    evaluate_semantic,
    load_json_object,
)


@dataclass
class GateOutcome:
    """Structured node/final gate result."""

    exit_code: int
    messages: list[str]
    decision: str = "passed"
    judge: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def __iter__(self):
        """Backward-compatible unpacking: code, messages = outcome."""
        yield self.exit_code
        yield self.messages


def _write_gate_result_file(directory: Path, payload: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "gate-result.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def _load_report_text(handoff: dict[str, Any], handoff_path: Path) -> str:
    report_path = handoff.get("report_path")
    if not isinstance(report_path, str) or not report_path.strip():
        return ""
    candidate = (handoff_path.parent / report_path).resolve()
    try:
        candidate.relative_to(handoff_path.parent.resolve())
    except ValueError:
        return ""
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return ""


def _run_semantic_judge(
    plugin_root: Path,
    skill_id: str,
    gate: dict[str, Any],
    handoff: dict[str, Any],
    handoff_path: Path,
    evidence: dict[str, Any],
    *,
    parent_handoffs: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    """Return (decision, messages, judge_payload)."""
    judge_cfg = (gate.get("semantic") or {}).get("judge") or {}
    rubric_id = judge_cfg.get("rubric", "company-profile-v1")
    request = build_judge_request(
        request_id=f"runtime-{skill_id}-{uuid.uuid4().hex[:12]}",
        case_id=f"runtime-{skill_id}",
        evaluator_id=f"{skill_id}-semantic-v1",
        rubric_id=rubric_id,
        subject=evidence.get("subject") or handoff.get("subject") or {},
        evidence=evidence,
        handoff=handoff,
        parent_handoffs=parent_handoffs or [],
        report=_load_report_text(handoff, handoff_path),
    )
    judge_result = apply_confidence_gate(
        run_judge_once(plugin_root, request),
        min_confidence=RUNTIME_MIN_CONFIDENCE,
    )
    decision = judge_result.get("decision", "needs_review")
    if decision not in {"passed", "needs_review"}:
        decision = "needs_review"
    reasons = sorted(
        {
            item.get("reason")
            for item in judge_result.get("findings") or []
            if isinstance(item, dict) and isinstance(item.get("reason"), str)
        }
    )
    if decision == "passed":
        return "passed", ["ok"], judge_result
    if not reasons:
        reasons = ["judge_needs_review"]
    messages = [f"{reason}: semantic judge requires review" for reason in reasons]
    return "needs_review", messages, judge_result


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
    plugin_root: Path,
    skill_id: str,
    handoff_path: Path,
    run_id: str | None = None,
    check_semantic: bool = True,
    check_judge: bool = True,
) -> GateOutcome:
    """Run node-mode gate. Returns GateOutcome (unpackable as exit_code, messages)."""
    try:
        _card, gate = load_skill_gate(plugin_root, skill_id)
    except CardGateError as exc:
        return GateOutcome(2, [str(exc)], decision="blocked")

    if gate is None:
        return GateOutcome(0, [f"skip: skill '{skill_id}' has no gate section"], decision="skip")

    try:
        handoff = _load_handoff(handoff_path)
    except CardGateError as exc:
        return GateOutcome(1, [str(exc)], decision="blocked")

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

    evidence: dict[str, Any] | None = None
    if check_semantic and gate.get("semantic"):
        evidence_path = handoff_path.parent / "evidence.json"
        if not evidence_path.is_file():
            errors.append(f"evidence_missing: required semantic evidence: {evidence_path}")
        else:
            try:
                evidence = load_json_object(evidence_path)
                semantic_reasons = evaluate_semantic(
                    handoff,
                    evidence,
                    handoff_path.parent,
                    dt.date.today().isoformat(),
                    gate["semantic"].get("required_claims"),
                    gate["semantic"].get("deterministic_checks"),
                )
                errors.extend(
                    f"{reason}: deterministic semantic check failed"
                    for reason in semantic_reasons
                )
            except SemanticGateError as exc:
                errors.append(f"evidence_invalid: {exc}")

    report_path = handoff.get("report_path")
    if isinstance(report_path, str) and report_path.strip():
        report_file = (handoff_path.parent / report_path).resolve()
        if not report_file.is_file():
            errors.append(f"report_path not readable: {report_path}")

    if errors:
        return GateOutcome(1, errors, decision="blocked")

    if (
        check_semantic
        and check_judge
        and gate.get("semantic")
        and (gate["semantic"].get("judge") is not None)
        and evidence is not None
    ):
        decision, messages, judge_result = _run_semantic_judge(
            plugin_root, skill_id, gate, handoff, handoff_path, evidence
        )
        if decision != "passed":
            return GateOutcome(1, messages, decision=decision, judge=judge_result)
        return GateOutcome(0, messages, decision="passed", judge=judge_result)

    return GateOutcome(0, ["ok"], decision="passed")


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
    plugin_root: Path,
    skill_id: str,
    run_dir: Path,
    run_id: str | None = None,
    check_judge: bool = True,
    candidate_skill_dir: Path | None = None,
) -> GateOutcome:
    """Run final-mode gate. Returns GateOutcome.

    ``run_dir`` is the company knowledge-base root (same as raw/wiki),
    i.e. ``CWS_COMPANY_KB_DIR`` or ``~/.cws/company-knowledge``.
    """
    try:
        _card, gate = load_skill_gate(plugin_root, skill_id)
    except CardGateError as exc:
        return GateOutcome(2, [str(exc)], decision="blocked")

    if gate is None or not gate.get("final"):
        return GateOutcome(
            0, [f"skip: skill '{skill_id}' has no gate.final section"], decision="skip"
        )
    if not run_id:
        return GateOutcome(2, ["--run-id is required for --mode final"], decision="blocked")

    final = gate["final"]
    workflow_path = plugin_root / "skills" / skill_id / "references" / "workflow.yaml"
    if not workflow_path.exists():
        return GateOutcome(2, [f"{workflow_path}: required for gate.final"], decision="blocked")

    try:
        stages = _parse_workflow_stages(workflow_path)
        output_to_skill = workflow_output_to_skill(stages)
    except CardGateError as exc:
        return GateOutcome(2, [str(exc)], decision="blocked")

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

    report_handoff: Path | None = None
    if final.get("require_report_handoff"):
        report_handoff = (
            candidate_skill_dir / "handoff.json"
            if candidate_skill_dir is not None
            else run_dir / "artifacts" / run_id / skill_id / "handoff.json"
        )
        if not report_handoff.is_file():
            errors.append(f"missing report handoff: {report_handoff}")
        else:
            try:
                _check_handoff_run_id(_load_handoff(report_handoff), report_handoff, run_id, errors)
            except CardGateError as exc:
                errors.append(str(exc))

    errors.extend(
        f"{reason}: deterministic final semantic check failed"
        for reason in evaluate_final_context(run_dir, run_id, report_handoff if candidate_skill_dir else None)
    )

    evidence: dict[str, Any] | None = None
    report_payload: dict[str, Any] | None = None
    if not errors and gate.get("semantic") and report_handoff is not None:
        evidence_path = report_handoff.parent / "evidence.json"
        if not evidence_path.is_file():
            errors.append(f"evidence_missing: required semantic evidence: {evidence_path}")
        else:
            try:
                report_payload = _load_handoff(report_handoff)
                evidence = load_json_object(evidence_path)
                semantic_reasons = evaluate_semantic(
                    report_payload,
                    evidence,
                    report_handoff.parent,
                    dt.date.today().isoformat(),
                    gate["semantic"].get("required_claims"),
                    gate["semantic"].get("deterministic_checks"),
                )
                errors.extend(
                    f"{reason}: deterministic final semantic check failed"
                    for reason in semantic_reasons
                )
            except (CardGateError, SemanticGateError) as exc:
                errors.append(f"evidence_invalid: {exc}")

    if errors:
        return GateOutcome(1, errors, decision="blocked")

    if (
        check_judge
        and gate.get("semantic")
        and (gate["semantic"].get("judge") is not None)
        and report_handoff is not None
        and evidence is not None
        and report_payload is not None
    ):
        parent_handoffs: list[dict[str, Any]] = []
        artifact_root = run_dir / "artifacts" / run_id
        for parent_path in sorted(artifact_root.glob("*/handoff.json")):
            if parent_path == report_handoff:
                continue
            try:
                parent_handoffs.append(minimize_handoff(_load_handoff(parent_path)))
            except CardGateError:
                continue
        decision, messages, judge_result = _run_semantic_judge(
            plugin_root,
            skill_id,
            gate,
            report_payload,
            report_handoff,
            evidence,
            parent_handoffs=parent_handoffs,
        )
        if decision != "passed":
            return GateOutcome(1, messages, decision=decision, judge=judge_result)
        return GateOutcome(0, messages, decision="passed", judge=judge_result)

    return GateOutcome(0, ["ok"], decision="passed")


def main(argv: list[str]) -> int:
    mode = None
    skill_id = None
    handoff = None
    run_dir = None
    run_id = None
    plugin_root = Path(".").resolve()
    write_result = True

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
        elif flag == "--no-write-result":
            write_result = False
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

    result_dir: Path | None = None
    if mode == "node":
        if handoff is None:
            print("error: --handoff is required for --mode node", file=sys.stderr)
            return 2
        outcome = check_node(plugin_root, skill_id, handoff, run_id)
        result_dir = handoff.parent
    else:
        if run_dir is None:
            print("error: --run-dir is required for --mode final", file=sys.stderr)
            return 2
        outcome = check_final(plugin_root, skill_id, run_dir, run_id)
        if run_id:
            result_dir = run_dir / "artifacts" / run_id / skill_id

    if write_result and result_dir is not None and outcome.decision != "skip":
        if outcome.decision == "passed":
            status = "passed"
        elif outcome.decision == "needs_review":
            status = "needs_review"
        else:
            status = "blocked"
        payload: dict[str, Any] = {
            "skill_id": skill_id,
            "mode": mode,
            "run_id": run_id,
            "decision": outcome.decision,
            "status": status,
            "exit_code": outcome.exit_code,
            "errors": [] if outcome.exit_code == 0 else list(outcome.messages),
        }
        if outcome.judge is not None:
            payload["judge"] = outcome.judge
        _write_gate_result_file(result_dir, payload)
        if outcome.decision == "needs_review":
            try:
                from gate_review import write_review_pack_from_handoff_dir

                write_review_pack_from_handoff_dir(result_dir)
            except Exception as exc:  # noqa: BLE001 — review pack must not change gate exit
                print(f"warning: failed to write review pack: {exc}", file=sys.stderr)

    stream = sys.stdout if outcome.exit_code == 0 else sys.stderr
    for message in outcome.messages:
        prefix = "OK" if outcome.exit_code == 0 else "ERROR"
        print(f"{prefix}: {message}", file=stream)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
