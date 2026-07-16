from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


FAMILY_RELATIONS = frozenset(
    {
        "spouse_of",
        "parent_of",
        "owner_of",
        "caregiver_of",
        "guardian_of",
    }
)


def _principal(
    principal_id: str,
    *,
    ordinal: int,
    roles: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": principal_id,
        "display_name": principal_id.replace("_", " ").title(),
        "roles": list(roles or []),
        "memx_key": f"fixture-key-{ordinal}",
        "identities": [
            {"channel": "telegram", "sender_id": str(910_000 + ordinal)},
            {"channel": "vk", "sender_id": str(920_000 + ordinal)},
        ],
    }


def principal_alpha() -> dict[str, Any]:
    return _principal("principal_alpha", ordinal=1)


def principal_beta() -> dict[str, Any]:
    return _principal("principal_beta", ordinal=2)


def principal_gamma() -> dict[str, Any]:
    return _principal("principal_gamma", ordinal=3)


def principal_child() -> dict[str, Any]:
    return _principal("principal_child", ordinal=4, roles=["child"])


def principal_registry_sample() -> dict[str, Any]:
    return {
        "principals": [
            principal_alpha(),
            principal_beta(),
            principal_gamma(),
            principal_child(),
        ]
    }


def family_graph_sample() -> dict[str, Any]:
    return {
        "nodes": [
            {"id": principal_id, "type": "principal"}
            for principal_id in (
                "principal_alpha",
                "principal_beta",
                "principal_gamma",
                "principal_child",
            )
        ],
        "edges": [
            {"from": "principal_alpha", "to": "principal_beta", "rel": "spouse_of"},
            {"from": "principal_alpha", "to": "principal_child", "rel": "parent_of"},
            {"from": "principal_alpha", "to": "principal_child", "rel": "owner_of"},
            {"from": "principal_beta", "to": "principal_child", "rel": "caregiver_of"},
            {"from": "principal_beta", "to": "principal_child", "rel": "guardian_of"},
        ],
        "updated_at_ms": 1,
    }


def topics_graph_sample() -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "topic_shared", "type": "topic"},
            {"id": "topic_alpha", "type": "topic"},
            {"id": "topic_beta", "type": "topic"},
        ],
        "edges": [
            {
                "from": "topic_shared",
                "to": "principal_alpha",
                "rel": "concerns",
                "concerns_as": "guardian_of",
            },
            {
                "from": "topic_shared",
                "to": "principal_beta",
                "rel": "concerns",
                "concerns_as": "guardian_of",
            },
            {
                "from": "topic_shared",
                "to": "principal_gamma",
                "rel": "concerns",
                "concerns_as": "guardian_of",
            },
            {
                "from": "topic_shared",
                "to": "principal_child",
                "rel": "concerns",
                "concerns_as": "guardian_of",
            },
            {
                "from": "topic_alpha",
                "to": "principal_alpha",
                "rel": "concerns",
                "concerns_as": "parent_of",
            },
            {
                "from": "topic_beta",
                "to": "principal_beta",
                "rel": "concerns",
                "concerns_as": "parent_of",
            },
        ],
        "updated_at_ms": 1,
    }


# This table is the shared oracle for the direct domain tests in tasks 1-2 and
# the synthetic acceptance barrier through task 8.
EXPECTED_MEMORY_READ_DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "case": "owner_reads_own_fact",
        "reader": "principal_alpha",
        "owner": "principal_alpha",
        "scope": "private",
        "key": "memory:fact_alpha",
        "tags": ["topic_alpha"],
        "static_policy": "deny",
        "relation": None,
        "relation_direction": "self",
        "allowed": True,
        "reason": "owner_self",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "spouse_forward_common_topic_explicit_deny",
        "reader": "principal_alpha",
        "owner": "principal_beta",
        "scope": "private",
        "key": "memory:fact_spouse",
        "tags": ["topic_shared"],
        "static_policy": "deny",
        "relation": "spouse_of",
        "relation_direction": "forward",
        "allowed": True,
        "reason": "family_common_topic",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "parent_forward_common_topic_secret",
        "reader": "principal_alpha",
        "owner": "principal_child",
        "scope": "private",
        "key": "memory:fact_parent_forward",
        "tags": ["topic_shared", "secret"],
        "static_policy": "no_matching_rule",
        "relation": "parent_of",
        "relation_direction": "forward",
        "allowed": True,
        "reason": "family_common_topic",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "parent_reverse_common_topic",
        "reader": "principal_child",
        "owner": "principal_alpha",
        "scope": "private",
        "key": "memory:fact_parent_reverse",
        "tags": ["topic_shared"],
        "static_policy": "no_matching_rule",
        "relation": "parent_of",
        "relation_direction": "reverse",
        "allowed": True,
        "reason": "family_common_topic",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "owner_forward_common_topic",
        "reader": "principal_alpha",
        "owner": "principal_child",
        "scope": "private",
        "key": "memory:fact_owner",
        "tags": ["topic_shared"],
        "static_policy": "no_matching_rule",
        "relation": "owner_of",
        "relation_direction": "forward",
        "allowed": True,
        "reason": "family_common_topic",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "caregiver_forward_common_topic",
        "reader": "principal_beta",
        "owner": "principal_child",
        "scope": "private",
        "key": "memory:fact_caregiver",
        "tags": ["topic_shared"],
        "static_policy": "no_matching_rule",
        "relation": "caregiver_of",
        "relation_direction": "forward",
        "allowed": True,
        "reason": "family_common_topic",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "guardian_forward_common_topic",
        "reader": "principal_beta",
        "owner": "principal_child",
        "scope": "private",
        "key": "memory:fact_guardian_forward",
        "tags": ["topic_shared"],
        "static_policy": "no_matching_rule",
        "relation": "guardian_of",
        "relation_direction": "forward",
        "allowed": True,
        "reason": "family_common_topic",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "guardian_reverse_common_topic",
        "reader": "principal_child",
        "owner": "principal_beta",
        "scope": "private",
        "key": "memory:fact_guardian_reverse",
        "tags": ["topic_shared"],
        "static_policy": "no_matching_rule",
        "relation": "guardian_of",
        "relation_direction": "reverse",
        "allowed": True,
        "reason": "family_common_topic",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "related_reader_legacy_untagged_memory",
        "reader": "principal_beta",
        "owner": "principal_alpha",
        "scope": "private",
        "key": "value:memory",
        "tags": [],
        "static_policy": "deny",
        "relation": "spouse_of",
        "relation_direction": "reverse",
        "allowed": True,
        "reason": "family_legacy_untagged",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "same_topic_without_family_relation",
        "reader": "principal_gamma",
        "owner": "principal_alpha",
        "scope": "private",
        "key": "memory:fact_shared",
        "tags": ["topic_shared"],
        "static_policy": "allow",
        "relation": None,
        "relation_direction": "none",
        "allowed": False,
        "reason": "topic_without_family_relation",
        "visibility": {"memory_get": False, "history": False, "index": False},
    },
    {
        "case": "related_reader_different_topic",
        "reader": "principal_alpha",
        "owner": "principal_beta",
        "scope": "private",
        "key": "memory:fact_beta",
        "tags": ["topic_beta"],
        "static_policy": "allow",
        "relation": "spouse_of",
        "relation_direction": "forward",
        "allowed": False,
        "reason": "no_common_topic",
        "visibility": {"memory_get": False, "history": False, "index": False},
    },
    {
        "case": "transaction_candidate_hidden_from_related_reader",
        "reader": "principal_beta",
        "owner": "principal_alpha",
        "scope": "private",
        "key": "history:source_alpha",
        "tags": ["topic_shared"],
        "static_policy": "allow",
        "relation": "spouse_of",
        "relation_direction": "reverse",
        "allowed": False,
        "reason": "internal_transaction_candidate",
        "visibility": {"memory_get": False, "history": False, "index": False},
    },
    {
        "case": "service_key_is_owner_only",
        "reader": "principal_beta",
        "owner": "principal_alpha",
        "scope": "private",
        "key": "pending_migration:choice_alpha",
        "tags": ["topic_shared"],
        "static_policy": "allow",
        "relation": "spouse_of",
        "relation_direction": "reverse",
        "allowed": False,
        "reason": "owner_only_service_key",
        "visibility": {"memory_get": False, "history": False, "index": False},
    },
    {
        "case": "pair_member",
        "reader": "principal_alpha",
        "owner": "principal_beta",
        "scope": "pair:pair-v1/15:principal_alpha/14:principal_beta",
        "key": "memory:pair_fact",
        "tags": [],
        "static_policy": "no_matching_rule",
        "relation": None,
        "relation_direction": "none",
        "allowed": True,
        "reason": "pair_member",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
    {
        "case": "pair_non_member",
        "reader": "principal_gamma",
        "owner": "principal_beta",
        "scope": "pair:pair-v1/15:principal_alpha/14:principal_beta",
        "key": "memory:pair_fact",
        "tags": ["topic_shared"],
        "static_policy": "allow",
        "relation": None,
        "relation_direction": "none",
        "allowed": False,
        "reason": "pair_non_member",
        "visibility": {"memory_get": False, "history": False, "index": False},
    },
    {
        "case": "shared_family_memory",
        "reader": "principal_beta",
        "owner": "principal_alpha",
        "scope": "shared",
        "key": "memory:shared_fact",
        "tags": [],
        "static_policy": "no_matching_rule",
        "relation": "spouse_of",
        "relation_direction": "reverse",
        "allowed": True,
        "reason": "shared_family_relation",
        "visibility": {"memory_get": True, "history": True, "index": True},
    },
)


@dataclass(frozen=True)
class SyntheticSnapshot:
    root: Path
    workspace: Path
    existing_memory: dict[str, str]
    family_graph: dict[str, Any]
    topics_graph: dict[str, Any]
    deterministic_conflict: dict[str, Any]
    expected_end_state: dict[str, Any]


def _history_record(cursor: int, actor: str | None, content: str) -> str:
    record: dict[str, Any] = {
        "schema_version": 1,
        "cursor": cursor,
        "timestamp": f"2026-07-16 10:{cursor:02d}",
        "content": content,
        "provenance": {"source": "synthetic_snapshot", "idempotency_key": None},
    }
    if actor is not None:
        record["actor"] = actor
    return json.dumps(record, sort_keys=True)


def write_synthetic_snapshot(base: Path) -> SyntheticSnapshot:
    root = base / "synthetic-snapshot"
    workspace = root / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)

    (root / "principals.json").write_text(
        json.dumps(principal_registry_sample(), sort_keys=True),
        encoding="utf-8",
    )
    family_graph = family_graph_sample()
    topics_graph = topics_graph_sample()
    deterministic_conflict = {
        "owner": "principal_alpha",
        "destination": "private:principal_alpha:memory:fact_conflict",
        "existing": {"value": "stable synthetic fact", "revision": 7},
        "proposed": {
            "value": "replacement synthetic fact",
            "expected_revision": 6,
        },
        "outcome": "awaiting_owner",
    }
    (workspace / "SOUL.md").write_bytes(b"synthetic soul bytes\n")
    (workspace / "USER.md").write_bytes(b"flat user data must be erased\n")
    (workspace / "MEMORY.md").write_bytes(b"root flat memory must be erased\n")
    (memory / "MEMORY.md").write_bytes(b"nested flat memory must be erased\n")
    history = [
        _history_record(1, "principal_alpha", "alpha profile fact"),
        _history_record(2, "principal_beta", "beta memory fact"),
        _history_record(3, "principal_gamma", "gamma memory fact"),
        _history_record(
            4,
            "principal_alpha",
            json.dumps(
                {
                    "destination": deterministic_conflict["destination"],
                    **deterministic_conflict["proposed"],
                },
                sort_keys=True,
            ),
        ),
        _history_record(5, "principal_unknown", "unknown content"),
        _history_record(6, None, "actorless content"),
        "{malformed json",
    ]
    (memory / "history.jsonl").write_text("\n".join(history) + "\n", encoding="utf-8")

    existing_memory = {
        "private:principal_alpha:value:user_profile": "existing alpha profile",
        "private:principal_alpha:value:memory": "existing alpha memory",
        "private:principal_beta:value:user_profile": "existing beta profile",
        "private:principal_beta:value:memory": "existing beta memory",
        "private:principal_gamma:value:user_profile": "existing gamma profile",
        "private:principal_gamma:value:memory": "existing gamma memory",
        "private:principal_child:value:user_profile": "existing child profile",
        "private:principal_child:value:memory": "existing child memory",
        "shared:family.graph": json.dumps(family_graph, sort_keys=True),
        "shared:topics.graph": json.dumps(topics_graph, sort_keys=True),
        deterministic_conflict["destination"]: json.dumps(
            deterministic_conflict["existing"], sort_keys=True
        ),
    }
    expected_end_state = {
        "flat_paths_zero_bytes": [
            "workspace/USER.md",
            "workspace/MEMORY.md",
            "workspace/memory/MEMORY.md",
        ],
        "soul_bytes": b"synthetic soul bytes\n",
        "pre_switch_side_effects": [],
        "repeat_restore_duplicate_count": 0,
        "unknown_content_absent_from": ["cleaned_history", "memx", "journals"],
        "conflict_owner_only": "principal_alpha",
        "conflict_notification_contains_fact_text": False,
        "decision_cases": [row["case"] for row in EXPECTED_MEMORY_READ_DECISIONS],
    }
    return SyntheticSnapshot(
        root=root,
        workspace=workspace,
        existing_memory=existing_memory,
        family_graph=family_graph,
        topics_graph=topics_graph,
        deterministic_conflict=deterministic_conflict,
        expected_end_state=expected_end_state,
    )
