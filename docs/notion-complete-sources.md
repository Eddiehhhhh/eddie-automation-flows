# Complete API-visible Notion source capture

The existing personal-data workflow now has an optional, disabled-by-default source adapter. It reuses the same private checkout proof, writer lock, schedule, source credential and concurrency group. Publishing this code does not activate a source, dispatch a job, change a source page or retire a local producer.

## Scope and ownership

The generic adapter accepts exactly the original eleven database IDs from the private `Raw/02 Notion/03 State/.local-sync-state.json`. Private configuration must match that set and its original date-property mappings. It does not search the workspace, infer scope from similar names, add related databases or collect the GitHub-to-Notion Wiki mirror targets.

Mixed flomo sources route each actual memo/page identity through the existing flomo owner. A whole mixed database is never excluded. Delegation is complete only when the existing owner's immutable source JSON has the new complete-property proof, matching memo/page/source identities, matching source edit version and verified file hashes. Older partial-property receipts remain `delegated_property_proof_pending`. This adapter never modifies the flomo state; its owner must upgrade those receipts separately through the original chain.

The generic Podcast catalog and the existing dedicated Episode source have different identities. Do not infer overlap from their names. The workout original records supplement the existing daily aggregates; their full body and text properties stay searchable. Candidate provenance groups the same Notion identity across original/aggregate/summary views without throwing away readable text.

Existing film summaries keep their existing caller and paths. Its optional `--original-source-only` mode invokes this same capture helper into a separate Raw14 namespace. That source must match the database ID already present in existing film-summary frontmatter. It is not a twelfth generic source. Date fallback remains the existing ordered `添加日期`, then `日期`, then explicitly labelled page creation time.

## Exact namespaces and consumer dependencies

| Owner | Base | State |
| --- | --- | --- |
| Original eleven source adapter | `Raw/02 Notion/云端原文` | `.github/state/notion-sources-sync-state.json` |
| Existing film caller, original mode | `Raw/14 豆瓣影视/云端原文` | `.github/state/notion-douban-source-state.json` |

For each state, `sources[key].pages[notion_page_id].current_path` and every `files`/`versions` path are relative to that row's base. `index.md` is the stable human entry. Source JSON, Markdown and assets live together in immutable `versions/<source-key>/<page-id>/<version>/` directories. The readable filename retains its title and is bounded by UTF-8 bytes; full titles remain in Markdown and JSON.

Before receiving these namespaces on the Mac, the primary operator must install the matching scripts and both state-to-base mappings in local dependency/sync readers. Required code includes `sync_notion_sources_to_raw.py`, `notion_complete_properties.py`, its existing shared imports, and the updated audit/closeout/filename readers. The local synchronizer remains a receiver; it does not become the source writer.

New Markdown uses `notion_page_id` and `source_identity`, never generic local-writer `id`, `messages`, `syncedIds` or `burnSyncedIds`. Every old `<!--nh:...-->` marker in rendered Markdown/index is made owner-neutral; original JSON is preserved. This prevents the old notehelper's whole-vault ID/marker scanner from claiming cloud notes. No local plugin is disabled or reloaded here.

## Fidelity and source dates

Each successful object saves the unmodified source-page object, original property response envelopes, a separately derived hydrated page, all recursive block JSON, materialized page/block data and real local asset bytes. Title, rich-text and people property-items paginate fully. Relations expand when their value is not explicitly complete. Formula and rollup terminal values must be readable and supported; unknown, unsupported, incomplete or malformed results hold the object. Item IDs identify the property, so repeated item IDs are not deduplicated. Original annotations, mentions, equations, links and order remain in JSON.

Readable Markdown includes every nonempty source property and full rendered block text, with local structured assets and literal Markdown/HTML image embeds. Link annotations remain readable. Covers, icons and file-property attachments are included. Types are not excluded; HTTPS, size and batch limits are safety/budget boundaries with explicit reasons. No source credential is forwarded to asset hosts; the existing pinned public-IP, no-redirect downloader is reused. Account credential shapes are checked before any object is written, including raw source JSON, rendered Markdown and asset bytes. One held object does not discard another object's completed progress.

Source event/date-property values, page creation, page edit and capture times are stored separately. Audits use source dates and source edit evidence, never capture/import time as a new event. Closeout chronology prefers the actual source date. Visible metadata fingerprints also detect property/formula changes without relying only on page edit time; Notion-hosted signed-file expiry churn does not force a new version. These are API-visible observations, not an atomic workspace snapshot or a backup of UI-only view/button automation definitions.

Old Raw files and manual content remain untouched and active. No old file is automatically suppressed. Edited owned current/history files hold the affected capture instead of being overwritten; edited current Markdown remains discoverable. Normal historical versions and index/evidence files do not become duplicate fact candidates. Physical inventory still counts them.

## Budgets, resumption and verification

A private batch declares maximum page attempts, per-source attempts, query pages, requests and elapsed seconds. Zero/unlimited budgets are rejected. Metadata pagination checkpoints preserve source IDs, edit versions, exact terminal evidence and the next cursor. Large source inventories resume later; an incomplete scan never advances a full-coverage claim. A complete unchanged receipt is reused, while new, changed or previously unproved objects hydrate. Cursor errors and identity conflicts fail closed.

The existing workflow records its 40-minute job deadline. The new adapters stop starting source requests with 300 seconds reserved for persistence/readback, so slow earlier sources reduce the new batch instead of consuming its commit window. This is in addition to each adapter budget; the first reviewed source-only run does not prove a later whole scheduled run.

The controller leaves time before network operations, catches a soft budget boundary and returns partial status so the workflow can commit every completed object plus state/index together. An unfinished object is not published as current. Scheduled source priority rotates; manual `notion_source_key` selects an explicit configured source. Source absence never deletes prior records. Selected-scope completion and all-configured-scope completion are separate; held and delegated-unproved objects cannot make either green.

The workflow captures each independently configured original source after the older business-source commit. Config failure and ordinary business failure do not swallow a successful independent source; cancellation prevents starting new writes. The generic and film paths/states commit together, then a fresh remote fetch binds both tree maps. A single streaming `git cat-file --batch` reads every referenced Git object; local and remote SHA-256, blob OID, size and state/index bytes are checked. Verification does not spawn Git once per file. Aggregate failure is reported after persistence/readback, not before it.

## Private configuration and first acceptance

The private `.github/state/notion-sources-config.json` has version `1`, global `enabled`, exactly eleven `sources` with stable `key`, authorized `database_id`, original `date_property` and `owner` (`generic`, `flomo` or `workout`), plus `batch`. The optional `dedicated_douban` has its own `enabled`, existing `database_id` and ordered `date_properties`. It does not expand the generic list. No credentials belong in this file.

Defaults: `max_pages=50`, `per_source_pages=50`, `max_seconds=900`, `max_requests=500`, `max_query_pages=20`. Film original mode caps its additional time/requests at 300/250. The existing `NOTION_API_KEY` and `PRIVATE_VAULT_TOKEN` are reused.

The primary operator first reviews configuration with `enabled=false`, then explicitly enables the reviewed adapter and dispatches the existing workflow with `notion_sources_only=true`, a configured source key and `notion_source_max_pages=50`. This is **up to** fifty completed attempts: full-property proof may consume the request/time budget earlier. Read the partial coverage and actual receipt set rather than assuming a green run or exactly fifty files.

Acceptance must read every actual source JSON/property envelope/current Markdown/local attachment from the fixed private commit, compare the exact configured source/date identities, verify remote object bytes, test the installed old plugin scanner with positive controls, and compare old manual files byte-for-byte. The primary operator also owns the source registry, Wiki/index routing and local dependency installation before receiving data. Later backfill uses the same existing entry with deliberate manual runs; this change creates no new scheduler or background host.
