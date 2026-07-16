"""Conflict-aware Familia memory migration.

The original RP-090 API below plans and rehearses repair against an isolated
snapshot.  The legacy-transition API additionally powers
``familia migrate hybrid-storage``: it classifies flat workspace files, groups
all actor-tagged history, and writes only explicit ``private:<actor>:...``
destinations through injected memX callbacks.  Ambiguous ownership never fans
out to every principal and never falls back to shared memory.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Protocol


MIGRATION_SCHEMA_VERSION = "1.0.0"
SNAPSHOT_SCHEMA_VERSION = "1.0.0"
SNAPSHOT_FORMAT_VERSION = "1.0.0"
MEMORY_CONTRACT_VERSION = "2.0.0"
MEMORY_CONTRACT_MIGRATION_KINDS = {
    "legacy-unversioned": "legacy_upgrade",
    "1.0.0": "legacy_upgrade",
    "2.0.0": "current_verify",
}
DISPOSITIONS = (
    "write",
    "skip",
    "conflict",
    "dirty_legacy",
    "llm_required",
    "quarantine_needs_review",
)
UNRESOLVED_DISPOSITIONS = {
    "conflict",
    "dirty_legacy",
    "llm_required",
    "quarantine_needs_review",
}

LEGACY_TRANSITION_SCHEMA_VERSION = "1.0.0"
_SIGNIFICANT_TOKEN = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_SIGNIFICANT_STOP_WORDS = {
    "будет",
    "были",
    "было",
    "быть",
    "всего",
    "когда",
    "который",
    "может",
    "нужно",
    "после",
    "потом",
    "потому",
    "просто",
    "своей",
    "свой",
    "также",
    "только",
    "чтобы",
    "этого",
    "этот",
    "from",
    "have",
    "that",
    "this",
    "with",
}


class MigrationError(RuntimeError):
    """Base error for safe migration refusal paths."""


class MigrationPreflightError(MigrationError):
    """Snapshot or target cannot be proven safe."""


class MigrationBlockedError(MigrationError):
    """Plan contains unresolved actions and therefore cannot be applied."""


def memory_contract_migration_kind(source_contract_version: str) -> str:
    """Select the only migration path allowed for a recognized source contract."""

    try:
        return MEMORY_CONTRACT_MIGRATION_KINDS[source_contract_version]
    except (KeyError, TypeError) as exc:
        raise MigrationPreflightError(
            f"unsupported memory contract version: {source_contract_version!r}"
        ) from exc


class MigrationTarget(Protocol):
    def get_hash(self, destination: str) -> str | None: ...

    def put_if_absent(
        self, destination: str, value: bytes, expected_sha256: str
    ) -> str: ...

    def publish_cursor(self, actor: str, cursor: int) -> str: ...


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise MigrationError("unsafe source identity")
    return path.as_posix()


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


def _append_journal(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        payload = _canonical_bytes(event) + b"\n"
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


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


def _action(
    *,
    phase: str,
    component: str,
    source_identity: str,
    raw: bytes,
    disposition: str,
    reason: str,
    actor: str | None = None,
    destination: str | None = None,
    source_order: int | None = None,
    actor_order: int | None = None,
    cursor: int | None = None,
) -> dict[str, Any]:
    if disposition not in DISPOSITIONS:
        raise MigrationError("unknown disposition")
    core = {
        "phase": phase,
        "component": component,
        "source_identity": source_identity,
        "source_sha256": _sha256(raw),
        "source_bytes": len(raw),
        "source_order": source_order,
        "actor": actor,
        "actor_order": actor_order,
        "cursor": cursor,
        "destination": destination,
        "disposition": disposition,
        "reason": reason,
        "writes": 1 if disposition == "write" else 0,
    }
    return {"action_id": _sha256(_canonical_bytes(core)), **core}


def _target_disposition(
    destination: str,
    source_sha256: str,
    target_probe: Callable[[str], str | None],
) -> tuple[str, str]:
    try:
        existing = target_probe(destination)
    except Exception:  # noqa: BLE001 - an unavailable target is a conflict, never absence
        return "conflict", "target_probe_failed"
    if existing is None:
        return "write", "target_absent"
    if existing == source_sha256:
        return "skip", "target_equal"
    return "conflict", "target_diverged"


def _heartbeat_template(raw: bytes) -> bool:
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return False
    normalized = " ".join(text.lower().split())
    return normalized in {"", "# heartbeat", "# heartbeat.md"} or (
        "heartbeat" in normalized and "add tasks" in normalized and len(normalized) < 240
    )


def build_migration_plan(
    *,
    source_root: Path,
    snapshot_manifest: dict[str, Any],
    known_actors: set[str],
    target_id: str,
    target_probe: Callable[[str], str | None],
    classifications: dict[str, Any],
) -> dict[str, Any]:
    """Build a deterministic, value-free migration plan from an isolated copy."""

    _snapshot_versions(snapshot_manifest)
    source_root = source_root.resolve(strict=True)
    actors = {actor for actor in known_actors if isinstance(actor, str) and actor.strip()}
    actions: list[dict[str, Any]] = []

    flat = (
        ("USER.md", "user_profile", "value:user_profile"),
        ("memory/MEMORY.md", "memory", "value:memory"),
        ("HEARTBEAT.md", "heartbeat", "value:heartbeat"),
    )
    for relative, component, suffix in flat:
        path = source_root.joinpath(*PurePosixPath(relative).parts)
        if not path.exists():
            actions.append(
                _action(
                    phase="phase_a",
                    component=component,
                    source_identity=relative,
                    raw=b"",
                    disposition="skip",
                    reason="source_missing",
                )
            )
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            actions.append(
                _action(
                    phase="phase_a",
                    component=component,
                    source_identity=relative,
                    raw=b"",
                    disposition="quarantine_needs_review",
                    reason="source_unreadable",
                )
            )
            continue
        if not raw.strip() or (component == "heartbeat" and _heartbeat_template(raw)):
            actions.append(
                _action(
                    phase="phase_a",
                    component=component,
                    source_identity=relative,
                    raw=raw,
                    disposition="skip",
                    reason="empty_or_template",
                )
            )
            continue
        decision = classifications.get(relative)
        if isinstance(decision, dict) and decision.get("disposition") == "write":
            actor = decision.get("actor")
            if actor not in actors:
                actions.append(
                    _action(
                        phase="phase_a",
                        component=component,
                        source_identity=relative,
                        raw=raw,
                        actor=actor if isinstance(actor, str) else None,
                        disposition="quarantine_needs_review",
                        reason="classification_actor_unknown",
                    )
                )
                continue
            destination = f"private:{actor}:{suffix}"
            disposition, reason = _target_disposition(destination, _sha256(raw), target_probe)
            actions.append(
                _action(
                    phase="phase_a",
                    component=component,
                    source_identity=relative,
                    raw=raw,
                    actor=actor,
                    destination=destination,
                    disposition=disposition,
                    reason=reason,
                )
            )
            continue
        default_disposition = {
            "user_profile": "dirty_legacy",
            "memory": "llm_required",
            "heartbeat": "quarantine_needs_review",
        }[component]
        actions.append(
            _action(
                phase="phase_a",
                component=component,
                source_identity=relative,
                raw=raw,
                disposition=default_disposition,
                reason="global_owner_ambiguous",
            )
        )

    history_path = source_root / "memory" / "history.jsonl"
    actor_orders: dict[str, int] = {}
    seen_history: set[tuple[str, str]] = set()
    history_lines = history_path.read_bytes().splitlines(keepends=True) if history_path.exists() else []
    for line_number, raw in enumerate(history_lines, start=1):
        source_identity = "memory/history.jsonl"
        parsed: dict[str, Any] | None = None
        try:
            candidate = json.loads(raw.decode("utf-8"))
            parsed = candidate if isinstance(candidate, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        valid_cursor = bool(
            parsed is not None
            and isinstance(parsed.get("cursor"), int)
            and not isinstance(parsed.get("cursor"), bool)
        )
        valid_shape = bool(
            parsed is not None
            and parsed.get("schema_version") == 1
            and valid_cursor
            and isinstance(parsed.get("content"), str)
            and isinstance(parsed.get("provenance"), dict)
        )
        if not valid_shape:
            actions.append(
                _action(
                    phase="phase_b",
                    component="history",
                    source_identity=source_identity,
                    raw=raw,
                    source_order=line_number,
                    disposition="quarantine_needs_review",
                    reason="history_malformed_or_unknown_schema",
                )
            )
            continue
        actor = parsed.get("actor")
        cursor = parsed["cursor"]
        if not isinstance(actor, str) or not actor.strip():
            actions.append(
                _action(
                    phase="phase_b",
                    component="history",
                    source_identity=source_identity,
                    raw=raw,
                    source_order=line_number,
                    cursor=cursor,
                    disposition="quarantine_needs_review",
                    reason="history_actorless",
                )
            )
            continue
        if actor not in actors:
            actions.append(
                _action(
                    phase="phase_b",
                    component="history",
                    source_identity=source_identity,
                    raw=raw,
                    source_order=line_number,
                    actor=actor,
                    cursor=cursor,
                    disposition="quarantine_needs_review",
                    reason="history_actor_unknown",
                )
            )
            continue
        raw_hash = _sha256(raw)
        destination = f"private:{actor}:history:{raw_hash}"
        identity = (actor, raw_hash)
        if identity in seen_history:
            actions.append(
                _action(
                    phase="phase_b",
                    component="history",
                    source_identity=source_identity,
                    raw=raw,
                    source_order=line_number,
                    actor=actor,
                    cursor=cursor,
                    destination=destination,
                    disposition="skip",
                    reason="duplicate_source_record",
                )
            )
            continue
        seen_history.add(identity)
        actor_orders[actor] = actor_orders.get(actor, 0) + 1
        disposition, reason = _target_disposition(destination, raw_hash, target_probe)
        actions.append(
            _action(
                phase="phase_b",
                component="history",
                source_identity=source_identity,
                raw=raw,
                source_order=line_number,
                actor=actor,
                actor_order=actor_orders[actor],
                cursor=cursor,
                destination=destination,
                disposition=disposition,
                reason=reason,
            )
        )

    pair_path = source_root / "memory" / "legacy_pair_keys.json"
    if pair_path.exists():
        try:
            pair_values = json.loads(pair_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pair_values = ["<unreadable>"]
        if not isinstance(pair_values, list):
            pair_values = ["<invalid-shape>"]
        for index, pair_value in enumerate(pair_values, start=1):
            raw = _canonical_bytes(pair_value)
            actions.append(
                _action(
                    phase="phase_b",
                    component="legacy_pair_key",
                    source_identity=f"memory/legacy_pair_keys.json#{index}",
                    raw=raw,
                    source_order=index,
                    disposition="quarantine_needs_review",
                    reason="legacy_pair_identity_ambiguous",
                )
            )

    scheduler_path = source_root / "cron" / "jobs.json"
    if scheduler_path.exists():
        try:
            scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
            jobs = scheduler.get("jobs", []) if isinstance(scheduler, dict) else []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            jobs = [{"id": "unreadable", "payload": {}}]
        for index, job in enumerate(jobs, start=1):
            if not isinstance(job, dict):
                job = {"id": f"invalid-{index}", "payload": {}}
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            target_actor = payload.get("targetActor", payload.get("target_actor"))
            known_target = isinstance(target_actor, str) and target_actor in actors
            actions.append(
                _action(
                    phase="phase_b",
                    component="scheduler",
                    source_identity=f"cron/jobs.json#{job.get('id', index)}",
                    raw=_canonical_bytes(job),
                    source_order=index,
                    actor=target_actor if isinstance(target_actor, str) else None,
                    disposition="skip" if known_target else "quarantine_needs_review",
                    reason="scheduler_target_explicit" if known_target else "scheduler_target_ambiguous",
                )
            )

    dream_cursor_path = source_root / "memory" / ".dream_cursor"
    if dream_cursor_path.exists():
        dream_cursor_raw = dream_cursor_path.read_bytes()
        try:
            dream_cursor_observed = dream_cursor_raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            dream_cursor_observed = "<non-utf8>"
    else:
        dream_cursor_raw = b""
        dream_cursor_observed = None

    counts = {name: 0 for name in DISPOSITIONS}
    for action in actions:
        counts[action["disposition"]] += 1
    migration_core = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "snapshot_id": snapshot_manifest["snapshot_id"],
        "target_id": target_id,
        "actions": actions,
    }
    migration_id = _sha256(_canonical_bytes(migration_core))
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration_id": migration_id,
        "created_at": _utc_now(),
        "status": (
            "blocked_needs_review"
            if any(action["disposition"] in UNRESOLVED_DISPOSITIONS for action in actions)
            else "planned"
        ),
        "dry_run": True,
        "source": {
            "snapshot_id": snapshot_manifest["snapshot_id"],
            "snapshot_schema_version": snapshot_manifest["schema_version"],
            "snapshot_format_version": snapshot_manifest["snapshot_format_version"],
            "contract_version": MEMORY_CONTRACT_VERSION,
            "dream_cursor": {
                "observed": dream_cursor_observed,
                "sha256": _sha256(dream_cursor_raw),
                "used_for_selection": False,
            },
        },
        "target": {"target_id": target_id, "isolated": True},
        "actions": actions,
        "summary": counts,
    }


def load_action_value(source_root: Path, action: dict[str, Any]) -> bytes:
    """Reload an action value from the isolated source and verify its hash."""

    if action.get("disposition") != "write":
        raise MigrationError("only write actions have loadable values")
    identity = _safe_relative(str(action["source_identity"]).split("#", 1)[0])
    path = source_root.resolve(strict=True).joinpath(*PurePosixPath(identity).parts)
    if action["component"] == "history":
        order = action.get("source_order")
        if not isinstance(order, int) or order < 1:
            raise MigrationError("history source order invalid")
        lines = path.read_bytes().splitlines(keepends=True)
        if order > len(lines):
            raise MigrationError("history source order missing")
        value = lines[order - 1]
    else:
        value = path.read_bytes()
    if _sha256(value) != action["source_sha256"] or len(value) != action["source_bytes"]:
        raise MigrationError("source changed after planning")
    return value


def _journal_state(path: Path, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return state
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("migration_id") != plan["migration_id"] or event.get("snapshot_id") != plan["source"]["snapshot_id"]:
            raise MigrationError("journal identity mismatch")
        action_id = event.get("action_id")
        if isinstance(action_id, str):
            state[action_id] = event
    return state


def _event(plan: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration_id": plan["migration_id"],
        "snapshot_id": plan["source"]["snapshot_id"],
        "recorded_at": _utc_now(),
        **fields,
    }


def apply_migration_plan(
    plan: dict[str, Any],
    target: MigrationTarget,
    journal_path: Path,
    value_loader: Callable[[dict[str, Any]], bytes],
) -> dict[str, Any]:
    """Apply a fully resolved plan with append-only per-action journal events."""

    unresolved = [
        action for action in plan.get("actions", [])
        if action.get("disposition") in UNRESOLVED_DISPOSITIONS
    ]
    if unresolved:
        raise MigrationBlockedError(f"unresolved migration actions: {len(unresolved)}")
    state = _journal_state(journal_path, plan)
    failed_actors: set[str] = set()
    failed_actions: set[str] = set()

    for action in plan["actions"]:
        action_id = action["action_id"]
        actor = action.get("actor")
        if actor in failed_actors:
            continue
        if action["disposition"] != "write":
            if action_id not in state:
                event = _event(
                    plan,
                    action_id=action_id,
                    actor=actor,
                    component=action["component"],
                    status="not_applied",
                    disposition=action["disposition"],
                )
                _append_journal(journal_path, event)
                state[action_id] = event
            continue
        destination = action["destination"]
        prior = state.get(action_id)
        if prior and prior.get("status") == "committed":
            if target.get_hash(destination) == action["source_sha256"]:
                continue
            event = _event(
                plan,
                action_id=action_id,
                actor=actor,
                component=action["component"],
                status="failed",
                reason="committed_target_diverged",
            )
            _append_journal(journal_path, event)
            state[action_id] = event
            failed_actions.add(action_id)
            if actor:
                failed_actors.add(actor)
            continue
        try:
            value = value_loader(action)
            result = target.put_if_absent(destination, value, action["source_sha256"])
            if result not in {"written", "equal"}:
                raise MigrationError("target conflict")
        except Exception as exc:  # noqa: BLE001 - failure is journaled and retryable
            event = _event(
                plan,
                action_id=action_id,
                actor=actor,
                component=action["component"],
                status="failed",
                reason=type(exc).__name__,
            )
            _append_journal(journal_path, event)
            state[action_id] = event
            failed_actions.add(action_id)
            if actor:
                failed_actors.add(actor)
            continue
        event = _event(
            plan,
            action_id=action_id,
            actor=actor,
            component=action["component"],
            status="committed",
            result=result,
            destination_sha256=action["source_sha256"],
        )
        _append_journal(journal_path, event)
        state[action_id] = event

    history_by_actor: dict[str, list[dict[str, Any]]] = {}
    for action in plan["actions"]:
        if (
            action["component"] == "history"
            and isinstance(action.get("actor"), str)
            and isinstance(action.get("actor_order"), int)
        ):
            history_by_actor.setdefault(action["actor"], []).append(action)
    for actor, records in history_by_actor.items():
        if actor in failed_actors:
            continue
        records.sort(key=lambda value: value["actor_order"])
        if [record["actor_order"] for record in records] != list(range(1, len(records) + 1)):
            continue
        durable = True
        for record in records:
            if record["disposition"] == "write":
                current = state.get(record["action_id"])
                if not current or current.get("status") != "committed":
                    durable = False
                    break
            if target.get_hash(record["destination"]) != record["source_sha256"]:
                durable = False
                break
        if not durable:
            continue
        cursor = max(record["cursor"] for record in records)
        cursor_action_id = _sha256(f"{plan['migration_id']}:{actor}:cursor:{cursor}".encode())
        prior = state.get(cursor_action_id)
        if prior and prior.get("status") == "cursor_published":
            continue
        result = target.publish_cursor(actor, cursor)
        if result in {"written", "equal"}:
            event = _event(
                plan,
                action_id=cursor_action_id,
                actor=actor,
                component="history_cursor",
                status="cursor_published",
                cursor=cursor,
                result=result,
            )
            _append_journal(journal_path, event)
            state[cursor_action_id] = event
        else:
            failed_actions.add(cursor_action_id)

    all_writes_durable = all(
        target.get_hash(action["destination"]) == action["source_sha256"]
        for action in plan["actions"]
        if action["disposition"] == "write"
    )
    status_value = "complete" if all_writes_durable and not failed_actions else "partial"
    _append_journal(
        journal_path,
        _event(
            plan,
            action_id=None,
            actor=None,
            component="migration",
            status="run_completed",
            result=status_value,
        ),
    )
    return {
        "status": status_value,
        "failed_actors": sorted(failed_actors),
        "failed_actions": sorted(failed_actions),
    }


def _memory_text(value: Any) -> str:
    """Normalize a decoded memX value without silently discarding structure."""

    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _significant_tokens(value: str) -> set[str]:
    return {
        token
        for token in _SIGNIFICANT_TOKEN.findall(value.casefold())
        if token not in _SIGNIFICANT_STOP_WORDS
    }


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


def _history_marker(actor: str, source_sha256: str) -> str:
    return (
        "familia-legacy-history-v1 "
        f"actor={actor} source_sha256={source_sha256}"
    )


def _read_transition_history(
    workspace: Path,
    known_actors: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Read legacy history while preserving every valid recorded actor.

    Schema version selects the supported record layout; it never changes
    identity.  Deployment-owner fallback is reserved for genuinely unscoped
    legacy files, not actor-tagged history rows.
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
        actor = record.get("actor")
        if not isinstance(actor, str) or not actor.strip():
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


def _dirty_candidate(
    text: str,
    history_groups: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    source_tokens = _significant_tokens(text)
    if not source_tokens:
        return None
    best: dict[str, Any] | None = None
    for actor, records in history_groups.items():
        actor_tokens = _significant_tokens(
            "\n".join(str(record.get("content") or "") for record in records)
        )
        common = source_tokens & actor_tokens
        union = source_tokens | actor_tokens
        jaccard = (len(common) / len(union)) if union else 0.0
        candidate = {
            "actor": actor,
            "common_significant_tokens": len(common),
            "jaccard": round(jaccard, 6),
        }
        if best is None or (
            candidate["common_significant_tokens"], candidate["jaccard"]
        ) > (best["common_significant_tokens"], best["jaccard"]):
            best = candidate
    if best and best["common_significant_tokens"] >= 10 and best["jaccard"] >= 0.10:
        return best
    return None


def _transition_file_action(
    *,
    workspace: Path,
    relative: str,
    component: str,
    destination_suffix: str,
    owner: str | None,
    known_actors: set[str],
    history_groups: dict[str, list[dict[str, Any]]],
    get_value: Callable[[str], Any],
) -> dict[str, Any]:
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    base = {
        "phase": "files",
        "component": component,
        "source": relative,
        "destination": None,
        "actor": None,
        "candidate_actor": None,
    }
    if not path.exists():
        return {**base, "disposition": "skip", "reason": "source_missing"}
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError):
        return {
            **base,
            "disposition": "quarantine_needs_review",
            "reason": "source_unreadable",
        }
    base.update({"source_sha256": _sha256(raw), "source_bytes": len(raw)})
    if not text.strip() or (component == "heartbeat" and _heartbeat_template(raw)):
        return {**base, "disposition": "skip", "reason": "empty_or_template"}

    if component in {"user_profile", "memory"}:
        dirty = _dirty_candidate(text, history_groups)
        owner_history = history_groups.get(owner or "", [])
        same_owner_v0_memory = bool(
            component == "memory"
            and dirty is not None
            and dirty["actor"] == owner
            and owner_history
            and all(
                isinstance(record.get("provenance"), dict)
                and record["provenance"].get("source") == "legacy_history_v0"
                for record in owner_history
            )
        )
        if dirty is not None and not same_owner_v0_memory:
            return {
                **base,
                "disposition": "dirty_legacy",
                "reason": "actor_history_overlap",
                "candidate_actor": dirty["actor"],
                "common_significant_tokens": dirty["common_significant_tokens"],
                "jaccard": dirty["jaccard"],
            }

    if owner is None:
        return {
            **base,
            "disposition": "quarantine_needs_review",
            "reason": "legacy_owner_ambiguous",
        }
    if owner not in known_actors:
        return {
            **base,
            "actor": owner,
            "disposition": "quarantine_needs_review",
            "reason": "legacy_owner_unknown",
        }

    destination = f"private:{owner}:{destination_suffix}"
    base.update({"actor": owner, "destination": destination})
    try:
        existing = _memory_text(get_value(destination))
    except Exception as exc:  # noqa: BLE001 - memX/auth failures are systemic
        raise MigrationError("target probe failed") from exc
    if not existing.strip():
        return {**base, "disposition": "write", "reason": "target_absent"}
    if existing == text:
        return {**base, "disposition": "skip", "reason": "target_equal"}
    return {**base, "disposition": "conflict", "reason": "target_diverged"}


def build_legacy_transition_plan(
    *,
    workspace: Path,
    known_actors: set[str],
    get_value: Callable[[str], Any],
    legacy_owner: str | None = None,
) -> dict[str, Any]:
    """Plan the operational legacy-files/history transition without fan-out.

    Flat files have no trustworthy owner in a multi-principal installation.
    They are therefore written only when an explicit ``legacy_owner`` is
    supplied (or the installation contains exactly one known actor). History
    rows already carry ownership and are grouped for LLM consolidation into
    the actor's private long-term memory.
    """

    workspace = workspace.resolve(strict=True)
    actors = {
        actor.strip()
        for actor in known_actors
        if isinstance(actor, str) and actor.strip()
    }
    owner = legacy_owner.strip() if isinstance(legacy_owner, str) and legacy_owner.strip() else None
    if owner is None and len(actors) == 1:
        owner = next(iter(actors))

    history_groups, history_issues = _read_transition_history(workspace, actors)
    actions = [
        _transition_file_action(
            workspace=workspace,
            relative="USER.md",
            component="user_profile",
            destination_suffix="value:user_profile",
            owner=owner,
            known_actors=actors,
            history_groups=history_groups,
            get_value=get_value,
        ),
        _transition_file_action(
            workspace=workspace,
            relative="memory/MEMORY.md",
            component="memory",
            destination_suffix="value:memory",
            owner=owner,
            known_actors=actors,
            history_groups=history_groups,
            get_value=get_value,
        ),
        _transition_file_action(
            workspace=workspace,
            relative="HEARTBEAT.md",
            component="heartbeat",
            destination_suffix="value:heartbeat",
            owner=owner,
            known_actors=actors,
            history_groups=history_groups,
            get_value=get_value,
        ),
    ]

    for actor, records in sorted(history_groups.items()):
        source_sha256 = _history_source_digest(records)
        destination = f"private:{actor}:value:memory"
        marker = _history_marker(actor, source_sha256)
        try:
            existing = _memory_text(get_value(destination))
        except Exception as exc:  # noqa: BLE001 - memX/auth failures are systemic
            raise MigrationError("target probe failed") from exc
        if marker in existing:
            disposition, reason = "skip", "history_already_imported"
        else:
            disposition, reason = "llm_required", "history_requires_consolidation"
        actions.append(
            {
                "phase": "history",
                "component": "history",
                "source": "memory/history.jsonl",
                "source_sha256": source_sha256,
                "actor": actor,
                "source_actors": sorted(
                    {
                        (
                            record.get("provenance", {}).get("legacy_actor")
                            if isinstance(record.get("provenance"), dict)
                            else None
                        )
                        or record["actor"]
                        for record in records
                    }
                ),
                "cursors": [record["cursor"] for record in records],
                "record_count": len(records),
                "destination": destination,
                "disposition": disposition,
                "reason": reason,
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
                "actor": issue["actor"],
                "cursor": issue["cursor"],
                "destination": None,
                "disposition": "skip_warning",
                "reason": issue["reason"],
            }
        )

    counts: dict[str, int] = {}
    for action in actions:
        counts[action["disposition"]] = counts.get(action["disposition"], 0) + 1
    unresolved = sum(
        counts.get(name, 0)
        for name in ("conflict", "quarantine_needs_review")
    )
    warnings = sum(counts.get(name, 0) for name in ("skip_warning", "dirty_legacy"))
    return {
        "schema_version": LEGACY_TRANSITION_SCHEMA_VERSION,
        "workspace": str(workspace),
        "known_actors": sorted(actors),
        "legacy_owner": owner,
        "dry_run": True,
        "status": (
            "needs_review"
            if unresolved
            else "ready_with_warnings"
            if warnings
            else "ready"
        ),
        "actions": actions,
        "summary": counts,
        "warnings": warnings,
    }


def _legacy_file_destination(workspace: Path, component: str) -> Path:
    name = {
        "user_profile": "USER.md",
        "memory": "MEMORY.md",
        "heartbeat": "HEARTBEAT.md",
    }[component]
    return workspace / "legacy" / name


def _move_applied_legacy_file(workspace: Path, action: dict[str, Any]) -> None:
    source = workspace.joinpath(*PurePosixPath(action["source"]).parts)
    destination = _legacy_file_destination(workspace, action["component"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != source.read_bytes():
            raise MigrationError("legacy destination conflict")
        source.unlink()
        return
    os.replace(source, destination)


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
    set_value: Callable[[str, str], None],
    consolidate_history: Callable[
        [str, list[dict[str, Any]], str], Awaitable[str]
    ],
) -> dict[str, Any]:
    """Apply only safe plan actions and publish Dream cursor last.

    ``history.jsonl`` is never deleted or moved. Actor-owned rows are sent to
    the supplied LLM consolidator and merged into ``private:<actor>:value:memory``.
    Any failed actor keeps the global Dream cursor unchanged so the transition
    remains retryable.
    """

    workspace = workspace.resolve(strict=True)
    if plan.get("schema_version") != LEGACY_TRANSITION_SCHEMA_VERSION:
        raise MigrationBlockedError("legacy transition schema mismatch")
    if plan.get("workspace") != str(workspace):
        raise MigrationBlockedError("legacy transition workspace mismatch")

    file_suffixes = {
        "user_profile": "value:user_profile",
        "memory": "value:memory",
        "heartbeat": "value:heartbeat",
    }
    for action in plan.get("actions", []):
        destination = action.get("destination")
        if destination is None:
            continue
        actor = action.get("actor")
        expected: str | None = None
        if action.get("phase") == "files" and action.get("component") in file_suffixes:
            expected = f"private:{actor}:{file_suffixes[action['component']]}"
        elif action.get("phase") == "history" and action.get("component") == "history":
            expected = f"private:{actor}:value:memory"
        if not isinstance(actor, str) or not actor or destination != expected:
            raise MigrationBlockedError("non-private or mismatched migration destination")

    actors = set(plan.get("known_actors") or [])
    history_groups, _current_history_issues = _read_transition_history(workspace, actors)
    failed_actions: list[str] = []
    failed_actors: set[str] = set()
    written_keys: list[str] = []
    applied_actions = 0
    fatal_failure: str | None = None

    for action in plan.get("actions", []):
        file_is_ready = action.get("disposition") == "write" or (
            action.get("disposition") == "skip"
            and action.get("reason") == "target_equal"
        )
        if action.get("phase") != "files" or not file_is_ready:
            continue
        source = workspace.joinpath(*PurePosixPath(action["source"]).parts)
        try:
            raw = source.read_bytes()
            if _sha256(raw) != action.get("source_sha256"):
                raise MigrationError("source changed after dry-run")
            text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            destination = str(action["destination"])
            existing = _memory_text(get_value(destination))
            if existing.strip() and existing != text:
                raise MigrationError("target changed after dry-run")
            if existing != text:
                set_value(destination, text)
                if _memory_text(get_value(destination)) != text:
                    raise MigrationError("target write not durable")
                written_keys.append(destination)
            _move_applied_legacy_file(workspace, action)
            applied_actions += 1
        except Exception as exc:  # noqa: BLE001 - result must remain actionable
            fatal_failure = f"{action.get('component')}:{type(exc).__name__}"
            failed_actions.append(fatal_failure)
            break

    history_actions = [
        action
        for action in plan.get("actions", [])
        if action.get("phase") == "history"
        and action.get("component") == "history"
        and isinstance(action.get("actor"), str)
        and isinstance(action.get("cursors"), list)
    ]
    completed_history_actors: set[str] = set()
    max_history_cursor: int | None = None
    for action in ([] if fatal_failure else history_actions):
        actor = action["actor"]
        records = history_groups.get(actor, [])
        try:
            if not records or _history_source_digest(records) != action.get("source_sha256"):
                raise MigrationError("history changed after dry-run")
            cursors = [record["cursor"] for record in records]
            if cursors != action.get("cursors"):
                raise MigrationError("history cursor set changed after dry-run")
            destination = str(action["destination"])
            existing = _memory_text(get_value(destination))
            marker = _history_marker(actor, action["source_sha256"])
            if marker not in existing:
                if action.get("disposition") != "llm_required":
                    raise MigrationError("history action is not approved for consolidation")
                consolidated = (await consolidate_history(actor, records, existing)).strip()
                if not consolidated:
                    raise MigrationError("history consolidation returned empty memory")
                cursor_list = ",".join(str(cursor) for cursor in cursors)
                imported_at = _utc_now()
                block = (
                    "## Imported from history.jsonl\n\n"
                    f"<!-- source=history.jsonl actor={actor} cursors={cursor_list} "
                    f"migrated_at={imported_at} {marker} -->\n\n"
                    f"{consolidated}\n"
                )
                merged = existing.rstrip() + ("\n\n" if existing.strip() else "") + block
                set_value(destination, merged)
                if _memory_text(get_value(destination)) != merged:
                    raise MigrationError("history memory write not durable")
                written_keys.append(destination)
            completed_history_actors.add(actor)
            max_history_cursor = max(max_history_cursor or 0, max(cursors))
            applied_actions += 1
        except Exception:  # noqa: BLE001 - actor remains retryable, details stay non-sensitive
            failed_actors.add(actor)
            fatal_failure = f"history:{actor}"
            failed_actions.append(fatal_failure)
            break

    unresolved_history = any(
        action.get("phase") == "history"
        and action.get("disposition") in {"conflict", "quarantine_needs_review"}
        for action in plan.get("actions", [])
    )
    all_history_complete = bool(history_actions) and (
        completed_history_actors == {action["actor"] for action in history_actions}
        and not failed_actors
        and not unresolved_history
    )
    if all_history_complete and max_history_cursor is not None:
        _write_dream_cursor(workspace, max_history_cursor)

    unresolved = [
        action
        for action in plan.get("actions", [])
        if action.get("disposition") in {
            "conflict",
            "quarantine_needs_review",
        }
    ]
    warnings = sum(
        action.get("disposition") in {"skip_warning", "dirty_legacy"}
        for action in plan.get("actions", [])
    )
    if fatal_failure:
        status_value = "fatal"
    elif unresolved:
        status_value = "partial" if applied_actions else "needs_review"
    elif warnings:
        status_value = "success_with_warnings"
    else:
        status_value = "success"
    return {
        "status": status_value,
        "applied_actions": applied_actions,
        "written_keys": sorted(set(written_keys)),
        "failed_actors": sorted(failed_actors),
        "failed_actions": sorted(failed_actions),
        "fatal_failure": fatal_failure,
        "needs_review": len(unresolved),
        "warnings": warnings,
        "dream_cursor_updated": bool(all_history_complete and max_history_cursor is not None),
    }


class IsolatedFileTarget:
    """Atomic local target used only inside a marked isolated restore root."""

    def __init__(self, root: Path):
        self.root = root
        self.values = root / "migration-state" / "values"
        self.cursors = root / "migration-state" / "cursors"
        self.values.mkdir(parents=True, exist_ok=True)
        self.cursors.mkdir(parents=True, exist_ok=True)
        os.chmod(self.values.parent, 0o700)
        os.chmod(self.values, 0o700)
        os.chmod(self.cursors, 0o700)

    @staticmethod
    def _name(value: str) -> str:
        return _sha256(value.encode("utf-8"))

    def _path(self, destination: str) -> Path:
        return self.values / f"{self._name(destination)}.json"

    def _load(self, destination: str) -> dict[str, Any] | None:
        path = self._path(destination)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("destination") != destination:
            raise MigrationError("isolated target identity mismatch")
        return value

    def get_hash(self, destination: str) -> str | None:
        value = self._load(destination)
        return value.get("sha256") if value else None

    def put_if_absent(self, destination: str, value: bytes, expected_sha256: str) -> str:
        if _sha256(value) != expected_sha256:
            raise MigrationError("target input hash mismatch")
        existing = self._load(destination)
        if existing:
            return "equal" if existing.get("sha256") == expected_sha256 else "conflict"
        record = {
            "destination": destination,
            "sha256": expected_sha256,
            "value_base64": base64.b64encode(value).decode("ascii"),
        }
        _write_private_atomic(self._path(destination), _canonical_bytes(record) + b"\n")
        return "written"

    def publish_cursor(self, actor: str, cursor: int) -> str:
        path = self.cursors / f"{self._name(actor)}.json"
        current = None
        if path.exists():
            current_value = json.loads(path.read_text(encoding="utf-8"))
            if current_value.get("actor") != actor:
                return "conflict"
            current = current_value.get("cursor")
        if isinstance(current, int) and current > cursor:
            return "conflict"
        if current == cursor:
            return "equal"
        _write_private_atomic(path, _canonical_bytes({"actor": actor, "cursor": cursor}) + b"\n")
        return "written"


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
    parser = argparse.ArgumentParser(description="Plan/rehearse isolated Familia memory repair")
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
        classifications = _load_json(args.classifications) if args.classifications else {}
        target = IsolatedFileTarget(target_root)
        plan = build_migration_plan(
            source_root=source_root,
            snapshot_manifest=snapshot,
            known_actors=_load_known_actors(source_root),
            target_id=marker["target_id"],
            target_probe=target.get_hash,
            classifications=classifications,
        )
        _write_private_atomic(args.manifest, _canonical_bytes(plan) + b"\n")
        result: dict[str, Any] = {"status": "dry_run", "migration_id": plan["migration_id"]}
        if args.apply:
            result = apply_migration_plan(
                plan,
                target,
                args.journal,
                lambda action: load_action_value(source_root, action),
            )
            result["migration_id"] = plan["migration_id"]
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(
                f"migration={result['status']} id={plan['migration_id']} "
                f"actions={len(plan['actions'])} unresolved="
                f"{sum(plan['summary'][name] for name in UNRESOLVED_DISPOSITIONS)}"
            )
        return 0 if result["status"] in {"dry_run", "complete"} else 1
    except (MigrationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"migration=refused reason={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
