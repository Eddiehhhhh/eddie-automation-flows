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
