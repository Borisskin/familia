import time
import json
from dataclasses import dataclass
from typing import Any

import redis
from redis_client import get_client


_redis = get_client()
VALUE_PREFIX = "memx:value:"
_EXPECTED_TS_UNSET = object()


class CorruptRecordError(RuntimeError):
    """Stored value/index bytes cannot be interpreted without guessing."""


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


def _decode_record(raw: str | bytes, key: str) -> dict[str, Any]:
    try:
        record = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CorruptRecordError(f"corrupt JSON record for '{key}'") from exc
    if (
        not isinstance(record, dict)
        or "value" not in record
        or not isinstance(record.get("ts"), (int, float))
        or isinstance(record.get("ts"), bool)
    ):
        raise CorruptRecordError(f"corrupt record shape for '{key}'")
    return record


def get_value(key):
    raw = _redis.get(_redis_key(key))
    if not raw:
        return None
    return _decode_record(raw, key)


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
    if (
        expected_ts is not _EXPECTED_TS_UNSET
        and expected_ts is not None
        and (
            not isinstance(expected_ts, (int, float))
            or isinstance(expected_ts, bool)
        )
    ):
        raise ValueError("expected_ts must be a number or null")
    redis_key = _redis_key(key)
    index_redis_key = _redis_key(index_update["key"]) if index_update else None
    if index_redis_key == redis_key:
        raise ValueError("value and index keys must be distinct")
    watched = (redis_key,) if index_redis_key is None else (redis_key, index_redis_key)

    with _redis.pipeline() as pipe:
        while True:
            try:
                pipe.watch(*watched)
                now = time.time()
                prev_raw = pipe.get(redis_key)
                prev: dict[str, Any] | None = None
                if prev_raw:
                    prev = _decode_record(prev_raw, key)
                current_ts = prev["ts"] if prev is not None else None
                if expected_ts is not _EXPECTED_TS_UNSET and expected_ts != current_ts:
                    pipe.unwatch()
                    return MutationResult(
                        status="conflict",
                        committed=False,
                        updated=False,
                        retryable=True,
                        version=current_ts,
                    )
                if prev is not None:
                    if now <= prev.get("ts", 0):
                        pipe.unwatch()
                        return MutationResult(
                            status="not_updated",
                            committed=False,
                            updated=False,
                            retryable=True,
                            version=current_ts,
                        )
                payload = {"value": value, "ts": now}
                index_payload: dict[str, Any] | None = None
                if index_update and index_redis_key:
                    entries = _decode_index_entries(
                        pipe.get(index_redis_key), index_update["key"]
                    )
                    entry = index_update["entry"]
                    entries = [item for item in entries if item["name"] != entry["name"]]
                    entries.append(entry)
                    entries = entries[-index_update["max_entries"]:]
                    index_payload = {
                        "value": json.dumps(
                            entries,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        "ts": now,
                    }
                pipe.multi()
                pipe.set(
                    redis_key,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                )
                if index_payload is not None and index_redis_key is not None:
                    pipe.set(
                        index_redis_key,
                        json.dumps(
                            index_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    )
                pipe.execute()
                return MutationResult(
                    status="committed",
                    committed=True,
                    updated=True,
                    retryable=False,
                    version=now,
                )
            except redis.WatchError:
                # Retry if the key was modified between watch and execute
                continue
            finally:
                pipe.reset()
