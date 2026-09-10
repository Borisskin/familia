# Policy and access control

Familia denies access by default. Every new record belongs to the speaker and
is physically stored only in that person's private memory.

## Family graph

The family graph stores principals and relationships:

```json
{
  "nodes": [
    {"id": "person_a", "type": "principal"},
    {"id": "person_b", "type": "principal"}
  ],
  "edges": [
    {"from": "person_a", "to": "person_b", "rel": "spouse_of"}
  ]
}
```

Five relationships are supported:

- `spouse_of`;
- `parent_of`;
- `guardian_of`;
- `owner_of`;
- `caregiver_of`.

A relationship is only one input to read authorization. It does not expose a
related person's entire memory or chat history, and it never permits writing
to that person's private memory. Read authorization depends on the existence
of a direct supported family relationship between the principals; the
direction in which its edge is stored neither grants nor removes access.

For example, `spouse_of` requires an exact private-catalog entry, a direct
relationship, and a verified common topic before it permits reading a foreign
`memory:<fact_id>`. `guardian_of` without a common topic does not grant access:
reading the foreign fact is denied. No family relationship replaces the
catalog and topic checks.

The graph is changed only in Admin. The model cannot create principals or
change relationships from chat.

## Topics

A topic is an existing entry in the topic graph managed by Admin. Memory uses
it only as a server-verified `topic_id` visibility tag. It never changes the
fact's physical storage location.

- An explicitly named existing accessible topic is attached to the private
  fact.
- If the topic exists but has no common links, the tag is kept and the person
  is told that the topic is not shared with anyone.
- If the topic is missing or unavailable, the fact is saved privately without
  a topic tag; the person is told that the topic is unavailable or not
  configured.
- If the destination is unclear, the record stays private or the model asks
  one short question.

Topics and topic links are created only in Admin. Automatic topic creation
from chat is forbidden.

Legacy physical `shared` and `pair` storage, a static rule, or a missing topic
tag never grants a separate foreign-read permission.

## Write, update, and delete

A principal can write only to their own private memory. An administrator role
in chat does not permit writing for another person. Mentioning another person
does not change ownership either: that fact remains in the speaker's memory.

An ordinary fact has a stable `fact_id`. The memX server assigns and returns
the technical `ts` version; the model and client do not send, assign, or
create it. Updates and deletes target the exact `fact_id` and version
previously returned through the trusted server path, without scanning all
memory before every write.

“Do not save” means:

- skip a fact that has not been written;
- delete the exact fact if it was already saved;
- remove excluded material during chat compaction and do not write it.

A chat compaction result always goes only to the current principal's private
memory and carries no topic tag.

If a write fails or remains unconfirmed, the source messages remain available
and the compaction boundary does not move. A conflict can be retried safely
after a fresh point read; a full catalog does not lose the message or evict an
existing name.

## `policy.yaml`

The server applies `policy.yaml` after identity is established and before a
read or write. The baseline decision is deny. Policy may permit reading a
specific fact only when the relationship, existing topic, topic links, and
server tag all match. Policy cannot permit a write to foreign private memory.
Foreign profiles, service keys, catalogs, and facts tagged `secret` remain
unavailable even when a topic is shared.

## Audit log

Every policy decision is appended to `audit.jsonl`. The record contains time,
principal, action, destination, decision, and reason. Secrets and fact values
are not logged.

Graphs and topics are edited in Admin, so their changes must also be tied to
an operator action in the audit log.

## Children and dependants

The `guardian_of` relationship does not grant automatic or full access in
either direction. A single foreign `memory:<fact_id>` can be read only when a
direct supported family relationship exists regardless of storage direction,
the catalog entry and tags match exactly, and a verified common topic links
the principals. The guardian cannot write for the child.
