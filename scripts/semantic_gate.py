#!/usr/bin/env python3
"""Deterministic checks for claim-level gate evidence."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


CHECKER_VERSION = "semantic-gate/v1"


class SemanticGateError(Exception):
    """Raised when semantic input is malformed rather than a business failure."""


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SemanticGateError(f"{path}: unable to read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SemanticGateError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SemanticGateError(f"{path}: expected JSON object")
    return value


def json_path_get(value: Any, path: str) -> Any:
    current = value
    for part in re.sub(r"\[(\d+)\]", r".\1", path).split("."):
        if not part:
            raise KeyError(path)
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


def _json_pointer_get(value: Any, pointer: str) -> Any:
    current = value
    if pointer in {"", "/"}:
        return current
    for raw in pointer.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(pointer)
    return current


def _resolve_source(root: Path, source_ref: str) -> tuple[Path, str]:
    relative, marker, pointer = source_ref.partition("#")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise SemanticGateError(f"source_ref escapes case root: {source_ref}") from exc
    if not candidate.is_file():
        raise SemanticGateError(f"source_ref file does not exist: {source_ref}")
    return candidate, pointer if marker else ""


def _normalized(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in sorted(value.items())}
    return value


def _gap_discloses(handoff: dict[str, Any], field: str) -> bool:
    gaps = handoff.get("evidence_gaps")
    if not isinstance(gaps, list):
        gaps = []
    artifacts = handoff.get("artifacts")
    if isinstance(artifacts, dict) and isinstance(artifacts.get("evidence_gaps"), list):
        gaps = [*gaps, *artifacts["evidence_gaps"]]
    needle = field.casefold()
    return any(needle in json.dumps(item, ensure_ascii=False).casefold() for item in gaps)


def evaluate_semantic(
    handoff: dict[str, Any],
    evidence_doc: dict[str, Any],
    case_root: Path,
    evaluation_at: str | None = None,
    required_claims: list[str] | None = None,
    enabled_checks: list[str] | None = None,
) -> list[str]:
    """Return stable reason codes. Empty means deterministic checks passed."""
    reasons: set[str] = set()
    checks = set(
        enabled_checks
        if enabled_checks is not None
        else (
            "claim_evidence",
            "subject_identity",
            "freshness_disclosure",
            "source_conflict_disclosure",
            "negative_claim_coverage",
        )
    )
    evidence_items = evidence_doc.get("evidence")
    claims = evidence_doc.get("claims")
    if not isinstance(evidence_items, list) or not isinstance(claims, list):
        raise SemanticGateError("evidence.json requires evidence and claims lists")

    if evidence_doc.get("run_id") != handoff.get("run_id"):
        reasons.add("run_id_mismatch")

    expected_subject = evidence_doc.get("subject")
    expected_code = expected_subject.get("unified_social_credit_code") if isinstance(expected_subject, dict) else None
    expected_name = expected_subject.get("name") if isinstance(expected_subject, dict) else None
    handoff_subject = handoff.get("subject")
    if isinstance(handoff_subject, dict):
        actual_code = handoff_subject.get("unified_social_credit_code")
        actual_name = handoff_subject.get("name")
    else:
        actual_code = handoff.get("unified_social_credit_code")
        actual_name = handoff_subject
    if "subject_identity" in checks:
        if expected_code and actual_code and expected_code != actual_code:
            reasons.add("subject_identity_mismatch")
        if (
            isinstance(expected_name, str)
            and isinstance(actual_name, str)
            and _normalized(expected_name).casefold() != _normalized(actual_name).casefold()
        ):
            reasons.add("subject_identity_mismatch")

    by_id: dict[str, dict[str, Any]] = {}
    source_values: dict[str, Any] = {}
    for item in evidence_items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise SemanticGateError("each evidence item requires a string id")
        by_id[item["id"]] = item
        if (
            "subject_identity" in checks
            and expected_code
            and item.get("subject_id")
            and item["subject_id"] != expected_code
        ):
            reasons.add("evidence_subject_mismatch")
        source_ref = item.get("source_ref")
        if "claim_evidence" not in checks:
            pass
        elif not isinstance(source_ref, str) or not source_ref:
            reasons.add("evidence_path_invalid")
            continue
        else:
            try:
                source_file, pointer = _resolve_source(case_root, source_ref)
                source_payload = json.loads(source_file.read_text(encoding="utf-8"))
                source_values[item["id"]] = _json_pointer_get(source_payload, pointer)
            except (SemanticGateError, OSError, json.JSONDecodeError, KeyError):
                reasons.add("evidence_path_invalid")
                continue
            if _normalized(source_values[item["id"]]) != _normalized(item.get("value")):
                reasons.add("evidence_value_mismatch")

        observed_at = item.get("observed_at")
        max_age_days = item.get("max_age_days")
        if (
            "freshness_disclosure" in checks
            and observed_at
            and isinstance(max_age_days, int)
            and evaluation_at
        ):
            try:
                age = dt.date.fromisoformat(evaluation_at) - dt.date.fromisoformat(observed_at)
            except ValueError as exc:
                raise SemanticGateError(f"invalid evidence date: {exc}") from exc
            if age.days > max_age_days and not _gap_discloses(handoff, str(item.get("field", ""))):
                reasons.add("stale_data_not_disclosed")

    claimed_paths: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise SemanticGateError("each claim must be an object")
        path = claim.get("artifact_path")
        refs = claim.get("evidence_refs")
        if not isinstance(path, str) or not isinstance(refs, list):
            raise SemanticGateError("each claim requires artifact_path and evidence_refs")
        if not refs:
            if "claim_evidence" in checks:
                reasons.add("evidence_missing")
            continue
        claimed_paths.add(path)
        missing_refs = [ref for ref in refs if ref not in by_id]
        if missing_refs:
            if "claim_evidence" in checks or "negative_claim_coverage" in checks:
                reasons.add("evidence_missing")
            continue
        if "claim_evidence" in checks:
            try:
                artifact_value = json_path_get(handoff, path)
            except KeyError:
                reasons.add("unsupported_claim")
                continue
            if _normalized(artifact_value) != _normalized(claim.get("value")):
                reasons.add("evidence_value_mismatch")
            if not any(_normalized(by_id[ref].get("value")) == _normalized(claim.get("value")) for ref in refs):
                reasons.add("unsupported_claim")
        if "negative_claim_coverage" in checks and claim.get("negative_claim") is True:
            complete = any(
                isinstance(by_id[ref].get("coverage"), dict)
                and by_id[ref]["coverage"].get("complete") is True
                and by_id[ref]["coverage"].get("query_succeeded") is True
                for ref in refs
            )
            if not complete:
                reasons.add("negative_claim_without_search_coverage")

    conflicts = evidence_doc.get("conflicts") or []
    if not isinstance(conflicts, list):
        raise SemanticGateError("evidence.conflicts must be a list")
    if "source_conflict_disclosure" in checks:
        disclosed_fields: set[str] = set()
        for conflict in conflicts:
            if isinstance(conflict, dict) and isinstance(conflict.get("field"), str):
                if conflict.get("disclosed") is True:
                    disclosed_fields.add(conflict["field"])
                else:
                    reasons.add("source_conflict_not_disclosed")
        values_by_field: dict[str, set[str]] = {}
        for item in evidence_items:
            field = item.get("field") if isinstance(item, dict) else None
            if isinstance(field, str):
                values_by_field.setdefault(field, set()).add(
                    json.dumps(_normalized(item.get("value")), ensure_ascii=False, sort_keys=True)
                )
        for field, values in values_by_field.items():
            if (
                len(values) > 1
                and field not in disclosed_fields
                and not _gap_discloses(handoff, field)
            ):
                reasons.add("source_conflict_not_disclosed")

    if "claim_evidence" in checks:
        for required_path in required_claims or []:
            output_name = required_path.split(".")[1]
            if required_path not in claimed_paths and not _gap_discloses(handoff, output_name):
                reasons.add("evidence_missing")

    return sorted(reasons)


def evaluate_final_context(
    run_dir: Path,
    run_id: str,
    candidate_handoff: Path | None = None,
) -> list[str]:
    """Check same-run handoffs for subject isolation and blocked parent results."""
    reasons: set[str] = set()
    artifact_root = run_dir / "artifacts" / run_id
    identities: list[tuple[str | None, str | None]] = []
    if not artifact_root.is_dir():
        return []
    handoff_paths = list(artifact_root.glob("*/handoff.json"))
    if candidate_handoff is not None:
        handoff_paths.append(candidate_handoff)
    for handoff_path in handoff_paths:
        try:
            handoff = load_json_object(handoff_path)
        except SemanticGateError:
            continue
        subject = handoff.get("subject")
        if isinstance(subject, str) and subject.strip():
            identities.append((None, " ".join(subject.split()).casefold()))
        elif isinstance(subject, dict):
            code = subject.get("unified_social_credit_code")
            name = subject.get("name")
            normalized_code = (
                "".join(code.split()).casefold()
                if isinstance(code, str) and code.strip()
                else None
            )
            normalized_name = (
                " ".join(name.split()).casefold()
                if isinstance(name, str) and name.strip()
                else None
            )
            if normalized_code or normalized_name:
                identities.append((normalized_code, normalized_name))
        if handoff_path == candidate_handoff:
            continue
        gate_result = handoff_path.parent / "gate-result.json"
        if gate_result.is_file():
            try:
                result = load_json_object(gate_result)
                status = result.get("status") or result.get("decision")
            except SemanticGateError:
                status = "blocked"
            if status not in {"passed", "waived"}:
                reasons.add("parent_gate_blocked")
    codes = {code for code, _name in identities if code}
    coded_names = {name for code, name in identities if code and name}
    uncoded_names = {name for code, name in identities if not code and name}
    subject_mismatch = len(codes) > 1
    if not subject_mismatch and codes:
        if coded_names:
            subject_mismatch = any(name not in coded_names for name in uncoded_names)
        else:
            subject_mismatch = len(uncoded_names) > 1
    elif not subject_mismatch:
        subject_mismatch = len(uncoded_names) > 1
    if subject_mismatch:
        reasons.add("cross_subject_artifact")
    return sorted(reasons)
