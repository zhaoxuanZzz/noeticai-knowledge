#!/usr/bin/env python3
"""Integration checks for the cross-host delegate gate runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "cws-workflow" / "scripts" / "workflow_cli.py"
PASS_HANDOFF = ROOT / "tests" / "fixtures" / "gates" / "company-profile" / "pass.handoff.json"
PASS_EVIDENCE = ROOT / "tests" / "fixtures" / "gates" / "company-profile" / "evidence.json"
PASS_RAW = ROOT / "tests" / "fixtures" / "gates" / "company-profile" / "raw" / "company.json"


class DelegateRunnerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.company_kb = Path(self.temporary.name) / "company-kb"
        self.env = os.environ.copy()
        self.env["CWS_COMPANY_KB_DIR"] = str(self.company_kb)
        self.env.setdefault("CWS_JUDGE_MODE", "mock")
        self.run_id = "run-delegate-test"

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

    def init(self, *extra: str) -> dict[str, object]:
        result = self.run_cli(
            "delegate",
            "init",
            "--skill",
            "cws-due-diligence",
            "--company",
            "杭州XX科技有限公司",
            "--run-id",
            self.run_id,
            *extra,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def write_profile_artifacts(self) -> Path:
        directory = self.company_kb / "artifacts" / self.run_id / "cws-company-profile"
        directory.mkdir(parents=True, exist_ok=True)
        handoff = directory / "handoff.json"
        data = json.loads(PASS_HANDOFF.read_text(encoding="utf-8"))
        data["run_id"] = self.run_id
        handoff.write_text(json.dumps(data), encoding="utf-8")
        evidence = json.loads(PASS_EVIDENCE.read_text(encoding="utf-8"))
        evidence["run_id"] = self.run_id
        (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        raw = directory / "raw" / "company.json"
        raw.parent.mkdir()
        raw.write_text(PASS_RAW.read_text(encoding="utf-8"), encoding="utf-8")
        return handoff

    def write_profile_attempt(self, directory: Path) -> None:
        data = json.loads(PASS_HANDOFF.read_text(encoding="utf-8"))
        data["run_id"] = self.run_id
        (directory / "handoff.json").write_text(json.dumps(data), encoding="utf-8")
        evidence = json.loads(PASS_EVIDENCE.read_text(encoding="utf-8"))
        evidence["run_id"] = self.run_id
        (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        raw = directory / "raw" / "company.json"
        raw.parent.mkdir()
        raw.write_text(PASS_RAW.read_text(encoding="utf-8"), encoding="utf-8")

    def test_init_persists_structured_plan_and_returns_first_ready_node(self) -> None:
        output = self.init()

        self.assertEqual(self.run_id, output["run_id"])
        self.assertEqual(["task1"], output["ready"])
        state_path = self.company_kb / "artifacts" / self.run_id / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        first = state["nodes"]["task1"]
        self.assertEqual("pending", first["status"])
        self.assertEqual("cws-company-profile", first["skill"])
        self.assertNotIn("check_artifact_gate.py", first["prompt"])
        self.assertEqual(self.run_id, first["node_gate"]["run_id"])
        self.assertTrue(first["handoff_path"].endswith("cws-company-profile/handoff.json"))
        self.assertIsNone(first["final_gate"])
        self.assertEqual("cws-due-diligence", state["nodes"]["task5"]["final_gate"]["skill"])

    def test_frozen_kb_is_persisted_in_delegate_state(self) -> None:
        self.init("--frozen-kb")

        state_path = self.company_kb / "artifacts" / self.run_id / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["frozen_kb"])
        self.assertTrue(all(node["frozen_kb"] for node in state["nodes"].values()))
        self.assertIn("冻结知识库模式", state["nodes"]["task1"]["prompt"])

    def test_missing_handoff_blocks_completion_and_keeps_children_pending(self) -> None:
        self.init()
        started = self.run_cli("delegate", "start", "--run-id", self.run_id, "--node", "task1")
        self.assertEqual(0, started.returncode, started.stderr)

        completed = self.run_cli("delegate", "complete", "--run-id", self.run_id, "--node", "task1")

        self.assertEqual(1, completed.returncode)
        output = json.loads(completed.stdout)
        self.assertEqual("blocked", output["status"])
        status = self.run_cli("delegate", "status", "--run-id", self.run_id)
        state = json.loads(status.stdout)
        self.assertEqual("blocked", state["nodes"]["task1"]["status"])
        self.assertEqual([], state["ready"])

    def test_passed_gate_unlocks_parallel_children_and_complete_is_idempotent(self) -> None:
        self.init()
        self.run_cli("delegate", "start", "--run-id", self.run_id, "--node", "task1")
        self.write_profile_artifacts()

        completed = self.run_cli("delegate", "complete", "--run-id", self.run_id, "--node", "task1")
        self.assertEqual(0, completed.returncode, completed.stderr)
        first_result = json.loads(completed.stdout)
        self.assertEqual("passed", first_result["status"])

        ready = self.run_cli("delegate", "ready", "--run-id", self.run_id)
        self.assertEqual(0, ready.returncode, ready.stderr)
        self.assertEqual(["task2", "task3", "task4"], json.loads(ready.stdout)["ready"])

        repeated = self.run_cli("delegate", "complete", "--run-id", self.run_id, "--node", "task1")
        self.assertEqual(first_result, json.loads(repeated.stdout))

    def test_blocked_node_can_be_repaired_and_revalidated_with_a_new_gate_attempt(self) -> None:
        self.init()
        self.run_cli("delegate", "start", "--run-id", self.run_id, "--node", "task1")
        blocked = self.run_cli("delegate", "complete", "--run-id", self.run_id, "--node", "task1")
        self.assertEqual(1, blocked.returncode)

        handoff = self.write_profile_artifacts()
        repaired = self.run_cli("delegate", "complete", "--run-id", self.run_id, "--node", "task1")

        self.assertEqual(0, repaired.returncode, repaired.stderr)
        self.assertEqual(2, json.loads(repaired.stdout)["attempt"])
        result_dir = handoff.parent
        self.assertTrue((result_dir / "gate-result-1.json").is_file())
        self.assertTrue((result_dir / "gate-result-2.json").is_file())

    def test_failed_child_can_be_recorded_and_started_again(self) -> None:
        self.init()
        self.run_cli("delegate", "start", "--run-id", self.run_id, "--node", "task1")
        failed = self.run_cli(
            "delegate", "fail", "--run-id", self.run_id, "--node", "task1", "--reason", "child crashed"
        )
        self.assertEqual(0, failed.returncode, failed.stderr)
        restarted = self.run_cli("delegate", "start", "--run-id", self.run_id, "--node", "task1")
        self.assertEqual(2, json.loads(restarted.stdout)["attempt"])

    def test_loop_enabled_delegate_uses_attempt_path_and_promotes_on_pass(self) -> None:
        self.init("--loop")
        started = self.run_cli("delegate", "start", "--run-id", self.run_id, "--node", "task1")
        self.assertEqual(0, started.returncode, started.stderr)
        claim = json.loads(started.stdout)
        self.assertEqual("execute", claim["action"])
        attempt_dir = Path(claim["attempt_dir"])
        self.write_profile_attempt(attempt_dir)

        completed = self.run_cli(
            "delegate",
            "complete",
            "--run-id",
            self.run_id,
            "--node",
            "task1",
            "--lease-id",
            claim["lease_id"],
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("passed", result["status"])
        self.assertTrue(
            (
                self.company_kb
                / "artifacts"
                / self.run_id
                / "cws-company-profile"
                / "handoff.json"
            ).is_file()
        )
        ready = json.loads(
            self.run_cli("delegate", "ready", "--run-id", self.run_id).stdout
        )
        self.assertEqual(["task2", "task3", "task4"], ready["ready"])

    def test_loop_enabled_delegate_failure_creates_new_attempt(self) -> None:
        self.init("--loop")
        first = json.loads(
            self.run_cli(
                "delegate", "start", "--run-id", self.run_id, "--node", "task1"
            ).stdout
        )
        failed = self.run_cli(
            "delegate",
            "fail",
            "--run-id",
            self.run_id,
            "--node",
            "task1",
            "--lease-id",
            first["lease_id"],
            "--reason",
            "maker crashed",
        )
        self.assertEqual(0, failed.returncode, failed.stderr)
        self.assertEqual("retryable", json.loads(failed.stdout)["status"])

        second = self.run_cli(
            "delegate", "start", "--run-id", self.run_id, "--node", "task1"
        )
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(2, json.loads(second.stdout)["attempt"])


if __name__ == "__main__":
    unittest.main()
