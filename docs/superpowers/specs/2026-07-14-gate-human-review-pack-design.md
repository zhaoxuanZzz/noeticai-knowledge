# Gate 人工审核包设计

日期：2026-07-14

状态：设计已确认；实现已落地（第一版仅审计，不自动 waive/解锁）

相关设计：`2026-07-13-gate-dataset-design.md`、`2026-07-14-runtime-judge-adapter-design.md`、`2026-07-14-atomic-skill-loop-tdd-design.md`

## 1. 背景

Gate 在确定性检查通过后可由 LLM Judge 返回 `needs_review`。当前产物以机器 JSON 为主（`needs-review.json`、`gate-result.json` 的 `judge.findings`），字段多为原因码、`artifact_path`、`evidence_refs`。人类无法在不翻 `handoff.json` / `evidence.json` / raw 的情况下理解「出了什么问题、证据是什么、该怎么判」。

本设计在保留机器真相的前提下，为离线评估与运行时真实任务提供同一套可读审核包。

## 2. 已确认决策

1. 离线评估与运行时共用同一渲染器与决策 schema。
2. 人工动作是「判 findings 对错 + 选整包动作」（放行 / 打回重跑 / 标能力缺口），第一版不在审核包内直接改 handoff。
3. 默认落盘 Markdown（`review.md`）+ 决策模板（`review-decision.json`）；CLI 打印同一份内容。
4. 第一版只记录决策与审计，不自动 `waive` 解锁下游、不自动触发重跑。

## 3. 目标与非目标

### 3.1 目标

- 人打开一份材料即可核对 findings，无需拼读多个机器 JSON。
- 机器审计文件保持完整、可回归。
- 决策结构化、可校验、可与生成时的输入哈希绑定。
- 零额外依赖；无网络也能从本地产物生成审核包。

### 3.2 非目标

- 交互式问答 CLI 或 Web UI。
- 自动改写 `handoff.json` / `evidence.json`。
- 自动 `waive` 改写 `gate-result` 或解锁下游节点。
- 飞书 / 邮件通知。
- 为 `blocked` / `passed` 强制生成审核包（`blocked` 已有明确错误信息）。

## 4. 职责边界与文件布局

### 4.1 原则

- **机器真相**：`gate-result.json`、离线 `needs-review.json` / `results.json` 继续给程序用。
- **人读材料**：`review.md` 由共享渲染器生成，不手改。
- **人的结论**：写入 `review-decision.json`；与 `review.md` 同目录。

### 4.2 运行时（公司 KB）

```text
artifacts/<run-id>/<skill-id>/
├── handoff.json
├── evidence.json
├── gate-result.json
├── review.md                 # needs_review 时自动生成
└── review-decision.json      # 模板；人填完后 status=completed
```

仅当 gate 决策为 `needs_review` 时生成审核包。

### 4.3 离线评估

```text
.scratch/gate-eval/<eval-id>/
├── results.json
├── report.md
├── needs-review.json
└── reviews/
    └── <case_id>/
        ├── review.md
        └── review-decision.json
```

`report.md` 摘要表增加指向 `reviews/<case_id>/review.md` 的链接或路径列。

### 4.4 共享模块

| 路径 | 职责 |
| --- | --- |
| `scripts/gate_review.py` | 组装上下文、原因码中文表、渲染 Markdown、写/校验决策模板 |
| `scripts/render_gate_review.py` | CLI：`--handoff-dir` / `--eval-dir`；默认写盘，`--stdout` 打印 |

## 5. `review.md` 内容结构

人打开审核包应能不翻其他 JSON 完成判断。固定章节：

```markdown
# Gate 人工审核 · <subject> · <skill>

## 摘要
- 决策：needs_review
- 置信度 / 模型 / rubric
- run_id 或 case_id / 检查时间
- 你需要做的事：核对 findings → 填写 review-decision.json

## Finding N：<中文标题>
- 原因码：`material_risk_omitted`
- 人话：…
- 产物摘录：`artifacts.…` → 当前值或「缺失」
- 相关证据：id / field / value / source_ref
- 建议动作：reject_rerun | waive | capability_gap

## 决策填写说明
（短表，指向同目录 review-decision.json）
```

### 渲染规则

- 原因码 → 中文标题与说明使用仓库内白名单映射表。
- 按 `artifact_path` 从 handoff 取最小摘录；按 `evidence_refs` 展开 evidence。
- 只引用已最小化字段值，不贴受限全文或密钥。
- 离线评估额外展示 `expected_decision` 与 `actual_decision`，便于判断 Judge 是否标对。

## 6. `review-decision.json` 与动作语义

### 6.1 模板

```json
{
  "schema_version": 1,
  "status": "pending",
  "reviewed_at": null,
  "reviewer": null,
  "gate_input_hash": "sha256:...",
  "findings": [
    {
      "reason": "material_risk_omitted",
      "artifact_path": "artifacts.risk_flags",
      "verdict": null,
      "note": null
    }
  ],
  "action": null,
  "action_reason": null
}
```

`gate_input_hash` 在生成模板时写入。离线评估优先复用案例结果里的 `input_hash`；运行时对同目录最小化后的 `handoff.json` + `evidence.json` + `gate-result.judge` 做稳定 SHA-256。重渲染时必须用同一算法，以便校验决策是否过期。

### 6.2 Finding 级 `verdict`

| 值 | 含义 |
| --- | --- |
| `confirm` | 该条 finding 判对 |
| `reject` | 误报，该条不算问题 |
| `uncertain` | 信息不足 |

### 6.3 整包 `action`

| 值 | 含义 | 第一版效果 |
| --- | --- | --- |
| `waive` | 带原因放行 | 只记录审计；不改 gate-result、不解锁下游 |
| `reject_rerun` | 打回重跑 | 只记录；需人工/后续 runner 新建 attempt 或 run |
| `capability_gap` | 登记能力缺口 | 只记录；离线侧可后续写入 expected |

### 6.4 校验

- `status` 从 `pending` → `completed` 时：每个 finding 有 `verdict`；`action` 与非空 `action_reason` 必填。
- `gate_input_hash` 必须与生成时一致；产物变更则决策作废，需重新渲染。
- `waive` 不能覆盖确定性 `blocked`（审核包本就不为 blocked 生成）。

### 6.5 与 atomic skill loop

决策文件是人工审计面。真正的 waive 解锁 / reject 触发重跑留给后续 runner 接入；本切片不实现自动副作用。与 atomic loop 设计中「`needs_review` 为终态、新 run 重放」一致。

## 7. 接入点

| 入口 | 行为 |
| --- | --- |
| `check_artifact_gate.py` | 决策为 `needs_review` 时写 handoff 同目录审核包 |
| `evaluate_gate_dataset.py` | 对 `actual_decision == needs_review` 写 `reviews/<case_id>/`；更新 `report.md` |
| `render_gate_review.py` | 独立重生成或仅打印 |

## 8. 实现顺序

1. `gate_review`：渲染、原因码中文表、决策 schema 校验。
2. CLI + 单测（优先复用 `judge-risk-omitted` / final 同类 fixture）。
3. 接入 `check_artifact_gate` 与 `evaluate_gate_dataset`。
4. 文档：README 或 gate-dataset 设计补用法一行。

## 9. 验收标准

- 人不打开 `needs-review.json` 也能理解问题与证据。
- 同一渲染器服务运行时与离线评估。
- 机器 JSON 仍完整保留。
- 决策模板可校验；hash 不一致拒绝 `completed`。
- 无密钥、无网络即可从本地产物生成审核包。

## 10. 测试边界

| 层 | 内容 |
| --- | --- |
| 单元 | 原因码映射、finding 摘录、决策 pending→completed 校验、hash 失配拒绝 |
| 集成 | 对 fixture 跑渲染；eval 输出含 `reviews/<case_id>/review.md` |
| 不测 | 真实模型、自动解锁、宿主 UI |
