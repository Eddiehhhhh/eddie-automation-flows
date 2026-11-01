# Notion flomo bridge candidate

This candidate consumes the existing flomo-to-Notion feed. It makes no requests
to flomo, makes no Notion writes, and never writes or renames `Raw/01 Flomo`.
Do not enable its writer or turn off the local collector merely because its
synthetic tests pass.

## Reuse and ownership

- Reuses `sync_notion_to_raw.py` for the existing authenticated read API, checked
  pagination, property summaries and Markdown rendering.
- Target identity is supplied at runtime using the existing private database
  registry, not copied into public workflow logs or this document.
- Uses the existing private-vault checkout/token and `run_private_command.py`.
  Future publication must retain `eddie-private-vault-writers` concurrency and
  use scoped `commit_private_vault.py`; never push through the real Mac vault.
- Persistent progress: `.github/state/notion-flomo-sync-state.json`. Per-ID current-version pointers, immutable version history and output hashes permit bounded batches without moving a global
  watermark past unprocessed older notes.

## Run contract

Set `VAULT_ROOT` to the verified private-vault checkout and `NOTION_API_KEY` to
the existing Notion integration. Read the database ID from the small private
`.github/state/notion-flomo-config.json`: `version: 1`, `database_id` from the
verified source registry, and `stage_enabled: false` until activation is approved.
This is independent of Get runtime bindings. Inventory writes nothing:

```sh
python scripts/run_private_command.py --label "Notion flomo inventory" -- \
  python scripts/sync_notion_flomo_to_raw.py \
    --config "$VAULT_ROOT/.github/state/notion-flomo-config.json"
```

Once separately ready to stage, the same command receives `--apply --max-pages
50`. Candidate wiring reuses `sync-notion-personal-data.yml` and its existing
03:20/15:20 Asia/Shanghai schedule and shared concurrency. It only stages when
the private configuration has `stage_enabled: true`. Manual testing can select
`flomo_stage_only` and `flomo_max_pages`; podcast full-sync inputs keep their
original meaning, and selecting both modes stops before writes. The old
`sync-flomo.yml` remains disabled and is not resumed. No new cron is added.

The ordinary source steps and their original commit run before any flomo
configuration or staging. Configuration errors are collected independently;
earlier ordinary-source failures do not prevent flomo processing, while every
flomo start/persist gate explicitly refuses cancellation. Missing configuration
defaults off. The stage step may exit partial, but the workflow first commits its scoped
Raw02 output and per-ID state using the existing private publisher, then reports
the incomplete result as a failed job. A held or pending source cannot erase completed objects. All source variants
can finish capture while divergent content remains explicitly unmerged in the
private report; this does not claim the overall system migration is complete. Publication failure remains a failure;
a following scoped readback fetches private main and verifies the state Git blob
against committed HEAD and actual state bytes, requires the same staged-content
subtree on remote main, and checks every state-referenced content hash against
the committed tree. An orphan state cannot be marked durable. Readback mismatch fails the job.
The state has a local single-writer lock plus byte checks before/after mutation.

## Private outputs

All new content is under
`Raw/02 Notion/01 Databases/碎片中心/flomo-stage/`:

- `versions/<memo_id>/<version_digest>/<memo_id>.md`: own and nested body text, tags, Notion properties/relations,
  original Notion page identity and existing local Raw/01 paths.
- `versions/<memo_id>/<version_digest>/<memo_id>.json`: unabridged API source page/blocks plus materialized snapshot;
  retains rich text annotations and fields not expressible as plain Markdown.
- `versions/<memo_id>/<version_digest>/assets/<sha256>.<extension>`: actual bytes with content identity; stage
  Markdown uses relative paths. The private JSON retains original source URLs
  for evidence. Never publish those URLs or source JSON to a public repository.
- `coverage.json`: source ID coverage, duplicate identities, pending and held
  objects. It is an inventory/processing report, not proof of original flomo
  completeness or Mac-independent collection.

Version directories are immutable: create-only publication never replaces an
existing file, including a file edited after precheck. State retains all prior
version hashes and points to the current version. The ID-based staging filenames
are temporary identity mappings. Final Raw/01
promotion must preserve existing human-readable paths, local edits, memo links,
assets and stable IDs; this script deliberately cannot perform that promotion.

## Failure and cutover conditions

- Capture identity is `(memo_id, notion_page_id)`. Multiple Notion pages are
  preserved separately under the existing immutable version directories; no
  title/date winner is chosen. A collection index links all captured variants
  and marks observed pages still pending. Exact copies retain every page's
  original JSON and receipt as well. Local-only memos are retained; source
  absence never authorizes deletion.
- Existing ordinary memo receipts remain unchanged. Only multi-page identities
  add `source_pages`, with one current version/history per original page, plus
  `current_variant_index` and retained immutable index versions. If an ordinary
  memo later gains another Notion page, its original receipt and old files are
  preserved. A page disappearing from the active listing retains its snapshot.
- `capture_complete` (and compatibility field `complete`) means every observed
  source page was captured/read back with no pending/held operation.
  `unmerged_variant_groups` and `semantic_merge_complete` independently report
  divergent bodies/properties. Complete capture does not select a canonical
  variant or authorize fact promotion: `fact_promotion_allowed` remains false.
- Apply verifies both origin fetch/push destinations and fresh GitHub private
  repository identity using `cloud_private_checkout.verify_private_checkout`
  before any filesystem write. The shared helper must land before this candidate.
- Stage versions are create-only. Existing versions require identical bytes;
  unknown files, edits and missing earlier versions are held. Failed pages do not move progress for those
  IDs. The source page version is re-read before staging.
- Before any object output, source JSON and rendered Markdown are checked for
  high-confidence GitHub account token, sk-key and private-key PEM shapes. A hit
  is held as `potential_credential_in_source`, without retaining the match or a
  secret fingerprint in reports/logs. Share parameters and temporary Notion
  resource URLs alone are not account credentials. Other objects can proceed.
- Attachments require HTTPS and globally routable addresses, connect to the
  validated IP with TLS hostname verification, send no Notion auth headers,
  and do not follow redirects. Failed downloads, unsupported blocks and source
  races remain held. Bounds are 50 MiB per asset and 200 MiB per record; exceeding
  one produces a persisted `asset_file_budget` or `asset_batch_bound` hold for
explicit investigation, not exclusion from scope or silent omission.
- The initial full scan is metadata coverage only. Actual source body, relations
  and attachment capture require successful object staging and readback. This
  preserves what Notion exposes; fields already absent from the upstream feed
  cannot be reconstructed as original flomo facts.
- Before production: verify staging with real changed notes and media; reconcile
  duplicate and local-only identities; prove the existing flomo-to-Notion
  executor's identity and a genuine Mac-off increment; complete Raw01 promotion
  protection and retrieval registration; then stop local capture and switch the
  single cloud writer. Obsidian Git syncing remains enabled.

## Iteration candidate boundary

Stage Markdown is marked `stage_only: true`. Both current snapshots and immutable
historical versions are capture/reconciliation evidence, not independent fact
sources or additional memory candidates. The shared
`iteration_incremental_audit.candidate_from_file` first excludes the exact
registered flomo-stage subtree before reading frontmatter or considering dates
and mtime. This covers current records, immutable variants/indexes and README
files even with frontmatter exceeding the 12000-character reader. Other Notion
notes are not excluded; the general `stage_only` marker remains active elsewhere. The
canonical `Raw/01 Flomo/` note remains eligible under the ordinary rules.
`cloud_weekly_bundle.py` and the manual `run_iteration_pipeline.py` consume this
shared audit. This filter does not enable any daily or weekly schedule.

`iteration_source_discovery` intentionally counts physical files and Markdown
files, including staging versions. Those counts measure inventory, not active
candidates or unique facts. Only the incremental audit candidate counts apply
the stage filter; no inventory entries are removed or hidden.

## Optional original-flomo enrichment

The same personal-data workflow can supplement the Notion mirror with the
original flomo HTML, full original JSON, second-precision creation/update times,
original file metadata, and content-addressed asset bytes. This is an additional
reader inside the existing verified, locked private writer. It never writes
Notion or Raw/01, enables another schedule, or restores the legacy flomo workflow.

Enable it only through the existing **private**
`.github/state/notion-flomo-config.json`: retain its existing fields and add
`original_source_enabled: true`. Keep both access and signing material out of
tracked configuration, including the private Git repository. The original reader uses the
existing signed GET `/api/v1/memo/updated/` contract and bounded complete pagination.
It does not call login, token refresh, rotation, account mutation or source writes.
A completed initial scan retains private page termination/ID proofs; a failed or
malformed scan advances no original-source capture progress.

The workflow requires two distinct Actions secrets, `FLOMO_WEB_ACCESS_TOKEN` and
`FLOMO_WEB_SIGNING_SUFFIX`, read as environment variables only in the existing
staging step. Their legal sources are the already configured Obsidian
flomo-bridge access token and the installed client's `SIGN_SECRET` protocol
constant respectively. The registered
MCP token is a different credential; do not silently substitute `FLOMO_TOKEN` or
`FLOMO_MCP_TOKEN`. Never copy credentials to tracked files, command arguments,
logs, reports or PRs. Deployment operator: securely set these two dedicated secrets,
update the small private configuration, then run the existing manual stage-only
entry point and inspect capture/media coverage plus private commit/readback.
This implementation does not configure the secret or enable the private switch.

Only the existing access token is used. Read-only access has been validated;
session refresh/rotation coexistence with the local plugin has **not** been
validated, so no automatic refresh is implemented. Expiry or access rejection
holds the supplement explicitly and preserves the separately captured Notion
progress. Keep local sync available during acceptance; do not log out, rotate,
or refresh the shared session as a recovery action without separate validation.

Supplement state/coverage live under the already registered staging subtree at
`original-source/state.json` and `original-source/coverage.json`. Existing Notion
state and receipts are untouched. Immutable original snapshots are grouped by
`(memo slug, Notion source page)`, including every duplicate variant; upstream-only
memos receive a clearly named `upstream-only` entry instead of being discarded.
Later mappings and edits add versions and retain old evidence. Missing entries or
upstream deletion flags never authorize deletion of local or historical data.
Notion receipts whose memo is not observed in a complete original-source scan
are explicitly held as `original_memo_not_observed`; existing Notion and original
evidence remains intact, and full capture/media fidelity is not asserted.
All supplement state and objects share the existing private commit and remote
blob verification; the ordinary Notion commit cannot publish this state early.

Original JSON/HTML and metadata pass the same high-confidence credential gate
before object writes. File URLs and HTML `img src`/ordinary image URLs are kept
privately and downloaded through the existing HTTPS public-address-pinned,
non-redirecting, bounded downloader. No flomo authorization is forwarded to asset
hosts. Files are not excluded by type. A refused external URL is preserved with
an explicit status such as `asset_http_403`; it is **not** a saved image. Resource
budgets are explicit degraded reasons. Successful original JSON can be saved even
when one image is unavailable; failed assets are retried on the next invocation.

`inventory_complete` means source pagination reached its terminal page;
`source_complete` additionally requires all selected original object receipts;
`media_complete` also requires every discovered asset to be saved. A pending,
held or degraded supplement returns incomplete overall while preserving successful
Notion and original progress. All original materials remain stage-only and are
excluded by the exact existing staging-path fact-promotion guard.

Acceptance must reconcile original source IDs, all Notion variants, historical
Raw/01-only entries and actual binary media. Three readable original memos or an
all-text staging run alone cannot justify disabling local sync or claim full
media fidelity. No Mac-off/cloud deployment or full-history attachment completion
is asserted by unit tests or a public code merge.


## Complete-property receipts (optional backfill)

The existing private `notion-flomo-config.json` can opt into strict API-visible
property archival without adding another collector or schedule:

```json
{
  "property_proof_enabled": true,
  "property_proof_batch": {"max_pages": 50, "max_seconds": 300, "max_requests": 250}
}
```

These fields supplement the existing source configuration; they are not a new
configuration file. The flag defaults to false. Keep it off until the public
runtime dependency and a small private source-only acceptance run are verified.
No source ID or credential belongs in this public example.

When enabled, title/rich-text/people properties are fully paginated with the
shared complete-property helper; relation/formula/rollup use that helper's
explicit completion rules. Each source JSON retains an untouched `original_page`,
all property response envelopes and the separate hydrated view inside
`property_capture`. The old `page`, `blocks` and `materialized` keys remain usable.
Completed rich-text properties also appear in a clearly labelled readable text
section of that stage version and its formal current, including explicit href /
text-link target addresses beside their labels; raw formatting remains
fully preserved in the property response envelopes. Only a completed object receives `property_proof_version: 1` in its receipt.
Old same-version body receipts remain intact but require this backfill before
being treated as proof of complete properties. New or visibly changed pages have
priority; completed unchanged versions skip hydration. Temporary Notion asset
URL query/expiry rotation does not force a new version.

The bounded defaults apply even when the old `--max-pages 0` is used. The private
batch supports 1–500 pages, 60–1200 seconds and 20–2000 requests; the CLI nonzero
page limit can reduce it. Actual Notion HTTP attempts count against the request
budget, use serial throttling and disable implicit read retries for this pass.
Assets share the 50 MiB per-file / 200 MiB per-pass budget. These limits defer
work, not exclude file types. Local time reserve and the existing workflow job
deadline preserve room for the already-configured private commit/readback steps.
If the global persistence reserve is reached, original-source/current followups
are deferred instead of starting more expensive work.

A budget exit persists already completed object receipts and reports the rest as
pending; incomplete metadata traversal never replaces source state or claims an
empty complete inventory. Changed identity, incomplete property responses,
credentials, edited immutable objects and missing previously owned source pages
remain explicit holds. Every duplicate source page keeps its own proof/version;
none is selected as a winner. Existing current pointers, original-source evidence
and legacy notes remain preserved. The same private commit paths and remote
byte/hash readback cover the added JSON, Markdown, assets and state together.
No generic collector writes this Flomo state. Generic delegation may accept a
matching Flomo proof only after verifying the pinned receipt, source bundle and
strictly equal metadata fingerprint. Missing legacy fingerprints stay pending;
unchanged edit timestamps alone cannot validate changed properties. Both source
and Flomo fingerprints use the same signed-resource URL normalization.

Code/tests alone do not establish production completeness. Acceptance must show
one real bounded run, a private commit containing state plus all referenced
objects, remote readback, and correct current consumers; historical backfill stays
pending until every observed owned source page has the new proof.


## Readable relation targets and renderer revision

Proof-enabled stage notes now render each complete relation target as a stable
Notion page link in the readable body. The source JSON, property response
annotations and relation values are unchanged. Unproved notes keep their old
rendering; the data proof stays `property_proof_version: 1`.

The separate receipt/Markdown field `property_renderer_revision: 2` governs this
reading format. Existing proof receipts with no field mean revision 1. An
unchanged source with a valid older proof can upgrade from its immutable cache:
all source JSON, response envelopes and assets must pass their recorded hashes;
property pagination is replayed offline; source identity and metadata must match
fresh before/after page GETs. Original JSON bytes are copied unchanged, all old
versions remain, and only a new Markdown/state version is published. Incomplete,
edited, credential-bearing or source-changed evidence holds or uses the existing
safe fresh-capture path; no data proof is fabricated. No property/block/asset
network reads occur solely to change cached formatting.

Page priority is new/changed sources, then completed proofs awaiting this format,
then unproved historical backfill. The existing bounded page/request/time budgets
still apply. Coverage distinguishes `renderer_rewritten` from `property_hydrated`;
`written` includes both and must not be called a count of newly acquired data
proofs. Source dates and old fact candidates are preserved.

Formal current follows the changed stage version through its existing source
hash gate and records `notion_property_renderer_revisions`. It renders legacy
proofs as revision 1 until their stage receipt upgrades. The global current
renderer version is not bumped, so unrelated current notes are not all rewritten.
The independent original-source followup can still update other notes under its
own existing limits; this revision does not change that stage or schedule.
