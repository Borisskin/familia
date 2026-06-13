#!/usr/bin/env bash
# Validate that patch metadata points at the intended nanobot baseline.

set -euo pipefail

EXPECTED_COMMIT="950dddec499fbbe0353e997158c99808f0bb41e1"
EXPECTED_VERSION="0.1.5.post2"
UPSTREAM_REPO="${UPSTREAM_REPO:-/d/chat/nanobot}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

grep -q "$EXPECTED_COMMIT" patches/README.md
grep -q "$EXPECTED_VERSION" patches/README.md
grep -q "$EXPECTED_COMMIT" patches/regenerate.sh
grep -q "$EXPECTED_VERSION" patches/regenerate.sh

! grep -R "328a386\\|0806ac02c" patches/README.md patches/regenerate.sh >/dev/null

test -s patches/command___init__.patch
test -s patches/command_builtin.patch
test -s patches/command_router.patch
test -s patches/runtime_adapters.patch

! grep -R "AppData/Local/Temp\\|/tmp/tmp\\|Temp/tmp" patches/*.patch >/dev/null

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/root/nanobot/nanobot" "$tmp/raw"
git -c safe.directory=D:/chat/nanobot -C "$UPSTREAM_REPO" archive "$EXPECTED_COMMIT" nanobot pyproject.toml README.md \
    | tar -x -C "$tmp/raw"
cp -a "$tmp/raw/nanobot/." "$tmp/root/nanobot/nanobot/"
cp "$tmp/raw/pyproject.toml" "$tmp/root/nanobot/pyproject.toml"
cp "$tmp/raw/README.md" "$tmp/root/nanobot/README.md"

cmp -s "$tmp/raw/README.md" nanobot/README.md

(
    cd "$tmp/root"
    git apply --check "$OLDPWD"/patches/*.patch
)
