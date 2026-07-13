"""Hermes Kanban completion gate for Noetic workflow tasks."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_CONTEXT = re.compile(r"noetic_gate: skill=([a-z0-9-]+) run_id=([a-z0-9-]+)")


def task_context(body: str | None) -> tuple[str, str] | None:
    match = _CONTEXT.search(body or "")
    return (match.group(1), match.group(2)) if match else None


def _result_dir(run_id: str, skill: str) -> Path:
    root = Path(os.environ.get("NOETICAI_COMPANY_KB_DIR", "~/.noeticai/company-knowledge")).expanduser()
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
    code, errors = check_node(ROOT, skill, handoff, run_id)
    gate_name = "node"
    if code == 0 and gate.get("final"):
        code, errors = check_final(ROOT, skill, _result_dir(run_id, skill).parents[2], run_id)
        gate_name = "final"
    result = {
        "run_id": run_id,
        "skill_id": skill,
        "gate": gate_name,
        "status": "passed" if code == 0 else "blocked",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "handoff_path": str(handoff),
        "exit_code": code,
        "errors": [] if code == 0 else errors,
    }
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
        kanban_db.block_task(conn, task_id, reason="Noetic gate failed: " + "; ".join(result["errors"]))
    finally:
        conn.close()
    return result


def retry(task_id: str, body: str | None, *, board: str | None = None) -> dict[str, Any]:
    context = task_context(body)
    if context is None:
        raise ValueError("task is not a Noetic gate task")
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
        raise ValueError("task is not a Noetic gate task")
    skill, run_id = context
    if not reason.strip():
        raise ValueError("--reason is required")
    result = {
        "run_id": run_id,
        "skill_id": skill,
        "gate": "final" if skill in {"noetic-due-diligence", "noetic-investment-analysis"} else "node",
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
        if not kanban_db.complete_task(conn, task_id, summary="Noetic gate waived: " + reason.strip(), metadata={"noetic_gate": result}):
            raise ValueError("task is not blocked or could not be completed")
    finally:
        conn.close()
    return result
