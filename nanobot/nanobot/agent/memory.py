"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
import re
import uuid
import weakref
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ContextManager, Iterator

from loguru import logger
from filelock import FileLock

from nanobot.utils.prompt_templates import render_template
from nanobot.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain, strip_think

from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.utils.gitstore import GitStore

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session, SessionManager


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------

class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.history_quarantine_file = self.memory_dir / "history.quarantine.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._history_lock = FileLock(str(self.memory_dir / ".history.lock"))
        self._quarantine_lock = FileLock(str(self.memory_dir / ".history-quarantine.lock"))
        self._corruption_logged = False  # rate-limit non-int cursor warning
        self._git = GitStore(workspace, tracked_files=[
            "SOUL.md", "USER.md", "memory/MEMORY.md",
        ])
        self._maybe_migrate_legacy_history()

    @property
    def git(self) -> GitStore:
        return self._git

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        if not self.legacy_history_file.exists():
            return
        if self.history_file.exists() and self.history_file.stat().st_size > 0:
            return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        """Parse pre-jsonl HISTORY.md into cursor-ordered entries.

        Legacy files mixed timestamp-prefixed entries and raw multiline
        message dumps. The splitter preserves raw dumps as one entry and uses
        the file mtime only when an entry has no timestamp prefix.
        """
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder

            entries.append({
                "schema_version": 1,
                "cursor": cursor,
                "timestamp": timestamp,
                "content": content,
                "provenance": {
                    "source": "legacy_history_migration",
                    "idempotency_key": None,
                },
            })
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        return self.read_file(self.memory_file)

    def write_memory(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self.user_file.write_text(content, encoding="utf-8")

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        long_term = self.read_memory()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def append_history(
        self,
        entry: str,
        actor: str | None = None,
        *,
        provenance_source: str = "runtime_history",
        idempotency_key: str | None = None,
    ) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        ``actor`` — optional principal id whose conversation the entry
        summarizes (familia per-scope Dream relies on this for private-memory
        routing).  ``None`` means «system / unknown / mixed» and the entry
        gets treated as not private to any single actor.

        Entries are passed through `strip_think` to drop template-level leaks
        (e.g. unclosed `<think` prefixes, `<channel|>` markers) before being
        persisted. If the cleaned content is empty but the raw entry wasn't,
        the record is persisted with an empty string rather than falling back
        to the raw leak — otherwise `strip_think`'s guarantees would be
        undone by history replay / consolidation downstream.
        """
        raw = entry.rstrip()
        content = strip_think(raw)
        with self._history_lock:
            if idempotency_key:
                for existing, cursor in self._iter_valid_entries():
                    provenance = existing.get("provenance")
                    if (
                        isinstance(provenance, dict)
                        and provenance.get("idempotency_key") == idempotency_key
                    ):
                        return cursor

            cursor = self._next_cursor()
            if raw and not content:
                logger.debug(
                    "history entry {} stripped to empty (likely template leak); "
                    "persisting empty content to avoid re-polluting context",
                    cursor,
                )
            record: dict[str, Any] = {
                "schema_version": 1,
                "cursor": cursor,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "content": content,
                "provenance": {
                    "source": provenance_source,
                    "idempotency_key": idempotency_key,
                },
            }
            if actor:
                record["actor"] = actor

            # The durable record always precedes the counter publication. If
            # either operation fails, .cursor can never claim an absent row.
            self._append_record_durable(record)
            self._atomic_write_text(self._cursor_file, str(cursor))
            return cursor

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _atomic_write_text(cls, path: Path, content: str) -> None:
        temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            cls._fsync_directory(path.parent)
        finally:
            temp.unlink(missing_ok=True)

    @classmethod
    def _append_jsonl_durable(cls, path: Path, record: dict[str, Any]) -> None:
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with open(path, "ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        cls._fsync_directory(path.parent)

    def _append_record_durable(self, record: dict[str, Any]) -> None:
        self._append_jsonl_durable(self.history_file, record)

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """Int cursors only — reject bool (``isinstance(True, int)`` is True)."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield ``(entry, cursor)`` for entries with int cursors; warn once on corruption."""
        poisoned: Any = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                self._record_history_quarantine(
                    reason="non_int_cursor",
                    raw=json.dumps(entry, ensure_ascii=False),
                )
                continue
            yield entry, cursor
        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains a non-int cursor ({!r}); it remains in "
                "the source and is excluded via history quarantine. Further "
                "occurrences are suppressed for this store.",
                poisoned,
            )

    def _next_cursor(self) -> int:
        """Return one greater than the greatest durably persisted cursor.

        ``.cursor`` is a published convenience counter, never the authority:
        trusting a stale/high counter would skip unpersisted records.
        Callers allocating a cursor hold ``_history_lock``.
        """
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with a valid cursor > *since_cursor*."""
        with self._history_lock:
            return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    def read_history_for_prompt(
        self,
        since_cursor: int,
        *,
        actor: str | None = None,
        actor_validator: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        """Select Recent History without crossing principal boundaries.

        A missing validator denotes explicit standalone mode and preserves the
        legacy local stream.  Supplying a validator enables multi-principal
        mode: the current actor must be valid, filtering happens before the
        caller applies its limit, and actorless/unknown rows receive a
        versioned quarantine disposition without mutating persisted history.
        """
        entries = self.read_unprocessed_history(since_cursor=since_cursor)
        quarantine: list[dict[str, Any]] = []
        if actor_validator is None:
            return {
                "entries": entries,
                "quarantine": {
                    "version": "history-quarantine-v1",
                    "records": quarantine,
                },
            }

        def _is_known(candidate: str | None) -> bool:
            if not candidate:
                return False
            try:
                return bool(actor_validator(candidate))
            except Exception:  # noqa: BLE001 - validation failure is deny
                return False

        current_actor_valid = _is_known(actor)
        selected: list[dict[str, Any]] = []
        for entry in entries:
            entry_actor = entry.get("actor")
            explicitly_safe = (
                entry.get("prompt_scope") in {"shared", "system"}
                and entry.get("prompt_safe_version") == "history-prompt-safe-v1"
            )
            if current_actor_valid and explicitly_safe:
                selected.append(entry)
                continue
            if not entry_actor:
                reason = "actorless_multi_principal"
            elif not _is_known(entry_actor):
                reason = "unknown_actor_multi_principal"
            else:
                if current_actor_valid and entry_actor == actor:
                    selected.append(entry)
                continue
            quarantine.append({
                "cursor": entry["cursor"],
                "reason": reason,
                "disposition": "quarantine_needs_review",
                "writes": 0,
            })

        return {
            "entries": selected,
            "quarantine": {
                "version": "history-quarantine-v1",
                "records": quarantine,
            },
        }

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds *max_history_entries*."""
        if self.max_history_entries <= 0:
            return
        with self._history_lock:
            entries = self._read_entries()
            if len(entries) <= self.max_history_entries:
                return
            kept = entries[-self.max_history_entries:]
            self._replace_entries_durable(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            self._record_history_quarantine(
                                reason="malformed_json",
                                raw=line,
                                line_number=line_number,
                            )
                            continue
                        if not isinstance(entry, dict):
                            self._record_history_quarantine(
                                reason="record_not_object",
                                raw=line,
                                line_number=line_number,
                            )
                            continue
                        version = entry.get("schema_version")
                        if version is not None and (
                            isinstance(version, bool)
                            or not isinstance(version, int)
                            or version != 1
                        ):
                            self._record_history_quarantine(
                                reason="unknown_schema_version",
                                raw=line,
                                line_number=line_number,
                            )
                            continue
                        if version == 1 and not self._current_record_shape_valid(entry):
                            self._record_history_quarantine(
                                reason="invalid_record_schema",
                                raw=line,
                                line_number=line_number,
                            )
                            continue
                        entries.append(entry)
        except FileNotFoundError:
            pass
        return entries

    @staticmethod
    def _current_record_shape_valid(entry: dict[str, Any]) -> bool:
        provenance = entry.get("provenance")
        actor = entry.get("actor")
        return bool(
            isinstance(entry.get("timestamp"), str)
            and isinstance(entry.get("content"), str)
            and isinstance(provenance, dict)
            and isinstance(provenance.get("source"), str)
            and (
                provenance.get("idempotency_key") is None
                or isinstance(provenance.get("idempotency_key"), str)
            )
            and (actor is None or isinstance(actor, str))
        )

    def _record_history_quarantine(
        self,
        *,
        reason: str,
        raw: str,
        line_number: int | None = None,
    ) -> None:
        fingerprint = hashlib.sha256(
            f"{reason}\0{raw}".encode("utf-8", errors="replace")
        ).hexdigest()
        with self._quarantine_lock:
            existing = {
                record.get("fingerprint")
                for record in self._read_quarantine_unlocked()
            }
            if fingerprint in existing:
                return
            record = {
                "schema_version": 1,
                "kind": "history_quarantine",
                "reason": reason,
                "fingerprint": fingerprint,
                "line_number": line_number,
                "raw": raw,
            }
            self._append_jsonl_durable(self.history_quarantine_file, record)
        logger.warning(
            "history record quarantined: reason={}, fingerprint={}",
            reason,
            fingerprint[:12],
        )

    def _read_quarantine_unlocked(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            with open(self.history_quarantine_file, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        logger.error("history quarantine sidecar contains malformed JSON")
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except FileNotFoundError:
            pass
        return records

    def read_history_quarantine(self) -> list[dict[str, Any]]:
        """Return durable, versioned corruption dispositions."""
        with self._quarantine_lock:
            return self._read_quarantine_unlocked()

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [l for l in data.split("\n") if l.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Atomically replace history.jsonl with durable serialized entries."""
        with self._history_lock:
            self._replace_entries_durable(entries)

    def _replace_entries_durable(self, entries: list[dict[str, Any]]) -> None:
        payload = "".join(
            json.dumps(entry, ensure_ascii=False) + "\n"
            for entry in entries
        )
        self._atomic_write_text(self.history_file, payload)

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            try:
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._atomic_write_text(self._dream_cursor_file, str(cursor))

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            actor = message.get("actor")
            role_tag = f"{message['role'].upper()}"
            if actor and message.get("role") == "user":
                role_tag = f"{role_tag}@{actor}"
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {role_tag}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    def raw_archive(self, messages: list[dict], actor: str | None = None) -> None:
        """Idempotently dump raw messages after a consolidation failure."""
        canonical = json.dumps(
            {"actor": actor, "messages": messages},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        idempotency_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.append_history(
            f"[RAW idempotency={idempotency_key}] {len(messages)} messages\n"
            f"{self._format_messages(messages)}",
            actor=actor,
            provenance_source="raw_consolidation_fallback",
            idempotency_key=idempotency_key,
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages (actor={})",
            len(messages), actor or "-",
        )



# ---------------------------------------------------------------------------
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------


class Consolidator:
    """Lightweight consolidation: summarizes evicted messages into history.jsonl."""

    _MAX_CONSOLIDATION_ROUNDS = 5
    _MAX_CHUNK_MESSAGES = 60  # hard cap per consolidation round

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
        archive_sink: Callable[[str, list[dict]], Awaitable[Any]] | None = None,
        private_session_owner_resolver: (
            Callable[[str, list[dict]], Awaitable[str | None]] | None
        ) = None,
    ):
        if archive_sink is not None and private_session_owner_resolver is None:
            raise ValueError(
                "private_session_owner_resolver is required when archive_sink is configured"
            )
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._archive_sink = archive_sink
        self._private_session_owner_resolver = private_session_owner_resolver
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @property
    def archive_sink_enabled(self) -> bool:
        return self._archive_sink is not None

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def _cap_consolidation_boundary(
        self,
        session: Session,
        end_idx: int,
    ) -> int | None:
        """Clamp the chunk size without breaking the user-turn boundary."""
        start = session.last_consolidated
        if end_idx - start <= self._MAX_CHUNK_MESSAGES:
            return end_idx

        capped_end = start + self._MAX_CHUNK_MESSAGES
        for idx in range(capped_end, start, -1):
            if session.messages[idx].get("role") == "user":
                return idx
        return None

    def estimate_session_prompt_tokens(
        self,
        session: Session,
        *,
        session_summary: str | None = None,
    ) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=0)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            session_summary=session_summary,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @staticmethod
    def _group_by_actor(messages: list[dict]) -> list[tuple[str | None, list[dict]]]:
        """Split a chunk into contiguous runs by the last-seen user actor.

        A user message's ``actor`` field defines the «current principal» for
        the assistant/tool turns that follow until the next user message.
        Messages before any actor-tagged user (usually historical pre-tag
        entries) get ``actor=None``.  Returns ``[(actor, messages), …]`` in
        the original order, so timestamps and causal chains are preserved
        within each group.
        """
        groups: list[tuple[str | None, list[dict]]] = []
        current_actor: str | None = None
        buf: list[dict] = []
        for m in messages:
            if m.get("role") == "user" and m.get("actor"):
                if buf and current_actor != m["actor"]:
                    groups.append((current_actor, buf))
                    buf = []
                current_actor = m["actor"]
            buf.append(m)
        if buf:
            groups.append((current_actor, buf))
        return groups

    async def archive(
        self,
        messages: list[dict],
        *,
        session_key: str | None = None,
    ) -> Any:
        """Summarize messages via LLM and append to history.jsonl.

        Splits the chunk by user-actor runs (see ``_group_by_actor``) so each
        resulting history entry carries a single principal in its ``actor``
        field — the per-scope Dream relies on this to route private facts
        without cross-leak.  Returns the last summary produced (matching the
        legacy single-return behavior callers use for ``_last_summary``), or
        None if nothing was archived.
        """
        if self._archive_sink is not None and (
            not isinstance(session_key, str) or not session_key
        ):
            raise ValueError("session_key is required when archive_sink is configured")
        if not messages:
            return None
        if self._archive_sink is not None:
            resolver = self._private_session_owner_resolver
            if resolver is None:  # Constructor validation keeps this branch unreachable.
                raise ValueError(
                    "private_session_owner_resolver is required when archive_sink is configured"
                )
            resolution = await resolver(session_key, messages)
            if not isinstance(resolution, str) or not resolution:
                raise RuntimeError("private session owner is unavailable")
            return await self._archive_sink(resolution, messages)
        last_summary: str | None = None
        for actor, chunk in self._group_by_actor(messages):
            if not chunk:
                continue
            summary = await self._archive_one(chunk, actor)
            if summary:
                last_summary = summary
        return last_summary

    async def _archive_one(self, messages: list[dict], actor: str | None) -> str | None:
        try:
            formatted = MemoryStore._format_messages(messages)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/consolidator_archive.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            summary = response.content or "[no summary]"
            self.store.append_history(summary, actor=actor)
            return summary
        except Exception:
            logger.warning(
                "Consolidation LLM call failed, raw-dumping to history (actor={})",
                actor or "-",
            )
            self.store.raw_archive(messages, actor=actor)
            return None

    async def maybe_consolidate_by_tokens(
        self,
        session: Session,
        *,
        session_summary: str | None = None,
    ) -> Session:
        """Loop: archive old messages until prompt fits within safe budget.

        The budget reserves space for completion tokens and a safety buffer
        so the LLM request never exceeds the context window.
        """
        key = session.key
        lock = self.get_lock(key)
        async with lock:
            session = self.sessions.get_or_create(key)
            if self.context_window_tokens <= 0:
                return session
            if not session.messages:
                return session

            original_messages = deepcopy(session.messages)
            original_last_consolidated = session.last_consolidated
            original_metadata = deepcopy(session.metadata)
            budget = self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER
            target = budget // 2
            try:
                estimated, source = self.estimate_session_prompt_tokens(
                    session,
                    session_summary=session_summary,
                )
            except Exception:
                logger.exception("Token estimation failed for {}", session.key)
                estimated, source = 0, "error"
            if estimated <= 0:
                return session
            if estimated < budget:
                unconsolidated_count = len(session.messages) - session.last_consolidated
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}, msgs={}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    unconsolidated_count,
                )
                return session

            last_summary = None
            changed = False
            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    break

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    break

                end_idx = boundary[0]
                end_idx = self._cap_consolidation_boundary(session, end_idx)
                if end_idx is None:
                    logger.debug(
                        "Token consolidation: no capped boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    break

                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    break

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                summary = await self.archive(chunk, session_key=session.key)
                if self._archive_sink is None:
                    if summary:
                        last_summary = summary
                    else:
                        break
                session.last_consolidated = end_idx
                changed = True

                try:
                    estimated, source = self.estimate_session_prompt_tokens(
                        session,
                        session_summary=session_summary,
                    )
                except Exception:
                    logger.exception("Token estimation failed for {}", session.key)
                    estimated, source = 0, "error"
                if estimated <= 0:
                    break

            # Persist the last summary to session metadata so it can be injected
            # into the runtime context on the next prepare_session() call, aligning
            # the summary injection strategy with AutoCompact._archive().
            if last_summary and last_summary != "(nothing)":
                session.metadata["_last_summary"] = {
                    "text": last_summary,
                    "last_active": session.updated_at.isoformat(),
                }
                changed = True

            if changed:
                try:
                    self.sessions.save(session)
                except Exception:
                    session.messages = original_messages
                    session.last_consolidated = original_last_consolidated
                    session.metadata = original_metadata
                    raise
            return session


# ---------------------------------------------------------------------------
# Dream — heavyweight cron-scheduled memory consolidation
# ---------------------------------------------------------------------------


# Single source of truth for the staleness threshold used in _annotate_with_ages
# *and* in the Phase 1 prompt template (passed as `stale_threshold_days`).
# Keep code and prompt aligned — if you bump this, the LLM's instruction string
# updates automatically.
_STALE_THRESHOLD_DAYS = 14


class Dream:
    """Two-phase memory processor: analyze history.jsonl, then edit files via AgentRunner.

    Phase 1 produces an analysis summary (plain LLM call).
    Phase 2 delegates to AgentRunner with read_file / edit_file tools so the
    LLM can make targeted, incremental edits instead of replacing entire files.
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        max_tool_result_chars: int = 16_000,
        annotate_line_ages: bool = True,
        dream_tool_installers: list[Callable[[ToolRegistry, MemoryStore], None]] | None = None,
        dream_turn_context: Callable[[], ContextManager[Any]] | None = None,
        dream_batch_context: Callable[[list[dict[str, Any]]], ContextManager[Any]] | None = None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        # Kill switch for the git-blame-based per-line age annotation in Phase 1.
        # Default True keeps the #3212 behavior; set False to feed MEMORY.md raw
        # (e.g. if a specific LLM reacts poorly to the `← Nd` suffix).
        self.annotate_line_ages = annotate_line_ages
        self._dream_tool_installers = dream_tool_installers or []
        self._dream_turn_context = dream_turn_context
        self._dream_batch_context = dream_batch_context
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    # -- tool registry -------------------------------------------------------

    def _build_tools(self) -> ToolRegistry:
        """Build a minimal tool registry for the Dream agent."""
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR
        from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool

        tools = ToolRegistry()
        workspace = self.store.workspace
        # Allow reading builtin skills for reference during skill creation
        extra_read = [BUILTIN_SKILLS_DIR] if BUILTIN_SKILLS_DIR.exists() else None
        tools.register(ReadFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            extra_allowed_dirs=extra_read,
        ))
        tools.register(EditFileTool(workspace=workspace, allowed_dir=workspace))
        # write_file resolves relative paths from workspace root, but can only
        # write under skills/ so the prompt can safely use skills/<name>/SKILL.md.
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        tools.register(WriteFileTool(workspace=workspace, allowed_dir=skills_dir))
        # Product adapters may add Dream-specific write tools without coupling
        # nanobot memory consolidation to their storage implementation.
        for install_tools in self._dream_tool_installers:
            install_tools(tools, self.store)
        return tools

    @staticmethod
    def _without_workspace_edit_instructions(prompt: str) -> str:
        """Remove file-edit guidance when the Dream registry has no editor."""
        removed_sections = {
            "## File paths (relative to workspace root)",
            "## Privacy invariant",
            "## Editing rules",
        }
        current_section = ""
        skip_directive_continuation = False
        lines: list[str] = []
        for line in prompt.splitlines():
            if line.startswith("Update memory files / scoped memX"):
                lines.append("Update scoped memX / skills based on the analysis below.")
                continue
            if line.startswith("## "):
                current_section = line
            if current_section in removed_sections:
                continue
            if line.startswith(("- [FILE] entries:", "- [FILE-REMOVE] entries:")):
                skip_directive_continuation = True
                continue
            if skip_directive_continuation and line.startswith("  "):
                continue
            skip_directive_continuation = False
            lines.append(line)
        return "\n".join(lines).strip()

    def _uses_atomic_operation_prompt(self) -> bool:
        return self._tools.has("dream_memory_set") and not self._tools.has("edit_file")

    @staticmethod
    def _atomic_operation_phase1_prompt() -> str:
        return """Extract only automatic principal-memory operations from the history.

The server has already fixed one private owner. Never infer or emit an owner,
scope, topic, participant, storage key, or routing decision.

Output one line per finding in exactly one of these forms:
[PROFILE] kind=profile value=<profile>
[MEMORY] kind=memory fact_id=<stable_fact_id> value=<atomic_fact>
[DELETE] kind=delete fact_id=<stable_fact_id>

Rules:
- PROFILE is the current participant profile or a correction to it.
- MEMORY is one atomic durable fact with a stable fact_id.
- Do not combine facts, invent routing, or copy temporary status and filler.
- Reuse the same semantic fact_id when a newer statement replaces an old one.
- If the participant says not to save a fact, do not emit MEMORY for it.
- Emit DELETE when that exact stable fact may have been saved earlier.
- Never copy the forbidden content or the prohibition itself into durable memory.
- Return [SKIP] when no operation is needed."""

    @staticmethod
    def _atomic_operation_phase2_prompt(_prompt: str = "") -> str:
        return """Apply only the atomic operations from the analysis below.
- [PROFILE] entries: call `dream_memory_set` with
  kind='profile', value='<profile>'
- [MEMORY] entries: call `dream_memory_set` with
  kind='memory', fact_id='<stable_fact_id>', value='<atomic_fact>'
- [DELETE] entries: call `dream_memory_set` with
  kind='delete', fact_id='<stable_fact_id>'

Never add owner, scope, topic, actor, other participant, or storage key fields.
If nothing needs updating, stop without calling tools."""

    async def archive_private(
        self,
        principal: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Apply automatic memory operations for one server-resolved owner."""
        if not isinstance(principal, str) or not principal:
            raise ValueError("private archive principal is required")
        if not messages:
            return None
        if not self._uses_atomic_operation_prompt():
            raise RuntimeError("automatic private memory tool is not configured")

        history_text = MemoryStore._format_messages(messages)
        phase1_response = await self.provider.chat_with_retry(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": self._atomic_operation_phase1_prompt(),
                },
                {
                    "role": "user",
                    "content": f"## Conversation History\n{history_text}",
                },
            ],
            tools=None,
            tool_choice=None,
        )
        analysis = phase1_response.content or ""
        runner_messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._atomic_operation_phase2_prompt(),
            },
            {
                "role": "user",
                "content": f"## Analysis Result\n{analysis}",
            },
        ]

        turn_context = (
            self._dream_turn_context()
            if self._dream_turn_context is not None
            else nullcontext()
        )
        principal_context = (
            self._dream_batch_context(principal)
            if self._dream_batch_context is not None
            else nullcontext()
        )
        with turn_context, principal_context:
            result = await self._runner.run(
                AgentRunSpec(
                    initial_messages=runner_messages,
                    tools=self._tools,
                    model=self.model,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    fail_on_tool_error=True,
                )
            )

        required_operations = sum(
            1
            for line in analysis.splitlines()
            if line.strip().startswith(("[PROFILE]", "[MEMORY]", "[DELETE]"))
        )
        tool_events = result.tool_events or []
        successful_operations = sum(
            1
            for event in tool_events
            if (
                event.get("name") == "dream_memory_set"
                and event.get("status") == "ok"
            )
        )
        if (
            result.stop_reason != "completed"
            or result.error
            or any(event.get("status") != "ok" for event in tool_events)
            or successful_operations < required_operations
        ):
            raise RuntimeError(
                "automatic private memory operations were not fully applied"
            )
        return None

    # -- skill listing --------------------------------------------------------

    def _list_existing_skills(self) -> list[str]:
        """List existing skills as 'name — description' for dedup context."""
        import re as _re

        from nanobot.agent.skills import BUILTIN_SKILLS_DIR

        _DESC_RE = _re.compile(r"^description:\s*(.+)$", _re.MULTILINE | _re.IGNORECASE)
        entries: dict[str, str] = {}
        for base in (self.store.workspace / "skills", BUILTIN_SKILLS_DIR):
            if not base.exists():
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                skill_md = d / "SKILL.md"
                if not skill_md.exists():
                    continue
                # Prefer workspace skills over builtin (same name)
                if d.name in entries and base == BUILTIN_SKILLS_DIR:
                    continue
                content = skill_md.read_text(encoding="utf-8")[:500]
                m = _DESC_RE.search(content)
                desc = m.group(1).strip() if m else "(no description)"
                entries[d.name] = desc
        return [f"{name} — {desc}" for name, desc in sorted(entries.items())]

    # -- main entry ----------------------------------------------------------

    def _annotate_with_ages(self, content: str) -> str:
        """Append per-line age suffixes to MEMORY.md content.

        Each non-blank line whose age exceeds ``_STALE_THRESHOLD_DAYS`` gets a
        suffix like ``← 30d`` indicating days since last modification.
        Returns the original content unchanged if git is unavailable,
        annotate fails, or the line count doesn't match the age count
        (which can happen with an uncommitted working-tree edit — better to
        skip annotation than to tag the wrong line).
        SOUL.md and USER.md are never annotated.
        """
        file_path = "memory/MEMORY.md"
        try:
            ages = self.store.git.line_ages(file_path)
        except Exception:
            logger.debug("line_ages failed for {}", file_path)
            return content
        if not ages:
            return content

        had_trailing = content.endswith("\n")
        lines = content.splitlines()
        # If HEAD-blob line count disagrees with the working-tree content we
        # received, ages would be assigned to the wrong lines — skip entirely
        # and feed the LLM un-annotated content rather than misleading data.
        if len(lines) != len(ages):
            logger.debug(
                "line_ages length mismatch for {} (lines={}, ages={}); skipping annotation",
                file_path, len(lines), len(ages),
            )
            return content

        annotated: list[str] = []
        for line, age in zip(lines, ages):
            if not line.strip():
                annotated.append(line)
                continue
            if age.age_days > _STALE_THRESHOLD_DAYS:
                annotated.append(f"{line}  \u2190 {age.age_days}d")
            else:
                annotated.append(line)
        result = "\n".join(annotated)
        if had_trailing:
            result += "\n"
        return result

    async def run(self) -> bool:
        """Process unprocessed history entries. Returns True if work was done."""
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR

        last_cursor = self.store.get_last_dream_cursor()
        entries = self.store.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return False

        operation_only = self._uses_atomic_operation_prompt()
        batch = entries[: self.max_batch_size]
        if operation_only:
            persisted_actor = batch[0].get("actor")
            prefix_size = 1
            for entry in batch[1:]:
                if entry.get("actor") != persisted_actor:
                    break
                prefix_size += 1
            batch = batch[:prefix_size]
        logger.info(
            "Dream: processing {} entries (cursor {}→{}), batch={}",
            len(entries), last_cursor, batch[-1]["cursor"], len(batch),
        )

        if operation_only:
            principal = batch[0].get("actor")
            if not isinstance(principal, str) or not principal:
                return False
            try:
                await self.archive_private(
                    principal,
                    [
                        {
                            "role": "user",
                            "content": str(entry.get("content") or ""),
                        }
                        for entry in batch
                    ],
                )
            except Exception:
                logger.exception("Dream private archive failed")
                return False
            new_cursor = batch[-1]["cursor"]
            self.store.set_last_dream_cursor(new_cursor)
            self.store.compact_history()
            return True

        # Build history text for the standalone file-backed Dream.
        def _render(e: dict) -> str:
            actor = e.get("actor") or "(untagged)"
            return f"[{e['timestamp']}] actor={actor}: {e['content']}"

        history_text = "\n".join(_render(e) for e in batch)

        # Current file contents + per-line age annotations (MEMORY.md only)
        current_date = datetime.now().strftime("%Y-%m-%d")
        raw_memory = self.store.read_memory() or "(empty)"
        current_memory = (
            self._annotate_with_ages(raw_memory)
            if self.annotate_line_ages
            else raw_memory
        )
        current_soul = self.store.read_soul() or "(empty)"
        current_user = self.store.read_user() or "(empty)"

        file_context = (
            f"## Current Date\n{current_date}\n\n"
            f"## Current MEMORY.md ({len(current_memory)} chars)\n{current_memory}\n\n"
            f"## Current SOUL.md ({len(current_soul)} chars)\n{current_soul}\n\n"
            f"## Current USER.md ({len(current_user)} chars)\n{current_user}"
        )

        # Phase 1: Analyze (no skills list — dedup is Phase 2's job)
        phase1_prompt = (
            f"## Conversation History\n{history_text}\n\n{file_context}"
        )

        try:
            phase1_response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/dream_phase1.md",
                            strip=True,
                            stale_threshold_days=_STALE_THRESHOLD_DAYS,
                        ),
                    },
                    {"role": "user", "content": phase1_prompt},
                ],
                tools=None,
                tool_choice=None,
            )
            analysis = phase1_response.content or ""
            logger.debug("Dream Phase 1 analysis ({} chars): {}", len(analysis), analysis[:500])
        except Exception:
            logger.exception("Dream Phase 1 failed")
            return False

        # Phase 2: Delegate to AgentRunner with read_file / edit_file
        existing_skills = self._list_existing_skills()
        skills_section = ""
        if existing_skills:
            skills_section = (
                "\n\n## Existing Skills\n"
                + "\n".join(f"- {s}" for s in existing_skills)
            )
        phase2_prompt = f"## Analysis Result\n{analysis}\n\n{file_context}{skills_section}"

        skill_creator_path = BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md"
        phase2_system_prompt = render_template(
            "agent/dream_phase2.md",
            strip=True,
            skill_creator_path=str(skill_creator_path),
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": phase2_system_prompt,
            },
            {"role": "user", "content": phase2_prompt},
        ]

        try:
            turn_context = (
                self._dream_turn_context()
                if self._dream_turn_context is not None
                else nullcontext()
            )
            with turn_context:
                result = await self._runner.run(AgentRunSpec(
                    initial_messages=messages,
                    tools=self._tools,
                    model=self.model,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    fail_on_tool_error=True,
                ))
            logger.debug(
                "Dream Phase 2 complete: stop_reason={}, tool_events={}",
                result.stop_reason, len(result.tool_events),
            )
            for ev in (result.tool_events or []):
                logger.info("Dream tool_event: name={}, status={}, detail={}", ev.get("name"), ev.get("status"), ev.get("detail", "")[:200])
        except Exception:
            logger.exception("Dream Phase 2 failed")
            result = None

        required_markers = ("[PRIVATE:", "[PAIR:")
        required_scoped_writes = sum(
            1
            for line in analysis.splitlines()
            if line.strip().startswith(required_markers)
        )
        tool_events = result.tool_events if result else []
        successful_scoped_writes = sum(
            1
            for event in tool_events
            if event.get("name") == "dream_memory_set" and event.get("status") == "ok"
        )
        tool_failed = any(event.get("status") != "ok" for event in tool_events)
        batch_succeeded = bool(
            result
            and result.stop_reason == "completed"
            and not result.error
            and not tool_failed
            and successful_scoped_writes >= required_scoped_writes
        )
        if not batch_succeeded:
            reason = result.stop_reason if result else "exception"
            logger.warning(
                "Dream incomplete ({}): cursor unchanged at {}; "
                "scoped writes {}/{}",
                reason,
                last_cursor,
                successful_scoped_writes,
                required_scoped_writes,
            )
            return False

        # Build changelog from tool events
        changelog: list[str] = []
        if result.tool_events:
            for event in result.tool_events:
                if event["status"] == "ok":
                    changelog.append(f"{event['name']}: {event['detail']}")

        # Cursor and compaction commit last, only after the complete Phase 2.
        new_cursor = batch[-1]["cursor"]
        self.store.set_last_dream_cursor(new_cursor)
        self.store.compact_history()

        logger.info(
            "Dream done: {} change(s), cursor advanced to {}",
            len(changelog), new_cursor,
        )

        # Git auto-commit (only when there are actual changes)
        if changelog and self.store.git.is_initialized():
            ts = batch[-1]["timestamp"]
            summary = f"dream: {ts}, {len(changelog)} change(s)"
            commit_msg = f"{summary}\n\n{analysis.strip()}"
            sha = self.store.git.auto_commit(commit_msg)
            if sha:
                logger.info("Dream commit: {}", sha)

        return True
