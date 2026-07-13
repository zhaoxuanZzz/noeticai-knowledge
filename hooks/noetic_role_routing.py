#!/usr/bin/env python3
"""Inject the short, host-neutral Noetic role-routing rule for Codex."""
from __future__ import annotations
import json
import sys

payload = json.load(sys.stdin)
prompt = str(payload.get("prompt", "")).lower()
if "noetic" in prompt or "企业" in prompt:
    print(json.dumps({"systemMessage": "NOETIC:ROLE_ROUTING", "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "Use noetic-data-agent for missing facts/context; use noetic-gen-agent for final synthesis."}}, ensure_ascii=False))
