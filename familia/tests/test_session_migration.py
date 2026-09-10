"""Contract tests for private session identity and safe legacy migration."""

from __future__ import annotations

import json
from base64 import urlsafe_b64encode
from pathlib import Path

import pytest

from familia.session_identity import make_private_session_key, parse_private_session_key
from familia.session_migration import analyze_sessions, apply_migration


def _write_session(
    root: Path,
    name: str,
    key: str,
    messages: list[dict[str, object]],
    *,
    cursor: int = 0,
    metadata: dict[str, object] | None = None,
) -> Path:
    path = root / name
    records = [
        {
            "_type": "metadata",
            "key": key,
            "created_at": "2026-09-07T08:00:00+00:00",
            "updated_at": "2026-09-07T08:01:00+00:00",
            "metadata": metadata or {},
            "last_consolidated": cursor,
        },
        *messages,
    ]
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _target_files(output: Path) -> list[Path]:
    return sorted((output / "sessions").glob("*.jsonl"))


def _read_target(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_private_key_round_trip_keeps_opaque_route_and_does_not_authorize() -> None:
    key = make_private_session_key("A.user-01", "telegram:123:thread")

    assert key == "familia:A.user-01:telegram:123:thread"
    assert parse_private_session_key(key) == ("A.user-01", "telegram:123:thread")
    assert parse_private_session_key("familia:unknown:telegram:123") == (
        "unknown",
        "telegram:123",
    )

    with pytest.raises(ValueError):
        make_private_session_key("bad id", "telegram:123")
    assert parse_private_session_key("familia:alice:") is None


def test_private_key_accepts_memory_contract_principal_boundaries() -> None:
    first = "A"
    last = "Z9._-" + "a" * 123

    assert parse_private_session_key(make_private_session_key(first, "route")) == (
        first,
        "route",
    )
    assert parse_private_session_key(make_private_session_key(last, "route")) == (
        last,
        "route",
    )
    assert parse_private_session_key("familia:_invalid:route") is None
    assert parse_private_session_key("familia:" + ("a" * 129) + ":route") is None


def test_mixed_owners_are_split_without_assigning_auxiliary_actor(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_session(
        source,
        "chat.jsonl",
        "telegram:shared",
        [
            {"role": "user", "actor": "alice", "content": "A"},
            {"role": "assistant", "content": "answer A", "trace": {"x": 1}},
            {"role": "user", "actor": "bob", "content": "B"},
            {"role": "assistant", "content": "answer B"},
        ],
    )

    plan = analyze_sessions(source, registry={"alice", "bob"})
    result = apply_migration(plan, tmp_path / "out")

    assert result.created_targets == 2
    files = _target_files(tmp_path / "out")
    assert len(files) == 2
    payloads = [_read_target(path) for path in files]
    assert {payload[0]["key"] for payload in payloads} == {
        "familia:alice:telegram:shared",
        "familia:bob:telegram:shared",
    }
    assert all(
        "actor" not in message
        for payload in payloads
        for message in payload[1:]
        if message.get("role") in {"assistant", "tool"}
    )


@pytest.mark.parametrize("role", ["assistant", "tool"])
def test_explicit_auxiliary_actor_mismatch_quarantines_whole_group(
    tmp_path: Path, role: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    messages: list[dict[str, object]] = [
        {"role": "user", "actor": "alice", "content": "private"},
    ]
    if role == "assistant":
        messages.append({"role": "assistant", "actor": "bob", "content": "wrong"})
    else:
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "calling",
                    "tool_calls": [{"id": "call-1"}],
                },
                {
                    "role": "tool",
                    "actor": "bob",
                    "tool_call_id": "call-1",
                    "content": "wrong",
                },
            ]
        )
    _write_session(source, "chat.jsonl", "telegram:shared", messages)

    plan = analyze_sessions(source, registry={"alice", "bob"})

    assert plan.target_count == 0
    entries = plan.files[0].quarantine
    mismatch = [entry for entry in entries if entry["reason"] == "actor_mismatch"]
    assert len(mismatch) == 1
    assert [message["role"] for message in mismatch[0]["record"]] == [
        message["role"] for message in messages
    ]


def test_unknown_corrupt_and_incomplete_groups_stay_in_quarantine(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = _write_session(
        source,
        "chat.jsonl",
        "telegram:shared",
        [
            {"role": "user", "actor": "alice", "content": "safe"},
            {"role": "assistant", "content": "safe answer"},
            {"role": "user", "actor": "mallory", "content": "unknown"},
            {
                "role": "user",
                "actor": "alice",
                "content": "incomplete",
            },
            {
                "role": "assistant",
                "content": "calling",
                "tool_calls": [{"id": "call-1"}],
            },
        ],
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
        handle.write(json.dumps({"role": "system", "content": "unknown role"}) + "\n")

    result = apply_migration(analyze_sessions(source, registry={"alice"}), tmp_path / "out")

    files = _target_files(tmp_path / "out")
    assert len(files) == 1
    payload = _read_target(files[0])
    assert [message["content"] for message in payload[1:]] == ["safe", "safe answer"]
    quarantine = list((tmp_path / "out" / "quarantine").glob("*.jsonl"))
    assert quarantine
    quarantine_text = "\n".join(path.read_text(encoding="utf-8") for path in quarantine)
    assert "unknown_user_actor" in quarantine_text
    assert "incomplete_tool_group" in quarantine_text
    assert "corrupt_json" in quarantine_text
    assert result.status == "partial"


def test_cursor_counts_only_complete_saved_groups_and_apply_is_repeatable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_session(
        source,
        "chat.jsonl",
        "telegram:shared",
        [
            {"role": "user", "actor": "alice", "content": "first"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "actor": "alice", "content": "second"},
            {"role": "assistant", "content": "second answer"},
        ],
        cursor=3,
    )
    output = tmp_path / "out"
    plan = analyze_sessions(source, registry={"alice"})
    first = apply_migration(plan, output)
    before = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
    second = apply_migration(plan, output)
    after = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}

    target = _read_target(_target_files(output)[0])
    assert target[0]["last_consolidated"] == 2
    assert first.created_targets == 1
    assert second.unchanged_targets == 1
    assert before == after


def test_source_is_copied_verbatim_and_output_cannot_be_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = _write_session(
        source,
        "chat.jsonl",
        "telegram:1",
        [{"role": "user", "actor": "alice", "content": "hello", "extra": [1, 2]}],
    )
    plan = analyze_sessions(source, registry={"alice"})
    output = tmp_path / "out"
    apply_migration(plan, output)

    snapshot = output / "source-snapshot" / "chat.jsonl"
    assert snapshot.read_bytes() == source_file.read_bytes()
    assert urlsafe_b64encode(b"familia:alice:telegram:1").decode().rstrip("=") in {
        path.stem for path in _target_files(output)
    }
    with pytest.raises(ValueError):
        apply_migration(plan, source / "nested-output")

    with pytest.raises(ValueError):
        apply_migration(plan, source.parent)
    assert not any(
        (source.parent / name).exists()
        for name in ("sessions", "source-snapshot", "quarantine", "migration-report.json")
    )

    file_plan = analyze_sessions(source_file, registry={"alice"})
    with pytest.raises(ValueError):
        apply_migration(file_plan, source.parent)
