"""Per-principal memory client used by ContextBuilder and CLI.

Wraps :mod:`familia.acl.graph_io` ``get_raw``/``set_raw`` with a thin
namespacing layer. The client is **per-actor**: it knows the
principal's id and api_key, and prepends ``private:<id>:`` to every
relative suffix (``value:user_profile``, ``value:memory`` etc).

Two flavours of access:

* :py:meth:`get` / :py:meth:`set` — own data, namespace is fixed to
  ``private:<self.principal_id>:``.

* :py:meth:`get_other` — read a peer's atomic fact only after an exact
  trusted-catalog match and the canonical memory decision. Never used
  to write — cross-principal writes from chat are policy-denied through
  the regular memory tools.

This is the single point that ContextBuilder uses to assemble per-turn
prompt content. Standalone nanobot (no ``principals.json``) never
constructs this client; the legacy file-based USER/MEMORY remains as
a fallback path in ContextBuilder.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from familia.acl import codec, graph_io
from familia.acl.graph_io import GraphIOError, get_raw, set_raw
from familia.policy import get_engine


_FAMILY_RELATIONS = frozenset(
    {
        "spouse_of",
        "parent_of",
        "owner_of",
        "caregiver_of",
        "guardian_of",
    }
)
_ORDINARY_VALUE_KEYS = frozenset(
    {
        "value:user_profile",
        "value:memory",
        "value:heartbeat",
        "value:private_index",
        "value:shared_index",
    }
)
_FACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PRINCIPAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NO_MATCHING_STATIC_POLICY = "no_matching_rule"
_ATOMIC_MEMORY_CATALOG_MAX_ENTRIES = 256


@dataclass(frozen=True, slots=True)
class MemoryReadDecision:
    """Stable result of the pure memory-read access decision."""

    allowed: bool
    reason: str


def is_valid_atomic_memory_key(key: object) -> bool:
    """Return whether key has the ingestor's exact atomic-fact shape."""
    if not isinstance(key, str) or not key.startswith("memory:"):
        return False
    return _FACT_ID_RE.fullmatch(key.removeprefix("memory:")) is not None


def canonical_memory_tags(tags: object) -> tuple[str, ...] | None:
    """Validate server tags and return one deterministic set representation."""
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Sequence):
        return None
    canonical: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if (
            not isinstance(tag, str)
            or not tag
            or any(character.isspace() for character in tag)
            or tag in seen
        ):
            return None
        seen.add(tag)
        canonical.append(tag)
    return tuple(sorted(canonical))


def _decode_atomic_memory_catalog(
    raw: Any,
) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """Decode one fail-closed catalog without accepting legacy entries."""
    text = _coerce_to_str(raw)
    if text is None:
        return ()
    try:
        wrapped = codec.decode(text)
        encoded = wrapped.value if wrapped is not None else text
        if not isinstance(encoded, str):
            return None
        decoded = json.loads(encoded)
    except Exception:  # noqa: BLE001
        return None
    if (
        not isinstance(decoded, list)
        or len(decoded) > _ATOMIC_MEMORY_CATALOG_MAX_ENTRIES
    ):
        return None

    entries: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for entry in decoded:
        if not isinstance(entry, dict) or set(entry) != {"name", "tags"}:
            return None
        name = entry["name"]
        tags = canonical_memory_tags(entry["tags"])
        if (
            not is_valid_atomic_memory_key(name)
            or tags is None
            or name in seen
        ):
            return None
        seen.add(name)
        entries.append((name, tags))
    return tuple(entries)


def decide_memory_read(
    *,
    reader: str,
    owner: str,
    scope: str,
    key: str,
    tags: Sequence[str],
    family_graph: Mapping[str, Any],
    topics_graph: Mapping[str, Any],
    static_policy: str,
) -> MemoryReadDecision:
    """Decide read access without I/O or mutation of the supplied graphs."""
    if not all(
        isinstance(value, str) and value
        for value in (reader, owner, scope, key, static_policy)
    ):
        return MemoryReadDecision(False, "invalid_record")
    if not _PRINCIPAL_ID_RE.fullmatch(reader) or not _PRINCIPAL_ID_RE.fullmatch(owner):
        return MemoryReadDecision(False, "invalid_record")
    record_tags = canonical_memory_tags(tags)
    if record_tags is None:
        return MemoryReadDecision(False, "invalid_record")

    key_kind = _classify_memory_key(key)
    if key_kind is None:
        return MemoryReadDecision(False, "invalid_key")

    graph_state = _validated_graph_state(family_graph, topics_graph)
    if graph_state is None:
        return MemoryReadDecision(False, "invalid_graph")
    family_edges, topic_readers, principal_ids = graph_state
    if reader not in principal_ids or owner not in principal_ids:
        return MemoryReadDecision(False, "invalid_record")

    topic_circles = [topic_readers[tag] for tag in record_tags if tag in topic_readers]
    linked_reader_circles = [
        frozenset(
            principal_id
            for principal_id in circle
            if principal_id != owner
            and _has_family_relation(family_edges, principal_id, owner)
        )
        for circle in topic_circles
    ]
    if len(linked_reader_circles) > 1 and any(
        circle != linked_reader_circles[0]
        for circle in linked_reader_circles[1:]
    ):
        return MemoryReadDecision(False, "invalid_record")

    pair_members: tuple[str, str] | None = None
    if scope.startswith("pair:"):
        pair_members = _decode_pair_scope(scope)
        if pair_members is None or owner not in pair_members:
            return MemoryReadDecision(False, "invalid_pair_scope")
    elif scope not in {"private", "shared"}:
        return MemoryReadDecision(False, "invalid_scope")

    if reader == owner:
        return MemoryReadDecision(True, "owner_self")
    if key_kind == "history":
        return MemoryReadDecision(False, "internal_transaction_candidate")
    if key_kind in {"service", "pending_migration"}:
        return MemoryReadDecision(False, "owner_only_service_key")
    if "secret" in record_tags:
        return MemoryReadDecision(False, "owner_only_secret")
    if pair_members is not None:
        reason = "pair_scope_not_readable"
        if reader not in pair_members:
            reason = "pair_non_member"
        return MemoryReadDecision(False, reason)
    if scope == "shared":
        return MemoryReadDecision(False, "shared_scope_not_readable")

    related = _has_family_relation(family_edges, reader, owner)
    common_topic = any(
        reader in circle and owner in circle for circle in topic_circles
    )
    if related and common_topic:
        return MemoryReadDecision(True, "family_common_topic")
    if related:
        return MemoryReadDecision(False, "no_common_topic")
    if common_topic:
        return MemoryReadDecision(False, "topic_without_family_relation")
    return MemoryReadDecision(False, "no_family_relation")


def _classify_memory_key(key: str) -> str | None:
    if key in _ORDINARY_VALUE_KEYS:
        return "service"
    if is_valid_atomic_memory_key(key):
        return "memory"
    if key.startswith("history:") and key.removeprefix("history:"):
        return "history"
    if key.startswith("pending_migration:") and key.removeprefix(
        "pending_migration:"
    ):
        return "pending_migration"
    return None


def _graph_edges(graph: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...] | None:
    if not isinstance(graph, Mapping):
        return None
    edges = graph.get("edges")
    if not isinstance(edges, list) or any(not isinstance(edge, Mapping) for edge in edges):
        return None
    normalized: list[Mapping[str, Any]] = []
    for edge in edges:
        source = edge.get("from") or edge.get("src") or ""
        destination = edge.get("to") or edge.get("dst") or ""
        if not source or not destination:
            return None
        normalized.append(
            {
                "from": str(source),
                "to": str(destination),
                "rel": str(edge.get("rel", "")),
            }
        )
    return tuple(normalized)


def _typed_node_ids(
    graph: Mapping[str, Any],
    expected_type: str,
) -> frozenset[str] | None:
    if not isinstance(graph, Mapping):
        return None
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or any(not isinstance(node, Mapping) for node in nodes):
        return None
    identifiers: list[str] = []
    all_identifiers: list[str] = []
    for node in nodes:
        identifier = node.get("id")
        node_type = node.get("type")
        if not isinstance(identifier, str) or not identifier:
            return None
        if not isinstance(node_type, str) or not node_type:
            return None
        all_identifiers.append(identifier)
        if node_type == expected_type:
            identifiers.append(identifier)
    if len(set(all_identifiers)) != len(all_identifiers):
        return None
    return frozenset(identifiers)


def _validated_graph_state(
    family_graph: Mapping[str, Any],
    topics_graph: Mapping[str, Any],
) -> tuple[
    tuple[Mapping[str, Any], ...],
    dict[str, frozenset[str]],
    frozenset[str],
] | None:
    principal_ids = _typed_node_ids(family_graph, "principal")
    topic_ids = _typed_node_ids(topics_graph, "topic")
    family_edges = _graph_edges(family_graph)
    topic_edges = _graph_edges(topics_graph)
    if (
        principal_ids is None
        or topic_ids is None
        or family_edges is None
        or topic_edges is None
    ):
        return None

    for edge in family_edges:
        if edge.get("rel") not in _FAMILY_RELATIONS:
            continue
        if (
            edge.get("from") not in principal_ids
            or edge.get("to") not in principal_ids
        ):
            return None

    readers: dict[str, set[str]] = {topic_id: set() for topic_id in topic_ids}
    for edge in topic_edges:
        if edge.get("rel") != "concerns":
            continue
        topic_id = edge.get("from")
        principal_id = edge.get("to")
        if topic_id not in topic_ids or principal_id not in principal_ids:
            return None
        readers[topic_id].add(principal_id)
    return (
        family_edges,
        {
            topic_id: frozenset(principals)
            for topic_id, principals in readers.items()
        },
        principal_ids,
    )


def _has_family_relation(
    edges: Sequence[Mapping[str, Any]],
    reader: str,
    owner: str,
) -> bool:
    return any(
        edge.get("rel") in _FAMILY_RELATIONS
        and (
            (edge.get("from") == reader and edge.get("to") == owner)
            or (edge.get("from") == owner and edge.get("to") == reader)
        )
        for edge in edges
    )


def _decode_pair_scope(scope: str) -> tuple[str, str] | None:
    prefix = b"pair:pair-v1/"
    raw = scope.encode("utf-8")
    if not raw.startswith(prefix):
        return None
    position = len(prefix)
    members: list[str] = []
    for index in range(2):
        colon = raw.find(b":", position)
        if colon == -1:
            return None
        length_bytes = raw[position:colon]
        if (
            not length_bytes
            or not length_bytes.isdigit()
            or (len(length_bytes) > 1 and length_bytes.startswith(b"0"))
        ):
            return None
        length = int(length_bytes)
        if length <= 0:
            return None
        start = colon + 1
        end = start + length
        if end > len(raw):
            return None
        try:
            member = raw[start:end].decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not _PRINCIPAL_ID_RE.fullmatch(member):
            return None
        members.append(member)
        position = end
        if index == 0:
            if position >= len(raw) or raw[position : position + 1] != b"/":
                return None
            position += 1
    if position != len(raw):
        return None
    if members[0].encode("utf-8") >= members[1].encode("utf-8"):
        return None
    return members[0], members[1]


class PrincipalMemoryClient:
    """memX gateway scoped to one principal's namespace."""

    def __init__(self, principal_id: str, api_key: str) -> None:
        if not principal_id:
            raise ValueError("principal_id is required")
        if not api_key:
            raise ValueError("api_key is required")
        self.principal_id = principal_id
        self._api_key = api_key

    def _own_key(self, suffix: str) -> str:
        return f"private:{self.principal_id}:{suffix}"

    def _other_key(self, other_id: str, suffix: str) -> str:
        return f"private:{other_id}:{suffix}"

    def get(self, suffix: str) -> str | None:
        """Read own ``private:<self.principal_id>:<suffix>`` value.

        Returns the raw string body, or ``None`` if the key is missing
        or memX is unreachable. Never raises — failure equals ``None``,
        ContextBuilder degrades gracefully.
        """
        try:
            raw = get_raw(self._own_key(suffix), api_key=self._api_key)
        except GraphIOError as exc:
            logger.warning("principal_memory.get({}): {}", suffix, exc)
            return None
        return _coerce_to_str(raw)

    def set(self, suffix: str, value: str) -> None:
        """Write own ``private:<self.principal_id>:<suffix>`` value.

        Raises :class:`GraphIOError` on memX failure — callers
        (CLI/admin) want to know.
        """
        set_raw(self._own_key(suffix), value, api_key=self._api_key)

    def _load_graph_snapshot(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Load one raw graph pair for a single higher-level operation."""
        try:
            family_graph = graph_io.load_graph_value(
                "shared:family.graph",
                api_key=self._api_key,
            )
            topics_graph = graph_io.load_graph_value(
                "shared:topics.graph",
                api_key=self._api_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("principal_memory graph snapshot failed: {}", exc)
            return None
        return family_graph, topics_graph

    def _load_other_memory_catalog(
        self,
        other_id: str,
    ) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
        """Read one foreign private catalog with the administrative key."""
        try:
            proxy_key = graph_io.resolve_admin_key()
            if not isinstance(proxy_key, str) or not proxy_key:
                return None
            raw = get_raw(
                self._other_key(other_id, "value:private_index"),
                api_key=proxy_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "principal_memory catalog read failed for {}: {}",
                other_id,
                exc,
            )
            return None
        return _decode_atomic_memory_catalog(raw)

    def _read_other_memory_entry(
        self,
        other_id: str,
        suffix: str,
        catalog_tags: tuple[str, ...],
        *,
        graphs: tuple[dict[str, Any], dict[str, Any]],
    ) -> str | None:
        """Read one fact whose exact catalog entry is already validated."""
        if other_id == self.principal_id or not is_valid_atomic_memory_key(suffix):
            return None
        full_key = self._other_key(other_id, suffix)
        try:
            proxy_key = graph_io.resolve_admin_key()
            if not isinstance(proxy_key, str) or not proxy_key:
                return None
            raw = get_raw(full_key, api_key=proxy_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("principal_memory.get_other({}): {}", other_id, exc)
            return None
        text = _coerce_to_str(raw)
        if text is None:
            return None
        try:
            family_graph, topics_graph = graphs
            wrapped = codec.decode(text)
            if wrapped is not None:
                tags = canonical_memory_tags(wrapped.tags)
                if tags is None or not isinstance(wrapped.value, str):
                    return None
                value = wrapped.value
            else:
                tags = ()
                value = text
            if tags != catalog_tags:
                return None
            decision = decide_memory_read(
                reader=self.principal_id,
                owner=other_id,
                scope="private",
                key=suffix,
                tags=tags,
                family_graph=family_graph,
                topics_graph=topics_graph,
                static_policy=_NO_MATCHING_STATIC_POLICY,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "principal_memory.get_other({}) invalid snapshot: {}",
                other_id,
                exc,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "principal_memory.get_other({}) decision failed: {}",
                other_id,
                exc,
            )
            return None
        return value if decision.allowed is True else None

    def project_other_memory_names(
        self,
        other_id: str,
        *,
        graphs: tuple[dict[str, Any], dict[str, Any]] | None = None,
        limit: int = 40,
    ) -> tuple[str, ...]:
        """Return newest authorized foreign atomic names, never their values."""
        if (
            other_id == self.principal_id
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            return ()
        limit = min(limit, 40)
        if graphs is None:
            graphs = self._load_graph_snapshot()
        if graphs is None:
            return ()
        try:
            family_graph, topics_graph = graphs
        except Exception:  # noqa: BLE001
            return ()
        catalog = self._load_other_memory_catalog(other_id)
        if catalog is None:
            return ()

        allowed_names: list[str] = []
        for name, tags in reversed(catalog):
            if not tags:
                continue
            try:
                preliminary = decide_memory_read(
                    reader=self.principal_id,
                    owner=other_id,
                    scope="private",
                    key=name,
                    tags=tags,
                    family_graph=family_graph,
                    topics_graph=topics_graph,
                    static_policy=_NO_MATCHING_STATIC_POLICY,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "principal_memory catalog projection failed for {}: {}",
                    other_id,
                    exc,
                )
                return ()
            if preliminary.allowed is not True:
                continue
            value = self._read_other_memory_entry(
                other_id,
                name,
                tags,
                graphs=graphs,
            )
            if value is None:
                continue
            allowed_names.append(name)
            if len(allowed_names) == limit:
                break
        return tuple(allowed_names)

    def get_other(
        self,
        other_id: str,
        suffix: str,
        *,
        graphs: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> str | None:
        """Read another principal through the canonical memory-read decision."""
        if other_id == self.principal_id:
            return self.get(suffix)
        if not is_valid_atomic_memory_key(suffix):
            return None
        catalog = self._load_other_memory_catalog(other_id)
        if catalog is None:
            return None
        matching_tags = [
            tags
            for name, tags in catalog
            if name == suffix
        ]
        if len(matching_tags) != 1:
            return None
        if graphs is None:
            graphs = self._load_graph_snapshot()
        if graphs is None:
            return None
        return self._read_other_memory_entry(
            other_id,
            suffix,
            matching_tags[0],
            graphs=graphs,
        )


def _coerce_to_str(raw: Any) -> str | None:
    """memX may return None / str / dict (legacy). Normalise to str.

    None → None. dict/list → JSON re-encoded (legacy structured
    values; we store text these days). Everything else → str().
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(raw)
