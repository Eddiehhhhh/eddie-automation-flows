# Full historical Get → Notion migration plan

## Evidence and priority

Use the coordinator's frozen source/Notion join, not a fresh title-based guess. Its batch-index SHA is `c233ccb245704ba26da6395b4ebed041e8e08275c11f3b7cdbf243a75c2f407e`. Counts are candidate inventory counts, not completed transfers:

- 2,924 Git note files, 2,916 distinct nonempty source IDs.
- 575 unique existing-page repair candidates: 565 confirmed placeholders with source body, 8 with body requiring parity review, and 2 still unclassified.
- 1,149 numeric source IDs with no same-ID Notion page at snapshot time.
- 220 duplicate Notion-ID conflicts; never delete a content-bearing duplicate merely to make the join unique.
- 924 supported old aliases: normalized bodies and tags agree, the historical date offset is consistent, and both sides have no media. Collapse these to the canonical numeric identity; do not create 924 additional pages or copy alias timestamps over canonical timestamps.
- 46 other legacy IDs, 8 nonstandard/missing IDs, and 57 Notion IDs absent from the pinned source snapshot remain review cases.

First priority is the 565 existing placeholders, with a fresh exact-ID lookup, full recursive page read, body/media/privacy preflight, and explicit ownership adoption. The property candidate is ready only after that body's verified receipt exists. The 8 content-bearing existing pages require parity and manual-edit checks; the other 2 cannot be presumed empty. The 1,149 absent numeric IDs are second priority, with a fresh uniqueness query immediately before creation.

## Required code/contracts beyond the property candidate

1. Historical source selection: introduce a separately approved immutable cohort adapter rather than changing `changed_content_ids`. `get_notion_executor.GitPublication` currently accepts actual committed Markdown deltas or exactly two initial records. `get_notion_outbox.markdown_delta`, `verified_publication`, `enqueue`, and `QueuePublication` rely on genuine canonical publication receipts. Historical cohort records must bind the inventory SHA, source commit, path/blob/SHA, canonical identity, approved alias evidence, body/assets coverage and privacy review. No state-only commit may pretend to contain source changes.
2. Identity: `get_notion_sink_plan.note_id` permits numeric strings, while `get_notion_native.note_id` and `get_notion_live_transport.API` enforce 19-digit IDs. Keep those contracts for the numeric cohort; use approved alias resolution rather than widening them to arbitrary old strings. Isolate unresolved/ambiguous IDs and same-ID Notion duplicates. Count the deduplicated canonical cohort before altering the current 2,500-record bound; the 2,924-file count alone is not a reason to raise it. If the actual resolved state exceeds the current 32-MiB/2,500-record envelope, design separately bounded shard/CAS contracts instead of raising limits blindly.
3. Existing placeholder adoption: `get_notion_executor.prepare` and `get_notion_initial_range` currently require owned blocks or a specifically approved prior range; matching the currently observed body never grants ownership. Add an exact empty/placeholder adoption scope with page/snapshot/source bindings and proven generated placeholder roots. Preserve title/properties and every unowned block. Use no destructive body replacement for an ambiguous or manually edited page. The coordinator has assigned this implementation separately.
4. Body fidelity: retain `get_notion_sink_plan.render_markdown`, native block comparison, list-boundary rules, rich-text semantics and unsupported-format holds. `get_notion_create_batches` currently handles long NEW pages only; long existing-page repairs need a separate resume-safe owned-region batch contract. Do not flatten unsupported blocks to text or re-use a new-page creation receipt to own an old page.
5. Assets: existing PNG preservation/upload modules are pinned to narrow cohorts. General historical images/audio/attachments need per-asset committed byte/hash/provenance evidence, destination-specific privacy approval and native readback. A URL in Markdown is not proof of a durable attachment. Expiring audio URLs and credentials must not be automatically exported or logged. Hold unsupported assets rather than claiming full fidelity.
6. Resume and accounting: all body transactions and property transactions share the existing durable remote journal/lease. Content receipt completion, dequeue and asset attachment binding remain atomic. Metadata-only success never drops body pending work. Persist coverage states independently: source inventoried, identity resolved, body verified, properties verified/held, assets verified/held, privacy reviewed/held, excluded empty version. Complete only against this matrix, not the count of pages created.
7. Privacy: preserve `get_notion_content_scope`, exact credential projection and prior denials. Current ordinary-note fallback is not a historical privacy review. A full historical manifest must explicitly bind source review for the destination and exact version. Sensitive or highly sensitive data remains subject to the existing policy and cannot be cleared by a broad source inventory request.

## Exact empty-version exclusion

The confirmed genuinely empty source/page selected for recoverable trash needs an explicit exclusion receipt binding canonical ID, source path/blob/SHA, exact page ID, archive action and fresh `in_trash`/archived readback. Exclusion means “this verified empty version was intentionally not recreated,” never “body synchronized successfully.” Do not clear pending work to fabricate a successful sync. A changed source blob/hash invalidates the exclusion and requires a fresh source/content review; future content must not be suppressed forever. The existing 14-second audio/status note is nonempty and belongs in original-page repair, not the trash cohort.

## Source metadata completeness

The four mapped Notion columns plus existing title/ID do not make Git's flat Markdown headers a lossless source representation.

- Canonical Markdown has 21 standard fields: source_system, note_id, title, note_type, source, created_at, updated_at, tags, topics, is_child_note, parent_id, children_ids, share_id, web_url, web_domain, web_excerpt, audio_url, audio_duration, audio_transcript, attachments.
- Canonical registry metadata retains original tag/topic objects, including original IDs and tag category such as system/AI. The new Notion `标签` field displays names only; original IDs/categories remain in the private registry and are not claimed to be represented in that column.
- Registry timestamps retain explicit timezone, created/user-edited/record-updated distinctions. The mapper uses the user-created/user-edited values only after exact Markdown consistency checks.
- Some audited recordings have empty flat `audio_url` and zero `audio_duration`, while registry attachment objects retain an audio URL, title and duration in milliseconds. The attachment registry is authoritative evidence for those fields; the flat zero/empty values cannot justify “no audio.” Expiring signed URLs are not copied into Notion by this patch.
- Registry structures may also retain timed transcript sentences with speaker attributes, chapter timelines, topics, child counts, content/metadata completeness states, display title, entry type, version, reference, timeline and opaque source metadata. These remain raw private evidence unless a separate renderer/schema is approved and verified. Original audio and binary attachment fidelity are separate acceptance dimensions.
- Final reporting must say which data is retained privately, rendered in body, mapped into columns, safely omitted, or still held. No blanket “all properties complete” claim is warranted from six visible columns.

## Rollout tests

- All frozen aliases versus canonical identity; equal text with different tags/media must not alias; ambiguous matches must remain isolated.
- Complete fresh pagination and exact-ID lookup before each create/adoption; a page created after inventory must be detected and reused/held, never duplicated.
- Every placeholder source is proven nonempty, every empty exclusion is version-bound, every media item is accounted for.
- Before/after hashes for owned and protected blocks; title/manual-property preservation; timezone/tag/type fidelity; manual edit races between prepare, durable in-flight checkpoint and write.
- Crash before/after every checkpoint, ambiguous HTTP timeout/429/5xx, readback mismatch, source changes and expired assets. Never retry an ambiguous create/append/PATCH automatically.
- Existing-page long-body range resume, exact per-request caps, journal-size/call/time headroom, stable no-op repeat and no starving held records.
- Independent review of combined runtime/controller/policy anchors, clean-runner hashed dependency install, and manual canary while idle before schedule activation.
