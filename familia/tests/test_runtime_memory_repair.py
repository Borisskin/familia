"""Focused regressions for the runtime memory-routing repair."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from familia import principals as principals_mod
from familia.acl import graph_io
from familia.acl import principal_memory as principal_memory_mod
from familia.acl.principal_memory import PrincipalMemoryClient
from familia.policy import Decision
from familia.principals import Identity, Principal, PrincipalRegistry
from familia.tools import dream_memory as dream_memory_mod
from familia.tools import memory as memory_mod
from familia.tools.memory import MemoryGetTool, _resolve_full_key


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


def test_principal_memory_rejects_peer_service_slot_before_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    dependency_calls: list[str] = []

    def forbidden_dependency(name: str) -> Callable[..., None]:
        def fail(*_args: object, **_kwargs: object) -> None:
            dependency_calls.append(name)
            raise AssertionError(f"{name} must not run for a peer service slot")

        return fail

    monkeypatch.setattr(
        principal_memory_mod,
        "get_engine",
        forbidden_dependency("policy"),
    )
    monkeypatch.setattr(
        graph_io,
        "resolve_admin_key",
        forbidden_dependency("admin key"),
    )
    monkeypatch.setattr(
        graph_io,
        "load_graph_value",
        forbidden_dependency("graph"),
    )
    monkeypatch.setattr(
        principal_memory_mod,
        "get_raw",
        forbidden_dependency("raw storage"),
    )

    client = PrincipalMemoryClient("actor_alpha", "alpha-key")

    assert client.get_other("actor_zeta", "value:memory") is None
    assert dependency_calls == []


@pytest.mark.parametrize(
    "legacy_decision",
    (Decision.ALLOW, Decision.DENY),
    ids=("legacy-allow", "legacy-deny"),
)
@pytest.mark.asyncio
async def test_runtime_pair_read_is_rejected_before_policy_and_http(
    monkeypatch: pytest.MonkeyPatch,
    legacy_decision: Decision,
) -> None:
    policy_calls: list[Decision] = []
    http_calls: list[dict[str, object]] = []

    def configured_legacy_engine() -> SimpleNamespace:
        policy_calls.append(legacy_decision)
        return SimpleNamespace(
            evaluate=lambda _context: SimpleNamespace(decision=legacy_decision)
        )

    def forbidden_http_client(**kwargs: object) -> None:
        http_calls.append(kwargs)
        raise AssertionError("pair rejection must not reach HTTP or storage")

    monkeypatch.setattr(memory_mod, "get_engine", configured_legacy_engine)
    monkeypatch.setattr(memory_mod.httpx, "AsyncClient", forbidden_http_client)

    result = await MemoryGetTool().execute(
        scope="pair:actor_alpha",
        key="value:memory",
    )

    assert result == (
        "Error: memory_get accepts only 'private' scope; "
        "use scope='private'. 'shared' and 'pair' are not readable."
    )
    assert policy_calls == []
    assert http_calls == []
