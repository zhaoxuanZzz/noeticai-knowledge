# Repository Guidelines

## 项目结构与模块组织

本仓库是 NoeticAI Work Suite 插件，不是传统应用服务；核心组织单元是可发现的 skill。

- `skills/<skill-name>/SKILL.md` 存放每个卡片或 workflow skill 的 Agent 执行说明。
- `skills/<skill-name>/card.yaml` 存放知识卡片的结构化元数据。
- `skills/<orchestrating-skill>/references/workflow.yaml` 定义编排型 skill 的前置流程，例如企业尽调和投资分析。
- `.codex-plugin/plugin.json`、`.claude-plugin/plugin.json`、`plugin.yaml`、`__init__.py` 是不同宿主的兼容入口。
- `scripts/validate_work_suite.py` 是静态契约校验脚本（含 `card.yaml` 的可选 `gate` 段）。
- `scripts/check_artifact_gate.py` 是 Agent 运行时产物门禁（验 `handoff.json`）。
- `scripts/card_gate.py` 是 gate 契约的共享解析与白名单。
- `tests/integration/` 存放 workflow 与 Hermes 后端集成测试；测试数据在 `tests/fixtures/`。
- `docs/` 存放设计说明和 workflow 架构文档。

## 构建、测试与开发命令

本仓库没有构建步骤。提交前优先校验静态结构：

```bash
python3 scripts/validate_work_suite.py .
python3 scripts/validate_work_suite.py --target all .
python3 scripts/validate_work_suite.py --self-test
python3 -m unittest tests.integration.test_noetic_workflow
python3 -m unittest tests.integration.test_artifact_gate
python3 -m unittest tests.integration.test_delegate_runner
```

使用 `--target codex|claude|hermes|work-suite` 做单宿主或单契约校验。修改 `skills/noetic-workflow/scripts/noetic_workflow.py`、workflow YAML 或插件注册逻辑时，运行集成测试。修改 `gate` 契约或 `check_artifact_gate.py` 时，运行 `test_artifact_gate`；修改跨宿主 delegate runner 时，运行 `test_delegate_runner`。

有 `gate` 的编排节点：Agent 必须先写企业信息库下的 `artifacts/<run-id>/<skill-id>/handoff.json`（根目录同 `NOETICAI_COMPANY_KB_DIR` / `~/.noeticai/company-knowledge`），并在 handoff 顶层写同一 `run_id`，再跑：

```bash
python3 scripts/check_artifact_gate.py --mode node --skill <skill-id> --handoff <company-kb>/artifacts/<run-id>/<skill-id>/handoff.json --run-id <run-id> --plugin-root .
```

exit code 非 0 不得交接下游。报告节点通过 node gate 后，运行 `--mode final --skill <orchestrating-skill> --run-dir <company-kb> --run-id <run-id>`；未声明 `gate` 的 skill 行为不变。

## 编码风格与命名约定

优先使用 Python 3 标准库。除非宿主契约已经要求依赖，否则 validator 和 workflow 辅助脚本保持零依赖。Python 使用 4 空格缩进；公共 helper 保留类型标注；错误信息应包含相关路径、stage 或字段。

Skill 名称使用 `noetic-` 前缀和小写 kebab-case，例如 `noetic-company-profile`。Workflow stage ID 应短、稳定、语义清晰。

## 测试规范

修改 workflow 编译、静态校验、Hermes 命令生成、gate 契约或 profile 处理时，在 `tests/integration/` 增加或更新最小相关测试。测试 fixture 应确定性强，并放在 `tests/fixtures/`（gate handoff 放在 `tests/fixtures/gates/`）。

仅修改文档通常不需要跑测试；如果改动涉及目录结构或 manifest，至少运行静态校验。

## 提交与 Pull Request 规范

现有提交使用简短祈使句，例如 `Add bearer token authentication...`、`Remove .env.example...`、`Update docs...`。继续沿用这种风格，并让标题具体说明改动对象。

PR 应包含简短摘要、受影响的 skill 或宿主入口、已运行命令，以及已知校验缺口。只有修改渲染文档或宿主 UI 行为时才需要截图。

## 安全与配置提示

不要提交企业数据、本地 wiki 内容或真实密钥。`QCC_MCP_TOKEN` 在 `plugin.yaml` 中声明为必需 secret，具体值应放在宿主环境中。默认企业知识库位于仓库外的 `~/.noeticai/company-knowledge`，可用 `NOETICAI_COMPANY_KB_DIR` 覆盖；运行时 `artifacts/` 与 `raw/`、`wiki/` 同根落在该目录下。Kanban 任务工作目录默认为 `~/.noeticai/kanban-runs/<tenant>`，可用 `NOETICAI_KANBAN_RUNS_DIR` 覆盖根目录。
