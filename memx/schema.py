import json
import jsonschema
from redis_client import get_client


_redis = get_client()
SCHEMA_PREFIX = "memx:schema:"


class CorruptSchemaError(RuntimeError):
    """Stored schema bytes are present but invalid and require repair."""


def _redis_key(key: str) -> str:
    return f"{SCHEMA_PREFIX}{key}"


def register_schema(key, schema_dict):
    jsonschema.Draft7Validator.check_schema(schema_dict)
    _redis.set(_redis_key(key), json.dumps(schema_dict))


def validate_schema(key, value):
    schema = get_schema(key)
    if schema:
        jsonschema.validate(instance=value, schema=schema)


def get_schema(key):
    raw = _redis.get(_redis_key(key))
    if not raw:
        return None
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorruptSchemaError(f"corrupt schema JSON for '{key}'") from exc
    if not isinstance(schema, dict):
        raise CorruptSchemaError(f"corrupt schema shape for '{key}'")
    return schema


def delete_schema(key):
    return _redis.delete(_redis_key(key)) == 1
