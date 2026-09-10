#!/usr/bin/env bash
# RP-090 plan/apply wrapper. Apply is confined to a marked RP-010 isolated
# restore; real-data authorization and mutation remain the RP-100 gate.

set -euo pipefail

SNAPSHOT=""
INSTALL_ROOT=""
WORKSPACE=""
PRINCIPALS=""
TARGET_CONFIG=""
MANIFEST=""
JOURNAL=""
CLASSIFICATIONS=""
APPLY=false
JSON=false

usage() {
    echo "usage: $0 --snapshot DIR --install-root DIR --workspace DIR --principals FILE --target-config FILE --manifest FILE --journal FILE [--classifications FILE] [--apply] [--json]" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --snapshot|--install-root|--workspace|--principals|--target-config|--manifest|--journal|--classifications)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            case "$1" in
                --snapshot) SNAPSHOT="$2" ;;
                --install-root) INSTALL_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --principals) PRINCIPALS="$2" ;;
                --target-config) TARGET_CONFIG="$2" ;;
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

if [[ -z "$SNAPSHOT" || -z "$INSTALL_ROOT" || -z "$WORKSPACE" || -z "$PRINCIPALS" || -z "$TARGET_CONFIG" || -z "$MANIFEST" || -z "$JOURNAL" ]]; then
    echo "migration=refused reason=required_paths_missing" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
ARGS=(
    --snapshot "$SNAPSHOT"
    --install-root "$INSTALL_ROOT"
    --workspace "$WORKSPACE"
    --principals "$PRINCIPALS"
    --target-config "$TARGET_CONFIG"
    --manifest "$MANIFEST"
    --journal "$JOURNAL"
)
[[ -z "$CLASSIFICATIONS" ]] || ARGS+=(--classifications "$CLASSIFICATIONS")
[[ "$APPLY" != true ]] || ARGS+=(--apply)
[[ "$JSON" != true ]] || ARGS+=(--json)

export PYTHONPATH="$REPO_ROOT/familia/src:$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m familia.memory_migration "${ARGS[@]}"
