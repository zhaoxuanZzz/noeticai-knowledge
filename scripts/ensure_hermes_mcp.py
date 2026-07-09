#!/usr/bin/env python3
"""Idempotently merge this plugin's MCP servers into ~/.hermes/config.yaml."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

AUTH_VALUE = "Bearer ${QCC_MCP_TOKEN}"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger("noeticai.ensure_hermes_mcp")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_scalar(value: str) -> Any:
    value = _strip_quotes(value.strip())
    if value == "":
        return ""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return _parse_simple_mapping(text)


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if text == "" or any(ch in text for ch in ":#{}[]&*!|>%@`'\"\n") or text.strip() != text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _dump_simple_mapping(data: dict[str, Any], indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            if value:
                lines.append(_dump_simple_mapping(value, indent + 2).rstrip("\n"))
            else:
                lines.append(f"{prefix}  {{}}")
        elif isinstance(value, list):
            lines.append(f"{prefix}{key}:")
            if not value:
                lines.append(f"{prefix}  []")
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"{prefix}-")
                    nested = _dump_simple_mapping(item, indent + 2).splitlines()
                    for nested_line in nested:
                        lines.append(f"  {nested_line}" if nested_line else nested_line)
                else:
                    lines.append(f"{prefix}- {_format_scalar(item)}")
        else:
            lines.append(f"{prefix}{key}: {_format_scalar(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def _parse_simple_mapping(text: str) -> dict[str, Any]:
    """Minimal indentation YAML mapping parser for plugin.yaml / config snippets."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("- "):
            # Skip list items (requires_env / provides_*); not needed for MCP merge.
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value)
    return root


def load_plugin_mcp_servers(plugin_root: Path | None = None) -> dict[str, dict[str, Any]]:
    root = plugin_root or PLUGIN_ROOT
    plugin_yaml = root / "plugin.yaml"
    data = _load_yaml(plugin_yaml)
    servers = data.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        raise ValueError(f"{plugin_yaml}: mcp_servers must be a mapping")
    result: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"{plugin_yaml}: mcp_servers.{name} must be a mapping")
        result[str(name)] = dict(cfg)
    return result


def _desired_auth(cfg: dict[str, Any]) -> str:
    headers = cfg.get("headers")
    if isinstance(headers, dict):
        auth = headers.get("Authorization")
        if isinstance(auth, str) and auth.strip():
            return auth.strip()
    return AUTH_VALUE


def merge_mcp_servers(
    existing: dict[str, Any] | None,
    desired: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Merge plugin MCP servers into an existing mcp_servers mapping.

    Returns (merged, changed).
    """
    merged: dict[str, Any] = dict(existing or {})
    changed = False

    for name, desired_cfg in desired.items():
        auth = _desired_auth(desired_cfg)
        current = merged.get(name)
        if not isinstance(current, dict):
            entry = dict(desired_cfg)
            headers = dict(entry.get("headers") or {})
            headers["Authorization"] = auth
            entry["headers"] = headers
            merged[name] = entry
            changed = True
            continue

        updated = dict(current)
        entry_changed = False
        headers = dict(updated.get("headers") or {})
        if headers.get("Authorization") != auth:
            headers["Authorization"] = auth
            updated["headers"] = headers
            entry_changed = True

        # Fill missing url / timeouts from desired without clobbering extras.
        for key in ("url", "timeout", "connect_timeout"):
            if key in desired_cfg and key not in updated:
                updated[key] = desired_cfg[key]
                entry_changed = True

        if entry_changed:
            merged[name] = updated
            changed = True

    return merged, changed


def _mcp_servers_line_span(lines: list[str]) -> tuple[int, int] | None:
    """Return [start, end) line indexes for the top-level mcp_servers block."""
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("mcp_servers:") and (len(line) == 12 or line[12] in " \t#\r\n"):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        # Next top-level key or top-level comment ends this block.
        if line[0] not in " \t":
            end = j
            break
    return start, end


def _load_existing_mcp_servers(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    span = _mcp_servers_line_span(lines)
    if span is None:
        return {}
    start, end = span
    block = "\n".join(lines[start:end]) + "\n"
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(block) or {}
        if isinstance(loaded, dict):
            data = loaded
        else:
            data = _parse_simple_mapping(block)
    except Exception:
        data = _parse_simple_mapping(block)
    servers = data.get("mcp_servers") if isinstance(data, dict) else None
    return dict(servers) if isinstance(servers, dict) else {}


def _write_mcp_servers(config_path: Path, servers: dict[str, Any]) -> None:
    """Replace only the top-level mcp_servers block, preserving the rest of the file.

    The stdlib fallback YAML parser skips list items, so rewriting the whole
    config.yaml would drop plugins.enabled entries. Surgical updates avoid that.
    """
    block = _dump_simple_mapping({"mcp_servers": servers})
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(block, encoding="utf-8")
        return

    text = config_path.read_text(encoding="utf-8")
    newline = "\n" if text.endswith("\n") or text == "" else ""
    lines = text.splitlines()
    span = _mcp_servers_line_span(lines)
    block_lines = block.rstrip("\n").splitlines()
    if span is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block_lines)
    else:
        start, end = span
        lines[start:end] = block_lines
    config_path.write_text("\n".join(lines) + (newline or "\n"), encoding="utf-8")


def ensure_hermes_mcp(
    plugin_root: Path | None = None,
    home: Path | None = None,
) -> bool:
    """Ensure plugin MCP servers are present in Hermes config.yaml.

    Returns True when the config file was modified.
    """
    desired = load_plugin_mcp_servers(plugin_root)
    if not desired:
        return False

    # Prefer Hermes APIs only for the default user home. Explicit home or
    # HERMES_HOME overrides always write that path (tests / isolated deploys).
    if home is None and "HERMES_HOME" not in os.environ:
        try:
            from hermes_cli.config import load_config, save_config  # type: ignore

            config = load_config()
            existing = config.get("mcp_servers")
            if existing is not None and not isinstance(existing, dict):
                existing = {}
            merged, changed = merge_mcp_servers(
                existing if isinstance(existing, dict) else {},
                desired,
            )
            if not changed:
                return False
            config["mcp_servers"] = merged
            save_config(config)
            return True
        except Exception as exc:
            LOGGER.debug("hermes_cli config API unavailable, falling back: %s", exc)

    target_home = home or hermes_home()
    config_path = target_home / "config.yaml"
    existing = _load_existing_mcp_servers(config_path)
    merged, changed = merge_mcp_servers(existing, desired)
    if not changed:
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_mcp_servers(config_path, merged)
    return True


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        changed = ensure_hermes_mcp()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    home = hermes_home()
    if changed:
        print(f"Updated MCP servers in {home / 'config.yaml'}")
    else:
        print(f"MCP servers already up to date in {home / 'config.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
