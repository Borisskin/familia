"""Cron adapters owned by familia.

Nanobot owns the cron engine and the generic Dream/heartbeat runners. Familia
owns per-principal storage choices: which actor's heartbeat to read and which
Dream write tool can write scoped facts to memX.
"""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
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


def _identity_matches(identity: Any, channel: str, chat_id: str) -> bool:
    if getattr(identity, "channel", None) != channel:
        return False
    sender_id = str(getattr(identity, "sender_id", ""))
    return sender_id == chat_id or (
        channel == "telegram" and sender_id.split("|", 1)[0] == chat_id
    )


def make_cron_job_access(
    *,
    is_admin: Callable[[str | None], bool],
    reachable_tags: Callable[[str | None], set[str]],
) -> Callable[[Any, Any], bool]:
    """Return the Familia visibility predicate for user-created cron jobs."""
    from familia.session_identity import parse_private_session_key

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
            except Exception:  # noqa: BLE001
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
    """Resolve the executor without inferring it from the delivery route."""
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

    origin_channel = getattr(payload, "origin_channel", None)
    origin_chat_id = getattr(payload, "origin_chat_id", None)
    if not isinstance(origin_channel, str) or not isinstance(origin_chat_id, str):
        raise TypeError("cron job is missing its original delivery route")
    parsed = parse_private_session_key(session_key)
    registry = get_registry()
    legacy_creator = getattr(payload, "created_by", None)
    if (
        not declared_actor
        and origin_actor is None
        and parsed is None
        and isinstance(legacy_creator, str)
        and legacy_creator.strip()
        and session_key == f"{origin_channel}:{origin_chat_id}"
    ):
        actor = legacy_creator.strip()
        if registry.get(actor) is None:
            raise ValueError("cron job has no registered owner actor")
        if registry.resolve_unique(origin_channel, origin_chat_id) is None:
            raise ValueError("cron job origin route has no unique registered recipient")
        return actor, make_private_session_key(actor, session_key)

    actor = declared_actor or origin_actor or (parsed[0] if parsed else None)
    if not actor or registry.get(actor) is None:
        raise ValueError("cron job has no registered owner actor")
    if parsed is not None and parsed[0] != actor:
        raise ValueError("cron job owner does not match its private session")
    if origin_actor is not None and origin_actor != actor:
        raise ValueError("cron job origin actor does not match its owner")

    principal = registry.get(actor)
    assert principal is not None  # guarded above
    if not any(_identity_matches(identity, origin_channel, origin_chat_id) for identity in principal.identities):
        raise ValueError("cron job origin route does not belong to its owner")

    private_key = session_key if parsed is not None else make_private_session_key(actor, session_key)
    return actor, private_key


@dataclass
class _CronDeliveryContext:
    job_id: str
    run_id: str
    creator: str
    origin_channel: str
    origin_chat_id: str
    candidates: tuple[tuple[str, str], ...] = ()
    candidate_index: int = 0
    pending_route: tuple[str, str] | None = None
    finished: bool = False


class _CronDeliveryObserver:
    """Try one recipient-owned route after a known final-delivery failure."""

    def __init__(
        self,
        publish_outbound: Callable[..., Any],
        enabled_channels: Callable[[], list[str]],
    ) -> None:
        self._publish_outbound = publish_outbound
        self._enabled_channels = enabled_channels
        # ponytail: retain 256 run contexts; add durable receipts only for cross-process deduplication.
        self._contexts: OrderedDict[tuple[str, str], _CronDeliveryContext] = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _run_key(message: Any) -> tuple[str, str] | None:
        from nanobot.cron.session_turns import CRON_TRIGGER_META

        metadata = getattr(message, "metadata", None)
        trigger = metadata.get(CRON_TRIGGER_META) if isinstance(metadata, Mapping) else None
        if not isinstance(trigger, Mapping):
            return None
        job_id = trigger.get("job_id")
        run_id = trigger.get("run_id")
        if (
            not isinstance(job_id, str)
            or not job_id
            or not isinstance(run_id, str)
            or not run_id
        ):
            return None
        return job_id, run_id

    @staticmethod
    def _is_final(message: Any) -> bool:
        event = getattr(message, "event", None)
        if event is None:
            return True
        from nanobot.bus.outbound_events import StreamedResponseEvent

        return isinstance(event, StreamedResponseEvent)

    def register(self, message: Any, creator: str) -> None:
        """Record the server-verified cron context immediately before submit."""
        key = self._run_key(message)
        channel = getattr(message, "channel", None)
        chat_id = getattr(message, "chat_id", None)
        if (
            key is None
            or not isinstance(creator, str)
            or not creator
            or not isinstance(channel, str)
            or not channel
            or not isinstance(chat_id, str)
            or not chat_id
        ):
            return
        if key in self._contexts:
            return
        self._contexts[key] = _CronDeliveryContext(*key, creator, channel, chat_id)
        while len(self._contexts) > 256:
            self._contexts.popitem(last=False)

    def _alternatives(self, context: _CronDeliveryContext) -> tuple[tuple[str, str], ...]:
        recipient = get_registry().resolve_unique(context.origin_channel, context.origin_chat_id)
        principal = get_registry().get(recipient) if recipient is not None else None
        if principal is None:
            return ()
        enabled = set(self._enabled_channels())
        routes: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for identity in principal.identities:
            channel = getattr(identity, "channel", None)
            chat_id = getattr(identity, "sender_id", None)
            if not isinstance(channel, str) or channel == context.origin_channel or channel not in enabled:
                continue
            if not isinstance(chat_id, str) or not chat_id:
                continue
            if channel == "telegram":
                chat_id = chat_id.split("|", 1)[0]
            route = (channel, chat_id)
            if chat_id and route not in seen:
                seen.add(route)
                routes.append(route)
        return tuple(routes)

    async def _publish_next(self, context: _CronDeliveryContext, message: Any) -> None:
        from nanobot.agent.outbound import OutboundDecision
        from nanobot.agent.tools.context import (
            RequestContext,
            bind_request_context,
            reset_request_context,
        )
        from nanobot.bus.events import InboundMessage

        if context.candidate_index >= len(context.candidates):
            context.finished = True
            return
        channel, chat_id = context.candidates[context.candidate_index]
        context.candidate_index += 1
        inbound = InboundMessage(
            channel=context.origin_channel,
            sender_id="cron",
            chat_id=context.origin_chat_id,
            content="",
            actor=context.creator,
            metadata={},
        )
        previous_actor = get_current_actor()
        previous_channel = get_current_channel()
        request_token = bind_request_context(
            RequestContext(
                channel=context.origin_channel,
                chat_id=context.origin_chat_id,
                metadata={"_familia_server_cron": True},
                sender_id="cron",
                actor=context.creator,
            )
        )
        try:
            set_current_actor(context.creator)
            set_current_channel(context.origin_channel)
            decision = await self._publish_outbound(
                replace(message, channel=channel, chat_id=chat_id),
                actor=context.creator,
                inbound=inbound,
                action="message.send",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Cron delivery fallback could not enter outbound policy")
            context.finished = True
            return
        finally:
            reset_request_context(request_token)
            set_current_actor(previous_actor)
            set_current_channel(previous_channel)
        if isinstance(decision, OutboundDecision) and decision.kind == "allow":
            context.pending_route = (channel, chat_id)
            return
        context.finished = True

    async def __call__(self, message: Any, result: str) -> None:
        if result not in {"delivered", "unavailable", "unknown"} or not self._is_final(message):
            return
        key = self._run_key(message)
        if key is None:
            return
        route = (getattr(message, "channel", None), getattr(message, "chat_id", None))
        if not all(isinstance(value, str) and value for value in route):
            return
        async with self._lock:
            context = self._contexts.get(key)
            if context is None or context.finished:
                return
            self._contexts.move_to_end(key)
            expected_route = context.pending_route or (context.origin_channel, context.origin_chat_id)
            if route != expected_route:
                return
            if result != "unavailable":
                context.finished = True
                return
            if context.pending_route is None:
                context.candidates = self._alternatives(context)
            await self._publish_next(context, message)


def make_cron_delivery_observer(
    publish_outbound: Callable[..., Any],
    cron: Any,
    enabled_channels: Callable[[], list[str]],
) -> Callable[[Any, str], Any]:
    """Create and attach the one process-local observer for bound cron runs."""
    observer = _CronDeliveryObserver(publish_outbound, enabled_channels)
    cron._familia_cron_delivery_observer = observer
    return observer


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
                from nanobot.agent.outbound import RUNTIME_REQUEST_CONTEXT_KEY
                from nanobot.agent.tools.context import RequestContext

                from familia.session_identity import make_private_session_key

                channel = str(getattr(msg, "channel", ""))
                chat_id = str(getattr(msg, "chat_id", ""))
                route_key = getattr(msg, "session_key_override", None)
                if not isinstance(route_key, str) or not route_key:
                    route_key = make_private_session_key(owner, f"{channel}:{chat_id}")
                metadata = dict(getattr(msg, "metadata", {}) or {})
                metadata[RUNTIME_REQUEST_CONTEXT_KEY] = RequestContext(
                    channel=channel,
                    chat_id=chat_id,
                    session_key=route_key,
                    original_user_text=getattr(msg, "content", None),
                    metadata={"actor": owner, "_familia_server_cron": True},
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
                observer = getattr(cron, "_familia_cron_delivery_observer", None)
                register = getattr(observer, "register", None)
                if callable(register):
                    register(admitted, owner)
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
