"""Tests for the family-by-default ``private:`` peer-read path.

Covers the Variant-A design (0.3.0) where ``private:<owner>:<key>``
records are readable by peer-edge principals unless tagged ``secret``,
with three reserved value:* slots (memory, user_profile, heartbeat)
that stay owner-only even with a peer-edge.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest import mock

import httpx
import pytest

from familia import principals as principals_mod
from familia.acl import codec, peers as peers_mod, schema as acl_schema
from familia.principals import Identity, Principal, PrincipalRegistry
from familia.tools import memory as memory_mod
from familia.tools.memory import (
    MemoryGetTool,
    SECRET_TAG,
    _resolve_full_key,
)


# ---- fixtures --------------------------------------------------------------


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="Owner", identities=[
            Identity(channel="vk", sender_id="1"),
        ], memx_key="owner_key", roles=["admin"]),
        Principal(id="spouse", display_name="Spouse", identities=[
            Identity(channel="vk", sender_id="2"),
        ], memx_key="spouse_key", roles=[]),
        Principal(id="child_a", display_name="ChildA", identities=[
            Identity(channel="vk", sender_id="3"),
        ], memx_key="child_a_key", roles=["child"]),
        Principal(id="unrelated", display_name="Unrelated", identities=[],
                  memx_key="unrelated_key", roles=[]),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    return reg


@pytest.fixture
def peer_graph(monkeypatch: pytest.MonkeyPatch, registry):
    """Provide canonical family/topics graphs to both read adapters."""
    peers_mod.reset_cache()
    family_doc = {
        "nodes": [
            {"id": principal_id, "type": "principal"}
            for principal_id in registry.ids
        ],
        "edges": [
            {"from": "owner", "to": "spouse", "rel": "spouse_of"},
            {"from": "owner", "to": "child_a", "rel": "guardian_of"},
            {"from": "spouse", "to": "child_a", "rel": "guardian_of"},
        ],
        "updated_at_ms": 1_000_000_000,
    }
    topics_doc = {
        "nodes": [
            {"id": "topic_shared", "type": "topic"},
            {"id": "hidden_topic", "type": "topic"},
        ],
        "edges": [
            {
                "from": "topic_shared",
                "to": principal_id,
                "rel": "concerns",
                "concerns_as": "spouse_of",
            }
            for principal_id in ("owner", "spouse", "unrelated")
        ],
        "updated_at_ms": 1_000_000_000,
    }
    monkeypatch.setattr(
        "familia.acl.graph_io.load_graph_value",
        lambda key, *a, **kw: (
            family_doc if key == "shared:family.graph" else topics_doc
        ),
        raising=False,
    )

    async def fetch_graph(_api_key, key, _base_url):
        raw = family_doc if key == "shared:family.graph" else topics_doc
        return acl_schema.Graph.from_dict(raw)

    monkeypatch.setattr(memory_mod, "_fetch_graph", fetch_graph)
    yield
    peers_mod.reset_cache()


# ---- _resolve_full_key with target_actor -----------------------------------


def test_resolve_private_own_namespace(registry):
    full, err = _resolve_full_key("private", "moto", "owner")
    assert err is None
    assert full == "private:owner:moto"


def test_resolve_private_with_target_actor(registry):
    """target_actor=peer rewrites the namespace to the peer's."""
    full, err = _resolve_full_key(
        "private", "moto", "spouse", target_actor="owner",
    )
    assert err is None
    assert full == "private:owner:moto"


def test_resolve_private_target_actor_same_as_caller(registry):
    """target_actor=self is identity — no rewrite."""
    full, err = _resolve_full_key(
        "private", "moto", "owner", target_actor="owner",
    )
    assert err is None
    assert full == "private:owner:moto"


def test_resolve_target_actor_rejected_for_shared(registry):
    _, err = _resolve_full_key(
        "shared", "calendar", "spouse", target_actor="owner",
    )
    assert err is not None
    assert "private" in err.lower()


def test_resolve_target_actor_rejected_for_pair(registry):
    _, err = _resolve_full_key(
        "pair:owner", "vacation", "spouse", target_actor="owner",
    )
    assert err is not None
    assert "private" in err.lower()


@pytest.mark.parametrize("reserved", [
    "value:memory",
    "value:user_profile",
    "value:heartbeat",
])
def test_reserved_value_keys_resolve_cross_actor(registry, reserved):
    """Reserved value:* slots resolve to the named peer's namespace.

    Under the family-by-default model (0.3.0 + reserved opening), the
    resolver no longer folds reserved keys back to the caller — they
    flow through the same is_peer + secret-tag gate as custom keys.
    """
    full, err = _resolve_full_key(
        "private", reserved, "spouse", target_actor="owner",
    )
    assert err is None
    assert full == f"private:owner:{reserved}"


# ---- MemoryGetTool peer-read path ------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeHttpxResponse:
    def __init__(self, status_code: int, json_data: Any = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeHttpxClient:
    def __init__(self, response: _FakeHttpxResponse):
        self._response = response
        self.captured: dict[str, Any] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, *, headers=None, params=None):
        self.captured = {"url": url, "headers": headers, "params": params}
        return self._response


def _set_actor(monkeypatch, actor_id):
    monkeypatch.setattr(
        memory_mod, "get_current_actor", lambda: actor_id,
    )


def _patch_audit(monkeypatch):
    events: list[dict] = []

    def _log(event_type, **kwargs):
        events.append({"type": event_type, **kwargs})

    monkeypatch.setattr(memory_mod.audit, "log_event", _log, raising=False)
    return events


def _patch_admin_key(monkeypatch, key="admin-proxy-key"):
    monkeypatch.setattr(memory_mod, "resolve_admin_key", lambda: key)


def _patch_decider(monkeypatch, *, allowed: bool, reason: str):
    calls: list[dict[str, Any]] = []

    def decide_memory_read(**kwargs):
        calls.append(dict(kwargs))
        return SimpleNamespace(allowed=allowed, reason=reason)

    monkeypatch.setattr(memory_mod, "decide_memory_read", decide_memory_read)
    return calls


def test_peer_reads_untagged_custom_private(monkeypatch, registry, peer_graph):
    """The canonical decision can allow an untagged private record."""
    _set_actor(monkeypatch, "spouse")
    _patch_admin_key(monkeypatch)
    audit_events = _patch_audit(monkeypatch)
    decision_calls = _patch_decider(
        monkeypatch,
        allowed=True,
        reason="family_legacy_untagged",
    )

    response = _FakeHttpxResponse(200, json_data={"value": "20 мая мотосервис"})
    client = _FakeHttpxClient(response)
    monkeypatch.setattr(
        memory_mod.httpx, "AsyncClient", lambda **kw: client,
    )

    tool = MemoryGetTool()
    result = _run(tool.execute(scope="private", key="moto", actor="owner"))

    assert result == "20 мая мотосервис"
    assert client.captured["headers"] == {"x-api-key": "admin-proxy-key"}
    assert client.captured["params"] == {"key": "private:owner:moto"}
    assert len(decision_calls) == 1
    allow_events = [e for e in audit_events
                    if e["type"] == "peer_private_read"
                    and e.get("decision") == "allow"
                    and e.get("reason") == "family_legacy_untagged"]
    assert len(allow_events) == 1


def test_peer_denied_secret_tagged(monkeypatch, registry, peer_graph):
    """The canonical decision hides a tagged record fail-closed."""
    _set_actor(monkeypatch, "spouse")
    _patch_admin_key(monkeypatch)
    audit_events = _patch_audit(monkeypatch)
    decision_calls = _patch_decider(
        monkeypatch,
        allowed=False,
        reason="no_common_topic",
    )

    wrapped = codec.encode("gift idea", [SECRET_TAG])
    response = _FakeHttpxResponse(200, json_data={"value": wrapped})
    client = _FakeHttpxClient(response)
    monkeypatch.setattr(
        memory_mod.httpx, "AsyncClient", lambda **kw: client,
    )

    tool = MemoryGetTool()
    result = _run(tool.execute(scope="private", key="gift", actor="owner"))

    # Fail-closed — indistinguishable from "no value stored".
    assert result == "(no value stored at 'private:owner:gift')"
    deny_events = [e for e in audit_events
                   if e["type"] == "peer_private_read"
                   and e.get("decision") == "deny"
                   and e.get("reason") == "no_common_topic"]
    assert len(decision_calls) == 1
    assert len(deny_events) == 1


def test_non_peer_denied(monkeypatch, registry, peer_graph):
    """An unrelated common-topic reader is denied by one stable reason."""
    _set_actor(monkeypatch, "unrelated")
    _patch_admin_key(monkeypatch)
    audit_events = _patch_audit(monkeypatch)
    decision_calls = _patch_decider(
        monkeypatch,
        allowed=False,
        reason="topic_without_family_relation",
    )
    wrapped = codec.encode("must-not-leak", ["topic_shared"])
    client = _FakeHttpxClient(
        _FakeHttpxResponse(200, json_data={"value": wrapped})
    )
    monkeypatch.setattr(
        memory_mod.httpx,
        "AsyncClient",
        lambda **kw: client,
    )

    tool = MemoryGetTool()
    result = _run(tool.execute(scope="private", key="moto", actor="owner"))

    assert result == "(no value stored at 'private:owner:moto')"
    deny_events = [e for e in audit_events
                   if e["type"] == "peer_private_read"
                   and e.get("decision") == "deny"
                   and e.get("reason") == "topic_without_family_relation"]
    assert len(decision_calls) == 1
    assert len(deny_events) == 1


def test_child_denied_parent_private(monkeypatch, registry, peer_graph):
    """A related reader without the record topic is denied canonically."""
    _set_actor(monkeypatch, "child_a")
    _patch_admin_key(monkeypatch)
    audit_events = _patch_audit(monkeypatch)
    decision_calls = _patch_decider(
        monkeypatch,
        allowed=False,
        reason="no_common_topic",
    )
    wrapped = codec.encode("must-not-leak", ["hidden_topic"])
    client = _FakeHttpxClient(
        _FakeHttpxResponse(200, json_data={"value": wrapped})
    )
    monkeypatch.setattr(
        memory_mod.httpx, "AsyncClient",
        lambda **kw: client,
    )

    tool = MemoryGetTool()
    result = _run(tool.execute(scope="private", key="moto", actor="owner"))

    assert result == "(no value stored at 'private:owner:moto')"
    deny_events = [e for e in audit_events
                   if e["type"] == "peer_private_read"
                   and e.get("decision") == "deny"
                   and e.get("reason") == "no_common_topic"]
    assert len(decision_calls) == 1
    assert len(deny_events) == 1


@pytest.mark.parametrize("reserved", [
    "value:memory",
    "value:user_profile",
    "value:heartbeat",
])
def test_peer_reads_reserved_slot_via_proxy(monkeypatch, registry, peer_graph, reserved):
    """The canonical decision can allow reserved slots through the proxy."""
    _set_actor(monkeypatch, "spouse")
    _patch_admin_key(monkeypatch)
    _patch_audit(monkeypatch)
    decision_calls = _patch_decider(
        monkeypatch,
        allowed=True,
        reason="family_legacy_untagged",
    )

    response = _FakeHttpxResponse(200, json_data={"value": "owner's reserved content"})
    client = _FakeHttpxClient(response)
    monkeypatch.setattr(
        memory_mod.httpx, "AsyncClient", lambda **kw: client,
    )

    tool = MemoryGetTool()
    result = _run(tool.execute(scope="private", key=reserved, actor="owner"))

    # Peer-proxy path: targets owner's namespace via admin key.
    assert client.captured["params"] == {"key": f"private:owner:{reserved}"}
    assert client.captured["headers"] == {"x-api-key": "admin-proxy-key"}
    assert result == "owner's reserved content"
    assert len(decision_calls) == 1


@pytest.mark.parametrize("reserved", [
    "value:memory",
    "value:user_profile",
    "value:heartbeat",
])
def test_peer_denied_secret_reserved_slot(monkeypatch, registry, peer_graph, reserved):
    """The canonical decision can hide a tagged reserved slot."""
    _set_actor(monkeypatch, "spouse")
    _patch_admin_key(monkeypatch)
    _patch_audit(monkeypatch)
    decision_calls = _patch_decider(
        monkeypatch,
        allowed=False,
        reason="no_common_topic",
    )

    from familia.acl import codec as _codec
    wrapped = _codec.encode("owner private journal entry", [SECRET_TAG])
    response = _FakeHttpxResponse(200, json_data={"value": wrapped})
    client = _FakeHttpxClient(response)
    monkeypatch.setattr(
        memory_mod.httpx, "AsyncClient", lambda **kw: client,
    )

    tool = MemoryGetTool()
    result = _run(tool.execute(scope="private", key=reserved, actor="owner"))

    assert result == f"(no value stored at 'private:owner:{reserved}')"
    assert len(decision_calls) == 1


def test_owner_reads_own_secret(monkeypatch, registry, peer_graph):
    """Owner always reads their own secret-tagged records — no actor= needed."""
    _set_actor(monkeypatch, "owner")
    _patch_audit(monkeypatch)

    wrapped = codec.encode("gift idea", [SECRET_TAG])
    response = _FakeHttpxResponse(200, json_data={"value": wrapped})
    client = _FakeHttpxClient(response)
    monkeypatch.setattr(
        memory_mod.httpx, "AsyncClient", lambda **kw: client,
    )

    monkeypatch.setattr(
        memory_mod, "get_engine",
        lambda: mock.MagicMock(evaluate=lambda ctx: mock.MagicMock(
            decision=memory_mod.Decision.ALLOW, reason=None,
        )),
    )

    tool = MemoryGetTool()
    result = _run(tool.execute(scope="private", key="gift"))

    # Owner-read path bypasses SECRET_TAG filter — own read sees own data.
    # The result is the unwrapped value when tags don't match
    # _check_read_acl OR when the wrapped value's tags include the actor.
    # In this owner case, the tag-ACL path will be invoked; ensure it
    # admin-bypasses (owner has role=admin).
    assert "gift idea" in result


def test_peer_read_when_missing_returns_no_value(monkeypatch, registry, peer_graph):
    """memX 404 for the peer's key returns the standard no-value string."""
    _set_actor(monkeypatch, "spouse")
    _patch_admin_key(monkeypatch)
    _patch_audit(monkeypatch)

    response = _FakeHttpxResponse(404, text="not found")
    client = _FakeHttpxClient(response)
    monkeypatch.setattr(
        memory_mod.httpx, "AsyncClient", lambda **kw: client,
    )

    tool = MemoryGetTool()
    result = _run(tool.execute(scope="private", key="nonexistent", actor="owner"))

    assert result == "(no value stored at 'private:owner:nonexistent')"
