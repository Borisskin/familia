from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_heartbeat_source_reader_uses_principal_memx(monkeypatch) -> None:
    from familia.nanobot_extension import cron

    class _Registry:
        def get(self, actor_id: str):
            assert actor_id == "principal_a"
            return SimpleNamespace(memx_key="mem_key_a")

    class _Client:
        def __init__(self, actor_id: str, memx_key: str) -> None:
            assert actor_id == "principal_a"
            assert memx_key == "mem_key_a"

        def get(self, key: str) -> str:
            assert key == "value:heartbeat"
            return "check shared calendar"

    monkeypatch.setattr(cron, "get_registry", lambda: _Registry())
    monkeypatch.setattr(cron, "PrincipalMemoryClient", _Client)

    reader = cron.make_heartbeat_source_reader("principal_a")

    assert reader() == ("check shared calendar", "memx")


def test_heartbeat_source_reader_fails_closed_without_memx_key(monkeypatch) -> None:
    from familia.nanobot_extension import cron

    class _Registry:
        def get(self, actor_id: str):
            assert actor_id == "principal_a"
            return SimpleNamespace(memx_key=None)

    monkeypatch.setattr(cron, "get_registry", lambda: _Registry())

    reader = cron.make_heartbeat_source_reader("principal_a")

    assert reader() == (None, None)


def test_make_dream_tool_installers_registers_dream_memory_tool(
    monkeypatch,
) -> None:
    from familia.nanobot_extension import cron
    from familia import memx_client, principal_memory_ingestor

    class _Registry:
        def __init__(self) -> None:
            self.tools = []
            self.events = []

        def unregister(self, name: str) -> None:
            self.events.append(("unregister", name))

        def register(self, tool) -> None:
            self.events.append(("register", tool.name))
            self.tools.append(tool)

    registry = _Registry()
    ingestor = MagicMock()
    ingestor_factory = MagicMock(return_value=ingestor)
    monkeypatch.setenv("DREAM_CONSOLIDATOR_MEMX_KEY", "dream-writer-key")
    monkeypatch.setattr(memx_client, "memx_base_url", lambda: "http://mock-memx:8000")
    monkeypatch.setattr(
        principal_memory_ingestor,
        "PrincipalMemoryIngestor",
        ingestor_factory,
    )
    profile_version_getter = MagicMock(return_value=41.0)
    installers = cron.make_dream_tool_installers(
        profile_version_getter=profile_version_getter
    )

    assert len(installers) == 1
    installers[0](registry, SimpleNamespace(workspace=None))
    assert registry.events == [
        ("unregister", "read_file"),
        ("unregister", "edit_file"),
        ("unregister", "write_file"),
        ("register", "dream_memory_set"),
    ]
    assert [tool.name for tool in registry.tools] == ["dream_memory_set"]
    ingestor_factory.assert_called_once_with(
        base_url="http://mock-memx:8000",
        api_key="dream-writer-key",
        server_topic_validator=None,
    )
    assert registry.tools[0]._ingestor is ingestor
    assert callable(registry.tools[0]._server_principal_getter)
    assert registry.tools[0]._profile_version_getter is profile_version_getter


@pytest.mark.asyncio
async def test_dream_writer_tracks_exception_before_reraising(monkeypatch) -> None:
    from familia import memx_client, principal_memory_ingestor
    from familia.nanobot_extension import cron
    from familia.principals import get_current_actor, set_current_actor
    from familia.tools import memory as memory_mod

    class _Registry:
        def __init__(self) -> None:
            self.tools = []

        def unregister(self, _name: str) -> None:
            pass

        def register(self, tool) -> None:
            self.tools.append(tool)

    ingestor = MagicMock()
    failure = RuntimeError("ingestor unavailable")
    ingestor.ingest = AsyncMock(side_effect=failure)
    tracker = MagicMock()
    monkeypatch.setattr(memx_client, "memx_base_url", lambda: "http://mock-memx:8000")
    monkeypatch.setattr(
        principal_memory_ingestor,
        "PrincipalMemoryIngestor",
        MagicMock(return_value=ingestor),
    )
    monkeypatch.setattr(memory_mod, "_check_memory_write_policy", lambda **_kwargs: None)
    registry = _Registry()
    cron.make_dream_tool_installers(
        server_principal_getter=lambda: "principal_a",
        result_tracker=tracker,
    )[0](registry, None)

    previous_actor = get_current_actor()
    set_current_actor("caller")
    try:
        with pytest.raises(RuntimeError) as raised:
            await registry.tools[0].execute(
                kind="memory",
                fact_id="fact-17",
                value="private fact",
            )
        assert raised.value is failure
        assert get_current_actor() == "caller"
    finally:
        set_current_actor(previous_actor)

    tracker.assert_called_once_with("Error: Dream automatic memory writer failed")
    ingestor.ingest.assert_awaited_once_with(
        server_principal="principal_a",
        server_topic=None,
        operation={
            "kind": "memory",
            "fact_id": "fact-17",
            "value": "private fact",
        },
    )


def test_cron_identity_round_trip_restart_and_route_distinction(tmp_path, monkeypatch) -> None:
    from familia.nanobot_extension import cron as familia_cron
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule

    class _Registry:
        def get(self, actor_id: str):
            if actor_id != "owner":
                return None
            return SimpleNamespace(
                identities=[SimpleNamespace(channel="tg", sender_id="2000001")],
            )

    monkeypatch.setattr(familia_cron, "get_registry", lambda: _Registry())

    monkeypatch.setattr(CronService, "_arm_timer", lambda _self: None)
    store_path = tmp_path / "cron" / "jobs.json"
    schedule = CronSchedule(kind="cron", expr="0 9 * * *", tz="Europe/Moscow")
    job_kwargs = {
        "name": "family reminder",
        "schedule": schedule,
        "message": "check the shared calendar",
        "deliver": True,
        "channel": "tg",
        "to": "2000001",
        "delete_after_run": False,
        "origin_metadata": {"actor": "owner"},
    }

    service = CronService(store_path)
    service._running = True
    original = service.add_job(**job_kwargs)

    restarted = CronService(store_path)
    restarted._running = True
    loaded = restarted.get_job(original.id)

    assert loaded is not None
    # Legacy delivery fields are normalized to a session-bound record.
    assert loaded.payload.deliver is False
    assert loaded.payload.channel is None
    assert loaded.payload.to is None
    assert loaded.payload.session_key == "tg:2000001"
    assert loaded.payload.origin_channel == "tg"
    assert loaded.payload.origin_chat_id == "2000001"

    owner, private_key = familia_cron._job_actor_and_session(loaded)
    assert owner == "owner"
    assert private_key == "familia:owner:tg:2000001"

    duplicate = restarted.add_job(**job_kwargs)
    different_target = restarted.add_job(**{**job_kwargs, "to": "2000002"})

    assert duplicate.id != original.id
    assert different_target.id != original.id
    assert different_target.id != duplicate.id
    assert len(restarted.list_jobs()) == 3

    persisted = json.loads(store_path.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    saved_payload = next(job["payload"] for job in persisted["jobs"] if job["id"] == original.id)
    assert saved_payload["sessionKey"] == "tg:2000001"
    assert saved_payload["originChannel"] == "tg"
    assert saved_payload["originChatId"] == "2000001"
    assert saved_payload["originMetadata"] == {"actor": "owner"}


def test_cron_owner_rejects_unproven_or_mismatched_origin(monkeypatch) -> None:
    from familia.nanobot_extension import cron as familia_cron

    class _Registry:
        def get(self, actor_id: str):
            if actor_id == "owner":
                return SimpleNamespace(
                    identities=[SimpleNamespace(channel="telegram", sender_id="recipient")]
                )
            return None

    monkeypatch.setattr(familia_cron, "get_registry", lambda: _Registry())
    unproven = SimpleNamespace(
        id="unproven",
        payload=SimpleNamespace(
            session_key="cron:unproven",
            origin_channel="telegram",
            origin_chat_id="recipient",
            origin_metadata={"actor": "creator"},
            creator_actor="creator",
        ),
    )
    mismatched = SimpleNamespace(
        id="mismatched",
        payload=SimpleNamespace(
            session_key="familia:owner:telegram:recipient",
            origin_channel="telegram",
            origin_chat_id="other-recipient",
            origin_metadata={"actor": "owner"},
            target_actor="owner",
        ),
    )

    with pytest.raises(ValueError, match="no registered owner"):
        familia_cron._job_actor_and_session(unproven)
    with pytest.raises(ValueError, match="route does not belong"):
        familia_cron._job_actor_and_session(mismatched)


def test_legacy_cron_creator_is_preserved_without_inventing_target(tmp_path) -> None:
    from nanobot.cron.service import CronService

    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "legacy-1",
                        "name": "legacy reminder",
                        "schedule": {"kind": "every", "everyMs": 60000},
                        "payload": {
                            "message": "legacy",
                            "createdBy": "owner",
                            "deliver": False,
                            "tags": ["legacy"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    service = CronService(store_path)
    loaded = service.get_job("legacy-1")

    assert loaded is not None
    assert loaded.payload.session_key is None
    assert loaded.enabled is False
    assert loaded.state.last_status == "error"
    assert loaded.state.last_error is not None
    assert "missing bound session" in loaded.state.last_error
    assert service._load_store().version == 1


def test_legacy_cron_owner_fields_and_tags_survive_save_reload_and_execution(tmp_path) -> None:
    from nanobot.cron.service import CronService

    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 2,
                "jobs": [
                    {
                        "id": "legacy-owners",
                        "name": "legacy owner reminder",
                        "schedule": {"kind": "every", "everyMs": 60000},
                        "payload": {
                            "message": "legacy",
                            "sessionKey": "telegram:100",
                            "originChannel": "telegram",
                            "originChatId": "100",
                            "createdBy": "creator",
                            "creatorActor": "creator",
                            "ownerActor": "owner",
                            "targetActor": "target",
                            "tags": ["shared", "topic"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    observed: list[tuple[str | None, str | None, str | None, str | None, list[str]]] = []

    async def on_job(job) -> None:
        observed.append(
            (
                job.payload.created_by,
                job.payload.creator_actor,
                job.payload.owner_actor,
                job.payload.target_actor,
                job.payload.tags,
            )
        )

    service = CronService(store_path, on_job=on_job)
    service._running = True
    service._arm_timer = lambda: None
    assert service.get_job("legacy-owners") is not None
    service._save_store()

    reloaded = CronService(store_path, on_job=on_job)
    reloaded._running = True
    reloaded._arm_timer = lambda: None
    loaded = reloaded.get_job("legacy-owners")

    assert loaded is not None
    assert (
        loaded.payload.created_by,
        loaded.payload.creator_actor,
        loaded.payload.owner_actor,
        loaded.payload.target_actor,
        loaded.payload.tags,
    ) == ("creator", "creator", "owner", "target", ["shared", "topic"])
    assert asyncio.run(reloaded.run_job("legacy-owners"))
    assert observed == [("creator", "creator", "owner", "target", ["shared", "topic"])]

    final = CronService(store_path).get_job("legacy-owners")
    assert final is not None
    assert (
        final.payload.created_by,
        final.payload.creator_actor,
        final.payload.owner_actor,
        final.payload.target_actor,
        final.payload.tags,
    ) == ("creator", "creator", "owner", "target", ["shared", "topic"])


def test_cron_owner_actor_round_trip_supports_snake_and_camel_fields(tmp_path) -> None:
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule

    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)
    service._running = True
    service._arm_timer = lambda: None
    job = service.add_job(
        name="dream",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="private owner turn",
        session_key="familia:owner:telegram:2000001",
        origin_channel="telegram",
        origin_chat_id="2000001",
        origin_metadata={"actor": "owner"},
    )

    restarted = CronService(store_path)
    restarted._running = True
    restarted._arm_timer = lambda: None
    loaded = restarted.get_job(job.id)

    assert loaded is not None
    assert loaded.payload.session_key == "familia:owner:telegram:2000001"
    assert loaded.payload.origin_channel == "telegram"
    assert loaded.payload.origin_chat_id == "2000001"
    assert loaded.payload.origin_metadata == {"actor": "owner"}
    persisted = json.loads(store_path.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    saved_payload = persisted["jobs"][0]["payload"]
    assert saved_payload["sessionKey"] == "familia:owner:telegram:2000001"
    assert saved_payload["originChannel"] == "telegram"
    assert saved_payload["originChatId"] == "2000001"
    assert saved_payload["originMetadata"] == {"actor": "owner"}
