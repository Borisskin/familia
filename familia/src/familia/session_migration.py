"""Safe, repeatable migration of legacy nanobot sessions.

The module deliberately does not import or start nanobot.  It reads legacy
JSONL files, proves message groups from their own records, and writes the
canonical JSONL shape used by nanobot 0.3.0 only when ``--apply`` is explicit.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .session_identity import make_private_session_key

DEFAULT_SOURCE_ROOT = Path(
    "D:/chat/familia/dist/nanobot-legacy-20260907-111309"
)
REPORT_NAME = "migration-report.json"
SOURCE_SNAPSHOT_DIR = "source-snapshot"
TARGET_SESSIONS_DIR = "sessions"
QUARANTINE_DIR = "quarantine"
SCHEMA_VERSION = "1.0"
_MESSAGE_ROLES = frozenset({"user", "assistant", "tool"})
_CHECKPOINT_KEYS = frozenset(
    {
        "checkpoint",
        "runtime_checkpoint",
        "inflight",
        "in_flight",
        "pending_tool_calls",
        "completed_tool_results",
    }
)
_EPOCH = "1970-01-01T00:00:00+00:00"
_PRINCIPAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object, *, indent: int | None = None) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _canonical_principal(value: object) -> str | None:
    """Validate the memory-contract syntax without checking registration."""
    if not isinstance(value, str) or not _PRINCIPAL_ID.fullmatch(value):
        return None
    return value


def _registry_ids(registry: object = None) -> set[str]:
    """Read IDs from PrincipalRegistry or an explicit set supplied by a caller."""
    if registry is None:
        try:
            registry = get_registry()
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive import boundary
            return set()
    try:
        from .principals import PrincipalRegistry
    except ImportError:  # pragma: no cover - package import boundary
        PrincipalRegistry = ()  # type: ignore[assignment,misc]
    if isinstance(registry, PrincipalRegistry):
        try:
            values = registry.ids
            return {value for value in values if isinstance(value, str)}
        except Exception:  # noqa: BLE001
            return set()
    if isinstance(registry, (set, frozenset)):
        return {value for value in registry if isinstance(value, str)}
    return set()


def get_registry() -> object:
    """Resolve the live Familia registry lazily for tests and CLI callers."""
    from .principals import get_registry as _get_registry

    return _get_registry()


def _load_registry_file(path: Path) -> set[str]:
    try:
        from .principals import load_registry

        return _registry_ids(load_registry(path))
    except Exception:  # noqa: BLE001
        return set()


def _storage_key(key: str) -> str:
    """Match ``JsonlSessionStore.storage_key`` in nanobot 0.3.0."""
    return base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii").rstrip("=")


def _session_target_path(output_root: Path, key: str) -> Path:
    return output_root / TARGET_SESSIONS_DIR / f"{_storage_key(key)}.jsonl"


def _valid_timestamp(value: object, issues: list[dict[str, Any]], field_name: str) -> str:
    if isinstance(value, str) and value:
        try:
            datetime.fromisoformat(value)
        except ValueError:
            issues.append({"reason": "invalid_timestamp", "field": field_name})
        else:
            return value
    elif value is not None:
        issues.append({"reason": "invalid_timestamp", "field": field_name})
    return _EPOCH


def _read_cursor(
    metadata: Mapping[str, Any],
    message_count: int,
    issues: list[dict[str, Any]],
    quarantine: list[dict[str, Any]],
) -> int:
    values: list[tuple[str, object]] = [
        (name, metadata[name])
        for name in ("last_archived", "last_consolidated")
        if name in metadata
    ]
    if not values:
        return 0
    parsed: list[int] = []
    for name, value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            issues.append({"reason": "invalid_cursor", "field": name, "value": value})
            quarantine.append(
                {"line": 1, "reason": "invalid_cursor", "record": {name: value}}
            )
            return 0
        if not 0 <= value <= message_count:
            issues.append({"reason": "cursor_out_of_range", "field": name, "value": value})
            quarantine.append(
                {"line": 1, "reason": "cursor_out_of_range", "record": {name: value}}
            )
            return 0
        parsed.append(value)
    if len(set(parsed)) > 1:
        issues.append({"reason": "conflicting_cursors", "values": parsed})
        quarantine.append(
            {
                "line": 1,
                "reason": "conflicting_cursors",
                "record": {name: value for name, value in values},
            }
        )
        return 0
    return parsed[0]


def _tool_call_ids(message: Mapping[str, Any]) -> set[str] | None:
    if "tool_calls" not in message:
        return set()
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return None
    result: set[str] = set()
    for call in calls:
        if not isinstance(call, Mapping):
            return None
        call_id = call.get("id") or call.get("tool_call_id") or call.get("call_id")
        if not isinstance(call_id, str) or not call_id or call_id in result:
            return None
        result.add(call_id)
    return result


@dataclass
class _Group:
    actor: str
    messages: list[dict[str, Any]]
    source_indexes: list[int]


@dataclass
class _FilePlan:
    path: Path
    relative_path: str
    digest: str
    source_key: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    source_cursor: int
    groups: list[_Group] = field(default_factory=list)
    quarantine: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    sidecars: list[Path] = field(default_factory=list)
    message_count: int = 0


@dataclass
class MigrationPlan:
    source_root: Path
    known_actors: tuple[str, ...]
    files: list[_FilePlan]
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def target_count(self) -> int:
        return sum(len(_groups_by_actor(item.groups)) for item in self.files)

    @property
    def quarantine_count(self) -> int:
        return sum(len(item.quarantine) for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        source_files: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        quarantine: list[dict[str, Any]] = []
        for item in self.files:
            groups = [
                {
                    "actor": group.actor,
                    "message_count": len(group.messages),
                    "source_indexes": list(group.source_indexes),
                }
                for group in item.groups
            ]
            source_files.append(
                {
                    "path": item.relative_path,
                    "sha256": item.digest,
                    "source_key": item.source_key,
                    "message_count": item.message_count,
                    "cursor": item.source_cursor,
                    "groups": groups,
                    "issues": item.issues,
                    "sidecars": [str(path) for path in item.sidecars],
                }
            )
            for actor, actor_groups in _groups_by_actor(item.groups).items():
                if item.source_key is None:
                    continue
                key = make_private_session_key(actor, item.source_key)
                cursor = _group_cursor(actor_groups, item.source_cursor)
                targets.append(
                    {
                        "actor": actor,
                        "key": key,
                        "path": str(
                            Path(TARGET_SESSIONS_DIR)
                            / f"{_storage_key(key)}.jsonl"
                        ),
                        "source": item.relative_path,
                        "message_count": sum(
                            len(group.messages) for group in actor_groups
                        ),
                        "last_consolidated": cursor,
                    }
                )
            quarantine.extend(
                {
                    "source": item.relative_path,
                    **entry,
                }
                for entry in item.quarantine
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "analysis",
            "source_root": str(self.source_root),
            "known_actors": list(self.known_actors),
            "source_files": source_files,
            "targets": sorted(
                targets,
                key=lambda value: (value["key"], value["source"]),
            ),
            "quarantine": quarantine,
            "issues": self.issues,
            "summary": {
                "source_files": len(self.files),
                "targets": len(targets),
                "quarantine": len(quarantine),
            },
        }


def _groups_by_actor(groups: Iterable[_Group]) -> dict[str, list[_Group]]:
    result: dict[str, list[_Group]] = {}
    for group in groups:
        result.setdefault(group.actor, []).append(group)
    return result


def _group_cursor(groups: Iterable[_Group], source_cursor: int) -> int:
    """Count only complete saved groups before the source cursor."""
    return sum(
        len(group.messages)
        for group in groups
        if group.source_indexes and max(group.source_indexes) + 1 <= source_cursor
    )


def _quarantine_group(
    target: list[dict[str, Any]],
    group: Mapping[str, Any],
    reason: str,
) -> None:
    target.append(
        {
            "line": group.get("line"),
            "reason": reason,
            "record": copy.deepcopy(group.get("messages", [])),
        }
    )


def _analyse_messages(
    events: list[tuple[int, object, str | None]],
    known_actors: set[str],
    quarantine: list[dict[str, Any]],
) -> tuple[list[_Group], int]:
    groups: list[_Group] = []
    current: dict[str, Any] | None = None
    message_index = 0

    def close_current(reason: str | None = None) -> None:
        nonlocal current
        if current is None:
            return
        if reason is not None:
            _quarantine_group(quarantine, current, reason)
        elif current["pending"]:
            _quarantine_group(
                quarantine,
                current,
                "incomplete_tool_group",
            )
        else:
            groups.append(
                _Group(
                    actor=current["actor"],
                    messages=current["messages"],
                    source_indexes=current["indexes"],
                )
            )
        current = None

    for line_number, value, parse_error in events:
        if parse_error is not None:
            close_current()
            quarantine.append(
                {
                    "line": line_number,
                    "reason": parse_error,
                    "record": value,
                }
            )
            continue
        if not isinstance(value, Mapping):
            close_current()
            quarantine.append(
                {
                    "line": line_number,
                    "reason": "message_not_object",
                    "record": value,
                }
            )
            continue

        message = dict(value)
        role = message.get("role")
        index = message_index
        message_index += 1
        if role not in _MESSAGE_ROLES:
            close_current()
            quarantine.append(
                {
                    "line": line_number,
                    "reason": "unknown_role",
                    "record": copy.deepcopy(message),
                }
            )
            continue

        if role == "user":
            actor = message.get("actor")
            if (
                not isinstance(actor, str)
                or _canonical_principal(actor) is None
                or actor not in known_actors
            ):
                close_current()
                quarantine.append(
                    {
                        "line": line_number,
                        "reason": "unknown_user_actor",
                        "record": copy.deepcopy(message),
                    }
                )
                continue
            close_current()
            current = {
                "actor": actor,
                "messages": [copy.deepcopy(message)],
                "indexes": [index],
                "pending": set(),
                "line": line_number,
            }
            continue

        if current is None:
            quarantine.append(
                {
                    "line": line_number,
                    "reason": "orphan_auxiliary_message",
                    "record": copy.deepcopy(message),
                }
            )
            continue

        # An omitted auxiliary actor inherits the proven user owner.  An
        # explicit different (or malformed) actor makes the whole group
        # ambiguous, including the offending record.
        explicit_actor = message.get("actor")
        if "actor" in message and (
            not isinstance(explicit_actor, str)
            or explicit_actor != current["actor"]
        ):
            current["messages"].append(copy.deepcopy(message))
            current["indexes"].append(index)
            close_current("actor_mismatch")
            continue

        if role == "assistant":
            if current["pending"]:
                close_current()
                quarantine.append(
                    {
                        "line": line_number,
                        "reason": "assistant_before_tool_results",
                        "record": copy.deepcopy(message),
                    }
                )
                continue
            ids = _tool_call_ids(message)
            if ids is None:
                close_current()
                quarantine.append(
                    {
                        "line": line_number,
                        "reason": "malformed_tool_calls",
                        "record": copy.deepcopy(message),
                    }
                )
                continue
            current["messages"].append(copy.deepcopy(message))
            current["indexes"].append(index)
            current["pending"].update(ids)
            continue

        # role == tool
        tool_id = message.get("tool_call_id")
        if (
            not isinstance(tool_id, str)
            or not tool_id
            or tool_id not in current["pending"]
        ):
            close_current()
            quarantine.append(
                {
                    "line": line_number,
                    "reason": "unmatched_tool_result",
                    "record": copy.deepcopy(message),
                }
            )
            continue
        current["messages"].append(copy.deepcopy(message))
        current["indexes"].append(index)
        current["pending"].remove(tool_id)

    close_current()
    return groups, message_index


def _sidecars_for(path: Path) -> list[Path]:
    result: list[Path] = []
    for candidate in (
        path.with_suffix(".checkpoint.json"),
        path.with_name(path.name + ".checkpoint.json"),
        path.with_suffix(".checkpoint"),
    ):
        if candidate.exists() and not candidate.is_symlink() and candidate.is_file():
            result.append(candidate)
    return result


def _analyse_file(path: Path, source_root: Path, known_actors: set[str]) -> _FilePlan:
    relative = path.relative_to(source_root).as_posix()
    raw = path.read_bytes()
    digest = _sha256(raw)
    issues: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    metadata_record: dict[str, Any] | None = None
    events: list[tuple[int, object, str | None]] = []
    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            value: object = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            events.append(
                (line_number, raw_line.decode("utf-8", errors="replace"), "corrupt_json")
            )
            continue
        if isinstance(value, Mapping) and value.get("_type") == "metadata":
            if metadata_record is not None:
                quarantine.append(
                    {
                        "line": line_number,
                        "reason": "duplicate_metadata_record",
                        "record": copy.deepcopy(value),
                    }
                )
            else:
                metadata_record = dict(value)
            continue
        if isinstance(value, Mapping) and value.get("_type") in {
            "checkpoint",
            "provider_state",
            "runtime_checkpoint",
        }:
            events.append((line_number, value, "unknown_checkpoint"))
            continue
        events.append((line_number, value, None))

    if metadata_record is None:
        issues.append({"reason": "missing_metadata_record"})
        quarantine.append(
            {
                "line": None,
                "reason": "session_without_metadata",
                "record": [value for _, value, _ in events],
            }
        )
        return _FilePlan(
            path=path,
            relative_path=relative,
            digest=digest,
            source_key=None,
            metadata={},
            created_at=_EPOCH,
            updated_at=_EPOCH,
            source_cursor=0,
            quarantine=quarantine,
            issues=issues,
            sidecars=_sidecars_for(path),
            message_count=0,
        )

    source_key = metadata_record.get("key")
    if (
        not isinstance(source_key, str)
        or not source_key
        or any(char in source_key for char in "\x00\r\n")
    ):
        issues.append({"reason": "missing_session_key"})
        source_key = None

    metadata_value = metadata_record.get("metadata", {})
    metadata = copy.deepcopy(metadata_value) if isinstance(metadata_value, dict) else {}
    for key in sorted(_CHECKPOINT_KEYS):
        if key in metadata_record or key in metadata:
            checkpoint = metadata_record.get(key, metadata.get(key))
            quarantine.append(
                {
                    "line": 1,
                    "reason": "unknown_checkpoint",
                    "record": {key: copy.deepcopy(checkpoint)},
                }
            )
            metadata.pop(key, None)

    # A malformed or incomplete checkpoint must never enter target metadata.
    cursor = _read_cursor(
        metadata_record,
        sum(
            1
            for _, value, error in events
            if error is None and isinstance(value, Mapping)
        ),
        issues,
        quarantine,
    )
    created_at = _valid_timestamp(
        metadata_record.get("created_at"), issues, "created_at"
    )
    updated_at = _valid_timestamp(
        metadata_record.get("updated_at"), issues, "updated_at"
    )
    groups, message_count = _analyse_messages(events, known_actors, quarantine)
    for sidecar in _sidecars_for(path):
        quarantine.append(
            {
                "line": None,
                "reason": "unknown_checkpoint_sidecar",
                "record": sidecar.relative_to(source_root).as_posix(),
            }
        )
    if source_key is None:
        # No logical route means no safe target key; preserve all proven groups
        # in quarantine instead of guessing from a lossy filename.
        for group in groups:
            _quarantine_group(
                quarantine,
                {
                    "line": None,
                    "messages": group.messages,
                },
                "missing_session_key",
            )
        groups = []
    return _FilePlan(
        path=path,
        relative_path=relative,
        digest=digest,
        source_key=source_key,
        metadata=metadata,
        created_at=created_at,
        updated_at=updated_at,
        source_cursor=cursor,
        groups=groups,
        quarantine=quarantine,
        issues=issues,
        sidecars=_sidecars_for(path),
        message_count=message_count,
    )


def _session_files(source_root: Path) -> list[Path]:
    if source_root.is_file():
        return [source_root] if source_root.suffix == ".jsonl" else []
    return sorted(
        path
        for path in source_root.rglob("*.jsonl")
        if path.is_file() and not path.is_symlink()
    )


def _validate_source_root(source_root: Path) -> Path:
    source_root = source_root.expanduser().resolve(strict=True)
    if not source_root.is_dir() and not source_root.is_file():
        raise ValueError(f"source does not exist: {source_root}")
    return source_root


def analyze_sessions(
    source_root: Path | str,
    *,
    registry: object = None,
    known_actors: Iterable[str] | None = None,
) -> MigrationPlan:
    """Read legacy sessions and return a side-effect-free migration plan."""
    source = _validate_source_root(Path(source_root))
    root = source if source.is_dir() else source.parent
    registry_values = _registry_ids(registry) if known_actors is None else set(known_actors)
    known = {
        actor
        for actor in registry_values
        if _canonical_principal(actor) is not None
    }
    files = [_analyse_file(path, root, known) for path in _session_files(source)]
    issues = [
        {"source": item.relative_path, **issue}
        for item in files
        for issue in item.issues
    ]
    return MigrationPlan(
        source_root=source,
        known_actors=tuple(sorted(known)),
        files=files,
        issues=issues,
    )


def build_migration_plan(
    source_root: Path | str,
    *,
    registry: object = None,
    known_actors: Iterable[str] | None = None,
) -> MigrationPlan:
    """Compatibility name for callers that use the planning vocabulary."""
    return analyze_sessions(
        source_root,
        registry=registry,
        known_actors=known_actors,
    )


def _write_new_or_compare(path: Path, data: bytes) -> str:
    """Write once; never overwrite an existing result."""
    _ensure_directory(path.parent)
    if path.is_symlink():
        raise ValueError(f"unsafe output path: {path}")
    if path.exists():
        try:
            return "unchanged" if path.read_bytes() == data else "different"
        except OSError:
            return "different"
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        return _write_new_or_compare(path, data)
    return "created"


def _ensure_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"unsafe output directory: {path}")
        return
    if path.parent != path:
        _ensure_directory(path.parent)
    path.mkdir()


def _write_conflict(path: Path, data: bytes, digest: str) -> Path:
    conflict = path.parent / "conflicts" / f"{path.name}.{digest[:16]}.conflict"
    _write_new_or_compare(conflict, data)
    return conflict


def _make_source_snapshot(path: Path, data: bytes, output_root: Path, relative: str) -> str:
    target = output_root / SOURCE_SNAPSHOT_DIR / Path(relative)
    status = _write_new_or_compare(target, data)
    if status == "different":
        _write_conflict(target, data, _sha256(data))
        status = "conflict"
    try:
        os.chmod(target, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass
    return status


def _target_bytes(item: _FilePlan, actor_groups: list[_Group], actor: str) -> tuple[str, bytes]:
    assert item.source_key is not None
    key = make_private_session_key(actor, item.source_key)
    metadata = copy.deepcopy(item.metadata)
    channel, separator, chat_id = item.source_key.partition(":")
    metadata.update(
        {
            "familia_actor": actor,
            "familia_original_session_key": item.source_key,
            "familia_source_file": item.relative_path,
            "familia_source_sha256": item.digest,
        }
    )
    if separator and channel and chat_id:
        metadata["familia_original_channel"] = channel
        metadata["familia_original_chat_id"] = chat_id
    cursor = _group_cursor(actor_groups, item.source_cursor)
    metadata_line = {
        "_type": "metadata",
        "key": key,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "metadata": metadata,
        "last_archived": cursor,
        "last_consolidated": cursor,
    }
    lines = [_json_bytes(metadata_line).rstrip(b"\n")]
    for group in actor_groups:
        lines.extend(
            _json_bytes(copy.deepcopy(message)).rstrip(b"\n")
            for message in group.messages
        )
    return key, b"\n".join(lines) + b"\n"


def _quarantine_bytes(item: _FilePlan) -> bytes:
    lines = [
        _json_bytes(
            {
                "source": item.relative_path,
                "source_sha256": item.digest,
                **entry,
            }
        ).rstrip(b"\n")
        for entry in item.quarantine
    ]
    return b"\n".join(lines) + (b"\n" if lines else b"")


@dataclass
class MigrationResult:
    status: str
    output_root: Path
    created_targets: int = 0
    unchanged_targets: int = 0
    conflicting_targets: int = 0
    copied_sources: int = 0
    unchanged_sources: int = 0
    quarantined_files: int = 0
    source_changed: int = 0
    report_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "status": self.status,
            "output_root": str(self.output_root),
            "created_targets": self.created_targets,
            "unchanged_targets": self.unchanged_targets,
            "conflicting_targets": self.conflicting_targets,
            "copied_sources": self.copied_sources,
            "unchanged_sources": self.unchanged_sources,
            "quarantined_files": self.quarantined_files,
            "source_changed": self.source_changed,
            "report": str(self.report_path) if self.report_path else None,
        }


def apply_migration(plan: MigrationPlan, output_root: Path | str) -> MigrationResult:
    """Apply a previously analysed plan into an isolated output directory."""
    output = Path(output_root).expanduser().resolve(strict=False)
    source = plan.source_root.resolve(strict=False)
    if (
        output == source
        or output.is_relative_to(source)
        or source.is_relative_to(output)
    ):
        raise ValueError("migration output must be disjoint from SOURCE")
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise ValueError("migration output must be a real directory")
    output.mkdir(parents=True, exist_ok=True)
    for name in (SOURCE_SNAPSHOT_DIR, TARGET_SESSIONS_DIR, QUARANTINE_DIR):
        directory = output / name
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise ValueError(f"migration output contains unsafe directory: {directory}")
        directory.mkdir(exist_ok=True)

    result = MigrationResult(status="complete", output_root=output)
    for item in plan.files:
        try:
            current = item.path.read_bytes()
        except OSError:
            result.source_changed += 1
            continue
        if _sha256(current) != item.digest:
            result.source_changed += 1
            continue
        snapshot_status = _make_source_snapshot(
            item.path,
            current,
            output,
            item.relative_path,
        )
        if snapshot_status == "created":
            result.copied_sources += 1
        elif snapshot_status == "unchanged":
            result.unchanged_sources += 1

        for sidecar in item.sidecars:
            try:
                sidecar_data = sidecar.read_bytes()
            except OSError:
                continue
            sidecar_root = (
                plan.source_root
                if plan.source_root.is_dir()
                else plan.source_root.parent
            )
            sidecar_relative = sidecar.relative_to(sidecar_root).as_posix()
            _make_source_snapshot(sidecar, sidecar_data, output, sidecar_relative)

        for actor, actor_groups in _groups_by_actor(item.groups).items():
            key, data = _target_bytes(item, actor_groups, actor)
            target = _session_target_path(output, key)
            status = _write_new_or_compare(target, data)
            if status == "created":
                result.created_targets += 1
            elif status == "unchanged":
                result.unchanged_targets += 1
            else:
                result.conflicting_targets += 1
                _write_conflict(target, data, _sha256(data))

        if item.quarantine:
            quarantine_path = output / QUARANTINE_DIR / f"{item.relative_path}.jsonl"
            quarantine_data = _quarantine_bytes(item)
            status = _write_new_or_compare(quarantine_path, quarantine_data)
            if status == "different":
                _write_conflict(quarantine_path, quarantine_data, _sha256(quarantine_data))
            result.quarantined_files += 1

    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "familia-session-migration-provenance",
        "source_root": str(plan.source_root),
        "source_files": [
            {
                "path": item.relative_path,
                "sha256": item.digest,
                "snapshot": str(Path(SOURCE_SNAPSHOT_DIR) / item.relative_path),
                "sidecars": [
                    {
                        "path": sidecar.relative_to(
                            plan.source_root
                            if plan.source_root.is_dir()
                            else plan.source_root.parent
                        ).as_posix(),
                        "snapshot": str(
                            Path(SOURCE_SNAPSHOT_DIR)
                            / sidecar.relative_to(
                                plan.source_root
                                if plan.source_root.is_dir()
                                else plan.source_root.parent
                            )
                        ),
                        "sha256": _sha256(sidecar.read_bytes()),
                    }
                    for sidecar in item.sidecars
                ],
            }
            for item in plan.files
        ],
        "targets": plan.to_dict()["targets"],
        "quarantine": plan.to_dict()["quarantine"],
        "issues": plan.to_dict()["issues"],
    }
    report_path = output / REPORT_NAME
    report_data = _json_bytes(report, indent=2)
    if _write_new_or_compare(report_path, report_data) == "different":
        _write_conflict(report_path, report_data, _sha256(report_data))
    result.report_path = report_path
    if result.source_changed or result.conflicting_targets or result.quarantined_files:
        result.status = "partial"
    return result


def migrate_sessions(
    source_root: Path | str,
    output_root: Path | str | None = None,
    *,
    apply: bool = False,
    registry: object = None,
    known_actors: Iterable[str] | None = None,
) -> MigrationPlan | MigrationResult:
    """Analyse by default; apply only when explicitly requested."""
    plan = analyze_sessions(
        source_root,
        registry=registry,
        known_actors=known_actors,
    )
    if not apply:
        return plan
    if output_root is None:
        raise ValueError("output_root is required with apply=True")
    return apply_migration(plan, output_root)


migrate = migrate_sessions


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", "--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output", "--target", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    registry: object = _load_registry_file(args.registry) if args.registry else None
    try:
        plan = analyze_sessions(args.source, registry=registry)
        if not args.apply:
            payload: object = plan.to_dict()
        else:
            if args.output is None:
                raise ValueError("--output is required with --apply")
            payload = apply_migration(plan, args.output).to_dict()
    except (OSError, ValueError) as exc:
        print(f"session migration: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


cli = main


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI
    raise SystemExit(main())


__all__ = [
    "MigrationPlan",
    "MigrationResult",
    "analyze_sessions",
    "apply_migration",
    "build_migration_plan",
    "get_registry",
    "main",
    "migrate",
    "migrate_sessions",
]
