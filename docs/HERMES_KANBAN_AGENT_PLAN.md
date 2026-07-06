# Hermes Kanban Trial Backend Plan

本文记录 `/noetic-workflow execute` 当前试行执行层的 Hermes Kanban 方案。通用 workflow 语义属于 `/noetic-workflow`；Hermes Kanban 只负责首版运行 backend。

## 结论

`/noetic-workflow` 负责读取编排型 skill 的 `references/workflow.yaml`；Hermes Kanban backend 负责把 workflow 变成任务图执行。

首版只保留一个 Hermes 执行 profile，角色分工交给 role skill：

| Profile / Skill | 用途 | 执行什么 | 不做什么 |
| --- | --- | --- | --- |
| `worker` profile | Hermes task 运行身份 | 接收 Kanban task，加载 NoeticAI Knowledge 套件 | 不表达业务角色 |
| `noetic-data-agent` skill | 前置卡片角色 | 企业画像、股权结构、司法风险、融资历史等中间产物卡片 | 不生成最终报告，不把缺失字段补写成确定结论 |
| `noetic-gen-agent` skill | 最终报告角色 | 企业尽调、投资分析等编排型报告卡片 | 不重新取数，不绕过父任务 artifact 补事实 |

不单独拆 `data` 和 `analysis`。现有知识卡片本身已经同时包含数据需求、业务判断和输出格式，强拆会先制造迁移成本。

## 角色职责

`noetic-data-agent` 是首版的前置卡片角色 skill。它接收一个具体知识卡片任务，只负责产出该卡片声明的 artifact。

`noetic-data-agent` 的输入：

- task body 中的目标公司、输入 artifact 和输出 artifact。
- 父任务 handoff 中的结构化 artifact、来源、数据时间和 `evidence_gaps`。
- 当前 NoeticAI Knowledge 套件里的对应 `SKILL.md` 和 `card.yaml`。

`noetic-data-agent` 的输出：

- 一个或多个中间 artifact，例如 `company_profile`、`shareholder_structure`、`litigation_risk`、`financing_history`。
- 每个 artifact 的来源、数据时间、关键判断和 `evidence_gaps`。
- kanban completion summary，供下游 data 角色或 `gen` 角色读取。

`noetic-data-agent` 必须遵守：

- 优先查企业信息库 wiki。
- 缺失或过期字段才补公开信息，并按卡片要求写回 raw/wiki。
- 能判断证据不足，但不能编造工商、司法、股权、融资或估值事实。
- 同一个 stage 标记 `parallel: true` 时，多个 data 角色 task 可以并行运行，但彼此不共享隐式状态，只通过 parent artifact 传递信息。

`noetic-gen-agent` 是首版的最终报告角色 skill。它只处理编排型报告卡片，例如 `noetic-due-diligence` 和 `noetic-investment-analysis`。

`noetic-gen-agent` 的输入：

- 所有父任务 handoff 中的 artifact、来源、数据时间和 `evidence_gaps`。
- 编排型报告 skill 的 `SKILL.md` 和 `card.yaml`。

`noetic-gen-agent` 的输出：

- 最终报告 artifact，例如 `due_diligence_report` 或 `investment_analysis_report`。
- 报告摘要、核心结论、引用来源、数据时间和关键 `evidence_gaps`。
- kanban completion summary，作为本次 workflow 的最终交付说明。

`noetic-gen-agent` 必须遵守：

- 只综合父任务已经交接的 artifact。
- 不重新取数，不补造缺失事实。
- 发现关键 artifact 缺失或证据不足时，在报告里保留数据缺口，而不是扩大结论。
- 质量约束由 `card.yaml` 的 `rules` 和 `SKILL.md` 表达；独立验收不在本仓库实现。

## 现有卡片结构

当前仓库里的编排型卡片：

| 编排型 skill | workflow | 最终产物 |
| --- | --- | --- |
| `noetic-due-diligence` | `skills/noetic-due-diligence/references/workflow.yaml` | `due_diligence_report` |
| `noetic-investment-analysis` | `skills/noetic-investment-analysis/references/workflow.yaml` | `investment_analysis_report` |

当前复用的前置卡片：

| Skill | 产物 | Role | Assignee |
| --- | --- | --- | --- |
| `noetic-company-profile` | `company_profile` | `data` | `worker` |
| `noetic-shareholder-structure` | `shareholder_structure` | `data` | `worker` |
| `noetic-litigation-risk` | `litigation_risk` | `data` | `worker` |
| `noetic-financing-history` | `financing_history` | `data` | `worker` |

这些前置卡片不是纯取数卡片。它们会按 `card.yaml` 的 `data_needs` 检索企业信息库或公开信息，也会产出结构化判断和 `evidence_gaps`。因此首版把它们统一交给 `worker` profile 执行，但用 `noetic-data-agent` / `noetic-gen-agent` 区分角色。

## Backend 映射

规则尽量静态，避免让模型猜编排：

| Noetic workflow | Hermes Kanban backend |
| --- | --- |
| `stages[].skills[]` | 每个 skill 创建一个 task |
| `stages[].inputs[]` | 找到产出该 artifact 的前置 task，作为 parent |
| `stages[].outputs[]` | 写入 task body，要求 data 产出这些 artifact |
| `parallel: true` | 同 stage 内 task 共享前置 parent，彼此不互相依赖 |
| `stage.id == report` | `role=gen`，`role_skill=noetic-gen-agent` |
| `stage.skills` 包含编排型 skill 本身（终端 stage） | `role=gen`，`role_skill=noetic-gen-agent` |
| 其他 stage | `role=data`，`role_skill=noetic-data-agent` |
| 所有 planned task | `--assignee worker` |

首版不使用 `hermes kanban swarm`。官方 swarm helper 是固定的 workers -> verifier -> synthesizer 拓扑；我们先不做 verifier，所以直接用 `kanban create` + parent 依赖表达 DAG。

## 企业尽调任务图

来源：`skills/noetic-due-diligence/references/workflow.yaml`

```text
noetic-company-profile
  -> noetic-shareholder-structure
  -> noetic-litigation-risk
  -> noetic-financing-history
      -> noetic-due-diligence
```

实际依赖是：

```text
profile task
  -> shareholder task
  -> litigation task
  -> financing task

shareholder task \
litigation task   -> due diligence report task
financing task   /
profile task    /
```

其中 `analysis.parallel: true`，所以股权、司法、融资三个任务可以并行。

## 投资分析任务图

来源：`skills/noetic-investment-analysis/references/workflow.yaml`

```text
profile task
  -> shareholder task
  -> litigation task
  -> financing task

shareholder task \
litigation task   -> investment analysis report task
financing task   /
profile task    /
```

它和企业尽调共用同一批前置卡片，只是最终 `gen` 任务换成 `noetic-investment-analysis`。

## 任务正文模板

前置 data task：

```text
执行 Noetic 知识卡片：<skill>

目标公司：<company>
输入 artifact：<inputs or none>
输出 artifact：<outputs>
委派角色 skill：noetic-data-agent
必需搭配 skill：noetic-karpathy-llm-wiki

要求：
- 按该 skill 的 SKILL.md 和 card.yaml 执行
- 按 noetic-karpathy-llm-wiki 规范优先检索企业信息库 wiki
- 缺失或过期时补齐公开信息并写回 raw/wiki
- 不编造数据，缺失字段写入 evidence_gaps
- 完成时在 kanban summary 中返回 artifact 摘要、来源、数据时间和 evidence_gaps
```

最终 gen task：

```text
执行 Noetic 编排型报告卡片：<skill>

目标公司：<company>
消费前置 artifact：<inputs>
输出最终 artifact：<outputs>
委派角色 skill：noetic-gen-agent

要求：
- 只综合父任务交接中的 artifact、来源、数据时间和 evidence_gaps
- 不重新取数，不补造缺失信息
- 按该 skill 的 SKILL.md 和 card.yaml 输出报告
- 完成时在 kanban summary 中返回最终报告摘要和关键 evidence_gaps
```

## 通用执行入口

通用 CLI 是：

```text
skills/noetic-workflow/scripts/noetic_workflow.py
```

职责：

1. 读取编排型 skill 的 `references/workflow.yaml`（planned 模式）。
2. 校验 `stages[].skills[]` 都存在。
3. 根据 `outputs -> task_id` 建 parent 关系。
4. `compile` 模式输出 DAG JSON，供查看和调试编译结果。
5. `execute` 模式委托 Hermes Kanban backend 生成或执行 `hermes kanban create` 命令。
6. 输出创建的 task id 和依赖关系。

## 执行模式

`execute` 支持两种编排模式，由 `--mode` 选择（默认 `planned`）：

| 模式 | 说明 | 前置条件 |
| --- | --- | --- |
| `planned` | 读 `workflow.yaml`，确定性创建多条 `kanban create`（含 assignee、parent、skill） | `worker` profile 已创建 |
| `auto` | 创建单条 `kanban create --triage`，由 Hermes `kanban_decomposer` 自动拆图 | gateway 运行中；`kanban.auto_decompose: true`；profile description 有助于路由 |

选型建议：

- 标准尽调/投分、要可复现 DAG → `planned`
- 用户只有粗需求、探索拆图效果 → `auto`（可加 `--dispatch` 立即 nudge dispatcher）

`auto` 模式不保证与 `workflow.yaml` 拓扑一致；`--skill` 可选，仅作为 triage body 中的编排型 skill 提示。

示例：

```bash
# planned（默认）
python skills/noetic-workflow/scripts/noetic_workflow.py execute \
  --mode planned \
  --skill noetic-due-diligence \
  --company "杭州XX科技有限公司" \
  --workspace scratch \
  --dry-run

# auto（Hermes 自动拆图）
python skills/noetic-workflow/scripts/noetic_workflow.py execute \
  --mode auto \
  --company "小米科技有限责任公司" \
  --skill noetic-due-diligence \
  --dispatch \
  --apply
```

原有示例：

```bash
python skills/noetic-workflow/scripts/noetic_workflow.py compile \
  --skill noetic-due-diligence \
  --company "杭州XX科技有限公司" \
  --workspace scratch
```

上面的 `compile` 只打印本地编译出的 `nodes` 和 `edges`，不调用 Hermes，不创建任务，不运行模型。它用于确认 workflow 会变成什么 DAG。

```bash
python skills/noetic-workflow/scripts/noetic_workflow.py execute \
  --skill noetic-due-diligence \
  --company "杭州XX科技有限公司" \
  --workspace scratch \
  --dry-run
```

`execute --dry-run` 打印将要执行的多条 `hermes kanban create` 命令。`execute --apply` 才真正提交给 Hermes。

## 运行链路

`skills/noetic-workflow/scripts/noetic_workflow.py execute` 不直接运行模型。它先把 workflow 编译成 DAG，再用多条 `hermes kanban create --parent ...` 把节点和边写入 Hermes board。

当前不是把一个 DAG JSON 直接交给 Hermes。Hermes CLI 暂无通用的 `import-dag` / `create-dag` 入口；DAG 能力通过 task parent 关系表达。`hermes kanban swarm` 可以一次创建图，但拓扑固定为 `workers -> verifier -> synthesizer`，不适合 Noetic workflow 的可变 stage graph。

运行前需要先准备一个 Hermes profile：

```bash
hermes profile create worker --clone --description "Runs Noetic workflow tasks using role skills from task context."
```

这个 profile 要能加载 NoeticAI Knowledge 套件；业务职责由任务正文中的 `noetic-data-agent` / `noetic-gen-agent` 约束，不再用 profile 名表达角色。

`--apply` 执行前会先调用 `hermes profile show worker`。如果 profile 不存在，脚本会在创建 Kanban task 前失败，并打印对应 `hermes profile create ...` 命令。`--dry-run` 不检查本机 profile。

一次完整运行是：

```bash
# 1. 初始化 kanban board，只需要做一次
hermes kanban init

# 2. 启动 gateway。dispatcher 默认在 gateway 里运行
hermes gateway start

# 3. 生成并提交 Noetic workflow 任务
python skills/noetic-workflow/scripts/noetic_workflow.py execute \
  --skill noetic-due-diligence \
  --company "小米科技有限责任公司" \
  --workspace dir:/absolute/path/to/noetic-run \
  --apply

# 4. 观察任务执行
hermes kanban watch
```

脚本的 `--apply` 模式按顺序执行 Hermes CLI：

```bash
hermes kanban create "<data title>" \
  --body "<data task body>" \
  --assignee worker \
  --skill noetic-company-profile \
  --workspace dir:/absolute/path/to/noetic-run \
  --json

hermes kanban create "<report title>" \
  --body "<gen task body>" \
  --assignee worker \
  --skill noetic-due-diligence \
  --parent <profile_task_id> \
  --parent <shareholder_task_id> \
  --parent <litigation_task_id> \
  --parent <financing_task_id> \
  --workspace dir:/absolute/path/to/noetic-run \
  --json
```

Hermes 后续自己接管：

1. 没有 parent 的 data task 进入 `ready`。
2. gateway dispatcher 认领 ready task，启动 `worker` profile。
3. worker 进程按任务正文里的 role skill 使用 data 角色，读取任务正文和父任务交接，不需要 shell 调 `hermes kanban show`。
4. data 角色完成后调用 `kanban_complete(summary=..., metadata=...)`。
5. 所有 parent 完成后，Hermes 自动把 report task 从 `todo` promoted 到 `ready`。
6. `worker` profile 被 dispatcher 启动，按 `noetic-gen-agent` role skill 读取父任务 handoff，生成最终报告并 complete。

所以通用脚本只负责“创建任务和依赖”；Hermes backend 负责“调度 worker profile、注入 kanban 工具、传递父任务结果”，角色职责由 role skill 承担。

## 对话入口触发

如果用户入口只在对话里，使用通用 workflow skill：

```text
skills/noetic-workflow/SKILL.md
```

这个 skill 不执行企业分析，只负责把自然语言请求转成 `noetic_workflow.py execute --apply`，并按用户选择使用 `planned` 或 `auto` 模式：

```bash
# planned（标准流程，默认）
python /path/to/noeticai-knowledge/skills/noetic-workflow/scripts/noetic_workflow.py execute \
  --mode planned \
  --skill noetic-due-diligence \
  --company "<用户输入的公司名>" \
  --workspace "dir:<本次运行目录>" \
  --apply

# auto（Hermes 自动拆图）
python /path/to/noeticai-knowledge/skills/noetic-workflow/scripts/noetic_workflow.py execute \
  --mode auto \
  --company "<用户输入的公司名>" \
  --skill noetic-due-diligence \
  --dispatch \
  --apply
```

用户在 Hermes 对话里说：

```text
/noetic-workflow 帮我对杭州XX科技有限公司跑企业尽调
```

orchestrator skill 做三件事：

1. 识别编排 workflow：企业尽调 -> `noetic-due-diligence`，投资分析 -> `noetic-investment-analysis`。
2. 识别目标公司和可选 workspace。
3. 调用 `skills/noetic-workflow/scripts/noetic_workflow.py execute --apply`，把创建出来的 task id 返回给用户。

不建议让普通 `noetic-due-diligence` skill 自己创建 Kanban 任务。报告卡片的职责是生成报告；编排 Kanban 是 orchestrator 职责。

Hermes 对话里也可以手工用 `/kanban create ...` 创建单个任务；但完整 Noetic workflow 需要多个 task、parent 依赖和固定 task body，用脚本生成更稳。

## 非目标

- 不让 Hermes 原生解析 Noetic `workflow.yaml`。
- 不把卡片内部强拆成纯 data / 纯 gen。
- 不做 eval/verifier。
- 不做可视化编排器。
- 不做多个角色 profile 或复杂 profile 白名单。

## 后续触发条件

- 如果最终报告频繁漏看父任务 evidence，再加 `verifier`。
- 如果某些前置卡片取数很重，再拆 `researcher` / `analyst`。
- 如果同一家公司要同时生成多个报告，再复用同一批 data artifact，创建多个 `gen` 子任务。
- 如果转换脚本稳定，再考虑接到 Hermes Custom Desktop。
