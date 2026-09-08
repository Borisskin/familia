"""Unit tests for Familia Dream memory.

Automatic compaction receives one server-resolved private owner and exposes
only atomic profile, memory, and delete operations.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from familia import principals as principals_mod
from familia.principals import Identity, Principal, PrincipalRegistry
from nanobot.agent.memory import Consolidator, MemoryStore
from familia.policy import Decision, PolicyContext, PolicyDecision, PolicyRule
from familia.policy.engine import load_engine
from familia.tools.dream_memory import DreamMemorySetTool
from nanobot.providers.base import LLMResponse


REPO_ROOT = Path(__file__).resolve().parents[1] / "src" / "familia" / "config"


# --- append_history --------------------------------------------------------

def test_append_history_records_actor(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    c1 = store.append_history("hi from member_a", actor="member_a")
    c2 = store.append_history("system tick")  # no actor
    entries = list(store._iter_valid_entries())
    by_cursor = {cursor: e for e, cursor in entries}
    assert by_cursor[c1]["actor"] == "member_a"
    assert "actor" not in by_cursor[c2]


# --- Consolidator._group_by_actor -----------------------------------------

def _msg(role: str, content: str, actor: str | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {"role": role, "content": content}
    if actor:
        m["actor"] = actor
    return m


def test_group_by_actor_splits_on_new_user() -> None:
    msgs = [
        _msg("user", "zh1", "member_a"),
        _msg("assistant", "resp1"),
        _msg("user", "vya1", "owner"),
        _msg("assistant", "resp2"),
        _msg("user", "zh2", "member_a"),
    ]
    groups = Consolidator._group_by_actor(msgs)
    assert [a for a, _ in groups] == ["member_a", "owner", "member_a"]
    assert [len(g) for _, g in groups] == [2, 2, 1]


def test_group_by_actor_preserves_run_when_actor_repeats() -> None:
    msgs = [
        _msg("user", "zh1", "member_a"),
        _msg("assistant", "resp1"),
        _msg("user", "zh2", "member_a"),
        _msg("assistant", "resp2"),
    ]
    groups = Consolidator._group_by_actor(msgs)
    assert len(groups) == 1
    assert groups[0][0] == "member_a"
    assert len(groups[0][1]) == 4


def test_group_by_actor_leading_untagged_goes_to_none() -> None:
    msgs = [
        _msg("assistant", "system notice"),
        _msg("user", "zh1", "member_a"),
    ]
    groups = Consolidator._group_by_actor(msgs)
    assert groups[0][0] is None
    assert groups[1][0] == "member_a"


# --- DreamMemorySetTool: automatic-operation boundary ----------------------

def test_dream_memory_tool_schema_excludes_model_routing_fields() -> None:
    tool = DreamMemorySetTool()
    properties = tool.parameters["properties"]

    assert set(properties) == {"kind", "fact_id", "value"}
    assert {
        "source_cursor",
        "scope",
        "actor",
        "other",
        "owner",
        "topic",
    }.isdisjoint(properties)
    assert tool.parameters["additionalProperties"] is False


def test_dream_memory_module_has_no_legacy_writer_branch() -> None:
    from familia.tools import dream_memory as dream_memory_mod

    assert {
        "_resolve_full_key",
        "_merge_memory_document",
        "httpx",
        "CONSOLIDATOR_KEY_ENV",
    }.isdisjoint(vars(dream_memory_mod))


# --- DreamMemorySetTool: policy gate --------------------------------------

@pytest.fixture(scope="module")
def policy_engine():
    # Reuse the real policy.yaml so the dream_consolidator rule is tested
    # against the actual deployed rules, not a local fiction.
    from familia import policy as policy_mod
    eng = load_engine(REPO_ROOT / "policy.yaml")
    policy_mod.get_engine()  # warm singleton module
    policy_mod._engine = eng  # type: ignore[attr-defined]
    yield eng


@pytest.fixture
def known_member(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    registry = PrincipalRegistry(
        [
            Principal(
                id="member_a",
                display_name="Member A",
                identities=[Identity(channel="test", sender_id="member-a")],
                memx_key="member-a",
                roles=[],
            ),
            Principal(
                id="member_b",
                display_name="Member B",
                identities=[Identity(channel="test", sender_id="member-b")],
                memx_key="member-b",
                roles=[],
            ),
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", registry)
    return registry


def test_dream_consolidator_allowed_for_private_member_a(policy_engine) -> None:
    r = policy_engine.evaluate(
        PolicyContext(
            action="memory.write", actor="dream_consolidator",
            to_chat="private:member_a:secret",
        )
    )
    assert r.decision is Decision.ALLOW
    assert r.rule and "dream_consolidator" in r.rule.name


def test_dream_consolidator_denied_for_memory_read(policy_engine) -> None:
    r = policy_engine.evaluate(
        PolicyContext(
            action="memory.read", actor="dream_consolidator",
            to_chat="private:member_a:secret",
        )
    )
    assert r.decision is Decision.DENY


@pytest.mark.parametrize(
    ("kind", "fact_id", "value", "expected_key", "decision"),
    [
        (
            "profile", None, "secret profile",
            "private:member_a:value:user_profile", Decision.DENY,
        ),
        (
            "memory", "fact-17", "secret fact",
            "private:member_a:memory:fact-17", Decision.ASK,
        ),
        (
            "delete", "fact-17", None,
            "private:member_a:memory:fact-17", Decision.DENY,
        ),
    ],
)
@pytest.mark.asyncio
async def test_dream_memory_set_policy_denial_precedes_ingestor(
    monkeypatch: pytest.MonkeyPatch,
    known_member: PrincipalRegistry,
    kind: str,
    fact_id: str | None,
    value: str | None,
    expected_key: str,
    decision: Decision,
) -> None:
    from familia.tools import memory as memory_mod
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    denied = MagicMock()
    denied.evaluate.return_value = PolicyDecision(
        decision,
        PolicyRule(name="deny automatic writes", reason="blocked"),
    )
    monkeypatch.setattr(memory_mod, "get_engine", lambda: denied)
    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value="committed: should not run")
    principal_getter = MagicMock(return_value="member_a")
    tool = DreamMemorySetTool(
        ingestor=ingestor,
        server_principal_getter=principal_getter,
    )
    set_current_actor(CONSOLIDATOR_ACTOR)

    result = await tool.execute(kind=kind, fact_id=fact_id, value=value)

    assert result.startswith("Policy denied memory.write")
    assert "secret" not in result
    denied.evaluate.assert_called_once()
    context = denied.evaluate.call_args.args[0]
    assert context.action == "memory.write"
    assert context.actor == CONSOLIDATOR_ACTOR
    assert context.to_chat == expected_key
    ingestor.ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_dream_memory_set_tool_delegates_without_own_http_writer() -> None:
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value="committed: stored")
    server_principal_getter = MagicMock(return_value="member_a")
    tool = DreamMemorySetTool(
        ingestor=ingestor,
        server_principal_getter=server_principal_getter,
    )
    set_current_actor(CONSOLIDATOR_ACTOR)

    result = await tool.execute(
        kind="memory",
        fact_id="fact-17",
        value="worried about deadline",
    )

    assert result == "committed: stored"
    server_principal_getter.assert_called_once_with()
    ingestor.ingest.assert_awaited_once_with(
        server_principal="member_a",
        server_topic=None,
        operation={
            "kind": "memory",
            "fact_id": "fact-17",
            "value": "worried about deadline",
        },
    )


@pytest.mark.asyncio
async def test_dream_memory_set_tool_deletes_exact_private_fact() -> None:
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value="deleted: removed")
    tool = DreamMemorySetTool(
        ingestor=ingestor,
        server_principal_getter=MagicMock(return_value="member_a"),
    )
    set_current_actor(CONSOLIDATOR_ACTOR)

    result = await tool.execute(
        kind="delete",
        fact_id="employment.current",
    )

    assert result == "deleted: removed"
    ingestor.ingest.assert_awaited_once_with(
        server_principal="member_a",
        server_topic=None,
        operation={
            "kind": "delete",
            "fact_id": "employment.current",
        },
    )


@pytest.mark.asyncio
async def test_dream_batch_context_fixes_private_owner(
    known_member: PrincipalRegistry,
) -> None:
    from familia.bootstrap import (
        make_dream_batch_context,
        make_dream_server_context_resolver,
        make_dream_turn_context,
    )
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value="committed: stored")
    owner = make_dream_server_context_resolver()
    tool = DreamMemorySetTool(
        ingestor=ingestor,
        server_principal_getter=owner,
    )
    set_current_actor(CONSOLIDATOR_ACTOR)

    assert owner() is None
    with make_dream_turn_context()(), make_dream_batch_context()("member_a"):
        result = await tool.execute(
            kind="memory",
            fact_id="employment.current",
            value="works at Example",
        )

    assert result == "committed: stored"
    assert owner() is None
    ingestor.ingest.assert_awaited_once_with(
        server_principal="member_a",
        server_topic=None,
        operation={
            "kind": "memory",
            "fact_id": "employment.current",
            "value": "works at Example",
        },
    )


def test_dream_profile_context_exposes_only_captured_revision() -> None:
    from familia.bootstrap import (
        make_dream_profile_context,
        make_dream_profile_version_resolver,
    )

    version = make_dream_profile_version_resolver()
    assert version() is None
    with make_dream_profile_context()({"value": "profile", "version": 17.0}):
        assert version() == 17.0
    assert version() is None


@pytest.mark.asyncio
async def test_standalone_phase1_prompt_and_history_keep_legacy_contract(
    tmp_path: Path,
) -> None:
    from nanobot.agent.memory import Dream
    from nanobot.agent.runner import AgentRunResult

    captured_messages: list[dict[str, Any]] = []
    provider = MagicMock()

    async def phase1(**kwargs: Any) -> LLMResponse:
        captured_messages.extend(kwargs["messages"])
        return LLMResponse(content="[SKIP]")

    provider.chat_with_retry = AsyncMock(side_effect=phase1)
    store = MemoryStore(tmp_path)
    store.append_history("standalone fact", actor="member_a")
    dream = Dream(store=store, provider=provider, model="test-model")
    dream._runner.run = AsyncMock(
        return_value=AgentRunResult(
            final_content="done",
            messages=[],
            stop_reason="completed",
            tool_events=[],
        )
    )

    assert await dream.run() is True
    phase1_system = captured_messages[0]["content"]
    phase1_history = captured_messages[1]["content"]
    assert "actor=member_a: standalone fact" in phase1_history
    assert "source_cursor" not in phase1_history
    assert "[FILE] atomic fact" in phase1_system
    assert "[PRIVATE:<actor>]" in phase1_system
    assert "[PAIR:<a>,<b>]" in phase1_system
    assert "source_cursor" not in phase1_system


@pytest.mark.asyncio
async def test_dream_sets_and_restores_consolidator_actor(tmp_path: Path) -> None:
    from nanobot.agent.memory import Dream
    from nanobot.agent.runner import AgentRunResult
    from familia.bootstrap import make_dream_turn_context
    from familia.principals import get_current_actor, set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    store = MemoryStore(tmp_path)
    store.append_history("shared system fact")
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=MagicMock(content="[SKIP]"))
    dream = Dream(
        store=store,
        provider=provider,
        model="test-model",
        dream_turn_context=make_dream_turn_context(),
    )

    async def _run(_spec):
        assert get_current_actor() == CONSOLIDATOR_ACTOR
        return AgentRunResult(
            final_content="done",
            messages=[],
            stop_reason="completed",
            tool_events=[],
        )

    dream._runner.run = AsyncMock(side_effect=_run)
    set_current_actor("prior_actor")

    assert await dream.run() is True
    assert get_current_actor() == "prior_actor"


@pytest.mark.asyncio
async def test_familia_dream_does_not_register_protected_file_editor(
    tmp_path: Path,
) -> None:
    from nanobot.agent.memory import Dream
    from familia.nanobot_extension.cron import make_dream_tool_installers

    store = MemoryStore(tmp_path)
    protected = {
        "USER.md": "user-before\n",
        "MEMORY.md": "root-memory-before\n",
        "memory/MEMORY.md": "memory-before\n",
        "SOUL.md": "soul-before\n",
    }
    for relative_path, content in protected.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    dream = Dream(
        store=store,
        provider=MagicMock(),
        model="test-model",
        dream_tool_installers=make_dream_tool_installers(),
    )

    protected_aliases = (
        "./USER.md",
        "./MEMORY.md",
        "memory/../memory/MEMORY.md",
        "sOuL.Md",
        "dream_scope='shared'",
    )
    assert dream._tools.get("edit_file") is None, protected_aliases
    assert {
        relative_path: (tmp_path / relative_path).read_text(encoding="utf-8")
        for relative_path in protected
    } == protected


def test_bootstrap_wires_history_validator_dream_scope_and_restore_policy(
    tmp_path: Path,
) -> None:
    from familia.bootstrap import make_agent_loop_kwargs

    kwargs = make_agent_loop_kwargs(tmp_path)

    assert callable(kwargs["history_actor_validator"])
    assert callable(kwargs["dream_turn_context"])
    assert callable(kwargs["dream_profile_reader"])
    assert callable(kwargs["dream_profile_context"])
    restore_policy = kwargs["dream_restore_policy"]
    assert callable(restore_policy)
    assert isinstance(restore_policy(["SOUL.md"]), str)
    assert restore_policy(["USER.md", "memory/MEMORY.md"]) is None


def test_bootstrap_wires_private_session_owner_resolver(tmp_path: Path) -> None:
    from familia.bootstrap import make_agent_loop_kwargs
    from familia.private_session_owner import PrivateSessionOwnerResolver

    kwargs = make_agent_loop_kwargs(tmp_path)

    assert isinstance(
        kwargs["private_session_owner_resolver"],
        PrivateSessionOwnerResolver,
    )


def _dream_restore_context(
    *,
    actor: str | None,
    changed_file: str,
    restore_policy,
):
    from nanobot.bus.events import InboundMessage
    from nanobot.command.router import CommandContext

    git = MagicMock()
    git.is_initialized.return_value = True
    if changed_file.startswith(" "):
        old_path = json.dumps(f"a/{changed_file}")
        new_path = json.dumps(f"b/{changed_file}")
    else:
        old_path = f"a/{changed_file}"
        new_path = f"b/{changed_file}"
    git.show_commit_diff.return_value = (
        MagicMock(sha="dream-sha"),
        f"diff --git {old_path} {new_path}\n"
        f"--- {old_path}\n"
        f"+++ {new_path}\n",
    )
    git.revert.return_value = "safety-sha"
    loop = MagicMock()
    loop.consolidator.store.git = git
    loop.dream_restore_policy = restore_policy
    message = InboundMessage(
        channel="telegram",
        sender_id="101",
        chat_id="101",
        content="/dream-restore dream-sha",
        actor=actor,
    )
    context = CommandContext(
        msg=message,
        session=None,
        key=message.session_key,
        raw=message.content,
        args="dream-sha",
        loop=loop,
    )
    return git, context


@pytest.mark.asyncio
async def test_familia_dream_restore_policy_blocks_soul() -> None:
    from familia.bootstrap import make_dream_restore_policy
    from nanobot.command.builtin import cmd_dream_restore

    git, context = _dream_restore_context(
        actor=None,
        changed_file="SOUL.md",
        restore_policy=make_dream_restore_policy(),
    )

    result = await cmd_dream_restore(context)

    git.revert.assert_not_called()
    assert "does not restore `SOUL.md`" in result.content


@pytest.mark.parametrize(
    "changed_file",
    ["unknown.md", "subdir/SOUL.md", "sOuL.Md", " SOUL.md"],
)
@pytest.mark.asyncio
async def test_familia_dream_restore_policy_blocks_unknown_or_distorted_path(
    changed_file: str,
) -> None:
    from familia.bootstrap import make_dream_restore_policy
    from nanobot.command.builtin import cmd_dream_restore

    git, context = _dream_restore_context(
        actor=None,
        changed_file=changed_file,
        restore_policy=make_dream_restore_policy(),
    )

    result = await cmd_dream_restore(context)

    git.revert.assert_not_called()
    assert "cannot verify which files" in result.content


@pytest.mark.parametrize("changed_file", ["USER.md", "memory/MEMORY.md"])
@pytest.mark.asyncio
async def test_familia_dream_restore_policy_allows_known_non_soul_diff(
    changed_file: str,
) -> None:
    from familia.bootstrap import make_dream_restore_policy
    from nanobot.command.builtin import cmd_dream_restore

    git, context = _dream_restore_context(
        actor="principal_alpha",
        changed_file=changed_file,
        restore_policy=make_dream_restore_policy(),
    )

    await cmd_dream_restore(context)

    git.revert.assert_called_once_with("dream-sha")
