#!/usr/bin/env python3
"""Runtime built-in LLM Judge adapter integration checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "gates" / "company-profile"
GATE_CHECKER = SCRIPTS / "check_artifact_gate.py"


class RuntimeJudgeIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.dir = Path(self.temporary.name)
        self.handoff = self.dir / "handoff.json"
        shutil.copy(FIXTURES / "pass.handoff.json", self.handoff)
        shutil.copy(FIXTURES / "evidence.json", self.dir / "evidence.json")
        raw = self.dir / "raw"
        raw.mkdir()
        shutil.copy(FIXTURES / "raw" / "company.json", raw / "company.json")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _env(self, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "CWS_JUDGE_MODE",
            "CWS_JUDGE_API_KEY",
            "OPENAI_API_KEY",
            "CWS_JUDGE_ENABLED",
            "CWS_JUDGE_ADAPTER",
        ):
            env.pop(key, None)
        env.update(overrides)
        return env

    def _run_node(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(GATE_CHECKER),
                "--mode",
                "node",
                "--skill",
                "cws-company-profile",
                "--handoff",
                str(self.handoff),
                "--run-id",
                "run-test",
                "--plugin-root",
                str(ROOT),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_mock_mode_passes_and_writes_judge_section(self) -> None:
        result = self._run_node(self._env(CWS_JUDGE_MODE="mock"))
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads((self.dir / "gate-result.json").read_text(encoding="utf-8"))
        self.assertEqual("passed", payload["decision"])
        self.assertIn("judge", payload)
        self.assertEqual("passed", payload["judge"]["decision"])
        self.assertEqual("mock-judge", payload["judge"]["model"])

    def test_disabled_judge_fail_closed(self) -> None:
        result = self._run_node(self._env(CWS_JUDGE_MODE="mock", CWS_JUDGE_ENABLED="0"))
        self.assertEqual(1, result.returncode)
        self.assertIn("judge_disabled", result.stderr)
        payload = json.loads((self.dir / "gate-result.json").read_text(encoding="utf-8"))
        self.assertEqual("needs_review", payload["decision"])
        reasons = {item["reason"] for item in payload["judge"]["findings"]}
        self.assertIn("judge_disabled", reasons)

    def test_missing_api_key_fail_closed(self) -> None:
        result = self._run_node(self._env(CWS_JUDGE_MODE="live"))
        self.assertEqual(1, result.returncode)
        self.assertIn("judge_unavailable", result.stderr)
        payload = json.loads((self.dir / "gate-result.json").read_text(encoding="utf-8"))
        self.assertEqual("needs_review", payload["decision"])
        reasons = {item["reason"] for item in payload["judge"]["findings"]}
        self.assertIn("judge_unavailable", reasons)

    def test_resolve_default_adapter_path(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        from gate_judge_adapter import default_judge_script, resolve_judge_path

        path = resolve_judge_path(ROOT)
        self.assertEqual(default_judge_script(ROOT), path)
        self.assertTrue(path.is_file())

    def test_judge_finding_accepts_bracket_array_path(self) -> None:
        handoff = json.loads(self.handoff.read_text(encoding="utf-8"))
        handoff["artifacts"]["risk_flags"] = ["需复核"]
        self.handoff.write_text(json.dumps(handoff), encoding="utf-8")
        adapter = self.dir / "judge.py"
        adapter.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            " request = json.loads(line)\n"
            " print(json.dumps({"
            "'protocol_version':'cws-gate-judge/v1',"
            "'request_id':request['request_id'],"
            "'decision':'needs_review','confidence':0.9,'model':'test-judge',"
            "'rubric_version':request['rubric_id'],"
            "'findings':[{'reason':'data_quality_issue',"
            "'artifact_path':'artifacts.risk_flags[0]',"
            "'evidence_refs':['e-status']}]}), flush=True)\n",
            encoding="utf-8",
        )

        result = self._run_node(
            self._env(CWS_JUDGE_ADAPTER=str(adapter), CWS_JUDGE_MODE="live")
        )

        self.assertEqual(1, result.returncode)
        payload = json.loads((self.dir / "gate-result.json").read_text(encoding="utf-8"))
        self.assertEqual("needs_review", payload["decision"])
        self.assertEqual("test-judge", payload["judge"]["model"])
        self.assertEqual(
            "artifacts.risk_flags[0]",
            payload["judge"]["findings"][0]["artifact_path"],
        )


if __name__ == "__main__":
    unittest.main()
