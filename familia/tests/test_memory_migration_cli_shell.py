from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import redirect_stdout
import inspect
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator

from familia import memory_migration
from familia.cli import graph_admin
from familia.memory_contract import MEMORY_CONTRACT


class _RecordingIngestor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ingest(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "committed:private:alice:memory:legacy-history"


def test_obsolete_isolated_migration_api_is_absent() -> None:
    obsolete_symbols = {
        "MIGRATION_SCHEMA_VERSION",
        "DISPOSITIONS",
        "UNRESOLVED_DISPOSITIONS",
        "MigrationTarget",
        "IsolatedFileTarget",
        "build_migration_plan",
        "load_action_value",
        "apply_migration_plan",
    }

    assert {
        name for name in obsolete_symbols if hasattr(memory_migration, name)
    } == set()


def test_canonical_transition_has_no_unreachable_review_tail(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    for path in (
        workspace / "USER.md",
        workspace / "MEMORY.md",
        memory_dir / "MEMORY.md",
    ):
        path.write_text("legacy flat memory", encoding="utf-8")
    (memory_dir / "history.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cursor": 1,
                "timestamp": "2026-07-26 10:00",
                "actor": "alice",
                "content": "Prefers short answers",
                "provenance": {
                    "source": "runtime_history",
                    "idempotency_key": None,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    forbidden = {
        "conflict",
        "quarantine_needs_review",
        "skip_warning",
        "dirty_legacy",
        "needs_review",
        "ready_with_warnings",
        "warnings",
    }
    code_surfaces = {
        "planner": inspect.getsource(
            memory_migration.build_legacy_transition_plan
        ),
        "apply": inspect.getsource(
            memory_migration.apply_legacy_transition_plan
        ),
        "isolated_cli": inspect.getsource(memory_migration.cli),
        "graph_admin": inspect.getsource(
            graph_admin.cmd_migrate_hybrid_storage
        ),
    }
    violations = [
        f"{surface}:{token}"
        for surface, source in code_surfaces.items()
        for token in sorted(forbidden)
        if token in source
    ]

    plan = memory_migration.build_legacy_transition_plan(
        workspace=workspace,
        known_actors={"alice"},
    )
    expected_summary = dict(
        Counter(action["disposition"] for action in plan["actions"])
    )
    if plan["status"] != "ready":
        violations.append(f"plan.status:{plan['status']}")
    if plan["summary"] != expected_summary:
        violations.append("plan.summary:not_real_action_counts")
    for key in sorted(forbidden & set(plan)):
        violations.append(f"plan.field:{key}")

    result = asyncio.run(
        memory_migration.apply_legacy_transition_plan(
            plan=plan,
            workspace=workspace,
            get_value=lambda _key: None,
            ingestor=_RecordingIngestor(),
            consolidate_history=lambda *_args: _consolidated_history(),
        )
    )
    expected_result_fields = {
        "status",
        "applied_actions",
        "written_keys",
        "failed_actors",
        "failed_actions",
        "fatal_failure",
        "dream_cursor_updated",
    }
    if set(result) != expected_result_fields:
        violations.append(
            "apply.fields:"
            + ",".join(sorted(set(result) - expected_result_fields))
        )
    if result["status"] not in {"complete", "partial", "failed"}:
        violations.append(f"apply.status:{result['status']}")

    migration_outcomes = MEMORY_CONTRACT["outcomes"]["migration_command"]
    if migration_outcomes["plan"] != {
        "values": ["ready"],
        "terminal": ["ready"],
    }:
        violations.append("contract.outcomes.plan")
    if MEMORY_CONTRACT["migration"]["exit_codes"]["plan"] != {"ready": 0}:
        violations.append("contract.exit_codes.plan")

    assert violations == []


async def _consolidated_history() -> str:
    return "consolidated legacy history"


def test_transition_help_describes_history_and_unread_flat_cleanup() -> None:
    graph_help = io.StringIO()
    with redirect_stdout(graph_help), pytest.raises(SystemExit) as graph_exit:
        graph_admin.build_parser().parse_args(
            ["migrate", "hybrid-storage", "--help"]
        )
    assert graph_exit.value.code == 0

    isolated_help = io.StringIO()
    with redirect_stdout(isolated_help), pytest.raises(SystemExit) as isolated_exit:
        memory_migration.cli(["--help"])
    assert isolated_exit.value.code == 0

    for help_text in (graph_help.getvalue(), isolated_help.getvalue()):
        lowered = help_text.lower()
        assert "legacy history" in lowered
        assert "private memory" in lowered
        assert "three flat memory files" in lowered
        assert "unread" in lowered
        assert "owner fallback" not in lowered
        assert "repair" not in lowered
        assert "move legacy memory" not in lowered


def test_isolated_cli_is_only_a_canonical_transition_shell(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    memory_dir = workspace / "memory"
    snapshot_root.mkdir()
    memory_dir.mkdir(parents=True)
    (target_root / ".familia-memory-migration-target.json").write_text(
        json.dumps({"target_id": "isolated-test"}),
        encoding="utf-8",
    )
    (workspace / "principals.json").write_text(
        json.dumps({"principals": [{"id": "alice"}]}),
        encoding="utf-8",
    )

    flat_paths = (
        workspace / "USER.md",
        workspace / "MEMORY.md",
        memory_dir / "MEMORY.md",
    )
    for index, path in enumerate(flat_paths, start=1):
        path.write_text(f"legacy flat memory {index}", encoding="utf-8")
    history_path = memory_dir / "history.jsonl"
    history_before = (
        json.dumps(
            {
                "schema_version": 1,
                "cursor": 1,
                "timestamp": "2026-07-26 10:00",
                "actor": "alice",
                "content": "Prefers short answers",
                "provenance": {
                    "source": "runtime_history",
                    "idempotency_key": None,
                },
            },
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    history_path.write_bytes(history_before)
    untouched = {
        workspace / "SOUL.md": b"shared soul\n",
        workspace / "HEARTBEAT.md": b"shared heartbeat\n",
    }
    for path, value in untouched.items():
        path.write_bytes(value)

    ingestor = _RecordingIngestor()
    original_read_bytes = Path.read_bytes
    forbidden_reads = {path.resolve() for path in flat_paths}
    observed_reads: list[Path] = []

    def read_history_only(path: Path) -> bytes:
        resolved = path.resolve()
        observed_reads.append(resolved)
        if resolved in forbidden_reads:
            raise AssertionError(f"flat memory must not be read: {resolved}")
        return original_read_bytes(path)

    async def consolidate(
        _actor: str,
        _records: list[dict[str, object]],
        _existing: str,
    ) -> str:
        return "- Prefers short answers."

    canonical_build = memory_migration.build_legacy_transition_plan
    canonical_apply = memory_migration.apply_legacy_transition_plan
    built_plans: list[dict[str, object]] = []

    def record_canonical_build(**kwargs: object) -> dict[str, object]:
        plan = canonical_build(**kwargs)
        built_plans.append(plan)
        return plan

    with (
        patch(
            "scripts.compare_memory_state.load_and_validate_manifest",
            return_value={"snapshot_id": "a" * 64},
        ),
        patch.object(Path, "read_bytes", new=read_history_only),
        patch.object(memory_migration, "validate_migration_preflight"),
        patch.object(
            memory_migration,
            "build_legacy_transition_plan",
            side_effect=record_canonical_build,
        ) as build_transition,
        patch.object(
            memory_migration,
            "apply_legacy_transition_plan",
            new=AsyncMock(wraps=canonical_apply),
        ) as apply_transition,
        patch.object(
            memory_migration,
            "make_configured_history_consolidator",
            return_value=consolidate,
        ),
        patch(
            "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
            return_value=ingestor,
        ) as ingestor_type,
        patch("familia.acl.graph_io.get_raw", return_value=None) as get_value,
        patch("familia.acl.graph_io.resolve_admin_key", return_value="admin-key"),
        patch("familia.memx_client.memx_base_url", return_value="http://memx.test"),
    ):
        exit_code = memory_migration.cli(
            [
                "--snapshot",
                str(snapshot_root),
                "--target",
                str(target_root),
                "--source-root",
                str(workspace),
                "--manifest",
                str(target_root / "migration-plan.json"),
                "--journal",
                str(target_root / "migration-journal.jsonl"),
                "--apply",
                "--json",
            ]
        )

    assert exit_code == 0
    build_transition.assert_called_once()
    apply_transition.assert_awaited_once()

    build_kwargs = build_transition.call_args.kwargs
    assert build_kwargs["workspace"] == workspace
    assert build_kwargs["known_actors"] == {"alice"}
    assert "get_value" not in build_kwargs
    assert "legacy_owner" not in build_kwargs

    apply_kwargs = apply_transition.await_args.kwargs
    assert apply_kwargs["workspace"] == workspace
    assert apply_kwargs["get_value"] is get_value
    assert apply_kwargs["ingestor"] is ingestor
    assert len(built_plans) == 1
    assert apply_kwargs["plan"] is built_plans[0]
    plan = apply_kwargs["plan"]
    assert {
        action["source"]
        for action in plan["actions"]
        if action["disposition"] == "erase_without_read"
    } == {"USER.md", "MEMORY.md", "memory/MEMORY.md"}
    assert {
        action["source"]
        for action in plan["actions"]
        if action["component"] == "history"
    } == {"memory/history.jsonl"}

    ingestor_type.assert_called_once_with(
        base_url="http://memx.test",
        api_key="admin-key",
    )
    assert len(ingestor.calls) == 1
    assert history_path.resolve() in observed_reads
    assert forbidden_reads.isdisjoint(observed_reads)
    assert all(path.read_bytes() == b"" for path in flat_paths)
    assert history_path.read_bytes() == history_before
    assert {path: path.read_bytes() for path in untouched} == untouched


def _run_stubbed_apply_cli(
    tmp_path: Path,
    *,
    result: dict[str, object],
    json_output: bool,
) -> int:
    snapshot_root = tmp_path / "snapshot"
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    snapshot_root.mkdir()
    workspace.mkdir(parents=True)
    (target_root / ".familia-memory-migration-target.json").write_text(
        json.dumps({"target_id": "isolated-test"}),
        encoding="utf-8",
    )
    (workspace / "principals.json").write_text(
        json.dumps({"principals": [{"id": "alice"}]}),
        encoding="utf-8",
    )
    plan = {
        "status": "ready",
        "actions": [
            {"disposition": "erase_without_read"},
            {"disposition": "erase_without_read"},
            {"disposition": "erase_without_read"},
        ],
        "summary": {"erase_without_read": 3},
    }
    argv = [
        "--snapshot",
        str(snapshot_root),
        "--target",
        str(target_root),
        "--source-root",
        str(workspace),
        "--manifest",
        str(target_root / "migration-plan.json"),
        "--journal",
        str(target_root / "migration-journal.jsonl"),
        "--apply",
    ]
    if json_output:
        argv.append("--json")

    with (
        patch(
            "scripts.compare_memory_state.load_and_validate_manifest",
            return_value={"snapshot_id": "a" * 64},
        ),
        patch.object(memory_migration, "validate_migration_preflight"),
        patch.object(
            memory_migration,
            "build_legacy_transition_plan",
            return_value=plan,
        ),
        patch.object(
            memory_migration,
            "apply_legacy_transition_plan",
            new=AsyncMock(return_value=result),
        ),
        patch(
            "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
            return_value=object(),
        ),
        patch("familia.acl.graph_io.get_raw", return_value=None),
        patch("familia.acl.graph_io.resolve_admin_key", return_value="admin-key"),
        patch("familia.memx_client.memx_base_url", return_value="http://memx.test"),
    ):
        return memory_migration.cli(argv)


def test_both_migration_cli_json_boundaries_match_release_schema(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    memory_dir = workspace / "memory"
    snapshot_root = tmp_path / "snapshot"
    memory_dir.mkdir(parents=True)
    snapshot_root.mkdir()
    (target_root / ".familia-memory-migration-target.json").write_text(
        json.dumps({"target_id": "isolated-test"}),
        encoding="utf-8",
    )
    (workspace / "principals.json").write_text(
        json.dumps({"principals": [{"id": "alice"}]}),
        encoding="utf-8",
    )
    plan = memory_migration.build_legacy_transition_plan(
        workspace=workspace,
        known_actors={"alice"},
    )
    result = {
        "status": "complete",
        "applied_actions": 3,
        "written_keys": [],
        "failed_actors": [],
        "failed_actions": [],
        "fatal_failure": None,
        "dream_cursor_updated": False,
    }
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "release"
        / "memory-migration.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    def run_isolated(*, apply: bool) -> dict[str, object]:
        argv = [
            "--snapshot",
            str(snapshot_root),
            "--target",
            str(target_root),
            "--source-root",
            str(workspace),
            "--manifest",
            str(target_root / "migration-plan.json"),
            "--journal",
            str(target_root / "migration-journal.jsonl"),
            "--json",
        ]
        if apply:
            argv.append("--apply")
        output = io.StringIO()
        with (
            patch(
                "scripts.compare_memory_state.load_and_validate_manifest",
                return_value={"snapshot_id": "a" * 64},
            ),
            patch.object(memory_migration, "validate_migration_preflight"),
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                return_value=plan,
            ),
            patch.object(
                memory_migration,
                "apply_legacy_transition_plan",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
                return_value=object(),
            ),
            patch("familia.acl.graph_io.get_raw", return_value=None),
            patch(
                "familia.acl.graph_io.resolve_admin_key",
                return_value="admin-key",
            ),
            patch(
                "familia.memx_client.memx_base_url",
                return_value="http://memx.test",
            ),
            redirect_stdout(output),
        ):
            assert memory_migration.cli(argv) == 0
        return json.loads(output.getvalue())

    def run_graph(*, apply: bool) -> dict[str, object]:
        output = io.StringIO()
        args = SimpleNamespace(
            workspace=workspace,
            config=None,
            dry_run=not apply,
            json=True,
        )
        with (
            patch.object(
                graph_admin,
                "_load_principals_json",
                return_value=(
                    workspace / "principals.json",
                    {"principals": [{"id": "alice"}]},
                ),
            ),
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                return_value=plan,
            ),
            patch.object(
                memory_migration,
                "apply_legacy_transition_plan",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
                return_value=object(),
            ),
            patch("familia.acl.graph_io.get_raw", return_value=None),
            patch("familia.acl.graph_io.set_raw"),
            patch(
                "familia.acl.graph_io.resolve_admin_key",
                return_value="admin-key",
            ),
            patch(
                "familia.memx_client.memx_base_url",
                return_value="http://memx.test",
            ),
            patch.object(graph_admin.audit, "log_event"),
            redirect_stdout(output),
        ):
            assert graph_admin.cmd_migrate_hybrid_storage(args) == 0
        return json.loads(output.getvalue())

    documents = {
        "isolated-plan": run_isolated(apply=False),
        "isolated-apply": run_isolated(apply=True),
        "graph-plan": run_graph(apply=False),
        "graph-apply": run_graph(apply=True),
    }
    invalid = {
        name: [error.message for error in validator.iter_errors(document)]
        for name, document in documents.items()
        if not validator.is_valid(document)
    }

    assert invalid == {}


def test_graph_registry_preserves_string_ids_for_builder_validation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    history = [
        {
            "schema_version": 1,
            "cursor": 1,
            "timestamp": "2026-07-27 10:01",
            "actor": "alice",
            "content": "Факт Алисы",
            "provenance": {
                "source": "runtime_history",
                "idempotency_key": None,
            },
        },
        {
            "schema_version": 1,
            "cursor": 2,
            "timestamp": "2026-07-27 10:02",
            "actor": " alice ",
            "content": "Факт с недопустимым владельцем",
            "provenance": {
                "source": "runtime_history",
                "idempotency_key": None,
            },
        },
        {
            "schema_version": 1,
            "cursor": 3,
            "timestamp": "2026-07-27 10:03",
            "actor": "123",
            "content": "Факт числового идентификатора из реестра",
            "provenance": {
                "source": "runtime_history",
                "idempotency_key": None,
            },
        },
    ]
    (memory_dir / "history.jsonl").write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in history
        ),
        encoding="utf-8",
    )
    registry = {
        "principals": [
            {"id": "alice"},
            {"id": " alice "},
            {"id": 123},
        ]
    }
    canonical_build = memory_migration.build_legacy_transition_plan
    seen_builder_ids: list[set[object]] = []

    def record_build(**kwargs: object) -> dict[str, object]:
        known_actors = kwargs["known_actors"]
        assert isinstance(known_actors, set)
        seen_builder_ids.append(set(known_actors))
        return canonical_build(**kwargs)

    async def consolidate(
        actor: str,
        _records: list[dict[str, object]],
        _existing: str,
    ) -> str:
        return f"- Факт для {actor}."

    ingestor = _RecordingIngestor()
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "release"
        / "memory-migration.schema.json"
    )
    validator = Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8"))
    )

    def run_graph(*, dry_run: bool) -> dict[str, object]:
        output = io.StringIO()
        args = SimpleNamespace(
            workspace=workspace,
            config=None,
            dry_run=dry_run,
            json=True,
        )
        with (
            patch.object(
                graph_admin,
                "_load_principals_json",
                return_value=(workspace / "principals.json", registry),
            ),
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                side_effect=record_build,
            ),
            patch.object(
                memory_migration,
                "make_configured_history_consolidator",
                return_value=consolidate,
            ),
            patch(
                "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
                return_value=ingestor,
            ),
            patch("familia.acl.graph_io.get_raw", return_value=None),
            patch("familia.acl.graph_io.set_raw"),
            patch(
                "familia.acl.graph_io.resolve_admin_key",
                return_value="admin-key",
            ),
            patch(
                "familia.memx_client.memx_base_url",
                return_value="http://memx.test",
            ),
            patch.object(graph_admin.audit, "log_event"),
            redirect_stdout(output),
        ):
            assert graph_admin.cmd_migrate_hybrid_storage(args) == 0
        return json.loads(output.getvalue())

    plan = run_graph(dry_run=True)
    result = run_graph(dry_run=False)
    routed_actors = {
        action["actor"]
        for action in plan["actions"]
        if action.get("disposition") == "llm_required"
    }
    discarded = [
        action
        for action in plan["actions"]
        if action.get("disposition") == "discarded_unknown"
    ]
    invalid = {
        name: [error.message for error in validator.iter_errors(document)]
        for name, document in (("plan", plan), ("result", result))
        if not validator.is_valid(document)
    }

    assert seen_builder_ids == [
        {"alice", " alice "},
        {"alice", " alice "},
    ]
    assert plan["known_actors"] == ["alice"]
    assert routed_actors == {"alice"}
    assert {
        (action["source_actor"], action["reason"])
        for action in discarded
    } == {
        (None, "history_actorless"),
        ("123", "history_actor_unknown"),
    }
    assert result["written_keys"] == [
        "private:alice:memory:legacy-history"
    ]
    assert {
        call["server_principal"]
        for call in ingestor.calls
    } == {"alice"}
    assert invalid == {}


@pytest.mark.parametrize(
    ("status", "expected_exit_code"),
    [
        ("partial", 2),
        ("failed", 1),
        ("complete", 0),
        ("unexpected", 1),
    ],
)
def test_isolated_cli_apply_exit_code_follows_result(
    tmp_path: Path,
    status: str,
    expected_exit_code: int,
) -> None:
    result = {
        "status": status,
        "applied_actions": 0,
        "failed_actors": [],
        "fatal_failure": None,
    }

    assert (
        _run_stubbed_apply_cli(tmp_path, result=result, json_output=True)
        == expected_exit_code
    )


@pytest.mark.parametrize(
    ("result", "expected_output"),
    [
        (
            {
                "status": "partial",
                "applied_actions": 2,
                "failed_actors": ["alice"],
                "fatal_failure": "history:alice",
            },
            "status=partial applied_actions=2 "
            "failed_actors=alice error=history:alice",
        ),
        (
            {
                "status": "failed",
                "applied_actions": 0,
                "failed_actors": ["alice", "bob"],
                "fatal_failure": "ingest:unavailable",
            },
            "status=failed applied_actions=0 "
            "failed_actors=alice,bob error=ingest:unavailable",
        ),
        (
            {
                "status": "failed",
                "applied_actions": 1,
                "failed_actors": ["carol"],
                "error": "explicit:failure",
                "fatal_failure": "fallback:must-not-win",
            },
            "status=failed applied_actions=1 "
            "failed_actors=carol error=explicit:failure",
        ),
    ],
)
def test_isolated_cli_plain_apply_summary_uses_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    result: dict[str, object],
    expected_output: str,
) -> None:
    _run_stubbed_apply_cli(tmp_path, result=result, json_output=False)

    assert capsys.readouterr().out.strip() == expected_output
