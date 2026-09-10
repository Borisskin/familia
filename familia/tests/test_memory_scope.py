"""Unit tests for ``familia.tools.memory._resolve_full_key``.

The fix (memory pair-scope idempotency) addresses a real bug seen in
production audit logs: the LLM would call
``memory_get(scope="pair:owner_member_a")`` (canonical pair form, copied
from a stored value) and the tool re-sorted
``[actor="member_a", "owner_member_a"]`` producing
``pair:member_a_owner_member_a:upcoming`` — which policy denied on every
heartbeat tick, breaking the upcoming-events check. The fix accepts both
``pair:<other_id>`` and ``pair:<a>_<b>`` shapes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from familia import principals as principals_mod
from familia.policy import Decision, PolicyDecision, PolicyRule
from familia.principals import Identity, Principal, PrincipalRegistry
from familia.tools import memory as memory_mod
from familia.tools.memory import MemoryGetTool, MemorySetTool, _resolve_full_key


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="O", identities=[
            Identity(channel="vk", sender_id="1000001"),
        ], memx_key="k1", roles=["admin"]),
        Principal(id="member_a", display_name="A", identities=[
            Identity(channel="vk", sender_id="1000002"),
        ], memx_key="k2", roles=[]),
        Principal(id="member_b", display_name="B", identities=[],
                  memx_key="k3", roles=[]),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    monkeypatch.setattr(memory_mod, "is_peer", lambda _a, _b: True)
    return reg


def test_shared(registry):
    full, err = _resolve_full_key("shared", "todo", "member_a")
    assert err is None
    assert full == "shared:todo"


def test_private(registry):
    full, err = _resolve_full_key("private", "feels", "member_a")
    assert err is None
    assert full == "private:member_a:feels"


def test_pair_other_id_form(registry):
    full, err = _resolve_full_key("pair:owner", "upcoming", "member_a")
    assert err is None
    assert full == "pair:member_a_owner:upcoming"


def test_pair_canonical_form_idempotent(registry):
    # The bug case: LLM passes already-sorted "pair:a_b".
    full, err = _resolve_full_key("pair:member_a_owner", "upcoming", "member_a")
    assert err is None
    assert full == "pair:member_a_owner:upcoming"


def test_pair_canonical_form_from_other_actor(registry):
    # Same canonical name resolves identically when called by the other peer.
    full, err = _resolve_full_key("pair:member_a_owner", "upcoming", "owner")
    assert err is None
    assert full == "pair:member_a_owner:upcoming"


def test_pair_argument_order_canonicalizes(registry):
    full, err = _resolve_full_key("pair:member_a", "x", "owner")
    assert err is None
    assert full == "pair:member_a_owner:x"


def test_pair_self_rejected(registry):
    _, err = _resolve_full_key("pair:member_a", "x", "member_a")
    assert err is not None
    assert "different principal" in err


def test_pair_unknown_principal_rejected(registry):
    _, err = _resolve_full_key("pair:nosuch", "x", "member_a")
    assert err is not None
    assert "unknown principal" in err.lower()


def test_pair_unrelated_canonical_rejected(registry):
    # "pair:owner_member_b" — actor member_a is not in the pair.
    _, err = _resolve_full_key("pair:owner_member_b", "x", "member_a")
    assert err is not None
    assert "unknown principal" in err.lower()


def test_pair_random_underscored_string_rejected(registry):
    # Stress the canonical-form scan: bogus "a_b_c" should fall through to
    # 'unknown principal', not silently succeed or hit the self-pair branch.
    _, err = _resolve_full_key("pair:a_b_c", "x", "member_a")
    assert err is not None
    assert "unknown principal" in err.lower()


def test_actual_pair_namespace_collision_is_rejected(monkeypatch):
    reg = PrincipalRegistry([
        Principal(id="a", memx_key="ka"),
        Principal(id="a_b", memx_key="kab"),
        Principal(id="b_c", memx_key="kbc"),
        Principal(id="c", memx_key="kc"),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    monkeypatch.setattr("familia.tools.memory.is_peer", lambda _a, _b: True)

    full, err = _resolve_full_key("pair:b_c", "x", "a")

    assert full is None
    assert err is not None
    assert "ambiguous pair namespace" in err.lower()


def test_pair_empty_other_rejected(registry):
    _, err = _resolve_full_key("pair:", "x", "member_a")
    assert err is not None


def test_unknown_scope_rejected(registry):
    _, err = _resolve_full_key("weird", "x", "member_a")
    assert err is not None


def test_empty_key_rejected(registry):
    _, err = _resolve_full_key("shared", "", "member_a")
    assert err is not None


@pytest.mark.parametrize("scope", ("shared", "pair:owner"))
def test_memory_get_rejects_legacy_scope_before_any_read_dependency(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
    scope: str,
) -> None:
    calls = {
        "policy": 0,
        "admin_key": 0,
        "graphs": 0,
        "http": 0,
        "memx": 0,
    }

    def touched(name: str):
        def fail(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError(f"{name} must not be used for scope={scope}")

        return fail

    class ForbiddenHttpClient:
        def __init__(self, *_args, **_kwargs):
            calls["http"] += 1
            raise AssertionError(f"HTTP must not be created for scope={scope}")

        async def get(self, *_args, **_kwargs):
            calls["memx"] += 1
            raise AssertionError(f"memX must not be read for scope={scope}")

    monkeypatch.setattr(memory_mod, "get_current_actor", lambda: "member_a")
    monkeypatch.setattr(memory_mod, "get_engine", touched("policy"))
    monkeypatch.setattr(memory_mod, "resolve_admin_key", touched("admin_key"))
    monkeypatch.setattr(memory_mod, "_fetch_graph", touched("graphs"))
    monkeypatch.setattr(memory_mod.httpx, "AsyncClient", ForbiddenHttpClient)

    result = asyncio.run(MemoryGetTool().execute(scope=scope, key="todo"))

    assert result.startswith("Error:")
    assert "private" in result.lower()
    assert calls == {
        "policy": 0,
        "admin_key": 0,
        "graphs": 0,
        "http": 0,
        "memx": 0,
    }


@pytest.mark.parametrize(
    ("value", "decision"),
    [(None, Decision.DENY), ("secret fact", Decision.ASK)],
)
def test_memory_set_policy_denial_precedes_topic_and_memx_write(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
    value: str | None,
    decision: Decision,
) -> None:
    denied = MagicMock()
    denied.evaluate.return_value = PolicyDecision(
        decision,
        PolicyRule(name="deny private writes", reason="blocked"),
    )
    monkeypatch.setattr(memory_mod, "get_engine", lambda: denied)
    topic_write_state = AsyncMock()
    monkeypatch.setattr(memory_mod, "_topic_write_state", topic_write_state)
    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value="committed: should not run")
    monkeypatch.setattr(memory_mod, "get_current_actor", lambda: "member_a")

    result = asyncio.run(
        MemorySetTool(ingestor=ingestor).execute(
            fact_id="fact-17", value=value, topic="family-topic"
        )
    )

    assert result.startswith("Error: Policy denied memory.write")
    assert "secret fact" not in result
    denied.evaluate.assert_called_once()
    context = denied.evaluate.call_args.args[0]
    assert context.action == "memory.write"
    assert context.actor == "member_a"
    assert context.to_chat == "private:member_a:memory:fact-17"
    topic_write_state.assert_not_awaited()
    ingestor.ingest.assert_not_awaited()


def test_memory_set_policy_allow_reaches_exact_ingestor_operation(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    allowed = MagicMock()
    allowed.evaluate.return_value = PolicyDecision(
        Decision.ALLOW,
        PolicyRule(name="allow private writes"),
    )
    monkeypatch.setattr(memory_mod, "get_engine", lambda: allowed)
    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value="committed: stored")
    monkeypatch.setattr(memory_mod, "get_current_actor", lambda: "member_a")

    result = asyncio.run(
        MemorySetTool(ingestor=ingestor).execute(
            fact_id="fact-17", value="stored fact"
        )
    )

    assert result == "committed: stored"
    allowed.evaluate.assert_called_once()
    ingestor.ingest.assert_awaited_once_with(
        server_principal="member_a",
        server_topic=None,
        operation={
            "kind": "memory",
            "fact_id": "fact-17",
            "value": "stored fact",
        },
    )
