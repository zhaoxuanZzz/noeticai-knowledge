# Runner Gate 旁路设计

## 1. 目标

将 Gate 的执行提示和状态控制从业务 skill 中移出，统一放入 runner 控制面。业务 skill 只描述业务输入、输出、字段、证据和缺口规则；runner 负责验收、审计、状态转换和下游解锁。

第一期覆盖：

- 单个有 Gate 的 skill，通过现有 atomic loop 执行；
- workflow 的 `delegate` 模式，包括普通 delegate 和启用 loop 的 delegate。

第一期不改变 Hermes `planned`、`auto`、宿主 hook 或 Gate 判定协议。

## 2. 设计原则

1. `card.yaml.gate` 保留业务特有的验收契约，不属于运行提示词。
2. data/gen 是 maker，只生成业务产物，不运行 Gate，也不声明 Gate 结果。
3. runner 是唯一执行 Gate 的控制面；子 agent 的完成声明不能解锁下游。
4. 继续复用 `check_node`、`check_final`、`gate-result.json` 和现有状态，不新增 Gate agent、DAG Gate 节点或新协议。
5. 普通 delegate 保留当前正式 handoff 路径；只有 loop 模式使用 attempt 隔离和通过后提升，避免把提示词解耦扩大成存储协议迁移。

## 3. 职责边界

### 3.1 业务 skill

业务 `SKILL.md` 保留：

- 输入、输出和字段含义；
- 关键 claim 和证据要求；
- 数据缺口、禁止编造和来源规则；
- 业务产物的建议结构。

业务 `SKILL.md` 删除：

- `check_artifact_gate.py` 命令；
- node/final Gate 的执行顺序；
- exit code、`blocked`、`needs_review` 等 runner 状态说明；
- “Gate 未通过不得完成”等依赖提示词自律的控制语句。

### 3.2 `card.yaml.gate`

`card.yaml.gate` 继续声明：

- required outputs 和 required metadata；
- evidence 与 semantic claim 契约；
- final Gate 的父产物要求；
- Judge rubric 等卡片专属验收配置。

本设计不修改 Gate schema。

### 3.3 data/gen maker

`cws-data-agent` 和 `cws-gen-agent`：

- 读取 runner 提供的 `run_id`、输入、父产物路径和输出路径；
- 生成 `handoff.json`、必要的 `evidence.json` 和报告；
- 返回业务产物摘要和 `evidence_gaps`；
- 不调用 checker，不返回自报的 Gate 状态。

### 3.4 runner

runner：

- 在 `complete` 边界直接调用 `check_node`，报告节点随后调用 `check_final`；
- 写独立的 `gate-result.json` 和带 attempt 编号的审计文件；
- 根据真实结果转换节点和 run 状态；
- 只有 `passed` 才使下游节点进入 ready；
- loop 模式通过后提升 candidate，失败时保留 attempt 和 finding。

`cws-workflow/SKILL.md` 可以保留 runner 调用说明，因为它属于控制面，不属于业务卡片。

## 4. 执行流

### 4.1 独立 skill

```text
loop init
  -> loop next 返回 maker context、attempt_dir 和 lease_id
  -> data/gen 写入 attempt_dir
  -> loop complete
  -> runner 执行 node Gate
  -> 如声明 final Gate，再执行 final Gate
  -> passed：提升到正式 artifact 目录
  -> failed：保留 attempt，并按现有策略 retry / needs_input / needs_review / exhausted
```

### 4.2 普通 workflow delegate

```text
delegate start
  -> data/gen 写入节点 handoff_path
  -> 父 agent 调用 delegate complete
  -> runner 执行 node Gate
  -> 报告节点由 runner 继续执行 final Gate
  -> passed：节点完成并解锁下游
  -> blocked / needs_review：不解锁下游
```

修复普通 delegate 的产物后，可再次调用 `delegate complete` 复验。第一期不为普通 delegate 引入 attempt/promotion。

### 4.3 workflow delegate loop

沿用独立 skill 的 attempt/promotion 语义，但节点依赖仍由 workflow runner 管理。Gate 通过和正式产物提升完成后，才解锁下游。

## 5. 任务上下文

delegate 任务提示只携带 maker 真正需要的信息：

- 目标公司和业务 skill；
- `run_id`；
- 输入及父产物；
- 输出 artifact；
- `handoff.json` 或 attempt 输出目录；
- 必须生成的业务文件和冻结知识库约束。

任务提示不再携带：

- node/final Gate 命令；
- Gate 的调用顺序；
- Gate exit code 和 runner 状态规则；
- 要求 maker 自行判断是否可以解锁下游的文字。

结构化节点中的 `node_gate`、`final_gate` 和 `handoff_path` 继续保留，供 runner 使用，不作为 maker 的执行提示。

## 6. 失败与审计

- `passed`：写 Gate 审计；普通 delegate 解锁下游，loop 先提升再解锁。
- `blocked`：不解锁；普通 delegate 允许修复后再次 `complete`。
- `needs_review`：暂停并等待人工复核，不由 maker 自行放行。
- loop 的 `retryable`、`needs_input`、`exhausted` 等状态沿用现有策略。
- Gate 缺失的普通 skill 保持原行为；atomic loop 仍只接受声明了 Gate 的 skill。
- Gate 审计继续与业务 `handoff.json` 分离。

## 7. 修改范围

### 7.1 代码与提示

- `skills/cws-workflow/scripts/workflow_planning.py`
  - 用纯产物上下文替换 `gate_rules`；
  - 保留 `run_id`、handoff/evidence 路径和业务文件要求；
  - 删除 Gate 命令和完成约束。
- `skills/cws-workflow/scripts/workflow_runtime_cli.py`
  - 将顶层说明改为 maker 只产出、父 agent 调用 `delegate complete`、runner 负责 Gate。
- `skills/cws-workflow/SKILL.md`
  - 说明 Gate 是 runner 旁路，不要求业务节点主动执行 checker。
- `skills/cws-data-agent/SKILL.md`
  - 删除主动运行 Gate、解释退出码和返回 Gate 结果的步骤。
- 声明 Gate 的业务 `skills/*/SKILL.md`
  - 删除 checker 命令、Gate 顺序和 runner 状态提示；
  - 保留输出、claims、evidence 与 `evidence_gaps` 业务要求。

`cws-gen-agent` 当前没有主动运行 Gate 的提示，不为对称性制造无必要改动。

### 7.2 明确不改

- `skills/*/card.yaml` 的 Gate 契约；
- `scripts/card_gate.py`；
- `scripts/check_artifact_gate.py`；
- semantic Gate、Judge adapter 和 rubric；
- delegate/atomic loop 状态协议；
- Hermes `planned`、`auto` 和 hook。

## 8. 验证

最小新增或调整测试：

1. `test_workflow.py` 断言 delegate prompt 不包含 `check_artifact_gate.py`、node/final Gate 命令或要求 maker 自行验收的文字。
2. 同一测试断言 prompt 仍包含 `run_id`、handoff 路径和需要的 evidence 文件信息。
3. 复用 `test_delegate_runner.py` 证明错误 handoff 被 runner 拦截且下游不 ready，通过后才解锁。
4. 复用 `test_atomic_loop.py` 证明 candidate 通过 Gate 后才提升。
5. 静态扫描业务 `SKILL.md`，确认不存在主动运行 checker 的命令。

实现后运行：

```bash
python3 -m unittest tests.integration.test_workflow
python3 -m unittest tests.integration.test_delegate_runner
python3 -m unittest tests.integration.test_atomic_loop
python3 scripts/validate_work_suite.py --target all .
python3 scripts/validate_work_suite.py --self-test
git diff --check
```

## 9. 验收标准

- 业务 `SKILL.md` 不再承担 Gate 的执行与状态控制。
- data/gen 只生成业务产物，不调用 Gate。
- 独立 skill 和 workflow delegate 都只能由 runner 的真实 Gate 结果完成验收。
- Gate 通过前，loop candidate 不被提升且 workflow 下游不被解锁。
- `card.yaml.gate`、Judge、审计格式和 `planned` 行为保持兼容。
