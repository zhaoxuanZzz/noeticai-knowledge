# Hermes Kanban Agent Plan

本文记录 NoeticAI Knowledge 与 Hermes Kanban/Profile 结合的执行方案。目标是复用 Hermes 已有的 profile、kanban task graph、worker dispatch 能力，让不同知识卡片按各自 workflow 执行。

## 结论

Hermes Kanban 固定的是任务图机制，不固定业务流程。

- `workflow.yaml` 继续作为 Noetic 的业务流程描述。
- Hermes Kanban 负责执行任务图、依赖、并发、重试和状态。
- Hermes Profile 负责 agent 角色、工具和 skill 边界。
- 不改 Hermes 原生能力，先加一个很薄的 Noetic 转换层，把 workflow 翻译成 kanban tasks。

## Agent 角色

先保留 3 个 profile，避免按业务场景提前拆死。

| Profile | 角色 | 职责 | 不做什么 |
| --- | --- | --- | --- |
| `data` | Data Agent | 执行数据取证子任务，记录来源、时间、缺失字段和原始发现 | 不写最终报告，不做业务结论 |
| `eval` | Eval Agent | 拆分任务、验收证据覆盖、检查冲突和缺口，决定是否可生成 | 不美化报告，不编事实 |
| `gen` | Gen Agent | 基于已验收证据和 eval notes 生成报告、摘要、文档草稿 | 不绕过 eval 重新取数或补事实 |

创建示例：

```bash
hermes profile create data --clone --description "General data agent: executes assigned evidence-gathering subtasks, records sources, timestamps, missing fields, and raw findings. It does not write final reports."

hermes profile create eval --clone --description "Eval agent: decomposes data work, verifies evidence coverage, checks conflicts and missing fields, and decides whether generation may proceed."

hermes profile create gen --clone --description "Gen agent: writes structured reports from verified evidence and evaluation notes. It must not invent facts."
```

## Skill 边界

每个 profile 有自己的 skills/config。不要默认让所有 agent 拥有所有 skill。

| Profile | 推荐 skill 范围 |
| --- | --- |
| `data` | 数据检索、QCC/wiki、网页搜索、证据记录 |
| `eval` | 证据覆盖、质量门禁、冲突检查、字段缺失检查 |
| `gen` | 报告生成、文档/PPT/结构化输出 |

`gen` 不应拥有完整取数 skill，避免绕过 eval；`data` 不应拥有完整报告生成 skill，避免职责混杂。

## 多 Data Agent

取数任务复杂时，不需要创建 `data1/data2/data3`。同一个 `data` profile 可以被多张 worker card 同时使用。

```bash
hermes kanban swarm "完成目标公司的尽调报告" \
  --worker data:"收集第一批关键事实和证据" \
  --worker data:"收集第二批关键事实和证据，避免重复第一批" \
  --worker data:"补充交叉验证、公开资料和缺失字段" \
  --verifier eval \
  --synthesizer gen
```

并发数量由 Hermes kanban 配置控制，例如：

```yaml
kanban:
  max_in_progress: 4
  max_in_progress_per_profile: 2
```

## Workflow 到 Kanban 的映射

Hermes 不直接执行 `skills/*/references/workflow.yaml`。Noetic 需要把 workflow 翻译为 kanban task graph。

映射规则：

| Noetic workflow | Hermes Kanban |
| --- | --- |
| `stage.id` | task title/body 中的阶段标识 |
| `stage.skills[]` | `hermes kanban create --skill ...` |
| `stage.inputs[]` | parent tasks |
| `stage.outputs[]` | task body 中要求产出的 artifact |
| `parallel: true` | 同阶段 tasks 不互相依赖 |
| `parallel: false` 或省略 | 同阶段 tasks 串行依赖 |
| data 类 stage | `--assignee data` |
| eval/quality gate stage | `--assignee eval` |
| generation/report stage | `--assignee gen` |

## 串行与并行

Kanban 用依赖关系表达串行/并行。

串行：

```text
t1 -> t2 -> t3 -> t4
```

并行：

```text
       -> t2
t1    -> t3
       -> t4
```

`parallel: true` 翻译成同一阶段任务共享前置 parent，但彼此不互为 parent。

## 执行入口

首版建议新增一个脚本，而不是修改 Hermes：

```text
scripts/workflow_to_kanban.py
```

职责：

1. 读取入口 skill 的 `references/workflow.yaml`。
2. 校验引用的 skill、artifact、quality gate。
3. 按 stage 生成 `hermes kanban create` 命令。
4. 用 parent 关系表达 stage 顺序和并行。
5. 输出 root task、worker tasks、eval task、gen task 的 ID。

示例命令形态：

```bash
python scripts/workflow_to_kanban.py \
  --skill noetic-due-diligence \
  --goal "完成目标公司的尽调报告" \
  --workspace scratch
```

## 非目标

- 不让 Hermes 原生解析 Noetic `workflow.yaml`。
- 不为每个业务场景创建固定 data profile。
- 不先做可视化编排器。
- 不把所有 skill 暴露给所有 profile。

## 后续触发条件

- 如果 `data` profile 成为瓶颈，再调 `kanban.max_in_progress_per_profile`。
- 如果某类取数长期稳定且工具集明显不同，再拆专业 data profile。
- 如果转换脚本稳定，再考虑在 Hermes Custom Desktop 里做可视化入口。
