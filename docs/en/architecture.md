# Architecture

Familia is a fork of [nanobot](https://github.com/HKUDS/nanobot) plus a
vendored copy of [memX](https://github.com/MehulG/memX), wrapped with a
small `familia` package that wires identity, policy and ACL into the
nanobot agent loop.

## Components

### `familia/` (this repo)

The thin layer that turns nanobot from a single-user agent into a
multi-principal one.

```list
familia/src/familia/
  identity_resolver.py   binds inbound messages to principal_id
  principals.py          loads/validates principals.json
  acl.py                 family.graph + topics.graph reachability
  policy/                policy.yaml engine (audit + decisions)
  tools/                 memory_*, send_buttons, family_graph,
                         and admin-grant tooling
  bootstrap.py           single insertion point into nanobot.agent.loop
  audit.py               append-only JSONL log + 50 MB × 5 rotation
  cli/                   familia CLI (graph_admin, audit_view)
```

### `nanobot/` (forked subtree)

The upstream agent loop. We patch a handful of files to hand control to
the `familia` layer at well-known points (channel ingress, prompt
build, tool dispatch, memory write). Patches live in `patches/`.

### `memx/` (vendored subtree)

FastAPI + Redis store with a per-principal ACL. Each principal has a unique
API key (`memx_key`); requests are authenticated by `X-API-Key`. The ACL file
(`memx-config/acl.json`) maps that key to the principal's private memory.

New memory is physically stored only for the current principal. The profile
uses `private:<principal>:value:user_profile`; each fact uses
`private:<principal>:memory:<fact_id>`, and its addressable
`memory:<fact_id>` name is stored with the same server-verified tags in the
private catalog `private:<principal>:value:private_index`. One conditional
operation changes the fact and catalog together. The catalog holds at most
256 distinct names: a new name in a full catalog returns `catalog_full`
without writing the fact or evicting an existing name.

memX assigns and returns the technical `ts` version; the model and client do
not send, assign, or invent it. Updates and deletes use only the exact version
previously returned through the trusted server path. A topic remains a
visibility tag on a private fact, not a separate shared store.

The family and topic graphs are structural data managed by Admin. Chat
compaction uses the same current principal: the result goes only to that
principal's private memory and receives no topic tag.

## Data flow (inbound message)

```text
1. Telegram / VK delivers a message to the channel adapter.
2. nanobot calls familia.identity_resolver.resolve(channel, sender_id).
3. We look up principals.json → principal_id, role.
4. set_current_actor(principal_id) is pinned for the rest of the turn.
5. The agent loop builds a prompt from shared SOUL/AGENTS/TOOLS files, the
   current principal's profile, permitted legacy `value:memory`, logical
   `memory:<fact_id>` names from their `value:private_index` without atomic
   fact values, and authorized foreign names, also without values.
6. The LLM produces a response and tool calls. The server resolves every
   write: the physical destination is always the current principal's private
   memory; a topic tag is added only after an existing accessible topic is
   verified.
7. A foreign atomic name enters the prompt only when an exact catalog entry
   has matching tags, the principals have a direct family relationship, and
   a verified common topic links them. Foreign profiles, service records, and
   secret records are not exposed.
8. Policy decisions are appended to audit.jsonl with the actor, operation,
   destination, decision, and reason.
9. Reply goes back through the same channel, same chat. No broadcast.
```

If a write fails or remains unconfirmed, the compaction boundary does not
move and the source messages remain available. A conflict permits a safe
retry after a fresh point read; a full catalog requires explicit resolution
and never loses messages.

## Storage layout

Two storage planes that intentionally don't mix:

| Plane | Purpose | Keyed by |
| --- | --- | --- |
| **Private memX memory** | a principal's profile and individual facts | `principal_id`, `fact_id` |
| **Structural graphs** | principals, relationships, topics, and links | Admin |
| **Shared files** (workspace) | bot persona, tool docs, and agent docs | one shared truth |

The security invariant is simple: chat writes only to the speaker's private
memory. A relationship alone neither opens another person's memory nor ever
permits a foreign write. A specific foreign fact is readable only through a
matching private-catalog entry, a direct supported family relationship, and
a matching common-topic tag verified by the server. Legacy physical
`shared`/`pair` storage, a static rule, or a missing tag cannot replace any
of these conditions.

## Trust boundaries

```text
                    ┌─────────────────┐
                    │ Operator laptop │  (admin .exe + WebView2Loader.dll)
                    └────────┬────────┘
                             │ SSH (port 22, key auth)
   ┌─────────────────────────┴───────────────────────────────────┐
   │  VM (Linux, root SSH only)                                  │
   │  ┌───────────────────────────────────────────────────────┐  │
   │  │ docker network: familia_default                       │  │
   │  │   familia-gateway   ──▶  memx-backend ──▶ memx-redis │  │
   │  └───────────────────────────────────────────────────────┘  │
   │  /opt/familia/{principals.json, policy.yaml, acl.json,      │
   │                .env, audit.jsonl}                           │
   └─────────────────────────────────────────────────────────────┘
                ▲                                ▲
                │                                │
   ┌────────────┴──────────┐         ┌───────────┴────────────┐
   │ Telegram channel API  │         │ LLM provider           │
   │ VK long-poll API      │         │ (OpenAI/Claude/Groq/…) │
   └───────────────────────┘         └────────────────────────┘
```

- The operator's laptop is **trusted**: it holds the SSH key.
- The VM is **trusted**: root on the host can read everything anyway.
- Telegram/VK channels are **outside the trust boundary** — they see
  inbound and outbound messages plain.
- The LLM provider is **outside the trust boundary** — it sees only the
  context that the server has already authorized for the current turn.

For the full threat model see [`security.md`](security.md).

## Why three compose stacks (and why they live together)

`docker-compose.yml`, `docker-compose.memx.yml` and
`docker-compose.cli.yml` are separate so you can stop, restart,
backup or rebuild each plane independently. In practice all three
run on the same VM and share the same docker network — the admin
app drives them as one unit.

There's also `docker-compose.exec-sandbox.yml` for nanobot's
bubblewrap sandbox dependencies (kept separate because changing
sandbox config shouldn't restart the gateway).
