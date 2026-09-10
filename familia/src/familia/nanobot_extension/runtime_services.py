"""Familia-owned services exposed at the nanobot 0.3.0 adapter seam.

Nanobot owns the queues, channel discovery, and generic runners.  This module
only supplies product hooks: the external VK descriptor, callback handling,
actor-scoped policy delivery, and owner-safe heartbeat/cron helpers.
"""

from __future__ import annotations

import inspect
import json
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from typing import Any

from loguru import logger

from familia.nanobot_extension.cron import (
    make_heartbeat_source_reader,
    make_scheduled_handler,
)
from familia.policy import gate_outbound_send
from familia.principals import (
    get_current_actor,
    get_current_channel,
    get_registry,
    set_current_actor,
    set_current_channel,
)

_DREAM_MAX_ENTRIES = 20
_CONSOLIDATOR_ACTOR = "dream_consolidator"
_SYSTEM_JOB_ORIGIN_KEY = "_familia_system_job"
_HEARTBEAT_PREAMBLE = (
    "[Your response will be delivered directly to the user's messaging app. "
    "Output ONLY the final user-facing message. Never reference internal "
    "storage, instructions, or your decision process. If nothing needs "
    "reporting, respond with just 'All clear.' and nothing else.]\n\n"
)


_PLUGIN_LOCK = threading.RLock()
_EXTERNAL_DESCRIPTORS: dict[str, Any] = {}


def make_vk_channel_plugin() -> Any:
    """Build the external VK descriptor without importing its SDK at startup."""
    from nanobot.channels.plugin import ChannelPlugin

    return ChannelPlugin(
        name="vk",
        display_name="VK",
        runtime="familia.channels.vk:VKChannel",
        default_enabled=True,
        settings_visible=True,
        capabilities=frozenset({"text", "media", "buttons", "callbacks"}),
    )


def register_channel_descriptor(descriptor: Any) -> None:
    """Register one external descriptor, rejecting name collisions."""
    from nanobot.channels.plugin import ChannelPlugin

    if not isinstance(descriptor, ChannelPlugin):
        raise TypeError("channel descriptor must be a nanobot ChannelPlugin")
    name = descriptor.name
    with _PLUGIN_LOCK:
        if name == "vk" or name in _EXTERNAL_DESCRIPTORS:
            raise ValueError(f"channel descriptor name collision: {name}")
        _EXTERNAL_DESCRIPTORS[name] = descriptor


def channel_plugins(enabled: set[str] | None = None) -> Mapping[str, Any]:
    """Return validated Familia descriptors before nanobot discovery."""
    selected = set(enabled) if enabled is not None else None
    with _PLUGIN_LOCK:
        descriptors = {"vk": make_vk_channel_plugin(), **_EXTERNAL_DESCRIPTORS}
    if selected is None:
        return descriptors
    return {name: plugin for name, plugin in descriptors.items() if name in selected}


def resolve_heartbeat_target(
    target_actor: str, enabled_channels: set[str]
) -> tuple[str, str] | None:
    """Resolve a configured actor to one of its registered enabled identities."""
    actor = target_actor.strip() if isinstance(target_actor, str) else ""
    if not actor:
        return None
    principal = get_registry().get(actor)
    if principal is None:
        return None
    for identity in principal.identities:
        if identity.channel in enabled_channels and identity.sender_id:
            return identity.channel, str(identity.sender_id)
    return None


def _request_context() -> Any | None:
    try:
        from nanobot.agent.tools.context import current_request_context

        return current_request_context()
    except Exception:  # noqa: BLE001  # pragma: no cover - standalone nanobot import
        return None


def _trusted_actor(candidate: Any) -> tuple[str | None, bool]:
    """Return the current server actor and whether a candidate is trustworthy.

    ``OutboundRequest.actor`` is copied by nanobot from a routing envelope. It
    is useful as a consistency check, but it cannot establish identity on its
    own because an arbitrary tool may supply metadata. The active request
    context/ContextVar remains the authority.
    """
    current = get_current_actor()
    context = _request_context()
    context_actor = getattr(context, "actor", None) if context is not None else None
    if current is None and isinstance(context_actor, str) and context_actor.strip():
        current = context_actor.strip()
    if current is not None and get_registry().get(current) is None:
        return None, False
    if candidate is not None:
        if not isinstance(candidate, str) or not candidate.strip():
            return None, False
        candidate = candidate.strip()
        if get_registry().get(candidate) is None:
            return None, False
        if current is None or candidate != current:
            return None, False
    return current, True


def _origin(request: Any) -> tuple[str | None, str | None]:
    """Prefer the immutable request context over metadata route hints."""
    context = _request_context()
    if context is not None:
        channel = getattr(context, "channel", None)
        chat_id = getattr(context, "chat_id", None)
        if isinstance(channel, str) and isinstance(chat_id, str):
            return channel, chat_id
    channel = getattr(request, "inbound_channel", None)
    chat_id = getattr(request, "inbound_chat_id", None)
    return (
        channel if isinstance(channel, str) and channel else None,
        chat_id if isinstance(chat_id, str) and chat_id else None,
    )


def make_outbound_guard() -> Callable[[Any], Awaitable[Any]]:
    """Create the single Familia policy guard for all nanobot deliveries."""
    from nanobot.agent.outbound import OutboundDecision

    async def _guard(request: Any) -> Any:
        action = getattr(request, "action", None)
        outbound = getattr(request, "outbound", None)
        if not isinstance(action, str) or not action.strip() or outbound is None:
            return OutboundDecision.deny("исходящее действие не прошло проверку")

        actor, valid = _trusted_actor(getattr(request, "actor", None))
        inbound_channel, inbound_chat_id = _origin(request)
        target_is_origin = (
            inbound_channel == getattr(outbound, "channel", None)
            and inbound_chat_id == getattr(outbound, "chat_id", None)
        )
        if not valid or (actor is None and not target_is_origin):
            return OutboundDecision.deny("нет доверенной личности отправителя")

        previous_actor = get_current_actor()
        previous_channel = get_current_channel()
        set_current_actor(actor)
        set_current_channel(inbound_channel)
        publisher = getattr(request, "publish_outbound", None)
        if not callable(publisher):
            async def _unavailable(_message: Any) -> None:
                raise RuntimeError("исходящий издатель не настроен")

            publisher = _unavailable
        try:
            result = await gate_outbound_send(
                action=action.strip(),
                outbound=outbound,
                inbound_channel=inbound_channel,
                inbound_chat_id=inbound_chat_id,
                publish_outbound=publisher,
            )
            return OutboundDecision(
                kind=result.kind,
                reason=result.reason,
                approvers_label=result.approvers_label,
                outbound=outbound if result.kind == "allow" else None,
            )
        finally:
            set_current_actor(previous_actor)
            set_current_channel(previous_channel)

    return _guard


def make_callback_handler(bus: Any) -> Callable[[Any], Awaitable[None]]:
    """Return a neutral callback callable with an optional lifecycle handle."""
    from familia.bus.callback_dispatcher import CallbackDispatcher

    dispatcher = CallbackDispatcher(bus)

    async def _handle(event: Any) -> None:
        await dispatcher.handle_callback(event)

    # The gateway can start/stop the dispatcher when a callback queue exists;
    # direct callback dispatch remains usable on older buses.
    _handle.dispatcher = dispatcher  # type: ignore[attr-defined]
    _handle.start = dispatcher.start  # type: ignore[attr-defined]
    _handle.stop = dispatcher.stop  # type: ignore[attr-defined]
    return _handle


def _registered_actor(candidate: Any) -> str | None:
    """Return a canonical registry id, never an id supplied by metadata."""
    if not isinstance(candidate, str):
        return None
    actor = candidate.strip()
    if not actor or get_registry().get(actor) is None:
        return None
    return actor


def _identity_route(
    actor: str,
    *,
    enabled_channels: set[str] | None = None,
) -> tuple[str, str] | None:
    """Choose one registry identity for an internal turn.

    Heartbeat passes an explicit enabled-channel set before calling this helper;
    Dream only uses it to satisfy the core's admission envelope.  Neither path
    ever derives ownership from a ``channel:chat_id`` session key.
    """
    principal = get_registry().get(actor)
    if principal is None:
        return None
    for identity in principal.identities:
        channel = getattr(identity, "channel", None)
        sender_id = getattr(identity, "sender_id", None)
        if not isinstance(channel, str) or not channel or channel == "system":
            continue
        if enabled_channels is not None and channel not in enabled_channels:
            continue
        if isinstance(sender_id, str) and sender_id:
            return channel, sender_id
    return None


def _enabled_channels(loop: Any) -> set[str]:
    """Read channels already resolved by the gateway; never invent one."""
    candidates = [
        loop,
        getattr(loop, "channels", None),
        getattr(loop, "channel_manager", None),
    ]
    for candidate in candidates:
        value = getattr(candidate, "enabled_channels", None)
        if value is None:
            continue
        try:
            return {str(channel) for channel in value if isinstance(channel, str)}
        except TypeError:
            return set()
    # AgentLoop receives ChannelsConfig before ChannelManager is constructed;
    # use only explicitly configured sections as a narrow fallback.  Empty or
    # disabled sections are not treated as a routable destination.
    channels_config = getattr(loop, "channels_config", None)
    extras = getattr(channels_config, "model_extra", None)
    if not isinstance(extras, Mapping):
        extras = getattr(channels_config, "__pydantic_extra__", None)
    if isinstance(extras, Mapping):
        return {
            str(name)
            for name, section in extras.items()
            if isinstance(name, str)
            and section is not None
            and getattr(section, "enabled", True) is not False
        }
    return set()


async def _silent_progress(*_args: Any, **_kwargs: Any) -> None:
    """Suppress progress delivery for internal turns."""


async def _process_internal_turn(
    loop: Any,
    *,
    prompt: str,
    actor: str,
    channel: str,
    chat_id: str,
    session_key: str,
    tools: Any | None = None,
    ephemeral: bool = False,
) -> Any:
    """Run one turn through the core's admission, lock, and processing path.

    Build the typed ``RequestContext`` before calling ``AgentLoop.process_direct``.
    The core then performs admission, MCP setup, session locking, and turn
    processing in its normal order; this adapter must not duplicate any of it.
    """
    from nanobot.agent.tools.context import RequestContext

    # The neutral admission contract accepts only this canonical route form.
    from familia.session_identity import (
        make_private_session_key,
        parse_private_session_key,
    )

    route_key = make_private_session_key(actor, f"{channel}:{chat_id}")
    admission_key = (
        session_key
        if parse_private_session_key(session_key) == (actor, f"{channel}:{chat_id}")
        else route_key
    )
    request_context = RequestContext(
        channel=channel,
        chat_id=chat_id,
        session_key=admission_key,
        original_user_text=prompt,
        metadata={"actor": actor},
        sender_id=chat_id,
        actor=actor,
    )
    process_direct = loop.process_direct
    result = process_direct(
        prompt,
        session_key=admission_key,
        channel=channel,
        chat_id=chat_id,
        sender_id=chat_id,
        on_progress=_silent_progress,
        ephemeral=ephemeral,
        tools=tools,
        request_context=request_context,
    )
    return await result if inspect.isawaitable(result) else result


def _private_memory_client(owner: str) -> Any | None:
    from familia.acl.principal_memory import PrincipalMemoryClient

    principal = get_registry().get(owner)
    if principal is None or not principal.memx_key:
        return None
    try:
        return PrincipalMemoryClient(owner, principal.memx_key)
    except (TypeError, ValueError):
        return None


def _read_archive_batch(
    owner: str,
) -> tuple[Any, list[str], list[Mapping[str, Any]]] | None:
    """Read the archive facts written by Familia's ArchiveHandler.

    Archive facts are enumerated through the owner's trusted private catalog,
    then decoded from their JSON message lists.  No target ``MemoryStore``
    cursor or workspace history is consulted: the catalog is the durable
    archive-to-Dream hand-off.
    """
    client = _private_memory_client(owner)
    if client is None:
        return None
    try:
        from familia.acl.principal_memory import _decode_atomic_memory_catalog

        catalog = _decode_atomic_memory_catalog(client.get("value:private_index"))
    except Exception:  # noqa: BLE001
        logger.exception("Dream archive catalog read failed for principal {}", owner)
        return None
    if catalog is None:
        return None

    archive_ids: list[str] = []
    messages: list[Mapping[str, Any]] = []
    for name, _tags in catalog:
        if not isinstance(name, str) or not name.startswith("memory:archive-"):
            continue
        fact_id = name.removeprefix("memory:")
        if not fact_id:
            continue
        raw = client.get(name)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Dream skipped malformed archive {} for {}", name, owner)
            continue
        if not isinstance(decoded, list) or not decoded:
            logger.warning("Dream skipped non-message archive {} for {}", name, owner)
            continue
        group: list[Mapping[str, Any]] = []
        valid = True
        for message in decoded:
            if not isinstance(message, Mapping):
                valid = False
                break
            declared_actor = message.get("actor")
            if declared_actor not in (None, owner):
                valid = False
                break
            metadata = message.get("metadata")
            if metadata is not None and not isinstance(metadata, Mapping):
                valid = False
                break
            if isinstance(metadata, Mapping) and metadata.get("actor") not in (None, owner):
                valid = False
                break
            role = message.get("role")
            content = message.get("content")
            if role not in {"user", "assistant", "tool", "system"}:
                valid = False
                break
            if not isinstance(content, str) or not content.strip():
                valid = False
                break
            group.append(message)
        if not valid:
            logger.warning("Dream quarantines malformed/mixed archive {} for {}", name, owner)
            continue
        if len(group) > _DREAM_MAX_ENTRIES or len(messages) + len(group) > _DREAM_MAX_ENTRIES:
            # Never truncate a source archive and then delete its fact: the
            # remaining messages would otherwise be lost from the next run.
            break
        archive_ids.append(fact_id)
        messages.extend(group)
        if len(messages) == _DREAM_MAX_ENTRIES:
            break
    if not messages:
        return None
    return client, archive_ids, messages


def _dream_prompt(
    owner: str,
    entries: list[Mapping[str, Any]],
    *,
    profile: str | None = None,
    memory: str | None = None,
) -> str:
    lines: list[str] = []
    for entry in entries:
        timestamp = entry.get("timestamp", "?")
        role = entry.get("role", "user")
        content = str(entry["content"]).strip()
        lines.append(f"[{timestamp}] {role}: {content[:4000]}")
    existing = (
        "## Existing private profile\n"
        + (profile.strip() if isinstance(profile, str) and profile.strip() else "(empty)")
        + "\n\n## Existing private memory\n"
        + (memory.strip() if isinstance(memory, str) and memory.strip() else "(empty)")
    )
    return (
        "You are Familia's private memory consolidator for principal "
        f"{owner!r}. Only use the owner-scoped conversation below. Do not "
        "read or edit workspace files, shared SOUL/USER/MEMORY files, or "
        "facts belonging to another principal. Persist durable profile or "
        "atomic facts only by calling dream_memory_set. When correcting a "
        "profile, write a complete replacement that preserves every current "
        "detail not corrected by the conversation. Omit transient or "
        "uncertain details. A successful tool result is the only durable "
        "write. When there is nothing durable, finish without a tool call.\n\n"
        + existing
        + "\n\n"
        "## Owner-scoped conversation\n"
        + "\n".join(lines)
    )


def _dream_tools(owner: str, *, profile_version: float | None) -> Any:
    """Build one registry containing only the Familia atomic Dream writer."""
    from nanobot.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    from familia.nanobot_extension.cron import make_dream_tool_installers

    tool_results: list[tuple[str, Any]] = []

    def _record(result: Any) -> None:
        tool_results.append(("dream_memory_set", result))

    for installer in make_dream_tool_installers(
        server_principal_getter=lambda: owner,
        profile_version_getter=lambda: profile_version,
        result_tracker=_record,
    ):
        installer(registry, None)
    registry._familia_dream_results = tool_results  # type: ignore[attr-defined]
    return registry


async def _delete_archive_facts(owner: str, fact_ids: list[str]) -> bool:
    """Delete consumed archive facts only after a completed, saved Dream."""
    if not fact_ids:
        return False
    try:
        from familia.memx_client import memx_base_url
        from familia.principal_memory_ingestor import PrincipalMemoryIngestor
        from familia.tools.memory import _check_memory_write_policy

        principal = get_registry().get(owner)
        if principal is None:
            return False
        for fact_id in fact_ids:
            policy_error = _check_memory_write_policy(
                actor=_CONSOLIDATOR_ACTOR,
                full_key=f"private:{owner}:memory:{fact_id}",
            )
            if policy_error:
                logger.warning("Dream archive cleanup denied for {}", owner)
                return False
        ingestor = PrincipalMemoryIngestor(
            base_url=memx_base_url(),
            api_key=os.environ.get("DREAM_CONSOLIDATOR_MEMX_KEY", ""),
        )
        for fact_id in fact_ids:
            result = await ingestor.ingest(
                server_principal=owner,
                server_topic=None,
                operation={"kind": "delete", "fact_id": fact_id},
            )
            if not isinstance(result, str) or not result.startswith(("deleted:", "absent:")):
                logger.warning("Dream archive {} remains for {}: {}", fact_id, owner, result)
                return False
    except Exception:  # noqa: BLE001
        logger.exception("Dream archive cleanup failed for principal {}", owner)
        return False
    return True


async def run_dream(owner: str, loop: Any) -> Any:
    """Run one actor-scoped Dream turn on the core-provided loop."""
    actor = _registered_actor(owner)
    if actor is None:
        return None
    selected = _read_archive_batch(actor)
    if selected is None:
        return None
    client, archive_ids, entries = selected
    route = _identity_route(actor)
    if route is None:
        logger.warning("Dream skipped: principal {} has no routable identity", actor)
        return None

    from familia.session_identity import make_private_session_key

    try:
        profile_snapshot = await client.get_profile_snapshot()
    except Exception:  # noqa: BLE001
        logger.exception("Dream profile read failed for principal {}", actor)
        return None
    if (
        not isinstance(profile_snapshot, dict)
        or (
            profile_snapshot.get("value") is not None
            and not isinstance(profile_snapshot.get("value"), str)
        )
        or (
            profile_snapshot.get("version") is not None
            and (
                isinstance(profile_snapshot.get("version"), bool)
                or not isinstance(profile_snapshot.get("version"), (int, float))
            )
        )
    ):
        logger.warning("Dream profile snapshot is invalid for principal {}", actor)
        return None
    profile = profile_snapshot.get("value")
    profile_version = profile_snapshot.get("version")
    memory = client.get("value:memory")
    private_key = make_private_session_key(actor, f"{route[0]}:{route[1]}")
    try:
        dream_tools = _dream_tools(actor, profile_version=profile_version)
    except Exception:  # noqa: BLE001
        logger.exception("Dream tool setup failed for principal {}", actor)
        return None
    previous_actor = get_current_actor()
    previous_channel = get_current_channel()
    set_current_actor(actor)
    set_current_channel(route[0])
    response: Any = None
    try:
        response = await _process_internal_turn(
            loop,
            prompt=_dream_prompt(actor, entries, profile=profile, memory=memory),
            actor=actor,
            channel=route[0],
            chat_id=route[1],
            session_key=private_key,
            tools=dream_tools,
            ephemeral=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Dream failed for principal {}", actor)
        return None
    finally:
        set_current_actor(previous_actor)
        set_current_channel(previous_channel)

    metadata = getattr(response, "metadata", None)
    completed = isinstance(metadata, Mapping) and metadata.get("_stop_reason") == "completed"
    if not completed:
        logger.warning("Dream did not complete for principal {}; archive retained", actor)
        return None

    tool_results = getattr(dream_tools, "_familia_dream_results", ())
    saved = bool(tool_results) and all(
        isinstance(result, str)
        and result.startswith(("committed:", "deleted:", "absent:"))
        for _name, result in tool_results
    )
    if not saved:
        logger.warning("Dream completed without a durable write for {}; archive retained", actor)
        return None
    if not await _delete_archive_facts(actor, archive_ids):
        return None
    return f"Dream completed for {actor}; archived group consumed."


def _heartbeat_has_active_tasks(content: str) -> bool:
    """Recognize active task lines without treating headings/comments as work."""
    in_comment = False
    in_active_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if not stripped or stripped.startswith("#"):
            if stripped.startswith("##") and not stripped.startswith("###"):
                in_active_section = stripped.lstrip("#").strip().lower().startswith("active tasks")
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped[4:]:
                in_comment = True
            continue
        if in_active_section:
            return True
    return False


async def _heartbeat_source(actor: str) -> str | None:
    reader = make_heartbeat_source_reader(actor)
    context_factory = getattr(reader, "execution_context", None)
    scope = context_factory() if callable(context_factory) else nullcontext()
    try:
        with scope:
            value = reader()
            if inspect.isawaitable(value):
                value = await value
    except Exception:  # noqa: BLE001
        logger.exception("Heartbeat source read failed for principal {}", actor)
        return None
    if isinstance(value, tuple):
        value = value[0] if value else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _heartbeat_keep_recent(loop: Any) -> int:
    config = getattr(loop, "config", None)
    gateway = getattr(config, "gateway", None)
    heartbeat = getattr(gateway, "heartbeat", None)
    value = getattr(heartbeat, "keep_recent_messages", 8)
    return value if isinstance(value, int) and value >= 0 else 8


async def _heartbeat_should_notify(loop: Any, response: str, prompt: str) -> bool:
    evaluator = getattr(loop, "evaluate_response", None)
    workspace = getattr(loop, "workspace", None)
    config = getattr(loop, "config", None)
    if workspace is None:
        workspace = getattr(config, "workspace_path", None)
    try:
        from nanobot.utils.evaluator import evaluate_response, resolve_evaluator_prompt

        evaluator_prompt = resolve_evaluator_prompt(workspace)
        callback = evaluator if callable(evaluator) else evaluate_response
        value = callback(
            response=response,
            task_context=prompt,
            provider=getattr(loop, "provider", None),
            model=getattr(loop, "model", None),
            evaluator_prompt=evaluator_prompt,
            default_notify=False,
        )
        return bool(await value if inspect.isawaitable(value) else value)
    except Exception:  # noqa: BLE001
        logger.warning("Heartbeat evaluator failed; suppressing delivery")
        return False


async def _publish_internal_response(
    loop: Any,
    response: Any,
    *,
    actor: str,
    inbound: Any,
    action: str,
) -> None:
    from nanobot.bus.events import OutboundMessage

    if isinstance(response, OutboundMessage):
        outbound = response
    else:
        outbound = OutboundMessage(
            channel=inbound.channel,
            chat_id=inbound.chat_id,
            content=str(response),
        )
    publisher = getattr(loop, "_publish_outbound", None)
    if not callable(publisher):
        raise TypeError("nanobot loop has no policy-gated outbound publisher")
    result = publisher(outbound, actor=actor, inbound=inbound, action=action)
    if inspect.isawaitable(result):
        await result


async def run_heartbeat(actor: str, loop: Any) -> Any:
    """Run one actor-owned memX heartbeat on the existing core loop."""
    principal = _registered_actor(actor)
    if principal is None:
        return None
    content = await _heartbeat_source(principal)
    if content is None or not _heartbeat_has_active_tasks(content):
        return None

    enabled_channels = _enabled_channels(loop)
    configured_resolver = getattr(
        getattr(loop, "runtime_adapters", None),
        "resolve_heartbeat_target",
        None,
    )
    resolver = configured_resolver if callable(configured_resolver) else resolve_heartbeat_target
    target = resolver(principal, enabled_channels)
    if inspect.isawaitable(target):
        target = await target
    if (
        not isinstance(target, tuple)
        or len(target) != 2
        or not all(isinstance(value, str) and value for value in target)
    ):
        logger.warning("Heartbeat skipped: no enabled identity for principal {}", principal)
        return None
    channel, chat_id = target
    from nanobot.bus.events import InboundMessage

    from familia.session_identity import make_private_session_key

    inbound = InboundMessage(
        channel=channel,
        sender_id=chat_id,
        chat_id=chat_id,
        content=content,
        actor=principal,
    )
    private_key = make_private_session_key(principal, f"{channel}:{chat_id}")
    prompt = (
        _HEARTBEAT_PREAMBLE
        + "You are executing periodic tasks for the authenticated principal "
        + f"{principal!r}. Read only the active tasks below and report what "
        "you did:\n\n"
        + content
    )
    previous_actor = get_current_actor()
    previous_channel = get_current_channel()
    set_current_actor(principal)
    set_current_channel(channel)
    message_tool = None
    suppress_token = None
    try:
        tools = getattr(loop, "tools", None)
        getter = getattr(tools, "get", None)
        message_tool = getter("message") if callable(getter) else None
        suppress = getattr(message_tool, "set_suppress_delivery", None)
        if callable(suppress):
            suppress_token = suppress(True)
        response = await _process_internal_turn(
            loop,
            prompt=prompt,
            actor=principal,
            channel=channel,
            chat_id=chat_id,
            session_key=private_key,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Heartbeat failed for principal {}", principal)
        return None
    finally:
        reset = getattr(message_tool, "reset_suppress_delivery", None)
        if callable(reset) and suppress_token is not None:
            reset(suppress_token)
        set_current_actor(previous_actor)
        set_current_channel(previous_channel)

    sessions = getattr(loop, "sessions", None)
    get_or_create = getattr(sessions, "get_or_create", None)
    session = get_or_create(private_key) if callable(get_or_create) else None
    retain = getattr(session, "retain_recent_legal_suffix", None)
    if callable(retain):
        retain(_heartbeat_keep_recent(loop))
        save = getattr(sessions, "save", None)
        if callable(save):
            save(session)

    response_text = response if isinstance(response, str) else getattr(response, "content", None)
    if not isinstance(response_text, str) or not response_text.strip():
        return response
    if not await _heartbeat_should_notify(loop, response_text, prompt):
        return response_text
    await _publish_internal_response(
        loop,
        response,
        actor=principal,
        inbound=inbound,
        action="heartbeat",
    )
    return response_text


def _is_trusted_system_job(job: Any, expected_id: str) -> bool:
    """Recognize only the server-registered system job, never a user name."""
    if getattr(job, "id", None) != expected_id:
        return False
    name = getattr(job, "name", None)
    if not isinstance(name, str) or name.strip().lower() != expected_id:
        return False
    payload = getattr(job, "payload", None)
    if getattr(payload, "kind", None) != "system_event":
        return False
    if any(
        getattr(payload, field, None) not in (None, "")
        for field in ("created_by", "creator_actor", "target_actor", "owner_actor")
    ):
        return False
    metadata = getattr(payload, "origin_metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    marker = metadata.get(_SYSTEM_JOB_ORIGIN_KEY)
    return marker == expected_id and set(metadata) == {_SYSTEM_JOB_ORIGIN_KEY}


def _dream_owners(loop: Any) -> list[str]:
    """Return registered principals that can safely receive a Dream turn."""
    registry = get_registry()
    enabled = _enabled_channels(loop)
    channel_sources = (
        loop,
        getattr(loop, "channels", None),
        getattr(loop, "channel_manager", None),
    )
    channels_are_known = any(
        getattr(source, "enabled_channels", None) is not None
        for source in channel_sources
    )
    if channels_are_known and not enabled:
        return []
    enabled_filter = enabled if channels_are_known else None
    owners: list[str] = []
    for candidate in getattr(registry, "ids", ()):
        actor = _registered_actor(candidate)
        if actor is None:
            continue
        principal = registry.get(actor)
        memx_key = getattr(principal, "memx_key", None) if principal else None
        if not isinstance(memx_key, str) or not memx_key.strip():
            continue
        if _identity_route(actor, enabled_channels=enabled_filter) is None:
            continue
        owners.append(actor)
    return owners


async def _run_dream_for_all(loop: Any) -> Any:
    """Run each suitable owner independently; one failure cannot stop others."""
    outcomes: list[str] = []
    for owner in _dream_owners(loop):
        try:
            result = await run_dream(owner, loop)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Dream failed for principal {}", owner)
            outcomes.append(f"Dream failed for {owner}: {exc}")
            continue
        if isinstance(result, str) and result.strip():
            outcomes.append(result)
    return "\n".join(outcomes) if outcomes else None


def _configured_job_owner(job: Any, loop: Any, name: str) -> str | None:
    payload = getattr(job, "payload", None)
    for source in (payload, job):
        for field in ("target_actor", "owner_actor", "creator_actor", "actor", "owner"):
            candidate = _registered_actor(getattr(source, field, None))
            if candidate is not None:
                return candidate
    if name == "heartbeat":
        config = getattr(loop, "config", None)
        heartbeat = getattr(getattr(config, "gateway", None), "heartbeat", None)
        candidate = _registered_actor(getattr(heartbeat, "target_actor", None))
        if candidate is not None:
            return candidate
    return None


async def run_scheduled(job: Any, loop: Any) -> Any:
    """Dispatch one scheduled job through the existing AgentLoop."""
    if _is_trusted_system_job(job, "dream"):
        return await _run_dream_for_all(loop)
    if _is_trusted_system_job(job, "heartbeat"):
        name = "heartbeat"
        owner = _configured_job_owner(job, loop, name)
        return await run_heartbeat(owner, loop) if owner is not None else None

    cron = getattr(loop, "cron_service", None)
    if cron is None:
        logger.warning("Cron job {} skipped: loop has no cron service", getattr(job, "id", "?"))
        return None
    try:
        from nanobot.cron.service import CronJobSkippedError
        from nanobot.cron.session_turns import is_bound_cron_job
    except ImportError:
        return None
    if not is_bound_cron_job(job):
        raise CronJobSkippedError("unbound agent cron job must be recreated from a chat session")
    return await make_scheduled_handler(loop, cron)(job)


def make_runtime_service_hooks(config: Any = None, bus: Any = None) -> dict[str, Any]:
    """Return concrete Familia hooks, including the real background runners.

    ``config`` is accepted because the adapter seam passes it, but no callback
    is read from it: Dream, heartbeat, and bound cron always use the ``loop``
    supplied by nanobot at invocation time.
    """
    del config
    hooks: dict[str, Any] = {
        "channel_plugins": channel_plugins,
        "register_channel_descriptor": register_channel_descriptor,
        "resolve_heartbeat_target": resolve_heartbeat_target,
        "make_heartbeat_source_reader": make_heartbeat_source_reader,
        "run_dream": run_dream,
        "run_heartbeat": run_heartbeat,
        "run_scheduled": run_scheduled,
        "outbound_guard": make_outbound_guard(),
    }
    if bus is not None:
        hooks["callback_handler"] = make_callback_handler(bus)
    return hooks


__all__ = [
    "channel_plugins",
    "make_callback_handler",
    "make_heartbeat_source_reader",
    "make_outbound_guard",
    "make_runtime_service_hooks",
    "make_scheduled_handler",
    "make_vk_channel_plugin",
    "register_channel_descriptor",
    "resolve_heartbeat_target",
    "run_dream",
    "run_heartbeat",
    "run_scheduled",
]
