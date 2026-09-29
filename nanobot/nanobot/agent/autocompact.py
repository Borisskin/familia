"""Auto compact: proactive compression of idle sessions to reduce token cost and latency."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

from nanobot.events import NO_EVENTS, EventSink
from nanobot.session.manager import (
    SESSION_FILE_CAP_CHECKED_KEY,
    SESSION_FILE_CAP_MAX_MESSAGES,
    SESSION_FILE_CAP_PENDING_KEY,
    Session,
    SessionManager,
)
from nanobot.session.summary import (
    SessionSummary,
    is_summary_checkpoint,
    session_summary_from_metadata,
)

if TYPE_CHECKING:
    from nanobot.agent.memory import Consolidator
    from nanobot.utils.llm_runtime import LLMRuntime

SessionEventFactory = Callable[[str], EventSink]


class AutoCompact:
    _INTERNAL_SESSION_PREFIXES = ("dream:",)

    def __init__(self, sessions: SessionManager, consolidator: Consolidator,
                 session_ttl_minutes: int = 0,
                 bind_events: SessionEventFactory | None = None):
        self.sessions = sessions
        self.consolidator = consolidator
        self._ttl = session_ttl_minutes
        self._archiving: set[str] = set()
        self._summaries: dict[str, SessionSummary] = {}
        self._bind_events = bind_events

    def _is_expired(self, ts: datetime | str | None,
                    now: datetime | None = None) -> bool:
        if self._ttl <= 0 or not ts:
            return False
        try:
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            current = now or datetime.now()
            if getattr(ts, "tzinfo", None) is not None or current.tzinfo is not None:
                idle_seconds = current.timestamp() - ts.timestamp()
            else:
                idle_seconds = (current - ts).total_seconds()
        except (OSError, OverflowError, TypeError, ValueError):
            # list_sessions() forwards raw persisted metadata; an unusable value
            # must not escape the idle scan and stop the agent loop.
            return False
        return idle_seconds >= self._ttl * 60

    def _has_unarchived_messages(self, key: str) -> bool:
        session = self.sessions.get_or_create(key)
        return any(
            not message.get("_command") and not is_summary_checkpoint(message)
            for message in session.messages[session.last_archived:]
        )

    @classmethod
    def _is_internal_session(cls, key: str) -> bool:
        return key.startswith(cls._INTERNAL_SESSION_PREFIXES)

    def check_expired(
        self,
        schedule_background: Callable[[Coroutine[Any, Any, None]], None],
        resolve_runtime: Callable[[Session], LLMRuntime],
        active_session_keys: Collection[str] = (),
    ) -> None:
        """Schedule archival for idle sessions, skipping those with in-flight agent tasks."""
        now = datetime.now()
        for info in self.sessions.list_sessions():
            key = info.get("key", "")
            if not key or self._is_internal_session(key) or key in self._archiving:
                continue
            if key in active_session_keys:
                continue
            read_metadata = getattr(self.sessions, "read_session_metadata", None)
            metadata_payload = read_metadata(key) if callable(read_metadata) else None
            metadata = (
                metadata_payload.get("metadata", {})
                if metadata_payload
                else {SESSION_FILE_CAP_CHECKED_KEY: True}
            )
            if (
                metadata.get(SESSION_FILE_CAP_CHECKED_KEY) is not True
                or metadata.get(SESSION_FILE_CAP_PENDING_KEY) is True
            ):
                self._schedule_file_cap(key, schedule_background, resolve_runtime)
                continue
            updated_at = info.get("updated_at")
            if self._is_expired(updated_at, now) and self._has_unarchived_messages(key):
                session = self.sessions.get_or_create(key)
                try:
                    runtime = resolve_runtime(session)
                except (KeyError, ValueError):
                    # Invalid session selections remain recoverable through /model.
                    continue
                self._archiving.add(key)
                schedule_background(self._archive(key, runtime=runtime))

    def schedule_file_cap_after_turn(
        self,
        key: str,
        schedule_background: Callable[[Coroutine[Any, Any, None]], None],
        resolve_runtime: Callable[[Session], LLMRuntime],
    ) -> None:
        """Schedule retention after a completed save, including busy sessions."""
        session = self.sessions.get_cached(key)
        if (
            session is None
            or self._is_internal_session(key)
            or key in self._archiving
            or (
                len(session.messages) <= SESSION_FILE_CAP_MAX_MESSAGES
                and not session.metadata.get(SESSION_FILE_CAP_PENDING_KEY)
            )
        ):
            return
        self._schedule_file_cap(key, schedule_background, resolve_runtime)

    def _schedule_file_cap(
        self,
        key: str,
        schedule_background: Callable[[Coroutine[Any, Any, None]], None],
        resolve_runtime: Callable[[Session], LLMRuntime],
    ) -> None:
        if self._is_internal_session(key) or key in self._archiving:
            return
        self._archiving.add(key)
        schedule_background(self._enforce_file_cap(key, resolve_runtime))

    async def _enforce_file_cap(
        self,
        key: str,
        resolve_runtime: Callable[[Session], LLMRuntime],
    ) -> None:
        try:
            session = self.sessions.get_or_create(key)
            if len(session.messages) <= SESSION_FILE_CAP_MAX_MESSAGES:
                session.metadata[SESSION_FILE_CAP_CHECKED_KEY] = True
                session.metadata.pop(SESSION_FILE_CAP_PENDING_KEY, None)
                self.sessions.save(session)
                return

            session.metadata[SESSION_FILE_CAP_CHECKED_KEY] = True
            session.metadata[SESSION_FILE_CAP_PENDING_KEY] = True
            self.sessions.save(session)
            try:
                runtime = resolve_runtime(session)
            except (KeyError, ValueError):
                return
            await self.consolidator.enforce_file_cap(key, runtime=runtime)
        except Exception:
            logger.exception("Auto-compact: file-cap retention failed for {}", key)
        finally:
            self._archiving.discard(key)

    async def _archive(self, key: str, *, runtime: LLMRuntime) -> None:
        if self._is_internal_session(key):
            self._archiving.discard(key)
            return
        try:
            summary = await self.consolidator.compact_idle_session(
                key,
                runtime=runtime,
                events=self._bind_events(key) if self._bind_events else NO_EVENTS,
            )
            if summary:
                session = self.sessions.get_or_create(key)
                stored = session_summary_from_metadata(
                    session.metadata,
                    fallback_last_active=session.updated_at,
                )
                if stored is not None:
                    self._summaries[key] = stored
        except Exception:
            logger.exception("Auto-compact: failed for {}", key)
        finally:
            self._archiving.discard(key)

    def prepare_session(self, session: Session, key: str) -> tuple[Session, SessionSummary | None]:
        if self._is_internal_session(key):
            self._archiving.discard(key)
            self._summaries.pop(key, None)
            return session, None
        if key in self._archiving or self._is_expired(session.updated_at):
            logger.info("Auto-compact: reloading session {} (archiving={})", key, key in self._archiving)
            session = self.sessions.get_or_create(key)
        # Hot path: summary from in-memory dict (process hasn't restarted).
        entry = self._summaries.pop(key, None)
        if entry:
            return session, entry
        # Cold path: summary persisted in session metadata (process restarted).
        # Persisted metadata may outlive schema changes; a malformed summary must
        # not abort turn preparation.
        return session, session_summary_from_metadata(
            session.metadata,
            fallback_last_active=session.updated_at,
        )
