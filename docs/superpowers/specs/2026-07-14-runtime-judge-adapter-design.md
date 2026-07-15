# 运行时内置 LLM Judge Adapter 设计

日期：2026-07-14

状态：设计已确认，待实现计划

范围：在不改宿主核心代码的前提下，为离线评估与在线 gate 提供同一套可预设的 JSONL Judge adapter（OpenAI 兼容，含 Qwen）

相关设计：`2026-07-13-gate-dataset-design.md`、`2026-07-14-atomic-skill-loop-tdd-design.md`

## 1. 背景

Gate 数据集与语义契约已落地：确定性 evidence 检查进入 runtime；LLM Judge 仅在离线 `evaluate_gate_dataset.py` 通过外部 `--judge-adapter` 可用。运行时 `check_artifact_gate.py` 尚未启动 Judge；设计要求 Judge 不可用时 fail closed 为 `needs_review`，但宿主接入层尚未实现。

核心约束：

- 不修改 Hermes / Codex / Claude 等宿主核心代码。
- 离线评估与在线 runtime 必须使用同一套 Judge 实现与协议。
- 插件保持零额外依赖（标准库即可调用 OpenAI 兼容 HTTP）。

## 2. 目标与非目标

### 2.1 目标

- 在插件内预设正式 Judge 可执行文件，默认可供离线与在线共用。
- 复用 `cws-gate-judge/v1` JSONL 协议与现有 `gate_judge_adapter.py` 客户端。
- 支持 OpenAI 兼容 endpoint（含 Qwen），密钥与模型仅来自环境变量。
- 支持 mock 模式供 CI / 联调；无密钥时不得静默放行。
- 确定性 gate 通过后才调用 Judge；结果写入 `gate-result.json`，供 delegate runner 与后续 atomic skill loop 消费。

### 2.2 非目标

- 不改宿主核心、不依赖宿主模型 API 或生命周期 hook。
- 不在仓库提交 API key、endpoint 密钥或真实企业原文。
- 不把无 Judge 配置当作 semantic 通过。
- 第一版不强制落地 atomic loop 的五维 0–4 打分协议扩展。
- 不引入独立 Judge 微服务或知识图谱。

## 3. 方案选择

考虑过三种接入面：

1. **插件内嵌 JSONL adapter 子进程**：与离线评估同一协议；宿主无感。
2. **Agent skill 扮演 Judge**：无额外进程，但离线难对齐、Maker/Judge 易混、跨宿主不稳定。
3. **混合（有 adapter 走 1，否则走 2）**：两套路径，违背「离线在线一套」。

采用方案 1。内置可执行文件采用双模式（live / mock），默认解析到插件 `scripts/` 下预设路径。

## 4. 架构与配置发现

```text
card.yaml gate.semantic.judge
        │
        ▼
check_artifact_gate / evaluate_gate_dataset
        │  解析 adapter 路径
        ▼
scripts/cws_gate_judge.py   ← 套件预设
        │  stdin/stdout JSONL  cws-gate-judge/v1
        ▼
OpenAI 兼容 HTTP（Qwen 等）  或  --mock
```

### 4.1 路径解析（优先级）

1. 显式：`--judge-adapter` 或环境变量 `CWS_JUDGE_ADAPTER`（绝对路径，单可执行文件）。
2. 默认：`<plugin-root>/scripts/cws_gate_judge.py`（由 runner 拼绝对路径，以 `python3` 启动脚本亦可：启动向量为 `[python3, abs_path]`，不经 shell）。
3. 文件不存在：运行时 fail closed `needs_review`（`judge_unavailable`）；离线 `semantic` profile 仍视为基础设施错误（exit 2）。

现有 `judge_adapter_example.py` 升格或替换为正式 `cws_gate_judge.py`；example 可保留为指向正式入口的薄包装或删除并在文档中说明迁移。

### 4.2 内置双模式

| 模式 | 触发 | 行为 |
| --- | --- | --- |
| mock | `--mock` 或 `CWS_JUDGE_MODE=mock` | 确定性假响应，供 CI / 协议联调 |
| live | 默认（配置了密钥时） | OpenAI 兼容 Chat Completions（标准库 `urllib`） |
| 无密钥 | live 但缺少 API key | 不自动降级为 mock；返回 `needs_review` + `judge_unavailable` |

### 4.3 环境变量（不入库）

| 变量 | 含义 |
| --- | --- |
| `CWS_JUDGE_ADAPTER` | 覆盖默认 adapter 路径 |
| `CWS_JUDGE_MODE` | `live`（默认）或 `mock` |
| `CWS_JUDGE_BASE_URL` | OpenAI 兼容 base URL（如 DashScope compatible-mode） |
| `CWS_JUDGE_API_KEY` | API key；亦接受 `OPENAI_API_KEY` |
| `CWS_JUDGE_MODEL` | 模型 ID，例如 `qwen3.7-max` |
| `CWS_JUDGE_TIMEOUT_SECONDS` | 单次请求超时，默认与现有 120s 对齐 |
| `CWS_JUDGE_ENABLED` | `0` 时跳过调用，但仍记 `needs_review` + `judge_disabled` |

`plugin.yaml` 可将上述 Judge 相关变量标为可选说明，**不**加入硬性 `requires_env`。

Qwen 示例（仅本地/宿主环境配置）：

```bash
export CWS_JUDGE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export CWS_JUDGE_API_KEY="<qwen-key>"
export CWS_JUDGE_MODEL="qwen3.7-max"
```

## 5. 运行时接入与结果映射

### 5.1 触发条件

仅当 `card.yaml` 声明了 `gate.semantic.judge`，且确定性 gate（结构 + evidence semantic）全部通过后，才启动 Judge。确定性失败时不调用模型。

### 5.2 接入点

| 入口 | 行为 |
| --- | --- |
| `check_artifact_gate.py --mode node` | 确定性通过后组装请求 → 调预设 adapter |
| `check_artifact_gate.py --mode final` | 同上（报告 + 父 handoff 摘要） |
| `delegate complete` | 仍只调用 gate checker；Judge 在 checker 内完成 |
| `evaluate_gate_dataset.py --profile semantic` | 默认指向同一 `scripts/cws_gate_judge.py`；`--judge-adapter` 可覆盖 |

进程：单次 runtime gate 可短生命周期（启 → 一问一答 → 退）；离线批量可长驻。协议不变。

### 5.3 `gate-result.json`

在现有 gate 结果上增加 `judge` 段，至少包含：

- `decision`：`passed` | `needs_review`
- `confidence`
- `model`
- `rubric_version`
- `findings`
- 可选：`evaluator_id`、错误原因码（如 `judge_unavailable`）

顶层 gate 决策为三态：`passed` / `blocked` / `needs_review`。`blocked` 仅来自确定性规则。

### 5.4 Exit 与 runner 行为

| 情况 | gate 决策 | CLI exit | 行为 |
| --- | --- | --- | --- |
| 确定性失败 | `blocked` | 1 | 不解锁下游 |
| Judge `passed` 且 confidence ≥ 0.75 | `passed` | 0 | 解锁 / accept |
| Judge `needs_review` 或 confidence < 0.75 | `needs_review` | 1 | 暂停；不自动放行 |
| adapter 缺失 / 无 key / 超时 / 协议错误 | `needs_review`（`judge_unavailable`） | 1 | fail closed |
| `CWS_JUDGE_ENABLED=0` | `needs_review`（`judge_disabled`） | 1 | 不静默通过 |
| 离线评估同类基础设施错误 | — | 2 | 整次评估失败 |

与 atomic skill loop：`needs_review` / `judge_unavailable` / `judge_disabled` 映射为 Runner Policy 的 `human_review`（不自动追分）；可修复 finding 且仍有次数时映射为 `revise`。第一版不新增宿主 hook；Hermes 等若已将非 0 exit 映为 blocked，继续复用。

## 6. 请求包、Rubric 与 Finding

### 6.1 请求包

复用 `cws-gate-judge/v1`。由 checker 在调用前组装，**不含** `expected_decision` / `expected_reasons`。内容包括：

- `subject`
- 最小化 `handoff`（沿用 `minimize_handoff`）
- `evidence`
- `raw_summaries`（事实摘要，非受限全文）
- 可选 `parent_handoffs`
- 可选截断后的 `report`

Adapter 不得自行读取案例目录或企业数据源，不得联网检索外部事实；仅允许访问配置的模型 endpoint。

### 6.2 Rubric

继续使用 `card.yaml` 白名单：`company-profile-v1`、`due-diligence-v1`。

第一版响应保持协议最小集：`decision`、`confidence`、`model`、`rubric_version`、`findings`。Atomic loop 的五维评分作为可选 v1.1 扩展字段，不阻塞本切片；通过条件以 `passed|needs_review` + confidence ≥ 0.75 为准。

### 6.3 Finding 校验

每条 finding 必须含稳定 `reason`、可解析 `artifact_path`、`evidence_refs`。校验失败则整次 Judge 结果视为无效，fail closed 为 `needs_review`。

新增稳定原因码（至少）：

```text
judge_unavailable
judge_disabled
judge_low_confidence
```

（`judge_error` 可作为 adapter 内部 finding reason，映射到 `needs_review`。）

## 7. 测试边界

| 层 | 内容 | 是否调用真实模型 |
| --- | --- | --- |
| 单元 | mock 协议往返、默认路径解析、无 key → `judge_unavailable`、`ENABLED=0` → `judge_disabled` | 否 |
| CI `ci` profile | 契约 + 确定性 semantic；纯 Judge 案例 `not_run` 或 mock | 否 |
| 本地 / 定期 `semantic` | 默认内置 adapter + Qwen（或其它兼容 endpoint）环境 | 是 |

常规 CI 不得依赖真实 API key。

## 8. 验收标准

- 未设置 `CWS_JUDGE_ADAPTER` 时，runtime 与离线 semantic 均能解析到插件内预设 adapter。
- 同一 adapter 二进制/脚本服务于 `evaluate_gate_dataset.py` 与 `check_artifact_gate.py`。
- 配置 Qwen（或其它 OpenAI 兼容）环境变量后，live 模式可完成一次合法协议往返。
- mock 模式无需网络即可通过协议测试。
- 无 key、超时、协议错误、显式禁用均不得得到 `passed`。
- 确定性失败不调用 Judge。
- `gate-result.json` 含可审计的 `judge` 段。
- 宿主核心代码无改动。

## 9. 实现顺序（供后续计划引用）

1. 正式化 `scripts/cws_gate_judge.py`（stdlib HTTP + mock）。
2. 共享路径解析 helper；接入 `check_artifact_gate` 与 `evaluate_gate_dataset` 默认值。
3. 扩展 `gate-result` 写入与 exit / `needs_review` 映射。
4. 单元测试（mock / unavailable / disabled）与文档中的 Qwen 配置说明。
5. 可选：清理或收敛 `judge_adapter_example.py`。
