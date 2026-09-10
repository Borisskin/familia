from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nanobot.agent.tools.context import RequestContext, ToolContext, request_context
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronPayload, CronSchedule

from familia import bootstrap
from familia.principals import Identity, Principal, PrincipalRegistry


def _registry() -> PrincipalRegistry:
    return PrincipalRegistry(
        [
            Principal(
                id="owner",
                identities=[
                    Identity(channel="telegram", sender_id="100|thread"),
                    Identity(channel="vk", sender_id="owner-vk"),
                ],
            ),
            Principal(
                id="member",
                identities=[
                    Identity(channel="telegram", sender_id="200"),
                    Identity(channel="discord", sender_id="100"),
                ],
            ),
            Principal(
                id="admin",
                identities=[Identity(channel="vk", sender_id="admin-vk")],
            ),
        ]
    )


def _request(
    actor: str,
    channel: str,
    chat_id: str,
    *,
    metadata: dict[str, str] | None = None,
) -> RequestContext:
    return RequestContext(
        channel=channel,
        chat_id=chat_id,
        actor=actor,
        session_key=f"familia:{actor}:{channel}:{chat_id}",
        metadata=metadata or {},
    )


def _execute(tool: CronTool, context: RequestContext, **params: object) -> str:
    with request_context(context):
        return asyncio.run(tool.execute(**params))


def _familia_cron_tool(tmp_path, monkeypatch) -> tuple[CronService, CronTool]:
    monkeypatch.setattr("familia.principals._registry", _registry())
    monkeypatch.setattr(
        "familia.roles.get_effective_roles",
        lambda actor: {"admin"} if actor == "admin" else set(),
    )
    monkeypatch.setattr(
        bootstrap,
        "make_reachable_tags_getter",
        lambda: lambda actor: {"shared"} if actor == "member" else set(),
    )

    service = CronService(tmp_path / "cron" / "jobs.json")
    service._running = True
    service._arm_timer = lambda: None
    monkeypatch.setattr(
        bootstrap,
        "_runtime_service_hooks",
        lambda _config, _bus: {
            name: lambda *_args: None
            for name in ("run_dream", "run_heartbeat", "run_scheduled")
        },
    )
    config = SimpleNamespace(workspace_path=tmp_path)
    context = ToolContext(
        config=config,
        workspace=str(tmp_path),
        cron_service=service,
    )
    registry = ToolRegistry()
    adapters = bootstrap.make_runtime_adapters(config)
    assert adapters.install_tools is not None
    adapters.install_tools(context, registry)
    ToolLoader(test_classes=[CronTool]).load(context, registry)
    tool = registry.get("cron")

    assert callable(context.cron_job_access)
    assert isinstance(tool, CronTool)
    assert tool._job_access is context.cron_job_access
    return service, tool


def test_familia_loader_preserves_cron_access_and_bound_owner_on_restart(tmp_path, monkeypatch) -> None:
    service, tool = _familia_cron_tool(tmp_path, monkeypatch)
    owner_telegram = _request("owner", "telegram", "100")

    created = _execute(
        tool,
        owner_telegram,
        action="add",
        message="owner reminder",
        every_seconds=60,
    )
    job = service.list_jobs()[0]

    assert job.id in created
    assert (
        job.payload.created_by,
        job.payload.creator_actor,
        job.payload.owner_actor,
        job.payload.target_actor,
    ) == ("owner", "owner", "owner", "owner")
    assert job.id in _execute(tool, _request("owner", "vk", "owner-vk"), action="list")
    telegram_legacy = service.add_job(
        name="telegram legacy recipient",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="recipient route",
        session_key="telegram:100",
        origin_channel="telegram",
        origin_chat_id="100",
    )
    assert telegram_legacy.id in _execute(tool, owner_telegram, action="list")

    restarted = CronService(tmp_path / "cron" / "jobs.json")
    loaded = restarted.get_job(job.id)
    assert loaded is not None
    assert (
        loaded.payload.created_by,
        loaded.payload.creator_actor,
        loaded.payload.owner_actor,
        loaded.payload.target_actor,
    ) == ("owner", "owner", "owner", "owner")
    restored_tool = CronTool(restarted, job_access=tool._job_access)
    assert job.id in _execute(restored_tool, _request("owner", "vk", "owner-vk"), action="list")


def test_familia_cron_access_hides_foreign_and_system_jobs_without_leaks(tmp_path, monkeypatch) -> None:
    service, tool = _familia_cron_tool(tmp_path, monkeypatch)
    owner = _request("owner", "telegram", "100")
    _execute(tool, owner, action="add", message="private", every_seconds=60)
    private = service.list_jobs()[0]
    service.add_job(
        name="tagged legacy",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="legacy",
        session_key="familia:owner:telegram:100",
        origin_channel="telegram",
        origin_chat_id="100",
        tags=["shared"],
    )
    service.register_system_job(
        CronJob(
            id="system",
            name="dream",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            payload=CronPayload(kind="system_event"),
        )
    )

    member = _request("member", "discord", "100", metadata={"actor": "owner"})
    member_jobs = _execute(tool, member, action="list")
    assert private.id not in member_jobs
    assert "tagged legacy" in member_jobs
    assert "dream" not in member_jobs
    assert _execute(tool, member, action="remove", job_id=private.id) == f"Job {private.id} not found"
    assert service.get_job(private.id) is not None
    assert _execute(tool, member, action="remove", job_id="system") == "Job system not found"

    from familia.nanobot_extension.cron import make_cron_job_access

    tag_lookups: list[str | None] = []

    def unexpected_tag_lookup(actor):
        tag_lookups.append(actor)
        return set()

    empty_tags_access = make_cron_job_access(
        is_admin=lambda _actor: False,
        reachable_tags=unexpected_tag_lookup,
    )
    assert not empty_tags_access(private, member)
    assert tag_lookups == []

    unknown = _request("unknown", "telegram", "100")
    assert _execute(tool, unknown, action="list") == "No scheduled jobs."
    assert _execute(tool, unknown, action="remove", job_id=private.id) == f"Job {private.id} not found"

    admin = _request("admin", "vk", "admin-vk")
    assert "dream" in _execute(tool, admin, action="list")
    assert _execute(tool, admin, action="remove", job_id="system") == (
        "Cannot remove job `dream`.\n"
        "This is a system-managed Dream memory consolidation job for long-term memory.\n"
        "It remains visible so you can inspect it, but it cannot be removed."
    )


def test_standalone_cron_tool_stays_unfiltered(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    service._running = True
    service._arm_timer = lambda: None
    job = service.add_job(
        name="standalone",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="all users can manage this",
        session_key="discord:100",
        origin_channel="discord",
        origin_chat_id="100",
    )

    tool = CronTool(service)

    assert job.id in tool._list_jobs()
    assert tool._remove_job(job.id) == f"Removed job {job.id}"
