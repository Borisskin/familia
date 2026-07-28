"""Tag-based ACL on memory_get (SR-7, SR-10, SR-11).

Mocks httpx so memX traffic is fake; relies on the Graph fixtures to
shape who-sees-what.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from familia import principals as principals_mod
from familia.acl import codec
from familia.principals import Identity, Principal, PrincipalRegistry
from familia.tools.memory import MemoryGetTool


# ---- shared graphs fixture --------------------------------------------------

FAMILY_GRAPH = {
    "nodes": [
        {"id": "owner", "type": "principal"},
        {"id": "member_a", "type": "principal"},
        {"id": "child", "type": "principal"},
        {"id": "nanny", "type": "principal"},
    ],
    "edges": [
        {"from": "owner", "to": "member_a", "rel": "spouse_of"},
        {"from": "owner", "to": "child", "rel": "parent_of"},
        {"from": "member_a", "to": "child", "rel": "parent_of"},
        {"from": "nanny", "to": "child", "rel": "caregiver_of"},
    ],
    "updated_at_ms": 100,
}

TOPICS_GRAPH = {
    "nodes": [
        {"id": "school", "type": "topic", "kind": "abstract"},
        {"id": "finance", "type": "topic", "kind": "abstract"},
    ],
    "edges": [
        {"from": "school", "to": "child", "rel": "concerns",
         "concerns_as": "guardian_of"},
        {"from": "finance", "to": "owner", "rel": "concerns",
         "concerns_as": "guardian_of"},
        {"from": "finance", "to": "member_a", "rel": "concerns",
         "concerns_as": "guardian_of"},
    ],
    "updated_at_ms": 200,
}


def _store_get_response(value: Any, *, ts: float | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    payload = {"value": value}
    if ts is not None:
        payload["ts"] = ts
    r.text = json.dumps(payload, ensure_ascii=False)
    r.json.return_value = payload
    return r


def _patched_client(values_by_key: dict[str, Any]):
    """httpx.AsyncClient that returns canned responses by key."""
    captured_writes: list[dict[str, Any]] = []

    async def get(url, headers=None, params=None):
        key = (params or {}).get("key", "")
        value = values_by_key.get(key)
        if key in {"shared:family.graph", "shared:topics.graph"}:
            return _store_get_response(value)
        return _store_get_response(value, ts=41.0)

    async def post(url, headers=None, json=None, **_):
        captured_writes.append(json or {})
        r = MagicMock()
        r.status_code = 200
        payload = {
            "ok": True,
            "status": "committed",
            "committed": True,
            "updated": True,
            "retryable": False,
            "version": 42.0,
        }
        r.text = "committed"
        r.json.return_value = payload
        return r

    client = AsyncMock()
    client.get = AsyncMock(side_effect=get)
    client.post = AsyncMock(side_effect=post)
    return client, captured_writes


def _make_registry(monkeypatch, role_overrides=None):
    overrides = role_overrides or {}
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="O",
                  identities=[Identity(channel="vk", sender_id="1000001")],
                  memx_key="k_owner",
                  roles=["admin"]),
        Principal(id="member_a", display_name="A",
                  identities=[Identity(channel="vk", sender_id="1000002")],
                  memx_key="k_member_a", roles=overrides.get("member_a", [])),
        Principal(id="child", display_name="V",
                  identities=[Identity(channel="tg", sender_id="3000001")],
                  memx_key="k_child", roles=overrides.get("child", ["child"])),
        Principal(id="nanny", display_name="N",
                  identities=[Identity(channel="vk", sender_id="1000003")],
                  memx_key="k_nanny", roles=overrides.get("nanny", [])),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    return reg


@pytest.fixture
def graphs_in_memx():
    return {
        "shared:family.graph": FAMILY_GRAPH,
        "shared:topics.graph": TOPICS_GRAPH,
    }


# ---- read-side --- SR-10 fail-closed --------------------------------------

def test_shared_scope_is_rejected_before_tag_intersection(
    monkeypatch,
    graphs_in_memx,
):
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("member_a")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "member_a": frozenset()})

    wrapped = codec.encode("тетради, ручки", ["child", "school"])
    values = dict(graphs_in_memx)
    values["shared:child.school_supplies"] = wrapped

    client, _ = _patched_client(values)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemoryGetTool(base_url="http://nope")
        out = asyncio.run(tool.execute(
            scope="shared", key="child.school_supplies",
        ))
    assert out.startswith("Error:")
    assert "private" in out.lower()
    client.get.assert_not_awaited()


def test_shared_scope_denial_does_not_probe_invisible_record(
    monkeypatch,
    graphs_in_memx,
):
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("nanny")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "nanny": frozenset()})

    # Record about finance, nanny has no path to it.
    wrapped = codec.encode("плати налог", ["finance"])
    values = dict(graphs_in_memx)
    values["shared:money_topic"] = wrapped

    client, _ = _patched_client(values)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemoryGetTool(base_url="http://nope")
        out = asyncio.run(tool.execute(scope="shared", key="money_topic"))
    assert out.startswith("Error:")
    assert "private" in out.lower()
    client.get.assert_not_awaited()


def test_shared_legacy_lookalike_is_not_returned(
    monkeypatch,
    graphs_in_memx,
):
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("nanny")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "nanny": frozenset()})

    legacy_lookalike = json.dumps({"tags": ["finance"], "value": "leak"})
    values = dict(graphs_in_memx)
    values["shared:adversarial"] = legacy_lookalike

    client, _ = _patched_client(values)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemoryGetTool(base_url="http://nope")
        out = asyncio.run(tool.execute(scope="shared", key="adversarial"))
    assert out.startswith("Error:")
    assert "private" in out.lower()
    assert "leak" not in out
    client.get.assert_not_awaited()
