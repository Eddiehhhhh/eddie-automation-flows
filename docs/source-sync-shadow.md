# Get source shadow acceptance

Status: Draft review. No production schedule, private-vault checkout, publication or state advancement is enabled by this change. The existing flomo sync stays as it is on `main`; this PR no longer changes it.

## Current bounded entry

`.github/workflows/source-sync-shadow.yml` is manual only. Its default `sample` mode reads at most two Get list pages (configurable to one, two or three), selects at most one `plain_text` note and one `recorder_audio` or `recorder_flash_audio` note, fetches at most two details, and repeats the same bounded sample in a fresh private temporary directory. If both types are not present within the page cap, it fails without claiming success. No source content enters the public checkout, artifact or log.

Sample mode caps Get list and detail responses at 2 MiB each, all attachment descriptors at two, and downloaded image/file assets at two, 2 MiB each and 4 MiB per pass, even if broader command-line limits are supplied. Audio/video bodies are skipped. HTTPS redirects, public-IP resolution, TLS hostname checks, path traversal, symlink and timeout rules still apply. The workflow caps each sample command at 12 minutes. A failed asset fetch or pending transcription fails the run; a prior complete recording Markdown is never replaced by a pending response.

The result explicitly says `scope: sample`, `source_complete: false`, and `status: sample_ok` only when both bounded reads produce identical file hashes and mtimes. Its receipt also has `scope: sample` and `complete: false`. A sample is **not** evidence of complete Get enumeration, historical edit coverage or production readiness. The fixed output allowlist contains only mode, counts and status. Raw details, original transcripts, note IDs, titles, attachment URLs and credentials stay in the temporary directory and are deleted with it.

On failure, the workflow now publishes a fixed `stage` and `error_code` plus bounded counts when known. These classify the failure without echoing child stdout, stderr, exception text or source fields. A missing or malformed private report becomes `invalid_report`; an unrecognized child failure becomes `unknown_failure`. A diagnostic narrows the next investigation but does not itself prove which private source value caused the failure.

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
