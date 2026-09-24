#!/usr/bin/env bash
# Regenerate the exact deterministic patch set against pinned nanobot.
#
# Usage:
#   ./patches/regenerate.sh
#   UPSTREAM_REPO=../nanobot UPSTREAM=<sha> ./patches/regenerate.sh
#
# The upstream package layout is nanobot/..., while this repository vendors it
# under nanobot/nanobot/.... The temporary comparison tree below normalizes the
# upstream side into the vendored layout before diffing.

set -euo pipefail

normalize_diff_headers() {
    sed \
        -e "/^diff --git /s|^diff --git a/up/|diff --git a/|" \
        -e "/^diff --git /s|^diff --git a/current/|diff --git a/|" \
        -e "/^diff --git /s| b/up/| b/|" \
        -e "/^diff --git /s| b/current/| b/|" \
        -e "/^--- /s|^--- a/up/|--- a/|" \
        -e "/^--- /s|^--- a/current/|--- a/|" \
        -e "/^+++ /s|^+++ b/up/|+++ b/|" \
        -e "/^+++ /s|^+++ b/current/|+++ b/|"
}

if [[ "${NORMALIZE_HEADERS_ONLY:-}" == "1" ]]; then
    normalize_diff_headers
    exit 0
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_VERSION="${UPSTREAM_VERSION:-0.3.5}"
UPSTREAM="${UPSTREAM:-1bb712d3488915ca4ed9ccc1a93067ff722f5ab9}"
UPSTREAM_REPO="${UPSTREAM_REPO:-$(cd "$REPO/.." && pwd)/nanobot}"
PATCH_DIR="${PATCH_DIR:-$REPO/patches}"

cd "$REPO"
mkdir -p "$PATCH_DIR"

if ! git -c "safe.directory=$UPSTREAM_REPO" -C "$UPSTREAM_REPO" rev-parse --verify "$UPSTREAM^{commit}" >/dev/null 2>&1; then
    echo "refusing to regenerate: UPSTREAM=$UPSTREAM not found in $UPSTREAM_REPO" >&2
    exit 2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/up/raw" "$tmp/up/nanobot" "$tmp/current/nanobot"
git -c "safe.directory=$UPSTREAM_REPO" -C "$UPSTREAM_REPO" archive "$UPSTREAM" \
    | /usr/bin/tar -x -C "$tmp/up/raw"
cp -a "$tmp/up/raw/." "$tmp/up/nanobot/"
while IFS= read -r -d '' rel; do
    target="$tmp/current/$rel"
    mkdir -p "$(dirname "$target")"
    cp -a "$REPO/$rel" "$target"
done < <(
    git -C "$REPO" ls-files --cached --others --exclude-standard -z -- nanobot
)

mapfile -t excluded_upstream_paths < <(
    python - "$REPO/patches/ownership.yaml" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    for row in json.load(handle)["excluded_upstream_paths"]:
        print(row["path"])
PY
)
 # Docker tar extraction applies the container umask instead of preserving
 # Git's mode bits. Normalize both trees, then restore the committed modes
 # recorded by their respective Git indexes.
find "$tmp/up/nanobot" -type f -exec chmod 0644 {} +
python - "$UPSTREAM_REPO" "$UPSTREAM" "$tmp/up/nanobot" <<'PY'
import os
import subprocess
import sys

repo, commit, upstream_root = sys.argv[1:]
records = subprocess.check_output(
    ["git", "-C", repo, "ls-tree", "-r", "-z", commit]
)
for record in records.split(b"\0"):
    if not record:
        continue
    metadata, raw_path = record.split(b"\t", 1)
    mode = metadata.split(b" ", 1)[0].decode("ascii")
    target = os.path.join(upstream_root, *raw_path.decode("utf-8").split("/"))
    if mode == "100755":
        os.chmod(target, 0o755)
    elif mode == "100644":
        os.chmod(target, 0o644)
PY

# Windows bind mounts make every copied file look executable to Linux tools.
# Normalize untracked files to regular mode, then restore the tracked modes
# recorded by the Familia index, including the deliberate install.sh delta.
find "$tmp/current/nanobot" -type f -exec chmod 0644 {} +
python - "$REPO" "$tmp/current/nanobot" <<'PY'
import os
import subprocess
import sys

repo, current_root = sys.argv[1:]
records = subprocess.check_output(
    ["git", "-C", repo, "ls-files", "--stage", "-z", "--", "nanobot"]
)
for record in records.split(b"\0"):
    if not record:
        continue
    metadata, raw_path = record.split(b"\t", 1)
    mode = metadata.split(b" ", 1)[0].decode("ascii")
    relative = raw_path.decode("utf-8").removeprefix("nanobot/")
    target = os.path.join(current_root, *relative.split("/"))
    if mode == "100755":
        os.chmod(target, 0o755)
    elif mode == "100644":
        os.chmod(target, 0o644)
PY

for rel in "${excluded_upstream_paths[@]}"; do
    rm -f "$tmp/up/nanobot/$rel"
done

/usr/bin/find "$PATCH_DIR" -maxdepth 1 -type f -name '*.patch' -delete

patch_name_for() {
    local rel="$1"
    if [[ "$rel" == "pyproject.toml" ]]; then
        echo "pyproject.patch"
        return
    fi
    if [[ "$rel" == "README.md" ]]; then
        echo "README.patch"
        return
    fi
    rel="${rel#nanobot/}"
    rel="${rel%.*}"
    echo "${rel//\//_}.patch"
}

emit_patch() {
    local rel="$1"
    local name
    name="$(patch_name_for "$rel")"
    local left="$tmp/up/nanobot/$rel"
    local right="$tmp/current/nanobot/$rel"
    local left_rel="up/nanobot/$rel"
    local right_rel="current/nanobot/$rel"

    if [[ -f "$left" && -f "$right" ]]; then
        diff_args=("$left_rel" "$right_rel")
    elif [[ -f "$left" ]]; then
        diff_args=("$left_rel" /dev/null)
    else
        diff_args=(/dev/null "$right_rel")
    fi

    (
        cd "$tmp"
        echo "# nanobot baseline: $UPSTREAM_VERSION"
        echo "# upstream commit: $UPSTREAM"
        echo
        git diff --no-index "${diff_args[@]}" 2>/dev/null || true
    ) | normalize_diff_headers > "$PATCH_DIR/$name"
    echo "  $PATCH_DIR/$name"
}

mapfile -t rels < <(
    {
        cd "$tmp/up/nanobot"
        /usr/bin/find . -type f | sed 's|^\./||'
        cd "$tmp/current/nanobot"
        /usr/bin/find . -type f | sed 's|^\./||'
    } | grep -v '__pycache__' | /usr/bin/sort -u
)

echo "-> regenerating patches against nanobot $UPSTREAM_VERSION ($UPSTREAM)"
echo "-> upstream repo: $UPSTREAM_REPO"

for rel in "${rels[@]}"; do
    left="$tmp/up/nanobot/$rel"
    right="$tmp/current/nanobot/$rel"
    if [[ -f "$left" && -f "$right" ]] \
        && cmp -s "$left" "$right" \
        && [[ "$(stat -c '%a' "$left")" == "$(stat -c '%a' "$right")" ]]; then
        continue
    fi
    emit_patch "$rel"
done

python_repo="$REPO"
python_upstream_repo="$UPSTREAM_REPO"
if command -v cygpath >/dev/null 2>&1; then
    python_repo="$(cygpath -w "$REPO")"
    python_upstream_repo="$(cygpath -w "$UPSTREAM_REPO")"
fi

echo "-> proving exact path/blob/mode reconstruction and ownership closure"
if [[ "${SKIP_CHECK:-0}" != "1" ]]; then
    python patches/check_exact_reconstruction.py \
        --repo "$python_repo" \
        --upstream-repo "$python_upstream_repo" \
        --upstream "$UPSTREAM" \
        --version "$UPSTREAM_VERSION" \
        --patch-dir "$PATCH_DIR" \
        --ownership "$PATCH_DIR/ownership.yaml"
fi

echo "-> done. Review with: git diff -- patches/"
