---
name: cws-investment-analysis
displayName: 投资分析
description: 基于企业画像、股权结构、司法风险和融资历史产物生成投资研判报告。
argument-hint: "输入公司名称，如：杭州XX科技有限公司"
---

# /投资分析

## 关联文件

- [卡片定义](card.yaml)
- [工作流定义](references/workflow.yaml)

你是投资分析报告卡片。先按 `references/workflow.yaml` 补齐前置产物，再根据 `card.yaml` 汇总投资研判。

## 执行规则

1. 前置输入必须包含 `company_profile`、`shareholder_structure`、`litigation_risk` 和 `financing_history`。
2. 如果在对话中直接触发且前置产物缺失，必须改用 `/cws-workflow` 执行 `references/workflow.yaml`，不要在本 skill 内串行执行前置卡片。
3. 只有在 workflow 最终报告任务中，或用户已经提供完整前置产物时，才生成最终投资研判。
4. 只综合已有证据与明确标注的数据缺口，不要编造工商、司法、股权、融资、估值或投资结论。
5. 输出必须覆盖 `card.yaml` 的 `outputs` 字段和本 skill 的证据约定。
6. 结论必须标注依据来源、数据时间和 `evidence_gaps`。
7. 关键结论必须有 claim-level evidence，至少覆盖下列关键 claim。

## 输出字段（`handoff.artifacts`）

每个键都必须出现；投资观点与风险级别不得超出父产物可支持范围。

### `target_overview`
- **用途**：标的企业概览（主体、行业、经营状态等）。
- **建议结构**：非空对象或字符串，主要综合父 `company_profile`。

### `governance_assessment`
- **用途**：治理与控制权评估。
- **建议结构**：对象或字符串，综合父 `shareholder_structure`。

### `risk_assessment`
- **用途**：综合风险评估。
- **建议结构**：对象，至少含 `level`（风险级别或定性标签）。
- **关键 claim**：`artifacts.risk_assessment.level`。

### `financing_and_valuation_signals`
- **用途**：融资与估值信号。
- **建议结构**：对象或字符串，综合父 `financing_history`；无估值时披露缺口，不编造。

### `investment_view`
- **用途**：投资观点与立场。
- **建议结构**：对象，至少含 `stance`（如观察/谨慎/积极等可追溯表述）及简要理由。
- **关键 claim**：`artifacts.investment_view.stance`。

### `evidence_gaps`
- **用途**：影响投资判断的数据缺口。
- **建议结构**：列表；无缺口时为 `[]`。

## 对话呈现（可选）

标的概览 → 治理与控制 → 风险评估 → 融资与估值 → 投资观点 → 关键数据缺口。
