#!/usr/bin/env python3
"""Validate the NoeticAI plugin static contract."""

from __future__ import annotations

import json
import sys
import tempfile
import ast
from pathlib import Path


class WorkSuiteError(Exception):
    pass


def parse_inline_list(value: str, path: Path, line_no: int) -> list[str]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise WorkSuiteError(f"{path}:{line_no}: unsupported shape, use inline list: [a, b]")
    inner = value[1:-1].strip()
    if not inner:
        return []
    return [item.strip().strip("'\"") for item in inner.split(",")]


def parse_workflow(path: Path) -> list[dict[str, object]]:
    stages: list[dict[str, object]] = []
    in_stages = False

    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()

        if line == "stages:" and indent == 0:
            in_stages = True
            continue

        if not in_stages:
            continue

        if indent == 0:
            in_stages = False
            continue

        if indent == 2 and line.startswith("- "):
            body = line[2:].strip()
            if not body.startswith("id:"):
                raise WorkSuiteError(f"{path}:{line_no}: unsupported stage item, expected '- id: ...'")
            stage_id = body.split(":", 1)[1].strip().strip("'\"")
            if not stage_id:
                raise WorkSuiteError(f"{path}:{line_no}: stage id is required")
            stages.append({"id": stage_id})
            continue

        if indent == 4 and ":" in line and stages:
            key, value = [part.strip() for part in line.split(":", 1)]
            if key in {"skills", "inputs", "outputs", "quality_gates"}:
                stages[-1][key] = parse_inline_list(value, path, line_no)
            elif key == "parallel":
                if value not in {"true", "false"}:
                    raise WorkSuiteError(f"{path}:{line_no}: parallel must be true or false")
                stages[-1][key] = value == "true"
            elif key == "id":
                stages[-1][key] = value.strip("'\"")
            else:
                raise WorkSuiteError(f"{path}:{line_no}: unsupported workflow field: {key}")
            continue

        raise WorkSuiteError(f"{path}:{line_no}: unsupported shape")

    if not stages:
        raise WorkSuiteError(f"{path}: stages is required")
    return stages


def yaml_names(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    return {path.stem for path in directory.glob("*.yaml")}


def read_json(path: Path, errors: list[str]) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{path}: invalid JSON: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{path}: expected object")
        return None
    return data


def parse_simple_yaml(path: Path, errors: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            errors.append(f"{path}:{line_no}: unsupported YAML shape")
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def skill_names(root: Path, errors: list[str]) -> set[str]:
    skills_dir = root / "skills"
    names: set[str] = set()
    if not skills_dir.exists():
        errors.append(f"missing {skills_dir}")
        return names

    for skill_dir in sorted(path for path in skills_dir.iterdir() if path.is_dir()):
        names.add(skill_dir.name)
        if not (skill_dir / "SKILL.md").exists():
            errors.append(f"missing {skill_dir / 'SKILL.md'}")
    return names


def validate_codex(root: Path) -> list[str]:
    errors: list[str] = []
    plugin_json = root / ".codex-plugin" / "plugin.json"

    if not plugin_json.exists():
        errors.append(f"missing {plugin_json}")
    else:
        plugin = read_json(plugin_json, errors)
        if plugin:
            if plugin.get("name") != root.name:
                errors.append(f"{plugin_json}: name must equal plugin root directory '{root.name}'")
            if plugin.get("skills") != "./skills/":
                errors.append(f"{plugin_json}: skills must be './skills/'")

    skill_names(root, errors)
    return errors


def validate_claude(root: Path) -> list[str]:
    errors: list[str] = []
    plugin_json = root / ".claude-plugin" / "plugin.json"

    if not plugin_json.exists():
        errors.append(f"missing {plugin_json}")
    else:
        plugin = read_json(plugin_json, errors)
        if plugin and plugin.get("name") != root.name:
            errors.append(f"{plugin_json}: name must equal plugin root directory '{root.name}'")

    skill_names(root, errors)
    return errors


def validate_hermes(root: Path) -> list[str]:
    errors: list[str] = []
    plugin_yaml = root / "plugin.yaml"
    init_py = root / "__init__.py"

    if not plugin_yaml.exists():
        errors.append(f"missing {plugin_yaml}")
    else:
        plugin = parse_simple_yaml(plugin_yaml, errors)
        if plugin.get("name") != root.name:
            errors.append(f"{plugin_yaml}: name must equal plugin root directory '{root.name}'")
        for key in ("version", "description"):
            if not plugin.get(key):
                errors.append(f"{plugin_yaml}: {key} is required")

    if not init_py.exists():
        errors.append(f"missing {init_py}")
    else:
        try:
            tree = ast.parse(init_py.read_text(encoding="utf-8"), filename=str(init_py))
        except SyntaxError as exc:
            errors.append(f"{init_py}: invalid Python: {exc}")
        else:
            register = next(
                (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "register"),
                None,
            )
            if register is None:
                errors.append(f"{init_py}: missing register(ctx)")
            elif not register.args.args:
                errors.append(f"{init_py}: register must accept ctx")
            elif not any(
                isinstance(node, ast.Attribute) and node.attr == "register_skill"
                for node in ast.walk(register)
            ):
                errors.append(f"{init_py}: register(ctx) must call ctx.register_skill(...)")

    skill_names(root, errors)
    return errors


def validate_work_suite(root: Path) -> list[str]:
    errors: list[str] = []
    skills_dir = root / "skills"
    artifact_names = yaml_names(root / "artifact-contracts")
    gate_names = yaml_names(root / "quality-gates")
    names = skill_names(root, errors)

    for workflow in sorted(skills_dir.glob("*/references/workflow.yaml")):
        try:
            stages = parse_workflow(workflow)
        except WorkSuiteError as exc:
            errors.append(str(exc))
            continue

        available_outputs: set[str] = set()
        for index, stage in enumerate(stages, 1):
            stage_id = stage["id"]
            for skill in stage.get("skills", []):
                if skill not in names:
                    errors.append(f"{workflow}: stage {stage_id}: unknown skill '{skill}'")

            for artifact in stage.get("inputs", []):
                if artifact not in available_outputs:
                    errors.append(f"{workflow}: stage {stage_id}: input '{artifact}' is not produced by a previous stage")

            for artifact in stage.get("outputs", []):
                if artifact not in artifact_names:
                    errors.append(f"{workflow}: stage {stage_id}: missing artifact-contracts/{artifact}.yaml")
                available_outputs.add(artifact)

            for gate in stage.get("quality_gates", []):
                if gate not in gate_names:
                    errors.append(f"{workflow}: stage {stage_id}: missing quality-gates/{gate}.yaml")

            if "skills" not in stage:
                errors.append(f"{workflow}: stage {index} ({stage_id}): skills is required")

    return errors


VALIDATORS = {
    "work-suite": validate_work_suite,
    "codex": validate_codex,
    "claude": validate_claude,
    "hermes": validate_hermes,
}


def validate(root: Path, target: str = "all") -> list[str]:
    names = VALIDATORS if target == "all" else {target: VALIDATORS[target]}
    errors: list[str] = []
    for name, validator in names.items():
        errors.extend(f"{name}: {error}" for error in validator(root))
    return errors


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_suite(root: Path) -> None:
    write(root / ".codex-plugin" / "plugin.json", json.dumps({"name": root.name, "skills": "./skills/"}))
    write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": root.name}))
    write(root / "plugin.yaml", f"name: {root.name}\nversion: 0.1.0\ndescription: Test plugin\n")
    write(root / "__init__.py", "def register(ctx):\n    ctx.register_skill('research', 'skills/research/SKILL.md')\n")
    write(root / "skills" / "research" / "SKILL.md")
    write(root / "artifact-contracts" / "context.yaml")
    write(root / "quality-gates" / "coverage.yaml")
    write(
        root / "skills" / "research" / "references" / "workflow.yaml",
        """name: OK
stages:
  - id: research
    skills: [research]
    outputs: [context]
    quality_gates: [coverage]
""",
    )


def run_self_test() -> int:
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)

        ok = base / "ok-suite"
        make_suite(ok)
        assert validate(ok) == []

        missing_skill = base / "missing-skill"
        make_suite(missing_skill)
        write(
            missing_skill / "skills" / "research" / "references" / "workflow.yaml",
            """name: Missing Skill
stages:
  - id: research
    skills: [ghost]
    outputs: [context]
""",
        )
        assert any("unknown skill 'ghost'" in error for error in validate(missing_skill))

        bad_input = base / "bad-input"
        make_suite(bad_input)
        write(
            bad_input / "skills" / "research" / "references" / "workflow.yaml",
            """name: Bad Input
stages:
  - id: research
    skills: [research]
    inputs: [context]
    outputs: [context]
""",
        )
        assert any("is not produced by a previous stage" in error for error in validate(bad_input))

        missing_contracts = base / "missing-contracts"
        make_suite(missing_contracts)
        (missing_contracts / "artifact-contracts" / "context.yaml").unlink()
        (missing_contracts / "quality-gates" / "coverage.yaml").unlink()
        errors = validate(missing_contracts)
        assert any("missing artifact-contracts/context.yaml" in error for error in errors)
        assert any("missing quality-gates/coverage.yaml" in error for error in errors)

    print("self-test ok")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return run_self_test()
    target = "all"
    if len(argv) == 3 and argv[0] == "--target":
        target = argv[1]
        argv = argv[2:]
    if target not in {"all", *VALIDATORS} or len(argv) != 1:
        print("usage: python3 scripts/validate_work_suite.py [--target all|work-suite|codex|claude|hermes] <plugin-root>", file=sys.stderr)
        print("       python3 scripts/validate_work_suite.py --self-test", file=sys.stderr)
        return 2

    root = Path(argv[0]).resolve()
    errors = validate(root, target)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"OK: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
