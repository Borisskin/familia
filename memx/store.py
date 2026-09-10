import time
import json
import math
from dataclasses import dataclass
from typing import Any

import redis
from redis_client import get_client


_redis = get_client()
VALUE_PREFIX = "memx:value:"
_EXPECTED_TS_UNSET = object()


class CorruptRecordError(RuntimeError):
    """Stored value/index bytes cannot be interpreted without guessing."""


class VersionGenerationError(RuntimeError):
    """A finite, strictly newer storage version cannot be generated."""


@dataclass(frozen=True)
class MutationResult:
    status: str
    committed: bool
    updated: bool
    retryable: bool
    version: float | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "status": self.status,
            "committed": self.committed,
            "updated": self.updated,
            "retryable": self.retryable,
            "version": self.version,
        }


def _redis_key(key: str) -> str:
    return f"{VALUE_PREFIX}{key}"


def _finite_float(
    value: Any,
    error_type: type[Exception],
    message: str,
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise error_type(message)
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise error_type(message) from exc
    if not math.isfinite(converted):
        raise error_type(message)
    if isinstance(value, int) and int(converted) != value:
        raise error_type(message)
    return converted


def _decode_record(raw: str | bytes, key: str) -> dict[str, Any]:
    try:
        record = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptRecordError(f"corrupt JSON record for '{key}'") from exc
    if not isinstance(record, dict) or "value" not in record:
        raise CorruptRecordError(f"corrupt record shape for '{key}'")
    record["ts"] = _finite_float(
        record.get("ts"),
        CorruptRecordError,
        f"corrupt record shape for '{key}'",
    )
    return record


def get_value(key):
    raw = _redis.get(_redis_key(key))
    if raw is None:
        return None
    return _decode_record(raw, key)


def _validated_expected_ts(
    expected_ts: float | None | object,
) -> float | None | object:
    if expected_ts is _EXPECTED_TS_UNSET or expected_ts is None:
        return expected_ts
    return _finite_float(
        expected_ts,
        ValueError,
        "expected_ts must be a finite number or null",
    )


def _next_version(*versions: float | int | None) -> float:
    try:
        clock_value = time.time()
    except Exception as exc:
        raise VersionGenerationError(
            "storage clock did not provide a finite version"
        ) from exc
    now = _finite_float(
        clock_value,
        VersionGenerationError,
        "storage clock did not provide a finite version",
    )
    previous = [
        _finite_float(
            version,
            VersionGenerationError,
            "stored versions cannot produce a finite successor",
        )
        for version in versions
        if version is not None
    ]
    if not previous:
        return now
    latest = max(previous)
    if now > latest:
        candidate = now
    else:
        try:
            candidate = math.nextafter(latest, math.inf)
        except (OverflowError, TypeError, ValueError) as exc:
            raise VersionGenerationError(
                "stored versions cannot produce a finite successor"
            ) from exc
    candidate = _finite_float(
        candidate,
        VersionGenerationError,
        "stored versions cannot produce a finite successor",
    )
    if candidate <= latest:
        raise VersionGenerationError(
            "stored versions cannot produce a finite successor"
        )
    return candidate


def _encode_record(value: Any, version: float) -> str:
    return json.dumps(
        {"value": value, "ts": version},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validated_index_remove(
    index_remove: dict[str, Any] | None,
) -> dict[str, str] | None:
    if index_remove is None:
        return None
    if (
        not isinstance(index_remove, dict)
        or index_remove.keys() != {"key", "name"}
    ):
        raise ValueError("index_remove must contain exactly key and name")
    key = index_remove["key"]
    name = index_remove["name"]
    if not isinstance(key, str) or not key:
        raise ValueError("index_remove.key must be non-empty")
    if not isinstance(name, str) or not name:
        raise ValueError("index_remove.name must be non-empty")
    return {"key": key, "name": name}


def delete_value(
    key: str,
    *,
    expected_ts: float | None | object = _EXPECTED_TS_UNSET,
    index_remove: dict[str, Any] | None = None,
) -> MutationResult:
    """Atomically delete a value and optional index entry with timestamp CAS."""
    expected_ts = _validated_expected_ts(expected_ts)
    index_remove = _validated_index_remove(index_remove)

    redis_key = _redis_key(key)
    index_redis_key = _redis_key(index_remove["key"]) if index_remove else None
    if index_redis_key == redis_key:
        raise ValueError("value and index keys must be distinct")
    watched = (
        (redis_key,)
        if index_redis_key is None
        else (redis_key, index_redis_key)
    )

    with _redis.pipeline() as pipe:
        while True:
            try:
                pipe.watch(*watched)
                raw = pipe.get(redis_key)
                record = (
                    _decode_record(raw, key)
                    if raw is not None
                    else None
                )
                current_ts = record["ts"] if record is not None else None

                remaining_entries: list[dict[str, Any]] | None = None
                index_changed = False
                index_ts: float | int | None = None
                if index_remove and index_redis_key:
                    index_raw = pipe.get(index_redis_key)
                    if index_raw is not None:
                        index_ts = _decode_record(
                            index_raw,
                            index_remove["key"],
                        )["ts"]
                    entries = _decode_index_entries(
                        index_raw,
                        index_remove["key"],
                    )
                    remaining_entries = [
                        item
                        for item in entries
                        if item["name"] != index_remove["name"]
                    ]
                    index_changed = len(remaining_entries) != len(entries)

                if expected_ts is not _EXPECTED_TS_UNSET and expected_ts != current_ts:
                    pipe.unwatch()
                    return MutationResult(
                        status="conflict",
                        committed=False,
                        updated=False,
                        retryable=True,
                        version=current_ts,
                    )

                if record is None and not index_changed:
                    pipe.unwatch()
                    return MutationResult(
                        status="absent",
                        committed=True,
                        updated=False,
                        retryable=False,
                        version=None,
                    )

                index_payload: str | None = None
                if index_changed and remaining_entries is not None:
                    index_payload = _encode_record(
                        json.dumps(
                            remaining_entries,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        _next_version(current_ts, index_ts),
                    )

                pipe.multi()
                if record is not None:
                    pipe.delete(redis_key)
                if index_payload is not None and index_redis_key is not None:
                    pipe.set(index_redis_key, index_payload)
                pipe.execute()
                return MutationResult(
                    status="deleted",
                    committed=True,
                    updated=True,
                    retryable=False,
                    version=current_ts,
                )
            except redis.WatchError:
                continue
            finally:
                pipe.reset()


def _decode_index_entries(raw: str | bytes | None, key: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    record = _decode_record(raw, key)
    encoded = record["value"]
    if not isinstance(encoded, str):
        raise CorruptRecordError(f"corrupt index value for '{key}'")
    try:
        decoded = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise CorruptRecordError(f"corrupt index JSON for '{key}'") from exc
    if not isinstance(decoded, list):
        raise CorruptRecordError(f"corrupt index shape for '{key}'")
    entries: list[dict[str, Any]] = []
    for item in decoded:
        if isinstance(item, str) and item:
            entries.append({"name": item, "tags": []})
            continue
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise CorruptRecordError(f"corrupt index entry for '{key}'")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise CorruptRecordError(f"corrupt index tags for '{key}'")
        entries.append({"name": item["name"], "tags": sorted(set(tags))})
    return entries


def _validated_index_update(index_update: dict[str, Any] | None) -> dict[str, Any] | None:
    if index_update is None:
        return None
    if not isinstance(index_update, dict):
        raise ValueError("index_update must be an object")
    key = index_update.get("key")
    entry = index_update.get("entry")
    maximum = index_update.get("max_entries")
    if not isinstance(key, str) or not key:
        raise ValueError("index_update.key must be non-empty")
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
        raise ValueError("index_update.entry.name must be non-empty")
    tags = entry.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
        raise ValueError("index_update.entry.tags must be strings")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1 or maximum > 4096:
        raise ValueError("index_update.max_entries is invalid")
    return {
        "key": key,
        "entry": {"name": entry["name"], "tags": sorted(set(tags))},
        "max_entries": maximum,
    }


def set_value(
    key: str,
    value: Any,
    index_update: dict[str, Any] | None = None,
    *,
    expected_ts: float | None | object = _EXPECTED_TS_UNSET,
) -> MutationResult:
    """Atomically commit a value and optional index, with optional timestamp CAS."""
    index_update = _validated_index_update(index_update)
    expected_ts = _validated_expected_ts(expected_ts)
    redis_key = _redis_key(key)
    index_redis_key = _redis_key(index_update["key"]) if index_update else None
    if index_redis_key == redis_key:
        raise ValueError("value and index keys must be distinct")
    watched = (redis_key,) if index_redis_key is None else (redis_key, index_redis_key)

    with _redis.pipeline() as pipe:
        while True:
            try:
                pipe.watch(*watched)
                prev_raw = pipe.get(redis_key)
                prev = (
                    _decode_record(prev_raw, key)
                    if prev_raw is not None
                    else None
                )
                current_ts = prev["ts"] if prev is not None else None

                entries: list[dict[str, Any]] | None = None
                index_ts: float | int | None = None
                if index_update and index_redis_key:
                    index_raw = pipe.get(index_redis_key)
                    if index_raw is not None:
                        index_ts = _decode_record(
                            index_raw,
                            index_update["key"],
                        )["ts"]
                    entries = _decode_index_entries(
                        index_raw,
                        index_update["key"],
                    )

                if expected_ts is not _EXPECTED_TS_UNSET and expected_ts != current_ts:
                    pipe.unwatch()
                    return MutationResult(
                        status="conflict",
                        committed=False,
                        updated=False,
                        retryable=True,
                        version=current_ts,
                    )

                entry: dict[str, Any] | None = None
                if index_update and entries is not None:
                    entry = index_update["entry"]
                    distinct_names = {
                        item["name"]
                        for item in entries
                    }
                    entry_exists = entry["name"] in distinct_names
                    if (
                        not entry_exists
                        and len(distinct_names) >= index_update["max_entries"]
                    ):
                        pipe.unwatch()
                        return MutationResult(
                            status="catalog_full",
                            committed=False,
                            updated=False,
                            retryable=False,
                            version=current_ts,
                        )
                    entries = [
                        item
                        for item in entries
                        if item["name"] != entry["name"]
                    ]
                    entries.append(entry)

                version = _next_version(current_ts, index_ts)
                payload = _encode_record(value, version)
                index_payload: str | None = None
                if entries is not None:
                    index_payload = _encode_record(
                        json.dumps(
                            entries,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        version,
                    )

                pipe.multi()
                pipe.set(redis_key, payload)
                if index_payload is not None and index_redis_key is not None:
                    pipe.set(index_redis_key, index_payload)
                pipe.execute()
                return MutationResult(
                    status="committed",
                    committed=True,
                    updated=True,
                    retryable=False,
                    version=version,
                )
            except redis.WatchError:
                # Retry if the key was modified between watch and execute
                continue
            finally:
                pipe.reset()
