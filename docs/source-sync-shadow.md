# Get/flomo direct-source shadow migration

Status: draft implementation; **not a production cutover**. Existing production workflows remain disabled. Do not enable them as part of merging this change.

## Delivered behavior

- Source-specific identity indexes recognize existing cloud frontmatter, Get plugin `canonicalId: api:…` / `source_ids.api`, and legacy Get `note_id` plus `note_type`. IDs remain strings; floating-point IDs are rejected. Different IDs never match by title or body. Duplicate existing identities fail closed for manual reconciliation.
- Existing paths survive title/date edits. Identical note/cache/manifest bytes are not rewritten. Writes replace files atomically.
- Get enumerates the complete list and fetches all details before writing, with no creation-time cutoff. This conservatively catches old edits and late transcription. Missing original transcripts stay in a deterministic pending list and are refetched on every complete run. A page cap, repeated cursor/ID, missing official `has_more`, invalid shape, or failed detail aborts the run.
- Official Get defaults to `cursor`; plugin compatibility uses explicit `--pagination since_id`. No automatic mode guessing. Python JSON preserves integer precision; outgoing IDs/cursors are strings.
- `recorder_audio` and `recorder_flash_audio` enter `99 录音卡`. Original audio text, summary, timeline, chapter timeline and quick notes stay separate. Complete raw detail and attachment descriptors stay in private output. No audio downloads occur.
- flomo preserves original content/tags and rejects missing content/update time, error envelopes, declared truncation, result caps and unhandled pagination. It never advances the old last-sync state from an unproven search window.
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
3. Get image/file download parity: this draft preserves asset manifests and raw details, **not binary attachments**. Download/refresh/size/MIME validation and private storage need a separate bounded implementation and real validation. Local audio download remains off.
4. Reconcile existing duplicate IDs and review plugin Markdown/local attachment link preservation before importing over existing production notes. Shadow fixtures do not prove safe in-place migration.
5. Confirm one writer, disable overlap during a coordinated handoff, validate the private target on its current head, publish without force, then advance state only with that successful publication. Production cutover requires acceptance by the parent/user. Do not pause local sync now.

The other automation jobs, MyVerse, Hermes and local Obsidian sync are outside this change.
