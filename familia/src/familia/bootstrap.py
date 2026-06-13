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

import os
from typing import Any

from loguru import logger

import sys

from familia.principals import get_current_actor, set_current_actor, set_current_channel
from familia.roles import load_effective_roles


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
        "tool_call_auditor": audit.log_event,
        "cron_tool_options": {
            "to_validator": make_principal_chat_validator(),
            "current_actor_getter": get_current_actor,
            "is_admin_getter": make_admin_check(),
            "reachable_tags_getter": make_reachable_tags_getter(),
        },
        "dream_tool_installers": make_dream_tool_installers(),
    }


def make_direct_actor_resolver() -> Any:
    """Return resolver for direct cron/heartbeat turns that skip channels."""
    from familia.principals import resolve_actor

    return resolve_actor


def make_dream_tool_installers() -> list[Any]:
    """Return familia Dream memory tool installers for nanobot Dream."""
    from familia.nanobot_extension.cron import make_dream_tool_installers as _make

    return _make()


def make_heartbeat_source_reader(target_actor: str | None) -> Any:
    """Return optional heartbeat source reader for a configured principal."""
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
    from familia.policy import gate_outbound_send
    from nanobot.agent.outbound import OutboundDecision

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
    except Exception:
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


def install_tools(loop: Any) -> None:
    """Register every familia tool on ``loop.tools``.

    Mirrors the set of registrations that used to live inline in
    ``AgentLoop._register_tools``.  Call this after upstream tools are
    registered (MessageTool etc.) so ordering is preserved.

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

    bus = loop.bus
    loop.tools.register(SendButtonsTool(send_callback=bus.publish_outbound))
    # AskPrincipalTool deprecated 2026-04-27: межпринципальные действия
    # решает peer-edge ACL + policy.yaml, без интерактивных подтверждений
    # у адресата. Регистрация снята, чтобы LLM не видел тул в списке
    # доступных. Сам класс и pending_asks оставлены в коде до полной
    # чистки — старые callback'и (если есть в персистентном state) ещё
    # маршрутизируются CallbackDispatcher'ом и не теряются.
    loop.tools.register(MemoryGetTool())
    loop.tools.register(MemorySetTool())
    loop.tools.register(ResolvePersonTool())
    loop.tools.register(AdminGrantTool())
    loop.tools.register(AdminRevokeTool())
    loop.tools.register(AdminListTool())
    loop.tools.register(AdminSetTzTool())

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
        _asyncio.get_event_loop().create_task(_models_refresh_daemon())
    except RuntimeError:
        # No running loop yet (called from a sync context). The CLI
        # path that spawns the gateway will set one up; this branch is
        # benign for tooling that imports bootstrap without a loop.
        pass

    logger.debug("familia.bootstrap: tools registered")


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
        "Используй эти id для tags=[...] в memory_set/cron add. "
        "Видны только участники/топики, к которым у тебя есть доступ.",
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
