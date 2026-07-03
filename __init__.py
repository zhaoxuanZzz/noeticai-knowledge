from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

PLUGIN_NAME = "noeticai-knowledge"
PLUGIN_DIR = Path(__file__).parent
SKILLS_DIR = PLUGIN_DIR / "skills"


def _skill_names() -> set[str]:
    return {
        child.name
        for child in SKILLS_DIR.iterdir() if child.is_dir() and (child / "SKILL.md").exists()
    } if SKILLS_DIR.exists() else set()


def _skill_prompt(command: str, args: str = "") -> str:
    tail = args.strip()
    suffix = f"\n\nUser arguments: {tail}" if tail else ""
    return f"Load and follow the Hermes plugin skill `{PLUGIN_NAME}:{command}`.{suffix}"


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


def register(ctx):
    for child in sorted(SKILLS_DIR.iterdir() if SKILLS_DIR.exists() else []):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)

    ctx.register_hook("pre_gateway_dispatch", rewrite_gateway_command)

    for command in sorted(_skill_names()):
        ctx.register_command(
            command,
            _make_skill_command_handler(ctx, command),
            description=f"Load NoeticAI skill {command}.",
            args_hint="[company or notes]",
        )
