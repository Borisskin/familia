"""RP-010 snapshot/restore/compare behavior on disposable fixtures only.

The suite deliberately never discovers or opens a real ``~/.nanobot`` tree.
Both the filesystem roots and the small Redis-compatible server are created
under pytest's disposable temporary directory/process.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pytest
from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_SCRIPT = REPO_ROOT / "bin" / "memory-snapshot.sh"
RESTORE_SCRIPT = REPO_ROOT / "bin" / "restore-memory-snapshot.sh"
COMPARE_SCRIPT = REPO_ROOT / "scripts" / "compare_memory_state.py"
SCHEMA_PATH = REPO_ROOT / "release" / "memory-snapshot.schema.json"
SECRET_SENTINEL = "RP010_SECRET_SENTINEL_9f3c18e1"
FIXED_VERSIONS = {
    "familia": "0.4.0",
    "nanobot": "0.1.5.post2",
    "memx": "fixture-1",
    "snapshot_schema": "1.0.0",
    "acl": "fixture-acl-1",
    "policy": "fixture-policy-1",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _safe_process_details(process: subprocess.CompletedProcess[str]) -> str:
    combined = f"exit={process.returncode}\nstdout={process.stdout}\nstderr={process.stderr}"
    return combined.replace(SECRET_SENTINEL, "<redacted-sentinel>")


def _assert_success(process: subprocess.CompletedProcess[str]) -> None:
    assert process.returncode == 0, _safe_process_details(process)


def _run(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    effective_env = os.environ.copy()
    if env:
        effective_env.update(env)
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env=effective_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _snapshot(config: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        "bash",
        str(SNAPSHOT_SCRIPT),
        "--read-only",
        "--config",
        str(config),
        "--output",
        str(output),
    )


def _restore(
    snapshot: Path,
    target: Path,
    token: str,
) -> subprocess.CompletedProcess[str]:
    return _run(
        "bash",
        str(RESTORE_SCRIPT),
        "--input",
        str(snapshot),
        "--target",
        str(target),
        env={"MEMORY_RESTORE_REDIS_ISOLATION_TOKEN": token},
    )


def _compare(left: Path, right: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        sys.executable,
        str(COMPARE_SCRIPT),
        str(left),
        str(right),
        "--require-byte-and-logical-equality",
    )


@dataclass
class _RedisEntry:
    value: bytes
    dump: bytes
    redis_type: bytes = b"string"
    pttl_ms: int = -1


@dataclass
class _RedisState:
    databases: dict[int, dict[bytes, _RedisEntry]] = field(default_factory=dict)
    commands: list[tuple[bytes, ...]] = field(default_factory=list)
    write_commands: list[tuple[bytes, ...]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def put(self, database: int, key: bytes, value: bytes) -> None:
        dump = b"fixture-dump-v1\x00" + value
        with self.lock:
            self.databases.setdefault(database, {})[key] = _RedisEntry(value=value, dump=dump)

    def snapshot(self, database: int) -> dict[bytes, tuple[bytes, bytes, bytes, int]]:
        with self.lock:
            return {
                key: (entry.value, entry.dump, entry.redis_type, entry.pttl_ms)
                for key, entry in self.databases.get(database, {}).items()
            }


def _read_resp_request(stream) -> list[bytes] | None:
    first = stream.readline()
    if not first:
        return None
    if not first.startswith(b"*"):
        raise ValueError("fixture accepts RESP arrays only")
    count = int(first[1:-2])
    result: list[bytes] = []
    for _ in range(count):
        length_line = stream.readline()
        if not length_line.startswith(b"$"):
            raise ValueError("fixture accepts bulk request arguments only")
        length = int(length_line[1:-2])
        value = stream.read(length)
        if stream.read(2) != b"\r\n":
            raise ValueError("invalid RESP terminator")
        result.append(value)
    return result


def _write_resp(stream, value: Any) -> None:
    if value is None:
        stream.write(b"$-1\r\n")
    elif isinstance(value, bytes):
        stream.write(b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n")
    elif isinstance(value, int):
        stream.write(b":" + str(value).encode() + b"\r\n")
    elif isinstance(value, list):
        stream.write(b"*" + str(len(value)).encode() + b"\r\n")
        for item in value:
            _write_resp(stream, item)
    elif isinstance(value, tuple) and value[0] == "simple":
        stream.write(b"+" + value[1] + b"\r\n")
    elif isinstance(value, tuple) and value[0] == "error":
        stream.write(b"-" + value[1] + b"\r\n")
    else:  # pragma: no cover - fixture implementation guard
        raise TypeError(type(value))
    stream.flush()


class _RedisHandler(socketserver.StreamRequestHandler):
    database = 0

    @property
    def state(self) -> _RedisState:
        return self.server.state  # type: ignore[attr-defined]

    def handle(self) -> None:
        self.database = 0
        while True:
            request = _read_resp_request(self.rfile)
            if request is None:
                return
            command = request[0].upper()
            with self.state.lock:
                self.state.commands.append(tuple(request))
            response = self._dispatch(command, request[1:])
            _write_resp(self.wfile, response)

    def _dispatch(self, command: bytes, args: list[bytes]) -> Any:
        if command == b"PING":
            return ("simple", b"PONG")
        if command == b"SELECT" and len(args) == 1:
            self.database = int(args[0])
            return ("simple", b"OK")

        with self.state.lock:
            database = self.state.databases.setdefault(self.database, {})
            if command == b"GET" and len(args) == 1:
                entry = database.get(args[0])
                return None if entry is None else entry.value
            if command == b"TYPE" and len(args) == 1:
                entry = database.get(args[0])
                return ("simple", b"none" if entry is None else entry.redis_type)
            if command == b"PTTL" and len(args) == 1:
                entry = database.get(args[0])
                return -2 if entry is None else entry.pttl_ms
            if command == b"DUMP" and len(args) == 1:
                entry = database.get(args[0])
                return None if entry is None else entry.dump
            if command == b"DBSIZE" and not args:
                return len(database)
            if command == b"SCAN" and args:
                return [b"0", sorted(database)]
            if command == b"RESTORE" and len(args) >= 3:
                key, ttl_raw, dump = args[:3]
                replace = any(arg.upper() == b"REPLACE" for arg in args[3:])
                if key in database and not replace:
                    return ("error", b"BUSYKEY target key exists")
                prefix = b"fixture-dump-v1\x00"
                if not dump.startswith(prefix):
                    return ("error", b"ERR invalid fixture dump")
                ttl = int(ttl_raw)
                database[key] = _RedisEntry(
                    value=dump[len(prefix) :],
                    dump=dump,
                    pttl_ms=-1 if ttl == 0 else ttl,
                )
                self.state.write_commands.append((command, *args))
                return ("simple", b"OK")
            if command in {b"SET", b"DEL", b"FLUSHDB", b"UNLINK", b"EXPIRE"}:
                self.state.write_commands.append((command, *args))
                return ("error", b"ERR unsupported fixture write")
        return ("error", b"ERR unsupported fixture command")


class _ThreadingRedisServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, state: _RedisState):
        super().__init__(("127.0.0.1", 0), _RedisHandler)
        self.state = state


@contextmanager
def _redis_fixture(*, database: int = 15) -> Iterator[tuple[_RedisState, int, str]]:
    state = _RedisState()
    token = "fixture-isolation-token-43b7"
    state.put(database, b"familia:restore:isolation-marker", token.encode())
    server = _ThreadingRedisServer(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, server.server_address[1], token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@dataclass
class _SanitizedFixture:
    root: Path
    config: Path
    redis_state: _RedisState
    redis_port: int
    redis_database: int
    redis_token: str


def _write_fixture_tree(root: Path) -> None:
    (root / "workspace" / "memory").mkdir(parents=True)
    (root / "workspace" / "cron").mkdir(parents=True)
    (root / "workspace" / "USER.md").write_bytes(b"# Sanitized fixture user\n")
    (root / "workspace" / "memory" / "MEMORY.md").write_bytes(
        b"# Sanitized shared legacy memory\n"
    )
    history = (
        b'{"actor":"alice","cursor":1,"text":"'
        + SECRET_SENTINEL.encode()
        + b'","timestamp":"2026-01-01T00:00:00Z"}\n'
        + b'{"actor":"alice","cursor":2,"text":\xff}\n'
    )
    (root / "workspace" / "memory" / "history.jsonl").write_bytes(history)
    (root / "workspace" / "memory" / ".dream_cursor").write_bytes(b"2\n")
    (root / "workspace" / "cron" / "jobs.json").write_bytes(
        _json_bytes(
            {
                "jobs": [
                    {
                        "id": "fixture-job",
                        "created_by": "alice",
                        "target": "alice",
                        "enabled": True,
                    }
                ]
            }
        )
    )
    principals = {
        "principals": [
            {
                "id": "alice",
                "display_name": "Fixture Alice",
                "memx_key": SECRET_SENTINEL,
                "identities": [{"channel": "fixture", "sender_id": "1001"}],
            }
        ]
    }
    (root / "principals.json").write_bytes(_json_bytes(principals))
    (root / "acl.json").write_bytes(
        _json_bytes({SECRET_SENTINEL: ["private:alice:*", "shared:*"]})
    )
    (root / "policy.json").write_bytes(
        _json_bytes(
            {
                "policy_version": "fixture-policy-1",
                "default": "deny",
                "rules": [
                    {
                        "actor": "alice",
                        "action": ["memory.read", "memory.write"],
                        "resource": "private:alice:*",
                        "decision": "allow",
                    }
                ],
            }
        )
    )
    (root / "non-secret-config.json").write_bytes(
        _json_bytes({"environment": "sanitized-fixture", "feature": "memory"})
    )


def _write_capture_config(
    path: Path,
    root: Path,
    redis_port: int,
    redis_database: int,
    redis_token: str,
) -> None:
    config = {
        "config_version": "1.0.0",
        "authorization": {
            "authorization_id": "sanitized-fixture-only",
            "source_kind": "sanitized_fixture",
            "writer_stop_started_at": "2026-01-01T00:00:00Z",
            "writer_stop_ended_at": "2026-01-01T00:01:00Z",
        },
        "source": {
            "root": str(root.resolve()),
            "host_id": "fixture-host",
            "deployment_id": "fixture-deployment",
            "logical_paths": ["~/.nanobot"],
        },
        "required_file_classes": {
            "nanobot_files": ["workspace/USER.md"],
            "workspace_memory": ["workspace/memory/MEMORY.md"],
            "history": ["workspace/memory/history.jsonl"],
            "cursors": ["workspace/memory/.dream_cursor"],
            "scheduler": ["workspace/cron/jobs.json"],
            "principals": ["principals.json"],
            "acl_policy": ["acl.json", "policy.json"],
            "non_secret_config": ["non-secret-config.json"],
        },
        "non_secret_config_paths": ["non-secret-config.json"],
        "redis": {
            "host": "127.0.0.1",
            "port": redis_port,
            "database": redis_database,
            "logical_endpoint": "redis://isolated-fixture/db15",
            "isolation_marker_key": "familia:restore:isolation-marker",
            "isolation_marker_sha256": _sha256(redis_token.encode()),
            "password_env": None,
        },
        "versions": FIXED_VERSIONS,
        "images": [
            {
                "component": "familia",
                "identity": "fixture/familia:0.4.0",
                "digest": "sha256:" + "1" * 64,
            },
            {
                "component": "memx",
                "identity": "fixture/memx:1",
                "digest": "sha256:" + "2" * 64,
            },
        ],
    }
    path.write_bytes(_json_bytes(config))


@pytest.fixture
def sanitized_fixture(tmp_path: Path) -> Iterator[_SanitizedFixture]:
    database = 15
    with _redis_fixture(database=database) as (state, port, token):
        root = tmp_path / "sanitized-source"
        root.mkdir(mode=0o700)
        _write_fixture_tree(root)
        state.put(
            database,
            b"memx:value:private:alice:value:memory",
            _json_bytes({"value": SECRET_SENTINEL, "ts": 1}),
        )
        state.put(
            database,
            b"memx:schema:private:alice:value:memory",
            _json_bytes({"type": "object", "schema_version": "fixture-1"}),
        )
        state.put(
            database,
            b"memx:value:private:alice:value:private_index",
            _json_bytes(["memory"]),
        )
        config = tmp_path / "capture-config.json"
        _write_capture_config(config, root, port, database, token)
        yield _SanitizedFixture(root, config, state, port, database, token)


def _make_restore_target(
    target: Path,
    redis_port: int,
    redis_database: int,
    redis_token: str,
    *,
    non_production: bool = True,
    host: str = "127.0.0.1",
) -> None:
    target.mkdir(mode=0o700)
    marker = {
        "marker_version": "1.0.0",
        "purpose": "familia-memory-restore",
        "target_id": "fixture-target-43b7",
        "non_production": non_production,
        "filesystem_root": str(target.resolve()),
        "redis": {
            "host": host,
            "port": redis_port,
            "database": redis_database,
            "logical_endpoint": "redis://isolated-restore-fixture/db15",
            "isolation_marker_key": "familia:restore:isolation-marker",
            "isolation_marker_sha256": _sha256(redis_token.encode()),
        },
        "compatibility": FIXED_VERSIONS,
    }
    marker_path = target / ".familia-memory-restore-target.json"
    marker_path.write_bytes(_json_bytes(marker))
    marker_path.chmod(0o600)


def _tree_digest(root: Path) -> str:
    records: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            records.append((relative, "symlink", 0))
        elif path.is_file():
            data = path.read_bytes()
            records.append((relative, _sha256(data), len(data)))
        elif path.is_dir():
            records.append((relative + "/", "directory", 0))
    return _sha256(json.dumps(records, separators=(",", ":")).encode())


def _load_manifest(snapshot: Path) -> dict[str, Any]:
    return json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))


def _assert_no_secret(value: str | bytes) -> None:
    needle = SECRET_SENTINEL if isinstance(value, str) else SECRET_SENTINEL.encode()
    if needle in value:
        pytest.fail("secret sentinel leaked outside the protected raw payload")


def test_fixture_round_trip_preserves_bytes_and_logical_state(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    source_tree_before = _tree_digest(sanitized_fixture.root)
    source_redis_before = sanitized_fixture.redis_state.snapshot(
        sanitized_fixture.redis_database
    )
    source_write_count = len(sanitized_fixture.redis_state.write_commands)

    snapshot = tmp_path / "snapshot"
    captured = _snapshot(sanitized_fixture.config, snapshot)
    _assert_success(captured)

    assert _tree_digest(sanitized_fixture.root) == source_tree_before
    assert (
        sanitized_fixture.redis_state.snapshot(sanitized_fixture.redis_database)
        == source_redis_before
    )
    assert len(sanitized_fixture.redis_state.write_commands) == source_write_count

    with _redis_fixture(database=sanitized_fixture.redis_database) as (
        target_redis,
        target_port,
        target_token,
    ):
        target = tmp_path / "isolated-target"
        _make_restore_target(
            target,
            target_port,
            sanitized_fixture.redis_database,
            target_token,
        )
        restored = _restore(snapshot, target, target_token)
        _assert_success(restored)

        compared = _compare(snapshot / "manifest.json", target / "manifest.json")
        _assert_success(compared)
        assert "byte_state=equal" in compared.stdout
        assert "logical_state=equal" in compared.stdout

        for source_file in sorted(sanitized_fixture.root.rglob("*")):
            if source_file.is_file():
                relative = source_file.relative_to(sanitized_fixture.root)
                restored_file = target / "state" / "files" / relative
                assert restored_file.read_bytes() == source_file.read_bytes()

        target_state = target_redis.snapshot(sanitized_fixture.redis_database)
        marker = b"familia:restore:isolation-marker"
        assert target_state[marker][0] == target_token.encode()
        assert {
            key: value for key, value in target_state.items() if key != marker
        } == {
            key: value
            for key, value in source_redis_before.items()
            if key != marker
        }

    manifest = _load_manifest(snapshot)
    assert manifest["status"] == "complete"
    assert manifest["inventory"]["class_counts"]["history"] == 1
    assert manifest["inventory"]["class_counts"]["cursors"] == 1
    assert manifest["inventory"]["class_counts"]["scheduler"] == 1
    assert manifest["inventory"]["class_counts"]["memx_values"] >= 1
    assert manifest["inventory"]["class_counts"]["schemas"] >= 1
    assert manifest["inventory"]["class_counts"]["discovery_indexes"] >= 1
    assert manifest["inventory"]["logical"]["access_matrix"]


def test_restore_refuses_non_isolated_target_before_write(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    snapshot = tmp_path / "snapshot"
    _assert_success(_snapshot(sanitized_fixture.config, snapshot))

    with _redis_fixture(database=sanitized_fixture.redis_database) as (
        target_redis,
        _target_port,
        target_token,
    ):
        target = tmp_path / "not-isolated"
        target.mkdir()
        canary = target / "must-not-change.txt"
        canary.write_bytes(b"unchanged")
        before = _tree_digest(target)
        redis_before = target_redis.snapshot(sanitized_fixture.redis_database)
        writes_before = len(target_redis.write_commands)

        refused = _restore(snapshot, target, target_token)
        assert refused.returncode != 0
        assert _tree_digest(target) == before
        assert target_redis.snapshot(sanitized_fixture.redis_database) == redis_before
        assert len(target_redis.write_commands) == writes_before
        assert not (target / "manifest.json").exists()


def test_snapshot_reports_are_secret_safe(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    snapshot = tmp_path / "snapshot"
    captured = _snapshot(sanitized_fixture.config, snapshot)
    _assert_success(captured)
    _assert_no_secret(captured.stdout)
    _assert_no_secret(captured.stderr)

    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    _assert_no_secret(manifest_bytes)
    protected_payloads = [path for path in snapshot.rglob("*") if path.is_file()]
    assert any(SECRET_SENTINEL.encode() in path.read_bytes() for path in protected_payloads)
    assert snapshot.stat().st_mode & 0o077 == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in protected_payloads)

    compared = _compare(snapshot / "manifest.json", snapshot / "manifest.json")
    _assert_success(compared)
    _assert_no_secret(compared.stdout)
    _assert_no_secret(compared.stderr)


def test_repeated_capture_has_stable_content_hashes_and_same_output_is_zero_write(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    first = tmp_path / "snapshot-a"
    second = tmp_path / "snapshot-b"
    _assert_success(_snapshot(sanitized_fixture.config, first))
    _assert_success(_snapshot(sanitized_fixture.config, second))
    manifest_a = _load_manifest(first)
    manifest_b = _load_manifest(second)
    assert manifest_a["content_hashes"] == manifest_b["content_hashes"]
    assert manifest_a["inventory"]["flat_files"] == manifest_b["inventory"]["flat_files"]
    assert manifest_a["inventory"]["redis"] == manifest_b["inventory"]["redis"]
    manifest_mtime = (first / "manifest.json").stat().st_mtime_ns
    _assert_success(_snapshot(sanitized_fixture.config, first))
    assert (first / "manifest.json").stat().st_mtime_ns == manifest_mtime


def test_repeated_restore_is_idempotent_and_divergence_refuses_before_write(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    snapshot = tmp_path / "snapshot"
    _assert_success(_snapshot(sanitized_fixture.config, snapshot))
    with _redis_fixture(database=sanitized_fixture.redis_database) as (
        target_redis,
        target_port,
        token,
    ):
        target = tmp_path / "isolated-target"
        _make_restore_target(target, target_port, sanitized_fixture.redis_database, token)
        _assert_success(_restore(snapshot, target, token))
        digest_after_first = _tree_digest(target)
        writes_after_first = len(target_redis.write_commands)
        manifest_mtime = (target / "manifest.json").stat().st_mtime_ns

        _assert_success(_restore(snapshot, target, token))
        assert _tree_digest(target) == digest_after_first
        assert len(target_redis.write_commands) == writes_after_first
        assert (target / "manifest.json").stat().st_mtime_ns == manifest_mtime

        divergent = target / "state" / "files" / "workspace" / "USER.md"
        divergent.write_bytes(b"divergent target bytes")
        diverged_digest = _tree_digest(target)
        refused = _restore(snapshot, target, token)
        assert refused.returncode != 0
        assert _tree_digest(target) == diverged_digest
        assert len(target_redis.write_commands) == writes_after_first


def test_schema_is_draft_2020_12_meta_valid_and_recursively_strict(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    snapshot = tmp_path / "snapshot"
    _assert_success(_snapshot(sanitized_fixture.config, snapshot))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    validator = Draft202012Validator(schema)
    manifest = _load_manifest(snapshot)
    assert not list(validator.iter_errors(manifest))

    malformed = json.loads(json.dumps(manifest))
    malformed["inventory"]["logical"]["records"][0]["undeclared"] = True
    assert list(validator.iter_errors(malformed))

    malformed = json.loads(json.dumps(manifest))
    malformed["inventory"]["flat_files"][0]["payload_path"] = "../escape"
    assert list(validator.iter_errors(malformed))

    malformed = json.loads(json.dumps(manifest))
    malformed["inventory"]["flat_files"][0]["payload_path"] = "/absolute/escape"
    assert list(validator.iter_errors(malformed))

    malformed = json.loads(json.dumps(manifest))
    malformed["inventory"]["flat_files"][0]["object_kind"] = "unknown-kind"
    assert list(validator.iter_errors(malformed))


@pytest.mark.parametrize(
    "corruption",
    ["traversal", "absolute", "duplicate", "missing", "size", "hash", "version"],
)
def test_invalid_manifest_or_payload_returns_comparator_exit_2(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
    corruption: str,
) -> None:
    snapshot = tmp_path / "snapshot"
    _assert_success(_snapshot(sanitized_fixture.config, snapshot))
    corrupt = tmp_path / f"corrupt-{corruption}"
    shutil.copytree(snapshot, corrupt)
    manifest_path = corrupt / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["inventory"]["flat_files"][0]

    if corruption == "traversal":
        entry["payload_path"] = "../escape"
        manifest_path.write_bytes(_json_bytes(manifest))
    elif corruption == "absolute":
        entry["payload_path"] = "/absolute/escape"
        manifest_path.write_bytes(_json_bytes(manifest))
    elif corruption == "duplicate":
        manifest["inventory"]["flat_files"].append(json.loads(json.dumps(entry)))
        manifest_path.write_bytes(_json_bytes(manifest))
    elif corruption == "version":
        manifest["snapshot_format_version"] = "999.0.0"
        manifest_path.write_bytes(_json_bytes(manifest))
    else:
        payload = corrupt / entry["payload_path"]
        if corruption == "missing":
            payload.unlink()
        elif corruption == "size":
            entry["byte_length"] += 1
            manifest_path.write_bytes(_json_bytes(manifest))
        elif corruption == "hash":
            payload.write_bytes(payload.read_bytes() + b"tamper")

    compared = _compare(snapshot / "manifest.json", manifest_path)
    assert compared.returncode == 2, _safe_process_details(compared)
    _assert_no_secret(compared.stdout)
    _assert_no_secret(compared.stderr)


def test_payload_symlink_escape_is_invalid_and_restore_writes_nothing(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    snapshot = tmp_path / "snapshot"
    _assert_success(_snapshot(sanitized_fixture.config, snapshot))
    corrupt = tmp_path / "symlink-snapshot"
    shutil.copytree(snapshot, corrupt)
    manifest = _load_manifest(corrupt)
    payload = corrupt / manifest["inventory"]["flat_files"][0]["payload_path"]
    outside = tmp_path / "outside.bin"
    outside.write_bytes(payload.read_bytes())
    payload.unlink()
    payload.symlink_to(outside)
    compared = _compare(snapshot / "manifest.json", corrupt / "manifest.json")
    assert compared.returncode == 2

    with _redis_fixture(database=sanitized_fixture.redis_database) as (
        target_redis,
        target_port,
        token,
    ):
        target = tmp_path / "isolated-target"
        _make_restore_target(target, target_port, sanitized_fixture.redis_database, token)
        before = _tree_digest(target)
        writes_before = len(target_redis.write_commands)
        refused = _restore(corrupt, target, token)
        assert refused.returncode != 0
        assert _tree_digest(target) == before
        assert len(target_redis.write_commands) == writes_before


def test_comparator_exit_1_distinguishes_byte_and_logical_mismatch(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    first = tmp_path / "snapshot-a"
    second = tmp_path / "snapshot-b"
    _assert_success(_snapshot(sanitized_fixture.config, first))
    user_file = sanitized_fixture.root / "workspace" / "USER.md"
    user_file.write_bytes(user_file.read_bytes() + b"changed bytes\n")
    _assert_success(_snapshot(sanitized_fixture.config, second))
    byte_difference = _compare(first / "manifest.json", second / "manifest.json")
    assert byte_difference.returncode == 1
    assert "byte_state=unequal" in byte_difference.stdout
    assert "logical_state=equal" in byte_difference.stdout

    third = tmp_path / "snapshot-c"
    principals_path = sanitized_fixture.root / "principals.json"
    principals = json.loads(principals_path.read_text(encoding="utf-8"))
    principals["principals"][0]["display_name"] = "Changed Fixture Name"
    principals_path.write_bytes(_json_bytes(principals))
    _assert_success(_snapshot(sanitized_fixture.config, third))
    logical_difference = _compare(second / "manifest.json", third / "manifest.json")
    assert logical_difference.returncode == 1
    assert "logical_state=unequal" in logical_difference.stdout


def test_malformed_jsonl_is_raw_preserved_and_flagged_without_value_disclosure(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    snapshot = tmp_path / "snapshot"
    _assert_success(_snapshot(sanitized_fixture.config, snapshot))
    original = sanitized_fixture.root / "workspace" / "memory" / "history.jsonl"
    manifest = _load_manifest(snapshot)
    history_entry = next(
        entry
        for entry in manifest["inventory"]["flat_files"]
        if entry["identity"] == "workspace/memory/history.jsonl"
    )
    assert (snapshot / history_entry["payload_path"]).read_bytes() == original.read_bytes()
    anomalies = manifest["inventory"]["logical"]["anomalies"]
    assert any(
        anomaly["raw_identity"] == "workspace/memory/history.jsonl"
        and anomaly["classification"] in {"invalid_utf8", "malformed_jsonl"}
        for anomaly in anomalies
    )
    _assert_no_secret(json.dumps(anomalies, sort_keys=True))


def test_snapshot_failure_has_no_final_manifest_and_source_is_unchanged(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    config = json.loads(sanitized_fixture.config.read_text(encoding="utf-8"))
    config["required_file_classes"]["history"] = ["missing-history.jsonl"]
    broken_config = tmp_path / "broken-config.json"
    broken_config.write_bytes(_json_bytes(config))
    output = tmp_path / "failed-snapshot"
    source_before = _tree_digest(sanitized_fixture.root)
    redis_before = sanitized_fixture.redis_state.snapshot(sanitized_fixture.redis_database)
    writes_before = len(sanitized_fixture.redis_state.write_commands)
    failed = _snapshot(broken_config, output)
    assert failed.returncode != 0
    assert not (output / "manifest.json").exists()
    assert _tree_digest(sanitized_fixture.root) == source_before
    assert sanitized_fixture.redis_state.snapshot(sanitized_fixture.redis_database) == redis_before
    assert len(sanitized_fixture.redis_state.write_commands) == writes_before


def test_snapshot_rejects_source_symlink_without_finalizing(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    outside = tmp_path / "outside-secret"
    outside.write_text(SECRET_SENTINEL, encoding="utf-8")
    (sanitized_fixture.root / "symlink-escape").symlink_to(outside)
    output = tmp_path / "failed-snapshot"
    failed = _snapshot(sanitized_fixture.config, output)
    assert failed.returncode != 0
    assert not (output / "manifest.json").exists()
    _assert_no_secret(failed.stdout)
    _assert_no_secret(failed.stderr)


def test_restore_refuses_production_like_redis_marker_before_any_write(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    snapshot = tmp_path / "snapshot"
    _assert_success(_snapshot(sanitized_fixture.config, snapshot))
    with _redis_fixture(database=sanitized_fixture.redis_database) as (
        target_redis,
        target_port,
        token,
    ):
        target = tmp_path / "unsafe-target"
        _make_restore_target(
            target,
            target_port,
            sanitized_fixture.redis_database,
            token,
            non_production=False,
        )
        before = _tree_digest(target)
        writes_before = len(target_redis.write_commands)
        refused = _restore(snapshot, target, token)
        assert refused.returncode != 0
        assert _tree_digest(target) == before
        assert len(target_redis.write_commands) == writes_before


def test_restore_refuses_incompatible_target_before_any_write(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    snapshot = tmp_path / "snapshot"
    _assert_success(_snapshot(sanitized_fixture.config, snapshot))
    with _redis_fixture(database=sanitized_fixture.redis_database) as (
        target_redis,
        target_port,
        token,
    ):
        target = tmp_path / "incompatible-target"
        _make_restore_target(target, target_port, sanitized_fixture.redis_database, token)
        marker_path = target / ".familia-memory-restore-target.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["compatibility"]["acl"] = "incompatible-acl"
        marker_path.write_bytes(_json_bytes(marker))
        marker_path.chmod(0o600)
        before = _tree_digest(target)
        writes_before = len(target_redis.write_commands)
        refused = _restore(snapshot, target, token)
        assert refused.returncode != 0
        assert _tree_digest(target) == before
        assert len(target_redis.write_commands) == writes_before


def test_restore_refuses_protected_and_symlinked_target_paths_without_redis_write(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    snapshot = tmp_path / "snapshot"
    _assert_success(_snapshot(sanitized_fixture.config, snapshot))
    with _redis_fixture(database=sanitized_fixture.redis_database) as (
        target_redis,
        _target_port,
        token,
    ):
        evidence_target = tmp_path / "evidence" / "target"
        evidence_target.mkdir(parents=True, mode=0o700)
        nanobot_target = tmp_path / ".nanobot" / "target"
        nanobot_target.mkdir(parents=True, mode=0o700)
        real_target = tmp_path / "real-target"
        real_target.mkdir(mode=0o700)
        symlink_target = tmp_path / "target-symlink"
        symlink_target.symlink_to(real_target, target_is_directory=True)
        writes_before = len(target_redis.write_commands)

        for unsafe_target in (
            REPO_ROOT,
            Path.home(),
            evidence_target,
            nanobot_target,
            symlink_target,
        ):
            refused = _restore(snapshot, unsafe_target, token)
            assert refused.returncode != 0
            assert len(target_redis.write_commands) == writes_before


def test_snapshot_requires_explicit_read_only_mode(
    tmp_path: Path,
    sanitized_fixture: _SanitizedFixture,
) -> None:
    output = tmp_path / "snapshot"
    process = _run(
        "bash",
        str(SNAPSHOT_SCRIPT),
        "--config",
        str(sanitized_fixture.config),
        "--output",
        str(output),
    )
    assert process.returncode != 0
    assert not (output / "manifest.json").exists()
