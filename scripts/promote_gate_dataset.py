#!/usr/bin/env python3
"""Promote human-approved staging bundles into the checked-in gate dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


ALLOWED_DECISIONS = {"passed", "blocked", "needs_review"}
ALLOWED_QUALITY = {"complete", "degraded", "invalid"}
SENSITIVE_KEY_PARTS = frozenset(
    {"authorization", "token", "cookie", "password", "secret", "api_key", "apikey"}
)
PATH_FIELDS = ("handoff", "base", "patch", "evidence", "case_root", "run_dir")
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class PromotionError(Exception):
    """Unsafe or incomplete gate dataset promotion."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PromotionError(f"{path}: unable to read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PromotionError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"{path}: expected JSON object")
    return value


def _inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise PromotionError(f"path escapes root: {relative}") from exc
    return candidate


def _scan_sensitive(value: Any, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                raise PromotionError(f"sensitive field at {location}.{key}")
            _scan_sensitive(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_sensitive(item, f"{location}[{index}]")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_case(case_id: str, case: Any) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise PromotionError(f"{case_id}: case must be an object")
    required_strings = ("kind", "skill_id", "run_id", "expected_decision", "quality_state", "current_decision")
    for field in required_strings:
        if not isinstance(case.get(field), str):
            raise PromotionError(f"{case_id}: case.{field} is required")
    if case["kind"] not in {"node", "final"}:
        raise PromotionError(f"{case_id}: invalid kind")
    if case["expected_decision"] not in ALLOWED_DECISIONS or case["current_decision"] not in ALLOWED_DECISIONS:
        raise PromotionError(f"{case_id}: invalid decision")
    if case["quality_state"] not in ALLOWED_QUALITY:
        raise PromotionError(f"{case_id}: invalid quality_state")
    for field in ("expected_reasons", "current_reasons", "tags"):
        if not isinstance(case.get(field), list):
            raise PromotionError(f"{case_id}: case.{field} must be a list")
    hashes = case.get("source_hashes")
    if not isinstance(hashes, dict) or not hashes:
        raise PromotionError(f"{case_id}: case.source_hashes must be a non-empty object")
    return dict(case, case_id=case_id)


def _prefix_path(prefix: str, value: str) -> str:
    if value in {"", "."}:
        return prefix
    return f"{prefix}/{value}"


def _prepare(staging: Path, dataset: Path) -> tuple[list[tuple[str, Path]], list[dict[str, Any]]]:
    review = _read_object(staging / "review.json")
    entries = review.get("cases")
    if review.get("schema_version") != "cws-gate-review/v1" or not isinstance(entries, list):
        raise PromotionError("review.json must use cws-gate-review/v1 with cases")
    existing_payload = _read_object(dataset / "cases.json")
    existing = existing_payload.get("cases")
    if not isinstance(existing, list):
        raise PromotionError("dataset cases.json requires cases list")
    seen = {item.get("case_id") for item in existing if isinstance(item, dict)}
    bundle_sources: dict[str, Path] = {}
    promoted: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("case_id"), str):
            raise PromotionError("each review entry requires case_id")
        case_id = entry["case_id"]
        if not SAFE_ID.fullmatch(case_id):
            raise PromotionError(f"invalid case_id: {case_id}")
        if entry.get("review_status") != "approved":
            raise PromotionError(f"{case_id}: case is not approved")
        if case_id in seen:
            raise PromotionError(f"duplicate case_id: {case_id}")
        seen.add(case_id)
        bundle_value = entry.get("bundle")
        if not isinstance(bundle_value, str):
            raise PromotionError(f"{case_id}: bundle is required")
        bundle = _inside(staging, bundle_value)
        if not bundle.is_dir():
            raise PromotionError(f"{case_id}: bundle directory does not exist")
        bundle_id = entry.get("bundle_id", case_id)
        if not isinstance(bundle_id, str) or not SAFE_ID.fullmatch(bundle_id):
            raise PromotionError(f"{case_id}: invalid bundle_id")
        previous_bundle = bundle_sources.get(bundle_id)
        if previous_bundle is not None and previous_bundle != bundle:
            raise PromotionError(f"{case_id}: bundle_id points to different sources")
        bundle_sources[bundle_id] = bundle
        for path in bundle.rglob("*"):
            if path.is_dir():
                continue
            if path.suffix.lower() == ".py":
                raise PromotionError(f"{case_id}: scripts cannot be promoted: {path.name}")
            if path.suffix.lower() == ".json":
                _scan_sensitive(_read_object(path), str(path.relative_to(bundle)))
        case = _validate_case(case_id, entry.get("case"))
        hashes = case["source_hashes"]
        for relative, expected in hashes.items():
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise PromotionError(f"{case_id}: source hashes must map strings to strings")
            source = _inside(bundle, relative)
            if not source.is_file() or _sha256(source) != expected:
                raise PromotionError(f"{case_id}: source hash mismatch: {relative}")
        prefix = f"business/real/{bundle_id}"
        for field in PATH_FIELDS:
            value = case.get(field)
            if isinstance(value, str):
                _inside(bundle, value)
                case[field] = _prefix_path(prefix, value)
        raw_summaries = case.get("raw_summaries")
        if raw_summaries is not None:
            if not isinstance(raw_summaries, list):
                raise PromotionError(f"{case_id}: raw_summaries must be a list")
            for value in raw_summaries:
                if not isinstance(value, str):
                    raise PromotionError(f"{case_id}: raw_summaries entries must be strings")
                _inside(bundle, value)
            case["raw_summaries"] = [_prefix_path(prefix, value) for value in raw_summaries]
        case["source_hashes"] = {
            _prefix_path(prefix, relative): digest for relative, digest in hashes.items()
        }
        promoted.append(case)
    return sorted(bundle_sources.items()), [*existing, *promoted]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    args = parser.parse_args(argv)
    candidate_root: Path | None = None
    backup: Path | None = None
    try:
        staging = args.staging.expanduser().resolve()
        dataset = args.dataset.expanduser().resolve()
        copies, cases = _prepare(staging, dataset)
        candidate_root = Path(tempfile.mkdtemp(prefix=f".{dataset.name}-", dir=dataset.parent))
        candidate = candidate_root / dataset.name
        shutil.copytree(dataset, candidate)
        for case_id, bundle in copies:
            target = candidate / "business" / "real" / case_id
            if target.exists():
                raise PromotionError(f"target already exists: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(bundle, target)
        (candidate / "cases.json").write_text(
            json.dumps({"cases": cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        backup = dataset.parent / f".{dataset.name}-backup-{os.getpid()}"
        os.replace(dataset, backup)
        try:
            os.replace(candidate, dataset)
        except Exception:
            os.replace(backup, dataset)
            backup = None
            raise
        shutil.rmtree(backup)
        backup = None
        print(f"Promoted cases: {len(copies)}")
        return 0
    except (PromotionError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if backup is not None and backup.exists():
            if not args.dataset.exists():
                os.replace(backup, args.dataset)
            else:
                shutil.rmtree(backup, ignore_errors=True)
        if candidate_root is not None:
            shutil.rmtree(candidate_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
