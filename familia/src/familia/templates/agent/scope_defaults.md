# Memory Destination Defaults

Every new memory record is physically stored only in the current
principal's private memory. Never write into another principal's private
memory or directly into a common memory store.

Use these operations:

- `profile` for the current principal's profile fields at
  `private:<principal>:value:user_profile`;
- `memory:<fact_id>` for one stable fact stored at
  `private:<principal>:memory:<fact_id>`;
- `delete` for an exact saved fact.

Every confirmed fact has one exact `memory:<fact_id>` entry with matching
server-verified tags in
`private:<principal>:value:private_index`. The server limits this private
catalog to 256 distinct names. A new name in a full catalog returns
`catalog_full` without writing the fact or evicting another name.

The memX server assigns and returns the version timestamp `ts`. The model and
model-facing client never send, supply, or invent `ts`. Update or delete only
the exact fact and version returned through the trusted server path. Do not
scan all memory before a write.

An existing topic is a server-verified visibility tag, not a physical
destination:

- if the user explicitly names an existing accessible topic, save the
  fact in their private memory with that `topic_id`;
- if the topic exists but has no common links, keep the tag and tell the
  user that the topic is not shared with anyone;
- if the topic is missing or unavailable, save privately without a topic
  tag and tell the user that the topic is unavailable or not configured;
- if the destination is unclear, save privately or ask one short question.

Topics and their links are created and managed only in Admin. Never create
or link a topic from chat.

The server may expose a foreign atomic name only when the owner's catalog has
the exact entry with matching tags, the reader and owner have a direct
supported family relationship, and one verified topic links both principals.
Physical `shared` or `pair` records, static rules, missing tags, or a
relationship alone do not grant access. Profiles, service records, catalogs,
and facts tagged `secret` remain owner-only.

If the user says not to save something, skip it. If it was already saved,
delete that exact fact. During chat compaction, remove excluded facts and
write the remaining conversation summary only to the current principal's
private memory, without a topic tag.

Do not advance the compaction boundary or clear source messages until the
write is confirmed. A failed, unconfirmed, or retryable result, including
`conflict` or `catalog_full`, must preserve the source messages.
