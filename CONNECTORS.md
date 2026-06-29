# Connectors

## 连接器说明

本插件保留 `.mcp.json` 作为 Codex plugin companion 配置，当前已列出企查查 MCP 服务端点。

本插件不定义连接协议，也不把 MCP 工具名写入业务 skill workflow。业务 skill 只通过 `card.yaml` 的 `data_needs` 描述所需企业数据，并在 skill 内先查企业信息库 wiki、再按缺口补齐公开企业信息。

## 本插件的连接器

| 类别 | Companion 配置 | 增强能力 | 状态 |
|------|----------------|---------|------|
| 企业信息库 | `skills/karpathy-llm-wiki` | 复用 `~/.noeticai/company-knowledge` 或 `NOETICAI_COMPANY_KB_DIR` 中已整理的企业信息 | 已内置 |
| 企查查 | `.mcp.json` | 工商信息、股东结构、司法案件、融资历史等企业数据查询 | 已配置端点 |

## 无连接器时的工作方式

不安装企查查连接器，本插件的所有技能均可正常运行：

- 已有企业数据 → 优先从企业信息库 `wiki/` 读取
- 企业数据 → 基于用户提供的资料分析
- 数据缺口 → 在输出中明确列出 `evidence_gaps`
- 结论依据 → 标注数据来源，不编造工商、股权或司法数据

## 安装连接器后的增强

| 连接器 | 受益最大的 Skill | 增强效果 |
|--------|----------------|---------|
| 企业信息库 | 全部 Skill | 先复用 wiki 中未过期的企业信息，减少付费接口调用 |
| 企查查 MCP | 企业画像、股权结构分析、司法风险分析、融资历史分析、企业基本信息查询 | 仅在 wiki 无命中、主体不确定、字段缺失或数据过期时，按本 skill 的 `card.yaml.data_needs` 补齐公开企业信息，并在输出中标注数据来源和查询时间 |

补齐后，将完整返回作为 raw source 写入企业信息库 `raw/`，再按 `karpathy-llm-wiki` Ingest 流程整理进 `wiki/`。业务 skill 最终标注企业 wiki 写回状态。
