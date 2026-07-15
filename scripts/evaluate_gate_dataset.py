#!/usr/bin/env python3
"""Replay the frozen artifact-gate dataset and write regression reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from check_artifact_gate import check_final, check_node  # noqa: E402
from card_gate import CardGateError, load_skill_gate  # noqa: E402
from gate_judge_adapter import (  # noqa: E402
    PROTOCOL_VERSION,
    JudgeAdapter,
    JudgeAdapterError,
    default_judge_script,
    judge_launch_command,
    minimize_handoff,
    resolve_judge_path,
)
from semantic_gate import (  # noqa: E402
    CHECKER_VERSION,
    SemanticGateError,
    evaluate_semantic,
    load_json_object,
)


ALLOWED_DECISIONS = {"passed", "blocked", "needs_review"}
REASON_BY_MESSAGE = (
    ("evidence_missing", "evidence_missing"),
    ("evidence_path_invalid", "evidence_path_invalid"),
    ("evidence_value_mismatch", "evidence_value_mismatch"),
    ("subject_identity_mismatch", "subject_identity_mismatch"),
    ("evidence_subject_mismatch", "evidence_subject_mismatch"),
    ("source_conflict_not_disclosed", "source_conflict_not_disclosed"),
    ("stale_data_not_disclosed", "stale_data_not_disclosed"),
    ("negative_claim_without_search_coverage", "negative_claim_without_search_coverage"),
    ("invalid JSON", "handoff_invalid_json"),
    ("unable to read", "handoff_missing"),
    ("missing required_output", "required_output_missing"),
    ("must be non-empty", "required_output_empty"),
    ("run_id", "run_id_mismatch"),
    ("final_report", "data_role_contains_final_report"),
    ("missing parent handoff", "parent_handoff_missing"),
    ("missing report handoff", "handoff_missing"),
    ("cross_subject_artifact", "cross_subject_artifact"),
    ("parent_gate_blocked", "parent_gate_blocked"),
)


class DatasetError(Exception):
    """Invalid dataset or adapter infrastructure."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetError(f"{path}: unable to read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{path}: invalid JSON: {exc}") from exc


def _resolve(root: Path, value: str) -> Path:
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise DatasetError(f"path escapes dataset root: {value}") from exc
    return candidate


def _discover_cases(dataset: Path) -> list[dict[str, Any]]:
    manifests = [dataset / "cases.json"] if (dataset / "cases.json").is_file() else []
    manifests.extend(sorted((dataset / "manifests").glob("*.json")))
    if not manifests:
        raise DatasetError(f"{dataset}: no cases.json or manifests/*.json")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in manifests:
        payload = _read_json(path)
        entries = payload.get("cases") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise DatasetError(f"{path}: cases must be a list")
        for case in entries:
            if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
                raise DatasetError(f"{path}: each case requires case_id")
            if case["case_id"] in seen:
                raise DatasetError(f"duplicate case_id: {case['case_id']}")
            if case.get("expected_decision") not in ALLOWED_DECISIONS:
                raise DatasetError(f"{case['case_id']}: invalid expected_decision")
            if case.get("quality_state") not in {"complete", "degraded", "invalid"}:
                raise DatasetError(f"{case['case_id']}: invalid quality_state")
            if not isinstance(case.get("expected_reasons"), list):
                raise DatasetError(f"{case['case_id']}: expected_reasons must be a list")
            if not isinstance(case.get("tags"), list):
                raise DatasetError(f"{case['case_id']}: tags must be a list")
            if case.get("capability_gap") is True:
                if case.get("current_decision") not in ALLOWED_DECISIONS:
                    raise DatasetError(
                        f"{case['case_id']}: capability_gap requires current_decision"
                    )
                if not isinstance(case.get("current_reasons"), list):
                    raise DatasetError(
                        f"{case['case_id']}: capability_gap requires current_reasons"
                    )
            seen.add(case["case_id"])
            cases.append(case)
    return cases


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _verify_hashes(dataset: Path, case: dict[str, Any]) -> None:
    hashes = case.get("source_hashes") or {}
    if not isinstance(hashes, dict):
        raise DatasetError(f"{case['case_id']}: source_hashes must be an object")
    for relative, expected in hashes.items():
        path = _resolve(dataset, str(relative))
        if not path.is_file() or _sha256(path) != expected:
            raise DatasetError(f"{case['case_id']}: source hash mismatch: {relative}")


def _merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


def _materialize_handoff(dataset: Path, case: dict[str, Any], temp: Path) -> Path:
    if "handoff" in case:
        path = _resolve(dataset, str(case["handoff"]))
        return path
    if "base" not in case:
        raise DatasetError(f"{case['case_id']}: node case requires handoff or base")
    payload = _read_json(_resolve(dataset, str(case["base"])))
    if "patch" in case:
        payload = _merge_patch(payload, _read_json(_resolve(dataset, str(case["patch"]))))
    path = temp / case["case_id"] / "handoff.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _structural_reasons(messages: list[str]) -> list[str]:
    reasons: set[str] = set()
    for message in messages:
        for needle, reason in REASON_BY_MESSAGE:
            if needle in message:
                reasons.add(reason)
                break
    return sorted(reasons or {"gate_contract_failed"})


def _load_report(handoff: dict[str, Any], handoff_path: Path) -> str:
    report_path = handoff.get("report_path")
    if not isinstance(report_path, str) or not report_path.strip():
        return ""
    candidate = (handoff_path.parent / report_path).resolve()
    try:
        candidate.relative_to(handoff_path.parent.resolve())
    except ValueError as exc:
        raise DatasetError(f"report_path escapes artifact directory: {report_path}") from exc
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(f"unable to read report: {candidate}: {exc}") from exc


def _judge_request(
    case: dict[str, Any],
    handoff: dict[str, Any],
    evidence: dict[str, Any],
    report: str,
    parent_handoffs: list[dict[str, Any]],
) -> dict[str, Any]:
    judge = case.get("judge") or {}
    raw_summaries = []
    for relative in case.get("raw_summaries", []):
        raw_summaries.append(_read_json(_resolve(Path(case["_dataset"]), str(relative))))
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": f"gate-eval-{case['case_id']}",
        "case_id": case["case_id"],
        "evaluator_id": judge.get("evaluator_id", "gate-semantic-v1"),
        "rubric_id": judge.get("rubric_id", "company-profile-v1"),
        "input": {
            "subject": evidence.get("subject", {}),
            "raw_summaries": raw_summaries,
            "evidence": evidence,
            "handoff": minimize_handoff(handoff),
            "parent_handoffs": parent_handoffs,
            "report": report,
        },
    }


def _evaluate_case(
    dataset: Path,
    plugin_root: Path,
    case: dict[str, Any],
    temp: Path,
    profile: str,
    adapter: JudgeAdapter | None,
) -> dict[str, Any]:
    started = time.monotonic()
    _verify_hashes(dataset, case)
    kind = case.get("kind")
    skill_id = case.get("skill_id")
    run_id = case.get("run_id")
    if kind not in {"node", "final"} or not isinstance(skill_id, str) or not isinstance(run_id, str):
        raise DatasetError(f"{case['case_id']}: kind, skill_id and run_id are required")

    handoff_path: Path | None = None
    handoff: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    parent_handoffs: list[dict[str, Any]] = []
    if kind == "node":
        handoff_path = _materialize_handoff(dataset, case, temp)
        code, messages = check_node(
            plugin_root,
            skill_id,
            handoff_path,
            run_id,
            check_semantic=False,
            check_judge=False,
        )
        if code == 2:
            raise DatasetError(f"{case['case_id']}: node gate config error: {messages}")
        if code == 1:
            actual, reasons = "blocked", _structural_reasons(messages)
        else:
            handoff = load_json_object(handoff_path)
            if "evidence" in case:
                evidence_path = _resolve(dataset, str(case["evidence"]))
                evidence = load_json_object(evidence_path)
                semantic_root = _resolve(dataset, str(case.get("case_root", ".")))
                try:
                    _card, gate = load_skill_gate(plugin_root, skill_id)
                except CardGateError as exc:
                    raise DatasetError(str(exc)) from exc
                semantic_config = (gate or {}).get("semantic") or {}
                reasons = evaluate_semantic(
                    handoff,
                    evidence,
                    semantic_root,
                    case.get("evaluation_at"),
                    semantic_config.get("required_claims"),
                    semantic_config.get("deterministic_checks"),
                )
                actual = "blocked" if reasons else "passed"
            else:
                reasons, actual = [], "passed"
    else:
        run_dir = _resolve(dataset, str(case.get("run_dir", "")))
        code, messages = check_final(
            plugin_root, skill_id, run_dir, run_id, check_judge=False
        )
        if code == 2:
            raise DatasetError(f"{case['case_id']}: final gate config error: {messages}")
        actual = "blocked" if code else "passed"
        reasons = _structural_reasons(messages) if code else []
        report_handoff = run_dir / "artifacts" / run_id / skill_id / "handoff.json"
        if report_handoff.is_file():
            handoff_path = report_handoff
            handoff = load_json_object(report_handoff)
        artifact_root = run_dir / "artifacts" / run_id
        for parent_path in sorted(artifact_root.glob("*/handoff.json")):
            if parent_path == report_handoff:
                continue
            parent_handoffs.append(minimize_handoff(load_json_object(parent_path)))
        if actual == "passed" and "evidence" in case:
            evidence = load_json_object(_resolve(dataset, str(case["evidence"])))
            semantic_root = _resolve(dataset, str(case.get("case_root", ".")))
            try:
                _card, gate = load_skill_gate(plugin_root, skill_id)
            except CardGateError as exc:
                raise DatasetError(str(exc)) from exc
            semantic_config = (gate or {}).get("semantic") or {}
            reasons = evaluate_semantic(
                handoff,
                evidence,
                semantic_root,
                case.get("evaluation_at"),
                semantic_config.get("required_claims"),
                semantic_config.get("deterministic_checks"),
            )
            actual = "blocked" if reasons else "passed"
    judge_result = None
    if actual == "passed" and case.get("judge"):
        if profile == "ci":
            actual = "not_run"
        else:
            if adapter is None:
                raise DatasetError("semantic profile requires --judge-adapter")
            report = _load_report(handoff, handoff_path) if handoff_path else ""
            case_for_request = dict(case, _dataset=str(dataset))
            judge_result = adapter.evaluate(
                _judge_request(
                    case_for_request, handoff, evidence, report, parent_handoffs
                )
            )
            actual = judge_result["decision"]
            reasons = sorted({item["reason"] for item in judge_result["findings"]})
            if judge_result["confidence"] < float((case.get("judge") or {}).get("min_confidence", 0.6)):
                actual = "needs_review"
                reasons = sorted({*reasons, "judge_low_confidence"})

    expected = case["expected_decision"]
    expected_reasons = set(case.get("expected_reasons") or [])
    matches = actual == expected and expected_reasons.issubset(reasons)
    if actual == "not_run" and case.get("judge") and profile == "ci":
        matches = True
    current_reasons = set(case.get("current_reasons") or [])
    known_gap = (
        case.get("capability_gap") is True
        and actual == case.get("current_decision")
        and current_reasons == set(reasons)
    )
    unexpected = not matches and not known_gap
    result: dict[str, Any] = {
        "case_id": case["case_id"],
        "kind": kind,
        "tags": case.get("tags", []),
        "expected_decision": expected,
        "actual_decision": actual,
        "reasons": reasons,
        "capability_gap": bool(case.get("capability_gap", False)),
        "known_gap": known_gap and not matches,
        "unexpected": unexpected,
        "checker_version": CHECKER_VERSION,
        "input_hash": _case_input_hash(dataset, case),
        "judge": judge_result,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    }
    if actual == "needs_review":
        subject_name = ""
        subject = evidence.get("subject") if isinstance(evidence.get("subject"), dict) else None
        if subject is None and isinstance(handoff.get("subject"), dict):
            subject = handoff["subject"]
        if isinstance(subject, dict):
            subject_name = str(subject.get("name") or "")
        result["review_context"] = {
            "skill_id": skill_id,
            "subject_name": subject_name or "（未知主体）",
            "run_id": run_id,
            "handoff": minimize_handoff(handoff) if handoff else {},
            "evidence": {
                key: evidence[key]
                for key in ("run_id", "skill_id", "subject", "evidence", "claims", "conflicts")
                if key in evidence
            },
            "judge": judge_result,
        }
    return result


def _case_input_hash(dataset: Path, case: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    for field in ("handoff", "base", "patch", "evidence"):
        value = case.get(field)
        if isinstance(value, str):
            path = _resolve(dataset, value)
            if path.is_file():
                digest.update(path.read_bytes())
    run_dir = case.get("run_dir")
    if isinstance(run_dir, str):
        root = _resolve(dataset, run_dir)
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _compare_baseline(
    dataset: Path, baseline: Path | None, cases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if baseline is None:
        return []
    baseline_cases = {case["case_id"]: case for case in _discover_cases(baseline.resolve())}
    drift: list[dict[str, Any]] = []
    for case in cases:
        previous = baseline_cases.pop(case["case_id"], None)
        if previous is None:
            drift.append({"case_id": case["case_id"], "change": "added"})
            continue
        changes = {}
        for field in (
            "expected_decision",
            "expected_reasons",
            "source_hashes",
            "capability_gap",
            "current_decision",
            "current_reasons",
        ):
            if case.get(field) != previous.get(field):
                changes[field] = {"baseline": previous.get(field), "candidate": case.get(field)}
        if changes:
            drift.append({"case_id": case["case_id"], "change": "changed", "fields": changes})
    drift.extend({"case_id": case_id, "change": "removed"} for case_id in sorted(baseline_cases))
    return drift


def _write_reports(
    output: Path,
    profile: str,
    results: list[dict[str, Any]],
    baseline_drift: list[dict[str, Any]],
) -> dict[str, int]:
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "total": len(results),
        "passed": sum(item["actual_decision"] == "passed" for item in results),
        "blocked": sum(item["actual_decision"] == "blocked" for item in results),
        "needs_review": sum(item["actual_decision"] == "needs_review" for item in results),
        "not_run": sum(item["actual_decision"] == "not_run" for item in results),
        "capability_gaps": sum(item["known_gap"] for item in results),
        "unexpected_regressions": sum(item["unexpected"] for item in results),
    }
    false_accepts = [item for item in results if item["expected_decision"] == "blocked" and item["actual_decision"] == "passed"]
    false_rejects = [item for item in results if item["expected_decision"] == "passed" and item["actual_decision"] == "blocked"]
    needs_review = [item for item in results if item["actual_decision"] == "needs_review"]
    drift = {
        "evaluation": [item for item in results if item["unexpected"] or item["known_gap"]],
        "baseline": baseline_drift,
    }
    review_links: list[tuple[str, str]] = []
    if needs_review:
        from gate_review import write_eval_review_pack

        reviews_root = output / "reviews"
        for item in needs_review:
            try:
                paths = write_eval_review_pack(reviews_root, item)
            except Exception as exc:  # noqa: BLE001 — keep evaluation report usable
                print(f"warning: review pack for {item.get('case_id')}: {exc}", file=sys.stderr)
                continue
            if paths is None:
                continue
            case_id = str(item["case_id"])
            review_links.append((case_id, f"reviews/{case_id}/review.md"))

    def _public_case(item: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if key != "review_context"}

    public_results = [_public_case(item) for item in results]
    payload = {
        "profile": profile,
        "checker_version": CHECKER_VERSION,
        "summary": summary,
        "baseline_drift": baseline_drift,
        "cases": public_results,
    }
    (output / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for name, value in (
        ("false-accepts.json", false_accepts),
        ("false-rejects.json", false_rejects),
        ("needs-review.json", [_public_case(item) for item in needs_review]),
        ("drift.json", drift),
    ):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Gate dataset evaluation",
        "",
        f"Profile: `{profile}`",
        "",
        "| total | passed | blocked | needs review | not run | regressions |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| {summary['total']} | {summary['passed']} | {summary['blocked']} | {summary['needs_review']} | {summary['not_run']} | {summary['unexpected_regressions']} |",
        "",
    ]
    if review_links:
        lines.extend(["## Needs review", "", "| case_id | review |", "| --- | --- |"])
        for case_id, relative in review_links:
            lines.append(f"| {case_id} | [{relative}]({relative}) |")
        lines.append("")
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--profile", required=True, choices=("ci", "semantic"))
    parser.add_argument("--judge-adapter", type=Path)
    parser.add_argument("--judge-timeout-seconds", type=int, default=120)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plugin-root", type=Path, default=SCRIPTS_DIR.parent)
    args = parser.parse_args(argv)
    if not 1 <= args.judge_timeout_seconds <= 600:
        parser.error("--judge-timeout-seconds must be between 1 and 600")
    plugin_root = args.plugin_root.resolve()
    if args.judge_adapter is None and args.profile == "semantic":
        resolved = resolve_judge_path(plugin_root)
        if resolved is None:
            parser.error(
                "semantic profile requires --judge-adapter or "
                f"{default_judge_script(plugin_root)}"
            )
        args.judge_adapter = resolved
    if args.judge_adapter:
        if not args.judge_adapter.is_absolute():
            parser.error("--judge-adapter must be an absolute path")
        if not args.judge_adapter.is_file():
            parser.error("--judge-adapter must be an existing file")
        # .py scripts may not be chmod +x; launch via python3.
        if args.judge_adapter.suffix != ".py" and not os.access(args.judge_adapter, os.X_OK):
            parser.error("--judge-adapter must be an executable file")
    return args


def main(argv: list[str]) -> int:
    try:
        args = _parse_args(argv)
        dataset = args.dataset.resolve()
        cases = _discover_cases(dataset)
        output = args.output.resolve() if args.output else Path(".scratch/gate-eval") / time.strftime("%Y%m%d-%H%M%S")
        adapter = (
            JudgeAdapter(
                judge_launch_command(args.judge_adapter),
                args.judge_timeout_seconds,
            )
            if args.judge_adapter
            else None
        )
        try:
            with tempfile.TemporaryDirectory(prefix="cws-gate-eval-") as temp:
                results = [
                    _evaluate_case(dataset, args.plugin_root.resolve(), case, Path(temp), args.profile, adapter)
                    for case in cases
                ]
        finally:
            if adapter:
                adapter.close()
        baseline_drift = _compare_baseline(dataset, args.baseline, cases)
        summary = _write_reports(output, args.profile, results, baseline_drift)
        print(f"Gate evaluation: {summary['total']} cases, {summary['unexpected_regressions']} unexpected regressions")
        print(f"Report: {output / 'report.md'}")
        return 1 if summary["unexpected_regressions"] else 0
    except (DatasetError, JudgeAdapterError, SemanticGateError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
