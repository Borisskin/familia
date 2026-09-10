import httpx
import asyncio
import websockets
import threading
import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SemanticWriteResult:
    status: str
    committed: bool
    updated: bool
    retryable: bool
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "SemanticWriteResult":
        if not isinstance(payload, dict):
            raise ValueError("memX returned a non-object semantic result")
        status = payload.get("status")
        committed = payload.get("committed")
        updated = payload.get("updated")
        retryable = payload.get("retryable")
        if (
            not isinstance(status, str)
            or not isinstance(committed, bool)
            or not isinstance(updated, bool)
            or not isinstance(retryable, bool)
        ):
            raise ValueError("memX returned an incomplete semantic result")
        if committed != (status == "committed" and updated):
            raise ValueError("memX returned a contradictory semantic result")
        return cls(
            status=status,
            committed=committed,
            updated=updated,
            retryable=retryable,
            raw=dict(payload),
        )

class memxContext:
    def __init__(self, api_key, base_url=None):
        # Local-first by default; override via env or argument for hosted deployments.
        default_base = os.getenv("MEMX_BASE_URL", "http://127.0.0.1:8000")
        self.api_key = api_key
        self.base_url = base_url or default_base

    def set(self, key, value):
        res = httpx.post(
            f"{self.base_url}/set",
            headers={"x-api-key": self.api_key},
            json={"key": key, "value": value}
        )
        res.raise_for_status()
        return SemanticWriteResult.from_payload(res.json())

    def get(self, key):
        res = httpx.get(
            f"{self.base_url}/get",
            headers={"x-api-key": self.api_key},
            params={"key": key}
        )
        res.raise_for_status()
        return res.json()

    def subscribe(self, key, callback):
        def _listen():
            uri = f"{self.base_url.replace('http', 'ws')}/subscribe/{key}"
            async def _inner():
                async with websockets.connect(uri, additional_headers={"x-api-key": self.api_key}) as ws:
                    while True:
                        try:
                            msg = await ws.recv()
                            data = json.loads(msg)
                            callback(data)
                        except Exception as e:
                            print("[WebSocket error]", e)
                            break
            asyncio.run(_inner())

        thread = threading.Thread(target=_listen, daemon=True)
        thread.start()

    def set_schema(self, key, schema):
        res = httpx.post(
            f"{self.base_url}/schema",
            headers={"x-api-key": self.api_key},
            json={"key": key, "schema": schema}
        )
        res.raise_for_status()
        return res.json()

    def get_schema(self, key):
        res = httpx.get(
            f"{self.base_url}/schema",
            headers={"x-api-key": self.api_key},
            params={"key": key}
        )
        res.raise_for_status()
        return res.json()

    def delete_schema(self, key):
        res = httpx.delete(
            f"{self.base_url}/schema",
            headers={"x-api-key": self.api_key},
            params={"key": key}
        )
        res.raise_for_status()
        return res.json()
