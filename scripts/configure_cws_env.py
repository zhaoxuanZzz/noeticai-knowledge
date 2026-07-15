#!/usr/bin/env python3
"""Interactively configure CWS secrets for Hermes .env and persistent shell env.

Writes:
  - $HERMES_HOME/.env          (Hermes / MCP)
  - ~/.cws/env.sh              (shell exports)
  - ensures `source ~/.cws/env.sh` in ~/.zshrc or ~/.bashrc

Existing values prompt for overwrite; default is keep (N).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
CWS_DIR = Path.home() / ".cws"
SHELL_ENV = CWS_DIR / "env.sh"

SOURCE_LINE = 'source "$HOME/.cws/env.sh"  # company-work-suite'
BLOCK_BEGIN = "# >>> company-work-suite env >>>"
BLOCK_END = "# <<< company-work-suite env <<<"

# (name, secret?, required?, default, help)
ENV_SPECS: list[tuple[str, bool, bool, str, str]] = [
    (
        "QCC_MCP_TOKEN",
        True,
        True,
        "",
        "企查查 MCP Bearer token（Hermes MCP 必需）",
    ),
    (
        "CWS_JUDGE_API_KEY",
        True,
        False,
        "",
        "Judge 模型 API key（OpenAI 兼容 / Qwen）；亦可用 OPENAI_API_KEY",
    ),
    (
        "CWS_JUDGE_BASE_URL",
        False,
        False,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "Judge OpenAI 兼容 base URL",
    ),
    (
        "CWS_JUDGE_MODEL",
        False,
        False,
        "qwen3.7-max",
        "Judge 模型 ID",
    ),
    (
        "CWS_JUDGE_MODE",
        False,
        False,
        "live",
        "Judge 模式：live 或 mock",
    ),
]


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def _read_dotenv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[name] = value
    return result


def _escape_dotenv(value: str) -> str:
    if re.search(r'[\s#"\'\\]', value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _escape_shell(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _upsert_dotenv(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.partition("=")[0].strip()
            if name in updates:
                out.append(f"{name}={_escape_dotenv(updates[name])}")
                seen.add(name)
                continue
        out.append(line)
    for name, value in updates.items():
        if name not in seen:
            out.append(f"{name}={_escape_dotenv(value)}")
    text = "\n".join(out).rstrip() + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_shell_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "#!/usr/bin/env bash",
        "# Managed by company-work-suite `make install`. Prefer editing via make install.",
        BLOCK_BEGIN,
    ]
    for name, _, _, _, _ in ENV_SPECS:
        if name in values and values[name]:
            lines.append(f"export {name}={_escape_shell(values[name])}")
    lines.append(BLOCK_END)
    lines.append("")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)
    path.chmod(0o600)


def _shell_rc_path() -> Path:
    shell = Path(os.environ.get("SHELL", "")).name
    home = Path.home()
    if shell == "bash":
        bash_profile = home / ".bash_profile"
        if bash_profile.is_file() or sys.platform == "darwin":
            return bash_profile
        return home / ".bashrc"
    return home / ".zshrc"


def _ensure_shell_source(rc_path: Path) -> None:
    marker = "company-work-suite"
    existing = rc_path.read_text(encoding="utf-8") if rc_path.is_file() else ""
    if marker in existing and ".cws/env.sh" in existing:
        return
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    block = (
        f"\n# company-work-suite env\n"
        f"if [ -f \"$HOME/.cws/env.sh\" ]; then\n"
        f"  {SOURCE_LINE}\n"
        f"fi\n"
    )
    with rc_path.open("a", encoding="utf-8") as handle:
        handle.write(block)


def _prompt_yes_no(message: str, *, default: bool = False) -> bool:
    suffix = " [y/N]: " if not default else " [Y/n]: "
    try:
        answer = input(message + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in {"y", "yes"}


def _prompt_value(name: str, *, secret: bool, default: str) -> str | None:
    hint = f" [{default}]" if default and not secret else ""
    try:
        raw = input(f"  {name}{hint}: ").strip()
    except EOFError:
        return None
    if not raw:
        return default if default else None
    return raw


def _existing_value(name: str, hermes_env: dict[str, str], shell_env: dict[str, str]) -> str:
    return (
        hermes_env.get(name)
        or shell_env.get(name)
        or os.environ.get(name, "")
        or ""
    )


def configure(*, non_interactive: bool = False) -> dict[str, str]:
    hermes_env_path = HERMES_HOME / ".env"
    hermes_env = _read_dotenv(hermes_env_path)
    shell_env: dict[str, str] = {}
    if SHELL_ENV.is_file():
        for raw in SHELL_ENV.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("export ") or "=" not in line:
                continue
            assign = line[len("export ") :]
            name, _, value = assign.partition("=")
            name = name.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            shell_env[name] = value

    selected: dict[str, str] = {}

    print("Configure company-work-suite environment")
    print(f"  Hermes .env : {hermes_env_path}")
    print(f"  Shell env   : {SHELL_ENV}")
    print("  Existing values default to keep (overwrite prompt: N).\n")

    if non_interactive or not sys.stdin.isatty():
        print("Non-interactive / non-TTY: keeping existing values, no prompts.")
        for name, _secret, required, default, _help in ENV_SPECS:
            current = _existing_value(name, hermes_env, shell_env)
            if current:
                selected[name] = current
            elif default:
                selected[name] = default
            elif required:
                print(f"WARNING: required {name} is missing", file=sys.stderr)
        return selected

    for name, secret, required, default, help_text in ENV_SPECS:
        current = _existing_value(name, hermes_env, shell_env)
        print(f"{name} — {help_text}")
        if current:
            print(f"  current: {_mask(current) if secret else current}")
            if not _prompt_yes_no("  overwrite existing value?", default=False):
                selected[name] = current
                print(f"  kept {name}\n")
                continue
        prompt_default = "" if (current and secret) else (current or default)
        value = _prompt_value(name, secret=secret, default=prompt_default)
        if value is None or value == "":
            if current:
                selected[name] = current
                print(f"  kept {name}\n")
            elif default and not required:
                selected[name] = default
                print(f"  using default {name}={default}\n")
            elif required:
                print(f"  WARNING: {name} left empty (required for MCP)\n", file=sys.stderr)
            else:
                print(f"  skipped {name}\n")
            continue
        selected[name] = value
        print(f"  set {name}\n")

    return selected


def apply(values: dict[str, str]) -> None:
    if not values:
        print("No values to write.")
        return
    hermes_env_path = HERMES_HOME / ".env"
    _upsert_dotenv(hermes_env_path, values)
    _write_shell_env(SHELL_ENV, values)
    rc_path = _shell_rc_path()
    _ensure_shell_source(rc_path)
    # Apply to current process for follow-up deploy/verify in same make recipe.
    for name, value in values.items():
        os.environ[name] = value
    print(f"Wrote {hermes_env_path}")
    print(f"Wrote {SHELL_ENV}")
    print(f"Ensured source line in {rc_path}")
    print("Open a new shell (or `source ~/.cws/env.sh`) for exports in other terminals.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Keep existing / defaults; do not prompt.",
    )
    parser.add_argument(
        "--skip-write",
        action="store_true",
        help="Resolve values but do not write files (dry preview).",
    )
    args = parser.parse_args(argv)
    values = configure(non_interactive=args.non_interactive)
    if args.skip_write:
        for name, value in values.items():
            secret = any(spec[0] == name and spec[1] for spec in ENV_SPECS)
            print(f"{name}={_mask(value) if secret else value}")
        return 0
    apply(values)
    if not values.get("QCC_MCP_TOKEN"):
        print(
            "WARNING: QCC_MCP_TOKEN is empty; Hermes MCP live checks will fail.",
            file=sys.stderr,
        )
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
