#!/usr/bin/env python3
"""Create a sanitized, review-only gate dataset staging snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ALLOWED_FIELDS = frozenset(
    {
        "name",
        "unified_social_credit_code",
        "status",
        "legal_representative",
        "registered_capital",
        "established_at",
        "industry",
        "business_scope",
        "risk",
        "observed_at",
        "source_id",
        "facts",
        "result",
    }
)
SENSITIVE_KEY_PARTS = frozenset(
    {"authorization", "token", "cookie", "password", "secret", "api_key"}
)


class RefreshError(Exception):
    """Unsafe or malformed refresh input."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RefreshError(f"{path}: unable to read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RefreshError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RefreshError(f"{path}: expected JSON object")
    return payload


def _scan_sensitive(value: Any, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
                raise RefreshError(f"sensitive field at {location}.{key}")
            _scan_sensitive(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_sensitive(item, f"{location}[{index}]")


def _minimize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _minimize(item)
            for key, item in value.items()
            if key in ALLOWED_FIELDS
        }
    if isinstance(value, list):
        return [_minimize(item) for item in value]
    return value


def _hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = args.manifest.resolve()
        output = args.output.resolve()
        fixture_root = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "gate-dataset").resolve()
        try:
            output.relative_to(fixture_root)
        except ValueError:
            pass
        else:
            raise RefreshError("--output must be staging outside the checked-in baseline")
        payload = _read_object(manifest)
        companies = payload.get("companies")
        if not isinstance(companies, list):
            raise RefreshError("manifest.companies must be a list")
        if output.exists() and any(output.iterdir()):
            raise RefreshError(f"output must be empty: {output}")
        baseline_value = payload.get("baseline_dataset")
        if baseline_value is not None:
            if not isinstance(baseline_value, str):
                raise RefreshError("manifest.baseline_dataset must be a string")
            baseline = (manifest.parent / baseline_value).resolve()
            if not (baseline / "cases.json").is_file():
                raise RefreshError(f"baseline dataset has no cases.json: {baseline}")
            shutil.copytree(baseline, output, dirs_exist_ok=True)
        raw_dir = output / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        refreshed_hashes: dict[str, str] = {}
        for company in companies:
            if not isinstance(company, dict) or not isinstance(company.get("case_id"), str):
                raise RefreshError("each company requires case_id")
            source = company.get("snapshot_file")
            if not isinstance(source, str):
                entries.append({"case_id": company["case_id"], "status": "awaiting_capture"})
                continue
            source_path = (manifest.parent / source).resolve()
            allowed_source_root = (
                (manifest.parent / baseline_value).resolve()
                if isinstance(baseline_value, str)
                else manifest.parent
            )
            try:
                source_path.relative_to(allowed_source_root)
            except ValueError as exc:
                raise RefreshError(f"snapshot_file escapes allowed source root: {source}") from exc
            raw = _read_object(source_path)
            _scan_sensitive(raw)
            minimized = _minimize(raw)
            target_value = company.get("target_file")
            target = (
                (output / target_value).resolve()
                if isinstance(target_value, str)
                else raw_dir / f"{company['case_id']}.json"
            )
            try:
                target.relative_to(output)
            except ValueError as exc:
                raise RefreshError(f"target_file escapes staging: {target_value}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(minimized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            refreshed_hashes[str(target.relative_to(output))] = _hash(target)
            entries.append(
                {
                    "case_id": company["case_id"],
                    "status": "candidate",
                    "source_hash": _hash(source_path),
                    "snapshot_hash": _hash(target),
                    "requires_human_review": True,
                }
            )
        cases_path = output / "cases.json"
        if refreshed_hashes and cases_path.is_file():
            cases_payload = _read_object(cases_path)
            for case in cases_payload.get("cases", []):
                hashes = case.get("source_hashes") if isinstance(case, dict) else None
                if isinstance(hashes, dict):
                    for relative, digest in refreshed_hashes.items():
                        if relative in hashes:
                            hashes[relative] = digest
            cases_path.write_text(
                json.dumps(cases_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        report = {"refresh_version": "gate-refresh/v1", "entries": entries}
        (output / "refresh-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Staging snapshot: {output}")
        print(f"Candidates: {sum(item['status'] == 'candidate' for item in entries)}")
        return 0
    except RefreshError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
