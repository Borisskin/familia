from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse


class _SyntheticProvider(LLMProvider):
    """Deterministic LLM endpoint; only model transport is synthetic."""

    def __init__(self) -> None:
        super().__init__()
        self.generation = GenerationSettings(max_tokens=1)
        self.fail_service_summary = False
        self.calls: list[dict[str, object]] = []
        self.prompt_estimates: list[int] = []

    def get_default_model(self) -> str:
        return "synthetic-cron-model"

    def estimate_prompt_tokens(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        model: str | None = None,
    ) -> tuple[int, str]:
        message_chars = sum(len(str(message.get("content", ""))) for message in messages)
        tool_chars = len(repr(tools or []))
        estimate = max(8, message_chars // 4 + tool_chars // 4 + len(messages) * 4)
        self.prompt_estimates.append(estimate)
        return estimate, "synthetic"

    async def chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        model: str | None = None,
        max_tokens: int = 1,
        temperature: float = 0.0,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, object] | None = None,
    ):
        self.calls.append({"tools": tools, "message_count": len(messages)})
        prompt_tokens, _ = self.estimate_prompt_tokens(messages, tools, model)
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 1}
        if tools is None:
            if self.fail_service_summary:
                return LLMResponse(
                    content="synthetic phase1 failure",
                    finish_reason="error",
                    usage=usage,
                )
            return LLMResponse(content="synthetic service summary", usage=usage)
        return LLMResponse(content="synthetic next output", usage=usage)


@pytest.mark.asyncio
async def test_saved_cron_reaches_real_loop_resolver_and_consolidator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise the CLI callback, ownership proof, service fallback and reload."""
    from familia.private_session_owner import PrivateSessionOwnerResolver
    from familia.principals import Identity, Principal, PrincipalRegistry
    from nanobot.agent.loop import AgentLoop as RealAgentLoop
    from nanobot.cli import commands as cli_commands
    from nanobot.config.schema import Config
    from nanobot.cron.service import CronService as RealCronService
    from nanobot.cron.types import CronSchedule
    # Keep the test's provider transport concrete while satisfying the
    # nanobot provider interface used by AgentRunner and Consolidator.
    provider = _SyntheticProvider()

    registry = PrincipalRegistry(
        [
            Principal(
                id="creator",
                identities=[Identity(channel="telegram", sender_id="2002")],
            ),
            Principal(
                id="recipient",
                identities=[Identity(channel="telegram", sender_id="1001")],
            ),
        ]
    )
    owner_resolver = PrivateSessionOwnerResolver(lambda: registry)
    archived: list[tuple[str, list[dict[str, object]]]] = []

    async def archive_sink(owner: str, messages: list[dict]) -> str:
        archived.append((owner, [dict(message) for message in messages]))
        return "synthetic private summary"

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    config.agents.defaults.model = "synthetic-cron-model"
    config.agents.defaults.context_window_tokens = 1_500
    config.agents.defaults.max_tool_iterations = 1
    config.agents.defaults.session_ttl_minutes = 1
    config.gateway.heartbeat.enabled = False

    store_path = config.workspace_path / "cron" / "jobs.json"
    monkeypatch.setattr(RealCronService, "_arm_timer", lambda self: None)
    seed = RealCronService(store_path)
    seed._running = True
    saved_job = seed.add_job(
        name="saved-recipient-reminder",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="preserve the latest output",
        deliver=False,
        channel="telegram",
        to="1001",
        created_by="creator",
        creator_actor="creator",
        target_actor="recipient",
    )
    reloaded = RealCronService(store_path).list_jobs(include_disabled=True)
    assert len(reloaded) == 1
    good_job = reloaded[0]
    assert good_job.id == saved_job.id
    assert good_job.payload.creator_actor == "creator"
    assert good_job.payload.target_actor == "recipient"
    assert good_job.payload.creator_actor != good_job.payload.target_actor

    bad_job = RealCronService(store_path).add_job(
        name="unproved-service-reminder",
        schedule=CronSchedule(kind="every", every_ms=120_000),
        message="do not write private history without proof",
        deliver=False,
        channel="telegram",
        to="9999",
        created_by="creator",
        creator_actor="creator",
    )

    seen: dict[str, object] = {}

    class _StopGateway(RuntimeError):
        pass

    class _CapturingCron(RealCronService):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            seen["cron"] = self

    def capture_agent(*args, **kwargs):
        agent = RealAgentLoop(*args, **kwargs)
        seen["agent"] = agent
        return agent

    class _StopBeforeGatewayRun:
        enabled_channels: list[str] = []

        def __init__(self, *_args, **_kwargs) -> None:
            raise _StopGateway("capture production callback before gateway run")

    monkeypatch.setattr("nanobot.cron.service.CronService", _CapturingCron)
    monkeypatch.setattr("nanobot.agent.loop.AgentLoop", capture_agent)
    monkeypatch.setattr("nanobot.channels.manager.ChannelManager", _StopBeforeGatewayRun)
    monkeypatch.setattr(cli_commands, "sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(cli_commands, "_make_provider", lambda _config: provider)
    monkeypatch.setattr(cli_commands, "make_channel_manager_kwargs", lambda: {})
    monkeypatch.setattr(cli_commands, "make_callback_handlers", lambda _bus: [])
    monkeypatch.setattr(cli_commands, "make_heartbeat_source_reader", lambda _actor: None)
    monkeypatch.setattr(
        cli_commands,
        "make_agent_loop_kwargs",
        lambda _workspace: {
            "direct_actor_resolver": registry.resolve,
            "current_actor_getter": lambda: "recipient",
            "private_session_owner_resolver": owner_resolver,
            "archive_sink": archive_sink,
        },
    )

    with pytest.raises(_StopGateway):
        cli_commands._run_gateway(config)

    cron = seen["cron"]
    agent = seen["agent"]
    assert isinstance(cron, _CapturingCron)
    assert isinstance(agent, RealAgentLoop)
    assert cron.on_job is not None

    pending: list[asyncio.Task] = []

    def schedule(coro) -> None:
        pending.append(asyncio.create_task(coro))

    agent._schedule_background = schedule

    async def drain() -> None:
        while pending:
            tasks, pending[:] = pending[:], []
            await asyncio.gather(*tasks)

    async def run(job, count: int = 1) -> list[str | None]:
        outputs = []
        for _ in range(count):
            outputs.append(await cron.on_job(job))
            await drain()
        return outputs

    good_outputs = await run(good_job, count=3)
    assert good_outputs[-1] == "synthetic next output"
    good_session = agent.sessions.get_or_create(f"cron:{good_job.id}")
    assert good_session.metadata["_private_session_route"] == {
        "channel": "telegram",
        "chat_id": "1001",
        "target_actor": "recipient",
    }
    production_budget = (
        config.agents.defaults.context_window_tokens
        - provider.generation.max_tokens
        - 1024
    )
    assert provider.prompt_estimates
    assert max(provider.prompt_estimates) > production_budget
    assert archived and {owner for owner, _ in archived} == {"recipient"}
    assert all(
        message.get("actor") in {None, "recipient"}
        for _, messages in archived
        for message in messages
    )
    assert good_session.last_consolidated > 0

    provider.fail_service_summary = True
    bad_session_key = f"cron:{bad_job.id}"
    await run(bad_job)
    bad_session = agent.sessions.get_or_create(bad_session_key)
    before_error_messages = [dict(message) for message in bad_session.messages]
    before_error_boundary = bad_session.last_consolidated
    with pytest.raises(RuntimeError, match="synthetic phase1 failure"):
        await run(bad_job)
    bad_session = agent.sessions.get_or_create(bad_session_key)
    assert bad_session.messages[: len(before_error_messages)] == before_error_messages
    assert len(bad_session.messages) > len(before_error_messages)
    assert bad_session.last_consolidated == before_error_boundary
    provider.fail_service_summary = False
    await run(bad_job)
    bad_session = agent.sessions.get_or_create(bad_session_key)
    assert bad_session.last_consolidated > before_error_boundary
    assert {owner for owner, _ in archived} == {"recipient"}
    memory_dir = config.workspace_path / "memory"
    assert not (memory_dir / "history.jsonl").exists()
    assert not (memory_dir / "MEMORY.md").exists()
    assert not (memory_dir / "private").exists()
    assert not (memory_dir / "shared").exists()

    # The idle path archives through the same real Consolidator, then a fresh
    # SessionManager reads the persisted suffix before the next cron run.
    bad_session.updated_at = datetime.now() - timedelta(minutes=5)
    agent.sessions.save(bad_session)
    agent.auto_compact.check_expired(schedule, active_session_keys=())
    await drain()
    from nanobot.session.manager import SessionManager

    restarted_sessions = SessionManager(config.workspace_path)
    restarted = restarted_sessions.get_or_create(bad_session_key)
    assert restarted.messages
    agent.sessions = restarted_sessions
    agent.consolidator.sessions = restarted_sessions
    agent.auto_compact.sessions = restarted_sessions
    assert await cron.on_job(good_job) == "synthetic next output"
    await drain()
