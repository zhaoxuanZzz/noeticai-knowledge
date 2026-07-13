# 跨宿主 Delegate Gate Runner 设计

## 目标

让 `delegate` 模式在 Codex、Claude、Hermes 等宿主中共享同一条可验证的完成边界：子代理返回成功不代表 workflow 节点完成；只有宿主无关的 runner 亲自验证产物并通过 gate，节点才可以解锁下游。

本设计不尝试统一不同宿主启动子代理的 API。父 Agent 仍使用当前宿主的原生委派能力，runner 只负责 DAG 状态、产物验收和依赖解锁。

## 设计原则

- `scripts/check_artifact_gate.py` 继续作为唯一 gate 判定器。
- 不依赖 `agent:end`、`subagent_stop` 等宿主生命周期 hook 保证正确性。
- 不相信子代理回复中声称的 gate 结果，也不解析自然语言回复。
- runner 是节点状态迁移和 ready 集合的唯一事实来源。
- 保留现有 `execute --mode delegate` 作为无副作用的 DAG 预览。
- 没有声明 `gate` 的 skill 保持兼容，由现有检查器按 skip/通过处理。

## 边界与强制保证

跨宿主环境无法阻止一个宿主或父 Agent 完全绕开本插件自行委派。因此，本设计的强制保证是：

> 任何由 delegate runner 标记为完成并据此解锁下游的 workflow 节点，都已经由 runner 执行对应 gate 并获得接受状态。

子代理没有修改 workflow 状态的权力。父 Agent 也不能直接将节点写成 `passed`；它只能请求 runner 验收。生命周期 hook 可以用于审计或提醒，但不参与正确性判断。

## 架构

系统分为三个边界清晰的部分：

1. **父 Agent / 宿主适配层**：读取 ready 节点，使用 Codex、Claude 或 Hermes 的原生能力启动子代理，等待其返回，然后请求 runner 验收。
2. **Delegate runner**：保存 workflow 状态、计算 ready 节点、调用 gate checker，并控制所有状态迁移。
3. **Gate checker**：读取 `card.yaml` 与 `handoff.json`，执行既有 node/final gate 判定，不感知宿主和委派方式。

执行流程：

```text
delegate init
  -> runner 固化 DAG 与 run_id
  -> delegate ready 返回当前可运行节点
  -> 父 Agent 使用宿主原生能力委派
  -> 子代理写 handoff.json 后返回
  -> delegate complete 请求 runner 验收
  -> runner 执行 node gate
       -> 失败：blocked，下游不解锁
       -> 通过：普通节点 passed
       -> 报告节点继续执行 final gate
            -> 失败：blocked
            -> 通过：passed
  -> runner 重新计算 ready 节点
```

## DAG 节点协议

现有 delegate JSON 应把 gate 信息从自然语言 `prompt` 提升为结构化字段。节点最小结构为：

```json
{
  "id": "company-profile",
  "stage": "profile",
  "skill": "noetic-company-profile",
  "parents": [],
  "handoff_path": "/company-kb/artifacts/run-xxx/noetic-company-profile/handoff.json",
  "node_gate": {
    "mode": "node",
    "run_id": "run-xxx"
  },
  "final_gate": null
}
```

报告节点的 `final_gate` 包含编排 skill、run ID 和 company KB 根目录。runner 根据这些结构化参数调用 Python 检查器，不执行从 JSON 或 prompt 中拼接出的任意 shell 字符串。

## 状态模型

运行状态写入：

```text
<company-kb>/artifacts/<run-id>/workflow-state.json
```

节点状态为：

```text
pending -> running -> validating -> passed
                           |
                           +-> blocked

running -> failed -> running
blocked -> validating
```

- `pending`：依赖尚未全部通过。
- `running`：父 Agent 已领取节点，可以委派或在当前 Agent 中执行。
- `validating`：runner 正在验收，属于短暂内部状态。
- `passed`：所需 gate 全部通过，可以解锁下游。
- `blocked`：产物或 gate 不合格，修复后可以重验。
- `failed`：子代理执行失败或未产生产物，可以重新领取。

一个节点仅在所有父节点均为 `passed` 时进入 ready 集合。`blocked`、`failed` 和子代理口头声明的成功都不满足依赖。

最小状态文件示例：

```json
{
  "schema_version": 1,
  "run_id": "run-xxx",
  "workflow_skill": "noetic-due-diligence",
  "status": "running",
  "nodes": {
    "company-profile": {
      "skill": "noetic-company-profile",
      "status": "passed",
      "attempt": 1,
      "handoff_path": "/company-kb/artifacts/run-xxx/noetic-company-profile/handoff.json",
      "gate_result_path": "/company-kb/artifacts/run-xxx/noetic-company-profile/gate-result.json"
    }
  }
}
```

## CLI

第一版提供以下入口：

```bash
# 固化 workflow DAG 并创建状态
python3 skills/noetic-workflow/scripts/noetic_workflow.py delegate init \
  --skill <orchestrating-skill> --company <company> [--run-id <id>]

# 返回当前允许领取的节点
python3 skills/noetic-workflow/scripts/noetic_workflow.py delegate ready \
  --run-id <id>

# 领取节点并将其迁移为 running
python3 skills/noetic-workflow/scripts/noetic_workflow.py delegate start \
  --run-id <id> --node <node-id>

# 子代理返回后，由 runner 亲自验收
python3 skills/noetic-workflow/scripts/noetic_workflow.py delegate complete \
  --run-id <id> --node <node-id>

# 记录子代理执行失败，使节点可以重试
python3 skills/noetic-workflow/scripts/noetic_workflow.py delegate fail \
  --run-id <id> --node <node-id> --reason <reason>

# 查看整体状态、ready 节点和阻塞原因
python3 skills/noetic-workflow/scripts/noetic_workflow.py delegate status \
  --run-id <id>
```

`execute --mode delegate` 继续只输出预览 DAG，不创建状态，也不宣称已经启用强制 gate。

## `complete` 事务

`delegate complete` 是唯一合法的成功入口，执行顺序固定：

1. 读取状态并确认节点为 `running` 或 `blocked`。
2. 验证节点、skill、run ID 和 handoff 路径均来自初始化时固化的 DAG。
3. 将状态迁移为 `validating`。
4. 调用 `check_artifact_gate.check_node()`，而不是相信子代理提供的退出码。
5. 如果是报告节点且 node gate 通过，继续调用 `check_final()`。
6. 原子写入该次 gate attempt 结果和稳定别名 `gate-result.json`。
7. gate 失败则迁移为 `blocked`；全部通过则迁移为 `passed`。
8. 根据所有父节点状态重新计算 ready 集合和 workflow 总状态。
9. 使用临时文件加 `os.replace()` 原子更新 `workflow-state.json`。

重复对 `passed` 节点调用 `complete` 返回原有成功结果，不产生新的 attempt，保证幂等。

## Gate 结果与审计

每次实际验收写入：

```text
artifacts/<run-id>/<skill-id>/gate-result-<attempt>.json
artifacts/<run-id>/<skill-id>/gate-result.json
```

结果至少包含：

- `run_id`
- `node_id`
- `skill`
- `attempt`
- `status`: `passed` 或 `blocked`
- `gate`: `node` 或 `final`
- `exit_code`
- `errors`
- `handoff_path`
- `checked_at`

不保存完整模型回复或企业原始数据。`handoff.json` 继续作为业务交接物，gate result 只保存验收事实。

## 失败与恢复

- handoff 不存在、JSON 无效、run ID 不匹配、gate 配置无效或 checker 异常时一律 fail-closed，节点进入 `blocked`。
- 修复产物后再次执行 `complete`，runner 增加 attempt 并重新检查。
- 子代理自身失败时使用 `fail` 记录原因；再次 `start` 增加执行 attempt。
- 报告节点 node gate 通过但 final gate 失败时，节点仍为 `blocked`，结果明确记录失败发生在 `final`。
- 第一版不提供 `waive`，避免将跨宿主人工审批一并引入。后续如增加，必须显式携带 reason，并限定到单一 run、节点和 gate attempt。

## 并发与所有权

第一版允许一个父编排器同时委派多个互不依赖的 ready 节点，但规定同一 run 只有一个父编排器进程负责调用 runner。状态写入仍使用原子替换，防止进程中断留下半份 JSON。

第一版不引入跨进程文件锁。若未来需要多个父编排器共同操作同一 run，再增加带版本号的 compare-and-swap 或标准库可实现的锁策略；在需求出现前不提前增加并发协议。

## 测试范围

集成测试至少覆盖：

- `init` 固化一个 run ID 和结构化 gate 参数。
- `ready` 只返回父节点全部 `passed` 的节点。
- 未生成 handoff 时 `complete` 必须失败且不能解锁下游。
- gate 失败使节点进入 `blocked`。
- 修复产物后重验可以从 `blocked` 迁移到 `passed`。
- 报告节点 node gate 通过但 final gate 失败时保持 `blocked`。
- `passed` 节点重复 `complete` 幂等。
- 两个平行节点可分别验收，互不覆盖状态。
- 子代理返回文本中的伪造成功声明不会影响验收结果。
- 没有 gate 的既有 skill 继续兼容。
- 原子写入失败时不破坏上一版有效状态文件。

## 非目标

- 不构建统一的 Codex、Claude、Hermes 子代理启动 API。
- 不使用 `agent:end` 或 `subagent_stop` 替代 runner 的完成事务。
- 不让 hook 返回值决定 workflow 状态。
- 不让 runner 生成企业分析内容。
- 不改变 Hermes Kanban 的 `pre_tool_call(kanban_complete)` 硬拦机制。
- 不在第一版实现豁免、分布式锁、常驻守护进程或 UI。

## 验收标准

当父 Agent 全程通过 runner 驱动 delegate workflow 时，即使子代理省略 gate 命令、错误声称完成或返回不合格 handoff，下游节点也不会进入 ready；只有 runner 对父节点执行 gate 并获得通过结果后，依赖节点才可被领取。
