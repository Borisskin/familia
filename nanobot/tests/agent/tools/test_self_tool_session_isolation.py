"""Session isolation and redaction checks for MyTool."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.subagent import SubagentManager, SubagentStatus
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.self import MyTool
from nanobot.bus.queue import MessageBus


def _status(task_id: str, session_key: str | None, label: str, description: str) -> SubagentStatus:
    return SubagentStatus(
        task_id=task_id,
        label=label,
        task_description=description,
        started_at=time.monotonic(),
        session_key=session_key,
        phase="awaiting_tools",
    )


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        model="test-model",
        model_preset=None,
        max_iterations=40,
        context_window_tokens=65_536,
        workspace=Path("/tmp/workspace"),
        provider_retry_mode="standard",
        max_tool_result_chars=16_000,
        _current_iteration=0,
        web_config=SimpleNamespace(enable=True),
        exec_config=SimpleNamespace(
            enable=True,
            env={
                "MEMX_API_KEY": "memx-secret-canary",
                "OPENAI_API_KEY": "openai-secret-canary",
                "PATH": "/tmp/bin",
            },
        ),
        workspace_sandbox=SimpleNamespace(enabled=False),
        _last_usage={},
        _runtime_vars={},
        subagents=SimpleNamespace(
            _task_statuses={
                "own-id": _status("own-id", "session-a", "own-label", "own-description"),
                "foreign-id": _status(
                    "foreign-id",
                    "session-b",
                    "foreign-label",
                    "foreign-description",
                ),
                "unknown-id": _status(
                    "unknown-id",
                    None,
                    "unknown-label",
                    "unknown-description",
                ),
            },
            _running_tasks={"foreign-id": object()},
        ),
    )


def _ctx(session_key: str | None) -> RequestContext:
    return RequestContext(
        channel="test",
        chat_id=session_key or "unknown",
        sender_id="sender",
        session_key=session_key,
    )


@dataclass
class _CronJob:
    session_key: str
    payload: dict[str, str]


@pytest.mark.asyncio
async def test_active_session_filters_overview_and_nested_status_paths() -> None:
    tool = MyTool(_state())

    with request_context(_ctx("session-a")):
        overview = await tool.execute(action="check")
        own = await tool.execute(
            action="check",
            key="subagents._task_statuses.own-id.task_description",
        )
        foreign = await tool.execute(
            action="check",
            key="subagents._task_statuses.foreign-id",
        )
        raw_manager = await tool.execute(action="check", key="subagents._running_tasks")

    assert "own-label" in overview
    assert "own-description" in overview
    assert "foreign-label" not in overview
    assert "foreign-description" not in overview
    assert "unknown-label" not in overview
    assert "own-description" in own
    assert "foreign-label" not in foreign
    assert "foreign-description" not in foreign
    assert "not accessible" in raw_manager


@pytest.mark.asyncio
async def test_scratchpad_is_partitioned_by_session() -> None:
    state = _state()
    tool = MyTool(state)

    with request_context(_ctx("session-a")):
        assert "Set scratchpad.note" in await tool.execute(
            action="set",
            key="note",
            value="A-only",
        )
        assert "A-only" in await tool.execute(action="check", key="scratchpad.note")

    with request_context(_ctx("session-b")):
        assert await tool.execute(action="check", key="scratchpad") == "scratchpad is empty"
        await tool.execute(action="set", key="note", value="B-only")
        assert "B-only" in await tool.execute(action="check", key="scratchpad.note")

    with request_context(_ctx("session-a")):
        assert "A-only" in await tool.execute(action="check", key="scratchpad.note")
        assert "B-only" not in await tool.execute(action="check", key="scratchpad")

    assert state._runtime_vars == {}


@pytest.mark.asyncio
async def test_unknown_context_hides_session_state_and_rejects_shared_scratchpad_write() -> None:
    state = _state()
    tool = MyTool(state)
    with request_context(_ctx("session-a")):
        await tool.execute(action="set", key="note", value="A-only")

    with request_context(_ctx(None)):
        overview = await tool.execute(action="check")
        hidden = await tool.execute(
            action="check",
            key="subagents._task_statuses.own-id.task_description",
        )
        scratchpad = await tool.execute(action="check", key="scratchpad")
        write = await tool.execute(action="set", key="note", value="leak")
        model_write = await tool.execute(action="set", key="model", value="other-model")

    assert "own-label" not in overview
    assert "A-only" not in overview
    assert "not found" in hidden or "not accessible" in hidden
    assert scratchpad == "scratchpad is empty"
    assert "server session key" in write
    assert "instance-wide" in model_write
    assert state._runtime_vars == {}


@pytest.mark.asyncio
async def test_nested_exec_env_redacts_api_key_values() -> None:
    tool = MyTool(_state())
    with request_context(_ctx("session-a")):
        env = await tool.execute(action="check", key="exec_config.env")
        direct = await tool.execute(action="check", key="exec_config.env.MEMX_API_KEY")

    assert "memx-secret-canary" not in env
    assert "openai-secret-canary" not in env
    assert "/tmp/bin" in env
    assert "not accessible" in direct


@pytest.mark.asyncio
async def test_allow_set_false_still_rejects_mutation_in_active_session() -> None:
    tool = MyTool(_state(), modify_allowed=False)
    with request_context(_ctx("session-a")):
        result = await tool.execute(action="set", key="note", value="nope")
    assert "allow_set is false" in result


@pytest.mark.asyncio
async def test_active_session_projects_runtime_and_rejects_unknown_roots() -> None:
    state = _state()
    state.cron_service = SimpleNamespace(
        _store=SimpleNamespace(
            jobs=[
                _CronJob(
                    session_key="session-b",
                    payload={"message": "foreign cron payload"},
                ),
            ],
        ),
    )
    state._exec_session_manager = SimpleNamespace(
        _sessions={"session-b": SimpleNamespace(output="foreign exec output")},
    )
    state._file_state_store = SimpleNamespace(_states={"session-b": object()})
    state.runtime_events = SimpleNamespace(manager=SimpleNamespace(events=[object()]))
    state.turn_delivery_factory = SimpleNamespace(manager=SimpleNamespace())
    state._deferred_automation_turns = {"session-b": [_CronJob("session-b", {})]}
    state._cron_turns = SimpleNamespace(manager=SimpleNamespace())
    state.future_shared_object = SimpleNamespace(payload="future shared value")

    tool = MyTool(state)
    with request_context(_ctx("session-a")):
        overview = await tool.execute(action="check")
        model = await tool.execute(action="check", key="model")
        config = await tool.execute(action="check", key="web_config.enable")
        environment = await tool.execute(action="check", key="exec_config.env")
        own_status = await tool.execute(
            action="check",
            key="subagents._task_statuses.own-id.task_description",
        )
        blocked = [
            await tool.execute(action="check", key=key)
            for key in (
                "cron_service._store.jobs",
                "_exec_session_manager._sessions",
                "_file_state_store._states",
                "runtime_events.manager",
                "turn_delivery_factory.manager",
                "_deferred_automation_turns",
                "_cron_turns.manager",
                "future_shared_object.payload",
            )
        ]

    assert "test-model" in model
    assert "True" in config
    assert "/tmp/bin" in environment
    assert "memx-secret-canary" not in environment
    assert "own-description" in own_status
    assert "own-label" in overview
    assert "foreign-description" not in overview
    assert all("not accessible" in result for result in blocked)
    assert all("foreign cron payload" not in result for result in blocked)
    assert all("foreign exec output" not in result for result in blocked)


@pytest.mark.asyncio
async def test_spawn_branches_record_session_provenance(tmp_path) -> None:
    manager = SubagentManager(
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=16_000,
    )
    release = asyncio.Event()

    async def hold(*_args, **_kwargs):
        await release.wait()
        return "done"

    manager._run_subagent = hold
    with request_context(_ctx("session-a")):
        await manager.spawn("background", session_key="session-a", runtime=object())
        assert [s.session_key for s in manager._task_statuses.values()] == ["session-a"]
        inline = asyncio.create_task(
            manager.run_inline("inline", session_key="session-a", runtime=object())
        )
        await asyncio.sleep(0)
        assert sorted(s.session_key for s in manager._task_statuses.values()) == [
            "session-a",
            "session-a",
        ]

    release.set()
    await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)
    await inline
