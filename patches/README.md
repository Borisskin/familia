# Upstream patches

Diffs of every nanobot upstream file that familia touches, against the
subtree merge commit `328a386` (nanobot@0806ac02c).

Phase 9 status: this directory is an upgrade aid, not the current source
of truth for familia integration. Do not blindly regenerate or re-apply
all patches before Phase 10 hunk-by-hunk audit.

Regenerate only after choosing the Phase 10 upstream baseline:

```bash
UPSTREAM=328a386
for f in nanobot/nanobot/agent/loop.py \
         nanobot/nanobot/agent/context.py \
         nanobot/nanobot/agent/memory.py \
         nanobot/nanobot/agent/tools/message.py \
         nanobot/nanobot/channels/base.py \
         nanobot/nanobot/cli/commands.py; do
    name=$(echo "$f" | sed 's|nanobot/nanobot/||;s|/|_|g;s|\.py$|.patch|')
    git diff $UPSTREAM -- "$f" > patches/"$name"
done
git diff $UPSTREAM -- nanobot/pyproject.toml > patches/pyproject.patch
```

## Why they exist

Familia is a subtree merge of upstream nanobot; the long-term plan is to
keep patches minimal so subtree pulls stay painless. Phases 1-9 moved the
integration toward neutral extension points, so several old patch files are
now stale or partially absorbed.

## Phase 9 triage

| file | Phase 9 status | Phase 10 action |
|------|----------------|-----------------|
| `command_builtin.patch` | removed in Phase 9; target `nanobot/nanobot/command/builtin.py` no longer exists | none |
| `cli_commands.patch` | stale as a patch; direct familia wiring moved behind `nanobot.runtime_adapters` entry points | rebuild from current neutral CLI boundary only if a real delta remains |
| `pyproject.patch` | suspicious residue, but version metadata may be intentional fork state | audit against chosen upstream baseline; keep only real dependency/version deltas |
| `agent_context.patch` | likely absorbed by `ContextExtension` and product template move | hunk-by-hunk audit; keep only neutral context extension deltas |
| `agent_loop.patch` | live/partly absorbed behavioral delta | hunk-by-hunk audit; keep only tool/inbound/outbound/audit extension point deltas |
| `agent_memory.patch` | likely absorbed except neutral cursor/history migration details | hunk-by-hunk audit; do not reintroduce Dream memory into core |
| `agent_tools_message.patch` | likely absorbed by outbound guard injection | hunk-by-hunk audit; keep only neutral outbound guard deltas |
| `channels_base.patch` | likely absorbed by inbound enricher protocol | hunk-by-hunk audit; keep only neutral channel enrichment deltas |

`channels_vk.patch` was removed in Phase 7 of the nanobot separation
track: VK is now a familia-owned channel adapter registered through the
neutral channel registry boundary, so regenerating patches must not
re-create `nanobot/nanobot/channels/vk.py`.
