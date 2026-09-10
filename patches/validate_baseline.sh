#!/usr/bin/env bash
# Validate metadata, applicability, and exact vendored-tree reconstruction.

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

! grep -R "AppData/Local/Temp\\|/tmp/tmp\\|Temp/tmp" patches/*.patch >/dev/null

python_repo="$REPO"
python_upstream_repo="$UPSTREAM_REPO"
if command -v cygpath >/dev/null 2>&1; then
    python_repo="$(cygpath -w "$REPO")"
    python_upstream_repo="$(cygpath -w "$UPSTREAM_REPO")"
fi

python patches/check_exact_reconstruction.py \
    --repo "$python_repo" \
    --upstream-repo "$python_upstream_repo" \
    --upstream "$EXPECTED_COMMIT" \
    --version "$EXPECTED_VERSION"

forbidden_command_wiring='from nanobot\.command import|import nanobot\.command\b|CommandRouter|register_builtin_commands'

! grep -R -E "$forbidden_command_wiring" \
    nanobot/nanobot/agent \
    nanobot/nanobot/channels \
    nanobot/nanobot/cli >/dev/null

if [ -d familia/src ]; then
    ! grep -R -E "$forbidden_command_wiring" familia/src >/dev/null
fi
