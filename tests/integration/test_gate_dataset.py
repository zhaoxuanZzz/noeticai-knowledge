#!/usr/bin/env python3
"""Integration checks for the offline gate dataset evaluator."""

from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVALUATOR = ROOT / "scripts" / "evaluate_gate_dataset.py"
REFRESHER = ROOT / "scripts" / "refresh_gate_dataset.py"
IMPORTER = ROOT / "scripts" / "import_gate_kb_snapshots.py"
PROMOTER = ROOT / "scripts" / "promote_gate_dataset.py"
DATASET = ROOT / "tests" / "fixtures" / "gate-dataset"


def run_command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class GateDatasetIntegrationTest(unittest.TestCase):
    def write_import_fixture(self, root: Path) -> tuple[Path, Path]:
        kb = root / "kb"
        raw = kb / "raw" / "示例公司" / "registration.json"
        raw.parent.mkdir(parents=True)
        raw.write_text(json.dumps({
            "result": {
                "name": "示例公司",
                "credit_code": "91330100REAL000001",
                "status": "存续",
                "legal_representative": "张三",
            },
            "ignored": "not promoted",
        }, ensure_ascii=False), encoding="utf-8")
        wiki = kb / "wiki" / "示例公司" / "企业画像.md"
        wiki.parent.mkdir(parents=True)
        wiki.write_text("# 示例公司\n", encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({"companies": [{
            "case_id": "example-company",
            "subject": {
                "name": "示例公司",
                "unified_social_credit_code": "91330100REAL000001",
            },
            "sources": [{
                "source_id": "registration",
                "file": "raw/示例公司/registration.json",
                "observed_at": "2026-07-10",
                "facts": {
                    "name": "/result/name",
                    "unified_social_credit_code": "/result/credit_code",
                    "status": "/result/status",
                },
            }],
            "wiki": ["wiki/示例公司/企业画像.md"],
        }]}, ensure_ascii=False), encoding="utf-8")
        return kb, manifest

    def test_importer_creates_minimal_read_only_review_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kb, manifest = self.write_import_fixture(root)
            before = (kb / "raw" / "示例公司" / "registration.json").read_bytes()
            output = root / "staging"
            result = run_command(
                str(IMPORTER), "--kb-root", str(kb),
                "--manifest", str(manifest), "--output", str(output),
            )
            snapshot = json.loads(
                (output / "companies" / "example-company" / "kb" / "raw" /
                 "示例公司" / "registration.json").read_text()
            )
            review = json.loads((output / "review.json").read_text())
            catalog = json.loads((output / "source-catalog.json").read_text())
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("存续", snapshot["facts"]["status"])
            self.assertNotIn("ignored", json.dumps(snapshot, ensure_ascii=False))
            self.assertEqual(before, (kb / "raw" / "示例公司" / "registration.json").read_bytes())
            self.assertEqual("pending", review["cases"][0]["review_status"])
            self.assertEqual("sha256:", catalog["sources"][0]["source_hash"][:7])
            self.assertTrue(
                (output / "companies" / "example-company" / "kb" / "wiki" /
                 "示例公司" / "企业画像.md").is_file()
            )

    def test_importer_rejects_sensitive_source_without_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kb, manifest = self.write_import_fixture(root)
            source = kb / "raw" / "示例公司" / "registration.json"
            payload = json.loads(source.read_text())
            payload["meta"] = {"access_token": "secret"}
            source.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "staging"
            result = run_command(
                str(IMPORTER), "--kb-root", str(kb),
                "--manifest", str(manifest), "--output", str(output),
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("sensitive field", result.stderr)
            self.assertFalse(output.exists())

    def test_importer_rejects_missing_pointer_script_and_nonempty_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kb, manifest = self.write_import_fixture(root)
            payload = json.loads(manifest.read_text())
            payload["companies"][0]["sources"][0]["facts"]["status"] = "/result/missing"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            missing = run_command(
                str(IMPORTER), "--kb-root", str(kb),
                "--manifest", str(manifest), "--output", str(root / "missing"),
            )
            payload["companies"][0]["sources"][0]["file"] = "raw/示例公司/query.py"
            (kb / "raw" / "示例公司" / "query.py").write_text("print('x')", encoding="utf-8")
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            script = run_command(
                str(IMPORTER), "--kb-root", str(kb),
                "--manifest", str(manifest), "--output", str(root / "script"),
            )
            output = root / "nonempty"
            output.mkdir()
            (output / "keep").write_text("x", encoding="utf-8")
            nonempty = run_command(
                str(IMPORTER), "--kb-root", str(kb),
                "--manifest", str(manifest), "--output", str(output),
            )

        self.assertEqual(2, missing.returncode)
        self.assertIn("JSON pointer", missing.stderr)
        self.assertEqual(2, script.returncode)
        self.assertIn("JSON source", script.stderr)
        self.assertEqual(2, nonempty.returncode)
        self.assertIn("output must be empty", nonempty.stderr)

    def write_promotion_fixture(self, root: Path, approved: bool = True) -> tuple[Path, Path]:
        staging = root / "staging"
        bundle = staging / "bundles" / "real-example"
        raw = bundle / "raw" / "company.json"
        raw.parent.mkdir(parents=True)
        raw.write_text('{"status":"存续"}\n', encoding="utf-8")
        (bundle / "handoff.json").write_text("{}\n", encoding="utf-8")
        (bundle / "evidence.json").write_text("{}\n", encoding="utf-8")
        digest = "sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest()
        review = {
            "schema_version": "cws-gate-review/v1",
            "cases": [{
                "case_id": "real-example",
                "review_status": "approved" if approved else "pending",
                "bundle": "bundles/real-example",
                "case": {
                    "kind": "node",
                    "skill_id": "cws-company-profile",
                    "run_id": "run-real-example",
                    "handoff": "handoff.json",
                    "evidence": "evidence.json",
                    "case_root": ".",
                    "source_hashes": {"raw/company.json": digest},
                    "expected_decision": "passed",
                    "quality_state": "complete",
                    "expected_reasons": [],
                    "current_decision": "passed",
                    "current_reasons": [],
                    "tags": ["business", "real-snapshot"],
                },
            }],
        }
        (staging / "review.json").write_text(json.dumps(review), encoding="utf-8")
        dataset = root / "dataset"
        dataset.mkdir()
        (dataset / "cases.json").write_text('{"cases":[]}\n', encoding="utf-8")
        return staging, dataset

    def test_promoter_copies_only_approved_validated_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            staging, dataset = self.write_promotion_fixture(Path(temp))
            review = json.loads((staging / "review.json").read_text())
            review["cases"][0]["bundle_id"] = "example-bundle"
            second = json.loads(json.dumps(review["cases"][0]))
            second["case_id"] = "real-example-final"
            second["case"]["kind"] = "final"
            second["case"]["skill_id"] = "cws-due-diligence"
            second["case"].pop("handoff")
            second["case"]["run_dir"] = "."
            review["cases"].append(second)
            (staging / "review.json").write_text(json.dumps(review), encoding="utf-8")
            result = run_command(
                str(PROMOTER), "--staging", str(staging), "--dataset", str(dataset)
            )
            cases = json.loads((dataset / "cases.json").read_text())["cases"]

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(2, len(cases))
            self.assertEqual("real-example", cases[0]["case_id"])
            self.assertEqual(
                "business/real/example-bundle/handoff.json", cases[0]["handoff"]
            )
            self.assertTrue(
                (dataset / "business" / "real" / "example-bundle" / "raw" / "company.json").is_file()
            )

    def test_promoter_rejects_unapproved_hash_drift_and_duplicate_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging, dataset = self.write_promotion_fixture(root, approved=False)
            before = (dataset / "cases.json").read_bytes()
            unapproved = run_command(
                str(PROMOTER), "--staging", str(staging), "--dataset", str(dataset)
            )
            self.assertEqual(before, (dataset / "cases.json").read_bytes())
            self.assertFalse((dataset / "business").exists())

            review = json.loads((staging / "review.json").read_text())
            review["cases"][0]["review_status"] = "approved"
            review["cases"][0]["case"]["source_hashes"]["raw/company.json"] = "sha256:bad"
            (staging / "review.json").write_text(json.dumps(review), encoding="utf-8")
            drift = run_command(
                str(PROMOTER), "--staging", str(staging), "--dataset", str(dataset)
            )
            self.assertEqual(before, (dataset / "cases.json").read_bytes())

            review["cases"][0]["case"]["source_hashes"]["raw/company.json"] = (
                "sha256:" + hashlib.sha256(
                    (staging / "bundles" / "real-example" / "raw" / "company.json").read_bytes()
                ).hexdigest()
            )
            (staging / "review.json").write_text(json.dumps(review), encoding="utf-8")
            (dataset / "cases.json").write_text(
                '{"cases":[{"case_id":"real-example"}]}\n', encoding="utf-8"
            )
            duplicate_before = (dataset / "cases.json").read_bytes()
            duplicate = run_command(
                str(PROMOTER), "--staging", str(staging), "--dataset", str(dataset)
            )
            self.assertEqual(2, unapproved.returncode)
            self.assertIn("not approved", unapproved.stderr)
            self.assertEqual(2, drift.returncode)
            self.assertIn("hash mismatch", drift.stderr)
            self.assertEqual(2, duplicate.returncode)
            self.assertIn("duplicate case_id", duplicate.stderr)
            self.assertEqual(duplicate_before, (dataset / "cases.json").read_bytes())
    def test_ci_profile_replays_deterministic_cases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = run_command(
                str(EVALUATOR),
                "--dataset",
                str(DATASET),
                "--profile",
                "ci",
                "--output",
                temp,
            )
            payload = json.loads((Path(temp) / "results.json").read_text(encoding="utf-8"))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertGreaterEqual(payload["summary"]["total"], 6)
        self.assertEqual(0, payload["summary"]["unexpected_regressions"])
        self.assertGreaterEqual(payload["summary"]["not_run"], 1)

    def test_semantic_profile_uses_jsonl_adapter(self) -> None:
        adapter_source = """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    final_case = request['case_id'].startswith('final-')
    if final_case and not request['input']['parent_handoffs']:
        raise SystemExit(8)
    print(json.dumps({
        'protocol_version': request['protocol_version'],
        'request_id': request['request_id'],
        'decision': 'needs_review',
        'confidence': 0.8,
        'model': 'fixture-judge',
        'rubric_version': request['rubric_id'],
        'findings': [{
            'reason': 'material_risk_omitted',
            'artifact_path': ('artifacts.litigation_and_operating_risks' if final_case else 'artifacts.risk_flags'),
            'evidence_refs': ['e-risk']
        }]
    }), flush=True)
"""
        with tempfile.TemporaryDirectory() as temp:
            adapter = Path(temp) / "judge"
            adapter.write_text(adapter_source, encoding="utf-8")
            adapter.chmod(adapter.stat().st_mode | stat.S_IXUSR)
            output = Path(temp) / "output"
            result = run_command(
                str(EVALUATOR),
                "--dataset",
                str(DATASET),
                "--profile",
                "semantic",
                "--judge-adapter",
                str(adapter),
                "--output",
                str(output),
            )
            payload = json.loads((output / "results.json").read_text(encoding="utf-8"))

        self.assertEqual(0, result.returncode, result.stderr)
        judged = [case for case in payload["cases"] if case.get("judge")]
        self.assertGreaterEqual(len(judged), 2)
        self.assertEqual("fixture-judge", judged[0]["judge"]["model"])

    def test_adapter_must_be_absolute_executable(self) -> None:
        result = run_command(
            str(EVALUATOR),
            "--dataset",
            str(DATASET),
            "--profile",
            "semantic",
            "--judge-adapter",
            "judge",
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("absolute", result.stderr)

    def test_source_hash_mismatch_is_dataset_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset = Path(temp) / "dataset"
            dataset.mkdir()
            (dataset / "cases.json").write_text(json.dumps({"cases": [{
                "case_id": "bad-hash",
                "kind": "node",
                "skill_id": "cws-company-profile",
                "run_id": "r1",
                "handoff": "handoff.json",
                "evidence": "evidence.json",
                "source_hashes": {"raw.json": "sha256:deadbeef"},
                "expected_decision": "blocked",
                "quality_state": "invalid",
                "expected_reasons": ["evidence_path_invalid"],
                "tags": ["hash"]
            }]}), encoding="utf-8")
            for name in ("handoff.json", "evidence.json", "raw.json"):
                (dataset / name).write_text("{}", encoding="utf-8")
            result = run_command(
                str(EVALUATOR), "--dataset", str(dataset), "--profile", "ci"
            )

        self.assertEqual(2, result.returncode)
        self.assertIn("hash mismatch", result.stderr)

    def test_judge_request_does_not_leak_expected_labels(self) -> None:
        adapter_source = """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    serialized = json.dumps(request)
    if 'expected_decision' in serialized or 'expected_reasons' in serialized:
        raise SystemExit(9)
    final_case = request['case_id'].startswith('final-')
    if final_case and not request['input']['parent_handoffs']:
        raise SystemExit(8)
    print(json.dumps({
        'protocol_version': request['protocol_version'],
        'request_id': request['request_id'],
        'decision': 'needs_review', 'confidence': 0.7,
        'model': 'no-leak', 'rubric_version': request['rubric_id'],
        'findings': [{'reason': 'material_risk_omitted',
          'artifact_path': ('artifacts.litigation_and_operating_risks' if final_case else 'artifacts.risk_flags'), 'evidence_refs': ['e-risk']}]
    }), flush=True)
"""
        with tempfile.TemporaryDirectory() as temp:
            adapter = Path(temp) / "judge"
            adapter.write_text(adapter_source, encoding="utf-8")
            adapter.chmod(adapter.stat().st_mode | stat.S_IXUSR)
            result = run_command(
                str(EVALUATOR), "--dataset", str(DATASET), "--profile", "semantic",
                "--judge-adapter", str(adapter), "--output", str(Path(temp) / "out")
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_refresh_writes_review_only_staging_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "staging"
            result = run_command(
                str(REFRESHER),
                "--manifest",
                str(DATASET / "refresh" / "companies.json"),
                "--output",
                str(output),
            )
            report = json.loads(
                (output / "refresh-report.json").read_text(encoding="utf-8")
            )
            replay = run_command(
                str(EVALUATOR),
                "--dataset",
                str(output),
                "--baseline",
                str(DATASET),
                "--profile",
                "ci",
                "--output",
                str(Path(temp) / "evaluation"),
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(0, replay.returncode, replay.stderr)
        self.assertEqual("candidate", report["entries"][0]["status"])
        self.assertTrue(report["entries"][0]["requires_human_review"])
        self.assertEqual("awaiting_capture", report["entries"][1]["status"])

    def test_refresh_rejects_nested_secret_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "snapshot.json").write_text(
                '{"result":{"status":"存续"},"meta":{"access_token":"secret"}}',
                encoding="utf-8",
            )
            (root / "manifest.json").write_text(
                '{"companies":[{"case_id":"unsafe","snapshot_file":"snapshot.json"}]}',
                encoding="utf-8",
            )
            result = run_command(
                str(REFRESHER),
                "--manifest",
                str(root / "manifest.json"),
                "--output",
                str(root / "staging"),
            )
        self.assertEqual(2, result.returncode)
        self.assertIn("sensitive field", result.stderr)

    def test_capability_gap_expansion_fails_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset = Path(temp) / "dataset"
            dataset.mkdir()
            handoff = json.loads(
                (DATASET / "schema" / "base" / "company-profile.handoff.json").read_text(
                    encoding="utf-8"
                )
            )
            del handoff["artifacts"]["company_summary"]
            (dataset / "handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
            case = {
                "case_id": "known-gap",
                "kind": "node",
                "skill_id": "cws-company-profile",
                "run_id": "run-dataset",
                "handoff": "handoff.json",
                "expected_decision": "passed",
                "quality_state": "invalid",
                "expected_reasons": [],
                "tags": ["capability-gap"],
                "capability_gap": True,
                "current_decision": "blocked",
                "current_reasons": ["required_output_missing"],
            }
            (dataset / "cases.json").write_text(
                json.dumps({"cases": [case]}), encoding="utf-8"
            )
            known = run_command(
                str(EVALUATOR), "--dataset", str(dataset), "--profile", "ci",
                "--output", str(Path(temp) / "known")
            )
            case["current_reasons"] = []
            (dataset / "cases.json").write_text(
                json.dumps({"cases": [case]}), encoding="utf-8"
            )
            expanded = run_command(
                str(EVALUATOR), "--dataset", str(dataset), "--profile", "ci",
                "--output", str(Path(temp) / "expanded")
            )
        self.assertEqual(0, known.returncode, known.stderr)
        self.assertEqual(1, expanded.returncode, expanded.stderr)

    def test_write_reports_emits_review_pack(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        from evaluate_gate_dataset import _write_reports

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            results = [
                {
                    "case_id": "judge-material-risk-omitted",
                    "kind": "node",
                    "tags": ["judge"],
                    "expected_decision": "needs_review",
                    "actual_decision": "needs_review",
                    "reasons": ["material_risk_omitted"],
                    "capability_gap": False,
                    "known_gap": False,
                    "unexpected": False,
                    "checker_version": "test",
                    "input_hash": "sha256:casehash",
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
                    "review_context": {
                        "skill_id": "cws-company-profile",
                        "subject_name": "杭州示例科技有限公司",
                        "run_id": "run-dataset",
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
                        "judge": {
                            "decision": "needs_review",
                            "findings": [
                                {
                                    "reason": "material_risk_omitted",
                                    "artifact_path": "artifacts.risk_flags",
                                    "evidence_refs": ["e-risk"],
                                }
                            ],
                        },
                    },
                    "duration_ms": 1.0,
                }
            ]
            _write_reports(out, "semantic", results, [])
            review = out / "reviews" / "judge-material-risk-omitted" / "review.md"
            self.assertTrue(review.is_file())
            report = (out / "report.md").read_text(encoding="utf-8")
            self.assertIn("reviews/judge-material-risk-omitted/review.md", report)
            needs = json.loads((out / "needs-review.json").read_text(encoding="utf-8"))
            self.assertNotIn("review_context", needs[0])


if __name__ == "__main__":
    unittest.main()
