"""Actor isolation and quarantine contract for Recent History prompts."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore


KNOWN_ACTORS = {"actor_a", "actor_b"}


def _is_known_actor(actor: str) -> bool:
    return actor in KNOWN_ACTORS


def _builder(tmp_path: Path) -> ContextBuilder:
    return ContextBuilder(tmp_path, history_actor_validator=_is_known_actor)


def _mark_last_entry(store: MemoryStore, **fields: object) -> None:
    entries = store._read_entries()
    entries[-1].update(fields)
    store._write_entries(entries)


def test_actor_a_prompt_never_contains_actor_b_history(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    builder.memory.append_history("A_ONLY", actor="actor_a")
    builder.memory.append_history("B_SECRET", actor="actor_b")
    builder.memory.append_history("SAFE_SYSTEM")
    _mark_last_entry(
        builder.memory,
        prompt_scope="system",
        prompt_safe_version="history-prompt-safe-v1",
    )

    prompt = builder.build_system_prompt(actor="actor_a")

    assert "A_ONLY" in prompt
    assert "SAFE_SYSTEM" in prompt
    assert "B_SECRET" not in prompt


def test_limit_is_applied_after_actor_filter(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    builder.memory.append_history("A_OLD_BUT_VISIBLE", actor="actor_a")
    for idx in range(builder._MAX_RECENT_HISTORY + 5):
        builder.memory.append_history(f"B_{idx}", actor="actor_b")

    prompt = builder.build_system_prompt(actor="actor_a")

    assert "A_OLD_BUT_VISIBLE" in prompt
    assert "B_54" not in prompt


def test_actorless_multi_principal_row_is_quarantined(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    actorless_cursor = store.append_history("ACTORLESS_SECRET")
    unknown_cursor = store.append_history("UNKNOWN_SECRET", actor="ghost")
    store.append_history("OTHER_VALID", actor="actor_b")

    selection = store.read_history_for_prompt(
        since_cursor=0,
        actor="actor_a",
        actor_validator=_is_known_actor,
    )

    assert selection["entries"] == []
    quarantine = selection["quarantine"]
    assert quarantine["version"] == "history-quarantine-v1"
    assert quarantine["records"] == [
        {
            "cursor": actorless_cursor,
            "reason": "actorless_multi_principal",
            "disposition": "quarantine_needs_review",
            "writes": 0,
        },
        {
            "cursor": unknown_cursor,
            "reason": "unknown_actor_multi_principal",
            "disposition": "quarantine_needs_review",
            "writes": 0,
        },
    ]


@pytest.mark.parametrize("actor", [None, "", "ghost"])
def test_missing_actor_fails_closed(tmp_path: Path, actor: str | None) -> None:
    builder = _builder(tmp_path)
    builder.memory.append_history("A_SECRET", actor="actor_a")
    builder.memory.append_history("B_SECRET", actor="actor_b")

    prompt = builder.build_system_prompt(actor=actor)

    assert "# Recent History" not in prompt
    assert "A_SECRET" not in prompt
    assert "B_SECRET" not in prompt


def test_explicit_standalone_actorless_compatibility(tmp_path: Path) -> None:
    builder = ContextBuilder(tmp_path)
    builder.memory.append_history("LOCAL_ACTORLESS")

    prompt = builder.build_system_prompt(actor=None)

    assert "# Recent History" in prompt
    assert "LOCAL_ACTORLESS" in prompt
