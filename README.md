# NoeticAI 知识卡片 Work Suite

将 noeticai 项目中的企业知识卡片整理为可被 Hermes、Codex、Qoder 等平台加载的 skills 插件。

> **免责声明：** 本套件输出仅用于研究和决策辅助，不替代正式尽职调查、法律意见或投资建议。

## 能力概览

| 卡片 | 说明 |
|------|------|
| 企业画像 | 汇总企业基本信息、经营状态与核心标签 |
| 股权结构分析 | 股东结构、实控人、持股链路与股权异常信号 |
| 司法风险分析 | 诉讼、执行、失信等司法风险研判 |
| 融资历史分析 | 融资轮次、投资方、估值与资本市场信号 |
| 企业尽调 | 按内部 workflow 补齐前置分析产物，生成企业尽调摘要 |
| 投资分析 | 按内部 workflow 补齐核心卡片产物，生成投资研判报告 |

## Skill 内 Workflow

业务 workflow 放在对应编排型 skill 的 `references/workflow.yaml` 中，声明该 skill 的前置 stage 与产物依赖；跨平台可发现入口仍然是 `skills/*/SKILL.md`。

| Skill | Workflow |
|----------|------|
| 企业尽调 | [skills/noetic-due-diligence/references/workflow.yaml](./skills/noetic-due-diligence/references/workflow.yaml) |
| 投资分析 | [skills/noetic-investment-analysis/references/workflow.yaml](./skills/noetic-investment-analysis/references/workflow.yaml) |

## 企业数据查询策略

每个企业类业务技能直接读取自己的 `card.yaml.data_needs`，先检索企业信息库 wiki，再按缺口补齐公开企业信息。

企业信息库默认位于 `~/.noeticai/company-knowledge`，可通过 `NOETICAI_COMPANY_KB_DIR` 覆盖。只有 wiki 无命中、主体不确定、字段缺失，或信息明显过期时，才补齐公开企业信息。

补齐后，完整结果写入企业信息库 `raw/`，再按 `noetic-karpathy-llm-wiki` Ingest 流程整理进 `wiki/`。每个业务技能最终标注企业 wiki 写回状态。插件仓库只保存能力和规则，不保存具体企业数据。

## 开发

本仓库为独立开发目录。本地部署到 Hermes（校验、软链到 `~/.hermes/plugins/`、启用插件、合并 MCP，并做安装/MCP 冒烟验证）：

```bash
make deploy-local
```

仅验证当前 Hermes 安装是否生效（插件已启用、MCP 配置齐全、连通性）：

```bash
make verify-hermes
```

详细方案见 [docs/noeticai-knowledge-plugin-plan.md](./docs/noeticai-knowledge-plugin-plan.md)，调研情况见 [docs/pluginization-research.md](./docs/pluginization-research.md)。

静态校验：

```bash
python3 scripts/validate_work_suite.py .
```

## 目录结构

```text
.
├── .claude-plugin/plugin.json   # Claude/Hermes 兼容 manifest
├── .codex-plugin/plugin.json    # Codex 兼容 manifest
├── .mcp.json                    # MCP companion 配置
├── skills/
│   ├── {skill-name}/SKILL.md        # 卡片执行说明
│   ├── {skill-name}/card.yaml       # 卡片结构化元数据
│   ├── {orchestrating-skill}/references/workflow.yaml
│   └── noetic-karpathy-llm-wiki/       # LLM Wiki 技能
├── CONNECTORS.md
├── docs/WORK_SUITE_WORKFLOW.md
└── docs/noeticai-knowledge-plugin-plan.md
```
