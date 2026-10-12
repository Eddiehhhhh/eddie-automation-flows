# Get source shadow acceptance

Status: Draft review. No production schedule, private-vault checkout, publication or state advancement is enabled by this change. The existing flomo sync stays as it is on `main`; this PR no longer changes it.

## Current bounded entry

### Existing private pair (no Get list discovery)

The explicit `existing_sample` workflow mode / `--sample-existing` selects one
`plain_text` and one `recorder_audio`/`recorder_flash_audio` note from the existing
private `Raw/03 Get/03 State/manifest.json`. It sorts eligible relative paths in
the existing `03 纯文本记录` and `99 录音卡笔记` categories and freezes the first
of each type before either pass. Voice-input folders are not recording-card
samples. No actual IDs, titles, private
commit SHA, paths or contents belong in this public repository or workflow inputs.

The already-existing `PRIVATE_VAULT_TOKEN` is used only for five fixed-host GitHub
reads: verify the repository remains private, pin `main` to a commit, read the
existing manifest, and read two selected Markdown files at that same commit.
The manifest is limited to 2 MiB / 5,000 entries; each Markdown is limited to
2 MiB. Reads reject redirects and environment proxies, have a 20-second hard
deadline and 10-second socket timeout, and do not retry. The documented GitHub
raw Contents media type handles the existing manifest above 1 MiB. There is no
vault clone, private repository write, new manifest, new secret or new grant.

The two Markdown files seed a private temporary shadow at their existing paths.
Headers must agree with manifest string IDs and raw note types. A temporary,
owner-readable-only pair file then selects exactly two Get details per pass,
with zero Get list calls. A string `note_id` is authoritative over a potentially
rounded numeric `id` alias; conflicting string aliases are rejected. Returned
IDs, types and content fields must match. The private-vault token is removed
from the Get subprocess environment. Existing bounded Get retries and attachment
budgets remain unchanged; audio/video remain links, never binary downloads.

Both passes use the same pair. The safe summary reports `sample_pages_limit: 0`,
`seeded_existing_notes: 2`, and `legacy_paths_preserved`, alongside the existing
two-pass hashes/mtimes and counters. A disappeared ID, wrong type, missing
transcript, unavailable/oversized private index, malformed header or failed asset
fails closed; it does not silently choose replacement notes or page the source.
All selected content and identity metadata are deleted with the temporary folder.

This proves only the two selected notes and old-path reuse when the real run
passes. The private index can be stale; it is sample discovery, not completeness
evidence. Synthetic transcript and layout tests are not real-source acceptance.
Production still requires full-source, real-asset, single-writer and destination
readback evidence. The production Get workflow remains untouched and disabled.

Validation: `python3 -m unittest discover -s tests -v`; targeted regressions are
in `tests/test_source_sync_sample.py`. They cover strict string identity, zero
list discovery, two details, old paths, original transcript boundaries, link-only
audio, repeat idempotence, pinned private reads, byte/entry caps, redirects,
malformed sources and private-output redaction.

`.github/workflows/source-sync-shadow.yml` is manual only. Its default `sample` mode reads at most two Get list pages (configurable to one, two or three), selects at most one `plain_text` note and one `recorder_audio` or `recorder_flash_audio` note, fetches at most two details, and repeats the same bounded sample in a fresh private temporary directory. If both types are not present within the page cap, it fails without claiming success. No source content enters the public checkout, artifact or log.

Sample mode caps Get list and detail responses at 2 MiB each, all attachment descriptors at two, and downloaded image/file assets at two, 2 MiB each and 4 MiB per pass, even if broader command-line limits are supplied. Audio/video bodies are skipped. HTTPS redirects, public-IP resolution, TLS hostname checks, path traversal, symlink and timeout rules still apply. The workflow caps each sample command at 12 minutes. A failed asset fetch or pending transcription fails the run; a prior complete recording Markdown is never replaced by a pending response.

The result explicitly says `scope: sample`, `source_complete: false`, and `status: sample_ok` only when both bounded reads produce identical file hashes and mtimes. Its receipt also has `scope: sample` and `complete: false`. A sample is **not** evidence of complete Get enumeration, historical edit coverage or production readiness. The fixed output allowlist contains only mode, counts and status. Raw details, original transcripts, note IDs, titles, attachment URLs and credentials stay in the temporary directory and are deleted with it.

On failure, the workflow now publishes a fixed `stage` and `error_code` plus bounded counts when known. These classify the failure without echoing child stdout, stderr, exception text or source fields. A missing or malformed private report becomes `invalid_report`; an unrecognized child failure becomes `unknown_failure`. A diagnostic narrows the next investigation but does not itself prove which private source value caused the failure.

Existing-pair runs also compare the two bounded raw-cache objects in memory. The
public report includes only fixed field-group change counts, whether content and
the original transcript stayed equal, and a count of audio URLs whose query alone
changed. No URL, value, private field name, hash, ID or path is exposed. These diagnostics do not relax the exact hash-and-mtime acceptance test.
Known signing refreshes are handled by the narrow retention policy below;
unrecognized query changes and real source edits continue to fail repeat acceptance.

### Bounded complete-list inventory before migration replay

The explicit `inventory` workflow choice runs `source_sync_inventory.py`. It
verifies the existing private repository, pins its main commit, and reads only
its bounded metadata manifest (three GitHub reads). The current source list is
then enumerated with a hard 300-page / 5,000-unique-note ceiling, 2 MiB per API
response, 64 MiB total API response bytes including retried API responses, and a
10-minute workflow command limit. These caps leave growth room above the existing
private-index scale; a cap is not permission to truncate and claim completeness.
One extra byte may be read solely to detect an exceeded byte boundary.

The inventory does not call the Get detail endpoint or download binary assets.
Incidental content present in list responses is discarded with each page; only
ID sets and the private path mapping remain in memory. It does not write notes,
state or remote data. Private IDs, paths, source fields, API cursors and credentials
are never printed or uploaded. The stdout publisher uses a fixed allowlist of
counts and statuses. Non-numeric local IDs and missing-ID records are counted
separately for preservation, never treated as Get API IDs. Duplicate API identities
or manifest paths, malformed paths, cursor cycles, repeated IDs, missing terminal
pagination or exceeded limits fail closed.

A successful result is `inventory_ok`, `enumeration_complete: true`, with counts
for source IDs, overlap with the prior API index, new API IDs, and prior IDs not
seen in the current enumeration. An absent prior ID is not a deletion instruction.
Every inventory result remains `source_complete: false` and `published: false`:
list completeness is not detail completeness, full existing-vault path replay,
attachment transfer, or destination publication/readback acceptance. The existing
`full` mode still starts empty and lacks these migration guarantees; do not use it
as evidence that the old vault has been safely migrated.

### Pinned old-path map and a real binary sample

`migration_check` is the bounded next gate after inventory, not a full migration.
It pins the private repository commit, walks only the root/Raw/Get Git trees,
and validates the Get tree and manifest using seven bounded metadata reads.
Trees are capped at 6,000 entries and 256 MiB declared prior blob bytes, with
2 MiB maximum tree/manifest responses (1 MiB for parent trees). Symlinks,
submodules, unsafe paths, Unicode/case path aliases and missing indexed paths
fail closed. Private object IDs and paths never reach the public report.

Every existing Markdown path gets an explicitly synthetic placeholder in a
temporary directory. The actual identity resolver must preserve all indexed API
paths despite a changed candidate title/date and avoid overwriting occupied
paths. Non-API IDs, missing-ID records and unindexed Markdown remain reserved.
This tests the complete manifest-to-path mapping only: placeholders are not
evidence that every real old Markdown header/body is valid or migrated.

At most three indexed image-note candidates are inspected, then one selected
note is read again: at most four Get detail calls, 2 MiB per response and 12 MiB
aggregate response bytes including API-level retries. No list re-enumeration is
needed. A candidate must have one or two supported image/file descriptors only.
The selected real old Markdown is read at the pinned commit (2 MiB cap), checked
against the Git blob hash/size and its actual identity header before replay.

The binary check uses the production asset, URL-retention, Markdown and atomic
storage helpers, not the full sync CLI. Each pass downloads fresh URLs with a
2 MiB per-file / 4 MiB per-pass / two-asset ceiling. Two passes therefore permit
at most 8 MiB of successful assets; three bounded transport attempts per asset
bound body reads to 24 MiB plus 12 overflow-detection bytes. Existing per-attempt
30-second limits and redirect/SSRF restrictions remain. The command stops after
12 minutes. No audio/video bodies are downloaded. No new receipt or published
watermark is written, and failed acquisition cannot change note/cache manifests.
Successful assets are read back and checked for size/hash/MIME and rendered local
links. Both whole temporary-tree hashes and mtimes must match on the repeat;
all unselected placeholders must remain unchanged. Private temporary files are
removed and only allowlisted aggregate evidence is emitted.

Even success says `full_existing_body_replay: false`, `source_complete: false`
and `published: false`. Full real-header/body replay, the inventory's absent old
API identity, all source details, private destination readback and single-writer
cutover remain separate gates. A failed bounded candidate must be diagnosed,
not silently bypassed by an unbounded search or larger download.

### Bounded real Get subtree seeding by exact blob objects

`pack_seed` tests a narrower alternative to normal sparse checkout. Cone-mode
sparse checkout includes files in ancestor directories, and blobless clones
still retrieve repository tree metadata; neither alone proves Get-only transfer.
The seed instead reuses the pinned, validated Get tree manifest, then requests
only its known blob OIDs from the same repository's Git upload-pack. It requests
protocol v2, accepts the standard smart-HTTP service preamble, and can use legacy
smart HTTP only when the server explicitly advertises reachable-SHA wants and
bounded sideband support. It never assumes access to unadvertised blob objects.
The existing runner token is used in memory on that fixed HTTPS repository only;
there are no credential helpers, token files, authenticated child processes,
redirects, environment proxies, remotes or persistent authorization changes.

A first request must prove exact-object support with one Get Markdown blob of
at most 64 KiB (2 MiB probe-pack cap). Only then is one whole Get blob pack requested:
at most 6,000 files/objects, 128 MiB pack payload, 129 MiB framed response, 32 MiB
per expanded blob and 256 MiB total seeded files. A fetch has a five-minute
deadline; the whole command stops at 15 minutes. No full-clone or larger-pack
fallback is allowed. Rejected wants, unexpected sections/URIs, unsupported
protocol, excess object count or any limit cause failure, not a complete claim.

An isolated empty Git object store indexes the pack with strict validation,
1 GiB child memory, 300 MiB child file-size and 120-second CPU limits. The complete
object-name/type/size set must exactly equal the requested pinned Get blob set.
Every expanded file is SHA-1 Git-blob verified before use, and no checkout,
filter, hook, submodule or source content is executed. Aggregate temporary disk
is bounded by the pack, verified object-output and 256 MiB target limits (under
1 GiB); all private data is removed with the job's temporary directory.

The full real Markdown tree is then scanned by the actual identity resolver and
all prior manifest API paths must match. This establishes real baseline bytes
and headers, unlike the earlier placeholders. It still does not fetch current
Get details, publish the destination, advance any receipt, or establish a safe
production cutover. Only aggregate counts/flags are emitted, never object IDs,
paths, source contents or credentials.

Protocol and behavior references: [Git protocol v2](https://git-scm.com/docs/gitprotocol-v2),
[Git sparse-checkout semantics](https://git-scm.com/docs/sparse-checkout.html),
[GitHub partial clone behavior](https://github.blog/open-source/git/get-up-to-speed-with-partial-clone-and-shallow-clone/).

### Expiring attachment links and exact repeats

The [real parameter diagnostic](https://github.com/Eddiehhhhh/eddie-automation-flows/actions/runs/38260739263)
confirmed one attachment `url` changed only `Expires` and `Signature`, with a
stable `OSSAccessKeyId`, no unknown query keys, and at least one day of remaining
validity. The three names and Unix-second expiry follow the
[OSS V1 signed URL contract](https://www.alibabacloud.com/help/en/oss/developer-reference/ddd-signatures-to-urls).
Public reports contain only allowlisted parameter names and coarse validity
counts, never their values, hosts, paths or source IDs.

`source_sync_urls.py` recognizes only credential-free HTTPS URLs containing
exactly one of each of those three parameters. It never strips query parameters
or reconstructs a URL. A previous complete URL is retained only when its exact
location and signer match, it remains valid for more than 12 hours, the newly
returned expiry is no earlier, every source field and attachment descriptor
(including unknown fields, IDs and order) is otherwise identical, and the acquired
asset results are identical. Unknown query parameters, other signing schemes,
malformed or short-lived URLs are not normalized.

Image/file acquisition always uses the newly returned URL before retention is
considered. Changed bytes at the same location have a different content hash
and cannot be hidden by URL retention. Audio/video remain link-only, so their
binary contents are not verified; their source fields and descriptors must match.
A signer, path, content, transcript, timestamp, descriptor or binary change adopts
the complete fresh source response. At 12 hours remaining or less, the current
URL replaces the prior one in Markdown, private raw-cache and private attachment
manifest together. No stale URL is pinned indefinitely. Refresh requires another
successful sync; this is not an always-live link service or evidence that an old
signature has not been independently revoked.

The private raw-cache is now a retained source snapshot: all source content is
preserved, but an unexpired signing URL may come from a previous response under
this policy. It is not an audit log of the latest transport/signature metadata.
No extra private artifacts or URL maps are published. `attachment_urls_reused`
reports per-pass bounded counts so a stable retained snapshot is not mistaken for
proof that the source signing parameters themselves stayed identical. Exact
whole-tree hash and mtime comparison, fixed-pair bounds and old-path checks remain
unchanged. Expiry-driven refresh is intentional and may fail a repeat comparison
if the refresh boundary is crossed between its two reads.

The `full` workflow choice and `verify_shadow_repeat.py --full` remain available for future explicit full-library acceptance. They are never the default. Full mode still fails closed on incomplete pagination, malformed details, pending transcription or asset budgets; it does not publish. Full acceptance is separately limited to 55 minutes per command. A successful full shadow is still not a production cutover.

The workflow must exist on GitHub's default branch before `workflow_dispatch` can run. After review, merge only the manual shadow implementation, then dispatch `sample`. Review the allowlisted summary and inspect private acceptance evidence without copying private content into this public repository or logs. Do not infer that ordinary notes or transcript boundaries are complete from the older one-page credential probe.

## Data rules retained from the draft

- Exact source IDs determine paths; title or content similarity never merges distinct IDs. Old paths survive title and date edits. Duplicate or contradictory existing identities fail closed before writes.
- Full reconciliation fetches every listed detail and does not use creation time to skip old edits. Pending recordings are retried; a complete existing recording remains byte-for-byte intact during a transient pending response. Originals, summaries, raw detail and attachment manifests remain separate.
- Authenticated Get requests stay on the fixed official HTTPS host. They disable environment proxies and reject every redirect without forwarding Authorization or X-Client-ID; upstream error messages and HTTP/transport exception URLs are not printed. The existing bounded 429/5xx/transport retries remain in place.
- Image/file binaries use content-addressed private `_assets` paths with hash, size and MIME manifests. The downloader forwards no Get credentials to asset hosts. Audio/video download remains disabled.
- Writes are atomic and unchanged bytes preserve mtimes. A failed asset cannot update note bodies or receipts. The shadow never advances a published watermark. Production publication would need a coordinated single writer and private remote content/state readback; this PR does not enable it.

## Verification and remaining gates

The new sample tests cover page/detail bounds, asset budgets, output scope, private-output suppression and unchanged repeat. Existing focused tests cover identity, delayed transcription, old complete-body protection, attachment failures and concurrent publication rejection. Synthetic tests are not real source or CDN proof.

Before a production cutover, verify a real ordinary body and recording original at both boundaries, a real image/file CDN response within an approved transfer budget, full-list completeness and historical edits, duplicate-ID migration, and single-writer private remote publication with content and state readback. Keep existing workflows in their current state until that acceptance is complete.

Evidence: https://github.com/Eddiehhhhh/eddie-automation-flows/actions/runs/38205977952 is a successful bounded credential/detail probe. It reports one 20-note page, a recording detail, an original transcript field and one attachment URL; it did not find an ordinary note in that first page or validate transcript boundaries or binary downloads.
