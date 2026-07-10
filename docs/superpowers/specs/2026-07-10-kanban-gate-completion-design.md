# Kanban Gate 完成拦截设计

## 目标

让声明在 `card.yaml` 中的 gate 成为 Hermes Kanban 的真实完成边界。Agent
可以写出 handoff，但只有 gate 通过或人工明确豁免后，任务才可以完成并解锁
下游。`delegate` 复用同一份产物与结果协议，但不假装拥有 Kanban 的任务状态机。

## 范围与边界

- Hermes Kanban 是唯一的硬完成拦截点。
- `scripts/check_artifact_gate.py` 仍是唯一 gate 判定器。hook 可以注入要求，
  但不承担完成拦截。
- 没有声明 `gate` 的既有 skill 保持现有行为。
- 不新增通用审批系统，也不新造 workflow 引擎。

## 完成状态机

```text
running
  -> gate_pending
     -> completed       （通过）
     -> gate_blocked    （失败，等待人工介入）
     -> completed       （已豁免，豁免信息保留）
```

worker 不再直接调用原生 Kanban 完成状态迁移，而是统一进入带 gate 的完成入口。
报告任务先跑 node gate，再跑 final gate；最后一个必需 gate 为 `passed` 或
`waived` 后，完成入口才调用原生完成动作。

## 完成入口

完成入口针对一个 task 依次执行：

1. 定位 task 对应的 skill、run ID 与预期 handoff。
2. 对普通节点运行 node mode；报告节点在 node mode 成功后继续运行 final mode。
3. 为本次检查落一份结果记录。
4. 仅当结果为 `passed` 或 `waived` 时完成任务；否则将任务置为
   `gate_blocked`，所有子任务保持不可执行。

所有解析或执行异常都 fail-closed：run ID、handoff、gate 配置或结果记录缺失/
损坏，或检查器异常退出，均阻断完成。状态迁移必须使用原生的条件/原子更新，避免
两个 worker 同时完成，或审计记录尚未落盘就完成任务。

## 依赖解锁规则

子节点仅在每个父节点都已完成，且完成原因属于 `passed` 或 `waived` 时才可
ready。`gate_blocked` 在依赖上不算完成。若父节点为人工豁免，下游任务上下文必须
带入简短豁免摘要，不能把它当作无瑕疵的输入。

## 人工介入

被拦截的 node gate 或 final gate 都有两条恢复路径：

1. **修复并重验。** 人工补充信息或修复产物；原任务重跑原 gate，只有通过后
   才能完成。
2. **豁免。** 人工记录为何可接受，并明确豁免该节点或终局 gate。任务可完成，
   但豁免永久可追溯，且对下游可见。

豁免只作用于一个 run、一个 task 和一种 gate 类型，绝不作为可复用的全局绕过。

## 检查结果记录

每次检查都在该 task 的 artifact 目录下保存不可覆盖的结果。最新结果可以用
`gate-result.json` 定位，历史尝试必须仍可访问。最小结构如下：

```json
{
  "run_id": "run-...",
  "skill_id": "noetic-company-profile",
  "gate": "node",
  "status": "passed | blocked | waived",
  "attempt": 2,
  "checked_at": "2026-07-10T00:00:00Z",
  "handoff_path": ".../handoff.json",
  "exit_code": 1,
  "errors": ["missing required_output ..."],
  "waiver": {
    "reason": "人工确认该公开字段不可获得",
    "actor": "user-id",
    "waived_at": "2026-07-10T00:00:00Z"
  }
}
```

只有 `status=waived` 时才出现 `waiver`。结果记录只保存检查错误与路径引用，
不保存完整模型输出或企业原始数据。`handoff.json` 仍是业务交接物，不混入执行审计
历史。

## 跨宿主 delegate 协议

每个委派 DAG 节点携带相同、宿主无关的字段：

```json
{
  "handoff_path": ".../artifacts/<run-id>/<skill>/handoff.json",
  "gate_command": "python3 scripts/check_artifact_gate.py ...",
  "completion_evidence": ".../gate-result.json",
  "accepted_gate_statuses": ["passed", "waived"]
}
```

Codex、Claude 等宿主遵守这份协议：写 handoff、运行 gate、落结果记录；只有接受
状态才能调度下游。出现 `blocked` 时停止委派并把结果交给人工。它们不宣称提供
Hermes 原生的任务状态硬拦截。

## 验证范围

最小测试集覆盖：

- node/final gate 的通过、失败、豁免、修复后重验；
- 被拦截的父节点绝不解锁子节点；
- 已豁免的父节点可以解锁子节点，且子节点拿到豁免摘要；
- 委派 DAG 输出包含该协议，不包含 Hermes 状态字段；
- 没有 `gate` 的 skill 继续兼容。

## 非目标

- 不用 hook 替代现有检查器。
- 不让 `auto` 模式声称满足静态 workflow gate 合规。
- 不增加超出单任务 gate 豁免范围的审批流程。
