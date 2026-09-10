"""Cron adapters owned by familia.

Nanobot owns the cron engine and the generic Dream/heartbeat runners. Familia
owns per-principal storage choices: which actor's heartbeat to read and which
Dream write tool can write scoped facts to memX.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from loguru import logger

from familia.acl.principal_memory import PrincipalMemoryClient
from familia.principals import (
    get_current_actor,
    get_current_channel,
    get_registry,
    set_current_actor,
    set_current_channel,
)


def make_cron_job_access(
    *,
    is_admin: Callable[[str | None], bool],
    reachable_tags: Callable[[str | None], set[str]],
) -> Callable[[Any, Any], bool]:
    """Return the Familia visibility predicate for user-created cron jobs."""
    from familia.session_identity import parse_private_session_key

    def _identity_matches(identity: Any, channel: str, chat_id: str) -> bool:
        if getattr(identity, "channel", None) != channel:
            return False
        sender_id = str(getattr(identity, "sender_id", ""))
        return sender_id == chat_id or (
            channel == "telegram" and sender_id.split("|", 1)[0] == chat_id
        )

    def _payload_actor(payload: Any) -> str | None:
        registry = get_registry()
        for name in ("creator_actor", "owner_actor", "created_by"):
            actor = getattr(payload, name, None)
            if isinstance(actor, str) and registry.get(actor) is not None:
                return actor
        parsed = parse_private_session_key(getattr(payload, "session_key", ""))
        if parsed is not None and registry.get(parsed[0]) is not None:
            return parsed[0]
        return None

    def _route_matches(payload: Any, actor: str, channel: str, chat_id: str) -> bool:
        job_channel = getattr(payload, "origin_channel", None) or getattr(payload, "channel", None)
        job_chat_id = getattr(payload, "origin_chat_id", None) or getattr(payload, "to", None)
        if not isinstance(job_channel, str) or not isinstance(job_chat_id, str) or job_channel != channel:
            return False
        principal = get_registry().get(actor)
        if principal is None:
            return False
        return any(
            _identity_matches(identity, channel, chat_id)
            and _identity_matches(identity, job_channel, job_chat_id)
            for identity in principal.identities
        )

    def _can_access(job: Any, request: Any) -> bool:
        actor = getattr(request, "actor", None)
        if not isinstance(actor, str) or get_registry().get(actor) is None:
            return False
        if is_admin(actor):
            return True

        payload = getattr(job, "payload", None)
        if payload is None or getattr(payload, "kind", None) == "system_event":
            return False
        if _payload_actor(payload) == actor:
            return True
        target = getattr(payload, "target_actor", None)
        if isinstance(target, str) and target == actor and get_registry().get(target) is not None:
            return True

        channel = getattr(request, "channel", None)
        chat_id = getattr(request, "chat_id", None)
        if isinstance(channel, str) and isinstance(chat_id, str) and _route_matches(
            payload, actor, channel, chat_id
        ):
            return True
        tags = getattr(payload, "tags", None)
        if isinstance(tags, list) and tags:
            try:
                return bool(set(tags) & set(reachable_tags(actor) or ()))
            except Exception:
                return False
        return False

    return _can_access


def make_heartbeat_source_reader(target_actor: str | None) -> Callable[[], tuple[str | None, str | None]]:
    """Return a fail-closed reader for ``value:heartbeat`` in a principal namespace.

    Once this reader is installed, familia owns heartbeat source selection
    completely. Empty memX content, a missing principal, a missing memX key, or
    a read failure must not fall back to ``HEARTBEAT.md``; otherwise stale file
    content can re-fire after its cron equivalent already exists.
    """
    actor_id = (target_actor or "").strip()

    @contextmanager
    def _execution_context() -> Iterator[None]:
        principal = get_registry().get(actor_id) if actor_id else None
        if principal is None or not principal.memx_key:
            raise RuntimeError("heartbeat target actor is missing or invalid")
        previous_actor = get_current_actor()
        set_current_actor(actor_id)
        try:
            yield
        finally:
            set_current_actor(previous_actor)

    def _read() -> tuple[str | None, str | None]:
        if not actor_id:
            return None, None
        principal = get_registry().get(actor_id)
        if principal is None or not principal.memx_key:
            return None, None
        try:
            # Familia heartbeat is stored beside the principal's other private
            # values, so the gateway tick reads the same memX namespace that
            # graph/admin tools maintain.
            text = PrincipalMemoryClient(actor_id, principal.memx_key).get("value:heartbeat")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Heartbeat: memX read failed for {}: {}", actor_id, exc)
            return None, None
        if text and text.strip():
            return text, "memx"
        return None, None

    # HeartbeatService consumes these neutral callable attributes without
    # importing familia. This keeps standalone nanobot behavior unchanged.
    _read.target_actor = actor_id or None
    _read.execution_context = _execution_context
    _read.requires_explicit_actor = True
    return _read


def _job_actor_and_session(job: Any) -> tuple[str, str]:
    """Resolve a cron owner from trusted fields, never from channel/chat."""
    from familia.session_identity import (
        make_private_session_key,
        parse_private_session_key,
    )

    payload = getattr(job, "payload", None)
    session_key = getattr(payload, "session_key", None)
    if not isinstance(session_key, str) or not session_key.strip():
        raise ValueError(f"cron job {getattr(job, 'id', '?')} is missing payload.session_key")
    session_key = session_key.strip()

    declared_actor: str | None = None
    for field_name in ("target_actor", "owner_actor", "creator_actor"):
        value = getattr(payload, field_name, None)
        if isinstance(value, str) and value.strip():
            declared_actor = value.strip()
            if field_name in {"target_actor", "owner_actor"}:
                break

    origin_metadata = getattr(payload, "origin_metadata", None)
    origin_actor: str | None = None
    if isinstance(origin_metadata, Mapping) and "actor" in origin_metadata:
        value = origin_metadata.get("actor")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("cron job origin actor is invalid")
        origin_actor = value.strip()

    parsed = parse_private_session_key(session_key)
    actor = declared_actor or origin_actor or (parsed[0] if parsed else None)
    if not actor or get_registry().get(actor) is None:
        raise ValueError("cron job has no registered owner actor")
    if parsed is not None and parsed[0] != actor:
        raise ValueError("cron job owner does not match its private session")

    if origin_actor is not None and origin_actor != actor:
        raise ValueError("cron job origin actor does not match its owner")

    origin_channel = getattr(payload, "origin_channel", None)
    origin_chat_id = getattr(payload, "origin_chat_id", None)
    if not isinstance(origin_channel, str) or not isinstance(origin_chat_id, str):
        raise TypeError("cron job is missing its original delivery route")
    principal = get_registry().get(actor)
    assert principal is not None  # guarded above
    if not any(
        identity.channel == origin_channel
        and (
            str(identity.sender_id) == origin_chat_id
            or (
                identity.channel == "telegram"
                and str(identity.sender_id).split("|", 1)[0] == origin_chat_id
            )
        )
        for identity in principal.identities
    ):
        raise ValueError("cron job origin route does not belong to its owner")

    private_key = session_key if parsed is not None else make_private_session_key(actor, session_key)
    return actor, private_key


def make_scheduled_handler(agent: Any, cron: Any) -> Callable[[Any], Any]:
    """Return the gateway's owner-scoped handler for bound cron jobs.

    The nanobot runner remains responsible for prompt rendering, run records,
    and turn execution. This adapter only supplies the server-verified actor
    and converts legacy/session keys into Familia's private form.
    """
    async def _run(job: Any) -> Any:
        from nanobot.cron.bound_runner import run_bound_cron_job

        owner, private_key = _job_actor_and_session(job)
        payload = getattr(job, "payload", None)
        run_job = job
        if payload is not None and getattr(payload, "session_key", None) != private_key:
            run_job = replace(job, payload=replace(payload, session_key=private_key))

        origin_channel = getattr(payload, "origin_channel", None)
        previous_actor = get_current_actor()
        previous_channel = get_current_channel()
        set_current_actor(owner)
        set_current_channel(origin_channel)

        class _ScopedAgent:
            tools = getattr(agent, "tools", None)

            async def submit_cron_turn(self, msg: Any) -> Any:
                # The core runner does not know Familia's actor field yet; set
                # it on the server-created message before entering the loop.
                sender_id = getattr(msg, "sender_id", None)
                if not isinstance(sender_id, str) or sender_id == "cron":
                    sender_id = str(getattr(msg, "chat_id", ""))
                from nanobot.agent.tools.context import (
                    RUNTIME_REQUEST_CONTEXT_KEY,
                    RequestContext,
                )

                from familia.session_identity import make_private_session_key

                channel = str(getattr(msg, "channel", ""))
                chat_id = str(getattr(msg, "chat_id", ""))
                route_key = make_private_session_key(owner, f"{channel}:{chat_id}")
                metadata = dict(getattr(msg, "metadata", {}) or {})
                metadata[RUNTIME_REQUEST_CONTEXT_KEY] = RequestContext(
                    channel=channel,
                    chat_id=chat_id,
                    session_key=route_key,
                    original_user_text=getattr(msg, "content", None),
                    metadata={"actor": owner},
                    sender_id=sender_id,
                    actor=owner,
                )
                admitted = replace(
                    msg,
                    sender_id=sender_id,
                    actor=owner,
                    metadata=metadata,
                    session_key_override=route_key,
                )
                return await agent.submit_cron_turn(admitted)

        try:
            return await run_bound_cron_job(
                run_job,
                agent=_ScopedAgent(),
                cron=cron,
            )
        finally:
            set_current_actor(previous_actor)
            set_current_channel(previous_channel)

    return _run


def make_dream_tool_installers(
    *,
    server_principal_getter: Callable[[], Any] | None = None,
    server_topic_validator: Callable[[str], bool] | None = None,
    profile_version_getter: Callable[[], Any] | None = None,
    result_tracker: Callable[[Any], None] | None = None,
) -> list[Callable[[Any, Any], None]]:
    """Return the configured Familia automatic-memory tool installer.

    Dream still runs in nanobot, but the memX write path is familia-specific.
    The integration removes nanobot's workspace-wide editor before adding the
    owner-bound atomic writer; standalone nanobot keeps its default editor.
    """

    def _install(registry: Any, _memory_store: Any) -> None:
        # Import lazily so importing the cron adapter does not construct tool
        # dependencies until nanobot is actually building the Dream registry.
        from familia.memx_client import memx_base_url
        from familia.principal_memory_ingestor import PrincipalMemoryIngestor
        from familia.tools.dream_memory import DreamMemorySetTool

        class FamiliaDreamMemorySetTool(DreamMemorySetTool):
            """Dream writer that reports its real commit result to the runner."""

            async def execute(self, *args: Any, **kwargs: Any) -> Any:
                previous_actor = get_current_actor()
                set_current_actor("dream_consolidator")
                try:
                    result = await super().execute(*args, **kwargs)
                    if result_tracker is not None:
                        result_tracker(result)
                    return result
                except Exception:
                    if result_tracker is not None:
                        result_tracker("Error: Dream automatic memory writer failed")
                    raise
                finally:
                    set_current_actor(previous_actor)

        principal_getter = server_principal_getter
        if principal_getter is None:
            from familia.bootstrap import make_dream_server_context_resolver

            principal_getter = make_dream_server_context_resolver()
        ingestor = PrincipalMemoryIngestor(
            base_url=memx_base_url(),
            api_key=os.environ.get("DREAM_CONSOLIDATOR_MEMX_KEY", ""),
            server_topic_validator=server_topic_validator,
        )
        for name in ("read_file", "edit_file", "write_file"):
            registry.unregister(name)
        registry.register(
            FamiliaDreamMemorySetTool(
                ingestor=ingestor,
                server_principal_getter=principal_getter,
                profile_version_getter=profile_version_getter,
            )
        )

    return [_install]
