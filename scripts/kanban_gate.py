"""Hermes Kanban completion gate for CWS workflow tasks."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
_CONTEXT = re.compile(r"cws_gate: skill=([a-z0-9-]+) run_id=([a-z0-9-]+)")
_LOOP_CONTEXT = re.compile(
    r"cws_loop: run_id=([a-z0-9-]+) node=([a-z0-9-]+)"
)


def task_context(body: str | None) -> tuple[str, str] | None:
    match = _CONTEXT.search(body or "")
    return (match.group(1), match.group(2)) if match else None


def loop_context(body: str | None) -> tuple[str, str] | None:
    match = _LOOP_CONTEXT.search(body or "")
    return (match.group(1), match.group(2)) if match else None


def _loop_runtime():
    scripts = ROOT / "skills" / "cws-workflow" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from atomic_loop import complete_attempt, next_attempt
    from delegate_runner import DelegateRunnerError, load_state

    return complete_attempt, next_attempt, DelegateRunnerError, load_state


def claim_loop(
    task_id: str,
    body: str | None,
    *,
    company_kb: Path | None = None,
) -> dict[str, Any] | None:
    context = loop_context(body)
    if context is None:
        return None
    run_id, node_id = context
    _complete_attempt, next_attempt, error_type, load_state = _loop_runtime()
    company_kb = company_kb or Path(
        os.environ.get("CWS_COMPANY_KB_DIR", "~/.cws/company-knowledge")
    ).expanduser()
    _path, state = load_state(company_kb, run_id)
    node = state["nodes"].get(node_id)
    if node is None or node.get("kanban_task_id") != task_id:
        raise error_type(f"Kanban task is not bound to loop node: {task_id}")
    return next_attempt(company_kb, run_id, node_id)


def _default_requeue(
    task_id: str,
    reason: str,
    *,
    board: str | None = None,
) -> bool:
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        retry_task = getattr(kanban_db, "retry_task", None)
        if retry_task is None:
            kanban_db.block_task(
                conn,
                task_id,
                reason=(
                    "CWS loop requires a worker-safe Hermes retry_task API: "
                    + reason
                ),
            )
            return False
        return bool(retry_task(conn, task_id, reason=reason))
    finally:
        conn.close()


def _block_loop_task(
    task_id: str,
    reason: str,
    *,
    board: str | None = None,
) -> None:
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        kanban_db.block_task(conn, task_id, reason=reason)
    finally:
        conn.close()


def complete_loop(
    task_id: str,
    body: str | None,
    *,
    board: str | None = None,
    company_kb: Path | None = None,
    requeue: Callable[[str, str], bool] | None = None,
) -> dict[str, Any] | None:
    context = loop_context(body)
    if context is None:
        return None
    run_id, node_id = context
    complete_attempt, _next_attempt, error_type, load_state = _loop_runtime()
    company_kb = company_kb or Path(
        os.environ.get("CWS_COMPANY_KB_DIR", "~/.cws/company-knowledge")
    ).expanduser()
    _path, state = load_state(company_kb, run_id)
    node = state["nodes"].get(node_id)
    if node is None or node.get("kanban_task_id") != task_id:
        raise error_type(f"Kanban task is not bound to loop node: {task_id}")
    lease_id = node.get("lease_id")
    if not lease_id:
        raise error_type(f"loop node has no active lease: {node_id}")
    result = complete_attempt(company_kb, run_id, lease_id, node_id)
    if result.get("status") == "passed":
        return result
    if result.get("next_action") != "revise":
        _block_loop_task(
            task_id,
            f"CWS loop paused: {result.get('next_action', result.get('status'))}",
            board=board,
        )
        result["kanban_action"] = "blocked"
        return result
    reason = "; ".join(result.get("errors") or ["CWS loop requested revision"])
    did_requeue = (
        requeue(task_id, reason)
        if requeue is not None
        else _default_requeue(task_id, reason, board=board)
    )
    result["kanban_action"] = "requeued" if did_requeue else "blocked"
    if not did_requeue:
        result["requeue_error"] = "Hermes retry_task capability is unavailable"
    return result


def _result_dir(run_id: str, skill: str) -> Path:
    root = Path(os.environ.get("CWS_COMPANY_KB_DIR", "~/.cws/company-knowledge")).expanduser()
    return root / "artifacts" / run_id / skill


def _write_result(run_id: str, skill: str, result: dict[str, Any]) -> Path:
    directory = _result_dir(run_id, skill)
    directory.mkdir(parents=True, exist_ok=True)
    attempts = sorted(directory.glob("gate-result-*.json"))
    result["attempt"] = len(attempts) + 1
    named = directory / f"gate-result-{result['attempt']}.json"
    for path in (named, directory / "gate-result.json"):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    return named


def check(skill: str, run_id: str) -> dict[str, Any] | None:
    """Run declared node/final gates; return None for skills without a gate."""
    from card_gate import CardGateError, load_skill_gate
    from check_artifact_gate import check_final, check_node

    try:
        _card, gate = load_skill_gate(ROOT, skill)
    except CardGateError as exc:
        return {"status": "blocked", "gate": "node", "errors": [str(exc)], "exit_code": 2}
    if gate is None:
        return None

    handoff = _result_dir(run_id, skill) / "handoff.json"
    outcome = check_node(ROOT, skill, handoff, run_id)
    gate_name = "node"
    if outcome.exit_code == 0 and gate.get("final"):
        outcome = check_final(ROOT, skill, _result_dir(run_id, skill).parents[2], run_id)
        gate_name = "final"
    if outcome.decision == "passed":
        status = "passed"
    elif outcome.decision == "needs_review":
        # Kanban maps needs_review to blocked pause; gate-result keeps decision.
        status = "blocked"
    else:
        status = "blocked"
    result = {
        "run_id": run_id,
        "skill_id": skill,
        "gate": gate_name,
        "status": status,
        "decision": outcome.decision,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "handoff_path": str(handoff),
        "exit_code": outcome.exit_code,
        "errors": [] if outcome.exit_code == 0 else list(outcome.messages),
    }
    if outcome.judge is not None:
        result["judge"] = outcome.judge
    result["result_path"] = str(_write_result(run_id, skill, result))
    return result


def gate_completion(task_id: str, body: str | None, *, board: str | None = None) -> dict[str, Any] | None:
    context = task_context(body)
    if context is None:
        return None
    skill, run_id = context
    result = check(skill, run_id)
    if result is None or result["status"] == "passed":
        return result
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        kanban_db.block_task(conn, task_id, reason="CWS gate failed: " + "; ".join(result["errors"]))
    finally:
        conn.close()
    return result


def retry(task_id: str, body: str | None, *, board: str | None = None) -> dict[str, Any]:
    context = task_context(body)
    if context is None:
        raise ValueError("task is not a CWS gate task")
    result = check(*context)
    if result is None or result["status"] == "passed":
        from hermes_cli import kanban_db

        conn = kanban_db.connect(board=board)
        try:
            if not kanban_db.unblock_task(conn, task_id):
                raise ValueError("task is not blocked")
        finally:
            conn.close()
    return result or {"status": "passed", "gate": "none"}


def waive(task_id: str, body: str | None, reason: str, *, board: str | None = None) -> dict[str, Any]:
    context = task_context(body)
    if context is None:
        raise ValueError("task is not a CWS gate task")
    skill, run_id = context
    if not reason.strip():
        raise ValueError("--reason is required")
    result = {
        "run_id": run_id,
        "skill_id": skill,
        "gate": "final" if skill in {"cws-due-diligence", "cws-investment-analysis"} else "node",
        "status": "waived",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "handoff_path": str(_result_dir(run_id, skill) / "handoff.json"),
        "exit_code": None,
        "errors": [],
        "waiver": {"reason": reason.strip(), "actor": os.environ.get("USER", "unknown"), "waived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    }
    result["result_path"] = str(_write_result(run_id, skill, result))
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        if not kanban_db.complete_task(conn, task_id, summary="CWS gate waived: " + reason.strip(), metadata={"cws_gate": result}):
            raise ValueError("task is not blocked or could not be completed")
    finally:
        conn.close()
    return result
