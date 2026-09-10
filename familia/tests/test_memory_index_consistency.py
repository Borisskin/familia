"""RP-050 atomic value/index mutation and corruption behavior."""

from __future__ import annotations

import inspect
import json
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pathlib import Path

import pytest


if "redis" not in sys.modules:
    redis_stub = types.ModuleType("redis")

    class _WatchError(Exception):
        pass

    class _Redis:
        @staticmethod
        def from_url(*_args, **_kwargs):
            return None

    redis_stub.WatchError = _WatchError
    redis_stub.Redis = _Redis
    sys.modules["redis"] = redis_stub

import redis


REPO_ROOT = Path(__file__).resolve().parents[2]
MEMX_ROOT = REPO_ROOT / "memx"
if str(MEMX_ROOT) not in sys.path:
    sys.path.insert(0, str(MEMX_ROOT))


class InProcessRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.versions: defaultdict[str, int] = defaultdict(int)
        self.lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self.lock:
            return self.data.get(key)

    def put(self, key: str, value: str) -> None:
        with self.lock:
            self.data[key] = value
            self.versions[key] += 1

    def pipeline(self):
        return InProcessPipeline(self)


class InProcessPipeline:
    def __init__(self, backend: InProcessRedis) -> None:
        self.backend = backend
        self.watched: dict[str, int] = {}
        self.pending: list[tuple[str, str, str | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.reset()

    def watch(self, *keys: str) -> None:
        with self.backend.lock:
            self.watched = {key: self.backend.versions[key] for key in keys}

    def get(self, key: str) -> str | None:
        return self.backend.get(key)

    def multi(self) -> None:
        return None

    def set(self, key: str, value: str) -> None:
        self.pending.append(("set", key, value))

    def delete(self, key: str) -> None:
        self.pending.append(("delete", key, None))

    def execute(self) -> list[bool]:
        with self.backend.lock:
            if any(self.backend.versions[key] != version for key, version in self.watched.items()):
                raise redis.WatchError
            for operation, key, value in self.pending:
                if operation == "delete":
                    self.backend.data.pop(key, None)
                else:
                    assert value is not None
                    self.backend.data[key] = value
                self.backend.versions[key] += 1
            return [True] * len(self.pending)

    def unwatch(self) -> None:
        self.watched = {}

    def reset(self) -> None:
        self.watched = {}
        self.pending = []


def _atomic_api():
    import store

    assert "index_update" in inspect.signature(store.set_value).parameters, (
        "store.set_value has no atomic index mutation"
    )
    error_type = getattr(store, "CorruptRecordError", None)
    assert inspect.isclass(error_type)
    return store, error_type


def _catalog_record(entries: list[dict[str, object]], *, ts: float = 5.0) -> str:
    return json.dumps(
        {
            "value": json.dumps(entries, separators=(",", ":"), sort_keys=True),
            "ts": ts,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _catalog_entries(backend: InProcessRedis, store, index_key: str):
    raw = backend.get(store._redis_key(index_key))
    if raw is None:
        return []
    return json.loads(json.loads(raw)["value"])


def _index_remove_api(store) -> None:
    assert "index_remove" in inspect.signature(store.delete_value).parameters, (
        "store.delete_value has no atomic catalog removal"
    )


def test_parallel_atomic_appends_retain_every_index_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = _atomic_api()
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    index_key = "private:alice:value:private_index"

    def write(number: int) -> None:
        result = store.set_value(
            f"private:alice:item-{number}",
            f"value-{number}",
            index_update={
                "key": index_key,
                "entry": {"name": f"item-{number}", "tags": []},
                "max_entries": 256,
            },
        )
        assert result.committed is True

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(32)))

    record = json.loads(backend.get(store._redis_key(index_key)))
    entries = json.loads(record["value"])
    assert {entry["name"] for entry in entries} == {f"item-{n}" for n in range(32)}


def test_corrupt_index_aborts_primary_write_without_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    store, error_type = _atomic_api()
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    index_key = "private:alice:value:private_index"
    corrupt = json.dumps({"value": "{broken-index", "ts": 1.0})
    backend.put(store._redis_key(index_key), corrupt)

    with pytest.raises(error_type, match="corrupt"):
        store.set_value(
            "private:alice:item",
            "value",
            index_update={
                "key": index_key,
                "entry": {"name": "item", "tags": []},
                "max_entries": 256,
            },
        )

    assert backend.get(store._redis_key("private:alice:item")) is None
    assert backend.get(store._redis_key(index_key)) == corrupt


def test_expected_ts_conflict_leaves_value_and_index_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    value_key = "private:actor_alpha:value:memory"
    index_key = "private:actor_alpha:value:private_index"
    value_before = json.dumps({"value": "before", "ts": 10.0})
    index_before = json.dumps(
        {
            "value": json.dumps([{"name": "before", "tags": []}]),
            "ts": 9.0,
        }
    )
    backend.put(store._redis_key(value_key), value_before)
    backend.put(store._redis_key(index_key), index_before)

    result = store.set_value(
        value_key,
        "after",
        expected_ts=8.0,
        index_update={
            "key": index_key,
            "entry": {"name": "after", "tags": []},
            "max_entries": 32,
        },
    )

    assert result.status == "conflict"
    assert result.committed is False
    assert result.retryable is True
    assert backend.get(store._redis_key(value_key)) == value_before
    assert backend.get(store._redis_key(index_key)) == index_before


def test_conflict_can_report_null_version_without_partial_catalog_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    value_key = "private:alice:memory:missing"
    index_key = "private:alice:value:private_index"

    result = store.set_value(
        value_key,
        "must not be written",
        expected_ts=7.0,
        index_update={
            "key": index_key,
            "entry": {"name": "memory:missing", "tags": []},
            "max_entries": 256,
        },
    )

    assert result.as_payload() == {
        "ok": True,
        "status": "conflict",
        "committed": False,
        "updated": False,
        "retryable": True,
        "version": None,
    }
    assert backend.get(store._redis_key(value_key)) is None
    assert backend.get(store._redis_key(index_key)) is None


def test_matching_expected_ts_commits_value_and_index_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    monkeypatch.setattr(store.time, "time", lambda: 11.0)
    value_key = "private:actor_alpha:value:memory"
    index_key = "private:actor_alpha:value:private_index"
    backend.put(
        store._redis_key(value_key),
        json.dumps({"value": "before", "ts": 10.0}),
    )

    result = store.set_value(
        value_key,
        "after",
        expected_ts=10.0,
        index_update={
            "key": index_key,
            "entry": {"name": "after", "tags": []},
            "max_entries": 32,
        },
    )

    assert result.status == "committed"
    assert json.loads(backend.get(store._redis_key(value_key)))["value"] == "after"
    index_record = json.loads(backend.get(store._redis_key(index_key)))
    assert json.loads(index_record["value"]) == [{"name": "after", "tags": []}]


def test_clock_collision_still_commits_with_a_new_numeric_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    monkeypatch.setattr(store.time, "time", lambda: 10.0)
    value_key = "private:alice:memory:clock-collision"
    index_key = "private:alice:value:private_index"
    backend.put(
        store._redis_key(value_key),
        json.dumps({"value": "before", "ts": 10.0}),
    )
    backend.put(
        store._redis_key(index_key),
        _catalog_record(
            [{"name": "memory:clock-collision", "tags": []}],
            ts=10.0,
        ),
    )

    result = store.set_value(
        value_key,
        "after",
        expected_ts=10.0,
        index_update={
            "key": index_key,
            "entry": {"name": "memory:clock-collision", "tags": []},
            "max_entries": 256,
        },
    )

    assert result.status == "committed"
    assert result.committed is True
    assert result.updated is True
    assert result.retryable is False
    assert isinstance(result.version, (int, float))
    assert result.version > 10.0
    assert json.loads(backend.get(store._redis_key(value_key)))["value"] == "after"


def test_full_catalog_rejects_new_name_without_evicting_or_writing_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    index_key = "private:alice:value:private_index"
    value_key = "private:alice:memory:new-fact"
    entries = [
        {"name": f"memory:fact-{number}", "tags": []}
        for number in range(256)
    ]
    catalog_before = _catalog_record(entries)
    backend.put(store._redis_key(index_key), catalog_before)

    result = store.set_value(
        value_key,
        "must not be written",
        expected_ts=None,
        index_update={
            "key": index_key,
            "entry": {"name": "memory:new-fact", "tags": []},
            "max_entries": 256,
        },
    )

    assert result.as_payload() == {
        "ok": True,
        "status": "catalog_full",
        "committed": False,
        "updated": False,
        "retryable": False,
        "version": None,
    }
    assert backend.get(store._redis_key(value_key)) is None
    assert backend.get(store._redis_key(index_key)) == catalog_before
    assert _catalog_entries(backend, store, index_key)[0]["name"] == "memory:fact-0"


def test_existing_name_updates_when_catalog_has_256_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    monkeypatch.setattr(store.time, "time", lambda: 11.0)
    index_key = "private:alice:value:private_index"
    value_key = "private:alice:memory:fact-0"
    entries = [
        {"name": f"memory:fact-{number}", "tags": []}
        for number in range(256)
    ]
    backend.put(store._redis_key(index_key), _catalog_record(entries))
    backend.put(
        store._redis_key(value_key),
        json.dumps({"value": "old", "ts": 10.0}),
    )

    result = store.set_value(
        value_key,
        "new",
        expected_ts=10.0,
        index_update={
            "key": index_key,
            "entry": {"name": "memory:fact-0", "tags": ["topic_work"]},
            "max_entries": 256,
        },
    )

    assert result.status == "committed"
    catalog = _catalog_entries(backend, store, index_key)
    assert len(catalog) == 256
    assert {"name": "memory:fact-0", "tags": ["topic_work"]} in catalog


def test_delete_removes_fact_and_exact_catalog_name_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    _index_remove_api(store)
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    index_key = "private:alice:value:private_index"
    value_key = "private:alice:memory:fact"
    backend.put(
        store._redis_key(value_key),
        json.dumps({"value": "old", "ts": 10.0}),
    )
    backend.put(
        store._redis_key(index_key),
        _catalog_record(
            [
                {"name": "memory:fact", "tags": ["topic_work"]},
                {"name": "memory:keep", "tags": []},
            ]
        ),
    )

    result = store.delete_value(
        value_key,
        expected_ts=10.0,
        index_remove={"key": index_key, "name": "memory:fact"},
    )

    assert result.as_payload() == {
        "ok": True,
        "status": "deleted",
        "committed": True,
        "updated": True,
        "retryable": False,
        "version": 10.0,
    }
    assert backend.get(store._redis_key(value_key)) is None
    assert _catalog_entries(backend, store, index_key) == [
        {"name": "memory:keep", "tags": []},
    ]


def test_delete_cleans_orphan_catalog_name_and_safe_repeat_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    _index_remove_api(store)
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    index_key = "private:alice:value:private_index"
    value_key = "private:alice:memory:orphan"
    backend.put(
        store._redis_key(index_key),
        _catalog_record([{"name": "memory:orphan", "tags": []}]),
    )
    operation = {
        "key": index_key,
        "name": "memory:orphan",
    }

    removed = store.delete_value(
        value_key,
        expected_ts=None,
        index_remove=operation,
    )
    repeated = store.delete_value(
        value_key,
        expected_ts=None,
        index_remove=operation,
    )

    assert removed.as_payload() == {
        "ok": True,
        "status": "deleted",
        "committed": True,
        "updated": True,
        "retryable": False,
        "version": None,
    }
    assert repeated.as_payload() == {
        "ok": True,
        "status": "absent",
        "committed": True,
        "updated": False,
        "retryable": False,
        "version": None,
    }
    assert _catalog_entries(backend, store, index_key) == []


def test_delete_conflict_leaves_fact_and_catalog_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    _index_remove_api(store)
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    index_key = "private:alice:value:private_index"
    value_key = "private:alice:memory:fact"
    fact_before = json.dumps({"value": "old", "ts": 10.0})
    catalog_before = _catalog_record([{"name": "memory:fact", "tags": []}])
    backend.put(store._redis_key(value_key), fact_before)
    backend.put(store._redis_key(index_key), catalog_before)

    result = store.delete_value(
        value_key,
        expected_ts=9.0,
        index_remove={"key": index_key, "name": "memory:fact"},
    )

    assert result.as_payload() == {
        "ok": True,
        "status": "conflict",
        "committed": False,
        "updated": False,
        "retryable": True,
        "version": 10.0,
    }
    assert backend.get(store._redis_key(value_key)) == fact_before
    assert backend.get(store._redis_key(index_key)) == catalog_before


def test_delete_from_full_catalog_frees_space_for_next_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _atomic_api()
    _index_remove_api(store)
    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    monkeypatch.setattr(store.time, "time", lambda: 11.0)
    index_key = "private:alice:value:private_index"
    old_key = "private:alice:memory:fact-0"
    entries = [
        {"name": f"memory:fact-{number}", "tags": []}
        for number in range(256)
    ]
    backend.put(store._redis_key(index_key), _catalog_record(entries))
    backend.put(
        store._redis_key(old_key),
        json.dumps({"value": "old", "ts": 10.0}),
    )

    removed = store.delete_value(
        old_key,
        expected_ts=10.0,
        index_remove={"key": index_key, "name": "memory:fact-0"},
    )
    created = store.set_value(
        "private:alice:memory:new-fact",
        "new",
        expected_ts=None,
        index_update={
            "key": index_key,
            "entry": {"name": "memory:new-fact", "tags": []},
            "max_entries": 256,
        },
    )

    assert removed.status == "deleted"
    assert created.status == "committed"
    catalog = _catalog_entries(backend, store, index_key)
    assert len(catalog) == 256
    assert any(item["name"] == "memory:new-fact" for item in catalog)
