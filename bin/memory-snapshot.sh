#!/usr/bin/env bash
# RP-010 read-only memory capture. Physical sources are supplied only through
# an explicit operator-owned config; this script never guesses ~/.nanobot or
# Redis credentials/endpoints.

set -euo pipefail

READ_ONLY=false
OUTPUT=""
CONFIG="${MEMORY_SNAPSHOT_CONFIG:-}"

usage() {
    echo "usage: $0 --read-only --output PATH [--config PATH]" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --read-only)
            READ_ONLY=true
            shift
            ;;
        --output)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            OUTPUT="$2"
            shift 2
            ;;
        --config)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            CONFIG="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

if [[ "$READ_ONLY" != true ]]; then
    echo "snapshot=invalid reason=explicit_read_only_required" >&2
    exit 2
fi
if [[ -z "$OUTPUT" || -z "$CONFIG" ]]; then
    echo "snapshot=invalid reason=output_and_config_required" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
exec python3 "$REPO_ROOT/scripts/compare_memory_state.py" \
    _capture \
    --config "$CONFIG" \
    --output "$OUTPUT" \
    --tool-path "$REPO_ROOT/bin/memory-snapshot.sh"
