#!/usr/bin/env bash
# RP-090 plan/apply wrapper. Apply is confined to a marked RP-010 isolated
# restore; real-data authorization and mutation remain the RP-100 gate.

set -euo pipefail

SNAPSHOT=""
TARGET=""
SOURCE_ROOT=""
MANIFEST=""
JOURNAL=""
CLASSIFICATIONS=""
APPLY=false
JSON=false

usage() {
    echo "usage: $0 --snapshot DIR --target DIR --manifest FILE --journal FILE [--source-root DIR] [--classifications FILE] [--apply] [--json]" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --snapshot|--target|--source-root|--manifest|--journal|--classifications)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            case "$1" in
                --snapshot) SNAPSHOT="$2" ;;
                --target) TARGET="$2" ;;
                --source-root) SOURCE_ROOT="$2" ;;
                --manifest) MANIFEST="$2" ;;
                --journal) JOURNAL="$2" ;;
                --classifications) CLASSIFICATIONS="$2" ;;
            esac
            shift 2
            ;;
        --apply) APPLY=true; shift ;;
        --dry-run) APPLY=false; shift ;;
        --json) JSON=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

if [[ -z "$SNAPSHOT" || -z "$TARGET" || -z "$MANIFEST" || -z "$JOURNAL" ]]; then
    echo "migration=refused reason=required_paths_missing" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
ARGS=(
    --snapshot "$SNAPSHOT"
    --target "$TARGET"
    --manifest "$MANIFEST"
    --journal "$JOURNAL"
)
[[ -z "$SOURCE_ROOT" ]] || ARGS+=(--source-root "$SOURCE_ROOT")
[[ -z "$CLASSIFICATIONS" ]] || ARGS+=(--classifications "$CLASSIFICATIONS")
[[ "$APPLY" != true ]] || ARGS+=(--apply)
[[ "$JSON" != true ]] || ARGS+=(--json)

export PYTHONPATH="$REPO_ROOT/familia/src:$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m familia.memory_migration "${ARGS[@]}"
