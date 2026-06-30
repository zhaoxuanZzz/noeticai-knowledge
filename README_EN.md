# NoeticAI Knowledge Cards Work Suite

Codex skills plugin that migrates NoeticAI enterprise knowledge cards into atomic and workflow-backed skills.

> **Disclaimer:** Outputs are for research and decision support only. They do not replace formal due diligence, legal opinions, or investment advice.

## Cards

| Card | Description |
|------|-------------|
| 企业画像 | Company profile, operating status, and key tags |
| 股权结构分析 | Shareholder structure, control chain, and equity risk signals |
| 司法风险分析 | Litigation, enforcement, and credit risk analysis |
| 融资历史分析 | Funding rounds, investors, valuation, and capital market signals |
| 企业尽调 | Due diligence report after completing its internal prerequisite workflow |
| 投资分析 | Investment analysis report after completing its internal prerequisite workflow |

## Skill Workflows

Business workflows live inside entry skills as `references/workflow.yaml`. Codex discovery still starts from `skills/*/SKILL.md`.

| Skill | Workflow |
|----------|-------------|
| 企业尽调 | [skills/noetic-due-diligence/references/workflow.yaml](./skills/noetic-due-diligence/references/workflow.yaml) |
| 投资分析 | [skills/noetic-investment-analysis/references/workflow.yaml](./skills/noetic-investment-analysis/references/workflow.yaml) |

## Company Data Lookup Strategy

Each company business skill reads its own `card.yaml.data_needs`, checks the local company wiki first, then fills missing public company data as needed.

The company knowledge base defaults to `~/.noeticai/company-knowledge` and can be overridden with `NOETICAI_COMPANY_KB_DIR`. Public lookup is used only when the wiki has no match, the company identity is uncertain, required fields are missing, or the matched data is clearly stale.

After lookup, the full result is stored as a raw source in the knowledge base `raw/`, then compiled into `wiki/` through the `noetic-karpathy-llm-wiki` Ingest flow. Each business skill reports the company wiki writeback status. This plugin stores capabilities and rules, not company data.

## Development

Link this repo into a local Codex plugin directory for testing:

```bash
ln -s /Users/zhaoxuan/code/noeticai-knowledge \
  ~/plugins/noeticai-knowledge
```

Static validation:

```bash
python3 scripts/validate_work_suite.py .
```

See [docs/noeticai-knowledge-plugin-plan.md](./docs/noeticai-knowledge-plugin-plan.md) for the full migration plan.

## Structure

```text
.
├── .codex-plugin/plugin.json
├── .mcp.json
├── artifact-contracts/*.yaml
├── quality-gates/*.yaml
├── skills/{skill-name}/SKILL.md
├── skills/{skill-name}/card.yaml
├── skills/{entry-skill}/references/workflow.yaml
└── scripts/validate_work_suite.py
```
