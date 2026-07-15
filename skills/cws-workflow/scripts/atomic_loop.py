"""Standalone atomic skill loop built on delegate run state."""

from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from delegate_runner import DelegateRunnerError, initialize, load_state, state_path, write_state


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from card_gate import CardGateError, load_skill_gate  # noqa: E402
from check_artifact_gate import check_final, check_node  # noqa: E402
from atomic_loop_support import (  # noqa: E402
    TERMINAL_STATUSES,
    check_elapsed,
    directory_sha256,
    finding_fingerprint,
    finish_promotion,
    gate_revision,
    locked,
    load_manifest,
    loop_node,
    normalize_findings,
    revision_errors,
    sha256_file,
    utc_at,
)


@locked
def initialize_loop(
    company_kb: Path,
    run_id: str,
    skill_id: str,
    company: str,
    input_path: Path,
    *,
    max_attempts: int = 3,
    lease_seconds: int = 3600,
    max_elapsed_seconds: int = 86400,
) -> dict[str, Any]:
    if state_path(company_kb, run_id).exists():
        raise DelegateRunnerError(f"delegate run already exists: {state_path(company_kb, run_id)}")
    try:
        _card, gate = load_skill_gate(ROOT, skill_id)
    except CardGateError as exc:
        raise DelegateRunnerError(str(exc)) from exc
    if gate is None:
        raise DelegateRunnerError(f"skill has no gate: {skill_id}")
    if not 1 <= max_attempts <= 10:
        raise DelegateRunnerError("max-attempts must be between 1 and 10")
    if not 1 <= lease_seconds <= 86400:
        raise DelegateRunnerError("lease-seconds must be between 1 and 86400")
    if not lease_seconds <= max_elapsed_seconds <= 604800:
        raise DelegateRunnerError(
            "max-elapsed-seconds must be between lease-seconds and 604800"
        )
    try:
        input_data = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DelegateRunnerError(f"cannot read loop input {input_path}: {exc}") from exc
    if not isinstance(input_data, dict):
        raise DelegateRunnerError("loop input must be a JSON object")
    input_company = input_data.get("company_name")
    if isinstance(input_company, str) and " ".join(input_company.split()) != " ".join(company.split()):
        raise DelegateRunnerError("loop input company_name does not match --company")

    skill_dir = ROOT / "skills" / skill_id
    source_files = [skill_dir / "SKILL.md", skill_dir / "card.yaml"]
    if any(not path.is_file() for path in source_files):
        raise DelegateRunnerError(f"skill files are incomplete: {skill_dir}")

    run_dir = company_kb / "artifacts" / run_id
    frozen_dir = run_dir / "frozen"
    frozen_skill = frozen_dir / "skills" / skill_id
    frozen_skill.mkdir(parents=True, exist_ok=True)
    frozen_input = frozen_dir / "input.json"
    frozen_input.write_text(
        json.dumps(input_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for source in source_files:
        shutil.copy2(source, frozen_skill / source.name)

    formal_dir = run_dir / skill_id
    initialize(
        company_kb,
        run_id,
        skill_id,
        company,
        [
            {
                "id": "atomic",
                "skill": skill_id,
                "parents": [],
                "handoff_path": str(formal_dir / "handoff.json"),
                "node_gate": {"mode": "node", "run_id": run_id},
                "final_gate": (
                    {"mode": "final", "skill": skill_id, "run_id": run_id}
                    if gate.get("final")
                    else None
                ),
                "loop_enabled": True,
                "max_attempts": max_attempts,
                "findings": [],
                "fingerprints": [],
            }
        ],
        frozen_kb=True,
    )
    state_file, state = load_state(company_kb, run_id)
    state["mode"] = "atomic-loop"
    state["status"] = "pending"
    write_state(state_file, state)

    created_epoch = time.time()
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "skill_id": skill_id,
        "subject": {"name": company},
        "created_at": utc_at(created_epoch),
        "created_epoch": created_epoch,
        "max_attempts": max_attempts,
        "lease_seconds": lease_seconds,
        "max_elapsed_seconds": max_elapsed_seconds,
        "input_sha256": sha256_file(frozen_input),
        "skill_revision": {
            path.name: sha256_file(frozen_skill / path.name) for path in source_files
        },
        "gate_revision": gate_revision(
            ROOT, ((gate.get("semantic") or {}).get("judge") or {}).get("rubric")
        ),
    }
    write_state(run_dir / "run-manifest.json", manifest)
    return {
        "run_id": run_id,
        "skill": skill_id,
        "status": "pending",
        "state_path": str(state_file),
        "manifest_path": str(run_dir / "run-manifest.json"),
    }


@locked
def enable_delegate_loop(
    company_kb: Path,
    run_id: str,
    *,
    max_attempts: int = 3,
    lease_seconds: int = 3600,
    max_elapsed_seconds: int = 86400,
) -> dict[str, Any]:
    state_file, state = load_state(company_kb, run_id)
    if not 1 <= max_attempts <= 10:
        raise DelegateRunnerError("max-attempts must be between 1 and 10")
    if not 1 <= lease_seconds <= 86400:
        raise DelegateRunnerError("lease-seconds must be between 1 and 86400")
    if not lease_seconds <= max_elapsed_seconds <= 604800:
        raise DelegateRunnerError(
            "max-elapsed-seconds must be between lease-seconds and 604800"
        )
    run_dir = state_file.parent
    frozen_dir = run_dir / "frozen"
    frozen_input = frozen_dir / "input.json"
    frozen_input.parent.mkdir(parents=True, exist_ok=True)
    frozen_input.write_text(
        json.dumps({"company_name": state["company"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    revisions: dict[str, dict[str, str]] = {}
    for node in state["nodes"].values():
        skill_id = node["skill"]
        skill_dir = ROOT / "skills" / skill_id
        frozen_skill = frozen_dir / "skills" / skill_id
        frozen_skill.mkdir(parents=True, exist_ok=True)
        revisions.setdefault(skill_id, {})
        for name in ("SKILL.md", "card.yaml"):
            source = skill_dir / name
            if not source.is_file():
                raise DelegateRunnerError(f"skill file is missing: {source}")
            target = frozen_skill / name
            shutil.copy2(source, target)
            revisions[skill_id][name] = sha256_file(target)
        node.update(
            {
                "loop_enabled": True,
                "max_attempts": max_attempts,
                "findings": [],
                "fingerprints": [],
            }
        )
    state["mode"] = "delegate-loop"
    write_state(state_file, state)
    created_epoch = time.time()
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "subject": {"name": state["company"]},
        "created_at": utc_at(created_epoch),
        "created_epoch": created_epoch,
        "max_attempts": max_attempts,
        "lease_seconds": lease_seconds,
        "max_elapsed_seconds": max_elapsed_seconds,
        "input_sha256": sha256_file(frozen_input),
        "skill_revisions": revisions,
        "gate_revision": gate_revision(ROOT),
    }
    write_state(run_dir / "run-manifest.json", manifest)
    return {"run_id": run_id, "status": state["status"], "ready": ["task1"]}


@locked
def bind_kanban_task(
    company_kb: Path,
    run_id: str,
    node_id: str,
    task_id: str,
) -> None:
    state_file, state = load_state(company_kb, run_id)
    node = state["nodes"].get(node_id)
    if node is None:
        raise DelegateRunnerError(f"unknown node: {node_id}")
    existing = node.get("kanban_task_id")
    if existing and existing != task_id:
        raise DelegateRunnerError(
            f"node already bound to Kanban task {existing}: {node_id}"
        )
    node["kanban_task_id"] = task_id
    write_state(state_file, state)


@locked
def next_attempt(
    company_kb: Path,
    run_id: str,
    node_id: str | None = None,
) -> dict[str, Any]:
    state_file, state = load_state(company_kb, run_id)
    node_id, node = loop_node(state, node_id)
    if node["status"] in TERMINAL_STATUSES:
        raise DelegateRunnerError(f"loop cannot start from status {node['status']}")
    check_elapsed(state_file, state, node)
    if node["status"] == "running":
        expires = node.get("lease_expires_epoch")
        if not isinstance(expires, (int, float)) or time.time() < expires:
            raise DelegateRunnerError(f"loop cannot start from status {node['status']}")
        node["status"] = "retryable"
        node["failure_reason"] = "lease expired"
    if any(state["nodes"][parent]["status"] != "passed" for parent in node["parents"]):
        raise DelegateRunnerError(f"node is not ready: {node_id}")
    changed = revision_errors(state_file.parent, ROOT, node["skill"])
    if changed:
        node["status"] = "needs_review"
        node["failure_reason"] = f"frozen revision changed: {', '.join(changed)}"
        state["status"] = "needs_review"
        write_state(state_file, state)
        raise DelegateRunnerError(node["failure_reason"])
    if node["status"] not in {"pending", "retryable"}:
        raise DelegateRunnerError(f"loop cannot start from status {node['status']}")
    attempt = node["attempt"] + 1
    if attempt > node["max_attempts"]:
        node["status"] = "exhausted"
        state["status"] = "exhausted"
        write_state(state_file, state)
        raise DelegateRunnerError("loop attempts exhausted")

    run_dir = state_file.parent
    attempt_dir = run_dir / "attempts" / node["skill"] / str(attempt)
    attempt_dir.mkdir(parents=True, exist_ok=False)
    lease_id = f"lease-{uuid.uuid4().hex}"
    try:
        lease_seconds = load_manifest(run_dir)["lease_seconds"]
    except (KeyError, ValueError) as exc:
        raise DelegateRunnerError(f"invalid lease configuration: {exc}") from exc
    lease_expires_epoch = time.time() + lease_seconds
    context = {
        "run_id": run_id,
        "skill_id": node["skill"],
        "attempt": attempt,
        "input_path": str(run_dir / "frozen" / "input.json"),
        "skill_path": str(run_dir / "frozen" / "skills" / node["skill"] / "SKILL.md"),
        "card_path": str(run_dir / "frozen" / "skills" / node["skill"] / "card.yaml"),
        "output_dir": str(attempt_dir),
        "previous_attempt_dir": (
            str(run_dir / "attempts" / node["skill"] / str(attempt - 1))
            if attempt > 1
            else None
        ),
        "parent_handoff_paths": [
            state["nodes"][parent]["handoff_path"] for parent in node["parents"]
        ],
        "findings": node.get("findings", []),
        "required_files": ["handoff.json", "evidence.json"],
        "forbidden_side_effects": ["external_commit"],
    }
    write_state(attempt_dir / "maker-context.json", context)
    node.update(
        {
            "status": "running",
            "attempt": attempt,
            "attempt_dir": str(attempt_dir),
            "lease_id": lease_id,
            "lease_started_at": utc_at(),
            "lease_expires_at": utc_at(lease_expires_epoch),
            "lease_expires_epoch": lease_expires_epoch,
        }
    )
    state["status"] = "running"
    write_state(state_file, state)
    claim = {
        "run_id": run_id,
        "skill": node["skill"],
        "node": node_id,
        "attempt": attempt,
        "action": "execute" if attempt == 1 else "revise",
        "lease_id": lease_id,
        "attempt_dir": str(attempt_dir),
        "maker_context_path": str(attempt_dir / "maker-context.json"),
    }
    write_state(run_dir / "contexts" / f"{node_id}.json", claim)
    return claim


@locked
def complete_attempt(
    company_kb: Path,
    run_id: str,
    lease_id: str,
    node_id: str | None = None,
) -> dict[str, Any]:
    state_file, state = load_state(company_kb, run_id)
    node_id, node = loop_node(state, node_id)
    if node["status"] == "promoting":
        if node.get("lease_id") != lease_id:
            raise DelegateRunnerError("lease-id does not match the active attempt")
        attempt_dir = Path(node["attempt_dir"])
        try:
            result = json.loads(
                (attempt_dir / "gate-result.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise DelegateRunnerError(f"cannot recover promotion: {exc}") from exc
        return finish_promotion(state_file, state, node, attempt_dir, result)
    if node["status"] != "running":
        raise DelegateRunnerError(f"loop cannot complete from status {node['status']}")
    check_elapsed(state_file, state, node)
    if node.get("lease_id") != lease_id:
        raise DelegateRunnerError("lease-id does not match the active attempt")
    expires = node.get("lease_expires_epoch")
    if isinstance(expires, (int, float)) and time.time() >= expires:
        node["status"] = "retryable"
        node["failure_reason"] = "lease expired"
        state["status"] = "running"
        write_state(state_file, state)
        raise DelegateRunnerError("lease expired")

    attempt_dir = Path(node["attempt_dir"])
    changed = revision_errors(state_file.parent, ROOT, node["skill"])
    if changed:
        finding = {
            "reason": "frozen_revision_changed",
            "severity": "fatal",
            "repairable": False,
            "action": "human_review",
            "artifact_path": "artifacts",
            "field": "",
            "evidence_refs": [],
            "detail": ", ".join(changed),
        }
        result = {
            "run_id": run_id,
            "skill_id": node["skill"],
            "attempt": node["attempt"],
            "lease_id": lease_id,
            "decision": "blocked",
            "gate": "preflight",
            "status": "needs_review",
            "next_action": "human_review",
            "exit_code": 1,
            "errors": ["frozen revision changed"],
            "findings": [finding],
            "fingerprints": [finding_fingerprint(finding)],
            "checked_at": utc_at(),
        }
        write_state(attempt_dir / "gate-result.json", result)
        node["status"] = "needs_review"
        node["findings"] = [finding]
        state["status"] = "needs_review"
        write_state(state_file, state)
        return result
    outcome = check_node(ROOT, node["skill"], attempt_dir / "handoff.json", run_id)
    gate_mode = "node"
    if outcome.exit_code == 0 and node.get("final_gate"):
        outcome = check_final(
            ROOT,
            state["workflow_skill"],
            company_kb,
            run_id,
            candidate_skill_dir=attempt_dir,
        )
        gate_mode = "final"
    findings = normalize_findings(outcome)
    fingerprints = sorted(finding_fingerprint(item) for item in findings)
    previous = node.get("fingerprints", [])
    repeated = bool(fingerprints) and fingerprints == previous
    new_major = bool(previous) and any(
        fingerprint not in previous and finding.get("severity") == "major"
        for finding, fingerprint in zip(findings, map(finding_fingerprint, findings))
    )
    if outcome.exit_code == 0:
        status, next_action = "passed", "accept"
    elif any(item.get("action") == "upstream_contract_gap" for item in findings):
        status, next_action = "needs_review", "upstream_contract_gap"
    elif outcome.decision == "needs_review" or any(
        not item.get("repairable", False) for item in findings
    ):
        status, next_action = "needs_review", "human_review"
    elif repeated or new_major or node["attempt"] >= node["max_attempts"]:
        status = next_action = "exhausted"
    else:
        status, next_action = "retryable", "revise"

    result = {
        "run_id": run_id,
        "skill_id": node["skill"],
        "node_id": node_id,
        "attempt": node["attempt"],
        "lease_id": lease_id,
        "decision": outcome.decision,
        "gate": gate_mode,
        "status": status,
        "next_action": next_action,
        "exit_code": outcome.exit_code,
        "errors": [] if outcome.exit_code == 0 else list(outcome.messages),
        "findings": findings,
        "fingerprints": fingerprints,
        "candidate_sha256": directory_sha256(attempt_dir),
        "checked_at": utc_at(),
    }
    if outcome.judge is not None:
        result["judge"] = outcome.judge
    write_state(attempt_dir / "gate-result.json", result)

    if outcome.exit_code == 0:
        return finish_promotion(state_file, state, node, attempt_dir, result)
    else:
        node["status"] = status
        node["findings"] = findings
        node["fingerprints"] = fingerprints
        state["status"] = status
    write_state(state_file, state)
    return result


def loop_status(company_kb: Path, run_id: str) -> dict[str, Any]:
    _path, state = load_state(company_kb, run_id)
    _node_id, node = loop_node(state)
    return {"run_id": run_id, "status": node["status"], "node": node}


@locked
def cancel_loop(company_kb: Path, run_id: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise DelegateRunnerError("cancel reason is required")
    state_file, state = load_state(company_kb, run_id)
    _node_id, node = loop_node(state)
    if node["status"] in TERMINAL_STATUSES:
        raise DelegateRunnerError(f"loop cannot cancel from status {node['status']}")
    node["status"] = "cancelled"
    node["cancel_reason"] = reason.strip()
    state["status"] = "cancelled"
    write_state(state_file, state)
    return {"run_id": run_id, "status": "cancelled", "reason": reason.strip()}
    promote,
