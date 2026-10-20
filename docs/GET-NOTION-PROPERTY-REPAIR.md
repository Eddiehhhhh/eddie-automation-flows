# Reviewed property repair candidate

## Status and exact scope

This is an unactivated property candidate based on runtime `6ad627793c23e80ad2ca9568048eb4e595c881a0`. No Notion write or schema change was performed while rebuilding these routing and fixture changes. Property work requires an idle engine journal or its own completely matching property transaction. The combined release separately includes a canonical-only exact run #53 attempt 2 recovery bridge, pinned to its checkpoint/state/blob and 20-verified-record prefix; it changes only the outer policy after durable CAS/readback/checkpoint and cannot authorize property or historical work on that journal. Local recovery tests are not evidence of live recovery.

Runnable paths now exist:

- `get_notion_executor.prepare(..., property_manifest=..., expected_property_manifest_sha256=..., property_schema=...)` adds approved metadata to new-page creation. `get_notion_pipeline.run(..., property_manifest=...)` requires the policy's matching `property_manifest_sha256` before enabling this path.
- `get_notion_property_transaction.prepare(...)` plans property-only changes to pages with a verified body receipt for the exact Markdown blob. `get_notion_executor.execute(...)` journals them in the existing engine journal and records independent `property_receipts`. Body receipts, body queue entries and publication cursors do not advance.
- `get_notion_runtime.py --property-manifest PRIVATE_FILE --approved-property-manifest-sha256 HASH` reaches a historical property reader and the real remote-journal transaction adapter. All existing policy, controller, private repository, destination, writer, source and journal provenance checks remain in front of the write.
- Read-only preparation also requires the existing `--policy`, `--approved-policy-sha256`, and `--expected-parent` inputs. Apply retains `--apply --scope-from-policy --publication-file` and authenticated GitHub Actions execution context. It is not a standalone local writer.
- Apply additionally requires the manifest hash to be in `get_notion_properties_runtime.APPROVED_MANIFESTS`. This set is deliberately empty in the candidate. A command-line hash alone cannot activate a batch. The standing scope binds the allowlist digest. Register only independently reviewed private manifests in the approved runtime release; never embed their private contents in public code.
- The same controller exposes manual `notion_mode=property-apply` plus `notion_input_sha256`. The digest must be in that property allowlist and `get_notion_historical_input.INPUT_ANCHORS`, whose exact private commit/blob/path/size registration is independently scope-bound. A credential-free preflight runs before Get; the route then validates the property contract, writes a mode-0600 temporary input, invokes this existing runtime CLI with its confirmed current publication/policy, and deletes the input on success or failure. There is no separate property writer or arbitrary-path/commit input. Scheduled and default dispatches remain canonical. Both registrations are empty in this candidate.
- An active engine journal is accepted only for this exact property manifest, unchanged publication, policy and rules. Canonical, historical, or foreign-property journals are rejected. Read-only evidence capture is a distinct permission and cannot activate property repair.

The default scheduled runtime also enables `get_notion_creation_properties.SCOPE` for new pages from a verified canonical outbox publication, without per-note manifests. The fixed scope is bound into the standing runtime policy and each immutable create plan. It accepts only the four typed existing columns/options, binds the exact source version and observed schema digest, and checks schema again before creation. It loads only the source commit's exact registry blob when needed; only matching timestamps are projected. Unknown timezone or unavailable options remain held and the successful body transfer is reported partial. Initial/historical cohorts cannot enter this standing scope, and existing-page property changes still need the exact repair manifest and ownership evidence. No privacy review hash is fabricated.

The registry/source/schema projection is not a new data-sharing authorization: existing per-version content/privacy/credential gates run unchanged before each eligible note. The scope adds neither audio URLs nor tag IDs/categories. New-page metadata receipts bind the standing scope hash in their existing `manifest_sha256` slot; historical repair receipts bind the exact manifest hash there. In both cases source commit/blob and page identity remain independent bindings.

The historical placeholder body writer is a separate implementation. This candidate cannot make an unowned old page writable by presenting its newly observed hash. Body adoption must finish and yield a verified receipt before the property-only runtime can repair that page.

## Typed mapping and ownership

- `created_at` → `创建时间` (`date`)
- `updated_at` → `更新时间` (`date`)
- `tags` → `标签` (`multi_select`)
- `note_type` → `笔记类型` (`select`)

`标题` and `笔记 ID` retain their existing creation behavior; metadata repair never changes them. All unrelated properties and all body blocks are compared before/after and preserved exactly.

A property may change only if it is empty under the explicit fill-empty scope, or equals this mapper's last verified submitted value. Nonempty unowned values and manual changes are held. Matching the desired value does not itself create ownership. An optional per-record `property_adoption` can bind exact page, snapshot, field baselines and independently reviewed prior-writer proof; it cannot override an already-managed field or bypass a changed snapshot. This is needed to safely increase precision on old generated date-only fields without overwriting manual dates.

Receipts with held fields remain visible on repeated runs: the plan keeps their identities in `skipped_conflicts` and the execution remains partial. Retry needs a newly reviewed manifest, such as a new schema hash or approved exact property-adoption scope. A repeat never turns held work into a verified completion. Short and batched new-page creation also report partial when source metadata fields are held, while preserving the successfully verified body receipt.

## Private manifest contract

Contract: `get-notion-property-scope-1`.

Top-level exact fields: `contract`, `source_commit`, `data_source_id`, `schema_sha256`, `fields`, `fill_empty`, `records`, `registry`.

- `fields` is exactly the sorted four names above; `fill_empty` must be true.
- Each `records[note_id]` binds `path`, `blob`, `sha256`, `privacy_review_sha256`; optional `property_adoption` binds `page_id`, `snapshot_sha256`, `prior_writer_proof_sha256`, and exact `fields` baselines.
- `registry` is null or an exact path/blob/SHA binding for `Raw/03 Get/03 State/canonical-registry.json` from the same source commit.
- Runtime batches remain at most 10 notes. The pure engine maximum remains 50. The inventory's 25-note batches must be split before property-runtime use.
- The schema digest covers stable property IDs/types and existing select/multi-select option IDs/names. A fresh different schema stops before a page write.
- `privacy_review_sha256` must identify actual independent review of that exact immutable source version. A generated checksum is not privacy approval. Historical sensitive notes must not inherit review from newer ordinary notes or from the broad inventory request.

## Dates and legacy tags

Existing explicit ISO timezone offsets are preserved. Naive canonical date strings require registry `newById[note_id].source_metadata.timestamps.created_at` / `edited_at`; the registry row must bind the exact Markdown blob, SHA and path, and the wall clock must match. `record_updated_at` is never substituted for the user edit time. Old nonnumeric aliases have a verified historical eight-hour offset relative to their canonical numeric counterparts; alias times must not overwrite canonical dates.

Legacy tags sometimes contain a YAML string whose contents are a YAML flow list. The decoder performs bounded safe YAML parsing and accepts only unique nonempty strings; aliases, anchors, explicit tags, nested values, coercions and excessive input are rejected. It does not split on commas.

Dependency: PyYAML 6.0.3. `requirements-notion-properties.txt` contains the official PyPI CPython 3.11/3.12 Linux x86-64 wheel hashes. The controller template, migration safety workflow and aggregate WeRead canonical-path workflow install it with `--require-hashes --only-binary=:all: --no-deps`; the runtime verifies the lock's SHA and exact parser version. The WeRead workflow also triggers on lock-file changes. Other interpreter/platform wheels are intentionally not authorized. The scheduled runtime checks the locked dependency before enqueue/checkpoint or page writes. The fresh-runner installation was not run from this workspace; CI must verify it before release. Sources: https://pypi.org/project/PyYAML/6.0.3/#files and https://pyyaml.org/wiki/PyYAMLDocumentation.

## Schema gate

This patch does not add schema options. Missing source note types or tag options produce `property_option_missing`; no value is coerced to `plain_text`. Before the intended first repair batch, the coordinator must review a source-derived additive option delta, apply it through the authorized Notion schema connector, preserve every existing property/option, and verify readback. Then re-read the schema and approve the new manifest hash. If the connector denies the action, stop rather than substituting another route.

## First live batch and acceptance

1. Finish the current body transaction and verify the journal is idle. Merge the independently reviewed historical adoption and canonical-efficiency changes, then rerun all focused and aggregate tests on the final combined bytes.
2. Verify the dependency lock installation in a clean Python 3.11 Ubuntu runner and regenerate runtime/controller/rules/scope anchors. No old in-flight plan may be relabeled under new rules.
3. Resolve required additive schema options, with fresh readback; separately approve exact privacy and ownership evidence for the selected sources/pages.
4. Select up to 10 unambiguous existing pages. For placeholders, use the separately reviewed body adoption flow first. For existing owned pages, require body/source parity before metadata repair.
5. Prepare the private manifest with zero external writes, inspect exact proposed fields and held reasons, register only its approved hash, and apply under the existing controller and remote journal.
6. Verify native body/block preservation, every mapped property value, unchanged title/identity/manual fields, source and registry hashes, and durable metadata receipts. Repeat the same batch: zero page writes, held fields still reported.
7. Expand only to remaining reviewed manifest batches. Do not claim the full library complete while duplicate IDs, unresolved aliases, media, privacy holds, unsupported fields or empty-source exclusions remain unresolved.

## Safety/fidelity not expanded

Existing 100-block/400-KiB write envelopes, 32-batch/3,200-block new-page bounds, 10-note runtime batches, 2,000 request/20-minute transport limits, exact source validation, one attempt per uncertain write, old-writer guards, private-only journals, and no-body logging remain. Metadata requests are additionally limited to 32 KiB. A confirmed schema change before dispatch resets the operation to prepared without claiming a network attempt. An uncertain PATCH is never repeated: exact readback may establish its effect; unchanged or conflicting readback stays unresolved.
