"""Prepare Familia's session root before nanobot creates a SessionManager."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .session_migration import (
    MigrationPlan,
    MigrationResult,
    SOURCE_SNAPSHOT_DIR,
    analyze_sessions,
    apply_migration,
)

_WORKSPACE_STATE_DIR = ".nanobot"
_WORKSPACE_ID_FILE = "workspace-id"
_WORKSPACE_ID_LENGTH = 32
_ROOT_CLAIM_FILE = ".familia-session-root"


@dataclass(frozen=True)
class PreparedSessionStorage:
    """Resolved parent passed as ``sessions_root`` to nanobot's core."""

    sessions_root: Path
    workspace_id: str
    migrations: tuple[MigrationResult, ...] = ()


def _config_path(config: Any, name: str) -> Path:
    value = getattr(config, name, None)
    if value is None:
        raise ValueError(f"Familia session preparation requires config.{name}")
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid config.{name}") from exc


def _workspace_id_marker(workspace: Path) -> Path:
    return workspace / _WORKSPACE_STATE_DIR / _WORKSPACE_ID_FILE


def _valid_workspace_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == _WORKSPACE_ID_LENGTH and all(
        char in "0123456789abcdef" for char in value
    )


def _read_workspace_id(marker: Path) -> str:
    if marker.is_symlink() or not marker.is_file():
        raise ValueError(f"invalid workspace identity marker: {marker}")
    value = marker.read_text(encoding="utf-8").strip()
    if not _valid_workspace_id(value):
        raise ValueError(f"invalid workspace identity marker: {marker}")
    return value


def _write_workspace_id(marker: Path, value: str) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{value}\n", encoding="utf-8")


def _same_workspace(recorded: str, workspace: Path) -> bool:
    candidate = Path(recorded).expanduser().resolve(strict=False)
    if candidate == workspace:
        return True
    try:
        return candidate.exists() and candidate.samefile(workspace)
    except OSError:
        return False


def _root_claim_marker(sessions_root: Path) -> Path:
    return sessions_root.parent / _ROOT_CLAIM_FILE


def _read_root_claim(marker: Path) -> Path:
    if marker.is_symlink() or not marker.is_file():
        raise ValueError(f"invalid session root claim: {marker}")
    value = marker.read_text(encoding="utf-8").strip()
    recorded = Path(value).expanduser()
    if not value or not recorded.is_absolute():
        raise ValueError(f"invalid session root claim: {marker}")
    return recorded.resolve(strict=False)


def _write_root_claim(marker: Path, root: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    pending = marker.with_name(marker.name + ".tmp")
    pending.write_text(f"{root}\n", encoding="utf-8")
    pending.replace(marker)


def _namespace_root(sessions_root: Path, workspace: Path) -> tuple[str, Path]:
    sessions_root.mkdir(parents=True, exist_ok=True)
    root_marker = _root_claim_marker(sessions_root)
    if root_marker.is_symlink():
        raise ValueError(f"invalid session root claim: {root_marker}")
    root_claim = _read_root_claim(root_marker) if root_marker.exists() else None
    marker = _workspace_id_marker(workspace)
    if marker.exists() or marker.is_symlink():
        workspace_id = _read_workspace_id(marker)
    else:
        workspace_id = ""
        matches: list[str] = []
        for candidate in sessions_root.iterdir():
            if not _valid_workspace_id(candidate.name) or not candidate.is_dir():
                continue
            namespace_marker = candidate / ".workspace"
            if namespace_marker.is_symlink() or not namespace_marker.is_file():
                continue
            try:
                if _same_workspace(
                    namespace_marker.read_text(encoding="utf-8").strip(),
                    workspace,
                ):
                    matches.append(candidate.name)
            except (OSError, UnicodeError):
                continue
        if len(matches) > 1:
            raise ValueError(f"multiple session namespaces claim {workspace}")
        if matches:
            workspace_id = matches[0]
            _write_workspace_id(marker, workspace_id)
        else:
            workspace_id = secrets.token_hex(16)
            _write_workspace_id(marker, workspace_id)

    namespace = sessions_root / workspace_id
    namespace_marker = namespace / ".workspace"
    if not namespace.exists():
        namespace.mkdir(parents=True)
        namespace_marker.write_text(f"{workspace}\n", encoding="utf-8")
        return workspace_id, namespace
    if namespace.is_symlink() or not namespace.is_dir():
        raise ValueError(f"unsafe session namespace: {namespace}")
    if namespace_marker.is_symlink():
        raise ValueError(f"unsafe session namespace marker: {namespace_marker}")
    if not namespace_marker.exists():
        if any(namespace.iterdir()):
            raise ValueError(f"session namespace has data but no marker: {namespace}")
        namespace_marker.write_text(f"{workspace}\n", encoding="utf-8")
        return workspace_id, namespace
    recorded = namespace_marker.read_text(encoding="utf-8").strip()
    if _same_workspace(recorded, workspace):
        namespace_marker.write_text(f"{workspace}\n", encoding="utf-8")
        return workspace_id, namespace

    # A root claim from a different data directory proves this is a restored
    # installation. Keep the original service identity and only rebind its
    # workspace marker; the core will then use the same namespace.
    current_root = sessions_root.parent.resolve(strict=False)
    if root_claim is not None and root_claim != current_root:
        namespace_marker.write_text(f"{workspace}\n", encoding="utf-8")
        return workspace_id, namespace

    # Without a proven restored root, an existing workspace path means this is
    # a shared-root collision. Never copy history from the other installation.
    raise ValueError(
        "shared-root collision: session namespace belongs to another existing workspace"
    )


def _legacy_roots(workspace: Path, sessions_root: Path) -> list[Path]:
    roots: list[Path] = []
    workspace_sessions = workspace / "sessions"
    if (
        workspace_sessions != sessions_root
        and workspace_sessions.is_dir()
        and not workspace_sessions.is_symlink()
        and any(
            path.is_file() and not path.is_symlink() and path.suffix == ".jsonl"
            for path in workspace_sessions.rglob("*.jsonl")
        )
    ):
        roots.append(workspace_sessions)
    if sessions_root.is_dir():
        # The parent also contains the active workspace namespaces. Feed only
        # its flat legacy files to the recursive migration, never a namespace.
        roots.extend(
            path
            for path in sorted(sessions_root.glob("*.jsonl"))
            if path.is_file() and not path.is_symlink()
        )
    return roots


def _archive_workspace_sessions(source_root: Path, data_dir: Path, workspace_id: str) -> None:
    if source_root.is_symlink() or not source_root.is_dir():
        return
    archive_parent = data_dir / "source-snapshot" / "workspace-sessions"
    archive_parent.mkdir(parents=True, exist_ok=True)
    if archive_parent.is_symlink() or not archive_parent.is_dir():
        raise ValueError(f"unsafe legacy session archive: {archive_parent}")
    archive = archive_parent / workspace_id
    if archive.exists() or archive.is_symlink():
        raise ValueError(f"legacy session archive already exists: {archive}")
    source_root.rename(archive)


def _merge_migration_plans(
    plans: list[MigrationPlan],
    *,
    workspace_sessions: Path,
    sessions_root: Path,
) -> MigrationPlan | None:
    if not plans:
        return None

    def is_workspace_source(plan: MigrationPlan) -> bool:
        # ``analyze_sessions(file)`` keeps the file as source_root. Only a
        # directory source represents workspace history; flat files belong to
        # the data/sessions parent even when their names match.
        return plan.source_root.is_dir() and plan.source_root != sessions_root

    def is_flat_source(plan: MigrationPlan) -> bool:
        return plan.source_root == sessions_root or (
            plan.source_root.is_file() and plan.source_root.parent == sessions_root
        )

    workspace_plans = [plan for plan in plans if is_workspace_source(plan)]
    has_workspace_source = bool(workspace_plans)
    has_flat_source = any(is_flat_source(plan) for plan in plans)
    files = []
    issues: list[dict[str, object]] = []
    for plan in plans:
        prefix = (
            "workspace-sessions"
            if has_workspace_source and has_flat_source and is_workspace_source(plan)
            else None
        )
        for item in plan.files:
            if prefix is None:
                files.append(item)
                continue
            relative_path = (Path(prefix) / item.relative_path).as_posix()
            files.append(replace(item, relative_path=relative_path))
        for issue in plan.issues:
            if prefix is None or not isinstance(issue.get("source"), str):
                issues.append(issue)
            else:
                issues.append(
                    {
                        **issue,
                        "source": (Path(prefix) / str(issue["source"])).as_posix(),
                    }
                )
    known_actors = tuple(sorted({actor for plan in plans for actor in plan.known_actors}))
    source_root = workspace_plans[0].source_root if has_workspace_source else sessions_root
    return MigrationPlan(
        source_root=source_root,
        known_actors=known_actors,
        files=files,
        issues=issues,
    )


def _plan_needs_replay(
    plan: MigrationPlan,
    data_dir: Path,
    workspace_id: str,
) -> bool:
    """Replay a source only when an earlier apply left output missing or stale."""
    source_digests = {item.relative_path: item.digest for item in plan.files}
    targets = plan.to_dict(target_namespace=workspace_id)["targets"]
    for target in targets:
        path = data_dir / str(target["path"])
        if path.is_symlink() or not path.is_file():
            return True
        expected_digest = source_digests.get(str(target["source"]))
        if expected_digest is None:
            return True
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
            metadata_line = json.loads(first_line)
            metadata = metadata_line.get("metadata", {})
        except (OSError, IndexError, UnicodeError, json.JSONDecodeError, AttributeError):
            return True
        if not isinstance(metadata, dict) or metadata.get("familia_source_sha256") != expected_digest:
            return True
    for item in plan.files:
        if not item.quarantine:
            continue
        path = data_dir / "quarantine" / f"{item.relative_path}.jsonl"
        if path.is_symlink() or not path.is_file():
            return True
    return not plan.files


def prepare_familia_session_storage(config: Any) -> PreparedSessionStorage:
    """Migrate legacy files and return the parent ``<data>/sessions`` root.

    ``config.runtime_data_dir`` is intentionally required: falling back to the
    process working directory can bind a restored history to the wrong install.
    """
    data_dir = _config_path(config, "runtime_data_dir")
    workspace = _config_path(config, "workspace_path")
    sessions_root = data_dir / "sessions"
    if sessions_root == workspace or sessions_root.is_relative_to(workspace):
        raise ValueError(
            "session storage must be outside the agent workspace; "
            "move --config outside --workspace or choose a nested workspace directory"
        )
    if sessions_root.is_symlink() or (
        sessions_root.exists() and not sessions_root.is_dir()
    ):
        raise ValueError(f"unsafe session root: {sessions_root}")
    root_marker = _root_claim_marker(sessions_root)
    if root_marker.is_symlink():
        raise ValueError(f"invalid session root claim: {root_marker}")
    if not root_marker.exists():
        # Establish ownership before creating any namespace. A copied root
        # therefore cannot be mistaken for a shared-root collision later.
        _write_root_claim(root_marker, data_dir)
    workspace_id, _namespace = _namespace_root(sessions_root, workspace)

    legacy_roots = _legacy_roots(workspace, sessions_root)
    workspace_sessions = workspace / "sessions"
    active_workspace_source = workspace_sessions if workspace_sessions in legacy_roots else None
    archived_workspace: Path | None = None
    if active_workspace_source is None:
        archived_workspace = (
            data_dir / SOURCE_SNAPSHOT_DIR / "workspace-sessions" / workspace_id
        )
        if (
            archived_workspace.is_dir()
            and not archived_workspace.is_symlink()
            and any(archived_workspace.rglob("*.jsonl"))
        ):
            legacy_roots.insert(0, archived_workspace)

    plans: list[MigrationPlan] = []
    for source_root in legacy_roots:
        candidate = analyze_sessions(source_root)
        if not _plan_needs_replay(candidate, data_dir, workspace_id):
            continue
        plans.append(candidate)
    plan = _merge_migration_plans(
        plans,
        workspace_sessions=workspace_sessions,
        sessions_root=sessions_root,
    )
    migrations: list[MigrationResult] = []
    if plan is not None:
        migration = apply_migration(
            plan,
            data_dir,
            target_namespace=workspace_id,
        )
        migrations.append(migration)
        if active_workspace_source is not None:
            if migration.source_changed:
                raise OSError("legacy session source changed during migration")
            _archive_workspace_sessions(active_workspace_source, data_dir, workspace_id)
    _write_root_claim(root_marker, data_dir)
    return PreparedSessionStorage(
        sessions_root=sessions_root,
        workspace_id=workspace_id,
        migrations=tuple(migrations),
    )


__all__ = ["PreparedSessionStorage", "prepare_familia_session_storage"]
