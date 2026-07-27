from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
        assert "legacy_owner" not in plan

        expected_flat_actions = {
            ("USER.md", "user_profile"),
            ("MEMORY.md", "memory"),
            ("memory/MEMORY.md", "memory"),
        }
        flat_actions = [
            action
            for action in plan["actions"]
            if action.get("phase") == "files"
        ]
        assert len(flat_actions) == 3
        assert {
            (action.get("source"), action.get("component"))
            for action in flat_actions
        } == expected_flat_actions
        for action in flat_actions:
            assert action.get("destination") is None
            assert action.get("disposition") == "erase_without_read"
            assert {
                "source_sha256",
                "source_bytes",
                "digest",
                "checksum",
                "raw",
                "jaccard",
                "common_significant_tokens",
            }.isdisjoint(action)

        history_actions = [
            action for action in plan["actions"] if action.get("phase") != "files"
        ]
        assert history_actions
        assert all(action.get("phase") == "history" for action in history_actions)
        assert all(
            action.get("source") == "memory/history.jsonl"
            for action in history_actions
        )
        assert not any(
            action.get("source") == "HEARTBEAT.md"
            or action.get("component") == "heartbeat"
            for action in plan["actions"]
        )
        assert {
            action.get("actor") for action in history_actions if action.get("actor")
        } <= known_principals
        assert {
            action.get("disposition") for action in history_actions
        } <= {"llm_required", "discarded_unknown"}
        assert any(
            action.get("disposition") == "discarded_unknown"
            for action in history_actions
        )

    @staticmethod
    def _task_4_apply_connection_point(
        *, snapshot: object, plan: dict[str, object], expected_end_state: dict[str, object]
    ) -> object:
        from familia.memory_migration import apply_legacy_transition_plan

        class RecordingIngestor:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def ingest(self, **kwargs: object) -> str:
                self.calls.append(dict(kwargs))
                server_principal = kwargs["server_principal"]
                operation = kwargs["operation"]
                assert isinstance(server_principal, str)
                assert isinstance(operation, dict)
                fact_id = operation["fact_id"]
                assert isinstance(fact_id, str)
                return (
                    "committed: Stored at "
                    f"'private:{server_principal}:memory:{fact_id}'"
                )

        consolidated_values: dict[str, str] = {}

        async def consolidate_history(
            actor: str,
            records: list[dict[str, object]],
            existing_memory: str,
        ) -> str:
            del existing_memory
            value = (
                f"consolidated:{actor}:"
                + ",".join(str(record["cursor"]) for record in records)
            )
            consolidated_values[actor] = value
            return value

        approved_history_actions = [
            action
            for action in plan["actions"]
            if action.get("phase") == "history"
            and action.get("source") == "memory/history.jsonl"
            and action.get("disposition") == "llm_required"
            and isinstance(action.get("actor"), str)
        ]
        assert approved_history_actions

        flat_paths = {
            (snapshot.workspace / "USER.md").resolve(),
            (snapshot.workspace / "MEMORY.md").resolve(),
            (snapshot.workspace / "memory" / "MEMORY.md").resolve(),
        }
        history_path = (snapshot.workspace / "memory" / "history.jsonl").resolve()
        soul_path = (snapshot.workspace / "SOUL.md").resolve()
        history_before = history_path.read_bytes()
        soul_before = soul_path.read_bytes()
        assert soul_before == expected_end_state["soul_bytes"]

        original_read_bytes = Path.read_bytes
        history_reads: list[Path] = []

        def read_history_only(path: Path) -> bytes:
            resolved = path.resolve()
            if resolved in flat_paths:
                raise AssertionError(
                    f"flat memory source must not be read during apply: {resolved}"
                )
            if resolved == history_path:
                history_reads.append(resolved)
            return original_read_bytes(path)

        ingestor = RecordingIngestor()
        with patch.object(Path, "read_bytes", read_history_only):
            apply_result = asyncio.run(
                apply_legacy_transition_plan(
                    plan=plan,
                    workspace=snapshot.workspace,
                    get_value=snapshot.existing_memory.get,
                    ingestor=ingestor,
                    consolidate_history=consolidate_history,
                )
            )

        assert history_reads
        assert history_path.read_bytes() == history_before
        assert soul_path.read_bytes() == soul_before
        assert all(path.is_file() and path.read_bytes() == b"" for path in flat_paths)

        approved_by_actor = {
            action["actor"]: action for action in approved_history_actions
        }
        assert len(approved_by_actor) == len(approved_history_actions)
        assert len(ingestor.calls) == len(approved_history_actions)
        assert {
            call.get("server_principal") for call in ingestor.calls
        } == set(approved_by_actor)

        discarded_actors = {
            action.get("source_actor")
            for action in plan["actions"]
            if action.get("disposition") == "discarded_unknown"
            and isinstance(action.get("source_actor"), str)
        }
        assert discarded_actors.isdisjoint(
            call.get("server_principal") for call in ingestor.calls
        )

        for call in ingestor.calls:
            assert set(call) == {"server_principal", "server_topic", "operation"}
            actor = call["server_principal"]
            assert isinstance(actor, str)
            assert call["server_topic"] is None
            operation = call["operation"]
            assert isinstance(operation, dict)
            assert set(operation) == {"kind", "fact_id", "value"}
            assert operation["kind"] == "memory"
            fact_id = operation["fact_id"]
            assert isinstance(fact_id, str) and fact_id
            planned_fact_id = approved_by_actor[actor].get("fact_id")
            assert isinstance(planned_fact_id, str) and planned_fact_id
            assert fact_id == planned_fact_id
            assert actor in consolidated_values
            assert operation["value"] == consolidated_values[actor]

        assert isinstance(apply_result, dict)
        assert apply_result.get("status") == "complete"
        written_keys = apply_result.get("written_keys", [])
        assert isinstance(written_keys, list)
        expected_written_keys = {
            f"private:{actor}:memory:{action['fact_id']}"
            for actor, action in approved_by_actor.items()
        }
        assert set(written_keys) == expected_written_keys
        return apply_result

    @staticmethod
    def _task_5_repeat_connection_point(
        *,
        snapshot: object,
        plan: dict[str, object],
    ) -> dict[str, object]:
        from familia.memory_migration import apply_legacy_transition_plan

        approved_actions = [
            action
            for action in plan["actions"]
            if action.get("disposition") == "llm_required"
        ]
        expected_by_actor = {
            action["actor"]: action["destination"]
            for action in approved_actions
        }
        assert len(expected_by_actor) == len(approved_actions)
        assert len(expected_by_actor) >= 2
        expected_actors = [
            action["actor"] for action in approved_actions
        ]
        expected_action_keys = [
            action["destination"] for action in approved_actions
        ]
        expected_keys = set(expected_by_actor.values())
        assert all(
            key == f"private:{actor}:memory:legacy-history"
            for actor, key in expected_by_actor.items()
        )
        discarded_actors = {
            action.get("source_actor")
            for action in plan["actions"]
            if action.get("disposition") == "discarded_unknown"
            and isinstance(action.get("source_actor"), str)
        }

        class ExactKeyMemory:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}
                self.get_calls: list[str] = []
                self.ingest_calls: list[dict[str, object]] = []
                self.fail_actor = expected_actors[1]
                self.failed_once = False

            def get(self, key: str) -> str | None:
                self.get_calls.append(key)
                assert key in expected_keys
                return self.values.get(key)

            async def ingest(self, **kwargs: object) -> str:
                call = dict(kwargs)
                operation = call.get("operation")
                assert set(call) == {
                    "server_principal",
                    "server_topic",
                    "operation",
                }
                assert isinstance(operation, dict)
                call["operation"] = dict(operation)
                self.ingest_calls.append(call)

                actor = call["server_principal"]
                assert isinstance(actor, str)
                assert actor in expected_by_actor
                assert actor not in discarded_actors
                assert call["server_topic"] is None
                assert set(operation) == {"kind", "fact_id", "value"}
                assert operation["kind"] == "memory"
                assert operation["fact_id"] == "legacy-history"
                assert isinstance(operation["value"], str)
                key = f"private:{actor}:memory:{operation['fact_id']}"
                assert key == expected_by_actor[actor]

                if actor == self.fail_actor and not self.failed_once:
                    self.failed_once = True
                    return "retryable_failure: synthetic one-shot failure"
                self.values[key] = operation["value"]
                return f"committed: Stored at '{key}'"

        memory = ExactKeyMemory()
        consolidation_counts: dict[str, int] = {}

        async def consolidate_history(
            actor: str,
            records: list[dict[str, object]],
            existing_memory: str,
        ) -> str:
            assert actor in expected_by_actor
            assert records
            key = expected_by_actor[actor]
            assert existing_memory == memory.values.get(key, "")
            consolidation_counts[actor] = consolidation_counts.get(actor, 0) + 1
            cursors = ",".join(str(record["cursor"]) for record in records)
            return (
                f"consolidated:{actor}:"
                f"attempt:{consolidation_counts[actor]}:{cursors}"
            )

        def apply_once() -> dict[str, object]:
            return asyncio.run(
                apply_legacy_transition_plan(
                    plan=plan,
                    workspace=snapshot.workspace,
                    get_value=memory.get,
                    ingestor=memory,
                    consolidate_history=consolidate_history,
                )
            )

        first = apply_once()
        first_key = expected_by_actor[expected_actors[0]]
        assert first["status"] == "partial"
        assert first["applied_actions"] == 1
        assert first["written_keys"] == [first_key]
        assert first["failed_actors"] == [expected_actors[1]]
        assert set(memory.values) == {first_key}
        assert [
            call["server_principal"]
            for call in memory.ingest_calls[:2]
        ] == expected_actors[:2]

        second = apply_once()
        assert second["status"] == "complete"
        assert set(second["written_keys"]) == expected_keys
        assert set(memory.values) == expected_keys
        keys_after_second = set(memory.values)
        values_after_second = dict(memory.values)

        third = apply_once()
        assert third["status"] == "complete"
        assert set(third["written_keys"]) == expected_keys
        assert set(memory.values) == keys_after_second == expected_keys
        assert all(
            memory.values[key] != values_after_second[key]
            for key in expected_keys
        )

        assert memory.get_calls == (
            expected_action_keys[:2]
            + expected_action_keys
            + expected_action_keys
        )
        assert {
            call["server_principal"] for call in memory.ingest_calls
        } == set(expected_actors)
        assert all(
            call["server_topic"] is None
            and call["operation"]["kind"] == "memory"
            and call["operation"]["fact_id"] == "legacy-history"
            for call in memory.ingest_calls
        )
        assert discarded_actors.isdisjoint(
            call["server_principal"] for call in memory.ingest_calls
        )
        assert set(memory.values) == {
            f"private:{actor}:memory:legacy-history"
            for actor in expected_actors
        }

        return {
            "partial": first,
            "retry": second,
            "repeat": third,
            "physical_keys": sorted(memory.values),
        }

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

        flat_paths = {
            (snapshot.workspace / "USER.md").resolve(),
            (snapshot.workspace / "MEMORY.md").resolve(),
            (snapshot.workspace / "memory" / "MEMORY.md").resolve(),
        }
        assert len(flat_paths) == 3
        assert all(path.is_file() for path in flat_paths)
        history_path = (snapshot.workspace / "memory" / "history.jsonl").resolve()
        original_read_bytes = Path.read_bytes
        history_reads: list[Path] = []

        def read_history_only(path: Path) -> bytes:
            resolved = path.resolve()
            if resolved in flat_paths:
                raise AssertionError(
                    f"flat memory source must not be read during transition: {resolved}"
                )
            if resolved == history_path:
                history_reads.append(resolved)
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", read_history_only):
            plan = build_legacy_transition_plan(
                workspace=snapshot.workspace,
                known_actors=known_principals,
            )
        assert history_reads
        self._assert_contract_2_plan(plan, known_principals)

        apply_result = self._task_4_apply_connection_point(
            snapshot=snapshot,
            plan=plan,
            expected_end_state=expected_end_state,
        )
        repeat_result = self._task_5_repeat_connection_point(
            snapshot=snapshot,
            plan=plan,
        )
        restore_result = self._task_7_restore_connection_point(
            snapshot=snapshot,
            apply_result=repeat_result["repeat"],
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
            "repeat": repeat_result,
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

    def test_retryable_ingestor_failure_preserves_snapshot_bytes(self) -> None:
        from familia.memory_migration import (
            apply_legacy_transition_plan,
            build_legacy_transition_plan,
        )

        class FailingFirstIngestor:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def ingest(self, **kwargs: object) -> str:
                self.calls.append(dict(kwargs))
                if len(self.calls) == 1:
                    return "retryable_failure: synthetic write failure"
                server_principal = kwargs["server_principal"]
                operation = kwargs["operation"]
                assert isinstance(server_principal, str)
                assert isinstance(operation, dict)
                fact_id = operation["fact_id"]
                assert isinstance(fact_id, str)
                return (
                    "committed: Stored at "
                    f"'private:{server_principal}:memory:{fact_id}'"
                )

        async def consolidate_history(
            actor: str,
            records: list[dict[str, object]],
            existing_memory: str,
        ) -> str:
            del existing_memory
            return (
                f"consolidated:{actor}:"
                + ",".join(str(record["cursor"]) for record in records)
            )

        with tempfile.TemporaryDirectory() as directory:
            snapshot = write_synthetic_snapshot(Path(directory))
            known_principals = {
                "principal_alpha",
                "principal_beta",
                "principal_gamma",
                "principal_child",
            }
            flat_paths = {
                (snapshot.workspace / "USER.md").resolve(),
                (snapshot.workspace / "MEMORY.md").resolve(),
                (snapshot.workspace / "memory" / "MEMORY.md").resolve(),
            }
            history_path = (
                snapshot.workspace / "memory" / "history.jsonl"
            ).resolve()
            soul_path = (snapshot.workspace / "SOUL.md").resolve()
            protected_paths = flat_paths | {history_path, soul_path}
            bytes_before = {
                path: path.read_bytes()
                for path in protected_paths
            }

            original_read_bytes = Path.read_bytes
            history_reads: list[Path] = []

            def read_history_only(path: Path) -> bytes:
                resolved = path.resolve()
                if resolved in flat_paths:
                    raise AssertionError(
                        f"flat memory source must not be read: {resolved}"
                    )
                if resolved == history_path:
                    history_reads.append(resolved)
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", read_history_only):
                plan = build_legacy_transition_plan(
                    workspace=snapshot.workspace,
                    known_actors=known_principals,
                )
            assert history_reads
            self._assert_contract_2_plan(plan, known_principals)

            history_reads.clear()
            ingestor = FailingFirstIngestor()
            with patch.object(Path, "read_bytes", read_history_only):
                apply_result = asyncio.run(
                    apply_legacy_transition_plan(
                        plan=plan,
                        workspace=snapshot.workspace,
                        get_value=snapshot.existing_memory.get,
                        ingestor=ingestor,
                        consolidate_history=consolidate_history,
                    )
                )

            assert history_reads
            assert len(ingestor.calls) == 1
            assert isinstance(apply_result, dict)
            assert apply_result.get("status") == "failed"
            assert apply_result.get("written_keys") == []
            assert {
                path: path.read_bytes()
                for path in protected_paths
            } == bytes_before

    def test_first_commit_then_retryable_failure_is_partial_with_written_key(
        self,
    ) -> None:
        from familia.memory_migration import (
            apply_legacy_transition_plan,
            build_legacy_transition_plan,
        )

        class CommitThenFailIngestor:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def ingest(self, **kwargs: object) -> str:
                self.calls.append(dict(kwargs))
                if len(self.calls) == 1:
                    actor = kwargs["server_principal"]
                    operation = kwargs["operation"]
                    assert isinstance(actor, str)
                    assert isinstance(operation, dict)
                    return (
                        f"committed:private:{actor}:memory:"
                        f"{operation['fact_id']}"
                    )
                return "retryable_failure: synthetic write failure"

        async def consolidate_history(
            actor: str,
            records: list[dict[str, object]],
            existing_memory: str,
        ) -> str:
            del existing_memory
            return (
                f"consolidated:{actor}:"
                + ",".join(str(record["cursor"]) for record in records)
            )

        with tempfile.TemporaryDirectory() as directory:
            snapshot = write_synthetic_snapshot(Path(directory))
            known_principals = {
                "principal_alpha",
                "principal_beta",
                "principal_gamma",
                "principal_child",
            }
            plan = build_legacy_transition_plan(
                workspace=snapshot.workspace,
                known_actors=known_principals,
            )
            required_actions = [
                action
                for action in plan["actions"]
                if action.get("disposition") == "llm_required"
            ]
            assert len(required_actions) >= 2
            first_written_key = required_actions[0]["destination"]

            flat_paths = {
                (snapshot.workspace / "USER.md").resolve(),
                (snapshot.workspace / "MEMORY.md").resolve(),
                (snapshot.workspace / "memory" / "MEMORY.md").resolve(),
            }
            flat_before = {path: path.read_bytes() for path in flat_paths}
            ingestor = CommitThenFailIngestor()
            apply_result = asyncio.run(
                apply_legacy_transition_plan(
                    plan=plan,
                    workspace=snapshot.workspace,
                    get_value=snapshot.existing_memory.get,
                    ingestor=ingestor,
                    consolidate_history=consolidate_history,
                )
            )

            assert len(ingestor.calls) == 2
            assert apply_result["status"] == "partial"
            assert apply_result["applied_actions"] == 1
            assert apply_result["written_keys"] == [first_written_key]
            assert {path: path.read_bytes() for path in flat_paths} == flat_before

    def test_cleanup_failure_after_all_history_commits_is_partial(self) -> None:
        from familia.memory_migration import (
            apply_legacy_transition_plan,
            build_legacy_transition_plan,
        )

        class CommittingIngestor:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def ingest(self, **kwargs: object) -> str:
                self.calls.append(dict(kwargs))
                actor = kwargs["server_principal"]
                operation = kwargs["operation"]
                assert isinstance(actor, str)
                assert isinstance(operation, dict)
                return (
                    f"committed:private:{actor}:memory:"
                    f"{operation['fact_id']}"
                )

        async def consolidate_history(
            actor: str,
            records: list[dict[str, object]],
            existing_memory: str,
        ) -> str:
            del existing_memory
            return (
                f"consolidated:{actor}:"
                + ",".join(str(record["cursor"]) for record in records)
            )

        with tempfile.TemporaryDirectory() as directory:
            snapshot = write_synthetic_snapshot(Path(directory))
            known_principals = {
                "principal_alpha",
                "principal_beta",
                "principal_gamma",
                "principal_child",
            }
            plan = build_legacy_transition_plan(
                workspace=snapshot.workspace,
                known_actors=known_principals,
            )
            required_actions = [
                action
                for action in plan["actions"]
                if action.get("disposition") == "llm_required"
            ]
            assert required_actions
            expected_written_keys = sorted(
                action["destination"] for action in required_actions
            )

            flat_paths = {
                (snapshot.workspace / "USER.md").resolve(),
                (snapshot.workspace / "MEMORY.md").resolve(),
                (snapshot.workspace / "memory" / "MEMORY.md").resolve(),
            }
            flat_before = {path: path.read_bytes() for path in flat_paths}
            history_path = snapshot.workspace / "memory" / "history.jsonl"
            history_before = history_path.read_bytes()
            ingestor = CommittingIngestor()
            with patch(
                "familia.memory_migration._write_private_atomic",
                side_effect=OSError("synthetic cleanup failure"),
            ):
                apply_result = asyncio.run(
                    apply_legacy_transition_plan(
                        plan=plan,
                        workspace=snapshot.workspace,
                        get_value=snapshot.existing_memory.get,
                        ingestor=ingestor,
                        consolidate_history=consolidate_history,
                    )
                )

            assert len(ingestor.calls) == len(required_actions)
            assert apply_result["status"] == "partial"
            assert apply_result["applied_actions"] == len(required_actions)
            assert apply_result["written_keys"] == expected_written_keys
            assert apply_result["dream_cursor_updated"] is False
            assert {path: path.read_bytes() for path in flat_paths} == flat_before
            assert history_path.read_bytes() == history_before
            assert not (snapshot.workspace / "memory" / ".dream_cursor").exists()


if __name__ == "__main__":
    unittest.main(verbosity=2)
