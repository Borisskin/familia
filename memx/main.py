from fastapi import FastAPI, WebSocket, Request, HTTPException
from store import CorruptRecordError, delete_value, get_value, set_value
from pubsub import subscribe, publish
from schema import (
    CorruptSchemaError,
    delete_schema,
    get_schema,
    register_schema,
    validate_schema,
)
import asyncio
import jsonschema
import validate_api

app = FastAPI()

_MISSING_AUXILIARY_OPERATION = object()

def _memory_key_parts(key: object) -> tuple[str, str] | None:
    if not isinstance(key, str):
        raise HTTPException(400, detail="key must be a string")

    parts = key.split(":")
    if len(parts) < 3 or parts[2] != "memory":
        return None

    if (
        len(parts) != 4
        or parts[0] != "private"
        or any(not part for part in parts)
    ):
        raise HTTPException(
            400,
            detail="Memory key must be private:<actor>:memory:<fact_id>",
        )
    return parts[1], parts[3]

def _validate_memory_index_update(
    key: object,
    index_update: object,
) -> dict | None:
    memory_parts = _memory_key_parts(key)
    if memory_parts is None:
        if index_update is not _MISSING_AUXILIARY_OPERATION:
            raise HTTPException(
                400,
                detail="index_update is only valid for a memory key",
            )
        return None

    actor, fact_id = memory_parts
    expected_index_key = f"private:{actor}:value:private_index"
    expected_name = f"memory:{fact_id}"
    if (
        not isinstance(index_update, dict)
        or index_update.keys() != {"key", "entry", "max_entries"}
    ):
        raise HTTPException(
            400,
            detail="Memory writes require the canonical private index update",
        )

    index_key = index_update["key"]
    entry = index_update["entry"]
    maximum = index_update["max_entries"]
    if (
        not isinstance(index_key, str)
        or index_key != expected_index_key
        or not isinstance(entry, dict)
        or entry.keys() != {"name", "tags"}
        or not isinstance(entry["name"], str)
        or entry["name"] != expected_name
        or not isinstance(entry["tags"], list)
        or not all(
            isinstance(tag, str) and len(tag) > 0
            for tag in entry["tags"]
        )
        or type(maximum) is not int
        or maximum != 256
    ):
        raise HTTPException(
            400,
            detail="Memory writes require the canonical private index update",
        )
    return {
        "key": index_key,
        "entry": {
            "name": entry["name"],
            "tags": list(entry["tags"]),
        },
        "max_entries": maximum,
    }

def _validate_memory_index_remove(
    key: object,
    index_remove: object,
) -> dict | None:
    memory_parts = _memory_key_parts(key)
    if memory_parts is None:
        if index_remove is not _MISSING_AUXILIARY_OPERATION:
            raise HTTPException(
                400,
                detail="index_remove is only valid for a memory key",
            )
        return None

    actor, fact_id = memory_parts
    if (
        not isinstance(index_remove, dict)
        or index_remove.keys() != {"key", "name"}
    ):
        raise HTTPException(
            400,
            detail="Memory deletes require the canonical private index removal",
        )

    index_key = index_remove["key"]
    name = index_remove["name"]
    if (
        not isinstance(index_key, str)
        or index_key != f"private:{actor}:value:private_index"
        or not isinstance(name, str)
        or name != f"memory:{fact_id}"
    ):
        raise HTTPException(
            400,
            detail="Memory deletes require the canonical private index removal",
        )
    return {"key": index_key, "name": name}

@app.get("/get")
async def get(key: str, request: Request):
    await validate_api.validate_api_key(request, key, action="read")
    namespaced_key = getattr(request.state, "namespaced_key", key)
    try:
        return get_value(namespaced_key)
    except CorruptRecordError as exc:
        raise HTTPException(
            409,
            detail={
                "status": "corruption_needs_repair",
                "committed": False,
                "retryable": False,
                "message": str(exc),
            },
        ) from exc

@app.post("/set")
async def set(request: Request):
    body = await request.json()
    key = body["key"]

    index_update = _validate_memory_index_update(
        key,
        body.get("index_update", _MISSING_AUXILIARY_OPERATION),
    )
    value = body["value"]
    if index_update is None:
        await validate_api.validate_api_key(request, key, action="write")
        namespaced_key = getattr(request.state, "namespaced_key", key)
        namespaced_index_update = None
    else:
        namespaced_key, namespaced_index_key = (
            await validate_api.validate_api_keys(
                request,
                (key, index_update["key"]),
                action="write",
            )
        )
        namespaced_index_update = {
            "key": namespaced_index_key,
            "entry": {
                "name": index_update["entry"]["name"],
                "tags": list(index_update["entry"]["tags"]),
            },
            "max_entries": index_update["max_entries"],
        }

    try:
        validate_schema(namespaced_key, value)
    except jsonschema.exceptions.ValidationError as e:
        raise HTTPException(400, detail=str(e))
    except CorruptSchemaError as exc:
        raise HTTPException(
            409,
            detail={
                "status": "corruption_needs_repair",
                "committed": False,
                "retryable": False,
                "message": str(exc),
            },
        ) from exc

    try:
        set_kwargs = {"index_update": namespaced_index_update}
        if "expected_ts" in body:
            set_kwargs["expected_ts"] = body["expected_ts"]
        result = set_value(namespaced_key, value, **set_kwargs)
    except ValueError as exc:
        raise HTTPException(
            400,
            detail={
                "status": "denied_invalid",
                "committed": False,
                "retryable": False,
                "message": str(exc),
            },
        ) from exc
    except CorruptRecordError as exc:
        raise HTTPException(
            409,
            detail={
                "status": "corruption_needs_repair",
                "committed": False,
                "retryable": False,
                "message": str(exc),
            },
        ) from exc
    if result.committed:
        await publish(
            namespaced_key,
            {"event": "value", "key": namespaced_key, "value": value},
            event="value",
        )

    return result.as_payload()


@app.post("/delete")
async def delete(request: Request):
    body = await request.json()
    key = body["key"]

    index_remove = _validate_memory_index_remove(
        key,
        body.get("index_remove", _MISSING_AUXILIARY_OPERATION),
    )
    if index_remove is None:
        await validate_api.validate_api_key(request, key, action="write")
        namespaced_key = getattr(request.state, "namespaced_key", key)
        namespaced_index_remove = None
    else:
        namespaced_key, namespaced_index_key = (
            await validate_api.validate_api_keys(
                request,
                (key, index_remove["key"]),
                action="write",
            )
        )
        namespaced_index_remove = {
            "key": namespaced_index_key,
            "name": index_remove["name"],
        }

    try:
        delete_kwargs = {}
        if namespaced_index_remove is not None:
            delete_kwargs["index_remove"] = namespaced_index_remove
        if "expected_ts" in body:
            delete_kwargs["expected_ts"] = body["expected_ts"]
        result = delete_value(namespaced_key, **delete_kwargs)
    except ValueError as exc:
        raise HTTPException(
            400,
            detail={
                "status": "denied_invalid",
                "committed": False,
                "retryable": False,
                "message": str(exc),
            },
        ) from exc
    except CorruptRecordError as exc:
        raise HTTPException(
            409,
            detail={
                "status": "corruption_needs_repair",
                "committed": False,
                "retryable": False,
                "message": str(exc),
            },
        ) from exc

    if result.updated:
        await publish(
            namespaced_key,
            {"event": "delete", "key": namespaced_key},
            event="value",
        )

    return result.as_payload()


@app.websocket("/subscribe/{key}")
async def websocket_endpoint(websocket: WebSocket, key: str):
    await websocket.accept()
    namespaced_key = await validate_api.validate_websocket(websocket, key)
    if not namespaced_key:
        return
    event_type = websocket.query_params.get("event", "value")
    print(f"[WebSocket] Subscribed to {namespaced_key} for event '{event_type}'")
    subscribe(namespaced_key, websocket, event=event_type)
    try:
        while True:
            await asyncio.sleep(1)
    except:
        print(f"[WebSocket] closed: {namespaced_key} ({event_type})")

@app.post("/schema")
async def set_schema(request: Request):
    body = await request.json()
    key = body["key"]
    schema = body["schema"]

    await validate_api.validate_api_key(request, key, action="write")
    namespaced_key = getattr(request.state, "namespaced_key", key)

    try:
        register_schema(namespaced_key, schema)
    except jsonschema.exceptions.SchemaError as e:
        raise HTTPException(400, detail=f"Invalid schema: {e}")
    await publish(
        namespaced_key,
        {"event": "schema", "action": "set", "key": namespaced_key, "schema": schema},
        event="schema",
    )
    return {"ok": True}

@app.get("/schema")
async def fetch_schema(key: str, request: Request):
    await validate_api.validate_api_key(request, key, action="read")
    namespaced_key = getattr(request.state, "namespaced_key", key)
    try:
        schema = get_schema(namespaced_key)
    except CorruptSchemaError as exc:
        raise HTTPException(
            409,
            detail={"status": "corruption_needs_repair", "message": str(exc)},
        ) from exc
    if not schema:
        raise HTTPException(404, detail="Schema not found")
    return {"key": key, "schema": schema}

@app.delete("/schema")
async def remove_schema(key: str, request: Request):
    await validate_api.validate_api_key(request, key, action="write")
    namespaced_key = getattr(request.state, "namespaced_key", key)
    deleted = delete_schema(namespaced_key)
    if not deleted:
        raise HTTPException(404, detail="Schema not found")
    await publish(
        namespaced_key,
        {"event": "schema", "action": "delete", "key": namespaced_key},
        event="schema",
    )
    return {"ok": True}
