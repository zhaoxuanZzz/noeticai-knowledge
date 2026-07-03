#!/usr/bin/env python3
"""Integration checks for the Noetic workflow Hermes backend."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "noetic-workflow" / "scripts" / "noetic_workflow.py"
VALIDATOR = ROOT / "scripts" / "validate_work_suite.py"
REAL_COMPANIES = ROOT / "tests" / "fixtures" / "real_companies.txt"


def run_command(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


class NoeticWorkflowIntegrationTest(unittest.TestCase):
    def test_real_company_fixture_has_ten_names(self) -> None:
        companies = real_companies()
        self.assertEqual(10, len(companies))
        self.assertEqual(len(companies), len(set(companies)))

    def test_static_suite_validation_passes(self) -> None:
        result = run_command(str(VALIDATOR), "--target", "all", ".")
        self.assertIn("OK:", result.stdout)

    def test_hermes_plugin_commands_load_namespaced_skills(self) -> None:
        plugin = load_plugin()
        ctx = FakeHermesCtx()

        plugin.register(ctx)

        self.assertIn("noetic-due-diligence", ctx.skills)
        self.assertIn("noetic-due-diligence", ctx.commands)
        command_prompt = ctx.commands["noetic-due-diligence"]("杭州XX科技有限公司")
        self.assertIn("`noeticai-knowledge:noetic-due-diligence`", command_prompt)
        self.assertIn("杭州XX科技有限公司", command_prompt)

        rewrite = ctx.hooks["pre_gateway_dispatch"](event=FakeEvent("/noetic-due-diligence 杭州XX科技有限公司"))
        self.assertEqual("rewrite", rewrite["action"])
        self.assertIn("`noeticai-knowledge:noetic-due-diligence`", rewrite["text"])

        self.assertIsNone(ctx.hooks["pre_gateway_dispatch"](event=FakeEvent("/unknown")))

    def test_entry_workflows_validate(self) -> None:
        for skill in ("noetic-due-diligence", "noetic-investment-analysis"):
            with self.subTest(skill=skill):
                result = run_command(str(SCRIPT), "validate", "--skill", skill)
                self.assertIn(f"OK: {skill} workflow (3 stages)", result.stdout)

    def test_due_diligence_dry_run_builds_expected_task_graph(self) -> None:
        result = run_command(
            str(SCRIPT),
            "execute",
            "--skill",
            "noetic-due-diligence",
            "--company",
            "杭州XX科技有限公司",
            "--workspace",
            "dir:/tmp/noetic-run",
            "--dry-run",
        )

        self.assertIn("Noetic workflow execution plan: noetic-due-diligence (5 tasks)", result.stdout)
        self.assertIn("task1: worker noetic-company-profile parents=[] outputs=['company_profile']", result.stdout)
        self.assertIn("task2: worker noetic-shareholder-structure parents=['task1']", result.stdout)
        self.assertIn("task3: worker noetic-litigation-risk parents=['task1']", result.stdout)
        self.assertIn("task4: worker noetic-financing-history parents=['task1']", result.stdout)
        self.assertIn("task5: gen noetic-due-diligence parents=['task1', 'task2', 'task3', 'task4']", result.stdout)

    def test_compile_outputs_dag_json_without_hermes_commands(self) -> None:
        result = run_command(
            str(SCRIPT),
            "compile",
            "--skill",
            "noetic-due-diligence",
            "--company",
            "杭州XX科技有限公司",
            "--workspace",
            "dir:/tmp/noetic-run",
        )
        graph = json.loads(result.stdout)

        self.assertEqual("noetic-due-diligence", graph["skill"])
        self.assertEqual(5, len(graph["nodes"]))
        self.assertEqual(
            [
                {"from": "task1", "to": "task2"},
                {"from": "task1", "to": "task3"},
                {"from": "task1", "to": "task4"},
                {"from": "task1", "to": "task5"},
                {"from": "task2", "to": "task5"},
                {"from": "task3", "to": "task5"},
                {"from": "task4", "to": "task5"},
            ],
            graph["edges"],
        )
        self.assertNotIn("hermes kanban create", result.stdout)

    def test_real_company_names_dry_run_for_all_entry_workflows(self) -> None:
        for company in real_companies():
            for skill in ("noetic-due-diligence", "noetic-investment-analysis"):
                with self.subTest(company=company, skill=skill):
                    result = run_command(
                        str(SCRIPT),
                        "execute",
                        "--skill",
                        skill,
                        "--company",
                        company,
                        "--workspace",
                        "dir:/tmp/noetic-run",
                        "--dry-run",
                    )
                    self.assertIn(f"Noetic workflow execution plan: {skill} (5 tasks)", result.stdout)
                    self.assertIn(f"[Noetic] {company} / profile / noetic-company-profile", result.stdout)

    def test_apply_uses_real_hermes_ids_for_parent_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)

            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"
            result = run_command(
                str(SCRIPT),
                "execute",
                "--skill",
                "noetic-investment-analysis",
                "--company",
                "杭州XX科技有限公司",
                "--workspace",
                "dir:/tmp/noetic-run",
                "--apply",
                env=env,
            )

            self.assertIn("created task5: h5", result.stdout)
            calls = [json.loads(line)["argv"] for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(5, len(calls))
            self.assertEqual("noetic-company-profile", calls[0][calls[0].index("--skill") + 1])
            self.assertNotIn("--parent", calls[0])
            self.assertEqual(["h1"], parent_values(calls[1]))
            self.assertEqual(["h1"], parent_values(calls[2]))
            self.assertEqual(["h1"], parent_values(calls[3]))
            self.assertEqual(["h1", "h2", "h3", "h4"], parent_values(calls[4]))

    def test_real_company_names_apply_with_fake_hermes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)

            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"
            for company in real_companies():
                with self.subTest(company=company):
                    result = run_command(
                        str(SCRIPT),
                        "execute",
                        "--skill",
                        "noetic-due-diligence",
                        "--company",
                        company,
                        "--workspace",
                        "dir:/tmp/noetic-run",
                        "--apply",
                        env=env,
                    )
                    self.assertIn("created task5:", result.stdout)

            calls = [json.loads(line)["argv"] for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(50, len(calls))
            for index, company in enumerate(real_companies()):
                chunk = calls[index * 5 : index * 5 + 5]
                self.assertIn(company, chunk[0][2])
                self.assertNotIn("--parent", chunk[0])
                first_id = f"h{index * 5 + 1}"
                self.assertEqual([first_id], parent_values(chunk[1]))
                self.assertEqual([first_id], parent_values(chunk[2]))
                self.assertEqual([first_id], parent_values(chunk[3]))
                self.assertEqual([f"h{index * 5 + offset}" for offset in range(1, 5)], parent_values(chunk[4]))

    def test_apply_fails_before_creating_tasks_when_profiles_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path, profiles=())

            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "execute",
                    "--skill",
                    "noetic-due-diligence",
                    "--company",
                    "小米科技有限责任公司",
                    "--workspace",
                    "dir:/tmp/noetic-run",
                    "--apply",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("missing Hermes profile(s): gen, worker", result.stderr)
            self.assertIn("hermes profile create worker", result.stderr)
            self.assertFalse(log_path.exists())

    def test_auto_dry_run_emits_single_triage_create(self) -> None:
        result = run_command(
            str(SCRIPT),
            "execute",
            "--mode",
            "auto",
            "--company",
            "小米科技有限责任公司",
            "--skill",
            "noetic-due-diligence",
            "--dry-run",
        )

        self.assertIn("Noetic workflow auto triage: 小米科技有限责任公司", result.stdout)
        self.assertIn("--triage", result.stdout)
        self.assertNotIn("--assignee", result.stdout)
        self.assertEqual(1, result.stdout.count("hermes kanban create"))

    def test_auto_apply_creates_triage_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)

            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"
            result = run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "auto",
                "--company",
                "小米科技有限责任公司",
                "--skill",
                "noetic-due-diligence",
                "--apply",
                env=env,
            )

            self.assertIn("created triage: h1", result.stdout)
            calls = [json.loads(line)["argv"] for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(1, len(calls))
            self.assertEqual("kanban", calls[0][0])
            self.assertEqual("create", calls[0][1])
            self.assertIn("--triage", calls[0])
            self.assertNotIn("--assignee", calls[0])

    def test_auto_apply_with_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)

            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"
            result = run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "auto",
                "--company",
                "小米科技有限责任公司",
                "--apply",
                "--dispatch",
                env=env,
            )

            self.assertIn("created triage: h1", result.stdout)
            self.assertIn("dispatched: nudged Hermes kanban dispatcher", result.stdout)
            calls = [json.loads(line)["argv"] for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(2, len(calls))
            self.assertEqual(["kanban", "dispatch"], calls[1])

    def test_planned_default_mode_unchanged(self) -> None:
        result = run_command(
            str(SCRIPT),
            "execute",
            "--skill",
            "noetic-due-diligence",
            "--company",
            "杭州XX科技有限公司",
            "--workspace",
            "dir:/tmp/noetic-run",
            "--dry-run",
        )

        self.assertIn("Noetic workflow execution plan: noetic-due-diligence (5 tasks)", result.stdout)
        self.assertEqual(5, result.stdout.count("hermes kanban create"))


def parent_values(argv: list[str]) -> list[str]:
    return [value for index, value in enumerate(argv) if index > 0 and argv[index - 1] == "--parent"]


def real_companies() -> list[str]:
    return [line.strip() for line in REAL_COMPANIES.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_plugin():
    spec = importlib.util.spec_from_file_location("noeticai_knowledge_plugin", ROOT / "__init__.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load plugin")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHermesCtx:
    def __init__(self) -> None:
        self.skills: dict[str, str] = {}
        self.hooks = {}
        self.commands = {}

    def register_skill(self, name: str, path: Path) -> None:
        self.skills[name] = Path(path).as_posix()

    def register_hook(self, name: str, handler) -> None:
        self.hooks[name] = handler

    def register_command(self, name: str, handler, description: str = "", args_hint: str = "") -> None:
        self.commands[name] = handler


class FakeEvent:
    def __init__(self, text: str) -> None:
        self.text = text


def write_fake_hermes(path: Path, log_path: Path, profiles: tuple[str, ...] = ("worker", "gen")) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

profiles = {list(profiles)!r}
if sys.argv[1:3] == ["profile", "show"]:
    raise SystemExit(0 if len(sys.argv) > 3 and sys.argv[3] in profiles else 1)

if sys.argv[1:3] == ["kanban", "dispatch"]:
    log = Path({str(log_path)!r})
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"argv": sys.argv[1:]}}, ensure_ascii=False) + "\\n")
    raise SystemExit(0)

log = Path({str(log_path)!r})
calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
task_id = f"h{{len(calls) + 1}}"
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "id": task_id}}, ensure_ascii=False) + "\\n")
print(json.dumps({{"id": task_id}}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
