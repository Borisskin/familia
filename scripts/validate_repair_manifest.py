#!/usr/bin/env python3
"""Validate an RP-090 value-free migration/repair plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "release" / "memory-migration.schema.json"
SOURCE_ROOT = REPO_ROOT / "familia" / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


class DuplicateKey(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKey(key)
        result[key] = value
    return result


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate(path: Path, *, require_zero_unreviewed: bool) -> dict[str, Any]:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    Draft202012Validator.check_schema(schema)
    manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)

    action_ids: set[str] = set()
    counts = {key: 0 for key in manifest["summary"]}
    for action in manifest["actions"]:
        action_id = action["action_id"]
        if action_id in action_ids:
            raise ValueError("duplicate action_id")
        action_ids.add(action_id)
        core = {key: value for key, value in action.items() if key != "action_id"}
        if _sha256(_canonical(core)) != action_id:
            raise ValueError("action_id mismatch")
        counts[action["disposition"]] += 1
        if action["disposition"] != "write" and action["writes"] != 0:
            raise ValueError("non-write action declares writes")
    if counts != manifest["summary"]:
        raise ValueError("summary mismatch")
    unresolved = sum(
        counts[name]
        for name in ("conflict", "dirty_legacy", "llm_required", "quarantine_needs_review")
    )
    if require_zero_unreviewed and unresolved:
        raise ValueError(f"unreviewed actions remain: {unresolved}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--require-zero-unreviewed", action="store_true")
    parser.add_argument("--require-approved-quarantine", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = validate(
            args.manifest,
            require_zero_unreviewed=(
                args.require_zero_unreviewed or args.require_approved_quarantine
            ),
        )
    except Exception as exc:  # noqa: BLE001 - safe validator CLI refusal
        print(f"repair_manifest=invalid reason={type(exc).__name__}:{exc}", file=sys.stderr)
        return 2
    print(
        f"repair_manifest=valid migration_id={manifest['migration_id']} "
        f"actions={len(manifest['actions'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
