from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_callback_consumer_runs_inside_agent_loop(tmp_path: Path, monkeypatch) -> None:
    from familia import bootstrap
    from familia.bus.callback_dispatcher import CallbackDispatcher
    from familia.principals import Identity, Principal, PrincipalRegistry
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import CallbackEvent
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import GenerationSettings, LLMResponse
    from nanobot.runtime_adapters import RuntimeAdapters

    registry = PrincipalRegistry(
        [
            Principal(
                id="owner",
                display_name="Owner",
                identities=[Identity(channel="telegram", sender_id="sender-1")],
                memx_key="owner-key",
            )
        ]
    )
    monkeypatch.setattr("familia.principals._registry", registry)
    monkeypatch.setattr(
        "familia.nanobot_extension.context.FamiliaContextExtension.build_sections",
        lambda self, **kwargs: [],
    )
    monkeypatch.setattr(
        "familia.nanobot_extension.context.FamiliaContextExtension.build_runtime_sections",
        lambda self, **kwargs: [],
    )

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.supports_progress_deltas = False
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="callback handled", usage={})
    )
    bus = MessageBus()
    adapters = RuntimeAdapters(
        admit=bootstrap._admit_message,
        context_factory=bootstrap._context_factory,
        context_builder_factory=bootstrap._context_builder_factory,
        turn_scope=bootstrap._turn_scope,
        callback_handler=CallbackDispatcher(bus),
    )
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        runtime_adapters=adapters,
        max_iterations=1,
    )

    run_task = asyncio.create_task(loop.run())
    await bus.publish_callback(
        CallbackEvent(
            channel="telegram",
            sender_id="sender-1",
            chat_id="chat-1",
            payload={"choice": "confirm"},
        )
    )
    outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=2)
    assert outbound.content == "callback handled"
    assert provider.chat_with_retry.await_count == 1
    loop._running = False
    await asyncio.wait_for(run_task, timeout=2)
    assert bus.callback_size == 0


@pytest.mark.asyncio
async def test_cron_create_save_reload_and_run(tmp_path: Path, monkeypatch) -> None:
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule

    calls: list[tuple[str, str | None, str | None]] = []

    async def on_job(job) -> None:
        calls.append((job.id, job.payload.owner_actor, job.payload.session_key))

    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path, on_job=on_job)
    monkeypatch.setattr(service, "_arm_timer", lambda: None)
    service._running = True
    job = service.add_job(
        name="private reminder",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="check the calendar",
        session_key="familia:owner:telegram:200",
        origin_channel="telegram",
        origin_chat_id="200",
        origin_metadata={"actor": "owner", "route": "private"},
        created_by="owner",
        creator_actor="owner",
        target_actor="owner",
        owner_actor="owner",
        tags=["acceptance"],
    )
    assert store_path.is_file()

    restarted = CronService(store_path, on_job=on_job)
    monkeypatch.setattr(restarted, "_arm_timer", lambda: None)
    loaded = restarted.get_job(job.id)
    assert loaded is not None
    assert loaded.payload.owner_actor == "owner"
    assert loaded.payload.session_key == "familia:owner:telegram:200"
    assert loaded.payload.origin_metadata == {"actor": "owner", "route": "private"}
    assert await restarted.run_job(job.id)
    assert calls == [(job.id, "owner", "familia:owner:telegram:200")]

    persisted = json.loads(store_path.read_text(encoding="utf-8"))
    saved = persisted["jobs"][0]
    assert saved["payload"]["ownerActor"] == "owner"
    assert saved["payload"]["creatorActor"] == "owner"
    assert saved["state"]["lastStatus"] == "ok"
    assert saved["state"]["runHistory"][-1]["status"] == "ok"


def test_two_source_session_migration_collision_preserves_first(tmp_path: Path) -> None:
    from familia import session_migration

    source = tmp_path / "source"
    source.mkdir()
    metadata = {
        "_type": "metadata",
        "key": "telegram:shared",
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
        "last_archived": 2,
        "last_consolidated": 2,
        "metadata": {},
    }

    def write_session(path: Path, content: str) -> None:
        path.write_text(
            "\n".join(
                [
                    json.dumps(metadata, sort_keys=True),
                    json.dumps({"role": "user", "actor": "owner", "content": content}),
                    json.dumps({"role": "assistant", "content": "ack"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    write_session(source / "a.jsonl", "first source")
    write_session(source / "b.jsonl", "second source")
    plan = session_migration.analyze_sessions(source, known_actors={"owner"})
    output = tmp_path / "output"
    result = session_migration.apply_migration(plan, output)

    key = "familia:owner:telegram:shared"
    target = output / "sessions" / (
        base64.urlsafe_b64encode(key.encode()).decode().rstrip("=") + ".jsonl"
    )
    assert result.created_targets == 1
    assert result.conflicting_targets == 1
    assert target.read_text(encoding="utf-8").find("first source") >= 0
    conflicts = list((output / "sessions" / "conflicts").glob("*.conflict"))
    assert len(conflicts) == 1
    assert "second source" in conflicts[0].read_text(encoding="utf-8")


def test_admin_model_config_round_trip_keeps_fallback_and_uses_main(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from familia.cli import graph_admin
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import load_config
    from nanobot.runtime_adapters import RuntimeAdapters

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "workspace": str(tmp_path / "workspace"),
                        "model": "openai/main-old",
                        "provider": "openai",
                        "unknownFamiliaField": {"keep": True},
                    }
                },
                "providers": {"openai": {"api_key": "synthetic-key"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAMILIA_CONFIG_FILE", str(config_path))

    assert graph_admin.main(
        ["agents", "set", "main", "--model", "openai/main-selected", "--provider", "openai"]
    ) == 0
    assert graph_admin.main(
        ["agents", "set", "fallback", "--model", "openai/fallback-selected", "--provider", "openai"]
    ) == 0
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["agents"]["defaults"]["model"] == "openai/main-selected"
    assert raw["agents"]["defaults"]["unknownFamiliaField"] == {"keep": True}
    assert raw["agents"]["familia_fallback"]["model"] == "openai/fallback-selected"

    result = graph_admin.main(["agents", "get", "--json"])
    assert result == 0
    shown = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert shown["main"]["model"] == "openai/main-selected"
    assert shown["fallback"]["model"] == "openai/fallback-selected"

    config = load_config(config_path)
    provider = MagicMock()
    provider.get_default_model.return_value = "provider-default"
    loop = AgentLoop.from_config(
        config,
        bus=MessageBus(),
        provider=provider,
        runtime_adapters=RuntimeAdapters(),
    )
    assert loop.model == "openai/main-selected"
    assert getattr(config.agents, "familia_fallback")["model"] == "openai/fallback-selected"
