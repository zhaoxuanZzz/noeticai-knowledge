#!/usr/bin/env python3
"""Compatibility wrapper — prefer scripts/cws_gate_judge.py.

Kept so existing --judge-adapter paths keep working. Forwards to the built-in
adapter (live OpenAI-compatible HTTP or --mock).
"""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    target = Path(__file__).resolve().with_name("cws_gate_judge.py")
    runpy.run_path(str(target), run_name="__main__")
