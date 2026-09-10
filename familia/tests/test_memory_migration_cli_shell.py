from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import redirect_stdout
import inspect
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from jsonschema import Draft202012Validator

from familia import memory_migration
from familia.cli import graph_admin
from familia.memory_contract import MEMORY_CONTRACT


class _RecordingIngestor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ingest(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "committed:private:alice:memory:legacy-history"


def _write_target_config(
    target_root: Path,
    workspace: Path,
    principals: Path,
) -> Path:
    config_path = target_root / "target-config.json"
    config = {
        "schema_version": "1.0.0",
        "isolation_id": "synthetic-target",
        "install_root": str(target_root.resolve()),
        "workspace": str(workspace.resolve()),
        "principals": str(principals.resolve()),
        "runner": {
            "name": "runner",
            "id": "a" * 64,
            "image_digest": "sha256:" + "1" * 64,
            "install_mount": {
                "name": "runner-volume",
                "id": "runner-volume",
                "destination": str(target_root.resolve()),
            },
        },
        "memx": {
            "name": "memx",
            "id": "b" * 64,
            "image_digest": "sha256:" + "2" * 64,
            "base_url": "http://memx:8000",
            "api_key": "synthetic-key",
            "redis_env": "REDIS_URL",
            "redis_url": "redis://redis:6379/0",
        },
        "redis": {
            "name": "redis",
            "id": "c" * 64,
            "image_digest": "sha256:" + "3" * 64,
            "storage_volume": {"name": "redis-volume", "id": "redis-volume"},
            "storage_destination": "/data",
            "port": 6379,
            "database": 0,
        },
        "network": {
            "name": "familia-internal",
            "id": "d" * 64,
        },
        "model": {
            "provider": "synthetic",
            "name": "synthetic-model",
            "config_path": str((target_root / "model.json").resolve()),
        },
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _docker_target_objects(
    *,
    runner_destination: str = "/work",
) -> dict[str, dict[str, object]]:
    labels = {"familia.target": "synthetic-target"}
    network_id = "d" * 64
    ids = {"runner": "a" * 64, "memx": "b" * 64, "redis": "c" * 64}
    objects: dict[str, dict[str, object]] = {}
    for section, image in (
        ("runner", "sha256:" + "1" * 64),
        ("memx", "sha256:" + "2" * 64),
        ("redis", "sha256:" + "3" * 64),
    ):
        env = ["REDIS_URL=redis://redis:6379/0"] if section == "memx" else []
        mounts = (
            [
                {
                    "Type": "volume",
                    "Name": "runner-volume",
                    "Destination": runner_destination,
                    "RW": True,
                }
            ]
            if section == "runner"
            else (
                [{"Type": "volume", "Name": "redis-volume", "Destination": "/data", "RW": True}]
                if section == "redis"
                else []
            )
        )
        objects[f"container:{section}"] = {
            "Id": ids[section],
            "Name": "/" + section,
            "Image": image,
            "State": {"Running": True},
            "Config": {"Labels": labels, "Env": env},
            "NetworkSettings": {
                "Networks": {
                    "familia-internal": {
                        "NetworkID": network_id,
                        "Aliases": [section],
                    }
                }
            },
            "Mounts": mounts,
        }
    objects["network:familia-internal"] = {
        "Id": network_id,
        "Name": "familia-internal",
        "Internal": True,
        "Labels": labels,
        "Containers": {value: {} for value in ids.values()},
    }
    objects["volume:runner-volume"] = {
        "Name": "runner-volume",
        "Id": "runner-volume",
        "Labels": labels,
    }
    objects["volume:redis-volume"] = {
        "Name": "redis-volume",
        "Id": "redis-volume",
        "Labels": labels,
    }
    return objects


def _prepare_cli_target(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, tuple[Path, ...]]:
    snapshot_root = tmp_path / "snapshot"
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    memory_dir = workspace / "memory"
    snapshot_root.mkdir()
    memory_dir.mkdir(parents=True)
    target_root.chmod(0o700)
    (target_root / ".familia-memory-migration-target.json").write_text(
        json.dumps(
            {
                "marker_version": "1.0.0",
                "purpose": "familia-memory-migration",
                "target_id": "isolated-test",
                "non_production": True,
                "filesystem_root": str(target_root.resolve()),
                "snapshot_id": "a" * 64,
                "contract_version": memory_migration.MEMORY_CONTRACT_VERSION,
            }
        ),
        encoding="utf-8",
    )
    principals_path = workspace / "principals.json"
    principals_path.write_text(
        json.dumps({"principals": [{"id": "alice", "memx_key": "alice-key"}]}),
        encoding="utf-8",
    )
    target_config = _write_target_config(target_root, workspace, principals_path)
    flat_paths = (
        workspace / "USER.md",
        workspace / "MEMORY.md",
        memory_dir / "MEMORY.md",
    )
    for path in flat_paths:
        path.write_text("legacy", encoding="utf-8")
    (memory_dir / "history.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cursor": 1,
                "timestamp": "2026-09-10 10:00",
                "actor": "alice",
                "content": "legacy fact",
                "provenance": {"source": "synthetic", "idempotency_key": None},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return (
        snapshot_root,
        target_root,
        workspace,
        principals_path,
        target_config,
        flat_paths,
    )


def _patch_docker_cli(
    monkeypatch: pytest.MonkeyPatch,
    objects: dict[str, dict[str, object]],
    local_namespaces: dict[str, tuple[int, int]],
    container_namespaces: dict[str, tuple[int, int]],
) -> tuple[list[list[str]], list[dict[str, str]]]:
    docker_commands: list[list[str]] = []
    docker_envs: list[dict[str, str]] = []
    original_stat = memory_migration.os.stat

    def fake_stat(path: str, *args: object, **kwargs: object) -> object:
        path_text = str(path)
        namespace = path_text.rsplit("/", 1)[-1]
        if path_text.startswith("/proc/self/ns/") and namespace in local_namespaces:
            device, inode = local_namespaces[namespace]
            return SimpleNamespace(st_dev=device, st_ino=inode)
        return original_stat(path, *args, **kwargs)

    def fake_docker_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        command = list(command)
        docker_commands.append(command)
        env = kwargs.get("env")
        assert isinstance(env, dict)
        docker_envs.append(dict(env))
        assert command[:3] == [
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
        ]
        if command[3] == "inspect":
            kind, reference = command[5], command[6]
            return SimpleNamespace(
                stdout=json.dumps([objects[f"{kind}:{reference}"]]),
                stderr="",
            )
        if command[3] == "exec":
            assert command[4] == objects["container:runner"]["Id"]
            assert command[5] == "python3"
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        namespace: {"st_dev": device, "st_ino": inode}
                        for namespace, (device, inode) in container_namespaces.items()
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected Docker command: {command}")

    monkeypatch.setattr(memory_migration.os, "stat", fake_stat)
    monkeypatch.setattr(memory_migration.subprocess, "run", fake_docker_run)
    return docker_commands, docker_envs


def _patch_target_http(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Any], dict[str, Any]]:
    import httpx

    requests: list[Any] = []
    client_type = httpx.AsyncClient
    values: dict[str, str] = {}
    versions: dict[str, int] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        key = request.url.params.get("key")
        if request.method == "GET":
            if key not in values:
                return httpx.Response(404, request=request)
            return httpx.Response(
                200,
                json={"value": values[key], "ts": versions[key]},
                request=request,
            )
        payload = json.loads(request.content.decode("utf-8"))
        if request.url.path.endswith("/delete"):
            values.pop(payload["key"], None)
            versions.pop(payload["key"], None)
            body = {
                "ok": True,
                "status": "deleted",
                "committed": True,
                "updated": True,
                "retryable": False,
                "version": None,
            }
        else:
            values[payload["key"]] = payload["value"]
            versions[payload["key"]] = versions.get(payload["key"], 0) + 1
            body = {
                "ok": True,
                "status": "committed",
                "committed": True,
                "updated": True,
                "retryable": False,
                "version": versions[payload["key"]],
            }
        return httpx.Response(200, json=body, request=request)

    async def handle_async(request: httpx.Request) -> httpx.Response:
        return handle(request)

    async_transport = httpx.MockTransport(handle_async)
    monkeypatch.setattr(
        "familia.principal_memory_ingestor.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=async_transport, **kwargs),
    )

    sync_transport = httpx.MockTransport(handle)

    def get(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=sync_transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", get)
    return requests, values


def test_f2_registry_is_explicit_and_missing_registry_refuses(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    workspace = install_root / "workspace"
    workspace.mkdir(parents=True)
    from familia.memory_migration import MigrationPreflightError, _load_known_actors

    with pytest.raises(MigrationPreflightError, match="principals registry"):
        _load_known_actors(install_root / "principals.json")

    principals = install_root / "principals.json"
    principals.write_text("{broken", encoding="utf-8")
    with pytest.raises((MigrationPreflightError, json.JSONDecodeError)):
        _load_known_actors(principals)


def test_f2_docker_proof_binds_runner_memx_redis_and_internal_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    principals = workspace / "principals.json"
    workspace.mkdir(parents=True)
    config_path = _write_target_config(target_root, workspace, principals)
    config = memory_migration._target_config(
        config_path, target_root.resolve(), workspace.resolve(), principals.resolve()
    )
    objects = _docker_target_objects(
        runner_destination=config["runner"]["install_mount"]["destination"],
    )
    monkeypatch.setattr(
        memory_migration,
        "_docker_inspect",
        lambda kind, reference: objects[f"{kind}:{reference}"],
    )
    config["runner"]["hostname"] = "configured-runner"
    objects["container:runner"]["Config"]["Hostname"] = "runtime-runner"
    namespace_values = {
        "net": (11, 101),
        "pid": (12, 102),
        "mnt": (13, 103),
    }

    original_stat = memory_migration.os.stat

    def fake_stat(path: str, *args: object, **kwargs: object) -> object:
        path_text = str(path)
        namespace = path_text.rsplit("/", 1)[-1]
        if path_text.startswith("/proc/self/ns/") and namespace in namespace_values:
            device, inode = namespace_values[namespace]
            return SimpleNamespace(st_dev=device, st_ino=inode)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(memory_migration.os, "stat", fake_stat)
    monkeypatch.setattr(
        memory_migration.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    namespace: {"st_dev": device, "st_ino": inode}
                    for namespace, (device, inode) in namespace_values.items()
                }
            ),
            stderr="",
        ),
    )
    memory_migration._docker_verify_target(config)

    config["install_root"] = "/other"
    with pytest.raises(
        memory_migration.MigrationPreflightError,
        match="install root is not on",
    ):
        memory_migration._docker_verify_target(config)

    config["install_root"] = str(target_root.resolve())
    foreign_mount = {
        "Type": "bind",
        "Source": "/other",
        "Destination": f"{config['install_root']}/nested",
        "RW": True,
    }
    objects["container:runner"]["Mounts"].append(foreign_mount)
    with pytest.raises(
        memory_migration.MigrationPreflightError,
        match="overlapped",
    ):
        memory_migration._docker_verify_target(config)
    objects["container:runner"]["Mounts"].remove(foreign_mount)

    objects["network:familia-internal"]["Internal"] = False
    with pytest.raises(memory_migration.MigrationPreflightError, match="internal"):
        memory_migration._docker_verify_target(config)


def test_f2_docker_proof_rejects_namespace_mismatch_even_with_matching_hostname(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    principals = workspace / "principals.json"
    workspace.mkdir(parents=True)
    config_path = _write_target_config(target_root, workspace, principals)
    config = memory_migration._target_config(
        config_path, target_root.resolve(), workspace.resolve(), principals.resolve()
    )
    objects = _docker_target_objects(
        runner_destination=config["runner"]["install_mount"]["destination"],
    )
    objects["container:runner"]["Config"]["Hostname"] = "configured-runner"
    config["runner"]["hostname"] = "configured-runner"
    monkeypatch.setattr(
        memory_migration,
        "_docker_inspect",
        lambda kind, reference: objects[f"{kind}:{reference}"],
    )
    local_namespaces = {
        "net": (21, 201),
        "pid": (22, 202),
        "mnt": (23, 203),
    }
    container_namespaces = {
        "net": (31, 301),
        "pid": (22, 202),
        "mnt": (23, 203),
    }

    original_stat = memory_migration.os.stat

    def fake_stat(path: str, *args: object, **kwargs: object) -> object:
        path_text = str(path)
        namespace = path_text.rsplit("/", 1)[-1]
        if path_text.startswith("/proc/self/ns/") and namespace in local_namespaces:
            device, inode = local_namespaces[namespace]
            return SimpleNamespace(st_dev=device, st_ino=inode)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(memory_migration.os, "stat", fake_stat)
    docker_exec = Mock(
        return_value=SimpleNamespace(
            stdout=json.dumps(
                {
                    namespace: {"st_dev": device, "st_ino": inode}
                    for namespace, (device, inode) in container_namespaces.items()
                }
            ),
            stderr="",
        )
    )
    monkeypatch.setattr(memory_migration.subprocess, "run", docker_exec)
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "untrusted")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "C:/untrusted-certs")

    with pytest.raises(memory_migration.MigrationPreflightError, match="namespace"):
        memory_migration._docker_verify_target(config)

    command = docker_exec.call_args.args[0]
    assert command[:3] == ["docker", "--host", "unix:///var/run/docker.sock"]
    assert command[command.index("exec") + 1] == config["runner"]["id"]
    assert command[command.index("exec") + 2] == "python3"
    assert "DOCKER_HOST" not in docker_exec.call_args.kwargs["env"]
    assert "DOCKER_CONTEXT" not in docker_exec.call_args.kwargs["env"]
    assert "DOCKER_TLS_VERIFY" not in docker_exec.call_args.kwargs["env"]
    assert "DOCKER_CERT_PATH" not in docker_exec.call_args.kwargs["env"]


def test_f2_docker_proof_rejects_invalid_namespace_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    principals = workspace / "principals.json"
    workspace.mkdir(parents=True)
    config_path = _write_target_config(target_root, workspace, principals)
    config = memory_migration._target_config(
        config_path, target_root.resolve(), workspace.resolve(), principals.resolve()
    )
    objects = _docker_target_objects(
        runner_destination=config["runner"]["install_mount"]["destination"],
    )
    monkeypatch.setattr(
        memory_migration,
        "_docker_inspect",
        lambda kind, reference: objects[f"{kind}:{reference}"],
    )
    original_stat = memory_migration.os.stat

    def fake_stat(path: str, *args: object, **kwargs: object) -> object:
        if str(path).startswith("/proc/self/ns/"):
            return SimpleNamespace(st_dev=41, st_ino=401)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(memory_migration.os, "stat", fake_stat)
    monkeypatch.setattr(
        memory_migration.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="not-json", stderr=""),
    )

    with pytest.raises(memory_migration.MigrationPreflightError, match="namespace"):
        memory_migration._docker_verify_target(config)


def test_apply_namespace_refusal_happens_before_manifest_write_or_target_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        snapshot_root,
        target_root,
        workspace,
        principals_path,
        target_config,
        flat_paths,
    ) = _prepare_cli_target(tmp_path)
    flat_before = {path: path.read_bytes() for path in flat_paths}
    manifest = target_root / "migration-plan.json"
    writes: list[tuple[Path, bytes]] = []
    objects = _docker_target_objects(
        runner_destination=str(target_root.resolve()),
    )
    local_namespaces = {
        "net": (51, 501),
        "pid": (52, 502),
        "mnt": (53, 503),
    }
    container_namespaces = {
        "net": (61, 601),
        "pid": (52, 502),
        "mnt": (53, 503),
    }
    docker_commands, docker_envs = _patch_docker_cli(
        monkeypatch,
        objects,
        local_namespaces,
        container_namespaces,
    )
    monkeypatch.setattr("socket.gethostname", lambda: "a" * 12)
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "untrusted")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "C:/untrusted-certs")

    def record_write(path: Path, data: bytes) -> None:
        writes.append((path, data))

    with (
        patch(
            "scripts.compare_memory_state.load_and_validate_manifest",
            return_value={
                "schema_version": memory_migration.SNAPSHOT_SCHEMA_VERSION,
                "snapshot_format_version": memory_migration.SNAPSHOT_FORMAT_VERSION,
                "status": "complete",
                "state_role": "source",
                "snapshot_id": "a" * 64,
                "versions": {
                    "snapshot_schema": memory_migration.SNAPSHOT_SCHEMA_VERSION,
                },
            },
        ),
        patch.object(
            memory_migration,
            "build_legacy_transition_plan",
            return_value={"status": "ready", "actions": [], "summary": {}},
        ),
        patch.object(
            memory_migration,
            "_write_private_atomic",
            side_effect=record_write,
        ),
        patch.object(
            memory_migration,
            "_target_get_raw",
            side_effect=AssertionError("target HTTP must not be configured"),
        ) as target_get_raw,
        patch(
            "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
            side_effect=AssertionError("ingestor must not be configured"),
        ) as ingestor_type,
    ):
        exit_code = memory_migration.cli(
            [
                "--snapshot",
                str(snapshot_root),
                "--install-root",
                str(target_root),
                "--workspace",
                str(workspace),
                "--principals",
                str(principals_path),
                "--target-config",
                str(target_config),
                "--manifest",
                str(manifest),
                "--journal",
                str(target_root / "migration-journal.jsonl"),
                "--apply",
            ]
        )

    assert exit_code == 2
    target_get_raw.assert_not_called()
    ingestor_type.assert_not_called()
    assert writes == []
    assert not manifest.exists()
    assert {path: path.read_bytes() for path in flat_paths} == flat_before
    assert len(docker_commands) == 7
    assert sum(command[3] == "inspect" for command in docker_commands) == 6
    assert sum(command[3] == "exec" for command in docker_commands) == 1
    assert all(
        command[:3] == ["docker", "--host", "unix:///var/run/docker.sock"]
        for command in docker_commands
    )
    assert all("DOCKER_HOST" not in env for env in docker_envs)
    assert all("DOCKER_CONTEXT" not in env for env in docker_envs)
    assert all("DOCKER_TLS_VERIFY" not in env for env in docker_envs)
    assert all("DOCKER_CERT_PATH" not in env for env in docker_envs)
    exec_command = next(command for command in docker_commands if command[3] == "exec")
    assert exec_command[4] == objects["container:runner"]["Id"]


def test_apply_namespace_proof_allows_matching_namespaces_and_repeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        snapshot_root,
        target_root,
        workspace,
        principals_path,
        target_config,
        flat_paths,
    ) = _prepare_cli_target(tmp_path)
    config = json.loads(target_config.read_text(encoding="utf-8"))
    config["runner"]["hostname"] = "custom-runner"
    target_config.write_text(json.dumps(config), encoding="utf-8")
    history_path = workspace / "memory" / "history.jsonl"
    history_before = history_path.read_bytes()

    objects = _docker_target_objects(
        runner_destination=str(target_root.resolve()),
    )
    objects["container:runner"]["Config"]["Hostname"] = "runtime-does-not-matter"
    namespaces = {
        "net": (71, 701),
        "pid": (72, 702),
        "mnt": (73, 703),
    }
    docker_commands, docker_envs = _patch_docker_cli(
        monkeypatch,
        objects,
        namespaces,
        namespaces,
    )
    monkeypatch.setattr("socket.gethostname", lambda: "host-does-not-matter")
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "untrusted")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "C:/untrusted-certs")
    requests, values = _patch_target_http(monkeypatch)
    manifest = target_root / "migration-plan.json"

    async def consolidate(
        _actor: str,
        _records: list[dict[str, Any]],
        _existing: str,
    ) -> str:
        return "consolidated legacy history"

    snapshot = {
        "schema_version": memory_migration.SNAPSHOT_SCHEMA_VERSION,
        "snapshot_format_version": memory_migration.SNAPSHOT_FORMAT_VERSION,
        "status": "complete",
        "state_role": "source",
        "snapshot_id": "a" * 64,
        "versions": {"snapshot_schema": memory_migration.SNAPSHOT_SCHEMA_VERSION},
    }
    argv = [
        "--snapshot",
        str(snapshot_root),
        "--install-root",
        str(target_root),
        "--workspace",
        str(workspace),
        "--principals",
        str(principals_path),
        "--target-config",
        str(target_config),
        "--manifest",
        str(manifest),
        "--journal",
        str(target_root / "migration-journal.jsonl"),
        "--apply",
        "--json",
    ]
    with (
        patch(
            "scripts.compare_memory_state.load_and_validate_manifest",
            return_value=snapshot,
        ),
        patch.object(
            memory_migration,
            "make_configured_history_consolidator",
            return_value=consolidate,
        ) as consolidator_factory,
    ):
        first_output = io.StringIO()
        with redirect_stdout(first_output):
            first_exit = memory_migration.cli(argv)
        second_output = io.StringIO()
        with redirect_stdout(second_output):
            second_exit = memory_migration.cli(argv)

    assert first_exit == 0
    assert second_exit == 0
    assert json.loads(first_output.getvalue())["status"] == "complete"
    assert json.loads(second_output.getvalue())["status"] == "complete"
    consolidator_factory.assert_called()
    assert values["private:alice:memory:legacy-history"] == "consolidated legacy history"
    assert all(path.read_bytes() == b"" for path in flat_paths)
    assert history_path.read_bytes() == history_before
    assert manifest.exists()
    assert [request.method for request in requests] == [
        "GET",
        "GET",
        "POST",
        "GET",
        "GET",
        "POST",
    ]
    assert [request.url.path for request in requests] == [
        "/get",
        "/get",
        "/set",
        "/get",
        "/get",
        "/set",
    ]
    assert len(docker_commands) == 14
    assert sum(command[3] == "inspect" for command in docker_commands) == 12
    assert sum(command[3] == "exec" for command in docker_commands) == 2
    assert all(
        command[:3] == ["docker", "--host", "unix:///var/run/docker.sock"]
        for command in docker_commands
    )
    assert all("DOCKER_HOST" not in env for env in docker_envs)
    assert all("DOCKER_CONTEXT" not in env for env in docker_envs)
    assert all("DOCKER_TLS_VERIFY" not in env for env in docker_envs)
    assert all("DOCKER_CERT_PATH" not in env for env in docker_envs)
    assert all(
        command[4] == objects["container:runner"]["Id"]
        for command in docker_commands
        if command[3] == "exec"
    )


def test_obsolete_isolated_migration_api_is_absent() -> None:
    obsolete_symbols = {
        "MIGRATION_SCHEMA_VERSION",
        "DISPOSITIONS",
        "UNRESOLVED_DISPOSITIONS",
        "MigrationTarget",
        "IsolatedFileTarget",
        "build_migration_plan",
        "load_action_value",
        "apply_migration_plan",
    }

    assert {
        name for name in obsolete_symbols if hasattr(memory_migration, name)
    } == set()


def test_canonical_transition_has_no_unreachable_review_tail(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    for path in (
        workspace / "USER.md",
        workspace / "MEMORY.md",
        memory_dir / "MEMORY.md",
    ):
        path.write_text("legacy flat memory", encoding="utf-8")
    (memory_dir / "history.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cursor": 1,
                "timestamp": "2026-07-26 10:00",
                "actor": "alice",
                "content": "Prefers short answers",
                "provenance": {
                    "source": "runtime_history",
                    "idempotency_key": None,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    forbidden = {
        "conflict",
        "quarantine_needs_review",
        "skip_warning",
        "dirty_legacy",
        "needs_review",
        "ready_with_warnings",
        "warnings",
    }
    code_surfaces = {
        "planner": inspect.getsource(
            memory_migration.build_legacy_transition_plan
        ),
        "apply": inspect.getsource(
            memory_migration.apply_legacy_transition_plan
        ),
        "isolated_cli": inspect.getsource(memory_migration.cli),
        "graph_admin": inspect.getsource(
            graph_admin.cmd_migrate_hybrid_storage
        ),
    }
    violations = [
        f"{surface}:{token}"
        for surface, source in code_surfaces.items()
        for token in sorted(forbidden)
        if token in source
    ]

    plan = memory_migration.build_legacy_transition_plan(
        workspace=workspace,
        known_actors={"alice"},
    )
    expected_summary = dict(
        Counter(action["disposition"] for action in plan["actions"])
    )
    if plan["status"] != "ready":
        violations.append(f"plan.status:{plan['status']}")
    if plan["summary"] != expected_summary:
        violations.append("plan.summary:not_real_action_counts")
    for key in sorted(forbidden & set(plan)):
        violations.append(f"plan.field:{key}")

    result = asyncio.run(
        memory_migration.apply_legacy_transition_plan(
            plan=plan,
            workspace=workspace,
            get_value=lambda _key: None,
            ingestor=_RecordingIngestor(),
            consolidate_history=lambda *_args: _consolidated_history(),
        )
    )
    expected_result_fields = {
        "status",
        "applied_actions",
        "written_keys",
        "failed_actors",
        "failed_actions",
        "fatal_failure",
        "dream_cursor_updated",
    }
    if set(result) != expected_result_fields:
        violations.append(
            "apply.fields:"
            + ",".join(sorted(set(result) - expected_result_fields))
        )
    if result["status"] not in {"complete", "partial", "failed"}:
        violations.append(f"apply.status:{result['status']}")

    migration_outcomes = MEMORY_CONTRACT["outcomes"]["migration_command"]
    if migration_outcomes["plan"] != {
        "values": ["ready"],
        "terminal": ["ready"],
    }:
        violations.append("contract.outcomes.plan")
    if MEMORY_CONTRACT["migration"]["exit_codes"]["plan"] != {"ready": 0}:
        violations.append("contract.exit_codes.plan")

    assert violations == []


async def _consolidated_history() -> str:
    return "consolidated legacy history"


def test_transition_help_describes_history_and_unread_flat_cleanup() -> None:
    graph_help = io.StringIO()
    with redirect_stdout(graph_help), pytest.raises(SystemExit) as graph_exit:
        graph_admin.build_parser().parse_args(
            ["migrate", "hybrid-storage", "--help"]
        )
    assert graph_exit.value.code == 0

    isolated_help = io.StringIO()
    with redirect_stdout(isolated_help), pytest.raises(SystemExit) as isolated_exit:
        memory_migration.cli(["--help"])
    assert isolated_exit.value.code == 0

    for help_text in (graph_help.getvalue(), isolated_help.getvalue()):
        lowered = help_text.lower()
        assert "legacy history" in lowered
        assert "private memory" in lowered
        assert "three flat memory files" in lowered
        assert "unread" in lowered
        assert "owner fallback" not in lowered
        assert "repair" not in lowered
        assert "move legacy memory" not in lowered


def test_isolated_cli_is_only_a_canonical_transition_shell(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    memory_dir = workspace / "memory"
    snapshot_root.mkdir()
    memory_dir.mkdir(parents=True)
    (target_root / ".familia-memory-migration-target.json").write_text(
        json.dumps({"target_id": "isolated-test"}),
        encoding="utf-8",
    )
    principals_path = workspace / "principals.json"
    principals_path.write_text(
        json.dumps({"principals": [{"id": "alice", "memx_key": "alice-key"}]}),
        encoding="utf-8",
    )
    target_config = _write_target_config(target_root, workspace, principals_path)

    flat_paths = (
        workspace / "USER.md",
        workspace / "MEMORY.md",
        memory_dir / "MEMORY.md",
    )
    for index, path in enumerate(flat_paths, start=1):
        path.write_text(f"legacy flat memory {index}", encoding="utf-8")
    history_path = memory_dir / "history.jsonl"
    history_before = (
        json.dumps(
            {
                "schema_version": 1,
                "cursor": 1,
                "timestamp": "2026-07-26 10:00",
                "actor": "alice",
                "content": "Prefers short answers",
                "provenance": {
                    "source": "runtime_history",
                    "idempotency_key": None,
                },
            },
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    history_path.write_bytes(history_before)
    untouched = {
        workspace / "SOUL.md": b"shared soul\n",
        workspace / "HEARTBEAT.md": b"shared heartbeat\n",
    }
    for path, value in untouched.items():
        path.write_bytes(value)

    ingestor = _RecordingIngestor()
    original_read_bytes = Path.read_bytes
    forbidden_reads = {path.resolve() for path in flat_paths}
    observed_reads: list[Path] = []

    def read_history_only(path: Path) -> bytes:
        resolved = path.resolve()
        observed_reads.append(resolved)
        if resolved in forbidden_reads:
            raise AssertionError(f"flat memory must not be read: {resolved}")
        return original_read_bytes(path)

    async def consolidate(
        _actor: str,
        _records: list[dict[str, object]],
        _existing: str,
    ) -> str:
        return "- Prefers short answers."

    canonical_build = memory_migration.build_legacy_transition_plan
    canonical_apply = memory_migration.apply_legacy_transition_plan
    built_plans: list[dict[str, object]] = []

    def record_canonical_build(**kwargs: object) -> dict[str, object]:
        plan = canonical_build(**kwargs)
        built_plans.append(plan)
        return plan

    with (
        patch(
            "scripts.compare_memory_state.load_and_validate_manifest",
            return_value={"snapshot_id": "a" * 64},
        ),
        patch.object(Path, "read_bytes", new=read_history_only),
        patch.object(memory_migration, "validate_migration_preflight"),
        patch.object(memory_migration, "_docker_verify_target"),
        patch.object(
            memory_migration,
            "build_legacy_transition_plan",
            side_effect=record_canonical_build,
        ) as build_transition,
        patch.object(
            memory_migration,
            "apply_legacy_transition_plan",
            new=AsyncMock(wraps=canonical_apply),
        ) as apply_transition,
        patch.object(
            memory_migration,
            "make_configured_history_consolidator",
            return_value=consolidate,
        ),
        patch(
            "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
            return_value=ingestor,
        ) as ingestor_type,
        patch("familia.acl.graph_io.get_raw", return_value=None) as get_value,
        patch.object(memory_migration, "_target_get_raw", return_value=get_value),
        patch("familia.acl.graph_io.resolve_admin_key", return_value="admin-key"),
        patch("familia.memx_client.memx_base_url", return_value="http://memx.test"),
    ):
        exit_code = memory_migration.cli(
            [
                "--snapshot",
                str(snapshot_root),
                "--install-root",
                str(target_root),
                "--workspace",
                str(workspace),
                "--principals",
                str(principals_path),
                "--target-config",
                str(target_config),
                "--manifest",
                str(target_root / "migration-plan.json"),
                "--journal",
                str(target_root / "migration-journal.jsonl"),
                "--apply",
                "--json",
            ]
        )

    assert exit_code == 0
    build_transition.assert_called_once()
    apply_transition.assert_awaited_once()

    build_kwargs = build_transition.call_args.kwargs
    assert build_kwargs["workspace"] == workspace
    assert build_kwargs["known_actors"] == {"alice"}
    assert "get_value" not in build_kwargs
    assert "legacy_owner" not in build_kwargs

    apply_kwargs = apply_transition.await_args.kwargs
    assert apply_kwargs["workspace"] == workspace
    assert apply_kwargs["get_value"] is get_value
    assert apply_kwargs["ingestor"] is ingestor
    assert len(built_plans) == 1
    assert apply_kwargs["plan"] is built_plans[0]
    plan = apply_kwargs["plan"]
    assert {
        action["source"]
        for action in plan["actions"]
        if action["disposition"] == "erase_without_read"
    } == {"USER.md", "MEMORY.md", "memory/MEMORY.md"}
    assert {
        action["source"]
        for action in plan["actions"]
        if action["component"] == "history"
    } == {"memory/history.jsonl"}

    ingestor_type.assert_called_once()
    ingestor_kwargs = ingestor_type.call_args.kwargs
    assert ingestor_kwargs["base_url"] == "http://memx:8000"
    assert ingestor_kwargs["api_key"] == "synthetic-key"
    assert ingestor_kwargs["principal_exists"]("alice") is True
    assert len(ingestor.calls) == 1
    assert history_path.resolve() in observed_reads
    assert forbidden_reads.isdisjoint(observed_reads)
    assert all(path.read_bytes() == b"" for path in flat_paths)
    assert history_path.read_bytes() == history_before
    assert {path: path.read_bytes() for path in untouched} == untouched


def _run_stubbed_apply_cli(
    tmp_path: Path,
    *,
    result: dict[str, object],
    json_output: bool,
) -> int:
    snapshot_root = tmp_path / "snapshot"
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    snapshot_root.mkdir()
    workspace.mkdir(parents=True)
    (target_root / ".familia-memory-migration-target.json").write_text(
        json.dumps({"target_id": "isolated-test"}),
        encoding="utf-8",
    )
    principals_path = workspace / "principals.json"
    principals_path.write_text(
        json.dumps({"principals": [{"id": "alice", "memx_key": "alice-key"}]}),
        encoding="utf-8",
    )
    target_config = _write_target_config(target_root, workspace, principals_path)
    plan = {
        "status": "ready",
        "actions": [
            {"disposition": "erase_without_read"},
            {"disposition": "erase_without_read"},
            {"disposition": "erase_without_read"},
        ],
        "summary": {"erase_without_read": 3},
    }
    argv = [
        "--snapshot",
        str(snapshot_root),
        "--install-root",
        str(target_root),
        "--workspace",
        str(workspace),
        "--principals",
        str(principals_path),
        "--target-config",
        str(target_config),
        "--manifest",
        str(target_root / "migration-plan.json"),
        "--journal",
        str(target_root / "migration-journal.jsonl"),
        "--apply",
    ]
    if json_output:
        argv.append("--json")

    with (
        patch(
            "scripts.compare_memory_state.load_and_validate_manifest",
            return_value={"snapshot_id": "a" * 64},
        ),
        patch.object(memory_migration, "validate_migration_preflight"),
        patch.object(memory_migration, "_docker_verify_target"),
        patch.object(
            memory_migration,
            "build_legacy_transition_plan",
            return_value=plan,
        ),
        patch.object(
            memory_migration,
            "apply_legacy_transition_plan",
            new=AsyncMock(return_value=result),
        ),
        patch(
            "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
            return_value=object(),
        ),
        patch("familia.acl.graph_io.get_raw", return_value=None),
    ):
        return memory_migration.cli(argv)


def test_both_migration_cli_json_boundaries_match_release_schema(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "isolated"
    workspace = target_root / "state" / "files"
    memory_dir = workspace / "memory"
    snapshot_root = tmp_path / "snapshot"
    memory_dir.mkdir(parents=True)
    snapshot_root.mkdir()
    (target_root / ".familia-memory-migration-target.json").write_text(
        json.dumps({"target_id": "isolated-test"}),
        encoding="utf-8",
    )
    principals_path = workspace / "principals.json"
    principals_path.write_text(
        json.dumps({"principals": [{"id": "alice", "memx_key": "alice-key"}]}),
        encoding="utf-8",
    )
    target_config = _write_target_config(target_root, workspace, principals_path)
    plan = memory_migration.build_legacy_transition_plan(
        workspace=workspace,
        known_actors={"alice"},
    )
    result = {
        "status": "complete",
        "applied_actions": 3,
        "written_keys": [],
        "failed_actors": [],
        "failed_actions": [],
        "fatal_failure": None,
        "dream_cursor_updated": False,
    }
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "release"
        / "memory-migration.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    def run_isolated(*, apply: bool) -> dict[str, object]:
        argv = [
            "--snapshot",
            str(snapshot_root),
            "--install-root",
            str(target_root),
            "--workspace",
            str(workspace),
            "--principals",
            str(principals_path),
            "--target-config",
            str(target_config),
            "--manifest",
            str(target_root / "migration-plan.json"),
            "--journal",
            str(target_root / "migration-journal.jsonl"),
            "--json",
        ]
        if apply:
            argv.append("--apply")
        output = io.StringIO()
        with (
            patch(
                "scripts.compare_memory_state.load_and_validate_manifest",
                return_value={"snapshot_id": "a" * 64},
            ),
            patch.object(memory_migration, "validate_migration_preflight"),
            patch.object(memory_migration, "_docker_verify_target"),
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                return_value=plan,
            ),
            patch.object(
                memory_migration,
                "apply_legacy_transition_plan",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
                return_value=object(),
            ),
            patch("familia.acl.graph_io.get_raw", return_value=None),
            patch(
                "familia.acl.graph_io.resolve_admin_key",
                return_value="admin-key",
            ),
            patch(
                "familia.memx_client.memx_base_url",
                return_value="http://memx.test",
            ),
            redirect_stdout(output),
        ):
            assert memory_migration.cli(argv) == 0
        return json.loads(output.getvalue())

    def run_graph(*, apply: bool) -> dict[str, object]:
        output = io.StringIO()
        args = SimpleNamespace(
            workspace=workspace,
            config=None,
            dry_run=not apply,
            json=True,
        )
        with (
            patch.object(
                graph_admin,
                "_load_principals_json",
                return_value=(
                    workspace / "principals.json",
                    {"principals": [{"id": "alice"}]},
                ),
            ),
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                return_value=plan,
            ),
            patch.object(
                memory_migration,
                "apply_legacy_transition_plan",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
                return_value=object(),
            ),
            patch("familia.acl.graph_io.get_raw", return_value=None),
            patch("familia.acl.graph_io.set_raw"),
            patch(
                "familia.acl.graph_io.resolve_admin_key",
                return_value="admin-key",
            ),
            patch(
                "familia.memx_client.memx_base_url",
                return_value="http://memx.test",
            ),
            patch.object(graph_admin.audit, "log_event"),
            redirect_stdout(output),
        ):
            assert graph_admin.cmd_migrate_hybrid_storage(args) == 0
        return json.loads(output.getvalue())

    documents = {
        "isolated-plan": run_isolated(apply=False),
        "isolated-apply": run_isolated(apply=True),
        "graph-plan": run_graph(apply=False),
        "graph-apply": run_graph(apply=True),
    }
    invalid = {
        name: [error.message for error in validator.iter_errors(document)]
        for name, document in documents.items()
        if not validator.is_valid(document)
    }

    assert invalid == {}


def test_graph_registry_preserves_string_ids_for_builder_validation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    history = [
        {
            "schema_version": 1,
            "cursor": 1,
            "timestamp": "2026-07-27 10:01",
            "actor": "alice",
            "content": "Факт Алисы",
            "provenance": {
                "source": "runtime_history",
                "idempotency_key": None,
            },
        },
        {
            "schema_version": 1,
            "cursor": 2,
            "timestamp": "2026-07-27 10:02",
            "actor": " alice ",
            "content": "Факт с недопустимым владельцем",
            "provenance": {
                "source": "runtime_history",
                "idempotency_key": None,
            },
        },
        {
            "schema_version": 1,
            "cursor": 3,
            "timestamp": "2026-07-27 10:03",
            "actor": "123",
            "content": "Факт числового идентификатора из реестра",
            "provenance": {
                "source": "runtime_history",
                "idempotency_key": None,
            },
        },
    ]
    (memory_dir / "history.jsonl").write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in history
        ),
        encoding="utf-8",
    )
    registry = {
        "principals": [
            {"id": "alice"},
            {"id": " alice "},
            {"id": 123},
        ]
    }
    canonical_build = memory_migration.build_legacy_transition_plan
    seen_builder_ids: list[set[object]] = []

    def record_build(**kwargs: object) -> dict[str, object]:
        known_actors = kwargs["known_actors"]
        assert isinstance(known_actors, set)
        seen_builder_ids.append(set(known_actors))
        return canonical_build(**kwargs)

    async def consolidate(
        actor: str,
        _records: list[dict[str, object]],
        _existing: str,
    ) -> str:
        return f"- Факт для {actor}."

    ingestor = _RecordingIngestor()
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "release"
        / "memory-migration.schema.json"
    )
    validator = Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8"))
    )

    def run_graph(*, dry_run: bool) -> dict[str, object]:
        output = io.StringIO()
        args = SimpleNamespace(
            workspace=workspace,
            config=None,
            dry_run=dry_run,
            json=True,
        )
        with (
            patch.object(
                graph_admin,
                "_load_principals_json",
                return_value=(workspace / "principals.json", registry),
            ),
            patch.object(
                memory_migration,
                "build_legacy_transition_plan",
                side_effect=record_build,
            ),
            patch.object(
                memory_migration,
                "make_configured_history_consolidator",
                return_value=consolidate,
            ),
            patch(
                "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
                return_value=ingestor,
            ),
            patch("familia.acl.graph_io.get_raw", return_value=None),
            patch("familia.acl.graph_io.set_raw"),
            patch(
                "familia.acl.graph_io.resolve_admin_key",
                return_value="admin-key",
            ),
            patch(
                "familia.memx_client.memx_base_url",
                return_value="http://memx.test",
            ),
            patch.object(graph_admin.audit, "log_event"),
            redirect_stdout(output),
        ):
            assert graph_admin.cmd_migrate_hybrid_storage(args) == 0
        return json.loads(output.getvalue())

    plan = run_graph(dry_run=True)
    result = run_graph(dry_run=False)
    routed_actors = {
        action["actor"]
        for action in plan["actions"]
        if action.get("disposition") == "llm_required"
    }
    discarded = [
        action
        for action in plan["actions"]
        if action.get("disposition") == "discarded_unknown"
    ]
    invalid = {
        name: [error.message for error in validator.iter_errors(document)]
        for name, document in (("plan", plan), ("result", result))
        if not validator.is_valid(document)
    }

    assert seen_builder_ids == [
        {"alice", " alice "},
        {"alice", " alice "},
    ]
    assert plan["known_actors"] == ["alice"]
    assert routed_actors == {"alice"}
    assert {
        (action["source_actor"], action["reason"])
        for action in discarded
    } == {
        (None, "history_actorless"),
        ("123", "history_actor_unknown"),
    }
    assert result["written_keys"] == [
        "private:alice:memory:legacy-history"
    ]
    assert {
        call["server_principal"]
        for call in ingestor.calls
    } == {"alice"}
    assert invalid == {}


@pytest.mark.parametrize(
    ("status", "expected_exit_code"),
    [
        ("partial", 2),
        ("failed", 1),
        ("complete", 0),
        ("unexpected", 1),
    ],
)
def test_isolated_cli_apply_exit_code_follows_result(
    tmp_path: Path,
    status: str,
    expected_exit_code: int,
) -> None:
    result = {
        "status": status,
        "applied_actions": 0,
        "failed_actors": [],
        "fatal_failure": None,
    }

    assert (
        _run_stubbed_apply_cli(tmp_path, result=result, json_output=True)
        == expected_exit_code
    )


@pytest.mark.parametrize(
    ("result", "expected_output"),
    [
        (
            {
                "status": "partial",
                "applied_actions": 2,
                "failed_actors": ["alice"],
                "fatal_failure": "history:alice",
            },
            "status=partial applied_actions=2 "
            "failed_actors=alice error=history:alice",
        ),
        (
            {
                "status": "failed",
                "applied_actions": 0,
                "failed_actors": ["alice", "bob"],
                "fatal_failure": "ingest:unavailable",
            },
            "status=failed applied_actions=0 "
            "failed_actors=alice,bob error=ingest:unavailable",
        ),
        (
            {
                "status": "failed",
                "applied_actions": 1,
                "failed_actors": ["carol"],
                "error": "explicit:failure",
                "fatal_failure": "fallback:must-not-win",
            },
            "status=failed applied_actions=1 "
            "failed_actors=carol error=explicit:failure",
        ),
    ],
)
def test_isolated_cli_plain_apply_summary_uses_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    result: dict[str, object],
    expected_output: str,
) -> None:
    _run_stubbed_apply_cli(tmp_path, result=result, json_output=False)

    assert capsys.readouterr().out.strip() == expected_output
