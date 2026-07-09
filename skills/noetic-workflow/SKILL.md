---
name: noetic-workflow
displayName: Noetic Workflow
description: 管理和执行 Noetic skill workflow。
argument-hint: "输入 workflow 操作、编排型 skill 和目标对象，如：执行 noetic-due-diligence，目标公司杭州XX科技有限公司"
---

# /noetic-workflow

你是 Noetic workflow 管理入口。你负责 workflow 规范解释、创建辅助和执行提交；你不执行企业分析，不生成最终报告。

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
| **auto** | 「自动拆图」「让 Hermes 编排」「探索式」 | 创建单条 triage 卡，由 Hermes `kanban_decomposer` 自动拆图 |

模式选择规则：

1. 用户已明确模式 → 直接执行对应 `--mode`。
2. 当前宿主不是 Hermes → 不展示多模式选项，直接使用 **delegate**；预览任务后再请求用户确认是否继续委派。
3. 当前宿主是 Hermes，且用户只说「跑尽调/投资分析」但未指明模式 → 先让用户选择：
   - **标准流程**（推荐，与 `workflow.yaml` 一致、可复现）
   - **自动编排**（Hermes 拆图，拓扑每次可能不同，适合探索）
   - **委派子代理**（不依赖 Kanban，适合 Codex/Claude 等支持子代理的宿主；无子代理时由当前 agent 顺序执行）
4. 缺公司名 → 先追问公司名。
5. `auto` 模式不保证与 `workflow.yaml` 拓扑一致；向用户说明这一点。
6. `delegate` 模式只生成子代理任务计划，不调用 Hermes；宿主支持子代理时按依赖委派，`parallel: true` 的 ready 节点可并发。
7. 委派子代理时按 `nodes[].required_skills` 带齐角色 skill：前置数据节点必须同时带上 `noetic-data-agent` 和 `noetic-karpathy-llm-wiki`，最终报告节点带上 `noetic-gen-agent`；`role` 表达 data/gen 角色，不等于 Hermes profile。

## 任务预览

提交或委派前，必须先向用户预览将要创建或委派的任务，并等待用户确认。

- Hermes `planned`：运行 `execute --mode planned ... --dry-run`，摘要展示每个 task 的 stage、skill、parents 和 outputs。
- Hermes `auto`：运行 `execute --mode auto ... --dry-run`，说明只会创建一条 triage 任务，由 Hermes 后续拆图。
- `delegate`：运行 `execute --mode delegate ...`，摘要展示 JSON DAG 中的 `nodes[].stage`、`skill`、`parents` 和 `outputs`；用户确认后再按依赖委派子代理。
- 用户明确说「不用预览」「直接执行」时才跳过确认。

## 执行规则

1. 根据用户意图选择编排型 skill：企业尽调使用 `noetic-due-diligence`；投资分析使用 `noetic-investment-analysis`。
2. `planned` 和 `delegate` 模式必须提取目标公司名称和编排型 skill；`--workspace` 可选，默认 `~/.noeticai/kanban-runs/<tenant>`（可用 `NOETICAI_KANBAN_RUNS_DIR` 覆盖根目录）。`auto` 模式同样默认该目录。
3. 每批任务应使用同一个 tenant，格式为 `batch-YYYYMMDD-<short-slug>`，例如 `batch-20260703-xiaomi`。`<short-slug>` 使用小写字母、数字和短横线，不使用空格或中文。
4. 预览和提交执行时调用本 skill 内的 `scripts/noetic_workflow.py execute`。
5. 创建 workflow 时优先保持最小 YAML 子集：行内数组、稳定 stage id、显式 inputs/outputs。
6. 不要在本 skill 内直接生成企业分析内容；`planned` 和 `auto` 由 Hermes 接管，`delegate` 由当前宿主 agent 按预览 DAG 委派或顺序执行。
7. Hermes `planned` 模式不指定 profile/assignee，使用 Hermes 默认 agent 承接 Kanban task；data/gen 分工只通过 `role_skill` 和任务正文表达。

## 命令模板

校验：

```bash
python skills/noetic-workflow/scripts/noetic_workflow.py validate --skill <orchestrating-skill>
```

delegate 执行（宿主子代理计划）：

```bash
python skills/noetic-workflow/scripts/noetic_workflow.py execute \
  --skill <orchestrating-skill> \
  --company "<company-name>" \
  --tenant "batch-YYYYMMDD-<short-slug>"
```

输出为 JSON DAG。每个 `nodes[].prompt` 是可直接交给子代理的任务正文；`nodes[].required_skills` 指定该节点必须带上的 skill；`parents` 全部完成后该节点才可执行。

planned 执行（Hermes Kanban）：

```bash
python skills/noetic-workflow/scripts/noetic_workflow.py execute \
  --mode planned \
  --skill <orchestrating-skill> \
  --company "<company-name>" \
  --tenant "batch-YYYYMMDD-<short-slug>" \
  --apply
```

未传 `--workspace` 时，工作目录默认为 `dir:~/.noeticai/kanban-runs/<tenant>`，避免 Hermes 清理 scratch 临时目录。需要自定义路径时可显式传入 `--workspace "dir:<run-workspace>"`。

auto 执行（Hermes 自动拆图）：

```bash
python skills/noetic-workflow/scripts/noetic_workflow.py execute \
  --mode auto \
  --company "<company-name>" \
  --skill <orchestrating-skill> \
  --tenant "batch-YYYYMMDD-<short-slug>" \
  --dispatch \
  --apply
```

`--tenant` 用于同一 Kanban board 内按批过滤，例如 `hermes kanban list --tenant batch-20260703-xiaomi`。`--skill` 在 auto 模式下可选；`--dispatch` 在 `--apply` 后立刻 nudge dispatcher，避免等 60s tick。dry-run 时去掉 `--apply`，可加 `--dry-run`。
