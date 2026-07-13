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
SCRIPT = ROOT / "skills" / "noetic-workflow" / "scripts" / "noetic_workflow.py"
PASS_HANDOFF = ROOT / "tests" / "fixtures" / "gates" / "company-profile" / "pass.handoff.json"


class DelegateRunnerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.company_kb = Path(self.temporary.name) / "company-kb"
        self.env = os.environ.copy()
        self.env["NOETICAI_COMPANY_KB_DIR"] = str(self.company_kb)
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

    def init(self) -> dict[str, object]:
        result = self.run_cli(
            "delegate",
            "init",
            "--skill",
            "noetic-due-diligence",
            "--company",
            "杭州XX科技有限公司",
            "--run-id",
            self.run_id,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_init_persists_structured_plan_and_returns_first_ready_node(self) -> None:
        output = self.init()

        self.assertEqual(self.run_id, output["run_id"])
        self.assertEqual(["task1"], output["ready"])
        state_path = self.company_kb / "artifacts" / self.run_id / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        first = state["nodes"]["task1"]
        self.assertEqual("pending", first["status"])
        self.assertEqual("noetic-company-profile", first["skill"])
        self.assertEqual(self.run_id, first["node_gate"]["run_id"])
        self.assertTrue(first["handoff_path"].endswith("noetic-company-profile/handoff.json"))
        self.assertIsNone(first["final_gate"])
        self.assertEqual("noetic-due-diligence", state["nodes"]["task5"]["final_gate"]["skill"])

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
        handoff = self.company_kb / "artifacts" / self.run_id / "noetic-company-profile" / "handoff.json"
        handoff.parent.mkdir(parents=True)
        data = json.loads(PASS_HANDOFF.read_text(encoding="utf-8"))
        data["run_id"] = self.run_id
        handoff.write_text(json.dumps(data), encoding="utf-8")

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

        handoff = self.company_kb / "artifacts" / self.run_id / "noetic-company-profile" / "handoff.json"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(PASS_HANDOFF.read_text(encoding="utf-8"))
        data["run_id"] = self.run_id
        handoff.write_text(json.dumps(data), encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
