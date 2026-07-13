#!/usr/bin/env bash
# Regenerate familia/requirements.lock from the pyproject specs of
# nanobot + familia. Run on a host with docker; uses the same digest-
# pinned base image as the production Dockerfile so the lock matches
# what will actually install at build time.
#
# When to re-run:
#   * After bumping any direct dep range in nanobot/pyproject.toml or
#     familia/pyproject.toml.
#   * On a periodic schedule to absorb security fixes in transitive deps
#     (no functional change required, just regen + commit).
#
# Output is committed to the repo so build-on-VM stays reproducible
# without needing the operator to have uv on their host.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Same digest as Dockerfile FROM. Update both together.
BASE_IMAGE="ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"

OUT="familia/requirements.lock"

compile_with_uv() {
  local uv_bin="$1"
  "$uv_bin" pip compile \
      --python-version 3.12 \
      --python-platform x86_64-manylinux_2_36 \
      --generate-hashes \
      --output-file "$OUT" \
      nanobot/pyproject.toml familia/pyproject.toml
}

if [[ -n "${UV_BIN:-}" ]]; then
  echo "+ resolving deps via $UV_BIN → $OUT"
  compile_with_uv "$UV_BIN"
elif command -v uv >/dev/null 2>&1; then
  echo "+ resolving deps via local uv → $OUT"
  compile_with_uv uv
elif command -v docker >/dev/null 2>&1; then
  echo "+ resolving deps via $BASE_IMAGE → $OUT"
  docker run --rm \
    -v "$REPO_ROOT":/work -w /work \
    --entrypoint /bin/sh \
    "$BASE_IMAGE" \
    -c '
      set -e
      uv pip compile \
          --python-version 3.12 \
          --python-platform x86_64-manylinux_2_36 \
          --generate-hashes \
          --output-file '"$OUT"' \
          nanobot/pyproject.toml familia/pyproject.toml
    '
else
  echo "error: install uv, set UV_BIN, or install Docker to regenerate $OUT" >&2
  exit 1
fi

echo "+ wrote $OUT ($(wc -l < "$OUT") lines)"
echo "  commit alongside the pyproject change that triggered the regen."
