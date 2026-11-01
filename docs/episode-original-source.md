# Complete original evidence for the existing Episode caller

This optional adapter supplements existing human-readable Episode notes. It does
not rewrite their bodies, transcripts, manual additions, dates or legacy index.
It reuses `sync_my_podcast_to_raw.py`'s exact `EPISODE_DATABASE_ID` and
`LISTENED_FILTER`; the separate Podcast library in the original eleven-source
configuration remains independent. No workspace discovery or unlistened Episode
expansion is introduced.

## Configuration and first acceptance

Add this section to the existing private `notion-sources-config.json`:

```json
{"dedicated_episode": {"enabled": false}}
```

The existing global source flag also applies. Only the boolean is configurable;
source identity, listened filter and `日期` semantics come from the existing caller.
The adapter shares bounded batch settings, capped at 300 seconds and 250 requests;
`--max-pages` can reduce the page budget for a two-page pilot. The workflow's
existing deadline preserves publication time. No new secret, host, task, cron or
concurrency group is needed.

After code review and coordinated deployment, the existing personal workflow can
run `notion_sources_only=true`, `notion_source_key=episode`,
`notion_source_max_pages=2`. Source-only mode skips the legacy note writer. The
scheduled path uses the same existing source capture/persist/readback group.
Capture failure or soft budget exit must still reach publication of successful
object receipts; cancellation must not start another writer. A partial capture is
never proof of complete properties, assets or the whole system migration.

## Ownership and readable output

- Namespace: `Raw/13 小宇宙/云端原文/`
- State: `.github/state/notion-episode-source-state.json`
- Stable human entry: `Raw/13 小宇宙/云端原文/index.md`
- Current pointer: `sources.episode.pages[page_id].current_path`, relative to that
  namespace; `files` / `versions` paths use the same base.
- Immutable object: `versions/episode/<page-id>/<version>/<readable-title>.md`,
  accompanied by full `source.json` and original attachment bytes under `assets/`.

The shared source capture keeps original page JSON, full property response
pagination, a separate hydrated view, recursive blocks and materialized assets.
Original transcript attachments are preserved as bytes and linked locally rather
than represented only by an expiring URL. Credential, download and unsupported
block safeguards are the same shared implementation; gaps remain explicit.
The source date is `日期`, with the original Notion creation/update/capture times
retained separately. Missing source dates are not replaced with import dates.

All immutable versions and existing notes remain on disk. Only the receipt's
current Markdown enters active facts; history, JSON and indexes do not. Existing
notes remain active and are grouped by `notion:<page-id>` with their full original
records, so they are not counted as separate personal events. Edited current
content holds instead of being overwritten. Legacy Episode scanners and index
rebuilds exclude only the precise new owned namespace.

The existing private commit includes this namespace and its state together.
The shared verifier checks current/index consistency, every owned byte, Git blob
identity and the fresh remote tree with bounded batch object reads. A green local
suite alone is not production acceptance.

## Local dependency and retrieval handoff

Before receiving the new namespace locally, deploy the updated caller's precise
legacy scan exclusion, the shared source module, active selectors, filename guard
and closeout consumer. Preserve the already-required transitive source helpers,
including `notion_source_budget.py`; do not install only the entrypoint. Add the
state-to-base dependency mapping above to the existing vault receiver, with
object/source JSON and attachment dependencies. No second local source writer is
introduced and existing local notes are not suppressed.

The primary owner must coordinate these private retrieval changes in the same
rollout (this public PR does not write the vault):

1. Extend the existing Raw/13 row in `Schema/来源目录注册.md` with the cloud original
   namespace, full-evidence purpose and legacy preservation rule.
2. Keep the existing Raw/13 link in `Wiki/索引.md` and add the stable cloud index.
3. Add a precise Episode entry to
   `Wiki/06 Systems/01 Routing/Notion 检索索引.md`: Episode original evidence uses
   the cloud current index; the Podcast library continues using its original
   generic source route. The complete index is the human entry; immutable history
   remains evidence rather than extra facts.

Pilot acceptance must compare at most two source pages against their recorded
full properties/blocks, verify attachment bytes and local relative links, prove
legacy notes and index unchanged by this adapter, publish state plus every object
in one private commit, independently read back remote blobs, and verify local
selectors/Obsidian reads after the dependency installation. Remaining inventory,
partial assets and later backfill stay explicitly pending.
