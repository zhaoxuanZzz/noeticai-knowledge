# Runner Gate 旁路实施方案

**目标：** 将 Gate 执行从业务提示词移到现有 runner，覆盖独立 atomic loop 与 workflow `delegate`，不改变 Gate 契约、判定器、状态协议或 Hermes `planned`。

**设计：** `docs/superpowers/specs/2026-07-14-runner-gate-sidecar-design.md`

**实现约束：** 当前目标文件已有未提交改动。实施时必须在现状上做窄修改，不回退业务字段、semantic Gate、冻结知识库或 atomic loop 相关工作；不得批量暂存无关文件。

---

## Task 1：先锁定 delegate prompt 的新边界

**Files:**

- Modify: `tests/integration/test_workflow.py`
- Test: `tests.integration.test_workflow.WorkflowIntegrationTest`

- [ ] 将 `test_delegate_plan_carries_one_run_id_and_gate_commands` 改为验证“结构化 Gate 元数据保留、maker prompt 不含 Gate 命令”。
- [ ] 对首个 data 节点和最终 report 节点断言：
  - prompt 包含同一 `run_id`；
  - prompt 包含对应 `handoff.json` 路径；
  - semantic 卡片仍提示生成 `evidence.json`；
  - prompt 不包含 `check_artifact_gate.py`、`--mode node`、`--mode final`、`node gate` 或“门禁未通过不得完成”。
- [ ] 同时断言节点结构中的 `node_gate`、`final_gate` 与 `handoff_path` 保持不变，证明只移除 maker 提示，不删除 runner 元数据。
- [ ] 运行单测并确认新断言在实现前失败：

```bash
python3 -m unittest tests.integration.test_workflow -v
```

Expected: 仅新 prompt 边界断言失败。

## Task 2：把 task body 收缩为纯交付上下文

**Files:**

- Modify: `skills/cws-workflow/scripts/workflow_planning.py`
- Modify: `skills/cws-workflow/scripts/workflow_runtime_cli.py`
- Test: `tests/integration/test_workflow.py`

- [ ] 在 `task_body()` 删除本地 `node_gate`、`final_gate` 和 `gate_rules` 文本构造。
- [ ] 用一个最小 `delivery_rules` 文本保留：
  - `run_id`；
  - handoff 目标路径；
  - handoff 顶层 `run_id`；
  - `card.yaml` 输出约定；
  - 卡片要求 claim evidence 时生成同目录 `evidence.json`。
- [ ] 不在 maker prompt 中出现 Gate 命令、Gate 顺序、退出码或 runner 状态。
- [ ] 保留 data/gen 业务分工和现有 `frozen_rules`，不改冻结知识库语义。
- [ ] 将 `command_execute_delegate()` 的顶层 `instructions` 改为：maker 只写产物；父 agent 在 maker 返回后调用 `delegate complete`；runner 自动执行 node/final Gate。不要要求 report maker 自行运行 final Gate。
- [ ] 不改 `structured_delegate_nodes()` 的 `node_gate`、`final_gate`、`handoff_path` 字段。
- [ ] 运行：

```bash
python3 -m unittest tests.integration.test_workflow -v
```

Expected: Task 1 的新断言通过，冻结知识库和 DAG 测试继续通过。

## Task 3：让 data agent 只负责产物

**Files:**

- Modify: `skills/cws-data-agent/SKILL.md`
- Modify: `tests/integration/test_workflow.py`

- [ ] 将 data agent 的产物规则改为使用任务或 runner 提供的输出路径，并覆盖业务 card 的 outputs、evidence 与 `evidence_gaps` 要求。
- [ ] 删除主动调用 `scripts/check_artifact_gate.py` 的命令块。
- [ ] 删除解释 Gate exit code、禁止解锁下游和返回 Gate 结果的步骤。
- [ ] 输出格式保留 handoff 路径，删除 checker/Gate 结果；保留 wiki 写回状态、来源、数据时间和 `evidence_gaps`。
- [ ] 在 `test_workflow.py` 增加一个窄静态断言：`cws-data-agent/SKILL.md` 不包含 `check_artifact_gate.py`。

Expected: data agent 仍知道写什么、写到哪里，但不再承担验收。

## Task 4：清理七个业务 skill 的运行时 Gate 提示

**Files:**

- Modify: `skills/cws-company-basic-info/SKILL.md`
- Modify: `skills/cws-company-profile/SKILL.md`
- Modify: `skills/cws-shareholder-structure/SKILL.md`
- Modify: `skills/cws-litigation-risk/SKILL.md`
- Modify: `skills/cws-financing-history/SKILL.md`
- Modify: `skills/cws-due-diligence/SKILL.md`
- Modify: `skills/cws-investment-analysis/SKILL.md`
- Modify: `tests/integration/test_workflow.py`

- [ ] 将“按 `gate` 执行”改为“按输入、输出、规则和证据约定执行”，避免业务提示把 Gate 当执行步骤。
- [ ] 删除 data agent、run 目录、checker 命令、node/final 顺序、exit code、`needs_review` 与 Judge 状态控制描述。
- [ ] 保留以下业务要求：
  - 输出字段和建议结构；
  - 关键 claim 必须有 evidence；
  - `evidence_gaps`；
  - 司法风险否定结论所需的完整检索覆盖；
  - 报告卡只综合父产物，不重新取数；
  - 报告所需 claim-level evidence。
- [ ] 不修改任何 `card.yaml`。
- [ ] 增加一个表驱动静态测试，扫描上述七个业务 skill，断言不包含 `check_artifact_gate.py`、`--mode final`、`node gate` 或“门禁未通过不得完成”。
- [ ] 不修改 `cws-gen-agent/SKILL.md`：当前没有主动运行 Gate 的提示。

Expected: 业务 skill 仍完整描述合格业务产物，但不再描述验收执行器。

## Task 5：把 runner 旁路写回控制面说明

**Files:**

- Modify: `skills/cws-workflow/SKILL.md`
- Test: `tests/integration/test_delegate_runner.py`
- Test: `tests/integration/test_atomic_loop.py`

- [ ] 在现有 delegate 规则中明确：
  - maker prompt 不携带 Gate 命令；
  - 父 agent 必须调用 `delegate complete`；
  - `complete` 内部自动执行 node/final Gate；
  - 只有 runner 返回 `passed` 才可继续 ready 节点。
- [ ] 保留现有 atomic loop、`--frozen-kb`、Hermes planned retry/waive 说明，不重写其协议。
- [ ] 不修改 `delegate_runner.py`、`atomic_loop.py` 或 checker；现有实现已经位于正确硬边界。
- [ ] 运行：

```bash
python3 -m unittest tests.integration.test_delegate_runner -v
python3 -m unittest tests.integration.test_atomic_loop -v
```

Expected: 错误 handoff 仍被拦截、下游不 ready；修复后可复验；loop 仅在通过后提升。

## Task 6：全量校验与差异审查

- [ ] 运行目标测试：

```bash
python3 -m unittest tests.integration.test_workflow
python3 -m unittest tests.integration.test_delegate_runner
python3 -m unittest tests.integration.test_atomic_loop
```

- [ ] 运行仓库静态契约校验：

```bash
python3 scripts/validate_work_suite.py --target all .
python3 scripts/validate_work_suite.py --self-test
```

- [ ] 扫描残留的业务 Gate 执行提示：

```bash
rg -n 'check_artifact_gate|--mode (node|final)|node gate|终局 gate|门禁未通过不得完成' \
  skills/cws-company-basic-info/SKILL.md \
  skills/cws-company-profile/SKILL.md \
  skills/cws-shareholder-structure/SKILL.md \
  skills/cws-litigation-risk/SKILL.md \
  skills/cws-financing-history/SKILL.md \
  skills/cws-due-diligence/SKILL.md \
  skills/cws-investment-analysis/SKILL.md \
  skills/cws-data-agent/SKILL.md
```

Expected: 无匹配。`cws-workflow/SKILL.md` 与 runner 源码允许出现 Gate，因为它们属于控制面。

- [ ] 运行：

```bash
git diff --check
git diff -- \
  skills/cws-workflow/scripts/workflow_planning.py \
  skills/cws-workflow/scripts/workflow_runtime_cli.py \
  skills/cws-workflow/SKILL.md \
  skills/cws-data-agent/SKILL.md \
  skills/cws-*/SKILL.md \
  tests/integration/test_workflow.py
```

- [ ] 人工确认差异没有回退当前工作区里的业务字段、semantic Gate、冻结知识库或 atomic loop 改动。
- [ ] 只有用户明确要求提交时，才精确暂存本方案涉及的文件；不要使用 `git add .`。

## 完成标准

- maker prompt 和业务/data skill 不再要求主动运行 Gate。
- `card.yaml.gate` 与结构化节点 Gate 元数据保持不变。
- 独立 skill 和 delegate 的真实完成边界仍是 runner。
- Gate 失败继续阻断下游，loop 失败 candidate 不提升。
- `planned`、`auto` 和 Hermes hook 没有行为变化。
