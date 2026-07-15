from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

PLUGIN_NAME = "company-work-suite"
PLUGIN_DIR = Path(__file__).parent
SKILLS_DIR = PLUGIN_DIR / "skills"
ROLE_ROUTING = """When using the Company Work Suite plugin, route work through role skills:
- `cws-data-agent`: prepares and verifies prerequisite information.
- `cws-gen-agent`: synthesizes prepared information into final deliverables.

Use data first when facts or context are missing; use gen when producing the final answer."""


def _skill_names() -> set[str]:
    return {
        child.name
        for child in SKILLS_DIR.iterdir() if child.is_dir() and (child / "SKILL.md").exists()
    } if SKILLS_DIR.exists() else set()


def _frontmatter_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if not lines or lines[0] != "---":
        return ""
    prefix = f"{key}:"
    for line in lines[1:]:
        if line == "---":
            return ""
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def _command_description(skill_md: Path, command: str) -> str:
    display = _frontmatter_value(skill_md, "displayName")
    description = _frontmatter_value(skill_md, "description")
    if display and description:
        return f"{display}：{description}"
    return description or display or f"Load Company Work Suite skill {command}."


def _command_args_hint(skill_md: Path) -> str:
    return _frontmatter_value(skill_md, "argument-hint") or "[company or notes]"


def _skill_prompt(command: str, args: str = "") -> str:
    tail = args.strip()
    suffix = f"\n\nUser arguments: {tail}" if tail else ""
    return f"Load and follow the Hermes plugin skill `{PLUGIN_NAME}:{command}`.\n\n{ROLE_ROUTING}{suffix}"


def _slash_access_denied(event: Any, gateway: Any, command: str) -> bool:
    if gateway is None or event is None:
        return False
    checker = getattr(gateway, "_check_slash_access", None)
    source = getattr(event, "source", None)
    if checker is None or source is None:
        return False
    try:
        return checker(source, command) is not None
    except Exception:
        return True


def rewrite_gateway_command(event: Any = None, gateway: Any = None, **_: Any) -> dict[str, str] | None:
    text = str(getattr(event, "text", "") or "").strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text[1:].partition(" ")
    command = head.replace("_", "-").lower()
    if command not in _skill_names() or _slash_access_denied(event, gateway, command):
        return None
    return {"action": "rewrite", "text": _skill_prompt(command, rest)}


def _make_skill_command_handler(ctx: Any, command: str) -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        prompt = _skill_prompt(command, raw_args or "")
        try:
            if ctx.inject_message(prompt):
                return f"Queued `{command}` for the agent."
        except Exception:
            pass
        return prompt

    return handler


def _ensure_hermes_mcp() -> None:
    """Merge plugin MCP servers into ~/.hermes/config.yaml on load."""
    try:
        scripts_dir = PLUGIN_DIR / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        from ensure_hermes_mcp import ensure_hermes_mcp

        ensure_hermes_mcp(plugin_root=PLUGIN_DIR)
    except Exception:
        pass


def register(ctx):
    _ensure_hermes_mcp()

    for child in sorted(SKILLS_DIR.iterdir() if SKILLS_DIR.exists() else []):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)

    ctx.register_hook("pre_gateway_dispatch", rewrite_gateway_command)
    ctx.register_hook("pre_tool_call", _gate_before_kanban_complete)
    ctx.register_hook("kanban_task_claimed", _loop_after_kanban_claim)

    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(
            name="cws-gate",
            help="Retry or waive a blocked CWS Kanban gate",
            setup_fn=_setup_gate_cli,
            handler_fn=_run_gate_cli,
        )

    for command in sorted(_skill_names()):
        skill_md = SKILLS_DIR / command / "SKILL.md"
        ctx.register_command(
            command,
            _make_skill_command_handler(ctx, command),
            description=_command_description(skill_md, command),
            args_hint=_command_args_hint(skill_md),
        )


def _gate_module():
    scripts_dir = PLUGIN_DIR / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import kanban_gate
    return kanban_gate


def _gate_before_kanban_complete(tool_name: str = "", args: Any = None, task_id: str = "", **_: Any):
    if tool_name != "kanban_complete" or not task_id:
        return None
    try:
        from hermes_cli import kanban_db
        board = (args or {}).get("board") if isinstance(args, dict) else None
        conn = kanban_db.connect(board=board)
        try:
            task = kanban_db.get_task(conn, task_id)
        finally:
            conn.close()
        if task is None:
            return None
        gate = _gate_module()
        if gate.loop_context(task.body):
            result = gate.complete_loop(task_id, task.body, board=board)
            if result and result["status"] != "passed":
                action = result.get("kanban_action", result.get("next_action"))
                return {
                    "action": "block",
                    "message": f"CWS loop kept task open: {action}",
                }
            return None
        result = gate.gate_completion(task_id, task.body, board=board)
        if result and result["status"] == "blocked":
            return {"action": "block", "message": "CWS gate blocked completion: " + "; ".join(result["errors"]) + ". A human must run `hermes cws-gate retry` after repair or `hermes cws-gate waive --reason ...`."}
    except Exception as exc:
        return {"action": "block", "message": f"CWS gate failed closed: {exc}"}
    return None


def _loop_after_kanban_claim(
    task_id: str = "", board: str | None = None, **_: Any
) -> None:
    if not task_id:
        return
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None:
        return
    try:
        _gate_module().claim_loop(task_id, task.body)
    except Exception as exc:
        conn = kanban_db.connect(board=board)
        try:
            kanban_db.block_task(
                conn, task_id, reason=f"CWS loop claim failed closed: {exc}"
            )
        finally:
            conn.close()


def _setup_gate_cli(parser: Any) -> None:
    parser.add_argument("action", choices=("retry", "waive"))
    parser.add_argument("task_id")
    parser.add_argument("--board")
    parser.add_argument("--reason")


def _run_gate_cli(args: Any) -> int:
    from hermes_cli import kanban_db
    conn = kanban_db.connect(board=args.board)
    try:
        task = kanban_db.get_task(conn, args.task_id)
    finally:
        conn.close()
    if task is None:
        raise ValueError(f"task not found: {args.task_id}")
    gate = _gate_module()
    result = gate.retry(args.task_id, task.body, board=args.board) if args.action == "retry" else gate.waive(args.task_id, task.body, args.reason or "", board=args.board)
    print(json.dumps(result, ensure_ascii=False))
    return 0
