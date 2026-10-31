# Guarded Notion mirror revisions

The existing Wiki-to-Notion workflow stays disabled until separately authorized
real-target acceptance. Its legacy whole-tree mode continues to hold mutations.
This release adds an explicit one-source executor, not automatic adoption of the
historical signature-only entries or a claim that all mirroring is restored.

## Explicit execution

Use a clean, current, verified private-vault checkout. `VAULT_ROOT` selects that
checkout. Set the existing `PRIVATE_VAULT_TOKEN` and `NOTION_API_KEY`; retain the
existing state-key configuration. The selected source must match its committed
Git bytes and remain within the existing mirror scope. Source paths and target
IDs belong only in private operational records, never public logs.

For the public workflow, submit only `revision_request`, a random UUIDv4. The
existing private state must contain one reviewed `execution_request` with
`request_id`, private `source_path`, exact `source_signature`, `parent_page_id`
and boolean `allow_new_page`. `--execute-request <UUID>` loads that private
record and fails if source bytes differ from the reviewed version. Never put
source paths, page IDs or source-content hashes in public dispatch inputs or
logs. This is one explicit request in existing state, not a new queue or outbox.

For direct execution inside a private environment,
`--execute-revision <source-relative-path>` appends a labeled source revision to
the existing exact managed target. Old blocks, human text, titles and relations
are never edited, archived or deleted. Each batch includes a stable operation
marker, visible source-version/batch heading and end marker. At most 97 source
blocks plus those three markers fit a Notion 100-block request.

Existing entries require exact `page_id`, `parent_page_id` and
`managed_block_baseline` identities/hashes. Historical entries with only source
signatures remain held. A title or pre-write hash is not authority to adopt a
human page and is not a Notion CAS operation. Unexpected edits, nested content,
duplicate targets or incomplete pagination remain held, preserving all content.
No baseline migration or mass adoption is performed by this command.

`--allow-new-page` permits a genuinely new source only under an explicitly bound
`--parent-page-id` (or the existing configured parent). It does not bypass a
legacy entry and cannot replace or duplicate an existing same-title page. Do not
create a replacement page to manufacture successful acceptance of an old source.

## Durable operation and recovery

The existing private `.github/state/notion-wiki-mirror-state.json` holds request
payloads, source snapshot/signature, owner, batch identity and target baseline.
The existing private commit helper publishes and reads back `prepared`, then
`dispatched`, before the single non-idempotent call. No request is sent after a
failed checkpoint. The executor never retries a lost/ambiguous result. A fresh
runner seeing a prepared/dispatched/unknown operation cannot resend it.

After a conclusive complete target readback, `verified`, target block IDs and
baseline are committed privately. Source signature advances only for the final
batch. Local state bytes must equal the committed state and the checkout HEAD
must match private remote main before execution or recovery persistence; this
prevents a failed final push from becoming a later false no-op.

`--recover-intents` performs target reads only and reports aggregate outcomes.
`--recover-intents --persist-recovery` additionally commits conclusive verified
outcomes to private state. Neither recovery mode sends Notion mutations. Partial,
missing, duplicate or human-modified results remain held; an absent marker is
not proof that a prior request never reached the server. Verified historical
operations count separately as `already_verified` recorded checkpoints, not a new live verification of their
old target prefix after later revisions were appended.

If source bytes change while an operation is unresolved, resolve the original
operation first. Never reconstruct its payload from a newer source. The original
bounded request is stored privately. Confirmed earlier batches are skipped;
future batches require their own durable attempt and target readback.

## Acceptance still required

Synthetic tests cover server commit with lost create/append response, restart,
checkpoint failure, duplication, partial writes, concurrent human edits, nested
content, strict pagination and multi-batch completion. They do not prove a real
Notion write or deployment. The primary task must review a precise source/target
manifest, perform one authorized real append and readback, repeat without
duplicates, recover from a fresh checkout, and only then coordinate the original
schedule and natural-run/Mac-independent acceptance. Keep the workflow disabled
throughout code publication and synthetic testing.
