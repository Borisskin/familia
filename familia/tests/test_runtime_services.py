"""Focused checks for Familia's nanobot 0.3.0 service hooks."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.agent.loop import AgentLoop
from nanobot.providers.base import LLMProvider
from nanobot.runtime_adapters import Admission, RuntimeAdapters

from familia import principals as principals_mod
from familia.nanobot_extension import runtime_services
from familia.policy import GateResult
from familia.principals import Identity, Principal, PrincipalRegistry, get_current_actor, set_current_actor


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    reg = PrincipalRegistry(
        [
            Principal(
                id="owner",
                identities=[Identity(channel="telegram", sender_id="chat-owner")],
                memx_key="owner-key",
            ),
            Principal(
                id="member",
                identities=[Identity(channel="telegram", sender_id="chat-member")],
                memx_key="member-key",
            ),
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", reg)
    return reg


def test_vk_descriptor_is_external_and_name_collision_is_rejected() -> None:
    descriptor = runtime_services.make_vk_channel_plugin()
    assert descriptor.runtime == "familia.channels.vk:VKChannel"
    with pytest.raises(ValueError, match="collision"):
        runtime_services.register_channel_descriptor(descriptor)


@pytest.mark.asyncio
async def test_outbound_guard_uses_context_actor_and_restores_it(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _allow(**_kwargs: object) -> GateResult:
        return GateResult("allow")

    monkeypatch.setattr(runtime_services, "gate_outbound_send", _allow)
    set_current_actor("owner")
    guard = runtime_services.make_outbound_guard()
    request = SimpleNamespace(
        action="message.send",
        outbound=OutboundMessage(channel="telegram", chat_id="chat-member", content="hi"),
        actor="owner",
        inbound_channel="telegram",
        inbound_chat_id="chat-owner",
        publish_outbound=None,
    )
    result = await guard(request)
    assert result.kind == "allow"
    assert get_current_actor() == "owner"
    set_current_actor(None)


@pytest.mark.asyncio
async def test_outbound_guard_rejects_metadata_actor_mismatch(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _gate(**_kwargs: object) -> GateResult:
        nonlocal called
        called = True
        return GateResult("allow")

    monkeypatch.setattr(runtime_services, "gate_outbound_send", _gate)
    set_current_actor("owner")
    guard = runtime_services.make_outbound_guard()
    result = await guard(
        SimpleNamespace(
            action="message.send",
            outbound=OutboundMessage(channel="telegram", chat_id="chat-member", content="hi"),
            actor="member",
            inbound_channel="telegram",
            inbound_chat_id="chat-owner",
            publish_outbound=None,
        )
    )
    assert result.kind == "deny"
    assert called is False
    set_current_actor(None)


def test_service_hooks_bind_real_handlers_not_config_callables() -> None:
    fake_config = SimpleNamespace(
        run_dream=lambda *_args: "wrong",
        run_heartbeat=lambda *_args: "wrong",
        run_scheduled=lambda *_args: "wrong",
        agent=object(),
        cron_service=object(),
    )
    hooks = runtime_services.make_runtime_service_hooks(fake_config)
    assert hooks["run_dream"] is runtime_services.run_dream
    assert hooks["run_heartbeat"] is runtime_services.run_heartbeat
    assert hooks["run_scheduled"] is runtime_services.run_scheduled


def test_internal_turn_targets_actual_agent_loop_seam() -> None:
    admit_params = inspect.signature(AgentLoop._admit_message).parameters
    process_params = inspect.signature(AgentLoop._process_message).parameters
    assert tuple(admit_params) == ("self", "msg")
    assert "request_context" in process_params
    assert "session_key" in process_params


@pytest.mark.asyncio
async def test_internal_turn_uses_actual_process_direct_lock_path(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=256, temperature=0.0, reasoning_effort=None)

    async def admit(message: object) -> Admission:
        return Admission(
            actor="owner",
            session_key=message.session_key_override,
            message=message,
        )

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        runtime_adapters=RuntimeAdapters(admit=admit),
        mcp_servers={},
    )
    monkeypatch.setattr(loop, "_connect_mcp", AsyncMock())
    captured: dict[str, object] = {}

    async def process(message: object, **kwargs: object) -> OutboundMessage:
        captured["message"] = message
        captured["kwargs"] = kwargs
        return OutboundMessage(channel="telegram", chat_id="chat-owner", content="ok")

    monkeypatch.setattr(loop, "_process_message", process)
    result = await runtime_services._process_internal_turn(
        loop,
        prompt="internal",
        actor="owner",
        channel="telegram",
        chat_id="chat-owner",
        session_key="familia:owner:telegram:chat-owner",
    )
    assert result.content == "ok"
    assert "familia:owner:telegram:chat-owner" in loop._session_locks
    request_context = captured["kwargs"]["request_context"]
    assert request_context.actor == "owner"
    assert request_context.session_key == "familia:owner:telegram:chat-owner"


def test_read_archive_batch_uses_catalog_and_quarantines_mixed_group(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "value:private_index": json.dumps(
            [
                {"name": "memory:archive-good", "tags": []},
                {"name": "memory:archive-mixed", "tags": []},
            ]
        ),
        "memory:archive-good": json.dumps(
            [{"role": "user", "content": "owner fact", "actor": "owner"}]
        ),
        "memory:archive-mixed": json.dumps(
            [
                {"role": "user", "content": "owner fact", "actor": "owner"},
                {"role": "assistant", "content": "foreign", "actor": "member"},
            ]
        ),
    }
    client = SimpleNamespace(get=lambda key: values.get(key))
    monkeypatch.setattr(runtime_services, "_private_memory_client", lambda _owner: client)
    result = runtime_services._read_archive_batch("owner")
    assert result is not None
    _client, archive_ids, messages = result
    assert archive_ids == ["archive-good"]
    assert [message["content"] for message in messages] == ["owner fact"]


@pytest.mark.asyncio
async def test_run_dream_reads_owner_archive_and_consumes_after_completion(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def profile_snapshot() -> dict[str, object]:
        return {"value": "profile", "version": 41.0}

    client = SimpleNamespace(
        get=lambda suffix: "" if suffix != "value:user_profile" else "profile",
        get_profile_snapshot=profile_snapshot,
    )
    monkeypatch.setattr(
        runtime_services,
        "_read_archive_batch",
        lambda _owner: (
            client,
            ["archive-hash"],
            [
                {"timestamp": "t1", "content": "owner fact", "actor": "owner", "role": "user"},
            ],
        ),
    )
    dream_tools = SimpleNamespace(
        _familia_dream_results=[("dream_memory_set", "committed: ok")],
    )

    def make_tools(_owner: str, *, profile_version: float | None) -> object:
        captured["profile_version"] = profile_version
        return dream_tools

    monkeypatch.setattr(runtime_services, "_dream_tools", make_tools)
    monkeypatch.setattr(runtime_services, "_delete_archive_facts", lambda *_args: _completed())

    class Loop:
        async def process_direct(self, prompt: str, **kwargs: object) -> object:
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return SimpleNamespace(metadata={"_stop_reason": "completed"})

    result = await runtime_services.run_dream("owner", Loop())
    assert "owner fact" in str(captured["prompt"])
    assert "member fact" not in str(captured["prompt"])
    request_context = captured["kwargs"]["request_context"]
    assert request_context.actor == "owner"
    assert request_context.session_key == "familia:owner:telegram:chat-owner"
    assert captured["profile_version"] == 41.0
    assert result is not None


async def _completed(*_args: object, **_kwargs: object) -> bool:
    return True


@pytest.mark.asyncio
async def test_run_dream_does_not_move_cursor_on_failed_turn(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def profile_snapshot() -> dict[str, object]:
        return {"value": None, "version": None}

    monkeypatch.setattr(
        runtime_services,
        "_read_archive_batch",
        lambda _owner: (
            SimpleNamespace(
                get=lambda _suffix: None,
                get_profile_snapshot=profile_snapshot,
            ),
            ["archive-hash"],
            [{"cursor": 1, "timestamp": "t1", "content": "fact", "actor": "owner", "role": "user"}],
        ),
    )
    monkeypatch.setattr(
        runtime_services,
        "_dream_tools",
        lambda _owner, *, profile_version: SimpleNamespace(_familia_dream_results=[]),
    )

    class Loop:
        async def process_direct(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(metadata={"_stop_reason": "error"})

    assert await runtime_services.run_dream("owner", Loop()) is None


@pytest.mark.asyncio
async def test_run_dream_keeps_archive_when_profile_snapshot_fails(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(
        get_profile_snapshot=AsyncMock(side_effect=RuntimeError("read failed"))
    )
    deleted = AsyncMock(return_value=True)
    monkeypatch.setattr(
        runtime_services,
        "_read_archive_batch",
        lambda _owner: (
            client,
            ["archive-hash"],
            [{"timestamp": "t1", "content": "fact", "actor": "owner", "role": "user"}],
        ),
    )
    monkeypatch.setattr(runtime_services, "_delete_archive_facts", deleted)

    class Loop:
        async def process_direct(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("profile failure must precede the Dream turn")

    assert await runtime_services.run_dream("owner", Loop()) is None
    deleted.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_dream_keeps_all_archives_on_mixed_tool_results(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def profile_snapshot() -> dict[str, object]:
        return {"value": "profile", "version": 41.0}

    client = SimpleNamespace(
        get=lambda _suffix: None,
        get_profile_snapshot=profile_snapshot,
    )
    dream_tools = SimpleNamespace(
        _familia_dream_results=[
            ("dream_memory_set", "committed: profile"),
            ("dream_memory_set", "Error: profile_conflict: profile changed"),
        ]
    )
    deleted = AsyncMock(return_value=True)
    monkeypatch.setattr(
        runtime_services,
        "_read_archive_batch",
        lambda _owner: (
            client,
            ["archive-first", "archive-second"],
            [{"timestamp": "t1", "content": "fact", "actor": "owner", "role": "user"}],
        ),
    )
    monkeypatch.setattr(
        runtime_services,
        "_dream_tools",
        lambda _owner, *, profile_version: dream_tools,
    )
    monkeypatch.setattr(runtime_services, "_delete_archive_facts", deleted)

    class Loop:
        async def process_direct(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(metadata={"_stop_reason": "completed"})

    assert await runtime_services.run_dream("owner", Loop()) is None
    deleted.assert_not_awaited()


@pytest.mark.asyncio
async def test_dream_cleanup_checks_every_policy_key_before_deleting(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from familia.tools import memory as memory_mod

    policy = MagicMock(side_effect=[None, "Error: Policy denied memory.write"])
    ingestor_type = MagicMock()
    monkeypatch.setattr(memory_mod, "_check_memory_write_policy", policy)
    monkeypatch.setattr(
        "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
        ingestor_type,
    )

    assert not await runtime_services._delete_archive_facts(
        "owner",
        ["archive-first", "archive-second"],
    )
    assert [call.kwargs["full_key"] for call in policy.call_args_list] == [
        "private:owner:memory:archive-first",
        "private:owner:memory:archive-second",
    ]
    ingestor_type.assert_not_called()


@pytest.mark.asyncio
async def test_run_heartbeat_uses_memx_owner_route_and_gate(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[object, str, object, str]] = []

    class Sessions:
        def get_or_create(self, _key: str) -> object:
            return SimpleNamespace(retain_recent_legal_suffix=lambda _keep: None)

        def save(self, _session: object) -> None:
            return None

    class Loop:
        enabled_channels = {"telegram"}
        sessions = Sessions()
        tools = SimpleNamespace(get=lambda _name: None)

        async def process_direct(self, *_args: object, **_kwargs: object) -> OutboundMessage:
            return OutboundMessage(channel="telegram", chat_id="chat-owner", content="done")

        async def _publish_outbound(self, message: object, **kwargs: object) -> None:
            sent.append((message, str(kwargs["actor"]), kwargs["inbound"], str(kwargs["action"])))

    async def fake_source(_actor: str) -> str:
        return "## Active Tasks\n- check"

    async def allow_notify(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(runtime_services, "_heartbeat_source", fake_source)
    monkeypatch.setattr(runtime_services, "_heartbeat_should_notify", allow_notify)
    result = await runtime_services.run_heartbeat("owner", Loop())
    assert result == "done"
    assert len(sent) == 1
    assert sent[0][1] == "owner"
    assert sent[0][3] == "heartbeat"


@pytest.mark.asyncio
async def test_scheduled_dream_fans_out_registered_owners_and_keeps_user_name_bound(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nanobot.cron.types import CronJob, CronPayload

    calls: list[str] = []

    async def fake_dream(actor: str, _loop: object) -> str:
        calls.append(actor)
        if actor == "member":
            raise RuntimeError("member failed")
        return "owner completed"

    monkeypatch.setattr(runtime_services, "run_dream", fake_dream)

    class Loop:
        enabled_channels = {"telegram"}
        cron_service = object()

    system_job = CronJob(
        id="dream",
        name="dream",
        payload=CronPayload(
            kind="system_event",
            origin_metadata={"_familia_system_job": "dream"},
        ),
    )
    result = await runtime_services.run_scheduled(system_job, Loop())

    assert calls == ["owner", "member"]
    assert result == "owner completed\nDream failed for member: member failed"

    bound = CronJob(
        id="user-dream",
        name="dream",
        payload=CronPayload(
            kind="agent_turn",
            session_key="familia:owner:telegram:chat-owner",
            origin_channel="telegram",
            origin_chat_id="chat-owner",
            origin_metadata={"actor": "owner"},
        ),
    )

    async def bound_handler(_job: object) -> str:
        return "bound"

    monkeypatch.setattr(runtime_services, "make_scheduled_handler", lambda *_args: bound_handler)
    assert await runtime_services.run_scheduled(bound, Loop()) == "bound"
    assert calls == ["owner", "member"]


@pytest.mark.asyncio
async def test_cron_timer_dispatches_system_fanout_and_user_dream_name(
    registry: PrincipalRegistry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore

    dream_calls: list[str] = []
    bound_calls: list[str] = []

    async def fake_dream(actor: str, _loop: object) -> str:
        dream_calls.append(actor)
        if actor == "member":
            raise RuntimeError("member failed")
        return "owner completed"

    def fake_bound_handler(_agent: object, _cron: object):
        async def _run(job: object) -> str:
            bound_calls.append(job.id)
            return "bound"

        return _run

    monkeypatch.setattr(runtime_services, "run_dream", fake_dream)
    monkeypatch.setattr(runtime_services, "make_scheduled_handler", fake_bound_handler)

    system = CronJob(
        id="dream",
        name="dream",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(
            kind="system_event",
            origin_metadata={"_familia_system_job": "dream"},
        ),
        state=CronJobState(next_run_at_ms=1),
    )
    user = CronJob(
        id="user-dream",
        name="dream",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(
            kind="agent_turn",
            session_key="familia:owner:telegram:chat-owner",
            origin_channel="telegram",
            origin_chat_id="chat-owner",
            origin_metadata={"actor": "owner"},
            target_actor="owner",
        ),
        state=CronJobState(next_run_at_ms=1),
    )

    class Loop:
        enabled_channels = {"telegram"}
        cron_service = None

        async def run_scheduled(self, job: CronJob) -> object:
            return await runtime_services.run_scheduled(job, self)

    loop = Loop()
    cron = CronService(tmp_path / "jobs.json", on_job=loop.run_scheduled)
    cron._running = True
    cron._store = CronStore(version=2, jobs=[system, user])
    cron._save_store()
    monkeypatch.setattr(cron, "_arm_timer", lambda: None)
    loop.cron_service = cron

    await cron._on_timer()

    assert dream_calls == ["owner", "member"]
    assert bound_calls == ["user-dream"]
    assert next(job for job in cron._store.jobs if job.id == "dream").state.last_status == "ok"
