"""Focused Dream manager and private-memory CAS regressions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from familia import principals as principals_mod
from familia.bootstrap import make_dream_turn_context
from familia.nanobot_extension.cron import make_dream_tool_installers
from familia.policy import Decision
from familia.principals import Identity, Principal, PrincipalRegistry, set_current_actor
from familia.tools import dream_memory as dream_memory_mod
from nanobot.agent.memory import Dream, MemoryStore
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.prompt_templates import render_template


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


@pytest.fixture
def allow_dream_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dream_memory_mod,
        "get_engine",
        lambda: SimpleNamespace(
            evaluate=lambda _context: SimpleNamespace(
                decision=Decision.ALLOW,
                reason=None,
            )
        ),
    )


@dataclass
class _Response:
    status_code: int
    payload: Any

    @property
    def text(self) -> str:
        return repr(self.payload)

    def json(self) -> Any:
        return self.payload


class _MemX:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.gets: list[str] = []
        self.posts: list[dict[str, Any]] = []
        self.failures: dict[str, str] = {}
        self.conflict_once: set[str] = set()

    async def get(self, _url: str, *, params: dict[str, Any], **_kwargs: Any) -> _Response:
        key = params["key"]
        self.gets.append(key)
        failure = self.failures.get(key)
        if failure == "auth":
            return _Response(403, {"detail": "denied"})
        if failure == "integrity":
            return _Response(200, {"value": "missing expected timestamp"})
        return _Response(200, self.records.get(key))

    async def post(self, _url: str, *, json: dict[str, Any], **_kwargs: Any) -> _Response:
        body = dict(json)
        self.posts.append(body)
        key = body["key"]
        failure = self.failures.get(key)
        if failure == "auth":
            return _Response(403, {"detail": "denied"})
        if failure == "integrity":
            return _Response(409, {"detail": "corruption_needs_repair"})
        if "expected_ts" not in body:
            return _Response(409, {"detail": "expected_ts is required"})

        current = self.records.get(key)
        current_ts = current["ts"] if current is not None else None
        if body["expected_ts"] != current_ts:
            return _Response(
                200,
                {
                    "ok": True,
                    "status": "conflict",
                    "committed": False,
                    "updated": False,
                    "retryable": True,
                    "version": current_ts,
                },
            )
        if key in self.conflict_once:
            self.conflict_once.remove(key)
            existing = current["value"] if current is not None else ""
            next_ts = (current_ts or 0.0) + 1.0
            self.records[key] = {
                "value": f"{existing}\nconcurrent fact".strip(),
                "ts": next_ts,
            }
            return _Response(
                200,
                {
                    "ok": True,
                    "status": "conflict",
                    "committed": False,
                    "updated": False,
                    "retryable": True,
                    "version": next_ts,
                },
            )

        next_ts = (current_ts or 0.0) + 1.0
        self.records[key] = {"value": body["value"], "ts": next_ts}
        return _Response(
            200,
            {
                "ok": True,
                "status": "committed",
                "committed": True,
                "updated": True,
                "retryable": False,
                "version": next_ts,
            },
        )


class _Client:
    def __init__(self, backend: _MemX) -> None:
        self.backend = backend

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def get(self, *args: Any, **kwargs: Any) -> _Response:
        return await self.backend.get(*args, **kwargs)

    async def post(self, *args: Any, **kwargs: Any) -> _Response:
        return await self.backend.post(*args, **kwargs)


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
) -> tuple[Dream, MemoryStore]:
    store = MemoryStore(tmp_path)
    dream = Dream(
        store=store,
        provider=provider,
        model="test-model",
        max_batch_size=10,
        dream_tool_installers=make_dream_tool_installers(),
        dream_turn_context=make_dream_turn_context(),
    )
    return dream, store


def test_phase2_private_route_uses_exact_actor_document() -> None:
    prompt = render_template(
        "agent/dream_phase2.md",
        strip=True,
        skill_creator_path="skills/skill-creator/SKILL.md",
    )

    assert "key='value:memory'" in prompt
    private_instructions = prompt.split("- [PRIVATE:<actor>]", 1)[1].split(
        "- [PAIR:<a>,<b>]",
        1,
    )[0]
    assert "<stable_key>" not in private_instructions
    assert "[FILE] entries: add the described content" in prompt
    assert "dream_scope='shared'" in prompt


def test_private_route_cannot_bypass_actor_document(
    registry: PrincipalRegistry,
) -> None:
    full_key, error = dream_memory_mod._resolve_full_key(
        "private",
        "feelings",
        "actor_alpha",
        None,
    )

    assert error is None
    assert full_key == "private:actor_alpha:value:memory"


@pytest.mark.asyncio
async def test_dream_run_continues_local_skips_and_commits_a_then_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
    allow_dream_policy: None,
) -> None:
    backend = _MemX()
    monkeypatch.setattr(
        dream_memory_mod.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(backend),
    )
    analysis = "\n".join(
        [
            "[PRIVATE:actor_alpha] fact A",
            "[PRIVATE:actor_missing] unknown item",
            "[PRIVATE:] actorless item",
            "[PRIVATE:actor_alpha] malformed item",
            "[PRIVATE:actor_alpha] unneeded item",
            "[PRIVATE:actor_beta] fact B",
        ]
    )
    provider = _ScriptedProvider(
        analysis,
        [
            _call("a", scope="private", actor="actor_alpha", key="value:memory", value="fact A"),
            _call("unknown", scope="private", actor="actor_missing", key="value:memory", value="unknown"),
            _call("actorless", scope="private", key="value:memory", value="actorless"),
            _call("malformed", scope="malformed", actor="actor_alpha", key="value:memory", value="bad"),
            _call("unneeded", scope="private", actor="actor_alpha", key="value:memory", value="   "),
            _call("b", scope="private", actor="actor_beta", key="value:memory", value="fact B"),
        ],
    )
    dream, store = _dream(tmp_path, provider)
    store.append_history("fact A", actor="actor_alpha")
    store.append_history("unknown", actor="actor_missing")
    store.append_history("actorless")
    store.append_history("malformed", actor="actor_alpha")
    store.append_history("unneeded", actor="actor_alpha")
    final_cursor = store.append_history("fact B", actor="actor_beta")

    assert await dream.run() is True
    assert store.get_last_dream_cursor() == final_cursor
    assert set(backend.records) == {
        "private:actor_alpha:value:memory",
        "private:actor_beta:value:memory",
    }
    assert backend.records["private:actor_alpha:value:memory"]["value"] == "fact A"
    assert backend.records["private:actor_beta:value:memory"]["value"] == "fact B"
    assert sum(result.startswith("Skipped:") for result in provider.tool_results) == 4
    assert not any(key.startswith("shared:") for key in backend.records)


@pytest.mark.asyncio
async def test_private_actor_memory_retries_expected_ts_without_lost_update(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
    allow_dream_policy: None,
) -> None:
    key = "private:actor_alpha:value:memory"
    backend = _MemX()
    backend.records[key] = {"value": "existing fact", "ts": 1.0}
    backend.conflict_once.add(key)
    monkeypatch.setattr(
        dream_memory_mod.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(backend),
    )
    set_current_actor(dream_memory_mod.CONSOLIDATOR_ACTOR)
    tool = dream_memory_mod.DreamMemorySetTool(api_key="dream-key")

    result = await tool.execute(
        scope="private",
        actor="actor_alpha",
        key="value:memory",
        value="incoming fact",
    )

    assert result == "Stored at 'private:actor_alpha:value:memory'"
    assert [post["expected_ts"] for post in backend.posts] == [1.0, 2.0]
    merged = backend.records[key]["value"]
    assert "existing fact" in merged
    assert "concurrent fact" in merged
    assert "incoming fact" in merged


@pytest.mark.parametrize("failure", ["auth", "integrity"])
@pytest.mark.asyncio
async def test_dream_systemic_failure_stops_before_later_actor(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
    allow_dream_policy: None,
) -> None:
    first_key = "private:actor_alpha:value:memory"
    later_key = "private:actor_beta:value:memory"
    backend = _MemX()
    backend.failures[first_key] = failure
    monkeypatch.setattr(
        dream_memory_mod.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(backend),
    )
    provider = _ScriptedProvider(
        "[PRIVATE:actor_alpha] fact A\n[PRIVATE:actor_beta] fact B",
        [
            _call("a", scope="private", actor="actor_alpha", key="value:memory", value="fact A"),
            _call("b", scope="private", actor="actor_beta", key="value:memory", value="fact B"),
        ],
    )
    dream, store = _dream(tmp_path, provider)
    store.append_history("fact A", actor="actor_alpha")
    store.append_history("fact B", actor="actor_beta")

    assert await dream.run() is False
    assert store.get_last_dream_cursor() == 0
    assert later_key not in backend.gets
    assert all(post["key"] != later_key for post in backend.posts)
    assert later_key not in backend.records
