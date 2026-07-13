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

from familia import audit
from familia.acl import codec, schema as acl_schema
from familia.acl.graph_io import resolve_admin_key
from familia.acl.peers import is_peer
from familia.acl.reachable import reachable_tag_ids
from familia.memx_client import memx_base_url
from familia.policy import Decision, PolicyContext, get_engine, is_explicit_deny
from familia.principals import (
    ambiguous_pair_namespaces,
    get_current_actor,
    get_registry,
    pair_namespace_token,
)
from familia.roles import get_effective_roles
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import ArraySchema, StringSchema, tool_parameters_schema
SCOPE_DESC = (
    "Memory scope: 'shared' (visible to the whole family) or "
    "'private' (default: only the current actor; readable by peer-edge "
    "principals when 'actor' parameter names a peer). "
    "Cross-principal access is gated by the family graph (is_peer) plus "
    "per-record tags. Records tagged 'secret' stay owner-only even with "
    "an active peer-edge."
)

# Opt-out tag: a record carrying this tag is readable only by its owner,
# regardless of peer-edges. Used for genuinely sensitive content the user
# does not want to share with peers despite the family-by-default model
# (gifts, therapy/health notes, work secrets).
SECRET_TAG = "secret"

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
# structural keys (graphs/roles). The canonical write paths are
# admin_grant/admin_revoke (for roles) and the `familia` CLI (for graphs).
_RESERVED_STRUCTURAL_PREFIXES = (
    "shared:roles.",
    "shared:family.graph",
    "shared:topics.graph",
)


def _is_reserved_structural_key(full_key: str) -> bool:
    return any(full_key.startswith(prefix)
               for prefix in _RESERVED_STRUCTURAL_PREFIXES)


# ---- tag-ACL helpers (Stage 5) ---------------------------------------------

# Both graphs are read with the actor's own memx_key — they're public-ish
# (every principal can read shared:family.graph and shared:topics.graph
# per current policy).
async def _fetch_graph(api_key: str, key: str, base_url: str) -> acl_schema.Graph:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{base_url}/get",
                headers={"x-api-key": api_key},
                params={"key": key},
            )
    except httpx.HTTPError as exc:
        logger.warning("memX graph {} unreachable: {}", key, exc)
        return acl_schema.Graph()
    if r.status_code == 404 or r.status_code == 403:
        return acl_schema.Graph()
    if r.status_code >= 400:
        logger.warning("memX graph {} {}: {}", key, r.status_code, r.text[:200])
        return acl_schema.Graph()
    try:
        payload = r.json()
    except ValueError:
        return acl_schema.Graph()
    if payload is None:
        return acl_schema.Graph()
    raw = payload.get("value", payload) if isinstance(payload, dict) else payload
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            # SR-10: fail-closed.
            logger.warning("memX graph {} value is malformed JSON", key)
            return acl_schema.Graph()
    return acl_schema.Graph.from_dict(raw if isinstance(raw, dict) else None)


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

    ``target_actor`` allows cross-principal reads of ``private:`` scope:
    when set and different from ``actor_id``, the key resolves to
    ``private:<target_actor>:<key>``. Permission to actually read is
    enforced downstream (is_peer check + secret-tag filter in
    MemoryGetTool.execute). ``target_actor`` is rejected for non-private
    scopes — ``shared:`` is global, ``pair:`` already names the other
    principal via scope syntax.

    For ``pair:`` scope we accept two forms:
      * ``pair:<other_id>`` — documented form, just the other principal.
      * ``pair:<a>_<b>`` — already-canonical form (sorted pair). LLMs
        frequently pass this back after seeing it in stored values, and
        previously the tool re-sorted ``[actor, "<a>_<b>"]`` producing a
        bogus ``pair:<a>_<b>_<actor>:<key>`` that always failed policy.
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
        # All private:<owner>:<key> reads flow through the same gate
        # (is_peer + secret-tag). Reserved value:* slots (user_profile,
        # memory, heartbeat, *_index) are no longer special-cased — a
        # peer can read them by default; the owner narrows specific
        # records back to themselves with the ``secret`` tag.
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


_TAGS_DESC = (
    "Optional list of tag-ids to attach to this record. Tags must be ids "
    "from the family graphs (principals or topics) that the current actor "
    "has access to. The record is then visible to anyone whose reachable "
    "tag-set intersects with these. Used for cross-cutting access (e.g. "
    "[varya, school] makes a record visible to anyone connected to "
    "principal varya OR the school topic). Omit for legacy scope-only ACL."
)


_ACTOR_PARAM_DESC = (
    "Optional principal id whose namespace to read. Defaults to the "
    "current actor (own namespace). Only valid for 'private' scope. "
    "When the named principal is a peer in the family graph "
    "(spouse_of / guardian_of edge, role:child excluded), reads any "
    "of their private records — custom keys AND reserved value:* "
    "slots (value:memory, value:user_profile, value:heartbeat, "
    "*_index). Records tagged 'secret' by the owner are never "
    "returned cross-actor and yield 'no value stored'."
)


@tool_parameters(
    tool_parameters_schema(
        scope=StringSchema(SCOPE_DESC),
        key=StringSchema("Bare memory key (no scope prefix; e.g. 'todo', 'grocery_list')"),
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
            "Read a value from scoped family memory (memX).\n\n"
            "Scopes:\n"
            "  • 'private' — by default the current actor's own "
            "namespace. With the optional 'actor' parameter set to a "
            "peer principal's id, reads ANY of that peer's private "
            "records — custom keys AND reserved value:* slots "
            "(value:memory, value:user_profile, value:heartbeat). "
            "Records the owner tagged 'secret' are filtered "
            "(returned as 'no value stored').\n"
            "  • 'shared' — keys visible to every family member.\n\n"
            "Family-by-default: every `private:` record is peer-"
            "readable unless tagged 'secret'. When a user asks about a "
            "peer's plans, schedule, notes, profile — TRY "
            "memory_get(scope='private', actor='<peer_id>', "
            "key='value:memory') first (the peer's running scratchpad "
            "where most coordination data lives) before reporting "
            "'nothing found'.\n\n"
            "Your own custom keys appear in the 'Private/Shared keys "
            "you've written' system-prompt blocks; peers' custom keys "
            "appear in the cross-principal index blocks. Reserved "
            "slots are not indexed — read them by their fixed names."
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
        actor_id, api_key, err = _current_actor_and_key()
        if err:
            return err
        target_actor = (actor or "").strip() or None
        full_key, err = _resolve_full_key(
            scope, key, actor_id, target_actor=target_actor,
        )
        if err:
            return err
        is_peer_read = (
            scope == "private"
            and target_actor is not None
            and target_actor != actor_id
        )
        if is_peer_read:
            # Peer read: gate at tool layer via is_peer (graph-based,
            # excludes child role). No policy lookup — policy.yaml only
            # carries scope-level rules, the cross-actor decision is
            # graph-driven by design.
            if not is_peer(actor_id, target_actor):
                try:
                    audit.log_event(
                        "peer_private_read", actor=actor_id,
                        peer=target_actor, key=full_key,
                        decision="deny", reason="not_peer",
                    )
                except Exception:  # noqa: BLE001
                    pass
                # Fail-closed: don't leak whether the principal exists
                # or whether they have such a key.
                return f"(no value stored at '{full_key}')"
            return await self._read_peer_private(
                actor_id=actor_id,
                peer_id=target_actor,
                full_key=full_key,
            )
        # Pair access has already passed the exact registry + graph gate in
        # _resolve_full_key. Evaluate policy for audit and explicit operator
        # denies; tolerate only the unmatched fallback from older deployed
        # policies that predate a pair:* coarse-allow rule.
        decision = get_engine().evaluate(
            PolicyContext(action="memory.read", actor=actor_id, to_chat=full_key)
        )
        if decision.decision is Decision.DENY and (
            not full_key.startswith("pair:") or is_explicit_deny(decision)
        ):
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
    ) -> str:
        """Fetch ``private:<peer_id>:<key>`` through the admin proxy key.

        Caller has already passed the is_peer gate. We use the
        admin/proxy key (``resolve_admin_key``) instead of the caller's
        per-actor narrow key, because per-principal memX
        keys only grant ``private:<self>:*`` — they cannot reach a
        peer's namespace directly. The proxy path keeps acl.json
        minimal and lets the family graph be the single source of
        truth for peer permissions.

        Defense-in-depth: even with the proxy key, records tagged
        ``SECRET_TAG`` are filtered here, fail-closed (no value, no hint
        about existence).
        """
        try:
            proxy_key = resolve_admin_key()
        except Exception as exc:  # noqa: BLE001
            logger.warning("peer-private: admin key unavailable: {}", exc)
            try:
                audit.log_event(
                    "peer_private_read", actor=actor_id, peer=peer_id,
                    key=full_key, decision="deny", reason="no_admin_key",
                )
            except Exception:  # noqa: BLE001
                pass
            return f"Error: peer-read backend unavailable for '{full_key}'"
        base_url = self._base_url_override or memx_base_url()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{base_url}/get",
                    headers={"x-api-key": proxy_key},
                    params={"key": full_key},
                )
        except httpx.HTTPError as exc:
            return f"Error: memX unreachable ({type(exc).__name__}: {exc})"
        if r.status_code == 404:
            try:
                audit.log_event(
                    "peer_private_read", actor=actor_id, peer=peer_id,
                    key=full_key, decision="not_found",
                )
            except Exception:  # noqa: BLE001
                pass
            return f"(no value stored at '{full_key}')"
        if r.status_code >= 400:
            return f"Error: memX {r.status_code}: {r.text[:200]}"
        try:
            payload = r.json()
        except ValueError:
            return r.text
        if payload is None:
            return f"(no value stored at '{full_key}')"
        if isinstance(payload, dict):
            value = payload.get("value", payload)
        else:
            value = payload
        if value is None:
            return f"(no value stored at '{full_key}')"
        wrapped = codec.decode(value) if isinstance(value, str) else None
        record_tags: set[str] = set()
        if wrapped is not None:
            record_tags = set(wrapped.tags or [])
            effective_value: Any = wrapped.value
        elif isinstance(value, (dict, list)):
            effective_value = json.dumps(value, ensure_ascii=False)
        else:
            effective_value = str(value)
        if SECRET_TAG in record_tags:
            try:
                audit.log_event(
                    "peer_private_read", actor=actor_id, peer=peer_id,
                    key=full_key, decision="deny", reason="secret_tag",
                )
            except Exception:  # noqa: BLE001
                pass
            # Fail-closed: indistinguishable from "no such key" — don't
            # leak even the existence of a secret-tagged record.
            return f"(no value stored at '{full_key}')"
        try:
            audit.log_event(
                "peer_private_read", actor=actor_id, peer=peer_id,
                key=full_key, decision="allow",
                tags=sorted(record_tags) if record_tags else [],
            )
        except Exception:  # noqa: BLE001
            pass
        return effective_value


@tool_parameters(
    tool_parameters_schema(
        scope=StringSchema(SCOPE_DESC),
        key=StringSchema("Bare memory key (no scope prefix)"),
        value=StringSchema("Value to store (use JSON-encoded string for structured data)"),
        tags=ArraySchema(StringSchema(""), description=_TAGS_DESC, nullable=True),
        required=["scope", "key", "value"],
    )
)
class MemorySetTool(Tool):
    """Write a scoped memory value via memX using the current actor's key."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url_override = base_url

    @property
    def name(self) -> str:
        return "memory_set"

    @property
    def description(self) -> str:
        return (
            "Write a value to scoped family memory (memX).\n\n"
            "Family-by-default: ``private:`` records without the "
            "``secret`` tag are readable by every peer-edge principal "
            "(spouse / guardian). To keep a record owner-only — gifts, "
            "therapy/health notes, work secrets — include ``secret`` in "
            "the ``tags`` list. The owner always sees their own records "
            "regardless of tag.\n\n"
            "WHERE TO WRITE — pick the scope deliberately:\n"
            "  • Personal facts about the current user (preferences, "
            "ongoing context, profile bits, anything that follows them "
            "between channels) → ALWAYS scope='private'.\n"
            "    - Profile-style summary (one canonical doc) → "
            "key='value:user_profile'.\n"
            "    - Running notes / scratchpad → key='value:memory'.\n"
            "    These two keys are auto-loaded into every prompt — "
            "write there and you'll see the data on the next turn "
            "regardless of channel.\n"
            "    Custom private keys also work (anything under "
            "'private:<actor>:*'); they get indexed under "
            "'private:<actor>:value:private_index' and surface in your "
            "system prompt as 'Private keys you've written' next turn, "
            "so you can rediscover them by name. Prefer "
            "'value:user_profile'/'value:memory' for the canonical "
            "data — custom keys are for genuinely separate categories.\n"
            "  • Family-wide facts that everyone should see (shared "
            "calendar, household rule) → scope='shared'. Every custom "
            "shared key you write gets indexed under "
            "'private:<actor>:value:shared_index' and surfaces in your "
            "prompt as 'Shared keys you've written'.\n"
            "  • To share a fact with one specific other principal "
            "(spouse, parent↔child, etc.), write it to scope='shared' "
            "and put their id in the 'tags' field. Peer-edge ACL + "
            "tag-ACL combine to make sure only that principal can read "
            "it — there is NO separate scope for two-person sharing.\n\n"
            "DO NOT stash personal facts about a single principal under "
            "custom shared keys without tags — the auto-prompt won't "
            "pick them up and you will look amnesiac on the next "
            "channel switch.\n\n"
            "Last-write-wins; no TTL/history at this layer."
        )

    async def execute(
        self, scope: str, key: str, value: str,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        actor_id, api_key, err = _current_actor_and_key()
        if err:
            return err
        full_key, err = _resolve_full_key(scope, key, actor_id)
        if err:
            return err
        # SR-14: belt-and-suspenders. Even if someone (mis)edits policy.yaml
        # to allow these keys, the tool itself refuses — graphs/roles edits
        # go through the `familia` CLI only.
        if _is_reserved_structural_key(full_key):
            return (
                f"Error: '{full_key}' is a structural key (graphs/roles) and "
                "cannot be written from chat. Use the `familia` CLI on the VM."
            )
        # Normalize tags early so size checks see the on-disk bytes.
        tag_set: set[str] = set()
        if tags:
            for t in tags:
                if isinstance(t, str) and t.strip():
                    tag_set.add(t.strip())
        if tag_set:
            ok, reason = await _check_write_acl(
                actor_id, api_key, tag_set, full_key,
            )
            if not ok:
                return (
                    f"Error: cannot tag with {sorted(tag_set)} — {reason}. "
                    "You can only tag records with ids in your reachable set."
                )
            stored_value = codec.encode(value, sorted(tag_set))
        else:
            stored_value = value
        value_bytes = (stored_value or "").encode("utf-8", errors="replace")
        if len(value_bytes) > _MAX_VALUE_BYTES:
            return (
                f"Error: value too large ({len(value_bytes)} bytes); "
                f"limit is {_MAX_VALUE_BYTES} bytes. Split into multiple "
                "keys or summarize."
            )
        index_update: dict[str, Any] | None = None
        if not _is_reserved_value_key(key):
            index_suffix: str | None = None
            index_limit = _PRIVATE_INDEX_MAX_ENTRIES
            if scope == "shared":
                index_suffix = "value:shared_index"
                index_limit = _SHARED_INDEX_MAX_ENTRIES
            elif scope == "private":
                index_suffix = "value:private_index"
            if index_suffix is not None:
                index_update = {
                    "key": f"private:{actor_id}:{index_suffix}",
                    "entry": {
                        "name": key,
                        "tags": sorted(tag_set) if tag_set else [],
                    },
                    "max_entries": index_limit,
                }
        # _resolve_full_key already applied the exact registry + peer-edge
        # gate for pair routes. Evaluate policy for audit and explicit denies;
        # tolerate only an unmatched fallback from a legacy policy.
        decision = get_engine().evaluate(
            PolicyContext(action="memory.write", actor=actor_id, to_chat=full_key)
        )
        if decision.decision is Decision.DENY and (
            not full_key.startswith("pair:") or is_explicit_deny(decision)
        ):
            reason = decision.reason or "policy denied"
            return f"Policy denied memory.write на '{full_key}': {reason}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    f"{self._base_url_override or memx_base_url()}/set",
                    headers={"x-api-key": api_key},
                    json={
                        "key": full_key,
                        "value": stored_value,
                        "index_update": index_update,
                    },
                )
        except httpx.HTTPError as exc:
            return f"Error: memX unreachable ({type(exc).__name__}: {exc})"
        if r.status_code == 403:
            return "denied_invalid: memX ACL rejected the write"
        if r.status_code >= 400:
            return f"error: memX transport status {r.status_code}: {r.text[:200]}"
        try:
            payload = r.json()
        except (TypeError, ValueError):
            return "error: memX returned a non-JSON semantic result"
        if not isinstance(payload, dict):
            return "error: memX returned a non-object semantic result"
        status = payload.get("status")
        if (
            status == "committed"
            and payload.get("committed") is True
            and payload.get("updated") is True
            and payload.get("retryable") is False
        ):
            pass
        elif status == "denied_invalid":
            return "denied_invalid: memX rejected the mutation"
        elif payload.get("retryable") is True or status == "not_updated":
            return f"retryable_failure: memX semantic state '{status or 'unknown'}'"
        else:
            return f"error: memX returned inconsistent semantic state '{status}'"
        if tag_set:
            tag_str = ", ".join(sorted(tag_set))
            return f"committed: Stored at '{full_key}' (теги: {tag_str})"
        return f"committed: Stored at '{full_key}'"


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
