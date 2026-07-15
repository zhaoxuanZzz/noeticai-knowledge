#!/usr/bin/env python3
"""Integration checks for the CWS workflow Hermes backend."""

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
SCRIPT = ROOT / "skills" / "cws-workflow" / "scripts" / "workflow_cli.py"
VALIDATOR = ROOT / "scripts" / "validate_work_suite.py"
REAL_COMPANIES = ROOT / "tests" / "fixtures" / "real_companies.txt"
ROLE_HOOK = ROOT / "hooks" / "role_routing.py"


def run_command(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


class WorkflowIntegrationTest(unittest.TestCase):
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

        with tempfile.TemporaryDirectory() as temp:
            env_home = Path(temp) / "hermes-home"
            env_home.mkdir()
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = str(env_home)
            try:
                plugin.register(ctx)
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous

        self.assertIn("cws-due-diligence", ctx.skills)
        self.assertIn("cws-due-diligence", ctx.commands)
        self.assertIn("kanban_task_claimed", ctx.hooks)
        self.assertIn("企业尽调", ctx.command_descriptions["cws-due-diligence"])
        self.assertIn("基于企业画像", ctx.command_descriptions["cws-due-diligence"])
        self.assertIn("输入公司名称", ctx.command_args_hints["cws-due-diligence"])
        command_prompt = ctx.commands["cws-due-diligence"]("杭州XX科技有限公司")
        self.assertIn("`company-work-suite:cws-due-diligence`", command_prompt)
        self.assertIn("cws-data-agent", command_prompt)
        self.assertIn("cws-gen-agent", command_prompt)
        self.assertIn("杭州XX科技有限公司", command_prompt)

        rewrite = ctx.hooks["pre_gateway_dispatch"](event=FakeEvent("/cws-due-diligence 杭州XX科技有限公司"))
        self.assertEqual("rewrite", rewrite["action"])
        self.assertIn("`company-work-suite:cws-due-diligence`", rewrite["text"])
        self.assertIn("route work through role skills", rewrite["text"])

        self.assertIsNone(ctx.hooks["pre_gateway_dispatch"](event=FakeEvent("/unknown")))

    def test_ensure_hermes_mcp_merges_and_rewrites_auth(self) -> None:
        helper = load_ensure_hermes_mcp()
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "hermes-home"
            home.mkdir()
            config_path = home / "config.yaml"
            config_path.write_text(
                """model:
  default: demo
plugins:
  enabled:
  - company-work-suite
  disabled: []
mcp_servers:
  other-server:
    url: https://example.com/mcp
  qcc-company:
    url: https://agent.qcc.com/mcp/company/stream
    headers:
      Authorization: Bearer hardcoded-token
extra_top_level: keep-me
""",
                encoding="utf-8",
            )

            changed = helper.ensure_hermes_mcp(plugin_root=ROOT, home=home)
            self.assertTrue(changed)

            text = config_path.read_text(encoding="utf-8")
            self.assertIn("extra_top_level: keep-me", text)
            self.assertIn("- company-work-suite", text)
            self.assertIn("default: demo", text)

            data = helper._load_yaml(config_path)
            servers = data["mcp_servers"]
            self.assertEqual("https://example.com/mcp", servers["other-server"]["url"])
            self.assertEqual(
                "Bearer ${QCC_MCP_TOKEN}",
                servers["qcc-company"]["headers"]["Authorization"],
            )
            for name in (
                "qcc-company",
                "qcc-risk",
                "qcc-ipr",
                "qcc-operation",
                "qcc-executive",
            ):
                self.assertIn(name, servers)
                self.assertEqual(
                    "Bearer ${QCC_MCP_TOKEN}",
                    servers[name]["headers"]["Authorization"],
                )

            changed_again = helper.ensure_hermes_mcp(plugin_root=ROOT, home=home)
            self.assertFalse(changed_again)

    def test_codex_role_hook_injects_only_for_cws_prompts(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROLE_HOOK)],
            input=json.dumps({"prompt": "Use company-work-suite to prepare a report"}),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertEqual("CWS:ROLE_ROUTING", output["systemMessage"])
        self.assertEqual("UserPromptSubmit", output["hookSpecificOutput"]["hookEventName"])
        self.assertIn("cws-data-agent", context)
        self.assertIn("cws-gen-agent", context)
        self.assertNotIn("company", context.lower())
        self.assertNotIn("qcc", context.lower())

        result = subprocess.run(
            [sys.executable, str(ROLE_HOOK)],
            input=json.dumps({"prompt": "帮我做企业画像"}),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("CWS:ROLE_ROUTING", result.stdout)

        result = subprocess.run(
            [sys.executable, str(ROLE_HOOK)],
            input=json.dumps({"prompt": "format this README"}),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual("", result.stdout)

    def test_entry_workflows_validate(self) -> None:
        for skill in ("cws-due-diligence", "cws-investment-analysis"):
            with self.subTest(skill=skill):
                result = run_command(str(SCRIPT), "validate", "--skill", skill)
                self.assertIn(f"OK: {skill} workflow (3 stages)", result.stdout)

    def test_due_diligence_dry_run_builds_expected_task_graph(self) -> None:
        result = run_command(
            str(SCRIPT),
            "execute",
            "--mode",
            "planned",
            "--skill",
            "cws-due-diligence",
            "--company",
            "杭州XX科技有限公司",
            "--workspace",
            "dir:/tmp/cws-run",
            "--dry-run",
        )

        self.assertIn("CWS workflow execution plan: cws-due-diligence (5 tasks)", result.stdout)
        self.assertIn("task1: data cws-company-profile parents=[] outputs=['company_profile']", result.stdout)
        self.assertIn("task2: data cws-shareholder-structure parents=['task1']", result.stdout)
        self.assertIn("task3: data cws-litigation-risk parents=['task1']", result.stdout)
        self.assertIn("task4: data cws-financing-history parents=['task1']", result.stdout)
        self.assertIn("task5: gen cws-due-diligence parents=['task1', 'task2', 'task3', 'task4']", result.stdout)
        self.assertNotIn("--assignee", result.stdout)

    def test_compile_outputs_dag_json_without_hermes_commands(self) -> None:
        result = run_command(
            str(SCRIPT),
            "compile",
            "--skill",
            "cws-due-diligence",
            "--company",
            "杭州XX科技有限公司",
            "--workspace",
            "dir:/tmp/cws-run",
        )
        graph = json.loads(result.stdout)

        self.assertEqual("cws-due-diligence", graph["skill"])
        self.assertEqual(5, len(graph["nodes"]))
        self.assertEqual("data", graph["nodes"][0]["role"])
        self.assertEqual("cws-data-agent", graph["nodes"][0]["role_skill"])
        self.assertEqual("gen", graph["nodes"][4]["role"])
        self.assertEqual("cws-gen-agent", graph["nodes"][4]["role_skill"])
        self.assertNotIn("assignee", graph["nodes"][0])
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

    def test_delegate_mode_outputs_subagent_plan_without_hermes_commands(self) -> None:
        result = run_command(
            str(SCRIPT),
            "execute",
            "--mode",
            "delegate",
            "--skill",
            "cws-due-diligence",
            "--company",
            "杭州XX科技有限公司",
            "--workspace",
            "dir:/tmp/cws-run",
        )
        graph = json.loads(result.stdout)

        self.assertEqual("delegate", graph["mode"])
        self.assertEqual("cws-due-diligence", graph["skill"])
        self.assertEqual(5, len(graph["nodes"]))
        self.assertEqual("cws-company-profile", graph["nodes"][0]["skill"])
        self.assertEqual("data", graph["nodes"][0]["role"])
        self.assertEqual("cws-data-agent", graph["nodes"][0]["role_skill"])
        self.assertEqual(["cws-data-agent", "cws-karpathy-llm-wiki"], graph["nodes"][0]["required_skills"])
        self.assertIn("执行 CWS 知识卡片：cws-company-profile", graph["nodes"][0]["prompt"])
        self.assertIn("必需搭配 skill：cws-karpathy-llm-wiki", graph["nodes"][0]["prompt"])
        self.assertEqual(["task1", "task2", "task3", "task4"], graph["nodes"][4]["parents"])
        self.assertEqual("gen", graph["nodes"][4]["role"])
        self.assertEqual("cws-gen-agent", graph["nodes"][4]["role_skill"])
        self.assertEqual(["cws-gen-agent"], graph["nodes"][4]["required_skills"])
        self.assertTrue(graph["nodes"][0]["handoff_path"].endswith("cws-company-profile/handoff.json"))
        self.assertEqual("node", graph["nodes"][0]["node_gate"]["mode"])
        self.assertIsNone(graph["nodes"][0]["final_gate"])
        self.assertEqual("cws-due-diligence", graph["nodes"][4]["final_gate"]["skill"])
        self.assertNotIn("assignee", graph["nodes"][0])
        self.assertNotIn("hermes kanban create", result.stdout)

    def test_delegate_plan_keeps_gate_metadata_out_of_maker_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            company_kb = Path(temp) / "company-kb"
            env = os.environ.copy()
            env["CWS_COMPANY_KB_DIR"] = str(company_kb)
            result = run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "delegate",
                "--skill",
                "cws-due-diligence",
                "--company",
                "杭州XX科技有限公司",
                "--run-id",
                "run-gate-test",
                env=env,
            )
        graph = json.loads(result.stdout)
        self.assertEqual("run-gate-test", graph["run_id"])
        self.assertEqual("node", graph["nodes"][0]["node_gate"]["mode"])
        self.assertEqual("cws-due-diligence", graph["nodes"][4]["final_gate"]["skill"])
        for node in graph["nodes"]:
            prompt = node["prompt"]
            self.assertIn("run-gate-test", prompt)
            self.assertIn(node["handoff_path"], prompt)
            self.assertIn("evidence.json", prompt)
            self.assertIn('"evidence": [{', prompt)
            self.assertIn('"claims": [{', prompt)
            self.assertIn('"artifact_path": "artifacts.', prompt)
            self.assertIn('"source_ref": "raw/', prompt)
            self.assertIn("source_ref 相对于 handoff 所在目录", prompt)
            self.assertIn("handoff 中每个事实字段都必须有 claim", prompt)
            self.assertNotIn("check_artifact_gate.py", prompt)
            self.assertNotIn("--mode node", prompt)
            self.assertNotIn("--mode final", prompt)
            self.assertNotIn("node gate", prompt)
            self.assertNotIn("门禁未通过不得完成", prompt)

    def test_delegate_frozen_kb_disables_external_refresh_in_every_node(self) -> None:
        result = run_command(
            str(SCRIPT),
            "execute",
            "--mode",
            "delegate",
            "--skill",
            "cws-due-diligence",
            "--company",
            "杭州XX科技有限公司",
            "--run-id",
            "run-frozen-test",
            "--frozen-kb",
        )
        graph = json.loads(result.stdout)

        self.assertTrue(graph["frozen_kb"])
        self.assertTrue(all(node["frozen_kb"] for node in graph["nodes"]))
        for node in graph["nodes"]:
            self.assertIn("冻结知识库模式", node["prompt"])
            self.assertIn("不得调用 MCP、网络搜索或其他外部数据源", node["prompt"])
            self.assertIn("artifact 目录的 raw/", node["prompt"])

    def test_delegate_default_keeps_wiki_first_refresh_behavior(self) -> None:
        result = run_command(
            str(SCRIPT), "execute", "--mode", "delegate",
            "--skill", "cws-due-diligence", "--company", "杭州XX科技有限公司",
        )
        graph = json.loads(result.stdout)

        self.assertFalse(graph["frozen_kb"])
        self.assertFalse(any(node["frozen_kb"] for node in graph["nodes"]))
        self.assertIn("缺失或过期时补齐公开信息", graph["nodes"][0]["prompt"])

    def test_business_and_data_skills_do_not_execute_gates(self) -> None:
        business_skills = (
            "cws-company-basic-info",
            "cws-company-profile",
            "cws-shareholder-structure",
            "cws-litigation-risk",
            "cws-financing-history",
            "cws-due-diligence",
            "cws-investment-analysis",
        )
        forbidden = (
            "check_artifact_gate.py",
            "--mode final",
            "node gate",
            "门禁未通过不得",
            "门禁 exit code",
            "运行时门禁",
            "Judge 返回可疑",
            "Judge 要求复核",
        )
        for skill in (*business_skills, "cws-data-agent"):
            text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            for phrase in forbidden:
                with self.subTest(skill=skill, phrase=phrase):
                    self.assertNotIn(phrase, text)
        for skill in business_skills:
            text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            with self.subTest(skill=skill):
                self.assertNotIn("cws-data-agent", text)
                self.assertNotIn("artifacts/<run-id>", text)

    def test_real_company_names_dry_run_for_all_entry_workflows(self) -> None:
        for company in real_companies():
            for skill in ("cws-due-diligence", "cws-investment-analysis"):
                with self.subTest(company=company, skill=skill):
                    result = run_command(
                        str(SCRIPT),
                        "execute",
                        "--mode",
                        "planned",
                        "--skill",
                        skill,
                        "--company",
                        company,
                        "--workspace",
                        "dir:/tmp/cws-run",
                        "--dry-run",
                    )
                    self.assertIn(f"CWS workflow execution plan: {skill} (5 tasks)", result.stdout)
                    self.assertIn(f"[CWS] {company} / profile / cws-company-profile", result.stdout)

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
                "--mode",
                "planned",
                "--skill",
                "cws-investment-analysis",
                "--company",
                "杭州XX科技有限公司",
                "--workspace",
                "dir:/tmp/cws-run",
                "--apply",
                env=env,
            )

            self.assertIn("created task5: h5", result.stdout)
            calls = [json.loads(line)["argv"] for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(5, len(calls))
            self.assertEqual("cws-company-profile", calls[0][calls[0].index("--skill") + 1])
            self.assertTrue(all("--assignee" not in call for call in calls))
            self.assertNotIn("--parent", calls[0])
            self.assertEqual(["h1"], parent_values(calls[1]))
            self.assertEqual(["h1"], parent_values(calls[2]))
            self.assertEqual(["h1"], parent_values(calls[3]))
            self.assertEqual(["h1", "h2", "h3", "h4"], parent_values(calls[4]))

    def test_planned_tasks_carry_run_id_and_gate_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)
            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"
            env["CWS_COMPANY_KB_DIR"] = str(temp_path / "company-kb")
            result = run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "planned",
                "--skill",
                "cws-due-diligence",
                "--company",
                "杭州XX科技有限公司",
                "--run-id",
                "run-gate-test",
                "--apply",
                env=env,
            )
            calls = [json.loads(line)["argv"] for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertIn("run_id: run-gate-test", result.stdout)
        bodies = [call[call.index("--body") + 1] for call in calls]
        self.assertTrue(all("--run-id run-gate-test" in body for body in bodies))
        self.assertIn("--mode final", bodies[-1])

    def test_apply_passes_tenant_to_every_planned_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)

            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"
            run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "planned",
                "--skill",
                "cws-due-diligence",
                "--company",
                "杭州XX科技有限公司",
                "--workspace",
                "dir:/tmp/cws-run",
                "--tenant",
                "batch-20260703-hzxx",
                "--apply",
                env=env,
            )

            calls = [json.loads(line)["argv"] for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(5, len(calls))
            self.assertTrue(all(call[call.index("--tenant") + 1] == "batch-20260703-hzxx" for call in calls))

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
                        "--mode",
                        "planned",
                        "--skill",
                        "cws-due-diligence",
                        "--company",
                        company,
                        "--workspace",
                        "dir:/tmp/cws-run",
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

    def test_auto_dry_run_emits_single_triage_create(self) -> None:
        result = run_command(
            str(SCRIPT),
            "execute",
            "--mode",
            "auto",
            "--company",
            "小米科技有限责任公司",
            "--skill",
            "cws-due-diligence",
            "--dry-run",
        )

        self.assertIn("CWS workflow auto triage: 小米科技有限责任公司", result.stdout)
        self.assertIn("--triage", result.stdout)
        self.assertNotIn("--assignee", result.stdout)
        self.assertEqual(1, result.stdout.count("hermes kanban create"))
        self.assertIn("does not guarantee static workflow gate compliance", result.stdout)

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
                "cws-due-diligence",
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

    def test_auto_apply_passes_tenant_to_triage_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)

            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"
            run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "auto",
                "--company",
                "小米科技有限责任公司",
                "--skill",
                "cws-due-diligence",
                "--tenant",
                "batch-xiaomi",
                "--apply",
                env=env,
            )

            calls = [json.loads(line)["argv"] for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual("batch-xiaomi", calls[0][calls[0].index("--tenant") + 1])

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

    def test_auto_loop_validated_plan_reuses_planned_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp) / "auto-plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": "profile",
                                "skill": "cws-company-profile",
                                "parents": [],
                                "outputs": ["company_profile"],
                            },
                            {
                                "id": "equity",
                                "skill": "cws-shareholder-structure",
                                "parents": ["profile"],
                                "inputs": ["company_profile"],
                                "outputs": ["shareholder_structure"],
                            },
                            {
                                "id": "litigation",
                                "skill": "cws-litigation-risk",
                                "parents": ["profile"],
                                "inputs": ["company_profile"],
                                "outputs": ["litigation_risk"],
                            },
                            {
                                "id": "financing",
                                "skill": "cws-financing-history",
                                "parents": ["profile"],
                                "inputs": ["company_profile"],
                                "outputs": ["financing_history"],
                            },
                            {
                                "id": "report",
                                "skill": "cws-due-diligence",
                                "parents": [
                                    "profile",
                                    "equity",
                                    "litigation",
                                    "financing",
                                ],
                                "inputs": [
                                    "company_profile",
                                    "shareholder_structure",
                                    "litigation_risk",
                                    "financing_history",
                                ],
                                "outputs": ["due_diligence_report"],
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "auto",
                "--skill",
                "cws-due-diligence",
                "--company",
                "杭州XX科技有限公司",
                "--run-id",
                "run-auto-loop",
                "--loop",
                "--plan",
                str(plan),
                "--dry-run",
            )

        self.assertIn("validated auto plan", result.stdout)
        self.assertIn("loop: enabled", result.stdout)
        self.assertEqual(5, result.stdout.count("hermes kanban create"))
        self.assertNotIn("--triage", result.stdout)

    def test_auto_loop_rejects_plan_missing_final_parent_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp) / "incomplete-plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": "profile",
                                "skill": "cws-company-profile",
                                "parents": [],
                                "outputs": ["company_profile"],
                            },
                            {
                                "id": "report",
                                "skill": "cws-due-diligence",
                                "parents": ["profile"],
                                "inputs": ["company_profile"],
                                "outputs": ["due_diligence_report"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "execute",
                    "--mode",
                    "auto",
                    "--skill",
                    "cws-due-diligence",
                    "--company",
                    "杭州XX科技有限公司",
                    "--loop",
                    "--plan",
                    str(plan),
                    "--dry-run",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing final parent artifacts", result.stderr)

    def test_auto_loop_triage_requests_plan_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            company_kb = temp_path / "company-kb"
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)
            env = os.environ.copy()
            env["CWS_COMPANY_KB_DIR"] = str(company_kb)
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"

            run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "auto",
                "--skill",
                "cws-due-diligence",
                "--company",
                "杭州XX科技有限公司",
                "--run-id",
                "run-auto-triage",
                "--loop",
                "--apply",
                env=env,
            )

            call = json.loads(log_path.read_text(encoding="utf-8"))["argv"]
            body = call[call.index("--body") + 1]
            plan_path = (
                company_kb / "artifacts" / "run-auto-triage" / "auto-plan.json"
            )
            self.assertIn("cws_auto_plan: run_id=run-auto-triage", body)
            self.assertIn(str(plan_path), body)
            self.assertIn("--mode auto --loop --plan", body)

    def test_execute_requires_explicit_mode(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "execute",
                "--skill",
                "cws-due-diligence",
                "--company",
                "杭州XX科技有限公司",
                "--workspace",
                "dir:/tmp/cws-run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("the following arguments are required: --mode", result.stderr)

    def test_planned_default_workspace_under_cws(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runs_root = Path(temp) / "kanban-runs"
            env = os.environ.copy()
            env["CWS_KANBAN_RUNS_DIR"] = str(runs_root)
            result = run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "planned",
                "--skill",
                "cws-due-diligence",
                "--company",
                "杭州XX科技有限公司",
                "--tenant",
                "batch-20260703-hzxx",
                "--dry-run",
                env=env,
            )

            expected = f"dir:{(runs_root / 'batch-20260703-hzxx').resolve()}"
            self.assertIn(f"workspace: {expected}", result.stdout)
            self.assertIn(f"--workspace {expected}", result.stdout)

    def test_auto_default_workspace_under_cws(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runs_root = Path(temp) / "kanban-runs"
            env = os.environ.copy()
            env["CWS_KANBAN_RUNS_DIR"] = str(runs_root)
            result = run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "auto",
                "--company",
                "小米科技有限责任公司",
                "--tenant",
                "batch-xiaomi",
                "--dry-run",
                env=env,
            )

            expected = f"dir:{(runs_root / 'batch-xiaomi').resolve()}"
            self.assertIn(f"workspace: {expected}", result.stdout)
            self.assertIn(f"--workspace {expected}", result.stdout)

    def test_apply_creates_default_workspace_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            runs_root = temp_path / "kanban-runs"
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)

            env = os.environ.copy()
            env["CWS_KANBAN_RUNS_DIR"] = str(runs_root)
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"
            run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "planned",
                "--skill",
                "cws-due-diligence",
                "--company",
                "杭州XX科技有限公司",
                "--tenant",
                "batch-20260703-hzxx",
                "--apply",
                env=env,
            )

            self.assertTrue((runs_root / "batch-20260703-hzxx").is_dir())

    def test_planned_loop_apply_initializes_runner_and_binds_kanban_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            company_kb = temp_path / "company-kb"
            log_path = temp_path / "hermes-calls.jsonl"
            fake_hermes = temp_path / "hermes"
            write_fake_hermes(fake_hermes, log_path)
            env = os.environ.copy()
            env["CWS_COMPANY_KB_DIR"] = str(company_kb)
            env["PATH"] = f"{temp_path}{os.pathsep}{env.get('PATH', '')}"

            result = run_command(
                str(SCRIPT),
                "execute",
                "--mode",
                "planned",
                "--skill",
                "cws-due-diligence",
                "--company",
                "杭州XX科技有限公司",
                "--run-id",
                "run-planned-loop",
                "--loop",
                "--apply",
                env=env,
            )

            self.assertIn("loop: enabled", result.stdout)
            calls = [
                json.loads(line)["argv"]
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(5, len(calls))
            state = json.loads(
                (
                    company_kb
                    / "artifacts"
                    / "run-planned-loop"
                    / "workflow-state.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual("delegate-loop", state["mode"])
            for index, node in enumerate(state["nodes"].values(), 1):
                self.assertEqual(f"h{index}", node["kanban_task_id"])
                body = calls[index - 1][calls[index - 1].index("--body") + 1]
                self.assertIn(
                    f"cws_loop: run_id=run-planned-loop node={node['id']}", body
                )
                self.assertNotIn("check_artifact_gate.py", body)


def parent_values(argv: list[str]) -> list[str]:
    return [value for index, value in enumerate(argv) if index > 0 and argv[index - 1] == "--parent"]


def real_companies() -> list[str]:
    return [line.strip() for line in REAL_COMPANIES.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_plugin():
    spec = importlib.util.spec_from_file_location("cws_knowledge_plugin", ROOT / "__init__.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load plugin")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_ensure_hermes_mcp():
    spec = importlib.util.spec_from_file_location(
        "ensure_hermes_mcp",
        ROOT / "scripts" / "ensure_hermes_mcp.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load ensure_hermes_mcp")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHermesCtx:
    def __init__(self) -> None:
        self.skills: dict[str, str] = {}
        self.hooks = {}
        self.commands = {}
        self.command_descriptions: dict[str, str] = {}
        self.command_args_hints: dict[str, str] = {}

    def register_skill(self, name: str, path: Path) -> None:
        self.skills[name] = Path(path).as_posix()

    def register_hook(self, name: str, handler) -> None:
        self.hooks[name] = handler

    def register_command(self, name: str, handler, description: str = "", args_hint: str = "") -> None:
        self.commands[name] = handler
        self.command_descriptions[name] = description
        self.command_args_hints[name] = args_hint

    def register_cli_command(self, *args, **kwargs) -> None:
        pass


class FakeEvent:
    def __init__(self, text: str) -> None:
        self.text = text


def write_fake_hermes(path: Path, log_path: Path) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

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
