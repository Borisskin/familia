"""One conditional memX writer for automatic principal-memory operations."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx

from familia.acl import codec
from familia.principals import get_registry


_MAX_CAS_ATTEMPTS = 3
_FACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


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
            except Exception:
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
                        expected_ts = current_payload.get("ts")
                        if (
                            not isinstance(expected_ts, (int, float))
                            or isinstance(expected_ts, bool)
                        ):
                            return "error: memX current record has invalid ts"
                    else:
                        return "error: memX current record is invalid"

                    write_payload: dict[str, Any] = {"key": full_key}
                    if not delete:
                        write_payload["value"] = stored_value
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
                    if (
                        delete
                        and payload.get("ok") is True
                        and payload.get("status") == "deleted"
                        and payload.get("committed") is True
                        and payload.get("updated") is True
                        and payload.get("retryable") is False
                        and payload.get("version") is not None
                    ):
                        return f"deleted: Removed '{full_key}'"
                    if (
                        delete
                        and payload.get("ok") is True
                        and payload.get("status") == "absent"
                        and payload.get("committed") is True
                        and payload.get("updated") is False
                        and payload.get("retryable") is False
                        and payload.get("version") is None
                    ):
                        return f"absent: Already removed '{full_key}'"
                    if (
                        not delete
                        and payload.get("ok") is True
                        and payload.get("status") == "committed"
                        and payload.get("committed") is True
                        and payload.get("updated") is True
                        and payload.get("retryable") is False
                        and payload.get("version") is not None
                    ):
                        return f"committed: Stored at '{full_key}'"
                    if (
                        payload.get("ok") is True
                        and payload.get("status") == "conflict"
                        and payload.get("committed") is False
                        and payload.get("updated") is False
                        and payload.get("retryable") is True
                        and payload.get("version") is not None
                    ):
                        continue
                    return "error: memX did not confirm the conditional commit"
        except httpx.HTTPError as exc:
            return f"error: memX unreachable ({type(exc).__name__}: {exc})"

        return f"retryable_failure: memX CAS conflict after {_MAX_CAS_ATTEMPTS} attempts"
