# Security model

Vulnerability reporting rules and supported versions are in the repository
root [`SECURITY.md`](../../SECURITY.md). This document describes the security
boundaries of an installed Familia instance.

## Audience model

- A person in chat can write only to their own private memory.
- Another adult, child, guardian, or caregiver sees only specific facts whose
  exact owner-catalog entry, direct supported family relationship, and
  verified existing common-topic tag all match.
- An administrator role in chat does not permit reading or changing another
  person's private memory.
- The VM operator and `root` can technically access files, keys, and Redis
  data. This is a trusted administrative boundary, not a chat principal's
  permission.

## Guarantees

1. Access is denied by default.
2. Every new record is physically stored only in the speaker's private
   memory.
3. Writes to foreign private memory and direct writes to a common store are
   forbidden.
4. A family relationship alone neither opens another person's memory nor
   grants write permission.
5. Topics and their links are created only in Admin; the model cannot create
   a topic.
6. The server verifies topic existence, principal access, and links before
   attaching a tag. An unavailable topic produces a private record without a
   tag and a notice to the person.
7. A foreign `memory:<fact_id>` is readable only when an exact catalog entry
   has the same tags, the principals have a direct family relationship, and
   they share the topic. Legacy `shared`/`pair` storage, a static rule, and a
   missing tag grant no access.
8. Profiles, service keys, private catalogs, and facts tagged `secret` are
   owner-only regardless of relationship or topic.
9. The memX server assigns and returns the `ts` version; the model and client
   do not send, assign, or create it. Updates and deletes use only the exact
   `fact_id` and version previously returned by the trusted server.
10. Chat compaction writes only to the current principal's private memory and
    uses no topic. If a write fails or remains unconfirmed, the source
    messages remain available and the compaction boundary does not move.
11. A corrupt or incomplete policy fails closed instead of widening access.
12. Access decisions are audited without fact values or secrets.
13. Private files and keys on the VM must be readable only by `root`.

## Non-guarantees

- `root`, the VM owner, or malware with equivalent privileges can read all
  data.
- Telegram, VK, and the language-model provider see content sent to them.
- Deleting a fact cannot retract information already read by a person or sent
  to an external provider.
- Familia policy does not replace OS updates, hosting-account protection,
  backup protection, or SSH-key hygiene.

## In scope: please report

- reading a foreign private fact without an exact catalog entry, direct
  relationship, and matching verified common topic;
- writing or deleting in foreign private memory;
- bypassing server verification of a topic tag;
- creating or linking a topic from chat;
- widening access through an administrator role in chat;
- leaking a key, secret, or fact value into the audit log;
- an agent tool escaping its configured sandbox.

## Out of scope

- `root` accessing the operator's own VM;
- a principal disclosing their own data in an authorized chat;
- behavior of an external language-model provider or messenger;
- unsupported manual edits to VM data, policy, or keys.

## Reporting

Do not post live secrets or personal data in an issue. Use the private channel
listed in [`SECURITY.md`](../../SECURITY.md) and include minimal reproduction
steps plus the expected and actual policy decision.
