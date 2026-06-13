# Nanobot patches

This directory contains generated deltas from the pinned upstream nanobot
baseline to the current vendored nanobot tree in this repository.

Baseline:

- upstream version: `0.1.5.post2`;
- upstream repo: `D:\chat\nanobot`;
- upstream commit: `950dddec499fbbe0353e997158c99808f0bb41e1`;
- reference unpacked tree: `D:\Boris\Downloads\nanobot-0.1.5.post2`.

The upstream package layout is `nanobot/...`. This repository vendors the
package under `nanobot/nanobot/...`, so patch paths are normalized to the
vendored layout.

## Regenerate

```bash
bash patches/regenerate.sh
```

Optional overrides:

```bash
UPSTREAM_REPO=/d/chat/nanobot \
UPSTREAM=950dddec499fbbe0353e997158c99808f0bb41e1 \
UPSTREAM_VERSION=0.1.5.post2 \
bash patches/regenerate.sh
```

Validate metadata and patch headers:

```bash
bash patches/validate_baseline.sh
```

## Scope

Patch files are generated for runtime nanobot package deltas and
`nanobot/pyproject.toml`. They are an upgrade/audit aid, not an instruction
to blindly apply every hunk.

Phase 10 must still do hunk-by-hunk review:

- keep neutral extension points that upstream lacks;
- delete hunks already absorbed by upstream;
- do not reintroduce VK, Dream memory, heartbeat, or familia product templates
  into nanobot core;
- do not use old patch names as proof that a behavior is still live.

## Notable baseline deltas

| Patch area | Meaning against `0.1.5.post2` |
|------------|--------------------------------|
| `command_*.patch` | Upstream slash-command package exists in `0.1.5.post2`; current familia tree deletes it after the natural-language/session refactor. |
| `runtime_adapters.patch` | Current familia tree adds neutral runtime adapter discovery for optional product wiring. |
| `agent_outbound.patch`, `channels_inbound.patch`, `bus_callbacks.patch` | Current familia tree adds neutral extension point modules that are absent in upstream `0.1.5.post2`. |
| `agent_context.patch`, `agent_loop.patch`, `agent_memory.patch`, `agent_tools_message.patch`, `channels_base.patch`, `cli_commands.patch` | Core behavioral deltas that need Phase 10 hunk-by-hunk audit. |
| `pyproject.patch` | Fork/version/dependency delta against upstream `0.1.5.post2`; audit before changing package metadata. |

`channels_vk.patch` is intentionally absent. VK is no longer a nanobot core
file; it is owned by `familia.channels.vk`.
