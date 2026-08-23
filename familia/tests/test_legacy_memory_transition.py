from __future__ import annotations

import asyncio
import io
import inspect
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "familia" / "src"))

from familia import memory_migration  # noqa: E402
from familia.cli import graph_admin  # noqa: E402


def _history_record(cursor: int, actor: str, content: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "cursor": cursor,
        "timestamp": f"2026-06-08 08:{cursor:02d}",
        "actor": actor,
        "content": content,
        "provenance": {"source": "runtime_history", "idempotency_key": None},
    }


def _legacy_history_record(cursor: int, actor: str, content: str) -> dict[str, Any]:
    return {
        "cursor": cursor,
        "timestamp": f"2026-05-04 15:{cursor:02d}",
        "actor": actor,
        "content": content,
    }


def _write_history(workspace: Path, records: list[dict[str, Any]]) -> bytes:
    history = workspace / "memory" / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        for record in records
    )
    history.write_bytes(payload)
    return payload


def _release_schema_errors(value: dict[str, Any]) -> list[str]:
    schema = json.loads(
        (REPO_ROOT / "release" / "memory-migration.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    return [
        error.message
        for error in sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    ]


class _RecordingIngestor:
    def __init__(
        self,
        values: dict[str, str],
        *,
        fail_actor: str | None = None,
    ) -> None:
        self.values = values
        self.fail_actor = fail_actor
        self.calls: list[dict[str, Any]] = []

    async def ingest(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        actor = kwargs["server_principal"]
        operation = kwargs["operation"]
        if actor == self.fail_actor:
            raise PermissionError("injected ingestor failure")
        key = f"private:{actor}:memory:{operation['fact_id']}"
        self.values[key] = operation["value"]
        return f"committed:{key}"


class LegacyMemoryTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="familia-memory-transition-")
        self.workspace = Path(self._temporary.name) / "workspace"
        (self.workspace / "memory").mkdir(parents=True)
        self.values: dict[str, str] = {}

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _get(self, key: str) -> str | None:
        return self.values.get(key)

    def test_flat_memory_is_retired_without_owner_inference_or_fan_out(self) -> None:
        flat_sources = {
            "USER.md": "Старый профиль",
            "MEMORY.md": "Старая корневая память",
            "memory/MEMORY.md": "Старая память рабочей области",
        }
        for relative, value in flat_sources.items():
            path = self.workspace.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        (self.workspace / "HEARTBEAT.md").write_text(
            "- служебное напоминание\n",
            encoding="utf-8",
        )
        _write_history(
            self.workspace,
            [_history_record(1, "kanin_mikhail", "Личный факт")],
        )

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"admin", "kanin_mikhail"},
        )

        file_actions = [
            action for action in plan["actions"] if action["phase"] == "files"
        ]
        self.assertEqual(
            {
                (action["source"], action["component"])
                for action in file_actions
            },
            {
                ("USER.md", "user_profile"),
                ("MEMORY.md", "memory"),
                ("memory/MEMORY.md", "memory"),
            },
        )
        self.assertTrue(
            all(
                action["disposition"] == "erase_without_read"
                and action["actor"] is None
                and action["candidate_actor"] is None
                and action["destination"] is None
                for action in file_actions
            )
        )
        self.assertNotIn("legacy_owner", plan)
        self.assertFalse(
            any(action.get("component") == "heartbeat" for action in plan["actions"])
        )
        self.assertFalse(
            any(
                (action.get("destination") or "").startswith("shared:")
                for action in plan["actions"]
            )
        )
        history = next(
            action for action in plan["actions"] if action["component"] == "history"
        )
        self.assertEqual(history["actor"], "kanin_mikhail")
        self.assertEqual(history["fact_id"], "legacy-history")
        self.assertEqual(
            history["destination"],
            "private:kanin_mikhail:memory:legacy-history",
        )
        self.assertEqual(history["disposition"], "llm_required")

    def test_completion_marker_accepts_only_the_exact_value(self) -> None:
        self.assertFalse(memory_migration.legacy_transition_is_complete(None))
        self.assertTrue(
            memory_migration.legacy_transition_is_complete(
                json.dumps(memory_migration.LEGACY_TRANSITION_COMPLETION_MARKER)
            )
        )

        for invalid in (
            "not-json",
            json.dumps({"status": "complete"}),
            {**memory_migration.LEGACY_TRANSITION_COMPLETION_MARKER, "extra": True},
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(memory_migration.MigrationBlockedError),
            ):
                memory_migration.legacy_transition_is_complete(invalid)

    def test_schema_less_known_actor_remains_exact_and_flat_owner_is_ignored(self) -> None:
        legacy_fact = (
            "Владелец предпочитает короткие практичные ответы списки документов "
            "семейные напоминания поездки расписания лекарства и важные даты"
        )
        (self.workspace / "memory" / "MEMORY.md").write_text(
            "Общая настройка старого развёртывания без actor tag",
            encoding="utf-8",
        )
        _write_history(
            self.workspace,
            [
                _legacy_history_record(1, "legacy_actor_x", legacy_fact),
                _legacy_history_record(2, "legacy_actor_x", "Второй факт"),
            ],
        )

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"owner_alpha", "member_beta", "legacy_actor_x"},
        )

        history = next(
            action
            for action in plan["actions"]
            if action["phase"] == "history" and action.get("cursors")
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(history["actor"], "legacy_actor_x")
        self.assertEqual(history["source_actors"], ["legacy_actor_x"])
        self.assertEqual(
            history["destination"],
            "private:legacy_actor_x:memory:legacy-history",
        )
        self.assertEqual(history["fact_id"], "legacy-history")
        self.assertEqual(history["record_count"], 2)
        self.assertEqual(history["disposition"], "llm_required")
        self.assertNotIn("legacy_owner", plan)
        memory = next(
            action
            for action in plan["actions"]
            if action["source"] == "memory/MEMORY.md"
        )
        self.assertIsNone(memory["actor"])
        self.assertIsNone(memory["destination"])
        self.assertEqual(memory["disposition"], "erase_without_read")

    def test_invalid_history_rows_are_discarded_without_blocking_later_actor(
        self,
    ) -> None:
        unknown = _history_record(2, "unknown_actor", "Неизвестный actor")
        valid = _legacy_history_record(3, "member_beta", "Поздний валидный факт")
        history_path = self.workspace / "memory" / "history.jsonl"
        history_payload = b"{malformed json\n" + b"".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n"
            for record in (unknown, valid)
        )
        history_path.write_bytes(history_payload)

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"owner_alpha", "member_beta"},
        )

        discarded = [
            action
            for action in plan["actions"]
            if action["disposition"] == "discarded_unknown"
        ]
        self.assertEqual(plan["status"], "ready")
        self.assertNotIn("warnings", plan)
        self.assertEqual(plan["summary"]["discarded_unknown"], 2)
        self.assertEqual(
            {action["reason"] for action in discarded},
            {"history_malformed_or_unknown_schema", "history_actor_unknown"},
        )
        self.assertTrue(
            all(
                action["actor"] is None and action["destination"] is None
                for action in discarded
            )
        )
        valid_action = next(
            action
            for action in plan["actions"]
            if action.get("actor") == valid["actor"] and action.get("cursors")
        )
        self.assertEqual(
            valid_action["destination"],
            f"private:{valid['actor']}:memory:legacy-history",
        )

        async def consolidate(
            actor: str, records: list[dict[str, Any]], _existing: str
        ) -> str:
            self.assertEqual(actor, valid["actor"])
            self.assertEqual([record["cursor"] for record in records], [3])
            return "- Поздний валидный факт."

        ingestor = _RecordingIngestor(self.values)
        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                ingestor=ingestor,
                consolidate_history=consolidate,
            )
        )

        self.assertEqual(result["status"], "complete")
        self.assertTrue({"warnings", "needs_review"}.isdisjoint(result))
        self.assertEqual(
            (self.workspace / "memory" / ".dream_cursor").read_text(encoding="utf-8"),
            "3\n",
        )
        self.assertEqual(history_path.read_bytes(), history_payload)
        self.assertEqual(len(ingestor.calls), 1)
        self.assertEqual(
            ingestor.calls[0]["server_principal"],
            valid["actor"],
        )

    def test_unknown_legacy_actors_are_discarded_and_known_actor_is_kept(
        self,
    ) -> None:
        _write_history(
            self.workspace,
            [
                _legacy_history_record(1, "known_member", "Известный владелец"),
                _legacy_history_record(2, "legacy_unknown", "Неоднозначный владелец"),
                _history_record(3, "versioned_unknown", "Новая схема с неизвестным actor"),
            ],
        )

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"owner_alpha", "known_member"},
        )

        discarded = [
            action
            for action in plan["actions"]
            if action["disposition"] == "discarded_unknown"
        ]
        self.assertEqual(plan["status"], "ready")
        self.assertNotIn("warnings", plan)
        self.assertEqual(plan["summary"]["discarded_unknown"], 2)
        self.assertEqual(
            {
                (action["actor"], action["source_actor"], action["reason"])
                for action in discarded
            },
            {
                (None, "legacy_unknown", "history_actor_unknown"),
                (None, "versioned_unknown", "history_actor_unknown"),
            },
        )
        known = next(
            action
            for action in plan["actions"]
            if action.get("actor") == "known_member" and action.get("cursors")
        )
        self.assertEqual(known["source_actors"], ["known_member"])
        self.assertEqual(known["fact_id"], "legacy-history")
        self.assertEqual(
            known["destination"],
            "private:known_member:memory:legacy-history",
        )

    def test_invalid_v1_actor_is_discarded_without_principal_field_or_ingest(
        self,
    ) -> None:
        invalid_actor = "bad actor"
        _write_history(
            self.workspace,
            [_history_record(1, invalid_actor, "Факт без допустимого владельца")],
        )

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={invalid_actor},
        )
        ingestor = _RecordingIngestor(self.values)

        async def consolidate(
            _actor: str,
            _records: list[dict[str, Any]],
            _existing: str,
        ) -> str:
            return "- Факт без допустимого владельца."

        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                ingestor=ingestor,
                consolidate_history=consolidate,
            )
        )
        discarded = [
            action
            for action in plan["actions"]
            if action["disposition"] == "discarded_unknown"
        ]
        principal_values = list(plan["known_actors"])
        for action in plan["actions"]:
            principal_values.extend(
                value
                for field in ("actor", "candidate_actor", "source_actor")
                if (value := action.get(field)) is not None
            )
            principal_values.extend(action.get("source_actors") or [])
            if action.get("destination") is not None:
                principal_values.append(action["destination"])

        violations = []
        if len(discarded) != 1:
            violations.append(f"discarded_actions={len(discarded)}")
        elif discarded[0]["source_actor"] is not None:
            violations.append(
                f"discarded_source_actor={discarded[0]['source_actor']!r}"
            )
        if invalid_actor in principal_values or any(
            invalid_actor in value for value in principal_values
        ):
            violations.append(f"invalid_principal={invalid_actor!r}")
        schema_errors = _release_schema_errors(plan)
        if schema_errors:
            violations.append(f"schema_errors={schema_errors!r}")
        if ingestor.calls:
            violations.append(f"ingestor_calls={len(ingestor.calls)}")
        if result["status"] != "complete":
            violations.append(f"status={result['status']!r}")

        self.assertEqual(violations, [])

    def test_untrusted_legacy_actor_provenance_never_enters_source_actors(
        self,
    ) -> None:
        record = _history_record(1, "known_actor", "Факт допустимого владельца")
        record["provenance"]["legacy_actor"] = "bad actor"
        _write_history(self.workspace, [record])

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"known_actor"},
        )
        history = next(
            action
            for action in plan["actions"]
            if action.get("actor") == "known_actor"
        )

        violations = []
        if history["source_actors"] != ["known_actor"]:
            violations.append(f"source_actors={history['source_actors']!r}")
        schema_errors = _release_schema_errors(plan)
        if schema_errors:
            violations.append(f"schema_errors={schema_errors!r}")

        self.assertEqual(violations, [])

    def test_actor_identifiers_with_outer_whitespace_are_never_canonicalized(
        self,
    ) -> None:
        invalid_actor = " known_actor "
        _write_history(
            self.workspace,
            [_history_record(1, invalid_actor, "Факт с пробелами во владельце")],
        )
        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"known_actor"},
        )
        ingestor = _RecordingIngestor(self.values)

        async def consolidate(
            _actor: str,
            _records: list[dict[str, Any]],
            _existing: str,
        ) -> str:
            return "- Факт с пробелами во владельце."

        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                ingestor=ingestor,
                consolidate_history=consolidate,
            )
        )
        discarded = [
            action
            for action in plan["actions"]
            if action["disposition"] == "discarded_unknown"
        ]
        principal_values = list(plan["known_actors"])
        for action in plan["actions"]:
            principal_values.extend(
                value
                for field in ("actor", "candidate_actor", "source_actor")
                if (value := action.get(field)) is not None
            )
            principal_values.extend(action.get("source_actors") or [])
            if action.get("destination") is not None:
                principal_values.append(action["destination"])

        known_input_workspace = self.workspace.parent / "known-input-workspace"
        (known_input_workspace / "memory").mkdir(parents=True)
        _write_history(
            known_input_workspace,
            [_history_record(2, "known_actor", "Факт допустимого владельца")],
        )
        whitespace_known_plan = memory_migration.build_legacy_transition_plan(
            workspace=known_input_workspace,
            known_actors={invalid_actor},
        )
        whitespace_known_discarded = [
            action
            for action in whitespace_known_plan["actions"]
            if action["disposition"] == "discarded_unknown"
        ]

        violations = []
        if len(discarded) != 1:
            violations.append(f"history_discarded_actions={len(discarded)}")
        elif discarded[0]["source_actor"] is not None:
            violations.append(
                f"history_source_actor={discarded[0]['source_actor']!r}"
            )
        if any(invalid_actor in value for value in principal_values):
            violations.append(f"invalid_principal={invalid_actor!r}")
        schema_errors = _release_schema_errors(plan)
        if schema_errors:
            violations.append(f"history_schema_errors={schema_errors!r}")
        if ingestor.calls:
            violations.append(f"history_ingestor_calls={len(ingestor.calls)}")
        if result["status"] != "complete":
            violations.append(f"history_status={result['status']!r}")
        if whitespace_known_plan["known_actors"]:
            violations.append(
                f"known_actors={whitespace_known_plan['known_actors']!r}"
            )
        if (
            len(whitespace_known_discarded) != 1
            or whitespace_known_discarded[0]["source_actor"] != "known_actor"
        ):
            violations.append(
                f"known_input_discarded={whitespace_known_discarded!r}"
            )
        whitespace_schema_errors = _release_schema_errors(whitespace_known_plan)
        if whitespace_schema_errors:
            violations.append(
                f"known_input_schema_errors={whitespace_schema_errors!r}"
            )

        self.assertEqual(violations, [])

    def test_apply_preserves_existing_atomic_memory_and_only_retires_flat_files(
        self,
    ) -> None:
        flat_paths = (
            self.workspace / "USER.md",
            self.workspace / "MEMORY.md",
            self.workspace / "memory" / "MEMORY.md",
        )
        for index, path in enumerate(flat_paths, start=1):
            path.write_text(f"старое содержимое {index}", encoding="utf-8")
        heartbeat_path = self.workspace / "HEARTBEAT.md"
        heartbeat_before = "- проверить лекарства\n".encode("utf-8")
        heartbeat_path.write_bytes(heartbeat_before)
        history_payload = _write_history(
            self.workspace,
            [
                _history_record(1, "kanin_mikhail", "Предпочитает краткие ответы"),
                _history_record(2, "kanin_mikhail", "Нужны списки действий"),
            ],
        )
        key = "private:kanin_mikhail:memory:legacy-history"
        self.values[key] = "# Existing\n\n- old fact\n"

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"admin", "kanin_mikhail"},
        )
        self.assertEqual(plan["status"], "ready")
        self.assertNotIn("warnings", plan)
        self.assertNotIn("legacy_owner", plan)

        async def consolidate(
            actor: str,
            records: list[dict[str, Any]],
            existing: str,
        ) -> str:
            self.assertEqual(actor, "kanin_mikhail")
            self.assertEqual([record["cursor"] for record in records], [1, 2])
            self.assertIn("old fact", existing)
            return (
                f"{existing.rstrip()}\n\n"
                "- Предпочитает краткие практичные ответы и чек-листы."
            )

        ingestor = _RecordingIngestor(self.values)
        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                ingestor=ingestor,
                consolidate_history=consolidate,
            )
        )

        self.assertEqual(result["status"], "complete")
        self.assertTrue({"warnings", "needs_review"}.isdisjoint(result))
        self.assertEqual(result["written_keys"], [key])
        self.assertEqual(len(ingestor.calls), 1)
        self.assertIsNone(ingestor.calls[0]["server_topic"])
        self.assertEqual(
            ingestor.calls[0]["operation"]["fact_id"],
            "legacy-history",
        )
        self.assertIn("# Existing", self.values[key])
        self.assertIn("Предпочитает краткие", self.values[key])
        self.assertTrue(all(path.read_bytes() == b"" for path in flat_paths))
        self.assertEqual(heartbeat_path.read_bytes(), heartbeat_before)
        self.assertFalse((self.workspace / "legacy").exists())
        self.assertEqual(
            (self.workspace / "memory" / ".dream_cursor").read_text(),
            "2\n",
        )
        self.assertEqual(
            (self.workspace / "memory" / "history.jsonl").read_bytes(),
            history_payload,
        )

    def test_failed_history_consolidation_keeps_cursor_and_private_memory_unchanged(self) -> None:
        _write_history(
            self.workspace,
            [_history_record(9, "kanin_mikhail", "Новый приватный факт")],
        )
        cursor = self.workspace / "memory" / ".dream_cursor"
        cursor.write_text("8\n", encoding="utf-8")
        flat_path = self.workspace / "USER.md"
        flat_before = b"legacy profile"
        flat_path.write_bytes(flat_before)
        key = "private:kanin_mikhail:memory:legacy-history"
        self.values[key] = "unchanged"
        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"kanin_mikhail"},
        )

        async def fail(*_args: Any) -> str:
            raise RuntimeError("provider unavailable")

        ingestor = _RecordingIngestor(self.values)
        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                ingestor=ingestor,
                consolidate_history=fail,
            )
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_actors"], ["kanin_mikhail"])
        self.assertEqual(cursor.read_text(encoding="utf-8"), "8\n")
        self.assertEqual(self.values[key], "unchanged")
        self.assertEqual(ingestor.calls, [])
        self.assertEqual(flat_path.read_bytes(), flat_before)

    def test_systemic_history_write_stops_before_later_actor(self) -> None:
        history_payload = _write_history(
            self.workspace,
            [
                _history_record(1, "actor_alpha", "Первый факт"),
                _history_record(2, "actor_beta", "Второй факт"),
            ],
        )
        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"actor_alpha", "actor_beta"},
        )
        consolidated: list[str] = []

        async def consolidate(
            actor: str, _records: list[dict[str, Any]], _existing: str
        ) -> str:
            consolidated.append(actor)
            return f"- Факт для {actor}."

        ingestor = _RecordingIngestor(self.values, fail_actor="actor_alpha")
        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                ingestor=ingestor,
                consolidate_history=consolidate,
            )
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_actors"], ["actor_alpha"])
        self.assertEqual(result["failed_actions"], ["history:actor_alpha"])
        self.assertEqual(consolidated, ["actor_alpha"])
        self.assertEqual(
            [call["server_principal"] for call in ingestor.calls],
            ["actor_alpha"],
        )
        self.assertNotIn("private:actor_beta:memory:legacy-history", self.values)
        self.assertFalse((self.workspace / "memory" / ".dream_cursor").exists())
        self.assertEqual(
            (self.workspace / "memory" / "history.jsonl").read_bytes(),
            history_payload,
        )

    def test_apply_blocks_new_known_actor_before_any_side_effect(self) -> None:
        _write_history(
            self.workspace,
            [_history_record(1, "alice", "Факт Алисы")],
        )
        flat_paths = [
            self.workspace / "USER.md",
            self.workspace / "MEMORY.md",
            self.workspace / "memory" / "MEMORY.md",
        ]
        for index, path in enumerate(flat_paths, start=1):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"legacy flat {index}".encode())
        cursor_path = self.workspace / "memory" / ".dream_cursor"
        cursor_path.write_bytes(b"0\n")

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"alice", "bob"},
        )
        history_path = self.workspace / "memory" / "history.jsonl"
        with history_path.open("ab") as stream:
            stream.write(
                json.dumps(
                    _history_record(2, "bob", "Факт Боба"),
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )

        protected_paths = [*flat_paths, history_path, cursor_path]
        bytes_after_append = {
            path: path.read_bytes()
            for path in protected_paths
        }
        unexpected_calls: list[str] = []

        def should_not_get(_key: str) -> None:
            unexpected_calls.append("get_value")
            raise AssertionError("get_value must not run")

        async def should_not_consolidate(*_args: Any) -> str:
            unexpected_calls.append("consolidate_history")
            raise AssertionError("history consolidator must not run")

        class GuardedIngestor:
            async def ingest(self, **_kwargs: Any) -> str:
                unexpected_calls.append("ingestor")
                raise AssertionError("ingestor must not run")

        with (
            patch(
                "familia.memory_migration._write_private_atomic",
                side_effect=AssertionError("atomic writer must not run"),
            ) as atomic_write,
            self.assertRaises(memory_migration.MigrationBlockedError),
        ):
            asyncio.run(
                memory_migration.apply_legacy_transition_plan(
                    plan=plan,
                    workspace=self.workspace,
                    get_value=should_not_get,
                    ingestor=GuardedIngestor(),
                    consolidate_history=should_not_consolidate,
                )
            )

        self.assertEqual(unexpected_calls, [])
        atomic_write.assert_not_called()
        self.assertEqual(
            {path: path.read_bytes() for path in protected_paths},
            bytes_after_append,
        )

    def test_history_consolidator_requests_atomic_private_facts(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def chat_with_retry(self, **kwargs: Any) -> Any:
                self.calls.append(kwargs)
                return SimpleNamespace(content="- Предпочитает краткие ответы.")

        provider = Provider()
        consolidate = memory_migration.make_history_consolidator(provider, "test-model")
        records = [
            _history_record(1, "kanin_mikhail", "Предпочитаю краткие ответы"),
            _history_record(2, "kanin_mikhail", "Предпочитаю краткие ответы"),
        ]

        result = asyncio.run(consolidate("kanin_mikhail", records, "- Старый факт"))

        self.assertEqual(result, "- Предпочитает краткие ответы.")
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertIsNone(call["tools"])
        prompt = "\n".join(message["content"] for message in call["messages"])
        self.assertIn("kanin_mikhail", prompt)
        self.assertIn("Старый факт", prompt)
        self.assertIn("Предпочитаю краткие ответы", prompt)
        self.assertEqual(prompt.count("Предпочитаю краткие ответы"), 1)
        self.assertIn('"duplicate_count": 2', prompt)
        self.assertIn("atomic", prompt.lower())
        self.assertIn("private", prompt.lower())

    def test_apply_rejects_tampered_history_fact_id_before_any_write(self) -> None:
        history_payload = _write_history(
            self.workspace,
            [_history_record(1, "kanin_mikhail", "Личный факт")],
        )
        flat_path = self.workspace / "USER.md"
        flat_before = b"legacy profile"
        flat_path.write_bytes(flat_before)
        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"kanin_mikhail"},
        )
        action = next(
            item
            for item in plan["actions"]
            if item["component"] == "history" and item.get("actor")
        )
        action["fact_id"] = "tampered"
        action["destination"] = (
            f"private:{action['actor']}:memory:{action['fact_id']}"
        )

        async def should_not_run(*_args: Any) -> str:
            self.fail("history consolidator must not run")

        ingestor = _RecordingIngestor(self.values)
        with self.assertRaises(memory_migration.MigrationBlockedError):
            asyncio.run(
                memory_migration.apply_legacy_transition_plan(
                    plan=plan,
                    workspace=self.workspace,
                    get_value=self._get,
                    ingestor=ingestor,
                    consolidate_history=should_not_run,
                )
            )

        self.assertEqual(ingestor.calls, [])
        self.assertEqual(flat_path.read_bytes(), flat_before)
        self.assertEqual(
            (self.workspace / "memory" / "history.jsonl").read_bytes(),
            history_payload,
        )
        self.assertFalse((self.workspace / "memory" / ".dream_cursor").exists())

    def test_migration_interface_has_no_legacy_owner_or_flat_file_help(
        self,
    ) -> None:
        plan = {
            "status": "ready",
            "actions": [],
            "summary": {},
        }
        args = SimpleNamespace(
            workspace=self.workspace,
            config=None,
            dry_run=True,
            json=True,
        )
        with (
            patch.dict("os.environ", {"FAMILIA_OWNER_ACTOR": "environment_owner"}),
            patch.object(
                graph_admin,
                "_load_principals_json",
                return_value=(
                    self.workspace / "principals.json",
                    {
                        "principals": [
                            {"id": "environment_owner"},
                            {"id": "explicit_owner"},
                        ]
                    },
                ),
            ),
            patch("familia.acl.graph_io.get_raw", side_effect=self._get),
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                return_value=plan,
            ) as build_plan,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(graph_admin.cmd_migrate_hybrid_storage(args), 0)

        help_output = io.StringIO()
        with (
            redirect_stdout(help_output),
            self.assertRaises(SystemExit) as help_exit,
        ):
            graph_admin.build_parser().parse_args(
                ["migrate", "hybrid-storage", "--help"]
            )
        self.assertEqual(help_exit.exception.code, 0)

        violations = []
        if hasattr(graph_admin, "_resolve_legacy_owner"):
            violations.append("_resolve_legacy_owner")
        if "legacy_owner" in inspect.signature(
            memory_migration.build_legacy_transition_plan
        ).parameters:
            violations.append("build_legacy_transition_plan.legacy_owner")
        if "legacy_owner" in build_plan.call_args.kwargs:
            violations.append("FAMILIA_OWNER_ACTOR guess")
        for forbidden in (
            "--legacy-owner",
            "--force",
            "USER",
            "MEMORY",
            "HEARTBEAT",
        ):
            if forbidden in help_output.getvalue():
                violations.append(f"help:{forbidden}")

        self.assertEqual(violations, [])

    def test_cli_skips_completed_transition_before_building_plan(self) -> None:
        args = SimpleNamespace(
            workspace=self.workspace,
            config=None,
            dry_run=False,
            json=True,
        )
        output = io.StringIO()
        with (
            patch.object(
                graph_admin,
                "_load_principals_json",
                return_value=(
                    self.workspace / "principals.json",
                    {"principals": [{"id": "member_beta"}]},
                ),
            ),
            patch(
                "familia.acl.graph_io.resolve_admin_key",
                return_value="synthetic-admin-key",
            ),
            patch(
                "familia.acl.graph_io.get_raw",
                return_value=json.dumps(
                    memory_migration.LEGACY_TRANSITION_COMPLETION_MARKER
                ),
            ) as get_marker,
            patch("familia.acl.graph_io.set_raw") as set_marker,
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                side_effect=AssertionError("history plan must not be built"),
            ) as build_plan,
            patch.object(graph_admin.audit, "log_event"),
            redirect_stdout(output),
        ):
            self.assertEqual(graph_admin.cmd_migrate_hybrid_storage(args), 0)

        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "status": "complete",
                "applied_actions": 0,
                "written_keys": [],
                "failed_actors": [],
                "failed_actions": [],
                "fatal_failure": None,
                "dream_cursor_updated": False,
            },
        )
        self.assertEqual(_release_schema_errors(json.loads(output.getvalue())), [])
        get_marker.assert_called_once_with(
            memory_migration.LEGACY_TRANSITION_COMPLETION_KEY,
            api_key="synthetic-admin-key",
        )
        build_plan.assert_not_called()
        set_marker.assert_not_called()

    def test_cli_blocks_unknown_marker_before_building_plan(self) -> None:
        args = SimpleNamespace(
            workspace=self.workspace,
            config=None,
            dry_run=False,
            json=True,
        )
        with (
            patch.object(
                graph_admin,
                "_load_principals_json",
                return_value=(
                    self.workspace / "principals.json",
                    {"principals": [{"id": "member_beta"}]},
                ),
            ),
            patch(
                "familia.acl.graph_io.resolve_admin_key",
                return_value="synthetic-admin-key",
            ),
            patch(
                "familia.acl.graph_io.get_raw",
                return_value='{"status":"complete"}',
            ),
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                side_effect=AssertionError("history plan must not be built"),
            ) as build_plan,
            self.assertRaises(memory_migration.MigrationBlockedError),
        ):
            graph_admin.cmd_migrate_hybrid_storage(args)

        build_plan.assert_not_called()

    def test_graph_admin_pair_acl_uses_exact_underscore_without_memory_contract(
        self,
    ) -> None:
        with patch(
            "familia.principals.get_registry",
            return_value=SimpleNamespace(ids=("actor_alpha", "actor_beta")),
        ):
            self.assertEqual(
                graph_admin._scopes_for_principal("actor_beta"),
                [
                    "shared:*",
                    "private:actor_beta:*",
                    "pair:actor_alpha_actor_beta:*",
                ],
            )
        source = Path(graph_admin.__file__).read_text(encoding="utf-8")
        self.assertNotIn("memory_contract", source)

    def test_cli_migration_exit_codes_follow_apply_status(self) -> None:
        plan = {
            "status": "ready",
            "actions": [],
            "summary": {},
        }
        base_result = {
            "applied_actions": 1,
            "written_keys": ["private:member_beta:memory:legacy-history"],
            "failed_actors": [],
            "failed_actions": [],
            "dream_cursor_updated": True,
        }
        args = SimpleNamespace(
            workspace=self.workspace,
            config=None,
            dry_run=False,
            json=True,
        )

        with (
            patch.object(
                graph_admin,
                "_load_principals_json",
                return_value=(
                    self.workspace / "principals.json",
                    {"principals": [{"id": "member_beta"}]},
                ),
            ),
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                return_value=plan,
            ),
            patch(
                "familia.acl.graph_io.resolve_admin_key",
                return_value="synthetic-admin-key",
            ),
            patch("familia.acl.graph_io.get_raw", return_value=None),
            patch("familia.acl.graph_io.set_raw") as set_marker,
            patch(
                "familia.memx_client.memx_base_url",
                return_value="http://synthetic-memx",
            ),
            patch("familia.principal_memory_ingestor.PrincipalMemoryIngestor"),
            patch.object(graph_admin.audit, "log_event"),
        ):
            for status, expected_exit_code in (
                ("complete", 0),
                ("partial", 2),
                ("failed", 1),
            ):
                with (
                    self.subTest(status=status),
                    patch.object(
                        memory_migration,
                        "apply_legacy_transition_plan",
                        new=AsyncMock(
                            return_value={**base_result, "status": status}
                        ),
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        graph_admin.cmd_migrate_hybrid_storage(args),
                        expected_exit_code,
                    )

            set_marker.assert_called_once_with(
                memory_migration.LEGACY_TRANSITION_COMPLETION_KEY,
                memory_migration.LEGACY_TRANSITION_COMPLETION_MARKER,
                api_key="synthetic-admin-key",
            )


if __name__ == "__main__":
    unittest.main()
