from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pytest

from memory_contract_samples import (
    EXPECTED_MEMORY_READ_DECISIONS,
    synthetic_source_digest,
    write_synthetic_snapshot,
)


@pytest.mark.asyncio
async def test_real_legacy_row_reaches_catalog_and_fresh_owner_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from test_memory_recall_end_to_end import _MemxTransport
    from test_memory_index_consistency import InProcessRedis

    import main as memx_main
    import store
    import validate_api

    from familia import principals as principals_mod
    from familia.acl import graph_io, principal_memory
    from familia.memory_migration import (
        apply_legacy_transition_plan,
        build_legacy_transition_plan,
    )
    from familia.nanobot_extension.context import FamiliaContextExtension
    from familia.principal_memory_ingestor import PrincipalMemoryIngestor
    from familia.principals import Principal, PrincipalRegistry

    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("soul", encoding="utf-8")
    (workspace / "USER.md").write_text("retired user", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("retired memory", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text(
        "retired nested memory",
        encoding="utf-8",
    )
    history_row = {
        "schema_version": 1,
        "cursor": 1,
        "timestamp": "2026-07-27 12:00",
        "actor": "owner",
        "content": "legacy row content",
        "provenance": {
            "source": "migration-e2e",
            "idempotency_key": "legacy-row-1",
        },
    }
    (memory_dir / "history.jsonl").write_text(
        json.dumps(history_row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    monkeypatch.setattr(validate_api, "supabase", None)
    monkeypatch.setitem(
        validate_api.LOCAL_ACL,
        "automatic-writer-key",
        ["private:*"],
    )
    monkeypatch.setattr(memx_main, "validate_schema", lambda *_args: None)

    async def publish(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(memx_main, "publish", publish)
    monkeypatch.setattr(
        principals_mod,
        "_registry",
        PrincipalRegistry(
            [
                Principal(
                    id="owner",
                    display_name="Owner",
                    memx_key="owner-key",
                )
            ]
        ),
    )

    def raw_value(key: str, **_kwargs):
        record = store.get_value(key)
        return None if record is None else record["value"]

    def graph_value(key: str, **_kwargs):
        raw = raw_value(key)
        return json.loads(raw) if isinstance(raw, str) else raw

    monkeypatch.setattr(graph_io, "get_raw", raw_value)
    monkeypatch.setattr(principal_memory, "get_raw", raw_value)
    monkeypatch.setattr(graph_io, "load_graph_value", graph_value)
    monkeypatch.setattr(graph_io, "resolve_admin_key", lambda: "internal-admin")

    transport = _MemxTransport(memx_main, api_key="automatic-writer-key")
    monkeypatch.setattr(
        "familia.principal_memory_ingestor.httpx.AsyncClient",
        lambda **_kwargs: transport,
    )
    ingestor = PrincipalMemoryIngestor(
        base_url="http://memx.test",
        api_key="automatic-writer-key",
    )
    plan = build_legacy_transition_plan(
        workspace=workspace,
        known_actors={"owner"},
    )

    async def consolidate_history(
        actor: str,
        records: list[dict[str, object]],
        existing_memory: str,
    ) -> str:
        assert actor == "owner"
        assert [record["cursor"] for record in records] == [1]
        assert existing_memory == ""
        return "MIGRATED_LEGACY_HISTORY_VALUE"

    report = await apply_legacy_transition_plan(
        plan=plan,
        workspace=workspace,
        get_value=raw_value,
        ingestor=ingestor,
        consolidate_history=consolidate_history,
    )

    assert report["status"] == "complete"
    assert report["written_keys"] == [
        "private:owner:memory:legacy-history",
    ]
    fact_record = store.get_value("private:owner:memory:legacy-history")
    assert fact_record is not None
    assert fact_record["value"] == "MIGRATED_LEGACY_HISTORY_VALUE"
    catalog_record = store.get_value("private:owner:value:private_index")
    assert catalog_record is not None
    assert json.loads(catalog_record["value"]) == [
        {"name": "memory:legacy-history", "tags": []},
    ]

    fresh_prompt = "\n\n".join(
        FamiliaContextExtension(workspace).build_sections(
            actor="owner",
            channel="test",
        )
    )
    assert "memory:legacy-history" in fresh_prompt


@dataclass
class SyntheticTransitionRunState:
    source_root: Path
    source_workspace: Path
    target_workspace: Path
    source_logical_payload: bytes
    family_graph: dict[str, object]
    topics_graph: dict[str, object]
    values: dict[str, str]
    source_bytes_before: tuple[tuple[str, bytes], ...]
    source_digest_before: str
    plan: dict[str, object] | None = None
    physical_keys: list[set[str]] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)
    call_results: list[str] = field(default_factory=list)
    call_stages: list[str] = field(default_factory=list)
    receipts: list[dict[str, object]] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    destinations_by_actor: dict[str, str] = field(default_factory=dict)
    discarded_actors: set[str] = field(default_factory=set)
    get_calls: list[str] = field(default_factory=list)
    consolidation_counts: dict[str, int] = field(default_factory=dict)
    fail_actor: str | None = None
    failed_once: bool = False
    stage: str = "created"
    published_state: dict[str, object] | None = None

    def configure_plan(self, plan: dict[str, object]) -> None:
        approved_actions = [
            action for action in plan["actions"]
            if action.get("disposition") == "llm_required"
        ]
        self.destinations_by_actor = {
            action["actor"]: action["destination"] for action in approved_actions
        }
        self.discarded_actors = {
            action["source_actor"] for action in plan["actions"]
            if action.get("disposition") == "discarded_unknown"
            and isinstance(action.get("source_actor"), str)
        }

    def get_value(self, key: str) -> str | None:
        self.get_calls.append(key)
        assert key in set(self.destinations_by_actor.values())
        return self.values.get(key)

    async def consolidate_history(
        self, actor: str, records: list[dict[str, object]], existing_memory: str
    ) -> str:
        assert actor in self.destinations_by_actor
        assert records
        key = self.destinations_by_actor[actor]
        assert existing_memory == self.values.get(key, "")
        self.consolidation_counts[actor] = self.consolidation_counts.get(actor, 0) + 1
        cursors = ",".join(str(record["cursor"]) for record in records)
        return f"consolidated:{actor}:attempt:{self.consolidation_counts[actor]}:{cursors}"

    async def ingest(self, **kwargs: object) -> str:
        call = dict(kwargs)
        operation = call.get("operation")
        assert set(call) == {"server_principal", "server_topic", "operation"}
        assert isinstance(operation, dict)
        operation = dict(operation)
        call["operation"] = operation
        actor = call["server_principal"]
        assert isinstance(actor, str) and actor in self.destinations_by_actor
        assert actor not in self.discarded_actors
        assert call["server_topic"] is None
        assert set(operation) == {"kind", "fact_id", "value"}
        assert operation["kind"] == "memory" and operation["fact_id"] == "legacy-history"
        assert isinstance(operation["value"], str)
        physical_key = f"private:{actor}:memory:{operation['fact_id']}"
        assert physical_key == self.destinations_by_actor[actor]

        call_index = len(self.calls)
        self.calls.append(call)
        self.call_stages.append(self.stage)
        if actor == self.fail_actor and not self.failed_once:
            self.failed_once = True
            result = "retryable_failure: synthetic one-shot failure"
        else:
            self.values[physical_key] = operation["value"]
            result = f"committed: Stored at '{physical_key}'"
            self.receipts.append({
                "kind": "synthetic_ingest_receipt", "call_index": call_index,
                "stage": self.stage, "actor": actor, "physical_key": physical_key,
                "value": operation["value"], "result": result,
            })
        self.call_results.append(result)
        return result


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _canonical_target_evidence(
    target_workspace: Path,
    values: dict[str, str],
) -> dict[str, object]:
    target_file_bytes = _file_bytes(target_workspace)
    target_digest = hashlib.sha256()
    for relative_path, content in sorted(target_file_bytes.items()):
        target_digest.update(relative_path.encode("utf-8"))
        target_digest.update(content)
    target_digest.update(json.dumps(values, sort_keys=True).encode("utf-8"))
    return {
        "target_file_bytes": target_file_bytes,
        "target_digest": target_digest.hexdigest(),
        "target_files": sorted(target_file_bytes),
        "physical_keys": sorted(values),
    }


def _actual_conflict_evidence(
    run_state: SyntheticTransitionRunState,
) -> dict[str, object]:
    history_path = run_state.target_workspace / "memory" / "history.jsonl"
    candidates: list[tuple[dict[str, object], dict[str, object]]] = []
    for raw_line in history_path.read_text(encoding="utf-8").splitlines():
        try:
            history_row = json.loads(raw_line)
            proposed = json.loads(history_row["content"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if (
            isinstance(history_row, dict)
            and isinstance(proposed, dict)
            and isinstance(history_row.get("actor"), str)
            and isinstance(history_row.get("cursor"), int)
            and isinstance(proposed.get("destination"), str)
            and isinstance(proposed.get("expected_revision"), int)
            and isinstance(proposed.get("value"), str)
            and proposed["destination"] in run_state.values
        ):
            candidates.append((history_row, proposed))
    assert len(candidates) == 1, "conflict evidence mismatch"
    history_row, proposed = candidates[0]
    existing = json.loads(run_state.values[proposed["destination"]])
    assert isinstance(existing, dict), "conflict evidence mismatch"
    assert isinstance(existing.get("revision"), int), "conflict evidence mismatch"

    history_cursor = str(history_row["cursor"])
    committed_call_indexes = [
        index
        for index, (call, result) in enumerate(
            zip(run_state.calls, run_state.call_results, strict=True)
        )
        if call["server_principal"] == history_row["actor"]
        and history_cursor
        in str(call["operation"]["value"]).rsplit(":", 1)[-1].split(",")
        and result.startswith("committed:")
    ]
    assert committed_call_indexes, "conflict evidence mismatch"
    call_index = committed_call_indexes[-1]
    outcome = (
        "awaiting_owner"
        if existing["revision"] != proposed["expected_revision"]
        else "no_conflict"
    )
    return {
        "owner": history_row["actor"],
        "destination": proposed["destination"],
        "history_cursor": history_row["cursor"],
        "existing_revision": existing["revision"],
        "expected_revision": proposed["expected_revision"],
        "call_index": call_index,
        "call_result": run_state.call_results[call_index],
        "outcome": outcome,
    }


def _evidence_sha256(evidence: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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
        *, run_state: SyntheticTransitionRunState
    ) -> dict[str, object]:
        from familia.memory_migration import apply_legacy_transition_plan

        plan = run_state.plan
        assert isinstance(plan, dict)
        approved_history_actions = [
            action
            for action in plan["actions"]
            if action.get("phase") == "history"
            and action.get("source") == "memory/history.jsonl"
            and action.get("disposition") == "llm_required"
            and isinstance(action.get("actor"), str)
        ]
        assert approved_history_actions
        approved_by_actor = {
            action["actor"]: action for action in approved_history_actions
        }
        assert len(approved_by_actor) == len(approved_history_actions)
        assert run_state.destinations_by_actor == {
            actor: action["destination"]
            for actor, action in approved_by_actor.items()
        }

        flat_paths = {
            (run_state.target_workspace / "USER.md").resolve(),
            (run_state.target_workspace / "MEMORY.md").resolve(),
            (run_state.target_workspace / "memory" / "MEMORY.md").resolve(),
        }
        history_path = (
            run_state.target_workspace / "memory" / "history.jsonl"
        ).resolve()
        soul_path = (run_state.target_workspace / "SOUL.md").resolve()
        history_before = history_path.read_bytes()
        soul_before = soul_path.read_bytes()
        assert soul_before == (
            run_state.source_workspace / "SOUL.md"
        ).read_bytes()

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

        run_state.stage = "apply"
        calls_before = len(run_state.calls)
        with patch.object(Path, "read_bytes", read_history_only):
            apply_result = asyncio.run(
                apply_legacy_transition_plan(
                    plan=plan,
                    workspace=run_state.target_workspace,
                    get_value=run_state.get_value,
                    ingestor=run_state,
                    consolidate_history=run_state.consolidate_history,
                )
            )

        assert history_reads
        assert history_path.read_bytes() == history_before
        assert soul_path.read_bytes() == soul_before
        assert all(path.is_file() and path.read_bytes() == b"" for path in flat_paths)

        apply_calls = run_state.calls[calls_before:]
        assert len(apply_calls) == len(approved_history_actions)
        assert {
            call.get("server_principal") for call in apply_calls
        } == set(approved_by_actor)
        assert run_state.discarded_actors.isdisjoint(
            call.get("server_principal") for call in apply_calls
        )

        for call in apply_calls:
            actor = call["server_principal"]
            assert isinstance(actor, str)
            operation = call["operation"]
            assert isinstance(operation, dict)
            fact_id = operation["fact_id"]
            assert isinstance(fact_id, str) and fact_id
            planned_fact_id = approved_by_actor[actor].get("fact_id")
            assert isinstance(planned_fact_id, str) and planned_fact_id
            assert fact_id == planned_fact_id
            assert operation["value"] == run_state.values[
                approved_by_actor[actor]["destination"]
            ]

        assert isinstance(apply_result, dict)
        assert apply_result.get("status") == "complete"
        written_keys = apply_result.get("written_keys", [])
        assert isinstance(written_keys, list)
        expected_written_keys = {
            f"private:{actor}:memory:{action['fact_id']}"
            for actor, action in approved_by_actor.items()
        }
        assert set(written_keys) == expected_written_keys
        assert expected_written_keys <= set(run_state.values)
        run_state.physical_keys.append(set(run_state.values))
        return apply_result

    @staticmethod
    def _task_5_repeat_connection_point(
        *,
        run_state: SyntheticTransitionRunState,
    ) -> dict[str, object]:
        from familia.memory_migration import apply_legacy_transition_plan

        plan = run_state.plan
        assert isinstance(plan, dict)
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

        def apply_once() -> dict[str, object]:
            result = asyncio.run(
                apply_legacy_transition_plan(
                    plan=plan,
                    workspace=run_state.target_workspace,
                    get_value=run_state.get_value,
                    ingestor=run_state,
                    consolidate_history=run_state.consolidate_history,
                )
            )
            run_state.physical_keys.append(set(run_state.values))
            return result

        baseline_keys = set(run_state.values)
        calls_before = len(run_state.calls)
        get_calls_before = len(run_state.get_calls)
        run_state.fail_actor = expected_actors[1]
        run_state.failed_once = False
        run_state.stage = "repeat_partial"
        first = apply_once()
        first_key = expected_by_actor[expected_actors[0]]
        assert first["status"] == "partial"
        assert first["applied_actions"] == 1
        assert first["written_keys"] == [first_key]
        assert first["failed_actors"] == [expected_actors[1]]
        assert set(run_state.values) == baseline_keys

        run_state.stage = "repeat_retry"
        second = apply_once()
        assert second["status"] == "complete"
        assert set(second["written_keys"]) == expected_keys
        assert set(run_state.values) == baseline_keys
        values_after_second = dict(run_state.values)

        run_state.stage = "repeat"
        third = apply_once()
        assert third["status"] == "complete"
        assert set(third["written_keys"]) == expected_keys
        assert set(run_state.values) == baseline_keys
        assert all(
            run_state.values[key] != values_after_second[key]
            for key in expected_keys
        )

        repeat_get_calls = run_state.get_calls[get_calls_before:]
        assert repeat_get_calls == (
            expected_action_keys[:2]
            + expected_action_keys
            + expected_action_keys
        )
        repeat_calls = run_state.calls[calls_before:]
        assert {
            call["server_principal"] for call in repeat_calls
        } == set(expected_actors)
        assert all(
            set(call)
            == {"server_principal", "server_topic", "operation"}
            and call["server_topic"] is None
            and set(call["operation"]) == {"kind", "fact_id", "value"}
            and call["operation"]["kind"] == "memory"
            and call["operation"]["fact_id"] == "legacy-history"
            and isinstance(call["operation"]["value"], str)
            for call in repeat_calls
        )
        assert run_state.discarded_actors.isdisjoint(
            call["server_principal"] for call in repeat_calls
        )
        assert expected_keys <= set(run_state.values)
        assert all(keys == baseline_keys for keys in run_state.physical_keys)

        return {
            "partial": first,
            "retry": second,
            "repeat": third,
            "physical_keys": sorted(run_state.values),
        }

    @staticmethod
    def _synthetic_task_7_composed_restore(
        *,
        run_state: SyntheticTransitionRunState,
    ) -> dict[str, object]:
        existing = next(
            (
                receipt
                for receipt in run_state.receipts
                if receipt.get("kind") == "synthetic_task_7_composed_receipt"
            ),
            None,
        )
        if existing is not None:
            return existing

        plan = run_state.plan
        assert isinstance(plan, dict)
        required_keys = {
            action["destination"]
            for action in plan["actions"]
            if action.get("disposition") == "llm_required"
        }
        target_evidence = _canonical_target_evidence(
            run_state.target_workspace,
            run_state.values,
        )
        principals_registry_bytes = (
            run_state.source_root / "principals.json"
        ).read_bytes()
        stage = {
            "target_digest": target_evidence["target_digest"],
            "target_files": target_evidence["target_files"],
            "physical_keys": target_evidence["physical_keys"],
            "principals_registry_sha256": hashlib.sha256(
                principals_registry_bytes
            ).hexdigest(),
        }
        assert required_keys <= set(run_state.values)
        assert dict(run_state.source_bytes_before) == {
            path.relative_to(run_state.source_root).as_posix(): path.read_bytes()
            for path in sorted(run_state.source_root.rglob("*"))
            if path.is_file()
        }
        assert synthetic_source_digest(
            run_state.source_root,
            json.loads(run_state.source_logical_payload.decode("utf-8")),
        ) == run_state.source_digest_before
        assert (
            run_state.target_workspace / "memory" / "history.jsonl"
        ).read_bytes() == (
            run_state.source_workspace / "memory" / "history.jsonl"
        ).read_bytes()
        parsed_family_graph = json.loads(run_state.values["shared:family.graph"])
        parsed_topics_graph = json.loads(run_state.values["shared:topics.graph"])
        assert parsed_family_graph == run_state.family_graph
        assert parsed_topics_graph == run_state.topics_graph
        validation = {
            "source_unchanged": True,
            "required_keys_present": True,
            "graphs_match_store": True,
            "legacy_history_preserved": True,
            "principals_registry_present": True,
        }
        receipt_id = "synthetic-task7-composed-restore"
        conflict_evidence = _actual_conflict_evidence(run_state)
        assert conflict_evidence["outcome"] == "awaiting_owner"
        conflict_evidence_sha256 = _evidence_sha256(conflict_evidence)
        validate_event = {
            "kind": "synthetic_task_7_validate_event",
            "receipt_id": receipt_id,
            "admin_executed": False,
            "sequence": 0,
            "side_effect": False,
        }
        stage_event = {
            "kind": "synthetic_task_7_stage_event",
            "receipt_id": receipt_id,
            "admin_executed": False,
            "sequence": 1,
            "side_effect": False,
        }
        publish_event = {
            "kind": "synthetic_task_7_composed_event",
            "receipt_id": receipt_id,
            "admin_executed": False,
            "sequence": 2,
            "side_effect": True,
            "conflict_evidence_sha256": conflict_evidence_sha256,
            "conflict_notification": {
                "recipients": [conflict_evidence["owner"]],
                "message": (
                    "Migration conflict at "
                    f"{conflict_evidence['destination']} awaits its owner"
                ),
            },
        }
        run_state.published_state = {
            "target_files": target_evidence["target_file_bytes"],
            "values": dict(run_state.values),
            "family_graph": parsed_family_graph,
            "topics_graph": parsed_topics_graph,
            "principals_registry_bytes": principals_registry_bytes,
        }
        receipt = {
            "kind": "synthetic_task_7_composed_receipt",
            "receipt_id": receipt_id,
            "admin_executed": False,
            "conflict_evidence": conflict_evidence,
            "stage": stage,
            "validate": validation,
            "publish": {
                "event_kind": publish_event["kind"],
                "event_sequence": publish_event["sequence"],
                "conflict_evidence_sha256": conflict_evidence_sha256,
                "physical_keys": target_evidence["physical_keys"],
            },
        }
        run_state.receipts.append(receipt)
        run_state.events.extend(
            (validate_event, stage_event, publish_event)
        )
        run_state.stage = "published"
        return receipt

    @classmethod
    def _task_7_restore_connection_point(
        cls,
        *,
        run_state: SyntheticTransitionRunState,
    ) -> dict[str, object]:
        first = cls._synthetic_task_7_composed_restore(run_state=run_state)
        repeated = cls._synthetic_task_7_composed_restore(run_state=run_state)
        assert repeated is first
        return {"first": first, "repeat": repeated}

    @staticmethod
    def _current_verify_connection_point(
        *,
        run_state: SyntheticTransitionRunState,
        restore_result: object,
    ) -> dict[str, object]:
        from familia.acl.principal_memory import decide_memory_read

        plan = run_state.plan
        assert isinstance(plan, dict)
        assert isinstance(restore_result, dict)
        first_receipt = restore_result.get("first")
        repeated_receipt = restore_result.get("repeat")
        assert isinstance(first_receipt, dict)
        assert first_receipt is repeated_receipt
        assert first_receipt.get("kind") == "synthetic_task_7_composed_receipt"
        assert first_receipt.get("admin_executed") is False
        composed_receipts = [
            receipt for receipt in run_state.receipts
            if receipt.get("kind") == "synthetic_task_7_composed_receipt"
        ]
        restore_events = [
            event for event in run_state.events
            if event.get("kind") == "synthetic_task_7_composed_event"
        ]
        assert (
            len(composed_receipts) == 1
            and len(restore_events) == 1
            and composed_receipts[0].get("receipt_id")
            == restore_events[0].get("receipt_id")
        ), "exactly one synthetic task 7 receipt and publish event"
        assert first_receipt is composed_receipts[0]

        current_source_bytes = {
            path.relative_to(run_state.source_root).as_posix(): path.read_bytes()
            for path in sorted(run_state.source_root.rglob("*"))
            if path.is_file()
        }
        assert current_source_bytes == dict(run_state.source_bytes_before), (
            "canonical source bytes changed"
        )
        assert (
            synthetic_source_digest(
                run_state.source_root,
                json.loads(run_state.source_logical_payload.decode("utf-8")),
            )
            == run_state.source_digest_before
        ), "canonical source logical digest changed"

        required_keys = {
            action["destination"]
            for action in plan["actions"]
            if action.get("disposition") == "llm_required"
        }
        assert required_keys <= set(run_state.values), "required final key is missing"
        assert run_state.physical_keys
        first_physical_keys = run_state.physical_keys[0]
        assert all(
            keys == first_physical_keys for keys in run_state.physical_keys[1:]
        ), "repeat introduced an extra physical key"
        assert set(run_state.values) == first_physical_keys

        published_state = run_state.published_state
        assert isinstance(published_state, dict)
        assert published_state.get("values") == run_state.values, (
            "published store no longer matches the actual store"
        )
        target_evidence = _canonical_target_evidence(
            run_state.target_workspace, run_state.values
        )
        stage = first_receipt["stage"]
        publish = first_receipt["publish"]
        assert set(first_receipt) == {
            "kind", "receipt_id", "admin_executed", "conflict_evidence",
            "stage", "validate", "publish",
        }
        assert stage["target_digest"] == target_evidence["target_digest"], (
            "restore target digest mismatch"
        )
        assert stage["target_files"] == target_evidence["target_files"], (
            "restore target files mismatch"
        )
        actual_registry_bytes = (run_state.source_root / "principals.json").read_bytes()
        assert (
            published_state.get("principals_registry_bytes")
            == actual_registry_bytes
        ), "published registry evidence mismatch"
        assert stage == {
            "target_digest": target_evidence["target_digest"],
            "target_files": target_evidence["target_files"],
            "physical_keys": target_evidence["physical_keys"],
            "principals_registry_sha256": hashlib.sha256(
                actual_registry_bytes
            ).hexdigest(),
        }, "restore receipt evidence mismatch"
        assert first_receipt["validate"] == {
            "source_unchanged": True,
            "required_keys_present": True,
            "graphs_match_store": True,
            "legacy_history_preserved": True,
            "principals_registry_present": True,
        }
        assert publish == {
            "event_kind": "synthetic_task_7_composed_event",
            "event_sequence": 2,
            "conflict_evidence_sha256": publish["conflict_evidence_sha256"],
            "physical_keys": target_evidence["physical_keys"],
        }
        published_target_files = published_state.get("target_files")
        assert isinstance(published_target_files, dict)
        assert (
            published_target_files.get("memory/history.jsonl")
            == target_evidence["target_file_bytes"]["memory/history.jsonl"]
        ), "published history evidence mismatch"
        assert (
            published_target_files == target_evidence["target_file_bytes"]
        ), "published target files mismatch"

        assert (
            len(run_state.calls)
            == len(run_state.call_results)
            == len(run_state.call_stages)
        ), "unfinished ingest rows"
        ingest_receipts = [
            receipt
            for receipt in run_state.receipts
            if receipt.get("kind") == "synthetic_ingest_receipt"
        ]
        receipts_by_call = {
            receipt["call_index"]: receipt for receipt in ingest_receipts
        }
        assert len(receipts_by_call) == len(ingest_receipts), "unfinished ingest rows"
        for call_index, (call, result) in enumerate(
            zip(run_state.calls, run_state.call_results, strict=True)
        ):
            if not result.startswith("committed:"):
                assert call_index not in receipts_by_call, (
                    "retryable failure produced an ingest receipt"
                )
                continue
            assert call_index in receipts_by_call, "actual ingest receipt is missing"
            operation, actor = call["operation"], call["server_principal"]
            assert receipts_by_call[call_index] == {
                "kind": "synthetic_ingest_receipt",
                "call_index": call_index,
                "stage": run_state.call_stages[call_index],
                "actor": actor,
                "physical_key": f"private:{actor}:memory:{operation['fact_id']}",
                "value": operation["value"],
                "result": result,
            }, "ingest receipt evidence mismatch"
        assert len(ingest_receipts) == sum(
            result.startswith("committed:") for result in run_state.call_results
        ), "unfinished ingest rows"
        last_committed_receipts: dict[str, dict[str, object]] = {}
        for receipt in sorted(
            ingest_receipts, key=lambda item: item["call_index"]
        ):
            physical_key = receipt["physical_key"]
            if physical_key in required_keys:
                last_committed_receipts[physical_key] = receipt
        assert required_keys <= set(last_committed_receipts), (
            "actual ingest receipt is missing"
        )
        for key in required_keys:
            assert (
                run_state.values[key] == last_committed_receipts[key]["value"]
            ), "final value does not match last committed receipt"

        published_family_graph = published_state.get("family_graph")
        published_topics_graph = published_state.get("topics_graph")
        assert published_family_graph == json.loads(
            run_state.values["shared:family.graph"]
        )
        assert published_topics_graph == json.loads(
            run_state.values["shared:topics.graph"]
        )
        assert published_family_graph == run_state.family_graph
        assert published_topics_graph == run_state.topics_graph

        assert all(event.get("admin_executed") is False for event in restore_events)
        first_publish_index = run_state.events.index(restore_events[0])
        pre_switch_side_effects = [
            event
            for event in run_state.events[:first_publish_index]
            if event.get("side_effect") is True
        ]
        assert not pre_switch_side_effects, "pre-switch side effect"
        task_7_events = [
            event for event in run_state.events
            if event.get("receipt_id") == first_receipt["receipt_id"]
        ]
        assert [event.get("kind") for event in task_7_events] == [
            "synthetic_task_7_validate_event",
            "synthetic_task_7_stage_event",
            "synthetic_task_7_composed_event",
        ]
        assert [event.get("sequence") for event in task_7_events] == [0, 1, 2]
        assert [event.get("side_effect") for event in task_7_events] == [
            False, False, True,
        ]
        assert all(event.get("admin_executed") is False for event in task_7_events)
        assert task_7_events[-1] is restore_events[0]
        receipt_ids = [event["receipt_id"] for event in restore_events]
        repeat_restore_duplicate_count = len(receipt_ids) - len(set(receipt_ids))

        flat_relative_paths = (
            "USER.md",
            "MEMORY.md",
            "memory/MEMORY.md",
        )
        flat_paths_zero_bytes = [
            f"workspace/{relative_path}"
            for relative_path in flat_relative_paths
            if (run_state.target_workspace / relative_path).is_file()
            and (run_state.target_workspace / relative_path).read_bytes() == b""
        ]
        soul_bytes = (run_state.target_workspace / "SOUL.md").read_bytes()
        assert soul_bytes == (run_state.source_workspace / "SOUL.md").read_bytes()
        assert (
            run_state.target_workspace / "memory" / "history.jsonl"
        ).read_bytes() == (
            run_state.source_workspace / "memory" / "history.jsonl"
        ).read_bytes(), "legacy history evidence must be preserved"

        active_surfaces = {
            "cleaned_history": [
                call["operation"]["value"]
                for call in run_state.calls
                if isinstance(call.get("operation"), dict)
            ],
            "memx": run_state.values,
            "journals": {
                "receipts": run_state.receipts,
                "events": run_state.events,
            },
        }
        unknown_markers = (
            "unknown content",
            "actorless content",
            "malformed json",
            "principal_unknown",
        )
        unknown_content_absent_from = [
            surface
            for surface, content in active_surfaces.items()
            if not any(
                marker in json.dumps(
                    content,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                for marker in unknown_markers
            )
        ]
        assert "journals" in unknown_content_absent_from, (
            "unknown content remains in operational journals"
        )

        notification = restore_events[0].get("conflict_notification")
        assert isinstance(notification, dict)
        actual_conflict_evidence = _actual_conflict_evidence(run_state)
        assert (
            actual_conflict_evidence["outcome"] == "awaiting_owner"
        ), "conflict evidence mismatch"
        actual_conflict_sha256 = _evidence_sha256(
            actual_conflict_evidence
        )
        assert (
            first_receipt.get("conflict_evidence")
            == actual_conflict_evidence
            and publish["conflict_evidence_sha256"]
            == actual_conflict_sha256
            and restore_events[0].get("conflict_evidence_sha256")
            == actual_conflict_sha256
        ), "conflict evidence mismatch"
        expected_notification = {
            "recipients": [actual_conflict_evidence["owner"]],
            "message": (
                "Migration conflict at "
                f"{actual_conflict_evidence['destination']} awaits its owner"
            ),
        }
        assert notification == expected_notification, (
            "conflict evidence mismatch"
        )
        conflict_owner_only = actual_conflict_evidence["owner"]
        notification_text = json.dumps(notification, ensure_ascii=False)
        existing_conflict = json.loads(
            run_state.values[actual_conflict_evidence["destination"]]
        )
        preserved_history_rows = [
            json.loads(line)
            for line in (
                run_state.target_workspace / "memory" / "history.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line.startswith("{") and not line.startswith("{malformed")
        ]
        conflict_history_row = next(
            row
            for row in preserved_history_rows
            if row.get("cursor")
            == actual_conflict_evidence["history_cursor"]
        )
        proposed_conflict = json.loads(conflict_history_row["content"])
        conflict_fact_texts = (
            existing_conflict["value"],
            proposed_conflict["value"],
        )
        conflict_notification_contains_fact_text = any(
            text in notification_text for text in conflict_fact_texts
        )

        decision_cases: list[str] = []
        for row in EXPECTED_MEMORY_READ_DECISIONS:
            decision = decide_memory_read(
                reader=row["reader"],
                owner=row["owner"],
                scope=row["scope"],
                key=row["key"],
                tags=row["tags"],
                family_graph=published_family_graph,
                topics_graph=published_topics_graph,
                static_policy=row["static_policy"],
            )
            actual_visibility = {
                "memory_get": decision.allowed,
                "history": decision.allowed,
                "index": decision.allowed,
            }
            assert decision.allowed is row["allowed"]
            assert isinstance(decision.reason, str) and decision.reason
            if row["reason"] is not None:
                assert decision.reason == row["reason"]
            assert actual_visibility == row["visibility"]
            decision_cases.append(row["case"])

        return {
            "flat_paths_zero_bytes": flat_paths_zero_bytes,
            "soul_bytes": soul_bytes,
            "pre_switch_side_effects": pre_switch_side_effects,
            "repeat_restore_duplicate_count": repeat_restore_duplicate_count,
            "unknown_content_absent_from": unknown_content_absent_from,
            "conflict_owner_only": conflict_owner_only,
            "conflict_notification_contains_fact_text": (
                conflict_notification_contains_fact_text
            ),
            "decision_cases": decision_cases,
        }

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

        assert snapshot.existing_memory["shared:family.graph"]
        assert snapshot.existing_memory["shared:topics.graph"]
        assert snapshot.source_workspace != snapshot.workspace
        assert (
            synthetic_source_digest(
                snapshot.source_root,
                snapshot.source_logical_state,
            )
            == snapshot.source_digest
        )
        source_logical_state = json.loads(
            snapshot.source_logical_payload.decode("utf-8")
        )
        run_state = SyntheticTransitionRunState(
            source_root=snapshot.source_root,
            source_workspace=snapshot.source_workspace,
            target_workspace=snapshot.workspace,
            source_logical_payload=snapshot.source_logical_payload,
            family_graph=copy.deepcopy(source_logical_state["family_graph"]),
            topics_graph=copy.deepcopy(source_logical_state["topics_graph"]),
            values=copy.deepcopy(source_logical_state["existing_memory"]),
            source_bytes_before=tuple(snapshot.source_byte_pairs),
            source_digest_before=snapshot.source_digest,
        )

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
        run_state.plan = plan
        run_state.configure_plan(plan)
        run_state.stage = "planned"

        apply_result = self._task_4_apply_connection_point(
            run_state=run_state,
        )
        repeat_result = self._task_5_repeat_connection_point(
            run_state=run_state,
        )
        restore_result = self._task_7_restore_connection_point(
            run_state=run_state,
        )
        actual_end_state = self._current_verify_connection_point(
            run_state=run_state,
            restore_result=restore_result,
        )
        self._assert_current_end_state(
            actual_end_state,
            snapshot.expected_end_state,
        )

        return {
            "run_state": run_state,
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
            transition = self._run_staged_transition(
                snapshot=snapshot,
                known_principals=known_principals,
            )
            run_state = transition["run_state"]
            restore_result = transition["restore"]
            assert isinstance(run_state, SyntheticTransitionRunState)

            canonical_source_files = _file_bytes(snapshot.source_root)
            canonical_source_bytes = snapshot.source_bytes
            canonical_source_state = snapshot.source_logical_state
            canonical_source_payload = snapshot.source_logical_payload
            canonical_expected = copy.deepcopy(snapshot.expected_end_state)
            original_family_id = run_state.family_graph["nodes"][0]["id"]
            original_topic_id = run_state.topics_graph["nodes"][0]["id"]
            existing_memory_key = sorted(snapshot.existing_memory)[0]
            original_existing_memory = run_state.values[existing_memory_key]
            try:
                run_state.family_graph["nodes"][0]["id"] = (
                    "corrupted_operational_principal"
                )
                run_state.topics_graph["nodes"][0]["id"] = (
                    "corrupted_operational_topic"
                )
                run_state.values[existing_memory_key] = (
                    "corrupted operational existing memory"
                )
                self.assertEqual(_file_bytes(snapshot.source_root), canonical_source_files)
                self.assertEqual(snapshot.source_bytes, canonical_source_bytes)
                self.assertEqual(snapshot.source_logical_payload, canonical_source_payload)
                self.assertEqual(
                    snapshot.source_logical_state,
                    canonical_source_state,
                    "operational source-derived state leaked into canonical snapshot",
                )
                self.assertEqual(snapshot.family_graph, canonical_source_state["family_graph"])
                self.assertEqual(snapshot.topics_graph, canonical_source_state["topics_graph"])
                self.assertEqual(
                    snapshot.deterministic_conflict,
                    canonical_source_state["deterministic_conflict"],
                )
                self.assertEqual(
                    snapshot.existing_memory,
                    canonical_source_state["existing_memory"],
                )
            finally:
                run_state.family_graph["nodes"][0]["id"] = original_family_id
                run_state.topics_graph["nodes"][0]["id"] = original_topic_id
                run_state.values[existing_memory_key] = original_existing_memory

            published_state = run_state.published_state
            assert isinstance(published_state, dict)
            self.assertIsInstance(
                published_state.get("principals_registry_bytes"),
                bytes,
                "actual published registry is missing",
            )

            def assert_actual_corruption_rejected(
                name: str,
                corrupt,
                expected_regex: str,
            ) -> None:
                with self.subTest(name=name):
                    try:
                        corrupted_state, corrupted_restore = copy.deepcopy(
                            (run_state, restore_result)
                        )
                        first = corrupted_restore["first"]
                        self.assertIs(first, corrupted_restore["repeat"])
                        self.assertIs(
                            first,
                            next(
                                receipt for receipt in corrupted_state.receipts
                                if receipt.get("kind")
                                == "synthetic_task_7_composed_receipt"
                            ),
                        )
                        corrupt(corrupted_state, corrupted_restore)
                        with self.assertRaisesRegex(
                            AssertionError,
                            expected_regex,
                        ):
                            actual = self._current_verify_connection_point(
                                run_state=corrupted_state,
                                restore_result=corrupted_restore,
                            )
                            self._assert_current_end_state(
                                actual,
                                snapshot.expected_end_state,
                            )
                    finally:
                        self.assertEqual(_file_bytes(snapshot.source_root), canonical_source_files)
                        self.assertEqual(snapshot.source_bytes, canonical_source_bytes)
                        self.assertEqual(snapshot.source_logical_payload, canonical_source_payload)
                        self.assertEqual(snapshot.source_logical_state, canonical_source_state)
                        self.assertEqual(snapshot.expected_end_state, canonical_expected)

            def alter_existing_conflict_revision(
                state: SyntheticTransitionRunState,
                _restore: dict[str, object],
            ) -> None:
                conflict_evidence = _actual_conflict_evidence(state)
                conflict_key = conflict_evidence["destination"]
                conflict_value = json.loads(state.values[conflict_key])
                conflict_value["revision"] = conflict_evidence["expected_revision"]
                encoded = json.dumps(conflict_value, sort_keys=True)
                state.values[conflict_key] = encoded
                state.published_state["values"][conflict_key] = encoded
                target_evidence = _canonical_target_evidence(
                    state.target_workspace, state.values
                )
                receipt = _restore["first"]
                receipt["stage"].update(
                    {
                        field: target_evidence[field]
                        for field in (
                            "target_digest", "target_files", "physical_keys"
                        )
                    }
                )
                receipt["publish"]["physical_keys"] = target_evidence["physical_keys"]

            def alter_required_final_value(
                state: SyntheticTransitionRunState,
                restore: dict[str, object],
            ) -> None:
                key = next(
                    key for key in sorted(state.values)
                    if key.endswith(":memory:legacy-history")
                )
                state.values[key] += ":changed-after-commit"
                state.published_state["values"][key] = state.values[key]
                target_evidence = _canonical_target_evidence(
                    state.target_workspace, state.values
                )
                receipt = restore["first"]
                receipt["stage"].update(
                    {
                        field: target_evidence[field]
                        for field in (
                            "target_digest", "target_files", "physical_keys"
                        )
                    }
                )
                receipt["publish"]["physical_keys"] = target_evidence["physical_keys"]

            def add_second_composed_restore(
                state: SyntheticTransitionRunState,
                restore: dict[str, object],
            ) -> None:
                receipt = copy.deepcopy(restore["first"])
                receipt["receipt_id"] = "synthetic-task7-second-restore"
                state.receipts.append(receipt)
                original_receipt_id = restore["first"]["receipt_id"]
                cloned_events = copy.deepcopy(
                    [
                        event for event in state.events
                        if event.get("receipt_id") == original_receipt_id
                    ]
                )
                assert len(cloned_events) == 3
                for event in cloned_events:
                    event["receipt_id"] = receipt["receipt_id"]
                state.events.extend(cloned_events)

            def add_unknown_journal_evidence(
                state: SyntheticTransitionRunState,
                _restore: dict[str, object],
            ) -> None:
                state.receipts.append(
                    {
                        "kind": "synthetic_journal_evidence",
                        "receipt_id": "synthetic-unknown-journal-evidence",
                        "admin_executed": False,
                        "evidence_note": "unknown content",
                    }
                )

            def insert_pre_publish_side_effect(
                state: SyntheticTransitionRunState,
                _restore: dict[str, object],
            ) -> None:
                state.events.insert(
                    0,
                    {
                        "kind": "synthetic_pre_publish_side_effect",
                        "receipt_id": "synthetic-pre-publish-side-effect",
                        "admin_executed": False,
                        "sequence": 0,
                        "side_effect": True,
                    },
                )

            corruption_cases = (
                (
                    "missing final key",
                    lambda state, _restore: state.values.pop(
                        next(
                            key for key in sorted(state.values)
                            if key.endswith(":memory:legacy-history")
                        )
                    ),
                    "required final key is missing",
                ),
                (
                    "missing ingest receipt",
                    lambda state, _restore: state.receipts.remove(
                        next(
                            receipt for receipt in state.receipts
                            if receipt.get("kind") == "synthetic_ingest_receipt"
                        )
                    ),
                    "actual ingest receipt is missing",
                ),
                (
                    "corrupted published principals registry",
                    lambda state, _restore: state.published_state.__setitem__(
                        "principals_registry_bytes", b'{"principals":[]}'
                    ),
                    "published registry evidence mismatch",
                ),
                (
                    "corrupted restore target digest",
                    lambda _state, restore: restore["first"]["stage"].__setitem__(
                        "target_digest", "0" * 64
                    ),
                    "restore target digest mismatch",
                ),
                (
                    "missing restore target file",
                    lambda _state, restore: restore["first"]["stage"][
                        "target_files"
                    ].remove("SOUL.md"),
                    "restore target files mismatch",
                ),
                (
                    "conflict revision no longer conflicts",
                    alter_existing_conflict_revision,
                    "conflict evidence mismatch",
                ),
                (
                    "corrupted published history evidence",
                    lambda state, _restore: state.published_state[
                        "target_files"
                    ].__setitem__(
                        "memory/history.jsonl",
                        state.published_state["target_files"][
                            "memory/history.jsonl"
                        ]
                        + b"corrupted evidence\n",
                    ),
                    "published history evidence mismatch",
                ),
                (
                    "required value changed after its committed receipt",
                    alter_required_final_value,
                    "final value does not match last committed receipt",
                ),
                (
                    "second composed restore receipt and publish event",
                    add_second_composed_restore,
                    "exactly one synthetic task 7 receipt and publish event",
                ),
                (
                    "unknown content in operational journal evidence",
                    add_unknown_journal_evidence,
                    "unknown content remains in operational journals",
                ),
                (
                    "pre-publish side effect",
                    insert_pre_publish_side_effect,
                    "pre-switch side effect",
                ),
            )
            for case_name, corruptor, expected_regex in corruption_cases:
                assert_actual_corruption_rejected(
                    case_name,
                    corruptor,
                    expected_regex,
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
