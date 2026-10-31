# Guarded Notion mirror revisions

The existing Wiki-to-Notion workflow stays disabled until separately authorized
real-target acceptance. Its legacy whole-tree mode continues to hold mutations.
The explicit one-source executor remains compatible. The bounded bulk executor
adds original-page binding and parent-first hierarchy creation from an approved
private scope. Neither code publication nor synthetic tests establish that the
full mirror or its schedule has been restored.

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
human page and is not a Notion CAS operation. Unexpected edits, duplicate targets or incomplete pagination remain held,
preserving all content. Nested content is read recursively, including table
cells, before hashing. No baseline migration is performed by the one-source
command; reviewed bulk mode can bind original pages as described below.

`--allow-new-page` permits a genuinely new source only under an explicitly bound
`--parent-page-id` (or the existing configured parent). It does not bypass a
legacy entry and cannot replace or duplicate an existing same-title page. Do not
create a replacement page to manufacture successful acceptance of an old source.

## Durable operation and recovery

The packaged `commit_private_vault.py` helper runs directly in an isolated
process group with a bounded timeout; no vault-only launcher is required. Public
failure diagnostics print only allowlisted checkpoint codes. Private paths,
request bodies and raw helper output remain suppressed.

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

## Bounded bulk migration and the existing cron

`--bulk-request <UUIDv4>` consumes the existing private state's `bulk_request`.
The public workflow accepts this opaque identifier, never a private source path,
target ID, content hash or arbitrary scope override. The unchanged cron and
repository-dispatch entry use `--execute-bulk`, which requires the same approved
private configuration. Keep the workflow disabled until real-target acceptance;
adding the consumer does not authorize its activation.

The private configuration contains `request_id`, `enabled: true`, explicit
`parent_page_id`, `include_patterns`, `exclude_patterns`, `source_snapshot` and a
sorted `sources` list of `{path, signature}`. Initial migration freezes these
exact source versions. `follow_source_changes: true`, when explicitly approved,
selects the current committed tree within the same approved scope for recurring
increments. The earlier formal scope remains AGENTS/README/Wiki/Schema excluding
the maintenance log. Project Markdown and exact source dependencies require the
separately reviewed scope; no blanket Raw or program-state expansion occurs.

Source bytes come from fixed Git objects. A sparse, fresh runner does not need
all project documents checked out locally. If a local source is present it must
match the committed bytes. When shallow history lacks the original checkpoint
commit, current Git source bytes must still match the entire reviewed manifest.
A private checkpoint commit advancing HEAD does not itself cause new content.

The executor reuses `build_nodes`, existing entries/intents and the packaged
private commit helper. A unique child title plus correct original parent and
matching source-rendered body establishes a historical binding; a title alone
cannot. Original root, directory and file IDs are retained. Additional human
blocks are included in the baseline. A previously known page that is missing,
renamed, moved, ambiguous or body-mismatched is held rather than replaced.

Every new directory is created and read back before dependent children. Changes
append a labeled version to the original file page, without modifying any old
block. Native table versions preserve the earlier plain-text version; append-only
history means readers will see older content before later labeled revisions.
Tables repeat their header when split. Payloads respect top-level, recursive
block and encoded size budgets. Existing native/human nested content is read
fully before comparison; newly appended human blocks can be retained in an
extended baseline when every previously recorded block is unchanged. A changed
old block holds the object without deleting human content. This is preservation
and post-write verification, not a claim of server-side compare-and-swap.

`max_batches` (default 100, maximum 1000) and `max_seconds` (default 900, maximum
1200) bound a pass. An individual HTTP/checkpoint call has its own timeout, so a
pass can finish that in-flight operation after the soft deadline. Continue
manual passes over the finite initial manifest until deferred count is zero;
do not wait one day per small batch. The same executor then handles actual source
increments. A pass with deferred nodes, held objects or held assets reports
`bulk_incomplete` and exits 2; only a fully drained verified pass reports
`bulk_verified` with exit 0. Counts and exact held paths remain in existing
private state; public output contains aggregate counts only.

A context-bound unknown outcome fences its original source/target and dependent
children. Unrelated branches can proceed. Legacy unknown outcomes without an
object identity still hold the entire runner until read-only recovery supplies
conclusive evidence. Prepared/dispatched/unknown intents are never automatically
resent. Checkpoint/publication failures stop the runner rather than being
misclassified as one object's source error.

Only verified intents may drop redundant request/prefix/baseline copies after
saving a receipt hash and fixed private Git `receipt_commit`; their original
full receipts remain recoverable from that exact history commit. Pending/unknown recovery payloads are never compacted. The current entry
retains the full most recent managed block baseline. Compacted historical
receipts are recorded prior verifications, not new target readbacks.

Required real acceptance remains: original-page adoption, genuine missing
hierarchy, true source update, human edit preservation, native table/attachments,
clean-runner unknown recovery, no-op, finite complete scope and an actual natural
run with target readback. Tests cannot substitute for those production checks.

## Native assets and completion counts

The bulk caller actually invokes `notion_private_assets.prepare/publish` for
tracked binary references outside fenced examples. Upload receipts live under
existing private `state.assets[sha256]`; each callback must return only after
private publication/readback. The library uses its documented upload API version
without migrating the source/database clients. Block requests containing
`file_upload` use that same upload-compatible version.

Native target file bytes are downloaded over the guarded Notion transport and
hashed. Their actual hash/size must match the source-pinned private receipt;
removing a temporary URL is not proof of binary equality. Historical source
receipts remain usable when shallow history no longer includes an older blob.
Human native files are also hashed from actual bytes, without inventing Git
provenance. Captions/names and recursive human content remain in the baseline.

`assets_ready`, `assets_held` and `assets_fallback` distinguish source delivery
from asset delivery. A readable source with a private original-file link is not
an uploaded Notion attachment. An asset hold makes the command nonzero even if
all source pages were verified. No source extension is permanently excluded;
workspace capabilities, multipart requirements and bounded-read limits remain
explicit holds. Git fallback needs the reader's private-repository access.

A rendered-content signature participates in revision/no-op decisions. Source
bytes unchanged with a legacy pipe table still receive a new native table
version; plain sources whose rendered blocks are identical retain `legacy-v1`
and remain genuine no-ops. Revision generations distinguish a real source
reversion from an already verified historical operation, so reverting to an old
source cannot silently skip the required current revision. Unchanged original
Markdown fallback links use the approved private main entry, avoiding a new
revision merely because an unrelated state-only Git commit changed HEAD.

Synthetic integration fixtures exercise the actual upload caller, remote
checkpoints, upload-ID reuse, default API fields, target binary change detection,
asset rejection with a clickable original, and repeated no-op. These are not
real production uploads or proof that every referenced resource is already
available. Local Markdown/Obsidian/HTML references and recursive HTML/CSS/JSON bundle
dependencies are inventoried from fixed Git bytes. HTML base rewriting, malformed
bundles and read/depth/count budgets remain explicit holds; dynamic JavaScript
resource construction is not executed or claimed as complete bundle discovery.

## Resuming a bounded scan without starving later objects

The existing private `bulk_progress` now retains `next_order`, `cycle_verified`
and per-file `cycle_assets`. The ordering key is depth, directory/file order and
private source path. The next pass resumes at the saved ordering key rather than
rechecking the unchanged prefix. Insertions before that position do not restart
the scan; a removed next object advances to the next surviving ordering key.
A partial multi-batch source retains its own position until it finishes.

Skipped ancestors are resolved on demand from already-managed original IDs,
with parent/title uniqueness and pending-intent checks. Missing, moved, renamed,
ambiguous or unknown ancestors hold their dependent subtree. Ancestor lookup
does not create a replacement or grant authority from a cursor alone. Current
entry identities/baselines and source signatures are still required before
retaining a prior cycle verification.

At the end of a cycle, the following invocation starts again from the beginning
for new increments and target checks. Source changes or new objects before a
saved position remain unverified/deferred until that next cycle; they cannot be
counted as delivered merely because earlier source bytes were checked. Frozen
initial requests retain their exact source-manifest guard. Contextual unknown
outcomes and previous asset failures remain visible across passes; a cursor
cannot turn them into a completed result. No new queue, state file, schedule or
public path parameter is introduced.

`total`, `verified`, `held` and `deferred` describe current-cycle coverage.
`adopted`, `skipped` and `verified_batches` describe work in this invocation.
A legacy progress record without a cursor begins one new full scan. All existing
checkpoint, single-attempt and incomplete-exit requirements remain in force.

## Rich-text run coalescing during intent recovery

Notion can return a long code block as one text run after receiving several
same-attribute runs. Intent readback first uses the existing exact hashes. On a
mismatch it may normalize adjacent text runs only when the original privately
checkpointed `context.request.children` is still present and its unchanged
canonical hashes agree with the saved `expected` hashes. Text, links, meaningful
annotations and all other properties must then agree after coalescing.

This compatibility check applies to both initial create/append recovery and the
second read in finalization. Before/after target baselines still use the existing
`block_identity`; no historical baseline hash migration, target mutation or
write retry is needed. Missing/tampered payloads, changed human prefixes, true
text/style/link changes and incomplete targets remain held.


The page-batch budget counts actual single-send attempts immediately after the
remote dispatched checkpoint and before sending. Unknown/rejected writes consume
an attempt too; reused verified history does not. `attempted_batches` reports
this separately from verified receipts, so failures cannot expand a bounded pass
past its requested number of attempts.
