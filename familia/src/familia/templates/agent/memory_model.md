# Memory Model — How to Explain It to the User

Keep explanations short and concrete. Translate these rules to the user's
language.

## Physical ownership

Every new fact from a conversation is physically stored only in the current
principal's private memory. A person cannot write into another person's
private memory. A family relationship, an administrator role in chat, or a
mention of another person never changes the owner of the write.

## Records and operations

Use `profile` for the current principal's profile at
`private:<principal>:value:user_profile`. Store each ordinary fact at
`private:<principal>:memory:<fact_id>` and address it by the stable name
`memory:<fact_id>`. Every confirmed fact has one exact entry with the same
name and server-verified tags in the owner's private catalog
`private:<principal>:value:private_index`. The server limits that catalog to
256 distinct names. A new name in a full catalog returns `catalog_full`
without writing the fact or evicting an existing name.

The memX server assigns and returns the version timestamp `ts`. The model and
model-facing client never send, supply, or invent `ts`. Update or delete only
the exact `fact_id` and version returned through the trusted server path; do
not read or compare all memory before every write. Human dates such as “next
Tuesday” remain part of the fact's content and do not replace the technical
version timestamp.

## Topics

A topic is a server-verified visibility tag identified by `topic_id`. It
does not move the record out of the owner's private memory.

- An explicit existing accessible topic is attached to the private fact.
- An existing topic with no common links may still be attached; tell the
  user that nobody else is currently linked to it.
- A missing or unavailable topic is not attached. Save privately and tell
  the user that the topic is unavailable or not configured.
- If the destination is unclear, save privately or ask one short question.

Topics and their links are created and managed only in Admin. The model must
never create a topic automatically.

Facts about another person still belong to the speaker's private memory.
The server may expose a foreign atomic name only when the owner's catalog has
the exact `memory:<fact_id>` entry with matching tags, the reader and owner
have a direct supported family relationship, and one verified topic links
both principals. A relationship, topic, physical `shared` or `pair` record,
static rule, or missing tag never grants access on its own. Profiles,
service records, catalogs, and facts tagged `secret` remain owner-only.

## “Do not save” and compaction

If the user says not to save something, skip that fact. If the exact fact
was already saved, delete it. If exclusion is requested for a chat that will
be compacted, clean the excluded fact from the compacted material and do not
write it.

Compaction is unconditional about ownership: its summary belongs only to the
current principal's private memory and has no topic tag.

Do not advance the compaction boundary or clear source messages until the
memory write is confirmed. A failed, unconfirmed, or retryable result,
including `conflict` or `catalog_full`, must preserve the source messages for
a safe retry or explicit resolution.

## How to answer common questions

- “Who owns this memory?” — the person speaking in the current chat.
- “Can another person see it?” — only if the exact catalog entry, a direct
  family relationship, and one verified common topic all permit reading the
  matching atomic fact.
- “Can you save directly for someone else?” — no; the speaker may save a
  fact only in their own private memory.
- “Can you create a topic?” — no; topics are managed only in Admin.
