from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch


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


class LegacyMemoryTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="familia-memory-transition-")
        self.workspace = Path(self._temporary.name) / "workspace"
        (self.workspace / "memory").mkdir(parents=True)
        self.values: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _get(self, key: str) -> str | None:
        return self.values.get(key)

    def _set(self, key: str, value: str) -> None:
        self.writes.append((key, value))
        self.values[key] = value

    def test_plan_marks_actor_contaminated_profile_and_never_fans_out(self) -> None:
        personal_fact = (
            "Михаил предпочитает краткие практичные ответы чеклисты медицинские описания "
            "распознавание документов поездки льготы многодетной семьи экономические аналогии"
        )
        (self.workspace / "USER.md").write_text(personal_fact, encoding="utf-8")
        (self.workspace / "memory" / "MEMORY.md").write_text(
            "Общая старая память без подтверждённого владельца", encoding="utf-8"
        )
        (self.workspace / "HEARTBEAT.md").write_text("# HEARTBEAT.md\n", encoding="utf-8")
        _write_history(
            self.workspace,
            [_history_record(1, "kanin_mikhail", personal_fact)],
        )

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"admin", "kanin_mikhail"},
            get_value=self._get,
        )

        profile = next(
            action for action in plan["actions"] if action["component"] == "user_profile"
        )
        self.assertEqual(profile["disposition"], "dirty_legacy")
        self.assertEqual(profile["candidate_actor"], "kanin_mikhail")
        self.assertIsNone(profile["destination"])
        self.assertFalse(
            any(
                (action.get("destination") or "").startswith("shared:")
                for action in plan["actions"]
            )
        )
        self.assertFalse(
            any(
                action["phase"] == "files"
                and action["component"] in {"user_profile", "memory"}
                and action["disposition"] == "write"
                for action in plan["actions"]
            )
        )
        history = next(action for action in plan["actions"] if action["component"] == "history")
        self.assertEqual(history["actor"], "kanin_mikhail")
        self.assertEqual(history["destination"], "private:kanin_mikhail:value:memory")
        self.assertEqual(history["disposition"], "llm_required")

    def test_schema_less_known_actor_remains_exact_and_owner_only_routes_file(self) -> None:
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
            get_value=self._get,
            legacy_owner="owner_alpha",
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
            history["destination"], "private:legacy_actor_x:value:memory"
        )
        self.assertEqual(history["record_count"], 2)
        self.assertEqual(history["disposition"], "llm_required")
        self.assertEqual(plan["summary"].get("quarantine_needs_review", 0), 0)
        memory = next(
            action for action in plan["actions"] if action["component"] == "memory"
        )
        self.assertEqual(memory["actor"], "owner_alpha")
        self.assertEqual(memory["disposition"], "write")

    def test_recoverable_history_rows_warn_and_do_not_block_later_actor_or_cursor(
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
            get_value=self._get,
            legacy_owner="owner_alpha",
        )

        warnings = [
            action
            for action in plan["actions"]
            if action["disposition"] == "skip_warning"
        ]
        self.assertEqual(plan["status"], "ready_with_warnings")
        self.assertEqual(plan["warnings"], 2)
        self.assertEqual(
            {action["reason"] for action in warnings},
            {"history_malformed_or_unknown_schema", "history_actor_unknown"},
        )
        valid_action = next(
            action
            for action in plan["actions"]
            if action.get("actor") == valid["actor"] and action.get("cursors")
        )
        self.assertEqual(
            valid_action["destination"],
            f"private:{valid['actor']}:value:memory",
        )

        async def consolidate(
            actor: str, records: list[dict[str, Any]], _existing: str
        ) -> str:
            self.assertEqual(actor, valid["actor"])
            self.assertEqual([record["cursor"] for record in records], [3])
            return "- Поздний валидный факт."

        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                set_value=self._set,
                consolidate_history=consolidate,
            )
        )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["warnings"], 2)
        self.assertEqual(result["needs_review"], 0)
        self.assertEqual(
            (self.workspace / "memory" / ".dream_cursor").read_text(encoding="utf-8"),
            "3\n",
        )
        self.assertEqual(history_path.read_bytes(), history_payload)
        self.assertEqual(
            [key for key, _value in self.writes],
            [f"private:{valid['actor']}:value:memory"],
        )

    def test_legacy_actor_resolution_warns_for_unknown_rows_and_keeps_known(self) -> None:
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
            get_value=self._get,
            legacy_owner="owner_alpha",
        )

        warnings = [
            action
            for action in plan["actions"]
            if action["disposition"] == "skip_warning"
        ]
        self.assertEqual(plan["status"], "ready_with_warnings")
        self.assertEqual(plan["warnings"], 2)
        self.assertEqual(
            {(action["actor"], action["reason"]) for action in warnings},
            {
                ("legacy_unknown", "history_actor_unknown"),
                ("versioned_unknown", "history_actor_unknown"),
            },
        )
        known = next(
            action
            for action in plan["actions"]
            if action.get("actor") == "known_member" and action.get("cursors")
        )
        self.assertEqual(known["source_actors"], ["known_member"])

    def test_apply_merges_history_into_private_memory_without_touching_shared_files(self) -> None:
        personal_fact = (
            "Михаил предпочитает краткие практичные ответы чеклисты медицинские описания "
            "распознавание документов поездки льготы многодетной семьи экономические аналогии"
        )
        user_path = self.workspace / "USER.md"
        user_path.write_text(personal_fact, encoding="utf-8")
        heartbeat_path = self.workspace / "HEARTBEAT.md"
        heartbeat_path.write_text("- проверить лекарства\n", encoding="utf-8")
        history_payload = _write_history(
            self.workspace,
            [
                _history_record(1, "kanin_mikhail", personal_fact),
                _history_record(2, "kanin_mikhail", "Нужны короткие списки действий"),
            ],
        )
        self.values["private:kanin_mikhail:value:memory"] = "# Existing\n\n- old fact\n"

        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"admin", "kanin_mikhail"},
            get_value=self._get,
            legacy_owner="kanin_mikhail",
        )
        self.assertEqual(plan["status"], "ready_with_warnings")
        self.assertEqual(plan["warnings"], 1)

        async def consolidate(actor: str, records: list[dict[str, Any]], existing: str) -> str:
            self.assertEqual(actor, "kanin_mikhail")
            self.assertEqual([record["cursor"] for record in records], [1, 2])
            self.assertIn("old fact", existing)
            return "- Предпочитает краткие практичные ответы и чек-листы."

        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                set_value=self._set,
                consolidate_history=consolidate,
            )
        )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["warnings"], 1)
        self.assertEqual(result["needs_review"], 0)
        self.assertEqual(user_path.read_text(encoding="utf-8"), personal_fact)
        self.assertFalse(any(key.startswith("shared:") for key, _ in self.writes))
        migrated = self.values["private:kanin_mikhail:value:memory"]
        self.assertIn("# Existing", migrated)
        self.assertIn("Imported from history.jsonl", migrated)
        self.assertIn("Предпочитает краткие", migrated)
        self.assertEqual((self.workspace / "memory" / ".dream_cursor").read_text(), "2\n")
        self.assertEqual(
            (self.workspace / "memory" / "history.jsonl").read_bytes(),
            history_payload,
        )
        self.assertFalse(heartbeat_path.exists())
        self.assertEqual(
            (self.workspace / "legacy" / "HEARTBEAT.md").read_text(encoding="utf-8"),
            "- проверить лекарства\n",
        )

    def test_failed_history_consolidation_keeps_cursor_and_private_memory_unchanged(self) -> None:
        _write_history(
            self.workspace,
            [_history_record(9, "kanin_mikhail", "Новый приватный факт")],
        )
        cursor = self.workspace / "memory" / ".dream_cursor"
        cursor.write_text("8\n", encoding="utf-8")
        self.values["private:kanin_mikhail:value:memory"] = "unchanged"
        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"kanin_mikhail"},
            get_value=self._get,
        )

        async def fail(*_args: Any) -> str:
            raise RuntimeError("provider unavailable")

        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                set_value=self._set,
                consolidate_history=fail,
            )
        )

        self.assertEqual(result["status"], "fatal")
        self.assertEqual(result["failed_actors"], ["kanin_mikhail"])
        self.assertEqual(cursor.read_text(encoding="utf-8"), "8\n")
        self.assertEqual(self.values["private:kanin_mikhail:value:memory"], "unchanged")
        self.assertEqual(self.writes, [])

    def test_target_probe_failure_is_fatal_before_plan(self) -> None:
        _write_history(
            self.workspace,
            [_history_record(1, "member_alpha", "Приватный факт")],
        )

        def fail_probe(_key: str) -> None:
            raise PermissionError("injected auth failure")

        with self.assertRaises(memory_migration.MigrationError):
            memory_migration.build_legacy_transition_plan(
                workspace=self.workspace,
                known_actors={"member_alpha"},
                get_value=fail_probe,
            )

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
            get_value=self._get,
        )
        consolidated: list[str] = []

        async def consolidate(
            actor: str, _records: list[dict[str, Any]], _existing: str
        ) -> str:
            consolidated.append(actor)
            return f"- Факт для {actor}."

        def fail_first_write(key: str, value: str) -> None:
            if key == "private:actor_alpha:value:memory":
                raise PermissionError("injected auth failure")
            self._set(key, value)

        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                set_value=fail_first_write,
                consolidate_history=consolidate,
            )
        )

        self.assertEqual(result["status"], "fatal")
        self.assertEqual(result["failed_actors"], ["actor_alpha"])
        self.assertEqual(result["failed_actions"], ["history:actor_alpha"])
        self.assertEqual(consolidated, ["actor_alpha"])
        self.assertNotIn("private:actor_beta:value:memory", self.values)
        self.assertFalse((self.workspace / "memory" / ".dream_cursor").exists())
        self.assertEqual(
            (self.workspace / "memory" / "history.jsonl").read_bytes(),
            history_payload,
        )

    def test_repeated_plan_skips_history_block_with_matching_provenance(self) -> None:
        _write_history(
            self.workspace,
            [_history_record(1, "kanin_mikhail", "Приватный факт")],
        )
        first = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"kanin_mikhail"},
            get_value=self._get,
        )

        calls = 0

        async def consolidate(*_args: Any) -> str:
            nonlocal calls
            calls += 1
            return "- Приватный факт."

        asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=first,
                workspace=self.workspace,
                get_value=self._get,
                set_value=self._set,
                consolidate_history=consolidate,
            )
        )
        second = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"kanin_mikhail"},
            get_value=self._get,
        )
        history = next(action for action in second["actions"] if action["component"] == "history")

        self.assertEqual(calls, 1)
        self.assertEqual(history["disposition"], "skip")
        self.assertEqual(history["reason"], "history_already_imported")

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

    def test_equal_file_target_finishes_interrupted_legacy_move(self) -> None:
        heartbeat = self.workspace / "HEARTBEAT.md"
        heartbeat.write_text("- проверить лекарства\n", encoding="utf-8")
        key = "private:kanin_mikhail:value:heartbeat"
        self.values[key] = "- проверить лекарства\n"
        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"kanin_mikhail"},
            get_value=self._get,
        )
        action = next(item for item in plan["actions"] if item["component"] == "heartbeat")
        self.assertEqual(action["disposition"], "skip")
        self.assertEqual(action["reason"], "target_equal")

        async def should_not_run(*_args: Any) -> str:
            self.fail("history consolidator must not run without history")

        result = asyncio.run(
            memory_migration.apply_legacy_transition_plan(
                plan=plan,
                workspace=self.workspace,
                get_value=self._get,
                set_value=self._set,
                consolidate_history=should_not_run,
            )
        )

        self.assertEqual(result["status"], "success")
        self.assertFalse(heartbeat.exists())
        self.assertEqual(
            (self.workspace / "legacy" / "HEARTBEAT.md").read_text(encoding="utf-8"),
            "- проверить лекарства\n",
        )
        self.assertEqual(self.writes, [])

    def test_apply_rejects_tampered_shared_destination_before_any_write(self) -> None:
        heartbeat = self.workspace / "HEARTBEAT.md"
        heartbeat.write_text("- проверить лекарства\n", encoding="utf-8")
        plan = memory_migration.build_legacy_transition_plan(
            workspace=self.workspace,
            known_actors={"kanin_mikhail"},
            get_value=self._get,
        )
        action = next(item for item in plan["actions"] if item["component"] == "heartbeat")
        self.assertEqual(action["disposition"], "write")
        action["destination"] = "shared:heartbeat"

        async def should_not_run(*_args: Any) -> str:
            self.fail("history consolidator must not run")

        with self.assertRaises(memory_migration.MigrationBlockedError):
            asyncio.run(
                memory_migration.apply_legacy_transition_plan(
                    plan=plan,
                    workspace=self.workspace,
                    get_value=self._get,
                    set_value=self._set,
                    consolidate_history=should_not_run,
                )
            )

        self.assertEqual(self.writes, [])
        self.assertTrue(heartbeat.exists())

    def test_cli_uses_canonical_owner_env_unless_explicitly_overridden(self) -> None:
        with patch.dict("os.environ", {"FAMILIA_OWNER_ACTOR": "kanin_mikhail"}):
            self.assertEqual(
                graph_admin._resolve_legacy_owner(SimpleNamespace(legacy_owner=None)),
                "kanin_mikhail",
            )
            self.assertEqual(
                graph_admin._resolve_legacy_owner(
                    SimpleNamespace(legacy_owner="explicit_owner")
                ),
                "explicit_owner",
            )

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

    def test_cli_migration_allows_review_only_partial_but_rejects_fatal(
        self,
    ) -> None:
        plan = {
            "status": "ready_with_warnings",
            "actions": [],
            "summary": {"skip_warning": 2},
        }
        base_result = {
            "applied_actions": 1,
            "written_keys": ["private:member_beta:value:memory"],
            "failed_actors": [],
            "failed_actions": [],
            "needs_review": 0,
            "warnings": 2,
            "dream_cursor_updated": True,
        }
        args = SimpleNamespace(
            workspace=self.workspace,
            legacy_owner="owner_alpha",
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
            patch.object(graph_admin.audit, "log_event"),
        ):
            warning_result = {**base_result, "status": "success_with_warnings"}
            with (
                patch.object(
                    memory_migration,
                    "apply_legacy_transition_plan",
                    new=AsyncMock(return_value=warning_result),
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(graph_admin.cmd_migrate_hybrid_storage(args), 0)

            review_only_result = {
                **base_result,
                "status": "partial",
                "applied_actions": 2,
                "failed_actors": [],
                "failed_actions": [],
                "fatal_failure": None,
                "needs_review": 2,
                "warnings": 0,
                "dream_cursor_updated": True,
            }
            with (
                patch.object(
                    memory_migration,
                    "apply_legacy_transition_plan",
                    new=AsyncMock(return_value=review_only_result),
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(graph_admin.cmd_migrate_hybrid_storage(args), 0)

            systemic_result = {
                **base_result,
                "status": "fatal",
                "failed_actors": ["member_beta"],
                "failed_actions": ["history:member_beta"],
                "warnings": 0,
                "dream_cursor_updated": False,
            }
            with (
                patch.object(
                    memory_migration,
                    "apply_legacy_transition_plan",
                    new=AsyncMock(return_value=systemic_result),
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(graph_admin.cmd_migrate_hybrid_storage(args), 1)


if __name__ == "__main__":
    unittest.main()
