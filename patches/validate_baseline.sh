#!/usr/bin/env bash
# Validate that patch metadata points at the intended nanobot baseline.

set -euo pipefail

EXPECTED_COMMIT="950dddec499fbbe0353e997158c99808f0bb41e1"
EXPECTED_VERSION="0.1.5.post2"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_REPO="${UPSTREAM_REPO:-$(cd "$REPO/.." && pwd)/nanobot}"
cd "$REPO"

grep -q "$EXPECTED_COMMIT" patches/README.md
grep -q "$EXPECTED_VERSION" patches/README.md
grep -q "$EXPECTED_COMMIT" patches/regenerate.sh
grep -q "$EXPECTED_VERSION" patches/regenerate.sh

! grep -R "328a386\\|0806ac02c" patches/README.md patches/regenerate.sh >/dev/null

for required_patch in \
    patches/command___init__.patch \
    patches/runtime_adapters.patch \
    patches/agent_loop.patch \
    patches/channels_base.patch \
    patches/channels_manager.patch \
    patches/cli_commands.patch
do
    test -s "$required_patch"
done

! grep -R "AppData/Local/Temp\\|/tmp/tmp\\|Temp/tmp" patches/*.patch >/dev/null

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/root/nanobot/nanobot" "$tmp/raw"
git -c "safe.directory=$UPSTREAM_REPO" -C "$UPSTREAM_REPO" archive "$EXPECTED_COMMIT" nanobot pyproject.toml README.md \
    | tar -x -C "$tmp/raw"
cp -a "$tmp/raw/nanobot/." "$tmp/root/nanobot/nanobot/"
cp "$tmp/raw/pyproject.toml" "$tmp/root/nanobot/pyproject.toml"
cp "$tmp/raw/README.md" "$tmp/root/nanobot/README.md"

cmp -s "$tmp/raw/README.md" nanobot/README.md

(
    cd "$tmp/root"
    git apply --check "$OLDPWD"/patches/*.patch
    git apply "$OLDPWD"/patches/*.patch
)

forbidden_command_wiring='from nanobot\.command import|import nanobot\.command\b|CommandRouter|register_builtin_commands'

! grep -R -E "$forbidden_command_wiring" \
    "$tmp/root/nanobot/nanobot/agent" \
    "$tmp/root/nanobot/nanobot/channels" \
    "$tmp/root/nanobot/nanobot/cli" >/dev/null

if [ -d familia/src ]; then
    ! grep -R -E "$forbidden_command_wiring" familia/src >/dev/null
fi
