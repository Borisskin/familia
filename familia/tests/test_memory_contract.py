from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

from jsonschema import Draft202012Validator

from memory_contract_samples import (
    EXPECTED_MEMORY_READ_DECISIONS,
    FAMILY_RELATIONS,
    family_graph_sample,
    principal_alpha,
    principal_beta,
    principal_child,
    principal_gamma,
    topics_graph_sample,
    write_synthetic_snapshot,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_SCHEMA_PATH = REPO_ROOT / "release" / "memory-contract.schema.json"
MIGRATION_SCHEMA_PATH = REPO_ROOT / "release" / "memory-migration.schema.json"
RELEASE_IDENTITY_PATH = REPO_ROOT / "release" / "release-identity.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(path: Path) -> Draft202012Validator:
    schema = _load_json(path)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _assert_valid(validator: Draft202012Validator, value: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def _assert_invalid(validator: Draft202012Validator, value: dict[str, Any]) -> None:
    assert list(validator.iter_errors(value))


def _canonical_contract() -> dict[str, Any]:
    module = importlib.import_module("familia.memory_contract")
    return module.MEMORY_CONTRACT


def _plan_report() -> dict[str, Any]:
    return {
        "schema_version": "2.0.0",
        "migration_kind": "legacy_upgrade",
        "source_contract_version": "1.0.0",
        "target_contract_version": "2.0.0",
        "workspace": "/srv/familia/workspace",
        "known_actors": ["principal_alpha"],
        "dry_run": True,
        "status": "ready",
        "actions": [
            {
                "phase": "files",
                "component": "user_profile",
                "source": "USER.md",
                "destination": None,
                "actor": None,
                "candidate_actor": None,
                "disposition": "erase_without_read",
                "reason": "flat_memory_retired",
            },
            {
                "phase": "files",
                "component": "memory",
                "source": "MEMORY.md",
                "destination": None,
                "actor": None,
                "candidate_actor": None,
                "disposition": "erase_without_read",
                "reason": "flat_memory_retired",
            },
            {
                "phase": "files",
                "component": "memory",
                "source": "memory/MEMORY.md",
                "destination": None,
                "actor": None,
                "candidate_actor": None,
                "disposition": "erase_without_read",
                "reason": "flat_memory_retired",
            },
            {
                "phase": "history",
                "component": "history",
                "source": "memory/history.jsonl",
                "source_sha256": "1" * 64,
                "actor": "principal_alpha",
                "fact_id": "legacy-history",
                "source_actors": ["principal_alpha"],
                "cursors": [1],
                "record_count": 1,
                "destination": "private:principal_alpha:memory:legacy-history",
                "disposition": "llm_required",
                "reason": "history_requires_consolidation",
            },
        ],
        "summary": {
            "erase_without_read": 3,
            "llm_required": 1,
        },
    }


def _apply_report(*, status: str = "complete") -> dict[str, Any]:
    reports = {
        "complete": {
            "status": "complete",
            "applied_actions": 4,
            "written_keys": [
                "private:principal_alpha:memory:legacy-history"
            ],
            "failed_actors": [],
            "failed_actions": [],
            "fatal_failure": None,
            "dream_cursor_updated": True,
        },
        "partial": {
            "status": "partial",
            "applied_actions": 1,
            "written_keys": [
                "private:principal_alpha:memory:legacy-history"
            ],
            "failed_actors": ["principal_beta"],
            "failed_actions": ["history:principal_beta"],
            "fatal_failure": "history:principal_beta",
            "dream_cursor_updated": False,
        },
        "failed": {
            "status": "failed",
            "applied_actions": 0,
            "written_keys": [],
            "failed_actors": ["principal_alpha"],
            "failed_actions": ["history:principal_alpha"],
            "fatal_failure": "history:principal_alpha",
            "dream_cursor_updated": False,
        },
    }
    return deepcopy(reports[status])


def test_contract_schema_declares_breaking_version_2() -> None:
    schema = _load_json(CONTRACT_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("memory-contract-2.0.0.json")
    assert schema["properties"]["contract_version"] == {"const": "2.0.0"}


def test_canonical_contract_export_exists_and_validates() -> None:
    contract = _canonical_contract()
    assert contract["contract_version"] == "2.0.0"
    _assert_valid(_validator(CONTRACT_SCHEMA_PATH), contract)


def test_contract_records_simple_private_write_and_consolidation_rules() -> None:
    contract = _canonical_contract()

    assert contract["authority"]["final_writer"] == "PrincipalMemoryIngestor"
    assert contract["authority"]["model_may_select_owner"] is False
    assert contract["authority"]["model_may_request_topic"] is True
    assert contract["authority"]["server_validates_topic"] is True
    assert contract["authority"]["model_may_create_topic"] is False

    assert contract["storage"]["atomic_fact_key"] == "private:<principal>:memory:<fact_id>"
    assert "transaction_candidate_key" not in contract["storage"]
    assert contract["storage"]["fact_topics"] == {
        "assigned_by": "server",
        "homogeneous_reader_set": True,
    }

    assert contract["write_routing"] == {
        "physical_owner": "actor",
        "owner_assigned_by": "server",
        "default_destination": "private",
        "third_party_fact_owner": "speaker",
        "foreign_private_allowed": False,
        "direct_shared_allowed": False,
        "direct_pair_allowed": False,
        "topic": {
            "form": "server_verified_private_fact_tag",
            "creation": "admin_only",
            "existing_without_shared_relations": "tag_and_notify_private_only",
            "missing_or_unavailable": "store_private_untagged_and_notify",
        },
    }

    assert contract["updates"] == {
        "identity": "stable_fact_id",
        "record_revision": "ts",
        "full_memory_scan_required": False,
        "operations": ["profile", "memory", "delete"],
        "delete_scope": "private_actor_fact_only",
        "delete_missing": "success",
    }

    assert contract["consolidation"] == {
        "supported_sources": ["vk_private", "telegram_private"],
        "automatic_destination": "private_owner_untagged",
        "owner_resolver": {
            "input": ["session_key", "messages"],
            "result": "principal_or_none",
            "before_model": True,
        },
        "archive_sink": {
            "input": ["principal", "messages"],
            "success": "return_without_exception",
            "failure": "exception",
            "failure_keeps_source_messages": True,
        },
        "retry": "reapply_stable_fact_operations",
        "session_serialization": "single_lock",
        "standalone_without_sink": "legacy_behavior",
    }
    assert "archive" not in contract

    assert contract["migration"]["exit_codes"] == {
        "plan": {"ready": 0},
        "apply": {"complete": 0, "partial": 2, "failed": 1},
    }


def test_contract_records_catalog_recall_and_exact_mutation_outcomes() -> None:
    contract = _canonical_contract()

    assert contract["storage"]["private_catalog"] == {
        "key": "private:<principal>:value:private_index",
        "entry": {"name": "memory:<fact_id>", "tags": "server_topic_tags"},
        "max_entries": 256,
        "overflow": "catalog_full_without_eviction",
        "update": "atomic_with_fact",
        "delete": "atomic_with_fact",
    }
    assert contract["recall"] == {
        "own_catalog": "trusted_private_catalog",
        "foreign_projection": "authorized_fact_names_only",
        "foreign_value_projection": False,
        "foreign_catalog_projection": False,
        "foreign_profile_projection": False,
        "max_names_per_owner": 40,
        "legacy_history_fact_id": "legacy-history",
    }
    assert contract["outcomes"]["memory_mutation"] == {
        "committed": {
            "committed": True,
            "updated": True,
            "retryable": False,
            "version": "new_number",
        },
        "catalog_full": {
            "committed": False,
            "updated": False,
            "retryable": False,
            "version": "current_number_or_null",
        },
        "deleted": {
            "committed": True,
            "updated": True,
            "retryable": False,
            "version": "previous_number_or_null",
        },
        "absent": {
            "committed": True,
            "updated": False,
            "retryable": False,
            "version": None,
        },
        "conflict": {
            "committed": False,
            "updated": False,
            "retryable": True,
            "version": "current_number_or_null",
        },
    }


def test_contract_records_registry_access_unknown_flat_file_and_soul_rules() -> None:
    contract = _canonical_contract()

    assert contract["registry"]["required_non_empty"] is True
    assert contract["registry"]["unique_fields"] == ["principal.id", "memx_key"]
    assert contract["registry"]["effective_identity_keys_unique_between_principals"] is True
    assert contract["registry"]["first_install_uses_same_validation"] is True

    access = contract["access"]
    assert set(access["family_relation_kinds"]) == FAMILY_RELATIONS
    assert access["family_relation_direction_matters"] is False
    assert access["common_topic_requires_family_relation"] is True
    assert "common_topic_ordinary_memory_overrides" not in access
    assert access["legacy_untagged_memory_available_to_family"] is False
    assert access["pair_scope"] == "deny"
    assert access["shared_scope"] == "deny"
    assert access["service_keys_owner_only"] is True
    assert access["secret_records_owner_only"] is True
    assert access["foreign_atomic_fact_requires"] == [
        "trusted_catalog_match",
        "direct_family_relation",
        "verified_common_topic",
    ]
    assert "transaction_candidate_internal_only" not in access

    assert contract["unknown_content"] == {
        "discard_before_model": True,
        "allowed_persistence": ["reason_counters"],
        "forbidden_persistence": [
            "content",
            "excerpt",
            "plain_hash",
            "cryptographic_hash",
            "derived_copy",
            "quarantine",
            "manual_review_queue",
        ],
    }
    assert contract["flat_memory"] == {
        "paths": ["USER.md", "MEMORY.md", "memory/MEMORY.md"],
        "may_be_memory_source": False,
        "required_size_bytes_after_transition": 0,
    }
    assert contract["soul"]["initial_template"] == (
        "admin/src-tauri/resources/personality.template.txt"
    )
    assert contract["soul"]["ordinary_writer"] == "Admin"
    assert contract["soul"]["restore_from_snapshot_is_only_exception"] is True
    assert contract["soul"]["preserve_bytes_during"] == [
        "dream",
        "dream_restore",
        "conversation_consolidation",
        "version_update",
        "legacy_transition",
        "failed_restore",
    ]


def test_contract_keeps_exact_migration_and_memory_mutation_outcomes() -> None:
    outcomes = _canonical_contract()["outcomes"]

    assert set(outcomes) == {"legacy_row", "migration_command", "memory_mutation"}
    assert outcomes["legacy_row"] == {
        "values": [
            "imported",
            "duplicate",
            "awaiting_owner",
            "discarded_unknown",
            "retryable_failure",
        ],
        "terminal": ["imported", "duplicate", "awaiting_owner", "discarded_unknown"],
    }
    assert outcomes["migration_command"] == {
        "plan": {
            "values": ["ready"],
            "terminal": ["ready"],
        },
        "apply": {
            "values": ["complete", "partial", "failed"],
            "terminal": ["complete", "partial", "failed"],
        },
    }


def test_contract_2_preserves_non_conflicting_identity_pair_and_failure_guarantees() -> None:
    contract = _canonical_contract()

    assert contract["identity"] == {
        "principal_id": {
            "canonical_regex": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
            "encoding": "UTF-8",
            "normalization": "none",
            "registered_required": True,
        },
        "multi_principal": {
            "explicit_actor_required": True,
            "actor_must_be_canonical": True,
            "actor_must_be_registered": True,
            "missing_or_invalid_outcome": "denied_invalid",
        },
        "executor_destination_owner_distinct": True,
    }
    assert contract["destination_registry"] == {
        "version": "2.0.0",
        "arbitrary_destinations_allowed": False,
        "generic_file_fallback_allowed": False,
    }

    pair_codec = contract["pair_codec"]
    assert pair_codec["version"] == "pair-key-v1"
    assert pair_codec["principal_id_domain"] == {
        "canonical_regex": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        "encoding": "UTF-8",
        "normalization": "none",
    }
    assert pair_codec["member_cardinality"] == 2
    assert pair_codec["members_distinct"] is True
    assert pair_codec["ordering"] == "ascending_utf8_bytes"
    assert pair_codec["physical_grammar"] == (
        "pair-v1/<decimal-utf8-byte-length>:<id>/"
        "<decimal-utf8-byte-length>:<id>"
    )
    assert pair_codec["length_grammar"] == "base10_no_leading_zero_positive_integer"
    assert set(pair_codec["decode_rejection_rules"]) == {
        "invalid_prefix",
        "invalid_or_noncanonical_length",
        "invalid_utf8_or_principal_id",
        "truncated_or_surplus_member_bytes",
        "noncanonical_member_order",
        "duplicate_members",
        "trailing_bytes",
    }
    assert pair_codec["injective_over_full_id_domain"] is True
    assert pair_codec["shared_by"] == ["value_keys", "acl_generation"]
    assert pair_codec["collision_vector"] == {
        "left_members": ["a_b", "c"],
        "right_members": ["a", "b_c"],
        "left_encoding": "pair-v1/3:a_b/1:c",
        "right_encoding": "pair-v1/1:a/3:b_c",
        "must_differ": True,
    }

    assert contract["failure_rules"] == {
        "unknown_scope": {
            "outcome": "denied_invalid",
            "destination_selected": False,
            "writes": 0,
        },
        "invalid_actor_multi_principal": {
            "cases": [
                "missing",
                "blank",
                "malformed",
                "non_canonical",
                "unknown_to_registry",
            ],
            "outcome": "denied_invalid",
            "destination_selected": False,
            "writes": 0,
            "prompt_assembly_allowed": False,
        },
        "malformed_pair": {
            "cases": [
                "missing_member",
                "one_member",
                "three_or_more_members",
                "duplicate_members",
                "invalid_member",
                "noncanonical_order",
            ],
            "outcome": "denied_invalid",
            "destination_selected": False,
            "writes": 0,
        },
        "unregistered_destination": {
            "outcome": "denied_invalid",
            "fallback": "none",
            "writes": 0,
        },
    }


def test_old_contract_rules_are_rejected_by_schema() -> None:
    validator = _validator(CONTRACT_SCHEMA_PATH)
    contract = _canonical_contract()

    old_version = deepcopy(contract)
    old_version["contract_version"] = "1.0.0"
    _assert_invalid(validator, old_version)

    quarantines_unknown = deepcopy(contract)
    quarantines_unknown["unknown_content"]["allowed_persistence"] = ["quarantine"]
    _assert_invalid(validator, quarantines_unknown)

    reads_flat_files = deepcopy(contract)
    reads_flat_files["flat_memory"]["may_be_memory_source"] = True
    _assert_invalid(validator, reads_flat_files)

    old_private_rule = deepcopy(contract)
    old_private_rule["access"]["common_topic_requires_family_relation"] = False
    _assert_invalid(validator, old_private_rule)


def test_migration_schema_accepts_current_plan_and_all_apply_results() -> None:
    validator = _validator(MIGRATION_SCHEMA_PATH)
    plan = _plan_report()
    assert plan["status"] == "ready"
    assert plan["summary"] == {
        disposition: sum(
            action["disposition"] == disposition
            for action in plan["actions"]
        )
        for disposition in {
            action["disposition"] for action in plan["actions"]
        }
    }
    assert {
        "warnings",
        "needs_review",
        "blocked",
        "blocked_needs_review",
    }.isdisjoint(plan)
    _assert_valid(validator, plan)

    expected_apply_fields = {
        "status",
        "applied_actions",
        "written_keys",
        "failed_actors",
        "failed_actions",
        "fatal_failure",
        "dream_cursor_updated",
    }
    for status in ("complete", "partial", "failed"):
        report = _apply_report(status=status)
        assert set(report) == expected_apply_fields
        assert {"warnings", "needs_review", "blocked"}.isdisjoint(report)
        _assert_valid(validator, report)


def test_current_verify_requires_distinct_machine_checks() -> None:
    required_checks = {
        "flat_files_empty",
        "soul_verified",
        "registry_verified",
        "memx_verified",
        "receipts_verified",
        "target_invariants_verified",
        "no_unfinished_rows",
        "unknown_content_discarded",
    }
    assert set(
        _canonical_contract()["migration"]["current_verify"]["required_checks"]
    ) == required_checks


def test_migration_schema_rejects_obsolete_plan_states_fields_and_counts() -> None:
    validator = _validator(MIGRATION_SCHEMA_PATH)

    for status in (
        "planned",
        "blocked_needs_review",
        "ready_with_warnings",
        "needs_review",
    ):
        obsolete_status = _plan_report()
        obsolete_status["status"] = status
        _assert_invalid(validator, obsolete_status)

    for field, value in (
        ("warnings", 0),
        ("needs_review", 0),
        ("blocked", False),
        ("exit_code", 0),
        ("phase", "plan"),
        ("rows", []),
    ):
        obsolete_field = _plan_report()
        obsolete_field[field] = value
        _assert_invalid(validator, obsolete_field)

    for disposition in (
        "conflict",
        "quarantine_needs_review",
        "skip_warning",
        "dirty_legacy",
    ):
        obsolete_summary = _plan_report()
        obsolete_summary["summary"][disposition] = 0
        _assert_invalid(validator, obsolete_summary)

    missing_source = _plan_report()
    del missing_source["source_contract_version"]
    _assert_invalid(validator, missing_source)

    future_source = _plan_report()
    future_source["source_contract_version"] = "3.0.0"
    _assert_invalid(validator, future_source)

    wrong_target = _plan_report()
    wrong_target["target_contract_version"] = "1.0.0"
    _assert_invalid(validator, wrong_target)


def test_migration_schema_rejects_obsolete_or_inconsistent_apply_results() -> None:
    validator = _validator(MIGRATION_SCHEMA_PATH)

    for status in ("ready", "planned", "needs_review", "blocked_needs_review"):
        obsolete_status = _apply_report()
        obsolete_status["status"] = status
        _assert_invalid(validator, obsolete_status)

    for field, value in (
        ("warnings", 0),
        ("needs_review", 0),
        ("blocked", False),
        ("exit_code", 0),
        ("phase", "apply"),
        ("rows", []),
        ("checks", {}),
    ):
        obsolete_field = _apply_report()
        obsolete_field[field] = value
        _assert_invalid(validator, obsolete_field)

    complete_with_failure = _apply_report()
    complete_with_failure["fatal_failure"] = "history:principal_alpha"
    complete_with_failure["failed_actions"] = ["history:principal_alpha"]
    _assert_invalid(validator, complete_with_failure)

    for status in ("partial", "failed"):
        missing_failure = _apply_report(status=status)
        missing_failure["fatal_failure"] = None
        missing_failure["failed_actions"] = []
        _assert_invalid(validator, missing_failure)


def test_migration_schema_keeps_plan_and_apply_shapes_distinct() -> None:
    validator = _validator(MIGRATION_SCHEMA_PATH)

    plan_with_apply_field = _plan_report()
    plan_with_apply_field["applied_actions"] = 0
    _assert_invalid(validator, plan_with_apply_field)

    apply_with_plan_field = _apply_report()
    apply_with_plan_field["summary"] = {"erase_without_read": 3}
    _assert_invalid(validator, apply_with_plan_field)


def test_release_identity_and_runtime_bridge_select_contract_2() -> None:
    from familia.memory_migration import (
        MEMORY_CONTRACT_MIGRATION_KINDS,
        MEMORY_CONTRACT_VERSION,
        MigrationPreflightError,
        memory_contract_migration_kind,
    )

    release_identity = _load_json(RELEASE_IDENTITY_PATH)
    assert release_identity["memory_contract"]["contract_version"] == "2.0.0"
    assert MEMORY_CONTRACT_VERSION == "2.0.0"
    assert MEMORY_CONTRACT_MIGRATION_KINDS == {
        "legacy-unversioned": "legacy_upgrade",
        "1.0.0": "legacy_upgrade",
        "2.0.0": "current_verify",
    }
    for source_version, migration_kind in MEMORY_CONTRACT_MIGRATION_KINDS.items():
        assert memory_contract_migration_kind(source_version) == migration_kind
    try:
        memory_contract_migration_kind("3.0.0")
    except MigrationPreflightError as exc:
        assert "unsupported memory contract version" in str(exc)
    else:
        raise AssertionError("future memory contract version was accepted")


def test_legacy_snapshot_preflight_targets_contract_2() -> None:
    from familia.memory_migration import validate_migration_preflight

    snapshot_id = "4" * 64
    snapshot = {
        "schema_version": "1.0.0",
        "snapshot_format_version": "1.0.0",
        "snapshot_id": snapshot_id,
        "status": "complete",
        "state_role": "source",
        "versions": {"snapshot_schema": "1.0.0"},
    }
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        marker = {
            "marker_version": "1.0.0",
            "purpose": "familia-memory-migration",
            "target_id": "fixture-target",
            "non_production": True,
            "filesystem_root": str(target.resolve()),
            "snapshot_id": snapshot_id,
            "contract_version": "2.0.0",
        }
        validate_migration_preflight(snapshot, target, marker)


def test_anonymized_factories_cover_all_relations_unrelated_pair_and_topics() -> None:
    principals = [principal_alpha(), principal_beta(), principal_gamma(), principal_child()]
    assert {principal["id"] for principal in principals} == {
        "principal_alpha",
        "principal_beta",
        "principal_gamma",
        "principal_child",
    }
    assert all(principal["display_name"].startswith("Principal ") for principal in principals)

    family_graph = family_graph_sample()
    assert {edge["rel"] for edge in family_graph["edges"]} == FAMILY_RELATIONS
    assert not any(
        "principal_gamma" in (edge["from"], edge["to"])
        for edge in family_graph["edges"]
    )

    topic_edges = topics_graph_sample()["edges"]
    shared_members = {
        edge["to"] for edge in topic_edges if edge["from"] == "topic_shared"
    }
    assert shared_members == {
        "principal_alpha",
        "principal_beta",
        "principal_gamma",
        "principal_child",
    }
    assert any(
        edge["from"] == "topic_alpha" and edge["to"] == "principal_alpha"
        for edge in topic_edges
    )
    assert any(
        edge["from"] == "topic_beta" and edge["to"] == "principal_beta"
        for edge in topic_edges
    )


def test_synthetic_snapshot_fixture_has_graphs_conflict_and_full_expected_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        snapshot = write_synthetic_snapshot(Path(directory))

        assert snapshot.family_graph == family_graph_sample()
        assert snapshot.topics_graph == topics_graph_sample()
        assert json.loads(snapshot.existing_memory["shared:family.graph"]) == snapshot.family_graph
        assert json.loads(snapshot.existing_memory["shared:topics.graph"]) == snapshot.topics_graph

        conflict = snapshot.deterministic_conflict
        assert conflict == {
            "owner": "principal_alpha",
            "destination": "private:principal_alpha:memory:fact_conflict",
            "existing": {"value": "stable synthetic fact", "revision": 7},
            "proposed": {
                "value": "replacement synthetic fact",
                "expected_revision": 6,
            },
            "outcome": "awaiting_owner",
        }
        existing_conflict = json.loads(snapshot.existing_memory[conflict["destination"]])
        assert existing_conflict == conflict["existing"]
        history = [
            json.loads(line)
            for line in (snapshot.workspace / "memory" / "history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.startswith("{") and not line.startswith("{malformed")
        ]
        conflict_record = next(row for row in history if row["cursor"] == 4)
        assert json.loads(conflict_record["content"]) == {
            "destination": conflict["destination"],
            **conflict["proposed"],
        }

        expected = snapshot.expected_end_state
        assert expected["flat_paths_zero_bytes"] == [
            "workspace/USER.md",
            "workspace/MEMORY.md",
            "workspace/memory/MEMORY.md",
        ]
        assert expected["soul_bytes"] == b"synthetic soul bytes\n"
        assert expected["pre_switch_side_effects"] == []
        assert expected["repeat_restore_duplicate_count"] == 0
        assert expected["unknown_content_absent_from"] == [
            "cleaned_history",
            "memx",
            "journals",
        ]
        assert expected["conflict_owner_only"] == "principal_alpha"
        assert expected["conflict_notification_contains_fact_text"] is False
        assert expected["decision_cases"] == [
            row["case"] for row in EXPECTED_MEMORY_READ_DECISIONS
        ]


def test_shared_expected_decision_table_covers_next_task_matrix() -> None:
    rows_by_case = {row["case"]: row for row in EXPECTED_MEMORY_READ_DECISIONS}
    assert len(rows_by_case) == len(EXPECTED_MEMORY_READ_DECISIONS)
    assert set(rows_by_case) == {
        "owner_reads_own_fact",
        "spouse_forward_common_topic_explicit_deny",
        "parent_forward_common_topic_secret",
        "parent_reverse_common_topic",
        "owner_forward_common_topic",
        "caregiver_forward_common_topic",
        "guardian_forward_common_topic",
        "guardian_reverse_common_topic",
        "related_reader_legacy_untagged_memory",
        "same_topic_without_family_relation",
        "related_reader_different_topic",
        "transaction_candidate_hidden_from_related_reader",
        "service_key_is_owner_only",
        "pair_member",
        "pair_non_member",
        "shared_family_memory",
    }

    required_fields = {
        "reader",
        "owner",
        "scope",
        "key",
        "tags",
        "static_policy",
        "relation",
        "relation_direction",
        "allowed",
        "reason",
        "visibility",
    }
    assert all(required_fields <= row.keys() for row in rows_by_case.values())
    for row in rows_by_case.values():
        assert set(row["visibility"]) == {"memory_get", "history", "index"}
        assert row["allowed"] == row["visibility"]["memory_get"]
        assert row["allowed"] == row["visibility"]["history"]

    relation_cases = {
        "spouse_forward_common_topic_explicit_deny": ("spouse_of", "forward"),
        "parent_forward_common_topic_secret": ("parent_of", "forward"),
        "parent_reverse_common_topic": ("parent_of", "reverse"),
        "owner_forward_common_topic": ("owner_of", "forward"),
        "caregiver_forward_common_topic": ("caregiver_of", "forward"),
        "guardian_forward_common_topic": ("guardian_of", "forward"),
        "guardian_reverse_common_topic": ("guardian_of", "reverse"),
    }
    graph_edges = {
        (edge["from"], edge["to"], edge["rel"])
        for edge in family_graph_sample()["edges"]
    }
    shared_topic_members = {
        edge["to"]
        for edge in topics_graph_sample()["edges"]
        if edge["from"] == "topic_shared"
    }
    for case, (relation, direction) in relation_cases.items():
        row = rows_by_case[case]
        assert (row["relation"], row["relation_direction"]) == (relation, direction)
        graph_edge = (
            (row["reader"], row["owner"], relation)
            if direction == "forward"
            else (row["owner"], row["reader"], relation)
        )
        assert graph_edge in graph_edges
        assert {row["reader"], row["owner"]} <= shared_topic_members
        assert "topic_shared" in row["tags"]
        if case == "parent_forward_common_topic_secret":
            assert row["allowed"] is False
            assert row["reason"] is None
        else:
            assert row["allowed"] is True
            assert row["reason"] == "family_common_topic"

    explicit_deny = rows_by_case["spouse_forward_common_topic_explicit_deny"]
    assert explicit_deny["static_policy"] == "deny"
    assert explicit_deny["allowed"] is True

    secret = rows_by_case["parent_forward_common_topic_secret"]
    assert "secret" in secret["tags"]
    assert secret["allowed"] is False

    catalog_grant = rows_by_case["spouse_forward_common_topic_explicit_deny"]
    assert catalog_grant["catalog"] == [
        {"name": catalog_grant["key"], "tags": catalog_grant["tags"]},
    ]

    candidate = rows_by_case["transaction_candidate_hidden_from_related_reader"]
    assert candidate["reader"] != candidate["owner"]
    assert candidate["relation"] == "spouse_of"
    assert candidate["key"].startswith("history:")
    assert candidate["visibility"] == {
        "memory_get": False,
        "history": False,
        "index": False,
    }


def load_tests(
    _loader: unittest.TestLoader,
    _tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
