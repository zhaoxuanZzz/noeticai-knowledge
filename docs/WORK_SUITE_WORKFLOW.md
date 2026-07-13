# Skill Workflow

本文定义 NoeticAI plugin 的 skill workflow 结构规范。目标是让 Hermes、Codex、Qoder 等平台可发现的入口保持为 `skills/*/SKILL.md`，同时允许编排型 skill 通过内部 workflow 声明前置 stage 与产物依赖。

## 目标与非目标

插件是面向企业研究的专家包，包含原子 skill、编排型 skill，以及编排型 skill 内部的 workflow。它建立在多平台 skills plugin 结构之上，不替代各宿主需要的 manifest。

首版只定义目录、静态引用和人工可读语义，不实现 workflow runner，不新增数据库模型。产物结构由各 skill 的 `card.yaml` 和 `SKILL.md` 表达；声明了 `gate` 的 skill 由 `scripts/check_artifact_gate.py` 做运行时硬拦（见 `docs/superpowers/specs/2026-07-10-agent-quality-gate-design.md`）。

## 标准目录结构

```text
<plugin-name>/
  .codex-plugin/
    plugin.json
  .claude-plugin/
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
  assets/
  README.md
```

硬性规则：

- `<plugin-name>` 使用 kebab-case，且必须等于 manifest 中的 `name`。
- 至少一个宿主 manifest 必须存在；支持 `.codex-plugin/plugin.json`、`.claude-plugin/plugin.json` 等平台入口。
- 不在 `plugin.json` 添加 workflow 自定义字段；workflow 元数据放在对应编排型 skill 的 `references/workflow.yaml`。
- `skills/` 下每个目录是一个 skill，必须包含 `SKILL.md`。
- 编排型 skill 可以在 `references/workflow.yaml` 显式编排前置原子 skills。
- workflow 只表达业务 SOP 编排，不写底层执行代码。
- 外部系统能力由 MCP/plugin 既有机制提供，Work Suite 不另行定义连接协议。
- 跨 skill 复用资料应优先收口到拥有该职责的 skill；只有真正跨职责复用的资料才单独建共享目录。
- `tests/` 只放对应 skill 的轻量验证。
- v1 校验只检查静态引用：plugin 名称、skill 目录、workflow 的 skill 引用与 inputs/outputs 依赖关系。

## 概念边界

| 概念 | 职责 |
| --- | --- |
| Plugin | 领域专家包，例如 `noeticai-knowledge` |
| 原子 Skill | 单项能力，例如企业画像、司法风险分析；可独立调用，也可作为其他 workflow 的 stage |
| 编排型 Skill | 拥有终端业务能力，并通过 `references/workflow.yaml` 声明前置 stage 与产物依赖，例如企业尽调、投资分析 |
| Skill Workflow | 编排型 skill 内部显式编排多个 skill 的 stage、前置、并行、产物传递 |
| Artifact | stage 间传递的结构化产物名称；具体字段由各 skill 的 `card.yaml` 定义 |
| `/noetic-workflow` | Noetic workflow 的规范解释、创建辅助和执行入口；支持 `planned`（静态 workflow.yaml）与 `auto`（Hermes triage 自动拆图）两种执行模式 |

核心原则：每个 skill 能力独立；需要标准前置流程时，由编排型 skill 的内部 workflow 显式表达业务顺序。Skill 可以描述输入、输出、触发条件和失败条件，但 v1 只把这些内容作为静态约定。

`/noetic-workflow` 是通用管理入口：它可以解释 workflow 规范、辅助创建 `references/workflow.yaml`，并把编排型 skill 的 workflow 提交到当前试行执行层。执行时用户可选择 **planned**（按 `workflow.yaml` 确定性编排）或 **auto**（提交 Hermes triage 卡由 `kanban_decomposer` 自动拆图）。编排型 skill 不应在缺少前置产物时自行串行调用前置卡片，而应转交 `/noetic-workflow` 执行对应 workflow。

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
```

字段约定：

- `name`：workflow 展示名，通常与编排型 skill 同名。
- `stages`：按顺序执行的阶段列表。
- `stages[].id`：稳定阶段标识，使用 snake_case。
- `stages[].skills`：本阶段需要调用的原子 skills。
- `stages[].inputs`：本阶段消费的 artifacts，只能引用前序阶段声明过的 outputs。
- `stages[].outputs`：本阶段产出的 artifacts。
- `stages[].parallel`：说明本阶段内 skills 在业务上互不依赖；v1 不承诺执行行为。

v1 校验脚本只支持上述 YAML 子集，尤其是 `skills`、`inputs`、`outputs` 使用行内数组写法，例如 `[context_pack, working_paper]`。复杂 YAML 写法应先转成这个最小形态。

## 与知识卡片调用机制的映射

| 知识卡片机制 | Skill workflow 机制 |
| --- | --- |
| `[调用卡片:id:<card_id>]` | `skills/<编排型skill>/references/workflow.yaml` 的 `stages[].skills` |
| 子卡片输出 | artifact |
| `task_relations` | workflow stage graph |
| `caller_session_id` | 后续 runner 可参考的 run/parent id |

这只是架构映射，不要求 skill workflow 复用现有知识卡片执行器。后续如实现 runner，应优先保留这些可观测性和防循环能力。

## 静态校验

首版提供零依赖校验脚本：

```bash
python3 scripts/validate_work_suite.py <plugin-root>
```

校验范围：

- 宿主 manifest 存在，且 `name` 等于 `<plugin-root>` 目录名。
- `skills/*/SKILL.md` 存在。
- `skills/*/references/workflow.yaml` 的 `stages[].skills` 引用已有 skill 目录名。
- `stages[].inputs` 只能引用前序 stage 声明过的 `outputs`。

## 示例套件

```text
credit-rating-analyst/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  skills/
    rating-knowledge-base/SKILL.md
    industry-research/SKILL.md
    financial-analysis/SKILL.md
    peer-comparison/SKILL.md
    compliance-due-diligence/SKILL.md
    rating-report/SKILL.md
    initial-rating/
      SKILL.md
      references/workflow.yaml
```

这个套件中，编排型 skill 持有 workflow SOP；各 skill 的 `card.yaml` 负责产物字段约束；跨平台可发现入口仍是 `skills/*/SKILL.md`。

## 评审场景

结构变更或新增 Work Suite 时，至少用以下场景检查：

- 单 stage 单 skill：能表达最简单任务。
- 多 stage 前置依赖：后续 stage 能消费前置 artifact。
- stage 内多 skill：能表达未来并行。

本规范只要求上述静态校验通过；不引入 runner。
