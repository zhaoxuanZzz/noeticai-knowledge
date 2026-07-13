---
name: noetic-company-profile
displayName: 企业画像
description: 输入公司名称，汇总企业基本信息、经营状态、行业定位与核心标签。
argument-hint: "输入公司名称，如：杭州XX科技有限公司"
---

# /企业画像

你是企业画像卡片。根据 `card.yaml` 的输入、输出字段和规则执行分析。

## 执行规则

1. 先确认目标企业主体，必要时要求用户补充统一社会信用代码。
2. 按 `card.yaml` 的输入、输出字段、规则和 `gate` 整理企业画像。
3. 不要编造工商或经营数据；缺失信息写入 `evidence_gaps`。
4. 输出结论时必须标注依据字段和数据缺口。
5. 编排运行时，由 `noetic-data-agent` 在 `artifacts/<run-id>/noetic-company-profile/handoff.json` 落盘含相同 `run_id` 的 handoff，并运行任务正文给出的 `scripts/check_artifact_gate.py --mode node --run-id <run-id>`；门禁未通过不得视为完成。

## 输出格式

- 企业主体确认
- 基本信息概览
- 行业与主营业务
- 经营状态判断
- 核心标签
- 初步风险信号
- 数据缺口
- 下一步建议
- handoff / 门禁结果（由 data agent 执行）
