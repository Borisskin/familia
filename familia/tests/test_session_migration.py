"""Contract tests for private session identity and safe legacy migration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from base64 import urlsafe_b64encode
from pathlib import Path

import pytest

from familia.session_identity import make_private_session_key, parse_private_session_key
from familia.session_migration import analyze_sessions, apply_migration
from familia.session_storage import prepare_familia_session_storage


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


def test_existing_private_key_is_preserved_with_continuation_and_boundaries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    key = "familia:alice:telegram:private"
    path = source / "private.jsonl"
    records = [
        {
            "_type": "metadata",
            "key": key,
            "created_at": "2026-09-07T08:00:00+00:00",
            "updated_at": "2026-09-07T08:01:00+00:00",
            "metadata": {"unknown": {"keep": True}, "runtime_checkpoint": {"ok": 1}},
            "last_archived": 1,
            "last_consolidated": 2,
            "unknown_top_level": [1, 2],
        },
        {"role": "user", "actor": "client-supplied", "content": "hello"},
        {"role": "assistant", "content": "reply", "extra": {"preserve": True}},
        {"_type": "provider_state", "state": {"continuation": "synthetic"}},
    ]
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    sidecar = source / "private.checkpoint.json"
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "session_key": key,
                "checkpoint": {"turn": "pending"},
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "out"
    result = apply_migration(
        analyze_sessions(source, registry={"alice"}),
        output,
    )

    assert result.created_targets == 1
    assert result.status == "complete"
    target = next(output.joinpath("sessions").glob("*.jsonl"))
    payload = _read_target(target)
    assert payload[0]["key"] == key
    assert payload[0]["last_archived"] == 1
    assert payload[0]["last_consolidated"] == 2
    assert payload[0]["metadata"]["unknown"] == {"keep": True}
    assert payload[0]["unknown_top_level"] == [1, 2]
    assert payload[1]["actor"] == "client-supplied"
    assert payload[-1]["_type"] == "provider_state"
    checkpoint = target.with_suffix(".checkpoint.json")
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["session_key"] == key


def test_route_owner_is_server_side_not_client_message_field(tmp_path: Path) -> None:
    @dataclass
    class Registry:
        ids: list[str]

        def resolve(self, channel: str, chat_id: str) -> str | None:
            return "alice" if (channel, chat_id) == ("telegram", "chat") else None

    source = tmp_path / "source"
    source.mkdir()
    _write_session(
        source,
        "chat.jsonl",
        "telegram:chat",
        [{"role": "user", "actor": "bob", "content": "spoof"}],
    )
    plan = analyze_sessions(source, registry=Registry(["alice", "bob"]))
    assert plan.target_count == 0
    assert any(
        entry["reason"] == "actor_mismatch"
        for entry in plan.files[0].quarantine
    )


def test_prepare_restored_root_preserves_workspace_namespace(tmp_path: Path) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path | None
        workspace_path: Path

    old_workspace = tmp_path / "old-workspace"
    old_workspace.mkdir()
    original_data = tmp_path / "original-data"
    data = tmp_path / "restored-data"
    old_sessions = data / "sessions"
    old_sessions.mkdir(parents=True)
    old_id = "a" * 32
    (old_workspace / ".nanobot").mkdir()
    (old_workspace / ".nanobot" / "workspace-id").write_text(old_id + "\n", encoding="utf-8")
    old_namespace = old_sessions / old_id
    old_namespace.mkdir()
    (old_namespace / ".workspace").write_text(str(old_workspace) + "\n", encoding="utf-8")
    (old_namespace / "history.jsonl").write_text("old\n", encoding="utf-8")
    (data / ".familia-session-root").write_text(
        str(original_data.resolve()) + "\n", encoding="utf-8"
    )

    restored_workspace = tmp_path / "restored-workspace"
    restored_workspace.mkdir()
    (restored_workspace / ".nanobot").mkdir()
    (restored_workspace / ".nanobot" / "workspace-id").write_text(
        old_id + "\n", encoding="utf-8"
    )

    prepared = prepare_familia_session_storage(
        Config(runtime_data_dir=data, workspace_path=restored_workspace)
    )

    assert prepared.sessions_root == data / "sessions"
    assert prepared.workspace_id == old_id
    assert (old_namespace / "history.jsonl").read_text(encoding="utf-8") == "old\n"
    assert (old_namespace / ".workspace").read_text(encoding="utf-8").strip() == str(
        restored_workspace
    )
    assert (data / ".familia-session-root").read_text(encoding="utf-8").strip() == str(
        data.resolve()
    )
    with pytest.raises(ValueError):
        prepare_familia_session_storage(
            Config(runtime_data_dir=None, workspace_path=restored_workspace)
        )


def test_prepare_shared_root_collision_refuses_foreign_history(tmp_path: Path) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    old_workspace = tmp_path / "old-workspace"
    old_workspace.mkdir()
    workspace = tmp_path / "copied-workspace"
    workspace.mkdir()
    data = tmp_path / "data"
    sessions = data / "sessions"
    namespace_id = "b" * 32
    (workspace / ".nanobot").mkdir()
    (workspace / ".nanobot" / "workspace-id").write_text(
        namespace_id + "\n", encoding="utf-8"
    )
    namespace = sessions / namespace_id
    namespace.mkdir(parents=True)
    (namespace / ".workspace").write_text(str(old_workspace) + "\n", encoding="utf-8")
    (namespace / "history.jsonl").write_text("foreign\n", encoding="utf-8")
    (data / ".familia-session-root").write_text(
        str(data.resolve()) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="shared-root collision"):
        prepare_familia_session_storage(Config(data, workspace))

    assert (namespace / "history.jsonl").read_text(encoding="utf-8") == "foreign\n"
    assert sorted(path.name for path in sessions.iterdir()) == [namespace_id]


def test_prepare_shared_root_missing_workspace_refuses_foreign_history(
    tmp_path: Path,
) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    workspace = tmp_path / "copied-workspace"
    missing_workspace = tmp_path / "removed-original-workspace"
    data = tmp_path / "data"
    sessions = data / "sessions"
    namespace_id = "c" * 32
    (workspace / ".nanobot").mkdir(parents=True)
    (workspace / ".nanobot" / "workspace-id").write_text(
        namespace_id + "\n", encoding="utf-8"
    )
    namespace = sessions / namespace_id
    namespace.mkdir(parents=True)
    (namespace / ".workspace").write_text(
        str(missing_workspace) + "\n", encoding="utf-8"
    )
    history = namespace / "history.jsonl"
    history.write_text("foreign\n", encoding="utf-8")
    (data / ".familia-session-root").write_text(
        str(data.resolve()) + "\n", encoding="utf-8"
    )
    before = {
        path.relative_to(data): path.read_bytes()
        for path in data.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ValueError, match="shared-root collision"):
        prepare_familia_session_storage(Config(data, workspace))

    after = {
        path.relative_to(data): path.read_bytes()
        for path in data.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert history.read_text(encoding="utf-8") == "foreign\n"


def test_prepare_repeated_real_storage_is_byte_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    import familia.session_migration as migration

    data = tmp_path / "data"
    source = data / "sessions"
    source.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_session(
        source,
        "repeat.jsonl",
        "familia:alice:telegram:repeat",
        [{"role": "user", "actor": "alice", "content": "once"}],
    )
    monkeypatch.setattr(migration, "get_registry", lambda: {"alice"})

    first = prepare_familia_session_storage(Config(data, workspace))
    before = {
        path.relative_to(data): path.read_bytes()
        for path in data.rglob("*")
        if path.is_file()
    }
    second = prepare_familia_session_storage(Config(data, workspace))
    after = {
        path.relative_to(data): path.read_bytes()
        for path in data.rglob("*")
        if path.is_file()
    }

    target = first.sessions_root / first.workspace_id
    target_file = next(target.glob("*.jsonl"))
    assert first.workspace_id == second.workspace_id
    assert before == after
    assert len(_read_target(target_file)[1:]) == 1


def test_prepare_moves_workspace_legacy_sessions_out_of_core_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    import familia.session_migration as migration

    workspace = tmp_path / "workspace"
    source = workspace / "sessions"
    source.mkdir(parents=True)
    data = tmp_path / "data"
    source_file = _write_session(
        source,
        "legacy.jsonl",
        "telegram:legacy",
        [{"role": "user", "actor": "alice", "content": "legacy"}],
    )
    source_bytes = source_file.read_bytes()
    monkeypatch.setattr(migration, "get_registry", lambda: {"alice"})

    prepared = prepare_familia_session_storage(Config(data, workspace))

    archived = (
        data
        / "source-snapshot"
        / "workspace-sessions"
        / prepared.workspace_id
        / "legacy.jsonl"
    )
    assert not source.exists()
    assert archived.read_bytes() == source_bytes
    assert (prepared.sessions_root / prepared.workspace_id).is_dir()


def test_prepare_rejects_session_root_inside_workspace_before_writing(tmp_path: Path) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = sorted(path.relative_to(workspace) for path in workspace.rglob("*"))

    with pytest.raises(ValueError, match="session storage must be outside"):
        prepare_familia_session_storage(Config(workspace, workspace))

    assert sorted(path.relative_to(workspace) for path in workspace.rglob("*")) == before


def test_prepare_repeats_after_interrupted_root_claim(tmp_path: Path) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    data = tmp_path / "data"
    workspace = tmp_path / "workspace"
    data.mkdir()
    workspace.mkdir()
    pending = data / ".familia-session-root.tmp"
    pending.write_text(str(data.resolve()) + "\n", encoding="utf-8")

    prepared = prepare_familia_session_storage(Config(data, workspace))

    assert prepared.sessions_root == data / "sessions"
    assert (data / ".familia-session-root").read_text(encoding="utf-8").strip() == str(
        data.resolve()
    )
    assert not pending.exists()


def test_prepared_parent_is_visible_to_the_standard_session_manager(tmp_path: Path, monkeypatch) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    import familia.session_migration as migration
    from nanobot.session.manager import SessionManager

    workspace = tmp_path / "workspace"
    legacy = workspace / "sessions"
    legacy.mkdir(parents=True)
    _write_session(
        legacy,
        "legacy.jsonl",
        "telegram:chat",
        [{"role": "user", "actor": "alice", "content": "legacy"}],
    )
    data = tmp_path / "data"
    monkeypatch.setattr(migration, "get_registry", lambda: {"alice"})

    prepared = prepare_familia_session_storage(Config(data, workspace))
    manager = SessionManager(workspace, sessions_root=prepared.sessions_root)

    assert manager.sessions_dir == prepared.sessions_root / prepared.workspace_id
    assert manager._get_session_path("familia:alice:telegram:chat").is_file()


def test_prepare_migrates_flat_runtime_sessions_into_claimed_namespace(
    tmp_path: Path, monkeypatch
) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    import familia.session_migration as migration

    data = tmp_path / "data"
    flat = data / "sessions"
    flat.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key = "familia:alice:telegram:flat"
    _write_session(
        flat,
        "flat.jsonl",
        key,
        [{"role": "user", "actor": "untrusted-field", "content": "kept"}],
    )
    monkeypatch.setattr(migration, "get_registry", lambda: {"alice"})

    prepared = prepare_familia_session_storage(Config(data, workspace))

    target = prepared.sessions_root / prepared.workspace_id
    target_file = next(target.glob("*.jsonl"))
    assert _read_target(target_file)[0]["key"] == key
    assert (data / "source-snapshot" / "flat.jsonl").is_file()
    assert (flat / "flat.jsonl").is_file()


def test_prepare_multiple_flat_sessions_has_one_repeatable_report(
    tmp_path: Path, monkeypatch
) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    import familia.session_migration as migration

    data = tmp_path / "data"
    flat = data / "sessions"
    flat.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_bytes: dict[str, bytes] = {}
    for name, chat_id in (("first.jsonl", "first"), ("second.jsonl", "second")):
        source = _write_session(
            flat,
            name,
            f"familia:alice:telegram:{chat_id}",
            [{"role": "user", "actor": "alice", "content": chat_id}],
        )
        source_bytes[name] = source.read_bytes()
    monkeypatch.setattr(migration, "get_registry", lambda: {"alice"})

    first = prepare_familia_session_storage(Config(data, workspace))
    before = {
        path.relative_to(data): path.read_bytes()
        for path in data.rglob("*")
        if path.is_file()
    }
    second = prepare_familia_session_storage(Config(data, workspace))
    after = {
        path.relative_to(data): path.read_bytes()
        for path in data.rglob("*")
        if path.is_file()
    }
    report = json.loads((data / "migration-report.json").read_text(encoding="utf-8"))

    target_root = first.sessions_root / first.workspace_id
    assert first.workspace_id == second.workspace_id
    assert report["source_root"] == str(flat)
    assert len(first.migrations) == 1
    assert second.migrations == ()
    assert len(list(target_root.glob("*.jsonl"))) == 2
    assert {path.name: path.read_bytes() for path in flat.glob("*.jsonl")} == source_bytes
    assert before == after
    assert not list(data.rglob("migration-report.json.*.conflict"))


def test_prepare_mixed_workspace_and_flat_sources_has_one_report(
    tmp_path: Path, monkeypatch
) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    import familia.session_migration as migration

    workspace = tmp_path / "workspace"
    workspace_sessions = workspace / "sessions"
    workspace_sessions.mkdir(parents=True)
    data = tmp_path / "data"
    flat = data / "sessions"
    flat.mkdir(parents=True)
    workspace_source = _write_session(
        workspace_sessions,
        "workspace.jsonl",
        "telegram:workspace",
        [{"role": "user", "actor": "alice", "content": "workspace"}],
    )
    workspace_bytes = workspace_source.read_bytes()
    flat_source = _write_session(
        flat,
        "flat.jsonl",
        "familia:alice:telegram:flat",
        [{"role": "user", "actor": "alice", "content": "flat"}],
    )
    flat_bytes = flat_source.read_bytes()
    monkeypatch.setattr(migration, "get_registry", lambda: {"alice"})

    first = prepare_familia_session_storage(Config(data, workspace))
    before = {
        path.relative_to(data): path.read_bytes()
        for path in data.rglob("*")
        if path.is_file()
    }
    second = prepare_familia_session_storage(Config(data, workspace))
    after = {
        path.relative_to(data): path.read_bytes()
        for path in data.rglob("*")
        if path.is_file()
    }

    archived = (
        data
        / "source-snapshot"
        / "workspace-sessions"
        / first.workspace_id
        / "workspace.jsonl"
    )
    report = json.loads((data / "migration-report.json").read_text(encoding="utf-8"))
    target_root = first.sessions_root / first.workspace_id
    assert not workspace_sessions.exists()
    assert archived.read_bytes() == workspace_bytes
    assert flat_source.read_bytes() == flat_bytes
    assert len(report["source_files"]) == 2
    assert len(list(target_root.glob("*.jsonl"))) == 2
    assert len(first.migrations) == 1
    assert second.migrations == ()
    assert before == after
    assert not list(data.rglob("migration-report.json.*.conflict"))


def test_prepare_mixed_same_named_sources_keep_distinct_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    import familia.session_migration as migration

    workspace = tmp_path / "workspace"
    workspace_sessions = workspace / "sessions"
    workspace_sessions.mkdir(parents=True)
    data = tmp_path / "data"
    flat = data / "sessions"
    flat.mkdir(parents=True)
    workspace_source = _write_session(
        workspace_sessions,
        "same.jsonl",
        "telegram:workspace-same",
        [{"role": "user", "actor": "alice", "content": "workspace"}],
    )
    workspace_bytes = workspace_source.read_bytes()
    flat_source = _write_session(
        flat,
        "same.jsonl",
        "familia:alice:telegram:flat-same",
        [{"role": "user", "actor": "alice", "content": "flat"}],
    )
    flat_bytes = flat_source.read_bytes()
    workspace_sidecar = workspace_sessions / "same.checkpoint.json"
    flat_sidecar = flat / "same.checkpoint.json"
    workspace_sidecar.write_text("workspace-sidecar", encoding="utf-8")
    flat_sidecar.write_text("flat-sidecar", encoding="utf-8")
    monkeypatch.setattr(migration, "get_registry", lambda: {"alice"})

    prepared = prepare_familia_session_storage(Config(data, workspace))

    report = json.loads((data / "migration-report.json").read_text(encoding="utf-8"))
    source_paths = {entry["path"] for entry in report["source_files"]}
    assert report["source_root"] == str(workspace_sessions)
    assert source_paths == {"workspace-sessions/same.jsonl", "same.jsonl"}
    assert (
        data / "source-snapshot" / "workspace-sessions" / "same.jsonl"
    ).read_bytes() == workspace_bytes
    assert (data / "source-snapshot" / "same.jsonl").read_bytes() == flat_bytes
    assert (
        data / "source-snapshot" / "workspace-sessions" / "same.checkpoint.json"
    ).read_text(encoding="utf-8") == "workspace-sidecar"
    assert (data / "source-snapshot" / "same.checkpoint.json").read_text(
        encoding="utf-8"
    ) == "flat-sidecar"
    assert len(prepared.migrations) == 1
    assert not list(data.rglob("migration-report.json.*.conflict"))


def test_prepare_skips_consumed_archive_after_standard_session_save(
    tmp_path: Path, monkeypatch
) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    import familia.session_migration as migration
    from nanobot.session.manager import SessionManager

    workspace = tmp_path / "workspace"
    legacy = workspace / "sessions"
    legacy.mkdir(parents=True)
    data = tmp_path / "data"
    key = "familia:alice:telegram:after-save"
    _write_session(
        legacy,
        "legacy.jsonl",
        key,
        [{"role": "user", "actor": "alice", "content": "before"}],
    )
    monkeypatch.setattr(migration, "get_registry", lambda: {"alice"})

    first = prepare_familia_session_storage(Config(data, workspace))
    manager = SessionManager(workspace, sessions_root=first.sessions_root)
    session = manager._jsonl_store.load(key)
    assert session is not None
    session.messages.append({"role": "user", "content": "after"})
    manager.save(session)

    second = prepare_familia_session_storage(Config(data, workspace))

    target = manager._get_session_path(key)
    assert second.migrations == ()
    assert [message["content"] for message in _read_target(target)[1:]] == [
        "before",
        "after",
    ]
    assert not list(data.rglob("migration-report.json.*.conflict"))


def test_prepare_rejects_sessions_symlink_before_any_write(tmp_path: Path) -> None:
    @dataclass
    class Config:
        runtime_data_dir: Path
        workspace_path: Path

    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "sessions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe session root"):
        prepare_familia_session_storage(Config(data, workspace))

    assert not (data / ".familia-session-root").exists()
    assert not list(outside.iterdir())


def test_apply_real_destination_conflict_keeps_existing_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_session(
        source,
        "conflict.jsonl",
        "familia:alice:telegram:conflict",
        [{"role": "user", "actor": "alice", "content": "source"}],
    )
    output = tmp_path / "output"
    plan = analyze_sessions(source, registry={"alice"})
    apply_migration(plan, output)
    target = next((output / "sessions").glob("*.jsonl"))
    original = target.read_bytes()
    target.write_bytes(b"existing\n")

    result = apply_migration(plan, output)

    assert result.conflicting_targets == 1
    assert target.read_bytes() == b"existing\n"
    assert list((target.parent / "conflicts").glob("*.conflict"))
    assert original != target.read_bytes()


def test_interrupted_apply_can_resume_from_preserved_source(tmp_path: Path, monkeypatch) -> None:
    import familia.session_migration as migration

    source = tmp_path / "source"
    source.mkdir()
    for name in ("first.jsonl", "second.jsonl"):
        _write_session(
            source,
            name,
            "telegram:" + name,
            [{"role": "user", "actor": "alice", "content": name}],
        )
    plan = analyze_sessions(source, registry={"alice"})
    output = tmp_path / "out"
    original_write = migration._write_new_or_compare
    failed = False

    def fail_once(path: Path, data: bytes) -> str:
        nonlocal failed
        if path.parent.name == "sessions" and not failed:
            failed = True
            raise OSError("synthetic interruption")
        return original_write(path, data)

    monkeypatch.setattr(migration, "_write_new_or_compare", fail_once)
    with pytest.raises(OSError, match="synthetic interruption"):
        apply_migration(plan, output)
    assert (output / "source-snapshot").is_dir()

    monkeypatch.setattr(migration, "_write_new_or_compare", original_write)
    result = apply_migration(plan, output)
    assert result.created_targets == 2
    assert len(list((output / "sessions").glob("*.jsonl"))) == 2
