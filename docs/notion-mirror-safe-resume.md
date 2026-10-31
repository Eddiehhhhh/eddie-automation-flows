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

## Preserving native code whitespace

Only the native renderer uses exact text chunks for fenced code and frontmatter.
The old renderer remains unchanged for historical original-page matching. Native
chunks preserve every character of the parsed fence body, including indentation,
blank lines, tabs and boundary whitespace, while staying below the API text
budget without splitting a Unicode codepoint. They do not trim around a chunk.

The rendered-content signature changes only when the actual native representation
changes. A source with a previously lossy long code block therefore receives an
append-only corrected version; unchanged short code retains a genuine no-op.
This is separate from intent recovery: recovery proves the originally sent
payload was complete, not that an earlier renderer preserved the Git fence body.

The private scan progress also records a fixed `renderer_revision`. A renderer
upgrade invalidates an older partial scan once, so unchanged source hashes before
a saved cursor cannot hide a required format correction. Ordinary state commits
or source insertions still do not reset that cursor. Rescanning does not rewrite
plain or short-code pages whose actual rendered content is unchanged.

## Protecting original pages whose source version is unknown

A reviewed existing page may use `signature: null` only with explicit
`preserve_target_content: true`, complete `original_identity` and a full private
`preserved_original` snapshot. Null means unknown, not an empty source or an
already delivered Git version. Without preservation identity or a verified owned
in-flight history, a null binding stays held.

Identity requires the original mirror root, unique parent/title binding, the
original source-quote block and exact path text, matching original creators,
and no foreign or unresolved operation markers. A cursor or content hash alone
cannot grant authority. The same protection can apply to a proven historical
Git source with interleaved manual additions: retain its real old signature and
all extra blocks, rather than relabeling it as unknown.

For every protected append, the existing prepared intent contains the complete
actual pre-append block tree, baseline and snapshot hash. Its private commit must
be acknowledged before a dispatched checkpoint stores the fixed preservation
reference and the one allowed send occurs. Finalization records that immutable
Git reference. Compaction removes a duplicate protected snapshot only after
reading the exact prepared snapshot back from its Git pin; inaccessible pins
keep the raw cache. Unknown receipts always keep their original raw snapshots.

New manual additions are privately preserved before baseline extension. A
manual replacement/reorder is also preserved as an observed target version;
current confirmed Git content is appended as another version, never used to
replace or delete the manual version. A manual change during a partial source
starts a new full generation instead of reusing incomplete verified fragments.
Known owned markers permit bounded continuation; unknown markers hold.

Native manual files need a durable matching private-Git binary reference in
addition to their temporary Notion URL. Potential credentials, missing durable
binary evidence or insufficient identity hold the object; these are preservation
requirements, not file-type exclusions. The initial reviewed text-only targets
must still receive real production checkpoint/readback acceptance before being
reported as restored.

Notion has no CAS. A target change after the checkpoint can still cause readback
to hold; the automation keeps the private before-version and never retries the
write blindly or overwrites the competing content. No new confirmation queue,
state file, page replacement, schedule or external storage is introduced.

## Exact Notion app-link query normalization during recovery

Request-bound semantic recovery additionally recognizes the observed API removal
of the sole terminal `?pvs=204` from an HTTPS `app.notion.com/p/<32 lowercase hex>`
text link. The full retained request must first match its original expected
hashes. Only the expected URL is normalized; destination ID/host/path, label,
annotations, other URL parameters and fragments remain exact. Query addition,
other hosts/routes and missing or altered request evidence stay held. Existing
block hashes, stored baselines and renderer profiles are unchanged.

This does not permit Unicode content loss: omitted zero-width characters are
actual content differences and keep their original unknown receipts held until
a complete preservation path is explicitly implemented and verified.

## Source-attachment preservation for proven Unicode display loss

A single final legacy file batch can use a restricted preservation transition
when its complete checkpointed request still matches its old expected hashes
and the actual unique same-parent/title target differs solely by deletion of
U+200B in paragraph or bulleted-list text (the two observed block types). Other characters, text-run structure, styles, links,
block counts, original operation/end markers and old baseline blocks remain
exact. A changed source signature or renderer profile holds. The first implementation
is limited to reference-free sources whose current pure native render exactly
matches the original legacy render; linked/asset-dependent sources stay held. This is not a
general Unicode normalization rule and does not make native text byte-equal.

First upload and the same-page preservation append require the existing explicit
`--execute-request <opaque UUID>` route, carried by the workflow's existing
`revision_request` input. Its private `execution_request` must use
`action: preserve_unicode_source`, `allow_new_page: false` and lock the original
`operation_id`, `source_path`, `source_signature`, `source_snapshot`, `source_blob`,
`source_sha256`, `source_size`, `render_version: legacy-v1`, `target_page_id`,
`parent_page_id` and `original_request_hash` (SHA-256 of the unchanged full original
request JSON, sorted keys, Unicode unescaped). The request UUID is v4. Missing or
changed locks hold before target writes. Ordinary bulk/cron and
`--execute-revision` do not consume this preservation request. Existing
`--recover-intents` remains GET/download-only; `--persist-recovery` additionally
publishes verified private state but never uploads or appends.

The executor reads the original Markdown bytes from the fixed authorized private
Git commit, verifies the source signature and its complete original legacy
rendered payload, then acknowledges a private full before-tree and complete original-source-byte
checkpoint. GET-only clean-runner recovery rechecks these bytes against the original
request, source signature, Git blob identity and attachment hash; a pointer alone
is insufficient. A
missing old Git object can be fetched by that exact commit only after private
checkout verification; HEAD, index and source files remain unchanged. Existing
asset receipts checkpoint the original-file upload. On the same original page,
one separately checkpointed PATCH appends a preservation marker, explicit
display-difference notice, the complete Markdown file and an end marker. Every
actual send consumes the existing batch budget after remote acknowledgement.

Only unchanged original blocks plus the complete notice/file suffix and an
actual attachment download matching the source SHA/size qualify the old intent
as `source_attachment_complete`. The old expected hashes and original request
remain intact, including after verified-state compaction. Matching source/profile
then no-ops using the actual target baseline; a later source revision is checked
and delivered independently. The reported fidelity and separate `source_attachment_complete` aggregate describe
a complete source attachment, never a claim that native body characters are identical.

A lost upload response uses existing upload-ID GET recovery or stays held when
its ID is unknown. A dispatched preservation PATCH is never reissued: clean
runners use GET-only recovery. Failed private publication, differing attachment
bytes, human target changes or ambiguous evidence hold only that object. Notion
has no CAS; a concurrent edit can therefore still leave this preservation held.
No original block is edited or deleted and no replacement page is created.

Both source uploads and manual private snapshots reject high-confidence old
`secret_` and current `ntn_` Notion credentials, in addition to the existing GitHub,
OpenAI and private-key signatures. This includes credentials different from the
currently configured API token.
