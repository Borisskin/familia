#!/usr/bin/env python3
"""Validate, compare, capture, and restore RP-010 memory state.

The public interface is the two-manifest comparator.  The shell entry points
call the private ``_capture`` and ``_restore`` modes so all three operations use
the same strict manifest and payload validator.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "release" / "memory-snapshot.schema.json"
SCHEMA_VERSION = "1.0.0"
FORMAT_VERSION = "1.0.0"
FILE_CLASSES = (
    "nanobot_files",
    "workspace_memory",
    "history",
    "cursors",
    "scheduler",
    "principals",
    "acl_policy",
    "non_secret_config",
)
VERSION_KEYS = ("familia", "nanobot", "memx", "snapshot_schema", "acl", "policy")
CLASS_COUNT_KEYS = FILE_CLASSES + (
    "redis_raw",
    "memx_values",
    "schemas",
    "discovery_indexes",
    "logical_records",
    "access_matrix",
    "anomalies",
)
RELATIVE_PATH_RE = re.compile(
    r"^(?!/)(?!.*\\)(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*//)"
    r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(Exception):
    """A fail-closed error whose code is safe to emit in evidence logs."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DuplicateJSONKey(ValueError):
    pass


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_utc(value: Any, code: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(code)
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceError(code) from exc
    if parsed.tzinfo is None:
        raise EvidenceError(code)
    return parsed


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKey(key)
        result[key] = value
    return result


def _decode_json(data: bytes, code: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError(code) from exc
    try:
        return json.loads(text, object_pairs_hook=_pairs_without_duplicates)
    except (json.JSONDecodeError, DuplicateJSONKey) as exc:
        raise EvidenceError(code) from exc


def _load_json_file(path: Path, code: str) -> Any:
    _assert_no_symlink_chain(path, require_leaf=True)
    try:
        return _decode_json(path.read_bytes(), code)
    except OSError as exc:
        raise EvidenceError(code) from exc


def _require_exact_keys(value: Any, keys: Iterable[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise EvidenceError(code)
    return value


def _require_nonempty(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise EvidenceError(code)
    return value


def _validate_relative_path(value: Any, code: str = "unsafe_relative_path") -> str:
    if not isinstance(value, str) or not RELATIVE_PATH_RE.fullmatch(value):
        raise EvidenceError(code)
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise EvidenceError(code)
    return value


def _assert_no_symlink_chain(path: Path, *, require_leaf: bool) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    parts = absolute.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        is_leaf = index == len(parts) - 1
        try:
            info = current.lstat()
        except FileNotFoundError:
            if require_leaf or not is_leaf:
                raise EvidenceError("path_missing")
            return
        except OSError as exc:
            raise EvidenceError("path_unreadable") from exc
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError("symlink_path_refused")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _safe_read_file(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceError("source_file_unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceError("unsupported_source_object")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise EvidenceError("source_changed_during_read")
    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise EvidenceError("source_changed_during_read")
    return data, before


def _schema_validator():
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:
        raise EvidenceError("draft202012_validator_unavailable") from exc
    schema = _load_json_file(SCHEMA_PATH, "schema_json_invalid")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise EvidenceError("schema_meta_invalid") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _resolve_payload(root: Path, relative: str) -> Path:
    relative = _validate_relative_path(relative)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    _assert_no_symlink_chain(candidate, require_leaf=True)
    if not candidate.is_file():
        raise EvidenceError("payload_not_regular")
    if not _is_within(candidate, root):
        raise EvidenceError("payload_escape")
    return candidate


def _unique_by_identity(values: list[dict[str, Any]], code: str) -> None:
    identities = [value.get("identity") for value in values]
    if len(identities) != len(set(identities)):
        raise EvidenceError(code)


def _computed_class_counts(manifest: dict[str, Any]) -> dict[str, int]:
    inventory = manifest["inventory"]
    result = {key: 0 for key in CLASS_COUNT_KEYS}
    for entry in inventory["flat_files"]:
        for file_class in entry["classes"]:
            result[file_class] += 1
    result["redis_raw"] = len(inventory["redis"])
    for entry in inventory["redis"]:
        if entry["category"] == "memx_value":
            result["memx_values"] += 1
        if entry["category"] == "schema":
            result["schemas"] += 1
        if entry["category"] == "discovery_index":
            result["discovery_indexes"] += 1
    logical = inventory["logical"]
    result["logical_records"] = len(logical["records"])
    result["access_matrix"] = len(logical["access_matrix"])
    result["anomalies"] = len(logical["anomalies"])
    return result


def _logical_core(logical: dict[str, Any]) -> dict[str, Any]:
    return {
        "records": logical["records"],
        "access_matrix": logical["access_matrix"],
        "anomalies": logical["anomalies"],
    }


def _verify_manifest_integrity(manifest: dict[str, Any], root: Path) -> None:
    inventory = manifest["inventory"]
    flat_files = inventory["flat_files"]
    redis_entries = inventory["redis"]
    logical = inventory["logical"]
    _unique_by_identity(flat_files, "duplicate_flat_identity")
    _unique_by_identity(redis_entries, "duplicate_redis_identity")
    _unique_by_identity(logical["records"], "duplicate_logical_identity")
    _unique_by_identity(logical["access_matrix"], "duplicate_access_identity")
    _unique_by_identity(logical["anomalies"], "duplicate_anomaly_identity")

    payload_paths: list[str] = []
    for entry in flat_files:
        payload_paths.append(entry["payload_path"])
        payload = _resolve_payload(root, entry["payload_path"])
        if payload.stat().st_size != entry["byte_length"]:
            raise EvidenceError("payload_size_mismatch")
        if _sha256_file(payload) != entry["sha256"]:
            raise EvidenceError("payload_hash_mismatch")
    for entry in redis_entries:
        specifications = (
            ("key_payload_path", "key_byte_length", "key_sha256"),
            ("dump_payload_path", "dump_byte_length", "dump_sha256"),
        )
        for path_key, length_key, hash_key in specifications:
            payload_paths.append(entry[path_key])
            payload = _resolve_payload(root, entry[path_key])
            if payload.stat().st_size != entry[length_key]:
                raise EvidenceError("payload_size_mismatch")
            if _sha256_file(payload) != entry[hash_key]:
                raise EvidenceError("payload_hash_mismatch")
        if entry["value_payload_path"] is not None:
            payload_paths.append(entry["value_payload_path"])
            payload = _resolve_payload(root, entry["value_payload_path"])
            if payload.stat().st_size != entry["value_byte_length"]:
                raise EvidenceError("payload_size_mismatch")
            if _sha256_file(payload) != entry["value_sha256"]:
                raise EvidenceError("payload_hash_mismatch")
    if len(payload_paths) != len(set(payload_paths)):
        raise EvidenceError("duplicate_payload_reference")

    if logical["record_count"] != len(logical["records"]):
        raise EvidenceError("logical_count_mismatch")
    if logical["access_count"] != len(logical["access_matrix"]):
        raise EvidenceError("access_count_mismatch")
    if logical["anomaly_count"] != len(logical["anomalies"]):
        raise EvidenceError("anomaly_count_mismatch")
    logical_hash = _canonical_hash(_logical_core(logical))
    if logical["aggregate_sha256"] != logical_hash:
        raise EvidenceError("logical_aggregate_mismatch")
    byte_hash = _canonical_hash({"flat_files": flat_files, "redis": redis_entries})
    if inventory["byte_aggregate_sha256"] != byte_hash:
        raise EvidenceError("byte_aggregate_mismatch")
    if manifest["content_hashes"] != {
        "algorithm": "sha256",
        "byte_state_sha256": byte_hash,
        "logical_state_sha256": logical_hash,
    }:
        raise EvidenceError("content_hash_mismatch")
    if inventory["class_counts"] != _computed_class_counts(manifest):
        raise EvidenceError("class_count_mismatch")
    expected_snapshot_id = _canonical_hash(
        {
            "snapshot_format_version": manifest["snapshot_format_version"],
            "versions": manifest["versions"],
            "images": manifest["images"],
            "content_hashes": manifest["content_hashes"],
        }
    )
    if manifest["snapshot_id"] != expected_snapshot_id:
        raise EvidenceError("snapshot_id_mismatch")


def load_and_validate_manifest(path: Path) -> dict[str, Any]:
    path = path.absolute()
    _assert_no_symlink_chain(path, require_leaf=True)
    manifest = _load_json_file(path, "manifest_json_invalid")
    validator = _schema_validator()
    try:
        errors = list(validator.iter_errors(manifest))
    except Exception as exc:
        raise EvidenceError("manifest_schema_invalid") from exc
    if errors:
        raise EvidenceError("manifest_schema_invalid")
    _verify_manifest_integrity(manifest, path.parent)
    return manifest


def _config(path: Path) -> dict[str, Any]:
    config = _load_json_file(path.absolute(), "capture_config_invalid")
    config = _require_exact_keys(
        config,
        (
            "config_version",
            "authorization",
            "source",
            "required_file_classes",
            "non_secret_config_paths",
            "redis",
            "versions",
            "images",
        ),
        "capture_config_invalid",
    )
    if config["config_version"] != "1.0.0":
        raise EvidenceError("capture_config_version_unsupported")
    authorization = _require_exact_keys(
        config["authorization"],
        (
            "authorization_id",
            "source_kind",
            "writer_stop_started_at",
            "writer_stop_ended_at",
        ),
        "capture_authorization_invalid",
    )
    _require_nonempty(authorization["authorization_id"], "capture_authorization_invalid")
    if authorization["source_kind"] not in {"sanitized_fixture", "authorized_prechange"}:
        raise EvidenceError("capture_authorization_invalid")
    started = _parse_utc(
        authorization["writer_stop_started_at"], "writer_stop_interval_invalid"
    )
    ended = _parse_utc(
        authorization["writer_stop_ended_at"], "writer_stop_interval_invalid"
    )
    if ended < started:
        raise EvidenceError("writer_stop_interval_invalid")
    source = _require_exact_keys(
        config["source"],
        ("root", "host_id", "deployment_id", "logical_paths"),
        "capture_source_invalid",
    )
    root = Path(_require_nonempty(source["root"], "capture_source_invalid"))
    if not root.is_absolute():
        raise EvidenceError("capture_source_invalid")
    _require_nonempty(source["host_id"], "capture_source_invalid")
    _require_nonempty(source["deployment_id"], "capture_source_invalid")
    if (
        not isinstance(source["logical_paths"], list)
        or not source["logical_paths"]
        or any(not isinstance(item, str) or not item for item in source["logical_paths"])
    ):
        raise EvidenceError("capture_source_invalid")
    classes = _require_exact_keys(
        config["required_file_classes"], FILE_CLASSES, "file_class_config_invalid"
    )
    for file_class, paths in classes.items():
        if not isinstance(paths, list) or not paths:
            raise EvidenceError("file_class_config_invalid")
        classes[file_class] = [
            _validate_relative_path(item, "file_class_config_invalid") for item in paths
        ]
    non_secret = config["non_secret_config_paths"]
    if not isinstance(non_secret, list) or not non_secret:
        raise EvidenceError("non_secret_config_invalid")
    config["non_secret_config_paths"] = [
        _validate_relative_path(item, "non_secret_config_invalid") for item in non_secret
    ]
    redis = _require_exact_keys(
        config["redis"],
        (
            "host",
            "port",
            "database",
            "logical_endpoint",
            "isolation_marker_key",
            "isolation_marker_sha256",
            "password_env",
        ),
        "redis_config_invalid",
    )
    _require_nonempty(redis["host"], "redis_config_invalid")
    _require_nonempty(redis["logical_endpoint"], "redis_config_invalid")
    if not isinstance(redis["port"], int) or not 1 <= redis["port"] <= 65535:
        raise EvidenceError("redis_config_invalid")
    if not isinstance(redis["database"], int) or not 0 <= redis["database"] <= 65535:
        raise EvidenceError("redis_config_invalid")
    for key in ("isolation_marker_key", "isolation_marker_sha256", "password_env"):
        if redis[key] is not None and not isinstance(redis[key], str):
            raise EvidenceError("redis_config_invalid")
    if redis["isolation_marker_sha256"] is not None and not SHA256_RE.fullmatch(
        redis["isolation_marker_sha256"]
    ):
        raise EvidenceError("redis_config_invalid")
    if authorization["source_kind"] == "sanitized_fixture" and (
        not redis["isolation_marker_key"] or not redis["isolation_marker_sha256"]
    ):
        raise EvidenceError("fixture_isolation_proof_missing")
    versions = _require_exact_keys(config["versions"], VERSION_KEYS, "versions_invalid")
    for value in versions.values():
        _require_nonempty(value, "versions_invalid")
    if versions["snapshot_schema"] != SCHEMA_VERSION:
        raise EvidenceError("versions_incompatible")
    if not isinstance(config["images"], list) or not config["images"]:
        raise EvidenceError("images_invalid")
    for image in config["images"]:
        image = _require_exact_keys(
            image, ("component", "identity", "digest"), "images_invalid"
        )
        _require_nonempty(image["component"], "images_invalid")
        _require_nonempty(image["identity"], "images_invalid")
        if not isinstance(image["digest"], str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", image["digest"]
        ):
            raise EvidenceError("images_invalid")
    config["images"] = sorted(
        config["images"], key=lambda item: (item["component"], item["identity"])
    )
    return config


def _anomaly(
    identity: str,
    raw_identity: str,
    classification: str,
    reason_code: str,
    raw: bytes,
) -> dict[str, Any]:
    return {
        "identity": identity,
        "raw_identity": raw_identity,
        "classification": classification,
        "reason_code": reason_code,
        "raw_sha256": _sha256_bytes(raw),
    }


def _logical_record(
    identity: str,
    source_identity: str,
    record_kind: str,
    parsed: Any,
    raw: bytes,
    *,
    owner: str | None = None,
    actor: str | None = None,
    scope: str | None = None,
    category: str | None = None,
    destination: str | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    if isinstance(parsed, dict):
        actor = actor or parsed.get("actor") or parsed.get("created_by")
        owner = owner or parsed.get("owner") or actor
        scope = scope or parsed.get("scope")
        category = category or parsed.get("category")
        destination = destination or parsed.get("destination") or parsed.get("target")
        schema = schema or parsed.get("schema_version")
    fields = (owner, actor, scope, category, destination, schema)
    if any(value is not None and (not isinstance(value, str) or not value) for value in fields):
        owner = owner if isinstance(owner, str) and owner else None
        actor = actor if isinstance(actor, str) and actor else None
        scope = scope if isinstance(scope, str) and scope else None
        category = category if isinstance(category, str) and category else None
        destination = destination if isinstance(destination, str) and destination else None
        schema = schema if isinstance(schema, str) and schema else None
    return {
        "identity": identity,
        "source_identity": source_identity,
        "record_kind": record_kind,
        "owner": owner,
        "actor": actor,
        "scope": scope,
        "category": category,
        "destination": destination,
        "schema": schema,
        "canonical_sha256": _canonical_hash(parsed),
        "raw_sha256": _sha256_bytes(raw),
        "parse_status": "valid",
    }


def _try_json(data: bytes) -> tuple[Any | None, str | None]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "invalid_utf8"
    try:
        return json.loads(text, object_pairs_hook=_pairs_without_duplicates), None
    except DuplicateJSONKey:
        return None, "duplicate_json_key"
    except json.JSONDecodeError:
        return None, "malformed_json"


def _parse_file_logical(
    identity: str,
    classes: list[str],
    data: bytes,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], Any | None]:
    records: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    parsed_cache: Any | None = None
    if "history" in classes or identity.endswith(".jsonl"):
        lines = data.splitlines(keepends=True)
        for number, line in enumerate(lines, 1):
            body = line.rstrip(b"\r\n")
            parsed, error = _try_json(body)
            line_identity = f"{identity}:line:{number}"
            if error:
                classification = "malformed_jsonl" if error == "malformed_json" else error
                anomalies.append(
                    _anomaly(
                        f"anomaly:{_sha256_bytes(line_identity.encode())}",
                        identity,
                        classification,
                        error,
                        line,
                    )
                )
                continue
            records.append(
                _logical_record(
                    line_identity,
                    identity,
                    "history_record",
                    parsed,
                    line,
                    scope="PRIVATE" if isinstance(parsed, dict) and parsed.get("actor") else None,
                    category="history",
                    destination=(
                        f"history:{parsed.get('actor')}"
                        if isinstance(parsed, dict) and parsed.get("actor")
                        else "history:quarantine"
                    ),
                )
            )
            if not line.endswith((b"\n", b"\r")):
                anomalies.append(
                    _anomaly(
                        f"anomaly:{_sha256_bytes((line_identity + ':torn').encode())}",
                        identity,
                        "torn_jsonl_line",
                        "missing_line_terminator",
                        line,
                    )
                )
        return ("malformed" if anomalies else "valid", records, anomalies, None)

    if "cursors" in classes:
        try:
            cursor = int(data.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            anomalies.append(
                _anomaly(
                    f"anomaly:{_sha256_bytes(identity.encode())}",
                    identity,
                    "malformed_json",
                    "cursor_not_integer",
                    data,
                )
            )
            return "malformed", records, anomalies, None
        records.append(
            _logical_record(
                f"{identity}:cursor",
                identity,
                "cursor",
                cursor,
                data,
                category="cursor",
                destination=identity,
            )
        )
        return "valid", records, anomalies, cursor

    if not identity.endswith(".json"):
        return "not_applicable", records, anomalies, None
    parsed, error = _try_json(data)
    if error:
        anomalies.append(
            _anomaly(
                f"anomaly:{_sha256_bytes(identity.encode())}",
                identity,
                error,
                error,
                data,
            )
        )
        return "malformed", records, anomalies, None
    parsed_cache = parsed
    if "principals" in classes and isinstance(parsed, dict):
        principals = parsed.get("principals", [])
        if isinstance(principals, list):
            for index, principal in enumerate(principals):
                if not isinstance(principal, dict):
                    continue
                principal_id = principal.get("id")
                safe_id = principal_id if isinstance(principal_id, str) else f"index-{index}"
                records.append(
                    _logical_record(
                        f"{identity}:principal:{safe_id}",
                        identity,
                        "principal",
                        principal,
                        _canonical_bytes(principal),
                        owner=safe_id,
                        actor=safe_id,
                        scope="PRIVATE",
                        category="principal_registry",
                        destination="principal_registry",
                    )
                )
    elif "scheduler" in classes and isinstance(parsed, dict):
        jobs = parsed.get("jobs", [])
        if isinstance(jobs, list):
            for index, job in enumerate(jobs):
                if not isinstance(job, dict):
                    continue
                job_id = job.get("id")
                safe_id = job_id if isinstance(job_id, str) else f"index-{index}"
                records.append(
                    _logical_record(
                        f"{identity}:job:{safe_id}",
                        identity,
                        "scheduler_job",
                        job,
                        _canonical_bytes(job),
                        category="scheduler_job",
                        destination=(job.get("target") if isinstance(job.get("target"), str) else None),
                    )
                )
    else:
        records.append(
            _logical_record(
                f"{identity}:json",
                identity,
                "json_document",
                parsed,
                data,
                category="configuration",
                destination=identity,
            )
        )
    return "valid", records, anomalies, parsed_cache


class RedisClient:
    def __init__(
        self,
        host: str,
        port: int,
        database: int,
        password: str | None = None,
    ) -> None:
        try:
            self.socket = socket.create_connection((host, port), timeout=5)
            self.socket.settimeout(5)
            self.stream = self.socket.makefile("rb")
        except OSError as exc:
            raise EvidenceError("redis_unreachable") from exc
        try:
            if password is not None:
                self.command("AUTH", password)
            self.command("SELECT", database)
            pong = self.command("PING")
            if pong != b"PONG":
                raise EvidenceError("redis_identity_unproven")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            self.socket.close()

    def __enter__(self) -> "RedisClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def command(self, *arguments: str | int | bytes) -> Any:
        encoded = [
            item
            if isinstance(item, bytes)
            else str(item).encode("utf-8")
            for item in arguments
        ]
        request = [b"*" + str(len(encoded)).encode() + b"\r\n"]
        for item in encoded:
            request.extend(
                (b"$" + str(len(item)).encode() + b"\r\n", item, b"\r\n")
            )
        try:
            self.socket.sendall(b"".join(request))
            return self._read_response()
        except EvidenceError:
            raise
        except OSError as exc:
            raise EvidenceError("redis_protocol_failure") from exc

    def _read_line(self) -> bytes:
        line = self.stream.readline()
        if not line.endswith(b"\r\n"):
            raise EvidenceError("redis_protocol_failure")
        return line[:-2]

    def _read_response(self) -> Any:
        prefix = self.stream.read(1)
        if prefix == b"+":
            return self._read_line()
        if prefix == b"-":
            self._read_line()
            raise EvidenceError("redis_command_failed")
        if prefix == b":":
            return int(self._read_line())
        if prefix == b"$":
            length = int(self._read_line())
            if length == -1:
                return None
            data = self.stream.read(length)
            if len(data) != length or self.stream.read(2) != b"\r\n":
                raise EvidenceError("redis_protocol_failure")
            return data
        if prefix == b"*":
            count = int(self._read_line())
            if count == -1:
                return None
            return [self._read_response() for _ in range(count)]
        raise EvidenceError("redis_protocol_failure")


def _redis_password(config: dict[str, Any]) -> str | None:
    name = config["redis"]["password_env"]
    if name is None:
        return None
    password = os.environ.get(name)
    if password is None or not password:
        raise EvidenceError("redis_credential_missing")
    return password


def _scan_redis(client: RedisClient, marker_key: bytes | None) -> dict[bytes, dict[str, Any]]:
    cursor = b"0"
    keys: set[bytes] = set()
    while True:
        response = client.command("SCAN", cursor, "COUNT", 1000)
        if (
            not isinstance(response, list)
            or len(response) != 2
            or not isinstance(response[0], bytes)
            or not isinstance(response[1], list)
        ):
            raise EvidenceError("redis_scan_invalid")
        cursor = response[0]
        for key in response[1]:
            if not isinstance(key, bytes):
                raise EvidenceError("redis_scan_invalid")
            if marker_key is None or key != marker_key:
                keys.add(key)
        if cursor == b"0":
            break
    result: dict[bytes, dict[str, Any]] = {}
    for key in sorted(keys):
        raw_type = client.command("TYPE", key)
        pttl = client.command("PTTL", key)
        dump = client.command("DUMP", key)
        if not isinstance(raw_type, bytes) or not isinstance(pttl, int) or not isinstance(dump, bytes):
            raise EvidenceError("redis_record_incomplete")
        value = client.command("GET", key) if raw_type == b"string" else None
        if raw_type == b"string" and not isinstance(value, bytes):
            raise EvidenceError("redis_record_incomplete")
        if pttl < -1:
            raise EvidenceError("redis_source_changed")
        result[key] = {"type": raw_type, "pttl": pttl, "dump": dump, "value": value}
    return result


def _redis_states_consistent(
    first: dict[bytes, dict[str, Any]], second: dict[bytes, dict[str, Any]]
) -> bool:
    if set(first) != set(second):
        return False
    for key in first:
        left = first[key]
        right = second[key]
        if (left["type"], left["dump"], left["value"]) != (
            right["type"],
            right["dump"],
            right["value"],
        ):
            return False
        if (left["pttl"] == -1) != (right["pttl"] == -1):
            return False
    return True


def _redis_identity_fields(key: bytes) -> tuple[str | None, str | None, str | None, str | None]:
    try:
        decoded = key.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, None, None
    if decoded.startswith("memx:value:"):
        logical = decoded[len("memx:value:") :]
        parts = logical.split(":")
        scope = parts[0].upper() if parts else None
        owner = parts[1] if len(parts) > 1 and parts[0] in {"private", "pair", "shared"} else None
        category = "discovery_index" if logical.endswith("_index") else "memx_value"
        return logical, scope, owner, category
    if decoded.startswith("memx:schema:"):
        logical = decoded[len("memx:schema:") :]
        parts = logical.split(":")
        scope = parts[0].upper() if parts else None
        owner = parts[1] if len(parts) > 1 and parts[0] in {"private", "pair", "shared"} else None
        return logical, scope, owner, "schema"
    return None, None, None, None


def _capture_redis(
    config: dict[str, Any],
    stage: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    redis_config = config["redis"]
    marker_key = (
        redis_config["isolation_marker_key"].encode("utf-8")
        if redis_config["isolation_marker_key"] is not None
        else None
    )
    with RedisClient(
        redis_config["host"],
        redis_config["port"],
        redis_config["database"],
        _redis_password(config),
    ) as client:
        if marker_key is not None:
            marker_value = client.command("GET", marker_key)
            if not isinstance(marker_value, bytes) or _sha256_bytes(marker_value) != redis_config[
                "isolation_marker_sha256"
            ]:
                raise EvidenceError("fixture_isolation_proof_invalid")
        first = _scan_redis(client, marker_key)
        second = _scan_redis(client, marker_key)
    if not _redis_states_consistent(first, second):
        raise EvidenceError("redis_source_changed")

    entries: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    for key, raw in first.items():
        key_hash = _sha256_bytes(key)
        identity = f"redis:{key_hash}"
        base = f"payload/redis/{key_hash}"
        key_path = f"{base}.key"
        dump_path = f"{base}.dump"
        _write_private(stage / key_path, key)
        _write_private(stage / dump_path, raw["dump"])
        raw_type = raw["type"].decode("ascii", errors="strict")
        value_path: str | None = None
        value_length: int | None = None
        value_hash: str | None = None
        logical_key, scope, owner, category = _redis_identity_fields(key)
        parse_status = "opaque"
        if raw_type == "string":
            value = raw["value"]
            value_path = f"{base}.value"
            _write_private(stage / value_path, value)
            value_length = len(value)
            value_hash = _sha256_bytes(value)
            if logical_key is not None:
                parsed, error = _try_json(value)
                if error:
                    classification = "invalid_utf8" if error == "invalid_utf8" else "malformed_redis_value"
                    anomalies.append(
                        _anomaly(
                            f"anomaly:{_sha256_bytes((identity + ':value').encode())}",
                            identity,
                            classification,
                            error,
                            value,
                        )
                    )
                    parse_status = "malformed"
                else:
                    records.append(
                        _logical_record(
                            f"{identity}:logical",
                            identity,
                            "redis_logical_record",
                            parsed,
                            value,
                            owner=owner,
                            scope=scope,
                            category=category,
                            destination=logical_key,
                            schema=config["versions"]["memx"],
                        )
                    )
                    parse_status = "valid"
        else:
            anomalies.append(
                _anomaly(
                    f"anomaly:{_sha256_bytes((identity + ':type').encode())}",
                    identity,
                    "unsupported_redis_type",
                    "raw_preserved_logical_parser_unavailable",
                    raw["dump"],
                )
            )
        entries.append(
            {
                "identity": identity,
                "object_kind": "redis_string" if raw_type == "string" else "redis_opaque",
                "raw_key_sha256": key_hash,
                "key_payload_path": key_path,
                "key_byte_length": len(key),
                "key_sha256": key_hash,
                "raw_type": raw_type,
                "ttl_kind": "persistent" if raw["pttl"] == -1 else "volatile",
                "pttl_ms": raw["pttl"],
                "dump_payload_path": dump_path,
                "dump_byte_length": len(raw["dump"]),
                "dump_sha256": _sha256_bytes(raw["dump"]),
                "value_payload_path": value_path,
                "value_byte_length": value_length,
                "value_sha256": value_hash,
                "logical_key": logical_key,
                "scope": scope,
                "owner": owner,
                "category": category,
                "schema": config["versions"]["memx"] if logical_key is not None else None,
                "parse_status": parse_status,
            }
        )
    return entries, records, anomalies


def _capture_files(
    config: dict[str, Any], stage: Path
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, tuple[int, int, int, int, str]],
]:
    root = Path(config["source"]["root"]).absolute()
    _assert_no_symlink_chain(root, require_leaf=True)
    if not root.is_dir():
        raise EvidenceError("source_root_invalid")
    class_by_path: dict[str, list[str]] = {}
    for file_class, paths in config["required_file_classes"].items():
        for identity in paths:
            class_by_path.setdefault(identity, []).append(file_class)
            required = root.joinpath(*PurePosixPath(identity).parts)
            _assert_no_symlink_chain(required, require_leaf=True)
            if not required.is_file():
                raise EvidenceError("required_source_missing")
    for identity in config["non_secret_config_paths"]:
        if identity not in class_by_path:
            raise EvidenceError("non_secret_config_not_in_inventory")

    paths: list[Path] = []
    for candidate in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError("source_symlink_refused")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise EvidenceError("unsupported_source_object")
        paths.append(candidate)
    if not paths:
        raise EvidenceError("source_inventory_empty")

    entries: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    parsed_documents: dict[str, Any] = {}
    source_state: dict[str, tuple[int, int, int, int, str]] = {}
    for path in paths:
        identity = path.relative_to(root).as_posix()
        _validate_relative_path(identity, "unsupported_source_identity")
        classes = sorted(class_by_path.get(identity, ["nanobot_files"]))
        data, info = _safe_read_file(path)
        digest = _sha256_bytes(data)
        source_state[identity] = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            digest,
        )
        payload_path = f"payload/files/{_sha256_bytes(identity.encode())}"
        _write_private(stage / payload_path, data)
        parse_status, file_records, file_anomalies, parsed = _parse_file_logical(
            identity, classes, data
        )
        if parsed is not None:
            parsed_documents[identity] = parsed
        records.extend(file_records)
        anomalies.extend(file_anomalies)
        entries.append(
            {
                "identity": identity,
                "classes": classes,
                "object_kind": "file",
                "byte_length": len(data),
                "sha256": digest,
                "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                "payload_path": payload_path,
                "parse_status": parse_status,
                "logical_record_count": len(file_records),
            }
        )
    return entries, records, anomalies, parsed_documents, source_state


def _verify_source_unchanged(
    root: Path, source_state: dict[str, tuple[int, int, int, int, str]]
) -> None:
    current_identities = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if current_identities != set(source_state):
        raise EvidenceError("source_changed_during_capture")
    for identity, expected in source_state.items():
        data, info = _safe_read_file(root.joinpath(*PurePosixPath(identity).parts))
        actual = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, _sha256_bytes(data))
        if actual != expected:
            raise EvidenceError("source_changed_during_capture")


def _access_matrix(parsed_documents: dict[str, Any]) -> list[dict[str, Any]]:
    principal_keys: dict[str, str] = {}
    for identity, document in parsed_documents.items():
        if identity.endswith("principals.json") and isinstance(document, dict):
            for principal in document.get("principals", []):
                if isinstance(principal, dict) and isinstance(principal.get("id"), str) and isinstance(
                    principal.get("memx_key"), str
                ):
                    principal_keys[principal["memx_key"]] = principal["id"]
    rows: list[dict[str, Any]] = []
    for identity, document in parsed_documents.items():
        if identity.endswith("acl.json") and isinstance(document, dict):
            for credential, patterns in document.items():
                if credential.startswith("_") or not isinstance(patterns, list):
                    continue
                actor = principal_keys.get(credential, f"unmapped-key-{_sha256_bytes(credential.encode())[:12]}")
                for pattern in patterns:
                    if not isinstance(pattern, str) or not pattern:
                        continue
                    core = {
                        "actor": actor,
                        "resource_pattern": pattern,
                        "actions": ["read", "write"],
                        "decision": "allow",
                        "source_identity": identity,
                    }
                    rows.append(
                        {
                            "identity": f"access:{_canonical_hash(core)}",
                            **core,
                            "canonical_sha256": _canonical_hash(core),
                        }
                    )
        if identity.endswith("policy.json") and isinstance(document, dict):
            for rule in document.get("rules", []):
                if not isinstance(rule, dict):
                    continue
                actors = rule.get("actor")
                resources = rule.get("resource", rule.get("to_chat"))
                actions = rule.get("action")
                actors = actors if isinstance(actors, list) else [actors]
                resources = resources if isinstance(resources, list) else [resources]
                actions = actions if isinstance(actions, list) else [actions]
                normalized_actions = sorted(
                    {
                        suffix
                        for action in actions
                        if isinstance(action, str)
                        for suffix in ("read", "write")
                        if suffix in action
                    }
                )
                if not normalized_actions:
                    continue
                for actor in actors:
                    for resource in resources:
                        if not isinstance(actor, str) or not actor or not isinstance(resource, str) or not resource:
                            continue
                        core = {
                            "actor": actor,
                            "resource_pattern": resource,
                            "actions": normalized_actions,
                            "decision": "allow" if rule.get("decision") == "allow" else "deny",
                            "source_identity": identity,
                        }
                        rows.append(
                            {
                                "identity": f"access:{_canonical_hash(core)}",
                                **core,
                                "canonical_sha256": _canonical_hash(core),
                            }
                        )
    deduplicated = {row["identity"]: row for row in rows}
    return sorted(deduplicated.values(), key=lambda row: row["identity"])


def _git_provenance() -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1", "--untracked-files=all"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            ).stdout
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError("repository_identity_unavailable") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise EvidenceError("repository_identity_invalid")
    return commit, dirty


def _finalize_manifest(
    config: dict[str, Any],
    started_at: str,
    ended_at: str,
    tool_path: Path,
    flat_files: list[dict[str, Any]],
    redis_entries: list[dict[str, Any]],
    records: list[dict[str, Any]],
    access: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
) -> dict[str, Any]:
    flat_files.sort(key=lambda entry: entry["identity"])
    redis_entries.sort(key=lambda entry: entry["identity"])
    records.sort(key=lambda entry: entry["identity"])
    access.sort(key=lambda entry: entry["identity"])
    anomalies.sort(key=lambda entry: entry["identity"])
    logical = {
        "records": records,
        "access_matrix": access,
        "anomalies": anomalies,
        "record_count": len(records),
        "access_count": len(access),
        "anomaly_count": len(anomalies),
        "aggregate_sha256": "0" * 64,
    }
    logical["aggregate_sha256"] = _canonical_hash(_logical_core(logical))
    inventory = {
        "class_counts": {key: 0 for key in CLASS_COUNT_KEYS},
        "flat_files": flat_files,
        "redis": redis_entries,
        "logical": logical,
        "byte_aggregate_sha256": _canonical_hash(
            {"flat_files": flat_files, "redis": redis_entries}
        ),
    }
    commit, dirty = _git_provenance()
    config_hashes = []
    flat_by_identity = {entry["identity"]: entry for entry in flat_files}
    for identity in sorted(config["non_secret_config_paths"]):
        entry = flat_by_identity.get(identity)
        if entry is None:
            raise EvidenceError("effective_config_missing")
        config_hashes.append({"identity": identity, "sha256": entry["sha256"]})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_format_version": FORMAT_VERSION,
        "snapshot_id": "0" * 64,
        "status": "complete",
        "state_role": "source",
        "capture": {
            "mode": "read_only",
            "started_at": started_at,
            "ended_at": ended_at,
            "writer_stop_started_at": config["authorization"]["writer_stop_started_at"],
            "writer_stop_ended_at": config["authorization"]["writer_stop_ended_at"],
            "authorization_id": config["authorization"]["authorization_id"],
            "source_kind": config["authorization"]["source_kind"],
            "source": {
                "host_id": config["source"]["host_id"],
                "deployment_id": config["source"]["deployment_id"],
                "logical_paths": sorted(config["source"]["logical_paths"]),
                "redis_endpoint": config["redis"]["logical_endpoint"],
            },
        },
        "provenance": {
            "hash_algorithm": "sha256",
            "capture_tool_sha256": _sha256_file(tool_path),
            "repository_commit": commit,
            "repository_dirty": dirty,
            "effective_config_hashes": config_hashes,
        },
        "versions": config["versions"],
        "images": config["images"],
        "inventory": inventory,
        "content_hashes": {
            "algorithm": "sha256",
            "byte_state_sha256": inventory["byte_aggregate_sha256"],
            "logical_state_sha256": logical["aggregate_sha256"],
        },
    }
    inventory["class_counts"] = _computed_class_counts(manifest)
    manifest["snapshot_id"] = _canonical_hash(
        {
            "snapshot_format_version": manifest["snapshot_format_version"],
            "versions": manifest["versions"],
            "images": manifest["images"],
            "content_hashes": manifest["content_hashes"],
        }
    )
    return manifest


def capture(config_path: Path, output: Path, tool_path: Path) -> None:
    started_at = _utc_now()
    config = _config(config_path)
    source_root = Path(config["source"]["root"]).absolute()
    output = output.absolute()
    if _is_within(output, source_root) or _is_within(source_root, output):
        raise EvidenceError("snapshot_output_aliases_source")
    if output.parent.exists():
        _assert_no_symlink_chain(output.parent, require_leaf=True)
    else:
        raise EvidenceError("snapshot_output_parent_missing")
    if output.is_symlink():
        raise EvidenceError("snapshot_output_symlink")
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.capture-", dir=output.parent))
    os.chmod(stage, 0o700)
    try:
        flat, file_records, file_anomalies, parsed, source_state = _capture_files(
            config, stage
        )
        redis_entries, redis_records, redis_anomalies = _capture_redis(config, stage)
        _verify_source_unchanged(source_root, source_state)
        manifest = _finalize_manifest(
            config,
            started_at,
            _utc_now(),
            tool_path,
            flat,
            redis_entries,
            file_records + redis_records,
            _access_matrix(parsed),
            file_anomalies + redis_anomalies,
        )
        _write_private(stage / "manifest.json", _canonical_bytes(manifest) + b"\n")
        load_and_validate_manifest(stage / "manifest.json")
        if output.exists():
            if not output.is_dir():
                raise EvidenceError("snapshot_output_exists_invalid")
            existing = load_and_validate_manifest(output / "manifest.json")
            if (
                existing["snapshot_id"] != manifest["snapshot_id"]
                or existing["content_hashes"] != manifest["content_hashes"]
            ):
                raise EvidenceError("snapshot_output_diverged")
            shutil.rmtree(stage)
            print(
                f"snapshot=unchanged snapshot_id={manifest['snapshot_id']} "
                f"files={len(flat)} redis={len(redis_entries)}"
            )
            return
        os.replace(stage, output)
        os.chmod(output, 0o700)
        print(
            f"snapshot=complete snapshot_id={manifest['snapshot_id']} "
            f"files={len(flat)} redis={len(redis_entries)} anomalies={len(file_anomalies) + len(redis_anomalies)}"
        )
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _load_restore_marker(target: Path) -> dict[str, Any]:
    marker_path = target / ".familia-memory-restore-target.json"
    marker = _load_json_file(marker_path, "restore_marker_invalid")
    marker = _require_exact_keys(
        marker,
        (
            "marker_version",
            "purpose",
            "target_id",
            "non_production",
            "filesystem_root",
            "redis",
            "compatibility",
        ),
        "restore_marker_invalid",
    )
    if marker["marker_version"] != "1.0.0" or marker["purpose"] != "familia-memory-restore":
        raise EvidenceError("restore_marker_invalid")
    _require_nonempty(marker["target_id"], "restore_marker_invalid")
    if marker["non_production"] is not True:
        raise EvidenceError("production_like_target_refused")
    if marker["filesystem_root"] != str(target.resolve(strict=True)):
        raise EvidenceError("restore_marker_target_mismatch")
    marker_info = marker_path.stat()
    if stat.S_IMODE(marker_info.st_mode) & 0o077:
        raise EvidenceError("restore_marker_permissions_unsafe")
    redis = _require_exact_keys(
        marker["redis"],
        (
            "host",
            "port",
            "database",
            "logical_endpoint",
            "isolation_marker_key",
            "isolation_marker_sha256",
        ),
        "restore_marker_invalid",
    )
    _require_nonempty(redis["host"], "restore_marker_invalid")
    _require_nonempty(redis["logical_endpoint"], "restore_marker_invalid")
    _require_nonempty(redis["isolation_marker_key"], "restore_marker_invalid")
    if not isinstance(redis["port"], int) or not 1 <= redis["port"] <= 65535:
        raise EvidenceError("restore_marker_invalid")
    if not isinstance(redis["database"], int) or not 0 <= redis["database"] <= 65535:
        raise EvidenceError("restore_marker_invalid")
    if not isinstance(redis["isolation_marker_sha256"], str) or not SHA256_RE.fullmatch(
        redis["isolation_marker_sha256"]
    ):
        raise EvidenceError("restore_marker_invalid")
    compatibility = _require_exact_keys(
        marker["compatibility"], VERSION_KEYS, "restore_compatibility_invalid"
    )
    for value in compatibility.values():
        _require_nonempty(value, "restore_compatibility_invalid")
    return marker


def _assert_safe_restore_target(target: Path, snapshot_root: Path) -> None:
    if not target.is_absolute():
        raise EvidenceError("restore_target_must_be_absolute")
    _assert_no_symlink_chain(target, require_leaf=True)
    if not target.is_dir():
        raise EvidenceError("restore_target_not_directory")
    resolved = target.resolve(strict=True)
    lowered_parts = {part.lower() for part in resolved.parts}
    if ".nanobot" in lowered_parts or "evidence" in lowered_parts:
        raise EvidenceError("protected_target_path_refused")
    if resolved.parts[:2] == ("/", "mnt"):
        raise EvidenceError("mounted_or_shared_target_refused")
    home = Path.home().resolve(strict=False)
    if _is_within(resolved, home):
        raise EvidenceError("home_target_refused")
    for protected in (REPO_ROOT, snapshot_root):
        if _is_within(resolved, protected) or _is_within(protected, resolved):
            raise EvidenceError("source_or_repository_target_refused")
    info = resolved.stat()
    if info.st_uid != os.getuid():
        raise EvidenceError("restore_target_owner_invalid")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise EvidenceError("restore_target_permissions_unsafe")


def _loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _target_redis_client(marker: dict[str, Any]) -> RedisClient:
    redis = marker["redis"]
    if not _loopback_host(redis["host"]):
        raise EvidenceError("redis_target_not_loopback")
    token = os.environ.get("MEMORY_RESTORE_REDIS_ISOLATION_TOKEN")
    if token is None or not token:
        raise EvidenceError("redis_target_token_missing")
    if _sha256_bytes(token.encode("utf-8")) != redis["isolation_marker_sha256"]:
        raise EvidenceError("redis_target_token_mismatch")
    client = RedisClient(redis["host"], redis["port"], redis["database"])
    marker_value = client.command("GET", redis["isolation_marker_key"].encode("utf-8"))
    if not isinstance(marker_value, bytes) or _sha256_bytes(marker_value) != redis[
        "isolation_marker_sha256"
    ]:
        client.close()
        raise EvidenceError("redis_target_isolation_unproven")
    return client


def _expected_redis(
    manifest: dict[str, Any], snapshot_root: Path
) -> dict[bytes, dict[str, Any]]:
    expected: dict[bytes, dict[str, Any]] = {}
    for entry in manifest["inventory"]["redis"]:
        key = _resolve_payload(snapshot_root, entry["key_payload_path"]).read_bytes()
        if _sha256_bytes(key) != entry["raw_key_sha256"]:
            raise EvidenceError("redis_key_identity_mismatch")
        if key in expected:
            raise EvidenceError("duplicate_redis_raw_key")
        dump = _resolve_payload(snapshot_root, entry["dump_payload_path"]).read_bytes()
        value = (
            _resolve_payload(snapshot_root, entry["value_payload_path"]).read_bytes()
            if entry["value_payload_path"] is not None
            else None
        )
        expected[key] = {
            "type": entry["raw_type"].encode("ascii"),
            "pttl": entry["pttl_ms"],
            "dump": dump,
            "value": value,
        }
    return expected


def _redis_equal(
    current: dict[bytes, dict[str, Any]], expected: dict[bytes, dict[str, Any]]
) -> bool:
    if set(current) != set(expected):
        return False
    for key, wanted in expected.items():
        actual = current[key]
        if (actual["type"], actual["dump"], actual["value"]) != (
            wanted["type"],
            wanted["dump"],
            wanted["value"],
        ):
            return False
        if (actual["pttl"] == -1) != (wanted["pttl"] == -1):
            return False
    return True


def _declared_payload_paths(manifest: dict[str, Any]) -> set[str]:
    result = {entry["payload_path"] for entry in manifest["inventory"]["flat_files"]}
    for entry in manifest["inventory"]["redis"]:
        result.add(entry["key_payload_path"])
        result.add(entry["dump_payload_path"])
        if entry["value_payload_path"] is not None:
            result.add(entry["value_payload_path"])
    return result


def _verify_restored_files(target: Path, manifest: dict[str, Any]) -> None:
    state_root = target / "state" / "files"
    _assert_no_symlink_chain(state_root, require_leaf=True)
    expected_identities = {entry["identity"] for entry in manifest["inventory"]["flat_files"]}
    actual_identities: set[str] = set()
    for path in state_root.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError("restored_file_symlink")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise EvidenceError("restored_object_unsupported")
        actual_identities.add(path.relative_to(state_root).as_posix())
    if actual_identities != expected_identities:
        raise EvidenceError("restored_file_inventory_mismatch")
    for entry in manifest["inventory"]["flat_files"]:
        path = state_root.joinpath(*PurePosixPath(entry["identity"]).parts)
        _assert_no_symlink_chain(path, require_leaf=True)
        if path.stat().st_size != entry["byte_length"] or _sha256_file(path) != entry["sha256"]:
            raise EvidenceError("restored_file_mismatch")
        if f"{stat.S_IMODE(path.stat().st_mode):04o}" != entry["mode"]:
            raise EvidenceError("restored_file_mode_mismatch")


def _verify_no_extra_payloads(target: Path, manifest: dict[str, Any]) -> None:
    payload_root = target / "payload"
    _assert_no_symlink_chain(payload_root, require_leaf=True)
    actual: set[str] = set()
    for path in payload_root.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError("restored_payload_symlink")
        if stat.S_ISREG(info.st_mode):
            actual.add(path.relative_to(target).as_posix())
        elif not stat.S_ISDIR(info.st_mode):
            raise EvidenceError("restored_payload_unsupported")
    if actual != _declared_payload_paths(manifest):
        raise EvidenceError("restored_payload_inventory_mismatch")


def _existing_restore_is_equal(
    target: Path,
    source_manifest: dict[str, Any],
    client: RedisClient,
    marker_key: bytes,
    expected_redis: dict[bytes, dict[str, Any]],
) -> bool:
    expected_root_names = {
        ".familia-memory-restore-target.json",
        "manifest.json",
        "payload",
        "state",
    }
    if {path.name for path in target.iterdir()} != expected_root_names:
        return False
    try:
        restored_manifest = load_and_validate_manifest(target / "manifest.json")
        if restored_manifest["state_role"] != "restored":
            return False
        for key in ("versions", "images", "inventory", "content_hashes", "snapshot_id"):
            if restored_manifest[key] != source_manifest[key]:
                return False
        _verify_no_extra_payloads(target, restored_manifest)
        _verify_restored_files(target, restored_manifest)
        current_redis = _scan_redis(client, marker_key)
        return _redis_equal(current_redis, expected_redis)
    except (EvidenceError, OSError):
        return False


def _stage_restore_files(
    stage: Path,
    snapshot_root: Path,
    manifest: dict[str, Any],
) -> None:
    for relative in sorted(_declared_payload_paths(manifest)):
        source = _resolve_payload(snapshot_root, relative)
        _write_private(stage.joinpath(*PurePosixPath(relative).parts), source.read_bytes())
    for entry in manifest["inventory"]["flat_files"]:
        source = _resolve_payload(snapshot_root, entry["payload_path"])
        destination = stage / "state" / "files" / PurePosixPath(entry["identity"])
        _write_private(destination, source.read_bytes())
        os.chmod(destination, int(entry["mode"], 8))
    restored_manifest = json.loads(json.dumps(manifest))
    restored_manifest["state_role"] = "restored"
    _write_private(stage / "manifest.json", _canonical_bytes(restored_manifest) + b"\n")


def _restore_redis_entries_transactional(
    client: RedisClient,
    expected_redis: dict[bytes, dict[str, Any]],
    marker_key: bytes,
) -> list[bytes]:
    """Restore into a proven-empty Redis target or roll every inserted key back."""

    restored_keys: list[bytes] = []
    try:
        for key, entry in sorted(expected_redis.items(), key=lambda item: item[0]):
            ttl = 0 if entry["pttl"] == -1 else max(1, entry["pttl"])
            response = client.command("RESTORE", key, ttl, entry["dump"])
            if response != b"OK":
                raise EvidenceError("redis_restore_failed")
            restored_keys.append(key)
        if not _redis_equal(_scan_redis(client, marker_key), expected_redis):
            raise EvidenceError("redis_restore_verification_failed")
        return restored_keys
    except Exception:
        for key in reversed(restored_keys):
            try:
                client.command("DEL", key)
            except Exception as rollback_error:
                raise EvidenceError("redis_restore_rollback_failed") from rollback_error
        if _scan_redis(client, marker_key):
            raise EvidenceError("redis_restore_rollback_failed")
        raise


def _rollback_restore_attempt(
    *,
    target: Path,
    published_paths: list[Path],
    client: RedisClient,
    restored_keys: list[bytes],
    marker_key: bytes,
) -> None:
    """Return the initially-empty isolated target to its marker-only state."""

    for path in reversed(published_paths):
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            raise EvidenceError("restore_rollback_unsafe_path")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    for key in reversed(restored_keys):
        try:
            client.command("DEL", key)
        except Exception as rollback_error:
            raise EvidenceError("restore_rollback_failed") from rollback_error
    if _scan_redis(client, marker_key):
        raise EvidenceError("restore_rollback_failed")
    if {path.name for path in target.iterdir()} != {
        ".familia-memory-restore-target.json"
    }:
        raise EvidenceError("restore_rollback_failed")


def restore(snapshot_root: Path, target: Path, _tool_path: Path) -> None:
    snapshot_root = snapshot_root.absolute()
    _assert_no_symlink_chain(snapshot_root, require_leaf=True)
    if not snapshot_root.is_dir():
        raise EvidenceError("snapshot_root_invalid")
    source_manifest = load_and_validate_manifest(snapshot_root / "manifest.json")
    if source_manifest["state_role"] != "source":
        raise EvidenceError("restore_requires_source_snapshot")
    target = target.absolute()
    _assert_safe_restore_target(target, snapshot_root)
    marker = _load_restore_marker(target)
    if marker["compatibility"] != source_manifest["versions"]:
        raise EvidenceError("restore_component_incompatible")

    root_names = {path.name for path in target.iterdir()}
    marker_name = ".familia-memory-restore-target.json"
    if marker_name not in root_names:
        raise EvidenceError("restore_marker_missing")
    if root_names != {marker_name} and root_names != {
        marker_name,
        "manifest.json",
        "payload",
        "state",
    }:
        raise EvidenceError("unexpected_target_data")

    expected_redis = _expected_redis(source_manifest, snapshot_root)
    marker_key = marker["redis"]["isolation_marker_key"].encode("utf-8")
    client = _target_redis_client(marker)
    try:
        current_redis = _scan_redis(client, marker_key)
        filesystem_empty = root_names == {marker_name}
        redis_empty = not current_redis
        if not filesystem_empty or not redis_empty:
            if _existing_restore_is_equal(
                target, source_manifest, client, marker_key, expected_redis
            ):
                print(
                    f"restore=unchanged snapshot_id={source_manifest['snapshot_id']} "
                    f"files={len(source_manifest['inventory']['flat_files'])} "
                    f"redis={len(expected_redis)}"
                )
                return
            raise EvidenceError("divergent_or_partial_target")

        # Every source, compatibility, filesystem, marker, target-state and
        # Redis isolation check above is read-only.  Staging begins only now.
        stage = Path(tempfile.mkdtemp(prefix=".restore-stage-", dir=target))
        os.chmod(stage, 0o700)
        try:
            _stage_restore_files(stage, snapshot_root, source_manifest)
            staged_manifest = load_and_validate_manifest(stage / "manifest.json")
            _verify_restored_files(stage, staged_manifest)
            _verify_no_extra_payloads(stage, staged_manifest)
            restored_keys: list[bytes] = []
            published_paths: list[Path] = []
            try:
                restored_keys = _restore_redis_entries_transactional(
                    client, expected_redis, marker_key
                )
                for name in ("payload", "state", "manifest.json"):
                    destination = target / name
                    os.replace(stage / name, destination)
                    published_paths.append(destination)
                load_and_validate_manifest(target / "manifest.json")
                _verify_restored_files(target, staged_manifest)
                _verify_no_extra_payloads(target, staged_manifest)
                print(
                    f"restore=complete snapshot_id={source_manifest['snapshot_id']} "
                    f"files={len(source_manifest['inventory']['flat_files'])} "
                    f"redis={len(expected_redis)}"
                )
            except Exception:
                _rollback_restore_attempt(
                    target=target,
                    published_paths=published_paths,
                    client=client,
                    restored_keys=restored_keys,
                    marker_key=marker_key,
                )
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    finally:
        client.close()


def compare(left_path: Path, right_path: Path) -> int:
    try:
        left = load_and_validate_manifest(left_path)
        right = load_and_validate_manifest(right_path)
    except EvidenceError as exc:
        print(f"comparison=invalid reason={exc.code}", file=sys.stderr)
        return 2
    left_inventory = left["inventory"]
    right_inventory = right["inventory"]
    byte_equal = (
        left_inventory["flat_files"] == right_inventory["flat_files"]
        and left_inventory["redis"] == right_inventory["redis"]
        and left["content_hashes"]["byte_state_sha256"]
        == right["content_hashes"]["byte_state_sha256"]
    )
    logical_equal = (
        _logical_core(left_inventory["logical"])
        == _logical_core(right_inventory["logical"])
        and left["content_hashes"]["logical_state_sha256"]
        == right["content_hashes"]["logical_state_sha256"]
        and left_inventory["class_counts"] == right_inventory["class_counts"]
    )
    byte_state = "equal" if byte_equal else "unequal"
    logical_state = "equal" if logical_equal else "unequal"
    if byte_equal and logical_equal:
        print(f"comparison=equal byte_state={byte_state} logical_state={logical_state}")
        return 0
    print(
        f"comparison=unequal byte_state={byte_state} logical_state={logical_state} "
        f"left_files={len(left_inventory['flat_files'])} "
        f"right_files={len(right_inventory['flat_files'])} "
        f"left_redis={len(left_inventory['redis'])} "
        f"right_redis={len(right_inventory['redis'])} "
        f"left_logical={left_inventory['logical']['record_count']} "
        f"right_logical={right_inventory['logical']['record_count']}"
    )
    return 1


def _capture_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="memory-snapshot internal capture")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tool-path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        capture(args.config, args.output, args.tool_path)
        return 0
    except EvidenceError as exc:
        print(f"snapshot=invalid reason={exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("snapshot=invalid reason=unexpected_internal_failure", file=sys.stderr)
        return 2


def _restore_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="memory-snapshot internal restore")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--tool-path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        restore(args.input, args.target, args.tool_path)
        return 0
    except EvidenceError as exc:
        print(f"restore=refused reason={exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("restore=refused reason=unexpected_internal_failure", file=sys.stderr)
        return 2


def _compare_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Compare complete RP-010 byte and logical memory manifests."
    )
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--require-byte-and-logical-equality", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if not args.require_byte_and_logical_equality:
        print("comparison=invalid reason=equality_mode_required", file=sys.stderr)
        return 2
    return compare(args.left, args.right)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "_capture":
        return _capture_cli(arguments[1:])
    if arguments and arguments[0] == "_restore":
        return _restore_cli(arguments[1:])
    return _compare_cli(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
