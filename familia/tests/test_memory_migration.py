from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


SNAPSHOT_ID = "a" * 64


def _snapshot() -> dict:
    return {
        "schema_version": "1.0.0",
        "snapshot_format_version": "1.0.0",
        "snapshot_id": SNAPSHOT_ID,
        "status": "complete",
        "state_role": "source",
        "versions": {"snapshot_schema": "1.0.0"},
    }


def _write_history(root: Path, records: list[bytes]) -> None:
    path = root / "memory" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(records))


def _record(cursor: int, actor: str | None, content: str) -> bytes:
    value = {
        "schema_version": 1,
        "cursor": cursor,
        "timestamp": f"2026-07-10 10:0{cursor}",
        "content": content,
        "provenance": {"source": "runtime_history", "idempotency_key": None},
    }
    if actor is not None:
        value["actor"] = actor
    return json.dumps(value, sort_keys=True).encode("utf-8") + b"\n"


def test_plan_never_fans_out_and_reads_all_history(tmp_path: Path) -> None:
    from familia.memory_migration import build_migration_plan

    source = tmp_path / "source"
    (source / "memory").mkdir(parents=True)
    (source / "cron").mkdir()
    (source / "USER.md").write_text("Alice and Bob mixed profile", encoding="utf-8")
    (source / "memory" / "MEMORY.md").write_text(
        "mixed household facts with unknown owner", encoding="utf-8"
    )
    (source / "HEARTBEAT.md").write_text("# HEARTBEAT.md\n", encoding="utf-8")
    (source / "memory" / ".dream_cursor").write_text("999", encoding="utf-8")

    alice = _record(1, "alice", "alice fact")
    records = [
        alice,
        b"{malformed json\n",
        _record(2, None, "actorless fact"),
        _record(3, "unknown", "unknown actor fact"),
        _record(4, "bob", "bob fact"),
        alice,
    ]
    _write_history(source, records)
    (source / "memory" / "legacy_pair_keys.json").write_text(
        json.dumps(["pair:alice_bob_carol:value:legacy"]), encoding="utf-8"
    )
    (source / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "legacy-job",
                        "payload": {"createdBy": "alice", "message": "remind"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def _probe(destination: str) -> str | None:
        if destination.startswith("private:bob:history:"):
            return "f" * 64
        return None

    plan = build_migration_plan(
        source_root=source,
        snapshot_manifest=_snapshot(),
        known_actors={"alice", "bob"},
        target_id="isolated-test",
        target_probe=_probe,
        classifications={},
    )

    dispositions = {action["disposition"] for action in plan["actions"]}
    assert {
        "write",
        "skip",
        "conflict",
        "dirty_legacy",
        "llm_required",
        "quarantine_needs_review",
    } <= dispositions

    phase_a = [action for action in plan["actions"] if action["phase"] == "phase_a"]
    assert not any(
        action["component"] in {"user_profile", "memory"}
        and action["destination"] is not None
        for action in phase_a
    )
    assert not any(
        action["source_identity"] in {"USER.md", "memory/MEMORY.md"}
        and action["actor"] in {"alice", "bob"}
        for action in phase_a
    )

    history = [action for action in plan["actions"] if action["component"] == "history"]
    assert len(history) == len(records)
    assert plan["source"]["dream_cursor"]["observed"] == "999"
    assert plan["source"]["dream_cursor"]["used_for_selection"] is False
    assert all(action["source_sha256"] for action in history)
    assert all(
        action["writes"] == 0
        for action in history
        if action["disposition"] in {"conflict", "quarantine_needs_review", "skip"}
    )
    assert any(
        action["component"] == "legacy_pair_key"
        and action["disposition"] == "quarantine_needs_review"
        for action in plan["actions"]
    )
    assert any(
        action["component"] == "scheduler"
        and action["disposition"] == "quarantine_needs_review"
        for action in plan["actions"]
    )


class _Target:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.cursors: dict[str, int] = {}
        self.put_count: dict[str, int] = {}
        self.fail_once: set[str] = set()

    def get_hash(self, destination: str) -> str | None:
        value = self.values.get(destination)
        return hashlib.sha256(value).hexdigest() if value is not None else None

    def put_if_absent(self, destination: str, value: bytes, expected_sha256: str) -> str:
        if destination in self.fail_once:
            self.fail_once.remove(destination)
            raise OSError("injected target failure")
        existing = self.values.get(destination)
        if existing is not None:
            return "equal" if hashlib.sha256(existing).hexdigest() == expected_sha256 else "conflict"
        self.values[destination] = value
        self.put_count[destination] = self.put_count.get(destination, 0) + 1
        return "written"

    def publish_cursor(self, actor: str, cursor: int) -> str:
        current = self.cursors.get(actor)
        if current is not None and current > cursor:
            return "conflict"
        self.cursors[actor] = cursor
        return "written" if current != cursor else "equal"


def test_apply_is_resumable_idempotent_and_cursor_safe(tmp_path: Path) -> None:
    from familia.memory_migration import apply_migration_plan, build_migration_plan, load_action_value

    source = tmp_path / "source"
    _write_history(
        source,
        [
            _record(1, "alice", "a1"),
            _record(2, "bob", "b1"),
            _record(3, "alice", "a2"),
        ],
    )
    plan = build_migration_plan(
        source_root=source,
        snapshot_manifest=_snapshot(),
        known_actors={"alice", "bob"},
        target_id="isolated-test",
        target_probe=lambda _destination: None,
        classifications={},
    )
    target = _Target()
    bob_action = next(
        action
        for action in plan["actions"]
        if action["component"] == "history" and action["actor"] == "bob"
    )
    target.fail_once.add(bob_action["destination"])
    journal = tmp_path / "journal.jsonl"
    loader = lambda action: load_action_value(source, action)

    first = apply_migration_plan(plan, target, journal, loader)
    assert first["status"] == "partial"
    assert target.cursors == {"alice": 3}
    assert "bob" not in target.cursors

    second = apply_migration_plan(plan, target, journal, loader)
    assert second["status"] == "complete"
    assert target.cursors == {"alice": 3, "bob": 2}
    counts_after_resume = dict(target.put_count)

    third = apply_migration_plan(plan, target, journal, loader)
    assert third["status"] == "complete"
    assert target.put_count == counts_after_resume
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert all(event["snapshot_id"] == SNAPSHOT_ID for event in events)
    assert any(event["status"] == "failed" and event["actor"] == "bob" for event in events)
    assert any(event["status"] == "cursor_published" and event["actor"] == "alice" for event in events)


def test_apply_refuses_unreviewed_plan_without_writes(tmp_path: Path) -> None:
    from familia.memory_migration import (
        MigrationBlockedError,
        apply_migration_plan,
        build_migration_plan,
        load_action_value,
    )

    source = tmp_path / "source"
    source.mkdir()
    (source / "USER.md").write_text("ambiguous owners", encoding="utf-8")
    plan = build_migration_plan(
        source_root=source,
        snapshot_manifest=_snapshot(),
        known_actors={"alice", "bob"},
        target_id="isolated-test",
        target_probe=lambda _destination: None,
        classifications={},
    )
    target = _Target()

    with pytest.raises(MigrationBlockedError, match="unresolved"):
        apply_migration_plan(
            plan,
            target,
            tmp_path / "journal.jsonl",
            lambda action: load_action_value(source, action),
        )
    assert target.values == {}
