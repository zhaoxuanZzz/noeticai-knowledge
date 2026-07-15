#!/usr/bin/env python3
"""Long-lived JSONL adapter client for semantic gate judges."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from semantic_gate import json_path_get


PROTOCOL_VERSION = "cws-gate-judge/v1"
JUDGE_DECISIONS = {"passed", "needs_review"}
DEFAULT_JUDGE_SCRIPT = "cws_gate_judge.py"
RUNTIME_MIN_CONFIDENCE = 0.75


class JudgeAdapterError(Exception):
    """Invalid judge protocol or adapter infrastructure."""


def minimize_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    allowed = ("run_id", "subject", "data_as_of", "evidence_gaps", "artifacts")
    return {key: handoff[key] for key in allowed if key in handoff}


def default_judge_script(plugin_root: Path) -> Path:
    return (plugin_root / "scripts" / DEFAULT_JUDGE_SCRIPT).resolve()


def resolve_judge_path(plugin_root: Path, override: Path | str | None = None) -> Path | None:
    """Resolve adapter path: override / CWS_JUDGE_ADAPTER / plugin default."""
    if override is not None:
        path = Path(override)
    else:
        env_path = os.environ.get("CWS_JUDGE_ADAPTER", "").strip()
        path = Path(env_path) if env_path else default_judge_script(plugin_root)
    if not path.is_absolute():
        path = path.resolve()
    if not path.is_file():
        return None
    return path


def judge_launch_command(adapter_path: Path, *, extra_args: list[str] | None = None) -> list[str]:
    """Build argv to start adapter without a shell."""
    args = list(extra_args or [])
    if adapter_path.suffix == ".py" or not os.access(adapter_path, os.X_OK):
        return [sys.executable, str(adapter_path), *args]
    return [str(adapter_path), *args]


def judge_enabled() -> bool:
    return os.environ.get("CWS_JUDGE_ENABLED", "1").strip() != "0"


def raw_summaries_from_evidence(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in evidence.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if not isinstance(field, str) or not field:
            continue
        summaries.append(
            {
                "source_id": item.get("id") or item.get("source_ref") or "unknown",
                "facts": {field: item.get("value")},
            }
        )
    return summaries


def build_judge_request(
    *,
    request_id: str,
    case_id: str,
    evaluator_id: str,
    rubric_id: str,
    subject: Any,
    evidence: dict[str, Any],
    handoff: dict[str, Any],
    parent_handoffs: list[dict[str, Any]] | None = None,
    report: str = "",
    raw_summaries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "case_id": case_id,
        "evaluator_id": evaluator_id,
        "rubric_id": rubric_id,
        "input": {
            "subject": subject if isinstance(subject, dict) else {},
            "raw_summaries": raw_summaries
            if raw_summaries is not None
            else raw_summaries_from_evidence(evidence),
            "evidence": evidence,
            "handoff": minimize_handoff(handoff),
            "parent_handoffs": parent_handoffs or [],
            "report": report,
        },
    }


def unavailable_judge_result(
    request: dict[str, Any] | None,
    reason: str,
    detail: str,
) -> dict[str, Any]:
    request = request or {}
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request.get("request_id", f"judge-{uuid.uuid4().hex[:12]}"),
        "decision": "needs_review",
        "confidence": 0.0,
        "model": "unavailable",
        "rubric_version": request.get("rubric_id", "unknown"),
        "findings": [
            {
                "reason": reason,
                "artifact_path": "artifacts",
                "evidence_refs": [],
                "detail": detail,
            }
        ],
    }


class JudgeAdapter:
    def __init__(self, command: list[str] | Path, timeout: int) -> None:
        self.timeout = timeout
        if isinstance(command, Path):
            self.command = judge_launch_command(command)
        else:
            self.command = list(command)
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise JudgeAdapterError(f"unable to start judge adapter: {exc}") from exc
        assert self.process.stdout is not None
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.process.poll() is not None:
            raise JudgeAdapterError("judge adapter exited unexpectedly")
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise JudgeAdapterError(f"judge adapter write failed: {exc}") from exc
        if not self.selector.select(self.timeout):
            raise JudgeAdapterError(f"judge adapter timed out after {self.timeout}s")
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JudgeAdapterError("judge adapter returned invalid JSON") from exc
        self._validate(response, request)
        return response

    @staticmethod
    def _validate(response: Any, request: dict[str, Any]) -> None:
        if not isinstance(response, dict):
            raise JudgeAdapterError("judge response must be an object")
        if response.get("protocol_version") != PROTOCOL_VERSION:
            raise JudgeAdapterError("judge protocol_version mismatch")
        if response.get("request_id") != request["request_id"]:
            raise JudgeAdapterError("judge request_id mismatch")
        if response.get("decision") not in JUDGE_DECISIONS:
            raise JudgeAdapterError("judge decision must be passed or needs_review")
        confidence = response.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise JudgeAdapterError("judge confidence must be between 0 and 1")
        if not isinstance(response.get("model"), str) or not response["model"]:
            raise JudgeAdapterError("judge model is required")
        if response.get("rubric_version") != request["rubric_id"]:
            raise JudgeAdapterError("judge rubric_version mismatch")
        findings = response.get("findings")
        if not isinstance(findings, list):
            raise JudgeAdapterError("judge findings must be a list")
        for finding in findings:
            if not isinstance(finding, dict) or not all(
                isinstance(finding.get(key), str) and finding[key]
                for key in ("reason", "artifact_path")
            ) or not isinstance(finding.get("evidence_refs"), list):
                raise JudgeAdapterError(
                    "judge finding requires reason, artifact_path and evidence_refs"
                )
            try:
                json_path_get(request["input"]["handoff"], finding["artifact_path"])
            except KeyError as exc:
                raise JudgeAdapterError(
                    f"judge finding artifact_path is not resolvable: "
                    f"{finding['artifact_path']}"
                ) from exc
            evidence_ids = {
                item.get("id")
                for item in request["input"]["evidence"].get("evidence", [])
                if isinstance(item, dict)
            }
            if any(ref not in evidence_ids for ref in finding["evidence_refs"]):
                raise JudgeAdapterError("judge finding has unknown evidence_refs")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


def run_judge_once(
    plugin_root: Path,
    request: dict[str, Any],
    *,
    timeout: int | None = None,
    adapter_override: Path | None = None,
) -> dict[str, Any]:
    """Start adapter, evaluate one request, close. Fail closed on infra errors."""
    if not judge_enabled():
        return unavailable_judge_result(
            request, "judge_disabled", "CWS_JUDGE_ENABLED=0"
        )
    adapter_path = resolve_judge_path(plugin_root, adapter_override)
    if adapter_path is None:
        return unavailable_judge_result(
            request,
            "judge_unavailable",
            "judge adapter executable not found",
        )
    if timeout is None:
        try:
            timeout = int(os.environ.get("CWS_JUDGE_TIMEOUT_SECONDS", "120"))
        except ValueError:
            timeout = 120
    timeout = max(1, min(timeout, 600))
    adapter: JudgeAdapter | None = None
    try:
        adapter = JudgeAdapter(judge_launch_command(adapter_path), timeout)
        return adapter.evaluate(request)
    except JudgeAdapterError as exc:
        return unavailable_judge_result(request, "judge_unavailable", str(exc))
    finally:
        if adapter is not None:
            adapter.close()


def apply_confidence_gate(
    judge_result: dict[str, Any],
    *,
    min_confidence: float = RUNTIME_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Force needs_review when confidence is below threshold."""
    result = dict(judge_result)
    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < min_confidence:
        result["decision"] = "needs_review"
        findings = list(result.get("findings") or [])
        if not any(
            isinstance(item, dict) and item.get("reason") == "judge_low_confidence"
            for item in findings
        ):
            findings.append(
                {
                    "reason": "judge_low_confidence",
                    "artifact_path": "artifacts",
                    "evidence_refs": [],
                }
            )
        result["findings"] = findings
    return result
