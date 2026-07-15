#!/usr/bin/env python3
"""Integration checks for the standalone atomic skill loop."""

from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "cws-workflow" / "scripts" / "workflow_cli.py"
FIXTURE = ROOT / "tests" / "fixtures" / "gates" / "company-profile"


class AtomicLoopIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.company_kb = self.root / "company-kb"
        self.input_path = self.root / "input.json"
        self.input_path.write_text(
            json.dumps({"company_name": "杭州示例科技有限公司"}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env["CWS_COMPANY_KB_DIR"] = str(self.company_kb)
        self.env["CWS_JUDGE_MODE"] = "mock"
        self.run_id = "run-atomic-test"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def init(self, skill: str = "cws-company-profile", *extra: str) -> dict[str, object]:
        result = self.run_cli(
            "loop",
            "init",
            "--skill",
            skill,
            "--company",
            "杭州示例科技有限公司",
            "--run-id",
            self.run_id,
            "--input",
            str(self.input_path),
            *extra,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def write_parent_handoffs(self) -> None:
        run_dir = self.company_kb / "artifacts" / self.run_id
        for skill in (
            "cws-company-profile",
            "cws-shareholder-structure",
            "cws-litigation-risk",
            "cws-financing-history",
        ):
            directory = run_dir / skill
            directory.mkdir(parents=True)
            (directory / "handoff.json").write_text(
                json.dumps(
                    {
                        "run_id": self.run_id,
                        "subject": "杭州示例科技有限公司",
                        "artifacts": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (directory / "gate-result.json").write_text(
                json.dumps({"status": "passed"}), encoding="utf-8"
            )

    def write_pass_artifacts(self, attempt_dir: Path) -> None:
        handoff = json.loads((FIXTURE / "pass.handoff.json").read_text(encoding="utf-8"))
        handoff["run_id"] = self.run_id
        (attempt_dir / "handoff.json").write_text(
            json.dumps(handoff, ensure_ascii=False), encoding="utf-8"
        )
        evidence = json.loads((FIXTURE / "evidence.json").read_text(encoding="utf-8"))
        evidence["run_id"] = self.run_id
        (attempt_dir / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False), encoding="utf-8"
        )
        raw = attempt_dir / "raw" / "company.json"
        raw.parent.mkdir()
        shutil.copy2(FIXTURE / "raw" / "company.json", raw)

    def write_failing_artifacts(self, attempt_dir: Path) -> None:
        handoff = json.loads((FIXTURE / "missing-summary.handoff.json").read_text(encoding="utf-8"))
        handoff["run_id"] = self.run_id
        (attempt_dir / "handoff.json").write_text(
            json.dumps(handoff, ensure_ascii=False), encoding="utf-8"
        )
        evidence = json.loads((FIXTURE / "evidence.json").read_text(encoding="utf-8"))
        evidence["run_id"] = self.run_id
        (attempt_dir / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False), encoding="utf-8"
        )
        raw = attempt_dir / "raw" / "company.json"
        raw.parent.mkdir()
        shutil.copy2(FIXTURE / "raw" / "company.json", raw)

    def test_passing_attempt_is_promoted_without_removing_audit_copy(self) -> None:
        initialized = self.init()
        self.assertEqual("pending", initialized["status"])
        run_dir = self.company_kb / "artifacts" / self.run_id
        self.assertTrue((run_dir / "run-manifest.json").is_file())
        self.assertTrue((run_dir / "frozen" / "input.json").is_file())
        self.assertTrue(
            (run_dir / "frozen" / "skills" / "cws-company-profile" / "SKILL.md").is_file()
        )

        started = self.run_cli("loop", "next", "--run-id", self.run_id)
        self.assertEqual(0, started.returncode, started.stderr)
        claim = json.loads(started.stdout)
        self.assertEqual("execute", claim["action"])
        attempt_dir = Path(claim["attempt_dir"])
        self.write_pass_artifacts(attempt_dir)

        completed = self.run_cli(
            "loop",
            "complete",
            "--run-id",
            self.run_id,
            "--lease-id",
            claim["lease_id"],
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("passed", result["status"])
        self.assertEqual("accept", result["next_action"])

        formal = run_dir / "cws-company-profile"
        self.assertTrue((formal / "handoff.json").is_file())
        self.assertTrue((formal / "gate-result.json").is_file())
        self.assertTrue((attempt_dir / "handoff.json").is_file())

    def test_repeated_repairable_findings_stop_after_second_attempt(self) -> None:
        self.init()
        first_claim = json.loads(
            self.run_cli("loop", "next", "--run-id", self.run_id).stdout
        )
        self.write_failing_artifacts(Path(first_claim["attempt_dir"]))
        first = self.run_cli(
            "loop",
            "complete",
            "--run-id",
            self.run_id,
            "--lease-id",
            first_claim["lease_id"],
        )
        self.assertEqual(1, first.returncode)
        first_result = json.loads(first.stdout)
        self.assertEqual("retryable", first_result["status"])
        self.assertEqual("revise", first_result["next_action"])
        self.assertTrue(first_result["findings"])

        second_claim = self.run_cli("loop", "next", "--run-id", self.run_id)
        self.assertEqual(0, second_claim.returncode, second_claim.stderr)
        second_payload = json.loads(second_claim.stdout)
        self.assertEqual("revise", second_payload["action"])
        self.write_failing_artifacts(Path(second_payload["attempt_dir"]))
        second = self.run_cli(
            "loop",
            "complete",
            "--run-id",
            self.run_id,
            "--lease-id",
            second_payload["lease_id"],
        )
        self.assertEqual(1, second.returncode)
        second_result = json.loads(second.stdout)
        self.assertEqual("exhausted", second_result["status"])
        self.assertEqual("exhausted", second_result["next_action"])

    def test_final_gate_reads_report_from_candidate_before_promotion(self) -> None:
        self.init("cws-due-diligence")
        self.write_parent_handoffs()
        claim = json.loads(self.run_cli("loop", "next", "--run-id", self.run_id).stdout)
        attempt_dir = Path(claim["attempt_dir"])
        report_fixture = ROOT / "tests" / "fixtures" / "gates" / "due-diligence"
        handoff = json.loads((report_fixture / "pass.handoff.json").read_text(encoding="utf-8"))
        handoff["run_id"] = self.run_id
        (attempt_dir / "handoff.json").write_text(
            json.dumps(handoff, ensure_ascii=False), encoding="utf-8"
        )
        evidence = json.loads((report_fixture / "evidence.json").read_text(encoding="utf-8"))
        evidence["run_id"] = self.run_id
        (attempt_dir / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False), encoding="utf-8"
        )
        raw = attempt_dir / "raw" / "report.json"
        raw.parent.mkdir()
        shutil.copy2(report_fixture / "raw" / "report.json", raw)

        completed = self.run_cli(
            "loop",
            "complete",
            "--run-id",
            self.run_id,
            "--lease-id",
            claim["lease_id"],
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("final", result["gate"])
        self.assertTrue(
            (self.company_kb / "artifacts" / self.run_id / "cws-due-diligence" / "handoff.json").is_file()
        )

    def test_missing_final_parent_stops_as_upstream_contract_gap(self) -> None:
        self.init("cws-due-diligence")
        claim = json.loads(self.run_cli("loop", "next", "--run-id", self.run_id).stdout)
        attempt_dir = Path(claim["attempt_dir"])
        report_fixture = ROOT / "tests" / "fixtures" / "gates" / "due-diligence"
        handoff = json.loads((report_fixture / "pass.handoff.json").read_text(encoding="utf-8"))
        handoff["run_id"] = self.run_id
        (attempt_dir / "handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
        evidence = json.loads((report_fixture / "evidence.json").read_text(encoding="utf-8"))
        evidence["run_id"] = self.run_id
        (attempt_dir / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        raw = attempt_dir / "raw" / "report.json"
        raw.parent.mkdir()
        shutil.copy2(report_fixture / "raw" / "report.json", raw)

        completed = self.run_cli(
            "loop",
            "complete",
            "--run-id",
            self.run_id,
            "--lease-id",
            claim["lease_id"],
        )
        self.assertEqual(1, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("upstream_contract_gap", result["next_action"])
        self.assertFalse(
            (self.company_kb / "artifacts" / self.run_id / "cws-due-diligence").exists()
        )

    def test_wrong_lease_cannot_complete_active_attempt(self) -> None:
        self.init()
        claim = json.loads(self.run_cli("loop", "next", "--run-id", self.run_id).stdout)
        self.write_pass_artifacts(Path(claim["attempt_dir"]))

        completed = self.run_cli(
            "loop",
            "complete",
            "--run-id",
            self.run_id,
            "--lease-id",
            "lease-wrong",
        )
        self.assertEqual(1, completed.returncode)
        self.assertIn("lease-id does not match", completed.stderr)
        status = json.loads(self.run_cli("loop", "status", "--run-id", self.run_id).stdout)
        self.assertEqual("running", status["status"])

    def test_changed_frozen_skill_stops_before_gate(self) -> None:
        self.init()
        claim = json.loads(self.run_cli("loop", "next", "--run-id", self.run_id).stdout)
        self.write_pass_artifacts(Path(claim["attempt_dir"]))
        frozen_skill = (
            self.company_kb
            / "artifacts"
            / self.run_id
            / "frozen"
            / "skills"
            / "cws-company-profile"
            / "SKILL.md"
        )
        frozen_skill.write_text(frozen_skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

        completed = self.run_cli(
            "loop",
            "complete",
            "--run-id",
            self.run_id,
            "--lease-id",
            claim["lease_id"],
        )
        self.assertEqual(1, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("needs_review", result["status"])
        self.assertEqual("frozen_revision_changed", result["findings"][0]["reason"])

    def test_cancel_is_terminal_and_preserves_attempt(self) -> None:
        self.init()
        claim = json.loads(self.run_cli("loop", "next", "--run-id", self.run_id).stdout)
        cancelled = self.run_cli(
            "loop", "cancel", "--run-id", self.run_id, "--reason", "operator requested"
        )
        self.assertEqual(0, cancelled.returncode, cancelled.stderr)
        self.assertEqual("cancelled", json.loads(cancelled.stdout)["status"])
        self.assertTrue(Path(claim["attempt_dir"]).is_dir())

        restarted = self.run_cli("loop", "next", "--run-id", self.run_id)
        self.assertEqual(1, restarted.returncode)
        self.assertIn("status cancelled", restarted.stderr)

    def test_only_one_concurrent_next_claims_the_attempt(self) -> None:
        self.init()
        command = [sys.executable, str(SCRIPT), "loop", "next", "--run-id", self.run_id]
        processes = [
            subprocess.Popen(
                command,
                cwd=ROOT,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = [process.communicate() + (process.returncode,) for process in processes]

        self.assertEqual([0, 1], sorted(item[2] for item in results))
        failed = next(item for item in results if item[2] == 1)
        self.assertIn("status running", failed[1])
        attempts = self.company_kb / "artifacts" / self.run_id / "attempts" / "cws-company-profile"
        self.assertEqual(["1"], sorted(path.name for path in attempts.iterdir()))

    def test_expired_lease_is_preserved_and_replaced_by_new_attempt(self) -> None:
        self.init("cws-company-profile", "--lease-seconds", "1")
        first = json.loads(self.run_cli("loop", "next", "--run-id", self.run_id).stdout)
        time.sleep(1.1)

        second = self.run_cli("loop", "next", "--run-id", self.run_id)
        self.assertEqual(0, second.returncode, second.stderr)
        replacement = json.loads(second.stdout)
        self.assertEqual(2, replacement["attempt"])
        self.assertEqual("revise", replacement["action"])
        self.assertTrue(Path(first["attempt_dir"]).is_dir())
        self.assertNotEqual(first["lease_id"], replacement["lease_id"])

    def test_existing_formal_artifact_is_not_overwritten(self) -> None:
        self.init()
        claim = json.loads(self.run_cli("loop", "next", "--run-id", self.run_id).stdout)
        self.write_pass_artifacts(Path(claim["attempt_dir"]))
        formal = (
            self.company_kb / "artifacts" / self.run_id / "cws-company-profile"
        )
        formal.mkdir()
        sentinel = formal / "existing.txt"
        sentinel.write_text("keep", encoding="utf-8")

        completed = self.run_cli(
            "loop",
            "complete",
            "--run-id",
            self.run_id,
            "--lease-id",
            claim["lease_id"],
        )
        self.assertEqual(1, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("needs_review", result["status"])
        self.assertEqual("formal_artifact_exists", result["findings"][0]["reason"])
        self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    def test_kanban_claim_allocates_attempt_and_publishes_current_context(self) -> None:
        self.init()
        workflow_scripts = ROOT / "skills" / "cws-workflow" / "scripts"
        sys.path.insert(0, str(workflow_scripts))
        try:
            from atomic_loop import bind_kanban_task

            bind_kanban_task(self.company_kb, self.run_id, "atomic", "h1")
            gate_path = ROOT / "scripts" / "kanban_gate.py"
            spec = importlib.util.spec_from_file_location("kanban_gate_test", gate_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            gate = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gate)
            claim = gate.claim_loop(
                "h1",
                f"cws_loop: run_id={self.run_id} node=atomic",
                company_kb=self.company_kb,
            )
        finally:
            sys.path.remove(str(workflow_scripts))

        self.assertEqual(1, claim["attempt"])
        current = (
            self.company_kb
            / "artifacts"
            / self.run_id
            / "contexts"
            / "atomic.json"
        )
        self.assertEqual(
            claim["maker_context_path"],
            json.loads(current.read_text(encoding="utf-8"))["maker_context_path"],
        )

    def test_kanban_completion_requeues_same_task_then_accepts_repair(self) -> None:
        self.init()
        workflow_scripts = ROOT / "skills" / "cws-workflow" / "scripts"
        sys.path.insert(0, str(workflow_scripts))
        try:
            from atomic_loop import bind_kanban_task

            bind_kanban_task(self.company_kb, self.run_id, "atomic", "h1")
            gate_path = ROOT / "scripts" / "kanban_gate.py"
            spec = importlib.util.spec_from_file_location("kanban_gate_retry_test", gate_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            gate = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gate)
            body = f"cws_loop: run_id={self.run_id} node=atomic"
            first = gate.claim_loop("h1", body, company_kb=self.company_kb)
            self.write_failing_artifacts(Path(first["attempt_dir"]))
            requeued: list[str] = []
            with mock.patch.dict(os.environ, {"CWS_JUDGE_MODE": "mock"}):
                failed = gate.complete_loop(
                    "h1",
                    body,
                    company_kb=self.company_kb,
                    requeue=lambda task_id, reason: requeued.append(task_id) or True,
                )
            second = gate.claim_loop("h1", body, company_kb=self.company_kb)
            self.write_pass_artifacts(Path(second["attempt_dir"]))
            with mock.patch.dict(os.environ, {"CWS_JUDGE_MODE": "mock"}):
                passed = gate.complete_loop(
                    "h1",
                    body,
                    company_kb=self.company_kb,
                    requeue=lambda task_id, reason: self.fail(
                        "passed attempt requeued"
                    ),
                )
        finally:
            sys.path.remove(str(workflow_scripts))

        self.assertEqual("retryable", failed["status"])
        self.assertEqual(["h1"], requeued)
        self.assertEqual(2, second["attempt"])
        self.assertEqual("passed", passed["status"])


if __name__ == "__main__":
    unittest.main()
