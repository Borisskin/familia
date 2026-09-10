"""Shared path helpers for workspace-scoped tools."""

import hashlib
import shutil
from pathlib import Path

from nanobot.config.paths import get_media_dir
from nanobot.security.workspace_policy import (
    is_path_within,
    resolve_allowed_path,
)
from nanobot.security.workspace_access import current_workspace_scope


def materialize_message_media(media: list[str]) -> list[str]:
    """Copy admitted channel media into the active private workspace.

    Standalone nanobot keeps its historical shared-media paths.  A product
    scope with shared extras disabled accepts only regular files physically
    below that message's channel media directory and returns private copies;
    paths from another actor, a different channel, or a symlink are dropped.
    """
    scope = current_workspace_scope()
    if scope is None or scope.allow_shared_extras:
        return list(media)

    channel = scope.source_channel
    if not isinstance(channel, str) or not channel or Path(channel).name != channel:
        return []

    shared_media = get_media_dir()
    source_root = get_media_dir(channel)
    shared_root = shared_media.resolve(strict=False)
    source_root = source_root.resolve(strict=False)
    try:
        if source_root.parent != shared_root:
            return []
    except (OSError, RuntimeError):
        return []

    private_root = scope.project_path / ".attachments" / channel
    try:
        private_root.mkdir(parents=True, exist_ok=True)
        private_root = private_root.resolve(strict=False)
        private_root.relative_to(scope.project_path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return []

    copied: list[str] = []
    seen: set[Path] = set()
    for raw_path in media:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        source = Path(raw_path).expanduser()
        if not source.is_absolute() or source in seen or source.is_symlink():
            continue
        try:
            logical_source = source.absolute()
            resolved_source = source.resolve(strict=True)
            if logical_source != resolved_source:
                continue
            resolved_source.relative_to(source_root)
            if not source.is_file():
                continue
        except (OSError, RuntimeError, ValueError):
            continue

        seen.add(source)
        name = source.name
        if not name or name in {".", ".."}:
            continue
        digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
        target = private_root / f"{digest}-{name}"
        try:
            if target.is_symlink():
                continue
            if target.exists():
                if target.resolve(strict=True) != target.absolute():
                    continue
            else:
                shutil.copyfile(source, target)
            copied.append(str(target))
        except (OSError, RuntimeError):
            continue
    return copied


def is_under(path: Path, directory: Path) -> bool:
    """Return True when path resolves under directory."""
    return is_path_within(path, directory)


def resolve_workspace_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
    extra_allowed_files: list[Path] | None = None,
    include_media_dir: bool = True,
) -> Path:
    """Resolve path against workspace and enforce allowed directory containment."""
    scope = current_workspace_scope()
    media_roots = [get_media_dir()] if (
        include_media_dir and (scope is None or scope.allow_shared_extras)
    ) else []
    extra_roots = [*media_roots, *(extra_allowed_dirs or [])] if allowed_dir else None
    return resolve_allowed_path(
        path,
        workspace=workspace,
        allowed_root=allowed_dir,
        extra_allowed_roots=extra_roots,
        extra_allowed_files=extra_allowed_files,
    )
