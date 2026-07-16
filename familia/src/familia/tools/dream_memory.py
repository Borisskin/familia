"""Dream-only tool for writing scoped memory under a privileged identity.

The per-scope Dream (#44 from SCENARIOS.md) routes private facts extracted
from one principal's history into their ``private:<actor>:*`` memX scope,
so that shared ``MEMORY.md`` stops being a leakage path.  Doing this from
the regular ``memory_set`` tool would be inconvenient — it can only write
to the *current* actor's private scope, and Dream has no single current
actor (it runs as system, summarizing many principals in one pass).

This tool therefore:

* takes an explicit ``scope`` + optional ``actor`` / ``other`` arguments,
* talks to memX using the key from ``$DREAM_CONSOLIDATOR_MEMX_KEY``
  (falls back to ``local_dev_key`` for single-node dev setups; in prod
  provision a dedicated full-ACL key in ``memx/config/acl.json``),
* evaluates policy as ``actor='dream_consolidator'`` — a single rule gates
  all writes in one place and they land in the audit log.

Only registered on the Dream agent's tool registry.  Not exposed to the
normal agent loop.
"""

from __future__ import annotations

from copy import deepcopy
import os
from typing import Any

import httpx
from loguru import logger

from familia.acl.peers import is_peer
from familia.memx_client import memx_base_url
from familia.policy import Decision, PolicyContext, get_engine
from familia.principals import ambiguous_pair_namespaces, pair_namespace_token
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema


CONSOLIDATOR_ACTOR = "dream_consolidator"
CONSOLIDATOR_KEY_ENV = "DREAM_CONSOLIDATOR_MEMX_KEY"
_MAX_CAS_ATTEMPTS = 3


class ScopedDreamEditGuard(Tool):
    """Allow protected file edits only for an explicit shared Dream finding."""

    def __init__(self, delegate: Tool) -> None:
        self._delegate = delegate

    @property
    def name(self) -> str:
        return self._delegate.name

    @property
    def description(self) -> str:
        return (
            self._delegate.description
            + " In Familia Dream, pass dream_scope='shared' for an explicit "
            "[FILE]/[FILE-REMOVE] edit. Scoped facts must use dream_memory_set."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        schema = deepcopy(self._delegate.parameters)
        properties = schema.setdefault("properties", {})
        properties["dream_scope"] = {
            "type": "string",
            "enum": ["shared"],
            "description": "Required only for explicit shared [FILE] findings.",
        }
        return schema

    async def execute(self, **kwargs: Any) -> Any:
        dream_scope = kwargs.pop("dream_scope", None)
        path = str(kwargs.get("path") or "").replace("\\", "/").rstrip("/")
        protected = (
            path == "USER.md"
            or path == "MEMORY.md"
            or path == "memory/MEMORY.md"
            or path.endswith("/USER.md")
            or path.endswith("/memory/MEMORY.md")
        )
        if protected and dream_scope != "shared":
            return (
                "Error: protected Dream file edit lacks explicit shared scope; "
                "route private/pair facts through dream_memory_set"
            )
        return await self._delegate.execute(**kwargs)


def _resolve_full_key(
    scope: str, key: str, actor: str | None, other: str | None
) -> tuple[str | None, str | None]:
    scope = (scope or "").strip()
    if not (key or "").strip():
        return None, "Skipped: malformed Dream item has no key"
    if scope == "shared":
        return f"shared:{key}", None
    if scope == "private":
        if not (actor or "").strip():
            return None, "Skipped: actorless private Dream item"
        return f"private:{actor}:value:memory", None
    if scope == "pair":
        if not (actor or "").strip() or not (other or "").strip():
            return None, "Skipped: actorless pair Dream item"
        if actor == other:
            return None, "Skipped: malformed pair Dream item names one principal twice"
        a, b = sorted([actor, other])
        return f"pair:{a}_{b}:{key}", None
    return None, f"Skipped: unknown Dream memory scope '{scope}'"


def _merge_memory_document(existing: str, incoming: str) -> str:
    """Append one Dream finding without discarding the actor's current document."""
    current = existing.strip()
    addition = incoming.strip()
    if not current:
        return addition
    if addition in current:
        return current
    return f"{current}\n{addition}"


@tool_parameters(
    tool_parameters_schema(
        scope=StringSchema(
            "Memory scope: 'shared' (family-wide), 'private' (of one principal; "
            "set 'actor' to the target), or 'pair' (set 'actor' + 'other')."
        ),
        actor=StringSchema(
            "For scope='private': the principal whose private scope to write. "
            "For scope='pair': one of the two principals. Omit for 'shared'."
        ),
        other=StringSchema(
            "For scope='pair': the second principal of the pair. Omit otherwise."
        ),
        key=StringSchema("Bare memory key without scope prefix (e.g. 'daily_routine')."),
        value=StringSchema("Value to store (use JSON-encoded string for structured data)."),
        required=["scope", "key", "value"],
    )
)
class DreamMemorySetTool(Tool):
    """Write scoped family memory on behalf of the Dream consolidator."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self._base_url_override = base_url
        self._api_key = api_key or os.environ.get(CONSOLIDATOR_KEY_ENV, "local_dev_key")

    @property
    def _base_url(self) -> str:
        return self._base_url_override or memx_base_url()

    @property
    def name(self) -> str:
        return "dream_memory_set"

    @property
    def description(self) -> str:
        return (
            "Write a scoped memory value extracted during Dream consolidation. "
            "Use scope='private' + actor=<id> + key='value:memory' to merge facts "
            "into that principal's private actor document "
            "(e.g. a private confession one principal made about work) — NEVER put such facts into "
            "MEMORY.md. scope='pair' + actor/other for facts only relevant to a pair. "
            "scope='shared' for facts the whole family knows or needs."
        )

    async def execute(
        self,
        scope: str,
        key: str,
        value: str,
        actor: str | None = None,
        other: str | None = None,
        **_: Any,
    ) -> str:
        # Defense-in-depth: this tool is only meant to be registered on the
        # Dream agent's loop (which pins ``set_current_actor(CONSOLIDATOR_ACTOR)``
        # for its turn). If it ever ends up on the main agent's registry by
        # mistake, the policy gate would still rubber-stamp anything as the
        # consolidator since ``actor=CONSOLIDATOR_ACTOR`` is hardcoded below.
        # Refuse to run unless the calling context actually IS the consolidator.
        from familia.principals import get_current_actor, get_registry

        current = get_current_actor()
        if current != CONSOLIDATOR_ACTOR:
            return (
                f"Error: dream_memory_set is only callable from the Dream "
                f"consolidator turn (current actor={current!r})"
            )
        full_key, err = _resolve_full_key(scope, key, actor, other)
        if err:
            logger.warning("dream_memory_set local skip: {}", err)
            return err
        if not (value or "").strip():
            logger.warning("dream_memory_set local skip: unneeded empty value")
            return "Skipped: unneeded Dream item has an empty value"
        registry = get_registry()
        resolved_scope = (scope or "").strip()
        target_actors = (
            [actor]
            if resolved_scope == "private"
            else [actor, other]
            if resolved_scope == "pair"
            else []
        )
        unknown = [
            candidate
            for candidate in target_actors
            if not candidate or registry.get(candidate) is None
        ]
        if unknown:
            logger.warning("dream_memory_set local skip: unknown principal")
            return "Skipped: Dream item names an unknown principal"
        if resolved_scope == "pair" and (
            not actor or not other or not is_peer(actor, other)
        ):
            logger.warning("dream_memory_set local skip: pair has no peer relationship")
            return "Skipped: pair Dream item requires an allowed peer relationship"
        if resolved_scope == "pair" and actor and other:
            pair_token = pair_namespace_token(actor, other)
            if pair_token in ambiguous_pair_namespaces(registry.ids):
                logger.warning(
                    "dream_memory_set local skip: ambiguous pair namespace {}",
                    pair_token,
                )
                return "Skipped: pair namespace belongs to multiple actor pairs"
        if resolved_scope != "pair":
            decision = get_engine().evaluate(
                PolicyContext(
                    action="memory.write",
                    actor=CONSOLIDATOR_ACTOR,
                    to_chat=full_key,
                )
            )
            if decision.decision is Decision.DENY:
                reason = decision.reason or "policy denied"
                return f"Error: Policy denied memory.write на '{full_key}' для {CONSOLIDATOR_ACTOR}: {reason}"
        if resolved_scope == "private" and full_key.endswith(":value:memory"):
            return await self._merge_private_memory(full_key, value)
        return await self._write_value(full_key, value)

    async def _write_value(self, full_key: str, value: str) -> str:
        """Write an already-routed shared/pair or non-document value."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    f"{self._base_url}/set",
                    headers={"x-api-key": self._api_key},
                    json={"key": full_key, "value": value},
                )
        except httpx.HTTPError as exc:
            return f"Error: memX unreachable ({type(exc).__name__}: {exc})"
        if r.status_code == 403:
            return f"Error: access denied by memX ACL for key '{full_key}' (check the consolidator memx-key ACL)"
        if r.status_code >= 400:
            return f"Error: memX {r.status_code}: {r.text[:200]}"
        try:
            payload = r.json()
        except (TypeError, ValueError):
            return "Error: memX returned an invalid non-JSON commit response"
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return "Error: memX returned an invalid commit response"
        if payload.get("updated") is not True:
            return "Error: memX semantic commit failed (updated:false)"
        logger.info("dream_memory_set stored at {}", full_key)
        return f"Stored at '{full_key}'"

    async def _merge_private_memory(self, full_key: str, value: str) -> str:
        """Merge one private finding with bounded expected-timestamp CAS retries."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                for _attempt in range(_MAX_CAS_ATTEMPTS):
                    current = await client.get(
                        f"{self._base_url}/get",
                        headers={"x-api-key": self._api_key},
                        params={"key": full_key},
                    )
                    if current.status_code == 403:
                        return (
                            "Error: access denied by memX ACL for key "
                            f"'{full_key}' (check the consolidator memx-key ACL)"
                        )
                    if current.status_code == 404:
                        current_payload = None
                    elif current.status_code >= 400:
                        return f"Error: memX {current.status_code}: {current.text[:200]}"
                    else:
                        try:
                            current_payload = current.json()
                        except (TypeError, ValueError):
                            return "Error: memX integrity failure: actor memory is not JSON"

                    if current_payload is None:
                        existing = ""
                        expected_ts: float | None = None
                    elif isinstance(current_payload, dict):
                        existing = current_payload.get("value")
                        expected_ts = current_payload.get("ts")
                        if (
                            not isinstance(existing, str)
                            or not isinstance(expected_ts, (int, float))
                            or isinstance(expected_ts, bool)
                        ):
                            return (
                                "Error: memX integrity failure: actor memory record "
                                "has invalid value/ts"
                            )
                    else:
                        return "Error: memX integrity failure: actor memory record is invalid"

                    merged = _merge_memory_document(existing, value)
                    committed = await client.post(
                        f"{self._base_url}/set",
                        headers={"x-api-key": self._api_key},
                        json={
                            "key": full_key,
                            "value": merged,
                            "expected_ts": expected_ts,
                        },
                    )
                    if committed.status_code == 403:
                        return (
                            "Error: access denied by memX ACL for key "
                            f"'{full_key}' (check the consolidator memx-key ACL)"
                        )
                    if committed.status_code >= 400:
                        return f"Error: memX {committed.status_code}: {committed.text[:200]}"
                    try:
                        payload = committed.json()
                    except (TypeError, ValueError):
                        return "Error: memX returned an invalid non-JSON commit response"
                    if not isinstance(payload, dict) or payload.get("ok") is not True:
                        return "Error: memX returned an invalid commit response"
                    if (
                        payload.get("status") == "committed"
                        and payload.get("committed") is True
                        and payload.get("updated") is True
                        and payload.get("retryable") is False
                    ):
                        logger.info("dream_memory_set merged at {}", full_key)
                        return f"Stored at '{full_key}'"
                    if (
                        payload.get("status") in {"conflict", "not_updated"}
                        and payload.get("retryable") is True
                    ):
                        continue
                    return "Error: memX semantic commit failed"
        except httpx.HTTPError as exc:
            return f"Error: memX unreachable ({type(exc).__name__}: {exc})"
        return f"Error: memX CAS conflict after {_MAX_CAS_ATTEMPTS} attempts"
