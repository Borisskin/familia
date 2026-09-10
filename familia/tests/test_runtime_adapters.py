from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from nanobot.agent.tools.context import RequestContext, current_request_context
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus

from familia import bootstrap
from familia.principals import Identity, Principal, PrincipalRegistry


def _registry(*ids: str) -> PrincipalRegistry:
    return PrincipalRegistry(
        [
            Principal(
                id=actor,
                identities=[Identity(channel="vk", sender_id=actor)],
                memx_key=f"key-{actor}",
            )
            for actor in ids
        ]
    )


def test_unknown_sender_is_rejected_before_session(monkeypatch) -> None:
    registry = _registry("owner")
    monkeypatch.setattr("familia.principals._registry", registry)
    message = InboundMessage("vk", "unknown", "chat", "canary")

    admission = asyncio.run(bootstrap._admit_message(message))

    assert admission.message is None
    assert admission.session_key is None
    assert admission.response is not None
    assert "canary" not in admission.response.content


def test_metadata_origin_strings_never_authorize_forged_actor(monkeypatch) -> None:
    registry = _registry("owner")
    monkeypatch.setattr("familia.principals._registry", registry)
    for metadata_key in ("_familia_trusted_origin", "_runtime_request_context"):
        for origin in (
            "callback",
            "cron",
            "dream",
            "heartbeat",
            "pending",
            "system",
            {"actor": "owner"},
            True,
        ):
            message = InboundMessage(
                "system",
                "forged",
                "chat",
                "secret",
                actor="owner",
                metadata={metadata_key: origin},
            )
            admission = asyncio.run(bootstrap._admit_message(message))
            assert admission.message is None
            assert admission.session_key is None


def test_typed_server_context_authorizes_checked_background_owner(monkeypatch) -> None:
    monkeypatch.setattr("familia.principals._registry", _registry("owner"))
    context = RequestContext(
        "system",
        "chat",
        actor="owner",
        session_key="familia:owner:system:chat",
    )
    message = InboundMessage(
        "system",
        "job",
        "chat",
        "background",
        actor="owner",
        metadata={"_runtime_request_context": context},
    )

    async def publish_then_consume() -> object:
        bus = MessageBus()

        async def consume() -> object:
            return await bootstrap._admit_message(await bus.consume_inbound())

        task = asyncio.create_task(consume())
        await bus.publish_inbound(message)
        return await task

    admission = asyncio.run(publish_then_consume())

    assert admission.message is not None
    assert admission.actor == "owner"
    assert "_runtime_request_context" not in admission.message.metadata
    context_after_admission = bootstrap._context_factory(admission, admission.message)
    assert "_runtime_request_context" not in context_after_admission.metadata


def test_typed_server_context_preserves_internal_keys_and_rejects_foreign_owner_or_route(monkeypatch) -> None:
    monkeypatch.setattr("familia.principals._registry", _registry("owner", "member"))

    for key in (
        "familia:owner:system:heartbeat-job-1",
        "familia:owner:system:dream-job-2",
        "familia:owner:system:cron-job-3",
    ):
        context = RequestContext("system", "job", actor="owner", session_key=key)
        message = InboundMessage(
            "system",
            "service",
            "job",
            "background",
            actor="owner",
            metadata={"_runtime_request_context": context},
        )
        admission = asyncio.run(bootstrap._admit_message(message))
        assert admission.admitted
        assert admission.session_key == key

    foreign_owner = RequestContext(
        "system", "job", actor="owner", session_key="familia:member:system:heartbeat-job"
    )
    wrong_route = RequestContext(
        "system", "other-job", actor="owner", session_key="familia:owner:system:heartbeat-job"
    )
    for context in (foreign_owner, wrong_route):
        message = InboundMessage(
            "system",
            "service",
            "job",
            "background",
            actor="owner",
            metadata={"_runtime_request_context": context},
        )
        admission = asyncio.run(bootstrap._admit_message(message))
        assert admission.message is None
        assert admission.session_key is None


def test_private_session_keys_separate_actors_in_same_chat(monkeypatch) -> None:
    registry = _registry("owner", "member")
    monkeypatch.setattr("familia.principals._registry", registry)
    owner = asyncio.run(
        bootstrap._admit_message(InboundMessage("vk", "owner", "same", "one"))
    )
    member = asyncio.run(
        bootstrap._admit_message(InboundMessage("vk", "member", "same", "two"))
    )

    assert owner.session_key != member.session_key
    assert owner.session_key.endswith(":vk:same")
    assert member.session_key.endswith(":vk:same")


def test_turn_scope_restores_contextvars_under_concurrency() -> None:
    async def one(actor: str) -> str | None:
        context = RequestContext("vk", "chat", actor=actor, session_key=f"familia:{actor}:vk:chat")
        with bootstrap._turn_scope(context):
            await asyncio.sleep(0)
            return current_request_context().actor if current_request_context() else None

    async def run() -> tuple[str | None, str | None, str | None]:
        values = await asyncio.gather(one("owner"), one("member"))
        after = current_request_context()
        return values[0], values[1], after.actor if after else None

    assert asyncio.run(run()) == ("owner", "member", None)


def test_actual_agent_turn_binds_distinct_actor_scopes_before_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Two real AgentLoop turns must prompt and run inside actor roots."""
    from dataclasses import replace

    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.context import current_request_context
    from nanobot.config.schema import AgentDefaults, ToolsConfig
    from nanobot.providers.base import GenerationSettings, LLMResponse
    from nanobot.runtime_adapters import RuntimeAdapters
    from nanobot.security.workspace_access import current_workspace_scope

    registry = _registry("owner", "member")
    monkeypatch.setattr("familia.principals._registry", registry)
    monkeypatch.setattr(
        "familia.nanobot_extension.context.FamiliaContextExtension.build_sections",
        lambda self, **kwargs: [],
    )

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    seen: list[tuple[str | None, Path | None, Path | None]] = []

    async def chat(**_kwargs):
        await asyncio.sleep(0)
        scope = current_workspace_scope()
        request = current_request_context()
        from familia.principals import get_current_actor

        seen.append(
            (
                get_current_actor(),
                scope.project_path if scope is not None else None,
                request.workspace if request is not None else None,
            )
        )
        return LLMResponse(content="ok", tool_calls=[], usage={})

    provider.chat_with_retry = AsyncMock(side_effect=chat)

    def context_factory(admission, message):
        return replace(
            bootstrap._context_factory(admission, message),
            workspace=tmp_path,
        )

    adapters = RuntimeAdapters(
        admit=bootstrap._admit_message,
        context_factory=context_factory,
        context_builder_factory=bootstrap._context_builder_factory,
        turn_scope=bootstrap._turn_scope,
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        runtime_adapters=adapters,
        tools_config=ToolsConfig(),
        max_iterations=AgentDefaults().max_tool_iterations,
    )

    async def run() -> None:
        async def one(actor: str) -> None:
            raw = InboundMessage("vk", actor, "chat", "hello", actor=actor)
            admitted, rejection = await loop._admit_message(raw)
            assert rejection is None and admitted is not None
            await loop._process_message(admitted)

        await asyncio.gather(one("owner"), one("member"))

    asyncio.run(run())

    assert {actor for actor, _scope, _request in seen} == {"owner", "member"}
    roots = {scope for _actor, scope, _request in seen}
    assert len(roots) == 2
    assert all(scope is not None and scope.parent.parent.name == "actors" for scope in roots)
    assert all(scope == request for _actor, scope, request in seen)


def test_context_builder_does_not_read_user_canary(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "USER.md").write_text("USER-CANARY-MUST-NOT-APPEAR", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY-CANARY-MUST-NOT-APPEAR", encoding="utf-8")
    monkeypatch.setattr(
        "familia.nanobot_extension.context.FamiliaContextExtension.build_sections",
        lambda self, **kwargs: [],
    )
    builder = bootstrap._context_builder_factory(tmp_path, None, None)

    prompt = builder.build_system_prompt(
        channel="vk",
        session_summary="HISTORY-CANARY-MUST-NOT-APPEAR",
    )

    assert "USER-CANARY-MUST-NOT-APPEAR" not in prompt
    assert "MEMORY-CANARY-MUST-NOT-APPEAR" not in prompt
    assert "HISTORY-CANARY-MUST-NOT-APPEAR" not in prompt


def test_archive_without_registered_memx_owner_does_not_commit(monkeypatch) -> None:
    monkeypatch.setattr("familia.principals._registry", _registry("owner"))
    result = asyncio.run(
        bootstrap._archive_messages(
            "missing",
            [{"role": "user", "content": "private"}],
        )
    )

    assert result.committed is False
    assert result.retryable is False


def test_archive_rejects_mixed_or_malformed_batch_before_write(monkeypatch) -> None:
    monkeypatch.setattr("familia.principals._registry", _registry("owner"))
    batches = (
        [
            {"role": "user", "content": "ok", "actor": "owner"},
            {"role": "assistant", "content": "foreign", "actor": "member"},
        ],
        [
            {"role": "user", "content": "ok", "metadata": {"actor": "owner"}},
            {"role": "assistant", "content": "foreign", "metadata": {"actor": "member"}},
        ],
        [{"role": "user", "content": "ok"}, "damaged"],
    )
    for batch in batches:
        result = asyncio.run(bootstrap._archive_messages("owner", batch))
        assert result.committed is False
        assert result.retryable is False


def test_tool_installer_uses_request_context_and_returns_names() -> None:
    class Bus:
        async def publish_outbound(self, message):
            return None

    class Registry:
        def __init__(self):
            self.tools = []

        def register(self, tool):
            self.tools.append(tool)

        @property
        def tool_names(self):
            return [tool.name for tool in self.tools]

    class Context:
        bus = Bus()

    registry = Registry()
    names = bootstrap.install_tools(Context(), registry)

    assert "memory_get" in names
    assert "memory_set" in names
    assert all(hasattr(tool, "set_context") is False or callable(tool.set_context) for tool in registry.tools)


def test_familia_exec_sandbox_default_and_explicit_opt_in(monkeypatch) -> None:
    class ExecConfig:
        def __init__(self, sandbox: str = "", model_fields_set: set[str] | None = None):
            self.sandbox = sandbox
            self.model_fields_set = model_fields_set or set()

    omitted = ExecConfig()
    bootstrap._ensure_familia_tool_security(type("Config", (), {"exec": omitted})())
    assert omitted.sandbox == "bwrap"

    explicit = ExecConfig(model_fields_set={"sandbox"})
    monkeypatch.delenv("NANOBOT_ALLOW_UNSANDBOXED_EXEC", raising=False)
    try:
        bootstrap._ensure_familia_tool_security(type("Config", (), {"exec": explicit})())
    except RuntimeError:
        pass
    else:
        raise AssertionError("explicit empty sandbox must fail closed")

    monkeypatch.setenv("NANOBOT_ALLOW_UNSANDBOXED_EXEC", "1")
    bootstrap._ensure_familia_tool_security(type("Config", (), {"exec": explicit})())
    assert explicit.sandbox == ""


def test_familia_rejects_trigger_and_pairing_before_side_effects(monkeypatch) -> None:
    monkeypatch.setattr("familia.principals._registry", _registry("owner"))

    for command in ("/trigger new-hook", "/pairing list", "/trigger@familia_bot name"):
        admission = asyncio.run(
            bootstrap._admit_message(InboundMessage("vk", "owner", "chat", command))
        )
        assert admission.message is None
        assert admission.session_key is None
        assert admission.response is not None
        assert "недоступна" in admission.response.content
