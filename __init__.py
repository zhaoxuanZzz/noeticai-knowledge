import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def register(ctx):
    plugin_dir = Path(__file__).parent

    # skills
    skills_dir = plugin_dir / "skills"
    for child in sorted(skills_dir.iterdir() if skills_dir.exists() else []):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
