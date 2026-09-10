"""Single entry point for wiring familia into a nanobot AgentLoop.

The goal of this module is to keep the loop.py patch as small as
possible — ideally two calls: one to register tools at construction
time, one to set up per-turn actor/role context when a message comes
in.  Everything else (policy engine, callback dispatcher, audit log,
pending_asks) is self-initializing through module-level singletons.

Usage from the patched loop.py::

    from familia import bootstrap as familia_bootstrap
    ...
    context_extensions=familia_bootstrap.make_context_extensions(workspace)
    ...
    familia_bootstrap.install_tools(self)     # inside _register_tools
    ...
    await familia_bootstrap.on_inbound(msg)   # wherever msg.actor is set
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any

from loguru import logger

from familia.principals import get_current_actor, set_current_actor, set_current_channel
from familia.roles import load_effective_roles


def _runtime_types() -> Any:
    """Import target boundary types lazily to keep standalone imports cheap."""
    from nanobot.runtime_adapters import (
        Admission,
        ArchiveResult,
        RuntimeAdapters,
        default_context_factory,
    )

    return Admission, ArchiveResult, RuntimeAdapters, default_context_factory


def _trusted_runtime_context(msg: Any) -> Any | None:
    """Return only a server-created context carried through the message bus.

    A ContextVar belongs to the producer task and is not propagated through an
    ``asyncio.Queue``.  The queue contract therefore carries the actual typed
    ``RequestContext`` object in metadata; untyped client JSON is rejected.
    """
    from nanobot.agent.tools.context import RequestContext

    from familia.principals import get_registry
    from familia.session_identity import parse_private_session_key

    metadata = getattr(msg, "metadata", None)
    candidate = metadata.get("_runtime_request_context") if isinstance(metadata, dict) else None
    if not isinstance(candidate, RequestContext):
        return None
    actor = candidate.actor
    channel = getattr(msg, "channel", "")
    chat_id = getattr(msg, "chat_id", "")
    if not isinstance(actor, str) or get_registry().get(actor) is None:
        return None
    if (candidate.channel, candidate.chat_id) != (channel, chat_id):
        return None
    parsed = parse_private_session_key(candidate.session_key or "")
    if parsed is None or parsed[0] != actor:
        return None
    return candidate


def _server_actor(msg: Any) -> str | None:
    """Resolve one server-owned actor before a session or command exists."""
    from familia.principals import get_registry, resolve_actor

    channel = getattr(msg, "channel", "")
    sender_id = getattr(msg, "sender_id", "")
    resolved = resolve_actor(channel, sender_id)
    supplied = getattr(msg, "actor", None)
    trusted = _trusted_runtime_context(msg)
    trusted_actor = getattr(trusted, "actor", None)
    if supplied is not None:
        # InboundMessage.actor is written by trusted channel/background code;
        # client metadata never reaches this branch.  Still require registry
        # membership and reject disagreement with the channel identity.
        if not isinstance(supplied, str) or get_registry().get(supplied) is None:
            return None
        if resolved is None and not (trusted is not None and supplied == trusted_actor):
            return None
        if resolved is not None and supplied != resolved:
            return None
        return supplied
    if trusted is not None:
        return trusted_actor
    return resolved


def _private_route(actor: str, msg: Any) -> str:
    """Build a private route from server fields; ignore client session overrides."""
    from familia.session_identity import make_private_session_key

    return make_private_session_key(
        actor,
        f"{getattr(msg, 'channel', '')}:{getattr(msg, 'chat_id', '')}",
    )


def _rejected_response(msg: Any) -> Any:
    from nanobot.bus.events import OutboundMessage

    return OutboundMessage(
        channel=str(getattr(msg, "channel", "")),
        chat_id=str(getattr(msg, "chat_id", "")),
        content="Неизвестный отправитель: доступ запрещён.",
    )


def _familia_command_rejection(msg: Any) -> Any | None:
    """Reject second-queue commands before sessions or stores are touched."""
    raw = str(getattr(msg, "content", "") or "").strip()
    token = raw.split(None, 1)[0].split("@", 1)[0].lower() if raw else ""
    if token not in {"/trigger", "/pairing"}:
        return None
    from nanobot.bus.events import OutboundMessage

    return OutboundMessage(
        channel=str(getattr(msg, "channel", "")),
        chat_id=str(getattr(msg, "chat_id", "")),
        content=f"Команда {token} недоступна в режиме Familia.",
    )


async def _admit_message(msg: Any) -> Any:
    """Admit only registry-backed senders, before session/command processing."""
    Admission, _ArchiveResult, _RuntimeAdapters, _default_context_factory = _runtime_types()
    actor = _server_actor(msg)
    if actor is None:
        return Admission(response=_rejected_response(msg))
    command_rejection = _familia_command_rejection(msg)
    if command_rejection is not None:
        return Admission(response=command_rejection)
    trusted = _trusted_runtime_context(msg)
    metadata = dict(getattr(msg, "metadata", {}) or {})
    # The in-process context proves admission but must never enter serialized
    # history, logs, or outbound payloads.
    metadata.pop("_runtime_request_context", None)
    admitted = replace(msg, actor=actor, metadata=metadata)
    return Admission(
        actor=actor,
        session_key=(trusted.session_key if trusted is not None else _private_route(actor, msg)),
        message=admitted,
    )


def _context_factory(admission: Any, message: Any) -> Any:
    _Admission, _ArchiveResult, _RuntimeAdapters, default_context_factory = _runtime_types()
    ctx = default_context_factory(admission, message)
    metadata = dict(ctx.metadata)
    metadata.pop("_runtime_request_context", None)
    metadata["familia_admitted"] = True
    return replace(ctx, metadata=metadata)


def _turn_scope(ctx: Any) -> Any:
    """Bind actor/channel/request ContextVars and restore all prior values."""
    @contextmanager
    def _scope():
        previous_actor = get_current_actor()
        from nanobot.security.workspace_access import (
            bind_workspace_scope,
            build_workspace_scope,
            reset_workspace_scope,
        )

        from familia.principals import get_current_channel

        previous_channel = get_current_channel()
        set_current_actor(ctx.actor)
        set_current_channel(ctx.channel)
        scope_token = None
        try:
            workspace = getattr(ctx, "workspace", None)
            base = Path(workspace).expanduser().resolve(strict=False) if workspace else Path.cwd().resolve()
            actor = str(getattr(ctx, "actor", "") or "")
            digest = hashlib.sha256(actor.encode("utf-8")).hexdigest()[:32]
            actor_root = base / "actors" / digest / "tool"
            # The root is server-derived from the admitted actor, never from
            # client metadata.  Refuse pre-existing symlink/junction escapes and
            # keep standalone contexts side-effect free until a real turn runs.
            lexical_root = actor_root.absolute()
            if lexical_root.exists() and lexical_root.resolve(strict=False) != lexical_root:
                raise ValueError("Familia actor workspace must not be a symlink")
            if workspace is not None:
                lexical_root.mkdir(parents=True, exist_ok=True)
                if lexical_root.resolve(strict=False) != lexical_root:
                    raise ValueError("Familia actor workspace must not be a symlink")
            scope = build_workspace_scope(
                lexical_root,
                "restricted",
                source_channel=ctx.channel,
                allow_shared_extras=False,
                sandbox_mask_root=base,
            )
            scope_token = bind_workspace_scope(scope)
            from nanobot.agent.tools.context import request_context

            bound_context = replace(ctx, workspace=scope.project_path)
            with request_context(bound_context) as entered:
                yield entered
        finally:
            if scope_token is not None:
                reset_workspace_scope(scope_token)
            set_current_actor(previous_actor)
            set_current_channel(previous_channel)

    return _scope()


def _context_builder_factory(
    workspace: Path,
    timezone: str | None,
    disabled_skills: list[str] | None,
) -> Any:
    from familia.nanobot_extension.context import FamiliaContextBuilder

    return FamiliaContextBuilder(
        workspace,
        timezone=timezone,
        disabled_skills=disabled_skills,
    )


async def _runtime_context_provider(ctx: Any) -> Any:
    from nanobot.runtime_context import RuntimeContextBlock

    from familia.nanobot_extension.context import FamiliaContextExtension

    sections = FamiliaContextExtension(ctx.workspace or Path.cwd()).build_runtime_sections(
        actor=ctx.actor,
        channel=ctx.channel,
        chat_id=ctx.chat_id,
    )
    return [RuntimeContextBlock(source="familia.acl", content=section) for section in sections]


async def _archive_messages(owner: str, messages: Any) -> Any:
    """Store one actor-owned atomic archive fact; never fall back to files."""
    _Admission, ArchiveResult, _RuntimeAdapters, _default_context_factory = _runtime_types()
    from familia.memx_client import memx_base_url
    from familia.principal_memory_ingestor import PrincipalMemoryIngestor
    from familia.principals import get_registry
    if not isinstance(owner, str) or not owner:
        return ArchiveResult(committed=False, retryable=False)
    actor = owner
    principal = get_registry().get(actor)
    if principal is None or not principal.memx_key:
        return ArchiveResult(committed=False, retryable=False)
    clean_messages: list[dict[str, Any]] = []
    for raw in messages or ():
        if not isinstance(raw, dict):
            return ArchiveResult(committed=False, retryable=False)
        if raw.get("actor") not in (None, actor):
            return ArchiveResult(committed=False, retryable=False)
        metadata = raw.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            return ArchiveResult(committed=False, retryable=False)
        if isinstance(metadata, dict) and metadata.get("actor") not in (None, actor):
            return ArchiveResult(committed=False, retryable=False)
        clean = {
            key: value
            for key, value in raw.items()
            if key not in {"_meta", "_runtime_request_context"}
        }
        if isinstance(metadata, dict):
            clean["metadata"] = {
                key: value
                for key, value in metadata.items()
                if key != "_runtime_request_context"
            }
        clean_messages.append(clean)
    if not clean_messages:
        return ArchiveResult(committed=False, retryable=False)
    encoded = json.dumps(clean_messages, ensure_ascii=False, sort_keys=True, default=str)
    fact_id = "archive-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:48]
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR
    from familia.tools.memory import _check_memory_write_policy

    if _check_memory_write_policy(
        actor=CONSOLIDATOR_ACTOR,
        full_key=f"private:{actor}:memory:{fact_id}",
    ):
        return ArchiveResult(committed=False, retryable=False)
    ingestor = PrincipalMemoryIngestor(
        base_url=memx_base_url(),
        api_key=principal.memx_key,
    )
    result = await ingestor.ingest(
        server_principal=actor,
        server_topic=None,
        operation={"kind": "memory", "fact_id": fact_id, "value": encoded},
    )
    committed = isinstance(result, str) and result.startswith("committed:")
    retryable = isinstance(result, str) and result.startswith(("error:", "retryable_failure:"))
    return ArchiveResult(committed=committed, retryable=retryable)

_dream_principal: ContextVar[str | None] = ContextVar(
    "familia_dream_principal",
    default=None,
)


def make_context_extensions(workspace: Any) -> list[Any]:
    """Return familia prompt/runtime extensions for a nanobot context builder."""
    # Keep nanobot.context generic: familia owns the concrete extension class
    # and exposes only already-built extension objects to the runtime loop.
    try:
        from familia.nanobot_extension.context import FamiliaContextExtension
    except ImportError:
        return []
    return [FamiliaContextExtension(workspace)]


def make_inbound_enrichers() -> list[Any]:
    """Return familia adapters for channel-level inbound enrichment."""
    try:
        from familia.nanobot_extension.inbound import FamiliaInboundEnricher
    except ImportError:
        return []
    return [FamiliaInboundEnricher()]


def make_channel_manager_kwargs() -> dict[str, Any]:
    """Return familia adapters for nanobot's neutral channel extension points."""
    return {
        "inbound_enrichers": make_inbound_enrichers(),
        "channel_classes": make_channel_classes(),
    }


def make_channel_classes() -> dict[str, Any]:
    """Return familia-owned channel classes for explicit runtime registration."""
    try:
        from familia.channels.vk import VKChannel
    except ImportError:
        return {}
    return {"vk": VKChannel}


def make_private_session_owner_resolver() -> Any:
    """Return Familia's resolver for private archive source ownership."""
    from familia.principals import get_registry
    from familia.private_session_owner import PrivateSessionOwnerResolver

    return PrivateSessionOwnerResolver(get_registry)


def make_agent_loop_kwargs(workspace: Any) -> dict[str, Any]:
    """Return familia adapters for nanobot's neutral extension points."""
    from familia import audit

    return {
        "context_extensions": make_context_extensions(workspace),
        "tool_installers": [install_tools],
        # Loop-level enrichment owns per-turn ContextVars/roles after the
        # channel-level enricher has already resolved msg.actor.
        "inbound_enrichers": [on_inbound],
        "outbound_guard": make_outbound_guard(),
        "pending_inbound_handler": handle_pending_inbound,
        "direct_actor_resolver": make_direct_actor_resolver(),
        "current_actor_getter": get_current_actor,
        "history_actor_validator": make_history_actor_validator(),
        "private_session_owner_resolver": make_private_session_owner_resolver(),
        "tool_call_auditor": audit.log_event,
        "cron_tool_options": {
            "to_validator": make_principal_chat_validator(),
            "current_actor_getter": get_current_actor,
            "is_admin_getter": make_admin_check(),
            "reachable_tags_getter": make_reachable_tags_getter(),
        },
        "dream_tool_installers": make_dream_tool_installers(),
        "dream_turn_context": make_dream_turn_context(),
        "dream_restore_policy": make_dream_restore_policy(),
        "dream_batch_context": make_dream_batch_context(),
    }


def make_direct_actor_resolver() -> Any:
    """Return resolver for direct cron/heartbeat turns that skip channels."""
    from familia.principals import resolve_actor

    return resolve_actor


def make_dream_tool_installers() -> list[Any]:
    """Return familia Dream memory tool installers for nanobot Dream."""
    from familia.nanobot_extension.cron import make_dream_tool_installers as _make

    return _make(server_principal_getter=make_dream_server_context_resolver())


def make_dream_restore_policy() -> Any:
    """Return Familia's fail-closed policy for ordinary Dream restores."""

    tracked_files = {"SOUL.md", "USER.md", "memory/MEMORY.md"}

    def _policy(changed_files: list[str] | None) -> str | None:
        if (
            not isinstance(changed_files, list)
            or not changed_files
            or any(
                not isinstance(path, str) or path not in tracked_files
                for path in changed_files
            )
        ):
            return (
                "Familia cannot verify which files this Dream change affects. "
                "Use the isolated snapshot restore path instead."
            )
        if "SOUL.md" in changed_files:
            return (
                "Familia does not restore `SOUL.md` through `/dream-restore`. "
                "Use the isolated snapshot restore path instead."
            )
        return None

    return _policy


def make_history_actor_validator() -> Any:
    """Return the neutral validator used by actor-aware Recent History."""
    from familia.principals import get_registry

    def _is_known(actor: str) -> bool:
        return bool(actor) and get_registry().get(actor) is not None

    return _is_known


def make_dream_turn_context() -> Any:
    """Pin Dream's executor identity for one Phase 2 turn and restore it."""
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    @contextmanager
    def _scope():
        previous = get_current_actor()
        set_current_actor(CONSOLIDATOR_ACTOR)
        try:
            yield
        finally:
            set_current_actor(previous)

    return _scope


def make_dream_batch_context() -> Any:
    """Bind one server-resolved private owner for the active Dream turn."""

    @contextmanager
    def _scope(principal: str):
        value = principal if isinstance(principal, str) and principal else None
        token = _dream_principal.set(value)
        try:
            yield
        finally:
            _dream_principal.reset(token)

    return _scope


def make_dream_server_context_resolver() -> Any:
    """Return the registry-verified owner bound to the active Dream turn."""
    from familia.principals import get_registry

    def _resolve() -> str | None:
        principal = _dream_principal.get()
        if (
            not isinstance(principal, str)
            or get_registry().get(principal) is None
        ):
            return None
        return principal

    return _resolve


def make_heartbeat_source_reader(target_actor: str | None) -> Any:
    """Return the adapter-owned heartbeat reader for a configured principal.

    ``None`` means nanobot may use its standalone legacy file path. A returned
    reader owns source selection, and empty reader content must fail closed in
    nanobot heartbeat service instead of falling back to ``HEARTBEAT.md``.
    """
    if not (target_actor or "").strip():
        return None
    from familia.nanobot_extension.cron import make_heartbeat_source_reader as _make

    return _make(target_actor)


def make_callback_handlers(bus: Any) -> list[Any]:
    """Return familia callback handlers for nanobot's neutral dispatcher."""
    from familia.bus.callback_dispatcher import CallbackDispatcher

    return [CallbackDispatcher(bus)]


def resolve_heartbeat_target(target_actor: str, enabled_channels: set[str]) -> tuple[str, str] | None:
    """Resolve configured heartbeat actor to an enabled channel identity."""
    from familia.principals import get_registry

    principal = get_registry().get(target_actor)
    if principal is None:
        return None
    for ident in principal.identities:
        if ident.channel in enabled_channels and ident.sender_id:
            return ident.channel, str(ident.sender_id)
    return None


def reload_runtime_registry() -> None:
    """Reload mutable familia registry state for gateway SIGHUP."""
    from familia.principals import reload_registry

    reload_registry()


def make_outbound_guard() -> Any:
    """Adapt familia outbound policy to nanobot's guard protocol."""
    from nanobot.agent.outbound import OutboundDecision

    from familia.policy import gate_outbound_send

    async def _guard(request: Any) -> Any:
        result = await gate_outbound_send(
            action=request.action,
            outbound=request.outbound,
            inbound_channel=request.inbound_channel,
            inbound_chat_id=request.inbound_chat_id,
            publish_outbound=request.publish_outbound,
        )
        return OutboundDecision(
            kind=result.kind,
            reason=result.reason,
            approvers_label=result.approvers_label,
            outbound=getattr(result, "outbound", None),
        )

    return _guard


async def handle_pending_inbound(msg: Any) -> tuple[bool, Any | None]:
    """Intercept unknown principals before nanobot logs or processes content."""
    if getattr(msg, "actor", None) is not None or getattr(msg, "channel", None) in (
        "cli",
        "system",
    ):
        return False, None

    from nanobot.bus.events import OutboundMessage

    try:
        from familia.pending import store as pending_store
        from familia.pending.messages import reply_for_pending

        display_name = ""
        meta = getattr(msg, "metadata", None) or {}
        # Channel adapters may provide a friendly name; fallback keeps admin
        # pending rows identifiable without exposing this logic to nanobot.
        for key in ("display_name", "first_name", "username", "from_name"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                display_name = value.strip()
                break
        if not display_name:
            display_name = str(getattr(msg, "sender_id", ""))

        import asyncio

        entry = await asyncio.to_thread(
            pending_store.record,
            channel=msg.channel,
            sender_id=msg.sender_id,
            display_name=display_name,
            message_preview=msg.content,
        )
        if entry is not None:
            return True, OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=reply_for_pending(),
            )
        return True, None
    # The pending-principal gate must fail closed for any storage or adapter error.
    except Exception:  # noqa: BLE001
        logger.exception(
            "pending-principal gate failed for {}:{}; replying with degraded notice",
            getattr(msg, "channel", None),
            getattr(msg, "sender_id", None),
        )
        return True, OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="Сейчас не могу обработать запрос. Попробуйте позже.",
        )


def _ensure_familia_tool_security(config: Any) -> None:
    """Keep shell and filesystem tools inside the selected runtime boundary."""
    exec_config = getattr(config, "exec", None)
    if exec_config is None:
        return
    sandbox = getattr(exec_config, "sandbox", "") or ""
    fields_set = getattr(exec_config, "model_fields_set", None)
    explicit_empty = (
        isinstance(fields_set, (set, frozenset))
        and "sandbox" in fields_set
        and not sandbox
    )
    if not sandbox:
        if explicit_empty:
            if os.environ.get("NANOBOT_ALLOW_UNSANDBOXED_EXEC") != "1":
                from nanobot.runtime_adapters import RuntimeAdapterError

                raise RuntimeAdapterError(
                    "Familia requires exec.sandbox='bwrap'; explicitly empty sandbox "
                    "needs NANOBOT_ALLOW_UNSANDBOXED_EXEC=1"
                )
            return
        # Omitted value keeps the historical Familia/container default. The
        # explicit empty value is only an opt-in development escape hatch.
        try:
            exec_config.sandbox = "bwrap"
        except (AttributeError, TypeError, ValueError) as exc:
            from nanobot.runtime_adapters import RuntimeAdapterError

            raise RuntimeAdapterError("Familia cannot set the exec sandbox") from exc
        return
    if sandbox != "bwrap":
        from nanobot.runtime_adapters import RuntimeAdapterError

        raise RuntimeAdapterError(f"unsupported Familia exec sandbox: {sandbox!r}")


def install_tools(context_or_loop: Any, registry: Any | None = None) -> Any:
    """Register Familia tools through the neutral ``ToolInstaller`` seam.

    The two-argument form is the 0.3.0 adapter contract.  The one-argument
    legacy form remains for the old bootstrap tests and starts the existing
    model-refresh task only in that compatibility path.

    Tool imports are deferred to break a circular dependency: the tool
    modules import ``nanobot.agent.tools.base``, and loading nanobot in
    turn executes ``nanobot.agent.loop`` which imports this module.
    """
    from familia.tools.admin import (
        AdminGrantTool,
        AdminListTool,
        AdminRevokeTool,
        AdminSetTzTool,
    )
    from familia.tools.buttons import SendButtonsTool
    from familia.tools.family_graph import ResolvePersonTool
    from familia.tools.memory import MemoryGetTool, MemorySetTool

    legacy_loop = registry is None
    loop = context_or_loop if legacy_loop else None
    if legacy_loop:
        registry = loop.tools
        context = None
        bus = loop.bus
    else:
        context = context_or_loop
        bus = getattr(context, "bus", None)
        _ensure_familia_tool_security(getattr(context, "config", None))
        from familia.nanobot_extension.cron import make_cron_job_access

        context.cron_job_access = make_cron_job_access(
            is_admin=make_admin_check(),
            reachable_tags=make_reachable_tags_getter(),
        )
    publish_outbound = getattr(bus, "publish_outbound", None)
    registry.register(SendButtonsTool(send_callback=publish_outbound))
    # AskPrincipalTool deprecated 2026-04-27: межпринципальные действия
    # решает peer-edge ACL + policy.yaml, без интерактивных подтверждений
    # у адресата. Регистрация снята, чтобы LLM не видел тул в списке
    # доступных. Сам класс и pending_asks оставлены в коде до полной
    # чистки — старые callback'и (если есть в персистентном state) ещё
    # маршрутизируются CallbackDispatcher'ом и не теряются.
    registry.register(MemoryGetTool())
    registry.register(MemorySetTool())
    registry.register(ResolvePersonTool())
    registry.register(AdminGrantTool())
    registry.register(AdminRevokeTool())
    registry.register(AdminListTool())
    registry.register(AdminSetTzTool())

    names = tuple(registry.tool_names)
    if not legacy_loop:
        logger.debug("familia.bootstrap: tools registered through RuntimeAdapters")
        return names

    # Daily background pull of provider /v1/models lists. The CLI
    # subprocess writes a cache file; the admin app's `agents get`
    # merges it into the model dropdown. Done here (not via nanobot
    # cron) to avoid touching the upstream cron dispatcher — a tiny
    # asyncio loop is enough for "once a day" cadence.
    import asyncio as _asyncio

    async def _models_refresh_daemon() -> None:
        # First run after a small grace period so the gateway has time
        # to settle (channels up, providers loaded).
        await _asyncio.sleep(60)
        while True:
            try:
                proc = await _asyncio.create_subprocess_exec(
                    sys.executable, "-m", "familia.cli.graph_admin",
                    "agents", "refresh-models", "--json",
                    stdout=_asyncio.subprocess.DEVNULL,
                    stderr=_asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except Exception as exc:  # noqa: BLE001
                logger.warning("models refresh daemon error: {}", exc)
            # Daily cadence; jitter not required for our scale.
            await _asyncio.sleep(24 * 60 * 60)

    try:
        _asyncio.get_running_loop().create_task(_models_refresh_daemon())
    except RuntimeError:
        # No running loop yet (called from a sync context). The CLI
        # path that spawns the gateway will set one up; this branch is
        # benign for tooling that imports bootstrap without a loop.
        pass

    logger.debug("familia.bootstrap: tools registered")
    return names


def make_admin_check() -> Any:
    """Return ``actor_id -> bool`` predicate: True iff actor has admin role.

    Plugged into CronTool so admins (effective role from principals.json or
    active grants) bypass the per-actor visibility filter on ``cron list``
    and ``cron remove`` — they own the household, they manage everything.
    """
    from familia.roles import get_effective_roles

    def _is_admin(actor_id: str | None) -> bool:
        if not actor_id:
            return False
        return "admin" in get_effective_roles(actor_id)

    return _is_admin


def build_vocabulary_for(actor: str) -> str:
    """Render the per-actor vocabulary block for the LLM system prompt (A1).

    Loads both graphs through the actor's memx_key, runs
    :func:`familia.acl.vocabulary.build_for` (filtered by reachable per
    SR-1), and formats as plain text. Returns ``""`` on any failure
    (SR-10) — the prompt then has no acl-vocab block, which is benign.
    """
    if not actor:
        return ""
    try:
        import json

        import httpx

        from familia.acl import vocabulary
        from familia.acl.schema import Graph
        from familia.memx_client import memx_base_url
        from familia.principals import get_registry
        from familia.roles import get_effective_roles
    except Exception:  # noqa: BLE001 — defensive import guard
        return ""
    reg = get_registry()
    p = reg.get(actor)
    if p is None or not p.memx_key:
        return ""

    def _fetch(key: str) -> Graph:
        try:
            r = httpx.get(
                f"{memx_base_url()}/get",
                headers={"x-api-key": p.memx_key},
                params={"key": key},
                timeout=3.0,
            )
        except httpx.HTTPError:
            return Graph()
        if r.status_code in (404, 403):
            return Graph()
        if r.status_code >= 400:
            return Graph()
        try:
            payload = r.json()
        except ValueError:
            return Graph()
        if payload is None:
            return Graph()
        raw = payload.get("value", payload) if isinstance(payload, dict) else payload
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return Graph()
        return Graph.from_dict(raw if isinstance(raw, dict) else None)

    family = _fetch("shared:family.graph")
    topics = _fetch("shared:topics.graph")
    role_map = {
        pid: frozenset(pp.roles or [])
        for pid in reg.ids
        if (pp := reg.get(pid)) is not None
    }
    is_admin = "admin" in get_effective_roles(actor)
    entries = vocabulary.build_for(
        actor=actor, family=family, topics=topics,
        principal_roles=role_map, is_admin=is_admin,
    )
    if not entries:
        return ""
    persons = [e for e in entries if e.kind == "principal"]
    topics_e = [e for e in entries if e.kind != "principal"]
    lines = [
        "<acl-vocabulary>",
        (
            "Используй эти id для tags=[...] в memory_set/cron add. "
            "Видны только участники/топики, к которым у тебя есть доступ."
        ),
        "",
    ]
    if persons:
        lines.append("Участники:")
        for e in persons:
            aliases = ", ".join(e.aliases) if e.aliases else "—"
            hint = f" — {e.relation_hint}" if e.relation_hint else ""
            lines.append(f"  {e.id} ({e.display_name}, aliases: {aliases}){hint}")
    if topics_e:
        lines.append("")
        lines.append("Топики:")
        for e in topics_e:
            aliases = ", ".join(e.aliases) if e.aliases else "—"
            hint = f" — {e.relation_hint}" if e.relation_hint else ""
            lines.append(f"  {e.id} [{e.kind}] ({e.display_name}, aliases: {aliases}){hint}")
    lines.append("</acl-vocabulary>")
    return "\n".join(lines)


def make_reachable_tags_getter() -> Any:
    """Return ``actor_id -> set[str]`` reachable tag-ids across both graphs.

    The returned callable is synchronous from the caller's POV but it
    delegates the HTTP work to a worker thread so it does NOT block the
    event loop when invoked from inside the async tool dispatch (e.g.
    CronTool._add_job). Without this, two 5-second httpx.get calls per
    cron op would stall every other coroutine for up to 10 seconds.

    Returns an empty set on any failure (SR-10).
    """
    import asyncio
    import json

    import httpx

    from familia.acl.reachable import reachable_tag_ids
    from familia.acl.schema import Graph
    from familia.memx_client import memx_base_url
    from familia.principals import get_registry

    def _fetch_sync(api_key: str, key: str) -> Graph:
        try:
            r = httpx.get(f"{memx_base_url()}/get",
                          headers={"x-api-key": api_key},
                          params={"key": key},
                          timeout=5.0)
        except httpx.HTTPError:
            return Graph()
        if r.status_code in (404, 403):
            return Graph()
        if r.status_code >= 400:
            return Graph()
        try:
            payload = r.json()
        except ValueError:
            return Graph()
        if payload is None:
            return Graph()
        raw = payload.get("value", payload) if isinstance(payload, dict) else payload
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return Graph()
        return Graph.from_dict(raw if isinstance(raw, dict) else None)

    def _principal_role_map() -> dict[str, frozenset[str]]:
        reg = get_registry()
        return {
            pid: frozenset(p.roles or [])
            for pid in reg.ids
            if (p := reg.get(pid)) is not None
        }

    def _compute(actor_id: str) -> set[str]:
        reg = get_registry()
        principal = reg.get(actor_id)
        if principal is None or not principal.memx_key:
            return set()
        family = _fetch_sync(principal.memx_key, "shared:family.graph")
        topics = _fetch_sync(principal.memx_key, "shared:topics.graph")
        return reachable_tag_ids(family, topics, actor_id, _principal_role_map())

    def _get(actor_id: str | None) -> set[str]:
        if not actor_id:
            return set()
        # When called from within a running event loop (the typical async
        # tool path), run the blocking httpx work in the loop's executor
        # via run_until_complete won't work (loop is running). Instead we
        # use ``asyncio.get_running_loop().run_in_executor`` if we're in
        # a coroutine, else direct sync. CronTool calls happen inside
        # asyncio.run/agent loop; so we always go through the executor
        # path when there IS a running loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _compute(actor_id)
        # Schedule the sync work on a worker thread so we don't block
        # the event loop. Caller is sync (a non-async getter), but the
        # surrounding loop is running our async tool — so we must NOT
        # call `loop.run_until_complete`. Use a thread directly.
        import threading
        result: dict[str, set[str]] = {}
        exc: dict[str, BaseException] = {}

        def _runner():
            try:
                result["v"] = _compute(actor_id)
            except BaseException as e:  # noqa: BLE001 — propagate via dict
                exc["e"] = e

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=15.0)  # graphs fetch worst-case ≤ 2*5s + overhead
        if "e" in exc:
            return set()  # SR-10 fail-closed
        return result.get("v", set())

    return _get


def make_principal_chat_validator() -> Any:
    """Return ``(channel, chat_id) -> bool``: True iff some principal owns it.

    Plugged into ``CronTool`` so the LLM can't redirect cron deliveries to
    chat ids outside the family graph (e.g. via prompt injection). Looks
    up the registry on every call so newly-loaded principals are honored
    without restart.
    """
    from familia.principals import get_registry

    def _validate(channel: str, chat_id: str) -> bool:
        if not channel or not chat_id:
            return False
        reg = get_registry()
        for pid in reg.ids:
            p = reg.get(pid)
            if p is None:
                continue
            for ident in p.identities:
                if ident.channel == channel and str(ident.sender_id) == str(chat_id):
                    return True
        return False

    return _validate


def apply_heartbeat_defaults(hb_cfg: Any) -> None:
    """Fill ``HeartbeatConfig.target_actor`` from FAMILIA_OWNER_ACTOR if blank.

    Only sets the field when the user hasn't pinned it explicitly in
    config.json. The env var is the documented "routing target for
    system/cron messages" — heartbeat shares that semantics.
    """
    current = (getattr(hb_cfg, "target_actor", "") or "").strip()
    if current:
        return
    owner = (os.environ.get("FAMILIA_OWNER_ACTOR") or "").strip()
    if not owner:
        return
    hb_cfg.target_actor = owner
    logger.debug("familia.bootstrap: heartbeat.target_actor defaulted to '{}'", owner)


async def on_inbound(msg: Any) -> None:
    """Per-turn setup: pin current actor + load effective roles.

    Safe no-op when ``msg.actor`` is empty (unknown principal).  Runs
    in the session task so ContextVars propagate into tool calls.
    """
    actor = getattr(msg, "actor", None)
    set_current_actor(actor)
    set_current_channel(getattr(msg, "channel", None))
    await load_effective_roles(actor)


def _runtime_service_hooks(config: Any, bus: Any) -> dict[str, Any]:
    """Load hooks owned by the independent service/channel adapter."""
    try:
        from familia.nanobot_extension import runtime_services
    except ImportError:
        return {}
    factory = getattr(runtime_services, "make_runtime_service_hooks", None)
    if not callable(factory):
        return {}
    hooks = factory(config, bus)
    if hooks is None:
        return {}
    if isinstance(hooks, dict):
        return dict(hooks)
    names = (
        "run_dream",
        "run_heartbeat",
        "run_scheduled",
        "resolve_heartbeat_target",
        "make_heartbeat_source_reader",
        "channel_plugins",
        "register_channel_descriptor",
        "callback_handler",
    )
    return {name: getattr(hooks, name) for name in names if callable(getattr(hooks, name, None))}


def _channel_plugins(enabled: set[str] | None = None) -> dict[str, Any]:
    """Expose Familia's VK descriptor before standard channel discovery."""
    if enabled is not None and "vk" not in enabled:
        return {}
    from nanobot.channels.plugin import ChannelPlugin

    return {
        "vk": ChannelPlugin(
            name="vk",
            display_name="VK",
            runtime="familia.channels.vk:VKChannel",
            default_enabled=True,
            settings_visible=True,
            capabilities=frozenset({"text", "media", "buttons", "callbacks"}),
        )
    }


def _register_channel_descriptor(descriptor: Any) -> None:
    """Validate a product descriptor; target registry performs registration."""
    if descriptor is None:
        return
    if not isinstance(getattr(descriptor, "name", None), str) or not descriptor.name:
        raise TypeError("Familia channel descriptor must have a name")


def _callback_handler(bus: Any) -> Any:
    if bus is None:
        return None
    from familia.bus.callback_dispatcher import CallbackDispatcher

    return CallbackDispatcher(bus).handle_callback


def make_runtime_adapters(config: Any, bus: Any = None) -> Any:
    """Build the complete Familia adapter object for nanobot 0.3.0."""
    _Admission, _ArchiveResult, RuntimeAdapters, _default_context_factory = _runtime_types()
    from nanobot.agent.outbound import OutboundDecision

    from familia.policy import gate_outbound_send

    async def outbound_guard(request: Any) -> Any:
        result = await gate_outbound_send(
            action=request.action,
            outbound=request.outbound,
            inbound_channel=request.inbound_channel,
            inbound_chat_id=request.inbound_chat_id,
            publish_outbound=request.publish_outbound,
        )
        return OutboundDecision(
            kind=result.kind,
            reason=result.reason,
            approvers_label=result.approvers_label,
            outbound=getattr(result, "outbound", None),
        )

    def context_factory(admission: Any, message: Any) -> Any:
        ctx = _context_factory(admission, message)
        workspace = getattr(config, "workspace_path", None)
        if workspace is None:
            return ctx
        return replace(ctx, workspace=Path(workspace))

    hooks = _runtime_service_hooks(config, bus)
    # Dream/heartbeat/scheduling are owned by the sibling service adapter;
    # selected Familia mode must fail closed until all three are present.
    missing_service_hooks = {
        name for name in ("run_dream", "run_heartbeat", "run_scheduled")
        if not callable(hooks.get(name))
    }
    if missing_service_hooks:
        from nanobot.runtime_adapters import RuntimeAdapterError

        missing = ", ".join(sorted(missing_service_hooks))
        raise RuntimeAdapterError(
            f"Familia runtime service hooks missing: {missing}"
        )
    values: dict[str, Any] = {
        "admit": _admit_message,
        "context_factory": context_factory,
        "context_builder_factory": _context_builder_factory,
        "context_providers": (_runtime_context_provider,),
        "install_tools": install_tools,
        "turn_scope": _turn_scope,
        "archive": _archive_messages,
        "channel_plugins": hooks.pop("channel_plugins", _channel_plugins),
        "register_channel_descriptor": hooks.pop(
            "register_channel_descriptor", _register_channel_descriptor
        ),
        "callback_handler": hooks.pop("callback_handler", _callback_handler(bus)),
        "outbound_guard": outbound_guard,
    }
    values.update(hooks)
    return RuntimeAdapters(**values)
