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


## Readable partial pages with verified button gaps

An unsupported block is still a hold by default. When a fully paginated original block tree contains unsupported blocks, the same service token can perform one extra read of that exact page: `GET /pages/{page_id}/markdown?markdown_version=v2`, with request-local `Notion-Version: 2026-03-11`. The other page, database, property and block requests retain the existing API version. No workspace search, child-database query, source mutation or new scheduler is added. The Markdown read uses the existing request/time budget and has no hidden retry.

Partial preservation is permitted only when the Markdown response identifies the same page, its unique `unknown_block_ids` exactly equal the original tree's unsupported IDs, and every corresponding `<unknown>` tag identifies a `button` on that same Notion page. Other unknown types, extra/missing IDs, malformed tags, or another page/host fail closed. A button may contain text/actions that these interfaces do not expose; it must never be rendered as empty content or counted as complete.

The immutable `source.json` retains the original page, complete property envelopes, hydrated properties, recursive original blocks and the complete Markdown-v2 response with its version and unknown/truncation fields. The existing asset materializer saves readable attachments with hashes before publication. The readable note reuses the structured renderer for all known blocks and adds an explicit missing-button placeholder for each verified gap, plus a visible partial-backup warning. Original blocks and the Markdown response are not rewritten. Child database references remain references, not a claim that their records have been captured.

A partial receipt has `capture_complete=false`, `readable_partial=true`, `button_partial_proof_version=1` and structured `incomplete_gaps`. It stays in the same source namespace and immutable version history; old versions and legacy/manual mirrors remain intact. Current selectors still expose the saved readable note. These flags distinguish readable evidence from a full backup; they grant no Wiki promotion authority.

Coverage separates `captured` (complete), `partial_saved` (current readable partial), and `unread_observed` (not currently preserved or verified delegated). The existing `pending_observed` continues to include partial objects because they remain incomplete. A partial object cannot make selected/configured completion green. Index labels show the same distinction.

On later passes, partial proofs matching the inventory version are reused without another capture. At most one matching partial per source is checked by an original-page GET each pass, rotating the existing source record's `partial_probe_after` ID so already-saved partials cannot exhaust requests before unread pages. When edit time and metadata fingerprint still match, no hydration, Markdown retrieval, attachment download or new immutable version is repeated, and it consumes no new page-capture slot. Fresh inventory or a rotating probe can detect a changed source; it is then recaptured through all normal gates, and if the unknown blocks are removed it may become complete. A failed/manual edit/race still holds that object, while unrelated later pages can continue. This is partial evidence preservation, not complete recovery of Notion button definitions.

The shared SDK also has a default-inactive request observer, called immediately before each actual HTTP attempt, including retries. The generic adapter disables an ambient observer around its already-budgeted SDK call and restores it in `finally`, so nested proof consumers cannot double count its requests. Consumers that enable an observer must restore their ContextVar token in `finally`. The pure source-budget module is a local consumer dependency when this implementation is installed.

References: [official Markdown retrieval](https://developers.notion.com/reference/retrieve-page-markdown), [supported types, unknown blocks and version-local syntax](https://developers.notion.com/guides/data-apis/working-with-markdown-content).
