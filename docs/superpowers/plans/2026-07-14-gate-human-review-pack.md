# Gate 人工审核包 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `needs_review` 生成人类可读的 `review.md` + 可校验的 `review-decision.json`，离线评估与运行时共用同一渲染器；第一版只记录审计，不自动 waive/解锁。

**Architecture:** 新增零依赖模块 `scripts/gate_review.py`（组装上下文、原因码中文表、Markdown 渲染、决策模板/校验）与 CLI `scripts/render_gate_review.py`。`check_artifact_gate.py` 在写入 `gate-result.json` 且决策为 `needs_review` 时同目录落盘审核包；`evaluate_gate_dataset.py` 对 `actual_decision == needs_review` 写 `.scratch/.../reviews/<case_id>/` 并在 `report.md` 加链接。复用 `semantic_gate.json_path_get` 与 `gate_judge_adapter.minimize_handoff`。

**Tech Stack:** Python 3 标准库；现有 unittest 集成测试；fixture `tests/fixtures/gate-dataset/business/judge-risk-omitted/` 与 case `judge-material-risk-omitted`。

**Spec:** `docs/superpowers/specs/2026-07-14-gate-human-review-pack-design.md`

---

## File structure

| 路径 | 职责 |
| --- | --- |
| `scripts/gate_review.py` | 核心：原因码表、hash、渲染、写模板、校验决策 |
| `scripts/render_gate_review.py` | CLI：`--handoff-dir` / `--eval-dir` / `--stdout` |
| `scripts/check_artifact_gate.py` | `needs_review` 时调用写审核包 |
| `scripts/evaluate_gate_dataset.py` | `_write_reports` 写 `reviews/` + 更新 `report.md` |
| `tests/integration/test_gate_review.py` | 新测试文件（单元 + 小集成） |
| `tests/integration/test_artifact_gate.py` | 补一条：needs_review 落盘审核包 |
| `tests/integration/test_gate_dataset.py` | 补一条：eval 输出含 reviews |
| `README.md` 或 gate-dataset 设计 §10 | 一行用法 |

不新建包目录；脚本与现有 `scripts/*.py` 同级，import 方式与 `check_artifact_gate` 一致（`sys.path` 含 `scripts/`）。

---

### Task 1: `gate_review` 原因码表 + finding 摘录

**Files:**
- Create: `scripts/gate_review.py`
- Create: `tests/integration/test_gate_review.py`
- Test: `tests/integration/test_gate_review.py`

- [ ] **Step 1: 写失败测试 — 原因码映射与摘录**

```python
# tests/integration/test_gate_review.py
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from gate_review import excerpt_finding, reason_label  # noqa: E402


class GateReviewUnitTests(unittest.TestCase):
    def test_reason_label_known(self) -> None:
        title, blurb = reason_label("material_risk_omitted")
        self.assertIn("风险", title)
        self.assertTrue(blurb)

    def test_reason_label_unknown(self) -> None:
        title, blurb = reason_label("totally_unknown_reason_xyz")
        self.assertEqual(title, "totally_unknown_reason_xyz")
        self.assertIn("未知", blurb)

    def test_excerpt_finding_from_handoff_and_evidence(self) -> None:
        handoff = {
            "artifacts": {
                "risk_flags": [],
                "company_summary": {"status": "存续"},
            }
        }
        evidence = {
            "evidence": [
                {
                    "id": "e-risk",
                    "field": "risk",
                    "value": "高风险",
                    "source_ref": "raw/risk.json#/risk",
                }
            ]
        }
        finding = {
            "reason": "material_risk_omitted",
            "artifact_path": "artifacts.risk_flags",
            "evidence_refs": ["e-risk"],
        }
        block = excerpt_finding(finding, handoff, evidence)
        self.assertEqual(block["artifact_path"], "artifacts.risk_flags")
        self.assertEqual(block["artifact_value"], [])
        self.assertEqual(len(block["evidence"]), 1)
        self.assertEqual(block["evidence"][0]["id"], "e-risk")
        self.assertEqual(block["evidence"][0]["value"], "高风险")
```

- [ ] **Step 2: 运行确认失败**

```bash
cd /Users/zhaoxuan/code/company-work-suite
uv run python -m unittest tests.integration.test_gate_review.GateReviewUnitTests -v
```

Expected: `ModuleNotFoundError: gate_review` 或 import 失败。

- [ ] **Step 3: 最小实现**

在 `scripts/gate_review.py` 实现：

```python
REASON_CATALOG: dict[str, tuple[str, str]] = {
    "material_risk_omitted": ("重大风险遗漏", "证据显示存在重大风险，但产物未披露。"),
    "source_conflict_not_disclosed": ("来源冲突未披露", "多来源结论冲突且未写入 conflicts/evidence_gaps。"),
    "judge_low_confidence": ("Judge 置信度不足", "语义评审置信度低于阈值，需人工确认。"),
    "judge_unavailable": ("Judge 不可用", "adapter/密钥/超时导致无法评审，fail closed。"),
    "judge_disabled": ("Judge 已禁用", "CWS_JUDGE_ENABLED=0，不能当作语义通过。"),
    "judge_needs_review": ("语义需复核", "Judge 返回 needs_review。"),
    # 再补设计 §9 / semantic_gate 常见 reason；未知走 fallback
}

def reason_label(reason: str) -> tuple[str, str]:
    if reason in REASON_CATALOG:
        return REASON_CATALOG[reason]
    return reason, "未知原因码，请按 artifact/evidence 人工判断。"

def excerpt_finding(finding, handoff, evidence_doc) -> dict:
    # 用 semantic_gate.json_path_get 取 artifact_path
    # 按 evidence_refs 从 evidence_doc["evidence"] 取 id 匹配
    # 缺失 path → artifact_value = None 并标记 missing
    ...
```

只返回结构化 dict，不渲染 Markdown。未知 `evidence_ref` 记入 `missing_evidence_refs`，不抛异常。

- [ ] **Step 4: 跑测试至通过**

```bash
uv run python -m unittest tests.integration.test_gate_review.GateReviewUnitTests -v
```

Expected: OK。

- [ ] **Step 5: Commit**（仅当用户要求提交时再做）

---

### Task 2: `gate_input_hash` + 决策模板 / 校验

**Files:**
- Modify: `scripts/gate_review.py`
- Modify: `tests/integration/test_gate_review.py`

- [ ] **Step 1: 写失败测试**

```python
class GateReviewDecisionTests(unittest.TestCase):
    def test_runtime_hash_stable(self) -> None:
        from gate_review import compute_runtime_gate_input_hash

        handoff = {"run_id": "r1", "artifacts": {"a": 1}}
        evidence = {"evidence": [], "claims": []}
        judge = {"decision": "needs_review", "confidence": 0.5, "findings": []}
        h1 = compute_runtime_gate_input_hash(handoff, evidence, judge)
        h2 = compute_runtime_gate_input_hash(handoff, evidence, judge)
        self.assertTrue(h1.startswith("sha256:"))
        self.assertEqual(h1, h2)
        judge2 = {**judge, "confidence": 0.51}
        self.assertNotEqual(h1, compute_runtime_gate_input_hash(handoff, evidence, judge2))

    def test_build_pending_decision_and_complete_validation(self) -> None:
        from gate_review import build_decision_template, validate_decision

        findings = [
            {
                "reason": "material_risk_omitted",
                "artifact_path": "artifacts.risk_flags",
                "evidence_refs": ["e-risk"],
            }
        ]
        tmpl = build_decision_template(findings, gate_input_hash="sha256:abc")
        self.assertEqual(tmpl["status"], "pending")
        self.assertIsNone(tmpl["action"])
        self.assertEqual(tmpl["findings"][0]["verdict"], None)

        bad = dict(tmpl)
        bad["status"] = "completed"
        ok, errs = validate_decision(bad, expected_hash="sha256:abc")
        self.assertFalse(ok)
        self.assertTrue(any("verdict" in e or "action" in e for e in errs))

        good = {
            **tmpl,
            "status": "completed",
            "reviewed_at": "2026-07-14T00:00:00Z",
            "reviewer": "tester",
            "action": "reject_rerun",
            "action_reason": "确认遗漏高风险",
            "findings": [
                {
                    "reason": "material_risk_omitted",
                    "artifact_path": "artifacts.risk_flags",
                    "verdict": "confirm",
                    "note": "ok",
                }
            ],
        }
        ok, errs = validate_decision(good, expected_hash="sha256:abc")
        self.assertTrue(ok, errs)

        stale = {**good, "gate_input_hash": "sha256:old"}
        ok, errs = validate_decision(stale, expected_hash="sha256:abc")
        self.assertFalse(ok)
```

- [ ] **Step 2: 跑测确认失败**

```bash
uv run python -m unittest tests.integration.test_gate_review.GateReviewDecisionTests -v
```

- [ ] **Step 3: 实现**

- `compute_runtime_gate_input_hash(handoff, evidence, judge)`：对 `minimize_handoff(handoff)` + 最小化 evidence（保留 `evidence`/`claims`/`conflicts`/`subject`/`run_id`）+ judge 子集（`decision`/`confidence`/`model`/`rubric_version`/`findings`）做 `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 后 SHA-256，前缀 `sha256:`。
- `build_decision_template(findings, gate_input_hash)`：按设计 §6.1。
- `validate_decision(doc, expected_hash)`：
  - `pending`：只检查 schema_version / hash 字段存在即可返回 ok（或仅校验 hash 匹配）。
  - `completed`：每个 finding 的 `verdict ∈ {confirm,reject,uncertain}`；`action ∈ {waive,reject_rerun,capability_gap}`；`action_reason` 非空字符串；`gate_input_hash == expected_hash`。
- 常量：`FINDING_VERDICTS`、`PACKAGE_ACTIONS`。

- [ ] **Step 4: 跑测通过**

```bash
uv run python -m unittest tests.integration.test_gate_review.GateReviewDecisionTests -v
```

---

### Task 3: Markdown 渲染 + 写盘 API

**Files:**
- Modify: `scripts/gate_review.py`
- Modify: `tests/integration/test_gate_review.py`
- Use fixture: `tests/fixtures/gate-dataset/business/judge-risk-omitted/` + schema base handoff

- [ ] **Step 1: 写失败测试 — 渲染含中文与证据**

构造最小上下文（可手写，不必跑完整 gate）：

```python
class GateReviewRenderTests(unittest.TestCase):
    def test_render_markdown_contains_sections(self) -> None:
        from gate_review import render_review_markdown, write_review_pack

        ctx = {
            "subject_name": "杭州示例科技有限公司",
            "skill_id": "cws-company-profile",
            "decision": "needs_review",
            "run_id": "run-dataset",
            "case_id": None,
            "checked_at": "2026-07-14T00:00:00Z",
            "judge": {
                "decision": "needs_review",
                "confidence": 0.71,
                "model": "mock",
                "rubric_version": "company-profile-v1",
                "findings": [
                    {
                        "reason": "material_risk_omitted",
                        "artifact_path": "artifacts.risk_flags",
                        "evidence_refs": ["e-risk"],
                    }
                ],
            },
            "handoff": {"artifacts": {"risk_flags": []}},
            "evidence": {
                "evidence": [
                    {
                        "id": "e-risk",
                        "field": "risk",
                        "value": "高风险",
                        "source_ref": "raw/risk.json#/risk",
                    }
                ]
            },
            "expected_decision": None,
            "actual_decision": "needs_review",
        }
        md = render_review_markdown(ctx)
        self.assertIn("# Gate 人工审核", md)
        self.assertIn("重大风险遗漏", md)
        self.assertIn("高风险", md)
        self.assertIn("review-decision.json", md)

    def test_write_review_pack_creates_files(self) -> None:
        import tempfile
        from pathlib import Path
        from gate_review import write_review_pack, validate_decision
        import json

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # 复用上例 ctx，或从 fixture 读 evidence
            ...
            paths = write_review_pack(out, ctx, gate_input_hash="sha256:test")
            self.assertTrue(paths["review_md"].is_file())
            self.assertTrue(paths["decision_json"].is_file())
            decision = json.loads(paths["decision_json"].read_text(encoding="utf-8"))
            self.assertEqual(decision["status"], "pending")
            self.assertEqual(decision["gate_input_hash"], "sha256:test")
```

离线章节：若 `expected_decision` 非空，Markdown 必须出现「期望决策」行。

- [ ] **Step 2: 跑测失败**

```bash
uv run python -m unittest tests.integration.test_gate_review.GateReviewRenderTests -v
```

- [ ] **Step 3: 实现 `render_review_markdown` / `write_review_pack`**

章节顺序严格按设计 §5。`write_review_pack(dir, ctx, gate_input_hash)`：

1. 写 `review.md`（utf-8）
2. 写 `review-decision.json`（indent=2 + 尾换行）
3. 返回路径 dict

不覆盖已有 `status=completed` 的决策文件：若目标 `review-decision.json` 已存在且 `status==completed` 且 hash 仍匹配，则只更新 `review.md`，保留决策；若 hash 不匹配则重写 pending 模板（并在 md 顶部提示「产物已变，旧决策作废」——可选，第一版可简化为始终重写 pending，测试注明）。

**第一版简化（推荐）：** 每次生成都覆盖为 pending 模板；人工 completed 文件需自行备份。在 md「决策填写说明」注明勿手改 `review.md`。

- [ ] **Step 4: 跑测通过**

---

### Task 4: CLI `render_gate_review.py`

**Files:**
- Create: `scripts/render_gate_review.py`
- Modify: `tests/integration/test_gate_review.py`

- [ ] **Step 1: 写失败测试 — handoff-dir 模式**

在临时目录放入最小 `handoff.json` / `evidence.json` / `gate-result.json`（decision=needs_review + finding），调用 CLI 子进程：

```python
class GateReviewCliTests(unittest.TestCase):
    def test_cli_handoff_dir_writes_pack(self) -> None:
        import json, subprocess, tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "handoff.json").write_text(
                json.dumps({"run_id": "r1", "subject": {"name": "X"}, "artifacts": {"risk_flags": []}}, ensure_ascii=False),
                encoding="utf-8",
            )
            (d / "evidence.json").write_text(
                json.dumps({"evidence": [{"id": "e1", "field": "risk", "value": "高", "source_ref": "raw/a.json#/r"}], "claims": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            (d / "gate-result.json").write_text(
                json.dumps({
                    "decision": "needs_review",
                    "skill_id": "cws-company-profile",
                    "run_id": "r1",
                    "judge": {
                        "decision": "needs_review",
                        "confidence": 0.6,
                        "model": "mock",
                        "rubric_version": "company-profile-v1",
                        "findings": [{"reason": "material_risk_omitted", "artifact_path": "artifacts.risk_flags", "evidence_refs": ["e1"]}],
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "render_gate_review.py"), "--handoff-dir", str(d)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((d / "review.md").is_file())
            self.assertTrue((d / "review-decision.json").is_file())
```

另测：`--stdout` 不强制写盘时可打印 md（若设计为「默认写盘 + stdout 额外打印」，则两者都有）；`decision != needs_review` 时 exit 0 且不写或 stderr 提示 skip——**约定：非 needs_review 时 exit 0、不写文件、stderr 一行 `skip: decision=...`**。

- [ ] **Step 2: 实现 CLI**

```text
usage: render_gate_review.py (--handoff-dir DIR | --eval-dir DIR) [--stdout] [--case-id ID]
```

- `--handoff-dir`：读同目录三文件，算 runtime hash，写包。
- `--eval-dir`：读 `results.json`（或 `needs-review.json`），对每个/指定 `case_id` 组装 ctx。离线 ctx 需从 dataset 再读 handoff/evidence——**第一版 eval 模式优先由 evaluate 内嵌调用 `write_review_pack`，CLI `--eval-dir` 仅对已有 `reviews/` 旁的缓存或 results 里已嵌入的最小快照重渲染**。

**简化以避免耦合过重：**

| 模式 | 行为 |
| --- | --- |
| `--handoff-dir` | 完整：读盘 → 渲染 → 写包 |
| `--eval-dir` | 若存在 `reviews/<case>/` 旁的 `context.json`（evaluate 写入的最小快照）则重渲染；否则从 `needs-review.json` + 可选 `--dataset` 解析 |

为控制范围，**Task 4 只实现 `--handoff-dir` + `--stdout`**。`--eval-dir` 放到 Task 6 与 evaluate 一起：evaluate 直接调 `write_review_pack`；CLI eval 模式可选后续。

若仍要 CLI eval：evaluate 写 `reviews/<case_id>/context.json`（最小化 handoff/evidence/judge/meta），CLI 只读 context 重渲染。推荐这样做，避免 CLI 依赖 dataset 路径。

- [ ] **Step 3: 跑测通过**

```bash
uv run python -m unittest tests.integration.test_gate_review.GateReviewCliTests -v
```

---

### Task 5: 接入 `check_artifact_gate.py`

**Files:**
- Modify: `scripts/check_artifact_gate.py`（约 535–553 行 `_write_gate_result_file` 之后）
- Modify: `tests/integration/test_artifact_gate.py`

- [ ] **Step 1: 找现有 needs_review 测试或加 fixture 驱动**

现有测试常用 `CWS_JUDGE_MODE=mock`。加测试：构造临时 handoff 目录，mock judge 返回 `needs_review` + finding，跑 `check_artifact_gate`，断言 `review.md` / `review-decision.json` 存在且 md 含原因中文。

若现有测试已有 needs_review 路径，扩展断言即可。

```python
def test_needs_review_writes_review_pack(self) -> None:
    # 环境：CWS_JUDGE_MODE=mock，必要时用可返回 needs_review 的 mock adapter
    # 跑 node gate
    self.assertTrue((handoff_dir / "review.md").is_file())
    self.assertTrue((handoff_dir / "review-decision.json").is_file())
```

注意：默认 mock 可能返回 `passed`。查看 `scripts/cws_gate_judge.py` / `judge_adapter_mock.sh`：若 mock 固定 passed，则测试直接调用 `write_review_pack` 的集成可放在 `test_gate_review`，artifact_gate 测试用 **注入**：在写完 `gate-result` 后手动构造 outcome，或临时设置 mock 响应文件。

**推荐最小侵入：** 在 `check_artifact_gate.main` 写完 `gate-result` 后：

```python
if write_result and outcome.decision == "needs_review" and result_dir is not None:
    from gate_review import write_review_pack_from_handoff_dir
    write_review_pack_from_handoff_dir(result_dir)
```

`write_review_pack_from_handoff_dir` 读已写入的 `gate-result.json` + handoff + evidence。单测可 **不跑完整 judge**：写好三文件后直接调该函数（放在 `test_gate_review`），artifact_gate 测 **调用链**：patch 或最小 e2e。

**本 Task 验收：**

1. `write_review_pack_from_handoff_dir` 有单测（Task 4/3 已覆盖逻辑）。
2. `check_artifact_gate` 在 needs_review 分支调用它；集成测用临时目录模拟「gate 已写完 result」或完整跑 mock。

- [ ] **Step 2: 实现接入**

仅在 `outcome.decision == "needs_review"` 且 `write_result` 时调用。失败写包不应掩盖 gate exit code：写包异常记 stderr warning，仍返回原 `exit_code`（或 fail soft）。**约定：写包失败 print stderr，exit 仍为 gate 的 1。**

- [ ] **Step 3: 跑相关测试**

```bash
uv run python -m unittest tests.integration.test_artifact_gate -v
uv run python -m unittest tests.integration.test_gate_review -v
```

---

### Task 6: 接入 `evaluate_gate_dataset.py`

**Files:**
- Modify: `scripts/evaluate_gate_dataset.py`（`_write_reports`）
- Modify: `tests/integration/test_gate_dataset.py`
- Optionally: `scripts/render_gate_review.py` 增加 `--eval-dir`（读 `reviews/*/context.json`）

- [ ] **Step 1: 写失败测试**

在现有 semantic/ci 小评估测试中，对会产生 `needs_review` 的 case（`judge-material-risk-omitted`，需 mock judge 返回该 finding），断言：

```text
output/reviews/judge-material-risk-omitted/review.md
output/reviews/judge-material-risk-omitted/review-decision.json
```

且 `report.md` 含 `reviews/judge-material-risk-omitted/review.md`。

若 `ci` profile 把纯 judge case 标为 `not_run`，则：

- 测试用 `--profile semantic` + `CWS_JUDGE_MODE=mock`，并确保 mock 对 `material_risk_omitted` 类请求返回 `needs_review`；或
- 在 `_write_reports` 单测中 **直接传入** 伪造的 `results` 列表（含 `actual_decision=needs_review` + 嵌入的 handoff/evidence/judge 摘要），避免依赖真实 Judge。

**推荐：** 扩展 result dict（评估时已有 `judge` 字段），`_write_reports` 需要 handoff/evidence 才能渲染 → 在 `_evaluate_case` 返回值增加可选 `review_context`（最小化），仅当 `actual_decision == needs_review` 时填充。测试可单元测 `_write_reports` 而不跑全数据集。

```python
def test_write_reports_emits_review_pack(self) -> None:
    from evaluate_gate_dataset import _write_reports
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        results = [{
            "case_id": "judge-material-risk-omitted",
            "actual_decision": "needs_review",
            "expected_decision": "needs_review",
            "input_hash": "sha256:casehash",
            "judge": {...},
            "review_context": {
                "skill_id": "cws-company-profile",
                "subject_name": "杭州示例科技有限公司",
                "handoff": {...},
                "evidence": {...},
            },
            ...
        }]
        _write_reports(out, "semantic", results, [])
        self.assertTrue((out / "reviews" / "judge-material-risk-omitted" / "review.md").is_file())
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("reviews/judge-material-risk-omitted/review.md", report)
```

- [ ] **Step 2: 实现**

1. `_evaluate_case`：needs_review 时附带 `review_context` + 使用已有 `input_hash` 作为 `gate_input_hash`。
2. `_write_reports`：对每个 needs_review 结果 `mkdir reviews/<case_id>`，调用 `write_review_pack`；可选写 `context.json` 供 CLI 重渲染。
3. `report.md` 增加小节：

```markdown
## Needs review

| case_id | review |
| --- | --- |
| judge-material-risk-omitted | [review.md](reviews/judge-material-risk-omitted/review.md) |
```

- [ ] **Step 3: 跑测**

```bash
uv run python -m unittest tests.integration.test_gate_dataset -v
```

---

### Task 7: 文档一行 + 设计状态更新

**Files:**
- Modify: `README.md`（开发节）或 `docs/superpowers/specs/2026-07-13-gate-dataset-design.md` §10.3
- Modify: `docs/superpowers/specs/2026-07-14-gate-human-review-pack-design.md` 状态 → `设计已确认，实现中/已落地`

- [ ] **Step 1: 补用法**

```bash
# 从 handoff 目录重生成人工审核包（needs_review）
python3 scripts/render_gate_review.py --handoff-dir <company-kb>/artifacts/<run-id>/<skill-id>
```

- [ ] **Step 2: 全量回归**

```bash
uv run python -m unittest tests.integration.test_gate_review tests.integration.test_artifact_gate tests.integration.test_gate_dataset -v
uv run python3 scripts/validate_work_suite.py --target all .
```

---

## 实现约束（执行时遵守）

1. **零额外依赖**；优先 `uv run python`。
2. **不改** waive 解锁、atomic loop、Judge 协议。
3. **不为** `blocked`/`passed` 强制写审核包。
4. 复用 `minimize_handoff`、`json_path_get`；原因码表放 `gate_review.py` 内常量，不放可执行 YAML。
5. 审核包与机器 JSON 并存；不删除/缩减 `needs-review.json` 字段。
6. 代码量控制：`gate_review.py` 目标 < 350 行；CLI < 120 行。

---

## 验收对照（设计 §9）

| 标准 | 由何覆盖 |
| --- | --- |
| 人不打开 needs-review.json 也能理解 | Task 3 渲染含中文标题、产物值、证据 |
| 同一渲染器服务 offline/runtime | Task 5–6 都调 `write_review_pack` |
| 机器 JSON 仍完整 | 不改 results/gate-result 原有字段 |
| 决策可校验、hash 失配拒绝 | Task 2 |
| 无密钥无网络可生成 | CLI + fixture 测试 |

---

## 执行交接

计划路径：`docs/superpowers/plans/2026-07-14-gate-human-review-pack.md`

两种执行方式：

1. **Subagent-driven（推荐）** — 每 Task 新开子代理，Task 间人工/主代理复核。
2. **Inline** — 本会话按 Task 顺序实现，每 Task 跑对应 unittest。

请回复 **按计划实现** 或指出计划需改之处；确认后再改代码。
