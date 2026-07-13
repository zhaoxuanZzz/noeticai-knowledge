#!/usr/bin/env python3
"""Integration checks for Agent runtime artifact quality gates."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "validate_work_suite.py"
GATE_CHECKER = ROOT / "scripts" / "check_artifact_gate.py"
FIXTURES = ROOT / "tests" / "fixtures" / "gates" / "company-profile"


def run_command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class ArtifactGateIntegrationTest(unittest.TestCase):
    run_id = "run-test"

    def node_gate(self, handoff: Path) -> subprocess.CompletedProcess[str]:
        return run_command(
            str(GATE_CHECKER),
            "--mode",
            "node",
            "--skill",
            "noetic-company-profile",
            "--handoff",
            str(handoff),
            "--run-id",
            self.run_id,
            "--plugin-root",
            str(ROOT),
        )

    def write_final_handoff(self, root: Path, skill: str, run_id: str = "run-test") -> Path:
        handoff = root / "artifacts" / self.run_id / skill / "handoff.json"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        return handoff

    def final_gate(self, root: Path) -> subprocess.CompletedProcess[str]:
        return run_command(
            str(GATE_CHECKER),
            "--mode",
            "final",
            "--skill",
            "noetic-due-diligence",
            "--run-dir",
            str(root),
            "--run-id",
            self.run_id,
            "--plugin-root",
            str(ROOT),
        )

    def test_company_profile_gate_contract_validates(self) -> None:
        result = run_command(str(VALIDATOR), "--target", "work-suite", ".")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("OK:", result.stdout)

    def test_pass_handoff_exits_zero(self) -> None:
        result = self.node_gate(FIXTURES / "pass.handoff.json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("OK:", result.stdout)

    def test_missing_summary_exits_one(self) -> None:
        result = self.node_gate(FIXTURES / "missing-summary.handoff.json")
        self.assertEqual(1, result.returncode)
        self.assertIn("company_summary", result.stderr)

    def test_empty_summary_exits_one(self) -> None:
        result = self.node_gate(FIXTURES / "empty-summary.handoff.json")
        self.assertEqual(1, result.returncode)
        self.assertIn("non-empty", result.stderr)

    def test_data_role_with_final_report_exits_one(self) -> None:
        result = self.node_gate(FIXTURES / "data-with-final-report.handoff.json")
        self.assertEqual(1, result.returncode)
        self.assertIn("final_report", result.stderr)

    def test_skill_without_gate_skips(self) -> None:
        result = run_command(
            str(GATE_CHECKER),
            "--mode",
            "node",
            "--skill",
            "noetic-litigation-risk",
            "--handoff",
            str(FIXTURES / "pass.handoff.json"),
            "--plugin-root",
            str(ROOT),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("skip:", result.stdout)

    def test_node_gate_rejects_missing_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            handoff = Path(temp) / "handoff.json"
            data = json.loads((FIXTURES / "pass.handoff.json").read_text(encoding="utf-8"))
            del data["run_id"]
            handoff.write_text(json.dumps(data), encoding="utf-8")
            result = self.node_gate(handoff)
        self.assertEqual(1, result.returncode)
        self.assertIn("run_id", result.stderr)

    def test_node_gate_rejects_mismatched_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            handoff = Path(temp) / "handoff.json"
            data = json.loads((FIXTURES / "pass.handoff.json").read_text(encoding="utf-8"))
            data["run_id"] = "run-other"
            handoff.write_text(json.dumps(data), encoding="utf-8")
            result = self.node_gate(handoff)
        self.assertEqual(1, result.returncode)
        self.assertIn("expected 'run-test'", result.stderr)

    def test_final_mode_without_final_config_skips(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = run_command(
                str(GATE_CHECKER),
                "--mode",
                "final",
                "--skill",
                "noetic-company-profile",
                "--run-dir",
                temp,
                "--plugin-root",
                str(ROOT),
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("skip:", result.stdout)

    def test_final_gate_rejects_missing_parent_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.final_gate(root)
        self.assertEqual(1, result.returncode)
        self.assertIn("missing parent handoff", result.stderr)

    def test_final_gate_rejects_invalid_parent_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for skill in ("noetic-company-profile", "noetic-shareholder-structure", "noetic-litigation-risk", "noetic-financing-history"):
                self.write_final_handoff(root, skill)
            broken = root / "artifacts" / self.run_id / "noetic-litigation-risk" / "handoff.json"
            broken.write_text("{", encoding="utf-8")
            self.write_final_handoff(root, "noetic-due-diligence")
            result = self.final_gate(root)
        self.assertEqual(1, result.returncode)
        self.assertIn("invalid JSON", result.stderr)

    def test_final_gate_rejects_mismatched_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for skill in ("noetic-company-profile", "noetic-shareholder-structure", "noetic-litigation-risk", "noetic-financing-history", "noetic-due-diligence"):
                self.write_final_handoff(root, skill, "run-other" if skill == "noetic-litigation-risk" else self.run_id)
            result = self.final_gate(root)
        self.assertEqual(1, result.returncode)
        self.assertIn("expected 'run-test'", result.stderr)

    def test_final_gate_accepts_complete_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for skill in ("noetic-company-profile", "noetic-shareholder-structure", "noetic-litigation-risk", "noetic-financing-history", "noetic-due-diligence"):
                self.write_final_handoff(root, skill)
            result = self.final_gate(root)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_invalid_gate_card_fails_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "suite"
            # Minimal suite via validator self-test helpers would be heavy;
            # invoke card_gate normalize through checker usage error path instead:
            # write a temp plugin with one bad card and run work-suite target.
            scripts = ROOT / "scripts"
            for name in ("validate_work_suite.py", "card_gate.py", "ensure_hermes_mcp.py"):
                target = root / "scripts" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text((scripts / name).read_text(encoding="utf-8"), encoding="utf-8")

            (root / ".codex-plugin").mkdir(parents=True)
            (root / ".codex-plugin" / "plugin.json").write_text(
                '{"name": "suite", "skills": "./skills/"}', encoding="utf-8"
            )
            (root / ".claude-plugin").mkdir(parents=True)
            (root / ".claude-plugin" / "plugin.json").write_text(
                '{"name": "suite"}', encoding="utf-8"
            )
            (root / "plugin.yaml").write_text(
                'name: suite\nversion: 0.1.0\ndescription: t\nmcp_servers:\n'
                "  qcc-company:\n"
                "    url: https://agent.qcc.com/mcp/company/stream\n"
                "    headers:\n"
                '      Authorization: "Bearer ${QCC_MCP_TOKEN}"\n',
                encoding="utf-8",
            )
            (root / ".mcp.json").write_text(
                '{"mcpServers":{"qcc-company":{"type":"http",'
                '"url":"https://agent.qcc.com/mcp/company/stream"}}}',
                encoding="utf-8",
            )
            (root / "__init__.py").write_text(
                "def register(ctx):\n    ctx.register_skill('research', 'skills/research/SKILL.md')\n",
                encoding="utf-8",
            )
            skill = root / "skills" / "research"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("", encoding="utf-8")
            (skill / "card.yaml").write_text(
                "id: research\noutputs:\n  - context\ngate:\n  handoff: optional\n",
                encoding="utf-8",
            )

            result = run_command(str(root / "scripts" / "validate_work_suite.py"), "--target", "work-suite", str(root))
            self.assertEqual(1, result.returncode)
            self.assertIn("gate.handoff must be 'required'", result.stderr)


if __name__ == "__main__":
    unittest.main()
