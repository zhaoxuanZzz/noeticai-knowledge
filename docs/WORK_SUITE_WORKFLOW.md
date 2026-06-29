# Skill Workflow

本文定义 NoeticAI plugin 的 skill workflow 结构规范。目标是让 Codex 可发现的入口保持为 `skills/*/SKILL.md`，同时允许入口 skill 通过内部 workflow 编排前置原子 skills。

## 目标与非目标

插件是面向企业研究的专家包，包含原子 skill、入口 skill、入口 skill 内部 workflow、产物协议和质量门禁。它建立在 Codex plugin 标准结构之上，不替代 `.codex-plugin/plugin.json`。

首版只定义目录、静态引用和人工可读语义，不实现 workflow runner，不新增数据库模型，不改知识卡片执行链路。

## 标准目录结构

```text
<plugin-name>/
  .codex-plugin/
    plugin.json
  skills/
    <skill-name>/
      SKILL.md
      card.yaml
      references/
        workflow.yaml
      scripts/
      assets/
      tests/
  artifact-contracts/
    <artifact-name>.yaml
  quality-gates/
    <gate-name>.yaml
  assets/
  README.md
```

硬性规则：

- `<plugin-name>` 使用 kebab-case，且必须等于 `.codex-plugin/plugin.json` 的 `name`。
- `.codex-plugin/plugin.json` 必须存在，`skills` 指向 `./skills/`。
- 不在 `plugin.json` 添加 workflow 自定义字段；workflow 元数据放在对应入口 skill 的 `references/workflow.yaml`。
- `skills/` 下每个目录是一个 skill，必须包含 `SKILL.md`。
- 入口 skill 可以在 `references/workflow.yaml` 显式编排前置原子 skills。
- workflow 只表达业务 SOP 编排，不写底层执行代码。
- 外部系统能力由 MCP/plugin 既有机制提供，Work Suite 不另行定义连接协议。
- 跨 skill 复用资料应优先收口到拥有该职责的 skill；只有真正跨职责复用的资料才单独建共享目录。
- `tests/` 只放对应 skill 的轻量验证。
- v1 校验只检查静态引用：plugin 名称、skill 目录、skill workflow 引用的 artifact contract 和 quality gate 是否存在。

## 概念边界

| 概念 | 职责 |
| --- | --- |
| Plugin | 领域专家包，例如 `noeticai-knowledge` |
| 原子 Skill | 单项能力，例如企业画像、司法风险分析 |
| 入口 Skill | 可被用户直接触发的业务能力，例如企业尽调、投资分析 |
| Skill Workflow | 入口 skill 内部显式编排多个 skill 的 stage、前置、并行、产物传递 |
| Artifact Contract | stage 间传递的结构化产物协议 |
| Quality Gate | 对 artifact 或最终结果描述人工或脚本可读的检查目标 |

核心原则：采用入口 skill 内部 workflow 显式表达业务顺序。Skill 可以描述输入、输出、触发条件和失败条件，但 v1 只把这些内容作为静态约定。

## Workflow YAML 最小语义

```yaml
name: 初评流程
stages:
  - id: context_research
    skills: [评级知识库, 行业研究]
    outputs: [context_pack, planning_tree]

  - id: analysis
    skills: [财务分析, 同业对比, 合规尽调]
    inputs: [context_pack]
    parallel: true
    outputs: [working_paper, evidence_records]

  - id: generation
    skills: [评级报告]
    inputs: [context_pack, working_paper, evidence_records]
    outputs: [rating_report]

  - id: review
    skills: [评级委员会材料]
    inputs: [rating_report, evidence_records]
    quality_gates: [evidence_coverage, methodology_consistency]
```

字段约定：

- `name`：workflow 展示名，通常与入口 skill 同名。
- `stages`：按顺序执行的阶段列表。
- `stages[].id`：稳定阶段标识，使用 snake_case。
- `stages[].skills`：本阶段需要调用的原子 skills。
- `stages[].inputs`：本阶段消费的 artifacts，只能引用前序阶段声明过的 outputs。
- `stages[].outputs`：本阶段产出的 artifacts。
- `stages[].parallel`：说明本阶段内 skills 在业务上互不依赖；v1 不承诺执行行为。
- `stages[].quality_gates`：本阶段完成后应检查的质量门禁。

v1 校验脚本只支持上述 YAML 子集，尤其是 `skills`、`inputs`、`outputs`、`quality_gates` 使用行内数组写法，例如 `[context_pack, working_paper]`。复杂 YAML 写法应先转成这个最小形态。

## Contract 示例

Artifact Contract 只描述产物形状和验收含义，不绑定具体执行代码，也不要求 runner 消费。

```yaml
name: working_paper
description: 评级分析工作底稿
required_fields:
  - issuer_profile
  - financial_analysis
  - peer_comparison
  - evidence_records
```

Quality Gate 描述人工或脚本可读的检查目标和输入，不负责业务流程编排，也不绑定 EvalAgent。

```yaml
name: evidence_coverage
inputs: [rating_report, evidence_records]
checks:
  - every_key_claim_has_evidence
  - evidence_source_is_traceable
```

## 与知识卡片调用机制的映射

| 知识卡片机制 | Skill workflow 机制 |
| --- | --- |
| `[调用卡片:id:<card_id>]` | `skills/<入口skill>/references/workflow.yaml` 的 `stages[].skills` |
| 子卡片输出 | artifact |
| `task_relations` | workflow stage graph |
| `caller_session_id` | 后续 runner 可参考的 run/parent id |
| EvalAgent | 后续 runner 可参考的 quality gate 执行方式 |

这只是架构映射，不要求 skill workflow 复用现有知识卡片执行器。后续如实现 runner，应优先保留这些可观测性和防循环能力。

## 静态校验

首版提供零依赖校验脚本：

```bash
python3 scripts/validate_work_suite.py <plugin-root>
```

校验范围：

- `.codex-plugin/plugin.json` 存在，且 `name` 等于 `<plugin-root>` 目录名。
- `skills/*/SKILL.md` 存在。
- `skills/*/references/workflow.yaml` 的 `stages[].skills` 引用已有 skill 目录名。
- `stages[].inputs` 只能引用前序 stage 声明过的 `outputs`。
- `stages[].outputs` 必须存在对应 `artifact-contracts/<name>.yaml`。
- `stages[].quality_gates` 必须存在对应 `quality-gates/<name>.yaml`。

## 示例套件

```text
credit-rating-analyst/
  .codex-plugin/plugin.json
  skills/
    rating-knowledge-base/SKILL.md
    industry-research/SKILL.md
    financial-analysis/SKILL.md
    peer-comparison/SKILL.md
    compliance-due-diligence/SKILL.md
    rating-report/SKILL.md
    committee-material/SKILL.md
    initial-rating/
      SKILL.md
      references/workflow.yaml
  artifact-contracts/
    context-pack.yaml
    working-paper.yaml
    rating-report.yaml
  quality-gates/
    evidence-coverage.yaml
    methodology-consistency.yaml
```

这个套件中，入口 skill 是 Codex 可发现入口；workflow 是入口 skill 内的 SOP；artifact contract 负责上下游数据约束；quality gate 负责评估闭环。

## 评审场景

结构变更或新增 Work Suite 时，至少用以下场景检查：

- 单 stage 单 skill：能表达最简单任务。
- 多 stage 前置依赖：后续 stage 能消费前置 artifact。
- stage 内多 skill：能表达未来并行。
- review stage：能挂 quality gate。

本规范只要求上述静态校验通过；不引入 runner。
