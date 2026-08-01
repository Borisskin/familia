"""Cron adapters owned by familia.

Nanobot owns the cron engine and the generic Dream/heartbeat runners. Familia
owns per-principal storage choices: which actor's heartbeat to read and which
Dream write tool can write scoped facts to memX.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from loguru import logger

from familia.acl.principal_memory import PrincipalMemoryClient
from familia.principals import get_current_actor, get_registry, set_current_actor


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


def make_dream_tool_installers(
    *,
    server_principal_getter: Callable[[], Any] | None = None,
    server_topic_validator: Callable[[str], bool] | None = None,
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
            DreamMemorySetTool(
                ingestor=ingestor,
                server_principal_getter=principal_getter,
            )
        )

    return [_install]
