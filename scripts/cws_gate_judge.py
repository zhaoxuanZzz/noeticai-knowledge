#!/usr/bin/env python3
"""Built-in CWS Gate Judge adapter (cws-gate-judge/v1).

OpenAI-compatible HTTP (stdlib) or mock mode. Shared by offline eval and runtime gate.

Environment:
  CWS_JUDGE_MODE=live|mock
  CWS_JUDGE_BASE_URL   OpenAI-compatible base URL
  CWS_JUDGE_API_KEY    or OPENAI_API_KEY
  CWS_JUDGE_MODEL      e.g. qwen3.7-max
  CWS_JUDGE_TIMEOUT_SECONDS  (default 120)

Usage:
  python3 scripts/cws_gate_judge.py
  python3 scripts/cws_gate_judge.py --mock
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

PROTOCOL_VERSION = "cws-gate-judge/v1"

MOCK_RESPONSES = {
    "company-profile-v1": {
        "decision": "passed",
        "confidence": 0.92,
        "findings": [],
    },
    "due-diligence-v1": {
        "decision": "passed",
        "confidence": 0.88,
        "findings": [],
    },
}


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _use_mock(argv: list[str]) -> bool:
    if "--mock" in argv:
        return True
    return _env("CWS_JUDGE_MODE", default="live").lower() == "mock"


def _mock_evaluate(request: dict[str, Any]) -> dict[str, Any]:
    rubric = request.get("rubric_id", "company-profile-v1")
    mock = MOCK_RESPONSES.get(rubric, MOCK_RESPONSES["company-profile-v1"])
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request["request_id"],
        "decision": mock["decision"],
        "confidence": mock["confidence"],
        "model": "mock-judge",
        "rubric_version": rubric,
        "findings": list(mock["findings"]),
    }


def _fallback(
    request: dict[str, Any],
    reason: str,
    detail: str,
    *,
    model: str = "error",
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request.get("request_id", "unknown"),
        "decision": "needs_review",
        "confidence": 0.0,
        "model": model,
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


def _build_prompt(request: dict[str, Any]) -> str:
    inp = request["input"]
    subject = inp.get("subject", {})
    handoff = inp.get("handoff", {})
    evidence = inp.get("evidence", {})
    report = inp.get("report", "")
    evidence_gaps = handoff.get("evidence_gaps", [])
    rubric = request.get("rubric_id", "company-profile-v1")

    if rubric == "company-profile-v1":
        task = (
            "You are evaluating a company profile handoff for quality and completeness. "
            "Check that:\n"
            "1. All claims in the handoff are supported by the provided evidence\n"
            "2. Any material risks visible in the evidence are reflected in the handoff\n"
            "3. Evidence gaps are honestly disclosed\n"
            "4. The subject identity in the handoff matches the evidence\n"
        )
    else:
        task = (
            "You are evaluating a due diligence report for quality and completeness. "
            "Check that:\n"
            "1. All claims are supported by evidence\n"
            "2. Material risks are not omitted\n"
            "3. Parent handoff data is correctly incorporated\n"
            "4. The report is consistent with the underlying evidence\n"
        )

    return f"""{task}

=== SUBJECT ===
{json.dumps(subject, ensure_ascii=False, indent=2)}

=== EVIDENCE ===
{json.dumps(evidence, ensure_ascii=False, indent=2)}

=== HANDOFF ===
{json.dumps(handoff, ensure_ascii=False, indent=2)}

=== EVIDENCE GAPS (disclosed) ===
{json.dumps(evidence_gaps, ensure_ascii=False, indent=2)}

=== REPORT ===
{report[:4000] if report else "(no report)"}

=== INSTRUCTIONS ===
Respond with a JSON object containing:
- decision: "passed" or "needs_review"
- confidence: float between 0 and 1
- findings: list of objects with reason, artifact_path, evidence_refs
  - reason should be a stable code such as material_risk_omitted, unsupported_claim,
    source_conflict_not_disclosed, or data_quality_issue
  - artifact_path is a JSON path in the handoff (e.g. "artifacts.company_summary")
  - evidence_refs is a list of evidence item IDs

Return ONLY the JSON object, no other text."""


def _chat_completions(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("chat completion response must be an object")
    return payload


def _live_evaluate(request: dict[str, Any]) -> dict[str, Any]:
    api_key = _env("CWS_JUDGE_API_KEY", "OPENAI_API_KEY")
    if not api_key:
        return _fallback(
            request,
            "judge_unavailable",
            "CWS_JUDGE_API_KEY or OPENAI_API_KEY is not set",
            model="unavailable",
        )
    base_url = _env(
        "CWS_JUDGE_BASE_URL",
        "OPENAI_BASE_URL",
        default="https://api.openai.com/v1",
    )
    model = _env("CWS_JUDGE_MODEL", "JUDGE_MODEL", default="gpt-4o-mini")
    try:
        timeout = float(_env("CWS_JUDGE_TIMEOUT_SECONDS", default="120"))
    except ValueError:
        timeout = 120.0
    prompt = _build_prompt(request)
    try:
        payload = _chat_completions(
            base_url=base_url,
            api_key=api_key,
            model=model,
            prompt=prompt,
            timeout=timeout,
        )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("chat completion missing choices")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("chat completion missing message content")
        result = json.loads(content)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return _fallback(request, "judge_error", str(exc), model=model)

    decision = result.get("decision", "needs_review")
    if decision not in {"passed", "needs_review"}:
        decision = "needs_review"
    try:
        confidence = float(result.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.0
    findings = result.get("findings", [])
    if not isinstance(findings, list):
        findings = []

    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request["request_id"],
        "decision": decision,
        "confidence": confidence,
        "model": model,
        "rubric_version": request.get("rubric_id", "company-profile-v1"),
        "findings": findings,
    }


def evaluate_line(line: str, *, use_mock: bool) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(request, dict) or "request_id" not in request:
        return None
    if use_mock:
        return _mock_evaluate(request)
    return _live_evaluate(request)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    use_mock = _use_mock(args)
    for line in sys.stdin:
        response = evaluate_line(line, use_mock=use_mock)
        if response is None:
            continue
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
