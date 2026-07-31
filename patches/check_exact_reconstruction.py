#!/usr/bin/env python3
"""Prove that pinned nanobot + committed patches equals the vendored scope.

The check deliberately reports patch applicability and exact equality as two
different facts.  A patch set may still apply cleanly while omitting later
worktree changes, stale paths, modes, or ownership records.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


DEFAULT_UPSTREAM = "950dddec499fbbe0353e997158c99808f0bb41e1"
DEFAULT_VERSION = "0.1.5.post2"
VALID_CATEGORIES = {
    "familia-invariant",
    "upstream-alignment",
    "generated-noise",
    "unknown",
}
TARGET_PREFIX = "nanobot/nanobot/"
TARGET_ROOT_FILES = {"nanobot/pyproject.toml", "nanobot/README.md"}
PATCH_HEADER_RE = re.compile(r"^@@(?: |$)", re.MULTILINE)


class ReconstructionError(RuntimeError):
    """An environment or manifest error that prevents a meaningful proof."""


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    data: bytes

    def blob_oid(self, object_format: str) -> str:
        digest = hashlib.new(object_format)
        digest.update(f"blob {len(self.data)}\0".encode("ascii"))
        digest.update(self.data)
        return digest.hexdigest()


@dataclass
class CheckResult:
    apply_valid: bool
    exact_equal: bool
    path_set_equal: bool
    blob_bytes_equal: bool
    modes_equal: bool
    deterministic_patch_names: bool
    ownership_complete: bool
    patch_count: int
    expected_patch_count: int
    delta_count: int
    unowned_delta_count: int
    unowned_hunk_count: int
    ownership_unknown_count: int
    direct_familia_import_count: int
    errors: list[str]


def _run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        [os.fspath(part) for part in argv],
        cwd=cwd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        command = " ".join(os.fspath(part) for part in argv)
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReconstructionError(f"command failed ({completed.returncode}): {command}\n{detail}")
    return completed


def _git(
    repo: Path,
    *args: str | os.PathLike[str],
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        ["git", "-c", f"safe.directory={repo}", "-C", repo, *args],
        input_bytes=input_bytes,
        check=check,
    )


def _target_path(source_path: str) -> str:
    if source_path.startswith("nanobot/"):
        return f"nanobot/nanobot/{source_path.removeprefix('nanobot/')}"
    if source_path == "pyproject.toml":
        return "nanobot/pyproject.toml"
    if source_path == "README.md":
        return "nanobot/README.md"
    raise ReconstructionError(f"path is outside declared upstream scope: {source_path}")


def _is_target_path(path: str) -> bool:
    return path.startswith(TARGET_PREFIX) or path in TARGET_ROOT_FILES


def _parse_stage_records(raw: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, _oid, stage = metadata.split(b" ", 2)
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if stage != b"0":
            raise ReconstructionError(f"unmerged index entry in declared scope: {path}")
        result[path] = mode.decode("ascii")
    return result


def _baseline_tree(upstream_repo: Path, upstream: str) -> tuple[dict[str, TreeEntry], str]:
    resolved = _git(upstream_repo, "rev-parse", "--verify", f"{upstream}^{{commit}}").stdout
    resolved_commit = resolved.decode("ascii").strip()
    object_format = (
        _git(upstream_repo, "rev-parse", "--show-object-format")
        .stdout.decode("ascii")
        .strip()
    )
    raw = _git(
        upstream_repo,
        "ls-tree",
        "-rz",
        "--full-tree",
        resolved_commit,
        "--",
        "nanobot",
        "pyproject.toml",
        "README.md",
    ).stdout
    tree: dict[str, TreeEntry] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, oid = metadata.split(b" ", 2)
        if object_type != b"blob":
            continue
        source_path = raw_path.decode("utf-8", errors="surrogateescape")
        path = _target_path(source_path)
        data = _git(upstream_repo, "cat-file", "blob", oid.decode("ascii")).stdout
        tree[path] = TreeEntry(mode=mode.decode("ascii"), data=data)
    if not tree:
        raise ReconstructionError("pinned upstream declared scope is empty")
    return tree, object_format


def _current_tree(repo: Path) -> dict[str, TreeEntry]:
    scopes = ["nanobot/nanobot", "nanobot/pyproject.toml", "nanobot/README.md"]
    raw_paths = _git(
        repo,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        *scopes,
    ).stdout
    index_modes = _parse_stage_records(
        _git(repo, "ls-files", "--stage", "-z", "--", *scopes).stdout
    )
    tree: dict[str, TreeEntry] = {}
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        if not _is_target_path(path):
            raise ReconstructionError(f"git returned an out-of-scope target path: {path}")
        absolute = repo / PurePosixPath(path)
        if absolute.is_symlink():
            data = os.readlink(absolute).encode("utf-8", errors="surrogateescape")
            inferred_mode = "120000"
        elif absolute.is_file():
            data = absolute.read_bytes()
            executable = bool(absolute.stat().st_mode & stat.S_IXUSR)
            inferred_mode = "100755" if executable else "100644"
        else:
            # A tracked worktree deletion is intentionally absent from the target set.
            continue
        tree[path] = TreeEntry(mode=index_modes.get(path, inferred_mode), data=data)
    if not tree:
        raise ReconstructionError("current declared vendored scope is empty")
    return tree


def patch_name_for(path: str) -> str:
    if path == "nanobot/pyproject.toml":
        return "pyproject.patch"
    if path == "nanobot/README.md":
        return "README.patch"
    if not path.startswith(TARGET_PREFIX):
        raise ReconstructionError(f"cannot name patch for out-of-scope path: {path}")
    relative = path.removeprefix("nanobot/")
    relative = relative.removeprefix("nanobot/")
    stem = relative.rsplit(".", 1)[0] if "." in PurePosixPath(relative).name else relative
    return f"{stem.replace('/', '_')}.patch"


def _delta_paths(
    baseline: dict[str, TreeEntry], current: dict[str, TreeEntry]
) -> list[str]:
    return sorted(
        path
        for path in baseline.keys() | current.keys()
        if baseline.get(path) != current.get(path)
    )


def _load_ownership(path: Path, upstream: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReconstructionError(f"ownership manifest is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReconstructionError(
            f"ownership manifest must be JSON-compatible YAML: {path}:{exc.lineno}: {exc.msg}"
        ) from exc
    if data.get("schema_version") != 1:
        raise ReconstructionError("ownership schema_version must be 1")
    if data.get("baseline", {}).get("commit") != upstream:
        raise ReconstructionError("ownership baseline commit does not match checker baseline")
    if not isinstance(data.get("deltas"), list):
        raise ReconstructionError("ownership deltas must be a list")
    return data


def _patch_hunk_count(path: Path) -> int:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    return len(PATCH_HEADER_RE.findall(content))


def _ownership_errors(
    manifest: dict[str, Any],
    delta_paths: list[str],
    expected_patch_by_path: dict[str, str],
    patch_dir: Path,
) -> tuple[list[str], int, int, int]:
    errors: list[str] = []
    rows: dict[str, dict[str, Any]] = {}
    for row in manifest["deltas"]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            errors.append("ownership row lacks a string path")
            continue
        path = row["path"]
        if path in rows:
            errors.append(f"duplicate ownership path: {path}")
            continue
        rows[path] = row

    expected = set(delta_paths)
    actual = set(rows)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    errors.extend(f"unowned delta path: {path}" for path in missing)
    errors.extend(f"stale ownership path: {path}" for path in extra)

    unowned_hunks = 0
    unknown_count = 0
    for path in sorted(expected & actual):
        row = rows[path]
        expected_patch = expected_patch_by_path[path]
        if row.get("patch") != expected_patch:
            errors.append(
                f"ownership patch mismatch for {path}: {row.get('patch')!r} != {expected_patch!r}"
            )
        category = row.get("category")
        if category not in VALID_CATEGORIES:
            errors.append(f"invalid ownership category for {path}: {category!r}")
        if not isinstance(row.get("owner"), str) or not row["owner"].strip():
            errors.append(f"ownership owner is missing for {path}")
        if category == "unknown":
            unknown_count += 1
        hunks = row.get("hunks")
        if not isinstance(hunks, list) or not hunks:
            errors.append(f"ownership hunks are missing for {path}")
            unowned_hunks += _patch_hunk_count(patch_dir / expected_patch)
            continue
        wildcard = False
        explicit: set[int] = set()
        for hunk in hunks:
            if not isinstance(hunk, dict):
                errors.append(f"invalid ownership hunk record for {path}")
                continue
            if hunk.get("category") not in VALID_CATEGORIES:
                errors.append(f"invalid hunk category for {path}: {hunk.get('category')!r}")
            if hunk.get("range") == "all":
                wildcard = True
            indices = hunk.get("indices", [])
            if isinstance(indices, list):
                explicit.update(index for index in indices if isinstance(index, int))
        hunk_count = _patch_hunk_count(patch_dir / expected_patch)
        if not wildcard:
            uncovered = set(range(1, hunk_count + 1)) - explicit
            unowned_hunks += len(uncovered)
            if uncovered:
                errors.append(
                    f"unowned hunks for {path}: {','.join(map(str, sorted(uncovered)))}"
                )
    return errors, len(missing), unowned_hunks, unknown_count


def _direct_familia_imports(current: dict[str, TreeEntry]) -> list[str]:
    findings: list[str] = []
    for path, entry in sorted(current.items()):
        if not path.startswith(TARGET_PREFIX) or not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(entry.data.decode("utf-8-sig"), filename=path)
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise ReconstructionError(f"cannot parse vendored Python for import audit: {path}: {exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] == "familia":
                        findings.append(f"{path}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.split(".", 1)[0] == "familia":
                    findings.append(f"{path}:{node.lineno}: from {module} import ...")
    return findings


def _write_baseline_index(root: Path, baseline: dict[str, TreeEntry]) -> None:
    _run(["git", "init", "-q"], cwd=root)
    _run(["git", "config", "core.autocrlf", "false"], cwd=root)
    _run(["git", "config", "core.filemode", "true"], cwd=root)
    for path, entry in baseline.items():
        absolute = root / PurePosixPath(path)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(entry.data)
    _run(["git", "add", "--force", "-A"], cwd=root)
    for path, entry in baseline.items():
        if entry.mode == "100755":
            _run(["git", "update-index", "--chmod=+x", "--", path], cwd=root)
        elif entry.mode == "100644":
            _run(["git", "update-index", "--chmod=-x", "--", path], cwd=root)
        else:
            raise ReconstructionError(f"unsupported baseline mode {entry.mode} for {path}")


def _indexed_tree(root: Path) -> dict[str, TreeEntry]:
    modes = _parse_stage_records(_run(["git", "ls-files", "--stage", "-z"], cwd=root).stdout)
    tree: dict[str, TreeEntry] = {}
    for path, mode in modes.items():
        absolute = root / PurePosixPath(path)
        if mode == "120000":
            data = os.readlink(absolute).encode("utf-8", errors="surrogateescape")
        else:
            data = absolute.read_bytes()
        tree[path] = TreeEntry(mode=mode, data=data)
    return tree


def _compare_trees(
    reconstructed: dict[str, TreeEntry], current: dict[str, TreeEntry]
) -> tuple[bool, bool, bool, list[str]]:
    errors: list[str] = []
    reconstructed_paths = set(reconstructed)
    current_paths = set(current)
    missing = sorted(current_paths - reconstructed_paths)
    extra = sorted(reconstructed_paths - current_paths)
    errors.extend(f"reconstruction missing path: {path}" for path in missing)
    errors.extend(f"reconstruction has extra path: {path}" for path in extra)
    byte_mismatches: list[str] = []
    mode_mismatches: list[str] = []
    for path in sorted(reconstructed_paths & current_paths):
        if reconstructed[path].data != current[path].data:
            byte_mismatches.append(path)
            errors.append(f"blob bytes differ: {path}")
        if reconstructed[path].mode != current[path].mode:
            mode_mismatches.append(path)
            errors.append(
                f"mode differs: {path}: {reconstructed[path].mode} != {current[path].mode}"
            )
    return not missing and not extra, not byte_mismatches, not mode_mismatches, errors


def check_exact_reconstruction(
    *,
    repo: Path,
    upstream_repo: Path,
    upstream: str,
    version: str,
    patch_dir: Path,
    ownership_path: Path,
) -> CheckResult:
    baseline, _object_format = _baseline_tree(upstream_repo, upstream)
    current = _current_tree(repo)
    delta_paths = _delta_paths(baseline, current)

    expected_patch_by_path = {path: patch_name_for(path) for path in delta_paths}
    if len(set(expected_patch_by_path.values())) != len(expected_patch_by_path):
        raise ReconstructionError("deterministic patch filename collision in declared scope")
    expected_patch_names = set(expected_patch_by_path.values())
    actual_patches = sorted(patch_dir.glob("*.patch"), key=lambda path: path.name)
    actual_patch_names = {path.name for path in actual_patches}
    patch_errors = [
        *(f"missing deterministic patch: {name}" for name in sorted(expected_patch_names - actual_patch_names)),
        *(f"extra deterministic patch: {name}" for name in sorted(actual_patch_names - expected_patch_names)),
    ]
    deterministic_patch_names = not patch_errors

    header_errors: list[str] = []
    expected_headers = (
        f"# nanobot baseline: {version}\n"
        f"# upstream commit: {upstream}\n"
    )
    for patch_path in actual_patches:
        if not patch_path.read_text(encoding="utf-8").startswith(expected_headers):
            header_errors.append(f"invalid patch header: {patch_path.name}")

    manifest = _load_ownership(ownership_path, upstream)
    ownership_errors, unowned_delta_count, unowned_hunk_count, unknown_count = (
        _ownership_errors(
            manifest,
            delta_paths,
            expected_patch_by_path,
            patch_dir,
        )
    )
    ownership_complete = not ownership_errors and not unowned_delta_count and not unowned_hunk_count

    direct_imports = _direct_familia_imports(current)

    apply_valid = False
    path_set_equal = False
    blob_bytes_equal = False
    modes_equal = False
    comparison_errors: list[str] = []
    apply_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="familia-rp110-") as temp:
        reconstruction_root = Path(temp)
        _write_baseline_index(reconstruction_root, baseline)
        check = _run(
            ["git", "apply", "--check", "--index", *actual_patches],
            cwd=reconstruction_root,
            check=False,
        )
        apply_valid = check.returncode == 0
        if not apply_valid:
            detail = check.stderr.decode("utf-8", errors="replace").strip()
            apply_errors.append(f"patch application check failed: {detail}")
        else:
            applied = _run(
                ["git", "apply", "--index", *actual_patches],
                cwd=reconstruction_root,
                check=False,
            )
            if applied.returncode != 0:
                apply_valid = False
                detail = applied.stderr.decode("utf-8", errors="replace").strip()
                apply_errors.append(f"patch application failed after successful check: {detail}")
            else:
                reconstructed = _indexed_tree(reconstruction_root)
                (
                    path_set_equal,
                    blob_bytes_equal,
                    modes_equal,
                    comparison_errors,
                ) = _compare_trees(reconstructed, current)

    errors = [
        *patch_errors,
        *header_errors,
        *ownership_errors,
        *(f"direct Familia import: {finding}" for finding in direct_imports),
        *apply_errors,
        *comparison_errors,
    ]
    exact_equal = bool(
        apply_valid
        and path_set_equal
        and blob_bytes_equal
        and modes_equal
        and deterministic_patch_names
        and not header_errors
        and ownership_complete
        and not direct_imports
    )
    return CheckResult(
        apply_valid=apply_valid,
        exact_equal=exact_equal,
        path_set_equal=path_set_equal,
        blob_bytes_equal=blob_bytes_equal,
        modes_equal=modes_equal,
        deterministic_patch_names=deterministic_patch_names,
        ownership_complete=ownership_complete,
        patch_count=len(actual_patches),
        expected_patch_count=len(expected_patch_names),
        delta_count=len(delta_paths),
        unowned_delta_count=unowned_delta_count,
        unowned_hunk_count=unowned_hunk_count,
        ownership_unknown_count=unknown_count,
        direct_familia_import_count=len(direct_imports),
        errors=errors,
    )


def _print_result(result: CheckResult, upstream: str) -> None:
    values: Iterable[tuple[str, object]] = (
        ("baseline_commit", upstream),
        ("patch_count", result.patch_count),
        ("expected_patch_count", result.expected_patch_count),
        ("delta_count", result.delta_count),
        ("apply_valid", str(result.apply_valid).lower()),
        ("exact_equal", str(result.exact_equal).lower()),
        ("path_set_equal", str(result.path_set_equal).lower()),
        ("blob_bytes_equal", str(result.blob_bytes_equal).lower()),
        ("modes_equal", str(result.modes_equal).lower()),
        ("deterministic_patch_names", str(result.deterministic_patch_names).lower()),
        ("ownership_complete", str(result.ownership_complete).lower()),
        ("unowned_delta_count", result.unowned_delta_count),
        ("unowned_hunk_count", result.unowned_hunk_count),
        ("ownership_unknown_count", result.ownership_unknown_count),
        ("direct_familia_import_count", result.direct_familia_import_count),
    )
    for key, value in values:
        print(f"{key}={value}")
    for error in result.errors:
        print(f"ERROR: {error}")
    print(f"RESULT={'PASS' if result.exact_equal else 'FAIL'}")


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=root)
    parser.add_argument("--upstream-repo", type=Path, default=root.parent / "nanobot")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--patch-dir", type=Path)
    parser.add_argument("--ownership", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.resolve()
    patch_dir = (args.patch_dir or repo / "patches").resolve()
    ownership_path = (args.ownership or patch_dir / "ownership.yaml").resolve()
    try:
        result = check_exact_reconstruction(
            repo=repo,
            upstream_repo=args.upstream_repo.resolve(),
            upstream=args.upstream,
            version=args.version,
            patch_dir=patch_dir,
            ownership_path=ownership_path,
        )
    except ReconstructionError as exc:
        print("apply_valid=false")
        print("exact_equal=false")
        print(f"ERROR: {exc}")
        print("RESULT=FAIL")
        return 2
    _print_result(result, args.upstream)
    return 0 if result.exact_equal else 1


if __name__ == "__main__":
    sys.exit(main())
