"""Tests for heartbeat target-actor wiring (Option B fix).

Background: ``_pick_heartbeat_target`` previously returned the most-recently
active session, so heartbeat ticks would ride whichever participant chatted
last. In a multi-user (familia) install, that meant heartbeat-triggered
``ask_principal`` and memory tools ran under the wrong actor — system-tick
semantics belong to the configured owner, not the most-recent talker.

Fix: ``HeartbeatConfig.target_actor`` (config) pins the principal; familia
defaults it from ``FAMILIA_OWNER_ACTOR`` when blank. Resolution to a
concrete (channel, chat_id) lives in ``cli/commands.py`` and uses
``familia.principals.get_registry``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from familia import bootstrap as familia_bootstrap
from familia import principals as principals_mod
from familia.principals import Identity, Principal, PrincipalRegistry


class _HBStub:
    def __init__(self, target_actor: str = "") -> None:
        self.target_actor = target_actor


@pytest.fixture
def env_owner(monkeypatch: pytest.MonkeyPatch):
    def _set(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("FAMILIA_OWNER_ACTOR", raising=False)
        else:
            monkeypatch.setenv("FAMILIA_OWNER_ACTOR", value)
    return _set


def test_apply_defaults_fills_from_env(env_owner):
    env_owner("owner")
    hb = _HBStub(target_actor="")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == "owner"


def test_apply_defaults_respects_explicit_config(env_owner):
    """User-pinned config wins — env never overrides explicit value."""
    env_owner("owner")
    hb = _HBStub(target_actor="member_a")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == "member_a"


def test_apply_defaults_strips_whitespace_only(env_owner):
    env_owner("owner")
    hb = _HBStub(target_actor="   ")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == "owner"


def test_apply_defaults_no_env_no_change(env_owner):
    env_owner(None)
    hb = _HBStub(target_actor="")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == ""


def test_apply_defaults_strips_env_whitespace(env_owner):
    env_owner("  owner  ")
    hb = _HBStub(target_actor="")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == "owner"


# ---- Resolution semantics (mirrors _pick_heartbeat_target's lookup) ----

@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="O", identities=[
            Identity(channel="vk", sender_id="1000001"),
            Identity(channel="tg", sender_id="2000001"),
        ], memx_key="k1", roles=["admin"]),
        Principal(id="member_a", display_name="A", identities=[
            Identity(channel="vk", sender_id="1000002"),
        ], memx_key="k2", roles=[]),
        Principal(id="ghost", display_name="G", identities=[],
                  memx_key="k3", roles=[]),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    return reg


def _resolve(target_actor: str, enabled: set[str]) -> tuple[str, str] | None:
    """Use the current Familia runtime resolver, not the removed CLI helper."""
    from familia.nanobot_extension.runtime_services import resolve_heartbeat_target

    return resolve_heartbeat_target(target_actor, enabled)


def test_resolve_owner_to_vk(registry):
    assert _resolve("owner", {"vk"}) == ("vk", "1000001")


def test_resolve_picks_first_enabled_channel(registry):
    # Owner has both vk and tg; only tg enabled — should pick tg.
    assert _resolve("owner", {"tg"}) == ("tg", "2000001")


def test_resolve_unknown_actor(registry):
    assert _resolve("nosuch", {"vk"}) is None


def test_resolve_actor_with_no_identities(registry):
    assert _resolve("ghost", {"vk"}) is None


def test_resolve_actor_with_no_enabled_channel(registry):
    # member_a is only on vk; enabled = {tg} — no match.
    assert _resolve("member_a", {"tg"}) is None


def test_resolve_first_identity_wins_with_multiple_enabled_channels(registry):
    """When both channels are enabled, identity-list order decides.

    The principal in the fixture has identities=[vk, tg]. With both channels
    enabled, the first match (vk) wins. This pins the contract so a future
    refactor that reorders identities surfaces in CI rather than silently
    flipping the heartbeat target between restarts.
    """
    assert _resolve("owner", {"vk", "tg"}) == ("vk", "1000001")


# ---- make_principal_chat_validator (HIGH finding: cron 'to' injection) ----

def test_validator_accepts_known_identity(registry):
    validate = familia_bootstrap.make_principal_chat_validator()
    assert validate("vk", "1000001") is True
    assert validate("vk", "1000002") is True
    assert validate("tg", "2000001") is True


def test_validator_rejects_unknown_chat_id(registry):
    validate = familia_bootstrap.make_principal_chat_validator()
    assert validate("vk", "999999") is False


def test_validator_rejects_wrong_channel_for_known_chat_id(registry):
    """Same chat_id on a different channel must not match."""
    validate = familia_bootstrap.make_principal_chat_validator()
    # 1000001 is owner's vk id; owner has no slack identity.
    assert validate("slack", "1000001") is False


def test_validator_rejects_empty_inputs(registry):
    validate = familia_bootstrap.make_principal_chat_validator()
    assert validate("", "1000001") is False
    assert validate("vk", "") is False


@pytest.mark.asyncio
async def test_heartbeat_execution_sets_and_restores_target_actor(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    from familia.nanobot_extension import runtime_services
    from familia.principals import get_current_actor, get_current_channel, set_current_actor, set_current_channel

    captured: dict[str, object] = {}

    class Loop:
        enabled_channels = {"vk"}
        runtime_adapters = SimpleNamespace(
            resolve_heartbeat_target=runtime_services.resolve_heartbeat_target
        )
        tools = SimpleNamespace(get=lambda _name: None)

        async def process_direct(self, prompt: str, **kwargs: object) -> str:
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            assert get_current_actor() == "member_a"
            assert get_current_channel() == "vk"
            return "done"

    async def source(actor: str) -> str:
        assert actor == "member_a"
        return "## Active Tasks\n- check calendar"

    monkeypatch.setattr(runtime_services, "_heartbeat_source", source)
    monkeypatch.setattr(runtime_services, "_heartbeat_should_notify", AsyncMock(return_value=False))
    previous_actor = get_current_actor()
    previous_channel = get_current_channel()
    set_current_actor("caller")
    set_current_channel("telegram")
    try:
        assert await runtime_services.run_heartbeat("member_a", Loop()) == "done"
        request_context = captured["kwargs"]["request_context"]
        assert request_context.actor == "member_a"
        assert request_context.session_key == "familia:member_a:vk:1000002"
        assert get_current_actor() == "caller"
        assert get_current_channel() == "telegram"
    finally:
        set_current_actor(previous_actor)
        set_current_channel(previous_channel)


@pytest.mark.asyncio
async def test_heartbeat_execution_error_is_observable_and_restores_actor(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    from familia.nanobot_extension import runtime_services
    from familia.principals import get_current_actor, get_current_channel, set_current_actor, set_current_channel

    class Loop:
        enabled_channels = {"vk"}
        runtime_adapters = SimpleNamespace(
            resolve_heartbeat_target=runtime_services.resolve_heartbeat_target
        )
        tools = SimpleNamespace(get=lambda _name: None)

        async def process_direct(self, *_args: object, **_kwargs: object) -> str:
            assert get_current_actor() == "member_a"
            assert get_current_channel() == "vk"
            raise RuntimeError("heartbeat boom")

    monkeypatch.setattr(
        runtime_services,
        "_heartbeat_source",
        AsyncMock(return_value="## Active Tasks\n- task"),
    )
    previous_actor = get_current_actor()
    previous_channel = get_current_channel()
    set_current_actor("caller")
    set_current_channel("telegram")
    try:
        assert await runtime_services.run_heartbeat("member_a", Loop()) is None
        assert get_current_actor() == "caller"
        assert get_current_channel() == "telegram"
    finally:
        set_current_channel(previous_channel)
        set_current_actor(previous_actor)


@pytest.mark.asyncio
async def test_heartbeat_without_explicit_target_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from familia.nanobot_extension import runtime_services

    executed: list[str] = []

    class Loop:
        enabled_channels = {"slack"}
        runtime_adapters = SimpleNamespace(resolve_heartbeat_target=lambda *_args: None)

        async def process_direct(self, prompt: str, **_kwargs: object) -> str:
            executed.append(prompt)
            return "unexpected"

    monkeypatch.setattr(
        runtime_services,
        "_heartbeat_source",
        AsyncMock(return_value="## Active Tasks\n- stale default task"),
    )

    assert await runtime_services.run_heartbeat("member_a", Loop()) is None
    assert executed == []
