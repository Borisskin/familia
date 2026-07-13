"""Unit tests for per-scope Dream (familia #44).

Scope:

* ``MemoryStore.append_history`` tags entries with ``actor``.
* ``Consolidator._group_by_actor`` splits a mixed chunk into per-actor runs.
* ``DreamMemorySetTool`` builds the right memX key for each scope and gates
  through policy as ``dream_consolidator``.

The heavy Dream.run path (LLM-driven Phase 1 + Phase 2) is not exercised
here — it's covered by live integration smoke.  These tests pin down
the mechanical guarantees the smoke depends on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from familia import principals as principals_mod
from familia.principals import Identity, Principal, PrincipalRegistry
from nanobot.agent.memory import Consolidator, MemoryStore
from familia.policy import Decision, PolicyContext
from familia.policy.engine import load_engine
from familia.tools.dream_memory import DreamMemorySetTool


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


# --- DreamMemorySetTool: key construction ---------------------------------

@pytest.mark.parametrize(
    "scope,actor,other,key,expected",
    [
        ("shared",  None,         None,     "todo",  "shared:todo"),
        ("private", "member_a",     None,     "feels", "private:member_a:value:memory"),
        # pair: alphabetical order regardless of argument order
        ("pair",    "member_a",     "owner", "note", "pair:member_a_owner:note"),
        ("pair",    "owner", "member_a",     "note", "pair:member_a_owner:note"),
    ],
)
def test_dream_memory_key_resolution(scope, actor, other, key, expected):
    from familia.tools.dream_memory import _resolve_full_key
    full, err = _resolve_full_key(scope, key, actor, other)
    assert err is None, err
    assert full == expected


@pytest.mark.parametrize(
    "scope,actor,other,key",
    [
        ("private", None,         None,      "x"),   # scope=private requires actor
        ("pair",    "member_a",     None,      "x"),   # pair requires both
        ("pair",    "member_a",     "member_a",  "x"),   # pair requires distinct
        ("shared",  None,         None,      ""),    # empty key
        ("weird",   None,         None,      "x"),   # unknown scope
    ],
)
def test_dream_memory_key_invalid(scope, actor, other, key):
    from familia.tools.dream_memory import _resolve_full_key
    _, err = _resolve_full_key(scope, key, actor, other)
    assert err is not None


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
            )
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


def test_dream_memory_set_tool_calls_memx_on_allow(policy_engine, known_member) -> None:
    import asyncio
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    tool = DreamMemorySetTool(base_url="http://mock-memx:8000", api_key="dream_consolidator_key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "ok"
    mock_resp.json.return_value = {
        "ok": True,
        "status": "committed",
        "committed": True,
        "updated": True,
        "retryable": False,
        "version": 1.0,
    }
    mock_get_resp = MagicMock(status_code=200, text="null")
    mock_get_resp.json.return_value = None

    # Dream agent pins this actor for its turn; the tool now refuses to run
    # outside that context (defense-in-depth against accidental registration
    # on the main loop). Mirror prod here.
    set_current_actor(CONSOLIDATOR_ACTOR)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_get_resp)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = asyncio.run(tool.execute(
            scope="private", actor="member_a",
            key="value:memory", value="worried about deadline",
        ))

    assert "Stored at 'private:member_a:value:memory'" in result
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args.kwargs["headers"]["x-api-key"] == "dream_consolidator_key"
    assert call_args.kwargs["json"] == {
        "key": "private:member_a:value:memory",
        "value": "worried about deadline",
        "expected_ts": None,
    }


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
async def test_explicit_shared_file_route_remains_available(tmp_path: Path) -> None:
    from nanobot.agent.memory import Dream
    from familia.nanobot_extension.cron import make_dream_tool_installers

    store = MemoryStore(tmp_path)
    store.write_memory("# Shared memory\n")
    dream = Dream(
        store=store,
        provider=MagicMock(),
        model="test-model",
        dream_tool_installers=make_dream_tool_installers(),
    )
    edit_tool = dream._tools.get("edit_file")
    assert edit_tool is not None

    result = await edit_tool.execute(
        path="memory/MEMORY.md",
        old_text="# Shared memory",
        new_text="# Shared memory\n- shared household fact",
        dream_scope="shared",
    )

    assert not result.startswith("Error:")
    assert store.read_memory() == "# Shared memory\n- shared household fact\n"


@pytest.mark.asyncio
async def test_dream_memory_updated_false_is_not_committed(
    policy_engine,
    known_member,
) -> None:
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    tool = DreamMemorySetTool(base_url="http://mock-memx:8000", api_key="key")
    response = MagicMock(status_code=200, text='{"ok":true,"updated":false}')
    response.json.return_value = {
        "ok": True,
        "status": "not_updated",
        "committed": False,
        "updated": False,
        "retryable": False,
        "version": None,
    }
    get_response = MagicMock(status_code=200, text="null")
    get_response.json.return_value = None
    set_current_actor(CONSOLIDATOR_ACTOR)

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=get_response)
        client.post = AsyncMock(return_value=response)
        client_cls.return_value.__aenter__.return_value = client
        result = await tool.execute(
            scope="private",
            actor="member_a",
            key="value:memory",
            value="unchanged",
        )

    assert result.startswith("Error:")
    assert "semantic commit failed" in result


def test_bootstrap_wires_history_validator_and_dream_scope(tmp_path: Path) -> None:
    from familia.bootstrap import make_agent_loop_kwargs

    kwargs = make_agent_loop_kwargs(tmp_path)

    assert callable(kwargs["history_actor_validator"])
    assert callable(kwargs["dream_turn_context"])
