#!/usr/bin/env python3
"""Shared card.yaml gate contract parsing and static validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any


REQUIRED_META_WHITELIST = frozenset(
    {
        "subject",
        "sources",
        "data_as_of",
        "evidence_gaps",
        "wiki_writeback",
        "run_id",
    }
)

ARTIFACT_CHECK_TYPES = frozenset({"list", "object_or_string"})

BEHAVIOR_CHECK_IDS = frozenset(
    {
        "no_fabricated_empty_fill",
        "data_role_no_final_report",
    }
)

DATA_ROLE_FORBIDDEN_KEYS = frozenset(
    {
        "final_report",
        "due_diligence_report",
        "investment_analysis_report",
    }
)

WIKI_WRITEBACK_STATUSES = frozenset({"written", "skipped", "failed"})


class CardGateError(Exception):
    """Raised when a card.yaml gate contract cannot be loaded or is invalid."""


def _parse_scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    if text in {"true", "True"}:
        return True
    if text in {"false", "False"}:
        return False
    if text in {"null", "Null", "~"}:
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item.strip()) for item in inner.split(",")]
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return text


def _empty_child_for_lookahead(lines: list[str], start: int, parent_indent: int) -> Any:
    for raw in lines[start:]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= parent_indent:
            return {}
        return [] if raw.strip().startswith("- ") else {}
    return {}


def parse_card_yaml(text: str) -> dict[str, Any]:
    """Parse the card.yaml YAML subset used by gate contracts."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        line_no = i

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]

        if line.startswith("- "):
            body = line[2:].strip()
            if not isinstance(container, list):
                raise CardGateError(f"line {line_no}: list item outside a list")
            # Mapping list item when body looks like "key:" / "key: value"
            # and is not an inline list / quoted scalar.
            is_mapping_item = (
                ":" in body
                and not body.startswith("[")
                and not (body.startswith('"') or body.startswith("'"))
            )
            if is_mapping_item:
                item: dict[str, Any] = {}
                container.append(item)
                key, _, value = body.partition(":")
                key = key.strip()
                value = value.strip()
                if value == "":
                    child = _empty_child_for_lookahead(lines, i, indent)
                    item[key] = child
                    stack.append((indent, item))
                    stack.append((indent + 2, child))
                else:
                    item[key] = _parse_scalar(value)
                    stack.append((indent, item))
            else:
                container.append(_parse_scalar(body) if body else "")
            continue

        if ":" not in line:
            raise CardGateError(f"line {line_no}: unsupported YAML shape")
        if not isinstance(container, dict):
            raise CardGateError(f"line {line_no}: mapping entry inside a non-mapping")

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            child = _empty_child_for_lookahead(lines, i, indent)
            container[key] = child
            stack.append((indent, child))
        else:
            container[key] = _parse_scalar(value)

    return root


def load_card(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CardGateError(f"{path}: unable to read: {exc}") from exc
    try:
        data = parse_card_yaml(text)
    except CardGateError as exc:
        raise CardGateError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CardGateError(f"{path}: expected mapping")
    return data


def _as_str_list(value: Any, path: Path, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CardGateError(f"{path}: {field} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CardGateError(f"{path}: {field} items must be non-empty strings")
        result.append(item.strip())
    return result


def normalize_gate(card: dict[str, Any], path: Path) -> dict[str, Any] | None:
    """Return normalized gate config, or None when the card has no gate section."""
    if "gate" not in card:
        return None
    gate = card["gate"]
    if not isinstance(gate, dict):
        raise CardGateError(f"{path}: gate must be a mapping")

    outputs = _as_str_list(card.get("outputs"), path, "outputs")
    handoff = gate.get("handoff")
    if handoff != "required":
        raise CardGateError(f"{path}: gate.handoff must be 'required'")

    if "required_outputs" in gate:
        required_outputs = _as_str_list(gate.get("required_outputs"), path, "gate.required_outputs")
    else:
        required_outputs = list(outputs)

    unknown = [name for name in required_outputs if name not in outputs]
    if unknown:
        raise CardGateError(
            f"{path}: gate.required_outputs must be subset of outputs; unknown: {unknown}"
        )

    required_meta = _as_str_list(gate.get("required_meta"), path, "gate.required_meta")
    bad_meta = [name for name in required_meta if name not in REQUIRED_META_WHITELIST]
    if bad_meta:
        raise CardGateError(
            f"{path}: gate.required_meta not in whitelist: {bad_meta}; "
            f"allowed={sorted(REQUIRED_META_WHITELIST)}"
        )

    artifact_checks_raw = gate.get("artifact_checks") or {}
    if not isinstance(artifact_checks_raw, dict):
        raise CardGateError(f"{path}: gate.artifact_checks must be a mapping")
    artifact_checks: dict[str, dict[str, Any]] = {}
    for name, spec in artifact_checks_raw.items():
        if not isinstance(spec, dict):
            raise CardGateError(f"{path}: gate.artifact_checks.{name} must be a mapping")
        check_type = spec.get("type")
        if check_type not in ARTIFACT_CHECK_TYPES:
            raise CardGateError(
                f"{path}: gate.artifact_checks.{name}.type must be one of "
                f"{sorted(ARTIFACT_CHECK_TYPES)}"
            )
        normalized_spec: dict[str, Any] = {"type": check_type}
        if "non_empty" in spec:
            if spec["non_empty"] is not True:
                raise CardGateError(
                    f"{path}: gate.artifact_checks.{name}.non_empty must be true when set"
                )
            normalized_spec["non_empty"] = True
        artifact_checks[str(name)] = normalized_spec

    behavior_raw = gate.get("behavior_checks") or []
    if not isinstance(behavior_raw, list):
        raise CardGateError(f"{path}: gate.behavior_checks must be a list")
    behavior_checks: list[dict[str, Any]] = []
    for index, item in enumerate(behavior_raw):
        if not isinstance(item, dict):
            raise CardGateError(f"{path}: gate.behavior_checks[{index}] must be a mapping")
        check_id = item.get("id")
        if check_id not in BEHAVIOR_CHECK_IDS:
            raise CardGateError(
                f"{path}: gate.behavior_checks[{index}].id must be one of "
                f"{sorted(BEHAVIOR_CHECK_IDS)}; got {check_id!r}"
            )
        behavior_checks.append(dict(item))

    final_raw = gate.get("final")
    final: dict[str, Any] | None = None
    if final_raw is not None:
        if not isinstance(final_raw, dict):
            raise CardGateError(f"{path}: gate.final must be a mapping")
        final = {
            "require_parent_artifacts": _as_str_list(
                final_raw.get("require_parent_artifacts"),
                path,
                "gate.final.require_parent_artifacts",
            ),
            "require_report_handoff": bool(final_raw.get("require_report_handoff", False)),
        }

    return {
        "handoff": "required",
        "required_outputs": required_outputs,
        "required_meta": required_meta,
        "artifact_checks": artifact_checks,
        "behavior_checks": behavior_checks,
        "final": final,
    }


def workflow_output_closure(stages: list[dict[str, object]]) -> set[str]:
    outputs: set[str] = set()
    for stage in stages:
        for artifact in stage.get("outputs", []) or []:
            outputs.add(str(artifact))
    return outputs


def workflow_output_to_skill(stages: list[dict[str, object]]) -> dict[str, str]:
    """Map workflow output names to the producing skill id."""
    mapping: dict[str, str] = {}
    for stage in stages:
        skills = [str(item) for item in (stage.get("skills") or [])]
        outputs = [str(item) for item in (stage.get("outputs") or [])]
        if not skills or not outputs:
            continue
        if len(skills) == 1:
            for output in outputs:
                mapping[output] = skills[0]
        elif len(skills) == len(outputs):
            for skill, output in zip(skills, outputs):
                mapping[output] = skill
        else:
            raise CardGateError(
                "multi-skill stages must have zero outputs or one output per skill"
            )
    return mapping


def validate_card_gate(
    path: Path,
    card: dict[str, Any] | None = None,
    stages: list[dict[str, object]] | None = None,
) -> list[str]:
    """Validate gate contract shape. Returns error strings (empty if ok / no gate)."""
    try:
        data = card if card is not None else load_card(path)
        gate = normalize_gate(data, path)
    except CardGateError as exc:
        return [str(exc)]

    if gate is None:
        return []

    errors: list[str] = []
    final = gate.get("final")
    if isinstance(final, dict) and final.get("require_parent_artifacts"):
        if stages is None:
            errors.append(
                f"{path}: gate.final.require_parent_artifacts requires references/workflow.yaml"
            )
        else:
            closure = workflow_output_closure(stages)
            missing = [
                name
                for name in final["require_parent_artifacts"]
                if name not in closure
            ]
            if missing:
                errors.append(
                    f"{path}: gate.final.require_parent_artifacts not in workflow "
                    f"outputs closure: {missing}"
                )
    return errors


def load_skill_gate(plugin_root: Path, skill_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Load card.yaml and normalized gate for a skill. Gate may be None."""
    path = plugin_root / "skills" / skill_id / "card.yaml"
    if not path.exists():
        raise CardGateError(f"missing card.yaml for skill '{skill_id}': {path}")
    card = load_card(path)
    gate = normalize_gate(card, path)
    return card, gate
