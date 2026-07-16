from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from memory_contract_samples import (
    EXPECTED_MEMORY_READ_DECISIONS,
    write_synthetic_snapshot,
)


class SyntheticSnapshotMemoryContractBarrier(unittest.TestCase):
    @staticmethod
    def _assert_contract_2_plan(
        plan: dict[str, object], known_principals: set[str]
    ) -> None:
        actual_schema = plan.get("schema_version")
        assert actual_schema == "2.0.0", (
            f"legacy transition schema version is {actual_schema!r}, expected '2.0.0'"
        )
        assert plan["migration_kind"] == "legacy_upgrade"
        assert plan["source_contract_version"] in {"1.0.0", "legacy-unversioned"}
        assert plan["target_contract_version"] == "2.0.0"
        assert plan.get("legacy_owner") is None

        flat_components = {"user_profile", "memory"}
        flat_actions = [
            action
            for action in plan["actions"]
            if action.get("phase") == "files"
            and action.get("component") in flat_components
        ]
        assert flat_actions
        assert all(action.get("destination") is None for action in flat_actions)
        assert all(
            action.get("disposition") == "erase_without_read" for action in flat_actions
        )

        history_actions = [
            action for action in plan["actions"] if action.get("component") == "history"
        ]
        assert {
            action.get("actor") for action in history_actions if action.get("actor")
        } <= known_principals
        assert not any(
            action.get("disposition") == "quarantine_needs_review"
            for action in history_actions
        )
        assert any(
            action.get("disposition") == "discarded_unknown"
            for action in history_actions
        )

    @staticmethod
    def _task_4_apply_connection_point(
        *, snapshot: object, plan: dict[str, object], expected_end_state: dict[str, object]
    ) -> object:
        raise NotImplementedError("task 4 connection point: apply is not implemented")

    @staticmethod
    def _task_7_restore_connection_point(
        *,
        snapshot: object,
        apply_result: object,
        expected_end_state: dict[str, object],
    ) -> object:
        raise NotImplementedError("task 7 connection point: restore is not implemented")

    @staticmethod
    def _current_verify_connection_point(
        *,
        snapshot: object,
        restore_result: object,
        expected_end_state: dict[str, object],
    ) -> dict[str, object]:
        raise NotImplementedError(
            "current_verify connection point is not implemented"
        )

    @staticmethod
    def _assert_current_end_state(
        actual_end_state: dict[str, object], expected_end_state: dict[str, object]
    ) -> None:
        for field in (
            "flat_paths_zero_bytes",
            "soul_bytes",
            "pre_switch_side_effects",
            "repeat_restore_duplicate_count",
            "unknown_content_absent_from",
            "conflict_owner_only",
            "conflict_notification_contains_fact_text",
            "decision_cases",
        ):
            assert actual_end_state[field] == expected_end_state[field]

    def _run_staged_transition(
        self, *, snapshot: object, known_principals: set[str]
    ) -> dict[str, object]:
        from familia.memory_migration import build_legacy_transition_plan

        expected_end_state = {
            "flat_paths_zero_bytes": snapshot.expected_end_state[
                "flat_paths_zero_bytes"
            ],
            "soul_bytes": snapshot.expected_end_state["soul_bytes"],
            "pre_switch_side_effects": snapshot.expected_end_state[
                "pre_switch_side_effects"
            ],
            "repeat_restore_duplicate_count": snapshot.expected_end_state[
                "repeat_restore_duplicate_count"
            ],
            "unknown_content_absent_from": snapshot.expected_end_state[
                "unknown_content_absent_from"
            ],
            "conflict_owner_only": snapshot.expected_end_state[
                "conflict_owner_only"
            ],
            "conflict_notification_contains_fact_text": snapshot.expected_end_state[
                "conflict_notification_contains_fact_text"
            ],
            "decision_cases": snapshot.expected_end_state["decision_cases"],
        }

        assert snapshot.existing_memory["shared:family.graph"]
        assert snapshot.existing_memory["shared:topics.graph"]
        assert snapshot.deterministic_conflict["outcome"] == "awaiting_owner"
        assert expected_end_state["decision_cases"] == [
            row["case"] for row in EXPECTED_MEMORY_READ_DECISIONS
        ]

        plan = build_legacy_transition_plan(
            workspace=snapshot.workspace,
            known_actors=known_principals,
            get_value=snapshot.existing_memory.get,
            legacy_owner="principal_alpha",
        )
        self._assert_contract_2_plan(plan, known_principals)

        apply_result = self._task_4_apply_connection_point(
            snapshot=snapshot,
            plan=plan,
            expected_end_state=expected_end_state,
        )
        restore_result = self._task_7_restore_connection_point(
            snapshot=snapshot,
            apply_result=apply_result,
            expected_end_state=expected_end_state,
        )
        actual_end_state = self._current_verify_connection_point(
            snapshot=snapshot,
            restore_result=restore_result,
            expected_end_state=expected_end_state,
        )
        self._assert_current_end_state(actual_end_state, expected_end_state)

        return {
            "plan": plan,
            "apply": apply_result,
            "restore": restore_result,
            "current_verify": actual_end_state,
        }

    def test_synthetic_snapshot_memory_contract_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = write_synthetic_snapshot(Path(directory))
            known_principals = {
                "principal_alpha",
                "principal_beta",
                "principal_gamma",
                "principal_child",
            }
            self._run_staged_transition(
                snapshot=snapshot,
                known_principals=known_principals,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
