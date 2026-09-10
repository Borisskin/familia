"""Durability, concurrency, quarantine, and retry tests for history.jsonl."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.agent.memory import MemoryStore


def _append_batch(workspace: Path, worker: int, count: int) -> list[int]:
    store = MemoryStore(workspace)
    return [
        store.append_history(f"worker={worker} item={item}", actor=f"actor_{worker}")
        for item in range(count)
    ]


def test_concurrent_sessions_allocate_unique_contiguous_cursors(tmp_path: Path) -> None:
    workers = 8
    per_worker = 20
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_append_batch, tmp_path, worker, per_worker)
            for worker in range(workers)
        ]
    cursors = [cursor for future in futures for cursor in future.result()]

    assert sorted(cursors) == list(range(1, workers * per_worker + 1))
    entries = MemoryStore(tmp_path).read_unprocessed_history(since_cursor=0)
    assert len(entries) == workers * per_worker
    assert sorted(entry["cursor"] for entry in entries) == sorted(cursors)


def test_stale_cursor_file_cannot_skip_unpersisted_records(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.append_history("persisted") == 1
    store._cursor_file.write_text("50", encoding="utf-8")

    assert store.append_history("next") == 2
    assert [
        entry["cursor"]
        for entry in store.read_unprocessed_history(since_cursor=0)
    ] == [1, 2]


def test_failed_append_does_not_advance_cursor(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.append_history("persisted") == 1

    with patch.object(
        store,
        "_append_record_durable",
        side_effect=OSError("disk full"),
        create=True,
    ):
        with pytest.raises(OSError, match="disk full"):
            store.append_history("not persisted")

    assert store._cursor_file.read_text(encoding="utf-8") == "1"
    assert [
        entry["content"]
        for entry in store.read_unprocessed_history(since_cursor=0)
    ] == ["persisted"]


def test_corrupt_records_are_preserved_and_versioned_quarantine_is_observable(
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path)
    raw = (
        "not-json\n"
        '{"schema_version":1,"cursor":"bad","timestamp":"2026-07-10 10:00",'
        '"content":"bad cursor","provenance":{"source":"external",'
        '"idempotency_key":null}}\n'
        '{"schema_version":999,"cursor":3,"timestamp":"2026-07-10 10:01",'
        '"content":"future schema","provenance":{"source":"external",'
        '"idempotency_key":null}}\n'
        '{"cursor":4,"timestamp":"2026-07-10 10:02","content":"known legacy"}\n'
    )
    store.history_file.write_text(raw, encoding="utf-8")

    entries = store.read_unprocessed_history(since_cursor=0)
    quarantine = store.read_history_quarantine()

    assert [entry["content"] for entry in entries] == ["known legacy"]
    assert store.history_file.read_text(encoding="utf-8") == raw
    assert {record["reason"] for record in quarantine} == {
        "malformed_json",
        "non_int_cursor",
        "unknown_schema_version",
    }
    assert all(record["schema_version"] == 1 for record in quarantine)
    assert all(record["kind"] == "history_quarantine" for record in quarantine)
    assert all(record["fingerprint"] for record in quarantine)

    # Re-reading is idempotent: the durable quarantine sidecar does not grow.
    store.read_unprocessed_history(since_cursor=0)
    assert store.read_history_quarantine() == quarantine


def test_raw_archive_retry_is_idempotent_and_marked(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    messages = [
        {"role": "user", "content": "hello", "actor": "actor_a"},
        {"role": "assistant", "content": "hi"},
    ]

    store.raw_archive(messages, actor="actor_a")
    store.raw_archive(messages, actor="actor_a")

    entries = store.read_unprocessed_history(since_cursor=0)
    assert len(entries) == 1
    assert entries[0]["content"].startswith("[RAW idempotency=")
    assert entries[0]["provenance"]["source"] == "raw_consolidation_fallback"
    assert entries[0]["provenance"]["idempotency_key"]
