"""Legacy history transition to private memory with unread flat-file cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable


SNAPSHOT_SCHEMA_VERSION = "1.0.0"
SNAPSHOT_FORMAT_VERSION = "1.0.0"
MEMORY_CONTRACT_VERSION = "2.0.0"
MEMORY_CONTRACT_MIGRATION_KINDS = {
    "legacy-unversioned": "legacy_upgrade",
    "1.0.0": "legacy_upgrade",
    "2.0.0": "current_verify",
}

LEGACY_TRANSITION_SCHEMA_VERSION = "2.0.0"
_PRINCIPAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class MigrationError(RuntimeError):
    """Base error for safe migration refusal paths."""


class MigrationPreflightError(MigrationError):
    """Snapshot or target cannot be proven safe."""


class MigrationBlockedError(MigrationError):
    """Plan fails the canonical transition contract and cannot be applied."""


def memory_contract_migration_kind(source_contract_version: str) -> str:
    """Select the only migration path allowed for a recognized source contract."""

    try:
        return MEMORY_CONTRACT_MIGRATION_KINDS[source_contract_version]
    except (KeyError, TypeError) as exc:
        raise MigrationPreflightError(
            f"unsupported memory contract version: {source_contract_version!r}"
        ) from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_private_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _snapshot_versions(snapshot_manifest: dict[str, Any]) -> None:
    expected = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
        "status": "complete",
        "state_role": "source",
    }
    for key, value in expected.items():
        if snapshot_manifest.get(key) != value:
            raise MigrationPreflightError(f"{key} mismatch")
    snapshot_id = snapshot_manifest.get("snapshot_id")
    if not isinstance(snapshot_id, str) or len(snapshot_id) != 64:
        raise MigrationPreflightError("snapshot_id invalid")
    try:
        int(snapshot_id, 16)
    except ValueError as exc:
        raise MigrationPreflightError("snapshot_id invalid") from exc
    versions = snapshot_manifest.get("versions")
    if not isinstance(versions, dict) or versions.get("snapshot_schema") != SNAPSHOT_SCHEMA_VERSION:
        raise MigrationPreflightError("snapshot_schema version mismatch")


def validate_migration_preflight(
    snapshot_manifest: dict[str, Any], target_root: Path, marker: dict[str, Any]
) -> None:
    """Validate immutable snapshot identity and a marked isolated target."""

    _snapshot_versions(snapshot_manifest)
    if not target_root.is_absolute():
        raise MigrationPreflightError("target must be absolute")
    if target_root.is_symlink() or not target_root.is_dir():
        raise MigrationPreflightError("target must be a real directory")
    resolved = target_root.resolve(strict=True)
    info = resolved.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise MigrationPreflightError("target permissions or owner invalid")
    required = {
        "marker_version",
        "purpose",
        "target_id",
        "non_production",
        "filesystem_root",
        "snapshot_id",
        "contract_version",
    }
    if not isinstance(marker, dict) or set(marker) != required:
        raise MigrationPreflightError("target marker invalid")
    if marker["marker_version"] != "1.0.0" or marker["purpose"] != "familia-memory-migration":
        raise MigrationPreflightError("target marker version or purpose invalid")
    if marker["non_production"] is not True:
        raise MigrationPreflightError("non_production target required")
    if marker["filesystem_root"] != str(resolved):
        raise MigrationPreflightError("target marker root mismatch")
    if marker["snapshot_id"] != snapshot_manifest["snapshot_id"]:
        raise MigrationPreflightError("target snapshot identity mismatch")
    if marker["contract_version"] != MEMORY_CONTRACT_VERSION:
        raise MigrationPreflightError("memory contract version mismatch")
    if not isinstance(marker["target_id"], str) or not marker["target_id"].strip():
        raise MigrationPreflightError("target id invalid")


def _memory_text(value: Any) -> str:
    """Normalize a decoded memX value without silently discarding structure."""

    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _canonical_actor(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if _PRINCIPAL_ID.fullmatch(value) else None


def _history_source_digest(records: list[dict[str, Any]]) -> str:
    stable = [
        {
            "actor": record["actor"],
            "content": record["content"],
            "cursor": record["cursor"],
            "timestamp": record.get("timestamp"),
        }
        for record in records
    ]
    return _sha256(_canonical_bytes(stable))


def _read_transition_history(
    workspace: Path,
    known_actors: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Read legacy history while preserving every valid recorded actor.

    Schema version selects the supported record layout; it never changes
    identity. Unknown actors are discarded instead of being reassigned.
    """

    groups: dict[str, list[dict[str, Any]]] = {}
    issues: list[dict[str, Any]] = []
    parsed_rows: list[dict[str, Any]] = []
    path = workspace / "memory" / "history.jsonl"
    if not path.exists():
        return groups, issues

    for line_number, raw in enumerate(path.read_bytes().splitlines(), start=1):
        try:
            candidate = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            candidate = None
        valid_v1 = bool(
            isinstance(candidate, dict)
            and candidate.get("schema_version") == 1
            and isinstance(candidate.get("cursor"), int)
            and not isinstance(candidate.get("cursor"), bool)
            and isinstance(candidate.get("content"), str)
        )
        valid_v0 = bool(
            isinstance(candidate, dict)
            and set(candidate) == {"actor", "content", "cursor", "timestamp"}
            and isinstance(candidate.get("cursor"), int)
            and not isinstance(candidate.get("cursor"), bool)
            and isinstance(candidate.get("timestamp"), str)
            and isinstance(candidate.get("content"), str)
        )
        if not valid_v1 and not valid_v0:
            issues.append(
                {
                    "line": line_number,
                    "actor": None,
                    "cursor": None,
                    "reason": "history_malformed_or_unknown_schema",
                    "source_sha256": _sha256(raw),
                }
            )
            continue
        assert isinstance(candidate, dict)
        record = dict(candidate)
        if valid_v0:
            record["schema_version"] = 1
            record["provenance"] = {
                "source": "legacy_history_v0",
                "idempotency_key": None,
                "legacy_actor": record.get("actor"),
            }
        parsed_rows.append(
            {
                "record": record,
                "legacy_v0": valid_v0,
                "line": line_number,
                "raw": raw,
            }
        )

    actor_rows: list[dict[str, Any]] = []
    for row in parsed_rows:
        record = row["record"]
        actor = _canonical_actor(record.get("actor"))
        if actor is None:
            issues.append(
                {
                    "line": row["line"],
                    "actor": None,
                    "cursor": record["cursor"],
                    "reason": "history_actorless",
                    "source_sha256": _sha256(row["raw"]),
                }
            )
            continue
        record["actor"] = actor
        row["source_actor"] = actor
        actor_rows.append(row)

    for row in actor_rows:
        record = row["record"]
        actor = row["source_actor"]
        if actor not in known_actors:
            issues.append(
                {
                    "line": row["line"],
                    "actor": actor,
                    "cursor": record["cursor"],
                    "reason": "history_actor_unknown",
                    "source_sha256": _sha256(row["raw"]),
                }
            )
            continue
        groups.setdefault(actor, []).append(record)

    for records in groups.values():
        records.sort(
            key=lambda record: (
                record["cursor"],
                str(record.get("timestamp") or ""),
            )
        )
    return groups, issues


def _transition_file_action(
    *,
    relative: str,
    component: str,
) -> dict[str, Any]:
    return {
        "phase": "files",
        "component": component,
        "source": relative,
        "destination": None,
        "actor": None,
        "candidate_actor": None,
        "disposition": "erase_without_read",
        "reason": "flat_memory_retired",
    }


def build_legacy_transition_plan(
    *,
    workspace: Path,
    known_actors: set[str],
) -> dict[str, Any]:
    """Plan the operational legacy-files/history transition without fan-out.

    Flat files have no trustworthy owner and are retired without being read.
    History rows already carry ownership and are grouped for LLM consolidation
    into the actor's private long-term memory.
    """

    workspace = workspace.resolve(strict=True)
    actors = {
        actor
        for value in known_actors
        if (actor := _canonical_actor(value)) is not None
    }
    history_groups, history_issues = _read_transition_history(workspace, actors)
    actions = [
        _transition_file_action(
            relative="USER.md",
            component="user_profile",
        ),
        _transition_file_action(
            relative="MEMORY.md",
            component="memory",
        ),
        _transition_file_action(
            relative="memory/MEMORY.md",
            component="memory",
        ),
    ]

    for actor, records in sorted(history_groups.items()):
        source_sha256 = _history_source_digest(records)
        fact_id = "legacy-history"
        destination = f"private:{actor}:memory:{fact_id}"
        actions.append(
            {
                "phase": "history",
                "component": "history",
                "source": "memory/history.jsonl",
                "source_sha256": source_sha256,
                "actor": actor,
                "fact_id": fact_id,
                "source_actors": sorted({record["actor"] for record in records}),
                "cursors": [record["cursor"] for record in records],
                "record_count": len(records),
                "destination": destination,
                "disposition": "llm_required",
                "reason": "history_requires_consolidation",
            }
        )

    for issue in history_issues:
        actions.append(
            {
                "phase": "history",
                "component": "history",
                "source": "memory/history.jsonl",
                "source_sha256": issue["source_sha256"],
                "source_line": issue["line"],
                "actor": None,
                "source_actor": issue["actor"],
                "cursor": issue["cursor"],
                "destination": None,
                "disposition": "discarded_unknown",
                "reason": issue["reason"],
            }
        )

    counts: dict[str, int] = {}
    for action in actions:
        counts[action["disposition"]] = counts.get(action["disposition"], 0) + 1
    return {
        "schema_version": LEGACY_TRANSITION_SCHEMA_VERSION,
        "migration_kind": "legacy_upgrade",
        "source_contract_version": "1.0.0",
        "target_contract_version": MEMORY_CONTRACT_VERSION,
        "workspace": str(workspace),
        "known_actors": sorted(actors),
        "dry_run": True,
        "status": "ready",
        "actions": actions,
        "summary": counts,
    }


def _write_dream_cursor(workspace: Path, cursor: int) -> None:
    path = workspace / "memory" / ".dream_cursor"
    current = 0
    try:
        current = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        current = 0
    _write_private_atomic(path, f"{max(current, cursor)}\n".encode("utf-8"))


def make_history_consolidator(provider: Any, model: str) -> Callable[
    [str, list[dict[str, Any]], str], Awaitable[str]
]:
    """Create the LLM step used by the operational history transition."""

    async def _consolidate(
        actor: str,
        records: list[dict[str, Any]],
        existing_memory: str,
    ) -> str:
        history_by_content: dict[str, dict[str, Any]] = {}
        for record in records:
            content = record["content"]
            existing = history_by_content.get(content)
            if existing is None:
                history_by_content[content] = {
                    "cursor": record["cursor"],
                    "timestamp": record.get("timestamp"),
                    "content": content,
                    "duplicate_count": 1,
                }
            else:
                existing["last_cursor"] = record["cursor"]
                existing["last_timestamp"] = record.get("timestamp")
                existing["duplicate_count"] += 1
        history = list(history_by_content.values())
        system = (
            "You migrate legacy conversation summaries into one principal's private memory. "
            "Return Markdown bullet points containing only durable atomic facts. Deduplicate "
            "against the existing private memory. Do not return raw dialogue, analysis, a code "
            "fence, headings, or facts about another actor. This output is private to the named "
            "actor and must never be phrased as shared family memory. Exact duplicate records "
            "are collapsed into duplicate_count and must not duplicate the resulting facts."
        )
        user = (
            f"Actor: {actor}\n\n"
            "Existing private memory:\n"
            f"{existing_memory or '(empty)'}\n\n"
            "Legacy history records (JSON):\n"
            f"{json.dumps(history, ensure_ascii=False, sort_keys=True)}"
        )
        response = await provider.chat_with_retry(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=None,
            tool_choice=None,
        )
        content = str(getattr(response, "content", "") or "").strip()
        if not content:
            raise MigrationError("history consolidation returned empty memory")
        if content.startswith("```"):
            raise MigrationError("history consolidation returned a code fence")
        return content

    return _consolidate


def make_configured_history_consolidator(
    config_path: Path | None = None,
) -> Callable[[str, list[dict[str, Any]], str], Awaitable[str]]:
    """Load nanobot's configured provider lazily for CLI migration apply."""

    from nanobot.config.loader import load_config, resolve_config_env_vars
    from nanobot.nanobot import _make_provider

    resolved = config_path.expanduser().resolve() if config_path is not None else None
    config = resolve_config_env_vars(load_config(resolved))
    provider = _make_provider(config)
    return make_history_consolidator(provider, config.agents.defaults.model)


async def apply_legacy_transition_plan(
    *,
    plan: dict[str, Any],
    workspace: Path,
    get_value: Callable[[str], Any],
    ingestor: Any,
    consolidate_history: Callable[
        [str, list[dict[str, Any]], str], Awaitable[str]
    ],
) -> dict[str, Any]:
    """Apply history through the principal ingestor, then retire flat files.

    ``history.jsonl`` and ``SOUL.md`` are never changed. The three contract
    flat-memory files are replaced with empty files only after every required
    actor-owned history write is confirmed. Dream cursor publication is last.
    """

    workspace = workspace.resolve(strict=True)
    if plan.get("schema_version") != LEGACY_TRANSITION_SCHEMA_VERSION:
        raise MigrationBlockedError("legacy transition schema mismatch")
    if plan.get("workspace") != str(workspace):
        raise MigrationBlockedError("legacy transition workspace mismatch")

    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise MigrationBlockedError("legacy transition actions missing")

    expected_flat_actions = {
        ("USER.md", "user_profile"),
        ("MEMORY.md", "memory"),
        ("memory/MEMORY.md", "memory"),
    }
    flat_actions = [
        action
        for action in actions
        if isinstance(action, dict) and action.get("phase") == "files"
    ]
    if (
        len(flat_actions) != 3
        or {
            (action.get("source"), action.get("component"))
            for action in flat_actions
        }
        != expected_flat_actions
        or any(
            action.get("destination") is not None
            or action.get("disposition") != "erase_without_read"
            for action in flat_actions
        )
    ):
        raise MigrationBlockedError("legacy transition flat-file actions mismatch")

    history_actions: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            raise MigrationBlockedError("legacy transition action is not an object")
        if action.get("phase") == "files":
            continue
        if (
            action.get("phase") != "history"
            or action.get("component") != "history"
            or action.get("source") != "memory/history.jsonl"
        ):
            raise MigrationBlockedError("legacy transition history action mismatch")
        actor = action.get("actor")
        if actor is None:
            if (
                action.get("destination") is not None
                or action.get("disposition") != "discarded_unknown"
            ):
                raise MigrationBlockedError("invalid discarded history action")
            continue
        fact_id = action.get("fact_id")
        expected = f"private:{actor}:memory:{fact_id}"
        if (
            not isinstance(actor, str)
            or not actor
            or fact_id != "legacy-history"
            or action.get("destination") != expected
            or not isinstance(action.get("cursors"), list)
        ):
            raise MigrationBlockedError("non-private or mismatched migration destination")
        history_actions.append(action)

    actors = set(plan.get("known_actors") or [])
    history_groups, _current_history_issues = _read_transition_history(workspace, actors)
    planned_history_digests = {
        action["actor"]: action.get("source_sha256")
        for action in history_actions
    }
    current_history_digests = {
        actor: _history_source_digest(records)
        for actor, records in history_groups.items()
    }
    if (
        len(planned_history_digests) != len(history_actions)
        or planned_history_digests != current_history_digests
    ):
        raise MigrationBlockedError("history changed after dry-run")

    failed_actions: list[str] = []
    failed_actors: set[str] = set()
    written_keys: list[str] = []
    applied_actions = 0
    fatal_failure: str | None = None
    max_history_cursor: int | None = None
    for action in history_actions:
        actor = action["actor"]
        records = history_groups.get(actor, [])
        try:
            if not records or _history_source_digest(records) != action.get("source_sha256"):
                raise MigrationError("history changed after dry-run")
            cursors = [record["cursor"] for record in records]
            if cursors != action.get("cursors"):
                raise MigrationError("history cursor set changed after dry-run")
            destination = str(action["destination"])
            disposition = action.get("disposition")
            if disposition != "llm_required":
                raise MigrationError("history action is not approved for consolidation")
            existing = _memory_text(get_value(destination))
            consolidated = (
                await consolidate_history(actor, records, existing)
            ).strip()
            if not consolidated:
                raise MigrationError("history consolidation returned empty memory")
            ingest_result = await ingestor.ingest(
                server_principal=actor,
                server_topic=None,
                operation={
                    "kind": "memory",
                    "fact_id": action["fact_id"],
                    "value": consolidated,
                },
            )
            if (
                not isinstance(ingest_result, str)
                or not ingest_result.startswith("committed:")
            ):
                raise MigrationError("history memory write was not committed")
            written_keys.append(destination)
            max_history_cursor = max(max_history_cursor or 0, max(cursors))
            applied_actions += 1
        except Exception:  # noqa: BLE001 - result remains non-sensitive and actionable
            failed_actors.add(actor)
            fatal_failure = f"history:{actor}"
            failed_actions.append(fatal_failure)
            break

    if fatal_failure:
        status_value = "partial" if applied_actions else "failed"
        return {
            "status": status_value,
            "applied_actions": applied_actions,
            "written_keys": sorted(set(written_keys)),
            "failed_actors": sorted(failed_actors),
            "failed_actions": sorted(failed_actions),
            "fatal_failure": fatal_failure,
            "dream_cursor_updated": False,
        }

    cleaned_files = 0
    try:
        for action in flat_actions:
            source = workspace.joinpath(*PurePosixPath(action["source"]).parts)
            _write_private_atomic(source, b"")
            cleaned_files += 1
    except Exception as exc:  # noqa: BLE001 - result remains non-sensitive
        fatal_failure = f"files:{type(exc).__name__}"
        failed_actions.append(fatal_failure)
        return {
            "status": "partial" if applied_actions or cleaned_files else "failed",
            "applied_actions": applied_actions + cleaned_files,
            "written_keys": sorted(set(written_keys)),
            "failed_actors": sorted(failed_actors),
            "failed_actions": sorted(failed_actions),
            "fatal_failure": fatal_failure,
            "dream_cursor_updated": False,
        }

    dream_cursor_updated = False
    try:
        if max_history_cursor is not None:
            _write_dream_cursor(workspace, max_history_cursor)
            dream_cursor_updated = True
    except Exception as exc:  # noqa: BLE001 - result remains non-sensitive
        fatal_failure = f"cursor:{type(exc).__name__}"
        failed_actions.append(fatal_failure)
        return {
            "status": "partial",
            "applied_actions": applied_actions + cleaned_files,
            "written_keys": sorted(set(written_keys)),
            "failed_actors": sorted(failed_actors),
            "failed_actions": sorted(failed_actions),
            "fatal_failure": fatal_failure,
            "dream_cursor_updated": False,
        }

    return {
        "status": "complete",
        "applied_actions": applied_actions + cleaned_files,
        "written_keys": sorted(set(written_keys)),
        "failed_actors": sorted(failed_actors),
        "failed_actions": sorted(failed_actions),
        "fatal_failure": None,
        "dream_cursor_updated": dream_cursor_updated,
    }


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _load_known_actors(source_root: Path) -> set[str]:
    candidates = (source_root / "principals.json", source_root / "config" / "principals.json")
    for candidate in candidates:
        if not candidate.exists():
            continue
        value = json.loads(candidate.read_text(encoding="utf-8"))
        return {
            item["id"]
            for item in value.get("principals", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    return set()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MigrationError("JSON object required")
    return value


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Consolidate legacy history into private memory; "
            "erase three flat memory files unread"
        )
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--classifications", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        import asyncio

        from familia.acl.graph_io import get_raw, resolve_admin_key
        from familia.memx_client import memx_base_url
        from familia.principal_memory_ingestor import PrincipalMemoryIngestor
        from scripts.compare_memory_state import load_and_validate_manifest

        snapshot_root = args.snapshot.resolve(strict=True)
        snapshot = load_and_validate_manifest(snapshot_root / "manifest.json")
        target_root = args.target.resolve(strict=True)
        marker_path = target_root / ".familia-memory-migration-target.json"
        marker = _load_json(marker_path)
        validate_migration_preflight(snapshot, target_root, marker)
        source_root = (args.source_root or (target_root / "state" / "files")).resolve(strict=True)
        if not _within(source_root, target_root):
            raise MigrationPreflightError("source root must be inside isolated target")
        for output in (args.manifest, args.journal):
            resolved_parent = output.absolute().parent.resolve(strict=True)
            if not _within(resolved_parent, target_root):
                raise MigrationPreflightError("manifest and journal must stay in isolated target")
        plan = build_legacy_transition_plan(
            workspace=source_root,
            known_actors=_load_known_actors(source_root),
        )
        _write_private_atomic(args.manifest, _canonical_bytes(plan) + b"\n")
        result: dict[str, Any] = {"status": "dry_run"}
        if args.apply:
            llm_required = any(
                action.get("disposition") == "llm_required"
                for action in plan["actions"]
            )
            if llm_required:
                consolidator = make_configured_history_consolidator()
            else:

                async def consolidator(
                    _actor: str,
                    _records: list[dict[str, Any]],
                    _existing: str,
                ) -> str:
                    raise RuntimeError(
                        "history consolidator called without an approved history action"
                    )

            ingestor = PrincipalMemoryIngestor(
                base_url=memx_base_url(),
                api_key=resolve_admin_key(),
            )
            result = asyncio.run(
                apply_legacy_transition_plan(
                    plan=plan,
                    workspace=source_root,
                    get_value=get_raw,
                    ingestor=ingestor,
                    consolidate_history=consolidator,
                )
            )
        if args.json:
            print(json.dumps(result if args.apply else plan, sort_keys=True))
        elif args.apply:
            failed_actors = result.get("failed_actors") or []
            error = result.get("error") or result.get("fatal_failure") or "-"
            print(
                f"status={result['status']} "
                f"applied_actions={result.get('applied_actions', 0)} "
                f"failed_actors={','.join(failed_actors) or '-'} "
                f"error={error}"
            )
        else:
            print(
                f"migration={plan['status']} actions={len(plan['actions'])} "
                f"llm_required={plan['summary'].get('llm_required', 0)} "
                f"discarded_unknown="
                f"{plan['summary'].get('discarded_unknown', 0)}"
            )
        return {
            "dry_run": 0,
            "complete": 0,
            "partial": 2,
            "failed": 1,
        }.get(result["status"], 1)
    except (MigrationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"migration=refused reason={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
