from __future__ import annotations

from pathlib import Path

import pytest


SNAPSHOT_ID = "b" * 64


def _snapshot() -> dict:
    return {
        "schema_version": "1.0.0",
        "snapshot_format_version": "1.0.0",
        "snapshot_id": SNAPSHOT_ID,
        "status": "complete",
        "state_role": "source",
        "versions": {"snapshot_schema": "1.0.0"},
    }


def _marker(target: Path) -> dict:
    return {
        "marker_version": "1.0.0",
        "purpose": "familia-memory-migration",
        "target_id": "fixture-target",
        "non_production": True,
        "filesystem_root": str(target.resolve()),
        "snapshot_id": SNAPSHOT_ID,
        "contract_version": "2.0.0",
    }


def test_migration_preflight_requires_versions_and_isolated_target(tmp_path: Path) -> None:
    from familia.memory_migration import MigrationPreflightError, validate_migration_preflight

    target = tmp_path / "isolated"
    target.mkdir(mode=0o700)
    validate_migration_preflight(_snapshot(), target, _marker(target))

    bad_snapshot = {**_snapshot(), "snapshot_format_version": "9.9.9"}
    with pytest.raises(MigrationPreflightError, match="snapshot_format_version"):
        validate_migration_preflight(bad_snapshot, target, _marker(target))

    bad_marker = {**_marker(target), "non_production": False}
    with pytest.raises(MigrationPreflightError, match="non_production"):
        validate_migration_preflight(_snapshot(), target, bad_marker)


class _Redis:
    def __init__(self) -> None:
        self.entries: dict[bytes, dict] = {}
        self.restore_calls = 0

    def command(self, name: str, *args):
        if name == "RESTORE":
            self.restore_calls += 1
            if self.restore_calls == 2:
                raise OSError("injected redis failure")
            key, ttl, dump = args
            self.entries[key] = {"type": b"string", "pttl": -1 if ttl == 0 else ttl, "dump": dump, "value": None}
            return b"OK"
        if name == "DEL":
            self.entries.pop(args[0], None)
            return 1
        raise AssertionError(name)


def test_redis_restore_fault_rolls_back_inserted_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import compare_memory_state as state

    client = _Redis()
    expected = {
        b"key-a": {"type": b"string", "pttl": -1, "dump": b"dump-a", "value": None},
        b"key-b": {"type": b"string", "pttl": -1, "dump": b"dump-b", "value": None},
    }
    monkeypatch.setattr(state, "_scan_redis", lambda current, _marker: dict(current.entries))

    with pytest.raises(OSError, match="injected redis failure"):
        state._restore_redis_entries_transactional(client, expected, b"marker")

    assert client.entries == {}


def test_restore_publication_rollback_removes_files_and_redis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import compare_memory_state as state

    target = tmp_path / "target"
    target.mkdir()
    marker = target / ".familia-memory-restore-target.json"
    marker.write_text("{}", encoding="utf-8")
    payload = target / "payload"
    state_dir = target / "state"
    payload.mkdir()
    state_dir.mkdir()
    manifest = target / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    client = _Redis()
    client.entries[b"key-a"] = {"type": b"string", "pttl": -1, "dump": b"dump-a", "value": None}
    monkeypatch.setattr(state, "_scan_redis", lambda current, _marker: dict(current.entries))

    state._rollback_restore_attempt(
        target=target,
        published_paths=[payload, state_dir, manifest],
        client=client,
        restored_keys=[b"key-a"],
        marker_key=b"marker",
    )

    assert {path.name for path in target.iterdir()} == {marker.name}
    assert client.entries == {}
