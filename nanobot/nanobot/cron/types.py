"""Cron types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from nanobot.utils.dict_keys import get_camel_snake


def _store_int(value: Any, default: int | None = 0) -> int | None:
    """Coerce JSON numerics to int; treat null/blank like a missing key."""
    if value is None or value == "":
        return default
    return int(value)


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronSchedule:
        return cls(
            kind=data["kind"],
            at_ms=_store_int(get_camel_snake(data, "atMs", "at_ms"), None),
            every_ms=_store_int(get_camel_snake(data, "everyMs", "every_ms"), None),
            expr=data.get("expr"),
            tz=data.get("tz"),
        )


@dataclass
class CronPayload:
    """What to do when the job runs."""
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    # Legacy delivery fields used by pre-session-bound cron jobs.
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number
    channel_meta: dict[str, Any] = field(default_factory=dict)
    session_key: str | None = None  # original session key for correct session recording
    origin_channel: str | None = None
    origin_chat_id: str | None = None
    origin_metadata: dict[str, Any] = field(default_factory=dict)
    # Typed ownership metadata survives cron save/load.  The runtime still
    # validates these ids against the server registry before using them.
    created_by: str | None = None
    tags: list[str] = field(default_factory=list)
    creator_actor: str | None = None
    target_actor: str | None = None
    owner_actor: str | None = None

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronPayload:
        raw_origin_metadata = get_camel_snake(
            data, "originMetadata", "origin_metadata", {}
        )
        origin_metadata = (
            dict(raw_origin_metadata) if isinstance(raw_origin_metadata, dict) else {}
        )
        created_by = get_camel_snake(data, "createdBy", "created_by")
        if created_by is None:
            created_by = get_camel_snake(origin_metadata, "createdBy", "created_by")
        if not isinstance(created_by, str) or not created_by.strip():
            created_by = None
        else:
            created_by = created_by.strip()
        creator_actor = get_camel_snake(data, "creatorActor", "creator_actor")
        if creator_actor is None:
            creator_actor = get_camel_snake(
                origin_metadata, "creatorActor", "creator_actor"
            )
        if not isinstance(creator_actor, str) or not creator_actor.strip():
            creator_actor = created_by
        else:
            creator_actor = creator_actor.strip()
        owner_actor = get_camel_snake(data, "ownerActor", "owner_actor")
        if owner_actor is None:
            owner_actor = get_camel_snake(origin_metadata, "ownerActor", "owner_actor")
        if not isinstance(owner_actor, str) or not owner_actor.strip():
            owner_actor = None
        else:
            owner_actor = owner_actor.strip()
        raw_tags = get_camel_snake(data, "tags", "tags", [])
        tags = (
            [tag.strip() for tag in raw_tags if isinstance(tag, str) and tag.strip()]
            if isinstance(raw_tags, list)
            else []
        )
        target_actor = get_camel_snake(data, "targetActor", "target_actor")
        if target_actor is None:
            target_actor = get_camel_snake(origin_metadata, "targetActor", "target_actor")
        if not isinstance(target_actor, str) or not target_actor.strip():
            target_actor = None
        else:
            target_actor = target_actor.strip()
        return cls(
            kind=data.get("kind", "agent_turn"),
            message=data.get("message", ""),
            deliver=data.get("deliver", False),
            channel=data.get("channel"),
            to=data.get("to"),
            channel_meta=dict(
                get_camel_snake(data, "channelMeta", "channel_meta", {}) or {}
            ),
            session_key=get_camel_snake(data, "sessionKey", "session_key"),
            origin_channel=get_camel_snake(data, "originChannel", "origin_channel"),
            origin_chat_id=get_camel_snake(data, "originChatId", "origin_chat_id"),
            origin_metadata=origin_metadata,
            created_by=created_by,
            tags=tags,
            creator_actor=creator_actor,
            target_actor=target_actor,
            owner_actor=owner_actor,
        )


@dataclass
class CronRunRecord:
    """A single execution record for a cron job."""
    run_at_ms: int
    status: Literal["ok", "error", "skipped"]
    duration_ms: int = 0
    error: str | None = None

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronRunRecord:
        return cls(
            run_at_ms=_store_int(get_camel_snake(data, "runAtMs", "run_at_ms", 0)),
            status=data["status"],
            duration_ms=_store_int(get_camel_snake(data, "durationMs", "duration_ms", 0)),
            error=data.get("error"),
        )


@dataclass
class CronJobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    run_history: list[CronRunRecord] = field(default_factory=list)

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronJobState:
        history = get_camel_snake(data, "runHistory", "run_history", []) or []
        return cls(
            next_run_at_ms=_store_int(
                get_camel_snake(data, "nextRunAtMs", "next_run_at_ms"), None
            ),
            last_run_at_ms=_store_int(
                get_camel_snake(data, "lastRunAtMs", "last_run_at_ms"), None
            ),
            last_status=get_camel_snake(data, "lastStatus", "last_status"),
            last_error=get_camel_snake(data, "lastError", "last_error"),
            run_history=[
                record
                if isinstance(record, CronRunRecord)
                else CronRunRecord.from_store_dict(record)
                for record in history
                if isinstance(record, (dict, CronRunRecord))
            ],
        )


@dataclass
class CronJob:
    """A scheduled job."""
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False

    @classmethod
    def from_dict(cls, kwargs: dict):
        state_kwargs = dict(kwargs.get("state", {}))
        state_kwargs["run_history"] = [
            record if isinstance(record, CronRunRecord) else CronRunRecord(**record)
            for record in state_kwargs.get("run_history", [])
        ]
        kwargs["schedule"] = CronSchedule(**kwargs.get("schedule", {"kind": "every"}))
        kwargs["payload"] = CronPayload(**kwargs.get("payload", {}))
        kwargs["state"] = CronJobState(**state_kwargs)
        return cls(**kwargs)

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronJob:
        """Load a job from jobs.json (camelCase with snake_case fallbacks)."""
        return cls(
            id=data["id"],
            name=data["name"],
            enabled=data.get("enabled", True),
            schedule=CronSchedule.from_store_dict(data["schedule"]),
            payload=CronPayload.from_store_dict(data.get("payload") or {}),
            state=CronJobState.from_store_dict(data.get("state") or {}),
            created_at_ms=_store_int(get_camel_snake(data, "createdAtMs", "created_at_ms", 0)),
            updated_at_ms=_store_int(get_camel_snake(data, "updatedAtMs", "updated_at_ms", 0)),
            delete_after_run=bool(
                get_camel_snake(data, "deleteAfterRun", "delete_after_run", False)
            ),
        )


@dataclass
class CronStore:
    """Persistent store for cron jobs."""
    version: int = 2
    jobs: list[CronJob] = field(default_factory=list)
