"""One conditional memX writer for automatic principal-memory operations."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

import httpx

from familia.acl import codec
from familia.principals import get_registry

_MAX_CAS_ATTEMPTS = 3
_FACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SEMANTIC_RESULT_FIELDS = frozenset(
    {"ok", "status", "committed", "updated", "retryable", "version"}
)


def _parse_memx_version(value: Any, *, allow_none: bool) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        # All malformed memX payload fields share one validation contract.
        raise ValueError  # noqa: TRY004
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError from exc
    if not math.isfinite(converted):
        raise ValueError
    if isinstance(value, int) and int(converted) != value:
        raise ValueError
    return converted


class PrincipalMemoryIngestor:
    """Validate one automatic operation and commit it conditionally to memX."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        server_topic_validator: Callable[[str], bool] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._server_topic_validator = server_topic_validator

    async def ingest(
        self,
        *,
        server_principal: str,
        server_topic: str | None,
        operation: dict[str, Any],
    ) -> str:
        if (
            not isinstance(server_principal, str)
            or not server_principal.strip()
            or get_registry().get(server_principal) is None
        ):
            return "denied_invalid: unknown server principal"
        if not isinstance(operation, dict):
            return "denied_invalid: automatic memory operation must be an object"

        kind = operation.get("kind")
        delete = kind == "delete"
        if delete and server_topic is not None:
            return "denied_invalid: delete does not accept server topic"
        if server_topic is not None:
            if not isinstance(server_topic, str) or not server_topic.strip():
                return "denied_invalid: invalid server topic"
            if self._server_topic_validator is None:
                return "denied_invalid: server topic is not verified"
            try:
                topic_valid = self._server_topic_validator(server_topic)
            # The configured validator is an external trust boundary.
            except Exception:  # noqa: BLE001
                return "denied_invalid: server topic validation failed"
            if topic_valid is not True:
                return "denied_invalid: unknown or untrusted server topic"

        if delete:
            if set(operation) != {"kind", "fact_id"}:
                return "denied_invalid: delete operation has unsupported fields"
            fact_id = operation.get("fact_id")
            if not isinstance(fact_id, str) or _FACT_ID_RE.fullmatch(fact_id) is None:
                return "denied_invalid: delete operation has invalid fact_id"
            full_key = f"private:{server_principal}:memory:{fact_id}"
        else:
            value = operation.get("value")
            if not isinstance(value, str) or not value.strip():
                return "denied_invalid: automatic memory operation has no value"

        if kind == "profile":
            if set(operation) != {"kind", "value"}:
                return "denied_invalid: profile operation has unsupported fields"
            full_key = f"private:{server_principal}:value:user_profile"
        elif kind == "memory":
            if set(operation) != {"kind", "fact_id", "value"}:
                return "denied_invalid: memory operation has unsupported fields"
            fact_id = operation.get("fact_id")
            if not isinstance(fact_id, str) or _FACT_ID_RE.fullmatch(fact_id) is None:
                return "denied_invalid: memory operation has invalid fact_id"
            full_key = f"private:{server_principal}:memory:{fact_id}"
        elif not delete:
            return "denied_invalid: unsupported automatic memory operation kind"

        if not delete:
            stored_value = (
                codec.encode(value, [server_topic])
                if server_topic is not None
                else value
            )
        headers = {"x-api-key": self._api_key}

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                for _attempt in range(_MAX_CAS_ATTEMPTS):
                    current = await client.get(
                        f"{self._base_url}/get",
                        headers=headers,
                        params={"key": full_key},
                    )
                    if current.status_code == 404:
                        current_payload = None
                    elif current.status_code >= 400:
                        return f"error: memX read status {current.status_code}"
                    else:
                        try:
                            current_payload = current.json()
                        except (TypeError, ValueError):
                            return "error: memX returned a non-JSON current record"

                    if current_payload is None:
                        expected_ts: float | None = None
                    elif isinstance(current_payload, dict):
                        try:
                            expected_ts = _parse_memx_version(
                                current_payload.get("ts"),
                                allow_none=False,
                            )
                        except ValueError:
                            return "error: memX current record has invalid ts"
                    else:
                        return "error: memX current record is invalid"

                    write_payload: dict[str, Any] = {"key": full_key}
                    if delete:
                        write_payload["index_remove"] = {
                            "key": (
                                f"private:{server_principal}:"
                                "value:private_index"
                            ),
                            "name": f"memory:{fact_id}",
                        }
                    else:
                        write_payload["value"] = stored_value
                        if kind == "memory":
                            write_payload["index_update"] = {
                                "key": (
                                    f"private:{server_principal}:"
                                    "value:private_index"
                                ),
                                "entry": {
                                    "name": f"memory:{fact_id}",
                                    "tags": (
                                        [server_topic]
                                        if server_topic is not None
                                        else []
                                    ),
                                },
                                "max_entries": 256,
                            }
                    write_payload["expected_ts"] = expected_ts
                    committed = await client.post(
                        f"{self._base_url}/{'delete' if delete else 'set'}",
                        headers=headers,
                        json=write_payload,
                    )
                    if committed.status_code >= 400:
                        return f"error: memX write status {committed.status_code}"
                    try:
                        payload = committed.json()
                    except (TypeError, ValueError):
                        return "error: memX returned a non-JSON semantic result"
                    if not isinstance(payload, dict):
                        return "error: memX returned a non-object semantic result"
                    if frozenset(payload) != _SEMANTIC_RESULT_FIELDS:
                        return "error: memX did not confirm the conditional commit"
                    try:
                        version = _parse_memx_version(
                            payload["version"],
                            allow_none=True,
                        )
                    except ValueError:
                        return "error: memX did not confirm the conditional commit"
                    if (
                        delete
                        and payload.get("ok") is True
                        and payload.get("status") == "deleted"
                        and payload.get("committed") is True
                        and payload.get("updated") is True
                        and payload.get("retryable") is False
                    ):
                        return f"deleted: Removed '{full_key}'"
                    if (
                        delete
                        and payload.get("ok") is True
                        and payload.get("status") == "absent"
                        and payload.get("committed") is True
                        and payload.get("updated") is False
                        and payload.get("retryable") is False
                        and version is None
                    ):
                        return f"absent: Already removed '{full_key}'"
                    if (
                        not delete
                        and payload.get("ok") is True
                        and payload.get("status") == "committed"
                        and payload.get("committed") is True
                        and payload.get("updated") is True
                        and payload.get("retryable") is False
                        and version is not None
                    ):
                        return f"committed: Stored at '{full_key}'"
                    if (
                        kind == "memory"
                        and payload.get("ok") is True
                        and payload.get("status") == "catalog_full"
                        and payload.get("committed") is False
                        and payload.get("updated") is False
                        and payload.get("retryable") is False
                    ):
                        return (
                            "catalog_full: Personal memory catalog is full; "
                            f"did not store '{full_key}'"
                        )
                    if (
                        payload.get("ok") is True
                        and payload.get("status") == "conflict"
                        and payload.get("committed") is False
                        and payload.get("updated") is False
                        and payload.get("retryable") is True
                    ):
                        continue
                    return "error: memX did not confirm the conditional commit"
        except httpx.HTTPError as exc:
            return f"error: memX unreachable ({type(exc).__name__}: {exc})"

        return (
            "retryable_failure: memX conditional commit failed after "
            f"{_MAX_CAS_ATTEMPTS} attempts"
        )
