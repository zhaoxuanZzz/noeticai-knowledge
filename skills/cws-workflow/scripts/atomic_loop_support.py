"""Shared state, hashing, and finding helpers for the atomic loop."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Any

from delegate_runner import DelegateRunnerError, load_state, state_path, write_state


TERMINAL_STATUSES = {"passed", "needs_input", "needs_review", "exhausted", "cancelled"}
GATE_FILES = (
    "scripts/check_artifact_gate.py",
    "scripts/semantic_gate.py",
    "scripts/gate_judge_adapter.py",
)


def locked(function):
    @wraps(function)
    def wrapped(company_kb: Path, run_id: str, *args: Any, **kwargs: Any):
        lock_path = state_path(company_kb, run_id).with_name(".workflow-state.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return function(company_kb, run_id, *args, **kwargs)

    return wrapped


def loop_node(
    state: dict[str, Any], node_id: str | None = None
) -> tuple[str, dict[str, Any]]:
    if node_id is None:
        if state.get("mode") != "atomic-loop" or len(state["nodes"]) != 1:
            raise DelegateRunnerError("run is not a standalone atomic loop")
        node_id = next(iter(state["nodes"]))
    node = state["nodes"].get(node_id)
    if node is None:
        raise DelegateRunnerError(f"unknown node: {node_id}")
    if not node.get("loop_enabled"):
        raise DelegateRunnerError(f"loop is not enabled for node: {node_id}")
    return node_id, node


def check_elapsed(state_file: Path, state: dict[str, Any], node: dict[str, Any]) -> None:
    try:
        manifest = load_manifest(state_file.parent)
    except ValueError as exc:
        raise DelegateRunnerError(str(exc)) from exc
    created = manifest.get("created_epoch")
    limit = manifest.get("max_elapsed_seconds")
    if isinstance(created, (int, float)) and isinstance(limit, int):
        if time.time() >= created + limit:
            node["status"] = "exhausted"
            state["status"] = "exhausted"
            write_state(state_file, state)
            raise DelegateRunnerError("loop max elapsed time exceeded")


def promote(run_dir: Path, skill_id: str, attempt_dir: Path) -> Path:
    target = run_dir / skill_id
    if target.exists():
        raise DelegateRunnerError(f"formal artifact already exists: {target}")
    temporary = run_dir / f".{skill_id}.promote-{uuid.uuid4().hex}"
    shutil.copytree(
        attempt_dir,
        temporary,
        ignore=shutil.ignore_patterns("maker-context.json"),
    )
    temporary.replace(target)
    return target


@locked
def fail_attempt(
    company_kb: Path,
    run_id: str,
    node_id: str,
    lease_id: str,
    reason: str,
) -> dict[str, Any]:
    if not reason.strip():
        raise DelegateRunnerError("failure reason is required")
    state_file, state = load_state(company_kb, run_id)
    _node_id, node = loop_node(state, node_id)
    if node["status"] != "running":
        raise DelegateRunnerError(f"loop cannot fail from status {node['status']}")
    if node.get("lease_id") != lease_id:
        raise DelegateRunnerError("lease-id does not match the active attempt")
    status = "exhausted" if node["attempt"] >= node["max_attempts"] else "retryable"
    node["status"] = status
    node["failure_reason"] = reason.strip()
    state["status"] = status if status == "exhausted" else "running"
    write_state(state_file, state)
    return {
        "run_id": run_id,
        "node": node_id,
        "attempt": node["attempt"],
        "status": status,
        "reason": reason.strip(),
    }


def finish_promotion(
    state_file: Path,
    state: dict[str, Any],
    node: dict[str, Any],
    attempt_dir: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    target = state_file.parent / node["skill"]
    candidate_hash = result["candidate_sha256"]
    if target.exists() and directory_sha256(target) != candidate_hash:
        finding = {
            "reason": "formal_artifact_exists",
            "severity": "fatal",
            "repairable": False,
            "action": "human_review",
            "artifact_path": "artifacts",
            "field": "",
            "evidence_refs": [],
            "detail": str(target),
        }
        result.update(
            {
                "decision": "blocked",
                "status": "needs_review",
                "next_action": "human_review",
                "exit_code": 1,
                "errors": ["formal artifact already exists"],
                "findings": [finding],
                "fingerprints": [finding_fingerprint(finding)],
            }
        )
        write_state(attempt_dir / "gate-result.json", result)
        node["status"] = "needs_review"
        node["findings"] = [finding]
        state["status"] = "needs_review"
        write_state(state_file, state)
        return result

    node["status"] = "promoting"
    node["candidate_sha256"] = candidate_hash
    state["status"] = "promoting"
    write_state(state_file, state)
    formal_dir = target if target.exists() else promote(state_file.parent, node["skill"], attempt_dir)
    node["status"] = "passed"
    node["gate_result_path"] = str(formal_dir / "gate-result.json")
    state["status"] = (
        "passed"
        if all(item["status"] == "passed" for item in state["nodes"].values())
        else "running"
    )
    write_state(state_file, state)
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        if path.name in {"gate-result.json", "maker-context.json"}:
            continue
        digest.update(str(path.relative_to(directory)).encode("utf-8"))
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def utc_at(epoch: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def load_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read run manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("run manifest must be a JSON object")
    return manifest


def gate_revision(root: Path, rubric_id: str | None = None) -> dict[str, Any]:
    return {
        "rubric_id": rubric_id,
        "judge_protocol": "cws-gate-judge/v1",
        "files": {name: sha256_file(root / name) for name in GATE_FILES},
    }


def revision_errors(run_dir: Path, root: Path, skill_id: str) -> list[str]:
    try:
        manifest = load_manifest(run_dir)
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    frozen_input = run_dir / "frozen" / "input.json"
    if not frozen_input.is_file() or sha256_file(frozen_input) != manifest.get("input_sha256"):
        errors.append("frozen/input.json")
    revisions = (manifest.get("skill_revisions") or {}).get(skill_id)
    if revisions is None:
        revisions = manifest.get("skill_revision") or {}
    for name, expected in revisions.items():
        frozen = run_dir / "frozen" / "skills" / skill_id / name
        live = root / "skills" / skill_id / name
        if not frozen.is_file() or sha256_file(frozen) != expected:
            errors.append(f"frozen/skills/{skill_id}/{name}")
        if not live.is_file() or sha256_file(live) != expected:
            errors.append(f"live/skills/{skill_id}/{name}")
    for name, expected in ((manifest.get("gate_revision") or {}).get("files") or {}).items():
        path = root / name
        if not path.is_file() or sha256_file(path) != expected:
            errors.append(f"live/{name}")
    return errors


def normalize_findings(outcome: Any) -> list[dict[str, Any]]:
    judge_findings = (outcome.judge or {}).get("findings") or []
    if judge_findings:
        return [dict(item) for item in judge_findings if isinstance(item, dict)]
    fatal = {"run_id_mismatch", "subject_identity_mismatch", "cross_subject_artifact"}
    findings: list[dict[str, Any]] = []
    for message in outcome.messages:
        reason, artifact_path = _reason_from_message(message)
        repairable = reason not in fatal
        action = "human_review" if not repairable else "revise"
        if reason == "upstream_contract_gap":
            repairable = False
            action = "upstream_contract_gap"
        findings.append(
            {
                "reason": reason,
                "severity": "fatal" if not repairable else "major",
                "repairable": repairable,
                "action": action,
                "artifact_path": artifact_path,
                "field": artifact_path.rsplit(".", 1)[-1] if "." in artifact_path else "",
                "evidence_refs": [],
                "detail": message,
            }
        )
    return findings


def _reason_from_message(message: str) -> tuple[str, str]:
    if message.startswith(("missing parent handoff for ", "cannot resolve parent artifact ")):
        return "upstream_contract_gap", "artifacts"
    if message.startswith("missing required_output in artifacts: "):
        field = message.rsplit(":", 1)[1].strip()
        return "required_output_missing", f"artifacts.{field}"
    if message.startswith("artifact_checks: missing artifacts."):
        field = message.rsplit(".", 1)[1].strip()
        return "required_output_missing", f"artifacts.{field}"
    prefix = message.split(":", 1)[0].strip()
    if prefix == "run_id":
        return "run_id_mismatch", "run_id"
    if re_match := re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        return re_match.group(0), "artifacts"
    return "gate_validation_failed", "artifacts"


def finding_fingerprint(finding: dict[str, Any]) -> str:
    payload = {
        "reason": finding.get("reason", ""),
        "artifact_path": finding.get("artifact_path", ""),
        "field": finding.get("field", ""),
        "evidence_refs": sorted(finding.get("evidence_refs") or []),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
