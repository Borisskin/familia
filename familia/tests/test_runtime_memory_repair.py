"""Focused regressions for the runtime memory-routing repair."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from familia import principals as principals_mod
from familia.acl import graph_io, peers
from familia.acl import principal_memory as principal_memory_mod
from familia.acl.principal_memory import PrincipalMemoryClient
from familia.policy import Decision
from familia.principals import Identity, Principal, PrincipalRegistry, set_current_actor
from familia.tools import dream_memory as dream_memory_mod
from familia.tools import memory as memory_mod
from familia.tools.memory import MemoryGetTool, _resolve_full_key


def _legacy_policy_must_not_run() -> None:
    raise AssertionError(
        "canonical pair routing must not consult the legacy default-deny policy"
    )


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    registry = PrincipalRegistry(
        [
            Principal(
                id="actor_alpha",
                display_name="Actor Alpha",
                identities=[Identity(channel="test", sender_id="alpha")],
                memx_key="alpha-key",
                roles=["admin"],
            ),
            Principal(
                id="actor_zeta",
                display_name="Actor Zeta",
                identities=[Identity(channel="test", sender_id="zeta")],
                memx_key="zeta-key",
                roles=[],
            ),
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", registry)
    return registry


def test_runtime_pair_key_matches_complete_origin_main_format(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    monkeypatch.setattr(memory_mod, "is_peer", lambda _left, _right: True)
    full_key, error = _resolve_full_key(
        "pair:actor_alpha",
        "value:memory",
        "actor_zeta",
    )

    assert error is None
    assert full_key == "pair:actor_alpha_actor_zeta:value:memory"


def test_runtime_pair_rejects_unknown_current_actor(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    monkeypatch.setattr(memory_mod, "is_peer", lambda _left, _right: True)

    full_key, error = _resolve_full_key(
        "pair:actor_alpha",
        "value:memory",
        "actor_unknown",
    )

    assert full_key is None
    assert error is not None and "unknown current principal" in error


def test_runtime_pair_rejects_principals_without_peer_relation(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    monkeypatch.setattr(memory_mod, "is_peer", lambda _left, _right: False)

    full_key, error = _resolve_full_key(
        "pair:actor_alpha",
        "value:memory",
        "actor_zeta",
    )

    assert full_key is None
    assert error is not None and "peer relationship" in error


def test_principal_memory_restores_peer_private_read(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    monkeypatch.setattr(
        principal_memory_mod,
        "get_engine",
        lambda: SimpleNamespace(
            evaluate=lambda _context: SimpleNamespace(decision=Decision.ALLOW)
        ),
        raising=False,
    )
    monkeypatch.setattr(peers, "is_peer", lambda left, right: {left, right} == {
        "actor_alpha",
        "actor_zeta",
    })
    monkeypatch.setattr(graph_io, "resolve_admin_key", lambda: "admin-proxy-key")
    family_graph = {
        "nodes": [
            {"id": "actor_alpha", "type": "principal"},
            {"id": "actor_zeta", "type": "principal"},
        ],
        "edges": [
            {
                "from": "actor_alpha",
                "to": "actor_zeta",
                "rel": "spouse_of",
            },
        ],
        "updated_at_ms": 1,
    }
    topics_graph = {"nodes": [], "edges": [], "updated_at_ms": 1}
    monkeypatch.setattr(
        graph_io,
        "load_graph_value",
        lambda key, **_kwargs: (
            family_graph
            if key == "shared:family.graph"
            else topics_graph
        ),
    )
    monkeypatch.setattr(
        principal_memory_mod,
        "get_raw",
        lambda key, *, api_key: "peer-visible"
        if key == "private:actor_zeta:value:memory" and api_key == "admin-proxy-key"
        else None,
    )

    client = PrincipalMemoryClient("actor_alpha", "alpha-key")

    assert client.get_other("actor_zeta", "value:memory") == "peer-visible"


class _Response:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(self._payload)

    def json(self) -> Any:
        return self._payload


class _RecordingClient:
    def __init__(
        self,
        requests: list[dict[str, Any]],
        *,
        get_response: _Response,
        post_response: _Response,
    ) -> None:
        self._requests = requests
        self._get_response = get_response
        self._post_response = post_response

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def get(self, _url: str, **_kwargs: Any) -> _Response:
        return self._get_response

    async def post(self, _url: str, **kwargs: Any) -> _Response:
        self._requests.append(kwargs["json"])
        return self._post_response


@pytest.mark.asyncio
async def test_runtime_pair_read_bypasses_legacy_policy_after_graph_gate(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    monkeypatch.setattr(memory_mod, "is_peer", lambda _left, _right: True)
    monkeypatch.setattr(
        memory_mod,
        "get_engine",
        _legacy_policy_must_not_run,
    )
    monkeypatch.setattr(
        memory_mod.httpx,
        "AsyncClient",
        lambda **_kwargs: _RecordingClient(
            [],
            get_response=_Response(200, "pair-visible"),
            post_response=_Response(500),
        ),
    )
    set_current_actor("actor_zeta")

    result = await MemoryGetTool().execute(
        scope="pair:actor_alpha",
        key="value:memory",
    )

    assert result == "pair-visible"


@pytest.mark.asyncio
async def test_runtime_pair_read_ignores_legacy_policy_after_graph_gate(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    monkeypatch.setattr(memory_mod, "is_peer", lambda _left, _right: True)
    monkeypatch.setattr(
        memory_mod,
        "get_engine",
        _legacy_policy_must_not_run,
    )
    monkeypatch.setattr(
        memory_mod.httpx,
        "AsyncClient",
        lambda **_kwargs: _RecordingClient(
            [],
            get_response=_Response(200, "must-not-be-visible"),
            post_response=_Response(500),
        ),
    )
    set_current_actor("actor_zeta")

    result = await MemoryGetTool().execute(
        scope="pair:actor_alpha",
        key="value:memory",
    )

    assert result == "must-not-be-visible"
