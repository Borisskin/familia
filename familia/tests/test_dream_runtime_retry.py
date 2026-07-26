"""Focused Dream manager and private-memory CAS regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest

from familia import principals as principals_mod
from familia.bootstrap import make_dream_turn_context
from familia.nanobot_extension.cron import make_dream_tool_installers
from familia.principals import Identity, Principal, PrincipalRegistry, set_current_actor
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
                identities=[Identity(channel="test", sender_id="alpha")],
                memx_key="alpha-key",
                roles=[],
            ),
            Principal(
                id="actor_beta",
                display_name="Actor Beta",
                identities=[Identity(channel="test", sender_id="beta")],
                memx_key="beta-key",
                roles=[],
            ),
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", value)
    return value


class _ScriptedProvider:
    def __init__(self, analysis: str, tool_calls: list[ToolCallRequest]) -> None:
        self.analysis = analysis
        self.tool_calls = tool_calls
        self.phase2_calls = 0
        self.tool_results: list[str] = []

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        if kwargs.get("tools") is None:
            return LLMResponse(content=self.analysis)
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
