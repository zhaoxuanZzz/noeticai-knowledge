#!/usr/bin/env python3
"""Render or regenerate a human gate review pack for needs_review artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gate_review import (
    build_context_from_handoff_dir,
    compute_runtime_gate_input_hash,
    render_review_markdown,
    write_review_pack,
    write_review_pack_from_handoff_dir,
)


def _render_from_context_json(path: Path, *, write: bool, stdout: bool) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    ctx = {
        "subject_name": payload.get("subject_name") or "（未知主体）",
        "skill_id": payload.get("skill_id"),
        "decision": "needs_review",
        "actual_decision": payload.get("actual_decision") or "needs_review",
        "expected_decision": payload.get("expected_decision"),
        "run_id": payload.get("run_id"),
        "case_id": payload.get("case_id"),
        "judge": payload.get("judge") or {},
        "handoff": payload.get("handoff") or {},
        "evidence": payload.get("evidence") or {},
    }
    gate_hash = payload.get("gate_input_hash")
    if not isinstance(gate_hash, str):
        gate_hash = compute_runtime_gate_input_hash(
            ctx["handoff"], ctx["evidence"], ctx["judge"]
        )
    md = render_review_markdown(ctx)
    if stdout:
        print(md)
    if write:
        write_review_pack(path.parent, ctx, gate_input_hash=gate_hash)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--handoff-dir", type=Path, help="Directory with handoff/evidence/gate-result")
    group.add_argument(
        "--eval-dir",
        type=Path,
        help="Eval output dir; re-renders reviews/*/context.json packs",
    )
    parser.add_argument("--case-id", help="Only render one case under --eval-dir")
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print Markdown to stdout (still writes files unless --no-write)",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Do not write review.md / review-decision.json",
    )
    args = parser.parse_args(argv)
    write = not args.no_write

    if args.handoff_dir is not None:
        directory = args.handoff_dir.resolve()
        ctx = build_context_from_handoff_dir(directory)
        if ctx is None:
            result_path = directory / "gate-result.json"
            decision = "missing"
            if result_path.is_file():
                try:
                    decision = json.loads(result_path.read_text(encoding="utf-8")).get(
                        "decision", "unknown"
                    )
                except (OSError, json.JSONDecodeError):
                    decision = "invalid"
            print(f"skip: decision={decision}", file=sys.stderr)
            return 0
        md = render_review_markdown(ctx)
        if args.stdout:
            print(md)
        if write:
            write_review_pack_from_handoff_dir(directory)
        return 0

    eval_dir = args.eval_dir.resolve()
    reviews = eval_dir / "reviews"
    if not reviews.is_dir():
        print(f"error: missing reviews directory: {reviews}", file=sys.stderr)
        return 2
    cases = sorted(reviews.iterdir())
    if args.case_id:
        cases = [reviews / args.case_id]
    rendered = 0
    for case_dir in cases:
        if not case_dir.is_dir():
            continue
        context_path = case_dir / "context.json"
        if not context_path.is_file():
            continue
        _render_from_context_json(context_path, write=write, stdout=args.stdout)
        rendered += 1
    if rendered == 0:
        print("error: no context.json found under reviews/", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
