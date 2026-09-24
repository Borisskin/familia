from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from familia.nanobot_extension import maintenance, runtime_services


def test_disk_usage_report_keeps_admin_schema_and_current_paths(tmp_path, monkeypatch):
    data = tmp_path / "data"
    workspace = tmp_path / "workspace"
    media = data / "media"
    sessions = workspace / "sessions"
    memory = workspace / "memory"
    git = workspace / ".git"
    cron = workspace / "cron"
    logs = data / "logs"
    audit = data / "audit.jsonl"
    for path in (media, sessions, memory, git, cron, logs):
        path.mkdir(parents=True)
    (media / "one.bin").write_bytes(b"123")
    audit.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(maintenance, "get_data_dir", lambda: data)
    monkeypatch.setattr(maintenance, "get_media_dir", lambda: media)
    monkeypatch.setattr(maintenance, "get_workspace_path", lambda: workspace)
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(audit))
    monkeypatch.delenv("NANOBOT_AUDIT_FILE", raising=False)

    report = maintenance.disk_usage_report()

    assert report["schema_version"] == 1
    assert {item["name"] for item in report["categories"]} == {
        "media",
        "sessions",
        "memory",
        "workspace_git",
        "audit",
        "cron",
        "logs",
    }
    assert all({"name", "path", "bytes", "files"} <= set(item) for item in report["categories"])
    assert {"path", "free_bytes", "total_bytes"} <= set(report["vm"])


def test_cleanup_media_and_sessions_respect_retention_and_scope(tmp_path, monkeypatch):
    media = tmp_path / "media"
    workspace = tmp_path / "workspace"
    sessions = workspace / "sessions"
    media.mkdir(parents=True)
    sessions.mkdir(parents=True)
    old_media = media / "old.bin"
    new_media = media / "new.bin"
    old_media.write_bytes(b"old")
    new_media.write_bytes(b"new")
    old_session = sessions / "old.jsonl"
    new_session = sessions / "new.jsonl"
    ignored = sessions / "old.txt"
    old_session.write_bytes(b"old")
    new_session.write_bytes(b"new")
    ignored.write_bytes(b"keep")
    old_time = time.time() - 10
    os.utime(old_media, (old_time, old_time))
    os.utime(old_session, (old_time, old_time))
    monkeypatch.setattr(maintenance, "get_media_dir", lambda: media)
    monkeypatch.setattr(maintenance, "get_workspace_path", lambda: workspace)

    assert maintenance.cleanup_media(ttl_seconds=5) == (1, 3)
    assert maintenance.cleanup_sessions(ttl_seconds=5) == (1, 3)
    assert not old_media.exists()
    assert new_media.exists()
    assert not old_session.exists()
    assert new_session.exists()
    assert ignored.exists()


def test_workspace_git_gc_is_confined_to_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    monkeypatch.setattr(maintenance, "get_workspace_path", lambda: workspace)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)

    assert maintenance.workspace_git_gc() is True
    assert calls[0][0] == ["git", "-C", str(workspace), "gc", "--auto"]
    assert calls[0][1]["timeout"] == 120


def test_maintenance_jobs_are_registered_with_stable_identity():
    jobs = []

    class Cron:
        def register_system_job(self, job):
            jobs.append(job)

    hooks = runtime_services.make_runtime_service_hooks()
    hooks["register_system_jobs"](Cron())

    assert [(job.id, job.name, job.schedule.every_ms) for job in jobs] == [
        ("media_cleanup", "media_cleanup", 60 * 60 * 1000),
        ("sessions_cleanup", "sessions_cleanup", 24 * 60 * 60 * 1000),
        ("workspace_git_gc", "workspace_git_gc", 30 * 24 * 60 * 60 * 1000),
    ]
    assert [job.payload.origin_metadata for job in jobs] == [
        {"_familia_system_job": job.id} for job in jobs
    ]


def test_scheduled_maintenance_job_routes_to_familia_function(monkeypatch):
    jobs = []

    class Cron:
        def register_system_job(self, job):
            jobs.append(job)

    runtime_services.make_runtime_service_hooks()["register_system_jobs"](Cron())
    calls = []
    monkeypatch.setattr(maintenance, "cleanup_media", lambda: calls.append("media"))

    asyncio.run(
        runtime_services.run_scheduled(
            jobs[0],
            SimpleNamespace(cron_service=SimpleNamespace()),
        )
    )

    assert calls == ["media"]


def test_runtime_factory_keeps_maintenance_registration_seam(tmp_path):
    from familia import bootstrap
    from nanobot.bus.queue import MessageBus

    config = SimpleNamespace(
        workspace_path=tmp_path / "workspace",
        runtime_data_dir=tmp_path / "runtime",
    )
    adapters = bootstrap.make_runtime_adapters(config, MessageBus())

    assert callable(adapters.register_system_jobs)


def test_reconstruction_contract_keeps_maintenance_cron_link():
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "patches" / "ownership.yaml").read_text(encoding="utf-8"))
    rows = {row["path"]: row for row in manifest["deltas"]}

    for path in (
        "nanobot/nanobot/cli/gateway_runtime.py",
        "nanobot/nanobot/runtime_adapters.py",
    ):
        assert rows[path]["category"] == "familia-invariant"
        assert "maintenance" in rows[path]["verification"]
    assert "register_system_jobs" in (
        root / "patches" / "cli_gateway_runtime.patch"
    ).read_text(encoding="utf-8")
    assert "register_system_jobs" in (
        root / "patches" / "runtime_adapters.patch"
    ).read_text(encoding="utf-8")
