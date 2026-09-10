"""Legacy history transition to private memory with unread flat-file cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

SNAPSHOT_SCHEMA_VERSION = "1.0.0"
SNAPSHOT_FORMAT_VERSION = "1.0.0"
MEMORY_CONTRACT_VERSION = "2.0.0"
MEMORY_CONTRACT_MIGRATION_KINDS = {
    "legacy-unversioned": "legacy_upgrade",
    "1.0.0": "legacy_upgrade",
    "2.0.0": "current_verify",
}

LEGACY_TRANSITION_SCHEMA_VERSION = "2.0.0"
LEGACY_TRANSITION_COMPLETION_KEY = "shared:familia.migrations.legacy-history-v1"
LEGACY_TRANSITION_COMPLETION_MARKER = {
    "schema_version": "1.0.0",
    "migration": "legacy-history-v1",
    "status": "complete",
    # This value is the immutable contract of the v1 marker. Do not couple it
    # to a future MEMORY_CONTRACT_VERSION bump: completed v1 transitions must
    # remain completed in later releases.
    "target_contract_version": "2.0.0",
}
_PRINCIPAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DOCKER_SOCKET = "unix:///var/run/docker.sock"
_DOCKER_ENV_BLOCKLIST = frozenset(
    {
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    }
)
_NAMESPACE_NAMES = ("net", "pid", "mnt")


class MigrationError(RuntimeError):
    """Base error for safe migration refusal paths."""


class MigrationPreflightError(MigrationError):
    """Snapshot or target cannot be proven safe."""


class MigrationBlockedError(MigrationError):
    """Plan fails the canonical transition contract and cannot be applied."""


def legacy_transition_is_complete(value: Any) -> bool:
    """Return whether the exact one-time transition marker is present.

    A missing value permits the initial transition or a retry after failure.
    Any present but unknown value blocks the migration so it cannot silently
    skip legacy history or repeat an already completed import.
    """

    if value is None:
        return False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MigrationBlockedError(
                "legacy-history completion marker is not valid JSON"
            ) from exc
    if value != LEGACY_TRANSITION_COMPLETION_MARKER:
        raise MigrationBlockedError(
            "legacy-history completion marker has an unsupported value"
        )
    return True


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
    _write_private_atomic(path, f"{max(current, cursor)}\n".encode())


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


def _load_known_actors(principals_path: Path) -> set[str]:
    """Load one explicit, non-empty principals registry.

    The migration must never silently fall back to a process-global registry.
    ``load_registry`` performs the existing duplicate-key checks after this
    function has rejected malformed or empty JSON structures.
    """

    try:
        value = _load_json(principals_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationPreflightError("principals registry is missing or invalid") from exc
    entries = value.get("principals")
    if not isinstance(entries, list) or not entries:
        raise MigrationPreflightError("principals registry is missing or empty")
    actor_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise MigrationPreflightError("principals registry entry invalid")
        actor = entry.get("id")
        if not isinstance(actor, str) or _canonical_actor(actor) is None:
            raise MigrationPreflightError("principals registry id invalid")
        if actor in actor_ids:
            raise MigrationPreflightError("principals registry contains duplicate id")
        actor_ids.append(actor)

    from familia.principals import load_registry

    registry = load_registry(principals_path)
    if set(registry.ids) != set(actor_ids):
        raise MigrationPreflightError("principals registry could not be loaded")
    return set(actor_ids)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MigrationError("JSON object required")
    return value


def _target_config(config_path: Path, install_root: Path, workspace: Path, principals: Path) -> dict[str, Any]:
    """Validate the explicit target contract without reading process config."""

    if not _within(config_path.resolve(strict=True), install_root):
        raise MigrationPreflightError("target config must be inside install root")
    config = _load_json(config_path)
    required = {
        "schema_version",
        "isolation_id",
        "install_root",
        "workspace",
        "principals",
        "runner",
        "memx",
        "redis",
        "network",
        "model",
    }
    if config.get("schema_version") != "1.0.0" or not required.issubset(config):
        raise MigrationPreflightError("target config schema invalid")
    if not isinstance(config["isolation_id"], str) or not config["isolation_id"].strip():
        raise MigrationPreflightError("target isolation id invalid")
    for key, path in (
        ("install_root", install_root),
        ("workspace", workspace),
        ("principals", principals),
    ):
        if config[key] != str(path):
            raise MigrationPreflightError(f"target config {key} mismatch")

    model = config["model"]
    if not isinstance(model, dict) or not all(
        isinstance(model.get(key), str) and model[key].strip()
        for key in ("provider", "name", "config_path")
    ):
        raise MigrationPreflightError("target model config invalid")
    model_path = Path(model["config_path"]).expanduser()
    if not model_path.is_absolute() or not _within(model_path.resolve(strict=False), install_root):
        raise MigrationPreflightError("target model config outside install root")

    for section in ("runner", "memx", "redis"):
        value = config[section]
        if not isinstance(value, dict):
            raise MigrationPreflightError(f"target {section} config invalid")
        for key in ("name", "id", "image_digest"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise MigrationPreflightError(f"target {section} identity invalid")
        if re.fullmatch(r"[0-9a-f]{64}", value["id"]) is None:
            raise MigrationPreflightError(f"target {section} id invalid")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_digest"]) is None:
            raise MigrationPreflightError(f"target {section} image digest invalid")
        endpoint_alias = value.get("endpoint_alias")
        if endpoint_alias is not None and (
            not isinstance(endpoint_alias, str) or not endpoint_alias.strip()
        ):
            raise MigrationPreflightError(f"target {section} endpoint alias invalid")
    network = config["network"]
    if not isinstance(network, dict) or not all(
        isinstance(network.get(key), str) and network[key].strip()
        for key in ("name", "id")
    ):
        raise MigrationPreflightError("target network identity invalid")
    if re.fullmatch(r"[0-9a-f]{64}", network["id"]) is None:
        raise MigrationPreflightError("target network id invalid")

    memx = config["memx"]
    redis = config["redis"]
    if not all(
        isinstance(memx.get(key), str) and memx[key].strip()
        for key in ("base_url", "api_key", "redis_env", "redis_url")
    ):
        raise MigrationPreflightError("target memX config invalid")
    if not isinstance(redis.get("storage_volume"), dict) or not isinstance(
        redis.get("storage_destination"), str
    ):
        raise MigrationPreflightError("target Redis storage config invalid")
    if not isinstance(redis.get("port"), int) or not 1 <= redis["port"] <= 65535:
        raise MigrationPreflightError("target Redis port invalid")
    if not isinstance(redis.get("database"), int) or not 0 <= redis["database"] <= 65535:
        raise MigrationPreflightError("target Redis database invalid")
    for key in ("name", "id"):
        if not isinstance(redis["storage_volume"].get(key), str) or not redis["storage_volume"][key].strip():
            raise MigrationPreflightError("target Redis volume identity invalid")
    if not redis["storage_destination"].startswith("/"):
        raise MigrationPreflightError("target Redis storage destination invalid")
    runner_mount = config["runner"].get("install_mount")
    if not isinstance(runner_mount, dict) or not all(
        isinstance(runner_mount.get(key), str) and runner_mount[key].strip()
        for key in ("name", "id", "destination")
    ):
        raise MigrationPreflightError("target install volume config invalid")
    return config


def _docker_command_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _DOCKER_ENV_BLOCKLIST
    }


def _namespace_payload(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict) or set(value) != set(_NAMESPACE_NAMES):
        raise MigrationPreflightError("Docker namespace evidence has an invalid shape")
    result: dict[str, dict[str, int]] = {}
    for name in _NAMESPACE_NAMES:
        pair = value.get(name)
        if not isinstance(pair, dict) or set(pair) != {"st_dev", "st_ino"}:
            raise MigrationPreflightError("Docker namespace evidence has an invalid shape")
        device = pair.get("st_dev")
        inode = pair.get("st_ino")
        if (
            isinstance(device, bool)
            or not isinstance(device, int)
            or isinstance(inode, bool)
            or not isinstance(inode, int)
        ):
            raise MigrationPreflightError("Docker namespace evidence has invalid stat values")
        result[name] = {"st_dev": device, "st_ino": inode}
    return result


def _local_namespace_payload() -> dict[str, dict[str, int]]:
    payload: dict[str, dict[str, int]] = {}
    try:
        for name in _NAMESPACE_NAMES:
            info = os.stat(f"/proc/self/ns/{name}")
            payload[name] = {"st_dev": info.st_dev, "st_ino": info.st_ino}
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise MigrationPreflightError("current namespace evidence is unavailable") from exc
    return _namespace_payload(payload)


def _docker_runner_namespace_proof(runner_id: str) -> None:
    local = _local_namespace_payload()
    script = (
        "import json, os\n"
        "namespaces = {}\n"
        "for name in ('net', 'pid', 'mnt'):\n"
        "    info = os.stat('/proc/self/ns/' + name)\n"
        "    namespaces[name] = {'st_dev': info.st_dev, 'st_ino': info.st_ino}\n"
        "print(json.dumps(namespaces))\n"
    )
    try:
        completed = subprocess.run(
            [
                "docker",
                "--host",
                _DOCKER_SOCKET,
                "exec",
                runner_id,
                "python3",
                "-c",
                script,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=_docker_command_env(),
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MigrationPreflightError("Docker namespace proof unavailable") from exc
    try:
        remote = _namespace_payload(json.loads(completed.stdout))
    except (TypeError, ValueError) as exc:
        raise MigrationPreflightError("Docker namespace proof returned invalid JSON") from exc
    if remote != local:
        raise MigrationPreflightError("Docker runner namespace mismatch")


def _docker_inspect(kind: str, reference: str) -> dict[str, Any]:
    """Inspect exactly one object through the selected local Docker daemon."""

    try:
        completed = subprocess.run(
            ["docker", "--host", _DOCKER_SOCKET, "inspect", "--type", kind, reference],
            check=True,
            capture_output=True,
            text=True,
            env=_docker_command_env(),
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MigrationPreflightError("Docker inspect unavailable") from exc
    try:
        values = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise MigrationPreflightError("Docker inspect returned invalid JSON") from exc
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise MigrationPreflightError("Docker inspect returned an unexpected object count")
    return values[0]


def _docker_verify_target(config: dict[str, Any]) -> None:
    """Prove the target is the one running isolated Docker environment."""

    isolation_id = config["isolation_id"]
    label_key = "familia.target"
    expected_network = config["network"]
    containers = {
        section: _docker_inspect("container", config[section]["name"])
        for section in ("runner", "memx", "redis")
    }
    expected_ids = {config[section]["id"] for section in containers}

    network_members: set[str] = set()
    for section, actual in containers.items():
        declared = config[section]
        if actual.get("Id") != declared["id"] or str(actual.get("Name", "")).lstrip("/") != declared["name"]:
            raise MigrationPreflightError(f"Docker {section} identity mismatch")
        if actual.get("Image") != declared["image_digest"]:
            raise MigrationPreflightError(f"Docker {section} image mismatch")
        if not (actual.get("State") or {}).get("Running"):
            raise MigrationPreflightError(f"Docker {section} is not running")
        labels = (actual.get("Config") or {}).get("Labels") or {}
        if labels.get(label_key) != isolation_id:
            raise MigrationPreflightError(f"Docker {section} isolation label mismatch")
        networks = (actual.get("NetworkSettings") or {}).get("Networks") or {}
        if set(networks) != {expected_network["name"]}:
            raise MigrationPreflightError(f"Docker {section} has an external network")
        network_info = networks.get(expected_network["name"]) or {}
        if network_info.get("NetworkID") != expected_network["id"]:
            raise MigrationPreflightError(f"Docker {section} network mismatch")
        network_members.add(actual["Id"])

    actual_network = _docker_inspect("network", expected_network["name"])
    if actual_network.get("Id") != expected_network["id"] or not actual_network.get("Internal"):
        raise MigrationPreflightError("Docker target network is not internal")
    labels = actual_network.get("Labels") or {}
    if labels.get(label_key) != isolation_id:
        raise MigrationPreflightError("Docker target network isolation label mismatch")
    actual_members = set((actual_network.get("Containers") or {}).keys())
    if actual_members != network_members or actual_members != expected_ids:
        raise MigrationPreflightError("Docker target network membership mismatch")

    runner_mount = config["runner"]["install_mount"]
    mounts = containers["runner"].get("Mounts") or []

    def is_runner_volume(mount: Any) -> bool:
        return (
            isinstance(mount, dict)
            and mount.get("Type") == "volume"
            and mount.get("Name") == runner_mount["name"]
            and mount.get("Destination") == runner_mount["destination"]
            and mount.get("RW") is True
        )

    if not any(is_runner_volume(mount) for mount in mounts):
        raise MigrationPreflightError("install root is not on the configured runner volume")
    install_root = PurePosixPath(config["install_root"])
    volume_destination = PurePosixPath(runner_mount["destination"])
    if not install_root.is_absolute() or not volume_destination.is_absolute():
        raise MigrationPreflightError("install root is not on the configured runner volume")
    covering_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict)
        and isinstance(mount.get("Destination"), str)
        and install_root.is_relative_to(PurePosixPath(mount["Destination"]))
    ]
    if not covering_mounts:
        raise MigrationPreflightError("install root is not on the configured runner volume")
    deepest = max(
        len(PurePosixPath(mount["Destination"]).parts)
        for mount in covering_mounts
    )
    deepest_mounts = [
        mount
        for mount in covering_mounts
        if len(PurePosixPath(mount["Destination"]).parts) == deepest
    ]
    if len(deepest_mounts) != 1 or not is_runner_volume(deepest_mounts[0]):
        raise MigrationPreflightError("install root is not on the configured runner volume")
    for mount in mounts:
        if (
            isinstance(mount, dict)
            and not is_runner_volume(mount)
            and isinstance(mount.get("Destination"), str)
            and PurePosixPath(mount["Destination"]).is_relative_to(install_root)
        ):
            raise MigrationPreflightError("install root is overlapped by another mount")
    runner_volume = _docker_inspect("volume", config["runner"]["install_mount"]["name"])
    runner_volume_id = runner_volume.get("Id") or runner_volume.get("Name")
    if runner_volume.get("Name") != runner_mount["name"] or runner_volume_id != runner_mount.get("id", runner_mount["name"]):
        raise MigrationPreflightError("runner install volume identity mismatch")
    runner_volume_labels = runner_volume.get("Labels") or {}
    if runner_volume_labels.get(label_key) != isolation_id:
        raise MigrationPreflightError("runner install volume isolation label mismatch")

    redis = containers["redis"]
    redis_volume = _docker_inspect("volume", config["redis"]["storage_volume"]["name"])
    declared_volume = config["redis"]["storage_volume"]
    redis_volume_id = redis_volume.get("Id") or redis_volume.get("Name")
    if redis_volume.get("Name") != declared_volume["name"] or redis_volume_id != declared_volume["id"]:
        raise MigrationPreflightError("Redis storage volume identity mismatch")
    volume_labels = redis_volume.get("Labels") or {}
    if volume_labels.get(label_key) != isolation_id:
        raise MigrationPreflightError("Redis storage volume isolation label mismatch")
    if not any(
        mount.get("Type") == "volume"
        and mount.get("Name") == declared_volume["name"]
        and mount.get("Destination") == config["redis"]["storage_destination"]
        for mount in redis.get("Mounts") or []
    ):
        raise MigrationPreflightError("Redis storage is not attached to the target")

    runner_id = containers["runner"].get("Id")
    if not isinstance(runner_id, str):
        raise MigrationPreflightError("Docker runner identity is invalid")
    _docker_runner_namespace_proof(runner_id)

    memx = config["memx"]
    parsed = urlparse(memx["base_url"])
    memx_alias = memx.get("endpoint_alias", memx["name"])
    if parsed.scheme not in {"http", "https"} or parsed.hostname != memx_alias:
        raise MigrationPreflightError("memX address is not the target network endpoint")
    memx_network = (containers["memx"].get("NetworkSettings") or {}).get("Networks", {}).get(expected_network["name"], {})
    if parsed.hostname not in (memx_network.get("Aliases") or []):
        raise MigrationPreflightError("memX address alias is not attached to target network")
    memx_env = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for item in (containers["memx"].get("Config") or {}).get("Env") or []
        if isinstance(item, str) and "=" in item
    }
    if memx_env.get(memx["redis_env"]) != memx["redis_url"]:
        raise MigrationPreflightError("memX Redis backend does not match target")
    redis_url = urlparse(memx["redis_url"])
    redis_alias = config["redis"].get("endpoint_alias", config["redis"]["name"])
    if (
        redis_url.hostname != redis_alias
        or redis_url.port != config["redis"]["port"]
        or redis_url.path != f"/{config['redis']['database']}"
    ):
        raise MigrationPreflightError("memX Redis host is not the target Redis")


def _target_get_raw(base_url: str, api_key: str) -> Callable[[str], Any]:
    """Build a raw memX reader bound to explicit target credentials."""

    import httpx

    def _get(key: str) -> Any:
        try:
            response = httpx.get(
                f"{base_url.rstrip('/')}/get",
                headers={"x-api-key": api_key},
                params={"key": key},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            raise MigrationError(f"target memX read failed: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise MigrationError(f"target memX read failed: status {response.status_code}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise MigrationError("target memX returned invalid JSON") from exc
        if isinstance(payload, dict) and "value" in payload:
            return payload["value"]
        return payload

    return _get


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Consolidate legacy history into private memory; "
            "erase three flat memory files unread"
        )
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--principals", type=Path, required=True)
    parser.add_argument("--target-config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--classifications", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        import asyncio

        from scripts.compare_memory_state import load_and_validate_manifest

        from familia.principal_memory_ingestor import PrincipalMemoryIngestor

        snapshot_root = args.snapshot.resolve(strict=True)
        snapshot = load_and_validate_manifest(snapshot_root / "manifest.json")
        target_root = args.install_root.resolve(strict=True)
        marker_path = target_root / ".familia-memory-migration-target.json"
        marker = _load_json(marker_path)
        validate_migration_preflight(snapshot, target_root, marker)
        source_root = args.workspace.resolve(strict=True)
        principals_path = args.principals.resolve(strict=True)
        if not _within(source_root, target_root) or not source_root.is_dir():
            raise MigrationPreflightError("workspace must be inside isolated target")
        if not _within(principals_path, target_root) or not principals_path.is_file():
            raise MigrationPreflightError("principals registry must be inside isolated target")
        target_config = _target_config(
            args.target_config.resolve(strict=True),
            target_root,
            source_root,
            principals_path,
        )
        for output in (args.manifest, args.journal):
            resolved_parent = output.absolute().parent.resolve(strict=True)
            if not _within(resolved_parent, target_root):
                raise MigrationPreflightError("manifest and journal must stay in isolated target")
        if args.apply:
            _docker_verify_target(target_config)
        known_actors = _load_known_actors(principals_path)
        plan = build_legacy_transition_plan(
            workspace=source_root,
            known_actors=known_actors,
        )
        _write_private_atomic(args.manifest, _canonical_bytes(plan) + b"\n")
        result: dict[str, Any] = {"status": "dry_run"}
        if args.apply:
            llm_required = any(
                action.get("disposition") == "llm_required"
                for action in plan["actions"]
            )
            if llm_required:
                consolidator = make_configured_history_consolidator(
                    Path(target_config["model"]["config_path"])
                )
            else:

                async def consolidator(
                    _actor: str,
                    _records: list[dict[str, Any]],
                    _existing: str,
                ) -> str:
                    raise RuntimeError(
                        "history consolidator called without an approved history action"
                    )

            memx = target_config["memx"]
            ingestor = PrincipalMemoryIngestor(
                base_url=memx["base_url"],
                api_key=memx["api_key"],
                principal_exists=known_actors.__contains__,
            )
            result = asyncio.run(
                apply_legacy_transition_plan(
                    plan=plan,
                    workspace=source_root,
                    get_value=_target_get_raw(memx["base_url"], memx["api_key"]),
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
