#!/usr/bin/env python3
"""Capture a command as an atomic, byte-exact RP-020 evidence bundle.

The module is deliberately standard-library-only and import-safe.  Runtime work
is performed only by :func:`main` after every path and identifier is validated.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import errno
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Sequence


CONTRACT_VERSION = "rp020-evidence-v1"
USAGE_EXIT = 64
COLLECTOR_FAILURE_EXIT = 125
COMMAND_NOT_FOUND_EXIT = 127
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
SENSITIVE_NAME_RE = re.compile(
    r"(?:API[_-]?KEY|AUTH|BEARER|COOKIE|CREDENTIAL|PASS(?:WORD)?|PRIVATE[_-]?KEY|SECRET|SESSION|TOKEN)",
    re.IGNORECASE,
)
SECRET_OPTION_RE = re.compile(
    r"^--?(?:api[-_]?key|auth|bearer|cookie|credential|pass(?:word)?|private[-_]?key|secret|session|token)(?:=|\Z)",
    re.IGNORECASE,
)
FIXED_ENTRIES = (
    "command.json",
    "environment.json",
    "hashes.json",
    "manifest.json",
    "source.json",
    "stderr.bin",
    "stdout.bin",
)
OPTIONAL_ENTRIES = ("dependency-install.bin", "failure.json")
SECRET_OUTPUT_DIAGNOSTIC = b"collector failure: secret-bearing target output rejected\n"
RESERVED_IDENTIFIERS = {
    *(name.casefold() for name in FIXED_ENTRIES),
    *(name.casefold() for name in OPTIONAL_ENTRIES),
    "incomplete",
}
BASE_ALLOWED_ENV = (
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONPATH",
    "SOURCE_DATE_EPOCH",
    "TZ",
    "VIRTUAL_ENV",
)
RUNTIME_DIR_NAMES = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
}
RUNTIME_FILE_NAMES = {".coverage"}
RUNTIME_FILE_SUFFIXES = (".pyc", ".pyo")


class ValidationError(Exception):
    """The request is unsafe or outside the evidence contract."""


class CaptureError(Exception):
    """The collector could not complete an evidence transaction."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return (text + "\n").encode("ascii")


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CaptureError("artifact is not an exclusive regular file")


def _write_json(path: Path, value: Any) -> None:
    _write_exclusive(path, _canonical_json(value))


def _validate_identifier(value: str) -> str:
    if not value or unicodedata.normalize("NFKC", value) != value:
        raise ValidationError("run identifier is invalid")
    if not RUN_ID_RE.fullmatch(value):
        raise ValidationError("run identifier is invalid")
    if value in {".", ".."} or value.casefold() in RESERVED_IDENTIFIERS:
        raise ValidationError("run identifier is reserved")
    return value


def _contains_path(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ValidationError("symlink path component is forbidden")


def _validate_existing_directory(raw: str, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise ValidationError(f"{label} must be an absolute normalized path")
    _assert_no_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} does not exist") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValidationError(f"{label} is not a directory")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ValidationError(f"{label} has ambiguous resolution")
    return resolved


def _validate_runtime_paths(namespace: argparse.Namespace) -> dict[str, Path]:
    repo = _validate_existing_directory(namespace.repo_root, "repository root")
    roots = {
        "evidence": _validate_existing_directory(namespace.evidence_root, "evidence root"),
        "venv": _validate_existing_directory(namespace.venv_root, "venv root"),
        "home": _validate_existing_directory(namespace.home_root, "HOME root"),
        "tmp": _validate_existing_directory(namespace.tmp_root, "TMP root"),
        "cache": _validate_existing_directory(namespace.cache_root, "cache root"),
        "pip_cache": _validate_existing_directory(namespace.pip_cache_root, "pip cache root"),
    }
    for label, root in roots.items():
        if _contains_path(repo, root):
            raise ValidationError(f"{label} root may not be inside the checkout")
    evidence = roots["evidence"]
    for label, root in roots.items():
        if label == "evidence":
            continue
        if _contains_path(root, evidence) or _contains_path(evidence, root):
            raise ValidationError("evidence and runtime roots must be disjoint")
    return {"repo": repo, **roots}


def _validate_relative_input(repo: Path, raw: str) -> tuple[str, Path]:
    relative = Path(raw)
    if not raw or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValidationError("input path must be a normalized repository-relative path")
    if os.path.normpath(raw) != raw or "\\" in raw:
        raise ValidationError("input path must be a normalized repository-relative path")
    path = repo / relative
    _assert_no_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValidationError("declared input does not exist") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValidationError("declared input must be a single-link regular file")
    resolved = path.resolve(strict=True)
    if not _contains_path(repo, resolved):
        raise ValidationError("declared input escapes the checkout")
    return relative.as_posix(), resolved


def _sanitize_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    if not parts.scheme or not parts.netloc:
        return value
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parts.port}" if parts.port is not None else ""
    netloc = hostname + port
    query = []
    for key, item in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "<redacted>" if SENSITIVE_NAME_RE.search(key) else item))
    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, urllib.parse.urlencode(query), "")
    )


def _validate_command_argv(argv: Sequence[str]) -> list[str]:
    if not argv:
        raise ValidationError("target argv is required")
    result = list(argv)
    for item in result:
        if "\x00" in item:
            raise ValidationError("target argv contains a NUL byte")
        if SECRET_OPTION_RE.match(item):
            raise ValidationError("secret-bearing command argument is forbidden")
        if "://" in item:
            parsed = urllib.parse.urlsplit(item)
            if parsed.username or parsed.password:
                raise ValidationError("credential-bearing URL argument is forbidden")
            if any(SENSITIVE_NAME_RE.search(key) for key, _ in urllib.parse.parse_qsl(parsed.query)):
                raise ValidationError("credential-bearing URL argument is forbidden")
    return result


def _read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.lower()] = value.strip().strip('"')
    return values


def _require_wsl_python() -> tuple[dict[str, str], str]:
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise ValidationError("final capture requires Linux Python 3.12")
    release = platform.release()
    os_release = _read_os_release()
    if "microsoft" not in release.lower() or "wsl2" not in release.lower():
        raise ValidationError("final capture requires WSL2")
    if os_release.get("id") != "ubuntu":
        raise ValidationError("final capture requires Ubuntu")
    return os_release, release


def _safe_child_environment(
    namespace: argparse.Namespace,
    roots: dict[str, Path],
) -> tuple[dict[str, str], list[str], dict[str, str], list[str]]:
    allowlist = list(BASE_ALLOWED_ENV)
    for name in namespace.allow_env:
        if not ENV_NAME_RE.fullmatch(name) or SENSITIVE_NAME_RE.search(name):
            raise ValidationError("requested environment variable is not safe to allow")
        if name not in allowlist:
            allowlist.append(name)

    environment: dict[str, str] = {}
    for name in allowlist:
        if name in os.environ:
            value = os.environ[name]
            environment[name] = _sanitize_url(value) if "://" in value else value
    environment.update(
        {
            "HOME": str(roots["home"]),
            "TMPDIR": str(roots["tmp"]),
            "XDG_CACHE_HOME": str(roots["cache"]),
            "PIP_CACHE_DIR": str(roots["pip_cache"]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(roots["cache"] / "pycache"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "TZ": "UTC",
        }
    )
    environment.setdefault("PATH", "/usr/bin:/bin")
    environment.setdefault("LANG", "C.UTF-8")
    environment.setdefault("LC_ALL", "C.UTF-8")
    pycache = roots["cache"] / "pycache"
    if pycache.exists():
        _assert_no_symlink_components(pycache)
        if not pycache.is_dir():
            raise ValidationError("external pycache path is not a directory")
    else:
        pycache.mkdir(mode=0o700)

    excluded: dict[str, str] = {}
    secret_values: list[str] = []
    for name, value in os.environ.items():
        if SENSITIVE_NAME_RE.search(name):
            excluded[name] = "<redacted-present>"
            if value:
                secret_values.append(value)
    return environment, sorted(set(allowlist) | set(environment)), excluded, secret_values


def _run_bytes(argv: Sequence[str], *, cwd: Path | None, env: dict[str, str]) -> bytes:
    process = subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )
    if process.returncode != 0:
        raise CaptureError("required identity command failed")
    return process.stdout


def _git(repo: Path, env: dict[str, str], *args: str, accepted: Iterable[int] = (0,)) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )
    if process.returncode not in set(accepted):
        raise CaptureError("repository identity command failed")
    return process


def _decode_nul_paths(data: bytes) -> list[str]:
    return sorted(
        item.decode("utf-8", "surrogateescape")
        for item in data.split(b"\0")
        if item
    )


def _file_identity(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    common = {"mode": stat.S_IMODE(info.st_mode)}
    if stat.S_ISLNK(info.st_mode):
        data = os.readlink(path).encode("utf-8", "surrogateescape")
        return {**common, "kind": "symlink", "bytes": len(data), "sha256": _sha256_bytes(data)}
    if stat.S_ISREG(info.st_mode):
        return {
            **common,
            "kind": "file",
            "bytes": info.st_size,
            "sha256": _sha256_file(path),
        }
    if stat.S_ISDIR(info.st_mode):
        return {**common, "kind": "directory"}
    return {**common, "kind": "other"}


def _git_metadata(repo: Path, env: dict[str, str]) -> dict[str, Any]:
    git_dir_raw = _git(repo, env, "rev-parse", "--absolute-git-dir").stdout.decode(
        "utf-8", "surrogateescape"
    ).strip()
    git_dir = Path(git_dir_raw)
    entries: dict[str, Any] = {}
    candidates = ["HEAD", "index", "config", "packed-refs"]
    head = git_dir / "HEAD"
    if head.is_file() and not head.is_symlink():
        line = head.read_text(encoding="utf-8", errors="surrogateescape").strip()
        if line.startswith("ref: "):
            candidates.append(line[5:])
    for relative in sorted(set(candidates)):
        path = git_dir / relative
        entries[relative] = _file_identity(path)
    return {"git_dir": str(git_dir), "control_files": entries}


def _runtime_artifacts(repo: Path) -> dict[str, Any]:
    found: dict[str, Any] = {}
    ignored_walk = {".git", ".code-index", "node_modules"}
    for current_raw, directories, files in os.walk(repo, followlinks=False):
        current = Path(current_raw)
        directories[:] = [name for name in directories if name not in ignored_walk]
        for name in list(directories):
            if name in RUNTIME_DIR_NAMES or name.endswith(".egg-info"):
                path = current / name
                relative = path.relative_to(repo).as_posix()
                info = path.lstat()
                found[relative] = {
                    "kind": "directory",
                    "mode": stat.S_IMODE(info.st_mode),
                    "mtime_ns": info.st_mtime_ns,
                }
        for name in files:
            if name not in RUNTIME_FILE_NAMES and not name.endswith(RUNTIME_FILE_SUFFIXES):
                continue
            path = current / name
            if path.is_symlink():
                continue
            info = path.lstat()
            relative = path.relative_to(repo).as_posix()
            found[relative] = {
                "kind": "file",
                "mode": stat.S_IMODE(info.st_mode),
                "bytes": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }
    return dict(sorted(found.items()))


def _source_snapshot(repo: Path, env: dict[str, str], input_names: Sequence[str]) -> dict[str, Any]:
    commit = _git(repo, env, "rev-parse", "HEAD").stdout.decode("ascii").strip()
    branch_process = _git(repo, env, "symbolic-ref", "--quiet", "--short", "HEAD", accepted=(0, 1))
    branch = (
        branch_process.stdout.decode("utf-8", "surrogateescape").strip()
        if branch_process.returncode == 0
        else None
    )
    status = _git(repo, env, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    ignored = _git(
        repo,
        env,
        "status",
        "--porcelain=v1",
        "-z",
        "--ignored=matching",
        "--untracked-files=all",
    ).stdout
    tracked_paths = _decode_nul_paths(_git(repo, env, "ls-files", "-z").stdout)
    inventory = {name: _file_identity(repo / name) for name in tracked_paths}
    declared_input_inventory = {
        name: _file_identity(repo / name) for name in sorted(set(input_names))
    }
    staged_dirty = _decode_nul_paths(_git(repo, env, "diff", "--cached", "--name-only", "-z").stdout)
    unstaged_dirty = _decode_nul_paths(_git(repo, env, "diff", "--name-only", "-z").stdout)
    dirty_paths = sorted(set(staged_dirty) | set(unstaged_dirty))
    dirty_manifest = {name: inventory.get(name, _file_identity(repo / name)) for name in dirty_paths}
    return {
        "commit": commit,
        "branch": branch,
        "detached": branch is None,
        "status_porcelain_v1_z_base64": base64.b64encode(status).decode("ascii"),
        "status_sha256": _sha256_bytes(status),
        "ignored_declaration_v1_z_base64": base64.b64encode(ignored).decode("ascii"),
        "ignored_declaration_sha256": _sha256_bytes(ignored),
        "tracked_dirty_manifest": dirty_manifest,
        "tracked_inventory": inventory,
        "tracked_inventory_sha256": _sha256_bytes(_canonical_json(inventory)),
        "declared_inputs": list(input_names),
        "declared_input_inventory": declared_input_inventory,
        "declared_input_inventory_sha256": _sha256_bytes(
            _canonical_json(declared_input_inventory)
        ),
        "git_metadata": _git_metadata(repo, env),
        "runtime_artifacts": _runtime_artifacts(repo),
    }


def _snapshot_changed_paths(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed: set[str] = set()
    before_inventory = before["tracked_inventory"]
    after_inventory = after["tracked_inventory"]
    for path in set(before_inventory) | set(after_inventory):
        if before_inventory.get(path) != after_inventory.get(path):
            changed.add(path)
    before_inputs = before["declared_input_inventory"]
    after_inputs = after["declared_input_inventory"]
    for path in set(before_inputs) | set(after_inputs):
        if before_inputs.get(path) != after_inputs.get(path):
            changed.add(path)
    before_runtime = before["runtime_artifacts"]
    after_runtime = after["runtime_artifacts"]
    for path in set(before_runtime) | set(after_runtime):
        if before_runtime.get(path) != after_runtime.get(path):
            changed.add(path)
    if before["status_sha256"] != after["status_sha256"]:
        before_dirty = set(before["tracked_dirty_manifest"])
        after_dirty = set(after["tracked_dirty_manifest"])
        changed.update(before_dirty ^ after_dirty)
        if not changed:
            changed.add("<checkout-status>")
    if before["ignored_declaration_sha256"] != after["ignored_declaration_sha256"]:
        changed.add("<ignored-state>")
    if before["git_metadata"] != after["git_metadata"]:
        changed.add("<git-metadata>")
    if before["commit"] != after["commit"] or before["branch"] != after["branch"]:
        changed.add("<git-head>")
    return sorted(changed)


def _apt_configuration() -> tuple[list[dict[str, Any]], str]:
    root = Path("/etc/apt")
    records: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            records.append(
                {"path": str(path), "bytes": path.stat().st_size, "sha256": _sha256_file(path)}
            )
    return records, _sha256_bytes(_canonical_json(records))


def _distribution_inventory() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name") or "<unknown>"
        version = distribution.version
        origin = str(Path(distribution.locate_file("")).resolve())
        direct_url: Any = None
        direct_url_text = distribution.read_text("direct_url.json")
        if direct_url_text:
            try:
                direct_url = json.loads(direct_url_text)
            except json.JSONDecodeError:
                direct_url = {"invalid": True}
            if isinstance(direct_url, dict) and isinstance(direct_url.get("url"), str):
                direct_url["url"] = _sanitize_url(direct_url["url"])
        file_hashes: list[dict[str, str]] = []
        for item in distribution.files or ():
            if item.hash is not None:
                file_hashes.append(
                    {"path": str(item), "mode": item.hash.mode, "value": item.hash.value}
                )
        file_hashes.sort(key=lambda item: item["path"])
        records.append(
            {
                "name": name,
                "normalized_name": re.sub(r"[-_.]+", "-", name).lower(),
                "version": version,
                "origin": origin,
                "direct_url": direct_url,
                "recorded_artifact_hashes": file_hashes,
                "recorded_artifact_hashes_sha256": _sha256_bytes(_canonical_json(file_hashes)),
            }
        )
    return sorted(
        records,
        key=lambda item: (item["normalized_name"], item["version"], item["origin"]),
    )


def _tool_version(argv: Sequence[str], env: dict[str, str]) -> str | None:
    try:
        process = subprocess.run(
            list(argv),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            shell=False,
        )
    except OSError:
        return None
    if process.returncode != 0:
        return None
    return process.stdout.decode("utf-8", "replace").splitlines()[0] if process.stdout else ""


def _environment_record(
    namespace: argparse.Namespace,
    roots: dict[str, Path],
    child_env: dict[str, str],
    allowlist: list[str],
    excluded: dict[str, str],
    os_release: dict[str, str],
    kernel_release: str,
    dependency_inputs: dict[str, dict[str, Any]],
    installer_argv: list[str],
    install_log: bytes,
) -> dict[str, Any]:
    identity_env = dict(child_env)
    dpkg = _run_bytes(
        ["dpkg-query", "-W", "-f", "${binary:Package}\t${Version}\t${Architecture}\n"],
        cwd=None,
        env=identity_env,
    )
    dpkg = b"\n".join(sorted(dpkg.splitlines())) + b"\n"
    apt_records, apt_hash = _apt_configuration()
    executable = Path(sys.executable).resolve(strict=True)
    inventory = _distribution_inventory()
    try:
        pip_version: str | None = importlib.metadata.version("pip")
    except importlib.metadata.PackageNotFoundError:
        pip_version = None
    soabi = sysconfig.get_config_var("SOABI") or "unknown"
    platform_name = sysconfig.get_platform()
    interpreter_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    python_record = {
        "executable": str(executable),
        "executable_sha256": _sha256_file(executable),
        "version": sys.version,
        "version_info": list(sys.version_info[:5]),
        "build": list(platform.python_build()),
        "implementation": platform.python_implementation(),
        "abi": soabi,
        "sysconfig_platform": platform_name,
        "platform_tags": [
            {"interpreter": interpreter_tag, "abi": soabi, "platform": platform_name}
        ],
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "declared_venv_root": str(roots["venv"]),
        "declared_venv_active": Path(sys.prefix).resolve(strict=True) == roots["venv"],
    }
    dependency_mode = namespace.dependency_mode
    if dependency_mode == "hash_locked":
        dependency_limitation = (
            "Artifacts were selected from hash-checked inputs; the WSL rootfs remains mutable."
        )
    else:
        dependency_limitation = (
            "This is an exact inventory of a mutable resolution; it is not hash-locked "
            "or an immutable environment identity."
        )
    dependency_record = {
        "mode": dependency_mode,
        "limitation": dependency_limitation,
        "pip_version": namespace.installer_version or pip_version,
        "installer_argv": installer_argv,
        "install_performed": bool(namespace.dependency_install_log),
        "install_log_bytes": len(install_log),
        "install_log_sha256": _sha256_bytes(install_log),
        "input_hashes": dependency_inputs,
        "package_index_identities": sorted(
            _sanitize_url(value) for value in namespace.package_index
        ),
        "installed_distributions": inventory,
        "installed_inventory_sha256": _sha256_bytes(_canonical_json(inventory)),
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "platform": {
            "identity_mode": "mutable_wsl_substitute",
            "limitation": (
                "Mutable WSL evidence identity; labels and inventories do not constitute "
                "an immutable image digest."
            ),
            "kernel": kernel_release,
            "uname": list(platform.uname()),
            "architecture": platform.machine(),
            "libc": list(platform.libc_ver()),
            "distribution": {
                "id": os_release.get("id"),
                "version_id": os_release.get("version_id"),
                "name": os_release.get("pretty_name"),
            },
            "dpkg_inventory": dpkg.decode("utf-8", "replace"),
            "dpkg_inventory_sha256": _sha256_bytes(dpkg),
            "apt_configuration_files": apt_records,
            "apt_configuration_aggregate_sha256": apt_hash,
        },
        "python": python_record,
        "dependencies": dependency_record,
        "tools": {
            "git": _tool_version(["git", "--version"], identity_env),
            "bash": _tool_version(["bash", "--version"], identity_env),
            "dpkg_query": _tool_version(["dpkg-query", "--version"], identity_env),
        },
        "runtime_roots": {name: str(path) for name, path in roots.items() if name != "repo"},
        "recorded_environment": dict(sorted(child_env.items())),
        "environment_allowlist": allowlist,
        "excluded_sensitive_variables": excluded,
        "timezone": child_env.get("TZ"),
        "locale": {"LANG": child_env.get("LANG"), "LC_ALL": child_env.get("LC_ALL")},
    }


def _resolve_executable(argv0: str, cwd: Path, path_value: str) -> Path | None:
    if "/" in argv0:
        candidate = Path(argv0)
        if not candidate.is_absolute():
            candidate = cwd / candidate
    else:
        found = shutil.which(argv0, path=path_value)
        if found is None:
            return None
        candidate = Path(found)
    try:
        candidate = candidate.absolute()
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None
    if not resolved.is_file() or not os.access(candidate, os.X_OK):
        return None
    # Execute through the discovered venv shim/symlink so Python observes its
    # pyvenv.cfg.  Hashing below follows the link and records the real binary.
    return candidate


def _safe_read_external_file(raw: str, repo: Path, label: str) -> tuple[Path, bytes]:
    path = Path(raw)
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise ValidationError(f"{label} must be an absolute normalized path")
    _assert_no_symlink_components(path)
    resolved = path.resolve(strict=True)
    if _contains_path(repo, resolved):
        raise ValidationError(f"{label} must be external")
    info = resolved.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValidationError(f"{label} must be a single-link regular file")
    return resolved, resolved.read_bytes()


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise CaptureError("atomic no-replace rename is unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = function(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace,
    )
    if result != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise ValidationError("final evidence run already exists")
        raise OSError(code, os.strerror(code))


def _remove_owned_stage(stage: Path, evidence_root: Path) -> None:
    if stage.parent != evidence_root:
        raise CaptureError("refusing cleanup outside evidence root")
    try:
        info = stage.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CaptureError("refusing cleanup of non-directory staging path")
    for entry in os.scandir(stage):
        entry_info = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(entry_info.st_mode) or entry_info.st_nlink != 1:
            raise CaptureError("refusing cleanup of unsafe staging entry")
        os.unlink(entry.path)
    os.rmdir(stage)


def _secret_present(stage: Path, secret_values: Sequence[str]) -> bool:
    needles = [item.encode("utf-8", "surrogatepass") for item in secret_values if item]
    if not needles:
        return False
    for path in stage.iterdir():
        if not path.is_file() or path.name == "hashes.json":
            continue
        data = path.read_bytes()
        if any(needle in data for needle in needles):
            return True
    return False


def _replay(path: Path, stream: Any) -> None:
    with path.open("rb") as handle:
        shutil.copyfileobj(handle, stream)
    stream.flush()


def _parse_installer_argv(raw: str | None, repo: Path) -> list[str]:
    if raw is None:
        return []
    _, data = _safe_read_external_file(raw, repo, "installer argv record")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("installer argv record is not valid JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("installer argv record must be a string array")
    return _validate_command_argv(value)


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture an exact argv command into an atomic RP-020 evidence bundle.",
        allow_abbrev=False,
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--venv-root", required=True)
    parser.add_argument("--home-root", required=True)
    parser.add_argument("--tmp-root", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--pip-cache-root", required=True)
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--dependency-input", action="append", default=[])
    parser.add_argument("--external-dependency-input", action="append", default=[])
    parser.add_argument("--dependency-install-log")
    parser.add_argument("--installer-argv-json")
    parser.add_argument("--installer-arg", action="append", default=[])
    parser.add_argument("--installer-version")
    parser.add_argument(
        "--dependency-mode",
        choices=("exactly_recorded", "hash_locked"),
        default="exactly_recorded",
    )
    parser.add_argument("--package-index", action="append", default=[])
    parser.add_argument("--allow-env", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _capture(namespace: argparse.Namespace) -> int:
    os_release, kernel_release = _require_wsl_python()
    run_id = _validate_identifier(namespace.run_id)
    roots = _validate_runtime_paths(namespace)
    command = list(namespace.command)
    if command and command[0] == "--":
        command = command[1:]
    command = _validate_command_argv(command)

    inputs: dict[str, Path] = {}
    for raw in namespace.input:
        name, path = _validate_relative_input(roots["repo"], raw)
        inputs[name] = path
    dependency_inputs: dict[str, dict[str, Any]] = {}
    for raw in namespace.dependency_input:
        name, path = _validate_relative_input(roots["repo"], raw)
        dependency_inputs[name] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
    for raw in namespace.external_dependency_input:
        path, data = _safe_read_external_file(raw, roots["repo"], "external dependency input")
        name = path.name
        if name in dependency_inputs:
            raise ValidationError("dependency input name collision")
        dependency_inputs[name] = {
            "path": str(path),
            "bytes": len(data),
            "sha256": _sha256_bytes(data),
        }

    child_env, allowlist, excluded, secret_values = _safe_child_environment(namespace, roots)
    installer_argv = _parse_installer_argv(namespace.installer_argv_json, roots["repo"])
    if namespace.installer_arg:
        if installer_argv:
            raise ValidationError("installer argv may be supplied through only one interface")
        installer_argv = _validate_command_argv(namespace.installer_arg)
    if namespace.dependency_mode == "hash_locked" and not namespace.dependency_install_log:
        raise ValidationError("hash-locked mode requires a complete install log")
    install_log = b""
    if namespace.dependency_install_log:
        _, install_log = _safe_read_external_file(
            namespace.dependency_install_log,
            roots["repo"],
            "dependency install log",
        )

    evidence_root = roots["evidence"]
    final = evidence_root / run_id
    stage = evidence_root / f".{run_id}.incomplete"
    if os.path.lexists(final) or os.path.lexists(stage):
        raise ValidationError("evidence run or staging path already exists")
    os.mkdir(stage, mode=0o700)
    created_stage = True
    finalized = False
    try:
        input_hashes = {
            name: {"bytes": path.stat().st_size, "sha256": _sha256_file(path)}
            for name, path in sorted(inputs.items())
        }
        before = _source_snapshot(roots["repo"], child_env, sorted(inputs))
        before_checked_utc = _utc_now()
        environment_record = _environment_record(
            namespace,
            roots,
            child_env,
            allowlist,
            excluded,
            os_release,
            kernel_release,
            dependency_inputs,
            installer_argv,
            install_log,
        )
        executable = _resolve_executable(command[0], roots["repo"], child_env["PATH"])
        started = _utc_now()
        stdout_path = stage / "stdout.bin"
        stderr_path = stage / "stderr.bin"
        target_returncode: int | None = None
        failure_record: dict[str, Any] | None = None
        if executable is None:
            _write_exclusive(stdout_path, b"")
            _write_exclusive(stderr_path, b"")
            collector_status = "command_not_found"
            failure_record = {
                "kind": "command_not_found",
                "message": "target executable was not found in the allowed PATH",
            }
        else:
            stdout_descriptor = os.open(
                stdout_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            stderr_descriptor = os.open(
                stderr_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                process = subprocess.Popen(
                    [str(executable), *command[1:]],
                    cwd=roots["repo"],
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_descriptor,
                    stderr=stderr_descriptor,
                    shell=False,
                )
                target_returncode = process.wait()
            finally:
                os.close(stdout_descriptor)
                os.close(stderr_descriptor)
            collector_status = "captured"
        finished = _utc_now()
        after = _source_snapshot(roots["repo"], child_env, sorted(inputs))
        after_checked_utc = _utc_now()

        if target_returncode is None:
            target_exit_code = None
            target_signal = None
            shell_status = None
        elif target_returncode < 0:
            number = -target_returncode
            target_exit_code = None
            try:
                signal_name = signal.Signals(number).name
            except ValueError:
                signal_name = f"SIGNAL_{number}"
            target_signal = {"name": signal_name, "number": number}
            shell_status = min(255, 128 + number)
        else:
            target_exit_code = target_returncode
            target_signal = None
            shell_status = target_returncode

        resolved_record = None
        if executable is not None:
            real_executable = executable.resolve(strict=True)
            resolved_record = {
                "path": str(executable),
                "real_path": str(real_executable),
                "bytes": real_executable.stat().st_size,
                "sha256": _sha256_file(real_executable),
            }
        final_snapshot = _source_snapshot(roots["repo"], child_env, sorted(inputs))
        final_checked_utc = _utc_now()
        changed_paths = sorted(
            set(_snapshot_changed_paths(before, after))
            | set(_snapshot_changed_paths(after, final_snapshot))
            | set(_snapshot_changed_paths(before, final_snapshot))
        )
        checkout_unchanged = before == after == final_snapshot
        if not checkout_unchanged and collector_status == "captured":
            collector_status = "source_mutation"

        command_record = {
            "contract_version": CONTRACT_VERSION,
            "argv": command,
            "resolved_executable": resolved_record,
            "working_directory": str(roots["repo"]),
            "started_utc": started,
            "finished_utc": finished,
            "target_returncode": target_returncode,
            "target_exit_code": target_exit_code,
            "target_signal": target_signal,
            "shell_status": shell_status,
            "collector_status": collector_status,
        }
        source_record = {
            "contract_version": CONTRACT_VERSION,
            "repository_root": str(roots["repo"]),
            "before": before,
            "after": after,
            "final": final_snapshot,
            "input_hashes": input_hashes,
            "comparison": {
                "checkout_unchanged": checkout_unchanged,
                "changed_paths": changed_paths,
                "before_status_sha256": before["status_sha256"],
                "after_status_sha256": after["status_sha256"],
                "before_tracked_inventory_sha256": before["tracked_inventory_sha256"],
                "after_tracked_inventory_sha256": after["tracked_inventory_sha256"],
                "final_tracked_inventory_sha256": final_snapshot["tracked_inventory_sha256"],
                "before_declared_input_inventory_sha256": before[
                    "declared_input_inventory_sha256"
                ],
                "after_declared_input_inventory_sha256": after[
                    "declared_input_inventory_sha256"
                ],
                "final_declared_input_inventory_sha256": final_snapshot[
                    "declared_input_inventory_sha256"
                ],
            },
            "integrity_attestation": {
                "before_checked_utc": before_checked_utc,
                "after_checked_utc": after_checked_utc,
                "final_checked_utc": final_checked_utc,
                "before_snapshot_sha256": _sha256_bytes(_canonical_json(before)),
                "after_snapshot_sha256": _sha256_bytes(_canonical_json(after)),
                "final_snapshot_sha256": _sha256_bytes(_canonical_json(final_snapshot)),
                "checkout_unchanged_through_finalization": checkout_unchanged,
                "atomic_finalize_method": "renameat2(RENAME_NOREPLACE)",
                "final_check_boundary": "immediately before evidence serialization and atomic rename",
            },
        }
        _write_json(stage / "command.json", command_record)
        _write_json(stage / "environment.json", environment_record)
        _write_json(stage / "source.json", source_record)
        if install_log:
            _write_exclusive(stage / "dependency-install.bin", install_log)
        if failure_record is not None:
            _write_json(stage / "failure.json", failure_record)

        valid = collector_status == "captured" and checkout_unchanged
        if collector_status == "command_not_found":
            collector_exit = COMMAND_NOT_FOUND_EXIT
        elif collector_status != "captured":
            collector_exit = COLLECTOR_FAILURE_EXIT
        elif target_returncode is not None and target_returncode < 0:
            collector_exit = min(255, 128 - target_returncode)
        else:
            collector_exit = target_returncode if target_returncode is not None else COLLECTOR_FAILURE_EXIT
        entries_without_hashes = sorted(path.name for path in stage.iterdir())
        manifest_record = {
            "contract_version": CONTRACT_VERSION,
            "run_id": run_id,
            "valid": valid,
            "collector_status": collector_status,
            "collector_exit_code": collector_exit,
            "created_utc": finished,
            "layout_entries": sorted(entries_without_hashes + ["hashes.json", "manifest.json"]),
            "aggregate_hash_contract": (
                "sha256 over ordered artifact name, byte length, and SHA-256; hashes.json "
                "uses its canonical representation with self_canonical_sha256 empty"
            ),
        }
        _write_json(stage / "manifest.json", manifest_record)

        if _secret_present(stage, secret_values):
            _remove_owned_stage(stage, evidence_root)
            created_stage = False
            sys.stderr.buffer.write(SECRET_OUTPUT_DIAGNOSTIC)
            sys.stderr.buffer.flush()
            return COLLECTOR_FAILURE_EXIT

        artifact_hashes: dict[str, dict[str, Any]] = {}
        for path in sorted(stage.iterdir(), key=lambda item: item.name):
            data = path.read_bytes()
            artifact_hashes[path.name] = {"bytes": len(data), "sha256": _sha256_bytes(data)}
        aggregate_material = b"".join(
            name.encode("utf-8")
            + b"\0"
            + str(record["bytes"]).encode("ascii")
            + b"\0"
            + record["sha256"].encode("ascii")
            + b"\n"
            for name, record in sorted(artifact_hashes.items())
        )
        hashes_record = {
            "algorithm": "sha256",
            "artifacts": artifact_hashes,
            "aggregate_evidence_sha256": _sha256_bytes(aggregate_material),
            "self_hash_contract": (
                "SHA-256 of canonical hashes.json bytes with self_canonical_sha256 set to empty"
            ),
            "self_canonical_sha256": "",
        }
        hashes_record["self_canonical_sha256"] = _sha256_bytes(_canonical_json(hashes_record))
        _write_json(stage / "hashes.json", hashes_record)
        directory_descriptor = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        _rename_noreplace(stage, final)
        finalized = True
        created_stage = False
        root_descriptor = os.open(evidence_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
        _replay(final / "stdout.bin", sys.stdout.buffer)
        _replay(final / "stderr.bin", sys.stderr.buffer)
        return collector_exit
    finally:
        if created_stage and not finalized:
            _remove_owned_stage(stage, evidence_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _create_parser()
    namespace = parser.parse_args(argv)
    try:
        return _capture(namespace)
    except ValidationError as exc:
        print(f"capture rejected: {exc}", file=sys.stderr)
        return USAGE_EXIT
    except (CaptureError, OSError) as exc:
        print(f"collector failure: {type(exc).__name__}", file=sys.stderr)
        return COLLECTOR_FAILURE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
