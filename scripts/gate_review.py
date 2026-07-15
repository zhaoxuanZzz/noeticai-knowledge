#!/usr/bin/env python3
"""Human-readable gate review packs for needs_review decisions."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from gate_judge_adapter import minimize_handoff
from semantic_gate import json_path_get

SCHEMA_VERSION = 1
FINDING_VERDICTS = frozenset({"confirm", "reject", "uncertain"})
PACKAGE_ACTIONS = frozenset({"waive", "reject_rerun", "capability_gap"})

REASON_CATALOG: dict[str, tuple[str, str]] = {
    "material_risk_omitted": ("重大风险遗漏", "证据显示存在重大风险，但产物未披露。"),
    "source_conflict_not_disclosed": (
        "来源冲突未披露",
        "多来源结论冲突且未写入 conflicts/evidence_gaps。",
    ),
    "stale_data_not_disclosed": ("过期数据未披露", "数据超过时效阈值且未标注缺口或风险。"),
    "unsupported_claim": ("结论缺乏证据", "产物中的结论无法被当前证据集合支持。"),
    "missing_data_not_disclosed": ("缺失未披露", "关键字段缺失但未写入 evidence_gaps。"),
    "subject_ambiguous": ("主体歧义", "无法唯一确定目标企业主体。"),
    "subject_identity_mismatch": ("主体不一致", "名称与统一社会信用代码等标识冲突。"),
    "judge_low_confidence": ("Judge 置信度不足", "语义评审置信度低于阈值，需人工确认。"),
    "judge_unavailable": ("Judge 不可用", "adapter/密钥/超时导致无法评审，fail closed。"),
    "judge_disabled": ("Judge 已禁用", "CWS_JUDGE_ENABLED=0，不能当作语义通过。"),
    "judge_needs_review": ("语义需复核", "Judge 返回 needs_review。"),
    "negative_claim_without_search_coverage": (
        "否定结论缺检索覆盖",
        "无诉讼/无风险等结论缺少完整检索证据。",
    ),
}


def reason_label(reason: str) -> tuple[str, str]:
    if reason in REASON_CATALOG:
        return REASON_CATALOG[reason]
    return reason, "未知原因码，请按 artifact/evidence 人工判断。"


def minimize_evidence(evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    allowed = ("run_id", "skill_id", "subject", "evidence", "claims", "conflicts")
    return {key: evidence[key] for key in allowed if key in evidence}


def minimize_judge(judge: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(judge, dict):
        return {}
    allowed = (
        "decision",
        "confidence",
        "model",
        "rubric_version",
        "evaluator_id",
        "findings",
        "error",
        "reason",
    )
    return {key: judge[key] for key in allowed if key in judge}


def compute_runtime_gate_input_hash(
    handoff: dict[str, Any],
    evidence: dict[str, Any] | None,
    judge: dict[str, Any] | None,
) -> str:
    payload = {
        "handoff": minimize_handoff(handoff if isinstance(handoff, dict) else {}),
        "evidence": minimize_evidence(evidence),
        "judge": minimize_judge(judge),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    return "sha256:" + digest.hexdigest()


def excerpt_finding(
    finding: dict[str, Any],
    handoff: dict[str, Any],
    evidence_doc: dict[str, Any] | None,
) -> dict[str, Any]:
    path = finding.get("artifact_path")
    artifact_value: Any = None
    artifact_missing = False
    if isinstance(path, str) and path:
        try:
            artifact_value = json_path_get(handoff, path)
        except KeyError:
            artifact_missing = True
    else:
        artifact_missing = True

    by_id = {
        item["id"]: item
        for item in (evidence_doc or {}).get("evidence") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    refs = finding.get("evidence_refs") or []
    if not isinstance(refs, list):
        refs = []
    evidence_rows: list[dict[str, Any]] = []
    missing_refs: list[str] = []
    for ref in refs:
        if not isinstance(ref, str):
            continue
        row = by_id.get(ref)
        if row is None:
            missing_refs.append(ref)
            continue
        evidence_rows.append(
            {
                "id": row.get("id"),
                "field": row.get("field"),
                "value": row.get("value"),
                "source_ref": row.get("source_ref"),
            }
        )
    return {
        "reason": finding.get("reason"),
        "artifact_path": path,
        "artifact_value": artifact_value,
        "artifact_missing": artifact_missing,
        "evidence": evidence_rows,
        "missing_evidence_refs": missing_refs,
    }


def build_decision_template(
    findings: list[dict[str, Any]],
    gate_input_hash: str,
) -> dict[str, Any]:
    rows = []
    for finding in findings:
        rows.append(
            {
                "reason": finding.get("reason"),
                "artifact_path": finding.get("artifact_path"),
                "verdict": None,
                "note": None,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pending",
        "reviewed_at": None,
        "reviewer": None,
        "gate_input_hash": gate_input_hash,
        "findings": rows,
        "action": None,
        "action_reason": None,
    }


def validate_decision(
    doc: dict[str, Any],
    *,
    expected_hash: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return False, ["decision must be a JSON object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if doc.get("gate_input_hash") != expected_hash:
        errors.append("gate_input_hash mismatch; regenerate review pack")
    status = doc.get("status")
    if status == "pending":
        return (len(errors) == 0), errors
    if status != "completed":
        errors.append("status must be pending or completed")
        return False, errors

    findings = doc.get("findings")
    if not isinstance(findings, list) or not findings:
        errors.append("completed decision requires findings")
    else:
        for index, row in enumerate(findings):
            if not isinstance(row, dict):
                errors.append(f"findings[{index}] must be object")
                continue
            if row.get("verdict") not in FINDING_VERDICTS:
                errors.append(f"findings[{index}].verdict must be one of {sorted(FINDING_VERDICTS)}")
    action = doc.get("action")
    if action not in PACKAGE_ACTIONS:
        errors.append(f"action must be one of {sorted(PACKAGE_ACTIONS)}")
    reason = doc.get("action_reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("action_reason must be a non-empty string")
    return (len(errors) == 0), errors


def _format_value(value: Any) -> str:
    if value is None:
        return "（缺失）"
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) > 800:
        return text[:800] + "…"
    return text


def render_review_markdown(ctx: dict[str, Any]) -> str:
    judge = ctx.get("judge") if isinstance(ctx.get("judge"), dict) else {}
    findings = judge.get("findings") if isinstance(judge.get("findings"), list) else []
    handoff = ctx.get("handoff") if isinstance(ctx.get("handoff"), dict) else {}
    evidence = ctx.get("evidence") if isinstance(ctx.get("evidence"), dict) else {}
    subject = ctx.get("subject_name") or "（未知主体）"
    skill = ctx.get("skill_id") or "（未知 skill）"
    lines = [
        f"# Gate 人工审核 · {subject} · {skill}",
        "",
        "## 摘要",
        f"- 决策：`{ctx.get('decision') or ctx.get('actual_decision') or 'needs_review'}`",
        f"- 置信度：{judge.get('confidence')}",
        f"- 模型：{judge.get('model')}",
        f"- rubric：{judge.get('rubric_version')}",
    ]
    if ctx.get("run_id"):
        lines.append(f"- run_id：`{ctx['run_id']}`")
    if ctx.get("case_id"):
        lines.append(f"- case_id：`{ctx['case_id']}`")
    if ctx.get("checked_at"):
        lines.append(f"- 检查时间：{ctx['checked_at']}")
    if ctx.get("expected_decision") is not None:
        lines.append(f"- 期望决策：`{ctx['expected_decision']}`")
        lines.append(f"- 实际决策：`{ctx.get('actual_decision')}`")
    lines.extend(
        [
            "- 你需要做的事：核对 findings → 填写同目录 `review-decision.json`",
            "",
        ]
    )
    if not findings:
        lines.extend(
            [
                "## Finding",
                "",
                "（无结构化 findings；请结合 gate-result / Judge 错误原因人工判断。）",
                "",
            ]
        )
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            continue
        reason = str(finding.get("reason") or "unknown")
        title, blurb = reason_label(reason)
        excerpt = excerpt_finding(finding, handoff, evidence)
        value_text = (
            "（路径缺失）"
            if excerpt["artifact_missing"]
            else _format_value(excerpt["artifact_value"])
        )
        lines.extend(
            [
                f"## Finding {index}：{title}",
                f"- 原因码：`{reason}`",
                f"- 人话：{blurb}",
                f"- 产物摘录：`{excerpt.get('artifact_path') or '（无路径）'}` →",
                "```json",
                value_text,
                "```",
                "- 相关证据：",
            ]
        )
        if excerpt["evidence"]:
            for row in excerpt["evidence"]:
                lines.append(
                    f"  - `{row.get('id')}` / {row.get('field')} = "
                    f"{_format_value(row.get('value'))} · `{row.get('source_ref')}`"
                )
        else:
            lines.append("  - （无）")
        if excerpt["missing_evidence_refs"]:
            lines.append(
                f"- 缺失 evidence_refs：{', '.join(excerpt['missing_evidence_refs'])}"
            )
        lines.extend(
            [
                "- 建议动作：核对后在决策文件中选择 `reject_rerun` | `waive` | `capability_gap`",
                "",
            ]
        )
    lines.extend(
        [
            "## 决策填写说明",
            "",
            "编辑同目录 `review-decision.json`：",
            "",
            "1. 为每条 finding 填写 `verdict`：`confirm` | `reject` | `uncertain`",
            "2. 填写整包 `action`：`waive` | `reject_rerun` | `capability_gap`",
            "3. 填写非空 `action_reason`，并将 `status` 改为 `completed`",
            "",
            "第一版仅记录审计，不会自动解锁下游或改写 gate-result。",
            "勿手改本 `review.md`；产物变更后请重新渲染审核包。",
            "",
        ]
    )
    return "\n".join(lines)


def write_review_pack(
    directory: Path,
    ctx: dict[str, Any],
    *,
    gate_input_hash: str,
) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    judge = ctx.get("judge") if isinstance(ctx.get("judge"), dict) else {}
    findings = judge.get("findings") if isinstance(judge.get("findings"), list) else []
    md_path = directory / "review.md"
    decision_path = directory / "review-decision.json"
    md_path.write_text(render_review_markdown(ctx), encoding="utf-8")
    template = build_decision_template(
        [item for item in findings if isinstance(item, dict)],
        gate_input_hash,
    )
    decision_path.write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"review_md": md_path, "decision_json": decision_path}


def build_context_from_handoff_dir(directory: Path) -> dict[str, Any] | None:
    result_path = directory / "gate-result.json"
    handoff_path = directory / "handoff.json"
    if not result_path.is_file() or not handoff_path.is_file():
        return None
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("decision") != "needs_review":
        return None
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    evidence: dict[str, Any] = {}
    evidence_path = directory / "evidence.json"
    if evidence_path.is_file():
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    subject = ""
    if isinstance(handoff.get("subject"), dict):
        subject = str(handoff["subject"].get("name") or "")
    elif isinstance(evidence.get("subject"), dict):
        subject = str(evidence["subject"].get("name") or "")
    return {
        "subject_name": subject or "（未知主体）",
        "skill_id": result.get("skill_id") or evidence.get("skill_id"),
        "decision": "needs_review",
        "actual_decision": "needs_review",
        "run_id": result.get("run_id") or handoff.get("run_id"),
        "checked_at": result.get("checked_at")
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "judge": result.get("judge") or {},
        "handoff": handoff,
        "evidence": evidence,
    }


def write_review_pack_from_handoff_dir(directory: Path) -> dict[str, Path] | None:
    ctx = build_context_from_handoff_dir(directory)
    if ctx is None:
        return None
    gate_hash = compute_runtime_gate_input_hash(
        ctx.get("handoff") or {},
        ctx.get("evidence"),
        ctx.get("judge"),
    )
    return write_review_pack(directory, ctx, gate_input_hash=gate_hash)


def write_eval_review_pack(
    reviews_root: Path,
    result: dict[str, Any],
) -> dict[str, Path] | None:
    if result.get("actual_decision") != "needs_review":
        return None
    case_id = result.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        return None
    review_ctx = result.get("review_context")
    if not isinstance(review_ctx, dict):
        return None
    out = reviews_root / case_id
    judge = result.get("judge") if isinstance(result.get("judge"), dict) else {}
    if not judge and isinstance(review_ctx.get("judge"), dict):
        judge = review_ctx["judge"]
    ctx = {
        "subject_name": review_ctx.get("subject_name") or "（未知主体）",
        "skill_id": review_ctx.get("skill_id"),
        "decision": "needs_review",
        "actual_decision": result.get("actual_decision"),
        "expected_decision": result.get("expected_decision"),
        "run_id": review_ctx.get("run_id"),
        "case_id": case_id,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "judge": judge,
        "handoff": review_ctx.get("handoff") or {},
        "evidence": review_ctx.get("evidence") or {},
    }
    gate_hash = result.get("input_hash")
    if not isinstance(gate_hash, str) or not gate_hash.startswith("sha256:"):
        gate_hash = compute_runtime_gate_input_hash(
            ctx["handoff"], ctx["evidence"], ctx["judge"]
        )
    paths = write_review_pack(out, ctx, gate_input_hash=gate_hash)
    context_path = out / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "case_id": case_id,
                "gate_input_hash": gate_hash,
                "subject_name": ctx["subject_name"],
                "skill_id": ctx["skill_id"],
                "run_id": ctx["run_id"],
                "expected_decision": ctx["expected_decision"],
                "actual_decision": ctx["actual_decision"],
                "handoff": minimize_handoff(ctx["handoff"]),
                "evidence": minimize_evidence(ctx["evidence"]),
                "judge": minimize_judge(ctx["judge"]),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    paths["context_json"] = context_path
    return paths
