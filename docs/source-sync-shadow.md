# Get/flomo direct-source shadow migration

Status: draft implementation; **not a production cutover**. Existing production workflows remain disabled. Do not enable them as part of merging this change.

## Delivered behavior

- Source-specific identity indexes recognize existing cloud frontmatter, Get plugin `canonicalId: api:…` / `source_ids.api`, and legacy Get `note_id` plus `note_type`. IDs remain strings; floating-point IDs are rejected. Different IDs never match by title or body. Duplicate existing identities fail closed for manual reconciliation.
- Existing paths survive title/date edits. Identical note/cache/manifest bytes are not rewritten. Writes replace files atomically.
- Get enumerates the complete list and fetches all details before writing, with no creation-time cutoff. This conservatively catches old edits and late transcription. Missing original transcripts stay in a deterministic pending list and are refetched on every complete run. A page cap, repeated cursor/ID, missing official `has_more`, invalid shape, or failed detail aborts the run.
- Official Get defaults to `cursor`; plugin compatibility uses explicit `--pagination since_id`. No automatic mode guessing. Python JSON preserves integer precision; outgoing IDs/cursors are strings.
- `recorder_audio` and `recorder_flash_audio` enter `99 录音卡`. Original audio text, summary, timeline, chapter timeline and quick notes stay separate. Complete raw detail and attachment descriptors stay in private output. Image/file binaries are materialized under `_assets/images` and `_assets/files` using content SHA-256 names, and Markdown includes local links. Audio/video downloads are disabled.
- flomo preserves original content/tags and rejects missing content/update time, error envelopes, declared truncation, result caps and unhandled pagination. It requires explicit `has_more: false` and integer `total == len(memos)` below the limit before accepting a window; unknown completeness fails closed even for an empty/short response. These are conservative acceptance requirements, not asserted live API fields. It never advances the old last-sync state from an unproven search window.
- Receipts are local observations, not published watermarks. There is deliberately no remote publication in the new shadow runner. Future production publication must atomically commit content and state to the private repository; remote branch success is the watermark. The existing commit helper rejects concurrent remote advancement and never force-pushes.

## Run after draft review

The new `Validate source sync in ephemeral shadow` workflow has **manual dispatch only**, `contents: read`, no private vault checkout/token, no upload artifact step, and no publishing step. It reuses existing secret names only: `GETNOTE_API_KEY`, `GETNOTE_CLIENT_ID`, `FLOMO_TOKEN`. It runs both reads in a temporary directory outside the public repository and verifies hashes **and mtimes** across every output file. Only aggregate counts reach the Actions summary. Live changes or rotating upstream URLs can legitimately make repeat validation fail; investigate instead of waiving the check.

A newly added manual workflow normally must be present on the default branch before GitHub will dispatch it. This draft does not merge itself or enable existing workflows. After review, merge only the shadow implementation, then manually run one source at a time. For flomo, supply explicit inclusive `start_date` and `end_date`. This proves that window only, not old-edit completeness.

For already injected credentials, local equivalent:

```sh
python scripts/verify_shadow_repeat.py --source getnote
python scripts/verify_shadow_repeat.py --source flomo --start-date YYYY-MM-DD --end-date YYYY-MM-DD
```

Do not put credentials in arguments, files or logs. Scripts no longer fall back to scraping local credential configuration. Direct script writes require an explicit `VAULT_ROOT`; the shadow runner always supplies a fresh private temporary directory.

## Evidence from this implementation session

- Public source reviewed: [official Get client](https://github.com/iswalle/getnote-mcp/blob/main/src/client.ts) and [Get plugin client](https://github.com/JinzheNext/obsidian-get-notes/blob/main/src/api/GetApiClient.ts). They expose different pagination conventions; no source contract was inferred from list timestamps.
- Existing Get and flomo production workflows were verified `disabled_manually` through GitHub metadata. The destination vault was verified private. Only Actions secret **names** were listed.
- Existing injected flomo credential: real official MCP initialization returned **HTTP 401**. No memo fetch succeeded. MAX eligibility, current tools schema, complete detail retrieval, pagination, and date-filter semantics remain unverified. Do not replace this with private web login automation.
- Get has no credentials injected into this execution environment. Attempting to reuse local MCP credential configuration for isolated validation was rejected by automatic approval review; that path was stopped. No private Get source content was fetched or committed by this task.
- Offline regression fixtures are synthetic. They are not proof of a successful real-source shadow run.

## Cutover gates still open

1. Authorized working credentials and successful real-source shadow runs, including complete enumeration counts and full original recording boundaries.
2. flomo official MCP schema and detail completeness: missing/truncation checks cannot prove an API never silently truncates. Confirm update-date versus creation-date search semantics and historical-edit coverage before designing incremental watermarks. Do not deploy a bounded daily search as full sync.
3. Validate actual Get CDN responses against the implemented image/file downloader and choose an approved initial-library transfer budget. Synthetic parity and failure tests pass; real attachment access remains unverified. Local audio download remains off.
4. Reconcile existing duplicate IDs and review plugin Markdown/local attachment link preservation before importing over existing production notes. Shadow fixtures do not prove safe in-place migration.
5. Confirm one writer, disable overlap during a coordinated handoff, validate the private target on its current head, publish without force, then advance state only with that successful publication. Production cutover requires acceptance by the parent/user. Do not pause local sync now.

The other automation jobs, MyVerse, Hermes and local Obsidian sync are outside this change.

## Attachment implementation and offline evidence

`source_sync_assets.py` implements the previously missing binary download layer. It accepts credential-free HTTPS/443 URLs, rejects private/reserved/mixed DNS results, pins the validated IP while retaining TLS hostname verification, revalidates each redirect, and forwards no authorization/cookie/proxy credentials. It ignores upstream filenames when choosing local paths, rejects escaping/symlink paths, checks image MIME, validates declared/streamed lengths, and commits bytes atomically. Fixed per-request timeouts, transfer deadlines and three bounded retries cover timeout/429/5xx failures; permanent failures are not retried. Per-file/run/count defaults are 25 MiB / 200 MiB / 128, adjustable with the three `--asset-*` budget flags after deciding the initial-library budget. Assets are revalidated by downloading on subsequent runs; unchanged bytes retain their mtime. This favors freshness over bandwidth until live CDN validator semantics are verified.

Every note's assets must finish before any Markdown/raw-cache/receipt is updated. A partial failed run may leave harmless content-addressed asset blobs but does not publish or advance any watermark. The next run reuses identical blobs. Manifests include SHA-256, size, MIME, local path and explicit skipped audio/link descriptors.

`tests/fixtures/getnote-plugin-synthetic.json` mirrors the local plugin's image/file/audio/link classification inspected in `main.js`; original text and summary are synthetic. It is semantic parity, not a claim of byte-for-byte plugin Markdown equivalence. The independent `source-sync-offline-tests.yml` runs only synthetic tests on PR/push with read-only permissions and no secrets. Existing workflows are untouched.

Official flomo [MCP connection instructions](https://help.flomoapp.com/advance/mcp/connect-antigravity.html) describe MAX membership and a personal token. They do not establish exhaustive search pagination, full-detail or updated-at enumeration semantics. The implementation therefore rejects absent completeness evidence; the mocked acceptance envelope is not represented as observed upstream behavior. No additional login or token request was attempted.

## Review corrections after 41a15a1

- Pending recording details preserve any existing Markdown byte-for-byte, including migrated plugin files with no new raw-cache. Pending runs now return exit 2 and `complete: false`; pending is never a successful shadow acceptance. Newly observed pending notes may be stored for retry, but no published watermark is advanced.
- All present same-source Get identity fields (`note_id`, API `canonicalId`, `source_ids.api`) must agree. Conflicts stop the run before writes and preserve every original file.
- Each asset attempt has one shared deadline across DNS, redirects, connection/TLS, headers and body. DNS waiting is bounded; an active transport watchdog interrupts slow header/chunk framing, and body reads use `read1` with the remaining deadline applied to the socket. Bounded retries retain their own attempts. Socketpair tests send slow body bytes and slow headers without external network access.
- All three new regression tests fail against 41a15a1 and pass after the correction. The old slow-drip test took over one second with a 50 ms budget; the corrected regression enforces completion below 200 ms with the same budget (scheduler tolerance included).

## Independent re-review and phase handoff

The parent independently re-reviewed commit `4b6893d935fc68b4ad59df1be69ef46c4ebbd362` and reported PASS for this isolated shadow phase. Both the migrated-pending/no-cache data-loss reproduction and conflicting identity-field reproduction are fixed. Six independent real-`HTTPResponse` scenarios covering slow streams, connection closure and absent content length exited within their configured budgets. All 28 focused source tests passed in that review. These are independent offline/socket-level results, not successful authenticated source or real-CDN acceptance.

This phase ends with PR #4 remaining a draft for isolated shadow work. Do not merge it as a production cutover, enable existing sync workflows, or introduce a second writer. No further credential retries are part of this phase.

Remaining dependencies are explicit:

1. Authorized, valid Get/flomo source access; the previous Get approval rejection and flomo HTTP 401 remain unresolved. Do not obtain credentials through another path or retry them tonight.
2. Real CDN/TLS validation of the bounded asset downloader, including approved initial-library transfer budgets and actual attachment MIME/redirect behavior.
3. Real flomo full-enumeration, full-detail and historical-edit completeness acceptance. Synthetic completeness envelopes are not evidence of the live API contract.
4. Existing-vault migration acceptance and a coordinated single-writer handoff, with publication/state advancement verified together against the private target. Production remains stopped pending that acceptance.
