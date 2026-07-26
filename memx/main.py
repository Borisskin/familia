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
    value = body["value"]
    key = body["key"]

    await validate_api.validate_api_key(request, key, action="write")
    namespaced_key = getattr(request.state, "namespaced_key", key)

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
        set_kwargs = {"index_update": body.get("index_update")}
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

    await validate_api.validate_api_key(request, key, action="write")
    namespaced_key = getattr(request.state, "namespaced_key", key)

    try:
        delete_kwargs = {}
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
