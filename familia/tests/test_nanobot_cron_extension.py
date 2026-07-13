from __future__ import annotations

import json
from types import SimpleNamespace


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


def test_make_dream_tool_installers_registers_dream_memory_tool() -> None:
    from familia.nanobot_extension import cron

    class _Registry:
        def __init__(self) -> None:
            self.tools = []

        def register(self, tool) -> None:
            self.tools.append(tool)

    registry = _Registry()
    installers = cron.make_dream_tool_installers()

    assert len(installers) == 1
    installers[0](registry, SimpleNamespace(workspace=None))
    assert [tool.name for tool in registry.tools] == ["dream_memory_set"]


def test_cron_identity_round_trip_restart_and_dedupe(tmp_path, monkeypatch) -> None:
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule

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
        "created_by": "owner",
        "creator_actor": "owner",
        "target_actor": "member_a",
        "tags": ["family", "calendar"],
    }

    service = CronService(store_path)
    service._running = True
    original = service.add_job(**job_kwargs)

    restarted = CronService(store_path)
    restarted._running = True
    loaded = restarted.get_job(original.id)

    assert loaded is not None
    assert loaded.payload.creator_actor == "owner"
    assert loaded.payload.target_actor == "member_a"
    assert loaded.payload.deliver is True
    assert loaded.payload.channel == "tg"
    assert loaded.payload.to == "2000001"
    assert loaded.payload.tags == ["family", "calendar"]

    duplicate = restarted.add_job(**job_kwargs)
    different_target = restarted.add_job(**{**job_kwargs, "target_actor": "member_b"})

    assert duplicate.id == original.id
    assert different_target.id != original.id
    assert len(restarted.list_jobs()) == 2

    persisted = json.loads(store_path.read_text(encoding="utf-8"))
    assert persisted["version"] == 2
    saved_payload = next(job["payload"] for job in persisted["jobs"] if job["id"] == original.id)
    assert saved_payload["creatorActor"] == "owner"
    assert saved_payload["targetActor"] == "member_a"


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
    assert loaded.payload.creator_actor == "owner"
    assert loaded.payload.target_actor is None
    assert service._load_store().version == 2
