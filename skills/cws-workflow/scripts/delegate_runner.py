"""Persistent state for host-neutral delegated workflow execution."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import sys
from pathlib import Path
from typing import Any


class DelegateRunnerError(Exception):
    pass


ROOT = Path(__file__).resolve().parents[3]


def state_path(company_kb: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        raise DelegateRunnerError("run-id must use lowercase letters, digits, and hyphens")
    return company_kb / "artifacts" / run_id / "workflow-state.json"


def ready_nodes(state: dict[str, Any]) -> list[str]:
    nodes = state["nodes"]
    return [
        node_id
        for node_id, node in nodes.items()
        if node["status"] in {"pending", "failed", "retryable"}
        and all(nodes[parent]["status"] == "passed" for parent in node["parents"])
    ]


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def load_state(company_kb: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    path = state_path(company_kb, run_id)
    if not path.is_file():
        raise DelegateRunnerError(f"delegate run not found: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DelegateRunnerError(f"cannot read delegate state {path}: {exc}") from exc
    if state.get("run_id") != run_id or not isinstance(state.get("nodes"), dict):
        raise DelegateRunnerError(f"invalid delegate state: {path}")
    return path, state


def state_view(state: dict[str, Any]) -> dict[str, Any]:
    return {**state, "ready": ready_nodes(state)}


def start(company_kb: Path, run_id: str, node_id: str) -> dict[str, Any]:
    path, state = load_state(company_kb, run_id)
    if node_id not in ready_nodes(state):
        raise DelegateRunnerError(f"node is not ready: {node_id}")
    node = state["nodes"][node_id]
    node["status"] = "running"
    node["attempt"] += 1
    node.pop("failure_reason", None)
    write_state(path, state)
    return {"run_id": run_id, "node": node_id, "status": "running", "attempt": node["attempt"]}


def fail(company_kb: Path, run_id: str, node_id: str, reason: str) -> dict[str, Any]:
    path, state = load_state(company_kb, run_id)
    node = state["nodes"].get(node_id)
    if node is None:
        raise DelegateRunnerError(f"unknown node: {node_id}")
    if node["status"] != "running":
        raise DelegateRunnerError(f"node cannot fail from status {node['status']}: {node_id}")
    if not reason.strip():
        raise DelegateRunnerError("failure reason is required")
    node["status"] = "failed"
    node["failure_reason"] = reason.strip()
    state["status"] = "running"
    write_state(path, state)
    return {"run_id": run_id, "node": node_id, "status": "failed", "reason": reason.strip()}


def _write_gate_result(node: dict[str, Any], result: dict[str, Any]) -> Path:
    directory = Path(node["handoff_path"]).parent
    directory.mkdir(parents=True, exist_ok=True)
    named = directory / f"gate-result-{result['attempt']}.json"
    write_state(named, result)
    write_state(directory / "gate-result.json", result)
    return named


def complete(company_kb: Path, run_id: str, node_id: str) -> dict[str, Any]:
    path, state = load_state(company_kb, run_id)
    node = state["nodes"].get(node_id)
    if node is None:
        raise DelegateRunnerError(f"unknown node: {node_id}")
    if node["status"] == "passed":
        result_path = node.get("gate_result_path")
        return json.loads(Path(result_path).read_text(encoding="utf-8")) if result_path else {
            "run_id": run_id, "node_id": node_id, "status": "passed"
        }
    if node["status"] not in {"running", "blocked", "needs_review"}:
        raise DelegateRunnerError(f"node cannot be completed from status {node['status']}: {node_id}")

    node["status"] = "validating"
    write_state(path, state)
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from check_artifact_gate import check_final, check_node

    outcome = check_node(ROOT, node["skill"], Path(node["handoff_path"]), run_id)
    gate = "node"
    if outcome.exit_code == 0 and node.get("final_gate"):
        outcome = check_final(ROOT, state["workflow_skill"], company_kb, run_id)
        gate = "final"
    if outcome.decision == "passed":
        status = "passed"
    elif outcome.decision == "needs_review":
        status = "needs_review"
    else:
        status = "blocked"
    result_dir = Path(node["handoff_path"]).parent
    gate_attempt = len(list(result_dir.glob("gate-result-*.json"))) + 1
    result = {
        "run_id": run_id,
        "node_id": node_id,
        "skill": node["skill"],
        "attempt": gate_attempt,
        "status": status,
        "decision": outcome.decision,
        "gate": gate,
        "exit_code": outcome.exit_code,
        "errors": [] if outcome.exit_code == 0 else list(outcome.messages),
        "handoff_path": node["handoff_path"],
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if outcome.judge is not None:
        result["judge"] = outcome.judge
    result_path = _write_gate_result(node, result)
    node["gate_result_path"] = str(result_path)
    node["status"] = result["status"]
    statuses = {item["status"] for item in state["nodes"].values()}
    if statuses == {"passed"}:
        state["status"] = "passed"
    elif "blocked" in statuses:
        state["status"] = "blocked"
    elif "needs_review" in statuses:
        state["status"] = "needs_review"
    else:
        state["status"] = "running"
    write_state(path, state)
    return result


def initialize(
    company_kb: Path,
    run_id: str,
    workflow_skill: str,
    company: str,
    plan_nodes: list[dict[str, Any]],
    frozen_kb: bool = False,
) -> dict[str, Any]:
    path = state_path(company_kb, run_id)
    if path.exists():
        raise DelegateRunnerError(f"delegate run already exists: {path}")

    nodes = {
        node["id"]: {
            **node,
            "status": "pending",
            "attempt": 0,
            "gate_result_path": None,
        }
        for node in plan_nodes
    }
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "workflow_skill": workflow_skill,
        "company": company,
        "frozen_kb": frozen_kb,
        "status": "running",
        "nodes": nodes,
    }
    write_state(path, state)
    return {"run_id": run_id, "state_path": str(path), "ready": ready_nodes(state)}
