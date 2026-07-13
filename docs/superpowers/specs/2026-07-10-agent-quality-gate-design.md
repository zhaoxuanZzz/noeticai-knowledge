# Agent 运行时质量门禁设计

日期：2026-07-10  
状态：已实现（v1，含编排 run 隔离）
范围：NoeticAI Work Suite 插件内的 Agent 运行时产物门禁

## 1. 背景与目标

本仓库已有 L0 静态契约门禁（`scripts/validate_work_suite.py` + 集成测试）。业务质量约束目前写在 `card.yaml` 的 `rules` / `outputs` 与 role skill 文案中，但**没有可执行的运行时验收**；文档也写明独立验收此前不在仓库实现。

本设计要补上 **Agent 运行时质量门禁**：

- 每个 data/gen 节点完成后硬拦：不过关不交接下游
- 编排型流程终局总检：企业尽调与投资分析按本次 run 隔离校验
- 以确定性产物检查为主，行为边界只做少量可判定规则
- **脚本是唯一硬门禁**；skill 只约定「先写可检查产物，再跑脚本」

### 非目标

- LLM rubric / 主观打分
- Hermes/Kanban 完成钩子自动拦截
- 把 gate 做成 `workflow.yaml` 中的独立 stage
- 一次给所有 skill 加 gate
- 恢复已移除的重型独立 artifact contracts 目录树

## 2. 决策摘要

| 决策点 | 选择 |
| --- | --- |
| 门禁类型 | Agent 运行时（非仅 CI） |
| 时机 | 节点硬拦 + 终局总检（终局接口预留） |
| 检查内容 | 确定性产物为主 + 少量可判定行为规则 |
| 执行方 | CLI 脚本硬拦；skill 强制先写后检 |
| 产物形态 | 必有 `handoff.json`；可选 Markdown 正文 |
| 契约存放 | 正式写入 `card.yaml` 的 `gate` 段（单一事实来源） |
| v1 范围 | 通用检查器 + `noetic-company-profile` 试点 |

曾考虑的替代方案：

1. 轻量 handoff + CLI、不改 `card.yaml` 契约 — 改动更小，但字段易漂移
2. **在 `card.yaml` 增加正式 `gate`（本方案）**
3. 把门禁做成 workflow stage — 过重，绑死 runner

## 3. 架构

```text
card.yaml (outputs + gate)
        │
        ├─ validate_work_suite.py     # 合入：gate 形态合法
        │
        └─ check_artifact_gate.py     # 运行时：验 handoff
              ├─ --mode node          # 单节点硬拦
              └─ --mode final         # 终局总检（父 artifacts + 报告 handoff）
```

数据流：

1. Agent 按业务 skill 执行，写出 `handoff.json`（及可选 `report.md`）
2. Agent 调用 `check_artifact_gate.py --mode node ...`
3. exit code `0` 才可标记任务完成并交接；非 `0` 则失败，不启动下游
4. 编排型流程的报告节点通过 node gate 后，运行 `--mode final` 收口；Hermes 完成状态的自动拦截仍由宿主钩子负责。

## 4. `card.yaml` 的 `gate` 契约

`gate` 为可选段。未声明 `gate` 的 skill：行为与今天相同；运行时检查器对其 skip/通过并提示，不失败。

### 4.1 字段语义

| 字段 | 含义 |
| --- | --- |
| `gate.handoff` | v1 仅允许 `required`：必须落盘 `handoff.json` |
| `gate.required_outputs` | 必须出现在 `handoff.artifacts`；必须 ⊆ `outputs`；省略则默认等于全部 `outputs` |
| `gate.required_meta` | handoff 顶层必填元数据；取值限于白名单 |
| `gate.artifact_checks` | 对单个 artifact 的类型/非空等确定性检查 |
| `gate.behavior_checks` | 少量可判定行为规则；`id` 必须是已实现的检查器 ID |
| `gate.final` | 编排型 skill 终局总检配置（可选；试点原子 skill 可不写） |

`rules` 仍供人与 Agent 阅读；**硬拦只认 `gate`**，避免自然语言无法自动判定。

### 4.2 `required_meta` 白名单（v1）

- `subject`
- `sources`
- `data_as_of`
- `evidence_gaps`
- `wiki_writeback`
- `run_id`（编排运行必填，用于隔离重跑和并发）

### 4.3 `artifact_checks` 类型枚举（v1）

- `list`
- `object_or_string`
- 可选 `non_empty: true`

### 4.4 `behavior_checks`（v1 实现集）

| id | 判定规则（可自动执行） |
| --- | --- |
| `no_fabricated_empty_fill` | 顶层与 `artifacts.evidence_gaps` 都必须存在且类型为 list。v1 **不**推断「是否真有缺口」；只保证缺口槽位存在，避免用省略字段冒充完整。 |
| `data_role_no_final_report` | 当 `handoff.role == "data"` 时，禁止在 handoff **顶层**或 `artifacts` 下出现键：`final_report`、`due_diligence_report`、`investment_analysis_report`。 |

未实现的 `behavior_checks[].id` 在静态校验阶段即失败。`when` / `require` / `forbid_keys` 等 YAML 附加字段若存在，必须与上表语义一致；检查器以 `id` 为准，不解释任意自然语言。

### 4.5 试点示例：`noetic-company-profile`

```yaml
outputs:
  - company_summary
  - industry_position
  - operating_status
  - key_tags
  - risk_flags
  - evidence_gaps

gate:
  handoff: required
  required_outputs:
    - company_summary
    - industry_position
    - operating_status
    - key_tags
    - risk_flags
    - evidence_gaps
  required_meta:
    - subject
    - sources
    - data_as_of
    - evidence_gaps
    - wiki_writeback
  artifact_checks:
    evidence_gaps:
      type: list
    company_summary:
      type: object_or_string
      non_empty: true
  behavior_checks:
    - id: no_fabricated_empty_fill
      when: missing_data
      require: evidence_gaps_non_null
    - id: data_role_no_final_report
      when: role == data
      forbid_keys: [final_report, due_diligence_report]
```

### 4.6 终局配置（接口预留）

```yaml
gate:
  handoff: required
  final:
    require_parent_artifacts: [company_profile, shareholder_structure, litigation_risk, financing_history]
    require_report_handoff: true
```

静态校验：若存在 `gate.final.require_parent_artifacts`，每个名字必须能在对应 `references/workflow.yaml` 的 outputs 闭包中解析到。

## 5. handoff 与落盘约定

### 5.1 路径

与企业信息库同根（`NOETICAI_COMPANY_KB_DIR`，未设置时为 `~/.noeticai/company-knowledge`），与 `raw/`、`wiki/` 并列：

```text
<company-kb>/artifacts/<run-id>/<skill-id>/handoff.json
<company-kb>/artifacts/<run-id>/<skill-id>/report.md   # 可选
```

终局模式的 `--run-dir` 即该 `<company-kb>` 根目录。

### 5.2 `handoff.json` 最小形状

```json
{
  "skill_id": "noetic-company-profile",
  "run_id": "run-20260710-example-a1b2c3d4",
  "role": "data",
  "subject": "杭州XX科技有限公司",
  "sources": [{"name": "...", "ref": "..."}],
  "data_as_of": "2026-07-10",
  "wiki_writeback": {"status": "written|skipped|failed", "paths": []},
  "evidence_gaps": [],
  "artifacts": {
    "company_summary": {},
    "industry_position": {},
    "operating_status": {},
    "key_tags": [],
    "risk_flags": [],
    "evidence_gaps": []
  },
  "report_path": "report.md"
}
```

规则：

- `artifacts` 的 key 必须覆盖 `gate.required_outputs`
- 顶层 `evidence_gaps` 与 `artifacts.evidence_gaps` 都必须存在且为 list
- Markdown 正文默认不参与硬拦，除非契约显式要求 `report_path` 存在且文件可读
- `wiki_writeback.status` 必须为 `written` | `skipped` | `failed`

## 6. 检查脚本

新增：`scripts/check_artifact_gate.py`（标准库，零第三方依赖，与现有 validator 风格一致）。

### 6.1 接口

```bash
python3 scripts/check_artifact_gate.py \
  --mode node \
  --skill noetic-company-profile \
  --handoff <path>/handoff.json \
  --run-id <run-id> \
  [--plugin-root .]

python3 scripts/check_artifact_gate.py \
  --mode final \
  --skill noetic-due-diligence \
  --run-dir <company-kb> \
  --run-id <run-id> \
  [--plugin-root .]
```

### 6.2 退出码

| code | 含义 |
| --- | --- |
| 0 | 通过 |
| 1 | 门禁失败（字段级错误打印到 stderr） |
| 2 | 用法错误或契约无法加载 |

### 6.3 节点模式检查顺序

1. 加载 skill 的 `card.yaml`；无 `gate` → 通过并提示 skip
2. 解析 `handoff.json`
3. 校验 `required_meta`
4. 校验 `required_outputs` 与 `artifact_checks`
5. 执行已注册的 `behavior_checks`
6. 汇总错误；有错则 exit 1

### 6.4 终局模式（v1）

- 若编排型 skill 无 `gate` 或无 `gate.final`：skip 通过并提示（与无 gate 的 node 模式一致），不 exit 2
- 若有 `gate.final`：检查 `<company-kb>/artifacts/<run-id>/` 下 `require_parent_artifacts` 对应 handoff 文件存在、可解析且 `run_id` 一致；若 `require_report_handoff: true`，同样检查报告 skill handoff
- final 不重复运行父节点的完整 node 规则集，避免未铺 gate 的父节点误杀

## 7. Agent 侧约定

更新 `noetic-data-agent`（及试点业务 skill 如需要）执行规则：

1. 先写 `<run-id>` 隔离目录下的 `handoff.json`（+ 可选 `report.md`），并在顶层写入相同 `run_id`
2. 再运行 `check_artifact_gate.py --mode node ...`
3. 非 0 则任务失败：不声明完成、不交接下游

脚本是唯一硬门禁；skill 文案不替代脚本判定。

## 8. 静态校验扩展

扩展 `scripts/validate_work_suite.py`（或抽出共享解析模块供两者复用）：

- 有 `gate` 时：`handoff` 必须为 `required`
- `required_outputs` ⊆ `outputs`；省略则默认全量 outputs（解析层规范化）
- `required_meta` ⊆ 白名单
- `artifact_checks` 类型合法
- `behavior_checks[].id` ∈ 已实现集合
- 若有 `gate.final.require_parent_artifacts`，对照同 skill 的 `workflow.yaml` outputs 闭包

无 `gate`：忽略，保持兼容。

优先复用解析逻辑，避免 validator 与 gate 脚本各写一份 YAML 子集解析。

## 9. 测试计划

| 类型 | 内容 |
| --- | --- |
| 契约 | 合法/非法 `gate` 的 card fixture → validator |
| 门禁 | `tests/fixtures/gates/company-profile/`：pass、缺字段、空 `company_summary`、data 角色带 `final_report` |
| 集成 | 覆盖 node run-id 缺失/不匹配、final 父 handoff 缺失/非法 JSON/不匹配/完整通过 |

不依赖真实 MCP 或企业数据。

## 10. 落地步骤

1. 定义 `gate` 解析与共享白名单/枚举
2. 实现 `check_artifact_gate.py`（node 完整；final 最小/可 skip）
3. 为 `noetic-company-profile/card.yaml` 增加 `gate`
4. 更新 `noetic-data-agent`（必要时业务 skill）的先写后检约定
5. 扩展 `validate_work_suite` + fixture 测试
6. 为尽调和投资分析报告 card 声明 final gate，并由 workflow 任务正文传递 run-id、node/final 命令
7. 更新 `AGENTS.md` 与相关 docs：有 `gate` 的编排节点必须过脚本

## 11. 成功标准

- 缺 `company_summary` 的 handoff → 节点门禁失败（exit 1）
- 合法 handoff → 通过（exit 0）
- 未声明 `gate` 的 skill → 现有流程不受影响
- `python3 scripts/validate_work_suite.py --target all .` 与 `--self-test` 保持通过

## 12. 后续（不在 v1）

- 将 `gate` 扩到尽调全链路 data 节点 + 终局总检
- 视需要把高频字段检查进一步结构化
- 可选：CI 仅跑契约静态校验与 fixture，不跑真实 Agent
- 可选：Hermes 完成钩子接入同一脚本
