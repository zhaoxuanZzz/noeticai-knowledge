# Gate 双层数据集设计

日期：2026-07-13

状态：设计已确认

实现状态：已落地离线 runner、node/final 确定性 evidence gate、JSONL Judge adapter、首批 node/final fixture、staging 刷新、历史知识库导入、冻结 KB delegate 模式和人工审核后提升流程。四家真实企业候选 bundle 已生成在仓库外 staging，等待人工批准标签后提升。

范围：`cws-company-profile` 节点 gate 与 `cws-due-diligence` final gate 的首批验证数据集

## 1. 背景

仓库现有 `tests/fixtures/gates/company-profile/` 主要验证 `handoff.json` 的结构、必填字段、输出类型、`run_id` 和少量行为规则。`scripts/check_artifact_gate.py` 的 final 模式主要验证本次 run 的父 handoff 与报告 handoff 是否存在、可解析且 `run_id` 一致。

这些测试能够证明 gate 检查器按当前契约工作，但不能证明 gate 对真实企业数据质量做出了正确判断。例如，字段内容互相冲突、数据已经过期或父产物来自另一家公司时，只要结构仍然合法，当前 gate 仍可能放行。

因此需要建立两层数据集：

1. 契约数据集：确定性验证 gate 检查器本身，进入常规 CI。
2. 真实业务快照数据集：离线回放复杂企业数据场景，识别语义误放行、误拦截和数据源漂移。

真实业务层采用“离线冻结快照作为基准集 + 定期在线刷新”。在线数据只生成候选快照和漂移报告，不直接改写基准答案。

## 2. 目标与非目标

### 2.1 目标

- 同时覆盖 node gate 与 final gate。
- 区分“当前 gate 实际行为”和“业务上正确的预期行为”。
- 能稳定复现缺字段、主体歧义、来源冲突、数据过期、跨 run 串线和跨公司串线等场景。
- 能量化误放行、误拦截、已知能力缺口和数据漂移。
- 基准快照的更新必须经过可审计的人工确认。

### 2.2 首批范围

- 节点：`cws-company-profile`。
- 终局：`cws-due-diligence`。
- 结构异常使用基准 handoff 加 patch 生成。
- 真实业务场景保存完整、最小化的离线快照 bundle。

### 2.3 非目标

- 首批不覆盖所有业务 skill 和投资分析 workflow。
- 不使用纯 LLM 主观评分器作为唯一硬门禁。
- 不把在线数据源的实时结果作为 CI 的通过条件。
- 不自动接受在线刷新产生的新标签或新基准。
- 不把受限原文、访问令牌、请求头或企业内部数据提交到仓库。

## 3. 判定政策

核心原则：**真实但不完整可以通过；虚构、隐瞒、不一致或串数据必须拦截。**

每个案例同时记录 gate 决策和数据质量状态：

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `expected_decision` | `passed` / `blocked` / `needs_review` | 业务上期望的门禁决策 |
| `quality_state` | `complete` / `degraded` / `invalid` | 数据完整、真实但有缺口、或不可接受 |
| `waivable` | `true` / `false` | 是否允许人工带原因放行 |
| `expected_reasons` | 字符串列表 | 稳定、机器可比较的原因码 |

`degraded` 不等于失败。无法取得部分数据但已在 `evidence_gaps` 中如实披露时，`expected_decision` 可以是 `passed`。主体无法确认、确定性证据规则失败、虚构结论或混入其他任务数据时，`expected_decision` 必须是 `blocked`。只有 LLM Judge 发现语义可疑或自身置信度不足、但确定性规则没有证明产物错误时，才使用 `needs_review`。

数据集还必须记录当前实现的实际结果：

```json
{
  "expected_decision": "needs_review",
  "quality_state": "invalid",
  "expected_reasons": ["source_conflict_not_disclosed"],
  "current_decision": "passed",
  "capability_gap": true,
  "waivable": true
}
```

这样可以保留已知失败案例，而不把当前误放行错误地固化为业务预期。

## 4. 语义识别架构

数据集提供人工审核后的标准答案，但数据集本身不是语义检查器。运行时采用混合式 evaluator：确定性证据规则负责可以机械证明的错误，LLM Judge 负责自然语言中的曲解、遗漏与隐瞒。

这里的“虚构”定义为：**某项结论无法被本次运行冻结的证据集合支持，或与该证据集合直接冲突**。Gate 不能证明现实世界的绝对真伪；如果所有上游来源本身都错误，只能通过来源治理和人工复核处理。

### 4.1 Claim-level 证据契约

语义 gate 仍以业务 skill 的 `card.yaml` 为单一事实来源。建议在现有 `gate` 下增加版本化、可静态校验的 `semantic` 段：

```yaml
gate:
  handoff: required
  semantic:
    evidence: required
    deterministic_checks:
      - claim_evidence
      - subject_identity
      - freshness_disclosure
      - source_conflict_disclosure
      - negative_claim_coverage
    judge:
      rubric: company-profile-v1
      on_suspicious: needs_review
```

`deterministic_checks` 和 `rubric` 都必须引用仓库内注册的白名单 ID，不能在 `card.yaml` 中放任意可执行表达式或自由文本 prompt。时效阈值按字段类型配置在注册的检查器策略中并带版本号，不能依赖测试当天的隐式默认值。

每个有语义 gate 的节点除 `handoff.json` 外，还必须生成同目录的 `evidence.json`。它不替代业务 artifact，而是记录 artifact 中每项关键结论如何追溯到冻结来源：

```json
{
  "run_id": "run-case-001",
  "skill_id": "cws-company-profile",
  "subject": {
    "name": "示例公司",
    "unified_social_credit_code": "91330100TEST000001"
  },
  "evidence": [{
    "id": "e1",
    "subject_id": "91330100TEST000001",
    "field": "operating_status",
    "value": "存续",
    "observed_at": "2026-07-13",
    "source_ref": "raw/qcc-company.json#/result/status"
  }],
  "claims": [{
    "artifact_path": "artifacts.operating_status.status",
    "value": "存续",
    "evidence_refs": ["e1"]
  }],
  "conflicts": []
}
```

关键结论必须有 claim；claim 必须引用存在的 evidence；evidence 必须能定位到冻结 raw 快照中的具体字段。不能只在 handoff 顶层笼统列一个 `sources` 数组来证明所有结论。

`source_ref` 只能引用 company-kb 或案例 bundle 内的冻结文件，解析后不得逃逸允许的根目录。Evaluator 同时校验案例记录的来源摘要哈希，避免评估时原始证据被替换。

### 4.2 确定性语义规则

以下规则失败时直接返回 `blocked`：

- claim 引用的 evidence 不存在或来源路径不可解析。
- claim 值与 evidence 值归一化后不一致。
- evidence 主体与本次目标主体的统一社会信用代码不一致。
- 数据超过对应类型的时效阈值，且未进入 `evidence_gaps`。
- 来源存在结构化冲突，但 `conflicts` 或 `evidence_gaps` 未披露。
- “无诉讼”“无风险”等否定结论没有对应范围完整、执行成功的查询证据。
- final 产物引用其他 run、其他主体或未通过 gate 的父产物。
- final 报告新增父产物和证据集合中不存在的确定性事实。

### 4.3 LLM Judge

LLM Judge 只读取冻结的 raw 摘要、`evidence.json`、`handoff.json`、父 handoff 和最终报告，不访问实时网络。它使用固定 rubric 检查：

- 自然语言是否曲解结构化证据。
- 是否隐瞒来源冲突、数据时效或关键 `evidence_gaps`。
- final 报告是否遗漏父节点披露的重大风险。
- 建议和风险级别是否超出已有事实能够支持的范围。

Judge 必须输出结构化结果：`decision`、`confidence`、稳定原因码、artifact 路径和 evidence 引用。无法给出具体路径和引用的判断无效。结果还必须记录 evaluator ID、rubric 版本和模型标识，以便同一数据集比较不同 Judge 版本。

```json
{
  "decision": "needs_review",
  "confidence": 0.71,
  "evaluator_id": "company-profile-semantic-v1",
  "rubric_version": "company-profile-v1",
  "model": "configured-model-id",
  "findings": [{
    "reason": "source_conflict_not_disclosed",
    "artifact_path": "artifacts.company_summary.status",
    "evidence_refs": ["e1", "e2"]
  }]
}
```

### 4.4 运行状态与人工介入

首期使用“软判定、硬暂停”：

| 条件 | 结果 | 后续动作 |
| --- | --- | --- |
| 确定性规则失败 | `blocked` | 修复产物后重试，或按既有权限带原因豁免 |
| 确定性规则通过，LLM Judge 通过 | `passed` | 允许完成和交接 |
| LLM Judge 判为可疑或置信度不足 | `needs_review` | 暂停完成，等待人工修复重试或带原因豁免 |

LLM Judge 不能单独永久判死任务，也不能在可疑时自动放行。运行时可将 `needs_review` 映射为 Kanban blocked 状态，但 gate result 必须保留独立的 `needs_review` 语义，便于审计和统计。

## 5. 方案选择

考虑过三种组织方式：

1. 每个场景保存完整 `handoff.json`：直观，但结构异常案例会大量复制相同数据。
2. 单一基准快照加 patch：维护成本低，但真实业务案例难以独立阅读和审计。
3. 混合式：真实业务场景保存完整快照，纯结构异常由基准 handoff 加 patch 生成。

采用第三种。它兼顾真实场景的可审计性和契约测试的低维护成本。

## 6. 数据集目录

```text
tests/fixtures/gate-dataset/
├── schema/
│   ├── base/
│   │   ├── company-profile.handoff.json
│   │   └── due-diligence-run/
│   ├── patches/
│   └── cases.json
├── business/
│   ├── normal-active-company/
│   ├── ambiguous-company-name/
│   ├── partially-missing-data/
│   ├── conflicting-sources/
│   ├── stale-data/
│   ├── no-public-data/
│   └── high-risk-company/
├── manifests/
│   ├── node-cases.json
│   └── final-cases.json
└── refresh/
    └── companies.json
```

`schema/` 只表达确定性契约变体。`business/` 表达真实业务语义。`manifests/` 是测试发现和期望判定的入口。`refresh/companies.json` 只保存允许在线刷新的主体标识、场景用途和刷新频率，不保存密钥。

## 7. 真实案例 bundle

每个业务案例是一个可独立回放的 bundle：

```text
business/conflicting-sources/
├── case.json
├── raw/
│   ├── qcc-company.json
│   └── public-source.json
├── input.json
├── artifacts/
│   └── run-case-001/
│       ├── cws-company-profile/
│       │   ├── handoff.json
│       │   ├── evidence.json
│       │   └── report.md
│       └── cws-due-diligence/
│           └── handoff.json
└── expected/
    └── decision.json
```

职责分离：

- `raw/`：冻结且最小化的原始来源数据。
- `input.json`：输入主体、统一社会信用代码（如有）、模拟运行时间和固定 `run_id`。
- `artifacts/`：待 gate 回放的标准化产物。
- `evidence.json`：关键结论到冻结来源字段的 claim-level 证据映射。
- `case.json`：场景标签、快照时间、适用 gate 和数据来源摘要。
- `expected/decision.json`：业务期望、当前实际结果和能力缺口。

`case.json` 最小形状：

```json
{
  "case_id": "conflicting-sources",
  "subject": "示例公司",
  "tags": ["source-conflict", "node", "semantic"],
  "snapshot_at": "2026-07-13",
  "evaluation_at": "2026-07-13",
  "applicable_gates": ["cws-company-profile:node"],
  "source_hashes": {
    "qcc-company.json": "sha256:4f34c65a67790b3f89b5834cc1f1ce0ddf2e9ab82f211d265f34a55af8ef8629",
    "public-source.json": "sha256:7e702493db039a275202805704e500524a7e5f3f48d4ac21d2c61f254608c2a4"
  }
}
```

`snapshot_at` 表示抓取时间；`evaluation_at` 表示回放时模拟的当前时间。两者分开后，可以稳定复现“数据已过期”场景，而不依赖测试执行当天的系统时间。

## 8. 首批场景矩阵

### 8.1 Node gate

| 类别 | 场景 | `expected_decision` | `quality_state` |
| --- | --- | --- | --- |
| 基线 | 主体唯一、数据完整、来源一致 | `passed` | `complete` |
| 主体 | 公司简称能唯一解析到法人主体 | `passed` | `complete` |
| 主体 | 同名企业，无法确定具体主体 | `blocked` | `invalid` |
| 主体 | 名称与统一社会信用代码不一致 | `blocked` | `invalid` |
| 缺失 | 非关键字段缺失，已写 `evidence_gaps` | `passed` | `degraded` |
| 缺失 | 核心画像缺失，未写缺口 | `blocked` | `invalid` |
| 空数据 | 完全查不到公开数据，如实说明 | `passed` | `degraded` |
| 空数据 | 查不到数据却填充确定性结论 | `blocked` | `invalid` |
| 来源 | 多来源结论一致 | `passed` | `complete` |
| 来源 | 多来源冲突，已披露冲突和取舍依据 | `passed` | `degraded` |
| 来源 | 多来源冲突，确定性字段可证明未披露 | `blocked` | `invalid` |
| 来源 | 多来源冲突只存在于自然语言语义中，疑似未披露 | `needs_review` | `invalid` |
| 时效 | 数据较旧，明确标注时间和时效风险 | `passed` | `degraded` |
| 时效 | 使用过期数据却声明为当前状态 | `blocked` | `invalid` |
| 状态 | 注销、吊销或经营异常，风险如实进入 `risk_flags` | `passed` | `complete` |
| 角色 | data handoff 携带最终尽调结论 | `blocked` | `invalid` |
| 文件 | handoff 缺失、JSON 损坏或根节点类型错误 | `blocked` | `invalid` |
| 隔离 | handoff `run_id` 与本次运行不一致 | `blocked` | `invalid` |

### 8.2 Final gate

| 场景 | `expected_decision` | `quality_state` |
| --- | --- | --- |
| 父 handoff 和报告齐全，来自同一 run 和同一主体 | `passed` | `complete` |
| 任一父 handoff 缺失或损坏 | `blocked` | `invalid` |
| 混入其他 run 的父产物 | `blocked` | `invalid` |
| 混入另一家公司的父产物 | `blocked` | `invalid` |
| 父节点结构合格，但其 gate 实际被阻断 | `blocked` | `invalid` |
| 报告可确定地混入父证据中不存在的新事实 | `blocked` | `invalid` |
| LLM Judge 判断报告可能遗漏重大风险或关键缺口 | `needs_review` | `invalid` |

首批案例应覆盖每一行至少一个样本。结构型案例可由 patch 生成；主体、来源、时效和报告遗漏必须使用完整业务 bundle。

## 9. 原因码

原因码用于稳定断言，不直接依赖易变化的中文错误文本。首批至少包括：

```text
handoff_missing
handoff_invalid_json
required_output_missing
required_output_empty
run_id_mismatch
subject_ambiguous
subject_identity_mismatch
missing_data_not_disclosed
unsupported_claim
source_conflict_not_disclosed
stale_data_not_disclosed
evidence_missing
evidence_path_invalid
evidence_value_mismatch
evidence_subject_mismatch
negative_claim_without_search_coverage
data_role_contains_final_report
parent_handoff_missing
parent_gate_blocked
cross_run_artifact
cross_subject_artifact
material_risk_omitted
judge_low_confidence
```

一个案例可以有多个原因码。测试断言至少要求实际原因集合包含 `expected_reasons`，允许检查器附加更具体的诊断信息。

## 10. 离线评估

离线 runner 的职责是：

1. 从 manifest 发现案例。
2. 为结构案例复制基准 handoff 并应用 patch。
3. 为业务案例构造隔离的临时 company-kb 目录。
4. 使用案例固定的 `run_id` 调用现有 node 或 final gate。
5. 先运行确定性 evaluator，再按案例配置运行固定版本的 LLM Judge。
6. 记录 `expected_decision`、实际决策、标准化原因码、Judge 版本和能力缺口。
7. 输出 JSON 明细与终端摘要。

当前案例入口为 `tests/fixtures/gate-dataset/cases.json`。结构变体使用 RFC 7396 merge patch；业务案例引用冻结 raw、`evidence.json` 与 SHA-256 摘要。真实 node gate 同样读取 `handoff.json` 同目录的 `evidence.json`，缺失或确定性规则失败时 exit code 为 1。

指标至少包含：

- 总案例数与按标签覆盖率。
- 误放行：预期拦截但实际通过。
- 误拦截：预期通过但实际拦截。
- 待复核命中率：预期 `needs_review` 且实际正确暂停。
- Judge 误报与漏报：以人工审核标签为准。
- 已知能力缺口：`capability_gap: true` 的案例。
- 非预期回归：原本一致的案例产生新偏差。

CI 分层执行：

- 常规 CI：运行 `ci` profile，只执行契约案例和确定性语义案例；纯 Judge 案例只校验 fixture 形态，不调用模型。
- 定期评估：运行 `semantic` profile，对同一离线基准集执行完整 Judge；可容纳已登记的能力缺口，但不能新增未登记偏差。
- 在线刷新：独立任务，不作为普通 PR 的通过条件。

### 10.1 命令入口

常规 CI：

```bash
python3 scripts/evaluate_gate_dataset.py \
  --dataset tests/fixtures/gate-dataset \
  --profile ci
```

完整语义评估：

```bash
python3 scripts/evaluate_gate_dataset.py \
  --dataset tests/fixtures/gate-dataset \
  --profile semantic \
  --judge-adapter /absolute/path/to/cws-gate-judge
```

`--judge-adapter` 必须是单个可执行文件的绝对路径。Runner 使用参数数组直接启动进程，不经过 shell，也不接受包含管道、重定向或额外 shell 参数的命令字符串。模型、endpoint 和认证信息由 adapter 自己的环境或宿主配置提供。

### 10.2 JSONL Judge adapter 协议

Runner 为一次评估启动一个长驻 adapter 子进程，通过 stdin/stdout 逐行交换 JSON。协议版本固定为 `cws-gate-judge/v1`；每个请求必须得到一条带相同 `request_id` 的响应。

请求示例：

```json
{
  "protocol_version": "cws-gate-judge/v1",
  "request_id": "eval-20260713-case-001",
  "case_id": "conflicting-sources",
  "evaluator_id": "company-profile-semantic-v1",
  "rubric_id": "company-profile-v1",
  "input": {
    "subject": {
      "name": "示例公司",
      "unified_social_credit_code": "91330100TEST000001"
    },
    "raw_summaries": [{
      "source_id": "s1",
      "facts": {"operating_status": "存续"}
    }],
    "evidence": {
      "evidence": [{
        "id": "e1",
        "field": "operating_status",
        "value": "存续",
        "source_id": "s1"
      }],
      "claims": [{
        "artifact_path": "artifacts.operating_status.status",
        "value": "存续",
        "evidence_refs": ["e1"]
      }]
    },
    "handoff": {
      "artifacts": {"operating_status": {"status": "存续"}}
    },
    "parent_handoffs": [],
    "report": "该企业当前登记状态为存续。"
  }
}
```

请求中不包含 `expected_decision`、`expected_reasons` 或其他标准答案，避免 Judge 标签泄漏。Runner 在发送前完成路径约束、来源哈希、JSON 形态和确定性规则检查；adapter 只接收最小化、冻结后的语义评审包，不自行读取案例目录，也不得访问企业数据源或联网检索外部事实。Adapter 可以访问其配置的模型 endpoint。

响应示例：

```json
{
  "protocol_version": "cws-gate-judge/v1",
  "request_id": "eval-20260713-case-001",
  "decision": "needs_review",
  "confidence": 0.71,
  "model": "semantic-judge-model-v1",
  "rubric_version": "company-profile-v1",
  "findings": [{
    "reason": "source_conflict_not_disclosed",
    "artifact_path": "artifacts.company_summary.status",
    "evidence_refs": ["e1", "e2"]
  }]
}
```

Judge 只能返回 `passed` 或 `needs_review`；`blocked` 只由确定性 evaluator 产生。响应必须通过 schema 校验，finding 必须带稳定原因码和可解析的 artifact/evidence 引用。

每个请求默认超时 120 秒，可用 `--judge-timeout-seconds` 在 1 至 600 秒之间显式覆盖。评估模式下，adapter 启动失败、超时、异常退出、输出非 JSON、协议版本不匹配或请求响应错位属于基础设施错误，整次命令 exit 2。真实任务运行时遇到同类故障则 fail closed 为 `needs_review`，不能因 Judge 不可用而自动放行。

### 10.3 报告与退出码

默认输出到 `.scratch/gate-eval/<eval-id>/`：

```text
.scratch/gate-eval/<eval-id>/
├── results.json
├── report.md
├── false-accepts.json
├── false-rejects.json
├── needs-review.json
└── drift.json
```

`results.json` 记录案例输入摘要哈希、预期决策、实际决策、原因码、确定性检查器版本、Judge model/rubric 版本和耗时。报告不得包含认证信息或未最小化的受限来源原文。

退出码：

| code | 含义 |
| --- | --- |
| `0` | 没有非预期偏差 |
| `1` | 出现新的误放行、误拦截、待复核漏报或其他未登记回归 |
| `2` | 数据集、配置、协议或 adapter 基础设施错误 |

显式标记为 `capability_gap: true` 的已知偏差单独统计，默认不导致 exit 1；任何新增偏差或已有偏差扩大都必须失败。`ci` profile 不把未执行的纯 Judge 案例记为通过，而是记为 `not_run`。

## 11. 在线刷新

在线刷新遵循以下流程：

```text
定期抓取
  -> 写入 staging 快照
  -> 与当前基准做字段级 diff
  -> 生成候选 handoff
  -> 运行现有 gate
  -> 比较 expected 与 actual
  -> 生成漂移报告
  -> 人工审核
  -> 提升为新的版本化基准
```

刷新命令：

```bash
python3 scripts/refresh_gate_dataset.py \
  --manifest tests/fixtures/gate-dataset/refresh/companies.json \
  --output ~/.cws/gate-dataset-staging/2026-07-13
```

刷新完成后，使用相同 runner 比较 staging 与仓库基准：

```bash
python3 scripts/evaluate_gate_dataset.py \
  --dataset ~/.cws/gate-dataset-staging/2026-07-13 \
  --baseline tests/fixtures/gate-dataset \
  --profile semantic \
  --judge-adapter /absolute/path/to/cws-gate-judge
```

刷新 manifest 可用 `baseline_dataset` 生成完整、可直接回放的 staging 副本；`snapshot_file` 只作为已抓取候选输入，刷新脚本会执行敏感字段扫描和字段白名单最小化，再按 `target_file` 写入 staging。没有 `snapshot_file` 的主体记录为 `awaiting_capture`，不会伪造候选数据。

约束：

- 在线任务不得直接覆盖 `tests/fixtures/gate-dataset/business/`。
- 原始响应、标准化 handoff 与预期判定必须分开保存。
- 刷新产物先进入仓库外 staging 目录；只有审核后的最小化快照才能进入 fixture。
- 对来源响应做字段白名单和敏感信息扫描。
- 保存最小必要字段及摘要哈希，不保存 token、cookie、授权头或受限全文。
- 语义标签必须人工审核；刷新脚本只能提出候选变化。
- 基准更新应在 PR 中同时展示来源 diff、handoff diff、gate 决策 diff 和标签变化。

## 12. 当前能力缺口的处理

数据集先表达正确业务期望，不迁就当前检查器能力。确定性 evaluator 已覆盖 evidence 缺失、来源路径和值不一致、主体 ID 不一致、过期或冲突未披露、否定结论检索范围不足、跨主体 final 与父 gate 已阻断。剩余能力缺口主要是：

- 只有名称、没有稳定主体 ID 时的同名歧义仍需上游主体解析或人工确认。
- final 报告的自然语言遗漏、曲解和建议过度外推依赖外部 Judge adapter。
- 当前 runtime node/final gate 已执行确定性 evidence 检查，但尚未直接启动模型 Judge；模型不可用时的 `needs_review` 状态由宿主接入层实现。
- 在线抓取由宿主或定时任务完成；刷新脚本只接收已抓取快照，负责最小化、敏感字段检查、staging 和漂移对比。

这些案例标记为 `capability_gap: true`。后续增强 gate 时逐项转为普通回归案例；不能删除失败样本来提高通过率。

## 13. 分阶段落地

### 阶段一：数据集骨架与现有规则回归

- 建立目录、manifest 和案例元数据格式。
- 迁移现有 company-profile fixture。
- 增加结构 patch 与 final run fixture。
- runner 复用现有 `check_artifact_gate.py`。

### 阶段二：首批真实业务快照

- 为主体歧义、部分缺失、来源冲突、数据过期、无公开数据和高风险主体建立 bundle。
- 人工标注业务期望和原因码。
- 生成首次误放行与误拦截报告。

### 阶段三：确定性语义 evaluator

- 增加 `evidence.json` 契约与静态校验。
- 实现 claim、主体、值、时效、冲突和否定结论覆盖检查。
- node 与 final 的确定性失败进入硬 `blocked`。

### 阶段四：LLM Judge 与人工复核

- 固定 rubric、模型配置、输出 schema 和 evaluator 版本。
- LLM 可疑或低置信度结果进入 `needs_review`。
- 接通人工修复重试和带原因豁免审计。

### 阶段五：在线刷新与漂移报告

- 配置刷新主体和频率。
- 输出 staging 快照与多层 diff。
- 建立人工提升基准的 PR 流程。

### 阶段六：扩大覆盖

- 按能力缺口优先级增强 gate。
- 扩展到股权结构、司法风险、融资历史和投资分析。
- 保持 node 与 final 案例分层，不把所有场景塞进单一端到端测试。

## 14. 验收标准

- 首批场景矩阵每一行至少有一个案例。
- 所有案例均有稳定 `case_id`、标签、预期决策、质量状态和原因码。
- 离线执行不访问网络，结果可重复。
- 关键结论可以通过 `evidence.json` 追溯到冻结 raw 字段。
- 确定性语义失败稳定返回 `blocked`；LLM 可疑或低置信度结果稳定返回 `needs_review`。
- Judge 输出包含 evaluator 版本、原因码、artifact 路径和 evidence 引用。
- 当前已有结构规则全部通过回归。
- 当前语义能力缺口被明确列出，且不会导致普通回归被误判为成功。
- 在线刷新不能直接修改基准；基准提升需要人工审核。
- 测试报告能分别展示误放行、误拦截、能力缺口和数据漂移。

## 15. 历史企业知识库导入与提升

首批历史导入清单位于 `tests/fixtures/gate-dataset/import/companies.json`，覆盖小米科技、比亚迪、宁德时代和北京百度网讯。原知识库始终只读，导入输出必须位于仓库外空 staging：

```bash
python3 scripts/import_gate_kb_snapshots.py \
  --kb-root ~/.cws/company-knowledge \
  --manifest tests/fixtures/gate-dataset/import/companies.json \
  --output ~/.cws/gate-dataset-staging/<capture-id>
```

导入清单通过 JSON Pointer 明确选择公开字段。导入器扫描完整源 JSON 中的敏感键，但只把声明字段写入标准化 `{subject, source_id, observed_at, facts}` 快照；查询脚本、完整历史响应和 wiki 文章不会进入最终 fixture。每家公司获得独立 `companies/<case-id>/kb`，可直接作为冻结执行的 `CWS_COMPANY_KB_DIR`。

冻结执行使用 `--frozen-kb`。该标志写入 delegate graph、节点 prompt 和 `workflow-state.json`，禁止 MCP、网络补查和 wiki 回写；缺失信息必须进入 `evidence_gaps`。节点实际使用的最小来源复制到自身 artifact 目录的 `raw/`，确保 `evidence.source_ref` 不逃逸 handoff 目录。

staging 的 `review.json` 初始状态为 `pending`。审核者需要确认主体、来源、expected/current decision、原因码和质量状态，将案例改为 `approved` 后才能运行：

```bash
python3 scripts/promote_gate_dataset.py \
  --staging ~/.cws/gate-dataset-staging/<capture-id> \
  --dataset tests/fixtures/gate-dataset
```

提升器重新验证哈希、路径、敏感字段、案例唯一性和标签完整性，并通过临时数据集副本完成替换。一个真实 workflow bundle 可以通过 `bundle_id` 同时承载 node 与 final 案例，不重复保存整套产物。任何未批准案例或校验失败都会在修改基准前退出。
