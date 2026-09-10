"""Focused Dream manager and private-memory CAS regressions."""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest

from familia import principals as principals_mod
from familia.bootstrap import make_dream_turn_context
from familia.nanobot_extension.cron import make_dream_tool_installers
from familia.policy import Decision, PolicyEngine, PolicyRule
from familia.principals import Identity, Principal, PrincipalRegistry, set_current_actor
from familia.private_session_owner import PrivateSessionOwnerResolver
from familia.tools import dream_memory as dream_memory_mod
from nanobot.agent.memory import Consolidator, Dream, MemoryStore
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import Session


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    value = PrincipalRegistry(
        [
            Principal(
                id="actor_alpha",
                display_name="Actor Alpha",
                identities=[Identity(channel="telegram", sender_id="private-chat")],
                memx_key="alpha-key",
                roles=[],
            ),
            Principal(
                id="actor_beta",
                display_name="Actor Beta",
                identities=[Identity(channel="telegram", sender_id="private-chat-beta")],
                memx_key="beta-key",
                roles=[],
            ),
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", value)
    return value


class _ScriptedProvider:
    def __init__(
        self,
        analysis: str,
        tool_calls: list[ToolCallRequest],
        *,
        phase1_finish_reason: str = "stop",
    ) -> None:
        self.analysis = analysis
        self.tool_calls = tool_calls
        self.phase1_finish_reason = phase1_finish_reason
        self.phase2_calls = 0
        self.tool_results: list[str] = []

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        if kwargs.get("tools") is None:
            return LLMResponse(
                content=self.analysis,
                finish_reason=self.phase1_finish_reason,
            )
        self.phase2_calls += 1
        if self.phase2_calls == 1:
            return LLMResponse(
                content="",
                tool_calls=self.tool_calls,
                finish_reason="tool_calls",
            )
        self.tool_results = [
            str(message.get("content") or "")
            for message in kwargs.get("messages", [])
            if message.get("role") == "tool"
        ]
        return LLMResponse(content="done")


def _call(call_id: str, **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="dream_memory_set",
        arguments=arguments,
    )


def _mock_memx_transport(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import httpx

    requests: list[Any] = []
    client_type = httpx.AsyncClient

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404, request=request)
        if request.url.path.endswith("/delete"):
            payload = {
                "ok": True,
                "status": "deleted",
                "committed": True,
                "updated": True,
                "retryable": False,
                "version": None,
            }
        else:
            payload = {
                "ok": True,
                "status": "committed",
                "committed": True,
                "updated": True,
                "retryable": False,
                "version": 1,
            }
        return httpx.Response(200, json=payload, request=request)

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        "familia.principal_memory_ingestor.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    return requests


def _dream(
    tmp_path: Path,
    provider: _ScriptedProvider,
    *,
    ingestor: Any | None = None,
    server_principal_getter: Any | None = None,
) -> tuple[Dream, MemoryStore]:
    installers = make_dream_tool_installers()
    if ingestor is not None:
        def install_dream_memory_tool(registry, _store) -> None:
            registry.unregister("edit_file")
            registry.register(
                dream_memory_mod.DreamMemorySetTool(
                    ingestor=ingestor,
                    server_principal_getter=server_principal_getter,
                )
            )

        installers = [install_dream_memory_tool]

    store = MemoryStore(tmp_path)
    dream = Dream(
        store=store,
        provider=provider,
        model="test-model",
        max_batch_size=10,
        dream_tool_installers=installers,
        dream_turn_context=make_dream_turn_context(),
    )
    return dream, store


def _denied_memory_dream(tmp_path: Path) -> tuple[Dream, MemoryStore]:
    provider = _ScriptedProvider(
        "[MEMORY] kind=memory fact_id=employment.current value=перестал работать",
        [
            _call(
                "memory",
                kind="memory",
                fact_id="employment.current",
                value="перестал работать",
            )
        ],
    )
    ingestor = Mock()
    ingestor.ingest = AsyncMock(
        return_value="denied_invalid: automatic memory operation was rejected"
    )
    return _dream(
        tmp_path,
        provider,
        ingestor=ingestor,
        server_principal_getter=lambda: "actor_alpha",
    )


@pytest.mark.asyncio
async def test_private_archive_rejects_denied_invalid_required_memory(
    tmp_path: Path,
) -> None:
    dream, _store = _denied_memory_dream(tmp_path)

    with pytest.raises(RuntimeError):
        await dream.archive_private(
            "actor_alpha",
            [{"role": "user", "content": "Я перестал работать"}],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("analysis", "operation", "decision"),
    [
        (
            "[PROFILE] kind=profile value=обновлённый профиль",
            {"kind": "profile", "fact_id": None, "value": "обновлённый профиль"},
            Decision.DENY,
        ),
        (
            "[MEMORY] kind=memory fact_id=fact-17 value=секретный факт",
            {"kind": "memory", "fact_id": "fact-17", "value": "секретный факт"},
            Decision.ASK,
        ),
        (
            "[DELETE] kind=delete fact_id=fact-17",
            {"kind": "delete", "fact_id": "fact-17", "value": None},
            Decision.DENY,
        ),
    ],
)
async def test_policy_refusal_preserves_session_history_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registry: PrincipalRegistry,
    analysis: str,
    operation: dict[str, Any],
    decision: Decision,
) -> None:
    from familia.tools import memory as memory_mod

    kind = operation["kind"]
    policy_key = (
        "private:actor_alpha:value:user_profile"
        if kind == "profile"
        else f"private:actor_alpha:memory:{operation['fact_id']}"
    )
    policy = PolicyEngine(
        [
            PolicyRule(
                name="deny automatic writes",
                action=["memory.write"],
                actor=["dream_consolidator"],
                to_chat=[policy_key],
                decision=decision,
                reason="blocked",
            )
        ]
    )
    monkeypatch.setattr(memory_mod, "get_engine", lambda: policy)
    requests = _mock_memx_transport(monkeypatch)
    from familia.principal_memory_ingestor import PrincipalMemoryIngestor

    ingestor = PrincipalMemoryIngestor(
        base_url="http://memx.test",
        api_key="synthetic-key",
    )
    provider = _ScriptedProvider(analysis, [_call("operation", **operation)])
    dream, store = _dream(
        tmp_path,
        provider,
        ingestor=ingestor,
        server_principal_getter=lambda: "actor_alpha",
    )
    session = Session(key="telegram:private-chat")
    session.add_message("user", "old user message")
    session.add_message("assistant", "old assistant message")
    session.add_message("user", "current user message")
    session.add_message("assistant", "current assistant message")
    expected_messages = [dict(message) for message in session.messages]
    expected_last_consolidated = session.last_consolidated
    sessions = Mock()
    sessions.get_or_create.return_value = session
    consolidator = Consolidator(
        store=store,
        provider=provider,
        model="test-model",
        sessions=sessions,
        context_window_tokens=100,
        build_messages=Mock(return_value=[]),
        get_tool_definitions=Mock(return_value=[]),
        max_completion_tokens=0,
        archive_sink=dream.archive_private,
        private_session_owner_resolver=PrivateSessionOwnerResolver(
            lambda: registry
        ),
    )
    consolidator._SAFETY_BUFFER = 0
    consolidator.estimate_session_prompt_tokens = Mock(
        side_effect=[(1000, "test"), (0, "test")]
    )
    consolidator.pick_consolidation_boundary = Mock(return_value=(2, 500))

    with pytest.raises(RuntimeError):
        await consolidator.maybe_consolidate_by_tokens(session)

    assert session.messages == expected_messages
    assert session.last_consolidated == expected_last_consolidated
    sessions.save.assert_not_called()
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [Decision.DENY, Decision.ASK])
async def test_partial_policy_refusal_preserves_prior_commit_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registry: PrincipalRegistry,
    decision: Decision,
) -> None:
    from familia.tools import memory as memory_mod

    first_key = "private:actor_alpha:memory:first-fact"
    second_key = "private:actor_alpha:memory:second-fact"
    policy = PolicyEngine(
        [
            PolicyRule(
                name="allow first automatic write",
                action=["memory.write"],
                actor=["dream_consolidator"],
                to_chat=[first_key],
                decision=Decision.ALLOW,
            ),
            PolicyRule(
                name="refuse second automatic write",
                action=["memory.write"],
                actor=["dream_consolidator"],
                to_chat=[second_key],
                decision=decision,
                reason="blocked",
            ),
        ]
    )
    monkeypatch.setattr(memory_mod, "get_engine", lambda: policy)
    requests = _mock_memx_transport(monkeypatch)
    from familia.principal_memory_ingestor import PrincipalMemoryIngestor

    ingestor = PrincipalMemoryIngestor(
        base_url="http://memx.test",
        api_key="synthetic-key",
    )
    provider = _ScriptedProvider(
        (
            "[MEMORY] kind=memory fact_id=first-fact value=первый факт\n"
            "[MEMORY] kind=memory fact_id=second-fact value=второй факт"
        ),
        [
            _call(
                "first",
                kind="memory",
                fact_id="first-fact",
                value="первый факт",
            ),
            _call(
                "second",
                kind="memory",
                fact_id="second-fact",
                value="второй факт",
            ),
        ],
    )
    dream, store = _dream(
        tmp_path,
        provider,
        ingestor=ingestor,
        server_principal_getter=lambda: "actor_alpha",
    )
    session = Session(key="telegram:private-chat")
    session.add_message("user", "old user message")
    session.add_message("assistant", "old assistant message")
    session.add_message("user", "current user message")
    session.add_message("assistant", "current assistant message")
    expected_messages = [dict(message) for message in session.messages]
    expected_last_consolidated = session.last_consolidated
    sessions = Mock()
    sessions.get_or_create.return_value = session
    consolidator = Consolidator(
        store=store,
        provider=provider,
        model="test-model",
        sessions=sessions,
        context_window_tokens=100,
        build_messages=Mock(return_value=[]),
        get_tool_definitions=Mock(return_value=[]),
        max_completion_tokens=0,
        archive_sink=dream.archive_private,
        private_session_owner_resolver=PrivateSessionOwnerResolver(
            lambda: registry
        ),
    )
    consolidator._SAFETY_BUFFER = 0
    consolidator.estimate_session_prompt_tokens = Mock(
        side_effect=[(1000, "test"), (0, "test")]
    )
    consolidator.pick_consolidation_boundary = Mock(return_value=(2, 500))

    with pytest.raises(RuntimeError):
        await consolidator.maybe_consolidate_by_tokens(session)

    assert session.messages == expected_messages
    assert session.last_consolidated == expected_last_consolidated
    sessions.save.assert_not_called()
    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[1].url.path == "/set"


@pytest.mark.asyncio
async def test_token_consolidation_keeps_source_when_private_archive_is_denied_invalid(
    tmp_path: Path,
) -> None:
    dream, store = _denied_memory_dream(tmp_path)
    session = Session(key="telegram:private-chat")
    session.add_message("user", "old user message")
    session.add_message("assistant", "old assistant message")
    session.add_message("user", "current user message")
    session.add_message("assistant", "current assistant message")
    expected_messages = [dict(message) for message in session.messages]
    expected_last_consolidated = session.last_consolidated
    sessions = Mock()
    sessions.get_or_create.return_value = session
    consolidator = Consolidator(
        store=store,
        provider=dream.provider,
        model="test-model",
        sessions=sessions,
        context_window_tokens=100,
        build_messages=Mock(return_value=[]),
        get_tool_definitions=Mock(return_value=[]),
        max_completion_tokens=0,
        archive_sink=dream.archive_private,
        private_session_owner_resolver=AsyncMock(return_value="actor_alpha"),
    )
    consolidator._SAFETY_BUFFER = 0
    consolidator.estimate_session_prompt_tokens = Mock(
        side_effect=[(1000, "test"), (0, "test")]
    )
    consolidator.pick_consolidation_boundary = Mock(return_value=(2, 500))

    error: RuntimeError | None = None
    try:
        await consolidator.maybe_consolidate_by_tokens(session)
    except RuntimeError as exc:
        error = exc

    assert session.messages == expected_messages
    assert session.last_consolidated == expected_last_consolidated
    sessions.save.assert_not_called()
    assert error is not None


@pytest.mark.asyncio
async def test_unowned_cron_consolidation_stays_in_service_session(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider("service-only summary", [])
    store = MemoryStore(tmp_path)
    session = Session(key="cron:job-1")
    session.add_message("user", "scheduled instruction")
    session.add_message("assistant", "service response")
    session.add_message("user", "next scheduled instruction")
    sessions = Mock()
    sessions.get_or_create.return_value = session
    private_sink = AsyncMock()
    consolidator = Consolidator(
        store=store,
        provider=provider,
        model="test-model",
        sessions=sessions,
        context_window_tokens=100,
        build_messages=Mock(return_value=[]),
        get_tool_definitions=Mock(return_value=[]),
        max_completion_tokens=0,
        archive_sink=private_sink,
        private_session_owner_resolver=AsyncMock(return_value=None),
    )
    consolidator._SAFETY_BUFFER = 0
    consolidator.estimate_session_prompt_tokens = Mock(
        side_effect=[(1000, "test"), (0, "test")]
    )
    consolidator.pick_consolidation_boundary = Mock(return_value=(2, 500))

    await consolidator.maybe_consolidate_by_tokens(
        session,
        session_context={
            "channel": "telegram",
            "chat_id": "unknown",
            "target_actor": None,
        },
    )

    private_sink.assert_not_awaited()
    assert session.last_consolidated == 2
    assert session.metadata["_last_summary"]["text"] == "service-only summary"
    assert not store.history_file.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("analysis", "finish_reason"),
    [("", "stop"), ("provider failed", "error")],
)
async def test_token_consolidation_rejects_failed_phase1_before_phase2(
    tmp_path: Path,
    analysis: str,
    finish_reason: str,
) -> None:
    provider = _ScriptedProvider(
        analysis,
        [],
        phase1_finish_reason=finish_reason,
    )
    dream, store = _dream(tmp_path, provider)
    session = Session(key="telegram:private-chat")
    session.add_message("user", "old user message")
    session.add_message("assistant", "old assistant message")
    session.add_message("user", "current user message")
    session.add_message("assistant", "current assistant message")
    expected_messages = [dict(message) for message in session.messages]
    expected_last_consolidated = session.last_consolidated
    sessions = Mock()
    sessions.get_or_create.return_value = session
    consolidator = Consolidator(
        store=store,
        provider=provider,
        model="test-model",
        sessions=sessions,
        context_window_tokens=100,
        build_messages=Mock(return_value=[]),
        get_tool_definitions=Mock(return_value=[]),
        max_completion_tokens=0,
        archive_sink=dream.archive_private,
        private_session_owner_resolver=AsyncMock(return_value="actor_alpha"),
    )
    consolidator._SAFETY_BUFFER = 0
    consolidator.estimate_session_prompt_tokens = Mock(
        side_effect=[(1000, "test"), (0, "test")]
    )
    consolidator.pick_consolidation_boundary = Mock(return_value=(2, 500))

    with pytest.raises(RuntimeError):
        await consolidator.maybe_consolidate_by_tokens(session)

    assert provider.phase2_calls == 0
    assert session.messages == expected_messages
    assert session.last_consolidated == expected_last_consolidated
    sessions.save.assert_not_called()


@pytest.mark.asyncio
async def test_dream_accepts_successful_skip_without_memory_operations(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider("[SKIP]", [])
    dream, store = _dream(tmp_path, provider)
    store.append_history("No durable fact", actor="actor_alpha")

    assert await dream.run() is True
    assert provider.phase2_calls >= 1
    assert store.get_last_dream_cursor() == 1


@pytest.mark.asyncio
async def test_private_archive_applies_memory_and_delete_for_fixed_owner(
    tmp_path: Path,
    registry: PrincipalRegistry,
) -> None:
    provider = _ScriptedProvider(
        (
            "[MEMORY] kind=memory fact_id=employment.current "
            "value=перестал работать\n"
            "[DELETE] kind=delete fact_id=private.old-note"
        ),
        [
            _call(
                "memory",
                kind="memory",
                fact_id="employment.current",
                value="перестал работать",
            ),
            _call(
                "delete",
                kind="delete",
                fact_id="private.old-note",
            ),
        ],
    )
    ingestor = Mock()
    ingestor.ingest = AsyncMock(
        side_effect=["committed: stored", "deleted: removed"]
    )

    def install_memory_tool(tools, _store) -> None:
        for name in ("read_file", "edit_file", "write_file"):
            tools.unregister(name)
        tools.register(
            dream_memory_mod.DreamMemorySetTool(
                ingestor=ingestor,
                server_principal_getter=lambda: "actor_alpha",
            )
        )

    store = MemoryStore(tmp_path)
    store.read_memory = Mock(side_effect=AssertionError("full memory scan"))
    store.read_user = Mock(side_effect=AssertionError("full profile scan"))
    store.read_soul = Mock(side_effect=AssertionError("full soul scan"))
    dream = Dream(
        store=store,
        provider=provider,
        model="test-model",
        dream_tool_installers=[install_memory_tool],
        dream_turn_context=make_dream_turn_context(),
    )

    result = await dream.archive_private(
        "actor_alpha",
        [
            {"role": "user", "content": "Я перестал работать"},
            {
                "role": "user",
                "content": "Старую личную заметку не сохраняй",
            },
        ],
    )

    assert result is None
    assert ingestor.ingest.await_args_list == [
        call(
            server_principal="actor_alpha",
            server_topic=None,
            operation={
                "kind": "memory",
                "fact_id": "employment.current",
                "value": "перестал работать",
            },
        ),
        call(
            server_principal="actor_alpha",
            server_topic=None,
            operation={
                "kind": "delete",
                "fact_id": "private.old-note",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_profile_snapshot_is_prompted_and_written_with_exact_version(
    tmp_path: Path,
    registry: PrincipalRegistry,
) -> None:
    provider = _ScriptedProvider(
        "[PROFILE] kind=profile value=name=Alice; birth=1990; city=Paris; surname=Smith",
        [
            _call(
                "profile",
                kind="profile",
                value="name=Alice; birth=1990; city=Paris; surname=Smith",
            )
        ],
    )
    ingestor = Mock()
    ingestor.ingest = AsyncMock(return_value="committed: stored")
    snapshot = {"value": "name=Alice; birth=1990; city=Paris", "version": 41.0}
    reader = AsyncMock(return_value=snapshot)
    active_snapshot: dict[str, Any] = {}

    @contextmanager
    def profile_context(value: dict[str, Any]):
        active_snapshot.update(value)
        try:
            yield
        finally:
            active_snapshot.clear()

    def install_memory_tool(tools, _store) -> None:
        for name in ("read_file", "edit_file", "write_file"):
            tools.unregister(name)
        tools.register(
            dream_memory_mod.DreamMemorySetTool(
                ingestor=ingestor,
                server_principal_getter=lambda: "actor_alpha",
                profile_version_getter=lambda: active_snapshot.get("version"),
            )
        )

    dream = Dream(
        store=MemoryStore(tmp_path),
        provider=provider,
        model="test-model",
        dream_tool_installers=[install_memory_tool],
        dream_turn_context=make_dream_turn_context(),
        dream_profile_reader=reader,
        dream_profile_context=profile_context,
    )

    await dream.archive_private("actor_alpha", [{"role": "user", "content": "Добавь фамилию"}])

    assert reader.await_args_list == [call("actor_alpha")]
    assert ingestor.ingest.await_args_list == [
        call(
            server_principal="actor_alpha",
            server_topic=None,
            operation={
                "kind": "profile",
                "value": "name=Alice; birth=1990; city=Paris; surname=Smith",
            },
            expected_version=41.0,
        )
    ]


@pytest.mark.asyncio
async def test_profile_conflict_rereads_and_reanalyzes_before_retry(
    tmp_path: Path,
    registry: PrincipalRegistry,
) -> None:
    class _ProfileProvider:
        def __init__(self) -> None:
            self.phase1_calls = 0
            self.phase2_calls = 0

        async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
            if kwargs.get("tools") is None:
                self.phase1_calls += 1
                return LLMResponse(
                    content=(
                        "[PROFILE] kind=profile value="
                        f"name=Alice; city=Paris; surname=Smith{self.phase1_calls}"
                    ),
                    finish_reason="stop",
                )
            self.phase2_calls += 1
            if self.phase2_calls in (1, 2):
                return LLMResponse(
                    content="",
                    tool_calls=[
                        _call(
                            f"profile-{self.phase2_calls}",
                            kind="profile",
                            value=(
                                "name=Alice; city=Paris; "
                                f"surname=Smith{self.phase1_calls}"
                            ),
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="done", finish_reason="stop")

    provider = _ProfileProvider()
    ingestor = Mock()
    ingestor.ingest = AsyncMock(
        side_effect=[
            "profile_conflict: profile changed during analysis",
            "committed: stored",
        ]
    )
    snapshots = iter(
        [
            {"value": "name=Alice; city=Paris", "version": 7.0},
            {"value": "name=Alice; city=Paris; surname=Jones", "version": 8.0},
        ]
    )
    reader = AsyncMock(side_effect=lambda _principal: next(snapshots))
    active_snapshot: dict[str, Any] = {}

    @contextmanager
    def profile_context(value: dict[str, Any]):
        active_snapshot.update(value)
        try:
            yield
        finally:
            active_snapshot.clear()

    def install_memory_tool(tools, _store) -> None:
        for name in ("read_file", "edit_file", "write_file"):
            tools.unregister(name)
        tools.register(
            dream_memory_mod.DreamMemorySetTool(
                ingestor=ingestor,
                server_principal_getter=lambda: "actor_alpha",
                profile_version_getter=lambda: active_snapshot.get("version"),
            )
        )

    dream = Dream(
        store=MemoryStore(tmp_path),
        provider=provider,
        model="test-model",
        dream_tool_installers=[install_memory_tool],
        dream_turn_context=make_dream_turn_context(),
        dream_profile_reader=reader,
        dream_profile_context=profile_context,
    )

    await dream.archive_private("actor_alpha", [{"role": "user", "content": "Добавь фамилию"}])

    assert provider.phase1_calls == 2
    assert reader.await_count == 2
    assert [call.kwargs["expected_version"] for call in ingestor.ingest.await_args_list] == [7.0, 8.0]


@pytest.mark.asyncio
async def test_profile_read_error_prevents_phase1_and_phase2(tmp_path: Path) -> None:
    provider = Mock()
    provider.chat_with_retry = AsyncMock()
    def install_memory_tool(tools, _store) -> None:
        tools.unregister("edit_file")
        tools.register(dream_memory_mod.DreamMemorySetTool())

    dream = Dream(
        store=MemoryStore(tmp_path),
        provider=provider,
        dream_tool_installers=[install_memory_tool],
        model="test-model",
        dream_profile_reader=AsyncMock(side_effect=RuntimeError("memX unavailable")),
    )

    with pytest.raises(RuntimeError, match="profile read failed"):
        await dream.archive_private("actor_alpha", [{"role": "user", "content": "x"}])

    provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_familia_phase2_prompt_omits_protected_file_directives(
    tmp_path: Path,
) -> None:
    from nanobot.agent.runner import AgentRunResult

    dream, store = _dream(tmp_path, _ScriptedProvider("analysis", []))
    store.append_history("household fact", actor="actor_alpha")
    captured_prompt: list[str] = []

    async def run(spec):
        captured_prompt.append(spec.initial_messages[0]["content"])
        return AgentRunResult(
            final_content="done",
            messages=[],
            stop_reason="completed",
            tool_events=[],
        )

    dream._runner.run = AsyncMock(side_effect=run)

    assert await dream.run() is True
    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]

    assert "- [PROFILE] entries:" in prompt
    assert "kind='profile', value='<profile>'" in prompt
    assert "- [MEMORY] entries:" in prompt
    assert (
        "kind='memory', "
        "fact_id='<stable_fact_id>', value='<atomic_fact>'"
    ) in prompt
    assert "- [DELETE] entries:" in prompt
    assert "kind='delete', fact_id='<stable_fact_id>'" in prompt
    assert "source_cursor" not in prompt
    assert "- [SKILL] entries:" not in prompt
    assert "skills/<name>/SKILL.md" not in prompt
    assert "Update memory files" not in prompt
    assert "edit_file" not in prompt
    assert "## Editing rules" not in prompt
    for protected_path_line in (
        "- SOUL.md",
        "- USER.md",
        "- memory/MEMORY.md",
    ):
        assert protected_path_line not in prompt
    for protected_path in (
        "USER.md",
        "MEMORY.md",
        "memory/MEMORY.md",
        "SOUL.md",
    ):
        assert f"[FILE] {protected_path}" not in prompt
        assert f"[FILE-REMOVE] {protected_path}" not in prompt
    assert "[FILE] entries:" not in prompt
    assert "[FILE-REMOVE] entries:" not in prompt
    for forbidden in (
        "scope=",
        "actor=",
        "other=",
        "[PRIVATE",
        "[PAIR",
        "value:memory",
    ):
        assert forbidden not in prompt


@pytest.mark.asyncio
async def test_standalone_phase2_prompt_keeps_file_editor_instructions(
    tmp_path: Path,
) -> None:
    from nanobot.agent.runner import AgentRunResult

    store = MemoryStore(tmp_path)
    dream = Dream(
        store=store,
        provider=_ScriptedProvider("analysis", []),
        model="test-model",
        max_batch_size=10,
    )
    store.append_history("standalone fact")
    captured_prompt: list[str] = []

    async def run(spec):
        captured_prompt.append(spec.initial_messages[0]["content"])
        return AgentRunResult(
            final_content="done",
            messages=[],
            stop_reason="completed",
            tool_events=[],
        )

    dream._runner.run = AsyncMock(side_effect=run)

    assert await dream.run() is True
    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]

    assert "Update memory files" in prompt
    assert "- [FILE] entries:" in prompt
    assert "- [FILE-REMOVE] entries:" in prompt
    assert "edit_file(..., dream_scope='shared')" in prompt
    assert "## Editing rules" in prompt
    assert "- SOUL.md" in prompt
    assert "- USER.md" in prompt
    assert "- memory/MEMORY.md" in prompt
    assert "source_cursor" not in prompt
    assert "[PROFILE] entries:" not in prompt
    assert "[MEMORY] entries:" not in prompt


def test_dream_memory_has_no_legacy_private_document_router() -> None:
    assert not hasattr(dream_memory_mod, "_resolve_full_key")
