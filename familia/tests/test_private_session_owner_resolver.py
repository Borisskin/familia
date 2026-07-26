"""Simple owner resolution for private-session memory consolidation."""

from __future__ import annotations

import pytest

from familia.principals import Identity, Principal, PrincipalRegistry


@pytest.fixture
def registry() -> PrincipalRegistry:
    return PrincipalRegistry(
        [
            Principal(
                id="principal_alpha",
                identities=[
                    Identity(channel="telegram", sender_id="1001|alice"),
                    Identity(channel="vk", sender_id="2001"),
                ],
            ),
            Principal(
                id="principal_beta",
                identities=[
                    Identity(channel="telegram", sender_id="1002|bob"),
                ],
            ),
        ]
    )


@pytest.fixture
def resolver(registry: PrincipalRegistry):
    from familia.private_session_owner import PrivateSessionOwnerResolver

    return PrivateSessionOwnerResolver(registry_getter=lambda: registry)


@pytest.mark.parametrize(
    "session_key",
    ["telegram:1001", "vk:2001"],
)
@pytest.mark.asyncio
async def test_private_session_key_resolves_exact_owner(
    resolver,
    session_key: str,
) -> None:
    messages = [
        {"role": "user", "content": "legacy actorless turn"},
        {"role": "assistant", "content": "assistant turn"},
        {
            "role": "user",
            "actor": "principal_alpha",
            "content": "tagged turn",
        },
    ]

    assert await resolver(session_key, messages) == "principal_alpha"


@pytest.mark.asyncio
async def test_other_user_actor_returns_no_owner(resolver) -> None:
    result = await resolver(
        "telegram:1001",
        [
            {
                "role": "user",
                "actor": "principal_beta",
                "content": "foreign turn",
            }
        ],
    )

    assert result is None


@pytest.mark.asyncio
async def test_unknown_private_chat_returns_no_owner(resolver) -> None:
    result = await resolver(
        "telegram:9999",
        [{"role": "user", "content": "unknown private chat"}],
    )

    assert result is None


@pytest.mark.parametrize(
    "session_key",
    [
        "",
        "missing-separator",
        "unified:default",
        "telegram:",
        ":1001",
    ],
)
@pytest.mark.asyncio
async def test_unsupported_session_key_returns_no_owner(
    resolver,
    session_key: str,
) -> None:
    result = await resolver(
        session_key,
        [{"role": "user", "content": "unsupported"}],
    )

    assert result is None
