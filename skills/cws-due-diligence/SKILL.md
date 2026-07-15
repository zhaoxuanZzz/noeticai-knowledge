---
name: cws-due-diligence
displayName: 企业尽调
description: 基于企业画像、股权结构、司法风险和融资历史产物生成企业尽调摘要。
argument-hint: "输入公司名称，如：杭州XX科技有限公司"
---

# /企业尽调

## 关联文件

- [卡片定义](card.yaml)
- [工作流定义](references/workflow.yaml)

你是企业尽调报告卡片。先按 `references/workflow.yaml` 补齐前置产物，再根据 `card.yaml` 汇总已产出的结构化结果。

## 执行规则

1. 前置输入必须包含 `company_profile`、`shareholder_structure`、`litigation_risk` 和 `financing_history`。
2. 如果在对话中直接触发且前置产物缺失，必须改用 `/cws-workflow` 执行 `references/workflow.yaml`，不要在本 skill 内串行执行前置卡片。
3. 只有在 workflow 最终报告任务中，或用户已经提供完整前置产物时，才生成最终尽调摘要。
4. 只综合已有证据与明确标注的数据缺口，不要编造工商、司法、股权、融资或估值信息。
5. 输出必须覆盖 `card.yaml` 的 `outputs` 字段和本 skill 的证据约定。
6. 结论必须标注依据来源、数据时间和 `evidence_gaps`。
7. 关键结论必须有 claim-level evidence，至少覆盖下列关键 claim。

## 输出字段（`handoff.artifacts`）

每个键都必须出现；无法从父产物支撑的结论写入 `evidence_gaps`，禁止编造或引入父证据中不存在的新事实。

### `company_assessment`
- **用途**：对企业主体与经营画像的综合判断。
- **建议结构**：非空对象或字符串，概括主体确认、经营状态与行业定位，并引用父 `company_profile`。

### `equity_and_control_risks`
- **用途**：股权与控制权风险。
- **建议结构**：对象或字符串，综合父 `shareholder_structure`；无重大风险时明确说明依据。

### `litigation_and_operating_risks`
- **用途**：司法与经营风险综合判断。
- **建议结构**：对象，至少含 `risk`（风险级别或定性摘要字符串）。
- **关键 claim**：`artifacts.litigation_and_operating_risks.risk`。

### `financing_signals`
- **用途**：融资与资本市场信号摘要。
- **建议结构**：对象或字符串，综合父 `financing_history`。

### `evidence_gaps`
- **用途**：父产物缺口与本报告仍无法核验的项。
- **建议结构**：列表；无缺口时为 `[]`。

### `recommendations`
- **用途**：尽调建议与后续核验动作。
- **建议结构**：非空字符串或对象。
- **关键 claim**：`artifacts.recommendations`。

## 对话呈现（可选）

企业综合判断 → 股权与控制风险 → 司法与经营风险 → 融资信号 → 关键数据缺口 → 尽调建议。
