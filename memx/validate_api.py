from __future__ import annotations

import fnmatch
import json
import os
from typing import Any, Sequence

try:
    from dotenv import load_dotenv
except ImportError:  # dependency-free ACL helper imports in release tests
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
supabase = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    try:
        from supabase import create_client
    except ImportError:
        create_client = None
    if create_client is not None:
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Fallback ACL for local/dev when Supabase is not configured or unreachable.
try:
    with open("config/acl.json") as f:
        LOCAL_ACL = json.load(f)
except Exception:
    LOCAL_ACL = {}

def _check_scope(scopes: dict, action: str, key: str, user_id: str):
    namespaced_key = key if not user_id else f"{user_id[:8]}:{key}"
    return any(fnmatch.fnmatch(namespaced_key, pattern) for pattern in scopes.get(action, []))

def _apply_namespace(key: str, record: dict) -> str:
    user_id = record.get("user_id", "")
    return key if not user_id else f"{user_id[:8]}:{key}"

def _authorize_keys(
    record: dict,
    keys: Sequence[str],
    action: str,
) -> tuple[list[str], str | None]:
    scopes = record.get("scopes", {})
    user_id = record.get("user_id", "")
    namespaced_keys: list[str] = []

    for key in keys:
        if not _check_scope(scopes, action, key, user_id):
            return namespaced_keys, key
        namespaced_keys.append(_apply_namespace(key, record))

    return namespaced_keys, None

async def validate_api_keys(
    request: Any,
    keys: Sequence[str],
    action: str = "write",
) -> list[str]:
    from fastapi import HTTPException

    logical_keys = tuple(keys)
    if not logical_keys:
        raise ValueError("At least one key is required")

    api_key = request.headers.get("x-api-key")
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    record = None
    if supabase:
        try:
            res = supabase.from_("api_keys").select("*").eq("key", api_key).eq("active", True).execute()
            if res.data:
                record = res.data[0]
        except Exception as e:
            print("[validate_api] Supabase lookup failed, falling back to local ACL:", e)

    if not record:
        local_patterns = LOCAL_ACL.get(api_key, [])
        if not local_patterns:
            raise HTTPException(status_code=403, detail="Invalid or inactive API key")
        record = {"user_id": "", "scopes": {"read": local_patterns, "write": local_patterns}}

    namespaced_keys, denied_key = _authorize_keys(record, logical_keys, action)

    if namespaced_keys:
        request.state.api_key = record
        request.state.namespaced_key = namespaced_keys[0]

    if denied_key is not None:
        raise HTTPException(
            status_code=403,
            detail=f"{action.upper()} not permitted for key '{denied_key}'",
        )

    return namespaced_keys

async def validate_api_key(request: Any, key: str, action: str = "write"):
    await validate_api_keys(request, (key,), action=action)

async def validate_websocket(websocket: Any, key: str):
    api_key = websocket.headers.get("x-api-key")
    if not api_key:
        await websocket.close(code=4401, reason="Missing API key")
        return False

    record = None
    if supabase:
        try:
            res = supabase.table("api_keys").select("*").eq("key", api_key).eq("active", True).execute()
            if res.data:
                record = res.data[0]
        except Exception as e:
            print("[validate_api] Supabase lookup failed for websocket, falling back to local ACL:", e)

    if not record:
        local_patterns = LOCAL_ACL.get(api_key, [])
        if not local_patterns:
            await websocket.close(code=4403, reason="Invalid API key")
            return False
        record = {"user_id": "", "scopes": {"read": local_patterns, "write": local_patterns}}

    namespaced_keys, denied_key = _authorize_keys(record, (key,), "read")
    if denied_key is not None:
        await websocket.close(code=4403, reason="Unauthorized to subscribe to this key")
        return False

    return namespaced_keys[0]
