#!/usr/bin/env bash
# Rebuild memX's pip-compatible requirements export from the committed
# Poetry lock without resolving or contacting a package index.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

MODE="${1:---write}"
if [[ "$MODE" != "--write" && "$MODE" != "--check" ]]; then
    echo "usage: $0 [--write|--check]" >&2
    exit 2
fi

python3 - "$MODE" <<'PY'
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

mode = sys.argv[1]
pyproject_path = Path("memx/pyproject.toml")
lock_path = Path("memx/poetry.lock")
requirements_path = Path("memx/requirements.txt")

pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))

requires_python = pyproject["project"]["requires-python"]
if requires_python != ">=3.12,<4.0":
    raise SystemExit(f"unsupported requires-python: {requires_python!r}")
python_marker = 'python_version >= "3.12" and python_version < "4.0"'

packages: dict[str, dict[str, object]] = {}
for package in lock.get("package", []):
    if "main" not in package.get("groups", []):
        continue
    name = str(package["name"]).lower().replace("_", "-")
    if name in packages:
        raise SystemExit(f"duplicate main lock package: {name}")
    packages[name] = package

direct_names: set[str] = set()
for requirement in pyproject["project"]["dependencies"]:
    match = re.match(r"^([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        raise SystemExit(f"cannot parse direct dependency: {requirement!r}")
    direct_names.add(match.group(1).lower().replace("_", "-"))
missing = sorted(direct_names - packages.keys())
if missing:
    raise SystemExit(f"direct dependencies absent from poetry.lock: {', '.join(missing)}")

lines: list[str] = []
for name, package in sorted(packages.items()):
    marker = python_marker
    package_marker = package.get("markers")
    if package_marker:
        package_marker = str(package_marker)
        if " or " in package_marker:
            package_marker = f"({package_marker})"
        marker = f"{marker} and {package_marker}"
    lines.append(f"{name}=={package['version']} ; {marker}")

rendered = "\n".join(lines) + "\n"
current = requirements_path.read_text(encoding="utf-8") if requirements_path.exists() else ""
if mode == "--check":
    if current != rendered:
        raise SystemExit("memx/requirements.txt is stale; run bin/regen-memx-lock.sh")
    print(f"PASS: memX requirements match poetry.lock ({len(lines)} pins)")
else:
    requirements_path.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {requirements_path} ({len(lines)} pins)")
PY
