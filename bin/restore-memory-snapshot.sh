#!/usr/bin/env bash
# RP-010 isolated restore. The target must already exist, carry the restrictive
# ownership marker, and prove a separate loopback Redis fixture/instance.

set -euo pipefail

INPUT=""
TARGET=""

usage() {
    echo "usage: $0 --input SNAPSHOT_DIR --target ISOLATED_TARGET" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            INPUT="$2"
            shift 2
            ;;
        --target)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            TARGET="$2"
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

if [[ -z "$INPUT" || -z "$TARGET" ]]; then
    echo "restore=refused reason=input_and_target_required" >&2
    exit 2
fi
if [[ "$TARGET" != /* ]]; then
    if [[ -z "${MEMORY_RESTORE_ROOT:-}" ]]; then
        echo "restore=refused reason=relative_target_requires_memory_restore_root" >&2
        exit 2
    fi
    TARGET="${MEMORY_RESTORE_ROOT%/}/$TARGET"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
exec python3 "$REPO_ROOT/scripts/compare_memory_state.py" \
    _restore \
    --input "$INPUT" \
    --target "$TARGET" \
    --tool-path "$REPO_ROOT/bin/restore-memory-snapshot.sh"
