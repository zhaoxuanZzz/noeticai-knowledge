#!/usr/bin/env python3
"""Verify local Hermes install of noeticai-knowledge and its MCP servers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN_NAME = "noeticai-knowledge"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ensure_hermes_mcp import (  # noqa: E402
    AUTH_VALUE,
    hermes_home,
    load_plugin_mcp_servers,
    _load_yaml,
)


class CheckResult:
    def __init__(self) -> None:
        self.ok: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def pass_(self, message: str) -> None:
        self.ok.append(message)
        print(f"PASS  {message}")

    def fail(self, message: str) -> None:
        self.errors.append(message)
        print(f"FAIL  {message}", file=sys.stderr)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"WARN  {message}")


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _read_dotenv_value(env_path: Path, key: str) -> str | None:
    if not env_path.exists():
        return None
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


def check_hermes_cli(result: CheckResult) -> str | None:
    hermes = shutil.which("hermes")
    if not hermes:
        result.fail("hermes command not found on PATH")
        return None
    result.pass_(f"hermes CLI found: {hermes}")
    return hermes


def check_plugin_link(home: Path, result: CheckResult) -> Path | None:
    target = home / "plugins" / PLUGIN_NAME
    if not target.exists() and not target.is_symlink():
        result.fail(f"plugin path missing: {target}")
        return None
    resolved = target.resolve()
    expected = PLUGIN_ROOT.resolve()
    if resolved != expected:
        result.fail(f"plugin path {target} resolves to {resolved}, expected {expected}")
        return None
    if not (resolved / "plugin.yaml").exists():
        result.fail(f"plugin.yaml missing under {resolved}")
        return None
    result.pass_(f"plugin linked: {target} -> {resolved}")
    return resolved


def check_plugin_enabled(hermes: str, result: CheckResult) -> None:
    proc = _run([hermes, "plugins", "list", "--json", "--user"])
    if proc.returncode != 0:
        result.fail(
            "hermes plugins list --json failed: "
            + (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        )
        return
    try:
        plugins = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        result.fail(f"hermes plugins list --json returned invalid JSON: {exc}")
        return
    if not isinstance(plugins, list):
        result.fail("hermes plugins list --json did not return a list")
        return

    match: dict[str, Any] | None = None
    for item in plugins:
        if isinstance(item, dict) and item.get("name") == PLUGIN_NAME:
            match = item
            break
    if match is None:
        result.fail(f"plugin {PLUGIN_NAME} not listed by hermes plugins list")
        return

    status = str(match.get("status") or "").strip().lower()
    if status != "enabled":
        result.fail(f"plugin {PLUGIN_NAME} status is {status!r}, expected 'enabled'")
        return
    result.pass_(f"plugin enabled: {PLUGIN_NAME} ({match.get('version', '?')})")


def check_token(home: Path, result: CheckResult) -> bool:
    env_value = _read_dotenv_value(home / ".env", "QCC_MCP_TOKEN")
    process_value = os.environ.get("QCC_MCP_TOKEN")
    token = env_value if env_value else process_value
    if not token:
        result.fail(
            "QCC_MCP_TOKEN missing: set it in "
            f"{home / '.env'} or the process environment"
        )
        return False
    source = f"{home / '.env'}" if env_value else "process environment"
    result.pass_(f"QCC_MCP_TOKEN present ({source}, length={len(token)})")
    return True


def check_mcp_config(home: Path, result: CheckResult) -> list[str]:
    desired = load_plugin_mcp_servers(PLUGIN_ROOT)
    names = sorted(desired)
    config_path = home / "config.yaml"
    if not config_path.exists():
        result.fail(f"Hermes config missing: {config_path}")
        return []

    config = _load_yaml(config_path)
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        result.fail(f"{config_path}: mcp_servers must be a mapping")
        return []

    for name, desired_cfg in desired.items():
        current = servers.get(name)
        if not isinstance(current, dict):
            result.fail(f"mcp_servers.{name} missing in {config_path}")
            continue
        desired_url = desired_cfg.get("url")
        if desired_url and current.get("url") != desired_url:
            result.fail(
                f"mcp_servers.{name}.url mismatch: "
                f"{current.get('url')!r} != {desired_url!r}"
            )
            continue
        headers = current.get("headers")
        auth = headers.get("Authorization") if isinstance(headers, dict) else None
        if auth != AUTH_VALUE:
            result.fail(
                f"mcp_servers.{name}.headers.Authorization must be {AUTH_VALUE!r}, "
                f"got {auth!r}"
            )
            continue
        result.pass_(f"mcp config ok: {name}")
    return names


def check_mcp_listed(hermes: str, expected: list[str], result: CheckResult) -> None:
    proc = _run([hermes, "mcp", "list"])
    if proc.returncode != 0:
        result.fail(
            "hermes mcp list failed: "
            + (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        )
        return
    text = proc.stdout or ""
    for name in expected:
        if name not in text:
            result.fail(f"hermes mcp list does not show {name}")
            continue
        # Prefer the enabled marker when present.
        if "✓ enabled" in text or "enabled" in text.lower():
            result.pass_(f"mcp listed/enabled: {name}")
        else:
            result.warn(f"mcp listed but enabled marker unclear: {name}")


def check_mcp_live(
    hermes: str,
    names: list[str],
    *,
    quick: bool,
    result: CheckResult,
) -> None:
    targets = names[:1] if quick else names
    for name in targets:
        proc = _run([hermes, "mcp", "test", name], timeout=120)
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        connected = "Connected" in output or "✓ Connected" in output
        if proc.returncode == 0 and connected:
            tools_line = next(
                (line.strip() for line in output.splitlines() if "Tools discovered" in line),
                "",
            )
            suffix = f" ({tools_line})" if tools_line else ""
            result.pass_(f"mcp live ok: {name}{suffix}")
            continue
        detail = output.splitlines()[-1] if output else f"exit {proc.returncode}"
        result.fail(f"mcp live failed: {name}: {detail}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Hermes local install of noeticai-knowledge and MCP.",
    )
    parser.add_argument(
        "--skip-live",
        action="store_true",
        help="Skip hermes mcp test connectivity checks.",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Only live-test the first MCP server (qcc-company).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    result = CheckResult()
    home = hermes_home()
    print(f"HERMES_HOME={home}")
    print(f"PLUGIN_ROOT={PLUGIN_ROOT}")

    hermes = check_hermes_cli(result)
    check_plugin_link(home, result)
    if hermes:
        check_plugin_enabled(hermes, result)

    token_ok = check_token(home, result)
    names = check_mcp_config(home, result)
    if hermes and names:
        check_mcp_listed(hermes, names, result)
        if args.skip_live:
            result.warn("skipped live MCP connectivity tests (--skip-live)")
        elif not token_ok:
            result.warn("skipped live MCP tests because QCC_MCP_TOKEN is missing")
        else:
            check_mcp_live(hermes, names, quick=args.quick, result=result)

    print()
    print(
        f"Summary: {len(result.ok)} passed, "
        f"{len(result.errors)} failed, {len(result.warnings)} warnings"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
