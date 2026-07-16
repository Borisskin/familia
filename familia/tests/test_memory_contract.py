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
        "phase": "plan",
        "dry_run": True,
        "migration_kind": "legacy_upgrade",
        "source_contract_version": "1.0.0",
        "target_contract_version": "2.0.0",
        "status": "planned",
        "rows": [{"row_id": "1" * 64, "outcome": "planned"}],
        "exit_code": 2,
    }


def _apply_report(*, migration_kind: str = "legacy_upgrade") -> dict[str, Any]:
    source_version = "2.0.0" if migration_kind == "current_verify" else "1.0.0"
    rows = [] if migration_kind == "current_verify" else [
        {"row_id": "2" * 64, "outcome": "imported"}
    ]
    return {
        "schema_version": "2.0.0",
        "phase": "apply",
        "dry_run": False,
        "migration_kind": migration_kind,
        "source_contract_version": source_version,
        "target_contract_version": "2.0.0",
        "status": "complete",
        "rows": rows,
        "checks": {
            "flat_files_empty": True,
            "soul_verified": True,
            "registry_verified": True,
            "memx_verified": True,
            "receipts_verified": True,
            "target_invariants_verified": True,
            "no_unfinished_rows": True,
            "unknown_content_discarded": True,
        },
        "exit_code": 0,
    }


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


def test_contract_records_task_zero_storage_archive_and_outcome_rules() -> None:
    contract = _canonical_contract()

    assert contract["authority"]["final_writer"] == "PrincipalMemoryIngestor"
    assert contract["storage"]["atomic_fact_key"] == "private:<principal>:memory:<fact_id>"
    assert contract["storage"]["transaction_candidate_key"] == (
        "private:<principal>:history:<source_id>"
    )
    assert contract["storage"]["fact_topics"] == {
        "assigned_by": "server",
        "homogeneous_reader_set": True,
    }
    assert contract["archive"]["archive_source"]["immutable_fields"] == [
        "source_kind",
        "session_key",
        "channel",
        "chat_id",
        "private_mode_proof",
        "session_generation_id",
        "message_seq_start",
        "message_seq_end",
        "trigger_reason",
        "algorithm_version",
    ]
    assert contract["archive"]["range_identity"]["range_id_fields"] == [
        "source_kind",
        "session_key",
        "session_generation_id",
        "message_seq_start",
        "message_seq_end",
    ]
    assert contract["archive"]["range_identity"]["source_id_fields"] == [
        "range_id",
        "principal",
    ]
    assert contract["archive"]["fingerprint"]["known_owner_only"] is True
    assert contract["archive"]["fingerprint"]["forbidden_for_discarded_unknown"] is True

    outcome_categories = (
        "operation",
        "part",
        "private_source",
        "unknown_private_source",
        "session_range",
        "legacy_row",
        "migration_command",
    )
    assert {name: contract["outcomes"][name] for name in outcome_categories} == {
        "operation": {
            "values": [
                "applied",
                "duplicate",
                "awaiting_owner",
                "denied_invalid",
                "retryable_failure",
            ],
            "terminal": ["applied", "duplicate", "awaiting_owner", "denied_invalid"],
        },
        "part": {
            "values": [
                "complete",
                "complete_with_denials",
                "retryable_failure",
                "integrity_conflict",
            ],
            "terminal": ["complete", "complete_with_denials"],
        },
        "private_source": {
            "values": ["complete", "duplicate", "retryable_failure", "integrity_conflict"],
            "terminal": ["complete", "duplicate"],
        },
        "unknown_private_source": {
            "values": ["discarded_unknown"],
            "terminal": ["discarded_unknown"],
        },
        "session_range": {
            "values": [
                "complete",
                "retryable_failure",
                "integrity_conflict",
                "unsupported_topology",
            ],
            "terminal": ["complete"],
        },
        "legacy_row": {
            "values": [
                "imported",
                "duplicate",
                "awaiting_owner",
                "discarded_unknown",
                "retryable_failure",
            ],
            "terminal": ["imported", "duplicate", "awaiting_owner", "discarded_unknown"],
        },
        "migration_command": {
            "plan": {
                "values": ["planned", "blocked_needs_review"],
                "terminal": ["planned", "blocked_needs_review"],
            },
            "apply": {
                "values": ["complete", "partial", "failed"],
                "terminal": ["complete", "partial", "failed"],
            },
        },
    }
    assert contract["migration"]["exit_codes"] == {
        "plan": {"planned": 2, "blocked_needs_review": 2},
        "apply": {"complete": 0, "partial": 2, "failed": 1},
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
    assert access["common_topic_ordinary_memory_overrides"] == [
        "secret",
        "explicit_deny",
        "no_matching_static_rule",
    ]
    assert access["legacy_untagged_memory_available_to_family"] is True
    assert access["transaction_candidate_internal_only"] is True

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


def test_contract_defines_exact_semantics_for_special_outcomes() -> None:
    semantics = _canonical_contract()["outcomes"]["semantics"]

    assert semantics["discarded_unknown"] == {
        "only_for": "unknown_private_source",
        "happens_before": ["llm", "fingerprint"],
        "content_or_derived_copy_persisted": False,
        "receipt_payload": ["server_coordinates", "reason_counter"],
        "terminal": True,
    }
    assert semantics["awaiting_owner"] == {
        "only_for": "deterministic_conflict_known_owner",
        "existing_value_replaced": False,
        "pending_candidate_visibility": "owner_only_secret",
        "excluded_from": ["memory_get_for_others", "indexes", "automatic_context"],
        "notification_contains_fact_text": False,
        "blocks_activation": False,
        "terminal": True,
    }
    assert semantics["denied_invalid"] == {
        "only_for": "single_deterministically_invalid_operation",
        "allowed_for_whole_model_response": False,
        "terminal": True,
    }
    assert semantics["unusable_whole_model_response"] == {
        "outcome": "retryable_failure",
        "terminal": False,
        "grants_coverage": False,
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


def test_migration_schema_accepts_plan_legacy_upgrade_and_current_verify_apply() -> None:
    validator = _validator(MIGRATION_SCHEMA_PATH)
    _assert_valid(validator, _plan_report())
    _assert_valid(validator, _apply_report())
    _assert_valid(validator, _apply_report(migration_kind="current_verify"))

    legacy_unversioned = _plan_report()
    legacy_unversioned["source_contract_version"] = "legacy-unversioned"
    _assert_valid(validator, legacy_unversioned)


def test_current_verify_requires_distinct_machine_checks() -> None:
    validator = _validator(MIGRATION_SCHEMA_PATH)
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

    report = _apply_report(migration_kind="current_verify")
    report["checks"] = {name: True for name in required_checks}
    valid_errors = list(validator.iter_errors(report))

    generic_report = _apply_report(migration_kind="current_verify")
    generic_report["checks"] = {
        "flat_files_empty": True,
        "soul_verified": True,
        "target_state_verified": True,
        "no_unfinished_rows": True,
        "unknown_content_discarded": True,
    }
    generic_report_accepted = not list(validator.iter_errors(generic_report))

    missing_checks_accepted = []
    for missing in sorted(required_checks):
        incomplete = deepcopy(report)
        del incomplete["checks"][missing]
        if not list(validator.iter_errors(incomplete)):
            missing_checks_accepted.append(missing)

    assert not valid_errors, "\n".join(error.message for error in valid_errors)
    assert generic_report_accepted is False
    assert missing_checks_accepted == []


def test_migration_schema_rejects_false_success_versions_and_inconsistent_outcomes() -> None:
    validator = _validator(MIGRATION_SCHEMA_PATH)

    dry_run_success = _plan_report()
    dry_run_success["exit_code"] = 0
    _assert_invalid(validator, dry_run_success)

    dry_run_failure = _plan_report()
    dry_run_failure["exit_code"] = 1
    assert list(validator.iter_errors(dry_run_failure)), (
        "plan + planned + exit_code=1 was accepted"
    )

    missing_source = _apply_report()
    del missing_source["source_contract_version"]
    _assert_invalid(validator, missing_source)

    future_source = _apply_report()
    future_source["source_contract_version"] = "3.0.0"
    _assert_invalid(validator, future_source)

    wrong_target = _apply_report()
    wrong_target["target_contract_version"] = "1.0.0"
    _assert_invalid(validator, wrong_target)

    partial_zero = _apply_report()
    partial_zero["status"] = "partial"
    _assert_invalid(validator, partial_zero)

    unchecked_complete = _apply_report()
    unchecked_complete["checks"]["flat_files_empty"] = False
    _assert_invalid(validator, unchecked_complete)

    quarantine = _apply_report()
    quarantine["rows"][0]["outcome"] = "quarantine_needs_review"
    _assert_invalid(validator, quarantine)

    current_verify_import = _apply_report(migration_kind="current_verify")
    current_verify_import["rows"] = [{"row_id": "3" * 64, "outcome": "imported"}]
    _assert_invalid(validator, current_verify_import)


def test_migration_schema_rejects_inconsistent_non_complete_statuses() -> None:
    validator = _validator(MIGRATION_SCHEMA_PATH)

    partial_without_unfinished_row = _apply_report()
    partial_without_unfinished_row.update(status="partial", exit_code=2)

    partial_claims_no_unfinished_rows = _apply_report()
    partial_claims_no_unfinished_rows.update(status="partial", exit_code=2)
    partial_claims_no_unfinished_rows["rows"] = [
        {"row_id": "4" * 64, "outcome": "retryable_failure"}
    ]

    failed_without_failure = _apply_report()
    failed_without_failure.update(status="failed", exit_code=1)

    accepted = [
        name
        for name, report in (
            ("partial_without_unfinished_row", partial_without_unfinished_row),
            ("partial_claims_no_unfinished_rows", partial_claims_no_unfinished_rows),
            ("failed_without_failure", failed_without_failure),
        )
        if not list(validator.iter_errors(report))
    ]
    assert accepted == [], f"schema accepted inconsistent reports: {accepted}"


def test_migration_schema_separates_plan_and_apply_status_precedence() -> None:
    validator = _validator(MIGRATION_SCHEMA_PATH)

    blocked_plan = _plan_report()
    blocked_plan["status"] = "blocked_needs_review"
    blocked_plan["rows"] = [
        {"row_id": "5" * 64, "outcome": "blocked_needs_review"}
    ]

    partial_retryable = _apply_report()
    partial_retryable.update(status="partial", exit_code=2)
    partial_retryable["rows"] = [
        {"row_id": "6" * 64, "outcome": "retryable_failure"}
    ]
    partial_retryable["checks"]["no_unfinished_rows"] = False

    failed_substantive_check = _apply_report()
    failed_substantive_check.update(status="failed", exit_code=1)
    failed_substantive_check["checks"]["memx_verified"] = False

    failed_retryable_substantive_check = deepcopy(partial_retryable)
    failed_retryable_substantive_check.update(status="failed", exit_code=1)
    failed_retryable_substantive_check["checks"]["memx_verified"] = False

    for report in (
        blocked_plan,
        partial_retryable,
        failed_substantive_check,
        failed_retryable_substantive_check,
    ):
        _assert_valid(validator, report)

    needs_review_complete = _apply_report()
    needs_review_complete.update(status="needs_review", exit_code=2)

    needs_review_retryable = deepcopy(partial_retryable)
    needs_review_retryable["status"] = "needs_review"

    failed_retryable_only = deepcopy(partial_retryable)
    failed_retryable_only.update(status="failed", exit_code=1)

    partial_with_substantive_failure = deepcopy(partial_retryable)
    partial_with_substantive_failure["checks"]["memx_verified"] = False

    accepted = [
        name
        for name, report in (
            ("needs_review_complete", needs_review_complete),
            ("needs_review_retryable", needs_review_retryable),
            ("failed_retryable_only", failed_retryable_only),
            ("partial_with_substantive_failure", partial_with_substantive_failure),
        )
        if not list(validator.iter_errors(report))
    ]
    assert accepted == [], f"schema accepted forbidden apply reports: {accepted}"


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
        assert row["allowed"] is True
        assert row["reason"] == "family_common_topic"

    explicit_deny = rows_by_case["spouse_forward_common_topic_explicit_deny"]
    assert explicit_deny["static_policy"] == "deny"
    assert explicit_deny["allowed"] is True

    secret = rows_by_case["parent_forward_common_topic_secret"]
    assert "secret" in secret["tags"]
    assert secret["allowed"] is True

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
