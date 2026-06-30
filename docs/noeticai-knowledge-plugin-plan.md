# NoeticAI 知识卡片 Work Suite 改造方案

> 版本：v0.2 · 更新日期：2026-06-28  
> 目标：将 noeticai 企业知识卡片整理为 Codex skills 插件。  
> 原则：卡片独立、入口 skill 内部 workflow 显式编排、产物协议可检查、暂不实现 runner。

## 1. 背景与目标

noeticai 现有知识卡片具备两类能力：

1. **知识分析能力**：卡片定义输入、分析逻辑、输出结构。
2. **数据获取能力**：卡片通过 `data_needs` 描述所需外部数据。

本方案将知识卡片迁移为 `noeticai-knowledge` skills 插件。迁移后：

- 每张卡片都是独立 `skill`
- 业务入口如 `企业尽调`、`投资分析` 也是独立 `skill`
- 业务顺序放入入口 skill 的 `references/workflow.yaml`
- stage 间产物由 `artifact-contracts/*.yaml` 描述
- 结果检查目标由 `quality-gates/*.yaml` 描述
- `.mcp.json` 只作为 Codex plugin companion 配置

不在本阶段实现 workflow runtime、数据库 schema 迁移或 MCP 工具名自动映射。

## 2. 目标目录结构

```text
noeticai-knowledge/
├── .codex-plugin/
│   └── plugin.json
├── .mcp.json
├── README.md
├── README_EN.md
├── CONNECTORS.md
├── artifact-contracts/
│   ├── company_profile.yaml
│   ├── shareholder_structure.yaml
│   ├── litigation_risk.yaml
│   ├── financing_history.yaml
│   ├── due_diligence_report.yaml
│   └── investment_analysis_report.yaml
├── quality-gates/
│   ├── evidence_coverage.yaml
│   └── no_fabricated_data.yaml
└── skills/
    ├── noetic-company-profile/
    │   ├── SKILL.md
    │   └── card.yaml
    ├── noetic-shareholder-structure/
    │   ├── SKILL.md
    │   └── card.yaml
    ├── noetic-litigation-risk/
    │   ├── SKILL.md
    │   └── card.yaml
    ├── noetic-financing-history/
    │   ├── SKILL.md
    │   └── card.yaml
    ├── noetic-due-diligence/
        ├── SKILL.md
        ├── card.yaml
        └── references/
            └── workflow.yaml
    └── noetic-investment-analysis/
        ├── SKILL.md
        ├── card.yaml
        └── references/
            └── workflow.yaml
```

旧 `.claude-plugin/`、`.qoder-plugin/` 和顶层 `workflows/` 不再保留。

## 3. Plugin Manifest

`.codex-plugin/plugin.json` 是唯一插件 manifest：

```json
{
  "name": "noeticai-knowledge",
  "version": "0.1.0",
  "description": "NoeticAI 企业知识卡片 Work Suite：围绕企业画像、股权结构、司法风险、融资历史和投资分析生成结构化研判。",
  "author": {
    "name": "NoeticAI"
  },
  "keywords": ["work-suite", "knowledge-card", "company-research"],
  "skills": "./skills/",
  "mcpServers": "./.mcp.json"
}
```

Workflow 自定义语义不写入 `plugin.json`；workflow 放在入口 skill 的 `references/workflow.yaml`，artifact contract 和 quality gate 使用独立 YAML 文件表达。

## 4. 单卡设计

原子卡片保持独立，只声明输入、数据需求、输出和分析规则。入口卡片可以通过 `references/workflow.yaml` 编排前置原子卡片。

`skills/noetic-shareholder-structure/card.yaml` 示例：

```yaml
id: noetic-shareholder-structure
name: 股权结构分析
description: 分析目标企业股东结构、实控人、持股链路和股权异常信号。

inputs:
  - company_name
  - unified_social_credit_code

data_needs:
  - 查询企业工商基本信息
  - 查询当前股东列表及持股比例
  - 查询历史股权变更
  - 查询实际控制人
  - 查询对外投资和关联企业

outputs:
  - shareholder_summary
  - control_chain
  - related_entities
  - risk_flags
  - evidence_gaps
```

## 5. Workflow 设计

workflow 只负责编排，不承载具体分析逻辑。v1 使用 `docs/WORK_SUITE_WORKFLOW.md` 中定义的最小 YAML 子集，并放在入口 skill 内。

`skills/noetic-due-diligence/references/workflow.yaml`：

```yaml
name: 企业尽调
stages:
  - id: profile
    skills: [noetic-company-profile]
    outputs: [company_profile]

  - id: analysis
    skills: [noetic-shareholder-structure, noetic-litigation-risk, noetic-financing-history]
    inputs: [company_profile]
    parallel: true
    outputs: [shareholder_structure, litigation_risk, financing_history]

  - id: report
    skills: [noetic-due-diligence]
    inputs: [company_profile, shareholder_structure, litigation_risk, financing_history]
    outputs: [due_diligence_report]
    quality_gates: [evidence_coverage, no_fabricated_data]
```

`report` stage 使用入口 `企业尽调` skill。前置画像、股权、司法和融资阶段由该 skill 的 workflow 显式编排。

## 6. 数据与 MCP 接入

数据库函数迁移为 `card.yaml` 的自然语言 `data_needs`。例如：

```text
getCompanyBasicInfo(company_id)
```

迁移为：

```yaml
data_needs:
  - 查询企业工商基本信息，包括企业名称、统一社会信用代码、法定代表人、注册资本、成立日期、经营状态、注册地址、经营范围
```

`.mcp.json` 保留企查查 MCP companion 配置。业务 skill 只声明 `data_needs`；每个企业类 skill 先检索企业信息库，默认目录为 `~/.noeticai/company-knowledge`，可通过 `NOETICAI_COMPANY_KB_DIR` 覆盖。仅在 wiki 无命中、主体不确定、字段缺失或数据明显过期时，才按缺口补齐公开企业信息。补齐后必须写回企业信息库 `raw/` 和 `wiki/`，并在输出中标注写回状态。不可用或缺失的数据必须列出 `evidence_gaps`，不得编造企业数据。

## 7. 验收标准

- `python3 scripts/validate_work_suite.py --self-test` 通过
- `python3 scripts/validate_work_suite.py .` 通过
- `.codex-plugin/plugin.json` 是唯一插件 manifest
- workflow 位于入口 skill 的 `references/workflow.yaml`
- workflow 输出都有对应 artifact contract
- workflow quality gate 都有对应 YAML
- 企业画像、股权结构分析、司法风险分析、融资历史分析、企业尽调和投资分析可作为独立 skill 被触发

## 8. 暂不实现

- workflow runtime
- 卡片 DAG 可视化
- 数据库 schema 迁移
- noeticai 后端函数兼容层
- MCP 工具名自动映射
