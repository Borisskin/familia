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

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_VERSION="${UPSTREAM_VERSION:-0.1.5.post2}"
UPSTREAM="${UPSTREAM:-950dddec499fbbe0353e997158c99808f0bb41e1}"
UPSTREAM_REPO="${UPSTREAM_REPO:-$(cd "$REPO/.." && pwd)/nanobot}"

cd "$REPO"

if ! git -c "safe.directory=$UPSTREAM_REPO" -C "$UPSTREAM_REPO" rev-parse --verify "$UPSTREAM^{commit}" >/dev/null 2>&1; then
    echo "refusing to regenerate: UPSTREAM=$UPSTREAM not found in $UPSTREAM_REPO" >&2
    exit 2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if tmp_win="$(cd "$tmp" && pwd -W 2>/dev/null)"; then
    tmp_win="$(printf '%s' "$tmp_win" | tr '\\' '/')"
else
    tmp_win="$tmp"
fi

mkdir -p "$tmp/up/raw" "$tmp/up/nanobot/nanobot" "$tmp/current/nanobot"
git -c "safe.directory=$UPSTREAM_REPO" -C "$UPSTREAM_REPO" archive "$UPSTREAM" nanobot pyproject.toml README.md \
    | /usr/bin/tar -x -C "$tmp/up/raw"
cp -a "$tmp/up/raw/nanobot/." "$tmp/up/nanobot/nanobot/"
cp "$tmp/up/raw/pyproject.toml" "$tmp/up/nanobot/pyproject.toml"
cp "$tmp/up/raw/README.md" "$tmp/up/nanobot/README.md"

cp -a nanobot/nanobot "$tmp/current/nanobot/"
cp nanobot/pyproject.toml "$tmp/current/nanobot/pyproject.toml"
cp nanobot/README.md "$tmp/current/nanobot/README.md"

/usr/bin/find patches -maxdepth 1 -type f -name '*.patch' -delete

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

    if [[ -f "$left" && -f "$right" ]]; then
        diff_args=("$left" "$right")
    elif [[ -f "$left" ]]; then
        diff_args=("$left" /dev/null)
    else
        diff_args=(/dev/null "$right")
    fi

    {
        echo "# nanobot baseline: $UPSTREAM_VERSION"
        echo "# upstream commit: $UPSTREAM"
        echo
        git diff --no-index "${diff_args[@]}" 2>/dev/null || true
    } \
        | sed \
            -e "s|$tmp/up/|a/|g" \
            -e "s|$tmp/current/|b/|g" \
            -e "s|$tmp_win/up/|a/|g" \
            -e "s|$tmp_win/current/|b/|g" \
            -e "s|a/a/|a/|g" \
            -e "s|b/b/|b/|g" \
            -e "s|a/b/|a/|g" \
            -e "s|b/a/|b/|g" \
        > "patches/$name"
    echo "  patches/$name"
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
    if [[ -f "$left" && -f "$right" ]] && cmp -s "$left" "$right"; then
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
python patches/check_exact_reconstruction.py \
    --repo "$python_repo" \
    --upstream-repo "$python_upstream_repo" \
    --upstream "$UPSTREAM" \
    --version "$UPSTREAM_VERSION"

echo "-> done. Review with: git diff -- patches/"
