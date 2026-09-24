"""Familia-owned maintenance jobs and the admin disk-usage contract."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from loguru import logger
from nanobot.config.paths import get_data_dir, get_media_dir, get_workspace_path

MEDIA_TTL_SECONDS = 24 * 60 * 60
SESSIONS_TTL_SECONDS = 90 * 24 * 60 * 60
MEDIA_CLEANUP_INTERVAL_MS = 60 * 60 * 1000
SESSIONS_CLEANUP_INTERVAL_MS = 24 * 60 * 60 * 1000
WORKSPACE_GIT_GC_INTERVAL_MS = 30 * 24 * 60 * 60 * 1000
_SYSTEM_JOB_ORIGIN_KEY = "_familia_system_job"


def cleanup_media(ttl_seconds: int = MEDIA_TTL_SECONDS) -> tuple[int, int]:
    """Delete media files older than the retention period."""
    root = get_media_dir()
    if not root.exists():
        return 0, 0
    cutoff = time.time() - ttl_seconds
    deleted = 0
    freed = 0
    for path in root.rglob("*"):
        try:
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            size = path.stat().st_size
            path.unlink()
            deleted += 1
            freed += size
        except OSError as exc:
            logger.warning("Media cleanup skipped {}: {}", path, exc)
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir() and path != root:
            try:
                path.rmdir()
            except OSError:
                pass
    logger.info("Media cleanup: deleted {} files, freed {} bytes", deleted, freed)
    return deleted, freed


def cleanup_sessions(ttl_seconds: int = SESSIONS_TTL_SECONDS) -> tuple[int, int]:
    """Delete stale JSONL sessions under the current workspace only."""
    root = get_workspace_path() / "sessions"
    if not root.exists():
        return 0, 0
    cutoff = time.time() - ttl_seconds
    deleted = 0
    freed = 0
    for path in root.glob("*.jsonl"):
        try:
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            size = path.stat().st_size
            path.unlink()
            deleted += 1
            freed += size
        except OSError as exc:
            logger.warning("Session cleanup skipped {}: {}", path, exc)
    logger.info("Session cleanup: deleted {} files, freed {} bytes", deleted, freed)
    return deleted, freed


def workspace_git_gc() -> bool:
    """Run only automatic garbage collection in the configured workspace."""
    workspace = get_workspace_path()
    if not (workspace / ".git").is_dir():
        return False
    try:
        subprocess.run(
            ["git", "-C", str(workspace), "gc", "--auto"],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Workspace git gc skipped: {}", exc)
        return False
    logger.info("Workspace git gc completed")
    return True


def disk_usage_report() -> dict[str, Any]:
    """Return the stable report consumed by the admin Maintenance page."""
    data_dir = get_data_dir()
    workspace = get_workspace_path()
    audit_file = (
        os.environ.get("NANOBOT_AUDIT_FILE")
        or os.environ.get("FAMILIA_AUDIT_FILE")
        or str(data_dir / "audit.jsonl")
    )
    categories = (
        ("media", get_media_dir()),
        ("sessions", workspace / "sessions"),
        ("memory", workspace / "memory"),
        ("workspace_git", workspace / ".git"),
        ("audit", Path(audit_file)),
        ("cron", workspace / "cron"),
        ("logs", data_dir / "logs"),
    )
    result = []
    for name, path in categories:
        size, files = _path_size(path)
        result.append({"name": name, "path": str(path), "bytes": size, "files": files})
    try:
        usage = shutil.disk_usage("/")
        vm = {"path": "/", "free_bytes": usage.free, "total_bytes": usage.total}
    except OSError:
        vm = {"path": "/", "free_bytes": 0, "total_bytes": 0}
    return {"schema_version": 1, "categories": result, "vm": vm}


def _path_size(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    try:
        if path.is_file():
            return path.stat().st_size, 1
        total = 0
        files = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
                    files += 1
            except OSError:
                continue
        return total, files
    except OSError:
        return 0, 0


def register_system_jobs(cron: Any) -> None:
    """Reconcile Familia maintenance jobs through Nanobot's system-job seam."""
    from nanobot.cron.types import CronJob, CronPayload, CronSchedule

    for job_id, interval_ms in (
        ("media_cleanup", MEDIA_CLEANUP_INTERVAL_MS),
        ("sessions_cleanup", SESSIONS_CLEANUP_INTERVAL_MS),
        ("workspace_git_gc", WORKSPACE_GIT_GC_INTERVAL_MS),
    ):
        cron.register_system_job(
            CronJob(
                id=job_id,
                name=job_id,
                schedule=CronSchedule(kind="every", every_ms=interval_ms),
                payload=CronPayload(
                    kind="system_event",
                    origin_metadata={_SYSTEM_JOB_ORIGIN_KEY: job_id},
                ),
            )
        )


__all__ = [
    "cleanup_media",
    "cleanup_sessions",
    "disk_usage_report",
    "register_system_jobs",
    "workspace_git_gc",
]
