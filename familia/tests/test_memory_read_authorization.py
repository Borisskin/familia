"""Direct domain tests for the canonical memory-read decision."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

import pytest

from familia.acl import principal_memory
from memory_contract_samples import (
    EXPECTED_MEMORY_READ_DECISIONS,
    family_graph_sample,
    topics_graph_sample,
)


DecisionFunction = Callable[..., Any]


def _decision_function() -> DecisionFunction:
    decide = getattr(principal_memory, "decide_memory_read", None)
    assert callable(decide), (
        "familia.acl.principal_memory.decide_memory_read must be the single "
        "pure memory-read decision function"
    )
    return decide


def _decide(row: dict[str, Any]) -> Any:
    family_graph = family_graph_sample()
    topics_graph = topics_graph_sample()
    original_family_graph = deepcopy(family_graph)
    original_topics_graph = deepcopy(topics_graph)

    decision = _decision_function()(
        reader=row["reader"],
        owner=row["owner"],
        scope=row["scope"],
        key=row["key"],
        tags=row["tags"],
        family_graph=family_graph,
        topics_graph=topics_graph,
        static_policy=row["static_policy"],
    )

    assert family_graph == original_family_graph
    assert topics_graph == original_topics_graph
    return decision


@pytest.mark.parametrize(
    "row",
    EXPECTED_MEMORY_READ_DECISIONS,
    ids=lambda row: row["case"],
)
def test_decide_memory_read_follows_canonical_matrix(row: dict[str, Any]) -> None:
    decision = _decide(row)

    assert decision.allowed is row["allowed"]
    assert decision.reason == row["reason"]
    assert {
        surface: decision.allowed
        for surface in ("history", "index", "memory_get")
    } == row["visibility"]


@pytest.mark.parametrize(
    "row",
    EXPECTED_MEMORY_READ_DECISIONS,
    ids=lambda row: row["case"],
)
def test_decide_memory_read_reason_is_stable(row: dict[str, Any]) -> None:
    first = _decide(row)
    second = _decide(row)

    assert (first.allowed, first.reason) == (second.allowed, second.reason)
    assert first.reason == row["reason"]


def _assert_direct_decision(
    expected_allowed: bool,
    expected_reason: str,
    *,
    reader: str = "principal_alpha",
    owner: str = "principal_beta",
    scope: str = "private",
    key: str = "memory:fact_direct",
    tags: list[str] | None = None,
    family_graph: dict[str, Any] | None = None,
    topics_graph: dict[str, Any] | None = None,
    static_policy: str = "no_matching_rule",
) -> None:
    family = deepcopy(family_graph or family_graph_sample())
    topics = deepcopy(topics_graph or topics_graph_sample())
    original_family = deepcopy(family)
    original_topics = deepcopy(topics)
    arguments = {
        "reader": reader,
        "owner": owner,
        "scope": scope,
        "key": key,
        "tags": list(tags if tags is not None else ["topic_shared"]),
        "family_graph": family,
        "topics_graph": topics,
        "static_policy": static_policy,
    }

    first = _decision_function()(**arguments)
    second = _decision_function()(**arguments)

    assert family == original_family
    assert topics == original_topics
    assert (first.allowed, first.reason) == (second.allowed, second.reason)
    assert first.allowed is expected_allowed
    assert first.reason == expected_reason


@pytest.mark.parametrize(
    "key",
    (
        "unknown:opaque",
        "service:opaque",
        "access:family_grant",
        "credential:family_token",
    ),
)
def test_unknown_and_sensitive_keys_fail_closed_before_family_topic(key: str) -> None:
    _assert_direct_decision(False, "invalid_key", key=key)


def test_unknown_key_fails_closed_before_static_allow() -> None:
    family = family_graph_sample()
    family["edges"] = []
    _assert_direct_decision(
        False,
        "invalid_key",
        reader="principal_gamma",
        owner="principal_alpha",
        key="unknown:static_candidate",
        family_graph=family,
        static_policy="allow",
    )


@pytest.mark.parametrize("key", ("memory:", "history:", "pending_migration:"))
def test_malformed_recognized_keys_are_invalid(key: str) -> None:
    _assert_direct_decision(False, "invalid_key", key=key, static_policy="allow")


@pytest.mark.parametrize(
    "key",
    (
        "memory:fact_allowed",
        "value:user_profile",
        "value:memory",
        "value:heartbeat",
        "value:private_index",
        "value:shared_index",
    ),
)
def test_exact_ordinary_key_forms_use_family_topic_rules(key: str) -> None:
    _assert_direct_decision(True, "family_common_topic", key=key)


@pytest.mark.parametrize(
    ("static_policy", "allowed", "reason"),
    (
        ("allow", True, "static_policy_allow"),
        ("deny", False, "static_policy_deny"),
    ),
)
def test_static_policy_is_used_for_valid_ordinary_record_outside_graph_rules(
    static_policy: str,
    allowed: bool,
    reason: str,
) -> None:
    family = family_graph_sample()
    family["edges"] = []
    _assert_direct_decision(
        allowed,
        reason,
        reader="principal_gamma",
        owner="principal_alpha",
        key="memory:static_fact",
        tags=["topic_alpha"],
        family_graph=family,
        static_policy=static_policy,
    )


def test_pending_migration_is_owner_only() -> None:
    _assert_direct_decision(
        True,
        "owner_self",
        reader="principal_alpha",
        owner="principal_alpha",
        key="pending_migration:choice_alpha",
        tags=[],
        static_policy="deny",
    )
    _assert_direct_decision(
        False,
        "owner_only_service_key",
        key="pending_migration:choice_beta",
        static_policy="allow",
    )


@pytest.mark.parametrize(
    ("key", "reason"),
    (
        ("unknown:owner_self", "invalid_key"),
        ("memory:", "invalid_key"),
        ("history:owner_source", "internal_transaction_candidate"),
    ),
)
def test_owner_self_does_not_override_key_restrictions(
    key: str,
    reason: str,
) -> None:
    _assert_direct_decision(
        False,
        reason,
        reader="principal_alpha",
        owner="principal_alpha",
        key=key,
        tags=[],
        static_policy="allow",
    )


def test_principal_id_tag_with_fake_concerns_is_invalid_graph() -> None:
    topics = topics_graph_sample()
    topics["edges"].extend(
        (
            {
                "from": "principal_alpha",
                "to": "principal_alpha",
                "rel": "concerns",
            },
            {
                "from": "principal_alpha",
                "to": "principal_beta",
                "rel": "concerns",
            },
        )
    )
    _assert_direct_decision(
        False,
        "invalid_graph",
        tags=["principal_alpha"],
        topics_graph=topics,
    )


@pytest.mark.parametrize("topic_state", ("missing", "wrong_type"))
def test_concerns_source_must_be_existing_topic_node(topic_state: str) -> None:
    topics = topics_graph_sample()
    if topic_state == "missing":
        topics["nodes"] = [
            node for node in topics["nodes"] if node["id"] != "topic_shared"
        ]
    else:
        next(node for node in topics["nodes"] if node["id"] == "topic_shared")[
            "type"
        ] = "principal"
    _assert_direct_decision(False, "invalid_graph", topics_graph=topics)


@pytest.mark.parametrize("principal_state", ("missing", "wrong_type"))
def test_concerns_target_must_be_existing_principal_node(principal_state: str) -> None:
    family = family_graph_sample()
    if principal_state == "missing":
        family["nodes"] = [
            node for node in family["nodes"] if node["id"] != "principal_beta"
        ]
    else:
        next(node for node in family["nodes"] if node["id"] == "principal_beta")[
            "type"
        ] = "topic"
    _assert_direct_decision(False, "invalid_graph", family_graph=family)


def test_topic_concerning_only_one_participant_is_not_common() -> None:
    topics = {
        "nodes": [{"id": "topic_one_sided", "type": "topic"}],
        "edges": [
            {
                "from": "topic_one_sided",
                "to": "principal_alpha",
                "rel": "concerns",
            }
        ],
        "updated_at_ms": 1,
    }
    _assert_direct_decision(
        False,
        "no_common_topic",
        tags=["topic_one_sided"],
        topics_graph=topics,
    )


def test_transitive_concerns_are_invalid_instead_of_granting_access() -> None:
    topics = {
        "nodes": [
            {"id": "topic_outer", "type": "topic"},
            {"id": "topic_inner", "type": "topic"},
        ],
        "edges": [
            {"from": "topic_outer", "to": "topic_inner", "rel": "concerns"},
            {
                "from": "topic_inner",
                "to": "principal_alpha",
                "rel": "concerns",
            },
            {
                "from": "topic_inner",
                "to": "principal_beta",
                "rel": "concerns",
            },
        ],
        "updated_at_ms": 1,
    }
    _assert_direct_decision(
        False,
        "invalid_graph",
        tags=["topic_outer"],
        topics_graph=topics,
    )


def test_multiple_topics_with_different_reader_circles_fail_closed() -> None:
    topics = {
        "nodes": [
            {"id": "topic_first", "type": "topic"},
            {"id": "topic_second", "type": "topic"},
        ],
        "edges": [
            {"from": "topic_first", "to": "principal_alpha", "rel": "concerns"},
            {"from": "topic_first", "to": "principal_beta", "rel": "concerns"},
            {"from": "topic_second", "to": "principal_alpha", "rel": "concerns"},
            {"from": "topic_second", "to": "principal_beta", "rel": "concerns"},
            {"from": "topic_second", "to": "principal_child", "rel": "concerns"},
        ],
        "updated_at_ms": 1,
    }
    _assert_direct_decision(
        False,
        "invalid_record",
        tags=["topic_first", "topic_second"],
        topics_graph=topics,
    )


def test_multiple_topics_ignore_different_unrelated_participants() -> None:
    family = family_graph_sample()
    family["nodes"].extend(
        (
            {"id": "principal_unrelated_first", "type": "principal"},
            {"id": "principal_unrelated_second", "type": "principal"},
        )
    )
    topics = {
        "nodes": [
            {"id": "topic_first", "type": "topic"},
            {"id": "topic_second", "type": "topic"},
        ],
        "edges": [
            {"from": "topic_first", "to": "principal_alpha", "rel": "concerns"},
            {"from": "topic_first", "to": "principal_beta", "rel": "concerns"},
            {
                "from": "topic_first",
                "to": "principal_unrelated_first",
                "rel": "concerns",
            },
            {"from": "topic_second", "to": "principal_alpha", "rel": "concerns"},
            {"from": "topic_second", "to": "principal_beta", "rel": "concerns"},
            {
                "from": "topic_second",
                "to": "principal_unrelated_second",
                "rel": "concerns",
            },
        ],
        "updated_at_ms": 1,
    }
    _assert_direct_decision(
        True,
        "family_common_topic",
        tags=["topic_first", "topic_second"],
        family_graph=family,
        topics_graph=topics,
    )


def test_exact_direct_common_topic_remains_allowed() -> None:
    topics = {
        "nodes": [{"id": "topic_exact", "type": "topic"}],
        "edges": [
            {"from": "topic_exact", "to": "principal_alpha", "rel": "concerns"},
            {"from": "topic_exact", "to": "principal_beta", "rel": "concerns"},
        ],
        "updated_at_ms": 1,
    }
    _assert_direct_decision(
        True,
        "family_common_topic",
        tags=["topic_exact"],
        topics_graph=topics,
    )


def _principal_graph(*principal_ids: str) -> dict[str, Any]:
    return {
        "nodes": [
            {"id": principal_id, "type": "principal"}
            for principal_id in principal_ids
        ],
        "edges": [],
        "updated_at_ms": 1,
    }


def _topics_graph(
    topic_ids: tuple[str, ...] = (),
    edges: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "nodes": [
            {"id": topic_id, "type": "topic"}
            for topic_id in topic_ids
        ],
        "edges": list(edges or []),
        "updated_at_ms": 1,
    }


def _edge_payload(
    form: str,
    source: str,
    destination: str,
    relation: str,
) -> dict[str, str]:
    if form == "legacy":
        return {"from": source, "to": destination, "rel": relation}
    if form == "schema":
        return {"src": source, "dst": destination, "rel": relation}
    if form == "schema_fallback":
        return {
            "from": "",
            "to": "",
            "src": source,
            "dst": destination,
            "rel": relation,
        }
    if form == "mixed_fallback":
        return {
            "from": source,
            "to": "",
            "src": "ignored_source",
            "dst": destination,
            "rel": relation,
        }
    if form == "legacy_precedence":
        return {
            "from": source,
            "to": destination,
            "src": "ignored_source",
            "dst": "ignored_destination",
            "rel": relation,
        }
    raise AssertionError(f"unknown edge form: {form}")


_EDGE_FORMS = (
    "legacy",
    "schema",
    "schema_fallback",
    "mixed_fallback",
    "legacy_precedence",
)


@pytest.mark.parametrize("edge_form", _EDGE_FORMS)
def test_family_relation_accepts_canonical_edge_forms(edge_form: str) -> None:
    family = _principal_graph("principal_alpha", "principal_beta")
    family["edges"] = [
        _edge_payload(
            edge_form,
            "principal_alpha",
            "principal_beta",
            "spouse_of",
        )
    ]
    topics = _topics_graph(
        ("topic_edge",),
        [
            _edge_payload(
                "legacy",
                "topic_edge",
                principal_id,
                "concerns",
            )
            for principal_id in ("principal_alpha", "principal_beta")
        ],
    )
    _assert_direct_decision(
        True,
        "family_common_topic",
        tags=["topic_edge"],
        family_graph=family,
        topics_graph=topics,
    )


@pytest.mark.parametrize("edge_form", _EDGE_FORMS)
def test_concerns_accepts_canonical_edge_forms(edge_form: str) -> None:
    family = _principal_graph("principal_alpha", "principal_beta")
    family["edges"] = [
        _edge_payload(
            "legacy",
            "principal_alpha",
            "principal_beta",
            "spouse_of",
        )
    ]
    topics = _topics_graph(
        ("topic_edge",),
        [
            _edge_payload(
                edge_form,
                "topic_edge",
                principal_id,
                "concerns",
            )
            for principal_id in ("principal_alpha", "principal_beta")
        ],
    )
    _assert_direct_decision(
        True,
        "family_common_topic",
        tags=["topic_edge"],
        family_graph=family,
        topics_graph=topics,
    )


@pytest.mark.parametrize(
    "edge",
    (
        {"to": "principal_beta", "rel": "spouse_of"},
        {"from": "principal_alpha", "rel": "spouse_of"},
        {"from": "", "src": "", "to": "principal_beta", "rel": "spouse_of"},
        {"from": "principal_alpha", "to": "", "dst": "", "rel": "spouse_of"},
    ),
)
def test_family_relation_requires_resolved_endpoints(edge: dict[str, str]) -> None:
    family = _principal_graph("principal_alpha", "principal_beta")
    family["edges"] = [edge]
    _assert_direct_decision(
        False,
        "invalid_graph",
        family_graph=family,
        topics_graph=_topics_graph(),
    )


@pytest.mark.parametrize(
    "edge",
    (
        {"to": "principal_alpha", "rel": "concerns"},
        {"from": "topic_edge", "rel": "concerns"},
        {"from": "", "src": "", "to": "principal_alpha", "rel": "concerns"},
        {"from": "topic_edge", "to": "", "dst": "", "rel": "concerns"},
    ),
)
def test_concerns_requires_resolved_endpoints(edge: dict[str, str]) -> None:
    family = _principal_graph("principal_alpha", "principal_beta")
    family["edges"] = [
        _edge_payload(
            "legacy",
            "principal_alpha",
            "principal_beta",
            "spouse_of",
        )
    ]
    _assert_direct_decision(
        False,
        "invalid_graph",
        tags=["topic_edge"],
        family_graph=family,
        topics_graph=_topics_graph(("topic_edge",), [edge]),
    )


@pytest.mark.parametrize(
    ("branch", "participant", "state"),
    (
        ("self", "reader_owner", "missing"),
        ("self", "reader_owner", "wrong_type"),
        ("pair", "reader", "missing"),
        ("pair", "reader", "wrong_type"),
        ("pair", "owner", "missing"),
        ("pair", "owner", "wrong_type"),
        ("static", "reader", "missing"),
        ("static", "reader", "wrong_type"),
        ("static", "owner", "missing"),
        ("static", "owner", "wrong_type"),
    ),
)
def test_reader_and_owner_must_be_registered_principals_before_allow(
    branch: str,
    participant: str,
    state: str,
) -> None:
    if branch == "self":
        reader = owner = "principal_alpha"
        principal_ids = ("principal_alpha",)
        target = "principal_alpha"
        scope = "private"
        static_policy = "deny"
    elif branch == "pair":
        reader = "principal_alpha"
        owner = "principal_beta"
        principal_ids = (reader, owner)
        target = reader if participant == "reader" else owner
        scope = "pair:pair-v1/15:principal_alpha/14:principal_beta"
        static_policy = "deny"
    else:
        reader = "principal_gamma"
        owner = "principal_alpha"
        principal_ids = (reader, owner)
        target = reader if participant == "reader" else owner
        scope = "private"
        static_policy = "allow"

    family = _principal_graph(*principal_ids)
    if state == "missing":
        family["nodes"] = [
            node for node in family["nodes"] if node["id"] != target
        ]
    else:
        next(node for node in family["nodes"] if node["id"] == target)[
            "type"
        ] = "topic"

    _assert_direct_decision(
        False,
        "invalid_record",
        reader=reader,
        owner=owner,
        scope=scope,
        tags=[],
        family_graph=family,
        topics_graph=_topics_graph(),
        static_policy=static_policy,
    )


@pytest.mark.parametrize(
    ("branch", "allowed_reason"),
    (
        ("self", "owner_self"),
        ("pair", "pair_member"),
        ("static", "static_policy_allow"),
    ),
)
def test_registered_principals_reach_existing_allow_branches(
    branch: str,
    allowed_reason: str,
) -> None:
    if branch == "self":
        reader = owner = "principal_alpha"
        principal_ids = ("principal_alpha",)
        scope = "private"
        static_policy = "deny"
    elif branch == "pair":
        reader = "principal_alpha"
        owner = "principal_beta"
        principal_ids = (reader, owner)
        scope = "pair:pair-v1/15:principal_alpha/14:principal_beta"
        static_policy = "deny"
    else:
        reader = "principal_gamma"
        owner = "principal_alpha"
        principal_ids = (reader, owner)
        scope = "private"
        static_policy = "allow"

    _assert_direct_decision(
        True,
        allowed_reason,
        reader=reader,
        owner=owner,
        scope=scope,
        tags=[],
        family_graph=_principal_graph(*principal_ids),
        topics_graph=_topics_graph(),
        static_policy=static_policy,
    )
