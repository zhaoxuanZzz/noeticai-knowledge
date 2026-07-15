#!/usr/bin/env python3
"""Import selected company knowledge into sanitized, review-only staging."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_KEY_PARTS = frozenset(
    {"authorization", "token", "cookie", "password", "secret", "api_key", "apikey"}
)
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class ImportError(Exception):
    """Unsafe or malformed knowledge-base import."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ImportError(f"{path}: unable to read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ImportError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ImportError(f"{path}: expected JSON object")
    return value


def _inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ImportError(f"path escapes knowledge base: {relative}") from exc
    return candidate


def _scan_sensitive(value: Any, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                raise ImportError(f"sensitive field at {location}.{key}")
            _scan_sensitive(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_sensitive(item, f"{location}[{index}]")


def _pointer(value: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return value
    if not pointer.startswith("/"):
        raise ImportError(f"JSON pointer must start with '/': {pointer}")
    current = value
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ImportError(f"JSON pointer does not exist: {pointer}")
    return current


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _validate_subject(value: Any, case_id: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ImportError(f"{case_id}: subject must be an object")
    name = value.get("name")
    code = value.get("unified_social_credit_code")
    if not isinstance(name, str) or not name.strip():
        raise ImportError(f"{case_id}: subject.name is required")
    if not isinstance(code, str) or not code.strip():
        raise ImportError(f"{case_id}: subject.unified_social_credit_code is required")
    return {"name": name.strip(), "unified_social_credit_code": code.strip()}


def _prepare(kb_root: Path, manifest: dict[str, Any]) -> tuple[list[tuple[Path, bytes]], dict[str, Any], dict[str, Any]]:
    companies = manifest.get("companies")
    if not isinstance(companies, list) or not companies:
        raise ImportError("manifest.companies must be a non-empty list")
    writes: list[tuple[Path, bytes]] = []
    catalog_sources: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    seen_cases: set[str] = set()
    seen_targets: set[Path] = set()
    for company in companies:
        if not isinstance(company, dict):
            raise ImportError("each company must be an object")
        case_id = company.get("case_id")
        if not isinstance(case_id, str) or not SAFE_ID.fullmatch(case_id):
            raise ImportError("each company requires a lowercase kebab-case case_id")
        if case_id in seen_cases:
            raise ImportError(f"duplicate case_id: {case_id}")
        seen_cases.add(case_id)
        subject = _validate_subject(company.get("subject"), case_id)
        sources = company.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ImportError(f"{case_id}: sources must be a non-empty list")
        case_hashes: dict[str, str] = {}
        for source in sources:
            if not isinstance(source, dict):
                raise ImportError(f"{case_id}: each source must be an object")
            source_id = source.get("source_id")
            if not isinstance(source_id, str) or not SAFE_ID.fullmatch(source_id):
                raise ImportError(f"{case_id}: invalid source_id")
            relative = source.get("file")
            if not isinstance(relative, str):
                raise ImportError(f"{case_id}/{source_id}: file is required")
            source_path = _inside(kb_root, relative)
            if source_path.suffix.lower() != ".json":
                raise ImportError(f"{case_id}/{source_id}: source must be a JSON source")
            raw_bytes = source_path.read_bytes()
            payload = _read_object(source_path)
            _scan_sensitive(payload)
            observed_at = source.get("observed_at")
            if not isinstance(observed_at, str):
                raise ImportError(f"{case_id}/{source_id}: observed_at is required")
            try:
                dt.date.fromisoformat(observed_at)
            except ValueError as exc:
                raise ImportError(f"{case_id}/{source_id}: invalid observed_at") from exc
            fact_pointers = source.get("facts")
            if not isinstance(fact_pointers, dict) or not fact_pointers:
                raise ImportError(f"{case_id}/{source_id}: facts must be a non-empty object")
            facts: dict[str, Any] = {}
            for name, pointer in fact_pointers.items():
                if not isinstance(name, str) or not isinstance(pointer, str):
                    raise ImportError(f"{case_id}/{source_id}: fact pointers must be strings")
                facts[name] = _pointer(payload, pointer)
            if facts.get("name") not in {None, subject["name"]}:
                raise ImportError(f"{case_id}/{source_id}: subject name mismatch")
            if facts.get("unified_social_credit_code") not in {
                None, subject["unified_social_credit_code"]
            }:
                raise ImportError(f"{case_id}/{source_id}: subject credit code mismatch")
            snapshot = {
                "subject": subject,
                "source_id": source_id,
                "observed_at": observed_at,
                "facts": facts,
            }
            snapshot_bytes = (
                json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            target = (
                Path("companies") / case_id / "kb" / "raw" /
                subject["name"] / f"{source_id}.json"
            )
            if target in seen_targets:
                raise ImportError(f"duplicate target: {target}")
            seen_targets.add(target)
            writes.append((target, snapshot_bytes))
            case_hashes[str(target)] = _sha256_bytes(snapshot_bytes)
            catalog_sources.append({
                "case_id": case_id,
                "source_id": source_id,
                "source_file": relative,
                "source_hash": _sha256_bytes(raw_bytes),
                "snapshot_file": str(target),
                "snapshot_hash": _sha256_bytes(snapshot_bytes),
                "observed_at": observed_at,
            })
        wiki_files = company.get("wiki") or []
        if not isinstance(wiki_files, list):
            raise ImportError(f"{case_id}: wiki must be a list")
        wiki_names: set[str] = set()
        for relative in wiki_files:
            if not isinstance(relative, str):
                raise ImportError(f"{case_id}: wiki paths must be strings")
            source_path = _inside(kb_root, relative)
            if source_path.suffix.lower() != ".md":
                raise ImportError(f"{case_id}: wiki source must be Markdown")
            if source_path.name in wiki_names:
                raise ImportError(f"{case_id}: duplicate wiki filename: {source_path.name}")
            wiki_names.add(source_path.name)
            writes.append((
                Path("companies") / case_id / "kb" / "wiki" /
                subject["name"] / source_path.name,
                source_path.read_bytes(),
            ))
        reviews.append({
            "case_id": case_id,
            "subject": subject,
            "review_status": "pending",
            "source_hashes": case_hashes,
            "company_kb": f"companies/{case_id}/kb",
            "expected_decision": None,
            "quality_state": None,
            "expected_reasons": None,
        })
    catalog = {"schema_version": "cws-kb-import/v1", "sources": catalog_sources}
    review = {"schema_version": "cws-gate-review/v1", "cases": reviews}
    return writes, catalog, review


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    temp: Path | None = None
    try:
        kb_root = args.kb_root.expanduser().resolve()
        manifest_path = args.manifest.expanduser().resolve()
        output = args.output.expanduser().resolve()
        try:
            output.relative_to(ROOT)
        except ValueError:
            pass
        else:
            raise ImportError("--output must be staging outside the repository")
        if output.exists() and any(output.iterdir()):
            raise ImportError(f"output must be empty: {output}")
        writes, catalog, review = _prepare(kb_root, _read_object(manifest_path))
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
        for relative, content in writes:
            target = temp / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (temp / "source-catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (temp / "review.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if output.exists():
            output.rmdir()
        os.replace(temp, output)
        temp = None
        print(f"Imported companies: {len(review['cases'])}")
        print(f"Staging: {output}")
        return 0
    except (ImportError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
