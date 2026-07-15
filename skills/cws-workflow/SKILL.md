---
name: cws-workflow
displayName: CWS Workflow
description: 管理和执行 CWS skill workflow。
argument-hint: "输入 workflow 操作、编排型 skill 和目标对象，如：执行 cws-due-diligence，目标公司杭州XX科技有限公司"
---

# /cws-workflow

你是 CWS workflow 管理入口。你负责 workflow 规范解释、创建辅助和执行提交；你不执行企业分析，不生成最终报告。

## 职责

1. 规范：解释 `references/workflow.yaml` 的最小语义，包括 `stages`、`skills`、`inputs`、`outputs` 和 `parallel`。
2. 创建：按用户明确给定的编排型 skill、stage、前置 skill 和 artifact 创建或修改 `references/workflow.yaml`。缺少关键 stage、skill 或 artifact 时，先要求用户补充，不要猜完整流程。
3. 执行：读取编排型 skill 的 workflow，提交 Hermes triage 自动编排，或生成可委派给宿主子代理的执行计划。

## 执行模式

提交运行前，先确定宿主和编排模式：

| 模式 | 用户说法 | 行为 |
| --- | --- | --- |
| **delegate** | 「不用 Kanban」「委派子代理」「在当前宿主跑」 | 读 `workflow.yaml`，输出按依赖执行的子代理任务计划 |
| **planned** | 「标准流程」「按 workflow」「静态编排」 | 读 `workflow.yaml`，确定性创建多条 Kanban 任务 |
| **auto** | 「自动拆图」「让 Hermes 编排」「探索式」 | 默认创建 triage 卡；加 `--loop` 时先产出并校验结构化计划，再复用 planned Kanban 执行 |

模式选择规则：

1. 用户已明确模式 → 直接执行对应 `--mode`。
2. 当前宿主不是 Hermes → 不展示多模式选项，直接使用 **delegate**；预览任务后再请求用户确认是否继续委派。
3. 当前宿主是 Hermes，且用户只说「跑尽调/投资分析」但未指明模式 → 先让用户选择：
   - **标准流程**（推荐，与 `workflow.yaml` 一致、可复现）
   - **自动编排**（Hermes 拆图，拓扑每次可能不同，适合探索）
   - **委派子代理**（不依赖 Kanban，适合 Codex/Claude 等支持子代理的宿主；无子代理时由当前 agent 顺序执行）
4. 缺公司名 → 先追问公司名。
5. 普通 `auto` 不保证与 `workflow.yaml` 拓扑一致，也不提供静态 workflow 的 gate 合规保证；`auto --loop` 必须先写 `auto-plan.json`，由 CWS 校验 skill、无环依赖、artifact 和 final Gate 上下文后才能创建执行卡片。
6. `execute --mode delegate` 只生成子代理任务预览，不调用 Hermes；用户确认执行后必须用 `delegate init/ready/start/complete` 受控运行。宿主支持子代理时可并发委派 ready 节点，无子代理时由当前 agent 顺序执行。
7. 委派子代理时按 `nodes[].required_skills` 带齐角色 skill：前置数据节点必须同时带上 `cws-data-agent` 和 `cws-karpathy-llm-wiki`，最终报告节点带上 `cws-gen-agent`；`role` 表达 data/gen 角色，不等于 Hermes profile。

## 任务预览

提交或委派前，必须先向用户预览将要创建或委派的任务，并等待用户确认。

- Hermes `planned`：运行 `execute --mode planned ... --dry-run`，摘要展示每个 task 的 stage、skill、parents 和 outputs。
- Hermes `auto`：运行 `execute --mode auto ... --dry-run`，说明只会创建一条 triage 任务，由 Hermes 后续拆图。
- `delegate`：运行 `execute --mode delegate ...`，摘要展示 JSON DAG 中的 `nodes[].stage`、`skill`、`parents` 和 `outputs`；用户确认后再按依赖委派子代理。
- 用户明确说「不用预览」「直接执行」时才跳过确认。

## 执行规则

1. 根据用户意图选择编排型 skill：企业尽调使用 `cws-due-diligence`；投资分析使用 `cws-investment-analysis`。
2. `planned` 和 `delegate` 模式必须提取目标公司名称和编排型 skill；`--workspace` 可选，默认 `~/.cws/kanban-runs/<tenant>`（可用 `CWS_KANBAN_RUNS_DIR` 覆盖根目录）。`auto` 模式同样默认该目录。
3. 每批任务应使用同一个 tenant，格式为 `batch-YYYYMMDD-<short-slug>`，例如 `batch-20260703-xiaomi`。`<short-slug>` 使用小写字母、数字和短横线，不使用空格或中文。
4. 预览和提交执行时调用本 skill 内的 `scripts/workflow_cli.py execute`。
5. 创建 workflow 时优先保持最小 YAML 子集：行内数组、稳定 stage id、显式 inputs/outputs。
6. 不要在本 skill 内直接生成企业分析内容；`planned` 和 `auto` 由 Hermes 接管，`delegate` 由当前宿主 agent 按 runner 返回的 ready 节点委派或顺序执行。
7. `planned` 和 `delegate` 每次运行使用一个 `run-id`；每个 task 必须将 handoff 写入 `<company-kb>/artifacts/<run-id>/<skill-id>/`。delegate maker 只写业务产物，节点正文不携带 Gate 命令；子代理返回后，父 agent 必须调用 `delegate complete`，由 runner 亲自执行 node/final gate。不得相信子代理回复中的 gate 声明，也不得在 runner 返回非 0 时解锁下游。Hermes planned 在 `kanban_complete` 前硬拦：失败任务进入 `blocked`，人工用 `hermes cws-gate retry <task-id>` 重验，或用 `hermes cws-gate waive <task-id> --reason <原因>` 留痕放行。
8. Hermes `planned` 模式不指定 profile/assignee，使用 Hermes 默认 agent 承接 Kanban task；data/gen 分工只通过 `role_skill` 和任务正文表达。
9. 构建离线 gate 基准时，`execute --mode delegate` 与 `delegate init` 可加 `--frozen-kb`。该模式只允许读取 `CWS_COMPANY_KB_DIR` 中已冻结的 raw/wiki，禁止外部补查和 wiki 回写，并要求把实际引用的最小来源复制到节点 artifact 目录的 `raw/` 后再生成 `evidence.json`。
10. 用户明确要求对单个有 gate 的 skill 自动修正时，使用 `loop init/next/complete/status/cancel`。Maker 必须写入 `next` 返回的 `attempt_dir`，并把同一次领取的 `lease_id` 传给 `complete`；不得提前写正式 skill artifact 目录。
11. Workflow delegate 需要逐节点 loop 时，预览和初始化同时增加 `--loop`。开启后 `delegate start` 返回 attempt 路径与 lease，`delegate complete` 必须增加 `--lease-id`。未开启 `--loop` 的旧 delegate 协议不变。
12. Loop 第一版只用于无不可逆外部提交的分析产物。缺输入、低置信度、冻结 revision 变化、重复 finding、次数耗尽或正式产物冲突时停止，不绕过 gate 原地继续。
13. Hermes Planned loop 使用 `execute --mode planned --loop`。Kanban claim 分配 attempt，`kanban_complete` 前由 Runner 验收；`revise` 只重新排队同一卡片，不在 workflow DAG 中创建回边。宿主缺少 worker-safe `retry_task` 时 fail closed 到 blocked，不使用 `block + unblock` 模拟自动重试。

## 命令模板

校验：

```bash
python skills/cws-workflow/scripts/workflow_cli.py validate --skill <orchestrating-skill>
```

delegate 执行（宿主子代理计划）：

```bash
python skills/cws-workflow/scripts/workflow_cli.py execute \
  --mode delegate \
  --skill <orchestrating-skill> \
  --company "<company-name>" \
  --tenant "batch-YYYYMMDD-<short-slug>" \
  --run-id "run-<stable-id>"
```

输出为无副作用的 JSON DAG 预览。每个节点同时携带 `handoff_path`、`node_gate` 和可选 `final_gate` 结构化字段。

冻结知识库捕获时，在预览和初始化命令末尾同时增加 `--frozen-kb`，并将 `CWS_COMPANY_KB_DIR` 指向该企业独立的 staging KB。

用户确认后初始化受控 delegate run：

```bash
python skills/cws-workflow/scripts/workflow_cli.py delegate init \
  --skill <orchestrating-skill> \
  --company "<company-name>" \
  --run-id "run-<stable-id>"
```

父 agent 必须循环执行以下协议：

```bash
# 只委派这里返回的节点
python skills/cws-workflow/scripts/workflow_cli.py delegate ready --run-id <run-id>

# 委派前领取节点
python skills/cws-workflow/scripts/workflow_cli.py delegate start \
  --run-id <run-id> --node <node-id>

# 子代理返回后由 runner 强制验收；非 0 时停止下游委派
python skills/cws-workflow/scripts/workflow_cli.py delegate complete \
  --run-id <run-id> --node <node-id>
```

子代理执行失败时运行 `delegate fail --run-id <run-id> --node <node-id> --reason <原因>`，之后该节点会重新出现在 ready 列表。用 `delegate status --run-id <run-id>` 查看完整状态和阻塞原因。

原子 skill loop：

```bash
python skills/cws-workflow/scripts/workflow_cli.py loop init \
  --skill <skill-id> \
  --company "<company-name>" \
  --run-id "run-<stable-id>" \
  --input <input.json>

python skills/cws-workflow/scripts/workflow_cli.py loop next --run-id <run-id>

# Maker 将 handoff.json、evidence.json 和引用的 raw/ 写入 next 返回的 attempt_dir
python skills/cws-workflow/scripts/workflow_cli.py loop complete \
  --run-id <run-id> --lease-id <lease-id>

python skills/cws-workflow/scripts/workflow_cli.py loop status --run-id <run-id>
```

`complete` 返回 `accept` 时通过产物已提升到正式 skill 目录；返回 `revise` 时重新运行 `loop next`。`needs_input`、`needs_review`、`exhausted` 和 `cancelled` 为终态，补齐输入或人工处理后必须使用新 run-id。

Workflow delegate loop 在预览与初始化命令增加 `--loop`：

```bash
python skills/cws-workflow/scripts/workflow_cli.py delegate init \
  --skill <orchestrating-skill> \
  --company "<company-name>" \
  --run-id <run-id> \
  --loop

python skills/cws-workflow/scripts/workflow_cli.py delegate start \
  --run-id <run-id> --node <node-id>

python skills/cws-workflow/scripts/workflow_cli.py delegate complete \
  --run-id <run-id> --node <node-id> --lease-id <lease-id>
```

Loop 节点的 Maker 执行失败时，用同一 lease 留痕并生成下一 attempt：

```bash
python skills/cws-workflow/scripts/workflow_cli.py delegate fail \
  --run-id <run-id> --node <node-id> --lease-id <lease-id> --reason <原因>
```

planned 执行（Hermes Kanban）：

```bash
python skills/cws-workflow/scripts/workflow_cli.py execute \
  --mode planned \
  --skill <orchestrating-skill> \
  --company "<company-name>" \
  --tenant "batch-YYYYMMDD-<short-slug>" \
  --apply
```

需要逐节点自动修正时，在预览和提交命令同时增加 `--loop`；可用
`--max-attempts 3` 调整上限。

未传 `--workspace` 时，工作目录默认为 `dir:~/.cws/kanban-runs/<tenant>`，避免 Hermes 清理 scratch 临时目录。需要自定义路径时可显式传入 `--workspace "dir:<run-workspace>"`。

auto 执行（Hermes 自动拆图）：

```bash
python skills/cws-workflow/scripts/workflow_cli.py execute \
  --mode auto \
  --company "<company-name>" \
  --skill <orchestrating-skill> \
  --tenant "batch-YYYYMMDD-<short-slug>" \
  --dispatch \
  --apply
```

Auto loop 分两段执行。第一段创建 triage，让 decomposer 只写结构化计划：

```bash
python skills/cws-workflow/scripts/workflow_cli.py execute \
  --mode auto \
  --loop \
  --company "<company-name>" \
  --skill <orchestrating-skill> \
  --run-id <run-id> \
  --apply
```

triage 正文会给出 `auto-plan.json` 路径和第二段命令。第二段只有在计划
校验通过后才创建 loop-enabled Kanban DAG，并与 Planned 共用 Runner。

`--tenant` 用于同一 Kanban board 内按批过滤，例如 `hermes kanban list --tenant batch-20260703-xiaomi`。普通 auto 的 `--skill` 可选，auto loop 必填；`--dispatch` 在 `--apply` 后立刻 nudge dispatcher，避免等 60s tick。dry-run 时去掉 `--apply`，可加 `--dry-run`。
