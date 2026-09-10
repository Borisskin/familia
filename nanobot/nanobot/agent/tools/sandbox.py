"""Sandbox backends for shell command execution.

To add a new backend, implement a function with the signature:
    _wrap_<name>(command: str, workspace: str, cwd: str) -> str
and register it in _BACKENDS below.
"""

import shlex
from pathlib import Path

from nanobot.config.paths import get_media_dir
from nanobot.security.workspace_access import current_workspace_scope


def _bwrap(command: str, workspace: str, cwd: str) -> str:
    """Wrap command in a bubblewrap sandbox (requires bwrap in container).

    Only the workspace is bind-mounted read-write; its parent dir (which holds
    config.json) is hidden behind a fresh tmpfs.  The media directory is
    bind-mounted read-only so exec commands can read uploaded attachments.
    """
    ws = Path(workspace).resolve()
    scope = current_workspace_scope()
    media = get_media_dir().resolve()

    try:
        sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws))
    except ValueError:
        sandbox_cwd = str(ws)

    required = ["/usr"]
    optional = [
        "/bin",
        "/lib",
        "/lib64",
        "/etc/alternatives",
        "/etc/ssl/certs",
        "/etc/pki/tls/certs",
        "/etc/pki/ca-trust",
        "/etc/crypto-policies",
        "/etc/resolv.conf",
        "/etc/ld.so.cache",
    ]

    args = ["bwrap", "--new-session", "--die-with-parent", "--setenv", "HOME", str(ws)]
    for p in required:
        args += ["--ro-bind", p, p]
    for p in optional:
        args += ["--ro-bind-try", p, p]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    isolated_scope = scope is not None and not scope.allow_shared_extras
    if isolated_scope:
        args.append("--unshare-pid")
    mask_root = scope.sandbox_mask_root if isolated_scope else None
    if mask_root is None:
        args += [
            "--tmpfs", str(ws.parent),        # mask config dir
            "--dir", str(ws),                 # recreate workspace mount point
        ]
    else:
        mask_root = mask_root.resolve(strict=False)
        try:
            relative_workspace = ws.relative_to(mask_root)
        except ValueError as exc:
            raise ValueError("sandbox mask root must contain the workspace") from exc
        args += ["--tmpfs", str(mask_root)]
        current = mask_root
        for part in relative_workspace.parts:
            current /= part
            args += ["--dir", str(current)]
    args += ["--bind", str(ws), str(ws)]
    # Standalone nanobot keeps its historical global-media read mount.  A
    # product adapter binds an actor-owned scope and explicitly disables that
    # shared capability; no path text or blacklist is involved here.
    if scope is None or scope.allow_shared_extras:
        args += ["--ro-bind-try", str(media), str(media)]
    args += ["--chdir", sandbox_cwd, "--", "sh", "-c", command]
    return shlex.join(args)


_BACKENDS = {"bwrap": _bwrap}


def wrap_command(sandbox: str, command: str, workspace: str, cwd: str) -> str:
    """Wrap *command* using the named sandbox backend."""
    if backend := _BACKENDS.get(sandbox):
        return backend(command, workspace, cwd)
    raise ValueError(f"Unknown sandbox backend {sandbox!r}. Available: {list(_BACKENDS)}")
