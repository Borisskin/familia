# Nanobot patches

This directory contains generated deltas from the pinned upstream nanobot
baseline to the current vendored nanobot tree in this repository.

Baseline:

- upstream version: `0.3.0`;
- upstream repo: sibling `../nanobot` next to this repository by default;
- upstream commit: `3f602fbc8c104b5af27aa4d3520e7dcef2fa70ec`.

The upstream package layout is `nanobot/...`. This repository vendors the
package under `nanobot/nanobot/...`, so patch paths are normalized to the
vendored layout.

## Regenerate

```bash
bash patches/regenerate.sh
```

Optional overrides:

```bash
UPSTREAM_REPO=../nanobot \
UPSTREAM=3f602fbc8c104b5af27aa4d3520e7dcef2fa70ec \
UPSTREAM_VERSION=0.3.0 \
bash patches/regenerate.sh
```

Validate metadata, applicability, ownership closure, and exact reconstruction:

```bash
bash patches/validate_baseline.sh
```

## Scope

Patch files are generated for runtime nanobot package deltas and
`nanobot/pyproject.toml`; `nanobot/README.md` is also inside the declared
comparison scope and currently matches the pinned baseline. The checker proves
that the sorted patch set reconstructs the current non-ignored worktree scope
with the exact path set, blob bytes, and Git modes. Patch applicability alone is
reported separately and is not accepted as equality.

`ownership.yaml` is JSON-compatible YAML with one row per current delta path.
Every patch hunk has a category (`familia-invariant`, `upstream-alignment`,
`generated-noise`, or `unknown`) and an explicit release decision owner. The
checker rejects missing/stale paths, missing hunk coverage, filename drift, and
direct imports of Familia from nanobot core.

Phase 10 must still do hunk-by-hunk review:

- keep neutral extension points that upstream lacks;
- delete hunks already absorbed by upstream;
- keep product-specific channel implementations and prompts outside nanobot core;
- do not use old patch names as proof that a behavior is still live.

## Notable baseline deltas

| Patch area | Meaning against `0.3.0` |
| --- | --- |
| `agent___init__.patch`, `runtime_adapters.patch` | Current Familia tree keeps neutral runtime extension points without product imports. |
| `agent_loop.patch`, `agent_memory.patch`, `session_manager.patch` | Core lifecycle, persistence, and identity deltas against nanobot `0.3.0`. |
| `channels_telegram_runtime.patch`, `channels_manager.patch`, `channels_registry.patch` | Channel discovery and Telegram runtime integration remain explicit. |
| `agent_tools_*.patch`, `audio_*.patch`, `security_workspace_access.patch` | Neutral tool, transcription, and workspace boundaries owned by Familia. |
| `pyproject.patch` | Fork/version/dependency delta against upstream `0.3.0`; audit before changing package metadata. |

`command_builtin.patch` records the synchronized `/new` archive-and-clear flow,
save rollback, and the injected Dream restore policy in
`nanobot/nanobot/command/builtin.py`. These are Familia-owned behavioral
invariants in `ownership.yaml`.
