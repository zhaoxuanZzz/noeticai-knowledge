import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def _plugin_mcp_config(plugin_dir: Path) -> dict:
    """Convert plugin.yaml mcp_servers into the format register_mcp_servers expects."""
    data = yaml.safe_load((plugin_dir / "plugin.yaml").read_text()) or {}
    cfg = {}
    for name, srv in data.get("mcp_servers", {}).items():
        entry = {
            "url": srv["url"],
            "timeout": 120,
            "connect_timeout": 30,
        }
        auth = srv.get("auth", {})
        token = re.sub(
            r"\$\{(\w+)\}|\$(\w+)",
            lambda m: os.environ.get(m.group(1) or m.group(2), ""),
            auth.get("token", ""),
        )
        if token:
            entry["headers"] = {"Authorization": f"Bearer {token}"}
        cfg[name] = entry
    return cfg


def register(ctx):
    from tools.mcp_tool import register_mcp_servers

    plugin_dir = Path(__file__).parent

    # skills
    skills_dir = plugin_dir / "skills"
    for child in sorted(skills_dir.iterdir() if skills_dir.exists() else []):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)

    # mcp — idempotent, Hermes handles dedup if config.yaml has them too
    servers = _plugin_mcp_config(plugin_dir)
    if servers:
        names = register_mcp_servers(servers)
        logger.info(
            "Registered %d MCP server(s): %s (%d tools discovered)",
            len(servers), ", ".join(servers), len(names),
        )
