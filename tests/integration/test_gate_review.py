#!/usr/bin/env python3
"""Tests for human-readable gate review packs."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from gate_review import (  # noqa: E402
    build_decision_template,
    compute_runtime_gate_input_hash,
    excerpt_finding,
    reason_label,
    render_review_markdown,
    validate_decision,
    write_eval_review_pack,
    write_review_pack,
    write_review_pack_from_handoff_dir,
)


class GateReviewUnitTests(unittest.TestCase):
    def test_reason_label_known(self) -> None:
        title, blurb = reason_label("material_risk_omitted")
        self.assertIn("风险", title)
        self.assertTrue(blurb)

    def test_reason_label_unknown(self) -> None:
        title, blurb = reason_label("totally_unknown_reason_xyz")
        self.assertEqual(title, "totally_unknown_reason_xyz")
        self.assertIn("未知", blurb)

    def test_excerpt_finding_from_handoff_and_evidence(self) -> None:
        handoff = {
            "artifacts": {
                "risk_flags": [],
                "company_summary": {"status": "存续"},
            }
        }
        evidence = {
            "evidence": [
                {
                    "id": "e-risk",
                    "field": "risk",
                    "value": "高风险",
                    "source_ref": "raw/risk.json#/risk",
                }
            ]
        }
        finding = {
            "reason": "material_risk_omitted",
            "artifact_path": "artifacts.risk_flags",
            "evidence_refs": ["e-risk"],
        }
        block = excerpt_finding(finding, handoff, evidence)
        self.assertEqual(block["artifact_path"], "artifacts.risk_flags")
        self.assertEqual(block["artifact_value"], [])
        self.assertEqual(len(block["evidence"]), 1)
        self.assertEqual(block["evidence"][0]["id"], "e-risk")
        self.assertEqual(block["evidence"][0]["value"], "高风险")


class GateReviewDecisionTests(unittest.TestCase):
    def test_runtime_hash_stable(self) -> None:
        handoff = {"run_id": "r1", "artifacts": {"a": 1}}
        evidence = {"evidence": [], "claims": []}
        judge = {"decision": "needs_review", "confidence": 0.5, "findings": []}
        h1 = compute_runtime_gate_input_hash(handoff, evidence, judge)
        h2 = compute_runtime_gate_input_hash(handoff, evidence, judge)
        self.assertTrue(h1.startswith("sha256:"))
        self.assertEqual(h1, h2)
        judge2 = {**judge, "confidence": 0.51}
        self.assertNotEqual(h1, compute_runtime_gate_input_hash(handoff, evidence, judge2))

    def test_build_pending_decision_and_complete_validation(self) -> None:
        findings = [
            {
                "reason": "material_risk_omitted",
                "artifact_path": "artifacts.risk_flags",
                "evidence_refs": ["e-risk"],
            }
        ]
        tmpl = build_decision_template(findings, gate_input_hash="sha256:abc")
        self.assertEqual(tmpl["status"], "pending")
        self.assertIsNone(tmpl["action"])
        self.assertIsNone(tmpl["findings"][0]["verdict"])

        bad = dict(tmpl)
        bad["status"] = "completed"
        ok, errs = validate_decision(bad, expected_hash="sha256:abc")
        self.assertFalse(ok)
        self.assertTrue(any("verdict" in e or "action" in e for e in errs))

        good = {
            **tmpl,
            "status": "completed",
            "reviewed_at": "2026-07-14T00:00:00Z",
            "reviewer": "tester",
            "action": "reject_rerun",
            "action_reason": "确认遗漏高风险",
            "findings": [
                {
                    "reason": "material_risk_omitted",
                    "artifact_path": "artifacts.risk_flags",
                    "verdict": "confirm",
                    "note": "ok",
                }
            ],
        }
        ok, errs = validate_decision(good, expected_hash="sha256:abc")
        self.assertTrue(ok, errs)

        stale = {**good, "gate_input_hash": "sha256:old"}
        ok, errs = validate_decision(stale, expected_hash="sha256:abc")
        self.assertFalse(ok)


class GateReviewRenderTests(unittest.TestCase):
    def _ctx(self) -> dict:
        return {
            "subject_name": "杭州示例科技有限公司",
            "skill_id": "cws-company-profile",
            "decision": "needs_review",
            "run_id": "run-dataset",
            "case_id": None,
            "checked_at": "2026-07-14T00:00:00Z",
            "judge": {
                "decision": "needs_review",
                "confidence": 0.71,
                "model": "mock",
                "rubric_version": "company-profile-v1",
                "findings": [
                    {
                        "reason": "material_risk_omitted",
                        "artifact_path": "artifacts.risk_flags",
                        "evidence_refs": ["e-risk"],
                    }
                ],
            },
            "handoff": {"artifacts": {"risk_flags": []}},
            "evidence": {
                "evidence": [
                    {
                        "id": "e-risk",
                        "field": "risk",
                        "value": "高风险",
                        "source_ref": "raw/risk.json#/risk",
                    }
                ]
            },
            "expected_decision": None,
            "actual_decision": "needs_review",
        }

    def test_render_markdown_contains_sections(self) -> None:
        md = render_review_markdown(self._ctx())
        self.assertIn("# Gate 人工审核", md)
        self.assertIn("重大风险遗漏", md)
        self.assertIn("高风险", md)
        self.assertIn("review-decision.json", md)

    def test_write_review_pack_creates_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            paths = write_review_pack(out, self._ctx(), gate_input_hash="sha256:test")
            self.assertTrue(paths["review_md"].is_file())
            self.assertTrue(paths["decision_json"].is_file())
            decision = json.loads(paths["decision_json"].read_text(encoding="utf-8"))
            self.assertEqual(decision["status"], "pending")
            self.assertEqual(decision["gate_input_hash"], "sha256:test")

    def test_write_eval_review_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = {
                "case_id": "judge-material-risk-omitted",
                "actual_decision": "needs_review",
                "expected_decision": "needs_review",
                "input_hash": "sha256:casehash",
                "judge": self._ctx()["judge"],
                "review_context": {
                    "skill_id": "cws-company-profile",
                    "subject_name": "杭州示例科技有限公司",
                    "run_id": "run-dataset",
                    "handoff": self._ctx()["handoff"],
                    "evidence": self._ctx()["evidence"],
                    "judge": self._ctx()["judge"],
                },
            }
            paths = write_eval_review_pack(root, result)
            assert paths is not None
            self.assertTrue(paths["review_md"].is_file())
            self.assertTrue((root / "judge-material-risk-omitted" / "context.json").is_file())
            md = paths["review_md"].read_text(encoding="utf-8")
            self.assertIn("期望决策", md)


class GateReviewCliTests(unittest.TestCase):
    def test_cli_handoff_dir_writes_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "handoff.json").write_text(
                json.dumps(
                    {
                        "run_id": "r1",
                        "subject": {"name": "X"},
                        "artifacts": {"risk_flags": []},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (d / "evidence.json").write_text(
                json.dumps(
                    {
                        "evidence": [
                            {
                                "id": "e1",
                                "field": "risk",
                                "value": "高",
                                "source_ref": "raw/a.json#/r",
                            }
                        ],
                        "claims": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (d / "gate-result.json").write_text(
                json.dumps(
                    {
                        "decision": "needs_review",
                        "skill_id": "cws-company-profile",
                        "run_id": "r1",
                        "judge": {
                            "decision": "needs_review",
                            "confidence": 0.6,
                            "model": "mock",
                            "rubric_version": "company-profile-v1",
                            "findings": [
                                {
                                    "reason": "material_risk_omitted",
                                    "artifact_path": "artifacts.risk_flags",
                                    "evidence_refs": ["e1"],
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "render_gate_review.py"), "--handoff-dir", str(d)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((d / "review.md").is_file())
            self.assertTrue((d / "review-decision.json").is_file())

    def test_write_from_handoff_dir_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "handoff.json").write_text(
                json.dumps({"run_id": "r1", "subject": {"name": "Y"}, "artifacts": {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            (d / "gate-result.json").write_text(
                json.dumps(
                    {
                        "decision": "needs_review",
                        "skill_id": "cws-company-profile",
                        "run_id": "r1",
                        "judge": {"decision": "needs_review", "findings": []},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            paths = write_review_pack_from_handoff_dir(d)
            assert paths is not None
            self.assertTrue(paths["review_md"].is_file())


if __name__ == "__main__":
    unittest.main()
