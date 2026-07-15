#!/usr/bin/env python3
"""Integration checks for Agent runtime artifact quality gates."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "validate_work_suite.py"
GATE_CHECKER = ROOT / "scripts" / "check_artifact_gate.py"
GATES_ROOT = ROOT / "tests" / "fixtures" / "gates"
FIXTURES = GATES_ROOT / "company-profile"

PASS_NODE_SKILLS = (
    ("cws-company-profile", "company-profile"),
    ("cws-shareholder-structure", "shareholder-structure"),
    ("cws-litigation-risk", "litigation-risk"),
    ("cws-financing-history", "financing-history"),
    ("cws-company-basic-info", "company-basic-info"),
)


def run_command(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    # Runtime judge is fail-closed without credentials; CI uses built-in mock.
    merged.setdefault("CWS_JUDGE_MODE", "mock")
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


class ArtifactGateIntegrationTest(unittest.TestCase):
    run_id = "run-test"

    def node_gate(
        self,
        handoff: Path,
        skill: str = "cws-company-profile",
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return run_command(
            str(GATE_CHECKER),
            "--mode",
            "node",
            "--skill",
            skill,
            "--handoff",
            str(handoff),
            "--run-id",
            self.run_id,
            "--plugin-root",
            str(ROOT),
            env=env,
        )

    def write_final_handoff(self, root: Path, skill: str, run_id: str = "run-test") -> Path:
        handoff = root / "artifacts" / self.run_id / skill / "handoff.json"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"run_id": run_id}
        if skill == "cws-due-diligence":
            payload.update({
                "subject": "杭州示例科技有限公司",
                "artifacts": {
                    "litigation_and_operating_risks": {"risk": "低风险"},
                    "recommendations": "继续核验后续变化",
                },
            })
            raw = handoff.parent / "raw" / "report.json"
            raw.parent.mkdir()
            raw.write_text(
                '{"risk":"低风险","recommendation":"继续核验后续变化"}',
                encoding="utf-8",
            )
            (handoff.parent / "evidence.json").write_text(
                json.dumps({
                    "run_id": run_id,
                    "subject": {"name": "杭州示例科技有限公司"},
                    "evidence": [
                        {"id": "e-risk", "field": "risk", "value": "低风险", "source_ref": "raw/report.json#/risk"},
                        {"id": "e-rec", "field": "recommendations", "value": "继续核验后续变化", "source_ref": "raw/report.json#/recommendation"},
                    ],
                    "claims": [
                        {"artifact_path": "artifacts.litigation_and_operating_risks.risk", "value": "低风险", "evidence_refs": ["e-risk"]},
                        {"artifact_path": "artifacts.recommendations", "value": "继续核验后续变化", "evidence_refs": ["e-rec"]},
                    ],
                    "conflicts": [],
                }),
                encoding="utf-8",
            )
        handoff.write_text(json.dumps(payload), encoding="utf-8")
        return handoff

    def final_gate(self, root: Path) -> subprocess.CompletedProcess[str]:
        return run_command(
            str(GATE_CHECKER),
            "--mode",
            "final",
            "--skill",
            "cws-due-diligence",
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

    def test_all_business_skill_pass_handoffs_exit_zero(self) -> None:
        for skill_id, fixture_dir in PASS_NODE_SKILLS:
            with self.subTest(skill=skill_id):
                handoff = GATES_ROOT / fixture_dir / "pass.handoff.json"
                result = self.node_gate(handoff, skill=skill_id)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("OK:", result.stdout)

    def test_semantic_gate_requires_sibling_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            handoff = Path(temp) / "handoff.json"
            handoff.write_text(
                (FIXTURES / "pass.handoff.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = self.node_gate(handoff)
        self.assertEqual(1, result.returncode)
        self.assertIn("evidence_missing", result.stderr)

    def test_semantic_gate_rejects_missing_required_claim_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            handoff = directory / "handoff.json"
            handoff.write_text(
                (FIXTURES / "pass.handoff.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            evidence = json.loads((FIXTURES / "evidence.json").read_text(encoding="utf-8"))
            evidence["claims"] = []
            (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
            raw = directory / "raw" / "company.json"
            raw.parent.mkdir()
            raw.write_text(
                (FIXTURES / "raw" / "company.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = self.node_gate(handoff)
        self.assertEqual(1, result.returncode)
        self.assertIn("evidence_missing", result.stderr)

    def test_semantic_gate_rejects_subject_name_mismatch_without_credit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            handoff = directory / "handoff.json"
            handoff.write_text(
                (FIXTURES / "pass.handoff.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            evidence = json.loads((FIXTURES / "evidence.json").read_text(encoding="utf-8"))
            evidence["subject"]["name"] = "另一家公司"
            (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
            raw = directory / "raw" / "company.json"
            raw.parent.mkdir()
            raw.write_text(
                (FIXTURES / "raw" / "company.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = self.node_gate(handoff)
        self.assertEqual(1, result.returncode)
        self.assertIn("subject_identity_mismatch", result.stderr)

    def test_needs_review_writes_review_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            handoff = directory / "handoff.json"
            handoff.write_text(
                (FIXTURES / "pass.handoff.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            evidence = json.loads((FIXTURES / "evidence.json").read_text(encoding="utf-8"))
            (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
            raw = directory / "raw" / "company.json"
            raw.parent.mkdir()
            raw.write_text(
                (FIXTURES / "raw" / "company.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = self.node_gate(handoff, env={"CWS_JUDGE_ENABLED": "0"})
            self.assertEqual(1, result.returncode)
            self.assertTrue((directory / "review.md").is_file())
            self.assertTrue((directory / "review-decision.json").is_file())
            md = (directory / "review.md").read_text(encoding="utf-8")
            self.assertIn("Gate 人工审核", md)
            decision = json.loads((directory / "review-decision.json").read_text(encoding="utf-8"))
            self.assertEqual(decision["status"], "pending")
            gate_result = json.loads((directory / "gate-result.json").read_text(encoding="utf-8"))
            self.assertEqual(gate_result["decision"], "needs_review")

    def test_semantic_gate_respects_explicit_empty_deterministic_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin = Path(temp) / "plugin"
            skill = plugin / "skills" / "cws-company-profile"
            skill.mkdir(parents=True)
            (skill / "card.yaml").write_text(
                "id: cws-company-profile\n"
                "outputs: [company_summary, operating_status]\n"
                "gate:\n"
                "  handoff: required\n"
                "  semantic:\n"
                "    evidence: required\n"
                "    deterministic_checks: []\n"
                "    required_claims: [artifacts.operating_status.status]\n",
                encoding="utf-8",
            )
            artifact = Path(temp) / "artifact"
            (artifact / "raw").mkdir(parents=True)
            (artifact / "raw" / "company.json").write_text(
                '{"status":"存续"}', encoding="utf-8"
            )
            handoff = artifact / "handoff.json"
            handoff.write_text(
                json.dumps({
                    "run_id": "run-test",
                    "subject": "目标公司",
                    "artifacts": {
                        "company_summary": {"status": "存续"},
                        "operating_status": {"status": "存续"},
                    },
                }),
                encoding="utf-8",
            )
            (artifact / "evidence.json").write_text(
                json.dumps({
                    "run_id": "run-test",
                    "subject": {"name": "另一家公司"},
                    "evidence": [{
                        "id": "e1", "field": "operating_status", "value": "存续",
                        "source_ref": "raw/company.json#/status",
                    }],
                    "claims": [{
                        "artifact_path": "artifacts.operating_status.status",
                        "value": "存续", "evidence_refs": ["e1"],
                    }],
                    "conflicts": [],
                }),
                encoding="utf-8",
            )
            result = run_command(
                str(GATE_CHECKER), "--mode", "node", "--skill", "cws-company-profile",
                "--handoff", str(handoff), "--run-id", "run-test",
                "--plugin-root", str(plugin),
            )
        self.assertEqual(0, result.returncode, result.stderr)

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
        with tempfile.TemporaryDirectory() as temp:
            plugin = Path(temp)
            skill = plugin / "skills" / "cws-ungated-demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("", encoding="utf-8")
            (skill / "card.yaml").write_text(
                "id: cws-ungated-demo\noutputs:\n  - note\n",
                encoding="utf-8",
            )
            handoff = plugin / "handoff.json"
            handoff.write_text(json.dumps({"run_id": self.run_id, "note": "x"}), encoding="utf-8")
            result = run_command(
                str(GATE_CHECKER),
                "--mode",
                "node",
                "--skill",
                "cws-ungated-demo",
                "--handoff",
                str(handoff),
                "--plugin-root",
                str(plugin),
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
                "cws-company-profile",
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
            for skill in ("cws-company-profile", "cws-shareholder-structure", "cws-litigation-risk", "cws-financing-history"):
                self.write_final_handoff(root, skill)
            broken = root / "artifacts" / self.run_id / "cws-litigation-risk" / "handoff.json"
            broken.write_text("{", encoding="utf-8")
            self.write_final_handoff(root, "cws-due-diligence")
            result = self.final_gate(root)
        self.assertEqual(1, result.returncode)
        self.assertIn("invalid JSON", result.stderr)

    def test_final_gate_rejects_mismatched_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for skill in ("cws-company-profile", "cws-shareholder-structure", "cws-litigation-risk", "cws-financing-history", "cws-due-diligence"):
                self.write_final_handoff(root, skill, "run-other" if skill == "cws-litigation-risk" else self.run_id)
            result = self.final_gate(root)
        self.assertEqual(1, result.returncode)
        self.assertIn("expected 'run-test'", result.stderr)

    def test_final_gate_accepts_complete_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for skill in ("cws-company-profile", "cws-shareholder-structure", "cws-litigation-risk", "cws-financing-history", "cws-due-diligence"):
                self.write_final_handoff(root, skill)
            result = self.final_gate(root)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_final_gate_accepts_mixed_subject_representations_for_same_company(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for skill in ("cws-company-profile", "cws-shareholder-structure", "cws-litigation-risk", "cws-financing-history", "cws-due-diligence"):
                self.write_final_handoff(root, skill)
            profile = root / "artifacts" / self.run_id / "cws-company-profile" / "handoff.json"
            profile.write_text(
                json.dumps({
                    "run_id": self.run_id,
                    "subject": {
                        "name": "杭州示例科技有限公司",
                        "unified_social_credit_code": "91330100TEST000001",
                    },
                }),
                encoding="utf-8",
            )
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
