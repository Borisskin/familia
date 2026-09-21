from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

from familia.nanobot_extension import cron as cron_extension
from familia.principals import Identity, Principal, PrincipalRegistry
from nanobot.agent.loop import AgentLoop
from nanobot.agent.outbound import OutboundDecision
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.outbound_events import StreamDeltaEvent, StreamedResponseEvent
from nanobot.cron.session_turns import CRON_TRIGGER_META


CREATOR = "creator"
RECIPIENT = "recipient"


def _message(*, chat_id: str = "recipient-vk", event: Any = None) -> OutboundMessage:
    metadata = {CRON_TRIGGER_META: {"job_id": "job-1", "run_id": "run-1"}}
    return OutboundMessage(
        channel="vk",
        chat_id=chat_id,
        content="final cron reply",
        event=event,
        metadata=metadata,
    )


def _registry(*, ambiguous: bool = False) -> PrincipalRegistry:
    principals = [
        Principal(CREATOR, identities=[Identity("vk", "creator-vk")]),
        Principal(
            RECIPIENT,
            identities=[
                Identity("vk", "recipient-vk"),
                Identity("telegram", "recipient-tg|username"),
                Identity("email", "recipient@example.test"),
            ],
        ),
    ]
    if ambiguous:
        principals.append(Principal("second", identities=[Identity("vk", "recipient-vk")]))
    return PrincipalRegistry(principals)


def _observer(monkeypatch: pytest.MonkeyPatch, publish: Any, *, ambiguous: bool = False) -> Any:
    monkeypatch.setattr(cron_extension, "get_registry", lambda: _registry(ambiguous=ambiguous))
    return cron_extension.make_cron_delivery_observer(
        publish,
        SimpleNamespace(),
        lambda: ["vk", "telegram", "email"],
    )


@pytest.mark.asyncio
async def test_fallback_uses_only_recipient_other_channel_and_preserves_policy_context(monkeypatch) -> None:
    attempts: list[tuple[OutboundMessage, dict[str, Any]]] = []

    async def publish(message: OutboundMessage, **kwargs: Any) -> OutboundDecision:
        attempts.append((message, kwargs))
        return OutboundDecision.allow(message)

    observer = _observer(monkeypatch, publish)
    source = _message()
    observer.register(source, CREATOR)

    await observer(source, "unavailable")

    assert len(attempts) == 1
    fallback, kwargs = attempts[0]
    assert (fallback.channel, fallback.chat_id) == ("telegram", "recipient-tg")
    assert kwargs["actor"] == CREATOR
    assert kwargs["action"] == "message.send"
    assert (kwargs["inbound"].channel, kwargs["inbound"].chat_id) == ("vk", "recipient-vk")
    assert fallback.chat_id != "creator-vk"

    await observer(fallback, "delivered")
    await observer(fallback, "unavailable")
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_confirmed_unavailable_alternative_advances_once_then_unknown_stops(monkeypatch) -> None:
    attempts: list[OutboundMessage] = []

    async def publish(message: OutboundMessage, **_kwargs: Any) -> OutboundDecision:
        attempts.append(message)
        return OutboundDecision.allow(message)

    observer = _observer(monkeypatch, publish)
    source = _message()
    observer.register(source, CREATOR)

    await observer(source, "unavailable")
    await observer(attempts[0], "unavailable")
    assert [(message.channel, message.chat_id) for message in attempts] == [
        ("telegram", "recipient-tg"),
        ("email", "recipient@example.test"),
    ]

    await observer(attempts[1], "unknown")
    await observer(attempts[1], "unavailable")
    assert len(attempts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["delivered", "unknown"])
async def test_confirmed_delivery_or_unknown_never_starts_fallback(monkeypatch, result: str) -> None:
    attempts: list[OutboundMessage] = []

    async def publish(message: OutboundMessage, **_kwargs: Any) -> OutboundDecision:
        attempts.append(message)
        return OutboundDecision.allow(message)

    observer = _observer(monkeypatch, publish)
    source = _message()
    observer.register(source, CREATOR)

    await observer(source, result)

    assert attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["deny", "asked"])
async def test_ambiguous_recipient_and_policy_refusal_stop_without_second_attempt(monkeypatch, kind: str) -> None:
    attempts: list[OutboundMessage] = []

    async def refused(message: OutboundMessage, **_kwargs: Any) -> OutboundDecision:
        attempts.append(message)
        if kind == "deny":
            return OutboundDecision.deny("policy denied")
        return OutboundDecision.asked("approval required", "owner")

    source = _message()
    observer = _observer(monkeypatch, refused, ambiguous=True)
    observer.register(source, CREATOR)
    await observer(source, "unavailable")
    assert attempts == []

    observer = _observer(monkeypatch, refused)
    observer.register(source, CREATOR)
    await observer(source, "unavailable")
    await observer(source, "unavailable")
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_only_registered_final_cron_message_can_trigger_one_fallback(monkeypatch) -> None:
    attempts: list[OutboundMessage] = []

    async def publish(message: OutboundMessage, **_kwargs: Any) -> OutboundDecision:
        attempts.append(message)
        return OutboundDecision.allow(message)

    observer = _observer(monkeypatch, publish)
    source = _message()

    await observer(_message(), "unavailable")
    observer.register(source, CREATOR)
    observer.register(replace(source, chat_id="forged-route"), CREATOR)
    await observer(replace(source, event=StreamDeltaEvent(content="partial")), "unavailable")
    await observer(replace(source, chat_id="forged-route"), "unavailable")
    assert attempts == []

    await observer(replace(source, event=StreamedResponseEvent()), "unavailable")
    await observer(source, "unavailable")
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_scheduled_handler_registers_the_runner_created_context(monkeypatch) -> None:
    attempts: list[OutboundMessage] = []
    source = _message()

    @dataclass(frozen=True)
    class Payload:
        session_key: str
        origin_channel: str
        origin_chat_id: str

    @dataclass(frozen=True)
    class Job:
        id: str
        payload: Payload

    async def publish(message: OutboundMessage, **_kwargs: Any) -> OutboundDecision:
        attempts.append(message)
        return OutboundDecision.allow(message)

    async def run_bound(_job: Any, *, agent: Any, cron: Any) -> None:
        await agent.submit_cron_turn(
            InboundMessage(
                channel=source.channel,
                sender_id="cron",
                chat_id=source.chat_id,
                content="scheduled",
                metadata=source.metadata,
                session_key_override="private-key",
            )
        )

    class Agent:
        tools: dict[str, Any] = {}

        def __init__(self) -> None:
            self.submitted: InboundMessage | None = None

        async def submit_cron_turn(self, message: InboundMessage) -> None:
            self.submitted = message
            return None

    monkeypatch.setattr(cron_extension, "_job_actor_and_session", lambda _job: (CREATOR, "private-key"))
    import nanobot.cron.bound_runner as bound_runner

    monkeypatch.setattr(bound_runner, "run_bound_cron_job", run_bound)
    observer = _observer(monkeypatch, publish)
    agent = Agent()
    handler = cron_extension.make_scheduled_handler(
        agent,
        SimpleNamespace(_familia_cron_delivery_observer=observer),
    )

    await handler(Job("job-1", Payload("private-key", "vk", "recipient-vk")))
    assert agent.submitted is not None
    await observer(agent.submitted, "unavailable")

    assert [(message.channel, message.chat_id) for message in attempts] == [("telegram", "recipient-tg")]


@pytest.mark.asyncio
async def test_neutral_publisher_returns_the_policy_decision() -> None:
    sent: list[OutboundMessage] = []

    class Bus:
        async def publish_outbound(self, message: OutboundMessage) -> None:
            sent.append(message)

    async def guard(_request: Any) -> OutboundDecision:
        return OutboundDecision.deny("policy denied")

    loop = SimpleNamespace(runtime_adapters=SimpleNamespace(outbound_guard=guard), bus=Bus())
    decision = await AgentLoop._publish_outbound(loop, _message(), actor=CREATOR, action="message.send")

    assert decision.kind == "deny"
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_result", "gate_kind", "expected_routes"),
    [
        ("unavailable", "allow", [("telegram", "recipient-tg")]),
        ("unavailable", "deny", []),
        ("unavailable", "asked", []),
        ("unknown", "allow", []),
    ],
)
async def test_fallback_rebinds_server_context_for_real_guard_after_turn(
    monkeypatch,
    tmp_path,
    delivery_result: str,
    gate_kind: str,
    expected_routes: list[tuple[str, str]],
) -> None:
    from unittest.mock import MagicMock

    from familia import principals as principals_mod
    from familia.nanobot_extension import runtime_services
    from familia.policy import GateResult
    from familia.principals import get_current_actor, set_current_actor
    from nanobot.agent.tools.context import RequestContext, current_request_context, request_context
    from nanobot.agent.turn_delivery import TurnDeliveryFactory
    from nanobot.bus.queue import MessageBus
    from nanobot.runtime_adapters import RuntimeAdapters

    registry = _registry()
    monkeypatch.setattr(principals_mod, "_registry", registry)
    monkeypatch.setattr(cron_extension, "get_registry", lambda: registry)
    seen: list[RequestContext] = []

    async def gate(**_kwargs: Any) -> GateResult:
        request = current_request_context()
        assert request is not None
        seen.append(request)
        assert request.actor == CREATOR
        assert request.metadata == {"_familia_server_cron": True}
        assert get_current_actor() == CREATOR
        return GateResult(gate_kind)

    monkeypatch.setattr(runtime_services, "gate_outbound_send", gate)
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        turn_delivery_factory=TurnDeliveryFactory(bus),
        runtime_adapters=RuntimeAdapters(outbound_guard=runtime_services.make_outbound_guard()),
    )
    observer = cron_extension.make_cron_delivery_observer(
        loop._publish_outbound,
        SimpleNamespace(),
        lambda: ["vk", "telegram"],
    )
    source = _message()
    observer.register(source, CREATOR)
    previous_actor = get_current_actor()
    set_current_actor(None)
    try:
        with request_context(
            RequestContext(channel="vk", chat_id="recipient-vk", actor=CREATOR)
        ):
            pass
        assert current_request_context() is None
        assert get_current_actor() is None

        await observer(source, delivery_result)

        published: list[OutboundMessage] = []
        while bus.outbound_size:
            published.append(await bus.consume_outbound())
        assert [(message.channel, message.chat_id) for message in published] == expected_routes
        assert (len(seen) == 1) is (delivery_result == "unavailable")
        assert get_current_actor() is None
        assert current_request_context() is None
    finally:
        set_current_actor(previous_actor)


def test_standard_v2_cron_keeps_owner_bound_to_current_origin(tmp_path, monkeypatch) -> None:
    from familia import principals as principals_mod
    from nanobot.agent.tools.context import RequestContext, request_context
    from nanobot.agent.tools.cron import CronTool
    from nanobot.cron.service import CronService

    registry = PrincipalRegistry(
        [
            Principal(
                "owner",
                identities=[Identity("vk", "owner-vk"), Identity("telegram", "owner-tg")],
            )
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", registry)
    service = CronService(tmp_path / "jobs.json")
    tool = CronTool(service)

    with request_context(
        RequestContext(
            channel="vk",
            chat_id="owner-vk",
            session_key="familia:owner:vk:owner-vk",
            metadata={"actor": "owner"},
            actor="owner",
        )
    ):
        result = tool._add_job("owner reminder", "check calendar", 60, None, None, None, None)

    job = service.list_jobs()[0]
    owner, private_key = cron_extension._job_actor_and_session(job)
    assert result.startswith("Created job")
    assert (job.payload.origin_channel, job.payload.origin_chat_id) == ("vk", "owner-vk")
    assert (job.payload.channel, job.payload.to, job.payload.target_actor) == (None, None, None)
    assert (owner, private_key) == ("owner", "familia:owner:vk:owner-vk")


def test_familia_runtime_exposes_the_delivery_observer_factory() -> None:
    from familia.nanobot_extension.runtime_services import make_runtime_service_hooks
    from nanobot.runtime_adapters import RuntimeAdapters

    adapters = RuntimeAdapters(**make_runtime_service_hooks())
    assert adapters.make_delivery_observer is cron_extension.make_cron_delivery_observer
