"""Familia scoped-memory tools backed by memX.

Two tools (``memory_get`` / ``memory_set``) give the agent scoped access
to a shared-across-principals memory service.  The caller specifies a
``scope`` (``shared``, ``private``, ``pair:<other_id>``) and a bare
``key``; the tool composes the actual memX key and calls memX with the
current actor's API key, so ACL enforcement happens server-side.

Key shapes (match the memX ACL naming conventions):

* ``shared:<key>``
* ``private:<actor_id>:<key>``
* ``pair:<a>_<b>:<key>`` — where ``a`` and ``b`` are sorted alphabetically
  so ``pair:a_b`` is identical regardless of which of ``a`` or ``b`` is the
  caller.

Base URL is taken from ``MEMX_BASE_URL`` (default
``http://memx-backend:8000``).  If the current actor is unknown
or lacks an ``memx_key``, the tool returns an error string.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

from familia import audit
from familia.acl import codec
from familia.acl import schema as acl_schema
from familia.acl.graph_io import resolve_admin_key
from familia.acl.peers import is_peer
from familia.acl.principal_memory import (
    _NO_MATCHING_STATIC_POLICY,
    _decode_atomic_memory_catalog,
    canonical_memory_tags,
    decide_memory_read,
    is_valid_atomic_memory_key,
)
from familia.acl.reachable import reachable_tag_ids
from familia.memx_client import memx_base_url
from familia.policy import Decision, PolicyContext, get_engine
from familia.principals import (
    ambiguous_pair_namespaces,
    get_current_actor,
    get_registry,
    pair_namespace_token,
)
from familia.roles import get_effective_roles

SCOPE_DESC = (
    "Memory read scope. Only 'private' is accepted; 'shared' and 'pair' "
    "are rejected before policy or storage access. Own private keys are "
    "read as their owner. When 'actor' names another owner, only an exact "
    "memory:<fact_id> may pass that owner's exact trusted catalog entry, "
    "a direct family relation, and an exact common topic. Service, internal, "
    "migration, and secret records remain owner-only."
)

# Opt-out tag: a record carrying this tag is readable only by its owner,
# regardless of peer-edges. Used for genuinely sensitive content the user
# does not want to share with peers despite the family-by-default model
# (gifts, therapy/health notes, work secrets).
SECRET_TAG = "secret"
_PEER_STORE_UNAVAILABLE = "Error: memory store unavailable"
_PEER_DENIED_REASON = "foreign_read_denied"

# Synthetic tags that don't refer to a graph identity (principal id or
# topic id). They are ACL-modifiers, not reachability handles, so
# _check_write_acl must not require the writer to "reach" them.
_SYSTEM_TAGS = frozenset({SECRET_TAG})

# Hard cap on a single memX value. memX itself doesn't enforce one and a
# jailbroken agent could otherwise fill the store with multi-MB blobs.
# 64 KiB is generous for natural-language facts/JSON we actually use.
_MAX_VALUE_BYTES = 64 * 1024


# SR-14 belt-and-suspenders. Tool-level refusal so that policy
# misconfiguration cannot accidentally open chat-side writes to
# structural keys (graphs/roles/migrations). The canonical write paths are
# admin_grant/admin_revoke (for roles) and the `familia` CLI (for graphs and
# migration state).
_RESERVED_STRUCTURAL_PREFIXES = (
    "shared:roles.",
    "shared:family.graph",
    "shared:topics.graph",
    "shared:familia.migrations.",
)


def _is_reserved_structural_key(full_key: str) -> bool:
    return any(full_key.startswith(prefix)
               for prefix in _RESERVED_STRUCTURAL_PREFIXES)


# ---- tag-ACL helpers (Stage 5) ---------------------------------------------

# Both graphs are read with the actor's own memx_key — they're public-ish
# (every principal can read shared:family.graph and shared:topics.graph
# per current policy).
def _empty_graph_mapping() -> dict[str, Any]:
    return {"nodes": [], "edges": [], "updated_at_ms": 0}


async def _fetch_raw_graph(
    api_key: str,
    key: str,
    base_url: str,
    *,
    fail_on_unavailable: bool = False,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{base_url}/get",
                headers={"x-api-key": api_key},
                params={"key": key},
            )
    except httpx.HTTPError as exc:
        logger.warning("memX graph {} unreachable: {}", key, exc)
        if fail_on_unavailable:
            raise
        return _empty_graph_mapping()
    try:
        status_code = r.status_code
    except Exception:  # noqa: BLE001
        return _empty_graph_mapping()
    if type(status_code) is not int:
        return _empty_graph_mapping()
    if status_code >= 500:
        if fail_on_unavailable:
            raise RuntimeError("memX graph storage unavailable")
        logger.warning("memX graph {} returned {}", key, status_code)
        return _empty_graph_mapping()
    if status_code in {403, 404}:
        return _empty_graph_mapping()
    if status_code != 200:
        logger.warning("memX graph {} returned {}", key, status_code)
        return _empty_graph_mapping()
    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        return _empty_graph_mapping()
    if payload is None:
        return _empty_graph_mapping()
    raw = payload.get("value", payload) if isinstance(payload, dict) else payload
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            # SR-10: fail-closed.
            logger.warning("memX graph {} value is malformed JSON", key)
            return _empty_graph_mapping()
    if not isinstance(raw, dict):
        logger.warning("memX graph {} value is not an object", key)
        return _empty_graph_mapping()
    raw.setdefault("nodes", [])
    raw.setdefault("edges", [])
    raw.setdefault("updated_at_ms", 0)
    return raw


async def _fetch_graph(api_key: str, key: str, base_url: str) -> acl_schema.Graph:
    return acl_schema.Graph.from_dict(
        await _fetch_raw_graph(api_key, key, base_url)
    )


def _decision_graph(graph: acl_schema.Graph) -> dict[str, Any]:
    """Convert the canonical typed graph into the pure decider's mapping."""
    return {
        "nodes": [
            {
                "id": node.id,
                "type": node.type,
                "display_name": node.display_name,
                "aliases": list(node.aliases),
                "kind": node.kind,
            }
            for node in graph.nodes
        ],
        "edges": [
            {
                "from": edge.src,
                "to": edge.dst,
                "rel": edge.rel,
                "concerns_as": edge.concerns_as,
            }
            for edge in graph.edges
        ],
        "updated_at_ms": graph.updated_at_ms,
    }


def _principal_role_map() -> dict[str, frozenset[str]]:
    """Static-roles snapshot for the SR-2 child asymmetry check."""
    reg = get_registry()
    return {
        pid: frozenset(p.roles or [])
        for pid in reg.ids
        if (p := reg.get(pid)) is not None
    }


def _is_admin(actor_id: str) -> bool:
    return "admin" in get_effective_roles(actor_id)


async def _reachable_for(actor_id: str, api_key: str, base_url: str) -> set[str]:
    family = await _fetch_graph(api_key, "shared:family.graph", base_url)
    topics = await _fetch_graph(api_key, "shared:topics.graph", base_url)
    return reachable_tag_ids(family, topics, actor_id, _principal_role_map())


async def _topic_write_state(
    actor_id: str,
    api_key: str,
    topic: str,
    base_url: str,
) -> str:
    """Return whether an Admin-created topic is shared, isolated, or unavailable."""
    try:
        topics = await _fetch_graph(api_key, "shared:topics.graph", base_url)
    except Exception:  # noqa: BLE001
        return "unavailable"

    topic_ids = {
        node.id
        for node in topics.nodes
        if node.type == "topic"
    }
    if topic not in topic_ids:
        return "unavailable"

    linked_principals = {
        edge.dst
        for edge in topics.edges
        if edge.rel == "concerns" and edge.src == topic
    }
    if not linked_principals:
        return "isolated"

    if not _is_admin(actor_id):
        try:
            family = await _fetch_graph(
                api_key,
                "shared:family.graph",
                base_url,
            )
            reachable = reachable_tag_ids(
                family,
                topics,
                actor_id,
                _principal_role_map(),
            )
        except Exception:  # noqa: BLE001
            return "unavailable"
        if topic not in reachable:
            return "unavailable"

    return "shared" if linked_principals - {actor_id} else "isolated"


async def _check_read_acl(
    actor_id: str, api_key: str, record_tags: set[str], full_key: str,
) -> tuple[bool, str]:
    """SR-7-mirror: gate reads of tagged records on reachable intersection.

    Admin bypasses (SR-2 carved-out at top of stack). Returns (allow, reason)
    and emits a ``tag_acl_decision`` audit event regardless.
    """
    if _is_admin(actor_id):
        audit.log_event(
            "tag_acl_decision", op="read", actor=actor_id,
            full_key=full_key, record_tags=sorted(record_tags),
            decision="allow", reason="admin_bypass",
        )
        return True, "admin_bypass"
    base_url = memx_base_url()
    reachable = await _reachable_for(actor_id, api_key, base_url)
    intersection = reachable & record_tags
    decision = "allow" if intersection else "deny"
    audit.log_event(
        "tag_acl_decision", op="read", actor=actor_id,
        full_key=full_key, record_tags=sorted(record_tags),
        reachable=sorted(reachable)[:200],  # cap defensively for SIEM
        decision=decision,
        reason=("intersection_nonempty" if intersection else "no_intersection"),
    )
    return bool(intersection), decision


async def _check_write_acl(
    actor_id: str, api_key: str, tags: set[str], full_key: str,
) -> tuple[bool, str]:
    """SR-7: writer must have access to every tag they're trying to set.

    System tags (currently just ``secret``) are exempt — they are
    synthetic ACL hints, not graph identities. ``secret`` lets any
    actor narrow their own record's visibility to themselves alone,
    without needing to be reachable to that "tag-id" in the graph.
    """
    if _is_admin(actor_id):
        audit.log_event(
            "tag_acl_decision", op="write", actor=actor_id,
            full_key=full_key, record_tags=sorted(tags),
            decision="allow", reason="admin_bypass",
        )
        return True, "admin_bypass"
    base_url = memx_base_url()
    reachable = await _reachable_for(actor_id, api_key, base_url)
    # System tags don't participate in the reachable-set check.
    tags_to_check = tags - _SYSTEM_TAGS
    missing = tags_to_check - reachable
    decision = "allow" if not missing else "deny"
    audit.log_event(
        "tag_acl_decision", op="write", actor=actor_id,
        full_key=full_key, record_tags=sorted(tags),
        reachable=sorted(reachable)[:200],
        decision=decision,
        reason=("all_tags_reachable" if not missing
                else f"unreachable_tags:{sorted(missing)}"),
    )
    return bool(not missing), (
        "all_tags_reachable" if not missing
        else f"unreachable_tags:{sorted(missing)}"
    )


def _resolve_full_key(
    scope: str,
    key: str,
    actor_id: str,
    target_actor: str | None = None,
) -> tuple[str | None, str | None]:
    """Return (full_key, error).  Full key is None when input is invalid.

    ``target_actor`` selects another principal only for ``private:`` scope.
    The public ``MemoryGetTool`` permits that read only for an exact
    ``memory:<fact_id>`` after a matching catalog entry and tags, a direct
    family relation, and a confirmed common topic. Service slots remain
    owner-only. ``shared`` and ``pair`` are not readable through that tool.

    For internal key normalization, ``pair:`` accepts two forms:
      * ``pair:<other_id>`` — documented form, just the other principal.
      * ``pair:<a>_<b>`` — already-canonical form (sorted pair). LLMs
        frequently pass this back after seeing it in stored values, and
        previously the tool re-sorted ``[actor, "<a>_<b>"]`` producing a
        bogus ``pair:<a>_<b>_<actor>:<key>`` that always failed policy.
    ``MemoryGetTool`` rejects both forms before policy or storage access.
    """
    if not key:
        return None, "Error: 'key' is required"
    scope = (scope or "").strip()
    if scope == "shared":
        if target_actor and target_actor != actor_id:
            return None, "Error: 'actor' parameter is only valid for 'private' scope"
        return f"shared:{key}", None
    if scope == "private":
        owner = (target_actor or actor_id).strip()
        if not owner:
            return None, "Error: 'actor' must be a non-empty principal id"
        # This only constructs the key; it does not grant access. Cross-owner
        # reads require an exact memory:<fact_id>, catalog/tag agreement, a
        # direct family relation, and a common topic. Service slots are
        # owner-only.
        return f"private:{owner}:{key}", None
    if scope.startswith("pair:"):
        if target_actor and target_actor != actor_id:
            return None, "Error: 'actor' parameter is only valid for 'private' scope"
        raw = scope[len("pair:"):].strip()
        if not raw:
            return None, "Error: pair scope requires another principal id, e.g. 'pair:member_a'"
        if raw == actor_id:
            return None, "Error: pair scope must name a different principal"
        reg = get_registry()
        if reg.get(actor_id) is None:
            return None, f"Error: unknown current principal '{actor_id}' for pair scope"
        other: str | None = None
        if reg.get(raw) is not None:
            other = raw
        else:
            # Maybe already-canonical "pair:<a>_<b>" — find the matching peer.
            matches: list[str] = []
            for pid in reg.ids:
                if pid == actor_id:
                    continue
                if pair_namespace_token(actor_id, pid) == raw:
                    matches.append(pid)
            if len(matches) == 1:
                other = matches[0]
            elif len(matches) > 1:
                return None, f"Error: ambiguous pair scope: '{raw}'"
        if other is None:
            return None, (
                f"Error: unknown principal in pair scope: '{raw}'. "
                "Use 'pair:<other_id>'."
            )
        if not is_peer(actor_id, other):
            return None, (
                "Error: pair scope requires an allowed peer relationship "
                f"between '{actor_id}' and '{other}'"
            )
        pair_token = pair_namespace_token(actor_id, other)
        if pair_token in ambiguous_pair_namespaces(reg.ids):
            return None, (
                f"Error: ambiguous pair namespace 'pair:{pair_token}' is shared "
                "by multiple actor pairs"
            )
        return f"pair:{pair_token}:{key}", None
    return None, (
        f"Error: unknown scope '{scope}'. Use 'shared', 'private', or 'pair:<other_id>'."
    )


def _current_actor_and_key() -> tuple[str | None, str | None, str | None]:
    actor_id = get_current_actor()
    if not actor_id:
        return None, None, "Error: no actor in context — memory operations require a known principal"
    principal = get_registry().get(actor_id)
    if principal is None or not principal.memx_key:
        return None, None, (
            f"Error: principal '{actor_id}' has no memx_key configured — "
            "add it to principals.json"
        )
    return actor_id, principal.memx_key, None


_ACTOR_PARAM_DESC = (
    "Optional principal id whose namespace to read. Defaults to the "
    "current actor (own namespace) and is valid only for 'private'. "
    "For another owner, only an exact memory:<fact_id> with an exact "
    "trusted catalog entry can proceed to family-relation and common-topic "
    "authorization. Service, internal, migration, and secret records are "
    "owner-only; a denial is indistinguishable from a missing value."
)


def _trusted_read_value(response: Any) -> tuple[str, Any]:
    """Classify one memX response without exposing record existence."""
    try:
        status_code = response.status_code
    except Exception:  # noqa: BLE001
        return "denied", None
    if type(status_code) is not int:
        return "denied", None
    if status_code >= 500:
        return "unavailable", None
    if status_code != 200:
        return "denied", None
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return "denied", None
    try:
        if (
            not isinstance(payload, dict)
            or "value" not in payload
            or "ts" not in payload
            or not isinstance(payload["ts"], (int, float))
            or isinstance(payload["ts"], bool)
        ):
            return "denied", None
        value = payload["value"]
    except Exception:  # noqa: BLE001
        return "denied", None
    return "ok", value


@tool_parameters(
    tool_parameters_schema(
        scope=StringSchema(SCOPE_DESC),
        key=StringSchema(
            "Exact key inside the private namespace. Own reads may use the "
            "owner's valid keys. A cross-owner read requires a non-empty exact "
            "memory:<fact_id> whose full tag list matches the exact owner catalog."
        ),
        actor=StringSchema(_ACTOR_PARAM_DESC, nullable=True),
        required=["scope", "key"],
    )
)
class MemoryGetTool(Tool):
    """Read a scoped memory value via memX using the current actor's key."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url_override = base_url

    @property
    def name(self) -> str:
        return "memory_get"

    @property
    def description(self) -> str:
        return (
            "Read a value from family memory using only `private`; "
            "`shared` and `pair` are rejected before policy or storage "
            "access. Own private reads use the current owner.\n\n"
            "A cross-owner read accepts only an exact non-empty "
            "`memory:<fact_id>`. It first requires an exact owner catalog "
            "entry, then fetches the fact and requires its full tag list to "
            "match. The canonical decision then requires a direct family "
            "relation and an exact common topic. Owner-only service records, "
            "internal transaction history, migration state, and secret "
            "records are never returned cross-owner. A denial is "
            "indistinguishable from a missing value."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        scope: str,
        key: str,
        actor: str | None = None,
        **kwargs: Any,
    ) -> str:
        normalized_scope = (scope or "").strip()
        if normalized_scope != "private":
            return (
                "Error: memory_get accepts only 'private' scope; "
                "use scope='private'. 'shared' and 'pair' are not readable."
            )
        actor_id, api_key, err = _current_actor_and_key()
        if err:
            return err
        target_actor = (actor or "").strip() or None
        is_peer_read = (
            target_actor is not None
            and target_actor != actor_id
        )
        if is_peer_read:
            full_key = f"private:{target_actor}:{key}"
            result = f"(no value stored at '{full_key}')"
            audit_decision = "deny"
            audit_reason = _PEER_DENIED_REASON
            audit_tags: tuple[str, ...] = ()
            try:
                if (
                    is_valid_atomic_memory_key(key)
                    and get_registry().get(target_actor) is not None
                ):
                    (
                        result,
                        audit_decision,
                        audit_reason,
                        audit_tags,
                    ) = await self._read_peer_private(
                        actor_id=actor_id,
                        peer_id=target_actor,
                        full_key=full_key,
                        key=key,
                        api_key=api_key,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("peer-private read failed: {}", exc)
                result = _PEER_STORE_UNAVAILABLE
                audit_decision = "error"
                audit_reason = "store_unavailable"
                audit_tags = ()
            try:
                audit.log_event(
                    "peer_private_read",
                    actor=actor_id,
                    peer=target_actor,
                    key=full_key,
                    decision=audit_decision,
                    reason=audit_reason,
                    tags=list(audit_tags),
                )
            except Exception as exc:  # noqa: BLE001
                # Audit failure must not alter the already computed read result.
                logger.warning("peer-private audit logging failed: {}", exc)
            return result
        full_key, err = _resolve_full_key(
            normalized_scope, key, actor_id, target_actor=target_actor,
        )
        if err:
            return err
        if not full_key.startswith("pair:"):
            decision = get_engine().evaluate(
                PolicyContext(action="memory.read", actor=actor_id, to_chat=full_key)
            )
            if decision.decision is Decision.DENY:
                reason = decision.reason or "policy denied"
                return f"Policy denied memory.read на '{full_key}': {reason}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{self._base_url_override or memx_base_url()}/get",
                    headers={"x-api-key": api_key},
                    params={"key": full_key},
                )
        except httpx.HTTPError as exc:
            return f"Error: memX unreachable ({type(exc).__name__}: {exc})"
        if r.status_code == 403:
            return f"Error: access denied by memX ACL for key '{full_key}'"
        if r.status_code == 404:
            return f"(no value stored at '{full_key}')"
        if r.status_code >= 400:
            return f"Error: memX {r.status_code}: {r.text[:200]}"
        try:
            payload = r.json()
        except ValueError:
            return r.text
        # memX returns 200 + body `null` for "key never written" rather than
        # 404, and 200 + `{"value": null}` for "explicitly written null". Treat
        # both as no-value so the agent gets a friendly message instead of a
        # raw AttributeError ("NoneType has no get") leaking up as a tool
        # error. roles.fetch_admin_grants does the same kind of guarding.
        if payload is None:
            return f"(no value stored at '{full_key}')"
        if isinstance(payload, dict):
            value = payload.get("value", payload)
        else:
            value = payload
        if value is None:
            return f"(no value stored at '{full_key}')"
        if isinstance(value, (dict, list)):
            # Stored as a JSON object — couldn't have come through encode()
            # because our encode emits a string. Treat as legacy structured
            # value, no tag ACL.
            return json.dumps(value, ensure_ascii=False)
        # ``value`` is a string. Try wrapped → tag ACL; else legacy.
        wrapped = codec.decode(value) if isinstance(value, str) else None
        # Owner-of-namespace bypass: reading your own private record
        # never gates on tag-ACL. Arbitrary tags you wrote on your own
        # record (e.g. the ``secret`` opt-out tag) must not lock you
        # out of your own data. Peers go through the separate
        # _read_peer_private path, which has its own secret-tag check.
        own_private = full_key.startswith(f"private:{actor_id}:")
        if wrapped is not None and wrapped.tags and not own_private:
            allowed, reason = await _check_read_acl(
                actor_id, api_key, set(wrapped.tags), full_key,
            )
            if not allowed:
                # Fail-closed: do not leak even the existence shape.
                return f"(no value stored at '{full_key}')"
            return wrapped.value
        if wrapped is not None:
            # Wrapped (own private OR no tags) — return raw value.
            return wrapped.value
        return str(value)

    async def _read_peer_private(
        self,
        *,
        actor_id: str,
        peer_id: str,
        full_key: str,
        key: str,
        api_key: str,
    ) -> tuple[str, str, str, tuple[str, ...]]:
        """Fetch, decode and authorize one cross-principal private read."""
        no_value = f"(no value stored at '{full_key}')"
        denied = (no_value, "deny", _PEER_DENIED_REASON, ())
        unavailable = (
            _PEER_STORE_UNAVAILABLE,
            "error",
            "store_unavailable",
            (),
        )
        try:
            proxy_key = resolve_admin_key()
        except Exception as exc:  # noqa: BLE001
            logger.warning("peer-private: admin key unavailable: {}", exc)
            return unavailable
        if not isinstance(proxy_key, str) or not proxy_key:
            return unavailable
        base_url = self._base_url_override or memx_base_url()
        catalog_key = f"private:{peer_id}:value:private_index"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                catalog_response = await client.get(
                    f"{base_url}/get",
                    headers={"x-api-key": proxy_key},
                    params={"key": catalog_key},
                )
                catalog_state, catalog_value = _trusted_read_value(
                    catalog_response
                )
                if catalog_state == "unavailable":
                    return unavailable
                if catalog_state != "ok" or catalog_value is None:
                    return denied
                catalog = _decode_atomic_memory_catalog(catalog_value)
                if catalog is None:
                    return denied
                matching_tags = [
                    tags
                    for name, tags in catalog
                    if name == key
                ]
                if len(matching_tags) != 1:
                    return denied
                catalog_tags = matching_tags[0]
                r = await client.get(
                    f"{base_url}/get",
                    headers={"x-api-key": proxy_key},
                    params={"key": full_key},
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "peer-private storage transport failed: {}",
                type(exc).__name__,
            )
            return unavailable
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "peer-private storage request failed: {}",
                type(exc).__name__,
            )
            return unavailable
        fact_state, value = _trusted_read_value(r)
        if fact_state == "unavailable":
            return unavailable
        if fact_state != "ok":
            return denied
        if value is None:
            return denied
        try:
            wrapped = codec.decode(value) if isinstance(value, str) else None
            if wrapped is not None:
                record_tags = canonical_memory_tags(wrapped.tags)
                if record_tags is None or not isinstance(wrapped.value, str):
                    return denied
                effective_value = wrapped.value
            elif isinstance(value, str):
                record_tags = ()
                effective_value = value
            else:
                return denied
        except Exception:  # noqa: BLE001
            return denied
        if record_tags != catalog_tags:
            return denied
        family = await _fetch_raw_graph(
            api_key,
            "shared:family.graph",
            base_url,
            fail_on_unavailable=True,
        )
        topics = await _fetch_raw_graph(
            api_key,
            "shared:topics.graph",
            base_url,
            fail_on_unavailable=True,
        )
        try:
            decision = decide_memory_read(
                reader=actor_id,
                owner=peer_id,
                scope="private",
                key=key,
                tags=record_tags,
                family_graph=family,
                topics_graph=topics,
                static_policy=_NO_MATCHING_STATIC_POLICY,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("peer-private decision failed: {}", exc)
            return denied
        reason = (
            decision.reason
            if isinstance(decision.reason, str) and decision.reason
            else _PEER_DENIED_REASON
        )
        if decision.allowed is not True:
            return no_value, "deny", reason, ()
        return effective_value, "allow", reason, record_tags


@tool_parameters(
    tool_parameters_schema(
        fact_id=StringSchema("Stable identifier of one atomic fact"),
        value=StringSchema(
            "One atomic fact to store. Omit value to delete exact fact_id"
        ),
        topic=StringSchema(
            "Optional existing shared topic requested by the person",
            nullable=True,
        ),
        required=["fact_id"],
    )
    | {"additionalProperties": False}
)
class MemorySetTool(Tool):
    """Write one atomic fact to the current actor's private memory."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        ingestor: Any | None = None,
    ) -> None:
        self._base_url_override = base_url
        self._ingestor = ingestor

    @property
    def name(self) -> str:
        return "memory_set"

    @property
    def description(self) -> str:
        return (
            "Store one atomic fact for the current person. The physical "
            "destination is always that person's private memory. Use "
            "topic only when the person explicitly requests an existing "
            "shared topic; it is stored as a server-verified tag. Never "
            "invent a topic or choose another person's memory. Omit value "
            "to delete exact fact_id. Topic is ignored when deleting."
        )

    async def execute(
        self,
        fact_id: str,
        value: str | None = None,
        topic: str | None = None,
        **kwargs: Any,
    ) -> str:
        actor_id, api_key, err = _current_actor_and_key()
        if err:
            return err
        delete = value is None
        server_topic: str | None = None
        requested_topic = ""
        topic_state = "none"
        if not delete:
            value_bytes = value.encode("utf-8", errors="replace")
            if len(value_bytes) > _MAX_VALUE_BYTES:
                return (
                    f"Error: value too large ({len(value_bytes)} bytes); "
                    f"limit is {_MAX_VALUE_BYTES} bytes. Split it into atomic facts."
                )

            requested_topic = topic.strip() if isinstance(topic, str) else ""
            if requested_topic:
                topic_state = await _topic_write_state(
                    actor_id,
                    api_key,
                    requested_topic,
                    self._base_url_override or memx_base_url(),
                )
                if topic_state in {"shared", "isolated"}:
                    server_topic = requested_topic

        ingestor = self._ingestor
        if ingestor is None:
            from familia.principal_memory_ingestor import PrincipalMemoryIngestor

            ingestor = PrincipalMemoryIngestor(
                base_url=self._base_url_override or memx_base_url(),
                api_key=api_key,
                server_topic_validator=(
                    (lambda candidate: candidate == server_topic)
                    if server_topic is not None
                    else None
                ),
            )
        result = await ingestor.ingest(
            server_principal=actor_id,
            server_topic=server_topic,
            operation=(
                {"kind": "delete", "fact_id": fact_id}
                if delete
                else {
                    "kind": "memory",
                    "fact_id": fact_id,
                    "value": value,
                }
            ),
        )
        if delete:
            return result
        committed = isinstance(result, str) and result.startswith("committed:")
        if topic_state == "isolated":
            if not committed:
                return (
                    f"{result}; у топика '{requested_topic}' нет общих связей"
                )
            return (
                f"{result}; сохранено в личную память с тегом топика "
                f"'{requested_topic}', но у топика '{requested_topic}' "
                "нет общих связей"
            )
        if topic_state == "unavailable":
            if not committed:
                return (
                    f"{result}; топик '{requested_topic}' недоступен и не настроен"
                )
            return (
                f"{result}; топик '{requested_topic}' недоступен и не настроен; "
                "сохранено в личную память без топика"
            )
        return result


# Maximum number of entries we keep in each per-actor key-index.
# Prevents the index from ballooning unbounded if the LLM goes through a
# write-spree. Older entries get evicted FIFO when we cross the cap; the
# LLM can still re-discover them via grep / tag search if it really
# needs them.
_SHARED_INDEX_MAX_ENTRIES = 256
_PRIVATE_INDEX_MAX_ENTRIES = 256


# Reserved ``value:*`` keys that the system manages directly (auto-loaded
# into the system prompt or used as indexes themselves). Writes to these
# keys must NOT trigger index updates, both because they're not "custom
# keys the LLM should rediscover" AND because indexing the index would
# loop on every write.
_RESERVED_VALUE_KEYS = frozenset({
    "value:user_profile",
    "value:memory",
    "value:heartbeat",
    "value:shared_index",
    "value:private_index",
})


def _is_reserved_value_key(key: str) -> bool:
    return key.strip() in _RESERVED_VALUE_KEYS


async def _append_to_index(
    *,
    actor_id: str,
    api_key: str,
    base_url: str,
    index_suffix: str,
    written_key: str,
    tags: list[str] | None = None,
) -> None:
    """Legacy API intentionally disabled: index mutation must be atomic."""
    raise RuntimeError(
        "deprecated non-atomic index mutation; use memX set(index_update=...)"
    )
